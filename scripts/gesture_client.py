"""
Gesture Client (Hailo mode)
Sends HAND_MAGIC frames to hailo_inference_server via the existing
/tmp/hailo_scrfd.sock — no separate gesture server needed.

Gesture IDs:
  0 = none  1 = fist  2 = thumbs_up
"""

import socket
from collections import deque, Counter
import struct
import threading
import time
import numpy as np
import logging

_log = logging.getLogger("gesture")

SOCK_PATH         = "/tmp/gesture.sock"       # dedicated gesture socket
HAND_MAGIC        = b'\xAA\xBB\xCC\xDD'
GESTURE_NONE      = 0
GESTURE_FIST      = 1
GESTURE_OPEN_HAND = 2
GESTURE_THUMBS_UP = 3
GESTURE_NAMES     = {GESTURE_NONE: None,
                     GESTURE_FIST:      "fist",
                     GESTURE_OPEN_HAND: "open_hand",
                     GESTURE_THUMBS_UP: "thumbs_up"}


class GestureClient:

    def __init__(self, config: dict, on_gesture=None):
        self.config      = config
        self.on_gesture  = on_gesture
        cfg              = config.get("gesture", {})

        self.enabled      = bool(cfg.get("enabled", True))
        self._hold_frames  = int(cfg.get("hold_frames", 8))
        self._hold_seconds = float(cfg.get("hold_seconds", 0.5))
        self._cooldown_s  = float(cfg.get("cooldown_s", 3.0))

        self._sock        = None
        self._lock        = threading.Lock()
        self._connected   = False
        self._busy        = False
        self._pending     = None
        self._fail_cnt    = 0

        self._current          = GESTURE_NONE
        self._current_since    = 0.0
        self._hold_count       = 0
        self._last_fired       : dict = {}
        self._last_fired_name  = None
        self.last_gesture      = None   # last fired gesture name
        self.last_gesture_time = 0.0
        self._vote_buf  = deque(maxlen=10)
        self._streak     = 0   # počet po sobě jdoucích stejných gest
        self._streak_gest = GESTURE_NONE
        self._warmup_frames = 15  # ignoruj první frames po startu
        self.last_landmarks = None   # posledních 21 bodů pro vykreslení
        # Per-gesture hold times (override hold_seconds from config)
        self._hold_per_gesture = {
            GESTURE_OPEN_HAND: float(cfg.get("open_hand_hold_s", 0.0)),
            GESTURE_THUMBS_UP: float(cfg.get("thumbs_up_hold_s", 1.5)),
            GESTURE_FIST:      float(cfg.get("hold_seconds", 0.0)),
        }

        # HANS_GESTURE_WAVE_V1 (6.9.) — zamavani otevrenou dlani = pozdrav.
        # Klasifikator umi jen STATICKOU otevrenou dlan; mavani je pohyb,
        # takze se sklada az tady z casove stopy jejich poloh.
        self._wave_only     = bool(cfg.get("wave_only", True))
        self._wave_window_s = float(cfg.get("wave_window_s", 1.5))
        self._wave_min_amp  = float(cfg.get("wave_min_amplitude", 0.02))
        # 0.02 = jen pojistka proti sumu; o mavnuti rozhoduje pomer
        # k sirce dlane nize (drive tu bylo 0.06 = skryty prah na vzdalenost).
        self._wave_min_rev  = int(cfg.get("wave_min_reversals", 1))
        self._wave_eps      = float(cfg.get("wave_eps", 0.01))
        self._wave_cooldown = float(cfg.get("wave_cooldown_s", 5.0))
        # Kolik poloh dlane musi v okne byt. Zmereno 6.9.: klasifikator
        # vraci open_hand nepravidelne (1x/s az 30x/s podle drzeni ruky),
        # takze tohle je hlavni paka, kdyz mavani "nechyta".
        self._wave_min_samples = int(cfg.get("wave_min_samples", 3))
        # Nasobek SIRKY DLANE — meritko nezavisle na vzdalenosti.
        self._wave_min_amp_rel = float(cfg.get("wave_min_amplitude_rel", 0.6))
        # HANS_GESTURE_ROI_V1
        self._roi_on     = bool(cfg.get("roi_enabled", True))
        self._roi_w_mult = float(cfg.get("roi_width_faces", 5.0))
        self._roi_min_w  = float(cfg.get("roi_min_width", 0.30))
        self._roi_up     = float(cfg.get("roi_up_faces", 1.0))
        self._roi_down   = float(cfg.get("roi_down_faces", 3.0))
        self._roi_skip_above = float(cfg.get("roi_skip_above", 0.85))
        # HANS_GESTURE_WAVE_EDGE_V1
        self._wave_armed     = True
        self._wave_last_seen = 0.0
        self._wave_rearm_s   = float(cfg.get("wave_rearm_s", 4.0))
        # HANS_GESTURE_WAVE_METRIKY_V1 — 0 = kriterium vypnute (jen se meri)
        self._wave_min_palm_face = float(cfg.get("wave_min_palm_vs_face", 0.0))
        self._wave_min_obr_s     = float(cfg.get("wave_min_reversals_per_s", 0.0))
        self._wave_max_od_tvare  = float(cfg.get("wave_max_face_widths", 0.0))
        self._wave_min_palm_w    = float(cfg.get("wave_min_palm_width", 0.0))
        self._sirka_tvare        = 0.0
        self._stred_tvare        = None
        self._wave_track    = deque(maxlen=60)   # (t, cx) normalizovane
        self._wave_last_fired = 0.0
        # HANS_GESTURE_DEBUG_V1 — `gesture.debug` zvedne diagnostiku na INFO.
        # Bez toho je cela cesta nema: chyby socketu jsou na DEBUG a vyjimka
        # v _loop se spolkne, takze mlceni klienta nejde odlisit od "nikdo
        # nemava". Slouzi i k ladeni prahu mavani.
        self._debug        = bool(cfg.get("debug", False))
        self._dbg_submits  = 0
        self._dbg_open     = 0
        self._dbg_frames   = 0
        self._dbg_last     = 0.0

        if not self.enabled:
            _log.info("GestureClient disabled")
            return

        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        _log.info("GestureClient started — using dedicated gesture socket %s", SOCK_PATH)

    def submit(self, frame: np.ndarray, oblast=None):
        """`oblast` = (x1,y1,x2,y2) normalizovane okoli osoby (bbox tvare).

        HANS_GESTURE_ROI_V1 (6.9.) — POSILAT VYREZ, NE CELY ZABER.
        Zmereno 6.9.: detektor dlane skaluje vstup vzdy na 192x192, takze
        rozhoduje POMER dlane k zaberu — a ten se zmensenim snimku NEMENI.
        Dlan ze 2 m mela 9,6 px at se poslalo 640x360 nebo 1280x720, proto
        zvetsovat rozliseni nema smysl. Vyrez ten pomer meni: pri treti
        sirky zaberu ma tataz dlan ~29 px.
        """
        if not self.enabled or self._busy:
            return
        import cv2 as _cv2
        vyrez = None
        if oblast is not None:
            try:
                # sirka TVARE je prirozene meritko: realna dlan je zhruba
                # velikosti obliceje, kdezto falesna detekce na textilii
                # byva mnohem mensi.
                self._sirka_tvare = max(float(oblast[2]) - float(oblast[0]), 1e-6)
                self._stred_tvare = ((float(oblast[0]) + float(oblast[2])) / 2.0,
                                     (float(oblast[1]) + float(oblast[3])) / 2.0)
            except Exception:
                pass
        if oblast is not None and self._roi_on:
            vyrez = self._spocti_vyrez(oblast)
        if vyrez is not None:
            h0, w0 = frame.shape[:2]
            _x1 = max(0, int(vyrez[0] * w0)); _y1 = max(0, int(vyrez[1] * h0))
            _x2 = min(w0, int(vyrez[2] * w0)); _y2 = min(h0, int(vyrez[3] * h0))
            if _x2 - _x1 >= 64 and _y2 - _y1 >= 64:
                frame = frame[_y1:_y2, _x1:_x2]
            else:
                vyrez = None
        # Zmenši na 640x360
        h, w = frame.shape[:2]
        if w > 640:
            frame = _cv2.resize(frame, (640, 360), interpolation=_cv2.INTER_LINEAR)
        # CLAHE — zvýší kontrast, pomůže palm detection
        lab = _cv2.cvtColor(frame, _cv2.COLOR_RGB2LAB)
        l, a, b = _cv2.split(lab)
        clahe = _cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        l = clahe.apply(l)
        lab = _cv2.merge([l, a, b])
        frame = _cv2.cvtColor(lab, _cv2.COLOR_LAB2RGB)
        with self._lock:
            # vyrez putuje S framem — bez nej by se vracene souradnice
            # nedaly prevest zpet do globalniho ramce
            self._pending = (frame.copy(), vyrez)
        self._dbg_submits += 1

    def connect(self) -> bool:
        # Počkej až do 3s na socket pokud ještě neexistuje
        import os, time as _time
        for _ in range(10):
            if os.path.exists(SOCK_PATH):
                break
            _time.sleep(0.3)
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(3.0)
            s.connect(SOCK_PATH)
            s.settimeout(5.0)
            self._sock      = s
            self._connected = True
            self._fail_cnt  = 0
            _log.info("GestureClient connected to gesture socket")
            print("[Gesture] Connected to gesture socket", flush=True)
            return True
        except Exception as e:
            if self._fail_cnt % 20 == 0:
                _log.warning("Cannot connect to gesture socket: %s", e)
                print(f"[Gesture] Connect failed: {e}", flush=True)
            self._fail_cnt += 1
            return False

    def is_connected(self) -> bool:
        return self._connected

    def reload_config(self, config: dict):
        cfg = config.get("gesture", {})
        self._hold_frames = int(cfg.get("hold_frames",  self._hold_frames))
        self._cooldown_s  = float(cfg.get("cooldown_s", self._cooldown_s))
        self.enabled      = bool(cfg.get("enabled",     self.enabled))
        # HANS_GESTURE_WAVE_V1 — prahy mavani za tepla, ladi se za behu
        # bez restartu (web admin zapise config -> _on_settings_save).
        self._wave_only     = bool(cfg.get("wave_only",      self._wave_only))
        self._wave_window_s = float(cfg.get("wave_window_s", self._wave_window_s))
        self._wave_min_amp  = float(cfg.get("wave_min_amplitude", self._wave_min_amp))
        self._wave_min_rev  = int(cfg.get("wave_min_reversals",   self._wave_min_rev))
        self._wave_eps      = float(cfg.get("wave_eps",      self._wave_eps))
        self._wave_cooldown = float(cfg.get("wave_cooldown_s", self._wave_cooldown))
        self._wave_min_samples = int(cfg.get("wave_min_samples", self._wave_min_samples))
        self._wave_min_amp_rel = float(cfg.get("wave_min_amplitude_rel", self._wave_min_amp_rel))
        self._wave_min_palm_face = float(cfg.get("wave_min_palm_vs_face", self._wave_min_palm_face))
        self._wave_min_obr_s     = float(cfg.get("wave_min_reversals_per_s", self._wave_min_obr_s))
        self._wave_max_od_tvare  = float(cfg.get("wave_max_face_widths", self._wave_max_od_tvare))
        self._wave_min_palm_w    = float(cfg.get("wave_min_palm_width", self._wave_min_palm_w))

    def _recv_exact(self, sock, n):
        buf = b""
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                return None
            buf += chunk
        return buf

    def _loop(self):
        while True:
            with self._lock:
                _job = self._pending
                self._pending = None
            if _job is None:
                time.sleep(0.02)
                continue
            frame, _vyrez = _job
            self._busy = True
            try:
                result = self._send_frame(frame)
                if result is None:
                    if self._debug:
                        _log.info("gesto[dbg]: server nevratil nic "
                                  "(spojeni=%s)", self._connected)
                else:
                    gesture_id, bbox = result[0], result[1]
                    lm = result[2] if len(result) > 2 else None
                    # HANS_GESTURE_ROI_V1 — zpet do souradnic CELEHO zaberu.
                    # Bez toho by posun vyrezu (clovek se pohne) sam vyrobil
                    # zdanlivy pohyb dlane = falesne mavani.
                    if bbox and _vyrez:
                        _vx1, _vy1, _vx2, _vy2 = _vyrez
                        _sw, _sh = (_vx2 - _vx1), (_vy2 - _vy1)
                        bbox = (_vx1 + bbox[0] * _sw, _vy1 + bbox[1] * _sh,
                                _vx1 + bbox[2] * _sw, _vy1 + bbox[3] * _sh)
                    self._update_state(gesture_id, bbox, lm)
            except Exception as _e:
                # HANS_GESTURE_DEBUG_V1 — drive `pass`: chyba ve zpracovani
                # framu mizela beze stopy.
                _log.warning("gesto: zpracovani framu selhalo: %s", _e)
            finally:
                self._busy = False

    def _send_frame(self, frame: np.ndarray):
        for _ in range(2):
            if not self._connected:
                if not self.connect():
                    return None
            try:
                h, w  = frame.shape[:2]
                data  = frame.tobytes()
                self._sock.sendall(struct.pack(">I", len(data)))
                self._sock.sendall(data)
                self._sock.sendall(struct.pack(">HH", w, h))
                resp = self._sock.recv(1)
                if not resp:
                    raise ConnectionError("no response")
                gesture_id = struct.unpack("B", resp)[0]
                # Receive palm bbox
                bbox_raw = self._recv_exact(self._sock, 16)
                if bbox_raw and len(bbox_raw) == 16:
                    bbox = struct.unpack('>ffff', bbox_raw)
                    if all(v == 0.0 for v in bbox):
                        bbox = None
                else:
                    bbox = None
                # Receive landmarks (63 floats = 252 bytes)
                lm_raw = self._recv_exact(self._sock, 252)
                if lm_raw and len(lm_raw) == 252:
                    lm = struct.unpack('>63f', lm_raw)
                    if all(v == 0.0 for v in lm):
                        lm = None
                else:
                    lm = None
                return gesture_id, bbox, lm, lm
            except Exception as e:
                _log.debug("Gesture send error: %s — reconnecting", e)
                try: self._sock.close()
                except Exception: pass
                self._sock      = None
                self._connected = False
        return None

    def _spocti_vyrez(self, tvar):
        """Okoli osoby kolem bboxu tvare. Mavajici ruka byva vedle hlavy
        nebo pod ni, proto je vyrez siroky a tahne se dolu. Vraci
        normalizovane (x1,y1,x2,y2), nebo None kdyz by vyrez nic neusetril
        (clovek je blizko — tam mavani funguje i bez vyrezu)."""
        try:
            fx1, fy1, fx2, fy2 = [float(v) for v in tvar[:4]]
        except Exception:
            return None
        fw, fh = fx2 - fx1, fy2 - fy1
        if fw <= 0 or fh <= 0:
            return None
        cx = (fx1 + fx2) / 2.0
        pw = max(fw * self._roi_w_mult, self._roi_min_w) / 2.0
        x1, x2 = cx - pw, cx + pw
        y1 = fy1 - fh * self._roi_up
        y2 = fy2 + fh * self._roi_down
        x1, y1 = max(0.0, x1), max(0.0, y1)
        x2, y2 = min(1.0, x2), min(1.0, y2)
        if (x2 - x1) >= self._roi_skip_above or (x2 - x1) <= 0.05:
            return None      # vyrez skoro cely zaber → nema smysl
        return (x1, y1, x2, y2)

    def _proc_ne(self, duvod: str):
        """Rekne, CO mavani chybelo. Bez toho se prahy ladi naslepo —
        'nesepnulo' muze znamenat tri ruzne veci. Hlasi se nejvys 1x za 2 s,
        aby to nezaplavilo log."""
        if not self._debug:
            return
        _t = time.time()
        if _t - getattr(self, "_proc_last", 0.0) < 2.0:
            return
        self._proc_last = _t
        _log.info("gesto[dbg]: mavani NE — %s", duvod)

    def _je_mavani(self, now: float) -> bool:
        """HANS_GESTURE_WAVE_V1 — je pohyb dlane zamavani?

        Mavani = dlan jde vodorovne SEM A TAM. Sama zmena polohy nestaci
        (ruka mohla jen prejet zaberem), proto se vedle amplitudy vyzaduje
        i ZMENA SMERU — to je jediny znak, ktery mavani od presunu odlisi.
        Drobny sum kolem klidne ruky se odfiltruje prahem _wave_eps.
        """
        okno = [p for p in self._wave_track
                if now - p[0] <= self._wave_window_s]
        if len(okno) < self._wave_min_samples:
            self._proc_ne("malo vzorku v okne: %d < %d"
                          % (len(okno), self._wave_min_samples))
            return False
        xs = [p[1] for p in okno]
        rozpeti = max(xs) - min(xs)
        # HANS_GESTURE_WAVE_RELAMP_V1 (6.9.) — rozpeti se meri v NASOBCICH
        # SIRKY DLANE, ne v podilu zaberu.
        # Zmereno 6.9. ze 2 m: „male rozpeti 0.042 < 0.060 (vzorku 5)" —
        # tyz pohyb rukou zabira z dvou metru zhruba polovicni podil zaberu
        # nez z jednoho, takze pevny prah je ve skutecnosti prah na
        # VZDALENOST. Snizit ho nejde: zblizka by zacal chytat drobne pohyby.
        # Sirka dlane se se vzdalenosti zmensuje stejne jako mavnuti, takze
        # jejich POMER uz na vzdalenosti nezavisi.
        _sirky = sorted(p[2] for p in okno)
        _dlan  = _sirky[len(_sirky) // 2]          # median
        _rel   = rozpeti / _dlan
        if rozpeti < self._wave_min_amp or _rel < self._wave_min_amp_rel:
            self._proc_ne("male rozpeti: %.3f (=%.2f sirky dlane %.3f) "
                          "< abs %.3f / rel %.2f, vzorku %d"
                          % (rozpeti, _rel, _dlan, self._wave_min_amp,
                             self._wave_min_amp_rel, len(okno)))
            return False
        smer = 0
        obraty = 0
        for i in range(1, len(xs)):
            d = xs[i] - xs[i - 1]
            if abs(d) < self._wave_eps:
                continue
            s = 1 if d > 0 else -1
            if smer and s != smer:
                obraty += 1
            smer = s
        if obraty < self._wave_min_rev:
            self._proc_ne("malo obratu: %d < %d (rozpeti %.3f, vzorku %d)"
                          % (obraty, self._wave_min_rev, rozpeti, len(okno)))
            return False
        # HANS_GESTURE_WAVE_METRIKY_V1 — dve veliciny, ktere podle snimku
        # z 13:45 oddeluji skutecne mavani od halucinace na polstari:
        #  (a) pomer dlan/tvar — realna dlan je zhruba jako oblicej,
        #      falesna detekce na textilii byva zlomkem;
        #  (b) obratu za sekundu — mavani je rytmicke, falesny zachyt byl
        #      dlouhy pomaly drift (24 vzorku, 1 obrat).
        # HANS_GESTURE_WAVE_MIN_PALM_V1 (6.9.) — ABSOLUTNI velikost dlane.
        # Rozhodnuti uzivatele 6.9.: radeji gesto jen zblizka, ale spolehlive.
        # Duvod z merenych dat: halucinace na textilii a zaluziich mely dlan
        # 0,012-0,021 sirky zaberu, skutecna dlan zblizka ~0,07-0,10.
        # Velikost je odlisi bezpecne, zatimco POMER (dlan/tvar) ne — u
        # falesne detekce vysel 0,48-0,80, protoze mala byla i tvar.
        # ⚠️ Cena: gesto dosahne jen asi na metr. Dosah se vrati az
        # s pretrenovanym modelem, ne dalsim ladenim prahu.
        if self._wave_min_palm_w > 0 and _dlan < self._wave_min_palm_w:
            self._proc_ne("dlan moc mala: %.3f < %.3f (asi halucinace na "
                          "textilii, nebo je clovek moc daleko)"
                          % (_dlan, self._wave_min_palm_w))
            return False
        _tvar = getattr(self, "_sirka_tvare", 0.0)
        _pomer_tvar = (_dlan / _tvar) if _tvar > 1e-6 else -1.0
        _doba = max(okno[-1][0] - okno[0][0], 1e-6)
        _obr_s = obraty / _doba
        self._wave_popis = (
            "rozpeti %.3f = %.2f sirky dlane, obratu %d, vzorku %d "
            "| dlan %.3f = %.2f tvare, %.1f obratu/s, %.1f s"
            % (rozpeti, _rel, obraty, len(okno),
               _dlan, _pomer_tvar, _obr_s, _doba))
        # HANS_GESTURE_WAVE_NEAR_FACE_V1 (6.9.) — mavajici ruka je U TELA.
        # Doloženo snimky z 13:45 a 13:53: falesne zachyty lezely na
        # prikryvce a polstari DALEKO od osob, klidne u kraje zaberu.
        # Vzdalenost se meri v nasobcich sirky tvare, takze plati na metr
        # i na dva.
        _stred = getattr(self, "_stred_tvare", None)
        if self._wave_max_od_tvare > 0 and _stred and _tvar > 1e-6:
            _sx = sum(p[1] for p in okno) / len(okno)
            _vzd = abs(_sx - _stred[0]) / _tvar
            if _vzd > self._wave_max_od_tvare:
                self._proc_ne("dlan daleko od tvare: %.1f sirky tvare > %.1f"
                              % (_vzd, self._wave_max_od_tvare))
                return False
            self._wave_popis += ", %.1f tvare od hlavy" % _vzd
        if self._wave_min_palm_face > 0 and 0 <= _pomer_tvar < self._wave_min_palm_face:
            self._proc_ne("dlan moc mala vuci tvari: %.2f < %.2f (%s)"
                          % (_pomer_tvar, self._wave_min_palm_face,
                             self._wave_popis))
            return False
        if self._wave_min_obr_s > 0 and _obr_s < self._wave_min_obr_s:
            self._proc_ne("prilis pomaly pohyb: %.1f obratu/s < %.1f (%s)"
                          % (_obr_s, self._wave_min_obr_s, self._wave_popis))
            return False
        return True

    def _update_state(self, gesture_id: int, bbox=None, lm=None):
        now = time.time()

        # Warmup — ignoruj první frames po startu
        if self._warmup_frames > 0:
            self._warmup_frames -= 1
            return

        if self._debug:
            self._dbg_frames += 1
            if now - self._dbg_last >= 2.0:
                self._dbg_last = now
                _log.info("gesto[dbg]: odeslano=%d zpracovano=%d dlani=%d "
                          "posledni_id=%s bbox=%s stopa=%d",
                          self._dbg_submits, self._dbg_frames, self._dbg_open,
                          gesture_id, "ano" if bbox else "ne",
                          len(self._wave_track))

        # HANS_GESTURE_WAVE_V1 — stopa polohy dlane. Sbira se PRED hlasovanim
        # a PRED vetvi na GESTURE_NONE zamerne: pri mavani je dlan casto
        # rozmazana pohybem, takze detekce mezi kmity vypadava. Kdyby se
        # stopa cistila s kazdym vypadkem, mavani by se nikdy neposkladalo.
        if gesture_id == GESTURE_OPEN_HAND and bbox:
            try:
                # HANS_GESTURE_WAVE_RELAMP_V1 — do stopy patri i SIRKA dlane:
                # slouzi jako meritko vzdalenosti (viz _je_mavani).
                _x1, _x2 = float(bbox[0]), float(bbox[2])
                self._wave_track.append(
                    (now, (_x1 + _x2) / 2.0, max(_x2 - _x1, 1e-6)))
            except Exception:
                pass
            self._dbg_open += 1
            # POZOR: nejdriv si zapamatuj, kdy byla dlan videna NAPOSLED,
            # a teprve pak prepis. Kontrola odjisteni nize porovnava prave
            # tuhle predchozi hodnotu — proti `now` by vysla vzdy 0.
            _naposled = self._wave_last_seen
            self._wave_last_seen = now
            # HANS_GESTURE_WAVE_PREVOTE_V1 (6.9.) — mavani se vyhodnocuje UZ
            # TADY, PRED majoritnim hlasovanim.
            # Duvod (zmereno za behu, ne odhadnuto): pri mavani je dlan
            # rozmazana a detekce vypadava — open_hand chodi ~0,5x/s proti
            # ~14 framum/s. Hlasovani chce 4 z 5 a sest nul za sebou buffer
            # vycisti, takze mavani se za nim NIKDY neposklada. Hlasovani je
            # spravny nastroj pro STATICKE gesto (drzena dlan, pest), kdezto
            # mavani je definovane POHYBEM — a ten uz stopa poloh nese sama.
            if self._wave_only and self.on_gesture:
                # HANS_GESTURE_WAVE_EDGE_V1 (6.9.) — mavani je UDALOST, ne stav.
                # Zmereno 6.9. se dvema lidmi v mistnosti: 701 platnych
                # vyhodnoceni → 34 POZDRAVU ZA 12 MINUT. Dokud se ruka hybe
                # (a pri hovoru se gestikuluje porad), podminky se plni
                # znovu a znovu, takze cooldown jen ridil zaplavu.
                # Po pozdravu se proto ceka, az dlan na chvili ZMIZI —
                # teprve pak je dalsi mavnuti nove.
                if not self._wave_armed:
                    if now - _naposled >= self._wave_rearm_s:
                        self._wave_armed = True
                        self._wave_track.clear()
                    else:
                        return
                if (self._je_mavani(now) and
                        now - self._wave_last_fired >= self._wave_cooldown):
                    self._wave_last_fired  = now
                    self._wave_armed       = False
                    self._wave_track.clear()
                    self._vote_buf.clear()
                    self._last_fired_name  = "wave"
                    self.last_gesture      = "wave"
                    self.last_gesture_time = now
                    self.last_landmarks    = lm
                    _log.info("gesto: zamavani — %s",
                              getattr(self, "_wave_popis", "?"))
                    print("[Gesture] FIRED: wave (pohyb)", flush=True)
                    self.on_gesture("wave", bbox)
                    return

        # Majority vote z posledních 5 framů
        self._vote_buf.append(gesture_id)

        # Reset po 5 nulách
        if gesture_id == GESTURE_NONE:
            self._none_count = getattr(self, '_none_count', 0) + 1
            if self._none_count > 5:
                self._current         = GESTURE_NONE
                self._last_fired_name = None
                self._vote_buf.clear()
                self.last_landmarks   = None
            return
        self._none_count = 0
        if lm is not None:
            self.last_landmarks = lm   # průběžná aktualizace

        # Potřebujeme alespoň 4 ze 5 stejných
        if len(self._vote_buf) < 5:
            return
        counts = Counter(self._vote_buf)
        voted, cnt = counts.most_common(1)[0]
        if voted == GESTURE_NONE or cnt < 4:
            return

        name = GESTURE_NAMES.get(voted)
        if not name:
            return

        if voted != self._current:
            self._current         = voted
            self._current_since   = now
            self._last_fired_name = None
            self._vote_buf.clear()   # vymaž buffer při změně gesta

        # Per-gesture hold time
        required_hold = self._hold_per_gesture.get(voted, self._hold_seconds)
        held = now - self._current_since
        if (held >= required_hold and
                name != self._last_fired_name and
                self.on_gesture):
            # HANS_GESTURE_WAVE_V1 — otevrena dlan strili JEN jako "wave".
            # Mavani JE otevrena dlan, takze bez tohohle by kazde zamavani
            # spustilo i akci staticke dlane (drive pauza Kodi).
            if voted == GESTURE_OPEN_HAND and self._wave_only:
                # HANS_GESTURE_WAVE_PREVOTE_V1 — mavani uz obslouzila vetev
                # nad hlasovanim; sem dojde jen DRZENA dlan, ktera zamerne
                # nedela nic (jinak by ji doprovazel kazdy pozdrav).
                return
            self._last_fired_name  = name
            self.last_gesture      = name
            self.last_gesture_time = now
            self.last_landmarks    = lm   # uložení pro vykreslení
            self._vote_buf.clear()
            print(f"[Gesture] FIRED: {name} votes={cnt}/5", flush=True)
            self.on_gesture(name, bbox)
