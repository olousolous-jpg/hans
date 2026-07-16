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


# HANS_AGENT_NO_WORDGATE_V1 — strukturální (gramatické) začátky otázek/žádostí.
# NENÍ to seznam spouštěcích slov akcí (to dělá LLM router), jen hrubý signál
# „tohle je dotaz/žádost, ať se Hans zamyslí“. Diakritika i bez ní.
_REQUEST_OPENERS = frozenset({
    # tázací zájmena/příslovce
    "co", "kdo", "kde", "kdy", "kolik", "jak", "proc", "proč", "kam", "odkud",
    "ci", "čí", "jaka", "jaky", "jake", "jaká", "jaký", "jaké", "která", "ktery",
    "který", "které", "kterou",
    # sloveso „být“ na začátku = zjišťovací otázka („je někdo doma“, „jsou tu…“)
    "je", "jsou", "byl", "byla", "bylo",
    # 2. osoba / zdvořilá žádost
    "muzes", "můžeš", "muzeš", "mohl", "mohla", "mohls", "dokazes", "dokážeš",
    "zvladnes", "zvládneš", "prosim", "prosím", "chci", "chtel", "chtěl",
    "potreboval", "potřeboval",
})


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


# ── Info dotazy (instant, bez potvrzení — jen odpoví z živých dat) ───────────

def _run_weather(handler, args) -> str:
    try:
        from scripts.weather_chmu import WeatherCHMU
        _w = (getattr(handler, "config", {}) or {}).get("weather", {}) or {}
        s = WeatherCHMU(lat=float(_w.get("lat", 50.08)),
                        lon=float(_w.get("lon", 14.42))).get_context_string()
        return s.replace("Počasí:", "Za oknem:").strip() if s else \
            "Aktuální počasí se mi teď nedaří zjistit, pane."
    except Exception:
        return "Aktuální počasí se mi teď nedaří zjistit, pane."


def _run_pc_health(handler, args) -> str:
    try:
        from scripts import pc_remote
        lines = pc_remote.display_lines(handler.config)
        if not lines:
            return "Počítač je teď nedostupný — nejspíš spí, pane."
        return "Počítač: " + ", ".join(lines) + "."
    except Exception:
        return "Stav počítače se mi teď nedaří zjistit, pane."


def _run_who_home(handler, args) -> str:
    hi = getattr(handler, "_hans_idle", None)
    names = [n for n in (getattr(hi, "_present_names", None) or [])
             if n and n not in ("Unknown", "?", "")]
    if names:
        if len(names) == 1:
            return f"Vidím tu {names[0]}."
        return "Vidím tu: " + ", ".join(names) + "."
    # fallback — nedávno spatření z deníku (posl. 15 min)
    try:
        import sqlite3
        db = sqlite3.connect(_diary_path(handler))
        r = db.execute(
            "SELECT title FROM diary WHERE event_type='person_seen' "
            "AND ts > ? ORDER BY ts DESC LIMIT 1",
            (time.time() - 900,)).fetchone()
        db.close()
        if r and r[0]:
            return f"Naposledy jsem tu zahlédl {r[0]}, teď tu ale nikoho nevidím."
    except Exception:
        pass
    return "Teď tu nikoho nevidím, pane."


def _run_now_playing(handler, args) -> str:
    kodi = getattr(getattr(handler, "_hans_idle", None), "kodi", None)
    if not kodi:
        return "K přehrávači se teď nedaří připojit, pane."
    try:
        np = kodi.get_now_playing()
    except Exception:
        np = None
    if np and np.get("title"):
        t = np.get("title")
        yr = np.get("year")
        return f"Na TV právě hraje „{t}“{f' ({yr})' if yr else ''}."
    return "Na TV teď nic nehraje, pane."


def _run_home_status(handler, args) -> str:
    """HANS_AGENT_HOME_STATUS_V1 — kompozitní odpověď na VÁGNÍ „děje se něco
    doma?“. Agreguje ŽIVÁ deterministická data (co hraje + kdo je doma), NIKDY
    nedomýšlí. Prázdný zdroj = „klid“, NE staré datum (přesně proti případu
    „Proud krve“, kde chat konfabuloval starý Kodi titul jako přítomnost)."""
    try:
        np = _run_now_playing(handler, {}) or ""
    except Exception:
        np = ""
    try:
        wh = _run_who_home(handler, {}) or ""
    except Exception:
        wh = ""
    _l = lambda s: s.lower()
    plays = bool(np) and "nic nehraje" not in _l(np) and "nedaří připojit" not in _l(np)
    home  = bool(wh) and "nikoho nevid" not in _l(wh)
    parts = []
    if plays:
        parts.append(np.rstrip("."))
    if home:
        parts.append(wh.rstrip("."))
    if not parts:
        return "Doma je klid, pane — na TV nic nehraje a nikoho tu teď nevidím."
    return ". ".join(parts) + "."


# ── Ovládání médií (confirm) ─────────────────────────────────────────────────

def _run_kodi_pause(handler, args) -> str:
    kodi = getattr(getattr(handler, "_hans_idle", None), "kodi", None)
    if not kodi:
        return "K přehrávači se nedaří připojit, pane."
    return "Pozastavuji." if kodi.pause_playback() else \
        "Pozastavení se nezdařilo, pane."


def _run_kodi_stop(handler, args) -> str:
    kodi = getattr(getattr(handler, "_hans_idle", None), "kodi", None)
    if not kodi:
        return "K přehrávači se nedaří připojit, pane."
    return "Zastaveno." if kodi.stop_playback() else \
        "Zastavení se nezdařilo, pane."


def _ground_playing(handler, args):
    """Pauza/stop dávají smysl jen když něco hraje."""
    kodi = getattr(getattr(handler, "_hans_idle", None), "kodi", None)
    if not kodi:
        return False, args, "kodi nedostupné"
    try:
        np = kodi.get_now_playing()
    except Exception:
        np = None
    if not np or not np.get("title"):
        return False, args, "nic nehraje"
    return True, args, ""


# ── Studijní téma z chatu (confirm) ──────────────────────────────────────────

def _run_add_study(handler, args) -> str:
    topic = (args.get("tema") or "").strip()
    if len(topic) < 2:
        return "Které téma mám nastudovat, pane?"
    try:
        from scripts.hans_study import add_pending_topic
        res = add_pending_topic(_diary_path(handler), topic)
        if res == "exists":
            return f"Téma „{topic}“ už mám ve studijním plánu."
        return (f"Dobře — „{topic}“ jsem si zařadil do studijního plánu. "
                f"Pustím se do něj, jakmile dokončím současné studium.")
    except Exception as e:
        log.warning("add_study: %s", e)
        return "Zařazení tématu se nezdařilo, pane."


def _ground_study(handler, args):
    t = (args.get("tema") or "").strip()
    return (len(t) >= 2, args, "" if len(t) >= 2 else "bez tématu")


# ── Poznámky / paměťové sliby (confirm, light) ───────────────────────────────

def _run_add_note(handler, args) -> str:
    text = (args.get("text") or "").strip()
    if len(text) < 2:
        return "Co mám poznamenat, pane?"
    try:
        import sqlite3
        db = sqlite3.connect(_diary_path(handler))
        db.execute(
            "CREATE TABLE IF NOT EXISTS hans_notes ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, text TEXT, "
            "done INTEGER NOT NULL DEFAULT 0)")
        db.execute("INSERT INTO hans_notes (ts, text, done) VALUES (?,?,0)",
                   (time.time(), text[:500]))
        db.commit()
        db.close()
        return f"Poznamenal jsem si: {text}"
    except Exception as e:
        log.warning("add_note: %s", e)
        return "Poznámku se nepodařilo uložit, pane."


def _ground_note(handler, args):
    t = (args.get("text") or "").strip()
    return (len(t) >= 2, args, "" if len(t) >= 2 else "prázdná poznámka")


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
               "na seznam ke", "wishlist", "knížk", "knizk"],
        args=["titul"], run=_run_book_wishlist, grounding=_ground_book,
        needs_confirm=True, cooldown_s=30),

    # ── Info dotazy (instant, bez potvrzení) ────────────────────────────────
    "report_weather": Action(
        "report_weather",
        "Odpovědět na dotaz o AKTUÁLNÍM počasí / jak je venku.",
        hints=["počasí", "pocasi", "venku", "prší", "prsi", "sněží", "snezi",
               "teplo venku", "zima venku", "za oknem", "slunečno"],
        args=[], run=_run_weather, grounding=None,
        needs_confirm=False, cooldown_s=10),
    "report_pc_health": Action(
        "report_pc_health",
        "Odpovědět na dotaz o stavu POČÍTAČE (teploty, VRAM, RAM, zda běží).",
        hints=["počítač", "pocitac", "jak je na tom pc", "teplota pc",
               "vram", "kolik ram", "gpu", "grafick", "jak se má počítač"],
        args=[], run=_run_pc_health, grounding=None,
        needs_confirm=False, cooldown_s=10),
    "report_who_is_home": Action(
        "report_who_is_home",
        "Odpovědět na dotaz o PŘÍTOMNOSTI osob TEĎ — kdo je doma / v místnosti / "
        "kdo tu je, ale i „kde je <jméno>?“, „je <jméno> doma?“, „co dělá "
        "<jméno>?“ (odpověď = koho Hans právě VIDÍ; NEDOMÝŠLET činnost, jen "
        "přítomnost). Vyber TUTO akci u dotazů na aktuální polohu/přítomnost "
        "konkrétní osoby.",
        hints=["kdo je doma", "kdo je tu", "je někdo doma", "je nekdo doma",
               "kdo tu je", "někdo tu", "nekdo tu", "jsem sám", "jsem sam",
               "kdo tady", "kde je", "co dělá", "co dela"],
        args=[], run=_run_who_home, grounding=None,
        needs_confirm=False, cooldown_s=10),
    "report_now_playing": Action(
        "report_now_playing",
        "Odpovědět na dotaz, co PRÁVĚ hraje na TV.",
        hints=["co hraje", "co běží", "co bezi", "co dávají", "co davaji",
               "co se přehrává", "co je na tv"],
        args=[], run=_run_now_playing, grounding=None,
        needs_confirm=False, cooldown_s=10),
    "report_home_status": Action(
        "report_home_status",
        "Odpovědět na ŠIROKÝ/VÁGNÍ dotaz o dění doma jako celku — „děje se "
        "něco doma?“, „co se doma děje?“, „co je doma nového?“, „je doma "
        "všechno v pořádku?“, „jak to vypadá doma?“. Shrne živý stav (co hraje "
        "na TV + kdo je doma). Vyber TUTO akci místo report_now_playing/"
        "report_who_is_home, když dotaz NENÍ konkrétně jen o TV ani jen o "
        "přítomnosti, ale o dění doma obecně.",
        hints=["děje se něco", "deje se neco", "co se doma", "co je doma",
               "doma nového", "doma noveho", "vypadá to doma", "vypada to doma",
               "všechno v pořádku doma", "vsechno v poradku doma",
               "jak to doma", "co je doma nového", "něco nového doma"],
        args=[], run=_run_home_status, grounding=None,
        needs_confirm=False, cooldown_s=10),

    # ── Ovládání médií (confirm) ────────────────────────────────────────────
    "kodi_pause": Action(
        "kodi_pause",
        "Pozastavit (pauza) běžící film/přehrávání na TV.",
        hints=["pauza", "pauzni", "pozastav", "zastav to", "dej pauzu",
               "stopni", "stop na chvíli"],
        args=[], run=_run_kodi_pause, grounding=_ground_playing,
        needs_confirm=True, cooldown_s=15),
    "kodi_stop": Action(
        "kodi_stop",
        "Úplně zastavit (ukončit) běžící film/přehrávání na TV.",
        hints=["vypni film", "vypni to", "ukonči film", "ukonci film",
               "zastav film", "vypni tv", "konec filmu"],
        args=[], run=_run_kodi_stop, grounding=_ground_playing,
        needs_confirm=True, cooldown_s=15),

    # ── Studijní téma z chatu (confirm) ─────────────────────────────────────
    "add_study_topic": Action(
        "add_study_topic",
        "Zařadit téma ke studiu do hloubky. Argument 'tema' = co si uživatel "
        "přeje aby Hans nastudoval / prostudoval / naučil se.",
        hints=["nastuduj", "prostuduj", "studuj", "nauč se", "nauc se",
               "zaměř se na", "zamer se na", "zjisti víc o", "prozkoumej téma"],
        args=["tema"], run=_run_add_study, grounding=_ground_study,
        needs_confirm=True, cooldown_s=30),

    # ── Poznámky / paměťové sliby (confirm, light) ──────────────────────────
    "add_note": Action(
        "add_note",
        "Přidat poznámku / úkol / položku na seznam. Argument 'text' = co si "
        "uživatel přeje poznamenat (nákup, připomínka, TODO).",
        hints=["poznamenej", "zapiš si", "zapis si", "na nákup", "na nakup",
               "nezapomeň", "nezapomen", "připomeň", "pripomen", "na seznam",
               "poznámka", "poznamka", "dej na seznam"],
        args=["text"], run=_run_add_note, grounding=_ground_note,
        needs_confirm=True, cooldown_s=10),
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
        # HANS_AGENT_NO_WORDGATE_V1 — True: pusť router i na obecné otázky/žádosti
        # (ne jen na hint slova akcí). False = staré chování (jen hinty).
        self.route_all_requests = bool(c.get("route_all_requests", True))
        # HANS_AGENT_KODI_CONFIRM_V1 — u návrhu filmu zrcadli potvrzení i na TV
        self.kodi_confirm_to_tv = bool(c.get("kodi_confirm_to_tv", True))
        self.kodi_confirm_countdown = int(c.get("kodi_confirm_countdown_s", 45))
        self._pending: dict[str, Proposal] = {}       # name → návrh
        self._last_fire: dict[str, float] = {}         # hash → ts (cooldown)
        self._rejected: dict[str, float] = {}          # name|hash → ts (echo)

    # ── pre-gate ────────────────────────────────────────────────────────────
    # HANS_AGENT_NO_WORDGATE_V1 — pre-gate dřív propustil router JEN když zpráva
    # trefila hint slovo konkrétní akce → novým formulacím („děje se něco doma?“)
    # se router NIKDY nezeptal a propadly do LLM = konfabulace živého stavu.
    # Teď: hint = rychlá cesta (zpětná kompatibilita), NAVÍC pusť router, kdykoli
    # zpráva STRUKTURÁLNĚ vypadá jako dotaz/žádost (otazník / tázací nebo
    # rozkazovací začátek). Rozhodnutí CO spustit dál dělá LLM router (whitelist);
    # heuristika jen rozhoduje JESTLI se Hans vůbec zamyslí. Router = rezidentní
    # hans-czech (VRAM zdarma), cena = latence jednoho krátkého volání.
    def _hint_match(self, text: str) -> bool:
        t = _norm(text)
        if len(t) < 2:
            return False
        for a in ACTIONS.values():
            for h in a.hints:
                if h in t:
                    return True
        return False

    def _looks_like_request(self, text: str) -> bool:
        """Strukturální (NE per-akce sémantická) heuristika: vypadá zpráva jako
        otázka nebo žádost? Otazník, nebo tázací/rozkazovací začátek."""
        raw = (text or "").strip()
        if len(raw) < 2:
            return False
        if raw.endswith("?"):
            return True
        t = _norm(raw)
        first = t.split()[0] if t.split() else ""
        return first in _REQUEST_OPENERS

    def _actionable(self, text: str) -> bool:
        if not self.route_all_requests:
            return self._hint_match(text)           # legacy chování (config off)
        return self._hint_match(text) or self._looks_like_request(text)

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
        # HANS_AGENT_KODI_CONFIRM_V1 — odpověď padla v chatu → zavři i dialog na TV
        # (ať okno na TV nečeká na vypršení timeoutu / nezůstane přes hrající film).
        if pend.action.id == "kodi_play_film":
            self._cancel_kodi_dialog(handler)
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
            prop = Proposal(action, args,
                            decision.get("propose_text", ""), conf,
                            decision.get("reason", ""))
            # INSTANT akce (needs_confirm=False) — info dotazy jen odpoví
            # z živých dat, žádné ano/ne. Proveď hned a vrať výsledek.
            if not action.needs_confirm:
                try:
                    result = action.run(handler, args)
                except Exception as e:
                    log.warning("agent instant %s selhala: %s", aid, e)
                    return None
                if not result:
                    return None
                self._last_fire[h] = time.time()
                self._log(handler, prop, "answered", result=result)
                log.info("agent: instant %s conf=%.2f pro %s", aid, conf, name)
                return result
            # CONFIRM akce — navrhni + ulož pending, čekej na ano/ne.
            text = (prop.text or self._default_text(action, args)).strip()
            if not text.endswith(("?",)):
                text += " Mám to udělat?"
            prop.text = text
            self._pending[name] = prop
            # HANS_AGENT_KODI_CONFIRM_V1 — u filmu zrcadli potvrzení i na TV
            # (dialog Ano/Ne přímo na Kodi, ne jen v chatu). Best-effort.
            if action.id == "kodi_play_film" and self._mirror_kodi_confirm(
                    handler, prop):
                text += " (potvrdit můžete i přímo na televizi.)"
                prop.text = text
            self._log(handler, prop, "proposed")
            log.info("agent: návrh %s conf=%.2f pro %s", aid, conf, name)
            return text
        except Exception as e:
            log.warning("agent.propose selhalo: %s", e)
            return None

    def _mirror_kodi_confirm(self, handler, prop: Proposal) -> bool:
        """HANS_AGENT_KODI_CONFIRM_V1 — ukaž potvrzení návrhu filmu i na TV.
        Reuse addon 'service.hans.suggest' (dialog Ano/Ne): na 'Pustit' addon film
        pustí, timeout/Ne nic neudělá (žádné auto-přehrání) → bezpečné zrcadlo chat
        potvrzení. Chat 'ano' i tlačítko na TV vedou k témuž filmu. Best-effort —
        selhání (Kodi dole) jen vrátí False, chat potvrzení běží dál."""
        if not self.kodi_confirm_to_tv:
            return False
        movie = (prop.args or {}).get("_movie")
        if not movie or movie.get("movieid") is None:
            return False
        hi = getattr(handler, "_hans_idle", None)
        kodi = getattr(hi, "kodi", None)
        if not kodi or not hasattr(kodi, "suggest_movie"):
            return False
        title = movie.get("title") or prop.args.get("titul") or "film"
        line = u'Mám pustit „%s"? Potvrďte „Pustit", nebo nechte být.' % title
        fcfg = (self.config.get("film_suggest", {}) or {})
        # Hansova tvář do dialogu (stejná cesta jako u návrhu filmu z klidu)
        face = None
        try:
            if fcfg.get("avatar_to_kodi", True) and hasattr(hi, "_tv_face_image"):
                face = hi._tv_face_image()
        except Exception:
            face = None
        # Hlas na TV — jen když nic nehraje (přehrání by běžící film přerušilo),
        # jako u vlastního návrhu filmu (voice_to_kodi).
        voice = None
        try:
            playing = bool(kodi.get_now_playing()) if hasattr(
                kodi, "get_now_playing") else True
            tts = getattr(handler, "tts_speaker", None)
            if (not playing and fcfg.get("voice_to_kodi", False)
                    and tts and hasattr(tts, "_get_mp3")):
                mp3 = tts._get_mp3(u'Mám pustit film „%s"?' % title)
                if mp3:
                    voice = str(mp3)
        except Exception:
            voice = None
        try:
            return bool(kodi.suggest_movie(
                movie, countdown=self.kodi_confirm_countdown, line=line,
                image_local=face, voice_local=voice,
                voice_volume=int(fcfg.get("voice_volume", 90)),
                voice_lead_ms=int(fcfg.get("voice_lead_ms", 900))))
        except Exception as e:
            log.warning("agent: zrcadlení potvrzení na TV selhalo: %s", e)
            return False

    def _cancel_kodi_dialog(self, handler):
        """Zavři otevřený návrhový dialog na TV (uživatel odpověděl v chatu)."""
        if not self.kodi_confirm_to_tv:
            return
        kodi = getattr(getattr(handler, "_hans_idle", None), "kodi", None)
        if kodi and hasattr(kodi, "cancel_dialog"):
            try:
                kodi.cancel_dialog()
            except Exception as e:
                log.warning("agent: zavření TV dialogu selhalo: %s", e)

    def _default_text(self, action: Action, args: dict) -> str:
        if action.id == "kodi_play_film":
            return f"Chcete, abych pustil „{args.get('titul')}“?"
        if action.id == "hans_sleep":
            return "Mám se ztišit a jít spát?"
        if action.id == "add_book_wishlist":
            return f"Mám „{args.get('titul')}“ přidat na seznam ke čtení?"
        if action.id == "kodi_pause":
            return "Mám pozastavit přehrávání?"
        if action.id == "kodi_stop":
            return "Mám zastavit přehrávání?"
        if action.id == "add_study_topic":
            return f"Mám si „{args.get('tema')}“ zařadit ke studiu?"
        if action.id == "add_note":
            return f"Mám si poznamenat „{args.get('text')}“?"
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
            "DŮLEŽITÉ: dotaz na INFORMACE/ZNALOST o něčem („co víš o X“, "
            "„zjisti/zjistit víc o X“, „řekni mi o X“, „znáš X?“, „pamatuješ "
            "na X“) NENÍ akce — uživatel chce, abys mu o tom POVĚDĚL, ne abys "
            "něco spustil (NEpouštěj film, NEpřidávej na seznam) → action=null. "
            "Akci (pustit/přidat/…) zvol JEN u jasného POKYNU něco UDĚLAT. "
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
