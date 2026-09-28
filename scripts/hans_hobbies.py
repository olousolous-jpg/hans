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


# ── HANS_HOBBY_NEAR_DUP_V1 (31.8.) ──────────────────────────────────
# Názvy koníčků vymýšlí LLM (`distill_hobbies`) a dedup běžel na PŘESNOU
# shodu `name_norm` (= jen lowercase + mezery). Model ale píše překlepy,
# takže z JEDNOHO zájmu vznikly TŘI řádky:
#   „historie a památky" (11) · „história a památky" (60) · „hřádění a památky" (2)
# Škoda nebyla kosmetická: ~1330 zaujetí se rozdělilo na třetiny, správně
# pojmenovaný řádek propadl filtrem `min_recent_days` a živil to překlep.
# ⛔ Do promptu už věta „použij JEHO PŘESNÝ název" PATŘÍ a NEFUNGUJE →
#    oprava musí být na ZÁPISOVÉ cestě, ne další věta ([[prompt-debt-tool-calling]]).
#
# ⚠️ PRÁH JE ZMĚŘENÝ, NE ODHADNUTÝ (všechny dvojice ze 17 koníčků):
#     0.944  historie a památky × história a památky   ← duplicita
#     0.686  hřádění a památky  × obojí výše           ← duplicita
#     0.500  fotografie × fotbal                       ← LEGITIMNÍ, nesmí splynout
#     0.490  hrady a historická architektura × historie a památky  ← LEGITIMNÍ
# Auto-slučuje se jen od 0.85 (bezpečná rezerva 0.35 k nejbližší legitimní
# dvojici). Pásmo 0.62–0.85 se NESLUČUJE, jen HLÁSÍ — ať se nová varianta
# ukáže, místo aby tiše založila čtvrtý řádek. Diakritika se pro porovnání
# odstraňuje („história" ~ „historia").
_MERGE_AUTO = 0.85
_MERGE_HLAS = 0.62


def _bez_diakritiky(s: str) -> str:
    import unicodedata as _u
    return "".join(c for c in _u.normalize("NFKD", s or "")
                   if not _u.combining(c))


def _podobnost(a: str, b: str) -> float:
    import difflib as _d
    return _d.SequenceMatcher(None, _bez_diakritiky(a),
                              _bez_diakritiky(b)).ratio()


# ── HANS_HOBBY_EXAMPLES_V1 (26. 9.) — PŘÍKLADY KONÍČKŮ, KTERÉ SEDÍ ─────────
# Změřeno 26. 9.: příklady byly (a) ZAMRZLÉ — ukládalo se prvních 20, nové se
# po naplnění nedostaly dovnitř; (b) vymyšlené nebo obecné („Design“ u Designu,
# věty „Dříve jsem přemýšlel o filmu 'X'.“); (c) jedno téma u více koníčků
# („Tančící figurky“ — Sherlockova povídka — u fotografie, japonské kultury
# i rostlin). Síla koníčku se počítá NAD příklady, takže chyba se přenáší.
# Porovnání významu (bge-m3) s názvem koníčku ZKOUŠENO A ZAMÍTNUTO: film či
# album se slovu „filmy“/„hudba“ nepodobá (Marketa Lazarová 0,31, The Fat of
# the Land 0,34) a Tančící figurky nechytil. Soudce (qwen2.5:7b) s kontextem
# z deníku: zjevné chyby vyřadil všechny, mýlí se ~12 % — převážně vyhodí
# správný příklad (bezpečnější strana).
_PRIKLAD_FILM = re.compile(r"^Dříve jsem přemýšlel o filmu ['„\"](.+?)['“\"]\.?$")
_PRIKLAD_PREDPONA = re.compile(r"^Seznam dílů (?:seriálu|pořadu)\s+", re.I)
# zástupné náměty dialogu s Koláčem (`hans_dialog`: weather/observation/free,
# pečivo) — podnět okolí, ne zájem; „počasí venku“ jinak vyrobilo koníček „počasí“
_PRIKLAD_PRYC = re.compile(r"^(Zprávy:|Události(?: v regionech)?$|počasí venku$|co je v místnosti$|"
                           r"dnešní den$|pečivo \()", re.I)


def cisti_priklad(x: str, hobby: str = "") -> str:
    """Kanonický tvar příkladu, nebo '' když to příklad není."""
    t = (x or "").strip()
    m = _PRIKLAD_FILM.match(t)
    if m:
        t = m.group(1).strip()
    t = _PRIKLAD_PREDPONA.sub("", t).strip()
    if len(t) < 3 or _PRIKLAD_PRYC.match(t) or (
            hobby and _podobnost(_norm(t), _norm(hobby)) >= _MERGE_AUTO):
        return ""                 # vč. překlepu ve vlastním názvu („historia…“)
    return t


_SOUDCE_SYSTEM = (
    "Rozhoduješ, jestli konkrétní téma patří pod koníček (je jeho příkladem). "
    "Téma je název článku, pořadu, filmu nebo námětu, ke kterému dostaneš krátký "
    "kontext z deníku. Patří jen tehdy, když je téma OBSAHEM toho koníčku (film "
    "o hradech patří pod hrady; detektivní povídka nepatří pod rostliny jen kvůli "
    "slovu v názvu).")


def _kontext_tematu(conn, t: str) -> str:
    try:
        r = conn.execute(
            "SELECT event_type, substr(replace(coalesce(nullif(note,''),data,''),"
            "char(10),' '),1,220) FROM diary WHERE title=? AND event_type IN "
            "('kodi_playing','movie_opinion','web_read','reading_takeaway',"
            "'movie_browsed','book_reflection') ORDER BY ts DESC LIMIT 1",
            (t,)).fetchone()
    except Exception:
        r = None
    return ("%s: %s" % r) if r else "(bez záznamu v deníku)"


def soudce_prikladu(config: dict, hobby: str, tema: str, kontext: str):
    """True/False; None = soudce nedostupný (herní mód, LLM dole)."""
    try:
        from scripts.ollama_client import ollama_generate
        raw = ollama_generate(
            str((config.get("hobbies", {}) or {}).get("judge_model", "qwen2.5:7b")),
            "Koníček: %s\nTéma: %s\nKontext: %s" % (hobby, tema, kontext),
            system=_SOUDCE_SYSTEM, config=config, timeout=60, keep_alive=300,
            format={"type": "object", "properties": {"patri": {"type": "boolean"}},
                    "required": ["patri"]},
            options={"temperature": 0, "num_predict": 20})
        if not raw:
            return None
        return bool(json.loads(raw).get("patri"))
    except Exception as e:
        _log.debug("soudce příkladu: %s", e)
        return None


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
        self._config = config or {}   # HANS_LEARNING_PROGRESS_V1
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
            # HANS_HOBBY_SILA_V1 — síla = zaujetí s vyhasínáním (poločas 30 d)
            try:
                db.execute("ALTER TABLE hobbies ADD COLUMN sila REAL NOT NULL DEFAULT 0")
            except sqlite3.OperationalError:
                pass
            # TRENDS_HISTORY_V1 — časová stopa evidence_count (graf růstu zájmu).
            db.execute("""
                CREATE TABLE IF NOT EXISTS hobby_history (
                    id             INTEGER PRIMARY KEY AUTOINCREMENT,
                    hobby_id       INTEGER NOT NULL,
                    ts             REAL NOT NULL,
                    evidence_count INTEGER NOT NULL,
                    event          TEXT
                )
            """)
            db.execute("CREATE INDEX IF NOT EXISTS idx_hobbyhist_hid "
                       "ON hobby_history(hobby_id, ts)")
            db.execute(
                "INSERT INTO hobby_history (hobby_id, ts, evidence_count, event) "
                "SELECT id, last_seen, evidence_count, 'seed' FROM hobbies h "
                "WHERE NOT EXISTS (SELECT 1 FROM hobby_history hh "
                "                  WHERE hh.hobby_id = h.id)")
            db.commit()

    def _connect(self):
        conn = sqlite3.connect(self._diary_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _hist(conn, hobby_id, ts, count, event):
        """TRENDS_HISTORY_V1 — jeden bod růstu zájmu (append-only)."""
        try:
            conn.execute(
                "INSERT INTO hobby_history (hobby_id, ts, evidence_count, event) "
                "VALUES (?,?,?,?)", (hobby_id, ts, int(count), event))
        except Exception:
            pass

    def history(self, hobby_id: int = None, limit: int = 2000):
        """READ-ONLY časová stopa evidence_count (pro graf)."""
        try:
            conn = self._connect()
            try:
                if hobby_id is None:
                    rows = conn.execute(
                        "SELECT hobby_id, ts, evidence_count, event FROM hobby_history "
                        "ORDER BY ts ASC LIMIT ?", (limit,)).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT hobby_id, ts, evidence_count, event FROM hobby_history "
                        "WHERE hobby_id=? ORDER BY ts ASC LIMIT ?",
                        (hobby_id, limit)).fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()
        except Exception as e:
            _log.warning("HobbyStore.history failed: %s", e)
            return []

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
                    # HANS_HOBBY_NEAR_DUP_V1 — přesná shoda selhala; než založíš
                    # nový koníček, zkus BLÍZKÝ existující (překlep od LLM).
                    blizky, skore = None, 0.0
                    for r2 in conn.execute(
                            "SELECT id, name, name_norm, examples FROM hobbies "
                            "WHERE status='active'").fetchall():
                        s = _podobnost(norm, r2["name_norm"])
                        if s > skore:
                            blizky, skore = r2, s
                    if blizky is not None and skore >= _MERGE_AUTO:
                        _log.info("hobby NEAR-DUP: %.40r ~ %.40r (%.2f) → posiluji "
                                  "existující [%s]", name, blizky["name"], skore,
                                  blizky["id"])
                        row = blizky          # spadne do větve „posílit"
                    elif blizky is not None and skore >= _MERGE_HLAS:
                        _log.warning("hobby: %.40r je podezřele blízko %.40r "
                                     "(%.2f) — zakládám NOVÝ, zkontroluj",
                                     name, blizky["name"], skore)
                if row is None:
                    conn.execute(
                        "INSERT INTO hobbies (name, name_norm, evidence_count, "
                        "first_seen, last_seen, examples, status) "
                        "VALUES (?,?,1,?,?,?,'active')",
                        (name.strip(), norm, now, now,
                         json.dumps(examples, ensure_ascii=False)))
                    conn.commit()
                    rid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                    self._hist(conn, rid, now, 1, "new"); conn.commit()
                    _log.info("hobby NEW [%s]: %.50s", rid, name)
                    return rid
                rid = row["id"]
                # HANS_HOBBY_EXAMPLES_V1 — NEJNOVĚJŠÍ první (dřív se po 20
                # položkách nic nového nedostalo dovnitř)
                stare = _load_examples(row["examples"])
                nove = {_norm(e) for e in examples}
                merged = list(examples) + [x for x in stare if _norm(x) not in nove]
                conn.execute(
                    "UPDATE hobbies SET evidence_count=evidence_count+1, last_seen=?, "
                    "examples=? WHERE id=?",
                    (now, json.dumps(merged[:20], ensure_ascii=False), rid))
                _newc = conn.execute("SELECT evidence_count FROM hobbies WHERE id=?",
                                     (rid,)).fetchone()[0]
                self._hist(conn, rid, now, _newc, "reinforce")
                conn.commit()
                _log.info("hobby REINFORCE [%s] (n+1): %.50s", rid, name)
                return rid
            finally:
                conn.close()
        except Exception as e:
            _log.warning("HobbyStore.add_or_reinforce failed: %s", e)
            return None

    def prepocitej_silu(self, polocas_dni: float = 30.0) -> dict:
        """HANS_HOBBY_SILA_V1 (26. 9.) — SÍLA KONÍČKU ROZLIŠUJE.
        `evidence_count` rostl +1 za každou noc, kdy model koníček zmínil,
        a nikdy neklesal: 12 koníčků mělo 64–84 bez ohledu na skutečný zájem.
        Síla = zaujetí (`hans_study._topic_engagement`: bez ozvěny studia,
        1 podnět za den, Kodi ×0,5) nad příklady, s vyhasínáním — podnět
        starý `polocas_dni` má poloviční váhu (literatura o degenerate
        feedback loops: decay místo věčného součtu). `evidence_count` zůstává
        jako VYTRVALOST (gate trvalých koníčků), pořadí řídí síla."""
        from scripts.hans_study import _topic_engagement
        # HANS_LEARNING_PROGRESS_V1 — síla × (0,5 + learning progress):
        # téma, o kterém Hans čte, ale nic nového se nedozví, slábne
        from scripts import hans_learning as _hl
        _lp = (_hl.learning_progress(self._config, self._diary_path)
               if _hl.zapnuto(self._config) else {})
        out = {}
        try:
            conn = self._connect()
            try:
                for r in conn.execute("SELECT id, name, examples FROM hobbies "
                                      "WHERE status='active'").fetchall():
                    s = float(_topic_engagement(self._diary_path,
                                                _load_examples(r["examples"]),
                                                polocas_dni=polocas_dni) or 0)
                    s *= _hl.nasobek(_lp, r["name"])
                    conn.execute("UPDATE hobbies SET sila=? WHERE id=?", (s, r["id"]))
                    out[r["name"]] = s
                conn.commit()
            finally:
                conn.close()
        except Exception as e:
            _log.warning("prepocitej_silu: %s", e)
        return out

    def top_hobbies(self, limit: int = 10) -> List[Hobby]:
        try:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT * FROM hobbies WHERE status='active' "
                    "ORDER BY sila DESC, evidence_count DESC, last_seen DESC LIMIT ?",
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
                    "ORDER BY sila DESC, evidence_count DESC",
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


# ── HANS_INTEREST_NO_ECHO_V1 (26. 9.) — ZÁJMY BEZ OZVĚNY VLASTNÍHO STUDIA ──
# Změřeno 26. 9.: za 30 dní šlo 18 % podkladů koníčků z dialogů s Koláčem,
# jejichž téma dosadilo AKTIVNÍ STUDIUM (`hans_dialog` priorita „study“) —
# „hudba“ 202× (5.–18. 9. = program Hudba), „fotbal“ 129×. Studium tak
# vyrábělo důkaz zájmu o sebe sama (kruh studium → dialog → koníček →
# zaujetí → studium); `_topic_engagement` navíc počítal i vlastní study_note.
# Literatura: degenerate feedback loop (Jiang a kol. 2019) — důkazy vyrobené
# samotným systémem nepočítat, opakování téhož podnětu nesčítat.
# Rozhodnutí uživatele: Kodi (co sleduje rodina) ZŮSTÁVÁ jako okno do světa,
# ale ČÁSTEČNĚ — váha 0,5 (i četba spuštěná televizí `[kodi]`).
KODI_VAHA = 0.5


def studijni_temata(conn) -> set:
    """Normalizovaná témata a pod-témata všech studijních programů."""
    from scripts.hans_study import _norm as _sn
    out = set()
    try:
        for t, cur in conn.execute("SELECT topic, curriculum FROM study_program"):
            out.add(_sn(t or ""))
            try:
                out |= {_sn(str(x)) for x in json.loads(cur or "[]")}
            except Exception:
                pass
    except Exception:
        pass
    out.discard("")
    return out


def tema_dialogu(note: str) -> str:
    return _topic_from_teddy_note(note)


def gather_topics(diary_db_path: str, window_days: int = 30,
                  min_count: int = 3) -> List[tuple]:
    """Vrátí [(téma, count)] opakujících se témat napříč streamy. Read-only."""
    since = time.time() - window_days * 86400
    counts: dict = {}

    def _bump(topic: str, by: float = 1):
        t = cisti_priklad(topic)     # HANS_HOBBY_EXAMPLES_V1 — kanonický tvar
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
        # dialogy: téma z note — HANS_INTEREST_NO_ECHO_V1: téma ze studia se
        # nepočítá, téže téma za den jen jednou, dialog o TV titulu ×0,5
        from scripts.hans_study import _norm as _sn
        _stud = studijni_temata(conn)
        _tv = {(t or "").strip().lower() for t, in conn.execute(
            "SELECT title FROM diary WHERE event_type IN ('kodi_playing',"
            "'movie_browsed') AND ts > ?", (since - window_days * 86400,))}
        _videno = set()
        for note, den in conn.execute(
                "SELECT note, date(ts,'unixepoch','localtime') FROM diary "
                "WHERE event_type='teddy_dialog' AND ts > ?", (since,)).fetchall():
            _t = _topic_from_teddy_note(note)
            if not _t or _sn(_t) in _stud or (_t.lower(), den) in _videno \
                    or not cisti_priklad(_t):        # zástupný námět dialogu
                continue
            _videno.add((_t.lower(), den))
            _film = "přemýšlel o filmu" in _t.lower()
            _t = cisti_priklad(_t)                   # „…o filmu 'X'.“ → „X“
            _bump(_t, KODI_VAHA if (_film or _t.lower() in _tv) else 1)
        # web/filmy/kodi: titulky
        # HANS_HOBBY_NO_GOAL_READS_V1 (2.9.) — čtení, které si OBJEDNAL aktIVNÍ
        # CÍL (`[goal]` v note), se do koníčků nepočítá. Jinak si cíl vyrobí
        # vlastní koníček a ten pak teče do Severky jako „podpis osobnosti".
        # Doloženo: cíl „Design" objednal 20 čtení TÉHOŽ článku za týden
        # (a 18 z 19 cílů za tři měsíce byl Design), takže koníček „Design"
        # má jako jediný `examples = ["Design"]` — obecné slovo místo
        # vlastních jmen, protože vznikl z jednoho pořád dokola čteného
        # článku, který se tak jmenuje.
        # ⚠️ Retroaktivně to NIC nespraví (starší čtení značku nemá, měřeno:
        # 111 → 91 v okně 30 dní, práh `min_count` 3 to nepřekročí ani tak).
        # Je to pojistka DOPŘEDU, aby si příští cíl koníček nevyrobil.
        # Týž filtr má od 28.8. `hans_distillation._select_candidates`.
        _videno2 = set()   # HANS_INTEREST_NO_ECHO_V1 — titul 1× za den
        for evt in ("web_read", "movie_browsed", "kodi_playing"):
            _kde = ("SELECT title, note, date(ts,'unixepoch','localtime') "
                    "FROM diary WHERE event_type=? AND ts > ?")
            if evt == "web_read":
                _kde += " AND COALESCE(note,'') NOT LIKE '[goal]%'"
            for title, note, den in conn.execute(_kde, (evt, since)).fetchall():
                _k = ((title or "").strip().lower(), den, evt == "web_read")
                if _k in _videno2:
                    continue
                _videno2.add(_k)
                _tv_w = (evt != "web_read"
                         or (note or "").startswith("[kodi]"))
                _bump(title, KODI_VAHA if _tv_w else 1)
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
    topics_block = "\n".join(f"- {t} (×{c:g})" for t, c in topics)
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
    # HANS_HOBBY_EXAMPLES_V1 — příklad jen ze SKUTEČNÝCH témat, jen u jednoho
    # koníčku, jen se souhlasem soudce; koníček bez platného příkladu se
    # neposílí (tvrzení modelu bez důkazu). Soudce nedostupný → fail-closed.
    _temata = {_norm(cisti_priklad(t)): cisti_priklad(t) for t, _ in topics
               if cisti_priklad(t)}
    _prideleno = set()
    try:
        _kc = sqlite3.connect("file:%s?mode=ro" % diary_db_path, uri=True, timeout=5.0)
    except Exception:
        _kc = None
    for it in items[:_max]:
        if not isinstance(it, dict):
            continue
        name = (it.get("hobby") or "").strip()
        if not name:
            continue
        ex = it.get("examples")
        ex = ex if isinstance(ex, list) else ([ex] if ex else [])
        platne, stopa = [], []
        for e in ex:
            c = cisti_priklad(str(e), name)
            k = _norm(c)
            if not c or k not in _temata or k in _prideleno:
                stopa.append("%s:%s" % (str(e)[:30], "mimo témata" if c and k not in _temata
                                        else ("už jinde" if c else "není příklad")))
                continue
            v = soudce_prikladu(config, name, _temata[k],
                                _kontext_tematu(_kc, _temata[k]) if _kc else "")
            if v:
                platne.append(_temata[k]); _prideleno.add(k)
            else:
                stopa.append("%s:%s" % (_temata[k][:30], "soudce ne" if v is False
                                        else "soudce nedostupný"))
        if not platne:
            _log.info("distill_hobbies: '%s' bez platného příkladu → neposiluji %s",
                      name, stopa[:6])
            continue
        if store.add_or_reinforce(name, platne):
            written += 1
            _log.info("distill_hobbies: '%s' ← %s (vyřazeno %s)", name, platne,
                      stopa[:6])
    if _kc is not None:
        _kc.close()
    _log.info("distill_hobbies: zpracováno %d koníčků z %d témat", written, len(topics))
    try:   # HANS_LEARNING_PROGRESS_V1 — nová četba ke koníčkům (před silou)
        from scripts import hans_learning as _hl
        if _hl.zapnuto(config):
            _n = _hl.prirad_novou_cetbu(config, diary_db_path)
            _lp = _hl.learning_progress(config, diary_db_path)
            _log.info("distill_hobbies: learning progress (přiřazeno %d nových) %s", _n,
                      {k: (None if v[0] is None else round(v[0], 2), v[1])
                       for k, v in sorted(_lp.items())})
    except Exception as e:
        _log.warning("distill_hobbies: learning progress: %s", e)
    try:   # HANS_HOBBY_SILA_V1 — síla po nočním zobecnění
        _s = store.prepocitej_silu(float(cfg.get("sila_polocas_dni", 30)))
        _log.info("distill_hobbies: síla %s", {k: round(v, 1) for k, v in
                  sorted(_s.items(), key=lambda kv: -kv[1])[:8]})
    except Exception as e:
        _log.warning("distill_hobbies: přepočet síly: %s", e)
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
        try:                                   # HANS_MAIN_CONFIG_IO_V1
            from scripts.config_io import load as _cfg_load
        except ImportError:                    # spusteno jako skript, root chybi
            import sys as _s, os as _o
            _s.path.insert(0, _o.path.dirname(_o.path.dirname(_o.path.abspath(__file__))))
            from scripts.config_io import load as _cfg_load
        cfg = _cfg_load()
    except Exception as exc:  # noqa
        print("WARN: config.json nenačten (%s)" % exc)
    db = cfg.get("diary_db", "data/hans_diary.db")
    print("=== gather_topics (read-only, všechny streamy, 30 dní) ===")
    for t, c in gather_topics(db):
        print(f"  ×{c:<3} {t}")
