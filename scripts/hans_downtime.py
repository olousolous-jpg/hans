"""
HANS_DOWNTIME_V1 — vědomí vlastního výpadku.

Když byl Hans delší dobu mimo provoz (vypnutý den, výpadek proudu, dlouhý
pád služby), při startu si toho VŠIMNE: spočítá mezeru od poslední reálné
aktivity (MAX(ts) v deníku) a pokud přesáhne práh, vrátí nález. Volající
(hans_idle) ho uloží do deníku jako `downtime_noticed`, jemně zabarví náladu
a u příchozí osoby se zmíní + zeptá, co se dělo.

Čistě deterministické — žádný LLM. Pure funkce kvůli testovatelnosti;
SQL dotaz dělá volající přes svoje připojení.
"""
from __future__ import annotations

import datetime as _dt
from typing import Optional


def analyze(last_alive_ts: Optional[float], now_ts: float,
            min_gap_hours: float = 14.0) -> Optional[dict]:
    """Pure: z poslední aktivity a 'teď' spočítá výpadek.

    Vrací {gap_hours, last_alive_ts, now_ts} když mezera > práh, jinak None.
    """
    if not last_alive_ts or last_alive_ts <= 0:
        return None
    gap_h = (now_ts - last_alive_ts) / 3600.0
    if gap_h < float(min_gap_hours):
        return None
    return {
        "gap_hours": round(gap_h, 1),
        "last_alive_ts": float(last_alive_ts),
        "now_ts": float(now_ts),
    }


def _duration_phrase(gap_h: float) -> str:
    if gap_h >= 44:
        return "skoro dva dny"
    if gap_h >= 30:
        return "víc než den"
    if gap_h >= 20:
        return "skoro celý den"
    return "zhruba %d hodin" % int(round(gap_h))


def downtime_sentence(info: dict) -> str:
    """Hans-voiced fakt o výpadku (1. osoba), groundovaný v čase."""
    gap_h = info.get("gap_hours", 0)
    last = _dt.datetime.fromtimestamp(info.get("last_alive_ts", 0))
    dur = _duration_phrase(gap_h)
    # %-d/%-m bez vedoucí nuly (Linux strftime)
    try:
        stamp = last.strftime("%-d.%-m. %H:%M")
    except ValueError:
        stamp = last.strftime("%d.%m. %H:%M")
    return ("Vypadá to, že jsem byl %s mimo provoz — naposledy jsem byl "
            "při vědomí %s." % (dur, stamp))
