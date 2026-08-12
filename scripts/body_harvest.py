"""
BODY_HARVEST_V1 — sběr podkladů pro ReID na dálku.

🔴 ZMĚNA PŘÍSTUPU 11.8. PO PRVNÍM POHLEDU NA SKUTEČNÉ VÝŘEZY — ČTI NEJDŘÍV:

Původně se trup dopočítával geometricky z boxu obličeje (obdélník pod bradou).
Uživatel namítl: *„pokud budeme dopočítávat tělo podle pozice hlavy, má to
vůbec smysl? všechny postavy budou stejné."* Diagnostický výpis celých snímků
(`debug_full`) ukázal, že **měl pravdu, a problém je ještě větší**: lidé v téhle
místnosti hlavně SEDÍ A POLOLEŽÍ na posteli, takže se tělo netáhne dolů, ale
DO STRANY. Obdélník pod bradou pak nabírá klín, postel a pozadí — a kontrolní
výřez „pozadí vedle člověka" spadne na TÉHOŽ člověka (jeho natažené nohy).
Obojí naráz: dotaz i kontrola jsou znečištěné.

→ **Geometrické odvození trupu je ZAMÍTNUTO.** Nezkoušet znovu laděním
konstant (`torso_*`) — vada není v číslech, ale v předpokladu „tělo je pod
hlavou", který u sedící a ležící postavy neplatí.

→ **Místo toho se ukládá CELÝ SNÍMEK** (zmenšený) + box a jméno z rozpoznané
tváře. Detektor osob se pustí až POTOM, offline na PC, kde nestojí žádný Hailo
context a kde jde vyzkoušet libovolný model. Sběr se tím stává modelově
neutrálním aktivem: ať se ReID nakonec udělá jakkoli (detektor osob, segmentace,
jiný výřez), data z jediného dopoledne, kdy jsou holky doma, se nemusí sbírat
znovu. Rozhodnutí, jak to bude běžet naživo, padne AŽ podle měření.

Vypnutí: `body_harvest.enabled: false`. Snímky jsou lokální (`data/` je
v `.gitignore`, nic neodchází ven) a je to celý pokoj, ne jen obličeje —
po dokončení měření složku smazat.

--- původní záměr (platí dál) -----------------------------------------------

PROČ VŮBEC (změřeno 10.–11.8.): ve vzdálenosti, kde má tvář ~75 px, je signál
identity v embeddingu prakticky nulový — model rozlišoval VZDÁLENOST, ne lidi.
Postava má ale ve stejném záběru stovky pixelů. Pro otázku „kdo je teď v pokoji"
to stačí.

⚠️ ReID podle těla platí JEN V RÁMCI DNE — oblečení se převléká. Proto se
galerie staví každý den znovu z tváří rozpoznaných zblízka (self-labeling),
nikdy se nepřenáší přes půlnoc.

TŘI ROZHODNUTÍ, KTERÁ STOJÍ ZA VYSVĚTLENÍ:

1. **Tělo se odvozuje GEOMETRICKY z boxu obličeje, ne detektorem osob.**
   `resources/` žádný person/yolo HEF nemá a nový by stál Hailo context —
   a ten je nejdražší veličina, kterou tu máme (2 contexty 6.2 ms, 4 contexty
   53.4 ms). SCRFD přitom detekuje i tváře o ploše 0.0010, takže kotvu na dálku
   už máme zadarmo. **Cena:** kdo je zády ke kameře, nemá tvář → nemá ani tělo.
   Detektor osob by to pokryl; stavět ho až kdyby se ukázalo, že to vadí.

2. **Vlastní vzorkovací brána, ne ta z `face_harvest`.** Ta zahazuje neostré
   tváře (á minutu stovky odmítnutí za `ostrost`) — a neostré jsou právě ty
   VZDÁLENÉ, tedy přesně ty, kvůli kterým ReID stavíme. Sdílet bránu by nasbíralo
   opačné rozložení, než potřebujeme. Na trupu na ostrosti obličeje nezáleží.

3. **Ukládá se OBRÁZEK, ne embedding.** Model pro tělo ještě není vybraný
   (barevný histogram vs. OSNet na PC). Obrázek je model-agnostický → archiv
   půjde kdykoli přepočítat lepším modelem, jako u tváří.

Sbírá se i to, co Hans nepozná (`guess=None`) — právě vzdálené vzorky jsou
předmětem měření. Filtruje se až při čtení, podle `conf`.

⚠️ NÁMITKA UŽIVATELE, KTERÁ URČILA PODOBU MĚŘENÍ (11.8.): *„pokud budeme
dopočítávat tělo podle pozice hlavy, má to vůbec smysl? všechny postavy budou
stejné."* — Tvar výřezu je opravdu u všech totožný a normalizace na 128×256
navíc zahazuje výšku i proporce; identita může být JEN v pixelech (oblečení).
Horší je, že výřez z velké části obsahuje POZADÍ — a to je pro dané místo
konstantní. Naivní měření by proto mohlo vyjít skvěle, i kdyby rozlišovalo
POUZE MÍSTO V POKOJI. Tatáž past jako „model rozlišoval vzdálenost" (10.8.)
a „embedding kóduje podmínky" (10.8. večer). Proti tomu se sbírá:
  * `cx`, `cy` — poloha hlavy v záběru → měření může kotvit na jednom místě
    a ptát se z jiného (**location-holdout**);
  * `control: true` → ke každému trupu se uloží STEJNĚ VELKÝ výřez POSREDU
    vedle člověka (čisté pozadí ve stejné výšce a vzdálenosti). Když
    „rozpozná" osoby stejně dobře jako trup, čteme pokoj, ne lidi, a celý
    nápad padá. **Bez téhle kontroly se výsledku nesmí věřit.**
"""
from __future__ import annotations

import json
import logging
import os
import queue
import threading
import time
from datetime import datetime

import cv2
import numpy as np

logger = logging.getLogger("body_harvest")

# Standardní vstup ReID modelů (Market-1501 konvence) — šířka × výška.
OUT_W, OUT_H = 128, 256


class BodyHarvest:
    """Ukládá výřezy trupu odvozené z boxu obličeje. Zápis ve vlastním vlákně."""

    def __init__(self, config: dict | None = None):
        cfg = (config or {}).get("body_harvest", {})
        self.enabled = bool(cfg.get("enabled", False))
        self.root = cfg.get("root", "data/body")

        # Geometrie trupu vůči obličeji (násobky rozměru tváře).
        self._half_w = float(cfg.get("torso_half_width", 1.6))   # ← 3.2× šířka tváře
        self._top = float(cfg.get("torso_top", 0.15))            # kousek pod bradou
        self._height = float(cfg.get("torso_height", 3.0))       # ← 3× výška tváře

        self._interval = float(cfg.get("interval_s", 2.0))
        self._max_per_hour = int(cfg.get("max_per_hour", 400))
        self._min_area = float(cfg.get("min_face_area", 0.0004))
        self._max_clip = float(cfg.get("max_clip", 0.35))
        self._jpeg_q = int(cfg.get("jpeg_quality", 90))
        # Kontrolní výřez pozadí vedle člověka — viz námitka v hlavičce.
        # Po dokončení měření se vypne (zdvojnásobuje zápis).
        self._control = bool(cfg.get("control", True))
        # Diagnostika: prvních N celých snímků s vykreslenými obdélníky.
        # Bez pohledu na SKUTEČNÝ výřez se geometrie ladit nedá — čísla
        # (clip, area) vypadají v pořádku, i když obdélník míří na zeď.
        self._debug_left = int(cfg.get("debug_full", 0))

        # Režim CELÝCH SNÍMKŮ (výchozí od 11.8., viz hlavička).
        self._frames = bool(cfg.get("save_frames", True))
        self._frame_w = int(cfg.get("frame_width", 1152))
        self._frame_interval = float(cfg.get("frame_interval_s", 3.0))
        self._last_frame = 0.0
        self._cur_file = None       # ke kterému snímku se váží další tváře
        self._cur_ts = 0.0

        self._last: dict[int, float] = {}
        self._hour = -1
        self._hour_n = 0
        self._saved_today = 0
        self._day = ""
        self._rej: dict[str, int] = {}
        self._rej_t = 0.0

        self._q: queue.Queue = queue.Queue(maxsize=64)
        self._stop = threading.Event()
        self._thr = None
        if self.enabled:
            self._thr = threading.Thread(target=self._writer, daemon=True,
                                         name="BodyHarvestWriter")
            self._thr.start()
            logger.info("BodyHarvest zapnut → %s (interval %.1fs, strop %d/hod)",
                        self.root, self._interval, self._max_per_hour)

    # ── veřejné API ──────────────────────────────────────────────────────
    def offer(self, track_id, frame, box, guess=None, conf=0.0):
        """Nabídne snímek ke sběru. Vrací True, když byl zařazen do fronty.

        `box` je znormalizovaný [x1, y1, x2, y2] obličeje (konvence celé
        pipeline), `frame` hlavní proud (2304×1296).
        """
        if not self.enabled or frame is None or box is None:
            return False
        if self._frames:
            return self._offer_frame(frame, box, guess, conf, track_id)
        try:
            now = time.time()
            if not self._rate_ok(track_id, now):
                return False

            crop, clip, area, ctrl, cx, cy = self._torso(frame, box)
            if crop is None:
                return False

            if self._debug_left > 0:
                self._debug_left -= 1
                try:
                    self._dump_debug(frame, box, now)
                except Exception as e:
                    logger.debug("debug dump selhal: %s", e)

            self._hour_n += 1
            self._saved_today += 1
            try:
                self._q.put_nowait({
                    "img": crop,
                    "ctrl": ctrl,
                    "track": int(track_id),
                    "guess": guess,
                    "conf": round(float(conf or 0.0), 3),
                    "face_area": round(float(area), 5),
                    "clip": round(float(clip), 3),
                    "cx": round(float(cx), 4),
                    "cy": round(float(cy), 4),
                    "ts": now,
                })
            except queue.Full:
                # Radši vzorek zahodit než brzdit main loop, na kterém visí kamera.
                self._note("fronta_plna")
                return False
            return True
        except Exception as e:          # sběr nikdy nesmí shodit rozpoznávání
            logger.debug("offer selhal: %s", e)
            return False

    def _offer_frame(self, frame, box, guess, conf, track_id):
        """Uloží celý (zmenšený) snímek a k němu JEDEN řádek za každou tvář.

        `offer` se volá jednou za obličej, ale obrázek chceme jen jeden. Tváře
        z téhož snímku se proto navěsí na naposledy uložený soubor — při čtení
        se seskupí podle `file`. Klíčové pro zítřek: **na jednom snímku můžou
        být všechny tři najednou**, a to je přesně situace, kvůli které se ReID
        staví.
        """
        try:
            now = time.time()
            h = int(now // 3600)
            if h != self._hour:
                self._hour, self._hour_n = h, 0
            d = datetime.fromtimestamp(now).strftime("%Y-%m-%d")
            if d != self._day:
                self._day, self._saved_today = d, 0

            new_frame = (now - self._last_frame) >= self._frame_interval
            if new_frame:
                if self._hour_n >= self._max_per_hour:
                    self._note("strop_hodina")
                    return False
                self._hour_n += 1
                self._saved_today += 1
                self._last_frame = now
                dt = datetime.fromtimestamp(now)
                self._cur_file = dt.strftime("%H%M%S_") + f"{int(now * 1000) % 1000:03d}.jpg"
                self._cur_ts = now
                H, W = frame.shape[:2]
                small = cv2.resize(frame, (self._frame_w,
                                           int(H * self._frame_w / W)),
                                   interpolation=cv2.INTER_AREA)
                try:
                    self._q.put_nowait({"frame_img": small, "file": self._cur_file,
                                        "ts": now})
                except queue.Full:
                    self._note("fronta_plna")
                    return False
            elif self._cur_file is None or (now - self._cur_ts) > 0.5:
                # Mezi snímky: tvář nemá k čemu patřit, zahodit.
                self._note("interval")
                return False

            x1, y1, x2, y2 = box
            try:
                self._q.put_nowait({
                    "face_of": self._cur_file, "ts": self._cur_ts,
                    "track": int(track_id), "guess": guess,
                    "conf": round(float(conf or 0.0), 3),
                    "box": [round(float(v), 4) for v in (x1, y1, x2, y2)],
                    "face_area": round(float((x2 - x1) * (y2 - y1)), 5),
                })
            except queue.Full:
                self._note("fronta_plna")
            return True
        except Exception as e:
            logger.debug("offer_frame selhal: %s", e)
            return False

    def stop(self):
        self._stop.set()
        if self._thr is not None:
            self._q.put(None)

    def stats(self) -> dict:
        return {"dnes": self._saved_today, "tuto_hodinu": self._hour_n,
                "fronta": self._q.qsize()}

    # ── vnitřek ──────────────────────────────────────────────────────────
    def _rate_ok(self, track_id, now) -> bool:
        h = int(now // 3600)
        if h != self._hour:
            self._hour, self._hour_n = h, 0
        d = datetime.fromtimestamp(now).strftime("%Y-%m-%d")
        if d != self._day:
            self._day, self._saved_today = d, 0
        if self._hour_n >= self._max_per_hour:
            self._note("strop_hodina")
            return False
        if now - self._last.get(track_id, 0.0) < self._interval:
            self._note("interval")
            return False
        self._last[track_id] = now
        if len(self._last) > 512:       # ať mapa neroste donekonečna
            cut = now - 300
            self._last = {k: v for k, v in self._last.items() if v > cut}
        return True

    def _torso(self, frame, box):
        """Výřez trupu pod obličejem.

        Vrací (crop, podíl_useknutí, plocha_tváře, kontrolní_pozadí, cx, cy).
        """
        H, W = frame.shape[:2]
        x1, y1, x2, y2 = box
        fw = (x2 - x1) * W
        fh = (y2 - y1) * H
        area = (x2 - x1) * (y2 - y1)
        ncx, ncy = (x1 + x2) * 0.5, (y1 + y2) * 0.5
        if area < self._min_area or fw < 12 or fh < 12:
            self._note("plocha")
            return None, 0.0, area, None, ncx, ncy

        cx = (x1 + x2) * 0.5 * W
        chin = y2 * H
        # zamýšlený obdélník (může přesahovat mimo snímek)
        wx1 = cx - self._half_w * fw
        wx2 = cx + self._half_w * fw
        wy1 = chin + self._top * fh
        wy2 = chin + (self._top + self._height) * fh
        want = max(1.0, (wx2 - wx1) * (wy2 - wy1))

        ix1, iy1 = int(max(0, wx1)), int(max(0, wy1))
        ix2, iy2 = int(min(W, wx2)), int(min(H, wy2))
        if ix2 - ix1 < 16 or iy2 - iy1 < 32:
            self._note("mimo_snimek")
            return None, 1.0, area, None, ncx, ncy

        clip = 1.0 - ((ix2 - ix1) * (iy2 - iy1)) / want
        if clip > self._max_clip:
            # Useknutý trup je pro ReID zavádějící: kus obrazu chybí a model
            # by se učil na tvaru výřezu, ne na oblečení.
            self._note("useknuty")
            return None, clip, area, None, ncx, ncy

        patch = frame[iy1:iy2, ix1:ix2]
        if patch.size == 0:
            return None, clip, area, None, ncx, ncy
        crop = cv2.resize(patch, (OUT_W, OUT_H), interpolation=cv2.INTER_AREA)
        return crop, clip, area, self._control_patch(frame, wx1, wx2, wy1, wy2), ncx, ncy

    def _control_patch(self, frame, wx1, wx2, wy1, wy2):
        """Stejně velký výřez POZADÍ vedle člověka — negativní kontrola.

        Kdyby „poznával" osoby stejně dobře jako trup, nečteme oblečení, ale
        pokoj. Bere se ta strana, která se do záběru vejde celá; když ani jedna,
        kontrola pro tenhle vzorek není (raději nic než napůl oříznuté pozadí).
        """
        if not self._control:
            return None
        H, W = frame.shape[:2]
        bw = wx2 - wx1
        for shift in (2.0 * bw, -2.0 * bw):
            sx1, sx2 = wx1 + shift, wx2 + shift
            if sx1 < 0 or sx2 > W:
                continue
            iy1, iy2 = int(max(0, wy1)), int(min(H, wy2))
            patch = frame[iy1:iy2, int(sx1):int(sx2)]
            if patch.size == 0 or patch.shape[0] < 32 or patch.shape[1] < 16:
                continue
            return cv2.resize(patch, (OUT_W, OUT_H), interpolation=cv2.INTER_AREA)
        return None

    def _dump_debug(self, frame, box, now):
        """Celý snímek s obdélníky: zelený obličej, žlutý trup, modré pozadí."""
        H, W = frame.shape[:2]
        x1, y1, x2, y2 = box
        fw, fh = (x2 - x1) * W, (y2 - y1) * H
        cx, chin = (x1 + x2) * 0.5 * W, y2 * H
        wx1 = cx - self._half_w * fw
        wx2 = cx + self._half_w * fw
        wy1 = chin + self._top * fh
        wy2 = chin + (self._top + self._height) * fh

        img = frame.copy()
        cv2.rectangle(img, (int(x1 * W), int(y1 * H)), (int(x2 * W), int(y2 * H)),
                      (0, 255, 0), 3)
        cv2.rectangle(img, (int(wx1), int(wy1)), (int(wx2), int(wy2)),
                      (0, 255, 255), 3)
        for shift in (2.0 * (wx2 - wx1), -2.0 * (wx2 - wx1)):
            sx1, sx2 = wx1 + shift, wx2 + shift
            if 0 <= sx1 and sx2 <= W:
                cv2.rectangle(img, (int(sx1), int(wy1)), (int(sx2), int(wy2)),
                              (255, 128, 0), 3)
                break
        d = os.path.join(self.root, "_debug")
        os.makedirs(d, exist_ok=True)
        small = cv2.resize(img, (1152, 648), interpolation=cv2.INTER_AREA)
        cv2.imwrite(os.path.join(
            d, datetime.fromtimestamp(now).strftime("%H%M%S_%f") + ".jpg"),
            small, [cv2.IMWRITE_JPEG_QUALITY, 88])

    def _note(self, why):
        self._rej[why] = self._rej.get(why, 0) + 1
        now = time.time()
        if now - self._rej_t > 60.0:
            if self._rej:
                logger.info("sběr těl — odmítnuto za minutu: %s (uloženo dnes %d)",
                            self._rej, self._saved_today)
                self._rej = {}
            self._rej_t = now

    def _writer(self):
        while not self._stop.is_set():
            try:
                item = self._q.get(timeout=1.0)
            except queue.Empty:
                continue
            if item is None:
                break
            try:
                self._write(item)
            except Exception as e:
                logger.debug("zápis selhal: %s", e)

    def _write(self, it):
        if "frame_img" in it or "face_of" in it:
            return self._write_frame(it)
        dt = datetime.fromtimestamp(it["ts"])
        day = dt.strftime("%Y-%m-%d")
        d = os.path.join(self.root, day)
        os.makedirs(d, exist_ok=True)
        stem = f"{dt.strftime('%H%M%S')}_t{it['track']}_{int(it['ts'] * 100) % 100:02d}"
        name = stem + ".jpg"
        q = [cv2.IMWRITE_JPEG_QUALITY, self._jpeg_q]
        cv2.imwrite(os.path.join(d, name), it["img"], q)
        ctrl_name = None
        if it.get("ctrl") is not None:
            ctrl_name = stem + "_bg.jpg"
            cv2.imwrite(os.path.join(d, ctrl_name), it["ctrl"], q)
        rec = {"time": dt.strftime("%Y-%m-%d %H:%M:%S"), "ts": round(it["ts"], 2),
               "track": it["track"], "guess": it["guess"], "conf": it["conf"],
               "face_area": it["face_area"], "clip": it["clip"],
               "cx": it["cx"], "cy": it["cy"], "file": name, "bg": ctrl_name}
        with open(os.path.join(d, "meta.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


    def _write_frame(self, it):
        dt = datetime.fromtimestamp(it["ts"])
        d = os.path.join(self.root, dt.strftime("%Y-%m-%d"))
        os.makedirs(d, exist_ok=True)
        if "frame_img" in it:
            cv2.imwrite(os.path.join(d, it["file"]), it["frame_img"],
                        [cv2.IMWRITE_JPEG_QUALITY, self._jpeg_q])
            return
        rec = {"time": dt.strftime("%Y-%m-%d %H:%M:%S"), "ts": round(it["ts"], 2),
               "file": it["face_of"], "track": it["track"], "guess": it["guess"],
               "conf": it["conf"], "box": it["box"], "face_area": it["face_area"]}
        with open(os.path.join(d, "faces.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


# ── čtení pro měření a stavbu galerie ────────────────────────────────────
def load_day(day: str | None = None, root="data/body") -> list:
    """Načte meta jednoho dne. Bez `day` bere dnešek."""
    day = day or datetime.now().strftime("%Y-%m-%d")
    p = os.path.join(root, day, "meta.jsonl")
    if not os.path.isfile(p):
        return []
    out = []
    for line in open(p, encoding="utf-8"):
        try:
            r = json.loads(line)
        except Exception:
            continue
        r["_path"] = os.path.join(root, day, r["file"])
        out.append(r)
    return out


def coverage(day: str | None = None, root="data/body") -> dict:
    """Kolik vzorků na osobu a jak jsou rozložené podle vzdálenosti.

    `face_area` je proxy vzdálenosti: velká plocha = blízko. Kotvy potřebujeme
    zblízka (tam tvář funguje), dotazy naopak z dálky — když v některém pásmu
    nic není, měření nemá o co se opřít.
    """
    rows = load_day(day, root)
    out: dict = {}
    for r in rows:
        n = r.get("guess") or "?"
        b = out.setdefault(n, {"n": 0, "blizko": 0, "stredne": 0, "daleko": 0,
                               "conf_max": 0.0})
        b["n"] += 1
        a = r.get("face_area", 0.0)
        b["blizko" if a >= 0.004 else "stredne" if a >= 0.0015 else "daleko"] += 1
        b["conf_max"] = max(b["conf_max"], r.get("conf") or 0.0)
    return out
