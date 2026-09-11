#!/usr/bin/env python3
"""Odečet: posunuly se Hansovy malířské aspekty po HANS_ART_COVERED_ASPECTS_V1?

Co se má stát, když mechanismus funguje: `composition` vyroste, `colour`
a `light` poklesnou, poměry se vyrovnají. Když se nezmění nic, mechanismus
nestačí — a je zbytečné ho kopírovat na webová díla.

⚠️ OPRAVENO 11. 9. — první verze srovnávala zapsaný baseline (30denní okno
k 10. 9.) s AKTUÁLNÍM 30denním oknem. Jenže ta dvě okna se z velké části
PŘEKRÝVAJÍ a okno navíc roste s počtem obrazů, takže „posun" vycházel
u všech sedmi aspektů nahoru a neříkal nic. Správně se srovnávají PODÍLY
ve dvou NEPŘEKRÝVAJÍCÍCH se skupinách (před × po nasazení) a k tomu
Fisherův test — u malých čísel se jinak čte šum jako trend.

Spuštění:  python3 tools/odecet_art_aspekty.py [dnů_okna_před]
"""
import sqlite3, sys, os, math, datetime, collections

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "data/hans_diary.db")
NASAZENO = datetime.datetime(2026, 9, 10, 13, 0)   # commit e374eb3


def _lf(n):
    return math.lgamma(n + 1)


def _p(a, b, c, d):
    return math.exp(_lf(a + b) + _lf(c + d) + _lf(a + c) + _lf(b + d)
                    - _lf(a) - _lf(b) - _lf(c) - _lf(d) - _lf(a + b + c + d))


def fisher(a, b, c, d):
    """Oboustranný Fisherův exaktní test na tabulce 2×2."""
    p0 = _p(a, b, c, d)
    tot = 0.0
    for i in range(0, a + b + 1):
        j, k = a + b - i, a + c - i
        l = c + d - k
        if k < 0 or l < 0:
            continue
        pi = _p(i, j, k, l)
        if pi <= p0 + 1e-12:
            tot += pi
    return min(tot, 1.0)


def _pocty(rows, aspekty):
    c = collections.Counter()
    for (note,) in rows:
        low = (note or "").lower()
        for a, slova in aspekty.items():
            if any(s in low for s in slova):
                c[a] += 1
    return c


def main(dnu=30):
    sys.path.insert(0, ROOT)
    from scripts.hans_art import _ART_ASPEKTY      # jedna pravda o aspektech
    con = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)
    nas = NASAZENO.timestamp()
    dotaz = ("SELECT note FROM diary WHERE event_type='art_lesson' "
             "AND note!='' AND ts>=? AND ts<?")
    pred = con.execute(dotaz, (nas - dnu * 86400, nas)).fetchall()
    po = con.execute(dotaz, (nas, 9e18)).fetchall()
    con.close()
    if not pred or not po:
        print("Málo dat: před %d, po %d" % (len(pred), len(po)))
        return
    cp, cpo = _pocty(pred, _ART_ASPEKTY), _pocty(po, _ART_ASPEKTY)
    print("ponaučení PŘED nasazením (%d dní): %d" % (dnu, len(pred)))
    print("ponaučení PO  nasazení           : %d" % len(po))
    print("aspektů na ponaučení: před %.2f · po %.2f  (má zůstat stejné — "
          "jde o REDISTRIBUCI, ne o víc aspektů)"
          % (sum(cp.values()) / len(pred), sum(cpo.values()) / len(po)))
    print("\n%-38s %11s %11s %8s %s"
          % ("aspekt", "před", "po", "posun", "Fisher p"))
    for a in sorted(_ART_ASPEKTY, key=lambda x: -cp.get(x, 0)):
        A, C = cp.get(a, 0), cpo.get(a, 0)
        pb, pa = 100.0 * A / len(pred), 100.0 * C / len(po)
        pv = fisher(A, len(pred) - A, C, len(po) - C)
        znak = "▲" if pa - pb > 3 else ("▼" if pa - pb < -3 else "=")
        hvez = " *" if pv < 0.05 else ("  ~" if pv < 0.15 else "")
        print("%-38s %3d %5.0f%% %3d %5.0f%% %s %+5.0f pb %6.3f%s"
              % (a, A, pb, C, pa, znak, pa - pb, pv, hvez))
    print("\n* = p < 0.05 (průkazné)   ~ = na hraně")
    print("Čeká se: composition ▲ a colour ▼. Bez hvězdičky je to zatím "
          "jen směr, ne důkaz — dokud jich nepřibude, netvrdit, že to zabírá.")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 30)
