#!/usr/bin/env python3
"""Změř LLM routing (`resolve_command_llm`) na REÁLNÝCH větách z deníku.

BASELINE 6.8.2026 (50 vět, PŘED `HANS_CMD_LLM_ROUTE_V4`):
    9 vybraných štítků / **6 únosů** / 3 správně
PO nasazení (týchž 50 vět):
    7 štítků / **0 únosů** / 3 správně  — zbylé `rozhovory` řeší A4 uvnitř

Únosy padaly VŠECHNY do dvou štítků: `rozhovory` a `nitky`.
Konkrétní štítky (zdravi, kalendar, seznam, rozvrh) nechybovaly.

⚠️ Volá LLM na každou větu (~1,4 s) → 50 vět ≈ 75 s. Vyžaduje běžící PC
(mozek). Výstup se posuzuje RUČNĚ — skript neví, co je únos.

    python3 tools/measure_cmd_routing.py [počet_vět]


Bere věty, které NEJSOU slash a které `parse_command` MINE — tedy přesně
ty, o kterých rozhoduje `resolve_command_llm`.
"""
import json
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.chat_commands import parse_command, resolve_command_llm  # noqa

cfg = json.load(open(Path(__file__).resolve().parents[1] / "config.json"))
db = sqlite3.connect(Path(__file__).resolve().parents[1] / "data/hans_diary.db")
rows = db.execute(
    "SELECT ts, note FROM diary WHERE event_type='human_chat' "
    "AND ts > strftime('%s','2026-07-20') ORDER BY id").fetchall()

seen, cases = set(), []
for ts, note in rows:
    if not note:
        continue
    q = note.split("\nHans:")[0]
    q = q.split(":", 1)[1].strip() if ":" in q else q.strip()
    if not q or q.startswith("/") or q in seen:
        continue
    if len(q.split()) > 14:
        continue
    if parse_command(q):          # regexy to chytnou samy → router neřeší
        continue
    seen.add(q)
    cases.append(q)

limit = int(sys.argv[1]) if len(sys.argv) > 1 else 60
cases = cases[-limit:]
print(f"měřím {len(cases)} reálných vět (regexy je minuly)\n")

hits, t0 = [], time.time()
for i, q in enumerate(cases, 1):
    s = time.time()
    try:
        r = resolve_command_llm(q, cfg)
    except Exception as e:
        r = ("CHYBA:%s" % e, "")
    dt = time.time() - s
    label = r[0] if r else "—"
    if r:
        hits.append((q, label))
    print(f"{i:3}. [{dt:4.1f}s] {label:12} | {q[:60]}")

print(f"\n{'='*70}")
print(f"celkem {len(cases)} vět, {time.time()-t0:.0f}s")
print(f"router vybral štítek u {len(hits)} ({100*len(hits)/max(1,len(cases)):.0f} %)")
print("\nVYBRANÉ ŠTÍTKY (posuď ručně, co je únos):")
for q, l in hits:
    print(f"  /{l:11} ← {q[:64]}")
