"""
Room Observer
Jednou denně pořídí snímek z kamery, pošle na llava-phi3 a uloží popis místnosti.
Popis je pak dostupný pro LLM kontext.
"""
import base64
import logging
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
import requests

_log = logging.getLogger("room_observer")


class RoomObserver:

    def __init__(self, config: dict, diary_db_path: str):
        self.config       = config
        self._diary_path  = diary_db_path
        self._last_obs    = 0.0
        self._description = ""
        self._lock        = threading.Lock()
        self._pending_frame = None  # frame cekajici na zpracovani

        cfg = config.get("room_observer", {})
        self._interval_h  = float(cfg.get("interval_hours", 24.0))
        self._ollama_url  = config.get("openwebui_chat", {}).get(
            "base_url", "http://127.0.0.1:11434")
        self._model       = cfg.get("model", "qwen2.5vl:7b")
        self._prompt      = cfg.get("prompt",
            "Popiš místnost na obrázku stručně česky. "
            "Zaměř se na: nábytek, osvětlení, předměty, celkovou atmosféru. "
            "Max 3 věty.")
        self._enabled     = bool(cfg.get("enabled", True))

        # Nacti posledni popis z DB
        self._load_last()

        if self._enabled:
            _log.info("RoomObserver ready — interval=%.0fh model=%s",
                      self._interval_h, self._model)

    def _load_last(self):
        """Nacti posledni popis z deniku."""
        try:
            conn = sqlite3.connect(self._diary_path)
            row  = conn.execute("""
                SELECT ts, note FROM diary
                WHERE event_type='room_description'
                ORDER BY ts DESC LIMIT 1
            """).fetchone()
            conn.close()
            if row:
                self._last_obs    = row[0]
                self._description = row[1] or ""
                dt = datetime.fromtimestamp(row[0]).strftime("%d.%m. %H:%M")
                _log.info("Loaded room description from %s", dt)
        except Exception as e:
            _log.error("Load error: %s", e)

    def submit_frame(self, frame: np.ndarray):
        """Zavolej z hlavni smycky s aktualnim frame."""
        if not self._enabled:
            return
        now = time.time()
        if now - self._last_obs < self._interval_h * 3600:
            return
        with self._lock:
            self._pending_frame = frame.copy()
        # Spust zpracovani v samostatnem vlaknu
        threading.Thread(target=self._process, daemon=True).start()
        self._last_obs = now  # zabran opakovanemu spusteni

    def _process(self):
        with self._lock:
            frame = self._pending_frame
            self._pending_frame = None
        if frame is None:
            return

        _log.info("Capturing room description...")
        try:
            # Uloz frame jako JPEG
            img_path = Path("/tmp/room_snapshot.jpg")
            bgr = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)
            cv2.imwrite(str(img_path), bgr, [cv2.IMWRITE_JPEG_QUALITY, 90])

            # Zakoduj do base64
            with open(img_path, "rb") as f:
                img_b64 = base64.b64encode(f.read()).decode("utf-8")

            # Posli na vision model (qwen2.5vl) — VRAM dance
            # ROOM_OBSERVER_VRAM_UNLOAD_V1: qwen-VL ~14.7G se nevejde vedle rezidentního
            # hans-czech (10.8G) → odlož chat → popiš → nahřej chat zpět (jako hodnocení obrazů).
            # OLLAMA_CLIENT_PATCH_ROOMOBSERVER
            from scripts.ollama_client import ollama_generate
            try:
                from scripts.avatar_render import _ollama_loaded, _ollama_unload, _ollama_warm
                _vram = True
            except Exception:
                _vram = False
            if _vram:
                _ollama_unload(self.config, _ollama_loaded(self.config))
            try:
                desc = ollama_generate(
                    self._model,
                    self._prompt,
                    images=[img_b64],
                    ollama_url=self._ollama_url,
                    keep_alive=0,  # vision on-demand, po popisu uvolni VRAM
                )
            finally:
                if _vram:
                    _dlg = (self.config.get("models", {}) or {}).get("dialog", "hans-czech:latest")
                    _ollama_warm(self.config, _dlg)

            if not desc:
                return

            _log.info("Room description: %s", desc[:100])
            self._description = desc

            # Uloz do deniku
            conn = sqlite3.connect(self._diary_path)
            conn.execute(
                "INSERT INTO diary (ts, event_type, title, note) VALUES (?,?,?,?)",
                (time.time(), "room_description", "Popis místnosti", desc))
            conn.commit()
            conn.close()

        except Exception as e:
            _log.error("Room observer error: %s", e)

    def get_context_string(self) -> str:
        """Vrať popis pro LLM kontext."""
        if not self._description:
            return ""
        dt = datetime.fromtimestamp(self._last_obs).strftime("%d.%m. %H:%M")
        return f"Popis místnosti (pořízeno {dt}): {self._description}"