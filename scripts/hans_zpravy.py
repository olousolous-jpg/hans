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


# ── HANS_ZPRAVY_CISLA_V1 (3. 10.) — čísla téže události napříč médii ────────
# Krok 3 plánu ověřování, BEZ LLM. Změřeno 3. 10. nad 72 h:
#   • plné texty: 1 397 dvojic, skoro vše šum (cena × zdražení, celkem × podíl)
#     → ⛔ jen TITULKY a PEREXY (nesou hlavní fakt);
#   • titulky s obecným slovem: 267 dvojic, ~40 % skutečných;
#   • jen POČTY níže: 82 dvojic / 9 událostí, většinou skutečné (mrtví 23 × 40).
# Většina rozdílů je VÝVOJ V ČASE, ne rozpor → výstup je ŘADA hodnot s médiem
# a časem; „proč se liší“ (vývoj / jiný rozsah / chyba) je až krok 4.
# ⚠️ Regionální tisk píše o svém městě (3 zatčení × 1 747 celostátně).
# ⚠️ Obecné „lidé“ ZÁMĚRNĚ chybí — míchalo demonstranty, demonstrace i dav.
_CISLO_RE = re.compile(
    r"(?<![\w.,])(\d{1,3}(?:[  .,]\d{3})+|\d+(?:[.,]\d+)?)\s*"
    r"(tis\.|tisíc\w*|thousand|milion\w*|million\w*|Millionen|miliard\w*|billion\w*|Milliarden)?"
    r"\s+([^\W\d_]{3,})", re.U)
VELICINY = [   # (klíč, popis, vzor na slovo ZA číslem bez diakritiky, malými)
    ("mrtvi", "mrtví", r"^(dead|death|kill|mrtv|obet|zemrel|umrel|tote|todes|opfer|morts?$|tue(e|s|es)?$|victim|deces)"),
    ("zraneni", "zranění", r"^(injur|wound|zranen|verletz|bless)"),
    ("zatceni", "zatčení", r"^(arrest|detain|zatcen|zadrzen|festnahm|festgenom|interpell)"),
    ("pohresovani", "pohřešovaní", r"^(missing|pohres|vermisst|disparu)"),
    ("evakuovani", "evakuovaní", r"^(evacu|evaku)"),
    ("cestujici", "cestující", r"^(passeng|cestuj|passagi)"),
    ("policiste", "policisté", r"^(police|polici|polizist)"),
    ("skoly", "školy", r"^(school|skol|schul|ecole|lycee|colleg)"),
    ("domy", "domy", r"^(homes|houses|domu|domy|hauser|maisons|logements)$"),
]
_VEL_RE = [(k, p, re.compile(v)) for k, p, v in VELICINY]
CISLA_ROZDIL = 0.15          # hodnoty se liší, když (max − min) / max > 15 %


def _bez_diakritiky(s: str) -> str:
    return "".join(ch for ch in unicodedata.normalize("NFD", (s or "").lower())
                   if unicodedata.category(ch) != "Mn")


def _cislo_hodnota(n: str, nasobek: str) -> float:
    s = n.replace(" ", " ")
    if re.fullmatch(r"\d{1,3}(?:[ .,]\d{3})+", s):
        v = float(re.sub(r"[ .,]", "", s))
    else:
        v = float(s.replace(",", "."))
    m = _bez_diakritiky(nasobek or "")
    if m.startswith(("miliard", "billion", "milliard")):
        v *= 1e9
    elif m.startswith("mil"):
        v *= 1e6
    elif m.startswith(("tis", "thous")):
        v *= 1e3
    return v


def vytahni_pocty(text: str) -> list:
    """[(klíč veličiny, hodnota, úryvek)] z titulku/perexu."""
    out = []
    for m in _CISLO_RE.finditer(text or ""):
        n, nas, slovo = m.groups()
        try:
            v = _cislo_hodnota(n, nas)
        except ValueError:
            continue
        if not nas and 1900 <= v <= 2100:
            continue                       # letopočet
        sl = _bez_diakritiky(slovo)
        for k, _p, rx in _VEL_RE:
            if rx.search(sl):
                out.append((k, v, m.group(0)))
                break
    return out


def _medium(zdroj: str) -> str:
    """Jedno jméno média pro RSS id i název z Google News („guardian“ = „The Guardian“)."""
    nazvy = {z[0]: z[1] for z in ZDROJE}
    s = _bez_diakritiky(nazvy.get(zdroj, zdroj or ""))
    s = re.sub(r"^the\s+|\s+(news|zpravy)$|\.(com|cz|de|fr|co\.uk|org)$", "", s.strip())
    return re.sub(r"[^a-z0-9]+", "", s)


def cisla_udalosti(udalost_id: int, path: str = DB) -> dict:
    """{veličina: [{hodnota, zdroj, cas, odhad, text}]} — jen veličiny, které
    uvádějí aspoň 2 média a hodnoty se liší o víc než CISLA_ROZDIL."""
    pod = {}
    nazvy = {z[0]: z[1] for z in ZDROJE}        # RSS id → název média
    for z in casova_osa(udalost_id, path):
        for k, v, ury in vytahni_pocty("%s. %s" % (z["titulek"] or "", z["perex"] or "")):
            pod.setdefault(k, []).append({"hodnota": v, "zdroj": nazvy.get(z["zdroj"], z["zdroj"] or "?"),
                                          "cas": z["cas"],
                                          "odhad": z["odhad"], "text": ury})
    out = {}
    for k, rada in pod.items():
        rada.sort(key=lambda r: r["cas"] or 0)
        videno, cista = set(), []
        for r in rada:                      # totéž číslo téhož média jednou (nejdřívější)
            klic = (_medium(r["zdroj"]), r["hodnota"])
            if klic not in videno:
                videno.add(klic)
                cista.append(r)
        if len({_medium(r["zdroj"]) for r in cista}) < 2:
            continue
        hi = max(r["hodnota"] for r in cista)
        lo = min(r["hodnota"] for r in cista)
        if hi > 0 and (hi - lo) / hi > CISLA_ROZDIL:
            out[k] = cista
    return out


def _db_cisla(c) -> None:
    c.execute("""CREATE TABLE IF NOT EXISTS udalost_cisla (
        udalost_id INTEGER PRIMARY KEY, data TEXT, spocteno REAL)""")


def prepocti_cisla(c, ted: float, hodin: float = 72.0) -> dict:
    """Po sběru: čísla událostí s ≥ 2 médii za posledních `hodin`."""
    import json as _json
    _db_cisla(c)
    ids = [r[0] for r in c.execute(
        "SELECT id FROM udalosti WHERE posledni_ts >= ? AND pocet_zdroju >= 2",
        (ted - hodin * 3600,))]
    n = 0
    for uid in ids:
        d = cisla_udalosti(uid)
        if d:
            c.execute("INSERT OR REPLACE INTO udalost_cisla VALUES (?,?,?)",
                      (uid, _json.dumps(d, ensure_ascii=False), ted))
            n += 1
        else:
            c.execute("DELETE FROM udalost_cisla WHERE udalost_id=?", (uid,))
    c.commit()
    return {"udalosti": len(ids), "s_rozdilem": n}


def rozdily_cisel(path: str = DB, hodin: float = 48.0) -> list:
    """Pro dashboard (jen čtení): události, u nichž se čísla mezi médii liší."""
    import json as _json
    if not os.path.exists(path):
        return []
    c = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=5)
    try:
        rows = c.execute(
            """SELECT u.id, u.titulek, u.pocet_zdroju, u.posledni_ts, x.data
               FROM udalost_cisla x JOIN udalosti u ON u.id = x.udalost_id
               WHERE u.posledni_ts >= ? ORDER BY u.pocet_zdroju DESC""",
            (time.time() - hodin * 3600,)).fetchall()
        pm = preklad_mapa(c, [r[1] for r in rows])      # HANS_ZPRAVY_PREKLAD_V1
    except sqlite3.OperationalError:
        return []
    finally:
        c.close()
    popisy = {k: p for k, p, _ in VELICINY}
    return [{"id": uid, "titulek": tit, "titulek_cs": pm.get(tit), "pocet_zdroju": n, "posledni_ts": ts,
             "veliciny": [{"klic": k, "popis": popisy.get(k, k), "rada": r}
                          for k, r in _json.loads(d).items()]}
            for uid, tit, n, ts, d in rows]


# ── HANS_DEMAGOG_V1 (3. 10.) — ověřené výroky politiků z Demagog.cz ─────────
# Pilot 3. 10. (NAPADY 🏛️): ve zprávách tvrdých ověřitelných výroků skoro není
# a vlastní ověření z dat Sněmovny zvládne ~15 % toho, co ověřuje Demagog →
# varianta A: Demagog jako ZDROJ. Hans jen předává jejich verdikt s odkazem,
# sám nic nehodnotí. Veřejné GraphQL bez klíče; jen ČR.
DEMAGOG_API = "https://demagog.cz/graphql"
DEMAGOG_MAX_NOVYCH = 120      # na jeden běh (≈ 0,5 s/výrok)
VERDIKTY = {"VERACITY_TRUE": "pravda", "VERACITY_UNTRUE": "nepravda",
            "VERACITY_MISLEADING": "zavádějící", "VERACITY_UNVERIFIABLE": "neověřitelné"}


def _db_demagog(c) -> None:
    c.execute("""CREATE TABLE IF NOT EXISTS demagog (
        id INTEGER PRIMARY KEY, vyrok TEXT, verdikt TEXT, mluvci TEXT, mluvci_f TEXT,
        strana TEXT, funkce TEXT, porad TEXT, medium TEXT, datum TEXT,
        kratce TEXT, stazeno REAL)""")
    c.execute("CREATE INDEX IF NOT EXISTS ix_dm_datum ON demagog(datum)")


def _demagog_q(dotaz: str, timeout: float = 60) -> dict:
    import json as _json
    import urllib.request as _ur
    r = _ur.Request(DEMAGOG_API, data=_json.dumps({"query": dotaz}).encode(),
                    headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0 (X11; Linux aarch64) HansZpravy/1.0"})
    with _ur.urlopen(r, timeout=timeout) as f:
        d = _json.load(f)
    if d.get("errors"):
        raise RuntimeError(str(d["errors"])[:200])
    return d["data"]


def _bez_html(s: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s or ""))).strip()


def sber_demagog(c, ted: float, max_novych: int = DEMAGOG_MAX_NOVYCH, roky: list = None) -> dict:
    """Stáhne výroky, které ještě nemáme (letošní, v lednu i loňské)."""
    _db_demagog(c)
    if not roky:                           # běžně letošní; ruční doplnění starších roků
        roky = [time.localtime(ted).tm_year]
        if time.localtime(ted).tm_mon == 1:
            roky.append(roky[0] - 1)
    ids = set()
    for r in roky:
        ids |= {int(x["id"]) for x in _demagog_q(
            "{ sitemapStatementsByYear(year:%d) { id } }" % r)["sitemapStatementsByYear"]}
    mame = {r[0] for r in c.execute("SELECT id FROM demagog")}
    nove = sorted(ids - mame, reverse=True)[:max_novych]     # nejnovější napřed
    ulozeno = chyb = 0
    for i in nove:
        try:
            s = _demagog_q(
                "{ statementV2(id:%d) { id content veracity { key } "
                "sourceSpeaker { fullName role body { shortName } } "
                "source { name releasedAt medium { name } } "
                "assessment { shortExplanation } } }" % i)["statementV2"]
        except Exception:
            chyb += 1
            continue
        if not s or not s.get("veracity"):
            continue                       # ještě neověřeno / nezveřejněno
        sp = s.get("sourceSpeaker") or {}
        src = s.get("source") or {}
        jm = re.sub(r"\s+", " ", sp.get("fullName") or "").strip()   # Demagog má i „Vít  Rakušan“
        c.execute("INSERT OR REPLACE INTO demagog VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (
            int(s["id"]), _bez_html(s.get("content")),
            VERDIKTY.get(s["veracity"]["key"], s["veracity"]["key"]),
            jm, _bez_diakritiky(jm), (sp.get("body") or {}).get("shortName") or "",
            sp.get("role") or "", src.get("name") or "",
            (src.get("medium") or {}).get("name") or "", src.get("releasedAt") or "",
            _bez_html((s.get("assessment") or {}).get("shortExplanation")), ted))
        ulozeno += 1
        time.sleep(0.2)
    c.commit()
    return {"novych": ulozeno, "chyb": chyb, "celkem": len(mame) + ulozeno}


_DM_NETEMA = ("demagog", "overil", "overov", "overuj", "overen", "vyrok", "vyroc", "nekdo",
              "pravd", "tvrdil", "tvrdi", "rikal", "rekl", "prohla", "fakt", "check",
              "politi", "muzes", "muzete", "podivej", "podivat", "najdi", "najdete",
              "posledn", "nejak", "nejnov", "zjist", "jestli", "zdali",
              # HANS_DEMAGOG_FOLLOWUP_V1 — slova navazujících otázek
              "dalsi", "dalsich", "dals", "ostatn", "jeste", "jine", "jiny", "jinych",
              "tema", "temat", "ciste", "ukazat", "ukaz", "prosim", "tykaj", "ohled",
              "souvis", "postoj", "historii", "nejake", "nejaky", "takove",
              "recil", "reci", "rika", "rekl", "rekla", "zakla")


def demagog_hledej(dotaz: str, limit: int = 4, path: str = DB, posun: int = 0) -> dict:
    """Výroky podle MLUVČÍHO (příjmení i skloňované: „Babišovi“) nebo podle slov
    výroku. Vrací {mluvci, vyroky:[...], od} — `od` = nejstarší uložený výrok,
    ať odpověď poctivě řekne, kam až záznamy sahají."""
    if not os.path.exists(path):
        return {"mluvci": None, "vyroky": [], "od": None}
    c = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=5)
    try:
        try:
            od = c.execute("SELECT min(datum) FROM demagog").fetchone()[0]
        except sqlite3.OperationalError:
            return {"mluvci": None, "vyroky": [], "od": None}
        slova = [w for w in re.findall(r"[a-z0-9]+", _bez_diakritiky(dotaz)) if len(w) >= 3]
        mluvci = None
        _jm_kmeny = set()
        for (jm, jmf) in c.execute("SELECT DISTINCT mluvci, mluvci_f FROM demagog"):
            prijm = (jmf or "").split()[-1] if jmf else ""
            if len(prijm) < 4:
                continue
            # HANS_DEMAGOG_FOLLOWUP_V1 — kmen podle SKLOŇOVÁNÍ, ne useknutím:
            # 3. 10. „jak“ z Jakoba sedlo na „jaké“. Fiala → fial(y/ovi),
            # Pavel → pavl(a), Babiš/Jakob celé (Babišovi, Jakobovi).
            kmeny_jm = {prijm[:-1] if prijm[-1] in "aeiouy" else prijm}
            if len(prijm) >= 5 and prijm[-2] == "e" and prijm[-1] not in "aeiouy":
                kmeny_jm.add(prijm[:-2] + prijm[-1])
            kmeny_jm = {k for k in kmeny_jm if len(k) >= 4}
            if any(w.startswith(k) for w in slova for k in kmeny_jm):
                _jm_kmeny_kand = kmeny_jm
                # přesnější shoda (i křestní jméno) má přednost
                if mluvci is None or any(w.startswith(jmf.split()[0][:4]) for w in slova):
                    mluvci = jm
                    _jm_kmeny = _jm_kmeny_kand
        sl = "id, vyrok, verdikt, mluvci, strana, funkce, porad, medium, datum, kratce"
        souhrn = {}
        # HANS_DEMAGOG_FOLLOWUP_V1 — téma i u mluvčího („výroky Fialy o migraci“);
        # jména mluvčího a slova žádosti se do tématu nepočítají
        _jm_slova = set(_bez_diakritiky(mluvci or "").split())
        def _je_jmeno(w):
            return any(w.startswith(j[:4]) for j in _jm_slova if len(j) >= 4) or \
                any(w.startswith(k) for k in _jm_kmeny)
        # téma stojí v češtině za PŘEDLOŽKOU („výroky o migraci“, „k poplatkům“);
        # 3. 10. bez toho vyhrála konverzační slova dlouhého souvětí („byste“)
        fq = " ".join(re.findall(r"[a-z0-9]+", _bez_diakritiky(dotaz)))   # i krátké předložky
        za_predl = []
        for m in re.finditer(r"\b(?:o|ohledne|k|ke|tykajici se|na tema|kolem)\s+([a-z]{4,})(?:\s+([a-z]{4,}))?", fq):
            za_predl += [w for w in m.groups() if w]
        kand = [w[:5] for w in (za_predl or slova) if len(w) >= 5
                and not w.startswith(_DM_NETEMA) and not _je_jmeno(w)]
        # téma = jen kmeny, které se ve výrocích VYSKYTUJÍ a nejsou běžné
        # („velmi“, „užitečné“ z dlouhého souvětí jinak shodily hledání na nulu)
        texty = [_bez_diakritiky(r[0]) for r in c.execute("SELECT vyrok FROM demagog")]
        n_t = max(1, len(texty))
        kmeny = []
        for k in dict.fromkeys(kand):
            df = sum(1 for x in texty if k in x)
            if 0 < df <= max(3, 0.03 * n_t):
                kmeny.append(k)
        kmeny = kmeny[:3]
        if mluvci and kmeny:
            # stačí JEDEN kmen tématu; víc shod = výš (dotaz mívá dvě témata)
            vse = [r for _, r in sorted(
                ((sum(k in _bez_diakritiky(r[1]) for k in kmeny), r) for r in c.execute(
                    "SELECT %s FROM demagog WHERE mluvci=? ORDER BY datum DESC, id DESC" % sl, (mluvci,))),
                key=lambda x: -x[0]) if _ > 0]
            for r in vse:
                souhrn[r[2]] = souhrn.get(r[2], 0) + 1
            rows = vse[posun:posun + limit]
        elif mluvci:
            # souhrn verdiktů + stránkování (opakovaný dotaz → další várka)
            souhrn = dict(c.execute("SELECT verdikt, count(*) FROM demagog WHERE mluvci=? "
                                    "GROUP BY verdikt", (mluvci,)).fetchall())
            rows = c.execute("SELECT %s FROM demagog WHERE mluvci=? ORDER BY datum DESC, id DESC "
                             "LIMIT ? OFFSET ?" % sl, (mluvci, limit, posun)).fetchall()
        else:
            # téma: slova dotazu BEZ slov, která nesou jen žádost („ověřil někdo
            # výrok…“) — 3. 10. jinak „demagog“ sedlo na „pozn. Demagog.cz“
            # (kmeny = informativní kmeny tématu, spočtené výš)
            tema = kmeny or None
            if not kmeny and kand:         # téma zadané, ale ve výrocích není → nic
                rows = []
            elif not kmeny:                # holý dotaz → nejnovější ověřené
                rows = c.execute("SELECT %s FROM demagog ORDER BY datum DESC, id DESC LIMIT ?"
                                 % sl, (limit,)).fetchall()
            if tema:
              # ⚠️ SQLite lower()/LIKE diakritiku neskládá („ducho“ × „důchod“)
              # → hledá se v Pythonu nad textem bez diakritiky (výroků jsou stovky)
              vse = [r for _, r in sorted(
                  ((sum(k in _bez_diakritiky(r[1]) for k in kmeny), r) for r in c.execute(
                      "SELECT %s FROM demagog ORDER BY datum DESC, id DESC" % sl)),
                  key=lambda x: -x[0]) if _ > 0][:200]
              for r in vse:
                  souhrn[r[2]] = souhrn.get(r[2], 0) + 1
              rows = vse[posun:posun + limit]
    finally:
        c.close()
    klice = ["id", "vyrok", "verdikt", "mluvci", "strana", "funkce", "porad", "medium",
             "datum", "kratce"]
    vy = [dict(zip(klice, r)) for r in rows]
    for v in vy:
        v["url"] = "https://demagog.cz/vyrok/%d" % v["id"]
    return {"mluvci": mluvci, "vyroky": vy, "od": od, "souhrn": souhrn,
            "celkem": sum(souhrn.values()), "posun": posun,
            "tema": kmeny, "klic": "%s|%s" % (mluvci or "", ",".join(kmeny))}


_DM_PRIPSANI = re.compile(
    r"podle\s+(?:serveru\s+|webu\s+|projektu\s+)?demagog|"
    r"demagog(?:\.cz)?\s+(?:uvad|overil|zjistil|pise|tvrdi|hodnot|oznacil|doloz)|"
    r"na\s+strank(?:ach|y)\s+demagog|demagog(?:\.cz)?\s*\(\s*\d")


def demagog_vety_pryc(text: str) -> str:
    """Vyhodí věty, které vymyšleně připisují něco Demagogu (zbytek nechá)."""
    vety = re.split(r"(?<=[.!?])\s+", text or "")
    zbyle = [v for v in vety if not (_DM_PRIPSANI.search(_bez_diakritiky(v))
                                     or "demagog" in _bez_diakritiky(v))]
    return " ".join(zbyle).strip()


def demagog_vymysleno(text: str, path: str = DB) -> bool:
    """HANS_DEMAGOG_GUARD_V1 (3. 10.) — cituje odpověď MODELU Demagog bez opory?
    /tazatel 3. 10.: 3× model zkopíroval tvar výpisu z historie a vyplnil ho
    smyšlenými výroky skutečných politiků s verdikty a odkazy (0/12 id v DB).
    True = odkaz na výrok, který nemáme, odkaz na skutečný výrok s jiným textem,
    nebo verdikt v uvozovkách bez odkazu. Prostá zmínka „Demagog“ projde."""
    f = _bez_diakritiky(text or "")
    if "demagog" not in f:
        return False
    ids = [int(x) for x in re.findall(r"demagog\.cz/vyrok/(\d+)", text or "")]
    verdikt = re.search(r"\b(PRAVDA|NEPRAVDA|ZAVÁDĚJÍCÍ|NEOVĚŘITELNÉ)\b", text or "")
    if not ids:
        # HANS_DEMAGOG_GUARD_V2 — i PŘIPSÁNÍ zdroje bez odkazu: 3. 10. Matrix
        # „Podle Demagog.cz (4. září 2026) studenti ve Francii protestují…“
        return bool((verdikt and re.search(r"[„\"“].{8,}[“\"”]", text or ""))
                    or _DM_PRIPSANI.search(f))
    if not os.path.exists(path):
        return True
    c = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=5)
    try:
        for i in ids:
            r = c.execute("SELECT vyrok FROM demagog WHERE id=?", (i,)).fetchone()
            if not r:
                return True
            if _bez_diakritiky(r[0])[:40] not in f:
                return True
    except sqlite3.OperationalError:
        return True
    finally:
        c.close()
    return False


def demagog_mluvci(path: str = DB) -> list:
    """Pro dashboard: politici s počty ověřených výroků podle verdiktu."""
    if not os.path.exists(path):
        return []
    c = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=5)
    try:
        rows = c.execute(
            """SELECT mluvci, max(strana), count(*), max(datum),
                      sum(verdikt='pravda'), sum(verdikt='nepravda'),
                      sum(verdikt='zavádějící'), sum(verdikt='neověřitelné')
               FROM demagog GROUP BY mluvci ORDER BY count(*) DESC""").fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        c.close()
    return [{"mluvci": m, "strana": s, "pocet": n, "posledni": d, "pravda": a,
             "nepravda": b, "zavadejici": z, "neoveritelne": x} for m, s, n, d, a, b, z, x in rows]


def demagog_vyroky(mluvci: str = "", dotaz: str = "", limit: int = 100, path: str = DB) -> dict:
    """Pro dashboard: výroky vybraného politika (přesné jméno) nebo hledání."""
    if mluvci:
        if not os.path.exists(path):
            return {"vyroky": []}
        c = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=5)
        try:
            rows = c.execute("SELECT id, vyrok, verdikt, mluvci, strana, funkce, porad, medium, "
                             "datum, kratce FROM demagog WHERE mluvci=? ORDER BY datum DESC, id DESC "
                             "LIMIT ?", (mluvci, limit)).fetchall()
        finally:
            c.close()
        k = ["id", "vyrok", "verdikt", "mluvci", "strana", "funkce", "porad", "medium", "datum", "kratce"]
        vy = [dict(zip(k, r)) for r in rows]
        for v in vy:
            v["url"] = "https://demagog.cz/vyrok/%d" % v["id"]
        return {"mluvci": mluvci, "vyroky": vy}
    return demagog_hledej(dotaz, limit=limit, path=path)


# ── HANS_ZPRAVY_CHAT_V1 (3. 10.) — zprávy pro Hanse (chat /zpravy) ─────────
# Test 3. 10. (10 otázek): podle VÝZNAMU (bge-m3, napříč jazyky) 8/10 trefa,
# klíčová slova jen čeština. Potřeba: práh (Brno 0,56 → francouzské zprávy),
# NEJNOVĚJŠÍ titulek události („jak dopadlo“), sloučit shluky téže události
# (letadlo 139/21/5 médií), český titulek když je, jinak originál (překlad
# zamítnut uživatelem). Spící PC → klíčová slova nad českými titulky.
ZPRAVY_PRAH = 0.62            # podobnost dotazu a titulku (bge-m3, kosinus) — stačí sama
ZPRAVY_PRAH_SLOVA = 0.55      # nižší podobnost projde, jen když v titulcích události
                              # jsou VŠECHNY kmeny tématu (jména: Babiš 0,59, Pikeová 0,59;
                              # falešné: Brno 0,56, Trump+cla 0,60 — samotný práh nedělí)
ZPRAVY_SLOUCIT = 0.80         # dvě události podobnější než tohle = jedna
_ZP_NETEMA = ("noveho", "nove", "zprav", "novin", "pisou", "pise", "stalo", "deje",
              "dopad", "nejak", "jsou", "svete", "svet", "dnes", "vcera", "zajima",
              "zajim", "slysel", "cetl", "muzes", "muzete", "mohl", "prosim", "rekni",
              "reknete", "vlastne", "porad", "jeste", "nejnov", "aktual", "posledn",
              # HANS_ZPRAVY_HOLY_DOTAZ_V1 (4. 10., hlas 3. 10.): „Jaké jsou dnešní
              # zprávy?“ bralo „jaké“ jako téma → „o tom nic nenašel“
              "jak", "dulez", "nejdulez", "hlavni", "udal", "dnesk", "sobot",
              "cosi", "neco", "nejak", "fakt", "zapom", "vubec", "nesti", "zajim",
              "sleduj", "zaznam", "zachyt", "nich", "precet", "cetl", "slyse", "videl",
              "jste", "mate", "budes", "budete", "tedy")


def _zp_udalost_popis(uid: int, path: str, kmeny: list = None) -> dict:
    if uid < 0:                               # samostatný RSS titulek (−id)
        c = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=5)
        try:
            r = c.execute("SELECT titulek, zdroj, COALESCE(publikovano, prvni_ts) FROM titulky "
                          "WHERE id=?", (-uid,)).fetchone()
        finally:
            c.close()
        if not r:
            return {}
        nazvy = {z[0]: z[1] for z in ZDROJE}
        return {"titulek": r[0], "zdroj": nazvy.get(r[1], r[1]), "cas": r[2], "prvni": r[2],
                "medii": 1, "cesky": True, "trefa": None}
    # bulvár (Google News ho nese i ve shlucích seriózních zpráv) se neukazuje
    osa = [z for z in casova_osa(uid, path) if _f(z.get("zdroj") or "") not in BULVAR]
    if not osa:
        return {}
    cz = [z for z in osa if z.get("jazyk") == "cs" and z.get("titulek")]
    posl = (cz or osa)[-1]                    # NEJNOVĚJŠÍ (vývoj, „jak dopadlo“)
    # + titulek, který k dotazu sedí nejlíp (nejnovější výsledek neřekne vždy)
    trefa = None
    if kmeny:
        sk = [(sum(k in _bez_diakritiky(z["titulek"] or "") for k in kmeny), z["cas"] or 0, z)
              for z in (cz or osa)]
        sk = [x for x in sk if x[0] > 0 and x[2] is not posl]
        if sk:
            trefa = max(sk, key=lambda x: (x[0], x[1]))[2]
    nazvy = {z[0]: z[1] for z in ZDROJE}
    medii = {_medium(z.get("zdroj") or "") for z in osa if z.get("zdroj")}
    return {"titulek": posl["titulek"], "zdroj": nazvy.get(posl["zdroj"], posl["zdroj"]),
            "cas": posl["cas"], "prvni": osa[0]["cas"], "medii": len(medii),
            "cesky": bool(cz),
            "trefa": ({"titulek": trefa["titulek"], "zdroj": nazvy.get(trefa["zdroj"], trefa["zdroj"]),
                       "cas": trefa["cas"]} if trefa else None)}


def zpravy_odkazy(uid: int, path: str = DB, medium: str = None, limit: int = 1) -> list:
    """HANS_ZPRAVY_ODKAZY_V1 (4. 10.) — odkazy na články k události.

    Odkaz mají jen titulky z RSS kanálů médií (`titulky.url`); Google News dává
    zašifrovaná přesměrování, ta se neukazují. Česky přednost, pak nejnovější,
    bez bulváru. `medium` = jen ten (porovnání přes `_medium`).
    → [{"zdroj": název, "titulek": …, "url": …}]"""
    c = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=5)
    try:
        if uid < 0:
            rows = c.execute("SELECT zdroj, titulek, url, jazyk, COALESCE(publikovano, prvni_ts) "
                             "FROM titulky WHERE id=?", (-uid,)).fetchall()
        else:
            rows = c.execute(
                """SELECT t.zdroj, t.titulek, t.url, t.jazyk, COALESCE(t.publikovano, t.prvni_ts)
                   FROM titulek_udalost x JOIN titulky t ON t.id = x.titulek_id
                   WHERE x.udalost_id = ?""", (uid,)).fetchall()
    finally:
        c.close()
    nazvy = {z[0]: z[1] for z in ZDROJE}
    chci = _medium(medium) if medium else None
    rows = [r for r in rows if r[2] and r[2].startswith("http")
            and _f(nazvy.get(r[0], r[0]) or "") not in BULVAR
            and (chci is None or _medium(r[0]) == chci)]
    rows.sort(key=lambda r: (r[3] != "cs", -(r[4] or 0)))
    out, videno = [], set()
    for zdr, tit, url, _j, _t in rows:
        if url in videno:
            continue
        videno.add(url)
        # sledovací přívěsky (utm_…) do odkazu pro člověka nepatří
        url = re.sub(r"#utm_.*$", "", url)
        url = re.sub(r"[?&]utm_[^#]*$", "", url)
        out.append({"zdroj": nazvy.get(zdr, zdr), "titulek": tit, "url": url})
        if len(out) >= limit:
            break
    return out


_MEDIUM_ALIAS = ((r"\bct\s*24\b|\bct\b|ceske? televiz", "ct24"), (r"rozhlas", "irozhlas"),
                 (r"\bseznam", "seznam"), (r"novink", "novinky"), (r"idnes", "idnes"),
                 (r"\bbbc\b", "bbc"), (r"guardian", "guardian"), (r"\bnpr\b", "npr"),
                 (r"al\s*d?zh?a?zeer|al\s*jazeer", "aljazeera"), (r"\bdw\b|deutsche welle", "dw"),
                 (r"tagesschau", "tagesschau"), (r"\brfi\b", "rfi"))


def medium_ze_zpravy(zprava: str):
    """HANS_ZPRAVY_ODKAZY_V1 — RSS id média jmenovaného ve větě („na Novinky“,
    „z ČT“), jinak None."""
    f = _bez_diakritiky(zprava or "")
    for vzor, rid in _MEDIUM_ALIAS:
        if re.search(vzor, f):
            return rid
    return None


_ZP_DOMENY = re.compile(r"https?://(?:www\.)?(?:[\w-]+\.)*(?:irozhlas\.cz|novinky\.cz|"
                        r"ceskatelevize\.cz|seznamzpravy\.cz|idnes\.cz|bbc\.co\.uk|bbc\.com|"
                        r"theguardian\.com|npr\.org|aljazeera\.com|dw\.com|tagesschau\.de|"
                        r"rfi\.fr|lidovky\.cz|aktualne\.cz|denik\.cz)[^\s)\]>\"'“”]*", re.I)


def odkazy_vymyslene(text: str, path: str = DB) -> list:
    """HANS_ZPRAVY_URL_GUARD_V1 (4. 10.) — odkazy na zpravodajské weby v odpovědi,
    které NEJSOU v nasbíraných titulcích (= vymyšlené). Živý test 4. 10.: Hans
    „poslal odkaz“ irozhlas.cz/…hamasu-153749 na zprávu, kterou si vymyslel."""
    urls = [u.rstrip(").,;]>\"'“") for u in _ZP_DOMENY.findall(text or "")]
    if not urls:
        return []
    out = []
    c = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=5)
    try:
        for u in dict.fromkeys(urls):
            r = c.execute("SELECT 1 FROM titulky WHERE url = ? OR url LIKE ? OR url LIKE ? LIMIT 1",
                          (u, u + "#%", u + "?%")).fetchone()
            if not r:
                out.append(u)
    except Exception:
        return []                      # DB nejde přečíst → radši nic neškrtat
    finally:
        c.close()
    return out


def udalosti_podle_url(urls: list, path: str = DB) -> list:
    """HANS_ZPRAVY_ODKAZY_V1 — id událostí (−id u samostatného titulku) k odkazům
    z předchozího výpisu /zpravy; pořadí jako v `urls`."""
    out = []
    c = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=5)
    try:
        for u in urls:
            r = c.execute("""SELECT t.id, x.udalost_id FROM titulky t
                             LEFT JOIN titulek_udalost x ON x.titulek_id = t.id
                             WHERE t.url = ? OR t.url LIKE ? OR t.url LIKE ?
                             LIMIT 1""", (u, u + "#%", u + "?%")).fetchone()   # výpis utm ořezal
            if r:
                uid = r[1] if r[1] is not None else -r[0]
                if uid not in out:
                    out.append(uid)
    finally:
        c.close()
    return out


# HANS_ZPRAVY_TEMA_ZALOHA_V1 (6. 10.) — /tazatel: „Jaké byly dnes v médiích
# nejzajímavější zprávy? Pokud sledujete, co se děje ve světě?“ → jako „téma“
# zbyla slova byly/médiích/nejzajímavější/pokud → „o tom jsem nic nenašel“.
# Seznam slov bez tématu se doplňuje od 4. 10. a nestačí; proto: nenajde-li se
# nic a věta téma VÝSLOVNĚ nejmenuje, je to obecný dotaz → přehled dne.
_ZP_VYSLOVNE = re.compile(
    r"\b(?:o|ohledne|kolem|okolo|tykajici\s+se|na\s+tema|k\s+tematu)\s+[a-z0-9]{4,}")


def _zp_vyslovne_tema(dotaz: str) -> bool:
    """Jmenuje dotaz téma výslovně? Krátký dotaz (heslo), vazba „o X“ nebo
    vlastní jméno uvnitř věty."""
    d = (dotaz or "").strip()
    if len(d.split()) <= 4:
        return True
    # „o sledování titulků“ / „o tom“ téma nejmenuje (HANS_ZPRAVY_CO_SE_DEJE_V1)
    for m in _ZP_VYSLOVNE.finditer(_bez_diakritiky(d)):
        w = m.group(0).split()[-1]
        if not w.startswith(_ZP_NETEMA + ("sledov", "titul", "medi", "tech", "cemz",
                                          "nicem", "vsem", "dobe", "tomto")):
            return True
    return bool(re.search(r"(?<![.!?]\s)(?<!^)\b[A-ZÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ][\w]{2,}", d))


def zpravy_hledej(dotaz: str, config: dict = None, hodin: float = 72.0, limit: int = 4,
                  path: str = DB, zaloha_prehled: bool = False,
                  bez_klauzi: bool = False) -> dict:
    """{rezim: 'prehled'|'vyznam'|'slova'|'nic', udalosti: [...], tema: bool}."""
    import numpy as np
    if not os.path.exists(path):
        return {"rezim": "nic", "udalosti": [], "tema": False}
    od = time.time() - hodin * 3600
    # HANS_ZPRAVY_KLAUZE_V1 (4. 10.) — /tazatel: „fakt jo, zapomněl jsem. a co je
    # teď ve zprávách? cosi zajímavého?“ → „cosi“ a „zapomněl“ byla „témata“,
    # hledání podle smyslu nic nenašlo a model si pak titulky VYMYSLEL. Téma
    # se bere jen z věty, která se na zprávy ptá (s ní i doplněk za otazníkem).
    _klauze = [k for k in re.split(r"(?<=[.!?;])\s+", dotaz or "") if k.strip()]
    if len(_klauze) > 1 and not bez_klauzi:
        _i = next((i for i, k in enumerate(_klauze)
                   if re.search(r"zpr[aá]v|novin|sv[eě]t|p[ií][sš]ou|ud[aá]lo|stalo",
                                _bez_diakritiky(k))), None)
        if _i is not None:
            dotaz = " ".join(_klauze[_i:_i + 2])
    slova = [w for w in re.findall(r"[a-z0-9]+", _bez_diakritiky(dotaz)) if len(w) >= 4]
    tema = [w for w in slova if not w.startswith(_ZP_NETEMA)]
    c = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=5)
    try:
        info = {r[0]: r[1] for r in c.execute(
            "SELECT id, pocet_zdroju FROM udalosti WHERE posledni_ts >= ?", (od,))}
        if not tema:                          # holý dotaz → největší události dne
            ids = [r[0] for r in c.execute(
                "SELECT id FROM udalosti WHERE posledni_ts >= ? ORDER BY pocet_zdroju DESC "
                "LIMIT ?", (time.time() - 24 * 3600, limit * 3))]
            rezim, skore = "prehled", {}
        else:
            e = _embed([dotaz], config or {}) if config is not None else None
            if e:
                rows = c.execute(
                    """SELECT uc.udalost_id, v.vec FROM vektory v JOIN pribehy p ON p.id=v.pribeh_id
                       JOIN udalost_clen uc ON uc.pribeh_id=p.id WHERE p.posledni_ts>=?
                       UNION ALL
                       SELECT COALESCE(tu.udalost_id, -t.id), v.vec FROM vektory_tit v
                       JOIN titulky t ON t.id=v.titulek_id
                       LEFT JOIN titulek_udalost tu ON tu.titulek_id=t.id WHERE t.posledni_ts>=?""",
                    (od, od)).fetchall()
                # RSS titulek BEZ události (přiřazení má práh 0,70) = samostatná zpráva:
                # 3. 10. „vláda ustála hlasování o nedůvěře“ (iROZHLAS, Seznam) v žádné nebyl
                for uid, _ in rows:
                    if uid < 0:
                        info.setdefault(uid, 1)
                q = np.array(e[0], dtype=np.float32); q /= (np.linalg.norm(q) or 1)
                nej, vek = {}, {}
                for uid, vb in rows:
                    if uid not in info:
                        continue
                    v = np.frombuffer(vb, dtype=np.float32)
                    v = v / (np.linalg.norm(v) or 1)
                    s = float(v @ q)
                    if s > nej.get(uid, -1):
                        nej[uid], vek[uid] = s, v
                kmeny = [w[:5] for w in tema][:4]

                def _slova_sedi(u):
                    if u < 0:
                        f = _bez_diakritiky(_zp_udalost_popis(u, path).get("titulek") or "")
                        return bool(kmeny) and all(k in f for k in kmeny)
                    f = _bez_diakritiky(" ".join("%s %s" % (z.get("titulek") or "", z.get("perex") or "")
                                                 for z in casova_osa(u, path)))
                    return bool(kmeny) and all(k in f for k in kmeny)
                kand = sorted((u for u in nej if nej[u] >= ZPRAVY_PRAH or
                               (nej[u] >= ZPRAVY_PRAH_SLOVA and _slova_sedi(u))),
                              key=lambda u: -nej[u])
                if kand:                          # o víc než 0,12 slabší než nejlepší = šum
                    kand = [u for u in kand if nej[u] >= nej[kand[0]] - 0.15]
                ids, vybrane = [], []
                for u in kand:                    # sloučit shluky téže události
                    if any(float(vek[u] @ vek[w]) >= ZPRAVY_SLOUCIT for w in vybrane):
                        continue
                    vybrane.append(u); ids.append(u)
                rezim, skore = "vyznam", nej
            else:                                 # PC spí → klíčová slova (jen čeština)
                kmeny = [w[:5] for w in tema][:4]
                sc = {}
                for uid, tit, per in c.execute(
                        """SELECT tu.udalost_id, t.titulek, t.perex FROM titulky t
                           JOIN titulek_udalost tu ON tu.titulek_id=t.id
                           WHERE t.posledni_ts>=? AND t.jazyk='cs'""", (od,)):
                    f = _bez_diakritiky("%s %s" % (tit, per or ""))
                    h = sum(k in f for k in kmeny)
                    if h and uid in info:
                        sc[uid] = max(sc.get(uid, 0), h + 0.001 * (info[uid] or 0))
                ids = sorted(sc, key=lambda u: -sc[u])
                rezim, skore = "slova", sc
    finally:
        c.close()
    out = []
    for uid in ids:
        d = _zp_udalost_popis(uid, path, [w[:5] for w in tema][:4])
        if not d or any(_norm_tit(d["titulek"]) == _norm_tit(x["titulek"]) for x in out):
            continue
        d["id"] = uid
        d["skore"] = round(float(skore.get(uid, 0)), 2)
        out.append(d)
        if len(out) >= limit:
            break
    # jen pro příkaz /zpravy (věta se na zprávy PTÁ); podklad ze zpráv k běžné
    # otázce by jinak dostal přehled dne k čemukoli
    if zaloha_prehled and not out and tema and not _zp_vyslovne_tema(dotaz):
        _log.info("HANS_ZPRAVY_TEMA_ZALOHA_V1: %s nenašlo nic a věta téma nejmenuje "
                  "→ přehled dne", tema[:5])
        return zpravy_hledej("", config, hodin, limit, path)
    return {"rezim": rezim if out else "nic", "hledano": rezim, "udalosti": out, "tema": bool(tema)}


# ── HANS_ZPRAVY_PREKLAD_V1 (3. 10.) — české titulky JEN PRO ČTENÍ na webu ─────
# Překlad PŘED zpracováním uživatel 1. 10. zamítl (vnáší falešné rozpory) a to
# platí dál: shlukování, čísla i hledání jedou nad ORIGINÁLY. Tady se jen
# pro zobrazení přidá česká verze. Test 3. 10. na 12 titulcích: translategemma
# přesnější („Fast 2000“ = „téměř 2000“; hans-czech „více než“ = opak,
# „mineur“ → „horník“), 28 s / 12 titulků. ~660 cizích titulků denně.
PREKLAD_MODEL = "translategemma:12b"
PREKLAD_DAVKA = 15
PREKLAD_MAX = 90              # na jeden hodinový běh


def _db_preklady(c) -> None:
    c.execute("""CREATE TABLE IF NOT EXISTS preklady (
        otisk TEXT PRIMARY KEY, puvodni TEXT, cesky TEXT, model TEXT, ts REAL)""")


def _otisk_textu(s: str) -> str:
    import hashlib
    return hashlib.sha1(re.sub(r"\s+", " ", (s or "").strip()).encode("utf-8")).hexdigest()


def _ollama_volna() -> bool:
    """Překládat jen když GPU nepotřebuje hra, překlad dokumentů ani render."""
    try:
        from scripts.ollama_client import game_mode_on, translate_pause_on
        if game_mode_on() or translate_pause_on():
            return False
    except Exception:
        return False
    try:
        d = sqlite3.connect("file:%s?mode=ro" % os.path.join(ROOT, "data", "hans_diary.db"),
                            uri=True, timeout=3)
        bezi = d.execute("SELECT count(*) FROM heavy_jobs WHERE status='running'").fetchone()[0]
        d.close()
        return not bezi
    except Exception:
        return True


def _preloz_davku(texty: list, config: dict):
    import json as _json
    import requests
    from scripts.ollama_client import _resolve_url
    prompt = ("Přelož tyto novinové titulky do přirozené češtiny. Zachovej jména, čísla a "
              "význam (např. „fast“ = „téměř“). Vrať JSON pole řetězců ve stejném pořadí, "
              "nic jiného.\n" + _json.dumps(texty, ensure_ascii=False))
    r = requests.post(_resolve_url(None, config) + "/api/generate", json={
        "model": PREKLAD_MODEL, "prompt": prompt, "stream": False, "keep_alive": "30s",
        "options": {"temperature": 0.1, "num_ctx": 4096}}, timeout=(5, 300))
    r.raise_for_status()
    o = r.json().get("response", "")
    o = re.sub(r"^\s*```(?:json)?|```\s*$", "", o.strip())
    m = re.search(r"\[.*\]", o, re.S)
    a = _json.loads(m.group(0)) if m else None
    if not isinstance(a, list) or len(a) != len(texty):
        return None                       # nesedí počet → radši nic než posunuté
    return [str(x).strip() for x in a]


def _cizi_titulky(c, od: float) -> list:
    out = []
    for (t,) in c.execute("SELECT titulek FROM pribehy WHERE posledni_ts>=? AND jazyk!='cs' "
                          "ORDER BY na_spici DESC, videno DESC", (od,)):
        out.append(t)
    for (t,) in c.execute("SELECT titulek FROM titulky WHERE posledni_ts>=? AND jazyk!='cs' "
                          "ORDER BY na_spici DESC, prvni_ts DESC", (od,)):
        out.append(t)
    for (t, j) in c.execute("SELECT titulek, jazyky FROM udalosti WHERE posledni_ts>=?", (od,)):
        if t and _je_cizi_titulek(c, t):
            out.append(t)
    return [t for t in dict.fromkeys(out) if t]


def _je_cizi_titulek(c, t: str) -> bool:
    r = (c.execute("SELECT jazyk FROM pribehy WHERE titulek=? LIMIT 1", (t,)).fetchone()
         or c.execute("SELECT jazyk FROM titulky WHERE titulek=? LIMIT 1", (t,)).fetchone())
    return bool(r) and r[0] != "cs"


def prelozit_titulky(c, config: dict, ted: float, max_n: int = PREKLAD_MAX) -> dict:
    _db_preklady(c)
    if not _ollama_volna():
        return {"prelozeno": 0, "odlozeno": "GPU obsazená"}
    mame = {r[0] for r in c.execute("SELECT otisk FROM preklady")}
    chybi = [t for t in _cizi_titulky(c, ted - 48 * 3600) if _otisk_textu(t) not in mame][:max_n]
    n = chyb = 0
    for i in range(0, len(chybi), PREKLAD_DAVKA):
        davka = chybi[i:i + PREKLAD_DAVKA]
        if i and not _ollama_volna():          # mezitím hra / render → přestat
            break
        pr = None
        for pokus in (1, 2):                   # 3. 10.: jedna chyba ukončila celé doplnění
            try:
                pr = _preloz_davku(davka, config)
                break
            except Exception as e:
                _log.info("překlad titulků: %s (pokus %d)", str(e)[:100], pokus)
                time.sleep(10)
        else:
            break
        if not pr:
            chyb += 1
            continue
        for a, b in zip(davka, pr):
            c.execute("INSERT OR REPLACE INTO preklady VALUES (?,?,?,?,?)",
                      (_otisk_textu(a), a, b, PREKLAD_MODEL, ted))
            n += 1
        c.commit()
    return {"prelozeno": n, "chybnych_davek": chyb, "zbyva": max(0, len(chybi) - n)}


def preklad_mapa(c, texty) -> dict:
    """{původní: český} pro zobrazení; co přeložené není, chybí."""
    try:
        ot = {_otisk_textu(t): t for t in texty if t}
        if not ot:
            return {}
        out = {}
        klice = list(ot)
        for i in range(0, len(klice), 500):
            q = klice[i:i + 500]
            for k, cz in c.execute("SELECT otisk, cesky FROM preklady WHERE otisk IN (%s)"
                                   % ",".join("?" * len(q)), q):
                out[ot[k]] = cz
        return out
    except sqlite3.OperationalError:
        return {}


# ── HANS_ZPRAVY_PODKLAD_V1 (3. 10.) — zprávy jako PODKLAD pro otázky na dění ──
# Matrix 3. 10.: „proč studenti ve Francii protestují?“ nešlo do /zpravy (bez
# slova zprávy) → model si vymyslel vysoké školy a připsal to Demagogu.
# Práh změřen nad 1 284 běžnými větami z deníku × 10 otázkami na dění:
# 0,65 → 8/10 dění, 9 běžných nad prahem (počasí × francouzské „Météo“, film,
# TV — ty skoro vždy obslouží jiná cesta dřív). Navíc: pod 0,72 musí aspoň
# jeden kmen tématu stát v titulcích události (vyřadí počasí i „kafe“).
PODKLAD_PRAH = 0.65
PODKLAD_JISTE = 0.72


_POSLEDNI_PODKLAD = {"dotaz": None, "uid": 0, "ts": 0.0, "radky": []}


def posledni_podklad(dotaz: str, max_age_s: float = 120.0):
    """HANS_ZPRAVY_RETELL_V1 — titulky („Médium: titulek — perex“) a id události
    z právě sestaveného podkladu pro TENTÝŽ dotaz, jinak None."""
    p = _POSLEDNI_PODKLAD
    if p.get("dotaz") != dotaz or time.time() - float(p.get("ts") or 0) > max_age_s \
            or not p.get("radky"):
        return None
    return {"uid": p["uid"], "radky": list(p["radky"])}


# ── HANS_ZPRAVY_RETELL_V1 (7. 10.) — zprávy se PŘEVYPRÁVĚJÍ, nevykládají ──────
# Měření 7. 10. na 8 událostech: volná odpověď nad podkladem měla 25 z 51 vět
# bez opory (příčiny protestů domyšleny, Nobelova cena z hlavy = loňská, ve
# 4 z 8 vymyšlený odkaz). Holý výpis by zase nešel číst hlasem. Úzké zadání
# „řekni, co píšou titulky, a jmenuj médium“ dalo na 7 událostech věrné krátké
# odpovědi („titulky neuvádějí důvod protestů…“, správný laureát).
# Výsledek se kontroluje (žádné nové jméno ani letopočet, žádný vlastní zdroj);
# když neprojde, řeknou se titulky samotné. Odkazy se přikládají kódem.
_RETELL_SYS = (
    "Jsi {jmeno}, zdvořilý a přemýšlivý společník. Mluvíš česky a tazateli vykáš.\n"
    "ÚKOL: Dostaneš OTÁZKU a TITULKY ZPRÁV s médii. Dvěma až třemi větami řekni, co o tom "
    "zprávy píšou, a jmenuj médium.\n"
    "PRAVIDLA: Říkej jen to, co stojí v titulcích. Čísla a jména opiš přesně. Nevysvětluj "
    "příčiny ani souvislosti, které v titulcích nejsou. Když titulky na otázku neodpovídají, "
    "řekni to. Žádné odkazy, nic nenabízej. Tazatele oslov jednou tvarem, který dostaneš.")


def prevypravej(config: dict, otazka: str, radky: list, osloveni: str = "",
                jmeno: str = "Hans") -> tuple:
    """(text, 'hlasem' | 'titulky'). Nikdy nevrací prázdno, když jsou řádky."""
    zaklad = "\n".join(radky[:4])
    zaloha = ("Ve zprávách k tomu mám tohle%s: " % ((", " + osloveni) if osloveni else "")
              + " ".join((x.split(" — ")[0].rstrip(".") + ".") for x in radky[:3]))
    try:
        from scripts.ollama_client import ollama_generate
        from scripts.hans_claim_filter import filtruj, _ROK, _CISLO_SLOVY
        model = str((config.get("models", {}) or {}).get("voice")
                    or (config.get("dialog", {}) or {}).get("model") or "hans-czech:latest")
        r = (ollama_generate(
            model, "OSLOVENÍ: %s\nOTÁZKA: %s\nTITULKY ZPRÁV:\n%s\n\nTvoje odpověď:"
            % (osloveni or "(bez oslovení)", (otazka or "")[:300], zaklad),
            system=_RETELL_SYS.format(jmeno=jmeno or "Hans"), config=config, timeout=60,
            options={"temperature": 0.3, "num_predict": 190}) or "").strip()
        if r and not (set(_ROK.findall(r)) - set(_ROK.findall(zaklad))) \
                and not (_CISLO_SLOVY.search(r) and not _CISLO_SLOVY.search(zaklad)):
            _n, st = filtruj(r, otazka, [zaklad] * 3, (osloveni,))
            if not (st["zdroj"] or st["tvrzeni"]):
                return r, "hlasem"
            _log.info("HANS_ZPRAVY_RETELL_V1: převyprávění neprošlo kontrolou %s → titulky", st)
    except Exception as e:
        _log.info("HANS_ZPRAVY_RETELL_V1: převyprávění nejde (%s) → titulky", str(e)[:80])
    return zaloha, "titulky"


def zpravy_podklad(dotaz: str, config: dict, path: str = DB):
    """Text podkladu ze zpráv (nebo None) — titulky + perexy nejbližší události
    a začátek plného českého článku, s médiem a časem."""
    from datetime import datetime as _dt
    r = zpravy_hledej(dotaz, config, limit=2, path=path)
    # HANS_ZPRAVY_PODKLAD_CELA_VETA_V1 (7. 10.) — zúžení na klauzi (KLAUZE_V1)
    # je stavěné pro příkaz /zpravy. U podkladu umí téma ZAHODIT: „…o detailech
    # toho útoku z dneška? Jak se to stalo…“ → vybrána druhá věta (kvůli „stalo“),
    # téma zůstalo v první → nic → dohledání na Wikipedii místo zprávy, kterou
    # Hans má. Když zúžené hledání nic nedá, zkusí se celá věta.
    # 📏 1 356 reálných vět: zúžení se týká 12, změna podkladu 0.
    if ((r.get("rezim") != "vyznam" or not r["udalosti"]
         or r["udalosti"][0]["skore"] < PODKLAD_PRAH)
            and len([k for k in re.split(r"(?<=[.!?;])\s+", dotaz or "") if k.strip()]) > 1):
        r = zpravy_hledej(dotaz, config, limit=2, path=path, bez_klauzi=True)
    if r.get("rezim") != "vyznam" or not r["udalosti"]:
        return None
    top = r["udalosti"][0]
    if top["skore"] < PODKLAD_PRAH:
        return None
    uid = top["id"]
    osa = casova_osa(uid, path) if uid > 0 else []
    kmeny = [w[:5] for w in re.findall(r"[a-z]+", _bez_diakritiky(dotaz))
             if len(w) >= 4 and not w.startswith(_ZP_NETEMA)]
    if top["skore"] < PODKLAD_JISTE:
        f = _bez_diakritiky(" ".join("%s %s" % (z.get("titulek") or "", z.get("perex") or "")
                                     for z in osa) or top["titulek"])
        if not any(k in f for k in kmeny):
            return None
    nazvy = {z[0]: z[1] for z in ZDROJE}
    radky = []
    osa = [z for z in osa if _f(z.get("zdroj") or "") not in BULVAR]   # bez bulváru
    vyber = [z for z in osa if z.get("jazyk") == "cs"][-5:] or osa[-5:]
    for z in vyber:
        cas = _dt.fromtimestamp(z["cas"] or 0).strftime("%d. %m. %H:%M")
        radky.append("- %s, %s: %s%s" % (cas, nazvy.get(z["zdroj"], z["zdroj"]), z["titulek"],
                                         (" — " + z["perex"][:240]) if z.get("perex") else ""))
    if not radky:
        radky.append("- %s: %s" % (top["zdroj"], top["titulek"]))
    # HANS_ZPRAVY_RETELL_V1 — titulky a událost si pamatuj pro převyprávění
    _POSLEDNI_PODKLAD.update(
        dotaz=dotaz, uid=uid, ts=time.time(),
        radky=[re.sub(r"^- \d\d\. \d\d\. \d\d:\d\d, ", "", x)[:300] for x in radky])
    clanek = ""
    if uid > 0:
        c = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=5)
        try:
            tids = [x[0] for x in c.execute(
                """SELECT t.id FROM titulek_udalost tu JOIN titulky t ON t.id=tu.titulek_id
                   JOIN clanky k ON k.titulek_id=t.id WHERE tu.udalost_id=? AND t.jazyk='cs'
                   AND k.sum=0 ORDER BY t.prvni_ts DESC LIMIT 1""", (uid,))]
            zdroj_cl = c.execute("SELECT zdroj FROM titulky WHERE id=?", (tids[0],)).fetchone()[0] \
                if tids else None
        except sqlite3.OperationalError:
            tids, zdroj_cl = [], None
        finally:
            c.close()
        if tids:
            tx = clanek_text(tids[0], path=path)
            if isinstance(tx, (list, tuple)):
                tx = tx[0] if tx else ""
            if tx:
                clanek = "\nZačátek článku (%s):\n%s" % (nazvy.get(zdroj_cl, zdroj_cl),
                                                         re.sub(r"\s+", " ", tx)[:900])
    return ("ZE SEBRANÝCH ZPRÁV (Hans je sbírá každou hodinu; uváděj médium, nic nedomýšlej, "
            "co tu není, nevíš; zdroj NENÍ Demagog):\n" + "\n".join(radky) + clanek)


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
    try:                                   # HANS_ZPRAVY_PREKLAD_V1 — jen zobrazení
        _pm = preklad_mapa(c, [r["titulek"] for r in spicka + nove + hlavni])
        for r in spicka + nove + hlavni:
            if r.get("titulek") in _pm:
                r["titulek_cs"] = _pm[r["titulek"]]
    except Exception:
        pass
    c.close()
    for r in spicka + nove:
        r["zdroj_nazev"] = nazvy.get(r["zdroj"], (r["zdroj"],))[0]
    try:                                   # HANS_ZPRAVY_CISLA_V1
        cisla = rozdily_cisel(path, hodin)
    except Exception:
        cisla = []
    return {"zdroje": zdroje, "spicka": spicka, "nove": nove, "sberu": sberu, "hlavni": hlavni,
            "skryto": skryto, "cisla": cisla,
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
        try:                                   # HANS_ZPRAVY_CISLA_V1
            _c = _db()
            print("čísla:", prepocti_cisla(_c, time.time()))
            _c.close()
        except Exception as e:
            _log.warning("čísla událostí selhala: %s", e)
        try:                                   # HANS_DEMAGOG_V1
            _c = _db()
            print("demagog:", sber_demagog(_c, time.time()))
            _c.close()
        except Exception as e:
            _log.warning("demagog selhal: %s", e)
        try:                                   # HANS_ZPRAVY_PREKLAD_V1 — jen pro čtení na webu
            from scripts.config_io import load as _cl
            _c = _db()
            print("překlad titulků:", prelozit_titulky(_c, _cl(), time.time()))
            _c.close()
        except Exception as e:
            _log.warning("překlad titulků selhal: %s", e)
    elif cmd == "demagog":                             # HANS_DEMAGOG_V1
        if len(sys.argv) > 2 and sys.argv[2] == "sber":   # demagog sber [rok …]
            _c = _db()
            print(sber_demagog(_c, time.time(), max_novych=5000,
                               roky=[int(x) for x in sys.argv[3:]] or None))
            _c.close()
        else:
            for v in demagog_hledej(" ".join(sys.argv[2:]), limit=8)["vyroky"]:
                print(v["datum"], v["verdikt"], v["mluvci"], "|", v["vyrok"][:100], v["url"])
    elif cmd == "cisla":                               # HANS_ZPRAVY_CISLA_V1
        from datetime import datetime as _dt
        if len(sys.argv) > 2 and sys.argv[2] == "prepocti":
            _c = _db(); print(prepocti_cisla(_c, time.time())); _c.close()
        for u in rozdily_cisel(hodin=float(sys.argv[2]) if len(sys.argv) > 2
                               and sys.argv[2] != "prepocti" else 48.0):
            print("%4d  %3d médií  %s" % (u["id"], u["pocet_zdroju"], (u["titulek"] or "")[:90]))
            for v in u["veliciny"]:
                print("        %-12s %s" % (v["popis"] + ":", "  →  ".join(
                    "%s %s (%s%s)" % (("%g" % r["hodnota"]), r["zdroj"],
                                      _dt.fromtimestamp(r["cas"] or 0).strftime("%d.%m. %H:%M"),
                                      "~" if r["odhad"] else "") for r in v["rada"])))
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
        _c = _db()                                     # HANS_ZPRAVY_CISLA_V1 — hlavička
        _u = _c.execute("SELECT titulek, pocet_zdroju FROM udalosti WHERE id=?",
                        (int(sys.argv[2]),)).fetchone()
        _c.close()
        if _u:
            print("%s\nmédií: %d%s" % (_u[0], _u[1] or 0,
                                     "  ⚠️ píše o tom jen jedno médium" if (_u[1] or 0) == 1 else ""))
        for _k, _r in cisla_udalosti(int(sys.argv[2])).items():
            print("čísla %-10s %s" % (_k + ":", "  →  ".join(
                "%g %s (%s)" % (r["hodnota"], r["zdroj"],
                                _dt.fromtimestamp(r["cas"] or 0).strftime("%d.%m. %H:%M")) for r in _r)))
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
