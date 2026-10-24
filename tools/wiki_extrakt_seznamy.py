#!/usr/bin/env python3
"""WIKI_SEZNAMY_V1 (24. 10. 2026) — wikiextractor, který NEZAHAZUJE seznamy.

WikiExtractor 3.1 bez --html v `compact()` odrážkové a číslované řádky
přeskočí (`else: continue`) a sekci, která má jen seznam, vyřadí celou.
Lokální Wikipedie tak od 17. 10. neměla potomky, filmografie ani díla
(Karel IV. „Potomci“: jen nadpisy „1. manželka Blanka z Valois (1316–1348):“).
Tady se položka seznamu vypíše jako vlastní řádek (jako online výtah), i se
zbylými nadpisy sekce. Spouští `tools/wiki_offline_build.py extrahuj`.
"""
import re
import sys

import wikiextractor.extract as _ex

# seznamy odkazů a literatury jsou balast (FTS i výběr sekcí)
_BEZ = re.compile(r"^(?:literatura|externí odkazy|související články|reference|poznámky|"
                  r"odkazy|zdroje|bibliografie|další literatura|prameny)$", re.I)
_NADPIS = re.compile(r"^(=+)\s*(.*?)\s*\1\s*$")

_puvodni = _ex.compact


def compact(text, mark_headers=False, extractor=None):
    if extractor is not None and extractor.HtmlFormatting:
        return _puvodni(text, mark_headers=mark_headers, extractor=extractor)
    # řádky seznamu označit tak, aby je původní compact bral jako běžný text,
    # a značku pak odstranit
    radky = []
    sekce = ""
    for line in text.split("\n"):
        m = _NADPIS.match(line)
        if m:
            sekce = m.group(2)
        if line[:1] in ("*", "#", ";"):
            polozka = line.lstrip("*#;:").strip()
            radky.append("\x01" + polozka if polozka and not _BEZ.match(sekce) else "")
        else:
            radky.append(line)
    out = _puvodni("\n".join(radky), mark_headers=mark_headers, extractor=extractor)
    return [p.replace("\x01", "") for p in out]


_ex.compact = compact

if __name__ == "__main__":
    from wikiextractor import WikiExtractor as _we
    _we.compact = compact     # importuje-li si ho WikiExtractor jménem
    sys.argv[0] = "WikiExtractor"
    _we.main()
