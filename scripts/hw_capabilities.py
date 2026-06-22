"""
HW_CAPABILITIES_V1 — detekce dostupného hardwaru při startu + graceful gating.

Princip:
  • MANDATORY lokální HW (kamera, Hailo) chybí → srozumitelná chyba (Hans nemá smysl).
  • OPTIONAL lokální HW (dual-eye displeje, servo, audio out, mikrofon) chybí →
    automaticky vypne odpovídající `config.features.*` flag (uživatel ho může mít
    vypnutý i ručně). Subsystémy pak flag respektují.
  • SÍŤOVÉ služby (LLM/Ollama, ComfyUI, Kodi) se NEgateují — přicházejí/odcházejí
    (PC v noci spí) → jen se reportují; degradace je dynamická jinde v kódu.

Probe je LEHKÝ (device files / import / krátký TCP) — NEOTVÍRÁ kameru ani Hailo,
aby nekolidoval s reálnou inicializací.
"""

import glob
import importlib.util
import logging
import os
import socket
from urllib.parse import urlparse

_log = logging.getLogger("hw_caps")


# ── nízkoúrovňové sondy ──────────────────────────────────────────────────────
def _has_module(mod: str) -> bool:
    try:
        return importlib.util.find_spec(mod) is not None
    except Exception:
        return False


def _tcp_ok(host: str, port: int, timeout: float = 2.0) -> bool:
    if not host or not port:
        return False
    try:
        with socket.create_connection((host, port), timeout):
            return True
    except Exception:
        return False


def _hostport(url: str, default_port: int = 80):
    try:
        u = urlparse(url if "://" in url else "http://" + url)
        return u.hostname, (u.port or default_port)
    except Exception:
        return None, None


# ── jednotlivé HW ────────────────────────────────────────────────────────────
def _camera() -> bool:
    if not _has_module("picamera2"):
        return False
    return os.path.exists("/dev/video0") or bool(glob.glob("/dev/media*"))


def _hailo() -> bool:
    return os.path.exists("/dev/hailo0")


def _spi_displays() -> bool:
    # dual-eye Waveshare jede po SPI0; spidev existuje = SPI povolené/zapojené
    return os.path.exists("/dev/spidev0.0") or bool(glob.glob("/dev/spidev*"))


def _i2c_servo() -> bool:
    # PCA9685 servo řadič je na I2C
    return bool(glob.glob("/dev/i2c-*"))


def _audio_out() -> bool:
    return bool(glob.glob("/dev/snd/pcmC*D*p"))  # playback PCM zařízení


def _mic() -> bool:
    return bool(glob.glob("/dev/snd/pcmC*D*c"))  # capture PCM zařízení


# ── hlavní detekce ───────────────────────────────────────────────────────────
def detect(config: dict) -> dict:
    """Vrátí dict capabilities (True/False). Síťové sondy mají krátký timeout."""
    ow = config.get("openwebui_chat", {}) or {}
    llm_host, llm_port = _hostport(ow.get("base_url", ""), 11434)
    comfy_host, comfy_port = _hostport(
        (config.get("hans_avatar", {}) or {}).get("comfyui_url", ""), 8188)
    kodi = config.get("kodi", {}) or {}
    kodi_host, kodi_port = kodi.get("host"), int(kodi.get("port", 8080) or 8080)

    return {
        # mandatory (lokální)
        "camera": _camera(),
        "hailo": _hailo(),
        # optional (lokální → gatuje se)
        "dual_eye_display": _spi_displays(),
        "servo": _i2c_servo(),
        "audio_out": _audio_out(),
        "mic": _mic(),
        # síťové služby (jen report, NEgatuje se)
        "llm": _tcp_ok(llm_host, llm_port),
        "comfyui": _tcp_ok(comfy_host, comfy_port),
        "kodi": _tcp_ok(kodi_host, kodi_port),
    }


# mapování: lokální optional HW → config.features flag
_OPTIONAL_FLAG = {
    "dual_eye_display": "dual_eye_display",
    "servo": "servo_tracking",
    "audio_out": "tts_audio",
    "mic": "voice_input",
}
_MANDATORY = ("camera", "hailo")
_NETWORK = ("llm", "comfyui", "kodi")


def report(caps: dict) -> str:
    def m(k):
        return "✓" if caps.get(k) else "✗"
    return (
        "mandatory[ kamera %s  hailo %s ]  "
        "optional[ displeje %s  servo %s  audio %s  mic %s ]  "
        "síť[ llm %s  comfyui %s  kodi %s ]" % (
            m("camera"), m("hailo"),
            m("dual_eye_display"), m("servo"), m("audio_out"), m("mic"),
            m("llm"), m("comfyui"), m("kodi")))


def apply(config: dict) -> dict:
    """Spustí detekci, zaloguje report, vynutí mandatory (chybí → hlasitá chyba),
    auto-vypne optional flagy pro chybějící lokální HW. Vrací caps dict.
    Nikdy nehází — selhání detekce nesmí shodit start."""
    try:
        caps = detect(config)
    except Exception as e:
        _log.warning("HW detekce selhala (pokračuji bez gatingu): %s", e)
        return {}

    _log.info("HW capabilities: %s", report(caps))

    missing = [m for m in _MANDATORY if not caps.get(m)]
    if missing:
        _log.error("=" * 60)
        _log.error("POVINNÝ HW CHYBÍ: %s", ", ".join(missing))
        _log.error("Hans je rozpoznávací robot — bez kamery a Hailo NPU nemůže "
                   "fungovat. Zkontroluj zapojení / ovladače.")
        _log.error("=" * 60)
    if not caps.get("llm"):
        _log.warning("LLM backend nedostupný (PC/Ollama) — Hans poběží degradovaně "
                     "(bez 'mozku'), dokud se neobjeví. Dynamická degradace běží.")

    feats = config.setdefault("features", {})
    for cap, flag in _OPTIONAL_FLAG.items():
        if not caps.get(cap) and feats.get(flag, True):
            _log.info("Auto-disable feature '%s' — HW '%s' nedostupný.", flag, cap)
            feats[flag] = False
    return caps


# ── ruční test: python3 -m scripts.hw_capabilities ──────────────────────────
if __name__ == "__main__":
    import json
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    cfg = json.load(open("config.json"))
    caps = detect(cfg)
    print("Detekce HW:\n  " + report(caps))
    print("\nMandatory chybí:", [m for m in _MANDATORY if not caps.get(m)] or "nic")
    print("Optional → auto-disable:",
          [_OPTIONAL_FLAG[c] for c in _OPTIONAL_FLAG if not caps.get(c)] or "nic")
