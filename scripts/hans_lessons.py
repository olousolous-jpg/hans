"""scripts/hans_lessons.py

HANS_CORRECTION_LEARNING_V1 (#4, KONZERVATIVNÍ) — Hans se učí z korekcí.

Když ho uživatel v rozhovoru OPRAVÍ / vyvrátí mu tvrzení / dá najevo, že se spletl,
Hans si z toho v noci vezme PONAUČENÍ (lekci) a:
  - uloží ho do deníku jako 'lesson_learned' (LOG + noční reflexe „kde jsem se mýlil"),
  - příště je má v kontextu chatu (aby chybu neopakoval),
  - ráno z toho pocítí jemnou pokoru (mírná nálada).

KONZERVATIVNÍ rozsah: NEMĚNÍ automaticky paměť, fakta ani postoje. Jen se z toho
poučí (vědomě) — případnou korekci paměti/postoje řeší člověk.

Anti-konfabulace: jen REÁLNÉ opravy z přepisu, ne pouhý jiný názor/vkus.

API:
  extract_corrections(config, diary_db_path, window_hours=26) -> int   # noční
  recent_lessons(diary_db_path, hours=48, limit=5) -> list[str]         # chat ctx
  scan_overnight_lessons(diary_db_path, since) -> int                   # mood nudge
"""
from __future__ import annotations
import json
import logging
import sqlite3
import time

_log = logging.getLogger("hans_lessons")

_SYSTEM = (
    "Jsi pozorný analytik. Dostaneš PŘEPIS dnešních rozhovorů jedné osoby s postavou "
    "jménem {persona_name} (řádky „osoba:…\" a „{persona_name}:…\"). Najdi momenty, "
    "kdy osoba {persona_name} OPRAVILA — vyvrátila mu tvrzení, upozornila, že se spletl, "
    "že něco řekl nepřesně nebo si vymyslel. Pro každý takový moment vrať objekt s klíči:\n"
    "  claim     = co {persona_name} řekl špatně (jeho chybné tvrzení),\n"
    "  correction= jak ho osoba opravila / jak to ve skutečnosti je,\n"
    "  lesson    = krátké ponaučení v 1. osobě, co si z toho {persona_name} bere "
    "(např. „Nemám si domýšlet preference lidí, když je neznám.\").\n"
    "PŘÍSNĚ ANTI-KONFABULACE: zahrň JEN reálné opravy faktu/omylu DOSLOVNĚ obsažené "
    "v přepisu. NEZAHRNUJ pouhý jiný názor či vkus, běžnou otázku, ani nesouhlas "
    "v preferenci (to není oprava). Když žádná oprava není, vrať prázdné pole. "
    "Vrať VÝHRADNĚ JSON pole objektů, nic víc."
)


def _extract_json_array(raw: str):
    """Robustní extrakce JSON pole (toleruje ```json fences)."""
    if not raw:
        return []
    s = raw.strip()
    if s.startswith("```"):
        s = s.strip("`")
        if s[:4].lower() == "json":
            s = s[4:]
    a = s.find("[")
    b = s.rfind("]")
    if a == -1 or b == -1 or b < a:
        return []
    try:
        out = json.loads(s[a:b + 1])
        return out if isinstance(out, list) else []
    except Exception:
        return []


def extract_corrections(config: dict, diary_db_path: str,
                        window_hours: float = 26.0) -> int:
    """Noční krok: z denních dialogů vytáhne momenty, kdy byl Hans opraven, a uloží
    'lesson_learned' do deníku. NEMĚNÍ paměť/postoje. Vrací počet lekcí. LLM offline
    / žádné dialogy → 0 (tichý skip)."""
    cfg = (config.get("corrections", {}) or {})
    if not cfg.get("enabled", True):
        return 0
    window_hours = float(cfg.get("window_hours", window_hours))
    since = time.time() - window_hours * 3600.0
    try:
        from scripts.hans_threads import _gather_dialogs
    except Exception as e:
        _log.warning("extract_corrections: _gather_dialogs nedostupný: %s", e)
        return 0
    dialogs = _gather_dialogs(diary_db_path, since)
    if not dialogs:
        _log.info("extract_corrections: žádné dialogy v okně, skip")
        return 0
    er = config.get("evening_reflection", {}) or {}
    model = str(cfg.get("model", er.get("model",
                "jobautomation/OpenEuroLLM-Czech:latest")))
    timeout = int(cfg.get("llm_timeout", 300))
    max_per_night = int(cfg.get("max_per_night", 3))
    try:
        from scripts.ollama_client import ollama_generate
    except Exception as e:
        _log.warning("extract_corrections: ollama_client nedostupný: %s", e)
        return 0
    try:
        from scripts.hans_persona import persona_name as _pn
        system = _SYSTEM.format(persona_name=_pn(config))
    except Exception:
        system = _SYSTEM.format(persona_name="Hans")

    written = 0
    for person, notes in dialogs.items():
        if written >= max_per_night:
            break
        transcript = "\n\n".join(notes)[:4000]
        if not transcript.strip():
            continue
        try:
            raw = ollama_generate(model=model, prompt=transcript, system=system,
                                  config=config, timeout=timeout, keep_alive=0,
                                  options={"temperature": 0.1})
        except Exception as e:
            _log.warning("extract_corrections LLM (%s): %s", person, e)
            continue
        items = _extract_json_array(raw)
        for it in items:
            if written >= max_per_night:
                break
            if not isinstance(it, dict):
                continue
            lesson = str(it.get("lesson", "") or "").strip()
            if len(lesson) < 8:
                continue
            claim = str(it.get("claim", "") or "").strip()
            correction = str(it.get("correction", "") or "").strip()
            try:
                db = sqlite3.connect(diary_db_path, timeout=5.0)
                db.execute(
                    "INSERT INTO diary (ts, event_type, title, note, data) "
                    "VALUES (?,?,?,?,?)",
                    (time.time(), "lesson_learned", person, lesson,
                     json.dumps({"claim": claim, "correction": correction},
                                ensure_ascii=False)))
                db.commit()
                db.close()
                written += 1
                _log.info("lesson_learned [%s]: %.70s", person, lesson)
            except Exception as e:
                _log.warning("extract_corrections diary write failed: %s", e)
    _log.info("extract_corrections: uloženo %d lekcí", written)
    return written


def recent_lessons(diary_db_path: str, hours: float = 48.0,
                   limit: int = 5) -> list:
    """READ-ONLY: posledních `limit` lekcí (note) za okno. '' / chyba → []."""
    since = time.time() - hours * 3600.0
    conn = None
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % diary_db_path, uri=True,
                               timeout=3.0)
        rows = conn.execute(
            "SELECT note FROM diary WHERE event_type='lesson_learned' "
            "AND ts > ? ORDER BY ts DESC LIMIT ?", (since, int(limit))).fetchall()
        return [r[0].strip() for r in rows if r and r[0] and r[0].strip()]
    except Exception as e:
        _log.debug("recent_lessons failed: %s", e)
        return []
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def scan_overnight_lessons(diary_db_path: str, since: float) -> int:
    """READ-ONLY: počet lekcí uložených od `since` (pro ranní mood nudge)."""
    conn = None
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % diary_db_path, uri=True,
                               timeout=3.0)
        n = conn.execute(
            "SELECT COUNT(*) FROM diary WHERE event_type='lesson_learned' "
            "AND ts > ?", (since,)).fetchone()[0]
        return int(n or 0)
    except Exception as e:
        _log.debug("scan_overnight_lessons failed: %s", e)
        return 0
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


if __name__ == "__main__":
    import tempfile, os
    print("=== hans_lessons smoke (temp DB) ===")
    fd, p = tempfile.mkstemp(suffix=".db"); os.close(fd)
    db = sqlite3.connect(p)
    db.execute("CREATE TABLE diary (id INTEGER PRIMARY KEY, ts REAL, event_type TEXT, "
               "title TEXT, note TEXT, data TEXT)")
    db.execute("INSERT INTO diary(ts,event_type,title,note) VALUES (?,?,?,?)",
               (time.time(), "lesson_learned", "standa",
                "Nemám si domýšlet, co lidé mají rádi, když to nevím."))
    db.commit(); db.close()
    print("recent_lessons:", recent_lessons(p))
    print("scan_overnight_lessons:", scan_overnight_lessons(p, time.time() - 3600))
    os.unlink(p)
