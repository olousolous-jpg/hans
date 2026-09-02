# -*- coding: utf-8 -*-
"""Srovnání PRODUKČNÍ cestou: syrový výstup routeru → `_uplatni_pravidla`.

⚠️ Bez tohohle kroku měří `retro_rozbor.py` něco jiného, než Hans dělá:
`report_home_status` má čtyři pravidla, `kodi_play_film` grounding, a
`report_person` fail-safe v běhounu (neznámé jméno → None → běžný hovor).
Řádek „nemá grounding" v Action() tedy NEznamená „bez pojistky".
"""
import json, os, sys, time
sys.path.insert(0, os.path.abspath("."))
from scripts.hans_agent import AgentRouter
from scripts.retro_router_srovnani import _Handler

cfg = json.load(open("config.json"))
r = AgentRouter(cfg); h = _Handler()
d = json.load(open("data/mereni/retro_vysledky.json"))
cache, t0 = {}, time.time()

def pres(aid, veta):
    if not aid:
        return None
    k = (aid, veta)
    if k not in cache:
        try:
            cache[k] = r._uplatni_pravidla(
                aid, veta, {"action": aid, "args": {}, "confidence": 0.95}, h)
        except Exception:
            cache[k] = aid
    return cache[k]

zajimave = [x for x in d if x["stary"] or x["novy"]]
print("radku k prepoctu: %d" % len(zajimave), flush=True)
for i, x in enumerate(zajimave, 1):
    x["stary_p"] = pres(x["stary"], x["veta"])
    x["novy_p"] = pres(x["novy"], x["veta"])
    if i % 50 == 0:
        print("  %d/%d · %.0f s" % (i, len(zajimave), time.time() - t0), flush=True)
for x in d:
    x.setdefault("stary_p", None); x.setdefault("novy_p", None)
json.dump(d, open("data/mereni/retro_produkcni.json", "w"), ensure_ascii=False, indent=1)

n = len(d)
def poc(f): return sum(1 for x in d if f(x))
print("\nPRODUKCNI CESTA, %d vet\n" % n)
print(f"{'':34} {'SYROVE':>12} {'PO PRAVIDLECH':>14}")
for jm, fs, fp in (
    ("shoda celkem", lambda x: x['stary']==x['novy'], lambda x: x['stary_p']==x['novy_p']),
    ("  z toho oba NIC", lambda x: not x['stary'] and not x['novy'],
                         lambda x: not x['stary_p'] and not x['novy_p']),
    ("stary vybral, novy nic", lambda x: x['stary'] and not x['novy'],
                               lambda x: x['stary_p'] and not x['novy_p']),
    ("novy vybral, stary nic", lambda x: x['novy'] and not x['stary'],
                               lambda x: x['novy_p'] and not x['stary_p']),
    ("oba jinou", lambda x: x['stary'] and x['novy'] and x['stary']!=x['novy'],
                  lambda x: x['stary_p'] and x['novy_p'] and x['stary_p']!=x['novy_p'])):
    a, b = poc(fs), poc(fp)
    print(f"{jm:34} {a:5} {100*a/n:5.1f}% {b:7} {100*b/n:5.1f}%")
print("\npravidla potlacila volbu:")
print("  staremu:", poc(lambda x: x['stary'] and not x['stary_p']))
print("  novemu :", poc(lambda x: x['novy'] and not x['novy_p']))
