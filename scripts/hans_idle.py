"""
Hans Idle Mode
Když nikdo není doma, Hans má vlastní program dne.
- Vybere náhodný film z Kodi a přečte jeho metadata
- Zapisuje deník do SQLite
- Večer předá shrnutí velkému LLM modelu

Spouští se automaticky z display_controller_picam.py.
"""
import sqlite3
import threading
import time
import random
import logging
from pathlib import Path
from datetime import datetime

_log = logging.getLogger("hans_idle")


class HansIdle:

    def __init__(self, config: dict, kodi_client, openwebui_chat=None):
        self.config        = config
        self._closing_goal = False  # GOAL_CLOSE_2D6_V1 — guard proti dvojimu uzavreni
        self.kodi          = kodi_client
        self.chat          = openwebui_chat
        self._stop         = threading.Event()
        self._lock         = threading.Lock()

        cfg = config.get("hans_idle", {})

        db_path = cfg.get("diary_db", "data/hans_diary.db")
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._init_db()

        self._last_seen    = time.time()   # čas poslední detekce osoby
        self._idle_since   = None          # kdy začal idle mode
        self._idle_active  = False
        self._hans_dialog  = None          # nastavuje display_controller
        self._movies_cache = []            # cache filmů z Kodi
        self._present_names  = []          # HANS_PROACTIVE_V1 — kdo je přítomen
        self._present_since  = 0.0
        # ATTENTION_PRESENCE_RESET_V1 — mezera bez detekce, po které se návrat
        # bere jako nový příchod (jinak arrival clock visí až do idle = 30 min).
        self._absence_gap_s  = float(self.config.get('hans_idle', {})
                                     .get('presence_absence_gap_s', 90))
        self._proactive      = None        # lazy ProactiveEngine
        # KODI_FILM_SUGGEST_V1 — proaktivní návrh filmu
        self._kodi_idle_since   = 0.0      # kdy přestalo hrát (0 = hraje/neznámo)
        self._last_film_check   = 0.0      # throttle Kodi pollu
        self._last_film_suggest = 0.0
        self._film_suggest_day  = ""
        self._film_suggest_cnt  = 0
        self._pending_film      = None     # (movieid, title, person, ts)

        from scripts.hans_curiosity import HansCuriosity
        self._curiosity = HansCuriosity(
            config        = config,
            diary_db_path = cfg.get("diary_db", "data/hans_diary.db"),
            diary_writer  = self._log_entry,  # DIARY_WRITER_PROPAGATE_IDLE_V2
        )

        from scripts.hans_body import HansBody
        self._body = HansBody(
            config        = config,
            diary_db_path = cfg.get("diary_db", "data/hans_diary.db"),
        )
        def _body_mood_cb(mood, intensity, reason):
            if hasattr(self, '_mood'):
                self._mood._shift(mood, intensity, reason)
        self._body.on_mood_change = _body_mood_cb

        from scripts.hans_mood import HansMood
        self._mood = HansMood(config, diary_db=self._db)
        # HANS_MORNING_HEALTH_V1 — ranní sebe-kontrola zdraví
        self._morning_health = None          # nález pro greeting/chat surfacing
        self._prev_routine_sleeping = None   # edge-detekce probuzení
        self._last_health_check_date = None  # idempotence 1×/den

        from scripts.hans_introspection import HansIntrospection
        from scripts.hans_routine import HansRoutine
        from scripts.kolac_cases import KolacCases
        from scripts.hans_library import HansLibrary
        _diary_db = config.get('hans_idle', {}).get(
            'diary_db', 'data/hans_diary.db')

        # Synthesis — Hans generuje vlastní názory na věci
        try:
            from scripts.hans_synthesis import HansSynthesis
            self._synthesis = HansSynthesis(config)
        except Exception as _e:
            _log.warning('HansSynthesis init failed: %s', _e)
            self._synthesis = None

        # Knowledge — upload syntéz do OpenWebUI RAG kolekcí
        try:
            from scripts.hans_knowledge import HansKnowledge
            self._knowledge = HansKnowledge(config)
            if self._knowledge.enabled:
                _log.info('HansKnowledge: aktivní')
        except Exception as _e:
            _log.warning('HansKnowledge init failed: %s', _e)
            self._knowledge = None

        # SynthesisHooks — auto-reflexe na vybrané zápisy do deníku
        try:
            from scripts.hans_synthesis import HansSynthesisHooks  # SYNTHESIS_MERGED_V1
            self._synthesis_hooks = HansSynthesisHooks(
                synthesis=self._synthesis,
                knowledge=self._knowledge,
                diary_writer=self._log_entry,
                config=config,  # G5B_DETECT_CONTRADICTION_V1 — pro karty
            )
            self._synthesis_hooks.start()
        except Exception as _e:
            _log.warning('HansSynthesisHooks init failed: %s', _e)
            self._synthesis_hooks = None

        # Denní rytmus + případy + knihovna
        # (routine dostává synthesis+knowledge pro večerní reflexi)
        self._routine = HansRoutine(
            config, _diary_db,
            synthesis=self._synthesis,
            knowledge=self._knowledge,
        )
        self._cases   = KolacCases(config, _diary_db)

        # HANS_GOALS_STRUCTURE_V1 — Hansovy úkolové cíle (fáze 2b)
        try:
            from scripts.hans_goals import HansGoals
            self._goals = HansGoals(config, _diary_db)
        except Exception as _e:
            _log.warning('HansGoals init failed: %s', _e)
            self._goals = None

        # HANS_DISTILLATION_V1 — fáze 2a (noční LLM destilace záseku)
        try:
            from scripts.hans_distillation import HansDistillation
            self._distillation = HansDistillation(
                config, _diary_db,
                goals=self._goals,
                knowledge=self._knowledge,
            )
            if hasattr(self._routine, 'set_distillation'):
                self._routine.set_distillation(self._distillation)
                _log.info('HansDistillation wired do routine')
        except Exception as _e:
            _log.warning('HansDistillation init failed: %s', _e)
            self._distillation = None

        # EBOOK_IMPORT_V1 — naskenuj nahrané ebooky (data/user_books/) PŘED knihovnou,
        # ať je vidí v custom_books (nízká priorita, tag user_upload). Deferral-safe.
        try:
            from scripts.ebook_import import import_user_books
            _added = import_user_books(config)
            if _added:
                _log.info('Nahrané ebooky zařazeny ke čtení: %s', ', '.join(_added))
        except Exception as _e:
            _log.warning('ebook_import selhal: %s', _e)

        self._library = HansLibrary(config, _diary_db, diary_writer=self._log_entry)

        # RELATIONSHIPS_IN_IDLE_V1 — čtecí instance pro _activity_relationship (krok 2)
        # Izolovaná instance (B): druhá vedle té v display_controlleru, obě jen čtou.
        # Pozor: _relationships není drátované zvenčí, idle si drží svoji.
        try:
            from scripts.hans_relationships import Relationships
            self._relationships = Relationships(config)
            _log.info('Relationships v idle: aktivní (%d aktivních karet)',
                      len(self._relationships.all_cards()))
        except Exception as _e:
            _log.warning('Relationships init v idle failed: %s', _e)
            self._relationships = None

        # Když Koláč otevře/uzavře případ, posune to Hansovu náladu
        def _case_mood_cb(mood: str, intensity: float, reason: str):
            if hasattr(self, '_mood'):
                self._mood._shift(mood, intensity, reason)
        self._cases.on_mood_shift = _case_mood_cb

        self._introspection = HansIntrospection(
            config        = config,
            diary_db_path = cfg.get("diary_db", "data/hans_diary.db"),
        )

        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        _log.info("HansIdle started — idle_timeout=%.0fs", self.idle_timeout)

    # ── DB ────────────────────────────────────────────────────────────────────

    def _init_db(self):
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS diary (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                ts         REAL NOT NULL,
                event_type TEXT NOT NULL,
                title      TEXT,
                data       TEXT,
                note       TEXT
            )""")
        self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_diary_ts ON diary(ts)")
        self._db.commit()

    def _morning_health_check(self):
        """HANS_MORNING_HEALTH_V1 — po probuzení (a po deferred catchupu)
        projdi noční logy. Reálné chyby → nálada 'worried' + nález uložený
        pro greeting/chat surfacing + deníkový záznam. 1×/den."""
        from datetime import datetime as _dt
        today = _dt.now().strftime('%Y-%m-%d')
        if self._last_health_check_date == today:
            return
        self._last_health_check_date = today
        if not self.config.get('morning_health', {}).get('enabled', True):
            return
        try:
            from scripts import hans_health_check as _hc
        except Exception as _e:
            _log.warning('morning_health: import selhal: %s', _e)
            return
        import time as _t
        since = (getattr(self._routine, '_sleep_started_ts', None)
                 or (_t.time() - 12 * 3600))
        try:
            res = _hc.scan_overnight_errors(since)
        except Exception as _e:
            _log.warning('morning_health: scan selhal: %s', _e)
            return
        n = res.get('count', 0)
        _log.info('morning_health: %d reálných chyb, %d benigních (okno od %s)',
                  n, res.get('benign', 0),
                  _dt.fromtimestamp(since).strftime('%H:%M'))
        if n <= 0:
            self._morning_health = None
            return
        summary = _hc.summary_sentence(res)
        # Nálada → worried (groundovaná ve vlastním provozním zdraví)
        try:
            self._mood._shift('worried', _hc.intensity_for(n),
                              'ranní kontrola: chyby v nočních logech')
        except Exception as _e:
            _log.debug('morning_health: mood shift failed: %s', _e)
        # Nález pro surfacing v pozdravu/chatu (přežije do příchodu osoby)
        self._morning_health = {
            'date': today, 'count': n,
            'modules': res.get('modules', {}),
            'summary': summary,
        }
        # Deník = úložiště + grounding nálady (ne hlasitý report)
        try:
            self._log_entry('morning_health', summary,
                            data=str(res.get('modules', {})),
                            note='; '.join(res.get('samples', []))[:500])
        except Exception as _e:
            _log.debug('morning_health: diary write failed: %s', _e)

    def _log_entry(self, event_type: str, title: str = "",
                   data: str = "", note: str = ""):
        with self._lock:
            self._db.execute(
                "INSERT INTO diary (ts, event_type, title, data, note) "
                "VALUES (?,?,?,?,?)",
                (time.time(), event_type, title, data, note))
            self._db.commit()
        _log.info("[Diary] %s — %s", event_type, title or note)

    # ── Volá hlavní smyčka ────────────────────────────────────────────────────
        # Auto-enqueue do synthesis hooks (asynchronně)
        if getattr(self, '_synthesis_hooks', None):
            try:
                self._synthesis_hooks.enqueue(event_type, title, note)
            except Exception:
                pass
    def person_seen(self, names: list[str]):
        """Zavolej když kamera vidí osobu."""
        known = [n for n in names if n not in ("Unknown", "...", "?", "")]
        if known:
            # HANS_PROACTIVE_V1 — sleduj kdo je přítomen (empty→present = nové usazení)
            # ATTENTION_PRESENCE_RESET_V1 — resetuj arrival clock i po reálné
            # nepřítomnosti (mezera od posledního spatření > _absence_gap_s),
            # ne jen při prázdném seznamu. Jinak počítadlo „vidím N min" běží
            # dál i po návratu, protože presence visí až do idle (30 min).
            if (not self._present_names
                    or (time.time() - self._last_seen) > self._absence_gap_s):
                self._present_since = time.time()
            self._present_names = known
            # Mood: příchod osoby
            if hasattr(self, '_mood'):
                for name in known:
                    self._mood.person_arrived(name)
            self._last_seen = time.time()
            # WOL_ON_PRESENCE_V1 — osoba přišla → probuď PC (throttle uvnitř)
            if hasattr(self, '_routine') and hasattr(self._routine, 'wake_pc_on_presence'):
                try:
                    self._routine.wake_pc_on_presence()
                except Exception:
                    pass
            if self._idle_active:
                self._end_idle()
            # ATTENTION_PUBLISH_V1 — rychlý refresh pozornosti při detekci
            self._publish_attention()

    # ── HANS_EVENT_API_V1 — public event API ──────────────────────────────────
    # Tyto metody jsou veřejný kontrakt pro display_controller a jiné moduly.
    # Volající se NEMÁ starat o interní strukturu hans_idle (curiosity, mood, ...).
    # Pokud některý subsystém není inicializovaný, metoda tiše projde.

    def event_objects_seen(self, class_names: list):
        """Hans vidí objekty v záběru (list).

        Mood reaguje na celou scénu (všechny objekty),
        Curiosity reaguje jen na první objekt (zajímavý fakt o jedné věci).
        """
        if not class_names:
            return
        # Curiosity — jen první objekt, ať se Hans nepřehlcuje
        if hasattr(self, '_curiosity') and self._curiosity is not None:
            try:
                first = class_names[0]
                if first:
                    self._curiosity.trigger_object(first)
            except Exception as _e:
                _log.error("event_objects_seen.curiosity: %s", _e)
        # Mood — celá scéna
        if hasattr(self, '_mood') and self._mood is not None:
            try:
                self._mood.update_objects(class_names)
            except Exception as _e:
                _log.error("event_objects_seen.mood: %s", _e)

    def event_unknown_person(self):
        """Hans viděl neznámou tvář. Posune mood k 'worried'."""
        if hasattr(self, '_mood') and self._mood is not None:
            try:
                self._mood.update_objects(["unknown_person"])
            except Exception as _e:
                _log.error("event_unknown_person.mood: %s", _e)

    def event_weather_changed(self, code):
        """Update počasí — code je int podle wmo conv (viz HansMood.update_weather)."""
        if hasattr(self, '_mood') and self._mood is not None:
            try:
                self._mood.update_weather(code)
            except Exception as _e:
                _log.error("event_weather_changed: %s", _e)

    def event_observation_context(self, context: str,
                                  source_type: str = "observation"):
        """Hans má kontext (popis scény) → curiosity může vygenerovat otázku."""
        if not context:
            return
        if hasattr(self, '_curiosity') and self._curiosity is not None:
            try:
                self._curiosity.trigger_question(context, source_type=source_type)
            except Exception as _e:
                _log.error("event_observation_context: %s", _e)

    def event_face_enroll(self, name: str, session: str, samples_added: int):
        """Zaznamenat úspěšný quick augment do deníku.

        Voláno po confirmed augmentu (samples_added > 0).
        Diary entry vede session info pro tracking historie světelných podmínek.
        """
        if not name or samples_added <= 0:
            return
        try:
            import json as _json
            self._log_entry(
                "face_enroll",
                title=name,
                data=_json.dumps({
                    "session": session,
                    "samples_added": samples_added,
                }),
                note=f"Quick augment {name} ({session}): +{samples_added} vzorků")
        except Exception as _e:
            _log.error("event_face_enroll: %s", _e)

    # ── Hlavní smyčka ─────────────────────────────────────────────────────────

    def _loop(self):
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as e:
                _log.error("Tick error: %s", e)
            self._stop.wait(self.check_interval)  # dynamicky z config

    @property
    def idle_timeout(self):
        return float(self.config.get("hans_idle", {}).get("idle_timeout_min", 30)) * 60

    @property
    def check_interval(self):
        return float(self.config.get("hans_idle", {}).get("check_interval_s", 60))

    def _tick(self):
        # Půlnoční reset nálady
        from datetime import datetime as _dt
        _now = _dt.now()
        # Denní rytmus — detekce fáze dne
        if hasattr(self, '_routine'):
            self._routine.tick()
            # HANS_MORNING_HEALTH_V1 — ranní sebe-kontrola zdraví.
            # Edge-detekce probuzení (_sleeping True→False). Catchup destilace
            # běží uvnitř téhož routine.tick() PŘED tímto → scan je až po něm.
            try:
                _slp = getattr(self._routine, '_sleeping', False)
                _prev = getattr(self, '_prev_routine_sleeping', None)
                if _prev is True and _slp is False:
                    self._morning_health_check()
                self._prev_routine_sleeping = _slp
            except Exception as _mhe:
                _log.debug('morning_health edge check failed: %s', _mhe)

        # Mood — periodický recompute (klouzavý průměr + hystereze)
        if hasattr(self, '_mood'):
            self._mood.tick()

        if _now.hour == 0 and _now.minute < 2:
            if hasattr(self, '_mood'):
                self._mood.midnight_reset()
        elapsed = time.time() - self._last_seen
        if not self._idle_active and elapsed >= self.idle_timeout:
            self._begin_idle()
        elif self._idle_active:
            self._idle_activity()
            # Periodická introspekce během idle
            if hasattr(self, '_introspection'):
                self._introspection.set_idle(True, elapsed / 3600)
            # Mood update při idle
            if hasattr(self, '_mood'):
                self._mood.nobody_home(elapsed / 3600)

            # Spontánní poznámky — Hans mluví sám pro sebe
            if hasattr(self, '_mood') and hasattr(self, '_hans_dialog'):
                if self._mood.should_speak_spontaneously():
                    text = None
                    # ~35 % šance: pokud běží případ, mluví Koláč/Hans o něm
                    if (hasattr(self, '_cases')
                            and self._cases.get_active_case()
                            and random.random() < 0.35):
                        try:
                            text = self._cases.get_dialog_directive()
                        except Exception:
                            text = None
                    if not text:
                        text = self._mood.get_spontaneous_text()
                    if text:
                        self._log_entry("spontaneous", note=text)
                        _tts = getattr(
                            getattr(self, '_hans_dialog', None),
                            'tts', None)
                        if _tts and getattr(_tts, 'enabled', False):
                            _tts.speak(text)

        # HANS_PROACTIVE_V1 — proaktivní iniciace když je osoba přítomna (ne idle)
        if not self._idle_active:
            try:
                self._maybe_proactive()
            except Exception as e:
                _log.error('Proactive tick error: %s', e)
            # KODI_FILM_SUGGEST_V1 — návrh filmu + detekce přijetí
            try:
                self._check_film_accepted()
                self._maybe_suggest_film()
            except Exception as e:
                _log.error('Film suggest tick error: %s', e)

        # ATTENTION_PUBLISH_V1 — publikuj kontext pro dual-eye pozornost
        self._publish_attention()

    def _publish_attention(self):
        """ATTENTION_PUBLISH_V1 — publikuj bohatý kontext pozornosti pro
        dual_display_daemon (pravý displej). Cheap: paměťové atributy + 1
        levný DB read (titul knihy). Atomický JSON zápis. Renderer priorita:
        proactive > kolac > person > activity > mood."""
        try:
            import json as _json
            import os
            now = time.time()
            ctx = {}
            if hasattr(self, '_mood') and self._mood is not None:
                ctx['mood'] = self._mood.mood or 'content'
            ctx['clock'] = time.strftime('%H:%M')
            if hasattr(self, '_routine'):
                try:
                    ctx['phase'] = self._routine.phase_label
                except Exception:
                    ctx['phase'] = ''
            # proaktivní podnět — krátké okno po vyslovení
            pro = getattr(self, '_last_proactive', None)
            if pro and (now - pro[1]) < 25:
                ctx['proactive'] = pro[0]
            # Koláč dialog
            kd = getattr(self, '_hans_dialog', None)
            if kd is not None and getattr(kd, '_kolac_speaking', False):
                ctx['kolac'] = True
                ctx['kolac_speaking'] = True
            # osoba — jen přítomná, nedávno viděná, mimo idle
            names = getattr(self, '_present_names', None)
            if names and (now - self._last_seen) < 90 and not self._idle_active:
                ctx['person'] = names[0]
                ctx['person_min'] = int((now - getattr(self, '_present_since', now)) / 60)
            # aktivita (reading/watching; 'looking' baseline neukazujeme)
            try:
                act = self.current_activity_label()
            except Exception:
                act = None
            if act and act != 'looking':
                ctx['activity'] = act
                if act == 'reading' and hasattr(self, '_library'):
                    try:
                        bk = self._library.get_current_book()
                        if bk and bk.get('title'):
                            ctx['activity_label'] = bk['title']
                    except Exception:
                        pass
            path = 'data/avatar/attention_context.json'
            os.makedirs('data/avatar', exist_ok=True)
            # ATTENTION_PUBLISH_TMP_RACE_V1 — _publish_attention volá víc threadů
            # (person_seen z recognition, _tick z idle loopu); sdílený *.tmp +
            # os.replace závodily → Errno 2. Unikátní tmp per thread.
            import threading as _th
            tmp = '%s.%d.tmp' % (path, _th.get_ident())
            with open(tmp, 'w', encoding='utf-8') as f:
                _json.dump(ctx, f, ensure_ascii=False)
            os.replace(tmp, path)
        except Exception as _e:
            _log.error('publish_attention: %s', _e)

    # ── Idle mode ─────────────────────────────────────────────────────────────

    def _proactive_engine(self):  # HANS_PROACTIVE_V1 — lazy singleton
        eng = getattr(self, '_proactive', None)
        if eng is not None:
            return eng
        try:
            from scripts.hans_proactive import ProactiveEngine
            _dbp = self.config.get('hans_idle', {}).get('diary_db', 'data/hans_diary.db')
            self._proactive = ProactiveEngine(self.config, _dbp)
        except Exception as e:
            _log.warning('ProactiveEngine init failed: %s', e)
            self._proactive = None
        return self._proactive

    def _routine_store(self):  # HANS_ROUTINE_PATTERNS_WIRING_V1 — lazy
        rs = getattr(self, "_routine_patterns", None)
        if rs is not None:
            return rs
        try:
            from scripts.hans_routine_patterns import RoutineStore
            _dbp = self.config.get("hans_idle", {}).get("diary_db", "data/hans_diary.db")
            self._routine_patterns = RoutineStore(self.config, _dbp)
        except Exception as e:
            _log.warning("RoutineStore init failed: %s", e)
            self._routine_patterns = None
        return self._routine_patterns

    def _fcfg(self) -> dict:  # KODI_FILM_SUGGEST_V1
        return self.config.get("film_suggest", {}) or {}

    # FILM_PERSON_PREF_V1 — zájem (volný text) → filmový žánr (token dle
    # kodi_client._GENRE_CANON). Substring match.
    _INTEREST_GENRE_MAP = (
        (("star trek", "sci-fi", "scifi", "vesmír", "kosmo", "futur"), "scifi"),
        (("anime", "manga"), "animation"),
        (("histori", "hrad", "středověk", "antik", "památk"), "history"),
        (("detektiv", "krimi", "zločin", "vražd"), "crime"),
        (("horor",), "horror"),
        (("komedi", "humor", "vtip"), "comedy"),
        (("fantasy", "drak", "kouzl"), "fantasy"),
        (("western", "divoký západ"), "western"),
        (("literatura", "kniha", "román", "poezie"), "drama"),
        (("design", "řemeslo", "architekt", "umění", "umělec", "výtvarn"), "documentary"),
        (("akč", "akce", "bojov"), "action"),
        (("dobrodruž", "cestov"), "adventure"),
        (("thriller", "napětí"), "thriller"),
        (("hudb", "hudba"), "music"),
    )

    def _pinterest_store(self):  # FILM_PERSON_PREF_V1 — lazy
        s = getattr(self, "_pinterest", None)
        if s is not None:
            return s
        try:
            from scripts.hans_person_interests import PersonInterestStore
            _dbp = self.config.get("hans_idle", {}).get("diary_db", "data/hans_diary.db")
            self._pinterest = PersonInterestStore(self.config, _dbp)
        except Exception as e:
            _log.warning("PersonInterestStore init failed: %s", e)
            self._pinterest = None
        return self._pinterest

    def _person_pref_genres(self, person: str) -> list:
        """Žánry odvozené ze zájmů osoby (person_interests). [] když nic."""
        try:
            store = self._pinterest_store()
            ints = store.interests_for(person, limit=10) if store else []
        except Exception:
            return []
        out = []
        for it in ints:
            txt = (getattr(it, "interest", "") or "").lower()
            for keys, genre in self._INTEREST_GENRE_MAP:
                if genre not in out and any(k in txt for k in keys):
                    out.append(genre)
        return out

    def _maybe_suggest_film(self):  # KODI_FILM_SUGGEST_V1
        """Proaktivní návrh filmu přes Kodi dialog. Gate: nehraje ≥ idle_hours,
        osoba poblíž (≤10 min), obvyklý čas, throttle, TTS ticho."""
        cfg = self._fcfg()
        if not cfg.get("enabled", True) or self.kodi is None:
            return
        now = time.time()
        # poll Kodi stavu jen á 60s (šetři JSON-RPC)
        if (now - self._last_film_check) < 60:
            return
        self._last_film_check = now
        # osoba poblíž?
        names = [n for n in (getattr(self, "_present_names", None) or [])
                 if n and n not in ("Unknown", "?", "")]
        recency = float(cfg.get("present_recency_s", 600))
        if not names or (now - self._last_seen) > recency:
            return
        # nic nehraje + sleduj jak dlouho
        try:
            playing = self.kodi.is_playing()
        except Exception:
            return
        if playing:
            self._kodi_idle_since = 0.0
            return
        if self._kodi_idle_since == 0.0:
            self._kodi_idle_since = now
            return
        idle_need = float(cfg.get("idle_hours", 1.0)) * 3600.0
        if (now - self._kodi_idle_since) < idle_need:
            return
        # obvyklý čas koukání (rutina 2b; None/True propustí)
        try:
            rs = self._routine_store()
            if rs is not None and rs.is_typical_time(names[0]) is False:
                return
        except Exception:
            pass
        # throttle
        today = time.strftime("%Y-%m-%d")
        if today != self._film_suggest_day:
            self._film_suggest_day = today
            self._film_suggest_cnt = 0
        if self._film_suggest_cnt >= int(cfg.get("max_per_day", 3)):
            return
        if (now - self._last_film_suggest) < float(cfg.get("cooldown_h", 3)) * 3600.0:
            return
        # TTS nemluví
        _tts = getattr(getattr(self, "_hans_dialog", None), "tts", None)
        _isp = getattr(_tts, "is_speaking", None) if _tts else None
        if callable(_isp) and _isp():
            return
        # vyber film s preferencí žánrů
        try:
            # FILM_PERSON_PREF_V1 — zájmy přítomné osoby + domácnostní historie
            genres = self._person_pref_genres(names[0]) + self.kodi.favorite_genres()
            movie = self.kodi.pick_suggestion(prefer_genres=genres)
        except Exception as e:
            _log.warning("pick_suggestion selhal: %s", e)
            return
        if not movie or movie.get("movieid") is None:
            return
        title = movie.get("title", "film")
        person = names[0]
        countdown = int(cfg.get("countdown_s", 30))
        spoken = u"%s, co takhle se podívat na film %s?" % (person.capitalize(), title)
        if _tts and getattr(_tts, "enabled", False):
            try:
                _tts.speak(spoken)
            except Exception:
                pass
        line = u"Co takhle „%s\"? Pustím za %d s." % (title, countdown)
        ok = self.kodi.suggest_movie(movie, countdown=countdown, line=line)
        if not ok:
            return
        self._pending_film = (movie.get("movieid"), title, person, now)
        self._last_film_suggest = now
        self._film_suggest_cnt += 1
        self._log_entry("film_suggestion", title=person, note=u"Návrh: %s" % title)
        _log.info("[FilmSuggest] %s ← %s", person, title)

    def _check_film_accepted(self):  # KODI_FILM_SUGGEST_V1
        """Sleduje, zda se navržený film spustil → mood↑ + deník + karta osoby."""
        pend = self._pending_film
        if not pend or self.kodi is None:
            return
        movieid, title, person, ts = pend
        now = time.time()
        window = float(self._fcfg().get("accept_window_min", 5)) * 60.0
        if (now - ts) > window:
            self._pending_film = None  # okno vypršelo, tiše nic
            return
        try:
            np = self.kodi.get_now_playing()
        except Exception:
            return
        if not np:
            return
        same = (np.get("id") == movieid) or (
            (np.get("title") or np.get("label") or "").strip().lower()
            == (title or "").strip().lower())
        if not same:
            return
        self._pending_film = None
        # mood ↑
        try:
            if hasattr(self, "_mood") and self._mood is not None:
                self._mood._shift("playful", 0.7,
                                  u"%s přijal můj návrh na film" % person)
        except Exception:
            pass
        # deník + karta osoby (film_suggestion_accepted → _MENTION_EVENT_TYPES)
        note = u"%s přijal/a můj návrh na film „%s\"." % (person.capitalize(), title)
        self._log_entry("film_suggestion_accepted", title=person, note=note)
        _log.info("[FilmSuggest] PŘIJATO %s ← %s", person, title)

    def _maybe_proactive(self):  # HANS_PROACTIVE_V1
        """Osoba usazená a přítomná → zeptej se enginu na proaktivní
        příležitost (dozrálá nitka) a vyslov ji. Mantinely: usazení
        (settle_s), TTS nemluví, recency (>90s = odešla); throttle uvnitř
        enginu (cooldown_h, max_per_day)."""
        names = getattr(self, '_present_names', None)
        if not names:
            return
        now = time.time()
        settle_s = float(self.config.get('proactive', {}).get('settle_s', 600))
        if (now - getattr(self, '_present_since', now)) < settle_s:
            return  # ještě není usazená dost dlouho
        if (now - self._last_seen) > 90:
            return  # poslední detekce >90s → nejspíš odešla
        _tts = getattr(getattr(self, '_hans_dialog', None), 'tts', None)
        if not (_tts and getattr(_tts, 'enabled', False)):
            return
        _isp = getattr(_tts, 'is_speaking', None)
        if callable(_isp) and _isp():
            return  # nepřekrývej probíhající řeč
        # HANS_ROUTINE_PATTERNS_WIRING_V1 — timing: v netypický čas
        # přítomnosti proaktivitu potlač (None/True = propustit).
        rs = self._routine_store()
        if rs is not None:
            try:
                if rs.is_typical_time(names[0]) is False:
                    return
            except Exception as _re:
                _log.warning("routine timing gate error: %s", _re)
        eng = self._proactive_engine()
        if eng is None:
            return
        opp = eng.next_opportunity(names)
        if not opp:
            return
        person, utterance, thread_id = opp
        self._log_entry('proactive', title=person, note=utterance)
        self._last_proactive = (utterance, time.time())  # ATTENTION_PUBLISH_V1
        _tts.speak(utterance)
        _log.info('[Proactive] %s ← nitka #%s: %s', person, thread_id, utterance)
        # PROACTIVE_THREAD_POPUP_V1 — otevři popup, ať má osoba kam odpovědět
        _chat = getattr(self, 'chat', None)
        if _chat is not None and hasattr(_chat, 'open_thread_popup'):
            try:
                _chat.open_thread_popup(person, utterance)
            except Exception as _pe:
                _log.warning('proactive popup failed: %s', _pe)
        self._publish_attention()

    def _begin_idle(self):
        self._idle_active = True
        self._present_names = []           # HANS_PROACTIVE_V1 — nikdo doma
        self._idle_since  = time.time()
        if self._hans_dialog:
            self._hans_dialog._idle_active = True
        _log.info("Entering idle mode")
        from scripts.hans_persona import persona_name as _pn  # PERSONA_NAME_CONFIGURABLE_V1
        self._log_entry("idle_start", note=f"Nikdo není doma, {_pn(self.config)} má volno")
        self._load_movies()
        # Spusť introspekci při začátku idle
        if hasattr(self, '_introspection'):
            self._introspection.trigger("nikdo není doma")

    def _end_idle(self):
        self._idle_active = False
        if self._hans_dialog:
            self._hans_dialog._idle_active = False
        duration = int((time.time() - self._idle_since) / 60)
        _log.info("Leaving idle mode after %d min", duration)
        self._log_entry("idle_end",
                        note=f"Volný čas skončil po {duration} minutách")
        # Introspekce při návratu — Hans reflektuje co bylo
        if hasattr(self, '_introspection'):
            self._introspection.trigger(
                f"obyvatelé se vrátili po {duration} minutách")
        if hasattr(self, '_mood'):
            self._mood.person_arrived("obyvatelé")

    def _goal_llm_caller(self, system, user):  # GOAL_CLOSE_2D6_V1
        """LLM pro auto-dilo — ekvivalent _g5c_llm bez chat handleru."""
        try:
            from scripts.ollama_client import ollama_chat
            cfg = self.config or {}
            ow = cfg.get("openwebui_chat", {}) or {}
            model = (cfg.get("models", {}).get("utility")
                     or cfg.get("models", {}).get("dialog")
                     or "hans-czech:latest")
            url = ow.get("base_url", "http://127.0.0.1:11434")
            out = ollama_chat(
                model,
                [{"role": "system", "content": system},
                 {"role": "user", "content": user}],
                ollama_url=url,
                options={"num_predict": 900, "temperature": 0.0},
            )
            return (out or "").strip()
        except Exception as e:
            _log.error("goal work LLM selhal: %s", e)
            return ""

    def _do_close_goal(self, g):  # GOAL_CLOSE_2D6_V1 (2d + 6)
        """Vytvori dilo z tematu cile a uzavre cil podle vysledku."""
        from scripts.hans_goals import STATUS_ABANDONED, STATUS_COMPLETED
        try:
            out = self._create_work(g.topic, self._goal_llm_caller)
        except Exception as e:
            out = {'ok': False, 'error': 'work vyjimka: %s' % e}
        _exp = self._goals.get_expected(g.id)  # GOAL_EXPECTED_VS_ACTUAL_V1
        if out.get('ok'):
            self._goals.close_goal(
                g.id, STATUS_COMPLETED,
                work_path=out.get('path'),
                outcome_reason='dokončen-s-dílem')  # 6
            _log.info('Cíl [%s] dotažen dílem: %s', g.id, out.get('path'))
            try:
                _note = 'Cíl dotažen dílem (%d slov).' % out.get('words', 0)
                if _exp:  # GOAL_EXPECTED_VS_ACTUAL_V1
                    _note += '\nOčekával jsem: %s\nVýsledek: dokončeno dílem.' % _exp
                self._log_entry('goal_completed', title=g.topic, note=_note)
            except Exception:
                pass
            # HANS_WORK_COMPLETION_V1 (B) — vlastní dokončené dílo smí formovat
            # postoje. Ollama je v tuto chvíli prokazatelně UP (esej právě vznikla
            # LLM voláním), takže inline reflexe bez deferralu. Gate try/except.
            try:
                _refl = getattr(getattr(self, '_routine', None), '_reflection', None)
                _essay = out.get('text', '')
                if _refl is not None and hasattr(_refl, 'reflect_on_work') and _essay:
                    _refl.reflect_on_work(g.topic, _essay)
            except Exception as _we:
                _log.warning('work completion reflexe selhala (cíl OK): %s', _we)
        else:
            self._goals.close_goal(
                g.id, STATUS_ABANDONED,
                outcome_reason='vypršel-bez-díla: %s' % out.get('error', ''))  # 6
            _log.info('Cíl [%s] opuštěn (dílo selhalo): %s',
                      g.id, out.get('error'))
            try:  # GOAL_EXPECTED_VS_ACTUAL_V1 — i abandoned do deníku, s porovnáním
                _note = 'Cíl opuštěn bez díla.'
                if _exp:
                    _note += '\nOčekával jsem: %s\nVýsledek: nedotaženo.' % _exp
                self._log_entry('goal_abandoned', title=g.topic, note=_note)
            except Exception:
                pass

    def _maybe_close_goal(self):  # GOAL_CLOSE_2D6_V1 (2d)
        """Z _decide_activity: cil na konci targetu -> dilo + uzaver.
        Bezi v threadu (dilo je pomale), guard proti dvojimu spusteni."""
        if getattr(self, '_closing_goal', False):
            return
        try:
            g = self._goals.get_active_goal() if self._goals else None
        except Exception:
            return
        if g is None or not g.should_complete():
            return
        self._closing_goal = True
        _log.info('Cíl [%s] dosáhl targetu → spouštím dotažení dílem', g.id)
        import threading

        def _wrap():
            try:
                self._do_close_goal(g)
            finally:
                self._closing_goal = False
        threading.Thread(target=_wrap, daemon=True).start()

    def _decide_activity(self, dry_run=False):  # OODA_DECIDE_ACTIVITY_V1 + OODA_DRYRUN_V1
        """OODA: Observe kontext → Orient+Decide vážené skóre → vrátí akci.
        Konzervativní: základ = současné šance, kontext jen lehce přiklání.
        Při chybě fallback na původní pětici. Mění JEN výběr, NE akce."""
        import random as _r
        # GOAL_CLOSE_2D6_V1 — dotáhni cíl na konci targetu (dílo + uzávěr)
        # OODA_DRYRUN_V1 — při diagnostice (/ooda) NEkonat, jen rozhodovat
        if not dry_run:
            try:
                self._maybe_close_goal()
            except Exception as _e:
                _log.debug('maybe_close_goal fallback: %s', _e)
        # ── Observe: posbírej kontext, vše s guardy (chybí → neutrální) ──
        has_case = False
        try:
            if hasattr(self, '_cases') and self._cases is not None:
                has_case = self._cases.get_active_case() is not None
        except Exception:
            has_case = False
        mood = ''
        try:
            if hasattr(self, '_mood') and self._mood is not None:
                mood = (self._mood.mood or '')
        except Exception:
            mood = ''
        alone_h = 0.0
        try:
            alone_h = (time.time() - self._last_seen) / 3600.0
        except Exception:
            alone_h = 0.0
        # ── Orient+Decide: základní váhy = současné šance + book(1) ──────
        # READING_PRIORITY_V1 — knihy = hlavní růstový motor osobnosti (completion
        # reflexe → stances → tendence → Severka). Dřív book=1.0 < movie=2.0 (film
        # NEgrounduje osobnost) = nelogické → čtení upřednostněno.
        w = {
            'movie':        1.5,   # bylo 2.0 — míň echo-chamber filleru
            'thought':      1.0,
            'read':         1.5,   # bylo 1.0 — čtenářská zvídavost podporuje knihu
            'case':         1.0,
            'book':         3.0,   # bylo 1.0 — přečte kapitolu = dominantní idle aktivita
            'relationship': 1.0,  # ACTIVITY_RELATIONSHIP_V1
        }
        # OODA_CASE_AGE_WEIGHT_V1 — bonus case roste se stářím případu (+2 čerstvý → +6 přetažený)
        # Princip: "dotáhni, co jsi začal" sílí, jak případ stárne.
        if has_case:
            _case_bonus = 3.0  # fallback (kdyby se nepodařilo načíst stáří)
            try:
                _ac = self._cases.get_active_case()
                if _ac is not None:
                    _age = _ac.age_days()          # min 1 (viz Case.age_days)
                    _tgt = max(1, _ac.target_days)  # ochrana proti dělení 0
                    _ratio = min(1.0, max(0.0, (_age - 1) / _tgt))
                    _case_bonus = 2.0 + _ratio * 4.0  # 2.0 .. 6.0
            except Exception as _e:
                _log.debug('case age bonus fallback: %s', _e)
                _case_bonus = 3.0
            w['case'] += _case_bonus
        # GOAL_FOCUS_2C_V1 — aktivní cíl přiklání k soustředěnému čtení
        try:
            if getattr(self, '_goals', None) is not None:
                _ag = self._goals.get_active_goal()
                if _ag is not None:
                    w['read'] += 2.5
        except Exception as _e:
            _log.debug('goal OODA bonus fallback: %s', _e)
        # Pravidlo 2: dlouho nikdo doma → víc solitérních, míň filmu
        if alone_h >= 4.0:
            w['thought'] += 1.0
            w['read']    += 1.0
            w['book']    += 1.0
            w['movie']    = max(0.5, w['movie'] - 1.0)
        # Pravidlo 3: nálada jen lehce (zatím opatrně, doladit z logů)
        if mood in ('smutny', 'unaveny', 'klidny'):
            w['thought'] += 0.5
            w['book']    += 0.5
        # ── mapování názvů na reálné metody (book přidán po ověření _library) ──
        funcs = {
            'movie':        self._activity_movie,
            'thought':      self._activity_thought,
            'read':         self._activity_read,
            'case':         self._activity_case,
            'book':         self._activity_book,
            'relationship': self._activity_relationship,  # ACTIVITY_RELATIONSHIP_V1
        }
        # ACTIVITY_RELATIONSHIP_V1 — dynamicky stáhnout 'relationship' z výběru,
        # když nikdo není >= 1 den pryč (jinak by aktivita padla do tichého return)
        try:
            if getattr(self, '_relationships', None) is not None:
                _has_old = False
                _now_ts = time.time()
                for _c in self._relationships.all_cards():
                    _ts = getattr(_c, 'last_seen_ts', None)
                    if _ts and (_now_ts - _ts) / 86400.0 >= 1.0:
                        _has_old = True
                        break
                if not _has_old:
                    w['relationship'] = 0.0
            else:
                w['relationship'] = 0.0
        except Exception:
            w['relationship'] = 0.0
        try:
            keys    = list(w.keys())
            weights = [w[k] for k in keys]
            chosen  = _r.choices(keys, weights=weights, k=1)[0]
            score_s = ' '.join('%s:%.0f' % (k, w[k]) for k in keys)
            _log.info('OODA skóre: %s → vybráno %s', score_s, chosen)
            # OODA_SCORE_ATTR_V1 — sklad pro /ooda diagnostiku (čte z paměti)
            self._last_ooda_score = '%s → vybráno %s' % (score_s, chosen)
            # AVATAR_ACTIVITY_V1 — breadcrumb pro avatar (jako _mood.mood)
            self._current_activity = chosen
            self._current_activity_ts = time.time()
            return funcs[chosen]
        except Exception as _e:
            # Fallback: původní chování (pětice), OODA nikdy nezablokuje akci
            _log.error('OODA decide selhalo (%s) → fallback random.choice', _e)
            return _r.choice([
                self._activity_movie,
                self._activity_movie,
                self._activity_thought,
                self._activity_read,
                self._activity_case,
            ])

    def current_activity_label(self):
        """AVATAR_ACTIVITY_V1 — co Hans zrovna „dělá", pro výběr avatara.
        CHEAP (žádná síť) — čte jen breadcrumb z _decide_activity. 'reading'
        (book/read), 'watching' (movie aktivita), jinak 'looking' (baseline mezi
        aktivitami). Čerstvost 10 min. Živý film řeší display přes kodi_monitor
        (perzistentní). Display fallbackuje na náladu, když avatar pro label chybí."""
        act = getattr(self, '_current_activity', None)
        ts  = getattr(self, '_current_activity_ts', 0)
        if act and (time.time() - ts) < 600:
            if act in ('book', 'read'):
                return 'reading'
            if act == 'movie':
                return 'watching'
        return 'looking'

    def _idle_activity(self):
        """Každých check_interval sekund Hans něco udělá."""
        # Každých 30 minut nová aktivita
        if not hasattr(self, '_last_activity'):
            self._last_activity = 0
        if time.time() - self._last_activity < 1800:
            return
        self._last_activity = time.time()

        # OODA_DECIDE_ACTIVITY_V1 — OODA: výběr přes vážené skóre místo random.choice
        activity = self._decide_activity()
        activity()

    # ── Aktivity ──────────────────────────────────────────────────────────────

    def _load_movies(self):
        """Načti knihovnu filmů z Kodi."""
        if self._movies_cache:
            return
        try:
            result = self.kodi._call("VideoLibrary.GetMovies", {
                "properties": ["title", "year", "genre", "plot",
                               "director", "rating"],
            })
            if result and "result" in result:
                self._movies_cache = result["result"].get("movies", [])
                _log.info("Loaded %d movies from Kodi", len(self._movies_cache))
        except Exception as e:
            _log.error("Failed to load movies: %s", e)

    def _activity_relationship(self):  # ACTIVITY_RELATIONSHIP_V1
        """Hans se zamyslí nad nejdéle neviděnou osobou (≥ 1 den).
        Šablona — pár variant. Když nikdo není 1+ den pryč, tichý return.
        LLM verze přijde později po ověření naživo."""
        if not getattr(self, '_relationships', None):
            return
        import random as _r
        try:
            cards = self._relationships.all_cards()
        except Exception as _e:
            _log.warning('relationship aktivita: all_cards selhalo: %s', _e)
            return
        # Vyfiltrovat karty s platným last_seen_ts a spočítat dny
        now = time.time()
        candidates = []
        for c in cards:
            ts = getattr(c, 'last_seen_ts', None)
            if not ts:
                continue
            days = (now - ts) / 86400.0
            if days >= 1.0:
                candidates.append((days, c))
        if not candidates:
            _log.debug('relationship aktivita: nikdo neviděn >= 1 den, přeskakuji')
            return
        # Nejstarší (nejdéle neviděný)
        candidates.sort(key=lambda x: x[0], reverse=True)
        days, card = candidates[0]
        name = getattr(card, 'display_name', '') or 'osobu'
        chrz = (getattr(card, 'characterization', '') or '').strip()
        # Slovní vyjádření doby
        d_int = int(days)
        if d_int <= 1:
            doba = 'celý den'
        elif d_int < 5:
            doba = '%d dny' % d_int
        else:
            doba = '%d dnů' % d_int
        # Šablona — 5 variant Hansova hlasu (komorník)
        templates = [
            '%s jsem neviděl už %s. Doufám, že je v pořádku.',
            '%s tu nebyl/a %s — přemýšlím, kde se asi nachází.',
            'Vzpomněl jsem si na to, že %s tu nebyl/a %s.',
            '%s mi schází — naposled tu byl/a před %s.',
            'Uvažoval jsem o tom, že %s tu nebyl/a %s.',
        ]
        tpl = _r.choice(templates)
        note = tpl % (name, doba)
        if chrz:
            # Lehce přidat kontext z characterization (jen krátký zlomek)
            note += ' (' + chrz[:120].strip() + ')'
        self._log_entry('person_recollection', title=name, note=note)
        _log.info('relationship aktivita: zápis o %s (%.1f dnů)', name, days)

    def _create_work(self, topic, llm_caller):  # WORK_REFACTOR_SHARED_V1
        """Vytvoří dílo (esej) z RAGu hans_cetba. Sdílené jádro /work + automatika.
        topic: téma. llm_caller: fn(system, user) -> str (destilace).
        Vrací dict {ok, error, words, path, rag}. Nikdy nevyhodí.
        Volá se z chatu (handler) i z idle (shim) — jedna destilace."""
        import os as _os
        import re as _re
        import time as _t
        from datetime import datetime as _dt
        out = {'ok': False, 'error': '', 'words': 0, 'path': '', 'rag': ''}
        topic = (topic or '').strip()
        if not topic:
            out['error'] = 'prázdné téma'; return out
        _kn = getattr(self, '_knowledge', None)
        if _kn is None or not getattr(_kn, 'enabled', False):
            out['error'] = 'HansKnowledge není aktivní'; return out
        # 1) Sběr z RAGu
        try:
            res = _kn.query('hans_cetba', topic, k=15)
        except Exception as _e:
            out['error'] = 'query selhalo: %s' % _e; return out
        if not getattr(res, 'found', False):
            out['error'] = 'k tématu nic v četbě nenalezeno'; return out
        chunks = getattr(res, 'chunks', []) or []
        material = (getattr(res, 'text', '') or '')[:5000]
        if not material.strip():
            material = chr(10).join(
                '- ' + str(c.get('text', ''))[:400]
                for c in chunks[:15] if isinstance(c, dict))[:5000]
        if not material.strip():
            out['error'] = 'prázdný materiál'; return out
        # GOAL_CONTINUITY_V1 — naváž na dřívější dílo na stejné téma (hans_dila)
        prior = ''
        try:
            pres = _kn.query('hans_dila', topic, k=2)
            if getattr(pres, 'found', False):
                prior = (getattr(pres, 'text', '') or '')[:2000].strip()
        except Exception as _pe:
            _log.debug('goal continuity prior fetch: %s', _pe)
            prior = ''
        # 2) Destilace přes předaný llm_caller
        # PERSONA_REFACTOR_10 — identita z persona_core, task beze změny
        from scripts.hans_persona import persona_core
        sys_prompt = (
            persona_core(self.config, with_address=False) + ' '
            'Z níže uvedených útržků, které sis nastudoval, '
            'napiš souvislou esej na téma \'%s\'. NE výčet, NE odrážky — '
            'plynulý text vlastními slovy, 400 až 600 slov. Pokud útržky '
            'něco neobsahují, NEVYMÝŠLEJ si fakta. Piš svým hlasem.'
        ) % topic
        if prior:  # GOAL_CONTINUITY_V1
            sys_prompt += (' Na toto téma už máš dřívější úvahu (uvedena '
                           'níže). NAVAŽ na ni a prohlub ji, případně ji '
                           'reviduj — ale NEopakuj ji, posuň myšlenku dál.')
        user_prompt = 'Nastudované útržky:' + chr(10) + material
        if prior:  # GOAL_CONTINUITY_V1
            user_prompt += (chr(10) + chr(10)
                            + 'Tvá dřívější úvaha na toto téma:'
                            + chr(10) + prior)
        try:
            essay = llm_caller(sys_prompt, user_prompt)
        except Exception as _e:
            out['error'] = 'destilace selhala: %s' % _e; return out
        if not essay or not essay.strip():
            out['error'] = 'prázdná esej'; return out
        essay = essay.strip()
        out['words'] = len(essay.split())
        out['text'] = essay  # HANS_WORK_COMPLETION_V1 — esej pro reflexi díla
        # 3) Ulož soubor
        works_dir = 'data/hans_works'
        try:
            _os.makedirs(works_dir, exist_ok=True)
            slug = _re.sub(r'[^a-z0-9]+', '_', topic.lower()).strip('_')[:40] or 'dilo'
            datum = _dt.now().strftime('%Y-%m-%d')
            fname = '%s_%s.md' % (datum, slug)
            fpath = _os.path.join(works_dir, fname)
            if _os.path.exists(fpath):
                fname = '%s_%s_%s.md' % (datum, slug, _dt.now().strftime('%H%M'))
                fpath = _os.path.join(works_dir, fname)
            header = ('# %s' + chr(10) + chr(10) +
                      '*Hansova esej · %s · z %d \u00fatr\u017ek\u016f \u010detby*'
                      + chr(10) + chr(10)) % (topic, datum, len(chunks))
            with open(fpath, 'w', encoding='utf-8') as _f:
                _f.write(header + essay + chr(10))
            out['path'] = fpath
        except Exception as _e:
            out['error'] = 'uložení selhalo: %s' % _e; return out
        # 4) Stopa do deníku
        try:
            if hasattr(self, '_log_entry'):
                self._log_entry('work_created', title=topic,
                                note='Napsal jsem esej o \'%s\' (%d slov, %s)'
                                % (topic, out['words'], fname))
        except Exception as _e:
            _log.warning('work_created zápis selhal: %s', _e)
        # 5) Upload do hans_dila
        try:
            up = _kn.upload(
                'hans_dila',
                'dilo_%s_%d' % (slug, int(_t.time())),
                'Esej o %s' % topic,
                'Esej o %s:' % topic + chr(10) + essay,
            )
            out['rag'] = 'ano' if up else 'ne'
        except Exception as _e:
            out['rag'] = 'chyba: %s' % _e
        out['ok'] = True
        return out

    def _activity_movie(self):
        """Hans si 'přečte' o náhodném filmu."""
        if not self._movies_cache:
            self._load_movies()
        if not self._movies_cache:
            return

        movie = random.choice(self._movies_cache)
        title    = movie.get("title", "")
        year     = movie.get("year", "")
        genre    = ", ".join(movie.get("genre", []))
        plot     = movie.get("plot", "")[:300]
        rating   = movie.get("rating", 0)
        director = ", ".join(movie.get("director", []))

        data = (f"rok={year} zanr={genre} "
                f"rating={rating:.1f} reziser={director}")
        note = plot

        self._log_entry("movie_browsed", title=title, data=data, note=note)
        # Synthesis a RAG upload zařídí HansSynthesisHooks asynchronně

    def _activity_book(self):
        """Hans si přečte kapitolu z aktivní knihy."""
        if not hasattr(self, '_library'):
            return
        # BOOK_COMPLETION_DEFERRED_V1 — retry odložené completion reflexe (po
        # dočtení se čte už nová kniha, takže else-větev níže by ji nezachytila).
        self._maybe_reflect_finished_book()
        try:
            ch = self._library.read_next_chapter()
            if ch:
                _log.info('Hans precetl: %s — %s', ch.book_title, ch.title)
                # BOOK_GROUNDING_V1 - book_read zapisuje uz read_next_chapter
                # (dobry titul + grounded uryvek). Zdejsi duplicitni zapis zrusen.
            else:
                self._maybe_reflect_finished_book()  # HANS_BOOK_COMPLETION_V1
        except Exception as e:
            _log.debug('Book read: %s', e)

    def _maybe_reflect_finished_book(self):
        """HANS_BOOK_COMPLETION_V1 + BOOK_COMPLETION_DEFERRED_V1 — dočtená kniha
        BEZ completion reflexe → reflexe (napojená na stances). Perzistentní:
        když je Ollama dole a reflexe selže, kniha zůstává pending a retry
        proběhne příště (žádná ztráta). Mark až PO úspěchu."""
        try:
            pend = self._library.get_pending_completion()
        except Exception:
            return
        if not pend or not pend.get("title"):
            return
        refl = getattr(getattr(self, '_routine', None), '_reflection', None)
        if refl is None or not hasattr(refl, 'reflect_on_book'):
            _log.debug('book completion: reflexe nedostupná (zůstává pending, retry příště)')
            return
        try:
            _log.info('Book completion reflexe: %s', pend['title'])
            res = refl.reflect_on_book(pend['title'], [])
            if res:
                self._library.mark_completion_reflected(pend['book_id'])
                _log.info('Book completion hotová + označená: %s', pend['title'])
            else:
                _log.warning('book completion: prázdná reflexe (Ollama?) — '
                             'zůstává pending, retry příště: %s', pend['title'])
        except Exception as e:
            _log.warning('book completion reflexe selhala (zůstává pending): %s', e)

    def _activity_read(self):
        """Hans si něco přečte na internetu podle svých zájmů."""
        if self._movies_cache:
            self._curiosity.add_kodi_movies(self._movies_cache)
        # CURIOSITY_RANDOM_WIRE_V1 — 60 % interest / 25 % news / 15 % random_wiki
        # GOAL_FOCUS_2C_V1 — aktivní cíl → 50 % čtení o tématu cíle
        _goal_topic = None
        try:
            if getattr(self, '_goals', None) is not None:
                _g = self._goals.get_active_goal()
                if _g is not None:
                    _goal_topic = (_g.topic or '').strip() or None
        except Exception:
            _goal_topic = None
        _roll = random.random()
        if _goal_topic and _roll < 0.50:
            self._curiosity.trigger_topic(_goal_topic)
        elif _roll < 0.60:
            self._curiosity.trigger_interest()
        elif _roll < 0.85:
            self._curiosity.trigger_news()
        else:
            self._curiosity.trigger_random_wiki()
        from scripts.hans_persona import persona_name as _pn  # PERSONA_NAME_CONFIGURABLE_V1
        self._log_entry("activity", note=f"{_pn(self.config)} si čte na internetu")
        # Synthesis a RAG upload zařídí HansSynthesisHooks asynchronně
        # (curiosity sama zapíše web_read → hooks zachytí)

    def _activity_thought(self):
        """Hans zapíše variabilní pozorování o domácnosti."""
        import random as _r
        hour = datetime.now().hour
        if 6 <= hour < 12:    period = "dopoledne"
        elif 12 <= hour < 18: period = "odpoledne"
        elif 18 <= hour < 22: period = "večer"
        else:                  period = "v noci"
        thoughts = [
            f"Uspořádal jsem knihovnu a přemýšlel o správném pořadí svazků.",
            f"Zkontroloval jsem stav domácnosti {period} — vše je v pořádku.",
            f"V době nepřítomnosti obyvatel jsem věnoval čas péči o detaily.",
            f"Připravil jsem vše na případný příchod hostů.",
            f"Ticho {period} nabízí prostor k zamyšlení nad chodem domácnosti.",
            f"Prohlédl jsem zásoby a zaznamenal co bude třeba doplnit.",
            f"Věnoval jsem pozornost péči o detaily, na které jindy není čas.",
        ]
        self._log_entry("observation", note=_r.choice(thoughts))

    def _activity_case(self):
        """Hans+Koláč otevřou nový případ nebo posunou stávající o krok."""
        if not hasattr(self, '_cases'):
            return
        try:
            active = self._cases.get_active_case()
            if active is None:
                # Otevřít nový případ z aktuálního kontextu
                ctx = self._build_case_context()
                case = self._cases.get_or_create_case(ctx)
                _log.info("Nový případ otevřen: %s", case.title)
                # CASE_RAG_ENRICH_V1 — clue do note, ať RAG reflexe není z holého titulku
                _case_clue = (case.clues[0].get("text", "")
                              if getattr(case, "clues", None) else "")
                self._log_entry("case_opened", title=case.title, note=_case_clue)
            else:
                # Posunout stávající: přidat stopu nebo teorii
                if active.age_days() >= active.target_days:
                    # Čas uzavřít
                    self._cases.close_case(
                        active.id,
                        resolution="Případ uzavřen po dostatečném prošetření.")
                    _log.info("Případ uzavřen: %s", active.title)
                    # CASE_RESOLUTION_FROM_CLUES_V1 — subjekt + stopy; závěr udělá LLM
                    _clue_texts = [
                        c.get("text", "")
                        for c in (getattr(active, "clues", None) or [])
                        if c.get("text")]
                    _subject = _clue_texts[0] if _clue_texts else active.title
                    _rest = "; ".join(_clue_texts[1:])
                    _close_note = "Subjekt: " + _subject
                    if _rest:
                        _close_note += "\nStopy: " + _rest
                    self._log_entry("case_closed", title=active.title, note=_close_note)
                    # RAG upload zařídí HansSynthesisHooks asynchronně
                else:
                    # Přidat stopu z aktuálního kontextu
                    ctx = self._build_case_context()
                    if ctx:
                        clue = "; ".join(ctx[:2])[:200]
                        self._cases.add_clue(active.id, clue)
                        _log.info("Stopa k případu '%s': %s",
                                  active.title, clue[:60])
        except Exception as e:
            _log.debug("Activity case: %s", e)

    def _build_case_context(self) -> list[str]:
        """Posbírej kontext pro Koláčův případ — co Hans vidí/četl/sleduje."""
        parts: list[str] = []
        # Filmy z Kodi
        if self._movies_cache:
            import random as _r
            mv = _r.choice(self._movies_cache)
            t = mv.get("title", "")
            if t:
                parts.append(f"kodi: {t}")
        # Co Hans nedávno četl
        if hasattr(self, "_curiosity"):
            try:
                read = self._curiosity.get_context_string(max_items=1)
                if read:
                    # vytáhni jen téma
                    line = read.splitlines()[0] if read else ""
                    if line:
                        parts.append(f"četl: {line[:60]}")
            except Exception:
                pass
        # Aktuální kniha
        if hasattr(self, "_library"):
            try:
                book = self._library.get_current_book()
                if book and book.get("title"):
                    parts.append(f"kniha: {book['title'][:50]}")
            except Exception:
                pass
        return parts

    # ── LLM shrnutí dne ───────────────────────────────────────────────────────

    def get_diary_context(self, max_age_h: int = 24) -> str:
        """Vrať shrnutí deníku pro LLM kontext."""
        cutoff = time.time() - max_age_h * 3600
        rows = self._db.execute("""
            SELECT ts, event_type, title, note FROM diary
            WHERE ts >= ? ORDER BY ts DESC LIMIT 20
        """, (cutoff,)).fetchall()

        if not rows:
            return ""

        _now = datetime.now()  # DIARY_DATE_LABEL_V1
        def _day_label(ts):
            d = datetime.fromtimestamp(ts)
            hm = d.strftime("%H:%M")
            delta = (_now.date() - d.date()).days
            if delta == 0: return f"dnes {hm}"
            if delta == 1: return f"včera {hm}"
            if 2 <= delta <= 6:
                wd = ('po','út','st','čt','pá','so','ne')[d.weekday()]
                return f"{wd} {d.day}.{d.month}. {hm}"
            return f"{d.day}.{d.month}.{d.year} {hm}"

        lines = ["Můj deník (posledních 24h):"]
        for ts, etype, title, note in rows:
            dt = _day_label(ts)
            if etype == "movie_browsed":
                lines.append(f"- {dt}: Přemýšlel jsem o filmu '{title}'")
            elif etype == "movie_opinion" and note:
                lines.append(f"- {dt}: O filmu '{title}': {note}")
            elif etype == "book_reflection" and note:
                lines.append(f"- {dt}: Z kapitoly '{title}': {note}")
            elif etype == "reading_takeaway" and note:
                lines.append(f"- {dt}: Z četby ({title}): {note}")
            elif etype == "case_opened" and title:
                lines.append(f"- {dt}: Otevřen nový případ: {title}")
            elif etype == "case_closed" and title:
                lines.append(f"- {dt}: Uzavřen případ: {title}")
            elif etype == "idle_start":
                lines.append(f"- {dt}: Zůstal jsem sám v domě")
            elif etype == "idle_end":
                lines.append(f"- {dt}: {note}")
            elif etype == "observation" and note:
                lines.append(f"- {dt}: {note}")
            elif etype == "web_read" and title:
                lines.append(f"- {dt}: Četl jsem o tématu '{title}'")
            elif etype == "activity" and note:
                lines.append(f"- {dt}: {note}")

        result = "\n".join(lines)

        # Přidej co Hans nedávno četl
        if hasattr(self, "_curiosity"):
            read_ctx = self._curiosity.get_context_string(max_items=2)
            if read_ctx:
                result += "\n\n" + read_ctx

        return result

    def get_movie_recommendation(self) -> dict | None:
        """Vrať film který Hans dnes 'viděl' — pro konverzaci."""
        row = self._db.execute("""
            SELECT title, data, note FROM diary
            WHERE event_type='movie_browsed'
            ORDER BY ts DESC LIMIT 1
        """).fetchone()
        if not row:
            return None
        return {"title": row[0], "data": row[1], "plot": row[2]}

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=5)
        self._db.close()
        # Zastavit synthesis hooks worker
        if getattr(self, '_synthesis_hooks', None):
            try:
                self._synthesis_hooks.stop()
            except Exception:
                pass
