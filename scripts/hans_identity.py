#!/usr/bin/env python3
"""
HANS_IDENTITY_V1 — verzování identity (guard k Severce, frontier #1).

Changelog Hansova CORE: kdo byl → kdo je → proč. Dělá sebezměnu (Severka)
BEZPEČNOU a VRATNOU: každá změna CORE je verze s časem, zdrojem, odůvodněním
a stavem. Tok: propose (pending) → člověk schválí (approve = aplikuje) /
zamítne (reject). Rollback vrací dřívější verzi.

Tabulka identity_versions v hans_diary.db:
  id, ts, core, source, rationale, approved_by, status
  status: pending | active | superseded | rejected | rolled_back

Apply (approve/rollback) = (1) mutace běžícího config["persona"]["core"]
(projeví se hned v dalším chatu, persona_core čte config) + (2) zápis do
config.json (přežije restart). config.json se před zápisem snapshotuje.
Seed = při prvním běhu uloží stávající CORE jako verzi 1 (source='seed').

API:
  store = IdentityStore(config, diary_db_path, config_path="config.json")
  store.ensure_seed()                                   # verze 1 ze stávajícího CORE
  store.current() -> Version|None                       # aktivní
  store.propose(new_core, rationale, source='severka') -> id   # pending
  store.pending() -> [Version]
  store.approve(version_id, approved_by) -> bool        # aktivuje + aplikuje
  store.reject(version_id, approved_by) -> bool
  store.rollback(to_version_id, approved_by) -> bool
  store.history(limit=20) -> [Version]
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import time
from typing import List, Optional

_log = logging.getLogger("hans_identity")

_SNAP_DIR = "data/patch_snapshots"


class Version:
    __slots__ = ("id", "ts", "core", "source", "rationale",
                 "approved_by", "status")

    def __init__(self, row):
        self.id = row["id"]
        self.ts = row["ts"] or 0.0
        self.core = row["core"] or ""
        self.source = row["source"] or ""
        self.rationale = row["rationale"] or ""
        self.approved_by = row["approved_by"] or ""
        self.status = row["status"] or ""

    def as_dict(self) -> dict:
        return {
            "id": self.id, "ts": self.ts, "core": self.core,
            "source": self.source, "rationale": self.rationale,
            "approved_by": self.approved_by, "status": self.status,
        }

    def __repr__(self):
        return f"<Version {self.id} {self.status} {self.source} {self.core[:40]!r}>"


class IdentityStore:
    def __init__(self, config: dict, diary_db_path: str,
                 config_path: str = "config.json"):
        self._config = config if config is not None else {}
        self._diary_path = diary_db_path
        self._config_path = config_path
        self._init_db()

    # ── DB ─────────────────────────────────────────────────────────────────
    def _init_db(self):
        with sqlite3.connect(self._diary_path) as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS identity_versions (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts          REAL NOT NULL,
                    core        TEXT NOT NULL,
                    source      TEXT NOT NULL DEFAULT 'severka',
                    rationale   TEXT DEFAULT '',
                    approved_by TEXT DEFAULT '',
                    status      TEXT NOT NULL DEFAULT 'pending'
                )
            """)
            db.commit()

    def _connect(self):
        conn = sqlite3.connect(self._diary_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _config_core(self) -> str:
        return (self._config.get("persona", {}) or {}).get("core", "")

    # ── Seed ─────────────────────────────────────────────────────────────────
    def ensure_seed(self) -> Optional[int]:
        """Při prázdné historii uloží stávající CORE jako verzi 1 (active)."""
        try:
            conn = self._connect()
            try:
                n = conn.execute(
                    "SELECT COUNT(*) FROM identity_versions").fetchone()[0]
                if n:
                    return None
                core = self._config_core()
                if not core:
                    _log.warning("ensure_seed: config nemá persona.core, skip")
                    return None
                cur = conn.execute(
                    "INSERT INTO identity_versions "
                    "(ts, core, source, rationale, approved_by, status) "
                    "VALUES (?,?,?,?,?,?)",
                    (time.time(), core, "seed",
                     "Výchozí identita z config.json", "system", "active"))
                conn.commit()
                _log.info("identity seed: verze %s (active) z config CORE",
                          cur.lastrowid)
                return cur.lastrowid
            finally:
                conn.close()
        except Exception as e:
            _log.warning("ensure_seed failed: %s", e)
            return None

    # ── Čtení ─────────────────────────────────────────────────────────────────
    def current(self) -> Optional[Version]:
        try:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT * FROM identity_versions WHERE status='active' "
                    "ORDER BY ts DESC LIMIT 1").fetchone()
                return Version(row) if row else None
            finally:
                conn.close()
        except Exception as e:
            _log.warning("current failed: %s", e)
            return None

    def pending(self) -> List[Version]:
        return self._by_status("pending")

    def _by_status(self, status: str, limit: int = 50) -> List[Version]:
        try:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT * FROM identity_versions WHERE status=? "
                    "ORDER BY ts DESC LIMIT ?", (status, limit)).fetchall()
                return [Version(r) for r in rows]
            finally:
                conn.close()
        except Exception as e:
            _log.warning("_by_status(%s) failed: %s", status, e)
            return []

    def history(self, limit: int = 20) -> List[Version]:
        try:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT * FROM identity_versions "
                    "ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
                return [Version(r) for r in rows]
            finally:
                conn.close()
        except Exception as e:
            _log.warning("history failed: %s", e)
            return []

    def get(self, version_id: int) -> Optional[Version]:
        try:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT * FROM identity_versions WHERE id=?",
                    (version_id,)).fetchone()
                return Version(row) if row else None
            finally:
                conn.close()
        except Exception as e:
            _log.warning("get failed: %s", e)
            return None

    # ── Zápis / workflow ───────────────────────────────────────────────────
    def propose(self, new_core: str, rationale: str = "",
                source: str = "severka") -> Optional[int]:
        """Vloží návrh nového CORE jako 'pending'. NIC neaplikuje."""
        new_core = (new_core or "").strip()
        if not new_core:
            return None
        try:
            conn = self._connect()
            try:
                cur = conn.execute(
                    "INSERT INTO identity_versions "
                    "(ts, core, source, rationale, approved_by, status) "
                    "VALUES (?,?,?,?,?,'pending')",
                    (time.time(), new_core, source, (rationale or "").strip(), ""))
                conn.commit()
                _log.info("identity propose: verze %s (pending) source=%s",
                          cur.lastrowid, source)
                return cur.lastrowid
            finally:
                conn.close()
        except Exception as e:
            _log.warning("propose failed: %s", e)
            return None

    def _apply_core(self, new_core: str) -> bool:
        """Mutace běžícího configu + zápis config.json (snapshot předem)."""
        try:
            # 1) živá mutace (persona_core čte config za běhu)
            self._config.setdefault("persona", {})["core"] = new_core
            # 2) persist na disk
            if self._config_path and os.path.exists(self._config_path):
                with open(self._config_path, encoding="utf-8") as fh:
                    disk = json.load(fh)
                os.makedirs(_SNAP_DIR, exist_ok=True)
                snap = f"{_SNAP_DIR}/config.json.{int(time.time())}.bak"
                shutil.copy2(self._config_path, snap)
                disk.setdefault("persona", {})["core"] = new_core
                with open(self._config_path, "w", encoding="utf-8") as fh:
                    json.dump(disk, fh, indent=2, ensure_ascii=False)
                _log.info("identity apply: config.json zapsán (záloha %s)", snap)
            return True
        except Exception as e:
            _log.warning("_apply_core failed: %s", e)
            return False

    def approve(self, version_id: int, approved_by: str = "user") -> bool:
        """Schválí pending verzi: aplikuje CORE + aktivuje, předchozí -> superseded."""
        v = self.get(version_id)
        if not v or v.status != "pending":
            _log.warning("approve: verze %s není pending", version_id)
            return False
        if not self._apply_core(v.core):
            return False
        try:
            conn = self._connect()
            try:
                conn.execute(
                    "UPDATE identity_versions SET status='superseded' "
                    "WHERE status='active'")
                conn.execute(
                    "UPDATE identity_versions SET status='active', "
                    "approved_by=?, ts=? WHERE id=?",
                    (approved_by, time.time(), version_id))
                conn.commit()
                _log.info("identity APPROVE: verze %s aktivní (schválil %s)",
                          version_id, approved_by)
                return True
            finally:
                conn.close()
        except Exception as e:
            _log.warning("approve commit failed: %s", e)
            return False

    def reject(self, version_id: int, approved_by: str = "user") -> bool:
        v = self.get(version_id)
        if not v or v.status != "pending":
            return False
        try:
            conn = self._connect()
            try:
                conn.execute(
                    "UPDATE identity_versions SET status='rejected', approved_by=? "
                    "WHERE id=?", (approved_by, version_id))
                conn.commit()
                _log.info("identity REJECT: verze %s zamítnuta", version_id)
                return True
            finally:
                conn.close()
        except Exception as e:
            _log.warning("reject failed: %s", e)
            return False

    def rollback(self, to_version_id: int, approved_by: str = "user") -> bool:
        """Vrátí dřívější verzi: re-aplikuje její CORE, ona -> active,
        současná active -> rolled_back."""
        v = self.get(to_version_id)
        if not v:
            return False
        if not self._apply_core(v.core):
            return False
        try:
            conn = self._connect()
            try:
                conn.execute(
                    "UPDATE identity_versions SET status='rolled_back' "
                    "WHERE status='active'")
                conn.execute(
                    "UPDATE identity_versions SET status='active', "
                    "approved_by=?, ts=? WHERE id=?",
                    (approved_by, time.time(), to_version_id))
                conn.commit()
                _log.info("identity ROLLBACK: zpět na verzi %s", to_version_id)
                return True
            finally:
                conn.close()
        except Exception as e:
            _log.warning("rollback failed: %s", e)
            return False


# ── Smoke (python3 -m scripts.hans_identity) ─────────────────────────────────
if __name__ == "__main__":
    import tempfile
    cfg = {}
    try:
        with open("config.json", encoding="utf-8") as fh:
            cfg = json.load(fh)
    except Exception as exc:  # noqa
        print("WARN: config.json nenačten (%s)" % exc)

    # Smoke na DOČASNÉ kopii DB i config — reálná data se nedotkneme.
    tmp_db = tempfile.mktemp(suffix=".db")
    tmp_cfg = tempfile.mktemp(suffix=".json")
    with open(tmp_cfg, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2, ensure_ascii=False)

    st = IdentityStore(cfg, tmp_db, config_path=tmp_cfg)
    print("seed ->", st.ensure_seed())
    print("current ->", st.current())
    pid = st.propose(
        "Jsi Hans, bývalý majordomus, který našel zálibu ve filozofii času.",
        rationale="Tendence: silné úvahy o plynutí času; napětí kolem předvídatelnosti.",
        source="severka")
    print("propose ->", pid, "| pending:", st.pending())
    print("approve ->", st.approve(pid, "alice"))
    print("current po approve ->", st.current())
    print("config (živá mutace) core ->", cfg["persona"]["core"][:60], "...")
    print("history:")
    for h in st.history():
        print("  ", h)
    os.remove(tmp_db); os.remove(tmp_cfg)
    print("smoke OK (tmp DB i config smazány, reálná data netknuta)")
