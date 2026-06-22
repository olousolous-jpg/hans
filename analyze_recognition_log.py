#!/usr/bin/env python3
"""Analýza recognition.log — vyhodnocení prahů (read-only, nic nemění)."""
import re, glob, json, statistics as st
from collections import Counter, defaultdict

import sys
CUTOFF = sys.argv[1] + " " + sys.argv[2] if len(sys.argv) > 2 else ""
# s cutoffem analyzuj jen aktuální log (post-restart data jsou tam)
FILES = ["data/recognition.log"] if CUTOFF else sorted(glob.glob("data/recognition.log*"))

# Známé osoby čteme z config.json (žádná jména natvrdo v kódu).
def _load_known():
    try:
        with open("config.json", encoding="utf-8") as f:
            kp = json.load(f).get("known_persons", {})
        names = set(kp.keys() if isinstance(kp, dict) else kp)
        if names:
            return names
    except Exception:
        pass
    return set()
KNOWN = _load_known()

# EMA řádek: track=.. area=A raw=N:C top1=N:C top2=N:C margin=±M -> N:C (reason) cons=N recent=[..]
RE_EMA = re.compile(
    r"area=(?P<area>[\d.]+) raw=(?P<rn>\S+?):(?P<rc>[\d.]+) "
    r"top1=(?P<t1n>\S+?):(?P<t1c>[\d.]+) top2=(?P<t2n>\S+?):(?P<t2c>[\d.]+) "
    r"margin=(?P<m>[+\-][\d.]+) -> (?P<dn>\S+?):(?P<dc>[\d.]+) \((?P<reason>\w+)\)"
)
# VOTE řádek: arc=N:C cluster=N:C(d=D) -> N:C (reason)
RE_VOTE = re.compile(
    r"arc=(?P<an>\S+?):(?P<ac>[\d.]+) cluster=(?P<cn>\S+?):(?P<cc>[\d.]+)"
    r"\(d=(?P<cd>[\d.]+)(?:,m=(?P<cm>[\d.]+))?\) "
    r"-> (?P<fn>\S+?):(?P<fc>[\d.]+) \((?P<reason>[\w+]+)\)"
)

ema_reason = Counter()
ema_ok_conf = defaultdict(list)       # name -> [top1 conf] u OK rozhodnutí
ema_rej_known = []                    # (name, top1c, margin, reason) raw=known ale zamítnuto
ema_facedb_unknown_while_cons = Counter()  # raw=Unknown ale EMA cons/decision = known
vote_reason = Counter()
vote_arcUnk_clusterNamed = 0
vote_arc_conf_ok = defaultdict(list)
cluster_rescue_margin = defaultdict(list)   # jméno -> [margin] u cluster_only rescue

n_ema = n_vote = 0
for fn in FILES:
    with open(fn, encoding="utf-8", errors="replace") as f:
        for line in f:
            if CUTOFF and line[:19] < CUTOFF:
                continue
            m = RE_EMA.search(line)
            if m:
                n_ema += 1
                d = m.groupdict()
                ema_reason[d["reason"]] += 1
                t1c = float(d["t1c"]); marg = float(d["m"])
                if d["reason"] == "ok" and d["dn"] in KNOWN:
                    ema_ok_conf[d["dn"]].append(t1c)
                # raw je known osoba ale finální EMA = Unknown (near-miss kvůli prahu)
                if d["rn"] in KNOWN and d["dn"] not in KNOWN and d["reason"] != "ok":
                    ema_rej_known.append((d["rn"], t1c, marg, d["reason"]))
                # face_db vrátil Unknown (raw), ale EMA konsenzus drží known
                if d["rn"] == "Unknown" and d["t1n"] in KNOWN and t1c >= 0.3:
                    ema_facedb_unknown_while_cons[d["t1n"]] += 1
                continue
            m = RE_VOTE.search(line)
            if m:
                n_vote += 1
                d = m.groupdict()
                vote_reason[d["reason"]] += 1
                if d["an"] == "Unknown" and d["cn"] in KNOWN:
                    vote_arcUnk_clusterNamed += 1
                if d["fn"] in KNOWN:
                    vote_arc_conf_ok[d["fn"]].append(float(d["ac"]))
                # cluster rescue margin per zachráněné jméno (ladění brány)
                if d["reason"] == "cluster_only" and d.get("cm"):
                    cluster_rescue_margin[d["fn"]].append(float(d["cm"]))

def pct(v, p):
    if not v: return float("nan")
    v = sorted(v); k = (len(v)-1)*p/100; i = int(k)
    return v[i] if i+1>=len(v) else v[i]+(v[i+1]-v[i])*(k-i)

print(f"Soubory: {FILES}")
print(f"EMA řádků: {n_ema}   VOTE řádků: {n_vote}\n")

print("=== EMA decision reasons (async_recognizer práh) ===")
tot = sum(ema_reason.values()) or 1
for r, c in ema_reason.most_common():
    print(f"  {r:14s} {c:8d}  ({100*c/tot:5.1f}%)")

print("\n=== EMA 'ok' top1-conf rozsah per known (operating range) ===")
print(f"  {'name':8s} {'n':>7s} {'p10':>6s} {'p50':>6s} {'p90':>6s}")
for nm, v in sorted(ema_ok_conf.items()):
    print(f"  {nm:8s} {len(v):7d} {pct(v,10):6.2f} {pct(v,50):6.2f} {pct(v,90):6.2f}")

print(f"\n=== Near-miss: raw=known osoba ZAMÍTNUTA EMA prahem (recoverable) ===")
print(f"  celkem: {len(ema_rej_known)}")
nm_c = Counter(x[0] for x in ema_rej_known)
rs_c = Counter(x[3] for x in ema_rej_known)
print("  per osoba:", dict(nm_c))
print("  per důvod:", dict(rs_c))
if ema_rej_known:
    t1s = [x[1] for x in ema_rej_known]; mgs = [x[2] for x in ema_rej_known]
    print(f"  top1 conf u near-miss:  p50={pct(t1s,50):.2f}  p90={pct(t1s,90):.2f}  (ema_thresh=0.30)")
    print(f"  margin u near-miss:     p50={pct(mgs,50):.2f}  p90={pct(mgs,90):.2f}  (ema_margin=0.08)")

print(f"\n=== face_db raw=Unknown, zatímco EMA konsenzus = known (face_db práh ztrácí frame) ===")
print("  ", dict(ema_facedb_unknown_while_cons), " (arcface_thresh=0.42, margin=0.12)")

print("\n=== VOTE fusion reasons ===")
tot=sum(vote_reason.values()) or 1
for r,c in vote_reason.most_common():
    print(f"  {r:14s} {c:8d}  ({100*c/tot:5.1f}%)")
print(f"\n  arc=Unknown ale cluster=known (cluster by mohl zachránit): {vote_arcUnk_clusterNamed}")
print("\n=== VOTE: arc conf když finál=known (per osoba p10/50/90) ===")
for nm,v in sorted(vote_arc_conf_ok.items()):
    print(f"  {nm:8s} n={len(v):7d}  p10={pct(v,10):.2f} p50={pct(v,50):.2f} p90={pct(v,90):.2f}")

print("\n=== CLUSTER RESCUE margin per zachráněné jméno (ladění cluster_rescue_margin) ===")
if any(cluster_rescue_margin.values()):
    for nm, v in sorted(cluster_rescue_margin.items()):
        print(f"  {nm:8s} n={len(v):6d}  margin p10={pct(v,10):.2f} p50={pct(v,50):.2f} p90={pct(v,90):.2f}")
    print("  → pokud falešné jméno (nepřítomná osoba) má NIŽŠÍ margin než pravé,")
    print("    nastav cluster_rescue_margin mezi ně.")
else:
    print("  (žádný m= v logu — data jsou z doby PŘED přidáním margin loggingu)")
