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


def _reasoning_model(config: dict) -> str:
    """Reasoning model pro krok 1 (EN úsudek). '' = vypnuto → legacy 1-call."""
    return (_cfg(config).get("reasoning_model") or "").strip()


def _voice_model(config: dict) -> str:
    """Model pro krok 2 (CZ hlas). Default = Hansův dialog model (hans-czech)."""
    return (_cfg(config).get("voice_model") or _model(config))


_THINK_RE = re.compile(r"<think>.*?</think>", re.IGNORECASE | re.DOTALL)
_THINK_OPEN_RE = re.compile(r"<think>.*", re.IGNORECASE | re.DOTALL)


def _strip_think(text: str) -> str:
    """Odstraň reasoning chain (<think>…</think>) z výstupu reasoning modelu.
    Ošetří i neuzavřený <think> (truncated) — vrátí prázdno, ať se to nepovažuje
    za odpověď."""
    t = _THINK_RE.sub("", text or "")
    t = _THINK_OPEN_RE.sub("", t)  # neuzavřený think = uříznuto → zbytek zahodit
    return t.strip()


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


def _embed_texts(config: dict, texts: List[str]) -> Optional[list]:
    """bge-m3 embeddingy přes Ollama /api/embed. None při selhání (→ fallback).
    Deferral-safe: herní mód / výpadek → None."""
    try:
        from scripts.ollama_client import game_mode_on
        if game_mode_on():
            return None
    except Exception:
        pass
    import requests
    c = _cfg(config)
    url = _base_url(config).rstrip("/") + "/api/embed"
    model = str(c.get("embed_model", "bge-m3:latest"))
    try:
        r = requests.post(url, json={"model": model, "input": texts,
                                     "keep_alive": 0}, timeout=60)
        r.raise_for_status()
        embs = r.json().get("embeddings")
        if not embs or len(embs) != len(texts):
            return None
        return embs
    except Exception as e:
        _log.info("_embed_texts selhal (fallback na random): %s", e)
        return None


def _select_related(config: dict, cands: List[dict], n: int) -> Optional[List[dict]]:
    """Vyber n semínek, která spolu aspoň volně souvisí: náhodná kotva (pestrost
    napříč nocemi) + jejího n-1 nejpodobnějších sousedů (bge-m3 kosinus).
    None → volající spadne na random.sample."""
    c = _cfg(config)
    emax = int(c.get("embed_max_chars", 400))
    texts = [(s["topic"] + ". " + s["text"])[:emax] for s in cands]
    embs = _embed_texts(config, texts)
    if not embs:
        return None
    try:
        import numpy as np
        M = np.asarray(embs, dtype=np.float32)
        norms = np.linalg.norm(M, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        M = M / norms
        anchor = random.randrange(len(cands))
        sims = M @ M[anchor]                      # kosinus (znormováno)
        order = sorted((i for i in range(len(cands)) if i != anchor),
                       key=lambda i: -float(sims[i]))
        idxs = [anchor] + order[:max(0, n - 1)]
        return [cands[i] for i in idxs]
    except Exception as e:
        _log.info("_select_related selhal (fallback na random): %s", e)
        return None


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
        # PŘEDVÝBĚR (lever b): místo 3 náhodných vybrat semínka, která spolu
        # aspoň volně souvisí (bge-m3 podobnost) → model má co reálně propojit,
        # míň prázdných univerzálií. Fallback na random když embeddings dole.
        c = _cfg(self.config)
        if c.get("related_seeds", True) and len(cands) > n:
            sel = _select_related(self.config, cands, n)
            if sel:
                return sel
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
        recent = self.recent_insights()
        if _reasoning_model(config):
            # 2-call pipeline: reasoning model (EN úsudek) → hans-czech (CZ hlas).
            en = _reason_insight(config, seeds, recent)
            if en is None:               # reasoning LLM dole → retry
                _log.info("synthesis: reasoning model nedostupný — retry")
                return None
            if en == "":                 # vysoká laťka — jen prázdná univerzálie
                _log.info("synthesis: reasoning model nenašel skutečný postřeh "
                          "(jen univerzálie) → idle")
                return {"result": "idle"}
            insight = _render_voice(config, en, seeds)
            if not insight or len(insight) < 30:
                _log.info("synthesis: hlasový render selhal (hans-czech dole?) "
                          "— retry")
                return None
        else:
            # legacy 1-call (hans-czech píše rovnou)
            insight = _generate_insight(config, seeds, recent)
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


# ── LLM: 2-call pipeline (reasoning EN úsudek → hans-czech CZ hlas) ──────────
def _reason_insight(config: dict, seeds: List[dict],
                    recent: List[str]) -> Optional[str]:
    """Krok 1 — reasoning model (QwQ ap.) hledá SKUTEČNÝ mezioborový postřeh.
    Uvažuje anglicky (nejsilnější), čte český materiál (čtení je jazykově
    neutrální). VYSOKÁ LAŤKA: raději vrátí NONE než prázdnou univerzálii.

    Návrat:
      None  — LLM dole / žádná parsovatelná odpověď → deferred (retry)
      ''    — reasoning model soudí, že žádný pravý postřeh není → idle
      text  — anglická formulace jednoho konkrétního postřehu
    """
    from scripts.ollama_client import ollama_chat
    c = _cfg(config)
    avoid = ""
    if recent:
        avoid = ("\n\nThese insights were already made — do NOT repeat them or "
                 "rephrase them:\n" + "\n".join(f"- {r[:150]}" for r in recent[:6]))
    system = (
        "You are a sharp analyst hunting for ONE genuine, non-obvious connection "
        "between several things someone recently learned across DIFFERENT domains. "
        "Think step by step in English. The material is in Czech — read it "
        "carefully.\n\n"
        "A GENUINE insight ties together SPECIFIC details from at least two of the "
        "items in a way that is surprising and substantive — it teaches something "
        "you couldn't get from either item alone.\n"
        "A WORTHLESS 'insight' is a vague universal that could be pasted onto almost "
        "any set of topics — e.g. 'it's all about adaptation', 'everything relates "
        "to time / change / human nature', 'these seemingly unrelated things share a "
        "common thread'. These are NOT insights.\n\n"
        "Hold a HIGH bar. If the only link you can find is such a vague universal, "
        "that is a failure — do NOT force it.\n\n"
        "After your reasoning, output EXACTLY ONE final line, nothing after it:\n"
        "  INSIGHT: <one specific, concrete cross-domain insight, 1-2 sentences>\n"
        "or, if no genuine connection exists:\n"
        "  INSIGHT: NONE" + avoid)
    body = "\n\n".join(f"[{s['topic']}]\n{s['text']}" for s in seeds)
    user = ("Here is what was recently learned, from different areas:\n\n" + body +
            "\n\nFind at most one genuine, specific cross-domain insight — or NONE.")
    opts = {"temperature": float(c.get("reasoning_temperature", 0.6)),
            "num_ctx": int(c.get("reasoning_num_ctx", 8192)),
            "num_predict": int(c.get("reasoning_num_predict", 2048)),
            "num_gpu": int(c.get("reasoning_num_gpu", 0))}  # 0 = celý do RAM/CPU
    try:
        raw = ollama_chat(
            _reasoning_model(config),
            [{"role": "system", "content": system},
             {"role": "user", "content": user}],
            ollama_url=_base_url(config),
            keep_alive=0,   # on-demand tier — po volání uvolni
            timeout=int(c.get("reasoning_timeout", 900)),
            options=opts)
    except Exception as e:
        _log.warning("_reason_insight selhal: %s", e)
        return None
    if not raw:
        return None
    clean = _strip_think(raw)
    if not clean:
        return None                       # jen think chain / truncated → retry
    # najdi poslední 'INSIGHT:' řádek
    m = None
    for mm in re.finditer(r"INSIGHT:\s*(.+)", clean, flags=re.IGNORECASE | re.DOTALL):
        m = mm
    if not m:
        _log.info("_reason_insight: chybí marker INSIGHT: — retry")
        return None
    ans = m.group(1).strip().strip('"').strip()
    # vezmi jen první odstavec (za markerem může model přidat balast)
    ans = ans.split("\n\n")[0].strip()
    if re.fullmatch(r"none[.!]?", ans, flags=re.IGNORECASE) or not ans:
        return ""                         # vysoká laťka → idle
    return ans[:int(c.get("reasoning_max_chars", 600))]


def _render_voice(config: dict, en_insight: str, seeds: List[dict]) -> str:
    """Krok 2 — hans-czech vyrenderuje anglický postřeh Hansovým hlasem česky.
    NE překlad — jak by to Hans řekl. Grounded v postřehu + semínkách."""
    from scripts.ollama_client import ollama_chat
    c = _cfg(config)
    name = _persona_name(config)
    system = (
        f"Jsi {name}. Při propojení několika věcí z různých oblastí tě napadl "
        "níže uvedený postřeh. Vyjádři ho SVÝM hlasem, česky — ne jako překlad, "
        "ale jak bys to řekl ty, přirozeně a přemýšlivě. 2-4 věty. Nepřidávej "
        "fakta nad rámec postřehu a materiálu, žádné emoji, žádný nadpis.")
    body = "\n\n".join(f"[{s['topic']}]\n{s['text'][:400]}" for s in seeds)
    user = (f"Můj postřeh (myšlenka, kterou chci vyjádřit):\n{en_insight}\n\n"
            f"Vzešel z tohohle materiálu:\n{body}\n\n"
            "Řekni ten postřeh svým hlasem, česky, 2-4 věty.")
    try:
        raw = ollama_chat(
            _voice_model(config),
            [{"role": "system", "content": system},
             {"role": "user", "content": user}],
            ollama_url=_base_url(config),
            options={"temperature": float(c.get("voice_temperature", 0.7)),
                     "num_ctx": int(c.get("num_ctx", 8192)),
                     "num_predict": int(c.get("voice_num_predict",
                                              c.get("num_predict", 280)))})
    except Exception as e:
        _log.warning("_render_voice selhal: %s", e)
        return ""
    return _strip_think(raw or "").strip()[:int(c.get("max_chars", 1200))]


# ── LLM: legacy 1-call (fallback bez reasoning modelu) ──────────────────────
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
