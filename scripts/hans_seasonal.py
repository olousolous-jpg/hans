"""HANS_SEASONAL_AVATAR_V1 (14. 9.) — sezónní podoba Hanse.

Přání uživatele: o Halloweenu (a tři dny před a po něm) má mít Hans podobu
Michaela Myerse — na displeji, ve webu i v dialogu s návrhem filmu na Kodi.

JEDNA funkce pro všechna místa, která Hansovu tvář zobrazují, aby se
nerozešla (displej by ukazoval Myerse a Kodi dál badatele). Malování
(`hans_art` — autoportrét, sny) ji ZÁMĚRNĚ nepoužívá: tam tvář slouží jako
předloha podoby a obrazy mají zůstat Hansovy.

Obrázek leží v `data/avatar/seasonal/` (gitignorováno — cizí autorské dílo
do veřejného repozitáře nepatří). Když chybí, vrací se None a všude platí
běžná podoba, nic se nerozbije.

Výchozí hodnoty jsou tady; config je smí přepsat klíčem
`seasonal_avatar.<svátek>` (enabled, month, day, days_before, days_after, image).
"""
from __future__ import annotations

import os
from datetime import date, datetime, timedelta
from typing import Optional

_VYCHOZI = {
    "halloween": {
        "enabled": True,
        "month": 10, "day": 31,
        "days_before": 3, "days_after": 3,
        "image": "data/avatar/seasonal/halloween.png",
    },
}


def seasonal_avatar(config: Optional[dict] = None,
                    now: Optional[datetime] = None) -> Optional[str]:
    """Cesta k sezónní podobě, když je právě její okno a soubor existuje.
    Jinak None (= běžná podoba). Nikdy nehází."""
    try:
        now = now or datetime.now()
        prepis = ((config or {}).get("seasonal_avatar") or {})
        for nazev, vych in _VYCHOZI.items():
            c = dict(vych)
            c.update(prepis.get(nazev) or {})
            if not c.get("enabled", True):
                continue
            stred = date(now.year, int(c["month"]), int(c["day"]))
            od = stred - timedelta(days=int(c["days_before"]))
            do = stred + timedelta(days=int(c["days_after"]))
            if od <= now.date() <= do:
                img = c.get("image") or ""
                if img and os.path.exists(img):
                    return img
    except Exception:
        return None
    return None
