"""hans_prava.py — HANS_PRAVA_V1 (27. 9.) — co která známá osoba smí.

Do 27. 9. rozlišoval Hans jen „známý × cizí“: všichni známí viděli totéž,
včetně Hansových postřehů o sobě navzájem, nitek a zájmů ostatních.
Pokyn uživatele: „aby se známá osoba nedostala k soukromým chatům druhé
známé osoby a podobně“, nastavitelné zaškrtávátky ve web adminu.

Kde co leží:
  config.json          pristup.enabled, pristup.role_defaults (bez jmen)
  config.private.json  known_persons.<klíč>.pristup = {"role": …, "povoleno": {…}}
                       (sekce known_persons je celá privátní, jména do gitu nejdou)

Pravidla platí jen pro data O JINÉ osobě — na vlastní data se nevztahují.
Cizí (neznámé) osoby mají svá pravidla jinde (HANS_HOUSEHOLD_PRIVACY_V1,
HANS_STRANGER_NO_MUTATE_V1) a tady nedostanou nic.

⚠️ Identita je spolehlivá jen na Matrixu (účet → osoba). Web admin používá
jen správce, hlas zatím mluví za `voice.default_speaker`. Rozpoznání mluvčího
podle tváře je další krok (data/NAPADY.md).
"""
from __future__ import annotations

import logging
from typing import Optional

_log = logging.getLogger("hans_prava")

#: (klíč, popisek pro admin) — pořadí = pořadí sloupců v tabulce.
KATEGORIE = (
    ("cizi_rozhovory", "Rozhovory jiných osob s Hansem"),
    ("karta_osoby", "Hansovy postřehy o jiných osobách (jinak jen jméno a vztah)"),
    ("nitky_zajmy", "Nitky a zájmy jiných osob"),
    ("akce", "Akce s následky (Kodi, PC, hlídání, stahování, poznámky…)"),
)
ROLE = ("spravce", "clen", "host")

_VYCHOZI = {
    "spravce": {k: True for k, _ in KATEGORIE},
    "clen": {"cizi_rozhovory": False, "karta_osoby": False,
             "nitky_zajmy": False, "akce": True},
    "host": {k: False for k, _ in KATEGORIE},
}


def _sekce(config: dict) -> dict:
    return (config or {}).get("pristup", {}) or {}


def zapnuto(config: dict) -> bool:
    return bool(_sekce(config).get("enabled", False))


def _klic(kdo: str, config: dict) -> str:
    """Konfigurační klíč osoby (malým), nebo ''."""
    k = (kdo or "").strip().lower()
    kp = (config or {}).get("known_persons", {}) or {}
    if k in kp:
        return k
    try:
        from scripts.cz_names import find_known_person
        return find_known_person(k, config) if k else ""
    except Exception:
        return ""


def role(kdo: str, config: dict) -> str:
    """Role osoby. Bez nastavení: majitel Matrixu = správce, ostatní člen."""
    k = _klic(kdo, config)
    if not k:
        return ""
    rec = ((config.get("known_persons", {}) or {}).get(k) or {})
    r = str(((rec.get("pristup") or {}).get("role")) or "").strip().lower()
    if r in ROLE:
        return r
    majitel = str(((config.get("matrix", {}) or {}).get("as_person")) or "").lower()
    return "spravce" if majitel and k == majitel else "clen"


def pravidla(kdo: str, config: dict) -> dict:
    """Výsledná pravidla osoby: výchozí podle role + výjimky osoby."""
    r = role(kdo, config)
    if not r:
        return {k: False for k, _ in KATEGORIE}
    zaklad = dict(_VYCHOZI[r])
    zaklad.update({k: bool(v) for k, v in
                   ((_sekce(config).get("role_defaults", {}) or {}).get(r) or {}).items()
                   if k in zaklad})
    if r == "spravce":            # správce si sám nic nezakáže (zamčení z adminu)
        return {k: True for k in zaklad}
    k = _klic(kdo, config)
    vlastni = ((((config.get("known_persons", {}) or {}).get(k) or {})
                .get("pristup") or {}).get("povoleno") or {})
    zaklad.update({x: bool(v) for x, v in vlastni.items() if x in zaklad})
    return zaklad


def muze(config: dict, kdo: str, kategorie: str,
         o_kom: Optional[str] = None) -> bool:
    """Smí `kdo` do `kategorie` (o osobě `o_kom`)? Vypnuté = jako dřív (ano)."""
    if not zapnuto(config):
        return True
    k = _klic(kdo, config)
    if not k:
        return False
    if o_kom is not None and _klic(o_kom, config) == k:
        return True                 # vlastní data
    ok = bool(pravidla(k, config).get(kategorie, False))
    if not ok:
        _log.info("HANS_PRAVA_V1: %s → %s%s zakázáno", k, kategorie,
                  (" (o %s)" % _klic(o_kom, config)) if o_kom else "")
    return ok


ODMITNUTI = ("To patří jiné osobě a na to se mne, prosím, zeptejte u ní. "
             "Snad mi to prominete.")


def prehled(config: dict) -> dict:
    """Pro web admin: kategorie, role a stav každé známé osoby."""
    kp = (config or {}).get("known_persons", {}) or {}
    osoby = []
    for k, rec in kp.items():
        rec = rec or {}
        osoby.append({
            "klic": k,
            "jmeno": str(rec.get("nom") or k),
            "role": role(k, config),
            "povoleno": pravidla(k, config),
            "vlastni": dict(((rec.get("pristup") or {}).get("povoleno") or {})),
        })
    return {"enabled": zapnuto(config),
            "kategorie": [{"klic": k, "popis": p} for k, p in KATEGORIE],
            "role": list(ROLE), "vychozi": _VYCHOZI, "osoby": osoby}
