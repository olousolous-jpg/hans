# -*- coding: utf-8 -*-
"""HANS_FACTS_V1 — strukturovaná fakta k entitám z Wikidat. BEZ LLM.

PROČ (nápad uživatele 24.8.2026): *„dát ke studiu podmínku, které základní
informace má získat — jako kde se třeba hrad nachází, sloh, vznik, majitele."*

Studium dnes plodí volnou PRÓZU, ze které se nedá ověřovat ani dotazovat.
Tenhle modul plodí FAKTA — a tím se výčtové dotazy („jaké hrady jsou v Českém
ráji") mění z generování na SELECT. To je přesně ta třída otázek, na které
ztroskotalo ladění guardů.

⛔ HLAVNÍ PAST, KVŮLI KTERÉ JE TU NULA LLM: formulář s prázdnými kolonkami je
magnet na konfabulaci — modelu se řekne „vyplň sloh" a on ho vyplní i tam, kde
ve zdroji není. Proto se hodnoty BEROU ZE ZDROJE, ne vymýšlejí:
  • zdroj = Wikidata (strukturovaná tvrzení, ne próza),
  • u každého faktu se drží PROVENIENCE (QID entity + kód vlastnosti),
  • PRÁZDNÉ POLE JE LEGITIMNÍ VÝSLEDEK — nic se nedopočítává.
Vzor je `entities.gloss` (první definiční věta zdroje verbatim, 0 % LLM).

OVĚŘENO NAŽIVO 26.8.2026:
  Kost (hrad)      → hrad, Podkost, Česko, vznik 1300, sloh gotická architektura
  Cardiffský hrad  → hrad, Spojené království, vznik 1080, sloh novogotika,
                     vlastník Cardiff Council
⚠️ Tři věci, které to při zkoušení rozbily a jsou tu proto ošetřené:
  1. `source_title` v `entities` NEODPOVÍDÁ vždy názvu článku („Hrad Kost" vs
     „Kost (hrad)") → nutné `redirects=1`, jinak QID nevznikne.
  2. Hodnoty tvrzení jsou QID, ne názvy → dobírají se JEDNÍM dávkovým dotazem.
  3. 429 (rate limit) NENÍ „neexistuje" — musí se odlišit, jinak si Hans
     odškrtne entitu jako neúspěšnou a už se k ní nevrátí.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request

_log = logging.getLogger("hans_facts")

USER_AGENT = ("HansBot/1.0 (osobni domaci asistent, nizka frekvence; "
              "kod: github.com/olousolous-jpg/hans)")
PAUZA_S = 2.0          # mezi dotazy — slušnost vůči Wikimedia API
TIMEOUT_S = 20
# ZMĚŘENO 26.8.: s pauzou 1 s přišlo HTTP 429 už po ČTYŘECH entitách. Wikimedia
# limity jsou přísnější, než se čekalo → pauza zdvojena a přidáno ustupování.
# ⚠️ Backfill je běh na dlouhou trať (uživatel 24.8.: „kvůli rate limitu tam
# můžou být delší pauzy"), takže je lepší jít pomalu než si nechat zavřít dveře.
BACKOFF_S = (5, 15, 45)   # kolik čekat po 429, než to vzdáme

# ── Schéma podle typu entity ────────────────────────────────────────────────
# 3-4 pole na typ, ne deset: čím víc kolonek, tím víc prázdna a šumu.
# `pojem` (nejpočetnější typ) tu SCHVÁLNĚ NENÍ — „poloha" a „sloh" se u pojmu
# neptají a formulář by jen sváděl k vyplňování.
SCHEMA = {
    "místo":      {"P31": "je to", "P131": "leží v", "P17": "stát",
                   "P571": "vznik", "P149": "sloh", "P127": "vlastník"},
    "organizace": {"P31": "je to", "P17": "stát", "P571": "vznik",
                   "P159": "sídlo"},
    "osoba":      {"P569": "narození", "P570": "úmrtí", "P106": "profese",
                   "P27": "národnost"},
    "dílo":       {"P31": "je to", "P50": "autor", "P577": "vydáno",
                   "P136": "žánr"},
    "postava":    {"P31": "je to", "P1441": "vystupuje v", "P50": "autor"},
    "událost":    {"P31": "je to", "P585": "datum", "P276": "místo"},
}


# Vlastnosti, které jsou z podstaty VÍCEHODNOTOVÉ — brát u nich jedinou hodnotu
# je polopravda, a polopravda v korpusu je horší než prázdno. Doloženo 26.8.:
# Terence Hill vyšel jako „filmový režisér", protože to je jeho první P106
# (Wikidata u něj nic nepovyšují) — herec zmizel. Bereme až MULTI_MAX hodnot.
MULTI = {"P106", "P136", "P50"}      # profese, žánr, autor
MULTI_MAX = 3


class RateLimit(Exception):
    """429/503 — NENÍ to „entita neexistuje". Volající musí počkat, ne
    odškrtnout entitu jako vyřízenou."""


def _get(url: str) -> dict:
    """GET s ustupováním při 429/503. Teprve když ani po BACKOFF_S neprojde,
    vyhodí `RateLimit` — a to volající NESMÍ brát jako „entita neexistuje"."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for i, cekej in enumerate((0,) + BACKOFF_S):
        if cekej:
            _log.info("hans_facts: rate limit → čekám %d s (pokus %d/%d)",
                      cekej, i, len(BACKOFF_S))
            time.sleep(cekej)
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT_S) as f:
                return json.load(f)
        except urllib.error.HTTPError as e:
            if e.code in (429, 503):
                continue
            raise
    raise RateLimit("HTTP 429/503 i po %d pokusech" % (len(BACKOFF_S) + 1))


def qid_for(source_title: str, lang: str = "cs") -> tuple:
    """Název článku → (QID, skutečný název článku). (None, None) = nenalezeno.

    `redirects=1` je NUTNÉ: Hansovy `source_title` jsou často přesměrování
    („Hrad Kost" → „Kost (hrad)"). Bez toho vrátí API `missing` a fakta se
    nikdy nenajdou — vypadalo by to, že entita ve Wikidatech není.
    """
    if not source_title:
        return None, None
    u = ("https://%s.wikipedia.org/w/api.php?action=query&prop=pageprops"
         "&redirects=1&titles=%s&format=json&formatversion=2"
         % (lang, urllib.parse.quote(source_title)))
    try:
        pg = (_get(u).get("query", {}).get("pages") or [{}])[0]
    except RateLimit:
        raise
    except Exception as e:
        _log.debug("qid_for(%r): %s", source_title, e)
        return None, None
    if pg.get("missing"):
        return None, None
    return (pg.get("pageprops") or {}).get("wikibase_item"), pg.get("title")


# HANS_FACTS_P31_V1 (2.9.) — QID → jakého je to typu (P31, „instance of“).
# Vzniklo z reálné vady: Hans na „pusť nějaký film" odpovídal glosou
# *„Fotografický film je plastový pás z polyesteru…"*. Vyhledávání Wikipedie
# je fuzzy, takže i dotaz „ten film (film)" vrátí článek o fotografickém
# materiálu — a ⛔ ani přípona „(film)", ani práh `_title_coverage` to
# nerozliší (obojí změřeno 2. 9. a zamítnuto: coverage dá „nahodny"→„Náhoda"
# 1.00, ale legitimnímu „Pulp Fiction" jen 0.50).
# Rozliší to až TYP entity ve Wikidatech, a ten je deterministický.
FILMOVE_QID = {
    "Q11424",      # film
    "Q24869",      # celovečerní film
    "Q202866",     # animovaný film
    "Q506240",     # televizní film
    "Q93204",      # dokumentární film
    "Q226730",     # němý film
    "Q24856",      # filmová série
    "Q5398426",    # televizní seriál
    "Q1259759",    # minisérie
    "Q21191270",   # díl televizního seriálu
    "Q1261214",    # televizní speciál
    "Q15416",      # televizní pořad   ┐ Kodi přehrává i pořady a díly
    "Q662197",     # Večerníček        ┘ (akce umí `find_episode`), takže
                   # obrazová díla mimo „film" sem patří taky. Doloženo:
                   # „Znovu u Spejbla a Hurvínka" má P31 = Večerníček.
    "Q24862",      # krátký film  ← doplněno po regresní kontrole 2.9.:
                   # „Násilník" → „Broken (film)" je krátký film a gate ho
                   # zamítal. Seznam typů se NEDÁ uhodnout dopředu; proto je
                   # níž `je_film_stav` tříhodnotový a chybějící typ už
                   # NEZNAMENÁ zahozenou glosu, jen „nevím".
}


def typy_qid(qid: str) -> set:
    """HANS_FACTS_P31_V1 — množina QID hodnot P31 („je instancí"). Prázdno =
    nevíme (síť, neznámá entita) — volající to NESMÍ číst jako „není film"."""
    if not qid:
        return set()
    u = ("https://www.wikidata.org/w/api.php?action=wbgetentities&ids=%s"
         "&props=claims&format=json" % qid)
    try:
        ent = (_get(u).get("entities") or {}).get(qid) or {}
    except RateLimit:
        raise
    except Exception as e:
        _log.debug("typy_qid(%r): %s", qid, e)
        return set()
    out = set()
    for c in (ent.get("claims") or {}).get("P31", []):
        dv = (c.get("mainsnak") or {}).get("datavalue") or {}
        v = (dv.get("value") or {})
        if isinstance(v, dict) and v.get("id"):
            out.add(v["id"])
    return out


def je_film_stav(source_title: str, lang: str = "cs"):
    """TŘI hodnoty: True = film · False = prokazatelně NENÍ film · None = nevím.

    ⚠️ Rozdíl mezi False a None je tu ten podstatný. První verze (V1) vracela
    jen True/False a „nevím" se chovalo jako „není film" — regresní kontrola
    hned ukázala, co to dělá: `Q24862` (krátký film) v seznamu chyběl, takže
    Hans přišel o glosu ke skutečnému filmu. Seznam typů nejde uhodnout
    dopředu, takže se zahazuje JEN při doložené neshodě.
    """
    try:
        q, _ = qid_for(source_title, lang)
        if not q:
            return None
        typy = typy_qid(q)
        if not typy:
            return None
        return bool(typy & FILMOVE_QID)
    except Exception as e:
        _log.debug("je_film_stav(%r): %s", source_title, e)
        return None


def je_film(source_title: str, lang: str = "cs") -> bool:
    """Zpětně kompatibilní obal: „nevím" → False. Na rozhodnutí, které něco
    ZAHAZUJE, použij `je_film_stav` a testuj `is False`."""
    return je_film_stav(source_title, lang) is True


# HANS_WIKI_NAMESAKE_V1 (5.9.) - CLANEK POJMENOVANY PO NEKOM NENI ODPOVED O NEM.
#
# Dolozeno zivym rozhovorem 5.9.: "Cetl jste neco od Karla Capka?" -> Hans
# odpovedel popisem ULICE V PISKU a chystal se to v noci zapsat do pameti.
# Pricina neni v prahu: kotva je ve sklonovanem tvaru ("Karla Capka") a
# cs.wikipedia ma clanek presne toho jmena, protoze ceske ulice se jmenuji
# 2. padem osob. Zmereno - `_title_similarity`/`_title_coverage` daji ulici
# 1.00/1.00, kdezto spravnemu "Karel Capek" 0.00/0.00, takze zadne
# prerovnani vysledku to nespravi. Rozhodne az VLASTNOST P138 (je
# pojmenovano po), a ta miri presne na spravny clanek.
#
# ZMERENO NA REALNYCH DATECH, A MERENI ZABILO NAIVNI VERZI: ze 40 entit
# s nejvyssi evidenci maji P138 tri - "Tour de France" -> Francie,
# "Zlate oko" -> dum Goldeneye, "Eden - Vitejte v raji" -> Eden. Samotne
# P138 by je vsechny presmerovalo SPATNE. Proto se prepina jen tehdy,
# kdyz je ten, po kom je to pojmenovano, CLOVEK (P31 = Q5) - tim vsechny
# tri odpadnou uz na typu.
#
# ⚠️ Poradi podminek je vec mereni, ne vkusu; obe strany overeny na sedmi
# pripadech (viz `regrese_helpers`). Levny predfiltr `_jmenoveho_tvaru`
# odmitne 71 % realnych entit ZDARMA, takze se na Wikidata chodi jen u
# tvaru, kde tahle kolize vubec muze nastat.
#
# ⛔ "Nevim" NENI "neni pojmenovano po cloveku" - sit dole, 429, chybejici
# P138 i chybejici sitelink vraceji None a volajici nechava puvodni clanek.
# Je to tentyz trihodnotovy slib jako u `je_film_stav` vys.
LIDSKE_QID = "Q5"


def _n_tokeny(text: str) -> list:
    """Slova titulu bez zavorkoveho upresneni ("Karla Capka (Pisek)")."""
    import re as _re
    text = _re.sub(r"\([^)]*\)", " ", text or "")
    return [w for w in _re.findall(r"[^\W\d_]+", text, _re.UNICODE) if len(w) > 1]


def _jmenoveho_tvaru(titul: str) -> bool:
    """Vypada titul jako holé JMENO (>=2 slova, vsechna velkym)?

    Levny predfiltr, aby se na Wikidata nechodilo u kazdeho dohledani.
    Zmereno na 551 realnych entitach: projde 29 %. Odmitne zdarma prave to,
    co ma v nazvu druhove slovo malym pismenem ("Cardiffsky hrad",
    "Zlate oko", "Tour de France") - tedy vetsinu falesnych kandidatu.
    """
    ws = _n_tokeny(titul)
    return len(ws) >= 2 and all(w[:1].isupper() for w in ws)


def _stejne_jmeno(titul: str, jmeno: str) -> bool:
    """Je `titul` jen JMENO te osoby, bez druhoveho slova navic?

    Porovnava se po dvojicich na tri znaky, protoze cesky 2. pad meni
    koncovku i kmen: "Karla Capka" x "Karel Capek" se na cely tvar ani na
    kmen bez samohlasky NESHODUJI (prchave -e-), na prefix ano.
    Volnost prefixu tu nevadi - porovnava se titul s popiskem entity, na
    kterou UKAZUJE SAM SEBOU pres P138, ne dva cizi retezce.
    """
    a, b = _n_tokeny(titul), _n_tokeny(jmeno)
    if not a or not b or len(a) != len(b):
        return False
    return all(x[:3].lower() == y[:3].lower() for x, y in zip(a, b))


def _cs_clanek(qid: str, lang: str = "cs"):
    """QID -> nazev clanku v dane jazykove mutaci (None = nema ho)."""
    if not qid:
        return None
    u = ("https://www.wikidata.org/w/api.php?action=wbgetentities&ids=%s"
         "&props=sitelinks&format=json" % qid)
    try:
        ent = (_get(u).get("entities") or {}).get(qid) or {}
    except RateLimit:
        raise
    except Exception as e:
        _log.debug("_cs_clanek(%r): %s", qid, e)
        return None
    return ((ent.get("sitelinks") or {}).get("%swiki" % lang) or {}).get("title")


def pojmenovano_po_cloveku(nalezeny_titul: str, dotaz: str,
                           lang: str = "cs"):
    """Nazev clanku o CLOVEKU, po kterem je `nalezeny_titul` pojmenovan.

    None = nechat puvodni clanek (nevim / neni to tenhle pripad).

    Prepne se JEN kdyz plati vsechno:
      1. titul je jmenoveho tvaru (levny predfiltr),
      2. dotaz je obsazen v titulu - jinak se resi jina vec,
      3. titul ma P138 a jeho cil je CLOVEK (P31 = Q5),
      4. uzivatel si o ten druh veci nerekl: bud je dotaz vlastni
         podmnozinou titulu ("Karla Capka" v "Pamatnik Karla Capka"),
         nebo je titul jen jmeno te osoby ("Bozeny Nemcove").
         Kdyz se nekdo zepta primo na "Pamatnik Karla Capka", nesplni ani
         jedno a pamatnik mu zustane.
    """
    if not nalezeny_titul or not dotaz:
        return None
    if not _jmenoveho_tvaru(nalezeny_titul):
        return None
    t_dotaz = [w.lower() for w in _n_tokeny(dotaz)]
    t_titul = [w.lower() for w in _n_tokeny(nalezeny_titul)]
    if not t_dotaz or not set(t_dotaz) <= set(t_titul):
        return None
    try:
        qid, _ = qid_for(nalezeny_titul, lang)
        if not qid:
            return None
        time.sleep(PAUZA_S)
        u = ("https://www.wikidata.org/w/api.php?action=wbgetentities&ids=%s"
             "&props=claims&format=json" % qid)
        claims = ((_get(u).get("entities") or {}).get(qid) or {}).get("claims") or {}
        cile = []
        for c in claims.get("P138", []):
            v = ((c.get("mainsnak") or {}).get("datavalue") or {}).get("value") or {}
            if isinstance(v, dict) and v.get("id"):
                cile.append(v["id"])
        if not cile:
            return None
        for cil in cile:
            time.sleep(PAUZA_S)
            if LIDSKE_QID not in typy_qid(cil):
                continue
            clanek = _cs_clanek(cil, lang)
            if not clanek:
                continue
            uzsi = set(t_dotaz) < set(t_titul)
            if not (uzsi or _stejne_jmeno(nalezeny_titul, clanek)):
                _log.info("HANS_WIKI_NAMESAKE_V1: %r nechavam - dotaz %r si "
                          "o ten druh veci rekl sam", nalezeny_titul, dotaz)
                return None
            _log.info("HANS_WIKI_NAMESAKE_V1: %r je pojmenovano po cloveku "
                      "%r -> beru jeho clanek (dotaz %r)",
                      nalezeny_titul, clanek, dotaz)
            return clanek
    except RateLimit:
        _log.info("HANS_WIKI_NAMESAKE_V1: rate limit - nechavam puvodni clanek")
        return None
    except Exception as e:
        _log.debug("pojmenovano_po_cloveku(%r): %s", nalezeny_titul, e)
        return None
    return None


def _popisky(qids: set, lang: str = "cs") -> dict:
    """QID → (český název, ANGLICKÝ název). Jedním dávkovým dotazem.

    HANS_FACTS_EN_V1 (26.8.) — angličtina se dřív tahala a ZAHAZOVALA. Přitom
    je to přesně to, co potřebuje obrazový model: „Neo-Gothic architecture"
    se dá do promptu připojit DETERMINISTICKY, kdežto české „novogotika" musí
    někdo přeložit — a když se o to poprosí LLM, dosadí si svoje.
    Doloženo: fakt `sloh: novogotika` → model napsal „Norman architecture".
    """
    if not qids:
        return {}
    out = {}
    ids = sorted(qids)
    for i in range(0, len(ids), 45):          # API bere max 50 id na dotaz
        u = ("https://www.wikidata.org/w/api.php?action=wbgetentities&ids=%s"
             "&props=labels&languages=%s|en&format=json"
             % ("|".join(ids[i:i + 45]), lang))
        try:
            for q, e in (_get(u).get("entities") or {}).items():
                L = e.get("labels") or {}
                cs = (L.get(lang) or {}).get("value")
                en = (L.get("en") or {}).get("value")
                if cs or en:
                    out[q] = (cs or en, en or "")
        except RateLimit:
            raise
        except Exception as e:
            _log.debug("popisky: %s", e)
    return out


def facts_for(source_title: str, etype: str, lang: str = "cs") -> dict:
    """Fakta k entitě podle jejího typu. Vrací
    {"qid":…, "clanek":…, "fakta": {klíč: (hodnota, vlastnost)}}.

    Prázdná `fakta` = legitimní výsledek (entita je, ale tvrzení nemá).
    `qid=None` = článek/QID se nenašel — to je něco JINÉHO než prázdná fakta.
    """
    pole = SCHEMA.get(etype or "")
    if not pole:
        return {"qid": None, "clanek": None, "fakta": {}, "duvod": "typ bez schématu"}
    qid, clanek = qid_for(source_title, lang)
    if not qid:
        return {"qid": None, "clanek": None, "fakta": {}, "duvod": "QID nenalezen"}
    time.sleep(PAUZA_S)
    u = ("https://www.wikidata.org/w/api.php?action=wbgetentities&ids=%s"
         "&props=claims&format=json" % qid)
    claims = ((_get(u).get("entities") or {}).get(qid) or {}).get("claims") or {}

    syrove, k_dobrani = {}, set()
    for prop, klic in pole.items():
        tvrzeni = claims.get(prop) or []
        if not tvrzeni:
            continue                      # prázdno je LEGITIMNÍ, nedopočítávat
        # Wikidata dávají tvrzením POŘADÍ DŮLEŽITOSTI (`rank`). Brát slepě
        # první je chyba: Terence Hill vyšel jako „filmový režisér", protože
        # to bylo první P106 — herec má ale rank `preferred`. Deprecated
        # tvrzení (vyvrácená) se vynechávají úplně.
        platna = [t for t in tvrzeni
                  if (t.get("rank") or "normal") != "deprecated"]
        if not platna:
            continue
        prednostni = [t for t in platna if t.get("rank") == "preferred"]
        vyber = (prednostni or platna)[:MULTI_MAX if prop in MULTI else 1]
        hodnoty = []
        for t in vyber:
            d = (t.get("mainsnak") or {}).get("datavalue", {}).get("value")
            if isinstance(d, dict) and d.get("id"):
                hodnoty.append(d["id"])
                k_dobrani.add(d["id"])
        if hodnoty:
            syrove[klic] = (hodnoty if len(hodnoty) > 1 else hodnoty[0], prop)
            continue
        dv = (vyber[0].get("mainsnak") or {}).get("datavalue", {}).get("value")
        if isinstance(dv, dict) and dv.get("id"):
            syrove[klic] = (dv["id"], prop)
            k_dobrani.add(dv["id"])
        elif isinstance(dv, dict) and dv.get("time"):
            # +1300-00-00T… → „1300"; přesnější formát nepotřebujeme
            syrove[klic] = (str(dv["time"])[1:5].lstrip("0"), prop)
        elif isinstance(dv, str):
            syrove[klic] = (dv, prop)

    if k_dobrani:
        time.sleep(PAUZA_S)
        lab = _popisky(k_dobrani, lang)

        def _cs(x):
            return lab.get(x, (x, ""))[0]

        def _en(x):
            return lab.get(x, ("", ""))[1]

        for klic, (v, prop) in list(syrove.items()):
            if isinstance(v, list):       # vícehodnotová vlastnost
                syrove[klic] = (", ".join(_cs(x) for x in v), prop,
                                ", ".join(e for e in (_en(x) for x in v) if e))
            elif v in lab:
                syrove[klic] = (_cs(v), prop, _en(v))
    return {"qid": qid, "clanek": clanek, "fakta": syrove, "duvod": ""}


# ── Uložení ─────────────────────────────────────────────────────────────────
# Samostatná tabulka, ne sloupce v `entities`: hlavní zisk celé věci je, že se
# výčtový dotaz stane SELECTem („které entity mají leží_v ~ Český ráj"), a to
# se sloupci s pevným počtem polí nejde.

def ensure_table(db_path: str) -> None:
    con = sqlite3.connect(db_path, timeout=10)
    try:
        con.execute("""CREATE TABLE IF NOT EXISTS entity_facts (
            entity_id INTEGER NOT NULL,
            klic      TEXT NOT NULL,
            hodnota   TEXT NOT NULL,
            qid       TEXT,
            prop      TEXT,
            ts        REAL NOT NULL,
            PRIMARY KEY (entity_id, klic))""")
        # HANS_FACTS_EN_V1 — přidáno později, proto ALTER (starší DB ho nemá).
        try:
            con.execute("ALTER TABLE entity_facts ADD COLUMN hodnota_en TEXT")
        except Exception:
            pass                       # sloupec už existuje
        con.execute("CREATE INDEX IF NOT EXISTS idx_ef_klic "
                    "ON entity_facts(klic, hodnota)")
        con.commit()
    finally:
        con.close()


def save_facts(db_path: str, entity_id: int, res: dict) -> int:
    """Ulož fakta k entitě. Vrací počet zapsaných polí (0 = nic k uložení)."""
    fakta = (res or {}).get("fakta") or {}
    if not fakta:
        return 0
    ensure_table(db_path)
    now = time.time()
    con = sqlite3.connect(db_path, timeout=10)
    try:
        for klic, polozka in fakta.items():
            hod, prop = polozka[0], polozka[1]
            en = polozka[2] if len(polozka) > 2 else ""
            con.execute(
                "INSERT INTO entity_facts "
                "(entity_id, klic, hodnota, hodnota_en, qid, prop, ts) "
                "VALUES (?,?,?,?,?,?,?) ON CONFLICT(entity_id, klic) DO UPDATE SET "
                "hodnota=excluded.hodnota, hodnota_en=excluded.hodnota_en, "
                "qid=excluded.qid, prop=excluded.prop, ts=excluded.ts",
                (entity_id, klic, str(hod), en or None, res.get("qid"), prop, now))
        con.commit()
    finally:
        con.close()
    return len(fakta)


# ── Backfill ────────────────────────────────────────────────────────────────
# Dávkově a RESUMOVATELNĚ. Nepotřebuje model → nesoutěží o VRAM a běží i
# s vypnutým PC, čistě na Pi ([[study-vram-handoff]] se ho netýká).

def _stav_tabulka(db_path: str) -> None:
    con = sqlite3.connect(db_path, timeout=10)
    try:
        con.execute("""CREATE TABLE IF NOT EXISTS entity_facts_stav (
            entity_id INTEGER PRIMARY KEY,
            vysledek  TEXT NOT NULL,   -- ok | bez_qid | bez_tvrzeni | typ_bez_schematu
            poli      INTEGER DEFAULT 0,
            ts        REAL NOT NULL)""")
        con.commit()
    finally:
        con.close()


def kandidati(db_path: str, limit: int = 20) -> list:
    """Entity, které ještě nebyly zkoušené. Nejdřív ty, o kterých Hans ví nejvíc.

    ⚠️ Vrací JEN typy, které mají schéma — `pojem` se ani nezkouší, aby se
    zbytečně nechodilo na síť.
    """
    _stav_tabulka(db_path)
    con = sqlite3.connect(db_path, timeout=10)
    try:
        typy = [t for t in SCHEMA]
        q = ("SELECT e.id, e.name, e.etype, e.source_title FROM entities e "
             "LEFT JOIN entity_facts_stav s ON s.entity_id = e.id "
             "WHERE s.entity_id IS NULL AND coalesce(e.source_title,'') <> '' "
             "AND e.etype IN (%s) ORDER BY e.evidence_count DESC LIMIT ?"
             % ",".join("?" * len(typy)))
        return con.execute(q, typy + [limit]).fetchall()
    finally:
        con.close()


def backfill(db_path: str, limit: int = 20, pauza: float = None) -> dict:
    """Doplň fakta pro N dosud nezkoušených entit. Vrací souhrn.

    ⚠️ `RateLimit` běh UKONČÍ, ale entitu NEODŠKRTNE — vrátíme se k ní příště.
    Zaměnit „došel limit" za „entita neexistuje" by ji vyřadilo napořád
    ([[study-findability-guards]]).
    """
    _stav_tabulka(db_path)
    p = PAUZA_S if pauza is None else pauza
    souhrn = {"zkouseno": 0, "ok": 0, "poli": 0, "bez_qid": 0,
              "bez_tvrzeni": 0, "rate_limit": False}
    for eid, name, etype, titul in kandidati(db_path, limit):
        try:
            r = facts_for(titul, etype)
        except RateLimit as e:
            _log.warning("backfill: rate limit u %r (%s) — končím, "
                         "entita ZŮSTÁVÁ nezkoušená", name, e)
            souhrn["rate_limit"] = True
            break
        except Exception as e:
            _log.warning("backfill: %r selhalo (%s) — nechávám na příště",
                         name, e)
            continue                       # NEodškrtávat, chyba ≠ výsledek
        souhrn["zkouseno"] += 1
        n = save_facts(db_path, eid, r)
        if r.get("qid") and n:
            vysledek, souhrn["ok"] = "ok", souhrn["ok"] + 1
            souhrn["poli"] += n
        elif r.get("qid"):
            vysledek = "bez_tvrzeni"       # entita je, tvrzení nemá — LEGITIMNÍ
            souhrn["bez_tvrzeni"] += 1
        else:
            vysledek = "bez_qid"
            souhrn["bez_qid"] += 1
        con = sqlite3.connect(db_path, timeout=10)
        try:
            con.execute("INSERT OR REPLACE INTO entity_facts_stav "
                        "(entity_id, vysledek, poli, ts) VALUES (?,?,?,?)",
                        (eid, vysledek, n, time.time()))
            con.commit()
        finally:
            con.close()
        _log.info("backfill: %s [%s] → %s (%d polí)", name, etype, vysledek, n)
        time.sleep(p)
    return souhrn
