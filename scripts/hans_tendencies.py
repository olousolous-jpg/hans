#!/usr/bin/env python3
"""
HANS_TENDENCIES_V1 — Fáze 3b: tendence z dat.

Deterministicky (BEZ LLM) čte dialektické postoje (tabulka stances) a odvozuje
TENDENCE = kam se Hansův charakter posouvá. Je to čistý, grounded vstup pro
3c Severku (tendence vs role → návrh změny system promptu).

Filozofie: datová vrstva musí být ČISTÁ (anti-konfabulace). Syntéza, porovnání
s rolí a LLM = až 3c. Tady jen hrubá klasifikace ze signálů store:
  confidence       — jak silně Hans postoj drží
  evidence_count   — kolikrát byl potvrzen
  counterargs      — má-li artikulovanou výhradu (dialektické napětí)
  last_seen        — pohnul se dnes?

Buckety (deterministicky, prahy laditelné přes config["tendencies"]):
  core      — pevně držené: conf >= strong_conf A evidence_count >= min_core_evidence
              (nebo prostě držené, když nespadnou jinam). Definují charakter.
              Příznak "s výhradou", když k nim Hans drží counterarg.
  tension   — v napětí: má counterargs A conf < strong_conf
              (výhrada převažuje / postoj byl reflexí oslaben → contradict()).
  emerging  — klíčící: evidence_count <= 1 NEBO conf < weak_conf.

API:
  derive_tendencies(config) -> Tendencies     # read-only, čistá funkce
  Tendencies.as_text() -> str                  # deterministický souhrn (pro deník / 3c)
  Tendencies.as_dict() -> dict                 # strukturovaně pro 3c
  snapshot(config, diary_db_path=None) -> str|None  # zapíše tendency_snapshot do deníku
"""
from __future__ import annotations

import logging
import sqlite3
import time
from datetime import datetime
from typing import List, Optional

_log = logging.getLogger("hans_tendencies")

# ── Prahy (fallback; laditelné přes config["tendencies"]) ───────────────────
STRONG_CONF = 0.75       # od kdy je postoj "pevně držený"
WEAK_CONF = 0.45         # pod tím je postoj "klíčící / slabý"
MIN_CORE_EVIDENCE = 3    # core musí být potvrzen aspoň tolikrát


# counterargs parser — sdílený se store; lokální fallback pro samostatný běh
try:
    from scripts.hans_stances import _load_cargs as _parse_cargs
except Exception:  # pragma: no cover
    import json as _json

    def _parse_cargs(raw) -> list:
        if not raw:
            return []
        try:
            v = _json.loads(raw)
            return [str(x) for x in v] if isinstance(v, list) else []
        except Exception:
            return []


class _S:
    """Lehký řádek postoje (read-only, mimo StanceStore — žádný write/migrace)."""
    __slots__ = ("id", "claim", "confidence", "evidence_count",
                 "last_seen", "counterargs", "bucket")

    def __init__(self, row):
        self.id = row["id"]
        self.claim = (row["claim"] or "").strip()
        self.confidence = row["confidence"] if row["confidence"] is not None else 0.5
        self.evidence_count = row["evidence_count"] or 0
        self.last_seen = row["last_seen"] or 0.0
        self.counterargs = _parse_cargs(row["counterargs"]) \
            if "counterargs" in row.keys() else []
        self.bucket = ""

    @property
    def has_carg(self) -> bool:
        return bool(self.counterargs)

    def as_dict(self) -> dict:
        return {
            "id": self.id, "claim": self.claim,
            "confidence": round(float(self.confidence), 2),
            "evidence_count": self.evidence_count,
            "counterargs": list(self.counterargs),
            "bucket": self.bucket,
        }


class Tendencies:
    """Výsledek 3b — grounded klasifikace postojů + dnešní pohyb."""

    def __init__(self, core: List[_S], tension: List[_S], emerging: List[_S],
                 moved_today: List[_S], date_str: str):
        self.core = core
        self.tension = tension
        self.emerging = emerging
        self.moved_today = moved_today
        self.date_str = date_str

    def is_empty(self) -> bool:
        return not (self.core or self.tension or self.emerging)

    def as_dict(self) -> dict:
        return {
            "date": self.date_str,
            "core": [s.as_dict() for s in self.core],
            "tension": [s.as_dict() for s in self.tension],
            "emerging": [s.as_dict() for s in self.emerging],
            "moved_today": [s.id for s in self.moved_today],
        }

    def as_text(self) -> str:
        """Deterministický souhrn — do deníku a jako vstup pro 3c."""
        if self.is_empty():
            return ""
        out = [f"Tendence z postojů (k {self.date_str}):"]

        def _line(s: _S, with_carg: bool = False) -> str:
            base = f"  - {s.claim} [conf {s.confidence:.2f}, ×{s.evidence_count}]"
            if with_carg and s.counterargs:
                base += f" — výhrada: {s.counterargs[-1]}"
            elif s.has_carg:
                base += " (s výhradou)"
            return base

        if self.core:
            out.append(f"Pevně držím ({len(self.core)}):")
            out += [_line(s) for s in self.core]
        if self.tension:
            out.append(f"V napětí / přehodnocuji ({len(self.tension)}):")
            out += [_line(s, with_carg=True) for s in self.tension]
        if self.emerging:
            out.append(f"Klíčící / nejisté ({len(self.emerging)}):")
            out += [_line(s) for s in self.emerging]
        if self.moved_today:
            claims = "; ".join(s.claim for s in self.moved_today)
            out.append(f"Dnes se pohnulo ({len(self.moved_today)}): {claims}")
        return "\n".join(out)


def _thresholds(config: dict):
    cfg = (config or {}).get("tendencies", {}) or {}
    return (
        float(cfg.get("strong_conf", STRONG_CONF)),
        float(cfg.get("weak_conf", WEAK_CONF)),
        int(cfg.get("min_core_evidence", MIN_CORE_EVIDENCE)),
    )


def _classify(s: _S, strong: float, weak: float, min_core: int) -> str:
    if s.evidence_count <= 1 or s.confidence < weak:
        return "emerging"
    if s.has_carg and s.confidence < strong:
        return "tension"
    return "core"


def derive_tendencies(config: dict, diary_db_path: Optional[str] = None,
                      date_str: Optional[str] = None) -> Tendencies:
    """READ-ONLY: z aktivních postojů odvodí tendence. Nikdy nepíše, nikdy
    nevyhazuje výjimku nahoru (vrátí prázdné Tendencies při chybě)."""
    db_path = diary_db_path or (config or {}).get("diary_db", "data/hans_diary.db")
    date_str = date_str or datetime.now().strftime("%Y-%m-%d")
    strong, weak, min_core = _thresholds(config)
    core: List[_S] = []
    tension: List[_S] = []
    emerging: List[_S] = []
    moved_today: List[_S] = []
    conn = None
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True, timeout=2.0)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM stances WHERE status='active' "
            "ORDER BY confidence DESC, evidence_count DESC, last_seen DESC"
        ).fetchall()
    except Exception as _e:
        _log.debug("derive_tendencies read failed: %s", _e)
        rows = []
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    for r in rows:
        s = _S(r)
        if not s.claim:
            continue
        s.bucket = _classify(s, strong, weak, min_core)
        {"core": core, "tension": tension, "emerging": emerging}[s.bucket].append(s)
        try:
            if datetime.fromtimestamp(s.last_seen).strftime("%Y-%m-%d") == date_str:
                moved_today.append(s)
        except Exception:
            pass

    return Tendencies(core, tension, emerging, moved_today, date_str)


def snapshot(config: dict, diary_db_path: Optional[str] = None,
             date_str: Optional[str] = None) -> Optional[str]:
    """Odvodí tendence a zapíše je do deníku jako event_type='tendency_snapshot'.
    Idempotentní pro den (přepíše dnešní snapshot). Vrací text nebo None."""
    db_path = diary_db_path or (config or {}).get("diary_db", "data/hans_diary.db")
    date_str = date_str or datetime.now().strftime("%Y-%m-%d")
    tnd = derive_tendencies(config, db_path, date_str)
    if tnd.is_empty():
        _log.info("tendency snapshot %s: žádné aktivní postoje, skip", date_str)
        return None
    text = tnd.as_text()
    try:
        db = sqlite3.connect(db_path, timeout=5.0)
        # idempotence: jeden snapshot na den
        db.execute(
            "DELETE FROM diary WHERE event_type='tendency_snapshot' "
            "AND date(ts,'unixepoch','localtime')=?", (date_str,))
        db.execute(
            "INSERT INTO diary (ts, event_type, title, note) VALUES (?,?,?,?)",
            (time.time(), "tendency_snapshot",
             f"Tendence z postojů — {date_str}", text))
        db.commit()
        db.close()
        _log.info("tendency snapshot %s: core=%d tension=%d emerging=%d moved_today=%d",
                  date_str, len(tnd.core), len(tnd.tension),
                  len(tnd.emerging), len(tnd.moved_today))
    except Exception as e:
        _log.warning("tendency snapshot zápis selhal: %s", e)
        return None
    return text


# ── Smoke (spustitelné: python3 -m scripts.hans_tendencies) ─────────────────
if __name__ == "__main__":
    import json
    cfg = {}
    try:
        with open("config.json", encoding="utf-8") as fh:
            cfg = json.load(fh)
    except Exception as exc:  # noqa
        print("WARN: config.json nenačten (%s), použiju defaulty" % exc)
    t = derive_tendencies(cfg)
    print("=== derive_tendencies().as_text() ===")
    print(t.as_text() or "(prázdné)")
    print("\n=== as_dict() ===")
    print(json.dumps(t.as_dict(), ensure_ascii=False, indent=2))
