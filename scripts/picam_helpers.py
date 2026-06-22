"""
Helper funkce pro Picamera2 display controller.

Čistě stateless utility — žádný import zpátky do display_controller_picam.
Bez závislosti na zbytku aplikace (jen cv2, numpy).
"""
import cv2
import numpy as np


# ── Box colour helper ─────────────────────────────────────────────────────────

def box_color(name: str) -> tuple:
    """Barva bounding boxu podle jména osoby."""
    if name not in ("Unknown", "?", "..."):
        return (0, 220, 0)
    return (0, 165, 255)


# ── Hand skeleton ─────────────────────────────────────────────────────────────

# MediaPipe hand connections — 21 bodů
_HAND_CONNECTIONS = [
    (0,1),(1,2),(2,3),(3,4),           # palec
    (0,5),(5,6),(6,7),(7,8),           # index
    (0,9),(9,10),(10,11),(11,12),      # prostředník
    (0,13),(13,14),(14,15),(15,16),    # prsteník
    (0,17),(17,18),(18,19),(19,20),    # malíček
    (5,9),(9,13),(13,17),              # dlaň
]


def draw_hand_skeleton(display: np.ndarray, lm_flat,
                       bbox=None, color=(0, 255, 180)):
    """Vykresli kostru ruky z 63 float landmarks do bbox oblasti."""
    if lm_flat is None or len(lm_flat) < 63:
        return
    dh, dw = display.shape[:2]
    if bbox and any(v > 0 for v in bbox):
        bx1, by1, bx2, by2 = bbox
        bx1 = int(bx1 * dw); by1 = int(by1 * dh)
        bx2 = int(bx2 * dw); by2 = int(by2 * dh)
        bw = max(bx2 - bx1, 1); bh = max(by2 - by1, 1)
    else:
        bx1, by1, bw, bh = 0, 0, dw, dh

    lm = [(float(lm_flat[i*3]), float(lm_flat[i*3+1])) for i in range(21)]
    xs = [p[0] for p in lm]; ys = [p[1] for p in lm]
    xmin, xmax = min(xs), max(xs); ymin, ymax = min(ys), max(ys)
    xr = max(xmax - xmin, 1e-6); yr = max(ymax - ymin, 1e-6)

    def pt(i):
        x = bx1 + int((lm[i][0] - xmin) / xr * bw)
        y = by1 + int((lm[i][1] - ymin) / yr * bh)
        return (max(0, min(dw - 1, x)), max(0, min(dh - 1, y)))

    for a, b in _HAND_CONNECTIONS:
        cv2.line(display, pt(a), pt(b), color, 2)
    for i in range(21):
        cv2.circle(display, pt(i), 4, color, -1)


# ── HQ zoom ──────────────────────────────────────────────────────────────────

def box_area(box) -> float:
    """Plocha normalizovaného bbox v rozsahu [0,1]² → [0,1]."""
    return max(0.0, box[2] - box[0]) * max(0.0, box[3] - box[1])


def hq_crop(frame: np.ndarray, box, padding: float,
            out_w: int, out_h: int) -> np.ndarray:
    """
    HQ crop z main streamu — vyřízne padded oblast kolem boxu
    a resizuje na (out_w, out_h). Pro vzdálené tváře nebo objekty.
    """
    H, W = frame.shape[:2]
    x1, y1, x2, y2 = box
    bw, bh = x2 - x1, y2 - y1
    x1p = max(0.0, x1 - bw * padding); y1p = max(0.0, y1 - bh * padding)
    x2p = min(1.0, x2 + bw * padding); y2p = min(1.0, y2 + bh * padding)
    ix1 = int(x1p * W); iy1 = int(y1p * H)
    ix2 = int(x2p * W); iy2 = int(y2p * H)
    if ix2 <= ix1 or iy2 <= iy1:
        return cv2.resize(frame, (out_w, out_h))
    return cv2.resize(frame[iy1:iy2, ix1:ix2], (out_w, out_h),
                      interpolation=cv2.INTER_LINEAR)


def draw_zoom_box(display: np.ndarray, box, padding: float,
                  color=(0, 255, 255)):
    """Čárkovaný rámeček kolem HQ zoom oblasti + label 'HQ'."""
    dh, dw = display.shape[:2]
    x1, y1, x2, y2 = box
    bw, bh = x2 - x1, y2 - y1
    rx1 = max(0, int((x1 - bw * padding) * dw))
    ry1 = max(0, int((y1 - bh * padding) * dh))
    rx2 = min(dw, int((x2 + bw * padding) * dw))
    ry2 = min(dh, int((y2 + bh * padding) * dh))
    dash = 12; gap = 6
    pts = [(rx1, ry1, rx2, ry1), (rx2, ry1, rx2, ry2),
           (rx2, ry2, rx1, ry2), (rx1, ry2, rx1, ry1)]
    for x0, y0, xn, yn in pts:
        length = max(abs(xn - x0), abs(yn - y0))
        if length == 0:
            continue
        dx = (xn - x0) / length; dy = (yn - y0) / length
        pos = 0
        while pos < length:
            end = min(pos + dash, length)
            cv2.line(display,
                     (int(x0 + dx * pos), int(y0 + dy * pos)),
                     (int(x0 + dx * end), int(y0 + dy * end)), color, 2)
            pos += dash + gap
    cv2.putText(display, "HQ", (rx1 + 4, ry1 + 16),
                cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1)
