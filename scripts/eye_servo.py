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
import time
from pathlib import Path

_log = logging.getLogger("eye_servo")

_DEFAULT_CALIB = {
    "channels": {"pan": "P2", "tilt": "P3"},
    "pan":  {"center": 0.0, "min": -30.0, "max": 30.0},
    "tilt": {"center": 0.0, "min": -20.0, "max": 34.0},
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
        self.available = False
        self._pan_s = None
        self._tilt_s = None
        self._pan_ema = None
        self._tilt_ema = None
        self._last_pan = None
        self._last_tilt = None
        self._last_move_ts = 0.0
        self._released = False

        self.calib = self._load_calib(cfg.get("calib_file", "eye_calibration.json"))
        self._pan_ch  = self.calib["channels"].get("pan", "P2")
        self._tilt_ch = self.calib["channels"].get("tilt", "P3")

        if not self.enabled:
            _log.info("EyeServoController vypnuto (config eye_servo.enabled=false)")
            return
        try:
            from robot_hat import Servo
            self._pan_s  = Servo(self._pan_ch)
            self._tilt_s = Servo(self._tilt_ch)
            self.available = True
            _log.info("EyeServoController ready — pan=%s tilt=%s pan_lim[%.0f,%.0f] tilt_lim[%.0f,%.0f]",
                      self._pan_ch, self._tilt_ch,
                      self.calib["pan"]["min"], self.calib["pan"]["max"],
                      self.calib["tilt"]["min"], self.calib["tilt"]["max"])
            self.center()
        except Exception as e:
            _log.warning("EyeServoController HW init selhal (%s) — oči neaktivní", e)
            self.available = False

    def _load_calib(self, path: str) -> dict:
        calib = json.loads(json.dumps(_DEFAULT_CALIB))  # deep copy
        try:
            p = Path(path)
            if p.exists():
                data = json.loads(p.read_text(encoding="utf-8"))
                for k in ("channels", "pan", "tilt"):
                    if k in data and isinstance(data[k], dict):
                        calib[k].update(data[k])
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
        if invert:
            dev = -dev
        if dev >= 0:
            angle = center + dev * (hi - center)
        else:
            angle = center + dev * (center - lo)
        return max(lo, min(hi, angle))

    def _send(self, servo, target: float, ema_attr: str, last_attr: str):
        prev = getattr(self, ema_attr)
        if prev is None:
            ema = target
        else:
            ema = self._smooth * target + (1 - self._smooth) * prev
        setattr(self, ema_attr, ema)
        last = getattr(self, last_attr)
        if last is not None and abs(ema - last) < self._deadband:
            return
        try:
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
        for servo in (self._pan_s, self._tilt_s):
            try:
                servo.pulse_width(0)
            except Exception as e:
                _log.debug("eye_servo: pulse_width(0) selhal: %s", e)
        self._released = True

    # ── veřejné API ─────────────────────────────────────────────────────
    def look_at_frac(self, cx: float, cy: float):
        """cx, cy ∈ 0..1 = střed bboxu osoby v rámu. Pohne očima tam."""
        if not self.available:
            return
        pan  = self._map_axis(cx, "pan",  self._pan_invert)
        tilt = self._map_axis(cy, "tilt", self._tilt_invert)
        self._send(self._pan_s,  pan,  "_pan_ema",  "_last_pan")
        self._send(self._tilt_s, tilt, "_tilt_ema", "_last_tilt")
        self._maybe_release()

    def center(self):
        if not self.available:
            return
        self.look_at_frac(0.5, 0.5)
