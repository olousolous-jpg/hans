#!/usr/bin/env python3.13
"""
Hailo combined inference server — SCRFD + ArcFace + YOLOv8s
============================================================
Three models on one VDevice (ROUND_ROBIN):
  1. SCRFD 2.5g    — face detection
  2. ArcFace       — face recognition
  3. YOLOv8s       — object detection (COCO, no person class)

Socket: /tmp/hailo_scrfd.sock  (unchanged — clients unaffected)

Protocol — Mode 1 (detect + embed, face mode):
  Client → server : uint32 n_bytes + RGB uint8 lores_w×lores_h
  Server → client : uint32 N
                    N × 4   float32  boxes [x1,y1,x2,y2]
                    N × 512 float32  ArcFace embeddings
                    N × 1   uint8    labels (0=face)

Protocol — Mode 2 (embed only):
  Client → server : EMBED_MAGIC + uint32 N + N × (112×112×3)
  Server → client : uint32 N + N × 512 float32

Protocol — Mode 3 (object detection):
  Client → server : OBJ_MAGIC + uint32 n_bytes + RGB uint8 lores_w×lores_h
  Server → client : uint32 N
                    N × 6 float32  [x1,y1,x2,y2,confidence,class_id]
"""

import json
import os
import sys
import socket
import struct
# BLAZE_PALM_ANCHORS_V1 — externí blaze_app už NENÍ potřeba (palm anchory jsou
# nově v scripts/blaze_palm_anchors.py). BlazeDetector se nikdy nepoužíval.
import logging
import threading
import numpy as np
import cv2
from pathlib import Path

# ── ML gesture classifier (optional) ─────────────────────────────────────────
_ML_MODEL = None
_ML_LE    = None
_ML_LABEL_MAP = {'open_hand': 2, 'thumbs_up': 1, 'fist': 1, 'none': 0}

def _load_ml_model():
    global _ML_MODEL, _ML_LE
    model_path = Path("data/gesture_model.pkl")
    if not model_path.exists():
        return False
    try:
        import pickle
        with open(model_path, 'rb') as f:
            data = pickle.load(f)
        _ML_MODEL = data['clf']
        _ML_LE    = data['le']
        import logging
        logging.getLogger("hailo").info("ML gesture model loaded: %s", list(_ML_LE.classes_))
        return True
    except Exception as e:
        import logging
        logging.getLogger("hailo").warning("ML model load failed: %s", e)
        return False

def _normalize_landmarks(lm_flat):
    lm = np.array(lm_flat, dtype=np.float32).reshape(21, 3)
    lm -= lm[0]
    scale = np.linalg.norm(lm[9])
    if scale > 1e-6:
        lm /= scale
    return lm.flatten()

def _ml_classify(lm_flat) -> int:
    """Klasifikuj gesto pomocí ML modelu. Vrátí GESTURE_* konstantu."""
    if _ML_MODEL is None:
        return None  # fallback na geometrii
    try:
        lm_norm = _normalize_landmarks(lm_flat).reshape(1, -1)
        proba = _ML_MODEL.predict_proba(lm_norm)[0]
        max_proba = proba.max()
        if max_proba < 0.75:   # nejistá predikce = none
            return 0
        pred_idx = proba.argmax()
        label = _ML_LE.inverse_transform([pred_idx])[0]
        return _ML_LABEL_MAP.get(label, 0)
    except Exception:
        return None

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [HAILO] %(message)s")
log = logging.getLogger(__name__)

# ── Magic bytes ───────────────────────────────────────────────────────────────
EMBED_MAGIC = b'\xEB\xED\xFE\xED'   # Mode 2: embed only
OBJ_MAGIC   = b'\x0B\x1E\xC7\xD0'  # Mode 3: object detection
HAND_MAGIC  = b'\xAA\xBB\xCC\xDD'  # Mode 4: hand landmarks
GESTURE_SOCK = "/tmp/gesture.sock"   # dedicated gesture socket

# ── Config ────────────────────────────────────────────────────────────────────
def _load_config():
    search = Path(__file__).resolve().parent
    for _ in range(4):
        c = search / "config.json"
        if c.exists():
            try:
                return json.load(open(c))
            except Exception as e:
                log.warning("Could not parse %s: %s", c, e)
        search = search.parent
    return {}

_CFG   = _load_config()
_HAILO = _CFG.get("hailo", {})
_OBJ   = _CFG.get("objects", {})

DETECT_HEF = _HAILO.get("detect_hef",
                          "/usr/share/hailo-models/scrfd_2.5g_h8l.hef")
RECOG_HEF  = _HAILO.get("recog_hef",
                          str(Path(__file__).parent.parent /
                              "resources/arcface_mobilefacenet.hef"))
OBJ_HEF    = _OBJ.get("hef",
                        "/usr/share/hailo-models/yolov8s_h8l.hef")

_HAND = _CFG.get("gesture", {})
_PALM_HEF_DEFAULT = "resources/palm_detection_lite.hef"
PALM_HEF          = _HAND.get("palm_detection_hef", _PALM_HEF_DEFAULT)
PALM_INPUT_SIZE   = 192
PALM_SCORE_THRESH = float(_HAND.get("palm_score_threshold", 0.5))
PALM_PADDING      = float(_HAND.get("palm_padding", 0.3))
HAND_HEF           = _HAND.get("hand_landmark_hef",
                               "resources/hand_landmark_lite.hef")
HAND_INPUT_SIZE    = 224
HAND_PRESENCE_THRESH = float(_HAND.get("presence_threshold", 0.5))

GESTURE_NONE      = 0
GESTURE_FIST      = 1
GESTURE_OPEN_HAND = 2  # 🖐 všechny prsty natažené
GESTURE_THUMBS_UP = 3  # 👍 palec nahoru, ostatní skrčené

# Hand landmark indices
_THUMB_TIP  = 4;  _THUMB_MCP  = 2
_INDEX_TIP  = 8;  _INDEX_PIP  = 6
_MIDDLE_TIP = 12; _MIDDLE_PIP = 10
_RING_TIP   = 16; _RING_PIP   = 14
_PINKY_TIP  = 20; _PINKY_PIP  = 18
SOCK_PATH  = "/tmp/hailo_scrfd.sock"

DET_W   = 640
DET_H   = 640
LORES_W = int(_CFG.get("camera", {}).get("lores_width",  640))
LORES_H = int(_CFG.get("camera", {}).get("lores_height", 480))

ARCFACE_SIZE = 112
ARCFACE_DIM  = 512

SCRFD_SCORE_THRESH = float(_HAILO.get("scrfd_score_thresh", 0.40))
SCRFD_NMS_THRESH   = float(_HAILO.get("scrfd_nms_thresh",   0.45))
OBJ_SCORE_THRESH   = float(_OBJ.get("score_thresh", 0.40))

LABEL_FACE   = 0
LABEL_PERSON = 1

log.info("Lores resolution from config: %dx%d", LORES_W, LORES_H)

# ── SCRFD constants ───────────────────────────────────────────────────────────
SCRFD_STRIDES   = [8, 16, 32]
SCRFD_MIN_SIZES = [[16, 32], [64, 128], [256, 512]]
SCRFD_LAYERS = {
    8:  ('scrfd_2_5g/conv42', 'scrfd_2_5g/conv43'),
    16: ('scrfd_2_5g/conv49', 'scrfd_2_5g/conv50'),
    32: ('scrfd_2_5g/conv55', 'scrfd_2_5g/conv56'),
}
SCRFD_LM_LAYERS = {8:  'scrfd_2_5g/conv44',
                   16: 'scrfd_2_5g/conv51',
                   32: 'scrfd_2_5g/conv57'}

_ARCFACE_REF = np.array([
    [38.2946, 51.6963], [73.5318, 51.5014], [56.0252, 71.7366],
    [41.5493, 92.3655], [70.7299, 92.2041]], dtype=np.float32)

# ── Anchors ───────────────────────────────────────────────────────────────────
def _build_anchors(stride, min_sizes, fh, fw):
    a = []
    for r in range(fh):
        for c in range(fw):
            cx = (c + 0.5) * stride
            cy = (r + 0.5) * stride
            for sz in min_sizes:
                a.append([cx, cy, float(sz), float(sz)])
    return np.array(a, dtype=np.float32)

_FEAT_HW = {8: (80, 80), 16: (40, 40), 32: (20, 20)}
_ANCHORS  = {s: _build_anchors(s, ms, *_FEAT_HW[s])
             for s, ms in zip(SCRFD_STRIDES, SCRFD_MIN_SIZES)}

# ── Letterbox helpers — handles any lores resolution ─────────────────────────

def _letterbox(frame: np.ndarray) -> np.ndarray:
    """Scale + pad frame to DET_W×DET_H square with grey bars."""
    h, w = frame.shape[:2]
    if w == DET_W and h == DET_H:
        return frame  # already square
    scale  = min(DET_W / w, DET_H / h)
    new_w  = int(w * scale)
    new_h  = int(h * scale)
    resized = cv2.resize(frame, (new_w, new_h), interpolation=cv2.INTER_LINEAR)
    pad_top    = (DET_H - new_h) // 2
    pad_bottom = DET_H - new_h - pad_top
    pad_left   = (DET_W - new_w) // 2
    pad_right  = DET_W - new_w - pad_left
    return cv2.copyMakeBorder(resized, pad_top, pad_bottom, pad_left, pad_right,
                               cv2.BORDER_CONSTANT, value=(114, 114, 114))


def _get_letterbox_params(src_w: int, src_h: int):
    """Return (scale, pad_left, pad_top) used by _letterbox for given source size."""
    scale    = min(DET_W / src_w, DET_H / src_h)
    new_w    = int(src_w * scale)
    new_h    = int(src_h * scale)
    pad_left = (DET_W - new_w) // 2
    pad_top  = (DET_H - new_h) // 2
    return scale, pad_left, pad_top


def _unletterbox_boxes(boxes: np.ndarray) -> np.ndarray:
    """Convert normalised boxes from letterboxed space back to lores space."""
    if len(boxes) == 0:
        return boxes
    scale, pad_left, pad_top = _get_letterbox_params(LORES_W, LORES_H)
    b = boxes.copy()
    b[:, 0] = np.clip((b[:, 0] * DET_W - pad_left) / (scale * LORES_W), 0.0, 1.0)
    b[:, 1] = np.clip((b[:, 1] * DET_H - pad_top)  / (scale * LORES_H), 0.0, 1.0)
    b[:, 2] = np.clip((b[:, 2] * DET_W - pad_left) / (scale * LORES_W), 0.0, 1.0)
    b[:, 3] = np.clip((b[:, 3] * DET_H - pad_top)  / (scale * LORES_H), 0.0, 1.0)
    return b


def _unletterbox_y(y: float) -> float:
    """Unletterbox a single normalised Y coordinate."""
    scale, _, pad_top = _get_letterbox_params(LORES_W, LORES_H)
    return float(np.clip((y * DET_H - pad_top) / (scale * LORES_H), 0.0, 1.0))


# ── SCRFD decode ──────────────────────────────────────────────────────────────
def decode_scrfd(outputs):
    all_boxes = []; all_scores = []
    for stride in SCRFD_STRIDES:
        sk, bk = SCRFD_LAYERS[stride]
        if sk not in outputs or bk not in outputs: continue
        scores = np.array(outputs[sk], np.float32).reshape(-1)
        boxes  = np.array(outputs[bk], np.float32).reshape(-1, 4)
        anch   = _ANCHORS[stride]
        mask   = scores >= SCRFD_SCORE_THRESH
        if not np.any(mask): continue
        ss = scores[mask]; bs = boxes[mask]; aa = anch[mask]
        cx = aa[:, 0]; cy = aa[:, 1]
        x1 = (cx - bs[:, 0] * stride) / DET_W
        y1 = (cy - bs[:, 1] * stride) / DET_H
        x2 = (cx + bs[:, 2] * stride) / DET_W
        y2 = (cy + bs[:, 3] * stride) / DET_H
        all_boxes.append(np.stack([x1, y1, x2, y2], 1))
        all_scores.append(ss)
    if not all_boxes:
        return np.empty((0, 4), np.float32), np.empty((0,), np.float32)
    return np.clip(np.vstack(all_boxes), 0, 1), np.concatenate(all_scores)


def nms_filter(boxes, scores, thresh=None):
    if thresh is None: thresh = SCRFD_NMS_THRESH
    if len(boxes) == 0: return boxes, scores
    order = np.argsort(scores)[::-1]; keep = []
    while len(order):
        i = order[0]; keep.append(i)
        if len(order) == 1: break
        xx1 = np.maximum(boxes[i, 0], boxes[order[1:], 0])
        yy1 = np.maximum(boxes[i, 1], boxes[order[1:], 1])
        xx2 = np.minimum(boxes[i, 2], boxes[order[1:], 2])
        yy2 = np.minimum(boxes[i, 3], boxes[order[1:], 3])
        inter = np.maximum(0, xx2 - xx1) * np.maximum(0, yy2 - yy1)
        ai = (boxes[i, 2] - boxes[i, 0]) * (boxes[i, 3] - boxes[i, 1])
        ar = ((boxes[order[1:], 2] - boxes[order[1:], 0]) *
              (boxes[order[1:], 3] - boxes[order[1:], 1]))
        iou = inter / (ai + ar - inter + 1e-6)
        order = order[1:][iou < thresh]
    k = np.array(keep, np.int32)
    return boxes[k], scores[k]


# ── Face alignment ────────────────────────────────────────────────────────────
def align_face(frame, lm_norm, out=ARCFACE_SIZE):
    try:
        H, W = frame.shape[:2]
        pts  = lm_norm.copy()
        pts[:, 0] *= W; pts[:, 1] *= H
        M, _ = cv2.estimateAffinePartial2D(pts, _ARCFACE_REF, method=cv2.LMEDS)
        if M is None: return None
        return cv2.warpAffine(frame, M, (out, out),
                              flags=cv2.INTER_LINEAR,
                              borderMode=cv2.BORDER_REPLICATE)
    except Exception:
        return None


def crop_face(frame, box, out=ARCFACE_SIZE):
    H, W = frame.shape[:2]
    x1, y1, x2, y2 = box
    bw, bh = x2 - x1, y2 - y1
    pad = 0.10
    ix1 = max(0, int((x1 - bw * pad) * W))
    iy1 = max(0, int((y1 - bh * pad) * H))
    ix2 = min(W, int((x2 + bw * pad) * W))
    iy2 = min(H, int((y2 + bh * pad) * H))
    if ix2 <= ix1 or iy2 <= iy1:
        return np.zeros((out, out, 3), np.uint8)
    return cv2.resize(frame[iy1:iy2, ix1:ix2], (out, out),
                      interpolation=cv2.INTER_LINEAR)


def l2_norm(v):
    v = np.array(v, np.float32).flatten()
    n = np.linalg.norm(v)
    return v / n if n > 1e-6 else v


def recv_exact(conn, n):
    buf = b""
    while len(buf) < n:
        c = conn.recv(n - len(buf))
        if not c: return None
        buf += c
    return buf


# ── YOLOv8 output decoder ─────────────────────────────────────────────────────
def decode_yolov8(out: dict, thresh: float, unletterbox: bool) -> list:
    """
    YOLOv8s HailoRT NMS output.
    Raw output: dict with one key, value is list of 80 per-class arrays.
    Each array shape (N, 5): [y1, x1, y2, x2, score].
    Returns list of {"class_id","confidence","x1","y1","x2","y2"}.
    """
    dets = []
    for key, val in out.items():
        raw = val[0]

        # Fast path: homogeneous ndarray (num_classes, max_det, 5)
        try:
            arr = np.array(raw, dtype=np.float32)
            if arr.ndim == 3:
                num_cls, max_det, _ = arr.shape
                for cls_id in range(num_cls):
                    if cls_id == 0: continue  # skip person
                    for row in arr[cls_id]:
                        score = float(row[4])
                        if score < thresh: continue
                        y1, x1, y2, x2 = row[0], row[1], row[2], row[3]
                        if unletterbox:
                            y1 = _unletterbox_y(y1)
                            y2 = _unletterbox_y(y2)
                        x1 = max(0.0, float(x1)); y1 = max(0.0, float(y1))
                        x2 = min(1.0, float(x2)); y2 = min(1.0, float(y2))
                        if x2 > x1 and y2 > y1:
                            dets.append({"class_id": cls_id, "confidence": score,
                                         "x1": x1, "y1": y1, "x2": x2, "y2": y2})
                break
        except ValueError:
            pass

        # Slow path: jagged list
        for cls_id, cls_data in enumerate(raw):
            if cls_id == 0: continue
            if cls_data is None or (hasattr(cls_data, '__len__') and len(cls_data) == 0):
                continue
            try:
                cls_arr = np.array(cls_data, dtype=np.float32)
            except ValueError:
                cls_arr = np.array(list(cls_data), dtype=np.float32)
            if cls_arr.ndim == 1:
                cls_arr = cls_arr.reshape(-1, 5)
            for row in cls_arr:
                score = float(row[4])
                if score < thresh: continue
                y1, x1, y2, x2 = row[0], row[1], row[2], row[3]
                if unletterbox:
                    y1 = _unletterbox_y(y1)
                    y2 = _unletterbox_y(y2)
                x1 = max(0.0, float(x1)); y1 = max(0.0, float(y1))
                x2 = min(1.0, float(x2)); y2 = min(1.0, float(y2))
                if x2 > x1 and y2 > y1:
                    dets.append({"class_id": cls_id, "confidence": score,
                                 "x1": x1, "y1": y1, "x2": x2, "y2": y2})
        break
    return dets



# ── Hand gesture classification ───────────────────────────────────────────────
def _lm_finger_curled(lm, tip, pip) -> bool:
    """Curled: tip.y > pip.y - práh (tip je níže než PIP kloub).
    Y roste dolů — ohnutý prst má tip níže než PIP.
    Funguje bez ohledu na orientaci ruky v Z ose.
    """
    tip_y = float(lm[tip*3+1])
    pip_y = float(lm[pip*3+1])
    # Detekuj škálu — absolutní coords jsou > 1.0
    scale = max(abs(tip_y), abs(pip_y))
    thresh = 2.0 if scale > 1.0 else 0.01  # absolutní vs relativní
    return tip_y > pip_y + thresh

def _lm_thumb_up(lm) -> bool:
    """Thumb up — 2D metoda orientačně nezávislá."""
    palm_idx = [0, 5, 9, 13, 17]
    px = sum(lm[i*3]   for i in palm_idx) / 5
    py = sum(lm[i*3+1] for i in palm_idx) / 5
    tip_dist = ((lm[_THUMB_TIP*3]-px)**2 + (lm[_THUMB_TIP*3+1]-py)**2)**0.5
    mcp_dist = ((lm[_THUMB_MCP*3]-px)**2 + (lm[_THUMB_MCP*3+1]-py)**2)**0.5
    if mcp_dist < 1e-6:
        return False
    import logging as _lg
    _lg.getLogger("hailo").debug("[THUMB] tip_dist=%.3f mcp_dist=%.3f ratio=%.2f",
                                tip_dist, mcp_dist,
                                tip_dist/mcp_dist if mcp_dist > 0 else 0)
    return tip_dist > mcp_dist * 1.05

def classify_gesture(lm: np.ndarray) -> int:
    """lm: flat array of 63 floats (21 landmarks x,y,z).
    GESTURE_FIST      (1): 3+ fingers curled               = ✊ zavřená dlaň
    GESTURE_OPEN_HAND (2): 3+ fingers extended             = 🖐 otevřená dlaň
    GESTURE_THUMBS_UP (3): thumb up + 3+ fingers curled    = 👍 palec nahoru
    """
    # Zkontroluj škálu landmarků — pokud jsou příliš malé, přeskoč
    # Relativní koordináty mají wrist~0 a ostatní ~0.01-0.1
    # Absolutní koordináty mají wrist~0 a ostatní ~10-200
    lm_scale = max(abs(lm[_INDEX_TIP*3]), abs(lm[_INDEX_TIP*3+1]),
                   abs(lm[_RING_TIP*3]),  abs(lm[_RING_TIP*3+1]))
    if lm_scale < 0.005:
        # Degenerované landmarky — přeskoč
        return GESTURE_NONE

    # Spolehlivé prsty: jen index a ring (prostředník systematicky nespolehlivý)
    index = _lm_finger_curled(lm, _INDEX_TIP, _INDEX_PIP)
    ring  = _lm_finger_curled(lm, _RING_TIP,  _RING_PIP)
    # Prostředník a malíček jako doplňkový hlas
    middle = _lm_finger_curled(lm, _MIDDLE_TIP, _MIDDLE_PIP)
    pinky  = _lm_finger_curled(lm, _PINKY_TIP,  _PINKY_PIP)
    thumb_up = _lm_thumb_up(lm)

    import logging as _logging
    _idx_z = abs(float(lm[_INDEX_TIP*3+2]))
    _mid_z = abs(float(lm[_MIDDLE_TIP*3+2]))
    # Debug: vypočítej 3D vzdálenosti pro index
    _wx,_wy,_wz = lm[0],lm[1],lm[2]
    _tip3d = ((lm[_INDEX_TIP*3]-_wx)**2+(lm[_INDEX_TIP*3+1]-_wy)**2+(lm[_INDEX_TIP*3+2]-_wz)**2)**0.5
    _pip3d = ((lm[_INDEX_PIP*3]-_wx)**2+(lm[_INDEX_PIP*3+1]-_wy)**2+(lm[_INDEX_PIP*3+2]-_wz)**2)**0.5
    _logging.getLogger("hailo").info(
        "[LM_DBG] idx=%s mid=%s rng=%s pnk=%s thumb_up=%s idx_z=%.1f tip3d=%.1f pip3d=%.1f ratio=%.2f",
        'C' if index else 'E', 'C' if middle else 'E',
        'C' if ring else 'E', 'C' if pinky else 'E', thumb_up,
        _idx_z, _tip3d, _pip3d, _tip3d/_pip3d if _pip3d > 0 else 0)

    # Thumbs up: palec nahoře + idx_z > 15 (prsty od kamery)
    # open_hand ke kameře: idx_z = 0-5
    # thumbs_up (prsty od kamery): idx_z = 19-34
    if thumb_up:
        idx_z = abs(float(lm[_INDEX_TIP*3+2]))
        if idx_z > 12.0:
            return GESTURE_THUMBS_UP

    # Otevřená dlaň: index=E a ring=E
    if not index and not ring:
        return GESTURE_OPEN_HAND

    # Pěst: index=C a ring=C bez palce
    if index and ring and not thumb_up:
        return GESTURE_FIST

    return GESTURE_NONE

def landmarks_to_bbox(lm: np.ndarray, wrist_xy: tuple) -> tuple:
    """Convert relative landmarks to normalised bbox (x1,y1,x2,y2).
    wrist_xy: (wx, wy) normalised 0-1 position of wrist in frame.
    Returns (x1, y1, x2, y2) normalised 0-1.
    """
    pts = lm.reshape(21, 3)
    # Scale factor: landmarks span ~0.1 units for a hand filling ~0.3 of frame
    scale = 3.0
    xs = pts[:, 0] * scale + wrist_xy[0]
    ys = pts[:, 1] * scale + wrist_xy[1]
    pad = 0.05
    x1 = float(np.clip(xs.min() - pad, 0, 1))
    y1 = float(np.clip(ys.min() - pad, 0, 1))
    x2 = float(np.clip(xs.max() + pad, 0, 1))
    y2 = float(np.clip(ys.max() + pad, 0, 1))
    return (x1, y1, x2, y2)


# ── Inference engine ──────────────────────────────────────────────────────────
# # p3_hailo_cleaned
class CombinedEngine:

    def __init__(self):
        from hailo_platform import (
            VDevice, HailoSchedulingAlgorithm, HEF,
            ConfigureParams, InputVStreamParams, OutputVStreamParams,
            FormatType, HailoStreamInterface, InferVStreams,
        )
        self._IVS = InferVStreams

        params = VDevice.create_params()
        params.scheduling_algorithm = HailoSchedulingAlgorithm.ROUND_ROBIN
        self.vdevice = VDevice(params)
        log.info("VDevice opened (ROUND_ROBIN)")

        def _load(path):
            hef = HEF(path)
            cfg = ConfigureParams.create_from_hef(hef, HailoStreamInterface.PCIe)
            ng  = self.vdevice.configure(hef, cfg)[0]
            inp = InputVStreamParams.make(ng, format_type=FormatType.UINT8)
            out = OutputVStreamParams.make(ng, format_type=FormatType.FLOAT32)
            return ng, inp, out

        log.info("Loading SCRFD: %s", DETECT_HEF)
        self.det_ng, self.det_in, self.det_out = _load(DETECT_HEF)

        log.info("Loading ArcFace: %s", RECOG_HEF)
        self.rec_ng, self.rec_in, self.rec_out = _load(RECOG_HEF)

        obj_available = Path(OBJ_HEF).exists()
        self.obj_ng = self.obj_in = self.obj_out = None
        if obj_available:
            log.info("Loading YOLOv8s: %s", OBJ_HEF)
            self.obj_ng, self.obj_in, self.obj_out = _load(OBJ_HEF)
        else:
            log.warning("YOLOv8s HEF not found — object detection disabled")

        # ── Palm detection (stage 1) ──────────────────────────────
        palm_available = Path(PALM_HEF).exists()
        self.palm_ng = self.palm_in = self.palm_out = None
        self._palm_lock = threading.Lock()
        if palm_available:
            log.info('Loading PalmDetection: %s', PALM_HEF)
            self.palm_ng, self.palm_in, self.palm_out = _load(PALM_HEF)
        else:
            log.warning('Palm HEF not found — falling back to full-frame landmark')

        hand_available = Path(HAND_HEF).exists()
        self.hand_ng = self.hand_in = self.hand_out = None
        if hand_available:
            log.info("Loading HandLandmark: %s", HAND_HEF)
            self.hand_ng, self.hand_in, self.hand_out = _load(HAND_HEF)
        else:
            log.warning("Hand landmark HEF not found — gesture disabled")

        self._hand_lock = threading.Lock()
        self._sticky_palm_bbox = None
        self._sticky_palm_miss = 0
        self._STICKY_PALM_MAX  = 3  # max 3 framy sticky palm

        self._det_lock = threading.Lock()
        self._rec_lock = threading.Lock()
        self._obj_lock = threading.Lock()

    def open_pipelines(self):
        self._det_pipe = self._IVS(
            self.det_ng, self.det_in, self.det_out).__enter__()
        self._rec_pipe = self._IVS(
            self.rec_ng, self.rec_in, self.rec_out).__enter__()
        if self.obj_ng:
            self._obj_pipe = self._IVS(
                self.obj_ng, self.obj_in, self.obj_out).__enter__()
        else:
            self._obj_pipe = None

        if self.hand_ng:
            self._hand_pipe = self._IVS(
                self.hand_ng, self.hand_in, self.hand_out).__enter__()
            log.info("All four pipelines opened (SCRFD+ArcFace+YOLOv8+Hand)")
        else:
            self._hand_pipe = None
            log.info("Pipelines opened (no hand landmark pipeline)")

    def run_detect(self, frame):
        name = self.det_ng.get_input_vstream_infos()[0].name
        with self._det_lock:
            out = self._det_pipe.infer({name: frame[np.newaxis]})
        return {k: np.array(v[0], np.float32) for k, v in out.items()}

    def run_recognize(self, face):
        inp  = self.rec_ng.get_input_vstream_infos()[0].name
        outn = self.rec_ng.get_output_vstream_infos()[0].name
        with self._rec_lock:
            out = self._rec_pipe.infer({inp: face[np.newaxis]})
        return np.array(out[outn]).flatten().astype(np.float32)

    def run_objects(self, frame) -> dict | None:
        if not self._obj_pipe:
            return None
        name = self.obj_ng.get_input_vstream_infos()[0].name
        with self._obj_lock:
            out = self._obj_pipe.infer({name: frame[np.newaxis]})
        return {k: v for k, v in out.items()}


    def run_palm(self, frame: np.ndarray):
        """Run palm detection. frame: any size RGB.
        Returns list of (x1,y1,x2,y2) normalised palm bboxes, or [].
        """
        if not self.palm_ng:
            return []
        # Resize with aspect-ratio padding to 192x192
        h, w = frame.shape[:2]
        if h >= w:
            w1 = int(PALM_INPUT_SIZE * w / h); h1 = PALM_INPUT_SIZE
        else:
            h1 = int(PALM_INPUT_SIZE * h / w); w1 = PALM_INPUT_SIZE
        img = cv2.resize(frame, (w1, h1), interpolation=cv2.INTER_LINEAR)
        padh = PALM_INPUT_SIZE - h1; padw = PALM_INPUT_SIZE - w1
        img = np.pad(img, ((padh//2, padh-padh//2),
                           (padw//2, padw-padw//2), (0,0)))
        img = np.ascontiguousarray(img, dtype=np.uint8)
        scale = max(h, w) / PALM_INPUT_SIZE
        pad_x = (padw//2) * scale
        pad_y = (padh//2) * scale

        inp_name = list(self.palm_in.keys())[0]
        with self._palm_lock:
            with self._IVS(self.palm_ng, self.palm_in, self.palm_out) as pipe:
                raw = pipe.infer({inp_name: img[np.newaxis]})

        # Decode scores and boxes
        infos = self.palm_ng.get_output_vstream_infos() if hasattr(self.palm_ng, 'get_output_vstream_infos') else []
        # Use fixed output name mapping
        sc_large = np.array(raw.get('palm_detection_lite/conv29', raw[list(raw.keys())[1]])[0]).reshape(-1)
        sc_small = np.array(raw.get('palm_detection_lite/conv24', raw[list(raw.keys())[0]])[0]).reshape(-1)
        bx_large = np.array(raw.get('palm_detection_lite/conv30', raw[list(raw.keys())[3]])[0]).reshape(-1, 18)
        bx_small = np.array(raw.get('palm_detection_lite/conv25', raw[list(raw.keys())[2]])[0]).reshape(-1, 18)

        _sig = lambda x: 1.0/(1.0+np.exp(-np.clip(x,-50,50)))
        scores = _sig(np.concatenate([sc_large, sc_small]))
        boxes  = np.concatenate([bx_large, bx_small], axis=0)

        # Anchor decode — BLAZE_PALM_ANCHORS_V1 (lokální modul, dřív externí
        # blaze_app). BlazeDetector se nikdy nepoužíval, dekód je inline níže.
        # Robustní import: server běží jako `python scripts/hailo_inference_server.py`
        # (path[0]=scripts/ → bare), ale může být i importován jako scripts.*.
        try:
            from blaze_palm_anchors import palm_anchors
        except ImportError:
            from scripts.blaze_palm_anchors import palm_anchors
        anchors = palm_anchors()

        log.debug('[PALM_DBG] max_score=%.4f thresh=%.2f', scores.max(), PALM_SCORE_THRESH)
        mask = scores >= PALM_SCORE_THRESH
        if not mask.any():
            return []

        sc = scores[mask]
        bx = boxes[mask]
        an = anchors[mask]

        # Decode boxes: cx,cy,w,h relative to anchor
        cx = bx[:,0] / PALM_INPUT_SIZE * an[:,2] + an[:,0]
        cy = bx[:,1] / PALM_INPUT_SIZE * an[:,3] + an[:,1]
        bw = bx[:,2] / PALM_INPUT_SIZE * an[:,2]
        bh = bx[:,3] / PALM_INPUT_SIZE * an[:,3]
        x1 = np.clip(cx - bw/2, 0, 1)
        y1 = np.clip(cy - bh/2, 0, 1)
        x2 = np.clip(cx + bw/2, 0, 1)
        y2 = np.clip(cy + bh/2, 0, 1)

        # Map back to frame coords
        results = []
        for i in range(len(sc)):
            results.append([
                float(x1[i] * PALM_INPUT_SIZE * scale - pad_x) / w,
                float(y1[i] * PALM_INPUT_SIZE * scale - pad_y) / h,
                float(x2[i] * PALM_INPUT_SIZE * scale - pad_x) / w,
                float(y2[i] * PALM_INPUT_SIZE * scale - pad_y) / h,
            ])
        # Return best detection only
        best = results[np.argmax(sc)]
        return [best]

    def run_hand(self, frame: np.ndarray):
        """Two-stage: palm detect → crop → landmark. frame: any size RGB.
        Returns (gesture_id, landmarks_or_None).
        """
        if not self._hand_pipe:
            return GESTURE_NONE, None

        # Stage 1: palm detection
        palms = self.run_palm(frame)
        log.debug('[PALM] detected=%d', len(palms))
        palm_bbox = None
        if palms:
            px1, py1, px2, py2 = palms[0]
            palm_bbox = (px1, py1, px2, py2)
            self._sticky_palm_bbox = palm_bbox
            self._sticky_palm_miss = 0
            h, w = frame.shape[:2]
            bw = px2 - px1; bh = py2 - py1
            # Asymetrický padding — prsty jsou nad dlaní
            cx1 = max(0.0, px1 - bw * 0.5)
            cy1 = max(0.0, py1 - bh * 2.0)   # hodně nahoru pro prsty
            cx2 = min(1.0, px2 + bw * 0.5)
            cy2 = min(1.0, py2 + bh * 0.5)
            ix1 = int(cx1*w); iy1 = int(cy1*h)
            ix2 = int(cx2*w); iy2 = int(cy2*h)
            if ix2 > ix1 and iy2 > iy1:
                crop = frame[iy1:iy2, ix1:ix2]
            else:
                crop = frame
        else:
            # No palm — zkus sticky bbox
            self._sticky_palm_miss += 1
            if (self._sticky_palm_bbox is not None and
                    self._sticky_palm_miss <= self._STICKY_PALM_MAX):
                px1,py1,px2,py2 = self._sticky_palm_bbox
                palm_bbox = self._sticky_palm_bbox
                h, w = frame.shape[:2]
                bw = px2-px1; bh = py2-py1
                cx1 = max(0.0, px1 - bw*0.5)
                cy1 = max(0.0, py1 - bh*2.0)
                cx2 = min(1.0, px2 + bw*0.5)
                cy2 = min(1.0, py2 + bh*0.5)
                ix1=int(cx1*w); iy1=int(cy1*h)
                ix2=int(cx2*w); iy2=int(cy2*h)
                if ix2>ix1 and iy2>iy1:
                    crop = frame[iy1:iy2, ix1:ix2]
                else:
                    crop = frame
            else:
                self._lm_ema = None
                return GESTURE_NONE, None, None

        # Stage 2: hand landmark on crop
        frame_224 = cv2.resize(crop, (HAND_INPUT_SIZE, HAND_INPUT_SIZE),
                               interpolation=cv2.INTER_LINEAR)
        frame_224 = np.ascontiguousarray(frame_224, dtype=np.uint8)

        inp_name = self.hand_ng.get_input_vstream_infos()[0].name
        with self._hand_lock:
            out = self._hand_pipe.infer({inp_name: frame_224[np.newaxis]})
        results = {k: np.array(v[0]).flatten() for k, v in out.items()}

        best_lm = None; best_score = -1.0
        for lm_key, sc_key in [
            ('hand_landmark_lite/fc1', 'hand_landmark_lite/fc2'),
            ('hand_landmark_lite/fc3', 'hand_landmark_lite/fc4'),
        ]:
            if lm_key not in results: continue
            score = float(results[sc_key][0])
            if score > best_score:
                best_score = score; best_lm = results[lm_key]

        log.debug('[HAND] palm=%s best_score=%.4f thresh=%.2f',
                 bool(palms), best_score, HAND_PRESENCE_THRESH)

        log.info("[HAND] proceeding with score=%.4f", best_score)
        if best_lm is None or best_score < HAND_PRESENCE_THRESH:
            return GESTURE_NONE, None, palm_bbox

        if not hasattr(self, '_lm_ema') or self._lm_ema is None:
            self._lm_ema = best_lm.copy()
            self._lm_alpha = 0.4
        else:
            self._lm_ema = self._lm_alpha * best_lm + (1 - self._lm_alpha) * self._lm_ema
        best_lm = self._lm_ema
        lm_r = best_lm.reshape(21,3)
        log.debug('[LM] idx z=%.3f pip z=%.3f mid z=%.3f pip z=%.3f thumb y=%.3f mcp y=%.3f',
                 lm_r[8,2],lm_r[6,2],lm_r[12,2],lm_r[10,2],lm_r[4,1],lm_r[2,1])
        # Lazy load ML modelu při první inferenci
        global _ML_MODEL
        if _ML_MODEL is None:
            _load_ml_model()
        # Zkus ML klasifikátor, fallback na geometrii
        gid = _ml_classify(best_lm)
        if gid is None:
            gid = classify_gesture(best_lm)
        log.debug('[HAND] gesture_id=%d', gid)
        return gid, best_lm, palm_bbox
    def close(self):
        try: self.vdevice.release()
        except Exception: pass


# ── Per-client handler ────────────────────────────────────────────────────────
def handle_gesture_client(conn, engine: CombinedEngine, lock: threading.Lock):
    """Handler for dedicated gesture socket — no HAND_MAGIC prefix needed."""
    print("[GESTURE_HANDLER] client connected", flush=True)
    try:
        while True:
            sz_hdr = recv_exact(conn, 4)
            if not sz_hdr:
                break  # klient se odpojil
            size = struct.unpack('>I', sz_hdr)[0]
            print(f"[GESTURE_HANDLER] frame size={size}", flush=True)
            if size > 1920 * 1080 * 3 * 2:  # allow up to 1920x1080
                print(f"[GESTURE_HANDLER] size too large: {size}", flush=True)
                break
            raw = recv_exact(conn, size)
            if not raw:
                print("[GESTURE_HANDLER] no raw — disconnecting", flush=True)
                break
            wh = recv_exact(conn, 4)
            if not wh:
                print("[GESTURE_HANDLER] no wh — disconnecting", flush=True)
                break
            w, h = struct.unpack('>HH', wh)
            print(f"[GESTURE_HANDLER] w={w} h={h}", flush=True)
            frame = np.frombuffer(raw, dtype=np.uint8).reshape(h, w, 3)
            # run_hand now handles resize internally (two-stage pipeline)
            result = engine.run_hand(frame)
            gesture_id, lm = result[0], result[1]
            palm_bbox = result[2] if len(result) > 2 else None
            print(f"[GESTURE_HANDLER] gesture_id={gesture_id}", flush=True)
            # Send gesture_id + bbox + landmarks
            conn.sendall(struct.pack('B', gesture_id))
            if palm_bbox is not None and gesture_id != GESTURE_NONE:
                x1,y1,x2,y2 = palm_bbox
            else:
                x1,y1,x2,y2 = 0.0,0.0,0.0,0.0
            conn.sendall(struct.pack('>ffff', x1, y1, x2, y2))
            # Send 63 floats landmarks (21 × xyz), or zeros if none
            if lm is not None and gesture_id != GESTURE_NONE:
                import numpy as _np
                lm_arr = _np.array(lm, dtype=_np.float32).flatten()[:63]
                if len(lm_arr) == 63:
                    conn.sendall(struct.pack('>63f', *lm_arr))
                else:
                    conn.sendall(struct.pack('>63f', *([0.0]*63)))
            else:
                conn.sendall(struct.pack('>63f', *([0.0]*63)))
    except Exception as e:
        print(f"[GESTURE_HANDLER] exception: {e}", flush=True)
        log.debug('Gesture client disconnected: %s', e)
    finally:
        try: conn.close()
        except: pass


def handle_client(conn, engine: CombinedEngine, lock: threading.Lock):
    log.info("Client connected")
    fb_lores = LORES_H * LORES_W * 3
    fb_640   = DET_H   * DET_W   * 3

    try:
        while True:
            hdr = recv_exact(conn, 4)
            if hdr is None: break

            # ── Mode 2: embed only ────────────────────────────────────────
            if hdr == EMBED_MAGIC:
                n_hdr = recv_exact(conn, 4)
                if n_hdr is None: break
                n = struct.unpack(">I", n_hdr)[0]
                if n == 0:
                    conn.sendall(struct.pack(">I", 0)); continue
                crop_bytes = ARCFACE_SIZE * ARCFACE_SIZE * 3
                embs = np.zeros((n, ARCFACE_DIM), np.float32)
                ok = True
                for i in range(n):
                    raw = recv_exact(conn, crop_bytes)
                    if raw is None: ok = False; break
                    face = np.frombuffer(raw, np.uint8).reshape(
                        ARCFACE_SIZE, ARCFACE_SIZE, 3).copy()
                    try:
                        with lock:
                            embs[i] = l2_norm(engine.run_recognize(
                                np.ascontiguousarray(face)))
                    except Exception as e:
                        log.warning("embed face %d: %s", i, e)
                if not ok: break
                conn.sendall(struct.pack(">I", n))
                conn.sendall(embs.tobytes())
                continue

            # ── Mode 3: object detection ──────────────────────────────────
            if hdr == OBJ_MAGIC:
                sz_hdr = recv_exact(conn, 4)
                if sz_hdr is None: break
                size = struct.unpack(">I", sz_hdr)[0]
                raw  = recv_exact(conn, size)
                if raw is None: break

                if size == fb_lores:
                    f_lores = np.frombuffer(raw, np.uint8).reshape(
                        LORES_H, LORES_W, 3).copy()
                    frame = _letterbox(f_lores)
                    lores = True
                else:
                    frame = np.frombuffer(raw, np.uint8).reshape(
                        DET_H, DET_W, 3).copy()
                    lores = False

                try:
                    with lock:
                        obj_out = engine.run_objects(frame)
                    if obj_out is None:
                        conn.sendall(struct.pack(">I", 0)); continue

                    dets = decode_yolov8(obj_out, OBJ_SCORE_THRESH, lores)
                    N = len(dets)
                    if N: log.info("Objects: %d", N)
                    conn.sendall(struct.pack(">I", N))
                    if N:
                        arr = np.array([[d["x1"], d["y1"], d["x2"], d["y2"],
                                         d["confidence"], float(d["class_id"])]
                                        for d in dets], np.float32)
                        conn.sendall(arr.tobytes())
                except Exception as e:
                    import traceback
                    log.error("Object inference: %s\n%s", e, traceback.format_exc())
                    conn.sendall(struct.pack(">I", 0))
                continue

            # ── Mode 4: hand gesture ──────────────────────────────────────
            if hdr == HAND_MAGIC:
                sz_hdr = recv_exact(conn, 4)
                if sz_hdr is None: break
                size = struct.unpack(">I", sz_hdr)[0]
                raw  = recv_exact(conn, size)
                if raw is None: break
                wh = recv_exact(conn, 4)
                if wh is None: break
                w, h = struct.unpack(">HH", wh)
                try:
                    frame = np.frombuffer(raw, np.uint8).reshape(h, w, 3)
                    # Předej původní frame — run_hand si dělá resize interně
                    with lock:
                        gesture = engine.run_hand(frame)
                    conn.sendall(struct.pack('B', gesture))
                except Exception as e:
                    log.error("Hand gesture error: %s", e)
                    conn.sendall(struct.pack('B', GESTURE_NONE))
                continue

            # ── Mode 1: detect + embed (face) ─────────────────────────────
            size = struct.unpack(">I", hdr)[0]
            if size == fb_lores:
                raw = recv_exact(conn, size)
                if raw is None: break
                f_lores = np.frombuffer(raw, np.uint8).reshape(
                    LORES_H, LORES_W, 3).copy()
                frame = _letterbox(f_lores)
                lores = True
            elif size == fb_640:
                raw = recv_exact(conn, size)
                if raw is None: break
                frame = np.frombuffer(raw, np.uint8).reshape(
                    DET_H, DET_W, 3).copy()
                lores = False
            else:
                log.warning("Bad frame size %d (expected %d or %d)",
                            size, fb_lores, fb_640)
                conn.sendall(struct.pack(">I", 0)); continue

            try:
                with lock:
                    det_raw = engine.run_detect(frame)
                boxes, scores = decode_scrfd(det_raw)
                boxes, scores = nms_filter(boxes, scores)
                if lores:
                    boxes = _unletterbox_boxes(boxes)
                N = len(boxes)
                if N: log.info("Faces: %d  scores=%s", N, scores.round(2).tolist())
                if N == 0:
                    conn.sendall(struct.pack(">I", 0)); continue

                # ── Landmarks ─────────────────────────────────────────────
                lm_list = [None] * N
                try:
                    all_lm = []; all_an = []
                    for stride in SCRFD_STRIDES:
                        lk = SCRFD_LM_LAYERS[stride]
                        bk = SCRFD_LAYERS[stride][1]
                        if lk not in det_raw or bk not in det_raw: continue
                        all_lm.append(np.array(det_raw[lk], np.float32).reshape(-1, 10))
                        all_an.append(_ANCHORS[stride])
                    if all_lm:
                        all_lm = np.vstack(all_lm)
                        all_an = np.vstack(all_an)
                        scale, pad_left, pad_top = _get_letterbox_params(LORES_W, LORES_H)
                        for fi, box in enumerate(boxes):
                            cx = (box[0] + box[2]) / 2
                            cy = (box[1] + box[3]) / 2
                            acx = all_an[:, 0] / DET_W
                            acy = all_an[:, 1] / DET_H
                            best = int(np.argmin((acx - cx) ** 2 + (acy - cy) ** 2))
                            sz   = all_an[best, 2]
                            st   = 8 if sz <= 32 else (16 if sz <= 128 else 32)
                            deltas = all_lm[best].reshape(5, 2)
                            pts = np.zeros((5, 2), np.float32)
                            for k in range(5):
                                pts[k, 0] = (all_an[best, 0] + deltas[k, 0] * st) / DET_W
                                pts[k, 1] = (all_an[best, 1] + deltas[k, 1] * st) / DET_H
                            pts = np.clip(pts, 0, 1)
                            if lores:
                                # Unletterbox landmark coords back to lores space
                                pts[:, 0] = np.clip(
                                    (pts[:, 0] * DET_W - pad_left) / (scale * LORES_W),
                                    0.0, 1.0)
                                pts[:, 1] = np.clip(
                                    (pts[:, 1] * DET_H - pad_top) / (scale * LORES_H),
                                    0.0, 1.0)
                            lm_list[fi] = pts
                except Exception:
                    pass

                # ── ArcFace embeddings ─────────────────────────────────────
                embs = np.zeros((N, ARCFACE_DIM), np.float32)
                for i, box in enumerate(boxes):
                    fc = None
                    if lm_list[i] is not None:
                        try:
                            fc = align_face(frame, lm_list[i])
                        except Exception:
                            fc = None
                    if fc is None:
                        fc = crop_face(frame, box.tolist())
                    with lock:
                        embs[i] = l2_norm(engine.run_recognize(
                            np.ascontiguousarray(fc, np.uint8)))

                labels = np.zeros(N, np.uint8)
                conn.sendall(struct.pack(">I", N))
                conn.sendall(boxes.tobytes())
                conn.sendall(embs.tobytes())
                conn.sendall(labels.tobytes())

            except Exception as e:
                import traceback
                log.error("Face inference: %s\n%s", e, traceback.format_exc())
                try: conn.sendall(struct.pack(">I", 0))
                except Exception: pass

    except Exception as e:
        log.error("Client error: %s", e)
    finally:
        conn.close()
        log.info("Client disconnected")


# ── Server ────────────────────────────────────────────────────────────────────
def run_server():
    log.info("Combined server: SCRFD + ArcFace + YOLOv8s + HandLandmark")
    log.info("Lores: %dx%d  Det: %dx%d", LORES_W, LORES_H, DET_W, DET_H)

    for desc, path in [("SCRFD", DETECT_HEF), ("ArcFace", RECOG_HEF)]:
        if not Path(path).exists():
            log.error("Missing %s: %s", desc, path)
            sys.exit(1)

    if not Path(OBJ_HEF).exists():
        log.warning("YOLOv8s HEF not found: %s — object detection disabled", OBJ_HEF)

    if os.path.exists(SOCK_PATH):
        os.remove(SOCK_PATH)

    log.info("Initialising engine...")
    try:
        engine = CombinedEngine()
        engine.open_pipelines()
    except Exception as e:
        import traceback
        log.error("Init failed: %s\n%s", e, traceback.format_exc())
        sys.exit(1)

    lock = threading.Lock()
    srv  = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(SOCK_PATH)
    srv.listen(4)

    # ── Dedicated gesture socket ──────────────────────────────────
    try: Path(GESTURE_SOCK).unlink()
    except FileNotFoundError: pass
    gesture_srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    gesture_srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    gesture_srv.bind(GESTURE_SOCK)
    gesture_srv.listen(4)
    os.chmod(GESTURE_SOCK, 0o666)
    log.info('Gesture socket ready: %s', GESTURE_SOCK)
    def _gesture_acceptor():
        while True:
            try:
                c, _ = gesture_srv.accept()
                threading.Thread(target=handle_gesture_client,
                                args=(c, engine, lock),
                                daemon=True).start()
            except Exception as e:
                log.debug('Gesture acceptor error: %s', e)
                break
    threading.Thread(target=_gesture_acceptor, daemon=True).start()
    os.chmod(SOCK_PATH, 0o666)

    log.info("Ready on %s", SOCK_PATH)
    log.info("Mode 1: face detect+embed | Mode 2: embed-only | Mode 3: object detect")

    try:
        while True:
            conn, _ = srv.accept()
            threading.Thread(target=handle_client,
                             args=(conn, engine, lock),
                             daemon=True).start()
    except KeyboardInterrupt:
        pass
    finally:
        srv.close()
        try: os.remove(SOCK_PATH)
        except OSError: pass
        engine.close()


if __name__ == "__main__":
    run_server()
