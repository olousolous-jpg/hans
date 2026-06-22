"""
encounter_tracker.py — runtime logika nad encounters tabulkou (T.3)

Marker: T3_ENCOUNTER_TRACKER_V1

Per-osoba encounter lifecycle:
  on_arrived(person, ts) → otevři encounter (nebo reuse pokud <reopen_threshold_s)
  on_left(person, ts)    → uzavři encounter (UPDATE ended_at)
  bump_sighting(person)  → n_sightings++
  bump_dialog(person)    → n_dialogs++

Při init: close-on-startup — všechny encounters s ended_at IS NULL se
uzavřou s ts=now a summary='přerušeno restartem'. Předpokládá že open
encounter existuje jen pokud Hans běžel a spadl.

Schéma předpokládá T.2 tabulku 'encounters' v hans_diary.db.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from typing import Optional

_log = logging.getLogger("encounter_tracker")

# Default: pokud osoba 'left' a do 5 min se vrátí, reuse encounter.
# Reálné prostředí: lidé chodí mimo záběr velmi často.
DEFAULT_REOPEN_THRESHOLD_S = 300.0


class EncounterTracker:
    """Lifecycle manager pro per-person encounters."""

    def __init__(
        self,
        diary_db_path: str,
        reopen_threshold_s: float = DEFAULT_REOPEN_THRESHOLD_S,
    ):
        self._db_path = diary_db_path
        self._reopen_s = float(reopen_threshold_s)
        self._lock = threading.Lock()
        self._close_orphan_encounters()
        _log.info(
            "EncounterTracker ready (db=%s, reopen_threshold=%.0fs)",
            self._db_path, self._reopen_s,
        )

    # ── Internal helpers ─────────────────────────────────────────────

    def _connect(self) -> sqlite3.Connection:
        c = sqlite3.connect(self._db_path)
        c.row_factory = sqlite3.Row
        return c

    def _close_orphan_encounters(self):
        """Při startu zavři všechny open encounters (Hans předtím spadl/restart)."""
        now = time.time()
        try:
            with self._connect() as db:
                cur = db.execute(
                    "SELECT id, person_id, started_at FROM encounters "
                    "WHERE ended_at IS NULL"
                )
                orphans = cur.fetchall()
                if orphans:
                    db.execute(
                        "UPDATE encounters "
                        "SET ended_at=?, summary='přerušeno restartem', updated_at=? "
                        "WHERE ended_at IS NULL",
                        (now, now),
                    )
                    db.commit()
                    _log.info(
                        "Closed %d orphan encounter(s) on startup: %s",
                        len(orphans),
                        [(r["person_id"], r["started_at"]) for r in orphans],
                    )
        except Exception as e:
            _log.error("Cleanup orphan encounters failed: %s", e)

    # ── Public API ───────────────────────────────────────────────────

    def on_arrived(self, person_id: str, ts: Optional[float] = None) -> int:
        """
        Otevři nový encounter nebo reuse poslední pokud byl recently uzavřen.

        Returns: encounter id (existing reopened nebo new).
        """
        if not person_id:
            return 0
        now = ts if ts is not None else time.time()

        with self._lock, self._connect() as db:
            # 1. Existuje už open encounter pro tuto osobu?
            row = db.execute(
                "SELECT id FROM encounters "
                "WHERE person_id=? AND ended_at IS NULL "
                "ORDER BY started_at DESC LIMIT 1",
                (person_id,),
            ).fetchone()
            if row is not None:
                _log.debug("on_arrived(%s): encounter %d already open",
                           person_id, row["id"])
                return row["id"]

            # 2. Existuje nedávno uzavřený encounter (<reopen threshold)?
            cutoff = now - self._reopen_s
            row = db.execute(
                "SELECT id, ended_at FROM encounters "
                "WHERE person_id=? AND ended_at IS NOT NULL AND ended_at >= ? "
                "ORDER BY ended_at DESC LIMIT 1",
                (person_id, cutoff),
            ).fetchone()
            if row is not None:
                eid = row["id"]
                db.execute(
                    "UPDATE encounters SET ended_at=NULL, updated_at=? WHERE id=?",
                    (now, eid),
                )
                db.commit()
                _log.info("on_arrived(%s): REOPEN encounter %d (gap %.1fs)",
                          person_id, eid, now - row["ended_at"])
                return eid

            # 3. Nový encounter
            cur = db.execute(
                "INSERT INTO encounters "
                "(person_id, started_at, created_at, updated_at) "
                "VALUES (?, ?, ?, ?)",
                (person_id, now, now, now),
            )
            db.commit()
            eid = cur.lastrowid
            _log.info("on_arrived(%s): NEW encounter %d", person_id, eid)
            return eid

    def on_left(self, person_id: str, ts: Optional[float] = None) -> bool:
        """Uzavři otevřený encounter. Returns encounter_id (truthy) nebo False/None.

        T6B: vrací id zavřeného encounteru (pro summary), nebo False pokud
        nebylo co zavřít. id je truthy, takže staré `if on_left(...)` funguje.
        """
        if not person_id:
            return False
        now = ts if ts is not None else time.time()

        with self._lock, self._connect() as db:
            row = db.execute(
                "SELECT id FROM encounters "
                "WHERE person_id=? AND ended_at IS NULL "
                "ORDER BY started_at DESC LIMIT 1",
                (person_id,),
            ).fetchone()
            if row is None:
                _log.debug("on_left(%s): žádný open encounter — ignore",
                           person_id)
                return False
            db.execute(
                "UPDATE encounters SET ended_at=?, updated_at=? WHERE id=?",
                (now, now, row["id"]),
            )
            db.commit()
            _eid = row["id"]
            _log.info("on_left(%s): closed encounter %d",
                      person_id, _eid)
            return _eid  # T6B_ENCOUNTER_SUMMARY_V1 — vrací id (truthy) místo True

    def bump_sighting(self, person_id: str) -> bool:
        """Inkrementuj n_sightings open encounteru. Returns True pokud bumped."""
        if not person_id:
            return False
        with self._lock, self._connect() as db:
            r = db.execute(
                "UPDATE encounters "
                "SET n_sightings = n_sightings + 1, updated_at=? "
                "WHERE person_id=? AND ended_at IS NULL",
                (time.time(), person_id),
            )
            db.commit()
            return r.rowcount > 0

    def bump_dialog(self, person_id: str) -> bool:
        """Inkrementuj n_dialogs open encounteru."""
        if not person_id:
            return False
        with self._lock, self._connect() as db:
            r = db.execute(
                "UPDATE encounters "
                "SET n_dialogs = n_dialogs + 1, updated_at=? "
                "WHERE person_id=? AND ended_at IS NULL",
                (time.time(), person_id),
            )
            db.commit()
            return r.rowcount > 0

    # ── Reads ────────────────────────────────────────────────────────

    def current(self, person_id: str) -> Optional[dict]:
        """Aktivní (open) encounter pro osobu, nebo None."""
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM encounters "
                "WHERE person_id=? AND ended_at IS NULL "
                "ORDER BY started_at DESC LIMIT 1",
                (person_id,),
            ).fetchone()
            return dict(row) if row else None

    def last(self, person_id: str, include_open: bool = False) -> Optional[dict]:
        """Poslední (nejnovější) encounter pro osobu. Default jen uzavřené."""
        sql = "SELECT * FROM encounters WHERE person_id=?"
        if not include_open:
            sql += " AND ended_at IS NOT NULL"
        sql += " ORDER BY started_at DESC LIMIT 1"
        with self._connect() as db:
            row = db.execute(sql, (person_id,)).fetchone()
            return dict(row) if row else None

    def get_by_id(self, encounter_id: int) -> Optional[dict]:
        """T6B — načti encounter podle id (pro summary generování)."""
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM encounters WHERE id=?", (encounter_id,)
            ).fetchone()
            return dict(row) if row else None

    def set_summary(self, encounter_id: int, summary: str) -> bool:
        """T6B — zapiš summary k encounteru."""
        with self._lock, self._connect() as db:
            r = db.execute(
                "UPDATE encounters SET summary=?, updated_at=? WHERE id=?",
                (summary, time.time(), encounter_id),
            )
            db.commit()
            return r.rowcount > 0

    def all_for(self, person_id: str, limit: int = 20) -> list[dict]:
        """N posledních encounterů pro osobu, DESC podle started_at."""
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM encounters WHERE person_id=? "
                "ORDER BY started_at DESC LIMIT ?",
                (person_id, int(limit)),
            ).fetchall()
            return [dict(r) for r in rows]
