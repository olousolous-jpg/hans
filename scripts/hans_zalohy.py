"""HANS_BACKUP_WATCH_V1 (29. 9.) — hlídač stáří záloh, hlásí AKTIVNĚ.

Proč: zálohy dosud jen skončily ve stavu `failed` nebo tiše zestárly a nikdo
se to nedozvěděl (45 dní s neplatnou IP NASu; PC bez průběžné zálohy od
července). Pull cesta (`/zdravi`) doručením není [[pull-only-surfacing-is-not-delivery]],
proto hlídač posílá zprávu sám — nejvýš jednou denně za problém.

Kontroluje:
- záloha Pi na NASu: nejnovější `hans_backup_*.tar.gz` v `pi_nas_dir` (denní),
- záloha PC (restic, `HANS_PC_RESTIC_V1`): čas v `pc_ok_file` (týdenní).
Nepřipojený NAS = problém sám o sobě (hlásí se taky).
"""
from __future__ import annotations

import glob
import logging
import os
import time

_log = logging.getLogger(__name__)


def _dni(n: float) -> str:
    n = int(round(n))
    return "%d %s" % (n, "den" if n == 1 else "dny" if 2 <= n <= 4 else "dní")


def _cfg(config: dict) -> dict:
    return (config or {}).get("zalohy_hlidac", {}) or {}


def zkontroluj(config: dict, ted: float | None = None) -> list:
    """Seznam problémů (česky, pro člověka). Prázdný = vše v pořádku."""
    c = _cfg(config)
    if not c.get("enabled", True):
        return []
    ted = ted or time.time()
    problemy = []
    pi_dir = str(c.get("pi_nas_dir", "/mnt/nas-hans/Hans_backups"))
    pi_max = float(c.get("pi_max_dni", 3))
    pc_ok = str(c.get("pc_ok_file", "/mnt/nas-hans/hans_pc_restic/POSLEDNI_OK"))
    pc_max = float(c.get("pc_max_dni", 10))

    try:
        os.listdir(os.path.dirname(pi_dir))       # automount se připojí přístupem
    except Exception as e:
        return ["NAS se na Pi nepřipojil (%s) — zálohy Pi ani PC nejde ověřit." % type(e).__name__]

    archivy = glob.glob(os.path.join(pi_dir, "hans_backup_*.tar.gz"))
    if not archivy:
        problemy.append("Na NASu není žádná záloha Pi (%s)." % pi_dir)
    else:
        stari = (ted - max(os.path.getmtime(a) for a in archivy)) / 86400
        if stari > pi_max:
            problemy.append("Poslední záloha Pi na NASu je stará %s (čekám denně)." % _dni(stari))

    try:
        with open(pc_ok, encoding="utf-8") as f:
            ts = float(f.read().strip() or 0)
        stari = (ted - ts) / 86400
        if stari > pc_max:
            problemy.append("Poslední záloha PC je stará %s (čekám týdně, v neděli)." % _dni(stari))
    except FileNotFoundError:
        problemy.append("Záloha PC na NASu chybí (%s)." % pc_ok)
    except Exception as e:
        problemy.append("Stav zálohy PC nejde přečíst (%s)." % type(e).__name__)
    return problemy


_nahlaseno: dict = {}      # problém → den posledního hlášení


def hlidej(config: dict, notifier) -> list:
    """Zkontroluje a nový/dnes nenahlášený problém pošle přes `notifier`."""
    try:
        problemy = zkontroluj(config)
    except Exception as e:
        _log.warning("hlídač záloh selhal: %s", e)
        return []
    den = time.strftime("%Y-%m-%d")
    nove = [p for p in problemy if _nahlaseno.get(p) != den]
    if nove:
        _log.warning("hlídač záloh: %s", " | ".join(nove))
        if notifier:
            try:
                notifier("⚠️ Zálohy: " + " ".join(nove))
            except Exception as e:
                _log.warning("hlídač záloh: hlášení neodešlo: %s", e)
        for p in nove:
            _nahlaseno[p] = den
    return problemy
