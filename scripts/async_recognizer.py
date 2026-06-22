"""
Async Recognizer Module
Background-thread face recognition with identity-tracked EMA smoothing.

Improvements over original:
  - EMA state tracked by identity (IoU box matching) not slot index.
  - Client-side similarity transform alignment on HQ crops.
  - Lower default EMA alpha (0.30) for more stable chatbot triggers.
  - Stale identity slots pruned after face_lost_timeout seconds.
  - Per-name notification cooldown prevents hammering chat handler every frame.
  - Fallback TTS greeting if chat handler is unavailable.
"""

import threading
import traceback
import time
import numpy as np
import cv2
from datetime import datetime

from scripts.hailo_client import ARCFACE_DIM, ARCFACE_SIZE, LABEL_FACE

# ArcFace canonical 5-point reference landmarks (112×112 space)
_ARCFACE_REF = np.array([
    [38.2946, 51.6963],   # left eye
    [73.5318, 51.5014],   # right eye
    [56.0252, 71.7366],   # nose tip
    [41.5493, 92.3655],   # left mouth corner
    [70.7299, 92.2041],   # right mouth corner
], dtype=np.float32)

# How long (seconds) to keep an EMA slot alive after its box disappears
_SLOT_TIMEOUT = 4.0


def _iou(a: list, b: list) -> float:
    """Intersection-over-Union for two normalised [x1,y1,x2,y2] boxes."""
    ix1 = max(a[0], b[0]); iy1 = max(a[1], b[1])
    ix2 = min(a[2], b[2]); iy2 = min(a[3], b[3])
    inter = max(0.0, ix2 - ix1) * max(0.0, iy2 - iy1)
    if inter == 0.0:
        return 0.0
    area_a = (a[2] - a[0]) * (a[3] - a[1])
    area_b = (b[2] - b[0]) * (b[3] - b[1])
    return inter / (area_a + area_b - inter + 1e-6)


class _IdentitySlot:
    """EMA state for one tracked face identity."""

    def __init__(self, names: list, box: list):
        self.scores    = {n: 0.0 for n in names}
        self.box       = box
        self.last_seen = time.time()

    def update_box(self, box: list):
        self.box       = box
        self.last_seen = time.time()

    def add_name(self, name: str):
        if name not in self.scores:
            self.scores[name] = 0.0

    def is_stale(self) -> bool:
        return time.time() - self.last_seen > _SLOT_TIMEOUT


from scripts.face_preprocess import FacePreprocessor as _FacePreprocessor


class AsyncRecognizer:
    """
    Submits hailo_results to a background thread for DB lookup.
    Identity-tracked EMA smoothing suppresses flicker and cross-contamination.

    Usage:
        rec = AsyncRecognizer(face_db, hailo_client, config, openwebui_chat)
        rec.submit(hailo_results, main_frame=rgb_1280x960)
        boxes, identities, labels = rec.get_identities()
    """

    def __init__(self, face_db, hailo_client, config=None, openwebui_chat=None):
        self.face_db        = face_db
        self.hailo          = hailo_client
        self.openwebui_chat = openwebui_chat
        self.config         = config or {}

        rt = self.config.get("recognition_tuning", {})
        self._ema_alpha  = float(rt.get("ema_alpha",    0.30))
        self._ema_thresh = float(rt.get("ema_thresh",   0.40))
        self._ema_margin = float(rt.get("ema_margin",   0.04))
        self._ema_head   = float(rt.get("ema_headroom", 0.02))
        self._iou_match  = float(rt.get("iou_match",    0.35))

        # 2C — greeting consensus
        # EMA decay: jak rychle padá score když raw=Unknown.
        # 1.0 = stejně rychle jako roste (původní chování — pošlapává).
        # 0.4 = padá 2.5× pomaleji než roste (drží score přes výpadky).
        self._ema_decay_factor = float(rt.get("ema_decay_factor", 0.4))
        self._consensus_min    = int(rt.get("consensus_min",    3))
        self._consensus_window = int(rt.get("consensus_window", 5))

        # 2D — diagnostic log (RECOGNITION_LOG_ROTATE_V1)
        self._diag_log_enabled = bool(rt.get("diag_log", True))
        self._diag_log_path    = rt.get("diag_log_path",
                                        "data/recognition.log")
        self._diag_logger      = None
        if self._diag_log_enabled:
            try:
                import logging as _logging
                from pathlib import Path as _Path
                _Path(self._diag_log_path).parent.mkdir(
                    parents=True, exist_ok=True)
                _diag = _logging.getLogger("recognition_diag")
                _diag.setLevel(_logging.INFO)
                if not any(
                    isinstance(h, _logging.FileHandler)
                    and getattr(h, "baseFilename", "")
                       .endswith(_Path(self._diag_log_path).name)
                    for h in _diag.handlers
                ):
                    from logging.handlers import RotatingFileHandler as _RFH
                    _max_mb = int(rt.get("diag_log_max_mb", 20))
                    _backups = int(rt.get("diag_log_backups", 3))
                    _h = _RFH(
                        self._diag_log_path, maxBytes=_max_mb * 1024 * 1024,
                        backupCount=_backups, encoding="utf-8")
                    _h.setFormatter(_logging.Formatter(
                        "%(asctime)s %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S"))
                    _diag.addHandler(_h)
                _diag.propagate = False
                self._diag_logger = _diag
                _diag.info("=== AsyncRecognizer started — "
                           "consensus=%d/%d ema_thresh=%.2f "
                           "ema_margin=%.2f ===",
                           self._consensus_min,
                           self._consensus_window,
                           self._ema_thresh, self._ema_margin)
            except Exception as _e:
                print(f"[AsyncRecognizer] diag log init failed: {_e}")

        self._lock       = threading.Lock()
        self._pending    = None
        self._boxes_out  = []
        self._ids_out    = []
        self._labels_out = []
        self._busy       = False

        # Identity-keyed slots: slot_id (int) → _IdentitySlot
        self._slots: dict = {}
        self._next_slot_id = 0

        # Per-name notification cooldown — prevents re-greeting when face
        # briefly leaves frame and comes back.
        # Set to 3600s (1 hour) — chat handler's once_per_session is the
        # primary gate, this is just a safety net against rapid re-triggers.
        self._last_notified:   dict = {}
        self._notify_cooldown: float = 3600.0

        # Direct TTS reference for fallback greeting (bypasses chat handler)
        self.tts_speaker = None  # set by PicamDisplayController after init

        self._face_prep = _FacePreprocessor(self.config)
        # Filtr mikro-detekcí — boxy menší než min_face_area se neposílají
        # do Hailo embedderu ani do EMA logiky (šetří čas + zabraňuje
        # zaplnění recent-decisions šumem z fantom tracků v dálce).
        self._min_face_area = float(
            config.get('async_recognizer', {})
                  .get('min_face_area', 0.005)
        )

        # Vztahové karty — sighting bump + person_seen events do deníku.
        # Defenzivně: pokud modul selže, recognizer běží dál bez karet.
        self._relationships = None
        self._diary_path_for_rel = self.config.get(
            "diary_db", "data/hans_diary.db")
        try:
            from scripts.hans_relationships import Relationships
            self._relationships = Relationships(self.config)
            self._relationships.seed_if_empty()
        except Exception as _e:
            print(f"[AsyncRecognizer] Relationships init failed: {_e}")

        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    # ── Public API ────────────────────────────────────────────────────────────

    def submit(self, hailo_results: list, main_frame=None, skip_embed_idx=None):
        """Non-blocking submit. Drops frame if worker is still busy.

        DETECT_DEDUP_EMBED_V1: skip_embed_idx = indexy, které už detect_faces
        hi-res embeddoval → async je znovu neembedduje (fallback na emb v results).
        """
        if self._busy:
            return
        with self._lock:
            self._pending = (list(hailo_results), main_frame,
                             set(skip_embed_idx) if skip_embed_idx else set())

    def get_identities(self) -> tuple:
        """Return (boxes, identities, labels) from the last completed job."""
        with self._lock:
            return (list(self._boxes_out),
                    list(self._ids_out),
                    list(self._labels_out))

    def seed_name(self, name: str):
        """Pre-warm all slots with a freshly enrolled person's name."""
        # _last_notified_lock_fix: _last_notified reset under lock
        rt   = self.config.get('recognition_tuning', {})
        seed = (float(rt.get('ema_thresh',    0.40))
              + float(rt.get('ema_headroom',  0.02))
              + float(rt.get('ema_margin',    0.04))
              + 0.05)
        with self._lock:
            # Reset greeting cooldown so new person gets greeted promptly
            self._last_notified.pop(name, None)
            if not self._slots:
                sid = self._next_slot_id
                self._next_slot_id += 1
                self._slots[sid] = _IdentitySlot([name], [0, 0, 1, 1])
                self._slots[sid].scores[name] = seed
            else:
                for slot in self._slots.values():
                    slot.add_name(name)
                    slot.scores[name] = seed

    # ── Diagnostic logging ────────────────────────────────────────────────────

    def _diag_log(self, slot, box, raw_name, raw_conf,
                  top1_name, top1_score, margin,
                  reason="", second_name="-", second_score=0.0,
                  consensus_name=None, recent=None,
                  final_name=None, final_conf=None):
        if not self._diag_logger:
            return
        try:
            slot_id = id(slot) % 100000
            bw = max(0.0, box[2] - box[0])
            bh = max(0.0, box[3] - box[1])
            box_area = round(bw * bh, 4)
            cons_str = (f" cons={consensus_name}"
                        if consensus_name else "")
            rec_str  = ""
            if recent:
                rec_str = " recent=[" + ",".join(
                    (r[0] if r != "Unknown" else "U") for r in recent
                ) + "]"
            # Pokud final_name = top1_name, kompaktnější výpis
            if final_name is None: final_name = top1_name
            if final_conf is None: final_conf = top1_score
            self._diag_logger.info(
                "track=%05d area=%.4f "
                "raw=%s:%.2f top1=%s:%.2f top2=%s:%.2f "
                "margin=%+.2f -> %s:%.2f (%s)%s%s",
                slot_id, box_area,
                raw_name, raw_conf,
                top1_name, top1_score,
                second_name, second_score,
                margin,
                final_name, final_conf, reason,
                cons_str, rec_str)
        except Exception:
            pass

    # ── Background worker ─────────────────────────────────────────────────────

    def _loop(self):
        while True:
            with self._lock:
                job = self._pending
                self._pending = None
            if job is None:
                time.sleep(0.005)
                continue

            hailo_results, main_frame, skip_embed_idx = job
            self._busy = True
            boxes      = []
            identities = []
            labels_out = []

            try:
                # Filtr mikro-detekcí (fantom tracky / vzdálené postavy)
                face_indices = []
                for fi in range(len(hailo_results)):
                    box = hailo_results[fi][0]
                    bw  = max(0.0, box[2] - box[0])
                    bh  = max(0.0, box[3] - box[1])
                    if bw * bh >= self._min_face_area:
                        face_indices.append(fi)
                # TODO: filter by gesture label when gesture detection is added
                hq_embs = {}

                # ── HQ Mode-2 embeddings ───────────────────────────────────
                # DETECT_DEDUP_EMBED_V1: obličeje, které už detect_faces hi-res
                # embeddoval (skip_embed_idx), znovu NEembeddujeme — downstream
                # hq_embs.get(idx, emb) fallback použije jejich HQ embed z
                # hailo_results. Embeddujeme jen zbytek (velké obličeje).
                _to_embed = [fi for fi in face_indices
                             if fi not in skip_embed_idx]
                if main_frame is not None and _to_embed:
                    crops = []
                    for fi in _to_embed:
                        box  = hailo_results[fi][0]
                        crop = self._aligned_crop(main_frame, box)
                        if crop is None:
                            crop = self._padded_crop(main_frame, box)
                        crops.append(crop if crop is not None
                                     else np.zeros((ARCFACE_SIZE, ARCFACE_SIZE, 3),
                                                   np.uint8))
                    try:
                        # Enhance crops před ArcFace (gamma korekce v tmavých podmínkách)
                        if self._face_prep.enabled:
                            crops = [self._face_prep.enhance_crop(c) for c in crops]
                        emb_list = self.hailo.embed_faces(crops)
                        for fi, emb in zip(_to_embed, emb_list):
                            if emb is not None:
                                hq_embs[fi] = emb
                    except Exception:
                        pass

                all_names = self.face_db.list_faces()

                # ── Prune stale slots ──────────────────────────────────────
                stale = [sid for sid, slot in self._slots.items()
                         if slot.is_stale()]
                for sid in stale:
                    del self._slots[sid]

                # ── Match each detection to an identity slot ───────────────
                used_slots: set = set()

                for det_idx, (box, emb, label) in enumerate(hailo_results):
                    boxes.append(box)
                    labels_out.append(LABEL_FACE)  # always face in scrfd mode

                    # TODO: handle gesture detection label here

                    # Skip DB lookup if embedding is all-zeros
                    raw_emb = hq_embs.get(det_idx, np.array(emb))
                    if np.linalg.norm(raw_emb) < 1e-6:
                        identities.append(("Unknown", 0.0))
                        continue

                    use_emb = raw_emb

                    try:
                        raw_name, raw_conf = self.face_db.identify(use_emb)
                    except Exception:
                        raw_name, raw_conf = "Unknown", 0.0

                    # ── EMA slot matching ──────────────────────────────────
                    slot = self._match_slot(box, used_slots, all_names)
                    slot.update_box(box)
                    used_slots.add(id(slot))

                    # ── EMA update ─────────────────────────────────────────
                    alpha = self._ema_alpha
                    state = slot.scores

                    decay_alpha = alpha * self._ema_decay_factor
                    if raw_name != "Unknown":
                        for n in list(state.keys()):
                            if n == raw_name:
                                state[n] = (alpha * raw_conf
                                            + (1.0 - alpha) * state.get(n, 0.0))
                            else:
                                state[n] *= (1.0 - decay_alpha)
                    else:
                        for n in list(state.keys()):
                            state[n] *= (1.0 - decay_alpha)

                    # ── Decision ───────────────────────────────────────────
                    if not state:
                        identities.append(("Unknown", 0.0))
                        self._diag_log(slot, box, raw_name, raw_conf,
                                       "Unknown", 0.0, 0.0,
                                       reason="empty_state")
                        continue

                    sorted_s = sorted(state.items(), key=lambda x: x[1], reverse=True)
                    best_n, best_s  = sorted_s[0]
                    second_s        = sorted_s[1][1] if len(sorted_s) > 1 else 0.0
                    margin          = best_s - second_s

                    if (best_s >= self._ema_thresh
                            and best_s >= self._ema_thresh + self._ema_head
                            and margin >= self._ema_margin):
                        conf_name = best_n
                        conf_conf = round(raw_conf if raw_name == best_n
                                          else best_s, 2)
                        decision_reason = "ok"
                    else:
                        conf_name = "Unknown"
                        conf_conf = 0.0
                        if best_s < self._ema_thresh:
                            decision_reason = "low_score"
                        elif margin < self._ema_margin:
                            decision_reason = "low_margin"
                        else:
                            decision_reason = "low_headroom"

                    identities.append((conf_name, conf_conf))

                    # 2C: Multi-frame consensus tracking
                    if not hasattr(slot, "recent_decisions"):
                        from collections import deque as _deque
                        slot.recent_decisions = _deque(
                            maxlen=self._consensus_window)
                    slot.recent_decisions.append(conf_name)

                    consensus_name = None
                    if conf_name != "Unknown":
                        n_match = sum(1 for x in slot.recent_decisions
                                      if x == conf_name)
                        if n_match >= self._consensus_min:
                            consensus_name = conf_name

                    # Diagnostic log — top1=best_n (skutečný favorit), conf_name=rozhodnutí
                    self._diag_log(slot, box, raw_name, raw_conf,
                                   best_n, best_s, margin,
                                   reason=decision_reason,
                                   second_name=(sorted_s[1][0]
                                                if len(sorted_s) > 1 else "-"),
                                   second_score=second_s,
                                   consensus_name=consensus_name,
                                   recent=list(slot.recent_decisions),
                                   final_name=conf_name,
                                   final_conf=conf_conf)

                    # Greeting trigger (jen po dosažení konsensu)
                    if consensus_name is not None:
                        now  = time.time()

                        # Vztahové karty — bump sighting + person_seen event.
                        # Throttle si řeší Relationships sám (default 30s).
                        # Tohle běží mimo notify_cooldown, ať se evidují
                        # i opakovaná setkání po pár minutách.
                        self._note_person_seen(consensus_name)

                        last = self._last_notified.get(consensus_name, 0)
                        if now - last >= self._notify_cooldown:
                            self._last_notified[consensus_name] = now

                            if self._diag_logger:
                                self._diag_logger.info(
                                    "GREETING fired name=%s conf=%.2f "
                                    "after %d/%d consensus",
                                    consensus_name, conf_conf,
                                    self._consensus_min,
                                    self._consensus_window)

                            if self.openwebui_chat:
                                try:
                                    self.openwebui_chat.handle_face_recognition(
                                        consensus_name, conf_conf)
                                except Exception:
                                    pass
                            elif self.tts_speaker and self.tts_speaker.enabled:
                                try:
                                    h = datetime.now().hour
                                    if 5 <= h < 12:    tod = "Good morning"
                                    elif 12 <= h < 17: tod = "Good afternoon"
                                    elif 17 <= h < 22: tod = "Good evening"
                                    else:              tod = "Hello"
                                    self.tts_speaker.speak(
                                        f"{tod} {consensus_name}!",
                                        priority=True)
                                except Exception:
                                    pass

            except Exception:
                print("[AsyncRecognizer] exception:")
                traceback.print_exc()
                identities = [("Unknown", 0.0)] * len(hailo_results)

            with self._lock:
                self._boxes_out  = boxes
                self._ids_out    = identities
                self._labels_out = labels_out
            self._busy = False

    # ── Slot management ───────────────────────────────────────────────────────

    def _match_slot(self, box: list, used: set, all_names: list) -> _IdentitySlot:
        """Find or create an EMA slot for this detection box."""
        best_iou  = self._iou_match
        best_slot = None

        for slot in self._slots.values():
            if id(slot) in used:
                continue
            iou = _iou(slot.box, box)
            if iou > best_iou:
                best_iou  = iou
                best_slot = slot

        if best_slot is not None:
            for n in all_names:
                best_slot.add_name(n)
            return best_slot

        sid = self._next_slot_id
        self._next_slot_id += 1
        self._slots[sid] = _IdentitySlot(all_names, box)
        return self._slots[sid]

    # ── Crop helpers ──────────────────────────────────────────────────────────

    def _padded_crop(self, frame: np.ndarray, box: list):
        """Simple padded box crop → 112×112."""
        H, W = frame.shape[:2]
        x1, y1, x2, y2 = box
        bw, bh = x2 - x1, y2 - y1
        pad    = 0.10
        ix1 = max(0, int((x1 - bw * pad) * W))
        iy1 = max(0, int((y1 - bh * pad) * H))
        ix2 = min(W, int((x2 + bw * pad) * W))
        iy2 = min(H, int((y2 + bh * pad) * H))
        if ix2 <= ix1 or iy2 <= iy1:
            return None
        patch = frame[iy1:iy2, ix1:ix2]
        return cv2.resize(patch, (ARCFACE_SIZE, ARCFACE_SIZE),
                          interpolation=cv2.INTER_LINEAR)

    def _note_person_seen(self, name: str):
        """Bump sighting count + zapiš person_seen event do deníku.

        Throttle: Relationships.record_sighting() sám rozhodne,
        jestli aktualizuje (default 1× za 30s na osobu). Pokud
        ne, do deníku taky nezapíšeme — držíme to v synchronu.
        """
        rel = self._relationships
        if rel is None:
            return
        try:
            wrote = rel.record_sighting(name)
            if not wrote:
                return
            # Doplníme i person_seen event do deníku — pro synthesis hooks
            # a večerní reflexi. Stejný formát jako kolac_cases / hans_dialog.
            import sqlite3 as _sql
            with _sql.connect(self._diary_path_for_rel) as _db:
                _db.execute(
                    "INSERT INTO diary (ts, event_type, title, note) "
                    "VALUES (?,?,?,?)",
                    (time.time(), "person_seen", name, "")
                )
                _db.commit()
        except Exception as _e:
            # Neshazuj recognizer kvůli vedlejšímu logu
            if self._diag_logger:
                self._diag_logger.warning(
                    "person_seen failed for %s: %s", name, _e)

    def _aligned_crop(self, frame: np.ndarray, box: list):
        """Similarity-transform crop aligned to ArcFace canonical landmarks."""
        try:
            H, W = frame.shape[:2]
            x1, y1, x2, y2 = box
            bx1 = x1 * W;  by1 = y1 * H
            bx2 = x2 * W;  by2 = y2 * H
            bw  = bx2 - bx1
            bh  = by2 - by1

            if bw < 20 or bh < 20:
                return None

            pts_src = np.array([
                [bx1 + bw * 0.30, by1 + bh * 0.37],
                [bx1 + bw * 0.70, by1 + bh * 0.37],
                [bx1 + bw * 0.50, by1 + bh * 0.55],
                [bx1 + bw * 0.35, by1 + bh * 0.75],
                [bx1 + bw * 0.65, by1 + bh * 0.75],
            ], dtype=np.float32)

            M, _ = cv2.estimateAffinePartial2D(
                pts_src, _ARCFACE_REF, method=cv2.LMEDS)
            if M is None:
                return None

            aligned = cv2.warpAffine(
                frame, M, (ARCFACE_SIZE, ARCFACE_SIZE),
                flags=cv2.INTER_LINEAR,
                borderMode=cv2.BORDER_REPLICATE)
            return aligned

        except Exception:
            return None
