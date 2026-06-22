# -*- coding: utf-8 -*-
"""
HansGoals — Hansovy úkolové cíle (fáze 2 OODA).

Cíl = pojmenovaný směr, který na pár dní zaostří Hansův výběr aktivit
a vyústí v dílo (esej destilovaná z RAGu hans_cetba).

Cíle vznikají z pozorování vzorce ve vlastním čtení (2a, přijde příště).
Žijí target_days, pak buď uspějí (work_path != NULL) nebo se opustí
(status=abandoned).

Strukturně podobné kolac_cases, ale významově jiné:
  - Case = Koláčova narativní hříčka (záhada → stopy → teorie)
  - Goal = Hansovo soustředění (téma → sběr → destilace → dílo)
"""
import json
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Optional

_log = __import__("scripts.logger", fromlist=["get_logger"]).get_logger("hans_goals")


STATUS_ACTIVE     = "active"
STATUS_COMPLETED  = "completed"
STATUS_ABANDONED  = "abandoned"

TRIGGER_MANUAL        = "manual"
TRIGGER_STUCK_PATTERN = "stuck_pattern"


@dataclass
class Goal:
    """Hansův cíl — soustředění na téma s vyústěním v dílo."""
    id: int = 0
    topic: str = ""
    opened_at: float = 0.0
    target_days: int = 5
    closed_at: Optional[float] = None
    status: str = STATUS_ACTIVE
    trigger_source: str = TRIGGER_MANUAL
    material_count: int = 0
    work_path: Optional[str] = None

    def age_days(self) -> int:
        """Stáří v dnech (min 1, jako Case.age_days)."""
        return max(1, int((time.time() - self.opened_at) / 86400) + 1)

    def should_complete(self) -> bool:
        """True když cíl dospěl k vyústění (age >= target)."""
        return self.status == STATUS_ACTIVE and self.age_days() >= self.target_days

    def is_active(self) -> bool:
        return self.status == STATUS_ACTIVE


class HansGoals:
    """Manager Hansových cílů. Vlastní tabulka v deníkové DB."""

    def __init__(self, config: dict, diary_db_path: str):
        cfg = (config or {}).get("hans_goals", {}) or {}
        self._diary_path = diary_db_path
        self._max_active = int(cfg.get("max_active", 1))
        self._default_target_days = int(cfg.get("default_target_days", 5))
        self._init_db()
        _log.info("HansGoals ready — max_active=%d, default_target_days=%d",
                  self._max_active, self._default_target_days)

    # ── DB ────────────────────────────────────────────────────────────
    def _init_db(self):
        with sqlite3.connect(self._diary_path) as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS hans_goals (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    topic           TEXT NOT NULL,
                    opened_at       REAL NOT NULL,
                    target_days     INTEGER DEFAULT 5,
                    closed_at       REAL,
                    status          TEXT NOT NULL DEFAULT 'active',
                    trigger_source  TEXT DEFAULT 'manual',
                    material_count  INTEGER DEFAULT 0,
                    work_path       TEXT
                )
            """)
            # GOAL_CLOSE_2D6_V1 — outcome_reason (idempotentni migrace)
            try:
                db.execute("ALTER TABLE hans_goals ADD COLUMN outcome_reason TEXT")
            except sqlite3.OperationalError:
                pass  # sloupec uz existuje
            # GOAL_EXPECTED_VS_ACTUAL_V1 — expected (idempotentni migrace)
            try:
                db.execute("ALTER TABLE hans_goals ADD COLUMN expected TEXT")
            except sqlite3.OperationalError:
                pass  # sloupec uz existuje
            db.commit()

    def _row_to_goal(self, row) -> Goal:
        return Goal(
            id=row["id"],
            topic=row["topic"] or "",
            opened_at=row["opened_at"] or 0.0,
            target_days=row["target_days"] or 5,
            closed_at=row["closed_at"],
            status=row["status"] or STATUS_ACTIVE,
            trigger_source=row["trigger_source"] or TRIGGER_MANUAL,
            material_count=row["material_count"] or 0,
            work_path=row["work_path"],
        )

    # ── Read ──────────────────────────────────────────────────────────
    def get_active_goal(self) -> Optional[Goal]:
        """Vrátí jediný aktivní cíl (max_active=1) nebo None."""
        with sqlite3.connect(self._diary_path) as db:
            db.row_factory = sqlite3.Row
            row = db.execute(
                "SELECT * FROM hans_goals WHERE status=? "
                "ORDER BY opened_at DESC LIMIT 1",
                (STATUS_ACTIVE,)
            ).fetchone()
            return self._row_to_goal(row) if row else None

    def all_goals(self, limit: int = 50) -> list:
        """Všechny cíle (i uzavřené), nejnovější první."""
        with sqlite3.connect(self._diary_path) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute(
                "SELECT * FROM hans_goals ORDER BY opened_at DESC LIMIT ?",
                (limit,)
            ).fetchall()
            return [self._row_to_goal(r) for r in rows]

    def count_by_status(self) -> dict:
        """{status: count} — pro pozdější introspekci (kolik dotáhl vs opustil)."""
        with sqlite3.connect(self._diary_path) as db:
            rows = db.execute(
                "SELECT status, COUNT(*) FROM hans_goals GROUP BY status"
            ).fetchall()
            return {r[0]: r[1] for r in rows}

    # ── Write ─────────────────────────────────────────────────────────
    def open_goal(self, topic: str,
                  trigger_source: str = TRIGGER_MANUAL,
                  target_days: Optional[int] = None,
                  expected: Optional[str] = None) -> Optional[Goal]:  # GOAL_EXPECTED_VS_ACTUAL_V1
        """Otevře nový cíl. Respektuje max_active=1 (vrátí None když už běží).

        Rozhodnutí 4: když je živý cíl, NEZALOŽÍ nový (ignorovat).
        Vrátí None — volající si může zalogovat 'ignored'.
        """
        if self.get_active_goal() is not None:
            _log.info("open_goal '%s' ignorován — už běží jiný cíl", topic)
            return None
        if not topic or not topic.strip():
            return None
        topic = topic.strip()
        tgt = int(target_days) if target_days else self._default_target_days
        with sqlite3.connect(self._diary_path) as db:
            cur = db.execute(
                "INSERT INTO hans_goals "
                "(topic, opened_at, target_days, status, trigger_source, expected) "  # GOAL_EXPECTED_VS_ACTUAL_V1
                "VALUES (?, ?, ?, ?, ?, ?)",
                (topic, time.time(), tgt, STATUS_ACTIVE, trigger_source,
                 (expected or "").strip() or None)
            )
            goal_id = cur.lastrowid
            db.commit()
        _log.info("Nový cíl otevřen [%d]: '%s' (target_days=%d, trigger=%s)",
                  goal_id, topic, tgt, trigger_source)
        return self.get_active_goal()

    def close_goal(self, goal_id: int,
                   status: str,
                   work_path: Optional[str] = None,
                   outcome_reason: Optional[str] = None) -> bool:  # GOAL_CLOSE_2D6_V1
        """Uzavře cíl (status=completed nebo abandoned).

        Rozhodnutí 5: při selhání destilace volat se status=abandoned.
        """
        if status not in (STATUS_COMPLETED, STATUS_ABANDONED):
            _log.warning("close_goal: neplatný status '%s'", status)
            return False
        with sqlite3.connect(self._diary_path) as db:
            db.execute(
                "UPDATE hans_goals SET closed_at=?, status=?, work_path=?, "
                "outcome_reason=? WHERE id=?",  # GOAL_CLOSE_2D6_V1
                (time.time(), status, work_path, outcome_reason, goal_id)
            )
            db.commit()
        _log.info("Cíl [%d] uzavřen jako %s%s",
                  goal_id, status,
                  (" → " + work_path) if work_path else "")
        return True

    def get_expected(self, goal_id: int) -> str:  # GOAL_EXPECTED_VS_ACTUAL_V1
        """Vrátí uložený 'expected' cíle (prázdný string když chybí)."""
        with sqlite3.connect(self._diary_path) as db:
            row = db.execute(
                "SELECT expected FROM hans_goals WHERE id=?", (goal_id,)
            ).fetchone()
        return (row[0] if row and row[0] else "")

    def bump_material(self, goal_id: int, by: int = 1) -> bool:
        """Inkrementuje material_count (počet relevantních záznamů za období).

        Bude se volat z 2c (až bude detekce relevance — zatím manuální).
        """
        with sqlite3.connect(self._diary_path) as db:
            db.execute(
                "UPDATE hans_goals SET material_count = material_count + ? "
                "WHERE id=?",
                (int(by), goal_id)
            )
            db.commit()
        return True
