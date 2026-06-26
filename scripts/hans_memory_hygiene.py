#!/usr/bin/env python3
"""
HANS_MEMORY_HYGIENE_V1 — #2 hygiena paměti: retenční prořez deníkového firehose.

Deník (hans_diary.db) rostl bez prořezu — ~75 % objemu je vjemový firehose
(person_seen, teddy_arrived, teddy_dialog, teddy_gone), který splnil účel
(detekce přítomnosti, stavba rutin, dialogy) a pak už jen narůstá. Tato vrstva
zavádí RETENČNÍ POLITIKU: per-typ okno, po kterém se staré řádky smažou.

BEZPEČNOST = WHITELIST: prořezávají se VÝHRADNĚ event typy uvedené v
`memory_hygiene.retention_days`. Cokoliv neuvedené (reflexe, lekce, kauzy,
studium, narativ, sny, díla, …) zůstává NETKNUTÉ navždy.

Okna jsou nastavena NAD lookback všech ověřených konzumentů (audit 26.6.):
  - person_seen   → routine_patterns (30d), relationships (~6d)   → retence 60d
  - teddy_dialog  → nejnovější(1), hobbies (30d), night-summary (dnešek),
                    synthesis (real-time)                          → retence 60d
  - teddy_arrived → ŽÁDNÝ čtenář (jen zápis + exclude listy)       → retence 14d
  - teddy_gone    → ŽÁDNÝ čtenář                                   → retence 14d

Čistě SQL (žádný LLM) → není „deferred", běží 1×/noc v hans_routine ticku.
VACUUM (reclaim místa na disku) je VOLITELNÝ (zamyká DB) — default vypnutý;
smazané řádky uvolní stránky k znovupoužití, takže růst se zastaví i bez něj.
"""
from __future__ import annotations

import logging
import sqlite3
import time

_log = logging.getLogger("hans_memory_hygiene")

# Výchozí retence (dny) — přepsatelné configem memory_hygiene.retention_days.
# JEN tyto typy se prořezávají (whitelist). Ověřeno auditem konzumentů.
DEFAULT_RETENTION = {
    "person_seen": 60,
    "teddy_dialog": 60,
    "teddy_arrived": 14,
    "teddy_gone": 14,
}


def _cfg(config: dict) -> dict:
    return (config.get("memory_hygiene", {}) or {})


def prune_diary(config: dict, diary_db_path: str) -> dict:
    """Smaže firehose řádky starší než per-typ retence. Vrací {typ: smazáno}.
    Whitelist — netkne se ničeho mimo retention mapu. Fault-tolerant."""
    if not _cfg(config).get("enabled", True):
        return {}
    retention = dict(DEFAULT_RETENTION)
    retention.update(_cfg(config).get("retention_days", {}) or {})

    now = time.time()
    deleted: dict = {}
    conn = None
    try:
        conn = sqlite3.connect(diary_db_path, timeout=10.0)
        for event_type, days in retention.items():
            try:
                days = int(days)
            except (TypeError, ValueError):
                continue
            if days <= 0:
                continue  # 0/záporné = neprořezávej (bezpečnostní opt-out)
            cutoff = now - days * 86400
            try:
                cur = conn.execute(
                    "DELETE FROM diary WHERE event_type=? AND ts < ?",
                    (event_type, cutoff))
                n = cur.rowcount or 0
                if n:
                    deleted[event_type] = n
            except Exception as e:
                _log.warning("prune_diary[%s] selhal: %s", event_type, e)
        conn.commit()
    except Exception as e:
        _log.warning("prune_diary selhal: %s", e)
        return deleted
    finally:
        if conn is not None:
            # VOLITELNÝ VACUUM — reclaim místa (zamyká DB; default OFF).
            if _cfg(config).get("vacuum", False) and deleted:
                try:
                    conn.execute("VACUUM")
                except Exception as e:
                    _log.debug("VACUUM přeskočen: %s", e)
            try:
                conn.close()
            except Exception:
                pass

    if deleted:
        total = sum(deleted.values())
        _log.info("memory_hygiene: prořezáno %d řádků (%s)",
                  total, ", ".join(f"{k}:{v}" for k, v in deleted.items()))
    else:
        _log.info("memory_hygiene: nic k prořezu")
    return deleted


# ── Smoke (python3 -m scripts.hans_memory_hygiene) ──────────────────────────
if __name__ == "__main__":
    import json
    cfg = {}
    try:
        with open("config.json", encoding="utf-8") as fh:
            cfg = json.load(fh)
    except Exception as exc:  # noqa
        print("WARN: config.json nenačten (%s)" % exc)
    db = cfg.get("diary_db", "data/hans_diary.db")
    ret = dict(DEFAULT_RETENTION)
    ret.update((_cfg(cfg).get("retention_days", {}) or {}))
    now = time.time()
    print("=== retence + kolik by se SMAZALO (dry-run) ===")
    conn = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
    for et, days in ret.items():
        cutoff = now - int(days) * 86400
        n = conn.execute(
            "SELECT COUNT(*) FROM diary WHERE event_type=? AND ts < ?",
            (et, cutoff)).fetchone()[0]
        keep = conn.execute(
            "SELECT COUNT(*) FROM diary WHERE event_type=? AND ts >= ?",
            (et, cutoff)).fetchone()[0]
        print(f"  {et:<16} retence {days:>3}d → smazat {n:<6} ponechat {keep}")
    conn.close()
