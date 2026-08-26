"""
GROUNDING_GUARD_V1 — kontrola, že Hans nepřidal fakta, která nedostal.

DOLOŽENÝ PŘÍPAD (12.8.2026, vrak u Sicílie). Uložený zdroj měl 293 znaků.
  • 1. dotaz „můžeš o tom zjistit víc?"  → odpověď PŘESNĚ podle zdroje ✅
  • 2. dotaz „to mě zajímá, zjisti více" → už nebylo z čeho, a Hans vyrobil
    „amfory s olejem nebo vínem", „obchodní cesta přes Středomoří", „vrak je
    poměrně dobře zachovalý" — nic z toho ve zdroji není. A podal to jako
    „Zprávy také uvádějí…", tedy s falešným připsáním zdroji.

⚠️ PROČ TO NEŘEŠÍ PROMPT: instrukce `ANTIKONFAB` v `openwebui_direct_handler`
**výslovně zakazuje** obrat „záznamy uvádějí" — a model ho přesto použil.
Brzda drží na prvním dotazu a povolí pod tlakem opakované žádosti. Přidat
další větu do promptu je přesně ta cesta, kterou projekt zavrhl
([[prompt-debt-tool-calling]]) → kontrola musí být AŽ PO generování.

JAK SE POZNÁ VYMYŠLENÁ VĚTA (změřeno na obou reálných odpovědích):
    dobrá odpověď   57–100 % kmenů kryto zdrojem
    vymyšlená        8–33 %
    zdvořilost        0 %   ← těch se to týkat nesmí
Proto se rozhoduje ve třech pásmech, ne prahem:
    překryv 0 %      → věta NENÍ o tématu (zdvořilost, meta) → nechat
    0 < překryv < práh → věta O TÉMATU mluví, ale zdroj ji nekryje → VYMYŠLENÁ
    překryv ≥ práh   → podloženo → nechat
Nulový překryv je tedy signál neviny, ne viny — vymýšlení se pozná právě tím,
že se tématu drží, jen si k němu přidává.

Čeština: porovnávají se PREFIXY pevné délky (4 znaky), ne celá slova —
„amfor|ami" a „amfor|y" se jinak nepotkají. Týž trik jako v HANS_KNOWLEDGE_FTS_V1.
DÉLKA 5 JE ZMĚŘENÁ na obou reálných odpovědích (dobrá i vymyšlená):
    kmen 4 → dobré odpovědi POŠKOZENY (zahodí i podložené věty) ⛔
    kmen 5 → dobré netknuté, z vymyšlené zahozeno 5 vět        ✅
    kmen 6 → dobré netknuté, ale zachytí jen 3 věty (slabší)
⛔ NEZKOUŠET ZNOVU kmen 4 — vypadal líp jen kvůli chybnému testu (délka byla
navázaná už při definici funkce, takže `check()` jelo pořád na pětce).
ZNÁMÁ HRANICE kmene 5: slovo „vrak" má 4 písmena, takže věta, kde je jediným
tématem, vyjde jako netematická a projde. V doloženém případě to nevadilo —
zachytilo ji pravidlo „nezůstala ani jedna podložená věta" — ale je to díra.

⚠️ Guard NEZLEPŠUJE odpovědi, jen ubírá. Když zdroj mlčí, správná odpověď je
přiznat to — ne to říct hezčími slovy.
"""
from __future__ import annotations

import re

STEM = 5
MIN_SUPPORT = 0.45
MIN_STEMS = 3          # kratší věty se neposuzují (zdvořilost, spojky)

_WORD = re.compile(r"[a-záčďéěíňóřšťúůýž]+")
# GROUNDING_GUARD_SENT_SPLIT_V2 (26.8.) — dělilo se za KAŽDOU tečkou, takže
# „pevnost ze 14. století" spadlo na dvě „věty". Guard pak zahodil přední půlku
# jako bez opory a v odpovědi zůstal osiřelý zlomek: doloženo živě —
# „Hrad Kost jsem studoval nedávno. století a postupně přestavovanou…".
# Rozlišovač: NOVÁ VĚTA ZAČÍNÁ VELKÝM PÍSMENEM; pořadové číslo („14. století")
# i zkratka s číslem („r. 1300") pokračují malým písmenem nebo číslicí.
# Ověřeno 6/6 včetně „Rok 1990. Pak…", kde se dělit MÁ.
_SENT = re.compile(r"(?<=[.!?])\s+(?![a-záčďéěíňóřšťúůýž0-9])")

# Věta, která nic netvrdí o tématu — jen se sloví. Kdyby prošla přes pásma,
# nevadí; tohle je jen zrychlení a ochrana proti krátkým zdvořilostem.
ABSTAIN = "Víc než tohle už o tom nemám, pane, a nerad bych si domýšlel."


def _stems(text: str, n: int = 0) -> set:
    n = n or STEM
    return {w[:n] for w in _WORD.findall((text or "").lower()) if len(w) >= n}


def check(answer: str, grounding: str,
          min_support: float = MIN_SUPPORT) -> tuple[str, list]:
    """Vrátí (odpověď_bez_vymyšleného, [zahozené věty]).

    `grounding` = text, který model dostal jako podklad. Porovnává se proti
    NĚMU, ne proti pravdě — otázka zní „zůstal u toho, co dostal?".
    """
    if not answer or not grounding:
        return answer, []
    G = _stems(grounding)
    if len(G) < 5:                     # podklad příliš chudý na soud
        return answer, []

    kept, dropped, topical_ok = [], [], 0
    for sent in _SENT.split(answer.strip()):
        s = sent.strip()
        if not s:
            continue
        S = _stems(s)
        if len(S) < MIN_STEMS:
            kept.append(s)
            continue
        overlap = len(S & G) / len(S)
        if overlap == 0.0:             # není o tématu → není co ověřovat
            kept.append(s)
        elif overlap < min_support:    # o tématu, ale zdroj to nekryje
            dropped.append(s)
        else:
            kept.append(s)
            topical_ok += 1

    if not dropped:
        return answer, []
    if topical_ok == 0:
        # Ze zdroje nezůstalo nic — zbytek by byl jen omáčka kolem výmyslu.
        return ABSTAIN, dropped
    return " ".join(kept).strip() + " " + ABSTAIN, dropped
