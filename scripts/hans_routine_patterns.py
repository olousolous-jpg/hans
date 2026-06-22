#!/usr/bin/env python3
"""HANS_ROUTINE_PATTERNS_V1 — proaktivní majordomus Fáze 2b: detekce rutin.

Deterministicky (BEZ LLM) z `person_seen` odvodí PRESENCE PROFIL osoby:
v kolika % DNÍ byla osoba viděna v dané hodině / dni v týdnu. Days-based
(ne počet detekcí) → jeden ukecaný den nezkreslí profil.

Použití (uživatel zvolil 17.6. „rutina jako timing/kontext", NE samostatná
promluva):
  1. TIMING gate proaktivity: `is_typical_time(person)` → proaktivní promluvu
     pustit jen v obvyklém čase přítomnosti (ne ve 3 ráno). None = málo dat →
     gate se NEaplikuje (fallback na stávající chování, bezpečné pro řídké osoby).
  2. KONTEXT do promptu: `summary(person)` → krátká česká věta o rutině.

Tabulka `person_routines` (cache, nočně přepočítaná `rebuild()`):
  person, bin_type('hour'|'dow'), bin(int), score(0..1), days_total, updated_ts

API:
  rs = RoutineStore(config, diary_db_path)
  rs.rebuild(window_days=30) -> dict             # noční precompute
  rs.presence_score(person, hour) -> float       # 0..1
  rs.is_typical_time(person, dt=None) -> bool|None
  rs.summary(person) -> str
"""
from __future__ import annotations

import logging
import sqlite3
import time
from datetime import datetime
from typing import Dict, Optional

_log = logging.getLogger("hans_routine_patterns")

_SKIP = {"unknown", "...", "?", ""}
_DOW_CZ = ["v neděli", "v pondělí", "v úterý", "ve středu",
           "ve čtvrtek", "v pátek", "v sobotu"]


def _norm(s: str) -> str:
    return (s or "").strip().lower()


class RoutineStore:
    def __init__(self, config: dict, diary_db_path: str):
        self.config = config or {}
        self._diary_path = diary_db_path
        self._cache: Optional[Dict] = None   # {person: {'hour':{h:score}, 'dow':{d:score}, 'days_total':n}}
        self._init_db()

    def _cfg(self) -> dict:
        return self.config.get("routine_patterns", {}) or {}

    def _init_db(self):
        try:
            with sqlite3.connect(self._diary_path) as db:
                db.execute("""
                    CREATE TABLE IF NOT EXISTS person_routines (
                        person     TEXT NOT NULL,
                        bin_type   TEXT NOT NULL,
                        bin        INTEGER NOT NULL,
                        score      REAL NOT NULL DEFAULT 0,
                        days_total INTEGER NOT NULL DEFAULT 0,
                        updated_ts REAL NOT NULL DEFAULT 0,
                        PRIMARY KEY (person, bin_type, bin)
                    )
                """)
                db.commit()
        except Exception as e:
            _log.warning("RoutineStore._init_db failed: %s", e)

    # ── precompute (nočně) ───────────────────────────────────────────────────
    def rebuild(self, window_days: Optional[int] = None) -> dict:
        """Z person_seen přepočítá presence profil per osoba. Read-then-write,
        deterministicky (žádný LLM) → bezpečné i přes den."""
        window_days = int(window_days or self._cfg().get("window_days", 30))
        since = time.time() - window_days * 86400
        persons = 0
        try:
            ro = sqlite3.connect("file:%s?mode=ro" % self._diary_path, uri=True,
                                 timeout=5.0)
        except Exception as e:
            _log.warning("rebuild: ro connect failed: %s", e)
            return {"persons": 0}
        rows = []
        try:
            # (person, den, hodina, dow) unikátní dny — days-based metrika
            cur = ro.execute(
                "SELECT lower(title) p, "
                "date(ts,'unixepoch','localtime') d, "
                "CAST(strftime('%H',datetime(ts,'unixepoch','localtime')) AS INT) h, "
                "CAST(strftime('%w',datetime(ts,'unixepoch','localtime')) AS INT) w "
                "FROM diary WHERE event_type='person_seen' AND ts > ? "
                "GROUP BY p, d, h", (since,))
            rows = cur.fetchall()
        except Exception as e:
            _log.warning("rebuild: query failed: %s", e)
        finally:
            try:
                ro.close()
            except Exception:
                pass

        # agregace v paměti
        agg: Dict[str, Dict] = {}
        for p, d, h, w in rows:
            p = _norm(p)
            if not p or p in _SKIP:
                continue
            a = agg.setdefault(p, {"days": set(), "hour": {}, "dow": {},
                                   "dow_days": {}})
            a["days"].add(d)
            a["hour"].setdefault(h, set()).add(d)
            a["dow"].setdefault(w, set()).add(d)

        now = time.time()
        out_rows = []
        for p, a in agg.items():
            days_total = len(a["days"])
            if days_total <= 0:
                continue
            for h, dset in a["hour"].items():
                out_rows.append((p, "hour", int(h), len(dset) / days_total,
                                 days_total, now))
            for w, dset in a["dow"].items():
                out_rows.append((p, "dow", int(w), len(dset),
                                 days_total, now))
            persons += 1

        try:
            with sqlite3.connect(self._diary_path, timeout=5.0) as db:
                db.execute("DELETE FROM person_routines")
                db.executemany(
                    "INSERT INTO person_routines "
                    "(person,bin_type,bin,score,days_total,updated_ts) "
                    "VALUES (?,?,?,?,?,?)", out_rows)
                db.commit()
        except Exception as e:
            _log.warning("rebuild: write failed: %s", e)
            return {"persons": 0}
        self._cache = None  # invalidate
        _log.info("routine_patterns: přepočítáno %d osob (%d binů, okno %dd)",
                  persons, len(out_rows), window_days)
        return {"persons": persons, "bins": len(out_rows)}

    # ── runtime čtení ──────────────────────────────────────────────────────────
    def _load(self) -> Dict:
        if self._cache is not None:
            return self._cache
        cache: Dict = {}
        try:
            conn = sqlite3.connect("file:%s?mode=ro" % self._diary_path, uri=True,
                                   timeout=5.0)
            try:
                for p, bt, b, sc, dt in conn.execute(
                        "SELECT person,bin_type,bin,score,days_total "
                        "FROM person_routines"):
                    e = cache.setdefault(p, {"hour": {}, "dow": {},
                                             "days_total": 0})
                    e["days_total"] = dt
                    e[bt][int(b)] = sc
            finally:
                conn.close()
        except Exception as e:
            _log.warning("RoutineStore._load failed: %s", e)
        self._cache = cache
        return cache

    def presence_score(self, person: str, hour: int) -> float:
        e = self._load().get(_norm(person))
        if not e:
            return 0.0
        return float(e["hour"].get(int(hour), 0.0))

    def is_typical_time(self, person: str,
                        dt: Optional[datetime] = None) -> Optional[bool]:
        """True/False zda je teď pro osobu typický čas přítomnosti.
        None = málo dat (gate se NEaplikuje → stávající chování)."""
        e = self._load().get(_norm(person))
        min_days = int(self._cfg().get("min_days", 7))
        if not e or e.get("days_total", 0) < min_days:
            return None
        dt = dt or datetime.now()
        min_score = float(self._cfg().get("typical_min_score", 0.35))
        return self.presence_score(person, dt.hour) >= min_score

    def summary(self, person: str) -> str:
        """Krátká česká věta o rutině osoby pro prompt. '' když málo dat."""
        e = self._load().get(_norm(person))
        min_days = int(self._cfg().get("min_days", 7))
        if not e or e.get("days_total", 0) < min_days:
            return ""
        thr = float(self._cfg().get("typical_min_score", 0.35))
        hours = sorted(h for h, s in e["hour"].items() if s >= thr)
        parts = []
        if hours:
            # souvislé okno (od první do poslední typické hodiny)
            lo, hi = hours[0], hours[-1]
            parts.append(f"obvykle bývá doma mezi {lo}–{hi}h")
        # víkend vs všední (dow uloženo jako počet dní)
        dow = e.get("dow", {})
        if dow:
            wknd = dow.get(0, 0) + dow.get(6, 0)          # ne + so
            week = sum(dow.get(d, 0) for d in (1, 2, 3, 4, 5))
            if wknd and week:
                if wknd / 2.0 > (week / 5.0) * 1.4:
                    parts.append("o víkendu více")
                elif (week / 5.0) > (wknd / 2.0) * 1.4:
                    parts.append("hlavně ve všední dny")
        if not parts:
            return ""
        return f"{person.capitalize()}: " + ", ".join(parts) + "."


# ── Smoke ────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    dbp = sys.argv[1] if len(sys.argv) > 1 else "data/hans_diary.db"
    rs = RoutineStore({}, dbp)
    print("rebuild:", rs.rebuild())
    for who in (sys.argv[2:] or ["alice", "bob", "carol"]):
        print(f"\n{who}: typical_now={rs.is_typical_time(who)}")
        print("  summary:", repr(rs.summary(who)))
        prof = rs._load().get(who.lower(), {})
        if prof:
            hrs = {h: round(s, 2) for h, s in sorted(prof["hour"].items())}
            print("  days_total:", prof["days_total"], "hour-score:", hrs)
