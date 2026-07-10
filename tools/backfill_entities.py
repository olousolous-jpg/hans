#!/usr/bin/env python3
"""HANS_ENTITY_STORE_C1_V1 — backfill entity store z historie čtení (OFFLINE).

Aby byl C1 užitečný HNED (Hans už přečetl stovky článků): projde distinct
`web_read` tituly a z nejnovější poznámky vezme první větu. PŘÍSNÝ FILTR
kvality — glos přijme JEN když opravdu začíná definicí entity: první token
věty prefix-odpovídá titulu A brzy následuje „je/byl/…". Tím odmítne
znečištěné personalizované shrnutí („Stando, zajímavé je…", „Rozloha Afriky
je…", „[Otázka:…]") a nechá jen pravé definice („Design je…", „Arthur …
byl britský spisovatel…"). Glos vydávaný za „ověřený fakt" musí být čistý.

Deterministické (bez LLM, bez sítě → žádný rate-limit), idempotentní.
Entity, které filtrem neprojdou, se zachytí čistě později při novém čtení.

Použití:  python3 -m tools.backfill_entities [--db …] [--dry]
"""
import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.hans_entities import EntityStore, _first_sentence, _tokens, _tok_match  # noqa: E402

_PREFIX = re.compile(r"^\s*\[[a-z_]+\]\s*")   # „[kodi] ", „[object] " …
_COPULA = {"je", "byl", "byla", "bylo", "jsou", "jest", "byli", "byly"}


def _clean_definition(title: str, note: str) -> str:
    """Vrať glos JEN když věta opravdu začíná definicí entity, jinak ''."""
    body = _PREFIX.sub("", note or "")
    gloss = _first_sentence(body)
    if not gloss:
        return ""
    gt = _tokens(gloss)
    tt = [t for t in _tokens(title) if len(t) >= 3]
    if not gt or not tt:
        return ""
    # 1. token věty musí prefix-odpovídat 1. významnému tokenu titulu
    if not _tok_match(tt[0], gt[0]):
        return ""
    # do 6 slov musí padnout sponová „je/byl…"
    if not any(t in _COPULA for t in gt[:6]):
        return ""
    return gloss


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/hans_diary.db")
    ap.add_argument("--dry", action="store_true")
    args = ap.parse_args()

    cfg = {}
    cfgp = Path("config.json")
    if cfgp.exists():
        cfg = json.loads(cfgp.read_text(encoding="utf-8"))
    cfg["diary_db"] = args.db

    # nejnovější poznámka per titul (freshest = nejlepší grounding po V1)
    conn = sqlite3.connect("file:%s?mode=ro" % args.db, uri=True)
    rows = conn.execute(
        "SELECT d.title, d.note FROM diary d "
        "JOIN (SELECT title, MAX(ts) mts FROM diary "
        "      WHERE event_type='web_read' AND title IS NOT NULL "
        "      GROUP BY title) m "
        "ON d.title=m.title AND d.ts=m.mts "
        "WHERE d.event_type='web_read'").fetchall()
    conn.close()

    es = None if args.dry else EntityStore(cfg, args.db)
    total = len(rows)
    accepted = shown = 0
    for title, note in rows:
        title = (title or "").strip()
        if len(title) < 2:
            continue
        gloss = _clean_definition(title, note)
        if not gloss:
            continue
        accepted += 1
        if args.dry:
            if shown < 25:
                print(f"  {title[:32]:32} | {gloss[:62]}")
                shown += 1
        else:
            es.upsert(title, gloss, source_title=title)

    print(f"\nTitulů: {total} | přijato (čistá definice): {accepted} | "
          f"odmítnuto: {total - accepted}")
    if es is not None:
        print(f"Store nyní obsahuje {es.count()} entit.")


if __name__ == "__main__":
    main()
