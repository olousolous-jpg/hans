"""
Fisheye Corrector
Undistorts frames from wide-angle / fisheye camera lenses.

No calibration checkerboard needed — uses estimated parameters based on
the camera's field of view (FOV). Works well enough for object detection;
for sub-pixel accurate measurements a proper calibration is better.

Config keys (under "fisheye"):
    enabled       : bool   — master switch (default false)
    fov_degrees   : float  — diagonal or horizontal FOV of your lens (default 160)
    balance       : float  — 0.0 = crop to valid pixels, 1.0 = keep full frame
                             with black borders (default 0.5)
    apply_to_lores: bool   — undistort the 640×480 inference frame (default true)
    apply_to_main : bool   — undistort the 1280×960 display frame (default false,
                             expensive — only enable if you need it)
"""

import cv2
import numpy as np


class FisheyeCorrector:

    def __init__(self, config: dict):
        self.config   = config
        fe_cfg        = config.get("fisheye", {})

        self.enabled       = bool(fe_cfg.get("enabled", False))
        # camera_model_fov_patch: use per-model FOV default
        _model_fov = 120.0 if config.get("camera_model", "v2") == "v3_wide" else 62.0
        self._fov          = float(fe_cfg.get("fov_degrees", _model_fov))
        self._balance      = float(fe_cfg.get("balance", 0.5))
        self._apply_lores  = bool(fe_cfg.get("apply_to_lores", True))
        self._apply_main   = bool(fe_cfg.get("apply_to_main", False))

        # Map cache: keyed by (width, height) so we build each map once
        self._maps: dict[tuple, tuple] = {}

        if self.enabled:
            print(f"[Fisheye] Enabled — FOV={self._fov}°  balance={self._balance}  "
                  f"lores={self._apply_lores}  main={self._apply_main}")
        else:
            print("[Fisheye] Disabled")

    # ── Public API ────────────────────────────────────────────────────────────

    def undistort_lores(self, frame: np.ndarray) -> np.ndarray:
        """Undistort the 640×480 inference frame."""
        if not self.enabled or not self._apply_lores:
            return frame
        return self._undistort(frame)

    def undistort_main(self, frame: np.ndarray) -> np.ndarray:
        """Undistort the main (high-res) frame."""
        if not self.enabled or not self._apply_main:
            return frame
        return self._undistort(frame)

    def reload_config(self, config: dict):
        """Hot-reload after settings change. Clears cached maps."""
        self.config      = config
        fe_cfg           = config.get("fisheye", {})
        self.enabled     = bool(fe_cfg.get("enabled", False))
        # camera_model_fov_patch
        _model_fov = 120.0 if config.get("camera_model", "v2") == "v3_wide" else 62.0
        self._fov        = float(fe_cfg.get("fov_degrees", _model_fov))
        self._balance    = float(fe_cfg.get("balance", 0.5))
        self._apply_lores= bool(fe_cfg.get("apply_to_lores", True))
        self._apply_main = bool(fe_cfg.get("apply_to_main", False))
        self._maps.clear()   # force rebuild for new params
        print(f"[Fisheye] Config reloaded — enabled={self.enabled}  FOV={self._fov}°")

    # ── Internal ──────────────────────────────────────────────────────────────

    def _get_maps(self, h: int, w: int) -> tuple:
        """Build (or return cached) remap tables for the given resolution."""
        key = (w, h)
        if key in self._maps:
            return self._maps[key]

        # Estimate camera matrix from FOV
        # For a fisheye lens the focal length relates to FOV as:
        #   f = (w/2) / tan(FOV_h/2)   for rectilinear
        # We use the fisheye model which is closer to:
        #   f ≈ (w/2) / (FOV_rad/2)    (equidistant projection)
        fov_rad = np.deg2rad(self._fov)
        f       = (max(w, h) / 2.0) / (fov_rad / 2.0)

        K = np.array([
            [f,   0,   w / 2.0],
            [0,   f,   h / 2.0],
            [0,   0,   1.0    ],
        ], dtype=np.float64)

        # Distortion coefficients — estimated for a typical wide-angle lens.
        # k1 drives barrel distortion (negative = barrel, positive = pincushion).
        # For a 160° fisheye, k1 ≈ -0.35 is a reasonable starting point.
        # k2-k4 add higher-order correction; keep small to avoid ringing.
        D = np.array([[-0.35], [0.12], [-0.03], [0.0]], dtype=np.float64)

        # New camera matrix — balance controls crop vs black border tradeoff
        new_K = cv2.fisheye.estimateNewCameraMatrixForUndistortRectify(
            K, D, (w, h), np.eye(3),
            balance=self._balance,
            new_size=(w, h),
            fov_scale=1.0,
        )

        map1, map2 = cv2.fisheye.initUndistortRectifyMap(
            K, D, np.eye(3), new_K, (w, h), cv2.CV_16SC2
        )

        self._maps[key] = (map1, map2)
        print(f"[Fisheye] Built remap tables for {w}×{h}  f={f:.1f}px")
        return map1, map2

    def _undistort(self, frame: np.ndarray) -> np.ndarray:
        h, w   = frame.shape[:2]
        m1, m2 = self._get_maps(h, w)
        return cv2.remap(frame, m1, m2,
                         interpolation=cv2.INTER_LINEAR,
                         borderMode=cv2.BORDER_CONSTANT,
                         borderValue=(0, 0, 0))