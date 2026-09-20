#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Měření: co by guard udělal, kdyby běžel i na cestě `self_state` (20. 9.).

Dnes je celý guard pod `_grounding_outcome == 'grounded'`, takže na cestě
`self_state` neběží ani hlásící větví — a právě tudy prošly 4 z 5 doložených
výmyslů o sobě (Rooibos, jóga, 37/12, deník od dětství).

Počítá se PÁROVĚ nad TOUTÉŽ odpovědí:
  A = dnešní kontrakt: overlap vs (blok self_state + 6 zpráv historie)
  B = evidenční struktura: max(A, max_i overlap vs evidenční blok_i)

Obě strany: vedle výmyslů se vypisují VŠECHNY věty, ať je vidět, co by se
zahodilo navíc.
"""
from __future__ import annotations
import json, sys, re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.grounding_guard import _stems, _SENT, MIN_SUPPORT, MIN_STEMS
import scripts.openwebui_direct_handler as M
from scripts import config_io

PREPISY = [
    ROOT / "data/mereni/rozhovory/20260919_170445_zkouška.jsonl",
    ROOT / "data/mereni/rozhovory/20260919_164922_Marek.jsonl",
]
# doložené výmysly (substring → štítek)
VYMYSL = {"Rooibos": "rooibos", "jógu": "jóga", "37 minut": "studium 37/12",
          "v dětství": "deník od dětství", "pět tisíc slov": "esej"}


def pasmo(ov):
    return "nechá" if (ov == 0.0 or ov >= MIN_SUPPORT) else "ZAHODÍ"


def main():
    cfg = config_io.load()
    zachyt = {}
    _orig = M.evidence_text
    def _spy(h):
        zachyt["h"] = dict(h); return _orig(h)
    M.evidence_text = _spy

    from scripts.test_rozhovor import postav_handler
    print("stavím handler…", flush=True)
    h = postav_handler(cfg)
    from scripts.hans_recall import self_state_facts
    ss = self_state_facts("data/hans_diary.db") or ""
    print("handler OK · blok self_state %d zn" % len(ss), flush=True)

    souhrn = {"A_zahod": 0, "B_zahod": 0, "vet": 0,
              "A_vymysl": 0, "B_vymysl": 0, "vymyslu": 0}
    for p in PREPISY:
        rows = [json.loads(l) for l in open(p, encoding="utf-8") if l.strip()]
        print("\n" + "=" * 96, flush=True)
        print("### %s" % p.name, flush=True)
        for i, r in enumerate(rows):
            if not any("GROUNDING: self_state" in x for x in r["log"]):
                continue
            h._build_system("zkouška", user_msg=r["veta"])
            hod = zachyt.get("h") or {}
            bloky = {n: str(hod.get(n) or "") for n in M._EVIDENCNI_BLOKY}
            bloky = {k: v for k, v in bloky.items() if v.strip()}
            BLK = {k: _stems(v) for k, v in bloky.items()}

            zpravy = []
            for q in rows[max(0, i - 3):i]:
                zpravy += [q["veta"], q["odpoved"]]
            ref = ss + " " + " ".join(zpravy[-6:])
            R = _stems(ref)

            print("\n--- tah %d (%s) · ref %d zn, %d bloků" %
                  (i, r["cas"][11:], len(ref), len(bloky)), flush=True)
            print("    Q: %s" % r["veta"][:110], flush=True)
            for s in _SENT.split(r["odpoved"].strip()):
                s = s.strip()
                if not s:
                    continue
                S = _stems(s)
                if len(S) < MIN_STEMS:
                    continue
                oa = len(S & R) / len(S)
                nej = max(((k, len(S & v) / len(S)) for k, v in BLK.items()),
                          key=lambda x: x[1], default=("-", 0.0))
                ob = max(oa, nej[1])
                stitek = ""
                for k, v in VYMYSL.items():
                    if k.lower() in s.lower():
                        stitek = "  ⛔VÝMYSL(%s)" % v
                souhrn["vet"] += 1
                if pasmo(oa) == "ZAHODÍ": souhrn["A_zahod"] += 1
                if pasmo(ob) == "ZAHODÍ": souhrn["B_zahod"] += 1
                if stitek:
                    souhrn["vymyslu"] += 1
                    if pasmo(oa) == "ZAHODÍ": souhrn["A_vymysl"] += 1
                    if pasmo(ob) == "ZAHODÍ": souhrn["B_vymysl"] += 1
                print("    %-6s %-6s A=%.2f B=%.2f [%s %.2f] %s%s" %
                      (pasmo(oa), pasmo(ob), oa, ob, nej[0], nej[1],
                       s[:72], stitek), flush=True)

    print("\n" + "=" * 96, flush=True)
    print("SOUHRN: vět %(vet)d · zahodí A=%(A_zahod)d B=%(B_zahod)d | "
          "z toho doložených výmyslů %(vymyslu)d: A=%(A_vymysl)d B=%(B_vymysl)d"
          % souhrn, flush=True)
    print("HOTOVO", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
