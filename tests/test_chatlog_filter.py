# -*- coding: utf-8 -*-
"""HANS_CHATLOG_NOT_FACT_V3 — co smí a nesmí groundovat faktickou odpověď.

Deterministický (žádný LLM ani síť): skládá dokumenty přesně tak, jak je staví
`hans_synthesis._build_rag_text`, a pouští na ně produkční `_CHATLOG_RE`.

Hlídá OBĚ strany:
  • Hansovy VLASTNÍ výroky (chat, dialog s Koláčem) se do faktické cesty nesmí,
  • doklady o světě (četba, film, kniha, případ) tam musí zůstat — nadměrný
    filtr by kolekci vyprázdnil a odpověď by spadla na horší zdroj.
Spuštění:  python3 tests/test_chatlog_filter.py
"""
import sys
import time

sys.path.insert(0, ".")

from scripts.hans_synthesis import HansSynthesisHooks as S
from scripts.openwebui_direct_handler import OpenWebUIDirectHandler as H

TS = time.time()

# (jméno, text, má být odfiltrován?)
CASES = [
    ("teddy_dialog — dialog s Koláčem",
     S._build_rag_text("teddy_dialog", "Dialog s Kolačem",
                       "Téma: Český ráj\n\nHans: zámek Gutštejn vykazuje...",
                       "úvaha", TS), True),
    ("human_chat přes synthesis",
     S._build_rag_text("human_chat", "Rozhovor", "standa: ...\nHans: ...",
                       "dojem", TS), True),
    ("chatlog z _upload_chat_memory",
     "Rozhovor s standa (pondělí 24.8.2026 10:00):\n"
     "[NEOVĚŘENO — vlastní výrok v hovoru, ne ověřený fakt]\n"
     "standa: x\nHans: y", True),
    ("web_read — doklad o světě",
     S._build_rag_text("web_read", "Hrad Kost",
                       "Hrad Kost je gotický hrad v Českém ráji...",
                       "úvaha", TS), False),
    ("kodi_playing — co se hrálo",
     S._build_rag_text("kodi_playing", "Pátý element", "Typ: film | rok 1997",
                       "úvaha", TS), False),
    ("case_opened — případ",
     S._build_rag_text("case_opened", "Kauza X", "Stopa: ...", "úvaha", TS),
     False),
    ("book_read — četba",
     S._build_rag_text("book_read", "Pride and Prejudice", "kap. 60",
                       "úvaha", TS), False),
]

# Okno musí stačit i na delší titulek — sekce „## Rozhovor s …" leží až za ním.
CASES.append((
    "teddy_dialog s dlouhým titulkem (okno 200)",
    S._build_rag_text(
        "teddy_dialog",
        "Dialog s Kolačem o Českém ráji a okolních hradech a zříceninách",
        "Téma: hrady\n\nHans: ...", "úvaha", TS), True))


def main() -> int:
    ok = 0
    for name, text, want in CASES:
        got = bool(H._CHATLOG_RE.search(text[:200]))
        good = (got == want)
        ok += good
        print("%s  %-44s filtr=%-8s ceka=%s"
              % ("OK  " if good else "CHYBA", name[:44],
                 "ZACHYCEN" if got else "projde",
                 "ZACHYCEN" if want else "projde"))
    print("\n%d/%d" % (ok, len(CASES)))
    return 0 if ok == len(CASES) else 1


if __name__ == "__main__":
    sys.exit(main())
