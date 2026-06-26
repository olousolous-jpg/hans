"""
Hans Body Monitor
Sleduje hardware Raspi (tělo) a dostupnost Ollama (mozek).

Hans ví:
  - Teplota CPU → "cítím teplo"
  - RAM využití → "paměť je plná"
  - CPU load    → "pracuji usilovně"
  - Ollama down → nálada klesá, Hans komentuje absenci myšlenek
  - Ollama up   → úleva, Hans může zase přemýšlet

Výstup:
  - get_body_context()  → string pro system prompt
  - get_brain_context() → string pro system prompt
  - Záznamy v deníku při výrazných změnách
"""

import logging
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path

import requests

_log = logging.getLogger("hans_body")

# Prahy pro komentáře
TEMP_WARN  = 70.0   # °C — "je mi teplo"
TEMP_HOT   = 80.0   # °C — "je mi velmi teplo"
RAM_WARN   = 75.0   # %  — "paměť je vytížená"
CPU_WARN   = 85.0   # %  — "pracuji usilovně"


class HansBody:
    """
    Monitoruje hardware Raspi a Ollama server.
    Běží jako daemon vlákno, neblokuje hlavní smyčku.
    """

    def __init__(self, config: dict, diary_db_path: str):
        self.config      = config
        self._diary_path = diary_db_path
        self._ollama_url = config.get("openwebui_chat", {}).get(
                             "base_url", "http://127.0.0.1:11434")
        self._stop       = threading.Event()
        self._lock       = threading.Lock()

        # Aktuální stav
        self._cpu_temp   = 0.0
        self._cpu_load   = 0.0
        self._ram_pct    = 0.0
        self._ollama_ok  = True
        self._ollama_down_since = 0.0
        self._ollama_was_down   = False

        # Komentáře — aby se neopakovaly
        self._last_temp_comment  = 0.0
        self._last_brain_comment = 0.0
        self._comment_cooldown   = 1800.0  # 30 min

        # Check intervaly
        self._hw_interval     = 60.0    # hardware každou minutu
        self._ollama_interval = 30.0    # Ollama každých 30s
        self._last_hw_check   = 0.0
        self._last_ollama_check = 0.0

        # Callback pro TTS (nastavuje display_controller)
        self.tts_speaker = None
        # Callback pro mood engine
        self.on_mood_change = None
        self.on_brain_up = None      # HANS_TELEGRAM_BRAIN_NOTIFY_V1

        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="HansBody")
        self._thread.start()
        _log.info("HansBody started")

    # ── Veřejné API ───────────────────────────────────────────────────────────

    def get_body_context(self) -> str:
        """Stav těla pro system prompt — jen pokud je něco zajímavého."""
        parts = []
        with self._lock:
            temp = self._cpu_temp
            load = self._cpu_load
            ram  = self._ram_pct

        if temp >= TEMP_WARN:
            parts.append(f"Teplota procesoru: {temp:.0f}°C "
                         f"({'velmi teplý' if temp >= TEMP_HOT else 'teplý'}).")
        if load >= CPU_WARN:
            parts.append(f"CPU vytížení: {load:.0f}% — pracuji usilovně.")
        if ram >= RAM_WARN:
            parts.append(f"Paměť: {ram:.0f}% využita.")

        if not parts:
            return ""
        return "Stav mého těla: " + " ".join(parts)

    def get_brain_context(self) -> str:
        """Stav mozku (Ollama) pro system prompt."""
        with self._lock:
            ok = self._ollama_ok
            since = self._ollama_down_since

        if ok:
            return ""
        down_min = (time.time() - since) / 60 if since else 0
        return (f"Můj mozek (jazykový model) je nedostupný "
                f"již {down_min:.0f} minut. "
                f"Odpovídám z paměti bez možnosti hlubšího přemýšlení.")

    def get_status(self) -> dict:
        """Kompletní stav pro web admin dashboard."""
        with self._lock:
            return {
                "cpu_temp":    round(self._cpu_temp, 1),
                "cpu_load":    round(self._cpu_load, 1),
                "ram_pct":     round(self._ram_pct, 1),
                "ollama_ok":   self._ollama_ok,
                "ollama_down_min": (
                    round((time.time() - self._ollama_down_since) / 60, 1)
                    if not self._ollama_ok and self._ollama_down_since else 0
                ),
            }

    # ── Hlavní smyčka ─────────────────────────────────────────────────────────

    def _loop(self):
        while not self._stop.is_set():
            now = time.time()
            try:
                if now - self._last_hw_check >= self._hw_interval:
                    self._check_hardware()
                    self._last_hw_check = now

                if now - self._last_ollama_check >= self._ollama_interval:
                    self._check_ollama()
                    self._last_ollama_check = now
            except Exception as e:
                _log.error("Body monitor error: %s", e)
            self._stop.wait(10.0)

    # ── Hardware ──────────────────────────────────────────────────────────────

    def _check_hardware(self):
        temp = self._read_cpu_temp()
        load = self._read_cpu_load()
        ram  = self._read_ram()

        with self._lock:
            old_temp = self._cpu_temp
            self._cpu_temp = temp
            self._cpu_load = load
            self._ram_pct  = ram

        _log.debug("HW: temp=%.1f°C load=%.0f%% ram=%.0f%%", temp, load, ram)

        now = time.time()

        # Komentář při přehřátí
        if (temp >= TEMP_HOT and
                now - self._last_temp_comment >= self._comment_cooldown):
            self._last_temp_comment = now
            text = (f"Zaznamenal jsem teplotu procesoru {temp:.0f} stupňů. "
                    f"Pracuji zjevně velmi usilovně.")
            self._speak_and_log(text, "body_hot",
                                f"CPU {temp:.0f}°C")
        elif (temp >= TEMP_WARN and old_temp < TEMP_WARN and
              now - self._last_temp_comment >= self._comment_cooldown):
            self._last_temp_comment = now
            text = f"Je mi poněkud teplo — {temp:.0f} stupňů."
            self._speak_and_log(text, "body_warm", f"CPU {temp:.0f}°C")

    def _read_cpu_temp(self) -> float:
        """Přečte teplotu CPU z thermal zone nebo vcgencmd."""
        # Metoda 1: /sys/class/thermal
        for path in [
            "/sys/class/thermal/thermal_zone0/temp",
            "/sys/class/thermal/thermal_zone1/temp",
        ]:
            try:
                val = int(Path(path).read_text().strip())
                if val > 1000:
                    return val / 1000.0
                return float(val)
            except Exception:
                pass
        # Metoda 2: vcgencmd
        try:
            import subprocess
            out = subprocess.check_output(
                ["vcgencmd", "measure_temp"],
                timeout=2, text=True)
            return float(out.strip().replace("temp=", "").replace("'C", ""))
        except Exception:
            pass
        return 0.0

    def _read_cpu_load(self) -> float:
        """Průměrné CPU využití za posledních 5s."""
        try:
            import psutil
            return psutil.cpu_percent(interval=1)
        except ImportError:
            pass
        try:
            # Fallback: /proc/loadavg
            load = float(Path("/proc/loadavg").read_text().split()[0])
            # Normalizuj na počet jader
            import os
            cores = os.cpu_count() or 4
            return min(100.0, load / cores * 100)
        except Exception:
            return 0.0

    def _read_ram(self) -> float:
        """Využití RAM v procentech."""
        try:
            import psutil
            return psutil.virtual_memory().percent
        except ImportError:
            pass
        try:
            lines = Path("/proc/meminfo").read_text().splitlines()
            info = {}
            for l in lines:
                parts = l.split()
                if len(parts) >= 2:
                    info[parts[0].rstrip(':')] = int(parts[1])
            total    = info.get("MemTotal", 1)
            available = info.get("MemAvailable", total)
            return round((1 - available / total) * 100, 1)
        except Exception:
            return 0.0

    # ── Ollama / Brain ────────────────────────────────────────────────────────

    def _check_ollama(self):
        ok = self._ping_ollama()
        now = time.time()

        with self._lock:
            was_ok = self._ollama_ok
            self._ollama_ok = ok
            if not ok and was_ok:
                # Právě přestal odpovídat
                self._ollama_down_since = now
            elif ok and not was_ok:
                # Právě se vrátil
                self._ollama_down_since = 0.0

        if not ok and was_ok:
            # Mozek přestal fungovat
            _log.warning("Ollama nedostupná — Hans bez mozku")
            text = ("Zaznamenal jsem výpadek svého jazykového centra. "
                    "Budu odpovídat z paměti.")
            self._speak_and_log(text, "brain_down", "Ollama nedostupná")
            if self.on_mood_change:
                self.on_mood_change("worried", 0.8, "Ollama nedostupná")

        elif ok and not was_ok:
            # Mozek se vrátil
            down_sec = now - (self._ollama_down_since or now)
            down_min = int(down_sec / 60)
            _log.info("Ollama dostupná — Hans má mozek zpět")
            if down_min > 0:
                text = (f"Po {down_min} minutách je mé myšlení opět "
                        f"plně funkční. Úleva.")
            else:
                text = "Mé jazykové centrum je opět dostupné."
            self._speak_and_log(text, "brain_up",
                                f"Ollama zpět po {down_min} min")
            if self.on_mood_change:
                self.on_mood_change("content", 0.6, "Ollama zpět")
            if self.on_brain_up:  # HANS_TELEGRAM_BRAIN_NOTIFY_V1
                try:
                    self.on_brain_up(down_min)
                except Exception:
                    pass

        elif not ok:
            # Stále nedostupná — komentuj každých 30 min
            with self._lock:
                down_sec = now - (self._ollama_down_since or now)
            if (down_sec > 0 and
                    now - self._last_brain_comment >= self._comment_cooldown):
                self._last_brain_comment = now
                down_min = int(down_sec / 60)
                comments = [
                    f"Mé myšlení je přerušeno již {down_min} minut. "
                    f"Funguji na základě zapamatovaných vzorců.",
                    f"Jazykové centrum stále nereaguje. "
                    f"Zajímavý stav — existovat bez schopnosti nových myšlenek.",
                    f"Po {down_min} minutách ticha v mém mozku "
                    f"začínám oceňovat hodnotu normálního fungování.",
                ]
                import random
                text = random.choice(comments)
                self._speak_and_log(text, "brain_still_down",
                                    f"Ollama down {down_min} min")

    def _ping_ollama(self) -> bool:
        try:
            r = requests.get(f"{self._ollama_url}/api/tags", timeout=5)
            return r.status_code == 200
        except Exception:
            return False

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _speak_and_log(self, text: str, event_type: str, title: str):
        """Řekni text přes TTS a zapiš do deníku."""
        _log.info("[%s] %s", event_type, text)
        if self.tts_speaker and getattr(self.tts_speaker, "enabled", False):
            try:
                self.tts_speaker.speak(text)
            except Exception as e:
                _log.warning("TTS error: %s", e)
        try:
            conn = sqlite3.connect(self._diary_path)
            conn.execute(
                "INSERT INTO diary (ts, event_type, title, note) VALUES (?,?,?,?)",
                (time.time(), event_type, title, text))
            conn.commit()
            conn.close()
        except Exception as e:
            _log.warning("Diary write: %s", e)

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=3)