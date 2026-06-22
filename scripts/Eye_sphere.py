"""
eye_sphere.py — 3D Sphere Eye Renderer for Waveshare Dual Eye Display
======================================================================
Renders a UV-mapped textured sphere on both GC9A01/GC9D01 160×160 displays.
Drop-in replacement for the frame-file approach in eye_controller.py.

Hardware: Raspberry Pi 5 + Waveshare Dual Eye LCD
Wiring (ATTENTION_DISPLAY rewire 16.6.2026 — viz connection.txt; CS přesunut z
CE0/CE1 na volné GPIO kvůli software-CS driveru):
  DIN  → GPIO10  (MOSI, SPI0,  pin 19)
  CLK  → GPIO11  (SCLK, SPI0,  pin 23)
  DC   → GPIO25  (pin 22)
  CS1  → GPIO5   (left eye,  pin 29)
  CS2  → GPIO6   (right eye, pin 31)
  RST1 → GPIO24  (pin 18)
  RST2 → GPIO23  (pin 16)
  BL1  → GPIO18  (pin 12, or tie to 3.3V)
  BL2  → GPIO19  (pin 35, or tie to 3.3V)

Requirements:
  pip install spidev lgpio Pillow numpy

Usage:
  # Standalone animated demo:
  python3 eye_sphere.py

  # Integrate with your robot — call update_pan() from your servo code:
  from eye_sphere import SphereEyeController
  eyes = SphereEyeController(config)
  eyes.start()
  eyes.update_pan(pan_angle_degrees)
  eyes.stop()

Eye texture:
  Place your equirectangular eye texture as 'eye.png' in the same directory.
  The texture must have the iris centered at u=0.5, v=0.5 (image center).
  Green-screen (#00FF7B) pupils are automatically replaced with black.
"""

import spidev
try:
    from scripts.logger import get_logger
    _eyelog = get_logger('eyes')
except ImportError:
    import logging as _logging
    _eyelog = _logging.getLogger('eyes')
import lgpio
import time
import math
import threading
import random
import os
import sys

import numpy as np
from PIL import Image

# ─── GPIO / PIN DEFINITIONS (ATTENTION_DISPLAY rewire 16.6.2026, BCM) ─────────
# Ověřeno testem: CS1=fyzicky PRAVÝ displej (pozornost), CS2=fyzicky LEVÝ (tvář).
# ⚠️ CS1 z GPIO5: GPIO5 = robot_hat MCURST (reset MCU) → přesunut na GPIO13.
# ⚠️⚠️ BL1/BL2 BYLY na GPIO18/19 = I2S audio hodiny (hifiberry-dac BCLK/LRCLK)!
#   → umlčelo to Hanse. PODSVIT PATŘÍ NA 3.3V (vždy on), NE na GPIO. BL = None.
DC   = 25
CS1  = 13   # pin 33 — FYZICKY PRAVÝ displej (pozornost)
CS2  = 6    # pin 31 — FYZICKY LEVÝ displej  (tvář)
RST1 = 24
RST2 = 23
BL1  = None  # tie 3.3V (NIKDY GPIO18 — I2S BCLK!)
BL2  = None  # tie 3.3V (NIKDY GPIO19 — I2S WS!)

W = H = 160
FRAME_SIZE = W * H * 2  # RGB565 bytes per frame


# ─── GC9A01 DISPLAY DRIVER ────────────────────────────────────────────────────

class GC9A01:
    """Minimal GC9A01/GC9D01 driver matching your existing init sequence."""

    def __init__(self, h, spi, cs, rst, rotation=0):
        self.h   = h
        self.spi = spi
        self.cs  = cs
        self.rst = rst
        self.rotation = rotation

    def _write_cmd(self, cmd, data=None):
        lgpio.gpio_write(self.h, DC, 0)
        lgpio.gpio_write(self.h, self.cs, 0)
        self.spi.writebytes2(bytes([cmd]))
        lgpio.gpio_write(self.h, self.cs, 1)
        if data:
            lgpio.gpio_write(self.h, DC, 1)
            lgpio.gpio_write(self.h, self.cs, 0)
            self.spi.writebytes2(bytes(data))
            lgpio.gpio_write(self.h, self.cs, 1)

    def _reset(self):
        lgpio.gpio_write(self.h, self.rst, 1); time.sleep(0.01)
        lgpio.gpio_write(self.h, self.rst, 0); time.sleep(0.1)
        lgpio.gpio_write(self.h, self.rst, 1); time.sleep(0.1)

    def init(self):
        """Exact init sequence from your eye_controller.py."""
        self._reset()
        wc = self._write_cmd
        wc(0xFE); wc(0xEF)
        for r in range(0x80, 0x90): wc(r, [0xFF])
        wc(0x3A, [0x05]); wc(0xEC, [0x01])
        wc(0x74, [0x02,0x0E,0x00,0x00,0x00,0x00,0x00])
        wc(0x98, [0x3E]); wc(0x99, [0x3E]); wc(0xB5, [0x0D,0x0D])
        wc(0x60, [0x38,0x0F,0x79,0x67]); wc(0x61, [0x38,0x11,0x79,0x67])
        wc(0x64, [0x38,0x17,0x71,0x5F,0x79,0x67])
        wc(0x65, [0x38,0x13,0x71,0x5B,0x79,0x67])
        wc(0x6A, [0x00,0x00])
        wc(0x6C, [0x22,0x02,0x22,0x02,0x22,0x22,0x50])
        wc(0x6E, [0x03,0x03,0x01,0x01,0x00,0x00,0x0f,0x0f,
                  0x0d,0x0d,0x0b,0x0b,0x09,0x09,0x00,0x00,
                  0x00,0x00,0x0a,0x0a,0x0c,0x0c,0x0e,0x0e,
                  0x10,0x10,0x00,0x00,0x02,0x02,0x04,0x04])
        wc(0xBF,[0x01]); wc(0xF9,[0x40]); wc(0x9B,[0x3B])
        wc(0x93,[0x33,0x7F,0x00]); wc(0x7E,[0x30])
        wc(0x70,[0x0D,0x02,0x08,0x0D,0x02,0x08])
        wc(0x71,[0x0D,0x02,0x08])
        wc(0x91,[0x0E,0x09]); wc(0xC3,[0x1F]); wc(0xC4,[0x1F]); wc(0xC9,[0x1F])
        wc(0xF0,[0x53,0x15,0x0A,0x04,0x00,0x3E])
        wc(0xF2,[0x53,0x15,0x0A,0x04,0x00,0x3A])
        wc(0xF1,[0x56,0xA8,0x7F,0x33,0x34,0x5F])
        wc(0xF3,[0x52,0xA4,0x7F,0x33,0x34,0xDF])
        wc(0x36, [[0xC8,0x68,0x08,0xA8][self.rotation % 4]])
        wc(0x3A,[0x05]); wc(0xB0,[0x00]); wc(0xB1,[0x00,0x00]); wc(0xB4,[0x00])
        wc(0x11); time.sleep(0.2); wc(0x29); wc(0x2C)

    def set_window(self):
        wc = self._write_cmd
        wc(0x2A, [0x00, 0x00, 0x00, W-1])
        wc(0x2B, [0x00, 0x00, 0x00, H-1])
        wc(0x2C)

    def send_frame(self, rgb565_bytes: bytes):
        """Push a full 160×160 frame (RGB565, big-endian)."""
        self.set_window()
        lgpio.gpio_write(self.h, DC, 1)
        lgpio.gpio_write(self.h, self.cs, 0)
        self.spi.writebytes2(rgb565_bytes)
        lgpio.gpio_write(self.h, self.cs, 1)


# ─── TEXTURE LOADING ─────────────────────────────────────────────────────────

def load_eye_texture(path: str) -> np.ndarray:
    """
    Load equirectangular eye texture and replace green chroma-key pupil
    with a natural dark pupil.  Returns (H, W, 3) uint8 numpy array.
    """
    img = Image.open(path).convert("RGB")
    arr = np.array(img, dtype=np.int16)
    r, g, b = arr[:,:,0], arr[:,:,1], arr[:,:,2]

    # Detect green chroma key: g much brighter than r and b
    green_mask = (
        (g.astype(int) - r.astype(int) > 55) &
        (g.astype(int) - b.astype(int) > 40) &
        (g > 80)
    )

    out = arr.astype(np.uint8)
    out[green_mask] = [12, 10, 10]   # near-black pupil
    n = int(green_mask.sum())
    print(f"  Texture: {img.width}×{img.height}px, replaced {n} chroma-key pixels → pupil")
    return out


# ─── SPHERE RENDERER ─────────────────────────────────────────────────────────

class SphereRenderer:
    """
    Renders a UV-mapped textured sphere onto a 160×160 canvas.

    Uses camera-facing equirectangular projection:
      - lon = atan2(Xn, Zn)  → u = (lon/2π + 0.5) % 1.0
      - lat = arcsin(Yn)     → v = 0.5 - lat/π

    This maps the texture center (u=0.5, v=0.5) to the front of the sphere,
    so the iris appears centred when yaw=0, pitch=0.

    All UV arithmetic is precomputed once; per-frame work is index-array
    lookup + RGB565 conversion — fast enough for 20+ fps on a Pi 5.
    """

    def __init__(self, tex_arr: np.ndarray, width=160, height=160, radius=75):
        self.w   = width
        self.h   = height
        self.r   = radius
        self.tex = tex_arr
        self.th, self.tw = tex_arr.shape[:2]
        self._precompute()

    def _precompute(self):
        cx, cy = self.w // 2, self.h // 2
        xs = np.arange(self.w, dtype=np.float32) - cx
        ys = np.arange(self.h, dtype=np.float32) - cy
        X, Y = np.meshgrid(xs, ys)
        d2   = X*X + Y*Y
        r2   = float(self.r * self.r)

        self.mask = d2 <= r2
        Z = np.where(self.mask, np.sqrt(np.maximum(r2 - d2, 0.0)), 0.0).astype(np.float32)

        self._Xn = (X / self.r).astype(np.float32)
        self._Yn = (Y / self.r).astype(np.float32)
        self._Zn = (Z / self.r).astype(np.float32)

        # Base longitude/latitude (camera-facing equirectangular)
        self._lon0 = np.arctan2(self._Xn, self._Zn)          # -π … +π
        self._lat0 = np.arcsin(np.clip(self._Yn, -1.0, 1.0)) # -π/2 … +π/2

        # Diffuse shading: fixed light from upper-left-front
        lx, ly, lz = -0.35, -0.55, 0.76
        ln = math.sqrt(lx*lx + ly*ly + lz*lz)
        self._diff = np.clip(
            self._Xn*(lx/ln) + self._Yn*(ly/ln) + self._Zn*(lz/ln),
            0.0, 1.0
        ).astype(np.float32)

        # Black background buffer
        self._bg = np.zeros((self.h, self.w, 3), dtype=np.uint8)

    def render(self, yaw: float, pitch: float) -> bytes:
        """
        Render the sphere for the given gaze angles (radians).
        Returns raw RGB565 bytes ready to write to the display.
        """
        tw, th = self.tw, self.th
        lon = self._lon0 + yaw
        lat = np.clip(self._lat0 + pitch, -math.pi/2, math.pi/2)

        # Equirectangular UV (centered: lon=0 → u=0.5)
        u = (((lon / (2.0*math.pi) + 0.5) % 1.0) * tw).astype(np.int32) % tw
        v = np.clip(((0.5 - lat / math.pi) * th).astype(np.int32), 0, th-1)

        mask = self.mask
        rgb  = self._bg.copy()
        rgb[mask] = self.tex[v[mask], u[mask]]

        # Diffuse shading with ambient
        AMBIENT = 0.42
        shade   = self._diff[:, :, np.newaxis]
        rgb[mask] = np.clip(
            rgb[mask].astype(np.float32) * (AMBIENT + (1.0 - AMBIENT) * shade[mask]),
            0, 255
        ).astype(np.uint8)

        # Convert to RGB565 big-endian (GC9A01 default)
        r16 = rgb[:,:,0].astype(np.uint16)
        g16 = rgb[:,:,1].astype(np.uint16)
        b16 = rgb[:,:,2].astype(np.uint16)
        px565 = ((r16 & 0xF8) << 8) | ((g16 & 0xFC) << 3) | (b16 >> 3)
        return px565.astype('>u2').tobytes()


# ─── EYE ANIMATOR ────────────────────────────────────────────────────────────

class EyeAnimator:
    """
    Maps a pan servo angle to smooth eye gaze (yaw + pitch),
    adding micro-tremor and idle saccades.

    Pan→gaze mapping:
      - Large pan delta → eyes follow direction (vergence)
      - Pan settles    → eyes drift back to center
      - Always        → random blink saccades every few seconds
    """

    # How many degrees of pan = 1 radian of eye rotation
    # 45° pan → ~1 rad (≈57°) eye movement — adjust to taste
    PAN_TO_YAW_SCALE = 1.0 / 15.0   # 15° pan = ~1 rad eye rotation

    def __init__(self, cfg: dict = None):
        cfg = cfg or {}
        self.blink_min = cfg.get('blink_interval_min', 3.0)
        self.blink_max = cfg.get('blink_interval_max', 6.5)
        self.look_delay = cfg.get('look_delay', 0.35)

        # Current rendered angles (lerp targets)
        self.yaw_l   = 0.0;  self.pitch_l = 0.0
        self.yaw_r   = 0.0;  self.pitch_r = 0.0

        # Smooth targets
        self._tgt_yaw   = 0.0
        self._tgt_pitch = 0.0

        # Pan tracking
        self._pan        = 0.0
        self._prev_pan   = None
        self._last_delta = 0.0
        self._last_move  = 0.0

        # Tilt tracking
        self._tilt       = 0.0
        self._prev_tilt  = None
        self._last_tilt_delta = 0.0
        self._last_tilt_move  = 0.0

        # Idle saccade
        self._saccade_t  = time.time() + random.uniform(2.0, 4.0)

        # Blink state (pupil dilation not needed since real blink = eyelid hardware)
        # We simulate a tiny downward pitch flick as a micro-blink
        self._blink_t    = time.time() + random.uniform(self.blink_min, self.blink_max)
        self._blink_dur  = 0.0
        self._blink_phase = 0

        self._t = 0.0

    def set_pan(self, pan_degrees: float):
        self._pan = pan_degrees

    def set_tilt(self, tilt_degrees: float):
        self._tilt = tilt_degrees

    def update(self, dt: float) -> tuple:
        """
        Advance animation by dt seconds.
        Returns (yaw_left, pitch_left, yaw_right, pitch_right) in radians.
        """
        self._t += dt
        now = time.time()

        # ── Pan → gaze (movement only) ────────────────────────────────
        pan = self._pan

        # Detect if camera is actively moving
        if self._prev_pan is not None:
            delta = pan - self._prev_pan
            if abs(delta) > 0.3:                # camera moved
                self._last_delta = delta
                self._last_move  = now
        self._prev_pan = pan

        SETTLE_TIME = 0.5   # seconds after movement stops → eyes return to center

        if (now - self._last_move) < SETTLE_TIME:
            # Camera is moving → eyes follow direction with scale
            self._tgt_yaw = max(-0.6, min(0.6,
                -self._last_delta * self.PAN_TO_YAW_SCALE * 8.0))
        else:
            # Camera settled → eyes return to center
            self._tgt_yaw = 0.0

        # ── Tilt → pitch (movement only, same as pan) ───────────────
        if self._prev_tilt is not None:
            tilt_delta = self._tilt - self._prev_tilt
            if abs(tilt_delta) > 0.3:
                self._last_tilt_delta = tilt_delta
                self._last_tilt_move  = now
        self._prev_tilt = self._tilt

        if (now - self._last_tilt_move) < SETTLE_TIME:
            self._tgt_pitch = max(-0.4, min(0.4,
                -self._last_tilt_delta * self.PAN_TO_YAW_SCALE * 8.0))
        else:
            self._tgt_pitch = 0.0

        # ── Idle saccades (only when centered) ────────────────────────
        if now >= self._saccade_t and (now - self._last_move) >= SETTLE_TIME:
            amp_y = math.radians(random.uniform(2, 8))
            amp_p = math.radians(random.uniform(1, 4))
            self._tgt_yaw   += random.choice([-1, 1]) * amp_y
            self._tgt_pitch += random.choice([-1, 1]) * amp_p
            self._tgt_yaw   = max(-0.30, min(0.30, self._tgt_yaw))
            self._tgt_pitch = max(-0.20, min(0.20, self._tgt_pitch))
            self._saccade_t  = now + random.uniform(1.5, 4.0)

        # ── Micro-blink (subtle pitch flick) ──────────────────────────
        blink_pitch_add = 0.0
        if now >= self._blink_t:
            self._blink_dur  += dt
            BLINK_LEN = 0.12
            phase = min(self._blink_dur / BLINK_LEN, 1.0)
            blink_pitch_add = math.sin(phase * math.pi) * 0.06  # tiny downward flick
            if self._blink_dur >= BLINK_LEN:
                self._blink_dur = 0.0
                self._blink_t   = now + random.uniform(self.blink_min, self.blink_max)

        # ── Micro-tremor (physiologically accurate ~50Hz drift) ───────
        TREMOR = 0.0018
        ty = TREMOR * math.sin(self._t * 47.3 + 1.1)
        tp = TREMOR * math.cos(self._t * 53.7 + 2.3)

        # ── Smooth lerp ───────────────────────────────────────────────
        LERP = min(dt * 14.0, 0.85)    # responsive but smooth
        self.yaw_l   += LERP * (self._tgt_yaw   + ty - self.yaw_l)
        self.pitch_l += LERP * (self._tgt_pitch + tp + blink_pitch_add - self.pitch_l)

        # Right eye: same direction as left, no vergence
        self.yaw_r   = self.yaw_l
        self.pitch_r = self.pitch_l

        return self.yaw_l, self.pitch_l, self.yaw_r, self.pitch_r


# ─── MAIN CONTROLLER ─────────────────────────────────────────────────────────

class SphereEyeController:
    """
    Drop-in replacement for EyeController.
    Same public API: __init__(config), start(), stop(), update_pan(angle).
    """

    def __init__(self, config: dict = None):
        config = config or {}
        self.eye_cfg = config.get('eyes', {})
        self.enabled = self.eye_cfg.get('enabled', True)
        self.fps     = self.eye_cfg.get('fps', 22)

        tex_path = self.eye_cfg.get('texture', 'eye.png')
        if not os.path.isfile(tex_path):
            # Try same directory as this script
            tex_path = os.path.join(os.path.dirname(__file__), 'eye.png')

        self._lock    = threading.Lock()
        self._running = False
        self._thread  = None
        self._pan     = 0.0
        self._tilt    = 0.0

        self.h   = None
        self.spi = None
        self.hw_ok = False

        if not self.enabled:
            print("👁  SphereEyeController disabled")
            return

        print("👁  Loading texture …")
        try:
            self.tex_arr = load_eye_texture(tex_path)
        except FileNotFoundError:
            print(f"  ⚠️  Texture not found: {tex_path}")
            print("       Place eye.png next to this script.")
            return

        print("👁  Precomputing sphere UV map …")
        self.renderer_l = SphereRenderer(self.tex_arr, radius=76)
        self.renderer_r = SphereRenderer(self.tex_arr, radius=76)
        self.animator   = EyeAnimator(self.eye_cfg)

        print("👁  Initialising hardware …")
        self._init_hw()

    def _init_hw(self):
        try:
            self.h = lgpio.gpiochip_open(0)
            for pin in [DC, RST1, RST2, CS1, CS2, BL1, BL2]:
                if pin is None:      # BL na 3.3V → nesahat (GPIO18/19 = I2S audio!)
                    continue
                lgpio.gpio_claim_output(self.h, pin, 0)

            self.spi = spidev.SpiDev()
            self.spi.open(0, 0)
            self.spi.max_speed_hz = 40_000_000
            self.spi.mode = 0

            self.disp_l = GC9A01(self.h, self.spi, CS1, RST1, rotation=1)
            self.disp_r = GC9A01(self.h, self.spi, CS2, RST2, rotation=3)
            self.disp_l.init()
            self.disp_r.init()

            if BL1 is not None:
                lgpio.gpio_write(self.h, BL1, 1)
            if BL2 is not None:
                lgpio.gpio_write(self.h, BL2, 1)

            self.hw_ok = True
            print("  ✅ Displays OK")
        except Exception as e:
            print(f"  ❌ Hardware error: {e}")
            self.hw_ok = False

    def update_pan(self, pan_angle: float):
        """Call from your servo thread with current pan angle in degrees."""
        with self._lock:
            self._pan = pan_angle

    def update_tilt(self, tilt_angle: float):
        """Call from your servo thread with current tilt angle in degrees."""
        with self._lock:
            self._tilt = tilt_angle

    def _loop(self):
        frame_time = 1.0 / self.fps
        t_prev = time.monotonic()
        frame  = 0

        while self._running:
            t0 = time.monotonic()
            dt = t0 - t_prev
            t_prev = t0

            with self._lock:
                pan  = self._pan
                tilt = self._tilt
            self.animator.set_pan(pan)
            self.animator.set_tilt(tilt)

            yl, pl, yr, pr = self.animator.update(dt)

            raw_l = self.renderer_l.render(yl, pl)
            raw_r = self.renderer_r.render(yr, pr)

            self.disp_l.send_frame(raw_l)
            self.disp_r.send_frame(raw_r)

            frame += 1
            elapsed = time.monotonic() - t0
            sleep   = frame_time - elapsed
            if sleep > 0:
                time.sleep(sleep)

            if frame % (self.fps * 60) == 0:   # log every ~60s instead of every 5s
                actual_fps = 1.0 / max(dt, 1e-6)
                _eyelog.debug("frame %d pan=%.1f° yaw_L=%.1f° fps=%.1f",
                              frame, pan, math.degrees(yl), actual_fps)

    def start(self):
        if not self.hw_ok or not self.enabled:
            return
        self._running = True
        self._thread  = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        print("👁  Eye loop started")

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=3)
        self._clear_displays()
        self._teardown_hw()
        print("👁  Eye loop stopped")

    def _clear_displays(self):
        if not self.hw_ok:
            return
        black = bytes(FRAME_SIZE)
        try:
            for d in (self.disp_l, self.disp_r):
                d.send_frame(black)
        except Exception:
            pass

    def _teardown_hw(self):
        try:
            if BL1 is not None:
                lgpio.gpio_write(self.h, BL1, 0)
            if BL2 is not None:
                lgpio.gpio_write(self.h, BL2, 0)
        except Exception:
            pass
        if self.spi:
            try: self.spi.close()
            except Exception: pass
        if self.h:
            try: lgpio.gpiochip_close(self.h)
            except Exception: pass

# # p3_eye_cleaned
