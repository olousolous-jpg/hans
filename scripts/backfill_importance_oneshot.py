"""Jednorázový backfill diary.importance (AUTOBIOGRAPHICAL_IMPORTANCE_V1).
Gentle: dávky po 40, sleep mezi, break na 0 (Ollama down → bezpečně skončí,
neoskórované zůstanou NULL = doženou se nočním hookem). Re-spustitelný.
Spouštět z rootu: python3 -m scripts.backfill_importance_oneshot
"""
import json, time, sys
from scripts.hans_importance import score_unscored

cfg = json.load(open("config.json"))
db = cfg.get("diary_db", "data/hans_diary.db")
er = cfg.get("evening_reflection", {}) or {}
model = str(er.get("model", "jobautomation/OpenEuroLLM-Czech:latest"))
timeout = int(er.get("llm_timeout", 300))

total = 0
batches = 0
for i in range(150):                       # cap 150×40 = 6000
    n = score_unscored(cfg, db, model, timeout, limit=20)
    total += n
    batches += 1
    print(f"[backfill] dávka {i+1}: +{n} (celkem {total})", flush=True)
    if n == 0:
        break
    time.sleep(4)                           # nehladovět živý LLM
print(f"BACKFILL DONE: oskórováno {total} epizod v {batches} dávkách", flush=True)
sys.exit(0)
