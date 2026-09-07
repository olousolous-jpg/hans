#!/usr/bin/env python3
"""HANS_OBJ_ANNOT_V1 (7.9.) — příprava snímků k RUČNÍMU POJMENOVÁNÍ objektů.

PROČ: detektor je COCO (80 tříd) a v tomhle pokoji se plete — `bed` je ve
skutečnosti gauč (potvrdil uživatel), v datech je i `banana` a historicky
`airplane`, `train`, `surfboard`. Remapping (`object_remapping` v configu)
existuje, ale zatím se plnil odhadem. Tohle dá ZMĚŘENOU pravdu: uživatel
u každého rámečku řekne, co v něm doopravdy je.

Vezme framy z `frame_sampling` (data/mereni/framy), pustí na ně detekci
objektů přes běžící Hailo server a uloží:
  data/mereni/objekty/<jméno>.jpg    snímek s ČÍSLOVANÝMI rámečky
  data/mereni/objekty/detekce.json   {soubor: [{i, class_name, conf, box}]}

Spouští se ručně; anotuje se na /objekty ve web adminu.
"""
import sys, json, glob
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import cv2
from scripts.object_client import ObjectClient
from scripts.surroundings_db import COCO_CLASSES

OUT = ROOT / "data/mereni/objekty"
BARVY = [(255, 128, 0), (0, 200, 255), (0, 255, 128), (255, 0, 200),
         (255, 255, 0), (128, 128, 255), (0, 128, 255), (200, 255, 0)]


def main(limit: int = 40):
    OUT.mkdir(parents=True, exist_ok=True)
    c = ObjectClient()
    if not c.connect():
        print("CHYBA: Hailo server nedostupný"); return 1

    framy = sorted(glob.glob(str(ROOT / "data/mereni/framy/*.jpg")))
    # rozptýlený vzorek přes celý den, ne prvních N (týž důvod jako u harvestu)
    if len(framy) > limit:
        idx = sorted({round(i * (len(framy) - 1) / (limit - 1)) for i in range(limit)})
        framy = [framy[i] for i in idx]

    vse, s_detekci = {}, 0
    for path in framy:
        img = cv2.imread(path)
        if img is None:
            continue
        h, w = img.shape[:2]
        small = cv2.resize(img, (1280, 720))
        dets = c.detect(cv2.cvtColor(small, cv2.COLOR_BGR2RGB)) or []
        if not dets:
            continue
        s_detekci += 1
        polozky, kresba = [], img.copy()
        for i, d in enumerate(sorted(dets, key=lambda x: -x["confidence"])):
            name = COCO_CLASSES.get(d["class_id"], str(d["class_id"]))
            x1, y1 = int(d["x1"] * w), int(d["y1"] * h)
            x2, y2 = int(d["x2"] * w), int(d["y2"] * h)
            barva = BARVY[i % len(BARVY)]
            cv2.rectangle(kresba, (x1, y1), (x2, y2), barva, 3)
            cv2.rectangle(kresba, (x1, max(0, y1 - 34)), (x1 + 58, y1), barva, -1)
            cv2.putText(kresba, f"#{i+1}", (x1 + 6, y1 - 8),
                        cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 0, 0), 3)
            polozky.append({"i": i + 1, "class_name": name,
                            "conf": round(float(d["confidence"]), 2),
                            "box": [x1, y1, x2, y2],
                            "barva": "#%02x%02x%02x" % (barva[2], barva[1], barva[0])})
        jmeno = Path(path).name
        cv2.imwrite(str(OUT / jmeno), kresba, [cv2.IMWRITE_JPEG_QUALITY, 88])
        vse[jmeno] = polozky

    (OUT / "detekce.json").write_text(
        json.dumps(vse, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"framů zpracováno: {len(framy)} | s detekcí: {s_detekci} | "
          f"objektů celkem: {sum(len(v) for v in vse.values())}")
    print(f"→ {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main(int(sys.argv[1]) if len(sys.argv) > 1 else 40))
