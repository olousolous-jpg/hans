"""
Hailo Client Module
Unix-socket client for hailo_inference_server.py.

Protocol:
  Mode 1 — detect + embed
    send : uint32 n_bytes  +  RGB uint8 640×480
    recv : uint32 N  +  N×4 float32 boxes  +  N×512 float32 embeddings  +  N uint8 labels

  Mode 2 — embed only (HQ crops from main frame)
    send : EMBED_MAGIC (4 bytes)  +  uint32 N  +  N × (112×112×3 uint8)
    recv : uint32 N  +  N×512 float32 embeddings
"""

import socket
import struct
import threading
import numpy as np
import cv2

SOCK_PATH    = "/tmp/hailo_scrfd.sock"

# Module-level lock prevents simultaneous reconnects from HailoClient
# and ObjectClient corrupting the server protocol state.
_reconnect_lock = threading.Lock()
ARCFACE_DIM  = 512
ARCFACE_SIZE = 112

# Mode 2 magic — must match hailo_inference_server.py
EMBED_MAGIC = b'\xEB\xED\xFE\xED'

# Label values
LABEL_FACE   = 0
LABEL_PERSON = 1


class HailoClient:
    """Thread-safe Unix-socket client for the Hailo inference server."""

    def __init__(self):
        self._sock      = None
        self._lock      = threading.Lock()
        self._connected = False
        self._fail_cnt  = 0

    # ── Connection ────────────────────────────────────────────────────────

    def connect(self):
        with _reconnect_lock:
            try:
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.settimeout(2.0)
                s.connect(SOCK_PATH)
                s.settimeout(30.0)
                self._sock      = s
                self._connected = True
                self._fail_cnt  = 0
                print("[HailoClient] Connected")
                return True
            except Exception as e:
                if self._fail_cnt % 50 == 0:
                    print(f"[HailoClient] Cannot connect: {e}")
                self._fail_cnt += 1
                return False

    def _reconnect(self):
        try:
            self._sock.close()
        except Exception:
            pass
        self._sock      = None
        self._connected = False

    # ── Mode 1: detect + embed ────────────────────────────────────────────

    def infer(self, frame_480: np.ndarray) -> list:
        """
        Detect faces and return embeddings from a 640×480 RGB frame.
        Returns list of (box [x1,y1,x2,y2], embedding ndarray(512,), label int).
        """
        with self._lock:
            for _ in range(2):
                if not self._connected:
                    if not self.connect():
                        return []
                try:
                    data = frame_480.tobytes()
                    self._sock.sendall(struct.pack(">I", len(data)))
                    self._sock.sendall(data)

                    hdr = self._recv_exact(4)
                    if not hdr:
                        raise ConnectionError("no header")
                    n = struct.unpack(">I", hdr)[0]
                    if n == 0:
                        return []

                    raw_boxes = self._recv_exact(n * 4 * 4)
                    if not raw_boxes:
                        raise ConnectionError("no boxes")
                    boxes = np.frombuffer(raw_boxes, dtype=np.float32).reshape(n, 4)

                    raw_embs = self._recv_exact(n * ARCFACE_DIM * 4)
                    if not raw_embs:
                        raise ConnectionError("no embeddings")
                    embs = np.frombuffer(raw_embs, dtype=np.float32).reshape(n, ARCFACE_DIM)

                    raw_labels = self._recv_exact(n)
                    if not raw_labels:
                        raise ConnectionError("no labels")
                    labels = np.frombuffer(raw_labels, dtype=np.uint8)

                    return list(zip(boxes.tolist(), embs, labels.tolist()))

                except Exception as e:
                    print(f"[HailoClient] infer error: {e} — reconnecting")
                    self._reconnect()
            return []

    # ── Mode 2: embed only ────────────────────────────────────────────────

    def embed_faces(self, crops_112: list) -> list:
        """
        Send N pre-cropped 112×112 RGB faces, receive N×512 embeddings.
        Returns list of ndarray(512,) or None per face on failure.
        """
        if not crops_112:
            return []
        with self._lock:
            for _ in range(2):
                if not self._connected:
                    if not self.connect():
                        return [None] * len(crops_112)
                try:
                    n = len(crops_112)
                    self._sock.sendall(EMBED_MAGIC)
                    self._sock.sendall(struct.pack(">I", n))
                    for crop in crops_112:
                        c = np.ascontiguousarray(
                            cv2.resize(crop, (ARCFACE_SIZE, ARCFACE_SIZE))
                            if crop.shape[:2] != (ARCFACE_SIZE, ARCFACE_SIZE)
                            else crop, dtype=np.uint8)
                        self._sock.sendall(c.tobytes())

                    hdr = self._recv_exact(4)
                    if not hdr:
                        raise ConnectionError("no header")
                    n_back = struct.unpack(">I", hdr)[0]
                    if n_back == 0:
                        return [None] * n

                    raw_embs = self._recv_exact(n_back * ARCFACE_DIM * 4)
                    if not raw_embs:
                        raise ConnectionError("no embeddings")
                    embs = np.frombuffer(raw_embs, dtype=np.float32).reshape(n_back, ARCFACE_DIM)
                    return list(embs)

                except Exception as e:
                    print(f"[HailoClient] embed_faces error: {e} — reconnecting")
                    self._reconnect()
            return [None] * len(crops_112)

    # ── Internal ──────────────────────────────────────────────────────────

    def _recv_exact(self, n: int) -> bytes | None:
        buf = b""
        while len(buf) < n:
            chunk = self._sock.recv(n - len(buf))
            if not chunk:
                return None
            buf += chunk
        return buf