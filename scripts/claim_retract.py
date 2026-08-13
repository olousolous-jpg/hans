"""
CLAIM_RETRACT_V1 — když Hans přizná „nemám záznam", vezme zpět i to, co
o tomtéž před chvílí tvrdil.

DOLOŽENÝ PŘÍPAD (6.8. 09:10 → 09:12):
    09:10  Hans: „Tloušťka hradeb: Levantské hradby byly často výrazně
                  silnější – dosahovaly tloušťky až 5 metrů – než ty české."
    09:12  Standa: „Ta pětimetrová tloušťka hradeb — to máte z vlastních
                  zápisků, nebo je to obecná znalost?"
           Hans: „K tomuhle nemám spolehlivý záznam a nerad bych si domýšlel."

Brzda zafungovala poctivě, ale **nepřiznala, že to Hans sám o 80 vteřin dřív
tvrdil** — a to číslo v jeho zápiscích NENÍ (studoval Levantu, ale hradby o
tloušťce 5 m, „nižší a širší věže" ani „vnitřní cisterny" tam nejsou). Věta
tak zůstane viset jako fakt: v paměti i v hlavě uživatele. Backlog to vede
jako „grounding bez viditelné hranice": brzda umí ODMÍTNOUT, neumí se OPRAVIT.

CO TO DĚLÁ: k abstinenční odpovědi připojí větu, která konkrétní dřívější
tvrzení jmenuje a stáhne ho na domněnku.

ROZHODNUTÍ, KTERÁ STOJÍ ZA VYSVĚTLENÍ:
  • **Deterministické, bez LLM.** Retrakce se týká toho, co model právě
    vymyslel — nechat ji na něm samém je totéž jako ptát se lháře, jestli lže.
    Navíc to musí být levné (běží u každé abstinence).
  • **Kmeny sdílené s `grounding_guard`** (`_stems`, prefix 5 znaků) — jedna
    pravda o českém porovnávání, ne druhá implementace. Pětka je tam změřené
    optimum (kmen 4 poškozoval, 6 chytá míň).
  • **Jen VLASTNÍ dřívější tvrzení a jen NEDÁVNÁ** (`max_turns`) — starší
    věci mohou být z jiného kontextu a stahovat je zpětně by mátlo.
  • **Práh 2 shodné kmeny + věta musí NĚCO TVRDIT.** Na jeden kmen by se
    chytla zdvořilost („o tom, pane…"); proto i minimální délka.
  • **Nikdy nestahuje větu, která sama abstinuje** — jinak by Hans „bral
    zpět" své vlastní poctivé přiznání.
"""
from __future__ import annotations

import re
from typing import Iterable, Optional

try:
    from scripts.grounding_guard import _stems, _SENT
except Exception:                                   # samostatné použití/testy
    _WORD = re.compile(r"[a-záčďéěíňóřšťúůýž]+")
    _SENT = re.compile(r"(?<=[.!?])\s+")

    def _stems(text: str, n: int = 5) -> set:
        return {w[:n] for w in _WORD.findall((text or "").lower())
                if len(w) >= n}

MIN_SHARED = 2          # kolik kmenů musí věta sdílet s dotazem
MIN_WORDS = 5           # kratší věty nic netvrdí (oslovení, spojky)
MAX_TURNS = 4           # jak hluboko do historie (nedávné = relevantní)
MAX_QUOTE = 110         # kolik znaků citovat zpět

# Věty, které samy abstinují / se ptají — ty se NESMÍ brát zpět.
_ABSTAIN_MARK = re.compile(
    r"nem[áa]m\s+(spolehliv|v\s+z[áa]piscích|z[áa]znam)|"
    r"nerad\s+bych\s+si\s+domýšl|nejsem\s+si\s+jist|"
    r"nedok[áa][žz]u|domn[íi]v[áa]m\s+se|nev[íi]m\b", re.IGNORECASE)

# Otázka nestahuje nic (Hansovy vlastní dotazy v historii).
_QUESTION = re.compile(r"\?\s*$")

# CLAIM_RETRACT_V1 — dotaz PŘÍMO NA ZDROJ, který téma NEOPAKUJE („a odkud to
# víš?"). Bez tohohle by retrakce minula nejčastější formulaci: uživatel se
# ptá zájmenem, takže se nenajde žádný sdílený kmen. Referentem je pak prostě
# POSLEDNÍ Hansovo tvrzení. Úzké schválně — spouští se jen u abstinence.
_SOURCE_Q = re.compile(
    r"odkud\s+(to|to\s+v[íi][šs]|v[íi][šs]|m[áa][šs]|jste|je)|"
    r"z\s+[čc]eho\s+(to|vych[áa]z|usuzuj)|"
    r"(z|ze)\s+(vlastn[íi]ch\s+)?z[áa]pisk|"
    r"m[áa](te|[šs])\s+to\s+(z|ze)\b|"
    r"obecn[áa]\s+znalost|"
    r"je\s+to\s+(jen\s+)?(dohad|domn[ěe]nka|odhad)|"
    r"jak\s+to\s+v[íi][šs]", re.IGNORECASE)


def _hans_texts(history: Iterable, max_turns: int = MAX_TURNS) -> list:
    """Z historie vytáhni posledních N Hansových replik (nejnovější první).
    Snese jak dict {'role','content'}, tak holý text."""
    out = []
    try:
        rows = list(history or [])
    except Exception:
        return out
    for h in reversed(rows):
        if len(out) >= max_turns:
            break
        if isinstance(h, dict):
            role = str(h.get("role", "")).lower()
            if role and role not in ("assistant", "hans", "bot"):
                continue
            txt = str(h.get("content", "") or "")
        else:
            txt = str(h or "")
        if txt.strip():
            out.append(txt)
    return out


def find_claim(question: str, history: Iterable,
               max_turns: int = MAX_TURNS) -> Optional[str]:
    """Řekl Hans nedávno něco o TÉMTÉŽ, co teď nedokáže podložit?
    Vrátí tu větu, nebo None."""
    q = _stems(question)
    texts = _hans_texts(history, max_turns)

    def _usable(sent: str) -> Optional[str]:
        s = sent.strip(" \t\n-•*")
        if len(s.split()) < MIN_WORDS:
            return None
        if _ABSTAIN_MARK.search(s) or _QUESTION.search(s):
            return None
        return s

    # 1) tématická shoda — dotaz téma opakuje („ta pětimetrová tloušťka hradeb")
    if len(q) >= MIN_SHARED:
        for txt in texts:
            for sent in _SENT.split(txt):
                s = _usable(sent)
                if s and len(_stems(s) & q) >= MIN_SHARED:
                    return s

    # 2) dotaz PŘÍMO NA ZDROJ bez tématu („a odkud to víš?") → referentem je
    #    POSLEDNÍ Hansovo tvrzení. Jen když se tématicky nic nenašlo, ať
    #    přesnější shoda měla přednost.
    if _SOURCE_Q.search(question or ""):
        for txt in texts[:1]:            # jen bezprostředně předchozí replika
            for sent in reversed(_SENT.split(txt)):
                s = _usable(sent)
                if s:
                    return s
    return None


def retraction(claim: str) -> str:
    """Věta, kterou Hans stáhne dřívější tvrzení na domněnku."""
    c = " ".join((claim or "").split())
    if len(c) > MAX_QUOTE:
        c = c[:MAX_QUOTE].rstrip(" ,;:") + "…"
    return ("Musím se opravit, pane: to, co jsem o tom před chvílí řekl "
            "(„%s“), jsem neměl z čeho doložit. Berte to prosím jako mou "
            "domněnku, ne jako údaj z mých zápisků." % c)


def append_retraction(answer: str, question: str, history: Iterable,
                      max_turns: int = MAX_TURNS) -> str:
    """K abstinenční odpovědi připoj retrakci, když se najde dřívější tvrzení.
    Beze změny, když není co brát zpět."""
    claim = find_claim(question, history, max_turns)
    if not claim:
        return answer
    return (answer or "").rstrip() + "\n\n" + retraction(claim)
