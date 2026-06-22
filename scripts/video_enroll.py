#!/usr/bin/env python3
"""Video enrollment session — sbírá embeddingy po N sekund.

Použití (z display_controller_picam):
    from scripts.video_enroll import VideoEnrollSession
    session = VideoEnrollSession(name="alice", duration_s=30,
                                  enroll_manager=self.enrollment_manager)
    session.start()
    # v každém frame loopu:
    session.feed(main_frame, lores_frame, num_faces=len(boxes))
    # session sama detekuje konec a otevře review window
"""
from __future__ import annotations

import logging
import time
import uuid
from pathlib import Path
from typing import Optional

# VIDEO_ENROLL_MARKER

_log = logging.getLogger("video_enroll")


class VideoEnrollSession:
    """Sbírá embeddingy z hlavní smyčky po N sekund (single-face filter).
    Na konci spustí UnknownEnrollmentWindow pro review."""

    def __init__(self, name: str, duration_s: int,
                 enroll_manager, ui=None, target_fps: float = 5.0,
                 phases: Optional[list] = None, on_phase_start=None):
        """# MULTIPHASE_V2"""
        self.name = name.strip().lower()
        self.duration_s = max(5, min(int(duration_s), 120))
        self.ctrl = enroll_manager
        self.ui = ui
        self.target_fps = target_fps
        self.frame_interval = 1.0 / target_fps if target_fps > 0 else 0.2

        if phases:
            self.phases = phases
        else:
            self.phases = [{"name": "default", "seconds": self.duration_s,
                            "lens": None, "tts": None}]
        self.duration_s = sum(int(p.get("seconds", 30)) for p in self.phases)
        self._phase_idx = -1
        self._phase_started_at = 0.0
        self._on_phase_start = on_phase_start

        self._started_at: Optional[float] = None
        self._last_capture_at = 0.0
        self._embeddings: list = []
        self._crops: list = []
        self._n_no_face = 0
        self._n_multi_face = 0
        self._n_captured = 0
        self._n_rejected = 0
        self._done = False

    def start(self):
        """Spustí session — od teď přijímá feed()."""
        self._started_at = time.time()
        _log.info("Video enroll start: name=%s duration=%ds fps=%.1f",
                  self.name, self.duration_s, self.target_fps)
        if self.ui:
            try:
                self.ui.toast(f"Video enroll '{self.name}' — {self.duration_s}s",
                              duration=3.0, color=(0, 220, 255))
            except Exception:
                pass

    @property
    def active(self) -> bool:
        return self._started_at is not None and not self._done

    @property
    def elapsed(self) -> float:
        if not self._started_at:
            return 0.0
        return time.time() - self._started_at

    @property
    def remaining(self) -> float:
        return max(0.0, self.duration_s - self.elapsed)

    def _advance_phase(self) -> bool:
        """# MULTIPHASE_V2 Posune na další fázi. False = konec."""
        self._phase_idx += 1
        if self._phase_idx >= len(self.phases):
            return False
        phase = self.phases[self._phase_idx]
        self._phase_started_at = time.time()
        _log.info("Phase %d/%d: %s (%ds)",
                  self._phase_idx + 1, len(self.phases),
                  phase.get("name"), phase.get("seconds", 30))
        if self._on_phase_start:
            try:
                self._on_phase_start(phase)
            except Exception as e:
                _log.warning("on_phase_start failed: %s", e)
        return True

    @property
    def current_phase(self) -> dict:
        if 0 <= self._phase_idx < len(self.phases):
            return self.phases[self._phase_idx]
        return {}

    @property
    def phase_elapsed(self) -> float:
        if self._phase_started_at == 0:
            return 0.0
        return time.time() - self._phase_started_at

    def feed(self, main_frame, lores_frame, num_faces: int) -> bool:
        """Volá se z hlavní smyčky. Vrátí True pokud session ještě běží."""
        if not self.active:
            return False

        # První frame — start první fáze
        if self._phase_idx < 0:
            if not self._advance_phase():
                self._finalize()
                return False

        # Konec aktuální fáze?
        if self.phase_elapsed >= self.current_phase.get("seconds", 30):
            if not self._advance_phase():
                self._finalize()
                return False

        # Throttle (cca target_fps)
        now = time.time()
        if now - self._last_capture_at < self.frame_interval:
            return True
        self._last_capture_at = now

        # Single-face check
        if num_faces == 0:
            self._n_no_face += 1
            return True
        if num_faces > 1:
            self._n_multi_face += 1
            return True

        # Extract embedding
        try:
            emb, crop = self.ctrl._capture_hq_embedding(main_frame, lores_frame)
        except Exception as e:
            _log.warning("Embedding extraction failed: %s", e)
            return True

        if emb is None:
            return True

        # # QUALITY_FILTER_V2 — Quality filter
        try:
            import numpy as _np
            emb_norm = float(_np.linalg.norm(emb))
        except Exception:
            emb_norm = 0.0
        MIN_EMB_NORM = 0.5

        blur_var = 999.0
        if crop is not None:
            try:
                import cv2 as _cv2
                _gray = (_cv2.cvtColor(crop, _cv2.COLOR_RGB2GRAY)
                         if len(crop.shape) == 3 else crop)
                blur_var = float(_cv2.Laplacian(_gray, _cv2.CV_64F).var())
            except Exception:
                blur_var = 999.0
        MIN_BLUR_VAR = 15.0

        rejected = []
        if emb_norm < MIN_EMB_NORM:
            rejected.append(f"weak_emb({emb_norm:.1f})")
        if blur_var < MIN_BLUR_VAR:
            rejected.append(f"blur({blur_var:.0f})")

        if rejected:
            self._n_rejected += 1
            if self._n_rejected % 5 == 0:
                _log.info("Quality reject %d: %s",
                          self._n_rejected, ",".join(rejected))
            return True

        self._embeddings.append(emb)
        if crop is not None:
            self._crops.append(crop)
        self._n_captured += 1

        # Toast každých 10
        if self.ui and self._n_captured % 10 == 0:
            try:
                self.ui.toast(
                    f"Captured {self._n_captured} "
                    f"(rej={self._n_rejected})",
                    duration=1.0, color=(0, 220, 0),
                )
            except Exception:
                pass

        return True

    def _finalize(self):
        """Konec capture — otevři review window."""
        self._done = True
        elapsed = self.elapsed
        _log.info(
            "Video enroll finished: captured=%d, rejected=%d, "
            "no_face=%d, multi_face=%d, elapsed=%.1fs",
            self._n_captured, self._n_rejected,
            self._n_no_face, self._n_multi_face, elapsed,
        )

        if not self._embeddings:
            msg = (f"Video enroll '{self.name}': žádný validní frame "
                   f"(no_face={self._n_no_face}, multi={self._n_multi_face}). "
                   f"Zkus znovu ve světle a sám v záběru.")
            _log.warning(msg)
            if self.ui:
                try:
                    self.ui.toast(msg, duration=5.0, color=(0, 80, 255))
                except Exception:
                    pass
            return

        # Otevři review window přes existující flow
        try:
            from scripts.unknown_enrollment_window import UnknownEnrollmentWindow
            import cv2

            session_dir = (Path("data/unknown_faces")
                           / f"video_enroll_{self.name}_{int(time.time())}")
            session_dir.mkdir(parents=True, exist_ok=True)

            # Ulož crops na disk
            for idx, crop in enumerate(self._crops):
                if crop is None:
                    continue
                img_path = session_dir / f"{idx:03d}.jpg"
                try:
                    # crop je RGB → BGR pro cv2.imwrite
                    bgr = cv2.cvtColor(crop, cv2.COLOR_RGB2BGR)
                    cv2.imwrite(str(img_path), bgr,
                                [cv2.IMWRITE_JPEG_QUALITY, 90])
                except Exception as e:
                    _log.debug("imwrite crop %d failed: %s", idx, e)

            # Callback po Enroll button.
            # # VIDEO_ENROLL_CALLBACK_FIX_V2
            # UnknownEnrollmentWindow volá on_done(name) nebo on_done(None).
            # Okno SAMO volá face_db.add() pro každý embedding — nesmím
            # to dělat znovu. Jen zavolám normalize_person pro dedup.
            def _on_done(name_or_none):
                try:
                    import shutil
                    if not name_or_none:
                        _log.info("Video enroll cancelled (window closed without enroll)")
                        return
                    target_name = str(name_or_none).strip().lower()
                    face_db = getattr(self.ctrl, "ctrl", None)
                    face_db = getattr(face_db, "face_db", None)
                    if not face_db:
                        _log.warning("face_db not accessible — skipping dedup")
                        return
                    try:
                        removed = face_db.normalize_person(target_name)
                    except Exception as de:
                        _log.warning("normalize_person failed: %s", de)
                        removed = 0
                    _log.info(
                        "Video enroll committed: %s (dedup -%d)",
                        target_name, removed,
                    )
                    if self.ui:
                        try:
                            self.ui.toast(
                                f"Enroll '{target_name}' OK (dedup -{removed})",
                                duration=4.0, color=(0, 220, 0),
                            )
                        except Exception:
                            pass
                except Exception as e:
                    _log.error("_on_done error: %s", e)
                finally:
                    try:
                        shutil.rmtree(session_dir)
                    except Exception:
                        pass

            # Spusť okno (musí běžet z GUI threadu)
            face_db_ref = getattr(self.ctrl, "ctrl", None)
            face_db_ref = getattr(face_db_ref, "face_db", None)
            UnknownEnrollmentWindow(session_dir, self._embeddings, face_db_ref,
                                    on_done=_on_done)

        except Exception as e:
            _log.error("Review window failed: %s", e)
            if self.ui:
                try:
                    self.ui.toast(f"Review window failed: {e}",
                                  duration=4.0, color=(0, 80, 255))
                except Exception:
                    pass
