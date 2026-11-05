"""HANS_VYLET_V1 (3. 11. 2026) — páteční návrh výletu na víkend (proaktivně na Matrix).

V pátek dopoledne jednou za týden: předpověď na sobotu a neděli pro domov (souřadnice
z `weather`), 3 místa do `r_km` podle Hansových zájmů (`hans_mista`), která ještě
nenavrhl, a akce v okolí domova, když běží model (`hans_akce`). Vypínač `vylet.enabled`.
🔒 domácí souřadnice ani navržená místa se nelogují; stav (navržená místa, týden) v
`data/vylet_stav.json`.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import re
import time

_log = logging.getLogger(__name__)
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_STAV = os.path.join(_ROOT, "data", "vylet_stav.json")


def _cfg(config: dict) -> dict:
    return (config or {}).get("vylet", {}) or {}


def _stav() -> dict:
    try:
        return json.load(open(_STAV, encoding="utf-8"))
    except Exception:
        return {}


def _uloz(st: dict) -> None:
    try:
        json.dump(st, open(_STAV + ".tmp", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        os.replace(_STAV + ".tmp", _STAV)
    except Exception as e:
        _log.warning("HANS_VYLET_V1: stav: %s", e)


def domov(config: dict):
    w = (config or {}).get("weather", {}) or {}
    try:
        return float(w["lat"]), float(w["lon"])
    except Exception:
        return None


def navrh(config: dict, ted: _dt.datetime = None, s_akcemi: bool = True) -> str:
    """Text návrhu (bez odeslání). Prázdné = nic k návrhu."""
    from scripts import hans_mista as hm
    k = _cfg(config)
    d = domov(config)
    if not d:
        return ""
    ted = ted or _dt.datetime.now()
    so = (ted + _dt.timedelta(days=(5 - ted.weekday()) % 7)).date()
    ne = so + _dt.timedelta(days=1)
    st = _stav()
    uz = set(st.get("navrzeno") or [])
    r_km = float(k.get("r_km", 40))
    try:
        mm = hm.mista(d[0], d[1], r_km, kolik=400)
    except Exception as e:
        _log.warning("HANS_VYLET_V1: místa: %s", str(e)[:100])
        mm = []
    # na výlet: vzdálenost do r_km vadí jen málo (skóre míst v okolí ji trestá /(1+km/6)),
    # zaniklé stavby pryč („zaniklá tvrz“ — není co vidět)
    min_km = float(k.get("min_km", 5))
    for x in mm:
        x["skore_v"] = x["skore"] * (1 + x["km"] / 6) / (1 + x["km"] / r_km)
    vyber = []
    for x in sorted(mm, key=lambda x: -x["skore_v"]):
        if x["titul"] in uz or x["km"] < min_km:
            continue
        if re.search(r"\bzanikl", x.get("veta") or hm._prvni_veta(x["titul"])):
            continue
        vyber.append(x)
        if len(vyber) >= int(k.get("mist", 3)):
            break
    for x in vyber:
        x.setdefault("veta", hm._prvni_veta(x["titul"]))
    if not vyber:
        return ""
    try:
        from scripts.weather_chmu import WeatherCHMU
        poc = WeatherCHMU(d[0], d[1]).get_days_string([so, ne])
    except Exception:
        poc = ""
    radky = ["Návrh na víkend:"]
    if poc:
        radky.append("Počasí: " + poc)
    radky.append("Kam bych se podíval:")
    for i, x in enumerate(vyber, 1):
        radky.append("%d. %s — %s, %s%s" % (i, hm._jmeno(x["titul"]), x["druh"], hm._km(x["km"]),
                                           (". " + x["veta"]) if x.get("veta") else ""))
    if s_akcemi:
        try:
            from scripts import hans_akce as ha
            dni = max(1, (ne - ted.date()).days)
            a = ha.zprava(config, d[0], d[1], dni=dni)
            if a.startswith("Akce v okolí"):
                radky.append(a)
        except Exception as e:
            _log.info("HANS_VYLET_V1: akce: %s", str(e)[:80])
    radky.append("(Pošlete mi polohu na místě a řeknu víc o okolí.)")
    global _posledni_vyber
    _posledni_vyber = [x["titul"] for x in vyber]
    return "\n".join(radky)


_posledni_vyber: list = []


def tick(config: dict, notifier=None, ted: float | None = None) -> str | None:
    """Zavolat z rutiny (á 10 min). V pátek od `hodina` (do +4 h) jednou za týden pošle návrh."""
    k = _cfg(config)
    if not k.get("enabled", True) or not notifier:
        return None
    d = _dt.datetime.fromtimestamp(ted or time.time())
    hod = int(k.get("hodina", 9))
    if d.weekday() != int(k.get("den", 4)) or not (hod <= d.hour < hod + 4):
        return None
    tyden = "%d-%02d" % d.isocalendar()[:2]
    st = _stav()
    if st.get("tyden") == tyden or time.time() - float(st.get("pokus_ts") or 0) < 1800:
        return None
    st["pokus_ts"] = time.time()
    _uloz(st)
    text = navrh(config, d)
    if not text:
        _log.info("HANS_VYLET_V1: nic k návrhu")
        st = _stav(); st["tyden"] = tyden; _uloz(st)
        return None
    try:
        ok = notifier(text) is not False
    except Exception as e:
        ok = False
        _log.warning("HANS_VYLET_V1: odeslání selhalo: %s", e)
    if ok:
        st = _stav(); st["tyden"] = tyden
        st["navrzeno"] = list(dict.fromkeys((st.get("navrzeno") or []) + _posledni_vyber))[-300:]
        # HANS_BEHAVIOUR_PRO_LIDI_V1 — kdy návrh odešel (pro Severku: co dělám pro lidi)
        st["odeslano_ts"] = ((st.get("odeslano_ts") or []) + [time.time()])[-100:]
        _uloz(st)
    _log.info("HANS_VYLET_V1: víkendový návrh %s", "odeslán" if ok else "NEODESLÁN")
    return text if ok else None
