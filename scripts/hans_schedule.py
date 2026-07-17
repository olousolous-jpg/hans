"""HANS_SCHEDULE_V1 — Hansův vlastní deklarativní rozvrh rutin (jeden zdroj).

Motivace ([[hans-own-schedule-declarative]] + [[robustness-silent-failure-audit]]):
Hans má víc autonomních rutin (analytika, reflexe, studium, catchup, guard),
dnes rozeseté v `config.json` + `_PHASE_SCHEDULE` + systemd timerech. Tři nezávislé
zdroje = nikdo neví, jestli rutina naposledy proběhla. Doložený případ: studium
„Design" 8/12 stojí 14 dní, `HANS_STUDY_SEQUENTIAL_V1` ho správně vybírá, ale
session se nespouští — a nikdo si toho nevšiml.

Tento modul přidává KONTROLNÍ VRSTVU (razítko + audit), NE náhradu za timery.
Každý autonomní tick zavolá `mark(name)`; `hans_health` pak ověří freshness.

TABULKA `hans_schedule`:
    name              — logický název rutiny (PK), např. 'nightly_analytics'
    kind              — 'periodic' | 'daily' | 'phase' (info, pro UI)
    period_s          — u periodic: cílová perioda (informativní)
    hour              — u daily/phase: cílová hodina (0-23)
    expected_gap_s    — MAX povolený gap; sebe-audit hlásí, když jde přes
    last_run_ts       — poslední úspěšný tick (0.0 = nikdy)
    last_run_ok       — 1 = OK, 0 = poslední tick byl SKIP (brain_down apod.)
    last_skip_reason  — když ok=0, proč (např. 'brain_down', 'game_mode')
    enabled           — 1 = zapnuto; 0 = vypnuto (audit ignoruje)
    note              — lidský popis (co ta rutina dělá)
    updated_ts        — kdy se ROW naposledy měnil (jakkoli)

MVP scope: READ-ONLY pro Hanse. NL editace („přesuň analytiku na 1:00")
přijde v kroku 4 s POTVRZENÍM + validací.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from typing import Optional

_log = logging.getLogger(__name__)

# Seed rutin — pro každou: (name, kind, period_s, hour, expected_gap_s, note).
# expected_gap_s = 2× nominální period + malý buffer, ať drobný jitter nehlásí.
# `hour` u periodic = None; `period_s` u daily = None.
_SEED = [
    # Noční analytika (deepseek reasoning tier přes noc, 3:00).
    ("nightly_analytics", "daily", None, 3, 30 * 3600,
     "Noční analytika (deepseek reasoning tier): syntéza, sebekritika, stance"),
    # Ranní reflexe (souhrn co bylo, kam dál) — na začátku fáze morning ~6:00.
    ("morning_reflection", "daily", None, 6, 30 * 3600,
     "Ranní reflexe: shrnutí noci, plán dne"),
    # Studium — jeden tick á cca 30 min (idle OODA vybere study slot).
    ("study_tick", "periodic", 30 * 60, None, 4 * 3600,
     "Studijní tick: postup v aktivním study_program"),
    # Zvědavost / čtení — periodic idle.
    ("curiosity_tick", "periodic", 30 * 60, None, 4 * 3600,
     "Zvědavý tick: čtení / prozkoumávání zájmů"),
    # Proton kalendář sync — každých 30 min (config.calendar.sync_interval_min).
    ("calendar_sync", "periodic", 30 * 60, None, 2 * 3600,
     "Sync Proton ICS kalendáře (nadcházející události)"),
    # Deferred catchup — po brain_up + po každém úspěšném čtení.
    # expected_gap NEmá tvrdý strop (může chybět celý den, když je málo pending);
    # nastaveno 25h → hlásí až po celém dni ticha.
    ("catchup_drain", "periodic", None, None, 25 * 3600,
     "Deferred pending catchup (po brain_up dojede backlog)"),
]


class ScheduleStore:
    def __init__(self, db_path: str):
        self._path = db_path
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self._path) as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS hans_schedule (
                    name             TEXT PRIMARY KEY,
                    kind             TEXT NOT NULL DEFAULT 'periodic',
                    period_s         REAL,
                    hour             INTEGER,
                    expected_gap_s   REAL NOT NULL,
                    last_run_ts      REAL NOT NULL DEFAULT 0,
                    last_run_ok      INTEGER NOT NULL DEFAULT 1,
                    last_skip_reason TEXT NOT NULL DEFAULT '',
                    enabled          INTEGER NOT NULL DEFAULT 1,
                    note             TEXT NOT NULL DEFAULT '',
                    updated_ts       REAL NOT NULL DEFAULT 0
                )
            """)
            # Seed idempotentně (INSERT OR IGNORE — nepřepisuje ruční změny).
            now = time.time()
            for name, kind, ps, hr, gap, note in _SEED:
                db.execute("""
                    INSERT OR IGNORE INTO hans_schedule
                    (name, kind, period_s, hour, expected_gap_s,
                     last_run_ts, last_run_ok, last_skip_reason,
                     enabled, note, updated_ts)
                    VALUES (?,?,?,?,?,0,1,'',1,?,?)
                """, (name, kind, ps, hr, gap, note, now))
            db.commit()

    # ── zápis (autonomní subsystémy) ─────────────────────────────────────────
    def mark(self, name: str, ok: bool = True,
             skip_reason: str = "") -> None:
        """Zapiš, že rutina teď proběhla.
        ok=False + skip_reason='brain_down' = rutina si všimla důvodu skip
        a NEbude to počítat jako úspěšný běh (last_run_ok=0).
        POKUD subsystém běžel jen částečně / degradovaně, dej ok=True s
        reason='' — audit tě nechá být, dokud freshness drží.
        """
        now = time.time()
        try:
            with sqlite3.connect(self._path, timeout=3.0) as db:
                cur = db.execute(
                    "UPDATE hans_schedule SET last_run_ts=?, last_run_ok=?, "
                    "last_skip_reason=?, updated_ts=? WHERE name=?",
                    (now, 1 if ok else 0, skip_reason[:120], now, name))
                if cur.rowcount == 0:
                    _log.debug("hans_schedule.mark: neznámá rutina '%s' "
                               "(seed ji neobsahuje) — ignoruji", name)
                db.commit()
        except Exception as e:
            _log.warning("hans_schedule.mark(%s) selhalo: %s", name, e)

    # ── čtení (dashboard / audit / /zdravi) ──────────────────────────────────
    def get(self, name: str) -> Optional[dict]:
        try:
            with sqlite3.connect(
                    "file:%s?mode=ro" % self._path, uri=True, timeout=3.0) as db:
                db.row_factory = sqlite3.Row
                r = db.execute(
                    "SELECT * FROM hans_schedule WHERE name=?", (name,)
                ).fetchone()
                return dict(r) if r else None
        except Exception:
            return None

    def all(self) -> list[dict]:
        try:
            with sqlite3.connect(
                    "file:%s?mode=ro" % self._path, uri=True, timeout=3.0) as db:
                db.row_factory = sqlite3.Row
                rows = db.execute(
                    "SELECT * FROM hans_schedule ORDER BY name"
                ).fetchall()
                return [dict(r) for r in rows]
        except Exception:
            return []

    def is_stale(self, name: str, now: Optional[float] = None) -> bool:
        """True = rutina si zaslouží pozornost. Nikdy neběžela → True
        až po expected_gap_s od NAINSTALOVÁNÍ (updated_ts)."""
        r = self.get(name)
        if not r or not r["enabled"]:
            return False
        n = now if now is not None else time.time()
        last = r["last_run_ts"] or r["updated_ts"]  # nikdy → měř od instalace
        return (n - last) > r["expected_gap_s"]

    def stale_list(self, now: Optional[float] = None) -> list[dict]:
        """Vrátí seznam ZASTARALÝCH rutin s late_s (kolik po termínu).
        Používá hans_health / /zdravi / behaviorální sebe-audit."""
        n = now if now is not None else time.time()
        out = []
        for r in self.all():
            if not r["enabled"]:
                continue
            last = r["last_run_ts"] or r["updated_ts"]
            late = n - last - r["expected_gap_s"]
            if late > 0:
                out.append({
                    "name": r["name"],
                    "late_s": late,
                    "last_run_ts": r["last_run_ts"],
                    "expected_gap_s": r["expected_gap_s"],
                    "last_skip_reason": r["last_skip_reason"],
                    "note": r["note"],
                })
        out.sort(key=lambda x: -x["late_s"])
        return out

    def stale_report(self, now: Optional[float] = None) -> str:
        """Human-readable — pro /zdravi kartu."""
        st = self.stale_list(now)
        if not st:
            return "Rozvrh: všechny rutiny běží podle plánu."
        lines = []
        for x in st:
            hrs = x["late_s"] / 3600
            reason = f" (posl. skip: {x['last_skip_reason']})" \
                if x["last_skip_reason"] else ""
            lines.append(f"  • {x['name']} — {hrs:.1f}h po termínu"
                         f" (max gap {x['expected_gap_s']/3600:.1f}h){reason}")
        return "Rozvrh — zaostávající rutiny:\n" + "\n".join(lines)


# ── Module-level shortcut ────────────────────────────────────────────────────
# Instrumentaci autonomních ticků chceme mít jednoduchou (JEDEN řádek per
# místo), bez rozvláčného passování store instancí. Singleton store proti
# defaultní DB path (`data/hans_diary.db`); lazy init, thread-safe.

import os as _os
import threading as _th

_SINGLETON: Optional[ScheduleStore] = None
_SINGLETON_LOCK = _th.Lock()
_DEFAULT_DB = _os.path.join(_os.path.dirname(_os.path.dirname(
    _os.path.abspath(__file__))), "data", "hans_diary.db")


def _default_store() -> ScheduleStore:
    global _SINGLETON
    if _SINGLETON is None:
        with _SINGLETON_LOCK:
            if _SINGLETON is None:
                _SINGLETON = ScheduleStore(_DEFAULT_DB)
    return _SINGLETON


def mark(name: str, ok: bool = True, skip_reason: str = "") -> None:
    """Zápis „rutina teď proběhla" proti defaultní DB. Bezpečné — tiché při chybě."""
    try:
        _default_store().mark(name, ok=ok, skip_reason=skip_reason)
    except Exception as _e:
        _log.debug("hans_schedule.mark shortcut failed: %s", _e)


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    st = ScheduleStore("data/hans_diary.db")
    if len(sys.argv) > 1 and sys.argv[1] == "mark":
        st.mark(sys.argv[2], ok=True)
        print("marked:", sys.argv[2])
    print(st.stale_report())
    print()
    for r in st.all():
        print(f"  {r['name']:24s} last={r['last_run_ts']:.0f} "
              f"ok={r['last_run_ok']} gap={r['expected_gap_s']/3600:.1f}h "
              f"enabled={r['enabled']}")
