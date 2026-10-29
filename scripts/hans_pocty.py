"""HANS_POCTY_V1 (29. 10.) — jednoduché výpočty spočítá program, ne model.

Měření úspěšnosti 29. 10.: „Kolik je 7 krát 8?“ → „padesát dva“, „druhá mocnina
dvanácti“ → „stopětšedesát čtyři“ (dříve 27. 10. 12 × 15 = „118“). Malý model
násobí z hlavy a plete se s jistotou. Tady se z otázky vytáhne jeden jednoduchý
výpočet (dvě čísla a operace, mocnina, odmocnina, procenta), spočítá se BEZ
`eval` a do podkladu jde řádek „VÝPOČET: 7 × 8 = 56“; model výsledek jen vysloví.
Čísla číslicemi i slovy (0–100, „dvanácti“, „sedmi“). Slovní úlohy ne
(`data/NAPADY.md` → `POCTARSKY_NASTROJ`).
"""
from __future__ import annotations

import math
import re
import unicodedata
from typing import Optional


def _f(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", (s or "").lower())
                   if unicodedata.category(c) != "Mn")


# kmeny číselných slov (bez diakritiky) → hodnota; delší kmeny dřív
_SLOVA = [
    ("devatenact", 19), ("osmnact", 18), ("sedmnact", 17), ("sestnact", 16), ("patnact", 15),
    ("ctrnact", 14), ("trinact", 13), ("dvanact", 12), ("jedenact", 11),
    ("devadesat", 90), ("osmdesat", 80), ("sedmdesat", 70), ("sedesat", 60), ("padesat", 50),
    ("ctyricet", 40), ("ctyrice", 40), ("tricet", 30), ("trice", 30), ("dvacet", 20), ("dvace", 20),
    ("deset", 10), ("deseti", 10), ("devet", 9), ("devit", 9), ("osm", 8), ("sedm", 7),
    ("sest", 6), ("pet", 5), ("ctyr", 4), ("tri", 3), ("dva", 2), ("dvou", 2), ("dve", 2),
    ("jedn", 1), ("nul", 0), ("sto", 100),
]
_CIS = r"(\d+(?:[.,]\d+)?|[a-zA-ZáčďéěíňóřšťúůýžÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ]+)"


def _cislo(tok: str) -> Optional[float]:
    t = (tok or "").strip()
    if re.fullmatch(r"\d+(?:[.,]\d+)?", t):
        return float(t.replace(",", "."))
    w = _f(t)
    for kmen, hod in _SLOVA:
        if w.startswith(kmen) and len(w) - len(kmen) <= 3:
            return float(hod)
    return None


_OP = [
    (re.compile(r"\b" + _CIS + r"\s*(?:krat|x|×|\*|nasobeno)\s*" + _CIS + r"\b"), "×", lambda a, b: a * b),
    (re.compile(r"\b" + _CIS + r"\s*(?:plus|\+)\s*" + _CIS + r"\b"), "+", lambda a, b: a + b),
    (re.compile(r"\b" + _CIS + r"(?:\s*(?:minus|−)\s*|\s+-\s+)" + _CIS + r"\b"), "−", lambda a, b: a - b),
    (re.compile(r"\b" + _CIS + r"(?:\s*(?:deleno|lomeno|÷)\s*|\s+/\s+)" + _CIS + r"\b"), "÷",
     lambda a, b: a / b if b else None),
]
_MOC_RE = re.compile(r"\b(druh|tret)\w*\s+mocnin\w*\s+(?:z\s+|cisla\s+)?" + _CIS)
_NA_RE = re.compile(r"\b" + _CIS + r"\s+na\s+(druhou|treti)\b")
_ODM_RE = re.compile(r"\b(?:(druh|tret)\w*\s+)?odmocnin\w*\s+(?:z\s+|ze\s+|cisla\s+)?" + _CIS)
_PROC_RE = re.compile(r"\b" + _CIS + r"\s*(?:procent\w*|%)\s+z\s+" + _CIS)
_OTAZKA = re.compile(r"\b(kolik|vypocit\w*|spocit\w*|spocti|jaky\s+je\s+vysledek|cemu\s+se\s+rovna|"
                     r"jaka\s+je|jaky\s+je|kolik\s+je)\b")


def _fmt(x: float) -> str:
    if abs(x - round(x)) < 1e-9:
        return str(int(round(x)))
    return ("%.4f" % x).rstrip("0").rstrip(".").replace(".", ",")


def vypocet(text: str) -> Optional[str]:
    """Řádek „7 × 8 = 56“ pro jednoduchý výpočet v otázce, jinak None."""
    t = _f(re.sub(r"^\S+\s+se\s+pt[áa]:\s*", "", text or ""))
    if not t or not _OTAZKA.search(t):
        return None
    m = _MOC_RE.search(t) or None
    if m:
        a = _cislo(m.group(2))
        if a is not None:
            n = 2 if m.group(1).startswith("druh") else 3
            return "%s^%d = %s" % (_fmt(a), n, _fmt(a ** n))
    m = _NA_RE.search(t)
    if m:
        a = _cislo(m.group(1))
        if a is not None:
            n = 2 if m.group(2) == "druhou" else 3
            return "%s^%d = %s" % (_fmt(a), n, _fmt(a ** n))
    m = _ODM_RE.search(t)
    if m:
        a = _cislo(m.group(2))
        if a is not None and a >= 0:
            n = 3 if (m.group(1) or "").startswith("tret") else 2
            v = round(a ** (1.0 / n), 6) if n == 3 else math.sqrt(a)
            return "%s odmocnina z %s = %s" % ("třetí" if n == 3 else "druhá", _fmt(a), _fmt(v))
    m = _PROC_RE.search(t)
    if m:
        a, b = _cislo(m.group(1)), _cislo(m.group(2))
        if a is not None and b is not None:
            return "%s %% z %s = %s" % (_fmt(a), _fmt(b), _fmt(a * b / 100.0))
    for rx, znak, fn in _OP:
        m = rx.search(t)
        if m:
            a, b = _cislo(m.group(1)), _cislo(m.group(2))
            if a is None or b is None:
                continue
            v = fn(a, b)
            if v is None:
                return None
            return "%s %s %s = %s" % (_fmt(a), znak, _fmt(b), _fmt(v))
    return None
