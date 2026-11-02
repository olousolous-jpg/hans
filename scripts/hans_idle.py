"""
Hans Idle Mode
Když nikdo není doma, Hans má vlastní program dne.
- Vybere náhodný film z Kodi a přečte jeho metadata
- Zapisuje deník do SQLite
- Večer předá shrnutí velkému LLM modelu

Spouští se automaticky z display_controller_picam.py.
"""
import os   # HANS_IDLE_IMPORTS_V1 (2. 10.) — `_climate_now` (os) a stopy případů (re)
import re   # padaly na NameError do tichého except: klima náladu nikdy neovlivnilo, shrnutí případu od 2. 9. chybělo
import sqlite3
import threading
import time
import random
import logging
from pathlib import Path
from datetime import datetime

from scripts.cz_names import was as _cz_was, saw as _cz_saw, \
    display_name as _cz_display, acc as _cz_acc, \
    person_gender as _cz_gender, past_verb as _cz_past  # HANS_NAME_INFLECTION_V1

_log = logging.getLogger("hans_idle")


def _cely_dej(plot: str, strop: int = 4000) -> str:
    """HANS_TEXT_LIMITS_V1 — děj filmu celý; jen extrémně dlouhý se utne,
    a to na konci věty, ne uprostřed slova."""
    p = (plot or "").strip()
    if len(p) <= strop:
        return p
    cut = p[:strop]
    poz = max(cut.rfind(c) for c in ".!?")
    return cut[:poz + 1] if poz >= strop // 2 else cut


from scripts.hans_idle_film import FilmMixin   # ROZDELENI_METOD_V1 — část třídy HansIdle
from scripts.hans_idle_oznameni import OznameniMixin   # ROZDELENI_METOD_V1 — část třídy HansIdle
from scripts.hans_idle_kontroly import KontrolyMixin   # ROZDELENI_METOD_V1 — část třídy HansIdle
from scripts.hans_idle_udalosti import UdalostiMixin   # ROZDELENI_METOD_V1 — část třídy HansIdle
from scripts.hans_idle_cinnosti import CinnostiMixin   # ROZDELENI_METOD_V1 — část třídy HansIdle
class HansIdle(FilmMixin, OznameniMixin, KontrolyMixin, UdalostiMixin, CinnostiMixin):

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
        # KODI_AUTOPLAY_V1 — autonomní pokračování zábavy u konce titulu
        self._last_autoplay_check  = 0.0
        self._last_autoplay_ts     = 0.0
        self._autoplay_day         = ""
        self._autoplay_cnt         = 0
        self._autoplay_armed       = None   # {item_id,next,line,streak,ts} čeká na konec
        self._autoplay_last_item   = None   # id titulu, pro který už jsme rozhodli
        self._autoplay_series_streak = (None, 0)  # (showtitle, počet auto-dílů v řadě)

        from scripts.hans_curiosity import HansCuriosity
        self._curiosity = HansCuriosity(
            config        = config,
            diary_db_path = cfg.get("diary_db", "data/hans_diary.db"),
            diary_writer  = self._log_entry,  # DIARY_WRITER_PROPAGATE_IDLE_V2
            # HANS_PENDING_NO_SYNTHESIS_V1 — až catchup dožije odložené čtení,
            # teprve tehdy se pustí reflexe (se skutečným obsahem).
            resummarized_cb = lambda _t, _n: (
                self._synthesis_hooks.enqueue("web_read", _t, _n)
                if getattr(self, "_synthesis_hooks", None) else None),
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
        def _body_brain_up_cb(down_min):
            # HANS_DEFERRED_SUMMARY_V1 — mozek naběhl → hned dojeď backlog
            # odložených čtení (nejstarší napřed), nečekej na další čtení.
            try:
                if getattr(self, '_curiosity', None) is not None:
                    self._curiosity.catchup_drain_async()
            except Exception as _e:
                _log.debug("brain_up catchup_drain: %s", _e)
            # HANS_STUDY_BRAIN_UP_CATCHUP_V1 — studium se v noci často odloží
            # (mozek dole v okně 2-6, PC se budí až ranním WOL po zavření
            # okna) → na naběhnutí mozku dojeď 1 studijní session, když dnes
            # ještě neproběhla. Gate (dnes/klid) je uvnitř study_catchup.
            try:
                _rt = getattr(self, '_routine', None)
                if _rt is not None and hasattr(_rt, 'study_catchup_async'):
                    _rt.study_catchup_async()
            except Exception as _e:
                _log.debug("brain_up study_catchup: %s", _e)
            # HANS_REFLECTION_BRAIN_UP_CATCHUP_V1 — večerní reflexe vypadává ze
            # stejného důvodu jako studium: okno je noční, ale PC v něm bývá
            # vypnuté. Na naběhnutí mozku dojeď včerejšek.
            try:
                _rt = getattr(self, '_routine', None)
                if _rt is not None and hasattr(_rt, 'reflection_catchup_async'):
                    _rt.reflection_catchup_async()
            except Exception as _e:
                _log.debug("brain_up reflection_catchup: %s", _e)
            # HANS_TELEGRAM_BRAIN_NOTIFY_V1 — mozek online: dej vědět na
            # Telegram, když uživatel nedávno psal (pending), ať ví, že
            # může psát naplno. BEZ WOL.
            try:
                tg = getattr(self.chat, 'telegram', None) if self.chat else None
                if tg is None or not getattr(tg, 'enabled', False):
                    return
                if not getattr(tg, '_pending_brain_notify', False):
                    return
                tg._pending_brain_notify = False
                # TELEGRAM_QUIET_HOURS_V1 — proaktivní → v noci se odloží do 9:00
                _send = getattr(tg, 'send_proactive', tg.send)
                _send('Můj mozek je opět online — teď si můžeme normálně povídat.')
            except Exception:
                pass
        self._body.on_brain_up = _body_brain_up_cb

        from scripts.hans_mood import HansMood
        self._mood = HansMood(config, diary_db=self._db)
        # TRENDS_HISTORY_V1 — vytvoř hobby_history + seed hned při startu (jinak
        # tabulka vznikne až lazy v noci a dashboard graf nemá co číst).
        try:
            from scripts.hans_hobbies import HobbyStore
            HobbyStore(config, (config.get("diary_db")
                                or config.get("hans_idle", {}).get("diary_db")
                                or "data/hans_diary.db"))
        except Exception:
            pass
        # HANS_MORNING_HEALTH_V1 — ranní sebe-kontrola zdraví
        self._morning_health = None          # nález pro greeting/chat surfacing
        self._prev_routine_sleeping = None   # edge-detekce probuzení
        self._last_health_check_date = None  # idempotence 1×/den
        # HANS_DOWNTIME_V1 — vědomí vlastního výpadku (delší než spánek)
        self._downtime = None                # nález pro greeting/chat surfacing
        self._downtime_checked = False       # 1× za běh procesu
        self._last_lessons_check_date = None  # HANS_CORRECTION_LEARNING_V1

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

        # HANS_READ_RAG_V1 — napoj RAG uploader do curiosity (vznikl dřív než
        # knowledge), ať se přečtené články ukládají do čtenářské paměti.
        try:
            if getattr(self, '_curiosity', None) is not None:
                self._curiosity.set_knowledge(self._knowledge)
        except Exception as _ke:
            _log.debug('curiosity set_knowledge: %s', _ke)

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
        # HANS_INSTANT_LOOKUP_V1 (4.8.) — noční ověření dohledaných nálezů zapisuje
        # do paměti přes `curiosity._store` (deník + entity + RAG + synthesis hook).
        # `HansRoutine._curiosity` byl deklarovaný, ale nikdo ho nenastavoval → bez
        # tohohle drátu by ověřené nálezy neměly kam zapsat a zůstaly by pending.
        # Drátujeme TADY (ne v display_controller), protože hans_idle vlastní obojí
        # → funguje i v bězích bez displeje.
        try:
            self._routine._curiosity = self._curiosity
        except Exception as _cwe:
            _log.debug('routine curiosity wiring: %s', _cwe)
        # SEVERKA_PROACTIVE_NOTIFY_V1 — Hans sám oznámí (Telegram) Severčin návrh
        # identity. Čte self.chat.telegram až při volání (chat se drátuje později).
        def _proactive_notify(text, direct=False, photo=None):
            # HANS_NOTIFY_DIAG_V1 (6.8.) — dřív tahle funkce TIŠE nic neudělala,
            # když most chyběl nebo byl vypnutý, a výjimky šly do debug logu.
            # Volající pak hlásil „odesláno", ačkoli zpráva nikam nedorazila
            # (doloženo: test notify_queue prošel, na Matrix nic).
            # Souvisí [[robustness-silent-failure-audit]].
            try:
                tg = getattr(self.chat, 'telegram', None) if self.chat else None
                if tg is None:
                    _log.warning('proactive_notify: ŽÁDNÝ most (chat=%s) — '
                                 'zpráva NEODESLÁNA: %.60s',
                                 'None' if not self.chat else 'bez .telegram',
                                 text)
                    return False
                if not getattr(tg, 'enabled', False):
                    _log.warning('proactive_notify: most VYPNUTÝ (%s) — '
                                 'zpráva NEODESLÁNA: %.60s',
                                 type(tg).__name__, text)
                    return False
                # TELEGRAM_QUIET_HOURS_V1 — Severka běží v noci → odloží do 9:00.
                # HANS_NOTIFY_DIRECT_V1 (7.8.) — ALE `direct=True` znamená, že
                # tohle není Hansův nápad, nýbrž VÝSLEDEK toho, oč uživatel právě
                # požádal a na co čeká. Doloženo 6.8. 22:38: omluva za nepovedený
                # obraz (žádost ve 22:33) šla přes `send_proactive` → tiché hodiny
                # ji odložily do 9:00 → uživatel se ve 22:42 ptal sám. Paměť
                # [[telegram-quiet-hours]] to říká přímo: proaktivní až po 9:00,
                # ODPOVĚDI UŽIVATELI VŽDY HNED. Tiché okno zůstává na Severce
                # a proaktivitě, kde dává smysl.
                # HANS_ART_RETRY_V1 — dlužný obraz jde i s obrázkem
                if photo and __import__('os').path.exists(photo) and hasattr(tg, 'send_photo'):
                    ok = tg.send_photo(photo, text)
                else:
                    _send = tg.send if direct else getattr(tg, 'send_proactive', tg.send)
                    ok = _send(text)
                if ok is False:
                    _log.warning('proactive_notify: most odmítl odeslat '
                                 '(tiché hodiny?) — %.60s', text)
                    return False
                _log.info('proactive_notify: odesláno přes %s',
                          type(tg).__name__)
                return True
            except Exception as _pe:
                _log.warning('proactive_notify selhal: %s', _pe)
                return False
        if hasattr(self._routine, 'set_notifier'):
            self._routine.set_notifier(_proactive_notify)
        # HANS_HEAVY_QUEUE_V1 (28. 9.) — fronta náročných úloh (úprava fotky,
        # malba na požádání). Doručení: fotka zpět do místnosti, odkud přišla;
        # obraz na Matrix jen osobě, které patří výchozí místnost.
        def _heavy_deliver(text, photo, job):
            try:
                tg = getattr(self.chat, 'telegram', None) if self.chat else None
                if tg is None or not getattr(tg, 'enabled', False):
                    _log.warning('heavy_queue: most neběží — výsledek #%s jen na '
                                 'disku (%s)', job.get('id'), photo)
                    return False
                room = (job or {}).get('room') or None
                # HANS_HEAVY_PAINT_ONE_DELIVERY_V1 (29. 9.) — malbu vyžádanou
                # přes Matrix hlídá i most (`state['paint']`) a posílá ji s
                # napojením na 👍/👎 → fronta ji posílala podruhé. Obraz nechat
                # mostu; při selhání poslat omluvu a hlídání v mostu zrušit.
                # `tg` je Notifier (obal mostů) → stav hledat i v jeho mostech.
                _mst = None
                for _b in [tg] + list(getattr(tg, '_bridges', None) or []):
                    if isinstance(getattr(_b, '_cmd_state', None), dict):
                        _mst = _b._cmd_state
                        break
                if ((job or {}).get('kind') == 'paint' and isinstance(_mst, dict)
                        and (_mst.get('paint') or {}).get('pending')):
                    if photo and __import__('os').path.exists(photo):
                        _log.info('heavy_queue: #%s obraz doručí most '
                                  '(s hodnocením)', job.get('id'))
                        return True
                    _mst.pop('paint', None)
                if not room:
                    _mx = (self.config.get('matrix', {}) or {})
                    if str((job or {}).get('person') or '').lower() != str(
                            _mx.get('as_person') or '').lower():
                        _log.info('heavy_queue: #%s pro %s bez Matrixu → jen '
                                  'nástěnka', job.get('id'), job.get('person'))
                        return True
                if photo and __import__('os').path.exists(photo):
                    return tg.send_photo(photo, text, room_id=room) if room \
                        else tg.send_photo(photo, text)
                return tg.send(text, room_id=room) if room else tg.send(text)
            except Exception as _hde:
                _log.warning('heavy_queue doručení: %s', _hde)
                return False
        try:
            from scripts.hans_heavy_queue import start_worker as _hq_start
            _hq_start(config, _diary_db, _heavy_deliver)
        except Exception as _hqe:
            _log.warning('heavy_queue: start selhal: %s', _hqe)
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
        # HANS_DIARY_TYPE_INDEX_V1 (2. 10.) — ~160 dotazů filtruje event_type
        self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_diary_type_ts ON diary(event_type, ts)")
        self._db.commit()

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
        # HANS_PENDING_NO_SYNTHESIS_V1 (24.8.) — z NÁHRADNÍHO textu se reflexe
        # psát NESMÍ. Když je mozek dole, `hans_curiosity` zapíše web_read
        # s note="[topic] (nezpracováno — mozek byl mimo, doženu to)" a
        # data.pending=1; ten marker se dosud posílal do hooku JAKO OBSAH
        # ČLÁNKU, model neměl z čeho psát „co tě zaujalo" → článek si VYMYSLEL.
        # Doloženo 24.8.: pod titulkem Hitchcockova filmu „Na sever
        # severozápadní linkou" vznikla reflexe o uchování mozkové tkáně.
        # Catchup později opraví note u web_read, ale smyšlený
        # reading_takeaway už v deníku zůstane → kazí DATA.
        # Reflexe se doplní až z catchupu (`hans_curiosity`, `_resummarized_cb`).
        _pending = '"pending": 1' in (data or "") or '"pending":1' in (data or "")
        if getattr(self, '_synthesis_hooks', None) and not _pending:
            try:
                self._synthesis_hooks.enqueue(event_type, title, note)
            except Exception:
                pass
    # ── HANS_EVENT_API_V1 — public event API ──────────────────────────────────
    # Tyto metody jsou veřejný kontrakt pro display_controller a jiné moduly.
    # Volající se NEMÁ starat o interní strukturu hans_idle (curiosity, mood, ...).
    # Pokud některý subsystém není inicializovaný, metoda tiše projde.

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
        # HANS_DOWNTIME_V1 — 1× za běh: všiml jsem si, že jsem byl dlouho mimo?
        if not self._downtime_checked:
            self._downtime_checked = True
            try:
                self._downtime_check()
            except Exception as _dte:
                _log.debug('downtime check failed: %s', _dte)

        # HANS_CAPABILITY_AWARENESS_V1 (V2) — přibyla mi nová schopnost?
        # Kontrola 1× při startu (catch-up po restartu) + při ranním probuzení
        # (níže, wake edge) — ne periodicky. Když si schopnost připíše sám po
        # vytvoření díla, maker spustí detekci OKAMŽITĚ (bez čekání na ráno).
        if not getattr(self, '_capabilities_checked', False):
            self._capabilities_checked = True
            self._capability_check()

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
                    self._morning_lessons_check()  # HANS_CORRECTION_LEARNING_V1
                    self._capability_check()       # ranní: co jsem se v noci naučil
                    try:
                        from scripts import hans_schedule  # HANS_SCHEDULE_V1
                        hans_schedule.mark('morning_reflection')
                    except Exception:
                        pass
                self._prev_routine_sleeping = _slp
            except Exception as _mhe:
                _log.debug('morning_health edge check failed: %s', _mhe)

        # HANS_COMMIT_FULFILL_V1 (timer) — časované připomínky 30 min před
        # termínem (Telegram i bez přítomnosti). Vlastní throttle 60 s.
        if (time.time() - getattr(self, '_last_reminder_timer', 0.0)) >= 60.0:
            self._last_reminder_timer = time.time()
            try:
                self._timed_reminders_check()
            except Exception as _rte:
                _log.debug('timed reminders: %s', _rte)

        # CHAT_DEFERRED_V1 — přehraj zprávy, na které nebyl mozek (herní mód /
        # spící PC). `brain_available` vrací False i při herním módu, takže
        # jedna podmínka kryje oba případy. Throttle 60 s (sonda jde po síti).
        if (time.time() - getattr(self, '_last_chat_defer', 0.0)) >= 60.0:
            self._last_chat_defer = time.time()
            try:
                from scripts.chat_deferred import pending as _cd_pend
                if _cd_pend():
                    from scripts.ollama_client import brain_available
                    if brain_available(self.config):
                        from scripts.chat_deferred import drain as _cd_drain
                        _tg = getattr(self.chat, 'telegram', None) \
                            if self.chat else None
                        _send = getattr(_tg, 'send', None) if _tg else None
                        if callable(_send) and self.chat is not None:
                            _cd_drain(self.chat, _send, self.config)
            except Exception as _cde:
                _log.debug('chat_deferred drain: %s', _cde)

        # PC_SHUTDOWN_DEFER_V1 — čeká se na dokončení práce, aby šel vypnout PC?
        # Throttle 60 s: telemetrie jde po SSH, častěji nemá smysl.
        if (time.time() - getattr(self, '_last_pc_defer', 0.0)) >= 60.0:
            self._last_pc_defer = time.time()
            try:
                from scripts.pc_deferred_shutdown import tick as _pc_defer_tick
                _msg = _pc_defer_tick(self)
                if _msg:
                    _log.info('PC_SHUTDOWN_DEFER_V1: %s', _msg)
                    try:
                        tg = getattr(self, '_telegram', None) or getattr(
                            self, '_matrix', None)
                        _s = getattr(tg, 'send', None) if tg else None
                        if callable(_s):
                            _s(_msg)     # okamžitě — je to výsledek povelu
                    except Exception as _pne:
                        _log.debug('defer notify: %s', _pne)
                    try:
                        self._log_entry('pc_shutdown', 'Odložené vypnutí PC',
                                        note=_msg)
                    except Exception:
                        pass
            except Exception as _pde:
                _log.debug('pc defer tick: %s', _pde)

        # HANS_MOOD_CLIMATE_V1 — teplota do nálady. Vlastní kadence (60 s): tick()
        # běží každých pár sekund a SELECT při každém by byl zbytečný, ale delší
        # rozestup než `MOOD_WINDOW_S` by znamenal, že hlas mezi voláními vyprší
        # a klima by náladu neovlivnilo nikdy.
        try:
            _kn = time.time()
            if _kn - getattr(self, "_last_climate_mood", 0.0) >= 60.0:
                self._last_climate_mood = _kn
                if hasattr(self, "_mood") and self._mood is not None:
                    _t = self._climate_now()
                    if _t is not None:
                        _cc = (self.config.get("climate", {}) or {})
                        self._mood.update_climate(
                            _t, int(_cc.get("mood_hot_c", 27)),
                            int(_cc.get("mood_cold_c", 17)))
        except Exception as _cme:
            _log.debug("climate mood: %s", _cme)

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
                    # HANS_SPONTANEOUS_TEMPLATE_MARK_V1 (27.8.) — OBĚ
                    # větve jsou ŠABLONY, ne Hansova myšlenka: `case` =
                    # `HansCases.get_dialog_directive()`, `mood` =
                    # `random.choice(MOOD_OBSERVATIONS[nálada])`. Změřeno 27.8.:
                    # ze 429 záznamů za 30 dní je šablonových 429 (66 % mood,
                    # 34 % case) — genuinní myšlenka tam nebyla ani jedna.
                    # Bez razítka je konzumenti brali jako doklad Hansova nitra
                    # (self_insight „SPONTANEOUS THOUGHTS", večerní reflexe →
                    # extrakce postojů → Severka, recall „Mě napadlo", semínka
                    # obrazů). Záznam ZŮSTÁVÁ (Hans to opravdu řekl nahlas),
                    # značka ho jen přestane vydávat za myšlenku.
                    _sp_src = "case" if text else "mood"
                    if not text:
                        text = self._mood.get_spontaneous_text()
                    if text:
                        self._log_entry("spontaneous", note=text,
                                        data='{"template": "%s"}' % _sp_src)
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
            # KODI_AUTOPLAY_V1 — pokračování zábavy u konce titulu
            try:
                self._maybe_autoplay_next()
            except Exception as e:
                _log.error('Autoplay tick error: %s', e)

        # ATTENTION_PUBLISH_V1 — publikuj kontext pro dual-eye pozornost
        self._publish_attention()

    # ── Idle mode ─────────────────────────────────────────────────────────────

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

    # ── Aktivity ──────────────────────────────────────────────────────────────

        # Synthesis a RAG upload zařídí HansSynthesisHooks asynchronně

        # Synthesis a RAG upload zařídí HansSynthesisHooks asynchronně
        # (curiosity sama zapíše web_read → hooks zachytí)

    # ── LLM shrnutí dne ───────────────────────────────────────────────────────

    def get_diary_context(self, max_age_h: int = 24, cetba: bool = True) -> str:
        """Vrať shrnutí deníku pro LLM kontext.

        HANS_CETBA_JEDNOU_V1 (26. 10.) — `cetba=False`: bez přílohy „… si
        nedávno přečetl“; chat ji má vlastním krokem (`_sy_cetba`) a v zadání
        byla dvakrát (~1 000 zn navíc v každé odpovědi, RYCHLOST_25_10)."""
        cutoff = time.time() - max_age_h * 3600
        # HANS_DIARY_CTX_LOCK_V1 (2. 10.) — volá to chat z jiného vlákna nad
        # sdíleným připojením; ostatní místa ho drží pod self._lock
        with self._lock:
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
        if cetba and hasattr(self, "_curiosity"):
            read_ctx = self._curiosity.get_context_string(max_items=2)
            if read_ctx:
                result += "\n\n" + read_ctx

        return result

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
