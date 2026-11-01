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
    z |= {dnes.year, dnes.month, dnes.day}
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
    r"(?:jaky|ktery|v\s+jaky)\s+den|den\s+v\s+tydnu|kolik\s+dni)\b")
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
    for z in [kus] + (wo._prvni_pad_vic(kus) if " " in kus else wo._tvary_slova(kus)):
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
    return None


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
            return []
        w = re.findall(r"[^\W\d_][\w\-]*\.?", q or "")
        out, i = [], 0
        while i < len(w) and len(out) < strop:
            hit = None
            if w[i][:1].isupper():
                for n in (3, 2, 1):
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
                if i > 0 and w[i][:1].isupper() and len(w[i].rstrip(".")) >= 3:
                    zbytek.append(w[i])
                i += 1
        zbytek += [x for x in w[i:] if x[:1].isupper() and len(x.rstrip(".")) >= 3]
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


def _wd_fmt(d) -> str:
    s = ("datum(%d, %d, %d)" % (d[0], d[1], d[2]) if d[2] else
         "%d (only the year is known — use the year number)" % d[0])
    return s + (" [Julian calendar]" if d[3] else "")


def _wd_cz(d) -> str:
    return ("%d. %d. %d" % (d[2], d[1], d[0]) if d[2] else "roku %d" % d[0])


def fakta_osob(q: str):
    """(věta pro model, věta pro Hanse, povolená čísla, juliánský) nebo None."""
    if not CUE_OSOBA.search(_f(q)):
        return None
    rad, cz, cis, jul, trojice = [], [], set(), False, set()
    if re.search(r"\b(vladl\w*|kraloval\w*|panoval\w*|uradoval\w*|prezident\w*|ve\s+funkci|na\s+trune)", _f(q)):
        _log.info("HANS_VYPOCTY_WD_V2b: otázka na úřad/vládu → bez faktů")   # „kolik let vládl Karel IV.“ ≠ délka života
        return None
    _os, _zb = osoby_a_zbytek(q)
    if _zb:
        _log.info("HANS_VYPOCTY_WD_V2: jména bez dat %s → bez faktů", _zb[:4])
        return None
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
    return ("Known facts from Wikidata: " + "; ".join(rad) + ".",
            "údaje z Wikidat: " + "; ".join(cz), cis, jul, trojice)


_SYS_OSOBA = (   # HANS_VYPOCTY_WD_V2 — jen když jsou data osoby
    " Write a date of the person ALWAYS as the full datum(...) given above, never as a bare "
    "year. Age at death or how long someone lived = vek(birth, death); age today = vek(birth, "
    "dnes()); years since death = vek(death, dnes()); years since birth = vek(birth, dnes()); age "
    "in year Y = Y - birth_year; lifespan in days = dny(birth, death); weekday of birth = "
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


def _volno() -> bool:
    try:
        from scripts.ollama_client import game_mode_on, base_slot_busy
        return not game_mode_on() and not base_slot_busy()
    except Exception:
        return True


def vypocet_modelem(config: dict, text: str, timeout: float = 25.0) -> Optional[str]:
    c = (config or {}).get("vypocty_model", {}) or {}
    if not c.get("enabled", True):
        return None
    q0 = re.sub(r"^\S+\s+se\s+pt[áa]:\s*", "", text or "").strip()
    _fo = fakta_osob(q0) if c.get("wikidata", True) else None   # HANS_VYPOCTY_WD_V1
    if not _fo and not vypada_jako_vypocet(text):
        return None
    if not _volno():
        _log.info("%s: přeskočeno (hra nebo noční dávka)", MARK)
        return None
    q = re.sub(r"^\S+\s+se\s+pt[áa]:\s*", "", text or "").strip()
    try:
        import requests
        from scripts.hans_book_summary import _cpu_url
        body = {"model": c.get("model", "qwen3:30b"), "stream": False, "format": "json", "think": False,
                "keep_alive": c.get("keep_alive", "10m"),
                "messages": [{"role": "system", "content": (_SYS if not _fo else _SYS.replace(
                                 "(e.g. needs facts about a person)", "(e.g. needs facts that are not given)").replace(
                                 "Use ONLY numbers that appear in the question", "Use ONLY numbers that appear "
                                 "in the question or in the known facts")).format(
                                 dnes=_dt.date.today().isoformat(), rok=_dt.date.today().year)},
                             {"role": "user", "content": q if not _fo else (
                                 q + "\n\n" + _fo[0] + _SYS_OSOBA)}],
                "options": {"temperature": 0, "num_ctx": 2048, "num_predict": 300}}
        t0 = time.time()
        r = requests.post(_cpu_url(config) + "/api/chat", json=body, timeout=timeout)
        d = json.loads(r.json().get("message", {}).get("content", "") or "{}")
    except Exception as e:
        _log.info("%s: model nedostupný (%s)", MARK, str(e)[:80])
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
    bez = _cisla_bez_opory(vyraz, q, _fo[2] if _fo else ())
    if _fo and _fo[3] and "den_v_tydnu" in vyraz:
        _log.info("%s: den v týdnu z juliánského data → zahazuji: %.120r", MARK, vyraz)
        return None
    if not bez and _fo:
        bez = _data_bez_opory(vyraz, q, _fo)
    if bez:
        _log.info("%s: čísla bez opory v otázce %s → zahazuji: %.120r", MARK, bez, vyraz)
        return None
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
    co = str(d.get("co") or "").strip()
    _log.info("%s: veličina %s", MARK, co[:60])
    out = "%svýpočet: %s → výsledek = %s%s" % ((_fo[1] + " · ") if _fo else "", vyraz.replace("**", "^").replace("*", "·"),
                                             _cz(x), (" " + jed) if jed and not isinstance(x, (_dt.date, str)) else "")
    _log.info("%s: %.1f s · %s", MARK, time.time() - t0, out[:200])
    return out
