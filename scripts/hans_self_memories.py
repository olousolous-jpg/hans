"""scripts/hans_self_memories.py

AUTOBIOGRAPHICAL_SELF_MEMORIES_V1 — krok 2 autobiografické vrstvy.

Kurátoruje set NEJDŮLEŽITĚJŠÍCH identity-relevantních epizod (self-defining
memories à la McAdams) z importance-skórovaného deníku (krok 1,
AUTOBIOGRAPHICAL_IMPORTANCE_V1). Read-only — žádný stav, jen čte diary.importance.

Severka je čte VEDLE stances/koníčků → návrh změny charakteru je grounded
v konkrétních PIVOTNÍCH EPIZODÁCH, ne jen v abstraktních tendencích.
Viz [[autobiographical-layer-roadmap]] (krok 3 = narativní konsolidace).
"""
import sqlite3
import logging
from datetime import datetime

_log = logging.getLogger("hans_self_memories")

# Šum / strojové typy bez vyprávěcí hodnoty — do pivotních vzpomínek nepatří.
_EXCLUDE_TYPES = (
    "distillation_finding", "tendency_snapshot", "kodi_playing", "person_seen",
    "teddy_arrived", "teddy_gone", "weather", "night_summary", "wol",
)


def self_defining_memories(db_path: str, min_importance: int = 7,
                           limit: int = 10) -> list:
    """Vrátí kurátorovaný set nejdůležitějších epizod (read-only).
    [{date, importance, event_type, text}], seřazeno importance DESC, ts DESC."""
    out = []
    try:
        con = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True, timeout=3.0)
    except Exception as e:
        _log.debug("self_memories connect failed: %s", e)
        return out
    try:
        ph = ",".join("?" * len(_EXCLUDE_TYPES))
        rows = con.execute(
            "SELECT ts, importance, event_type, COALESCE(data, note, title, '') "
            "FROM diary WHERE importance >= ? AND event_type NOT IN (%s) "
            "AND COALESCE(data, note, title, '') != '' "
            "ORDER BY importance DESC, ts DESC LIMIT ?" % ph,
            (int(min_importance), *_EXCLUDE_TYPES, int(limit) * 2)).fetchall()
    except Exception as e:
        _log.debug("self_memories query failed: %s", e)
        rows = []
    finally:
        con.close()

    seen = set()
    for ts, imp, et, txt in rows:
        t = " ".join(str(txt).split())
        if t.startswith("{"):           # surový JSON → přeskoč
            continue
        t = t[:200]
        key = t[:60].lower()
        if key in seen:                 # dedup near-duplicitních
            continue
        seen.add(key)
        out.append({
            "date": datetime.fromtimestamp(ts).strftime("%-d.%-m.%Y"),
            "importance": int(imp),
            "event_type": et,
            "text": t,
        })
        if len(out) >= limit:
            break
    return out


def format_block(mems: list) -> str:
    """Textový blok pro prompt (Severka)."""
    if not mems:
        return "(žádné)"
    return "\n".join(f"- [{m['date']}, váha {m['importance']}/10] {m['text']}"
                     for m in mems)


if __name__ == "__main__":  # smoke: python3 scripts/hans_self_memories.py
    import sys
    db = sys.argv[1] if len(sys.argv) > 1 else "data/hans_diary.db"
    mems = self_defining_memories(db)
    print("self-defining memories: %d\n" % len(mems))
    print(format_block(mems))
