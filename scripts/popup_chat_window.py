"""
Popup Chat Window
Tkinter chat window that opens when a known face is recognised.
Uses shared TkManager root — no threads, no mainloop().
pump() in the main OpenCV loop drives all Tkinter events.
"""

import tkinter as tk
from tkinter import scrolledtext
import threading          # ← must be at module level (was missing — caused UnboundLocalError)
import queue
import time
from datetime import datetime

from scripts.tk_manager import tk_mgr
import re

# Citation markery z RAG odpovědí ([1], [3, 4]). Stripujeme je před TTS,
# aby Hans nevyslovoval čísla. UI v okně dál uvidí plný text s markery.
_CITATION_RE = re.compile(r"\s*\[\s*\d+(?:\s*,\s*\d+)*\s*\]")

try:
    from scripts.config_manager import should_debug, debug_print
except ImportError:
    def should_debug(config): return False
    def debug_print(config, message): pass


class SimplePopupChat:
    """Popup chat window as a Toplevel on the shared Tk root."""

    def __init__(self, chat_handler, person_name, confidence, already_greeted=False,
                 initial_question=None, question_id=None):  # HANS_QUESTION_POPUP_V1
        self.chat_handler       = chat_handler
        self.person_name        = person_name
        self.confidence         = confidence
        self.already_greeted    = already_greeted
        self.initial_question   = initial_question
        self.question_id        = question_id
        self._question_answered = False
        self.response_queue     = queue.Queue()
        self.active             = True
        self.processing_message = False
        self.message_lock       = threading.Lock()
        self.config             = getattr(chat_handler, 'config', {})
        self.tts_speaker        = getattr(chat_handler, 'tts_speaker', None)
        self.tts_enabled        = bool(self.tts_speaker and self.tts_speaker.enabled)
        self.root               = None

        try:
            # Use call_soon() — safe from any thread, fires on next pump()
            tk_mgr.call_soon(self._create_window)
        except Exception as e:
            print(f"[POPUP] Schedule error: {e}")

    # ── Window creation (runs on main thread via after) ───────────────────────

    def _create_window(self):
        try:
            handler_type = ("OpenWebUI"
                            if hasattr(self.chat_handler, 'base_url')
                            and 'localhost:8080' in str(self.chat_handler.base_url)
                            else "Ollama")

            self.root = tk.Toplevel(tk_mgr.get_root())
            self.root.title(f"Chat — {self.person_name}")
            self.root.geometry("520x480")
            self.root.protocol("WM_DELETE_WINDOW", self.safe_close)

            self._setup_ui(handler_type)
            self._center_window()
            self._schedule_queue_check()

        except Exception as e:
            print(f"[POPUP] Window creation error: {e}")
            import traceback
            traceback.print_exc()

    def _setup_ui(self, handler_type):
        header = tk.Frame(self.root, bg='#2c3e50', height=44)
        header.pack(fill=tk.X)
        header.pack_propagate(False)

        tk.Label(header, text=f"  {self.person_name}",
                 font=("Arial", 13, "bold"),
                 bg='#2c3e50', fg='white').pack(side=tk.LEFT, pady=8)

        tk.Button(header, text="✕", command=self.safe_close,
                  bg='#e74c3c', fg='white', font=('Arial', 11),
                  relief=tk.FLAT, padx=8).pack(side=tk.RIGHT, padx=6, pady=6)

        self.chat = scrolledtext.ScrolledText(
            self.root, height=16,
            font=("Arial", 10), state=tk.DISABLED,
            wrap=tk.WORD, bg='#f9f9f9')
        self.chat.pack(fill=tk.BOTH, expand=True, padx=8, pady=6)

        input_frame = tk.Frame(self.root)
        input_frame.pack(fill=tk.X, padx=8, pady=(0, 4))

        self.entry = tk.Entry(input_frame, font=("Arial", 11))
        self.entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 6))
        self.entry.bind('<Return>', self._on_send)
        self.entry.focus()

        self.send_btn = tk.Button(input_frame, text="Odeslat",
                                  command=self._on_send,
                                  font=("Arial", 10))
        self.send_btn.pack(side=tk.RIGHT)

        tts_frame = tk.Frame(self.root)
        tts_frame.pack(fill=tk.X, padx=8, pady=(0, 6))

        tts_lbl = "🔊 TTS: ZAP" if self.tts_enabled else "🔇 TTS: VYP"
        tts_bg  = "#a9dfbf" if self.tts_enabled else "#f1948a"
        self.tts_btn = tk.Button(tts_frame, text=tts_lbl,
                                 command=self._toggle_tts,
                                 font=("Arial", 9), bg=tts_bg)
        self.tts_btn.pack(side=tk.LEFT)

        self.status_lbl = tk.Label(tts_frame, text="Připraven.",
                                   font=("Arial", 9), fg="#7f8c8d")
        self.status_lbl.pack(side=tk.LEFT, padx=10)

        tk.Label(tts_frame, text="/note <text> = trvalá poznámka",
                 font=("Arial", 8), fg="#aaaaaa").pack(side=tk.RIGHT, padx=6)

        # HANS_QUESTION_POPUP_V1 — okno otevřel Hans s konkrétní otázkou →
        # zobraz ji jako Hansovu zprávu místo generické hlášky.
        if self.initial_question:
            try:
                from scripts.hans_persona import persona_name as _pn
                _who = _pn(self.config)
            except Exception:
                _who = "Hans"
            self._add_message(_who, self.initial_question)
        else:
            self._add_message("Systém", "Dobrý den. Jak vám mohu pomoci?")

    # ── Messaging ─────────────────────────────────────────────────────────────

    def _on_send(self, event=None):
        if self.processing_message:
            return
        msg = self.entry.get().strip()
        if not msg or not self.active:
            return
        with self.message_lock:
            if self.processing_message:
                return
            self.processing_message = True
        self.entry.delete(0, tk.END)
        self._add_message("Vy", msg)
        self.status_lbl.config(text="AI přemýšlí…")
        self.send_btn.config(state=tk.DISABLED)
        threading.Thread(target=self._send_to_ai, args=(msg,), daemon=True).start()

    def _send_to_ai(self, message):
        try:
            # HANS_QUESTION_POPUP_V1 — první odpověď uživatele = odpověď na
            # položenou otázku → označ ji zodpovězenou (jen jednou).
            if self.question_id and not self._question_answered:
                self._question_answered = True
                try:
                    _qs = self.chat_handler._questions_store()
                    if _qs is not None:
                        _qs.answer_question(self.question_id, message, via="popup")
                except Exception as _ae:
                    print(f"[POPUP] answer_question failed: {_ae}")
            response = None
            for attempt in range(2):
                try:
                    if hasattr(self.chat_handler, 'send_chat_message'):
                        # HANS_CHAT_CHANNEL_AWARE_V1 — desktop popup tag
                        response = self.chat_handler.send_chat_message(
                            self.person_name, message, channel="popup")
                    elif hasattr(self.chat_handler, '_send_ollama_message'):
                        prompt = self._build_prompt(message)
                        response = self.chat_handler._send_ollama_message(prompt)
                    elif hasattr(self.chat_handler, '_send_openwebui_message'):
                        response = self.chat_handler._send_openwebui_message(message)
                    if response:
                        break
                except Exception as e:
                    if attempt == 1:
                        response = f"Chyba komunikace: {e}"
                    time.sleep(1)

            text = response or "Omlouvám se, žádná odpověď."
            self.response_queue.put(("ai", text))

            # SLASH_CMD_NO_TTS_V1 — výstup slash příkazu (/help, /nitky…) nečíst nahlas
            _is_slash_cmd = (message or "").strip().startswith("/")
            if (not _is_slash_cmd) and self.tts_speaker and self.tts_speaker.enabled and self.tts_enabled and response:
                # Strip [N], [N, M] markery — TTS by je vyslovoval doslova
                tts_text = _CITATION_RE.sub("", response).strip()
                if tts_text:
                    threading.Thread(
                        target=lambda: self.tts_speaker.speak(tts_text),
                        daemon=True).start()

        except Exception as e:
            self.response_queue.put(("ai", f"Chyba: {e}"))
        finally:
            self.processing_message = False

    def _build_prompt(self, message):
        # PERSONA_REFACTOR_1_4 — jednotný zdroj identity
        from scripts.hans_persona import persona_core
        system_base = persona_core(self.config)
        known_persons = self.config.get("known_persons", {})
        if known_persons:
            lines = []
            for pname, pdata in known_persons.items():
                if isinstance(pdata, dict):
                    gender = pdata.get("gender", "")
                    notes  = pdata.get("notes", "").strip()
                    line   = f"- {pname}"
                    if gender == "žena":   line += " (ženského rodu)"
                    elif gender == "muž":  line += " (mužského rodu)"
                    if notes:              line += f": {notes}"
                else:
                    line = f"- {pname}"
                lines.append(line)
            persons_ctx = "\n\nZnáš tyto osoby z domu:\n" + "\n".join(lines)
        else:
            persons_ctx = ""

        profile = known_persons.get(self.person_name, {})
        if isinstance(profile, dict) and profile.get("gender") == "žena":
            current_ctx = f"\n\nAktuálně mluvíš s {self.person_name}, která je ženského rodu."
        elif isinstance(profile, dict) and profile.get("gender") == "muž":
            current_ctx = f"\n\nAktuálně mluvíš s {self.person_name}, který je mužského rodu."
        else:
            current_ctx = f"\n\nAktuálně mluvíš s {self.person_name}."
        if isinstance(profile, dict) and profile.get("notes"):
            current_ctx += f" {profile['notes']}"

        return system_base + persons_ctx + current_ctx, message

    # ── Queue check ───────────────────────────────────────────────────────────

    def _schedule_queue_check(self):
        if self.active and self.root:
            try:
                self.root.after(100, self._check_queue)
            except Exception:
                pass

    def _check_queue(self):
        if not self.active or not self.root:
            return
        try:
            while True:
                msg_type, msg_data = self.response_queue.get_nowait()
                if msg_type == "ai":
                    self._add_message("AI", msg_data)
                    try:
                        self.status_lbl.config(text="Připraven.")
                        self.send_btn.config(state=tk.NORMAL)
                    except Exception:
                        pass
                elif msg_type == "disp":  # VOICE_TRANSCRIPT_POPUP_V1
                    _snd, _txt = msg_data
                    self._add_message(_snd, _txt)
        except queue.Empty:
            pass
        except Exception:
            pass
        self._schedule_queue_check()

    # ── Helpers ───────────────────────────────────────────────────────────────

    def external_message(self, sender, message):  # VOICE_TRANSCRIPT_POPUP_V1
        """Thread-safe display-only zpráva (přepis hlasu) — nepouští AI."""
        try:
            self.response_queue.put(("disp", (sender, message)))
        except Exception:
            pass

    def _add_message(self, sender, message):
        if not self.active or not self.root:
            return
        try:
            self.chat.config(state=tk.NORMAL)
            ts = datetime.now().strftime("%H:%M:%S")
            self.chat.insert(tk.END, f"[{ts}] {sender}:\n{message}\n\n")
            self.chat.see(tk.END)
            self.chat.config(state=tk.DISABLED)
        except Exception:
            pass

    def _toggle_tts(self):
        self.tts_enabled = not self.tts_enabled
        lbl = "🔊 TTS: ZAP" if self.tts_enabled else "🔇 TTS: VYP"
        bg  = "#a9dfbf"     if self.tts_enabled else "#f1948a"
        try:
            self.tts_btn.config(text=lbl, bg=bg)
        except Exception:
            pass

    def _center_window(self):
        try:
            self.root.update_idletasks()
            sw = self.root.winfo_screenwidth()
            sh = self.root.winfo_screenheight()
            self.root.geometry(f"520x480+{(sw-520)//2}+{(sh-480)//2}")
        except Exception:
            pass

    def safe_close(self):
        self.active = False
        self.processing_message = False
        try:
            while True: self.response_queue.get_nowait()
        except queue.Empty:
            pass
        if self.root:
            try:
                self.root.destroy()
            except Exception:
                pass
            self.root = None

    def close(self):
        self.safe_close()


# =============================================================================
# Popup Manager
# =============================================================================

class PopupChatManager:
    """Manages popup chat windows — one per recognised person."""

    def __init__(self, chat_handler):
        self.chat_handler     = chat_handler
        self.active_popups    = {}
        self.last_popup_times = {}
        self.config           = getattr(chat_handler, 'config', {})

        if hasattr(chat_handler, 'chat_config'):
            popup_cfg = chat_handler.chat_config.get('popup_chat', {})
        else:
            popup_cfg = self.config.get('openwebui_direct', {}).get('popup_chat', {})

        self.enabled        = popup_cfg.get('enabled', True)
        self.min_confidence = popup_cfg.get('min_confidence', 0.55)
        self.cooldown       = popup_cfg.get('window_cooldown', 60)
        self.max_popups     = popup_cfg.get('max_windows', 2)

        tts_ok = bool(getattr(chat_handler, 'tts_speaker', None))
        print(f"PopupChatManager initialized for {type(chat_handler).__name__}")
        print(f"  Enabled: {self.enabled}")
        print(f"  Min confidence: {self.min_confidence}")
        print(f"  TTS available: {tts_ok}")

    def handle_face_detection(self, person_name, confidence, already_greeted=False):
        try:
            print(f"[POPUP] handle_face_detection: name={person_name} "
                  f"conf={confidence:.2f} enabled={self.enabled} "
                  f"min_conf={self.min_confidence}")

            if not self.enabled:
                return
            if confidence < self.min_confidence:
                print(f"[POPUP] SKIP: confidence {confidence:.2f} < {self.min_confidence}")
                return

            now  = time.time()
            last = self.last_popup_times.get(person_name, 0)

            if now - last < self.cooldown:
                print(f"[POPUP] SKIP: cooldown {self.cooldown-(now-last):.0f}s remaining")
                return
            if person_name in self.active_popups:
                p = self.active_popups[person_name]
                if p.active and p.root:
                    print(f"[POPUP] SKIP: popup already open for {person_name}")
                    return
                else:
                    del self.active_popups[person_name]

            if len(self.active_popups) >= self.max_popups:
                print(f"[POPUP] SKIP: max popups reached ({self.max_popups})")
                return

            print(f"[POPUP] Creating popup for {person_name}...")
            popup = SimplePopupChat(
                self.chat_handler, person_name, confidence, already_greeted)
            self.active_popups[person_name]    = popup
            self.last_popup_times[person_name] = now

        except Exception as e:
            debug_print(self.config, f"Failed to create popup for {person_name}: {e}")

    def cleanup_closed(self):
        for name in list(self.active_popups.keys()):
            p = self.active_popups[name]
            if not p.active or not p.root:
                del self.active_popups[name]

    def close_all_windows(self):
        for popup in list(self.active_popups.values()):
            try: popup.safe_close()
            except: pass
        self.active_popups.clear()

    def get_active_count(self):
        self.cleanup_closed()
        return len(self.active_popups)
