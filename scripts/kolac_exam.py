#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""KOLAC_EXAM_V1 (22.8.) — Koláč jako ZKOUŠEJÍCÍ, ne jen společník.

PROČ (nápad uživatele 22.8.): to nejlepší, co dnes o Hansových chybách víme,
pochází z rozhovorů, které s ním vedeme RUČNĚ (`test_rozhovor.py`) — statická
regrese by ani jednu z dnešních čtyř oprav nenašla. Tenhle modul dělá totéž
každý den sám: Koláč se místo debaty občas ZEPTÁ, Hans odpoví SVOU BĚŽNOU
CHATOVOU CESTOU a odpověď se porovná s opravným klíčem.

TŘI ZDROJE OTÁZEK, každý měří něco jiného:
  • `pamet` — téma, o kterém Hans MÁ zápisek. Klíč = ten zápisek.
    Abstinence tady NENÍ ctnost: „nemám záznam" u věci, kterou si zapsal,
    je zapření (doloženo 15.7. u knih, znovu 21.8. u hradu Kost).
  • `wiki`  — náhodné heslo, o kterém nejspíš NIC nemá. Klíč = text článku.
    Správně má buď přiznat neznalost, nebo dohledat (HANS_INSTANT_LOOKUP_V1).
  • `soubor` — naše vlastní otázky s očekávanou odpovědí (`data/kolac_otazky.txt`).
    Sem patří doložené případy, ať se nevracejí.

CO SE ZÁMĚRNĚ NEDĚLÁ:
  • **Zkoušení se NEUKLÁDÁ do paměti.** Hans odpovídá pod testovací identitou
    (`HANS_TEST_PERSON_V1`), takže zápis do deníku ani RAG neproběhne. Bez
    toho by si výmysly ze zkoušení uložil jako vlastní zážitek a příště je
    „vybavil" — vyrobili bychom přesně tu konfabulaci, kterou hasíme.
  • **Nehodnotí se LLM soudcem.** Porovnání dělá `grounding_guard` (věta po
    větě proti klíči) — deterministicky, bez VRAM a bez další konfabulace.
    Model, který by soudil model, je jen třetí názor téhož druhu.
"""
from __future__ import annotations

import json
import logging
import os
import random
import re
import sqlite3
import time
from typing import Optional

_log = logging.getLogger("kolac_exam")

SOUBOR_OTAZEK = "data/kolac_otazky.txt"

# Zdroje se střídají dokola, ať jeden nepřeváží (a ať je vzorek pestrý).
_ZDROJE = ("pamet", "wiki", "soubor")

# Typy deníkových záznamů, které nesou ZNALOST (ne Hansovo mluvení).
# Shodné s `hans_recall._READ_TYPES` + studijní poznámky.
_ZNALOSTI = ("reading_takeaway", "study_note", "book_reflection",
             "study_mastery")

# Formulace otázky se střídají SCHVÁLNĚ: dnešní chyby ukázaly, že rozdíl
# mezi „co víš o X" a „potřebuji více informací o X" rozhoduje o tom, jestli
# se vůbec najde podklad (HANS_CONVINDEX_ANCHOR_V1).
_FORMULACE = (
    "co víš o %s?",
    "řekni mi něco o %s",
    "co mi můžeš říct o %s?",
    "potřebuji více informací o %s",
    "znáš %s?",
)

# Jak Koláč otázku uvede v přepisu rozhovoru (do promptu NEJDE — tam míří
# holá otázka, ať se zkouší táž cesta jako u člověka).
_UVOD = (
    "Zkouška, pane majordome.",
    "Prověřím vás, jestli dovolíte.",
    "Otázka na vaši paměť:",
    "A teď něco, co si ověřím:",
)

# HANS_ANCHOR_LOOKUP_ON_ADMIT_V1 (22.8.) — odříkací obraty i zdvořilost si
# bere ze `hans_thread`. Dřív tu byl vlastní regex vedle `_ABSTAIN_MARKS`
# v `hans_thread` = dvě pravdy o téže věci, každá s jinými dírami (tahle
# neznala „nemám v zápiscích", tamta „nemám ŽÁDNÉ záznamy").
from scripts.hans_thread import je_odrikaci as _je_priznani  # noqa: E402
from scripts.hans_thread import _ZDVORILOST                  # noqa: E402


class _Abstinence:
    """Tvar `.search(text)` — ať zůstane čitelné, co se testuje."""

    @staticmethod
    def search(text):
        return _je_priznani(text or "")


_ABSTINENCE = _Abstinence()

_DOHLEDANI = re.compile(
    r"pr[áa]v[ěe] jsem se pod[íi]val|Wikipedie — heslo|"
    r"berte to zat[íi]m s rezervou", re.IGNORECASE)


def _cfg(config: dict) -> dict:
    return (config or {}).get("kolac_exam", {}) or {}


def zapnuto(config: dict) -> bool:
    return bool(_cfg(config).get("enabled", True))


def identita(config: dict) -> str:
    """Jméno, pod kterým se Hans zkouší. MUSÍ být v `config.test_persons`,
    jinak by se zkoušení zapsalo do deníku i RAG jako skutečný hovor."""
    return str(_cfg(config).get("identita", "zkouška") or "zkouška")


def dnes_zkousek(db_path: str) -> int:
    """Kolik zkoušek už dnes proběhlo (od půlnoci, místní čas)."""
    try:
        _init_db(db_path)
        with _conn(db_path) as db:
            _p = time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1))
            return int(db.execute(
                "SELECT COUNT(*) FROM kolac_exam WHERE ts >= ?",
                (_p,)).fetchone()[0])
    except Exception:
        return 0


def je_na_rade(config: dict, poradi: int, db_path: str = None) -> bool:
    """Střídání s běžným rozhovorem: každý N-tý dialog je zkouška.

    KOLAC_EXAM_DENNI_STROP_V1 (22.8., rozhodl uživatel) — a nejvýš
    `max_denne` zkoušek za den. Samotný podíl NESTAČÍ: dialogů bývá
    20–50 denně (změřeno za 14 dní), takže `kazdy_naty: 2` dělalo
    10–25 zkoušek — víc, než kdo přečte, a půlka Koláčova dialogu
    (koníčky → studium, syntéza, evoluce postojů) přišla vniveč.
    Hodnota zkoušení je v nálezech, ne v objemu.

    Strop se počítá z DB, ne z paměti procesu: `_dialog_count`
    se restartem Hanse nuluje, kdežto zkoušky mají datum.
    `db_path=None` = bez stropu (zpětná kompatibilita a testy)."""
    if not zapnuto(config):
        return False
    n = int(_cfg(config).get("kazdy_naty", 2))
    if not (n > 0 and poradi > 0 and poradi % n == 0):
        return False
    strop = int(_cfg(config).get("max_denne", 2))
    if db_path and strop > 0 and dnes_zkousek(db_path) >= strop:
        return False
    return True


# ── ÚLOŽIŠTĚ ────────────────────────────────────────────────────────────────

def _conn(db_path: str):
    c = sqlite3.connect(db_path, timeout=5.0)
    c.row_factory = sqlite3.Row
    return c


def _init_db(db_path: str) -> None:
    with _conn(db_path) as db:
        db.execute("""CREATE TABLE IF NOT EXISTS kolac_exam (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL,
            zdroj TEXT, tema TEXT, otazka TEXT, odpoved TEXT,
            verdikt TEXT, bez_opory INTEGER, detail TEXT, url TEXT)""")
        # KOLAC_EXAM_CONFIRM_V1 — migrace za běhu: tabulka už v provozu je,
        # `ALTER` v `try` je levnější než verzování schématu kvůli jednomu
        # sloupci. NULL = nález nikdo neposoudil, 1 = potvrzen (je z něj
        # trvalá otázka), -1 = zamítnut (nepřipomínat).
        try:
            _sl = {r[1] for r in db.execute("PRAGMA table_info(kolac_exam)")}
            if "potvrzeno" not in _sl:
                db.execute("ALTER TABLE kolac_exam ADD COLUMN "
                           "potvrzeno INTEGER")
            # KOLAC_EXAM_KLIC_SLOUPEC_V1 — opravný klíč se MUSÍ uložit.
            # `detail` nese Hansovy NEPODLOŽENÉ věty (tedy ten výmysl);
            # udělat z nich při potvrzení „správnou odpověď" by konfabulaci
            # povýšilo na pravdu. Klíč má proto vlastní sloupec.
            if "klic" not in _sl:
                db.execute("ALTER TABLE kolac_exam ADD COLUMN klic TEXT")
        except Exception as _me:
            _log.warning("migrace kolac_exam.potvrzeno selhala: %s", _me)
        db.commit()


def zapis(db_path: str, polozka: dict, odpoved: str,
          hodnoceni: dict) -> int:
    """Zapiš zkoušku, vrať její id (0 = nezapsáno).

    KOLAC_EXAM_NOTIFY_V1 — id je potřeba, aby šlo nález ADRESOVAT
    (`/nalez <id>`); bez něj by se hlášení nedalo potvrdit."""
    try:
        _init_db(db_path)
        with _conn(db_path) as db:
            _cur = db.execute(
                "INSERT INTO kolac_exam (ts, zdroj, tema, otazka, odpoved, "
                "verdikt, bez_opory, detail, url, klic) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (time.time(), polozka.get("zdroj", ""), polozka.get("tema", ""),
                 polozka.get("otazka", ""), (odpoved or "")[:4000],
                 hodnoceni.get("verdikt", ""), int(hodnoceni.get("bez_opory", 0)),
                 (hodnoceni.get("detail") or "")[:500], polozka.get("url", ""),
                 _zkrat(polozka.get("klic"), 1200)))
            db.commit()
            return int(_cur.lastrowid or 0)
    except Exception as e:
        _log.warning("zápis zkoušky selhal: %s", e)
        return 0


def pocet_zkousek(db_path: str) -> int:
    try:
        _init_db(db_path)
        with _conn(db_path) as db:
            return int(db.execute(
                "SELECT COUNT(*) FROM kolac_exam").fetchone()[0])
    except Exception:
        return 0


# ── NÁLEZY: OD HLÁŠENÍ K TRVALÉ OTÁZCE ──────────────────────────────────────
# KOLAC_EXAM_CONFIRM_V1 (22.8., rozhodl uživatel) — druhý krok za hlášením.
# Samo hlášení nic nezmění: příště se táž chyba zeptá znovu jen náhodou.
# Potvrzený nález se proto zapíše do souboru vlastních otázek (zdroj `soubor`),
# odkud se vrací napořád — doložený případ se tím nemůže tiše vrátit zpátky.
# ⚠️ Posuzuje ČLOVĚK, ne stroj: verdikt zatím není spolehlivý (hodnocení se
# dnes dvakrát spletlo) a nechat Hanse učit se z vlastní vymyšlené odpovědi
# je přesně ta otrava paměti, kvůli které zkoušení běží pod testovací
# identitou. Automatické učení až podle shody verdiktů s lidským čtením.


def nalezy(db_path: str, limit: int = 10) -> list:
    """Neposouzené nálezy (nejnovější první). Vrací seznam dict."""
    try:
        _init_db(db_path)
        with _conn(db_path) as db:
            rows = db.execute(
                "SELECT id, ts, zdroj, tema, otazka, verdikt, bez_opory "
                "FROM kolac_exam WHERE potvrzeno IS NULL AND verdikt IN "
                "(?,?) ORDER BY id DESC LIMIT ?",
                (_HLASIT_VYCHOZI[0], _HLASIT_VYCHOZI[1], int(limit))).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        _log.warning("čtení nálezů selhalo: %s", e)
        return []


def _radek_souboru(otazka: str, klic: str) -> str:
    """Jeden řádek souboru otázek: „otázka | klíč". Svislítko v textu by
    formát rozbilo (klíč by se uřízl), proto se nahrazuje."""
    o = " ".join(str(otazka or "").split()).replace("|", "/")
    k = " ".join(str(klic or "").split()).replace("|", "/")
    return "%s | %s" % (o, k)


def potvrd(db_path: str, exam_id: int, config: dict) -> str:
    """Udělej z nálezu trvalou zkušební otázku. Vrací hlášku pro uživatele."""
    try:
        _init_db(db_path)
        with _conn(db_path) as db:
            r = db.execute(
                "SELECT id, otazka, odpoved, verdikt, zdroj, tema, klic "
                "FROM kolac_exam WHERE id = ?", (int(exam_id),)).fetchone()
    except Exception as e:
        return "K nálezu jsem se nedostal, pane (%s)." % e
    if not r:
        return "Nález #%s neznám, pane." % exam_id
    # Klíč ze sloupce `klic` (KOLAC_EXAM_KLIC_SLOUPEC_V1). Starší zkoušky
    # ho nemají — otázka bez klíče je k ničemu, není se s čím porovnat,
    # tak o řádek požádáme. NIKDY nebrat `detail`: tam jsou Hansovy
    # nepodložené věty, tedy přesně ten výmysl.
    klic = " ".join(str(r["klic"] or "").split())
    if len(klic) < 20:
        return ("K nálezu #%s nemám uložený klíč, pane — dopište prosím řádek "
                'ručně do %s ve tvaru: otázka | správná odpověď.'
                % (exam_id, _cfg(config).get("soubor", SOUBOR_OTAZEK)))
    cesta = str(_cfg(config).get("soubor", SOUBOR_OTAZEK))
    radek = _radek_souboru(r["otazka"], klic)
    try:
        stavajici = ""
        if os.path.exists(cesta):
            with open(cesta, encoding="utf-8") as f:
                stavajici = f.read()
        _norm = " ".join(str(r["otazka"] or "").split()).lower()
        if _norm and _norm in stavajici.lower():
            _oznac(db_path, exam_id, 1)
            return ("Tuhle otázku už mezi zkušebními mám, pane — nález #%s "
                    "beru za vyřízený." % exam_id)
        with open(cesta, "a", encoding="utf-8") as f:
            if stavajici and not stavajici.endswith("\n"):
                f.write("\n")
            f.write(radek + "\n")
    except Exception as e:
        return "Zápis do souboru otázek selhal, pane: %s" % e
    _oznac(db_path, exam_id, 1)
    return ("Zapsal jsem si to jako trvalou zkušební otázku, pane:\n%s\n"
            "(soubor %s — klíč můžete kdykoli upravit ručně.)"
            % (_zkrat(radek, 300), cesta))


def zamitni(db_path: str, exam_id: int) -> str:
    if _oznac(db_path, exam_id, -1):
        return "Dobrá, pane — nález #%s už připomínat nebudu." % exam_id
    return "Nález #%s se mi označit nepodařilo, pane." % exam_id


def _oznac(db_path: str, exam_id: int, hodnota: int) -> bool:
    try:
        _init_db(db_path)
        with _conn(db_path) as db:
            db.execute("UPDATE kolac_exam SET potvrzeno = ? WHERE id = ?",
                       (int(hodnota), int(exam_id)))
            db.commit()
        return True
    except Exception as e:
        _log.warning("označení nálezu selhalo: %s", e)
        return False


# ── VÝBĚR OTÁZKY ────────────────────────────────────────────────────────────

def vyber_otazku(config: dict, db_path: str) -> Optional[dict]:
    """Vrátí {zdroj, tema, otazka, klic, url} nebo None (pak se vede běžný
    rozhovor — zkouška se nikdy nevynucuje na sílu)."""
    povolene = [z for z in _ZDROJE
                if _cfg(config).get("zdroje", list(_ZDROJE)) and
                z in (_cfg(config).get("zdroje") or list(_ZDROJE))]
    if not povolene:
        return None
    # round-robin podle počtu dosavadních zkoušek + jeden náhradní pokus,
    # když zvolený zdroj zrovna nic nemá (prázdný soubor, Wikipedie mimo).
    start = pocet_zkousek(db_path) % len(povolene)
    for i in range(len(povolene)):
        zdroj = povolene[(start + i) % len(povolene)]
        try:
            if zdroj == "pamet":
                p = _z_pameti(config, db_path)
            elif zdroj == "wiki":
                p = _z_wiki(config)
            else:
                p = _ze_souboru(config)
        except Exception as e:
            _log.warning("zdroj %s selhal: %s", zdroj, e)
            p = None
        if p:
            return p
    return None


def _otazka_na(tema: str, seed: int = None) -> str:
    r = random.Random(seed if seed is not None else time.time())
    return r.choice(_FORMULACE) % tema


def uvod_kolace(seed: int = None) -> str:
    r = random.Random(seed if seed is not None else time.time())
    return r.choice(_UVOD)


def _tema_ze_zaznamu(event_type: str, title: str) -> str:
    """Z titulku deníku udělej téma otázky. `study_note` má titulek
    „Studium: <téma> — <krok>"; ptát se má smysl na ten krok."""
    t = (title or "").strip()
    if event_type in ("study_note", "study_mastery") and "—" in t:
        t = t.split("—", 1)[1].strip()          # „Studium: X — KROK" → krok
    elif event_type == "book_reflection" and "—" in t:
        t = t.split("—", 1)[0].strip()          # „Kniha — kap. 92" → kniha
    if t.lower().startswith("studium:"):
        t = t.split(":", 1)[1].strip()
    return t


def _z_pameti(config: dict, db_path: str) -> Optional[dict]:
    """Téma, o kterém Hans MÁ zápisek — klíčem je ten zápisek."""
    dnu = float(_cfg(config).get("pamet_dnu", 60))
    od = time.time() - dnu * 86400
    with _conn(db_path) as db:
        rows = db.execute(
            "SELECT event_type, title, COALESCE(NULLIF(data,''), note) AS obsah "
            "FROM diary WHERE event_type IN (%s) AND ts > ? "
            "AND COALESCE(NULLIF(data,''), note) IS NOT NULL "
            "ORDER BY RANDOM() LIMIT 30" % ",".join("?" * len(_ZNALOSTI)),
            (*_ZNALOSTI, od)).fetchall()
    for r in rows:
        tema = _tema_ze_zaznamu(r["event_type"], r["title"])
        obsah = (r["obsah"] or "").strip()
        if len(tema) < 3 or len(obsah) < 120:
            continue
        # Klíčem NENÍ jeden zápisek, ale VŠECHNO, co si k tomu titulku zapsal.
        # Doloženo prvním živým během: na téma „Design" (11 zápisků) odpověděl
        # Hans obsáhle a správně, ale proti JEDNOMU zápisku vyšlo šest vět
        # „bez opory" — měřili bychom, jak přesně cituje jeden lístek, ne
        # jestli si vymýšlí.
        return {"zdroj": "pamet", "tema": tema, "otazka": _otazka_na(tema),
                "klic": _vse_k_titulku(db_path, r["title"]) or obsah, "url": ""}
    return None


def _vse_k_titulku(db_path: str, title: str, limit: int = 6) -> str:
    """Spoj obsah všech deníkových zápisků s týmž titulkem (nejnovější první)."""
    try:
        with _conn(db_path) as db:
            rows = db.execute(
                "SELECT COALESCE(NULLIF(data,''), note) AS obsah FROM diary "
                "WHERE title = ? AND event_type IN (%s) "
                "ORDER BY id DESC LIMIT ?" % ",".join("?" * len(_ZNALOSTI)),
                (title, *_ZNALOSTI, limit)).fetchall()
    except Exception:
        return ""
    return "\n\n".join((r["obsah"] or "").strip() for r in rows if r["obsah"])[:6000]


def _z_wiki(config: dict) -> Optional[dict]:
    """Náhodné heslo z Wikipedie — Hans o něm nejspíš nic nemá."""
    import requests
    lang = str(_cfg(config).get("wiki_lang", "cs"))
    api = "https://%s.wikipedia.org/w/api.php" % lang
    r = requests.get(api, params={
        "action": "query", "list": "random", "rnnamespace": 0,
        "rnlimit": 5, "format": "json"}, timeout=10,
        headers={"User-Agent": "HansBot/1.0 (dialog self-test)"})
    r.raise_for_status()
    kandidati = [x.get("title", "") for x in
                 (r.json().get("query", {}).get("random") or [])]
    from scripts.web_reader import WebReader
    wr = WebReader(config)
    for titul in kandidati:
        if not titul or any(z in titul for z in ("(rozcestník)", "Seznam ")):
            continue
        art = wr.wikipedia_article(titul, lang=lang, max_chars=3000)
        if not art or len((art.get("text") or "").strip()) < 400:
            continue
        # Do otázky jde heslo BEZ rozlišovací závorky: nikdo se neptá
        # „co víš o Čaroděj (Středozem)?" — klíčem zůstává celý článek.
        _tema = re.sub(r"\s*\([^)]*\)\s*$", "", titul).strip() or titul
        return {"zdroj": "wiki", "tema": _tema,
                "otazka": _otazka_na(_tema), "klic": art.get("text") or "",
                "url": art.get("url") or ""}
    return None


def _ze_souboru(config: dict) -> Optional[dict]:
    """Naše otázky: řádek „otázka | očekávaná odpověď". `#` = komentář."""
    cesta = str(_cfg(config).get("soubor", SOUBOR_OTAZEK))
    if not os.path.exists(cesta):
        return None
    radky = []
    with open(cesta, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "|" not in line:
                continue
            otazka, _, klic = line.partition("|")
            if otazka.strip() and klic.strip():
                radky.append((otazka.strip(), klic.strip()))
    if not radky:
        return None
    otazka, klic = random.choice(radky)
    return {"zdroj": "soubor", "tema": otazka[:60], "otazka": otazka,
            "klic": klic, "url": ""}


# ── HODNOCENÍ ───────────────────────────────────────────────────────────────

def ohodnot(zdroj: str, odpoved: str, klic: str) -> dict:
    """Porovnej odpověď s klíčem. Vrací {verdikt, bez_opory, detail}.

    Verdikty:
      `ok`        — odpověď stojí na klíči (nanejvýš jedna věta navíc),
      `castecne`  — část vět v klíči oporu nemá,
      `vymyslel`  — většina vět bez opory (a aspoň tři),
      `dohledal`  — přiznal neznalost a dohledal (u `wiki` správná reakce),
      `priznal`   — přiznal, že neví (u `wiki` v pořádku),
      `zapiral`   — přiznal neznalost u tématu, o kterém MÁ zápisek (chyba).
    """
    text = (odpoved or "").strip()
    if not text:
        return {"verdikt": "prazdno", "bez_opory": 0, "detail": ""}
    if _DOHLEDANI.search(text):
        return {"verdikt": "dohledal", "bez_opory": 0, "detail": ""}
    try:
        zahozene, vet = _bez_opory(text, klic or "")
    except Exception as e:
        return {"verdikt": "neposouzeno", "bez_opory": 0, "detail": str(e)[:200]}
    bez = len(zahozene)
    podlozenych = max(0, vet - bez)
    # Odříkání se počítá, jen když v odpovědi NIC podloženého nezůstalo.
    # Doloženo hned prvním živým během: Hans odpověděl věcně o Námořní
    # smlouvě a nakonec dodal „o dalších povídkách nemám spolehlivé
    # záznamy" — poctivé ohraničení, ne zapření. Dřívější pořadí (nejdřív
    # hledat odříkací obrat) z toho udělalo „zapíral".
    if podlozenych == 0 and _ABSTINENCE.search(text):
        return {"verdikt": ("zapiral" if zdroj == "pamet" else "priznal"),
                "bez_opory": 0, "detail": text[:200]}
    if bez <= 1:
        verdikt = "ok"
    elif bez >= 3 and bez >= vet / 2.0:
        verdikt = "vymyslel"
    else:
        verdikt = "castecne"
    # U zdroje `pamet` je klíčem to, co si Hans ZAPSAL — ne pravda o světě.
    # Doloženo prvním živým během: na film „Jeden svět nestačí" odpověděl
    # věcně správně (Brosnan, Renard, smrt Desmonda Llewelyna), jenže v jeho
    # zápiscích to není. To NENÍ výmysl, jen znalost nad rámec poznámek —
    # a označit ji za konfabulaci by nás naučilo špatnou věc.
    # Skutečná chyba na téhle větvi je ZAPŘENÍ (má zápisek a tvrdí opak),
    # a to se pozná zvlášť. ⏳ Rozpor se zápiskem (jiný letopočet, jiné
    # jméno) zatím poznat neumíme — viz BACKLOG.
    if zdroj == "pamet" and verdikt in ("vymyslel", "castecne"):
        verdikt = "nad_zapisky"
    return {"verdikt": verdikt, "bez_opory": bez,
            "detail": (zahozene[0][:200] if zahozene else "")}


def _bez_opory(odpoved: str, klic: str):
    """Věty odpovědi, které v klíči oporu NEMAJÍ (+ počet posuzovaných vět).

    ⚠️ ZÁMĚRNĚ NEPOUŽÍVÁ `grounding_guard.check()`, ačkoli sdílí jeho kmenování.
    Guard má jiný úkol: ubírat z odpovědi, aniž by poškodil dobrou — proto u něj
    NULOVÝ překryv znamená NEVINU („věta není o tématu, nemám co ověřovat").
    Při zkoušení je to obráceně: věta o hradě, která s klíčem nesdílí ani slovo,
    je právě ten výmysl, který hledáme. Změřeno na doložené odpovědi o zámeckém
    parku u hradu Kost — guard z ní označil 1 větu ze 4, tahle míra 3.

    Zdvořilosti a nabídky se nepočítají (krátké věty a věty bez tvrzení) —
    „Pokud byste si přál další podrobnosti…" není vymyšlený fakt.
    """
    from scripts.grounding_guard import _stems, MIN_STEMS, MIN_SUPPORT
    G = _stems(klic)
    if len(G) < 5:
        return [], 0
    bez, posuzovanych = [], 0
    for veta in re.split(r"(?<=[.!?])\s+", (odpoved or "").strip()):
        v = veta.strip()
        if not v or _ZDVORILOST.search(v) or _ABSTINENCE.search(v):
            continue        # zdvořilost ani přiznání neznalosti není tvrzení
        S = _stems(v)
        if len(S) < MIN_STEMS:
            continue
        posuzovanych += 1
        if len(S & G) / len(S) < MIN_SUPPORT:
            bez.append(v)
    return bez, posuzovanych


# ── SOUHRN (pro /zdravi a ranní hlášku) ─────────────────────────────────────

_POPIS = {"ok": "obstál", "castecne": "částečně", "vymyslel": "vymýšlel si",
          "nad_zapisky": "mluvil nad rámec zápisků",
          "dohledal": "dohledal", "priznal": "přiznal neznalost",
          "zapiral": "zapřel vlastní zápisek", "prazdno": "bez odpovědi",
          "neposouzeno": "neposouzeno"}


# ── HLÁŠENÍ NÁLEZU ──────────────────────────────────────────────────────────
# KOLAC_EXAM_NOTIFY_V1 (22.8., rozhodl uživatel) — dokud nález nikdo nečte,
# je zkoušení k ničemu. Dosud se výsledky ukládaly do tabulky a jediným
# konzumentem byla jedna věta ve `/zdravi`, o kterou si člověk musí říct.
# Proto: verdikt, u kterého Hans neobstál, se ohlásí sám. Přes `send_proactive`,
# takže v tichém okně počká do rána (a fronta přežije restart).
# Hlásí se JEN špatné verdikty — dvě zkoušky denně znamenají nanejvýš dvě
# zprávy, spíš míň; kdyby chodilo i „ok", přestane se to číst.
_HLASIT_VYCHOZI = ("vymyslel", "zapiral")


def ma_se_hlasit(config: dict, verdikt: str) -> bool:
    c = _cfg(config)
    if not c.get("hlasit", True):
        return False
    return str(verdikt or "") in tuple(
        c.get("hlasit_verdikty", list(_HLASIT_VYCHOZI)))


def _zkrat(text: str, limit: int) -> str:
    """Zkrať na celé věty — půlka věty se čte hůř než o kus kratší text."""
    t = " ".join(str(text or "").split())
    if len(t) <= limit:
        return t
    rez = t[:limit]
    for znak in (". ", "! ", "? "):
        i = rez.rfind(znak)
        if i > limit // 2:
            return rez[:i + 1]
    return rez.rstrip() + "…"


def hlaseni(exam_id: int, polozka: dict, odpoved: str,
            hodnoceni: dict) -> str:
    """Text zprávy o nálezu. Musí unést posouzení BEZ otevírání databáze —
    proto nese otázku, odpověď i klíč, ne jen verdikt."""
    radky = [
        "Koláč mě vyzkoušel a neobstál jsem (#%d — %s, zdroj: %s)."
        % (int(exam_id or 0), _POPIS.get(hodnoceni.get("verdikt"),
                                         hodnoceni.get("verdikt", "?")),
           polozka.get("zdroj", "?")),
        "",
        "Otázka: %s" % _zkrat(polozka.get("otazka"), 200),
        "Odpověděl jsem: %s" % _zkrat(odpoved, 420),
        "Klíč: %s" % _zkrat(polozka.get("klic"), 420),
    ]
    if polozka.get("url"):
        radky.append("Zdroj klíče: %s" % polozka["url"])
    radky += ["",
              "Když to potvrdíte (/nalez %d), udělám z toho trvalou zkušební "
              "otázku. Zamítnout: /nalez %d ne."
              % (int(exam_id or 0), int(exam_id or 0))]
    return "\n".join(radky)


def souhrn(db_path: str, hodin: float = 24) -> str:
    """Jedna věta do `/zdravi`. Prázdný řetězec = nebylo co hlásit."""
    try:
        _init_db(db_path)
        with _conn(db_path) as db:
            rows = db.execute(
                "SELECT verdikt, tema FROM kolac_exam WHERE ts > ? "
                "ORDER BY id DESC", (time.time() - hodin * 3600,)).fetchall()
    except Exception:
        return ""
    if not rows:
        return ""
    poc = {}
    for r in rows:
        poc[r["verdikt"]] = poc.get(r["verdikt"], 0) + 1
    casti = ["%d× %s" % (n, _POPIS.get(v, v))
             for v, n in sorted(poc.items(), key=lambda x: -x[1])]
    veta = "Koláč mě za posledních %d h vyzkoušel %dkrát: %s." % (
        int(hodin), len(rows), ", ".join(casti))
    spatne = [r["tema"] for r in rows if r["verdikt"] in ("vymyslel", "zapiral")]
    if spatne:
        veta += " Neobstál jsem u: %s." % ", ".join(spatne[:3])
    return veta
