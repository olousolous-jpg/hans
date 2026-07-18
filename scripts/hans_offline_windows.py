"""HANS_OFFLINE_WINDOWS_V1 — kauzálně-agnostický scorable záznam „byl jsem offline T1–T2".

Motivace ([[genuinni-sebe-odvozeni]] návrh v CLAUDE.md 2.7.):
Hans má sám odvodit vzorec „herní mód → Hans offline". Dnes to nejde: `brain_down`
event je noisy (v poslední 2 dny 5 `brain_still_down` heartbeatů + 2 blipy) a není
ve scorable tabulce → Hans nemá čistý signál na reflexi. Tady vyrábíme JEDEN
záznam per skutečný offline interval (start_ts + end_ts + duration_s), bez
domýšlení příčiny (analytika je jiný modul, dostane surová data).

Deduplikace: `start_ts` jako primary key (jeden window per brain_down timestamp).
Filtr: `min_duration_s` (default 300 = 5 min) — krátké blipy (síťové zaškubnutí,
Ollama swap) nejsou zajímavé pro reflexi.

⚠️ **Anti-konfabulace:** modul ULOŽÍ jen fakta z data (kdy začal, kdy skončil,
jak dlouho). NIC o důvodu (herní mód / crash / restart PC / síť). Kauzální
propojení dělá až `hans_self_insight` z korelace s `game_mode` diary.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from typing import Optional

_log = logging.getLogger(__name__)


def _init(db) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS offline_windows (
        start_ts    REAL PRIMARY KEY,
        end_ts      REAL NOT NULL,
        duration_s  REAL NOT NULL,
        source      TEXT NOT NULL DEFAULT 'brain_down_up',
        created_ts  REAL NOT NULL)""")


def _populate_from_game_mode(conn, since: float, min_duration_s: float,
                             now: float) -> int:
    """HANS_OFFLINE_WINDOWS_GAMEMODE_V1 (18.7.) — Hans si sám vypnul myšlení
    přes herní mód (`game_mode_on()` gate v ollama_client → LLM cesty vracejí
    None). Ollama server žije → `brain_down` to nezachytí → self-induced offline
    nikde. Vezmi ZAP→VYP páry z `game_mode` diary a vlož je jako windows
    s `source='game_mode_pair'`. Hans pak v evidence uvidí obě kategorie."""
    # existující game_mode start_ts pro dedup (jen tento source)
    seen = {r[0] for r in conn.execute(
        "SELECT start_ts FROM offline_windows "
        "WHERE start_ts >= ? AND source='game_mode_pair'",
        (since,)).fetchall()}
    rows = conn.execute(
        "SELECT ts, note FROM diary WHERE event_type='game_mode' AND ts >= ? "
        "ORDER BY ts ASC", (since,)).fetchall()
    added = 0
    zap_ts = None
    for ts, note in rows:
        nl = (note or "").lower()
        if "zapnul" in nl or "aktivoval" in nl:
            zap_ts = ts
        elif ("vypnul" in nl or "deaktivoval" in nl) and zap_ts is not None:
            if ts > zap_ts and (ts - zap_ts) >= min_duration_s and zap_ts not in seen:
                try:
                    conn.execute(
                        "INSERT INTO offline_windows "
                        "(start_ts, end_ts, duration_s, source, created_ts) "
                        "VALUES (?, ?, ?, 'game_mode_pair', ?)",
                        (zap_ts, ts, ts - zap_ts, now))
                    added += 1
                except sqlite3.IntegrityError:
                    pass  # dup s brain_down_up na stejný start_ts — necháme brain_down
            zap_ts = None
    return added


def populate_offline_windows(diary_db_path: str,
                             min_duration_s: float = 300.0,
                             lookback_days: float = 60.0) -> int:
    """Z `brain_down` / `brain_up` eventů v `diary` vyrobí `offline_windows`.

    Pairing: každý `brain_down` se spáruje s NEJBLIŽŠÍM následujícím `brain_up`
    (max 24h dopředu — pokud není žádný v okně, ignorujeme, protože Hans zjevně
    tvrdě spadl a nikdo neví, kdy naběhl). Dedup na start_ts (idempotentní).
    Filtr `min_duration_s` (default 5 min).

    Vrací počet NOVĚ přidaných window; existující se neaktualizují.
    """
    since = time.time() - lookback_days * 86400.0
    try:
        conn = sqlite3.connect(diary_db_path, timeout=5.0)
    except Exception as e:
        _log.warning("offline_windows: DB open selhalo: %s", e)
        return 0
    try:
        _init(conn)
        # vezmi všechny brain_down a brain_up v okně, seřazené chronologicky
        rows = conn.execute(
            "SELECT ts, event_type FROM diary "
            "WHERE event_type IN ('brain_down','brain_up') AND ts >= ? "
            "ORDER BY ts ASC", (since,)).fetchall()
        # existující start_ts pro dedup
        seen = {r[0] for r in conn.execute(
            "SELECT start_ts FROM offline_windows WHERE start_ts >= ?",
            (since,)).fetchall()}
        added = 0
        now = time.time()
        i = 0
        while i < len(rows):
            ts, etype = rows[i]
            if etype != 'brain_down':
                i += 1
                continue
            # už máme? skip (idempotentní)
            if ts in seen:
                i += 1
                continue
            # najdi NEJBLIŽŠÍ následující brain_up (max 24h)
            end_ts = None
            for j in range(i + 1, len(rows)):
                ts2, et2 = rows[j]
                if et2 == 'brain_up':
                    if ts2 - ts < 24 * 3600:
                        end_ts = ts2
                    break
                # dva brain_down za sebou → první je opuštěný (nemá párový up
                # v okně); přeskoč (odpovídá to tvrdému pádu bez čistého recovery)
                if et2 == 'brain_down':
                    break
            if end_ts is None:
                i += 1
                continue
            dur = end_ts - ts
            if dur < min_duration_s:
                i += 1
                continue
            try:
                conn.execute(
                    "INSERT INTO offline_windows "
                    "(start_ts, end_ts, duration_s, source, created_ts) "
                    "VALUES (?, ?, ?, 'brain_down_up', ?)",
                    (ts, end_ts, dur, now))
                added += 1
            except sqlite3.IntegrityError:
                pass  # duplikát (kdyby PK check nezachytil)
            i += 1
        # Přidej i self-induced offline (herní mód ZAP-VYP páry) — jiný source
        added_gm = _populate_from_game_mode(conn, since, min_duration_s, now)
        conn.commit()
        if added or added_gm:
            _log.info("offline_windows: přidáno %d brain_down + %d game_mode "
                      "(lookback %dd)", added, added_gm, int(lookback_days))
        return added + added_gm
    finally:
        conn.close()


def get_windows(diary_db_path: str, since_ts: Optional[float] = None,
                limit: int = 200) -> list[dict]:
    """Čtecí API: vrátí offline windows (nejnovější první). Bezpečné — nevyhodí.
    Kauzálně-agnostické: každý dict = {start_ts, end_ts, duration_s}, žádný důvod."""
    if since_ts is None:
        since_ts = 0.0
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % diary_db_path,
                               uri=True, timeout=3.0)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT start_ts, end_ts, duration_s, source FROM offline_windows "
            "WHERE start_ts >= ? ORDER BY start_ts DESC LIMIT ?",
            (since_ts, limit)).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    db = sys.argv[1] if len(sys.argv) > 1 else "data/hans_diary.db"
    added = populate_offline_windows(db)
    print("added:", added)
    print()
    print("=== last 10 offline windows ===")
    import datetime as _dt
    for w in get_windows(db, limit=10):
        s = _dt.datetime.fromtimestamp(w["start_ts"]).strftime("%Y-%m-%d %H:%M")
        h = w["duration_s"] / 3600
        print(f"  {s}  {h:5.1f}h  ({w['source']})")
