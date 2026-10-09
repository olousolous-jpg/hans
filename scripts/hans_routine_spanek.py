"""Metody třídy `HansRoutine` přesunuté z `scripts/hans_routine.py` (ROZDELENI_METOD_V1).

Text metod je beze změny; jména původního modulu se čtou přes `_rt.` až při
volání. `HansRoutine` má třídu `SpanekMixin` mezi předky, takže volání přes `self` platí dál.
Import původního modulu je na KONCI souboru (kruhový import oběma směry).
"""
from __future__ import annotations

from typing import Callable, Optional
from datetime import datetime, timedelta
import sqlite3


class SpanekMixin:
    """Část třídy `HansRoutine` — viz hlavička modulu."""

    def _sleep_window_start(self) -> float:
        """HANS_SLEEP_TS_PERSIST_V1 — začátek AKTUÁLNÍHO spánkového okna.

        Spánek je čistě hodinový (`sleep_start_hour`), takže hranici lze
        spočítat místo držení dalšího stavu: po `sleep_start_hour` začala
        dnes, jinak včera. Slouží k poznání, jestli je uložená časovka
        usnutí z TÉHLE noci, nebo je to zapomenuté razítko z minulé —
        bez toho by se okno ranního scanu den ode dne natahovalo.
        """
        from datetime import timedelta as _td
        now = datetime.now()
        start_h = int(self.config.get("sleep_start_hour", 23))
        base = now.replace(hour=start_h, minute=0, second=0, microsecond=0)
        if now.hour < start_h:
            base -= _td(days=1)
        return base.timestamp()

    def set_manual_sleep(self, state: bool):
        """Manuální override z chat /sleep. Toggle proběhne ve volajícím.
        Override platí přes okno a expiruje na opačné hraně přirozeně."""
        self._manual_override = bool(state)
        _rt._log.info('SLEEP: manual override set to %s', state)
        self._apply_sleep_mode(state)

    def _apply_sleep_mode(self, active: bool):
        """Aktivuje/deaktivuje spánkový režim. Defenzivně: každý
        krok ve try/except, selhání jedné periferie nepoloží zbytek.
        Volá se jednou denně z tick() na hraně hodiny."""
        if active:
            # SLEEP_FLAG_EARLY_V1 — _sleeping=True PŘED akcemi, aby settery viděly True
            self._sleeping = True
            # HANS_MORNING_HEALTH_V1 — zaznamenej čas usnutí (okno pro ranní
            # sebe-kontrolu logů; hans_idle scanuje od této chvíle do probuzení).
            # HANS_SLEEP_TS_PERSIST_V1 (7.8.) — zapiš JEN na přechodu bdění→spánek.
            # Tahle metoda se volá i při každém restartu v noci a při ručním
            # `/sleep`; dřív se časovka pokaždé přepsala na „teď" → ranní scan
            # prošel místo celé noci jen posledních pár minut a hlásil „0 chyb"
            # (doloženo 5.–7.8.: okna 11 / 20 / 90 min).
            import time as _t_mh
            _prev_ts = self._sleep_started_ts
            if not _prev_ts or _prev_ts < self._sleep_window_start():
                # razítko je z minulé noci (nebo žádné) → tahle noc začíná teď
                self._sleep_started_ts = _t_mh.time()
                self._save_routine_state()
            _rt._log.info('SLEEP: aktivuji (TTS off + kamera nahoru)')
            # 1) Zapamatuj polohu serva + tracking stav
            try:
                if self._servo is not None and hasattr(self._servo, 'get_current_position'):
                    pos = self._servo.get_current_position()
                    # get_current_position vrací nějakou strukturu — ulož celé
                    self._saved_tilt = pos
                    _tt = getattr(self._servo, 'tracking_thread', None)  # TRACKING_RESTORE_FIX_V1
                    self._saved_tracking = bool(_tt is not None and _tt.is_alive())
            except Exception as _e:
                _rt._log.warning('SLEEP: save pos failed: %s', _e)
            # 2) Stop tracking (aby se servo nevracelo za tváří)
            try:
                if self._servo is not None and hasattr(self._servo, 'stop_tracking'):
                    self._servo.stop_tracking()
            except Exception as _e:
                _rt._log.warning('SLEEP: stop_tracking failed: %s', _e)
            # 3) Pohni kamerou nahoru — kamera je fyzicky obráceně, takže tilt_min = fyzicky nahoru
            # (SLEEP_TILT_FLIP_V1 — z naživo testu 22:00: +tilt = dolů, -tilt = nahoru)
            # HANS_GUARD_V1: při zapnutém hlídání kameru NEZVEDAT — jinak by
            # v noci (kdy hlídání nejvíc dává smysl) střežila strop.
            _guard_on = False
            try:
                from scripts import hans_guard as _hg
                _guard_on = _hg.armed()
            except Exception:
                pass
            if _guard_on:
                _rt._log.info('SLEEP: hlídání zapnuto → kamera zůstává v místnosti')
            try:
                if (not _guard_on and self._servo is not None
                        and hasattr(self._servo, 'manual_tilt')):
                    tmin = getattr(self._servo, 'tilt_min', -30)
                    self._servo.manual_tilt(tmin)
                    _rt._log.info('SLEEP: servo tilt -> %d (strop, kamera obráceně)', tmin)
            except Exception as _e:
                _rt._log.warning('SLEEP: manual_tilt up failed: %s', _e)
            # 4) TTS off — nejdůležitější (Hans/Koláč v noci nevybafnou)
            try:
                if self._tts is not None:
                    self._tts.enabled = False
                    _rt._log.info('SLEEP: TTS enabled=False')
            except Exception as _e:
                _rt._log.warning('SLEEP: TTS off failed: %s', _e)
            # 4b) Vypni kameru + recognition (SLEEP_VISION_OFF_V1)
            try:
                if self._vision is not None and hasattr(self._vision, 'pause_vision'):
                    self._vision.pause_vision()
                    _rt._log.info('SLEEP: vision off (kamera+recognition)')
            except Exception as _e:
                _rt._log.warning('SLEEP: pause_vision failed: %s', _e)
            # 4c) Oči na střed + zavřít víčka (SLEEP_EYES_CLOSE_V1) — ve spánku
            # je řízení očí za vision gate, takže by jinak zůstaly koukat tam,
            # kde naposled někoho viděly.
            try:
                if self._vision is not None and hasattr(self._vision, 'eyes_sleep'):
                    if self._vision.eyes_sleep():
                        _rt._log.info('SLEEP: oči na střed, víčka zavřena')
            except Exception as _e:
                _rt._log.warning('SLEEP: eyes_sleep failed: %s', _e)
            # 5) Stopa do deníku
            try:
                from scripts.hans_persona import persona_name as _pn  # PERSONA_NAME_CONFIGURABLE_V1
                self._diary_write('sleep_start',
                                  f'{_pn(self.config)} usnul',
                                  'TTS vypnuto, kamera otočena ke stropu.')
            except Exception as _e:
                _rt._log.debug('SLEEP: diary sleep_start failed: %s', _e)
            # SLEEP_FLAG_EARLY_V1 — self._sleeping=True přesunut na začátek if-active
        else:
            _rt._log.info('SLEEP: deaktivuji (probuzení)')
            # HANS_SLEEP_TS_PERSIST_V1 — časovku usnutí tu ZÁMĚRNĚ NEMAŽEME:
            # `_morning_health_check()` a `_morning_lessons_check()` běží až PO
            # `routine.tick()` (hans_idle:1053), takže by si přečetly prázdno.
            # O zneplatnění se stará hranice okna při dalším usnutí.
            # 1) TTS zpět
            try:
                if self._tts is not None:
                    self._tts.enabled = True
                    _rt._log.info('SLEEP: TTS enabled=True')
            except Exception as _e:
                _rt._log.warning('SLEEP: TTS on failed: %s', _e)
            # 1b) Nahoď kameru + recognition (SLEEP_VISION_OFF_V1)
            try:
                if self._vision is not None and hasattr(self._vision, 'resume_vision'):
                    self._vision.resume_vision()
                    _rt._log.info('SLEEP: vision on (kamera+recognition)')
            except Exception as _e:
                _rt._log.warning('SLEEP: resume_vision failed: %s', _e)
            # 1c) Otevřít víčka + oči na střed (SLEEP_EYES_CLOSE_V1)
            try:
                if self._vision is not None and hasattr(self._vision, 'eyes_wake'):
                    if self._vision.eyes_wake():
                        _rt._log.info('SLEEP: víčka otevřena, oči na střed')
            except Exception as _e:
                _rt._log.warning('SLEEP: eyes_wake failed: %s', _e)
            # 2) Vrátit polohu serva (zapamatovaná → fallback move_to_center)
            try:
                if self._servo is not None:
                    restored = False
                    if self._saved_tilt is not None:
                        # Zkus získat tilt číslo z různých možných tvarů (tuple/dict/scalar)
                        try:
                            t_val = None
                            sv = self._saved_tilt
                            if isinstance(sv, (int, float)):
                                t_val = sv
                            elif isinstance(sv, dict):
                                t_val = sv.get('tilt', sv.get('current_tilt'))
                            elif isinstance(sv, (tuple, list)) and len(sv) >= 2:
                                t_val = sv[1]   # předpokládáme (pan, tilt)
                            if t_val is not None and hasattr(self._servo, 'manual_tilt'):
                                self._servo.manual_tilt(float(t_val))
                                restored = True
                                _rt._log.info('SLEEP: servo tilt -> %s (návrat)', t_val)
                        except Exception as _ee:
                            _rt._log.debug('SLEEP: parse saved tilt failed: %s', _ee)
                    if not restored and hasattr(self._servo, 'move_to_center'):
                        self._servo.move_to_center()
                        _rt._log.info('SLEEP: servo -> center (fallback)')
            except Exception as _e:
                _rt._log.warning('SLEEP: restore servo failed: %s', _e)
            # 3) Tracking zpět (pokud byl)
            try:
                if (self._servo is not None and self._saved_tracking
                        and hasattr(self._servo, 'start_tracking')):
                    self._servo.start_tracking()
                    _rt._log.info('SLEEP: tracking obnoveno')
            except Exception as _e:
                _rt._log.warning('SLEEP: start_tracking failed: %s', _e)
            # 4) Stopa do deníku
            try:
                from scripts.hans_persona import persona_name as _pn  # PERSONA_NAME_CONFIGURABLE_V1
                self._diary_write('sleep_end',
                                  f'{_pn(self.config)} se probudil',
                                  'TTS zapnuto, kamera vrácena.')
            except Exception as _e:
                _rt._log.debug('SLEEP: diary sleep_end failed: %s', _e)
            self._saved_tilt = None
            self._saved_tracking = None
            self._sleeping = False

    def _sleep_watcher_loop(self):
        """SLEEP_WATCHER_THREAD_V1 — periodicky kontroluje spánkové okno NEZÁVISLE
        na tick() (který může viset na noční LLM analytice). _check_sleep_window je
        rychlý (LLM v něm neběží), takže usínání proběhne včas i při zaseklé Ollamě."""
        while not self._stop.is_set():
            # wait-first: dej startu čas navázat serva/TTS/kameru, ať první
            # _apply_sleep_mode nepropadne na None periferiích
            if self._stop.wait(self._sleep_check_interval):
                break
            try:
                if self._enabled:
                    self._check_sleep_window()
            except Exception as _e:
                _rt._log.debug('sleep watcher: %s', _e)

    def _check_sleep_window(self):
        """Idempotentně přepíná _sleeping podle hodiny. Volá SLEEP_WATCHER_THREAD_V1
        (vlastní vlákno). Zámek (non-blocking) chrání před souběhem s manuálním /sleep."""
        lock = getattr(self, '_sleep_lock', None)
        if lock is not None and not lock.acquire(blocking=False):
            return  # už běží v druhém vlákně → přeskoč (idempotentní)
        try:
            from datetime import datetime as _dt
            h = _dt.now().hour
            # Spánek: od sleep_start_hour (vč.) do sleep_end_hour (vyl.)
            # SLEEP_WRAP_WINDOW_V1 - okno muze pretekat pres pulnoc (napr. 23->9)
            if self._sleep_start_hour <= self._sleep_end_hour:
                in_window = (h >= self._sleep_start_hour and h < self._sleep_end_hour)
            else:
                in_window = (h >= self._sleep_start_hour or h < self._sleep_end_hour)

            # SLEEP_MANUAL_OVERRIDE_V1 — edge detection pro auto-expiraci
            if self._prev_in_window is None:
                self._prev_in_window = in_window
            window_started = (not self._prev_in_window) and in_window
            window_ended   = self._prev_in_window and (not in_window)
            self._prev_in_window = in_window

            # Auto-expirace override na opačné hraně okna
            if self._manual_override is True and window_ended:
                _rt._log.info('SLEEP: manual override (sleep) expired at natural wake')
                self._manual_override = None
            elif self._manual_override is False and window_started:
                _rt._log.info('SLEEP: manual override (wake) expired at natural sleep')
                self._manual_override = None

            # Výpočet should_sleep — override má přednost
            if self._manual_override is not None:
                should_sleep = self._manual_override
            else:
                should_sleep = in_window

            # SLEEP_DEBUG_CLEANUP_V1 — SLEEP CHK ztlumeno na debug (provozni sum)
            _rt._log.debug('SLEEP CHK: h=%s start=%s end=%s in_window=%s override=%s should_sleep=%s _sleeping=%s',
                       h, self._sleep_start_hour, self._sleep_end_hour, in_window, self._manual_override, should_sleep, self._sleeping)

            # WOL_TICKLOOP_REMOVED_V1 — tick-loop WOL cesta odstranena; WOL resi
            # vyhradne WOL_POLL_LOOP_V1 (hodinovy gate hour<sleep_end_hour). Stara
            # cesta volala _maybe_wake_pc bez horni casove meze -> pri vecer uspanem
            # PC hrozilo nocni probuzeni (videno 23:03 trigger, skip jen diky online).

            # HANS_DISTILLATION_V1 — fáze 2a noční destilace (po usnutí, jen 02-04)
            if (self._distillation is not None
                    and self._sleeping
                    and self._manual_override is None):
                self._maybe_distill()

            if should_sleep and not self._sleeping:
                self._apply_sleep_mode(True)
            elif (not should_sleep) and self._sleeping:
                self._apply_sleep_mode(False)
                # DISTILL_MORNING_CATCHUP_V1 — dozen destilaci po probuzeni,
                # pokud v noci padla. Idempotenci (1x/den) hlida run() sam.
                if (self._distillation is not None
                        and self._manual_override is None):
                    self._maybe_distill(ignore_window=True)
        except Exception as _e:
            _rt._log.warning('SLEEP: window check failed: %s', _e)
        finally:
            if lock is not None:
                try:
                    lock.release()
                except Exception:
                    pass

    def _morning_routine(self):
        """Ráno — počasí + co se stalo v noci."""
        # Počasí
        if self._weather:
            try:
                wx = self._weather.get_weather()
                if wx:
                    desc = wx.get("description", "")
                    temp = wx.get("temp_current")
                    wx_str = f"{desc}, {temp:.0f} C" if temp else desc
                    self._diary_write("morning_weather",
                                      "Ranní počasí", wx_str)
                    _rt._log.info("Ranní počasí: %s", wx_str)
            except Exception as e:
                _rt._log.debug("Weather error: %s", e)

    def _write_night_summary(self):
        """NIGHT_SUMMARY_REFLECTIVE_V1 — REFLEKTIVNÍ shrnutí dne (Hansovým hlasem,
        groundované ve faktech dne). Fallback na statistiku, když je Ollama dole
        (deferral-safe — shrnutí se neztratí)."""
        try:
            db = sqlite3.connect(self._diary_path)
            # NIGHT_SUMMARY_DATE_FIX_V1 — po půlnoci shrň KONČÍCÍ den (včera).
            _now = datetime.now()
            if _now.hour < self._morning_hour:
                today = datetime.fromtimestamp(_now.timestamp() - 86400).strftime("%Y-%m-%d")
            else:
                today = _now.strftime("%Y-%m-%d")

            db.close()
            # HANS_DAY_FACTS_SHARED_V1 (7.8.) — sběr faktů dne žije v
            # `hans_recall.day_facts`, aby ho sdílelo noční shrnutí i chatový
            # `/dnes` (HANS_DAY_AT_HOME_V1). Dřív tu bylo vlastní SQL; dvě
            # kopie nad týmž dnem by se časem rozešly.
            from scripts.hans_recall import (day_facts as _day_facts,
                                             day_fact_lines as _day_lines)
            _f = _day_facts(self._diary_path, today)
            n_events = _f["n_events"]
            n_dialogs = _f["n_dialogs"]
            types = _f["types"]
            reads = _f["reads"]

            stats = f"({n_events} událostí, {n_dialogs} dialogů s Kolačem)"
            facts = _day_lines(_f, self.config)

            reflective = self._night_reflection(facts) if facts else None
            if reflective:
                summary = reflective + " " + stats
            else:
                type_str = ", ".join(f"{t}({n})" for t, n in types) if types else "nic"
                read_str = ", ".join(reads) if reads else "nic"
                summary = (f"Denní shrnutí: {n_events} událostí, {n_dialogs} dialogů "
                           f"s Kolačem. Typy: {type_str}. Četl: {read_str}.")

            self._diary_write("night_summary", "Shrnutí dne", summary)
            _rt._log.info("Noční shrnutí (%s): %.100s",
                      "reflexe" if reflective else "statistika", summary)
        except Exception as e:
            _rt._log.error("Night summary error: %s", e)

    def _night_reflection(self, facts) -> Optional[str]:
        """LLM ohlédnutí za dnem z faktů (Hansův hlas). None = Ollama dole / krátké
        → volající spadne na statistiku."""
        if not facts:
            return None
        try:
            from scripts.ollama_client import ollama_generate
            from scripts.hans_persona import persona_core
        except Exception:
            return None
        try:
            core = persona_core(self.config, with_address=False)
        except Exception:
            core = ""
        model = (self.config.get("models", {}) or {}).get("dialog", "hans-czech:latest")
        system = (core + "\n\n" if core else "") + (
            "Než usneš, ohlédni se do svého deníku za DNEŠNÍM dnem — krátká osobní "
            "reflexe (4-6 vět, první osoba, tvým hlasem). Souvislé ohlédnutí, ne výčet: "
            "co se dělo, kdo tu byl, co tě zaujalo, jak na tebe den působil. Vyjdi "
            "POUZE z faktů níže — nic si nepřimýšlej. Žádný nadpis, žádné uvozovky.")
        try:
            out = ollama_generate(
                model,
                "FAKTA DNEŠNÍHO DNE:\n" + "\n".join(facts) + "\n\nNapiš reflexi.",
                system=system, config=self.config, timeout=120)
        except Exception as e:
            _rt._log.warning("night reflection LLM failed: %s", e)
            return None
        text = (out or "").strip().strip('"')
        return text[:1200] if len(text) >= 60 else None

    def _dream_fragments(self) -> str:
        """DREAM_LLM_V1 — sesbírá útržky dneška z deníku pro grounding snu."""
        import sqlite3 as _sql
        import time as _tm
        # DREAM_FRAGMENTS_24H_V1 — okno posledních 24 h, NE kalendářní „dnešek".
        # Sen se píše i po půlnoci (podmínka `_last_dream_date != today` se
        # překlopením data spustí znovu), a tam byl `date(ts)=today` prázdný →
        # `frags` prázdné → LLM se kvůli `if frags:` ani nezavolalo → pokaždé
        # generický seed. Doloženo: 6 ze 6 snů v 00:0x bylo 81–92 znaků a
        # doslovný `_DREAM_SEEDS`, zatímco večerní měly 180–262 znaků.
        since = _tm.time() - 86400
        bits = []
        try:
            db = _sql.connect(self._diary_path)
            # HANS_DREAM_DAY_MIX_V1 (14. 9.) — sen ma vychazet z CELEHO dne:
            # cetba, rozhovory, filmy, Hansova PRACE (obrazy, studium, psani)
            # i Kolac. Driv jeden nahodny vyber ze 8 typu bez prace — a v tom
            # by prehlusily caste zaznamy (7 dni: teddy_dialog 350, introspekce
            # 319 × study_note 10, writing_section 4). Proto PO JEDNOM z kazde
            # kategorie, ktera za 24 h neco ma (pokyn uzivatele 14. 9.).
            _KATEGORIE = (
                ("web_read", "reading_takeaway", "book_read", "book_reflection"),
                ("human_chat", "chat_reflection"),
                ("movie_opinion", "kodi_playing"),
                ("artwork", "study_note", "writing_section", "art_lesson"),
                ("teddy_dialog", "dialog_reflection", "case_opened", "case_closed"),
                ("introspection", "room_description"),
            )
            rows = []
            for _typy in _KATEGORIE:
                _r = db.execute(
                    "SELECT title, COALESCE(NULLIF(note,''), data) FROM diary "
                    "WHERE ts>=? AND event_type IN (%s) "
                    "AND (title<>'' OR note<>'' OR data<>'') "
                    "ORDER BY RANDOM() LIMIT 1" % ",".join("?" * len(_typy)),
                    (since, *_typy)).fetchone()
                if _r:
                    rows.append(_r)
            bk = db.execute(
                "SELECT book_title, author FROM hans_library WHERE status='reading' "
                "ORDER BY started_at DESC LIMIT 1").fetchone()
            db.close()
            for t, n in rows:
                frag = (t or "").strip()
                if n:
                    frag = (frag + ": " + n.strip()) if frag else n.strip()
                if frag:
                    bits.append("- " + frag[:120])
            if bk and bk[0]:
                bits.append(f"- čte knihu {bk[0]}" + (f" od {bk[1]}" if bk[1] else ""))
        except Exception:
            return ""
        return "\n".join(bits[:6])

    def _write_dream(self):
        """DREAM_LLM_V1 — Hans 'sní' surreální sen GROUNDOVANÝ v dnešních zážitcích
        (LLM, vysoká teplota → varieta). Běží nočně 1× za noc.

        HANS_DREAM_DEFER_V1 (14. 9.) — Vraci True, kdyz sen vznikl. Pri vypadku
        mozku, chybe LLM nebo prazdnem vystupu vrati False a NIC nezapise:
        sablona z `_DREAM_SEEDS` by v deniku lhala, ze se Hansovi neco zdalo
        ([[ollama-deferred-processing]] — vypadek LLM nesmi vyrobit nahradni data)."""
        dream = None
        if not self._brain_up():
            _rt._log.debug("sen: mozek nedostupný — odloženo")
            return False
        try:
            frags = self._dream_fragments()
            if frags:
                from scripts.ollama_client import ollama_chat
                from scripts.hans_persona import persona_name
                cfg = self.config
                ow = cfg.get("openwebui_chat", {}) or {}
                model = (cfg.get("models", {}).get("dialog") or "hans-czech:latest")
                url = ow.get("base_url", "http://127.0.0.1:11434")
                nm = persona_name(cfg)
                # PERSONA_REFACTOR_11 — sen je Hansův, identita patří z configu
                from scripts.hans_persona import persona_system as _ps
                system = _ps(cfg, (
                    "Napiš KRÁTKÝ surreální SEN "
                    "(1–2 věty, první osoba, česky), volně inspirovaný útržky z dneška. "
                    "Sen je symbolický a snový, NE doslovný popis dne. Žádné vysvětlování "
                    "ani úvod. Začni přirozeně, např. 'Zdálo se mi…' nebo 'V noci…'."))
                out = ollama_chat(
                    model,
                    [{"role": "system", "content": system},
                     {"role": "user", "content": "Útržky z dneška:\n" + frags}],
                    ollama_url=url,
                    options={"num_predict": 110, "temperature": 0.95})
                out = (out or "").strip().strip('"').strip()
                if out and len(out) > 15:
                    dream = out
        except Exception as e:
            _rt._log.warning("LLM sen selhal, sen odložen: %s", e)
        if not dream:
            _rt._log.info("sen: nevznikl (bez útržků nebo prázdná odpověď) — odloženo")
            return False
        self._diary_write("dream", "Sen", dream)
        _rt._log.info("Hans sní: %s", dream[:80])
        return True

# ROZDELENI_METOD_V1 — až na konci, viz hlavička
from scripts import hans_routine as _rt  # noqa: E402
