#!/usr/bin/env python3.13
"""
Hailo Gesture Server — dvoustupňový pipeline na Hailo8L
=======================================================
Stupeň 1: palm_detection_lite  (192×192) — najde bbox ruky v plném framu
Stupeň 2: hand_landmark_lite   (224×224) — spustí se jen na oříznuté ruce

Socket: /tmp/gesture.sock

Protokol:
  Client → server : uint32 n_bytes + RGB frame bytes + uint16 W + uint16 H
  Server → client : uint8 gesture_id + 4× float32 bbox (x1,y1,x2,y2)
                    gesture_id: 0=none  1=fist  2=open_hand  3=thumbs_up
                    bbox: normalizované 0-1, nebo 0,0,0,0 pokud žádná ruka
"""

import os, sys, json, socket, struct, logging, threading
import numpy as np
import cv2
from pathlib import Path

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [GESTURE] %(message)s")
log = logging.getLogger(__name__)

SOCK_PATH         = "/tmp/gesture.sock"
PALM_INPUT_SIZE   = 192
LAND_INPUT_SIZE   = 224

GESTURE_NONE      = 0
GESTURE_FIST      = 1
GESTURE_OPEN_HAND = 2
GESTURE_THUMBS_UP = 3

# MediaPipe hand landmark indices
_WRIST      = 0
_THUMB_TIP  = 4;  _THUMB_MCP  = 2
_INDEX_TIP  = 8;  _INDEX_PIP  = 6
_MIDDLE_TIP = 12; _MIDDLE_PIP = 10
_RING_TIP   = 16; _RING_PIP   = 14
_PINKY_TIP  = 20; _PINKY_PIP  = 18


# ── Config ────────────────────────────────────────────────────────────────────

def _load_config():
    search = Path(__file__).resolve().parent
    for _ in range(4):
        c = search / "config.json"
        if c.exists():
            try:
                return json.load(open(c))
            except Exception:
                pass
        search = search.parent
    return {}


# ── Gesture classification ────────────────────────────────────────────────────

def _finger_up(lm, tip, pip) -> bool:
    return float(lm[tip][1]) < float(lm[pip][1]) - 0.02

def _thumb_up(lm) -> bool:
    return float(lm[_THUMB_TIP][1]) < float(lm[_THUMB_MCP][1]) - 0.05

def classify(lm: np.ndarray) -> int:
    """lm: (21, 3) normalised x,y,z"""
    index  = _finger_up(lm, _INDEX_TIP,  _INDEX_PIP)
    middle = _finger_up(lm, _MIDDLE_TIP, _MIDDLE_PIP)
    ring   = _finger_up(lm, _RING_TIP,   _RING_PIP)
    pinky  = _finger_up(lm, _PINKY_TIP,  _PINKY_PIP)
    thumb  = _thumb_up(lm)
    up     = sum([index, middle, ring, pinky])

    if up >= 3:
        return GESTURE_OPEN_HAND
    if up == 0 and not thumb:
        return GESTURE_FIST
    if thumb and up == 0:
        return GESTURE_THUMBS_UP
    return GESTURE_NONE


# ── Palm detection decoder ────────────────────────────────────────────────────

# Anchors pre-computed for 192×192 palm_detection_lite
# (same as MediaPipe BlazePalm)
def _make_anchors():
    """Generate SSD anchors matching palm_detection_lite 192×192."""
    anchors = []
    # Large scale: 24×24 grid, 2 anchors per cell
    for y in range(24):
        for x in range(24):
            cx = (x + 0.5) / 24.0
            cy = (y + 0.5) / 24.0
            for _ in range(2):
                anchors.append([cx, cy])
    # Small scale: 12×12 grid, 6 anchors per cell
    for y in range(12):
        for x in range(12):
            cx = (x + 0.5) / 12.0
            cy = (y + 0.5) / 12.0
            for _ in range(6):
                anchors.append([cx, cy])
    return np.array(anchors, dtype=np.float32)  # (2016, 2)

_ANCHORS = _make_anchors()   # built once at import

def _sigmoid(x):
    return 1.0 / (1.0 + np.exp(-np.clip(x, -20, 20)))

def _decode_palm_boxes(raw_boxes, raw_scores, score_thresh=0.5):
    """
    raw_boxes : (2016, 18)  — tx,ty,tw,th + 7 keypoints×2
    raw_scores: (2016, 1)   — logits
    Returns list of [x1,y1,x2,y2,score] normalised 0-1.
    """
    scores = _sigmoid(raw_scores[:, 0])
    mask   = scores >= score_thresh
    if not mask.any():
        return []

    idx    = np.where(mask)[0]
    anc    = _ANCHORS[idx]          # (N, 2)
    boxes  = raw_boxes[idx]         # (N, 18)
    sc     = scores[idx]

    # Decode centre
    cx = boxes[:, 0] / 192.0 + anc[:, 0]
    cy = boxes[:, 1] / 192.0 + anc[:, 1]
    w  = boxes[:, 2] / 192.0
    h  = boxes[:, 3] / 192.0

    x1 = cx - w / 2;  y1 = cy - h / 2
    x2 = cx + w / 2;  y2 = cy + h / 2

    result = []
    for i in range(len(idx)):
        result.append([
            float(np.clip(x1[i], 0, 1)),
            float(np.clip(y1[i], 0, 1)),
            float(np.clip(x2[i], 0, 1)),
            float(np.clip(y2[i], 0, 1)),
            float(sc[i]),
        ])
    return result

def _nms(boxes, iou_thresh=0.3):
    """Simple greedy NMS. boxes: list of [x1,y1,x2,y2,score]."""
    if not boxes:
        return []
    boxes  = sorted(boxes, key=lambda b: b[4], reverse=True)
    kept   = []
    while boxes:
        best = boxes.pop(0)
        kept.append(best)
        def iou(a, b):
            ix1 = max(a[0], b[0]); iy1 = max(a[1], b[1])
            ix2 = min(a[2], b[2]); iy2 = min(a[3], b[3])
            inter = max(0, ix2-ix1) * max(0, iy2-iy1)
            ua = (a[2]-a[0])*(a[3]-a[1]) + (b[2]-b[0])*(b[3]-b[1]) - inter
            return inter / ua if ua > 0 else 0
        boxes = [b for b in boxes if iou(best, b) < iou_thresh]
    return kept


# ── Hailo engine ──────────────────────────────────────────────────────────────

class GestureEngine:
    """Drží oba modely na stejném VDevice (ROUND_ROBIN)."""

    def __init__(self, palm_hef_path: str, land_hef_path: str,
                 palm_score_thresh: float = 0.5,
                 presence_thresh:   float = 0.5):
        from hailo_platform import (
            VDevice, HailoSchedulingAlgorithm, HEF,
            ConfigureParams, HailoStreamInterface,
            InputVStreamParams, OutputVStreamParams,
            FormatType, InferVStreams,
        )
        self._IVS        = InferVStreams
        self._palm_thresh = palm_score_thresh
        self._pres_thresh = presence_thresh

        params = VDevice.create_params()
        params.scheduling_algorithm = HailoSchedulingAlgorithm.ROUND_ROBIN
        self.vdevice = VDevice(params)

        def _load(path):
            hef = HEF(path)
            cfg = ConfigureParams.create_from_hef(hef, HailoStreamInterface.PCIe)
            ng  = self.vdevice.configure(hef, cfg)[0]
            inp = InputVStreamParams.make(ng, format_type=FormatType.UINT8)
            out = OutputVStreamParams.make(ng, format_type=FormatType.FLOAT32)
            return ng, inp, out

        self._palm_ng,  self._palm_inp,  self._palm_out  = _load(palm_hef_path)
        self._land_ng,  self._land_inp,  self._land_out  = _load(land_hef_path)
        self._lock = threading.Lock()
        log.info("GestureEngine ready  palm=%s  land=%s", palm_hef_path, land_hef_path)

    # ── Palm detection ────────────────────────────────────────────────────────

    def detect_palms(self, frame: np.ndarray) -> list:
        """frame: any size RGB. Returns list of [x1,y1,x2,y2,score]."""
        img = cv2.resize(frame, (PALM_INPUT_SIZE, PALM_INPUT_SIZE),
                         interpolation=cv2.INTER_LINEAR)
        img = np.ascontiguousarray(img, dtype=np.uint8)

        inp_name = self._palm_ng.get_input_vstream_infos()[0].name
        with self._lock:
            with self._IVS(self._palm_ng,
                           self._palm_inp,
                           self._palm_out) as pipe:
                raw = pipe.infer({inp_name: img[np.newaxis]})

        # Collect outputs — shapes confirmed from HEF probe:
        # conv24 (12,12,6)=small scores  conv29 (24,24,2)=large scores
        # conv25 (12,12,108)=small boxes  conv30 (24,24,36)=large boxes
        r = {k: np.array(v[0]) for k, v in raw.items()}

        scores_large = r["palm_detection_lite/conv29"].reshape(-1, 1)   # (1152,1)
        scores_small = r["palm_detection_lite/conv24"].reshape(-1, 1)   # (864, 1) — 12*12*6
        boxes_large  = r["palm_detection_lite/conv30"].reshape(-1, 18)  # (1152,18)
        boxes_small  = r["palm_detection_lite/conv25"].reshape(-1, 18)  # (864, 18)

        all_scores = np.concatenate([scores_large, scores_small], axis=0)  # (2016,1)
        all_boxes  = np.concatenate([boxes_large,  boxes_small],  axis=0)  # (2016,18)

        detections = _decode_palm_boxes(all_boxes, all_scores, self._palm_thresh)
        return _nms(detections)

    # ── Hand landmark ─────────────────────────────────────────────────────────

    def hand_landmarks(self, crop224: np.ndarray):
        """
        crop224: 224×224 RGB uint8.
        Returns (lm_21x3, gesture_id) or (None, GESTURE_NONE).
        """
        img = np.ascontiguousarray(crop224, dtype=np.uint8)
        inp_name = self._land_ng.get_input_vstream_infos()[0].name
        with self._lock:
            with self._IVS(self._land_ng,
                           self._land_inp,
                           self._land_out) as pipe:
                raw = pipe.infer({inp_name: img[np.newaxis]})

        r = {k: np.array(v[0]).flatten() for k, v in raw.items()}

        # fc1/fc2 = right hand,  fc3/fc4 = left hand
        best_lm    = None
        best_score = -1.0
        for lm_key, sc_key in [
            ("hand_landmark_lite/fc1", "hand_landmark_lite/fc2"),
            ("hand_landmark_lite/fc3", "hand_landmark_lite/fc4"),
        ]:
            if lm_key not in r or sc_key not in r:
                continue
            score = float(r[sc_key][0])
            if score > best_score:
                best_score = score
                best_lm    = r[lm_key]

        if best_lm is None or best_score < self._pres_thresh:
            log.debug("No hand — best_score=%.3f thresh=%.3f",
                      best_score if best_lm is not None else -1, self._pres_thresh)
            return None, GESTURE_NONE

        lm = best_lm.reshape(21, 3)
        return lm, classify(lm)

    # ── Full pipeline ─────────────────────────────────────────────────────────

    def run(self, frame: np.ndarray):
        """
        Full two-stage pipeline on one frame.
        Returns (gesture_id, bbox_or_None)
          bbox = (x1,y1,x2,y2) normalised, or None if no hand detected.
        """
        palms = self.detect_palms(frame)
        if not palms:
            return GESTURE_NONE, None

        # Pick highest-score palm
        best = max(palms, key=lambda p: p[4])
        x1, y1, x2, y2, score = best
        log.debug("Palm score=%.3f  box=(%.2f,%.2f,%.2f,%.2f)", score, x1,y1,x2,y2)

        # Crop with padding
        cfg     = self._cfg if hasattr(self, '_cfg') else {}
        padding = float(cfg.get("palm_padding", 0.4))
        H, W    = frame.shape[:2]
        bw = x2 - x1;  bh = y2 - y1
        cx1 = max(0.0, x1 - bw * padding)
        cy1 = max(0.0, y1 - bh * padding)
        cx2 = min(1.0, x2 + bw * padding)
        cy2 = min(1.0, y2 + bh * padding)

        ix1 = int(cx1 * W);  iy1 = int(cy1 * H)
        ix2 = int(cx2 * W);  iy2 = int(cy2 * H)
        if ix2 <= ix1 or iy2 <= iy1:
            return GESTURE_NONE, (x1, y1, x2, y2)

        crop = frame[iy1:iy2, ix1:ix2]
        crop224 = cv2.resize(crop, (LAND_INPUT_SIZE, LAND_INPUT_SIZE),
                             interpolation=cv2.INTER_LINEAR)

        lm, gesture = self.hand_landmarks(crop224)
        bbox = (x1, y1, x2, y2)
        return gesture, bbox

    def close(self):
        try:
            self.vdevice.release()
        except Exception:
            pass


# ── Socket helpers ────────────────────────────────────────────────────────────

def recv_exact(conn, n: int) -> bytes | None:
    buf = b""
    while len(buf) < n:
        c = conn.recv(n - len(buf))
        if not c:
            return None
        buf += c
    return buf


def handle_client(conn, engine: GestureEngine):
    log.info("Client connected")
    try:
        while True:
            hdr = recv_exact(conn, 4)
            if hdr is None:
                break
            size = struct.unpack(">I", hdr)[0]
            raw  = recv_exact(conn, size)
            if raw is None:
                break
            wh = recv_exact(conn, 4)
            if wh is None:
                break
            w, h = struct.unpack(">HH", wh)

            frame = np.frombuffer(raw, np.uint8).reshape(h, w, 3)

            gesture, bbox = engine.run(frame)

            # Send gesture byte + bbox floats (0,0,0,0 if no bbox)
            conn.sendall(struct.pack("B", gesture))
            if bbox:
                conn.sendall(struct.pack(">ffff", *bbox))
            else:
                conn.sendall(struct.pack(">ffff", 0.0, 0.0, 0.0, 0.0))

    except Exception as e:
        log.error("Client error: %s", e)
    finally:
        conn.close()
        log.info("Client disconnected")


# ── Server entry point ────────────────────────────────────────────────────────

def run_server():
    cfg  = _load_config()
    gcfg = cfg.get("gesture", {})

    palm_hef  = gcfg.get("palm_detection_hef",  "resources/palm_detection_lite.hef")
    land_hef  = gcfg.get("hand_landmark_hef",   "resources/hand_landmark_lite.hef")
    palm_score = float(gcfg.get("palm_score_threshold", 0.3))
    presence   = float(gcfg.get("presence_threshold",  0.3))

    for path in [palm_hef, land_hef]:
        if not Path(path).exists():
            log.error("HEF not found: %s", path)
            sys.exit(1)

    log.info("Loading models — palm=%s  land=%s", palm_hef, land_hef)
    log.info("Thresholds — palm_score=%.2f  presence=%.2f", palm_score, presence)

    try:
        engine = GestureEngine(palm_hef, land_hef,
                               palm_score_thresh=palm_score,
                               presence_thresh=presence)
        engine._cfg = gcfg   # expose config to run()
    except Exception as e:
        import traceback
        log.error("Engine init failed: %s\n%s", e, traceback.format_exc())
        sys.exit(1)

    if os.path.exists(SOCK_PATH):
        os.remove(SOCK_PATH)

    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(SOCK_PATH)
    srv.listen(2)
    os.chmod(SOCK_PATH, 0o666)
    log.info("Gesture server ready on %s", SOCK_PATH)

    # Warm-up inference — eliminuje první-frame latenci
    try:
        dummy = np.zeros((480, 640, 3), dtype=np.uint8)
        engine.run(dummy)
        log.info("Warm-up done")
    except Exception as e:
        log.warning("Warm-up failed (non-fatal): %s", e)

    try:
        while True:
            conn, _ = srv.accept()
            threading.Thread(target=handle_client,
                             args=(conn, engine),
                             daemon=True).start()
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
