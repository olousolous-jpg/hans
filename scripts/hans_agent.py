"""HANS_AGENT_V1 — agentní vrstva: kontextové akce z konverzace.

Uživatel v chatu napíše „je tu tma" a Hans podle KONTEXTU pozná, že by
ocenil rozsvícení, a NAVRHNE akci („rozsvítím ti obývák? [ano/ne]") — ne
jen odpoví textem. JEDNA zastřešující vrstva pro vše: 1 LLM router, 1
whitelist akcí, 1 confirm smyčka, 1 deník. Přidat akci = přidat 1 spec.

Tok (v `openwebui_direct_handler.send_chat_message`, PO parse_command):
  1. pending confirm? (osoba má návrh + řekne ano/ne) → proveď / zruš
  2. pre-gate (deterministické hinty) → věrohodně akce? jinak běžný chat
  3. LLM router (hans-czech, rezidentní) → {action,args,confidence,propose_text}
  4. validace: whitelist + práh + cooldown + anti-echo + grounding argumentů
  5. návrh (propose_text) jako Hansova odpověď + ulož pending → čeká na ano

Pojistky (proti otravování + konfabulaci akcí, vzor anti-konfab principu):
  - whitelist strict (router nezná nic mimo něj)
  - vždy confirm (human-in-the-loop jako Severka)
  - confidence práh (pod → mlčí, běžný chat)
  - cooldown per (akce, args) + anti-echo po odmítnutí
  - grounding argumentů (film jen z knihovny; neznámý → nenavrhne)
  - jen aktivní konverzace (send_chat_message = někdo píše, ne do prázdna)
  - deník `agent_action` (návrh + výsledek → pozdější tuning)
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from typing import Callable, Optional

log = logging.getLogger(__name__)

_YES = {"ano", "jo", "jasně", "jasan", "pusť", "pust", "spusť", "spust",
        "dej", "davej", "dávej", "ok", "okej", "tak jo", "prosím", "prosim",
        "sure", "yes", "můžeš", "muzes", "do toho", "platí", "plati", "beru"}
_NO = {"ne", "nech", "nechci", "nemusíš", "nemusis", "raději ne", "radeji ne",
       "zruš", "zrus", "ne díky", "ne diky", "nedávej", "nedavej", "no",
       "later", "teď ne", "ted ne", "ne teď", "ne ted"}


def _norm(s: str) -> str:
    return (s or "").strip().lower().strip("!?.,")


def _args_hash(action: str, args: dict) -> str:
    raw = action + "|" + json.dumps(args or {}, sort_keys=True,
                                    ensure_ascii=False)
    return hashlib.md5(raw.encode("utf-8")).hexdigest()[:12]


# ── Registr akcí (whitelist) ─────────────────────────────────────────────────
# Každá akce: hints (pre-gate slova), args (co router vyplní), needs_confirm,
# cooldown_s, grounding(handler,args)->(ok,resolved_args,msg), run(handler,args)
# ->str. grounding=None → argumenty se neověřují (bez-argumentové akce).

class Action:
    def __init__(self, aid, desc, hints, args, run,
                 grounding=None, needs_confirm=True, cooldown_s=60):
        self.id = aid
        self.desc = desc          # popis pro LLM router
        self.hints = hints        # pre-gate klíčová slova (lower, bez diakr.)
        self.args = args          # jména argumentů
        self.run = run            # (handler, args) -> str
        self.grounding = grounding
        self.needs_confirm = needs_confirm
        self.cooldown_s = cooldown_s


# ── Handlery akcí ────────────────────────────────────────────────────────────

def _run_kodi_play(handler, args) -> str:
    m = args.get("_movie") or {}
    mid, title = m.get("movieid"), m.get("title", args.get("titul", ""))
    kodi = getattr(getattr(handler, "_hans_idle", None), "kodi", None)
    if not kodi or mid is None:
        return "Bohužel se mi teď nedaří k přehrávači připojit, pane."
    ok = kodi.play_movie(mid)
    return (f"Pouštím „{title}“." if ok
            else "Nepodařilo se mi film spustit, pane.")


def _ground_kodi_play(handler, args):
    title = (args.get("titul") or "").strip()
    if not title:
        return False, args, "bez názvu"
    kodi = getattr(getattr(handler, "_hans_idle", None), "kodi", None)
    if not kodi:
        return False, args, "kodi nedostupné"
    m = kodi.find_movie(title)
    if not m:
        return False, args, "film není v knihovně"
    args["_movie"] = m
    args["titul"] = m.get("title", title)  # kanonický název z knihovny
    return True, args, ""


def _run_sleep(handler, args) -> str:
    hi = getattr(handler, "_hans_idle", None)
    rt = getattr(hi, "_routine", None) if hi else None
    if not rt or not hasattr(rt, "set_manual_sleep"):
        return "Uspat se teď neumím, pane."
    try:
        rt.set_manual_sleep(True)
        return "Dobře, ztiším se a odpočinu si. Kdykoli mě probuďte."
    except Exception:
        return "Uspání se nezdařilo, pane."


def _run_book_wishlist(handler, args) -> str:
    title = (args.get("titul") or "").strip()
    if not title:
        return "Který titul mám přidat, pane?"
    try:
        from scripts.hans_art import add_to_wishlist
        dbp = _diary_path(handler)
        res = add_to_wishlist(dbp, title)
        if res == "exists":
            return f"„{title}“ už na svém seznamu ke čtení mám."
        return f"Přidal jsem „{title}“ na seznam ke čtení."
    except Exception as e:
        log.warning("book_wishlist: %s", e)
        return "Přidání na seznam se nezdařilo, pane."


def _ground_book(handler, args):
    title = (args.get("titul") or "").strip()
    if len(title) < 2:
        return False, args, "bez názvu"
    return True, args, ""


ACTIONS: dict[str, Action] = {
    "kodi_play_film": Action(
        "kodi_play_film",
        "Pustit konkrétní film na TV. Argument 'titul' = název filmu, který "
        "uživatel chce vidět (musí být v knihovně).",
        hints=["film", "pust", "pusť", "koukni", "podivat", "podívat",
               "smrtonosn", "bond", "sledovat", "na tv", "dej to", "spust"],
        args=["titul"], run=_run_kodi_play, grounding=_ground_kodi_play,
        needs_confirm=True, cooldown_s=300),
    "hans_sleep": Action(
        "hans_sleep",
        "Uspat sebe (Hanse) — ztišit se, přestat mluvit. Když uživatel řekne "
        "ať jde spát / ať je ticho / dobrou noc s přáním klidu.",
        hints=["spát", "spat", "spi", "ztich", "ticho", "klid", "unaven",
               "dobrou noc", "jdi spat", "bež spát", "odpočin"],
        args=[], run=_run_sleep, grounding=None,
        needs_confirm=True, cooldown_s=120),
    "add_book_wishlist": Action(
        "add_book_wishlist",
        "Přidat knihu na seznam ke čtení. Argument 'titul' = název knihy, "
        "kterou uživatel zmíní že by si chtěl přečíst / ať přečteš.",
        hints=["kniha", "knihu", "přečíst", "precist", "číst", "cist",
               "na seznam", "wishlist", "knížk", "knizk"],
        args=["titul"], run=_run_book_wishlist, grounding=_ground_book,
        needs_confirm=True, cooldown_s=30),
}


def _diary_path(handler) -> str:
    hi = getattr(handler, "_hans_idle", None)
    db = getattr(hi, "_db", None) if hi else None
    if db is not None:
        # sqlite3.Connection — potřebujeme cestu; zkus config
        pass
    cfg = getattr(handler, "config", {}) or {}
    return (cfg.get("diary", {}) or {}).get("db_path", "data/hans_diary.db")


class Proposal:
    def __init__(self, action: Action, args: dict, text: str,
                 confidence: float, reason: str = ""):
        self.action = action
        self.args = args
        self.text = text
        self.confidence = confidence
        self.reason = reason
        self.ts = time.time()
        self.hash = _args_hash(action.id, {k: args.get(k) for k in action.args})


class AgentRouter:
    """LLM router + confirm smyčka + cooldown + deník. Instanci drží handler."""

    def __init__(self, config: dict):
        self.config = config or {}
        c = (config.get("agent", {}) or {})
        self.enabled = bool(c.get("enabled", False))
        self.threshold = float(c.get("confidence_threshold", 0.7))
        self.context_msgs = int(c.get("context_msgs", 5))
        self.model = c.get("model", "hans-czech:latest")
        self.num_predict = int(c.get("num_predict", 160))
        self.temperature = float(c.get("temperature", 0.1))
        self.timeout = int(c.get("timeout", 30))
        self.cooldown_default = int(c.get("cooldown_default_s", 60))
        self.reject_cooldown = int(c.get("reject_cooldown_s", 3600))
        self._pending: dict[str, Proposal] = {}       # name → návrh
        self._last_fire: dict[str, float] = {}         # hash → ts (cooldown)
        self._rejected: dict[str, float] = {}          # name|hash → ts (echo)

    # ── pre-gate ────────────────────────────────────────────────────────────
    def _actionable(self, text: str) -> bool:
        t = _norm(text)
        if len(t) < 2:
            return False
        for a in ACTIONS.values():
            for h in a.hints:
                if h in t:
                    return True
        return False

    # ── confirm smyčka ──────────────────────────────────────────────────────
    def check_confirmation(self, handler, name: str,
                           message: str) -> Optional[str]:
        """Má osoba čekající návrh a odpovídá ano/ne? → proveď/zruš, vrať text.
        Jinak None (žádný pending / nejednoznačné → nechá projít do chatu)."""
        pend = self._pending.get(name)
        if not pend:
            return None
        # návrh vyprší po 3 min bez odpovědi
        if time.time() - pend.ts > 180:
            self._pending.pop(name, None)
            return None
        m = _norm(message)
        first = m.split()[0] if m.split() else ""
        is_yes = (m in _YES or first in _YES
                  or any(m.startswith(y + " ") for y in _YES))
        is_no = (m in _NO or first in _NO
                 or any(m.startswith(n + " ") for n in _NO))
        if not is_yes and not is_no:
            # nejednoznačné — návrh zahoď a nech projít do běžného chatu
            self._pending.pop(name, None)
            self._log(handler, pend, "ignored")
            return None
        self._pending.pop(name, None)
        if is_no:
            self._rejected[f"{name}|{pend.hash}"] = time.time()
            self._log(handler, pend, "rejected")
            return "Dobře, nechám to být."
        # ANO → proveď
        try:
            result = pend.action.run(handler, pend.args)
        except Exception as e:
            log.warning("agent akce %s selhala: %s", pend.action.id, e)
            result = "Něco se při provádění pokazilo, pane."
        self._last_fire[pend.hash] = time.time()
        self._log(handler, pend, "accepted", result=result)
        return result

    # ── návrh ───────────────────────────────────────────────────────────────
    def propose(self, handler, name: str, message: str) -> Optional[str]:
        """Vrátí propose_text (Hansův návrh + [ano/ne]) nebo None (běžný chat)."""
        if not self.enabled:
            return None
        try:
            if not self._actionable(message):
                return None
            decision = self._route(handler, name, message)
            if not decision:
                return None
            aid = decision.get("action")
            action = ACTIONS.get(aid)
            if not action:
                return None
            conf = float(decision.get("confidence", 0) or 0)
            if conf < self.threshold:
                return None
            args = {k: (decision.get("args", {}) or {}).get(k)
                    for k in action.args}
            h = _args_hash(aid, args)
            # anti-echo: nedávno odmítnuto touž osobou?
            rk = f"{name}|{h}"
            if time.time() - self._rejected.get(rk, 0) < self.reject_cooldown:
                return None
            # cooldown per (akce, args)
            cd = action.cooldown_s or self.cooldown_default
            if time.time() - self._last_fire.get(h, 0) < cd:
                return None
            # grounding argumentů
            if action.grounding:
                ok, args, gmsg = action.grounding(handler, args)
                if not ok:
                    log.info("agent: %s grounding zamítl (%s)", aid, gmsg)
                    return None
            text = (decision.get("propose_text")
                    or self._default_text(action, args))
            text = text.strip()
            if not text.endswith(("?",)):
                text += " Mám to udělat?"
            prop = Proposal(action, args, text, conf,
                            decision.get("reason", ""))
            self._pending[name] = prop
            self._log(handler, prop, "proposed")
            log.info("agent: návrh %s conf=%.2f pro %s", aid, conf, name)
            return text
        except Exception as e:
            log.warning("agent.propose selhalo: %s", e)
            return None

    def _default_text(self, action: Action, args: dict) -> str:
        if action.id == "kodi_play_film":
            return f"Chcete, abych pustil „{args.get('titul')}“?"
        if action.id == "hans_sleep":
            return "Mám se ztišit a jít spát?"
        if action.id == "add_book_wishlist":
            return f"Mám „{args.get('titul')}“ přidat na seznam ke čtení?"
        return "Mám to zařídit?"

    # ── LLM router ──────────────────────────────────────────────────────────
    def _route(self, handler, name: str, message: str) -> Optional[dict]:
        from scripts.ollama_client import ollama_generate
        catalog = "\n".join(
            f"- {a.id}: {a.desc} Argumenty: {a.args or 'žádné'}."
            for a in ACTIONS.values())
        ctx = self._context(handler, name)
        system = (
            "Jsi router akcí pro domácího společníka Hanse. Z POSLEDNÍ zprávy "
            "uživatele (a kontextu) rozhodni, zda si uživatel PŘEJE nějakou "
            "konkrétní AKCI ze seznamu níže. Vrať POUZE JSON, nic jiného.\n\n"
            "AKCE (smíš zvolit JEN z tohoto seznamu, nic nevymýšlej):\n"
            + catalog +
            "\n\nJSON formát: {\"action\": <id akce nebo null>, \"args\": "
            "{...}, \"confidence\": <0.0-1.0>, \"reason\": \"<krátce proč>\", "
            "\"propose_text\": \"<jak se Hans zeptá, česky, uctivě, končí "
            "otázkou>\"}\n"
            "Pravidla: Když si uživatel žádnou akci ze seznamu nepřeje "
            "(běžná otázka, povídání), vrať action=null a confidence 0. "
            "Nikdy nevymýšlej akci mimo seznam. args vyplň jen když je znáš "
            "z textu (titul filmu/knihy). Buď konzervativní — při pochybnosti "
            "action=null.")
        prompt = f"{ctx}\nPOSLEDNÍ zpráva ({name}): {message}\n\nJSON:"
        raw = ollama_generate(
            self.model, prompt, system=system, config=self.config,
            timeout=self.timeout, keep_alive=-1,
            options={"temperature": self.temperature,
                     "num_predict": self.num_predict})
        if not raw:
            return None
        return self._parse_json(raw)

    @staticmethod
    def _parse_json(raw: str) -> Optional[dict]:
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if not m:
            return None
        try:
            d = json.loads(m.group(0))
            if not isinstance(d, dict) or not d.get("action"):
                return None
            return d
        except Exception:
            return None

    def _context(self, handler, name: str) -> str:
        parts = []
        # čas / fáze
        try:
            hi = getattr(handler, "_hans_idle", None)
            rt = getattr(hi, "_routine", None) if hi else None
            if rt and hasattr(rt, "phase_label"):
                parts.append(f"Situace: {rt.phase_label()}.")
        except Exception:
            pass
        # živý stav — co hraje na TV
        try:
            hi = getattr(handler, "_hans_idle", None)
            kodi = getattr(hi, "kodi", None) if hi else None
            if kodi and hasattr(kodi, "get_now_playing"):
                np = kodi.get_now_playing()
                if np and np.get("title"):
                    parts.append(f"Na TV právě hraje: {np.get('title')}.")
                else:
                    parts.append("Na TV teď nic nehraje.")
        except Exception:
            pass
        # posledních N výměn
        try:
            hist = handler.conv_store.get_history(name) or []
            for msg in hist[-self.context_msgs:]:
                role = msg.get("role")
                c = (msg.get("content") or "")[:200]
                who = name if role == "user" else "Hans"
                if c:
                    parts.append(f"{who}: {c}")
        except Exception:
            pass
        return "\n".join(parts)

    # ── deník ───────────────────────────────────────────────────────────────
    def _log(self, handler, prop: Proposal, outcome: str, result: str = ""):
        try:
            hi = getattr(handler, "_hans_idle", None)
            if hi and hasattr(hi, "_log_entry"):
                hi._log_entry(
                    "agent_action",
                    f"{prop.action.id} → {outcome}",
                    data=json.dumps({"action": prop.action.id,
                                     "args": {k: prop.args.get(k)
                                              for k in prop.action.args},
                                     "outcome": outcome,
                                     "confidence": round(prop.confidence, 2)},
                                    ensure_ascii=False),
                    note=(result or prop.text)[:300])
        except Exception:
            pass
