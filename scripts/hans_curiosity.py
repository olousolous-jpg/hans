"""
Hans Curiosity Engine
Hans se zajímá o věci které vidí, slyší nebo mu připomenou jeho zájmy.

Zdroje zvědavosti (v pořadí priority):
  1. Kodi právě hraje film/seriál → hledej na Wikipedii
  2. Detekovaný objekt v místnosti → zajímavý fakt
  3. Hansovy zájmy (sopky, literatura, etiketa) → čtení na témata
  4. Zprávy → jednou denně stáhne headlines a vybere jednu

Výstup:
  - Záznam do deníku ("přečteno")
  - Dostupný pro LLM kontext (build_llm_context())
  - Může se spontánně zmínit v dialogu s Kolačem
"""

import logging
import random
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional

from scripts.web_reader import WebReader, ReadResult
from scripts.hans_questions import (
    HansQuestionsStore, generate_question_via_llm)

_log = __import__("scripts.logger", fromlist=["get_logger"]).get_logger("hans_curiosity")

# Hansovy zájmy — klíčová slova pro Wikipedia hledání
# Čerpáno z hans_dialog.kolac_interests a hans_dialog.hans_interests
# CURIOSITY_BROADEN_V1 — rozšířeno z 17 na 66 dotazů (9 kat. + filmy)
_INTEREST_QUERIES = {
    "sopky": [
        "supervulkán Yellowstone", "Mauna Loa sopka",
        "Krakatoa výbuch", "Vesuv Pompeje", "Island sopky",
        "Stromboli sopka", "Etna sopka", "Krakatau",
        "podmořské sopky", "hydrotermální průduchy", "Campi Flegrei",
    ],
    "literatura": [
        "Oscar Wilde", "P.G. Wodehouse", "Agatha Christie",
        "anglická viktoriánská literatura", "Sherlock Holmes",
        "Charles Dickens", "Jules Verne", "Edgar Allan Poe",
        "Jane Austenová", "sestry Brontëovy",
    ],
    "etiketa": [
        "anglická etiketa", "victorian etiquette", "butlerství tradice",
        "stolování ve viktoriánské době", "dress code",
        "společenské hierarchie", "anglická konverzace", "dopisování etiketa",
    ],
    "detektivky": [
        "Hercule Poirot", "Miss Marple", "zlatý věk detektivní literatury",
        "locked room mystery",
        "Arthur Conan Doyle", "Dorothy L. Sayersová",
        "G. K. Chesterton", "Mike Hammer",
    ],
    "gastronomie": [
        "skotská whisky", "anglický čaj", "sýr Cheddar", "sušenky",
        "klasická anglická kuchyně", "viktoriánské recepty",
        "čokoláda historie",
    ],
    "kuriozity_prirody": [
        "hlubinné ryby", "jeskynní systémy", "jeskynní malby",
        "polární záře", "slavné meteority", "krasové jevy",
        "sloupcovitý čedič",
    ],
    "historie": [
        "viktoriánská éra", "průmyslová revoluce",
        "britská koloniální éra", "Velký požár Londýna",
        "viktoriánské vynálezy", "světové výstavy",
    ],
    "psychologie": [
        "lidská paměť", "snění", "halucinace", "optické iluze",
        "hypnóza", "déjà vu",
    ],
    "umeni": [
        "Johann Sebastian Bach", "baroko hudba",
        "slavné obrazy", "Auguste Rodin",
        "anglické katedrály", "gotické umění",
    ],
    "filmy": [],   # doplňují se dynamicky z Kodi
}

# COCO objekty → Wikipedia témata (co Hans hledá když vidí objekt)
_OBJECT_TOPICS = {
    "book":         "zajímavá knižní novinka",
    "clock":        "historické hodiny slavné",
    "vase":         "čínský porcelán historie",
    "scissors":     "řemesla a nůžky",
    "bottle":       "víno nebo whisky výroba",
    "remote":       None,   # nezajímavé
    "cell phone":   None,
    "laptop":       None,
    "tv":           None,
    "teddy bear":   "plyšoví medvídci historie původ",
    "chair":        "design nábytku 19 století",
    "couch":        "anglický interiér victorian",
    "potted plant": "pokojové rostliny exotické",
    "cup":          "čajová tradice Anglie",
    "wine glass":   "sommelier víno",
    "umbrella":     "deštník Victorian Anglie",
}


class HansCuriosity:
    """
    Zvědavostní engine — spouští čtení na základě triggerů.
    Bezpečný pro vlákna — čtení vždy probíhá na pozadí.
    """

    def __init__(self, config: dict, diary_db_path: str,
                 diary_writer=None):
        # DIARY_WRITER_PATCH_CURIOSITY
        self._diary_writer = diary_writer
        self.config        = config
        self._reader       = WebReader(config)
        self._diary_path   = diary_db_path
        self._ollama       = config.get("openwebui_chat", {}).get(
                               "base_url", "http://127.0.0.1:11434")
        # Priorita: models.utility → hans_dialog.ollama_model →
        #           openwebui_chat.model_name → fallback
        self._model        = (config.get("models", {}).get("utility")
                              or config.get("hans_dialog", {}).get("ollama_model")
                              or config.get("openwebui_chat", {}).get(
                                  "model_name", "hans-czech:latest"))
        self._lock         = threading.Lock()
        self._busy         = False   # právě čte → nespouštěj další

        # Cooldown per trigger type (sekundy)
        self._cooldowns = {
            "kodi":     3600,    # 1× za hodinu na film
            "object":   7200,    # 1× za 2h na objekt
            "interest": 5400,    # 1× za 1.5h na zájem
            "news":     43200,   # 1× za 12h zprávy
        }
        self._last_read: dict[str, float] = {}  # topic_key → timestamp

        # Cache posledního přečteného — pro LLM kontext
        self._recent: list[ReadResult] = []
        self._max_recent = 5

        # Načti z deníku co Hans naposledy četl
        self._load_recent_from_diary()

        # Otázky pro obyvatele — asynchronní fronta
        self._questions = HansQuestionsStore(diary_db_path, config)
        self._known_persons = list(config.get("known_persons", {}).keys())
        self._last_q_target_idx = 0   # round-robin přes obyvatele
        self._last_expire_check = 0.0

        _log.info("HansCuriosity ready — interests: %s, targets: %s",
                  list(_INTEREST_QUERIES.keys()),
                  self._known_persons or ["anyone"])

    # ── Triggery ──────────────────────────────────────────────────────────────

    def trigger_kodi(self, title: str, media_type: str = "movie"):
        """
        Kodi hraje film/seriál → Hans si ho chce vyhledat.
        """
        if not title or self._on_cooldown("kodi", title):
            return
        _log.info("Curiosity trigger: Kodi — '%s'", title)
        self._read_async(
            fn    = lambda: self._reader.wikipedia_read(title),
            topic = "kodi",
            key   = title,
        )

    def trigger_object(self, obj_name: str):
        """
        Hans vidí objekt → hledá zajímavý fakt.
        """
        topic_query = _OBJECT_TOPICS.get(obj_name)
        if topic_query is None:  # explicitně ignorovaný objekt
            return
        if not topic_query or self._on_cooldown("object", obj_name):
            return
        _log.info("Curiosity trigger: object — '%s' → '%s'", obj_name, topic_query)
        self._read_async(
            fn    = lambda q=topic_query: self._reader.wikipedia_read(q),
            topic = "object",
            key   = obj_name,
        )

    def trigger_topic(self, topic: str):  # GOAL_FOCUS_2C_V1
        """Cilene cteni o tematu aktivniho cile (faze 2c — soustredeni)."""
        topic = (topic or "").strip()
        if not topic or self._on_cooldown("topic", topic):
            return
        _log.info("Curiosity trigger: topic — '%s' (cil)", topic)
        self._read_async(
            fn    = lambda q=topic: self._reader.wikipedia_read(q),
            topic = "interest",
            key   = "goal:%s" % topic[:40],
        )

    def trigger_interest(self):
        """
        Periodický trigger — Hans sleduje svůj zájem.
        Vybere náhodnou kategorii a z ní náhodný dotaz.
        """
        if self._on_cooldown("interest", "any"):
            return

        # Dynamicky přidej filmy z Kodi do kategorie "filmy"
        # (volá se externě přes set_kodi_movies)
        categories = {k: v for k, v in _INTEREST_QUERIES.items() if v}
        if not categories:
            return

        cat  = random.choice(list(categories.keys()))
        query = random.choice(categories[cat])
        _log.info("Curiosity trigger: interest — %s / '%s'", cat, query)
        self._read_async(
            fn    = lambda q=query: self._reader.wikipedia_read(q),
            topic = "interest",
            key   = "any",
        )

    def trigger_news(self):
        """
        Jednou denně stáhne zprávy a vybere co Hanse zaujalo.
        """
        if self._on_cooldown("news", "daily"):
            return
        feed = random.choice(["zpravy", "veda", "kultura"])
        _log.info("Curiosity trigger: news — feed=%s", feed)
        self._read_async(
            fn    = lambda f=feed: self._reader.rss_summary(f),
            topic = "news",
            key   = "daily",
        )

    def trigger_random_wiki(self):  # CURIOSITY_BROADEN_V1
        """Náhodný článek z cs.wikipedia — zdroj překvapení mimo zájmy.
        Vola Wiki API list=random, dostane title, předá _reader.wikipedia.
        Bez cooldownu (zapojí _activity_read pravděpodobností)."""
        import json
        import urllib.request
        try:
            url = ('https://cs.wikipedia.org/w/api.php'
                   '?action=query&list=random'
                   '&rnnamespace=0&rnlimit=1&format=json')
            req = urllib.request.Request(url,
                headers={'User-Agent': 'HansBot/1.0 (curiosity)'})
            with urllib.request.urlopen(req, timeout=10) as r:
                data = json.loads(r.read().decode('utf-8'))
            randoms = data.get('query', {}).get('random', [])
            if not randoms:
                _log.warning('Curiosity random_wiki: API vrátilo prázdný seznam')
                return
            title = randoms[0].get('title', '')
            if not title:
                return
            _log.info("Curiosity trigger: random_wiki — '%s'", title)
            self._read_async(
                fn    = lambda t=title: self._reader.wikipedia_read(t),
                topic = 'random_wiki',
                key   = 'any',
            )
        except Exception as _e:
            _log.warning('Curiosity random_wiki selhalo: %s', _e)

    def trigger_url(self, url: str, topic: str = "url"):
        """
        Manuální trigger — Hans přečte konkrétní URL.
        Lze volat z chatu (/read <url>).
        """
        if self._busy:
            _log.info("WebReader busy — URL trigger skipped")
            return
        _log.info("Curiosity trigger: URL — %s", url)
        self._read_async(
            fn    = lambda u=url, t=topic: self._reader.fetch_url(u, t),
            topic = "url",
            key   = url,
        )

    def add_kodi_movies(self, movies: list[dict]):
        """Přidej názvy filmů z Kodi do zájmů."""
        titles = [m.get("title", "") for m in movies if m.get("title")]
        _INTEREST_QUERIES["filmy"] = titles[:20]

    def trigger_question(self, context: str,
                         source_type: str = "thought"):
        """
        Hans si přečetl/viděl něco a chce vědět víc.

        source_type="observation" nebo "room":
            → LLM vygeneruje otázku a uloží ji do hans_questions
              pro rodinu (Wikipedia by neznala odpověď).
        jiný source_type:
            → LLM vygeneruje otázku a hledá odpověď na Wikipedii.

        context: co Hans viděl/slyšel
        """
        if self._busy:
            return
        _log.info("Self-question trigger [%s]: %s",
                  source_type, context[:60])

        if source_type in ("observation", "room"):
            # Nespouštět web_read — jen generuj otázku pro rodinu
            import threading as _th
            _th.Thread(
                target=self._route_to_family_queue,
                args=(context, source_type),
                daemon=True,
                name="HansQ:family",
            ).start()
        else:
            self._read_async(
                fn    = lambda: self._ask_and_search(context),
                topic = "self_question",
                key   = context[:40],
            )

    def _route_to_family_queue(self, context: str,
                               source_type: str = "observation",
                               delay: int = 60):
        """
        Pro observation/room triggery:
        Vygeneruje otázku a uloží ji do hans_questions pro rodinu.
        Wikipedia search se nespustí — rodina zná místnost lépe.
        """
        import time as _t
        _t.sleep(delay)  # Ollama může ještě pracovat

        question = self._generate_question(context)
        if not question:
            _log.debug("_route_to_family_queue: no question generated")
            return

        target = self._pick_target_person_for_context(context)
        from scripts.hans_persona import persona_name as _pn  # PERSONA_NAME_CONFIGURABLE_V1
        ctx_note = (f"{_pn(self.config)} si všiml: {context[:120]}")
        qid = self._questions.add_question(
            question      = question,
            target_person = target,
            source_type   = source_type,
            source_ref    = "",
            context       = ctx_note,
        )
        if qid:
            _log.info("Q[%d] (family, %s) for %s: %s",
                      qid, source_type, target, question)

    def _ask_and_search(self, context: str):
        """
        Krok 1: LLM vygeneruje otázku kterou by Hans chtěl zodpovědět.
        Krok 2: Hans hledá odpověď na Wikipedii.
        """
        # Krok 1 — vygeneruj otázku
        question = self._generate_question(context)
        if not question:
            return None
        _log.info("Hans se ptá: %s", question)

        # Krok 2 — hledej odpověď
        result = self._reader.wikipedia_read(question)
        if result:
            result.topic = "self_question"
            # Přidej kontext otázky k summary
            result.summary = f"[Otázka: {question}] {result.summary}"
        return result

    def _generate_question(self, context: str) -> str | None:
        """LLM vygeneruje jednu zvídavou otázku na základě kontextu.
        # CURIOSITY_MEMORY_PATCH
        Předá LLM seznam předchozích otázek aby se Hans neopakoval."""
        recent_qs = self._recent_questions(limit=15)
        history_block = ""
        if recent_qs:
            history_block = (
                "\n\nNa tyto otázky ses už ptal v posledních dnech — "
                "NEOPAKUJ je ani jejich varianty:\n"
                + "\n".join(f"- {q}" for q in recent_qs)
                + "\n\nVygeneruj otázku na ÚPLNĚ jiný aspekt nebo téma.\n"
            )

        # PERSONA_REFACTOR_7_8 — identita z persona_core (system), úkol (user)
        from scripts.hans_persona import persona_core
        sys_prompt = persona_core(self.config, with_address=False)
        user_prompt = (
            "Zaznamenal jsi toto: "
            + context
            + history_block
            + "\nPolož jednomu ze členů domácnosti jednu krátkou, přirozenou a "
            "vřelou otázku. V otázce LEHCE zmiň, co tě k ní přivedlo, ať tázaný "
            "ví, proč se ptáš (např. „Když jsem si všiml…, napadlo mě, …?“). "
            "Žádný kvíz ani akademický tón — mluv lidsky. "
            "DŮLEŽITÉ: ten, koho se ptáš, o tom tématu nejspíš nic odborného "
            "neví — nepředpokládej u něj znalosti. Zeptej se tak, aby mohl "
            "odpovědět z vlastní zkušenosti, pocitu nebo názoru, ne na fakta "
            "či odbornost. "
            # QUESTIONS_OBSERVATION_TIDY_V1 — vyjdi JEN ze vstupu, nevymýšlej scénu
            "Vyjdi POUZE z toho, co je ve vstupu výše — nevymýšlej si scénu, "
            "činnost ani věci, které tam nejsou (zahlédl-li jsi jen předmět, "
            "drž se toho předmětu). "
            "Vrať POUZE tu jednu otázku jako jednu větu (smíš ji uvést půlvětou "
            "proč se ptáš). NEpřevyprávěj, co jsi viděl, a nepiš žádný samostatný "
            "úvod ani druhou větu před otázkou. Bez uvozovek, česky."  # QUESTIONS_NATURAL_V1 + QUESTIONS_ACCESSIBLE_V1
        )
        from scripts.ollama_client import ollama_chat
        try:
            q = ollama_chat(
                self._model,
                [{"role": "system", "content": sys_prompt},
                 {"role": "user", "content": user_prompt}],
                ollama_url=self._ollama,
                options={"num_predict": 80},  # QUESTIONS_NATURAL_V1 — místo na úvod
            )
            if q and len(q) > 5:
                # QUESTIONS_OBSERVATION_TIDY_V1 — model občas předřadí
                # převyprávěcí úvod + prázdný řádek; vezmi vlastní otázku.
                q = q.strip()
                if "\n" in q:
                    _parts = [p.strip() for p in q.splitlines() if p.strip()]
                    _ql = [p for p in _parts if "?" in p]
                    q = (_ql[-1] if _ql else (_parts[-1] if _parts else q))
                return q
        except Exception as e:
            _log.warning("Question generation error: %s", e)
        return None

    def _recent_questions(self, limit: int = 25) -> list[str]:
        """Vrátí seznam posledních otázek (z self_question + hans_questions).
        # CURIOSITY_MEMORY_ORDER_FIX
        Slouží jako paměť aby se Hans neopakoval. self_question první =
        Wikipedia search z curiosity má prioritu před family questions."""
        out: list[str] = []

        # 1. Self-question otázky v diary (Wikipedia search) — PRIORITA
        try:
            import sqlite3, re
            conn = sqlite3.connect(self._diary_path)
            rows = conn.execute(
                "SELECT note FROM diary WHERE event_type='web_read' "
                "AND note LIKE '%self_question%' "
                "ORDER BY ts DESC LIMIT ?",
                (limit,)
            ).fetchall()
            conn.close()
            for (note,) in rows:
                m = re.search(r"\[Otázka:\s*(.+?)\]", note or "")
                if m:
                    out.append(m.group(1).strip())
        except Exception as e:
            _log.debug("_recent_questions diary error: %s", e)

        # 2. Otázky uložené do hans_questions (pro rodinu)
        try:
            qs = self._questions.list_questions(limit=limit)
            for q in qs:
                if hasattr(q, "question") and q.question:
                    out.append(q.question.strip())
        except Exception as e:
            _log.debug("_recent_questions store error: %s", e)

        # Deduplikace (preserve order), max limit
        seen = set()
        dedup = []
        for q in out:
            key = q.lower()
            if key not in seen and len(q) > 5:
                seen.add(key)
                dedup.append(q)
                if len(dedup) >= limit:
                    break
        return dedup

    # ── LLM kontext ───────────────────────────────────────────────────────────

    def get_context_string(self, max_items: int = 3) -> str:
        """
        Vrať co Hans nedávno četl — pro system prompt.
        """
        if not self._recent:
            return ""
        items = self._recent[:max_items]
        from scripts.hans_persona import persona_name as _pn  # PERSONA_NAME_CONFIGURABLE_V1
        lines = [f"{_pn(self.config)} si nedávno přečetl:"]
        for r in items:
            from datetime import datetime
            dt = datetime.fromtimestamp(r.fetched_at).strftime("%d.%m. %H:%M")
            lines.append(f"- [{dt}] {r.title}: {r.summary}")
        return "\n".join(lines)

    def get_latest(self) -> Optional[ReadResult]:
        """Vrátí poslední přečtenou věc — pro dialog s Kolačem."""
        return self._recent[0] if self._recent else None

    # ── Otázky pro obyvatele ──────────────────────────────────────────────────

    def _pick_target_person(self, result) -> str:
        """
        Vybere komu Hans položí otázku k tomuto čtení.
        V1: round-robin přes známé osoby. Pokud žádná → "anyone".
        V2 (TODO): LLM rozhodne podle profilu osoby a tématu.
        """
        persons = self._known_persons
        if not persons:
            return "anyone"
        if len(persons) == 1:
            return persons[0].lower()
        target = persons[self._last_q_target_idx % len(persons)].lower()
        self._last_q_target_idx += 1
        return target

    def _delayed_question(self, result: ReadResult, delay: int = 90):
        """Počká delay sekund, pak zkusí vygenerovat otázku.
        90s prodleva = Ollama stihne dokončit sumarizaci a dialog."""
        import time as _t
        _t.sleep(delay)
        self._generate_and_store_question(result)

    def _pick_target_person_for_context(self, context: str) -> str:
        """OBSERVATION_TARGET_CONTEXT_V1 — cíl observation otázky podle toho, O KOM
        pozorování je (jméno osoby v kontextu). Když nelze určit (nebo víc osob) →
        'anyone' (ať to dostane kdokoli přítomný). Dřív slepý round-robin → otázka
        o jedné osobě dostala náhodně cíl na jinou (záměna osob)."""
        ctx = (context or "").lower()
        matched = [p for p in (self._known_persons or [])
                   if p and p.lower() in ctx]
        if len(matched) == 1:
            return matched[0]
        return "anyone"

    def _active_goal_topic(self):
        """GOAL_ADVANCING_QUESTIONS_V1 — téma aktivního cíle (read-only)
        nebo None. Nikdy nevyhodí."""
        try:
            import sqlite3 as _s
            conn = _s.connect("file:%s?mode=ro" % self._diary_path,
                              uri=True, timeout=2.0)
            row = conn.execute(
                "SELECT topic FROM hans_goals WHERE status='active' "
                "ORDER BY opened_at DESC LIMIT 1").fetchone()
            conn.close()
            return (row[0].strip() if row and row[0] else "") or None
        except Exception as _e:
            _log.debug("_active_goal_topic failed: %s", _e)
            return None

    def _generate_and_store_question(self, result: ReadResult):
        """Po čtení vygeneruje otázku a uloží do hans_questions."""
        # Self-question už je otázka kterou si Hans odpověděl
        if result.topic == "self_question":
            return
        try:
            target = self._pick_target_person(result)
            question = generate_question_via_llm(
                topic         = result.title,
                summary       = result.summary,
                target_person = target,
                ollama_url    = self._ollama,
                model         = self._model,
                config        = self.config,
                goal          = self._active_goal_topic(),  # GOAL_ADVANCING_QUESTIONS_V1
            )
            if not question:
                _log.debug("No question generated for '%s'", result.title)
                return
            from scripts.hans_persona import persona_name as _pn  # PERSONA_NAME_CONFIGURABLE_V1
            context = (f"{_pn(self.config)} prečetl o tématu "
                       f"„{result.title}“ ({result.topic}).")
            source_type = "reading" if result.source != "diary" else "thought"
            qid = self._questions.add_question(
                question      = question,
                target_person = target,
                source_type   = source_type,
                source_ref    = result.title[:120],
                context       = context,
            )
            if qid:
                _log.info("Q[%d] for %s: %s", qid, target, question)
        except Exception as e:
            _log.warning("Question gen/store error: %s", e)

    def _maybe_expire_old_questions(self):
        """Periodická údržba — max 1× za 6h projde a expiruje staré."""
        now = time.time()
        if now - self._last_expire_check < 6 * 3600:
            return
        self._last_expire_check = now
        try:
            self._questions.expire_old()
        except Exception as e:
            _log.debug("expire_old: %s", e)

    # ── Interní ───────────────────────────────────────────────────────────────

    def _on_cooldown(self, trigger: str, key: str) -> bool:
        ck  = f"{trigger}:{key}"
        cd  = self._cooldowns.get(trigger, 3600)
        now = time.time()
        if now - self._last_read.get(ck, 0) < cd:
            return True
        return False

    def _read_async(self, fn, topic: str, key: str):
        """Spustí čtení na pozadí — neblokuje hlavní smyčku."""
        with self._lock:
            if self._busy:
                _log.debug("WebReader busy — skipping %s:%s", topic, key)
                return
            self._busy = True

        def _worker():
            try:
                result: Optional[ReadResult] = fn()
                # HANS_DEFERRED_SUMMARY_V1 — pending = mozek byl mimo, summary
                # se nevyrobila; přesto ULOŽ (raw_text se podrží, doplní catchup).
                if result and (result.summary or result.pending):
                    result.topic = topic
                    self._store(result)
                    ck = f"{topic}:{key}"
                    with self._lock:
                        self._last_read[ck] = time.time()
                    _log.info("Read OK [%s] '%s': %s",
                              topic, result.title, result.summary[:80])
                else:
                    _log.debug("Read returned empty for %s:%s", topic, key)
            except Exception as e:
                _log.error("Read error (%s:%s): %s", topic, key, e)
            finally:
                with self._lock:
                    self._busy = False

        threading.Thread(target=_worker, daemon=True,
                         name=f"HansCuriosity:{topic}").start()

    def _store(self, result: ReadResult):
        """Uloží výsledek do paměti a deníku."""
        # HANS_DEFERRED_SUMMARY_V1 — mozek byl mimo → poznatek se NEvyrobil.
        # Podrž raw_text v deníku (data JSON, pending:1), NEpřidávej do _recent
        # (Hans o tom nesmí mluvit jako o poznaném), nespouštěj otázku/entitu.
        # Catchup ho zpracuje, až Ollama naběhne.
        if getattr(result, "pending", False):
            self._store_pending(result)
            return

        with self._lock:
            self._recent.insert(0, result)
            self._recent = self._recent[:self._max_recent]

        # Ulož do deníku — přes diary_writer callback (spouští synthesis hook)
        _wr_title = result.title[:120]
        _wr_note = f"[{result.topic}] {result.summary}"
        if self._diary_writer:
            try:
                self._diary_writer("web_read", _wr_title, note=_wr_note)
            except Exception as e:
                _log.warning("Diary writer error: %s", e)
                self._diary_writer = None  # fallback na SQL
        if not self._diary_writer:
            try:
                conn = sqlite3.connect(self._diary_path)
                conn.execute(
                    "INSERT INTO diary (ts, event_type, title, note) VALUES (?,?,?,?)",
                    (result.fetched_at, "web_read", _wr_title, _wr_note))
                conn.commit()
                conn.close()
            except Exception as e:
                _log.warning("Diary store error: %s", e)

        # HANS_ENTITY_STORE_C1_V1 — zachyť entitu z přečteného článku (name=
        # vyřešený titul, gloss=první definiční věta ze zdroje → 0 konfabulace).
        # Jen wikipedia (definiční úvod); RSS/news mají první větu neurčitou.
        if getattr(result, "source", "") == "wikipedia":
            try:
                self._entity_store().capture_from_reading(
                    result.title, result.raw_text, url=result.url,
                    lang=self.config.get("curiosity", {}).get("wiki_lang", "cs"))
            except Exception as _ee:
                _log.debug("entity capture: %s", _ee)

        # Vygeneruj otázku se zpožděním — Ollama může být ještě zaneprázdněna
        import threading as _th
        _th.Thread(
            target=self._delayed_question,
            args=(result,), daemon=True,
            name='HansQ:delayed'
        ).start()
        self._maybe_expire_old_questions()

        # HANS_DEFERRED_SUMMARY_V1 — úspěšné čtení = mozek je prokazatelně
        # nahoře → dožeň dřívější odložené (pending) záznamy. Throttle uvnitř.
        _th.Thread(target=self._catchup_pending, daemon=True,
                   name='HansCatchup').start()

    def _store_pending(self, result: ReadResult):
        """HANS_DEFERRED_SUMMARY_V1 — ulož čtení, u kterého mozek nestihl
        vyrobit poznatek. Do deníku jde NEUTRÁLNÍ marker (ne raw text) a do
        `data` JSON se podrží raw_text pro pozdější zpracování v catchup.
        NIKDY se nevydává za Hansův poznatek a nejde do _recent."""
        import json as _json
        raw = (result.raw_text or "").strip()
        if not raw:
            _log.debug("pending bez raw_text (%s) — skip", result.title)
            return
        payload = _json.dumps({
            "pending":  1,
            "raw_text": raw[:8000],
            "query":    result.title,
            "source":   result.source,
            "topic":    result.topic,
            "url":      result.url,
        }, ensure_ascii=False)
        marker = f"[{result.topic}] (nezpracováno — mozek byl mimo, doženu to)"
        _title = result.title[:120]
        try:
            if self._diary_writer:
                self._diary_writer("web_read", _title, data=payload, note=marker)
            else:
                conn = sqlite3.connect(self._diary_path)
                conn.execute(
                    "INSERT INTO diary (ts, event_type, title, data, note) "
                    "VALUES (?,?,?,?,?)",
                    (result.fetched_at, "web_read", _title, payload, marker))
                conn.commit()
                conn.close()
            _log.info("Read DEFERRED [%s] '%s' — mozek mimo, podrženo (%d B raw)",
                      result.topic, result.title, len(raw))
        except Exception as e:
            _log.warning("Deferred store error: %s", e)

    def _catchup_batch(self, limit: int) -> int:
        """HANS_DEFERRED_SUMMARY_V1 — jádro: vezmi `limit` NEJSTARŠÍCH pending
        `web_read`, z podrženého raw_textu vyrob skutečný poznatek, přepiš
        deníkový řádek (marker → poznatek, pending:0). Vrací počet zpracovaných;
        0 = nic nezbývá NEBO mozek mlčí (raw se nevydává, řádek zůstane pending).
        Bez zámku — volá se pod `_catchup_lock` (viz `_catchup_pending`/`catchup_drain`)."""
        import json as _json
        try:
            from scripts.ollama_client import game_mode_on
            if game_mode_on():
                return 0  # herní mód → mozek schválně off, nedožeň teď
        except Exception:
            pass
        try:
            conn = sqlite3.connect(self._diary_path)
            rows = conn.execute(
                "SELECT id, data FROM diary WHERE event_type='web_read' "
                "AND data LIKE '%\"pending\": 1%' ORDER BY ts ASC LIMIT ?",
                (limit,)).fetchall()
        except Exception as e:
            _log.debug("catchup query: %s", e)
            return 0
        done = 0
        for _id, data in rows:
            try:
                d = _json.loads(data or "{}")
            except Exception:
                continue
            raw = (d.get("raw_text") or "").strip()
            query = d.get("query") or ""
            if not raw:
                continue
            summary = self._reader._summarize(
                text=raw, query=query,
                style=("Nejdřív JEDNOU větou stručně uveď, KDO nebo CO to je. "
                       "Pak JEDNOU větou napiš, co tě nejvíc zaujalo. Drž se "
                       "faktů z textu, nic si nepřidávej."),
                max_text=len(raw) or 6000,
            )
            if not summary:
                # mozek stále mlčí → nech pending, přeruš (další stejně selžou)
                break
            topic = d.get("topic") or "wikipedia"
            new_note = f"[{topic}] {summary}"
            d["pending"] = 0
            try:
                conn.execute(
                    "UPDATE diary SET note=?, data=? WHERE id=?",
                    (new_note, _json.dumps(d, ensure_ascii=False), _id))
                conn.commit()
                done += 1
                _log.info("Catchup web_read #%s zpracováno: %s", _id, summary[:70])
            except Exception as e:
                _log.debug("catchup update #%s: %s", _id, e)
        conn.close()
        return done

    def _catchup_lock_get(self):
        if not getattr(self, "_catchup_lock", None):
            self._catchup_lock = threading.Lock()
        return self._catchup_lock

    def _catchup_pending(self, limit: int = 5):
        """Per-read trigger — po úspěšném čtení dožeň malou dávku. Non-blocking:
        když už catchup/drain běží, tiše odejdi (ten backlog pokryje)."""
        lock = self._catchup_lock_get()
        if not lock.acquire(blocking=False):
            return
        try:
            n = self._catchup_batch(limit)
            if n:
                _log.info("HANS_DEFERRED_SUMMARY_V1 catchup: %d záznamů doplněno", n)
        finally:
            lock.release()

    def catchup_drain(self, batch: int = 10, max_batches: int = 200):
        """HANS_DEFERRED_SUMMARY_V1 — na naběhnutí mozku (brain_up) DOJEĎ CELÝ
        backlog odložených čtení, NEJSTARŠÍ NAPŘED, nečekej na další čtení.
        Blocking zámek (běží v bg threadu). Skončí, když nic nezbývá, mozek
        znovu spadne (batch vrátí 0) nebo po `max_batches` (pojistka)."""
        lock = self._catchup_lock_get()
        lock.acquire()
        try:
            total = 0
            for _ in range(max_batches):
                n = self._catchup_batch(batch)
                if n == 0:
                    break        # backlog prázdný nebo mozek zas mlčí
                total += n
            if total:
                _log.info("HANS_DEFERRED_SUMMARY_V1 drain (brain_up): %d záznamů dožito", total)
        finally:
            lock.release()

    def catchup_drain_async(self, batch: int = 10):
        """Spusť `catchup_drain` na pozadí (neblokuj volajícího — např.
        health-check thread na brain_up)."""
        threading.Thread(target=self.catchup_drain, kwargs={"batch": batch},
                         daemon=True, name="HansCatchupDrain").start()

    def _entity_store(self):
        # HANS_ENTITY_STORE_C1_V1 — lazy singleton EntityStore
        _es = getattr(self, "_es_inst", None)
        if _es is not None:
            return _es
        from scripts.hans_entities import EntityStore
        self._es_inst = EntityStore(self.config, self._diary_path)
        return self._es_inst

    def _load_recent_from_diary(self):
        """Načti co Hans četl naposledy (při startu)."""
        try:
            conn = sqlite3.connect(self._diary_path)
            rows = conn.execute("""
                SELECT ts, title, note FROM diary
                WHERE event_type='web_read'
                  AND (data IS NULL OR data NOT LIKE '%"pending": 1%')
                ORDER BY ts DESC LIMIT 5
            """).fetchall()
            conn.close()
            for ts, title, note in rows:
                topic = "diary"
                summary = note or ""
                if summary.startswith("["):
                    # Extrahuj topic ze závorky
                    import re
                    m = re.match(r"\[(\w+)\]\s*(.*)", summary)
                    if m:
                        topic, summary = m.group(1), m.group(2)
                self._recent.append(ReadResult(
                    source="diary", title=title, url="",
                    raw_text="", summary=summary,
                    topic=topic, fetched_at=ts,
                ))
            if self._recent:
                _log.info("Loaded %d recent reads from diary", len(self._recent))
        except Exception as e:
            _log.debug("Load from diary: %s", e)