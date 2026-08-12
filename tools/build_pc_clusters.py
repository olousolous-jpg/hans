#!/usr/bin/env python3
"""
Postaví PÓZOVÉ CENTROIDY nad float embeddingy z PC — druhý hlas rozhodování.

PROČ DVA HLASY A NE TŘI (změřeno 11.8.2026, den-holdout: galerie 9.+10.8.,
dotazy 11.8.):
    1 hlas  (jen PC)                67.4 % správně / 5.8 % záměn
    2 hlasy (PC + float cluster)    66.8 % / 3.5 %   ← nejlepší poměr
    2 hlasy (PC + Hailo)            67.1 % / 4.5 %
    3 hlasy (PC + cluster + Hailo)  66.8 % / 3.2 %   ← +0.3 b. za složitost navíc
Druhý hlas úspěšnost NEZVEDNE, ale **srazí záměny o 40 %** — a špatné jméno
v deníku je horší než „nevím". Třetí hlas už se nevyplatí.

⚠️ Starý cluster (`known_faces_cluster.pkl`) stojí na HAILO int8 embeddingech,
které lidi rozlišit neumí — proto musí většinou abstinovat a jako hlas je slabý.
Tenhle staví nad TÝMIŽ float embeddingy, které dělají finální rozhodnutí, ale
agreguje je jinak (pózové centroidy místo top-k), takže přináší jiný pohled.

Použití:
    python3 tools/build_pc_clusters.py            # náhled
    python3 tools/build_pc_clusters.py --write
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


def labeled(root="data/harvest"):
    out = {}
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
            if lab and lab not in SKIP and r.get("file"):
                r["_d"] = day
                out.setdefault(lab, []).append(r)
    return out


def embed(sub, url, bs=64):
    out = []
    for i in range(0, len(sub), bs):
        A = np.array([cv2.cvtColor(cv2.imread(os.path.join(r["_d"], r["file"])),
                                   cv2.COLOR_BGR2RGB) for r in sub[i:i + bs]], np.uint8)
        rr = urllib.request.urlopen(urllib.request.Request(
            url, data=A.tobytes(),
            headers={"Content-Type": "application/octet-stream"}), timeout=300)
        out.append(np.frombuffer(rr.read(), np.float32).reshape(-1, 512))
    return np.vstack(out)


def kmeans_sphere(E, k, iters=15):
    """k-means na jednotkové kouli. Semínka farthest-pointem, ať pokryjí pózy."""
    k = min(k, len(E))
    idx = [int(np.argmax((E @ E.T).sum(1)))]
    while len(idx) < k:
        d = (E @ E[idx].T).max(1)
        d[idx] = 9.0
        idx.append(int(np.argmin(d)))
    C = E[idx].copy()
    for _ in range(iters):
        a = np.argmax(E @ C.T, axis=1)
        for j in range(k):
            m = a == j
            if m.sum():
                v = E[m].mean(0)
                C[j] = v / (np.linalg.norm(v) + 1e-9)
    return C


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data/known_faces_pc_clusters.pkl")
    ap.add_argument("--url", default="http://127.0.0.1:8765/embed")
    ap.add_argument("--per-person", type=int, default=400,
                    help="kolik vzorků poslat na PC (z nich se počítají centroidy)")
    ap.add_argument("--clusters", type=int, default=6)
    ap.add_argument("--write", action="store_true")
    a = ap.parse_args()

    data = labeled()
    if not data:
        print("Žádné označené vzorky."); return 1
    print(f"označený sběr: { {k: len(v) for k, v in data.items()} }")
    if not a.write:
        print("NÁHLED — nic se nezapsalo. Spusť s --write.")
        return 0

    cent = {}
    for name, sub in data.items():
        if len(sub) > a.per_person:
            sub = [sub[i] for i in np.linspace(0, len(sub) - 1, a.per_person).astype(int)]
        E = embed(sub, a.url)
        E = E / (np.linalg.norm(E, axis=1, keepdims=True) + 1e-9)
        C = kmeans_sphere(E, a.clusters)
        cent[name] = C
        # jak dobře centroidy pokrývají: průměrná shoda vzorku s nejbližším
        cov = float(np.mean((E @ C.T).max(1)))
        print(f"  {name:8s}: {len(E):4d} vzorků → {len(C)} centroidů, pokrytí {cov:.3f}")

    outp = Path(a.out)
    if outp.exists():
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        bak = Path("data/patch_snapshots") / f"{outp.name}.pre_{stamp}.bak"
        bak.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(outp, bak)
        print(f"záloha: {bak}")
    # GALLERY_PROVENANCE_V1 — bez tohohle se za čtyři měsíce nepozná,
    # že galerie stojí na letním světle a proto v zimě selhává.
    from scripts.face_harvest import source_meta
    allrows = [r for sub in data.values() for r in sub]
    out_obj = dict(cent)
    out_obj["_meta"] = source_meta(allrows, "build_pc_clusters.py", a.per_person)
    print("  původ: vzorky %s–%s, cfg %s"
          % (out_obj["_meta"]["vzorky_od"], out_obj["_meta"]["vzorky_do"],
             ",".join(out_obj["_meta"]["cfg"])[:40]))
    with open(outp, "wb") as f:
        pickle.dump(out_obj, f)
    print(f"\nuloženo → {outp}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
