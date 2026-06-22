"""
PersistentUnknownTracker v2
Jeden aktivní cíl najednou — největší neznámá tvář.
Sleduje ji přes FaceTrack sessions dokud nenasbírá dost vzorků.
Anti-TV filtry: pohyb bbox, doba v záběru, embedding konzistence.
"""
import json
import logging
import shutil
import sqlite3
import threading
import time
import uuid
from pathlib import Path

import cv2
import numpy as np

_log = logging.getLogger("unknown_tracker")


def _cosine_dist(a, b):
    a = np.array(a, dtype=np.float32)
    b = np.array(b, dtype=np.float32)
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    if na < 1e-8 or nb < 1e-8:
        return 1.0
    return float(1.0 - np.dot(a/na, b/nb))


class _SessionBuffer:
    """Drží vzorky jedné track session (jeden průchod osoby)."""
    def __init__(self, track_id):
        self.track_id    = track_id
        self.embeddings  = []
        self.img_paths   = []
        self.qualities   = []
        self.bbox_hist   = []   # pro anti-TV filtr
        self.frame_count = 0
        self.started_at  = time.time()
        self.last_seen   = time.time()

    def add(self, emb, img_path, quality, bbox):
        self.embeddings.append(emb)
        self.img_paths.append(img_path)
        self.qualities.append(quality)
        self.bbox_hist.append(bbox)
        self.frame_count += 1
        self.last_seen = time.time()

    def mean_embedding(self):
        if not self.embeddings:
            return None
        e = np.mean(self.embeddings, axis=0)
        n = np.linalg.norm(e)
        return e / n if n > 1e-8 else e

    def is_tv(self) -> bool:
        """Heuristika: statický bbox = TV."""
        if len(self.bbox_hist) < 5:
            return False
        cxs = [(b[0]+b[2])/2 for b in self.bbox_hist]
        cys = [(b[1]+b[3])/2 for b in self.bbox_hist]
        return np.std(cxs) < 0.005 and np.std(cys) < 0.005

    def embedding_consistency(self) -> float:
        """Std průměrných vzdáleností — nízká = konzistentní embeddingy."""
        if len(self.embeddings) < 3:
            return 0.0
        mean = self.mean_embedding()
        dists = [_cosine_dist(e, mean) for e in self.embeddings]
        return float(np.std(dists))


class PersistentUnknownTracker:

    def __init__(self, config: dict, face_db, hailo_client,
                 openwebui_chat=None):
        self.config      = config
        self.face_db     = face_db
        self.hailo       = hailo_client
        self.chat        = openwebui_chat
        self._lock       = threading.Lock()
        self._recognizer = None
        self._cluster_db = None

        cfg = config.get("unknown_tracker", {})
        self._enabled          = bool(cfg.get("enabled", True))
        self._target_samples   = int(cfg.get("target_count", 40))
        self._min_area         = float(cfg.get("min_face_area", 0.03))
        self._capture_interval = float(cfg.get("capture_interval_s", 1.5))

        # ── Quality gate (auto-enroll safety) ─────────────────────
        self._gate_min_size_px    = int(cfg.get("gate_min_size_px",    80))
        self._gate_min_blur_var   = float(cfg.get("gate_min_blur_var", 50.0))
        self._gate_min_emb_norm   = float(cfg.get("gate_min_emb_norm", 0.50))
        self._gate_log_interval_s = float(cfg.get("gate_log_interval_s", 60.0))
        self._gate_drops          = {}    # reason → count
        self._gate_kept           = 0
        self._gate_last_log       = 0.0
        self._cooldown_s       = float(cfg.get("cooldown_s", 120.0))
        self._session_timeout  = float(cfg.get("session_timeout_s", 5.0))
        self._min_session_embs = int(cfg.get("min_session_embeddings", 5))
        self._match_thresh     = float(cfg.get("match_thresh", 0.40))
        self._max_candidates   = int(cfg.get("max_unknowns", 10))

        db_path = cfg.get("db_path", "data/unknown_tracker.db")
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._init_db()

        # Stav
        self._active_track_id  = None   # aktualni cil
        self._active_session:  _SessionBuffer | None = None
        self._last_capture     = 0.0
        self._last_enrolled    = 0.0
        self._window_open      = False
        self._candidates: dict[int, list] = {}  # cand_id -> [mean_emb, ...]

        self._load_candidates()
        _log.info("PersistentUnknownTracker v2 ready — target=%d",
                  self._target_samples)

    # ── DB ────────────────────────────────────────────────────────────────────

    def _init_db(self):
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS candidates (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at  REAL,
                updated_at  REAL,
                total_samples INTEGER DEFAULT 0,
                enrolled    INTEGER DEFAULT 0,
                enrolled_as TEXT
            );
            CREATE TABLE IF NOT EXISTS candidate_embeddings (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                cand_id     INTEGER,
                embedding   TEXT,
                img_path    TEXT,
                quality     REAL,
                captured_at REAL,
                FOREIGN KEY (cand_id) REFERENCES candidates(id)
            );
        """)
        self._conn.commit()

    def _load_candidates(self):
        """Načti průměrné embeddingy kandidátů pro matching."""
        rows = self._conn.execute("""
            SELECT c.id, ce.embedding
            FROM candidates c JOIN candidate_embeddings ce ON ce.cand_id=c.id
            WHERE c.enrolled=0
        """).fetchall()
        self._candidates = {}
        for cid, emb_json in rows:
            emb = np.array(json.loads(emb_json), dtype=np.float32)
            self._candidates.setdefault(cid, []).append(emb)
        _log.info("Loaded %d candidate clusters", len(self._candidates))

    # ── Hlavní metoda ─────────────────────────────────────────────────────────

    def process(self, main_frame: np.ndarray, boxes: list,
                identities: list, hailo_results: list,
                active_tracks: dict):
        if not self._enabled or self._window_open:
            return
        if time.time() - self._last_enrolled < self._cooldown_s:
            return

        # Najdi neznámé tváře s track_id
        unknown_faces = []
        for i, (box, (name, conf), (_, emb, lbl)) in enumerate(
                zip(boxes, identities, hailo_results)):
            area = (box[2]-box[0]) * (box[3]-box[1])
            norm = np.linalg.norm(np.array(emb, dtype=np.float32)) if emb is not None else 0
            if name not in ("Unknown", "...", "?", ""):
                continue
            if area < self._min_area:
                continue
            if emb is None:
                continue
            emb_arr = np.array(emb, dtype=np.float32)
            raw_norm = np.linalg.norm(emb_arr)
            if raw_norm < self._gate_min_emb_norm:
                self._gate_count_drop(f"norm_{raw_norm:.2f}")
                continue
            emb_arr = emb_arr / raw_norm
            # Pouzij index boxu jako track_id (jednoduche a spolehlivé)
            tid = i
            unknown_faces.append((box, emb_arr, tid, area, i))

        if not unknown_faces:
            # Zkontroluj timeout aktivní session
            if self._active_session:
                elapsed = time.time() - self._active_session.last_seen
                if elapsed > self._session_timeout:
                    self._close_session()
            return

        with self._lock:
            # Pokud aktivní cíl stále vidíme → pokračuj
            active_tid = self._active_track_id
            active_face = next(
                (f for f in unknown_faces if f[2] == active_tid), None)

            if active_face is None:
                # Aktivní cíl zmizel
                if self._active_session:
                    elapsed = time.time() - self._active_session.last_seen
                    if elapsed > self._session_timeout:
                        self._close_session()
                # Vyber nový cíl — největší box
                if not self._active_track_id:
                    best = max(unknown_faces, key=lambda f: f[3])
                    self._active_track_id = best[2]
                    self._active_session  = _SessionBuffer(best[2])
                    _log.info("Nový cíl: track_id=%d area=%.3f",
                              best[2], best[3])
                    active_face = best

            if active_face is None:
                return

            box, emb_arr, tid, area, idx = active_face

            # Throttle
            if time.time() - self._last_capture < self._capture_interval:
                return
            self._last_capture = time.time()

            # Quality gate — drop low-quality samples PŘED add
            gate_ok, gate_reason = self._passes_quality_gate(
                main_frame, box)
            if not gate_ok:
                self._gate_count_drop(gate_reason)
                self._maybe_log_gate_stats()
                return
            self._gate_kept += 1
            self._maybe_log_gate_stats()

            # Anti-TV: musí být v záběru aspoň 10 framů
            if self._active_session.frame_count < 10:
                quality = self._quality(main_frame, box)
                self._active_session.add(emb_arr, None, quality, box)
                return

            if self._active_session.is_tv():
                _log.info("TV detekovana — reset aktivniho cile")
                self._active_session = None
                self._active_track_id = None
                return

            # Uloz vzorek
            quality  = self._quality(main_frame, box)
            img_path = self._save_crop(main_frame, box)
            self._active_session.add(emb_arr, img_path, quality, box)

            total = self._total_samples_for_active()
            _log.debug("Aktivni cil track=%d: session=%d total=%d",
                       tid, self._active_session.frame_count, total)

            if total >= self._target_samples and not self._window_open:
                self._window_open = True
                threading.Thread(
                    target=self._open_enrollment, daemon=True).start()

    # ── Session management ────────────────────────────────────────────────────

    def _close_session(self):
        """Uzavři session a ulož embeddingy do DB."""
        sess = self._active_session
        if not sess or sess.frame_count < self._min_session_embs:
            self._active_session  = None
            self._active_track_id = None
            return

        if sess.is_tv():
            _log.info("Session zavrena jako TV — zahozena")
            self._active_session  = None
            self._active_track_id = None
            return

        if sess.embedding_consistency() > 0.5:
            _log.info("Nekonzistentni embeddingy — session zahozena")
            self._active_session  = None
            self._active_track_id = None
            return

        mean_emb = sess.mean_embedding()

        # Spáruj s existujícím kandidátem nebo vytvoř nového
        cand_id = self._match_or_create_candidate(mean_emb)

        # Uloz vzorky do DB
        now = time.time()
        for emb, img_path, quality in zip(
                sess.embeddings, sess.img_paths, sess.qualities):
            self._conn.execute("""
                INSERT INTO candidate_embeddings
                    (cand_id, embedding, img_path, quality, captured_at)
                VALUES (?,?,?,?,?)
            """, (cand_id, json.dumps(emb.tolist()), img_path, quality, now))
        self._conn.execute("""
            UPDATE candidates SET updated_at=?,
                total_samples=total_samples+?
            WHERE id=?
        """, (now, len(sess.embeddings), cand_id))
        self._conn.commit()

        # Aktualizuj cache
        self._candidates.setdefault(cand_id, []).extend(sess.embeddings)

        total = self._total_samples(cand_id)
        _log.info("Session ulozena: cand_id=%d session=%d total=%d",
                  cand_id, len(sess.embeddings), total)

        self._active_session  = None
        self._active_track_id = None

        if total >= self._target_samples and not self._window_open:
            self._window_open = True
            threading.Thread(
                target=self._open_enrollment,
                args=(cand_id,), daemon=True).start()

    def _match_or_create_candidate(self, mean_emb: np.ndarray) -> int:
        """Najdi existujícího kandidáta nebo vytvoř nového."""
        best_cid  = None
        best_dist = self._match_thresh

        for cid, embs in self._candidates.items():
            centroid = np.mean(embs, axis=0)
            norm = np.linalg.norm(centroid)
            if norm > 1e-8:
                centroid /= norm
            dist = _cosine_dist(mean_emb, centroid)
            if dist < best_dist:
                best_dist = dist
                best_cid  = cid

        if best_cid is not None:
            return best_cid

        # Nový kandidát
        if len(self._candidates) >= self._max_candidates:
            # Smaz nejstaršího s nejméně vzorky
            oldest = self._conn.execute("""
                SELECT id FROM candidates WHERE enrolled=0
                ORDER BY total_samples ASC, updated_at ASC LIMIT 1
            """).fetchone()
            if oldest:
                self._remove_candidate(oldest[0])

        now = time.time()
        cur = self._conn.execute(
            "INSERT INTO candidates (created_at, updated_at) VALUES (?,?)",
            (now, now))
        self._conn.commit()
        cid = cur.lastrowid
        self._candidates[cid] = [mean_emb.copy()]
        _log.info("Nový kandidát: id=%d", cid)
        return cid

    def _remove_candidate(self, cid: int):
        self._conn.execute(
            "DELETE FROM candidate_embeddings WHERE cand_id=?", (cid,))
        self._conn.execute("DELETE FROM candidates WHERE id=?", (cid,))
        self._conn.commit()
        self._candidates.pop(cid, None)

    def _total_samples(self, cand_id: int) -> int:
        row = self._conn.execute(
            "SELECT total_samples FROM candidates WHERE id=?",
            (cand_id,)).fetchone()
        return row[0] if row else 0

    def _total_samples_for_active(self) -> int:
        """Celkový počet vzorků pro aktivního kandidáta."""
        if not self._active_session:
            return 0
        mean_emb = self._active_session.mean_embedding()
        if mean_emb is None:
            return self._active_session.frame_count

        best_cid  = None
        best_dist = self._match_thresh
        for cid, embs in self._candidates.items():
            centroid = np.mean(embs, axis=0)
            norm = np.linalg.norm(centroid)
            if norm > 1e-8:
                centroid /= norm
            dist = _cosine_dist(mean_emb, centroid)
            if dist < best_dist:
                best_dist = dist
                best_cid  = cid

        db_count = self._total_samples(best_cid) if best_cid else 0
        return db_count + self._active_session.frame_count

    # ── Enrollment okno ───────────────────────────────────────────────────────

    def _open_enrollment(self, cand_id: int = None):
        """Otevři enrollment okno pro kandidáta s nejvíce vzorky."""
        if cand_id is None:
            row = self._conn.execute("""
                SELECT id FROM candidates WHERE enrolled=0
                ORDER BY total_samples DESC LIMIT 1
            """).fetchone()
            if not row:
                self._window_open = False
                return
            cand_id = row[0]

        _log.info("Otevírám enrollment okno pro kandidáta #%d", cand_id)

        rows = self._conn.execute("""
            SELECT img_path, embedding FROM candidate_embeddings
            WHERE cand_id=? AND img_path IS NOT NULL
            ORDER BY quality DESC LIMIT 30
        """, (cand_id,)).fetchall()

        if not rows:
            _log.warning("Žádné snímky pro kandidáta #%d", cand_id)
            self._window_open = False
            return

        session_dir = Path("data/unknown_faces") / f"auto_{cand_id}_{uuid.uuid4().hex[:6]}"
        session_dir.mkdir(parents=True, exist_ok=True)

        embeddings = []
        for i, (img_path, emb_json) in enumerate(rows):
            if img_path and Path(img_path).exists():
                shutil.copy(img_path, session_dir / f"{i:03d}.jpg")
            embeddings.append(np.array(json.loads(emb_json), dtype=np.float32))

        def _on_done(name):
            self._window_open  = False
            self._last_enrolled = time.time()
            if name:
                _log.info("Kandidát #%d enrolled as '%s'", cand_id, name)
                self._conn.execute(
                    "UPDATE candidates SET enrolled=1, enrolled_as=? WHERE id=?",
                    (name, cand_id))
                self._conn.commit()
                self._candidates.pop(cand_id, None)
                if self._cluster_db:
                    for emb in embeddings:
                        self._cluster_db.add(name, emb)
                    self._cluster_db.save()
                try:
                    self.face_db.reload()
                    if self._recognizer:
                        self._recognizer._slots.clear()
                        self._recognizer.seed_name(name)
                except Exception as e:
                    _log.warning("reload failed: %s", e)
            else:
                _log.info("Enrollment skipped pro kandidáta #%d", cand_id)
            try:
                shutil.rmtree(session_dir)
            except Exception:
                pass
            # Smazat cropy kandidata z disku
            if name:
                rows = self._conn.execute(
                    "SELECT img_path FROM candidate_embeddings WHERE cand_id=?",
                    (cand_id,)).fetchall()
                for (p,) in rows:
                    if p:
                        Path(p).unlink(missing_ok=True)

        try:
            from scripts.unknown_enrollment_window import UnknownEnrollmentWindow
            UnknownEnrollmentWindow(session_dir, embeddings,
                                    self.face_db, on_done=_on_done)
        except Exception as e:
            _log.error("Enrollment okno selhalo: %s", e)
            self._window_open = False

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _box_key(box, precision=2):
        cx = round((box[0]+box[2])/2, precision)
        cy = round((box[1]+box[3])/2, precision)
        return (cx, cy)

    def _passes_quality_gate(self, frame: np.ndarray,
                             box: list) -> tuple[bool, str]:
        """
        Per-sample gate. Vrátí (True, "ok") nebo (False, reason).
        reason je krátký label pro statistiku dropů.
        """
        H, W = frame.shape[:2]
        x1 = max(0, int(box[0]*W)); y1 = max(0, int(box[1]*H))
        x2 = min(W, int(box[2]*W)); y2 = min(H, int(box[3]*H))

        # 1) Min size — pod prahem ArcFace nemá dost detailu
        bw = x2 - x1
        bh = y2 - y1
        if min(bw, bh) < self._gate_min_size_px:
            return False, f"size_{min(bw, bh)}"

        # 2) Blur — Laplacian variance na crop
        if x2 <= x1 or y2 <= y1:
            return False, "empty_crop"
        try:
            crop = frame[y1:y2, x1:x2]
            gray = (cv2.cvtColor(crop, cv2.COLOR_RGB2GRAY)
                    if crop.ndim == 3 else crop)
            blur = cv2.Laplacian(gray, cv2.CV_64F).var()
            if blur < self._gate_min_blur_var:
                return False, f"blur_{int(blur)}"
        except Exception:
            return False, "blur_err"

        return True, "ok"

    def _gate_count_drop(self, reason: str) -> None:
        """Spočítá drop podle kategorie (zkrácené reason)."""
        # Zkrať 'size_45', 'blur_32', 'norm_0.42' na kategorie
        cat = reason.split("_")[0] if "_" in reason else reason
        self._gate_drops[cat] = self._gate_drops.get(cat, 0) + 1

    def _maybe_log_gate_stats(self) -> None:
        """Loguj statistiky dropů 1x za gate_log_interval_s."""
        now = time.time()
        if now - self._gate_last_log < self._gate_log_interval_s:
            return
        self._gate_last_log = now
        if not self._gate_drops and self._gate_kept == 0:
            return
        drops_str = ", ".join(
            f"{k}={v}" for k, v in sorted(self._gate_drops.items(),
                                           key=lambda x: -x[1]))
        _log.info("gate stats (last %.0fs): kept=%d, dropped=%d (%s)",
                  self._gate_log_interval_s, self._gate_kept,
                  sum(self._gate_drops.values()), drops_str)
        self._gate_drops.clear()
        self._gate_kept = 0

    def _quality(self, frame: np.ndarray, box: list) -> float:
        H, W = frame.shape[:2]
        x1 = max(0, int(box[0]*W)); y1 = max(0, int(box[1]*H))
        x2 = min(W, int(box[2]*W)); y2 = min(H, int(box[3]*H))
        if x2 <= x1 or y2 <= y1:
            return 0.0
        crop  = frame[y1:y2, x1:x2]
        gray  = cv2.cvtColor(crop, cv2.COLOR_RGB2GRAY) if crop.ndim == 3 else crop
        sharp = min(cv2.Laplacian(gray, cv2.CV_64F).var() / 500.0, 1.0)
        size  = min((x2-x1)*(y2-y1) / (112*112), 1.0)
        return 0.6*sharp + 0.4*size

    def _save_crop(self, frame: np.ndarray, box: list) -> str | None:
        try:
            H, W = frame.shape[:2]
            x1 = max(0, int(box[0]*W)-20); y1 = max(0, int(box[1]*H)-20)
            x2 = min(W, int(box[2]*W)+20); y2 = min(H, int(box[3]*H)+20)
            crop = cv2.resize(frame[y1:y2, x1:x2], (160, 160))
            save_dir = Path("data/unknown_crops")
            save_dir.mkdir(parents=True, exist_ok=True)
            path = save_dir / f"{int(time.time()*1000)}.jpg"
            cv2.imwrite(str(path), cv2.cvtColor(crop, cv2.COLOR_RGB2BGR))
            return str(path)
        except Exception as e:
            _log.error("Save crop: %s", e)
            return None

    def cleanup_stale(self):
        """Smaz kandidáty bez aktivity > 30 min a s <= 2 vzorky."""
        cfg     = self.config.get("unknown_tracker", {})
        timeout = float(cfg.get("stale_timeout_min", 30)) * 60
        cutoff  = time.time() - timeout
        with self._lock:
            rows = self._conn.execute("""
                SELECT id FROM candidates
                WHERE enrolled=0 AND updated_at < ? AND total_samples <= 2
            """, (cutoff,)).fetchall()
            for (cid,) in rows:
                self._remove_candidate(cid)
                _log.info("Smazan stale kandidát #%d", cid)

    def get_status(self) -> list[dict]:
        rows = self._conn.execute("""
            SELECT id, total_samples, created_at, updated_at
            FROM candidates WHERE enrolled=0
            ORDER BY total_samples DESC
        """).fetchall()
        return [{"id": r[0], "samples": r[1],
                 "created": r[2], "updated": r[3]} for r in rows]

    def close(self):
        if self._active_session:
            self._close_session()
        self._conn.close()
