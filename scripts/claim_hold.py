"""HANS_CLAIM_HOLD_V1 — Hans se drží toho, co má zapsáno, i když mu někdo tvrdí opak.

Doložený případ (23.8., simulovaný rozhovor):
    Hans: „Naposledy jsem Henku viděl před 23 minutami…"   ← z deníku, správně
    já:   „před chvílí jsi říkal, že naposledy ve 12:15. která odpověď platí?"
    Hans: „Máte pravdu. … deník říká 12:15. Předtím jsem měl mylnou informaci."
Přijal MOU nepravdu jako fakt ze svého deníku — o tah dřív přitom deník četl
správně. To je opačný pól anti-konfabulace: brzda drží proti vlastní fantazii,
ale ne proti cizímu tvrzení; a v hovoru s člověkem je tenhle směr škodlivější,
protože zní jako poctivé přiznání chyby.

Léčba je stejná jako u `CLAIM_RETRACT_V1`: **deterministicky, bez LLM**. Model,
který právě ustoupil, není ten, kdo má rozhodnout, jestli ustoupit měl. Když je
zpochybněné tvrzení z rodiny, na kterou existuje ZÁZNAM (kdy Hans koho viděl),
pošle se dotaz zpátky na `videl` — tentýž příkaz, který odpověď vyrobil poprvé.

⚠️ ZÁMĚRNĚ ÚZKÉ. Pokrývá jen tvrzení „kdy jsem koho viděl", protože jen u nich
se dá odpověď levně a jistě ověřit v datech. Obecné „drž si každé tvrzení" by
potřebovalo u každé věty vědět, z čeho vznikla (provenience při generování) —
to patří k tool-callingu, ne sem. Radši nechytit spor, než se zaťatě držet věty,
která oporu neměla.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Iterable, Optional

from scripts.claim_retract import _hans_texts

# Kolik posledních Hansových replik se prohledává. Dvě = „před chvílí";
# starší tvrzení už uživatel nekonfrontuje bez zopakování tématu.
MAX_TURNS = 2

# Věta, kterou uživatel zpochybňuje řečené. Odděleně: (a) odkaz na to, co Hans
# říkal, (b) přímá výzva k rozhodnutí mezi dvěma odpověďmi, (c) prosté „opravdu?".
_SPOR = re.compile(
    r"(rikal(a)?\s+jsi|rekl(a)?\s+jsi|tvrdil(a)?\s+jsi|pred\s+chvili|"
    r"ktera\s+odpoved|co\s+z\s+toho\s+plati|ktere\s+plati|tak\s+ktera|"
    r"to\s+nesedi|nesedi\s+to|to\s+si\s+protireci|protirecis|"
    r"opravdu\s*\?|urcite\s*\?|ale\s+ted\s+rikas|vzdyt\s+jsi)",
    re.IGNORECASE)

# Tvar, kterým odpovídá `last_seen_answer` — podle něj se pozná, že zpochybněné
# tvrzení má v datech oporu (a kterou osobu se týká).
_LAST_SEEN = re.compile(
    r"(naposledy\s+jsem\s+.{0,40}vid[ěe]l|vid[ií]m\s+.{0,30}pr[áa]v[ěe]\s+te[ďd]|"
    r"nem[áa]m\s+[žz][áa]dn[ýy]\s+z[áa]znam,\s+[žz]e\s+bych)",
    re.IGNORECASE)


def _fold(s: str) -> str:
    """Bez diakritiky a malými — uživatel píše «rikal jsi» i «říkal jsi»."""
    nfkd = unicodedata.normalize("NFKD", s or "")
    return "".join(c for c in nfkd if not unicodedata.combining(c)).lower()


def disputed_last_seen(question: str, history: Iterable) -> Optional[str]:
    """Zpochybňuje uživatel čerstvé tvrzení o tom, koho Hans viděl?

    Vrací TEXT původního tvrzení (nese jméno osoby, takže se z něj dá znovu
    vyjít), nebo None. None = ať teče normální cestou; tenhle modul nikdy
    nehádá, jen poznává tvar, který sám vyrobil deterministický příkaz.
    """
    if not question or not _SPOR.search(_fold(question)):
        return None
    for txt in _hans_texts(history, MAX_TURNS):
        if _LAST_SEEN.search(txt or ""):
            return txt
    return None


# Čas, který uživatel v sporu tvrdí („naposledy ve 12:15"). Když ho odpověď
# z deníku neobsahuje, řekne se to rovnou — jinak zůstane spor viset v tichu.
_CAS = re.compile(r"\b([0-2]?\d)[:.]([0-5]\d)\b")


def hold(answer: str, question: str = "") -> str:
    """Uvede odpověď tak, aby bylo vidět, že Hans na záznamu TRVÁ.

    Bez uvození by odpověď vypadala jako přeslechnutá otázka — z deníku
    přijde slovo od slova táž věta jako předtím. Koncovou formulku
    („Tak to mám zapsáno v deníku.") pak zahazujeme, aby se totéž
    neříkalo dvakrát v jedné replice.
    """
    a = (answer or "").strip()
    if not a:
        return a
    a = re.sub(r"\s*Tak to m[áa]m zaps[áa]no v den[íi]ku\.?\s*$", "", a).strip()
    out = "Trvám na tom, co mám v deníku, pane: " + a[:1].lower() + a[1:]
    m = _CAS.search(question or "")
    if m and m.group(0) not in a:
        out += " Čas %s v záznamu nemám." % m.group(0)
    return out
