#!/usr/bin/env python3
"""Odečet: posunuly se Hansovy malířské aspekty po HANS_ART_COVERED_ASPECTS_V1?

Baseline k 10. 9. 2026 (30denní okno, PŘED nasazením paměti aspektů):
    colour 22 · depth 21 · texture 21 · narrative 21 · light 21 ·
    popředí/pozadí 18 · composition 10

Co se má stát, když mechanismus funguje: `composition` vyroste, `colour`
a `light` poklesnou, poměry se vyrovnají. Když se za měsíc nezmění nic,
mechanismus nestačí — a je zbytečné ho kopírovat na webová díla.

Spuštění:  python3 tools/odecet_art_aspekty.py [dnů_okna]
"""
import sqlite3, sys, os, datetime, collections

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "data/hans_diary.db")
NASAZENO = datetime.datetime(2026, 9, 10, 13, 0)   # commit e374eb3

BASELINE = {"colour & saturation": 22, "depth / atmospheric perspective": 21,
            "texture & detail": 21, "narrative & mood": 21,
            "light & shadow": 21, "foreground / background separation": 18,
            "composition & framing": 10}


def spocti(rows, aspekty):
    poc = collections.Counter()
    for (note,) in rows:
        low = (note or "").lower()
        for a, slova in aspekty.items():
            if any(s in low for s in slova):
                poc[a] += 1
    return poc


def main(dnu=30):
    sys.path.insert(0, ROOT)
    from scripts.hans_art import _ART_ASPEKTY      # jedna pravda o aspektech
    con = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)
    od = (datetime.datetime.now() - datetime.timedelta(days=dnu)).timestamp()
    po_nasazeni = con.execute(
        "SELECT note FROM diary WHERE event_type='art_lesson' AND note!='' "
        "AND ts>=?", (max(od, NASAZENO.timestamp()),)).fetchall()
    okno = con.execute(
        "SELECT note FROM diary WHERE event_type='art_lesson' AND note!='' "
        "AND ts>=?", (od,)).fetchall()
    con.close()

    print("ponaučení za %d dní: %d   z toho PO nasazení paměti: %d"
          % (dnu, len(okno), len(po_nasazeni)))
    if len(po_nasazeni) < 10:
        print("⚠️ Po nasazení je zatím jen %d ponaučení — na závěr je brzy "
              "(Hans maluje ~3 denně, počkej aspoň týden)." % len(po_nasazeni))
    ted = spocti(okno, _ART_ASPEKTY)
    print("\n%-38s %8s %8s %s" % ("aspekt", "baseline", "teď", "posun"))
    for a in sorted(BASELINE, key=lambda x: -BASELINE[x]):
        b, t = BASELINE[a], ted.get(a, 0)
        sipka = "▲" if t > b else ("▼" if t < b else "=")
        print("%-38s %8d %8d   %s %+d" % (a, b, t, sipka, t - b))
    print("\nSleduj hlavně: composition ▲ a colour/light ▼ = mechanismus "
          "zabírá.\nBeze změny = nestačí, NEkopírovat ho na webová díla.")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 30)
