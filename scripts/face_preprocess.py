"""
Face Frame Preprocessor
Jemné zvýraznění kontrastu / jasu pro SCRFD detekci a ArcFace embeddingy.

Aktivuje se pouze v horším osvětlení (jas pod prahem) — v dobrém světle
je výstup identický s vstupem, žádný výkonnostní dopad.

Config (v config.json pod "face_preprocess"):
    enabled         : bool  — master switch (default false)
    clahe_lores     : bool  — CLAHE na lores frame před detekcí (default true)
    clahe_clip      : float — clipLimit (default 1.5)
    clahe_tile      : int   — tileGridSize (default 16)
    gamma_crop      : bool  — gamma na crop před ArcFace (default true)
    gamma_auto      : bool  — automatická gamma podle jasu (default true)
    gamma_fixed     : float — fixní gamma pokud auto=false (default 1.2)
    gamma_threshold : int   — pod tímto průměrným jasem se aktivuje (default 100)
    lores_threshold : int   — lores: aktivuje CLAHE pod tímto jasem (default 160)
"""

import cv2
import numpy as np


def _build_lut(gamma: float) -> np.ndarray:
    inv = 1.0 / max(gamma, 0.1)
    return np.array(
        [min(255, int((i / 255.0) ** inv * 255)) for i in range(256)],
        dtype=np.uint8,
    )


_LUT_CACHE: dict[float, np.ndarray] = {}


def _get_lut(gamma: float) -> np.ndarray:
    key = round(gamma, 2)
    if key not in _LUT_CACHE:
        _LUT_CACHE[key] = _build_lut(key)
    return _LUT_CACHE[key]


class FacePreprocessor:
    """
    Preprocessing pipeline pro face detection a recognition.

    Použití:
        prep = FacePreprocessor(config)

        # před hailo.infer():
        lores_proc = prep.enhance_lores(lores_frame)

        # před hailo.embed_faces() — na již oříznutý 112×112 crop:
        crop_proc  = prep.enhance_crop(crop)
    """

    def __init__(self, config: dict):
        self._reload(config)

    def _reload(self, config: dict):
        self.config         = config
        cfg = config.get("face_preprocess", {})
        self.enabled        = bool(cfg.get("enabled",          False))
        self._clahe_crop    = bool(cfg.get("clahe_crop",       False))
        self._clahe_crop_clip = float(cfg.get("clahe_crop_clip", 1.5))
        self._clahe_lores   = bool(cfg.get("clahe_lores",      True))
        self._clahe_clip    = float(cfg.get("clahe_clip",       1.5))
        self._clahe_tile    = int(cfg.get("clahe_tile",         16))
        self._gamma_crop    = bool(cfg.get("gamma_crop",        True))
        self._gamma_auto    = bool(cfg.get("gamma_auto",        True))
        self._gamma_fixed   = float(cfg.get("gamma_fixed",      1.2))
        self._gamma_thresh  = int(cfg.get("gamma_threshold",    100))
        self._lores_thresh  = int(cfg.get("lores_threshold",    160))
        self._clahe = cv2.createCLAHE(
            clipLimit=self._clahe_clip,
            tileGridSize=(self._clahe_tile, self._clahe_tile),
        )

    def reload_config(self, config: dict):
        self._reload(config)

    # ── Lores frame (před SCRFD detekcí) ──────────────────────────────────────

    def enhance_lores(self, frame: np.ndarray) -> np.ndarray:
        """
        CLAHE na L kanál LAB — zvýší kontrast v tmavé scéně.
        Jemnější parametry než u gest (clip 1.5, tile 16) aby nebyla
        přesaturována textura obličeje.
        Přeskočí pokud průměrný jas > lores_threshold.
        """
        if not self.enabled or not self._clahe_lores:
            return frame
        if int(np.mean(frame)) > self._lores_thresh:
            return frame
        lab = cv2.cvtColor(frame, cv2.COLOR_RGB2LAB)
        l, a, b = cv2.split(lab)
        l = self._clahe.apply(l)
        return cv2.cvtColor(cv2.merge([l, a, b]), cv2.COLOR_LAB2RGB)

    # ── Face crop (před ArcFace embeddings) ───────────────────────────────────

    def enhance_crop(self, crop: np.ndarray) -> np.ndarray:
        """
        Gamma korekce na 112×112 crop před ArcFace.
        Gamma (ne CLAHE) zachová relativní textury kůže.
        Automatická gamma: čím tmavší crop, tím větší korekce (max 2.0).
        Přeskočí pokud průměrný jas >= gamma_threshold.
        """
        if not self.enabled:
            return crop

        # CLAHE na crop — pomaha se stiny z bodoveho osvetleni
        if self._clahe_crop:
            lab = cv2.cvtColor(crop, cv2.COLOR_RGB2LAB)
            l, a, b = cv2.split(lab)
            clahe = cv2.createCLAHE(clipLimit=self._clahe_crop_clip, tileGridSize=(4, 4))
            l = clahe.apply(l)
            crop = cv2.cvtColor(cv2.merge([l, a, b]), cv2.COLOR_LAB2RGB)

        if not self._gamma_crop:
            return crop

        mean_lum = int(np.mean(crop))

        if self._gamma_auto:
            if mean_lum >= self._gamma_thresh:
                return crop
            # lineární škálování: jas 0 → gamma 2.0, jas=thresh → gamma 1.0
            gamma = 1.0 + (1.0 - mean_lum / max(self._gamma_thresh, 1))
            gamma = min(gamma, 2.0)
        else:
            if mean_lum >= self._gamma_thresh:
                return crop
            gamma = self._gamma_fixed

        return cv2.LUT(crop, _get_lut(gamma))
