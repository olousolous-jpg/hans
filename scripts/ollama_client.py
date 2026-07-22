#!/usr/bin/env python3
"""Centrální Ollama klient — jednotný timeout, keep_alive, retry.

Použití:
    from scripts.ollama_client import ollama_chat, ollama_generate, ollama_warmup
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Optional

import requests

from scripts._log_circuit import for_url as _breaker_for, is_conn_error

_log = logging.getLogger("ollama_client")

# ── OLLAMA_GAME_MODE_V1 — herní mód ────────────────────────
# Flag soubor = sdílený signál napříč procesy (Hans, web_admin, subprocess skripty).
# Když existuje, Hans NEvolá Ollamu → VRAM zůstane volná pro hru na PC.
_PAUSE_FLAG = Path(__file__).resolve().parent.parent / "data" / ".ollama_paused"


def game_mode_on() -> bool:
    """True = herní mód aktivní → veškerá volání Ollamy se přeskočí (return None)."""
    try:
        return _PAUSE_FLAG.exists()
    except Exception:
        return False


def ollama_unload_all(ollama_url: str | None = None,
                      config: dict | None = None) -> int:
    """Uvolni VŠECHNY právě nahrané modely z VRAM (keep_alive=0). Vrátí počet."""
    url = _resolve_url(ollama_url, config)
    models = []
    try:
        r = requests.get(f"{url}/api/ps", timeout=10)
        r.raise_for_status()
        models = [m.get("model") or m.get("name")
                  for m in (r.json() or {}).get("models", [])]
    except Exception as exc:
        _log.warning("unload_all: /api/ps selhal: %s", exc)
    n = 0
    for m in models:
        if not m:
            continue
        try:
            requests.post(f"{url}/api/generate",
                          json={"model": m, "prompt": "", "keep_alive": 0},
                          timeout=30)
            _log.info("Ollama unload: %s", m)
            n += 1
        except Exception as exc:
            _log.warning("unload %s selhal: %s", m, exc)
    return n


def loaded_vram(ollama_url: str | None = None,
                config: dict | None = None) -> list:
    """Co právě drží VRAM: [{'name':..., 'gb':...}] pro modely se size_vram>0.
    Chyba → []. Slouží k ověření, že herní mód reálně uvolnil grafiku."""
    url = _resolve_url(ollama_url, config)
    try:
        r = requests.get(f"{url}/api/ps", timeout=10)
        r.raise_for_status()
        out = []
        for m in (r.json() or {}).get("models", []):
            vram = int(m.get("size_vram", 0) or 0)
            if vram > 0:
                out.append({"name": m.get("model") or m.get("name") or "?",
                            "gb": round(vram / 1e9, 1)})
        return out
    except Exception as exc:
        _log.warning("loaded_vram: /api/ps selhal: %s", exc)
        return []


def set_game_mode(on: bool, ollama_url: str | None = None,
                  config: dict | None = None) -> dict:
    """Zapni/vypni herní mód. on=True: vytvoř flag (Hans přestane volat Ollamu) +
    uvolni VRAM. on=False: smaž flag (mozek zase k dispozici)."""
    try:
        if on:
            _PAUSE_FLAG.parent.mkdir(parents=True, exist_ok=True)
            _PAUSE_FLAG.write_text(str(time.time()))   # flag PRVNÍ → nové volání se gate
            time.sleep(0.4)
            freed = ollama_unload_all(ollama_url, config)
            _log.info("HERNÍ MÓD ZAP — uvolněno %d modelů, Ollama se nepoužívá", freed)
            _log_game_mode_diary(config, True)
            return {"game_mode": True, "unloaded": freed}
        try:
            _PAUSE_FLAG.unlink()
        except FileNotFoundError:
            pass
        _log.info("HERNÍ MÓD VYP — Ollama opět k dispozici")
        _log_game_mode_diary(config, False)
        return {"game_mode": False}
    except Exception as exc:
        _log.error("set_game_mode(%s) selhal: %s", on, exc)
        return {"error": str(exc)}


def _log_game_mode_diary(config: dict | None, on: bool) -> None:
    """HANS_GAME_MODE_DIARY_V1 — zaznamenej přepnutí herního módu do deníku.
    NEUTRÁLNĚ (jen fakt přepnutí, ŽÁDNÉ pre-vysvětlení následku) — aby případné
    budoucí odvození souvislosti (herní mód ↔ výpadek mozku) bylo GENUINNÍ, ne
    parafráze zadaného faktu. Best-effort, čistý SQL (funguje i bez mozku)."""
    try:
        cfg = config or {}
        db = (cfg.get("diary_db")
              or (cfg.get("hans_idle", {}) or {}).get("diary_db")
              or "data/hans_diary.db")
        note = "Zapnul jsem herní mód." if on else "Vypnul jsem herní mód."
        import sqlite3
        conn = sqlite3.connect(db, timeout=5.0)
        conn.execute(
            "INSERT INTO diary (ts, event_type, title, note, data) "
            "VALUES (?,?,?,?,?)",
            (time.time(), "game_mode", "herní mód", note, "on" if on else "off"))
        conn.commit()
        conn.close()
    except Exception as exc:
        _log.debug("_log_game_mode_diary: %s", exc)

# ── Defaults ───────────────────────────────────────────────
DEFAULT_URL     = "http://127.0.0.1:11434"
DEFAULT_TIMEOUT = 120          # sekundy
DEFAULT_KEEP_ALIVE = -1         # drž model v VRAM napořád  # KEEPALIVE_FIX_V2
MAX_RETRIES     = 1            # 1 retry při timeout (celkem 2 pokusy)

# HANS_WARMUP_PAUSE_V1 — VRAM handoff: uspat keepalive warmup, dokud noční
# base-model analytika drží VRAM. Bez toho 4min pin hans-czech (8GB) evictuje
# base OpenEuroLLM (8GB) uprostřed generování (8+8 > 16GB VRAM) → 300s timeouty.
_warmup_pause_until = 0.0

def pause_warmup(seconds: float) -> None:
    """Uspi keepalive warmup na `seconds` (auto-expiry = cap, kdyby dávka
    spadla bez resume). Idempotentní: okno jen prodlouží, nezkrátí."""
    global _warmup_pause_until
    _warmup_pause_until = max(_warmup_pause_until, time.time() + float(seconds))

def resume_warmup() -> None:
    """Zruš pauzu warmupu (konec analytické dávky)."""
    global _warmup_pause_until
    _warmup_pause_until = 0.0

def warmup_paused() -> bool:
    return time.time() < _warmup_pause_until

# OLLAMA_CLIENT_MARKER (idempotence)


def _resolve_url(ollama_url: str | None, config: dict | None) -> str:
    """Zjisti Ollama URL — explicitní arg > config > default."""
    if ollama_url:
        return ollama_url.rstrip("/")
    if config:
        return config.get("openwebui_chat", {}).get(
            "base_url", DEFAULT_URL).rstrip("/")
    return DEFAULT_URL


def ollama_chat(
    model: str,
    messages: list[dict],
    *,
    ollama_url: str | None = None,
    config: dict | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    keep_alive: int = DEFAULT_KEEP_ALIVE,
    stream: bool = False,
    options: dict | None = None,
) -> Optional[str]:
    """Pošle /api/chat request. Vrátí text odpovědi nebo None při chybě."""
    if game_mode_on():   # OLLAMA_GAME_MODE_V1 — herní mód: nech VRAM volnou
        return None
    url = _resolve_url(ollama_url, config)
    payload: dict = {
        "model": model,
        "messages": messages,
        "stream": stream,
        "keep_alive": keep_alive,
    }
    if options:
        payload["options"] = options

    return _post_with_retry(f"{url}/api/chat", payload, timeout,
                            _extract_chat)


def ollama_generate(
    model: str,
    prompt: str,
    *,
    system: str | None = None,
    images: list[str] | None = None,
    ollama_url: str | None = None,
    config: dict | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    keep_alive: int = DEFAULT_KEEP_ALIVE,
    stream: bool = False,
    options: dict | None = None,
) -> Optional[str]:
    """Pošle /api/generate request. Vrátí text odpovědi nebo None."""
    if game_mode_on():   # OLLAMA_GAME_MODE_V1
        return None
    url = _resolve_url(ollama_url, config)
    payload: dict = {
        "model": model,
        "prompt": prompt,
        "stream": stream,
        "keep_alive": keep_alive,
    }
    if system:
        payload["system"] = system
    if images:
        payload["images"] = images
    if options:
        payload["options"] = options

    return _post_with_retry(f"{url}/api/generate", payload, timeout,
                            _extract_generate)


def ollama_warmup(
    model: str,
    *,
    ollama_url: str | None = None,
    config: dict | None = None,
    keep_alive: int = DEFAULT_KEEP_ALIVE,
) -> bool:
    """Pošle prázdný request aby se model nahrál do VRAM. Vrátí True při úspěchu."""
    if game_mode_on():   # OLLAMA_GAME_MODE_V1 — nepřihřívej, ať VRAM zůstane volná
        return False
    if warmup_paused():  # HANS_WARMUP_PAUSE_V1 — base analytika drží VRAM
        _log.debug("Warmup: přeskočeno (%s) — noční analytika drží VRAM", model)
        return False
    url = _resolve_url(ollama_url, config)
    br = _breaker_for(url)  # LOG_CIRCUIT_V1
    try:
        # když už víme, že endpoint je dole, ani INFO nespamuj
        if br.snapshot().get("down"):
            _log.debug("Warmup: loading %s ... (endpoint stále down)", model)
        else:
            _log.info("Warmup: loading %s ...", model)
        t0 = time.time()
        r = requests.post(
            f"{url}/api/generate",
            json={"model": model, "prompt": "", "keep_alive": keep_alive},
            timeout=300,
        )
        r.raise_for_status()
        _log.info("Warmup: %s ready (%.1fs)", model, time.time() - t0)
        br.note_success(_log)
        return True
    except Exception as exc:
        if is_conn_error(exc):
            if br.should_log(exc):
                _log.error("Warmup failed for %s: %s", model, exc)
        else:
            _log.error("Warmup failed for %s: %s", model, exc)
        return False


# ── Internals ──────────────────────────────────────────────

def _post_with_retry(url: str, payload: dict, timeout: int,
                     extractor) -> Optional[str]:
    """POST s retry při timeout. LOG_CIRCUIT_V1: potlač spam z mrtvého endpointu."""
    br = _breaker_for(url)
    last_exc = None
    for attempt in range(1, MAX_RETRIES + 2):
        try:
            r = requests.post(url, json=payload, timeout=timeout)
            r.raise_for_status()
            out = extractor(r.json())
            br.note_success(_log)
            return out
        except requests.exceptions.Timeout as exc:
            last_exc = exc
            if attempt <= MAX_RETRIES:
                _log.warning("Ollama timeout (%ds), retry %d/%d: %s",
                             timeout, attempt, MAX_RETRIES, url)
            else:
                _log.error("Ollama timeout (%ds) after %d attempts: %s",
                           timeout, attempt, url)
        except requests.exceptions.ConnectionError as exc:
            if br.should_log(exc):
                _log.error("Ollama connection error: %s — %s", url, exc)
            return None
        except Exception as exc:
            if is_conn_error(exc):
                if br.should_log(exc):
                    _log.error("Ollama connection error: %s — %s", url, exc)
            else:
                _log.error("Ollama request error: %s — %s", url, exc)
            return None
    return None


def _extract_chat(data: dict) -> Optional[str]:
    try:
        return data["message"]["content"].strip()
    except (KeyError, AttributeError):
        _log.error("Unexpected chat response: %s", data)
        return None


def _extract_generate(data: dict) -> Optional[str]:
    try:
        return data["response"].strip()
    except (KeyError, AttributeError):
        _log.error("Unexpected generate response: %s", data)
        return None
