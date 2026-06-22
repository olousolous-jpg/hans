"""
Gesture Client (Hailo mode)
Sends HAND_MAGIC frames to hailo_inference_server via the existing
/tmp/hailo_scrfd.sock — no separate gesture server needed.

Gesture IDs:
  0 = none  1 = fist  2 = thumbs_up
"""

import socket
from collections import deque, Counter
import struct
import threading
import time
import numpy as np
import logging

_log = logging.getLogger("gesture")

SOCK_PATH         = "/tmp/gesture.sock"       # dedicated gesture socket
HAND_MAGIC        = b'\xAA\xBB\xCC\xDD'
GESTURE_NONE      = 0
GESTURE_FIST      = 1
GESTURE_OPEN_HAND = 2
GESTURE_THUMBS_UP = 3
GESTURE_NAMES     = {GESTURE_NONE: None,
                     GESTURE_FIST:      "fist",
                     GESTURE_OPEN_HAND: "open_hand",
                     GESTURE_THUMBS_UP: "thumbs_up"}


class GestureClient:

    def __init__(self, config: dict, on_gesture=None):
        self.config      = config
        self.on_gesture  = on_gesture
        cfg              = config.get("gesture", {})

        self.enabled      = bool(cfg.get("enabled", True))
        self._hold_frames  = int(cfg.get("hold_frames", 8))
        self._hold_seconds = float(cfg.get("hold_seconds", 0.5))
        self._cooldown_s  = float(cfg.get("cooldown_s", 3.0))

        self._sock        = None
        self._lock        = threading.Lock()
        self._connected   = False
        self._busy        = False
        self._pending     = None
        self._fail_cnt    = 0

        self._current          = GESTURE_NONE
        self._current_since    = 0.0
        self._hold_count       = 0
        self._last_fired       : dict = {}
        self._last_fired_name  = None
        self.last_gesture      = None   # last fired gesture name
        self.last_gesture_time = 0.0
        self._vote_buf  = deque(maxlen=10)
        self._streak     = 0   # počet po sobě jdoucích stejných gest
        self._streak_gest = GESTURE_NONE
        self._warmup_frames = 15  # ignoruj první frames po startu
        self.last_landmarks = None   # posledních 21 bodů pro vykreslení
        # Per-gesture hold times (override hold_seconds from config)
        self._hold_per_gesture = {
            GESTURE_OPEN_HAND: float(cfg.get("open_hand_hold_s", 0.0)),
            GESTURE_THUMBS_UP: float(cfg.get("thumbs_up_hold_s", 1.5)),
            GESTURE_FIST:      float(cfg.get("hold_seconds", 0.0)),
        }

        if not self.enabled:
            _log.info("GestureClient disabled")
            return

        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        _log.info("GestureClient started — using dedicated gesture socket %s", SOCK_PATH)

    def submit(self, frame: np.ndarray):
        if not self.enabled or self._busy:
            return
        import cv2 as _cv2
        # Zmenši na 640x360
        h, w = frame.shape[:2]
        if w > 640:
            frame = _cv2.resize(frame, (640, 360), interpolation=_cv2.INTER_LINEAR)
        # CLAHE — zvýší kontrast, pomůže palm detection
        lab = _cv2.cvtColor(frame, _cv2.COLOR_RGB2LAB)
        l, a, b = _cv2.split(lab)
        clahe = _cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        l = clahe.apply(l)
        lab = _cv2.merge([l, a, b])
        frame = _cv2.cvtColor(lab, _cv2.COLOR_LAB2RGB)
        with self._lock:
            self._pending = frame.copy()

    def connect(self) -> bool:
        # Počkej až do 3s na socket pokud ještě neexistuje
        import os, time as _time
        for _ in range(10):
            if os.path.exists(SOCK_PATH):
                break
            _time.sleep(0.3)
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(3.0)
            s.connect(SOCK_PATH)
            s.settimeout(5.0)
            self._sock      = s
            self._connected = True
            self._fail_cnt  = 0
            _log.info("GestureClient connected to gesture socket")
            print("[Gesture] Connected to gesture socket", flush=True)
            return True
        except Exception as e:
            if self._fail_cnt % 20 == 0:
                _log.warning("Cannot connect to gesture socket: %s", e)
                print(f"[Gesture] Connect failed: {e}", flush=True)
            self._fail_cnt += 1
            return False

    def is_connected(self) -> bool:
        return self._connected

    def reload_config(self, config: dict):
        cfg = config.get("gesture", {})
        self._hold_frames = int(cfg.get("hold_frames",  self._hold_frames))
        self._cooldown_s  = float(cfg.get("cooldown_s", self._cooldown_s))
        self.enabled      = bool(cfg.get("enabled",     self.enabled))

    def _recv_exact(self, sock, n):
        buf = b""
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                return None
            buf += chunk
        return buf

    def _loop(self):
        while True:
            with self._lock:
                frame = self._pending
                self._pending = None
            if frame is None:
                time.sleep(0.02)
                continue
            self._busy = True
            try:
                result = self._send_frame(frame)
                if result is not None:
                    gesture_id, bbox = result[0], result[1]
                    lm = result[2] if len(result) > 2 else None
                    self._update_state(gesture_id, bbox, lm)
            except Exception:
                pass
            finally:
                self._busy = False

    def _send_frame(self, frame: np.ndarray):
        for _ in range(2):
            if not self._connected:
                if not self.connect():
                    return None
            try:
                h, w  = frame.shape[:2]
                data  = frame.tobytes()
                self._sock.sendall(struct.pack(">I", len(data)))
                self._sock.sendall(data)
                self._sock.sendall(struct.pack(">HH", w, h))
                resp = self._sock.recv(1)
                if not resp:
                    raise ConnectionError("no response")
                gesture_id = struct.unpack("B", resp)[0]
                # Receive palm bbox
                bbox_raw = self._recv_exact(self._sock, 16)
                if bbox_raw and len(bbox_raw) == 16:
                    bbox = struct.unpack('>ffff', bbox_raw)
                    if all(v == 0.0 for v in bbox):
                        bbox = None
                else:
                    bbox = None
                # Receive landmarks (63 floats = 252 bytes)
                lm_raw = self._recv_exact(self._sock, 252)
                if lm_raw and len(lm_raw) == 252:
                    lm = struct.unpack('>63f', lm_raw)
                    if all(v == 0.0 for v in lm):
                        lm = None
                else:
                    lm = None
                return gesture_id, bbox, lm, lm
            except Exception as e:
                _log.debug("Gesture send error: %s — reconnecting", e)
                try: self._sock.close()
                except Exception: pass
                self._sock      = None
                self._connected = False
        return None

    def _update_state(self, gesture_id: int, bbox=None, lm=None):
        now = time.time()

        # Warmup — ignoruj první frames po startu
        if self._warmup_frames > 0:
            self._warmup_frames -= 1
            return

        # Majority vote z posledních 5 framů
        self._vote_buf.append(gesture_id)

        # Reset po 5 nulách
        if gesture_id == GESTURE_NONE:
            self._none_count = getattr(self, '_none_count', 0) + 1
            if self._none_count > 5:
                self._current         = GESTURE_NONE
                self._last_fired_name = None
                self._vote_buf.clear()
                self.last_landmarks   = None
            return
        self._none_count = 0
        if lm is not None:
            self.last_landmarks = lm   # průběžná aktualizace

        # Potřebujeme alespoň 4 ze 5 stejných
        if len(self._vote_buf) < 5:
            return
        counts = Counter(self._vote_buf)
        voted, cnt = counts.most_common(1)[0]
        if voted == GESTURE_NONE or cnt < 4:
            return

        name = GESTURE_NAMES.get(voted)
        if not name:
            return

        if voted != self._current:
            self._current         = voted
            self._current_since   = now
            self._last_fired_name = None
            self._vote_buf.clear()   # vymaž buffer při změně gesta

        # Per-gesture hold time
        required_hold = self._hold_per_gesture.get(voted, self._hold_seconds)
        held = now - self._current_since
        if (held >= required_hold and
                name != self._last_fired_name and
                self.on_gesture):
            self._last_fired_name  = name
            self.last_gesture      = name
            self.last_gesture_time = now
            self.last_landmarks    = lm   # uložení pro vykreslení
            self._vote_buf.clear()
            print(f"[Gesture] FIRED: {name} votes={cnt}/5", flush=True)
            self.on_gesture(name, bbox)
