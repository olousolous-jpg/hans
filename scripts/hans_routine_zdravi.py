"""Metody třídy `HansRoutine` přesunuté z `scripts/hans_routine.py` (ROZDELENI_METOD_V1).

Text metod je beze změny; jména původního modulu se čtou přes `_rt.` až při
volání. `HansRoutine` má třídu `ZdraviMixin` mezi předky, takže volání přes `self` platí dál.
Import původního modulu je na KONCI souboru (kruhový import oběma směry).
"""
from __future__ import annotations

from datetime import datetime, timedelta
import json
import os
import time


class ZdraviMixin:
    """Část třídy `HansRoutine` — viz hlavička modulu."""

    def _router_watcher_loop(self):
        """HANS_ROUTER_V1 — krok automatiky VPN á `router.watch_interval_s`.
        Oznámení jde přes `_notifier` bez `direct` → v tichém okně počká."""
        if self._stop.wait(120.0):
            return
        while not self._stop.is_set():
            _rc = self.config.get('router') or {}
            try:
                from scripts import hans_router
                hans_router.watch_tick(self.config, self._router_st,
                                       notify=self._notifier)
            except Exception as _e:
                _rt._log.debug('router watcher: %s', _e)
            if self._stop.wait(float(_rc.get('watch_interval_s', 60))):
                break

    def _model_stuck_heal(self, oll_status) -> bool:
        """HANS_OLLAMA_MODEL_STUCK_V1 — restart Ollamy, když ollama_client
        zapsal značku „model se nenačetl“. Jen čerstvá značka (15 min), ne
        v herním módu ani při plánované nedostupnosti PC, a nejvýš 1× za
        30 min (kdyby to restart nespravil, ať se netočí dokola)."""
        import json as _json, os as _os, time as _t
        from scripts.ollama_client import STUCK_FLAG, game_mode_on
        from scripts import hans_health
        try:
            with open(STUCK_FLAG, encoding="utf-8") as f:
                zn = _json.load(f)
        except FileNotFoundError:
            return False
        except Exception:
            zn = {}
        stari = _t.time() - float(zn.get("ts") or 0)
        if stari > 900:
            _os.remove(STUCK_FLAG)
            return False
        if oll_status != hans_health.OK or game_mode_on() \
                or self._pc_planned_unavailable():
            return False
        if _t.time() - getattr(self, "_model_stuck_heal_ts", 0.0) < 1800:
            return False
        _rt._log.warning("health: model %s se nenačítá (hans-czech odpovídá) → "
                     "restartuji Ollamu", zn.get("model"))
        self._model_stuck_heal_ts = _t.time()
        try:
            _os.remove(STUCK_FLAG)
        except Exception:
            pass
        return bool(hans_health.heal_ollama(self.config))

    def _health_watcher_loop(self):
        """HANS_HEALTH_V1 — periodická probe závislostí + self-heal zaseklé
        Ollamy. Vlastní vlákno (nezávislé na tick). Self-heal AŽ po N po sobě
        jdoucích WEDGED (transientní timeout nerestartuje); DOWN (PC spí / služba
        neběží) se NEheal-uje — to není zásek, ale legitimní nedostupnost."""
        # po startu nech služby usadit
        if self._stop.wait(min(120.0, self._health_interval)):
            return
        while not self._stop.is_set():
            # HANS_BACKUP_WATCH_V1 (29. 9.) — stáří záloh Pi (NAS) a PC (restic),
            # 1× denně po záloze Pi (21:50); častější přístup by budil NAS,
            # který má usínat. Hlášení jde samo na Matrix.
            try:
                _zc = (self.config.get("zalohy_hlidac", {}) or {})
                _zd = datetime.now()
                if (getattr(self, "_zalohy_den", "") != _zd.strftime("%Y-%m-%d")
                        and (_zd.hour, _zd.minute) >= (int(_zc.get("hodina", 22)),
                                                       int(_zc.get("minuta", 30)))):
                    self._zalohy_den = _zd.strftime("%Y-%m-%d")
                    from scripts.hans_zalohy import hlidej as _zh
                    _zh(self.config, self._notifier)
            except Exception as _ze:
                _rt._log.warning("hlídač záloh: %s", _ze)
            # HANS_HLAVNI_ZPRAVA_V1 (8. 10.) — hlavní zpráva posledních dvou dnů
            # sama na Matrix (výběr, text a podmínky: `hans_hlavni_zprava.tick`).
            # Ve vlákně: psaní volá model a tick nesmí stát.
            try:
                import time as _hzt
                if (_hzt.time() - getattr(self, "_hz_ts", 0.0) > 600
                        and not getattr(self, "_hz_bezi", False)):
                    self._hz_ts = _hzt.time()
                    self._hz_bezi = True

                    def _hz_prace():
                        try:
                            from scripts.hans_hlavni_zprava import tick as _hz_tick
                            _hz_tick(self.config, self._notifier, self._diary_path)
                        except Exception as _hze:
                            _rt._log.warning("hlavní zpráva: %s", _hze)
                        finally:
                            self._hz_bezi = False
                    import threading as _hzth
                    _hzth.Thread(target=_hz_prace, name="hlavni-zprava", daemon=True).start()
            except Exception as _hze:
                _rt._log.warning("hlavní zpráva: %s", _hze)
            # HANS_LETAKY_V1 (9. 10.) — středeční přehled slev z letáků podle
            # hlídaného seznamu (kdy a co: `hans_letaky.tick`). Ve vlákně:
            # stahování trvá ~2 minuty a tick nesmí stát. Bez modelu.
            try:
                import time as _ltt
                if (_ltt.time() - getattr(self, "_letaky_ts", 0.0) > 600
                        and not getattr(self, "_letaky_bezi", False)):
                    self._letaky_ts = _ltt.time()
                    self._letaky_bezi = True

                    def _letaky_prace():
                        try:
                            from scripts.hans_letaky import tick as _lt_tick
                            _lt_tick(self.config, self._notifier)
                        except Exception as _lte:
                            _rt._log.warning("letáky: %s", _lte)
                        finally:
                            self._letaky_bezi = False
                    import threading as _ltth
                    _ltth.Thread(target=_letaky_prace, name="letaky", daemon=True).start()
            except Exception as _lte:
                _rt._log.warning("letáky: %s", _lte)
            # BODY_TRACK_ROZPOR_HLIDAC_V1 (4. 10.) — snímky rozporů stopy postavy
            # a tváře (BODY_TRACK_ROZPOR_SNIMEK_V1): až jich je dost a aspoň ze
            # dvou dní, JEDNOU dát vědět na Matrix, že je čas je označit.
            try:
                _bc = (self.config.get("body_track", {}) or {})
                _bn = int(_bc.get("rozpor_snapshot_notify", 0) or 0)
                _bd = os.path.join(_bc.get("log_dir", "data/mereni/postava_stin"), "snimky")
                _bf = os.path.join(_bd, ".nahlaseno")
                if _bn and os.path.isdir(_bd) and not os.path.exists(_bf):
                    _bs = [f for f in os.listdir(_bd) if f.endswith(".jpg")]
                    _bdny = {f[:8] for f in _bs}
                    if len(_bs) >= _bn and len(_bdny) >= 2:
                        if self._notifier and self._notifier.send_proactive(
                                "Snímků s rozporem postavy a tváře je %d (z %d dní) — dost "
                                "na rozhodnutí. Leží v %s; u každého stačí říct, jestli má "
                                "pravdu zelený rámeček (postava), nebo červený (tvář)."
                                % (len(_bs), len(_bdny), _bd)):
                            open(_bf, "w").write(time.strftime("%Y-%m-%d %H:%M"))
                            _rt._log.info("BODY_TRACK_ROZPOR_HLIDAC_V1: nahlášeno (%d snímků)",
                                      len(_bs))
            except Exception as _be:
                _rt._log.warning("hlídač snímků postavy: %s", _be)
            try:
                from scripts import hans_health
                health = hans_health.probe_all(self.config)
                self._health_last = health
                healed = []
                oll = (health.get('ollama', {}) or {}).get('status')
                if oll == hans_health.WEDGED:
                    self._health_wedge_strikes += 1
                    need = int((self.config.get('health', {}) or {}).get(
                        'wedge_strikes', 2))
                    # HANS_HEALTH_EMBED_HEAL_NOW_V1 (8. 10.) — „vektory visí“ sonda
                    # hlásí až po DVOU vlastních selháních; další dva údery tady
                    # znamenaly opravu až po 30–40 min (8. 10. 12:23 → ručně 12:44).
                    # Jen přes den: v noci počítá úsudkový model zčásti na CPU a vektory
                    # pak mohou být jen pomalé (obava ze 7. 10.) — tam zůstávají dva údery.
                    if ('vektory vis' in str((health.get('ollama', {}) or {}).get('detail', ''))
                            and 8 <= datetime.now().hour < 22):
                        need = 1
                    if self._health_wedge_strikes >= need:
                        # HANS_HEALTH_NIGHT_AWARE_V1 — ráno po WOL Ollama BOOTUJE
                        # (server běží, negeneruje = „WEDGED"), ale mozek nebyl
                        # zaseklý. NErestartuj (prodloužilo by boot) a NEhlaš
                        # „zasekl se, restartoval jsem ho" — nech doběhnout.
                        if self._pc_planned_unavailable():
                            self._health_wedge_strikes = 0
                        elif hans_health.heal_ollama(self.config):
                            healed.append('ollama')
                            self._health_wedge_strikes = 0
                            if self._notifier:
                                try:
                                    self._notifier("Můj mozek se zasekl, "
                                                   "restartoval jsem ho.")
                                except Exception:
                                    pass
                else:
                    self._health_wedge_strikes = 0
                # HANS_OLLAMA_MODEL_STUCK_V1 — zásek per model (značka od
                # ollama_client). Sonda ho nevidí, protože zkouší jen hans-czech.
                try:
                    if 'ollama' not in healed and self._model_stuck_heal(oll):
                        healed.append('ollama')
                except Exception as _se:
                    _rt._log.warning("health: model stuck heal: %s", _se)
                hans_health._write_state(health, healed)
                bad = hans_health.degraded_services(health)
                # HANS_HEALTH_LOG_CIRCUIT_V1 — hlas jen na HRANĚ stavu: WARNING
                # jednou při zhoršení, pak ticho (DEBUG) dokud se stav nezmění,
                # a jedno INFO při obnově. Bez toho health mlel WARNING à 10 min
                # celou noc, když byl PC dole z důvodu, který Hans nezpůsobil
                # (uživatel/spánek/pád → _pc_shutdown_ts se nenastaví).
                bad_key = tuple(sorted(bad))
                prev_bad = getattr(self, '_health_last_bad', ())
                if bad:
                    if self._pc_planned_unavailable():
                        _rt._log.debug('health: degradováno %s (PC záměrně dole/boot)',
                                   bad)
                    elif bad_key == prev_bad and not healed:
                        _rt._log.debug('health: stále degradováno %s', bad)
                    else:
                        _rt._log.warning('health: degradováno %s (healed=%s, strikes=%d)',
                                     bad, healed, self._health_wedge_strikes)
                elif prev_bad:
                    _rt._log.info('health: obnoveno — vše OK (bylo %s)', list(prev_bad))
                # VOICE_MIC_WATCHDOG_V1 — hluchý mikrofon se hlasem neohlásí;
                # na hraně pošli zprávu (v tichých hodinách počká do rána).
                if 'mic' in bad and 'mic' not in prev_bad and self._notifier:
                    try:
                        self._notifier(
                            "Neslyším: nahrávání z mikrofonu nejde (%s). "
                            "Zkouším ho sám obnovit."
                            % ((health.get('mic') or {}).get('detail') or 'důvod neznám'))
                    except Exception as _me:
                        _rt._log.debug('health: hlaseni mikrofonu: %s', _me)
                self._health_last_bad = bad_key
                # HANS_SCHEDULE_NOTIFY_V1 (19. 9.) — hlidac tichych selhani mel
                # vetu pro uzivatele (`_schedule_sentence`), ale NIKDO ji
                # nevolal: `summary_sentence` nema v produkci volajiciho, takze
                # WARN skoncil v health_state.json a cekal, az se nekdo zepta
                # (/zdravi za celou historii 1x). Hlasi se na HRANE, tymz
                # vzorem jako log vys: jednou pri vzniku, pak ticho, dokud se
                # mnozina zaostavajicich rutin nezmeni.
                # ⚠️ `direct` se NEPREDAVA → callback pouzije `send_proactive`,
                # takze v tichem okne (22-9) zprava pocka do rana. Zaostavajici
                # rutina neni nic, kvuli cemu budit.
                try:
                    # HANS_SCHEDULE_NOTIFY_BRAIN_UP_V1 (21. 9.) — kdyz je mozek
                    # dole, rutiny zavisle na LLM zaostavaji NUTNE a neni to
                    # jejich vada: v noci spi PC a `curiosity_tick` (perioda
                    # 4 h) pretece jeste pred svitanim. Hlidac na to poslal
                    # zpravu 20. 9. 05:25 i 21. 9. 01:38 — obe do tiche fronty,
                    # takze uzivateli prisly rano jako poplach na neco, co se
                    # mezitim samo srovnalo. ZMERENO na historii hlaseni: obe
                    # falesna maji mozek DOLE, jediny pravy poplach (19. 9.
                    # 16:24, agent_action 50 h) ho ma NAHORE a projde dal.
                    # 🔑 Hrana se schvalne NEKONZUMUJE — kdyz rutina visi
                    # doopravdy, ohlasi se, jakmile je mozek zpatky. Tim se
                    # poplach jen ODKLADA, nezahazuje.
                    _brain_down = oll in (hans_health.DOWN, hans_health.WEDGED)
                    # HANS_SCHEDULE_NOTIFY_GRACE_V1 (30. 9.) — po návratu mozku
                    # dát rutinám čas doběhnout. Doloženo 30. 9.: PC v noci
                    # vypnuté → study_tick „zaostává“, hlášení čekalo na mozek
                    # a odešlo v 03:01:03, HNED po WOL; studium proběhlo úspěšně
                    # v 03:03 a ráno přišel poplach na nic. Hrana se ani tady
                    # nekonzumuje — co visí i po lhůtě, ohlásí se.
                    _ted = time.time()
                    if _brain_down:
                        self._health_brain_down_seen = True
                    elif getattr(self, '_health_brain_down_seen', False):
                        self._health_brain_down_seen = False
                        self._health_brain_up_ts = _ted
                    _lhuta = float((self.config.get('hans_schedule', {}) or {})
                                   .get('notify_grace_after_brain_s', 1800))
                    if _ted - getattr(self, '_health_brain_up_ts', 0.0) < _lhuta:
                        _brain_down = True       # → větev „čeká“, hrana zůstane
                    _sched = (health.get('schedule') or {}).get('stale') or []
                    _sched_key = tuple(sorted(s['name'] for s in _sched))
                    # HANS_SCHEDULE_NOTIFY_PERSIST_V1 (5. 10.) — hrana přežije restart:
                    # dřív žila jen v paměti, takže každý restart ohlásil tutéž
                    # množinu zaostávajících rutin znovu (5. 10. sedmkrát za den).
                    if not hasattr(self, '_health_last_sched'):
                        self._health_last_sched = self._sched_notified_load()
                    _sched_prev = self._health_last_sched
                    if _sched_key and _brain_down:
                        # Stopa na HRANE (tyz vzor jako log degradovanych
                        # sluzeb vys): jednou pri vzniku, pak ticho. Bez ni
                        # by odlozeny poplach nesel dohledat — a prave to je
                        # jediny zaznam o tom, ze hlidac nemlci kvuli vade.
                        if _sched_key != getattr(self, '_health_sched_muted', ()):
                            _rt._log.info('health: rozvrh zaostava (%s), ale mozek '
                                      'je dole — hlaseni ceka na mozek',
                                      ', '.join(_sched_key))
                        self._health_sched_muted = _sched_key
                    else:
                        # HANS_SCHEDULE_NOTIFY_PERSIST_V1 — hlásí se jen NOVĚ zaostávající
                        # rutina; když se množina jen zmenší (jedna doběhla), mlčí se.
                        if _sched_key and not set(_sched_key) <= set(_sched_prev):
                            _veta = hans_health._schedule_sentence(health)
                            if _veta and self._notifier:
                                self._notifier(_veta)
                                _rt._log.info('health: rozvrh ohlasen uzivateli — %s',
                                          _veta)
                            elif _veta:
                                _rt._log.warning('health: rozvrh zaostava, ale most '
                                             'chybi — NEODESLANO: %s', _veta)
                        if _sched_key != self._health_last_sched:
                            self._sched_notified_save(_sched_key)
                        self._health_last_sched = _sched_key
                except Exception as _se:
                    _rt._log.debug('health: hlaseni rozvrhu: %s', _se)
            except Exception as _e:
                _rt._log.debug('health watcher: %s', _e)
            # COMFY_RECLAIM_PERIODIC_V1 (14.8.) — pojistka na zaseklý runlist.
            # Úklid po renderu (COMFY_GPU_RECLAIM_V1) pokrývá běžný případ, ale
            # nemusí doběhnout: restart Hanse uprostřed renderu, výpadek SSH při
            # měření, nebo plná fronta ComfyUI (tam ustupuje schválně). Pak by
            # grafika visela na 99 % do dalšího malování a stála ~40 W navíc
            # (změřeno: klid 6 W × zaseklý runlist 45-49 W). Tady se to dorovná
            # nejpozději za `check_interval_s`. Sdílí TÝŽ kód, jen bez čekání na
            # usazení — po deseti minutách je stav dávno ustálený.
            try:
                from scripts.avatar_render import _comfy_reclaim_gpu
                if _comfy_reclaim_gpu(self.config, after_render=False):
                    _rt._log.info('health: uvolnil jsem zaseklou grafiku '
                              '(fronty po renderu)')
            except Exception as _ge:
                _rt._log.debug('health: reclaim GPU: %s', _ge)
            if self._stop.wait(self._health_interval):
                break

    def _sched_notified_load(self) -> tuple:
        """HANS_SCHEDULE_NOTIFY_PERSIST_V1 — naposledy ohlášená množina
        zaostávajících rutin. Starší než `hans_schedule.notify_repeat_h`
        (24 h) se nebere → co visí déle, připomene se po restartu znovu."""
        try:
            with open(self._SCHED_NOTIFIED, encoding="utf-8") as f:
                d = json.load(f)
            _h = float((self.config.get('hans_schedule', {}) or {})
                       .get('notify_repeat_h', 24))
            if time.time() - float(d.get('ts') or 0) > _h * 3600:
                return ()
            return tuple(d.get('rutiny') or ())
        except Exception:
            return ()

    def _sched_notified_save(self, key) -> None:
        try:
            with open(self._SCHED_NOTIFIED, "w", encoding="utf-8") as f:
                json.dump({'ts': time.time(), 'rutiny': list(key)}, f)
        except Exception as _e:
            _rt._log.debug('sched notified save: %s', _e)

    def _drain_notify_queue(self, path: str = "data/notify_queue.jsonl"):
        """HANS_NOTIFY_QUEUE_V1 — pošli zprávy, které do fronty zapsal skript
        běžící MIMO Hansův proces (systemd timer, ruční nástroj).

        Proč fronta a ne přímé odeslání: Matrix E2E store snese jen jednoho
        klienta (`hans_matrix.py:16`), takže cizí proces nesmí založit
        vlastní nio session — poškodil by olm klíče. Tudy jde zpráva
        Hansovým existujícím mostem, tedy ŠIFROVANĚ.

        Fronta se maže AŽ PO úspěšném odeslání: když most zrovna neběží
        (mozek dole, start), zpráva počká na další tick místo ztráty
        ([[ollama-deferred-processing]]).
        """
        import json
        import os
        if not self._notifier:
            return
        try:
            if not os.path.exists(path) or os.path.getsize(path) == 0:
                return
            with open(path, "r", encoding="utf-8") as f:
                lines = [ln.strip() for ln in f if ln.strip()]
            if not lines:
                return
            kept = []
            for ln in lines:
                direct = False
                try:
                    _rec = json.loads(ln) or {}
                    txt = _rec.get("text") or ""
                    # HANS_NOTIFY_DIRECT_V1 (7.8.) — `direct` = tohle NENÍ Hansův
                    # nápad, ale VÝSLEDEK toho, oč uživatel právě požádal a na co
                    # čeká. Tiché hodiny takovou zprávu odložily do 9:00 (doloženo
                    # 6.8. 22:38 u nepovedeného obrazu) → slib zase visel.
                    direct = bool(_rec.get("direct"))
                except Exception:
                    txt = ln           # holý text taky bereme
                if not txt:
                    continue
                try:
                    # HANS_NOTIFY_DIAG_V1 — notifier vrací False, když most
                    # chybí / je vypnutý / odmítl (tiché hodiny). Dřív se tu
                    # návratová hodnota ignorovala → fronta se vyprázdnila
                    # a zpráva se ZTRATILA, přestože log hlásil „odesláno".
                    # HANS_NOTIFY_DIRECT_V1 — starší notifier `direct` neumí;
                    # pak pošli postaru (zpráva je důležitější než příznak).
                    try:
                        _sent = self._notifier(txt, direct=direct)
                    except TypeError:
                        _sent = self._notifier(txt)
                    if _sent is False:
                        _rt._log.warning("HANS_NOTIFY_QUEUE_V1: neodesláno — "
                                     "nechávám ve frontě na další tick")
                        kept.append(ln)
                        continue
                    _rt._log.info("HANS_NOTIFY_QUEUE_V1: odesláno (%d zn)", len(txt))
                except Exception as e:
                    _rt._log.warning("notify queue: odeslání selhalo (%s) — "
                                 "nechávám ve frontě", e)
                    kept.append(ln)
            if kept:
                with open(path, "w", encoding="utf-8") as f:
                    f.write("\n".join(kept) + "\n")
            else:
                os.remove(path)
        except Exception as e:
            _rt._log.warning("notify queue selhala: %s", e)

    def _maybe_calendar_sync(self):
        """HANS_CALENDAR_V1 — stáhne ICS feed (throttle sync_interval_min).
        Běží ve vlákně, ať síťový fetch neblokuje tick. No-op když vypnuto."""
        try:
            cc = (self.config.get("calendar", {}) or {})
            from scripts.hans_calendar import is_enabled as _cal_enabled
            if not _cal_enabled(self.config):  # per-osoba (people map)
                return
            interval = int(cc.get("sync_interval_min", 30)) * 60
            last = getattr(self, "_last_cal_sync", 0.0)
            now = time.time()
            if now - last < interval:
                return
            self._last_cal_sync = now
            import threading

            def _do():
                try:
                    from scripts.hans_calendar import CalendarStore
                    CalendarStore(self.config, self._diary_path).sync()
                    from scripts import hans_schedule  # HANS_SCHEDULE_V1
                    hans_schedule.mark('calendar_sync')
                except Exception as e:
                    _rt._log.warning("calendar sync selhal: %s", e)
            threading.Thread(target=_do, daemon=True).start()
        except Exception:
            pass

    def _maybe_prevence_sber(self):
        """HANS_PREVENTION_V1 — jednou za hodinu, ve vlastním vlákně (SSH na PC
        smí čekat, tick ne)."""
        now = time.time()
        if now - getattr(self, "_last_prevence", 0.0) < 3600:
            return
        self._last_prevence = now
        import threading

        def _do():
            from scripts import hans_prevence
            hans_prevence.sber(self.config, self._diary_path)
        threading.Thread(target=_do, daemon=True, name="prevence").start()

    def _maybe_webshare_presun(self):
        """HANS_WEBSHARE_PRESUN_TICK_V1 — hotové stažení přesuň z Pi na PC.

        ⚠️ Levné, když není co dělat: `hotove_ke_presunu()` je jen pohled do
        JSON souboru a `os.path.exists`, žádné SSH. Teprve když něco čeká, sáhne
        se na PC. Tick běží často, takže dotaz po síti v každém kole by byl
        zbytečná zátěž.
        ⚠️ PC se kvůli přesunu NEBUDÍ — když spí, soubor na Pi počká.
        """
        try:
            from scripts import hans_webshare as _ws
            # HANS_WEBSHARE_RESUME_V1 — zastavené stahování naváž (1× za minutu)
            if time.time() - getattr(self, "_ws_hlidac_ts", 0) >= 60:
                self._ws_hlidac_ts = time.time()
                for _z in _ws.hlidej_stahovani(self.config):
                    _rt._log.info("webshare: %s", _z)
            if not _ws.hotove_ke_presunu():
                return
            for zprava in _ws.presun_hotove(self.config):
                _rt._log.info("webshare: %s", zprava)
                # HANS_WEBSHARE_NOTIFIER_FIX_V1 — `self.notify()` na téhle třídě
                # NEEXISTUJE (napsal jsem ho z hlavy; grep ukázal, že jediný
                # výskyt byl ten můj) a volání by tiše spadlo do `except`.
                # Proaktivní zprávy chodí přes callback `_notifier` — týž vzor
                # jako u Severky (ř. 666) a směru (ř. 690).
                if self._notifier:
                    try:
                        self._notifier(zprava)
                    except Exception as _ne:
                        _rt._log.warning("webshare notifier selhal: %s", _ne)
        except Exception as e:
            _rt._log.debug("webshare presun tick: %s", e)

    def _maybe_retry_paint(self):
        """HANS_ART_RETRY_V1 (24. 9.) — dlužné obrazy (slib kind='paint' od
        `paint_subject(zadal=…)` nebo když ComfyUI spalo) zkusí namalovat
        á 10 min, JEN když je mozek, dílna a nehraje se. Hotový obraz pošle
        hned na Matrix s fotkou (ne až při zahlédnutí kamerou). 3 nezdary →
        poctivá zpráva a konec. Render běží ve vlákně (trvá minuty)."""
        import threading as _th
        import time as _t
        if _t.time() - getattr(self, "_retry_paint_ts", 0.0) < 600:
            return
        self._retry_paint_ts = _t.time()
        if getattr(self, "_retry_paint_busy", False):
            return
        try:
            from scripts.hans_commitments import open_paint_retries
            rows = open_paint_retries(self._diary_path)
        except Exception:
            return
        if not rows:
            return
        try:
            from scripts.ollama_client import game_mode_on
            from scripts import hans_art
            if game_mode_on() or not self._brain_up() \
                    or not hans_art.comfy_available(self.config):
                return
            # HANS_HEAVY_QUEUE_V1 — táž kontrola zdrojů a TENTÝŽ zámek jako
            # fronta náročných úloh: dvě náročné věci nikdy naráz
            from scripts import hans_heavy_queue as _hq
            if not _hq.zdroje_volne(self.config, "paint")[0]:
                return
        except Exception:
            return
        cid, person, topic, tries, _styl = rows[0]

        def _run():
            from scripts import hans_heavy_queue as _hq
            if not _hq.ZAMEK.acquire(blocking=False):
                return
            self._retry_paint_busy = True
            try:
                import sqlite3 as _sq
                from scripts.hans_commitments import (_fulfill_paint,
                                                      mark_reported)
                zavreno = _fulfill_paint(self.config, self._diary_path, cid,
                                         topic, int(tries or 0))
                if not zavreno:
                    _rt._log.info("HANS_ART_RETRY_V1: „%s“ zatím nevyšel (pokus %d)",
                              topic, int(tries or 0) + 1)
                    return
                _d = _sq.connect(self._diary_path, timeout=5.0)
                _r = _d.execute("SELECT result FROM commitments WHERE id=?",
                                (cid,)).fetchone()
                _d.close()
                vysl = (_r[0] or "") if _r else ""
                import os as _os
                if vysl and _os.path.exists(vysl):
                    text = ("Slíbený obraz na téma „%s“ je hotový — tady je." % topic)
                    foto = vysl
                else:
                    text = ("Obraz na téma „%s“ se mi ani na třetí pokus nepodařilo "
                            "namalovat. Omlouvám se — zkusíte mi ho zadat znovu?" % topic)
                    foto = None
                ok = False
                if self._notifier:
                    try:
                        ok = self._notifier(text, direct=True, photo=foto)
                    except TypeError:
                        ok = self._notifier(text)
                if ok is not False:
                    mark_reported(self._diary_path, [cid])
                _rt._log.info("HANS_ART_RETRY_V1: „%s“ → %s, doručeno=%s",
                          topic, "obraz" if foto else "vzdáno", ok)
            except Exception as e:
                _rt._log.warning("HANS_ART_RETRY_V1: %s", e)
            finally:
                self._retry_paint_busy = False
                _hq.ZAMEK.release()
        _th.Thread(target=_run, daemon=True, name="art-retry").start()

# ROZDELENI_METOD_V1 — až na konci, viz hlavička
from scripts import hans_routine as _rt  # noqa: E402
