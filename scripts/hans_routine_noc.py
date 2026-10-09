"""Metody třídy `HansRoutine` přesunuté z `scripts/hans_routine.py` (ROZDELENI_METOD_V1).

Text metod je beze změny; jména původního modulu se čtou přes `_rt.` až při
volání. `HansRoutine` má třídu `NocMixin` mezi předky, takže volání přes `self` platí dál.
Import původního modulu je na KONCI souboru (kruhový import oběma směry).
"""
from __future__ import annotations

from datetime import datetime, timedelta
import sqlite3
import time


class NocMixin:
    """Část třídy `HansRoutine` — viz hlavička modulu."""

    def _run_night_tasks(self):
        """NIGHT_WORKER_THREAD_V1 — všechny noční LLM/analytické úlohy (dříve
        inline v tick()). Každá je idempotentní (date guard) + deferral-safe,
        takže opakované volání z workeru je bezpečné."""
        # Noční aktivity (jednou za noc)
        # HANS_NIGHT_TASKS_SPLIT_V1 (29. 9.) — dřív jedna funkce o 736 řádcích;
        # bloky jsou teď metody `_nt_*` ve STEJNÉM pořadí (vygenerováno z původního
        # textu a ověřeno porovnáním AST). Sdílený stav drží `ctx` (dnešní datum,
        # drain rozpočet, `creative_busy`). Výjimky se chovají jako dřív: blok bez
        # vlastního try shodí zbytek dávky. Nové je jen hlášení úloh nad 30 s.
        today = datetime.now().strftime("%Y-%m-%d")
        # HANS_NIGHT_DAY_CATCHUP_V1 — mimo noční fázi jen při dohánění
        # a jen úlohy z `_NIGHT_CATCHUP_TASKS` (ostatní patří večeru/půlnoci).
        _dohaneni = (not self.is_night and time.time()
                     < getattr(self, "_night_catchup_until", 0.0))
        if self.is_night or _dohaneni:
            import types as _types
            ctx = _types.SimpleNamespace(today=today, creative_busy=False,
                                         brain_down=False, drain_all=True,
                                         drain_until=0.0)
            for _jmeno, _uloha in self._night_task_list():
                if _dohaneni and _jmeno not in self._NIGHT_CATCHUP_TASKS:
                    continue
                _t0 = time.time()
                _uloha(ctx)
                _dt = time.time() - _t0
                if _dt >= 30:
                    _rt._log.info("noční úloha %s: %.0f s", _jmeno, _dt)

    def _night_task_list(self):
        """HANS_NIGHT_TASKS_SPLIT_V1 — pořadí nočních úloh (= pořadí bloků před
        rozdělením). `drain_setup` musí zůstat před první úlohou se `slot_free`."""
        return (
            ('schedule_mark', self._nt_schedule_mark),
            ('lock_audit', self._nt_lock_audit),
            ('offline_windows', self._nt_offline_windows),
            ('self_insight', self._nt_self_insight),
            ('verify_findings', self._nt_verify_findings),
            ('anomaly', self._nt_anomaly),
            ('night_summary', self._nt_night_summary),
            ('dream', self._nt_dream),
            ('relationship_reflection', self._nt_relationship_reflection),
            ('evening_reflection', self._nt_evening_reflection),
            ('stance_debates', self._nt_stance_debates),   # KOLAC_DEBATE_NIGHT_JUDGE_V1
            ('place', self._nt_place),
            ('art', self._nt_art),
            ('severka', self._nt_severka),
            ('direction', self._nt_direction),
            ('narrative', self._nt_narrative),
            ('drain_setup', self._nt_drain_setup),
            ('study', self._nt_study),
            ('toolscout', self._nt_toolscout),
            ('maker', self._nt_maker),
            ('authorship', self._nt_authorship),
            ('synthesis', self._nt_synthesis),
            ('selfcritique', self._nt_selfcritique),
            ('immune', self._nt_immune),
            ('dashboard', self._nt_dashboard),
            ('capability_curiosity', self._nt_capability_curiosity),
            ('memory_hygiene', self._nt_memory_hygiene),
            ('facts', self._nt_facts),
            ('entity_images', self._nt_entity_images),   # HANS_ENTITY_IMAGE_V1
            ('creation_reflection', self._nt_creation_reflection),
        )

    def _synthesis_due(self, today: str) -> bool:
        """HANS_SYNTHESIS_IDEAS_V1 — kadenční guard: synteze ne každou noc,
        ale po `synthesis.cadence_days` (default 3). Prázdný guard = due."""
        last = self._last_synthesis_date
        if not last:
            return True
        try:
            cad = int(self.config.get("synthesis", {}).get("cadence_days", 3))
            d0 = datetime.strptime(last, "%Y-%m-%d").date()
            d1 = datetime.strptime(today, "%Y-%m-%d").date()
            return (d1 - d0).days >= cad
        except Exception:
            return True

    def _immune_due(self, today: str) -> bool:
        """HANS_IMMUNE_A2_V1 — kadenční guard: imunitní kontrola po
        `immune.cadence_days` (default 1). Prázdný guard = due."""
        last = self._last_immune_date
        if not last:
            return True
        try:
            cad = int(self.config.get("immune", {}).get("cadence_days", 1))
            d0 = datetime.strptime(last, "%Y-%m-%d").date()
            d1 = datetime.strptime(today, "%Y-%m-%d").date()
            return (d1 - d0).days >= cad
        except Exception:
            return True

    def _selfcritique_due(self, today: str) -> bool:
        """HANS_SELFCRITIQUE_V1 — kadenční guard: sebekritika po
        `selfcritique.cadence_days` (default 2). Prázdný guard = due."""
        last = self._last_selfcritique_date
        if not last:
            return True
        try:
            cad = int(self.config.get("selfcritique", {}).get("cadence_days", 2))
            d0 = datetime.strptime(last, "%Y-%m-%d").date()
            d1 = datetime.strptime(today, "%Y-%m-%d").date()
            return (d1 - d0).days >= cad
        except Exception:
            return True

    def _nt_stance_debates(self, ctx):
        # KOLAC_DEBATE_NIGHT_JUDGE_V1 (4. 10.) — Koláčovy debaty o postojích
        # soudí v noční frontě silnější model (hans_stance_debate). Nezávisí
        # na tom, jestli se povedla reflexe: čekající debata zůstává ve frontě
        # (pending), dokud ji soud nezpracuje — nic se nepřeskočí, jen odloží.
        # Nejvýš 1× za hodinu, ať se 14b nenahrává ke každé noční debatě zvlášť.
        if time.time() - getattr(self, "_debaty_ts", 0.0) < 3600:
            return
        try:
            import sqlite3 as _sq
            _c = _sq.connect(self._diary_path, timeout=5)
            try:
                _n = _c.execute("SELECT count(*) FROM stance_debates "
                                "WHERE status='pending'").fetchone()[0]
            except _sq.OperationalError:
                _n = 0                      # tabulka ještě nevznikla
            finally:
                _c.close()
            if not _n or not self._brain_up():
                return
            self._debaty_ts = time.time()
            from scripts.hans_stance_debate import posud
            posud(self.config, self._diary_path)
        except Exception as _de:
            _rt._log.warning("soud Koláčových debat: %s", _de)

    def _nt_slot_free(self, ctx, important: bool = False) -> bool:
        if ctx.brain_down:
            return False        # LLM úlohy nemá smysl zkoušet
        if not ctx.drain_all:
            return not ctx.creative_busy      # původní chování
        if important:
            return True
        return time.time() < ctx.drain_until

    def _nt_schedule_mark(self, ctx):
        # HANS_SCHEDULE_V1 — razítko, že noční okno tikalo (i když jednotlivé
        # kroky byly deferred kvůli brain_down; audit chce vědět, že se sem
        # cesta vůbec dostala).
        try:
            from scripts import hans_schedule
            hans_schedule.mark('nightly_analytics')
        except Exception:
            pass

    def _nt_lock_audit(self, ctx):
        # HANS_LOCK_AUDIT_V1 (24. 9.) — týdenní měření uzamčení (jen čte
        # deník, 1× za ISO týden; výsledek pro lidi přes /uzamceni).
        try:
            from scripts.hans_uzamceni import record_week
            record_week(self._diary_path)
        except Exception as _ue:
            _rt._log.warning("uzamceni: %s", _ue)

    def _nt_offline_windows(self, ctx):
        # HANS_OFFLINE_WINDOWS_V1 (18.7.) — vyrobit scorable záznamy
        # „byl jsem offline T1–T2" z brain_down/up eventů (pro budoucí
        # sebe-odvození vzorců přes hans_self_insight). Idempotentní,
        # deferral-safe (jen SQL, žádný LLM).
        try:
            from scripts.hans_offline_windows import populate_offline_windows
            populate_offline_windows(self._diary_path)
        except Exception as _owe:
            _rt._log.debug("offline_windows populate: %s", _owe)

    def _nt_self_insight(self, ctx):
        # HANS_SELF_INSIGHT_V1 (18.7.) — weekly LLM analýza vlastních
        # offline/game_mode dat. Deferral-safe, dedup přes evidence_hash,
        # gate `self_insight.enabled` (default False, opatrně — reasoning tier).
        try:
            from scripts.hans_self_insight import maybe_run
            maybe_run(self._diary_path, self.config)
        except Exception as _sie:
            _rt._log.debug("self_insight maybe_run: %s", _sie)

    def _nt_verify_findings(self, ctx):
        # HANS_INSTANT_LOOKUP_V1 (4.8.) — ověř nálezy z okamžitého dohledání
        # (na co Hans odpověděl PROVIZORNĚ a záměrně si to NEZAPSAL do
        # paměti). Co projde → TEĎ se zapíše do paměti; co neprojde →
        # zůstane mimo paměť a jde ráno jako oprava uživateli.
        # Deferral-safe: mozek dole → 'deferred', zkusí se příští tick.
        try:
            from scripts.hans_findings import verify_pending, purge_old
            _vcode = verify_pending(self.config, self._diary_path,
                                    curiosity=self._curiosity)
            if _vcode not in ("idle", "deferred"):
                _rt._log.info("Ověření dohledaných nálezů: %s", _vcode)
                try:
                    purge_old(self._diary_path)
                except Exception:
                    pass
        except Exception as _ile:
            _rt._log.debug("instant_lookup verify: %s", _ile)

    def _nt_anomaly(self, ctx):
        # HANS_ANOMALY_V1 (18.7.) — algoritmický detektor odchylek (levný,
        # jen SQL count + voice krok). Kadence weekly. Doplněk k self_insight.
        try:
            from scripts.hans_anomaly import maybe_run as _anom_run
            _anom_run(self._diary_path, self.config)
        except Exception as _aoe:
            _rt._log.debug("anomaly maybe_run: %s", _aoe)

    def _nt_night_summary(self, ctx):
        if self._night_summary_enabled and self._last_summary_date != ctx.today:
            self._last_summary_date = ctx.today
            self._save_routine_state()  # HANS_NIGHT_RESTART_ONCE_V1
            self._write_night_summary()

    def _nt_dream(self, ctx):
        # HANS_DREAM_DEFER_V1 (14. 9.) — JEDEN sen za NOC a jen SKUTECNY.
        # Driv klic = kalendarni den: sen ve 22:0x a znovu po pulnoci, kdy uz
        # je PC vypnute → misto snu doslovna sablona z `_DREAM_SEEDS`.
        # Zmereno: od 7. 9. je sablona 7 z 8 pulnocnich snu a `paint_dream`
        # ji namaloval jako skutecny sen. Klic je ted datum NOCI (cas − 6 h:
        # 22:00 i 00:30 patri k tetaz noci) a razitko se zapise AZ PO uspechu,
        # takze pri vypadku mozku se sen zkusi znovu (nejvys 1× za 15 min) —
        # v praxi pri rannim WOL ve 3:00. Za 30 noci mel Hans mozek ve 22:00
        # pokazde, zadna noc tedy o sen neprijde.
        _noc = (datetime.now() - timedelta(hours=6)).strftime("%Y-%m-%d")
        if (self._night_dream_enabled and self._last_dream_date != _noc
                and time.time() >= getattr(self, "_dream_next_try", 0.0)):
            if self._write_dream():
                self._last_dream_date = _noc
                self._save_routine_state()
            else:
                self._dream_next_try = time.time() + 900

    def _nt_relationship_reflection(self, ctx):
        # Reflexe vztahových karet — 1× denně v nočním okně.
        # Spouští se po 22:30, ať to nepadne přesně se začátkem
        # noci a nezasahuje do night_summary.
        if (self._relationship_reflection is not None
                and self._last_rel_reflection_date != ctx.today):
            now_dt = datetime.now()
            if now_dt.hour > 22 or (now_dt.hour == 22 and now_dt.minute >= 30):
                self._last_rel_reflection_date = ctx.today
                self._save_routine_state()  # ROUTINE_STATE_PERSIST_V1
                try:
                    n = self._relationship_reflection.reflect_due_persons()
                    _rt._log.info("Reflexe vztahových karet: %d updatováno", n)
                    try:   # HANS_SCHEDULE_NIGHT_STEPS_V1
                        from scripts import hans_schedule as _hs
                        _hs.mark('relationship_reflection')
                    except Exception:
                        pass
                except Exception as _e:
                    _rt._log.error("Reflexe vztahových karet selhala: %s", _e)

    def _nt_evening_reflection(self, ctx):
        # Večerní reflexe dne — 1× za noc, jakmile je noc (22:00+).
        # Když run() vrátí None (Ollama nedostupná), flag se nenastaví
        # a další tick to zkusí znovu (každých check_interval_s).
        # Ruční spuštění (run_evening_reflection) také nastaví flag,
        # takže když uživatel napsal /denik dřív, auto v noci přeskočí.
        # REFLECTION_PREMIDNIGHT_ONLY_V1 - jen vecer pred pulnoci (hour>=night_hour);
        # po 00:00 today flipne a guard by firnul reflexi pro sotva zacaty
        # novy den (thin data -> konfabulace). Mirror gate u relationship refl.
        # HANS_REFLECTION_BRAIN_UP_CATCHUP_V1 — `_brain_up()` PŘED pokusem:
        # bez něj se do vypnutého PC mlátilo každý tick (38× za hodinu).
        if (self._reflection is not None
                and self._last_reflection_date != ctx.today
                and datetime.now().hour >= self._night_hour
                and self._chat_quiet_ok()      # REFLECTION_QUIET_GATE_V1
                and self._brain_up()):
            try:
                # G5G_VERIFY_IN_NIGHTTICK_V1 — ověř fakta i v noční
                # automatice (stejně jako /denik). Log-only (G5E).
                try:
                    self._g5d_verify_day(ctx.today)
                except Exception as _ve:
                    _rt._log.warning('G5D: noční verifikace selhala (reflexe pokračuje): %s', _ve)
                # HANS_NIGHT_RESTART_ONCE_V1 (7. 10.) — text reflexe se do
                # deniku zapise hned, razitko az po ~25 min navazujici
                # analytiky. Restart mezi tim psal reflexi tehoz dne znovu
                # (6. 10.: 4x). Nocni tick proto existujici text PREVEZME.
                result = self._reflection.run(reuse_existing=True)
                if result:
                    self._last_reflection_date = ctx.today
                    self._save_routine_state()  # ROUTINE_STATE_PERSIST_V1
                    _rt._log.info("Večerní reflexe: zapsána (%d znaků)",
                              len(result))
                    try:
                        from scripts.hans_schedule import mark as _sched_mark
                        _sched_mark("evening_reflection", True)
                    except Exception:
                        pass
                else:
                    _rt._log.info("Večerní reflexe: odložena "
                              "(Ollama nedostupná, zkusím znovu)")
            except Exception as _e:
                _rt._log.error("Večerní reflexe selhala: %s", _e)

    def _nt_place(self, ctx):
        # HANS_PLACE_V1 — smysl pro místo: zpracuj nové širší fotky místnosti
        # z drop-folderu data/room_photos/ na mentální mapu (qwen-VL, VRAM
        # tanec uvnitř). Idempotentní (sidecar) — když nic nového, vrátí 0
        # PŘED jakýmkoliv LLM voláním (levné). Gate night+quiet (jako art).
        if (datetime.now().hour >= self._night_hour and self._chat_quiet_ok()):
            try:
                from scripts.hans_place import PlaceStore
                _n = PlaceStore(self.config, self._diary_path).ingest_photos(self.config)
                if _n:
                    _rt._log.info("hans_place: zpracováno %d nových fotek místnosti", _n)
            except Exception as _pe:
                _rt._log.warning("hans_place: ingest fotek selhal: %s", _pe)

    def _nt_art(self, ctx):
        # HANS_ART_WIRING_V1 — ve volné chvíli (v noci) namaluj obraz k
        # dočtené knize (1 obraz/knihu, SDXL přes ComfyUI). VRAM orchestrace
        # uvnitř (unload LLM → render → warm). Deferral-safe (ComfyUI dole →
        # retry příští noc). In-memory cooldown 30 min proti hammeru. Nikdy
        # neshodí tick. Běží každou noc (gen vrátí brzo, když nic k malování).
        # HANS_ART_AFTER_DRAIN_V1 — na dnech s analytickým wake NErenderuj art
        # v night-ticku: kolidoval by s LLM drainem (catchup/study/voice na
        # GPU; FLUX vyhodí hans-czech → thrash, timeouty). Art jde přes
        # pre-shutdown render (PO drainu). Night-tick art zůstává jen pro dny
        # BEZ wake. _last_analytics_wake_date se nastaví ve 03:00 před vším.
        _woke_today = (self._last_analytics_wake_date == ctx.today
                       and bool(getattr(self, "_analytics_wake_ts", 0)))
        if (self._in_night_window() and self._chat_quiet_ok()
                and not _woke_today
                and (time.time() - getattr(self, "_last_art_attempt", 0.0)) > 1800):
            self._last_art_attempt = time.time()   # cooldown: 1 pokus / 30 min
            # HANS_ART_NIGHT_AWARE_V2 — art potřebuje ComfyUI na PC → gate na
            # REÁLNOU dostupnost (_pc_up ping), NE stavový _pc_planned_unavailable
            # (po restartu Hanse = 0 → warningy celou noc). PC dole → tiše skip.
            # _pc_up() až ZDE (po cooldownu) = ping max 1×/30 min.
            _painted = False
            if self._pc_up():
                try:
                    from scripts.hans_art import generate_pending_artwork
                    _painted = generate_pending_artwork(self.config, self._diary_path)
                except Exception as _arte:
                    _rt._log.warning("hans_art: noční render selhal: %s", _arte)
                # HANS_DREAMS_PER_DREAM_V1 — sen za KAŽDÝ nový sen (priorita, mimo
                # 2denní throttle; idempotence dle dream_ts + krátký odstup uvnitř).
                if not _painted:
                    try:
                        from scripts.hans_art import paint_dream
                        _painted = paint_dream(self.config, self._diary_path)
                    except Exception as _de:
                        _rt._log.warning("hans_art: malování snu selhalo: %s", _de)
                # HANS_CREATIONS_V1 (Fáze 2) — když nebyl ani sen, Hans SÁM zváží
                # den/úvahu (2denní throttle + variace uvnitř creative_impulse).
                if not _painted:
                    try:
                        from scripts.hans_creations import creative_impulse
                        creative_impulse(self.config, self._diary_path)
                    except Exception as _de:
                        _rt._log.warning("hans_creations: tvůrčí impuls selhal: %s", _de)

    def _nt_severka(self, ctx):
        # HANS_SEVERKA_V1 (3c) — týdenní check identity. Gate uvnitř
        # evaluate() drží, že navrhne jen při trvalé tendenci.
        # NIGHT_DEFERRAL_SAFE_V1 — guard NASTAV AŽ po ne-odloženém běhu
        # (dřív set-before → výpadek Ollamy zahodil check na CELÝ TÝDEN).
        # HANS_NIGHT_THROTTLE_REACH_V1 - bez throttle se pri mozku dole zkousela
        # kazdy tick (60 s) = 138x za noc, vcetne obou avatar kroku uvnitr.
        # SEVERKA_PENDING_GUARD_V1 — pending check je ZÁMĚRNĚ až za
        # throttlem: `_night_throttled` si pokus zaznamenává, takže se do
        # DB sáhne nejvýš 2× za hodinu, ne každých 60 s.
        if (self._severka is not None and self._severka_due(ctx.today)
                and not self._night_throttled("severka", 1800)
                and not self._severka_pending_ceka()):
            try:
                _sv_deferred = self._run_severka_check(ctx.today)
                if not _sv_deferred:
                    self._last_severka_check = ctx.today
                    self._save_routine_state()
            except Exception as _se:
                _rt._log.error("Severka check selhal: %s", _se)
            # AVATAR_DESCRIPTOR_V1 — vzhled se posune s identitou (za Severkou,
            # čerstvý CORE). Uloží novou podobu jen když needs_rerender; render
            # (SDXL) je odd. úloha co dožene pending. Selhání nesmí shodit tick.
            try:
                from scripts.avatar_descriptor import maybe_update_descriptor
                maybe_update_descriptor(self.config, self._diary_path)
            except Exception as _ae:
                _rt._log.warning("avatar descriptor update selhal (Severka OK): %s", _ae)
            # AVATAR_RENDER_WIRING_V1 — dožeň render pending descriptoru (SDXL přes
            # ComfyUI). Běží v nočním ticku za Severkou (VRAM orchestrace uvnitř:
            # unload LLM → render → warm hans-czech). Deferral-safe, nikdy nehází.
            try:
                from scripts.avatar_render import render_pending
                render_pending(self.config, self._diary_path)
            except Exception as _re:
                _rt._log.warning("avatar render pending selhal (Severka OK): %s", _re)

    def _nt_direction(self, ctx):
        # HANS_DIRECTION_V1 — týdenní úvaha o vlastním SMĚRU (intencionální
        # vrstva). Reasoning tier (qwen3, num_gpu:0 = CPU → nesoupeří o VRAM,
        # žádný handoff). Deferral-safe. Samostatná kadence, ask-first.
        if (self._direction_due(ctx.today) and self._chat_quiet_ok()
                and not self._night_throttled("direction", 1800)):
            try:
                _dir_deferred = self._run_direction_check(ctx.today)
                if not _dir_deferred:
                    self._last_direction_check = ctx.today
                    self._save_routine_state()
            except Exception as _de:
                _rt._log.error("Direction check selhal: %s", _de)

    def _nt_narrative(self, ctx):
        # AUTOBIOGRAPHICAL_NARRATIVE_V1 (krok 3) — týdenní narativní kapitola
        # (samostatná kadence, nezávislá na Severce). Base LLM, deferral-safe.
        # NIGHT_DEFERRAL_SAFE_V1 — guard NASTAV AŽ po úspěšném consolidate
        # (dřív set-before → výpadek Ollamy zahodil kapitolu na CELÝ TÝDEN).
        if (self._narrative_due(ctx.today)
                and not self._night_throttled("narrative", 1800)):
            try:
                from scripts.hans_narrative import consolidate
                _chap = consolidate(self.config, self._diary_path)
                if _chap:
                    self._last_narrative = ctx.today
                    self._save_routine_state()
                    # NARRATIVE_RAG_UPLOAD_V1 — kapitola do RAG (identita)
                    if self._knowledge is not None:
                        try:
                            self._knowledge.upload(
                                "hans_identita",
                                "narrative_%d" % int(time.time()),
                                "Kapitola životního příběhu (%s)" % ctx.today,
                                _chap,
                                metadata={"kdy": ctx.today,
                                          "typ": "narrative_chapter"})
                        except Exception as _ue:
                            _rt._log.debug("narrative RAG upload: %s", _ue)
                else:
                    _rt._log.info("narrativní kapitola: odložena "
                              "(Ollama nedostupná, zkusím znovu)")
            except Exception as _ne:
                _rt._log.warning("narrative konsolidace selhala: %s", _ne)

    def _nt_drain_setup(self, ctx):
        # HANS_SYNTHESIS_IDEAS_V1 — within-tick guard: studium/autorství/synteze
        # jsou těžké LLM tasky; ať v JEDNOM ticku neběží víc než jeden (tick by
        # zbytečně dlouho visel). Kdo z nich fírne, zvedne flag a další počká
        # na příští tick.
        ctx.creative_busy = False

        # HANS_NIGHT_DRAIN_ALL_V1 (4.8.) — „jeden task za tick" v noci HLADOVÍ.
        # Doloženo 4.8.: PC se probudil ve 3:00 KVŮLI těžké analytice, ale okno
        # 03:01→03:18 celé snědl maker (14 min, 8 obrázků) — a protože maker je
        # v pořadí PŘED syntézou/sebekritikou, ty se na řadu vůbec nedostaly.
        # `self_critique` tak nespustil od 2.8., ačkoli byl 4.8. due.
        # V nočním okně proto DRAINUJEME: běží všechno, co může, jedno po druhém
        # v témž ticku (noční worker má vlastní vlákno — nic jiného neblokuje,
        # a `_maybe_shutdown_pc` běží až PO drainu, takže PC nezhasne uprostřed).
        # Pojistky: (a) rozpočet `night_drain_budget_min` zastaví SPOUŠTĚNÍ
        # dalších úloh (rozdělaná doběhne), ať se smyčka dostane k vypnutí PC;
        # (b) těžká analytika rozpočet IGNORUJE — vyhladovět se nesmí, přesně
        # kvůli ní se budí; (c) `night_drain_all=false` = původní 1 task/tick.
        # HANS_NIGHT_DRAIN_BRAIN_GATE_V1 (5.8.) — všechny kreativní úlohy
        # jsou LLM úlohy. Když je mozek dole (noční shutdown PC / herní
        # mód), NEMÁ smysl je zkoušet: každá udělá plný pokus a zaloguje
        # „deferred". Doloženo v noci 4.→5.8.: 411 řádků mezi 00:00–03:00,
        # protože DRAIN (4.8.) nechá běžet všechny úlohy každý tick, ne
        # jednu — chování je správné, jen se to trojnásobně projevilo
        # v logu. Jedna sonda za tick místo N marných pokusů; jakmile PC
        # ve 3:00 naběhne, drain se rozjede bez zdržení (proto NE throttle
        # na čas — ten by práci po probuzení zdržel až o 30 min).
        # Vzor HANS_BRAIN_GATE_V1 / HANS_STUDY_BRAIN_GATE_V1.
        ctx.brain_down = False
        try:
            from scripts.ollama_client import brain_available as _brain_ok
            if self._in_night_window() and not _brain_ok(self.config):
                ctx.brain_down = True
                _rt._log.debug("noční tvorba: mozek dole → přeskakuji dávku")
        except Exception as _bge:
            _rt._log.debug("night drain brain gate: %s", _bge)
        _rcfg = self.config.get("hans_routine", {}) or {}
        ctx.drain_all = bool(_rcfg.get("night_drain_all", True))
        ctx.drain_until = time.time() + 60.0 * float(
            _rcfg.get("night_drain_budget_min", 90))

    def _nt_study(self, ctx):
        # HANS_STUDY_V1 — studijní program: 1 noční session = nastuduj další
        # pod-téma durable koníčku (Wikipedia → poznámka → deník+RAG). Po
        # dokončení kurikula mistrovská reflexe (grounduje vocational identitu).
        # Base LLM keep_alive=0 (VRAM tier), jen v noci. Deferral-safe:
        # 'deferred' (Ollama/wiki dole) → guard se NEnastaví, zkusí se znovu.
        # HANS_NIGHT_THROTTLE_REACH_V1 - jen NOCNI tick; brain_up catchup zustava bez
        # throttle, aby se studium po nabehnuti PC nezdrzelo o 30 min.
        if (self._last_study_date != ctx.today
                and self._in_night_window()
                and self._chat_quiet_ok()
                and not self._night_throttled("study_night", 1800)):
            ctx.creative_busy = True
            try:
                from scripts.hans_study import (run_study_session,
                                                 is_transient)
                # diary_writer záměrně NEpředáváme: _diary_write píše do
                # sloupce `note`, ale studijní poznámky musí do `data`
                # (odkud je čte _gather_notes pro mistrovskou reflexi).
                # HANS_STUDY_SINGLE_FLIGHT_V1
                if not self._study_lock.acquire(blocking=False):
                    _rt._log.debug("studium (noc): jiná session už běží → "
                               "přeskakuji tick")
                    raise _rt._StudyBusy()
                try:
                    if self._last_study_date == ctx.today:
                        raise _rt._StudyBusy()   # catchup ji mezitím dojel
                    _scode = run_study_session(
                        self.config, self._diary_path,
                        knowledge=self._knowledge)
                finally:
                    self._study_lock.release()
                if not is_transient(_scode):   # HANS_STUDY_UNIFY_V1
                    self._last_study_date = ctx.today
                    self._save_routine_state()
                    _rt._log.info("Studijní session: %s", _scode)
                else:
                    # HANS_STUDY_DEFER_LOG_V1 — brain dole → deferred každý
                    # tick (~70s): DEBUG, ne INFO (dřív ~600 řádků/noc spamu).
                    _rt._log.debug("Studijní session: deferred")
            except _rt._StudyBusy:
                pass          # HANS_STUDY_SINGLE_FLIGHT_V1 — ne chyba
            except Exception as _stue:
                _rt._log.warning("Studijní session selhala: %s", _stue)

    def _nt_toolscout(self, ctx):
        # HANS_TOOLSCOUT_V1 — po dostudování domény (study_program completed)
        # navrhni nástroj (LLM) pro finální dílo. Idempotentní per téma
        # (has_for_topic). Lehké (1 search + krátký resident LLM), deferral-safe.
        if (self._in_night_window() and self._chat_quiet_ok()
                and not self._night_throttled("toolscout", 1800)):
            try:
                from scripts import hans_toolscout as _ts
                if _ts.enabled(self.config):
                    # HANS_TOOLSCOUT_VERIFY_V1 — napřed ověř, jestli dřív
                    # schválené stahování doopravdy dorazilo (`ollama pull`
                    # běží odpojeně, takže to nikdo jinde nezjistí).
                    try:
                        _ts.verify_approved(self.config, self._diary_path)
                    except Exception as _vae:
                        _rt._log.debug("toolscout verify: %s", _vae)
                    _c = sqlite3.connect(self._diary_path, timeout=10)
                    _done = [r[0] for r in _c.execute(
                        "SELECT topic FROM study_program WHERE "
                        "status='completed'").fetchall()]
                    _c.close()
                    _store = _ts.ToolStore(self._diary_path)
                    for _tp in _done:
                        if _store.has_for_topic(_tp):
                            continue
                        _r = _ts.propose_tool(self.config, self._diary_path, _tp)
                        if _r.get("status") == "proposed" and self._notifier:
                            _top = (_r.get("proposals") or [{}])[0]
                            self._notifier(
                                "Dostudoval jsem %s. Pro finální dílo navrhuji "
                                "nástroj %s — mrkni na /nastroj." % (
                                    _tp, _top.get("tool_name", "?")))
                        # HANS_TOOLSCOUT_NO_MATCH_V1 — odložení kvůli síti
                        # je provozní šum (~20 řádků/noc), ne událost.
                        if _r.get("status") == "deferred":
                            _rt._log.debug("Toolscout '%s': deferred (%s)",
                                       _tp, _r.get("reason"))
                        else:
                            _rt._log.info("Toolscout '%s': %s (%s)", _tp,
                                      _r.get("status"), _r.get("reason", ""))
                        break  # jeden návrh za noc
            except Exception as _tse:
                _rt._log.warning("Toolscout selhal: %s", _tse)

    def _nt_maker(self, ctx):
        # HANS_MAKER_V1 + HANS_STUDY_DEEPEN_V1 — spirála studium→dílo→kritika:
        # dostudované téma → vyrob artefakt pro aktuální kolo (B); až artefakt
        # je → kriticky prohluť studium o hlubší pod-témata (C, pod capem) →
        # znovu se nastuduje (jen NOVÉ) → příště lepší dílo. 1 těžký krok/noc.
        _mk_auto = (self.config.get("maker", {}) or {}).get("auto", True)
        if (_mk_auto and self._nt_slot_free(ctx) and self._in_night_window()
                and self._chat_quiet_ok()):
            try:
                from scripts import hans_maker as _mk
                _mc = sqlite3.connect(self._diary_path, timeout=10)
                _mc.row_factory = sqlite3.Row
                _comp = _mc.execute("SELECT topic, deepen_round FROM "
                                    "study_program WHERE status='completed'"
                                    ).fetchall()
                _mc.close()
                for _pr in _comp:
                    _tp = _pr["topic"]
                    _rnd = int(_pr["deepen_round"] or 0)
                    if not _mk.has_artifact_for_round(self._diary_path, _tp, _rnd):
                        # B) vyrob dílo pro aktuální kolo (těžké → jeden/noc)
                        ctx.creative_busy = True
                        _res = _mk.make_from_study(self.config,
                                                   self._diary_path, _tp,
                                                   "coder", _rnd)
                        if _res.get("status") == "made" and self._notifier:
                            self._notifier("Z toho, co jsem nastudoval o %s, "
                                           "jsem vytvořil dílo." % _tp)
                        _rt._log.info("Maker '%s' kolo %d: %s", _tp, _rnd,
                                  _res.get("status"))
                        break
                    else:
                        # C) dílo hotové → NAVRHNI prohloubení (kritika +
                        # hlubší témata), ulož jako pending a ZEPTEJ SE
                        # uživatele (ask-first). Aplikuje se až na schválení
                        # přes /prohloubit. HANS_STUDY_DEEPEN_V2.
                        from scripts.hans_study import StudyStore as _SS
                        _dres = _SS(self.config, self._diary_path
                                    ).create_deepen_proposal(self.config, _tp)
                        if _dres.get("status") == "proposed":
                            if self._notifier:
                                _subs = "; ".join(_dres["subtopics"][:4])
                                self._notifier(
                                    "Vytvořil jsem dílo o %s. Sám vidím, že "
                                    "mu chybí: %s Navrhuji doučit se: %s. Co "
                                    "na to říkáš? (/prohloubit schválit, "
                                    "/prohloubit <vlastní kritika>, nebo "
                                    "/prohloubit ne)" % (
                                        _tp, _dres.get("critique", ""), _subs))
                            _rt._log.info("Deepen NÁVRH '%s' kolo %d (+%d, pending)",
                                      _tp, _dres["round"],
                                      len(_dres["subtopics"]))
                            break
            except Exception as _mke:
                _rt._log.warning("Maker/deepen selhal: %s", _mke)

    def _nt_authorship(self, ctx):
        # HANS_AUTHORSHIP_V1 — autorský projekt: 1 noční session = napiš další
        # sekci díla na pokračování (grounded v RAG čtení/studia). Po dokončení
        # osnovy dovětek + složení do data/works/. Deferral-safe (deferred=retry).
        # Gate: jiná noc než studium PROBĚHLO (ať se nestřetnou 2 těžké LLM tasky
        # v jednu noc) — autorství běží, jen když studium dnes nebylo potřeba.
        if (self._nt_slot_free(ctx)
                and self._last_writing_date != ctx.today
                and self._last_study_date == ctx.today
                and self._in_night_window()
                and self._chat_quiet_ok()):
            ctx.creative_busy = True
            try:
                from scripts.hans_authorship import run_writing_session
                _wcode = run_writing_session(
                    self.config, self._diary_path, knowledge=self._knowledge)
                if _wcode != "deferred":
                    self._last_writing_date = ctx.today
                    self._save_routine_state()
                _rt._log.info("Autorská session: %s", _wcode)
                # HANS_SCHEDULE_NIGHT_STEPS_V1 (20.8.) — noční krok se hlásí
                # rozvrhu, aby šlo poznat, že přestal běhat. `deferred`
                # = mozek byl dole → NEpočítá se jako úspěšný běh
                # (HANS_SCHEDULE_LAST_OK_V1).
                try:
                    from scripts import hans_schedule as _hs
                    _hs.mark('writing_session', ok=(_wcode != 'deferred'),
                             skip_reason='' if _wcode != 'deferred' else 'deferred')
                except Exception:
                    pass
            except Exception as _aue:
                _rt._log.warning("Autorská session selhala: %s", _aue)

    def _nt_synthesis(self, ctx):
        # HANS_SYNTHESIS_IDEAS_V1 (#2) — vlastní nápady / synteze: propojí věci
        # z RŮZNÝCH oblastí (reading_takeaway/study_note za 30 dní) do JEDNOHO
        # nového postřehu (Hansův hlas, grounded). Kadence `cadence_days` (default
        # 3) — ne každou noc. Lehčí než studium (1 LLM volání), ale stejně těžké
        # na VRAM → within-tick guard, jen v noci, deferral-safe (deferred=retry).
        if (self._nt_slot_free(ctx, important=True)
                and self._synthesis_due(ctx.today)
                and self._in_night_window()
                and self._chat_quiet_ok()):
            ctx.creative_busy = True
            try:
                from scripts.hans_ideas import run_synthesis_session
                _ycode = run_synthesis_session(
                    self.config, self._diary_path, knowledge=self._knowledge)
                if _ycode != "deferred":
                    self._last_synthesis_date = ctx.today
                    self._save_routine_state()
                _rt._log.info("Synteze nápadů: %s", _ycode)
                # HANS_SCHEDULE_NIGHT_STEPS_V1 (20.8.) — noční krok se hlásí
                # rozvrhu, aby šlo poznat, že přestal běhat. `deferred`
                # = mozek byl dole → NEpočítá se jako úspěšný běh
                # (HANS_SCHEDULE_LAST_OK_V1).
                try:
                    from scripts import hans_schedule as _hs
                    _hs.mark('synthesis_session', ok=(_ycode != 'deferred'),
                             skip_reason='' if _ycode != 'deferred' else 'deferred')
                except Exception:
                    pass
            except Exception as _yue:
                _rt._log.warning("Synteze nápadů selhala: %s", _yue)

    def _nt_selfcritique(self, ctx):
        # HANS_SELFCRITIQUE_V1 (#6) — sebekritika z vlastního popudu: z Hansových
        # nedávných replik (human_chat/teddy_dialog) najde slabé místo KVALITY
        # projevu (rozvláčnost/opakování/fráze) a uloží ponaučení `self_critique`.
        # NEMĚNÍ paměť/postoje; příště to má v chat kontextu vedle korekčních lekcí.
        # Base LLM keep_alive=0, kadence `selfcritique.cadence_days` (default 2),
        # within-tick guard. Deferral-safe: 'deferred' (LLM dole) → guard se
        # NEnastaví, retry; 'idle'/'critiqued' (LLM běžel) → kadence drží odstup.
        if (self._nt_slot_free(ctx, important=True)
                and self._selfcritique_due(ctx.today)
                and self._in_night_window()
                and self._chat_quiet_ok()):
            ctx.creative_busy = True
            try:
                from scripts.hans_selfcritique import run_self_critique
                _ccode = run_self_critique(self.config, self._diary_path)
                if _ccode != "deferred":
                    self._last_selfcritique_date = ctx.today
                    self._save_routine_state()
                _rt._log.info("Sebekritika: %s", _ccode)
                # HANS_SCHEDULE_NIGHT_STEPS_V1 (20.8.) — noční krok se hlásí
                # rozvrhu, aby šlo poznat, že přestal běhat. `deferred`
                # = mozek byl dole → NEpočítá se jako úspěšný běh
                # (HANS_SCHEDULE_LAST_OK_V1).
                try:
                    from scripts import hans_schedule as _hs
                    _hs.mark('selfcritique', ok=(_ccode != 'deferred'),
                             skip_reason='' if _ccode != 'deferred' else 'deferred')
                except Exception:
                    pass
            except Exception as _cue:
                _rt._log.warning("Sebekritika selhala: %s", _cue)

    def _nt_immune(self, ctx):
        # HANS_IMMUNE_A2_V1 — imunitní systém: noční fact-check Hansových
        # VLASTNÍCH tvrzení („X je/byl Y") proti entity store (verbatim
        # glosy z jeho čtení). Rozpor → lesson_learned (surfacuje se
        # v chatu i Koláčově dialogu existujícím wiringem). NEMAŽE záznamy.
        # Lehké LLM (base, num_predict 8, max 6 volání), kadence
        # `immune.cadence_days` (default 1). Deferral-safe: 'deferred'
        # (LLM dole) → guard se NEnastaví → retry příští tick.
        if (self._nt_slot_free(ctx)
                and self._immune_due(ctx.today)
                and self._in_night_window()
                and self._chat_quiet_ok()):
            ctx.creative_busy = True
            try:
                # HANS_BASE_MODEL_BATCH_V1 — immune běží na base OpenEuroLLM
                # (8GB). Aktivní VRAM handoff (pause + unload hans-czech),
                # ne jen pause: keep_alive=-1 hans-czech sám nevyprší → jinak
                # 8+8 > 16GB → 300s timeout (doloženo v noci 2.8.).
                # HANS_IMMUNE_SLOT_LATE_V1 (11. 9.) — dávku si otevírá AŽ
                # `run_immune_check` sám, a to teprve když má co kontrolovat.
                # Obalovat ji odsud znamenalo čekat na VRAM slot (11. 9.
                # celých 600 s) a odpojit hans-czech i v nocích, kdy immune
                # rovnou skončí na „žádné kontrolovatelné tvrzení".
                from scripts.hans_immune import run_immune_check
                _icode = run_immune_check(self.config, self._diary_path)
                if _icode != "deferred":
                    self._last_immune_date = ctx.today
                    self._save_routine_state()
                _rt._log.info("Imunitní kontrola: %s", _icode)
                # HANS_SCHEDULE_NIGHT_STEPS_V1 (20.8.) — noční krok se hlásí
                # rozvrhu, aby šlo poznat, že přestal běhat. `deferred`
                # = mozek byl dole → NEpočítá se jako úspěšný běh
                # (HANS_SCHEDULE_LAST_OK_V1).
                try:
                    from scripts import hans_schedule as _hs
                    _hs.mark('immune_check', ok=(_icode != 'deferred'),
                             skip_reason='' if _icode != 'deferred' else 'deferred')
                except Exception:
                    pass
            except Exception as _iue:
                _rt._log.warning("Imunitní kontrola selhala: %s", _iue)

    def _nt_dashboard(self, ctx):
        # HANS_DASHBOARD_PROPOSAL_V1 (Tier 1) — JEDNORÁZOVĚ po dostudování
        # Designu: Hans napíše designovou kritiku + návrh vlastní nástěnky
        # (grounding = fakta z šablony + jeho studijní poznámky) + SDXL
        # mockup. Gate uvnitř run_ (completed studium && žádný proposal).
        # Deferral-safe ('deferred' → retry příští tick), within-tick guard.
        if (self._nt_slot_free(ctx)
                and self._in_night_window()
                and self._chat_quiet_ok()):
            try:
                from scripts.hans_dashboard import run_dashboard_proposal
                _dcode = run_dashboard_proposal(self.config, self._diary_path)
                if _dcode == "proposed":
                    ctx.creative_busy = True
                    _rt._log.info("Návrh nástěnky: proposed")
                elif _dcode == "deferred":
                    _rt._log.info("Návrh nástěnky: deferred (retry)")
            except Exception as _dbe:
                _rt._log.warning("Návrh nástěnky selhal: %s", _dbe)

    def _nt_capability_curiosity(self, ctx):
        # HANS_CAPABILITY_CURIOSITY_V1 — Hans si nově objevenou schopnost
        # ZVĚDAVĚ vyzkouší (u paint reálně namaluje) + reflexe „co svedu, co
        # jsem zjistil". Gate: čeká pending exploration. Deferral-safe.
        if (self._nt_slot_free(ctx)
                and self._in_night_window()
                and self._chat_quiet_ok()):
            try:
                from scripts.hans_capabilities import (
                    pending_explorations, explore_capability)
                if pending_explorations(self._diary_path):
                    _ecode = explore_capability(self.config, self._diary_path)
                    if _ecode == "explored":
                        ctx.creative_busy = True
                        _rt._log.info("Zvědavost na schopnost: explored")
                    elif _ecode == "deferred":
                        _rt._log.info("Zvědavost na schopnost: deferred (retry)")
            except Exception as _ece:
                _rt._log.warning("Zvědavost na schopnost selhala: %s", _ece)

    def _nt_memory_hygiene(self, ctx):
        # HANS_MEMORY_HYGIENE_V1 (#2) — retenční prořez deníkového firehose
        # (person_seen/teddy_* starší než per-typ okno). Whitelist (smysluplné
        # eventy netknuté). Čistě SQL → rychlé, 1×/noc, gate night+quiet
        # (DELETE krátce zamkne diary DB — proto když je ticho).
        if (self._last_hygiene_date != ctx.today
                and self._in_night_window()
                and self._chat_quiet_ok()):
            self._last_hygiene_date = ctx.today
            self._save_routine_state()
            try:
                from scripts.hans_memory_hygiene import prune_diary
                _pruned = prune_diary(self.config, self._diary_path)
                if _pruned:
                    _rt._log.info("memory_hygiene: prořezáno %d řádků",
                              sum(_pruned.values()))
            except Exception as _hye:
                _rt._log.warning("memory_hygiene prořez selhal: %s", _hye)

    def _nt_facts(self, ctx):
        # HANS_FACTS_NIGHTLY_V1 (31.8.) — DOPLNĚNÍ FAKT z Wikidat pro nové
        # entity. `scripts/hans_facts.py` byl postaven 26.8., BACKFILL SE
        # PUSTIL RUČNĚ A PAK UŽ NIKDY — `grep -rn hans_facts scripts/*.py`
        # vracel NULA volajících. Za pět dní se nasbíralo 27 entit bez
        # fakt; čte je `hans_maker` (HANS_MAKER_IMAGE_FACTS_V1), takže se
        # nová entita maluje bez slohu, materiálu a datace.
        # ⚠️ Přesně tentýž případ jako `IMPORTANCE_SCHEDULE_V1` (rutina
        # stála 2 měsíce a sebe-audit hlásil „v pořádku", protože o ní
        # nevěděl) → proto je krok ZÁROVEŇ v `hans_schedule`.
        # ✅ NEPOTŘEBUJE MOZEK: Wikidata, 0 % LLM, žádná VRAM → smí běžet
        # i když je PC vypnuté. Gate je jen noc+ticho kvůli síti a zámku DB.
        # ⚠️ `limit` malý schválně — Wikidata vracely 429 už po ČTYŘECH
        # entitách; `backfill` sám ustupuje a při rate limitu končí, aniž
        # by entitu odškrtl ([[study-findability-guards]]).
        if (self._last_facts_date != ctx.today
                and self._in_night_window()
                and self._chat_quiet_ok()):
            self._last_facts_date = ctx.today
            self._save_routine_state()
            try:
                from scripts.hans_facts import backfill as _fb
                _fr = _fb(self._diary_path, limit=20)
                if _fr.get("zkouseno"):
                    _rt._log.info("facts: doplněno %d polí u %d entit "
                              "(ok %d, bez tvrzení %d, bez qid %d%s)",
                              _fr.get("poli", 0), _fr.get("zkouseno", 0),
                              _fr.get("ok", 0), _fr.get("bez_tvrzeni", 0),
                              _fr.get("bez_qid", 0),
                              ", RATE LIMIT" if _fr.get("rate_limit") else "")
                try:
                    import scripts.hans_schedule as _hs
                    _hs.mark("facts_backfill", ok=True)
                except Exception:
                    pass
            except Exception as _fe:
                _rt._log.warning("facts backfill selhal: %s", _fe)

    def _nt_entity_images(self, ctx):
        # HANS_ENTITY_IMAGE_V1 (29. 9.) — k osobám/místům/dílům z Wikipedie
        # doplní hlavní obrázek (data/entity_images/). Malá dávka 1×/noc kvůli
        # limitům Wikipedie; nepotřebuje mozek ani PC. Líně se obrázek stáhne
        # i při malování (`hans_art._fetch_person_ref`).
        if (getattr(self, "_last_entity_images_date", "") != ctx.today
                and self._in_night_window()
                and self._chat_quiet_ok()):
            self._last_entity_images_date = ctx.today
            try:
                from scripts.hans_entity_images import backfill as _eib
                _er = _eib(self.config, self._diary_path, limit=int(
                    (self.config.get("entity_images", {}) or {}).get("night_batch", 10)))
                if _er.get("zkouseno"):
                    _rt._log.info("entity_images: noční dávka — uloženo %d z %d",
                              _er.get("ulozeno", 0), _er.get("zkouseno", 0))
            except Exception as _eie:
                _rt._log.warning("entity_images noční dávka selhala: %s", _eie)

    def _nt_creation_reflection(self, ctx):
        # HANS_CREATION_REFLECTION_V1 (D) — týdně: reflexe vlastní tvorby
        # (sebepoznání, NE postoje). Samostatná kadence. Deferral-safe.
        try:
            _last_cr = self._last_creation_reflection
            _due_cr = True
            if _last_cr:
                try:
                    _d0 = datetime.strptime(_last_cr, "%Y-%m-%d").date()
                    _d1 = datetime.strptime(ctx.today, "%Y-%m-%d").date()
                    _due_cr = (_d1 - _d0).days >= 7
                except Exception:
                    _due_cr = True
            if (_due_cr and self._reflection is not None
                    and hasattr(self._reflection, 'reflect_on_creations')):
                # HANS_CREATION_REFLECTION_GUARD_FIX_V1 (5.8.) — razítko se
                # dávalo PŘED voláním, takže neúspěch (mozek dole → text=None,
                # nebo málo tvorby) zablokoval reflexi na CELÝ TÝDEN.
                # Doloženo: routine_state hlásil běh 30.7., ale poslední
                # `creation_reflection` v deníku je z 16.7. — dvacet dní ticha
                # při 141 obrazech za měsíc. Deferral-safe vzor (jako studium
                # a syntéza): razítkuj AŽ po úspěchu, jinak zkus příští noc.
                _cr = self._reflection.reflect_on_creations()
                if _cr:
                    self._last_creation_reflection = ctx.today
                    self._save_routine_state()
                    _rt._log.info("Reflexe tvorby: zapsána")
                    try:   # HANS_SCHEDULE_NIGHT_STEPS_V1
                        from scripts import hans_schedule as _hs
                        _hs.mark('creation_reflection')
                    except Exception:
                        pass
                else:
                    _rt._log.debug("Reflexe tvorby: neproběhla (mozek/málo dat) "
                               "— zkusím příště")
        except Exception as _cre:
            _rt._log.warning("creation reflection selhala: %s", _cre)

# ROZDELENI_METOD_V1 — až na konci, viz hlavička
from scripts import hans_routine as _rt  # noqa: E402
