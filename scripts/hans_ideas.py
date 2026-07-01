#!/usr/bin/env python3
"""hans_ideas.py — HANS_SYNTHESIS_IDEAS_V1

Hansovy VLASTNÍ NÁPADY / synteze: z vlastního popudu periodicky propojí věci,
které se v poslední době dozvěděl z RŮZNÝCH oblastí, do JEDNOHO nového postřehu —
ne shrnutí, ale nečekaná souvislost mezi tím, co spolu na první pohled nesouvisí.

Vzor = hans_study.py / hans_authorship.py (store + deferral-safe top-level).

Tok:
  - _gather_seeds: vybere z deníku pár bohatých „semínek" (reading_takeaway,
    study_note, book_completion_reflection) z RŮZNÝCH témat za posledních N dní;
    pro pestrost náhodně vybere z distinct-témat kandidátů.
  - generate_idea: LLM (Hansův hlas) najde mezi semínky vlastní postřeh; grounded
    POUZE v semínkách (anti-konfabulace), anti-repetice proti dřívějším nápadům.
  - uloží: deník `synthesis_idea` + RAG hans_identita (= součást toho, jak Hans myslí).

Model = dialog hans-czech (Hansův hlas; materiál je injektovaný → nízké riziko
konfabulace). Konfigurovatelný.

Deferral-safe: LLM dole → vrací None/'deferred', guard se nenastaví, retry.
Potřebuje aspoň `min_topics` (2) různá témata, jinak 'idle' (z jednoho nelze
propojovat).
"""
from __future__ import annotations

import re
import time
import random
import logging
import sqlite3
from typing import List, Optional

_log = logging.getLogger("hans_ideas")


# ── Helpers ─────────────────────────────────────────────────────────────────
def _cfg(config: dict) -> dict:
    return config.get("synthesis", {}) or {}


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _model(config: dict) -> str:
    return (_cfg(config).get("model")
            or config.get("models", {}).get("dialog")
            or "hans-czech:latest")


def _base_url(config: dict) -> str:
    return config.get("openwebui_chat", {}).get(
        "base_url", "http://127.0.0.1:11434")


def _persona_name(config: dict) -> str:
    try:
        from scripts.hans_persona import persona_name
        return persona_name(config)
    except Exception:
        return "Hans"


def _topic_key(event_type: str, title: str) -> str:
    """Hrubé téma semínka pro deduplikaci (chceme RŮZNÁ témata).
    study_note title = 'Studium: Design — Teorie barev…' → 'design'."""
    t = title or ""
    if event_type == "study_note":
        t = re.sub(r"^\s*studium:\s*", "", t, flags=re.IGNORECASE)
        t = t.split("—")[0].split(":")[0]
    return _norm(t)


# ── Store ───────────────────────────────────────────────────────────────────
class IdeaStore:
    SEED_TYPES = ("reading_takeaway", "study_note",
                  "book_completion_reflection")

    def __init__(self, config: dict, diary_db_path: str):
        self.config = config
        self._diary_path = diary_db_path
        self._init_db()

    def _connect(self):
        return sqlite3.connect(self._diary_path, timeout=10.0)

    def _init_db(self):
        try:
            with self._connect() as db:
                db.execute("""
                    CREATE TABLE IF NOT EXISTS synthesis_idea (
                        id      INTEGER PRIMARY KEY AUTOINCREMENT,
                        ts      REAL NOT NULL,
                        topics  TEXT,
                        insight TEXT
                    )""")
                db.commit()
        except Exception as e:
            _log.warning("IdeaStore _init_db: %s", e)

    # ── seeds ────────────────────────────────────────────────────────────────
    def _gather_seeds(self, lookback_days: int, min_topics: int,
                      max_topics: int, max_seed_chars: int) -> List[dict]:
        """Vrať [{topic, text}] z RŮZNÝCH témat (náhodně vybráno pro pestrost).
        [] když < min_topics distinct témat."""
        since = time.time() - lookback_days * 86400
        ph = ",".join("?" for _ in self.SEED_TYPES)
        try:
            with self._connect() as db:
                rows = db.execute(
                    f"SELECT event_type,title,COALESCE(NULLIF(data,''),note) "
                    f"FROM diary WHERE event_type IN ({ph}) AND ts>=? "
                    f"ORDER BY ts DESC LIMIT 400",
                    (*self.SEED_TYPES, since)).fetchall()
        except Exception as e:
            _log.warning("_gather_seeds: %s", e)
            return []
        # nejnovější semínko per distinct téma
        by_topic: dict[str, dict] = {}
        for etype, title, content in rows:
            content = (content or "").strip()
            if len(content) < 40:
                continue
            key = _topic_key(etype, title or "")
            if not key or key in by_topic:
                continue
            by_topic[key] = {"topic": (title or key).strip(),
                             "text": content[:max_seed_chars]}
        cands = list(by_topic.values())
        if len(cands) < min_topics:
            return []
        n = min(max_topics, len(cands))
        return random.sample(cands, n)

    # ── recent insights (anti-repetice) ──────────────────────────────────────
    def recent_insights(self, limit: int = 8) -> List[str]:
        try:
            with self._connect() as db:
                rows = db.execute(
                    "SELECT insight FROM synthesis_idea "
                    "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            return [r[0] for r in rows if r and r[0]]
        except Exception:
            return []

    def latest(self) -> Optional[dict]:
        try:
            with self._connect() as db:
                row = db.execute(
                    "SELECT id,ts,topics,insight FROM synthesis_idea "
                    "ORDER BY id DESC LIMIT 1").fetchone()
            if not row:
                return None
            return {"id": row[0], "ts": row[1],
                    "topics": row[2], "insight": row[3]}
        except Exception:
            return None

    def all_ideas(self, limit: int = 20) -> List[dict]:
        try:
            with self._connect() as db:
                rows = db.execute(
                    "SELECT id,ts,topics,insight FROM synthesis_idea "
                    "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            return [{"id": r[0], "ts": r[1], "topics": r[2],
                     "insight": r[3]} for r in rows]
        except Exception:
            return []

    def _store(self, topics: str, insight: str):
        """Ulož postřeh do synthesis_idea (vlastní tabulka pro latest/anti-repetici)
        i do deníku (autobiografický event pro narativ/importance/RAG kurátory)."""
        now = time.time()
        try:
            with self._connect() as db:
                db.execute("INSERT INTO synthesis_idea (ts,topics,insight) "
                           "VALUES (?,?,?)", (now, topics, insight))
                db.execute("""CREATE TABLE IF NOT EXISTS diary (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL,
                    event_type TEXT NOT NULL, title TEXT, data TEXT, note TEXT)""")
                db.execute("INSERT INTO diary (ts,event_type,title,data) "
                           "VALUES (?,?,?,?)",
                           (now, "synthesis_idea", topics, insight))
                db.commit()
        except Exception as e:
            _log.warning("_store: %s", e)

    # ── generate ─────────────────────────────────────────────────────────────
    def generate_idea(self, config: dict, knowledge=None) -> Optional[dict]:
        """Vygeneruj a ulož jeden nápad. None když nelze (málo témat / LLM dole).
        {'topics':..., 'insight':...} při úspěchu."""
        c = _cfg(config)
        seeds = self._gather_seeds(
            lookback_days=int(c.get("lookback_days", 30)),
            min_topics=int(c.get("min_topics", 2)),
            max_topics=int(c.get("max_topics", 3)),
            max_seed_chars=int(c.get("max_seed_chars", 600)))
        if not seeds:
            return {"result": "idle"}
        insight = _generate_insight(config, seeds, self.recent_insights())
        if not insight or len(insight) < 30:
            _log.info("synthesis: postřeh se nevygeneroval (LLM dole?) — retry")
            return None
        topics = " × ".join(s["topic"] for s in seeds)
        self._store(topics, insight)
        if knowledge is not None:
            try:
                knowledge.upload(
                    collection_key=str(c.get("rag_collection", "hans_identita")),
                    doc_id=f"idea_{int(time.time())}",
                    title=f"Vlastní postřeh: {topics}", text=insight,
                    metadata={"kdy": time.strftime("%Y-%m-%d"),
                              "typ": "nápad"})
            except Exception:
                pass
        _log.info("synthesis: nový postřeh (%s): %s", topics, insight[:80])
        return {"result": "created", "topics": topics, "insight": insight}


# ── LLM ─────────────────────────────────────────────────────────────────────
def _generate_insight(config: dict, seeds: List[dict],
                      recent: List[str]) -> str:
    from scripts.ollama_client import ollama_chat
    c = _cfg(config)
    name = _persona_name(config)
    avoid = ""
    if recent:
        avoid = ("\n\nTyto myšlenky už jsi vyslovil — NEopakuj je ani jinými "
                 "slovy:\n" + "\n".join(f"- {r[:120]}" for r in recent[:6]))
    system = (
        f"Jsi {name}. Máš před sebou pár věcí, které ses v poslední době dozvěděl "
        "z RŮZNÝCH oblastí. Tvým úkolem je najít MEZI NIMI nečekanou souvislost — "
        "jeden vlastní postřeh, který tě napadne, když je dáš dohromady. NE shrnutí, "
        "NE výčet, ne převyprávění jednotlivostí, ale myšlenka, která spojuje to, co "
        "spolu na první pohled nesouvisí. 2-4 věty, tvým hlasem. Vyjdi POUZE z toho, "
        "co je níže — nevymýšlej fakta. Česky, bez emoji, bez nadpisu." + avoid)
    body = "\n\n".join(f"[{s['topic']}]\n{s['text']}" for s in seeds)
    user = ("Tady je, co jsem se dozvěděl z různých stran:\n\n" + body +
            "\n\nNajdi mezi tím jednu nečekanou souvislost — svůj vlastní postřeh.")
    try:
        raw = ollama_chat(
            _model(config),
            [{"role": "system", "content": system},
             {"role": "user", "content": user}],
            ollama_url=_base_url(config),
            options={"temperature": float(c.get("temperature", 0.85)),
                     "num_ctx": int(c.get("num_ctx", 8192)),
                     "num_predict": int(c.get("num_predict", 280))})
    except Exception as e:
        _log.warning("_generate_insight selhal: %s", e)
        return ""
    return (raw or "").strip()[:int(c.get("max_chars", 1200))]


# ── chat surfacing ──────────────────────────────────────────────────────────
def latest_idea_context(config: dict, diary_db_path: str,
                        max_age_days: int = 7) -> str:
    """Read-only: krátký kontext o posledním Hansově postřehu pro chat.
    '' když žádný / starší než max_age_days."""
    if not _cfg(config).get("enabled", True):
        return ""
    try:
        idea = IdeaStore(config, diary_db_path).latest()
    except Exception:
        return ""
    if not idea or not idea.get("insight"):
        return ""
    try:
        if (time.time() - float(idea.get("ts") or 0)) > max_age_days * 86400:
            return ""
    except Exception:
        pass
    return ("Nedávno mě napadlo, když jsem si propojil pár věcí z různých oblastí: "
            + idea["insight"].strip())


# ── Top-level ───────────────────────────────────────────────────────────────
def run_synthesis_session(config: dict, diary_db_path: str,
                          knowledge=None) -> str:
    """Jedna noční synteze. Kódy:
       'created'  — vznikl nový postřeh
       'idle'     — málo různých témat (z jednoho nelze propojovat)
       'deferred' — transientní selhání (Ollama dole) → retry."""
    if not _cfg(config).get("enabled", True):
        return "idle"
    try:
        store = IdeaStore(config, diary_db_path)
    except Exception as e:
        _log.warning("run_synthesis_session init: %s", e)
        return "deferred"
    res = store.generate_idea(config, knowledge=knowledge)
    if res is None:
        return "deferred"
    return res.get("result", "created")
