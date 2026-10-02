"""ADAFACE_SHADOW_V1 (27. 9.) — stínové rozpoznávání druhým modelem.

Rozhoduje dál ArcFace r50 (`PCEmbedder` + `FaceDB.identify_pc`). Tenhle modul
dostane TYTÉŽ výřezy tváří, nechá je na PC spočítat AdaFace IR101 (endpoint
`/embed2` služby hans-face-embed), porovná s vlastní galerií a jen ZAPÍŠE, jak
by rozhodl on. Nic nevrací a nic neovlivňuje.

Proč: offline na označeném sběru (den-holdout 9.–10. × 11.–12. 8.) byl AdaFace
při stejném náskoku 0,06 lepší ve všem — správně 71,2 × 66,7 %, záměny
2,6 × 4,2 %, přijaté falešné detekce 25,8 × 27,8 %. Sběr ale obsahuje hlavně
dobré snímky, proto stín ze skutečného provozu. Spolupráce obou modelů
(průměr skóre, shoda) byla offline HORŠÍ než AdaFace sám. Backlog `ADAFACE_27_09`.

Běží ve vlastním vlákně s krátkou frontou: když nestíhá, dávka se zahodí
(počítá se), rozpoznávání nikdy nečeká.
"""
from __future__ import annotations

import logging
import pickle
import queue
import threading
import time
import urllib.request

import numpy as np

_log = logging.getLogger("face_shadow")
_diag = logging.getLogger("recognition_diag")
_NIC = ("Unknown", "unknown", "", "?", "...", None)


class FaceShadow:
    def __init__(self, config: dict):
        pc = (config.get("pc_embed", {}) or {})
        c = (pc.get("shadow", {}) or {})
        self.enabled = bool(c.get("enabled", False))
        base = str(pc.get("url", "http://127.0.0.1:8765/embed")).rsplit("/", 1)[0]
        self._url = c.get("url") or base + "/embed2"
        self._timeout = float(c.get("timeout_s", 3.0))
        # ADAFACE_SHADOW_SAVE_V1 (29. 9., souhlas uživatele) — výřezy rozporů
        # k ručnímu roztřídění (kdo to je, kdo měl pravdu). Jen lokálně v data/
        # (gitignore); po vyhodnocení složku SMAZAT a volbu vypnout.
        self._uloz = bool(c.get("save_rozpory", False))
        self._uloz_dir = str(c.get("save_dir", "data/mereni/adaface/rozpory"))
        self._uloz_limit = int(c.get("save_jednostranne_den", 20))
        self._uloz_stav = {}        # druh → (den, počet, čas posledního)
        self._margin = float(c.get("margin", 0.06))
        self._k = int(c.get("top_k", 3))
        self._gal = {}
        self._q = queue.Queue(maxsize=int(c.get("queue", 4)))
        self._st = self._nove()
        self._chyb = 0
        self._pauza_do = 0.0
        if not self.enabled:
            return
        try:
            with open(c.get("gallery", "data/known_faces_pc_ada.pkl"), "rb") as f:
                g = pickle.load(f)
            for n, v in g.items():
                if str(n).startswith("_"):
                    continue
                m = np.asarray(v, dtype=np.float32)
                self._gal[n] = m / (np.linalg.norm(m, axis=1, keepdims=True) + 1e-9)
        except Exception as e:
            _log.warning("stín AdaFace: galerie nenačtena (%s) — vypínám", e)
            self.enabled = False
            return
        threading.Thread(target=self._smycka, name="face-shadow", daemon=True).start()
        _log.info("stín AdaFace zapnut (%s, osob %d, náskok %.2f)",
                  self._url, len(self._gal), self._margin)

    @staticmethod
    def _nove():
        return {"t": time.time(), "n": 0, "r50": 0, "ada": 0, "shoda": 0,
                "jine": 0, "jen_ada": 0, "jen_r50": 0, "zahozeno": 0, "chyba": 0}

    def submit(self, crops: list, r50_names: list):
        """Nečeká. crops = RGB 112×112 uint8, r50_names = rozhodnutí r50 ke každému."""
        if not self.enabled or not crops or time.time() < self._pauza_do:
            return
        try:
            self._q.put_nowait((list(crops), list(r50_names)))
        except queue.Full:
            self._st["zahozeno"] += 1

    def identify(self, emb) -> tuple:
        q = np.asarray(emb, dtype=np.float32)
        q = q / (np.linalg.norm(q) + 1e-9)
        sc = {}
        for n, m in self._gal.items():
            s = m @ q
            k = min(self._k, len(s))
            sc[n] = float(np.mean(np.partition(s, -k)[-k:]))
        if not sc:
            return "Unknown", 0.0
        srt = sorted(sc.items(), key=lambda x: x[1], reverse=True)
        druhe = srt[1][1] if len(srt) > 1 else 0.0
        if srt[0][1] - druhe < self._margin:
            return "Unknown", 0.0
        return srt[0][0], round(srt[0][1], 2)

    def _uloz_rozpor(self, crop, r50, ada, conf, r_ok, a_ok):
        """ADAFACE_SHADOW_SAVE_V1 — rozpor jmen vždy; jednostranné jméno jen
        vzorek (1/min, `save_jednostranne_den` denně od každého druhu)."""
        try:
            druh = "jine" if (r_ok and a_ok) else ("jen_ada" if a_ok else "jen_r50")
            ted = time.time()
            den = time.strftime("%Y%m%d")
            d0, n, t0 = self._uloz_stav.get(druh, (den, 0, 0.0))
            if d0 != den:
                n = 0
            if druh != "jine" and (n >= self._uloz_limit or ted - t0 < 60):
                return
            import os
            from PIL import Image
            cesta = os.path.join(self._uloz_dir, druh)
            os.makedirs(cesta, exist_ok=True)
            jm = "%s%03d_r50-%s_ada-%s_%.2f.png" % (time.strftime("%Y%m%d_%H%M%S"),
                                                     int(ted * 1000) % 1000,
                                                     r50 or "nic", ada or "nic", conf)
            Image.fromarray(np.asarray(crop, dtype=np.uint8)).save(os.path.join(cesta, jm))
            self._uloz_stav[druh] = (den, n + 1, ted)
        except Exception as ex:
            _log.debug("stín: uložení výřezu: %s", ex)

    def _smycka(self):
        while True:
            crops, jmena = self._q.get()
            try:
                arr = np.ascontiguousarray(np.stack(crops), dtype=np.uint8)
                req = urllib.request.Request(
                    self._url, data=arr.tobytes(),
                    headers={"Content-Type": "application/octet-stream"})
                with urllib.request.urlopen(req, timeout=self._timeout) as r:
                    E = np.frombuffer(r.read(), np.float32).reshape(len(crops), -1)
                self._chyb = 0
            except Exception as e:
                self._st["chyba"] += 1
                self._chyb += 1
                if self._chyb >= 3:
                    self._pauza_do = time.time() + 60.0
                    _log.info("stín AdaFace: PC neodpovídá (%s), 60 s pauza", e)
                    self._chyb = 0
                continue
            for _i, (e, r50) in enumerate(zip(E, jmena)):
                ada, conf = self.identify(e)
                s = self._st
                s["n"] += 1
                r_ok, a_ok = r50 not in _NIC, ada not in _NIC
                s["r50"] += r_ok
                s["ada"] += a_ok
                if r_ok and a_ok:
                    s["shoda" if r50 == ada else "jine"] += 1
                elif a_ok:
                    s["jen_ada"] += 1
                elif r_ok:
                    s["jen_r50"] += 1
                if r50 != ada and (r_ok or a_ok):
                    _diag.info("SHADOW r50=%s ada=%s:%.2f", r50 or "?", ada, conf)
                    if self._uloz:
                        self._uloz_rozpor(crops[_i], r50, ada, conf, r_ok, a_ok)
            if time.time() - self._st["t"] >= 60.0:
                s = self._st
                if s["n"] or s["chyba"] or s["zahozeno"]:
                    _log.info("stín AdaFace za minutu: tváří %d | jméno r50 %d, ada %d | "
                              "shoda %d, jiné jméno %d, jen ada %d, jen r50 %d | "
                              "zahozeno %d, chyb %d", s["n"], s["r50"], s["ada"],
                              s["shoda"], s["jine"], s["jen_ada"], s["jen_r50"],
                              s["zahozeno"], s["chyba"])
                self._st = self._nove()
