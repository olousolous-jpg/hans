"""HANS_MISTA_V1 (3. 11. 2026) — zajímavá místa v okolí podle polohy z telefonu.

Člen domácnosti pošle Hansovi přes Matrix (Element X) polohu (`m.location`,
`geo:lat,lon`). Hans se zeptá Wikidat, co v okruhu má článek na české Wikipedii,
vybere památky a přírodu (ne obce, rybníky, zastávky), seřadí podle svých zájmů,
významu (délka článku v lokální Wikipedii) a vzdálenosti a pošle krátký seznam.

🔒 Soukromí: souřadnice se nikam neukládají ani nelogují — jen poslední poloha
v paměti procesu (pro „a další?“), mezipaměť výsledků taky jen v paměti.
"""
from __future__ import annotations

import logging
import math
import re
import time

_log = logging.getLogger(__name__)

_SPARQL = "https://query.wikidata.org/sparql"

# druh (štítek P31 česky) → váha zájmu. Hrady a architektura jsou Hansovy koníčky.
_DRUHY = {
    "hrad": 3.0, "zřícenina hradu": 3.0, "skalní hrad": 3.0, "tvrz": 2.5, "zámek": 2.8,
    "zřícenina": 2.2, "hradiště": 2.0, "klášter": 2.5, "kostel": 1.8, "katedrála": 2.5,
    "bazilika minor": 2.3, "poutní kostel": 2.0, "synagoga": 2.0, "kaple": 1.2,
    "muzeum": 2.0, "galerie": 1.8, "skanzen": 2.0, "rozhledna": 2.0, "vyhlídka": 1.6,
    "vyhlídková věž": 2.0, "skalní město": 2.5, "jeskyně": 2.0, "skalní útvar": 1.5,
    "národní přírodní památka v Česku": 2.0, "národní přírodní rezervace v Česku": 2.0,
    "přírodní památka v Česku": 1.3, "přírodní rezervace v Česku": 1.4,
    "chráněná krajinná oblast v Česku": 1.5, "národní park": 2.0, "vodopád": 1.6,
    "vodní mlýn": 1.4, "větrný mlýn": 1.6, "naučná stezka": 1.0, "sousoší": 1.0,
    "socha": 0.8, "pomník": 1.0, "památník": 1.3, "hrobka": 1.2, "zahrada": 1.6,
    "park": 1.2, "zámecký park": 1.6, "most": 1.0, "viadukt": 1.3, "kamenný most": 1.5,
    "pivovar": 1.0, "hvězdárna": 1.8, "kopec": 0.8, "hora": 1.0, "významný strom": 0.6,
}

_mezipamet: dict = {}            # (lat, lon, r) zaokrouhleně → (ts, seznam) — jen v paměti
posledni: dict = {}             # osoba → (lat, lon, ts) — jen v paměti


def parse_geo(geo_uri: str):
    """„geo:50.468,15.135;u=12“ → (lat, lon), nebo None."""
    m = re.match(r"\s*geo:\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)", geo_uri or "")
    if not m:
        return None
    lat, lon = float(m.group(1)), float(m.group(2))
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return lat, lon


def _wikidata(lat: float, lon: float, r_km: float, timeout: float = 30.0) -> list:
    """[(titul cs, vzdálenost km, {druhy})] z Wikidat (jen položky s českým článkem)."""
    import requests
    from scripts.hans_facts import USER_AGENT
    q = ("SELECT ?cs ?dist ?loc ?typLabel WHERE { SERVICE wikibase:around { ?item wdt:P625 ?loc . "
         "bd:serviceParam wikibase:center \"Point(%.5f %.5f)\"^^geo:wktLiteral . "
         "bd:serviceParam wikibase:radius \"%g\" . bd:serviceParam wikibase:distance ?dist . } "
         "?cs schema:about ?item ; schema:isPartOf <https://cs.wikipedia.org/> . "
         "OPTIONAL { ?item wdt:P31 ?typ . } "
         "SERVICE wikibase:label { bd:serviceParam wikibase:language \"cs\". } } "
         "ORDER BY ?dist LIMIT 6000" % (lon, lat, r_km))
    for pokus in (1, 2):                 # výpadek/přetížení dotazovací služby → jednou znovu
        try:
            r = requests.get(_SPARQL, params={"query": q, "format": "json"},
                             headers={"User-Agent": USER_AGENT}, timeout=timeout)
            r.raise_for_status()
            break
        except Exception:
            if pokus == 2:
                raise
            time.sleep(3)
    out: dict = {}
    for b in r.json().get("results", {}).get("bindings", []):
        t = requests.utils.unquote(b["cs"]["value"].rsplit("/", 1)[1]).replace("_", " ")
        m = re.search(r"Point\(\s*(-?[\d.]+)\s+(-?[\d.]+)", (b.get("loc") or {}).get("value", ""))
        v = out.setdefault(t, [float(b["dist"]["value"]), set(),
                               (float(m.group(2)), float(m.group(1))) if m else None])
        v[1].add((b.get("typLabel") or {}).get("value", ""))
    return [(t, v[0], v[1], v[2]) for t, v in out.items()]


def _delka_clanku(titul: str) -> int:
    try:
        from scripts import hans_wiki_offline as wo
        r = wo._cil(titul)
        if not r:
            return 0
        return int(wo._db().execute("SELECT length(text) FROM clanek WHERE id=?",
                                    (r[0],)).fetchone()[0] or 0)
    except Exception:
        return 0


def _prvni_veta(titul: str, strop: int = 170) -> str:
    try:
        from scripts import hans_wiki_offline as wo
        text = (wo.extrakt(titul, intro_only=True)[0] or "").strip()
    except Exception:
        return ""
    radky = [l.strip() for l in text.split("\n") if l.strip()
             and not l.startswith(("Další význam", "Tento článek", "O ", "Na je heslo",
                                   "Možná hledáte", "Nezaměňovat", "Viz též"))]
    if not radky:
        return ""
    t = re.sub(r"\s*\([^()]{0,120}\)", "", radky[0])      # závorky s výslovností a daty pryč
    # konec věty, ne zkratka („při bazilice sv. Jiří“, „st.“, „n. l.“)
    m = re.search(r"^(.{40,}?[a-zě-ž0-9\)])(?<!\ssv)(?<!\sst)(?<!\sn)(?<!\sl)(?<!\sr)\.(\s|$)", t)
    v = (m.group(1) + ".") if m else t
    return v if len(v) <= strop else v[:strop].rsplit(" ", 1)[0] + "…"


def mista(lat: float, lon: float, r_km: float = 15.0, kolik: int = 6) -> list:
    """[{titul, km, druh, skore, veta}] seřazené podle zájmu, významu a vzdálenosti."""
    klic = (round(lat, 2), round(lon, 2), r_km)
    c = _mezipamet.get(klic)
    if c and time.time() - c[0] < 6 * 3600:
        seznam = c[1]
    else:
        seznam = []
        for titul, km, druhy, poloha in _wikidata(lat, lon, r_km):
            vahy = [(_DRUHY[d], d) for d in druhy if d in _DRUHY]
            if not vahy:
                continue
            w, druh = max(vahy)
            delka = _delka_clanku(titul)
            if delka < 600:                                 # pahýl — nic k vyprávění
                continue
            skore = w * (1 + math.log10(delka / 600)) / (1 + km / 6)
            seznam.append({"titul": titul, "km": km, "druh": druh, "skore": skore,
                           "poloha": poloha})
        seznam.sort(key=lambda x: -x["skore"])
        _mezipamet[klic] = (time.time(), seznam)
    out = []
    # výběr podle zájmu a významu, pořadí v odpovědi od nejbližšího (uživatel 3. 11.)
    for x in sorted(seznam[:kolik], key=lambda x: x["km"]):
        out.append(dict(x, veta=_prvni_veta(x["titul"])))
    return out


def _jmeno(titul: str) -> str:
    """Titul bez upřesnění v závorce, jen když je upřesnění druh („Kost (hrad)“ → „Kost“);
    jinak celé („Kostel svatého Mikuláše (Staré Město)“)."""
    m = re.match(r"^(.*?)\s*\(([^)]*)\)$", titul)
    if m and (m.group(2) in _DRUHY or m.group(2) in ("hrad", "zámek", "zřícenina")):
        return m.group(1)
    return titul


_precteno = {"ts": 0.0, "db": None, "tituly": set()}


def _cetl(db: str, titul: str) -> bool:
    """Má Hans tento článek v četbě nebo studiu? Názvy z deníku se převedou na titul
    lokální Wikipedie („Hrad Kost“ → „Kost (hrad)“); mezipaměť 1 h."""
    import sqlite3
    try:
        if _precteno["db"] != db or time.time() - _precteno["ts"] > 3600:
            from scripts.kolac_podnety import _CTENI
            from scripts import hans_wiki_offline as wo
            c = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=3)
            try:
                syrove = {t for (t,) in c.execute(
                    "SELECT DISTINCT title FROM diary WHERE event_type IN (%s)"
                    % ",".join("?" * len(_CTENI)), _CTENI).fetchall() if t}
            finally:
                c.close()
            tit = set(syrove)
            for t in syrove:
                r = wo._cil(t)
                if r:
                    tit.add(r[1])
            _precteno.update(ts=time.time(), db=db, tituly=tit)
        return titul in _precteno["tituly"]
    except Exception:
        return False


# ── HANS_MISTA_MAPA_V1 (3. 11.) — mapa s očíslovanými body z dlaždic OpenStreetMap ──
# Pár map denně podmínky OSM dovolují (vlastní User-Agent, uvedení zdroje, žádné hromadné
# stahování). Dlaždice ani obrázek se neukládají natrvalo (mapa jde do dočasného souboru,
# volající ho po odeslání smaže).
_DLAZDICE = "https://tile.openstreetmap.org/%d/%d/%d.png"
_dlazdice_ram: dict = {}


def _px(lat: float, lon: float, z: int):
    n = 256 * 2 ** z
    x = (lon + 180.0) / 360.0 * n
    y = (1 - math.log(math.tan(math.radians(lat)) + 1 / math.cos(math.radians(lat))) / math.pi) / 2 * n
    return x, y


def _dlazdice(z: int, x: int, y: int):
    from io import BytesIO
    from PIL import Image
    import requests
    from scripts.hans_facts import USER_AGENT
    k = (z, x, y)
    if k not in _dlazdice_ram:
        r = requests.get(_DLAZDICE % k, headers={"User-Agent": USER_AGENT}, timeout=15)
        r.raise_for_status()
        if len(_dlazdice_ram) > 300:
            _dlazdice_ram.clear()
        _dlazdice_ram[k] = r.content
    return Image.open(BytesIO(_dlazdice_ram[k])).convert("RGB")


def mapa(lat: float, lon: float, body: list, cesta: str, w: int = 900, h: int = 700) -> bool:
    """Obrázek mapy: střed = poloha (modrý bod), `body` = [(lat, lon)] očíslované 1…N."""
    from PIL import Image, ImageDraw, ImageFont
    vse = [(lat, lon)] + [b for b in body if b]
    z = 15
    while z > 7:                                   # největší přiblížení, kam se vše vejde
        xs, ys = zip(*(_px(a, b, z) for a, b in vse))
        if max(xs) - min(xs) < w - 120 and max(ys) - min(ys) < h - 120:
            break
        z -= 1
    xs, ys = zip(*(_px(a, b, z) for a, b in vse))
    cx, cy = (max(xs) + min(xs)) / 2, (max(ys) + min(ys)) / 2
    x0, y0 = cx - w / 2, cy - h / 2
    img = Image.new("RGB", (w, h), (235, 235, 235))
    for tx in range(int(x0 // 256), int((x0 + w) // 256) + 1):
        for ty in range(int(y0 // 256), int((y0 + h) // 256) + 1):
            if 0 <= ty < 2 ** z:
                img.paste(_dlazdice(z, tx % 2 ** z, ty), (int(tx * 256 - x0), int(ty * 256 - y0)))
    d = ImageDraw.Draw(img)
    try:
        f = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 17)
        fm = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 12)
    except Exception:
        f = fm = ImageFont.load_default()
    px, py = _px(lat, lon, z)
    px, py = px - x0, py - y0
    d.ellipse((px - 9, py - 9, px + 9, py + 9), fill=(30, 110, 230), outline="white", width=3)
    for i, b in enumerate(body, 1):
        if not b:
            continue
        bx, by = _px(b[0], b[1], z)
        bx, by = bx - x0, by - y0
        d.ellipse((bx - 14, by - 14, bx + 14, by + 14), fill=(200, 30, 40), outline="white", width=2)
        tw = d.textlength(str(i), font=f)
        d.text((bx - tw / 2, by - 11), str(i), fill="white", font=f)
    pozn = "© přispěvatelé OpenStreetMap"
    tw = d.textlength(pozn, font=fm)
    d.rectangle((w - tw - 10, h - 20, w, h), fill=(255, 255, 255))
    d.text((w - tw - 5, h - 17), pozn, fill=(60, 60, 60), font=fm)
    img.save(cesta, "PNG")
    return True


def mapa_k_mistum(lat: float, lon: float, cesta: str, r_km: float = 15.0) -> bool:
    """Mapa k poslednímu seznamu míst (z mezipaměti `mista`). Nikdy nehází."""
    try:
        mm = mista(lat, lon, r_km)
        if not any(x.get("poloha") for x in mm):
            return False
        return mapa(lat, lon, [x.get("poloha") for x in mm], cesta)
    except Exception as e:
        _log.warning("HANS_MISTA_MAPA_V1: mapa se nepovedla: %s", str(e)[:120])
        return False


def uloz_podnety(db: str, mm: list, strop: int = 3) -> int:
    """HANS_MISTA_PODNETY_V1 (3. 11., uživatel) — místa ze seznamu, o kterých Hans nic
    nečetl, do zásoby Koláčových podnětů (Koláč se zeptá, Hans si článek dohledá).
    Téma bez osoby a polohy — z dialogu se nesmí dát vyčíst, kde kdo byl."""
    import sqlite3
    n = 0
    try:
        from scripts import kolac_podnety as kp
        con = sqlite3.connect(db, timeout=10)
        try:
            uz = {(r[0] or "").lower() for r in con.execute("SELECT nazev FROM kolac_podnety")}
            for x in mm:
                if n >= strop:
                    break
                t = x["titul"]
                if t.lower() in uz or _cetl(db, t) or kp.v_pameti(db, _jmeno(t)):
                    continue
                con.execute("INSERT INTO kolac_podnety (ts, tema, nazev, typ, stav) "
                            "VALUES (?,?,?,?,'novy')",
                            (time.time(), "zvenku: zajímavá místa v okolí", t, "misto"))
                n += 1
            con.commit()
        finally:
            con.close()
    except Exception as e:
        _log.warning("HANS_MISTA_PODNETY_V1: %s", str(e)[:120])
    if n:
        _log.info("HANS_MISTA_PODNETY_V1: +%d podnětů z míst v okolí", n)
    return n


def _km(k: float) -> str:
    return ("%.1f km" % k).replace(".", ",") if k < 10 else "%d km" % round(k)


def zprava(lat: float, lon: float, osoba: str = "", vlastni: bool = True,
           db: str = "data/hans_diary.db", r_km: float = 15.0) -> str:
    """Hotová odpověď pro Matrix."""
    if osoba:
        posledni[osoba] = (lat, lon, time.time())
    try:
        mm = mista(lat, lon, r_km)
    except Exception as e:
        _log.warning("HANS_MISTA_V1: Wikidata nedostupná: %s", str(e)[:120])
        return "Polohu mám, ale seznam míst se mi teď nepodařilo zjistit (Wikidata neodpovídají)."
    if not mm:
        return "V okolí %s jsem nenašel nic, o čem bych vám uměl povědět." % _km(r_km)
    uvod = ("Zajímavá místa %s (do %s):" % ("ve vašem okolí" if vlastni else "kolem toho bodu",
                                            _km(r_km)))
    radky = [uvod]
    for i, x in enumerate(mm, 1):
        cetl = _cetl(db, x["titul"])
        jm = _jmeno(x["titul"])
        radky.append("%d. %s — %s, %s%s%s" % (
            i, jm, x["druh"], _km(x["km"]),
            (". " + x["veta"]) if x["veta"] else "",
            " (o tomhle jsem si četl)" if cetl else ""))
    if osoba and osoba != "test":
        uloz_podnety(db, mm)
    return "\n".join(radky)
