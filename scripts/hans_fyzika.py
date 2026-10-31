"""HANS_FYZIKA_V1 (31. 10.) — fyzikální výpočty s jednotkami spočítá program, ne model.

Měření 31. 10. (`data/mereni/pocty_fyzika_31_10/`): vzorce model zná, ale dosazuje
a počítá z hlavy — kámen z 20 m „6,32 m/s“ (správně 19,8), auto 1000 kg / 50 km/h
„98 750 J“ (správně 96 450). Tady se z otázky vytáhnou veličiny s jednotkami,
podle slov se pozná hledaná veličina, najde se vzorec (nejvýš dva kroky) a výsledek
i s postupem jde do podkladu; model ho jen vysvětlí.

Umí také převod jednotek („kolik je 100 km/h v m/s“) a ke známým zákonům
(zachování energie, Newton, Ohm, Archimédes…) vzorový příklad s čísly, která
spočítal program. Slovní úlohy s volbou postupu (šikmý vrh, tření, složité
obvody) ne — tam se nevloží nic a Hans odpovídá jako dřív.
Bez `eval`: jen pevné vzorce v Pythonu.
"""
from __future__ import annotations

import math
import re
import unicodedata
from typing import Optional

G = 9.81


def _f(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", (s or "").lower())
                   if unicodedata.category(c) != "Mn")


def _fmt(x: float) -> str:
    """Česky, rozumně zaokrouhleno (4 platné číslice, celá čísla bez desetin)."""
    if x == 0:
        return "0"
    a = abs(x)
    if a >= 1000 and abs(x - round(x)) < 1e-6:
        s = "%d" % round(x)
    elif a >= 100:
        s = ("%.1f" % x)
    elif a >= 10:
        s = ("%.1f" % x)
    elif a >= 1:
        s = ("%.2f" % x)
    else:
        s = ("%.4g" % x)
    if "." in s and "e" not in s:
        s = s.rstrip("0").rstrip(".")
    s = s.replace(".", ",")
    # mezery v tisících
    m = re.match(r"^(-?)(\d+)(,\d+)?$", s)
    if m and len(m.group(2)) > 4:
        cel = m.group(2)
        cel = " ".join([cel[max(0, i - 3):i] for i in range(len(cel), 0, -3)][::-1])
        s = m.group(1) + cel + (m.group(3) or "")
    return s


# ── jednotky: (veličina, násobek do SI) ──────────────────────────────────────
# klíče jsou SLOŽENÉ tvary bez diakritiky; delší dřív
_JEDNOTKY = [
    (r"km\s*/\s*h|km/hod|kilometr\w*\s+za\s+hodinu|kilometr\w*\s+v\s+hodine", ("rychlost", 1 / 3.6)),
    (r"m\s*/\s*s\s*(?:2|\^2|²)|metr\w*\s+za\s+sekundu\s+na\s+druhou", ("zrychleni", 1.0)),
    (r"m\s*/\s*s|metr\w*\s+za\s+sekundu|metr\w*\s+za\s+vterinu", ("rychlost", 1.0)),
    (r"kwh|kilowatthodin\w*", ("energie", 3.6e6)),
    (r"wh|watthodin\w*", ("energie", 3600.0)),
    (r"kj|kilojoul\w*", ("energie", 1000.0)),
    (r"mj|megajoul\w*", ("energie", 1e6)),
    (r"j|joul\w*", ("energie", 1.0)),
    (r"kw|kilowatt\w*", ("vykon", 1000.0)),
    (r"mw|megawatt\w*", ("vykon", 1e6)),
    (r"w|watt\w*", ("vykon", 1.0)),
    (r"kn|kilonewton\w*", ("sila", 1000.0)),
    (r"n|newton\w*", ("sila", 1.0)),
    (r"kv|kilovolt\w*", ("napeti", 1000.0)),
    (r"v|volt\w*", ("napeti", 1.0)),
    (r"ma|miliamper\w*", ("proud", 0.001)),
    (r"a|amper\w*", ("proud", 1.0)),
    (r"kohm\w*|kiloohm\w*|kω", ("odpor", 1000.0)),
    (r"ohm\w*|ω", ("odpor", 1.0)),
    (r"km|kilometr\w*", ("delka", 1000.0)),
    (r"cm|centimetr\w*", ("delka", 0.01)),
    (r"mm|milimetr\w*", ("delka", 0.001)),
    (r"m|metr\w*", ("delka", 1.0)),
    (r"kg|kil\w*|kilogram\w*", ("hmotnost", 1.0)),
    (r"t|tun\w*", ("hmotnost", 1000.0)),
    (r"g|gram\w*", ("hmotnost", 0.001)),
    (r"ms|milisekund\w*", ("cas", 0.001)),
    (r"s|sekund\w*|vterin\w*|sec", ("cas", 1.0)),
    (r"min|minut\w*", ("cas", 60.0)),
    (r"h|hod|hodin\w*", ("cas", 3600.0)),
]
_JED_RE = [(re.compile(r"^(?:%s)(?![a-z0-9])" % p), v) for p, v in _JEDNOTKY]
_SI = {"delka": "m", "hmotnost": "kg", "cas": "s", "rychlost": "m/s", "zrychleni": "m/s²",
       "sila": "N", "energie": "J", "vykon": "W", "napeti": "V", "proud": "A", "odpor": "Ω"}
# výstupní jednotky pro převod („v metrech za sekundu“)
_CIL = [
    (r"km\s*/\s*h|kilometr\w*\s+za\s+hodinu", ("rychlost", 1 / 3.6, "km/h")),
    (r"m\s*/\s*s|metr\w*\s+za\s+sekundu", ("rychlost", 1.0, "m/s")),
    (r"kwh|kilowatthodin\w*", ("energie", 3.6e6, "kWh")),
    (r"kj|kilojoul\w*", ("energie", 1000.0, "kJ")),
    (r"joul\w*|j\b", ("energie", 1.0, "J")),
    (r"kw\b|kilowatt\w*", ("vykon", 1000.0, "kW")),
    (r"watt\w*|w\b", ("vykon", 1.0, "W")),
    (r"km\b|kilometr\w*", ("delka", 1000.0, "km")),
    (r"cm\b|centimetr\w*", ("delka", 0.01, "cm")),
    (r"metr\w*|m\b", ("delka", 1.0, "m")),
    (r"sekund\w*|s\b", ("cas", 1.0, "s")),
    (r"minut\w*", ("cas", 60.0, "min")),
    (r"hodin\w*", ("cas", 3600.0, "h")),
    (r"gram\w*|g\b", ("hmotnost", 0.001, "g")),
    (r"tun\w*", ("hmotnost", 1000.0, "t")),
    (r"kilogram\w*|kg\b", ("hmotnost", 1.0, "kg")),
]

_CISLO = r"(\d+(?:[.,]\d+)?)"


def _hodnoty(t: str) -> list:
    """[(veličina, hodnota_SI, původní text)] z čísel s jednotkou (t složené)."""
    out = []
    for m in re.finditer(_CISLO + r"\s*", t):
        rest = t[m.end():]
        for rx, (vel, k) in _JED_RE:
            mm = rx.match(rest)
            if mm:
                x = float(m.group(1).replace(",", "."))
                out.append((vel, x * k, (m.group(0) + mm.group(0)).strip()))
                break
    return out


# ── hledaná veličina ─────────────────────────────────────────────────────────
_HLEDANA = [
    (r"kinetick\w*\s+energi|pohybov\w*\s+energi", "Ek"),
    (r"potencialn\w*\s+energi|polohov\w*\s+energi", "Ep"),
    (r"\bvykon", "P"),
    (r"\bproud\b|\bproud\w*\s+tece|jaky\s+proud", "I"),
    (r"\bnapet", "U"),
    (r"(?<!vzdusn)(?<!vzduchu)\bodpor\b(?!\s+vzduch)", "R"),
    (r"\bzrychlen", "a"),
    (r"jak\w*\s+rychl|rychlost", "v"),
    (r"jak\s+dlouho|za\s+jak\s+dlouho|doba\s+padu|dobu|\bcas\b", "t"),
    (r"\bprac\w*|\benergi", "W"),
    (r"\bsil\w*", "F"),
    (r"jak\s+vysok|\bvysk|z\s+jake\s+vysky", "h"),
]
_PAD = re.compile(r"\b(pad\w*|dopad\w*|spadn\w*|spadl\w*|pust\w*|upust\w*|volny\s+pad|hodi\w*\s+dolu)")
_ZVED = re.compile(r"\b(zved\w*|zdvih\w*|vyzved\w*|vytah\w*|vynes\w*)")
_OTAZKA = re.compile(r"\b(kolik|jak\w*|vypocit\w*|spocit\w*|spocti|urci|jaka|jaky|jake|za\s+jak)\b|\?")


def _vstupy(t: str) -> dict:
    """Známé veličiny podle jednotek a kontextu → {symbol: hodnota SI}."""
    k = {}
    pad = bool(_PAD.search(t)); zved = bool(_ZVED.search(t))
    for vel, x, _txt in _hodnoty(t):
        if vel == "delka":
            k.setdefault("h" if (pad or zved or re.search(r"vysk|vysc|vysoko", t)) else "s", x)
        elif vel == "hmotnost":
            k.setdefault("m", x)
        elif vel == "cas":
            k.setdefault("t", x)
        elif vel == "rychlost":
            k.setdefault("v", x)
        elif vel == "zrychleni":
            k.setdefault("a", x)
        elif vel == "sila":
            k.setdefault("F", x)
        elif vel == "energie":
            k.setdefault("W", x)
        elif vel == "vykon":
            k.setdefault("P", x)
        elif vel == "napeti":
            k.setdefault("U", x)
        elif vel == "proud":
            k.setdefault("I", x)
        elif vel == "odpor":
            k.setdefault("R", x)
    return k


# vzorce: (výsledek, potřebné, funkce, zápis, podmínka kontextu)
#   podmínka: None = vždy, "pad" = volný pád z klidu, "zved" = zvedání
_VZORCE = [
    ("Ep", ("m", "h"), lambda d: d["m"] * G * d["h"], "Ep = m·g·h", None),
    ("Ek", ("m", "v"), lambda d: 0.5 * d["m"] * d["v"] ** 2, "Ek = ½·m·v²", None),
    ("v", ("h",), lambda d: math.sqrt(2 * G * d["h"]), "v = √(2·g·h)", "pad"),
    ("t", ("h",), lambda d: math.sqrt(2 * d["h"] / G), "t = √(2·h/g)", "pad"),
    ("v", ("t",), lambda d: G * d["t"], "v = g·t", "pad"),
    ("h", ("t",), lambda d: 0.5 * G * d["t"] ** 2, "h = ½·g·t²", "pad"),
    ("W", ("m", "h"), lambda d: d["m"] * G * d["h"], "W = m·g·h", "zved"),
    ("F", ("m",), lambda d: d["m"] * G, "F = m·g", "zved"),
    ("F", ("m", "a"), lambda d: d["m"] * d["a"], "F = m·a", None),
    ("a", ("F", "m"), lambda d: d["F"] / d["m"], "a = F/m", None),
    ("m", ("F", "a"), lambda d: d["F"] / d["a"], "m = F/a", None),
    ("W", ("F", "s"), lambda d: d["F"] * d["s"], "W = F·s", None),
    ("P", ("W", "t"), lambda d: d["W"] / d["t"], "P = W/t", None),
    ("W", ("P", "t"), lambda d: d["P"] * d["t"], "W = P·t", None),
    ("t", ("W", "P"), lambda d: d["W"] / d["P"], "t = W/P", None),
    ("P", ("U", "I"), lambda d: d["U"] * d["I"], "P = U·I", None),
    ("I", ("P", "U"), lambda d: d["P"] / d["U"], "I = P/U", None),
    ("U", ("P", "I"), lambda d: d["P"] / d["I"], "U = P/I", None),
    ("I", ("U", "R"), lambda d: d["U"] / d["R"], "I = U/R", None),
    ("U", ("R", "I"), lambda d: d["R"] * d["I"], "U = R·I", None),
    ("R", ("U", "I"), lambda d: d["U"] / d["I"], "R = U/I", None),
    ("P", ("F", "v"), lambda d: d["F"] * d["v"], "P = F·v", None),
    ("v", ("s", "t"), lambda d: d["s"] / d["t"], "v = s/t", "rovnomerne"),
    ("t", ("s", "v"), lambda d: d["s"] / d["v"], "t = s/v", "rovnomerne"),
    ("s", ("v", "t"), lambda d: d["v"] * d["t"], "s = v·t", "rovnomerne"),
    ("a", ("v", "t"), lambda d: d["v"] / d["t"], "a = v/t", None),
]
_JED_SYM = {"Ep": "J", "Ek": "J", "W": "J", "v": "m/s", "t": "s", "h": "m", "s": "m",
            "F": "N", "a": "m/s²", "m": "kg", "P": "W", "U": "V", "I": "A", "R": "Ω"}


def _pis(sym: str, x: float) -> str:
    j = _JED_SYM.get(sym, "")
    s = "%s = %s %s" % (sym, _fmt(x), j)
    if sym == "v":
        s += " (%s km/h)" % _fmt(x * 3.6)
    elif sym in ("W", "Ep", "Ek") and x >= 3.6e5:
        s += " (%s kWh)" % _fmt(x / 3.6e6)
    elif sym in ("W", "Ep", "Ek") and x >= 10000:
        s += " (%s kJ)" % _fmt(x / 1000)
    elif sym == "t" and x >= 3600:
        s += " (%s h)" % _fmt(x / 3600)
    elif sym == "t" and x >= 120:
        s += " (%s min)" % _fmt(x / 60)
    elif sym == "P" and x >= 1000:
        s += " (%s kW)" % _fmt(x / 1000)
    return s


def _res(cil: str, zname: dict, ctx: set, hloubka: int = 2):
    """→ (hodnota, [kroky]) nebo None. Kroky: (zápis, vstupy, výsledek)."""
    if cil in zname:
        return None
    for vys, potr, fn, zapis, pod in _VZORCE:
        if vys != cil or (pod and pod not in ctx):
            continue
        if all(p in zname for p in potr):
            try:
                x = fn(zname)
            except (ZeroDivisionError, ValueError):
                continue
            return x, [(zapis, {p: zname[p] for p in potr}, (vys, x))]
    if hloubka <= 1:
        return None
    for vys, potr, fn, zapis, pod in _VZORCE:
        if vys != cil or (pod and pod not in ctx):
            continue
        chybi = [p for p in potr if p not in zname]
        if len(chybi) != 1:
            continue
        r = _res(chybi[0], zname, ctx, hloubka - 1)
        if not r:
            continue
        z2 = dict(zname); z2[chybi[0]] = r[0]
        try:
            x = fn(z2)
        except (ZeroDivisionError, ValueError):
            continue
        return x, r[1] + [(zapis, {p: z2[p] for p in potr}, (vys, x))]
    return None


def _krok_text(zapis: str, vstupy: dict, vysl: tuple) -> str:
    dos = ", ".join("%s = %s %s" % (k, _fmt(v), _JED_SYM.get(k, "")) for k, v in vstupy.items())
    return "%s; dosazeno %s → %s" % (zapis, dos.replace("  ", " "), _pis(*vysl))


def vypocet_fyzika(text: str) -> Optional[str]:
    """Řádek s postupem fyzikálního výpočtu, jinak None."""
    t = _f(re.sub(r"^\S+\s+se\s+pt[áa]:\s*", "", text or ""))
    if not t or not _OTAZKA.search(t):
        return None
    # HANS_FYZIKA_MODEL_V1 — jiné těleso než Země (g = 9,81 by bylo špatně) → nechat modelu
    if re.search(r"\b(mesic\w*|mars\w*|jupiter\w*|venus\w*|saturn\w*|planet\w*|vesmir\w*|slunc\w*)", t):
        return None
    zname = _vstupy(t)
    if not zname:
        return None
    ctx = set()
    if _PAD.search(t) and "h" in zname or (_PAD.search(t) and "t" in zname):
        ctx.add("pad")
    if _ZVED.search(t):
        ctx.add("zved")
    if "s" in zname or ("v" in zname and "t" in zname):
        ctx.add("rovnomerne")
    cile = [sym for rx, sym in _HLEDANA if re.search(rx, t)]
    for cil in cile:
        if cil in zname:
            continue
        r = _res(cil, zname, ctx)
        if r:
            kroky = [_krok_text(*k) for k in r[1]]
            pred = []
            if "pad" in ctx:
                pred.append("volný pád z klidu, bez odporu vzduchu")
            if any("g" in k[0] for k in r[1]):
                pred.append("g = 9,81 m/s²")
            return ("; ".join(kroky) + (" (předpoklad: %s)" % ", ".join(pred) if pred else ""))
    return None


# ── převod jednotek ──────────────────────────────────────────────────────────
_PREVOD_RE = re.compile(r"kolik\s+(?:je\s+)?" + _CISLO + r"\s*(.+?)\s+(?:v|ve|na)\s+(.+?)\s*\??$")
_PREVOD2_RE = re.compile(r"kolik\s+(.+?)\s+(?:je|ma|odpovida)\s+" + _CISLO + r"\s*(.+?)\s*\??$")
_PREVOD3_RE = re.compile(r"(?:preved|prepocit\w*|prepoc\w*)\s+" + _CISLO + r"\s*(.+?)\s+(?:na|do)\s+(.+?)\s*\??$")


def _jed_z(s: str):
    s = s.strip()
    for rx, (vel, k) in _JED_RE:
        if rx.match(s):
            return vel, k
    return None


def _cil_z(s: str):
    s = s.strip()
    for p, v in _CIL:
        if re.match(r"(?:%s)" % p, s):
            return v
    return None


def prevod(text: str) -> Optional[str]:
    t = _f(re.sub(r"^\S+\s+se\s+pt[áa]:\s*", "", text or "")).strip()
    for rx, poradi in ((_PREVOD_RE, "xzc"), (_PREVOD3_RE, "xzc"), (_PREVOD2_RE, "cxz")):
        m = rx.search(t)
        if not m:
            continue
        g = dict(zip(poradi, m.groups()))
        try:
            x = float(g["x"].replace(",", "."))
        except ValueError:
            continue
        z = _jed_z(g["z"]); c = _cil_z(g["c"])
        if not z or not c or z[0] != c[0]:
            continue
        y = x * z[1] / c[1]
        return "%s = %s %s" % (re.sub(r"\s+", " ", (g["x"] + " " + g["z"].split()[0]
                                                     if " za " not in g["z"] else g["x"] + " " + g["z"])),
                                 _fmt(y), c[2])
    return None


# ── zákony: vzorový příklad s čísly ──────────────────────────────────────────
def _priklad_energie():
    m, h = 0.5, 10.0
    ep = m * G * h; v = math.sqrt(2 * G * h); ek = 0.5 * m * v * v
    return ("míč o hmotnosti 0,5 kg ve výšce 10 m má polohovou energii Ep = m·g·h = 0,5·9,81·10 = %s J; "
            "těsně před dopadem má rychlost v = √(2·g·h) = %s m/s a pohybovou energii Ek = ½·m·v² = %s J "
            "— součet energií je stejný (bez odporu vzduchu); při dopadu se mění na teplo, zvuk a deformaci"
            % (_fmt(ep), _fmt(v), _fmt(ek)))


def _priklad_hybnost():
    m1, v1, m2 = 2.0, 3.0, 1.0
    v = m1 * v1 / (m1 + m2)
    return ("vozík 2 kg jedoucí 3 m/s narazí do stojícího vozíku 1 kg a spojí se s ním: hybnost před "
            "srážkou p = 2·3 = 6 kg·m/s, po srážce jedou spolu rychlostí v = 6/(2+1) = %s m/s (hybnost "
            "zůstává 6 kg·m/s)" % _fmt(v))


def _priklad_newton2():
    return "na vozík o hmotnosti 20 kg působí síla 50 N → zrychlení a = F/m = 50/20 = 2,5 m/s²"


def _priklad_newton1():
    return ("kniha na stole zůstane ležet, dokud do ní nikdo nestrčí; míček na hladkém ledu by bez tření "
            "jel stále stejnou rychlostí")


def _priklad_newton3():
    return ("když stojíte na bruslích a odstrčíte se od zdi silou 100 N, zeď působí na vás stejnou silou 100 N "
            "opačným směrem — proto se rozjedete od zdi")


def _priklad_ohm():
    return ("baterie 9 V připojená k odporu 450 Ω → proud I = U/R = 9/450 = 0,02 A (20 mA); "
            "při dvojnásobném odporu 900 Ω by tekl poloviční proud 0,01 A")


def _priklad_archimedes():
    V = 0.002
    F = 1000 * V * G
    return ("těleso o objemu 2 litry (0,002 m³) ponořené do vody nadnáší síla F = ρ·V·g = 1000·0,002·9,81 "
            "= %s N; pokud těleso váží méně (hmotnost pod 2 kg), plave" % _fmt(F))


def _priklad_pascal():
    p = 1000 * G * 10
    return ("v hloubce 10 m pod hladinou je hydrostatický tlak p = ρ·g·h = 1000·9,81·10 = %s Pa "
            "(asi tolik, kolik je atmosférický tlak); podle Pascalova zákona se tlak v kapalině šíří "
            "všemi směry stejně — na tom stojí hydraulický zvedák" % _fmt(p))


def _priklad_hooke():
    return ("pružina s tuhostí k = 200 N/m natažená o 5 cm (0,05 m) táhne zpět silou F = k·x = 200·0,05 = 10 N; "
            "natažená o 10 cm silou 20 N")


def _priklad_pad():
    h = 20.0
    return ("kámen puštěný z výšky 20 m padá t = √(2·h/g) = %s s a dopadne rychlostí v = √(2·g·h) = %s m/s "
            "(%s km/h), bez odporu vzduchu; těžký i lehký předmět padají ve vakuu stejně rychle"
            % (_fmt(math.sqrt(2 * h / G)), _fmt(math.sqrt(2 * G * h)), _fmt(3.6 * math.sqrt(2 * G * h))))


_ZAKONY = [
    (r"zachovan\w*\s+energi|zakon\w*\s+o\s+zachovani\s+energi", _priklad_energie, "zákon zachování energie"),
    (r"zachovan\w*\s+hybnost", _priklad_hybnost, "zákon zachování hybnosti"),
    (r"(druh\w*|2\.)\s+newton\w*\s+zakon|newton\w*\s+(druh\w*|2\.)\s+zakon|zakon\w*\s+sily", _priklad_newton2,
     "2. Newtonův zákon"),
    (r"(prvn\w*|1\.)\s+newton\w*\s+zakon|newton\w*\s+(prvn\w*|1\.)\s+zakon|zakon\w*\s+setrvacnost",
     _priklad_newton1, "1. Newtonův zákon"),
    (r"(tret\w*|3\.)\s+newton\w*\s+zakon|newton\w*\s+(tret\w*|3\.)\s+zakon|akce\s+a\s+reakce",
     _priklad_newton3, "3. Newtonův zákon"),
    (r"ohm\w*\s+zakon", _priklad_ohm, "Ohmův zákon"),
    (r"archimed\w*\s+zakon|vztlakov\w*\s+sil", _priklad_archimedes, "Archimédův zákon"),
    (r"pascal\w*\s+zakon|hydrostatick\w*\s+tlak", _priklad_pascal, "Pascalův zákon"),
    (r"hook\w*\s+zakon|tuhost\w*\s+pruzin", _priklad_hooke, "Hookův zákon"),
    (r"volny\w*\s+pad|volnem\s+padu", _priklad_pad, "volný pád"),
]
_VYKLAD = re.compile(r"\b(vysvetl\w*|jak\s+funguj\w*|co\s+(?:je|rika|znamena|tvrdi)|"
                     r"priklad\w*|popis\w*|o\s+cem\s+je|rekni\s+mi\s+o|co\s+vis\s+o|objasn\w*|nauc)")


def priklad_zakona(text: str) -> Optional[str]:
    t = _f(re.sub(r"^\S+\s+se\s+pt[áa]:\s*", "", text or ""))
    if not _VYKLAD.search(t):
        return None
    for rx, fn, nazev in _ZAKONY:
        if re.search(rx, t):
            return "%s — %s" % (nazev, fn())
    return None


def podklad(text: str) -> Optional[tuple]:
    """→ (druh, řádek): ('vypocet'|'prevod'|'priklad', text), nebo None."""
    try:
        r = prevod(text)
        if r:
            return ("prevod", r)
        r = vypocet_fyzika(text)
        if r:
            return ("vypocet", r)
        r = priklad_zakona(text)
        if r:
            return ("priklad", r)
    except Exception:
        return None
    return None
