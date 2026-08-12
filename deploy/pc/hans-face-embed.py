#!/usr/bin/env python3
"""
hans-face-embed — embedding služba pro Hanse, běží na PC.

PROČ EXISTUJE (změřeno 8.–11. 8. 2026):
  Hailo počítá embedding v int8 a v tom se ztrácí identita. Při SROVNANÉ
  vzdálenosti (aby čísla neovlivnila póza ani odstup) vyšly odstupy takto:

      mobilefacenet int8 (Hailo):  +0.039 / -0.010 / +0.025
      arcface r50   int8 (Hailo):  +0.017 / -0.010 / +0.025
      arcface r50   FLOAT (tady):  +0.142 / +0.129 / +0.049

  Není to tedy architekturou, ale PŘESNOSTÍ VÝPOČTU — proto float na PC.
  Silnější model na Hailo by narazil na tutéž kvantizační zeď.

ZÁMĚRNĚ NA CPU, NE NA GPU:
  Na GPU by služba soupeřila o VRAM s Ollamou (hans-czech ~10.8 GB rezidentně),
  s ComfyUI (SDXL ~7 GB) a se hrami. Na CPU (3950X, 32 vláken) je při
  škrceném tempu (~5 embeddingů/s) zátěž zanedbatelná a odpadá:
    • souboj o VRAM,
    • nutnost hlídat herní mód,
    • a hlavně: rozpoznávání funguje I BĚHEM HRANÍ.
  Ze dvou výpadkových oken (noc + hraní) tak zbude jen noční vypnutí PC.
  Na GPU se dá přejít později, kdyby latence vadila (ORT_PROVIDER=ROCm).

PROTOKOL (schválně hloupý, ať nemá co selhat):
  POST /embed   tělo = syrové bajty N × 112×112×3 uint8 (RGB)
                odpověď = syrové bajty N × 512 float32, L2-normalizované
  GET  /health  {"ok", "model", "vlaken", "embedu", "ms_prumer"}

⚠️ Pi si MUSÍ poradit, když služba neodpovídá (v noci je PC vypnuté).
   Fallback na Hailo embedding je povinný, ne volitelný.
"""
import os
import sys
import time
import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np

MODEL = os.environ.get("FACE_MODEL", os.path.expanduser("~/hans/w600k_r50.onnx"))
PORT = int(os.environ.get("FACE_PORT", "8765"))
THREADS = int(os.environ.get("FACE_THREADS", "8"))
PROVIDER = os.environ.get("ORT_PROVIDER", "CPU")
SIZE = 112
DIM = 512

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("face-embed")

_stats = {"embedu": 0, "ms_celkem": 0.0}


def _make_session():
    import onnxruntime as ort
    so = ort.SessionOptions()
    so.intra_op_num_threads = THREADS
    so.log_severity_level = 3
    prov = (["ROCMExecutionProvider", "CPUExecutionProvider"]
            if PROVIDER.upper() == "ROCM" else ["CPUExecutionProvider"])
    s = ort.InferenceSession(MODEL, so, providers=prov)
    log.info("model %s | provider %s | vláken %d", MODEL, s.get_providers()[0], THREADS)
    return s


SESS = None
INPUT = None


def embed(buf: bytes) -> bytes:
    n = len(buf) // (SIZE * SIZE * 3)
    if n == 0 or len(buf) % (SIZE * SIZE * 3):
        raise ValueError(f"špatná délka těla: {len(buf)} B")
    imgs = np.frombuffer(buf, np.uint8).reshape(n, SIZE, SIZE, 3).astype(np.float32)
    # stejná normalizace jako insightface: (x-127.5)/127.5, pořadí NCHW
    x = ((imgs - 127.5) / 127.5).transpose(0, 3, 1, 2)
    t0 = time.perf_counter()
    out = SESS.run(None, {INPUT: np.ascontiguousarray(x)})[0]
    dt = (time.perf_counter() - t0) * 1000.0
    _stats["embedu"] += n
    _stats["ms_celkem"] += dt
    out = np.asarray(out, np.float32)
    out /= (np.linalg.norm(out, axis=1, keepdims=True) + 1e-9)
    log.info("embed n=%d za %.0f ms (%.1f ms/kus)", n, dt, dt / n)
    return out.astype(np.float32).tobytes()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):        # vlastní logging, ne stderr spam
        pass

    def _send(self, code, body: bytes, ctype="application/octet-stream"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/health"):
            n = _stats["embedu"]
            b = json.dumps({"ok": True, "model": os.path.basename(MODEL),
                            "provider": SESS.get_providers()[0] if SESS else None,
                            "vlaken": THREADS, "embedu": n,
                            "ms_prumer": round(_stats["ms_celkem"] / n, 1) if n else None},
                           ensure_ascii=False).encode()
            self._send(200, b, "application/json")
        else:
            self._send(404, b"not found")

    def do_POST(self):
        if not self.path.startswith("/embed"):
            self._send(404, b"not found"); return
        try:
            ln = int(self.headers.get("Content-Length", 0))
            buf = b""
            while len(buf) < ln:
                c = self.rfile.read(ln - len(buf))
                if not c:
                    break
                buf += c
            self._send(200, embed(buf))
        except Exception as e:
            log.error("embed selhal: %s", e)
            self._send(400, str(e).encode(), "text/plain; charset=utf-8")


if __name__ == "__main__":
    if not os.path.exists(MODEL):
        log.error("model nenalezen: %s", MODEL); sys.exit(1)
    SESS = _make_session()
    INPUT = SESS.get_inputs()[0].name
    # rozehřát — první běh je vždy pomalý a zkreslil by měření
    embed(np.zeros((SIZE, SIZE, 3), np.uint8).tobytes())
    _stats["embedu"] = 0; _stats["ms_celkem"] = 0.0
    log.info("poslouchám na 0.0.0.0:%d", PORT)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
