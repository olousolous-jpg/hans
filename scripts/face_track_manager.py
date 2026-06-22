"""
Per-track face recognition with quality-weighted voting.
Replaces per-frame EMA with accumulated evidence over a track lifetime.
"""
import time
import numpy as np
import cv2


class FaceTrack:
    def __init__(self, track_id, max_embeddings=20, decision_after=5):
        self.track_id       = track_id
        self.embeddings     = []
        self.quality_scores = []
        self.crops          = []          # keep for PC upload
        self.last_seen      = time.time()
        self.decision       = None
        self.decision_conf  = 0.0
        self.frame_count    = 0
        self.max_embeddings = max_embeddings
        self.decision_after = decision_after

    # ------------------------------------------------------------------
    def add(self, embedding, crop):
        q = self._quality(crop)
        self.embeddings.append(np.array(embedding, dtype=np.float32))
        self.quality_scores.append(q)
        self.crops.append(crop)
        self.frame_count += 1
        self.last_seen = time.time()

        # keep only the best max_embeddings
        if len(self.embeddings) > self.max_embeddings:
            worst = int(np.argmin(self.quality_scores))
            self.embeddings.pop(worst)
            self.quality_scores.pop(worst)
            self.crops.pop(worst)

    def _quality(self, crop):
        if crop is None or crop.size == 0:
            return 0.0
        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY) if crop.ndim == 3 else crop

        # sharpness (Laplacian variance, capped at 1.0)
        sharpness = min(cv2.Laplacian(gray, cv2.CV_64F).var() / 500.0, 1.0)

        # brightness (penalize too dark / too bright)
        b = gray.mean() / 255.0
        brightness = max(0.0, 1.0 - abs(b - 0.5) * 2)

        # face size vs ArcFace native 112x112
        size = min(gray.shape[0] * gray.shape[1] / (112 * 112), 1.0)

        return 0.5 * sharpness + 0.3 * brightness + 0.2 * size

    # ------------------------------------------------------------------
    def weighted_embedding(self):
        """Return quality-weighted mean embedding — the "best guess" for this track."""
        if not self.embeddings:
            return None
        w = np.array(self.quality_scores, dtype=np.float32)
        if w.sum() < 1e-8:          # všechny crop=None → plain mean
            w = np.ones(len(w), dtype=np.float32)
        w /= w.sum()
        emb = np.average(self.embeddings, weights=w, axis=0)
        norm = np.linalg.norm(emb)
        return emb / norm if norm > 0 else emb

    def best_crop(self):
        if not self.crops:
            return None
        return self.crops[int(np.argmax(self.quality_scores))]

    def ready(self):
        return self.frame_count >= self.decision_after

    def set_decision(self, name, confidence):
        self.decision      = name
        self.decision_conf = confidence


# ======================================================================
class FaceTrackManager:
    def __init__(self, stale_timeout=2.0, decision_after=5, max_embeddings=20):
        self.tracks        = {}
        self.stale_timeout = stale_timeout
        self.decision_after = decision_after
        self.max_embeddings = max_embeddings

    def update(self, track_id, embedding, crop) -> FaceTrack:
        if track_id not in self.tracks:
            self.tracks[track_id] = FaceTrack(
                track_id,
                max_embeddings=self.max_embeddings,
                decision_after=self.decision_after,
            )
        t = self.tracks[track_id]
        t.add(embedding, crop)
        return t

    def get(self, track_id) -> FaceTrack | None:
        return self.tracks.get(track_id)

    def cleanup_stale(self) -> list:
        now   = time.time()
        stale = [tid for tid, t in self.tracks.items()
                 if now - t.last_seen > self.stale_timeout]
        for tid in stale:
            del self.tracks[tid]
        return stale
