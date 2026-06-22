#!/usr/bin/env python3.13
"""
Hailo inference server — yolov5s_personface + ArcFace
======================================================
Model 1 — yolov5s_personface_h8l.hef  (person + face detector)
  Input  : yolov5s_personface/input_layer1   (640, 640, 3)  uint8
  Output : yolov5s_personface/yolov5_nms_postprocess
           shape (2, 5, 80)  float32   HAILO NMS
           dim 0 → class  (0=person, 1=face)
           dim 1 → [y1, x1, y2, x2, score]  normalised 0-1
           dim 2 → up to 80 detections per class

Model 2 — arcface_mobilefacenet.hef  (face embedder)
  Same as original server — unchanged.

Face ↔ Person pairing (server-side):
  Each face box is matched to the person box with the highest IoU.
  Paired person boxes are tagged with the same slot_id as their face
  so the client can associate them without running a second model.

Socket protocol — identical to hailo_inference_server.py:
  Mode 1 (detect + embed):
    Client → server : uint32 n_bytes  +  RGB uint8 640×480
    Server → client : uint32 N
                      N × 4  float32  boxes [x1,y1,x2,y2] normalised
                      N × 512 float32 ArcFace embeddings
                              (zeros for LABEL_PERSON boxes)
                      N × 1  uint8    labels (LABEL_FACE=0, LABEL_PERSON=1)

  Mode 2 (embed only) — identical to original server.
"""

import json
import os
import sys
import socket
import struct
import logging
import threading
import numpy as np
import cv2
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [HAILO-PF] %(message)s",
)
log = logging.getLogger(__name__)

# ── Mode 2 magic (must match hailo_client.py) ─────────────────────────────────
EMBED_MAGIC = b'\xEB\xED\xFE\xED'

# ── Label bytes ───────────────────────────────────────────────────────────────
LABEL_FACE   = 0
LABEL_PERSON = 1

# ── Model output class indices ────────────────────────────────────────────────
# yolov5s_personface NMS output: dim-0 class 0=person, 1=face
NMS_CLASS_PERSON = 0
NMS_CLASS_FACE   = 1

# ── Config loading ────────────────────────────────────────────────────────────
def _load_config():
    search = Path(__file__).resolve().parent
    for _ in range(4):
        candidate = search / "config.json"
        if candidate.exists():
            try:
                with open(candidate) as f:
                    return json.load(f)
            except Exception as e:
                log.warning("Could not parse %s: %s", candidate, e)
        search = search.parent
    return {}

_CFG   = _load_config()
_HAILO = _CFG.get("hailo", {})
_PF    = _CFG.get("hailo_server", {})

# ── Model paths ───────────────────────────────────────────────────────────────
DETECT_HEF = _PF.get("personface_hef",
                      "/usr/share/hailo-models/yolov5s_personface_h8l.hef")
RECOG_HEF  = _HAILO.get("recog_hef",
                         str(Path(__file__).parent.parent /
                             "resources/arcface_mobilefacenet.hef"))
SOCK_PATH  = "/tmp/hailo_scrfd.sock"   # same socket path — client unchanged

# ── Sizes ─────────────────────────────────────────────────────────────────────
# lores_from_config_patch
DET_W        = 640
DET_H        = 640
LORES_W      = int(_CFG.get('camera', {}).get('lores_width',  640))
LORES_H      = int(_CFG.get('camera', {}).get('lores_height', 480))
ARCFACE_SIZE = 112
ARCFACE_DIM  = 512

# ── Detection thresholds (config-driven) ──────────────────────────────────────
SCORE_THRESH = float(_PF.get("score_thresh", 0.35))

# ── ArcFace canonical 5-point reference (112×112) ────────────────────────────
_ARCFACE_REF = np.array([
    [38.2946, 51.6963],
    [73.5318, 51.5014],
    [56.0252, 71.7366],
    [41.5493, 92.3655],
    [70.7299, 92.2041],
], dtype=np.float32)


# =============================================================================
# Letterbox helpers  (640×480 → 640×640)
# =============================================================================

def _letterbox(frame_lores: np.ndarray) -> np.ndarray:
    """Scale + pad any lores frame to DET_W×DET_H square."""
    import cv2 as _cv2
    h, w = frame_lores.shape[:2]
    if w == DET_W and h == DET_H:
        return frame_lores
    scale  = min(DET_W / w, DET_H / h)
    nw, nh = int(w * scale), int(h * scale)
    resized = _cv2.resize(frame_lores, (nw, nh), interpolation=_cv2.INTER_LINEAR)
    pt = (DET_H - nh) // 2; pb = DET_H - nh - pt
    pl = (DET_W - nw) // 2; pr = DET_W - nw - pl
    return _cv2.copyMakeBorder(resized, pt, pb, pl, pr,
                               _cv2.BORDER_CONSTANT, value=(114, 114, 114))

def _get_letterbox_params_pf(src_w: int, src_h: int):
    scale    = min(DET_W / src_w, DET_H / src_h)
    new_w    = int(src_w * scale)
    new_h    = int(src_h * scale)
    pad_left = (DET_W - new_w) // 2
    pad_top  = (DET_H - new_h) // 2
    return scale, pad_left, pad_top

def _unletterbox_y(y: float) -> float:
    """Convert Y from letterboxed space back to lores normalised."""
    scale, _, pad_top = _get_letterbox_params_pf(LORES_W, LORES_H)
    return float(np.clip((y * DET_H - pad_top) / (scale * LORES_H), 0.0, 1.0))


# =============================================================================
# NMS output decoder
# =============================================================================

def decode_nms(raw: list, score_thresh: float,
               unletterbox: bool) -> tuple[list, list, list]:
    """
    Decode yolov5_nms_postprocess output.

    raw: list of 2 ndarrays  (one per class)
      raw[0] → person detections  shape (N, 5)
      raw[1] → face detections    shape (M, 5)
      each row → [y1, x1, y2, x2, score]  normalised 0-1

    Returns three parallel lists:
      boxes   — [[x1,y1,x2,y2], ...]  normalised 0-1
      scores  — [float, ...]
      classes — [NMS_CLASS_PERSON | NMS_CLASS_FACE, ...]
    """
    boxes   = []
    scores  = []
    classes = []

    for cls_idx, cls_data in enumerate(raw):
        if cls_data is None or len(cls_data) == 0:
            continue
        arr = np.array(cls_data, dtype=np.float32)   # (N, 5)
        for row in arr:
            score = float(row[4])
            if score < score_thresh:
                continue
            y1, x1, y2, x2 = float(row[0]), float(row[1]), \
                              float(row[2]), float(row[3])

            # Un-letterbox Y coordinates if frame was padded
            if unletterbox:
                y1 = _unletterbox_y(y1)
                y2 = _unletterbox_y(y2)

            x1, y1 = max(0.0, x1), max(0.0, y1)
            x2, y2 = min(1.0, x2), min(1.0, y2)

            if x2 <= x1 or y2 <= y1:
                continue

            boxes.append([x1, y1, x2, y2])
            scores.append(score)
            classes.append(cls_idx)

    return boxes, scores, classes


# =============================================================================
# Face ↔ Person pairing
# =============================================================================

def _iou(a: list, b: list) -> float:
    ix1 = max(a[0], b[0]); iy1 = max(a[1], b[1])
    ix2 = min(a[2], b[2]); iy2 = min(a[3], b[3])
    inter = max(0.0, ix2 - ix1) * max(0.0, iy2 - iy1)
    if inter == 0.0:
        return 0.0
    area_a = (a[2] - a[0]) * (a[3] - a[1])
    area_b = (b[2] - b[0]) * (b[3] - b[1])
    return inter / (area_a + area_b - inter + 1e-6)


def pair_detections(boxes: list, classes: list) -> dict:
    """
    Match each face box to the person box with the highest IoU (if any).
    Returns {face_idx: person_idx} for pairs found.
    Min IoU threshold: 0.05 (face box is much smaller than person box,
    so true IoU is low even when perfectly overlapping).
    """
    face_idxs   = [i for i, c in enumerate(classes) if c == NMS_CLASS_FACE]
    person_idxs = [i for i, c in enumerate(classes) if c == NMS_CLASS_PERSON]

    pairs = {}   # face_idx → person_idx
    used_persons = set()

    for fi in face_idxs:
        best_iou = 0.05   # minimum to count as paired
        best_pi  = None
        for pi in person_idxs:
            if pi in used_persons:
                continue
            iou = _iou(boxes[fi], boxes[pi])
            if iou > best_iou:
                best_iou = iou
                best_pi  = pi
        if best_pi is not None:
            pairs[fi]    = best_pi
            used_persons.add(best_pi)

    return pairs


# =============================================================================
# Face crop helpers
# =============================================================================

def crop_face(frame: np.ndarray, box: list) -> np.ndarray:
    """Padded box crop → 112×112."""
    H, W = frame.shape[:2]
    x1, y1, x2, y2 = box
    bw, bh = x2 - x1, y2 - y1
    pad    = 0.10
    ix1 = max(0, int((x1 - bw * pad) * W))
    iy1 = max(0, int((y1 - bh * pad) * H))
    ix2 = min(W, int((x2 + bw * pad) * W))
    iy2 = min(H, int((y2 + bh * pad) * H))
    if ix2 <= ix1 or iy2 <= iy1:
        return np.zeros((ARCFACE_SIZE, ARCFACE_SIZE, 3), np.uint8)
    return cv2.resize(frame[iy1:iy2, ix1:ix2],
                      (ARCFACE_SIZE, ARCFACE_SIZE),
                      interpolation=cv2.INTER_LINEAR)


def align_face(frame: np.ndarray, box: list) -> np.ndarray | None:
    """Similarity-transform aligned crop → 112×112. Falls back to None."""
    try:
        H, W = frame.shape[:2]
        x1, y1, x2, y2 = box
        bx1, by1 = x1 * W, y1 * H
        bx2, by2 = x2 * W, y2 * H
        bw, bh   = bx2 - bx1, by2 - by1
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
        return cv2.warpAffine(frame, M, (ARCFACE_SIZE, ARCFACE_SIZE),
                               flags=cv2.INTER_LINEAR,
                               borderMode=cv2.BORDER_REPLICATE)
    except Exception:
        return None


def l2_norm(v: np.ndarray) -> np.ndarray:
    v = np.array(v, dtype=np.float32).flatten()
    n = np.linalg.norm(v)
    return v / n if n > 1e-6 else v


# =============================================================================
# Inference engine
# =============================================================================

class HailoInferenceEngine:

    def __init__(self, detect_hef_path: str, recog_hef_path: str):
        from hailo_platform import (
            VDevice, HailoSchedulingAlgorithm, HEF,
            ConfigureParams, InputVStreamParams, OutputVStreamParams,
            FormatType, HailoStreamInterface, InferVStreams,
        )
        self._InferVStreams = InferVStreams

        params = VDevice.create_params()
        params.scheduling_algorithm = HailoSchedulingAlgorithm.ROUND_ROBIN
        self.vdevice = VDevice(params)
        log.info("VDevice opened (ROUND_ROBIN)")

        det_hef = HEF(detect_hef_path)
        rec_hef = HEF(recog_hef_path)

        det_cfg = ConfigureParams.create_from_hef(det_hef, HailoStreamInterface.PCIe)
        rec_cfg = ConfigureParams.create_from_hef(rec_hef, HailoStreamInterface.PCIe)

        self.det_ng = self.vdevice.configure(det_hef, det_cfg)[0]
        self.rec_ng = self.vdevice.configure(rec_hef, rec_cfg)[0]

        self.det_in_p  = InputVStreamParams.make(self.det_ng, format_type=FormatType.UINT8)
        self.det_out_p = OutputVStreamParams.make(self.det_ng, format_type=FormatType.FLOAT32)
        self.rec_in_p  = InputVStreamParams.make(self.rec_ng, format_type=FormatType.UINT8)
        self.rec_out_p = OutputVStreamParams.make(self.rec_ng, format_type=FormatType.FLOAT32)

        self._det_lock = threading.Lock()
        self._rec_lock = threading.Lock()

        log.info("Det ng=%s", self.det_ng.name)
        for o in self.det_ng.get_output_vstream_infos():
            log.info("  out: %s %s", o.name, o.shape)
        log.info("Rec ng=%s", self.rec_ng.name)

    def _open_pipelines(self):
        self._det_pipeline = self._InferVStreams(
            self.det_ng, self.det_in_p, self.det_out_p).__enter__()
        self._rec_pipeline = self._InferVStreams(
            self.rec_ng, self.rec_in_p, self.rec_out_p).__enter__()
        log.info("Persistent inference pipelines opened")

    def run_detect(self, frame_rgb_640: np.ndarray) -> list:
        """
        Returns raw NMS output: list of 2 arrays, one per class.
          result[0] → person detections, shape (N, 5)  float32
          result[1] → face detections,   shape (M, 5)  float32
          each row  → [y1, x1, y2, x2, score]  normalised 0-1
        """
        inp_name = self.det_ng.get_input_vstream_infos()[0].name
        out_name = self.det_ng.get_output_vstream_infos()[0].name
        with self._det_lock:
            out = self._det_pipeline.infer({inp_name: frame_rgb_640[np.newaxis]})
        return out[out_name][0]   # list of 2 ndarrays

    def run_recognize(self, face_rgb_112: np.ndarray) -> np.ndarray:
        inp_name = self.rec_ng.get_input_vstream_infos()[0].name
        out_name = self.rec_ng.get_output_vstream_infos()[0].name
        with self._rec_lock:
            out = self._rec_pipeline.infer({inp_name: face_rgb_112[np.newaxis]})
        return np.array(out[out_name]).flatten().astype(np.float32)

    def close(self):
        try:
            self.vdevice.release()
        except Exception:
            pass


# =============================================================================
# Socket helpers
# =============================================================================

def recv_exact(conn, n: int) -> bytes | None:
    buf = b""
    while len(buf) < n:
        chunk = conn.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf


# =============================================================================
# Per-client handler
# =============================================================================

def handle_client(conn, engine: HailoInferenceEngine,
                  infer_lock: threading.Lock):
    log.info("Client connected")
    frame_bytes_lores = LORES_H * LORES_W * 3
    frame_bytes_640   = DET_H  * DET_W * 3

    try:
        while True:
            hdr = recv_exact(conn, 4)
            if hdr is None:
                break

            # ── Mode 2: embed-only ────────────────────────────────────────
            if hdr == EMBED_MAGIC:
                n_hdr = recv_exact(conn, 4)
                if n_hdr is None:
                    break
                n_faces = struct.unpack(">I", n_hdr)[0]
                if n_faces == 0:
                    conn.sendall(struct.pack(">I", 0))
                    continue

                crop_bytes = ARCFACE_SIZE * ARCFACE_SIZE * 3
                embeddings = np.zeros((n_faces, ARCFACE_DIM), np.float32)
                ok = True
                for i in range(n_faces):
                    raw = recv_exact(conn, crop_bytes)
                    if raw is None:
                        ok = False
                        break
                    face_crop = np.frombuffer(raw, dtype=np.uint8).reshape(
                        ARCFACE_SIZE, ARCFACE_SIZE, 3).copy()
                    face_crop = np.ascontiguousarray(face_crop, dtype=np.uint8)
                    try:
                        with infer_lock:
                            embeddings[i] = l2_norm(engine.run_recognize(face_crop))
                    except Exception as e:
                        log.warning("embed_only face %d failed: %s", i, e)

                if not ok:
                    break
                conn.sendall(struct.pack(">I", n_faces))
                conn.sendall(embeddings.tobytes())
                continue

            # ── Mode 1: detect + embed ────────────────────────────────────
            size = struct.unpack(">I", hdr)[0]

            if size == frame_bytes_lores:
                raw_frame = recv_exact(conn, size)
                if raw_frame is None:
                    break
                frame_480 = np.frombuffer(raw_frame, dtype=np.uint8).reshape(
                    LORES_H, DET_W, 3).copy()
                frame     = _letterbox(frame_480)
                use_lores = True
            elif size == frame_bytes_640:
                raw_frame = recv_exact(conn, size)
                if raw_frame is None:
                    break
                frame     = np.frombuffer(raw_frame, dtype=np.uint8).reshape(
                    DET_H, DET_W, 3).copy()
                use_lores = False
            else:
                log.warning("Bad frame size %d", size)
                conn.sendall(struct.pack(">I", 0))
                continue

            try:
                # ── Detection ─────────────────────────────────────────────
                with infer_lock:
                    nms_raw = engine.run_detect(frame)

                boxes, scores, classes = decode_nms(
                    nms_raw, SCORE_THRESH, unletterbox=use_lores)

                if not boxes:
                    conn.sendall(struct.pack(">I", 0))
                    continue

                n_face   = sum(1 for c in classes if c == NMS_CLASS_FACE)
                n_person = sum(1 for c in classes if c == NMS_CLASS_PERSON)
                if n_face or n_person:
                    log.info("Detections: %d face, %d person", n_face, n_person)

                # ── Pair face ↔ person ────────────────────────────────────
                pairs = pair_detections(boxes, classes)   # {face_idx: person_idx}

                # ── Embed faces only ──────────────────────────────────────
                # Build per-detection output arrays
                N          = len(boxes)
                out_boxes  = np.zeros((N, 4), np.float32)
                out_embs   = np.zeros((N, ARCFACE_DIM), np.float32)
                out_labels = np.zeros(N, np.uint8)

                for i, (box, cls) in enumerate(zip(boxes, classes)):
                    out_boxes[i] = box

                    if cls == NMS_CLASS_FACE:
                        out_labels[i] = LABEL_FACE
                        # Aligned crop, fallback to padded box crop
                        crop = align_face(frame, box)
                        if crop is None:
                            crop = crop_face(frame, box)
                        crop = np.ascontiguousarray(crop, dtype=np.uint8)
                        try:
                            with infer_lock:
                                out_embs[i] = l2_norm(engine.run_recognize(crop))
                        except Exception as e:
                            log.warning("ArcFace failed for face %d: %s", i, e)

                    else:   # NMS_CLASS_PERSON
                        out_labels[i] = LABEL_PERSON
                        # Embeddings for person boxes stay zeros —
                        # the client uses the face embedding for identity.

                # ── Send ──────────────────────────────────────────────────
                conn.sendall(struct.pack(">I", N))
                conn.sendall(out_boxes.tobytes())
                conn.sendall(out_embs.tobytes())
                conn.sendall(out_labels.tobytes())

            except Exception as exc:
                import traceback
                log.error("Inference error: %s\n%s", exc, traceback.format_exc())
                try:
                    conn.sendall(struct.pack(">I", 0))
                except Exception:
                    pass

    except Exception as exc:
        import traceback
        log.error("Client error: %s\n%s", exc, traceback.format_exc())
    finally:
        conn.close()
        log.info("Client disconnected")


# =============================================================================
# Server
# =============================================================================

def run_server():
    log.info("personface detect HEF : %s", DETECT_HEF)
    log.info("ArcFace recog HEF     : %s", RECOG_HEF)
    log.info("Score threshold       : %.2f", SCORE_THRESH)

    for desc, path in [("Detection HEF",    DETECT_HEF),
                       ("Recognition HEF",  RECOG_HEF)]:
        if not Path(path).exists():
            log.error("Missing %s: %s", desc, path)
            sys.exit(1)

    if os.path.exists(SOCK_PATH):
        os.remove(SOCK_PATH)

    log.info("Initialising Hailo inference engine...")
    try:
        engine = HailoInferenceEngine(DETECT_HEF, RECOG_HEF)
    except Exception as exc:
        import traceback
        log.error("Failed to initialise: %s\n%s", exc, traceback.format_exc())
        sys.exit(1)

    infer_lock = threading.Lock()
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(SOCK_PATH)
    srv.listen(2)
    os.chmod(SOCK_PATH, 0o666)
    engine._open_pipelines()

    log.info("Ready — listening on %s", SOCK_PATH)
    log.info("Mode 1: detect+embed (yolov5s_personface — face + person)")
    log.info("Mode 2: embed-only   (pre-cropped 112×112 HQ patches)")

    try:
        while True:
            conn, _ = srv.accept()
            threading.Thread(
                target=handle_client,
                args=(conn, engine, infer_lock),
                daemon=True,
            ).start()
    except KeyboardInterrupt:
        pass
    finally:
        srv.close()
        try:
            os.remove(SOCK_PATH)
        except OSError:
            pass
        engine.close()


if __name__ == "__main__":
    run_server()
