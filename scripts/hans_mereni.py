# -*- coding: utf-8 -*-
"""MERENI_PODKLAD_V1 (13. 10.) — nahrávka podkladu, který model dostal, pro měření.

Přepisy zkušebních rozhovorů ani stopy text podkladu neukládají, takže nešlo
zpětně spočítat, kolik výmyslů by zachytila kontrola konkrétních údajů
(letopočet, číslo, vlastní jméno mimo podklad).

Vypnuto, dokud neexistuje soubor `data/mereni/.nahravej_podklad`. Zapisuje JEN
u zkušebních identit (`config.test_persons`) do `data/mereni/podklady.jsonl`;
u členů domácnosti nikdy. Chyba se spolkne — měření nesmí shodit odpověď.
"""
from __future__ import annotations

import json
import os
import time

PRIZNAK = "data/mereni/.nahravej_podklad"
SOUBOR = "data/mereni/podklady.jsonl"


def nahraj_podklad(config: dict, name: str, veta: str, prepis: str,
                   podklad) -> bool:
    try:
        if not os.path.exists(PRIZNAK):
            return False
        tp = [str(x).strip().lower() for x in (config.get("test_persons") or [])]
        if (name or "").strip().lower() not in tp:
            return False
        with open(SOUBOR, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": time.time(), "cas": time.strftime("%Y-%m-%d %H:%M:%S"),
                "osoba": name, "veta": veta, "prepis": prepis or "",
                "podklad": podklad if isinstance(podklad, str) else ""},
                ensure_ascii=False) + "\n")
        return True
    except Exception:
        return False
