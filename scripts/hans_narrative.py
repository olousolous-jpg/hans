"""scripts/hans_narrative.py

AUTOBIOGRAPHICAL_NARRATIVE_V1 — krok 3 autobiografické vrstvy: narativní konsolidace.

Periodicky (týdně) destiluje importance-vážený deník + postoje + koníčky + lidi
+ cíle + minulou kapitolu → KRÁTKOU reflektivní „kapitolu životního příběhu"
(1. osoba, Hans o sobě). Dává Hansovi narrative identity (McAdams) přes týdny —
ne jen sémantická fakta (stances), ale PŘÍBĚH: co ho formovalo, kým se stává.

Báze: analytický base model (anti-konfabulace, [[hans-czech-is-openeurollm-finetune]]),
striktně grounded v materiálu. Kapitola → diary event 'narrative_chapter'
(čte ji příští konsolidace pro kontinuitu; volitelně Severka/persona).
Viz [[autobiographical-layer-roadmap]].
"""
import sqlite3
import logging
from datetime import datetime, timedelta

_log = logging.getLogger("hans_narrative")

_EXCLUDE = ("distillation_finding", "tendency_snapshot", "kodi_playing",
            "person_seen", "teddy_arrived", "teddy_gone", "weather",
            "night_summary", "wol", "narrative_chapter")


def _ro(db_path):
    return sqlite3.connect("file:%s?mode=ro" % db_path, uri=True, timeout=4.0)


def gather(db_path: str, period_days: int = 7, min_importance: int = 6) -> dict:
    """Posbírá materiál za období (read-only)."""
    out = {"episodes": [], "stances": [], "hobbies": [], "people": [],
           "goals": [], "prev": ""}
    since = (datetime.now() - timedelta(days=period_days)).timestamp()
    try:
        con = _ro(db_path)
    except Exception as e:
        _log.debug("narrative gather connect: %s", e)
        return out
    try:
        ph = ",".join("?" * len(_EXCLUDE))
        # pivotní epizody období (importance-vážené)
        for ts, imp, txt in con.execute(
                "SELECT ts, importance, COALESCE(data,note,title,'') FROM diary "
                "WHERE ts>=? AND importance>=? AND event_type NOT IN (%s) "
                "AND COALESCE(data,note,title,'')!='' "
                "ORDER BY importance DESC, ts DESC LIMIT 16" % ph,
                (since, int(min_importance), *_EXCLUDE)).fetchall():
            t = " ".join(str(txt).split())
            if not t.startswith("{"):
                out["episodes"].append((int(imp), t[:200]))
        # postoje (aktivní, nejsilnější)
        try:
            for claim, conf, ec in con.execute(
                    "SELECT claim, confidence, evidence_count FROM stances "
                    "WHERE status='active' ORDER BY confidence DESC LIMIT 8").fetchall():
                out["stances"].append((claim, conf, ec))
        except Exception:
            pass
        # koníčky
        try:
            for name, ec in con.execute(
                    "SELECT name, evidence_count FROM hobbies "
                    "ORDER BY evidence_count DESC LIMIT 8").fetchall():
                out["hobbies"].append((name, ec))
        except Exception:
            pass
        # lidé (charakterizace)
        try:
            for nm, role, ch in con.execute(
                    "SELECT display_name, role, characterization FROM relationships "
                    "WHERE deactivated_at IS NULL ORDER BY sightings_count DESC LIMIT 5").fetchall():
                if ch:
                    out["people"].append((nm, role or "", " ".join(str(ch).split())[:220]))
        except Exception:
            pass
        # cíle
        try:
            for topic, status in con.execute(
                    "SELECT topic, status FROM hans_goals ORDER BY opened_at DESC LIMIT 4").fetchall():
                out["goals"].append((topic, status))
        except Exception:
            pass
        # minulá kapitola (kontinuita)
        try:
            r = con.execute(
                "SELECT COALESCE(data,note,'') FROM diary WHERE event_type='narrative_chapter' "
                "ORDER BY ts DESC LIMIT 1").fetchone()
            if r and r[0]:
                out["prev"] = " ".join(str(r[0]).split())[:600]
        except Exception:
            pass
    finally:
        con.close()
    return out


def latest_chapter(db_path: str) -> str:
    """Nejnovější narativní kapitola (text) nebo '' — pro Severku/personu."""
    try:
        con = _ro(db_path)
        r = con.execute("SELECT COALESCE(data,note,'') FROM diary "
                        "WHERE event_type='narrative_chapter' ORDER BY ts DESC LIMIT 1").fetchone()
        con.close()
        return " ".join(str(r[0]).split()) if r and r[0] else ""
    except Exception as e:
        _log.debug("latest_chapter failed: %s", e)
        return ""


def _build_prompt(m: dict, name: str) -> str:
    def block(items, fmt):
        return "\n".join(fmt(x) for x in items) or "(žádné)"
    ep = block(m["episodes"], lambda x: f"- [{x[0]}/10] {x[1]}")
    st = block(m["stances"], lambda x: f"- {x[0]} (jistota {x[1]:.2f})")
    ho = block(m["hobbies"], lambda x: f"- {x[0]} (×{x[1]})")
    pe = block(m["people"], lambda x: f"- {x[0]} ({x[1]}): {x[2]}")
    go = block(m["goals"], lambda x: f"- {x[0]} [{x[1]}]")
    prev = m["prev"] or "(zatím žádná předchozí kapitola)"
    return (f"MINULÁ KAPITOLA (naváž na ni, kontinuita):\n{prev}\n\n"
            f"PIVOTNÍ EPIZODY období:\n{ep}\n\n"
            f"POSTOJE:\n{st}\n\nKONÍČKY:\n{ho}\n\nLIDÉ:\n{pe}\n\nCÍLE:\n{go}")


def consolidate(config: dict, db_path: str, model: str = None,
                period_days: int = 7, timeout: int = 300) -> str:
    """Vytvoří a uloží narativní kapitolu. Vrací text (nebo '' při selhání).
    Deferral-safe — když LLM dole, nic nezapíše (zkusí se příště)."""
    from scripts.hans_persona import persona_name as _pn
    name = _pn(config)
    m = gather(db_path, period_days)
    if not m["episodes"] and not m["stances"]:
        _log.info("narrative: málo materiálu, skip")
        return ""
    if not model:
        er = config.get("evening_reflection", {}) or {}
        model = config.get("narrative", {}).get(
            "model", er.get("model", "jobautomation/OpenEuroLLM-Czech:latest"))
    system = (
        f"Jsi {name}ova autobiografická paměť. Z materiálu napiš KRÁTKOU reflektivní "
        f"kapitolu {name}ova životního příběhu v 1. OSOBĚ ({name} mluví o sobě). "
        "Shrň, co ho v tomto období formovalo, k čemu inklinuje, kým se POSTUPNĚ stává "
        "a jak se vyvíjejí jeho vztahy. Naváž na minulou kapitolu (kontinuita příběhu). "
        "STRIKTNĚ vycházej z materiálu — nic si nevymýšlej. Důstojný, střídmý, "
        "introspektivní tón. 4-7 vět, souvislý odstavec, žádné odrážky.")
    try:
        from scripts.ollama_client import ollama_generate
        text = ollama_generate(
            model=model, prompt=_build_prompt(m, name), system=system,
            config=config, timeout=timeout, keep_alive=0,
            options={"temperature": 0.4})
    except Exception as e:
        _log.warning("narrative: LLM selhal (zkusím příště): %s", e)
        return ""
    text = (text or "").strip()
    if not text or len(text) < 40:
        _log.warning("narrative: prázdná kapitola, skip")
        return ""
    # ulož do deníku
    try:
        con = sqlite3.connect(db_path, timeout=5.0)
        title = "Kapitola: %s" % datetime.now().strftime("%-d.%-m.%Y")
        con.execute("INSERT INTO diary (ts, event_type, title, data) VALUES (?,?,?,?)",
                    (datetime.now().timestamp(), "narrative_chapter", title, text))
        con.commit()
        con.close()
        _log.info("narrative: kapitola uložena (%d znaků)", len(text))
    except Exception as e:
        _log.warning("narrative: zápis selhal: %s", e)
    return text


if __name__ == "__main__":  # smoke: python3 scripts/hans_narrative.py [--dry]
    import sys, json
    cfg = json.load(open("config.json"))
    if "--dry" in sys.argv:
        mm = gather("data/hans_diary.db")
        print(_build_prompt(mm, "Hans")[:1500])
    else:
        print(consolidate(cfg, "data/hans_diary.db"))
