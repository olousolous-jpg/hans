#!/usr/bin/env python3
"""
HANS_PERSON_INTERESTS_V1 — Frontier #4 (Theory of mind), krok 2: per-osoba zájmy.

Co kterou OSOBU zajímá (na rozdíl od `hans_hobbies`, což jsou Hansovy vlastní
koníčky, a plochého `interest_update`, což je Hansův proud zájmů). Model „co koho
zajímá" → Hans přizpůsobí hovor + cíleně se doptává.

Tok (deferral-safe, [[ollama-deferred-processing]] / [[ollama-vram-tiers]]):
  1. CAPTURE (nočně, base OpenEuroLLM keep_alive=0): přiživeno na noční průchod
     nitek (sdílí `_gather_dialogs` z hans_threads). Z denních human_chat per
     osoba vytáhne, co osobu zajímá / o čem mluví / co má ráda. Anti-konfabulace
     (jen co osoba doslovně řekla). Dedup proti známým zájmům osoby (reuse-known
     jako STANCE_MERGE / hobbies). Zrcadlo HobbyStore, ale klíčované OSOBOU.
  2. USE: inject do chat/greeting kontextu (Hans přizpůsobí hovor); + curiosity
     generuje zjišťovací otázky když je model osoby tenký (krok 2.4).

Tabulka `person_interests` v hans_diary.db:
  person, interest, interest_norm, evidence_count, first_seen, last_seen,
  examples(JSON), status

API:
  store = PersonInterestStore(config, diary_db_path)
  store.add_or_reinforce(person, interest, examples=None) -> id|None
  store.interests_for(person, limit=10) -> [PersonInterest]
  store.persons_with_interests() -> [person_norm, …]
  extract_person_interests(config, diary_db_path, window_hours=26) -> int
  format_block(interests) -> str    # do promptu (greeting/chat)
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from typing import List, Optional

_log = logging.getLogger("hans_person_interests")

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


class PersonInterest:
    __slots__ = ("id", "person", "interest", "evidence_count", "first_seen",
                 "last_seen", "examples", "status")

    def __init__(self, row):
        self.id = row["id"]
        self.person = (row["person"] or "").strip()
        self.interest = (row["interest"] or "").strip()
        self.evidence_count = row["evidence_count"] or 0
        self.first_seen = row["first_seen"] or 0.0
        self.last_seen = row["last_seen"] or 0.0
        self.examples = _load_examples(row["examples"]) if "examples" in row.keys() else []
        self.status = row["status"] or "active"

    def age_days(self) -> int:
        return max(0, int((time.time() - self.first_seen) / 86400))

    def as_dict(self) -> dict:
        return {"id": self.id, "person": self.person, "interest": self.interest,
                "evidence_count": self.evidence_count,
                "age_days": self.age_days(), "examples": list(self.examples)}

    def __repr__(self):
        return (f"<PersonInterest {self.id} {self.person} n={self.evidence_count} "
                f"{self.interest[:40]!r}>")


class PersonInterestStore:
    def __init__(self, config: dict, diary_db_path: str):
        self._diary_path = diary_db_path
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self._diary_path) as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS person_interests (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    person          TEXT NOT NULL,
                    interest        TEXT NOT NULL,
                    interest_norm   TEXT NOT NULL,
                    evidence_count  INTEGER NOT NULL DEFAULT 1,
                    first_seen      REAL NOT NULL,
                    last_seen       REAL NOT NULL,
                    examples        TEXT,
                    status          TEXT NOT NULL DEFAULT 'active'
                )
            """)
            db.execute("CREATE INDEX IF NOT EXISTS idx_pinterests_person "
                       "ON person_interests(person, interest_norm)")
            db.commit()

    def _connect(self):
        conn = sqlite3.connect(self._diary_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        return conn

    def add_or_reinforce(self, person: str, interest: str,
                         examples: list = None) -> Optional[int]:
        pnorm = _norm(person)
        inorm = _norm(interest)
        if not pnorm or not inorm:
            return None
        now = time.time()
        examples = [str(e).strip() for e in (examples or []) if str(e).strip()]
        try:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT id, examples FROM person_interests WHERE person=? "
                    "AND interest_norm=? AND status='active' ORDER BY id LIMIT 1",
                    (pnorm, inorm)).fetchone()
                if row is None:
                    conn.execute(
                        "INSERT INTO person_interests (person, interest, "
                        "interest_norm, evidence_count, first_seen, last_seen, "
                        "examples, status) VALUES (?,?,?,1,?,?,?,'active')",
                        (pnorm, interest.strip(), inorm, now, now,
                         json.dumps(examples, ensure_ascii=False)))
                    conn.commit()
                    rid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                    _log.info("interest NEW [%s] %s: %.40s", rid, pnorm, interest)
                    return rid
                rid = row["id"]
                merged = _load_examples(row["examples"])
                seen = {_norm(x) for x in merged}
                for e in examples:
                    if _norm(e) not in seen:
                        merged.append(e); seen.add(_norm(e))
                conn.execute(
                    "UPDATE person_interests SET evidence_count=evidence_count+1, "
                    "last_seen=?, examples=? WHERE id=?",
                    (now, json.dumps(merged[:20], ensure_ascii=False), rid))
                conn.commit()
                _log.info("interest REINFORCE [%s] %s (n+1): %.40s",
                          rid, pnorm, interest)
                return rid
            finally:
                conn.close()
        except Exception as e:
            _log.warning("PersonInterestStore.add_or_reinforce failed: %s", e)
            return None

    def interests_for(self, person: str, limit: int = 10) -> List[PersonInterest]:
        pnorm = _norm(person)
        try:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT * FROM person_interests WHERE person=? AND status='active' "
                    "ORDER BY evidence_count DESC, last_seen DESC LIMIT ?",
                    (pnorm, limit)).fetchall()
                return [PersonInterest(r) for r in rows]
            finally:
                conn.close()
        except Exception as e:
            _log.warning("interests_for failed: %s", e)
            return []

    def persons_with_interests(self) -> List[str]:
        try:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT DISTINCT person FROM person_interests "
                    "WHERE status='active'").fetchall()
                return [r["person"] for r in rows]
            finally:
                conn.close()
        except Exception as e:
            _log.warning("persons_with_interests failed: %s", e)
            return []


# ── Surfacing helper (do promptu greeting/chat) ─────────────────────────────
def format_block(interests: List[PersonInterest]) -> str:
    """Kompaktní blok zájmů osoby do system promptu / greeting promptu."""
    if not interests:
        return ""
    return ", ".join(i.interest for i in interests)


# ── Noční extrakce (přiživeno na průchod nitek — sdílí _gather_dialogs) ──────
# PERSONA_NAME_CONFIGURABLE_V1 — {persona_name} se doplní z configu při .format()
_EXTRACT_SYSTEM = (
    "Jsi pozorný analytik vztahů postavy jménem {persona_name}. Dostaneš PŘEPIS "
    "dnešních rozhovorů s jednou osobou a seznam JEJÍCH UŽ ZNÁMÝCH ZÁJMŮ. "
    "Tvůj úkol: zjistit, co tuto osobu ZAJÍMÁ — témata, záliby, oblasti, o kterých "
    "mluví se zaujetím nebo které má ráda (např. 'fotbal', 'historie', 'vaření', "
    "'detektivky'). ZOBECNI konkrétní zmínky na trvalý zájem (např. zmínka o Realu "
    "Madrid → 'fotbal'). Když zájem odpovídá už známému, použij JEHO PŘESNÝ název "
    "(posílení, ne duplikát). "
    "PŘÍSNĚ ANTI-KONFABULACE: vytěž JEN to, co osoba DOSLOVNĚ řekla v přepisu; "
    "NIC si nedomýšlej. Mluví-li jen postava {persona_name}, ignoruj to (zajímá nás "
    "OSOBA, ne {persona_name}). Běžný pozdrav nebo dotaz na čas NENÍ zájem. "
    "Vrať VÝHRADNĚ JSON pole prvků s klíči: interest (název zájmu) a examples "
    "(konkrétní zmínky z přepisu). Když nic, vrať prázdné pole []."
)


def _parse_list(raw: str):
    s = re.sub(r"^```(?:json)?|```$", "", (raw or "").strip(), flags=re.MULTILINE).strip()
    i, j = s.find("["), s.rfind("]")
    if i == -1 or j == -1 or j < i:
        return []
    try:
        data = json.loads(s[i:j + 1])
    except Exception:
        return []
    return data if isinstance(data, list) else []


def extract_person_interests(config: dict, diary_db_path: str,
                             window_hours: float = 26.0) -> int:
    """Noční krok (po extract_threads, sdílí sběr dialogů): per osoba vytáhne
    zájmy z denních human_chat → PersonInterestStore. Vrací počet zpracovaných.
    LLM offline / parse fail → 0 (tichý skip)."""
    try:
        from scripts.hans_threads import _gather_dialogs
    except Exception as e:
        _log.warning("extract_person_interests: _gather_dialogs nedostupný: %s", e)
        return 0
    cfg = (config.get("person_interests", {}) or {})
    window_hours = float(cfg.get("window_hours", window_hours))
    since = time.time() - window_hours * 3600.0
    dialogs = _gather_dialogs(diary_db_path, since)
    if not dialogs:
        _log.info("extract_person_interests: žádné dialogy v okně, skip")
        return 0

    store = PersonInterestStore(config, diary_db_path)
    er = config.get("evening_reflection", {}) or {}
    model = str(cfg.get("model", er.get("model",
                "jobautomation/OpenEuroLLM-Czech:latest")))
    timeout = int(cfg.get("llm_timeout", 300))
    max_per_person = int(cfg.get("max_per_person", 8))
    try:
        from scripts.ollama_client import ollama_generate
    except ImportError:
        _log.warning("extract_person_interests: ollama_client nedostupný, skip")
        return 0
    try:
        from scripts.hans_persona import persona_name as _pn
        _system = _EXTRACT_SYSTEM.format(persona_name=_pn(config))
    except Exception:
        _system = _EXTRACT_SYSTEM.format(persona_name="Hans")

    written = 0
    for person, notes in dialogs.items():
        known = store.interests_for(person, limit=30)
        known_block = ""
        if known:
            known_block = ("UŽ ZNÁMÉ ZÁJMY (při shodě použij přesný název):\n"
                           + "\n".join(f"- {k.interest}" for k in known) + "\n\n")
        transcript = "\n---\n".join(notes[-20:])
        prompt = f"{known_block}PŘEPIS ROZHOVORŮ:\n{transcript}"
        try:
            raw = ollama_generate(model=model, prompt=prompt, system=_system,
                                  config=config, timeout=timeout,
                                  keep_alive=0,  # MODEL_KEEPALIVE_TIERS_V1
                                  options={"temperature": 0.2})
        except Exception as e:
            _log.warning("extract_person_interests: LLM failed (%s): %s", person, e)
            continue
        for it in _parse_list(raw)[:max_per_person]:
            if not isinstance(it, dict):
                continue
            interest = (it.get("interest") or "").strip()
            if not interest:
                continue
            ex = it.get("examples")
            ex = ex if isinstance(ex, list) else ([ex] if ex else [])
            if store.add_or_reinforce(person, interest, ex):
                written += 1
    _log.info("extract_person_interests: zpracováno %d zájmů (osob: %d)",
              written, len(dialogs))

    # HANS_PERSON_INTERESTS_V1 — když je model osoby tenký, generuj zjišťovací
    # otázku (surfacing přes HANS_QUESTIONS_SURFACING_V1). Vypnutelné configem.
    if cfg.get("probe_questions", True):
        try:
            generate_interest_questions(config, diary_db_path)
        except Exception as e:
            _log.warning("generate_interest_questions selhal: %s", e)
    return written


def generate_interest_questions(config: dict, diary_db_path: str,
                                min_known: int = 2) -> int:
    """Pro osoby z domu, o jejichž zájmech víme málo (< min_known), vygeneruje
    jednu vřelou zjišťovací otázku do hans_questions (source_type='interest',
    target_person=osoba). Surfacing zařídí HANS_QUESTIONS_SURFACING_V1 (greeting/
    chat). Denní strop drží add_question (max_new_per_day_per_source). base model
    keep_alive=0. Vrací počet vytvořených otázek."""
    persons = [p for p in (config.get("known_persons", {}) or {}).keys()
               if (p or "").strip()]
    if not persons:
        return 0
    cfg = (config.get("person_interests", {}) or {})
    er = config.get("evening_reflection", {}) or {}
    model = str(cfg.get("model", er.get("model",
                "jobautomation/OpenEuroLLM-Czech:latest")))
    timeout = int(cfg.get("llm_timeout", 300))
    try:
        from scripts.ollama_client import ollama_generate
        from scripts.hans_questions import HansQuestionsStore
        from scripts.hans_persona import persona_core, persona_name
    except Exception as e:
        _log.warning("generate_interest_questions: import selhal: %s", e)
        return 0
    store = PersonInterestStore(config, diary_db_path)
    qstore = HansQuestionsStore(diary_db_path, config)
    system = persona_core(config, with_address=False)
    written = 0
    for person in persons:
        pnorm = _norm(person)
        ints = store.interests_for(pnorm, limit=10)
        if len(ints) >= min_known:
            continue
        known_block = ""
        if ints:
            known_block = ("Už víš, že osobu " + person + " zajímá: "
                           + ", ".join(i.interest for i in ints)
                           + ". Zeptej se na NĚCO JINÉHO, ať ji poznáš víc.\n")
        user = (known_block
                + "Chceš lépe poznat osobu jménem " + person + " — co ji baví, "
                "čemu se věnuje ve volném čase, co má ráda. Polož JEDNU krátkou, "
                "vřelou a přirozenou českou otázku, kterou se nenásilně zeptáš na "
                "její zájmy. Žádný výslech, žádný akademický tón. Vrať jen tu otázku.")
        try:
            out = ollama_generate(model=model, prompt=user, system=system,
                                  config=config, timeout=timeout,
                                  keep_alive=0, options={"temperature": 0.5})
        except Exception as e:
            _log.warning("generate_interest_questions LLM (%s): %s", person, e)
            continue
        q = (out or "").strip()
        if "?" in q:
            q = q.split("?")[0].strip() + "?"
        q = q.strip(" \"'\n\t-—")
        if len(q) < 8 or len(q) > 240:
            continue
        qid = qstore.add_question(
            question=q, target_person=pnorm, source_type="interest",
            context=(persona_name(config) + " chce lépe poznat zájmy osoby " + person))
        if qid:
            written += 1
            _log.info("interest-probe Q[%s] pro %s: %.60s", qid, pnorm, q)
    _log.info("generate_interest_questions: vytvořeno %d otázek", written)
    return written


def generate_personal_questions(config: dict, diary_db_path: str) -> int:
    """HANS_PERSONAL_QUESTIONS_V1 (#3) — pro osoby z domu občas vytvoří JEDNU
    vřelou OSOBNÍ otázku (jak se daří, co potěšilo, na co se těší) — projev zájmu
    o člověka, ne o téma z četby. Nezávislé na tom, zda proběhl dialog. Surfacing
    přes HANS_QUESTIONS_SURFACING_V1. Denní strop drží add_question. Vrací počet."""
    pcfg = (config.get("personal_questions", {}) or {})
    if not pcfg.get("enabled", True):
        return 0
    persons = [p for p in (config.get("known_persons", {}) or {}).keys()
               if (p or "").strip()]
    if not persons:
        return 0
    er = config.get("evening_reflection", {}) or {}
    model = str(pcfg.get("model", er.get("model",
                "jobautomation/OpenEuroLLM-Czech:latest")))
    timeout = int(pcfg.get("llm_timeout", 300))
    try:
        from scripts.ollama_client import ollama_generate
        from scripts.hans_questions import HansQuestionsStore
        from scripts.hans_persona import persona_core, persona_name
    except Exception as e:
        _log.warning("generate_personal_questions: import selhal: %s", e)
        return 0
    store = PersonInterestStore(config, diary_db_path)
    qstore = HansQuestionsStore(diary_db_path, config)
    system = persona_core(config, with_address=False)
    written = 0
    for person in persons:
        pnorm = _norm(person)
        ints = store.interests_for(pnorm, limit=6)
        known_block = ""
        if ints:
            known_block = ("Víš, že osobu " + person + " zajímá: "
                           + ", ".join(i.interest for i in ints)
                           + ". Můžeš na to lehce navázat, ale neptej se na fakta.\n")
        user = (known_block
                + "Chceš dát osobě jménem " + person + " najevo lidský zájem — ne o "
                "nějaké téma, ale o NI samotnou. Polož JEDNU krátkou, vřelou, osobní "
                "českou otázku: jak se jí daří, co ji v poslední době potěšilo, na co "
                "se těší, nebo jak strávila den — něco, na co může odpovědět cokoliv "
                "ze svého života. Žádný výslech, nic dotěrného, žádné téma z četby. "
                "Vrať jen tu jednu otázku.")
        try:
            out = ollama_generate(model=model, prompt=user, system=system,
                                  config=config, timeout=timeout,
                                  keep_alive=0, options={"temperature": 0.6})
        except Exception as e:
            _log.warning("generate_personal_questions LLM (%s): %s", person, e)
            continue
        q = (out or "").strip()
        if "?" in q:
            q = q.split("?")[0].strip() + "?"
        q = q.strip(" \"'\n\t-—")
        if len(q) < 8 or len(q) > 240:
            continue
        qid = qstore.add_question(
            question=q, target_person=pnorm, source_type="personal",
            context=(persona_name(config) + " chce projevit osobní zájem o " + person))
        if qid:
            written += 1
            _log.info("personal Q[%s] pro %s: %.60s", qid, pnorm, q)
    _log.info("generate_personal_questions: vytvořeno %d otázek", written)
    return written


# ── Smoke (python3 -m scripts.hans_person_interests) ────────────────────────
if __name__ == "__main__":
    import tempfile, os
    print("=== PersonInterestStore smoke (temp DB) ===")
    fd, tmp = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        st = PersonInterestStore({}, tmp)
        st.add_or_reinforce("alice", "fotbal", ["Real Madrid", "zápas"])
        st.add_or_reinforce("alice", "design", ["sériová výroba"])
        st.add_or_reinforce("alice", "Fotbal", ["liga"])  # dedup → reinforce
        st.add_or_reinforce("carol", "detektivky", ["Agatha Christie"])
        oi = st.interests_for("alice")
        print(f"  alice interests: {len(oi)} (čekáno 2)")
        for i in oi:
            print("   ", i, "ex=", i.examples)
        print("  persons:", st.persons_with_interests())
        print("  format_block(alice):", format_block(oi))
    finally:
        os.unlink(tmp)
    print("=== smoke OK ===")
