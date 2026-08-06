#!/usr/bin/env python3
"""Měření KVALITY ROZHOVORŮ — before/after pro HANS_THREAD_V1 & spol.

PROČ TENHLE SKRIPT EXISTUJE
---------------------------
6.8.2026 se nasadily vrstvy A (vlákno rozhovoru), B (FTS index rozhovorů),
`HANS_CMD_LLM_ROUTE_V4` (brzdy routingu) a `HANS_STUDY_KNOWN_TOPIC_V1`
(commit edea960). Jestli reálně zabraly, se pozná JEN z provozních dat —
ne z testů. Tenhle skript to změří týmiž metrikami jako baseline.

KDY SPUSTIT
-----------
Až se od 6.8.2026 nasbírá **~100 nových výměn** (běžným používáním, ne
testováním). Při ~50 výměnách denně to jsou zhruba 2–3 dny.

    python3 tools/measure_chat_quality.py

JAK ČÍST VÝSLEDEK
-----------------
Klesly-li obě čísla proti baseline, vrstvy zabraly. Pokud NE, A/B nepomohly
a nemá smysl na nich stavět dál (např. vrstvu C = průběžné pojmenování
tématu) — spíš je vrátit a hledat příčinu jinde.

⚠️ Metriky jsou PROXY, ne pravda:
  • opakovaný dotaz  = uživateli odpověď nesedla a ptá se znovu
  • duplicitní odpověď = Hans vrací tutéž šablonu dokola
Obě mají legitimní výskyt („namaluj kočku" 5× je záměr) — proto se sledují
TRENDY, ne absolutní hodnoty.
"""
import re
import sqlite3
import statistics as stat
import sys
import time
import unicodedata
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data/hans_diary.db"

# ── BASELINE (změřeno 6.8.2026 na 250 výměnách od 22.7., PŘED nasazením) ──
BASE_REPEAT_PCT = 20.0     # dotazů zopakovaných do hodiny
BASE_DUP_PCT = 20.0        # doslova identických odpovědí
# Nejčastější odpovědí za 2 týdny byla abstinenční šablona (15×).
BASE_TOP_ANSWER = "K tomuhle nemám spolehlivý záznam"
# Routing (měřeno zvlášť na 50 reálných větách): 9 štítků / 6 únosů → 0 únosů.

CUTOVER = time.mktime(time.strptime("2026-08-06 09:40", "%Y-%m-%d %H:%M"))


def _fold(s):
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(c for c in s if not unicodedata.combining(c)).lower()


def _norm(s):
    return re.sub(r"[^a-z0-9 ]", "", _fold(s)).strip()


def load(since=None, until=None):
    """Výměny z deníku → [(ts, dotaz, odpověď)]."""
    q = "SELECT ts, note FROM diary WHERE event_type='human_chat'"
    args = []
    if since:
        q += " AND ts >= ?"; args.append(since)
    if until:
        q += " AND ts < ?"; args.append(until)
    conn = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)
    try:
        rows = conn.execute(q + " ORDER BY id", args).fetchall()
    finally:
        conn.close()
    out = []
    for ts, note in rows:
        if not note:
            continue
        m = re.match(r"^(\w+):\s*(.*?)\nHans:\s*(.*)$", note, re.S)
        if m:
            out.append((ts, m.group(2).strip(), m.group(3).strip()))
    return out


def measure(pairs, label):
    if not pairs:
        print(f"\n{label}: žádná data")
        return None
    seen, rep = {}, 0
    for ts, q, _a in pairs:
        k = _norm(q)
        if k in seen and ts - seen[k] < 3600:
            rep += 1
        seen[k] = ts
    ans = Counter(a for _t, _q, a in pairs)
    dup = sum(c - 1 for c in ans.values() if c > 1)
    n = len(pairs)
    rp, dp = 100 * rep / n, 100 * dup / n
    lens = [len(q.split()) for _t, q, _a in pairs]
    print(f"\n── {label}  ({n} výměn)")
    print(f"   opakovaný dotaz do 60 min : {rep:3} ({rp:.0f} %)")
    print(f"   doslovný duplikát odpovědi: {dup:3} ({dp:.0f} %)")
    print(f"   medián délky dotazu       : {stat.median(lens):.0f} slov")
    top = ans.most_common(3)
    print("   nejčastější odpovědi:")
    for a, c in top:
        if c > 1:
            print(f"      {c}× {a[:64]}")
    return rp, dp, ans


def main():
    after = load(since=CUTOVER)
    before = load(until=CUTOVER)[-250:]
    print("=" * 68)
    print("KVALITA ROZHOVORŮ — dopad HANS_THREAD_V1 & spol. (commit edea960)")
    print("=" * 68)
    measure(before, "PŘED nasazením (posledních 250 výměn)")
    res = measure(after, "PO nasazení (od 6.8.2026 09:40)")

    if not res:
        return
    rp, dp, ans = res
    n = len(after)
    print("\n" + "=" * 68)
    if n < 100:
        print(f"⚠️  Zatím jen {n} výměn — na závěr je potřeba ~100. "
              f"Spusť znovu za pár dní.")
        return
    print("VERDIKT (proti baseline 6.8.: opakování 20 %, duplicity 20 %)")
    ok = 0
    for name, val, base in (("opakované dotazy", rp, BASE_REPEAT_PCT),
                            ("duplicitní odpovědi", dp, BASE_DUP_PCT)):
        d = val - base
        mark = "✅ kleslo" if d < -2 else ("➖ beze změny" if d < 2 else "❌ STOUPLO")
        print(f"  {mark:16} {name:22} {base:.0f} % → {val:.0f} % ({d:+.0f})")
        ok += d < -2
    abst = [a for a, _ in ans.most_common(3) if BASE_TOP_ANSWER in a]
    print(f"  {'✅' if not abst else '❌'} abstinenční šablona "
          f"{'už není' if not abst else 'JE POŘÁD'} mezi 3 nejčastějšími")
    print()
    if ok == 2:
        print("→ Vrstvy zabraly. Další krok podle backlogu "
              "(study_note do FTS indexu).")
    elif ok == 0:
        print("→ NEZABRALY. Nestavět na nich dál — vrátit a hledat příčinu "
              "jinde. Vrstvu C (pojmenování tématu) NESTAVĚT.")
    else:
        print("→ Částečný efekt. Podívat se, která metrika drží a proč.")


if __name__ == "__main__":
    sys.exit(main())
