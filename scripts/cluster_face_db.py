"""
Cluster-based face database.
Each enrolled person has up to max_clusters centroids representing
different appearance conditions (lighting, angle, glasses, ...).
Matching uses distance to the NEAREST centroid, not a single average.

Drop-in companion to database_manager.py — handles its own pickle file.
Auto-migrates the old flat {name: [embeddings]} format on first load.
"""
import pickle
import threading
import time
from pathlib import Path

import numpy as np


# ── internal helpers ──────────────────────────────────────────────────

def _cosine_dist(a, b):
    a = a / (np.linalg.norm(a) + 1e-8)
    b = b / (np.linalg.norm(b) + 1e-8)
    return float(1.0 - np.dot(a, b))


class _Cluster:
    def __init__(self, seed, max_members=30):
        self.centroid    = np.array(seed, dtype=np.float32)
        self.members     = [self.centroid.copy()]
        self.max_members = max_members
        self.created_at  = time.time()
        self.updated_at  = time.time()

    def add(self, emb):
        self.members.append(np.array(emb, dtype=np.float32))
        if len(self.members) > self.max_members:
            self.members.pop(0)
        self.centroid   = np.mean(self.members, axis=0)
        self.updated_at = time.time()

    def dist(self, emb):
        return _cosine_dist(self.centroid, emb)


class _Person:
    def __init__(self, name, max_clusters=6):
        self.name         = name
        self.clusters     = []
        self.max_clusters = max_clusters
        self.n_samples    = 0

    def add(self, emb, cluster_thresh=0.25):
        emb = np.array(emb, dtype=np.float32)
        if not self.clusters:
            self.clusters.append(_Cluster(emb))
            self.n_samples += 1
            return

        dists      = [c.dist(emb) for c in self.clusters]
        min_dist   = min(dists)
        nearest    = int(np.argmin(dists))

        if min_dist < cluster_thresh:
            # fits into existing cluster
            self.clusters[nearest].add(emb)
        elif len(self.clusters) < self.max_clusters:
            # new condition, new cluster
            self.clusters.append(_Cluster(emb))
        else:
            # replace the stalest cluster
            oldest = min(range(len(self.clusters)),
                         key=lambda i: self.clusters[i].updated_at)
            self.clusters[oldest] = _Cluster(emb)

        self.n_samples += 1

    def match(self, emb):
        """Return distance to nearest centroid (0 = perfect match)."""
        if not self.clusters:
            return 1.0
        return min(c.dist(emb) for c in self.clusters)


# ── public API ────────────────────────────────────────────────────────

class ClusterFaceDB:
    """
    Usage:
        db = ClusterFaceDB("data/known_faces_cluster.pkl")
        db.add("Jan", embedding)
        name, dist = db.match(embedding)   # dist < threshold => known
        db.save()
    """

    def __init__(self, db_path,
                 max_clusters=6,
                 cluster_thresh=0.25,
                 match_thresh=0.40):
        self.db_path       = Path(db_path)
        self.max_clusters  = max_clusters
        self.cluster_thresh = cluster_thresh
        self.match_thresh  = match_thresh
        self._lock         = threading.Lock()
        self._persons: dict[str, _Person] = {}
        self.load()

    # ── persistence ───────────────────────────────────────────────────

    def load(self):
        if not self.db_path.exists():
            return
        with open(self.db_path, "rb") as f:
            data = pickle.load(f)
        if isinstance(data, dict) and all(type(v).__name__ == "_Person" for v in data.values()):
            self._persons = data
            print(f"[ClusterFaceDB] loaded {len(self._persons)} persons")
        else:
            self._migrate(data)

    def _migrate(self, old):
        """Migrate flat {name: embedding | [embeddings]} format."""
        for name, val in old.items():
            embeddings = val if isinstance(val, list) else [val]
            rec = _Person(name, self.max_clusters)
            for e in embeddings:
                rec.add(e, self.cluster_thresh)
            self._persons[name] = rec
        print(f"[ClusterFaceDB] migrated {len(self._persons)} persons — saving")
        self.save()

    def save(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.db_path.with_suffix(".tmp")
        with self._lock:
            with open(tmp, "wb") as f:
                pickle.dump(self._persons, f)
        tmp.replace(self.db_path)

    # ── write ─────────────────────────────────────────────────────────

    def add(self, name: str, embedding):
        with self._lock:
            if name not in self._persons:
                self._persons[name] = _Person(name, self.max_clusters)
            self._persons[name].add(embedding, self.cluster_thresh)

    def remove(self, name: str) -> bool:
        with self._lock:
            return bool(self._persons.pop(name, None))

    # ── read ──────────────────────────────────────────────────────────

    def match(self, embedding) -> tuple[str, float]:
        """
        Returns (name, distance).
        name == "unknown" if best distance >= match_thresh.
        """
        with self._lock:
            if not self._persons:
                return "unknown", 1.0
            emb = np.array(embedding, dtype=np.float32)
            norm = np.linalg.norm(emb)
            if norm > 0:
                emb /= norm
            results = {n: p.match(emb) for n, p in self._persons.items()}
        best  = min(results, key=results.get)
        dist  = results[best]
        return (best, dist) if dist < self.match_thresh else ("unknown", dist)

    def match_with_margin(self, embedding) -> tuple[str, float, float]:
        """CLUSTER_RESCUE_V1 — jako match(), ale vrací i margin = (dist k 2.
        nejbližší OSOBĚ − dist k nejbližší). Velký margin = jednoznačně jedna
        osoba (pojistka proti záměně při rescue). name='unknown' když best
        dist >= match_thresh."""
        with self._lock:
            if not self._persons:
                return "unknown", 1.0, 0.0
            emb = np.array(embedding, dtype=np.float32)
            norm = np.linalg.norm(emb)
            if norm > 0:
                emb /= norm
            results = {n: p.match(emb) for n, p in self._persons.items()}
        ordered = sorted(results.items(), key=lambda x: x[1])
        best, dist = ordered[0]
        second_dist = ordered[1][1] if len(ordered) > 1 else 1.0
        margin = second_dist - dist
        name = best if dist < self.match_thresh else "unknown"
        return name, dist, margin

    def info(self) -> dict:
        with self._lock:
            return {
                n: {"clusters": len(p.clusters), "samples": p.n_samples}
                for n, p in self._persons.items()
            }