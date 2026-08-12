"""
PC_EMBED_V1 — klient k float embedding službě na PC.

PROČ (změřeno 9.–11. 8. 2026, poctivý den-holdout: galerie 9.+10.8., dotazy 11.8.):
    HAILO int8    46 % správně / 14 % záměn
    PC r50 float  73 % správně /  7 % záměn
Zisk +26 p.b. a poloviční záměny. Není to architekturou, ale PŘESNOSTÍ výpočtu —
tentýž r50 v int8 na Hailu je stejně slabý jako mobilefacenet.

⚠️ FALLBACK NENÍ VOLITELNÝ. PC se v noci vypíná (`pc_night_shutdown`), takže
tenhle klient MUSÍ umět tiše selhat a nechat rozhodnout Hailo. Rozpoznávání je
nepřetržitá funkce; kdyby na PC viselo, Hans by v noci oslepl.

Návrh:
  • krátký timeout — radši Hailo hned než čekat na mrtvý PC
  • jistič: po N chybách se přestane zkoušet na `cooldown_s`, ať se nezahltí
    log ani main loop opakovanými pokusy (vzor `_log_circuit`)
  • spojení přes SSH tunel na 127.0.0.1 — služba na PC není vystavená do sítě
    (na PC běží firewalld i OpenSnitch a služba nemá ověřování)
"""

from __future__ import annotations

import logging
import threading
import time
import urllib.error
import urllib.request

import numpy as np

log = logging.getLogger("pc_embed")

SIZE = 112
DIM = 512


class PCEmbedder:
    """Vrací float embeddingy z PC, nebo None. None = použij Hailo."""

    def __init__(self, config: dict | None = None):
        cfg = (config or {}).get("pc_embed", {})
        self.enabled = bool(cfg.get("enabled", False))
        self._url = cfg.get("url", "http://127.0.0.1:8765/embed")
        self._timeout = float(cfg.get("timeout_s", 2.0))
        self._fail_max = int(cfg.get("fail_before_open", 3))
        self._cooldown = float(cfg.get("cooldown_s", 60.0))

        self._lock = threading.Lock()
        self._fails = 0
        self._open_until = 0.0
        self._stats = {"ok": 0, "fail": 0, "skip": 0}

    # ── stav ─────────────────────────────────────────────────────────────

    @property
    def available(self) -> bool:
        """Je jistič sepnutý? (neznamená, že PC odpoví — jen že to má cenu zkusit)"""
        return self.enabled and time.time() >= self._open_until

    def stats(self) -> dict:
        return dict(self._stats, jistic_otevren=not self.available)

    # ── hlavní API ───────────────────────────────────────────────────────

    def embed(self, crops: list) -> np.ndarray | None:
        """crops = seznam 112×112×3 RGB uint8. Vrací (N,512) float32 nebo None.

        None znamená „PC teď nejde" — volající MUSÍ mít záložní cestu.
        """
        if not crops or not self.available:
            if crops:
                self._stats["skip"] += 1
            return None
        try:
            arr = np.ascontiguousarray(
                np.stack([self._fix(c) for c in crops]), dtype=np.uint8)
            req = urllib.request.Request(
                self._url, data=arr.tobytes(),
                headers={"Content-Type": "application/octet-stream"})
            with urllib.request.urlopen(req, timeout=self._timeout) as r:
                raw = r.read()
            out = np.frombuffer(raw, np.float32)
            if out.size != len(crops) * DIM:
                raise ValueError(f"čekal {len(crops)*DIM} floatů, přišlo {out.size}")
            with self._lock:
                self._fails = 0
                self._stats["ok"] += len(crops)
            return out.reshape(len(crops), DIM)
        except Exception as e:
            self._trip(e)
            return None

    # ── vnitřek ──────────────────────────────────────────────────────────

    @staticmethod
    def _fix(c):
        import cv2
        if c.shape[:2] != (SIZE, SIZE):
            c = cv2.resize(c, (SIZE, SIZE))
        return c

    def _trip(self, err):
        with self._lock:
            self._fails += 1
            self._stats["fail"] += 1
            if self._fails >= self._fail_max:
                self._open_until = time.time() + self._cooldown
                self._fails = 0
                # jedna hláška za cooldown, ne za každý pokus — v noci by
                # to jinak zaplavilo log na celé hodiny
                log.info("PC embedding nedostupný (%s) → %.0f s jedu na Hailo",
                         type(err).__name__, self._cooldown)
