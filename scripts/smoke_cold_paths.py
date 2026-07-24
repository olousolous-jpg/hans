#!/usr/bin/env python3
"""HANS_SMOKE_COLD_PATHS_V1 — smoke test 'studených' autonomních cest.

SPUSŤ PŘED restartem po úpravě proaktivní/notifikační/command vrstvy:
    python3 -m scripts.smoke_cold_paths

Proč: `py_compile`/import NECHYTÁ chybějící import použitý jen za běhu ve
zřídka volané metodě (NameError spolknutý na except → tiché selhání, jako
`sqlite3` bug v hans_matrix 24.7. — obraz se po domalování NIKDY nedoručil).

Princip: prožene každou studenou cestu s MOCKEM odesílání a ověří OBSERVABLE
efekt (že něco odeslala). Coding bug uvnitř → metoda spolkne výjimku → nic
neodešle → FAIL. Nezávisí na log úrovni.

Exit 0 = OK, 1 = nějaká cesta tiše selhala.
"""
import json
import sqlite3
import sys
import time

FAILS: list[str] = []


def _diary_max_artwork() -> int:
    try:
        con = sqlite3.connect("file:data/hans_diary.db?mode=ro", uri=True, timeout=3)
        row = con.execute("SELECT MAX(rowid) FROM diary WHERE "
                          "event_type='artwork'").fetchone()
        con.close()
        return int(row[0]) if row and row[0] else 0
    except Exception:
        return 0


def check_matrix_proactive(cfg):
    from scripts.hans_matrix import MatrixBridge
    b = MatrixBridge(cfg)
    b.room_id = "!smoke:local"
    sent: list = []
    b.send = lambda t, r=None: (sent.append(("TEXT", t)) or True)
    b.send_photo = lambda p, c="", r=None: (sent.append(("PHOTO", p)) or True)
    b.send_proactive = lambda t, r=None: (sent.append(("PROACT", t)) or True)

    # art delivery — nejsilnější kontrola (přesně sem spadl sqlite3 bug):
    # pending požadavek + baseline pod nejnovějším obrazem → MUSÍ doručit.
    maxid = _diary_max_artwork()
    if maxid:
        b._cmd_state["paint"] = {"pending": time.time()}
        b._last_art_id = maxid - 1
        try:
            b._maybe_deliver_requested_art()
        except Exception as e:
            FAILS.append(f"_maybe_deliver_requested_art: uncaught {type(e).__name__}: {e}")
        if not sent:
            FAILS.append("_maybe_deliver_requested_art: pending + existující obraz "
                         "→ NIC neodesláno (tiché selhání v DB/doručení?)")
    else:
        print("  (přeskočeno art delivery — v deníku není žádný obraz)")

    # calendar / questions / tick — nemají deterministický výstup (může legitimně
    # nic nebýt), takže jen že neshodí uncaught výjimkou.
    for nm, fn in [("_maybe_calendar_reminders", b._maybe_calendar_reminders),
                   ("_maybe_push_questions", b._maybe_push_questions),
                   ("_proactive_tick", b._proactive_tick)]:
        try:
            fn()
        except Exception as e:
            FAILS.append(f"{nm}: uncaught {type(e).__name__}: {e}")


def check_bridge_commands(cfg):
    from scripts import bridge_commands as bc

    def mk_ctx():
        out: list = []
        ctx = bc.BridgeCtx(
            send=lambda t: (out.append(("T", t)) or True),
            send_photo=lambda p, c="": (out.append(("P", p)) or True),
            person="olda", is_full=True, handler=None, config=cfg, state={})
        return ctx, out

    # tyto příkazy MAJÍ vždy něco odeslat (nápověda/stav/deník/úvaha/obraz)
    for cmd in ["/help", "/stav", "/denik", "/uvaha", "/obraz"]:
        ctx, out = mk_ctx()
        try:
            bc.handle(cmd, ctx)
        except Exception as e:
            FAILS.append(f"bridge handle({cmd}): uncaught {type(e).__name__}: {e}")
        if not out:
            FAILS.append(f"bridge handle({cmd}): NIC neodesláno (tiché selhání?)")

    # intent detekce — sanity, ať se regex cesta nerozbije
    for text, want in [("namaluj kočku", "paint"), ("jaký je stav systému", "status"),
                       ("vypni pc", "pcoff")]:
        got = bc.detect_intent(text)
        if got != want:
            FAILS.append(f"detect_intent({text!r}) = {got!r}, čekáno {want!r}")


def main():
    try:
        cfg = json.load(open("config.json"))
    except Exception as e:
        print(f"config.json nelze načíst: {e}")
        sys.exit(1)

    print("Smoke test studených cest…")
    for name, fn in [("matrix proaktivní vrstva", check_matrix_proactive),
                     ("bridge příkazy", check_bridge_commands)]:
        try:
            fn(cfg)
        except Exception as e:
            FAILS.append(f"[{name}] import/setup selhal: {type(e).__name__}: {e}")

    if FAILS:
        print("\nSMOKE FAIL — tiché selhání ve studené cestě:")
        for f in FAILS:
            print("  ✗", f)
        sys.exit(1)
    print("SMOKE OK — studené cesty proběhly a odeslaly očekávaný výstup.")
    sys.exit(0)


if __name__ == "__main__":
    main()
