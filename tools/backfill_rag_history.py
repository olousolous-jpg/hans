#!/usr/bin/env python3
"""
backfill_rag_history.py — jednorázový backfill Hansovy historické paměti do RAG.

Po ztrátě OpenWebUI dat (RAG kolekce) je deník (hans_diary.db) nedotčený, ale
RAG umí sémanticky vybavit jen to, co vzniklo od obnovy. Tento skript nasype
KURÁTOROVANÝ vysoký signál z deníku do RAG kolekcí (NE vjemový firehose jako
person_seen/teddy_arrived), aby Hans zase sémanticky dosáhl do své minulosti.

- Text bere přes COALESCE(NULLIF(data,''), note) (kanonický vzor v codebase).
- doc_id je deterministický (hist_<typ>_<id>) → idempotentní, re-run bezpečný
  (HansKnowledge.upload nahradí existující).
- Bez LLM — reflexe už v deníku JSOU, jen se formátují + embedují (bge-m3).

Spuštění z kořene projektu:  python3 tools/backfill_rag_history.py
"""
from __future__ import annotations

import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import json
from scripts.hans_knowledge import HansKnowledge

# event_type → RAG kolekce (rozsah: vysoký signál + web_read + human_chat)
TARGETS = {
    "hans_cetba":   ["reading_takeaway", "book_reflection",
                     "book_completion_reflection", "study_note", "web_read"],
    "hans_filmy":   ["movie_opinion"],
    "hans_pripady": ["case_closed", "case_resolution", "chat_reflection",
                     "human_chat"],
    "hans_denik":   ["evening_reflection", "night_summary", "dream",
                     "introspection", "spontaneous", "musing", "observation",
                     "narrative_chapter"],
    "hans_identita": ["study_mastery", "synthesis_idea", "creation_reflection"],
    "hans_dila":    ["work_created", "writing_section", "artwork"],
}

_CZ_LABEL = {
    "reading_takeaway": "Co jsem si odnesl ze čtení",
    "book_reflection": "Úvaha ke knize", "book_completion_reflection": "Dočtená kniha",
    "study_note": "Studijní poznámka", "web_read": "Co jsem četl",
    "movie_opinion": "Můj názor na film", "case_closed": "Uzavřený případ",
    "case_resolution": "Rozuzlení případu", "chat_reflection": "Dojem z rozhovoru",
    "human_chat": "Rozhovor", "evening_reflection": "Večerní reflexe",
    "night_summary": "Ohlédnutí za dnem", "dream": "Sen",
    "introspection": "Vnitřní úvaha", "spontaneous": "Spontánní myšlenka",
    "musing": "Zamyšlení", "observation": "Pozorování",
    "narrative_chapter": "Kapitola mého příběhu", "study_mastery": "Zvládnuté téma",
    "synthesis_idea": "Postřeh", "creation_reflection": "Úvaha o tvorbě",
    "work_created": "Vytvořené dílo", "writing_section": "Část díla",
    "artwork": "Obraz",
}

_WD = ('pondělí', 'úterý', 'středa', 'čtvrtek', 'pátek', 'sobota', 'neděle')
MIN_LEN = 40
MAX_LEN = 4000


def _doc_text(title: str, ts: float, body: str) -> str:
    parts = []
    if title:
        parts.append(f"# {title}")
    if ts:
        d = datetime.fromtimestamp(ts)
        parts.append(f"_Kdy: {_WD[d.weekday()]} {d.day}.{d.month}.{d.year}_")
    parts.append(body.strip()[:MAX_LEN])
    return "\n\n".join(parts)


def main() -> int:
    cfg = json.load(open(ROOT / "config.json", encoding="utf-8"))
    kn = HansKnowledge(cfg)
    if not kn.enabled:
        print("✗ HansKnowledge disabled (chybí token/kolekce?) — končím.")
        return 1

    db = sqlite3.connect(str(ROOT / "data" / "hans_diary.db"))
    db.row_factory = sqlite3.Row

    total_ok = total_skip = total_fail = 0
    t_start = time.time()

    for collection, types in TARGETS.items():
        ph = ",".join("?" * len(types))
        rows = db.execute(
            f"SELECT id, ts, event_type, title, "
            f"       COALESCE(NULLIF(data,''), note) AS body "
            f"FROM diary WHERE event_type IN ({ph}) "
            f"ORDER BY ts ASC", types).fetchall()
        col_ok = 0
        for r in rows:
            body = (r["body"] or "").strip()
            if len(body) < MIN_LEN:
                total_skip += 1
                continue
            title = (r["title"] or "").strip() or _CZ_LABEL.get(
                r["event_type"], r["event_type"])
            text = _doc_text(title, r["ts"], body)
            doc_id = f"hist_{r['event_type']}_{r['id']}"
            meta = {"typ": r["event_type"],
                    "kdy": datetime.fromtimestamp(r["ts"]).strftime("%Y-%m-%d")
                    if r["ts"] else ""}
            ok = kn.upload(collection, doc_id, title, text, meta)
            if ok:
                col_ok += 1
                total_ok += 1
            else:
                total_fail += 1
            if total_ok % 100 == 0 and total_ok:
                el = time.time() - t_start
                print(f"  … {total_ok} nahráno ({el:.0f}s, "
                      f"{total_ok/el:.1f}/s), právě {collection}", flush=True)
        print(f"✓ {collection:15s} {col_ok} nahráno", flush=True)

    try:
        kn.stop()
    except Exception:
        pass
    el = time.time() - t_start
    print(f"\nHOTOVO za {el:.0f}s — {total_ok} nahráno, "
          f"{total_skip} přeskočeno (krátké), {total_fail} chyb.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
