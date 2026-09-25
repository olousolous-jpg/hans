"""HANS_FILM_SIMILAR_V1 (25. 9.) — „něco podobného jako X“ → filmy téhož režiséra,
které v knihovně Kodi NEMÁME.

Nápad uživatele 22. 9. (NAPADY: „nabídni podobný film, ať si ho opatří“), zadání 25. 9.:
jen NA POŽÁDÁNÍ (nic proaktivního) a nabídka z Webshare ve stejném výpisu jako
`HANS_KODI_WEBSHARE_NABIDKA_V1`. ⛔ TMDb zamítnut uživatelem 2. 9. → jen Wikidata.

Cesta je deterministická: film z knihovny → IMDb ID (Kodi ho má u 954 z 968) →
položka Wikidat (P345) → režisér (P57) → jeho další filmy (haswbstatement, běžné
API, ne SPARQL) → pryč s tím, co knihovna UŽ MÁ (podle IMDb), bez české Wikipedie
a se seriály → seřadit podle počtu jazykových verzí (≈ známost).
Změřeno 25. 9. na 6 posledních filmech: Duna → Mulholland Drive, Sloní muž, Modrý
samet; Mys hrůzy → Taxikář, Zuřící býk… (Mazací hlava, Prokletý ostrov, Ponorka
správně poznány jako „máme“).

⚠️ `hans_facts._get` čeká až 20 s na dotaz a při 429 dalších 65 s — to je na odpověď
v chatu moc. Tady krátký timeout a bez ustupování: radši poctivé „teď to nejde“.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

_log = logging.getLogger("hans_film_podobny")

TIMEOUT_S = 8
_API = "https://www.wikidata.org/w/api.php?format=json&"
# seriály a epizody nejsou „film k opatření“
_NE_FILM = {"Q5398426", "Q21191270", "Q1259759", "Q15416", "Q526877",
            # krátký film, animovaný krátký film, hudební klip (Mijazaki: „On Your Mark“)
            "Q24862", "Q17517379", "Q64100970", "Q193977"}
# Bez české Wikipedie projde jen dost známý film — „Chlapec a volavka“ (2023) cs
# článek nemá, a byl to jediný Mijazakiho film, který knihovně chyběl.
MIN_SITELINKU_BEZ_CS = 15


class Nedostupne(Exception):
    """Wikidata teď neodpověděla (síť, 429) — NENÍ to „nic podobného není“."""


# Wikidata 25. 9. vracela 429 už po pár dotazech za sebou (sdílená kvóta s čtením
# filmových článků a nočním doplňováním faktů) s `Retry-After: 15`. Jeden dotaz
# na podobný film stojí 5–7 požadavků → jedno opakování po vyžádané pauze
# (nejvýš RETRY_MAX_S) a mezipaměť výsledků (filmografie se nemění).
RETRY_MAX_S = 15
CACHE_TTL_S = 30 * 86400
_CACHE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "data", "film_podobny_cache.json")


def _get(q: str) -> dict:
    from scripts.hans_facts import USER_AGENT
    req = urllib.request.Request(_API + q, headers={"User-Agent": USER_AGENT})
    for pokus in (0, 1):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT_S) as f:
                return json.load(f)
        except urllib.error.HTTPError as e:
            ra = 0.0
            try:
                ra = float((e.headers or {}).get("Retry-After") or 0)
            except Exception:
                pass
            if e.code == 429 and pokus == 0 and 0 < ra <= RETRY_MAX_S:
                _log.info("HANS_FILM_SIMILAR_V1: Wikidata 429 → čekám %.0f s", ra)
                time.sleep(ra)
                continue
            raise Nedostupne(str(e)) from e
        except Exception as e:
            raise Nedostupne(str(e)) from e
    raise Nedostupne("429")


def _cache_nacti() -> dict:
    try:
        with open(_CACHE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _cache_uloz(klic: str, hodnota) -> None:
    try:
        c = _cache_nacti()
        c[klic] = {"ts": time.time(), "v": hodnota}
        tmp = _CACHE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(c, f, ensure_ascii=False)
        os.replace(tmp, _CACHE)
    except Exception as e:
        _log.debug("cache zápis: %s", e)


def _cache_vem(klic: str):
    z = _cache_nacti().get(klic)
    if z and time.time() - float(z.get("ts", 0)) < CACHE_TTL_S:
        return z.get("v")
    return None


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


# ── název z věty ────────────────────────────────────────────────────────────

_ANAFORA = re.compile(
    r"^(?:t(?:en|enhle|ento|ohle|omuhle|omuto|omu|ahle|ato|o)|to\s+co\s+\w+|ten\s+co\s+\w+)"
    r"(?:\s+film\w*)?$", re.I)


def nazev_z_vety(veta: str) -> str:
    """Co stojí za „jako / k / podobný“. '' = nic (→ zkusí se, co právě běží)."""
    v = (veta or "").strip()
    m = (re.search(r"\bpodobn\w*[^?.!]{0,30}?\b(?:jako|k|ke)\s+(.+)$", v, re.I)
         or re.search(r"\bre[zž]is[eé]r\w*\s+(?:jako\s+(?:m[aá]\s+|u\s+)?|filmu\s+)(.+)$", v, re.I)
         or re.search(r"\bpodobn\w*\s+(?:film\w*\s+)?(.+)$", v, re.I))
    if not m:
        return ""
    t = re.sub(r"[?.!]+\s*$", "", m.group(1)).strip(" „“\"'")
    t = re.sub(r"^(?:film\w*|filmu)\s+", "", t, flags=re.I)
    # vata na konci věty („…, prosím“, „ … co máme“)
    t = re.sub(r"\s*,?\s*(?:pros[ií]m|pros[ií]m\s+t[eě]|d[eě]kuji|co\s+m[aá]me)\s*$", "", t, flags=re.I)
    return "" if _ANAFORA.match(t) else t


def _varianty(t: str) -> list:
    """Název v jiném pádě („Duně“, „Taxikáři“) → kandidáti 1. pádu.
    Hrubě a konečně: koncovky jednotlivých slov, bez slovníku."""
    slova = t.split()
    out = [t]
    for i, w in enumerate(slova):
        for kon, nahr in (("ě", "a"), ("e", "a"), ("u", "a"), ("y", "a"),
                          ("ou", "a"), ("i", ""), ("ovi", ""), ("em", ""), ("u", "")):
            if len(w) > len(kon) + 2 and w.lower().endswith(kon):
                nw = w[: len(w) - len(kon)] + nahr
                out.append(" ".join(slova[:i] + [nw] + slova[i + 1:]))
    return list(dict.fromkeys(out))


# ── knihovna ────────────────────────────────────────────────────────────────

def knihovna(kodi) -> list:
    r = kodi._call("VideoLibrary.GetMovies", {
        "properties": ["title", "originaltitle", "year", "uniqueid"],
        "limits": {"start": 0, "end": 5000}})
    return ((r or {}).get("result", {}) or {}).get("movies", []) or []


def najdi_v_knihovne(filmy: list, dotaz: str):
    """Přesná shoda názvu (i originálního) v kterékoli pádové variantě, pak
    shoda celého dotazu na hranici slova (min. 4 znaky — viz HANS_KODI_FIND_V3)."""
    vv = [_norm(x) for x in _varianty(dotaz)]
    vv = [x for x in vv if x]
    if not vv:
        return None
    for v in vv:
        for m in filmy:
            if v in (_norm(m.get("title")), _norm(m.get("originaltitle"))):
                return m
    for v in vv:
        if len(v) < 4:
            continue
        pat = re.compile(r"(?:^| )%s(?: |$)" % re.escape(v))
        shody = [m for m in filmy
                 if pat.search(_norm(m.get("title"))) or pat.search(_norm(m.get("originaltitle")))]
        if shody:   # víc dílů série → nejkratší název je nejblíž dotazu
            return min(shody, key=lambda m: len(m.get("title") or ""))
    return None


# ── Wikidata ────────────────────────────────────────────────────────────────

def _entity(ids: list, props="claims|labels|sitelinks") -> dict:
    out = {}
    for i in range(0, len(ids), 50):
        d = _get("action=wbgetentities&languages=cs|en&props=%s&ids=%s"
                 % (props, "|".join(ids[i:i + 50])))
        out.update(d.get("entities", {}) or {})
    return out


def _vals(e: dict, p: str) -> list:
    out = []
    for c in (e.get("claims", {}) or {}).get(p, []) or []:
        dv = (c.get("mainsnak", {}) or {}).get("datavalue")
        if dv:
            out.append(dv.get("value"))
    return out


def _label(e: dict) -> str:
    L = e.get("labels", {}) or {}
    return ((L.get("cs") or L.get("en") or {}).get("value") or "").strip()


def _cs_nazev(e: dict) -> str:
    sl = (e.get("sitelinks", {}) or {}).get("cswiki", {}) or {}
    t = re.sub(r"\s*\((?:film|[^)]*film)[^)]*\)\s*$", "", sl.get("title") or "").strip()
    return t or _label(e)


def _qid(film: dict) -> str:
    uid = film.get("uniqueid")
    uid = uid if isinstance(uid, dict) else {}   # TV kanál má uniqueid číslo
    q = (uid.get("wikidata") or "").strip()
    if re.match(r"^Q\d+$", q):
        return q
    imdb = (uid.get("imdb") or "").strip()
    if not re.match(r"^tt\d{5,10}$", imdb):
        return ""
    z = _cache_vem("imdb:" + imdb)
    if z:
        return z
    d = _get("action=query&list=search&srlimit=1&srnamespace=0&srsearch="
             + urllib.parse.quote("haswbstatement:P345=" + imdb))
    hits = (d.get("query", {}) or {}).get("search", []) or []
    q = (hits[0].get("title") or "") if hits else ""
    q = q if re.match(r"^Q\d+$", q) else ""
    if q:
        _cache_uloz("imdb:" + imdb, q)
    return q


def qid_podle_nazvu(nazev: str) -> tuple:
    """Film MIMO knihovnu („podobný jako Taxikář“, který nemáme): položka podle
    názvu, jen když je to film (má režiséra i IMDb ID). → (qid, český název)."""
    for t in _varianty(nazev)[:4]:
        d = _get("action=wbsearchentities&type=item&limit=7&language=cs&uselang=cs&search="
                 + urllib.parse.quote(t))
        ids = [x.get("id") for x in d.get("search", []) or [] if x.get("id")]
        if not ids:
            continue
        E = _entity(ids, "claims|labels|sitelinks")
        for q in ids:
            e = E.get(q) or {}
            if _vals(e, "P57") and _vals(e, "P345"):
                return q, _cs_nazev(e)
    return "", ""


def podobne(film: dict, filmy: list, limit: int = 3) -> tuple:
    """(režisér, [ {nazev, rok, sitelinky} ]) — filmy téhož režiséra, které
    knihovna nemá. Vyhodí `Nedostupne`, když Wikidata neodpoví."""
    q = film.get("_qid") or _qid(film)
    if not q:
        return "", []
    mame = {(m.get("uniqueid") or {}).get("imdb") or "" for m in filmy
            if isinstance(m.get("uniqueid") or {}, dict)} - {""}
    # V mezipaměti je filmografie PŘED odečtením knihovny — co mezitím přibylo
    # do Kodi, se odečte znovu.
    z = _cache_vem("rez:" + q)
    if z is None:
        z = _filmografie(q)
        _cache_uloz("rez:" + q, z)
    rez, kand = z
    kand = [k for k in kand if not set(k.get("imdb") or []) & mame]
    for k in kand:
        k.pop("imdb", None)
    return rez, kand[:limit]


def _filmografie(q: str) -> list:
    """[režisér(i), [kandidáti se seznamem IMDb ID]] — bez ohledu na knihovnu."""
    e = _entity([q], "claims").get(q, {})
    reziseri = [v.get("id") for v in _vals(e, "P57") if isinstance(v, dict)][:2]
    if not reziseri:
        return "", []
    jmena, kand = [], {}
    for d in reziseri:
        s = _get("action=query&list=search&srlimit=50&srnamespace=0&srsearch="
                 + urllib.parse.quote("haswbstatement:P57=" + d))
        qs = [x.get("title") for x in (s.get("query", {}) or {}).get("search", []) or []]
        qs = [x for x in qs if x and x != q and x not in kand]
        E = _entity(qs + [d])
        jmena.append(_label(E.get(d, {})))
        for q2 in qs:
            e2 = E.get(q2) or {}
            imdb = [v for v in _vals(e2, "P345") if isinstance(v, str) and v.startswith("tt")]
            if not imdb:
                continue
            typy = {v.get("id") for v in _vals(e2, "P31") if isinstance(v, dict)}
            if typy & _NE_FILM:
                continue
            sl = e2.get("sitelinks", {}) or {}
            if "cswiki" not in sl and len(sl) < MIN_SITELINKU_BEZ_CS:
                continue
            roky = [str(v.get("time", ""))[1:5] for v in _vals(e2, "P577") if isinstance(v, dict)]
            roky = sorted(r for r in roky if r.isdigit())
            kand[q2] = {"nazev": _cs_nazev(e2), "rok": roky[0] if roky else "",
                        "sitelinky": len(sl), "imdb": imdb}
    vys = sorted(kand.values(), key=lambda k: -k["sitelinky"])[:40]
    return [" a ".join(j for j in jmena if j), vys]
