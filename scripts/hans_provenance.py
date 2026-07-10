"""HANS_PROVENANCE_V1 — provenience / epistemické značky.

Vůdčí princip anti-konfabulace, bod 3 konvergentní cesty: každé držené
tvrzení nese ZDROJ (zažil / řekli mi / četl / odvodil / představil si /
vytvořil) → kalibrovaná jistota + rozlišení VZPOMÍNKA vs. IMAGINACE.

Zbytková třída konfabulací = imaginace prosakující do paměti (vysněná
"první vzpomínka", syntézní fantom "AJ II" tvrzený jako fakt). A1/C1/#2
řeší EXTERNÍ fakta; provenience je PRŮŘEZOVÁ vrstva na znalost samotnou —
donese epistemický status až k místu generace, aby Hans nevydával
představu za skutečnost.

SSOT: kanonická mapa event_type→třída a kolekce→třída. Deterministické,
BEZ LLM. Používá se (a) při zápisu (sloupec `diary.provenance`), (b) při
surfacingu (deníkový kontext + RAG chunky dostanou značku), (c) ve steeru
promptu (source-monitoring).
"""
from __future__ import annotations

import logging
import sqlite3
from typing import Optional

log = logging.getLogger(__name__)

# ── Kanonické třídy: klíč → (CZ label pro prompt, epistemický tier) ──────────
# tier: 'fakt' = smí se tvrdit jako skutečnost; 'odvozeno' = vlastní úsudek,
#       ne přímý fakt; 'imaginace' = NESMÍ se tvrdit jako skutečnost;
#       'nejisté' = smíšený zdroj, mluv opatrně.
CLASSES: dict[str, tuple[str, str]] = {
    "zazil":      ("zažil jsem",                             "fakt"),
    "rekli":      ("řekli mi",                               "fakt"),
    "cetl":       ("četl jsem",                              "fakt"),
    "vytvoril":   ("sám jsem vytvořil",                      "fakt"),
    "odvodil":    ("odvodil jsem úvahou",                    "odvozeno"),
    "predstavil": ("představil jsem si / vysnil",            "imaginace"),
    "nejiste":    ("z mého deníku (vzpomínka nebo úvaha)",   "nejisté"),
}

# ── event_type → třída ───────────────────────────────────────────────────────
EVENT_MAP: dict[str, str] = {
    # zažil (přímá percepce / prožitek)
    "person_seen": "zazil", "room_description": "zazil", "kodi_playing": "zazil",
    "movie_opinion": "zazil", "movie_browsed": "zazil", "activity": "zazil",
    "idle_start": "zazil", "idle_end": "zazil", "teddy_arrived": "zazil",
    "teddy_gone": "zazil", "observation": "zazil", "phase_change": "zazil",
    "game_launched": "zazil", "downtime_noticed": "zazil",
    "agent_action": "zazil",
    "capability_gained": "zazil", "capability_explored": "zazil",
    "kodi_paused": "zazil", "autoplay_next": "zazil",
    # řekli mi (od člověka v chatu / korekce)
    "human_chat": "rekli", "chat_reflection": "rekli",
    "downtime_account": "rekli", "lesson_learned": "rekli",
    # četl jsem (kniha / web / studium)
    "web_read": "cetl", "reading_takeaway": "cetl", "book_read": "cetl",
    "book_reflection": "cetl", "book_completion_reflection": "cetl",
    "study_note": "cetl", "study_mastery": "cetl",
    # odvodil jsem (analytika / reflexe = vlastní úsudek, ne přímý fakt)
    "introspection": "odvodil", "night_summary": "odvodil",
    "synthesis_idea": "odvodil", "tendency_snapshot": "odvodil",
    "narrative_chapter": "odvodil", "creation_reflection": "odvodil",
    "work_completion_reflection": "odvodil", "self_critique": "odvodil",
    "immune_check": "odvodil", "contradiction_flag": "odvodil",
    "severka_proposal": "odvodil", "morning_health": "odvodil",
    "dialog_reflection": "odvodil", "evening_reflection": "odvodil",
    # představil jsem si (fantazie — NESMÍ jako fakt)
    "dream": "predstavil", "musing": "predstavil", "spontaneous": "predstavil",
    "artwork": "predstavil", "teddy_dialog": "predstavil",
    # vytvořil jsem (vlastní dílo — reálně existuje)
    "work_created": "vytvoril", "writing_section": "vytvoril",
    "writing_completed": "vytvoril",
}

# ── RAG kolekce → třída (fallback, když chunk nenese per-event proveniences) ──
# hans_denik ZÁMĚRNĚ 'nejiste' — míchá prožitky se sny/úvahami; na úrovni
# kolekce nelze rozlišit → mluv opatrně (abstinence > sebejistý omyl).
COLLECTION_MAP: dict[str, str] = {
    "hans_cetba": "cetl",
    "hans_filmy": "zazil",
    "hans_pripady": "rekli",
    "hans_identita": "odvodil",
    "hans_dila": "vytvoril",
    "hans_denik": "nejiste",
}

_DEFAULT = "zazil"

# Steer do system promptu (full mód, ne pozdrav) — source-monitoring.
STEER = (
    "PŮVOD VZPOMÍNEK (rozlišuj, odkud znalost máš): co jsi ZAŽIL, ČETL, "
    "ŘEKLI TI nebo SÁM VYTVOŘIL smíš tvrdit jako skutečnost. Co sis "
    "PŘEDSTAVIL či VYSNIL nebo jen ODVODIL úvahou NENÍ prokázaný fakt — "
    "mluv o tom jako o své představě/úvaze (zdálo se mi / napadlo mě / "
    "mám dojem), NIKDY to nevydávej za skutečnou událost. Údaje z "
    "deníku označené jako vzpomínka-nebo-úvaha ber opatrně. Neznáš-li "
    "původ, přiznej to."
)


def provenance_of(event_type: Optional[str], default: str = _DEFAULT) -> str:
    """event_type → třída provenience."""
    if not event_type:
        return default
    return EVENT_MAP.get(event_type, default)


def provenance_of_collection(coll: Optional[str], default: str = _DEFAULT) -> str:
    """RAG kolekce → třída provenience (fallback)."""
    if not coll:
        return default
    return COLLECTION_MAP.get(coll, default)


def label(cls: Optional[str]) -> str:
    """Třída → CZ label pro prompt."""
    return CLASSES.get(cls or "", CLASSES[_DEFAULT])[0]


def tier(cls: Optional[str]) -> str:
    """Třída → epistemický tier (fakt/odvozeno/imaginace/nejisté)."""
    return CLASSES.get(cls or "", CLASSES[_DEFAULT])[1]


def marker(cls: Optional[str]) -> str:
    """Kompaktní inline značka pro řádek kontextu, např. '[zažil jsem]'."""
    return f"[{label(cls)}]"


# ── Persistence: sloupec diary.provenance ("provenience do zápisu") ──────────

def ensure_column(db: sqlite3.Connection) -> None:
    """Idempotentně přidá sloupec `provenance` do diary."""
    try:
        cols = [r[1] for r in db.execute("PRAGMA table_info(diary)").fetchall()]
        if "provenance" not in cols:
            db.execute("ALTER TABLE diary ADD COLUMN provenance TEXT")
            db.commit()
            log.info("hans_provenance: sloupec diary.provenance přidán")
    except Exception as e:  # pragma: no cover
        log.warning("hans_provenance.ensure_column: %s", e)


def catchup(db: sqlite3.Connection, limit: Optional[int] = None) -> int:
    """Doplní provenance u řádků, kde je NULL (self-healing / nové zápisy).

    Deterministicky z EVENT_MAP. Vzor importance NULL-backstop. Vrací počet
    aktualizovaných řádků. `limit=None` = všechny (levné, čisté SQL).
    """
    try:
        ensure_column(db)
        before = db.execute(
            "SELECT COUNT(*) FROM diary WHERE provenance IS NULL"
        ).fetchone()[0]
        if before == 0:
            return 0
        # Skupiny event_type po třídách → hromadné UPDATE (IN(...)).
        by_class: dict[str, list[str]] = {}
        for et, cls in EVENT_MAP.items():
            by_class.setdefault(cls, []).append(et)
        for cls, ets in by_class.items():
            ph = ",".join("?" * len(ets))
            db.execute(
                f"UPDATE diary SET provenance=? "
                f"WHERE provenance IS NULL AND event_type IN ({ph})",
                (cls, *ets),
            )
        # neznámé event_types → default (aby nezůstaly navždy NULL)
        db.execute(
            "UPDATE diary SET provenance=? WHERE provenance IS NULL",
            (_DEFAULT,),
        )
        db.commit()
        after = db.execute(
            "SELECT COUNT(*) FROM diary WHERE provenance IS NULL"
        ).fetchone()[0]
        done = before - after
        log.info("hans_provenance.catchup: doplněno %d řádků", done)
        return done
    except Exception as e:  # pragma: no cover
        log.warning("hans_provenance.catchup: %s", e)
        return 0


if __name__ == "__main__":  # ruční backfill/kontrola
    import sys
    logging.basicConfig(level=logging.INFO)
    path = sys.argv[1] if len(sys.argv) > 1 else "data/hans_diary.db"
    conn = sqlite3.connect(path)
    catchup(conn)
    print("Rozložení provenience v deníku:")
    for cls, n in conn.execute(
        "SELECT provenance, COUNT(*) FROM diary GROUP BY provenance "
        "ORDER BY COUNT(*) DESC"
    ).fetchall():
        print(f"  {cls or '(NULL)':12} {n}")
    conn.close()
