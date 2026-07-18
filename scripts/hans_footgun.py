"""HANS_FOOTGUN_V1 — bezpečný experiment: Hans si sám zapne herní mód
s AUTO-RESUME. Vyzkouší, co se stane, a v deníku má neutrální záznam
(bez hypotézy o následku — ať vhled vzejde z dat, ne z předepsaného textu).

**Bezpečnost:**
- AUTO-RESUME watchdog (try/finally) — i když Hans nemůže sám vypnout (protože
  jeho LLM je pausnutý), thread ho vypne po `duration_s`.
- Config gate `hans_experiment.enabled` (default False, opatrně)
- Max limit `hans_experiment.max_duration_s` (default 1800 = 30 min).
- Idempotence: jen JEDEN experiment naráz (kontrola thread stavu).

**Anti-konfab deník:**
- Zápis START = „Experimentuji: zapínám herní mód na N minut."
- Zápis END = „Experiment skončil (po N minutách)."
- NIKDE nezmiňujeme „chci zjistit, co se stane" ani „ověřuji hypotézu" —
  ať Hans SÁM ze self_insight vyvodí, co se dělo (jinak by dostal
  ready-made narativ „ověřoval jsem X").
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time

_log = logging.getLogger(__name__)

_LOCK = threading.Lock()
_RUNNING: dict = {"active": False, "started_ts": 0.0, "duration_s": 0}


def is_running() -> bool:
    with _LOCK:
        return bool(_RUNNING.get("active"))


def status() -> dict:
    with _LOCK:
        d = dict(_RUNNING)
    if d.get("active") and d.get("started_ts"):
        d["remaining_s"] = max(
            0, int(d["duration_s"] - (time.time() - d["started_ts"])))
    return d


def _log_diary(diary_db_path: str, event_type: str, title: str, note: str) -> None:
    """Zápis do diary. Deferral-safe."""
    try:
        conn = sqlite3.connect(diary_db_path, timeout=5.0)
        conn.execute(
            "INSERT INTO diary (ts, event_type, title, note) VALUES (?,?,?,?)",
            (time.time(), event_type, title, note))
        conn.commit()
        conn.close()
    except Exception as e:
        _log.warning("footgun: diary insert selhal: %s", e)


def experiment_run(config: dict, duration_s: int = 300) -> dict:
    """Spustí experiment: zapne game_mode, po `duration_s` vypne. Watchdog
    v samostatném threadu → i když volající umře, auto-resume proběhne.

    Vrací dict {ok, message, duration_s}. NEblokuje (thread běží asynchronně).
    """
    cfg = (config.get("hans_experiment", {}) or {})
    if not cfg.get("enabled", False):
        return {"ok": False, "message": "hans_experiment.enabled je False"}
    max_dur = int(cfg.get("max_duration_s", 1800))
    duration_s = max(60, min(int(duration_s), max_dur))

    with _LOCK:
        if _RUNNING.get("active"):
            return {"ok": False, "message": "experiment už běží",
                    "remaining_s": max(0, int(
                        _RUNNING["duration_s"] - (time.time() - _RUNNING["started_ts"])))}
        _RUNNING["active"] = True
        _RUNNING["started_ts"] = time.time()
        _RUNNING["duration_s"] = duration_s

    diary_db = (config.get("diary_db")
                or (config.get("hans_idle", {}) or {}).get("diary_db")
                or "data/hans_diary.db")

    def _worker():
        started = time.time()
        try:
            from scripts.ollama_client import set_game_mode
            # 1) zápis START (neutrální — bez hypotézy)
            mins = duration_s // 60
            _log_diary(diary_db, "experiment",
                       "Experiment",
                       f"Experimentuji: zapínám herní mód na {mins} minut.")
            _log.info("footgun: experiment START (%d min)", mins)
            # 2) zapnout game_mode
            try:
                set_game_mode(True, config=config)
            except Exception as _sme:
                _log.warning("footgun: set_game_mode(True) selhal: %s", _sme)
            # 3) čekat
            time.sleep(duration_s)
        finally:
            # 4) AUTO-RESUME (i když se něco pokazí)
            try:
                from scripts.ollama_client import set_game_mode
                set_game_mode(False, config=config)
                _log.info("footgun: AUTO-RESUME provedeno")
            except Exception as _re:
                _log.error("footgun: AUTO-RESUME selhal: %s", _re)
            ended = time.time()
            # 5) zápis END
            _log_diary(diary_db, "experiment",
                       "Experiment skončil",
                       f"Experiment skončil (po {duration_s // 60} minutách).")
            # 6) offline_window (source='experiment') — ať to self_insight
            # uvidí jako separátní kategorii Hansovy iniciativy (odlišnou od
            # user-triggered game_mode a od server-side brain_down_up).
            try:
                from scripts.hans_offline_windows import _init
                conn = sqlite3.connect(diary_db, timeout=5.0)
                _init(conn)
                conn.execute(
                    "INSERT OR IGNORE INTO offline_windows "
                    "(start_ts, end_ts, duration_s, source, created_ts) "
                    "VALUES (?,?,?,'experiment',?)",
                    (started, ended, ended - started, ended))
                conn.commit(); conn.close()
            except Exception as _owe:
                _log.debug("footgun: offline_window insert: %s", _owe)
            with _LOCK:
                _RUNNING["active"] = False
                _RUNNING["started_ts"] = 0.0
                _RUNNING["duration_s"] = 0

    threading.Thread(target=_worker, daemon=True, name="HansFootgun").start()
    return {"ok": True, "message": f"Experiment spuštěn na {duration_s // 60} min",
            "duration_s": duration_s}


if __name__ == "__main__":
    import json, sys
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    cfg = json.load(open("config.json"))
    cfg.setdefault("hans_experiment", {})["enabled"] = True
    dur = int(sys.argv[1]) if len(sys.argv) > 1 else 300
    r = experiment_run(cfg, duration_s=dur)
    print("start:", r)
    print("čekám na dokončení (auto-resume), sleduj log...")
    time.sleep(dur + 10)
    print("konec:", status())
