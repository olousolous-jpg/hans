"""HANS_DOPORUCENI_OVERENI_V1 (26. 10.) — doporučený titul musí existovat.

/tazatel 26. 10.: „Znáte nějakou dobrou knihu o historii Českého ráje?“ →
„doporučil bych studii „Český ráj: příroda a kultura“ od Josefa Králíka“
(kniha neexistuje) a na doptání vymyšlený původ („narazil jsem při studiu“).
Údaj o světě musí být pravdivý i ve volném hovoru (uživatel 23. 10.).

Po odpovědi MODELU na žádost o doporučení: titul v uvozovkách / kurzívě ve
větě s doporučením se ověří v lokální Wikipedii (i v 1. pádě a s upřesněním),
v Hansově knihovně, deníku, Kodi a nakonec ve Wikidatech (štítek nebo alias
přesně). Nenalezený → věta (a navazující věty o něm) pryč; když nic nezbude,
poctivá věta. 📏 44 starších doporučení (`data/mereni/doporuceni_26_10/`):
zachyceno 8 vymyšlených (Obzory Manhattanu ×2, Sedm pechátů, Království
z kamene, Dovolená s duchy, Památky České krajiny, Záznamy z turnaje, Český
ráj: příroda a kultura) + 1 hraniční (Albertiho „O budově“ — skutečné dílo
pod vymyšleným českým názvem); skutečné tituly (Casablanca, Ivanhoe, Obraz
Doriana Graye i anglicky, filmy z Kodi, knihy z knihovny) projdou.
"""
from __future__ import annotations

import logging
import re
import sqlite3
import unicodedata
import urllib.parse

_log = logging.getLogger(__name__)

_ZADOST = re.compile(
    r"doporu[čc]\w*|navrhn\w*\s+(?:mi\s+)?(?:n[ěe]jak|knih|film)|"
    r"(?:zn[áa][šs]|zn[áa]te|v[íi][šs]|v[íi]te)\s+(?:o\s+)?(?:n[ěe]jak|dobr)\w*\s+"
    r"(?:knih|film|kn[íi][žz]k|studi|publikac|seri[áa]l|d[íi]l)|"
    r"co\s+(?:si\s+)?(?:p[řr]e[čc][íi]st|zhl[ée]dnout|pustit)", re.I)
_VETA_DOPORUCENI = re.compile(
    r"doporu[čc]\w*|navrhl?\w*|nab[íi]dl?\w*|zv[áa][žz]il\w*|zkuste|p[řr]e[čc][íi]st|"
    r"zhl[ée]dnout|vychutnat", re.I)
_TITUL = re.compile(r"[„\"“]([^„“\"\n]{2,80})[“\"”]|(?<!\*)\*([^*\n]{2,80})\*(?!\*)")
_NAVAZUJE = re.compile(
    r"(?:Je to|Jde o|Jedná se o|Považuji|Tato|Tento|Toto|Kniha|Film|Studie|Režíroval|"
    r"Napsal|Obsahuje|Věnuje se|Příběh|Hlavní)\b")


def _f(s) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", str(s or "").lower())
                   if unicodedata.category(c) != "Mn").strip(" .,:;")


def je_zadost(otazka: str) -> bool:
    return bool(otazka and _ZADOST.search(otazka))


def tituly_vety(veta: str) -> list:
    out = []
    for a, b in _TITUL.findall(veta or ""):
        x = re.sub(r"\s*\(\d{4}[^)]*\)$", "", (a or b)).strip(" .,:;")
        if len(x) >= 3 and x[:1].isupper() and len(x.split()) <= 8:
            out.append(x)
    return out


def _wd_existuje(t: str) -> bool:
    try:
        from scripts.hans_facts import _get
    except Exception:
        return True       # bez Wikidat nerozhodnu → nechat
    for jaz in ("cs", "en"):
        d = _get("https://www.wikidata.org/w/api.php?action=wbsearchentities&format=json"
                 "&limit=7&language=%s&search=%s" % (jaz, urllib.parse.quote(t))) or {}
        for e in d.get("search") or []:
            if (_f(e.get("label")) == _f(t) or any(_f(a) == _f(t) for a in (e.get("aliases") or []))
                    or _f((e.get("match") or {}).get("text")) == _f(t)):
                return True
    return False


def existuje(t: str, db_path: str = "data/hans_diary.db", kodi=None) -> str:
    """Kde titul existuje ('' = nikde)."""
    from scripts import hans_wiki_offline as wo
    try:
        r = wo._cil(t) or (wo._presna_zaloha(t) or (None, None))[1]
        if r:
            return "wiki"
        fdb = wo._fold_db()
        for suf in ("", " (kniha)", " (film)", " (román)", " (novela)", " (seriál)"):
            if fdb.execute("SELECT 1 FROM t WHERE fold=? LIMIT 1",
                           (wo._fold_tzv(t + suf),)).fetchone():
                return "wiki"
    except Exception as e:
        _log.debug("HANS_DOPORUCENI_OVERENI_V1 wiki: %s", e)
        return "?"       # lokální Wikipedie nedostupná → nerozhoduji
    db = None
    try:
        db = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True)
        if db.execute("SELECT 1 FROM hans_library WHERE lower(book_title) LIKE ?",
                      ("%" + t.lower() + "%",)).fetchone():
            return "knihovna"
        if db.execute("SELECT 1 FROM diary WHERE event_type IN ('movie_opinion','kodi_playing',"
                      "'book_read','web_read') AND lower(title)=?", (t.lower(),)).fetchone():
            return "denik"
    except Exception:
        pass
    finally:
        if db is not None:
            db.close()
    if kodi is not None:
        try:
            m = kodi.find_movie(t)
            if m and _f(m.get("title")) == _f(t):
                return "kodi"
        except Exception:
            pass
    return "wd" if _wd_existuje(t) else ""


def over(otazka: str, odpoved: str, db_path: str = "data/hans_diary.db", kodi=None):
    """Opravená odpověď, nebo None (beze změny)."""
    if not je_zadost(otazka) or not odpoved:
        return None
    vety = re.split(r"(?<=[.!?])\s+", odpoved.strip())
    pryc = set()
    for i, v in enumerate(vety):
        if not _VETA_DOPORUCENI.search(v):
            continue
        for t in tituly_vety(v):
            if not existuje(t, db_path, kodi):
                pryc.add(i)
                _log.info("HANS_DOPORUCENI_OVERENI_V1: doporučený titul %r nikde není "
                          "(Wikipedie, knihovna, deník, Kodi, Wikidata) → věta pryč", t)
                break
    if not pryc:
        return None
    zbyt, _ven = [], False
    for i, v in enumerate(vety):
        _ven = i in pryc or (_ven and bool(_NAVAZUJE.match(v)))
        if not _ven:
            zbyt.append(v)
    poctive = ("Konkrétní titul vám teď s jistotou doporučit nedokážu — nerad bych "
               "vymyslel knihu nebo film, které neexistují.")
    if zbyt and re.match(r"(?:Nebo|A také|Také|Případně)\s+", zbyt[0]):
        _z = re.sub(r"^(?:Nebo|A také|Také|Případně)\s+", "", zbyt[0])
        zbyt[0] = _z[:1].upper() + _z[1:]
    obsah = [v for v in zbyt if len(v) > 25 and not re.match(r"(?:Dobr|Ahoj|Zdrav)", v)]
    return (" ".join(zbyt + [poctive]) if not obsah else " ".join(zbyt)).strip()
