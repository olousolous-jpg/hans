"""scripts/hans_selfcritique.py

HANS_SELFCRITIQUE_V1 (#6) — Hans se kriticky ohlíží za VLASTNÍMI nedávnými projevy
a z VLASTNÍHO POPUDU si bere ponaučení, jak se příště vyjádřit lépe.

Rozšíření učení z korekcí ([[hans_lessons]] = člověk opraví Hanse) o sebereflexi:
tady NIKDO Hanse neopravuje — Hans sám u sebe najde slabé místo (rozvláčnost,
opakování, vyhýbavost, prázdné fráze, neodpovězení na otázku) a uloží ponaučení.

NE faktická chyba (tu řeší hans_lessons.extract_corrections) — tady jde o KVALITU
projevu. KONZERVATIVNÍ: nemění paměť/postoje, jen se poučí (vědomě) → příště to má
v kontextu chatu (vedle korekčních lekcí) a snaží se zlepšit.

Zdroj = Hansovy vlastní repliky z dialogů (human_chat = s lidmi, teddy_dialog =
s Koláčem). Base model (analytik, anti-konfabulace), keep_alive=0, jen v noci.

API:
  run_self_critique(config, diary_db_path, window_hours=72) -> int     # noční
  recent_selfcritiques(diary_db_path, hours=72, limit=4) -> list[str]  # chat ctx
"""
from __future__ import annotations
import json
import logging
import sqlite3
import time

_log = logging.getLogger("hans_selfcritique")

_SYSTEM = (
    "Jsi {persona_name} a kriticky se ohlížíš za SVÝMI VLASTNÍMI nedávnými replikami "
    "(řádky „{persona_name}:…\"). Tvým úkolem je u SEBE najít JEDEN konkrétní moment, "
    "kde ses mohl vyjádřit lépe — kde jsi byl zbytečně rozvláčný, vyhýbavý, opakoval "
    "ses (tytéž obraty/myšlenky), plácal obecné fráze bez obsahu, nebo jsi vlastně "
    "neodpověděl na to, co padlo. Jde o KVALITU tvého projevu, NE o faktickou chybu.\n"
    "Vrať VÝHRADNĚ JSON objekt s klíči:\n"
    "  weak_quote = tvá slabá pasáž (krátký doslovný úryvek tvé repliky),\n"
    "  issue      = čím je slabá (1 věta),\n"
    "  lesson     = ponaučení v 1. osobě, jak se příště vyjádřit lépe "
    "(např. „Příště odpovím stručně a rovnou k věci, bez omluvných úvodů.\").\n"
    "PŘÍSNĚ ANTI-KONFABULACE: vyjdi JEN z uvedených replik, necituj nic, co tam není. "
    "Buď k sobě upřímný, ale konstruktivní. Vyber JEN to opravdu nejslabší místo. "
    "Když jsou tvé repliky v pořádku, vrať prázdný objekt {{}}."
)


def _extract_json_obj(raw: str):
    """Robustní extrakce JSON objektu (toleruje ```json fences)."""
    if not raw:
        return None
    s = raw.strip()
    if s.startswith("```"):
        s = s.strip("`")
        if s[:4].lower() == "json":
            s = s[4:]
    a = s.find("{")
    b = s.rfind("}")
    if a == -1 or b == -1 or b < a:
        return None
    try:
        obj = json.loads(s[a:b + 1])
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


def _gather_own_dialogs(diary_db_path: str, since: float,
                        persona_name: str, max_chars: int = 4000) -> str:
    """Read-only: poslední Hansovy dialogy (human_chat + teddy_dialog) v okně,
    jen ty, které obsahují Hansovu repliku. '' když nic."""
    conn = None
    notes = []
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % diary_db_path, uri=True,
                               timeout=5.0)
        rows = conn.execute(
            "SELECT note FROM diary WHERE event_type IN "
            "('human_chat','teddy_dialog') AND ts > ? AND note IS NOT NULL "
            "AND note != '' ORDER BY ts DESC LIMIT 60", (since,)).fetchall()
        marker = "%s:" % persona_name
        for (note,) in rows:
            n = (note or "").strip()
            if marker in n:
                notes.append(n)
    except Exception as e:
        _log.warning("_gather_own_dialogs failed: %s", e)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    if not notes:
        return ""
    # nejnovější napřed; ořízni na rozpočet znaků
    out, total = [], 0
    for n in notes:
        if total + len(n) > max_chars:
            break
        out.append(n)
        total += len(n)
    return "\n\n".join(out)


def run_self_critique(config: dict, diary_db_path: str,
                      window_hours: float = 72.0) -> str:
    """Noční krok z vlastního popudu: z Hansových replik najde slabé místo a uloží
    'self_critique' do deníku. NEMĚNÍ paměť/postoje. Kódy (deferral-safe):
       'critiqued' — vzniklo ponaučení
       'idle'      — vypnuto / žádné repliky / repliky v pořádku (běželo, ale nic)
       'deferred'  — LLM nedostupná → retry (guard se nenastaví)."""
    cfg = (config.get("selfcritique", {}) or {})
    if not cfg.get("enabled", True):
        return "idle"
    window_hours = float(cfg.get("window_hours", window_hours))
    since = time.time() - window_hours * 3600.0
    try:
        from scripts.hans_persona import persona_name as _pn
        pname = _pn(config)
    except Exception:
        pname = "Hans"
    transcript = _gather_own_dialogs(diary_db_path, since, pname,
                                     int(cfg.get("max_chars", 4000)))
    if not transcript.strip():
        _log.info("self_critique: žádné vlastní repliky v okně, skip")
        return "idle"
    # anti-repetice: nedávné sebekritiky do promptu, ať se neopakuje
    recent = recent_selfcritiques(diary_db_path,
                                  hours=float(cfg.get("avoid_hours", 168.0)),
                                  limit=6)
    er = config.get("evening_reflection", {}) or {}
    model = str(cfg.get("model", er.get("model",
                "jobautomation/OpenEuroLLM-Czech:latest")))
    timeout = int(cfg.get("llm_timeout", 300))
    system = _SYSTEM.format(persona_name=pname)
    user = transcript
    if recent:
        user += ("\n\n(Tato ponaučení už sis vzal — najdi něco JINÉHO, neopakuj je:\n"
                 + "\n".join("- %s" % r[:120] for r in recent) + ")")
    try:
        from scripts.ollama_client import ollama_generate
    except Exception as e:
        _log.warning("self_critique: ollama_client nedostupný: %s", e)
        return "deferred"
    try:
        raw = ollama_generate(model=model, prompt=user, system=system,
                              config=config, timeout=timeout, keep_alive=0,
                              options={"temperature": 0.3})
    except Exception as e:
        _log.warning("self_critique LLM: %s", e)
        return "deferred"
    if raw is None:
        # ollama_generate vrací None při výpadku / herním módu → retry
        return "deferred"
    obj = _extract_json_obj(raw)
    if not obj:
        _log.info("self_critique: model nevrátil objekt (nebo {} = bez výtky)")
        return "idle"
    lesson = str(obj.get("lesson", "") or "").strip()
    if len(lesson) < 8:
        _log.info("self_critique: bez ponaučení (vše v pořádku)")
        return "idle"
    weak = str(obj.get("weak_quote", "") or "").strip()
    issue = str(obj.get("issue", "") or "").strip()
    try:
        db = sqlite3.connect(diary_db_path, timeout=5.0)
        db.execute(
            "INSERT INTO diary (ts, event_type, title, note, data) "
            "VALUES (?,?,?,?,?)",
            (time.time(), "self_critique", pname, lesson,
             json.dumps({"weak_quote": weak, "issue": issue},
                        ensure_ascii=False)))
        db.commit()
        db.close()
    except Exception as e:
        _log.warning("self_critique diary write failed: %s", e)
        return "deferred"
    _log.info("self_critique: %.80s", lesson)
    return "critiqued"


def recent_selfcritiques(diary_db_path: str, hours: float = 72.0,
                         limit: int = 4) -> list:
    """READ-ONLY: posledních `limit` sebekritik (note) za okno. chyba → []."""
    since = time.time() - hours * 3600.0
    conn = None
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % diary_db_path, uri=True,
                               timeout=3.0)
        rows = conn.execute(
            "SELECT note FROM diary WHERE event_type='self_critique' "
            "AND ts > ? ORDER BY ts DESC LIMIT ?", (since, int(limit))).fetchall()
        return [r[0].strip() for r in rows if r and r[0] and r[0].strip()]
    except Exception as e:
        _log.debug("recent_selfcritiques failed: %s", e)
        return []
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


if __name__ == "__main__":
    import tempfile, os
    print("=== hans_selfcritique smoke (temp DB) ===")
    fd, p = tempfile.mkstemp(suffix=".db"); os.close(fd)
    db = sqlite3.connect(p)
    db.execute("CREATE TABLE diary (id INTEGER PRIMARY KEY, ts REAL, event_type TEXT, "
               "title TEXT, note TEXT, data TEXT)")
    db.execute("INSERT INTO diary(ts,event_type,title,note) VALUES (?,?,?,?)",
               (time.time(), "self_critique", "Hans",
                "Příště odpovím rovnou k věci, bez zdlouhavých omluvných úvodů."))
    db.commit(); db.close()
    print("recent_selfcritiques:", recent_selfcritiques(p))
    print("_gather_own_dialogs (prázdné):", repr(_gather_own_dialogs(p, 0, "Hans")))
    os.unlink(p)
