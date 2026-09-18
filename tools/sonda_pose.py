#!/usr/bin/env python3
"""Sonda: posli ulozeny snimek zpet do pose modelu pres /tmp/gesture.sock.

Necte obrazek ocima — spocita TATAZ cisla jako `gesture_client._update_pose`
(sirka ramen, nad, loket, od nosu) a rekne, KTERA brana by snimek zahodila.
Tim se rozlisi "prah je moc prisny" od "model klade zapesti spatne".

Pouziti:
    python3 tools/sonda_pose.py data/gesta/<snimek>.jpg [dalsi.jpg ...]

⚠️ ZASADNI OMEZENI VSTUPU (zmereno 17. 9. 2026): ulozeny snimek NENI ten,
ktery udalost spustil. `_snimek_zamavani` uklada `self._posledni_frame`, tedy
ZIVY snimek v dobe zapisu. Doklad: log 16:27:07 hlasi `nad 1.62` (paze nahore),
snimek zapsany v 16:27:10 ma obe zapesti pod rameny. Sonda tedy meri POZDEJSI
okamzik — rozhodujici cisla ber z `data/mereni/gesta*.log`, sondou overuj
chovani modelu (klouby, jistoty, meritko), ne duvod konkretniho zahozeni.

Prahy se ctou z configu pres config_io, ne z kodu — jinak by sonda merila
proti jinym cislum nez bezici Hans.
"""
import os
import socket
import struct
import sys

import cv2
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from scripts.config_io import load as cfg_load  # noqa: E402

SOCK = "/tmp/gesture.sock"
GESTURE_POSE = 200

G = cfg_load().get("gesture", {})
KP_CONF = float(G.get("pose_kp_conf", 0.3))
MIN_SW = float(G.get("pose_min_shoulder_h", 0.05))
NAD_MIN = float(G.get("pose_wrist_above_shoulder", 0.3))
# HANS_GESTURE_POSE_NAD_MAX_V1 — horni mez, 99 = vypnuto
NAD_MAX = float(G.get("pose_wrist_above_max", 99.0))
LOKET_MIN = float(G.get("pose_elbow_min", -0.4))
NOSU_MIN = float(G.get("pose_wrist_from_nose", 0.5))

JMENA = {0: "nos", 5: "rameno L", 6: "rameno P", 7: "loket L",
         8: "loket P", 9: "zapesti L", 10: "zapesti P"}


def recv_exact(s, n):
    buf = b""
    while len(buf) < n:
        ch = s.recv(n - len(buf))
        if not ch:
            return None
        buf += ch
    return buf


def sonda(cesta):
    frame = cv2.imread(cesta)
    if frame is None:
        print("  !! nelze nacist %s" % cesta)
        return
    h, w = frame.shape[:2]
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(10.0)
    try:
        s.connect(SOCK)
    except OSError as e:
        print("  !! socket %s nedostupny (%s) — bezi hailo server?" % (SOCK, e))
        return
    raw = frame.tobytes()
    s.sendall(struct.pack(">I", len(raw)))
    s.sendall(raw)
    s.sendall(struct.pack(">HH", w, h))
    gid = struct.unpack("B", recv_exact(s, 1))[0]
    bbox = struct.unpack(">ffff", recv_exact(s, 16))
    lm = None
    if gid == GESTURE_POSE:
        lm = struct.unpack(">63f", recv_exact(s, 252))
    s.close()

    print("  id=%d (200=osoba)  bbox=%s" % (
        gid, ", ".join("%.3f" % b for b in bbox)))
    if lm is None:
        print("  model NEVIDI postavu — nelze merit")
        return

    k = np.asarray(lm[:51], dtype=np.float32).reshape(17, 3)
    ar = float(w) / float(h)
    X, Y, Cf = k[:, 0] * ar, k[:, 1], k[:, 2]
    print("  skore osoby=%.3f  pomer stran=%.3f  (%dx%d)" % (
        lm[51], ar, w, h))
    for i, jm in JMENA.items():
        print("    %-10s x=%.3f y=%.3f jistota=%.2f%s" % (
            jm, k[i, 0], k[i, 1], Cf[i],
            "   <-- POD prahem jistoty" if Cf[i] < KP_CONF else ""))

    if min(Cf[5], Cf[6]) < KP_CONF:
        print("  >> ZAHOZENO branou `jistota` (ramena pod %.2f)" % KP_CONF)
        return
    sw = abs(float(X[5] - X[6]))
    print("  sirka ramen = %.4f  (prah %.3f -> %s)" % (
        sw, MIN_SW, "PROJDE" if sw >= MIN_SW else "ZAHOZENO `uzka_ramena`"))
    if sw < MIN_SW:
        return
    print("  prah `nad` %.2f sirky ramen = %.1f px na tomto snimku" % (
        NAD_MIN, NAD_MIN * sw * h))
    for jm, sh, el, wr in (("L", 5, 7, 9), ("P", 6, 8, 10)):
        if Cf[wr] < KP_CONF or Cf[el] < KP_CONF:
            print("  paze %s: ZAHOZENA branou `jistota` "
                  "(zapesti %.2f, loket %.2f)" % (jm, Cf[wr], Cf[el]))
            continue
        nad = float(Y[sh] - Y[wr]) / sw
        loket = float(Y[sh] - Y[el]) / sw
        # pozor: bez duveryhodneho nosu padne `odnosu` na 9.0 a branu TISE pusti
        odnosu = abs(float(X[wr] - X[0])) / sw if Cf[0] >= KP_CONF else 9.0
        duvod = ("nad_ramenem" if nad < NAD_MIN
                 else ("pazi_nahore" if nad > NAD_MAX
                       else ("loket" if loket < LOKET_MIN
                             else ("od_nosu" if odnosu < NOSU_MIN else None))))
        print("  paze %s: nad=%+.3f (pasmo %.2f..%.2f) | loket=%+.3f "
              "(prah %.2f) | od_nosu=%.3f (prah %.2f)  -> %s" % (
                  jm, nad, NAD_MIN, NAD_MAX, loket, LOKET_MIN, odnosu,
                  NOSU_MIN,
                  "PROJDE" if duvod is None else "ZAHOZENO `%s`" % duvod))


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    for _c in sys.argv[1:]:
        print("\n=== %s" % _c)
        sonda(_c)
    print("\nHOTOVO")
