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
    c.commit()  # HANS_PREVENTION_NO_NET_LOCK_V1 — spící NAS se probouzí sekundy
    for m in ("/mnt/nas-hans",):
        if os.path.ismount(m):
            _disk(c, day, "nas", m)
    # disky PC — jen když PC běží (jinak SSH jen čeká na timeout)
    c.commit()  # HANS_PREVENTION_NO_NET_LOCK_V1 — ne přes SSH
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


# ── Kodi box (OSMC) ─────────────────────────────────────────────────────────
# HANS_KODI_LOG_RO_V1 (25. 9.) — na přání uživatele i chyby z Kodi boxu. Hranice
# „Kodi mimo Hanse“ (22. 8.) je tím otevřená ÚZCE, po vzoru HANS_ROUTER_V1:
# vlastní klíč `~/.ssh/hans_kodi_svc` smí na boxu spustit JEN `~/hans-log-cist`
# (authorized_keys: command=…,restrict) — vrátí error/warning řádky kodi.log
# (+ kodi.old.log) novější než zadaný čas a zaplnění disku. Nic nezapisuje.
# Interval 5 h (uživatel): čte se přírůstkově, takže interval mění jen čerstvost;
# ztráta hrozí jen při DVOU restartech Kodi v jednom intervalu (log se točí restartem).
_KODI_LINE = re.compile(
    r"^(\d{4}-\d\d-\d\d) [\d:.]+ T:\d+\s+(error|warning) <([^>]+)>: (.*)$")
KODI_INTERVAL_S = 5 * 3600


def podpis_kodi(zprava: str) -> str:
    s = re.sub(r"https?://\S+", "<url>", zprava or "")
    s = re.sub(r"(?:smb:|special:|resource:|image:)?/\S+", "<path>", s)
    return podpis(s)


def sber_kodi(c, config: dict, vynutit: bool = False) -> int:
    kc = (config.get("kodi", {}) or {})
    host = (kc.get("host") or "").strip()
    pc = (config.get("prevence", {}) or {})
    klic = os.path.expanduser(str(pc.get("kodi_ssh_key", "~/.ssh/hans_kodi_svc")))
    if not host or not os.path.exists(klic):
        return 0
    r0 = c.execute("SELECT v FROM health_meta WHERE k='kodi_beh_ts'").fetchone()
    interval = float(pc.get("kodi_interval_s", KODI_INTERVAL_S))
    if not vynutit and r0 and time.time() - float(r0[0] or 0) < interval:
        return 0
    row = c.execute("SELECT v FROM health_meta WHERE k='kodi_posledni'").fetchone()
    od = row[0] if row else ""
    import subprocess
    try:
        r = subprocess.run(["ssh", "-i", klic, "-o", "BatchMode=yes",
                            "-o", "IdentitiesOnly=yes", "-o", "ConnectTimeout=6",
                            "-o", "StrictHostKeyChecking=accept-new",
                            "osmc@%s" % host, od],
                           capture_output=True, text=True, timeout=60)
    except Exception as e:
        _log.info("prevence: Kodi box nedostupný: %s", e)
        return 0
    if r.returncode != 0:
        _log.info("prevence: Kodi čtení selhalo (exit %s): %s",
                  r.returncode, (r.stderr or "")[:120])
        return 0
    day = time.strftime("%Y-%m-%d")
    n, nejnovejsi = 0, od
    for l in r.stdout.splitlines():
        if l.startswith("DF "):
            p = l.split()
            if len(p) == 4 and p[1].isdigit():
                _nastav(c, day, "disk_free_gb", "kodi_root", round(int(p[3]) / 1e9, 2))
                _nastav(c, day, "disk_used_pct", "kodi_root",
                        round(100.0 * int(p[2]) / max(1, int(p[1])), 1))
            continue
        m = _KODI_LINE.match(l)
        if not m:
            continue
        d, lvl, mod, msg = m.groups()
        _pricti(c, d, "kodi_err" if lvl == "error" else "kodi_warn",
                "%s|%s" % (mod, podpis_kodi(msg)))
        n += 1
        if l[:23] > nejnovejsi:
            nejnovejsi = l[:23]
    c.execute("INSERT OR REPLACE INTO health_meta (k, v) VALUES ('kodi_posledni', ?)",
              (nejnovejsi,))
    c.execute("INSERT OR REPLACE INTO health_meta (k, v) VALUES ('kodi_beh_ts', ?)",
              (str(time.time()),))
    return n


def sber(config: dict, db_path: str) -> dict:
    """Jeden běh sběru. Nikdy nevyhazuje."""
    try:
        c = _db(db_path)
        try:
            n = sber_logu(c)
            # HANS_PREVENTION_NO_NET_LOCK_V1 (30. 9.) — uzavřít zápis PŘED voláním
            # po síti. Dřív zůstal zápis do deníku otevřený přes SSH na PC (až 8 s)
            # a čtení kodi.log (až 60 s) → ostatní zápisy dostaly „database is
            # locked“ (30. 9. 03:01 se tak ztratila reflexe tvorby).
            c.commit()
            sber_meridel(c, config)
            c.commit()
            nk = 0
            try:
                nk = sber_kodi(c, config)
            except Exception as ke:
                _log.info("prevence: Kodi sběr: %s", ke)
            c.commit()
        finally:
            c.close()
        try:
            from scripts import hans_schedule
            hans_schedule.mark("prevence_sber")
        except Exception:
            pass
        _log.info("HANS_PREVENTION_V1: sběr hotov (%d řádků chyb a varování, "
                  "Kodi %d)", n, nk)
        return {"ok": True, "radku": n, "kodi": nk}
    except Exception as e:
        _log.warning("HANS_PREVENTION_V1: sběr selhal: %s", e)
        return {"ok": False, "chyba": str(e)}


# ── HANS_PREVENTION_REPORT_V1 (7. 10.) — krok 2: HLÁŠENÍ nad nasbíranými dny ──
# Jen čte `health_daily` a porovná poslední dny se základem (okno 14 dní):
# co je NOVÉ, co SKOČILO, kolik bylo samooprav, kam míří disky a paměť.
# Nic neopravuje ani neposílá — vrací seznam řádků; kam hlášení půjde
# (ranní kontrola, Matrix), se rozhoduje jinde.
def hlaseni(db_path: str, dnes: str = None, okno: int = 14, cerstve: int = 2,
            nasobek: float = 3.0, min_pocet: int = 10) -> dict:
    """{"dny": n, "radky": [text…], "nove": [...], "skoky": [...], "disky": [...]}"""
    import datetime as _dt
    import statistics as _st
    c = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True, timeout=10)
    try:
        dny_vse = [r[0] for r in c.execute("SELECT DISTINCT day FROM health_daily ORDER BY day")]
        if not dny_vse:
            return {"dny": 0, "radky": ["Prevence: zatím žádná data."]}
        dnes = dnes or dny_vse[-1]
        d0 = _dt.date.fromisoformat(dnes)
        den = lambda i: (d0 - _dt.timedelta(days=i)).isoformat()
        nove_dny = [den(i) for i in range(cerstve)]
        zaklad = [den(i) for i in range(cerstve, okno)]
        zaklad = [d for d in zaklad if d in dny_vse]
        out = {"dny": len([d for d in dny_vse if d >= den(okno - 1)]), "radky": [],
               "nove": [], "skoky": [], "disky": []}
        R = out["radky"]
        if len(zaklad) < 5:
            R.append("Prevence: základ má jen %d dní — hlášení je orientační." % len(zaklad))

        def tab(kind):
            t = {}
            for day, key, v in c.execute(
                    "SELECT day, key, value FROM health_daily WHERE kind=? AND day>=?",
                    (kind, den(okno - 1))):
                t.setdefault(key, {})[day] = v
            return t

        nazev = {"err": "chyba", "warn": "varování", "kodi_err": "chyba Kodi",
                 "kodi_warn": "varování Kodi"}
        for kind in ("err", "warn", "kodi_err", "kodi_warn"):
            for key, dd in tab(kind).items():
                ted = sum(dd.get(d, 0) for d in nove_dny)
                if not ted:
                    continue
                drive = [dd.get(d, 0) for d in zaklad]
                if zaklad and not any(drive):
                    if ted >= (1 if kind == "err" else 3):
                        out["nove"].append((kind, key, int(ted)))
                elif zaklad:
                    med = _st.median(drive) or 0.5
                    prum = ted / float(cerstve)
                    if prum >= min_pocet and prum >= nasobek * med:
                        out["skoky"].append((kind, key, int(ted), round(med, 1)))
        out["nove"].sort(key=lambda x: (x[0] != "err", -x[2]))
        out["skoky"].sort(key=lambda x: -x[2])
        if out["nove"]:
            R.append("NOVÉ za poslední %d dny (v předchozích %d dnech ani jednou):"
                     % (cerstve, len(zaklad)))
            for kind, key, n in out["nove"][:8]:
                R.append("  • %s %d× — %s" % (nazev[kind], n, key[:110]))
            if len(out["nove"]) > 8:
                R.append("  … a dalších %d" % (len(out["nove"]) - 8))
        if out["skoky"]:
            R.append("SKOK proti obvyklému dni:")
            for kind, key, n, med in out["skoky"][:6]:
                R.append("  • %s %d× za %d dny (obvykle %s/den) — %s"
                         % (nazev[kind], n, cerstve, med, key[:90]))
        heal = tab("heal")
        for key, dd in sorted(heal.items()):
            ted = sum(dd.get(d, 0) for d in nove_dny)
            drive = sum(dd.get(d, 0) for d in zaklad)
            if ted or drive:
                R.append("Samoopravy %s: %d× za poslední %d dny, předtím %d× za %d dní"
                         % (key, ted, cerstve, drive, len(zaklad)))
        for key, dd in sorted(tab("disk_used_pct").items()):
            body = sorted(dd.items())
            if len(body) < 3:
                continue
            x = [(_dt.date.fromisoformat(d) - d0).days for d, _ in body]
            y = [v for _, v in body]
            mx, my = sum(x) / len(x), sum(y) / len(y)
            jm = sum((a - mx) ** 2 for a in x) or 1.0
            smer = sum((a - mx) * (b - my) for a, b in zip(x, y)) / jm   # % za den
            posl = y[-1]
            do95 = (95.0 - posl) / smer if smer > 0.05 else None
            out["disky"].append((key, round(posl, 1), round(smer, 2), do95))
            if posl >= 85 or (do95 is not None and do95 < 60):
                R.append("Disk %s: %.1f %% plný, %+.2f %% za den%s"
                         % (key, posl, smer,
                            (" → 95 %% za ~%d dní" % do95) if do95 is not None else ""))
        for kind, klic, jedn in (("mem_mb", "hans_rss", "MB"),):
            dd = tab(kind).get(klic) or {}
            body = [v for _, v in sorted(dd.items())]
            if len(body) >= 5:
                zac, kon = _st.median(body[:3]), _st.median(body[-3:])
                if kon > 1.5 * zac and kon - zac > 200:
                    R.append("Paměť Hanse roste: %d → %d %s (medián prvních a posledních 3 dnů)"
                             % (zac, kon, jedn))
        if len(R) == (1 if len(zaklad) < 5 else 0):
            R.append("Prevence: za poslední %d dny nic nového ani neobvyklého." % cerstve)
        return out
    finally:
        c.close()


if __name__ == "__main__":
    import sys as _sys
    if "--hlaseni" in _sys.argv:
        print("\n".join(hlaseni(os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "data", "hans_diary.db"))["radky"]))

