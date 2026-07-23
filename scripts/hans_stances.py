#!/usr/bin/env python3
"""
STANCE_STORE_V1 — úložiště Hansových názorů (P1 stances).

Strukturovaný store tendencí pro Fázi 3b + personu. MIMO RAG (přímý read,
jako hans_goals / interest_update) — viz rozhodnutí o RAGu. Zrcadlí vzor HansGoals.

Tabulka `stances` v hans_diary.db:
  claim          — tvrzení/názor (jak ho Hans vyslovil)
  claim_norm     — normalizovaný klíč pro dedup/match (lower+strip+collapse ws)
  confidence     — 0..1; reinforcement při re-assertion (bayes-lite)
  evidence_count — kolikrát potvrzeno
  first/last_seen
  source         — odkud (default evening_reflection)
  status         — active / retracted
  counterargs    — JSON list (ODLOŽENÝ upgrade; teď NULL)

Bayes-lite: re-assertion = pozitivní evidence → confidence asymptoticky k CONF_CAP
(new = conf + (1-conf)*alpha). Plný bayes, protiargumenty a paraphrase-merge
(embeddings/LLM match) = pozdější kvalitativní upgrad. Kontradikce (snížení) = stub.

API:
  store = StanceStore(config, diary_db_path)
  store.add_or_reinforce(claim, confidence=0.5, source="evening_reflection") -> id|None
  store.top_stances(limit=5, min_confidence=0.0) -> [Stance]   # pro personu (read-only)
  store.all_stances(limit=100) -> [Stance]                     # inspekce / 3b
  store.count_active() -> int
"""
import sqlite3
import time
import re
import json
from typing import Optional, List

_log = __import__("scripts.logger", fromlist=["get_logger"]).get_logger("hans_stances")

REINFORCE_ALPHA = 0.15   # rychlost posilování confidence při re-assertion
CONF_CAP = 0.98          # strop — nikdy "absolutní jistota"
CONF_FLOOR = 0.02

_WS = re.compile(r"\s+")


def _clamp(x, lo: float = CONF_FLOOR, hi: float = CONF_CAP) -> float:
    try:
        x = float(x)
    except (TypeError, ValueError):
        return 0.5
    return max(lo, min(hi, x))


def _normalize(claim: str) -> str:
    return _WS.sub(" ", (claim or "").strip().lower())


# STANCE_DIALECTIC_V1 — counterargs jako JSON list (dedup dle normalizace)
def _load_cargs(raw) -> list:
    if not raw:
        return []
    try:
        v = json.loads(raw)
        return [str(x) for x in v] if isinstance(v, list) else []
    except Exception:
        return []

def _merge_carg(existing: list, new: str) -> list:
    new = (new or "").strip()
    if not new:
        return existing
    if _normalize(new) in {_normalize(x) for x in existing}:
        return existing
    return existing + [new]


class Stance:
    __slots__ = ("id", "claim", "confidence", "evidence_count",
                 "first_seen", "last_seen", "source", "status",
                 "counterargs")

    def __init__(self, row):
        self.id = row["id"]
        self.claim = row["claim"] or ""
        self.confidence = row["confidence"] if row["confidence"] is not None else 0.5
        self.evidence_count = row["evidence_count"] or 0
        self.first_seen = row["first_seen"] or 0.0
        self.last_seen = row["last_seen"] or 0.0
        self.source = row["source"] or ""
        self.status = row["status"] or "active"
        self.counterargs = (_load_cargs(row["counterargs"])
                            if "counterargs" in row.keys() else [])

    def __repr__(self):
        return f"<Stance {self.id} conf={self.confidence:.2f} n={self.evidence_count} {self.claim[:40]!r}>"


class StanceStore:
    def __init__(self, config: dict, diary_db_path: str):
        self._diary_path = diary_db_path
        cfg = (config or {}).get("stances", {}) or {}
        self._alpha = float(cfg.get("reinforce_alpha", REINFORCE_ALPHA))
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self._diary_path) as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS stances (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    claim           TEXT NOT NULL,
                    claim_norm      TEXT NOT NULL,
                    confidence      REAL NOT NULL DEFAULT 0.5,
                    evidence_count  INTEGER NOT NULL DEFAULT 1,
                    first_seen      REAL NOT NULL,
                    last_seen       REAL NOT NULL,
                    source          TEXT DEFAULT 'evening_reflection',
                    status          TEXT NOT NULL DEFAULT 'active'
                )
            """)
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_stances_norm ON stances(claim_norm)")
            # STANCE_COUNTERARGS — odložený upgrade (idempotentní migrace)
            try:
                db.execute("ALTER TABLE stances ADD COLUMN counterargs TEXT")
            except sqlite3.OperationalError:
                pass  # sloupec už existuje
            # STANCE_HISTORY_V1 — časová stopa confidence (graf vývoje postoje).
            db.execute("""
                CREATE TABLE IF NOT EXISTS stance_history (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    stance_id  INTEGER NOT NULL,
                    ts         REAL NOT NULL,
                    confidence REAL NOT NULL,
                    event      TEXT
                )
            """)
            db.execute("CREATE INDEX IF NOT EXISTS idx_stancehist_sid "
                       "ON stance_history(stance_id, ts)")
            # Seed: jednorázově aktuální hodnota jako první bod pro postoje bez
            # historie (minulost je přepsaná → tohle je kotva „od teď").
            db.execute(
                "INSERT INTO stance_history (stance_id, ts, confidence, event) "
                "SELECT id, last_seen, confidence, 'seed' FROM stances s "
                "WHERE NOT EXISTS (SELECT 1 FROM stance_history h "
                "                  WHERE h.stance_id = s.id)")
            db.commit()

    def _connect(self):
        conn = sqlite3.connect(self._diary_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _hist(conn, stance_id, ts, confidence, event):
        """STANCE_HISTORY_V1 — zapiš jeden bod vývoje confidence (append-only,
        v téže transakci co změna). Selhání nesmí shodit uložení postoje."""
        try:
            conn.execute(
                "INSERT INTO stance_history (stance_id, ts, confidence, event) "
                "VALUES (?,?,?,?)", (stance_id, ts, float(confidence), event))
        except Exception:
            pass

    def history(self, stance_id: int = None, limit: int = 2000):
        """READ-ONLY časová stopa confidence (pro graf). stance_id=None → vše,
        vzestupně dle času. Vrací [{stance_id, ts, confidence, event}]."""
        try:
            conn = self._connect()
            try:
                if stance_id is None:
                    rows = conn.execute(
                        "SELECT stance_id, ts, confidence, event FROM stance_history "
                        "ORDER BY ts ASC LIMIT ?", (limit,)).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT stance_id, ts, confidence, event FROM stance_history "
                        "WHERE stance_id=? ORDER BY ts ASC LIMIT ?",
                        (stance_id, limit)).fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()
        except Exception as e:
            _log.warning("StanceStore.history failed: %s", e)
            return []

    def add_or_reinforce(self, claim: str, confidence: float = 0.5,
                         source: str = "evening_reflection",
                         counterarg: str = None) -> Optional[int]:
        """Nový claim -> insert; existující (claim_norm match, active) -> reinforce
        (confidence asymptoticky k CONF_CAP, evidence_count++). Vrací id nebo None."""
        norm = _normalize(claim)
        if not norm:
            return None
        now = time.time()
        try:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT id, confidence, counterargs FROM stances "
                    "WHERE claim_norm=? AND status='active' ORDER BY id LIMIT 1",
                    (norm,)).fetchone()
                if row is None:
                    _cargs = (json.dumps(_merge_carg([], counterarg))
                              if counterarg else None)
                    cur = conn.execute(
                        "INSERT INTO stances "
                        "(claim, claim_norm, confidence, evidence_count, "
                        " first_seen, last_seen, source, status, counterargs) "
                        "VALUES (?,?,?,1,?,?,?,'active',?)",
                        (claim.strip(), norm, _clamp(confidence), now, now,
                         source, _cargs))
                    self._hist(conn, cur.lastrowid, now, _clamp(confidence), "new")
                    conn.commit()
                    _log.info("stance NEW [%s] conf=%.2f: %.60s",
                              cur.lastrowid, _clamp(confidence), claim)
                    return cur.lastrowid
                sid = row["id"]
                conf = row["confidence"] if row["confidence"] is not None else 0.5
                new_conf = _clamp(conf + (1.0 - conf) * self._alpha)
                if counterarg:
                    _merged = _merge_carg(_load_cargs(row["counterargs"]), counterarg)
                    conn.execute(
                        "UPDATE stances SET confidence=?, evidence_count=evidence_count+1, "
                        "last_seen=?, counterargs=? WHERE id=?",
                        (new_conf, now, json.dumps(_merged), sid))
                else:
                    conn.execute(
                        "UPDATE stances SET confidence=?, "
                        "evidence_count=evidence_count+1, last_seen=? WHERE id=?",
                        (new_conf, now, sid))
                self._hist(conn, sid, now, new_conf, "reinforce")
                conn.commit()
                _log.info("stance REINFORCE [%s] conf %.2f->%.2f: %.60s",
                          sid, conf, new_conf, claim)
                return sid
            finally:
                conn.close()
        except Exception as e:
            _log.warning("StanceStore.add_or_reinforce failed: %s", e)
            return None

    def contradict(self, target_claim: str, counter_claim: str = None,
                   source: str = "evening_reflection") -> Optional[int]:
        """STANCE_DIALECTIC_V1 — Hans v reflexi popřel dřívější postoj:
        sniž confidence (new=conf*(1-α)) + ulož protiargument. Vrací id|None."""
        norm = _normalize(target_claim)
        if not norm:
            return None
        now = time.time()
        try:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT id, confidence, counterargs FROM stances "
                    "WHERE claim_norm=? AND status='active' ORDER BY id LIMIT 1",
                    (norm,)).fetchone()
                if row is None:
                    return None
                sid = row["id"]
                conf = row["confidence"] if row["confidence"] is not None else 0.5
                new_conf = _clamp(conf * (1.0 - self._alpha))
                merged = _merge_carg(_load_cargs(row["counterargs"]), counter_claim)
                conn.execute(
                    "UPDATE stances SET confidence=?, last_seen=?, counterargs=? "
                    "WHERE id=?",
                    (new_conf, now, json.dumps(merged), sid))
                self._hist(conn, sid, now, new_conf, "contradict")
                conn.commit()
                _log.info("stance WEAKEN [%s] conf %.2f->%.2f (+counterarg): %.60s",
                          sid, conf, new_conf, target_claim)
                return sid
            finally:
                conn.close()
        except Exception as e:
            _log.warning("StanceStore.contradict failed: %s", e)
            return None

    def top_stances(self, limit: int = 5, min_confidence: float = 0.0) -> List[Stance]:
        """READ-ONLY: nejsilnější aktivní názory (pro personu)."""
        try:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT * FROM stances WHERE status='active' AND confidence>=? "
                    "ORDER BY confidence DESC, evidence_count DESC, last_seen DESC "
                    "LIMIT ?", (min_confidence, limit)).fetchall()
                return [Stance(r) for r in rows]
            finally:
                conn.close()
        except Exception as e:
            _log.warning("StanceStore.top_stances failed: %s", e)
            return []

    def all_stances(self, limit: int = 100) -> List[Stance]:
        try:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT * FROM stances ORDER BY last_seen DESC LIMIT ?",
                    (limit,)).fetchall()
                return [Stance(r) for r in rows]
            finally:
                conn.close()
        except Exception as e:
            _log.warning("StanceStore.all_stances failed: %s", e)
            return []

    def count_active(self) -> int:
        try:
            conn = self._connect()
            try:
                return conn.execute(
                    "SELECT COUNT(*) FROM stances WHERE status='active'").fetchone()[0]
            finally:
                conn.close()
        except Exception as e:
            _log.warning("StanceStore.count_active failed: %s", e)
            return 0