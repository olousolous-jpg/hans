# -*- coding: utf-8 -*-
"""Vlastní shoda STARÉHO routeru: tytéž věty přes něj podruhé.

Bez tohohle čísla nemá „shoda 68 %" měřítko — starý router jede na
temperature 0.1, takže část neshod je jeho vlastní los, ne rozdíl routerů.
Vzorek je STRATIFIKOVANÝ (půl akcí, půl null), protože flakiness zajímá
hlavně tam, kde router něco vybral.
"""
import json, os, random, sys, time
sys.path.insert(0, os.path.abspath("."))
from scripts.hans_agent import AgentRouter
from scripts.ollama_client import game_mode_on
from scripts.retro_router_srovnani import _Handler

Z = "data/mereni/retro_vysledky.json"
V = "data/mereni/retro_stabilita.json"
N = int(os.environ.get("N", "40"))

d = json.load(open(Z))
random.seed(20260902)
s_akci = random.sample([r for r in d if r["stary"]], N)
s_null = random.sample([r for r in d if not r["stary"]], N)
vzorek = s_akci + s_null

cfg = json.load(open("config.json"))
r = AgentRouter(cfg); h = _Handler()
out, t0 = [], time.time()
for i, rec in enumerate(vzorek, 1):
    if game_mode_on():
        print("game/preklad — koncim na %d" % i, flush=True); break
    try:
        dd = r._route(h, rec["jmeno"], rec["veta"])
    except Exception:
        dd = None
    a2 = (dd or {}).get("action")
    k2 = float((dd or {}).get("confidence", 0) or 0)
    if a2 and k2 < r.threshold:
        a2 = None
    out.append({"veta": rec["veta"], "beh1": rec["stary"], "beh2": a2,
                "stejne": rec["stary"] == a2, "mel_akci": bool(rec["stary"])})
    if i % 20 == 0:
        print("  %d/%d · %.0f s" % (i, len(vzorek), time.time() - t0), flush=True)
json.dump(out, open(V, "w"), ensure_ascii=False, indent=1)

def pct(s):
    return 100 * sum(1 for x in s if x["stejne"]) / max(1, len(s))
sa = [x for x in out if x["mel_akci"]]
sn = [x for x in out if not x["mel_akci"]]
print("\nVLASTNI SHODA STAREHO ROUTERU (dva behy tehoz vstupu):")
print("  kde v behu 1 vybral AKCI : %5.1f %%  (n=%d)" % (pct(sa), len(sa)))
print("  kde v behu 1 vybral NIC  : %5.1f %%  (n=%d)" % (pct(sn), len(sn)))
print("  celkem                   : %5.1f %%  (n=%d)" % (pct(out), len(out)))
