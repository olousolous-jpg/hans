"""HANS_OSM_V1 (3. 11. 2026) — praktické údaje k místům z OpenStreetMap offline
(`data/osm/poi.db`, sestavuje `tools/osm_poi.py`): kde se najíst, otevírací doba,
značené trasy, parkování. Data © přispěvatelé OpenStreetMap (ODbL).
"""
from __future__ import annotations

import logging
import math
import os
import re
import sqlite3

_log = logging.getLogger(__name__)
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(_ROOT, "data", "osm", "poi.db")

_DNY = {"Mo": "po", "Tu": "út", "We": "st", "Th": "čt", "Fr": "pá", "Sa": "so", "Su": "ne",
        "PH": "svátky", "off": "zavřeno", "closed": "zavřeno"}
_MESICE = {"Jan": "led", "Feb": "úno", "Mar": "bře", "Apr": "dub", "May": "kvě", "Jun": "čer",
           "Jul": "čvc", "Aug": "srp", "Sep": "zář", "Oct": "říj", "Nov": "lis", "Dec": "pro"}
_JIDLO = {"restaurant": "restaurace", "cafe": "kavárna", "pub": "hospoda", "fast_food": "bistro",
          "biergarten": "pivní zahrádka", "ice_cream": "zmrzlina"}
_BARVY = {"red": "červená", "blue": "modrá", "green": "zelená", "yellow": "žlutá",
          "white": "bílá", "black": "černá", "orange": "oranžová", "purple": "fialová"}


def _db():
    if not os.path.exists(DB):
        return None
    return sqlite3.connect("file:%s?mode=ro" % DB, uri=True, timeout=3)


def _vzdal(a1, o1, a2, o2) -> float:
    p1, p2 = math.radians(a1), math.radians(a2)
    h = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(o2 - o1) / 2) ** 2)
    return 2 * 6371.0 * math.asin(math.sqrt(h))


def _okolo(c, tab: str, lat: float, lon: float, r_km: float, kde: str = "", par=()):
    dl = r_km / 111.0
    do = r_km / (111.0 * max(0.2, math.cos(math.radians(lat))))
    return c.execute("SELECT * FROM %s WHERE lat BETWEEN ? AND ? AND lon BETWEEN ? AND ? %s"
                     % (tab, kde), (lat - dl, lat + dl, lon - do, lon + do, *par)).fetchall()


def cesky_cas(oh: str) -> str:
    """OSM opening_hours → čitelně česky („Tu-Su 09:00-17:00“ → „út–ne 9:00–17:00“)."""
    s = (oh or "").strip()
    if not s:
        return ""
    s = re.sub(r"\b(Mo|Tu|We|Th|Fr|Sa|Su|PH|off|closed)\b", lambda m: _DNY[m.group(1)], s)
    s = re.sub(r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\b",
               lambda m: _MESICE[m.group(1)], s)
    s = re.sub(r"\b0(\d):", r"\1:", s)
    s = s.replace("-", "–").replace(";", ";").replace("24/7", "nonstop")
    s = re.sub(r"\b(\w{3})–\1\b", r"\1", s)       # „dub–dub“ → „dub“ (HANS_OSM_KONTEXT_V1)
    return s


def oteviraci_doba(nazev: str, lat: float, lon: float, r_km: float = 1.0, druhy=None,
                   presne: bool = False) -> str:
    """Otevírací doba místa podle názvu v okolí souřadnic (OSM), nebo ''."""
    c = _db()
    if c is None:
        return ""
    try:
        from scripts.hans_mista import _jmeno
        jm = _f(_jmeno(nazev))
        kand = []
        for r in _okolo(c, "poi", lat, lon, r_km, "AND otevreno != ''"):
            if r[2] in _JIDLO or r[2] == "parkoviště":
                continue                  # „Hotel Podkost“ není hrad Kost
            if druhy and not str(r[2]).startswith(tuple(druhy)):   # HANS_OSM_ZOO_V1
                continue
            n = _f(r[3])
            vz = r"(?<!\w)%s(?!\w)"
            if n and (n == jm or (not presne and (re.search(vz % re.escape(jm), n)
                                                  or re.search(vz % re.escape(n), jm)))):
                kand.append((_vzdal(lat, lon, r[0], r[1]), r[4]))
        return cesky_cas(min(kand)[1]) if kand else ""
    finally:
        c.close()


def web_mista(nazev: str, lat: float, lon: float, r_km: float = 1.0, druhy=None,
              presne: bool = False) -> str:
    """HANS_OSM_ZAMEK_V1 — web místa podle názvu v okolí (OSM `website`), nebo ''."""
    c = _db()
    if c is None:
        return ""
    try:
        from scripts.hans_mista import _jmeno
        jm = _f(_jmeno(nazev))
        kand = []
        for r in _okolo(c, "poi", lat, lon, r_km, "AND web != ''"):
            if r[2] in _JIDLO or r[2] == "parkoviště":
                continue
            if druhy and not str(r[2]).startswith(tuple(druhy)):   # HANS_OSM_ZOO_V1
                continue
            n = _f(r[3])
            vz = r"(?<!\w)%s(?!\w)"
            if n and (n == jm or (not presne and (re.search(vz % re.escape(jm), n)
                                                  or re.search(vz % re.escape(n), jm)))):
                kand.append((_vzdal(lat, lon, r[0], r[1]), r[5]))
        return min(kand)[1] if kand else ""
    finally:
        c.close()


def _f(s: str) -> str:
    import unicodedata
    return "".join(ch for ch in unicodedata.normalize("NFD", (s or "").lower())
                   if unicodedata.category(ch) != "Mn").strip()


def jidlo(lat: float, lon: float, r_km: float = 1.5, kolik: int = 6) -> list:
    """[(km, druh, název, otevřeno)] nejbližší restaurace, kavárny, hospody."""
    c = _db()
    if c is None:
        return []
    try:
        out = [(_vzdal(lat, lon, r[0], r[1]), _JIDLO.get(r[2], r[2]), r[3], cesky_cas(r[4]))
               for r in _okolo(c, "poi", lat, lon, r_km,
                               "AND druh IN (%s)" % ",".join("?" * len(_JIDLO)), tuple(_JIDLO))]
    finally:
        c.close()
    return sorted(o for o in out if o[0] <= r_km)[:kolik]


def parkovani(lat: float, lon: float, r_km: float = 1.5, kolik: int = 3) -> list:
    """[(km, název, poplatek)]"""
    c = _db()
    if c is None:
        return []
    try:
        out = [(_vzdal(lat, lon, r[0], r[1]), r[3] or "parkoviště",
                {"yes": "placené", "no": "zdarma"}.get(r[7], ""))
               for r in _okolo(c, "poi", lat, lon, r_km, "AND druh='parkoviště'")]
    finally:
        c.close()
    return sorted(o for o in out if o[0] <= r_km)[:kolik]


def _barva(r) -> str:
    b = (r[2] or "").lower()
    if not b and r[3]:
        b = r[3].split(":")[0].lower()
    return _BARVY.get(b, b)


def trasy(lat: float, lon: float, r_km: float = 1.0, kolik: int = 6) -> list:
    """[(km k trase, barva, název, od, do)] značené pěší trasy procházející okolím."""
    c = _db()
    if c is None:
        return []
    try:
        blizko: dict = {}
        for t, a, b in _okolo(c, "trasa_bod", lat, lon, r_km):
            d = _vzdal(lat, lon, a, b)
            if d <= r_km and d < blizko.get(t, 99):
                blizko[t] = d
        out = []
        for t, d in sorted(blizko.items(), key=lambda x: x[1]):
            r = c.execute("SELECT * FROM trasa WHERE id=?", (t,)).fetchone()
            if r:
                out.append((d, _barva(r), r[1], r[4], r[5]))
    finally:
        c.close()
    vid, uniq = set(), []
    for o in out:
        k = (o[1], o[2])
        if k not in vid:
            vid.add(k)
            uniq.append(o)
    return uniq[:kolik]


def _km(k: float) -> str:
    return ("%d m" % (round(k * 1000, -1))) if k < 1 else ("%.1f km" % k).replace(".", ",")


# ── dotaz z Matrixu ─────────────────────────────────────────────────────────
_CO = [("jidlo", r"\b(?:naj[ií]st|naj[ií]me|j[ií]dl\w*|restaurac\w*|hospod\w*|kav[aá]rn\w*|ob[eě]d\w*|"
                 r"ve[cč]e[rř]\w*|ob[cč]erstven\w*)\b"),
       ("otevreno", r"\b(?:otev[rř]en\w*|otev[ií]rac\w*|otev[ií]r[aá]\w*|zav[ií]r[aá]\w*|"
                    r"vstup\w*\s+do)\b"),
       ("trasy", r"\b(?:tras\w*|stezk\w*|pěšk\w*|pesk\w*|turistick\w*|značk\w*|znack\w*|"
                 r"kudy)\b"),
       ("parkovani", r"\b(?:parkov\w*|zaparkov\w*|auto\w*\s+nech)\b"),
       # HANS_OSM_KONTEXT_V1 — počasí jen u pojmenovaného místa / „tam“ (obecné má agent)
       ("pocasi", r"\b(?:po[cč]as\w*|pr[sš][ií]\w*|bude\s+pr[sš]et|teplot\w*)\b")]
# HANS_OSM_KONTEXT_V1 (3. 11.) — „u/kolem HRADU Pernštejn“: druh stavby mezi předložkou
# a jménem (dřív jen velké písmeno hned za předložkou → hovor → článek bez restaurací)
_DRUH_MISTA = {"hrad": "hrad", "z[aá]m[e]?k": "zámek", "kl[aá][sš]ter": "klášter",
               "z[rř][ií]cenin": "zřícenina", "rozhledn": "rozhledna", "muze": "muzeum",
               "kostel": "kostel"}
_MISTO = re.compile(r"\b(?P<p>u|v|ve|na|do|kolem|pobl[ií][zž])\s+(?:(?P<d>hrad|z[aá]m[e]?k|kl[aá][sš]ter|"
                    r"z[rř][ií]cenin|rozhledn|muze|kostel)\w{0,3}\s+)?(?P<m>(?-i:[A-ZÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ])"
                    r"[^?!.,]{1,40})", re.I)
_OKOLI = re.compile(r"\b(?:v\s+oko[a-zíi]{1,3}|pobl[ií][zž]|nabl[ií]zku|tady|tu|tam|kolem|n[eě]kde)\b", re.I)
# poslední pojmenované místo, na které se osoba ptala (jen v paměti, 2 h):
# „kdy je otevřený Pernštejn?“ → „dá se tam někde najíst?“
posledni_misto: dict = {}       # osoba → (jméno v dotazu, lat, lon, titul, druh, ts)


def _druh(slovo: str) -> str:
    for vz, d in _DRUH_MISTA.items():
        if re.match(vz, slovo or "", re.I):
            return d
    return ""


def je_dotaz(text: str):
    """(co, název místa | '') nebo None."""
    t = text or ""
    co = next((k for k, p in _CO if re.search(p, t, re.I)), None)
    if not co:
        return None
    m = _MISTO.search(t)
    if m:
        # jméno končí před slovesem / časem („Pernštejn zítra počasí“, „Kost dá najíst“)
        jm = re.split(r"\s+(?:z[ií]tra|dnes|poz[ií]t[rř][ií]|o\s+v[ií]kendu|bude|je|d[aá]|da|se|"
                      r"po[cč]as\w*|naj[ií]st|otev\w*|parkov\w*|tras\w*)\b", m.group("m"), 1, flags=re.I)[0]
        pr = m.group("p").lower()
        return co, jm.strip(), _druh(m.group("d") or ""), ("u" if pr == "do" else pr)
    if co == "pocasi" and not re.search(r"\btam\b", t, re.I):
        return None                      # obecné počasí = doma (agent), ne poslední místo
    if not m and co == "otevreno":       # „kdy je otevřený Kokořín?“, „kdy otevírá Kost“
        m = re.search(r"\botev\w*\s+(?:je\s+)?(?:(?P<d>hrad|z[aá]mek|muzeum|z[rř][ií]cenin\w*|kl[aá][sš]ter|galerie|rozhledn\w*|zoo|zoologick\w*\s+zahrad\w*)\s+)?(?P<m>(?-i:[A-ZÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ])[^?!.,]{1,40})", t, re.I)
    if m:                                # HANS_OSM_ZAMEK_V1 — druh i tady („zámek Lednice“)
        return co, m.group("m").strip(), _druh(m.group("d") or ""), ""
    if _OKOLI.search(t):
        return co, "", "", "u"
    return None


_STAVBY = ("castle", "ruins", "fort", "manor", "monastery")
_ZOO = re.compile(r"\b(?:zoo|zoologick\w*\s+zahrad\w*|zoologi\w*)\b", re.I)


def zoo_u(lat: float, lon: float, r_km: float = 15.0):
    """HANS_OSM_ZOO_V1 — nejbližší zoo (vzdálenost, název, opening_hours, web), nebo None."""
    c = _db()
    if c is None:
        return None
    try:
        rows = [r for r in _okolo(c, "poi", lat, lon, r_km, "AND druh = 'zoo'") if r[3]]
    finally:
        c.close()
    # skutečná zoo před motýlím domem a minizoo: „zoo/zoologická“ v názvu, heslo ve Wikidatech
    kand = []
    for r in rows:
        k = _vzdal(lat, lon, r[0], r[1])
        if k <= r_km:
            kand.append(((not re.search(r"\bzoo|zoologick", _f(r[3])), not r[6], k),
                         (k, r[3], r[4] or "", r[5] or "")))
    return min(kand)[1] if kand else None


def odpoved_na_dotaz(text: str, osoba: str):
    r = je_dotaz(text)
    if not r:
        return None
    co, jm, druh, pr = r
    from scripts import hans_mista as hm
    import time as _t
    if jm:
        s = None
        if druh:                         # „hradu Pernštejn“ → nejdřív „Pernštejn (hrad)“
            s = hm.souradnice_lokalne("%s (%s)" % (hm._jmeno(jm), druh)) if hasattr(hm, "souradnice_lokalne") else None
            if s:
                s = (s[0], s[1], "%s (%s)" % (hm._jmeno(jm), druh))
        s = s or hm.souradnice(jm)
        if not s:
            return None
        lat, lon, titul = s
        kde = ("%s %s" % ({"hrad": "hradu", "zámek": "zámku", "klášter": "kláštera", "zřícenina": "zříceniny",
                           "rozhledna": "rozhledny", "muzeum": "muzea", "kostel": "kostela"}[druh], jm)
               if druh else jm)          # tvar, jak ho napsal člověk („u Kosti“, „u hradu Pernštejn“)
        if not pr:                       # „kdy je otevřený Pernštejn“ — jméno v 1. pádě
            kde, pr = "místa " + hm._jmeno(titul), "u"
        zapamatuj(osoba, kde, lat, lon, titul, pr)
    elif osoba in posledni_misto and _t.time() - posledni_misto[osoba][5] < hm._CERSTVA_S and (
            not hm.posledni.get(osoba) or hm.posledni[osoba][2] < posledni_misto[osoba][5]):
        kde, lat, lon, titul = posledni_misto[osoba][:4]   # HANS_OSM_KONTEXT_V1
        pr = posledni_misto[osoba][4] or "u"
        _log.info("HANS_OSM_KONTEXT_V1: bez místa v dotazu → poslední místo z hovoru (%s)", titul)
    else:
        p = hm.posledni.get(osoba)
        if not (p and _t.time() - p[2] < hm._CERSTVA_S):
            return ("Polohu si sám vyžádat neumím — pošlete mi ji prosím (+ → Poloha), nebo "
                    "napište název místa, třeba „kde se najíst u Kosti?“")
        lat, lon, titul, kde, pr = p[0], p[1], "", "vás", "u"
    if co == "pocasi":                   # HANS_OSM_KONTEXT_V1
        pp = hm.pocasi(lat, lon)
        return ("Počasí %s %s: %s" % (pr, kde, pp)) if pp else "Předpověď pro %s teď nemám." % kde
    if _db() is None:
        return "Mapová data OpenStreetMap ještě nemám stažená."
    if _ZOO.search(text or ""):            # HANS_OSM_ZOO_V1 — „zoo u Hluboké“
        z = zoo_u(lat, lon)
        if not z:
            return "Zoologickou zahradu do 15 km od %s v mapových datech nemám." % (kde if titul else "vás")
        k, nz, oh, wb = z
        if co == "otevreno":
            if oh:
                return ("%s (%s od %s): %s (podle OpenStreetMap — před cestou raději ověřte%s)."
                        % (nz, _km(k), ("místa " + hm._jmeno(titul)) if titul else "vás", cesky_cas(oh),
                           (" na webu: " + wb) if wb else ""))
            return ("%s (%s): otevírací dobu v mapových datech nemám%s"
                    % (nz, _km(k), (" — aktuální bude na webu: " + wb) if wb else "."))
        zz = hm.souradnice_lokalne(nz) or None
        if co in ("jidlo", "parkovani", "trasy") and zz:
            lat, lon, kde, pr = zz[0], zz[1], nz, "u"
    if co == "otevreno":
        if not titul:
            return "Napište prosím, o které místo jde, třeba „kdy je otevřený Kokořín?“"
        # stavba má přednost před stejnojmennou obcí („Kokořín“ = obec, hrad je ~1,5 km dál)
        # HANS_OSM_ZAMEK_V1 (3. 11.) — nejdřív jméno, jak ho člověk napsal, s druhem z otázky:
        # „zámek Lednice“ → „Lednice (zámek)“ (dřív se z holého „Lednice“ stala slovenská Lednica)
        o, web, jm1 = "", "", hm._jmeno(titul)
        zak = hm._jmeno(titul)
        kandidati = ([jm + " (%s)" % druh] if druh and jm else []) + (
            [jm + " (hrad)", jm + " (zámek)"] if jm else []) + [zak + " (hrad)", zak + " (zámek)", titul]
        for kand in dict.fromkeys(kandidati):
            s2 = hm.souradnice_lokalne(kand)
            if s2:
                # HANS_OSM_ZOO_V1 (4. 11.) — „Praha (hrad)“ sedlo na „Kunsthalle Praha“:
                # kandidát s druhem smí jen na stavbu, holý název jen přesně
                _dr = (_STAVBY if re.search(r"\((?:hrad|zámek)\)$", kand) else None)
                o = oteviraci_doba(kand, s2[0], s2[1], druhy=_dr, presne=_dr is None)
                web = web or web_mista(kand, s2[0], s2[1], druhy=_dr, presne=_dr is None)
                if o or web:
                    jm1 = hm._jmeno(kand)
                    zdr = re.search(r"\((hrad|zámek)\)$", kand)   # druh z článku, ne z otázky
                    d1 = zdr.group(1) if zdr else ""
                    if d1 and d1 not in jm1.lower():
                        jm1 = "%s %s" % (d1, jm1)
                    break
        if not o and not web:
            o = oteviraci_doba(titul, lat, lon, presne=True)
            web = web_mista(titul, lat, lon, presne=True)
        _gen = {"hrad": "hradu", "zámek": "zámku"}
        d0 = jm1.split(" ", 1)[0].lower()
        jm_gen = ("%s %s" % (_gen[d0], jm1.split(" ", 1)[1])) if d0 in _gen and " " in jm1 else "místa " + jm1
        if o:
            return ("%s: %s (podle OpenStreetMap — před cestou raději ověřte na webu místa%s)."
                    % (jm1[:1].upper() + jm1[1:], o, (": " + web) if web else ""))
        if web:
            return ("Otevírací dobu %s v mapových datech nemám — aktuální bude na webu: %s" % (jm_gen, web))
        return "Otevírací dobu %s v mapových datech nemám." % jm_gen
    if co == "jidlo":
        jj = jidlo(lat, lon)
        if not jj:
            return "Do 1,5 km od %s jsem v mapě nenašel restauraci ani kavárnu." % (kde if titul else "vás")
        return "Kde se najíst %s %s:\n" % (pr, kde) + "\n".join(
            "• %s (%s), %s%s" % (n, d, _km(k), (" — " + o) if o else "") for k, d, n, o in jj)
    if co == "parkovani":
        pp = parkovani(lat, lon)
        if not pp:
            return "Pojmenované ani placené parkoviště do 1,5 km od %s v mapě nemám." % (kde if titul else "vás")
        return "Parkování %s %s:\n" % (pr, kde) + "\n".join(
            "• %s, %s%s" % (n, _km(k), (" — " + p) if p else "") for k, n, p in pp)
    tt = trasy(lat, lon)
    if not tt:
        return "Značenou turistickou trasu do 1 km od %s v mapě nemám." % (kde if titul else "vás")
    return "Značené trasy %s %s:\n" % (pr, kde) + "\n".join(
        "• %s%s%s, %s" % (("%s: " % b) if b else "", n or "trasa",
                          (" (%s → %s)" % (od, do_)) if od and do_ else "", _km(k))
        for k, b, n, od, do_ in tt)


def zapamatuj(osoba: str, kde: str, lat: float, lon: float, titul: str, pr: str = "u") -> None:
    """HANS_OSM_KONTEXT_V1 — poslední pojmenované místo dotazu (i z `hans_mista`)."""
    import time as _t
    if osoba:
        posledni_misto[osoba] = (kde, lat, lon, titul, pr, _t.time())
