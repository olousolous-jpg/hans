"""
CZ_NUMBERS_V1 — český převod číslic na slova pro TTS.

Hans občas vygeneruje text s číslicemi („15. srpna", „23–39 °C", „celkem 7",
„22:00"), které syntéza řeči přečte špatně nebo přeskočí. Tento modul je
převede na slova JEN pro mluvený výstup (volá se v tts_speaker._clean) —
v chatu/deníku číslice zůstávají.

Pragmatické, ne dokonalé: kardinálie v nominativu, dny v genitivu (nejčastější
tvar v datech „patnáctého srpna"). Skloňování podle rodu se neřeší — pro TTS
„dost dobré". Bez závislostí.
"""
from __future__ import annotations

import re

_UNITS = ['nula', 'jedna', 'dva', 'tři', 'čtyři', 'pět', 'šest', 'sedm',
          'osm', 'devět']
_TEENS = ['deset', 'jedenáct', 'dvanáct', 'třináct', 'čtrnáct', 'patnáct',
          'šestnáct', 'sedmnáct', 'osmnáct', 'devatenáct']
_TENS = ['', '', 'dvacet', 'třicet', 'čtyřicet', 'padesát', 'šedesát',
         'sedmdesát', 'osmdesát', 'devadesát']
_HUND = ['', 'sto', 'dvě stě', 'tři sta', 'čtyři sta', 'pět set', 'šest set',
         'sedm set', 'osm set', 'devět set']

_DAY_GEN = {
    1: 'prvního', 2: 'druhého', 3: 'třetího', 4: 'čtvrtého', 5: 'pátého',
    6: 'šestého', 7: 'sedmého', 8: 'osmého', 9: 'devátého', 10: 'desátého',
    11: 'jedenáctého', 12: 'dvanáctého', 13: 'třináctého', 14: 'čtrnáctého',
    15: 'patnáctého', 16: 'šestnáctého', 17: 'sedmnáctého', 18: 'osmnáctého',
    19: 'devatenáctého', 20: 'dvacátého', 21: 'dvacátého prvního',
    22: 'dvacátého druhého', 23: 'dvacátého třetího', 24: 'dvacátého čtvrtého',
    25: 'dvacátého pátého', 26: 'dvacátého šestého', 27: 'dvacátého sedmého',
    28: 'dvacátého osmého', 29: 'dvacátého devátého', 30: 'třicátého',
    31: 'třicátého prvního',
}
_MONTH_GEN = {
    1: 'ledna', 2: 'února', 3: 'března', 4: 'dubna', 5: 'května', 6: 'června',
    7: 'července', 8: 'srpna', 9: 'září', 10: 'října', 11: 'listopadu',
    12: 'prosince',
}


def _under1000(n: int) -> str:
    parts = []
    h, r = n // 100, n % 100
    if h:
        parts.append(_HUND[h])
    if r < 10:
        if r:
            parts.append(_UNITS[r])
    elif r < 20:
        parts.append(_TEENS[r - 10])
    else:
        t, u = r // 10, r % 10
        parts.append(_TENS[t])
        if u:
            parts.append(_UNITS[u])
    return ' '.join(parts)


def cardinal(n) -> str:
    """Kardinální číslovka 0–9999 (nominativ)."""
    n = int(n)
    if n == 0:
        return 'nula'
    if n < 0:
        return 'mínus ' + cardinal(-n)
    parts = []
    th, r = n // 1000, n % 1000
    if th:
        if th == 1:
            parts.append('tisíc')
        elif th in (2, 3, 4):
            parts.append(_under1000(th) + ' tisíce')
        else:
            parts.append(_under1000(th) + ' tisíc')
    if r:
        parts.append(_under1000(r))
    return ' '.join(p for p in parts if p)


def normalize(text: str) -> str:
    """Převede číslice v textu na slova (datumy, teploty, časy, počty)."""
    if not text or not any(c.isdigit() for c in text):
        return text

    # 1) Číselné datum DD.MM. nebo DD.MM.YYYY → „den-gen měsíc-gen [rok]"
    def _date(m):
        d, mo = int(m.group(1)), int(m.group(2))
        if 1 <= d <= 31 and 1 <= mo <= 12:
            s = _DAY_GEN[d] + ' ' + _MONTH_GEN[mo]
            if m.group(3):
                s += ' ' + cardinal(int(m.group(3)))
            return s
        return m.group(0)
    text = re.sub(r'\b(\d{1,2})\.\s?(\d{1,2})\.(?:\s?(\d{4}))?', _date, text)

    # 2) „D. <název měsíce>" → „den-gen <měsíc>"
    months = '|'.join(_MONTH_GEN.values())

    def _dmonth(m):
        d = int(m.group(1))
        return (_DAY_GEN[d] + ' ' + m.group(2)) if 1 <= d <= 31 else m.group(0)
    text = re.sub(r'\b(\d{1,2})\.\s+(' + months + r')\b', _dmonth, text,
                  flags=re.IGNORECASE)

    # 3) Teplotní rozsah „23–39 °C" + jednotlivá teplota
    text = re.sub(r'(\d+)\s*[–—-]\s*(\d+)\s*°?\s*[Cc]\b',
                  lambda m: cardinal(m.group(1)) + ' až ' + cardinal(m.group(2))
                  + ' stupňů Celsia', text)
    text = re.sub(r'(\d+)\s*°\s*[Cc]\b',
                  lambda m: cardinal(m.group(1)) + ' stupňů Celsia', text)
    text = re.sub(r'(\d+)\s*°',
                  lambda m: cardinal(m.group(1)) + ' stupňů', text)

    # 4) Čas HH:MM
    def _time(m):
        h, mi = int(m.group(1)), int(m.group(2))
        s = cardinal(h) + ' hodin'
        if mi:
            s += ' ' + cardinal(mi) + ' minut'
        return s
    text = re.sub(r'\b([0-2]?\d):([0-5]\d)\b', _time, text)

    # 5) Procenta
    text = re.sub(r'(\d+)\s*%',
                  lambda m: cardinal(m.group(1)) + ' procent', text)

    # 6) Zbylá samostatná celá čísla → kardinálie. (Osamělou „N." ZÁMĚRNĚ
    # neřešíme jako řadovou — „celkem 7." je počet + tečka věty, ne „sedmého";
    # data „D. měsíc" / „DD.MM." pokrývají kroky 1–2. Tečka zůstane na místě.)
    text = re.sub(r'\b\d+\b', lambda m: cardinal(m.group(0)), text)
    return text
