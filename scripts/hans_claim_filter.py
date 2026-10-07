"""HANS_CLAIM_FILTER_V1 (7. 10.) — odpověď BEZ FAKT se drží toho, co model říká stabilně.

Na cestě `factual_nofacts` (v zápiscích nic) rozhoduje A1: pět krátkých vzorků
odpovědi, a když si jsou podobné, otázka se pustí k běžné odpovědi. Jenže ta
vzniká ZNOVU, s osobností a bez omezení délky, a rozchází se s nimi:
měřeno 7. 10. na 8 otázkách — vzorky 5× „Jindřich II.“, odpověď „král Francie
Filip II. August“; vzorky 1378, odpověď „smrti v roce 1379“; a v 6 z 8 odpovědí
věta o vlastním zdroji („četl jsem o tom na Wikipedii“, „mám v deníku poznámky“),
ačkoli právě tahle cesta znamená, že žádný záznam není.

Tady se nic negeneruje. Z hotové odpovědi se
  1. vyřadí věty tvrdící vlastní četbu, záznamy nebo podklady,
  2. vyřadí věty, jejichž letopočet nebo vlastní jméno nezazní aspoň ve dvou
     vzorcích A1,
  3. a když tím padne i první věta s tvrzením (jádro odpovědi), vrátí se místo
     toho nejtypičtější vzorek — krátká, stabilní odpověď téhož modelu.
Platí i pravdivé podrobnosti, které se ve vzorcích neopakují (vzorky jsou krátké):
to je cena, o detail víc se dá doptat. ⚠️ Stabilní omyl to nechytí — hlídá se
shoda se vzorky, ne pravda.
"""
from __future__ import annotations

import re

# dělí jen před VELKÝM písmenem nebo uvozovkou — „31. března“ a „Karel IV. vládl“ zůstanou celé
_SENT = re.compile(r"(?<=[.!?])\s+(?=[A-ZÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ„\"*(–-])|\n+")
_NABIDKA = re.compile(r"\b(?:pokud\s+byste|jestli\s+(?:chce|bude)|kdybyste|mohu\s+v[áa]m|m[ůu][žz]u\s+v[áa]m|mohu\s+ti|m[ůu][žz]u\s+ti)\b", re.I)
_ROK = re.compile(r"(?<![\d.,])\d{3,4}(?![\d])")
_JMENO = re.compile(r"\b[A-ZÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ][a-záčďéěíňóřšťúůýž]{3,}\b")
# Věta o VLASTNÍM zdroji / paměti — na cestě bez fakt nemá čím být podložená.
VLASTNI_ZDROJ = re.compile(
    r"\b(?:[čc]etl\w*\s+jsem|jsem\s+(?:si\s+)?(?:ne)?d[áa]vno\s+[čc]etl|"
    r"jsem\s+(?:si\s+)?(?:pro)?[čc]etl|prohl[ée]dl\w*\s+jsem|studoval\w*\s+jsem|"
    r"jsem\s+(?:si\s+)?(?:pro)?studoval|kdy\s+jsem\s+studoval|"
    r"m[áa]m\s+(?:i\s+|tak[ée]\s+|k\s+dispozici\s+(?:i\s+)?|v\s+den[íi]ku\s+|ve\s+sv[ýy]ch\s+)?"
    r"(?:\w+\s+){0,2}?(?:z[áa]znam\w*|pozn[áa]m\w*|z[áa]pis\w*|pl[áa]ny|v[ýy]kres\w*|podklad\w*|spoustu\s+informac[íi])|"
    r"v\s+m[ýy]ch\s+(?:z[áa]znamech|z[áa]pisc[íi]ch)|(?:po)?dle\s+m[ýy]ch\s+(?:z[áa]znam[ůu]|z[áa]pisk[ůu]|pozn[áa]mek)|"
    r"odkazy\s+na\s+[čc]l[áa]nky)\b", re.I)


def _vety(text: str) -> list:
    return [s.strip() for s in _SENT.split(text or "") if s and s.strip()]


def tvrde(veta: str, vynech: set) -> set:
    """Letopočty a kmeny vlastních jmen uvnitř věty (první slovo věty se nebere)."""
    t = set(_ROK.findall(veta))
    telo = veta.split(" ", 1)[1] if " " in veta else ""
    for w in _JMENO.findall(telo):
        k = w[:5].lower()
        if k not in vynech:
            t.add(k)
    return t


def filtruj(odpoved: str, otazka: str, vzorky: list, osloveni: tuple = (),
            min_shoda: int = 2) -> tuple:
    """(nová odpověď, {"zdroj": n, "tvrzeni": n, "nahrazeno": bool}).

    `vzorky` smí být prázdné → vyřadí se jen věty o vlastním zdroji."""
    vety = _vety(odpoved)
    stat = {"zdroj": 0, "tvrzeni": 0, "nahrazeno": False}
    if not vety:
        return odpoved, stat
    low = [(v or "").lower() for v in (vzorky or []) if v and v.strip()]
    vynech = {w[:5].lower() for w in re.findall(r"\w{4,}", otazka or "")}
    vynech |= {str(o)[:5].lower() for o in osloveni if o}
    roky_vzorku = {r for a in low for r in set(_ROK.findall(a))
                   if sum(1 for b in low if r in b) * 2 > len(low)}
    nech, jadro_padlo, prvni_tvrzeni = [], False, True
    po_zdroji = False
    for v in vety:
        if VLASTNI_ZDROJ.search(v):
            stat["zdroj"] += 1
            po_zdroji = True
            continue
        # nabídka navazující na vyřazenou větu („…mohu vám ho sdělit“) by visela ve vzduchu
        if po_zdroji and len(v) < 140 and _NABIDKA.search(v):
            continue
        po_zdroji = False
        tv = tvrde(v, vynech) if len(low) >= 3 else set()
        if tv:
            slabe = [t for t in tv if sum(1 for a in low if t in a) < min_shoda]
            if slabe:
                stat["tvrzeni"] += 1
                # padá první věta s tvrzením, nebo věta, která vedle nepodloženého
                # údaje nese i údaj z většiny vzorků („začala 1887 a skončila 1889“)
                # → vyřazením by zmizela sama odpověď; nahradí ji stabilní vzorek
                silne = any(sum(1 for a in low if t in a) * 2 > len(low) for t in tv)
                # věta uvádí JINÝ letopočet, než na kterém se shodne většina vzorků
                # („vyšla 1837“ × vzorky „1855“) → rozpor v jádru, ne ozdoba
                jiny_rok = bool(roky_vzorku) and any(_ROK.fullmatch(t) for t in slabe) \
                    and not (roky_vzorku & tv)
                if prvni_tvrzeni or silne or jiny_rok:
                    jadro_padlo = True
                prvni_tvrzeni = False
                continue
            prvni_tvrzeni = False
        nech.append(v)
    if jadro_padlo and low:
        # nejtypičtější vzorek = sdílí nejvíc tvrdých údajů s ostatními
        def _skore(i):
            ti = tvrde(vzorky[i], vynech)
            return sum(1 for t in ti for j, a in enumerate(low) if j != i and t in a)
        nej = max(range(len(low)), key=_skore)
        stat["nahrazeno"] = True
        # i vybraný vzorek může nést údaj, který ostatní vzorky nemají (položka
        # výčtu „Nový Městský zámek“) → jeho věty a odrážky projdou týmž sítem
        cast = []
        for v in _vety(vzorky[nej]):
            tv = tvrde(v, vynech)
            if any(sum(1 for a in low if t in a) < min_shoda for t in tv):
                continue
            cast.append(v)
        text = ""
        for v in cast:          # odrážky na nový řádek, věty za sebou
            text += ("\n" if re.match(r"[*•\-–]\s", v) else " ") + v
        return (text.strip() or vzorky[nej].strip()), stat
    if not stat["zdroj"] and not stat["tvrzeni"]:
        return odpoved, stat
    return (" ".join(nech).strip() or odpoved), stat


# ── HANS_CLAIM_RESTYLE_V1 (7. 10.) — stabilní vzorek Hansovým hlasem ─────────
# Nápad uživatele: strohou náhradní větu „prohnat modelem“, ať odpoví po svém.
# Jde to, ale jen s úzkým zadáním a KONTROLOU výsledku: při volném zadání model
# přepisoval letopočty slovy a špatně („1378“ → „tisíc tři sta osmdesát osm“,
# „1928“ → „v roce devětadvacátém“) a přidával „dle mých poznámek“ (5 čistých
# z 12). S pokynem opsat čísla číslicemi a s kontrolou níž: 18 z 18, ~3 s.
_RESTYLE_SYS = (
    "Jsi {jmeno}, zdvořilý a přemýšlivý společník. Mluvíš česky a tazateli vykáš.\n"
    "ÚKOL: Dostaneš OTÁZKU a OVĚŘENOU ODPOVĚĎ. Řekni tu odpověď jednou až dvěma větami "
    "vlastními slovy.\n"
    "PRAVIDLA: Letopočty a čísla opiš ČÍSLICEMI přesně tak, jak jsou v ověřené odpovědi. "
    "Jména opiš přesně. Nepřidávej žádný další údaj, hodnocení ani podrobnost. "
    "Neříkej, odkud to víš. Nic nenabízej. Tazatele oslov jednou tvarem, který dostaneš.")
_CISLO_SLOVY = re.compile(r"\b(?:tis[íi]c\w*|stolet\w*|\w*set\b|des[áa]t\w*|\w+n[áa]ct\w*)\b", re.I)


def verne(text: str, zaklad: str, otazka: str, osloveni: tuple = ()) -> bool:
    """Drží se přeformulování základu? Stejné letopočty číslicemi, žádné nové
    jméno, žádná věta o vlastním zdroji, žádné číslo slovy."""
    if not text or not text.strip():
        return False
    if set(_ROK.findall(zaklad)) != set(_ROK.findall(text)):
        return False
    if _CISLO_SLOVY.search(text) and not _CISLO_SLOVY.search(zaklad):
        return False
    _n, st = filtruj(text, otazka, [zaklad] * 3, osloveni)
    return not (st["zdroj"] or st["tvrzeni"])


def prestyluj(config: dict, otazka: str, zaklad: str, osloveni: str = "",
              jmeno: str = "Hans") -> str:
    """Stabilní vzorek Hansovým hlasem, nebo '' (pak platí strohý vzorek).
    Jedno krátké volání modelu; výsledek musí projít `verne`."""
    try:
        from scripts.ollama_client import ollama_generate
        model = str(((config.get("selfconsistency", {}) or {}).get("model"))
                    or (config.get("models", {}) or {}).get("voice")
                    or "hans-czech:latest")
        r = ollama_generate(
            model,
            "OSLOVENÍ: %s\nOTÁZKA: %s\nOVĚŘENÁ ODPOVĚĎ: %s\n\nTvoje odpověď:"
            % (osloveni or "(bez oslovení)", (otazka or "")[:300],
               (zaklad or "").replace("\n", " ")[:900]),
            system=_RESTYLE_SYS.format(jmeno=jmeno or "Hans"), config=config,
            timeout=45, options={"temperature": 0.3, "num_predict": 140})
    except Exception:
        return ""
    r = (r or "").strip()
    return r if verne(r, zaklad, otazka, (osloveni,)) else ""

