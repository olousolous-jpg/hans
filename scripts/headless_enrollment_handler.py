"""
Headless Enrollment Handler
===========================
Used when display.headless = true and an unknown face is detected.

Flow:
  1. Take the best crop from the unknown-face session
  2. Send it to Ollama vision model (e.g. llava) for a spoken description
  3. Speak the description via TTS so the user knows who is at the door
  4. Open a minimal Tk popup with the description + name entry
  5. On confirm → enroll all collected embeddings into FaceDB
  6. On skip   → discard session

Config keys (under "headless_enrollment"):
    vision_model    : str   — Ollama model with vision support (default "llava")
    describe_prompt : str   — override the vision prompt
    window_timeout  : int   — seconds before popup auto-skips (0 = never, default 0)

Uses openwebui_chat.base_url for the Ollama endpoint (no separate config needed).
"""

import base64
import json
import os
import threading
import time
import tkinter as tk
from pathlib import Path

import cv2
import numpy as np
import re
import requests

from scripts.tk_manager import tk_mgr
from scripts.logger import get_logger

_log = get_logger("headless_enroll")

# ── Colours (match existing dark UI) ─────────────────────────────────────────
BG     = "#1e1e2e"
BG2    = "#2a2a3e"
BG3    = "#313145"
ACCENT = "#89b4fa"
GREEN  = "#a6e3a1"
RED    = "#f38ba8"
ORANGE = "#fab387"
FG     = "#cdd6f4"
FG2    = "#a6adc8"

_DEFAULT_PROMPT = (
    "Describe the person in this image briefly in 1-2 sentences. "
    "Focus on: gender, approximate age, hair colour and style, "
    "and any distinctive features (glasses, beard, hat, clothing colour). "
    "Be concise and factual. Reply in Czech."
)


class HeadlessEnrollmentHandler:
    """
    Manages the full headless enrollment flow for one unknown-face session.

    Instantiate once per session from UnknownFaceCollector._open_enrollment_window()
    when headless mode is active.  Mirrors the public API of UnknownEnrollmentWindow
    so the collector doesn't need to know which mode is active:
        .add_photo(img_path, embedding)
        .set_collecting_done()
    """

    def __init__(self, session_dir: Path, embeddings: list,
                 face_db, config: dict, tts_speaker=None, on_done=None,
                 target_count: int = 20):
        self.session_dir  = session_dir
        self.embeddings   = list(embeddings)
        self.face_db      = face_db
        self.config       = config
        self.tts          = tts_speaker
        self.on_done      = on_done
        self.target_count = target_count
        self.active       = True

        he_cfg = config.get("headless_enrollment", {})
        chat_cfg = config.get("openwebui_chat", {})

        self._ollama_url   = chat_cfg.get("base_url", "http://localhost:11434")
        self._vision_model = he_cfg.get("vision_model", "qwen2.5vl:7b")
        self._prompt          = he_cfg.get("describe_prompt", _DEFAULT_PROMPT)
        self._translate_prompt = he_cfg.get(
            "translate_prompt",
            "Preloz do cestiny jednou vetou, bez tabulek: {description}"
        )
        self._chat_model      = config.get("openwebui_chat", {}).get(
            "model_name", "jobautomation/OpenEuroLLM-Czech:latest")
        self._timeout_s    = int(he_cfg.get("window_timeout", 0))

        # Live-update state
        self._img_paths: list[Path] = list(session_dir.glob("*.jpg"))
        self._collecting_done = False
        self._description: str = ""
        self._win     = None
        self._srv_dot = None
        self._srv_lbl = None
        self._mdl_dot = None
        self._mdl_lbl = None

        # Schedule window creation on Tk thread
        tk_mgr.call_soon(self._create_window)
        _log.info("HeadlessEnrollmentHandler started — session %s", session_dir.name)

    # ── Public API (mirrors UnknownEnrollmentWindow) ──────────────────────────

    def add_photo(self, img_path: Path, embedding):
        """Called by collector when a new frame arrives."""
        if not self.active:
            return
        self.embeddings.append(embedding)
        self._img_paths.append(img_path)
        if self._win:
            try:
                self._win.after(0, self._refresh_count)
            except Exception:
                pass

    def set_collecting_done(self):
        """Called by collector when target_count reached."""
        self._collecting_done = True
        if self._win:
            try:
                self._win.after(0, self._on_collecting_done)
            except Exception:
                pass

    # ── Window ────────────────────────────────────────────────────────────────

    def _create_window(self):
        root = tk_mgr.get_root()
        if root is None:
            tk_mgr.call_soon(self._create_window)
            return

        self._win = tk.Toplevel(root)
        self._win.title("👤 Neznámá osoba — Headless Enrollment")
        self._win.configure(bg=BG)
        self._win.resizable(False, False)
        self._win.protocol("WM_DELETE_WINDOW", self._on_skip)

        self._build_ui()
        self._center_window()

        # Kick off status check + vision describe in background
        threading.Thread(target=self._check_ollama_status, daemon=True).start()
        threading.Thread(target=self._run_vision_describe, daemon=True).start()

        if self._timeout_s > 0:
            self._win.after(self._timeout_s * 1000, self._on_skip)

        _log.info("Headless enrollment window opened")

    def _build_status_bar(self, parent: "tk.Frame"):
        """Ollama connectivity indicator — shown at top of window."""
        bar = tk.Frame(parent, bg=BG3, pady=4)
        bar.pack(fill=tk.X, padx=16, pady=(4, 0))

        # Server label
        tk.Label(bar, text="Ollama:",
                 bg=BG3, fg=FG2, font=("Consolas", 8)).pack(side=tk.LEFT, padx=(4, 2))
        tk.Label(bar, text=self._ollama_url,
                 bg=BG3, fg=ACCENT, font=("Consolas", 8)).pack(side=tk.LEFT)

        # Server status dot + text
        self._srv_dot  = tk.Label(bar, text="●", bg=BG3, fg=ORANGE,
                                  font=("Consolas", 10))
        self._srv_dot.pack(side=tk.LEFT, padx=(8, 2))
        self._srv_lbl  = tk.Label(bar, text="spojuji...",
                                  bg=BG3, fg=ORANGE, font=("Consolas", 8))
        self._srv_lbl.pack(side=tk.LEFT)

        # Separator
        tk.Label(bar, text="│", bg=BG3, fg=BG2,
                 font=("Consolas", 9)).pack(side=tk.LEFT, padx=8)

        # Model label
        tk.Label(bar, text="Model:",
                 bg=BG3, fg=FG2, font=("Consolas", 8)).pack(side=tk.LEFT, padx=(0, 2))
        tk.Label(bar, text=self._vision_model,
                 bg=BG3, fg=ACCENT, font=("Consolas", 8)).pack(side=tk.LEFT)

        # Model status dot + text
        self._mdl_dot  = tk.Label(bar, text="●", bg=BG3, fg=ORANGE,
                                  font=("Consolas", 10))
        self._mdl_dot.pack(side=tk.LEFT, padx=(8, 2))
        self._mdl_lbl  = tk.Label(bar, text="...",
                                  bg=BG3, fg=ORANGE, font=("Consolas", 8))
        self._mdl_lbl.pack(side=tk.LEFT)

    def _update_status(self, server_ok: bool, model_ok: bool,
                       available_models: list):
        """Update the status bar widgets — called from Tk thread via after()."""
        if not self._win:
            return
        # Guard: widgets may be None if _build_status_bar hasn't run yet
        if not all([self._srv_dot, self._srv_lbl, self._mdl_dot, self._mdl_lbl]):
            # Retry in 200ms
            try:
                self._win.after(200, lambda: self._update_status(
                    server_ok, model_ok, available_models))
            except Exception:
                pass
            return
        try:
            if server_ok:
                self._srv_dot.config(fg=GREEN)
                self._srv_lbl.config(text="online", fg=GREEN)
            else:
                self._srv_dot.config(fg=RED)
                self._srv_lbl.config(text="offline", fg=RED)

            if not server_ok:
                self._mdl_dot.config(fg=RED)
                self._mdl_lbl.config(text="nedostupný", fg=RED)
            elif model_ok:
                self._mdl_dot.config(fg=GREEN)
                self._mdl_lbl.config(text="ready", fg=GREEN)
            else:
                self._mdl_dot.config(fg=RED)
                vision = [m for m in available_models
                          if any(v in m for v in
                                 ["qwen2.5vl", "qwen2-vl", "llava", "vision",
                                  "moondream", "bakllava", "minicpm"])]
                hint = f" — zkus: {vision[0]}" if vision else ""
                self._mdl_lbl.config(text=f"není stažen{hint}", fg=RED)
        except Exception as e:
            import logging
            logging.getLogger("headless_enroll").debug(
                "_update_status error: %s", e)


    def _check_ollama_status(self):
        """Background thread — check server + model, then update status bar.
        Uses tk_mgr.call_soon() instead of self._win.after() because this
        runs in a background thread and Tkinter after() requires the main thread.
        """
        import requests as _req
        server_ok = False
        model_ok  = False
        available = []
        try:
            r = _req.get(f"{self._ollama_url}/api/tags", timeout=5)
            if r.status_code == 200:
                server_ok = True
                available = [m.get("name", "") for m in r.json().get("models", [])]
                model_ok  = any(
                    n == self._vision_model or
                    n.startswith(self._vision_model + ":")
                    for n in available
                )
                _log.info("Ollama status: online  model_ok=%s  available=%s",
                          model_ok, available)
            else:
                _log.warning("Ollama /api/tags returned HTTP %d", r.status_code)
        except Exception as e:
            _log.warning("Ollama server check failed: %s", e)

        # tk_mgr.call_soon() is thread-safe — fires on next pump() in main loop
        _s = server_ok
        _m = model_ok
        _a = available
        tk_mgr.call_soon(lambda: self._update_status(_s, _m, _a))


    def _build_ui(self):
        # ── Header ────────────────────────────────────────────────────────
        hdr = tk.Frame(self._win, bg=BG2, pady=8)
        hdr.pack(fill=tk.X)
        tk.Label(hdr, text="👤  Neznámá osoba detekována",
                 font=("Segoe UI", 13, "bold"),
                 bg=BG2, fg=ACCENT).pack(side=tk.LEFT, padx=16)

        # ── Ollama status bar ─────────────────────────────────────────────
        self._build_status_bar(self._win)

        # ── Snapshot preview (best crop) ──────────────────────────────────
        preview_frame = tk.Frame(self._win, bg=BG, pady=8)
        preview_frame.pack(fill=tk.X, padx=16)

        self._img_label = tk.Label(preview_frame, bg=BG3,
                                   width=22, height=11,
                                   text="⏳ Načítám snímek…",
                                   fg=FG2, font=("Segoe UI", 9))
        self._img_label.pack(side=tk.LEFT, padx=(0, 16))

        # ── Description area ──────────────────────────────────────────────
        right = tk.Frame(preview_frame, bg=BG)
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        tk.Label(right, text="Popis osoby (AI):",
                 bg=BG, fg=ACCENT, font=("Segoe UI", 9, "bold")).pack(anchor="w")

        self._desc_text = tk.Text(right, bg=BG3, fg=FG,
                                  font=("Segoe UI", 10),
                                  relief="flat", wrap=tk.WORD,
                                  height=7, width=36,
                                  state=tk.DISABLED)
        self._desc_text.pack(fill=tk.BOTH, expand=True, pady=(4, 0))

        self._desc_status = tk.Label(right, text="⏳ Analyzuji obličej…",
                                     bg=BG, fg=ORANGE,
                                     font=("Segoe UI", 8))
        self._desc_status.pack(anchor="w", pady=(2, 0))

        # ── Count / progress ──────────────────────────────────────────────
        prog_frame = tk.Frame(self._win, bg=BG)
        prog_frame.pack(fill=tk.X, padx=16, pady=(0, 4))
        self._count_lbl = tk.Label(prog_frame, text=self._count_text(),
                                   bg=BG, fg=FG2, font=("Segoe UI", 8))
        self._count_lbl.pack(side=tk.LEFT)

        # ── Name entry ────────────────────────────────────────────────────
        name_frame = tk.Frame(self._win, bg=BG, pady=6)
        name_frame.pack(fill=tk.X, padx=16)

        tk.Label(name_frame, text="Jméno osoby:",
                 bg=BG, fg=FG, font=("Segoe UI", 11)).pack(side=tk.LEFT)

        self._name_var = tk.StringVar()
        self._name_entry = tk.Entry(name_frame, textvariable=self._name_var,
                                    bg=BG3, fg=FG, insertbackground=FG,
                                    relief="flat", font=("Consolas", 12), width=22)
        self._name_entry.pack(side=tk.LEFT, padx=10)
        self._name_entry.bind("<Return>", lambda _: self._on_enroll())
        self._name_entry.focus()

        self._status_lbl = tk.Label(name_frame, text="",
                                    bg=BG, fg=GREEN, font=("Segoe UI", 9))
        self._status_lbl.pack(side=tk.LEFT, padx=8)

        # ── Buttons ───────────────────────────────────────────────────────
        btn_frame = tk.Frame(self._win, bg=BG2, pady=8)
        btn_frame.pack(fill=tk.X, side=tk.BOTTOM)

        tk.Button(btn_frame, text="✓  Enrollovat",
                  command=self._on_enroll,
                  bg=GREEN, fg=BG, font=("Segoe UI", 11, "bold"),
                  relief="flat", padx=20, cursor="hand2").pack(side=tk.RIGHT, padx=8)

        tk.Button(btn_frame, text="✕  Přeskočit",
                  command=self._on_skip,
                  bg=BG3, fg=RED, font=("Segoe UI", 10),
                  relief="flat", padx=16, cursor="hand2").pack(side=tk.RIGHT)

        tk.Button(btn_frame, text="🔊  Znovu přečíst",
                  command=self._speak_description,
                  bg=BG3, fg=ACCENT, font=("Segoe UI", 9),
                  relief="flat", padx=12, cursor="hand2").pack(side=tk.LEFT, padx=8)

        # Load best snapshot into preview
        self._load_preview()

    # ── Vision describe ───────────────────────────────────────────────────────

    def _best_crop_path(self) -> Path | None:
        """Pick the largest-face crop from the session as the best image."""
        paths = sorted(self.session_dir.glob("*.jpg"))
        if not paths:
            return None
        # Pick the one with the largest detected face area — we stored HQ crops
        # all at the same display size (160×160), so just pick the middle frame
        # which is most likely to be well-lit and centred.
        mid = len(paths) // 2
        return paths[mid]

    def _check_vision_model(self) -> bool:
        """Query /api/tags on remote Ollama — warn if vision model is missing."""
        try:
            resp = requests.get(f"{self._ollama_url}/api/tags", timeout=5)
            if resp.status_code != 200:
                return True  # can't check — proceed anyway
            names = [m.get("name", "") for m in resp.json().get("models", [])]
            found = any(
                n == self._vision_model or n.startswith(self._vision_model + ":")
                for n in names
            )
            if not found:
                _log.warning(
                    "Vision model '%s' not found on %s. Available: %s",
                    self._vision_model, self._ollama_url, names
                )
                msg = (
                    f"Model '{self._vision_model}' není na Ollama serveru.\n"
                    f"Dostupné: {', '.join(names) or 'žádné'}\n"
                    f"Spusť: ollama pull {self._vision_model}"
                )
                self._schedule_description(msg)
                return False
            return True
        except Exception as e:
            _log.debug("Model check failed (non-fatal): %s", e)
            return True

    def _start_thinking(self):
        """Start animated dots in description box while vision model is working."""
        import time
        self._thinking = True
        self._think_start = time.time()

        def _animate():
            if not self._thinking or not self._win:
                return
            elapsed = int(time.time() - self._think_start)
            dots    = "." * (3 + (elapsed % 4))
            # Colour shifts from orange → yellow as time passes (looks alive)
            color   = "#a6e3a1" if elapsed < 2 else (
                      "#f9e2af" if elapsed < 5 else "#fab387")
            try:
                self._desc_text.config(state="normal")
                self._desc_text.delete("1.0", "end")
                self._desc_text.insert("end",
                    f"⏳ vision model analyzuje obličej{dots}\n\n"
                    f"Čas: {elapsed}s  (GPU · RX6800)")
                self._desc_text.config(state="disabled")
                self._desc_status.config(
                    text=f"⏳ Analyzuji... {elapsed}s / max 20s",
                    fg=color
                )
                self._win.after(500, _animate)
            except Exception:
                pass

        if self._win:
            try:
                self._win.after(0, _animate)
            except Exception:
                pass

    def _stop_thinking(self):
        """Stop the animation."""
        self._thinking = False

    def _translate_description(self, english_text: str) -> str:
        """Stage 2: translate English vision output to Czech via chat model."""
        # Flatten bullet points/tabs to a single line before translating
        cleaned = re.sub(r"[\*\-•\t]", "", english_text)
        cleaned = re.sub(r"\s*\n\s*", ", ", cleaned).strip().strip(",")
        cleaned = re.sub(r",\s*,", ",", cleaned).strip()
        prompt = self._translate_prompt.format(description=cleaned)
        try:
            resp = requests.post(
                f"{self._ollama_url}/api/chat",
                json={
                    "model": self._chat_model,
                    "messages": [{"role": "user", "content": prompt}],
                    "stream": False,
                },
                timeout=60,
            )
            if resp.status_code == 200:
                translated = (
                    resp.json().get("message", {}).get("content", "").strip()
                )
                if translated:
                    _log.info("Translation: %s", translated)
                    return translated
            _log.warning("Translation HTTP %d", resp.status_code)
        except Exception as e:
            _log.warning("Translation failed: %s — using English", e)
        return english_text

    def _run_vision_describe(self):
        """
        Send best crop to Ollama /api/chat with images inside the message object.
        This is the correct Ollama vision API — images must be inside the message,
        NOT as a top-level field.
        """
        self._start_thinking()
        try:
            img_path = self._best_crop_path()
            if img_path is None or not img_path.exists():
                self._set_description("Snímek není k dispozici.")
                return

            self._check_vision_model()

            with open(img_path, "rb") as f:
                img_b64 = base64.b64encode(f.read()).decode("utf-8")

            _log.info("Sending image → %s  model=%s  endpoint=/api/chat",
                      self._ollama_url, self._vision_model)

            # images must be inside the message object, not top-level
            payload = {
                "model": self._vision_model,
                "messages": [
                    {
                        "role":    "user",
                        "content": self._prompt,
                        "images":  [img_b64],
                    }
                ],
                "stream": False,
                "keep_alive": 0,  # MODEL_KEEPALIVE_TIERS_V1 — vision on-demand, po popisu uvolni VRAM
            }

            # ENROLL_VRAM_UNLOAD_V1: qwen-VL ~14.7G se nevejde vedle hans-czech (10.8G)
            # → odlož chat → popiš → nahřej chat zpět (jako hodnocení obrazů / room_observer).
            try:
                from scripts.avatar_render import _ollama_loaded, _ollama_unload, _ollama_warm
                _vram = True
            except Exception:
                _vram = False
            if _vram:
                _ollama_unload(self.config, _ollama_loaded(self.config))
            try:
                resp = requests.post(
                    f"{self._ollama_url}/api/chat",
                    json=payload,
                    timeout=60,  # studený qwen-VL load (~28s) + popis
                )
            finally:
                if _vram:
                    _dlg = (self.config.get("models", {}) or {}).get("dialog", "hans-czech:latest")
                    _ollama_warm(self.config, _dlg)

            if resp.status_code == 200:
                description = (
                    resp.json().get("message", {}).get("content", "").strip()
                    or "Popis není k dispozici."
                )
            else:
                _log.warning("Ollama vision HTTP %d — %s",
                             resp.status_code, resp.text[:300])
                if resp.status_code == 404:
                    description = (
                        f"Model '{self._vision_model}' nenalezen.\n"
                        f"Spusť: ollama pull {self._vision_model}"
                    )
                else:
                    description = f"Chyba analýzy (HTTP {resp.status_code})."

        except requests.exceptions.ConnectionError:
            _log.warning("Cannot reach Ollama at %s", self._ollama_url)
            description = f"Ollama není dostupná ({self._ollama_url})."
        except requests.exceptions.Timeout:
            _log.warning("Ollama vision timed out after 60s")
            description = "Analýza trvala příliš dlouho (timeout 20s)."
        except Exception as e:
            _log.error("Vision describe error: %s", e)
            description = "Chyba při analýze obličeje."

        self._stop_thinking()

        # Stage 2: translate English → Czech (skip error messages)
        _skip = ("Model '", "Ollama", "Chyba", "Analýza", "Snímek")
        if description and not any(description.startswith(s) for s in _skip):
            _en = description
            tk_mgr.call_soon(lambda d=_en: self._set_description(
                f"Překládám...\n\n{d}"))
            description = self._translate_description(description)

        self._description = description
        _log.info("Vision description: %s", description)

        if self._win:
            try:
                tk_mgr.call_soon(lambda: self._set_description(description))
            except Exception:
                pass

        # Speak the description
        self._speak_description()

    def _set_description(self, text: str):
        """Update description text widget (call from Tk thread)."""
        try:
            self._desc_text.config(state=tk.NORMAL)
            self._desc_text.delete("1.0", tk.END)
            self._desc_text.insert(tk.END, text)
            self._desc_text.config(state=tk.DISABLED)
            self._desc_status.config(
                text="✓ Analýza dokončena", fg=GREEN)
        except Exception:
            pass

    def _speak_description(self):
        """Speak the current description via TTS (safe to call any time)."""
        if not self.tts or not self.tts.enabled:
            return
        text = self._description or "Neznámá osoba detekována, zadejte jméno."
        threading.Thread(
            target=lambda: self.tts.speak(text, priority=True),
            daemon=True,
        ).start()

    # ── Preview image ─────────────────────────────────────────────────────────

    def _load_preview(self):
        """Load the best crop into the Tk label as a PhotoImage."""
        try:
            from PIL import Image, ImageTk
        except ImportError:
            self._img_label.config(text="(PIL není k dispozici)")
            return

        img_path = self._best_crop_path()
        if img_path is None or not img_path.exists():
            self._img_label.config(text="(žádný snímek)")
            return

        try:
            img = Image.open(img_path).resize((160, 160), Image.LANCZOS)
            self._photo_ref = ImageTk.PhotoImage(img)   # keep reference
            self._img_label.config(image=self._photo_ref, text="",
                                   width=160, height=160)
        except Exception as e:
            self._img_label.config(text=f"(chyba: {e})")

    # ── Enrollment actions ────────────────────────────────────────────────────

    def _on_enroll(self):
        name = self._name_var.get().strip()
        if not name:
            self._status_lbl.config(text="⚠ Zadej jméno!", fg=ORANGE)
            return

        enrolled = 0
        for emb in self.embeddings:
            if emb is not None:
                if self.face_db.add(name, emb, force=True):
                    enrolled += 1

        if enrolled > 0:
            self._status_lbl.config(
                text=f"✓ Enrollováno {enrolled} snímků jako '{name}'",
                fg=GREEN)
            _log.info("Enrolled '%s' — %d embeddings", name, enrolled)

            # Speak confirmation
            if self.tts and self.tts.enabled:
                threading.Thread(
                    target=lambda: self.tts.speak(
                        f"Osoba uložena jako {name}.", priority=True),
                    daemon=True,
                ).start()

            self._win.after(1500, lambda: self._finish(name))
        else:
            self._status_lbl.config(
                text="⚠ Enrollování selhalo — embeddingy chybí", fg=RED)
            _log.warning("Enroll failed for '%s' — no valid embeddings", name)

    def _on_skip(self):
        _log.info("Headless enrollment skipped")
        self._finish(None)

    def _finish(self, name_or_none):
        self.active = False
        if self.on_done:
            self.on_done(name_or_none)
        if self._win:
            try:
                self._win.destroy()
            except Exception:
                pass
            self._win = None

    # ── Live update helpers ───────────────────────────────────────────────────

    def _refresh_count(self):
        if self._count_lbl:
            try:
                self._count_lbl.config(text=self._count_text())
            except Exception:
                pass

    def _on_collecting_done(self):
        if self._count_lbl:
            try:
                n = len(self._img_paths)
                self._count_lbl.config(
                    text=f"✓ {n}/{self.target_count} snímků — sbírání dokončeno",
                    fg=GREEN)
            except Exception:
                pass
        # Refresh preview with a later (better-lit) frame
        self._load_preview()

    def _count_text(self) -> str:
        n = len(self._img_paths)
        if self._collecting_done:
            return f"✓ {n}/{self.target_count} snímků — hotovo"
        return f"Sbírám... {n}/{self.target_count} snímků"

    def _center_window(self):
        try:
            self._win.update_idletasks()
            sw = self._win.winfo_screenwidth()
            sh = self._win.winfo_screenheight()
            w, h = 620, 400
            self._win.geometry(f"{w}x{h}+{(sw-w)//2}+{(sh-h)//2}")
        except Exception:
            pass