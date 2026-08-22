#!/usr/bin/env python3
"""
EyeServoController — animatronická serva OČÍ (HW přes robot_hat).

Oddělené od:
  • kamerových serv (ServoController, P0/P1)
  • displejových očí (Eye_sphere / LCD koule)

Návrh chování (uživatel 23.6.): OČI vedou, kamera dohání.
  • Oči sledují střed bboxu osoby v rámu (rychle, každý snímek).
  • Kamera (P0/P1) se hne jen když je osoba na kraji rámu → vycentruje →
    oči se samy vrátí na střed (sledují stejný bbox, který je teď uprostřed).

Kalibrace z eye_calibration.json:
  channels: {pan: P2, tilt: P3}
  pan/tilt: {center, min, max}   (úhly ve stupních, asymetrie OK)

API:
  eyes = EyeServoController(config)
  eyes.available            # True když HW + povoleno
  eyes.look_at_frac(cx, cy) # cx,cy v 0..1 (střed bboxu v rámu) → pohyb očí
  eyes.center()             # oči na kalibrovaný střed
"""
from __future__ import annotations

import json
import logging
import random
import threading
import time
from pathlib import Path

_log = logging.getLogger("eye_servo")

_DEFAULT_CALIB = {
    "channels": {"pan": "P2", "tilt": "P3"},
    "pan":  {"center": 0.0, "min": -30.0, "max": 30.0},
    "tilt": {"center": 0.0, "min": -20.0, "max": 34.0},
    "lids": {},
    # HANS_EYE_GAZE_TILT_OFFSET_V1 — přičte se k tilt úhlu pohledu (kamera je
    # výš než oči → obličej ve středu rámu, ale oči koukají moc nízko → posun
    # nahoru). Laděno v eye_calibration.json (hot-reload, bez restartu).
    "gaze_tilt_offset": 0.0,
}


class EyeServoController:
    def __init__(self, config: dict):
        self.config = config or {}
        cfg = self.config.get("eye_servo", {}) or {}
        self.enabled = bool(cfg.get("enabled", False))
        self._smooth = float(cfg.get("smooth", 0.8))      # EMA (1.0 = bez vyhlazení)
        self._deadband = float(cfg.get("deadband_deg", 0.5))
        self._pan_invert  = bool(cfg.get("pan_invert", False))
        self._tilt_invert = bool(cfg.get("tilt_invert", False))
        # uvolnění serv proti pískání: po idle_release_s bez reálného pohybu →
        # pulse_width(0) → serva limp → ticho. Reálný pohyb je znovu probudí.
        self._idle_release_s = float(cfg.get("idle_release_s", 2.0))
        # HANS_EYE_BLINK_V1 — animatronická víčka (P4-P7) + mrkání stavovým
        # automatem (bez vlákna, bez sleep → neblokuje kameru).
        self._blink_enabled = bool(cfg.get("blink_enabled", True))
        self._blink_min_s = float(cfg.get("blink_min_s", 3.0))
        self._blink_max_s = float(cfg.get("blink_max_s", 9.0))
        self._blink_hold_s = float(cfg.get("blink_hold_s", 0.10))
        # HANS_EYE_BLINK_SMOOTH_V1 — plynulé (rampované) mrknutí = tišší serva.
        # Delší close/open = pomalejší pohyb = méně hluku (uživatel 16.8.).
        self._blink_close_s = float(cfg.get("blink_close_s", 0.30))
        self._blink_open_s = float(cfg.get("blink_open_s", 0.30))
        self._lid_release_s = float(cfg.get("lid_release_s", 0.0))  # 0 = drž (proti průhybu)
        self._lid_servos = {}
        self._lid_open = {}
        self._lid_closed = {}
        self._lids_available = False
        self._blinking = False           # běží vlákno _run_blink?
        self._blink_thread = None
        self._io_lock = threading.Lock() # serializace I2C (pan/tilt × víčka)
        self._next_blink_ts = 0.0
        self._lid_last_move_ts = 0.0
        self._lids_released = False
        self.available = False
        self._pan_s = None
        self._tilt_s = None
        self._pan_ema = None
        self._tilt_ema = None
        self._last_pan = None
        self._last_tilt = None
        self._last_move_ts = 0.0
        self._released = False
        # EYE_SMOOTH_TICK_V1 — pohyb očí je ROZPOJENÝ od snímkové smyčky.
        # Dřív se na serva posílalo přímo z look_at_frac(), tedy jen jednou za
        # DETECT_EVERY snímků: při 41 fps 13×/s, po zpomalení kamery (rotace
        # obrazu, 22.8.) 10×/s → viditelné schody. Navíc EMA v _send byla
        # per-VOLÁNÍ, takže s klesající frekvencí rostla i její časová
        # konstanta — fps tak měnilo i svižnost pohledu. Teď look_at_frac()
        # jen uloží CÍL a k němu dojíždí vlákno pevných tick_hz s ČASOVÝM
        # vyhlazením → plynulost ani svižnost už na fps nezávisí.
        self._tgt_pan  = None
        self._tgt_tilt = None
        self._tick_hz  = float(cfg.get("tick_hz", 50.0))
        # EYE_TAU_HOTRELOAD_V1 — config drží VÝCHOZÍ rychlost pohledu, ale
        # eye_calibration.json ji smí přebít a přenačíst se za běhu. Bez toho
        # stála každá zkouška svižnosti restart Hanse, zatímco střed a zisk
        # (ty jsou v kalibraci) se ladily okamžitě — ladit se má všechno stejně.
        self._tau_cfg  = float(cfg.get("smooth_tau_s", 0.10))
        self._tau      = self._tau_cfg
        self._tick_thread = None
        self._tick_lock   = threading.Lock()

        self._calib_path = cfg.get("calib_file", "eye_calibration.json")
        self.calib = self._load_calib(self._calib_path)
        self._calib_mtime = self._file_mtime(self._calib_path)
        self._calib_check_ts = 0.0
        self._pan_ch  = self.calib["channels"].get("pan", "P2")
        self._tilt_ch = self.calib["channels"].get("tilt", "P3")
        self._gaze_tilt_offset = float(self.calib.get("gaze_tilt_offset", 0.0))
        self._tau = float(self.calib.get("smooth_tau_s", self._tau_cfg))  # EYE_TAU_HOTRELOAD_V1

        if not self.enabled:
            _log.info("EyeServoController vypnuto (config eye_servo.enabled=false)")
            return
        try:
            # EYE_SERVO_MCU_RESET_V1 (13.8.) — HAT MCU se MUSÍ resetovat, než se
            # serva vytvoří, jinak jsou povely TIŠE IGNOROVÁNY (týž důvod i týž
            # postup jako v `servo_controller`, kde to je od začátku).
            # Doložený bug: oči šlo z menu zapnout jednou a fungovaly; po vypnutí
            # (`release()` → pulse_width(0)) je už žádné zapnutí nerozhýbalo —
            # stav serva se bez resetu nezvedne. Po bootu je MCU ještě použitelné,
            # proto to PRVNÍ zapnutí vypadalo v pořádku a chyba se zdála být
            # v přepínači. Ověřeno naživo: s resetem se oči rozhýbou i po cyklu
            # release→recenter, bez něj se nehnou vůbec.
            # ⚠️ Reset provádíme JEN když kamerová serva běžet nemají — jinak ho
            # udělal `ServoController` při startu a druhý reset by kamerovým
            # servům uprostřed práce podrazil nohy (dnes jsou vypnutá kvůli
            # krátkému CSI kabelu, ale až se vrátí, tohle to ošetří).
            if not bool((self.config.get("servo_tracking", {}) or {})
                        .get("enable_tracking", False)):
                try:
                    from robot_hat import utils as _rh_utils
                    _rh_utils.reset_mcu()
                    time.sleep(0.2)      # MCU potřebuje chvíli na náběh
                    _log.info("eye_servo: HAT MCU resetován (kamerová serva "
                              "vypnutá → neudělal to ServoController)")
                except Exception as _re:
                    _log.warning("eye_servo: reset MCU selhal (%s) — povely "
                                 "serv mohou být ignorovány", _re)
            from robot_hat import Servo
            self._pan_s  = Servo(self._pan_ch)
            self._tilt_s = Servo(self._tilt_ch)
            self.available = True
            _log.info("EyeServoController ready — pan=%s tilt=%s pan_lim[%.0f,%.0f] tilt_lim[%.0f,%.0f]",
                      self._pan_ch, self._tilt_ch,
                      self.calib["pan"]["min"], self.calib["pan"]["max"],
                      self.calib["tilt"]["min"], self.calib["tilt"]["max"])
            self.center()
            self._init_lids()          # HANS_EYE_BLINK_V1
        except Exception as e:
            _log.warning("EyeServoController HW init selhal (%s) — oči neaktivní", e)
            self.available = False

    # ── víčka / mrkání (HANS_EYE_BLINK_V1) ──────────────────────────────
    def _init_lids(self):
        """Vytvoř serva víček z kalibrace (lids: {key:{channel,closed,open}}).
        Klidová poloha = OTEVŘENO. Bez víček v kalibraci = mrkání tiše vypnuté."""
        lids = (self.calib.get("lids") or {})
        if not lids:
            _log.info("eye_servo: žádná víčka v kalibraci — mrkání vypnuto")
            return
        try:
            from robot_hat import Servo
            for key, rec in lids.items():
                ch = (rec or {}).get("channel")
                if not ch:
                    continue
                self._lid_servos[key] = Servo(ch)
                self._lid_open[key] = float(rec.get("open", 0.0))
                self._lid_closed[key] = float(rec.get("closed", 0.0))
            if self._lid_servos:
                self._lids_available = True
                self._set_lids("open")                  # klid = otevřeno
                self._schedule_next_blink(time.time())
                _log.info("eye_servo: víčka připojena (%s), mrkání %s",
                          ",".join(sorted(self._lid_servos)),
                          "ON" if self._blink_enabled else "OFF")
        except Exception as e:
            _log.warning("eye_servo: init víček selhal (%s) — mrkání vypnuto", e)
            self._lids_available = False

    def _set_lids(self, state: str):
        """Nastav všechna víčka na 'open' nebo 'closed' (jednorázový povel)."""
        if not self._lids_available:
            return
        tbl = self._lid_open if state == "open" else self._lid_closed
        with self._io_lock:
            for key, servo in self._lid_servos.items():
                try:
                    servo.angle(tbl.get(key, 0.0))
                except Exception as e:
                    _log.debug("eye_servo: lid angle selhal (%s): %s", key, e)
        self._lid_last_move_ts = time.time()
        self._lids_released = False

    def _schedule_next_blink(self, now: float):
        self._next_blink_ts = now + random.uniform(self._blink_min_s,
                                                    self._blink_max_s)

    def open_lids(self):
        self._set_lids("open")

    def close_lids(self):
        self._set_lids("closed")

    def _maybe_release_lids(self, now: float):
        """Volitelně uvolní serva víček po klidu (proti pískání). Default 0 =
        držet (aby otevřené víčko nepropadlo). Zapni jen když víčko drží i limp."""
        if (self._lid_release_s <= 0 or self._lids_released
                or self._blinking):
            return
        if now - self._lid_last_move_ts < self._lid_release_s:
            return
        with self._io_lock:
            for servo in self._lid_servos.values():
                try:
                    servo.pulse_width(0)
                except Exception:
                    pass
        self._lids_released = True

    def _lids_to_frac(self, frac: float):
        """Nastav víčka na zlomek dráhy: 0 = otevřeno, 1 = zavřeno. I2C zámek,
        ať se to nepere s pan/tilt povely z hlavní smyčky."""
        frac = max(0.0, min(1.0, frac))
        with self._io_lock:
            for key, servo in self._lid_servos.items():
                o = self._lid_open.get(key, 0.0)
                c = self._lid_closed.get(key, 0.0)
                try:
                    servo.angle(o + (c - o) * frac)
                except Exception as e:
                    _log.debug("eye_servo: lid frac angle selhal (%s): %s", key, e)
        self._lid_last_move_ts = time.time()
        self._lids_released = False

    def _run_blink(self):
        """HANS_EYE_BLINK_SMOOTH_V2 — mrknutí v samostatném vlákně JEMNÝMI kroky
        (á ~12 ms), aby servo bylo pořád v pohybu = PLYNULE (dřív řídké povely
        1×/snímek → servo dojelo a čekalo → pohyb „po pulzech")."""
        try:
            dt = 0.012
            n_close = max(3, int(self._blink_close_s / dt))
            for i in range(1, n_close + 1):
                self._lids_to_frac(i / n_close)
                time.sleep(dt)
            time.sleep(self._blink_hold_s)
            n_open = max(3, int(self._blink_open_s / dt))
            for i in range(1, n_open + 1):
                self._lids_to_frac(1.0 - i / n_open)
                time.sleep(dt)
        except Exception as e:
            _log.debug("eye_servo: _run_blink selhal: %s", e)
        finally:
            self._blinking = False

    def maybe_blink(self):
        """Volat KAŽDÝ snímek. Neblokuje. Jen SPOUŠTÍ periodické mrkání (když
        blink_enabled) a uvolňuje serva víček po klidu — samotný pohyb běží ve
        vlákně (_run_blink), aby byl plynulý."""
        if not self._lids_available:
            return
        now = time.time()
        if self._blink_enabled and not self._blinking:
            if self._next_blink_ts <= 0.0:
                self._schedule_next_blink(now)
            elif now >= self._next_blink_ts:
                self.blink_now()
                self._schedule_next_blink(now)
        if not self._blinking:
            self._maybe_release_lids(now)

    def blink_now(self):
        """Jednorázové mrknutí na povel (pozdrav, /blink). Neblokuje — spustí
        vlákno _run_blink (plynulá jemná rampa). Ignoruje, pokud už mrká."""
        if not self._lids_available or self._blinking:
            return
        self._blinking = True
        self._blink_thread = threading.Thread(target=self._run_blink, daemon=True)
        self._blink_thread.start()

    def _load_calib(self, path: str) -> dict:
        calib = json.loads(json.dumps(_DEFAULT_CALIB))  # deep copy
        try:
            p = Path(path)
            if p.exists():
                data = json.loads(p.read_text(encoding="utf-8"))
                for k in ("channels", "pan", "tilt"):
                    if k in data and isinstance(data[k], dict):
                        calib[k].update(data[k])
                if isinstance(data.get("lids"), dict):   # HANS_EYE_BLINK_V1
                    calib["lids"] = data["lids"]
                if "gaze_tilt_offset" in data:           # HANS_EYE_GAZE_TILT_OFFSET_V1
                    calib["gaze_tilt_offset"] = data["gaze_tilt_offset"]
                if "smooth_tau_s" in data:               # EYE_TAU_HOTRELOAD_V1
                    calib["smooth_tau_s"] = data["smooth_tau_s"]
        except Exception as e:
            _log.warning("eye_servo: čtení kalibrace %s selhalo (%s) — defaulty", path, e)
        return calib

    # ── mapování ────────────────────────────────────────────────────────
    def _map_axis(self, frac: float, axis: str, invert: bool) -> float:
        """frac 0..1 (poloha v rámu) → úhel serva. 0.5 = střed.
        Asymetrické: záporná strana škáluje k min, kladná k max."""
        c = self.calib[axis]
        center = float(c["center"])
        lo, hi = float(c["min"]), float(c["max"])
        dev = (frac - 0.5) * 2.0          # -1 (levý/horní okraj) .. +1
        # EYE_GAZE_GAIN_V1 — zisk mapování (eye_calibration.json: pan.gain /
        # tilt.gain, hot-reload). Rám NENÍ zorný úhel: po otočení kamery o 90°
        # (22.8.) je vodorovný záběr užší a svislý širší, takže tatáž výchylka
        # v rámu znamená jiný skutečný úhel — oči vodorovně přestřelují a
        # svisle podstřelují. gain srovná citlivost, aniž se sahá na min/max
        # (ty drží fyzické meze serva). 1.0 = beze změny.
        dev *= float(c.get("gain", 1.0))
        dev = max(-1.0, min(1.0, dev))
        if invert:
            dev = -dev
        if dev >= 0:
            angle = center + dev * (hi - center)
        else:
            angle = center + dev * (center - lo)
        return max(lo, min(hi, angle))

    def _send(self, servo, target: float, ema_attr: str, last_attr: str,
              alpha: float = None):
        """Posune vyhlazenou hodnotu k cíli a pošle ji, až překročí deadband.

        alpha=None → původní per-volání EMA (self._smooth). Vlákno _run_tick
        dodává alpha spočítanou Z ČASU (EYE_SMOOTH_TICK_V1), takže vyhlazení
        nezávisí na tom, jak často se volá."""
        a = self._smooth if alpha is None else alpha
        prev = getattr(self, ema_attr)
        if prev is None:
            ema = target
        else:
            ema = a * target + (1 - a) * prev
        setattr(self, ema_attr, ema)
        last = getattr(self, last_attr)
        if last is not None and abs(ema - last) < self._deadband:
            return
        try:
            with self._io_lock:          # I2C sdílené s vláknem víček
                servo.angle(ema)
            setattr(self, last_attr, ema)
            self._last_move_ts = time.time()
            self._released = False
        except Exception as e:
            _log.debug("eye_servo: angle() selhal: %s", e)

    def _maybe_release(self):
        """Po idle_release_s bez reálného pohybu uvolní serva (ticho)."""
        if not self.available or self._released or self._idle_release_s <= 0:
            return
        if (time.time() - self._last_move_ts) < self._idle_release_s:
            return
        self.release()

    def release(self):
        """pulse_width(0) → přeruší PWM → serva limp → přestanou pískat."""
        if not self.available:
            return
        # EYE_SMOOTH_TICK_RELEASE_V1 — zahoď cíl, jinak by k němu vlákno
        # (_run_tick) dojíždělo dál a uvolněná serva by hned zase probudilo:
        # dřív se posílalo přímo z look_at_frac, takže vypnutí očí povely
        # utnulo samo. Teď to musí říct release() explicitně. Nový cíl přijde
        # z look_at_frac / center() / recenter(), takže se nic neztrácí.
        with self._tick_lock:
            self._tgt_pan = None
            self._tgt_tilt = None
        with self._io_lock:
            for servo in (self._pan_s, self._tilt_s):
                try:
                    servo.pulse_width(0)
                except Exception as e:
                    _log.debug("eye_servo: pulse_width(0) selhal: %s", e)
        self._released = True

    # ── veřejné API ─────────────────────────────────────────────────────
    @staticmethod
    def _file_mtime(path: str) -> float:
        try:
            return Path(path).stat().st_mtime
        except OSError:
            return 0.0

    def _maybe_reload_calib(self):
        """EYE_SERVO_CALIB_RELOAD_V1 — hot-reload kalibrace při změně souboru
        (throttle 1 s), aby se pan/tilt center dal ladit bez restartu Hanse.
        Kanály (serva) se nemění, jen center/min/max."""
        now = time.time()
        if now - self._calib_check_ts < 1.0:
            return
        self._calib_check_ts = now
        mt = self._file_mtime(self._calib_path)
        if mt and mt != self._calib_mtime:
            self._calib_mtime = mt
            self.calib = self._load_calib(self._calib_path)
            self._gaze_tilt_offset = float(self.calib.get("gaze_tilt_offset", 0.0))
            self._tau = float(self.calib.get("smooth_tau_s", self._tau_cfg))
            _log.info("eye_servo: kalibrace přenačtena (pan center=%.1f "
                      "tilt center=%.1f tilt_offset=%.1f tau=%.3f "
                      "gain pan/tilt=%.2f/%.2f)",
                      self.calib["pan"]["center"], self.calib["tilt"]["center"],
                      self._gaze_tilt_offset, self._tau,
                      float(self.calib["pan"].get("gain", 1.0)),
                      float(self.calib["tilt"].get("gain", 1.0)))

    def _ensure_tick(self):
        """EYE_SMOOTH_TICK_V1 — líné spuštění vlákna pohybu očí."""
        if not self.available or self._tick_thread is not None:
            return
        self._tick_thread = threading.Thread(target=self._run_tick,
                                             daemon=True, name="eye_tick")
        self._tick_thread.start()
        _log.info("eye_servo: plynulý pohyb očí zapnut (%.0f Hz, tau=%.2f s)",
                  self._tick_hz, self._tau)

    def _run_tick(self):
        """EYE_SMOOTH_TICK_V1 — dojíždí k poslednímu cíli pevnou frekvencí,
        nezávisle na kameře.

        Vyhlazení je ČASOVÉ: alpha = 1 - exp(-dt/tau), takže tatáž svižnost
        při jakémkoli fps. Deadband zůstává — jen se kroky posílají tick_hz×/s
        místo ~10×/s, takže je z nich plynulý náběh a ne schody. Když je EMA
        na cíli, nepošle se nic → idle_release_s (ticho serv) platí dál.
        Vlákno běží i s vypnutýma očima (jen mlčí) — cena je jeden sleep."""
        import math
        period = 1.0 / max(1.0, self._tick_hz)
        prev_ts = time.time()
        while True:
            time.sleep(period)
            now = time.time()
            dt = min(0.5, max(1e-3, now - prev_ts))
            prev_ts = now
            with self._tick_lock:
                tp, tt = self._tgt_pan, self._tgt_tilt
            if tp is None or tt is None or not self.available:
                continue
            alpha = 1.0 - math.exp(-dt / max(1e-3, self._tau))
            try:
                self._send(self._pan_s,  tp, "_pan_ema",  "_last_pan",  alpha)
                self._send(self._tilt_s, tt, "_tilt_ema", "_last_tilt", alpha)
                self._maybe_release()
            except Exception as e:
                _log.debug("eye_servo: tick selhal: %s", e)

    def look_at_frac(self, cx: float, cy: float):
        """cx, cy ∈ 0..1 = střed bboxu osoby v rámu. ULOŽÍ cíl pohledu.

        Na serva už odsud nejde nic — k cíli dojede vlákno (_run_tick), takže
        se sem smí volat jak zřídka chce (dnes 1× za DETECT_EVERY snímků).
        Plynulost drží vlákno, ne frekvence volání. EYE_SMOOTH_TICK_V1."""
        if not self.available:
            return
        self._maybe_reload_calib()
        pan  = self._map_axis(cx, "pan",  self._pan_invert)
        tilt = self._map_axis(cy, "tilt", self._tilt_invert)
        # HANS_EYE_GAZE_TILT_OFFSET_V1 — posun pohledu nahoru/dolů (kamera výš
        # než oči), oříznutý na kalibrované meze.
        if self._gaze_tilt_offset:
            _t = self.calib["tilt"]
            tilt = max(float(_t["min"]), min(float(_t["max"]),
                                             tilt + self._gaze_tilt_offset))
        with self._tick_lock:
            self._tgt_pan, self._tgt_tilt = pan, tilt
        self._ensure_tick()

    def center(self):
        if not self.available:
            return
        self.look_at_frac(0.5, 0.5)

    def recenter(self):
        """EYE_SERVO_RECENTER_V1 — vynuceně na kalibrovaný střed + reset EMA/last.
        Volá se při RE-aktivaci očí (po vypnutí). Bez tohohle cached controller
        drží poslední (často off-center) pozici z okamžiku vypnutí a EMA/deadband
        ji berou jako výchozí → oči „koukají" mimo. Reset EMA=None → _send pošle
        přímo cílový střed (bez vyhlazení ze staré pozice)."""
        if not self.available:
            return
        self._pan_ema = None
        self._tilt_ema = None
        self._last_pan = None
        self._last_tilt = None
        self._released = False
        self.center()
