"""HANS_HEALTH_V1 — živý watchdog závislostí + sebe-uzdravení.

Hans je bohatý na featury, ale KŘEHKÝ na infrastruktuře: Ollama se zasekne
(chat 120s timeout), ComfyUI spadne (malování mlčky nefunguje), qwen-VL/llava
hodí EOF (zrak tiše rozbitý), PC usne. Dosud to uživatel odhaloval RUČNĚ.

Tento modul dává Hansovi VĚDOMÍ o zdraví vlastních nástrojů:
  - `probe_all(config)` — reálně proboduje závislosti (deferral-safe, nikdy nehází)
  - klíč = ROZLIŠENÍ Ollama stavů: ok / paused(herní mód) / wedged / down.
    „wedged" = server žije (/api/tags odpoví), ale INFERENCE visí → `/api/tags`
    to NEodhalí, proto zkoušíme malou generaci. To je jediný self-heal kandidát.
  - `heal_ollama(config)` — restart zaseklé Ollamy na PC přes SSH (scoped sudo).
  - `run_health_check(config, ...)` — probe → volitelně heal → shrnutí + stav JSON.

BEZ LLM (kromě 1 triviální ping-generace k detekci wedge). Vzor pc_night_shutdown.
"""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path
from typing import Optional

from scripts.logger import get_logger

_log = get_logger("hans_health")

_ROOT = Path(__file__).resolve().parent.parent
_STATE_PATH = _ROOT / "data" / "health_state.json"

# stavy služby
OK = "ok"
PAUSED = "paused"      # záměrně vypnuto (herní mód)
WEDGED = "wedged"      # server žije, ale visí → self-heal kandidát
DOWN = "down"          # nedostupné (PC spí / služba neběží)
UNKNOWN = "unknown"


def _cfg(config: dict) -> dict:
    return (config or {}).get("health", {}) or {}


def enabled(config: dict) -> bool:
    return bool(_cfg(config).get("enabled", True))


# ── Ollama: ok / paused / wedged / down ──────────────────────────────────────
def probe_ollama(config: dict) -> dict:
    """Reálná probe: nejdřív zkus TRIVIÁLNÍ inference (odhalí wedge). Když selže,
    rozliš herní mód (paused) vs. server žije-ale-visí (wedged) vs. mrtvý (down)."""
    t0 = time.time()
    try:
        from scripts.ollama_client import game_mode_on
        if game_mode_on():
            return {"status": PAUSED, "detail": "herní mód", "latency_s": 0.0}
    except Exception:
        pass
    # 1) malá generace = reálný test, že engine odpovídá (ne jen HTTP server)
    raw = None
    try:
        from scripts.ollama_client import ollama_generate
        model = ((config.get("dialog", {}) or {}).get("model")
                 or "hans-czech:latest")
        to = int(_cfg(config).get("ollama_probe_timeout", 25))
        raw = ollama_generate(model, "ok", config=config, timeout=to,
                              keep_alive=-1, options={"num_predict": 1,
                                                      "temperature": 0})
    except Exception as e:
        _log.debug("probe_ollama generate: %s", e)
    lat = round(time.time() - t0, 1)
    if raw is not None and str(raw).strip() != "":
        return {"status": OK, "detail": "inference ok", "latency_s": lat}
    # 2) inference selhala — žije aspoň HTTP server? (rozliš wedged vs down)
    try:
        import requests
        from scripts.ollama_client import _resolve_url
        url = _resolve_url(None, config)
        r = requests.get(f"{url}/api/tags", timeout=6)
        if r.ok:
            return {"status": WEDGED, "detail": "server žije, inference visí",
                    "latency_s": lat}
    except Exception as e:
        _log.debug("probe_ollama tags: %s", e)
    return {"status": DOWN, "detail": "Ollama nedostupná", "latency_s": lat}


# ── ComfyUI (malování/avatar) ────────────────────────────────────────────────
def probe_comfyui(config: dict) -> dict:
    try:
        import requests
        from scripts.avatar_render import _comfy_url
        url = _comfy_url(config)
        r = requests.get(f"{url}/system_stats", timeout=6)
        if r.ok:
            return {"status": OK, "detail": "system_stats ok"}
        return {"status": DOWN, "detail": "HTTP %s" % r.status_code}
    except Exception as e:
        return {"status": DOWN, "detail": str(e)[:80]}


# ── Kodi (media) ─────────────────────────────────────────────────────────────
def probe_kodi(config: dict) -> dict:
    try:
        from scripts.kodi_client import KodiClient
        kc = KodiClient(config)
        res = kc._call("JSONRPC.Ping")
        if res is not None:
            return {"status": OK, "detail": "ping ok"}
        return {"status": DOWN, "detail": "bez odpovědi"}
    except Exception as e:
        return {"status": DOWN, "detail": str(e)[:80]}


# ── STT / Whisper (hlas) ─────────────────────────────────────────────────────
def probe_stt(config: dict) -> dict:
    url = (config.get("voice", {}) or {}).get("stt_url")
    if not url:
        return {"status": UNKNOWN, "detail": "stt_url nenastaveno"}
    try:
        import requests
        from urllib.parse import urlparse
        p = urlparse(url)
        base = "%s://%s" % (p.scheme, p.netloc)
        # /health je lehký; endpoint samotný vyžaduje audio → jen dostupnost hostu
        r = requests.get(f"{base}/health", timeout=6)
        if r.ok:
            return {"status": OK, "detail": "host ok"}
        return {"status": DOWN, "detail": "HTTP %s" % r.status_code}
    except Exception as e:
        return {"status": DOWN, "detail": str(e)[:80]}


# ── PC (SSH) ─────────────────────────────────────────────────────────────────
def probe_pc(config: dict) -> dict:
    try:
        from scripts import pc_remote
        if not pc_remote.enabled(config):
            return {"status": UNKNOWN, "detail": "pc_remote vypnut"}
        out = pc_remote.run(config, "echo ok", timeout=8)
        if out is not None and "ok" in str(out):
            return {"status": OK, "detail": "ssh ok"}
        return {"status": DOWN, "detail": "PC spí / SSH neodpovídá"}
    except Exception as e:
        return {"status": DOWN, "detail": str(e)[:80]}


# ── Disk (Pi) ────────────────────────────────────────────────────────────────
def probe_disk(config: dict) -> dict:
    try:
        total, used, free = shutil.disk_usage(str(_ROOT))
        free_gb = round(free / 1e9, 1)
        min_gb = float(_cfg(config).get("min_disk_gb", 2.0))
        st = OK if free_gb >= min_gb else DOWN
        return {"status": st, "detail": "%.1f GB volných" % free_gb,
                "free_gb": free_gb}
    except Exception as e:
        return {"status": UNKNOWN, "detail": str(e)[:80]}


# ── agregace ─────────────────────────────────────────────────────────────────
def probe_all(config: dict) -> dict:
    """Proboduje všechny závislosti. Vrací {service: {status, detail, ...}}.
    Deferral-safe — každá probe je try/except, žádná neshodí celek."""
    checks = {
        "ollama": probe_ollama,
        "comfyui": probe_comfyui,
        "kodi": probe_kodi,
        "stt": probe_stt,
        "pc": probe_pc,
        "disk": probe_disk,
    }
    only = _cfg(config).get("probes")  # volitelně podmnožina
    out = {}
    for name, fn in checks.items():
        if only and name not in only:
            continue
        try:
            out[name] = fn(config)
        except Exception as e:
            out[name] = {"status": UNKNOWN, "detail": str(e)[:80]}
    return out


def degraded_services(health: dict) -> list:
    """Služby, které jsou reálně rozbité (wedged/down) — ne paused/unknown.
    paused = záměr (herní mód), unknown = nemáme jak změřit → nehlásíme jako vadu."""
    return [n for n, s in (health or {}).items()
            if s.get("status") in (WEDGED, DOWN)]


# ── self-heal ────────────────────────────────────────────────────────────────
def heal_ollama(config: dict) -> bool:
    """Restart zaseklé Ollamy na PC přes SSH. Vyžaduje na PC scoped sudoers:
      <user> ALL=(ALL) NOPASSWD: /usr/bin/systemctl restart ollama
    Vrací True když příkaz prošel. Konzervativní — volá se JEN na status WEDGED."""
    if not _cfg(config).get("self_heal_ollama", True):
        _log.info("health: self-heal Ollamy vypnut configem")
        return False
    try:
        from scripts import pc_remote
        if not pc_remote.enabled(config):
            return False
        cmd = _cfg(config).get("ollama_restart_cmd",
                               "sudo -n systemctl restart ollama")
        _log.warning("health: Ollama WEDGED → restartuji na PC (%s)", cmd)
        out = pc_remote.run(config, cmd, timeout=30)
        _log.info("health: restart Ollamy odeslán (out=%r)", out)
        return out is not None
    except Exception as e:
        _log.warning("health: self-heal Ollamy selhal: %s", e)
        return False


# ── stav pro dashboard / surfacing ───────────────────────────────────────────
def _write_state(health: dict, healed: list) -> None:
    try:
        _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = {"ts": time.time(), "services": health,
                   "degraded": degraded_services(health), "healed": healed}
        tmp = _STATE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(_STATE_PATH)
    except Exception as e:
        _log.debug("health: zápis stavu selhal: %s", e)


def read_state() -> Optional[dict]:
    try:
        return json.loads(_STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None


def summary_sentence(health: dict, healed: list) -> str:
    """Krátká věta pro Hansovo surfacing (1. osoba, upřímně)."""
    bad = degraded_services(health)
    if not bad:
        return ""
    labels = {"ollama": "můj mozek (Ollama)", "comfyui": "malování (ComfyUI)",
              "kodi": "televize (Kodi)", "stt": "sluch (přepis řeči)",
              "pc": "počítač", "disk": "místo na disku"}
    parts = [labels.get(b, b) for b in bad]
    s = "Zaznamenal jsem potíž: " + ", ".join(parts) + "."
    if "ollama" in healed:
        s += " Zkusil jsem svůj mozek restartovat."
    return s


def run_health_check(config: dict, heal: bool = True) -> dict:
    """Hlavní vstup: probe → (volitelně) heal wedged Ollamu → zapiš stav.
    Vrací {health, healed, degraded}. Deferral-safe."""
    if not enabled(config):
        return {"health": {}, "healed": [], "degraded": []}
    health = probe_all(config)
    healed = []
    if heal and health.get("ollama", {}).get("status") == WEDGED:
        if heal_ollama(config):
            healed.append("ollama")
    _write_state(health, healed)
    bad = degraded_services(health)
    if bad:
        _log.warning("health: degradované služby: %s (healed=%s)", bad, healed)
    else:
        _log.info("health: vše ok")
    return {"health": health, "healed": healed, "degraded": bad}


if __name__ == "__main__":
    import sys
    cfg = json.loads((_ROOT / "config.json").read_text(encoding="utf-8"))
    do_heal = "--heal" in sys.argv
    res = run_health_check(cfg, heal=do_heal)
    for name, st in res["health"].items():
        print("%-9s %-8s %s" % (name, st.get("status"), st.get("detail", "")))
    if res["degraded"]:
        print("\ndegradováno:", res["degraded"], "| healed:", res["healed"])
        print("věta:", summary_sentence(res["health"], res["healed"]))
    else:
        print("\nvše ok")
