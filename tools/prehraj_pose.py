#!/usr/bin/env python3
"""Offline prehravac syrove stopy pozy (HANS_GESTURE_POSE_RAW_V1).

Cte zaznam z `data/mereni/pose_raw.log` (nebo z ulozeneho korpusu) a zkousi
NAD SKUTECNYM POHYBEM, co by delaly jine prahy: kolik snimku projde trojici
bran (nad / loket / od nosu), kolik z toho vznikne SOUVISLYCH useku stopy
a kolikrat by z nich vzniklo mavnuti.

Rozhodovaci logika je opsana z `GestureClient._pose_je_mavani`, aby se dala
menit CISLA, ne kod. Produkcni hodnoty se ctou z configu pres `config_io`,
takze nastroj nezestarne po kazde zmene prahu.

⚠️ Past, do ktere jsem sel napoprve: stopa se pri neuspesnem snimku PRERUSI,
takze "kolik snimku proslo" se nesmi cist z delky stopy na konci behu — je to
soucet pres vsechny souvisle useky.

⚠️ Stopa obsahuje jen snimky, ktere prosly branou sirky ramen
(`pose_min_shoulder_h`). Ze vzdalenosti, kde jsou ramena uzsi, nevi nastroj
NIC — a "nic ve stope" tam znamena jinou vadu, ne jine prahy.

Pouziti:
    python3 tools/prehraj_pose.py [--log CESTA] [od [do]] [--mrizka]
    od/do jsou HH:MM:SS dnesniho dne; bez nich se bere cely soubor.
"""
import sys
import time
from datetime import datetime

sys.path.insert(0, "scripts")
import config_io                                    # noqa: E402

VYCHOZI_LOG = "data/mereni/pose_raw.log"


def produkcni():
    g = config_io.load().get("gesture") or {}
    return {
        "pose_wrist_above_shoulder": float(g.get("pose_wrist_above_shoulder", 0.3)),
        # HANS_GESTURE_POSE_NAD_MAX_V1 — zrcadli produkcni branu.
        # Radky v pose_raw.log vznikly pod PUVODNI mezi sirky
        # ramen, takze se da prehravat i prisnejsi prah.
        "pose_wrist_above_max": float(g.get("pose_wrist_above_max", 99.0)),
        "pose_min_shoulder_h": float(g.get("pose_min_shoulder_h", 0.05)),
        "pose_elbow_min": float(g.get("pose_elbow_min", -0.4)),
        "pose_wrist_from_nose": float(g.get("pose_wrist_from_nose", 0.5)),
        "pose_window_s": float(g.get("pose_window_s", 2.0)),
        "pose_min_samples": int(g.get("pose_min_samples", 3)),
        "pose_min_swing": float(g.get("pose_min_swing", 0.4)),
        "pose_eps": float(g.get("pose_eps", 0.1)),
        "pose_min_reversals": int(g.get("pose_min_reversals", 1)),
    }


def nacti(cesta, od=None, do=None):
    """Radky: cas, strana, duvod, nad, loket, od nosu, sirka ramen, vychylka."""
    ven = []
    for radek in open(cesta, encoding="utf-8"):
        if radek.startswith("#"):
            continue
        c = radek.rstrip("\n").split("\t")
        if len(c) != 8:
            continue
        try:
            t = float(c[0])
        except ValueError:
            continue
        if (od is not None and t < od) or (do is not None and t > do):
            continue
        ven.append({
            "t": t, "jm": c[1], "duvod": c[2], "nad": float(c[3]),
            "loket": float(c[4]), "odnosu": float(c[5]),
            "sw": float(c[6]), "dx": float(c[7]),
        })
    ven.sort(key=lambda r: r["t"])
    return ven


def projde(r, p):
    return (r["nad"] >= p["pose_wrist_above_shoulder"] and
            r["nad"] <= p["pose_wrist_above_max"] and
            r["sw"] >= p["pose_min_shoulder_h"] and
            r["loket"] >= p["pose_elbow_min"] and
            r["odnosu"] >= p["pose_wrist_from_nose"])


def useky(radky, jm, p):
    """Souvisle useky proslych snimku jedne strany (neuspech stopu prerusi)."""
    ven, cur = [], []
    for r in radky:
        if r["jm"] != jm:
            continue
        if projde(r, p):
            cur.append(r)
        elif cur:
            ven.append(cur)
            cur = []
    if cur:
        ven.append(cur)
    return ven


def mavnuti(stopa, p):
    """Kolikrat by v tomto useku vznikl vystrel + nejlepsi dosazene rozpeti."""
    poc, nejrozpeti, nejobratu = 0, 0.0, 0
    for i in range(len(stopa)):
        okno = [r for r in stopa[:i + 1]
                if stopa[i]["t"] - r["t"] <= p["pose_window_s"]]
        if len(okno) < p["pose_min_samples"]:
            continue
        xs = [r["dx"] for r in okno]
        rozpeti = max(xs) - min(xs)
        nejrozpeti = max(nejrozpeti, rozpeti)
        obr, smer, kotva = 0, 0, xs[0]
        for x in xs[1:]:
            d = x - kotva
            if abs(d) < p["pose_eps"]:
                continue
            s = 1 if d > 0 else -1
            if smer and s != smer:
                obr += 1
            smer, kotva = s, x
        nejobratu = max(nejobratu, obr)
        if rozpeti >= p["pose_min_swing"] and obr >= p["pose_min_reversals"]:
            poc += 1
    return poc, nejrozpeti, nejobratu


def vyhodnot(radky, p, nazev):
    kusy = [nazev]
    for jm in ("L", "P"):
        us = useky(radky, jm, p)
        v = [mavnuti(u, p) for u in us] or [(0, 0.0, 0)]
        kusy.append("%s: snimku %4d, useku %3d, nejdelsi %3d, rozpeti %.2f, "
                    "obratu %d, MAVNUTI %3d"
                    % (jm, sum(len(u) for u in us), len(us),
                       max((len(u) for u in us), default=0),
                       max(x[1] for x in v), max(x[2] for x in v),
                       sum(x[0] for x in v)))
    print("  ".join(kusy))


def popis(p):
    return ("nad %5.2f..%5.2f sw>=%.2f loket>=%5.2f vzorku>=%d "
            "rozpeti>=%.2f"
            % (p["pose_wrist_above_shoulder"], p["pose_wrist_above_max"],
               p["pose_min_shoulder_h"], p["pose_elbow_min"],
               p["pose_min_samples"], p["pose_min_swing"]))


def hhmmss(s):
    d = datetime.now().replace(hour=int(s[:2]), minute=int(s[3:5]),
                               second=int(s[6:8]), microsecond=0)
    return d.timestamp()


def main():
    argv = sys.argv[1:]
    cesta = VYCHOZI_LOG
    if "--log" in argv:
        i = argv.index("--log")
        cesta = argv[i + 1]
        argv = argv[:i] + argv[i + 2:]
    casy = [x for x in argv if not x.startswith("--")]
    radky = nacti(cesta, hhmmss(casy[0]) if casy else None,
                  hhmmss(casy[1]) if len(casy) > 1 else None)
    if not radky:
        print("stopa je prazdna — jiny cas, jiny soubor, nebo nikdo v zaberu")
        return 1
    prod = produkcni()
    print("%s — vzorku %d, okno %s–%s, sirka ramen %.3f–%.3f"
          % (cesta, len(radky),
             time.strftime("%H:%M:%S", time.localtime(radky[0]["t"])),
             time.strftime("%H:%M:%S", time.localtime(radky[-1]["t"])),
             min(r["sw"] for r in radky), max(r["sw"] for r in radky)))
    print()
    vyhodnot(radky, prod, "PRODUKCE " + popis(prod))
    if "--mrizka" not in sys.argv:
        return 0
    print()
    for nad in (prod["pose_wrist_above_shoulder"], -0.4, -0.6):
        for vzorku in (prod["pose_min_samples"], 2):
            for swing in (prod["pose_min_swing"], 0.30, 0.25, 0.20):
                p = dict(prod, pose_wrist_above_shoulder=nad,
                         pose_min_samples=vzorku, pose_min_swing=swing)
                vyhodnot(radky, p, "         " + popis(p))
    return 0


if __name__ == "__main__":
    sys.exit(main())
