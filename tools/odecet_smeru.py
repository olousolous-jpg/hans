#!/usr/bin/env python3
"""Odečet směrů rozvoje z 29.–30. 10. 2026 (termín 12. 11., `data/ODECTY.md`).

Porovná stejně dlouhé okno PŘED nasazením a PO něm. Jen čte (read-only), model
nevolá. `python3 tools/odecet_smeru.py [--od 2026-10-29T16:44]`.
Směry: podněty Koláče (zdroj, výběr Hansem, vzdálenost od koníčků), studium
mimo koníčky, filmy (úspěšnost po osobách, film po filmu), malby (zdroj),
Bluesky, kapitola vyprávění (opakování, události), nové koníčky.
"""
import argparse, datetime, json, os, re, sqlite3, sys, unicodedata
from collections import Counter
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("HANS_LOG_FILE", "data/mereni/zkusebni.log")

ap = argparse.ArgumentParser()
ap.add_argument("--od", default="2026-10-29T16:44")
ap.add_argument("--db", default="data/hans_diary.db")
a = ap.parse_args()
OD = datetime.datetime.fromisoformat(a.od).timestamp()
DO = datetime.datetime.now().timestamp()
PRED = OD - (DO - OD)
c = sqlite3.connect("file:%s?mode=ro" % a.db, uri=True)
q = lambda s, *p: c.execute(s, p).fetchall()
dni = (DO - OD) / 86400


def f(s):
    return "".join(ch for ch in unicodedata.normalize("NFD", (s or "").lower())
                   if unicodedata.category(ch) != "Mn")


def okna(et, extra=""):
    pr = q("SELECT count(*) FROM diary WHERE event_type=? AND ts>=? AND ts<? %s" % extra, et, PRED, OD)[0][0]
    po = q("SELECT count(*) FROM diary WHERE event_type=? AND ts>=? %s" % extra, et, OD)[0][0]
    return pr, po


print("ODEČET SMĚRŮ — PO %.1f dne od %s, srovnání se stejně dlouhým oknem PŘED\n" % (
    dni, datetime.datetime.fromtimestamp(OD).strftime("%-d. %-m. %H:%M")))

# 1) Koláčovy podněty
print("1) KOLÁČOVY PODNĚTY")
for nm, od, do in (("před", PRED, OD), ("po", OD, DO)):
    r = q("SELECT tema, stav FROM kolac_podnety WHERE coalesce(polozeno_ts, ts)>=? AND coalesce(polozeno_ts, ts)<? AND stav='polozeno'", od, do)
    z = Counter("fakta" if (t or "").startswith("fakta") else "zvenku" if (t or "").startswith("zvenku") else "model" for t, _ in r)
    print("   %s: položeno %d — %s" % (nm, len(r), dict(z)))
vyb = q("SELECT data FROM diary WHERE event_type='kolac_podnet' AND ts>=?", OD)
s_proc = [json.loads(d or "{}") for (d,) in vyb]
print("   Hans vybral sám (má zdůvodnění): %d z %d" % (sum(1 for x in s_proc if x.get("proc")), len(s_proc)))
konicky = [f(n) for (n,) in q("SELECT name FROM hobbies WHERE coalesce(status,'')<>'zrusen'")]
studium = [f(t) for (t,) in q("SELECT topic FROM study_program")]
kmeny = {w[:5] for s in konicky + studium for w in re.findall(r"\w{5,}", s)}
for nm, od, do in (("před", PRED, OD), ("po", OD, DO)):
    r = q("SELECT coalesce(nullif(heslo,''), nazev), tema FROM kolac_podnety WHERE stav='polozeno' AND polozeno_ts>=? AND polozeno_ts<?", od, do)
    blizko = sum(1 for h, t in r if {w[:5] for w in re.findall(r"\w{5,}", f(h + " " + (t or "")))} & kmeny)
    print("   %s: podnět souvisí s koníčkem/studiem (kmen) %d z %d" % (nm, blizko, len(r)))
print("   lessons z noční kontroly (omyly): před %d, po %d" % okna("lesson_learned", "AND note LIKE '%nočním ověření%'"))

# 2) Studium mimo koníčky
print("\n2) STUDIUM MIMO KONÍČKY")
for (tp, st, dt) in q("SELECT topic, status, datetime(ts,'unixepoch','localtime') FROM study_topic_proposals WHERE direction_id=-1 ORDER BY id"):
    print("   návrh %s „%s“ → %s" % (dt[:10], tp, st))
for (tp, st, cur, cu) in q("SELECT topic, status, current_index, curriculum FROM study_program WHERE started_ts>=? OR updated_ts>=?", OD, OD):
    try:
        n = len(json.loads(cu or "[]"))
    except Exception:
        n = 0
    print("   program „%s“: %s, %s/%s" % (tp, st, cur, n))

# 3) Filmy
print("\n3) FILMY")
try:
    from scripts import hans_film_volba as fv
    for nm, od, do in (("před", PRED, OD), ("po", OD, DO)):
        S = q("SELECT ts, title, note FROM diary WHERE event_type='film_suggestion' AND ts>=? AND ts<?", od, do)
        A = q("SELECT ts, title FROM diary WHERE event_type='film_suggestion_accepted' AND ts>=? AND ts<?", od, do + 3600)
        po_os = Counter(t for _, t, _ in S)
        pr_os = Counter(t for _, t in A)
        print("   %s: přijato/navrženo %s" % (nm, ", ".join(
            "%s %d/%d" % (o, pr_os.get(o, 0), n) for o, n in po_os.items()) or "žádné návrhy"))
except Exception as e:
    print("   filmy: %s" % e)
print("   film po filmu (autoplay): před %d, po %d" % okna("autoplay_next"))
ap_proc = q("SELECT count(*) FROM diary WHERE event_type='autoplay_next' AND ts>=? AND data LIKE '%proc\": \"_%'", OD)[0][0]
print("   z toho po se zdůvodněním Hanse: %d" % ap_proc)

# 4) Malby
print("\n4) MALBY (zdroj)")
for nm, od, do in (("před", PRED, OD), ("po", OD, DO)):
    r = Counter(json.loads(d or "{}").get("source") or "?" for (d,) in q("SELECT data FROM diary WHERE event_type='artwork' AND ts>=? AND ts<?", od, do))
    print("   %s: %s" % (nm, dict(r)))

# 5) Bluesky
print("\n5) BLUESKY")
print("   přečteno příspěvků: po %d" % okna("bluesky_cteni")[1])
print("   rozhovory s Koláčem o přečteném: %d" % q("SELECT count(*) FROM diary WHERE event_type='teddy_dialog' AND ts>=? AND data LIKE '%bluesky%'", OD)[0][0])

# 6) Kapitola vyprávění
print("\n6) KAPITOLA VYPRÁVĚNÍ")
K = q("SELECT ts, coalesce(data, note) FROM diary WHERE event_type='narrative_chapter' ORDER BY ts DESC LIMIT 4")
def sh(t):   # kořeny delších slov (Kopeckého/Kopeckého smrt → „kopec“)
    return {w[:5] for w in re.findall(r"\w{6,}", f(t))}
for i, (ts, tx) in enumerate(K[:-1]):
    pr = K[i + 1][1]
    shoda = len(sh(tx) & sh(pr)) / max(1, len(sh(tx)))
    print("   %s: %d zn, společné kořeny s předchozí %.0f %%, začátek: %.70s" % (
        datetime.datetime.fromtimestamp(ts).strftime("%-d. %-m."), len(tx), 100 * shoda, " ".join(tx.split())))

# 7) Koníčky
print("\n7) NOVÉ KONÍČKY (first_seen po startu)")
for (n, fs) in q("SELECT name, first_seen FROM hobbies"):
    try:
        t = float(fs) if not str(fs).count("-") else datetime.datetime.fromisoformat(str(fs)[:19]).timestamp()
    except Exception:
        continue
    if t >= OD:
        print("   + %s" % n)
c.close()
