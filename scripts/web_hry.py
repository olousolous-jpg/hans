"""HANS_WEB_HRY_V1 (5. 11. 2026) — web: volný čas a úspěchy (šachy, NetHack,
zkoušení Koláče, síň úspěchů). Jen čtení z hans_diary.db; jeden endpoint
`/api/hry` pro kartu na úvodní straně (templates/index.html).
"""
from __future__ import annotations

import sqlite3
import time

from fastapi import APIRouter

router = APIRouter()
_DB = "data/hans_diary.db"


def _q(c, sql, args=()):
    try:
        return c.execute(sql, args).fetchall()
    except sqlite3.OperationalError:
        return []


def _datum(ts) -> str:
    return time.strftime("%-d. %-m. %H:%M", time.localtime(float(ts or 0)))


@router.get("/api/hry")
def api_hry():
    c = sqlite3.connect("file:%s?mode=ro" % _DB, uri=True, timeout=3.0)
    try:
        sachy = _q(c, "SELECT konec_ts, vysledek, kolac_sila, length(tahy) FROM sachy_partie "
                      "WHERE stav='konec' ORDER BY id DESC LIMIT 6")
        sbil = dict(_q(c, "SELECT vysledek, count(*) FROM sachy_partie WHERE stav='konec' "
                          "GROUP BY 1"))
        sila = _q(c, "SELECT hodnota FROM sachy_stav WHERE klic='kolac_sila'")
        nh = _q(c, "SELECT konec_ts, skore, max_hloubka, duvod, kroky FROM nethack_hry "
                   "WHERE stav IN ('konec','prerusena') ORDER BY id DESC LIMIT 6")
        nh_bezi = _q(c, "SELECT skore, max_hloubka, kroky FROM nethack_hry WHERE stav='bezi' "
                        "ORDER BY id DESC LIMIT 1")
        nh_best = _q(c, "SELECT max(skore), max(max_hloubka), count(*) FROM nethack_hry "
                        "WHERE stav IN ('konec','prerusena')")
        uc1 = _q(c, "SELECT count(*), coalesce(sum(verdikt='ok'),0) FROM kolac_ucitel WHERE kolo=1")
        uc2 = _q(c, "SELECT count(*), coalesce(sum(verdikt='ok'),0) FROM kolac_ucitel WHERE kolo=2")
        uc = _q(c, "SELECT ts, tema, verdikt, kolo FROM kolac_ucitel ORDER BY id DESC LIMIT 5")
        sin = _q(c, "SELECT ts, popis FROM motivace_udalosti WHERE sin=1 ORDER BY ts DESC LIMIT 8")
        zet = _q(c, "SELECT coalesce(sum(zetony),0) FROM motivace_udalosti")
        vys = _q(c, "SELECT ts, popis FROM motivace_vysady ORDER BY ts DESC LIMIT 5")
    finally:
        c.close()
    try:
        from scripts import config_io
        from scripts.hans_motivace import vaha
        cfg = config_io.load()
        vahy = {k: round(vaha(cfg, k), 2) for k in ("sachy", "nethack", "ucitel")}
    except Exception:
        vahy = {}
    zetony = int(zet[0][0]) - len(vys) if zet else 0
    return {
        "sachy": {
            "vyhry": sbil.get("výhra", 0), "remizy": sbil.get("remíza", 0),
            "prohry": sbil.get("prohra", 0),
            "kolac_sila": round(float(sila[0][0]), 2) if sila else None,
            "partie": [{"kdy": _datum(t), "vysledek": v, "sila": round(float(s or 0), 2),
                        "tahu": ((n or 0) // 5 + 1) // 2} for t, v, s, n in sachy],
        },
        "nethack": {
            "her": nh_best[0][2] if nh_best else 0,
            "nejlepsi": (nh_best[0][0] or 0) if nh_best else 0,
            "patro": (nh_best[0][1] or 0) if nh_best else 0,
            "bezi": ({"skore": nh_bezi[0][0] or 0, "patro": nh_bezi[0][1] or 1,
                      "rozhodnuti": nh_bezi[0][2] or 0} if nh_bezi else None),
            "hry": [{"kdy": _datum(t), "skore": s or 0, "patro": p or 1, "konec": d or "",
                     "rozhodnuti": k or 0} for t, s, p, d, k in nh],
        },
        "ucitel": {
            "zkouseno": uc1[0][0] if uc1 else 0, "vedel": uc1[0][1] if uc1 else 0,
            "opakovano": uc2[0][0] if uc2 else 0, "zapamatoval": uc2[0][1] if uc2 else 0,
            "posledni": [{"kdy": _datum(t), "tema": te, "verdikt": v, "kolo": k}
                         for t, te, v, k in uc],
        },
        "motivace": {
            "zetony": zetony, "vahy": vahy,
            "sin": [{"kdy": _datum(t), "text": p} for t, p in sin],
            "vysady": [{"kdy": _datum(t), "text": p} for t, p in vys],
        },
    }
