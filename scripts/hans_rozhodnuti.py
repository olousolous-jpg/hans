# -*- coding: utf-8 -*-
"""ROZHODNUTI_VSTUP_V1 (11. 10.) — jeden vstup pro otázku „kdo tuhle větu
vezme?“, bez provedení čehokoli.

Kroky před modelem v `send_chat_message` mají volbu oddělenou od provedení:
krok `_sc_x` se nejdřív zeptá podmetody `_sc_x_volba(ctx)` a teprve podle ní
něco udělá. `rozhodni()` volá tytéž podmetody ve stejném pořadí, jaké má
`send_chat_message` (pořadí se čte z jejího zdrojáku, žádný druhý seznam),
a vrátí první krok, který se k větě přihlásí.

Nic neprovádí a nic neukládá: nespustí obsluhu příkazu, nezapíše do historie
ani do deníku. Smí se ale zeptat routeru příkazů s modelem (stejně jako krok
příkazů; jeho odpověď zůstane ve vyrovnávací paměti routeru).

Kroky, které volbu oddělenou ještě nemají, `rozhodni()` nehádá — skončí
u prvního z nich a vrátí ho v poli `nedosel`.
"""
from __future__ import annotations

import inspect
import re
import types

_VOLANI = re.compile(r"^\s*(_r = )?self\.(_sc_\w+)\(ctx\)\s*$")
_poradi_cache: dict = {}

# co z návratu volby znamená „beru“ (výchozí: cokoli pravdivého)
_BERE = {
    "_sc_prikazy": lambda v: bool(v and v[0]),
    "_sc_znalosti": lambda v: bool(v and v[0]),
}


def poradi(handler) -> list:
    """[(krok, smí_ukončit)] v pořadí, v jakém je volá `send_chat_message`."""
    f = type(handler).send_chat_message
    kod = f.__code__
    if kod not in _poradi_cache:
        ven = []
        for radek in inspect.getsource(f).splitlines():
            m = _VOLANI.match(radek)
            if m:
                ven.append((m.group(2), bool(m.group(1))))
        _poradi_cache[kod] = ven
    return _poradi_cache[kod]


def rozhodni(handler, name: str, veta: str, channel: str = None) -> dict:
    """Kdo větu vezme, kdyby teď přišla. Vrací slovník:
      krok     — jméno kroku, který se přihlásil, nebo None
      prikaz   — [jméno, argument] u kroku příkazů, jinak None
      nedosel  — první krok bez oddělené volby (dál rozhoduje starý kód),
                 nebo None, když prošly všechny
      prosel   — kroky, které se nepřihlásily
      chyba    — {krok: výjimka} u voleb, které spadly (krok se pak chová
                 jako nepřihlášený, stejně jako v `send_chat_message`)
    """
    ctx = types.SimpleNamespace(name=name, user_message=veta, on_sentence=None,
                                channel=channel)
    ctx.stripped = (veta or "").strip()
    ven = {"krok": None, "prikaz": None, "nedosel": None, "prosel": [],
           "chyba": {}}
    for krok, konci in poradi(handler):
        if not konci:
            continue            # krok bez návratu o větě nerozhoduje
        volba = getattr(handler, krok + "_volba", None)
        if volba is None:
            ven["nedosel"] = krok
            return ven
        try:
            v = volba(ctx)
        except Exception as e:
            ven["chyba"][krok] = "%s: %s" % (type(e).__name__, e)
            v = None
        if _BERE.get(krok, bool)(v):
            ven["krok"] = krok
            if krok == "_sc_prikazy":
                ven["prikaz"] = [str(v[0][0]), str(v[0][1] or "")]
            return ven
        ven["prosel"].append(krok)
    return ven
