"""HANS_UCI_KOLACE_V1 (5. 11. 2026) — Hans zkouší Koláče z toho, co se sám naučil.

Obrácená Koláčova zkouška: dosud se ptal jen Koláč a Hans byl zkoušený. Tady se
ptá Hans (otázka ze zásoby faktů `kolac_otazky`, jen ty, na které Hans sám
odpověděl správně), Koláč odpoví z hlavy, KÓD soudí podle klíče (`kolac_fakta.soud`)
a když Koláč neví, Hans mu to vysvětlí. Vysvětlení si Koláč pamatuje
(`kolac_ucitel.vysvetleni`) a po odstupu se ho Hans zeptá znovu (kolo 2) —
měří se, jestli si Koláč z Hansova vysvětlení zapamatoval správný údaj.

Zásoba `kolac_otazky` se tu jen ČTE (stav Hansových zkoušek se nemění).
Zápis do deníku `hans_zkousi` (vlastní typ, ne `teddy_dialog`). Vypínač
`kolac_exam.hans_zkousi_podil` (0 = vypnuto).
"""
from __future__ import annotations

import logging
import sqlite3
import time

_log = logging.getLogger("kolac_ucitel")
_DEN = 86400


def _conn(db: str) -> sqlite3.Connection:
    c = sqlite3.connect(db, timeout=10.0)
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE IF NOT EXISTS kolac_ucitel (id INTEGER PRIMARY KEY, ts REAL, "
              "otazka_id INTEGER, tema TEXT, otazka TEXT, klic TEXT, odpoved TEXT, "
              "verdikt TEXT, vysvetleni TEXT, vysvetleni_ok INTEGER, kolo INTEGER, "
              "puvodni INTEGER, opakovano INTEGER DEFAULT 0)")
    return c


def vyber(config: dict, db: str):
    """Nejdřív opakování (kolo 2) toho, co Koláč nevěděl a Hans vysvětlil, po
    odstupu; jinak nová otázka, na kterou Hans sám odpověděl správně."""
    dni = float(((config or {}).get("kolac_exam", {}) or {}).get("hans_zkousi_opakovat_dni", 1))
    c = _conn(db)
    try:
        r = c.execute("SELECT * FROM kolac_ucitel WHERE kolo=1 AND verdikt!='ok' AND "
                      "coalesce(vysvetleni,'')!='' AND opakovano=0 AND ts<? ORDER BY ts "
                      "LIMIT 1", (time.time() - dni * _DEN,)).fetchone()
        if r:
            return {"otazka_id": r["otazka_id"], "tema": r["tema"], "otazka": r["otazka"],
                    "klic": r["klic"], "veta": "", "kolo": 2, "puvodni": r["id"],
                    "pamet": r["vysvetleni"]}
        q = c.execute("SELECT * FROM kolac_otazky WHERE stav='ok' AND posledni_verdikt='ok' "
                      "AND id NOT IN (SELECT otazka_id FROM kolac_ucitel) "
                      "ORDER BY RANDOM() LIMIT 1").fetchone()
    except sqlite3.OperationalError as e:
        _log.debug("HANS_UCI_KOLACE_V1: zásoba: %s", e)
        return None
    finally:
        c.close()
    if not q:
        return None
    return {"otazka_id": q["id"], "tema": q["clanek"] or q["tema"] or "",
            "otazka": q["otazka"], "klic": q["klic"], "veta": q["veta"] or "",
            "kolo": 1, "puvodni": None, "pamet": ""}


def soud(odpoved: str, klic: str, otazka: str) -> str:
    from scripts import kolac_fakta as kf
    return kf.soud(odpoved or "", klic or "", otazka or "")


def zapis(db: str, p: dict, odpoved: str, verdikt: str, vysvetleni: str,
          vysvetleni_ok) -> int:
    c = _conn(db)
    try:
        cur = c.execute("INSERT INTO kolac_ucitel (ts, otazka_id, tema, otazka, klic, odpoved, "
                        "verdikt, vysvetleni, vysvetleni_ok, kolo, puvodni) VALUES "
                        "(?,?,?,?,?,?,?,?,?,?,?)",
                        (time.time(), p["otazka_id"], p["tema"], p["otazka"], p["klic"],
                         odpoved, verdikt, vysvetleni, vysvetleni_ok, p["kolo"], p["puvodni"]))
        if p.get("puvodni"):
            c.execute("UPDATE kolac_ucitel SET opakovano=1 WHERE id=?", (p["puvodni"],))
        c.commit()
        return cur.lastrowid
    finally:
        c.close()


def bilance(db: str, since: float = 0.0) -> dict:
    """Pro Severku a přehled: kolikrát zkoušel, co Koláč věděl, co si zapamatoval."""
    c = _conn(db)
    try:
        r1 = c.execute("SELECT count(*), sum(verdikt='ok') FROM kolac_ucitel WHERE kolo=1 "
                       "AND ts>=?", (since,)).fetchone()
        r2 = c.execute("SELECT count(*), sum(verdikt='ok') FROM kolac_ucitel WHERE kolo=2 "
                       "AND ts>=?", (since,)).fetchone()
    finally:
        c.close()
    return {"zkouseno": r1[0] or 0, "vedel": r1[1] or 0,
            "opakovano": r2[0] or 0, "zapamatoval": r2[1] or 0}
