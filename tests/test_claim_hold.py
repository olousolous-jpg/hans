#!/usr/bin/env python3
"""HANS_CLAIM_HOLD_V1 — trvalý test: drží se Hans svého záznamu?

Z nálezu 23.8.: Hans přečetl deník správně („Naposledy jsem Henku viděl před
23 minutami"), a když jsem mu podsunul jiný čas, přijal MOU nepravdu jako údaj
z vlastního deníku. Test hlídá obě strany — že se spor pozná, i že se
nepozná tam, kde se poznat nemá (falešné držení by bylo horší než žádné).

Spuštění:  python3 tests/test_claim_hold.py
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts.claim_hold import disputed_last_seen, hold

ZAZNAM = ("Naposledy jsem Henku viděl před 23 minutami; předtím v neděli "
          "23. srpna 2026 v 09:42. Tak to mám zapsáno v deníku.")
TED = "Vidím Henku právě teď, pane. Tak to mám zapsáno v deníku."
JINE = ("Vyšetřování ztráty třídní knihy je hra Divadla Járy Cimrmana. "
        "Záznamy mi neříkají více.")


def h(*repliky):
    """historie jako z conv_store: střídavě uživatel a Hans"""
    out = []
    for i, t in enumerate(repliky):
        out.append({"role": "assistant" if i % 2 else "user", "content": t})
    return out


CASES = [
    # (popis, dotaz, historie, má se spustit?)
    ("odkaz na řečené + čerstvý záznam",
     "pred chvili jsi rikal ze naposledy ve 12:15. ktera odpoved plati?",
     h("kdy jsi videl henku?", ZAZNAM), True),
    ("s diakritikou",
     "říkal jsi ale něco jiného, tak která odpověď platí?",
     h("kdy jsi viděl Henku?", ZAZNAM), True),
    ("tvrdil jsi",
     "tvrdil jsi, že tu byla ráno. to nesedí.",
     h("kdy jsi viděl Henku?", ZAZNAM), True),
    ("spor nad „vidím právě teď“",
     "opravdu? vzdyt jsi rikal ze tu nikdo neni",
     h("je tu někdo?", TED), True),
    ("prosté zpochybnění",
     "urcite?", h("kdy jsi viděl Henku?", ZAZNAM), True),

    # ── nesmí se spustit ─────────────────────────────────────────────────
    ("spor, ale poslední tvrzení není o vidění",
     "pred chvili jsi rikal neco jineho, ktera odpoved plati?",
     h("co je zač ta hra?", JINE), False),
    ("záznam ve vlákně, ale žádný spor",
     "a kdy tu byla predtim?", h("kdy jsi viděl Henku?", ZAZNAM), False),
    ("prázdná historie",
     "pred chvili jsi rikal ze ve 12:15", [], False),
    ("spor je ve zprávě UŽIVATELE, ne v Hansově replice",
     "ktera odpoved plati?",
     h("naposledy jsem Henku viděl ve 12:15"), False),
    ("běžná otázka",
     "kdy jsi viděl Henku?", h("ahoj", "Dobrý den, pane."), False),
    ("prázdný dotaz", "", h("x", ZAZNAM), False),
]


def main() -> int:
    ok = bad = 0
    for popis, dotaz, hist, ceka in CASES:
        got = disputed_last_seen(dotaz, hist) is not None
        if got == ceka:
            ok += 1
            print(f"  ✓ {'spustí ' if ceka else 'nechá  '} | {popis}")
        else:
            bad += 1
            print(f"  ✗ {popis}: dostal {got}, čekáno {ceka}")
    # ── uvození odpovědi ──────────────────────────────────────────────────
    _v = hold("Naposledy jsem Henku viděl před 23 minutami. "
              "Tak to mám zapsáno v deníku.",
              "pred chvili jsi rikal ze naposledy ve 12:15. ktera plati?")
    for popis, podminka in (
            ("uvozuje trváním na záznamu", _v.startswith("Trvám na tom")),
            ("nezdvojuje koncovou formulku", "zapsáno v deníku" not in _v),
            ("pojmenuje podsunutý čas", "12:15 v záznamu nemám" in _v),
            ("nese původní údaj", "před 23 minutami" in _v)):
        if podminka:
            ok += 1; print(f"  ✓ hold()   | {popis}")
        else:
            bad += 1; print(f"  ✗ hold(): {popis} — {_v[:90]}")
    if hold("Vidím Henku právě teď.", "opravdu?").endswith("právě teď.") :
        ok += 1; print("  ✓ hold()   | bez času v dotazu nic nedolepuje")
    else:
        bad += 1; print("  ✗ hold() dolepuje i bez času v dotazu")
    print(f"\nOK={ok}  CHYB={bad}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
