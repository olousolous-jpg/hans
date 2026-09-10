#!/usr/bin/env python3
"""Audit mrtvých konfiguračních klíčů — podklad pro čištění kódu.

Najde klíče v `config.json`, které ŽÁDNÝ kód nečte, a rozliší dvě
závažnosti:
  (A) klíč je v `config_schema.py` → uživatel ho VIDÍ ve web adminu
      a může ho editovat, jenže editace nic nedělá = past na uživatele;
  (B) klíč nikde → jen balast v configu.

Metoda: každý skalární klíč se hledá v kódu jako "klic" / 'klic'.
`config_schema.py` je z hledání VYŇAT (jinak by se každé UI pole
označilo za živé samo sebou).

⚠️ HRUBÝ GREP LŽE OBĚMA SMĚRY — výsledek je seznam KANDIDÁTŮ, ne verdikt:
  - falešně ŽIVÝ: klíč se čte dynamicky (`cfg.get(promenna)`), např.
    v cyklu přes seznam jmen → tenhle audit ho neuvidí jako mrtvý;
  - falešně MRTVÝ: naopak nehrozí, ale POZOR na opak — `model_path`
    vypadal živě při hrubém `grep model_path` (6 výskytů), jenže všechny
    byly LOKÁLNÍ PROMĚNNÉ (`model_path = Path(...)`), ne čtení configu.
Před smazáním klíč vždy ověř ručně: `grep -rn '"<klic>"' scripts/`.

Vynechává `_*` a `*_note` — to je dokumentace, ne mrtvý kód.

Spuštění:  python3 tools/audit_mrtve_klice.py
"""
import json, re, glob, os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def nacti_kod() -> str:
    kod = []
    vzory = ["scripts/*.py", "*.py", "deploy/**/*.py", "tools/*.py"]
    for v in vzory:
        for f in glob.glob(os.path.join(ROOT, v), recursive=True):
            if f.endswith("config_schema.py"):
                continue          # UI definice by potvrdila samu sebe
            try:
                kod.append(open(f, encoding="utf-8").read())
            except Exception:
                pass
    return "\n".join(kod)


def audit():
    cfg = json.load(open(os.path.join(ROOT, "config.json"), encoding="utf-8"))
    kod = nacti_kod()
    schema = open(os.path.join(ROOT, "scripts/config_schema.py"),
                  encoding="utf-8").read()
    mrtve, celkem = [], 0

    def chodi(pref, d):
        nonlocal celkem
        for k, v in (d or {}).items():
            if k.startswith("_") or k.endswith("_note"):
                continue          # dokumentace, ne kod
            if isinstance(v, dict):
                chodi(pref + k + ".", v)
                continue
            celkem += 1
            if not re.search(r'["\']%s["\']' % re.escape(k), kod):
                mrtve.append((pref + k, repr(v)[:60], k in schema))

    chodi("", cfg)
    return celkem, mrtve


if __name__ == "__main__":
    celkem, mrtve = audit()
    v_ui = [m for m in mrtve if m[2]]
    balast = [m for m in mrtve if not m[2]]
    print("skalárních klíčů (bez _* a *_note): %d" % celkem)
    print("nikdo je nečte: %d\n" % len(mrtve))
    print("(A) VIDITELNÉ v config_schema.py — past na uživatele: %d" % len(v_ui))
    for k, v, _ in sorted(v_ui):
        print("    %-46s = %s" % (k, v))
    print("\n(B) jen balast v config.json: %d" % len(balast))
    for k, v, _ in sorted(balast):
        print("    %-46s = %s" % (k, v))
    print("\n⚠️ Před smazáním ověř ručně: grep -rn '\"<klic>\"' scripts/")
