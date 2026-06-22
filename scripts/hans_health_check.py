#!/usr/bin/env python3
"""HANS_MORNING_HEALTH_V1 — ranní sebe-kontrola zdraví.

Hans si po probuzení projde noční logy (data/system.log + .1) a rozliší
REÁLNÉ chyby (Traceback / [ERROR] / [CRITICAL]) od BENIGNÍHO šumu
(hlavně 'No route to host' / Ollama výpadky když PC v noci spí).

Čistá logika bez side-efektů — scan vrací souhrn, o náladu/deník/surfacing
se stará volající (hans_idle._morning_health_check).
"""
from __future__ import annotations

import re
from collections import Counter
from datetime import datetime
from pathlib import Path

# Soubory s logem (aktuální + 1 rotace; noční chyby se sem vejdou).
_LOG_FILES = ["data/system.log", "data/system.log.1"]

# Řádek logu: "YYYY-MM-DD HH:MM:SS [LEVEL] name: zpráva"
_TS_FMT = "%Y-%m-%d %H:%M:%S"
_RE_LEVEL_NAME = re.compile(r"\[(ERROR|CRITICAL)\]\s+([\w.]+):")

# BENIGNÍ vzory — i [ERROR] řádek je šum, když obsahuje něco z tohoto.
# Drtivá většina = PC v noci spí → Ollama/knowledge nedostupné (síťové blipy).
_BENIGN = (
    "no route to host",
    "errno 113",
    "max retries exceeded",
    "read timed out",
    "read timeout",
    "failed to establish a new connection",
    "newconnectionerror",
    "connection refused",
    "warmup failed for",
    "ollama connection error",
    "llm nedostupn",          # deferral-safe hlášky (data se neztratí)
    "ollama timeout",
)

# REÁLNOU chybu spustí Traceback marker (i víceřádkový) nebo [ERROR]/[CRITICAL].
_TRACEBACK_MARK = "traceback (most recent call last)"


def _parse_ts(line: str) -> float | None:
    """Vrať unixový čas z prefixu řádku, nebo None (continuation řádek)."""
    if len(line) < 19:
        return None
    try:
        return datetime.strptime(line[:19], _TS_FMT).timestamp()
    except (ValueError, TypeError):
        return None


def _is_benign(low: str) -> bool:
    return any(p in low for p in _BENIGN)


def scan_overnight_errors(since_ts: float,
                          log_files: list[str] | None = None,
                          max_samples: int = 3) -> dict:
    """Projdi logy od `since_ts` do teď a vrať souhrn reálných chyb.

    Návrat: {
      'count':   int,                 # počet reálných chybových řádků
      'modules': dict[str, int],      # logger name -> počet (top)
      'samples': list[str],           # ukázky (oseknuté) reálných chyb
      'benign':  int,                 # kolik benigních [ERROR] se přeskočilo
    }
    """
    files = log_files if log_files is not None else _LOG_FILES
    count = 0
    benign = 0
    modules: Counter = Counter()
    samples: list[str] = []
    # in_window/real status dědí continuation řádky tracebacku (bez ts)
    cur_in_window = False

    for fn in files:
        p = Path(fn)
        if not p.exists():
            continue
        try:
            with p.open(encoding="utf-8", errors="replace") as f:
                for raw in f:
                    line = raw.rstrip("\n")
                    ts = _parse_ts(line)
                    if ts is not None:
                        cur_in_window = ts >= since_ts
                    # (ts is None → continuation, zdědí cur_in_window)
                    if not cur_in_window:
                        continue
                    low = line.lower()

                    is_tb = _TRACEBACK_MARK in low
                    m = _RE_LEVEL_NAME.search(line)
                    if not (is_tb or m):
                        continue
                    if _is_benign(low):
                        if m:
                            benign += 1
                        continue
                    # reálná chyba
                    count += 1
                    if m:
                        modules[m.group(2)] += 1
                    elif is_tb:
                        modules["traceback"] += 1
                    if len(samples) < max_samples:
                        samples.append(line[:240])
        except OSError:
            continue

    return {
        "count": count,
        "modules": dict(modules.most_common(5)),
        "samples": samples,
        "benign": benign,
    }


def intensity_for(count: int) -> float:
    """Závažnost nálezu → intenzita nálady (0..1) pro mood._shift."""
    if count <= 0:
        return 0.0
    if count <= 2:
        return 0.5
    if count <= 5:
        return 0.7
    return 0.85


def summary_sentence(result: dict) -> str:
    """Krátká česká věta o nálezu (pro deník / surfacing)."""
    n = result.get("count", 0)
    if n <= 0:
        return "Ranní kontrola: noční logy čisté, vše v pořádku."
    mods = result.get("modules", {})
    mod_str = ", ".join(sorted(mods)) if mods else "neznámý modul"
    word = "chybu" if n == 1 else ("chyby" if n < 5 else "chyb")
    return (f"Ranní kontrola: našel jsem v nočních logech {n} {word} "
            f"({mod_str}). Něco se mi nezdá v pořádku.")


if __name__ == "__main__":
    # Ruční test: scan za posledních 24 h.
    import time as _t
    res = scan_overnight_errors(_t.time() - 24 * 3600)
    print("scan (24h):", res)
    print("intensity:", intensity_for(res["count"]))
    print("summary:", summary_sentence(res))
