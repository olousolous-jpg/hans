# -*- coding: utf-8 -*-
"""HANS_FILM_DIRECTOR_CHECK_V1 (21.8.) — hlídá ATRIBUCI filmu, ne chuť.

PROČ: v simulovaném rozhovoru 21.8. Hans na otázku, který film by si rád
pustil znovu, odpověděl „Sedmikrásky od Miloše Formana". Sedmikrásky natočila
Věra Chytilová a o tom filmu nemá Hans ŽÁDNÝ záznam — vymyslel si titul
i režiséra. Uživatel k tomu řekl podstatnou věc: **přát si film, který doma
nemáme, je v pořádku — to je jeho zvídavost. Ale režiséra má mít správně.**

Proto se NEomezuje výběr filmu (žádný seznam „vybírej jen z knihovny"), jen se
kontroluje tvrzení o tom, KDO film natočil:
  1. Kodi knihovna (má-li film doma) — jméno v čistém tvaru.
  2. Wikipedia — spolehlivě rozliší (změřeno: „Forman" v článku o Sedmikráskách
     není, „Chytilová" i „Menzel" u svých filmů ano).
Když se tvrzení nedá ověřit, NEDĚLÁ se nic (falešná abstinence je taky vada).

⚠️ Když je jméno špatně a známe správné jen z Wikipedie, atribuce se ODSTRANÍ
místo nahrazení: článek nese jméno ve skloňovaném tvaru („režisérky Věry
Chytilové") a vlepit ho za „od" by dalo paskvil typu „od Jiřím Menzelem".
Z Kodi jméno přijde v prvním pádě, tam se nahradit dá.
"""
from __future__ import annotations

import logging
import re
import unicodedata

_log = logging.getLogger(__name__)

# „…film *Sedmikrásky* od Miloše Formana", „snímek „Vlaky" od Jiřího Menzela",
# „režie Věra Chytilová", „natočil Miloš Forman"
_ATRIBUCE = re.compile(
    r"(?P<cele>\s*(?:od|re[žz]ie|re[žz][ií]s[ée]r[aky]?|re[žz]is[ée]rem|"
    r"re[žz]is[ée]rky|nato[čc]il[aiy]?)\s+"
    r"(?P<kdo>[A-ZÁ-Ž][\wá-ž]+(?:\s+[A-ZÁ-Ž][\wá-ž]+){1,2}))",
    re.UNICODE)
# titul: nejbližší „…" / *…* / "…" PŘED atribucí
_TITUL = re.compile(r"[„\"*]([^„\"*\n]{2,70})[\"“*]")

# HANS_DIRECTOR_ONLY_FILM_V1 — rozlišení spouštěče a filmového kontextu.
_JEN_OD = re.compile(r"^od\b", re.IGNORECASE)
_FILMOVE_SLOVO = re.compile(
    r"\b(film\w*|sn[íi]m\w*|dokument\w*|seri[áa]l\w*|epizod\w*|d[íi]l\b|"
    r"kinematograf\w*|nato[čc]en\w*|re[žz][ií]\w*|promít\w*|kino\w*)\b",
    re.IGNORECASE)


def _fold(s: str) -> str:
    s = unicodedata.normalize("NFKD", (s or "").lower())
    return "".join(c for c in s if not unicodedata.combining(c))


def _prijmeni(jmeno: str) -> str:
    """Poslední slovo jména (příjmení), bez diakritiky."""
    casti = [c for c in _fold(jmeno).split() if c]
    return casti[-1] if casti else ""


def _sedi(a: str, b: str) -> bool:
    """Shoda příjmení navzdory skloňování — TÁŽ funkce jako u entit."""
    if not a or not b:
        return False
    if a == b:
        return True
    try:
        from scripts.hans_entities import _tok_match
        return bool(_tok_match(a, b))
    except Exception:
        return a[:5] == b[:5]


def _z_kodi(kodi, titul: str):
    """Režie z knihovny (první pád) nebo None. Nikdy nevyhodí výjimku."""
    if not kodi or not titul:
        return None
    try:
        m = kodi.find_movie(titul)
        if m and hasattr(kodi, "movie_details"):
            d = kodi.movie_details(m.get("movieid")) or {}
            rez = d.get("director") or []
            if rez:
                return [str(x) for x in rez]
        ep = kodi.find_episode(titul) if hasattr(kodi, "find_episode") else None
        if ep and hasattr(kodi, "episode_details"):
            d = kodi.episode_details(ep.get("episodeid")) or {}
            rez = d.get("director") or []
            if rez:
                return [str(x) for x in rez]
    except Exception as e:
        _log.debug("kodi režie: %s", e)
    return None


def _z_wikipedie(config, titul: str) -> str:
    """Text článku o filmu ('' když nic). Zdroj pravdy pro ověření jména."""
    try:
        from scripts.web_reader import WebReader
        w = WebReader(config or {})
        for q in ("%s (film)" % titul, titul):
            art = w.wikipedia_article(q, lang="cs", max_chars=1500)
            if art and (art.get("text") or "").strip():
                return art["text"]
    except Exception as e:
        _log.debug("wiki režie: %s", e)
    return ""


def zkontroluj_rezii(odpoved: str, kodi=None, config=None) -> str:
    """Vrátí odpověď s ověřenou atribucí (nebo beze změny)."""
    if not odpoved:
        return odpoved
    m = _ATRIBUCE.search(odpoved)
    if not m:
        return odpoved
    tvrzeny = m.group("kdo")
    pred = odpoved[:m.start()]
    tituly = _TITUL.findall(pred)
    if not tituly:
        return odpoved              # bez titulu nemáme co ověřit
    titul = tituly[-1].strip()
    # HANS_DIRECTOR_ONLY_FILM_V1 (4.9.) — HOLÉ „od" NENÍ TVRZENÍ O REŽII.
    # Doloženo 3× ve třech testovacích rozhovorech: věta „Režiséra si ale
    # zpaměti raději neurčím." se lepila za pozdrav, za doporučení KNIH
    # („kniha „Fotografie" od Susan Sontagové" → autorka čtena jako režisérka)
    # a za tip na večer. Atribuce se přitom SMAZALA, takže odpověď zůstala
    # useknutá a doplněná nesouvisející omluvou.
    # Explicitní spouštěče (`režie`, `režisér`, `natočil`) jsou samy o sobě
    # filmové a platí dál beze změny. Jen u holého „od" se navíc žádá, aby
    # kolem titulu bylo FILMOVÉ slovo — přesně tak, jak vypadají zamýšlené
    # případy v komentáři výš (film *Sedmikrásky* od…, snímek Vlaky od…).
    if _JEN_OD.match(m.group("cele").strip()) and not _FILMOVE_SLOVO.search(pred):
        _log.info("HANS_DIRECTOR_ONLY_FILM_V1: %r — spoustec od bez "
                  "filmoveho kontextu, atribuci nechavam byt", titul)
        return odpoved
    prij = _prijmeni(tvrzeny)

    rezie = _z_kodi(kodi, titul)
    if rezie:
        if any(_sedi(prij, _prijmeni(r)) for r in rezie):
            return odpoved
        spravne = rezie[0]
        _log.info("HANS_FILM_DIRECTOR_CHECK_V1: %r není režisér %r → %r "
                  "(z knihovny)", tvrzeny, titul, spravne)
        return odpoved.replace(m.group("cele"), " od %s" % spravne, 1)

    clanek = _z_wikipedie(config, titul)
    if not clanek:
        _log.info("HANS_FILM_DIRECTOR_CHECK_V1: %r se ověřit nedá → ponechávám",
                  titul)
        return odpoved
    # ⚠️ NE podřetězcem: „Menzela" (od koho) × „Menzelem" (kým) se v koncovce
    # rozejdou a poctivé tvrzení by se zahodilo. Porovnává se po SLOVECH touž
    # funkcí jako u entit — odhaleno testem, ne úvahou.
    if prij and any(_sedi(prij, w) for w in re.findall(r"[\wá-ž]+", _fold(clanek))):
        return odpoved
    _log.info("HANS_FILM_DIRECTOR_CHECK_V1: %r u %r nesedí (v článku není) "
              "→ atribuce odstraněna", tvrzeny, titul)
    upravena = odpoved.replace(m.group("cele"), "", 1)
    # HANS_DIRECTOR_ONLY_FILM_V1 — bez „pane": tahle funkce adresáta NEZNÁ
    # a doloženě se ta věta lepila i do odpovědí ženám.
    return upravena.rstrip() + " Režiséra si ale zpaměti raději neurčím."
