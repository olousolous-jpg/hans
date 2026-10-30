"""
HANS_SMISENE_PISMO_V1 (27. 10.) — slovo smíchané z latinky a cyrilice.

Model občas vloží doprostřed českého slova cyrilská písmena („vylouчено“,
„objeвили“, „háдали“). V chatu je to vzácné (testy 1 z 2 359 odpovědí), ale
v rozhovorech Hanse s Koláčem 12 z 1 910 — a ty jdou do deníku a odtud do
večerní reflexe. Slovo, které má písmena OBOU abeced, se přepíše do latinky;
celá ruská slova („Рихард Зорге“ v odpovědi o Sorgem) zůstanou.
"""
from __future__ import annotations

import re

_MAPA = {
    "а": "a", "б": "b", "в": "v", "г": "h", "ґ": "g", "д": "d", "е": "e", "ё": "e",
    "є": "e", "ж": "ž", "з": "z", "и": "i", "і": "i", "ї": "i", "й": "j", "к": "k",
    "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t",
    "у": "u", "ф": "f", "х": "ch", "ц": "c", "ч": "č", "ш": "š", "щ": "šč", "ъ": "",
    "ы": "y", "ь": "", "э": "e", "ю": "ju", "я": "ja",
}
_CYR = re.compile(r"[Ѐ-ӿ]")
_LAT = re.compile(r"[A-Za-zÀ-ſ]")
_SLOVO = re.compile(r"[\wЀ-ӿ]+")


def _prepis(slovo: str) -> str:
    out = []
    for ch in slovo:
        lo = ch.lower()
        if lo in _MAPA:
            t = _MAPA[lo]
            out.append(t.capitalize() if ch != lo and t else t)
        else:
            out.append(ch)
    return "".join(out)


def oprav_smisene_pismo(text: str) -> str:
    """Slova s latinkou i cyrilicí → latinka; ostatní beze změny."""
    t = str(text or "")
    if not _CYR.search(t):
        return t
    return _SLOVO.sub(lambda m: _prepis(m.group(0))
                      if (_CYR.search(m.group(0)) and _LAT.search(m.group(0)))
                      else m.group(0), t)
