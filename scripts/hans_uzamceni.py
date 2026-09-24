#!/usr/bin/env python3
"""HANS_LOCK_AUDIT_V1 (24. 9.) — týdenní měření „uzamčení“ (self-locking).

Inspirace: AutoPersonas (arXiv 2607.08252) — persona může psát pořád nový
text a přitom se funkčně točit v kruhu. Článek měří opakování témat
v klouzavém okně proti celé předchozí historii, podíl pěti nejčastějších
témat a počet nových témat za blok. Tady totéž nad Hansovým deníkem, plus
dvě obsahové míry, protože retro měření 24. 9. ukázalo, že Hans NENÍ
uzamčený v tématech (opakování 22–36 %, článek výchozí 61,8 %), ale v tom,
JAK o nich mluví:
  - ozvěna persony: „detail / preciznost / pečlivost“ v textu (známá rohatka
    postojů, viz CLAUDE.md „POSTOJE JSOU OZVĚNA“), 15–40 % replik týdně;
  - gravitace hradů: repliky Koláčovi o hradech v dialogu s JINÝM tématem
    (téma „fotbal“, řeč o Karlštejnu), 7–43 % týdně.

⛔ Výsledek je pro lidi (`/uzamceni`), NE pro Hanse: nepatří do `/vhledy`
ani do Severčina promptu — článek varuje, že diagnostika vrácená do
generování se stane dalším zdrojem téže gravitace.

Jen čte deník (read-only), zapisuje řádek do data/mereni/uzamceni.jsonl.
Idempotentní na ISO týden: první noc nového týdne zapíše týden předchozí.
"""
from __future__ import annotations

import collections
import json
import logging
import os
import re
import sqlite3
import time
from datetime import datetime, timedelta

_log = logging.getLogger("hans_uzamceni")

OUT = "data/mereni/uzamceni.jsonl"
_ECHO = re.compile(r"detail|preciz|pe[čc]liv", re.I)
_HRADY = re.compile(r"hrad|karl[sš]tejn|architekt|zámk|zámek", re.I)
_TEMA = re.compile(r"Téma:\s*(.+?)(?:\s{2,}|\s+Hans:|$)")


def _dny(ts: float):
    return datetime.fromtimestamp(ts).date()


def _temata(rows):
    """[(ts, štítek)] → {den: set(štítků)} (štítek jednou za den)."""
    d = collections.defaultdict(set)
    for ts, lab in rows:
        lab = " ".join(str(lab or "").lower().split())
        if lab and lab != "?":
            d[_dny(ts)].add(lab)
    return d


def _miry_temat(po_dnech, konec, okno=5, blok=10) -> dict:
    """Opakování v okně `okno` dní proti všemu dřív + podíl top-5 + nová
    témata za posledních `blok` dní. Stejné definice jako v článku."""
    vse = collections.Counter()
    for dn, s in po_dnech.items():
        for t in s:
            vse[t] += 1
    win = [t for dn, s in po_dnech.items() if 0 <= (konec - dn).days < okno for t in s]
    driv = {t for dn, s in po_dnech.items() if (konec - dn).days >= okno for t in s}
    blk = {t for dn, s in po_dnech.items() if 0 <= (konec - dn).days < blok for t in s}
    pred_blk = {t for dn, s in po_dnech.items() if (konec - dn).days >= blok for t in s}
    celk = sum(vse.values()) or 1
    return {
        "opakovani_pct": round(100 * sum(t in driv for t in win) / len(win), 1) if win else None,
        "top5_pct": round(100 * sum(c for _, c in vse.most_common(5)) / celk, 1),
        "nova_za_10_dni": len(blk - pred_blk),
        "top3": [t for t, _ in vse.most_common(3)],
    }


def measure(db_path: str, konec=None, historie_dni: int = 60) -> dict:
    """Změří týden končící dnem `konec` (default včera)."""
    konec = konec or (datetime.now().date() - timedelta(days=1))
    od = datetime.combine(konec - timedelta(days=historie_dni), datetime.min.time()).timestamp()
    do = datetime.combine(konec + timedelta(days=1), datetime.min.time()).timestamp()
    tyden_od = datetime.combine(konec - timedelta(days=6), datetime.min.time()).timestamp()
    con = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True, timeout=5.0)
    try:
        q = lambda et, col: con.execute(
            "SELECT ts, %s FROM diary WHERE event_type=? AND ts>=? AND ts<? "
            "ORDER BY ts" % col, (et, od, do)).fetchall()
        dialog = q("teddy_dialog", "note")
        cteni = q("web_read", "title")
        obrazy = q("artwork", "title")
        textove = {et: q(et, "COALESCE(NULLIF(data,''), note, '')")
                   for et in ("dialog_reflection", "introspection")}
    finally:
        con.close()

    out = {"tyden_do": konec.isoformat(), "ts": time.time()}
    # témata dialogů ze štítku „Téma: X“
    dl = []
    for ts, n in dialog:
        m = _TEMA.search(n or "")
        dl.append((ts, m.group(1) if m else ""))
    out["dialog_temata"] = _miry_temat(_temata(dl), konec)
    out["cteni_tituly"] = _miry_temat(_temata(cteni), konec)
    out["obrazy_tituly"] = _miry_temat(_temata(obrazy), konec)

    # obsah za posledních 7 dní
    def _podil(rows, rx, jen_hans=False, bez_tematu_hrady=False):
        n = k = 0
        for ts, t in rows:
            if ts < tyden_od:
                continue
            t = t or ""
            if bez_tematu_hrady:
                m = _TEMA.search(t)
                if m and _HRADY.search(m.group(1)):
                    continue
            if jen_hans:
                t = t.split("Hans:", 1)[-1]
            n += 1
            k += bool(rx.search(t))
        return {"n": n, "pct": round(100 * k / n, 1) if n else None}

    out["ozvena_persony"] = {
        "dialog": _podil(dialog, _ECHO, jen_hans=True),
        **{et: _podil(r, _ECHO) for et, r in textove.items()},
    }
    out["hrady_v_jinem_tematu"] = _podil(dialog, _HRADY, jen_hans=True,
                                         bez_tematu_hrady=True)
    return out


def _zapsane(path: str) -> set:
    try:
        with open(path, encoding="utf-8") as f:
            return {json.loads(l).get("tyden_do") for l in f if l.strip()}
    except FileNotFoundError:
        return set()
    except Exception as e:
        _log.warning("uzamceni: čtení %s selhalo: %s", path, e)
        return set()


def record_week(db_path: str, path: str = OUT) -> bool:
    """První noc ISO týdne zapíše týden předchozí (neděle = konec). Idempotentní."""
    dnes = datetime.now().date()
    konec = dnes - timedelta(days=dnes.isoweekday())      # minulá neděle
    if konec.isoformat() in _zapsane(path):
        return False
    try:
        row = measure(db_path, konec)
    except Exception as e:
        _log.warning("uzamceni: měření selhalo: %s", e)
        return False
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    _log.info("HANS_LOCK_AUDIT_V1: týden do %s — ozvěna v dialogu %s %%, "
              "hrady v jiném tématu %s %%", row["tyden_do"],
              row["ozvena_persony"]["dialog"]["pct"],
              row["hrady_v_jinem_tematu"]["pct"])
    return True


def _f(v):
    return "–" if v is None else ("%g" % v)


def format_report(db_path: str, path: str = OUT, tydnu: int = 6) -> str:
    """Text pro `/uzamceni`: živé měření posledních 7 dní + historie týdnů."""
    cur = measure(db_path, datetime.now().date())
    radky = ["Uzamčení (self-locking) — posledních 7 dní:"]
    for k, nazev in (("dialog_temata", "témata dialogů s Koláčem"),
                     ("cteni_tituly", "čtené články"),
                     ("obrazy_tituly", "obrazy")):
        m = cur[k]
        radky.append("  %s: opakování %s %% · top-5 %s %% · nová za 10 dní %s"
                     % (nazev, _f(m["opakovani_pct"]), _f(m["top5_pct"]),
                        m["nova_za_10_dni"]))
    oz = cur["ozvena_persony"]
    radky.append("  ozvěna persony (detail/preciznost/pečlivost): dialog %s %% · "
                 "reflexe dialogu %s %% · introspekce %s %%"
                 % (_f(oz["dialog"]["pct"]), _f(oz["dialog_reflection"]["pct"]),
                    _f(oz["introspection"]["pct"])))
    radky.append("  hrady v dialogu na jiné téma: %s %%"
                 % _f(cur["hrady_v_jinem_tematu"]["pct"]))
    hist = []
    try:
        with open(path, encoding="utf-8") as f:
            hist = [json.loads(l) for l in f if l.strip()][-tydnu:]
    except Exception:
        pass
    if hist:
        radky.append("")
        radky.append("Týdny (opakování dialogů · ozvěna v dialogu · hrady jinde):")
        for h in hist:
            radky.append("  do %s: %s %% · %s %% · %s %%" % (
                h["tyden_do"], _f(h["dialog_temata"]["opakovani_pct"]),
                _f(h["ozvena_persony"]["dialog"]["pct"]),
                _f(h["hrady_v_jinem_tematu"]["pct"])))
    radky.append("")
    radky.append("Pro srovnání: v článku AutoPersonas mělo uzamčení výchozích "
                 "61,8 % opakování, po zásahu 36,3 %.")
    return "\n".join(radky)


if __name__ == "__main__":
    import sys
    db = "data/hans_diary.db"
    if len(sys.argv) > 1 and sys.argv[1] == "--zpetne":
        # zpětné doplnění týdnů (jen čte deník): --zpetne 8
        n = int(sys.argv[2]) if len(sys.argv) > 2 else 8
        dnes = datetime.now().date()
        posl = dnes - timedelta(days=dnes.isoweekday())
        hotove = _zapsane(OUT)
        os.makedirs(os.path.dirname(OUT), exist_ok=True)
        with open(OUT, "a", encoding="utf-8") as f:
            for i in range(n - 1, -1, -1):
                k = posl - timedelta(days=7 * i)
                if k.isoformat() in hotove:
                    continue
                f.write(json.dumps(measure(db, k), ensure_ascii=False) + "\n")
    print(format_report(db))
