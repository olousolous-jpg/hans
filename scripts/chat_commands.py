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
        # NL bez diakritiky — uživatelé často píšou „kalendar"/„udalosti".
        # Fold i vzor i vstup → matchne s háčky i bez nich.
        "nl_fold":  [re.compile(_fold_diacritics(p), re.IGNORECASE)
                     for p in nl_patterns],
        "handler":  handler,
        "help":     help_text,
    }


def _fold_diacritics(s: str) -> str:
    """Odstraní diakritiku (á→a, ř→r, ž→z…). Bezpečné i pro regex vzory
    (mění jen písmena, ne strukturu)."""
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFKD", s or "")
                   if not unicodedata.combining(c))


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

    # Natural language (diakritika i bez ní)
    msg_fold = _fold_diacritics(msg)
    for cmd_id, spec in _COMMANDS.items():
        for pat in spec["nl"]:
            if pat.search(msg):
                return (cmd_id, msg)
        for pat in spec.get("nl_fold", []):
            if pat.search(msg_fold):
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

def _cmd_seznam(handler, name, args) -> str:  # HANS_AGENT_V1 — poznámky/seznam
    """/seznam — výpis poznámek; /seznam hotovo N; /seznam smaz N."""
    import sqlite3 as _sql
    cfg = getattr(handler, "config", {}) or {}
    dbp = (cfg.get("diary", {}) or {}).get("db_path", "data/hans_diary.db")
    a = (args or "").strip().lower()
    try:
        db = _sql.connect(dbp, timeout=5.0)
        db.execute("CREATE TABLE IF NOT EXISTS hans_notes ("
                   "id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, text TEXT, "
                   "done INTEGER NOT NULL DEFAULT 0)")
        m = re.match(r"(hotovo|smaz|smaž)\s+(\d+)", a)
        if m:
            nid = int(m.group(2))
            if m.group(1).startswith("hotov"):
                db.execute("UPDATE hans_notes SET done=1 WHERE id=?", (nid,))
                db.commit(); db.close()
                return f"✓ Položka {nid} označena jako hotová."
            db.execute("DELETE FROM hans_notes WHERE id=?", (nid,))
            db.commit(); db.close()
            return f"✓ Položka {nid} smazána."
        rows = db.execute("SELECT id, text, done FROM hans_notes "
                          "ORDER BY done ASC, id ASC LIMIT 40").fetchall()
        db.close()
        if not rows:
            return "Seznam je prázdný, pane."
        lines = ["📝 Seznam:"]
        for i, t, d in rows:
            lines.append(f"  {'✓' if d else '•'} [{i}] {t}")
        return "\n".join(lines)
    except Exception as e:
        return f"/seznam: chyba ({e})"


register(
    "seznam",
    slash_aliases=["seznam", "poznamky", "poznámky", "todo"],
    nl_patterns=[
        r"\bco.{0,8}m[áa]m.{0,12}seznam",
        r"\buka[žz].{0,12}seznam",
        r"\bm[ůu]j\s+seznam",
        r"\bseznam.{0,12}pozn[áa]mek",
        r"\bco.{0,8}jsem.{0,8}(si\s+)?poznamenal",
    ],
    handler=_cmd_seznam,
    help_text="Výpis poznámek/úkolů (/seznam, /seznam hotovo N, /seznam smaz N)",
)

def _cmd_kalendar(handler, name, args) -> str:  # HANS_CALENDAR_V1
    """/kalendar — nadcházející události z Proton kalendáře; /kalendar sync."""
    cfg = getattr(handler, "config", {}) or {}
    try:
        from scripts.hans_calendar import CalendarStore, is_enabled, people_map
    except Exception:
        return "/kalendar: modul nedostupný."
    person = (name or "").lower()
    if not is_enabled(cfg) or person not in people_map(cfg):
        return ("Váš kalendář zatím nemám napojený, pane. Nasdílejte mi v Proton "
                "Calendar odkaz („pro kohokoli\") a přidejte ho do "
                "config.calendar.people.")
    try:
        dbp = (cfg.get("diary", {}) or {}).get("db_path", "data/hans_diary.db")
        st = CalendarStore(cfg, dbp)
        if (args or "").strip().lower().startswith("sync"):
            n = st.sync()
            return (f"✓ Kalendář synchronizován — {n} událostí." if n >= 0
                    else "⚠ Synchronizace se nezdařila (síť/odkaz).")
        evs = st.upcoming(person, hours=24 * 14, limit=15)
        if not evs:
            return "V nejbližších dvou týdnech nemám ve vašem kalendáři žádnou událost."
        lines = ["📅 Nadcházející:"]
        for e in evs:
            loc = f" ({e['location']})" if e.get("location") else ""
            lines.append(f"  • {st._fmt_when(e)} — {e['summary']}{loc}")
        return "\n".join(lines)
    except Exception as ex:
        return f"/kalendar: chyba ({ex})"


register(
    "kalendar",
    slash_aliases=["kalendar", "kalendář", "kalendar", "calendar"],
    nl_patterns=[
        r"kalend[áa]ř?",
        r"\bco.{0,8}m[áa]m.{0,12}(dnes|z[ií]tra|tento t[ýy]den|tenhle t[ýy]den)",
        r"\bmoje?\s+ud[áa]losti",
        r"napl[áa]novan",  # co mám naplánováno / nemám něco naplánovaného
        r"\bschůzk|\bschuzk",
    ],
    handler=_cmd_kalendar,
    help_text="Nadcházející události z Proton kalendáře (/kalendar, /kalendar sync)",
)


# ─── /rozvrh — Hansův behaviorální rozvrh (HANS_SCHEDULE_V1) ─────────────────
def _cmd_rozvrh(handler, name, args) -> str:  # HANS_SCHEDULE_V1
    """/rozvrh — kompletní Hansův rozvrh autonomních rutin (kdy naposledy tikly,
    zaostávají-li). Doplněk k /zdravi, který ukazuje jen zaostávající."""
    try:
        from scripts.hans_schedule import ScheduleStore
        import os, time
        db = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "data", "hans_diary.db")
        st = ScheduleStore(db)
        rows = st.all()
        if not rows:
            return "Rozvrh je prázdný, pane."
    except Exception as e:
        return f"/rozvrh: chyba ({e})"

    labels = {
        "nightly_analytics":  "Noční analytika",
        "morning_reflection": "Ranní reflexe",
        "study_tick":         "Studijní tick",
        "curiosity_tick":     "Zvědavý tick",
        "calendar_sync":      "Sync Proton kalendáře",
        "catchup_drain":      "Dohnání odložených čtení",
    }
    now = time.time()
    lines = ["📋 Můj rozvrh (autonomní rutiny):"]
    stale_list = st.stale_list(now)
    stale_names = {s["name"] for s in stale_list}
    for r in rows:
        lbl = labels.get(r["name"], r["name"])
        last_ts = r["last_run_ts"]
        if not last_ts:
            when = "ještě neproběhla"
        else:
            age_h = (now - last_ts) / 3600
            if age_h < 1:
                when = f"před {age_h*60:.0f} min"
            elif age_h < 24:
                when = f"před {age_h:.1f}h"
            else:
                when = f"před {age_h/24:.1f} dny"
        gap_h = r["expected_gap_s"] / 3600
        marker = "⚠️ " if r["name"] in stale_names else "  "
        skip = ""
        if r["last_skip_reason"] and not r["last_run_ok"]:
            skip = f" [posl. skip: {r['last_skip_reason']}]"
        enabled = "" if r["enabled"] else " (vypnuto)"
        lines.append(f"{marker}• {lbl} — {when} (max gap {gap_h:.0f}h){skip}{enabled}")
    if stale_list:
        lines.append("")
        lines.append(f"⚠️ Zaostává: {len(stale_list)} z {len(rows)} rutin.")
    else:
        lines.append("")
        lines.append("✅ Vše běží podle plánu.")
    return "\n".join(lines)


register(
    "rozvrh",
    slash_aliases=["rozvrh", "schedule"],
    nl_patterns=[
        r"m[ůu]j\s+rozvrh",
        r"tv[ůu]j\s+rozvrh",
        r"hans[ůu]v\s+(rozvrh|kalend[áa]ř)",
        r"tv[ůu]j\s+kalend[áa]ř",   # „tvůj kalendář" = Hansův (ne Proton)
        r"\brutin[yaou]?\b",
        r"co\s+d[ěe]l[áa]š\s+(v\s+noci|automaticky|rutinn[ěe])",
    ],
    handler=_cmd_rozvrh,
    help_text="Můj rozvrh autonomních rutin (kdy naposled tikly, zaostávají-li)",
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
    # HANS_AUTHORSHIP_V1 — „dilo"/„dílo" patří autorskému projektu (_cmd_dilo),
    # ne tomuhle staršímu ad-hoc /work; jinak by ho /work stínil (registrován dřív).
    slash_aliases=["work", "esej"],
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
        # „co umíš" → schopnosti (capabilities); help drží jen dotaz na příkazy
        r"\bjak[éý].{0,10}p[řr][íi]kaz",
        r"\bseznam\s+p[řr][íi]kaz",
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


# ─── /herni — herní mód: uvolni VRAM pro hru na PC (OLLAMA_GAME_MODE_V1) ──────
_HERNI_ON  = {"zap", "zapni", "on", "1", "ano", "start"}
_HERNI_OFF = {"vyp", "vypni", "off", "0", "ne", "stop", "konec"}


def _cmd_herni(handler, name, args) -> str:
    """/herni [zap|vyp] — herní mód. ZAP: Hans uvolní modely z VRAM a přestane
    používat Ollamu (volná grafika pro hru). VYP: mozek zase k dispozici. Bez
    argumentu přepíná."""
    from scripts.ollama_client import set_game_mode, game_mode_on
    _hi = getattr(handler, "_hans_idle", None)
    cfg = getattr(_hi, "config", None) if _hi else None
    a = (args or "").strip().lower()
    if a in ("stav", "status"):
        return ("Herní mód je ZAPNUTÝ (grafika volná, mozek nepoužívám)."
                if game_mode_on() else "Herní mód je vypnutý.")
    if a in _HERNI_ON:
        target = True
    elif a in _HERNI_OFF:
        target = False
    else:
        target = not game_mode_on()   # toggle
    res = set_game_mode(target, config=cfg)
    if "error" in res:
        return "/herni selhal: %s" % res["error"]
    if target:
        return ("Herní mód ZAP — uvolnil jsem %d model(ů) z grafické paměti a "
                "mozek teď nepoužívám. Až dohraješ: /herni vyp."
                % res.get("unloaded", 0))
    return "Herní mód VYP — mozek je zase k dispozici."


register(
    "herni",
    slash_aliases=["herni", "herní", "hra", "game", "hrani", "hraní"],
    nl_patterns=[
        r"\bjdu\s+hrát",
        r"\bspou[št]t[íě]m\s+hru",
        r"\bhern[íi]\s+m[óo]d",
    ],
    handler=_cmd_herni,
    help_text="Herní mód — uvolní grafiku pro hru na PC (/herni vyp = zpět).",
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


# ─── namaluj/nakresli <téma> — obraz na LIBOVOLNÉ téma (HANS_CAPABILITY_AWARENESS_V1) ──
def _cmd_namaluj(handler, name, args) -> str:
    """namaluj/nakresli <téma> — Hans namaluje obraz na libovolné téma, nebo
    dojem z nedávného rozhovoru (když téma neurčíš). Render na pozadí."""
    import threading as _t
    import re as _re
    cfg = getattr(handler, "config", {}) or {}
    db = (cfg.get("diary_db")
          or (cfg.get("hans_idle", {}) or {}).get("diary_db")
          or "data/hans_diary.db")
    try:
        from scripts import hans_art
    except Exception as e:
        return "Malování není dostupné: %s" % e

    # HANS_ART_SELF_V1 — „namaluj sebe / svůj avatar / jak vypadáš" = Hans maluje
    # SÁM SEBE (ne uživatele!). Musí PŘED strip sloves (ten „se" spolkne jako
    # spojku) i před distill (ten „mě=tazatel" by „sebe" zmapoval na uživatele).
    _raw = (args or "").lower()
    if _re.search(r"\bsebe\b|s[áa]m\s+sebe|\bsv[ůu][jě]?\s+avatar|sv[ée]ho\s+avatar"
                  r"|jak\s+(ty\s+)?vypad[áa]|autoportr[ée]t|namaluj\s+se\b|"
                  r"nakresli\s+se\b", _raw):
        _full = bool(_re.search(
            r"post(av|avu|avě|avou)|cel(ou|é|ého)\s*(t[ěe]lo|postav)?|"
            r"full\s*body|od\s+hlavy", _raw))
        _style_self = ""
        _ss = _re.search(
            r"(?i)(?:ve?\s+stylu|stylem|jako\s+od|po\s+vzoru)\s+(.+)$", _raw)
        if _ss:
            _style_self = _ss.group(1).strip(" ?.!,")

        def _self_render():
            try:
                r = hans_art.paint_self(cfg, db, full_figure=_full,
                                        style=_style_self)
                _log.info("namaluj SEBE (full=%s) → %s", _full,
                          "ok" if r else "nevyšlo")
            except Exception as _e:
                _log.warning("paint_self: %s", _e)
        _t.Thread(target=_self_render, daemon=True).start()
        return ("Namaluji sám sebe, pane — %s ze své avatarové podoby. Chvíli "
                "to potrvá, pak se podívej do galerie." %
                ("celou postavu" if _full else "podobiznu"))

    # HANS_ART_TV_V1 — „namaluj co dávají v TV / co běží / co hraje" → ŽIVÝ Kodi
    # stav (ne konverzace!). Namaluje aktuálně běžící pořad/film.
    if _re.search(r"co\s+(d[áa]v|b[ěe][žz]|hraj|je)\w*\s+(pr[áa]v[ěe]\s+)?"
                  r"(v\s+)?(tv|telev|kin[eě]|obrazovc)|"
                  r"co\s+(se\s+)?(pr[áa]v[ěe]\s+)?(hraje|d[áa]v[áa]|b[ěe][žz][íi]|"
                  r"koukám|d[íi]v[áa]m)|(film|po[řr]ad|seri[áa]l)\s+co\s+(hraje|"
                  r"b[ěe][žz]|d[áa]v)", _raw):
        try:
            from scripts.kodi_client import KodiClient
            _np = KodiClient(cfg).get_now_playing()
        except Exception as _ke:
            _log.debug("namaluj TV kodi: %s", _ke)
            _np = None
        if _np and (_np.get("title") or _np.get("label")):
            # HANS_ART_TV_GROUNDING_V1 — námět z POPISU děje (Kodi plot), při
            # chybějícím popisku dohledej na internetu; teprve pak jen název.
            _disp = (_np.get("title") or _np.get("label") or "").split(",")[0].strip()
            try:
                _subj, _src = hans_art.tv_paint_subject(cfg, db, _np)
            except Exception as _te:
                _log.debug("tv_paint_subject: %s", _te)
                _subj, _src = _disp, "jen podle názvu"
            _t.Thread(target=lambda: hans_art.paint_subject(cfg, db, _subj),
                      daemon=True).start()
            _log.info("namaluj CO V TV → '%s' (%s)", _subj[:60], _src)
            _note = {"z popisu pořadu": "podle jeho děje",
                     "z internetu (popisek u pořadu chyběl)":
                        "u pořadu chyběl popisek, tak jsem si děj dohledal na internetu",
                     "jen podle názvu":
                        "popisek chyběl a nedohledal jsem víc, takže jen podle názvu"
                     }.get(_src, "")
            return ("Namaluji, co právě běží na obrazovce, pane — „%s“%s. Chvíli "
                    "to potrvá, pak se podívej do galerie." %
                    (_disp, (" (%s)" % _note) if _note else ""))
        return ("V tuto chvíli na televizi nic nehraje, pane — nemám co "
                "namalovat z obrazovky.")

    # vytáhni téma z požadavku (odřízni sloveso a spojky)
    subj = (args or "").strip()
    # \w* za kmenem slovesa pokryje ČASOVANÉ tvary: „namaluješ/namaluje/namaloval
    # bys/nakreslíš" (dřív se ořízl jen „namaluj" → zbylo „eš o tom obraz").
    subj = _re.sub(r"(?i)^\s*(prosím\s+|můžeš\s+|mohl\s+bys\s+|nemohl\s+bys\s+)?"
                   r"(mi\s+)?(namaluj\w*|namalovat|namaloval\w*|nakresl\w*|"
                   r"vytvoř\w*|přemaluj\w*|překresl\w*)"
                   r"(\s+bys?|\s+byste)?"
                   r"\s*(mi\s+)?(prosím\s+)?(obraz|obrázek)?\s*"
                   r"(o\s+|s\s+|se\s+|na\s+t[eé]ma\s+|ohledně\s+|podle\s+|"
                   r"toho\s+jak\s+)?", "", subj).strip(" ?.!,")

    # HANS_ART_STYLE_V4 — odděl STYL od námětu („X ve stylu Y" / „stylem Y")
    style = ""
    _sm = _re.search(
        r"(?i)[\s,]+(?:ve?\s+stylu|stylem|jako\s+od|po\s+vzoru|[àa]\s+la)\s+(.+)$",
        subj)
    if _sm:
        style = _sm.group(1).strip(" ?.!,")
        subj = subj[:_sm.start()].strip(" ?.!,")

    # odkaz na rozhovor → sestav téma z posledních uživatelských zpráv
    if not subj or _re.search(r"(?i)bavili|mluvili|povídali|rozhovor|o\s+čem", subj):
        try:
            hist = handler.conv_store.get_history(name) or []
            ux = [m["content"] for m in hist if m.get("role") == "user"][-4:]
            ux = [u for u in ux if not u.strip().lower().startswith(
                ("namaluj", "nakresli", "vytvoř"))]
            if ux:
                subj = "náš rozhovor: " + " • ".join(u[:80] for u in ux[-3:])
        except Exception:
            pass
    if not subj:
        subj = "dojem z našeho nedávného rozhovoru"

    # HANS_ART_SUBJECT_DISTILL_V1 — messy požadavek / odkaz na rozhovor
    # („tu kočku", „zkus znovu", „o čem jsme se bavili") → destiluj JEDEN
    # čistý námět přes LLM + kontext historie (řeší reference, ořeže instrukce).
    subj = _distill_paint_subject(cfg, name, handler, subj)

    if not hans_art.comfy_available(cfg):
        return ("Rád bych, pane — ale má výtvarná dílna (ComfyUI na PC) teď neběží. "
                "Až bude PC vzhůru, obraz namaluji.")

    def _worker():
        try:
            hans_art.paint_subject(cfg, db, subj, style=style)
        except Exception as _e:
            _log.warning("namaluj render selhal: %s", _e)
    _t.Thread(target=_worker, daemon=True).start()
    _st = (" ve stylu „%s\"" % style) if style else ""
    return ("S radostí, pane — maluji obraz na téma „%s\"%s. Za chvíli se objeví na "
            "nástěnce (Co Hans namaloval); chat může být asi minutu zaneprázdněný."
            % (subj[:70], _st))


def _distill_paint_subject(config, name, handler, subj: str) -> str:
    """HANS_ART_SUBJECT_DISTILL_V1 — z messy požadavku + kontextu rozhovoru
    destiluj JEDEN výtvarný námět (2-6 slov, česky). Řeší odkazy („tu kočku"
    → kočka, „o čem jsme se bavili" → téma), ořeže instrukce („zkus znovu",
    „vypadá nedokončeně"). Fallback = původní subj (LLM dole/podezřelý výstup)."""
    import re as _re2
    toks = set(_re2.findall(r"\w+", subj.lower()))
    _ref = {"to", "ho", "ji", "tu", "ten", "tenhle", "tohle", "toho",
            "tuhle", "tamtu", "mě", "mne", "mně", "mnou", "me", "sebe",
            # odkazy na PŘEDCHOZÍ téma („o tom", „o něm") → destiluj z kontextu
            "tom", "tomhle", "tomto", "něm", "nem", "něj", "nej", "nich"}
    # POZOR: NEspouštět destilaci jen podle DÉLKY — explicitní víceslovný námět
    # („velký mimoň a spousta malých") se pak s těžkým kontextem přebil na téma
    # z předchozího rozhovoru („Les Camerounais"). Destiluj JEN u skutečných
    # odkazů (zájmena) nebo instrukčního šumu — jinak zadání RESPEKTUJ.
    messy = (subj.startswith(("náš rozhovor:", "dojem z"))
             or bool(toks & _ref)
             or any(k in subj.lower() for k in (
                 "zkus", "jeste", "ještě", "znovu", "vypad", "nedokon",
                 "myslel jsem", "o mně", "o mne")))
    if not messy:
        return subj
    try:
        conv = getattr(handler, "conv_store", None)
        hist = conv.get_history(name) if conv else []
        ctx = "\n".join(
            "%s: %s" % ("Uživatel" if m.get("role") == "user" else "Hans",
                        (m.get("content") or "")[:150])
            for m in (hist or [])[-6:])
        from scripts.ollama_client import ollama_generate
        model = ((config.get("dialog", {}) or {}).get("model")
                 or "hans-czech:latest")
        _who = ("Ten, kdo píše, se jmenuje %s. „mě/o mně\" = tato osoba "
                "(portrét či scéna o ní), NE obecný pojem „uživatel\". "
                % name) if name else ""
        system = (
            "Jsi extraktor výtvarného NÁMĚTU pro malbu. Z posledního "
            "požadavku uživatele a kontextu rozhovoru urči JEDEN konkrétní "
            "námět obrazu (CO má být namalováno), česky, 2 až 6 slov. Rozřeš "
            "odkazy: „tu kočku\" → kočka; „to/o čem jsme se bavili\" → to "
            "téma z kontextu; „dnešní počasí\" → konkrétní počasí z kontextu. "
            + _who +
            "DŮLEŽITÉ: když požadavek UŽ pojmenovává konkrétní věc k namalování "
            "(„velký mimoň a spousta malých\", „západ slunce nad mořem\"), vrať "
            "PŘESNĚ TU VĚC (jen zkrať) — kontext použij POUZE k rozřešení "
            "zájmen (to/tom/ho/toho); NIKDY jím NEPŘEBÍJEJ jasně zadaný námět. "
            "IGNORUJ instrukce jako „zkus znovu\", „vypadá nedokončeně\" — to "
            "NENÍ námět. NEPŘIDÁVEJ nic navíc. Vrať POUZE námět, jedním "
            "krátkým slovním spojením.")
        prompt = "%s\n\nPožadavek: %s\n\nNÁMĚT:" % (ctx, subj)
        raw = ollama_generate(model, prompt, system=system, config=config,
                              timeout=25, keep_alive=-1,
                              options={"temperature": 0.1, "num_predict": 30})
        if not raw:
            return subj
        out = raw.strip().splitlines()[0]
        out = _re2.sub(r"(?i)^\s*n[áa]m[ěe]t\s*:?\s*", "", out)
        out = out.strip(" \"'„“”?.!:•-")
        if out and 1 <= len(out.split()) <= 8 and 2 <= len(out) <= 60 \
                and not out.lower().startswith(("nevím", "nemám", "promiň")):
            _log.info("art subject destilován: %r → %r", subj[:50], out)
            return out
    except Exception as _e:
        _log.debug("distill subject: %s", _e)
    return subj


register(
    "namaluj",
    slash_aliases=["namaluj", "nakresli"],
    nl_patterns=[r"\bnamaluj", r"\bnamalovat\b", r"\bnakresli", r"vytvoř\s+obr",
                 r"\bp[řr]ekresli", r"\bp[řr]emaluj",
                 r"\boprav\s+(ten\s+|ten[hz]le\s+)?(obraz|obr[áa]zek)"],
    handler=_cmd_namaluj,
    help_text="Hans namaluje/překreslí obraz: namaluj <téma> (i namaluj to jinak)",
)


# ─── /schopnosti — co Hans reálně umí (HANS_CAPABILITY_AWARENESS_V1) ─────────
def _cmd_schopnosti(handler, name, args) -> str:
    try:
        from scripts.hans_capabilities import capabilities_report
        return capabilities_report()
    except Exception as e:
        return "Přehled schopností nedostupný: %s" % e


register(
    "schopnosti",
    slash_aliases=["schopnosti", "umis", "umíš", "capabilities"],
    nl_patterns=[r"co\s+(v[šs]echno\s+)?um[ií][šs]", r"co\s+dok[aá][žz]e[šs]"],
    handler=_cmd_schopnosti,
    help_text="Přehled toho, co Hans umí: /schopnosti",
)


# ─── co hraje? — ŽIVÁ kontrola Kodi (HANS_LIVE_PLAYBACK_QUERY_V1) ────────────
def _cmd_hraje(handler, name, args) -> str:
    """co hraje / co se přehrává — Hans zkontroluje ŽIVÝ stav Kodi (ne deník);
    nic nehraje → navrhne film. Funguje i přes Telegram (jde přes send_chat_message).
    Pozn.: Kodi (252) je samostatné zařízení, funguje i když PC spí."""
    cfg = getattr(handler, "config", {}) or {}
    _hi = getattr(handler, "_hans_idle", None)
    kodi = getattr(_hi, "kodi", None) if _hi is not None else None
    if kodi is None:
        try:
            from scripts.kodi_client import KodiClient
            kodi = KodiClient(cfg)
        except Exception:
            return "Bohužel se teď nemohu spojit s přehrávačem, pane."
    # 1) živý stav
    try:
        now = kodi.get_now_playing()
    except Exception as e:
        _log.warning("hraje: get_now_playing selhal: %s", e)
        now = None
    if now:
        title = (now.get("title") or now.get("label") or "").strip()
        show = (now.get("showtitle") or "").strip()
        ep, se = now.get("episode"), now.get("season")
        if show and ep:
            base = f"seriál „{show}\""
            if se:
                base += f" (řada {se}, díl {ep})"
            elif ep:
                base += f" (díl {ep})"
            if title and title != show:
                base += f" – {title}"
            desc = base
        else:
            yr = now.get("year")
            desc = f"„{title}\"" + (f" ({yr})" if yr else "")
        return f"Právě se přehrává {desc}, pane."
    # 2) nic nehraje → návrh filmu
    names = list(getattr(_hi, "_present_names", []) or []) if _hi is not None else []
    if not names and name:
        names = [name]
    m = None
    if _hi is not None:
        try:
            m = _hi._pick_next_film(names, cfg.get("film_suggest", {}) or {})
        except Exception as e:
            _log.warning("hraje: _pick_next_film selhal: %s", e)
    if m is None:
        try:
            m = kodi.pick_suggestion(prefer_genres=kodi.favorite_genres())
        except Exception:
            m = None
    if m:
        mt = (m.get("title") or m.get("label") or "").strip()
        yr = m.get("year")
        return (f"Teď nic nehraje, pane. Mohl bych navrhnout „{mt}\""
                + (f" ({yr})" if yr else "")
                + " — stačí říct a pustím to.")
    return "Teď se nic nepřehrává, pane, a vhodný návrh se mi teď nepodařilo najít."


register(
    "hraje",
    slash_aliases=["hraje", "prehrava", "přehrává"],
    nl_patterns=[
        r"co\s+(te[ďd]\s+)?hraj[eí]",
        r"hraje\s+(te[ďd]\s+)?n[ěe]jak",
        r"co\s+se\s+(te[ďd]\s+)?p[řr]ehr[aá]v[aá]",
        r"co\s+b[ěe][žz][ií]\s+(te[ďd]\s+)?(v\s+)?(televiz|tv|kodi)",
        r"co\s+d[aá]vaj[ií]\s+(te[ďd]\s+)?(v\s+)?(televiz|tv)",
    ],
    handler=_cmd_hraje,
    help_text="Co se právě přehrává (živě z Kodi): co hraje?",
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


# ─── /dilo — autorský projekt (HANS_AUTHORSHIP_V1) ───────────────────────────
def _cmd_dilo(handler, name, args) -> str:
    """/dilo — stav autorského projektu; /dilo vše = všechny; /dilo teď = napiš
    další sekci na pozadí (jinak noční práce)."""
    cfg = getattr(handler, "config", {}) or {}
    db = cfg.get("diary_db", "data/hans_diary.db")
    try:
        from scripts.hans_authorship import AuthorshipStore, run_writing_session
    except Exception as e:
        return "Autorský modul nedostupný: %s" % e
    store = AuthorshipStore(cfg, db)
    sub = (args or "").strip().lower()

    if sub in {"teď", "ted", "now", "piš", "pis", "session"}:
        import threading as _th
        kn = getattr(handler, "_knowledge", None) or getattr(handler, "knowledge", None)

        def _run():
            try:
                _log.info("/dilo teď → %s", run_writing_session(cfg, db, knowledge=kn))
            except Exception as _e:
                _log.warning("/dilo teď selhalo: %s", _e)
        _th.Thread(target=_run, daemon=True, name="WriteNow").start()
        return ("Pustil jsem se do psaní, pane — napíšu další sekci svého díla. "
                "Chvíli to potrvá, výsledek pak uvidíte v /dilo a v deníku.")

    if sub in {"vše", "vse", "all", "projekty"}:
        projs = store.all_projects()
        if not projs:
            return "Zatím jsem nezačal žádné dílo, pane."
        out = ["Má díla:"]
        for p in projs:
            out.append("  [%d] „%s\" (%s) — %s (%d/%d sekcí)" % (
                p["id"], p["title"], p["kind"], p["status"],
                p["current_index"], len(p["outline"])))
        return NL_RUNTIME.join(out)

    ap = store.get_active()
    if not ap:
        projs = store.all_projects()
        if projs:
            last = projs[0]
            return ("Právě nepíšu, pane. Naposledy: „%s\" (%s). Najdete ho "
                    "v data/works/. Další dílo si vyberu z trvalého koníčku. "
                    "(/dilo vše, /dilo teď)" % (last["title"], last["status"]))
        return ("Zatím jsem nezačal psát, pane — vyberu si trvalý koníček a "
                "navrhnu dílo. (/dilo teď to spustí ručně)")

    cur, total = ap["current_index"], len(ap["outline"])
    out = ["Píšu: „%s\" (%s) — sekce %d z %d:" % (
        ap["title"], ap["kind"], cur + 1 if cur < total else total, total)]
    if ap.get("premise"):
        out.append("   námět: %s" % ap["premise"])
    for i, s in enumerate(ap["outline"]):
        mark = "✓" if i < cur else ("→" if i == cur else " ")
        out.append("   %s %s" % (mark, s))
    out.append("")
    out.append("Sessions: %d  |  ručně: /dilo teď" % ap["sessions_done"])
    return NL_RUNTIME.join(out)


register(
    "dilo",
    slash_aliases=["dilo", "dílo", "psani", "psaní", "kniha_moje"],
    nl_patterns=[],
    handler=_cmd_dilo,
    help_text="Autorský projekt: /dilo [vše|teď]",
)


# ─── /napad — vlastní nápady / synteze (HANS_SYNTHESIS_IDEAS_V1, #2) ──────────
def _cmd_napad(handler, name, args) -> str:
    """/napad — poslední Hansův postřeh; /napad vše = všechny; /napad teď =
    propoj věci z různých oblastí do nového postřehu (na pozadí, jinak v noci)."""
    cfg = getattr(handler, "config", {}) or {}
    db = cfg.get("diary_db", "data/hans_diary.db")
    try:
        from scripts.hans_ideas import IdeaStore, run_synthesis_session
    except Exception as e:
        return "Modul nápadů nedostupný: %s" % e
    store = IdeaStore(cfg, db)
    sub = (args or "").strip().lower()

    if sub in {"teď", "ted", "now", "synteze", "syntéza"}:
        import threading as _th
        kn = getattr(handler, "_knowledge", None) or getattr(handler, "knowledge", None)

        def _run():
            try:
                _log.info("/napad teď → %s",
                          run_synthesis_session(cfg, db, knowledge=kn))
            except Exception as _e:
                _log.warning("/napad teď selhalo: %s", _e)
        _th.Thread(target=_run, daemon=True, name="SynthNow").start()
        return ("Zkusím propojit pár věcí, co jsem se dozvěděl, pane — chvíli to "
                "potrvá. Postřeh pak najdete v /napad a v deníku.")

    if sub in {"vše", "vse", "all"}:
        ideas = store.all_ideas()
        if not ideas:
            return "Zatím mě nic nového nenapadlo, pane."
        import time as _t
        out = ["Mé postřehy:"]
        for it in ideas:
            day = _t.strftime("%-d.%-m.", _t.localtime(it["ts"]))
            out.append("  [%s] %s" % (day, (it["topics"] or "—")))
            out.append("     %s" % (it["insight"] or ""))
        return NL_RUNTIME.join(out)

    last = store.latest()
    if not last:
        return ("Zatím mě nic nového nenapadlo, pane — propojím věci z různých "
                "oblastí, které jsem si přečetl. (/napad teď to spustí ručně)")
    import time as _t
    day = _t.strftime("%-d.%-m.", _t.localtime(last["ts"]))
    return NL_RUNTIME.join([
        "Poslední postřeh (%s) — propojil jsem: %s" % (day, last["topics"] or "—"),
        "", last["insight"] or "",
        "", "ručně: /napad teď"])


register(
    "napad",
    slash_aliases=["napad", "nápad", "napady", "nápady", "synteze", "syntéza"],
    nl_patterns=[],
    handler=_cmd_napad,
    help_text="Vlastní nápady / synteze: /napad [vše|teď]",
)


# ─── /kritika — sebekritika z vlastního popudu (HANS_SELFCRITIQUE_V1, #6) ─────
def _cmd_kritika(handler, name, args) -> str:
    """/kritika — nedávná Hansova ponaučení o kvalitě vlastního projevu;
    /kritika teď = projdi své poslední repliky a vezmi si ponaučení (na pozadí)."""
    cfg = getattr(handler, "config", {}) or {}
    db = cfg.get("diary_db", "data/hans_diary.db")
    try:
        from scripts.hans_selfcritique import (
            recent_selfcritiques, run_self_critique)
    except Exception as e:
        return "Modul sebekritiky nedostupný: %s" % e
    sub = (args or "").strip().lower()

    if sub in {"teď", "ted", "now"}:
        import threading as _th

        def _run():
            try:
                _log.info("/kritika teď → %s", run_self_critique(cfg, db))
            except Exception as _e:
                _log.warning("/kritika teď selhalo: %s", _e)
        _th.Thread(target=_run, daemon=True, name="SelfCritique").start()
        return ("Projdu si své poslední odpovědi, pane, a vezmu si z nich "
                "ponaučení. Chvíli to potrvá — pak je uvidíte v /kritika.")

    crits = recent_selfcritiques(db, hours=24 * 30, limit=10)
    if not crits:
        return ("Zatím jsem si žádné ponaučení o vlastním projevu nevzal, pane. "
                "(/kritika teď to spustí ručně)")
    out = ["Co u sebe chci zlepšit:"]
    for c in crits:
        out.append("  • %s" % c)
    return NL_RUNTIME.join(out)


register(
    "kritika",
    slash_aliases=["kritika", "sebekritika", "sebereflexe"],
    nl_patterns=[],
    handler=_cmd_kritika,
    help_text="Sebekritika vlastního projevu: /kritika [teď]",
)


# ─── /dashboard — Hansův návrh vlastní nástěnky (HANS_DASHBOARD_PROPOSAL_V1) ──
def _cmd_dashboard(handler, name, args) -> str:
    """/dashboard — Hansova designová kritika + návrh vlastní nástěnky;
    /dashboard teď = vygeneruj hned (i bez dokončeného studia, na pozadí)."""
    cfg = getattr(handler, "config", {}) or {}
    db = cfg.get("diary_db", "data/hans_diary.db")
    try:
        from scripts.hans_dashboard import latest_proposal, run_dashboard_proposal
    except Exception as e:
        return "Modul návrhu nástěnky nedostupný: %s" % e
    sub = (args or "").strip().lower()

    if sub in {"teď", "ted", "now"}:
        import threading as _th

        def _run():
            try:
                _log.info("/dashboard teď → %s",
                          run_dashboard_proposal(cfg, db, force=True))
            except Exception as _e:
                _log.warning("/dashboard teď selhalo: %s", _e)
        _th.Thread(target=_run, daemon=True, name="DashboardProposal").start()
        return ("Zamyslím se nad podobou své nástěnky, pane — kritika i návrh "
                "chvíli potrvají (a zkusím i obrazový mockup). Pak /dashboard.")

    p = latest_proposal(db)
    if not p:
        return ("Návrh své nástěnky jsem zatím nesepsal — přijde sám po "
                "dostudování designu, nebo ho vyžádejte přes /dashboard teď.")
    import datetime as _dt
    when = _dt.datetime.fromtimestamp(p["ts"]).strftime("%d.%m. %H:%M")
    out = f"Můj návrh nástěnky ({when}):\n\n{p['text']}"
    if p.get("path"):
        out += f"\n\n(Mockup: {p['path']} — najdete v galerii.)"
    return out


register(
    "dashboard",
    slash_aliases=["dashboard", "nastenka", "nástěnka"],
    nl_patterns=[],
    handler=_cmd_dashboard,
    help_text="Hansův návrh vlastní nástěnky: /dashboard [teď]",
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


# ─── HANS_RECALL_SHORTCIRCUIT_V1 — vnitřní paměťové dotazy PŘÍMO Z DAT ────────
# (#1 anti-konfabulačního pořadí) — „první vzpomínka" / „co jsi četl" /
# „kdy jsi mě viděl" se NEposílají do LLM: odpověď je deterministická šablona
# z deníku (vzor HANS_LIVE_PLAYBACK_QUERY_V1). Nulová konfabulace.

def _recall_db(handler) -> str:
    cfg = getattr(handler, "config", {}) or {}
    return (cfg.get("diary_db")
            or (cfg.get("hans_idle", {}) or {}).get("diary_db")
            or "data/hans_diary.db")


def _cmd_vzpominka(handler, name, args) -> str:
    from scripts.hans_recall import first_memory_answer
    out = first_memory_answer(_recall_db(handler))
    return out or "Nepodařilo se mi teď nahlédnout do deníku, pane."


register(
    "vzpominka",
    slash_aliases=["vzpominka", "vzpomínka"],
    nl_patterns=[
        r"(prvn[íi]|nejstarš[íi])\s+(tvoje?\s+|tv[áa]\s+)?vzpom[íi]nk",
        r"vzpom[íi]nk\w*\s+(m[áa]š\s+)?(jako\s+)?(úplně\s+)?prvn[íi]",
        r"co\s+si\s+pamatuje[šs]\s+(jako\s+|ze\s+všeho\s+)?(úplně\s+)?"
        r"(prvn[íi]|nejd[řr][íi]v)",
        r"nejstarš[íi]\s+z[áa]znam",
    ],
    handler=_cmd_vzpominka,
    help_text="Má první/nejstarší vzpomínka (přímo z deníku, žádný odhad)",
)


def _cmd_cetl(handler, name, args) -> str:
    from scripts.hans_recall import reading_answer
    out = reading_answer(_recall_db(handler), args or "")
    return out or "Nepodařilo se mi teď nahlédnout do deníku, pane."


register(
    "cetl",
    slash_aliases=["cetl", "četl", "cteni", "čtení"],
    nl_patterns=[
        r"\bco\s+(jsi|sis)\s+(dnes\w*\s+|včera\s+|naposledy\s+)?"
        r"(pře)?[čc]etl",
        r"\bcos?\s+(dnes\w*\s+|včera\s+|naposledy\s+)?[čc]etl",
        r"\bkdy\s+(jsi|sis)\s+[čc]etla?\b",
        r"\b(pře)?[čc]etla?\s+(jsi|sis)\s+(něco|neco|někdy|nekdy|už|uz)?\s*o?\b.{2,}\?",
        r"\bco\s+(pr[áa]vě\s+|te[ďd]\s+)?[čc]te[šs]\b",
    ],
    handler=_cmd_cetl,
    help_text="Co/kdy jsem četl (přímo z deníku): co jsi četl? četl jsi o X?",
)


def _cmd_videl(handler, name, args) -> str:
    cfg = getattr(handler, "config", {}) or {}
    from scripts.hans_recall import last_seen_answer
    out = last_seen_answer(_recall_db(handler), cfg, args or "", name)
    return out or "Nepodařilo se mi teď nahlédnout do deníku, pane."


register(
    "videl",
    slash_aliases=["videl", "viděl"],
    nl_patterns=[
        # jen mě/nás — obecné „kdy jsi X viděl" (film, věc) patří LLM,
        # špatný deterministický únos by byl horší než žádný. Jiné osoby
        # jdou přes /videl <jméno> (resolve person_name_forms v handleru).
        r"\bkdy\s+(jsi|si)\s+(m[ěe]|n[áa]s)\s+(naposledy\s+)?vid[ěe]l",
        r"\bvid[ěe]l\s+(jsi|si)\s+m[ěe]\s+(dnes|včera|naposledy)",
    ],
    handler=_cmd_videl,
    help_text="Kdy jsem koho naposledy viděl (přímo z deníku person_seen)",
)


def _cmd_film(handler, name, args) -> str:  # HANS_RECALL_FILM_V1
    from scripts.hans_recall import films_watched_answer
    out = films_watched_answer(_recall_db(handler), args or "")
    return out or "Nepodařilo se mi teď nahlédnout do deníku, pane."


register(
    "film",
    slash_aliases=["film", "filmy"],
    nl_patterns=[
        r"posledn[ií].{0,10}film",
        r"jak[ýy].{0,10}film",
        r"co\s+(jsi|sis)\s+(dnes\w*\s+|včera\s+|naposledy\s+)?"
        r"(vid[ěe]l|koukal|sledoval|d[íi]val)",
        r"co\s+jsem?\s+(dnes\w*\s+)?(vid[ěe]l|koukal|sledoval)",
        r"\bfilm\w*\s+(jsi|sis)\s+(vid|koukal|sledoval)",
    ],
    handler=_cmd_film,
    help_text="Jaký film/pořad jsem viděl (přímo z deníku kodi_playing)",
)


def _cmd_rozhovory(handler, name, args) -> str:  # HANS_CHAT_SUMMARY_V1
    """Sumář toho, o čem se TAZATEL s Hansem bavil (deterministicky z deníku).
    Časová reference v dotazu („v pátek", „27. dubna 2026", „minulý týden")
    zúží období; bez ní = poslední den, kdy spolu mluvili. Delší období →
    témata; „připomeň rozhovor o X" → doslovné vybavení té výměny."""
    from scripts.hans_recall import (chat_summary, topic_conversation,
                                     _extract_conv_topic)
    cfg = getattr(handler, "config", {}) or {}
    q = args or ""
    topic = _extract_conv_topic(q)
    if topic:
        out = topic_conversation(_recall_db(handler), name, topic)
        if out:
            return out
    out = chat_summary(_recall_db(handler), name, q, config=cfg)
    return out or "Nepodařilo se mi teď nahlédnout do deníku, pane."


register(
    "rozhovory",
    slash_aliases=["rozhovory", "rozhovor", "souhrn", "sumar", "sumář"],
    nl_patterns=[
        # Uživatel píše s překlepy a i/y („bavily", „pripomenou") → vzory musí
        # být tolerantní; jinak dotaz propadne do LLM a to si vymyslí rozhovor,
        # který se nikdy nestal (doložený případ 13.7. — smyšlený Vietnam).
        # „o čem jsme (se) bavili/mluvili/povídali (v pátek / 27. dubna)"
        r"o\s+[čc]em\s+jsme\b",
        r"co\s+jsme\s+(spolu\s+)?(prob[íi]r|[řr]e[šs]il|probir)",
        # „shrň náš rozhovor", „shrň o čem jsme mluvili"
        r"(shr[ňn]|shrnout|sum[áa]rizuj)\s+\w*\s*(n[áa][šs]\s+)?"
        r"(rozhovor|konverzac|chat)",
        # „vytáhni/vytáhneš vzpomínku z 27. dubna", „vzpomínky z minulého týdne"
        r"(vyt[áa]h\w*|uk[áa][žz]\w*|najdi)\s+\w*\s*vzpom[íi]nk",
        r"vzpom[íi]nk\w*\s+z\s+(\d|[a-zá-ž]{4,})",
        # „na co jsem se tě ptal (v pátek)"
        r"na\s+co\s+jsem\s+se\s+t[ěe]\s+ptal",
        # detail na vyžádání: „připomeň (mi) rozhovor o Maradonovi"
        r"(p[řr]ipome[ňnt]\w*|vzpome[ňn]\w*|zopakuj)\s+.{0,25}"
        r"(rozhovor|konverzac|bavil|mluvil|pov[íi]dal)",
        r"(rozhovor|konverzac\w*)\s+o\s+\w{3,}",
        r"(bavil|mluvil|pov[íi]dal)[iy]\s+jsme\s+(se\s+)?o\s+\w{3,}",
        # „pošli detail o rychlém obědě…", „ukaž ten recept", „vypiš záznam o…"
        # Bez tohohle Hans odpověď VYGENERUJE ZNOVU (doložený případ 13.7. —
        # do receptu si přidal koriandr, který v původním zápisu nebyl).
        r"(po[šs]l\w*|uka[žz]\w*|zopakuj\w*|vypi[šs]\w*|dej\s+mi)\s+"
        r".{0,25}(detail|recept|postup|z[áa]znam|z[áa]pis)",
        r"co\s+jsi\s+(mi\s+)?(psal|napsal|poslal|[řr][íi]kal|navrhl|"
        r"doporu[čc]il)",
        r"\b(ten|tu|to)\s+(recept|postup|n[áa]vrh)\b",
    ],
    handler=_cmd_rozhovory,
    help_text="O čem jsme se bavili (z deníku): /rozhovory [v pátek | "
              "27. dubna 2026 | minulý týden]; detail: „připomeň rozhovor o X“",
)


# ─── /hlidej — hlídací režim (HANS_GUARD_V1) ─────────────────────────────────
# Prázdný dům: Hans střeží místnost a při POHYBU / NÁHLÉ ZMĚNĚ SVĚTLA pošle
# snímek na Telegram. Obchází noční spánek vidění (framy tečou vždy) a drží
# kameru v místnosti (jinak by v noci koukala do stropu).

def _guard_camera_down(handler) -> None:
    """Zapnuto během spánku → vrať kameru z stropu do místnosti."""
    try:
        hi = getattr(handler, "_hans_idle", None)
        routine = getattr(hi, "_routine", None) if hi else None
        servo = getattr(routine, "_servo", None) if routine else None
        if routine is not None and getattr(routine, "_sleeping", False) \
                and servo is not None and hasattr(servo, "manual_tilt"):
            servo.manual_tilt(0)
            _log.info("/hlidej: Hans spí → kamera vrácena do místnosti")
    except Exception as e:
        _log.warning("/hlidej: návrat kamery selhal: %s", e)


def _cmd_hlidej(handler, name, args) -> str:
    from scripts import hans_guard as g
    a = (args or "").strip().lower()
    # NL cesta posílá CELOU větu jako args → „vypni hlídání" nesmí režim
    # omylem ZAPNOUT. Rozhoduje záměr ve větě, ne přesná shoda.
    if re.search(r"\b(stop|vypni|vypnout|konec|off|zru[šs])\b", a):
        g.disarm()
        return "Hlídání jsem vypnul, pane. Snímky už posílat nebudu."
    if re.search(r"\b(stav|status)\b", a):
        return g.status_text()

    cfg = getattr(handler, "config", {}) or {}
    tg = getattr(handler, "telegram", None)
    if tg is None or not getattr(tg, "enabled", False):
        return ("Hlídat mohu, ale nemám kam poslat snímky — Telegram není "
                "zapojený, pane. Bez něj by poplach nikdo neviděl.")
    g.arm(by=name or "")
    _guard_camera_down(handler)
    c = (cfg.get("guard", {}) or {})
    return ("Hlídám, pane. Při pohybu nebo náhlé změně světla pošlu snímek "
            "na Telegram (nejvýš jednou za %d s, do %d snímků denně). "
            "Postupné rozednívání poplach nespustí. Kamera zůstane namířená "
            "do místnosti i v noci. Vypnout: /hlidej stop."
            % (int(c.get("cooldown_s", 60)), int(c.get("max_per_day", 60))))


register(
    "hlidej",
    slash_aliases=["hlidej", "hlídej", "guard", "hlidani", "hlídání"],
    nl_patterns=[
        r"(hl[íi]dej|str[ěe][žz]|dohl[íi]žej)\s+(dům|dum|byt|m[íi]stnost|to\b)",
        r"zapni\s+(hl[íi]d[áa]n[íi]|str[áa][žz])",
        r"vypni\s+(hl[íi]d[áa]n[íi]|str[áa][žz])",
    ],
    handler=_cmd_hlidej,
    help_text="Hlídací režim: /hlidej [stop|stav] — při pohybu/změně světla "
              "pošlu snímek na Telegram",
)


# ─── /vypnipc — ruční vypnutí PC (HANS_PC_SHUTDOWN_CMD_V1) ───────────────────
# Protějšek /wol. Vypínání samo je hotové (HANS_PC_NIGHT_SHUTDOWN: S3 suspend
# je na téhle desce rozbitý → čistý poweroff přes SSH + ranní WOL); tady se
# jen dává na povel. Ověření pingem, ať Hans netvrdí „vypnuto“ naslepo.

def _pc_ping(config: dict, timeout: int = 2) -> bool:
    import subprocess
    ip = (str(config.get("wol_pc_ip", "") or "")
          or str((config.get("pc_remote", {}) or {}).get("host", "") or ""))
    if not ip:
        return False
    try:
        return subprocess.run(["ping", "-c", "1", "-W", str(timeout), ip],
                              capture_output=True,
                              timeout=timeout + 2).returncode == 0
    except Exception:
        return False


def _cmd_vypnipc(handler, name, args) -> str:
    cfg = getattr(handler, "config", {}) or {}
    try:
        from scripts import pc_remote
    except Exception:
        return "Na počítač teď nedosáhnu, pane."
    if not pc_remote.enabled(cfg):
        return ("Vzdálený přístup k počítači nemám povolený "
                "(pc_remote.enabled), pane — vypnout ho neumím.")
    if not _pc_ping(cfg):
        return "Počítač je už teď vypnutý (neodpovídá), pane. Nic nedělám."
    out = pc_remote.run(cfg, "sudo -n systemctl poweroff", timeout=10)
    if out is None:
        # poweroff často utne SSH spojení dřív, než stihne vrátit výstup —
        # proto None NEznamená selhání; rozhodne až ping.
        _log.info("/vypnipc: poweroff bez výstupu (SSH nejspíš utnuto)")
    import time as _t
    for _ in range(10):
        _t.sleep(3)
        if not _pc_ping(cfg):
            try:
                db = _recall_db(handler)
                import sqlite3 as _sql
                c = _sql.connect(db, timeout=5.0)
                c.execute("INSERT INTO diary (ts, event_type, title, note) "
                          "VALUES (?,?,?,?)",
                          (_t.time(), "pc_shutdown", "Vypnutí PC na povel",
                           "Na požádání jsem vypnul počítač."))
                c.commit()
                c.close()
            except Exception:
                pass
            return ("Počítač je vypnutý, pane. Můj mozek tím usnul — "
                    "ráno ho probudím, nebo si řekněte o /wol.")
    return ("Poslal jsem počítači povel k vypnutí, ale ještě odpovídá, pane. "
            "Možná se vypíná pomalu — nebo se něco vzpírá.")


register(
    "vypnipc",
    slash_aliases=["vypnipc", "vypnipc", "shutdown", "pcoff", "vypnout"],
    # HANS_UNIFY_ACTIONS_V1 — přirozená řeč („vypni pc") ZÁMĚRNĚ nemá regex:
    # vypnutí PC je destruktivní → padá k agentní akci pc_shutdown, která se
    # NAPŘED zeptá a vypíše, co na PC běží. Explicitní /vypnipc slash zůstává
    # okamžitý (výslovný povel = výslovný záměr). Ztráta při mozku dole = žádná
    # (PC dole ⇒ není co vypínat), slash funguje vždy.
    nl_patterns=[],
    handler=_cmd_vypnipc,
    help_text="Vypnu počítač (PC) — protějšek /wol",
)


# ─── /zdravi — zdraví závislostí (HANS_HEALTH_V1) ────────────────────────────
def _cmd_zdravi(handler, name, args) -> str:  # HANS_HEALTH_V1
    """Živá probe závislostí (Ollama/ComfyUI/Kodi/STT/PC/disk). /zdravi vylec =
    zkusí self-heal zaseklé Ollamy."""
    try:
        from scripts import hans_health
    except Exception as _e:
        return "Nemohu teď zkontrolovat své zdraví, pane. (%s)" % _e
    cfg = getattr(handler, "config", {}) or {}
    do_heal = bool(args) and args.strip().lower() in (
        "vylec", "vyléč", "heal", "restart", "oprav")
    res = hans_health.run_health_check(cfg, heal=do_heal)
    health = res.get("health", {})
    if not health:
        return "Kontrola zdraví je vypnutá, pane."
    _lbl = {"ollama": "Mozek (Ollama)", "comfyui": "Malování (ComfyUI)",
            "kodi": "Televize (Kodi)", "stt": "Sluch (přepis)",
            "pc": "Počítač", "disk": "Disk", "camera": "Kamera",
            "schedule": "Rozvrh (autonomní rutiny)"}
    _ico = {"ok": "✅", "paused": "⏸️", "wedged": "⚠️", "down": "❌",
            "unknown": "❔", "warn": "⚠️"}
    lines = ["Stav mých systémů, pane:"]
    for k, s in health.items():
        st = s.get("status", "unknown")
        lines.append("%s %s — %s" % (_ico.get(st, "❔"), _lbl.get(k, k),
                                     s.get("detail", st)))
    # HANS_SCHEDULE_V1 — zvlášť vypsat KTERÉ rutiny zaostávají (detail)
    sched_stale = ((health.get("schedule") or {}).get("stale")) or []
    if sched_stale:
        lines.append("")
        lines.append("Zaostávající rutiny:")
        for x in sched_stale:
            reason = " [%s]" % x["last_skip_reason"] if x["last_skip_reason"] else ""
            lines.append("  • %s — %.1fh po termínu (max %.1fh)%s"
                         % (x["name"], x["late_s"] / 3600,
                            x["expected_gap_s"] / 3600, reason))
    if res.get("healed"):
        lines.append("(Zaseklý mozek jsem zkusil restartovat.)")
    return "\n".join(lines)


register(
    "zdravi",
    slash_aliases=["zdravi", "zdraví", "health"],
    nl_patterns=[
        r"jak\s+(ti|se\s+ti|se\s+m[áa]š?).{0,15}(zdrav|syst[ée]m|slu[žz]b)",
        r"(zdrav|stav).{0,10}(syst[ée]m|slu[žz]eb|z[áa]vislost)",
        r"(funguje|jede|b[ěe][žz][íi]).{0,12}(ollama|comfyui|mozek|zrak)",
        r"jsi\s+v\s+po[řr][áa]dku",
    ],
    handler=_cmd_zdravi,
    help_text="Zdraví závislostí (Ollama/ComfyUI/Kodi/STT/PC/disk); /zdravi vylec = self-heal",
)


# ─── /nastroj — Hans si najde LLM nástroj pro dílo (HANS_TOOLSCOUT_V1) ────────
def _cmd_nastroj(handler, name, args) -> str:  # HANS_TOOLSCOUT_V1
    """/nastroj — stav návrhů; /nastroj <téma> = najdi nástroj pro doménu;
    /nastroj schválit N = schval + stáhni; /nastroj zamítnout N."""
    import threading as _th
    from scripts import hans_toolscout as ts
    cfg = getattr(handler, "config", {}) or {}
    db = _recall_db(handler)
    a = (args or "").strip()
    low = a.lower()

    def _fmt(props) -> str:
        if not props:
            return ""
        _F = {"coexist": "vejde se vedle chatu", "on_demand": "jen samostatně",
              "too_big": "nevejde se", "unknown": "?"}
        out = []
        for p in props:
            out.append("#%d [%s] %s %s (~%s GB, %s, %s stažení)\n   %s\n   %s" % (
                p["id"], p["status"], p["tool_name"], p["size_tag"], p["est_gb"],
                _F.get(p["fit"], p["fit"]), p["pulls"],
                (p.get("rationale") or "")[:180], p["url"]))
        return "\n".join(out)

    # schválit / zamítnout
    m = re.match(r"(schv[áa]l\w*|zam[íi]t\w*|odm[íi]t\w*)\s+(\d+)", low)
    if m:
        pid = int(m.group(2))
        store = ts.ToolStore(db)
        p = store.get(pid)
        if not p:
            return "Návrh č. %d neznám, pane." % pid
        if m.group(1).startswith(("zam", "odm")):
            store.set_status(pid, "rejected")
            return "Zamítnuto, pane. %s nebudu stahovat." % p["tool_name"]
        # schválit → pull na PC
        store.set_status(pid, "approved")
        res = ts.pull_model(cfg, p["tool_name"])
        if res.get("ok"):
            store.set_status(pid, "installed")
            return ("Schváleno, pane. Stahuji %s na počítač — %s. Až doběhne, "
                    "budu ho moci použít pro dílo." % (p["tool_name"], res["detail"]))
        return ("Schválil jsem %s, ale stažení jsem nespustil: %s"
                % (p["tool_name"], res.get("detail", "")))

    # stav / výpis
    if not a or low in ("stav", "status", "seznam"):
        store = ts.ToolStore(db)
        pend = store.list("pending")
        if pend:
            return "Mé návrhy nástrojů, pane:\n" + _fmt(pend) + \
                "\n\n(/nastroj schválit N nebo zamítnout N)"
        allp = store.list()
        if allp:
            return "Aktuálně nemám čekající návrh. Poslední:\n" + _fmt(allp[:3])
        return ("Zatím jsem žádný nástroj nenavrhl, pane. Napiš /nastroj <téma> "
                "a prozkoumám vhodné modely (např. /nastroj Design).")

    # /nastroj <téma> → scout na pozadí (síť + LLM)
    topic = a

    def _scout():
        try:
            r = ts.propose_tool(cfg, db, topic)
            _log.info("/nastroj %s → %s", topic, r.get("status"))
        except Exception as _e:
            _log.warning("/nastroj scout selhal: %s", _e)
    _th.Thread(target=_scout, daemon=True).start()
    return ("Prozkoumám vhodné nástroje pro „%s“, pane, chvíli to potrvá. "
            "Pak zadej /nastroj a ukážu, co jsem našel." % topic)


register(
    "nastroj",
    slash_aliases=["nastroj", "nástroj", "tool"],
    nl_patterns=[
        r"najdi\s+(mi\s+)?(vhodn|n[ěe]jak).{0,15}(model|n[áa]stroj|llm)",
        r"jak[ýy]\s+(model|n[áa]stroj|llm).{0,20}(pro|na)\s+",
    ],
    handler=_cmd_nastroj,
    help_text="Najdi LLM nástroj pro dílo: /nastroj <téma>; schválit/zamítnout N",
)


# ─── /brief — destilát studia do prompt/briefu pro dílo (HANS_BRIEF_V1) ───────
def _cmd_brief(handler, name, args) -> str:  # HANS_BRIEF_V1
    """/brief — poslední brief; /brief <téma> [coder|esej|obraz] = destiluj
    studium do nejlepšího promptu pro tvorbu díla (bez persony, grounded)."""
    import threading as _th
    from scripts import hans_brief as hb
    cfg = getattr(handler, "config", {}) or {}
    db = _recall_db(handler)
    a = (args or "").strip()

    # bez argumentu → poslední brief
    if not a:
        last = hb.BriefStore(db).latest()
        if not last:
            done = hb.completed_study_topics(db)
            hint = (" Dostudoval jsem: %s." % ", ".join(done)) if done else ""
            return ("Zatím jsem žádný brief nedestiloval, pane. Napiš "
                    "/brief <téma> a připravím prompt z nastudovaného.%s" % hint)
        return ("Poslední brief — %s (%s), z poznámek: %s\n\n%s" % (
            last["topic"], last["target"],
            (last.get("source_notes") or "")[:120], last["brief"]))

    # /brief <téma> [cíl]
    parts = a.rsplit(None, 1)
    target = "coder"
    topic = a
    if len(parts) == 2 and parts[1].lower() in (
            "coder", "kod", "kód", "esej", "essay", "obraz", "image"):
        topic = parts[0]
        t = parts[1].lower()
        target = ("essay" if t in ("esej", "essay")
                  else "image" if t in ("obraz", "image") else "coder")

    def _build():
        try:
            r = hb.build_brief(cfg, db, topic, target)
            _log.info("/brief %s (%s) → %s", topic, target, r.get("status"))
        except Exception as _e:
            _log.warning("/brief selhal: %s", _e)
    _th.Thread(target=_build, daemon=True).start()
    return ("Destiluji, co jsem se naučil o „%s“, do promptu pro dílo (%s), "
            "pane — chvíli to potrvá. Pak zadej /brief a ukážu ho." % (
                topic, target))


register(
    "brief",
    slash_aliases=["brief", "zadani", "zadání"],
    nl_patterns=[
        r"(p[řr]iprav|udělej).{0,15}(brief|zad[áa]n[íi]|prompt)",
    ],
    handler=_cmd_brief,
    help_text="Destiluj studium do promptu pro dílo: /brief <téma> [coder|esej|obraz]",
)


# ─── /vytvor — celá smyčka studium → brief → nástroj → artefakt (HANS_MAKER_V1) ─
def _cmd_vytvor(handler, name, args) -> str:  # HANS_MAKER_V1
    """/vytvor <téma> [coder|obraz] — Hans z nastudovaného vyrobí reálný
    artefakt (coder→HTML/CSS, obraz→SDXL). Běží na pozadí (pomalé)."""
    import threading as _th
    from scripts import hans_maker as hm
    cfg = getattr(handler, "config", {}) or {}
    db = _recall_db(handler)
    a = (args or "").strip()
    if not a:
        arts = hm.latest_artifacts(db, 3)
        if arts:
            out = ["Má poslední díla, pane:"]
            for x in arts:
                out.append("• %s (%s) → %s" % (x.get("topic", x.get("title")),
                           x.get("target", "?"), x.get("path", "")))
            return "\n".join(out)
        return ("Zatím jsem žádné dílo nevytvořil, pane. Napiš /vytvor <téma> "
                "a z nastudovaného vyrobím artefakt (např. /vytvor Design).")
    parts = a.rsplit(None, 1)
    target, topic = "coder", a
    if len(parts) == 2 and parts[1].lower() in ("coder", "obraz", "image"):
        topic = parts[0]
        target = "image" if parts[1].lower() in ("obraz", "image") else "coder"

    def _make():
        try:
            r = hm.make_from_study(cfg, db, topic, target)
            _log.info("/vytvor %s (%s) → %s (%s)", topic, target,
                      r.get("status"), r.get("path", r.get("reason", "")))
        except Exception as _e:
            _log.warning("/vytvor selhal: %s", _e)
    _th.Thread(target=_make, daemon=True).start()
    return ("Pouštím se do díla k „%s“ (%s) z toho, co jsem nastudoval, pane. "
            "Vyrobí to nástroj podle mého briefu — chvíli to potrvá (i pár "
            "minut). Pak zadej /vytvor a ukážu, co vzniklo." % (topic, target))


register(
    "vytvor",
    slash_aliases=["vytvor", "vytvoř", "vyrob", "artefakt"],
    nl_patterns=[
        r"vytvo[řr].{0,20}(z\s+toho|co\s+ses|nastudova|dílo|artefakt)",
    ],
    handler=_cmd_vytvor,
    help_text="Vyrob artefakt z nastudovaného: /vytvor <téma> [coder|obraz]",
)


# ─── /prohloubit — schválení/kritika návrhu prohloubení studia (DEEPEN_V2) ────
def _cmd_prohloubit(handler, name, args) -> str:  # HANS_STUDY_DEEPEN_V2
    """/prohloubit — Hansovy návrhy prohloubení; /prohloubit schválit = přijmi;
    /prohloubit <vlastní kritika> = prohluť podle tebe; /prohloubit ne = zamítni."""
    import threading as _th
    from scripts.hans_study import StudyStore
    cfg = getattr(handler, "config", {}) or {}
    db = _recall_db(handler)
    st = StudyStore(cfg, db)
    a = (args or "").strip()
    low = a.lower()

    pend = st.get_pending_deepen()
    # bez argumentu → výpis návrhů
    if not a:
        if not pend:
            return ("Teď nemám žádný návrh na prohloubení, pane. Vytvořím ho, "
                    "až dokončím dílo z nastudovaného.")
        out = ["Mé návrhy na prohloubení studia, pane:"]
        for p in pend:
            out.append("• %s (kolo %d)\n  Kritika: %s\n  Doučil bych se: %s" % (
                p["topic"], p["round"], p["critique"] or "—",
                "; ".join(p["subtopics"])))
        out.append("\n/prohloubit schválit  ·  /prohloubit <vlastní kritika>  ·  "
                   "/prohloubit ne")
        return "\n".join(out)

    if not pend:
        return "Nemám čekající návrh, pane."

    # zamítnout
    if low in ("ne", "zamítnout", "zamitnout", "odmítnout", "odmitnout", "ignoruj"):
        st.reject_deepen_proposal(pend[0]["id"])
        return "Dobře, pane. Prohloubení „%s“ nechám být." % pend[0]["topic"]

    # schválit (dle Hansova návrhu) NEBO vlastní kritika
    is_approve = low in ("schválit", "schvalit", "ano", "ok", "jo", "souhlasím",
                         "souhlasim", "schvaluji")
    user_crit = "" if is_approve else a

    def _apply():
        try:
            r = st.apply_deepen_proposal(cfg, pend[0]["id"], user_critique=user_crit)
            _log.info("/prohloubit → %s (+%s)", r.get("status"),
                      len(r.get("added", [])))
        except Exception as _e:
            _log.warning("/prohloubit selhalo: %s", _e)
    _th.Thread(target=_apply, daemon=True).start()
    if user_crit:
        return ("Beru tvou kritiku, pane, a podle ní prohloubím studium „%s“. "
                "Chvíli to potrvá; pak uvidíš nová pod-témata v /studium." %
                pend[0]["topic"])
    return ("Schváleno, pane. Prohloubím studium „%s“ podle svého návrhu — "
            "nová pod-témata uvidíš v /studium a příště z nich vytvořím lepší "
            "dílo." % pend[0]["topic"])


register(
    "prohloubit",
    slash_aliases=["prohloubit", "prohloub", "deepen"],
    nl_patterns=[],
    handler=_cmd_prohloubit,
    help_text="Návrh prohloubení studia: /prohloubit [schválit|ne|<vlastní kritika>]",
)


# ─── /vhledy — Hansovy sebe-vhledy (HANS_SELF_INSIGHT_V1) ────────────────────
def _cmd_vhledy(handler, name, args) -> str:  # HANS_SELF_INSIGHT_V1
    """/vhledy — co si Hans všiml ve vlastních datech (offline_windows,
    game_mode). Podklady = nightly LLM analýza (deepseek-r1 → hans-czech).
    /vhledy teď = spusť run hned (bez ohledu na kadenci)."""
    try:
        from scripts.hans_self_insight import latest_insights, run_analysis
    except Exception as _e:
        return "Sebe-vhledy nejsou dostupné, pane. (%s)" % _e
    cfg = getattr(handler, "config", {}) or {}
    dbp = (cfg.get("diary_db")
           or (cfg.get("hans_idle", {}) or {}).get("diary_db")
           or "data/hans_diary.db")
    args_s = (args or "").strip().lower()

    if args_s in ("ted", "teď", "run", "nyní", "nyni"):
        # Vyžádaný okamžitý run (mimo weekly kadenci). Blokuje ~1-2 min.
        import threading as _th
        def _bg():
            try:
                run_analysis(dbp, cfg, force=True)
            except Exception:
                pass
        _th.Thread(target=_bg, daemon=True).start()
        return ("Spouštím rozbor svých vlastních dat na pozadí, pane. "
                "Za pár minut zkuste /vhledy znovu — objeví se v seznamu.")

    ins = latest_insights(dbp, limit=int(args_s) if args_s.isdigit() else 3)
    if not ins:
        return ("Zatím jsem si o svých vlastních vzorcích nic nezapsal, pane. "
                "Zkuste /vhledy teď — spustím rozbor.")
    import datetime as _dt
    lines = ["Co jsem si v poslední době všiml ve vlastních datech:"]
    for i, r in enumerate(ins, 1):
        when = _dt.datetime.fromtimestamp(r["ts"]).strftime("%d.%m. %H:%M")
        lines.append("")
        lines.append("── %s (%dd okno) ──" % (when, r["window_days"]))
        lines.append(r["insight_cs"])
    return "\n".join(lines)


register(
    "vhledy",
    slash_aliases=["vhledy", "insights"],
    nl_patterns=[
        r"\bco\s+sis?\s+vši?ml?\s+(u\s+sebe|na\s+sob[ěe])",
        r"\btv[ée]\s+vhledy?\b",
        r"\bm[áa]š?\s+n[ěe]jak[éy]\s+vhled",
    ],
    handler=_cmd_vhledy,
    help_text="Co si Hans všiml ve vlastních datech (offline/herní mód); /vhledy teď = spusť rozbor hned",
)


# ─── /experiment — footgun s auto-resume (HANS_FOOTGUN_V1) ───────────────────
def _cmd_experiment(handler, name, args) -> str:
    """/experiment [minut] — spusť experiment: Hans si zapne herní mód na
    N minut (default 5), pak auto-resume. Neutrální deník záznam. Config
    gate `hans_experiment.enabled`."""
    try:
        from scripts.hans_footgun import experiment_run, status, is_running
    except Exception as _e:
        return "Experiment modul nedostupný, pane. (%s)" % _e
    cfg = getattr(handler, "config", {}) or {}
    args_s = (args or "").strip().lower()
    if args_s in ("stav", "status"):
        s = status()
        if s.get("active"):
            return ("Experiment běží: %d minut, zbývá %d minut."
                    % (s["duration_s"] // 60, s.get("remaining_s", 0) // 60))
        return "Aktuálně žádný experiment neběží."
    if is_running():
        s = status()
        return ("Experiment už běží — zbývá %d minut. Počkej na auto-resume."
                % (s.get("remaining_s", 0) // 60))
    # default 5 min
    dur_min = 5
    try:
        if args_s and args_s.isdigit():
            dur_min = int(args_s)
    except Exception:
        pass
    r = experiment_run(cfg, duration_s=dur_min * 60)
    if not r.get("ok"):
        return "Experiment nespuštěn: %s" % r.get("message", "?")
    return ("Spouštím experiment: zapínám si herní mód na %d minut. "
            "Auto-resume je zajištěn — i kdybych se během toho nemohl "
            "vyjádřit, systém mě vrátí." % dur_min)


register(
    "experiment",
    slash_aliases=["experiment", "footgun"],
    nl_patterns=[],
    handler=_cmd_experiment,
    help_text="Experiment: zapnu si herní mód na N minut (default 5), pak auto-resume",
)
