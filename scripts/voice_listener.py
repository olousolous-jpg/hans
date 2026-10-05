"""
Voice listener — aktivace gestem.

Použití:
    listener.trigger()   # zavolá gesture handler při detekci gesta
                         # spustí nahrávání → STT → LLM → TTS
"""

import io, re, struct, subprocess, sys, time, logging, threading, queue
import requests
import numpy as np

log = logging.getLogger("voice")

# VOICE_ACK_PHRASE_V1 — potvrzení po dotazu, gender-neutrální
_ACKS = ["Okamžik, prosím.",
         "Nechte mě chvíli přemýšlet.",
         "Zajisté, hned to bude.",
         "Dovolte mi to zvážit.",
         "Jistě, již na tom pracuji."]
# VOICE_GAP_FILL_V1 (2. 10.) — výplně ticha, než Hans začne odpovídat:
# (po kolika s od přepisu, hlášky). Změřeno 29.–30. 9. (21 výměn, přepis →
# celá odpověď): čas/počasí 3–11 s, agentní akce 12–16 s (bez streamování),
# volný hovor 19–36 s. Jedna hláška hned po dotazu pokryla jen začátek.
_VYPLNE = [
    (1.2, _ACKS),
    (7.0, ["Ještě chviličku, prosím.",
           "Už to skoro mám.",
           "Hned jsem u toho."]),
    (15.0, ["Omlouvám se, trvá to déle než obvykle."]),
]
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


# HANS_STT_TURBO_V1 — známé výmysly Whisperu na tichu/šumu a přepis tvořený
# jen samotným oslovením (nahrávka bez dotazu) → prázdno. Záměrně úzké:
# celá výpověď musí být výmysl, jinak se nic nemaže.
_VYMYSLY_RE = re.compile(
    r"^\W*(titulky\s+(vytvo[řr]il|p[řr]ipravil|pro\b|:).*|.*johnyx.*|.*amara\.org.*|"
    r"(hej|hey|ahoj)?\W*han[szc]i\W*(han[szc]i\W*)*)\W*$", re.I)


# HANS_STT_TURBO_V1 — přepsala poslední výpověď jen záloha (base, 70 % chyb)?
# Čte `_skip_memory` v handleru: takovou výměnu si Hans do paměti nebere.
_POSLEDNI_ZALOHOU = False


def posledni_prepis_zalohou() -> bool:
    return _POSLEDNI_ZALOHOU


def _je_vymysl(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    if not re.search(r"\w", t):          # jen tečky a interpunkce
        return True
    return bool(_VYMYSLY_RE.match(t))


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
        # HANS_STT_TURBO_V1 (29. 9.) — záloha (původní base na CPU) pro herní
        # mód a výpadek PC/tunelu.
        self.stt_url_fallback = vcfg.get("stt_url_fallback", "")
        self.alsa_device  = vcfg.get("alsa_device", "plughw:3,0")
        self._spk_device  = config.get("tts", {}).get("alsa_device", "plughw:2,0")
        self.sample_rate  = int(vcfg.get("sample_rate", 16000))
        self.max_speech_s = float(vcfg.get("max_speech_seconds", 12.0))
        self.silence_s    = float(vcfg.get("silence_seconds", 1.5))
        self.min_rec_s    = float(vcfg.get("min_recording_seconds", 1.0))
        # HANS_VOICE_LEAD_IN_V1 (29. 9.) — kolik času má člověk po oslovení,
        # než začne mluvit; ticho do konce se počítá až od začátku řeči.
        self.lead_in_s    = float(vcfg.get("lead_in_seconds", 5.0))
        self._holdoff_s   = float(vcfg.get("wake_holdoff_seconds", 1.2))  # HANS_WAKE_HOLDOFF_V1
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
        # VOICE_ACK_PREPARED_V1 — potvrzení po dotazu připrav předem (jednou)
        if self._tts is not None and hasattr(self._tts, "priprav"):
            def _priprav():
                try:
                    n = self._tts.priprav([h for _s, hs in _VYPLNE for h in hs])
                    if n:
                        log.info("[Voice] připraveno %d hlášek po dotazu", n)
                except Exception as e:
                    log.info(f"[Voice] příprava hlášek selhala: {e}")
            threading.Thread(target=_priprav, daemon=True, name="voice-ack-prep").start()

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
        # HANS_STT_TURBO_V1 (29. 9.) — primárně whisper.cpp turbo na PC (tunel),
        # při herním módu nebo selhání záloha. Řízený test 20 vět: chybovost
        # slov turbo 11–12 % × base 70 % (backlog STT_TEST_29_09).
        # Turbo dostává SUROVÝ zvuk: odšumění mu škodí (29. 9. živě „Kolik
        # kehojin“ × bez odšumění „Kolik je hodin?“; offline 11 × 12 vět z 20
        # bez chyby) a na Pi trvá ~2,5 s. Odšumění zůstává jen pro zálohu (base).
        try:
            wav_raw = _to_wav_bytes(pcm, self.sample_rate)
        except Exception as e:
            log.info(f"[Voice] STT příprava zvuku selhala: {e}")
            return ""
        _wav_den = []

        def _wav_pro(url):
            if url == self.stt_url:
                return wav_raw
            if not _wav_den:
                try:
                    _wav_den.append(_to_wav_bytes(self._denoise(pcm), self.sample_rate))
                except Exception:
                    _wav_den.append(wav_raw)
            return _wav_den[0]
        headers = {"Authorization": f"Bearer {self.stt_token}"} if self.stt_token else {}
        cesty = [self.stt_url]
        try:
            from scripts.ollama_client import game_mode_on
            if game_mode_on() and self.stt_url_fallback:
                cesty = []                  # při hře grafiku nebereme
        except Exception:
            pass
        if self.stt_url_fallback and self.stt_url_fallback not in cesty:
            cesty.append(self.stt_url_fallback)
        global _POSLEDNI_ZALOHOU
        for i, url in enumerate(cesty):
            _POSLEDNI_ZALOHOU = (url != self.stt_url)
            try:
                resp = requests.post(
                    url, headers=headers,
                    files={"file": ("speech.wav", _wav_pro(url), "audio/wav")},
                    data={"model": "whisper-1", "language": "cs",
                          "response_format": "json"},
                    timeout=10 if i < len(cesty) - 1 else 15)
                if resp.status_code == 200:
                    text = resp.json().get("text", "").strip()
                    if i > 0:
                        log.info("[Voice] STT přes zálohu (%s)" % url.split("/")[2])
                    if _je_vymysl(text):
                        log.info(f"[Voice] STT výmysl zahozen: {text!r}")
                        return ""
                    return text
                log.info(f"[Voice] STT HTTP {resp.status_code} ({url.split('/')[2]})")
            except Exception as e:
                log.info(f"[Voice] STT chyba ({url.split('/')[2]}): {e}")
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
                # WAKE_DIAG_V1 (29. 9.) — přeskakování detekce se dřív nikde
                # neobjevilo; hlas se pak „neslyšel" bez stopy. Hlásí se, když
                # trvá déle než 10 s, i s důvodem.
                if _busy and self._oww is not None:
                    _od = getattr(self, "_busy_od", 0.0) or time.time()
                    self._busy_od = _od
                    if time.time() - _od > 10 and not getattr(self, "_busy_hlaseno", False):
                        self._busy_hlaseno = True
                        log.info("[Voice] wake přeskakován >10 s (%s)" % (
                            "zpracovávám řeč" if self._processing.is_set() else "mluvím"))
                else:
                    if getattr(self, "_busy_hlaseno", False):
                        log.info("[Voice] wake zase poslouchá (po %.0f s)"
                                 % (time.time() - getattr(self, "_busy_od", time.time())))
                    self._busy_od = 0.0
                    self._busy_hlaseno = False
                # HANS_WAKE_HOLDOFF_V1 (29. 9.) — po skončení vlastní řeči ještě
                # chvíli neposlouchat: reproduktor dohrává a dozvuk probudil Hanse
                # sám (21:47:59, skóre 0,98, nikdo nemluvil). Zvuk se zahodí
                # a detektor i jeho zásobník se vynulují.
                if _busy:
                    self._holdoff_do = time.time() + float(getattr(self, "_holdoff_s", 1.2))
                elif time.time() < getattr(self, "_holdoff_do", 0.0):
                    _busy = True
                    self._wake_buf = None
                    try:
                        if self._oww is not None:
                            self._oww.reset()
                    except Exception:
                        pass
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
                                        self._kodi_listening_note()  # HANS_WAKE_KODI_NOTE_V1
                                        _hit = True
                                        break
                            except Exception as _pe:
                                log.info(f"[Voice] wake predict ERR: {_pe!r}")
                        self._wake_buf = None if _hit else _cat[_i:]
                        if not _hit and _bmax >= 0.2:            # WAKE_DIAG_V1
                            log.info("[Voice] wake blízko: %.2f (%s, práh %.2f)"
                                     % (_bmax, _bname, self._wake_threshold))
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
            _mluvil = False     # HANS_VOICE_LEAD_IN_V1 — začal už člověk mluvit?

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

                elapsed = time.time() - speech_start
                if is_speech:
                    silent_frames = 0
                    # prvních 0,5 s dozní samotné oslovení — to za začátek řeči nebereme
                    if elapsed >= 0.5:
                        _mluvil = True
                else:
                    silent_frames += 1
                if self._stop_requested and elapsed >= self.min_rec_s:
                    log.info(f"[Voice] Done — {elapsed:.1f}s (gesture released)")
                    break
                if elapsed > self.max_speech_s:
                    log.info(f"[Voice] Done — {elapsed:.1f}s (maxlen)")
                    break
                # HANS_VOICE_LEAD_IN_V1 (29. 9.) — dřív skončilo po 1,2 s ticha i tehdy,
                # když člověk po „hej Hanzi“ teprve čekal na odezvu (nahrávka 1,7 s,
                # přepis prázdný, věta se nenahrála). Ticho do konce platí až po
                # začátku řeči; kdo nezačne do `lead_in_seconds`, nahrávka skončí.
                if (_mluvil and silent_frames >= max_silent
                        and elapsed >= self.min_rec_s):
                    log.info(f"[Voice] Done — {elapsed:.1f}s (silence)")
                    break
                if not _mluvil and elapsed >= self.lead_in_s:
                    log.info(f"[Voice] Done — {elapsed:.1f}s (nikdo nezačal mluvit)")
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

    def _uloz_nahravku(self, audio, text, stt_s):
        """HANS_VOICE_SAVE_V1 (27. 9.) — sběr pro měření přepisu řeči.
        Ukládá JEN výpověď po oslovení (wake word / gesto), tedy přesně to, co
        šlo do STT — nikdy zvuk místnosti. Syrový zvuk PŘED `_denoise`, aby šly
        modely porovnat i s jiným předzpracováním. Zůstává na Pi: `data/mereni`
        záloha nebere. Po vyhodnocení SMAZAT (rozhodnutí uživatele 27. 9.).
        Vypnuto = `voice.save_audio.enabled` false."""
        c = (self.config.get("voice", {}) or {}).get("save_audio", {}) or {}
        if not c.get("enabled", False) or audio is None or not len(audio):
            return
        try:
            import json as _json, os as _os
            d = c.get("dir", "data/mereni/hlas")
            _os.makedirs(d, exist_ok=True)
            fn = time.strftime("%Y%m%d_%H%M%S") + ".wav"
            with open(_os.path.join(d, fn), "wb") as f:
                f.write(_to_wav_bytes(np.asarray(audio), self.sample_rate))
            with open(_os.path.join(d, "zaznamy.jsonl"), "a", encoding="utf-8") as f:
                f.write(_json.dumps({"ts": time.time(), "file": fn,
                                     "delka_s": round(len(audio) / self.sample_rate, 2),
                                     "stt_s": round(stt_s, 2), "text": text,
                                     "stt_url": self.stt_url}, ensure_ascii=False) + "\n")
            log.info(f"[Voice] STT za {stt_s:.1f} s, uloženo {fn}")
        except Exception as e:
            log.warning(f"[Voice] uložení nahrávky selhalo: {e}")

    def _process(self, audio: np.ndarray):
        log.info(f"[Voice] STT {len(audio)/self.sample_rate:.1f}s...")
        _t0 = time.time()
        text = self._stt(audio)
        self._uloz_nahravku(audio, text, time.time() - _t0)   # HANS_VOICE_SAVE_V1
        if not text:
            log.info("[Voice] Empty")
            return
        log.info(f"[Voice] Heard: {text}")
        # VOICE_ACK_PREPARED_V1 (2. 10.) — uznání AŽ PO PŘEPISU a jen když je
        # dotaz (dřív zaznělo i při falešném probuzení / tichu: „Empty“ 4 ze 14
        # probuzení 29.–30. 9.); řídí ho výplně v `_dispatch` (VOICE_GAP_FILL_V1).
        self._voice_popup_msg("Vy (hlas)", text)  # VOICE_TRANSCRIPT_POPUP_V1
        self._dispatch(text)

    def _matrix_copy(self, ch, name, heard, response):
        """HANS_VOICE_MATRIX_COPY_V1 (28. 9., přání uživatele) — hlasovou výměnu
        pošli i na Matrix: CO HANS SLYŠEL (přepis) + odpověď. Přepis je tam
        schválně — 28. 9. „pusť film Predátor“ → „Půst film prelátor“ a bez něj
        nejde poznat, že vada je v přepisu, ne v Hansovi. Jen osobě, která má
        Matrix účet (`matrix.as_person` / `matrix.users`); ve vlastním vlákně.
        Gate voice.matrix_copy."""
        vcfg = self.config.get("voice", {}) or {}
        if not vcfg.get("matrix_copy", True) or not response:
            return
        mcfg = self.config.get("matrix", {}) or {}
        maji = {str(mcfg.get("as_person") or "").lower()} | {
            str((u or {}).get("as_person") or "").lower() for u in (mcfg.get("users") or [])}
        if str(name or "").lower() not in maji:
            return
        mx = getattr(ch, "telegram", None)       # historický název: drží Matrix
        if mx is None or not getattr(mx, "enabled", False):
            return
        zprava = "🎙️ Slyšel jsem: „%s“\n\n%s" % ((heard or "").strip(), response.strip())

        def _run():
            try:
                ok = mx.send(zprava)
                log.info("[Voice] kopie na Matrix: %s", "odeslána" if ok else "NEODESLÁNA")
            except Exception as e:
                log.info(f"[Voice] kopie na Matrix selhala: {e!r}")
        threading.Thread(target=_run, daemon=True, name="voice-matrix-copy").start()

    def _kodi_listening_note(self):
        """HANS_WAKE_KODI_NOTE_V1 (28. 9., přání uživatele) — po rozpoznání
        probouzecího slova krátká hláška „Poslouchám…“ vpravo nahoře v Kodi,
        stejně jako upozornění na už viděný film (HANS_KODI_SEEN_BEFORE_V1:
        GUI.ShowNotification, Hansova tvář). Ve vlastním vlákně — Kodi nesmí
        zdržet nahrávání. Gate voice.kodi_wake_notify."""
        vcfg = self.config.get("voice", {}) or {}
        if not vcfg.get("kodi_wake_notify", True):
            return

        def _run():
            try:
                k = getattr(self, "_kodi_note_client", None)
                if k is None:
                    from scripts.kodi_client import KodiClient
                    k = self._kodi_note_client = KodiClient(self.config)
                kc = getattr(k, "_kcfg", {}) or {}
                ok = k.notify(getattr(k, "_persona", "Hans"),
                              str(vcfg.get("kodi_wake_text", "Poslouchám…")),
                              float(vcfg.get("kodi_wake_display_s", 6)),
                              image=kc.get("seen_before_image",
                                           "special://home/addons/service.hans.suggest"
                                           "/media/hans_face.png"))
                log.info("[Voice] Kodi hláška poslouchám: %s",
                         "ukázáno" if ok else "Kodi neodpověděl")
            except Exception as e:
                log.info(f"[Voice] Kodi hláška selhala: {e!r}")
        threading.Thread(target=_run, daemon=True, name="wake-kodi-note").start()

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
                    _spoke["any"] = True
                    tts.speak(s, priority=not _spoke.get("veta"))
                    _spoke["veta"] = True
            # VOICE_GAP_FILL_V1 — dokud Hans nezačne odpovídat, vyplň ticho
            # připravenou hláškou (1,2 / 7 / 15 s). Rychlá odpověď (čas, příkaz)
            # první výplň předběhne. Odpověď má přednost: její první věta
            # (priority) vyčistí frontu, takže nezačatá výplň už nezazní.
            hotovo = threading.Event()

            def _vypln():
                import random as _rnd
                t0 = time.time()
                for i, (po_s, hlasky) in enumerate(_VYPLNE):
                    if hotovo.wait(max(0.0, t0 + po_s - time.time())) or _spoke["any"]:
                        return
                    if tts and getattr(tts, "enabled", False):
                        tts.speak(_rnd.choice(hlasky), priority=(i == 0))
                        log.info("[Voice] výplň %d po %.1f s", i + 1, time.time() - t0)
            threading.Thread(target=_vypln, daemon=True, name="voice-vypln").start()
            # HANS_CHAT_CHANNEL_AWARE_V1 — hlasový vstup tag
            _t0_nab = time.time()   # HANS_OFFER_TO_PENDING_V1
            try:
                response = ch.send_chat_message(name, text,
                                                on_sentence=_on_sentence,
                                                channel="voice")
            finally:
                hotovo.set()
            # HANS_OFFER_TO_PENDING_V1 — nabídka akce → čekající návrh; když už
            # odpověď zazněla po větách, dořekni jen otázku / poctivou větu
            try:
                from scripts.hans_offer import zpracuj as _nab
                response, _dod = _nab(ch, name, text, response or "", _t0_nab)
                if _dod and _spoke["any"] and tts and getattr(tts, "enabled", False):
                    tts.speak(_dod)
            except Exception:
                pass
            if not response:
                return
            log.info(f"[Voice] ← {response[:80]}")
            self._matrix_copy(ch, name, text, response)   # HANS_VOICE_MATRIX_COPY_V1
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
