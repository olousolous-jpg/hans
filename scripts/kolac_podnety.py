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


# ── KOLAC_PODNETY_Z_FAKTU_V1 (28. 10.) — podněty ze jmen v Hansových faktech ──
# Model navrhoval ke stále stejným studijním programům pořád totéž (zásoba 0,
# 5–14 návrhů denně, ~25 % nálezů noc zamítla jako výmysly). Jména osob, děl
# a míst z uložených výpisů faktů (`hans_recall_znalost._ft_nacti`) převedená
# do 1. pádu, s PŘESNÝM článkem v lokální Wikipedii, která Hans v paměti nemá:
# nic vymyšleného, souvisí s tím, co Hans četl, a zdroj se sám obnovuje (každé
# dohledání uloží nová fakta). 📏 230 výpisů → 2 272 jmen → 153 kandidátů.
_JMENO = re.compile(r"(?<![\w.])([A-ZÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ][a-záčďéěíňóřšťúůýž]+"
                    r"(?:\s+(?:z|ze|von|van|de|da|di|of|the|a)\s+[A-ZÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ][\wáčďéěíňóřšťúůýž]+"
                    r"|\s+[A-ZÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ][\wáčďéěíňóřšťúůýž.]+){1,3})")
_DRUH_DILO = {"film", "kniha", "album", "skladba", "opera", "videohra", "televizní pořad"}
_DRUH_MISTO = {"sídlo světa", "české sídlo", "česká obec", "region", "hora", "vodní tok",
               "budova", "kostel", "chráněné území", "vodní plocha", "hrad", "zámek",
               "stát", "zaniklý stát"}
_PORADI_TYP = {"osoba": 0, "dilo": 1, "pojem": 2, "misto": 3}


def _typ_clanku(r) -> str:
    from scripts import hans_wiki_offline as wo
    druh, text = wo._db().execute(
        "SELECT infobox_druh, substr(text,1,600) FROM clanek WHERE id=?", (r[0],)).fetchone()
    druh = druh or ""
    if druh in _DRUH_DILO:
        return "dilo"
    if druh in _DRUH_MISTO:
        return "misto"
    z = re.sub(r"\s*\(.*?\)$", "", r[1]).split()[0]
    for ln in (text or "").split("\n")[:4]:
        if ln.strip().startswith(z):
            # osoba: v první větě data narození („(* 1940)“, „(1879 – 1955)“)
            if re.match(r"^[^(]{0,120}\((?:\*|[^()]*\d{3,4}[^()]*[–-])", ln.strip()):
                return "osoba"
            break
    return "pojem"


def kandidati_z_faktu(db: str, kolik: int = 20, uz=None) -> list:
    """[(nazev článku, typ, zdrojový článek)] — nejčerstvější výpisy první,
    osoby a díla před pojmy a místy."""
    from scripts import hans_wiki_offline as wo
    from scripts.hans_recall_znalost import _ft_nacti
    if not wo.dostupna():
        return []
    vyp, _t = _ft_nacti(db)
    uz = set(uz or ())
    out, videno = [], set()
    for poradi, (tit, radky) in enumerate(reversed(list(vyp.items()))):
        _zdroj = wo._cil(tit)
        _ze_zdroje = 0
        for ln in radky:
            if _ze_zdroje >= 2:          # pestrost: nejvýš 2 ze zdroje na dávku
                break
            for m in _JMENO.finditer(" " + ln):
                n = m.group(1).strip(" .")
                if n in videno or n.lower() in uz or n.lower() in (tit or "").lower():
                    continue
                videno.add(n)
                r = wo._cil(n)
                if not r:
                    _t1 = wo.predmet_titul(n)
                    r = wo._cil(_t1) if _t1 else None
                if (not r or r[1].lower() in uz or r[1] == tit
                        or (_zdroj and r[0] == _zdroj[0])):   # „Ole Rømer“ = zdroj
                    continue
                if wo._db().execute("SELECT rozcestnik FROM clanek WHERE id=?",
                                    (r[0],)).fetchone()[0]:
                    continue
                if v_pameti(db, r[1]):
                    uz.add(r[1].lower())
                    continue
                uz.add(r[1].lower())
                out.append((r[1], _typ_clanku(r), tit, poradi))
                _ze_zdroje += 1
                if _ze_zdroje >= 2:
                    break
        if len(out) >= kolik * 3:
            break
    out.sort(key=lambda x: (_PORADI_TYP.get(x[1], 9), x[3]))
    return [(n, typ, tit) for n, typ, tit, _p in out[:kolik]]


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
    # KOLAC_PODNETY_Z_FAKTU_V1 — nejdřív jména z vlastních faktů (bez modelu)
    if bool(_cfg(config).get("podnety_z_faktu", True)):
        try:
            for nazev, typ, zdroj in kandidati_z_faktu(db, kolik, uz):
                c = _conn(db)
                try:
                    c.execute("INSERT INTO kolac_podnety (ts, tema, nazev, typ, stav) "
                              "VALUES (?,?,?,?,'novy')", (time.time(), "fakta: " + zdroj, nazev, typ))
                    c.commit()
                finally:
                    c.close()
                uz.add(nazev.lower())
                pridano += 1
            _log.info("KOLAC_PODNETY_Z_FAKTU_V1: +%d podnětů ze jmen ve faktech", pridano)
        except Exception as e:
            _log.warning("KOLAC_PODNETY_Z_FAKTU_V1 selhalo: %s", e)
        if pridano >= kolik:
            _log.info("KOLAC_PODNETY_V1: zásoba podnětů +%d (nových %d, model netřeba)",
                      pridano, zasoba(db))
            return pridano
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


# ── KOLAC_PODNETY_ZVENKU_V1 (29. 10.) — podnět zvenku: dnešní výročí ─────────
# Rozbor 29. 10. (`SMERY_ROZVOJE_29_10`): všech 188 položených podnětů vzešlo
# z Hansových studijních témat → kruh (koníčky → studium → podněty → koníčky)
# a Severka pak z podkladu pořád odvodí „archiváře“. Část podnětů proto chodí
# ZVENKU: jméno nebo pojem z článku lokální Wikipedie o dnešním dni (události,
# narození, úmrtí) s vlastním dlouhým článkem, který Hans v paměti nemá. Každý
# den jiné obory; nic vymyšleného (přesný článek), bez modelu.
# 📏 29. 10., 30. 10., 5. 3., 17. 11.: 57–86 nových kandidátů na den.
_MESICE = ("leden", "únor", "březen", "duben", "květen", "červen", "červenec",
           "srpen", "září", "říjen", "listopad", "prosinec")
_SLOVO = re.compile(r"[^\W\d_][\w\-]*\.?")      # tečka kvůli „Kýros II.“
_DRUH_NE = {"jméno", "příjmení", "latinka", "písmeno"}


def kandidati_vyroci(db: str, kdy=None, uz=None) -> list:
    """[{nazev, typ, kontext}] z článku o dnešním dni v lokální Wikipedii."""
    from scripts import hans_wiki_offline as wo
    if not wo.dostupna():
        return []
    lt = time.localtime(kdy or time.time())
    den = "%d. %s" % (lt.tm_mday, _MESICE[lt.tm_mon - 1])
    r = wo._db().execute("SELECT text FROM clanek WHERE titul=?", (den,)).fetchone()
    if not r:
        return []
    uz = {u.lower() for u in (uz or ())}
    out, videno, sekce = [], set(), ""
    for ln in (r[0] or "").split("\n"):
        s = ln.strip()
        if s in ("Události.", "Narození.", "Úmrtí.", "Svátky."):
            sekce = s[:-1]
            continue
        if sekce not in ("Události", "Narození", "Úmrtí"):
            continue
        m = re.match(r"^(\d{3,4}(?: př\. n\. l\.)?)\s*–\s*(.+)$", s)
        if not m:
            continue
        rok, vec = m.group(1).lstrip("0"), m.group(2)
        hledej = re.sub(r"\(.*?\)", "", vec)
        if sekce != "Události":
            hledej = hledej.split(",")[0]      # jen jméno osoby
        w = _SLOVO.findall(hledej)
        i = 0
        while i < len(w):
            if not w[i][0].isupper():
                i += 1
                continue
            hit = None
            for n in (4, 3, 2, 1):
                if i + n > len(w):
                    continue
                f = " ".join(w[i:i + n])
                c = wo._cil(f) or (wo._cil(f.rstrip(".")) if f.endswith(".") else None)
                if c:
                    hit = (n, c)
                    break
            if not hit:
                i += 1
                continue
            n, c = hit
            na_zacatku = i == 0
            span = w[i:i + n]
            i += n
            tit = c[1]
            if tit in videno or tit.lower() in uz or tit[0].isdigit():
                continue
            videno.add(tit)
            roz, delka, druh = wo._db().execute(
                "SELECT rozcestnik, length(text), infobox_druh FROM clanek WHERE id=?",
                (c[0],)).fetchone()
            if roz or (delka or 0) < 4000 or (druh or "") in _DRUH_NE or len(tit) < 3:
                continue
            if len(span) == 1:
                # skloněný tvar se shodou jinde („Prahy“ = obec, myšlena Praha)
                _z = span[0].rstrip(".")
                if _z[-1:] in "yiue":
                    _n = wo._cil(_z[:-1] + "a")
                    if _n and _n[0] != c[0]:
                        continue
            if v_pameti(db, tit):
                continue
            if sekce == "Události":
                typ = _typ_clanku(c)
                # první slovo věty má velké písmeno jen kvůli začátku věty:
                # „Televizní stanice ČTV…“ → obecný článek; osoba nebo víc
                # vlastních slov („Kýros II. Veliký“) projde
                if na_zacatku and typ != "osoba" and sum(
                        x[0].isupper() for x in span) < 2:
                    continue
                kontext = "Dnes je výročí: v roce %s – %s" % (rok, vec.rstrip("."))
            else:
                typ = "osoba"
                kontext = "Dnes je výročí %s: %s (%s)." % (
                    "narození" if sekce == "Narození" else "úmrtí",
                    re.sub(r"\s*\((?:†|\*).*?\)", "", vec).rstrip("."), rok)
            out.append({"nazev": tit, "typ": typ, "kontext": kontext[:300],
                        "udalost": sekce == "Události"})
    return out


def podnet_zvenku(config: dict, db: str):
    """Jeden podnět z dnešního výročí, zapsaný do zásoby (stav 'novy');
    vrací dict jako `vyber` + 'kontext', nebo None."""
    c = _conn(db)
    try:
        uz = [r[0] for r in c.execute("SELECT nazev FROM kolac_podnety")]
    finally:
        c.close()
    k = kandidati_vyroci(db, uz=uz)
    if not k:
        return None
    # události mají dvojí váhu (pestřejší obory než seznam narozených)
    vahy = [2 if x["udalost"] else 1 for x in k]
    x = random.choices(k, weights=vahy, k=1)[0]
    lt = time.localtime()
    c = _conn(db)
    try:
        cur = c.execute("INSERT INTO kolac_podnety (ts, tema, nazev, typ, stav) "
                        "VALUES (?,?,?,?,'novy')",
                        (time.time(), "zvenku: výročí %d. %d." % (lt.tm_mday, lt.tm_mon),
                         x["nazev"], x["typ"]))
        c.commit()
        r = c.execute("SELECT * FROM kolac_podnety WHERE id=?", (cur.lastrowid,)).fetchone()
    finally:
        c.close()
    d = dict(r)
    d["kontext"] = x["kontext"]
    return d


def _vyber_s_podilem(config: dict, db: str):
    """KOLAC_PODNETY_ZVENKU_V1 — s pravděpodobností `podnety_zvenku_podil`
    (výchozí 0,25) podnět zvenku, jinak ze zásoby jako dřív."""
    podil = float(_cfg(config).get("podnety_zvenku_podil", 0.25))
    if podil > 0 and random.random() < podil:
        try:
            p = podnet_zvenku(config, db)
            if p:
                _log.info("KOLAC_PODNETY_ZVENKU_V1: podnět zvenku '%s' — %s",
                          p["nazev"], p.get("kontext", ""))
                return p
        except Exception as e:
            _log.warning("KOLAC_PODNETY_ZVENKU_V1 selhalo: %s", e)
    return vyber(db)


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
        p = _vyber_s_podilem(config, db)   # KOLAC_PODNETY_ZVENKU_V1
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
        # KOLAC_PODNETY_Z_FAKTU_V1 — jméno z věty faktu s přesným článkem:
        # jmenovec nehrozí, téma (zdrojový článek) bývá anglicky („Zionism“)
        if p.get("typ") == "osoba" and not (p.get("tema") or "").startswith(("fakta:", "zvenku:")) \
                and not souvisi_s_tematem(
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
                "nazev": p["nazev"], "tema": p.get("tema") or "",
                "uvod": p.get("kontext") or ""}
    _log.info("KOLAC_PODNETY_V1: %d podnětů po sobě nevyšlo → volný hovor", hledano)
    return None
