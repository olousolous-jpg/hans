#!/usr/bin/env python3
"""
servo_poke — bezpečné testování jednoho serva malými kroky.

Pošle servo na zadaný úhel JEMNÝM rampem (1°/krok) z naposledy zapamatované
polohy (per kanál v /tmp). Neresetuje MCU → bezpečně koexistuje s běžícím Hansem.
Sahá jen na zadaný kanál (test serva na P9/P10/P11; Hans má P0–P3).

Použití:
  python3 tools/servo_poke.py 9 0          # jemně na center (bezpečný 1. krok)
  python3 tools/servo_poke.py 9 10         # ramp na +10°
  python3 tools/servo_poke.py 9 -10        # ramp na -10°
  python3 tools/servo_poke.py 9 --release  # pulse_width(0) → limp/ticho
  python3 tools/servo_poke.py 9 20 --limit 30   # dočasně povol větší rozsah

STOP při bzučení/odporu = doraz! Poslední ÚSPĚŠNÝ úhel je mez.
"""
import sys
import time
import json
from pathlib import Path

from robot_hat import Servo

RAMP_STEP = 1.0     # stupeň na dílčí krok
RAMP_DELAY = 0.04   # s mezi dílčími kroky
DEFAULT_LIMIT = 40.0


def _state_path(ch_num: int) -> Path:
    return Path(f"/tmp/servo_poke_P{ch_num}.json")


def _load_last(ch_num: int) -> float:
    p = _state_path(ch_num)
    if p.exists():
        try:
            return float(json.loads(p.read_text()).get("angle", 0.0))
        except Exception:
            pass
    return 0.0


def _save_last(ch_num: int, angle: float):
    try:
        _state_path(ch_num).write_text(json.dumps({"angle": angle}))
    except Exception:
        pass


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return
    ch_num = int(args[0].lstrip("Pp"))
    ch = f"P{ch_num}"
    release = "--release" in args
    limit = DEFAULT_LIMIT
    if "--limit" in args:
        limit = float(args[args.index("--limit") + 1])
    delay = RAMP_DELAY
    if "--delay" in args:
        delay = float(args[args.index("--delay") + 1])
    if "--slow" in args:
        delay = 0.12

    servo = Servo(ch)

    if "--twitch" in args:
        # Nejjemnější test: krátké pulzy U NEUTRÁLU (±8° od středu), každý držet
        # jen ~0.4 s a HNED utnout. Dobré servo cukne malinko u středu a zastaví
        # (nikdy se nepřiblíží dorazu). Vadné by se točilo, ale pulz utneme dřív.
        seq = [0.0, -8.0, 8.0, 0.0]
        print(f"{ch}: twitch test u neutrálu (±8°) — sleduj, jestli CUKNE a zastaví, nebo se TOČÍ")
        try:
            for ang in seq:
                servo.angle(ang)
                print(f"  → {ang:+.0f}° (krátký pulz)")
                time.sleep(0.4)
                servo.pulse_width(0)     # utni → limp, ať neujede
                time.sleep(0.5)
            print(f"{ch}: hotovo, servo uvolněno. Cuklo a zastavilo = OK; točilo se = vadná zpětná vazba.")
        except KeyboardInterrupt:
            servo.pulse_width(0)
            print(f"\n{ch}: STOP — uvolněno")
        return

    if release:
        try:
            servo.pulse_width(0)
            print(f"{ch}: uvolněno (pulse_width 0) — limp/ticho")
        except Exception as e:
            print(f"{ch}: release selhal: {e}")
        return

    # cílový úhel = první nenázvový číselný argument po kanálu
    target = None
    for a in args[1:]:
        try:
            target = float(a)
            break
        except ValueError:
            continue
    if target is None:
        print("Chybí cílový úhel. Příklad: python3 tools/servo_poke.py 9 10")
        return

    target = max(-limit, min(limit, target))
    cur = _load_last(ch_num)
    near_limit = abs(target) >= (limit - 5)
    print(f"{ch}: {cur:+.1f}° → {target:+.1f}°  (limit ±{limit:.0f}°)"
          + ("  !! BLÍZKO LIMITU — sleduj odpor" if near_limit else ""))

    d = RAMP_STEP if target > cur else -RAMP_STEP
    a = cur
    steps = int(abs(target - cur) / RAMP_STEP) + 2
    try:
        for _ in range(steps):
            a += d
            if (d > 0 and a > target) or (d < 0 and a < target):
                a = target
            servo.angle(a)
            _save_last(ch_num, a)        # ukládej průběžně → po Ctrl-C ramp pokračuje odsud
            time.sleep(delay)
            if a == target:
                break
        print(f"{ch}: na poloze {target:+.1f}°  (drží — pulse_width(0) pro ticho)")
    except KeyboardInterrupt:
        print(f"\n{ch}: STOP na {a:+.1f}° (přerušeno)")


if __name__ == "__main__":
    main()
