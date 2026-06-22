"""
Unknown Face Collector
Sbírá crops a embeddingy neznámých tváří.
Okno se otevře ihned po prvním snímku.
Sbírání se zastaví po target_count snímcích.
"""

import cv2
try:
    from scripts.logger import get_logger
    _uclog = get_logger('unknown_collector')
except ImportError:
    import logging as _logging
    _uclog = _logging.getLogger('unknown_collector')
import uuid
import time
import numpy as np
from pathlib import Path


_UNKNOWN_DIR = Path("data/unknown_faces")


class UnknownFaceCollector:
    """
    Sleduje neznámé tváře a sbírá jejich snímky.
    - Okno se otevře ihned po prvním zachyceném snímku.
    - Sbírání se tvrdě zastaví po target_count snímcích.
    - Okno zobrazuje snímky živě jak přicházejí.
    """

    def __init__(self, config: dict, face_db, hailo_client,
                 recognizer=None):
        # autoenroll_seed_patch
        self.config     = config
        self.face_db    = face_db
        self.hailo      = hailo_client
        self._recognizer = recognizer  # AsyncRecognizer ref for seed_name

        self._last_seen_unknown: float = 0.0
        self._reset_grace: float = 2.0   # seconds of no unknown before resetting

        self._reload_cfg(config)
        _UNKNOWN_DIR.mkdir(parents=True, exist_ok=True)

        # Session state
        self._session_id   : str | None  = None
        self._session_dir  : Path | None = None
        self._crops        : list        = []   # Path list
        self._embeddings   : list        = []   # np.ndarray list
        self._last_done    : float       = 0.0
        self._last_capture : float       = 0.0
        self._window_open  : bool        = False
        self._enrollment_win             = None
        self._collecting_done: bool      = False

        print(f"[UnknownCollector] target={self._target}  "
              f"min_area={self._min_area}  cooldown={self._cooldown}s  "
              f"interval={self._capture_interval}s")

    def _reload_cfg(self, config: dict):
        unk_cfg = config.get("unknown_enrollment", {})
        self._target           = int(unk_cfg.get("target_count", 20))
        self._open_after       = int(unk_cfg.get("open_after", 3))
        self._min_area         = float(unk_cfg.get("min_face_area", 0.01))
        self._cooldown         = float(unk_cfg.get("cooldown_s", 300))
        self._enabled          = bool(unk_cfg.get("enabled", True))
        self._capture_interval = float(unk_cfg.get("capture_interval_s", 0.5))

    # ── Public API ────────────────────────────────────────────────────────────

    def process(self, main_frame: np.ndarray, boxes: list, identities: list):
        if not self._enabled:
            return
        # Block new captures while window is open and done collecting
        if self._window_open and self._collecting_done:
            return
        if time.time() - self._last_done < self._cooldown:
            return

        # Find unknown faces
        unknown_boxes = [
            b for b, (name, _) in zip(boxes, identities)
            if name in ("Unknown", "...", "?", "")
        ]

        if not unknown_boxes:
            # No unknown face — reset session only if window is NOT open
            if (self._session_id and not self._window_open and
                    time.time() - self._last_seen_unknown > self._reset_grace):
                self._reset_session()
            return

        self._last_seen_unknown = time.time()

        # Already collected enough
        if len(self._crops) >= self._target:
            if not self._collecting_done:
                self._collecting_done = True
                if self._enrollment_win is not None:
                    self._enrollment_win.set_collecting_done()
            return

        # Pick largest unknown face
        best = max(unknown_boxes, key=lambda b: (b[2]-b[0]) * (b[3]-b[1]))
        area = (best[2]-best[0]) * (best[3]-best[1])
        if area < self._min_area:
            return

        # Start session on first unknown face
        if self._session_id is None:
            self._start_session()

        # Throttle captures
        if time.time() - self._last_capture < self._capture_interval:
            return

        # Capture frame
        img_path, emb = self._capture(main_frame, best)
        if img_path is None:
            return

        # Open window after open_after captures
        if not self._window_open and len(self._crops) >= self._open_after:
            self._open_enrollment_window()
        elif self._window_open and self._enrollment_win is not None:
            # Push new photo to live window
            self._enrollment_win.add_photo(img_path, emb)

        # Check if we just hit the target
        if len(self._crops) >= self._target:
            self._collecting_done = True
            if self._enrollment_win is not None:
                self._enrollment_win.set_collecting_done()
            _uclog.info("Collecting done — %d frames captured", len(self._crops))

    def reload_config(self, config: dict):
        self.config = config
        self._reload_cfg(config)

    # ── Internal ──────────────────────────────────────────────────────────────

    def _start_session(self):
        self._session_id     = str(uuid.uuid4())[:8]
        self._session_dir    = _UNKNOWN_DIR / self._session_id
        self._session_dir.mkdir(parents=True, exist_ok=True)
        self._crops          = []
        self._embeddings     = []
        self._last_capture   = 0.0
        self._collecting_done = False
        _uclog.info("New unknown session %s", self._session_id)

    def _reset_session(self):
        import shutil
        if self._session_dir and self._session_dir.exists():
            try:
                shutil.rmtree(self._session_dir)
            except Exception:
                pass
        self._session_id     = None
        self._session_dir    = None
        self._crops          = []
        self._embeddings     = []
        self._last_capture   = 0.0
        self._collecting_done = False

    def _capture(self, main_frame: np.ndarray, box: list):
        """
        Capture aligned crop + embedding.
        Returns (img_path, embedding) or (None, None).

        Pipeline:
          1. Ořízne region z main_frame podle bbox (s paddingem)
          2. Spustí hailo.infer() na zmenšenou verzi regionu
             → získá přesný bbox s možností aligned crop
          3. Aligned crop přes similarity transform (stejně jako manuální enrollment)
          4. Fallback: padded crop pokud infer nenajde obličej
          5. Loguje normu embeddings pro diagnostiku
        """
        try:
            import numpy as _np
            from scripts.hailo_client import ARCFACE_SIZE, LABEL_FACE
            from scripts.async_recognizer import _ARCFACE_REF

            H, W = main_frame.shape[:2]
            x1, y1, x2, y2 = box
            bw, bh = x2 - x1, y2 - y1

            # ── Preview crop (160×160) pro zobrazení v okně ───────────────
            pad = 0.20
            ix1 = max(0, int((x1 - bw * pad) * W))
            iy1 = max(0, int((y1 - bh * pad) * H))
            ix2 = min(W, int((x2 + bw * pad) * W))
            iy2 = min(H, int((y2 + bh * pad) * H))
            if ix2 <= ix1 or iy2 <= iy1:
                return None, None

            context_crop = cv2.resize(main_frame[iy1:iy2, ix1:ix2],
                                      (160, 160),
                                      interpolation=cv2.INTER_LINEAR)
            idx      = len(self._crops)
            img_path = self._session_dir / f"{idx:03d}.jpg"
            cv2.imwrite(str(img_path), context_crop)

            # ── Aligned crop pro ArcFace ──────────────────────────────────
            # Pokus 1: aligned crop přes přibližné landmarks z bbox
            face_crop = self._make_aligned_crop(main_frame, box, H, W, ARCFACE_SIZE)

            # Pokus 2: fallback — padded crop (lepší než prostý resize)
            if face_crop is None:
                bpad = 0.10
                fx1 = max(0, int((x1 - bw * bpad) * W))
                fy1 = max(0, int((y1 - bh * bpad) * H))
                fx2 = min(W, int((x2 + bw * bpad) * W))
                fy2 = min(H, int((y2 + bh * bpad) * H))
                if fx2 > fx1 and fy2 > fy1:
                    face_crop = cv2.resize(
                        main_frame[fy1:fy2, fx1:fx2],
                        (ARCFACE_SIZE, ARCFACE_SIZE),
                        interpolation=cv2.INTER_LINEAR)

            if face_crop is None:
                _uclog.warning("Capture: no valid crop for box %s", box)
                return None, None

            # ── Preprocessing (gamma) pokud zapnut ───────────────────────
            try:
                from scripts.face_preprocess import FacePreprocessor as _FP
                if not hasattr(self, '_face_prep'):
                    self._face_prep = _FP(self.config)
                face_crop = self._face_prep.enhance_crop(face_crop)
            except Exception:
                pass

            # ── Embedding ─────────────────────────────────────────────────
            emb_list = self.hailo.embed_faces([face_crop])
            emb = emb_list[0] if emb_list and emb_list[0] is not None else None

            if emb is not None:
                norm = float(_np.linalg.norm(emb))
                n = len(self._crops) + 1
                if n % 5 == 0 or norm < 0.4:
                    _uclog.info("Unknown collector: %d/%d frames, norm=%.3f",
                                n, self._target, norm)
                if norm < 0.2:
                    _uclog.warning("Very low norm %.3f — discarding", norm)
                    emb = None
            else:
                _uclog.warning("Capture: embed_faces returned None")

            self._crops.append(img_path)
            self._embeddings.append(emb)
            self._last_capture = time.time()

            return img_path, emb

        except Exception as e:
            _uclog.error("Capture error: %s", e)
            import traceback
            _uclog.error(traceback.format_exc())
            return None, None

    @staticmethod
    def _make_aligned_crop(frame: np.ndarray, box: list,
                           H: int, W: int, out_size: int):
        """
        Similarity-transform aligned crop z main_frame.
        Používá přibližné pozice landmarks odvozené z bbox
        (stejná metoda jako async_recognizer._aligned_crop).
        Vrátí None pokud se transform nepodaří.
        """
        try:
            from scripts.async_recognizer import _ARCFACE_REF
            x1, y1, x2, y2 = box
            bx1, by1 = x1 * W, y1 * H
            bx2, by2 = x2 * W, y2 * H
            fw = bx2 - bx1
            fh = by2 - by1
            if fw < 20 or fh < 20:
                return None
            pts_src = np.array([
                [bx1 + fw * 0.30, by1 + fh * 0.37],
                [bx1 + fw * 0.70, by1 + fh * 0.37],
                [bx1 + fw * 0.50, by1 + fh * 0.55],
                [bx1 + fw * 0.35, by1 + fh * 0.75],
                [bx1 + fw * 0.65, by1 + fh * 0.75],
            ], dtype=np.float32)
            M, _ = cv2.estimateAffinePartial2D(
                pts_src, _ARCFACE_REF, method=cv2.LMEDS)
            if M is None:
                return None
            return cv2.warpAffine(frame, M, (out_size, out_size),
                                  flags=cv2.INTER_LINEAR,
                                  borderMode=cv2.BORDER_REPLICATE)
        except Exception:
            return None

    def _open_enrollment_window(self):
        """Open window with the first photo already in it.
        Routes to HeadlessEnrollmentHandler when:
          - display.headless = true, OR
          - headless_enrollment.simulate = true  (for testing with video on)
        """
        self._window_open = True

        def _on_done(name):
            self._window_open     = False
            self._enrollment_win  = None
            self._last_done       = time.time()
            self._session_id      = None
            self._session_dir     = None
            self._crops           = []
            self._embeddings      = []
            self._collecting_done = False
            if name:
                _uclog.info("Enrolled as '%s'", name)
                # reload_after_enroll
                try:
                    self.face_db.reload()
                    if self._recognizer is not None:
                        self._recognizer._slots.clear()
                        self._recognizer.seed_name(name)
                        _uclog.info("[AutoEnroll] DB reloaded")
                except Exception as _e:
                    _uclog.warning("reload failed: %s", _e)
                # normalize_after_enroll
                try:
                    self.face_db.normalize_person(name)
                except Exception as _e:
                    _uclog.warning("normalize_person failed: %s", _e)
                if self._recognizer is not None:
                    try:
                        self._recognizer.seed_name(name)
                        _uclog.info("[AutoEnroll] seed_name OK: %s", name)
                    except Exception as _e:
                        _uclog.warning("seed_name failed: %s", _e)
            else:
                _uclog.info("Enrollment skipped")

        headless = bool(self.config.get("display", {}).get("headless", False))
        simulate = bool(self.config.get("headless_enrollment", {}).get("simulate", False))

        if headless or simulate:
            from scripts.headless_enrollment_handler import HeadlessEnrollmentHandler
            tts = getattr(self, "_tts_speaker", None)
            self._enrollment_win = HeadlessEnrollmentHandler(
                self._session_dir,
                list(self._embeddings),
                self.face_db,
                config       = self.config,
                tts_speaker  = tts,
                on_done      = _on_done,
                target_count = self._target,
            )
            mode = "simulate" if simulate else "headless"
        else:
            from scripts.unknown_enrollment_window import UnknownEnrollmentWindow
            self._enrollment_win = UnknownEnrollmentWindow(
                self._session_dir,
                list(self._embeddings),
                self.face_db,
                on_done      = _on_done,
                target_count = self._target,
            )
            mode = "display"

        _uclog.info(
            "Enrollment window opened (%s mode, 1/%d)",
            mode, self._target,
        )
