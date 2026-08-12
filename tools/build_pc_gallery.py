#!/usr/bin/env python3
"""
Postaví FLOAT galerii pro PC embedder (`known_faces_pc.pkl`) z označeného sběru.

PROČ TENHLE SOUBOR EXISTUJE: galerie se 11.8. stavěla ad hoc přímo v konzoli,
takže po dalším označování ji nešlo přestavět stejným způsobem — a galerie je
to, na čem stojí každé rozhodnutí. Reprodukovatelnost je tu důležitější než
pohodlí.

VÝBĚR VZORKŮ: `--per-person` nejrozmanitějších farthest-pointem, ne prvních N.
Galerie totiž nemá být přehlídkou jedné pózy z jednoho sezení — právě proto
vznikl průběžný sběr. Počty se drží VYROVNANÉ napříč lidmi: kdo má v galerii
víc vzorků, ten při top-k skórování vyhrává i cizí dotazy (magnet efekt,
doloženo 8.8.).

⚠️ NA ČEM SE VÝSLEDEK NESMÍ MĚŘIT: na vzorcích, které jsou v galerii. Dá to
90+ % a neznamená to nic (self-match). Poctivé je držet stranou celý den nebo
sezení — viz [[within-session-numbers-lie]].

Použití:
    python3 tools/build_pc_gallery.py                 # náhled, nic nezapíše
    python3 tools/build_pc_gallery.py --write
    python3 tools/build_pc_gallery.py --write --exclude-after "2026-08-11 21:00"
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import pickle
import shutil
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

SKIP = {"neni_tvar", "_vyrazeno"}


def labeled(root="data/harvest", exclude_after=None, only_after=None):
    out: dict[str, list] = {}
    for day in sorted(glob.glob(f"{root}/*/")):
        mp = Path(day) / "meta.jsonl"
        if not mp.is_file():
            continue
        for line in open(mp, encoding="utf-8"):
            try:
                r = json.loads(line)
            except Exception:
                continue
            lab = r.get("label")
            if not lab or lab in SKIP or not r.get("file"):
                continue
            t = r.get("time", "")
            if exclude_after and t >= exclude_after:
                continue
            if only_after and t < only_after:
                continue
            r["_d"] = day
            out.setdefault(lab, []).append(r)
    return out


def embed(sub, url="http://127.0.0.1:8765/embed", bs=64):
    out = []
    for i in range(0, len(sub), bs):
        imgs = []
        for r in sub[i:i + bs]:
            im = cv2.imread(os.path.join(r["_d"], r["file"]))
            if im is None:
                continue
            imgs.append(cv2.cvtColor(im, cv2.COLOR_BGR2RGB))
        if not imgs:
            continue
        A = np.array(imgs, np.uint8)
        rr = urllib.request.urlopen(urllib.request.Request(
            url, data=A.tobytes(),
            headers={"Content-Type": "application/octet-stream"}), timeout=300)
        out.append(np.frombuffer(rr.read(), np.float32).reshape(-1, 512))
    if not out:
        return np.zeros((0, 512), np.float32)
    E = np.vstack(out)
    return E / (np.linalg.norm(E, axis=1, keepdims=True) + 1e-9)


def farthest_point(E: np.ndarray, k: int) -> list:
    """k nejrozmanitějších vzorků — pokrytí póz a světel, ne prvních k."""
    if len(E) <= k:
        return list(range(len(E)))
    sel = [int(np.argmax((E @ E.T).sum(1)))]
    while len(sel) < k:
        d = (E @ E[sel].T).max(1)
        d[sel] = 9.0
        sel.append(int(np.argmin(d)))
    return sel


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data/known_faces_pc.pkl")
    ap.add_argument("--url", default="http://127.0.0.1:8765/embed")
    ap.add_argument("--per-person", type=int, default=200)
    ap.add_argument("--exclude-after", default=None,
                    help='držet stranou vzorky od tohoto času ("2026-08-11 21:00")')
    ap.add_argument("--write", action="store_true")
    a = ap.parse_args()

    data = labeled(exclude_after=a.exclude_after)
    if not data:
        print("Žádné označené vzorky."); return 1
    print(f"označený sběr: { {k: len(v) for k, v in data.items()} }")
    if a.exclude_after:
        print(f"(vynecháno vše od {a.exclude_after})")
    if not a.write:
        print("NÁHLED — nic se nezapsalo. Spusť s --write.")
        return 0

    gal = {}
    for name, sub in data.items():
        E = embed(sub, a.url)
        if len(E) == 0:
            print(f"  {name:8s}: žádné načtené snímky, přeskakuji"); continue
        sel = farthest_point(E, a.per_person)
        gal[name] = E[sel]
        print(f"  {name:8s}: {len(E):5d} vzorků → {len(sel)} v galerii")

    outp = Path(a.out)
    if outp.exists():
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        bak = Path("data/patch_snapshots") / f"{outp.name}.pre_{stamp}.bak"
        bak.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(outp, bak)
        print(f"záloha: {bak}")
    with open(outp, "wb") as f:
        pickle.dump({k: v for k, v in gal.items()}, f)
    print(f"\nuloženo → {outp}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
