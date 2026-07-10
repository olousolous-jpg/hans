#!/usr/bin/env python3
"""
HANS_OPINION_GROUNDING_G1_V1 — grounding NÁZORU (imaginativní registr).

Zrcadlo faktického groundingu: u FILOSOFICKÝCH / NÁZOROVÝCH dotazů se do
system promptu injektují Hansovy VLASTNÍ postoje (stances s výhradami),
tendence (core/tension) a poslední syntézní postřeh + steer „zaujmi postoj"
— místo aby Hans odpovídal „od nuly" generickou LLM vyvážeností.

Princip 2 registrů ([[anticonfabulation-guiding-principle]]):
  - faktický registr  → groundovat na datech / abstinovat (G3B/A1/C1),
  - imaginativní registr → vymýšlet ŽÁDOUCÍ, ale groundované na TOM, KDO HANS JE
    (jeho postoje), ne na faktech.

Volá se POUZE když intent klasifikoval dotaz jako NEfaktický (gate dělá
volající — openwebui_direct_handler.send_chat_message). Detekce názorového
dotazu je deterministická (regex), aby se postoje nevnucovaly do běžného
chatu („jak se máš" ≠ filosofie).

Read-only, deferral-safe: cokoliv chybí/selže → '' (chat jede beze změny).
"""
from __future__ import annotations

import logging
import re
import sqlite3
import time
from typing import Optional

_log = logging.getLogger(__name__)

# ── Detekce názorového / filosofického dotazu ────────────────────────────────
# Přímá žádost o názor — silný signál sám o sobě.
_ASK_PAT = re.compile(
    r"("
    r"co\s+si\s+(o\s+\w+\s+)?mysl[íi]|co\s+mysl[íi]š|"
    r"jak[ýy]\s+(je\s+tv[ůu]j|m[áa]š)\s+n[áa]zor|tv[ůu]j\s+n[áa]zor|"
    r"mysl[íi]š,?\s+že|"
    r"(ne)?souhlas[íi]š|"
    r"jak\s+to\s+(ty\s+)?vid[íi]š|jak\s+to\s+vn[íi]m[áa]š|"
    r"podle\s+tebe|"
    r"věř[íi]š(,|\s)|"
    r"co\s+je\s+pro\s+tebe|"
    r"je\s+spr[áa]vn[ée]|mělo?\s+by\s+se|"
    r"co\s+bys\s+(udělal|zvolil|raději|preferoval)|"
    r"je\s+lepš[íi]\s+.+\s+nebo\s+"
    r")",
    re.IGNORECASE,
)

# Filosofická témata — spouští jen SPOLU s otázkovým signálem.
_PHIL_PAT = re.compile(
    r"\b("
    r"smysl\s+(života|existence|být[íi])|smysl\s+život|"
    r"svoboda|svobodn[áé]\s+v[ůu]le|vědom[íi]|duše|"
    r"štěst[íi]|smrt|smrteln|nesmrteln|"
    r"morálk|etik|mravn|spravedln|"
    r"pravd[aě]|krás[aoy]|uměn[íi]|osud|"
    r"existenc|byt[íi]|nicot|"
    r"l[áa]sk[aou]|přátelstv[íi]|"
    r"uměl[áa]\s+inteligence|strojů?m?\s+myslet|"
    r"budoucnost\s+lidstva|lidsk[áé]\s+povah"
    r")",
    re.IGNORECASE,
)

_QUESTION_SIGNAL = re.compile(r"(\?|\b(proč|jak|co|zda|jestli)\b)",
                              re.IGNORECASE)


def is_opinion_query(text: str) -> bool:
    """True když dotaz žádá NÁZOR/postoj (ne fakt, ne běžný small talk)."""
    if not text or not text.strip():
        return False
    msg = text.strip()
    if _ASK_PAT.search(msg):
        return True
    # filosofické téma + otázkový signál (samotné slovo „láska" v běžné větě
    # postoje nespouští)
    if _PHIL_PAT.search(msg) and _QUESTION_SIGNAL.search(msg):
        return True
    return False


# ── Stavba bloku postojů ─────────────────────────────────────────────────────

def _read_stances(db_path: str, limit: int = 6) -> list[dict]:
    """Aktivní postoje s výhradami, nejdřív nejpevnější. Read-only."""
    conn = None
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True,
                               timeout=2.0)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT claim, confidence, evidence_count, counterargs "
            "FROM stances WHERE status='active' "
            "ORDER BY evidence_count DESC, confidence DESC LIMIT ?",
            (limit,)).fetchall()
        out = []
        for r in rows:
            cargs = []
            if r["counterargs"]:
                try:
                    import json as _j
                    cargs = _j.loads(r["counterargs"]) or []
                except Exception:
                    cargs = []
            out.append({
                "claim": (r["claim"] or "").strip(),
                "confidence": float(r["confidence"] or 0.5),
                "evidence": int(r["evidence_count"] or 1),
                "counterarg": (str(cargs[-1]).strip() if cargs else ""),
            })
        return [s for s in out if s["claim"]]
    except Exception as e:
        _log.debug("opinion: stances read failed: %s", e)
        return []
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _latest_insight(db_path: str, max_age_days: int = 30) -> str:
    """Poslední syntézní postřeh (<max_age_days). '' když žádný."""
    conn = None
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True,
                               timeout=2.0)
        row = conn.execute(
            "SELECT insight FROM synthesis_idea WHERE ts > ? "
            "ORDER BY id DESC LIMIT 1",
            (time.time() - max_age_days * 86400,)).fetchone()
        return (row[0] or "").strip() if row else ""
    except Exception:
        return ""
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


_STEER = (
    "Toto je otázka na NÁZOR, ne na fakta — tady smíš (a máš) přemýšlet"
    " svobodně. ZAUJMI VLASTNÍ POSTOJ: mluv v 1. osobě s přesvědčením,"
    " klidně vyhraněně; smíš nesouhlasit s tazatelem, spekulovat i být"
    " originální. VYHNI SE generické oboustranné vyváženosti („na jednu"
    " stranu… na druhou stranu…“) a akademickému shrnování cizích pozic —"
    " odpověz, co si myslíš TY a PROČ, klidně s výhradou, kterou sám cítíš."
    " Negrounduj na faktech, ale na tom, kdo jsi (postoje výše). Když se"
    " tvé postoje dotazu netýkají, zformuluj stanovisko nové — v jejich duchu.")


def opinion_block(config: dict, db_path: Optional[str] = None,
                  max_stances: int = 6) -> str:
    """Blok „MOJE POSTOJE" pro system prompt názorového dotazu.

    Obsah: postoje (teze + poslední výhrada), poslední syntézní postřeh,
    steer „zaujmi postoj". '' když vypnuto. I bez postojů vrací steer
    (odvaha je jádro G1 — generická vyváženost je problém i bez stance dat).
    """
    og = (config or {}).get("opinion_grounding", {}) or {}
    if not og.get("enabled", True):
        return ""
    dbp = (db_path
           or (config or {}).get("diary_db")
           or ((config or {}).get("hans_idle", {}) or {}).get("diary_db")
           or "data/hans_diary.db")

    parts = ["\n\nMOJE VLASTNÍ POSTOJE (vznikly z mého čtení, přemýšlení a"
             " rozhovorů — z NICH vycházej, ne z obecných frází):"]
    stances = _read_stances(dbp, limit=int(og.get("max_stances", max_stances)))
    if stances:
        for s in stances:
            line = f"- {s['claim']}"
            if s["counterarg"]:
                line += f" (má výhrada: {s['counterarg']})"
            parts.append(line)
    else:
        parts = ["\n"]  # bez postojů jen steer (žádný prázdný nadpis)

    insight = _latest_insight(dbp)
    if insight:
        parts.append("Můj nedávný vlastní postřeh (smíš z něj vyjít): "
                     + insight)

    parts.append("\n" + _STEER)
    return "\n".join(parts)


# ── Smoke (python3 -m scripts.hans_opinion) ──────────────────────────────────
if __name__ == "__main__":
    import json
    cfg = {}
    try:
        with open("config.json", encoding="utf-8") as fh:
            cfg = json.load(fh)
    except Exception:
        pass

    tests = [
        ("Co si myslíš o moderní architektuře?", True),
        ("Má podle tebe život smysl?", True),
        ("Myslíš, že stroje můžou mít vědomí?", True),
        ("Souhlasíš s tím, že tradice jsou důležité?", True),
        ("Je správné lhát, když to někoho chrání?", True),
        ("Jak se máš?", False),
        ("Kdo je Hercule Poirot?", False),
        ("Kolik je hodin?", False),
        ("Ahoj Hansi", False),
        ("Mám rád lásku k dobrému jídlu", False),  # téma bez otázky
        ("Proč je svoboda důležitá?", True),
    ]
    ok = 0
    for q, want in tests:
        got = is_opinion_query(q)
        mark = "✓" if got == want else "✗"
        if got == want:
            ok += 1
        print(f"{mark} {got!s:5} (chci {want!s:5}) {q}")
    print(f"\n{ok}/{len(tests)} detekce OK")

    blk = opinion_block(cfg)
    print("\n=== opinion_block ===")
    print(blk if blk else "(prázdný)")
