"""BODY_TRACK_SHADOW_V1 (3. 10.) — stínová stopa POSTAVY, která nese jméno z tváře.

Nápad uživatele: tvář rozpozná KDO, obdélník postavy pak jméno nese dál podle
polohy a pohybu (odkud kam jde), takže se tvář nemusí rozpoznávat pořád dokola
a jméno vydrží i ve chvíli, kdy je tvář odvrácená nebo rozmazaná.
Soukromí schváleno 3. 10. („nic se vlastně nemění, jen se roztáhne obdélník“):
z postavy se bere JEN obdélník a body (nos), žádný vzhled — kdo to je, určuje
dál výhradně tvář. ⛔ Model vzhledu postavy (oblečení, stavba) je zamítnut 11. 8.

Postava: yolov8s_pose na Hailu přes `/tmp/hailo_body.sock` (celý záběr,
práh 0,3 — změřeno 3. 10. na 69 záběrech: tvář přiřazena 36/37, falešné jen
sluchátka u kamery, která tvář nikdy nedostanou; 0,2 už páruje tvář se
dvěma postavami). Tvář → postava podle bodu NOSU v boxu tváře (funguje i vleže).

STÍN: nic nemění, jen jednou za minutu zapíše, co by stopa udělala:
  • kolik tváří bez jména by pojmenovala a kolikrát by se s tváří rozešla,
  • kolik rozpoznání by bylo potřeba (nová/nejmenovaná stopa, po křížení,
    kontrola jednou za `verify_s`) proti dnešnímu „každý snímek“.
Rozpory a křížení jdou i do `data/mereni/postava_stin/<den>.jsonl`; k rozporu od 4. 10.
nejvýš 1 snímek za 30 s do `snimky/` (BODY_TRACK_ROZPOR_SNIMEK_V1) — k ručnímu označení.
"""
import json
import logging
import os
import socket
import struct
import threading
import time
from collections import deque

import numpy as np

_log = logging.getLogger("body_track")
SOCK = "/tmp/hailo_body.sock"
_NEZNAMY = ("Unknown", "unknown", "...", "?", "", None)


def _iou(a, b):
    xx1, yy1 = max(a[0], b[0]), max(a[1], b[1])
    xx2, yy2 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0.0, xx2 - xx1) * max(0.0, yy2 - yy1)
    ua = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / ua if ua > 0 else 0.0


class _Stopa:
    _dalsi = 1

    def __init__(self, box, kp, t):
        self.id = _Stopa._dalsi
        _Stopa._dalsi += 1
        self.box = list(box)
        self.kp = kp
        self.v = (0.0, 0.0)          # rychlost středu (zlomek záběru / s)
        self.t = t                   # poslední pose detekce
        self.vznik = t
        self.jmeno = None
        self.jmeno_t = 0.0           # poslední potvrzení jména tváří
        self.over_t = 0.0            # poslední (hypotetické) rozpoznání
        self.hlasy = deque(maxlen=6)
        self.tvar_t = 0.0            # kdy naposledy měla přiřazenou tvář
        self.mela_tvar = False
        self.kriz_t = 0.0            # poslední křížení s jinou stopou
        self.pohyb = 0.0             # uražená dráha středu

    def stred(self, b=None):
        b = b or self.box
        return ((b[0] + b[2]) / 2.0, (b[1] + b[3]) / 2.0)

    def predikce(self, t):
        dt = max(0.0, min(1.5, t - self.t))
        dx, dy = self.v[0] * dt, self.v[1] * dt
        b = self.box
        return [b[0] + dx, b[1] + dy, b[2] + dx, b[3] + dy]

    def nos(self, kp_min):
        if self.kp is None:
            return None
        for j in (0, 1, 2):
            if self.kp[j, 2] >= kp_min:
                return float(self.kp[j, 0]), float(self.kp[j, 1]), j
        return None


class BodyTrackShadow:
    def __init__(self, config):
        c = (config or {}).get("body_track", {}) or {}
        self.enabled = bool(c.get("enabled", False))
        self.interval = float(c.get("interval_s", 0.33))
        self.prah = float(c.get("pose_prah", 0.3))
        self.kp_min = float(c.get("kp_min", 0.3))
        self.hold_s = float(c.get("hold_s", 120.0))
        self.verify_s = float(c.get("verify_s", 20.0))
        self.min_hlasu = int(c.get("min_votes", 2))
        self.prepis_hlasu = int(c.get("override_votes", 4))   # z posledních 6
        self.rezerva = float(c.get("face_margin", 0.5))        # × šířka tváře
        self.grace_s = float(c.get("grace_s", 1.5))
        self.iou_min = float(c.get("iou_min", 0.2))
        self.po_krizeni_s = float(c.get("after_cross_s", 3.0))
        self.sirka = int(c.get("send_width", 640))
        self.dir = str(c.get("log_dir", "data/mereni/postava_stin"))
        self.snimek_rozporu = bool(c.get("rozpor_snapshot", False))
        self.snimek_rozporu_s = float(c.get("rozpor_snapshot_interval_s", 30.0))
        self.snimek_rozporu_keep = int(c.get("rozpor_snapshot_keep", 300))
        self._rozpor_snimek_t = 0.0
        self._lock = threading.Lock()
        self._snimek = None
        self._snimek_t = 0.0
        self._stopy = []
        self._sock = None
        self._st = self._nove_st(time.time())
        if self.enabled:
            threading.Thread(target=self._smycka, daemon=True,
                             name="body_track").start()
            _log.info("BODY_TRACK_SHADOW_V1: stínová stopa postavy běží "
                      "(%.2f s, práh %.2f)", self.interval, self.prah)

    @staticmethod
    def _nove_st(t):
        return {"t0": t, "tvari": 0, "bez_jm": 0, "pojmenuje": 0, "shoda": 0,
                "rozpor": 0, "rozpor_po_kriz": 0, "potreba": 0, "kriz": 0,
                "jm_bez_tvare_s": 0.0, "pose_ms": [], "chyby": 0,
                "worry_snimku": 0, "worry_odvraceno": 0, "vyprselo": 0}

    # ── vstup z frame_pipeline (vlákno hlavní smyčky — jen rychlé věci) ──────
    def feed(self, frame, tvare, now):
        """tvare = [(box, jmeno_ze_snimku, zobrazene_jmeno)] po hlasování."""
        if not self.enabled:
            return
        try:
            with self._lock:
                if frame is not None:
                    self._snimek, self._snimek_t = frame, now
                self._zpracuj_tvare(tvare, now)
        except Exception as e:
            self._st["chyby"] += 1
            _log.debug("body_track feed: %s", e)

    def _prirad(self, fbox):
        # Tvář a postava jsou z různých snímků (pose běží po ~0,33 s), proto
        # rezerva: 3. 10. ležel nos 0,004 vedle boxu tváře široké 0,015.
        mx = (fbox[2] - fbox[0]) * self.rezerva
        my = (fbox[3] - fbox[1]) * self.rezerva
        x1, y1, x2, y2 = fbox[0] - mx, fbox[1] - my, fbox[2] + mx, fbox[3] + my
        kand = []
        for s in self._stopy:
            n = s.nos(self.kp_min)
            if n and x1 <= n[0] <= x2 and y1 <= n[1] <= y2:
                kand.append(s)
        if len(kand) > 1:
            # víc nosů v rozšířeném boxu → vezmi ten uvnitř původního boxu
            uvn = [s for s in kand if (lambda n: fbox[0] <= n[0] <= fbox[2]
                                       and fbox[1] <= n[1] <= fbox[3])(s.nos(self.kp_min))]
            kand = uvn if len(uvn) == 1 else kand
        if len(kand) == 1:
            return kand[0]
        if not kand:
            cx, cy = (fbox[0] + fbox[2]) / 2, (fbox[1] + fbox[3]) / 2
            uvnitr = [s for s in self._stopy
                      if s.box[0] <= cx <= s.box[2] and s.box[1] <= cy <= s.box[3]]
            if len(uvnitr) == 1:
                return uvnitr[0]
        return None

    def _zpracuj_tvare(self, tvare, now):
        st = self._st
        worry = False
        worry_vse_pojmenovano = True
        for box, syrove, zobrazene in tvare:
            st["tvari"] += 1
            s = self._prirad(box)
            if s is not None:
                s.tvar_t, s.mela_tvar = now, True
                if syrove not in _NEZNAMY:
                    s.hlasy.append(syrove)
                    posl = list(s.hlasy)[-self.min_hlasu:]
                    if s.jmeno is None:
                        if len(posl) == self.min_hlasu and len(set(posl)) == 1:
                            s.jmeno, s.jmeno_t = syrove, now
                    elif s.jmeno == syrove:
                        s.jmeno_t = now
                    # přepsat JINÉ jméno chce silnější doklad než nové dát:
                    # 3. 10. stačily 2 chybné snímky po sobě a stopa přeskočila
                    elif list(s.hlasy).count(syrove) >= self.prepis_hlasu:
                        self._udalost("prepis", s, now, tvar=syrove)
                        s.jmeno, s.jmeno_t = syrove, now
            elif now - getattr(self, "_nepr_t", 0.0) >= 10.0:
                # vzorek tváře bez postavy (jen souřadnice) — proč se nespárovala
                self._nepr_t = now
                self._udalost_raw({"t": round(now, 2), "druh": "nepřiřazena",
                                   "tvar": [round(x, 3) for x in box[:4]],
                                   "jmeno_snimek": syrove, "zobrazeno": zobrazene,
                                   "stopy": [{"id": x.id, "box": [round(v, 3) for v in x.box],
                                              "nos": (lambda n: [round(n[0], 3), round(n[1], 3), n[2]] if n else None)(x.nos(self.kp_min)),
                                              "jmeno": x.jmeno} for x in self._stopy]})
            # potřebné rozpoznání (politika stopy)
            if (s is None or s.jmeno is None
                    or now - s.kriz_t < self.po_krizeni_s
                    or now - s.over_t >= self.verify_s):
                st["potreba"] += 1
                if s is not None:
                    s.over_t = now
            # srovnání s tím, co Hans dnes ukazuje
            zn = zobrazene not in _NEZNAMY
            if not zn:
                st["bez_jm"] += 1
                worry = True
                if s is not None and s.jmeno:
                    st["pojmenuje"] += 1
                else:
                    worry_vse_pojmenovano = False
            elif s is not None and s.jmeno:
                if s.jmeno == zobrazene:
                    st["shoda"] += 1
                else:
                    st["rozpor"] += 1
                    if now - s.kriz_t < 10.0:
                        st["rozpor_po_kriz"] += 1
                    self._udalost("rozpor", s, now, tvar=zobrazene,
                                  snimek=self._snimek_rozporu(s, box, zobrazene, now))
        if worry:
            st["worry_snimku"] += 1
            if worry_vse_pojmenovano:
                st["worry_odvraceno"] += 1

    # ── vlastní vlákno: pose na celém záběru + sledování ──────────────────
    def _smycka(self):
        while True:
            t0 = time.time()
            try:
                with self._lock:
                    fr, ft = self._snimek, self._snimek_t
                    self._snimek = None
                if fr is not None and t0 - ft < 1.0:
                    osoby = self._pose(fr)
                    if osoby is not None:
                        with self._lock:
                            self._aktualizuj(osoby, time.time())
                with self._lock:
                    self._souhrn(time.time())
            except Exception as e:
                self._st["chyby"] += 1
                self._sock = None
                _log.debug("body_track: %s", e)
            time.sleep(max(0.05, self.interval - (time.time() - t0)))

    def _pose(self, frame):
        import cv2
        h, w = frame.shape[:2]
        if w > self.sirka:
            nh = int(h * self.sirka / w)
            frame = cv2.resize(frame, (self.sirka, nh), interpolation=cv2.INTER_AREA)
            h, w = frame.shape[:2]
        frame = np.ascontiguousarray(frame, dtype=np.uint8)
        if self._sock is None:
            if not os.path.exists(SOCK):
                return None
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(2.0)
            s.connect(SOCK)
            self._sock = s
        t = time.time()
        raw = frame.tobytes()
        self._sock.sendall(struct.pack(">I", len(raw)) + raw
                           + struct.pack(">HHf", w, h, self.prah))
        n = struct.unpack(">I", self._recv(4))[0]
        data = np.frombuffer(self._recv(n * 56 * 4), dtype=">f4").reshape(n, 56) \
            if n else np.zeros((0, 56))
        self._st["pose_ms"].append(1000.0 * (time.time() - t))
        return [(float(r[0]), [float(x) for x in r[1:5]],
                 np.asarray(r[5:56], np.float32).reshape(17, 3)) for r in data]

    def _recv(self, n):
        buf = b""
        while len(buf) < n:
            c = self._sock.recv(n - len(buf))
            if not c:
                raise ConnectionError("hailo_body: spojení zavřeno")
            buf += c
        return buf

    def _aktualizuj(self, osoby, t):
        pred = {s.id: s.predikce(t) for s in self._stopy}
        # křížení: dvě pojmenované stopy se predikcí překrývají
        for i, a in enumerate(self._stopy):
            for b in self._stopy[i + 1:]:
                if (a.jmeno and b.jmeno and a.jmeno != b.jmeno
                        and _iou(pred[a.id], pred[b.id]) >= 0.3
                        and t - a.kriz_t > 2.0):
                    a.kriz_t = b.kriz_t = t
                    self._st["kriz"] += 1
                    self._udalost("krizeni", a, t, druha=b.jmeno)
        # párování predikce × detekce (hladově podle IoU)
        pary = sorted(((_iou(pred[s.id], o[1]), si, oi)
                       for si, s in enumerate(self._stopy)
                       for oi, o in enumerate(osoby)), reverse=True)
        us, uo = set(), set()
        for iou, si, oi in pary:
            if iou < self.iou_min:
                break
            if si in us or oi in uo:
                continue
            us.add(si); uo.add(oi)
            s, o = self._stopy[si], osoby[oi]
            dt = max(1e-3, t - s.t)
            (x0, y0), (x1, y1) = s.stred(), s.stred(o[1])
            vx, vy = (x1 - x0) / dt, (y1 - y0) / dt
            s.v = (0.5 * s.v[0] + 0.5 * vx, 0.5 * s.v[1] + 0.5 * vy)
            s.pohyb += abs(x1 - x0) + abs(y1 - y0)
            s.box, s.kp, s.t = list(o[1]), o[2], t
        for oi, o in enumerate(osoby):
            if oi not in uo:
                self._stopy.append(_Stopa(o[1], o[2], t))
        ziva = []
        for s in self._stopy:
            if t - s.t > self.grace_s:
                continue
            if s.jmeno and t - s.jmeno_t > self.hold_s:
                s.jmeno = None
                self._st["vyprselo"] += 1
            if s.jmeno and t - s.tvar_t > 1.0:
                self._st["jm_bez_tvare_s"] += self.interval
            ziva.append(s)
        self._stopy = ziva

    def _udalost(self, druh, s, t, **kw):
        zaznam = {"t": round(t, 2), "druh": druh, "stopa": s.id,
                  "jmeno": s.jmeno, "box": [round(x, 3) for x in s.box],
                  "v": [round(x, 3) for x in s.v]}
        zaznam.update(kw)
        self._udalost_raw(zaznam)

    def _snimek_rozporu(self, s, fbox, zobrazene, now):
        """BODY_TRACK_ROZPOR_SNIMEK_V1 (4. 10.) — snímek k rozporu stopy a tváře.

        Rozbor 3. 10.: 441 rozporů, 98 krátkých úseků, z čísel nejde poznat,
        jestli se plete tvář, nebo stopa. Uloží se snímek s oběma rámečky
        (zelený = postava + jméno stopy, červený = tvář + zobrazené jméno),
        nejvýš jeden za `rozpor_snapshot_interval_s`; uživatel je označí.
        Vrací jméno souboru (do jsonl), nebo None. Zápis běží ve vlákně.
        """
        if not self.snimek_rozporu or self._snimek is None:
            return None
        if now - self._rozpor_snimek_t < self.snimek_rozporu_s:
            return None
        self._rozpor_snimek_t = now
        jmeno = time.strftime("%Y%m%d_%H%M%S", time.localtime(now)) + "_s%d.jpg" % s.id
        args = (self._snimek.copy(), list(s.box), list(fbox[:4]),
                str(s.jmeno), str(zobrazene), jmeno)
        threading.Thread(target=self._uloz_snimek_rozporu, args=args,
                         daemon=True).start()
        return jmeno

    def _uloz_snimek_rozporu(self, img, sbox, fbox, jm_stopa, jm_tvar, jmeno):
        try:
            import cv2
            d = os.path.join(self.dir, "snimky")
            os.makedirs(d, exist_ok=True)
            h, w = img.shape[:2]
            if w > 1024:
                img = cv2.resize(img, (1024, int(h * 1024 / w)))
                h, w = img.shape[:2]
            img = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)   # jako frame_sampling
            for b, barva, popis in ((sbox, (0, 200, 0), "stopa: " + jm_stopa),
                                    (fbox, (0, 0, 230), "tvar: " + jm_tvar)):
                x0, y0, x1, y1 = (int(b[0] * w), int(b[1] * h),
                                  int(b[2] * w), int(b[3] * h))
                cv2.rectangle(img, (x0, y0), (x1, y1), barva, 2)
                cv2.putText(img, popis, (x0, max(14, y0 - 4)),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.5, barva, 1, cv2.LINE_AA)
            cv2.imwrite(os.path.join(d, jmeno), img, [cv2.IMWRITE_JPEG_QUALITY, 85])
            # označené snímky (tools/oznac_rozpory.py → stitky.json) se nemažou
            try:
                with open(os.path.join(d, "stitky.json"), encoding="utf-8") as f:
                    oznacene = set(json.load(f))
            except Exception:
                oznacene = set()
            jpg = sorted(x for x in os.listdir(d) if x.endswith(".jpg") and x not in oznacene)
            for stary in jpg[:-self.snimek_rozporu_keep]:
                try: os.unlink(os.path.join(d, stary))
                except Exception: pass
        except Exception as e:
            _log.debug("body_track snímek rozporu: %s", e)

    def _udalost_raw(self, zaznam):
        try:
            os.makedirs(self.dir, exist_ok=True)
            with open(os.path.join(self.dir, time.strftime("%Y%m%d") + ".jsonl"),
                      "a", encoding="utf-8") as f:
                f.write(json.dumps(zaznam, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def _souhrn(self, t):
        st = self._st
        if t - st["t0"] < 60.0:
            return
        if st["tvari"] or self._stopy:
            ms = st["pose_ms"]
            jm = sum(1 for s in self._stopy if s.jmeno)
            nehybne = sum(1 for s in self._stopy
                          if not s.mela_tvar and t - s.vznik > 60 and s.pohyb < 0.05)
            _log.info(
                "postava stín za minutu: tváří %d (bez jména %d → stopa by pojmenovala %d), "
                "shoda %d, rozpor %d (po křížení %d), rozpoznání potřeba %d z %d, "
                "stopy %d (se jménem %d, nehybné bez tváře %d), křížení %d, "
                "jméno bez tváře %.0f s, vypršelo %d, worry snímků %d (odvráceno %d), "
                "pose %.0f ms, chyby %d",
                st["tvari"], st["bez_jm"], st["pojmenuje"], st["shoda"],
                st["rozpor"], st["rozpor_po_kriz"], st["potreba"], st["tvari"],
                len(self._stopy), jm, nehybne, st["kriz"], st["jm_bez_tvare_s"],
                st["vyprselo"], st["worry_snimku"], st["worry_odvraceno"],
                float(np.median(ms)) if ms else -1.0, st["chyby"])
        self._st = self._nove_st(t)
