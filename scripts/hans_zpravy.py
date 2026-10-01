#!/usr/bin/env python3
"""HANS_ZPRAVY_SBER_V1 (30. 9. 2026) — sběr titulků zpráv z více zdrojů.

Krok 1 nápadu „ověřování hlavní zprávy z více zdrojů“ (data/NAPADY.md, 📰):
zatím JEN SBĚR, bez LLM. Každou hodinu (systemd timer `hans-zpravy.timer`)
stáhne RSS ~11 českých a zahraničních zdrojů a u každého titulku eviduje,
kolikrát byl při sběru vidět a kolikrát byl mezi prvními `SPICKA` položkami
kanálu („na špici“). Z toho půjde později vybrat zprávu, která se držela
nejdéle. Vlastní databáze `data/hans_zpravy.db` — nesdílí zámek s deníkem.

⚠️ „Pozice“ je pořadí v RSS kanálu. U většiny kanálů je to čas zveřejnění,
ne redakční důležitost (výjimka: ČT24 „výběr redakce“) — proto „na špici“
měří spíš ČERSTVOST × VYTRVALOST; po týdnu sběru posoudit s uživatelem.

Použití:
    python3 scripts/hans_zpravy.py sber       # jeden sběr (volá timer)
    python3 scripts/hans_zpravy.py prehled    # co se sebralo
"""
from __future__ import annotations

import html
import logging
import os
import re
import sqlite3
import sys
import time
import unicodedata
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "data", "hans_zpravy.db")
_log = logging.getLogger("hans_zpravy")

SPICKA = 5            # prvních N položek kanálu = „na špici“
UCHOVAT_DNI = 45      # starší titulky (podle posledního výskytu) se mažou

# (id, název, jazyk, url) — dostupnost ověřena z Pi 30. 9. (curl).
# ❌ Reuters 401, AP 403 — agentury jen nepřímo přes ostatní zdroje.
ZDROJE = [
    ("ct24",       "ČT24",           "cs", "https://ct24.ceskatelevize.cz/rss/hlavni-zpravy"),
    ("irozhlas",   "iROZHLAS",       "cs", "https://www.irozhlas.cz/rss/irozhlas"),
    ("seznam",     "Seznam Zprávy",  "cs", "https://www.seznamzpravy.cz/rss"),
    ("novinky",    "Novinky",        "cs", "https://www.novinky.cz/rss"),
    ("idnes",      "iDNES",          "cs", "https://servis.idnes.cz/rss.aspx?c=zpravodaj"),
    ("bbc",        "BBC World",      "en", "https://feeds.bbci.co.uk/news/world/rss.xml"),
    ("guardian",   "Guardian World", "en", "https://www.theguardian.com/world/rss"),
    ("npr",        "NPR",            "en", "https://feeds.npr.org/1001/rss.xml"),
    ("aljazeera",  "Al Jazeera",     "en", "https://www.aljazeera.com/xml/rss/all.xml"),
    ("dw",         "DW",             "en", "https://rss.dw.com/rdf/rss-en-top"),
    ("tagesschau", "tagesschau",     "de", "https://www.tagesschau.de/xml/rss2/"),
    # Le Monde vyměněn 30. 9.: články blokuje JS kontrolou („Client Challenge“),
    # celý text nešel stáhnout ani jednou. RFI: celé články 3,5–4 tis. zn.
    ("rfi",        "RFI",            "fr", "https://www.rfi.fr/fr/rss"),
]


# HANS_ZPRAVY_GOOGLE_V1 (30. 9.) — HLAVNÍ ZPRÁVY podle důležitosti. Kanály médií
# řadí podle času (na 1. místě je vždy nejčerstvější) → „na špici“ z nich měří
# čerstvost, ne význam (námitka uživatele 30. 9.). Google News řadí podle
# důležitosti a každá položka je SHLUK téže události z ~5 médií. Kanály médií
# zůstávají: odkazy Google jsou zašifrovaná přesměrování, skutečný článek pro
# pozdější ověřování se dohledá v kanálu média podle titulku.
GOOGLE = [
    ("gn_cz",    "Google News CZ",    "cs", "https://news.google.com/rss?hl=cs&gl=CZ&ceid=CZ:cs"),
    ("gn_svet",  "Google News svět",  "en", "https://news.google.com/rss/topics/CAAqJggKIiBDQkFTRWdvSUwyMHZNRGx1YlY4U0FtVnVHZ0pWVXlnQVAB?hl=en-US&gl=US&ceid=US:en"),
    ("gn_us",    "Google News USA",   "en", "https://news.google.com/rss?hl=en-US&gl=US&ceid=US:en"),
    ("gn_gb",    "Google News UK",    "en", "https://news.google.com/rss?hl=en-GB&gl=GB&ceid=GB:en"),
    ("gn_de",    "Google News DE",    "de", "https://news.google.com/rss?hl=de&gl=DE&ceid=DE:de"),
    ("gn_fr",    "Google News FR",    "fr", "https://news.google.com/rss?hl=fr&gl=FR&ceid=FR:fr"),
]
SHODA_OKNO_H = 48     # příběh se páruje s tím, co bylo vidět za posledních N hodin


# HANS_ZPRAVY_BULVAR_V1 (30. 9., pokyn uživatele „odfiltruj bulvár český
# i zahraniční“) — Google „hlavní zprávy“ míchají zprávy s bulvárem a sportem.
# Kategorie příběhu: (1) média ve shluku — ≥ polovina bulvárních/sportovních;
# (2) jinak slova v titulku (4 jazyky). Změřeno na 288 příbězích: bulvár 10,
# sport 18; odstraněny falešné poplachy „people“ (EN lidé), „Golf“ (DE záliv),
# „promi…“ (promises). Sporný zůstal 1 (dopravní zpráva jen z The Sun).
def _f(s): return re.sub(r"[^a-z0-9]+"," ","".join(c for c in unicodedata.normalize("NFD",(s or "").lower()) if unicodedata.category(c)!="Mn")).strip()
BULVAR={_f(x) for x in """super.cz|blesk.cz|blesk|extra.cz|expres.cz|aha!|ahaonline.cz|kafe.cz|zena-in.cz|prima zeny|hradecka drbna|krimi plzen|medium.cz|
the sun|daily mail|mail online|dailymail.co.uk|mirror|daily mirror|daily express|express.co.uk|daily star|new york post|page six|tmz|people.com|people|
e! online|us weekly|hello!|ok!|good housekeeping|hypebeast.com|variety|the hollywood reporter|deadline|entertainment weekly|
bild|bunte|gala.de|promiflash|rtl.de|in.de|
gala|voici|closer|public|parismatch.com|paris match|madame figaro|marie france, magazine feminin|vanity fair|tele-loisirs|purepeople|yahoo life france""".replace("\n","").split("|")}
SPORT={_f(x) for x in """sport.cz|isport.cz|nhl.cz|livesport|footballclub.cz|efotbal|ct sport|sport.aktualne.cz|
espn|espn.com|mlb.com|nba.com|nfl.com|yahoo sports|sports.yahoo.com|bleacher report|sky sports|talksport|manchester city fc|the athletic|cbs sports|
kicker|speedweek.com|sportschau.de|sport.de|sport bild|sport1|spox|fussballdaten.de|transfermarkt|
l equipe|rmc sport|footmercato.net|sports.orange.fr|eurosport|so foot""".replace("\n","").split("|")}
def kategorie(clenove):
    z=[_f(x[1]) for x in clenove if x[1]]
    if not z: return "zprava"
    b=sum(1 for x in z if x in BULVAR); s=sum(1 for x in z if x in SPORT)
    if b*2>=len(z) and b>=1: return "bulvar"
    if s*2>=len(z) and s>=1: return "sport"
    return "zprava"
KW_BULVAR=re.compile(r"\b(herec|herecka|zpevak|zpevacka|modelk\w*|celebrit\w*|kraska|zadost o ruku|vdava|svatb\w*|rozvod\w*|tehotn\w*|milenec|milenk\w*|"
 r"fashion week|na premiere (noveho )?filmu|proměn\w*|promen\w*|"
 r"actor|actress|singer|celebrity|pregnant|engaged|divorce|red carpet|kardashian|royal family|prince harry|meghan|"
 r"schauspieler\w*|sangerin|sanger|prominente\w*|hochzeit|schwanger|trennung|"
 r"acteur|actrice|chanteu\w*|mariage|enceinte|divorc\w*)\b")
KW_SPORT=re.compile(r"\b(nhl|nba|nfl|mlb|fotbal\w*|hokej\w*|tenis\w*|liga|lize|lig[uy]|zapas\w*|olympi\w*|reprezentac\w*|kvalifikac\w*|"
 r"premier league|champions league|world cup|playoffs?|touchdown|grand slam|"
 r"bundesliga|dfb|trainer|landerspiel|"
 r"ligue 1|mercato|ballon d or|tour de france)\b")
def kategorie2(titulek, clenove):
    k=kategorie(clenove)
    if k!="zprava": return k,"zdroje"
    t=_f(titulek)
    if KW_BULVAR.search(t): return "bulvar","slovo"
    if KW_SPORT.search(t): return "sport","slovo"
    return "zprava",""


def _db(path: str = DB) -> sqlite3.Connection:
    c = sqlite3.connect(path, timeout=10)
    c.execute("""CREATE TABLE IF NOT EXISTS titulky (
        id INTEGER PRIMARY KEY, zdroj TEXT NOT NULL, jazyk TEXT, url TEXT NOT NULL,
        titulek TEXT, perex TEXT, publikovano REAL,
        prvni_ts REAL, posledni_ts REAL, videno INTEGER DEFAULT 0,
        na_spici INTEGER DEFAULT 0, nejlepsi_pozice INTEGER,
        prvni_na_spici_ts REAL, posledni_na_spici_ts REAL,
        UNIQUE(zdroj, url))""")
    c.execute("""CREATE TABLE IF NOT EXISTS sbery (
        id INTEGER PRIMARY KEY, ts REAL, zdroj TEXT, ok INTEGER,
        pocet INTEGER, chyba TEXT, trvani_s REAL)""")
    c.execute("""CREATE TABLE IF NOT EXISTS pribehy (
        id INTEGER PRIMARY KEY, vydani TEXT NOT NULL, jazyk TEXT,
        titulek TEXT, zdroj_hlavni TEXT, clenove TEXT, pocet_zdroju INTEGER,
        publikovano REAL, prvni_ts REAL, posledni_ts REAL, videno INTEGER DEFAULT 0,
        na_spici INTEGER DEFAULT 0, nejlepsi_pozice INTEGER,
        prvni_na_spici_ts REAL, posledni_na_spici_ts REAL)""")
    c.execute("CREATE INDEX IF NOT EXISTS ix_pr_vyd ON pribehy(vydani, posledni_ts)")
    if "kategorie" not in {r[1] for r in c.execute("PRAGMA table_info(pribehy)")}:
        c.execute("ALTER TABLE pribehy ADD COLUMN kategorie TEXT")
    c.execute("CREATE INDEX IF NOT EXISTS ix_tit_posl ON titulky(posledni_ts)")
    c.execute("CREATE INDEX IF NOT EXISTS ix_sbery_ts ON sbery(ts)")
    return c


def _text(s: str) -> str:
    s = re.sub(r"<[^>]+>", " ", s or "")
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def _cas(s: str):
    if not s:
        return None
    try:
        return parsedate_to_datetime(s).timestamp()
    except Exception:
        pass
    try:
        from datetime import datetime
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def parse_feed(xml_bytes: bytes) -> list:
    """RSS 2.0, RDF (RSS 1.0) i Atom → [{titulek, url, perex, publikovano}]
    v pořadí kanálu. Jmenné prostory se ignorují (porovnává se lokální jméno)."""
    root = ET.fromstring(xml_bytes)

    def loc(el):
        return el.tag.rsplit("}", 1)[-1].lower()

    polozky = [el for el in root.iter() if loc(el) in ("item", "entry")]
    out = []
    for it in polozky:
        d = {}
        for ch in it:
            n = loc(ch)
            if n == "title":
                d["titulek"] = _text(ch.text or "")
            elif n == "link":
                d.setdefault("url", (ch.get("href") or ch.text or "").strip())
            elif n in ("description", "summary", "encoded") and not d.get("perex"):
                d["perex"] = _text(ch.text or "")[:600]
            elif n in ("pubdate", "date", "published", "updated") and not d.get("publikovano"):
                d["publikovano"] = _cas((ch.text or "").strip())
        if d.get("titulek") and d.get("url"):
            out.append(d)
    return out


def _norm_tit(s: str) -> str:
    import unicodedata as _ud
    s = "".join(ch for ch in _ud.normalize("NFD", (s or "").lower())
                if _ud.category(ch) != "Mn")
    return re.sub(r"\W+", " ", s).strip()


def parse_google(xml_bytes: bytes) -> list:
    """Google News RSS → [{titulek, zdroj_hlavni, clenove:[[titulek, zdroj]], publikovano}]
    v pořadí důležitosti. Členové shluku jsou v `description` jako <li><a>…</a> <font>zdroj</font>."""
    root = ET.fromstring(xml_bytes)
    out = []
    for it in root.iter("item"):
        tit = _text(it.findtext("title") or "")
        desc = html.unescape(it.findtext("description") or "")
        clenove = [[_text(a), _text(z)] for a, z in re.findall(
            r"<a [^>]*>(.*?)</a>.*?<font[^>]*>(.*?)</font>", desc, re.S)]
        zdroj = ""
        m = re.match(r"^(.*)\s+-\s+([^-]+)$", tit)
        if m:
            tit, zdroj = m.group(1).strip(), m.group(2).strip()
        if not clenove:
            clenove = [[tit, zdroj]]
        if tit:
            out.append({"titulek": tit, "zdroj_hlavni": zdroj, "clenove": clenove,
                        "publikovano": _cas((it.findtext("pubDate") or "").strip())})
    return out


def _sber_google(c, ted: float) -> dict:
    import json as _json
    souhrn = {}
    for vid, _naz, jaz, url in GOOGLE:
        t0 = time.time()
        try:
            polozky = parse_google(_stahni(url))
            kandidati = []
            for pid, cl in c.execute(
                    "SELECT id, clenove FROM pribehy WHERE vydani=? AND posledni_ts>=?",
                    (vid, ted - SHODA_OKNO_H * 3600)):
                kandidati.append((pid, {_norm_tit(x[0]) for x in _json.loads(cl or "[]")}))
            pouzite = set()
            zahozeno = 0
            for i, p in enumerate(polozky, 1):
                spicka = i <= SPICKA
                # HANS_ZPRAVY_BULVAR_V1 — bulvár a sport se NEUKLÁDAJÍ (pokyn uživatele
                # 30. 9.: „odfiltrovat přímo a zahodit, ať to zbytečně neplní paměť“).
                if kategorie2(p["titulek"], p["clenove"])[0] != "zprava":
                    zahozeno += 1
                    continue
                klic = {_norm_tit(x[0]) for x in p["clenove"]} | {_norm_tit(p["titulek"])}
                shoda = None
                for pid, kk in kandidati:
                    if pid not in pouzite and kk & klic:
                        shoda = pid
                        break
                cl = _json.dumps(p["clenove"], ensure_ascii=False)
                if shoda is None:
                    cur = c.execute(
                        """INSERT INTO pribehy (vydani, jazyk, titulek, zdroj_hlavni, clenove,
                           pocet_zdroju, publikovano, prvni_ts, posledni_ts, videno, na_spici,
                           nejlepsi_pozice, prvni_na_spici_ts, posledni_na_spici_ts, kategorie)
                           VALUES (?,?,?,?,?,?,?,?,?,1,?,?,?,?,?)""",
                        (vid, jaz, p["titulek"], p["zdroj_hlavni"], cl, len(p["clenove"]),
                         p["publikovano"], ted, ted, 1 if spicka else 0, i,
                         ted if spicka else None, ted if spicka else None,
                         kategorie2(p["titulek"], p["clenove"])[0]))
                    pouzite.add(cur.lastrowid)
                else:
                    pouzite.add(shoda)
                    # členy slučuj (shluk se v čase mění), titulek = aktuální vedoucí
                    stare = _json.loads(c.execute("SELECT clenove FROM pribehy WHERE id=?",
                                                  (shoda,)).fetchone()[0] or "[]")
                    zname = {_norm_tit(x[0]) for x in stare}
                    slouceno = stare + [x for x in p["clenove"] if _norm_tit(x[0]) not in zname]
                    if kategorie2(p["titulek"], slouceno)[0] != "zprava":
                        c.execute("DELETE FROM pribehy WHERE id=?", (shoda,))
                        zahozeno += 1
                        continue
                    c.execute(
                        """UPDATE pribehy SET titulek=?, zdroj_hlavni=?, clenove=?, kategorie=?,
                           pocet_zdroju=?, posledni_ts=?, videno=videno+1,
                           na_spici=na_spici+?, nejlepsi_pozice=MIN(COALESCE(nejlepsi_pozice,999), ?),
                           prvni_na_spici_ts=COALESCE(prvni_na_spici_ts, ?),
                           posledni_na_spici_ts=COALESCE(?, posledni_na_spici_ts)
                           WHERE id=?""",
                        (p["titulek"], p["zdroj_hlavni"], _json.dumps(slouceno[:40], ensure_ascii=False),
                         kategorie2(p["titulek"], slouceno)[0],
                         len({x[1] for x in slouceno}), ted, 1 if spicka else 0, i,
                         ted if spicka else None, ted if spicka else None, shoda))
            c.execute("INSERT INTO sbery (ts, zdroj, ok, pocet, chyba, trvani_s) VALUES (?,?,?,?,?,?)",
                      (ted, vid, 1, len(polozky), None, round(time.time() - t0, 1)))
            souhrn[vid] = "%d (bulvár/sport zahozeno %d)" % (len(polozky) - zahozeno, zahozeno)
        except Exception as e:
            c.execute("INSERT INTO sbery (ts, zdroj, ok, pocet, chyba, trvani_s) VALUES (?,?,?,?,?,?)",
                      (ted, vid, 0, 0, str(e)[:200], round(time.time() - t0, 1)))
            souhrn[vid] = "chyba: %s" % str(e)[:80]
        c.commit()
    return souhrn


def _stahni(url: str) -> bytes:
    import requests
    r = requests.get(url, timeout=20, headers={
        "User-Agent": "Mozilla/5.0 (X11; Linux aarch64) HansZpravy/1.0"})
    r.raise_for_status()
    return r.content


def sber(path: str = DB) -> dict:
    """Jeden sběr ze všech zdrojů. Selhání zdroje nezastaví ostatní."""
    c = _db(path)
    ted = time.time()
    souhrn = {}
    for zid, _naz, jaz, url in ZDROJE:
        t0 = time.time()
        try:
            polozky = parse_feed(_stahni(url))
            for i, p in enumerate(polozky, 1):
                spicka = i <= SPICKA
                c.execute(
                    """INSERT INTO titulky (zdroj, jazyk, url, titulek, perex, publikovano,
                       prvni_ts, posledni_ts, videno, na_spici, nejlepsi_pozice,
                       prvni_na_spici_ts, posledni_na_spici_ts)
                       VALUES (?,?,?,?,?,?,?,?,1,?,?,?,?)
                       ON CONFLICT(zdroj, url) DO UPDATE SET
                         titulek=excluded.titulek,
                         perex=COALESCE(excluded.perex, perex),
                         posledni_ts=excluded.posledni_ts,
                         videno=videno+1,
                         na_spici=na_spici+excluded.na_spici,
                         nejlepsi_pozice=MIN(COALESCE(nejlepsi_pozice, 999), excluded.nejlepsi_pozice),
                         prvni_na_spici_ts=COALESCE(prvni_na_spici_ts, excluded.prvni_na_spici_ts),
                         posledni_na_spici_ts=COALESCE(excluded.posledni_na_spici_ts, posledni_na_spici_ts)""",
                    (zid, jaz, p["url"], p["titulek"], p.get("perex"), p.get("publikovano"),
                     ted, ted, 1 if spicka else 0, i,
                     ted if spicka else None, ted if spicka else None))
            c.execute("INSERT INTO sbery (ts, zdroj, ok, pocet, chyba, trvani_s) VALUES (?,?,?,?,?,?)",
                      (ted, zid, 1, len(polozky), None, round(time.time() - t0, 1)))
            souhrn[zid] = len(polozky)
        except Exception as e:
            c.execute("INSERT INTO sbery (ts, zdroj, ok, pocet, chyba, trvani_s) VALUES (?,?,?,?,?,?)",
                      (ted, zid, 0, 0, str(e)[:200], round(time.time() - t0, 1)))
            souhrn[zid] = "chyba: %s" % str(e)[:80]
        c.commit()
    souhrn.update(_sber_google(c, ted))
    c.execute("DELETE FROM titulky WHERE posledni_ts < ?", (ted - UCHOVAT_DNI * 86400,))
    c.execute("DELETE FROM pribehy WHERE posledni_ts < ?", (ted - UCHOVAT_DNI * 86400,))
    c.execute("DELETE FROM sbery WHERE ts < ?", (ted - UCHOVAT_DNI * 86400,))
    c.commit()
    c.close()
    return souhrn


def prehled(path: str = DB, hodin: float = 48.0, limit: int = 40,
            vse: bool = False) -> dict:
    """Pro dashboard: stav zdrojů, nejdéle na špici, nejnovější. Jen čtení."""
    if not os.path.exists(path):
        return {"zdroje": [], "spicka": [], "nove": [], "sberu": 0}
    c = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=5)
    c.row_factory = sqlite3.Row
    od = time.time() - hodin * 3600
    nazvy = {z[0]: (z[1], z[2]) for z in ZDROJE + GOOGLE}
    zdroje = []
    for zid, (naz, jaz) in nazvy.items():
        posl = c.execute("SELECT ts, ok, pocet, chyba FROM sbery WHERE zdroj=? "
                         "ORDER BY ts DESC LIMIT 1", (zid,)).fetchone()
        n = c.execute("SELECT count(*) FROM %s WHERE %s=? AND prvni_ts>=?"
                      % (("pribehy", "vydani") if zid.startswith("gn_") else ("titulky", "zdroj")),
                      (zid, od)).fetchone()[0]
        ok24 = c.execute("SELECT count(*), sum(ok) FROM sbery WHERE zdroj=? AND ts>=?",
                         (zid, time.time() - 86400)).fetchone()
        zdroje.append({"id": zid, "nazev": naz, "jazyk": jaz, "novych": n,
                       "posledni_ts": posl["ts"] if posl else None,
                       "ok": bool(posl["ok"]) if posl else None,
                       "chyba": posl["chyba"] if posl else None,
                       "sberu_24h": ok24[0] or 0, "ok_24h": ok24[1] or 0})
    spicka = [dict(r) for r in c.execute(
        "SELECT zdroj, jazyk, titulek, perex, url, na_spici, videno, nejlepsi_pozice, "
        "prvni_ts, posledni_ts, prvni_na_spici_ts, posledni_na_spici_ts FROM titulky "
        "WHERE posledni_ts>=? AND na_spici>0 ORDER BY na_spici DESC, nejlepsi_pozice "
        "LIMIT ?", (od, limit))]
    nove = [dict(r) for r in c.execute(
        "SELECT zdroj, jazyk, titulek, url, prvni_ts, publikovano FROM titulky "
        "ORDER BY prvni_ts DESC, nejlepsi_pozice LIMIT ?", (limit,))]
    import json as _json
    hlavni = []
    try:
        for r in c.execute(
                "SELECT vydani, jazyk, titulek, zdroj_hlavni, clenove, pocet_zdroju, na_spici, "
                "videno, nejlepsi_pozice, prvni_ts, posledni_ts, prvni_na_spici_ts, "
                "posledni_na_spici_ts, kategorie FROM pribehy WHERE posledni_ts>=? "
                "AND (? OR COALESCE(kategorie,'zprava')='zprava') "
                "ORDER BY na_spici DESC, videno DESC, nejlepsi_pozice LIMIT ?",
                (od, 1 if vse else 0, limit * 8)):
            d = dict(r)
            d["clenove"] = _json.loads(d["clenove"] or "[]")
            d["vydani_nazev"] = nazvy.get(d["vydani"], (d["vydani"],))[0]
            hlavni.append(d)
    except sqlite3.OperationalError:
        pass
    try:
        skryto = dict(c.execute("SELECT kategorie, count(*) FROM pribehy WHERE posledni_ts>=? "
                                "AND kategorie IN ('bulvar','sport') GROUP BY kategorie", (od,)).fetchall())
    except sqlite3.OperationalError:
        skryto = {}
    sberu = c.execute("SELECT count(DISTINCT ts) FROM sbery").fetchone()[0]
    c.close()
    for r in spicka + nove:
        r["zdroj_nazev"] = nazvy.get(r["zdroj"], (r["zdroj"],))[0]
    return {"zdroje": zdroje, "spicka": spicka, "nove": nove, "sberu": sberu, "hlavni": hlavni,
            "skryto": skryto,
            "spicka_n": SPICKA, "hodin": hodin}


def zahod_bulvar(path: str = DB) -> int:
    """Jednorázově smaže už uložené příběhy s kategorií bulvár/sport."""
    dopocitej_kategorie(path)
    c = _db(path)
    n = c.execute("DELETE FROM pribehy WHERE kategorie IN ('bulvar','sport')").rowcount
    c.commit()
    c.execute("VACUUM")
    c.close()
    return n


def dopocitej_kategorie(path: str = DB) -> dict:
    import json as _json
    c = _db(path)
    n = {}
    for pid, tit, cl in c.execute("SELECT id, titulek, clenove FROM pribehy").fetchall():
        k = kategorie2(tit, _json.loads(cl or "[]"))[0]
        c.execute("UPDATE pribehy SET kategorie=? WHERE id=?", (k, pid))
        n[k] = n.get(k, 0) + 1
    c.commit()
    c.close()
    return n


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    cmd = sys.argv[1] if len(sys.argv) > 1 else "prehled"
    if cmd == "zahod":
        print("smazáno:", zahod_bulvar())
    elif cmd == "kategorie":
        print(dopocitej_kategorie())
    elif cmd == "sber":
        for k, v in sber().items():
            print("%-11s %s" % (k, v))
    else:
        p = prehled()
        print("sběrů:", p["sberu"])
        for z in p["zdroje"]:
            print("%-14s %s ok=%s nových=%s %s" % (z["nazev"], z["jazyk"], z["ok"],
                                                  z["novych"], z["chyba"] or ""))
        for r in p["spicka"][:15]:
            print("%3d× %-14s %s" % (r["na_spici"], r["zdroj_nazev"], r["titulek"][:90]))
