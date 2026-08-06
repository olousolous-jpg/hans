#!/usr/bin/env python3
"""Hlídač: až se nasbírá dost rozhovorů, dej vědět na Matrix.

Spouští se systemd timerem (`hans-chatquality.timer`, 1× denně). Sám nic
neměří dokola — jen se podívá, kolik výměn přibylo od nasazení vrstev
A/B (commit edea960, 6.8.2026), a když je jich dost:

  1. spustí `tools/measure_chat_quality.py`
  2. report uloží do `data/chat_quality_report.txt`
  3. shrnutí zapíše do `data/notify_queue.jsonl` → **Hans ho odešle sám**
     svým Matrix mostem (`HANS_NOTIFY_QUEUE_V1`)
  4. zapíše razítko, aby neotravoval každý den znovu

⚠️ ZÁMĚRNĚ neposílá Matrix zprávu sám: E2E store snese jen jednoho klienta
(`hans_matrix.py:16`), druhá session by poškodila olm klíče. Proto fronta.

Ruční spuštění (vynutí report bez ohledu na počet):
    python3 tools/watch_chat_quality.py --force
"""
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data/hans_diary.db"
QUEUE = ROOT / "data/notify_queue.jsonl"
REPORT = ROOT / "data/chat_quality_report.txt"
STAMP = ROOT / "data/.chat_quality_reported"

# Musí odpovídat CUTOVER v measure_chat_quality.py
CUTOVER = time.mktime(time.strptime("2026-08-06 09:40", "%Y-%m-%d %H:%M"))
NEEDED = 100


def exchanges_since(ts: float) -> int:
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % DB, uri=True, timeout=10)
    except Exception:
        return 0
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM diary WHERE event_type='human_chat' "
            "AND ts >= ?", (ts,)).fetchone()
        return int(row[0] or 0)
    except Exception:
        return 0
    finally:
        conn.close()


def enqueue(text: str) -> None:
    """Zapiš zprávu do fronty — odešle ji Hans (šifrovaně, svým mostem)."""
    QUEUE.parent.mkdir(parents=True, exist_ok=True)
    with open(QUEUE, "a", encoding="utf-8") as f:
        f.write(json.dumps({"text": text, "ts": time.time()},
                           ensure_ascii=False) + "\n")


def main() -> int:
    force = "--force" in sys.argv
    if STAMP.exists() and not force:
        return 0                      # už hlášeno, nechceme otravovat
    n = exchanges_since(CUTOVER)
    if n < NEEDED and not force:
        print(f"zatím {n}/{NEEDED} výměn — mlčím")
        return 0

    out = subprocess.run(
        [sys.executable, str(ROOT / "tools/measure_chat_quality.py")],
        capture_output=True, text=True, timeout=300, cwd=str(ROOT))
    report = (out.stdout or "") + (out.stderr or "")
    REPORT.write_text(report, encoding="utf-8")

    # Do zprávy jen VERDIKT a čísla, ne celý výpis — na telefon se hodí krátce.
    verdict = ""
    for line in report.splitlines():
        if line.startswith("→"):
            verdict = line.strip()
    nums = [l.strip() for l in report.splitlines()
            if re.search(r"(kleslo|STOUPLO|beze změny)", l)]
    msg = ("Pane, nasbíralo se %d rozhovorů od úprav ze 6. srpna — "
           "změřil jsem, jestli pomohly.\n\n%s%s\n\n"
           "Celý rozbor mám v data/chat_quality_report.txt."
           % (n, ("\n".join(nums) + "\n\n") if nums else "",
              verdict or "Verdikt viz report."))
    enqueue(msg)
    STAMP.write_text(str(time.time()), encoding="utf-8")
    print(f"report hotov ({n} výměn), zpráva ve frontě")
    return 0


if __name__ == "__main__":
    sys.exit(main())
