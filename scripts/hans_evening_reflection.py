"""
HansEveningReflection — Hans 1× večer projde dnešní deník napříč
relevantními typy událostí, syntetizuje vlastní reflexi dne svým hlasem
a uploaduje ji do RAG kolekce hans_denik.

Volá se ručně z hans_routine, např.:
    refl = HansEveningReflection(config, diary_db_path, synthesis, knowledge)
    refl.run()  # vrátí text reflexe nebo None

Filozofie:
  - Hans dostane FAKTA o dni (co Kolač řekl, co četl, co se hrálo, atd.)
  - Synthesis modul s novým stylem 'evening_reflection' z toho udělá
    osobní zápis v Hansově hlase (1-2 odstavce)
  - Reflexe jde do deníku (event_type='evening_reflection') a do RAGu
"""
from __future__ import annotations

import logging
import sqlite3
import time
from datetime import datetime
from typing import Optional

_log = logging.getLogger(__name__)


# Které event_types brát do reflexe a jak se v promptu označí
_RELEVANT_EVENTS = {
    # HUMAN_CHAT_EVENING
    "human_chat":       "Rozhovory s lidmi",
    "fact_correction":  "Opravy faktů (ověřeno proti Wikipedii)",  # G5D_VERIFY_BEFORE_DIARY_V1
    "chat_reflection":  "Mé dojmy z rozhovorů",
    "teddy_dialog":     "Dialogy s Koláčem",
    "web_read":         "Co jsem četl",
    "kodi_playing":     "Co se hrálo na TV",
    "introspection":    "Vlastní úvahy",
    "room_description": "Co jsem viděl v místnosti",
    "movie_browsed":    "Filmy které jsem prohlížel",
    "case_opened":      "Otevřené případy",
    "spontaneous":      "Spontánní myšlenky",
    "observation":      "Pozorování",
    "morning_weather":  "Ranní počasí",
    "brain_up":         "Probuzení mysli",
    "brain_down":       "Uspání mysli",
    "brain_still_down": "Mysl stále spí",
    "body_warm":        "Tělesné pocity",
    "activity":         "Aktivita",
}

# Limity — kolik událostí každého typu vzít do promptu (šetří tokeny)
_LIMITS = {
    "human_chat":       6,
    "fact_correction":  5,  # G5D_VERIFY_BEFORE_DIARY_V1
    "chat_reflection":  6,
    "teddy_dialog":     8,
    "web_read":         5,
    "kodi_playing":     5,
    "introspection":    5,
    "room_description": 2,
    "movie_browsed":    3,
    "case_opened":      3,
    "spontaneous":      3,
    "observation":      3,
    "morning_weather":  1,
    "brain_up":         3,
    "brain_down":       3,
    "brain_still_down": 2,
    "body_warm":        3,
    "activity":         5,
}


class HansEveningReflection:
    """Generuje a ukládá Hansovu večerní reflexi dne."""

    def __init__(
        self,
        config: dict,
        diary_db_path: str,
        synthesis,        # HansSynthesis instance
        knowledge=None,   # HansKnowledge instance (může být None)
    ):
        self._diary_path = diary_db_path
        self._synthesis = synthesis
        self._knowledge = knowledge
        cfg = config.get("evening_reflection", {}) or {}
        self._max_tokens = int(cfg.get("max_tokens", 1200))
        self._knowledge_collection = cfg.get(
            "knowledge_collection", "hans_denik")

        # STANCE_EXTRACT_V1 — stance store + LLM extrakce config
        self._config = config
        self._stances = None
        try:
            from scripts.hans_stances import StanceStore
            self._stances = StanceStore(config, diary_db_path)
        except Exception as _e:
            _log.warning("StanceStore nedostupny (%s), stance extrakce vypnuta", _e)
        self._stance_model = str(cfg.get("model", "hans-czech:latest"))
        self._stance_timeout = int(cfg.get("llm_timeout", 300))
        self._stance_max_per_run = int(cfg.get("stance_max_per_run", 6))

    # ── Public API ───────────────────────────────────────────────────────────

    def run(self, target_date: Optional[str] = None) -> Optional[str]:
        """Vygeneruje reflexi pro daný den (default: dnes).

        Args:
            target_date: 'YYYY-MM-DD' nebo None (= dnes)

        Returns:
            Text reflexe, nebo None pokud se nepodařilo / není co reflektovat.
        """
        date_str = target_date or datetime.now().strftime("%Y-%m-%d")

        facts = self._collect_facts(date_str)
        if not facts:
            _log.info("Žádné události k reflexi pro %s", date_str)
            return None

        facts_text = self._format_facts(facts)
        _log.info("Reflexe %s: %d typů událostí, %d znaků faktů",
                  date_str, len(facts), len(facts_text))

        # Pošli do synthesis
        text = self._synthesis.synthesize(
            topic="můj den",
            facts=facts_text,
            style="evening_reflection",
            max_tokens=self._max_tokens,
            max_chars=2500,
        )
        if not text:
            _log.warning("Synthesis vrátila prázdnou reflexi pro %s", date_str)
            return None

        # Ulož do deníku
        self._write_to_diary(text, date_str)

        # Push do RAGu
        if self._knowledge and self._knowledge.enabled:
            self._upload_to_knowledge(text, date_str)

        # STANCE_EXTRACT_V1 — nazory z reflexe do stance store (mimo RAG)
        self._extract_stances(text, date_str)

        # HANS_TENDENCIES_V2 (3b) — po extrakci postojů přepočti tendence.
        # Tady (ne v calleru), ať to dostane manuální /denik i noční automatika.
        try:
            from scripts.hans_tendencies import snapshot as _tnd_snapshot
            _tnd_snapshot(self._config, self._diary_path, date_str)
        except Exception as _te:
            _log.warning("tendency snapshot selhal (reflexe OK): %s", _te)

        # AUTOBIOGRAPHICAL_IMPORTANCE_V1 — oskóruj neoskórované epizody dne
        # (base model, WHERE importance IS NULL = self-healing catch-up).
        try:
            from scripts.hans_importance import score_unscored as _imp_score
            _ic = (self._config.get("importance", {}) or {})
            # IMPORTANCE_NIGHTLY_CATCHUP_V1 — dávkový loop: pokryj denní příliv
            # i postupně vyprázdni historii (cap > inflow). keep_alive warm přes dávky.
            _batch = int(_ic.get("max_per_run", 30))
            _cap = int(_ic.get("nightly_cap", 400))
            _done = 0
            while _done < _cap:
                _n = _imp_score(self._config, self._diary_path,
                                self._stance_model, self._stance_timeout,
                                limit=min(_batch, _cap - _done),
                                keep_alive="5m")
                if not _n:
                    break
                _done += _n
            if _done:
                _log.info("importance: oskórováno %d epizod (noční catchup)", _done)
        except Exception as _ie:
            _log.warning("importance scoring selhal (reflexe OK): %s", _ie)

        # HANS_THREADS_WIRING_V1 — Frontier #4: z dnešních dialogů vytáhni
        # rozjeté nitky per osoba + uzavři vyřešené (base model keep_alive=0,
        # deferral-safe). Běží nočně vždy.
        try:
            from scripts.hans_threads import extract_threads as _xthreads
            _xthreads(self._config, self._diary_path)
        except Exception as _xe:
            _log.warning("extract_threads selhal (reflexe OK): %s", _xe)

        # HANS_CORRECTION_LEARNING_V1 (#4) — lekce z korekcí (kde mě opravili).
        # Nezávislé na ostatních; base model keep_alive=0, anti-konfabulace.
        try:
            from scripts.hans_lessons import extract_corrections as _xcorr
            _xcorr(self._config, self._diary_path)
        except Exception as _ce:
            _log.warning("extract_corrections selhal (reflexe OK): %s", _ce)

        # HANS_PERSON_INTERESTS_WIRING_V1 — per-osoba zájmy (přiživeno na
        # průchod nitek, sdílí _gather_dialogs). base model keep_alive=0.
        try:
            from scripts.hans_person_interests import extract_person_interests as _xpi
            _xpi(self._config, self._diary_path)
        except Exception as _pe:
            _log.warning("extract_person_interests selhal (reflexe OK): %s", _pe)

        # HANS_PERSONAL_QUESTIONS_V1 (#3) — vřelá OSOBNÍ otázka per osoba.
        # NEZÁVISLE na dialozích: i bez konverzace chce Hans projevit zájem.
        try:
            from scripts.hans_person_interests import generate_personal_questions as _gpq
            _gpq(self._config, self._diary_path)
        except Exception as _gqe:
            _log.warning("generate_personal_questions selhal (reflexe OK): %s", _gqe)

        # HANS_BOOK_MENTIONS_V1 — zmínky knih v chatu → Gutenberg → wishlist
        # (sdílí _gather_dialogs). base model keep_alive=0, síťový Gutendex lookup.
        try:
            from scripts.hans_book_mentions import extract_book_mentions as _xbm
            _xbm(self._config, self._diary_path)
        except Exception as _bme:
            _log.warning("extract_book_mentions selhal (reflexe OK): %s", _bme)

        # HANS_ROUTINE_PATTERNS_WIRING_V1 — deterministický presence profil
        # (Fáze 2b, žádný LLM) pro timing/kontext proaktivity.
        try:
            from scripts.hans_routine_patterns import RoutineStore as _RS
            _RS(self._config, self._diary_path).rebuild()
        except Exception as _re:
            _log.warning("routine_patterns rebuild selhal (reflexe OK): %s", _re)

        return text

    # ── Sběr faktů z deníku ──────────────────────────────────────────────────

    def reflect_on_book(self, book_title: str, summaries: list, date_str=None):
        """HANS_BOOK_COMPLETION_V1 — po dočtení knihy hluboká reflexe. Dočtená
        kniha = vytrvalé angažmá → SMÍ formovat postoje (na rozdíl od jednorázových
        reakcí). Syntéza ohlédnutí → deník + RAG + _extract_stances."""
        if not self._synthesis or not (book_title or "").strip():
            return None
        date_str = date_str or datetime.now().strftime("%Y-%m-%d")
        # HANS_BOOK_COMPLETION_V3 — materiál: předané souhrny, jinak Hansovy
        # VLASTNÍ per-kapitola reflexe (book_reflection.DATA — hook píše text do
        # sloupce 'data', ne 'note'), fallback surový text kapitol (book_read.note).
        mats = [str(s).strip() for s in (summaries or []) if s and str(s).strip()]
        if not mats:
            try:
                con = sqlite3.connect("file:%s?mode=ro" % self._diary_path,
                                      uri=True, timeout=3.0)
                rows = con.execute(
                    "SELECT data FROM diary WHERE event_type='book_reflection' "
                    "AND title LIKE ? AND data IS NOT NULL AND data!='' ORDER BY ts",
                    (book_title + "%",)).fetchall()
                if not rows:  # fallback: surový text kapitol
                    rows = con.execute(
                        "SELECT note FROM diary WHERE event_type='book_read' "
                        "AND title LIKE ? AND note IS NOT NULL AND note!='' ORDER BY ts",
                        (book_title + "%",)).fetchall()
                con.close()
                mats = [r[0].strip() for r in rows if r and r[0] and r[0].strip()]
            except Exception as _ge:
                _log.debug("book completion: diary gather failed: %s", _ge)
        body = "\n".join(f"- {s}" for s in mats)[:3000]
        facts = (
            f"Právě jsi dočetl knihu \u201e{book_title}\u201c. Tvé poznámky z kapitol:\n"
            f"{body}\n\nOhlédni se za celou knihou: co v tobě zůstává, co tě oslovilo "
            "nebo naopak dráždilo, a zda či jak to potvrdilo nebo proměnilo tvůj pohled "
            "na svět."
        )
        text = self._synthesis.synthesize(
            topic=f"dočtená kniha {book_title}", facts=facts,
            style="book_completion", max_tokens=max(self._max_tokens, 700),
            max_chars=2000, facts_max_chars=3000)  # HANS_BOOK_COMPLETION_STYLE_V1
        if not text:
            _log.warning("book completion: prázdná reflexe pro %s", book_title)
            return None
        try:
            db = sqlite3.connect(self._diary_path)
            db.execute(
                "INSERT INTO diary (ts, event_type, title, note) VALUES (?,?,?,?)",
                (time.time(), "book_completion_reflection",
                 f"Dočtená kniha — {book_title}", text))
            db.commit()
            db.close()
        except Exception as e:
            _log.warning("book completion diary write failed: %s", e)
        if self._knowledge and self._knowledge.enabled:
            try:
                self._knowledge.upload(
                    collection_key=self._knowledge_collection,
                    doc_id=f"book_{date_str.replace('-','')}_{abs(hash(book_title)) % 100000}",
                    title=f"Dočtená kniha — {book_title}", text=text,
                    metadata={"datum": date_str, "typ": "reflexe dočtené knihy",
                              "kniha": book_title})
            except Exception as e:
                _log.warning("book completion RAG upload failed: %s", e)
        # STANCES — dočtená kniha smí formovat postoje
        self._extract_stances(text, date_str)
        _log.info("Book completion reflexe + stances: %s (%d znaků)",
                  book_title, len(text))
        return text

    def reflect_on_work(self, topic, essay_text, date_str=None):
        """HANS_WORK_COMPLETION_V1 (B) — po dokoncení vlastního díla (esej k cíli)
        hluboká reflexe. Vlastní vytrvalá práce = legitimní kanál tvorby postojů
        (jako dočtená kniha). Syntéza ohlédnutí -> deník + RAG + _extract_stances."""
        if not self._synthesis or not (topic or "").strip():
            return None
        essay_text = (essay_text or "").strip()
        if not essay_text:
            return None
        date_str = date_str or datetime.now().strftime("%Y-%m-%d")
        facts = (
            "Právě jsi dopsal vlastní esej na téma " + str(topic).strip() + ". "
            "Tvůj text:\n" + essay_text[:3000] + "\n\nOhlédni se za tím, co jsi "
            "napsal: k čemu jsi při psaní došel a jaký trvalejší názor v tobě po "
            "sepsání zůstává."
        )
        text = self._synthesis.synthesize(
            topic="dokončené dílo " + str(topic).strip(), facts=facts,
            style="work_completion", max_tokens=max(self._max_tokens, 700),
            max_chars=2000, facts_max_chars=3200)
        if not text:
            _log.warning("work completion: prázdná reflexe pro %s", topic)
            return None
        try:
            db = sqlite3.connect(self._diary_path)
            db.execute(
                "INSERT INTO diary (ts, event_type, title, note) VALUES (?,?,?,?)",
                (time.time(), "work_completion_reflection",
                 "Dokončené dílo — " + str(topic).strip(), text))
            db.commit()
            db.close()
        except Exception as e:
            _log.warning("work completion diary write failed: %s", e)
        if self._knowledge and self._knowledge.enabled:
            try:
                self._knowledge.upload(
                    collection_key=self._knowledge_collection,
                    doc_id="work_%s_%d" % (date_str.replace("-", ""),
                                           abs(hash(topic)) % 100000),
                    title="Dokončené dílo — " + str(topic).strip(), text=text,
                    metadata={"datum": date_str, "typ": "reflexe dokončeného díla",
                              "tema": str(topic).strip()})
            except Exception as e:
                _log.warning("work completion RAG upload failed: %s", e)
        # STANCES — vlastní vytrvalé dílo smí formovat postoje (jako kniha)
        self._extract_stances(text, date_str)
        _log.info("Work completion reflexe + stances: %s (%d znaku)",
                  topic, len(text))
        return text

    def reflect_on_creations(self, date_str=None, window_days=14):
        """HANS_CREATION_REFLECTION_V1 (D) — periodická reflexe vlastní tvorby
        (úvahy/obrazy/eseje) jako SEBEPOZNÁNÍ. -> deník + RAG. NEvolá _extract_stances
        (sebepoznání, ne postoje). Krmí self-memories/narativ skrze importance."""
        if not self._synthesis:
            return None
        date_str = date_str or datetime.now().strftime("%Y-%m-%d")
        import json as _json
        items = []
        try:
            since = time.time() - window_days * 86400
            con = sqlite3.connect("file:%s?mode=ro" % self._diary_path,
                                  uri=True, timeout=3.0)
            for et, label in (("musing", "úvaha"), ("work_created", "esej")):
                rows = con.execute(
                    "SELECT title, COALESCE(note,'') FROM diary WHERE event_type=? "
                    "AND ts>? ORDER BY ts DESC LIMIT 6", (et, since)).fetchall()
                for t, n in rows:
                    body = (n or t or "").strip()
                    if body:
                        items.append("(" + label + ") " + body[:240])
            rows = con.execute(
                "SELECT title, COALESCE(data,'') FROM diary WHERE event_type='artwork' "
                "AND ts>? ORDER BY ts DESC LIMIT 6", (since,)).fetchall()
            for t, d in rows:
                cap = ""
                try:
                    cap = (_json.loads(d).get("caption") or "").strip()
                except Exception:
                    cap = ""
                lab = (t or cap or "").strip()
                if lab:
                    items.append("(obraz) " + lab[:240])
            con.close()
        except Exception as _ge:
            _log.debug("creation reflection gather failed: %s", _ge)
        if len(items) < 2:
            _log.info("creation reflection: málo tvorby (%d), skip", len(items))
            return None
        body = "\n".join("- " + s for s in items)[:2500]
        facts = ("Tady je přehled toho, co jsi v posledních dnech sám vytvořil:\n"
                 + body + "\n\nZamysli se, co tvá tvorba prozrazuje o tom, kdo teď jsi.")
        text = self._synthesis.synthesize(
            topic="má vlastní tvorba", facts=facts,
            style="creation_reflection", max_tokens=max(self._max_tokens, 600),
            max_chars=1800, facts_max_chars=2600)
        if not text:
            return None
        try:
            db = sqlite3.connect(self._diary_path)
            db.execute(
                "INSERT INTO diary (ts, event_type, title, note) VALUES (?,?,?,?)",
                (time.time(), "creation_reflection",
                 "Ohlédnutí za vlastní tvorbou", text))
            db.commit()
            db.close()
        except Exception as e:
            _log.warning("creation reflection diary write failed: %s", e)
        if self._knowledge and self._knowledge.enabled:
            try:
                self._knowledge.upload(
                    collection_key=self._knowledge_collection,
                    doc_id="creation_" + date_str.replace("-", ""),
                    title="Ohlédnutí za vlastní tvorbou", text=text,
                    metadata={"datum": date_str, "typ": "reflexe vlastní tvorby"})
            except Exception as e:
                _log.warning("creation reflection RAG upload failed: %s", e)
        _log.info("Creation reflexe (sebepoznání): %d znaku z %d del",
                  len(text), len(items))
        return text

    def _extract_stances(self, text: str, date_str: str):
        """STANCE_EXTRACT_V1 — z reflexe vytahne Hansovy nazory (claim +
        confidence) do StanceStore (mimo RAG). LLM dotaz; offline/parse fail
        -> tichy skip. Anti-konfabulace: jen nazory, ktere text vyjadruje."""
        if not text or not text.strip():
            return
        if not self._stances:
            return
        try:
            from scripts.ollama_client import ollama_generate
        except ImportError:
            _log.warning("stance extract: ollama_client nedostupny, skip")
            return
        # STANCE_DIALECTIC_PROMPT_V2 — 2-krok: revize polarity + nové postoje
        system = (
            "Jsi extraktor TRVALÝCH názorů a postojů z Hansovy večerní reflexe. "
            "Pracuj ve dvou krocích.\n"
            "KROK 1 — REVIZE: Projdi UŽ ZNÁMÉ POSTOJE. U každého, kterého se reflexe "
            "DOTÝKÁ, urči, zda ho reflexe POTVRZUJE, nebo POPÍRÁ/MĚNÍ. Když reflexe "
            "vyjadřuje OPAČNÝ či změněný postoj (vzor 'dřív… ale teď už ne', 'začalo "
            "mě to nudit'), vrať prvek s klíčem 'contradicts' = PŘESNÉ znění toho "
            "známého postoje a 'claim' = nový opačný názor. NIKDY takový popřený "
            "postoj neuváděj jako stále držený.\n"
            "KROK 2 — NOVÉ: Vytáhni nové trvalé postoje (hodnoty, preference), které "
            "reflexe vyjadřuje a neodpovídají ničemu známému. Obecné tvrzení v 1. osobě, "
            "BEZ odkazu na dnešek či konkrétní událost. NEZAHRNUJ popis dne, jednorázové "
            "reakce ani náladu. Při shodě se známým postojem použij JEHO přesné znění "
            "(posílení). Přiřaď confidence 0.0-1.0 = jak silně a trvale ten postoj platí.\n"
            "Volitelně přidej 'counterarg' = výhradu, kterou v reflexi sám "
            "vyslovil. NEVYMÝŠLEJ námitky ani postoje. Když nic, vrať []. "
            "Vrať VÝHRADNĚ JSON pole prvků s klíči claim, confidence a volitelně "
            "counterarg, contradicts."
        )
        # STANCE_MERGE_VIA_EXTRACTOR_V1 — dej LLM známé postoje, ať u shody reusne přesné znění
        try:
            _known = self._stances.top_stances(limit=40)
        except Exception:
            _known = []
        _known_block = ""
        if _known:
            _known_block = (
                "UŽ ZNÁMÉ POSTOJE (při shodě použij přesné znění):\n"
                + "\n".join(f"- {s.claim}" for s in _known)
                + "\n\n"
            )
        prompt = f"{_known_block}REFLEXE ({date_str}):\n{text.strip()[:3000]}"
        try:
            raw = ollama_generate(
                model=self._stance_model, prompt=prompt, system=system,
                config=self._config, timeout=self._stance_timeout,
                keep_alive=0,  # MODEL_KEEPALIVE_TIERS_V1 — analytika on-demand, po extrakci uvolni VRAM
                options={"temperature": 0.2},
            )
        except Exception as _e:
            _log.warning("stance extract: LLM call failed: %s", _e)
            return
        if not raw:
            _log.info("stance extract: zadna LLM odpoved, skip")
            return
        items = self._parse_stances(raw)
        if not items:
            _log.info("stance extract: 0 nazoru z reflexe %s", date_str)
            return
        written = 0
        weakened = 0
        for _it in items[: self._stance_max_per_run]:
            if not isinstance(_it, dict):
                continue
            claim = (_it.get("claim") or "").strip()
            _ca = _it.get("counterarg")  # STANCE_DIALECTIC_PROMPT_V2 — model může vrátit list
            if isinstance(_ca, list):
                _ca = " ".join(str(x) for x in _ca)
            counterarg = (str(_ca).strip() if _ca else "") or None
            _co = _it.get("contradicts")
            if isinstance(_co, list):
                _co = _co[0] if _co else ""
            contradicts = str(_co or "").strip()
            # STANCE_DIALECTIC_EXTRACT_V1 — obrat názoru: oslab dřívější postoj
            if contradicts:
                if self._stances.contradict(
                        contradicts, counter_claim=(claim or counterarg),
                        source="evening_reflection"):
                    weakened += 1
                    continue
                # cíl se nenašel → propadni na běžné zpracování claimu
            if not claim:
                continue
            if self._stances.add_or_reinforce(
                    claim, _it.get("confidence", 0.5), "evening_reflection",
                    counterarg=counterarg):
                written += 1
        _log.info("stance extract %s: zpracovano %d, oslabeno %d / %d nazoru",
                  date_str, written, weakened, len(items))

    @staticmethod
    def _parse_stances(raw: str):
        """Robustni extrakce JSON pole z LLM odpovedi (toleruje ```json fences)."""
        import json as _json, re as _re
        s = _re.sub(r"^```(?:json)?|```$", "", (raw or "").strip(),
                    flags=_re.MULTILINE).strip()
        i, j = s.find("["), s.rfind("]")
        if i == -1 or j == -1 or j < i:
            return []
        try:
            data = _json.loads(s[i:j + 1])
        except Exception:
            return []
        return data if isinstance(data, list) else []

    def _collect_facts(self, date_str: str) -> dict:
        """Vrátí {event_type: [(title, note), ...]} pro daný den."""
        out: dict[str, list[tuple[str, str]]] = {}
        try:
            db = sqlite3.connect(self._diary_path)
            for evt, limit in _LIMITS.items():
                rows = db.execute(
                    "SELECT title, note FROM diary "
                    "WHERE event_type=? "
                    "AND date(ts,'unixepoch','localtime')=? "
                    "ORDER BY ts ASC LIMIT ?",
                    (evt, date_str, limit),
                ).fetchall()
                if rows:
                    out[evt] = [(r[0] or "", r[1] or "") for r in rows]
            db.close()
        except Exception as e:
            _log.error("Sběr faktů selhal: %s", e)
        return out

    def _format_facts(self, facts: dict) -> str:
        """Zformátuje fakta pro LLM prompt — čistý seznam událostí
        po kategoriích, bez instrukcí (ty patří do system promptu)."""
        parts = []
        for evt, items in facts.items():
            label = _RELEVANT_EVENTS.get(evt, evt)
            parts.append(f"[{label}]")
            for title, note in items:
                note_short = note[:300].strip()
                if title and note_short:
                    parts.append(f"  • {title}: {note_short}")
                elif title:
                    parts.append(f"  • {title}")
                elif note_short:
                    parts.append(f"  • {note_short}")
            parts.append("")
        return "\n".join(parts).strip()

    # ── Zápis ────────────────────────────────────────────────────────────────

    def _write_to_diary(self, text: str, date_str: str):
        try:
            db = sqlite3.connect(self._diary_path)
            db.execute(
                "INSERT INTO diary (ts, event_type, title, note) "
                "VALUES (?,?,?,?)",
                (time.time(), "evening_reflection",
                 f"Reflexe dne {date_str}", text),
            )
            db.commit()
            db.close()
            _log.info("Reflexe zapsána do deníku (%d znaků)", len(text))
        except Exception as e:
            _log.error("Zápis reflexe do deníku selhal: %s", e)

    def _upload_to_knowledge(self, text: str, date_str: str):
        try:
            doc_id = f"reflection_{date_str.replace('-', '')}"
            ok = self._knowledge.upload(
                collection_key=self._knowledge_collection,
                doc_id=doc_id,
                title=f"Reflexe dne — {date_str}",
                text=text,
                metadata={"datum": date_str, "typ": "večerní reflexe"},
            )
            if ok:
                _log.info("Reflexe pushnuta do RAG: %s/%s",
                          self._knowledge_collection, doc_id)
            else:
                _log.warning("RAG upload reflexe selhal")
        except Exception as e:
            _log.error("RAG upload error: %s", e)
