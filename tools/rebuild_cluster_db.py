#!/usr/bin/env python3
"""
Přestaví cluster databázi z OZNAČENÉHO SBĚRU (`data/harvest`).

PROČ (naměřeno 11.8.2026): cluster hlas byl postavený z Hailo embeddingů
z doby PŘED opravou landmarků a mýlil se skoro pokaždé — když byl doma jen
jeden člověk, tvrdil 1545x a 903x cizí jména (správně jen 37x). Nekazil sice
výsledek (arc ho přebije), ale byl to mrtvý balast v hlasování.

Struktura cíle (`cluster_face_db`): každý člověk má max `max_clusters` shluků
po `max_members` vzorcích; při překročení se nejstarší vzorek zahodí. Krmit
tisíci embeddingy tedy nemá smysl — vejde se max_clusters × max_members.
Proto se vybírá `--per-person` nejrozmanitějších vzorků (farthest-point).

Použití:
    python3 tools/rebuild_cluster_db.py            # náhled, nic nezapíše
    python3 tools/rebuild_cluster_db.py --write    # přestaví a uloží
"""
from __future__ import annotations

import argparse
import base64
import glob
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DEFAULT_DB = "data/known_faces_cluster.pkl"
SKIP_LABELS = {"neni_tvar", "_vyrazeno"}


def load_labeled(root="data/harvest") -> dict:
    """{jméno: (N,512) L2-normalizované Hailo embeddingy} z označeného sběru."""
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
            if not lab or lab in SKIP_LABELS:
                continue
            e = np.frombuffer(base64.b64decode(r["emb"]), np.float16).astype(np.float32)
            out.setdefault(lab, []).append(e)
    res = {}
    for k, v in out.items():
        A = np.array(v, np.float32)
        res[k] = A / (np.linalg.norm(A, axis=1, keepdims=True) + 1e-9)
    return res


def farthest_point(E: np.ndarray, k: int) -> list:
    """Vybere k nejrozmanitějších vzorků — pokrytí póz, ne prvních k."""
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
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--harvest", default="data/harvest")
    ap.add_argument("--per-person", type=int, default=180,
                    help="max_clusters(6) × max_members(30) = 180; víc se stejně nevejde")
    ap.add_argument("--write", action="store_true", help="bez toho jen náhled")
    a = ap.parse_args()

    data = load_labeled(a.harvest)
    if not data:
        print("V sběru nejsou žádné označené vzorky."); return 1

    cfg = json.load(open("config.json", encoding="utf-8"))
    rt = cfg.get("recognition_tuning", {})
    print(f"označený sběr: { {k: len(v) for k, v in data.items()} }")
    print(f"vyberu {a.per_person}/osobu (farthest-point), "
          f"cluster_thresh={rt.get('cluster_thresh', 0.25)}, "
          f"max_clusters={rt.get('max_clusters', 6)}\n")

    if not a.write:
        print("NÁHLED — nic se nezapsalo. Spusť s --write.")
        return 0

    from scripts.cluster_face_db import ClusterFaceDB

    dbp = Path(a.db)
    if dbp.exists():
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        bak = Path("data/patch_snapshots") / f"{dbp.name}.pre_rebuild_{stamp}.bak"
        bak.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(dbp, bak)
        print(f"záloha: {bak}")
        dbp.unlink()          # od nuly, ať nezůstanou staré shluky

    db = ClusterFaceDB(str(dbp),
                       max_clusters=int(rt.get("max_clusters", 6)),
                       cluster_thresh=float(rt.get("cluster_thresh", 0.25)),
                       match_thresh=float(rt.get("match_thresh", 0.40)))
    for name, E in data.items():
        sel = farthest_point(E, a.per_person)
        for i in sel:
            db.add(name, E[i])
        print(f"  {name:8s}: {len(sel)} vzorků")
    db.save()
    print(f"\nuloženo → {dbp}")
    print("info:", db.info())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
