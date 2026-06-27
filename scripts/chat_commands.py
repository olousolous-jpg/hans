#!/usr/bin/env python3
"""Chat commands — slash (/denik) i natural language ("připrav deník").

Použití:
    from scripts.chat_commands import parse_command, dispatch

    cmd = parse_command("/denik")               # ("denik", "")
    cmd = parse_command("Hansi, připrav deník")  # ("denik", "")
    cmd = parse_command("zapomeň naši historii") # ("zapomen", "")
    cmd = parse_command("ahoj")                  # None

    if cmd:
        reply = dispatch(cmd, handler, name=user_name)
"""
from __future__ import annotations

import logging
import re
import threading
from typing import Callable, Optional

_log = logging.getLogger("chat_commands")

# CHAT_COMMANDS_MARKER

# ── Registr commands ───────────────────────────────────────────────────

_COMMANDS: dict[str, dict] = {}


def register(command_id: str, *,
             slash_aliases: list[str],
             nl_patterns: list[str],
             handler: Callable,
             help_text: str = ""):
    """Zaregistruj command. slash_aliases: ['denik','reflexe'] - matche /denik /reflexe.
    nl_patterns: regex patterny (case-insensitive) pro natural language."""
    _COMMANDS[command_id] = {
        "slash":    [s.lower().lstrip("/") for s in slash_aliases],
        "nl":       [re.compile(p, re.IGNORECASE) for p in nl_patterns],
        "handler":  handler,
        "help":     help_text,
    }


# ── Parser ─────────────────────────────────────────────────────────────

def parse_command(message: str) -> Optional[tuple[str, str]]:
    """Pokus se rozpoznat command. Vrátí (command_id, args) nebo None.
    Slash má prioritu. NL detekce běží jen pokud message nezačíná /."""
    msg = message.strip()
    if not msg:
        return None

    # Slash commands
    if msg.startswith("/"):
        parts = msg[1:].split(maxsplit=1)
        if not parts:
            return None
        slash_name = parts[0].lower()
        args = parts[1] if len(parts) > 1 else ""
        for cmd_id, spec in _COMMANDS.items():
            if slash_name in spec["slash"]:
                return (cmd_id, args)
        return None  # neznámý slash → ne-command

    # Natural language
    for cmd_id, spec in _COMMANDS.items():
        for pat in spec["nl"]:
            if pat.search(msg):
                return (cmd_id, msg)
    return None


# ── Dispatcher ─────────────────────────────────────────────────────────

def dispatch(command: tuple[str, str], handler, name: Optional[str]) -> str:
    """Spustí command. handler = openwebui_direct_handler instance.
    Vrátí text odpovědi pro chat."""
    cmd_id, args = command
    spec = _COMMANDS.get(cmd_id)
    if not spec:
        return f"⚠ Neznámý příkaz: {cmd_id}"
    try:
        return spec["handler"](handler, name, args)
    except Exception as e:
        _log.error("dispatch %s failed: %s", cmd_id, e)
        return f"⚠ Příkaz {cmd_id} selhal: {e}"


def list_commands() -> list[dict]:
    """Vrátí seznam dostupných commands pro /help."""
    return [
        {"id": cid, "slash": spec["slash"][0], "help": spec["help"]}
        for cid, spec in _COMMANDS.items()
    ]


NL_RUNTIME = chr(10)  # G5C: nový řádek jako runtime znak

# ── Command implementations ────────────────────────────────────────────

# G5C_VERIFY_COMMAND_V1 ─────────────────────────────────────────────────
def _g5c_llm(handler, system, user, num_predict=200):
    """Zavolá LLM stejným vzorem jako web_reader._summarize (ollama_chat)."""
    try:
        from scripts.ollama_client import ollama_chat
        cfg = getattr(handler, "config", {}) or {}
        ow = cfg.get("openwebui_chat", {}) or {}
        model = (cfg.get("models", {}).get("utility")
                 or cfg.get("models", {}).get("dialog")
                 or getattr(handler, "model_name", None)
                 or "hans-czech:latest")
        url = ow.get("base_url", "http://127.0.0.1:11434")
        # G5I_VERIFY_DETERMINISTIC_V1 — temperature 0.0: verify musí být
        # reprodukovatelné (extrakce entity i porovnání). Bez ní ollama
        # default ~0.7 → flip-flop na identickém vstupu (R.U.R. apod.).
        out = ollama_chat(
            model,
            [{"role": "system", "content": system},
             {"role": "user", "content": user}],
            ollama_url=url,
            options={"num_predict": num_predict, "temperature": 0.0},
        )
        return (out or "").strip()
    except Exception as e:
        _log.error("G5C LLM selhal: %s", e)
        return ""


def _cmd_verify(handler, name, args) -> str:
    """G5C: ověř faktická tvrzení proti Wikipedii. JEN diagnostika.
    /verify <text>  → ověří text;  /verify → poslední Hansovu odpověď.
    """
    # 1) Zdroj textu: arg má přednost, jinak poslední Hansova odpověď
    text = (args or "").strip()
    if not text:
        try:
            hist = handler.conv_store.get_history(name) or []
            for msg in reversed(hist):
                if msg.get("role") == "assistant" and msg.get("content"):
                    text = msg["content"].strip()
                    break
        except Exception as e:
            _log.error("G5C: čtení historie selhalo: %s", e)
    if not text:
        return "Nemám co ověřovat, pane. Zadejte /verify <text> nebo se nejdřív na něco zeptejte."

    _log.info("G5C: verify začíná, text=%r", text[:120])

    # 2) Extrakce tvrzení (LLM)
    # G5J_VERIFY_UNIFY_V1 (a) — ENTITA = předmět tvrzení (dílo/událost/místo),
    # NE osoba, co o nich něco tvrdí. Sjednoceno s hans_routine extraktorem.
    extract_sys = (
        "Jsi extraktor faktických tvrzení. Z textu vypiš ověřitelná "
        "faktická tvrzení o světě (osoby, díla, události, místa, data). "
        "ENTITA je PŘEDMĚT tvrzení s vlastním heslem na Wikipedii — "
        "u díla samotné dílo, NE jeho autor. Tvrzení 'R.U.R. napsal "
        "Čapek' → ENTITA je 'R.U.R.' (dílo), NE 'Čapek'. Tvrzení "
        "'Wells napsal Válku světů' → ENTITA 'Válka světů'. "
        "Ignoruj dojmy, zdvořilosti, názory. Každé tvrzení na samostatný "
        "řádek ve tvaru 'ENTITA | tvrzení'. Max 3. Pokud žádné, napiš PRÁZDNÉ."
    )
    extract = _g5c_llm(handler, extract_sys, "Text:" + NL_RUNTIME + text, num_predict=200)
    if not extract or "PRÁZDNÉ" in extract.upper():
        return "Pane, v textu nenacházím konkrétní faktické tvrzení k ověření."

    # 3) Wikipedia raw_text pro každé tvrzení
    try:
        from scripts.web_reader import WebReader
        cfg = getattr(handler, "config", {}) or {}
        wr = WebReader(cfg)
    except Exception as e:
        _log.error("G5C: WebReader init selhal: %s", e)
        return "Pane, ověření selhalo (čtečka webu nedostupná): " + str(e)

    lines = [l.strip() for l in extract.splitlines() if l.strip()][:3]
    results = []
    for line in lines:
        entity = line.split("|", 1)[0].strip() if "|" in line else line
        claim = line.split("|", 1)[1].strip() if "|" in line else line
        if not entity:
            continue
        # G5J_VERIFY_UNIFY_V1 (b) — plný článek dle PŘEDMĚTU (kopie G5F z routine):
        # _wikipedia_search → fetch_url (2500 zn.), fallback REST summary.
        from urllib.parse import quote as _q
        wiki = ""
        try:
            _title = wr._wikipedia_search(entity)
            if _title:
                _url = 'https://cs.wikipedia.org/wiki/' + _q(_title.replace(' ', '_'))
                _full = wr.fetch_url(_url, topic='verify')
                if _full and getattr(_full, 'raw_text', ''):
                    wiki = _full.raw_text[:2500]
                    _log.info('G5C: [%s] plný článek %r (%d zn.)',
                              entity, _title, len(_full.raw_text))
            if not wiki:
                _rr = wr.wikipedia(entity)
                if _rr and getattr(_rr, 'raw_text', ''):
                    wiki = _rr.raw_text[:1200]
                    _log.info('G5C: [%s] fallback summary', entity)
        except Exception as e:
            _log.warning("G5C: zdroj pro %r selhal: %s", entity, e)
        if not wiki:
            results.append("• " + entity + ": Wikipedie nenašla (nelze ověřit)")
            continue
        cmp_sys = (
            "Jsi ověřovatel faktů. Porovnej TVRZENÍ s textem z Wikipedie. "
            "Odpověz stručně jedním z: SHODA / ROZPOR / NELZE OVĚŘIT, "
            "a krátce proč (max 1 věta). Buď přísný na fakta."
        )
        cmp_user = "TVRZENÍ: " + claim + NL_RUNTIME + NL_RUNTIME + "WIKIPEDIE:" + NL_RUNTIME + wiki
        verdict = _g5c_llm(handler, cmp_sys, cmp_user, num_predict=120)
        _log.info("G5C: [%s] verdikt=%s", entity, verdict[:120])
        results.append("• " + entity + ": " + verdict)

    if not results:
        return "Pane, nepodařilo se extrahovat ověřitelné entity."

    body = NL_RUNTIME.join(results)
    return ("Ověření proti Wikipedii, pane:" + NL_RUNTIME + NL_RUNTIME + body
            + NL_RUNTIME + NL_RUNTIME + "(Pozn.: jen diagnostika, nic se neukládá.)")



def _cmd_denik(handler, name, args) -> str:
    """Spustí evening reflection v jiném threadu."""
    _hi = getattr(handler, "_hans_idle", None)
    _routine = getattr(_hi, "_routine", None) if _hi else None
    if not _routine or not hasattr(_routine, "run_evening_reflection"):
        return "Omlouvám se, večerní reflexe není dostupná."

    def _run():
        try:
            _log.info("Chat command: spouštím evening reflection")
            result = _routine.run_evening_reflection()
            if result:
                _log.info("Evening reflection done: %s", result[:80])
            else:
                _log.warning("Evening reflection vrátil None")
        except Exception as e:
            _log.error("Evening reflection failed: %s", e)

    threading.Thread(target=_run, daemon=True).start()
    return "Připravuji dnešní deník, pane. Bude to chvíli trvat."


def _cmd_dialog(handler, name, args) -> str:
    """Spustí Hans-Koláč dialog flag souborem (totéž co web admin tlačítko)."""
    from pathlib import Path as _P
    try:
        flag = _P("data/.trigger_dialog")
        flag.parent.mkdir(exist_ok=True)
        flag.touch()
        from scripts.hans_kolac import kolac_name as _kn  # KOLAC_NAME_CONFIGURABLE_V1
        _k = _kn(getattr(handler, "config", {}) or {})
        return f"Zavolám {_k}. Dialog se spustí za chvíli."
    except Exception as e:
        return f"Nepodařilo se mi zavolat společníka: {e}"


def _cmd_zapomen(handler, name, args) -> str:
    """Smaže conversation history aktuální osoby."""
    if not name:
        return "Nevím, čí historii mám smazat."
    store = getattr(handler, "conv_store", None)
    if not store or not hasattr(store, "clear"):
        return "Conversation store není dostupný."
    try:
        store.clear(name)
        return f"Vymazal jsem naše předchozí hovory, {name}."
    except Exception as e:
        return f"Nepodařilo se smazat historii: {e}"


def _cmd_info(handler, name, args) -> str:
    """Vrátí stav."""
    parts = []
    store = getattr(handler, "conv_store", None)
    if store and hasattr(store, "get_history") and name:
        try:
            h = store.get_history(name)
            parts.append(f"Mám v paměti {len(h)} zpráv z našich hovorů.")
        except Exception:
            pass
    _hi = getattr(handler, "_hans_idle", None)
    if _hi:
        from scripts.hans_persona import persona_name as _pn  # PERSONA_NAME_CONFIGURABLE_V1
        parts.append(f"{_pn(getattr(handler, 'config', {}) or {})} idle modul běží.")
    parts.append(f"Aktuálně mluvím s: {name or 'neznámý'}.")
    return " ".join(parts) if parts else "Nemám k tomu žádné informace."


def _cmd_help(handler, name, args) -> str:
    """Seznam commands."""
    lines = ["Dostupné příkazy:"]
    for c in list_commands():
        lines.append(f"  /{c['slash']} — {c['help']}")
    return "\n".join(lines)


def _cmd_zaptej(handler, name, args) -> str:
    """Vyvolá curiosity — Hans vygeneruje otázku a hledá odpověď.
    # CMD_ZAPTEJ_PATCH
    args = volitelný kontext (pokud prázdné, vezme z místnosti)."""
    _hi = getattr(handler, "_hans_idle", None)
    _cur = getattr(_hi, "_curiosity", None) if _hi else None
    if not _cur:
        return "Curiosity modul není dostupný."

    # Kontext: explicitně args nebo "rozhovor s {name}" jako placeholder
    context = args.strip() if args else f"rozhovor s osobou {name or 'host'}"

    import threading
    def _run():
        try:
            # source_type = jiný než observation/room → půjde do Wikipedie
            _cur.trigger_question(context, source_type="manual")
        except Exception as e:
            print(f"[Chat] zaptej failed: {e}")

    threading.Thread(target=_run, daemon=True).start()
    return "Položím si otázku a hledám odpověď, pane."


def _cmd_enroll(handler, name, args) -> str:
    """Spustí video enroll. # ENROLL_MULTI_DEFAULT
    Bez sekund (jen jméno) → multi-phase (3 vzdálenosti, ~2 min).
    Se sekundami → single phase (jen aktuální vzdálenost).
    """
    from pathlib import Path as _P
    parts = (args or "").strip().split()
    target_name = parts[0] if parts else (name or "")
    if not target_name:
        return "Použití: /enroll <jméno> [sekundy]   (bez sekund = multi-phase)"
    try:
        flag = _P("data/.video_enroll")
        flag.parent.mkdir(exist_ok=True)
        if len(parts) < 2:
            # Multi-phase mode (3 vzdálenosti, ~2 min)
            flag.write_text(f"multi:{target_name.lower()}|0")
            return (f"Spouštím multi-phase enrollment pro '{target_name}'. "
                    f"Budu vás vést — postavte se prosím asi metr od kamery.")
        try:
            secs = int(parts[1])
        except ValueError:
            secs = 30
        secs = max(5, min(secs, 120))
        flag.write_text(f"{target_name.lower()}|{secs}")
        return (f"Spouštím video enroll pro '{target_name}' na {secs}s. "
                f"Otáčejte hlavou pomalu.")
    except Exception as e:
        return f"Nepodařilo se spustit enroll: {e}"


# ── Registrace ─────────────────────────────────────────────────────────

def _cmd_ooda(handler, name, args) -> str:  # OODA_CMD_V1
    """/ooda — diagnostika OODA. Zavolá _decide_activity (zaloguje skóre),
    akci NEVYKONÁ, vrátí název vybrané aktivity. Skóre: grep z logu.
    Cesta k idle objektu kopíruje _cmd_denik (handler._hans_idle)."""
    _hi = getattr(handler, "_hans_idle", None)
    if _hi is None:
        return "OODA: idle objekt (handler._hans_idle) není dostupný."
    if not hasattr(_hi, "_decide_activity"):
        return "OODA: _decide_activity na idle objektu chybí (patch aplikován?)."
    try:  # OODA_CMD_SCORE_V1
        chosen_fn = _hi._decide_activity(dry_run=True)  # OODA_DRYRUN_V1
        fn_name = getattr(chosen_fn, "__name__", str(chosen_fn))
        label = fn_name.replace("_activity_", "")
        score = getattr(_hi, "_last_ooda_score", None)
        if score:
            return (
                "OODA skóre: %s\n"
                "(akce NEvykonána — jen diagnostika)"
            ) % score
        # Fallback: atribut chybí (starý běh / první patch nenasazen)
        return (
            "OODA by teď vybralo: %s\n"
            "(akce NEvykonána). Skóre v logu:\n"
            "  grep 'OODA skóre:' data/system.log | tail -1"
        ) % label
    except Exception as _e:
        return "OODA diagnostika selhala: %s" % _e


register(
    "ooda",
    slash_aliases=["ooda"],
    nl_patterns=[
        r"\bjak.{0,15}rozhod",
        r"\bco.{0,10}by.{0,10}d[ěe]lal",
    ],
    handler=_cmd_ooda,
    help_text="Diagnostika OODA — co by Hans teď vybral (akci nevykoná)",
)

def _cmd_work(handler, name, args) -> str:  # WORK_CMD_V1 + WORK_REFACTOR_SHARED_V1
    """/work <téma> — tenký wrapper. Jádro je v hans_idle._create_work,
    aby ho mohla volat i automatika (idle smyčka) přes shim."""
    topic = (args or '').strip()
    if not topic:
        return 'Použití: /work <téma>  (např. /work sopky)'
    _hi = getattr(handler, '_hans_idle', None)
    if _hi is None:
        return 'WORK: idle objekt (handler._hans_idle) není dostupný.'
    if not hasattr(_hi, '_create_work'):
        return 'WORK: _create_work chybí na idle (refaktor patch nasazen?).'
    # llm_caller: zabal _g5c_llm s reálným handlerem (num_predict=900 jak dřív)
    _caller = lambda s, u: _g5c_llm(handler, s, u, num_predict=900)
    r = _hi._create_work(topic, _caller)
    if not r.get('ok'):
        return 'WORK: %s' % r.get('error', 'neznámá chyba')
    return (('Hotovo. Napsal jsem esej o \'%s\' (%d slov). '
             'Uloženo: %s · RAG: %s')
            % (topic, r.get('words', 0), r.get('path', '?'), r.get('rag', '?')))


def _cmd_interest(handler, name, args) -> str:  # INTEREST_CMD_C2 / INTEREST_DEL
    """/interest                -> výpis naučených zájmů
    /interest <téma>       -> zapíše nový zájem
    /interest del <téma>   -> smaže zájem(y) s daným textem
    /interest reset ano    -> smaže VŠECHNY naučené (Hans spadne na seed)
    Tytéž řádky (event_type=interest_update) řídí personu."""
    import sqlite3
    import time
    cfg = getattr(handler, "config", {}) or {}
    db_path = cfg.get("diary_db", "data/hans_diary.db")
    raw = (args or "").strip()
    parts = raw.split(None, 1)
    sub = parts[0].lower() if parts else ""
    rest = parts[1].strip() if len(parts) > 1 else ""

    # ── /interest del <téma> ────────────────────────────────────────────
    if sub == "del":
        if not rest:
            return "Použití: /interest del <téma>  (smaže zájem s tímto textem)"
        try:
            conn = sqlite3.connect(db_path, timeout=5.0)
            try:
                cur = conn.execute(
                    "SELECT id, note FROM diary WHERE event_type='interest_update' "
                    "AND lower(note)=lower(?)", (rest,)).fetchall()
                if not cur:
                    return "Žádný naučený zájem '%s' jsem nenašel." % rest
                conn.execute(
                    "DELETE FROM diary WHERE event_type='interest_update' "
                    "AND lower(note)=lower(?)", (rest,))
                conn.commit()
            finally:
                conn.close()
        except Exception as _e:
            return "INTEREST: mazání selhalo: %s" % _e
        n = len(cur)
        return ("Smazal jsem zájem '%s'%s." %
                (rest, (" (%dx)" % n) if n > 1 else ""))

    # ── /interest reset [ano] ───────────────────────────────────────────
    if sub == "reset":
        if rest.lower() != "ano":
            from scripts.hans_persona import persona_name as _pn  # PERSONA_NAME_CONFIGURABLE_V1
            return (f"Reset smaže VŠECHNY naučené zájmy a {_pn(cfg)} spadne zpět "
                    "na základní. Pro potvrzení napiš: /interest reset ano")
        try:
            conn = sqlite3.connect(db_path, timeout=5.0)
            try:
                cur = conn.execute(
                    "SELECT COUNT(*) FROM diary "
                    "WHERE event_type='interest_update'").fetchone()
                n = cur[0] if cur else 0
                conn.execute(
                    "DELETE FROM diary WHERE event_type='interest_update'")
                conn.commit()
            finally:
                conn.close()
        except Exception as _e:
            return "INTEREST: reset selhal: %s" % _e
        from scripts.hans_persona import persona_name as _pn  # PERSONA_NAME_CONFIGURABLE_V1
        return ("Smazal jsem všechny naučené zájmy (%d). "
                "%s se vrací k základním." % (n, _pn(cfg)))

    # ── /interest (výpis) ───────────────────────────────────────────────
    if not raw:
        try:
            from scripts.hans_persona import recent_interests
            cur = recent_interests(db_path, limit=5)
        except Exception as _e:
            return "INTEREST: čtení selhalo: %s" % _e
        if not cur:
            seed = cfg.get("persona", {}).get("interests_seed", "")
            from scripts.hans_persona import persona_name as _pn  # PERSONA_NAME_CONFIGURABLE_V1
            return ("Žádné naučené zájmy zatím nejsou. "
                    "%s vychází ze základních: %s" % (_pn(cfg), seed or "(žádné)"))
        return ("Aktuální naučené zájmy (nejnovější první): %s\n"
                "Smazat: /interest del <téma>" % cur)

    # ── /interest <téma> (zápis) ────────────────────────────────────────
    try:
        conn = sqlite3.connect(db_path, timeout=5.0)
        try:
            conn.execute(
                "INSERT INTO diary (ts, event_type, title, note) VALUES (?,?,?,?)",
                (time.time(), "interest_update", "Zájem", raw))
            conn.commit()
        finally:
            conn.close()
    except Exception as _e:
        return "INTEREST: zápis selhal: %s" % _e
    return ("Zaznamenal jsem nový zájem: '%s'. "
            "Promítne se do Hansovy osobnosti." % raw)


register(
    "interest",
    slash_aliases=["interest", "zajem", "zájem"],
    nl_patterns=[],
    handler=_cmd_interest,
    help_text="Zapíše Hansův zájem do deníku (/interest <téma>) nebo vypíše naučené (/interest)",
)


register(
    "work",
    slash_aliases=["work", "dilo", "esej"],
    nl_patterns=[
        r"\bnapi\w+.{0,15}esej",
        r"\bnapi\w+.{0,15}pr\u00e1ci",
    ],
    handler=_cmd_work,
    help_text="Hans napíše esej z nastudované četby na zadané téma",
)

register(
    "denik",
    slash_aliases=["denik", "deník", "reflexe", "shrnuti"],
    nl_patterns=[
        r"\bpřiprav.{0,20}den[íi]k",
        r"\bzapis.{0,20}dnes",
        r"\bshrnut[íi].{0,20}dne",
        r"\bzapis.{0,20}den[íi]k",
        r"\bden[íi]k.{0,20}dnes",
    ],
    handler=_cmd_denik,
    help_text="Spustí večerní reflexi (uloží shrnutí dne do deníku a RAGu)",
)

register(
    "dialog",
    slash_aliases=["dialog", "kolac", "koláč"],
    nl_patterns=[
        r"\bzavolej.{0,20}kol[aá]č",
        r"\bpromluv.{0,20}kol[aá]č",
        r"\bdialog.{0,20}kol[aá]č",
    ],
    handler=_cmd_dialog,
    help_text="Vyvolá rozhovor Hanse s panem Koláčem",
)

register(
    "zapomen",
    slash_aliases=["zapomen", "zapomeň", "vymaz", "reset"],
    nl_patterns=[
        r"\bzapomeň.{0,30}(naš|histor|hovor|rozhov)",
        r"\bvymaž.{0,20}histor",
        r"\bzač[ěn][i]?.{0,20}znovu",
    ],
    handler=_cmd_zapomen,
    help_text="Smaže historii našich hovorů",
)

register(
    "info",
    slash_aliases=["info", "stav"],
    nl_patterns=[
        r"\bjak[ýé].{0,10}stav",
        r"\bco.{0,5}ví[šs].{0,10}o\s*sob",
    ],
    handler=_cmd_info,
    help_text="Zobrazí aktuální stav (kolik zpráv v paměti atd.)",
)

register(
    "help",
    slash_aliases=["help", "pomoc", "napoveda", "nápověda"],
    nl_patterns=[
        r"\bco.{0,5}um[íi]š",
        r"\bjak[éý].{0,10}p[řr][íi]kaz",
    ],
    handler=_cmd_help,
    help_text="Seznam příkazů",
)
register(
    "zaptej",
    slash_aliases=["zaptej", "otazka", "otázka", "zeptej"],
    nl_patterns=[
        r"\bvyvolej.{0,15}ot[áa]zk",
        r"\bzaptej\s+se",
        r"\bpolož[íi].{0,10}ot[áa]zk",
        r"\bzv[íi]davost",
    ],
    handler=_cmd_zaptej,
    help_text="Hans si položí otázku a hledá odpověď (curiosity)",
)
register(
    "enroll",
    slash_aliases=["enroll", "video_enroll", "trenuj"],
    nl_patterns=[
        r"\bspust[íi]\s+video\s+enroll",
        r"\btrenu[jí]\s+m[ěe]",
    ],
    handler=_cmd_enroll,
    help_text="Spustí video enrollment (zachytí 30s video tváří)",
)


# G5C_VERIFY_COMMAND_V1 — registrace /verify
register(
    "verify",
    slash_aliases=["verify", "over", "overit"],
    nl_patterns=[],
    handler=_cmd_verify,
    help_text="Ověří faktická tvrzení proti Wikipedii (/verify <text> nebo poslední odpověď)",
)


# ─── /sleep — manuální override spánku (SLEEP_TOGGLE_V1) ──────────────
def _cmd_sleep(handler, name, args) -> str:
    """/sleep — toggle Hansova spánku.
    Vzhůru → uspat (tichý). Spí → probudit (mluvící).
    Override drží přes okno, expiruje na přirozené opačné hraně."""
    _hi = getattr(handler, "_hans_idle", None)
    if _hi is None:
        return "/sleep: idle objekt není dostupný."
    _rt = getattr(_hi, "_routine", None)
    if _rt is None:
        return "/sleep: routine objekt není dostupný."
    if not hasattr(_rt, "set_manual_sleep"):
        return "/sleep: set_manual_sleep chybí (patch aplikován?)."
    try:
        currently_sleeping = bool(getattr(_rt, "_sleeping", False))
        new_state = not currently_sleeping
        _rt.set_manual_sleep(new_state)
        return "Usínám." if new_state else "Probudil jsem se."
    except Exception as _e:
        return "/sleep selhal: %s" % _e


register(
    "sleep",
    slash_aliases=["sleep"],
    nl_patterns=[
        r"\bb[ěe]ž\s+spát",
        r"\bjdi\s+spat",
        r"\bvzbu[ďd]\s+se",
        r"\bprobu[ďd]\s+se",
    ],
    handler=_cmd_sleep,
    help_text="Toggle spánkového režimu (manuální override).",
)


# ─── /severka — sebereflexe identity (HANS_SEVERKA_V1, Fáze 3c) ──────────
_SEVERKA_APPROVE = {"schválit", "schvalit", "approve", "ano", "ok", "souhlasím", "souhlasim"}
_SEVERKA_REJECT  = {"zamítnout", "zamitnout", "reject", "ne", "nesouhlasím", "nesouhlasim"}
_SEVERKA_HISTORY = {"historie", "history", "log"}
_SEVERKA_ROLLBACK = {"rollback", "vrať", "vrat", "zpět", "zpet"}
_SEVERKA_RUN = {"teď", "ted", "run", "check", "spusť", "spust", "zkontroluj"}


def _cmd_severka(handler, name, args) -> str:
    """/severka — stav / schválit / zamítnout / historie / rollback / teď."""
    _hi = getattr(handler, "_hans_idle", None)
    _rt = getattr(_hi, "_routine", None) if _hi else None
    if not _rt:
        return "Severka není dostupná (routine chybí)."
    ident = getattr(_rt, "_identity", None)
    sev = getattr(_rt, "_severka", None)
    if ident is None:
        return "Verzování identity není dostupné."
    parts = (args or "").strip().split(maxsplit=1)
    cmd = parts[0].lower() if parts else "stav"
    rest = parts[1].strip() if len(parts) > 1 else ""
    by = name or "user"

    pend = ident.pending()

    if cmd in _SEVERKA_APPROVE:
        target = int(rest) if rest.isdigit() else (pend[0].id if pend else None)
        if target is None:
            return "Není co schvalovat — žádný čekající návrh, pane."
        if ident.approve(target, approved_by=by):
            cur = ident.current()
            core = cur.core if cur else ""
            return ("Děkuji za důvěru, pane. Přijal jsem novou podobu sebe sama. "
                    "Od této chvíle jsem:" + NL_RUNTIME + NL_RUNTIME + "„" + core + "\"")
        return "Schválení se nezdařilo (verze %s není čekající?)." % target

    if cmd in _SEVERKA_REJECT:
        target = int(rest) if rest.isdigit() else (pend[0].id if pend else None)
        if target is None:
            return "Není co zamítat, pane."
        if ident.reject(target, approved_by=by):
            return "Rozumím, pane. Zůstávám, kým jsem byl."
        return "Zamítnutí se nezdařilo."

    if cmd in _SEVERKA_ROLLBACK:
        if not rest.isdigit():
            return "Uveďte verzi: /severka rollback <id> (viz /severka historie)."
        if ident.rollback(int(rest), approved_by=by):
            cur = ident.current()
            return ("Vrátil jsem se k dřívější podobě:" + NL_RUNTIME + NL_RUNTIME
                    + "„" + (cur.core if cur else "") + "\"")
        return "Rollback se nezdařil."

    if cmd in _SEVERKA_HISTORY:
        hist = ident.history(limit=15)
        if not hist:
            return "Historie identity je prázdná."
        out = ["Historie mé identity, pane:"]
        for v in hist:
            out.append("  [%d] %s — %s: %.70s" % (v.id, v.status, v.source, v.core))
        return NL_RUNTIME.join(out)

    if cmd in _SEVERKA_RUN:
        if sev is None:
            return "Rozhodovací mechanismus není dostupný."
        def _run():
            try:
                sev.evaluate()
            except Exception as _e:
                _log.error("severka manual run: %s", _e)
        threading.Thread(target=_run, daemon=True).start()
        return ("Zamýšlím se nad tím, kým se stávám, pane. Chvíli to potrvá; "
                "výsledek pak najdete v /severka stav.")

    # default: stav
    cur = ident.current()
    out = []
    if cur:
        out.append("Současná identita (verze %d, zdroj %s):" % (cur.id, cur.source))
        out.append("„" + cur.core + "\"")
    if pend:
        out.append("")
        out.append("Čekající návrh změny:")
        for p in pend:
            out.append("  [verze %d] „%s\"" % (p.id, p.core))
            if p.rationale:
                out.append("    důvod: %s" % p.rationale)
        out.append("")
        out.append("Schválit: /severka schválit  |  zamítnout: /severka zamítnout")
    else:
        out.append("")
        out.append("Žádný čekající návrh změny, pane.")
    return NL_RUNTIME.join(out)


register(
    "severka",
    slash_aliases=["severka"],
    nl_patterns=[],
    handler=_cmd_severka,
    help_text="Sebereflexe identity: /severka [stav|schválit|zamítnout|historie|rollback <id>|teď]",
)


# ─── /art — Hans namaluje obraz k aktuální knize (HANS_ART_V1) ────────────
def _cmd_art(handler, name, args) -> str:
    """/art [název knihy] — Hans hned namaluje obraz k (zadané/aktuální) knize.
    Render běží na pozadí (chat odpoví hned), VRAM orchestrace uvnitř."""
    import threading as _t
    cfg = getattr(handler, "config", {}) or {}
    db = cfg.get("diary_db") or "data/hans_diary.db"
    title = (args or "").strip()
    try:
        from scripts import hans_art
    except Exception as e:
        return "Malování není dostupné: %s" % e
    # HANS_ART_UNREAD_WISHLIST_V1 — nečtenou knihu nemaluj naslepo; přiznej + přidej k přečtení
    if title and not hans_art.book_is_read(db, title):
        res = hans_art.add_to_wishlist(db, title)
        if res == "exists":
            return ("Knihu „%s\" jsem ještě nečetl, pane — a už ji mám na seznamu "
                    "k přečtení. Až ji poznám, rád k ní namaluji obraz." % title)
        return ("Přiznám se, pane — knihu „%s\" jsem ještě nečetl, takže bych jen "
                "hádal, oč v ní jde. Přidal jsem si ji na seznam k přečtení (nízká "
                "priorita); až ji přečtu, rád k ní namaluji obraz." % title)

    if not hans_art.comfy_available(cfg):
        return ("Bohužel, pane — výtvarná dílna (ComfyUI na PC) teď neběží, "
                "tak nemohu malovat. Zkuste to, až bude PC vzhůru.")
    book = title or hans_art._current_book_title(db)

    def _worker():
        try:
            hans_art.render_now(cfg, db, title)
        except Exception as _e:
            _log.warning("/art render selhal: %s", _e)
    _t.Thread(target=_worker, daemon=True).start()
    return ("Dám se do toho, pane — maluji obraz inspirovaný knihou „%s\". "
            "Za chvíli se objeví na nástěnce (Co Hans namaloval). "
            "Chat může být asi minutu zaneprázdněný." % book)


register(
    "art",
    slash_aliases=["art", "obraz"],
    nl_patterns=[],
    handler=_cmd_art,
    help_text="Hans namaluje obraz k aktuální knize: /art [název knihy]",
)


# ─── /nitky — rozjeté nitky per osoba (HANS_THREADS_V1, frontier #4) ──────
_NITKY_CLOSE = {"zavři", "zavri", "close", "uzavři", "uzavri"}
_NITKY_ALL = {"vše", "vse", "all", "vsechny", "všechny"}


def _cmd_nitky(handler, name, args) -> str:
    """/nitky — výpis otevřených nitek; /nitky zavři <id>; /nitky vše."""
    import sqlite3 as _s
    cfg = getattr(handler, "config", {}) or {}
    db = cfg.get("diary_db") or "data/hans_diary.db"
    parts = (args or "").strip().split(maxsplit=1)
    cmd = parts[0].lower() if parts else ""
    rest = parts[1].strip() if len(parts) > 1 else ""

    if cmd in _NITKY_CLOSE:
        if not rest.isdigit():
            return "Uveďte id: /nitky zavři <id> (viz /nitky)."
        try:
            from scripts.hans_threads import ThreadStore
            ok = ThreadStore(cfg, db).close(int(rest), resolution="ručně uzavřeno")
            return ("Nitku %s jsem uzavřel, pane." % rest if ok
                    else "Tu nitku se nepodařilo uzavřít (už uzavřená?).")
        except Exception as e:
            return "Chyba při uzavírání: %s" % e

    include_closed = cmd in _NITKY_ALL
    try:
        conn = _s.connect("file:%s?mode=ro" % db, uri=True, timeout=3.0)
        conn.row_factory = _s.Row
        sql = ("SELECT id,person,topic,follow_up,status,times_surfaced "
               "FROM person_threads "
               + ("" if include_closed else "WHERE status='open' ")
               + "ORDER BY person, updated_ts DESC")
        rows = conn.execute(sql).fetchall()
        conn.close()
    except Exception as e:
        return "Nitky nedostupné: %s" % e
    if not rows:
        return "Zatím žádné rozjeté nitky, pane."
    out = ["Rozjeté nitky%s:" % (" (vč. uzavřených)" if include_closed else "")]
    cur_person = None
    for r in rows:
        if r["person"] != cur_person:
            cur_person = r["person"]
            out.append("")
            out.append("• %s:" % cur_person)
        mark = "" if r["status"] == "open" else " [%s]" % r["status"]
        out.append("   [%d] %s%s → „%s\" (×%d)" % (
            r["id"], r["topic"], mark, r["follow_up"] or "", r["times_surfaced"]))
    out.append("")
    out.append("Uzavřít: /nitky zavři <id>  |  vše vč. uzavřených: /nitky vše")
    return NL_RUNTIME.join(out)


register(
    "nitky",
    slash_aliases=["nitky", "threads"],
    nl_patterns=[],
    handler=_cmd_nitky,
    help_text="Rozjeté nitky per osoba: /nitky [vše|zavři <id>]",
)


# ─── /zajmy — per-osoba zájmy (HANS_PERSON_INTERESTS_V1, frontier #4) ─────
def _cmd_zajmy(handler, name, args) -> str:
    """/zajmy [jméno] — co kterou osobu zajímá."""
    import sqlite3 as _s
    cfg = getattr(handler, "config", {}) or {}
    db = cfg.get("diary_db") or "data/hans_diary.db"
    who = (args or "").strip().lower()
    try:
        conn = _s.connect("file:%s?mode=ro" % db, uri=True, timeout=3.0)
        conn.row_factory = _s.Row
        if who:
            rows = conn.execute(
                "SELECT person,interest,evidence_count FROM person_interests "
                "WHERE status='active' AND person=? ORDER BY evidence_count DESC",
                (who,)).fetchall()
        else:
            rows = conn.execute(
                "SELECT person,interest,evidence_count FROM person_interests "
                "WHERE status='active' ORDER BY person, evidence_count DESC").fetchall()
        conn.close()
    except Exception as e:
        return "Zájmy nedostupné: %s" % e
    if not rows:
        return (("O zájmech osoby %s zatím nic nevím, pane." % who) if who
                else "Zatím neznám zájmy žádné osoby, pane.")
    out = ["Zájmy%s:" % ((" — " + who) if who else "")]
    cur_p = None
    for r in rows:
        if r["person"] != cur_p:
            cur_p = r["person"]
            out.append("")
            out.append("• %s:" % cur_p)
        out.append("   %s (×%d)" % (r["interest"], r["evidence_count"]))
    return NL_RUNTIME.join(out)


register(
    "zajmy",
    slash_aliases=["zajmy", "zájmy", "interests"],
    nl_patterns=[],
    handler=_cmd_zajmy,
    help_text="Co koho zajímá: /zajmy [jméno]",
)


# ─── /studium — studijní program z koníčku (HANS_STUDY_V1, #1 odbornost) ──
_STUDY_NOW = {"teď", "ted", "now", "session", "studuj"}


def _cmd_studium(handler, name, args) -> str:
    """/studium — stav studijního programu; /studium programy = všechny;
    /studium teď = spustí jednu studijní session na pozadí (noční práce ručně)."""
    cfg = getattr(handler, "config", {}) or {}
    db = cfg.get("diary_db", "data/hans_diary.db")
    try:
        from scripts.hans_study import StudyStore, run_study_session
    except Exception as e:
        return "Studijní modul nedostupný: %s" % e
    store = StudyStore(cfg, db)
    sub = (args or "").strip().lower()

    if sub in _STUDY_NOW:
        import threading as _th
        kn = getattr(handler, "_knowledge", None) or getattr(handler, "knowledge", None)

        def _run():
            try:
                code = run_study_session(cfg, db, knowledge=kn)
                _log.info("/studium teď → %s", code)
            except Exception as _e:
                _log.warning("/studium teď selhalo: %s", _e)
        _th.Thread(target=_run, daemon=True, name="StudyNow").start()
        return ("Pustil jsem se do studia, pane — nastuduji další pod-téma. "
                "Chvíli to potrvá (čtení + zápis poznámky), výsledek pak "
                "uvidíte v /studium a v deníku.")

    if sub in {"programy", "programs", "vše", "vse", "all"}:
        progs = store.all_programs()
        if not progs:
            return "Zatím jsem nezačal žádný studijní program, pane."
        out = ["Studijní programy:"]
        for p in progs:
            out.append("  [%d] %s — %s (%d/%d, %d sessions)" % (
                p["id"], p["topic"], p["status"], p["current_index"],
                len(p["curriculum"]), p["sessions_done"]))
        return NL_RUNTIME.join(out)

    ap = store.get_active_program()
    if not ap:
        progs = store.all_programs()
        if progs:
            last = progs[0]
            return ("Právě nestuduji, pane. Naposledy: „%s\" (%s, %d/%d). "
                    "Další program si vyberu z trvalého koníčku. "
                    "(/studium programy, /studium teď)" % (
                        last["topic"], last["status"], last["current_index"],
                        len(last["curriculum"])))
        return ("Zatím jsem nezačal studijní program, pane — vyberu si trvalý "
                "koníček a sestavím kurikulum. (/studium teď to spustí ručně)")

    cur = ap["current_index"]
    total = len(ap["curriculum"])
    out = ["Studuji: „%s\" — pod-téma %d z %d:" % (ap["topic"], cur + 1 if cur < total else total, total)]
    for i, s in enumerate(ap["curriculum"]):
        if i < cur:
            mark = "✓"
        elif i == cur:
            mark = "→"
        else:
            mark = " "
        out.append("   %s %s" % (mark, s))
    out.append("")
    out.append("Sessions: %d  |  ručně: /studium teď" % ap["sessions_done"])
    return NL_RUNTIME.join(out)


register(
    "studium",
    slash_aliases=["studium", "study", "učení", "uceni"],
    nl_patterns=[],
    handler=_cmd_studium,
    help_text="Studijní program: /studium [programy|teď]",
)


# AVATAR_CMD_V1 — ruční inspekce/refresh vizuálního descriptoru (fáze 2 avatara).
_AVATAR_GEN = {"gen", "generuj", "nový", "novy", "znovu", "teď", "ted", "refresh"}


def _cmd_avatar(handler, name, args) -> str:
    """/avatar — vizuální descriptor: bez argumentu ukáže aktuální, 'gen' přegeneruje
    z identity (CORE + tendence + koníčky). Render obrázku = fáze 3 (zatím TBD)."""
    cfg = getattr(handler, "config", {}) or {}
    db = cfg.get("diary_db", "data/hans_diary.db")
    try:
        from scripts.avatar_descriptor import (
            latest_descriptor, generate_descriptor, _save_descriptor,
            render_signature, needs_rerender, ALL_FIELDS)
    except Exception as e:
        return "Avatar modul nedostupný: %s" % e

    sub = (args or "").strip().lower()
    _RENDER_NOTE = ("⚠ Obrázek se zatím negeneruje — render (fáze 3, ComfyUI) "
                    "není postaven. Tohle je jen popis vzhledu.")

    def _fmt(d):
        lines = ["Podoba v%d:" % d.get("version", 0)]
        for f in ALL_FIELDS:
            lines.append("  %s: %s" % (f, d.get(f, "")))
        lines.append("  signature: %s" % render_signature(d))
        return NL_RUNTIME.join(lines)

    if sub in _AVATAR_GEN:
        prev = latest_descriptor(db)
        new = generate_descriptor(cfg, db, prev=prev)
        if not new:
            return "Nepodařilo se vygenerovat descriptor (qwen/Ollama nedostupná? viz log)."
        if needs_rerender(prev, new):
            _save_descriptor(db, new)
            tail = "Uloženo jako v%d (čeká na render)." % new["version"]
        else:
            tail = "Vzhled se neposunul (charakter stejný) — neukládám novou verzi."
        return NL_RUNTIME.join([_fmt(new), "", tail, _RENDER_NOTE])

    cur = latest_descriptor(db)
    if not cur:
        return ("Zatím žádná podoba. /avatar gen ji vygeneruje z aktuální identity "
                "(CORE + tendence + koníčky). " + _RENDER_NOTE)
    return NL_RUNTIME.join([_fmt(cur), "",
                            "/avatar gen = přegeneruj z aktuální identity.", _RENDER_NOTE])


register(
    "avatar",
    slash_aliases=["avatar", "podoba", "tvar"],
    nl_patterns=[],
    handler=_cmd_avatar,
    help_text="Vizuální podoba (descriptor): /avatar [stav|gen]. Render obrázku = fáze 3 (TBD).",
)


# ─── /misto — model místa „Kde jsem" (HANS_PLACE_V1, frontier #4) ─────────
_MISTO_SUBS = {
    "mistnost": "room", "místnost": "room", "pokoj": "room",
    "okno": "window", "okna": "window",
    "dvere": "door", "dveře": "door",
    "vedle": "neighbor", "soused": "neighbor", "sousedni": "neighbor",
    "rozlozeni": "layout", "rozložení": "layout", "layout": "layout",
    "pozn": "note", "poznamka": "note", "poznámka": "note",
}
_MISTO_DEL = {"smaz", "smaž", "odeber", "zrus", "zruš", "del"}
_MISTO_CAT_LABEL = {
    "room": "Místnost", "window": "Okno", "door": "Dveře",
    "neighbor": "Vedle", "layout": "Rozložení", "note": "Pozn.",
    "mental_map": "Z fotek",
}


def _cmd_misto(handler, name, args) -> str:
    """/misto — model domova (kde Hans je). Bez argumentu vypíše model.
    /misto okno <text> | dvere <text> | vedle <text> | mistnost <text> |
    rozlozeni <text> | pozn <text> = přidá fakt. /misto smaz <id> = odebere."""
    cfg = getattr(handler, "config", {}) or {}
    db = (cfg.get("diary_db")
          or (cfg.get("hans_idle", {}) or {}).get("diary_db")
          or "data/hans_diary.db")
    try:
        from scripts.hans_place import PlaceStore
        store = PlaceStore(cfg, db)
    except Exception as e:
        return "Modul místa nedostupný: %s" % e

    parts = (args or "").strip().split(maxsplit=1)
    sub = parts[0].lower() if parts else ""
    rest = parts[1].strip() if len(parts) > 1 else ""

    # namaluj domov (HANS_PLACE_PAINT_V1) — Hans vyrenderuje, jak si dům představuje
    if sub in {"obraz", "namaluj", "render", "nakresli"}:
        import threading as _t
        try:
            from scripts import hans_art
        except Exception as e:
            return "Malování není dostupné: %s" % e
        if not hans_art.comfy_available(cfg):
            return ("Bohužel, pane — výtvarná dílna (ComfyUI na PC) teď neběží, "
                    "tak domov namalovat nemohu. Zkuste to, až bude PC vzhůru.")
        if not store.get_facts():
            return ("Nemám zatím žádný model domova, pane — nejdřív mi ho popište "
                    "(/misto …) nebo nechte fotky v data/room_photos/.")

        def _worker():
            try:
                # Textový render (hezčí výsledek než img2img z reálné fotky).
                # paint_home_from_photo zůstává v hans_art pro budoucí použití.
                hans_art.render_home_now(cfg, db)
            except Exception as _e:
                _log.warning("/misto obraz render selhal: %s", _e)
        _t.Thread(target=_worker, daemon=True).start()
        return ("Dám se do toho, pane — maluji, jak si představuji svůj domov. "
                "Za chvíli se objeví na nástěnce (Co Hans namaloval). "
                "Chat může být asi minutu zaneprázdněný.")

    # smazání faktu
    if sub in _MISTO_DEL:
        if not rest.isdigit():
            return "Uveďte id: /misto smaz <id> (viz /misto)."
        ok = store.remove_fact(int(rest))
        return ("Fakt %s jsem odebral, pane." % rest if ok
                else "Ten fakt se nepodařilo odebrat (špatné id?).")

    # přidání faktu
    if sub in _MISTO_SUBS:
        if not rest:
            return "Doplňte text: /misto %s <popis>." % sub
        cat = _MISTO_SUBS[sub]
        fid = store.add_fact(cat, rest, source="user")
        if cat == "room":
            return "Zapsal jsem, že jsem v místnosti: %s." % rest
        return "Zapsal jsem fakt o místě [%s] (id %s)." % (
            _MISTO_CAT_LABEL.get(cat, cat), fid)

    if sub:
        return ("Neznámá část. Použij: /misto [mistnost|okno|dvere|vedle|"
                "rozlozeni|pozn] <text>, /misto smaz <id>, nebo /misto pro výpis.")

    # výpis modelu
    facts = store.get_facts()
    if not facts:
        return ("O svém místě zatím nic nevím, pane. Můžete mi to popsat: "
                "/misto mistnost <text>, /misto okno <text>, /misto vedle <text> … "
                "Nebo nechte širší fotku místnosti ve složce data/room_photos/ "
                "(udělám si z ní představu při startu).")
    by_cat: dict = {}
    for f in facts:
        by_cat.setdefault(f["category"], []).append(f)
    out = ["Můj model domova (kde jsem):"]
    order = ["room", "window", "door", "neighbor", "layout", "note", "mental_map"]
    for cat in order:
        for f in by_cat.get(cat, []):
            out.append("   [%d] %s: %s" % (
                f["id"], _MISTO_CAT_LABEL.get(cat, cat), f["content"]))
    out.append("")
    out.append("Přidat: /misto okno <text> (i dvere/vedle/mistnost/rozlozeni/pozn)  "
               "|  smazat: /misto smaz <id>  |  namalovat domov: /misto obraz")
    return NL_RUNTIME.join(out)


register(
    "misto",
    slash_aliases=["misto", "místo", "kdejsem", "domov"],
    nl_patterns=[],
    handler=_cmd_misto,
    help_text="Model domova (kde jsem): /misto [mistnost|okno|dvere|vedle|rozlozeni|pozn <text> | smaz <id>]",
)
