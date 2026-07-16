#!/usr/bin/env python3
"""HANS_GUARD_V1 — hlídací režim („/hlidej“), když nikdo není doma.

Hans začne místnost střežit jako kamera: při POHYBU nebo NÁHLÉ ZMĚNĚ SVĚTLA
(rozsvícení, ne postupné rozednívání) pošle snímek na Telegram.

Návrh (a proč tak):
- **Stav v SOUBORU** (`data/.hans_guard`), ne v paměti → přežije restart Hanse
  i watchdog. Když jsi pryč týden, nesmí hlídání zmizet po jednom restartu.
- **Běží na framech, ne na rozpoznávání.** Framy tečou v main loopu vždy
  (i ve spánku — viz SLEEP_VISION_OFF_V1, kde se gatuje jen recognition), takže
  hlídání funguje i v noci. Detekce je čistý OpenCV rozdíl snímků, žádné LLM.
- **Rozednívání netriggeruje:** vzorkujeme ~1×/s a porovnáváme JAS proti
  KRÁTKODOBÉ základně (EMA). Rozsvícení = skok o desítky jednotek během
  vteřiny; svítání = pár jednotek za minuty → EMA se plynule doveze a nikdy
  nepřekročí práh.
- **Kamera musí koukat do místnosti**, ne do stropu (spánek ji otáčí nahoru) —
  o to se stará `hans_routine` (při zapnutém hlídání servo nezvedá).

Config `guard.{motion_pixels_pct, motion_delta, light_jump, sample_every_s,
cooldown_s, warmup_s, max_per_day, snapshot_dir, telegram}`.
"""
from __future__ import annotations

import json
import queue
import threading
import logging
import os
import time
from datetime import datetime
from typing import Optional

_log = logging.getLogger("hans_guard")

_STATE_FILE = "data/.hans_guard"
_SNAP_DIR = "data/guard"


def _cfg(config: dict) -> dict:
    return (config or {}).get("guard", {}) or {}


# ── stav (soubor → přežije restart) ─────────────────────────────────────────

def state() -> dict:
    try:
        with open(_STATE_FILE, encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def armed() -> bool:
    return bool(state().get("armed"))


def _save(st: dict) -> None:
    try:
        os.makedirs(os.path.dirname(_STATE_FILE) or ".", exist_ok=True)
        with open(_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f)
    except Exception as e:
        _log.warning("guard: zápis stavu selhal: %s", e)


def arm(by: str = "") -> dict:
    st = {"armed": True, "since": time.time(), "by": by or "",
          "sent_today": 0, "day": datetime.now().strftime("%Y-%m-%d")}
    _save(st)
    _log.info("guard: ZAPNUTO hlídání (%s)", by or "?")
    return st


def disarm() -> dict:
    st = state()
    st["armed"] = False
    st["stopped"] = time.time()
    _save(st)
    _log.info("guard: vypnuto hlídání")
    return st


# ── detektor (volá se z main loopu na každém N-tém framu) ────────────────────

class _Recorder(threading.Thread):
    """HANS_GUARD_RECORD_V1 — zápis videa ve VLASTNÍM threadu.

    Main loop drží kameru, UI i recovery → nesmí se zdržet enkódováním. Framy sem
    tečou přes frontu; když se writer nestíhá, framy se ZAHAZUJÍ (radši kratší
    video než zatuhlý Hans).
    """

    def __init__(self, path, fps, seconds, size, on_done=None):
        super().__init__(daemon=True)
        self.path, self.fps, self.seconds, self.size = path, fps, seconds, size
        self.on_done = on_done
        self.q = queue.Queue(maxsize=90)
        self._stop = threading.Event()
        self.started_at = time.time()

    def push(self, frame):
        try:
            self.q.put_nowait(frame)
        except Exception:
            pass          # fronta plná → zahoď (nikdy neblokuj main loop)

    def active(self) -> bool:
        return not self._stop.is_set() and (time.time() - self.started_at) < self.seconds

    def run(self):
        w = None
        try:
            import cv2
            fourcc = cv2.VideoWriter_fourcc(*"mp4v")
            w = cv2.VideoWriter(self.path, fourcc, self.fps, self.size)
            if not w.isOpened():
                _log.warning("guard: VideoWriter se neotevřel (%s)", self.path)
                return
            end = self.started_at + self.seconds
            n = 0
            # HANS_GUARD_RECORD_REALTIME_V1 — zapisuj jen cílovým tempem (fps).
            # Main loop dodává framy rychleji (~20/s); kdybychom psali všechny,
            # video se hlavičkou 10 fps přehraje jako ZPOMALENÝ film (5 s děje = 10 s záznamu).
            spf = 1.0 / max(1.0, float(self.fps))
            next_write = self.started_at
            while time.time() < end and not self._stop.is_set():
                try:
                    f = self.q.get(timeout=0.5)
                except Exception:
                    continue
                now = time.time()
                if now < next_write:
                    continue                      # frame navíc → zahoď (drž reálný čas)
                next_write = max(now, next_write + spf)
                try:
                    w.write(cv2.resize(f, self.size))
                    n += 1
                except Exception:
                    pass
            _log.info("guard: záznam hotov (%s, %d snímků)", self.path, n)
        except Exception as e:
            _log.warning("guard recorder: %s", e)
        finally:
            self._stop.set()
            try:
                if w is not None:
                    w.release()
            except Exception:
                pass
            if self.on_done and os.path.exists(self.path):
                try:
                    self.on_done(self.path)
                except Exception as e:
                    _log.warning("guard recorder on_done: %s", e)


class GuardWatch:
    """Držák stavu detekce mezi framy. Levné: šedý downscale + rozdíl."""

    def __init__(self, config: dict, notifier=None, video_notifier=None):
        self.config = config or {}
        self._notifier = notifier      # callable(path, caption) → posílá ven
        # HANS_GUARD_RECORD_V1_TICK — záznam videa po poplachu (0 = vypnuto).
        self._video_notifier = video_notifier
        self._rec = None
        self._prev = None              # předchozí malý šedý frame
        self._light_ema = None         # krátkodobá základna jasu
        self._last_sample = 0.0
        self._last_alert = 0.0
        self._armed_seen = False       # náběh po zapnutí (warmup)
        self._armed_at = 0.0

    # -- pomocné ------------------------------------------------------------
    def _c(self, key, default):
        return _cfg(self.config).get(key, default)

    def _small_gray(self, frame):
        import cv2
        g = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        return cv2.resize(g, (160, 120))

    def _reset(self):
        self._prev = None
        self._light_ema = None

    # -- hlavní vstup -------------------------------------------------------
    def tick(self, frame) -> Optional[str]:
        """Zavolej s aktuálním framem. Vrátí důvod poplachu ('pohyb' /
        'světlo') když se poplach ODESLAL, jinak None. Nikdy nevyhazuje."""
        try:
            if frame is None or not armed():
                if self._armed_seen:
                    self._armed_seen = False
                    self._reset()
                return None

            now = time.time()
            if not self._armed_seen:       # čerstvě zapnuto → náběh
                self._armed_seen = True
                self._armed_at = now
                self._reset()

            # HANS_GUARD_RECORD_FPS_FIX_V1 — běžící záznam krmíme KAŽDÝM framem,
            # tedy PŘED vzorkovacím gate. (Za ním by dostal 1 frame/s → video by
            # mělo pár snímků místo minuty. Detekce vzorkuje 1×/s, záznam ne.)
            if self._rec is not None:
                if self._rec.active():
                    self._rec.push(frame)
                else:
                    self._rec = None

            if now - self._last_sample < float(self._c("sample_every_s", 1.0)):
                return None
            self._last_sample = now

            import cv2
            import numpy as np
            small = self._small_gray(frame)
            mean = float(small.mean())

            prev, ema = self._prev, self._light_ema
            self._prev = small
            # EMA jasu = krátkodobá základna. Pomalá změna (svítání) ji jen
            # doveze; skok (rozsvícení) se od ní utrhne.
            self._light_ema = mean if ema is None else (0.9 * ema + 0.1 * mean)

            if prev is None or ema is None:
                return None
            # warmup — po zapnutí chvíli nic (ať tě nevyfotí, jak odcházíš)
            if now - self._armed_at < float(self._c("warmup_s", 20)):
                return None
            if now - self._last_alert < float(self._c("cooldown_s", 60)):
                return None

            reason = None
            # (a) NÁHLÁ ZMĚNA SVĚTLA — skok proti základně.
            # HANS_GUARD_LIGHT_DARK_ONLY_V1 — jen když je TMA (ema = základna PŘED
            # změnou). Ve dne dělá slunce zpoza mraku falešné poplachy a rozsvícení
            # nic neznamená; v noci je naopak rozsvícení nejsilnější signál, že je
            # někdo v domě. Podle skutečné tmy, NE podle hodin (pevné noční okno
            # nesedí přes rok: v červenci šero ve 22:00, v prosinci tma od 16:00).
            _light_on = (not bool(self._c("light_only_in_dark", True))
                         or ema < float(self._c("dark_below", 50.0)))
            if _light_on and abs(mean - ema) >= float(self._c("light_jump", 18.0)):
                reason = "náhlá změna světla"
            else:
                # (b) POHYB — podíl pixelů, co se výrazně změnily.
                # HANS_GUARD_MOTION_LIGHT_INVARIANT_V1 — porovnáváme až PO odečtení
                # průměrného jasu každého snímku. Bez toho plošná změna osvětlení
                # (slunce zpoza mraku, rozsvícení) posune VŠECHNY pixely naráz a
                # tváří se jako pohyb celého obrazu → falešný poplach. Po odečtení
                # průměru globální posun zmizí a zůstane jen LOKÁLNÍ změna = postava.
                _s = small.astype(np.int16) - int(small.mean())
                _p = prev.astype(np.int16) - int(prev.mean())
                diff = np.abs(_s - _p)
                changed = float(np.count_nonzero(
                    diff > int(self._c("motion_delta", 25)))) / diff.size
                if changed >= float(self._c("motion_pixels_pct", 0.02)):
                    reason = "pohyb"

            if not reason:
                return None
            if not self._quota_ok():
                return None
            self._last_alert = now
            self._alert(frame, reason)
            self._start_record(frame, reason)
            return reason
        except Exception as e:
            _log.warning("guard tick: %s", e)
            return None

    # -- záznam videa -------------------------------------------------------
    def _start_record(self, frame, reason: str) -> None:
        """HANS_GUARD_RECORD_V1_TICK — po poplachu natoč video (vlastní thread).

        Lokální archiv + odeslání na Telegram. ODKAZ by z dovolené nefungoval
        (Pi je v LAN) → posílá se rovnou soubor. `record_s: 0` = vypnuto.
        """
        try:
            secs = float(self._c("record_s", 0))
            if secs <= 0 or self._rec is not None:
                return
            os.makedirs(_SNAP_DIR, exist_ok=True)
            self._rotate_recordings()
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            path = os.path.join(_SNAP_DIR, f"rec_{ts}.mp4")
            h, w = frame.shape[:2]
            out_w = int(self._c("record_width", 640))
            out_h = max(2, int(round(out_w * h / float(w))) // 2 * 2)
            fps = float(self._c("record_fps", 10))

            def _done(p):
                if self._video_notifier:
                    self._video_notifier(p, f"🎥 Záznam po poplachu ({reason})")

            self._rec = _Recorder(path, fps, secs, (out_w, out_h), on_done=_done)
            self._rec.start()
            _log.info("guard: nahrávám %d s → %s", int(secs), path)
        except Exception as e:
            _log.warning("guard start record: %s", e)
            self._rec = None

    def _rotate_recordings(self) -> None:
        """Ať data/guard/ neroste donekonečna."""
        try:
            keep = int(self._c("record_keep", 20))
            recs = sorted((os.path.join(_SNAP_DIR, f) for f in os.listdir(_SNAP_DIR)
                           if f.startswith("rec_") and f.endswith(".mp4")),
                          key=os.path.getmtime, reverse=True)
            for old in recs[max(0, keep - 1):]:
                try:
                    os.remove(old)
                except Exception:
                    pass
        except Exception:
            pass

    # -- poplach ------------------------------------------------------------
    def _quota_ok(self) -> bool:
        st = state()
        today = datetime.now().strftime("%Y-%m-%d")
        if st.get("day") != today:
            st["day"], st["sent_today"] = today, 0
        if int(st.get("sent_today", 0)) >= int(self._c("max_per_day", 60)):
            return False
        st["sent_today"] = int(st.get("sent_today", 0)) + 1
        _save(st)
        return True

    def _alert(self, frame, reason: str) -> None:
        import cv2
        os.makedirs(_SNAP_DIR, exist_ok=True)
        ts = datetime.now()
        path = os.path.join(_SNAP_DIR, ts.strftime("guard_%Y%m%d_%H%M%S.jpg"))
        try:
            cv2.imwrite(path, frame)
        except Exception as e:
            _log.warning("guard: snímek se neuložil: %s", e)
            return
        caption = "🔔 Hlídání: %s — %s" % (reason, ts.strftime("%d.%m. %H:%M:%S"))
        _log.info("guard: POPLACH (%s) → %s", reason, path)
        try:
            if self._notifier:
                self._notifier(path, caption)
        except Exception as e:
            _log.warning("guard: odeslání selhalo: %s", e)
        self._diary(reason, path)

    def _diary(self, reason: str, path: str) -> None:
        try:
            import sqlite3
            db = (self.config.get("diary_db")
                  or (self.config.get("hans_idle", {}) or {}).get("diary_db")
                  or "data/hans_diary.db")
            c = sqlite3.connect(db, timeout=5.0)
            c.execute("INSERT INTO diary (ts, event_type, title, note) "
                      "VALUES (?,?,?,?)",
                      (time.time(), "guard_alert", "Hlídání: %s" % reason,
                       "Zaznamenal jsem %s v prázdném domě a poslal snímek "
                       "(%s)." % (reason, os.path.basename(path))))
            c.commit()
            c.close()
        except Exception as e:
            _log.debug("guard diary: %s", e)


def status_text() -> str:
    """Lidsky čitelný stav pro chat/Telegram."""
    st = state()
    if not st.get("armed"):
        return ("Hlídání je vypnuté. Zapnu ho příkazem /hlidej — pak při "
                "pohybu nebo náhlé změně světla pošlu snímek na Telegram.")
    since = st.get("since") or 0
    h = (time.time() - since) / 3600.0
    return ("Hlídám. Zapnuto před %s, dnes odesláno %d snímků. "
            "Vypnout: /hlidej stop."
            % (("%.0f h" % h) if h >= 1 else ("%.0f min" % (h * 60)),
               int(st.get("sent_today", 0))))
