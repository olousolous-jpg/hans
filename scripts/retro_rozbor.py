# -*- coding: utf-8 -*-
"""Rozbor výsledků retro srovnání. Read-only, jen počítá."""
import json, os, sys
from collections import Counter
C = "data/mereni/retro_vysledky.json"
d = json.load(open(C))
n = len(d)
oba_null  = [r for r in d if r["stary"] is None and r["novy"] is None]
oba_stej  = [r for r in d if r["stary"] and r["stary"] == r["novy"]]
jen_novy  = [r for r in d if r["stary"] is None and r["novy"]]
jen_stary = [r for r in d if r["stary"] and r["novy"] is None]
jina      = [r for r in d if r["stary"] and r["novy"] and r["stary"] != r["novy"]]
print(f"VZOREK: {n} reálných vět\n")
def rad(jm, s, pozn=""):
    print(f"  {jm:34} {len(s):4}  {100*len(s)/max(1,n):5.1f} %  {pozn}")
rad("shoda — oba NIC", oba_null, "(konverzační věta, správně)")
rad("shoda — táž akce", oba_stej)
print(f"  {'SHODA CELKEM':34} {len(oba_null)+len(oba_stej):4}  {100*(len(oba_null)+len(oba_stej))/max(1,n):5.1f} %")
print()
rad("nový vybral, starý NIC", jen_novy, "← NEsoudit bez štítků")
rad("starý vybral, nový NIC", jen_stary, "← NEsoudit bez štítků")
rad("oba vybrali, ale JINOU", jina)
print("\nNEJČASTĚJŠÍ 'nový vybral, starý nic':")
for a, k in Counter(r["novy"] for r in jen_novy).most_common(8):
    print(f"  {k:4}× {a}")
print("\nNEJČASTĚJŠÍ 'starý vybral, nový nic':")
for a, k in Counter(r["stary"] for r in jen_stary).most_common(8):
    print(f"  {k:4}× {a}")
print("\nZÁMĚNY (starý → nový):")
for (a, b), k in Counter((r["stary"], r["novy"]) for r in jina).most_common(8):
    print(f"  {k:4}× {a} → {b}")
if len(sys.argv) > 1 and sys.argv[1] == "-v":
    print("\n── vzorky 'nový vybral, starý nic' ──")
    for r in jen_novy[:20]: print(f"  {r['novy']:24} | {r['veta'][:60]}")
    print("\n── vzorky 'starý vybral, nový nic' ──")
    for r in jen_stary[:20]: print(f"  {r['stary']:24} | {r['veta'][:60]}")
