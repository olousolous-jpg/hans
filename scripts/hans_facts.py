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


def _popisky(qids: set, lang: str = "cs") -> dict:
    """QID → český (nebo anglický) název. Jedním dávkovým dotazem."""
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
                v = (L.get(lang) or L.get("en") or {}).get("value")
                if v:
                    out[q] = v
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
        for klic, (v, prop) in list(syrove.items()):
            if isinstance(v, list):       # vícehodnotová vlastnost
                syrove[klic] = (", ".join(lab.get(x, x) for x in v), prop)
            elif v in lab:
                syrove[klic] = (lab[v], prop)
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
        for klic, (hod, prop) in fakta.items():
            con.execute(
                "INSERT INTO entity_facts (entity_id, klic, hodnota, qid, prop, ts) "
                "VALUES (?,?,?,?,?,?) ON CONFLICT(entity_id, klic) DO UPDATE SET "
                "hodnota=excluded.hodnota, qid=excluded.qid, prop=excluded.prop, "
                "ts=excluded.ts",
                (entity_id, klic, str(hod), res.get("qid"), prop, now))
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
