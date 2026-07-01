#!/usr/bin/env python3
"""hans_authorship.py — HANS_AUTHORSHIP_V1

Hansův vlastní AUTORSKÝ PROJEKT: dílo na pokračování (povídka / esej / průvodce),
které píše po nocích týdny do hotového artefaktu. Z vlastního popudu.

Vzor = hans_study.py (StudyStore):
  - vybere durable koníček (HobbyStore) → LLM vymyslí PLÁN díla (název/druh/námět/
    osnova sekcí) → 1 aktivní projekt
  - 1 noční session = napiš DALŠÍ sekci (grounded v RAG Hansových čtení/studia +
    návaznost na předchozí) → uloží do tabulky writing_section + deníku + RAG hans_dila
  - po poslední sekci → dovětek autora + složení celého díla do data/works/<slug>.md

Modely (split jako jinde):
  - PLÁN (strukturovaný JSON) = base OpenEuroLLM (čistší, keep_alive=0)
  - PRÓZA (Hansův hlas) = dialog model hans-czech (rezidentní)

Deferral-safe: když LLM dole → vrací None/'deferred', guard se nenastaví, retry.
"""
from __future__ import annotations

import json
import re
import time
import logging
import sqlite3
from pathlib import Path
from typing import List, Optional

_log = logging.getLogger("hans_authorship")


# ── Helpers ─────────────────────────────────────────────────────────────────
def _cfg(config: dict) -> dict:
    return config.get("authorship", {}) or {}


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _slug(s: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", (s or "dilo").lower().strip())
    return s.strip("_")[:60] or "dilo"


def _prose_model(config: dict) -> str:
    return (_cfg(config).get("model")
            or config.get("models", {}).get("dialog")
            or "hans-czech:latest")


def _plan_model(config: dict) -> str:
    return (_cfg(config).get("plan_model")
            or config.get("evening_reflection", {}).get("model")
            or "jobautomation/OpenEuroLLM-Czech:latest")


def _base_url(config: dict) -> str:
    return config.get("openwebui_chat", {}).get(
        "base_url", "http://127.0.0.1:11434")


def _persona_name(config: dict) -> str:
    try:
        from scripts.hans_persona import persona_name
        return persona_name(config)
    except Exception:
        return "Hans"


def _parse_json_obj(raw: str) -> Optional[dict]:
    if not raw:
        return None
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


# ── LLM: plán díla ──────────────────────────────────────────────────────────
def _generate_plan(config: dict, topic: str, examples: list) -> Optional[dict]:
    """Z koníčku vymyslí dílo: {title, kind, premise, outline:[...]}.
    Base model, keep_alive=0. None když se nevygeneruje."""
    from scripts.ollama_client import ollama_chat
    c = _cfg(config)
    n_min = int(c.get("sections_min", 5))
    n_max = int(c.get("sections_max", 8))
    ex = ", ".join(str(e) for e in (examples or [])[:6])
    name = _persona_name(config)
    system = (
        f"Jsi {name} a vymýšlíš si VLASTNÍ dílo na pokračování k tématu, které tě "
        "dlouhodobě zajímá. Vrať POUZE JSON objekt bez doprovodného textu, ve tvaru:\n"
        '{"title": "...", "kind": "esej|povídka|průvodce|úvaha", '
        '"premise": "1-2 věty o čem dílo je a proč ho píšeš", '
        f'"outline": ["sekce 1", "sekce 2", ...]}}\n'
        f"Osnova má mít {n_min} až {n_max} sekcí v logickém pořadí. Drž se tématu, "
        "ať to můžeš napsat z toho, co znáš. Česky, bez emoji."
    )
    user = f"Téma: {topic}\nCo tě na něm konkrétně zajímá: {ex}\nNavrhni dílo."
    try:
        raw = ollama_chat(
            _plan_model(config),
            [{"role": "system", "content": system},
             {"role": "user", "content": user}],
            ollama_url=_base_url(config), keep_alive=0,
            options={"temperature": 0.7,
                     "num_ctx": int(c.get("num_ctx", 8192)),
                     "num_predict": 500})
    except Exception as e:
        _log.warning("_generate_plan selhal: %s", e)
        return None
    obj = _parse_json_obj(raw or "")
    if not obj:
        return None
    outline = obj.get("outline")
    if not isinstance(outline, list) or len(outline) < 3:
        return None
    obj["outline"] = [str(s).strip() for s in outline if str(s).strip()][:n_max]
    obj.setdefault("title", topic)
    obj.setdefault("kind", "esej")
    obj.setdefault("premise", "")
    return obj


# ── LLM: próza jedné sekce ──────────────────────────────────────────────────
def _gather_grounding(knowledge, topic: str, section: str,
                      max_chars: int = 2500) -> str:
    """Best-effort: z Hansových RAG kolekcí (čtení/identita/díla) vytáhne podklady
    k tématu+sekci. '' když nic / knowledge None."""
    if knowledge is None:
        return ""
    parts: List[str] = []
    for coll in ("hans_cetba", "hans_identita", "hans_dila"):
        try:
            res = knowledge.query(coll, f"{topic} {section}", k=3)
            if res is not None and getattr(res, "found", False) and res.text:
                parts.append(res.text)
        except Exception:
            continue
    return ("\n".join(parts))[:max_chars]


def _generate_section(config: dict, project: dict, section: str,
                      prev_summary: str, grounding: str) -> str:
    from scripts.ollama_client import ollama_chat
    c = _cfg(config)
    name = _persona_name(config)
    system = (
        f"Jsi {name} a píšeš SVÉ vlastní dílo — {project.get('kind', 'esej')} "
        f"s názvem „{project.get('title', '')}“. Námět: {project.get('premise', '')}\n"
        f"Napiš jednu sekci: „{section}“. Souvislý text (1-3 odstavce), tvým hlasem, "
        "navazuj na předchozí sekci. Vyjdi z PODKLADŮ (svého čtení a studia) — "
        "nevymýšlej fakta nad jejich rámec; kde fakta nejsou, drž se obecné úvahy. "
        "Nepiš nadpis sekce, jen text. Česky, bez emoji."
    )
    user = (f"PODKLADY (mé čtení/studium):\n{grounding or '(zatím bez podkladů — piš z obecné znalosti, opatrně)'}\n\n"
            f"PŘEDCHOZÍ SEKCE (shrnutí):\n{prev_summary or '(toto je první sekce)'}\n\n"
            f"Napiš sekci: {section}")
    try:
        raw = ollama_chat(
            _prose_model(config),
            [{"role": "system", "content": system},
             {"role": "user", "content": user}],
            ollama_url=_base_url(config),
            options={"temperature": 0.8,
                     "num_ctx": int(c.get("num_ctx", 8192)),
                     "num_predict": int(c.get("section_num_predict", 500))})
    except Exception as e:
        _log.warning("_generate_section selhal: %s", e)
        return ""
    return (raw or "").strip()[:int(c.get("section_max_chars", 4000))]


def _generate_afterword(config: dict, project: dict, sections: List[str]) -> str:
    from scripts.ollama_client import ollama_chat
    c = _cfg(config)
    name = _persona_name(config)
    gist = "\n".join(f"- {s[:120]}" for s in sections[:10])
    system = (f"Jsi {name}. Právě jsi dokončil své dílo „{project.get('title', '')}“. "
              "Napiš krátký osobní dovětek (2-4 věty): co ti psaní dalo, co jsi si "
              "uvědomil. Tvým hlasem, česky, bez emoji.")
    try:
        raw = ollama_chat(
            _prose_model(config),
            [{"role": "system", "content": system},
             {"role": "user", "content": "Sekce díla:\n" + gist}],
            ollama_url=_base_url(config),
            options={"temperature": 0.8, "num_ctx": int(c.get("num_ctx", 8192)),
                     "num_predict": 220})
    except Exception:
        return ""
    return (raw or "").strip()[:1200]


# ── Store ───────────────────────────────────────────────────────────────────
class AuthorshipStore:
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
                    CREATE TABLE IF NOT EXISTS writing_project (
                        id            INTEGER PRIMARY KEY AUTOINCREMENT,
                        topic         TEXT,
                        topic_norm    TEXT,
                        title         TEXT,
                        kind          TEXT,
                        premise       TEXT,
                        outline       TEXT,
                        current_index INTEGER DEFAULT 0,
                        status        TEXT DEFAULT 'active',
                        sessions_done INTEGER DEFAULT 0,
                        started_ts    REAL,
                        updated_ts    REAL,
                        last_session_ts REAL DEFAULT 0
                    )""")
                db.execute("""
                    CREATE TABLE IF NOT EXISTS writing_section (
                        id         INTEGER PRIMARY KEY AUTOINCREMENT,
                        project_id INTEGER NOT NULL,
                        idx        INTEGER NOT NULL,
                        title      TEXT,
                        text       TEXT,
                        ts         REAL
                    )""")
                db.execute("CREATE INDEX IF NOT EXISTS idx_wsec_proj "
                           "ON writing_section(project_id, idx)")
                db.commit()
        except Exception as e:
            _log.warning("AuthorshipStore _init_db: %s", e)

    @staticmethod
    def _row_to_dict(row) -> dict:
        cols = ("id", "topic", "topic_norm", "title", "kind", "premise",
                "outline", "current_index", "status", "sessions_done",
                "started_ts", "updated_ts", "last_session_ts")
        d = dict(zip(cols, row))
        try:
            d["outline"] = json.loads(d.get("outline") or "[]")
        except Exception:
            d["outline"] = []
        return d

    def get_active(self) -> Optional[dict]:
        try:
            with self._connect() as db:
                row = db.execute(
                    "SELECT id,topic,topic_norm,title,kind,premise,outline,"
                    "current_index,status,sessions_done,started_ts,updated_ts,"
                    "last_session_ts FROM writing_project WHERE status='active' "
                    "ORDER BY id DESC LIMIT 1").fetchone()
            return self._row_to_dict(row) if row else None
        except Exception as e:
            _log.warning("get_active: %s", e)
            return None

    def all_projects(self, limit: int = 20) -> List[dict]:
        try:
            with self._connect() as db:
                rows = db.execute(
                    "SELECT id,topic,topic_norm,title,kind,premise,outline,"
                    "current_index,status,sessions_done,started_ts,updated_ts,"
                    "last_session_ts FROM writing_project ORDER BY id DESC "
                    "LIMIT ?", (limit,)).fetchall()
            return [self._row_to_dict(r) for r in rows]
        except Exception:
            return []

    def _written_topic_norms(self) -> set:
        try:
            with self._connect() as db:
                rows = db.execute(
                    "SELECT DISTINCT topic_norm FROM writing_project").fetchall()
            return {r[0] for r in rows if r and r[0]}
        except Exception:
            return set()

    def _update(self, pid: int, **fields):
        if not fields:
            return
        sets = ", ".join(f"{k}=?" for k in fields)
        vals = list(fields.values()) + [pid]
        try:
            with self._connect() as db:
                db.execute(f"UPDATE writing_project SET {sets} WHERE id=?", vals)
                db.commit()
        except Exception as e:
            _log.warning("_update: %s", e)

    # ── ensure_project ──────────────────────────────────────────────────────
    def ensure_project(self, config: dict) -> Optional[dict]:
        active = self.get_active()
        if active:
            return active
        c = _cfg(config)
        try:
            from scripts.hans_hobbies import HobbyStore
        except ImportError:
            _log.warning("ensure_project: HobbyStore nedostupný")
            return None
        hobbies = HobbyStore(config, self._diary_path).durable_hobbies(
            min_evidence=int(c.get("min_evidence", 8)),
            min_age_days=int(c.get("min_age_days", 21)),
            min_recent_days=int(c.get("min_recent_days", 14)))
        if not hobbies:
            _log.info("authorship.ensure_project: žádný durable koníček")
            return None
        done = self._written_topic_norms()
        chosen = next((h for h in hobbies if _norm(h.name) not in done), None)
        if chosen is None:
            _log.info("authorship.ensure_project: o všech koníčcích už dílo je")
            return None
        plan = _generate_plan(config, chosen.name, chosen.examples)
        if not plan:
            _log.info("authorship.ensure_project: plán se nevygeneroval (LLM dole?)")
            return None
        now = time.time()
        try:
            with self._connect() as db:
                cur = db.execute(
                    "INSERT INTO writing_project (topic,topic_norm,title,kind,"
                    "premise,outline,current_index,status,sessions_done,"
                    "started_ts,updated_ts,last_session_ts) "
                    "VALUES (?,?,?,?,?,?,0,'active',0,?,?,0)",
                    (chosen.name, _norm(chosen.name), plan["title"],
                     plan["kind"], plan["premise"],
                     json.dumps(plan["outline"], ensure_ascii=False), now, now))
                db.commit()
                pid = cur.lastrowid
        except Exception as e:
            _log.warning("ensure_project INSERT: %s", e)
            return None
        _log.info("authorship: NOVÝ projekt [%d] „%s“ (%s) — %d sekcí",
                  pid, plan["title"], plan["kind"], len(plan["outline"]))
        return self.get_active()

    def _last_section_text(self, pid: int) -> str:
        try:
            with self._connect() as db:
                row = db.execute(
                    "SELECT text FROM writing_section WHERE project_id=? "
                    "ORDER BY idx DESC LIMIT 1", (pid,)).fetchone()
            return (row[0] if row and row[0] else "")[:1200]
        except Exception:
            return ""

    def _diary(self, event_type: str, title: str, text: str):
        try:
            with self._connect() as db:
                db.execute("""CREATE TABLE IF NOT EXISTS diary (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL,
                    event_type TEXT NOT NULL, title TEXT, data TEXT, note TEXT)""")
                db.execute("INSERT INTO diary (ts,event_type,title,data) "
                           "VALUES (?,?,?,?)", (time.time(), event_type, title, text))
                db.commit()
        except Exception as e:
            _log.debug("_diary: %s", e)

    # ── write_next ──────────────────────────────────────────────────────────
    def write_next(self, config: dict, knowledge=None) -> Optional[dict]:
        prog = self.get_active()
        if not prog:
            return None
        outline = prog["outline"]
        idx = int(prog["current_index"])
        if idx >= len(outline):
            return self._complete(config, prog, knowledge)
        section = outline[idx]
        grounding = _gather_grounding(knowledge, prog["topic"], section)
        prev = self._last_section_text(prog["id"]) or prog["premise"]
        prose = _generate_section(config, prog, section, prev, grounding)
        if not prose or len(prose) < 40:
            _log.info("write_next: sekce '%s' se nenapsala (LLM dole?) — retry", section)
            return None
        now = time.time()
        try:
            with self._connect() as db:
                db.execute("INSERT INTO writing_section (project_id,idx,title,"
                           "text,ts) VALUES (?,?,?,?,?)",
                           (prog["id"], idx, section, prose, now))
                db.commit()
        except Exception as e:
            _log.warning("write_next INSERT section: %s", e)
            return None
        title = f"{prog['title']} — {section}"
        self._diary("writing_section", title, prose)
        if knowledge is not None:
            try:
                knowledge.upload(
                    collection_key=str(_cfg(config).get("rag_collection", "hans_dila")),
                    doc_id=f"work_{prog['id']}_s{idx}", title=title, text=prose,
                    metadata={"kdy": time.strftime("%Y-%m-%d"), "typ": "dílo"})
            except Exception:
                pass
        self._update(prog["id"], current_index=idx + 1,
                     sessions_done=int(prog["sessions_done"]) + 1,
                     updated_ts=now, last_session_ts=now)
        _log.info("authorship: projekt [%d] „%s“ — sekce %d/%d: %s",
                  prog["id"], prog["title"], idx + 1, len(outline), section)
        if (idx + 1) >= len(outline):
            # poslední sekce hotová → rovnou slož dílo (dovětek + soubor)
            prog["current_index"] = idx + 1
            return self._complete(config, prog, knowledge)
        return {"result": "written",
                "section": section, "index": idx + 1, "total": len(outline)}

    # ── complete ────────────────────────────────────────────────────────────
    def _complete(self, config: dict, prog: dict, knowledge=None) -> dict:
        try:
            with self._connect() as db:
                rows = db.execute(
                    "SELECT idx,title,text FROM writing_section WHERE project_id=? "
                    "ORDER BY idx", (prog["id"],)).fetchall()
        except Exception:
            rows = []
        sections = [(r[1], r[2]) for r in rows]
        afterword = _generate_afterword(config, prog, [t for _, t in sections])
        # složení do souboru
        path = ""
        try:
            d = Path("data/works")
            d.mkdir(parents=True, exist_ok=True)
            path = str(d / f"{_slug(prog['title'])}.md")
            lines = [f"# {prog['title']}", "", f"*{prog.get('kind', '')}*  ",
                     f"*{prog.get('premise', '')}*", ""]
            for st, tx in sections:
                lines += [f"## {st}", "", tx, ""]
            if afterword:
                lines += ["---", "", f"*{afterword}*", ""]
            Path(path).write_text("\n".join(lines), encoding="utf-8")
        except Exception as e:
            _log.warning("_complete write file: %s", e)
        self._update(prog["id"], status="completed", updated_ts=time.time())
        note = afterword or f"Dokončil jsem dílo „{prog['title']}“."
        self._diary("writing_completed", prog["title"], note)
        if knowledge is not None and afterword:
            try:
                knowledge.upload(collection_key="hans_identita",
                                 doc_id=f"work_done_{prog['id']}",
                                 title=f"Dokončené dílo: {prog['title']}",
                                 text=note,
                                 metadata={"kdy": time.strftime("%Y-%m-%d"),
                                           "typ": "dílo-dokončeno"})
            except Exception:
                pass
        _log.info("authorship: projekt [%d] „%s“ DOKONČEN → %s",
                  prog["id"], prog["title"], path or "(bez souboru)")
        return {"result": "completed", "title": prog["title"], "path": path}


# ── Top-level ───────────────────────────────────────────────────────────────
def run_writing_session(config: dict, diary_db_path: str, knowledge=None) -> str:
    """Jedna noční autorská session. Kódy:
       'written'   — napsána sekce
       'completed' — dílo dokončeno
       'idle'      — nic k psaní (žádný durable koníček / o všech už dílo je)
       'deferred'  — transientní selhání (Ollama dole) → retry."""
    if not _cfg(config).get("enabled", True):
        return "idle"
    try:
        store = AuthorshipStore(config, diary_db_path)
    except Exception as e:
        _log.warning("run_writing_session init: %s", e)
        return "deferred"
    prog = store.ensure_project(config)
    if not prog:
        c = _cfg(config)
        try:
            from scripts.hans_hobbies import HobbyStore
            hobs = HobbyStore(config, diary_db_path).durable_hobbies(
                min_evidence=int(c.get("min_evidence", 8)),
                min_age_days=int(c.get("min_age_days", 21)),
                min_recent_days=int(c.get("min_recent_days", 14)))
        except Exception:
            hobs = []
        unwritten = [h for h in hobs
                     if _norm(h.name) not in store._written_topic_norms()]
        return "deferred" if unwritten else "idle"
    res = store.write_next(config, knowledge=knowledge)
    if res is None:
        return "deferred"
    return res.get("result", "written")
