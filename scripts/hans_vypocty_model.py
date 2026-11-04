"""HANS_VYPOCTY_MODEL_V1 (31. 10.) — matematika, slovní úlohy a počítání s datem:
model napíše VÝRAZ s povolenými funkcemi, program ho spočítá.

Stejný princip jako `hans_fyzika_model` (LLM jako tlumočník): qwen3 na CPU Ollamě
přeloží otázku na výraz, vlastní vyhodnocovač (AST, jen čísla, operace a funkce ze
seznamu — nic se nespouští jako kód) ho spočítá. Pojistka: každé číslo ve výrazu
musí být v otázce (kromě běžných převodních čísel), takže si model nevymyslí datum
narození ani sazbu; datum smí být z otázky, dnešek nebo pevný svátek.
"""
from __future__ import annotations

import ast
import datetime as _dt
import json
import logging
import math
import os
import re
import threading
import time
import unicodedata
from typing import Optional

_log = logging.getLogger(__name__)
MARK = "HANS_VYPOCTY_MODEL_V1"
_DNY_CZ = ["pondělí", "úterý", "středa", "čtvrtek", "pátek", "sobota", "neděle"]
_MESICE = ["ledna", "února", "března", "dubna", "května", "června", "července", "srpna",
           "září", "října", "listopadu", "prosince"]


def _f(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", (s or "").lower())
                   if unicodedata.category(c) != "Mn")


# ── povolené funkce ──────────────────────────────────────────────────────────
def _velikonoce(r: int) -> _dt.date:
    a = r % 19; b, c = divmod(r, 100); d, e = divmod(b, 4); f = (b + 8) // 25
    g = (b - f + 1) // 3; h = (19 * a + b - d - g + 15) % 30; i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7; m = (a + 11 * h + 22 * l) // 451
    mes, den = divmod(h + l - 7 * m + 114, 31)
    return _dt.date(r, mes, den + 1)


def _vek(a, b):
    if isinstance(b, (int, float)):            # HANS_VYPOCTY_JEN_ROK_V1 — i druhý jen rok
        return int(b) - (int(a) if isinstance(a, (int, float)) else a.year)
    if isinstance(a, (int, float)):            # jen rok („od roku 1918“)
        return b.year - int(a)
    return b.year - a.year - ((b.month, b.day) < (a.month, a.day))


def _splatka(jistina, urok_proc, let):
    i = urok_proc / 100 / 12; n = int(round(let * 12))
    return jistina / n if i == 0 else jistina * i / (1 - (1 + i) ** -n)


def _reseni(rovnice, prom="x"):
    import sympy as sp
    if not isinstance(rovnice, str) or len(rovnice) > 120 or re.search(r"[^0-9a-z+\-*/().=\s^,]", rovnice):
        raise ValueError("rovnice")
    x = sp.Symbol(prom)
    l, _, p = rovnice.replace("^", "**").replace(",", ".").partition("=")
    loc = {prom: x, "sqrt": sp.sqrt}
    sol = sp.solve(sp.sympify(l, locals=loc) - sp.sympify(p or "0", locals=loc), x)
    out = [float(s) for s in sol if s.is_real]
    if not out:
        raise ValueError("bez reálného řešení")
    return out[0] if len(out) == 1 else tuple(sorted(out))


def _dnes():
    return _dt.date.today()


FUNKCE = {
    "sqrt": math.sqrt, "log10": math.log10, "ln": math.log, "exp": math.exp, "abs": abs,
    "round": lambda x, n=0: round(x, int(n)), "min": min, "max": max,
    "prumer": lambda *a: sum(a) / len(a),
    "procento_zmeny": lambda a, b: (b - a) / a * 100,
    "procento": lambda cast, celek: cast / celek * 100,
    "splatka": _splatka,                                   # měsíční splátka anuitního úvěru
    "slozene": lambda j, u, let: j * (1 + u / 100) ** let,  # roční složené úročení
    "reseni": _reseni,
    "datum": lambda r, m, d: _dt.date(int(r), int(m), int(d)),
    "dnes": _dnes,
    "dny": lambda a, b: abs((b - a).days),      # bez znaménka: model prohazoval pořadí
    "pricti_dny": lambda d, n: d + _dt.timedelta(days=int(n)),
    "den_v_tydnu": lambda d: _DNY_CZ[d.weekday()],
    "vek": _vek,
    "velikonoce": lambda r: _velikonoce(int(r)),
    "rok": lambda d: d.year,
}
_POVOLENE_UZLY = (ast.Expression, ast.BinOp, ast.UnaryOp, ast.Constant, ast.Call, ast.Name,
                  ast.Load, ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow, ast.Mod, ast.FloorDiv,
                  ast.USub, ast.UAdd)


def vyhodnot(vyraz: str):
    """Bezpečně vyhodnotí výraz (čísla, + - * / ** % //, povolené funkce, řetězec jen
    jako rovnice pro `reseni`). Chyba → ValueError."""
    v = (vyraz or "").replace("^", "**").replace("×", "*").replace("·", "*").strip()
    if not v or len(v) > 300:
        raise ValueError("prázdný/dlouhý")
    strom = ast.parse(v, mode="eval")
    for u in ast.walk(strom):
        if not isinstance(u, _POVOLENE_UZLY):
            raise ValueError("nepovolený prvek %s" % type(u).__name__)
        if isinstance(u, ast.Name) and u.id not in FUNKCE:
            raise ValueError("neznámé jméno %s" % u.id)
        if isinstance(u, ast.Call) and not (isinstance(u.func, ast.Name) and u.func.id in FUNKCE):
            raise ValueError("nepovolené volání")
        if isinstance(u, ast.Constant) and isinstance(u.value, str):
            pass
        if isinstance(u, ast.BinOp) and isinstance(u.op, ast.Pow):
            if isinstance(u.right, ast.Constant) and isinstance(u.right.value, (int, float)) and abs(u.right.value) > 400:
                raise ValueError("velký exponent")

    def ev(n):
        if isinstance(n, ast.Expression):
            return ev(n.body)
        if isinstance(n, ast.Constant):
            return n.value
        if isinstance(n, ast.UnaryOp):
            x = ev(n.operand)
            return -x if isinstance(n.op, ast.USub) else +x
        if isinstance(n, ast.BinOp):
            a, b = ev(n.left), ev(n.right)
            op = type(n.op)
            if op is ast.Add: return a + b
            if op is ast.Sub: return a - b
            if op is ast.Mult: return a * b
            if op is ast.Div: return a / b
            if op is ast.Pow:
                if abs(b) > 400 or (abs(a) > 1e6 and abs(b) > 20):
                    raise ValueError("velký exponent")
                return a ** b
            if op is ast.Mod: return a % b
            if op is ast.FloorDiv: return a // b
        if isinstance(n, ast.Call):
            return FUNKCE[n.func.id](*[ev(a) for a in n.args])
        raise ValueError("uzel")
    return ev(strom)


# ── pojistka: čísla z otázky ─────────────────────────────────────────────────
_BEZNA = {0, 1, 2, 3, 4, 7, 10, 12, 24, 52, 60, 100, 365, 366, 1000, 3600, 0.5}
_SVATKY_FN = ("velikonoce", "dnes")


def _cisla_otazky(q: str) -> set:
    t = (q or "").replace(" ", " ")
    t = re.sub(r"(?<=\d) (?=\d{3}\b)", "", t)                   # 200 000 → 200000
    out = set()
    for m in re.finditer(r"\d+(?:[.,]\d+)?", t):
        s = m.group(0).replace(",", ".")
        try:
            out.add(float(s))
        except ValueError:
            pass
    # slovní násobky: „200 tisíc“, „2 miliony“, „1,5 milionu“
    for m in re.finditer(r"(\d+(?:[.,]\d+)?)\s*(tis\w*|mil\w*on\w*|mld|miliard\w*)", _f(t)):
        k = 1e3 if m.group(2).startswith("tis") else (1e9 if m.group(2).startswith(("mld", "miliard")) else 1e6)
        out.add(float(m.group(1).replace(",", ".")) * k)
    # svátky a měsíce slovy → čísla, která v datu smějí být
    tf = _f(t)
    if re.search(r"vanoc|stedr", tf): out |= {12, 24, 25, 26}
    if re.search(r"silvestr", tf): out |= {12, 31}
    if re.search(r"novy\s+rok|novym\s+rok", tf): out |= {1}
    if re.search(r"leto|prazdnin", tf): out |= {6, 7, 8, 21, 30, 31}
    for i, mes in enumerate(["led", "unor", "bre", "dub", "kvet", "cervn", "cerven", "srp", "zari", "rij", "listop", "prosin"]):
        if re.search(r"\b" + mes, tf):
            out.add(i + 1 if mes != "cerven" else 7)
    return out


def _cisla_bez_opory(vyraz: str, q: str, navic=()) -> list:
    z = _cisla_otazky(q) | set(navic)   # HANS_VYPOCTY_WD_V1 — data z Wikidat
    dnes = _dt.date.today()
    z |= {dnes.year, dnes.month, dnes.day, dnes.year + 1}   # HANS_VYPOCTY_PRISTI_DATUM_V1: „do 1. května“ = příští rok
    bez = []
    for m in re.finditer(r"(?<![\w.])\d+(?:\.\d+)?(?:[eE][+-]?\d+)?", vyraz or ""):
        x = float(m.group(0))
        if x in _BEZNA or x in z:
            continue
        # procenta jako desetinné číslo (0.06 za „6 %“) a podíl (/100) — povolit x*100 z otázky
        if any(abs(x * 100 - y) < 1e-9 or abs(x - y / 100) < 1e-9 for y in z):
            continue
        bez.append(m.group(0))
    return bez


# ── HANS_VYPOCTY_WD_V1 (1. 11.) — data osob z Wikidat ───────────────────────
# Uživatel 31. 10.: „kolik let bylo Mozartovi, když zemřel“, „kolik let by dnes bylo
# Havlovi“ — pojistka výš taková čísla zahodí (nejsou v otázce). Osoba v otázce
# (přesný článek lokální Wikipedie, převod do 1. pádu, typ osoba) → narození
# a úmrtí z Wikidat (P569/P570) → do zadání modelu jako známá data a jejich čísla
# smějí do výrazu. Bere se jen jisté datum: preferovaná hodnota (jinak všechny
# běžné musí souhlasit), přesnost aspoň rok („kolem 1370“ u Husa ne), ne př. n. l.
# (mezipaměť hans_overeni znaménko ztrácí → vlastní dotaz). Juliánský kalendář:
# věk sedí, den v týdnu ne → výraz s den_v_tydnu se zahodí.
# 📏 data/mereni/vypocty_wd_1_11/: 20 otázek → osoba a data 17/20 (chybí samotné
# příjmení s rozcestníkem a „Marie Curie“); 1 420 zpráv z chatu → 0 spuštění.
CUE_OSOBA = re.compile(
    r"\b(kolik\s+let|v\s+kolika\s+letech|jak\s+star\w*|doz\w*l\w*\s+se|se\s+doz\w*l\w*|"
    r"jak\s+dlouho\s+(?:zil|zila|zili)|kolik\s+(?:dni|tydnu|mesicu|roku)\s+(?:zil|zila)|"
    r"o\s+kolik\s+(?:let|roku)|kolik\s+(?:je|bylo|bude)\s+(?:to\s+)?let|"
    r"(?:jaky|ktery|v\s+jaky)\s+den|den\s+v\s+tydnu|kolik\s+dni|"
    r"kolik\s+by\s+(?:mu|ji|jim|jemu)\s+(?:bylo|bude)|"   # HANS_VYPOCTY_ZAJMENO_V1
    # HANS_VYPOCTY_CUE_V2 (2. 11., /tazatel A+B) — „kolik by dnes bylo Karlu Čapkovi“,
    # „kolik mu (tehdy) bylo, když zemřel“, „kolik by mu dnes bylo“: výpočet se nespustil
    # a model počítal z hlavy (53 let, „narozen 2024“). 📏 3 979 vět: 5 nových, všechny na věk.
    r"kolik\s+(?:by\s+)?(?:(?:mu|ji|jim|jemu|dnes|tehdy|ted|vlastne|asi|zhruba)\s+){1,3}(?:bylo|bude|je)|"
    r"kolik\s+by\s+(?:(?:dnes|ted|asi)\s+)?bylo)\b")
# HANS_VYPOCTY_ZAJMENO_V1 (1. 11., /tazatel) — navazující „a kolik by mu bylo dnes?“:
# osoba z předchozích otázek (jako rok u „tehdy“, HANS_YEAR_EVENTS_V1); příbuzní
# („jeho otec“, „manželce“) ne — jejich data nemáme, model by vzal data osoby.
_ZAJMENO = re.compile(r"\b(mu|jemu|ji|jim|jeho|jej|on|ona|nej|nemu)\b")
_PRIBUZNI = re.compile(r"\b(otec|otce|otci|otcem|matk\w*|manzel\w*|chot\w*|syn\w*|dcer\w*|bratr\w*|"
                       r"sestr\w*|dite|ditet\w*|deti|vnuk\w*|vnuc\w*|dedeck\w*|babick\w*|predk\w*|potom\w*)\b")
_WD_CACHE = "data/wd_zivot.json"
_wd_c = None


def _wd_cache() -> dict:
    global _wd_c
    if _wd_c is None:
        try:
            _wd_c = json.load(open(_WD_CACHE, encoding="utf-8"))
        except Exception:
            _wd_c = {}
    return _wd_c


def _osoba_titul(kus: str):
    from scripts import hans_wiki_offline as wo
    from scripts.kolac_podnety import _typ_clanku
    tvary = [kus] + (wo._prvni_pad_vic(kus) if " " in kus else wo._tvary_slova(kus))
    for z in tvary:
        r = wo._cil(z[0].upper() + z[1:])
        if not r:
            continue
        if wo._db().execute("SELECT rozcestnik FROM clanek WHERE id=?", (r[0],)).fetchone()[0]:
            t = wo.vyznam_prevazujici(r)
            r = wo._cil(t) if t else None
            if not r:
                continue
        if _typ_clanku(r) == "osoba":
            return r[1]
    if " " in kus:
        # HANS_OSOBA_DRUH_INFOBOXU_V1 (3. 11.) — 1. pád bez ohledu na háčky: „Antonímu
        # Gaudímu“ → „Antoní Gaudí“ ≠ titul „Antoni Gaudí“; jen JEDINÝ článek (ne rozcestník)
        try:
            for z in tvary[1:]:
                ts = [x for (x,) in wo._fold_db().execute(
                    "SELECT titul FROM t WHERE fold=? AND rozcestnik=0 LIMIT 3", (wo._fold_tzv(z),))]
                if len(ts) == 1:
                    r = wo._cil(ts[0])
                    if r and _typ_clanku(r) == "osoba":
                        return r[1]
        except Exception as e:
            _log.debug("HANS_OSOBA_DRUH_INFOBOXU_V1: %s", e)
    if " " not in kus:                                   # HANS_VYPOCTY_PRIJMENI_V1
        for z in tvary:
            t = _prijmeni_slavna(z[0].upper() + z[1:])
            if t:
                return t
    return None


# HANS_VYPOCTY_PRIJMENI_V1 (1. 11.) — samotné příjmení („kolik by bylo Havlovi“, „od
# smrti Smetany“): kandidáti ze stránky „X (příjmení)“ / rozcestníku, sláva = počet
# Wikipedií osoby (sitelinks, jeden dotaz). Vezme se, jen když má nejslavnější aspoň 40
# a 3× víc než druhý — 📏 28 příjmení: Havel 144:17, Smetana 93:10, Kafka 242:14 ✓;
# Neruda (Pablo 167 : Jan 66), Kennedy, Čapek (110:54), Hašek, Curie → nic (radši žádný
# výpočet než cizí osoba). Výsledek do mezipaměti data/wd_zivot.json („P:<příjmení>“).
def _prijmeni_slavna(p: str):
    try:
        if len(p) < 3 or not p[:1].isupper():
            return None
        c = _wd_cache()
        if "P:" + p in c:
            return c["P:" + p] or None
        from scripts import hans_wiki_offline as wo
        from scripts.kolac_podnety import _typ_clanku
        db = wo._db()
        txt = ""
        for tt in (p + " (příjmení)", p + " (rozcestník)", p):
            r = wo._cil(tt)
            if r:
                row = db.execute("SELECT rozcestnik, text, titul FROM clanek WHERE id=?", (r[0],)).fetchone()
                if row and (row[0] or "příjmení" in row[2]):
                    txt = row[1] or ""
                    break
        kand = []
        for ln in txt.splitlines():
            m = re.match(r"\s*([^–\n(]{3,60}?)\s*(?:\(|–|,)", ln)
            if not m:
                continue
            tt = m.group(1).strip(" .\"")
            if _f(p)[:4] not in _f(tt) or len(tt.split()) < 2:
                continue
            r = wo._cil(tt)
            if r and r[1] not in kand and _typ_clanku(r) == "osoba":
                kand.append(r[1])
        vys = None
        if kand:
            import requests
            from scripts.hans_facts import USER_AGENT
            rq = requests.get("https://www.wikidata.org/w/api.php", timeout=4, headers={"User-Agent": USER_AGENT},
                              params={"action": "wbgetentities", "sites": "cswiki", "titles": "|".join(kand[:50]),
                                      "props": "sitelinks", "format": "json"})
            if rq.status_code != 200:
                _log.info("HANS_VYPOCTY_PRIJMENI_V1: Wikidata %r: HTTP %s", p, rq.status_code)
                return None                              # neukládat — zkusí se příště
            sl = []
            for e in (rq.json().get("entities") or {}).values():
                s = e.get("sitelinks") or {}
                cs = (s.get("cswiki") or {}).get("title")
                if cs:
                    sl.append((len(s), cs))
            sl.sort(reverse=True)
            if sl and sl[0][0] >= 40 and (len(sl) == 1 or sl[0][0] >= 3 * sl[1][0]):
                vys = sl[0][1]
            _log.info("HANS_VYPOCTY_PRIJMENI_V1: %r → %r (%s)", p, vys, sl[:3])
        c["P:" + p] = vys or ""
        try:
            json.dump(c, open(_WD_CACHE, "w", encoding="utf-8"), ensure_ascii=False)
        except Exception:
            pass
        return vys
    except Exception as e:
        _log.debug("HANS_VYPOCTY_PRIJMENI_V1: %s", e)
        return None


_VYKANI = {"vy", "vas", "vam", "vami", "vas", "vase", "vasi", "vaseho", "vasemu", "vasim", "vasich",
           "vasimi", "ty", "tebe", "tobe", "tebou", "pane", "pani"}


_BEZNA_SLOVA = {"dekuji", "diky", "prosim", "tak", "takze", "jak", "dobry", "dobre", "ted", "rad", "rada",
                "ano", "ne", "no", "jo", "a", "ale", "ahoj", "pak", "potom", "jeste", "kdyz", "pokud",
                "mozna", "vlastne", "mimochodem", "zajimalo", "zajima", "zkuste", "spocitejte", "rekni",
                "reknete", "vypocitejte", "spocitej", "jaky", "jaka", "jake", "ktery", "ktera", "ktere"}


def osoby(q: str, strop: int = 2) -> list:
    """Tituly článků osob zmíněných v otázce (velké počáteční písmeno, 1–3 slova)."""
    return osoby_a_zbytek(q, strop)[0]


def osoby_a_zbytek(q: str, strop: int = 2):
    """(tituly osob, slova s velkým písmenem, která žádné osobě nepatří).
    HANS_VYPOCTY_WD_V2: zbytek = jméno, ke kterému data nemáme (Schubert s rozcestníkem,
    Kolumbus nad strop, „Nobelovu“, „Mou vlast“) → fakta se nepřidají, jinak model
    vezme data jiné osoby (měření 1. 11.: Beethoven − Schubert = 57 let)."""
    zbytek = []
    try:
        from scripts import hans_wiki_offline as wo
        if not wo.dostupna():
            return [], []
        _m = list(re.finditer(r"[^\W\d_][\w\-]*\.?", q or ""))
        w = [m.group(0) for m in _m]
        # HANS_VYPOCTY_JMENA_V1 (1. 11., /tazatel B) — velké písmeno na začátku věty
        # („…, prosím? Kolik let…“, „Děkuji. A kolik…“) a vykání („Vám“) není jméno;
        # 4 z 20 tahů zdvořilého tazatele tím přišly o data z Wikidat.
        _zac = {k for k, m in enumerate(_m)
                if not (q[:m.start()].rstrip()) or q[:m.start()].rstrip()[-1] in ".?!…:;\"„“"}
        # na začátku věty jméno jen s heslem na Wikipedii a mimo běžná slova
        # („Schubert, o kolik…“ ano; „Kolik“, „Ještě“ nemají heslo; „Děkuji“ je album)
        _neni_jmeno = lambda k: (_f(w[k].rstrip(".")) in _VYKANI or (k in _zac and (
            _f(w[k].rstrip(".")) in _BEZNA_SLOVA or not wo._cil(w[k].rstrip(".")))))
        out, i = [], 0
        while i < len(w) and len(out) < strop:
            hit = None
            if w[i][:1].isupper():
                for n in (4, 3, 2, 1):   # HANS_VYPOCTY_JMENO4_V1 — „Johann Wolfgang von Goethe“
                    if i + n > len(w):
                        continue
                    kus = " ".join(w[i:i + n])
                    if not re.search(r"\b[IVX]+\.$", kus):
                        kus = kus.rstrip(".")
                    if len(kus) < 4:
                        continue
                    t = _osoba_titul(kus)
                    if t:
                        hit = (t, n)
                        break
            if hit:
                if hit[0] not in out:
                    out.append(hit[0])
                i += hit[1]
            else:
                if w[i][:1].isupper() and len(w[i].rstrip(".")) >= 3 and not _neni_jmeno(i):
                    zbytek.append(w[i])
                i += 1
        zbytek += [w[k] for k in range(i, len(w))
                   if w[k][:1].isupper() and len(w[k].rstrip(".")) >= 3 and not _neni_jmeno(k)]
        return out, zbytek
    except Exception as e:
        _log.debug("HANS_VYPOCTY_WD_V1: osoby: %s", e)
        return [], []


def _datum_wd(claims: list):
    """Tvrzení Wikidat → [rok, měsíc, den, juliánský] (0 = neznámé), nebo None."""
    cl = [c for c in claims if c.get("rank") != "deprecated"]
    cl = [c for c in cl if c.get("rank") == "preferred"] or cl
    hodn = set()
    for c in cl:
        v = ((c.get("mainsnak") or {}).get("datavalue") or {}).get("value") or {}
        m = re.match(r"([+-])0*(\d+)-(\d\d)-(\d\d)", v.get("time") or "")
        if not m or m.group(1) == "-":
            return None
        p = int(v.get("precision") or 0)
        if p < 9:
            return None
        r, mes, den = int(m.group(2)), int(m.group(3)), int(m.group(4))
        hodn.add((r, mes if p >= 10 else 0, den if p >= 11 else 0,
                  (v.get("calendarmodel") or "").endswith("Q1985786")))
    return list(hodn.pop()) if len(hodn) == 1 else None


def zivot(titul: str, timeout: float = 4.0):
    """{'qid', 'nar', 'zem'} z Wikidat (mezipaměť data/wd_zivot.json), None = nedostupné."""
    C = _wd_cache()
    if titul in C:
        return C[titul]
    try:
        import requests
        from scripts.hans_facts import USER_AGENT
        r = requests.get("https://www.wikidata.org/w/api.php",
                         params={"action": "wbgetentities", "sites": "cswiki", "titles": titul,
                                 "props": "claims", "format": "json"},
                         headers={"User-Agent": USER_AGENT}, timeout=timeout)
        if r.status_code != 200:
            _log.info("HANS_VYPOCTY_WD_V1: Wikidata %r: HTTP %s", titul, r.status_code)
            return None
        out = {"qid": None, "nar": None, "zem": None}
        for q, e in (r.json().get("entities") or {}).items():
            if q.startswith("Q"):
                out["qid"] = q
                out["nar"] = _datum_wd((e.get("claims") or {}).get("P569") or [])
                out["zem"] = _datum_wd((e.get("claims") or {}).get("P570") or [])
    except Exception as e:
        _log.info("HANS_VYPOCTY_WD_V1: Wikidata %r: %s", titul, str(e)[:80])
        return None
    C[titul] = out
    try:
        json.dump(C, open(_WD_CACHE + ".tmp", "w", encoding="utf-8"), ensure_ascii=False)
        os.replace(_WD_CACHE + ".tmp", _WD_CACHE)
    except Exception:
        pass
    return out


# ── HANS_VYPOCTY_UDALOST_ROK_V1 (2. 11., /tazatel A) ───────────────────────
# „kolik let uplynulo od konce druhé světové války?“ — rok 1945 v otázce není, kontrola ho
# zahodila a model pak počítal z hlavy („dnes je 2024, 79 let“). Rok mimo otázku teď projde,
# jen když je to datum UDÁLOSTI za „od …“ ve Wikidatech (začátek, konec, okamžik, vznik,
# zánik, otevření, vydání) u článku, který se našel podle té fráze a sdílí s ní slovo.
_UDAL_PROP = (("P582", "konec"), ("P580", "začátek"), ("P585", "datum"), ("P571", "vznik"),
              ("P576", "zánik"), ("P1619", "otevření"), ("P577", "vydání"))
_UDAL_UVOD = re.compile(r"^(?:konce|skončení|ukončení|začátku|zahájení|vypuknutí|vzniku|založení|"
                        r"stavby|dostavby|otevření|podpisu|vydání)\s+", re.I)


def roky_udalosti(q: str, f1=None, timeout: float = 4.0) -> dict:
    """{rok: (titul článku, popis vlastnosti)} pro událost za „od …“ v otázce, nebo {}."""
    out = {}
    try:
        from scripts import hans_wiki_offline as wo
        if not wo.dostupna():
            return out
        frazy = []
        for zdroj in (f1, q):
            m = re.search(r"\bod\s+(?:roku\s+)?(.{4,80}?)\s*(?:\?|$|,|\s+do\s)", zdroj or "")
            if m and not re.match(r"\d", m.group(1)):
                frazy.append(m.group(1).strip())
        kand = []
        for fr in frazy:
            holo = _UDAL_UVOD.sub("", fr)
            for t in (wo.srsearch(fr, 3) or []) + (wo.srsearch(holo, 3) or []) + (wo.prefixsearch(holo, 2) or []):
                kmeny = {_f(w)[:4] for w in re.findall(r"\w{4,}", holo)}
                if t not in kand and kmeny & {_f(w)[:4] for w in re.findall(r"\w{4,}", t)}:
                    kand.append(t)
        kand = kand[:4]
        if not kand:
            return out
        import requests
        from scripts.hans_facts import USER_AGENT
        r = requests.get("https://www.wikidata.org/w/api.php",
                         params={"action": "wbgetentities", "sites": "cswiki", "titles": "|".join(kand),
                                 "props": "claims|sitelinks", "sitefilter": "cswiki", "format": "json"},
                         headers={"User-Agent": USER_AGENT}, timeout=timeout)
        if r.status_code != 200:
            _log.info("HANS_VYPOCTY_UDALOST_ROK_V1: Wikidata HTTP %s", r.status_code)
            return out
        for q_id, e in (r.json().get("entities") or {}).items():
            if not q_id.startswith("Q"):
                continue
            tit = ((e.get("sitelinks") or {}).get("cswiki") or {}).get("title") or q_id
            for prop, lab in _UDAL_PROP:
                d = _datum_wd((e.get("claims") or {}).get(prop) or [])
                if d and d[0] and d[0] not in out:
                    out[d[0]] = (tit, lab)
    except Exception as e:
        _log.info("HANS_VYPOCTY_UDALOST_ROK_V1: %s", str(e)[:80])
    return out


def _wd_fmt(d) -> str:
    s = ("datum(%d, %d, %d)" % (d[0], d[1], d[2]) if d[2] else
         "%d (only the year is known — use the year number)" % d[0])
    return s + (" [Julian calendar]" if d[3] else "")


def _wd_cz(d) -> str:
    return ("%d. %d. %d" % (d[2], d[1], d[0]) if d[2] else "roku %d" % d[0])


def fakta_osob(q: str, predchozi=(), f1=None, odpoved=""):
    """(věta pro model, věta pro Hanse, povolená čísla, juliánský, data, osoba z historie)
    nebo None. `predchozi` = dřívější otázky téhož člověka, nejnovější první."""
    if not CUE_OSOBA.search(_f(q)):
        return None
    if _PRIBUZNI.search(_f(q)):
        _log.info("HANS_VYPOCTY_ZAJMENO_V1: otázka na příbuzného → bez faktů")
        return None
    # HANS_VYPOCTY_UDALOST_V1 (1. 11.) — věk při jiné události než narození/úmrtí („začal
    # skládat“, „když dostal Nobelovu cenu / složil / se stal…“): data té události nemáme;
    # model to dřív poznal jen někdy (Mozart „začal skládat“ → 270 let)
    _fq = _f(q)
    if re.search(r"\bzac(?:al|ala|ali|alo)\b", _fq) or (
            re.search(r"\b(?:kdyz|v\s+dobe\s+kdy|pri\s+(?:udeleni|nastupu|vydani|zalozeni|svatbe))\b", _fq)
            and not re.search(r"\b(?:zemrel\w*|umrel\w*|zahynul\w*|narodil\w*|zesnul\w*|zavrazd\w*|popraven\w*|upalen\w*|upalili)\b", _fq)):
        _log.info("HANS_VYPOCTY_UDALOST_V1: věk při jiné události → bez faktů")
        return None
    rad, cz, cis, jul, trojice = [], [], set(), False, set()
    if re.search(r"\b(vladl\w*|kraloval\w*|panoval\w*|uradoval\w*|prezident\w*|ve\s+funkci|na\s+trune)", _f(q)):
        _log.info("HANS_VYPOCTY_WD_V2b: otázka na úřad/vládu → bez faktů")   # „kolik let vládl Karel IV.“ ≠ délka života
        return None
    _os, _zb = osoby_a_zbytek(q)
    _z_hist = ""
    # HANS_VYPOCTY_F1_OSOBA_V1 (2. 11., /tazatel) — jméno v jiném pádě nebo bez háčků
    # („Capek“, „Karlu Capkovi“) a osoba jen z Hansovy odpovědi („ten autor“) najde až
    # přepis otázky F1. Přepis si ale jméno umí vymyslet (2. 11.: Cimrman → Šternberk),
    # proto osoba z přepisu jen s kotvou: kmen jména ve větě, v dřívějších otázkách
    # nebo v Hansově poslední odpovědi. 📏 přepisy tazatele: 24 vět s nenalezenou osobou
    # → 5 vezme přepis; Edison/žárovka (bez jména i zájmena ve větě) se nebere.
    def _z_prepisu(duvod):
        if not f1:
            return None
        _o1, _z1 = osoby_a_zbytek(f1)
        _kt = _f(" ".join([q] + list(predchozi or ())[:4] + [odpoved or ""]))
        if _o1 and not _z1 and all(any(_f(w)[:4] in _kt for w in o.split() if len(w) > 3) for o in _o1):
            _log.info("HANS_VYPOCTY_F1_OSOBA_V1: osoba z přepisu %s (věta: %s)", _o1, duvod)
            return _o1
        return None
    if _zb:
        _o1 = _z_prepisu(_zb)
        if _o1:
            _os, _zb = _o1, []
    if _zb:
        _log.info("HANS_VYPOCTY_WD_V2: jména bez dat %s → bez faktů", _zb[:4])
        return None
    if not _os and _ZAJMENO.search(_f(q)):
        for p in list(predchozi or ())[:4]:
            _po, _pz = osoby_a_zbytek(p)
            if _po:
                if len(_po) == 1 and not _pz:      # jen jednoznačně jedna osoba
                    _os, _z_hist = _po, _po[0]
                    _log.info("HANS_VYPOCTY_ZAJMENO_V1: zájmeno → %s (z předchozí otázky)", _z_hist)
                break
        if not _os:
            _o1 = _z_prepisu("zájmeno")
            if _o1:
                _os = _o1
                _z_hist = _o1[0] if len(_o1) == 1 else ""
    for o in _os:
        z = zivot(o)
        if not z or not (z.get("nar") or z.get("zem")):
            continue
        en, kc = [], []
        for k, lab, lc in (("nar", "birth", "datum narození"), ("zem", "death", "datum úmrtí")):
            d = z.get(k)
            if d:
                en.append("%s = %s" % (lab, _wd_fmt(d)))
                if d[2]:
                    trojice.add((d[0], d[1], d[2]))
                kc.append("%s %s" % (lc, _wd_cz(d)))
                cis |= {float(x) for x in d[:3] if x}
                jul = jul or bool(d[3])
        if z.get("nar") and not z.get("zem"):
            en.append("no death date recorded (probably alive)")
        rad.append("%s: %s" % (o, ", ".join(en)))
        cz.append("%s %s" % (o, ", ".join(kc)))
    if not rad:
        return None
    return ("Known facts from Wikidata: " + "; ".join(rad) + "." + (
                " The pronoun in the question (mu, jeho, on…) means %s." % _z_hist if _z_hist else ""),
            "údaje z Wikidat: " + "; ".join(cz), cis, jul, trojice, _z_hist)


_SYS_OSOBA = (   # HANS_VYPOCTY_WD_V2 — jen když jsou data osoby
    " Write a date of the person ALWAYS as the full datum(...) given above, never as a bare "
    "year. Age at death or how long someone lived = vek(birth, death); age today (also 'kolik let by dnes bylo X' for a dead person) = vek(birth, "
    "dnes()); years since death ('kolik je to let, co zemřel', 'kolik let uplynulo od smrti') = vek(death, dnes()) — not the age at death; years since birth = vek(birth, dnes()); age "
    "in year Y = Y - birth_year; how many years A was older or younger than B = vek(earlier birth, later birth) (compare BIRTH dates, not lifespans); lifespan in days = dny(birth, death); weekday of birth = "
    "den_v_tydnu(birth). Use these dates only for the person they belong to. If the question needs "
    "any other date or fact (e.g. when someone became president, won a prize, wrote a work or "
    "started something), output {\"vyraz\": \"\"}.")


def _data_bez_opory(vyraz: str, q: str, fo) -> list:
    """HANS_VYPOCTY_WD_V2: datum(r, m, d) ve výrazu s daty osoby musí být datum z Wikidat
    nebo mít rok z otázky (Smetana: vek(1824, datum(2026, 12, 24)) = vymyšlený den)."""
    z = _cisla_otazky(q)
    bez = []
    for m in re.finditer(r"datum\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\)", vyraz or ""):
        r3 = tuple(int(x) for x in m.groups())
        if r3 not in fo[4] and float(r3[0]) not in z:
            bez.append(m.group(0))
    return bez


# ── rozpoznání ───────────────────────────────────────────────────────────────
_CUE = re.compile(
    r"\b(procent\w*|urok\w*|splat\w*|hypotek\w*|uver\w*|pujck\w*|zdrazil\w*|zlevnil\w*|sleva|slevou|"
    r"prumer\w*|rovnic\w*|vyres\w*\s+(?:rovnic\w*|\d|x)|kolik\s+(?:dni|tydnu|mesicu)\s+(?:zbyva|zbyvaji|do|mezi|od|uplynulo|ubehlo|je\s+(?:do|mezi|od))|jake\s+datum|uplynul\w*\s+od|kolik\s+let\s+(?:by|bylo|je|bude|mu|ji|jim)|"
    r"jaky\s+den|ktery\s+den|jaky\s+byl\s+den|v\s+jaky\s+den|den\s+v\s+tydnu|za\s+kolik\s+dni|"
    r"do\s+vanoc|do\s+silvestr\w*|do\s+velikonoc|kdy\s+jsou\s+velikonoce|kdy\s+budou\s+velikonoce|"
    r"kolik\s+zaplatim|kolik\s+usetr\w*|kolik\s+(?:je|bude|vyjde|stoji)\s+\d|rozdel\w*\s+(?:na|mezi|do)|kazdy\s+dostane|"
    r"za\s+kus|na\s+osobu|kolikrat|pomer\w*|kazdy|zbude|zbyde)\b|%")
_OTAZ = re.compile(r"\b(kolik\w*|jak\w*|jaky|jaka|jake|ktery|kdy|vypocit\w*|spocit\w*|spocti|vyres\w*|urci)\b|\?")
_NE = re.compile(r"\b(hodin\s+je|je\s+hodin|kolik\s+je\s+ti|kolik\s+ti\s+je|tvuj|tve|tobe|vas\b|vam\b|"
                 r"umis|umite|umel\w*|dokazes|dokazete|zvladnes|zvladnete|muzes\s+spocit\w*|muzete\s+spocit\w*)\b")


def vypada_jako_vypocet(text: str) -> bool:
    t = _f(re.sub(r"^\S+\s+se\s+pt[áa]:\s*", "", text or ""))
    if not t or not _OTAZ.search(t) or _NE.search(t):
        return False
    if _CUE.search(t):
        return True
    # HANS_VYPOCTY_DNY_POREDI_V1 (4. 11., /tazatel) — „a do prvního května kolik dní?“ (opačný
    # slovosled) výpočet nespustil a model napsal 156 místo 178. 📏 4 030 vět: 1 nová (tahle).
    if re.search(r"(?=[^?!]*\b(?:do|zbyva|zbyvaji|od|mezi|uplynulo)\b)[^?!]*\bkolik\s+(?:dni|tydnu|mesicu)\b", t):
        return True
    # slovní úloha bez klíčového slova: aspoň dvě čísla a „kolik“
    return bool(re.search(r"\bkolik", t) and len(re.findall(r"\d+(?:[.,]\d+)?", t)) >= 2)


# ── formát výsledku ──────────────────────────────────────────────────────────
def _cz(x) -> str:
    if isinstance(x, _dt.date):
        return "%d. %s %d (%s)" % (x.day, _MESICE[x.month - 1], x.year, _DNY_CZ[x.weekday()])
    if isinstance(x, str):
        return x
    if isinstance(x, tuple):
        return " nebo ".join(_cz(v) for v in x)
    if isinstance(x, bool):
        return str(x)
    if isinstance(x, int) or (isinstance(x, float) and abs(x - round(x)) < 1e-9 and abs(x) < 1e15):
        s = "%d" % round(x)
    else:
        s = ("%.2f" % x) if abs(x) >= 1 else ("%.4g" % x)
        s = s.rstrip("0").rstrip(".") if "." in s else s
    s = s.replace(".", ",")
    mm = re.match(r"^(-?)(\d+)(,\d+)?$", s)
    if mm and len(mm.group(2)) > 4:
        c = mm.group(2)
        s = mm.group(1) + " ".join([c[max(0, i - 3):i] for i in range(len(c), 0, -3)][::-1]) + (mm.group(3) or "")
    return s


_SYS = (
    "Today is {dnes}. You convert a question into ONE expression for a calculator. Do NOT compute "
    "the result. Allowed: numbers, + - * / ** % //, parentheses and ONLY these functions: "
    "sqrt(x), ln(x), log10(x), exp(x), abs(x), round(x, n), min(...), max(...), prumer(a, b, ...), "
    "procento(part, whole) [what percent part is of whole], procento_zmeny(old, new) [percent change], splatka(principal, yearly_rate_percent, years) "
    "[monthly annuity payment], slozene(principal, yearly_rate_percent, years) [compound value], "
    "reseni(\"equation in x\") [solve, e.g. reseni(\"2*x + 3 = 11\")], datum(year, month, day), "
    "dnes(), dny(date_a, date_b) [number of days between], pricti_dny(date, n), den_v_tydnu(date), "
    "vek(birth_date_or_year, date) [full years], velikonoce(year) [Easter Sunday], rok(date). Weeks = dny(...) / 7. This year is {rok}; letos means {rok}. "
    "Use ONLY numbers that appear in the question (plus 12, 100, 365, 7 etc. for unit conversions); "
    "never invent dates, rates or prices that are not in the question. Christmas Eve = 24 December, "
    "New Year's Eve = 31 December. Output ONLY JSON: {{\"vyraz\": \"...\", \"jednotka\": \"<unit or "
    "empty>\", \"co\": \"<short English name of the result>\"}}. If the question cannot be answered "
    "from its own numbers and today's date (e.g. needs facts about a person), output {{\"vyraz\": \"\"}}.")


# HANS_VYPOCTY_NAHRATI_V1 (1. 11.) — qwen3:30b se na CPU Ollamě načítá ~73 s, výpočet čeká
# 25 s a Ollama při odpojení klienta načítání ZRUŠÍ → po 10 min nečinnosti model už nikdy
# nenaskočil (test 1. 11.: 12/12 volání „Read timed out“, Hans pak počítal z hlavy).
# Proto: nahřát na pozadí při zprávě v chatu a po timeoutu, s vlastním dlouhým čekáním.
_NAHR = {"bezi": False, "ts": 0.0}


def _render_bezi() -> bool:
    try:
        import sqlite3
        c = sqlite3.connect("data/hans_diary.db", timeout=2)
        try:
            return bool(c.execute("SELECT 1 FROM heavy_jobs WHERE status='running' LIMIT 1").fetchone())
        finally:
            c.close()
    except Exception:
        return False


def nahrej_na_pozadi(config: dict, duvod: str = "") -> bool:
    """Načte (nebo jen prodlouží) qwen3 na CPU Ollamě ve vlákně; nečeká. Ne při hře,
    noční dávce ani renderu (RAM na PC)."""
    c = (config or {}).get("vypocty_model", {}) or {}
    if not c.get("enabled", True) or not c.get("nahrivat", True):
        return False
    if _NAHR["bezi"] or time.time() - _NAHR["ts"] < 60:
        return False
    if not _volno() or _render_bezi():
        return False
    try:
        from scripts.hans_book_summary import _cpu_url
        url = _cpu_url(config)
    except Exception:
        return False
    if not url:
        return False
    _NAHR.update(bezi=True, ts=time.time())
    model, ka = c.get("model", "qwen3:30b"), c.get("keep_alive", "30m")

    def _run():
        t0 = time.time()
        try:
            if not pamet_pro_qwen3(config, "nahřátí: " + duvod):   # HANS_VYPOCTY_RAM_V1
                return
            import requests
            requests.post(url + "/api/generate", json={"model": model, "keep_alive": ka,
                          # stejné okno jako výpočet a fyzika — jiné num_ctx = nové načtení
                          "options": {"num_ctx": 2048}}, timeout=240)
            if time.time() - t0 > 5:
                _log.info("HANS_VYPOCTY_NAHRATI_V1: %s načten za %.0f s (%s)", model, time.time() - t0, duvod)
        except Exception as e:
            _log.info("HANS_VYPOCTY_NAHRATI_V1: nahřátí selhalo (%s): %s", duvod, str(e)[:80])
        finally:
            _NAHR["bezi"] = False

    threading.Thread(target=_run, daemon=True, name="vypocty-nahrati").start()
    return True


# HANS_VYPOCTY_RAM_V1 (3. 11.) — ráno 3. 11. PC zamrzlo: qwen3:30b (~18 GB RAM na CPU)
# nahřátý zprávou v chatu vedle hans-czech a mezipamětí promptů → swap → uváznutí
# ovladače grafiky (amdgpu SVM). Než se qwen3 NAČTE (už načtený jen prodlouží), musí
# mít PC dost volné RAM (stejné měření jako `HANS_BOOK_SUMMARY_RAM_V1`). Každé
# rozhodnutí do logu (povoleno i odmítnuto), ať jde spočítat, jak často to brzdí.
def pamet_pro_qwen3(config: dict, kde: str = "") -> bool:
    c = (config or {}).get("vypocty_model", {}) or {}
    model = str(c.get("model", "qwen3:30b"))
    try:
        from scripts.hans_book_summary import _ps, _cpu_url
        if any(m == model or m == model + ":latest" for m in _ps(_cpu_url(config))):
            return True
        from scripts import pc_remote
        volno = (pc_remote.telemetry(config) or {}).get("ram_avail_gb")
    except Exception as e:
        _log.info("HANS_VYPOCTY_RAM_V1: %s — RAM nejde změřit (%s), nenačítám", kde, str(e)[:60])
        return False
    need = float(c.get("min_ram_gb_nacteni", 22))
    if volno is None or volno < need:
        _log.info("HANS_VYPOCTY_RAM_V1: %s — nenačítám %s, volná RAM na PC %s < %.0f GB",
                  kde, model, "?" if volno is None else "%.1f GB" % volno, need)
        return False
    _log.info("HANS_VYPOCTY_RAM_V1: %s — načítám %s, volná RAM na PC %.1f GB", kde, model, volno)
    return True


def _volno() -> bool:
    try:
        from scripts.ollama_client import game_mode_on, base_slot_busy
        return not game_mode_on() and not base_slot_busy()
    except Exception:
        return True


# HANS_VYPOCTY_PRISTI_DATUM_V1 (4. 11.) — „kolik dní zbývá do 1. května?“ v listopadu:
# model napíše datum(letošní rok, 5, 1) a `dny` (bez znaménka) vrátí dny OD minulého
# května (183 místo 179; chyba všech měřených modelů 31. 10. i 3. 11.). Otázka bez
# roku, která míří dopředu, myslí nejbližší budoucí výskyt → letošní datum, které už
# bylo, posune kód o rok. Otázka do minulosti („od“, „uplynulo“, „před“) beze změny.
_DOPREDU_RE = re.compile(r"(?:zb[yý]v|\bdo\b|za\s+kolik|kdy\s+bude|p[rř][ií][sš]t[ií])", re.I)
_DOZADU_RE = re.compile(r"(?:\bod\b|uplynul|ub[eě]hl|\bp[rř]ed\b|\bbylo\b|\bbyl[ao]?\b)", re.I)


def pristi_datum(vyraz: str, q: str) -> str:
    if not vyraz or re.search(r"\b\d{4}\b", q or "") or not _DOPREDU_RE.search(q or "") \
            or _DOZADU_RE.search(q or ""):
        return vyraz
    dnes = _dt.date.today()

    def _sub(m):
        r, me, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            if r == dnes.year and _dt.date(r, me, d) < dnes:
                return "datum(%d, %d, %d)" % (r + 1, me, d)
        except ValueError:
            pass
        return m.group(0)
    v2 = re.sub(r"datum\(\s*(\d{4})\s*,\s*(\d{1,2})\s*,\s*(\d{1,2})\s*\)", _sub, vyraz)
    if v2 != vyraz:
        _log.info("HANS_VYPOCTY_PRISTI_DATUM_V1: datum už bylo → příští rok: %s → %s", vyraz[:80], v2[:80])
    return v2


def vypocet_modelem(config: dict, text: str, timeout: float = 25.0, predchozi=(),
                    f1=None, odpoved="") -> Optional[str]:
    c = (config or {}).get("vypocty_model", {}) or {}
    if not c.get("enabled", True):
        return None
    q0 = re.sub(r"^\S+\s+se\s+pt[áa]:\s*", "", text or "").strip()
    _fo = (fakta_osob(q0, predchozi, f1=f1, odpoved=odpoved)   # HANS_VYPOCTY_F1_OSOBA_V1
           if c.get("wikidata", True) else None)   # HANS_VYPOCTY_WD_V1
    # HANS_VYPOCTY_UDALOST_ROK_V1 — data události za „od …“ předem jako známá fakta
    # (model jinak rok buď vymyslel, nebo poslechl zákaz a nevrátil nic — Bílá hora)
    _ru = {}
    if not _fo and re.search(r"\bod\b", _f(q0)) and (CUE_OSOBA.search(_f(q0)) or vypada_jako_vypocet(text)):
        _ru = roky_udalosti(q0, f1)
    if not _fo and not _ru and not vypada_jako_vypocet(text):
        return None
    if not _volno():
        _log.info("%s: přeskočeno (hra nebo noční dávka)", MARK)
        return None
    q = re.sub(r"^\S+\s+se\s+pt[áa]:\s*", "", text or "").strip()
    try:   # HANS_CISLA_SLOVY_V1 — „tři miliony“, „čtyřicet pět ze sto osmdesáti“ → číslice
        from scripts.hans_pocty import cisla_slovy
        q = cisla_slovy(q)
    except Exception:
        pass
    if not pamet_pro_qwen3(config, "výpočet"):   # HANS_VYPOCTY_RAM_V1
        return None
    try:
        import requests
        from scripts.hans_book_summary import _cpu_url
        body = {"model": c.get("model", "qwen3:30b"), "stream": False, "format": "json", "think": False,
                "keep_alive": c.get("keep_alive", "30m"),
                "messages": [{"role": "system", "content": (_SYS if not (_fo or _ru) else _SYS.replace(
                                 "(e.g. needs facts about a person)", "(e.g. needs facts that are not given)").replace(
                                 "Use ONLY numbers that appear in the question", "Use ONLY numbers that appear "
                                 "in the question or in the known facts")).format(
                                 dnes=_dt.date.today().isoformat(), rok=_dt.date.today().year)},
                             {"role": "user", "content": (q + "\n\n" + _fo[0] + _SYS_OSOBA) if _fo else (
                                 (q + "\n\nKnown facts from Wikidata about the event: " + "; ".join(
                                     "%s: %s = %d" % (v[0], {"konec": "end", "začátek": "start", "datum": "date",
                                                             "vznik": "inception", "zánik": "dissolution",
                                                             "otevření": "opening", "vydání": "publication"}[v[1]], k)
                                     for k, v in sorted(_ru.items())) + ". Use the year that fits the question.")
                                 if _ru else q)}],
                "options": {"temperature": 0, "num_ctx": 2048, "num_predict": 300}}
        t0 = time.time()
        # HANS_VYPOCTY_NAHRATI_V1: v provozu se na PC přetahuje o CPU (25 s nestačilo, sólo 5–10 s)
        r = requests.post(_cpu_url(config) + "/api/chat", json=body, timeout=max(timeout, float(c.get("timeout", 45))))
        d = json.loads(r.json().get("message", {}).get("content", "") or "{}")
    except Exception as e:
        _log.info("%s: model nedostupný (%s)", MARK, str(e)[:80])
        if "timed out" in str(e):
            nahrej_na_pozadi(config, "timeout výpočtu")   # HANS_VYPOCTY_NAHRATI_V1
        return None
    vyraz = str(d.get("vyraz") or "").strip()
    if not vyraz:
        _log.info("%s: model nevrátil výraz (nejde spočítat z otázky)", MARK)
        return None
    # HANS_VYPOCTY_PROCENTO_Z_V1 — „X % z Y“ je X procent Z Y, ne kolik procent je X z Y
    # (1. 11.: „15 procent z 2400“ → procento(15, 2400) = 0,625; nápověda v zadání nepomohla)
    _pz = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:%|procent\w*)\s+z\s+(\d+(?:[.,]\d+)?)", _f(q))
    if _pz and not re.search(r"\bkolik\s+procent|\bv\s+procentech", _f(q)):
        _a, _b = (x.replace(",", ".") for x in _pz.groups())
        _v2 = re.sub(r"procento\(\s*%s\s*,\s*%s\s*\)" % (re.escape(_a), re.escape(_b)),
                     "%s * %s / 100" % (_b, _a), vyraz)
        if _v2 != vyraz:
            _log.info("%s: X %% z Y → %s", MARK, _v2[:80])
            vyraz = _v2
    # HANS_VYPOCTY_JEN_ROK_V1 (2. 11., /tazatel B) — u osoby s jen rokem narození/úmrtí
    # ve Wikidatech (Vermeer: jen 1632) model psal datum(1632, 1, 1) i přes pokyn
    # „use the year number“ a kontrola výpočet zahodila (Vermeer, Rembrandt × Vermeer).
    # Takové datum se převede na holý rok — věk pak vyjde na rok přesně.
    if _fo:
        _yo = {int(x) for x in _fo[2] if x > 31 and x == int(x)
               and not any(tr[0] == int(x) for tr in _fo[4])}
        if _yo:
            _v2 = re.sub(r"datum\(\s*(\d{3,4})\s*,\s*\d+\s*,\s*\d+\s*\)",
                         lambda m: m.group(1) if int(m.group(1)) in _yo else m.group(0), vyraz)
            if _v2 != vyraz:
                _log.info("HANS_VYPOCTY_JEN_ROK_V1: %s → %s", vyraz[:80], _v2[:80])
                vyraz = _v2
    bez = _cisla_bez_opory(vyraz, q, _fo[2] if _fo else ())
    if _fo and _fo[3] and "den_v_tydnu" in vyraz:
        _log.info("%s: den v týdnu z juliánského data → zahazuji: %.120r", MARK, vyraz)
        return None
    if not bez and _fo:
        bez = _data_bez_opory(vyraz, q, _fo)
    _ud_popis = ""
    if bez and not _fo and all(re.fullmatch(r"\d{3,4}", b_) for b_ in bez):   # HANS_VYPOCTY_UDALOST_ROK_V1
        _ru = _ru or roky_udalosti(q, f1)
        if all(int(b_) in _ru for b_ in bez):
            _ud_popis = "údaje z Wikidat: " + "; ".join(
                "%s – %s %s" % (_ru[int(b_)][0], _ru[int(b_)][1], b_) for b_ in dict.fromkeys(bez))
            _log.info("HANS_VYPOCTY_UDALOST_ROK_V1: rok %s doložen (%s)", bez, _ud_popis)
            bez = []
        else:
            _log.info("HANS_VYPOCTY_UDALOST_ROK_V1: rok %s nedoložen (Wikidata %s)", bez, sorted(_ru)[:8])
    if bez:
        _log.info("%s: čísla bez opory v otázce %s → zahazuji: %.120r", MARK, bez, vyraz)
        return None
    if not _fo:   # HANS_VYPOCTY_PRISTI_DATUM_V1
        vyraz = pristi_datum(vyraz, q)
    if re.fullmatch(r"[\d\s.,]+", vyraz):   # HANS_VYPOCTY_WD_V2 — holé číslo není výpočet
        _log.info("%s: výraz je jen číslo → zahazuji: %.60r", MARK, vyraz)
        return None
    try:
        x = vyhodnot(vyraz)
    except Exception as e:
        _log.info("%s: výraz nelze spočítat (%s): %.120r", MARK, str(e)[:60], vyraz)
        return None
    jed = str(d.get("jednotka") or "").strip()
    jed = {"years": "let", "year": "let", "days": "dní", "day": "dní", "weeks": "týdnů",   # HANS_VYPOCTY_WD_V2b
           "months": "měsíců", "procenta": "%", "procent": "%", "percent": "%"}.get(jed.lower(), jed)
    if jed.lower() in ("dny", "dní", "dnů", "dni", "days", "day", "den"):   # HANS_VYPOCTY_VETOU_V1 — „17 881 dny“
        try:
            _xi = int(round(float(x)))
            jed = "den" if _xi == 1 else ("dny" if 2 <= _xi <= 4 else "dní")
        except Exception:
            pass
    co = str(d.get("co") or "").strip()
    _log.info("%s: veličina %s", MARK, co[:60])
    out = "%svýpočet: %s → výsledek = %s%s" % ((_fo[1] + " · ") if _fo else ((_ud_popis + " · ") if _ud_popis else ""), vyraz.replace("**", "^").replace("*", "·"),
                                             _cz(x), (" " + jed) if jed and not isinstance(x, (_dt.date, str)) else "")
    _log.info("%s: %.1f s · %s", MARK, time.time() - t0, out[:200])
    return out


# ── HANS_VYPOCTY_CISLO_Z_PROGRAMU_V1 (1. 11.) ────────────────────────────────
# Model občas vysloví jiné číslo, než spočítal program („Beethovenovi bylo 57 let“,
# program 56; „o 19 let starší“, program 14) a pojistka pak připojila „Výsledek: 56“
# → odpověď si protiřečila. Když výsledek v odpovědi chybí a je v ní PRÁVĚ JEDNO jiné
# číslo se stejnou jednotkou, nahradí se výsledkem. Víc kandidátů = nejisté → beze změny.
_JEDN_RODINY = [
    ("let", r"(?:let|roky|roků|rok|roku)"), ("dní", r"(?:dní|dny|dnů|den|dne)"),
    ("Kč", r"(?:Kč|korun\w*)"), ("%", r"(?:%|procent\w*)"), ("týdnů", r"(?:týdn\w*|týden)"),
    ("měsíců", r"(?:měsíc\w*)"), ("m/s", r"m/s"), ("km/h", r"km/h"), ("J", r"(?:J|joul\w*)"),
    ("kJ", r"kJ"), ("W", r"(?:W|watt\w*)"), ("kg", r"kg"), ("m", r"(?:m|metr\w*)"), ("s", r"(?:s|sekund\w*)"),
]
_DNY_TYDNE = ("pondělí", "úterý", "středa", "středu", "čtvrtek", "pátek", "sobota", "sobotu", "neděle", "neděli")


# ── HANS_VYPOCTY_DATA_Z_WD_V1 (4. 11.) ──────────────────────────────────────
# „A kolik by dnes bylo Antonímu Gaudímu?“ (3. 11.): výpočet měl z Wikidat „datum
# úmrtí 10. 6. 1926“, model přesto napsal „– 10. listopadu 1926“ (opsal tvar
# předchozí odpovědi s úvodem článku). Datum osoby v odpovědi se stejným ROKEM jako
# datum z Wikidat, ale jiným dnem/měsícem → přepsat na datum z Wikidat (týž tvar).
_MESICE_2 = ("ledna", "února", "března", "dubna", "května", "června", "července",
             "srpna", "září", "října", "listopadu", "prosince")


def srovnej_data(odpoved: str, vypocet: str):
    """(odpověď, změněno) — data osoby v odpovědi podle údajů z Wikidat ve výpočtu."""
    if not odpoved or not vypocet or "údaje z Wikidat" not in vypocet:
        return odpoved, False
    wd = {}
    for d, m, r in re.findall(r"datum (?:narození|úmrtí) (\d{1,2})\. (\d{1,2})\. (-?\d{1,4})", vypocet):
        wd.setdefault(int(r), set()).add((int(d), int(m)))
    jiste = {r: next(iter(s)) for r, s in wd.items() if len(s) == 1}
    if not jiste:
        return odpoved, False
    zmena = False

    def _slovy(mt):
        nonlocal zmena
        d, mes, r = int(mt.group(1)), mt.group(2).lower(), int(mt.group(3))
        if r not in jiste or mes not in _MESICE_2:
            return mt.group(0)
        sd, sm = jiste[r]
        if (d, _MESICE_2.index(mes) + 1) == (sd, sm):
            return mt.group(0)
        zmena = True
        return "%d. %s %d" % (sd, _MESICE_2[sm - 1], r)

    def _cisly(mt):
        nonlocal zmena
        d, m, r = int(mt.group(1)), int(mt.group(2)), int(mt.group(3))
        if r not in jiste or (d, m) == jiste[r] or not (1 <= m <= 12):
            return mt.group(0)
        zmena = True
        return "%d. %d. %d" % (jiste[r][0], jiste[r][1], r)
    def _radove(mt):   # „desátého července roku 1926“
        nonlocal zmena
        d = _RADOVE.get(mt.group(1).lower().replace(" ", ""))
        mes, r = mt.group(2).lower(), int(mt.group(3))
        if d is None or r not in jiste or mes not in _MESICE_2:
            return mt.group(0)
        if (d, _MESICE_2.index(mes) + 1) == jiste[r]:
            return mt.group(0)
        zmena = True
        sd, sm = jiste[r]
        return "%d. %s %d" % (sd, _MESICE_2[sm - 1], r)
    out = re.sub(r"\b(\d{1,2})\.\s*(%s)\s+(\d{3,4})\b" % "|".join(_MESICE_2), _slovy, odpoved,
                 flags=re.I)
    out = re.sub(r"\b(\d{1,2})\.\s*(\d{1,2})\.\s*(\d{3,4})\b", _cisly, out)
    out = re.sub(r"\b((?:[a-zá-ž]+(?:ého|ýho))(?:\s+(?:prvního|druhého|třetího|čtvrtého|pátého|šestého|"
                 r"sedmého|osmého|devátého))?)\s+(%s)\s+(?:roku\s+)?(\d{3,4})\b" % "|".join(_MESICE_2),
                 _radove, out, flags=re.I)
    return out, zmena


_RAD_JEDN = ("prvního", "druhého", "třetího", "čtvrtého", "pátého", "šestého", "sedmého",
             "osmého", "devátého")
_RADOVE = {w: i + 1 for i, w in enumerate(_RAD_JEDN)}
_RADOVE.update({w: i + 10 for i, w in enumerate((
    "desátého", "jedenáctého", "dvanáctého", "třináctého", "čtrnáctého", "patnáctého",
    "šestnáctého", "sedmnáctého", "osmnáctého", "devatenáctého"))})
for _des, _z in (("dvacátého", 20), ("třicátého", 30)):
    _RADOVE[_des] = _z
    for _i, _w in enumerate(_RAD_JEDN):
        _RADOVE[_des + _w] = _z + _i + 1                       # „dvacátého prvního“ (bez mezery)
for _i, _p in enumerate(("jedna", "dva", "tři", "čtyři", "pět", "šest", "sedm", "osm", "devět")):
    _RADOVE[_p + "advacátého"] = 21 + _i                       # „pětadvacátého“
_RADOVE["jednatřicátého"] = 31


# HANS_VYPOCTY_LETOS_V1 (4. 11.) — „kdyby žila dosud, v tomto roce, tedy v roce 2024,
# by jí bylo 158 let“: model si letopočet „teď“ vzal z tréninku. U výpočtu od dneška
# rok u slov „dnes/letos/v tomto roce/dosud“ (do 40 zn) = letošní rok.
# HANS_VYPOCTY_VYSLEDEK_JE_V1 (4. 11., /tazatel) — „Výsledek je 66 let.“ a vzápětí „60 let“
# (Smetana): tvar „Výsledek je X“ z předchozí odpovědi model opsal i s vlastním číslem.
def srovnej_vysledek_je(odpoved: str, vypocet: str):
    if not odpoved or "→ výsledek =" not in (vypocet or ""):
        return odpoved, False
    seg = vypocet.split("→ výsledek =")[-1].strip()
    m = re.match(r"(-?\d+(?:[  ]\d{3})*(?:,\d+)?)", seg)
    if not m:
        return odpoved, False
    spravne = m.group(1).strip()
    zmena = False

    def _r(mt):
        nonlocal zmena
        if re.sub(r"[\s  ]", "", mt.group(2)) == re.sub(r"[\s  ]", "", spravne):
            return mt.group(0)
        zmena = True
        return mt.group(1) + spravne
    out = re.sub(r"(\b[Vv]ýsledek\s+je\s+(?:tedy\s+)?(?:přibližně\s+|zhruba\s+)?)(-?\d+(?:[  ]\d{3})*(?:,\d+)?)", _r, odpoved)
    return out, zmena


def srovnej_letos(odpoved: str, vypocet: str):
    if not odpoved or "dnes()" not in (vypocet or ""):
        return odpoved, False
    rok = _dt.date.today().year
    zmena = False

    def _r(mt):
        nonlocal zmena
        y = int(mt.group(2))
        if rok - 5 <= y < rok:
            zmena = True
            return mt.group(1) + str(rok)
        return mt.group(0)
    out = re.sub(r"(\b(?:dnes\w*|letos\w*|v\s+tomto\s+roce|tento\s+rok|leto[sš]n\w+\s+rok\w*|dosud|nyní|"
                 r"v\s+současnosti)[^.!?\n\d]{0,40}?)(20\d\d)\b", _r, odpoved, flags=re.I)
    return out, zmena


def _num(s: str):
    try:
        return float(re.sub(r"[\s ]", "", s).replace(",", "."))
    except Exception:
        return None


def srovnej_cislo(odpoved: str, vypocet: str):
    """(odpověď, změněno) — jedno jiné číslo téže jednotky → výsledek z programu.
    Číslovky slovy („padesát sedm let“) se zkusí po převodu na číslice."""
    r, ch = _srovnej_cislo(odpoved, vypocet)
    if ch:
        return r, ch
    try:
        from scripts.hans_pocty import cisla_slovy
        o2 = _desetinna(cisla_slovy(odpoved))
        if o2 != odpoved:
            r2, ch2 = _srovnej_cislo(o2, vypocet)
            if ch2:
                return r2, True
        r3, ch3 = _srovnej_bez_jednotky(o2, vypocet)
        if ch3:
            return r3, True
    except Exception:
        pass
    return odpoved, False


def _desetinna(s: str) -> str:
    """„21 bod 4 5“, „24 celých 5 desetin“, „24 koma 5“ → „21,45“, „24,5“ (po cisla_slovy)."""
    s = re.sub(r"\b(\d+)\s+(?:celých|celé|celá|bod|koma|čárka)\s+(?:a\s+)?(\d(?:\s\d)*|\d+)(?:\s+(?:desetin\w*|setin\w*|tisícin\w*))?\b",
               lambda m: m.group(1) + "," + re.sub(r"\s", "", m.group(2)), s)
    return re.sub(r"\b(\d+)\s+a\s+půl\b", r"\1,5", s)


def _srovnej_bez_jednotky(odpoved: str, vypocet: str):
    """HANS_VYPOCTY_CISLO_Z_PROGRAMU_V1 — výsledek bez jednotky („7 % z 350 = 24,5“):
    čísla ze zadání se vynechají; zbude-li v odpovědi právě jedno jiné číslo → výsledek."""
    if "→" in (vypocet or ""):
        lev, prav = vypocet.rsplit("→", 1)
    elif "=" in (vypocet or ""):
        lev, prav = vypocet.rsplit("=", 1)
    else:
        return odpoved, False
    prav = re.sub(r"^\s*(?:výsledek\s*=\s*)?", "", prav).strip().rstrip(".")
    if not re.fullmatch(r"-?\d[\d\s\u00a0]*(?:,\d+)?", prav):
        return odpoved, False
    hod = _num(prav)
    zadani = {_num(x) for x in re.findall(r"\d+(?:[.,]\d+)?", lev)}
    rx = re.compile(r"(?<![\d,.])(\d{1,3}(?:[ \u00a0]\d{3})+|\d+)(?:,(\d+))?(?![\d])")
    kand = [(mm, _num(mm.group(1) + ("," + mm.group(2) if mm.group(2) else ""))) for mm in rx.finditer(odpoved)]
    if any(v is not None and abs(v - hod) < 1e-6 for _, v in kand):
        return odpoved, False
    jine = {v for _, v in kand if v is not None and v not in zadani}
    if len(jine) != 1:
        return odpoved, False
    spatne = jine.pop()
    out, kon = [], 0
    for mm, v in kand:
        if v == spatne:
            out.append(odpoved[kon:mm.start(1)])
            out.append(prav)
            kon = mm.end(2) if mm.group(2) else mm.end(1)
    out.append(odpoved[kon:])
    return "".join(out), True


def _srovnej_cislo(odpoved: str, vypocet: str):
    try:
        seg = (vypocet or "").split("→")[-1]
        seg = re.sub(r"^\s*(?:výsledek\s*=\s*|[a-zA-Z_]\w*\s*=\s*)", "", seg.strip())
        seg = re.split(r"\s\(předpoklad", seg)[0].strip().rstrip(".")
        seg = re.sub(r"\s*\([^()]*\)\s*$", "", seg).strip()        # „15 000 J (15 kJ)“
        if seg.lower() in _DNY_TYDNE:
            jine = {d for d in _DNY_TYDNE if re.search(r"\b%s\b" % d, odpoved, re.I)}
            if seg.lower() not in {d.lower() for d in jine} and len(jine) == 1:
                d = jine.pop()
                return re.sub(r"\b%s\b" % d, seg, odpoved, flags=re.I), True
            return odpoved, False
        # HANS_VYPOCTY_CISLO_SCI_V1 (4. 11., /tazatel B) — fyzika vrací „1,046·10^7 J“ a model
        # napsal „1 046 500 J“ (o řád méně) → rozpor v jedné odpovědi; vědecký zápis porovnat
        # s tolerancí zaokrouhlení (4 platné číslice) a špatné číslo nahradit tímto zápisem
        msci = re.match(r"(-?\d+(?:,\d+)?)·10\^(-?\d+)\s*(.*)$", seg)
        tol = 1e-6
        cil_sci = None
        if msci and _num(msci.group(1)) is not None:
            hod = _num(msci.group(1)) * 10 ** int(msci.group(2))
            jed = msci.group(3).strip()
            cil_sci = "%s·10^%s" % (msci.group(1), msci.group(2))
            tol = abs(hod) * 0.005
            m = None
        else:
            m = re.match(r"(-?\d[\d\s ]*(?:,\d+)?)\s*(.*)$", seg)
            if not m:
                return odpoved, False
            hod, jed = _num(m.group(1)), m.group(2).strip()
        rod = next((rx for k, rx in _JEDN_RODINY if re.fullmatch(rx, jed)), None)
        if hod is None or not rod:
            return odpoved, False
        rx = re.compile(r"(?<![\d,.])(\d{1,3}(?:[  ]\d{3})+|\d+)(?:,(\d+))?\s*(" + rod + r")(?![\w/])")
        kand = [(mm, _num(mm.group(1) + ("," + mm.group(2) if mm.group(2) else ""))) for mm in rx.finditer(odpoved)]
        if any(v is not None and abs(v - hod) <= max(tol, 1e-6) for _, v in kand):
            return odpoved, False                 # výsledek v odpovědi je
        jine = {v for _, v in kand if v is not None}
        if len(jine) != 1:
            return odpoved, False
        spatne = jine.pop()
        cil = cil_sci or m.group(1).strip()
        out, kon = [], 0
        for mm, v in kand:
            if v == spatne:
                out.append(odpoved[kon:mm.start(1)])
                out.append(cil)
                kon = mm.end(2) if mm.group(2) else mm.end(1)
        out.append(odpoved[kon:])
        return "".join(out), True
    except Exception:
        return odpoved, False
