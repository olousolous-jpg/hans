"""
TTS Speaker Module
Uses edge-tts to generate MP3 + mpg123 to play through Robot HAT speaker.
robot_hat.enable_speaker() activates the GPIO-controlled amplifier.

Config keys (under "tts"):
    enabled        : bool   — master switch
    voice          : str    — edge-tts voice (default "cs-CZ-AntoninNeural")
    volume         : int    — 0-100 (default 80)
    max_length     : int    — max characters sent to edge-tts (default 200)
    cache_enabled  : bool   — cache MP3 files to avoid re-generating (default true)
    alsa_device    : str    — ALSA device for mpg123 (default "plughw:2,0")
    alsa_control   : str    — ALSA mixer control name (default "robot-hat speaker")
"""

import os
import subprocess
import threading
import hashlib
import queue
import time
import shutil
import asyncio
from pathlib import Path


# ── Optional robot_hat integration ───────────────────────────────────────────
try:
    from robot_hat import enable_speaker, set_volume as _rh_set_volume
    _ROBOT_HAT = True
except ImportError:
    _ROBOT_HAT = False
    print("[TTS] robot_hat not available — speaker enable skipped")

try:
    import edge_tts
    _EDGE_TTS = True
except ImportError:
    _EDGE_TTS = False
    print("[TTS] edge-tts not available — install with: pip install edge-tts")


# # p3_tts_cleaned
class TTSSpeaker:
    """
    Text-to-speech via edge-tts + mpg123.
    Speak requests are queued and processed in a background thread.
    """

    def __init__(self, config: dict):
        self.config     = config
        self._tts_cfg   = config.get('tts', {})

        self.enabled       = self._tts_cfg.get('enabled', False)
        self._voice        = self._tts_cfg.get('voice', 'cs-CZ-AntoninNeural')
        self._volume       = int(self._tts_cfg.get('volume', 80))
        self._max_len      = int(self._tts_cfg.get('max_length', 200))
        self._cache_on     = self._tts_cfg.get('cache_enabled', True)
        self._alsa_device  = self._tts_cfg.get('alsa_device', 'plughw:2,0')
        self._alsa_control = self._tts_cfg.get('alsa_control', 'robot-hat speaker')

        self._cache_dir = Path('data/tts_cache')
        self._queue     = queue.Queue()
        self._speaking  = False
        self._current_pitch = None   # AVATAR_TALK_SPEAKER_V1 — pitch právě hrané věty (Koláč=+40Hz)
        self._thread    = None

        if not self.enabled:
            print("[TTS] Disabled in config")
            return

        if not _EDGE_TTS:
            print("[TTS] edge-tts missing — disabling")
            self.enabled = False
            return

        if not self._check_mpg123():
            print("[TTS] mpg123 not found — disabling")
            self.enabled = False
            return

        # Enable Robot HAT amplifier
        if _ROBOT_HAT:
            enable_speaker()

        # Set ALSA volume using correct control name
        self._set_alsa_volume(self._volume)

        # Cache directory
        if self._cache_on:
            self._cache_dir.mkdir(parents=True, exist_ok=True)
            self._evict_cache()

        # Start background worker
        self._thread = threading.Thread(target=self._worker, daemon=True)
        self._thread.start()

        print(f"[TTS] Ready — voice={self._voice}  vol={self._volume}  "
              f"device={self._alsa_device}  "
              f"control='{self._alsa_control}'  "
              f"cache={'on' if self._cache_on else 'off'}")

    # ── Public API ────────────────────────────────────────────────────────────

    def speak(self, text: str, priority: bool = False,
              voice: str | None = None, pitch: str | None = None):
        # TTS_VOICE_PITCH_V1 — volitelné per-call voice/pitch (Hans+Kolač dialog)
        if not self.enabled:
            return
        text = self._clean(text)
        if not text:
            return
        if priority:
            self._clear_queue()
        self._queue.put((text, voice, pitch))
    def is_speaking(self) -> bool:
        return self._speaking

    def set_volume(self, volume: int):
        """Set volume 0-100."""
        self._volume = max(0, min(100, volume))
        self._set_alsa_volume(self._volume)

    def _set_alsa_volume(self, volume: int):
        """Set ALSA mixer volume using the correct control name."""
        pct = max(0, min(100, volume))
        try:
            result = subprocess.run(
                ['amixer', '-c', self._alsa_device.split(':')[1].split(',')[0],
                 'sset', self._alsa_control, f'{pct}%'],
                capture_output=True, text=True
            )
            if result.returncode == 0:
                print(f"[TTS] ALSA '{self._alsa_control}' → {pct}%")
            else:
                print(f"[TTS] ALSA set failed: {result.stderr.strip()}")
        except Exception as e:
            print(f"[TTS] ALSA volume error: {e}")
    def stop(self):
        self._clear_queue()
        try:
            subprocess.run(['pkill', '-f', 'mpg123'], capture_output=True)
        except Exception:
            pass

    def cleanup(self):
        self.enabled = False
        self._clear_queue()
        try:
            subprocess.run(['pkill', '-f', 'mpg123'], capture_output=True)
        except Exception:
            pass
        print("[TTS] Stopped")

    # ── Internal ──────────────────────────────────────────────────────────────

    def _worker(self):
        while True:
            try:
                item = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue
            if not self.enabled:
                continue
            # TTS_VOICE_PITCH_V1 — rozbal tuple, plain string = zpětná kompat
            if isinstance(item, tuple) and len(item) == 3:
                text, voice, pitch = item
            else:
                text, voice, pitch = item, None, None
            while self._speaking:
                time.sleep(0.05)
            self._current_pitch = pitch   # AVATAR_TALK_SPEAKER_V1 — kdo mluví (Koláč=+40Hz)
            self._speaking = True
            try:
                mp3 = self._get_mp3(text, voice, pitch)
                if mp3 and mp3.exists():
                    self._play(mp3)
            except Exception as e:
                print(f"[TTS] Playback error: {e}")
            finally:
                self._speaking = False

    def _get_mp3(self, text: str, voice: str | None = None,
                 pitch: str | None = None) -> Path | None:
        # TTS_VOICE_PITCH_V1 — cache key zahrne voice+pitch
        eff_voice = voice or self._voice
        eff_pitch = pitch or '+0Hz'
        if self._cache_on:
            key  = hashlib.md5(
                f"{eff_voice}:{eff_pitch}:{text}".encode()).hexdigest()
            path = self._cache_dir / f"{key}.mp3"
            if path.exists():
                return path
        else:
            path = Path('/tmp/tts_speak.mp3')

        try:
            async def _generate():
                communicate = edge_tts.Communicate(
                    text, eff_voice, pitch=eff_pitch)
                await communicate.save(str(path))
            asyncio.run(_generate())
            self._evict_cache()
            return path
        except Exception as e:
            print(f"[TTS] edge-tts error: {e}")
            return None

    def _play(self, mp3_path: Path):
        """Play MP3 via mpg123. Use --scale for software volume boost."""
        try:
            # --scale: 32768 = normal, higher = louder (max ~65536 before clipping)
            # Map volume 0-100 → scale 0-65536
            scale = int((self._volume / 100.0) * 65536)
            scale = max(1, min(65536, scale))

            result = subprocess.run(
                ['mpg123', '-a', self._alsa_device,
                 '--scale', str(scale), str(mp3_path)],
                capture_output=True, timeout=30,
            )
            if result.returncode != 0:
                # Fallback without --scale
                subprocess.run(
                    ['mpg123', '-a', self._alsa_device, str(mp3_path)],
                    capture_output=True, timeout=30,
                )
        except subprocess.TimeoutExpired:
            print("[TTS] mpg123 timed out")
        except Exception as e:
            print(f"[TTS] mpg123 error: {e}")

    def _clean(self, text: str) -> str:
        import re
        # URL pryč (původní)
        text = re.sub(r'http\S+', '', text)

        # G0_TTS_SANITIZE_V1 — markdown/speciální znaky pryč.
        # Model občas vrací '*název*' nebo '**tučně**' → TTS čte "hvězdička".
        # Odstraníme markdown markery, ponecháme obsah.
        text = re.sub(r'\*{1,3}([^*]+)\*{1,3}', r'\1', text)   # *x* **x** ***x***
        text = re.sub(r'_{1,3}([^_]+)_{1,3}', r'\1', text)        # _x_ __x__
        text = re.sub(r'`{1,3}([^`]+)`{1,3}', r'\1', text)        # `x` ```x```
        text = re.sub(r'~~([^~]+)~~', r'\1', text)                # ~~x~~
        # Osamělé markery které zbyly (nepárové) — smaž
        text = re.sub(r'[*_`~#>|]', '', text)
        # Markdown nadpisy/odrážky na začátku řádku
        text = re.sub(r'^\s*[-•]\s+', '', text, flags=re.MULTILINE)

        # G0_TTS_SANITIZE_V1 — nadbytečná oslovení (vokativ 2×+ → ponech první)
        text = self._dedupe_vocatives(text)

        # Normalizace mezer (původní) + úklid interpunkce po smazání
        text = re.sub(r'\s+([,.!?])', r'\1', text)   # mezera před interpunkcí
        text = re.sub(r',\s*,', ',', text)             # dvojitá čárka
        text = re.sub(r'\s+', ' ', text).strip()
        text = re.sub(r'^[,\s]+', '', text)            # čárka na začátku
        if len(text) > self._max_len:
            cut  = text[:self._max_len].rfind('.')
            text = text[:cut + 1] if cut > 0 else text[:self._max_len]
        return text

    def _vocatives(self) -> list:
        """G0 — vokativy známých jmen z configu (a→o pravidlo + override).

        Cache: spočítá se jednou. known_persons jména → vokativ:
          Standa→Stando, Jana→Jano (čeština: koncové -a → -o).
        Pro nepravidelná jména lze v configu greeting.vocatives přidat override.
        """
        if getattr(self, "_vocatives_cache", None) is not None:
            return self._vocatives_cache
        vocs = []
        try:
            kp = self.config.get("known_persons", {}) if hasattr(self, "config") else {}
            names = list(kp.keys()) if isinstance(kp, dict) else []
            # override z configu (volitelné)
            override = {}
            if hasattr(self, "config"):
                override = self.config.get("greeting", {}).get("vocatives", {}) or {}
            for n in names:
                if not n:
                    continue
                disp = n.strip()
                # display jméno s velkým písmenem
                disp_cap = disp[:1].upper() + disp[1:]
                # vokativ: override → pravidlo a→o → samo jméno
                if disp.lower() in {k.lower() for k in override}:
                    voc = next(v for k, v in override.items()
                               if k.lower() == disp.lower())
                elif disp_cap.endswith("a"):
                    voc = disp_cap[:-1] + "o"   # Standa→Stando, Jana→Jano
                else:
                    voc = disp_cap
                vocs.append(voc)
        except Exception:
            pass
        self._vocatives_cache = vocs
        return vocs

    def _dedupe_vocatives(self, text: str) -> str:
        """G0 — ponech první výskyt každého vokativu, další smaž i s čárkou."""
        import re
        vocs = self._vocatives()
        if not vocs:
            return text
        for voc in vocs:
            # Najdi všechny výskyty jako celé slovo (case-insensitive)
            pat = re.compile(r'\b' + re.escape(voc) + r'\b', re.IGNORECASE)
            matches = list(pat.finditer(text))
            if len(matches) <= 1:
                continue
            # Ponech první, smaž od druhého dál (zprava, ať se indexy neposouvají).
            # Smaž i přilehlou čárku+mezeru: ", Stando," → "," nebo ", Stando" → ""
            for m in reversed(matches[1:]):
                s, e = m.start(), m.end()
                # rozšíř o okolní ", " z obou stran
                left = s
                while left > 0 and text[left-1] in ' ,':
                    left -= 1
                right = e
                while right < len(text) and text[right] in ' ,':
                    right += 1
                # nahraď jedním oddělovačem podle kontextu
                before = text[:left]
                after = text[right:]
                # pokud uprostřed věty, vlož ", " jen pokud after nezačíná interpunkcí
                sep = ""
                if before and after and after[0] not in '.!?':
                    sep = ", " if before[-1] not in ' ,' else ""
                text = before + sep + after
        return text

    def _clear_queue(self):
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break

    def _evict_cache(self, keep: int = 200):
        """Delete oldest MP3 files from cache, keeping the newest `keep` files."""
        if not self._cache_on or not self._cache_dir.exists():
            return
        files = sorted(self._cache_dir.glob("*.mp3"), key=lambda f: f.stat().st_mtime)
        excess = len(files) - keep
        if excess <= 0:
            return
        for f in files[:excess]:
            try:
                f.unlink()
            except Exception:
                pass
        print(f"[TTS] Cache eviction: removed {excess} old file(s), kept {keep}")

    def _clear_cache(self):
        if self._cache_dir.exists():
            for f in self._cache_dir.glob("*.mp3"):
                try:
                    f.unlink()
                except Exception:
                    pass

    @staticmethod
    def _check_mpg123() -> bool:
        return shutil.which('mpg123') is not None
