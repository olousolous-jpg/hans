#!/usr/bin/env python3
"""HANS_TTS_ENGINE_V1 — záložní motor: Piper na PC (kasandra).

Běží NA PC, protože tam je binárka i hlasy. Hans (Pi) sem pošle SRT přes SSH
a stáhne si hotovou stopu. Sdílí `srt_track.py` s Pi, aby zarovnání na časy
bylo v obou cestách stejné — jinak by se motory chovaly různě.

Použití: hans_piper_track.py in.srt out.wav <piper_bin> <voice.onnx>
"""
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import srt_track as st

FLOOR = 0.85


def batch(jobs, piper, voice, scale):
    """Piper bere JEDNO tempo na proces → repliky se seskupují podle tempa.
    Jemná mřížka by znamenala stovky spuštění a načtení modelu pokaždé."""
    if not jobs:
        return
    lines = [json.dumps({"text": t, "output_file": p}) for _, t, p in jobs]
    r = subprocess.run([os.path.expanduser(piper), "-m", os.path.expanduser(voice),
                        "--json-input", "--output_dir", os.path.dirname(jobs[0][2]),
                        "--length_scale", str(scale)],
                       input="\n".join(lines), text=True, capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(f"piper selhal: {r.stderr[-400:]}")


def main():
    srt, out, piper, voice = sys.argv[1:5]
    cues = st.load_cues(srt)
    wd = os.path.join(os.path.dirname(os.path.abspath(out)), "kusy")
    os.makedirs(wd, exist_ok=True)
    paths = [os.path.join(wd, f"{i:05d}.wav") for i in range(len(cues))]

    batch([(i, c["text"], p) for i, (c, p) in enumerate(zip(cues, paths))], piper, voice, 1.0)

    # co přeteče svůj čas, namluvit rychleji — seskupeno podle tempa
    sl = st.slots(cues)
    podle_tempa: dict = {}
    for i, p in enumerate(paths):
        if not os.path.exists(p):
            continue
        d = st.duration(p)
        if d > sl[i]:
            podle_tempa.setdefault(st.fit_scale(sl[i], d, FLOOR), []).append(
                (i, cues[i]["text"], p))
    for scale, jobs in podle_tempa.items():
        batch(jobs, piper, voice, scale)

    stats = st.assemble(cues, lambda i: paths[i] if os.path.exists(paths[i]) else None, out)
    print(json.dumps(stats, ensure_ascii=False))


if __name__ == "__main__":
    main()
