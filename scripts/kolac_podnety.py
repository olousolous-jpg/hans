"""KOLAC_PODNETY_V1 (12. 10.) — Koláč přináší Hansovi věci, které nezná.

Třetí podoba dialogu vedle volného hovoru a zkoušky (`kolac_fakta`). Koláč se
zeptá na osobnost, dílo, místo nebo pojem, který souvisí s tím, co Hans
studuje, ale v paměti ho Hans NEMÁ. Hans si heslo hned dohledá stejným
mechanismem jako člověku (`hans_findings.lookup_now`): odpoví provizorně a
nález jde do noční čekárny; co noc ověří, zapíše se do paměti jako četba i
s výpisem faktů — a odtud se později ptá zkouška.

Proč se dohledání volá PŘÍMO a ne chatovou cestou: měřeno 12. 10. na 10
otázkách — chatová cesta dohledala 1× (a špatné heslo), 9× odpověděl model
z hlavy, tedy nic nového do paměti. Poměr tří podob drží `rezim()` podle
`kolac_exam.pomery` (výchozí volný 30 : nové 50 : zkouška 20).
"""
import json
import logging
import random
import re
import sqlite3
import time

_log = logging.getLogger("kolac_exam")

_SCHEMA = """CREATE TABLE IF NOT EXISTS kolac_podnety (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, tema TEXT,
    nazev TEXT NOT NULL, typ TEXT NOT NULL DEFAULT 'pojem',
    stav TEXT NOT NULL DEFAULT 'novy',   -- novy|polozeno|nenalezeno|v_pameti
    polozeno_ts REAL NOT NULL DEFAULT 0, heslo TEXT NOT NULL DEFAULT '')"""
_TVAR = {"osoba": ("Kdo byl %s?", "Co víte o osobnosti jménem %s?"),
         "dilo": ("Co je %s?", "Znáte dílo %s?"),
         "misto": ("Co je %s?", "Co víte o místě %s?"),
         "pojem": ("Co je %s?", "Co se rozumí pojmem %s?")}
_UVOD = ("Něco jsem zaslechl a nedá mi to.", "Tohle by vás mohlo zajímat.",
         "Otázka, na kterou sám neznám odpověď:", "Když už jsme u toho:")
_CTENI = ("web_read", "study_note", "reading_takeaway", "book_reflection",
          "study_note_part", "study_mastery")
_SYS = (
    "Jsi zvídavý společník studenta. Dostaneš téma, které student studoval, a "
    "seznam toho, co už probral. Navrhni 5 věcí, které v seznamu NEJSOU, s "
    "tématem PŘÍMO souvisejí a posunuly by ho dál: konkrétní osobnosti, díla, "
    "místa nebo pojmy, které mají vlastní heslo v encyklopedii (žádné nově "
    "složené názvy, nic vymyšleného). U každé uveď typ: osoba | dilo | misto "
    "| pojem.\nVrať POUZE JSON: {\"navrhy\":[{\"nazev\":\"…\",\"typ\":\"…\"}]} "
    "(česky).")


def _cfg(config: dict) -> dict:
    return (config or {}).get("kolac_exam", {}) or {}


def _conn(db: str):
    c = sqlite3.connect(db, timeout=10)
    c.row_factory = sqlite3.Row
    c.execute(_SCHEMA)
    return c


# ── poměr tří podob dialogu ─────────────────────────────────────────────────
def poradi_rezimu(pomery: dict, delka: int = 10) -> list:
    """Rozlož váhy do cyklu `delka` dialogů rovnoměrně (žádné shluky)."""
    vahy = {k: max(0.0, float(v)) for k, v in (pomery or {}).items()}
    soucet = sum(vahy.values())
    if soucet <= 0:
        return ["volny"] * delka
    dluh = {k: 0.0 for k in vahy}
    out = []
    for _ in range(delka):
        for k in vahy:
            dluh[k] += vahy[k] / soucet
        k = max(sorted(dluh), key=lambda x: dluh[x])
        dluh[k] -= 1.0
        out.append(k)
    return out


def rezim(config: dict, poradi: int) -> str:
    """Podoba N-tého dialogu: volny | nove | zkouska."""
    pom = _cfg(config).get("pomery") or {"volny": 30, "nove": 50, "zkouska": 20}
    cyk = poradi_rezimu(pom)
    return cyk[(max(1, int(poradi)) - 1) % len(cyk)]


# ── zásoba podnětů ──────────────────────────────────────────────────────────
def v_pameti(db: str, nazev: str) -> bool:
    """Má Hans o věci už zápisek, četbu nebo entitu?"""
    f = "%" + (nazev or "").strip() + "%"
    c = _conn(db)
    try:
        n = c.execute(
            "SELECT count(*) FROM diary WHERE event_type IN (%s) AND "
            "(title LIKE ? OR note LIKE ?)" % ",".join("?" * len(_CTENI)),
            (*_CTENI, f, f)).fetchone()[0]
        if n:
            return True
        try:
            return bool(c.execute("SELECT 1 FROM entities WHERE name LIKE ? "
                                  "LIMIT 1", (f,)).fetchone())
        except Exception:
            return False
    finally:
        c.close()


def cisty_nazev(nazev: str) -> str:
    """„Offside rule (pravidlo ofsajdu)“ → bez závorky a uvozovek."""
    n = re.sub(r"\s*\(.*?\)\s*", " ", str(nazev or ""))
    return re.sub(r"\s+", " ", n).strip(" .\"„“'")[:80]


# KOLAC_PODNETY_DALSI_V1 (13. 10.) — první noc: z 10 pokusů o podnět vyšly 4
# a každý neúspěch znamenal volný hovor (podíl nového ~14 % místo 50 %).
# Dvě páky: neúspěšný podnět hned střídá další (`podnety_pokusu`) a dlouhé
# složené názvy se do zásoby neberou — heslo encyklopedie bývá krátké, kdežto
# vymyšlené názvy studií mají 5 a víc slov (2 ze 2 se nedohledaly).
def prilis_slozeny(nazev: str, max_slov: int = 4) -> bool:
    slova = re.findall(r"[^\W_]+", nazev or "")
    return len(slova) > max_slov


def zasoba(db: str) -> int:
    try:
        c = _conn(db)
        try:
            return int(c.execute("SELECT count(*) FROM kolac_podnety WHERE "
                                 "stav='novy'").fetchone()[0])
        finally:
            c.close()
    except Exception:
        return 0


# ── KOLAC_PODNET_HESLO_V1 (27. 10.) — do zásoby jen to, co má článek ──────────
# Model si názvy podnětů vymýšlí volně: 6 z 10 skončilo „nenalezeno“ (318 z 529)
# — výmysly („Valtlavínské klastry“, „Boží Zápasy“), skutečné věci pod nehesllovým
# názvem („Gestalt teorie“, „Fotbalová legenda Pelé“) a spojení, která heslem
# nejsou („Hudba a Parkinsonova choroba“). Dialog pak třikrát zkusil, nic nenašel
# a spadl do volného hovoru → podnětů 27 % dialogů místo 50 %. Kontrola proti
# lokální Wikipedii (jen lokálně, bez sítě) a uložení pod názvem článku.
# 📏 484 podnětů (`data/mereni/podnety_27_10/kontrola.py`): úspěšných projde
# 135/166, neúspěšných se vyřadí 278/318 (ztráta hlavně anglických názvů).
def heslo_podnetu(config: dict, nazev: str):
    """Název článku v lokální Wikipedii pro podnět, None = nemá článek.
    Nedostupná lokální wiki → název beze změny (chování jako dřív)."""
    try:
        from scripts import hans_wiki_offline as wo
        if not wo.dostupna():
            return nazev
        r = wo._cil(nazev) or (wo._presna_zaloha(nazev) or (None, None))[1]
        if r and not wo.extrakt(r[1])[1]:
            return r[1]
        from scripts.web_reader import WebReader
        from scripts.hans_overeni import _jen_lokalne
        from scripts import hans_findings as hf
        wr = WebReader(config)
        wr._get = _jen_lokalne
        t = wr._wikipedia_search(nazev)
        if t and not wo.extrakt(t)[1] and hf._titles_align(nazev, t):
            return t
    except Exception as e:
        _log.debug("KOLAC_PODNET_HESLO_V1 %r: %s", nazev, e)
        return nazev
    return None


def pripravit(config: dict, db: str, kolik: int = 20) -> int:
    """Noční doplnění zásoby: model navrhne související věci k Hansovým
    studijním tématům, kód vyřadí ty, které Hans v paměti má."""
    from scripts.ollama_client import ollama_generate
    model = ((config.get("study", {}) or {}).get("model")
             or (config.get("evening_reflection", {}) or {}).get("model")
             or "jobautomation/OpenEuroLLM-Czech:latest")
    c = _conn(db)
    try:
        progs = c.execute(
            "SELECT topic, curriculum FROM study_program WHERE status IN "
            "('active','completed') ORDER BY id DESC LIMIT 6").fetchall()
        uz = {r[0].lower() for r in c.execute("SELECT nazev FROM kolac_podnety")}
    finally:
        c.close()
    random.shuffle(progs)
    pridano = 0
    dole = False
    vyrazeno = 0
    # KOLAC_PODNET_HESLO_V1 — projde jen ~třetina návrhů → témata až dvakrát
    for p in progs + progs:
        if pridano >= kolik:
            break
        try:
            probrano = json.loads(p["curriculum"] or "[]")
        except Exception:
            probrano = []
        raw = ollama_generate(
            model, "Téma: %s\nUž probral:\n%s\n\nJSON:" % (
                p["topic"], "\n".join("- %s" % s for s in probrano)),
            system=_SYS, config=config, timeout=120, keep_alive=0,
            options={"temperature": 0.6, "num_ctx": 4096, "num_predict": 300})
        if not raw:
            dole = True                   # model dole → při další příležitosti
            break
        m = re.search(r"\{.*\}", raw, re.S)
        try:
            navrhy = json.loads(m.group(0)).get("navrhy", []) if m else []
        except Exception:
            navrhy = []
        for nv in navrhy:
            nazev = cisty_nazev(nv.get("nazev", "")) if isinstance(nv, dict) else ""
            typ = str(nv.get("typ", "pojem")).strip().lower() if isinstance(nv, dict) else "pojem"
            if len(nazev) < 3 or nazev.lower() in uz:
                continue
            if prilis_slozeny(nazev, int(_cfg(config).get("podnety_max_slov", 4))):
                continue
            uz.add(nazev.lower())
            _hes = heslo_podnetu(config, nazev)   # KOLAC_PODNET_HESLO_V1
            if not _hes:
                vyrazeno += 1
                continue
            if _hes.lower() in uz and _hes != nazev:
                continue
            nazev = _hes
            uz.add(nazev.lower())
            stav = "v_pameti" if v_pameti(db, nazev) else "novy"
            c = _conn(db)
            try:
                c.execute("INSERT INTO kolac_podnety (ts, tema, nazev, typ, stav) "
                          "VALUES (?,?,?,?,?)", (time.time(), p["topic"], nazev,
                                                 typ if typ in _TVAR else "pojem",
                                                 stav))
                c.commit()
            finally:
                c.close()
            pridano += stav == "novy"
    _log.info("KOLAC_PODNETY_V1: zásoba podnětů +%d (nových %d, bez článku vyřazeno %d)%s",
              pridano, zasoba(db), vyrazeno,
              " — model neodpověděl, zkusím znovu" if dole else "")
    return None if dole else pridano


# KOLAC_ZASOBY_RETRY_V1 (12. 10., pokyn uživatele) — když model není dostupný,
# doplnění se nesmí odložit o celý den: i další noc může být model pryč.
# Noční úloha se proto neoznačí za hotovou, dokud doplnění neproběhlo, a přes
# den si dialog malou dávku doplní sám, jakmile zásoba skoro dojde.
_posledni_nouze = 0.0


def dopln_zasoby(config: dict, db: str, nouze: bool = False):
    """Doplň obě zásoby (otázky ke zkoušce, podněty). `nouze` = denní malá
    dávka jen při skoro prázdné zásobě. Vrací True (hotovo nebo nebylo
    potřeba), None když model není dostupný (zkusit znovu)."""
    from scripts import kolac_fakta as kf
    kc = _cfg(config)
    potreba_f = ("fakta" in (kc.get("zdroje") or [])
                 and kf.zasoba(db) < (5 if nouze else int(kc.get("zasoba_min", 40))))
    potreba_p = zasoba(db) < (3 if nouze else int(kc.get("podnety_min", 15)))
    if not (potreba_f or potreba_p):
        return True
    try:
        from scripts.ollama_client import brain_available
        if not brain_available(config):
            return None
    except Exception:
        return None
    if potreba_f and kf.pripravit(
            config, db, 5 if nouze else int(kc.get("zasoba_davka", 30))) is None:
        return None
    if potreba_p and pripravit(
            config, db, 5 if nouze else int(kc.get("podnety_davka", 20))) is None:
        return None
    return True


def dopln_v_nouzi(config: dict, db: str, odstup_s: float = 1800.0):
    """Voláno z dialogu přes den: nejvýš jednou za `odstup_s` zkus malou dávku,
    když zásoba skoro došla."""
    global _posledni_nouze
    if time.time() - _posledni_nouze < odstup_s:
        return None
    _posledni_nouze = time.time()
    return dopln_zasoby(config, db, nouze=True)


def souvisi_s_tematem(tema: str, text: str) -> bool:
    """Zmiňuje článek aspoň jedno významové slovo tématu (kmen 5 znaků)?
    Pojistka proti jmenovci: 12. 10. se k Norimberskému procesu dohledal
    „Robert Jackson“ fotograf místo žalobce. Bez tématu se nefiltruje."""
    import unicodedata

    def _f(s):
        return "".join(ch for ch in unicodedata.normalize("NFD", (s or "").lower())
                       if unicodedata.category(ch) != "Mn")
    kmeny = {w[:5] for w in re.findall(r"[^\W\d_]+", _f(tema)) if len(w) >= 5}
    kmeny -= {"jejic", "ktery", "infor", "okoln", "zprac"}
    if not kmeny:
        return True
    t = _f(text)
    return any(k in t for k in kmeny)


def cekajicich(db: str, asker: str) -> int:
    """Kolik Koláčových nálezů ještě čeká na noční ověření."""
    try:
        c = sqlite3.connect(db, timeout=5)
        try:
            return int(c.execute(
                "SELECT count(*) FROM unverified_findings WHERE "
                "status='pending' AND asker=?", (asker,)).fetchone()[0])
        finally:
            c.close()
    except Exception:
        return 0


def vyber(db: str):
    c = _conn(db)
    try:
        r = c.execute("SELECT * FROM kolac_podnety WHERE stav='novy' "
                      "ORDER BY RANDOM() LIMIT 1").fetchone()
    finally:
        c.close()
    return dict(r) if r else None


def otazka(podnet: dict, seed=None) -> str:
    r = random.Random(seed if seed is not None else time.time())
    return r.choice(_TVAR.get(podnet.get("typ"), _TVAR["pojem"])) % podnet["nazev"]


def uvod(seed=None) -> str:
    return random.Random(seed if seed is not None else time.time()).choice(_UVOD)


def _oznac(db: str, pid: int, stav: str, heslo: str = "") -> None:
    c = _conn(db)
    try:
        c.execute("UPDATE kolac_podnety SET stav=?, polozeno_ts=?, heslo=? "
                  "WHERE id=?", (stav, time.time(), heslo, pid))
        c.commit()
    finally:
        c.close()


def poloz(config: dict, db: str, asker: str):
    """Jedna výměna „něco nového“. Vrací {otazka, odpoved, heslo} nebo None
    (není z čeho, čekárna je plná, heslo se nenašlo → volný hovor)."""
    from scripts import hans_findings as hf
    strop = int(_cfg(config).get("nove_max_cekajicich", 10))
    if cekajicich(db, asker) >= strop:
        _log.info("KOLAC_PODNETY_V1: v čekárně je %d+ nálezů → dnes už nic nového",
                  strop)
        return None
    pokusu = max(1, int(_cfg(config).get("podnety_pokusu", 3)))
    max_slov = int(_cfg(config).get("podnety_max_slov", 4))
    hledano = 0
    for _ in range(pokusu + 6):           # strop i pro věci vyřazené bez hledání
        if hledano >= pokusu:
            break
        p = vyber(db)
        if not p:
            return None
        if v_pameti(db, p["nazev"]):
            _oznac(db, p["id"], "v_pameti")
            continue
        if prilis_slozeny(p["nazev"], max_slov):
            _oznac(db, p["id"], "nenalezeno")
            continue
        hledano += 1
        ot = otazka(p)
        odp = hf.lookup_now(config, db, p["nazev"], ot, asker=asker)
        if not odp:
            # výpadek modelu (i herní mód) není vina podnětu: nic neoznačovat,
            # jinak by tři pokusy po sobě vyjedly zásobu
            try:
                from scripts.ollama_client import brain_available
                if not brain_available(config):
                    return None
            except Exception:
                return None
            _oznac(db, p["id"], "nenalezeno")
            _log.info("KOLAC_PODNETY_V1: '%s' se nedohledalo → zkusím další",
                      p["nazev"])
            continue
        nal = hf.recent_pending_for_topic(db, p["nazev"]) or {}
        heslo = nal.get("resolved_title") or ""
        if heslo and not hf._titles_align(p["nazev"], heslo):
            # cizí heslo do paměti nepouštět (12. 10.: „Teorie informační
            # entropie“ → heslo „Teorie“)
            if nal.get("id"):
                hf._set_status(db, int(nal["id"]), "rejected",
                               "kolac: titul hesla nesedí na dotaz")
            _oznac(db, p["id"], "nenalezeno", heslo)
            _log.info("KOLAC_PODNETY_V1: '%s' → heslo '%s' nesedí, zahozeno",
                      p["nazev"], heslo)
            continue
        # jen u osob: jmenovci jsou past u jmen; u míst a pojmů kontrola
        # zahazovala i správná hesla (Flossenbürg k Norimberskému procesu)
        if p.get("typ") == "osoba" and not souvisi_s_tematem(
                p.get("tema") or "", nal.get("raw_text") or ""):
            if nal.get("id"):
                hf._set_status(db, int(nal["id"]), "rejected",
                               "kolac: článek nesouvisí s tématem podnětu")
            _oznac(db, p["id"], "nenalezeno", heslo)
            _log.info("KOLAC_PODNETY_V1: '%s' → heslo '%s' nesouvisí s tématem "
                      "'%s', zahozeno", p["nazev"], heslo, p.get("tema") or "")
            continue
        _oznac(db, p["id"], "polozeno", heslo)
        _log.info("KOLAC_PODNETY_V1: podnět '%s' (%s) → heslo '%s', čeká na "
                  "noční ověření", p["nazev"], p.get("tema") or "", heslo)
        return {"otazka": ot, "odpoved": odp, "heslo": heslo,
                "nazev": p["nazev"], "tema": p.get("tema") or ""}
    _log.info("KOLAC_PODNETY_V1: %d podnětů po sobě nevyšlo → volný hovor", hledano)
    return None
