"""
Picamera2 setup + autofokus controller.

Encapsulates:
  - Camera tuning file resolution (v2 vs v3_wide NoIR)
  - Autofocus mode setup (continuous / triggered / manual)
  - Smart AF re-trigger based on face position, time, and bbox size changes

Used by display_controller_picam.py.
"""
import time
from picamera2 import Picamera2


# ── Konstanty ────────────────────────────────────────────────────────────────

_CAM_TUNING = {
    "v2":      None,
    "v3_wide": "/usr/share/libcamera/ipa/rpi/pisp/imx708_wide_noir.json",
}

_AF_MODE_MANUAL     = 0
_AF_MODE_AUTO       = 1
_AF_MODE_CONTINUOUS = 2

# Default lens position pro manuální AF (override v configu)
_DEFAULT_LENS_POS = 1.0


# ── Tuning ───────────────────────────────────────────────────────────────────

def get_tuning_for_model(config: dict):
    """Vrátí Picamera2 tuning objekt pro nakonfigurovaný model, nebo None."""
    model = config.get("camera_model", "v2")
    path = _CAM_TUNING.get(model)
    if not path:
        return None
    try:
        tuning = Picamera2.load_tuning_file(path)
        print(f"[PicamCamera] Loaded tuning: {path}")
        return tuning
    except Exception as e:
        print(f"[PicamCamera] Could not load tuning '{path}': {e}")
        return None


def apply_camera_model(picam2, config: dict):
    """No-op — AF se nastavuje až po picam2.start() přes setup_af()."""
    pass


# ── Autofokus setup (po startu) ──────────────────────────────────────────────

def setup_af(picam2, config: dict):
    """Volá se PO picam2.start(). Nastaví AF mode podle configu."""
    if config.get("camera_model") != "v3_wide":
        return
    mode = config.get("autofocus_mode", "triggered")
    try:
        if mode == "continuous":
            picam2.set_controls({"AfMode": _AF_MODE_CONTINUOUS})
            print("[AF] continuous mode")
        elif mode == "manual":
            lens = float(config.get("autofocus_lens_position", _DEFAULT_LENS_POS))
            picam2.set_controls({
                "AfMode":       _AF_MODE_MANUAL,
                "LensPosition": lens,
            })
            print(f"[AF] manual mode, LensPosition={lens}")
        else:  # triggered
            picam2.set_controls({"AfMode": _AF_MODE_AUTO})
            print("[AF] triggered mode — will focus on first face")
    except Exception as e:
        print(f"[AF] setup failed: {e}")


# ── Autofokus controller — držitel stavu pro re-trigger logiku ───────────────

class AutoFocusController:
    """
    Spravuje "smart" re-triggers AF:
      - když se nejbližší tvář pohne (změna pozice)
      - periodicky (autofocus.retrigger_s, default 5s)
      - při významné změně velikosti bbox (osoba se přiblížila/oddálila)
    """

    def __init__(self):
        self._face_was_visible = False
        self._last_nearest_key = None
        self._last_trigger_t   = 0.0
        self._last_area        = 0.0

    def reset(self):
        """Reset stavu (např. při enrollmentu)."""
        self._face_was_visible = False
        self._last_nearest_key = None
        self._last_trigger_t   = 0.0
        self._last_area        = 0.0

    def update(self, picam2, has_face: bool, config: dict, boxes=None):
        """
        Hlavní entry point. Volá se každý frame s informací o detekovaných tvářích.

        Args:
          picam2:     Picamera2 instance
          has_face:   bool — alespoň jedna tvář detekována tento frame
          config:     globální config dict
          boxes:      seznam normalizovaných bbox [x1,y1,x2,y2] tváří, nebo None
        """
        if config.get("camera_model") != "v3_wide":
            return
        if config.get("autofocus_mode", "triggered") != "triggered":
            return

        af_cfg      = config.get("autofocus", {})
        retrigger_s = float(af_cfg.get("retrigger_s",        5.0))
        size_thresh = float(af_cfg.get("size_change_thresh", 0.3))

        try:
            if not has_face:
                if self._face_was_visible:
                    picam2.set_controls({"AfMode": _AF_MODE_AUTO, "AfTrigger": 1})
                    self._last_nearest_key = None
                    self._last_area = 0.0
                self._face_was_visible = False
                return

            self._face_was_visible = True
            if not boxes:
                return

            def _area(b): return (b[2] - b[0]) * (b[3] - b[1])

            cx = round(sum((b[0] + b[2]) / 2 for b in boxes) / len(boxes), 2)
            cy = round(sum((b[1] + b[3]) / 2 for b in boxes) / len(boxes), 2)
            cur_area = sum(_area(b) for b in boxes) / len(boxes)
            nearest_key = (cx, cy)

            now = time.time()
            person_changed = nearest_key != self._last_nearest_key
            time_elapsed   = (now - self._last_trigger_t) >= retrigger_s
            size_changed   = (self._last_area > 0 and
                              abs(cur_area - self._last_area) / self._last_area
                              > size_thresh)

            if person_changed or time_elapsed or size_changed:
                picam2.set_controls({"AfMode": _AF_MODE_AUTO, "AfTrigger": 0})
                self._last_nearest_key = nearest_key
                self._last_trigger_t   = now
                self._last_area        = cur_area
        except Exception:
            pass


# ── Singleton + module-level funkce pro zpětnou kompatibilitu ────────────────
# display_controller_picam.py volá _update_af(picam2, has_face, config, boxes)
# jako module-level funkci. Držíme tu signaturu přes singleton.

_af_singleton = AutoFocusController()


def update_af(picam2, has_face: bool, config: dict, boxes=None):
    """Module-level wrapper kolem singleton AutoFocusController."""
    _af_singleton.update(picam2, has_face, config, boxes)
