#!/usr/bin/env python3
"""
HANS_THREADS_V1 — Frontier #4 (Theory of mind), krok 1: rozjeté nitky per osoba.

Jádro: když někdo v dialogu zmíní něco s BUDOUCNOSTÍ („dcera má zkoušku",
„jedu na dovolenou", „čtu tu knihu"), Hans to zachytí jako OTEVŘENOU NITKU.
Když ta osoba PŘÍŠTĚ dorazí, navnáže follow-up otázkou („jak dopadla ta zkouška?").

Tok (deferral-safe, [[ollama-deferred-processing]] / [[ollama-vram-tiers]]):
  1. EXTRAKCE (nočně, base OpenEuroLLM keep_alive=0): projde denní `human_chat`
     per osoba → vytáhne budoucnost-orientované zmínky → open nitka + follow-up.
     Anti-konfabulace: jen co osoba DOSLOVNĚ řekla. Dedup proti otevřeným nitkám
     osoby (reuse-known jako STANCE_MERGE). Současně detekuje ROZUZLENÍ dříve
     otevřené nitky v pozdějším dialogu → close.
  2. SURFACING (voice-ready, single entry point `surface_for(person)`): vrátí
     nejvhodnější otevřenou nitku k navnaváze. Volá greeting i textový chat
     (`openwebui_direct_handler`); až bude hlas hotový, zavolá ji i voice path.
     Caller po injektnutí volá `mark_surfaced(id)` (cap proti otravování).

Tabulka `person_threads` v hans_diary.db:
  person, topic, context, follow_up, status(open/closed), created_ts, updated_ts,
  source_ref, times_surfaced, last_surfaced_ts, resolution

API:
  store = ThreadStore(config, diary_db_path)
  store.add_thread(person, topic, context, follow_up, source_ref="") -> id|None
  store.open_threads(person, limit=10) -> [Thread]
  store.surface_for(person, max_surfaces=3, cooldown_h=12) -> Thread|None
  store.mark_surfaced(thread_id) -> None
  store.close(topic_or_id, person=None, resolution="") -> bool
  extract_threads(config, diary_db_path, window_hours=26) -> dict  # noční krok
  format_block(threads, person=None) -> str   # do promptu (greeting/chat)
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from typing import List, Optional

_log = logging.getLogger("hans_threads")

_WS = re.compile(r"\s+")


def _norm(s: str) -> str:
    return _WS.sub(" ", (s or "").strip().lower())


class Thread:
    __slots__ = ("id", "person", "topic", "context", "follow_up", "status",
                 "created_ts", "updated_ts", "source_ref", "times_surfaced",
                 "last_surfaced_ts", "resolution", "due_ts")  # THREAD_DUE_MATURATION_V1

    def __init__(self, row):
        self.id = row["id"]
        self.person = (row["person"] or "").strip()
        self.topic = (row["topic"] or "").strip()
        self.context = (row["context"] or "").strip()
        self.follow_up = (row["follow_up"] or "").strip()
        self.status = row["status"] or "open"
        self.created_ts = row["created_ts"] or 0.0
        self.updated_ts = row["updated_ts"] or 0.0
        self.source_ref = row["source_ref"] or ""
        self.times_surfaced = row["times_surfaced"] or 0
        self.last_surfaced_ts = row["last_surfaced_ts"] or 0.0
        self.resolution = row["resolution"] or "" if "resolution" in row.keys() else ""
        # THREAD_DUE_MATURATION_V1 — 0 = bezčasová nitka; >0 = epoch dozrání
        self.due_ts = (row["due_ts"] or 0.0) if "due_ts" in row.keys() else 0.0

    def age_days(self) -> int:
        return max(0, int((time.time() - self.created_ts) / 86400))

    def as_dict(self) -> dict:
        return {"id": self.id, "person": self.person, "topic": self.topic,
                "follow_up": self.follow_up, "status": self.status,
                "age_days": self.age_days(),
                "times_surfaced": self.times_surfaced,
                "due_ts": self.due_ts}  # THREAD_DUE_MATURATION_V1

    def __repr__(self):
        return (f"<Thread {self.id} {self.person} {self.status} "
                f"surf={self.times_surfaced} {self.topic[:40]!r}>")


class ThreadStore:
    def __init__(self, config: dict, diary_db_path: str):
        self._diary_path = diary_db_path
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self._diary_path) as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS person_threads (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    person          TEXT NOT NULL,
                    topic           TEXT NOT NULL,
                    context         TEXT,
                    follow_up       TEXT,
                    status          TEXT NOT NULL DEFAULT 'open',
                    created_ts      REAL NOT NULL,
                    updated_ts      REAL NOT NULL,
                    source_ref      TEXT DEFAULT '',
                    times_surfaced  INTEGER NOT NULL DEFAULT 0,
                    last_surfaced_ts REAL NOT NULL DEFAULT 0,
                    resolution      TEXT DEFAULT ''
                )
            """)
            db.execute("CREATE INDEX IF NOT EXISTS idx_threads_person "
                       "ON person_threads(person, status)")
            try:  # THREAD_DUE_MATURATION_V1 — aditivní sloupec
                db.execute("ALTER TABLE person_threads ADD COLUMN due_ts REAL DEFAULT 0")
            except sqlite3.OperationalError:
                pass  # už existuje
            db.commit()

    def _connect(self):
        conn = sqlite3.connect(self._diary_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        return conn

    def add_thread(self, person: str, topic: str, context: str = "",
                   follow_up: str = "", source_ref: str = "",
                   due_ts: float = 0.0) -> Optional[int]:  # THREAD_DUE_MATURATION_V1
        """Otevře novou nitku. Dedup: pokud osoba má otevřenou nitku se shodným
        normalizovaným topicem, jen ji aktualizuje (updated_ts + context/follow_up)."""
        pnorm = _norm(person)
        tnorm = _norm(topic)
        if not pnorm or not tnorm:
            return None
        now = time.time()
        try:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT id FROM person_threads WHERE person=? AND status='open' "
                    "AND lower(topic)=? ORDER BY id LIMIT 1",
                    (pnorm, tnorm)).fetchone()
                if row is not None:
                    rid = row["id"]
                    conn.execute(
                        "UPDATE person_threads SET context=?, follow_up=?, "
                        "updated_ts=?, source_ref=?, due_ts=? WHERE id=?",  # THREAD_DUE_MATURATION_V1
                        (context.strip(), follow_up.strip(), now,
                         source_ref, float(due_ts or 0.0), rid))
                    conn.commit()
                    _log.info("thread UPDATE [%s] %s: %.50s", rid, pnorm, topic)
                    return rid
                cur = conn.execute(
                    "INSERT INTO person_threads (person, topic, context, follow_up, "
                    "status, created_ts, updated_ts, source_ref, due_ts) "  # THREAD_DUE_MATURATION_V1
                    "VALUES (?,?,?,?,'open',?,?,?,?)",
                    (pnorm, topic.strip(), context.strip(), follow_up.strip(),
                     now, now, source_ref, float(due_ts or 0.0)))
                conn.commit()
                rid = cur.lastrowid
                _log.info("thread NEW [%s] %s: %.50s", rid, pnorm, topic)
                return rid
            finally:
                conn.close()
        except Exception as e:
            _log.warning("ThreadStore.add_thread failed: %s", e)
            return None

    def open_threads(self, person: str, limit: int = 10) -> List[Thread]:
        pnorm = _norm(person)
        try:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT * FROM person_threads WHERE person=? AND status='open' "
                    "ORDER BY updated_ts DESC LIMIT ?",
                    (pnorm, limit)).fetchall()
                return [Thread(r) for r in rows]
            finally:
                conn.close()
        except Exception as e:
            _log.warning("open_threads failed: %s", e)
            return []

    def surface_for(self, person: str, max_surfaces: int = 3,
                    cooldown_h: float = 12.0) -> Optional[Thread]:
        """Voice-ready single entry point: nejvhodnější otevřená nitka osoby
        k navnaváze. Vynechá nitky přes cap (otravování) nebo v cooldownu.
        Read-only — caller po injektnutí do promptu volá mark_surfaced(id)."""
        pnorm = _norm(person)
        now = time.time()
        cutoff = now - cooldown_h * 3600.0
        try:
            conn = self._connect()
            try:
                # THREAD_DUE_MATURATION_V1 — nezralé (due_ts>now) vynech;
                # dozrálé (due_ts>0 & <=now) mají přednost, pak nejdřív splatné.
                row = conn.execute(
                    "SELECT * FROM person_threads WHERE person=? AND status='open' "
                    "AND times_surfaced < ? AND last_surfaced_ts <= ? "
                    "AND (due_ts = 0 OR due_ts <= ?) "
                    "ORDER BY (CASE WHEN due_ts > 0 THEN 0 ELSE 1 END) ASC, "
                    "times_surfaced ASC, due_ts ASC, updated_ts ASC LIMIT 1",
                    (pnorm, max_surfaces, cutoff, now)).fetchone()
                return Thread(row) if row else None
            finally:
                conn.close()
        except Exception as e:
            _log.warning("surface_for failed: %s", e)
            return None

    def mark_surfaced(self, thread_id: int) -> None:
        try:
            conn = self._connect()
            try:
                conn.execute(
                    "UPDATE person_threads SET times_surfaced=times_surfaced+1, "
                    "last_surfaced_ts=? WHERE id=?", (time.time(), thread_id))
                conn.commit()
            finally:
                conn.close()
        except Exception as e:
            _log.warning("mark_surfaced failed: %s", e)

    def close(self, topic_or_id, person: Optional[str] = None,
              resolution: str = "") -> bool:
        """Uzavře nitku — buď podle id (int), nebo podle (person, topic)."""
        now = time.time()
        try:
            conn = self._connect()
            try:
                if isinstance(topic_or_id, int) or (isinstance(topic_or_id, str)
                                                    and topic_or_id.isdigit()):
                    cur = conn.execute(
                        "UPDATE person_threads SET status='closed', updated_ts=?, "
                        "resolution=? WHERE id=? AND status='open'",
                        (now, resolution.strip(), int(topic_or_id)))
                else:
                    pnorm = _norm(person or "")
                    cur = conn.execute(
                        "UPDATE person_threads SET status='closed', updated_ts=?, "
                        "resolution=? WHERE person=? AND lower(topic)=? "
                        "AND status='open'",
                        (now, resolution.strip(), pnorm, _norm(topic_or_id)))
                conn.commit()
                ok = cur.rowcount > 0
                if ok:
                    _log.info("thread CLOSED %s (%s)", topic_or_id,
                              resolution[:40])
                return ok
            finally:
                conn.close()
        except Exception as e:
            _log.warning("ThreadStore.close failed: %s", e)
            return False


# ── Surfacing helper (do promptu greeting/chat) ─────────────────────────────
def format_block(threads: List[Thread], person: Optional[str] = None) -> str:
    """Kompaktní blok otevřených nitek do system promptu / greeting promptu."""
    if not threads:
        return ""
    lines = []
    for t in threads:
        fu = t.follow_up or f"zeptat se, jak to dopadlo s: {t.topic}"
        lines.append(f"- {fu}")
    return "\n".join(lines)


# ── Noční extrakce z dialogů ────────────────────────────────────────────────
def _gather_dialogs(diary_db_path: str, since: float) -> dict:
    """Vrátí {person_norm: [dialog_text, …]} z human_chat za okno. Read-only."""
    out: dict = {}
    conn = None
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % diary_db_path, uri=True,
                               timeout=5.0)
        rows = conn.execute(
            "SELECT title, note FROM diary WHERE event_type='human_chat' "
            "AND ts > ? ORDER BY ts ASC", (since,)).fetchall()
        for title, note in rows:
            p = _norm(title or "")
            if not p or not (note or "").strip():
                continue
            out.setdefault(p, []).append(note.strip())
    except Exception as e:
        _log.warning("_gather_dialogs failed: %s", e)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    return out


# PERSONA_NAME_CONFIGURABLE_V1 — {persona_name} se doplní z configu při .format()
_EXTRACT_SYSTEM = (
    "Jsi pozorný analytik vztahů postavy jménem {persona_name}. Dostaneš PŘEPIS "
    "dnešních rozhovorů s jednou osobou a seznam UŽ OTEVŘENÝCH NITEK s touto osobou. "
    "Tvůj úkol: najít, co osoba zmínila s BUDOUCNOSTÍ nebo nedokončeností — věci, "
    "na které se dá příště smysluplně navázat (např. 'dcera má příští týden zkoušku', "
    "'jedu na dovolenou do Itálie', 'čtu zrovna tu knihu', 'čeká nás stěhování'). "
    "Pro každou takovou věc vytvoř NITKU: topic (krátké téma), context (co přesně "
    "osoba řekla), follow_up (přirozená česká otázka, kterou se {persona_name} příště "
    "zeptá, jak to dopadlo). "
    # THREAD_EXTRACT_QUALITY_V1 — follow_up srozumitelný sám o sobě + dedup
    "DŮLEŽITÉ: follow_up MUSÍ být srozumitelný SÁM O SOBĚ — obsahovat konkrétní "
    "téma/podstatu, protože se vysloví izolovaně bez kontextu. NIKDY nepiš vágní "
    "otázky typu „co tě na tom napadlo?\" nebo „co si o tom myslíš?\" bez uvedení, "
    "ČEHO se týkají — pojmenuj věc (např. „jak pokračuje ta kniha Slupka všeho zla?\"). "
    "Mluvila-li osoba o jedné věci z více úhlů, vytvoř JEN JEDNU nitku "
    "(nefragmentuj jedno téma do několika nitek). "
    # THREAD_DUE_EXTRACT_V1 — date-maturation
    "Pokud má událost KONKRÉTNÍ ČAS (zkouška ve čtvrtek, dovolená příští týden, "
    "schůzka zítra), přidej i pole \"ask_after\" = datum ve formátu RRRR-MM-DD, "
    "OD KTERÉHO už bude po události a dá se smysluplně zeptat, jak dopadla "
    "(tj. PO zkoušce, PO návratu z dovolené). Dnešní datum dostaneš v přepisu. "
    "Když událost nemá jasný čas (čte knihu, obecný zájem), pole \"ask_after\" VYNECH. "
    "PŘÍSNĚ ANTI-KONFABULACE: vytěž JEN to, co osoba DOSLOVNĚ řekla v přepisu; "
    "NIC si nedomýšlej, nevymýšlej jména ani události. Mluví-li jen postava "
    "{persona_name}, ignoruj to (zajímá nás, co řekla OSOBA). Běžný pozdrav, dotaz "
    "na čas/počasí nebo jednorázová poznámka bez budoucnosti NENÍ nitka. "
    "Dále: projdi UŽ OTEVŘENÉ NITKY — pokud z přepisu plyne, že se některá VYŘEŠILA "
    "(osoba řekla, jak to dopadlo), zařaď ji do 'resolved' s krátkým shrnutím. "
    "Vrať VÝHRADNĚ JSON objekt: {{\"new\": [{{\"topic\":..., \"context\":..., "
    "\"follow_up\":..., \"ask_after\":...}}], \"resolved\": [{{\"topic\":..., "
    "\"resolution\":...}}]}}. "
    "Když není nic, vrať prázdná pole."
)


def _parse_obj(raw: str) -> dict:
    s = re.sub(r"^```(?:json)?|```$", "", (raw or "").strip(), flags=re.MULTILINE).strip()
    i, j = s.find("{"), s.rfind("}")
    if i == -1 or j == -1 or j < i:
        return {}
    try:
        data = json.loads(s[i:j + 1])
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _ask_after_to_ts(s: str) -> float:  # THREAD_DUE_EXTRACT_V1
    """ISO RRRR-MM-DD → epoch (lokální půlnoc). Cokoliv nevalidního nebo
    >365 dní dopředu → 0.0 (bezčasová nitka, bezpečný fallback)."""
    s = (s or "").strip()
    if not _ISO.match(s):
        return 0.0
    try:
        ts = time.mktime(time.strptime(s, "%Y-%m-%d"))
    except Exception:
        return 0.0
    if ts > time.time() + 365 * 86400:
        return 0.0  # absurdně daleko → nejspíš halucinace
    return ts


def extract_threads(config: dict, diary_db_path: str,
                    window_hours: float = 26.0) -> dict:
    """Noční krok: z denních human_chat vytáhne otevřené nitky per osoba +
    uzavře vyřešené. Vrací {'opened': n, 'closed': m}. LLM offline → {0,0}."""
    cfg = (config.get("threads", {}) or {})
    window_hours = float(cfg.get("window_hours", window_hours))
    since = time.time() - window_hours * 3600.0
    dialogs = _gather_dialogs(diary_db_path, since)
    if not dialogs:
        _log.info("extract_threads: žádné dialogy v okně, skip")
        return {"opened": 0, "closed": 0}

    store = ThreadStore(config, diary_db_path)
    er = config.get("evening_reflection", {}) or {}
    model = str(cfg.get("model", er.get("model",
                "jobautomation/OpenEuroLLM-Czech:latest")))
    timeout = int(cfg.get("llm_timeout", 300))
    max_open = int(cfg.get("max_open_per_person", 6))
    try:
        from scripts.ollama_client import ollama_generate
    except ImportError:
        _log.warning("extract_threads: ollama_client nedostupný, skip")
        return {"opened": 0, "closed": 0}
    try:
        from scripts.hans_persona import persona_name as _pn
        _system = _EXTRACT_SYSTEM.format(persona_name=_pn(config))
    except Exception:
        _system = _EXTRACT_SYSTEM.format(persona_name="Hans")

    opened = closed = 0
    for person, notes in dialogs.items():
        existing = store.open_threads(person, limit=max_open)
        open_block = ""
        if existing:
            open_block = ("UŽ OTEVŘENÉ NITKY:\n"
                          + "\n".join(f"- {t.topic}: {t.follow_up}"
                                      for t in existing) + "\n\n")
        transcript = "\n---\n".join(notes[-20:])
        _today = time.strftime("DNES je %Y-%m-%d (%A).")  # THREAD_DUE_EXTRACT_V1
        prompt = f"{_today}\n\n{open_block}PŘEPIS ROZHOVORŮ:\n{transcript}"
        try:
            raw = ollama_generate(model=model, prompt=prompt, system=_system,
                                  config=config, timeout=timeout,
                                  keep_alive=0,  # MODEL_KEEPALIVE_TIERS_V1
                                  options={"temperature": 0.2})
        except Exception as e:
            _log.warning("extract_threads: LLM call failed (%s): %s", person, e)
            continue
        obj = _parse_obj(raw)
        if not obj:
            continue
        for it in (obj.get("new") or [])[:max_open]:
            if not isinstance(it, dict):
                continue
            topic = (it.get("topic") or "").strip()
            if not topic:
                continue
            if store.add_thread(person, topic,
                                context=(it.get("context") or "").strip(),
                                follow_up=(it.get("follow_up") or "").strip(),
                                source_ref=time.strftime("%Y-%m-%d"),
                                due_ts=_ask_after_to_ts(it.get("ask_after"))):  # THREAD_DUE_EXTRACT_V1
                opened += 1
        for it in (obj.get("resolved") or []):
            if not isinstance(it, dict):
                continue
            topic = (it.get("topic") or "").strip()
            if not topic:
                continue
            if store.close(topic, person=person,
                           resolution=(it.get("resolution") or "").strip()):
                closed += 1

    _log.info("extract_threads: otevřeno %d, uzavřeno %d (osob: %d)",
              opened, closed, len(dialogs))
    return {"opened": opened, "closed": closed}


# ── Smoke (python3 -m scripts.hans_threads) ─────────────────────────────────
if __name__ == "__main__":
    import tempfile, os
    print("=== ThreadStore smoke (temp DB) ===")
    fd, tmp = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        st = ThreadStore({}, tmp)
        i1 = st.add_thread("alice", "dceřina zkouška",
                           context="dcera má příští týden zkoušku z matematiky",
                           follow_up="Jak dopadla zkouška vaší dcery?")
        st.add_thread("alice", "dovolená Itálie",
                      context="jedu na dovolenou do Itálie",
                      follow_up="Jaká byla dovolená v Itálii?")
        # dedup: stejný topic → update, ne nový
        st.add_thread("alice", "Dceřina Zkouška", context="x", follow_up="y")
        op = st.open_threads("alice")
        print(f"  open threads: {len(op)} (čekáno 2)")
        for t in op:
            print("   ", t)
        s = st.surface_for("alice")
        print("  surface_for:", s)
        if s:
            st.mark_surfaced(s.id)
            st.mark_surfaced(s.id)
            st.mark_surfaced(s.id)
            s2 = st.surface_for("alice", max_surfaces=3)
            print("  po 3 surfacech (cap=3) surface_for vrací:", s2,
                  "(čekán druhý topic nebo None v cooldownu)")
        ok = st.close("dovolená Itálie", person="alice", resolution="byla skvělá")
        print("  close by topic:", ok, "→ open:", len(st.open_threads("alice")))
        print("  format_block:\n" + format_block(st.open_threads("alice")))
    finally:
        os.unlink(tmp)
    print("=== smoke OK ===")
