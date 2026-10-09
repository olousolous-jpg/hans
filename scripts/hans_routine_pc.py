"""Metody třídy `HansRoutine` přesunuté z `scripts/hans_routine.py` (ROZDELENI_METOD_V1).

Text metod je beze změny; jména původního modulu se čtou přes `_rt.` až při
volání. `HansRoutine` má třídu `PcMixin` mezi předky, takže volání přes `self` platí dál.
Import původního modulu je na KONCI souboru (kruhový import oběma směry).
"""
from __future__ import annotations

from datetime import datetime, timedelta
import time


class PcMixin:
    """Část třídy `HansRoutine` — viz hlavička modulu."""

    # ─── WOL_ON_PRESENCE_V1 ───────────────────────────────────────────
    def wake_pc_on_presence(self):
        """Osoba přišla → pokud PC spí, vzbuď ho (teplá Ollama na chat).
        Throttle (wol_presence_cooldown_min) + neblokující. Volá person_seen."""
        if not self._wol_pc_enabled or not self._wol_on_presence or not self._wol_pc_mac:
            return
        import time as _t
        # WOL_NO_WAKE_ON_RESTART_V1 — přítomnost hned po startu Hanse nezapíná PC
        # (jinak by restart + někdo v místnosti = probuzení). Denní presence
        # po uplynutí lhůty funguje normálně.
        if (_t.time() - getattr(self, "_wol_boot_ts", 0)) < getattr(
                self, "_wol_startup_grace_s", 300):
            return
        now = _t.time()
        cd = float(self.config.get("hans_routine", {}).get(
            "wol_presence_cooldown_min",
            self.config.get("wol_presence_cooldown_min", 15))) * 60.0
        if (now - self._wol_presence_last) < cd:
            return
        self._wol_presence_last = now
        import threading
        threading.Thread(target=self._wol_presence_flow, daemon=True).start()

    def _wol_presence_flow(self):
        """Ping PC; offline → packet + verify. Daemon thread."""
        import time as _t
        try:
            ip, mac = self._wol_pc_ip, self._wol_pc_mac
            if self._ping_host(ip):
                return  # PC běží
            _rt._log.info("WOL(presence): %s offline → packet (osoba přišla)", ip)
            self._send_wol_packet(mac)
            for _i in range(8):
                _t.sleep(15)
                if self._ping_host(ip):
                    _rt._log.info("WOL(presence): %s ONLINE %ds po packetu",
                              ip, (_i + 1) * 15)
                    break
        except Exception as _e:
            _rt._log.warning("WOL(presence) flow: %s", _e)

    # ─── WOL_WAKE_PC_V1 ───────────────────────────────────────────────
    def _wol_timer_loop(self):
        """WOL_POLL_LOOP_V1 — robustni poller (nahradil krehky one-shot sleep,
        ktery tise nefiroval). Kazdych POLL_S rano (pred sleep_end_hour) zavola
        _maybe_wake_pc; ta sama hlida okno [start_dt, ..) + 1x/den guard."""
        import time as _t
        from datetime import datetime as _dt
        _rt._log.info("WOL: poll thread started (sleep_end_hour=%s, lead=%dm)",
                  self._sleep_end_hour, self._wol_min_before)
        POLL_S = 600  # 10 min
        while True:
            try:
                now = _dt.now()
                if now.hour < self._sleep_end_hour:
                    self._maybe_wake_pc(now)
            except Exception as _e:
                _rt._log.warning("WOL poll loop: %s", _e)
            _t.sleep(POLL_S)

    def _maybe_wake_pc(self, now):
        """Triggered z _check_sleep_window. Spawn WOL flow asynchronně."""
        from datetime import timedelta as _td
        wake_dt = now.replace(hour=self._sleep_end_hour,
                              minute=0, second=0, microsecond=0)
        start_dt = wake_dt - _td(minutes=self._wol_min_before)
        # WOL_WINDOW_FIX_V1 — dropnuta horní mez (now < wake_dt): řídký sleep-tik
        # úzké okno míjel. Fírni na 1. tiku po start_dt (1x/den guard níž;
        # caller hlídá _sleeping → po probuzení už nefírne).
        if now < start_dt:
            return
        today = now.date()
        if self._wol_last_date == today:
            return  # už dnes spuštěno
        self._wol_last_date = today
        _rt._log.info('WOL: trigger in window — spawning wake_pc')
        import threading
        threading.Thread(target=self._wol_wake_pc, daemon=True).start()

    def _wol_wake_pc(self):
        """Ping → wait → ping → WOL packet. Běží v daemon thread."""
        import time as _t
        try:
            ip = self._wol_pc_ip
            mac = self._wol_pc_mac
            if not mac:
                _rt._log.warning('WOL: MAC nenastaveno, skip')
                return
            if self._ping_host(ip):
                _rt._log.info('WOL: %s online (ping 1) — skip', ip)
                return
            _rt._log.info('WOL: %s offline (ping 1), čekám 60s', ip)
            _t.sleep(60)
            if self._ping_host(ip):
                _rt._log.info('WOL: %s online (ping 2) — skip', ip)
                return
            _rt._log.info('WOL: %s pořád offline, posílám magic packet', ip)
            self._send_wol_packet(mac)
            # WOL_VERIFY_V1 — po packetu ověř probuzení (poll ~2 min), ať je WOL z logů verifikovatelný
            for _i in range(8):
                _t.sleep(15)
                if self._ping_host(ip):
                    _rt._log.info('WOL: %s ONLINE %ds po packetu — probuzeno', ip, (_i + 1) * 15)
                    self._wol_online_ts = _t.time()  # HANS_HEALTH_NIGHT_AWARE_V1
                    break
            else:
                _rt._log.warning('WOL: %s pořád offline 120s po packetu — probuzení se nezdařilo?', ip)
        except Exception as _e:
            _rt._log.error('WOL: wake_pc failed: %s', _e)

    @staticmethod
    def _ping_host(ip, timeout=2):
        """True pokud ping prošel."""
        import subprocess
        if not ip:
            return False
        try:
            r = subprocess.run(['ping', '-c', '1', '-W', str(timeout), ip],
                               capture_output=True, timeout=timeout + 1)
            return r.returncode == 0
        except Exception:
            return False

    @staticmethod
    def _send_wol_packet(mac):
        """Pošli WOL magic packet — sdílená implementace (HANS_WOL_SHARED_V1)."""
        from scripts import pc_remote
        if pc_remote.wake(mac=mac):
            _rt._log.info('WOL: magic packet sent to %s', mac)
        else:
            raise ValueError('Invalid MAC: %r' % mac)

    def _pc_up(self):
        """Rychlá kontrola, jestli PC běží (ping, ~2s)."""
        try:
            return self._ping_host(self._wol_pc_ip)
        except Exception:
            return False

    def _maybe_wake_for_analytics(self):
        """HANS_PC_NIGHT_ANALYTICS_WAKE — v noční hodinu probuď PC (když je
        vypnutý) pro noční analytiku. Analytika pak proběhne (_run_night_tasks)
        a _maybe_shutdown_pc PC zase vypne. 1×/noc. Když PC běží / analytika
        dnes už proběhla → nebudí."""
        try:
            c = (self.config.get("pc_night_shutdown", {}) or {})
            if not c.get("enabled") or not c.get("wake_for_analytics", True):
                return
            now = datetime.now()
            today = now.strftime("%Y-%m-%d")
            if self._last_analytics_wake_date == today:
                return
            if now.hour != int(c.get("analytics_hour", 3)):
                return
            import time as _t
            nowts = _t.time()
            # HANS_NIGHT_WAKE_GATE_V2 — buď JEN když v okně 2-6 zbývá práce
            # potřebující mozek (těžká analytika NEBO studium), ne jen podle
            # „analytika za 6h". Dřív: večerní syntéza (běží s mozkem nahoře,
            # ~22-01) byla ve 3:00 stará <6h → gate usoudil „hotovo, nebudit"
            # → PC se neprobudil a studium/drain ve 2-6 hladověly.
            pending = self._night_work_pending(nowts)
            self._last_analytics_wake_date = today
            self._analytics_wake_ts = nowts
            self._save_routine_state()
            if not pending:
                return  # noční mozková práce hotová → nebuď (šetři proud)
            if self._pc_up():
                return  # PC běží → práce proběhne sama
            _rt._log.info("PC night wake: budím PC pro noční práci (analytika/studium)")
            self._send_wol_packet(self._wol_pc_mac)
            # HANS_PC_NIGHT_RESHUTDOWN_V1 — buzení otevírá nový cyklus: když se
            # PC ten den už jednou vypínal (noční práce doběhla až po půlnoci,
            # typicky po hře), strážce 1×/den v _maybe_shutdown_pc by ho po
            # tomhle probuzení nechal běžet do rána (8. 10.: 3:00–6:40).
            if self._last_pc_shutdown_date == today:
                self._last_pc_shutdown_date = ""
                self._save_routine_state()
                _rt._log.info("HANS_PC_NIGHT_RESHUTDOWN_V1: PC se dnes už vypínal "
                          "→ po nočním probuzení smí vypnutí proběhnout znovu")
            for _ in range(9):  # čekej na náběh z S5 (~40-90s)
                _t.sleep(10)
                if self._pc_up():
                    _rt._log.info("PC night wake: PC naběhl → noční práce poběží")
                    return
            _rt._log.warning("PC night wake: PC nenaběhl do 90s")
        except Exception as _e:
            _rt._log.warning("pc_night_wake: %s", _e)

    def _maybe_start_night_catchup(self):
        """HANS_NIGHT_DAY_CATCHUP_V1 (5. 10.) — noc, ve které po půlnoci nebyl
        mozek ani jednou k dispozici (PC nenaběhl: výpadek proudu, selhané
        buzení), se dožene přes den, jakmile mozek naběhne. Dřív noční úlohy
        běžely jen pod `is_night` a propadlá noc čekala do dalšího večera.

        Po půlnoci jen zapisuje, že mozek byl vidět (= noc měla šanci).
        Přes den (morning_hour..night_hour) otevře 1× za den okno
        `night_catchup_budget_min`; v něm `_run_night_tasks` pustí úlohy
        z `_NIGHT_CATCHUP_TASKS`. Jejich vlastní pojistky (datum, kadence,
        klid v místnosti, herní mód) platí beze změny."""
        try:
            _rcfg = self.config.get("hans_routine", {}) or {}
            if not _rcfg.get("night_catchup_enabled", True):
                return
            now = datetime.now()
            today = now.strftime("%Y-%m-%d")
            if now.hour < self._morning_hour:
                if (getattr(self, "_night_brain_date", "") != today
                        and self._brain_up()):
                    self._night_brain_date = today
                    self._save_routine_state()
                return
            if now.hour >= self._night_hour:
                return                  # večerní okno → běžný noční tick
            if (getattr(self, "_night_brain_date", "") == today
                    or getattr(self, "_night_catchup_date", "") == today):
                return
            from scripts.ollama_client import game_mode_on
            if game_mode_on() or not self._brain_up():
                return
            _min = float(_rcfg.get("night_catchup_budget_min", 120))
            self._night_catchup_date = today
            self._night_catchup_until = time.time() + 60.0 * _min
            self._save_routine_state()
            _rt._log.info("HANS_NIGHT_DAY_CATCHUP_V1: noc proběhla bez mozku → "
                      "doháním noční práci přes den (okno %.0f min)", _min)
        except Exception as _e:
            _rt._log.warning("night catchup: %s", _e)

    def _night_work_pending(self, nowts) -> bool:
        """HANS_NIGHT_WAKE_GATE_V2 — zbývá v nočním okně (2-6) práce, co
        potřebuje mozek? True když (a) těžká analytika (synthesis/self_critique)
        neproběhla za 6h, NEBO (b) studium dnes neproběhlo a je aktivní program.
        Řídí buzení ve 3:00 (dřív gate jen na analytiku → večerní syntéza si
        sama zablokovala buzení). Chyba čtení = fail-open (radši vzbudit)."""
        today = datetime.now().strftime("%Y-%m-%d")
        # (a) těžká reasoning analytika
        try:
            import sqlite3 as _sql
            conn = _sql.connect("file:%s?mode=ro" % self._diary_path,
                                uri=True, timeout=3.0)
            ph = ",".join("?" * len(self._HEAVY_ANALYTICS_EVENTS))
            ra = conn.execute(
                "SELECT MAX(ts) FROM diary WHERE event_type IN (%s) AND ts >= ?"
                % ph, (*self._HEAVY_ANALYTICS_EVENTS, nowts - 16 * 3600)
            ).fetchone()
            conn.close()
            if not (ra and ra[0] and nowts - ra[0] < 6 * 3600):
                return True  # analytika ještě nebyla → je co dělat
        except Exception:
            return True  # radši vzbudit než tiše vynechat
        # (b) studium — dnes neproběhlo a je co studovat
        try:
            if (self._last_study_date != today
                    and (self.config.get("study", {}) or {}).get("enabled", True)):
                from scripts.hans_study import StudyStore
                if StudyStore(self.config, self._diary_path).get_active_program():
                    return True
        except Exception:
            pass
        # (c) maker — dokončený program bez artefaktu pro aktuální kolo prohloubení
        # (dílo studium→artefakt taky potřebuje mozek v okně 2-6; bez tohohle by
        # se PC neprobudil a dílo by nevzniklo, i když je co vyrobit)
        try:
            if (self.config.get("maker", {}) or {}).get("auto", True):
                from scripts import hans_maker as _mk
                import sqlite3 as _sq
                _c = _sq.connect("file:%s?mode=ro" % self._diary_path,
                                 uri=True, timeout=3.0)
                _comp = _c.execute("SELECT topic, deepen_round FROM study_program "
                                   "WHERE status='completed'").fetchall()
                _c.close()
                for _tp, _rnd in _comp:
                    if not _mk.has_artifact_for_round(self._diary_path, _tp,
                                                      int(_rnd or 0)):
                        return True
        except Exception:
            pass
        return False

    def _render_pending_art_before_shutdown(self):
        """HANS_ART_NIGHT_RENDER_V1 — dorenderuj pending art, dokud je PC vzhůru
        (analytický wake, VRAM volná po doběhnuté analytice), NEŽ ho vypneme.
        Jinak art nemá PC-up okno: PC je přes noc dole a ranní WOL je až za
        night window. Book art přednost, jinak sen. Blokující (poweroff počká).
        Nikdy nevyhodí — selhání = jen retry příští noc (deferral)."""
        painted = False
        try:
            from scripts.hans_art import generate_pending_artwork
            painted = generate_pending_artwork(self.config, self._diary_path)
        except Exception as _e:
            _rt._log.warning("pre-shutdown art (kniha): %s", _e)
        if not painted:
            try:
                from scripts.hans_art import paint_dream
                painted = paint_dream(self.config, self._diary_path)
            except Exception as _e:
                _rt._log.warning("pre-shutdown art (sen): %s", _e)
        if not painted:  # HANS_ART_AFTER_DRAIN_V1 — plná sekvence jako night-tick
            try:
                from scripts.hans_creations import creative_impulse
                creative_impulse(self.config, self._diary_path)
            except Exception as _e:
                _rt._log.warning("pre-shutdown art (tvůrčí impuls): %s", _e)

    def _maybe_shutdown_pc(self):
        """HANS_PC_NIGHT_SHUTDOWN — po dokončení noční analytiky vypni PC.
        S3 suspend je na téhle desce rozbitý (reboot); čistý poweroff + ranní
        WOL. Guardy: hluboké noční okno, analytika usazená (žádný event N min),
        žádný recent chat, PC vzhůru, 1× za noc."""
        try:
            c = (self.config.get("pc_night_shutdown", {}) or {})
            if not c.get("enabled"):
                return
            from scripts import pc_remote
            if not pc_remote.enabled(self.config):
                return
            now = datetime.now()
            today = now.strftime("%Y-%m-%d")
            if self._last_pc_shutdown_date == today:
                return
            h0 = int(c.get("night_start_hour", 2))
            h1 = int(c.get("night_end_hour", 6))
            if not (h0 <= now.hour < h1):
                return
            import sqlite3 as _sql
            import time as _t
            nowts = _t.time()
            conn = _sql.connect("file:%s?mode=ro" % self._diary_path,
                                uri=True, timeout=3.0)
            # recent chat → nevypínej
            chat_q = int(c.get("chat_quiet_minutes", 30)) * 60
            rc = conn.execute(
                "SELECT MAX(ts) FROM diary WHERE event_type='human_chat'"
            ).fetchone()
            if rc and rc[0] and nowts - rc[0] < chat_q:
                conn.close()
                return
            # analytika usazená? poslední OHRANIČENÁ noční práce > settle_minutes
            # (HANS_SHUTDOWN_SETTLE_FIX_V1 — bez ambient introspekce, jinak
            # nikdy nesettle a PC se nevypne).
            settle = int(c.get("settle_minutes", 20)) * 60
            ph = ",".join("?" * len(self._SHUTDOWN_SETTLE_EVENTS))
            ra = conn.execute(
                "SELECT MAX(ts) FROM diary WHERE event_type IN (%s) "
                "AND ts >= ?" % ph,
                (*self._SHUTDOWN_SETTLE_EVENTS, nowts - 16 * 3600)).fetchone()
            conn.close()
            if ra and ra[0] and nowts - ra[0] < settle:
                return  # analytika ještě běží
            # HANS_NIGHT_DRAIN_V1 — po analytickém wake drž PC nahoře aspoň
            # wake_drain_minutes, ať tick dojede ODLOŽENÝ backlog (catchup,
            # studium, self_insight) + art. Jinak vypne po ~2 min a zbytek se
            # odloží na ráno = škoda probuzení/proudu. Jen když jsme DNES budili.
            drain_s = int(c.get("wake_drain_minutes", 15)) * 60
            if (drain_s > 0 and self._last_analytics_wake_date == today
                    and self._analytics_wake_ts
                    and nowts - self._analytics_wake_ts < drain_s):
                return  # ještě dojíždíme deferred backlog
            # NEvypni PŘED analytikou: povol shutdown jen když analytika DNES
            # proběhla, NEBO jsme kvůli ní budili a dali jí čas (settle) doběhnout.
            had_today = bool(ra and ra[0] and nowts - ra[0] < 16 * 3600)
            woke_settled = (self._last_analytics_wake_date == today
                            and self._analytics_wake_ts
                            and nowts - self._analytics_wake_ts >= settle)
            if not (had_today or woke_settled):
                return  # analytika ještě neproběhla (čekáme na wake+běh)
            # PC vzhůru? (rychlý ping — když dole, není co vypínat; guard NEnastavuj)
            if not self._pc_up():
                return
            # HANS_SHUTDOWN_WAIT_WORK_V1 (14. 9.) — i noční vypnutí čeká, až
            # Hans dodělá práci, a to OPAKOVANĚ po sobě (klid ve chvíli kontroly
            # bývá mezera mezi úlohami). Doloženo 14. 9.: PC vypnuto 04:06:21,
            # toolscout 04:07:24 už na vypnutém stroji. Jedna pravda o „pracuje"
            # s povelem uživatele: `pc_deferred_shutdown.pc_busy`.
            _need = int(c.get("idle_checks", 3))
            try:
                from scripts.pc_deferred_shutdown import pc_busy as _pc_busy
                _busy, _why = _pc_busy(self.config)
            except Exception as _be:
                _busy, _why = True, "stav nezjištěn (%s)" % _be
            if _busy:
                if (getattr(self, "_night_idle_hits", 0)
                        or getattr(self, "_night_busy_logged", "") != today):
                    self._night_busy_logged = today
                    _rt._log.info("PC night shutdown: čekám, až dodělám práci — %s",
                              _why)
                self._night_idle_hits = 0
                return
            self._night_idle_hits = getattr(self, "_night_idle_hits", 0) + 1
            if self._night_idle_hits < _need:
                return
            self._night_idle_hits = 0
            # HANS_ART_NIGHT_RENDER_V1 — poslední spolehlivé PC-up okno v noci:
            # dorenderuj pending art PŘED vypnutím (jinak 3 noci sucho).
            try:
                self._render_pending_art_before_shutdown()
            except Exception as _ae:
                _rt._log.warning("PC night shutdown: art render selhal: %s", _ae)
            # vypni
            pc_remote.run(self.config, "sudo -n systemctl poweroff", timeout=10)
            self._last_pc_shutdown_date = today
            import time as _t
            self._pc_shutdown_ts = _t.time()  # HANS_HEALTH_NIGHT_AWARE_V1
            self._save_routine_state()
            _rt._log.info("PC night shutdown: analytika hotová → PC vypnut "
                      "(ranní WOL probudí)")
            try:
                conn2 = _sql.connect(self._diary_path, timeout=5.0)
                conn2.execute(
                    "INSERT INTO diary (ts, event_type, title, note) "
                    "VALUES (?,?,?,?)",
                    (nowts, "pc_shutdown", "Vypnutí PC na noc",
                     "Noční analytika dokončena — vypnul jsem počítač; "
                     "ráno ho probudím."))
                conn2.commit()
                conn2.close()
            except Exception:
                pass
        except Exception as _e:
            _rt._log.warning("pc_night_shutdown: %s", _e)

    def _pc_planned_unavailable(self) -> bool:
        """HANS_HEALTH_NIGHT_AWARE_V1 — je PC ZÁMĚRNĚ dole (noční shutdown) nebo
        se právě probouzí (WOL boot grace)? Pak výpadek Ollamy/PC je OČEKÁVANÝ,
        ne porucha → nehlásit „mozek se zasekl", nehealit, warning→debug. Váže se
        na skutečný cyklus (shutdown↔WOL), NE na hodiny (ranní WOL je po
        morning_hour). Pozn.: timestampy nejsou persistované → po restartu Hanse
        uprostřed noci se suppression ztratí (jen víc log-šumu, ne falešná
        hláška — DOWN≠WEDGED, notify nefíruje)."""
        import time as _t
        now = _t.time()
        sd = getattr(self, '_pc_shutdown_ts', 0.0)
        wo = getattr(self, '_wol_online_ts', 0.0)
        if sd and sd > wo:
            return True  # naposledy jsme PC vypnuli, WOL ho ještě nevrátil → dole
        if wo and (now - wo) < getattr(self, '_wol_startup_grace_s', 300):
            return True  # čerstvě probuzeno → Ollama bootuje
        return False

# ROZDELENI_METOD_V1 — až na konci, viz hlavička
from scripts import hans_routine as _rt  # noqa: E402
