"""HANS_AVATAR_ANIMATE_V1 — regeneruj LivePortrait klipy pro novou avatar verzi.

Volá se **za** `avatar_render.render_descriptor` po úspěšném `_mark_rendered`:
`paint_self` vyrobil `data/avatar/vN/idle.png` (nová tvář) → tento modul
vezme `idle.png`, uploaduje na PC ComfyUI, spustí `ComfyUI-LivePortraitKJ`
workflow 4× (d9/d13/d3/d6 driving templates → blink/talk/idle/talk klipy)
a stáhne výsledky do `data/avatar/clips/`. Tím dva source pravdy (statická
tvář + animace) drží krok.

**Deferral-safe:** herní mód / ComfyUI down / VRAM žádný → return False,
retry příště. Nikdy neshodí volajícího.

**Konvence** (viz [[avatar-animation-plan]]): source VŽDY `vN/idle.png`
(nejnovější), NIKDY greeting/talking/mood_*/act_*.

Konfigurace (`config.hans_avatar.animate`):
    enabled            — default False (opatrně — VRAM heavy, ~3-5 min)
    comfy_url          — default http://192.168.1.100:8188
    ollama_url         — pro unload, default http://192.168.1.100:11434
    driving            — dict output_prefix → (driving_video, frame_load_cap)
                         default: hlp_d9/d13 pro krátké, hans_idleloop/talkloop pro dlouhé
    per_clip_timeout_s — max per klip; default 900 (d20 talkloop reálně 9 min)
    trim_talkloop      — ořez talkloop na segment (start_s, dur_s); default (1.0, 3.5)
                         (bez oříznutí je 40s dramatický klip, koulí očima — parkano)
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

import requests

_log = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parent.parent
_CLIPS_DIR = _ROOT / "data" / "avatar" / "clips"

# Default driving templates — pár output_prefix → (driving_video, frame_load_cap).
# d9 = blink (40 frames ≈ 1.6s), d13 = talk (60 frames ≈ 2.4s),
# d3 = klidný idle (celých ~11.8s), d20 = klidný talk (celých 7s, přirozený).
# Historie: d6 (33s dramatický) byl původní talkloop → potřeboval ořez na 3.5s;
# d20 vygeneruje krátký přirozený talk bez potřeby ořezu.
_DEFAULT_DRIVING = {
    "hlp_d9":        ("d9.mp4",  40),
    "hlp_d13":       ("d13.mp4", 60),
    "hans_idleloop": ("d3.mp4",  0),   # 0 = celá délka driving
    "hans_talkloop": ("d20.mp4", 0),   # d20 = klidný krátký talk, 7s
}

# Ořez volitelný (dnes NEni potřeba, d20 je už krátký). Ponecháno jako opt-in
# pro případ, že by budoucí driving byl dlouhý/dramatický (jako d6 minule).
_DEFAULT_TRIM_TALKLOOP = None  # (start_s, dur_s) nebo None = bez ořezu


def _cfg(config: dict) -> dict:
    av = config.get("hans_avatar", {}) or {}
    return av.get("animate", {}) or {}


def enabled(config: dict) -> bool:
    return bool(_cfg(config).get("enabled", False))


def _comfy_url(config: dict) -> str:
    return _cfg(config).get("comfy_url", "http://192.168.1.100:8188").rstrip("/")


def _ollama_url(config: dict) -> str:
    # Ollama na PC (ne Pi localhost!) — LivePortrait konkuruje o VRAM s Hansovým chat modelem.
    return _cfg(config).get("ollama_url", "http://192.168.1.100:11434").rstrip("/")


def _latest_version_idle() -> Optional[Path]:
    """Najdi nejnovější `data/avatar/vN/idle.png`. None když žádná."""
    root = _ROOT / "data" / "avatar"
    vers = []
    for d in root.glob("v*"):
        try:
            vers.append((int(d.name[1:]), d))
        except ValueError:
            pass
    if not vers:
        return None
    idle = max(vers)[1] / "idle.png"
    return idle if idle.exists() else None


def _vram_cleanup(config: dict) -> None:
    """Uvolni VRAM na PC — Ollama unload_all + ComfyUI /free.
    LivePortrait potřebuje ~6 GB, karta má 16, chat model drží 8 → bez unloadu OOM."""
    ourl = _ollama_url(config)
    curl = _comfy_url(config)
    try:
        r = requests.get(f"{ourl}/api/ps", timeout=8)
        for m in (r.json() or {}).get("models", []):
            name = m.get("name") or m.get("model")
            if not name:
                continue
            try:
                requests.post(f"{ourl}/api/generate",
                              json={"model": name, "prompt": "", "keep_alive": 0},
                              timeout=15)
                _log.debug("avatar_animate: Ollama unload %s", name)
            except Exception as _ue:
                _log.debug("avatar_animate: Ollama unload %s selhalo: %s", name, _ue)
    except Exception as _pe:
        _log.debug("avatar_animate: Ollama /api/ps selhalo: %s", _pe)
    try:
        requests.post(f"{curl}/free",
                      json={"unload_models": True, "free_memory": True},
                      timeout=8)
    except Exception as _fe:
        _log.debug("avatar_animate: ComfyUI /free selhalo: %s", _fe)
    time.sleep(2)  # dát AMD driveru chvíli


def _upload_source(config: dict, idle_path: Path) -> bool:
    """POST /upload/image → ComfyUI/input/hans_tmpl.png."""
    curl = _comfy_url(config)
    try:
        with open(idle_path, "rb") as f:
            files = {"image": ("hans_tmpl.png", f, "image/png")}
            data = {"overwrite": "true"}
            r = requests.post(f"{curl}/upload/image", files=files, data=data,
                              timeout=15)
        return r.status_code == 200
    except Exception as _ue:
        _log.warning("avatar_animate: upload selhalo: %s", _ue)
        return False


def _workflow(driving_video: str, frame_load_cap: int, output_prefix: str) -> dict:
    """API workflow pro LivePortrait (extrahováno z metadat červnového mp4).
    src (LoadImage: hans_tmpl.png) + drv (VHS_LoadVideo) → cropper → process →
    composite → VHS_VideoCombine."""
    return {
        "mdl": {"class_type": "DownloadAndLoadLivePortraitModels",
                "inputs": {"precision": "fp16", "mode": "human"}},
        "cl":  {"class_type": "LivePortraitLoadCropper",
                "inputs": {"onnx_device": "CPU", "keep_model_loaded": True,
                           "detection_threshold": 0.5}},
        "src": {"class_type": "LoadImage",
                "inputs": {"image": "hans_tmpl.png"}},
        "drv": {"class_type": "VHS_LoadVideo",
                "inputs": {"video": driving_video, "force_rate": 0.0,
                           "custom_width": 0, "custom_height": 0,
                           "frame_load_cap": int(frame_load_cap),
                           "skip_first_frames": 0, "select_every_nth": 1,
                           "format": "None"}},
        "cr":  {"class_type": "LivePortraitCropper",
                "inputs": {"pipeline": ["mdl", 0], "cropper": ["cl", 0],
                           "source_image": ["src", 0],
                           "dsize": 512, "scale": 2.3, "vx_ratio": 0.0,
                           "vy_ratio": -0.125, "face_index": 0,
                           "face_index_order": "large-small", "rotate": True}},
        "pr":  {"class_type": "LivePortraitProcess",
                "inputs": {"pipeline": ["mdl", 0], "crop_info": ["cr", 1],
                           "source_image": ["src", 0],
                           "driving_images": ["drv", 0],
                           "lip_zero": False, "lip_zero_threshold": 0.03,
                           "stitching": True, "delta_multiplier": 1.0,
                           "mismatch_method": "constant",
                           "relative_motion_mode": "relative",
                           "driving_smooth_observation_variance": 3e-06}},
        "co":  {"class_type": "LivePortraitComposite",
                "inputs": {"source_image": ["src", 0],
                           "cropped_image": ["pr", 0],
                           "liveportrait_out": ["pr", 1]}},
        "sv":  {"class_type": "VHS_VideoCombine",
                "inputs": {"images": ["co", 0], "frame_rate": 25.0,
                           "loop_count": 0, "filename_prefix": output_prefix,
                           "format": "video/h264-mp4", "pingpong": False,
                           "save_output": True}},
    }


def _submit_and_wait(config: dict, workflow: dict,
                     timeout_s: int = 600) -> Optional[str]:
    """POST /prompt, pak polling /history/${PID} do success/error/timeout.
    Vrací filename úspěšného mp4 nebo None."""
    curl = _comfy_url(config)
    try:
        r = requests.post(f"{curl}/prompt", json={"prompt": workflow}, timeout=15)
        pid = (r.json() or {}).get("prompt_id")
    except Exception as _se:
        _log.warning("avatar_animate: submit selhalo: %s", _se)
        return None
    if not pid:
        return None
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        time.sleep(5)
        try:
            r = requests.get(f"{curl}/history/{pid}", timeout=8)
            hist = r.json() or {}
        except Exception:
            continue
        if not hist:
            continue
        entry = list(hist.values())[0]
        st = (entry.get("status") or {}).get("status_str", "running")
        if st == "success":
            # najdi mp4 ve výstupech
            for node_out in (entry.get("outputs") or {}).values():
                for gif in node_out.get("gifs") or []:
                    fn = gif.get("filename")
                    if fn and fn.endswith(".mp4"):
                        return fn
            return None
        if st == "error":
            for msg in (entry.get("status") or {}).get("messages", []):
                if msg[0] == "execution_error":
                    e = msg[1]
                    _log.warning("avatar_animate: %s selhal v node %s: %s",
                                 pid[:8], e.get("node_id"),
                                 str(e.get("exception_message", ""))[:200])
                    break
            return None
    _log.warning("avatar_animate: %s timeout po %ds", pid[:8], timeout_s)
    return None


def _fetch_output(config: dict, filename: str, target: Path) -> bool:
    """GET /view?filename=X&type=output → binary mp4 → target path."""
    curl = _comfy_url(config)
    try:
        r = requests.get(f"{curl}/view",
                         params={"filename": filename, "type": "output"},
                         timeout=30, stream=True)
        r.raise_for_status()
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "wb") as f:
            for chunk in r.iter_content(chunk_size=65536):
                f.write(chunk)
        return target.exists() and target.stat().st_size > 0
    except Exception as _fe:
        _log.warning("avatar_animate: fetch %s selhalo: %s", filename, _fe)
        return False


def _trim_ffmpeg(src: Path, start_s: float, dur_s: float) -> bool:
    """Ořez mp4 přes ffmpeg (re-encode, ne -c copy → přesný start bez keyframe rounding).
    Nahradí src in-place; při chybě src zůstane netknutý."""
    if not src.exists():
        return False
    tmp = src.with_suffix(".trim.mp4")
    try:
        subprocess.run([
            "ffmpeg", "-y", "-ss", str(start_s), "-t", str(dur_s),
            "-i", str(src), "-c:v", "libx264", "-preset", "veryfast",
            "-crf", "20", "-an", str(tmp)
        ], check=True, capture_output=True, timeout=60)
        if tmp.exists() and tmp.stat().st_size > 0:
            tmp.replace(src)
            return True
    except Exception as _te:
        _log.debug("avatar_animate: trim %s selhal: %s", src.name, _te)
        if tmp.exists():
            tmp.unlink()
    return False


def regenerate_clips(version: int, config: dict) -> bool:
    """Hlavní funkce: pro nejnovější vN/idle.png regeneruj 4 klipy.
    Vrátí True když aspoň 1 klip úspěšný, False jinak. Deferral-safe."""
    if not enabled(config):
        _log.debug("avatar_animate: vypnuto (hans_avatar.animate.enabled)")
        return False
    # herní mód — nesahej na VRAM
    try:
        from scripts.ollama_client import game_mode_on
        if game_mode_on():
            _log.info("avatar_animate: herní mód → skip regenerace")
            return False
    except Exception:
        pass
    idle = _latest_version_idle()
    if not idle:
        _log.warning("avatar_animate: žádný vN/idle.png — skip")
        return False
    _log.info("avatar_animate: regenerace klipů pro v%d (source=%s)",
              version, idle.relative_to(_ROOT))

    # Ověř ComfyUI je nahoře
    try:
        r = requests.get(f"{_comfy_url(config)}/system_stats", timeout=5)
        if r.status_code != 200:
            _log.warning("avatar_animate: ComfyUI down (%d) — skip", r.status_code)
            return False
    except Exception as _ce:
        _log.warning("avatar_animate: ComfyUI nedostupné (%s) — skip", _ce)
        return False

    # Upload source (jednou)
    _vram_cleanup(config)
    if not _upload_source(config, idle):
        _log.warning("avatar_animate: upload source selhal — skip")
        return False

    driving = _cfg(config).get("driving") or _DEFAULT_DRIVING
    per_clip_timeout = int(_cfg(config).get("per_clip_timeout_s", 900))
    _trim_cfg = _cfg(config).get("trim_talkloop", _DEFAULT_TRIM_TALKLOOP)
    trim_talkloop = tuple(_trim_cfg) if _trim_cfg else None
    n_ok = 0
    for prefix, spec in driving.items():
        drv_video, frame_cap = spec[0], spec[1] if len(spec) > 1 else 0
        _log.info("avatar_animate: %s (driving=%s, frame_cap=%d)",
                  prefix, drv_video, frame_cap)
        # VRAM cleanup PŘED KAŽDÝM klipem — LivePortrait drží model mezi joby → OOM.
        _vram_cleanup(config)
        wf = _workflow(drv_video, frame_cap, prefix)
        fn = _submit_and_wait(config, wf, timeout_s=per_clip_timeout)
        if not fn:
            _log.warning("avatar_animate: %s nedokončeno", prefix)
            continue
        target = _CLIPS_DIR / f"{prefix}_00001.mp4"
        if not _fetch_output(config, fn, target):
            continue
        # ořez talkloop (dramatický → uprostřed segment)
        if prefix == "hans_talkloop" and trim_talkloop:
            if _trim_ffmpeg(target, trim_talkloop[0], trim_talkloop[1]):
                _log.info("avatar_animate: %s ořezán na %.1fs od %.1fs",
                          prefix, trim_talkloop[1], trim_talkloop[0])
        _log.info("avatar_animate: ✓ %s (%d B)", target.name, target.stat().st_size)
        n_ok += 1

    # Warm Ollama chat model zpět (jako `_ollama_warm` v avatar_render)
    try:
        model = (config.get("models", {}) or {}).get("dialog", "hans-czech:latest")
        requests.post(f"{_ollama_url(config)}/api/generate",
                      json={"model": model, "prompt": "", "keep_alive": -1},
                      timeout=120)
        _log.debug("avatar_animate: %s warmed", model)
    except Exception:
        pass

    _log.info("avatar_animate: hotovo — %d/%d klipů pro v%d",
              n_ok, len(driving), version)
    return n_ok > 0


def regenerate_clips_async(version: int, config: dict) -> None:
    """Fire-and-forget varianta pro hook — spustí regeneraci v pozadí,
    aby neblokovala noční tick / paint_self runner."""
    def _worker():
        try:
            regenerate_clips(version, config)
        except Exception as _e:
            _log.warning("avatar_animate: async worker failed: %s", _e)
    threading.Thread(target=_worker, daemon=True,
                     name="HansAvatarAnimate").start()


if __name__ == "__main__":
    # Manuální test: python3 -m scripts.hans_avatar_animate [version]
    import sys
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    cfg = json.load(open("config.json"))
    # sila: zapnout na tento run
    cfg.setdefault("hans_avatar", {}).setdefault("animate", {})["enabled"] = True
    ver = int(sys.argv[1]) if len(sys.argv) > 1 else 2
    ok = regenerate_clips(ver, cfg)
    print("OK" if ok else "FAILED")
