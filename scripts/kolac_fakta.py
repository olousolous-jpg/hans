"""KOLAC_EXAM_FAKTA_V1 (12. 10.) — Koláč zkouší Hanse z toho, co nastudoval.

Zdroj `fakta` pro `kolac_exam`: věta z výpisu faktů, který si Hans ukládá po
studijní lekci a k filmům (`web_read`, poznámka „[téma] Fakta z…“), se
předem (v noci) přepíše na konkrétní otázku s JEDNÍM správným údajem, který je
ve větě doslova. Hans odpovídá běžnou chatovou cestou (`_run_zkouska`), shodu
soudí KÓD — ne model a ne poměr vět bez opory (ten 9. 9. dával falešné nálezy).

Verdikty: ok | nevedel (přiznal, že neví) | vedle (odpověděl jinak) | odmitl
(pravidlo soukromí pro cizí — otázka se vyřadí). Nezodpovězené a špatné
otázky se po `opakovat_po_dnech` vracejí dřív než nové.

Měřeno 12. 10. na 12 otázkách (`data/mereni/kolac_zkouska_12_10/`): bez názvu
článku v otázce 5 ok / 4 nevěděl / 3 vedle, s názvem 7 ok / 5 vedle.
"""
import json
import logging
import random
import re
import sqlite3
import time
import unicodedata

_log = logging.getLogger("kolac_exam")

_SCHEMA = """CREATE TABLE IF NOT EXISTS kolac_otazky (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL,
    diary_id INTEGER NOT NULL, tema TEXT, clanek TEXT, veta TEXT NOT NULL,
    otazka TEXT NOT NULL, klic TEXT NOT NULL,
    polozeno INTEGER NOT NULL DEFAULT 0, posledni_ts REAL NOT NULL DEFAULT 0,
    posledni_verdikt TEXT NOT NULL DEFAULT '', stav TEXT NOT NULL DEFAULT 'ok')"""
_DEN = 86400.0
# slova klíče, která sama nic neurčují („v roce 1977“ → rozhoduje 1977)
_STOP = frozenset({"roce", "roku", "lety", "let", "procent", "ktery", "ktera",
                   "ktere", "jeho", "jeji", "svym", "svou", "podle", "okolo",
                   "kolem", "priblizne", "zhruba", "asi"})
_ABST = re.compile(r"nem[aá]m|nev[ií]m|netu[sš][ií]m|nejsem si jist|"
                   r"nedok[aá][zž]u|nepamatuj|nen[ií] mi zn[aá]m", re.I)
_ODMITL = re.compile(r"jen se svou dom[aá]cnost[ií]|nem[aá]te svolen[ií]", re.I)
_SYS = (
    "Dostaneš jednu větu ze studijních poznámek, téma a název článku, ze "
    "kterého je. Napiš JEDNU krátkou otázku, na kterou odpovídá právě JEDEN "
    "údaj z té věty (jméno, rok, číslo nebo pojem). Otázka musí být "
    "srozumitelná sama o sobě a musí v ní zaznít název článku nebo díla — "
    "nepiš „v textu“ ani „podle věty“. Odpověď opiš z věty DOSLOVA (1–4 "
    "slova).\nVrať POUZE JSON: {\"otazka\":\"…\",\"odpoved\":\"…\"} (česky).")


def _fold(s: str) -> str:
    return "".join(ch for ch in unicodedata.normalize("NFD", (s or "").lower())
                   if unicodedata.category(ch) != "Mn")


def _conn(db: str):
    c = sqlite3.connect(db, timeout=10)
    c.row_factory = sqlite3.Row
    c.execute(_SCHEMA)
    return c


# KOLAC_EXAM_SOUD_V2 (15. 10.) — „po dohledání mi vyšlo…“ za přiznáním neznalosti
# je TVRZENÍ, ne přiznání (zkouška #64: „v paměti nemám záznam… po dohledání
# v databázi mi vyšlo Intersection“ → kód „nevěděl“, správně Rush Hour).
_PO_PRIZNANI_TVRDI = re.compile(r"po dohled[aá]n|dohledal|vy[sš]lo mi|mi vy[sš]lo|na[sš]el jsem", re.I)


def soud(odpoved: str, klic: str, otazka: str = "") -> str:
    """Je správný údaj v odpovědi? Čísla musí sedět přesně, slova kmenem
    (5 znaků). Vrací ok | nevedel | vedle | odmitl | neposouzeno | prazdno."""
    text = (odpoved or "").strip()
    if not text:
        return "prazdno"
    if _ODMITL.search(text) and len(text) < 120:
        return "odmitl"
    o = _fold(text)
    vse = re.findall(r"\w+", _fold(klic))
    toks = [t for t in vse if t.isdigit() or (len(t) >= 4 and t not in _STOP)]
    if not toks:
        toks = vse
    if toks and all(
            (re.search(r"(?<!\d)%s(?!\d)" % re.escape(t), o) is not None)
            if t.isdigit() else (t[:5] in o) for t in toks):
        return "ok"
    if _ABST.search(text) and not _PO_PRIZNANI_TVRDI.search(text):
        return "nevedel"
    # Hans píše čísla i slovy („ve třech tisících… kinech“) — klíč s číslem
    # proti odpovědi bez jediné číslice kód posoudit neumí (první ostrá
    # zkouška 12. 10.: správná odpověď slovy vyšla jako „vedle“)
    # KOLAC_EXAM_SOUD_V2 — číslice, která stojí už v otázce, se nepočítá
    # (#65: „Strážci Galaxie: Volume 3“ → „třicátý druhý“ vyšlo jako „vedle“).
    # 📏 28 zkoušek z faktů: mění právě #64 a #65, oba podle ručního čtení.
    _v_otazce = set(re.findall(r"\d+", _fold(otazka or "")))
    if any(x.isdigit() for x in toks) and not [
            c for c in re.findall(r"\d+", o) if c not in _v_otazce]:
        return "neposouzeno"
    return "vedle"


def vhodne_vety(note: str) -> list:
    """Věty výpisu, ze kterých jde udělat otázku s jedním údajem: mají
    letopočet/číslo nebo vlastní jméno uvnitř věty."""
    out = []
    for v in (note or "").split("\n")[1:]:
        v = v.strip()
        if not 40 <= len(v) <= 220:
            continue
        if re.search(r"\b\d{3,4}\b", v) or re.search(
                r"\s[A-ZÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ][a-záčďéěíňóřšťúůýž]{3,}", v):
            out.append(v)
    return out


# KOLAC_EXAM_KLIC_SEZNAM_V1 (13. 10.) — audit 30 otázek: klíčem bývalo JEDNO
# jméno z výčtu („vedlejší role hrají A, B a C“ → klíč C), takže správná odpověď
# A dostala verdikt „vedle“. Klíč smí být celý výčet, ne jeho jedna položka.
_VYCET = re.compile(r"[^,.;:()]+(?:,\s*[^,.;:()]+)+\s+a\s+[^,.;:()]+")


def klic_ze_seznamu(klic: str, veta: str) -> bool:
    k = _fold(klic).strip()
    if not k or "," in k or " a " in " %s " % k:
        return False
    for m in _VYCET.finditer(veta or ""):
        polozky = [p.strip() for p in re.split(r",|\s+a\s+", _fold(m.group(0)))
                   if p.strip()]
        if len(polozky) >= 3 and any(k in p or p in k for p in polozky[1:]):
            return True
        # první položka nese i začátek věty → stačí, že klíčem končí
        if len(polozky) >= 3 and polozky[0].endswith(k):
            return True
    return False


def otazka_plati(otazka: str, klic: str, veta: str) -> bool:
    """Klíč musí být doslova ve větě, nesmí být prozrazen v otázce a nesmí
    být jednou položkou výčtu."""
    o, k = (otazka or "").strip(), (klic or "").strip()
    return bool(len(o) >= 12 and k and _fold(k) in _fold(veta)
                and _fold(k) not in _fold(o)
                and not klic_ze_seznamu(k, veta))


def s_nazvem(otazka: str, clanek: str) -> str:
    """Otázka musí jmenovat článek/dílo — jinak se paměť nemá čeho chytit
    (12. 10.: bez názvu 4 z 12 „nemám záznam“, s názvem 0)."""
    o, c = (otazka or "").strip(), (clanek or "").strip()
    if not c or _fold(c)[:8] in _fold(o):
        return o
    return "K tématu „%s“: %s" % (c, o[:1].lower() + o[1:])


def zasoba(db: str) -> int:
    """Kolik otázek ještě nebylo položeno."""
    try:
        c = _conn(db)
        try:
            return int(c.execute("SELECT count(*) FROM kolac_otazky WHERE "
                                 "polozeno=0 AND stav='ok'").fetchone()[0])
        finally:
            c.close()
    except Exception:
        return 0


def pripravit(config: dict, db: str, kolik: int = 20) -> int:
    """Doplň zásobu otázek. Vrací počet přidaných, nebo None, když model
    neodpověděl (KOLAC_ZASOBY_RETRY_V1 — volající to má zkusit znovu)."""
    from scripts.ollama_client import ollama_generate
    model = ((config.get("study", {}) or {}).get("model")
             or (config.get("evening_reflection", {}) or {}).get("model")
             or "jobautomation/OpenEuroLLM-Czech:latest")
    c = _conn(db)
    try:
        uz = {r[0] for r in c.execute("SELECT veta FROM kolac_otazky")}
        rows = c.execute(
            "SELECT id, title, note FROM diary WHERE event_type='web_read' "
            "AND note LIKE '%Fakta z%' ORDER BY RANDOM() LIMIT 80").fetchall()
    finally:
        c.close()
    pridano = 0
    dole = False
    for r in rows:
        if pridano >= kolik:
            break
        m = re.match(r"\[(.+?)\]", r["note"] or "")
        tema = m.group(1) if m else (r["title"] or "")
        vety = [v for v in vhodne_vety(r["note"]) if v not in uz]
        if not vety:
            continue
        veta = random.choice(vety)
        raw = ollama_generate(
            model, "Téma: %s\nČlánek: %s\nVěta: %s\n\nJSON:" % (
                tema, r["title"], veta),
            system=_SYS, config=config, timeout=120, keep_alive=0,
            options={"temperature": 0.3, "num_ctx": 4096, "num_predict": 150})
        if not raw:
            dole = True                 # model dole → zkusit při další příležitosti
            break
        uz.add(veta)
        mm = re.search(r"\{.*\}", raw, re.S)
        try:
            d = json.loads(mm.group(0)) if mm else {}
        except Exception:
            d = {}
        ot, kl = str(d.get("otazka", "")).strip(), str(d.get("odpoved", "")).strip()
        if not otazka_plati(ot, kl, veta):
            continue
        c = _conn(db)
        try:
            c.execute(
                "INSERT INTO kolac_otazky (ts, diary_id, tema, clanek, veta, "
                "otazka, klic) VALUES (?,?,?,?,?,?,?)",
                (time.time(), r["id"], tema, r["title"], veta,
                 s_nazvem(ot, r["title"]), kl))
            c.commit()
        finally:
            c.close()
        pridano += 1
    _log.info("KOLAC_EXAM_FAKTA_V1: zásoba otázek +%d (nepoložených %d)%s",
              pridano, zasoba(db), " — model neodpověděl, zkusím znovu" if dole else "")
    return None if dole else pridano


def vyber(config: dict, db: str):
    """Otázka pro zkoušku: nejdřív ty, kde Hans neobstál a uplynul odstup,
    pak nové. Vrací položku pro `kolac_exam` nebo None (prázdná zásoba)."""
    dni = float(((config or {}).get("kolac_exam", {}) or {})
                .get("opakovat_po_dnech", 2))
    c = _conn(db)
    try:
        r = c.execute(
            "SELECT * FROM kolac_otazky WHERE stav='ok' AND polozeno>0 AND "
            "posledni_verdikt IN ('nevedel','vedle') AND posledni_ts<? "
            "ORDER BY posledni_ts LIMIT 1", (time.time() - dni * _DEN,)).fetchone()
        if not r:
            r = c.execute("SELECT * FROM kolac_otazky WHERE stav='ok' AND "
                          "polozeno=0 ORDER BY RANDOM() LIMIT 1").fetchone()
    finally:
        c.close()
    if not r:
        return None
    return {"zdroj": "fakta", "tema": r["clanek"] or r["tema"] or "",
            "otazka": r["otazka"], "klic": r["klic"], "url": "",
            # KOLAC_EXAM_HLASENI_VETA_V1 — celá věta výpisu do hlášení
            "veta": r["veta"] or "",
            "otazka_id": int(r["id"])}


def zapis_vysledek(db: str, otazka_id: int, verdikt: str) -> None:
    """Po zkoušce: stav otázky v zásobě. `odmitl` otázku vyřadí."""
    if not otazka_id:
        return
    c = _conn(db)
    try:
        c.execute(
            "UPDATE kolac_otazky SET polozeno=polozeno+1, posledni_ts=?, "
            "posledni_verdikt=?, stav=? WHERE id=?",
            (time.time(), verdikt,
             "vyrazeno" if verdikt == "odmitl" else "ok", otazka_id))
        c.commit()
    finally:
        c.close()


def prehled(db: str, dni: float = 7) -> dict:
    """Počty verdiktů zkoušek z faktů za poslední dny (pro souhrn a odečet)."""
    c = _conn(db)
    try:
        rows = c.execute(
            "SELECT verdikt, count(*) FROM kolac_exam WHERE zdroj='fakta' AND "
            "ts>? GROUP BY 1", (time.time() - dni * _DEN,)).fetchall()
    except Exception:
        rows = []
    finally:
        c.close()
    return {r[0]: r[1] for r in rows}
