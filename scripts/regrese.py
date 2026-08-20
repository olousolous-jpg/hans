#!/usr/bin/env python3
"""regrese.py — RYCHLÁ regresní sada (bez LLM, bez sítě).

PROČ EXISTUJE (20.8., nápad uživatele):
  Opravy posledních dnů stojí na konkrétních doložených větách („co ses o tom
  divadle dozvěděl?", „si zapiš…", „umíte pustit něco na televizi?"). Ležely
  ale roztroušené v próze backlogu, takže se nedaly SPUSTIT — a bez toho je
  plánovaný úklid zaplátovaných funkcí (send_chat_message 702 ř.,
  _build_system 744 ř.) hazard: nikdo nepozná, co přepis rozbil.

CO TESTUJE A CO NE:
  ✅ deterministické rozhodovací funkce — směrování příkazů, hranice vzorů,
     převody textu, pomocné predikáty. Běží ve VTEŘINÁCH a nepotřebuje PC.
  ❌ NEtestuje odpovědi modelu ani celý řetěz chatu. To je „živá vrstva",
     která potřebuje zapnuté PC a minuty; sem záměrně nepatří, ať tahle sada
     jde spustit po každé změně.

PROČ SE TVRDÍ TAKHLE:
  Text odpovědi se mění s modelem, ROZHODNUTÍ ne. Proto se tvrdí, KTERÁ cesta
  větu obslouží a co vrátí čistá funkce — ne jak to Hans nakonec formuluje
  ([[test-the-fix-not-the-symptom]]).

DATA JSOU JINDE:  data/regrese_cases.json  (gitignored — reálné věty z
  domácnosti). Tenhle soubor je jen mechanismus, aby šel commitnout.

SPUŠTĚNÍ:  python3 scripts/regrese.py [cesta_k_json]
  Návratový kód 0 = vše prošlo, 1 = aspoň jeden případ selhal.
"""
from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

CASES = Path("data/regrese_cases.json")


def _volej(fq: str, argumenty: list):
    """Zavolá 'modul.funkce' se seznamem argumentů. Modul se importuje lazy,
    ať sada nespadne kvůli závislosti, kterou zrovna netestujeme."""
    mod, _, fn = fq.rpartition(".")
    m = importlib.import_module(mod)
    f = getattr(m, fn)
    return f(*argumenty)


def _vysledek(pripad: dict):
    """Vrátí (prosel, skutecnost, duvod)."""
    try:
        got = _volej(pripad["funkce"], pripad.get("argumenty", []))
    except Exception as e:
        return False, None, f"výjimka: {type(e).__name__}: {e}"
    # některé funkce vracejí n-tici (parse_command) → volitelný index
    if "index" in pripad and isinstance(got, (list, tuple)):
        got = got[pripad["index"]] if got else None
    if "rovna" in pripad:
        return got == pripad["rovna"], got, ""
    if "obsahuje" in pripad:
        return pripad["obsahuje"] in str(got or ""), got, ""
    if "neobsahuje" in pripad:
        return pripad["neobsahuje"] not in str(got or ""), got, ""
    return False, got, "případ nemá tvrzení (rovna/obsahuje/neobsahuje)"


def main() -> int:
    cesta = Path(sys.argv[1]) if len(sys.argv) > 1 else CASES
    if not cesta.exists():
        print(f"Soubor s případy nenalezen: {cesta}")
        return 1
    data = json.loads(cesta.read_text(encoding="utf-8"))
    pripady = data.get("pripady", [])
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

    padlo, skupina_teď = [], None
    for p in pripady:
        sk = p.get("skupina", "")
        if sk != skupina_teď:
            skupina_teď = sk
            print(f"\n── {sk}")
        ok, got, duvod = _vysledek(p)
        popis = p.get("popis") or str(p.get("argumenty", [""])[0])[:60]
        if ok:
            print(f"   OK    {popis}")
        else:
            padlo.append(p)
            ocek = p.get("rovna", p.get("obsahuje", p.get("neobsahuje")))
            print(f"   CHYBA {popis}")
            print(f"         čekáno {ocek!r}, vráceno {got!r} {duvod}")
            if p.get("puvod"):
                print(f"         původ: {p['puvod']}")
    print(f"\n{len(pripady) - len(padlo)}/{len(pripady)} prošlo")
    return 1 if padlo else 0


if __name__ == "__main__":
    raise SystemExit(main())
