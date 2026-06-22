"""
Face Database Module
Wraps DatabaseManager with ArcFace-specific matching logic.

Improvements over original:
  - Per-person mean embedding computed from all samples (stronger, noise-resistant)
  - Individual sample gallery kept as fallback for small enrollment sets
  - Quality floor: low-norm embeddings rejected at add() time
  - Inter-person margin check: match rejected if top-2 scores are too close
  - Sanitises legacy 128-d dlib embeddings on load
"""

import numpy as np
from scripts.hailo_client import ARCFACE_DIM

# Minimum cosine similarity margin between 1st and 2nd best match.
# If the gap is smaller than this the result is treated as ambiguous → Unknown.
_MARGIN = 0.08

# Minimum L2 norm accepted during enrollment.  Embeddings below this are
# likely from a blank / occluded crop and should be discarded.
_MIN_NORM = 0.4


class FaceDB:
    """
    ArcFace embedding store backed by DatabaseManager.

    Storage layout inside DatabaseManager.known_faces:
      name → list of ndarray(512,)   (raw per-sample embeddings)

    At match time a mean embedding is derived on-the-fly from all samples,
    then compared against the query with cosine similarity.
    """

    def __init__(self, database_manager=None, config=None):
        self.db_mgr = database_manager
        self.config  = config or {}

        rt = self.config.get("recognition_tuning", {})
        self._arcface_thresh = float(rt.get("arcface_thresh", 0.55))
        self._margin         = float(rt.get("margin",         _MARGIN))
        self._min_norm       = float(rt.get("min_norm",        _MIN_NORM))
        self._debug          = self.config.get("debug", False)

        # Cache: {name: (normalised_matrix, mean_vector)} invalidated on add/reload
        self._emb_cache: dict = {}

        if self.db_mgr:
            self._sanitise_dimensions()
            count = len(self.db_mgr.known_faces)
            print(f"[FaceDB] {count} face(s) loaded")
        else:
            print("[FaceDB] WARNING: No DatabaseManager — faces will not persist")

    # ── Setup ─────────────────────────────────────────────────────────────

    def _sanitise_dimensions(self):
        """Remove legacy 128-d (dlib) embeddings incompatible with ArcFace."""
        if not self.db_mgr:
            return
        to_remove = []
        for name, embeddings in list(self.db_mgr.known_faces.items()):
            lst  = embeddings if isinstance(embeddings, list) else [embeddings]
            good = [e for e in lst
                    if hasattr(e, '__len__') and len(e) == ARCFACE_DIM]
            bad  = len(lst) - len(good)
            if bad:
                if good:
                    self.db_mgr.known_faces[name] = good
                    print(f"[FaceDB] '{name}': removed {bad} wrong-dim embedding(s)")
                else:
                    to_remove.append(name)
                    print(f"[FaceDB] '{name}': all embeddings wrong dim — re-enroll")
        for name in to_remove:
            del self.db_mgr.known_faces[name]
        if to_remove:
            self.db_mgr.save_face_database()
            print(f"[FaceDB] Removed {len(to_remove)} unusable face(s).")

    # ── Public API ────────────────────────────────────────────────────────

    def add(self, name: str, embedding: np.ndarray,
            force: bool = False) -> bool:  # force_enroll_patch
        """# diversity_check_patch
        Store one enrollment embedding.
        Rejects: low norm, duplicates, over max_samples limit.
        Returns True if accepted, False if rejected.
        """
        emb  = np.array(embedding, dtype=np.float32)
        norm = float(np.linalg.norm(emb))
        if norm < self._min_norm:
            if self._debug:
                print(f"[FaceDB] '{name}': rejected low-quality "
                      f"(norm={norm:.3f})")
            return False
        if not self.db_mgr:
            print("[FaceDB] Cannot save — no DatabaseManager")
            return False
        # diversity_fixed
        existing = self.db_mgr.known_faces.get(name, [])
        if not isinstance(existing, list):
            existing = [existing]
        # Filtruj jen platné 512-d embeddingy
        existing = [e for e in existing
                    if hasattr(e, "__len__") and len(e) == ARCFACE_DIM]
        if not force:  # force_enroll_patch
            rt = self.config.get("recognition_tuning", {})
            max_s   = int(rt.get("max_samples_per_person", 50))
            min_div = float(rt.get("min_sample_diversity", 0.02))
            # Vždy přijmi první vzorek
            if len(existing) == 0:
                pass  # první vzorek — přijmi vždy
            elif len(existing) >= max_s:
                if self._debug:
                    print(f"[FaceDB] '{name}': max samples ({max_s}) reached")
                return False
            elif len(existing) >= 3:
                # Diversity check — jen když máme alespoň 3 vzorky
                emb_n = emb / (norm + 1e-6)
                mat   = np.array(existing, dtype=np.float32)
                norms = np.linalg.norm(mat, axis=1, keepdims=True)
                mat_n = mat / (norms + 1e-6)
                sims  = mat_n @ emb_n
                max_sim = float(np.max(sims))
                if max_sim > 1.0 - min_div:
                    if self._debug:
                        print(f"[FaceDB] '{name}': duplicate rejected "
                              f"(sim={max_sim:.3f})")
                    return False
        self.db_mgr.add_face_encoding(name, emb)
        self._emb_cache.pop(name, None)  # invalidate cache
        if self._debug:
            n = len(self.db_mgr.known_faces.get(name, []))
            print(f"[FaceDB] '{name}': {n} sample(s) stored")
        return True

    def remove(self, name: str):
        if self.db_mgr:
            self.db_mgr.remove_face(name)
            print(f"[FaceDB] Removed '{name}'")
        else:
            print("[FaceDB] Cannot delete — no DatabaseManager")

    def identify(self, embedding: np.ndarray) -> tuple[str, float]:
        """
        Return (name, confidence 0-1) or ('Unknown', 0.0).

        Steps:
          1. Normalise query embedding.
          2. For each person compute cosine sim against their mean embedding
             AND against each individual sample (gallery).  Take the max.
          3. Require top score ≥ threshold AND margin over second-best ≥ _margin.
        """
        if not self.db_mgr:
            return "Unknown", 0.0
        known = self.db_mgr.get_all_encodings()
        if not known:
            return "Unknown", 0.0
        return self._match(embedding, known)

    def normalize_person(self, name: str) -> int:
        """# normalize_person_patch
        Deduplikuje vzorky pro danou osobu.
        Ponechá max max_samples_per_person diverzních vzorků.
        Vrátí počet odstraněných vzorků.
        """
        if not self.db_mgr:
            return 0
        existing = self.db_mgr.known_faces.get(name, [])
        if not isinstance(existing, list):
            existing = [existing]
        existing = [e for e in existing
                    if hasattr(e, '__len__') and len(e) == ARCFACE_DIM]
        if len(existing) <= 1:
            return 0
        rt      = self.config.get('recognition_tuning', {})
        max_s   = int(rt.get('max_samples_per_person', 100))
        min_div = float(rt.get('min_sample_diversity', 0.02))
        # Seřaď podle normy — nejkvalitnější první
        norms   = [float(np.linalg.norm(e)) for e in existing]
        sorted_embs = [e for _, e in
                       sorted(zip(norms, existing),
                              key=lambda x: x[0], reverse=True)]
        # Greedy deduplikace
        kept = [sorted_embs[0]]
        for emb in sorted_embs[1:]:
            if len(kept) >= max_s:
                break
            n = float(np.linalg.norm(emb))
            if n < self._min_norm:
                continue
            emb_n = emb / (n + 1e-6)
            mat   = np.array(kept, dtype=np.float32)
            nrms  = np.linalg.norm(mat, axis=1, keepdims=True)
            mat_n = mat / (nrms + 1e-6)
            sims  = mat_n @ emb_n
            if float(np.max(sims)) <= 1.0 - min_div:
                kept.append(emb)
        removed = len(existing) - len(kept)
        if removed > 0:
            self.db_mgr.known_faces[name] = kept
            self.db_mgr.save_face_database()
            print(f'[FaceDB] normalize {name}: '
                  f'{len(existing)} → {len(kept)} vzorků '
                  f'(odstraněno {removed})')
        return removed

    def reload(self):
        """# reload_patch
        Znovu načte DB z disku. Volat po každém enrollmentu.
        """
        if self.db_mgr:
            self.db_mgr.known_faces = self.db_mgr.load_face_database()
            self._sanitise_dimensions()
            self._emb_cache.clear()  # invalidate all cached embeddings
            print(f'[FaceDB] reloaded — '
                  f'{len(self.db_mgr.known_faces)} osob')

    def list_faces(self) -> list[str]:
        if self.db_mgr:
            return list(self.db_mgr.get_all_encodings().keys())
        return []

    def get_sample_counts(self) -> dict[str, int]:
        """Return {name: sample_count} for diagnostics."""
        if not self.db_mgr:
            return {}
        return {name: len(embs if isinstance(embs, list) else [embs])
                for name, embs in self.db_mgr.known_faces.items()}

    # ── Matching ──────────────────────────────────────────────────────────

    def _normalise(self, v: np.ndarray) -> np.ndarray | None:
        v    = np.array(v, dtype=np.float32).flatten()
        norm = np.linalg.norm(v)
        if norm < 1e-6:
            return None
        return v / norm

    def _get_cached(self, name: str, embeddings: list):
        """
        Return (normalised_matrix, mean_vector) for a person, using cache.
        Cache entry is invalidated by add() and reload().
        """
        if name in self._emb_cache:
            return self._emb_cache[name]

        valid = [e for e in embeddings
                 if hasattr(e, '__len__') and len(e) == ARCFACE_DIM]
        if not valid:
            return None, None

        mat   = np.array(valid, dtype=np.float32)
        norms = np.linalg.norm(mat, axis=1, keepdims=True)
        norms = np.where(norms > 1e-6, norms, 1.0)
        mat   = mat / norms

        mean_emb  = mat.mean(axis=0)
        mean_norm = np.linalg.norm(mean_emb)
        mean_vec  = mean_emb / mean_norm if mean_norm > 1e-6 else mean_emb

        self._emb_cache[name] = (mat, mean_vec)
        return mat, mean_vec

    def _person_score(self, query_norm: np.ndarray,
                      embeddings: list, name: str = "") -> float:
        """
        Score a single person against query_norm.
        Uses cached normalised matrix and mean vector.
        Final score = 0.6 * mean_sim + 0.4 * max_sample_sim
        """
        mat, mean_vec = self._get_cached(name, embeddings)
        if mat is None:
            return 0.0

        sims     = mat @ query_norm
        max_sim  = float(np.max(sims))
        mean_sim = float(np.dot(mean_vec, query_norm))
        return 0.6 * mean_sim + 0.4 * max_sim

    def _match(self, embedding: np.ndarray,
               db: dict) -> tuple[str, float]:
        """Full matching with margin check."""
        query = self._normalise(embedding)
        if query is None:
            return "Unknown", 0.0

        if len(query) != ARCFACE_DIM:
            print(f"[FaceDB] WARNING: query dim={len(query)}, expected {ARCFACE_DIM}")
            return "Unknown", 0.0

        scores = {}
        for name, embeddings in db.items():
            lst = embeddings if isinstance(embeddings, list) else [embeddings]
            scores[name] = self._person_score(query, lst, name)

        if not scores:
            return "Unknown", 0.0

        sorted_scores = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        best_name, best_score   = sorted_scores[0]
        second_score            = sorted_scores[1][1] if len(sorted_scores) > 1 else 0.0

        if self._debug:
            for n, s in sorted_scores[:3]:
                print(f"[FaceDB]   {n}: {s:.3f}")
            margin = best_score - second_score
            print(f"[FaceDB] best={best_name} @ {best_score:.3f}  "
                  f"margin={margin:.3f}  thresh={self._arcface_thresh}")

        # Threshold check
        if best_score < self._arcface_thresh:
            if self._debug:
                print("[FaceDB] → REJECT (below threshold)")
            return "Unknown", 0.0

        # Margin check — ambiguous if top-2 are too close
        if best_score - second_score < self._margin:
            if self._debug:
                print(f"[FaceDB] → REJECT (margin {best_score - second_score:.3f} "
                      f"< {self._margin})")
            return "Unknown", 0.0

        if self._debug:
            print(f"[FaceDB] → MATCH {best_name}")
        return best_name, round(best_score, 2)
