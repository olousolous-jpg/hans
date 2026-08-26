"""HANS_TRANSLATE_V1 — SRT: parsování a sestavení souvislé zvukové stopy.

Sdílí se mezi Pi (edge-tts) a PC (Piper), proto tu NENÍ nic o konkrétním motoru.

Dvě věci, které odhalilo až měření 26.8. a bez kterých to nefunguje:
  1) Syntéza přidává ke každé replice pevné ticho (Piper ~1,2 s), které se
     zpomalením/zrychlením NEZKRACUJE. Přes 700 replik to dělá čtvrthodinu
     ticha navíc a stopa se ke konci hodinového pořadu opozdí o ~60 s.
     → `load_trimmed` ho ořízne.
  2) Dorovnání tempa se zaokrouhluje na hrubou mřížku a VŽDY DOLŮ: nahoru
     znamená, že se replika do svého času nevejde.
"""
from __future__ import annotations

import array
import os
import re
import wave

_TS = re.compile(r"(\d+):(\d\d):(\d\d)[,.](\d+)\s*-->\s*(\d+):(\d\d):(\d\d)[,.](\d+)")
_TAG = re.compile(r"<[^>]+>|\{[^}]*\}")

TRIM_THRESH = 250     # amplituda 16bit, pod níž je to ticho
TRIM_MARGIN = 0.03    # necháme 30 ms nádechu
GAP = 0.15            # rezerva mezi replikami (s)


def read_text(path: str) -> tuple[str, str]:
    """SRT bývají v cp1250 i utf-8 — poznat to musíme sami."""
    raw = open(path, "rb").read()
    for enc in ("utf-8-sig", "utf-8", "cp1250", "iso-8859-2"):
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return raw.decode("cp1250", "replace"), "cp1250/replace"


def _secs(h, m, s, ms) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms.ljust(3, "0")[:3]) / 1000.0


def parse_srt(path: str) -> tuple[list[dict], str]:
    txt, enc = read_text(path)
    txt = txt.replace("\r\n", "\n").replace("\r", "\n")
    cues, cur = [], None
    for line in txt.split("\n"):
        m = _TS.search(line)
        if m:
            if cur and cur["text"]:
                cues.append(cur)
            cur = {"start": _secs(*m.group(1, 2, 3, 4)),
                   "end": _secs(*m.group(5, 6, 7, 8)), "text": []}
        elif cur is not None:
            t = _TAG.sub("", line).strip()
            if t and not t.isdigit():
                cur["text"].append(t)
    if cur and cur["text"]:
        cues.append(cur)
    for c in cues:
        c["text"] = " ".join(c["text"]).strip()
    return cues, enc


def dedup(cues: list[dict]) -> list[dict]:
    """Některé ripy mají rozbitý první titulek: dva záznamy s týmž časem,
    první useknutý. Při shodném start+end necháme ten delší."""
    out: dict = {}
    for c in cues:
        k = (round(c["start"], 3), round(c["end"], 3))
        if k not in out or len(c["text"]) > len(out[k]["text"]):
            out[k] = c
    return sorted(out.values(), key=lambda c: c["start"])


def load_cues(path: str, minutes: float = 0.0) -> list[dict]:
    cues = dedup(parse_srt(path)[0])
    if minutes:
        cues = [c for c in cues if c["start"] < minutes * 60]
    return cues


def slots(cues: list[dict]) -> list[float]:
    """Kolik času má replika, než začne další."""
    out = []
    for i, c in enumerate(cues):
        nxt = cues[i + 1]["start"] if i + 1 < len(cues) else c["end"] + 5.0
        out.append(max(0.5, nxt - c["start"] - GAP))
    return out


def load_trimmed(path: str) -> tuple[bytes, int, int, int]:
    """Vrátí (pcm, rate, ch, width) s useknutým vodicím a koncovým tichem."""
    with wave.open(path, "rb") as w:
        rate, ch, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
        data = w.readframes(w.getnframes())
    a = array.array("h")
    a.frombytes(data)
    lo, hi = 0, len(a) - 1
    while lo < hi and abs(a[lo]) < TRIM_THRESH:
        lo += 1
    while hi > lo and abs(a[hi]) < TRIM_THRESH:
        hi -= 1
    m = int(TRIM_MARGIN * rate) * ch
    lo, hi = max(0, lo - m), min(len(a) - 1, hi + m)
    return a[lo:hi + 1].tobytes(), rate, ch, width


def duration(path: str) -> float:
    pcm, rate, ch, width = load_trimmed(path)
    return len(pcm) / (rate * width * ch)


def fit_scale(need: float, have: float, floor: float, grid: float = 0.05) -> float:
    """Tempo, aby se replika vešla. VŽDY DOLŮ — nahoru se nevejde."""
    return max(floor, int((need / have) / grid) * grid)


def assemble(cues: list[dict], wav_for, out_path: str) -> dict:
    """Poskládá repliky na jejich časy do jedné stopy.

    `wav_for(i)` vrátí cestu k wav i-té repliky, nebo None (replika se vynechá).
    Když se replika nevejde, posune se — ale nikdy se nepřekrývá s předchozí.
    """
    first = next((wav_for(i) for i in range(len(cues)) if wav_for(i)), None)
    if not first:
        raise ValueError("žádná replika se nenasyntetizovala")
    _, rate, ch, width = load_trimmed(first)
    total = cues[-1]["end"] if cues else 0.0
    buf = bytearray((int(total * rate) + rate) * width * ch)

    cursor = 0
    posunuto = 0
    drift = 0.0
    for i, c in enumerate(cues):
        p = wav_for(i)
        if not p or not os.path.exists(p):
            continue
        data = load_trimmed(p)[0]
        want = int(c["start"] * rate)
        pos = max(want, cursor)
        if pos > want:
            posunuto += 1
            drift = max(drift, (pos - want) / rate)
        off = pos * width * ch
        end = off + len(data)
        if end > len(buf):
            buf.extend(b"\x00" * (end - len(buf)))
        buf[off:end] = data
        cursor = pos + len(data) // (width * ch)

    with wave.open(out_path, "wb") as w:
        w.setnchannels(ch)
        w.setsampwidth(width)
        w.setframerate(rate)
        w.writeframes(bytes(buf))
    return {"rate": rate, "posunuto": posunuto, "max_opozdeni_s": round(drift, 2),
            "delka_s": round(len(buf) / (rate * width * ch), 1)}
