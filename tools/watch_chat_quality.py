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


def _numbers(report: str, section: str):
    """Vytáhne (opakované %, duplicity %) z dané sekce reportu."""
    part = report.split(section, 1)
    if len(part) < 2:
        return None, None
    block = part[1].split("──", 1)[0]
    rep = re.search(r"opakovan\S* dotaz[^:]*:\s*\d+\s*\((\d+)\s*%\)", block)
    dup = re.search(r"duplik\S*[^:]*:\s*\d+\s*\((\d+)\s*%\)", block)
    return (rep.group(1) if rep else None), (dup.group(1) if dup else None)


def _compose(report: str, n: int) -> str:
    """Zpráva na telefon: VŽDY nese čísla, i když je dat málo.

    Dřív se posílalo jen „Verdikt viz report" — measure skript totiž při
    < 100 výměnách skončí dřív, než verdikt vypíše, takže zpráva byla
    k ničemu (doloženo při ostrém testu 6.8.).
    """
    b_rep, b_dup = _numbers(report, "PŘED nasazením")
    a_rep, a_dup = _numbers(report, "PO nasazení")
    lines = []
    if a_rep and b_rep:
        lines.append("opakované dotazy: %s %% → %s %%" % (b_rep, a_rep))
    if a_dup and b_dup:
        lines.append("duplicitní odpovědi: %s %% → %s %%" % (b_dup, a_dup))
    verdict = next((l.strip() for l in report.splitlines()
                    if l.startswith("→")), "")
    if not verdict:
        verdict = ("Vzorek je zatím malý (%d z ~100), takže to ber jako "
                   "průběžný stav, ne závěr." % n)
    return ("Pane, od úprav ze 6. srpna se nasbíralo %d rozhovorů — "
            "změřil jsem, jestli pomohly.\n\n%s\n\n%s\n\n"
            "Celý rozbor je v data/chat_quality_report.txt."
            % (n, "\n".join(lines) if lines else "(čísla viz report)",
               verdict))


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

    msg = _compose(report, n)
    enqueue(msg)
    STAMP.write_text(str(time.time()), encoding="utf-8")
    print(f"report hotov ({n} výměn), zpráva ve frontě")
    return 0


if __name__ == "__main__":
    sys.exit(main())
