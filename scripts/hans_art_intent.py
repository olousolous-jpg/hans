"""
HANS_ART_INTENT_V1 (5.8.2026) — TRVALÉ TVŮRČÍ ZÁMĚRY.

Proč vzniklo: Hans za 30 dní namaloval 141 obrazů a ke každému si uložil
`art_lesson`. Ta ponaučení jsou textově unikátní (115 ze 115), ale slovníkem
zaměnitelná — „introduce subtle X to enhance visual depth" v mnoha převlecích
(subtle 40×, introduce 39×, enhance 34×, visual 42×). Do promptu se vracela
POSLEDNÍ TŘI, takže Hans dokola dostával tři náhodné variace téže rady:
vypadá to jako učení, ale nic se nekumuluje. Běžecký pás, ne trajektorie.

Záměr je jiná věc než ponaučení:
  • ponaučení = reakce na JEDEN obraz („v tomhle chybí hloubka pozadí")
  • ZÁMĚR     = co Hans dlouhodobě sleduje („chci, aby v obrazech bylo ticho")

Záměrů je málo (2–3), mění se POMALU (destiluje je týdenní reflexe tvorby
z reálných děl a ponaučení, ne z ničeho) a drží se, dokud je reflexe nezmění.
Tím dostane tvorba směr, na který může navázat i kontinuita díla (krok 2).

Anti-konfabulace: záměry se NEVYMÝŠLEJÍ — vznikají destilací toho, co Hans
SKUTEČNĚ vytvořil a co si k tomu zapsal. Když destilace selže (mozek dole),
staré záměry zůstávají; prázdný výsledek nikdy nepřepíše neprázdný.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from typing import List, Optional

_log = logging.getLogger("hans_art_intent")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS art_intentions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    text       TEXT NOT NULL,
    created_ts REAL NOT NULL,
    retired_ts REAL NOT NULL DEFAULT 0,
    source     TEXT NOT NULL DEFAULT 'creation_reflection'
)
"""


def _conn(db_path: str):
    c = sqlite3.connect(db_path, timeout=5.0)
    c.execute(_SCHEMA)
    return c


def active_intentions(db_path: str, limit: int = 3) -> List[str]:
    """Aktuální trvalé záměry (nejnovější první). [] když žádné."""
    try:
        c = _conn(db_path)
        try:
            rows = c.execute(
                "SELECT text FROM art_intentions WHERE retired_ts=0 "
                "ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
        finally:
            c.close()
        return [r[0] for r in rows if (r[0] or "").strip()]
    except Exception as e:
        _log.debug("active_intentions: %s", e)
        return []


def set_intentions(db_path: str, texts: List[str],
                   source: str = "creation_reflection") -> int:
    """Nahraď záměry novými. Prázdný vstup NIC nezmění (mozek dole ≠ ztráta
    směru). Vrací počet nových záměrů."""
    fresh = [t.strip() for t in (texts or []) if t and t.strip()]
    if not fresh:
        _log.debug("set_intentions: prázdný vstup → staré záměry zůstávají")
        return 0
    now = time.time()
    try:
        c = _conn(db_path)
        try:
            # staré NEMAŽEME — jen odejdou do penze, ať je vidět vývoj směru
            c.execute("UPDATE art_intentions SET retired_ts=? WHERE retired_ts=0",
                      (now,))
            for t in fresh[:3]:
                c.execute("INSERT INTO art_intentions (text, created_ts, source) "
                          "VALUES (?,?,?)", (t[:300], now, source))
            c.commit()
        finally:
            c.close()
        _log.info("art záměry aktualizovány (%d): %s", len(fresh[:3]),
                  " | ".join(t[:60] for t in fresh[:3]))
        return len(fresh[:3])
    except Exception as e:
        _log.warning("set_intentions: %s", e)
        return 0


def history(db_path: str, limit: int = 20) -> List[tuple]:
    """(text, created, retired) — pro `/malovani` a pro doložení vývoje."""
    try:
        c = _conn(db_path)
        try:
            return c.execute(
                "SELECT text, created_ts, retired_ts FROM art_intentions "
                "ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
        finally:
            c.close()
    except Exception:
        return []
