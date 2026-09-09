#!/usr/bin/env python3
"""backfill_zanr_knih.py — HANS_RAG_ZANR_BACKFILL_V1 (9. 9.)

Přepíše STÁVAJÍCÍ knižní chunky v RAG kolekci `hans_cetba` tak, aby nesly
ŽÁNROVOU HLAVIČKU („## Úryvek z beletrie" / „## Úryvek z literatury faktu").

Proč: `HANS_RAG_ZANR_KNIHY_V1` opravil jen NOVĚ nahrávané chunky. 729
stávajících leželo dál pod obecným „## Záznam", tedy označených stejně jako
věcný zdroj — a kapitoly románů sdílejí kolekci s Wikipedií a odbornými
pracemi.

⚠️ BEZPEČNOSTNÍ ZÁSADY (kvůli nim to není jen `for` cyklus):
  • přepisují se JEN doc_id, která v `hans_cetba` UŽ EXISTUJÍ — skript
    nikdy nezaloží nový dokument. Co se kdysi nenahrálo (23 kapitol bez
    reflexe), zůstane nenahrané.
  • idempotentní: `HansKnowledge.upload` nahradí obsah pod týmž doc_id,
    takže opakované spuštění nic nezdvojí.
  • obnovitelné: hotová doc_id se zapisují do `data/mereni/zanr_backfill.log`
    a při dalším běhu se přeskočí.
  • config přes `config_io.load()` — token a kolekce jsou od
    `HANS_CONFIG_SPLIT_V1` v privátní části. (Právě na tomhle je rozbitý
    starší `backfill_rag_history.py`.)

Spuštění z kořene projektu:
    python3 tools/backfill_zanr_knih.py            # ostrý běh
    python3 tools/backfill_zanr_knih.py --limit 3  # zkouška na pár kusech
    python3 tools/backfill_zanr_knih.py --suchy    # nic nenahraje, jen vypíše
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.config_io import load as nacti_config
from scripts.hans_knowledge import HansKnowledge
import scripts.hans_knowledge as hk
import scripts.hans_synthesis as HS
from scripts.hans_synthesis import HansSynthesisHooks as H

KOLEKCE = "hans_cetba"
POSTUP = ROOT / "data" / "mereni" / "zanr_backfill.log"
_WD = ("pondělí", "úterý", "středa", "čtvrtek", "pátek", "sobota", "neděle")


def _hotove() -> set:
    try:
        return {r.strip() for r in POSTUP.read_text(encoding="utf-8").splitlines()
                if r.strip()}
    except Exception:
        return set()


def _hist_text(title: str, ts: float, body: str, zanr: str) -> str:
    """Formát `backfill_rag_history.py` + žánrová hlavička."""
    hl = {"beletrie": "## Úryvek z beletrie",
          "fakta": "## Úryvek z literatury faktu"}.get(zanr, "## Úryvek z knihy")
    parts = []
    if title:
        parts.append("# %s" % title)
    if ts:
        d = datetime.fromtimestamp(ts)
        parts.append("_Kdy: %s %d.%d.%d_" % (_WD[d.weekday()], d.day, d.month, d.year))
    parts.append("%s\n\n%s" % (hl, body.strip()[:4000]))
    return "\n\n".join(parts)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--suchy", action="store_true")
    a = ap.parse_args()

    cfg = nacti_config()
    kn = HansKnowledge(cfg)
    if not kn.enabled and not a.suchy:
        print("✗ HansKnowledge vypnuté — končím."); return 1

    # co v RAGu UŽ JE (jen tyhle se smí přepsat)
    kc = sqlite3.connect(hk._KNOWLEDGE_DB)
    v_ragu = {r[0] for r in kc.execute(
        "SELECT doc_id FROM uploads WHERE collection_key=?", (KOLEKCE,))}
    kc.close()

    db = sqlite3.connect(str(ROOT / "data" / "hans_diary.db"))
    raw, refl = {}, {}
    for i, ts, t, b in db.execute(
            "SELECT id,ts,title,COALESCE(NULLIF(note,''),data,'') FROM diary "
            "WHERE event_type='book_read' AND COALESCE(title,'')<>''"):
        raw[t] = (i, ts, b)
    for i, ts, t, b in db.execute(
            "SELECT id,ts,title,COALESCE(NULLIF(note,''),data,'') FROM diary "
            "WHERE event_type='book_reflection' AND COALESCE(title,'')<>''"):
        refl[t] = (i, ts, b)

    ukoly = []          # (doc_id, title, text, zanr)
    for t, (i, ts, b) in raw.items():
        did = "book_" + HS._safe_id(t, 50)
        if did not in v_ragu:
            continue
        z = H._zanr_knihy(cfg, t)
        ukoly.append((did, t, H._build_rag_text(
            "book_read", t, b, (refl.get(t) or (0, 0, ""))[2], ts, z), z))
    for et in ("book_reflection", "book_completion_reflection"):
        for i, ts, t, b in db.execute(
                "SELECT id,ts,title,COALESCE(NULLIF(note,''),data,'') FROM diary "
                "WHERE event_type=? AND COALESCE(title,'')<>''", (et,)):
            did = "hist_%s_%d" % (et, i)
            if did not in v_ragu or len((b or "").strip()) < 40:
                continue
            z = H._zanr_knihy(cfg, t)
            ukoly.append((did, t, _hist_text(t, ts, b, z), z))
    db.close()

    hotove = _hotove()
    zbyva = [u for u in ukoly if u[0] not in hotove]
    print("dokumentů k přepsání: %d (hotovo dřív: %d)" % (len(zbyva), len(hotove)))
    from collections import Counter
    print("žánry:", dict(Counter(u[3] or "(neznámý)" for u in zbyva)))
    if a.limit:
        zbyva = zbyva[:a.limit]
    if a.suchy:
        for did, t, txt, z in zbyva[:3]:
            print("\n--- %s [%s]\n%s" % (did, z or "neznámý", txt[:260]))
        return 0

    t0 = time.time(); ok = chyb = 0
    # ⚠️ buffering=1 (řádkový) — bez něj se zápis drží v paměti a slib
    # „obnovitelné" NEPLATÍ: při pádu by se ztratil celý rozdělaný běh.
    # Doloženo při prvním ostrém běhu: soubor hlásil 3 hotové, zatímco
    # skript byl na 50. Týž vzor jako u měřicích běhů v CLAUDE.md.
    with open(POSTUP, "a", encoding="utf-8", buffering=1) as f:
        for n, (did, t, txt, z) in enumerate(zbyva, 1):
            try:
                if kn.upload(KOLEKCE, did, t, txt,
                             {"typ": "book_read", "zanr": z or "neznamy"}):
                    ok += 1; f.write(did + "\n")
                else:
                    chyb += 1
            except Exception as e:
                chyb += 1
                print("  ✗ %s: %s" % (did, e), flush=True)
            if n % 25 == 0:
                el = time.time() - t0
                print("  … %d/%d (%.0f s, %.1f/s)" % (n, len(zbyva), el, n / max(el, 1e-6)),
                      flush=True)
            time.sleep(0.15)          # ať to nezahltí OpenWebUI vedle Hanse
    try: kn.stop()
    except Exception: pass
    print("HOTOVO za %.0f s — %d přepsáno, %d chyb." % (time.time() - t0, ok, chyb))
    return 0


if __name__ == "__main__":
    sys.exit(main())
