"""HANS_PREVENTION_V1 (25. 9.) — krok 1 prevence: SBĚR denních souhrnů, zatím nic nehlásí.

Nápad uživatele 25. 9.: Hans má poruchy zachytit DŘÍV, než něco spadne (prevence),
ne je rozebírat až po pádu. Inventura téhož dne ukázala, že `hans_health` má jen
PRAHY („teď OK/DOWN“) a TRENDY nejdou spočítat vůbec:
  • logy držely jen ~3 dny (rotace 2 MB × 3) — prodlouženo v `logger.py`,
  • žádná tabulka metrik, takže „roste to?“ nemá z čeho odpovědět,
  • samooprav bylo za 3 dny 15 (4× zaseklá Ollama, 11× uvízlá GPU → restart
    ComfyUI) a nikdo neví, jestli je to hodně — samooprava umí zhoršení maskovat,
  • disky PC nehlídá nic (SSD s ComfyUI 91 %).

Tohle je jen SBĚR do `health_daily` (den × druh × klíč → hodnota). Hlášení
(nový typ chyby mimo výpadek mozku, samoopravy nad obvyklou mírou, disk plný do
N dní) se zapne až nad ~14 dny základu — bez něj by hlásilo pořád (40 typů
varování, 11–14 „nových“ denně).

Běží z `HansRoutine.tick` jednou za hodinu ve vlastním vlákně. Log se čte
PŘÍRŮSTKOVĚ (od posledního zpracovaného času), aby se nic nezapočítalo dvakrát
a nic neztratilo při rotaci. `hans_schedule.mark('prevence_sber')` → když sběr
tiše umře, pozná to hlídač rozvrhu.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import sqlite3
import time
from pathlib import Path

_log = logging.getLogger("hans_prevence")

_ROOT = Path(__file__).resolve().parent.parent
_LOG = _ROOT / "data" / "system.log"
_LINE = re.compile(
    r"^(\d{4}-\d\d-\d\d) (\d\d:\d\d:\d\d)[,.\d]* \[(ERROR|WARNING|CRITICAL)\] ([\w.]+): (.*)$")

# Samoopravy — co Hans sám spravil (změřeno 25. 9. nad logy 22.–25. 9.).
_HEAL = [
    ("comfyui_restart", re.compile(r"GPU uvízla .*restartuji")),
    ("ollama_wedged_restart", re.compile(r"Ollama WEDGED .*restartuji")),
    ("ollama_model_restart", re.compile(r"se nenačítá .*restartuj")),
    ("camera_recovery", re.compile(r"(?i)camera.*(recover|restart|obnov)")),
]


def podpis(zprava: str) -> str:
    """Typ hlášky bez proměnných částí (čísla, URL, citované hodnoty)."""
    s = re.sub(r"https?://\S+", "<url>", zprava or "")
    s = re.sub(r"'[^']{0,80}'", "'…'", s)
    s = re.sub(r"\"[^\"]{0,80}\"", "\"…\"", s)
    s = re.sub(r"„[^“]{0,80}“", "„…“", s)
    s = re.sub(r"\d+(?:[.,]\d+)?", "N", s)
    return s[:120]


# ── úložiště ────────────────────────────────────────────────────────────────

def _db(path: str) -> sqlite3.Connection:
    c = sqlite3.connect(path, timeout=10)
    c.execute("""CREATE TABLE IF NOT EXISTS health_daily (
                   day TEXT NOT NULL, kind TEXT NOT NULL, key TEXT NOT NULL,
                   value REAL NOT NULL, updated_ts REAL NOT NULL,
                   PRIMARY KEY (day, kind, key))""")
    c.execute("""CREATE TABLE IF NOT EXISTS health_meta (
                   k TEXT PRIMARY KEY, v TEXT)""")
    return c


def _pricti(c, day, kind, key, n=1.0):
    c.execute("""INSERT INTO health_daily (day, kind, key, value, updated_ts)
                 VALUES (?,?,?,?,?)
                 ON CONFLICT(day, kind, key) DO UPDATE SET
                   value = value + excluded.value, updated_ts = excluded.updated_ts""",
              (day, kind, key, float(n), time.time()))


def _nastav(c, day, kind, key, v):
    """Měřidlo (disk, paměť): drží POSLEDNÍ hodnotu dne."""
    c.execute("""INSERT INTO health_daily (day, kind, key, value, updated_ts)
                 VALUES (?,?,?,?,?)
                 ON CONFLICT(day, kind, key) DO UPDATE SET
                   value = excluded.value, updated_ts = excluded.updated_ts""",
              (day, kind, key, float(v), time.time()))


# ── sběr ────────────────────────────────────────────────────────────────────

def _radky_od(posledni: str, vse: bool = False):
    """Řádky logu novější než `posledni` ('YYYY-MM-DD HH:MM:SS'), přes rotaci
    (rotované v data/logs/, HANS_LOG_RETENTION_V1). Běžně stačí .1 + aktuální
    (sběr co hodinu, soubor vydrží ~16 h); první běh (`vse`) projde všechny."""
    rot = _LOG.parent / "logs"
    if vse:
        cisla = sorted((int(p.name.rsplit(".", 1)[1]) for p in rot.glob(_LOG.name + ".*")
                        if p.name.rsplit(".", 1)[1].isdigit()), reverse=True)
        soubory = [rot / ("%s.%d" % (_LOG.name, n)) for n in cisla] + [_LOG]
    else:
        soubory = [rot / (_LOG.name + ".1"), _LOG]
    for p in soubory:
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                for l in f:
                    if l[:19] > posledni:
                        yield l
        except FileNotFoundError:
            continue


def sber_logu(c) -> int:
    row = c.execute("SELECT v FROM health_meta WHERE k='log_posledni'").fetchone()
    # první běh: celá dostupná historie (základ nezačíná od nuly)
    posledni = row[0] if row else ""
    nove, nejnovejsi = 0, posledni
    for l in _radky_od(posledni, vse=not row):
        ts = l[:19]
        if ts > nejnovejsi and re.match(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d", ts):
            nejnovejsi = ts
        m = _LINE.match(l.rstrip("\n"))
        if m:
            day, _t, lvl, mod, msg = m.groups()
            _pricti(c, day, "err" if lvl in ("ERROR", "CRITICAL") else "warn",
                    "%s|%s" % (mod, podpis(msg)))
            nove += 1
        for klic, pat in _HEAL:
            if pat.search(l):
                _pricti(c, l[:10], "heal", klic)
    c.execute("INSERT OR REPLACE INTO health_meta (k, v) VALUES ('log_posledni', ?)",
              (nejnovejsi,))
    return nove


def _disk(c, day, klic, cesta):
    try:
        t, u, f = shutil.disk_usage(cesta)
        _nastav(c, day, "disk_free_gb", klic, round(f / 1e9, 2))
        _nastav(c, day, "disk_used_pct", klic, round(100.0 * u / t, 1))
    except Exception:
        pass


def sber_meridel(c, config: dict) -> None:
    day = time.strftime("%Y-%m-%d")
    _disk(c, day, "pi_root", str(_ROOT))
    for m in ("/mnt/nas-hans",):
        if os.path.ismount(m):
            _disk(c, day, "nas", m)
    # disky PC — jen když PC běží (jinak SSH jen čeká na timeout)
    try:
        from scripts.ollama_client import brain_available
        if brain_available(config):
            from scripts import pc_remote
            out = pc_remote.run(config, "df -B1 --output=target,size,used,avail "
                                        "/ /run/media/ssd 2>/dev/null", timeout=8)
            for l in (out or "").splitlines()[1:]:
                p = l.split()
                if len(p) == 4 and p[1].isdigit():
                    klic = "pc" + (p[0].replace("/run/media/", "_") if p[0] != "/" else "_root")
                    _nastav(c, day, "disk_free_gb", klic, round(int(p[3]) / 1e9, 2))
                    _nastav(c, day, "disk_used_pct", klic,
                            round(100.0 * int(p[2]) / max(1, int(p[1])), 1))
    except Exception as e:
        _log.debug("prevence: disky PC: %s", e)
    # paměť Hansova procesu (sběr běží v něm) — únik paměti = rostoucí RSS
    try:
        with open("/proc/self/status") as f:
            for l in f:
                if l.startswith("VmRSS:"):
                    _nastav(c, day, "mem_mb", "hans_rss", round(int(l.split()[1]) / 1024, 1))
    except Exception:
        pass
    # velikost databází a logů
    try:
        for db in (_ROOT / "data").glob("*.db"):
            _nastav(c, day, "size_mb", db.name, round(db.stat().st_size / 1e6, 2))
        logy = sum(p.stat().st_size for p in list((_ROOT / "data").glob("system.log*"))
                   + list((_ROOT / "data" / "logs").glob("system.log*")))
        _nastav(c, day, "size_mb", "system.log*", round(logy / 1e6, 2))
    except Exception:
        pass


def sber(config: dict, db_path: str) -> dict:
    """Jeden běh sběru. Nikdy nevyhazuje."""
    try:
        c = _db(db_path)
        try:
            n = sber_logu(c)
            sber_meridel(c, config)
            c.commit()
        finally:
            c.close()
        try:
            from scripts import hans_schedule
            hans_schedule.mark("prevence_sber")
        except Exception:
            pass
        _log.info("HANS_PREVENTION_V1: sběr hotov (%d řádků chyb a varování)", n)
        return {"ok": True, "radku": n}
    except Exception as e:
        _log.warning("HANS_PREVENTION_V1: sběr selhal: %s", e)
        return {"ok": False, "chyba": str(e)}
