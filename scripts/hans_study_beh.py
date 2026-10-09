"""Funkce přesunuté z `scripts/hans_study.py` (ROZDELENI_PRIKAZU_V1).

Text funkcí je beze změny; jména původního modulu se čtou přes `_hs.` až při
volání. Původní modul si funkce bere zpět importem, takže dosavadní importy platí.
Import původního modulu je na KONCI souboru (kruhový import oběma směry).
"""
from __future__ import annotations

import sqlite3
import time

# ── Surfacing / introspekce (HANS_STUDY_SURFACING_V1, #2; Severka #3) ────────
def _latest_diary_text(diary_db_path: str, event_type: str,
                       title_like: str = None) -> tuple:
    """(title, data, ts) posledního deníkového eventu daného typu nebo (None…)."""
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % diary_db_path,
                               uri=True, timeout=4.0)
        if title_like:
            row = conn.execute(
                "SELECT title, data, ts FROM diary WHERE event_type=? "
                "AND title LIKE ? ORDER BY ts DESC LIMIT 1",
                (event_type, title_like)).fetchone()
        else:
            row = conn.execute(
                "SELECT title, data, ts FROM diary WHERE event_type=? "
                "ORDER BY ts DESC LIMIT 1", (event_type,)).fetchone()
        conn.close()
        return (row[0], row[1], row[2]) if row else (None, None, None)
    except Exception as e:
        _hs._log.debug("_latest_diary_text failed: %s", e)
        return (None, None, None)


def study_context_string(config: dict, diary_db_path: str,
                         max_chars: int = 360) -> str:
    """Krátký kontext o Hansově studiu pro chat prompt (#2 proaktivní zmínka).
    Read-only. Aktivní program → téma + poslední poznámka; jinak nedávno
    dokončené studium → mistrovská reflexe. '' když nic."""
    try:
        store = _hs.StudyStore(config, diary_db_path)
        ap = store.get_active_program()
    except Exception:
        return ""
    if ap:
        topic = ap["topic"]
        _t, data, _ts = _hs._latest_diary_text(
            diary_db_path, "study_note", f"Studium: {topic} —%")
        out = (f"Posledních pár dní studuji do hloubky téma \u201e{topic}\u201c "
               f"(pod-téma {min(ap['current_index'] + 1, len(ap['curriculum']))}"
               f"/{len(ap['curriculum'])}).")
        if data:
            out += " Naposledy mě zaujalo: " + data.strip().replace("\n", " ")
        # HANS_STUDY_TODAY_SHARED_V1 — ořež NEJDŘÍV a teprve pak připoj dnešek,
        # jinak by ho strop `max_chars` uřízl — a je to ta část, kvůli které se
        # to dělá: bez ní si model z „posledních pár dní studuji" vyrobil
        # „dnes jsem studoval", ačkoli dnes studium neproběhlo (18.8.).
        out = out[:max_chars]
        _tl = _hs.today_line(diary_db_path)
        return (out + " " + _tl).strip() if _tl else out
    # žádný aktivní → nedávno dokončené?
    title, data, ts = _hs._latest_diary_text(diary_db_path, "study_mastery")
    if title and data and ts and (time.time() - ts) < 14 * 86400:
        topic = title.replace("Mistrovská reflexe:", "").strip()
        return (f"Nedávno jsem dostudoval téma \u201e{topic}\u201c. "
                + data.strip().replace("\n", " "))[:max_chars]
    return ""


def study_dialog_seed(config: dict, diary_db_path: str,
                      max_chars: int = 260) -> str:
    """Seed pro dialog s Kolačem (HANS_STUDY_KOLAC_V1). Marker 'Studuji do
    hloubky:' rozpozná klasifikátor témat v hans_dialog. '' když nestuduje."""
    try:
        ap = _hs.StudyStore(config, diary_db_path).get_active_program()
    except Exception:
        return ""
    if not ap:
        return ""
    topic = ap["topic"]
    _t, data, _ts = _hs._latest_diary_text(
        diary_db_path, "study_note", f"Studium: {topic} —%")
    seed = f"Studuji do hloubky: {topic}."
    if data:
        seed += " " + data.strip().replace("\n", " ")
    return seed[:max_chars]


def completed_studies_block(config: dict, diary_db_path: str,
                            limit: int = 4, max_chars: int = 900) -> str:
    """Blok pro Severku (#3): dokončené studijní programy + aktivní směr.
    Grounduje vocational návrh identity REÁLNOU znalostí. '' když nic."""
    try:
        store = _hs.StudyStore(config, diary_db_path)
        progs = store.all_programs(limit=20)
    except Exception:
        return ""
    if not progs:
        return ""
    lines = []
    done = [p for p in progs if p["status"] == "completed"]
    for p in done[:limit]:
        _t, data, _ts = _hs._latest_diary_text(
            diary_db_path, "study_mastery", f"%{p['topic']}%")
        gist = (data or "").strip().replace("\n", " ")
        lines.append(f"- Dostudoval jsem do hloubky \u201e{p['topic']}\u201c. "
                     + (gist[:200] if gist else ""))
    active = next((p for p in progs if p["status"] == "active"), None)
    if active:
        lines.append(f"- Právě studuji do hloubky \u201e{active['topic']}\u201c "
                     f"({active['current_index']}/{len(active['curriculum'])}).")
    if not lines:
        return ""
    return "\n".join(lines)[:max_chars]


# ── Top-level noční vstup (volá hans_routine) ───────────────────────────────
def run_study_session(config: dict, diary_db_path: str, knowledge=None,
                      diary_writer=None) -> str:
    """Jedna studijní session. Vrací JEDEN ze ŠESTI kódů (HANS_STUDY_UNIFY_V1 —
    dřív jich docstring jmenoval jen 4, `skipped` a `noread` chyběly):
       'studied'   — nastudováno pod-téma
       'completed' — kurikulum dokončeno (mistrovská reflexe)
       'skipped'   — pod-téma po `max_subtopic_failures` přeskočeno (program se
                     POSUNUL, ale nic se nenaučil)
       'noread'    — k pod-tématu se nenašlo čtení; fail_count++, program STOJÍ
       'idle'      — nic ke studiu (žádný durable koníček / vše prostudováno)
       'deferred'  — transientní selhání (Ollama/wiki dole) → zkusit znovu
    Volající NEMÁ kódy porovnávat řetězcem — na to jsou `is_transient()`
    (nastavit denní guard?) a `made_progress()` (posunulo se to?)."""
    if not _hs._cfg(config).get("enabled", True):
        return "idle"
    # HANS_STUDY_VRAM_HANDOFF_V1 — studium běží na base OpenEuroLLM (8GB), ale
    # hans-czech (8GB) je rezidentní a 8+8 > 16GB VRAM. Kroky:
    #  1) pause_warmup → oba keepalive (ping_model + ollama_warmup) přestanou
    #     re-pinovat hans-czech po dobu dávky.
    #  2) ollama_unload_all → AKTIVNĚ uvolni hans-czech HNED. Samotná pauza
    #     nestačí: hans-czech je nahraný s keep_alive=-1, který sám nevyprší,
    #     a Ollama ho neevictuje ani pro nový request → base model se nevejde
    #     → 300s timeout → deferred (přesně tenhle býval symptom). V noci to
    #     „projde" jen náhodou (hans-czech vyprší při klidu), ve dne/ránu ne.
    # Po session resume_warmup re-povolí keepalive → hans-czech se dotáhne.
    # Auto-expiry pauzy 20 min = cap, kdyby impl spadl bez resume.
    # HANS_BASE_SLOT_V1 (8.9.) — inline pause+unload nahrazen SDÍLENÝM
    # `base_model_batch`, protože samotný handoff nestačil: studium a další
    # base-model dávky se navzájem nevidí. Doloženo 8.9. — studium jelo cestou
    # „brain_up catchup", immune cestou `_night_tick`; `_creative_busy` je
    # lokální proměnná jednoho ticku, takže mezi těmito dvěma cestami
    # NEEXISTOVALO vzájemné vyloučení. Context manager dělá totéž co dosavadní
    # inline kód (pause_warmup + aktivní unload, resume ve finally) a navíc
    # zabere slot, takže druhá dávka počká místo souběhu.
    try:
        from scripts.ollama_client import base_model_batch as _bmb
    except Exception:
        _bmb = None
    if _bmb is None:
        return _hs._run_study_session_impl(config, diary_db_path, knowledge,
                                       diary_writer)
    with _bmb(config, pause_s=1200, label="studium"):
        return _hs._run_study_session_impl(config, diary_db_path, knowledge,
                                       diary_writer)


def _run_study_session_impl(config: dict, diary_db_path: str, knowledge=None,
                            diary_writer=None) -> str:
    try:
        store = _hs.StudyStore(config, diary_db_path)
    except Exception as e:
        _hs._log.warning("run_study_session init selhal: %s", e)
        return "deferred"
    prog = store.ensure_program(config)
    if not prog:
        # rozliš: durable koníček ale LLM dole (deferred) vs opravdu nic (idle).
        # ensure_program loguje důvod; konzervativně 'idle' jen když není žádný
        # durable koníček, jinak 'deferred'. Levné rozlišení:
        c = _hs._cfg(config)
        try:
            from scripts.hans_hobbies import HobbyStore
            hobs = HobbyStore(config, diary_db_path).durable_hobbies(
                min_evidence=int(c.get("min_evidence", 8)),
                min_age_days=int(c.get("min_age_days", 21)),
                min_recent_days=int(c.get("min_recent_days", 14)))
        except Exception:
            hobs = []
        # je-li durable koníček a přesto není program → kurikulum selhalo → retry
        unstudied = [h for h in hobs
                     if _hs._norm(h.name) not in store._studied_topic_norms()]
        return "deferred" if unstudied else "idle"
    res = store.study_next(config, knowledge=knowledge, diary_writer=diary_writer)
    # HANS_SCHEDULE_V1 — razítko včetně důvodu skip (deferred/idle).
    # ok=True jen když session opravdu proběhla (studied/completed); jinak si
    # audit může všimnout, PROČ study visí (nejčastěji brain_down = deferred).
    try:
        from scripts import hans_schedule
        if res is None:
            hans_schedule.mark('study_tick', ok=False, skip_reason='deferred')
        else:
            rr = res.get("result", _hs.RESULT_STUDIED)
            if _hs.produced_knowledge(rr):   # přísnější než made_progress — viz _KNOWLEDGE
                hans_schedule.mark('study_tick', ok=True)
            else:
                hans_schedule.mark('study_tick', ok=False, skip_reason=rr)
    except Exception:
        pass
    if res is None:
        return _hs.RESULT_DEFERRED
    return res.get("result", _hs.RESULT_STUDIED)


def today_line(diary_db_path: str = "data/hans_diary.db") -> str:
    """HANS_STUDY_TODAY_SHARED_V1 (19.8.) — JEDNA věta o tom, jak dopadl DNEŠEK.

    Jedna pravda pro dvě místa: výpis `/studium` i kontext volného hovoru.
    Původně to bylo jen ve výpisu (HANS_STUDY_TODAY_LINE_V1) a hovor o dnešku
    nevěděl nic — doloženo 18.8., kdy Hans v jednom chatu řekl „Dnes jsem
    studoval do hloubky Český ráj" a o tři výměny později „Dnes se mi nic
    nastudovat nepodařilo". Druhá kopie logiky by se rozešla stejně.

    Zdroj je `hans_schedule.study_tick`, kde se od HANS_SCHEDULE_LAST_OK_V1
    rozlišuje „kdy to naposledy ZKUSILO" od „kdy naposledy USPĚLO".
    '' když se stav nedá zjistit (volající pak nic nepřidává).
    """
    try:
        import datetime as _dt
        from scripts.hans_schedule import ScheduleStore
        row = ScheduleStore(diary_db_path).get("study_tick") or {}
        today = _dt.date.today()

        def _is_today(ts):
            try:
                return bool(ts) and _dt.date.fromtimestamp(float(ts)) == today
            except Exception:
                return False

        if _is_today(row.get("last_ok_ts")):
            # HANS_STUDY_TODAY_TOPIC_V1 (19.8.) — říct i CO. Bez tématu si model
            # ve volném hovoru vzal starší téma z deníku: doloženo 19.8., kdy
            # Hans tvrdil „studoval jsem Český ráj", ačkoli dnes studoval
            # Cimrmana (Český ráj dokončil předchozí večer).
            try:
                import sqlite3 as _sq
                with _sq.connect("file:%s?mode=ro" % diary_db_path,
                                 uri=True, timeout=3.0) as _db:
                    _r = _db.execute(
                        "SELECT title FROM diary WHERE event_type='study_note' "
                        "AND ts >= ? ORDER BY ts DESC LIMIT 1",
                        (_dt.datetime.combine(today, _dt.time.min).timestamp(),)
                    ).fetchone()
                _t = (_r[0] if _r else "") or ""
                # „Studium: <téma> — <pod-téma>"
                _t = _t.replace("Studium:", "").strip()
                if " — " in _t:
                    _tema, _sub = [x.strip() for x in _t.split(" — ", 1)]
                    return ("Dnes se mi povedlo nastudovat pod-téma „%s“ "
                            "z tématu „%s“." % (_sub, _tema))
                if _t:
                    return "Dnes se mi povedlo nastudovat „%s“." % _t
            except Exception as _e2:
                _hs._log.debug("today_line: téma nešlo zjistit (%s)", _e2)
            return "Dnes se mi povedlo nastudovat pod-téma."
        if _is_today(row.get("last_run_ts")):
            why = {"deferred": "encyklopedie nebo mozek neodpovídaly",
                   "noread": "k pod-tématu jsem nenašel zdroj",
                   "idle": "neměl jsem co studovat",
                   "skipped": "pod-téma jsem přeskočil"}
            r = (row.get("last_skip_reason") or "").strip()
            kdy = _dt.datetime.fromtimestamp(
                float(row["last_run_ts"])).strftime("%H:%M")
            txt = why.get(r, r)   # neznámý kód syrový, ne domyšlený
            return ("Dnes se mi nic nastudovat nepodařilo (poslední pokus %s%s)."
                    % (kdy, (", důvod: " + txt) if txt else ""))
        return "Dnes jsem se ke studiu ještě nedostal."
    except Exception as e:
        _hs._log.debug("today_line: %s", e)
        return ""

# ROZDELENI_PRIKAZU_V1 — až na konci, viz hlavička
from scripts import hans_study as _hs  # noqa: E402
