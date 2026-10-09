"""HANS_LETAKY_V1 (9. 10. 2026) — slevy z akčních letáků podle ručního seznamu domácnosti.

Jednou týdně (středa 18:00, první den platnosti letáků) stáhne akční nabídku
obchodů, najde v ní položky z hlídaného seznamu a pošle přehled na Matrix.
Bez modelu: párování dělají pravidla, takže běží i při herním módu.

Zdroje (průzkum 4. 10. a měření 9. 10., `data/NAPADY.md` → SLEVOVE_LETAKY):
  • Kaufland — přehled týdne, 1 dotaz, data v `window.SSR['…'] = {…}`;
  • Lidl — kampaně z úvodní stránky (`/c/<slug>/a<id>`), produkty v `data-grid-data`;
    jen potraviny a drogerie (mezi kampaněmi jsou i hračky a sport);
  • ostatní obchody — hledání na AkcniCeny.cz, 1 dotaz na položku (crawl-delay 1 s).
⛔ Kupi.cz se nepoužívá (podmínky zakazují přebírání obsahu).

Párování (změřeno na seznamu uživatele: 13 správně, 1 navíc, 0 chybí):
  • slova položky se hledají po kmenech na začátku slova názvu;
  • JEDNOSLOVNÁ položka je podstatné jméno — „máslo“ nechytí „máslový croissant“
    ani „bagetku s máslem“;
  • položka může mít NÁHRADNÍ VÝRAZY (`hovězí na polévku: hovězí kližka`), protože
    letáky zboží jmenují jinak; pak se hledají jen ony.
"""
from __future__ import annotations

import html as _html
import json
import logging
import os
import re
import sqlite3
import time
import unicodedata
from datetime import datetime as _dt

_log = logging.getLogger(__name__)

DB = "data/hans_letaky.db"
OBCHODY = ["Lidl", "Globus", "Kaufland", "Albert", "Penny", "Billa"]
_UA = {"User-Agent": "Mozilla/5.0 (X11; Linux aarch64) domaci-asistent/1.0 "
                     "(soukrome pouziti, 1x tydne)", "Accept-Language": "cs"}
_BALAST = {"na", "do", "se", "bez", "pro", "chlazene", "chlazena", "chlazeny",
           "cerstve", "cerstva", "cerstvy"}
_NE = re.compile(r"pro kocky|pro psy|prichut|zmrzlin")


def _cfg(config: dict) -> dict:
    return (config or {}).get("letaky", {}) or {}


def _fold(s) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", str(s or "").lower())
                   if not unicodedata.combining(c))


def _kmen(w: str) -> str:
    return w[:max(4, len(w) - 2)] if len(w) > 5 else w[:max(3, len(w) - 1)]


# ── párování ────────────────────────────────────────────────────────────────

def sedi(vzor: str, nazev: str) -> bool:
    """Odpovídá název z letáku hledanému výrazu? Čistá funkce (regresní sada)."""
    T = _fold(nazev)
    if _NE.search(T):
        return False
    slova = [w for w in re.findall(r"[a-z0-9]+", _fold(vzor))
             if w not in _BALAST and len(w) >= 3]
    if not slova:
        return False
    tok = re.findall(r"[a-z0-9]+", T)
    for w in slova:
        k = _kmen(w)
        if len(slova) == 1:
            # podstatné jméno: tvar slova, ne přídavné jméno ani 7. pád; a ne
            # hotové jídlo nebo směs („kuře s rýží“, „kuře s kaší a zeleninou“)
            if not any(t.startswith(k) and len(t) <= len(w) + 1
                       and not re.search(r"ov(y|a|e|ym|ou|eho)$|em$", t)
                       and not (i + 1 < len(tok) and tok[i + 1] in ("s", "se"))
                       for i, t in enumerate(tok)):
                return False
        elif not any(t.startswith(k) for t in tok):
            return False
    return True


def polozka_sedi(polozka: str, aliasy, nazev: str) -> bool:
    """Položka seznamu × název z letáku; s náhradními výrazy se hledají jen ony."""
    vzory = [a for a in (aliasy or []) if str(a).strip()] or [polozka]
    return any(sedi(v, nazev) for v in vzory)


# ── seznam hlídaných položek ────────────────────────────────────────────────

def _db(path: str = DB) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    c = sqlite3.connect(path, timeout=10.0)
    c.execute("CREATE TABLE IF NOT EXISTS seznam (id INTEGER PRIMARY KEY AUTOINCREMENT, "
              "polozka TEXT NOT NULL, aliasy TEXT, pridal TEXT, ts REAL)")
    c.execute("CREATE TABLE IF NOT EXISTS behy (id INTEGER PRIMARY KEY AUTOINCREMENT, "
              "ts REAL, tyden TEXT, odeslano INTEGER, nalezeno INTEGER, text TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS nabidka (ts REAL, data TEXT)")
    # HANS_LETAKY_HISTORIE_V1 — ceny HLÍDANÝCH položek, jeden řádek na den stažení
    c.execute("CREATE TABLE IF NOT EXISTS ceny (id INTEGER PRIMARY KEY AUTOINCREMENT, "
              "datum TEXT, tyden TEXT, polozka TEXT, obchod TEXT, nazev TEXT, cena REAL, "
              "puvodni REAL, puvodni_zdroj TEXT, sleva INTEGER, plati_od TEXT, plati_do TEXT, "
              "jednotka TEXT, za_kg REAL, UNIQUE(datum, polozka, obchod, nazev, cena))")
    # HANS_LETAKY_CSU_V1 — měsíční průměrné ceny ČSÚ (obvyklá cena pro srovnání)
    c.execute("CREATE TABLE IF NOT EXISTS csu (mesic TEXT, druh TEXT, cena REAL, za_kg REAL, "
              "UNIQUE(mesic, druh))")
    c.execute("CREATE TABLE IF NOT EXISTS stav (klic TEXT PRIMARY KEY, hodnota TEXT)")
    return c


def seznam(path: str = DB) -> list:
    """[(položka, [náhradní výrazy])] v pořadí přidání."""
    c = _db(path)
    try:
        return [(r[0], json.loads(r[1] or "[]"))
                for r in c.execute("SELECT polozka, aliasy FROM seznam ORDER BY id")]
    finally:
        c.close()


def pridej(polozka: str, aliasy=None, kdo: str = "", path: str = DB) -> str:
    """Přidá položku (nebo jí přepíše náhradní výrazy). Vrací 'pridano' | 'upraveno' | ''."""
    polozka = re.sub(r"\s+", " ", str(polozka or "")).strip(" .,;:")
    aliasy = [re.sub(r"\s+", " ", str(a)).strip(" .,;:") for a in (aliasy or [])]
    aliasy = [a for a in aliasy if a]
    if len(_fold(polozka)) < 3:
        return ""
    c = _db(path)
    try:
        for rid, p in c.execute("SELECT id, polozka FROM seznam").fetchall():
            if _fold(p) == _fold(polozka):
                c.execute("UPDATE seznam SET aliasy=? WHERE id=?",
                          (json.dumps(aliasy, ensure_ascii=False), rid))
                c.commit()
                return "upraveno"
        c.execute("INSERT INTO seznam (polozka, aliasy, pridal, ts) VALUES (?,?,?,?)",
                  (polozka, json.dumps(aliasy, ensure_ascii=False), kdo or "", time.time()))
        c.commit()
        return "pridano"
    finally:
        c.close()


def odeber(polozka: str, path: str = DB) -> bool:
    c = _db(path)
    try:
        for rid, p in c.execute("SELECT id, polozka FROM seznam").fetchall():
            if _fold(p) == _fold(str(polozka or "").strip(" .,;:")):
                c.execute("DELETE FROM seznam WHERE id=?", (rid,))
                c.commit()
                return True
        return False
    finally:
        c.close()


# ── čtení stránek obchodů (bez sítě — vstupem je stažený text) ──────────────

def _datum(s) -> str:
    """'2026-10-07' nebo '7.10.2026' → 'RRRR-MM-DD'; jinak ''."""
    s = str(s or "").strip()
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return m.group(0)
    m = re.match(r"(\d{1,2})\.\s?(\d{1,2})\.\s?(\d{4})", s)
    return "%s-%02d-%02d" % (m.group(3), int(m.group(2)), int(m.group(1))) if m else ""


def _cislo(x) -> float | None:
    """'39,90' / 39.9 → 39.9; nula, prázdno a nesmysl → None."""
    try:
        v = float(str(x).replace("\u00a0", "").replace(" ", "").replace(",", "."))
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def _http(u) -> str:
    """Jen úplná adresa https — nic jiného se do stránky jako obrázek nepustí."""
    u = str(u or "").strip()
    return u if u.startswith("https://") and '"' not in u and "<" not in u else ""


def kaufland_z_html(text: str) -> list:
    dec, out, videno = json.JSONDecoder(), [], set()

    def projdi(x):
        if isinstance(x, dict):
            if "offerId" in x and x.get("title"):
                if x["offerId"] not in videno:
                    videno.add(x["offerId"])
                    nazev = ("%s %s" % (x.get("title") or "", x.get("subtitle") or "")
                             ).replace("\n", " ").strip()
                    try:
                        cena = float(x.get("price"))
                    except (TypeError, ValueError):
                        cena = None
                    out.append({"obchod": "Kaufland", "nazev": re.sub(r"\s+", " ", nazev),
                                "cena": cena, "sleva": int(x.get("discount") or 0),
                                "od": _datum(x.get("dateFrom")), "do": _datum(x.get("dateTo")),
                                "jednotka": str(x.get("unit") or "")[:40],
                                "obrazek": _http(x.get("listImage")), "url": "",
                                "puvodni": _cislo(x.get("formattedOldPrice"))})
            for v in x.values():
                projdi(v)
        elif isinstance(x, list):
            for v in x:
                projdi(v)
    for m in re.finditer(r"window\.SSR\['[^']+'\]\s*=\s*", text or ""):
        try:
            obj, _ = dec.raw_decode(text, m.end())
        except ValueError:
            continue
        projdi(obj)
    return out


def lidl_kampane(text: str) -> list:
    return sorted(set(re.findall(r'href="(/c/[a-z0-9-]+/a\d+)', text or "")))


def lidl_z_html(text: str) -> list:
    out = []
    for g in re.findall(r'data-grid-data="([^"]+)"', text or ""):
        try:
            d = json.loads(_html.unescape(g))
        except ValueError:
            continue
        for o in (d if isinstance(d, list) else [d]):
            if not isinstance(o, dict):
                continue
            kat = str(((o.get("keyfacts") or {}).get("wonCategoryPrimary")) or "")
            if not (o.get("category") == "Food" or "Potraviny" in kat or "Drogerie" in kat):
                continue                      # hračky, sport, dílna…
            p = o.get("price") or {}
            try:
                cena = float(p.get("price"))
            except (TypeError, ValueError):
                cena = None
            sl = re.search(r"(\d{1,2})\s*%", str((p.get("discount") or {}).get("discountText") or ""))
            od = do = ""
            for b in (((o.get("stockAvailability") or {}).get("badgeInfoV2")) or []):
                try:
                    if b.get("validFrom"):
                        od = time.strftime("%Y-%m-%d", time.localtime(int(b["validFrom"])))
                    if b.get("validUntil"):
                        do = time.strftime("%Y-%m-%d", time.localtime(int(b["validUntil"])))
                except (TypeError, ValueError):
                    pass
            nazev = str(o.get("fullTitle") or o.get("title") or "").strip()
            if nazev:
                out.append({"obchod": "Lidl", "nazev": nazev, "cena": cena,
                            "sleva": int(sl.group(1)) if sl else 0, "od": od, "do": do,
                            "jednotka": str((p.get("basePrice") or {}).get("text") or "")[:40],
                            "obrazek": _http(o.get("image")),
                            "url": ("https://www.lidl.cz" + o["canonicalUrl"])
                            if str(o.get("canonicalUrl") or "").startswith("/") else "",
                            "puvodni": _cislo(p.get("oldPrice"))})
    return out


def _obchod(jmeno: str, obchody: list) -> str:
    f = _fold(jmeno)
    for o in obchody:
        if f.startswith(_fold(o)):
            return o
    return ""


def akcniceny_z_html(text: str, obchody: list | None = None) -> list:
    """Stránka hledání AkcniCeny.cz → nabídky obchodů ze seznamu (schema.org Product/Offer)."""
    obchody = obchody or OBCHODY
    out = []
    for prod in (text or "").split('itemtype="http://schema.org/Product"')[1:]:
        m = re.search(r'itemprop="name"\s+content="([^"]+)"', prod)
        if not m:
            continue
        nazev = _html.unescape(m.group(1)).strip()
        mi = re.search(r'<img src="([^"]+)"[^>]*itemprop="image"', prod)
        mu = re.search(r'itemprop="url"\s+content="(/akce/[^"]+)"', prod)
        obrazek = _http(mi.group(1)) if mi else ""
        odkaz = ("https://www.akcniceny.cz" + mu.group(1)) if mu else ""
        for off in prod.split('itemtype="http://schema.org/Offer"')[1:]:
            ms = re.search(r'itemprop="name"[^>]*content="([^"]+)"', off)
            mp = re.search(r'itemprop="price"[^>]*content="([\d.]+)"', off)
            if not ms or not mp:
                continue
            ob = _obchod(_html.unescape(ms.group(1)), obchody)
            if not ob:
                continue
            holy = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", off))
            sl = re.search(r"-\s?(\d{1,2})\s?%", holy)
            do = re.search(r"Plat[ií] do:?\s*(\d{1,2}\.\s?\d{1,2}\.\s?\d{4})", holy)
            od = re.search(r"Plat[ií] od:?\s*(\d{1,2}\.\s?\d{1,2}\.\s?\d{4})", holy)
            out.append({"obchod": ob, "nazev": nazev, "cena": float(mp.group(1)),
                        "sleva": int(sl.group(1)) if sl else 0,
                        "od": _datum(od.group(1)) if od else "",
                        "do": _datum(do.group(1)) if do else "", "jednotka": "",
                        "obrazek": obrazek, "url": odkaz})
    return out


# ── stažení ─────────────────────────────────────────────────────────────────

def _get(url: str, params=None, pauza: float = 1.5) -> str:
    import requests
    r = requests.get(url, params=params, headers=_UA, timeout=25)
    time.sleep(pauza)
    if r.status_code != 200:
        raise RuntimeError("HTTP %s" % r.status_code)
    return r.text


def stahni(config: dict, polozky: list) -> tuple:
    """(nabídky, [obchody/zdroje, které se nepodařilo načíst]). Každý zdroj zvlášť —
    výpadek jednoho nezahodí ostatní."""
    k = _cfg(config)
    obchody = list(k.get("obchody") or OBCHODY)
    nab, chyby = [], []
    if "Kaufland" in obchody:
        try:
            x = kaufland_z_html(_get("https://prodejny.kaufland.cz/nabidka/aktualni-tyden/prehled.html"))
            if not x:
                raise RuntimeError("prázdná nabídka")
            nab += x
        except Exception as e:
            chyby.append("Kaufland")
            _log.warning("HANS_LETAKY_V1: Kaufland selhal: %s", e)
    if "Lidl" in obchody:
        try:
            kamp = lidl_kampane(_get("https://www.lidl.cz/"))
            if not kamp:
                raise RuntimeError("žádné kampaně na úvodní stránce")
            n0, videno = len(nab), set()
            for kp in kamp[:int(k.get("lidl_max_kampani", 45))]:
                try:
                    for o in lidl_z_html(_get("https://www.lidl.cz" + kp)):
                        kl = (o["nazev"], o["cena"])
                        if kl not in videno:
                            videno.add(kl)
                            nab.append(o)
                except Exception as e:
                    _log.debug("HANS_LETAKY_V1: Lidl %s: %s", kp, e)
            if len(nab) == n0:
                raise RuntimeError("žádné produkty v kampaních")
        except Exception as e:
            chyby.append("Lidl")
            _log.warning("HANS_LETAKY_V1: Lidl selhal: %s", e)
    ostatni = [o for o in obchody if o not in ("Kaufland", "Lidl")]
    if ostatni:
        spatne = 0
        dotazy = []
        for pol, al in polozky:
            for v in ((al or [])[:3] or [pol]):
                d = " ".join(w for w in re.findall(r"[a-z0-9]+", _fold(v)) if w not in _BALAST)
                if d and d not in dotazy:
                    dotazy.append(d)
        for d in dotazy:
            try:
                nab += akcniceny_z_html(_get("https://www.akcniceny.cz/hledej/", {"s": d}), ostatni)
            except Exception as e:
                spatne += 1
                _log.warning("HANS_LETAKY_V1: AkcniCeny „%s“ selhalo: %s", d, e)
        if dotazy and spatne == len(dotazy):
            chyby += ostatni
    return nab, chyby


# ── HANS_LETAKY_HISTORIE_V1 (9. 10.) — ukládání cen pro pozdější vývoj ───────
# Ukládají se JEN ceny hlídaných položek (rozhodnutí uživatele 9. 10.: „neukládej
# cenu všeho, ale jen vybraných“), jeden řádek na DEN stažení → časová řada
# s datem a týdnem, ze které jde poznat sezóna. Vedle akční ceny i PŮVODNÍ cena
# před slevou, když ji zdroj dává (Kaufland, Lidl) nebo jde dopočítat ze slevy.
_VAHA = re.compile(r"(\d+(?:[.,]\d+)?)\s*(kg|g|l|ml)\b", re.I)


def za_kg(nazev: str, jednotka: str, cena) -> float | None:
    """Cena přepočtená na 1 kg nebo 1 l (kvůli srovnání různých balení); None, když
    balení z textu nejde poznat. „cena za 1 kg“ → cena sama."""
    if cena is None:
        return None
    j = _fold(jednotka)
    if "za 1 kg" in j or "za 1 l" in j:
        return round(float(cena), 2)
    for zdroj in (jednotka, nazev):
        m = _VAHA.search(str(zdroj or "").replace("\u00a0", " "))
        if not m:
            continue
        mnozstvi = float(m.group(1).replace(",", "."))
        j2 = m.group(2).lower()
        kg = mnozstvi / 1000.0 if j2 in ("g", "ml") else mnozstvi
        if 0.02 <= kg <= 30:
            return round(float(cena) / kg, 2)
    return None


def puvodni_cena(o: dict) -> tuple:
    """(cena před slevou, odkud je): 'obchod' = uvedl ji obchod, 'ze slevy' = dopočtena
    z procent slevy (zaokrouhleně), (None, '') = neznámá."""
    p = o.get("puvodni")
    if p and o.get("cena") and p > o["cena"]:
        return round(float(p), 2), "obchod"
    s = int(o.get("sleva") or 0)
    if o.get("cena") and 0 < s < 95:
        return round(float(o["cena"]) / (1 - s / 100.0), 1), "ze slevy"
    return None, ""


def uloz_historii(nalezy: dict, path: str = DB, dnes: str | None = None) -> int:
    """Zapíše dnešní ceny hlídaných položek (`najdi` → {položka: [nabídky]}).
    Vrací počet nových řádků; opakované stažení týž den nic nezdvojí."""
    dnes = dnes or time.strftime("%Y-%m-%d")
    tyden = "%d-%02d" % _dt.strptime(dnes, "%Y-%m-%d").isocalendar()[:2]
    c = _db(path)
    nove = 0
    try:
        for pol, hity in (nalezy or {}).items():
            for o in hity:
                if not o.get("nazev") or o.get("cena") is None:
                    continue
                pu, zdroj = puvodni_cena(o)
                cur = c.execute(
                    "INSERT OR IGNORE INTO ceny (datum, tyden, polozka, obchod, nazev, cena, "
                    "puvodni, puvodni_zdroj, sleva, plati_od, plati_do, jednotka, za_kg) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (dnes, tyden, pol, o.get("obchod") or "", o["nazev"], float(o["cena"]),
                     pu, zdroj, int(o.get("sleva") or 0), o.get("od") or "", o.get("do") or "",
                     o.get("jednotka") or "",
                     za_kg(o["nazev"], o.get("jednotka") or "", o["cena"])))
                nove += cur.rowcount
        c.commit()
    finally:
        c.close()
    return nove


def vyvoj(polozka: str, path: str = DB) -> list:
    """Časová řada cen hlídané položky od nejstarší: [{datum, tyden, obchod, nazev,
    cena, puvodni, puvodni_zdroj, sleva, za_kg, plati_od, plati_do}]."""
    klice = ("datum", "tyden", "obchod", "nazev", "cena", "puvodni", "puvodni_zdroj",
             "sleva", "za_kg", "plati_od", "plati_do")
    c = _db(path)
    try:
        rows = c.execute("SELECT %s FROM ceny WHERE polozka=? ORDER BY datum, obchod, cena"
                         % ", ".join(klice), (polozka,)).fetchall()
    finally:
        c.close()
    return [dict(zip(klice, r)) for r in rows]


# ── HANS_LETAKY_CSU_V1 (9. 10.) — obvyklá cena podle ČSÚ ─────────────────────
# Letáky znají jen akční ceny a e-shopy mají čtení zakázané podmínkami (Rohlík,
# Billa — průzkum 9. 10.). ČSÚ vydává jako otevřená data MĚSÍČNÍ průměrné
# spotřebitelské ceny ~100 druhů zboží za celé Česko (sada CEN0101N „od 2026“,
# jeden dotaz, zpoždění ~6 týdnů). Slouží jako „obvyklá cena“: akce se proti ní
# dá posoudit. Je to průměr přes obchody a značky, ne cena konkrétního výrobku.
CSU_URL = "https://data.csu.gov.cz/api/dotaz/v1/data/sady/CEN0101N?format=CSV"
_CSU_MESICE = {"leden": 1, "unor": 2, "brezen": 3, "duben": 4, "kveten": 5, "cerven": 6,
               "cervenec": 7, "srpen": 8, "zari": 9, "rijen": 10, "listopad": 11, "prosinec": 12}
_MESIC_2P = ["ledna", "února", "března", "dubna", "května", "června", "července", "srpna",
             "září", "října", "listopadu", "prosince"]
# (vzor nad položkou a jejími náhradními výrazy bez diakritiky, začátek názvu druhu u ČSÚ);
# pořadí rozhoduje — užší před širším („kuřecí prsa“ před „kuře“).
_CSU_DRUHY = [
    (r"\bmaslo\b", "Máslo ["), (r"\blucin", "Lučina"), (r"\bkureci prs", "Kuřecí prsní řízky"),
    (r"\bkureci steh", "Kuřecí stehna"), (r"\bkruti prs", "Krůtí prsní řízky"),
    (r"\bkure\b|\bkurata\b", "Kuřata kuchaná celá"),
    (r"hovezi.*(klizk|predni|polevk)", "Hovězí maso přední bez kosti"),
    (r"hovezi.*svickov", "Hovězí svíčková pravá"), (r"hovezi.*zadni", "Hovězí maso zadní bez kosti"),
    (r"veprov\w* krkovic", "Vepřová krkovice"), (r"veprov\w* kyt", "Vepřová kýta bez kosti"),
    (r"veprov\w* plec", "Vepřová plec"), (r"veprov\w* buc|\bbucek", "Vepřový bůček"),
    (r"veprov\w* pecen", "Vepřová pečeně ["),
    (r"\beidam", "Eidamská cihla"), (r"\bgoud", "Gouda"), (r"\bhermelin", "Hermelín"),
    (r"\bniva\b", "Niva"), (r"\btvaroh", "Tvaroh měkký konzumní"),
    (r"\bvejce\b|\bvajic", "Vejce slepičí čerstvá"),
    (r"mleko.*trvanliv.*plnotuc|mleko.*plnotuc.*trvanliv", "Mléko plnotučné trvanlivé"),
    (r"mleko.*trvanliv", "Mléko polotučné trvanlivé"), (r"\bmleko\b", "Mléko polotučné pasterované"),
    (r"\bbrambor", "Konzumní brambory"), (r"\bcukr.*moucka", "Cukr moučkový"),
    (r"\bcukr\b", "Cukr krystalový"), (r"mouka.*hrub", "Pšeničná mouka hrubá"),
    (r"\bmouka\b", "Pšeničná mouka hladká"), (r"\bryze\b", "Rýže loupaná dlouhozrnná"),
    (r"\bbanan", "Banány žluté"), (r"\bjablk", "Jablka konzumní"), (r"\bpomeranc", "Pomeranče"),
    (r"\bcitron", "Citrony"), (r"\brajcat|rajsk", "Rajská jablka červená kulatá"),
    (r"\bpaprik[ay]\b", "Papriky"), (r"okurk\w* salat|salatov\w* okurk", "Okurky salátové"),
    (r"\bcibul", "Cibule suchá"), (r"\bcesnek", "Česnek suchý"), (r"\bmrkev|\bmrkv", "Mrkev"),
    (r"kava.*zrnk", "Káva zrnková pražená"), (r"kava.*rozpust|instantni kav", "Káva rozpustná"),
    (r"\bolej\b", "Olej rostlinný"), (r"\bsunk[au]\b", "Šunka vepřová"),
    (r"\bparky\b|\bparek", "Párky"), (r"\bchleb|\bchleba", "Chléb konzumní kmínový"),
]


def csu_druh(polozka: str, aliasy=None) -> str:
    """Začátek názvu druhu zboží u ČSÚ pro položku seznamu, nebo '' (ČSÚ ji nesleduje)."""
    text = " | ".join(_fold(x) for x in [polozka] + list(aliasy or []))
    for vzor, druh in _CSU_DRUHY:
        if re.search(vzor, text):
            return druh
    return ""


def csu_z_csv(text: str) -> list:
    """CSV sady ČSÚ → [(měsíc 'RRRR-MM', druh, cena, cena za 1 kg / 1 l nebo None)]."""
    import csv
    import io
    out = []
    for x in csv.DictReader(io.StringIO(text or "")):
        m = re.match(r"(\S+)\s+(\d{4})", x.get("Měsíce") or "")
        druh = (x.get("Druh zboží") or "").strip()
        try:
            cena = float(x.get("Hodnota") or "")
            mes = "%s-%02d" % (m.group(2), _CSU_MESICE[_fold(m.group(1))])
        except (AttributeError, KeyError, ValueError):
            continue
        j = re.search(r"\[([^\]]+)\]", druh)
        out.append((mes, druh, cena, za_kg(j.group(1) if j else "", "", cena) if j else None))
    return out


def csu_obnov(path: str = DB, max_stari_dni: float = 6.0) -> int:
    """Stáhne aktuální měsíční ceny ČSÚ, je-li poslední stažení starší. Vrací počet řádků
    (0 = nebylo potřeba nebo se nepovedlo). Selhání nevadí — srovnání prostě chybí."""
    c = _db(path)
    try:
        r = c.execute("SELECT hodnota FROM stav WHERE klic='csu_ts'").fetchone()
    finally:
        c.close()
    if r and time.time() - float(r[0] or 0) < max_stari_dni * 86400.0:
        return 0
    try:
        radky = csu_z_csv(_get(CSU_URL, pauza=0.0))
    except Exception as e:
        _log.warning("HANS_LETAKY_CSU_V1: ČSÚ se nepodařilo stáhnout: %s", e)
        return 0
    if not radky:
        return 0
    c = _db(path)
    try:
        c.executemany("INSERT OR REPLACE INTO csu (mesic, druh, cena, za_kg) VALUES (?,?,?,?)", radky)
        c.execute("INSERT OR REPLACE INTO stav (klic, hodnota) VALUES ('csu_ts', ?)",
                  (str(time.time()),))
        c.commit()
    finally:
        c.close()
    _log.info("HANS_LETAKY_CSU_V1: ceny ČSÚ obnoveny (%d řádků, poslední měsíc %s)",
              len(radky), max(x[0] for x in radky))
    return len(radky)


def obvykla(polozka: str, aliasy=None, path: str = DB) -> dict | None:
    """Poslední známá obvyklá cena podle ČSÚ: {druh, mesic, mesic_slovy, za_kg} nebo None."""
    druh = csu_druh(polozka, aliasy)
    if not druh:
        return None
    c = _db(path)
    try:
        r = c.execute("SELECT mesic, druh, za_kg FROM csu WHERE druh LIKE ? AND za_kg IS NOT NULL "
                      "ORDER BY mesic DESC LIMIT 1", (druh + "%",)).fetchone()
    finally:
        c.close()
    if not r:
        return None
    rok, mes = r[0].split("-")
    return {"druh": r[1].split(" [")[0], "mesic": r[0], "za_kg": r[2],
            "mesic_slovy": "%s %s" % (["leden", "únor", "březen", "duben", "květen", "červen",
                                       "červenec", "srpen", "září", "říjen", "listopad",
                                       "prosinec"][int(mes) - 1], rok)}


# ── výběr a zpráva ──────────────────────────────────────────────────────────

def najdi(polozky: list, nabidka: list, obchody: list | None = None,
          dnes: str | None = None) -> dict:
    """{položka: [nabídky]} — jen platné (neskončené), řazeno podle priority obchodu a ceny."""
    obchody = obchody or OBCHODY
    dnes = dnes or time.strftime("%Y-%m-%d")
    out = {}
    for pol, al in polozky:
        hity, videno = [], set()
        for o in nabidka:
            if o.get("do") and o["do"] < dnes:
                continue
            if o.get("obchod") not in obchody or not polozka_sedi(pol, al, o.get("nazev")):
                continue
            kl = (o["obchod"], _fold(o["nazev"]), o.get("cena"))
            if kl in videno:
                continue
            videno.add(kl)
            hity.append(o)
        hity.sort(key=lambda o: (obchody.index(o["obchod"]), o.get("cena") or 9e9))
        out[pol] = hity
    return out


def _kc(c) -> str:
    return ("%.2f" % c).replace(".", ",") + " Kč" if c is not None else "cena neuvedena"


def _den(d: str) -> str:
    try:
        x = _dt.strptime(d, "%Y-%m-%d")
        return "%d. %d." % (x.day, x.month)
    except (TypeError, ValueError):
        return ""


def sestav_zpravu(nalezy: dict, chyby: list | None = None, dnes: str | None = None,
                  na_obchod: int = 3, obvykle: dict | None = None) -> str:
    dnes = dnes or time.strftime("%Y-%m-%d")
    radky, nic = ["🛒 Slevy z vašeho seznamu (%s)" % _den(dnes)], []
    for pol, hity in nalezy.items():
        if not hity:
            nic.append(pol)
            continue
        radky.append("")
        ob_c = (obvykle or {}).get(pol)
        radky.append("%s%s:" % (pol[:1].upper() + pol[1:],
                                (" — obvykle %s/kg (ČSÚ, %s)" % (_kc(ob_c["za_kg"]), ob_c["mesic_slovy"]))
                                if ob_c else ""))
        po_obchodech = {}
        for o in hity:
            po_obchodech.setdefault(o["obchod"], []).append(o)
        for ob, seznam_o in po_obchodech.items():
            kusy = []
            for o in seznam_o[:na_obchod]:
                t = "%s %s" % (o["nazev"], _kc(o.get("cena")))
                dop = []
                if o.get("sleva"):
                    dop.append("−%d %%" % o["sleva"])
                if o.get("jednotka") and "cena za" in o["jednotka"]:
                    dop.append(o["jednotka"].replace("cena za ", "za "))
                if o.get("od") and o["od"] > dnes:
                    dop.append("od %s" % _den(o["od"]))
                elif o.get("do"):
                    dop.append("do %s" % _den(o["do"]))
                kusy.append(t + (" (%s)" % ", ".join(dop) if dop else ""))
            if len(seznam_o) > na_obchod:
                kusy.append("a %d dalších" % (len(seznam_o) - na_obchod))
            radky.append("• %s — %s" % (ob, "; ".join(kusy)))
    if len(radky) == 1:
        radky.append("")
        radky.append("Tento týden není v akci nic z toho, co hlídám.")
    elif nic:
        radky.append("")
        radky.append("V akci teď nejsou: %s." % ", ".join(nic))
    if chyby:
        radky.append("Nepodařilo se načíst: %s." % ", ".join(dict.fromkeys(chyby)))
    return "\n".join(radky)


def prehled(config: dict, path: str = DB, max_stari_h: float = 20.0) -> tuple:
    """(text zprávy, počet položek v akci). Nabídku bere z mezipaměti, je-li čerstvá."""
    polozky = seznam(path)
    if not polozky:
        return ("V seznamu hlídaných slev zatím nic nemám. Přidejte položku: "
                "/sleva přidej máslo"), 0
    k = _cfg(config)
    c = _db(path)
    try:
        r = c.execute("SELECT ts, data FROM nabidka ORDER BY ts DESC LIMIT 1").fetchone()
    finally:
        c.close()
    klic = json.dumps(polozky, ensure_ascii=False, sort_keys=True)
    nab = chyby = None
    _stazeno = False
    if r and time.time() - r[0] < max_stari_h * 3600.0:
        try:
            d = json.loads(r[1])
            if d.get("klic") == klic:
                nab, chyby = d["nabidka"], d.get("chyby") or []
        except ValueError:
            pass
    if nab is None:
        nab, chyby = stahni(config, polozky)
        _stazeno = True
        c = _db(path)
        try:
            c.execute("DELETE FROM nabidka")
            c.execute("INSERT INTO nabidka (ts, data) VALUES (?,?)",
                      (time.time(), json.dumps({"klic": klic, "nabidka": nab, "chyby": chyby},
                                               ensure_ascii=False)))
            c.commit()
        finally:
            c.close()
    nal = najdi(polozky, nab, list(k.get("obchody") or OBCHODY))
    if _stazeno:                               # HANS_LETAKY_HISTORIE_V1
        try:
            _log.info("HANS_LETAKY_HISTORIE_V1: uloženo %d cen hlídaných položek",
                      uloz_historii(nal, path))
        except Exception as e:
            _log.warning("HANS_LETAKY_HISTORIE_V1: uložení cen selhalo: %s", e)
    _log.info("HANS_LETAKY_V1: nabídek %d, položek v akci %d z %d, nenačteno %s",
              len(nab), sum(1 for v in nal.values() if v), len(polozky), chyby or "nic")
    obv = {}
    try:                                       # HANS_LETAKY_CSU_V1
        if _stazeno:
            csu_obnov(path)
        obv = {p: o for p, o in ((p, obvykla(p, a, path)) for p, a in polozky) if o}
    except Exception as e:
        _log.warning("HANS_LETAKY_CSU_V1: obvyklé ceny: %s", e)
    return sestav_zpravu(nal, chyby, obvykle=obv), sum(1 for v in nal.values() if v)


# ── HANS_LETAKY_WEB_V1 (9. 10.) — týdenní výběr pro webovou stránku /slevy ────
_obnova_bezi = False


def web_data(config: dict, path: str = DB) -> dict:
    """Výběr z POSLEDNÍHO stažení (web sám nic nestahuje): položky seznamu s nabídkami
    včetně obrázku. `stazeno` None = ještě nic staženo."""
    polozky = seznam(path)
    c = _db(path)
    try:
        r = c.execute("SELECT ts, data FROM nabidka ORDER BY ts DESC LIMIT 1").fetchone()
    finally:
        c.close()
    out = {"stazeno": None, "polozky": [], "nic": [], "chyby": [], "bezi": _obnova_bezi,
           "seznam": [{"polozka": p, "aliasy": a} for p, a in polozky],
           "obchody": list(_cfg(config).get("obchody") or OBCHODY)}
    if not r:
        return out
    try:
        d = json.loads(r[1])
    except ValueError:
        return out
    out["stazeno"] = r[0]
    out["chyby"] = list(dict.fromkeys(d.get("chyby") or []))
    nal = najdi(polozky, d.get("nabidka") or [], out["obchody"])
    al = dict(polozky)
    for pol, hity in nal.items():
        if not hity:
            out["nic"].append(pol)
            continue
        try:
            obv = obvykla(pol, al.get(pol), path)       # HANS_LETAKY_CSU_V1
        except Exception:
            obv = None
        nab = []
        for o in hity:
            o = dict(o)
            o["za_kg"] = za_kg(o.get("nazev") or "", o.get("jednotka") or "", o.get("cena"))
            o["proti_obvykle"] = (round(100.0 * (o["za_kg"] / obv["za_kg"] - 1.0))
                                  if obv and o["za_kg"] else None)
            nab.append(o)
        out["polozky"].append({"polozka": pol, "nabidky": nab, "obvykle": obv})
    return out


def obnov_async(config: dict, path: str = DB) -> bool:
    """Stáhne nabídku znovu ve vlákně (tlačítko na webu). False = už běží nebo prázdný seznam."""
    global _obnova_bezi
    if _obnova_bezi or not seznam(path):
        return False
    _obnova_bezi = True

    def _prace():
        global _obnova_bezi
        try:
            prehled(config, path, max_stari_h=0.0)
        except Exception as e:
            _log.warning("HANS_LETAKY_WEB_V1: obnova selhala: %s", e)
        finally:
            _obnova_bezi = False
    import threading
    threading.Thread(target=_prace, name="letaky-web", daemon=True).start()
    return True


def ma_cerstvou(path: str = DB, max_stari_h: float = 20.0) -> bool:
    """Je v mezipaměti nabídka stažená pro dnešní seznam? (pak je přehled hned)"""
    c = _db(path)
    try:
        r = c.execute("SELECT ts, data FROM nabidka ORDER BY ts DESC LIMIT 1").fetchone()
    finally:
        c.close()
    if not r or time.time() - r[0] >= max_stari_h * 3600.0:
        return False
    try:
        return json.loads(r[1]).get("klic") == json.dumps(seznam(path), ensure_ascii=False,
                                                           sort_keys=True)
    except ValueError:
        return False


def tick(config: dict, notifier=None, ted: float | None = None, path: str = DB) -> str | None:
    """Zavolat z rutiny. Ve zvolený den po zvolené hodině pošle JEDNOU za týden přehled.
    Vrací odeslaný text, jinak None."""
    k = _cfg(config)
    if not k.get("enabled", True):
        return None
    ted = ted or time.time()
    d = _dt.fromtimestamp(ted)
    hod = int(k.get("hodina", 18))
    if d.weekday() != int(k.get("den", 2)) or not (hod <= d.hour < hod + 4):
        return None
    tyden = "%d-%02d" % d.isocalendar()[:2]
    c = _db(path)
    try:
        hotovo = c.execute("SELECT 1 FROM behy WHERE tyden=? AND odeslano=1", (tyden,)).fetchone()
        pokus = (c.execute("SELECT MAX(ts) FROM behy WHERE tyden=? AND odeslano=0",
                           (tyden,)).fetchone() or [None])[0] or 0
    finally:
        c.close()
    if hotovo or ted - pokus < 1800 or not seznam(path):
        return None
    text, n = prehled(config, path, max_stari_h=3.0)
    ok = True
    if notifier:
        try:
            ok = notifier(text) is not False
        except Exception as e:
            ok = False
            _log.warning("HANS_LETAKY_V1: odeslání selhalo: %s", e)
    c = _db(path)
    try:
        c.execute("INSERT INTO behy (ts, tyden, odeslano, nalezeno, text) VALUES (?,?,?,?,?)",
                  (ted, tyden, 1 if ok else 0, n, text))
        c.commit()
    finally:
        c.close()
    _log.info("HANS_LETAKY_V1: týdenní přehled %s (položek v akci %d)",
              "odeslán" if ok else "NEODESLÁN — zkusím znovu", n)
    return text if ok else None
