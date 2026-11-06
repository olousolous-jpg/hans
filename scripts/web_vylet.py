"""HANS_VYLET_WEB_V1 (6. 11. 2026) — web: poslední víkendový návrh výletu (místa,
počasí, všechny akce v okolí o víkendu). Uživatel: když je akcí hodně, na Matrix jde
jen ukázka a celý přehled patří sem. Jen čtení `data/vylet_posledni.json`, který píše
`hans_vylet.tick` po odeslání návrhu.
"""
from __future__ import annotations

import json

from fastapi import APIRouter

router = APIRouter()
_SOUBOR = "data/vylet_posledni.json"


@router.get("/api/vylet")
def api_vylet():
    try:
        return json.load(open(_SOUBOR, encoding="utf-8"))
    except Exception:
        return {}
