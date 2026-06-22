"""
Voice listener — aktivace gestem.

Použití:
    listener.trigger()   # zavolá gesture handler při detekci gesta
                         # spustí nahrávání → STT → LLM → TTS
"""

import io, struct, subprocess, sys, time, logging, threading, queue
import requests
import numpy as np

log = logging.getLogger("voice")
if not log.handlers:
    _h = logging.StreamHandler(sys.stderr)
    _h.setFormatter(logging.Formatter("%(message)s"))
    log.addHandler(_h)
log.setLevel(logging.DEBUG)

try:
    import webrtcvad as _webrtcvad
    _VAD_OK = True
except ImportError:
    _VAD_OK = False


def _to_wav_bytes(pcm: np.ndarray, sr: int) -> bytes:
    data = pcm.astype(np.int16).tobytes()
    buf  = io.BytesIO()
    buf.write(struct.pack('<4sI4s', b'RIFF', 36 + len(data), b'WAVE'))
    buf.write(struct.pack('<4sIHHIIHH', b'fmt ', 16, 1, 1, sr, sr*2, 2, 16))
    buf.write(struct.pack('<4sI', b'data', len(data)))
    buf.write(data)
    return buf.getvalue()


class VoiceListener:

    def __init__(self, config: dict, chat_handler):
        self.config       = config
        self.chat_handler = chat_handler
        self._thread      = None
        self._running     = False
        self._proc        = None
        self._recording   = threading.Event()  # gesture nastaví na True
        self._processing  = threading.Event()  # STT běží
        self._stop_requested = False           # push-to-talk stop

        vcfg = config.get("voice", {})
        self.enabled      = vcfg.get("enabled", False)
        self.stt_url      = vcfg.get("stt_url",
                            "http://127.0.0.1:8080/api/v1/audio/transcriptions")
        self.stt_token    = vcfg.get("stt_token", "")
        self.alsa_device  = vcfg.get("alsa_device", "plughw:3,0")
        self._spk_device  = config.get("tts", {}).get("alsa_device", "plughw:2,0")
        self.sample_rate  = int(vcfg.get("sample_rate", 16000))
        self.max_speech_s = float(vcfg.get("max_speech_seconds", 12.0))
        self.silence_s    = float(vcfg.get("silence_seconds", 1.5))
        self.min_rec_s    = float(vcfg.get("min_recording_seconds", 1.0))
        self.vad_mode     = int(vcfg.get("vad_aggressiveness", 2))
        self.default_name = vcfg.get("default_speaker", "")  # PORTABILITY: z configu
        self.get_visible_person = None
        self._tts         = None
        self._ok          = self.enabled
        self._vad_dirty   = False  # set True by reload_config to recreate Vad

        # WAKE_WORD_OWW_V1 — hands-free aktivace přes openWakeWord (lokální, ~8.5x RT)
        self._oww = None
        self._wake_threshold = float(vcfg.get("wake_threshold", 0.5))
        if vcfg.get("wake_enabled", False):
            try:
                import os, openwakeword as _oww_pkg
                from openwakeword.model import Model as _OWWModel
                _mname = vcfg.get("wake_model", "hey_jarvis_v0.1")
                if os.path.isfile(_mname):
                    _mpath = _mname
                else:
                    _fn = _mname if _mname.endswith(".onnx") else _mname + ".onnx"
                    _mpath = os.path.join(os.path.dirname(_oww_pkg.__file__),
                                          "resources", "models", _fn)
                self._oww = _OWWModel(wakeword_model_paths=[_mpath])
                log.info(f"[Voice] WakeWord ON — {os.path.basename(_mpath)} "
                      f"(thr={self._wake_threshold})")
            except Exception as _we:
                log.info(f"[Voice] WakeWord init failed: {_we}")
                self._oww = None

    # ── Public API ────────────────────────────────────────────────────────────

    def start(self):
        if not self._ok:
            return
        self._running = True
        self._thread  = threading.Thread(target=self._loop, daemon=True,
                                         name="VoiceListener")
        self._thread.start()
        log.info("[Voice] Ready — waiting for gesture trigger")

    def stop(self):
        self._running = False
        self._recording.set()  # odblokuj čekání
        if self._proc:
            try: self._proc.kill()
            except: pass
        if self._thread:
            self._thread.join(timeout=3)
        log.info("[Voice] Stopped")

    def trigger(self):
        """Zavolej z gesture handleru — spustí nahrávání."""
        if not self._running:
            return
        if self._recording.is_set():
            log.info("[Voice] Already recording — ignored")
            return
        if self._processing.is_set():
            log.info("[Voice] STT busy — ignoring trigger")
            return
        log.info("[Voice] Gesture trigger — recording...")
        self._stop_requested = False
        self._beep("beep_start")
        self._recording.set()

    @property
    def is_recording(self) -> bool:
        return self._recording.is_set()

    def _beep(self, sound: str):
        """Přehraj beep zvuk přes speaker."""
        try:
            wav_path = f"data/sounds/{sound}.wav"
            subprocess.Popen(
                ["aplay", "-D", self._spk_device, wav_path],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
        except Exception:
            pass

    def stop_recording(self):
        """Zavolej při uvolnění gesta — ukončí nahrávání."""
        if self._recording.is_set():
            self._stop_requested = True

    def reload_config(self, config: dict):
        vcfg = config.get("voice", {})
        self.silence_s    = float(vcfg.get("silence_seconds", self.silence_s))
        self.default_name = vcfg.get("default_speaker", self.default_name)
        new_vad_mode = int(vcfg.get("vad_aggressiveness", self.vad_mode))
        if new_vad_mode != self.vad_mode:
            self.vad_mode = new_vad_mode
            # Signal the loop to recreate the Vad object with the new mode
            self._vad_dirty = True

    # ── arecord ───────────────────────────────────────────────────────────────

    def _start_arecord(self):
        cmd = ["arecord", "-D", self.alsa_device,
               "-f", "S16_LE", "-r", str(self.sample_rate),
               "-c", "1", "--buffer-size=16384", "-t", "raw", "-"]
        return subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, bufsize=0)

    # ── STT ───────────────────────────────────────────────────────────────────

    def _denoise(self, pcm: np.ndarray) -> np.ndarray:
        """Odstraň šum pomocí noisereduce — použij první 0.5s jako noise profile."""
        try:
            import noisereduce as nr
            # První 0.5s = šum (před řečí)
            noise_sample = pcm[:int(self.sample_rate * 0.5)]
            if len(noise_sample) < 100:
                return pcm
            reduced = nr.reduce_noise(
                y=pcm.astype(np.float32),
                sr=self.sample_rate,
                y_noise=noise_sample.astype(np.float32),
                prop_decrease=0.8,   # 80% redukce šumu
                stationary=True,
            )
            return reduced.astype(np.int16)
        except ImportError:
            return pcm  # noisereduce není k dispozici
        except Exception as e:
            log.info(f"[Voice] Denoise error: {e}", file=__import__('sys').stderr)
            return pcm

    def _stt(self, pcm: np.ndarray) -> str:
        try:
            pcm = self._denoise(pcm)
            wav     = _to_wav_bytes(pcm, self.sample_rate)
            headers = {"Authorization": f"Bearer {self.stt_token}"} if self.stt_token else {}
            resp    = requests.post(
                self.stt_url, headers=headers,
                files={"file": ("speech.wav", wav, "audio/wav")},
                data={"model": "whisper-1", "language": "cs"},
                timeout=15)
            if resp.status_code == 200:
                return resp.json().get("text", "").strip()
        except Exception as e:
            print(f"[Voice] STT error: {e}")
        return ""

    # ── Main loop ─────────────────────────────────────────────────────────────

    def _loop(self):
        sr         = self.sample_rate
        frame_ms   = 30
        frame_samp = sr * frame_ms // 1000
        frame_blen = frame_samp * 2

        try:
            self._proc = self._start_arecord()
        except Exception as e:
            log.info(f"[Voice] arecord failed: {e}")
            return

        # Reader thread — neustále drénuje pipe
        _q = queue.Queue(maxsize=500)
        def _reader():
            while self._running:
                chunk = self._proc.stdout.read(frame_blen)
                if not chunk:
                    break
                try: _q.put_nowait(chunk)
                except queue.Full: pass
            _q.put(None)
        threading.Thread(target=_reader, daemon=True, name="VoiceReader").start()

        vad        = _webrtcvad.Vad(self.vad_mode) if _VAD_OK else None
        max_silent = int(self.silence_s * 1000 / frame_ms)
        log.info(f"[Voice] _loop start — wake={'ON' if self._oww is not None else 'OFF'} "
                 f"thr={self._wake_threshold} vad={_VAD_OK}")  # WAKE_WORD_LOG_V1

        while self._running:
            # Recreate Vad if aggressiveness changed via reload_config
            if self._vad_dirty and _VAD_OK:
                vad = _webrtcvad.Vad(self.vad_mode)
                self._vad_dirty = False
                log.info(f"[Voice] VAD aggressiveness updated to {self.vad_mode}")
            max_silent = int(self.silence_s * 1000 / frame_ms)
            # Čekej na gesture trigger (nebo wake word)
            triggered = self._recording.wait(timeout=0.5)
            if not triggered or not self._running:
                # WAKE_WORD_OWW_V1 — frames místo zahození prožeň wake detektorem.
                # Gate: ne když běží STT nebo Hans mluví (self-trigger TTS).
                _busy = (self._processing.is_set()
                         or (self._tts is not None
                             and self._tts.is_speaking()))
                if self._oww is not None and self._running and not _busy:
                    # WAKE_WORD_CHUNK_FIX_V1 — bufferuj a krm oww 1280-vzorkovými
                    # (80ms) bloky; jednotlivé pipe framy bývají <400 vzorků →
                    # oww padá ('min 400 samples'). Zbytek <1280 do dalšího kola.
                    _bmax = 0.0; _bname = ""; _fed = 0
                    _chunks = []
                    while not _q.empty():
                        try: fr = _q.get_nowait()
                        except Exception: break
                        if fr is None:
                            continue
                        _chunks.append(np.frombuffer(fr, dtype=np.int16))
                    if _chunks:
                        _prev = getattr(self, "_wake_buf", None)
                        if _prev is not None and len(_prev):
                            _chunks.insert(0, _prev)
                        _cat = np.concatenate(_chunks)
                        _i = 0; _CK = 1280; _hit = False
                        while len(_cat) - _i >= _CK:
                            _block = _cat[_i:_i + _CK]; _i += _CK
                            try:
                                scores = self._oww.predict(_block)
                                _fed += 1
                                if scores:
                                    _mk = max(scores, key=scores.get)
                                    _mv = float(scores[_mk])
                                    if _mv > _bmax: _bmax, _bname = _mv, _mk
                                    if _mv >= self._wake_threshold:
                                        log.info("[Voice] WAKE " + str({k: round(float(v), 2)
                                                 for k, v in scores.items()}))
                                        self._oww.reset()
                                        self._stop_requested = False
                                        self._beep("beep_start")
                                        self._recording.set()
                                        self._open_voice_popup()  # VOICE_TRANSCRIPT_POPUP_V1
                                        _hit = True
                                        break
                            except Exception as _pe:
                                log.info(f"[Voice] wake predict ERR: {_pe!r}")
                        self._wake_buf = None if _hit else _cat[_i:]
                else:
                    # Vyprázdni frontu aby se nehromadily staré frames
                    while not _q.empty():
                        try: _q.get_nowait()
                        except Exception: pass
                continue

            # Nahrávej dokud není ticho nebo max délka
            speech_frames = []
            silent_frames = 0
            speech_start  = time.time()

            log.info("[Voice] Recording...")

            while self._running:
                try:
                    frame = _q.get(timeout=0.5)
                except queue.Empty:
                    continue
                if frame is None:
                    break

                speech_frames.append(np.frombuffer(frame, dtype=np.int16))

                is_speech = False
                if vad:
                    try: is_speech = vad.is_speech(frame, sr)
                    except: pass
                else:
                    is_speech = np.abs(np.frombuffer(frame, dtype=np.int16)).mean() > 300

                if is_speech:
                    silent_frames = 0
                else:
                    silent_frames += 1

                elapsed = time.time() - speech_start
                if self._stop_requested and elapsed >= self.min_rec_s:
                    log.info(f"[Voice] Done — {elapsed:.1f}s (gesture released)")
                    break
                if elapsed > self.max_speech_s:
                    log.info(f"[Voice] Done — {elapsed:.1f}s (maxlen)")
                    break
                if silent_frames >= max_silent and elapsed >= self.min_rec_s:
                    log.info(f"[Voice] Done — {elapsed:.1f}s (silence)")
                    break

            # Resetuj trigger
            self._recording.clear()
            self._beep("beep_stop")

            if speech_frames:
                audio = np.concatenate(speech_frames)
                if len(audio) / sr > 0.3:
                    threading.Thread(target=self._process,
                                     args=(audio,), daemon=True).start()

            log.info("[Voice] Ready — waiting for gesture trigger")

        try: self._proc.kill()
        except: pass

    # ── STT + dispatch ────────────────────────────────────────────────────────

    def _process(self, audio: np.ndarray):
        # VOICE_ACK_PHRASE_V1 — Hans řekne krátké uznání (místo pípnutí), v charakteru
        # majordoma. Gender-neutrální (sedí na muže i ženu). TTS cache → po pár užitích
        # instant; generování stejně skryté za STT (~7s).
        try:
            import random as _rnd
            _acks = ["Okamžik, prosím.",
                     "Nechte mě chvíli přemýšlet.",
                     "Zajisté, hned to bude.",
                     "Dovolte mi to zvážit.",
                     "Jistě, již na tom pracuji."]
            if self._tts is not None and getattr(self._tts, "enabled", False):
                self._tts.speak(_rnd.choice(_acks), priority=True)
        except Exception:
            pass
        log.info(f"[Voice] STT {len(audio)/self.sample_rate:.1f}s...")
        text = self._stt(audio)
        if not text:
            log.info("[Voice] Empty")
            return
        log.info(f"[Voice] Heard: {text}")
        self._voice_popup_msg("Vy (hlas)", text)  # VOICE_TRANSCRIPT_POPUP_V1
        self._dispatch(text)

    def _open_voice_popup(self, name=None):  # VOICE_TRANSCRIPT_POPUP_V1
        """Při aktivačním slově otevři okno s přepisem (co Hans slyšel + říká),
        ať je vidět práce voice recognition. Gate voice.transcript_popup."""
        vcfg = self.config.get("voice", {}) or {}
        if not vcfg.get("transcript_popup", True):
            return
        try:
            p = getattr(self, "_voice_popup", None)
            if p is not None and getattr(p, "active", False) and getattr(p, "root", None):
                return
            if name is None:
                try:
                    name = (self.get_visible_person() if self.get_visible_person else None)
                except Exception:
                    name = None
                name = name or self.default_name
            from scripts.popup_chat_window import SimplePopupChat
            self._voice_popup = SimplePopupChat(self.chat_handler, name, 1.0,
                                                already_greeted=True)
            self._voice_popup.external_message("🎤 Hlas", "Poslouchám…")
        except Exception as e:
            log.info(f"[Voice] transcript popup open failed: {e}")

    def _voice_popup_msg(self, sender, text):  # VOICE_TRANSCRIPT_POPUP_V1
        try:
            p = getattr(self, "_voice_popup", None)
            if p is not None and getattr(p, "active", False) and text:
                p.external_message(sender, text)
        except Exception:
            pass

    def _dispatch(self, text: str):
        try:
            ch = self.chat_handler
            if not ch or not getattr(ch, "enabled", False):
                return
            name = None
            if self.get_visible_person:
                try: name = self.get_visible_person()
                except: pass
            name = name or self.default_name
            log.info(f"[Voice] → {name}: {text}")
            # VOICE_STREAMING_ACK_V1 — streaming TTS: mluv větu po větě jak LLM
            # generuje (1. věta priority=interrupt idle, zbytek queue). Hans
            # začne mluvit po STT, ne až po celé ~14s odpovědi.
            tts = getattr(ch, "tts_speaker", None)
            _spoke = {"any": False}
            def _on_sentence(s):
                if tts and getattr(tts, "enabled", False) and s and s.strip():
                    tts.speak(s, priority=not _spoke["any"])
                    _spoke["any"] = True
            response = ch.send_chat_message(name, text, on_sentence=_on_sentence)
            if not response:
                return
            log.info(f"[Voice] ← {response[:80]}")
            try:  # VOICE_TRANSCRIPT_POPUP_V1 — Hansovu odpověď do přepisu
                from scripts.hans_persona import persona_name as _pn
                self._voice_popup_msg(_pn(self.config), response)
            except Exception:
                self._voice_popup_msg("Hans", response)
            # Fallback: streaming nic neřeklo (slash command) → řekni celé
            if tts and getattr(tts, "enabled", False) and not _spoke["any"]:
                threading.Thread(target=tts.speak,
                    args=(response,), kwargs={"priority": True},
                    daemon=True).start()
            pm = getattr(ch, "popup_manager", None)
            if pm and getattr(ch, "popup_enabled", False):
                pm.handle_face_detection(name, 1.0, already_greeted=True)
        except Exception as e:
            log.info(f"[Voice] dispatch error: {e}")
