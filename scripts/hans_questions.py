"""
Hans Questions Store
====================
Asynchronní fronta otázek které Hans pokládá obyvatelům domu.

Otázky vznikají z četby, pozorování, sledování Kodi atd. Osoba nemusí
odpovídat hned — otázka čeká v queue dokud:
  - nezodpoví ji v dashboardu        (answer_via='dashboard')
  - nezodpoví ji při hlasovém pozdravu (answer_via='voice')   ← TODO až bude voice
  - nezodpoví ji v chat okně          (answer_via='chat')
  - neuplyne expirace (14 dní default)

Po zodpovězení Hans (volitelně) vygeneruje krátkou reakci a uloží ji
do `hans_reaction` — to může web admin zobrazit a chat handler v budoucnu
využít k navázání rozhovoru.

Použití:
    store = HansQuestionsStore("data/hans_diary.db", config)

    # 1. Vznik otázky
    qid = store.add_question(
        question="Víte zda byly Pompeje znovu osídleny?",
        target_person="alice",
        source_type="reading",
        source_ref="Pompeje",
        context="Hans dnes četl Wikipedii o Pompejích.",
    )

    # 2. Dashboard
    pending = store.list_questions(status="pending", target="alice")

    # 3. Když Alice odpoví v dashboardu:
    store.answer_question(qid, "Ano, byly osídleny v 17. století.",
                          via="dashboard")

    # 4. Hansova reakce (volá se asynchronně po answer_question)
    reaction = generate_hans_reaction(question_text, answer_text, ollama_url, model)
    store.set_reaction(qid, reaction)

    # 5. Hlas — TODO až bude voice integrace:
    q = store.next_for_voice("alice")
    if q:
        store.mark_asked_voice(q.id)
        # ... TTS položí otázku, parsuje odpověď, zavolá answer_question
"""

import logging
import os
import random
import sqlite3
import threading
import time
from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional

import requests

_log = logging.getLogger("hans_questions")

# ─── Defaults (přepiš v config.json -> "hans_questions") ──────────────────────
DEFAULT_EXPIRES_DAYS               = 14
DEFAULT_MAX_PENDING_PER_PERSON     = 3
DEFAULT_MAX_NEW_PER_DAY_PER_SOURCE = 1
DEFAULT_MIN_AGE_BEFORE_VOICE_H     = 1.0    # neptej se hlasem hned po vzniku
DEFAULT_VOICE_ASK_PROBABILITY      = 0.3    # při pozdravu

# Statusy
STATUS_PENDING     = "pending"
STATUS_ASKED_VOICE = "asked_voice"
STATUS_ANSWERED    = "answered"
STATUS_EXPIRED     = "expired"
STATUS_DISMISSED   = "dismissed"

# Kanály odpovědi
ANSWER_VIA_VOICE     = "voice"
ANSWER_VIA_DASHBOARD = "dashboard"
ANSWER_VIA_CHAT      = "chat"


# ─── Datový objekt ────────────────────────────────────────────────────────────
@dataclass
class Question:
    id: int = 0
    ts_asked: float = 0.0
    target_person: str = "anyone"
    source_type: str = "thought"     # reading / observation / kodi / thought
    source_ref: str = ""             # název článku / objektu / filmu
    question: str = ""
    context: str = ""                # proč se Hans ptá
    status: str = STATUS_PENDING
    asked_voice_at: Optional[float] = None
    answered_at: Optional[float] = None
    answer: Optional[str] = None
    answer_via: Optional[str] = None
    hans_reaction: Optional[str] = None
    expires_at: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @property
    def age_seconds(self) -> float:
        return time.time() - self.ts_asked

    @property
    def is_expired(self) -> bool:
        return time.time() >= self.expires_at


# ─── Store ────────────────────────────────────────────────────────────────────
class HansQuestionsStore:
    """SQLite store nad tabulkou hans_questions. Thread-safe."""

    def __init__(self, db_path: str, config: Optional[dict] = None):
        self._db_path = db_path
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._init_db()

        cfg = (config or {}).get("hans_questions", {}) if config else {}
        self.expires_days               = float(cfg.get("expires_days",
                                                        DEFAULT_EXPIRES_DAYS))
        self.max_pending_per_person     = int(cfg.get("max_pending_per_person",
                                                      DEFAULT_MAX_PENDING_PER_PERSON))
        self.max_new_per_day_per_source = int(cfg.get("max_new_per_day_per_source",
                                                      DEFAULT_MAX_NEW_PER_DAY_PER_SOURCE))
        self.min_age_before_voice_h     = float(cfg.get("min_age_before_voice_h",
                                                        DEFAULT_MIN_AGE_BEFORE_VOICE_H))
        self.voice_ask_probability      = float(cfg.get("voice_ask_probability",
                                                        DEFAULT_VOICE_ASK_PROBABILITY))

        _log.info("HansQuestionsStore ready (db=%s, max_pending/person=%d, expires=%.0fd)",
                  db_path, self.max_pending_per_person, self.expires_days)

    # ── DB init ───────────────────────────────────────────────────────────────
    def _init_db(self):
        with self._lock:
            self._db.execute("""
                CREATE TABLE IF NOT EXISTS hans_questions (
                    id             INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts_asked       REAL    NOT NULL,
                    target_person  TEXT    NOT NULL DEFAULT 'anyone',
                    source_type    TEXT    NOT NULL DEFAULT 'thought',
                    source_ref     TEXT    DEFAULT '',
                    question       TEXT    NOT NULL,
                    context        TEXT    DEFAULT '',
                    status         TEXT    NOT NULL DEFAULT 'pending',
                    asked_voice_at REAL,
                    answered_at    REAL,
                    answer         TEXT,
                    answer_via     TEXT,
                    hans_reaction  TEXT,
                    expires_at     REAL    NOT NULL
                )
            """)
            self._db.execute("CREATE INDEX IF NOT EXISTS idx_hq_status "
                             "ON hans_questions(status)")
            self._db.execute("CREATE INDEX IF NOT EXISTS idx_hq_target "
                             "ON hans_questions(target_person)")
            self._db.execute("CREATE INDEX IF NOT EXISTS idx_hq_ts "
                             "ON hans_questions(ts_asked DESC)")
            self._db.execute("CREATE INDEX IF NOT EXISTS idx_hq_source "
                             "ON hans_questions(source_type, source_ref)")
            self._db.commit()

    # ── CRUD ──────────────────────────────────────────────────────────────────
    def add_question(self,
                     question: str,
                     target_person: str = "anyone",
                     source_type: str = "thought",
                     source_ref: str = "",
                     context: str = "",
                     skip_limits: bool = False) -> Optional[int]:
        """Vloží novou otázku. Vrátí id, nebo None pokud byla zamítnuta limity."""
        question = (question or "").strip()
        if not question:
            return None
        target_person = (target_person or "anyone").strip().lower()

        if not skip_limits:
            if not self._can_add_for_person(target_person):
                _log.info("Q rejected — too many pending for %s", target_person)
                return None
            if not self._can_add_for_source(source_type):
                _log.info("Q rejected — daily limit for source=%s", source_type)
                return None
            if self._is_duplicate_today(question, source_ref):
                _log.info("Q rejected — duplicate today")
                return None

        now = time.time()
        with self._lock:
            cur = self._db.execute("""
                INSERT INTO hans_questions
                    (ts_asked, target_person, source_type, source_ref,
                     question, context, status, expires_at)
                VALUES (?,?,?,?,?,?,?,?)
            """, (now, target_person, source_type, source_ref,
                  question, context, STATUS_PENDING,
                  now + self.expires_days * 86400.0))
            self._db.commit()
            qid = cur.lastrowid
        _log.info("Q[%d] added target=%s source=%s: %s",
                  qid, target_person, source_type, question[:80])
        return qid

    def get_question(self, qid: int) -> Optional[Question]:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM hans_questions WHERE id=?", (qid,)
            ).fetchone()
        return self._row_to_question(row)

    def list_questions(self,
                       status: Optional[str] = None,
                       target: Optional[str] = None,
                       source_type: Optional[str] = None,
                       limit: int = 200) -> List[Question]:
        sql, args = "SELECT * FROM hans_questions WHERE 1=1", []
        if status and status != "all":
            sql += " AND status=?";        args.append(status)
        if target and target != "all":
            sql += " AND target_person=?"; args.append(target.lower())
        if source_type and source_type != "all":
            sql += " AND source_type=?";   args.append(source_type)
        sql += " ORDER BY ts_asked DESC LIMIT ?"
        args.append(int(limit))
        with self._lock:
            rows = self._db.execute(sql, args).fetchall()
        return [self._row_to_question(r) for r in rows if r is not None]

    def count_pending(self, target: Optional[str] = None) -> int:
        sql, args = "SELECT COUNT(*) FROM hans_questions WHERE status=?", [STATUS_PENDING]
        if target:
            sql += " AND target_person=?"
            args.append(target.lower())
        with self._lock:
            return self._db.execute(sql, args).fetchone()[0]

    # ── Akce ──────────────────────────────────────────────────────────────────
    def answer_question(self,
                        qid: int,
                        answer: str,
                        via: str = ANSWER_VIA_DASHBOARD,
                        hans_reaction: Optional[str] = None) -> bool:
        answer = (answer or "").strip()
        if not answer:
            return False
        with self._lock:
            self._db.execute("""
                UPDATE hans_questions
                   SET status=?, answer=?, answered_at=?, answer_via=?,
                       hans_reaction=COALESCE(?, hans_reaction)
                 WHERE id=?
            """, (STATUS_ANSWERED, answer, time.time(), via, hans_reaction, qid))
            self._db.commit()
        _log.info("Q[%d] answered via %s: %s", qid, via, answer[:80])
        return True

    def set_reaction(self, qid: int, reaction: str) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE hans_questions SET hans_reaction=? WHERE id=?",
                (reaction, qid))
            self._db.commit()

    def mark_asked_voice(self, qid: int) -> None:
        with self._lock:
            self._db.execute("""
                UPDATE hans_questions
                   SET status=?, asked_voice_at=?
                 WHERE id=? AND status=?
            """, (STATUS_ASKED_VOICE, time.time(), qid, STATUS_PENDING))
            self._db.commit()

    def dismiss(self, qid: int) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE hans_questions SET status=? WHERE id=?",
                (STATUS_DISMISSED, qid))
            self._db.commit()
        _log.info("Q[%d] dismissed", qid)

    def reassign(self, qid: int, new_target: str) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE hans_questions SET target_person=? WHERE id=?",
                ((new_target or "anyone").strip().lower(), qid))
            self._db.commit()

    def expire_old(self) -> int:
        """Označí staré pending/asked_voice otázky jako expired. Vrátí počet."""
        now = time.time()
        with self._lock:
            cur = self._db.execute("""
                UPDATE hans_questions
                   SET status=?
                 WHERE status IN (?,?) AND expires_at < ?
            """, (STATUS_EXPIRED, STATUS_PENDING, STATUS_ASKED_VOICE, now))
            n = cur.rowcount
            self._db.commit()
        if n:
            _log.info("Expired %d old questions", n)
        return n

    # ── Hlas (pro budoucí integraci) ─────────────────────────────────────────
    def next_for_voice(self, person: str) -> Optional[Question]:
        """
        Vybere nejstarší otázku vhodnou k hlasovému položení.
        Vrací None pokud:
          - random gate (voice_ask_probability) padne
          - žádná otázka nesplňuje kritéria
        """
        if random.random() > self.voice_ask_probability:
            return None
        cutoff = time.time() - self.min_age_before_voice_h * 3600.0
        with self._lock:
            row = self._db.execute("""
                SELECT * FROM hans_questions
                 WHERE status=?
                   AND (target_person=? OR target_person='anyone')
                   AND ts_asked <= ?
                   AND expires_at > ?
                 ORDER BY ts_asked ASC
                 LIMIT 1
            """, (STATUS_PENDING, person.lower(), cutoff, time.time())
            ).fetchone()
        return self._row_to_question(row)

    def next_for_person(self, person: str,
                        min_age_h: float = 1.0,
                        source_type: Optional[str] = None) -> Optional["Question"]:
        """HANS_QUESTIONS_SURFACING_V1 — nejstarší pending otázka pro osobu
        (target_person=osoba NEBO 'anyone'), bez voice random gate. Pro
        greeting i textový chat. min_age_h brání položit otázku těsně po
        vygenerování."""
        cutoff = time.time() - min_age_h * 3600.0
        with self._lock:
            # HANS_PERSONAL_QUESTIONS_V1 — volitelný source_type filtr
            _sql = ("SELECT * FROM hans_questions WHERE status=? "
                    "AND (target_person=? OR target_person='anyone') "
                    "AND ts_asked <= ? AND expires_at > ?")
            _args = [STATUS_PENDING, person.lower(), cutoff, time.time()]
            if source_type:
                _sql += " AND source_type=?"; _args.append(source_type)
            _sql += " ORDER BY ts_asked ASC LIMIT 1"
            row = self._db.execute(_sql, tuple(_args)).fetchone()
        return self._row_to_question(row)

    # ── Limity ────────────────────────────────────────────────────────────────
    def _can_add_for_person(self, person: str) -> bool:
        with self._lock:
            n = self._db.execute("""
                SELECT COUNT(*) FROM hans_questions
                 WHERE status IN (?,?) AND target_person=?
            """, (STATUS_PENDING, STATUS_ASKED_VOICE, person)).fetchone()[0]
        return n < self.max_pending_per_person

    def _can_add_for_source(self, source_type: str) -> bool:
        cutoff = time.time() - 86400.0
        with self._lock:
            n = self._db.execute("""
                SELECT COUNT(*) FROM hans_questions
                 WHERE source_type=? AND ts_asked >= ?
            """, (source_type, cutoff)).fetchone()[0]
        return n < self.max_new_per_day_per_source

    def _is_duplicate_today(self, question: str, source_ref: str) -> bool:
        cutoff = time.time() - 86400.0
        with self._lock:
            row = self._db.execute("""
                SELECT 1 FROM hans_questions
                 WHERE ts_asked >= ?
                   AND (question=? OR (source_ref=? AND source_ref<>''))
                 LIMIT 1
            """, (cutoff, question, source_ref)).fetchone()
        return row is not None

    # ── Util ──────────────────────────────────────────────────────────────────
    @staticmethod
    def _row_to_question(row) -> Optional[Question]:
        if row is None:
            return None
        return Question(**{k: row[k] for k in row.keys()})

    def stats(self) -> Dict[str, int]:
        """{'pending': 4, 'answered': 12, ...}"""
        with self._lock:
            rows = self._db.execute(
                "SELECT status, COUNT(*) AS n FROM hans_questions GROUP BY status"
            ).fetchall()
        return {r["status"]: r["n"] for r in rows}

    def close(self):
        with self._lock:
            try: self._db.close()
            except Exception: pass


# ─── LLM helpery (Ollama) ─────────────────────────────────────────────────────
# Standalone, žádná závislost na zbytku aplikace.
# Použít z hans_curiosity.py po úspěšném čtení.

# Znaky které ořezáváme z LLM výstupu (uvozovky, hvězdičky, pomlčky)
_TRIM_CHARS = " \t\"'*-\u201e\u201c\u201d\u2018\u2019"


def _ollama_chat(ollama_url: str, model: str,
                 system: str, user: str,
                 timeout: int = 120) -> Optional[str]:
    """Volá /api/chat přes ollama_client. Vrátí trimmed text nebo None."""
    # OLLAMA_CLIENT_PATCH_QUESTIONS
    from scripts.ollama_client import ollama_chat
    return ollama_chat(
        model,
        [
            {"role": "system", "content": system},
            {"role": "user",   "content": user},
        ],
        ollama_url=ollama_url,
        timeout=timeout,
        options={"temperature": 0.7, "num_predict": 120},
    )


def generate_question_via_llm(topic: str,
                              summary: str,
                              target_person: str,
                              ollama_url: str,
                              model: str,
                              config: dict = None,
                              goal: Optional[str] = None) -> Optional[str]:  # GOAL_ADVANCING_QUESTIONS_V1
    """
    Vygeneruje JEDNU otázku z nedávno přečteného obsahu.
    `summary` může být shrnutí z web_reader nebo plný text (zkráti se).
    """
    summary = (summary or "").strip()
    if not summary:
        return None
    if len(summary) > 1500:
        summary = summary[:1500] + "..."

    person_label = ("pana " + target_person.capitalize()
                    if target_person != "anyone" else "obyvatele domu")

    from scripts.hans_persona import persona_core  # PERSONA_REFACTOR_9B_Q
    system = persona_core(config or {}, with_address=False)
    user = (f"Právě jsi přečetl o tématu \u201e{topic}\u201c. "
            f"Toto je shrnutí:\n\n{summary}\n\n"
            f"Z tohoto čtení tě něco zaujalo. Polož {person_label} jednu "
            f"krátkou, přirozenou a vřelou otázku — v otázce LEHCE zmiň, že tě "
            f"k ní přivedla četba o tématu \u201e{topic}\u201c, ať tázaný ví, "
            f"proč se ptáš. Mluv lidsky, žádný akademický tón. "
            f"DŮLEŽITÉ: ten, koho se ptáš, o tématu „{topic}“ nejspíš nic "
            f"odborného neví — NEPŘEDPOKLÁDEJ u něj znalosti. Zeptej se tak, "
            f"aby mohl odpovědět z vlastní každodenní zkušenosti, pocitu nebo "
            f"názoru, ne na fakta či odbornost. Klidně mu krátce řekni, co tě "
            f"zaujalo, a zeptej se, jak to vidí on nebo jestli to zná ze svého "
            f"života. Vrať jen otázku "
            f"i s krátkým úvodem, česky.")  # QUESTIONS_NATURAL_V1 + QUESTIONS_ACCESSIBLE_V1
    if goal:  # GOAL_ADVANCING_QUESTIONS_V1 — posouvající otázka když běží cíl
        user += (f"\n\nPOZNÁMKA: Máš rozpracovaný tvůrčí cíl — chystáš se napsat dílo na téma \u201e{goal}\u201c. Pokud toto čtení s tvým cílem souvisí, polož místo běžné otázky takovou, která tě v cíli POSUNE: vytěží z {person_label} jeho OSOBNÍ pohled, pocit nebo zkušenost (i laickou — nepředpokládej odborné znalosti), který bys mohl využít ve svém díle. Pokud čtení s cílem nesouvisí, zeptej se přirozeně na četbu jako obvykle.")

    out = _ollama_chat(ollama_url, model, system, user)
    if not out:
        return None
    # Vyčistit — vzít jen první otázku (do prvního otazníku)
    if "?" in out:
        out = out.split("?")[0].strip() + "?"
    out = out.strip(_TRIM_CHARS)
    if len(out) < 8 or len(out) > 240:
        return None
    return out


def generate_hans_reaction(question: str,
                           answer: str,
                           ollama_url: str,
                           model: str,
                           target_person: str = "",
                           config: dict = None) -> Optional[str]:
    """
    Krátká reakce Hanse po obdržení odpovědi (1 věta, max ~15 slov).
    Slouží pro zápis do hans_reaction a pro budoucí použití v dialogu.
    """
    answer = (answer or "").strip()
    if not answer:
        return None

    from scripts.hans_persona import persona_core  # PERSONA_REFACTOR_9B_Q
    system = (persona_core(config or {}, with_address=False)
              + " Odpovídáš jednou krátkou větou.")
    person_label = (f" pana {target_person.capitalize()}"
                    if target_person and target_person != "anyone" else "")
    user = (f"Položil jsi{person_label} otázku:\n"
            f"\u201e{question}\u201c\n\n"
            f"Dostal jsi odpověď:\n"
            f"\u201e{answer}\u201c\n\n"
            f"Zformuluj svou tichou reakci — JEDNU větu, max 15 slov, "
            f"formálně, česky. Vrať pouze tu jednu větu, nic jiného.")

    out = _ollama_chat(ollama_url, model, system, user)
    if not out:
        return None
    # Vzít jen první větu
    for term in [".", "!", "?"]:
        if term in out:
            out = out.split(term)[0].strip() + term
            break
    out = out.strip(_TRIM_CHARS)
    if len(out) < 4 or len(out) > 200:
        return None
    return out


# ─── Self-test ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name

    s = HansQuestionsStore(path)
    qid = s.add_question(
        question="Víte zda byly Pompeje znovu osídleny po výbuchu?",
        target_person="alice",
        source_type="reading",
        source_ref="Pompeje",
        context="Hans dnes ráno četl Wikipedii o Pompejích.",
    )
    assert qid is not None, "add_question failed"
    print(f"Vytvořena otázka [{qid}]")

    print("Pending:", [q.question for q in s.list_questions(status="pending")])
    print("Stats:",   s.stats())

    s.answer_question(qid, "Ano, byly osídleny v 17. století.",
                      via="dashboard")
    s.set_reaction(qid, "Tedy obnova trvala téměř dvě století.")

    print("Po zodpovězení:")
    q = s.get_question(qid)
    print(f"  status={q.status}  answer={q.answer!r}")
    print(f"  reaction={q.hans_reaction!r}")
    print("Stats:", s.stats())

    s.close()
    os.unlink(path)
    print("OK")
