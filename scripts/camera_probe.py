#!/usr/bin/env python3
"""
Camera probe — runs BEFORE main app, saves sensor modes to data/camera_modes.json.
Called from run.sh before starting the main application.
"""

import json
from pathlib import Path

OUT_PATH = Path("data/camera_modes.json")

# camera_model_probe_patch
_CAM_TUNING_PROBE = {
    "v2":      None,
    "v3_wide": "/usr/share/libcamera/ipa/rpi/pisp/imx708_wide_noir.json",
}

def _load_config_model():
    import json
    for candidate in [OUT_PATH.parent.parent / "config.json",
                      Path("config.json")]:
        try:
            return json.load(open(candidate)).get("camera_model", "v2")
        except Exception:
            pass
    return "v2"


def probe():
    try:
        from picamera2 import Picamera2
        model   = _load_config_model()
        tuning_path = _CAM_TUNING_PROBE.get(model)
        if tuning_path:
            try:
                tuning = Picamera2.load_tuning_file(tuning_path)
                cam = Picamera2(tuning=tuning)
                print(f"[CameraProbe] Model={model}, tuning={tuning_path}")
            except Exception as e:
                print(f"[CameraProbe] Tuning load failed ({e}), using default")
                cam = Picamera2()
        else:
            cam = Picamera2()
            print(f"[CameraProbe] Model={model}, using default tuning")
        modes = []
        for i, m in enumerate(cam.sensor_modes):
            w, h  = m["size"]
            fps   = float(m.get("fps", m.get("frame_rate", 0)))
            bits  = m.get("bit_depth", 0)
            modes.append({
                "index":     i,
                "width":     w,
                "height":    h,
                "fps":       fps,
                "bit_depth": bits,
            })
        cam.close()
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(OUT_PATH, "w") as f:
            json.dump(modes, f, indent=2)
        print(f"[CameraProbe] Saved {len(modes)} sensor mode(s) to {OUT_PATH}")
        for m in modes:
            print(f"  [{m['index']}] {m['width']}×{m['height']} "
                  f"@ {m['fps']:.0f}fps  {m['bit_depth']}bit")
    except Exception as e:
        print(f"[CameraProbe] Failed: {e}")
        # Write empty file so GUI knows probe ran but failed
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(OUT_PATH, "w") as f:
            json.dump([], f)

if __name__ == "__main__":
    probe()
