"""regrese_helpers.py — tenké obálky pro regresní sadu.

Některé kontroly potřebují METODU instance (např. `AgentRouter._hint_match`),
ale sada volá funkce podle jména „modul.funkce". Aby v datovém souboru
nemusela být logika, bydlí ta obálka tady — a NIC nepočítá sama, jen
zavolá skutečný kód, aby se testovalo to, co běží v provozu.
"""
from __future__ import annotations

import json
from functools import lru_cache


@lru_cache(maxsize=1)
def _router():
    from scripts.hans_agent import AgentRouter
    with open("config.json", encoding="utf-8") as f:
        return AgentRouter(json.load(f))


def hint_match(text: str) -> bool:
    """Trefila věta některou agentní nápovědu? (brána PŘED LLM routerem)"""
    return bool(_router()._hint_match(text))


def pravidlo(aid: str, text: str, args: dict | None = None):
    """HANS_AGENT_RULES_TABLE_V1 — co udělá tabulka pravidel se zvolenou akcí?

    Vrací nové id akce, nebo None (= potlačeno). Volá se PŘÍMO
    `_uplatni_pravidla`, tedy bez routeru i bez provádění akce — test tak
    tvrdí ROZHODNUTÍ, ne text odpovědi.
    """
    dec = {"action": aid, "args": args or {}, "confidence": 0.95}
    r = _router()
    r._is_small_talk = lambda m: False   # LLM klasifikátor mimo hru
    return r._uplatni_pravidla(aid, text, dec, None)


def poradi_bloku(varianta: str) -> str:
    """HANS_PROMPT_BLOCKS_TABLE_V1 — v jakém pořadí se skládá system prompt?
    Vrací názvy bloků oddělené '|' (hodnota bloku = jeho jméno)."""
    from scripts.openwebui_direct_handler import _PROMPT_BLOKY, slozit_prompt
    return slozit_prompt({n: n + "|" for n, _ in _PROMPT_BLOKY}, varianta)


def posledni_blok(varianta: str) -> str:
    """Poslední blok varianty — u plného promptu MUSÍ být adresát (`current`)."""
    return poradi_bloku(varianta).rstrip("|").split("|")[-1]
