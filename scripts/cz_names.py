"""
CZ_NAMES_V1 — vokativ + skloňování sloves podle rodu známé osoby.

Nahrazuje generické "jana přišel/a" → "Přišla Jana" a "Standa" v oslovení
→ "Stando". Zdroj rodu: config.known_persons[name].gender ("muž" | "žena").
Neznámé jméno / chybějící config = fallback = base (žádná změna vs dřívější
chování — nic se nerozbije).

Pragmatické, ne dokonalé: pokrývá jen tvary skutečně používané v Hansově
kódu (přišel/přišla, byl/byla, viděl/viděla, řekl/řekla). Rozšiřuj podle
potřeby — přidej pár do _PAST_M_TO_F.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

_ROOT = Path(__file__).resolve().parent.parent

# ── Vokativ ──────────────────────────────────────────────────────────────

def vocative(name: str, gender: Optional[str] = None) -> str:
    """Vokativ (5. pád) pro oslovení.

    Pravidla (běžná CZ jména končící -a):
      končí -a → -o        (Standa→Stando, Jana→Jano, Honza→Honzo)
      končí -e → beze změny (Marie zůstává Marie)
      jinak    → beze změny (Petr zůstává Petr; palatalizaci si netroufáme)

    Rod se u zakončení -a nerozlišuje — muž i žena berou -o. Rod se zatím
    zadává jen kvůli symetrii s ostatními helpery a pro budoucí rozšíření.
    """
    if not name:
        return name
    base = _title(name)
    if base.lower().endswith("a"):
        return base[:-1] + "o"
    return base


# ── Skloňování minulého času (l-participium) ─────────────────────────────

_PAST_M_TO_F = {
    "přišel":   "přišla",
    "nepřišel": "nepřišla",
    "odešel":   "odešla",
    "neodešel": "neodešla",
    "byl":      "byla",
    "nebyl":    "nebyla",
    "viděl":    "viděla",
    "neviděl":  "neviděla",
    "řekl":     "řekla",
    "šel":      "šla",
    "napsal":   "napsala",
    "spal":     "spala",
}


def past_verb(masc_form: str, gender: Optional[str]) -> str:
    """Vrátí mužský nebo ženský tvar minulého l-participia.

    gender='žena' → ženský tvar (přišla). Jinak → mužský (přišel).
    Neznámé sloveso → vrátí vstup beze změny (nic nerozbít).
    """
    if gender in ("žena", "z", "f", "female"):
        return _PAST_M_TO_F.get(masc_form, masc_form)
    return masc_form


# ── Config lookup (rod z known_persons) ───────────────────────────────────

_config_cache: Optional[dict] = None


def _load_config() -> dict:
    global _config_cache
    if _config_cache is None:
        try:
            _config_cache = json.loads(
                (_ROOT / "config.json").read_text(encoding="utf-8"))
        except Exception:
            _config_cache = {}
    return _config_cache


def person_gender(name: str, config: Optional[dict] = None) -> Optional[str]:
    """Vytáhne rod z config.known_persons (klíč = lower-case jméno).

    Vrací 'muž' / 'žena' / None (neznámý)."""
    if not name:
        return None
    cfg = config if config is not None else _load_config()
    persons = (cfg.get("known_persons") or {})
    rec = persons.get(name.lower())
    if not rec:
        return None
    return rec.get("gender")


def display_name(name: str) -> str:
    """'jana' → 'Jana'. Pro deník/log/hlášky (žádné rozbití diakritiky)."""
    return _title(name) if name else name


def _title(name: str) -> str:
    if len(name) <= 1:
        return name.upper()
    return name[0].upper() + name[1:].lower()


# ── Convenience wrappery (nejčastější použití v Hansově kódu) ────────────

def address(name: str, config: Optional[dict] = None) -> str:
    """Vokativ podle registrace v known_persons — jednořádkový shortcut."""
    return vocative(name, person_gender(name, config))


def came(name: str, config: Optional[dict] = None) -> str:
    """'přišel' / 'přišla' podle rodu Osoby (dle known_persons)."""
    return past_verb("přišel", person_gender(name, config))


def left(name: str, config: Optional[dict] = None) -> str:
    """'odešel' / 'odešla' podle rodu Osoby."""
    return past_verb("odešel", person_gender(name, config))


def was(name: str, config: Optional[dict] = None, negate: bool = False) -> str:
    """'byl' / 'byla' (nebo 'nebyl' / 'nebyla' při negate=True)."""
    return past_verb("nebyl" if negate else "byl", person_gender(name, config))


def saw(name: str, config: Optional[dict] = None, negate: bool = False) -> str:
    """'viděl' / 'viděla' (nebo 'neviděl' / 'neviděla')."""
    return past_verb("neviděl" if negate else "viděl", person_gender(name, config))


# ── Smoke (python3 -m scripts.cz_names) ──────────────────────────────────
if __name__ == "__main__":
    print("=== vocative ===")
    for n, g in [("Standa", "muž"), ("Jana", "žena"), ("Honza", "muž"),
                 ("Marie", "žena"), ("Petr", "muž"), ("", None)]:
        print(f"  {n!r:12} ({g}) → {vocative(n, g)!r}")

    print("\n=== past_verb ===")
    for v in ("přišel", "odešel", "byl", "nebyl", "viděl", "řekl"):
        print(f"  {v!r:10} muž → {past_verb(v, 'muž')!r},  "
              f"žena → {past_verb(v, 'žena')!r}")

    print("\n=== config lookup (jména z lokálního configu, ne napevno) ===")
    _names = list((_load_config().get("known_persons") or {}).keys()) or ["unknown"]
    for n in _names + ["unknown"]:
        g = person_gender(n)
        print(f"  {n!r:10} → gender={g!r},  address={address(n)!r},  "
              f"came={came(n)!r},  was_neg={was(n, negate=True)!r}")
