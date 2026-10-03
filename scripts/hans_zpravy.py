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
    python3 scripts/hans_zpravy.py udalosti   # spojit tutéž událost napříč vydáními
    python3 scripts/hans_zpravy.py osa <id>   # časová osa události: kdo co kdy napsal
    python3 scripts/hans_zpravy.py snimky     # stáhnout texty nových článků + kontroly změn
    python3 scripts/hans_zpravy.py clanek <titulek_id>   # verze textu článku
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
    _db_udalosti(c)                                  # HANS_ZPRAVY_OSA_V1 — sběr píše historii
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
                    _zapis_historii(c, cur.lastrowid, p, ted)    # HANS_ZPRAVY_OSA_V1
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
                    _zapis_historii(c, shoda, p, ted)            # HANS_ZPRAVY_OSA_V1
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
                # HANS_ZPRAVY_OSA_V1 — redakce titulek článku přepisují (URL i čas
                # zveřejnění zůstávají) → každá verze s časem, kdy se poprvé objevila
                tid = c.execute("SELECT id FROM titulky WHERE zdroj=? AND url=?",
                                (zid, p["url"])).fetchone()[0]
                c.execute("INSERT OR IGNORE INTO titulek_verze VALUES (?,?,?,?,?,0)",
                          (tid, _norm_tit(p["titulek"]), p["titulek"], p.get("perex"), ted))
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


# ── HANS_ZPRAVY_UDALOSTI_V1 (1. 10.) — tatáž událost napříč vydáními a jazyky ──
# Vektor příběhu = bge-m3 titulku + průměr bge-m3 titulků jeho členů (≤ EMBED_CLENU),
# obojí normované a sečtené. Shlukování average-link nad posledními UDALOST_OKNO_H
# hodinami, práh UDALOST_PRAH. Změřeno 1. 10. (backlog ZPRAVY_SPOJENI_01_10):
# samotný titulek nestačí (tatáž událost 0,57), zákaz téhož vydání by událost s více
# úhly rozdělil. Mezi jazyky vychází podobnost systematicky níž (flydubai EN×FR 0,794,
# Pike EN×CZ/FR 0,796) → práh 0,78, ne 0,80: z 20 spojení navíc ~14 táž událost
# (hlavně napříč jazyky), ~4 jen téma, 2 omyl — nespojená cizí verze je pro ověřování
# větší ztráta než šum, který posoudí až další krok. Bez překladu: model je vícejazyčný a překlad
# by vnášel falešné rozdíly (rozhodnutí uživatele 1. 10.). Embedding běží na CPU PC
# (bge-m3-cpu, VRAM nebere); PC spí / herní mód → vektory se dopočtou příště.
EMBED_MODEL = "bge-m3-cpu:latest"
EMBED_CLENU = 8
EMBED_MAX_PRIBEHU = 300   # strop na jeden běh (služba má TimeoutStartSec 300)
UDALOST_PRAH = 0.78
UDALOST_OKNO_H = 48


def _db_udalosti(c: sqlite3.Connection) -> None:
    c.execute("""CREATE TABLE IF NOT EXISTS vektory (
        pribeh_id INTEGER PRIMARY KEY, otisk TEXT, model TEXT, vec BLOB, ts REAL)""")
    c.execute("""CREATE TABLE IF NOT EXISTS udalosti (
        id INTEGER PRIMARY KEY, titulek TEXT, prvni_ts REAL, posledni_ts REAL,
        pocet_pribehu INTEGER, pocet_zdroju INTEGER, vydani TEXT, jazyky TEXT,
        aktualizovano REAL)""")
    c.execute("""CREATE TABLE IF NOT EXISTS udalost_clen (
        pribeh_id INTEGER PRIMARY KEY, udalost_id INTEGER)""")
    c.execute("CREATE INDEX IF NOT EXISTS ix_uc_ud ON udalost_clen(udalost_id)")
    # HANS_ZPRAVY_OSA_V1 — historie příběhů GN (vedoucí titulek se při sběru
    # přepisuje → bez téhle tabulky se ztrácí, co redakce psala dřív) a napojení
    # přímých RSS titulků (mají přesný čas zveřejnění) na události.
    c.execute("""CREATE TABLE IF NOT EXISTS pribeh_vedouci (
        pribeh_id INTEGER, titulek TEXT, zdroj TEXT, publikovano REAL,
        prvni_ts REAL, posledni_ts REAL, odhad INTEGER DEFAULT 0,
        PRIMARY KEY (pribeh_id, titulek))""")
    c.execute("""CREATE TABLE IF NOT EXISTS pribeh_clen (
        pribeh_id INTEGER, klic TEXT, titulek TEXT, zdroj TEXT,
        prvni_ts REAL, odhad INTEGER DEFAULT 0, PRIMARY KEY (pribeh_id, klic))""")
    c.execute("""CREATE TABLE IF NOT EXISTS vektory_tit (
        titulek_id INTEGER PRIMARY KEY, otisk TEXT, vec BLOB, ts REAL)""")
    c.execute("""CREATE TABLE IF NOT EXISTS titulek_udalost (
        titulek_id INTEGER PRIMARY KEY, udalost_id INTEGER, pribeh_id INTEGER,
        podobnost REAL)""")
    c.execute("CREATE INDEX IF NOT EXISTS ix_tu_ud ON titulek_udalost(udalost_id)")
    c.execute("""CREATE TABLE IF NOT EXISTS titulek_verze (
        titulek_id INTEGER, klic TEXT, titulek TEXT, perex TEXT, prvni_ts REAL,
        odhad INTEGER DEFAULT 0, PRIMARY KEY (titulek_id, klic))""")


def _otisk(titulek: str, clenove: list) -> str:
    import hashlib
    s = (titulek or "") + "\n" + "\n".join(x[0] for x in clenove[:EMBED_CLENU] if x)
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:16]


def _embed(texty: list, config: dict):
    """bge-m3 přes Ollamu na PC. None = nyni nejde (herní mód, PC spí, chyba)."""
    import requests
    from scripts.ollama_client import game_mode_on, _resolve_url
    if game_mode_on():
        return None
    try:
        r = requests.post(_resolve_url(None, config) + "/api/embed",
                          json={"model": EMBED_MODEL, "input": texty, "keep_alive": "2m"},
                          timeout=(5, 120))      # spící PC = rychle vzdát
        r.raise_for_status()
        e = r.json().get("embeddings")
        return e if e and len(e) == len(texty) else None
    except Exception as e:
        _log.info("události: embedding nejde (%s) — dopočtu příště", str(e)[:120])
        return None


def dopocitej_vektory(c, config: dict, ted: float, limit: int = EMBED_MAX_PRIBEHU) -> int:
    """Vektor pro příběhy z okna, které ho nemají nebo se jim změnil titulek/členové."""
    import json as _json
    import numpy as np
    n = lambda v: v / (np.linalg.norm(v) or 1.0)
    todo = []
    for pid, tit, cl, ot in c.execute(
            """SELECT p.id, p.titulek, p.clenove, v.otisk FROM pribehy p
               LEFT JOIN vektory v ON v.pribeh_id = p.id
               WHERE p.posledni_ts >= ? ORDER BY p.posledni_ts DESC""",
            (ted - UDALOST_OKNO_H * 3600,)).fetchall():
        clen = _json.loads(cl or "[]")
        o = _otisk(tit, clen)
        if o != ot:
            todo.append((pid, tit, clen, o))
    todo = todo[:limit]
    hotovo = 0
    for i in range(0, len(todo), 8):          # 8 příběhů ≈ 72 textů na dotaz
        davka, texty, rozsah = todo[i:i + 8], [], []
        for pid, tit, clen, o in davka:
            a = len(texty)
            texty.append(tit or "")
            texty += [x[0] for x in clen[:EMBED_CLENU] if x and x[0]]
            rozsah.append((a, len(texty)))
        E = _embed(texty, config)
        if E is None:
            break
        for (pid, tit, clen, o), (a, b) in zip(davka, rozsah):
            t = n(np.array(E[a], dtype=np.float64))
            m = n(np.mean(np.array(E[a + 1:b], dtype=np.float64), axis=0)) if b > a + 1 else t
            c.execute("INSERT OR REPLACE INTO vektory VALUES (?,?,?,?,?)",
                      (pid, o, EMBED_MODEL, n(t + m).astype(np.float32).tobytes(), ted))
            hotovo += 1
        c.commit()
    return hotovo


def shlukuj(c, ted: float) -> dict:
    """Average-link nad příběhy z okna → tabulka událostí. Id události zůstává
    stabilní: shluk převezme id, které měla většina jeho příběhů minule."""
    import json as _json
    from collections import Counter
    import numpy as np
    from scipy.cluster.hierarchy import fcluster, linkage
    from scipy.spatial.distance import squareform
    rows = c.execute(
        """SELECT p.id, p.vydani, p.jazyk, p.titulek, p.pocet_zdroju, p.prvni_ts,
                  p.posledni_ts, v.vec, p.clenove FROM pribehy p JOIN vektory v ON v.pribeh_id = p.id
           WHERE p.posledni_ts >= ?""", (ted - UDALOST_OKNO_H * 3600,)).fetchall()
    if not rows:
        return {"pribehu": 0}
    if len(rows) == 1:
        lab = [1]
    else:
        M = np.vstack([np.frombuffer(r[7], dtype=np.float32) for r in rows]).astype(np.float64)
        D = np.clip(1.0 - M @ M.T, 0.0, 2.0)
        np.fill_diagonal(D, 0.0)
        D = (D + D.T) / 2
        lab = fcluster(linkage(squareform(D, checks=False), method="average"),
                       1.0 - UDALOST_PRAH, criterion="distance")
    skup = {}
    for r, l in zip(rows, lab):
        skup.setdefault(int(l), []).append(r)
    stare = dict(c.execute("SELECT pribeh_id, udalost_id FROM udalost_clen").fetchall())
    # HANS_ZPRAVY_UDALOST_ID_V2 (2. 10.) — id rozdává POČET HLASŮ napříč všemi
    # shluky, ne velikost shluku. Dřív si větší shluk s jediným příběhem, který
    # kdysi patřil k id X, vzal X dřív než shluk, kde X mělo 6 ze 7 příběhů
    # (1. → 2. 10. tak jedna událost přešla z id 3 na 774).
    skupiny = sorted(skup.values(), key=len, reverse=True)
    hlasy = []
    for gi, g in enumerate(skupiny):
        for kand, n in Counter(stare[r[0]] for r in g if r[0] in stare).items():
            hlasy.append((-n, -len(g), gi, kand))
    prideleno, pouzite = {}, set()
    for _n, _l, gi, kand in sorted(hlasy):
        if gi not in prideleno and kand not in pouzite:
            prideleno[gi] = kand
            pouzite.add(kand)
    for gi, g in enumerate(skupiny):
        uid = prideleno.get(gi)
        if uid is None:
            uid = c.execute("INSERT INTO udalosti (aktualizovano) VALUES (?)", (ted,)).lastrowid
        hlavni = max(g, key=lambda r: r[4] or 0)
        c.execute("""UPDATE udalosti SET titulek=?, prvni_ts=?, posledni_ts=?, pocet_pribehu=?,
                     pocet_zdroju=?, vydani=?, jazyky=?, aktualizovano=? WHERE id=?""",
                  (hlavni[3], min(r[5] for r in g), max(r[6] for r in g), len(g),
                   len({x[1] for r in g for x in _json.loads(r[8] or "[]") if len(x) > 1}),
                   _json.dumps(sorted({r[1] for r in g})),
                   _json.dumps(sorted({r[2] or "" for r in g})), ted, uid))
        for r in g:
            c.execute("INSERT OR REPLACE INTO udalost_clen VALUES (?,?)", (r[0], uid))
    c.execute("DELETE FROM udalost_clen WHERE pribeh_id NOT IN (SELECT id FROM pribehy)")
    c.execute("DELETE FROM vektory WHERE pribeh_id NOT IN (SELECT id FROM pribehy)")
    c.execute("DELETE FROM udalosti WHERE id NOT IN (SELECT udalost_id FROM udalost_clen)")
    c.commit()
    vel = sorted((len(g) for g in skup.values()), reverse=True)
    return {"pribehu": len(rows), "udalosti": len(vel),
            "spojenych": sum(1 for v in vel if v > 1), "nejvetsi": vel[:5]}


# ── HANS_ZPRAVY_OSA_V1 (1. 10.) — časová osa události ──
# Zdroje času: (1) přímé RSS titulky — přesné zveřejnění + perex, na událost se
# napojí podobností titulek+perex × vektor příběhu ≥ TITULEK_PRAH (měření 1. 10.:
# ≥0,72 skoro vždy táž zpráva, 0,66–0,72 asi 5 z 8; 34 % titulků ≥0,72);
# (2) vedoucí titulky příběhů GN s časem zveřejnění; (3) první výskyt článku ve
# shluku GN (přesnost = hodina sběru). Před 1. 10. 9 h historie GN neexistovala →
# doplněno z aktuálního stavu s `odhad=1` (čas = první výskyt příběhu).
TITULEK_PRAH = 0.70
TITULEK_PEREX = 300
EMBED_MAX_TITULKU = 400


def _zapis_historii(c, pid: int, p: dict, ted: float) -> None:
    c.execute("""INSERT INTO pribeh_vedouci (pribeh_id, titulek, zdroj, publikovano,
                 prvni_ts, posledni_ts) VALUES (?,?,?,?,?,?)
                 ON CONFLICT(pribeh_id, titulek) DO UPDATE SET posledni_ts=excluded.posledni_ts""",
              (pid, p["titulek"], p["zdroj_hlavni"], p["publikovano"], ted, ted))
    for x in p["clenove"]:
        if x and x[0]:
            c.execute("INSERT OR IGNORE INTO pribeh_clen VALUES (?,?,?,?,?,0)",
                      (pid, _norm_tit(x[0]), x[0], x[1] if len(x) > 1 else "", ted))


def _dopln_historii(c) -> int:
    """Příběhy bez historie (vznikly před HANS_ZPRAVY_OSA_V1) → stav nyni, odhad=1."""
    import json as _json
    n = 0
    for pid, tit, zdr, pub, prvni, posl, cl in c.execute(
            """SELECT id, titulek, zdroj_hlavni, publikovano, prvni_ts, posledni_ts, clenove
               FROM pribehy WHERE id NOT IN (SELECT pribeh_id FROM pribeh_vedouci)""").fetchall():
        c.execute("INSERT OR IGNORE INTO pribeh_vedouci VALUES (?,?,?,?,?,?,1)",
                  (pid, tit, zdr, pub, prvni, posl))
        for x in _json.loads(cl or "[]"):
            if x and x[0]:
                c.execute("INSERT OR IGNORE INTO pribeh_clen VALUES (?,?,?,?,?,1)",
                          (pid, _norm_tit(x[0]), x[0], x[1] if len(x) > 1 else "", prvni))
        n += 1
    for tid, tit, per, prvni in c.execute(
            """SELECT id, titulek, perex, prvni_ts FROM titulky
               WHERE id NOT IN (SELECT titulek_id FROM titulek_verze)""").fetchall():
        c.execute("INSERT OR IGNORE INTO titulek_verze VALUES (?,?,?,?,?,1)",
                  (tid, _norm_tit(tit), tit, per, prvni))
    c.commit()
    return n


def dopocitej_vektory_tit(c, config: dict, ted: float, limit: int = EMBED_MAX_TITULKU) -> int:
    import hashlib
    import numpy as np
    todo = []
    for tid, tit, per, ot in c.execute(
            """SELECT t.id, t.titulek, t.perex, v.otisk FROM titulky t
               LEFT JOIN vektory_tit v ON v.titulek_id = t.id
               WHERE t.posledni_ts >= ? ORDER BY t.posledni_ts DESC""",
            (ted - UDALOST_OKNO_H * 3600,)).fetchall():
        text = (tit or "") + ". " + (per or "")[:TITULEK_PEREX]
        o = hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]
        if o != ot:
            todo.append((tid, text, o))
    todo = todo[:limit]
    hotovo = 0
    for i in range(0, len(todo), 64):
        davka = todo[i:i + 64]
        E = _embed([x[1] for x in davka], config)
        if E is None:
            break
        for (tid, _t, o), e in zip(davka, E):
            v = np.array(e, dtype=np.float64)
            v /= (np.linalg.norm(v) or 1.0)
            c.execute("INSERT OR REPLACE INTO vektory_tit VALUES (?,?,?,?)",
                      (tid, o, v.astype(np.float32).tobytes(), ted))
            hotovo += 1
        c.commit()
    return hotovo


def prirad_titulky(c, ted: float) -> int:
    """RSS titulek → událost nejpodobnějšího příběhu v okně (≥ TITULEK_PRAH)."""
    import numpy as np
    od = ted - UDALOST_OKNO_H * 3600
    P = c.execute("""SELECT p.id, u.udalost_id, v.vec FROM pribehy p
                     JOIN vektory v ON v.pribeh_id = p.id
                     JOIN udalost_clen u ON u.pribeh_id = p.id
                     WHERE p.posledni_ts >= ?""", (od,)).fetchall()
    T = c.execute("""SELECT t.id, v.vec FROM titulky t JOIN vektory_tit v ON v.titulek_id = t.id
                     WHERE t.posledni_ts >= ?""", (od,)).fetchall()
    if not P or not T:
        return 0
    M = np.vstack([np.frombuffer(p[2], dtype=np.float32) for p in P]).astype(np.float64)
    X = np.vstack([np.frombuffer(t[1], dtype=np.float32) for t in T]).astype(np.float64)
    S = X @ M.T
    nej, pod = S.argmax(axis=1), S.max(axis=1)
    n = 0
    for (tid, _v), j, s in zip(T, nej, pod):
        if s >= TITULEK_PRAH:
            c.execute("INSERT OR REPLACE INTO titulek_udalost VALUES (?,?,?,?)",
                      (tid, P[j][1], P[j][0], round(float(s), 3)))
            n += 1
        else:
            c.execute("DELETE FROM titulek_udalost WHERE titulek_id=?", (tid,))
    c.execute("DELETE FROM titulek_udalost WHERE titulek_id NOT IN (SELECT id FROM titulky)")
    c.execute("DELETE FROM vektory_tit WHERE titulek_id NOT IN (SELECT id FROM titulky)")
    c.commit()
    return n


def casova_osa(udalost_id: int, path: str = DB) -> list:
    """Seřazené záznamy události: [{cas, odhad, druh, zdroj, jazyk, vydani, titulek, perex}].
    Táž věta téhož média se uvádí jednou (nejdřív přesný čas, pak nejdřívější)."""
    c = _db(path)
    try:
        zaz = []
        for tit, zdr, pub, prvni, odh, vyd, jaz in c.execute(
                """SELECT h.titulek, h.zdroj, h.publikovano, h.prvni_ts, h.odhad, p.vydani, p.jazyk
                   FROM pribeh_vedouci h JOIN udalost_clen u ON u.pribeh_id = h.pribeh_id
                   JOIN pribehy p ON p.id = h.pribeh_id WHERE u.udalost_id = ?""", (udalost_id,)):
            zaz.append({"cas": pub or prvni, "odhad": bool(odh) and not pub, "druh": "GN vedoucí",
                        "zdroj": zdr, "jazyk": jaz, "vydani": vyd, "titulek": tit, "perex": None})
        for tit, zdr, prvni, odh, vyd, jaz in c.execute(
                """SELECT m.titulek, m.zdroj, m.prvni_ts, m.odhad, p.vydani, p.jazyk
                   FROM pribeh_clen m JOIN udalost_clen u ON u.pribeh_id = m.pribeh_id
                   JOIN pribehy p ON p.id = m.pribeh_id WHERE u.udalost_id = ?""", (udalost_id,)):
            zaz.append({"cas": prvni, "odhad": bool(odh), "druh": "GN článek", "zdroj": zdr,
                        "jazyk": jaz, "vydani": vyd, "titulek": tit, "perex": None})
        for tit, per, vprvni, odh, zdr, jaz, pub, prvni, pod in c.execute(
                """SELECT v.titulek, v.perex, v.prvni_ts, v.odhad, t.zdroj, t.jazyk,
                          t.publikovano, t.prvni_ts, x.podobnost
                   FROM titulek_udalost x JOIN titulky t ON t.id = x.titulek_id
                   JOIN titulek_verze v ON v.titulek_id = t.id
                   WHERE x.udalost_id = ?""", (udalost_id,)):
            # první verze = čas zveřejnění; pozdější = kdy se nový titulek objevil;
            # doplněná zpětně (odhad) = čas zveřejnění, ale titulek může být pozdější
            prvni_verze = odh or (vprvni or 0) <= (prvni or 0) + 60
            zaz.append({"cas": (pub or vprvni) if prvni_verze else vprvni, "odhad": bool(odh),
                        "druh": "RSS", "zdroj": zdr, "jazyk": jaz, "vydani": None,
                        "titulek": tit, "perex": per, "podobnost": pod})
    finally:
        c.close()
    # přednost: přesný čas, pak RSS (čas zveřejnění) před členem GN (hodina sběru)
    zaz.sort(key=lambda z: (z["odhad"], z["druh"] != "RSS", z["cas"] or 0))
    videno, out = set(), []
    for z in zaz:
        k = ((z["zdroj"] or "").lower(), _norm_tit(z["titulek"]))
        if k not in videno:
            videno.add(k)
            out.append(z)
    out.sort(key=lambda z: z["cas"] or 0)
    return out


def udalosti(path: str = DB, config: dict = None) -> dict:
    """Krok po sběru: dopočítat vektory a přeshlukovat okno."""
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    if config is None:
        from scripts.config_io import load
        config = load()
    c = _db(path)
    try:
        _db_udalosti(c)
        ted = time.time()
        nove = dopocitej_vektory(c, config, ted)
        s = shlukuj(c, ted)
        s["novych_vektoru"] = nove
        try:                                           # HANS_ZPRAVY_OSA_V1
            s["historie_doplnena"] = _dopln_historii(c)
            s["novych_vektoru_tit"] = dopocitej_vektory_tit(c, config, ted)
            s["titulku_k_udalosti"] = prirad_titulky(c, ted)
        except Exception as e:
            _log.warning("události: časová osa selhala: %s", e)
        _log.info("události: %s", s)
        return s
    finally:
        c.close()


# ── HANS_ZPRAVY_SNIMKY_V1 (1. 10.) — text článku při prvním výskytu + kontroly změn ──
# Titulek a perex nestačí: redakce články přepisují (titulky prokazatelně, viz
# HANS_ZPRAVY_OSA_V1) a URL časem ukáže jiný text nebo nic. Proto: snímek textu
# hned, jak se článek objeví v RSS, a kontrolní stažení po KONTROLY_H hodinách;
# změněný text = nová verze (tichá úprava je pro ověřování sama informace).
# Ukládá se jen text odstavců (zlib) pro vlastní porovnání, nic se nepublikuje.
# Vynecháno: iDNES (zeď „souhlas s reklamou, nebo předplatné“ — obcházet ji
# podvrženým souhlasem nechceme), Le Monde (paywall, HTTP 402). Zkouška 1. 10.:
# ostatních 11 zdrojů 2–9 tis. znaků na článek.
SNIMKY_VYNECHAT = {"idnes", "lemonde"}
KONTROLY_H = (6, 24)
SNIMKY_ROZPOCET_S = 120       # na jeden běh služby (TimeoutStartSec 300)
SNIMKY_ZDRZENI_S = 0.3


def _db_snimky(c) -> None:
    c.execute("""CREATE TABLE IF NOT EXISTS clanky (
        titulek_id INTEGER, verze INTEGER, stazeno_ts REAL, otisk TEXT, delka INTEGER,
        text BLOB, odhad INTEGER DEFAULT 0, PRIMARY KEY (titulek_id, verze))""")
    try:                                               # HANS_ZPRAVY_SNIMKY_SUM_V1
        c.execute("ALTER TABLE clanky ADD COLUMN sum INTEGER DEFAULT 0")
    except Exception:
        pass
    c.execute("""CREATE TABLE IF NOT EXISTS clanek_stav (
        titulek_id INTEGER PRIMARY KEY, prvni_ts REAL, kontrol INTEGER DEFAULT 0,
        dalsi_ts REAL, verzi INTEGER DEFAULT 0, posledni_otisk TEXT, stav TEXT,
        chyb INTEGER DEFAULT 0, posledni_ts REAL)""")
    c.execute("CREATE INDEX IF NOT EXISTS ix_cs_dalsi ON clanek_stav(dalsi_ts)")


def _vytahni_text(html_text: str) -> str:
    """Odstavce článku: z <article> a <main> ten delší, jinak celá stránka."""
    from bs4 import BeautifulSoup
    s = BeautifulSoup(html_text, "html.parser")
    for el in s(["script", "style", "nav", "footer", "aside", "form", "noscript"]):
        el.decompose()

    def odst(el):
        out = []
        for p in el.find_all("p"):
            x = re.sub(r"\s+", " ", p.get_text(" ", strip=True)).strip()
            if x and (not out or out[-1] != x):
                out.append(x)
        return out
    kand = [odst(el) for el in (s.find("article"), s.find("main")) if el is not None]
    nej = max(kand, key=lambda o: sum(map(len, o)), default=[])
    if not nej:
        nej = odst(s)
    return "\n".join(nej)


# HANS_ZPRAVY_SNIMKY_SUM_V1 (2. 10.) — verze, která se od předchozí liší jen
# odstavci sdílenými s JINÝM článkem téhož zdroje (upoutávky na další články,
# „přihlaste se, chcete-li článek poslouchat“), je šum: uloží se, ale se sum=1.
# Změřeno 2. 10.: iRozhlas 147/181 „změněných“ článků → 39; ostatní zdroje
# skoro beze změny (skutečné redakční úpravy). Celkem 367 → 232.
def _sdilene_odstavce(c, zdroj: str) -> dict:
    """odstavec → množina titulek_id téhož zdroje, v jejichž verzích stojí."""
    import zlib
    out = {}
    for tid, blob in c.execute("""SELECT k.titulek_id, k.text FROM clanky k
                                  JOIN titulky t ON t.id = k.titulek_id WHERE t.zdroj = ?""", (zdroj,)):
        for o in zlib.decompress(blob).decode("utf-8").split("\n"):
            o = o.strip()
            if o:
                out.setdefault(o, set()).add(tid)
    return out


def _jadro(text: str, tid: int, sdilene: dict) -> list:
    """Odstavce textu bez těch, které má i jiný článek téhož zdroje."""
    return [o.strip() for o in (text or "").split("\n")
            if o.strip() and not (sdilene.get(o.strip(), set()) - {tid})]


def _stahni_clanek(url: str):
    """→ (stav, text). stav: ok | nedostupne (paywall/souhlas) | zmizelo | chyba."""
    import requests
    try:
        r = requests.get(url, timeout=(5, 15), headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux aarch64) HansZpravy/1.0"})
    except Exception as e:
        return "chyba", str(e)[:200]
    if r.status_code in (404, 410):
        return "zmizelo", None
    if r.status_code in (401, 402, 403) or "nastaveni-souhlasu" in r.url:
        return "nedostupne", None
    if r.status_code >= 400:
        return "chyba", "HTTP %d" % r.status_code
    r.encoding = r.encoding if r.encoding and r.encoding.lower() != "iso-8859-1" else r.apparent_encoding
    return "ok", _vytahni_text(r.text)


def snimky(c, ted: float, rozpocet_s: float = SNIMKY_ROZPOCET_S) -> dict:
    """Nové články z okna (nejnovější napřed), pak splatné kontroly. Časový rozpočet."""
    import fcntl
    import hashlib
    import zlib
    # timer a ruční běh naráz by stáhly totéž a druhý by to započetl jako kontrolu
    zamek = open(os.path.join(ROOT, "data", ".hans_zpravy_snimky.lock"), "w")
    try:
        fcntl.flock(zamek, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        zamek.close()
        _log.info("snímky článků: běží jiný proces, přeskakuji")
        return {"zamceno": True}
    try:
        return _snimky(c, ted, rozpocet_s)
    finally:
        zamek.close()


def _snimky(c, ted: float, rozpocet_s: float) -> dict:
    import hashlib
    import zlib
    _db_snimky(c)
    t0 = time.time()
    od = ted - UDALOST_OKNO_H * 3600
    for tid, zdr, prvni in c.execute(
            """SELECT id, zdroj, prvni_ts FROM titulky WHERE posledni_ts >= ?
               AND id NOT IN (SELECT titulek_id FROM clanek_stav)""", (od,)).fetchall():
        if zdr in SNIMKY_VYNECHAT:
            c.execute("INSERT INTO clanek_stav (titulek_id, prvni_ts, stav) VALUES (?,?,'vynechano')",
                      (tid, prvni))
    c.commit()
    nove = c.execute("""SELECT t.id, t.url, t.prvni_ts, NULL, t.zdroj FROM titulky t
                        WHERE t.posledni_ts >= ? AND t.id NOT IN (SELECT titulek_id FROM clanek_stav)
                        ORDER BY t.prvni_ts DESC""", (od,)).fetchall()
    splatne = c.execute("""SELECT t.id, t.url, t.prvni_ts, s.kontrol, t.zdroj FROM clanek_stav s
                           JOIN titulky t ON t.id = s.titulek_id
                           WHERE s.stav IN ('ok', 'chyba') AND s.dalsi_ts IS NOT NULL
                             AND s.dalsi_ts <= ? ORDER BY s.dalsi_ts""", (ted,)).fetchall()
    stat = {"novych": 0, "kontrol": 0, "zmen": 0, "sum": 0, "nedostupnych": 0, "chyb": 0, "zbyva": 0}
    sdilene_cache = {}
    for i, (tid, url, prvni, kontrol, zdroj) in enumerate(nove + splatne):
        if time.time() - t0 > rozpocet_s:
            stat["zbyva"] = len(nove) + len(splatne) - i
            break
        nyni = time.time()
        stav, text = _stahni_clanek(url)
        row = c.execute("SELECT kontrol, verzi, posledni_otisk, chyb, prvni_ts FROM clanek_stav "
                        "WHERE titulek_id=?", (tid,)).fetchone()
        if row is None:
            c.execute("INSERT INTO clanek_stav (titulek_id, prvni_ts, kontrol, verzi, chyb) "
                      "VALUES (?,?,0,0,0)", (tid, nyni))
            row = (0, 0, None, 0, nyni)
            stat["novych"] += 1
        else:
            stat["kontrol"] += 1
        k, verzi, otisk0, chyb, prvni_snimek = row
        verzi_pred = verzi
        if stav == "chyba":
            chyb += 1
            stat["chyb"] += 1
            # přechodná chyba: zkusit příští běh, po 3 chybách vzdát
            c.execute("UPDATE clanek_stav SET stav=?, chyb=?, dalsi_ts=?, posledni_ts=? WHERE titulek_id=?",
                      ("chyba" if chyb < 3 else "vzdano", chyb, nyni + 3600 if chyb < 3 else None, nyni, tid))
        elif stav in ("nedostupne", "zmizelo"):
            stat["nedostupnych"] += 1
            c.execute("UPDATE clanek_stav SET stav=?, dalsi_ts=NULL, posledni_ts=? WHERE titulek_id=?",
                      (stav, nyni, tid))
        else:
            norm = re.sub(r"\s+", " ", text or "").strip()
            otisk = hashlib.sha1(norm.encode("utf-8")).hexdigest()[:16]
            if otisk != otisk0:
                verzi += 1
                sum_ = 0
                if verzi > 1:
                    if zdroj not in sdilene_cache:
                        sdilene_cache[zdroj] = _sdilene_odstavce(c, zdroj)
                    pred = c.execute("SELECT text FROM clanky WHERE titulek_id=? ORDER BY verze DESC LIMIT 1",
                                     (tid,)).fetchone()
                    pred = zlib.decompress(pred[0]).decode("utf-8") if pred else ""
                    sd = sdilene_cache[zdroj]
                    sum_ = 1 if _jadro(pred, tid, sd) == _jadro(text, tid, sd) else 0
                    stat["sum" if sum_ else "zmen"] += 1
                # snímek starého článku (objevil se dřív než 2 h před snímkem) není originál
                odhad = 1 if verzi == 1 and (prvni or 0) < nyni - 2 * 3600 else 0
                c.execute("INSERT OR REPLACE INTO clanky (titulek_id, verze, stazeno_ts, otisk, delka, "
                          "text, odhad, sum) VALUES (?,?,?,?,?,?,?,?)",
                          (tid, verzi, nyni, otisk, len(text or ""),
                           zlib.compress((text or "").encode("utf-8")), odhad, sum_))
                if zdroj in sdilene_cache:             # nová verze patří do sdílených
                    for o in (text or "").split("\n"):
                        if o.strip():
                            sdilene_cache[zdroj].setdefault(o.strip(), set()).add(tid)
            # kontroly se počítají od PRVNÍHO snímku; po poslední kontrole konec
            k_novy = k + 1 if verzi_pred > 0 else 0      # první úspěšný snímek = kontrola 0
            dalsi = (prvni_snimek + KONTROLY_H[k_novy] * 3600) if k_novy < len(KONTROLY_H) else None
            c.execute("""UPDATE clanek_stav SET stav='ok', kontrol=?, verzi=?, posledni_otisk=?,
                         dalsi_ts=?, chyb=0, posledni_ts=? WHERE titulek_id=?""",
                      (k_novy, verzi, otisk, dalsi, nyni, tid))
        c.commit()
        time.sleep(SNIMKY_ZDRZENI_S)
    c.execute("DELETE FROM clanky WHERE titulek_id NOT IN (SELECT id FROM titulky)")
    c.execute("DELETE FROM clanek_stav WHERE titulek_id NOT IN (SELECT id FROM titulky)")
    c.commit()
    stat["s"] = round(time.time() - t0)
    _log.info("snímky článků: %s", stat)
    return stat


def clanek_text(titulek_id: int, verze: int = None, path: str = DB):
    """Text článku (výchozí = první verze). None, když snímek není."""
    import zlib
    c = _db(path)
    try:
        _db_snimky(c)
        r = c.execute("SELECT text FROM clanky WHERE titulek_id=? AND (? IS NULL OR verze=?) "
                      "ORDER BY verze LIMIT 1", (titulek_id, verze, verze)).fetchone()
        return zlib.decompress(r[0]).decode("utf-8") if r else None
    finally:
        c.close()


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
        try:                                   # HANS_ZPRAVY_UDALOSTI_V1 — sběr nesmí padnout kvůli tomu
            print("události:", udalosti())
        except Exception as e:
            _log.warning("události selhaly: %s", e)
        try:                                   # HANS_ZPRAVY_SNIMKY_V1
            _c = _db()
            print("snímky:", snimky(_c, time.time()))
            _c.close()
        except Exception as e:
            _log.warning("snímky článků selhaly: %s", e)
    elif cmd == "snimky":
        _c = _db()
        print(snimky(_c, time.time(), float(sys.argv[2]) if len(sys.argv) > 2 else SNIMKY_ROZPOCET_S))
        _c.close()
    elif cmd == "clanek":
        from datetime import datetime as _dt
        _c = _db()
        _db_snimky(_c)
        print(_c.execute("SELECT zdroj, titulek, url FROM titulky WHERE id=?", (int(sys.argv[2]),)).fetchone())
        print(_c.execute("SELECT stav, kontrol, verzi FROM clanek_stav WHERE titulek_id=?", (int(sys.argv[2]),)).fetchone())
        for v, ts, n, odh, sm in _c.execute("SELECT verze, stazeno_ts, delka, odhad, sum FROM clanky "
                                            "WHERE titulek_id=? ORDER BY verze", (int(sys.argv[2]),)):
            print("verze %d  %s%s  %d zn%s" % (v, _dt.fromtimestamp(ts).strftime("%d.%m. %H:%M"),
                                               "~" if odh else " ", n, "  (šum)" if sm else ""))
        _c.close()
        print((clanek_text(int(sys.argv[2])) or "")[:1500])
    elif cmd == "osa":                                 # HANS_ZPRAVY_OSA_V1
        from datetime import datetime as _dt
        for z in casova_osa(int(sys.argv[2])):
            print("%s%s %-10s %-22s %s" % (
                _dt.fromtimestamp(z["cas"] or 0).strftime("%d.%m. %H:%M"),
                "~" if z["odhad"] else " ", z["druh"], ("%s (%s)" % (z["zdroj"], z["jazyk"]))[:22],
                (z["titulek"] or "")[:100]))
    elif cmd == "udalosti":
        print(udalosti())
        c = _db()
        for uid, tit, n, z, vyd in c.execute(
                """SELECT id, titulek, pocet_pribehu, pocet_zdroju, vydani FROM udalosti
                   WHERE pocet_pribehu > 1 ORDER BY pocet_pribehu DESC, pocet_zdroju DESC LIMIT 20"""):
            print("%4d %2d příb. %3d zdr. %-40s %s" % (uid, n, z, vyd, (tit or "")[:80]))
    else:
        p = prehled()
        print("sběrů:", p["sberu"])
        for z in p["zdroje"]:
            print("%-14s %s ok=%s nových=%s %s" % (z["nazev"], z["jazyk"], z["ok"],
                                                  z["novych"], z["chyba"] or ""))
        for r in p["spicka"][:15]:
            print("%3d× %-14s %s" % (r["na_spici"], r["zdroj_nazev"], r["titulek"][:90]))
