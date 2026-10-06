#!/usr/bin/env python3
"""hans_heavy_queue.py — HANS_HEAVY_QUEUE_V1 (28. 9., přání uživatele)

Fronta VÝPOČETNĚ NÁROČNÝCH úloh na PC (úprava fotky FLUX Kontext, malba na
požádání). Proč:
  * úprava fotky se spouštěla hned — při tréninku na PC (RAM plná, swap 20 GB)
    se zasekla, po 10 min selhala a držela VRAM (Hans bez mozku);
  * malba a úprava fotky mohly běžet NARÁZ (obě ~13 GB VRAM);
  * chat čekal na úpravu 8–9 minut.
Požadavek se jen ZAŘADÍ (odpověď hned, s pořadím), jeden pracovník bere úlohy
PO JEDNÉ a spustí je, jen když je PC opravdu volné:
  ComfyUI běží · nehraje se · na PC neběží dlouhá práce (`~/.hans_nevypinat`)
  · dost volné RAM (`heavy_queue.min_free_gb` podle druhu) · jiný render neběží.
Hotové → doručit (fotka zpět do místnosti, odkud přišla; obraz tomu, kdo
žádal, má-li Matrix). Selhání → znovu, nejvýš `max_attempts`; po `max_age_h`
propadne s omluvou. Dlužné obrazy ze slibů (HANS_ART_RETRY_V1) sdílejí
tentýž ZÁMEK a tutéž kontrolu zdrojů — dvě náročné věci nikdy naráz.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import Callable, Optional

_log = __import__("scripts.logger", fromlist=["get_logger"]).get_logger(
    "hans_heavy_queue")

ZAMEK = threading.Lock()          # jedna náročná úloha naráz (i HANS_ART_RETRY_V1)
_kick = threading.Event()
_worker: Optional[threading.Thread] = None

_SCHEMA = """CREATE TABLE IF NOT EXISTS heavy_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_ts REAL NOT NULL, kind TEXT NOT NULL, person TEXT, room TEXT,
    payload TEXT, status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT,
    started_ts REAL, done_ts REAL, result TEXT)"""


def _cfg(config: dict) -> dict:
    return (config or {}).get("heavy_queue", {}) or {}


def _conn(db: str):
    c = sqlite3.connect(db, timeout=10.0)
    c.execute(_SCHEMA)
    return c


def enqueue(db: str, kind: str, person: str, payload: dict,
            room: str = "") -> tuple:
    """→ (id, kolik úloh je PŘED ní). Probudí pracovníka."""
    c = _conn(db)
    try:
        ahead = c.execute("SELECT count(*) FROM heavy_jobs WHERE status IN "
                          "('pending','running')").fetchone()[0]
        cur = c.execute(
            "INSERT INTO heavy_jobs (created_ts, kind, person, room, payload) "
            "VALUES (?,?,?,?,?)", (time.time(), kind, (person or "").lower(),
                                   room or "", json.dumps(payload, ensure_ascii=False)))
        c.commit()
        jid = int(cur.lastrowid)
    finally:
        c.close()
    _log.info("heavy_queue: +#%d %s pro %s (před ní %d)", jid, kind, person, ahead)
    _kick.set()
    return jid, int(ahead)


def poradi_text(ahead: int) -> str:
    if ahead <= 0:
        return "Pustím se do toho hned, jak bude počítač volný."
    if ahead == 1:
        return "Přede mnou je ještě jedna úloha, pak přijde na řadu tahle."
    if ahead < 5:
        return "Přede mnou jsou ještě %d úlohy, pak přijde na řadu tahle." % ahead
    return "Přede mnou je ještě %d úloh, pak přijde na řadu tahle." % ahead


# ── kontrola zdrojů ──────────────────────────────────────────────────────────
def zdroje_volne(config: dict, kind: str) -> tuple:
    """(True, "") nebo (False, důvod). Levné kontroly napřed."""
    try:
        from scripts.ollama_client import game_mode_on
        if game_mode_on():
            return False, "hraje se"
    except Exception:
        pass
    try:
        from scripts.avatar_render import render_in_progress
        if render_in_progress():
            return False, "běží jiný render"
    except Exception:
        pass
    try:
        from scripts.hans_art import _comfy_ready
        if not _comfy_ready(config):
            return False, "ComfyUI/PC neodpovídá"
    except Exception:
        return False, "ComfyUI nejde ověřit"
    try:
        from scripts import pc_remote
        out = pc_remote.run(config, "test -e ~/.hans_nevypinat && echo DRZI; "
                                    "ps -C python -o rss=,args= | awk '/ComfyUI\\/main.py/"
                                    "{s+=$1} END{print \"COMFY\", s+0}'; "
                                    "awk '/MemAvailable/{print $2}' /proc/meminfo")
    except Exception:
        out = None
    if out is None:
        return False, "PC neodpovídá přes SSH"
    if "DRZI" in out:
        return False, "na PC běží dlouhá práce"
    try:
        free_gb = int(out.strip().split()[-1]) / 1048576
    except Exception:
        return False, "nešlo změřit RAM"
    need = float((_cfg(config).get("min_free_gb", {}) or {}).get(
        kind, {"photo_edit": 14, "photo_review": 4}.get(kind, 8)))
    if free_gb < need:
        # HANS_HEAVY_QUEUE_COMFY_RAM_V1 — ComfyUI si po renderu nechává model
        # v RAM (28. 9.: 18,5 GB), `/free` uvolní jen VRAM → fronta by čekala
        # donekonečna na paměť, kterou drží její vlastní nástroj. Když by po
        # restartu ComfyUI RAM stačila, restartuj ho (čistý start ~20 s).
        try:
            comfy_gb = int(out.split("COMFY")[1].split()[0]) / 1048576
        except Exception:
            comfy_gb = 0.0
        if comfy_gb > 2 and free_gb + comfy_gb >= need:
            try:
                pc_remote.run(config, "systemctl --user restart comfyui", timeout=30)
                _log.info("heavy_queue: ComfyUI držel %.1f GB RAM (volno %.1f < %.0f) "
                          "→ restartován", comfy_gb, free_gb, need)
            except Exception as e:
                _log.warning("heavy_queue: restart ComfyUI selhal: %s", e)
            return False, "restartuji ComfyUI (držel %.1f GB RAM)" % comfy_gb
        return False, "málo RAM na PC (%.1f < %.0f GB)" % (free_gb, need)
    return True, ""


# ── úlohy ────────────────────────────────────────────────────────────────────
def _run_photo_edit(config, db, p) -> tuple:
    from scripts import hans_foto as _hf
    out = _hf.uprav(config, p["path"], p.get("pokyn", ""), en=p.get("en", ""))
    if out:
        _hf.pridej_vysledek(p.get("person", ""), p.get("room", ""), out)
        return True, out, ""
    return False, None, _hf.duvod_selhani()


def _run_paint(config, db, p) -> tuple:
    from scripts import hans_art
    r = hans_art.paint_subject(config, db, p["subject"], style=p.get("style", ""),
                               _retry=True)
    if r:
        return True, r[0], ""
    return False, None, "obraz se nevyrenderoval"


def _run_photo_review(config, db, p) -> tuple:
    from scripts import hans_foto as _hf
    odp = _hf.posud(config, p.get("paths") or [], p.get("otazka", ""))
    if odp:
        _hf.uloz_posudek(p.get("person", ""), p.get("room", ""), odp)
        return True, None, odp
    return False, None, "posudek se nepodařil"


_HANDLERS = {"photo_edit": _run_photo_edit, "paint": _run_paint,
             "photo_review": _run_photo_review}


def _hotovo_text(kind, p):
    if kind == "photo_edit":
        return "Upravená fotka je hotová — tady je."
    return "Obraz na téma „%s“ je hotový — tady je." % (p.get("subject") or "")[:80]


def _vzdano_text(kind, p, duvod):
    if kind == "photo_review":
        return "Fotku se mi nepodařilo posoudit (%s). Zkusíte se zeptat znovu?" % duvod
    if kind == "photo_edit":
        return ("Fotku se mi ani na opakovaný pokus nepodařilo upravit (%s). "
                "Omlouvám se — zkusíte mi ji poslat znovu?" % duvod)
    return ("Obraz na téma „%s“ se mi ani na opakovaný pokus nepodařilo namalovat. "
            "Omlouvám se — zkusíte mi ho zadat znovu?" % (p.get("subject") or "")[:80])


def _tick(config: dict, db: str, deliver: Callable) -> None:
    c = _conn(db)
    try:
        c.row_factory = sqlite3.Row
        # zaseknuté „running“ (restart uprostřed) → zpět do fronty
        c.execute("UPDATE heavy_jobs SET status='pending' WHERE status='running' "
                  "AND started_ts < ?", (time.time() - 3600,))
        max_age = float(_cfg(config).get("max_age_h", 24)) * 3600
        stare = c.execute("SELECT * FROM heavy_jobs WHERE status='pending' AND "
                          "created_ts < ?", (time.time() - max_age,)).fetchall()
        for r in stare:
            c.execute("UPDATE heavy_jobs SET status='expired', done_ts=? WHERE id=?",
                      (time.time(), r["id"]))
            c.commit()
            deliver(_vzdano_text(r["kind"], json.loads(r["payload"] or "{}"),
                                 "počítač celý den nebyl volný"), None, r)
        job = c.execute("SELECT * FROM heavy_jobs WHERE status='pending' "
                        "ORDER BY id LIMIT 1").fetchone()
        c.commit()
    finally:
        c.close()
    if not job:
        return
    ok_zdroje, duvod = zdroje_volne(config, job["kind"])
    if not ok_zdroje:
        _log.debug("heavy_queue: #%d čeká — %s", job["id"], duvod)
        return
    if not ZAMEK.acquire(blocking=False):
        return
    try:
        p = json.loads(job["payload"] or "{}")
        c = _conn(db)
        c.execute("UPDATE heavy_jobs SET status='running', started_ts=?, "
                  "attempts=attempts+1 WHERE id=?", (time.time(), job["id"]))
        c.commit()
        c.close()
        _log.info("heavy_queue: ▶ #%d %s (pokus %d)", job["id"], job["kind"],
                  job["attempts"] + 1)
        try:
            ok, vysl, chyba = _HANDLERS[job["kind"]](config, db, p)
        except Exception as e:
            ok, vysl, chyba = False, None, repr(e)[:200]
        c = _conn(db)
        _st = c.execute("SELECT status FROM heavy_jobs WHERE id=?",
                        (job["id"],)).fetchone()
        if _st and _st[0] == "cancelled":       # zrušeno uživatelem za běhu
            c.close()
            _log.info("heavy_queue: #%d zrušena během běhu → nic nedoručuji", job["id"])
            return
        if ok:
            c.execute("UPDATE heavy_jobs SET status='done', done_ts=?, result=? "
                      "WHERE id=?", (time.time(), vysl or "", job["id"]))
            c.commit()
            c.close()
            _log.info("heavy_queue: ✔ #%d %s → %s", job["id"], job["kind"], vysl)
            deliver(chyba if job["kind"] == "photo_review" else
                    _hotovo_text(job["kind"], p), vysl, dict(job))
            return
        maxp = int(_cfg(config).get("max_attempts", 3))
        konec = job["attempts"] + 1 >= maxp
        c.execute("UPDATE heavy_jobs SET status=?, last_error=?, done_ts=? WHERE id=?",
                  ("failed" if konec else "pending", chyba, time.time() if konec else None,
                   job["id"]))
        c.commit()
        c.close()
        _log.warning("heavy_queue: ✘ #%d %s (%s)%s", job["id"], job["kind"], chyba,
                     " → vzdávám" if konec else " → zkusím znovu")
        if konec:
            deliver(_vzdano_text(job["kind"], p, chyba), None, dict(job))
    finally:
        ZAMEK.release()


def start_worker(config: dict, db: str, deliver: Callable) -> None:
    """`deliver(text, photo_path|None, job_row)` — doručení výsledku."""
    global _worker
    if _worker and _worker.is_alive():
        return
    interval = float(_cfg(config).get("interval_s", 60))

    try:   # restart uprostřed úlohy → zpět do fronty (pokus se nepočítá)
        c = _conn(db)
        c.execute("UPDATE heavy_jobs SET status='pending', attempts=MAX(attempts-1,0) "
                  "WHERE status='running'")
        c.commit()
        c.close()
    except Exception as e:
        _log.warning("heavy_queue: reset running: %s", e)

    def _loop():
        time.sleep(20)
        while True:
            try:
                _tick(config, db, deliver)
            except Exception as e:
                _log.warning("heavy_queue tick: %s", e)
            try:    # HANS_ART_LESSON_PENDING_V1 — odložené lekce po hodnocení
                from scripts.hans_art import dozen_lekce_po_hodnoceni
                dozen_lekce_po_hodnoceni(config, db)
            except Exception as e:
                _log.debug("heavy_queue: dohnání lekcí: %s", e)
            _kick.wait(interval)
            _kick.clear()
    _worker = threading.Thread(target=_loop, daemon=True, name="heavy-queue")
    _worker.start()
    _log.info("heavy_queue: pracovník běží (á %.0f s)", interval)


def stav(db: str) -> list:
    c = _conn(db)
    try:
        return c.execute("SELECT id, kind, person, status, attempts FROM heavy_jobs "
                         "WHERE status IN ('pending','running') ORDER BY id").fetchall()
    finally:
        c.close()
