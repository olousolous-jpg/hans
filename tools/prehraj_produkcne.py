#!/usr/bin/env python3
"""Prehraje `data/mereni/pose_raw.log` PRODUKCNI logikou gest.

Proc existuje (19. 9.): sesterský `prehraj_pose.py` prerusuje stopu pri kazdem
neuspesnem snimku (`useky()`), kdezto produkce drzi `_pose_stopa` TRVALE a maze
ji jen vystrel nebo rearm. V zasumenem okne proto podpocita — namerene 2
vystrely tam, kde produkce mela 6. Na porovnani variant nad tymz korpusem to
staci, na ABSOLUTNI pocty ne.

Tenhle nastroj misto toho:
  - vola PRODUKCNI `GestureClient._pose_je_mavani` (zadna kopie logiky),
  - drzi trvalou stopu, rearm (`wave_rearm_s`) i cooldown (`wave_cooldown_s`),
  - umi se VALIDOVAT proti `data/mereni/gesta.log`: musi trefit skutecne
    vystrely. ⛔ Bez te validace nemerit — 19. 9. prave ona odhalila, ze
    predchozi mereni merilo neco jineho.

Pouziti:
    python3 tools/prehraj_produkcne.py --validace
    python3 tools/prehraj_produkcne.py --od "2026-09-18 19:23" --do "2026-09-18 19:31"
    python3 tools/prehraj_produkcne.py --od ... --do ... --zmen pose_min_samples=5,pose_nose_required=1

Varianty v `--zmen` se prepisuji NAD config, takze jde zmerit navrh drive,
nez se patchuje (viz pravidlo "zmer i samu opravu" v CLAUDE.md).
"""
import argparse
import datetime
import sys
import time
from collections import deque
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import config_io                                   # noqa: E402
import gesture_client as gc                        # noqa: E402

RAW = ROOT / "data/mereni/pose_raw.log"
GESTA = ROOT / "data/mereni/gesta.log"


def nacti_syrove(cesta=RAW):
    """Radky: cas, strana, duvod, nad, loket, od nosu, sirka ramen, vychylka
    [, jistota oci, odklon hlavy].

    🔴 OSM NEBO DESET SLOUPCU. HANS_GESTURE_POSE_TVAR_V1 (21. 9.) pripsal dva
    na konec; puvodni `len(p) != 8: continue` by kazdy NOVY radek TISE zahodil
    a korpus by se tvaril prazdny presne ve chvili, kdy zacne narustat.
    Starsi radky tvar nenesou — dostanou (-1, 9.0), tedy "nezmereno".
    """
    ven = []
    for radek in open(cesta, encoding="utf-8", errors="replace"):
        p = radek.rstrip("\n").split("\t")
        if len(p) not in (8, 10):
            continue
        try:
            oci = float(p[8]) if len(p) == 10 else -1.0
            odklon = float(p[9]) if len(p) == 10 else 9.0
            ven.append((float(p[0]), p[1], float(p[3]), float(p[4]),
                        float(p[5]), float(p[6]), float(p[7]), oci, odklon))
        except ValueError:
            continue
    ven.sort(key=lambda r: r[0])
    return ven


def prahy(zmen=None):
    cfg = dict(config_io.load().get("gesture") or {})
    cfg.update(zmen or {})
    c = object.__new__(gc.GestureClient)
    c._pose = None
    c._pose_cfg(cfg)                                # produkcni nacteni prahu
    return c, cfg


def prehraj(radky, od, do, zmen=None):
    """Vrati casy vystrelu. Replikuje `_update_pose` krome bboxu a snimku."""
    c, cfg = prahy(zmen)
    p = c._pose
    rearm = float(cfg.get("wave_rearm_s", 4.0))
    cool = float(cfg.get("wave_cooldown_s", 5.0))
    stopa = {"L": deque(maxlen=60), "P": deque(maxlen=60)}
    armed, naposled_vystrel, naposled = True, -1e9, -1e9
    fires, i = [], 0
    R = [r for r in radky if (od is None or r[0] >= od) and (do is None or r[0] <= do)]
    while i < len(R):
        t = R[i][0]
        ram = []
        while i < len(R) and R[i][0] == t:          # jeden snimek = oba pazy
            ram.append(R[i])
            i += 1
        zvednuto = []
        for (_t, jm, nad, loket, odnosu, sw, dx, oci, odklon) in ram:
            if sw < p["pose_min_shoulder_h"]:
                continue
            # HANS_GESTURE_POSE_TVAR_V1 — brana pozornosti.
            # 🔴 Radek BEZ zmerene tvare (oci < 0, starsi korpus) se pousti
            # DAL. Kdyby se zahazoval, vysla by kazda varianta s touhle branou
            # jako zazracne zlepseni — zahodila by proste cely stary korpus.
            # Cena teto brany je tedy meritelna AZ na radcich od 21. 9.
            if oci >= 0.0:
                if p["pose_face_min_eye_conf"] > 0 and oci < p["pose_face_min_eye_conf"]:
                    continue
                if odklon > p["pose_face_max_yaw"]:
                    continue
            # v syrovem logu se nedostupny nos pozna podle `odnosu` 9.0
            if p["pose_nose_required"] and odnosu >= 8.0:
                continue
            if nad < p["pose_wrist_above_shoulder"] or nad > p["pose_wrist_above_max"]:
                continue
            if loket < p["pose_elbow_min"] or odnosu < p["pose_wrist_from_nose"]:
                continue
            stopa[jm].append((_t, dx, nad, loket, odnosu, 0.0, 0.0, sw))
            zvednuto.append(jm)
        if not zvednuto:
            continue
        _predchozi, naposled = naposled, t
        if not armed:
            if t - _predchozi >= rearm:
                armed = True
                for s in stopa.values():
                    s.clear()
            continue
        if t - naposled_vystrel < cool:
            continue
        for jm in zvednuto:
            popis, _ne = c._pose_je_mavani(list(stopa[jm]), t)
            if popis is None:
                continue
            naposled_vystrel, armed = t, False
            for s in stopa.values():
                s.clear()
            fires.append((t, jm, popis))
            break
    return fires


def skutecne(od, do):
    """Vystrely zapsane produkci do gesta.log — ground truth pro validaci."""
    ven = []
    for radek in open(GESTA, encoding="utf-8", errors="replace"):
        p = radek.rstrip("\n").split("\t")
        if len(p) != 2:
            continue
        try:
            t = datetime.datetime.strptime(p[0], "%Y-%m-%d %H:%M:%S").timestamp()
        except ValueError:
            continue
        if (od is None or t >= od) and (do is None or t <= do):
            ven.append(t)
    return ven


def cas(s):
    return datetime.datetime.strptime(s, "%Y-%m-%d %H:%M").timestamp()


def hm(t):
    return time.strftime("%H:%M:%S", time.localtime(t))


# 🔴 Validace MUSI bezet s prahy, ktere byly zive V DOBE ZAZNAMU — ne
# s dnesnimi. Jinak zarve pokazde, kdyz se prah zmeni, a to je prave to, co
# se od opravy ceka. Vsechna ctyri okna vznikla mezi 18. 9. 16:30 (nasazeni
# `pose_wrist_above_max`) a 19. 9. 10:35 (nasazeni `pose_nose_required`),
# takze sdileji jednu sadu.
HISTORICKE = {"pose_min_shoulder_h": 0.08, "pose_wrist_above_shoulder": -0.4,
              "pose_wrist_above_max": 0.05, "pose_elbow_min": -0.8,
              "pose_wrist_from_nose": 0.5, "pose_nose_required": 0.0,
              "pose_min_samples": 3, "pose_min_swing": 0.4,
              "pose_min_reversals": 1, "pose_eps": 0.1, "pose_window_s": 2.0,
              # tvar se 18.-19. 9. jeste nemerila → brana vypnuta
              "pose_face_min_eye_conf": 0.0, "pose_face_max_yaw": 99.0}

# Okna se znamou pravdou; ocekavany pocet se cte z gesta.log.
VALIDACE = [("falešné 18. 9. večer", "2026-09-18 19:23", "2026-09-18 19:31"),
            ("falešné 19. 9. ráno", "2026-09-19 10:00", "2026-09-19 10:05"),
            ("pravé 18. 9. test", "2026-09-18 16:33", "2026-09-18 16:36"),
            ("pravé 19. 9. ráno", "2026-09-19 09:11", "2026-09-19 09:14")]


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--od")
    ap.add_argument("--do")
    ap.add_argument("--zmen", default="")
    ap.add_argument("--validace", action="store_true")
    a = ap.parse_args()

    zmen = {}
    for kus in filter(None, a.zmen.split(",")):
        k, _, v = kus.partition("=")
        zmen[k.strip()] = float(v)
    radky = nacti_syrove()
    if not radky:
        print("Syrový korpus je prázdný — běží `wave_reject_log`?")
        return 1
    print("korpus %d vzorků, %s – %s" % (len(radky),
          time.strftime("%d.%m. %H:%M", time.localtime(radky[0][0])),
          time.strftime("%d.%m. %H:%M", time.localtime(radky[-1][0]))))
    if zmen:
        print("varianta nad configem: %s" % zmen)

    if a.validace:
        print("\n⛔ Dokud tahle validace neprojde, naměřená čísla nic neznamenají.")
        print("   (běží s prahy platnými v době záznamu, ne s dnešními)")
        ok = True
        for popis, od_s, do_s in VALIDACE:
            od, do = cas(od_s), cas(do_s)
            mam = len(prehraj(radky, od, do, HISTORICKE))
            ceka = len(skutecne(od, do))
            sedi = mam == ceka
            ok &= sedi
            print("  %-22s přehrátí %d × gesta.log %d   %s"
                  % (popis, mam, ceka, "OK" if sedi else "!! NESEDÍ"))
        print("  → %s" % ("validace prošla" if ok else "NEPOUŽÍVEJ, přehrává se něco jiného"))
        if not zmen:
            return 0 if ok else 1

    od = cas(a.od) if a.od else None
    do = cas(a.do) if a.do else None
    if od or do:
        f = prehraj(radky, od, do, zmen)
        print("\nvýstřelů: %d" % len(f))
        for t, jm, popis in f:
            print("  %s  paže %s  %s" % (hm(t), jm, popis.replace("postava: ", "")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
