"""HANS_FYZIKA_MODEL_V1 (31. 10.) — obecný fyzikální výpočet: model napíše VÝRAZ,
program ho spočítá.

Vzniklo z otázky uživatele „nemůže si ostatní věci vyřešit Hans sám, než to všechno
kódovat?“. Měření `data/mereni/vyraz_model_31_10/` (35 otázek: gravitace, Kepler,
oběžné a únikové rychlosti, teplo, tlak, optika, foton, plyn, kyvadlo…):
qwen3:30b s tabulkou pojmenovaných konstant 33/35 (medián 2,8 s), hans-czech
z hlavy 8/25. Zbylé chyby jsou věrohodné fyzikální (2 l vody jako 2 m³, Slunce–Země
se vzdáleností Měsíce) → do podkladu jde i výraz s konstantami, ať Hans postup ukáže.

Volá se jen, když `hans_fyzika` nic nenašla a otázka vypadá jako výpočet. qwen3 běží
na druhé Ollamě jen na CPU (grafiku nezabírá); ne při hře ani při noční dávce
(PC zamrzlo, když qwen3 jel souběžně se studiem). Výraz se nespouští jako kód:
povolené znaky a jména, SymPy.
"""
from __future__ import annotations

import json
import logging
import re
import time
import unicodedata
from typing import Optional

_log = logging.getLogger(__name__)
MARK = "HANS_FYZIKA_MODEL_V1"

KONST = {"G": 6.674e-11, "c_light": 2.998e8, "h_planck": 6.626e-34, "k_B": 1.381e-23, "R_gas": 8.314,
         "N_A": 6.022e23, "g0": 9.81, "M_earth": 5.972e24, "R_earth": 6.371e6,
         "M_moon": 7.342e22, "R_moon": 1.737e6, "d_moon": 3.844e8, "M_sun": 1.989e30,
         "R_sun": 6.957e8, "AU": 1.496e11, "M_mars": 6.417e23, "R_mars": 3.3895e6,
         "M_jupiter": 1.898e27, "R_jupiter": 6.9911e7, "year": 3.156e7, "day": 86400.0,
         "g_moon": 1.62, "g_mars": 3.71, "c_water": 4186.0, "rho_water": 1000.0, "e_charge": 1.602e-19, "m_e": 9.109e-31}
_POPIS_K = {"G": "gravitační konstanta", "c_light": "rychlost světla", "h_planck": "Planckova konstanta",
            "k_B": "Boltzmannova konstanta", "R_gas": "plynová konstanta", "N_A": "Avogadrova konstanta",
            "g0": "tíhové zrychlení Země", "M_earth": "hmotnost Země", "R_earth": "poloměr Země",
            "M_moon": "hmotnost Měsíce", "R_moon": "poloměr Měsíce", "d_moon": "vzdálenost Země–Měsíc",
            "M_sun": "hmotnost Slunce", "R_sun": "poloměr Slunce", "AU": "vzdálenost Země–Slunce (1 AU)",
            "M_mars": "hmotnost Marsu", "R_mars": "poloměr Marsu", "M_jupiter": "hmotnost Jupiteru",
            "R_jupiter": "poloměr Jupiteru", "year": "rok v sekundách", "day": "den v sekundách",
            "g_moon": "tíhové zrychlení Měsíce", "g_mars": "tíhové zrychlení Marsu", "c_water": "měrná tepelná kapacita vody", "rho_water": "hustota vody",
            "e_charge": "elementární náboj", "m_e": "hmotnost elektronu"}
_JED = {"g/cm^3": (1000, "kg/m³"), "g/cm³": (1000, "kg/m³"), "g/cm3": (1000, "kg/m³"),
        "km/s": (1000, "m/s"), "km/h": (1 / 3.6, "m/s"), "km": (1000, "m"), "cm": (0.01, "m"),
        "mm": (0.001, "m"), "kj": (1000, "J"), "mj": (1e6, "J"), "kwh": (3.6e6, "J"),
        "min": (60, "s"), "h": (3600, "s"), "hod": (3600, "s"), "dny": (86400, "s"),
        "dní": (86400, "s"), "dni": (86400, "s"), "days": (86400, "s"), "day": (86400, "s"),
        "kpa": (1000, "Pa"), "mpa": (1e6, "Pa"), "kn": (1000, "N"), "ev": (1.602e-19, "J")}
_SYS = ("Available named constants (SI, use these names in vyraz instead of typing values): "
        + ", ".join("%s=%g" % kv for kv in KONST.items()) + ". "
        "You convert a physics question into ONE calculation. Do NOT compute the result. "
        "Output ONLY JSON: {\"vyraz\": \"<arithmetic expression using numbers in SI units and the "
        "named constants, + - * / ** ( ), sqrt, pi, exp, log>\", \"jednotka\": \"<SI unit of the "
        "result>\", \"co\": \"<short English name of the computed quantity>\"}. Convert every "
        "given value to SI first (litres of water → kg, km → m, minutes → s). If the question "
        "is not a numeric physics calculation, output {\"vyraz\": \"\"}. Use a named constant ONLY for "
        "exactly the quantity it names (never d_moon for the Sun distance, never M_moon for another "
        "mass); values given in the question go in as numbers; every name in vyraz must be one of "
        "the constants above. "
        # HANS_FYZIKA_VZORCE_V1 (4. 11.) — oběh ISS: model psal 2*pi*r/sqrt(G*M) (chybí r^3/2)
        "For a circular orbit use the functions orbital_period(r, M) (seconds) and "
        "orbital_speed(r, M) (m/s), where r is the distance from the CENTRE of the body "
        "(its radius + altitude, in m) and M its mass.")


def _f(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", (s or "").lower())
                   if unicodedata.category(c) != "Mn")


# ── je to výpočet? ───────────────────────────────────────────────────────────
_FYZ = re.compile(
    r"\b(sil\w*|rychlost\w*|zrychlen\w*|energi\w*|tlak\w*|tepl[ao]\b|tepla\b|vykon\w*|proud\w*|"
    r"napet\w*|hybnost\w*|hustot\w*|frekvenc\w*|vlnov\w*|period\w*|obeh\w*|obiha\w*|unikov\w*|"
    r"gravitac\w*|gravitacn\w*|tihov\w*|prac[ei]\b|ohnisk\w*|foton\w*|kmit\w*|prikon\w*|leti|letet|uvoln\w*|"
    r"vztlak\w*|odpor\w*|naboj\w*|padat|pada|padne|dopad\w*|vyleti|vyletí|doleti|doletí|"
    r"prita[hz]\w*|vazil\w*|vazi\b|vazit|vahu|vaha|tiha|tihu)\b")
_TELESO = re.compile(r"\b(zem[eie]|mesic\w*|slunc\w*|mars\w*|jupiter\w*|druzic\w*|iss|planet\w*|"
                     r"svetl\w*|zvuk\w*|vod[aeyu]\b|plyn\w*|kyvad\w*|cock\w*|elektron\w*)\b")
_OTAZ = re.compile(r"\b(kolik|jak\w*|jaky|jaka|jake|vypocit\w*|spocit\w*|spocti|urci)\b")
_CISLO_JED = re.compile(r"\d+(?:[.,]\d+)?\s*(?:m|km|cm|mm|kg|g|t|s|min|h|hod\w*|km/h|m/s|n|j|kj|kwh|w|kw|"
                        r"v|a|pa|kpa|hz|nm|l|litr\w*|gram\w*|°c|stup\w*|au|astronom\w*)\b")
_NE = re.compile(r"\b(umis|umite|dokazes|dokazete|stoji|cena|cenu|kc\b|korun|hodin\s+je|je\s+hodin|film\w*|serial\w*|"
                 r"tv\b|televiz\w*|kodi|ty\b|ti\b|tvuj|tve|vy\b|vas\b|vam\b|tebe|sebe)\b")


def vypada_jako_vypocet(text: str) -> bool:
    t = _f(re.sub(r"^\S+\s+se\s+pt[áa]:\s*", "", text or ""))
    if not t or not _OTAZ.search(t) or not _FYZ.search(t):
        return False
    if _NE.search(t) and not _CISLO_JED.search(t):
        return False
    return bool(_CISLO_JED.search(t) or _TELESO.search(t))


# ── výraz → číslo ────────────────────────────────────────────────────────────
def spocti(vyraz: str) -> Optional[float]:
    import sympy as sp
    v = (vyraz or "").replace("^", "**").replace("×", "*").replace("·", "*")
    v = re.sub(r"(?<=\d),(?=\d)", ".", v)   # HANS_FYZIKA_VZORCE_V1: desetinná čárka, ne oddělovač argumentů
    if not v.strip() or len(v) > 300 or re.search(r"[^0-9eE+\-*/().,\s A-Za-z_]", v):
        return None
    fn = {"sqrt": sp.sqrt, "pi": sp.pi, "exp": sp.exp, "log": sp.log, "sin": sp.sin, "cos": sp.cos}
    _Gs = sp.Float(KONST["G"])   # HANS_FYZIKA_VZORCE_V1 — pojmenované vzorce oběhu
    fn["orbital_period"] = lambda r, M: 2 * sp.pi * sp.sqrt(r ** 3 / (_Gs * M))
    fn["orbital_speed"] = lambda r, M: sp.sqrt(_Gs * M / r)
    for w in re.findall(r"[A-Za-z_][A-Za-z_0-9]*", v):
        if w not in fn and w not in KONST and not re.fullmatch(r"e\d*", w):
            return None
    try:
        loc = dict(fn); loc.update({k: sp.Float(x) for k, x in KONST.items()})
        x = complex(sp.N(sp.sympify(v, locals=loc, evaluate=True)))
        if abs(x.imag) > 1e-9 * max(1.0, abs(x.real)):
            return None
        return float(x.real)
    except Exception:
        return None


def _cz(x: float) -> str:
    a = abs(x)
    if a != 0 and (a >= 1e6 or a < 1e-3):
        m, e = ("%.3e" % x).split("e")
        return "%s·10^%d" % (m.rstrip("0").rstrip(".").replace(".", ","), int(e))
    s = ("%.4g" % x)
    if "e" in s:
        s = "%.0f" % x
    s = s.replace(".", ",")
    mm = re.match(r"^(-?)(\d+)(,\d+)?$", s)
    if mm and len(mm.group(2)) > 4:
        c = mm.group(2)
        s = mm.group(1) + " ".join([c[max(0, i - 3):i] for i in range(len(c), 0, -3)][::-1]) + (mm.group(3) or "")
    return s


# HANS_FYZIKA_MODEL_V1 — konstanta musí mít v otázce oporu (slovo). Doloženo měřením:
# zvuk 440 Hz → c_light (otázka dávala 343 m/s), „1 gram hmoty“ → m_e, Slunce–Země →
# d_moon. Bez opory → výsledek zahodit (Hans odpoví jako dřív, ne přesným nesmyslem).
_OPORA = {
    "c_light": r"svetl|foton|radiov|signal|elektromagnet|laser|vlnov\w*\s+delk\w*\s+svetl|"
               r"(hmot\w*.*energi|energi\w*.*hmot)|e\s*=\s*mc",
    "h_planck": r"foton|kvant|planck|svetl", "k_B": r"boltzmann|molekul|atom|castic",
    "R_gas": r"plyn|mol\b|molu|molar", "N_A": r"mol\b|molu|atom|molekul|avogadr",
    "M_earth": r"zem[eiě]|zemsk|druzic|iss|obezn|unikov|orbit|tihov",
    "R_earth": r"zem[eiě]|zemsk|druzic|iss|obezn|unikov|orbit|povrch|vysce|vysk",
    "M_moon": r"mesic", "R_moon": r"mesic", "d_moon": r"mesic", "g_moon": r"mesic", "g_mars": r"mars",
    "M_sun": r"slunc|planet\w*\s+obiha|obiha\w*\s+kolem", "R_sun": r"slunc",
    "AU": r"slunc|astronom|\bau\b|planet", "M_mars": r"mars", "R_mars": r"mars",
    "M_jupiter": r"jupiter", "R_jupiter": r"jupiter", "year": r"rok|roc|let\b|oben|obeh|svetel",
    "day": r"den|dny|dni|dnu|oben|obeh", "c_water": r"vod|led", "rho_water": r"vod|ponor|plav|hloubk|vztlak",
    "e_charge": r"elektron|naboj|ev\b|elektronvolt", "m_e": r"elektron",
}


def _konstanty_s_oporou(vyraz: str, otazka: str) -> list:
    """→ seznam konstant BEZ opory v otázce (prázdný = vše v pořádku)."""
    t = _f(otazka)
    bez = []
    for n, rx in _OPORA.items():
        if re.search(r"(?<![A-Za-z_])%s(?![A-Za-z_0-9])" % re.escape(n), vyraz) and not re.search(rx, t):
            bez.append(n)
    return bez


_RADY = [(24, "kvadrilionů"), (21, "triliard"), (18, "trilionů"), (15, "biliard"), (12, "bilionů"),
         (9, "miliard"), (6, "milionů")]


def _slovy(x: float) -> str:
    """Velké číslo česky („198 trilionů“) — model mocniny deseti slovy plete."""
    a = abs(x)
    for e, slovo in _RADY:
        if a >= 10 ** e:
            v = x / 10 ** e
            s = ("%.3g" % v)
            if "e" in s:
                s = "%.0f" % v
            return "%s %s" % (s.replace(".", ","), slovo)
    return ""


def _gpu_cpu_volno() -> bool:
    try:
        from scripts.ollama_client import game_mode_on, base_slot_busy
        return not game_mode_on() and not base_slot_busy()
    except Exception:
        return True


# HANS_FYZIKA_LITRY_V1 (4. 11.) — „ohřátí 2 litrů vody“: model psal 2 · 1000 · 4186 · 80
# (litry jako gramy → 670 MJ místo 670 kJ); tutéž chybu měl i qwen3:30b 31. 10., ačkoli
# zadání převod litrů na kg výslovně chce. Litry/mililitry VODY převede kód předem.
def litry_vody_na_kg(q: str) -> str:
    def _r(m):
        try:
            v = float(m.group(1).replace(",", "."))
        except ValueError:
            return m.group(0)
        if m.group(2).lower().startswith("m"):
            v /= 1000.0
        s = ("%g" % v).replace(".", ",")
        return "%s kg %s" % (s, m.group(3))
    return re.sub(r"(\d+(?:[.,]\d+)?)\s*(ml|mililitr\w*|l|litr\w*)\.?\s+(vod\w*)", _r, q or "",
                  flags=re.I)


def vypocet_modelem(config: dict, text: str, timeout: float = 25.0) -> Optional[str]:
    """Řádek „výraz s konstantami → výsledek“, nebo None (nevýpočet, chyba, pomalé)."""
    c = (config or {}).get("fyzika_model", {}) or {}
    if not c.get("enabled", True) or not vypada_jako_vypocet(text):
        return None
    if not _gpu_cpu_volno():
        _log.info("%s: přeskočeno (hra nebo noční dávka)", MARK)
        return None
    try:   # HANS_VYPOCTY_RAM_V1 — týž qwen3 jako výpočty
        from scripts.hans_vypocty_model import pamet_pro_qwen3
        if not pamet_pro_qwen3(config, "fyzika"):
            return None
    except Exception:
        pass
    try:
        import requests
        from scripts.hans_book_summary import _cpu_url
        url = _cpu_url(config)
        q = re.sub(r"^\S+\s+se\s+pt[áa]:\s*", "", text or "").strip()
        q = litry_vody_na_kg(q)   # HANS_FYZIKA_LITRY_V1
        body = {"model": c.get("model", "qwen3:30b"), "stream": False, "format": "json",
                "think": False, "keep_alive": c.get("keep_alive", "30m"),
                "messages": [{"role": "system", "content": _SYS}, {"role": "user", "content": q}],
                "options": {"temperature": 0, "num_ctx": 2048, "num_predict": 300}}
        t0 = time.time()
        r = requests.post(url + "/api/chat", json=body, timeout=max(timeout, float(c.get("timeout", 45))))   # HANS_VYPOCTY_NAHRATI_V1
        d = json.loads(r.json().get("message", {}).get("content", "") or "{}")
    except Exception as e:
        _log.info("%s: model nedostupný (%s)", MARK, str(e)[:80])
        if "timed out" in str(e):   # HANS_VYPOCTY_NAHRATI_V1 — týž model jako výpočty
            try:
                from scripts.hans_vypocty_model import nahrej_na_pozadi
                nahrej_na_pozadi(config, "timeout fyziky")
            except Exception:
                pass
        return None
    vyraz = str(d.get("vyraz") or "").strip()
    _bez = _konstanty_s_oporou(vyraz, q)
    if _bez:
        _log.info("%s: konstanta bez opory v otázce %s → zahazuji: %.100r", MARK, _bez, vyraz)
        return None
    x = spocti(vyraz)
    if x is None:
        _log.info("%s: výraz nelze spočítat: %.100r", MARK, vyraz)
        return None
    jed = str(d.get("jednotka") or "").strip()
    k = _JED.get(jed.lower().replace(" ", ""))
    if k:
        x, jed = x * k[0], k[1]
    jed = jed.replace("^2", "²").replace("**2", "²").replace("^3", "³").replace("**3", "³")
    pouz = [n for n in KONST if re.search(r"(?<![A-Za-z_])%s(?![A-Za-z_0-9])" % re.escape(n), vyraz)]
    konst = "; ".join("%s = %s (%s)" % (n, _cz(KONST[n]), _POPIS_K.get(n, n)) for n in pouz)
    zapis = vyraz.replace("**", "^").replace("*", "·")
    co = str(d.get("co") or "").strip()
    _sl = _slovy(x)
    out = "%s%s%s → výsledek = %s %s%s" % (
        "", "výpočet: %s" % zapis,
        ("; dosazené konstanty: %s" % konst) if konst else "", _cz(x), jed,
        (" (slovy: %s %s)" % (_sl, jed)) if _sl else "")
    _log.info("%s: %.1f s · %s", MARK, time.time() - t0, out[:200])
    return out
