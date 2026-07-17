"""
Hans Introspection
Autonomní smyčka — Hans periodicky reflektuje co zažil
ve vztahu ke své existenci jako majordomus.

Ne faktické záznamy, ale skutečný vnitřní monolog:
  "Můj pán dnes přišel pozdě. Zaznamenal jsem únavu v jeho chůzi."
  "Místnost je prázdná již čtyři hodiny. Tikání hodin je hlasitější."
  "Přečetl jsem o Krakatoě. Sopky mě fascinují — síla bez záměru."

Výstup jde do deníku jako 'introspection' a vstupuje do system promptu.
"""

import logging
import random
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path

import requests

_log = logging.getLogger("hans_introspection")


class HansIntrospection:
    """
    Periodicky generuje Hansův vnitřní monolog na základě
    toho co Hans zažil — kamera, lidé, počasí, četba.
    """

    def __init__(self, config: dict, diary_db_path: str):
        self.config       = config
        self._diary_path  = diary_db_path
        self._ollama      = config.get("openwebui_chat", {}).get(
                              "base_url", "http://127.0.0.1:11434")
        self._model       = (config.get("models", {}).get("utility")
                              or config.get("hans_dialog", {}).get("ollama_model")
                              or config.get("openwebui_chat", {}).get(
                                  "model_name", "hans-czech:latest"))
        from scripts.hans_persona import persona_core  # PERSONA_REFACTOR_1_4
        self._persona     = persona_core(config)
        self._stop        = threading.Event()
        self._lock        = threading.Lock()
        self._last_thought = 0.0
        self._interval    = float(
            config.get("hans_idle", {}).get("dialog_interval_min", 20)) * 60

        # Reference na ostatní moduly — nastavuje display_controller
        self._room_observer = None
        self._curiosity     = None
        self._weather       = None
        self._kodi_monitor  = None

        # Cache posledních myšlenek pro LLM kontext
        self._recent_thoughts: list[str] = []
        self._load_recent()

        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="HansIntrospection")
        self._thread.start()
        _log.info("HansIntrospection started — interval=%.0fs", self._interval)

    # ── Veřejné API ───────────────────────────────────────────────────────────

    def trigger(self, reason: str = ""):
        """Vyvolej okamžitou introspekci — např. při příchodu osoby."""
        threading.Thread(
            target=self._think,
            args=(reason,),
            daemon=True
        ).start()

    def get_context_string(self, max_items: int = 2) -> str:
        """Vrať poslední myšlenky pro system prompt."""
        if not self._recent_thoughts:
            return ""
        items = self._recent_thoughts[:max_items]
        return "Hansovy poslední myšlenky:\n" + "\n".join(f"- {t}" for t in items)

    def set_idle(self, is_idle: bool, alone_hours: float = 0):
        """Informuj o idle stavu — spustí introspekci pokud je čas."""
        if not is_idle:
            return
        now = time.time()
        if now - self._last_thought >= self._interval:
            self._last_thought = now
            threading.Thread(
                target=self._think,
                args=(f"sám {alone_hours:.1f}h",),
                daemon=True
            ).start()

    # ── Hlavní smyčka ─────────────────────────────────────────────────────────

    def _loop(self):
        while not self._stop.is_set():
            self._stop.wait(60.0)

    def _think(self, reason: str = ""):
        """Generuj vnitřní monolog pomocí LLM."""
        with self._lock:
            pass  # jen jeden najednou — lock jen pro log

        _log.info("Introspecting... reason=%s", reason or "periodic")

        # Sestav kontext
        context = self._build_context(reason)

        # Prompt — Hans píše vnitřní monolog, ne instrukce
        prompt = (
            f"Napiš svůj vnitřní monolog — jednu nebo dvě věty které "
            f"si říkáš sám pro sebe. Nezačínej jménem ani dvojtečkou. "
            f"Piš v první osobě. Buď konkrétní, ne obecný. "
            f"Reaguj na to co právě zažíváš:\n\n{context}"
        )

        thought = self._call_ollama(prompt)
        if not thought:
            return

        _log.info("Thought: %s", thought[:100])

        # Ulož do deníku
        self._save(thought, reason)

        # Cache pro LLM kontext
        self._recent_thoughts.insert(0, thought)
        self._recent_thoughts = self._recent_thoughts[:5]

    def _build_context(self, reason: str) -> str:
        """Sestav popis aktuální situace pro LLM."""
        parts = []

        # Čas a den
        now  = datetime.now()
        hour = now.hour
        if 5 <= hour < 12:    tod = "ráno"
        elif 12 <= hour < 18: tod = "odpoledne"
        elif 18 <= hour < 22: tod = "večer"
        else:                  tod = "v noci"
        parts.append(f"Je {now.strftime('%H:%M')}, {tod}.")

        # Důvod
        if reason:
            parts.append(f"Situace: {reason}.")

        # Počasí
        if self._weather:
            wx = self._weather.get_weather()
            if wx:
                desc = wx.get("description", "")
                temp = wx.get("temp_current")
                s = desc
                if temp: s += f" {temp:.0f}°C"
                parts.append(f"Venku: {s}.")

        # Popis místnosti
        if self._room_observer:
            ctx = self._room_observer.get_context_string()
            if ctx:
                parts.append(ctx)

        # Co Hans četl
        if self._curiosity and self._curiosity._recent:
            r = self._curiosity._recent[0]
            parts.append(f"Naposledy jsem četl o: {r.title}.")

        # Kodi
        if self._kodi_monitor:
            np = self._kodi_monitor.get_now_playing_context()
            if np:
                parts.append(np)

        # Poslední události z deníku
        try:
            conn = sqlite3.connect(self._diary_path)
            rows = conn.execute("""
                SELECT event_type, title, note FROM diary
                WHERE event_type IN ('idle_start','idle_end','movie_browsed',
                                     'web_read','teddy_dialog','room_description')
                ORDER BY ts DESC LIMIT 4
            """).fetchall()
            conn.close()
            for etype, title, note in rows:
                if etype == "idle_start":
                    parts.append("Jsem sám v domě.")
                elif etype == "idle_end" and note:
                    parts.append(note)
                elif etype == "movie_browsed" and title:
                    parts.append(f"Přemýšlel jsem o filmu '{title}'.")
                elif etype == "web_read" and title:
                    parts.append(f"Přečetl jsem o '{title}'.")
        except Exception as e:
            _log.debug("Diary read: %s", e)

        return "\n".join(parts)

    # ── Ollama ────────────────────────────────────────────────────────────────

    def _call_ollama(self, prompt: str) -> str | None:
        # OLLAMA_CLIENT_PATCH_INTROSPECTION
        from scripts.ollama_client import ollama_chat
        return ollama_chat(
            self._model,
            [
                {"role": "system", "content": self._persona},
                {"role": "user",   "content": prompt},
            ],
            ollama_url=self._ollama,
            # HANS_DIARY_LONGER_V1 (17.7.) — 80 tokens = ~1 věta; 250 dá prostor
            # na 3-4 věty introspekce (deník má být rozpoznatelný text, ne heslo).
            options={"num_predict": 250},
        )

    # ── Persistence ───────────────────────────────────────────────────────────

    def _save(self, thought: str, reason: str):
        try:
            conn = sqlite3.connect(self._diary_path)
            conn.execute(
                "INSERT INTO diary (ts, event_type, title, note) VALUES (?,?,?,?)",
                (time.time(), "introspection",
                 reason or "periodic", thought))
            conn.commit()
            conn.close()
        except Exception as e:
            _log.warning("Save error: %s", e)

    def _load_recent(self):
        try:
            conn = sqlite3.connect(self._diary_path)
            rows = conn.execute("""
                SELECT note FROM diary
                WHERE event_type='introspection'
                ORDER BY ts DESC LIMIT 5
            """).fetchall()
            conn.close()
            self._recent_thoughts = [r[0] for r in rows if r[0]]
            if self._recent_thoughts:
                _log.info("Loaded %d recent thoughts", len(self._recent_thoughts))
        except Exception as e:
            _log.debug("Load thoughts: %s", e)

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=3)