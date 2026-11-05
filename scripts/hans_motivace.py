"""HANS_MOTIVACE_V1 (5. 11. 2026) — odměny za skutečné úspěchy (uživatel 5. 11.).

Hansův model se z odměn neučí (váhy se nemění), takže „motivace“ = úspěchy
mění, co Hans DĚLÁ a co o sobě VÍ. Úspěch vždy počítá KÓD (výsledek partie,
skóre hry, zkouška Koláče), nikdy Hansovo sebehodnocení.

Tři páky:
1. ČAS PODLE POKROKU — činnost, ve které se Hans zlepšuje, dostane víc volného
   času (`vaha()`: 0,5–2×; mění rozestupy šachů a NetHacku a podíl zkoušení
   Koláče). Nic neklesne na nulu.
2. VÝSADY — úspěch dá žeton; žeton Hans použije sám, když ho potřebuje:
   téma studia, které navrhl, zařadí bez čekání na schválení (`pouzij_vysadu`).
3. SÍŇ ÚSPĚCHŮ — milníky (první výhra, nový rekord, nové patro, Koláč si
   zapamatoval) jdou do deníku jako `uspech` s důležitostí 9 → sebe-definující
   vzpomínky, kapitola a Severka.

Vyhodnocení je idempotentní (`motivace_udalosti`, klíč události) a běží ze smyčky
volného času (`vyhodnot`). Vypínač `motivace.enabled`.
"""
from __future__ import annotations

import logging
import sqlite3
import time

_log = logging.getLogger("hans_motivace")


def _cfg(config):
    d = {"enabled": True, "vaha_min": 0.5, "vaha_max": 2.0}
    d.update((config or {}).get("motivace", {}) or {})
    return d


def _db(config):
    c = sqlite3.connect((config or {}).get("diary_db") or "data/hans_diary.db", timeout=10.0)
    c.execute("CREATE TABLE IF NOT EXISTS motivace_udalosti (klic TEXT PRIMARY KEY, ts REAL, "
              "druh TEXT, popis TEXT, zetony INTEGER, sin INTEGER)")
    c.execute("CREATE TABLE IF NOT EXISTS motivace_vysady (id INTEGER PRIMARY KEY, ts REAL, "
              "druh TEXT, popis TEXT)")
    return c


def _tab(c, t) -> bool:
    return bool(c.execute("SELECT 1 FROM sqlite_master WHERE name=?", (t,)).fetchone())


def _udalosti(c) -> list:
    """[(klic, druh, popis, zetony, do_sine)] — všechny úspěchy spočítané z dat."""
    out = []
    if _tab(c, "sachy_partie"):
        rows = c.execute("SELECT id, vysledek, kolac_sila FROM sachy_partie WHERE "
                         "stav='konec' ORDER BY id").fetchall()
        vyher, max_sila = 0, None
        for pid, vys, sila in rows:
            if vys == "výhra":
                vyher += 1
                out.append(("sachy_vyhra_%d" % pid, "šachy",
                            ("Poprvé jsem porazil Koláče v šachu." if vyher == 1 else
                             "Porazil jsem Koláče v šachu (%d. výhra, jeho síla %d %%)."
                             % (vyher, round(100 * (sila or 0)))), 1,
                            vyher in (1, 5, 10, 25, 50)))
            if sila is not None and (max_sila is None or sila > max_sila + 1e-9):
                if max_sila is not None:
                    out.append(("sachy_sila_%d" % round(100 * sila), "šachy",
                                "Koláč proti mně musí hrát silněji než kdy dřív (%d %%)."
                                % round(100 * sila), 0, True))
                max_sila = sila
    if _tab(c, "nethack_hry"):
        # HANS_MOTIVACE_JEN_DOHRANE_V1 — přerušená hra (restart) není úspěch
        # (5. 11.: žeton „rekord 50“ z hry, kterou přerušil restart)
        rows = c.execute("SELECT id, skore, max_hloubka FROM nethack_hry WHERE stav='konec' "
                         "ORDER BY id").fetchall()
        best, hl = None, 1
        for hid, sk, mh in rows:
            sk, mh = int(sk or 0), int(mh or 1)
            if best is not None and sk > best:
                out.append(("nethack_rekord_%d" % hid, "NetHack",
                            "V NetHacku jsem překonal svůj rekord: skóre %d." % sk, 1, True))
            if mh > hl:
                out.append(("nethack_patro_%d" % mh, "NetHack",
                            "V NetHacku jsem se poprvé dostal do %d. patra." % mh, 1, True))
                hl = mh
            best = sk if best is None else max(best, sk)
    if _tab(c, "kolac_ucitel"):
        rows = c.execute("SELECT id, tema FROM kolac_ucitel WHERE kolo=2 AND verdikt='ok' "
                         "ORDER BY id").fetchall()
        for i, (uid, tema) in enumerate(rows, 1):
            out.append(("ucitel_%d" % uid, "učení Koláče",
                        ("Poprvé si Koláč zapamatoval, co jsem ho naučil (%s)." % tema
                         if i == 1 else "Koláč si zapamatoval, co jsem ho naučil (%s)." % tema),
                        1, i in (1, 10, 25)))
    return out


def vyhodnot(config: dict) -> int:
    """Zapíše nové úspěchy (žetony + síň). Vrací počet nových."""
    if not _cfg(config).get("enabled"):
        return 0
    c = _db(config)
    try:
        nove = 0
        for klic, druh, popis, zet, sin in _udalosti(c):
            if c.execute("SELECT 1 FROM motivace_udalosti WHERE klic=?", (klic,)).fetchone():
                continue
            c.execute("INSERT INTO motivace_udalosti VALUES (?,?,?,?,?,?)",
                      (klic, time.time(), druh, popis, zet, 1 if sin else 0))
            if sin:
                try:
                    c.execute("INSERT INTO diary (ts, event_type, title, note, importance) "
                              "VALUES (?,?,?,?,?)", (time.time(), "uspech",
                                                     "Úspěch: %s" % druh, popis, 9))
                except sqlite3.OperationalError:
                    c.execute("INSERT INTO diary (ts, event_type, title, note) VALUES "
                              "(?,?,?,?)", (time.time(), "uspech", "Úspěch: %s" % druh, popis))
            nove += 1
            _log.info("HANS_MOTIVACE_V1: úspěch [%s] %s%s%s", druh, popis,
                      " +%d žeton" % zet if zet else "", " → síň úspěchů" if sin else "")
        c.commit()
        return nove
    finally:
        c.close()


def zetony(config: dict) -> int:
    c = _db(config)
    try:
        z = c.execute("SELECT coalesce(sum(zetony),0) FROM motivace_udalosti").fetchone()[0]
        u = c.execute("SELECT count(*) FROM motivace_vysady").fetchone()[0]
        return int(z) - int(u)
    finally:
        c.close()


def pouzij_vysadu(config: dict, druh: str, popis: str) -> bool:
    """Má-li Hans žeton, použije ho (True) a zapíše to do deníku."""
    if not _cfg(config).get("enabled"):
        return False
    try:
        vyhodnot(config)
        if zetony(config) <= 0:
            return False
        c = _db(config)
        try:
            c.execute("INSERT INTO motivace_vysady (ts, druh, popis) VALUES (?,?,?)",
                      (time.time(), druh, popis))
            c.execute("INSERT INTO diary (ts, event_type, title, note) VALUES (?,?,?,?)",
                      (time.time(), "vysada", "Využil jsem výsadu: %s" % druh,
                       "Za své úspěchy jsem si zasloužil výsadu a použil ji: %s" % popis))
            c.commit()
        finally:
            c.close()
        _log.info("HANS_MOTIVACE_V1: výsada [%s] %s", druh, popis)
        return True
    except Exception as e:
        _log.warning("HANS_MOTIVACE_V1: výsada selhala: %s", e)
        return False


def _trend(hodnoty: list) -> float:
    """Pokrok: průměr posledních 3 proti 3 předchozím, normalizováno (−1..1)."""
    if len(hodnoty) < 4:
        return 0.0
    a = hodnoty[-6:-3] or hodnoty[:-3]
    b = hodnoty[-3:]
    pa, pb = sum(a) / len(a), sum(b) / len(b)
    m = max(abs(pa), abs(pb), 1e-6)
    return max(-1.0, min(1.0, (pb - pa) / m))


def vaha(config: dict, aktivita: str) -> float:
    """Násobek volného času pro činnost podle pokroku (0,5–2×; 1 bez dat)."""
    k = _cfg(config)
    if not k.get("enabled"):
        return 1.0
    try:
        c = _db(config)
        try:
            if aktivita == "sachy" and _tab(c, "sachy_partie"):
                h = [float(r[0] or 0) for r in c.execute(
                    "SELECT kolac_sila FROM sachy_partie WHERE stav='konec' ORDER BY id")]
            elif aktivita == "nethack" and _tab(c, "nethack_hry"):
                h = [float(r[0] or 0) for r in c.execute(
                    "SELECT skore FROM nethack_hry WHERE stav IN ('konec','prerusena') "
                    "ORDER BY id")]
            elif aktivita == "ucitel" and _tab(c, "kolac_ucitel"):
                h = [1.0 if r[0] == "ok" else 0.0 for r in c.execute(
                    "SELECT verdikt FROM kolac_ucitel WHERE kolo=2 ORDER BY id")]
            else:
                h = []
        finally:
            c.close()
    except Exception:
        return 1.0
    t = _trend(h)
    return max(float(k["vaha_min"]), min(float(k["vaha_max"]), 1.0 + t))


def prehled(config: dict) -> str:
    c = _db(config)
    try:
        sin = [r[0] for r in c.execute("SELECT popis FROM motivace_udalosti WHERE sin=1 "
                                       "ORDER BY ts DESC LIMIT 5")]
        vys = c.execute("SELECT count(*) FROM motivace_vysady").fetchone()[0]
    finally:
        c.close()
    z = zetony(config)
    out = ["Žetonů za úspěchy mám %d, výsad jsem využil %d." % (z, vys)]
    if sin:
        out.append("Síň úspěchů: " + " ".join(sin))
    out.append("Víc volného času dostává, v čem se zlepšuji: šachy %.1f×, NetHack %.1f×, "
               "učení Koláče %.1f×." % (vaha(config, "sachy"), vaha(config, "nethack"),
                                        vaha(config, "ucitel")))
    return "\n".join(out)
