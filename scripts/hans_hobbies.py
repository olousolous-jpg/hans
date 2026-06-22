#!/usr/bin/env python3
"""
HANS_HOBBIES_V1 — Fáze 3d: vrstva koníčků (topic → koníček → povolání).

Mezi plochými tématy (interest_update, kauzy, dialogy, web, filmy) a identitou
(Severka) chyběla ABSTRAKCE + PERSISTENCE. 3d ji dodává:
  1. Sběr opakujících se témat ze VŠECH streamů (read-only).
  2. ZOBECNĚNÍ base LLM: konkrétní instance ('Cardiffský hrad', 'Conwy') →
     obecný koníček ('hrady a historická architektura'). Anti-duplikace:
     LLM dostane už známé koníčky, ať u shody reusne přesný název (jako
     STANCE_MERGE_VIA_EXTRACTOR_V1).
  3. Durable HobbyStore (zrcadlo StanceStore) — akumuluje evidence_count + age.
Severka pak čte DURABLE koníčky vedle stances → umožní vocational návrh identity
('historik se specializací na hrady' = koherentní postava, ne objekt).

Tabulka `hobbies` v hans_diary.db:
  name, name_norm, evidence_count, first_seen, last_seen, examples(JSON), status

API:
  store = HobbyStore(config, diary_db_path)
  store.add_or_reinforce(name, examples=None) -> id|None
  store.top_hobbies(limit=10) -> [Hobby]
  store.durable_hobbies(min_evidence, min_age_days, min_recent_days) -> [Hobby]
  distill_hobbies(config, diary_db_path) -> int   # noční krok (sběr+LLM+zápis)
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from typing import List, Optional

_log = logging.getLogger("hans_hobbies")

REINFORCE_NOOP = None
_WS = re.compile(r"\s+")


def _norm(s: str) -> str:
    return _WS.sub(" ", (s or "").strip().lower())


def _load_examples(raw) -> list:
    if not raw:
        return []
    try:
        v = json.loads(raw)
        return [str(x) for x in v] if isinstance(v, list) else []
    except Exception:
        return []


class Hobby:
    __slots__ = ("id", "name", "evidence_count", "first_seen", "last_seen",
                 "examples", "status")

    def __init__(self, row):
        self.id = row["id"]
        self.name = (row["name"] or "").strip()
        self.evidence_count = row["evidence_count"] or 0
        self.first_seen = row["first_seen"] or 0.0
        self.last_seen = row["last_seen"] or 0.0
        self.examples = _load_examples(row["examples"]) if "examples" in row.keys() else []
        self.status = row["status"] or "active"

    def age_days(self) -> int:
        return max(0, int((time.time() - self.first_seen) / 86400))

    def as_dict(self) -> dict:
        return {"id": self.id, "name": self.name,
                "evidence_count": self.evidence_count,
                "age_days": self.age_days(), "examples": list(self.examples)}

    def __repr__(self):
        return f"<Hobby {self.id} n={self.evidence_count} {self.name[:40]!r}>"


class HobbyStore:
    def __init__(self, config: dict, diary_db_path: str):
        self._diary_path = diary_db_path
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self._diary_path) as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS hobbies (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    name            TEXT NOT NULL,
                    name_norm       TEXT NOT NULL,
                    evidence_count  INTEGER NOT NULL DEFAULT 1,
                    first_seen      REAL NOT NULL,
                    last_seen       REAL NOT NULL,
                    examples        TEXT,
                    status          TEXT NOT NULL DEFAULT 'active'
                )
            """)
            db.execute("CREATE INDEX IF NOT EXISTS idx_hobbies_norm ON hobbies(name_norm)")
            db.commit()

    def _connect(self):
        conn = sqlite3.connect(self._diary_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        return conn

    def add_or_reinforce(self, name: str, examples: list = None) -> Optional[int]:
        norm = _norm(name)
        if not norm:
            return None
        now = time.time()
        examples = [str(e).strip() for e in (examples or []) if str(e).strip()]
        try:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT id, examples FROM hobbies WHERE name_norm=? "
                    "AND status='active' ORDER BY id LIMIT 1", (norm,)).fetchone()
                if row is None:
                    conn.execute(
                        "INSERT INTO hobbies (name, name_norm, evidence_count, "
                        "first_seen, last_seen, examples, status) "
                        "VALUES (?,?,1,?,?,?,'active')",
                        (name.strip(), norm, now, now,
                         json.dumps(examples, ensure_ascii=False)))
                    conn.commit()
                    rid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                    _log.info("hobby NEW [%s]: %.50s", rid, name)
                    return rid
                rid = row["id"]
                merged = _load_examples(row["examples"])
                seen = {_norm(x) for x in merged}
                for e in examples:
                    if _norm(e) not in seen:
                        merged.append(e); seen.add(_norm(e))
                conn.execute(
                    "UPDATE hobbies SET evidence_count=evidence_count+1, last_seen=?, "
                    "examples=? WHERE id=?",
                    (now, json.dumps(merged[:20], ensure_ascii=False), rid))
                conn.commit()
                _log.info("hobby REINFORCE [%s] (n+1): %.50s", rid, name)
                return rid
            finally:
                conn.close()
        except Exception as e:
            _log.warning("HobbyStore.add_or_reinforce failed: %s", e)
            return None

    def top_hobbies(self, limit: int = 10) -> List[Hobby]:
        try:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT * FROM hobbies WHERE status='active' "
                    "ORDER BY evidence_count DESC, last_seen DESC LIMIT ?",
                    (limit,)).fetchall()
                return [Hobby(r) for r in rows]
            finally:
                conn.close()
        except Exception as e:
            _log.warning("top_hobbies failed: %s", e)
            return []

    def durable_hobbies(self, min_evidence: int = 8, min_age_days: int = 21,
                        min_recent_days: int = 14) -> List[Hobby]:
        """Koníčky, které prošly filtrem stálosti (pro Severku)."""
        now = time.time()
        min_first = now - min_age_days * 86400
        min_last = now - min_recent_days * 86400
        try:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT * FROM hobbies WHERE status='active' "
                    "AND evidence_count >= ? AND first_seen <= ? AND last_seen >= ? "
                    "ORDER BY evidence_count DESC",
                    (min_evidence, min_first, min_last)).fetchall()
                return [Hobby(r) for r in rows]
            finally:
                conn.close()
        except Exception as e:
            _log.warning("durable_hobbies failed: %s", e)
            return []


# ── Sběr témat ze všech streamů (read-only) ─────────────────────────────────
def _topic_from_teddy_note(note: str) -> str:
    first = (note or "").split("\n", 1)[0].strip()
    low = first.lower()
    if low.startswith("téma:") or low.startswith("tema:"):
        return first.split(":", 1)[1].strip()
    return ""


def gather_topics(diary_db_path: str, window_days: int = 30,
                  min_count: int = 3) -> List[tuple]:
    """Vrátí [(téma, count)] opakujících se témat napříč streamy. Read-only."""
    since = time.time() - window_days * 86400
    counts: dict = {}

    def _bump(topic: str, by: int = 1):
        t = (topic or "").strip()
        if len(t) < 3:
            return
        k = _norm(t)
        if k not in counts:
            counts[k] = [t, 0]
        counts[k][1] += by

    conn = None
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % diary_db_path, uri=True, timeout=5.0)
        # interest_update + kauzy: kurátorské → vždy zahrnout (váha 2)
        for note, in conn.execute(
                "SELECT note FROM diary WHERE event_type='interest_update' "
                "AND ts > ?", (since,)).fetchall():
            _bump(note, 2)
        for title, in conn.execute(
                "SELECT title FROM kolac_cases WHERE opened_at > ?",
                (since,)).fetchall():
            _bump(title, 2)
        # dialogy: téma z note
        for note, in conn.execute(
                "SELECT note FROM diary WHERE event_type='teddy_dialog' "
                "AND ts > ?", (since,)).fetchall():
            _bump(_topic_from_teddy_note(note), 1)
        # web/filmy/kodi: titulky
        for evt in ("web_read", "movie_browsed", "kodi_playing"):
            for title, in conn.execute(
                    "SELECT title FROM diary WHERE event_type=? AND ts > ?",
                    (evt, since)).fetchall():
                _bump(title, 1)
    except Exception as e:
        _log.warning("gather_topics failed: %s", e)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    out = [(v[0], v[1]) for v in counts.values() if v[1] >= min_count]
    out.sort(key=lambda x: x[1], reverse=True)
    return out[:40]


# PERSONA_NAME_CONFIGURABLE_V1 — {persona_name} se doplní z configu při .format()
_DISTILL_SYSTEM = (
    "Jsi analytik zájmů postavy jménem {persona_name}. Dostaneš seznam TÉMAT, kterými se {persona_name} opakovaně "
    "zabýval (s počtem výskytů), a seznam UŽ ZNÁMÝCH KONÍČKŮ. Seskup témata do "
    "OBECNÝCH, trvalých koníčků — ZOBECNI konkrétní instance (např. 'Cardiffský hrad', "
    "'Conwy' → 'hrady a historická architektura'; 'Krakatoa', 'Yellowstone' → "
    "'geologie a sopky'). Když téma odpovídá už známému koníčku, použij JEHO PŘESNÝ "
    "název (posílení, ne duplikát). NEVYMÝŠLEJ koníčky, které témata nepodporují; "
    "jednorázový šum vynech. Každý koníček je trvalý zájem, ne jednotlivá událost. "
    "Vrať VÝHRADNĚ JSON pole prvků s klíči: hobby (název koníčku) a examples "
    "(seznam konkrétních témat, která pod něj spadají)."
)


def distill_hobbies(config: dict, diary_db_path: str,
                    window_days: int = 30, min_count: int = 3) -> int:
    """Noční krok: sběr témat → base LLM zobecní na koníčky → HobbyStore.
    Vrací počet zpracovaných koníčků. LLM offline / parse fail → 0 (tichý skip)."""
    topics = gather_topics(diary_db_path, window_days, min_count)
    if not topics:
        _log.info("distill_hobbies: žádná opakující se témata, skip")
        return 0
    store = HobbyStore(config, diary_db_path)
    known = store.top_hobbies(limit=30)
    known_block = ""
    if known:
        known_block = ("UŽ ZNÁMÉ KONÍČKY (při shodě použij přesný název):\n"
                       + "\n".join(f"- {h.name}" for h in known) + "\n\n")
    topics_block = "\n".join(f"- {t} (×{c})" for t, c in topics)
    prompt = f"{known_block}TÉMATA:\n{topics_block}"

    cfg = (config.get("hobbies", {}) or {})
    er = config.get("evening_reflection", {}) or {}
    model = str(cfg.get("model", er.get("model", "jobautomation/OpenEuroLLM-Czech:latest")))
    timeout = int(cfg.get("llm_timeout", 300))
    try:
        from scripts.ollama_client import ollama_generate
    except ImportError:
        _log.warning("distill_hobbies: ollama_client nedostupný, skip")
        return 0
    try:
        from scripts.hans_persona import persona_name as _pn  # PERSONA_NAME_CONFIGURABLE_V1
        _system = _DISTILL_SYSTEM.format(persona_name=_pn(config))
        raw = ollama_generate(model=model, prompt=prompt, system=_system,
                              config=config, timeout=timeout,
                              keep_alive=0,  # MODEL_KEEPALIVE_TIERS_V1 — analytika on-demand
                              options={"temperature": 0.2})
    except Exception as e:
        _log.warning("distill_hobbies: LLM call failed: %s", e)
        return 0
    items = _parse_hobbies(raw)
    if not items:
        _log.info("distill_hobbies: 0 koníčků z LLM")
        return 0
    written = 0
    _max = int(cfg.get("max_per_run", 12))
    for it in items[:_max]:
        if not isinstance(it, dict):
            continue
        name = (it.get("hobby") or "").strip()
        if not name:
            continue
        ex = it.get("examples")
        ex = ex if isinstance(ex, list) else ([ex] if ex else [])
        if store.add_or_reinforce(name, ex):
            written += 1
    _log.info("distill_hobbies: zpracováno %d koníčků z %d témat", written, len(topics))
    return written


def _parse_hobbies(raw: str):
    s = re.sub(r"^```(?:json)?|```$", "", (raw or "").strip(), flags=re.MULTILINE).strip()
    i, j = s.find("["), s.rfind("]")
    if i == -1 or j == -1 or j < i:
        return []
    try:
        data = json.loads(s[i:j + 1])
    except Exception:
        return []
    return data if isinstance(data, list) else []


# ── Smoke (python3 -m scripts.hans_hobbies) ─────────────────────────────────
if __name__ == "__main__":
    cfg = {}
    try:
        with open("config.json", encoding="utf-8") as fh:
            cfg = json.load(fh)
    except Exception as exc:  # noqa
        print("WARN: config.json nenačten (%s)" % exc)
    db = cfg.get("diary_db", "data/hans_diary.db")
    print("=== gather_topics (read-only, všechny streamy, 30 dní) ===")
    for t, c in gather_topics(db):
        print(f"  ×{c:<3} {t}")
