"""
Object Detection Client
Sends OBJ_MAGIC frames to the combined hailo_inference_server.
Uses the same /tmp/hailo_scrfd.sock as the face client.
"""

import socket
import struct
import threading
import numpy as np

SOCK_PATH = "/tmp/hailo_scrfd.sock"

# Shared reconnect lock — imported from hailo_client to prevent
# simultaneous reconnects corrupting the server protocol.
try:
    from scripts.hailo_client import _reconnect_lock
except ImportError:
    import threading as _threading
    _reconnect_lock = _threading.Lock()
OBJ_MAGIC = b'\x0B\x1E\xC7\xD0'


class ObjectClient:

    def __init__(self):
        self._sock      = None
        self._lock      = threading.Lock()
        self._connected = False
        self._fail_cnt  = 0

    def connect(self) -> bool:
        with _reconnect_lock:
            try:
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.settimeout(2.0)
                s.connect(SOCK_PATH)
                s.settimeout(10.0)
                self._sock      = s
                self._connected = True
                self._fail_cnt  = 0
                print("[ObjectClient] Connected")
                return True
            except Exception as e:
                if self._fail_cnt % 20 == 0:
                    print(f"[ObjectClient] Cannot connect: {e}")
                self._fail_cnt += 1
                return False

    def _reconnect(self):
        try: self._sock.close()
        except Exception: pass
        self._sock      = None
        self._connected = False

    def detect(self, frame_480: np.ndarray) -> list:
        """
        Send OBJ_MAGIC + 640×480 RGB frame, receive object detections.
        Returns list of dicts:
          {"class_id": int, "confidence": float,
           "x1": float, "y1": float, "x2": float, "y2": float}
        """
        with self._lock:
            for _ in range(2):
                if not self._connected:
                    if not self.connect():
                        return []
                try:
                    data = frame_480.tobytes()
                    # Mode 3: OBJ_MAGIC + uint32 size + frame
                    self._sock.sendall(OBJ_MAGIC)
                    self._sock.sendall(struct.pack(">I", len(data)))
                    self._sock.sendall(data)

                    hdr = self._recv_exact(4)
                    if not hdr:
                        raise ConnectionError("no header")
                    n = struct.unpack(">I", hdr)[0]
                    if n == 0:
                        return []

                    raw = self._recv_exact(n * 6 * 4)
                    if not raw:
                        raise ConnectionError("no detections")

                    arr = np.frombuffer(raw, dtype=np.float32).reshape(n, 6)
                    return [
                        {"x1": float(r[0]), "y1": float(r[1]),
                         "x2": float(r[2]), "y2": float(r[3]),
                         "confidence": float(r[4]),
                         "class_id":   int(r[5])}
                        for r in arr
                    ]
                except Exception as e:
                    print(f"[ObjectClient] detect error: {e} — reconnecting")
                    self._reconnect()
            return []

    def is_connected(self) -> bool:
        return self._connected

    def _recv_exact(self, n: int) -> bytes | None:
        buf = b""
        while len(buf) < n:
            chunk = self._sock.recv(n - len(buf))
            if not chunk:
                return None
            buf += chunk
        return buf
