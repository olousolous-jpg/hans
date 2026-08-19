"""
CZ_NAMES_V1 — vokativ + skloňování sloves podle rodu známé osoby.

Nahrazuje generické "jana přišel/a" → "Přišla Jana" a "Standa" v oslovení
→ "Stando". Zdroj rodu: config.known_persons[name].gender ("muž" | "žena").
Neznámé jméno / chybějící config = fallback = base (žádná změna vs dřívější
chování — nic se nerozbije).

Pragmatické, ne dokonalé: pokrývá jen tvary skutečně používané v Hansově
kódu (přišel/přišla, byl/byla, viděl/viděla, řekl/řekla). Rozšiřuj podle
potřeby — přidej pár do _PAST_M_TO_F.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional

_ROOT = Path(__file__).resolve().parent.parent

# ── Vokativ ──────────────────────────────────────────────────────────────

# HANS_VOCATIVE_CONSONANT_V1 — generické fallbacky, NE křestní jména: souhláskový
# vokativ by je zmršil („Uživatel"→„Uživatle"). Necháváme beze změny (jako dřív).
_GENERIC_ADDRESS = frozenset({
    "uživatel", "uzivatel", "unknown", "unknown_person", "neznámý", "neznamy",
    "host", "návštěva", "navsteva", "člověk", "clovek", "pán", "pan",
})


def vocative(name: str, gender: Optional[str] = None,
             config: Optional[dict] = None) -> str:
    """Vokativ (5. pád) pro oslovení.

    Pravidla (běžná CZ jména končící -a):
      končí -a → -o        (Standa→Stando, Jana→Jano, Honza→Honzo)
      končí -e → beze změny (Marie zůstává Marie)
      jinak    → beze změny (Petr zůstává Petr; palatalizaci si netroufáme)

    Rod se u zakončení -a nerozlišuje — muž i žena berou -o. Rod se zatím
    zadává jen kvůli symetrii s ostatními helpery a pro budoucí rozšíření.
    """
    if not name:
        return name
    over = _person_form(name, "voc", config)   # HANS_NAME_INFLECTION_V2
    if over:
        return over
    base = _title(name)
    low = base.lower()
    if low.endswith("a"):
        return base[:-1] + "o"
    if low.endswith(("e", "i", "o", "u", "y", "í", "é", "á", "ý", "ů", "ú")):
        return base                            # samohláska → beze změny (Marie, Jiří)
    if low in _GENERIC_ADDRESS:
        return base                            # „Uživatel"/„Unknown" nejsou jména
    # HANS_VOCATIVE_CONSONANT_V1 — mužský souhláskový vokativ (Petr→Petře).
    # Dřív se souhláska nechávala beze změny → nováček „Petr" zůstal „Petr".
    # Aplikuje se JEN když rod NENÍ ženský (žena končící souhláskou = vokativ
    # == nominativ: Dagmar→Dagmar). Neznámý rod (cizí člověk) → mužský tvar,
    # protože drtivá většina souhláskových křestních jmen je mužská.
    if gender in ("žena", "z", "f", "female"):
        return base
    return _masc_consonant_vocative(base)


def _masc_consonant_vocative(base: str) -> str:
    """Mužský vokativ jmen končících souhláskou. Pragmatické, ne dokonalé —
    pokrývá běžné vzory; okrajové (Zdeněk) svěř config override `voc`."""
    low = base.lower()
    if low[-1] in "šžčřc" or low.endswith("j"):
        return base + "i"                      # Tomáš→Tomáši, Ondřej→Ondřeji
    if low.endswith("ek"):
        return base[:-2] + "ku"                # Marek→Marku, Radek→Radku (e-drop)
    if low.endswith(("k", "g", "h")) or low.endswith("ch"):
        return base + "u"                      # Patrik→Patriku, Bedřich→Bedřichu
    if low.endswith("el"):
        return base[:-2] + "le"                # Karel→Karle, Pavel→Pavle (e-drop)
    if len(low) >= 2 and low[-1] == "r" and low[-2] not in "aeiouyáéíóúůě":
        return base[:-1] + "ře"                # Petr→Petře, Alexandr→Alexandře
    return base + "e"                          # Martin→Martine, Jan→Jane, Igor→Igore


# ── Akuzativ (4. pád) — „Viděl jsem Janu / Standu / Petra“ ────────

def accusative(name: str, gender: Optional[str] = None,
               config: Optional[dict] = None) -> str:
    """Akuzativ (4. pád) jména. Config override known_persons[name].acc má
    přednost; jinak algoritmus:
      končí -a → -u (Jana→Janu, Standa→Standu; muž i žena stejně)
      muž + souhláska → +a (Petr→Petra)
      jinak → beze změny (nic nerozbit).
    """
    if not name:
        return name
    over = _person_form(name, "acc", config)   # HANS_NAME_INFLECTION_V2
    if over:
        return over
    base = _title(name)
    low = base.lower()
    if low.endswith("a"):
        return base[:-1] + "u"
    if gender in ("muž", "m", "male") and low[-1:] not in "aeiouyáéíóúůě":
        return base + "a"
    return base


# ── Skloňování minulého času (l-participium) ─────────────────────────────

_PAST_M_TO_F = {
    "přišel":   "přišla",
    "nepřišel": "nepřišla",
    "odešel":   "odešla",
    "neodešel": "neodešla",
    "byl":      "byla",
    "nebyl":    "nebyla",
    "viděl":    "viděla",
    "neviděl":  "neviděla",
    "řekl":     "řekla",
    "šel":      "šla",
    "napsal":   "napsala",
    "spal":     "spala",
    "přijal":   "přijala",
    "sledoval": "sledovala",
    "zmínil":   "zmínila",
}


def past_verb(masc_form: str, gender: Optional[str]) -> str:
    """Vrátí mužský nebo ženský tvar minulého l-participia.

    gender='žena' → ženský tvar (přišla). Jinak → mužský (přišel).
    Neznámé sloveso → vrátí vstup beze změny (nic nerozbít).
    """
    if gender in ("žena", "z", "f", "female"):
        return _PAST_M_TO_F.get(masc_form, masc_form)
    return masc_form


# ── Config lookup (rod z known_persons) ───────────────────────────────────

_config_cache: Optional[dict] = None


def _load_config() -> dict:
    global _config_cache
    if _config_cache is None:
        try:
            _config_cache = json.loads(
                (_ROOT / "config.json").read_text(encoding="utf-8"))
        except Exception:
            _config_cache = {}
    return _config_cache


def is_known_person(name: str, config: Optional[dict] = None) -> bool:
    """HANS_HOUSEHOLD_PRIVACY_V1 (19.8.) — je tazatel někdo ze `známých osob`?

    Zdroj je jediný: `known_persons` v configu. Porovnává se přes složenou
    diakritiku a bere i tvary `nom`/`full`, aby „Johana" i „jana" sedly.
    """
    import unicodedata as _ud
    n = "".join(c for c in _ud.normalize("NFKD", (name or "").strip().lower())
                if not _ud.combining(c))
    if not n:
        return False
    kp = ((config if config is not None else _load_config()) or {}).get(
        "known_persons", {}) or {}
    for key, rec in kp.items():
        forms = {str(key)}
        for f in ("nom", "full"):
            v = (rec or {}).get(f)
            if v:
                forms.add(str(v))
        for form in forms:
            ff = "".join(c for c in _ud.normalize("NFKD", form.strip().lower())
                         if not _ud.combining(c))
            if ff and ff == n:
                return True
    return False


def find_known_person(message: str, config: Optional[dict] = None) -> str:
    """HANS_PERSON_CARD_V1 (18.8.) — jmenuje věta někoho ze `known_persons`?
    Vrátí KLÍČ osoby (`klara`), jinak "".

    Sem se to vytáhlo z `hans_agent._asks_person_presence`, aby dvě cesty
    (dotaz na PŘÍTOMNOST × dotaz KDO TO JE) nehledaly jméno každá po svém a
    nerozešly se. Bere tvary z configu (`nom`/`acc`/`voc`), diakritiku skládá
    pryč a porovnává na PREFIX bez poslední hlásky, takže projdou i pády,
    které v configu nejsou („Klárou“, „o Kláře“).
    """
    import re as _re
    import unicodedata as _ud

    def _fold(s):
        return "".join(c for c in _ud.normalize("NFKD", (s or "").lower())
                       if not _ud.combining(c))

    msg = _fold(message)
    if not msg:
        return ""
    cfg = config if config is not None else _load_config()
    kp = (cfg or {}).get("known_persons", {}) or {}
    best, best_len = "", 0
    for key, rec in (kp or {}).items():
        forms = {_fold(key)}
        for f in ("nom", "acc", "voc", "full", "gen", "dat", "loc", "ins"):
            v = (rec or {}).get(f)
            if v:
                forms.add(_fold(v))
        for n in forms:
            if len(n) < 3:
                continue
            stem = n[:-1] or n
            if _re.search(r"\b%s" % _re.escape(stem), msg):
                # nejdelší shoda vyhrává (delší tvar = jistější trefa)
                if len(stem) > best_len:
                    best, best_len = key, len(stem)
    return best


def person_gender(name: str, config: Optional[dict] = None) -> Optional[str]:
    """Vytáhne rod z config.known_persons (klíč = lower-case jméno).

    Vrací 'muž' / 'žena' / None (neznámý)."""
    if not name:
        return None
    cfg = config if config is not None else _load_config()
    persons = (cfg.get("known_persons") or {})
    rec = persons.get(name.lower())
    if not rec:
        return None
    return rec.get("gender")


def _person_form(name: str, key: str,
                 config: Optional[dict] = None) -> Optional[str]:
    """Explicitní pádový tvar z known_persons[name][key] (nom/acc/voc).
    None = nezadáno → algoritmický fallback. HANS_NAME_INFLECTION_V2."""
    if not name:
        return None
    cfg = config if config is not None else _load_config()
    rec = (cfg.get("known_persons") or {}).get(name.lower())
    if not rec:
        return None
    v = rec.get(key)
    return v.strip() if isinstance(v, str) and v.strip() else None


def display_name(name: str, config: Optional[dict] = None) -> str:
    """'jana' → 'Jana'; známá osoba → config nom ('jana' → 'Jana').
    HANS_NAME_INFLECTION_V2 — nom z known_persons opraví i ASCII klíč."""
    if not name:
        return name
    nom = _person_form(name, "nom", config)
    return nom if nom else _title(name)


def formal_name(name: str, config: Optional[dict] = None) -> str:
    """HANS_NAME_RESTORE_V1 — plné/formální jméno (known_persons[name].full,
    např. 'Johana' pro zdrobnělinu 'Jana'); jinak běžné display jméno.
    Pro úvahy/reflexe (self_insight), kde je formální tvar na místě."""
    if not name:
        return name
    full = _person_form(name, "full", config)
    return full if full else display_name(name, config)


def _title(name: str) -> str:
    if len(name) <= 1:
        return name.upper()
    return name[0].upper() + name[1:].lower()


# ── Convenience wrappery (nejčastější použití v Hansově kódu) ────────────

def address(name: str, config: Optional[dict] = None) -> str:
    """Vokativ podle registrace v known_persons — jednořádkový shortcut."""
    return vocative(name, person_gender(name, config), config)


def came(name: str, config: Optional[dict] = None) -> str:
    """'přišel' / 'přišla' podle rodu Osoby (dle known_persons)."""
    return past_verb("přišel", person_gender(name, config))


def left(name: str, config: Optional[dict] = None) -> str:
    """'odešel' / 'odešla' podle rodu Osoby."""
    return past_verb("odešel", person_gender(name, config))


def was(name: str, config: Optional[dict] = None, negate: bool = False) -> str:
    """'byl' / 'byla' (nebo 'nebyl' / 'nebyla' při negate=True)."""
    return past_verb("nebyl" if negate else "byl", person_gender(name, config))


def saw(name: str, config: Optional[dict] = None, negate: bool = False) -> str:
    """'viděl' / 'viděla' (nebo 'neviděl' / 'neviděla')."""
    return past_verb("neviděl" if negate else "viděl", person_gender(name, config))


def acc(name: str, config: Optional[dict] = None) -> str:
    """Akuzativ jména podle known_persons — shortcut ('Janu')."""
    return accusative(name, person_gender(name, config), config)


# ── Obnova zmršených jmen z voice modelu (HANS_NAME_RESTORE_V1) ───────────

def _known_person_forms(config: Optional[dict] = None) -> set:
    """Množina KANONICKÝCH tvarů známých osob (nom/acc/voc z configu + regulární
    gen -y, instr -ou u -a jmen). Proti nim se poměřují zmršené varianty."""
    cfg = config if config is not None else _load_config()
    out: set = set()
    for key, rec in (cfg.get("known_persons") or {}).items():
        if not isinstance(rec, dict):
            continue
        nom = (rec.get("nom") or _title(key)).strip()
        if nom:
            out.add(nom)
        for k in ("acc", "voc"):
            v = rec.get(k)
            if isinstance(v, str) and v.strip():
                out.add(v.strip())
        if nom.endswith("a"):
            base = nom[:-1]
            out.update({base + "y", base + "ou"})
        # HANS_NAME_RESTORE_V1 — plné jméno (zdrobnělina→plné, Jana=Johana):
        # uznej i jeho tvary, ať je voice model nemrší. Bez config pádů → deriv.
        full = (rec.get("full") or "").strip()
        if full:
            out.add(full)
            if full.endswith("a"):
                fb = full[:-1]
                out.update({fb + "y", fb + "u", fb + "ou", fb + "o"})
    return out


def _is_subseq(a: str, b: str) -> bool:
    it = iter(b)
    return all(c in it for c in a)


def restore_person_names(text: str, config: Optional[dict] = None) -> str:
    """HANS_NAME_RESTORE_V1 — obnoví jména známých osob zmršená voice modelem
    (Jana→Janika/Janiku/Janikou). Token, z něhož smazáním 1–3 znaků vznikne
    kanonický tvar (a sdílí první 2 znaky), nahradí NEJDELŠÍ takovou shodou."""
    if not text:
        return text
    forms = _known_person_forms(config)
    if not forms:
        return text
    lower_ok = {f.lower() for f in forms}

    def _fix(m):
        w = m.group(0)
        wl = w.lower()
        if wl in lower_ok:
            return w
        best = None
        for f in forms:
            if (f[:2].lower() == wl[:2] and 1 <= len(w) - len(f) <= 3
                    and _is_subseq(f.lower(), wl)):
                if best is None or len(f) > len(best):
                    best = f
        return best if best else w

    return re.sub(r"\b\w+\b", _fix, text)


# ── Smoke (python3 -m scripts.cz_names) ──────────────────────────────────
if __name__ == "__main__":
    print("=== vocative ===")
    for n, g in [("Standa", "muž"), ("Jana", "žena"), ("Honza", "muž"),
                 ("Marie", "žena"), ("Petr", "muž"), ("", None)]:
        print(f"  {n!r:12} ({g}) → {vocative(n, g)!r}")

    print("\n=== past_verb ===")
    for v in ("přišel", "odešel", "byl", "nebyl", "viděl", "řekl"):
        print(f"  {v!r:10} muž → {past_verb(v, 'muž')!r},  "
              f"žena → {past_verb(v, 'žena')!r}")

    print("\n=== config lookup (jména z lokálního configu, ne napevno) ===")
    _names = list((_load_config().get("known_persons") or {}).keys()) or ["unknown"]
    for n in _names + ["unknown"]:
        g = person_gender(n)
        print(f"  {n!r:10} → gender={g!r},  address={address(n)!r},  "
              f"came={came(n)!r},  was_neg={was(n, negate=True)!r}")


# ── HANS_ADDRESSEE_V2 (4.8.) — deterministická oprava oslovení ───────────────
def fix_addressee(text: str, partner: str, config: Optional[dict] = None):
    """Přepiš oslovení CIZÍ osoby na skutečného partnera. Vrací (text, počet).

    Proč deterministicky a ne promptem: hans-czech je persona finetune a
    system prompt přebíjí — doloženo 4.8., kdy uživatel napsal „jak se máš?" a Hans
    odpověděl „Odpovím vám, paní Jano" (a po zesílení promptu + přesunu
    adresáta na konec ještě „Jsem v pořádku, Jano"). Týž vzor jako
    HANS_KNOWLEDGE_CHECK_V1: tři iterace promptu nezabraly, zabral až bypass.

    Přepisuje VÝHRADNĚ VOKATIV (5. pád) cizí osoby — tedy tvar, který slouží
    k oslovení („Jano"). Zmínky ve 3. osobě („Jana je doma", „vidím Janu")
    zůstávají nedotčené, protože ty jsou legitimní odpovědí na dotaz o domě.
    """
    if not text or not partner:
        return text, 0
    known = (config or {}).get("known_persons", {}) or {}
    if not known:
        return text, 0
    target = address(partner, config)          # správné oslovení partnera
    n = 0
    # HANS_ADDRESSEE_V3 (5.8.) — nejdřív srovnej oslovení SAMOTNÉHO partnera.
    # Model bere jméno z konfiguračního klíče, takže osloví 1. pádem a malým
    # písmenem („Odpovídám vám, jana:" místo „Jano"). Doloženo 4.8. 17:53;
    # v týdenním vzorku 1/103 odpovědí, takže jde o variabilitu, ne o pravidlo —
    # oprava je ale zadarmo, brzda po odpovědi běží tak jako tak. Řeší i
    # diakritiku (config klíč bez háčků → správný tvar s diakritikou).
    for _form in {str(partner), display_name(partner, config)}:
        if not _form or len(_form) < 3 or _form.lower() == target.lower():
            continue
        # jen v OSLOVENÍ: za čárkou / před dvojtečkou — ne ve 3. osobě
        _p_addr = re.compile(r"(?<=,)(\s*)" + re.escape(_form) + r"\b(?=\s*[:,.!?]|\s)",
                             re.IGNORECASE)
        text, _c = _p_addr.subn(lambda m: m.group(1) + target, text)
        n += _c
        # HANS_VOCATIVE_CONSONANT_V1 — „pane Petr" (titul + 1. pád partnera, ne
        # po čárce → předchozí vzor to minul). Titul = jednoznačné oslovení.
        _p_title = re.compile(r"\b(pan[íi]|pane)\s+" + re.escape(_form) + r"\b",
                              re.IGNORECASE)
        text, _ct = _p_title.subn(lambda m: m.group(1) + " " + target, text)
        n += _ct
    for pname in known:
        if str(pname).strip().lower() == str(partner).strip().lower():
            continue
        voc = address(pname, config)           # vokativ cizí osoby
        if not voc or len(voc) < 3:
            continue
        # volitelný titul „pane/paní" před jménem se přepíše zároveň
        pat = re.compile(r"\b(?:pan[íi]\s+|pane\s+)?" + re.escape(voc) + r"\b",
                         re.IGNORECASE)
        text, cnt = pat.subn(target, text)
        n += cnt
        # „paní Jana" — model titul spojí s 1. pádem, ne s vokativem.
        # HANS_ADDRESSEE_3RD_PERSON_V1 (19.8.) — dřív se to bralo za
        # JEDNOZNAČNÉ oslovení a přepisovalo KDEKOLI ve větě. To je ale chyba:
        # „paní Jana je paní domu" je 3. osoba, ne oslovení. Doloženo 4× živě,
        # mimo jiné „V tomto domě bydlí pan Standa, **Stando** a slečna Klára"
        # (člen domácnosti z výčtu ZMIZEL) a „**Stando** si zakládá na pohodlí"
        # (věta byla o paní domu). Docstring téhle funkce přitom slibuje, že
        # zmínky ve 3. osobě zůstanou nedotčené — tenhle vzor ten slib porušoval.
        # Nově se přepisuje JEN ve skutečné oslovovací pozici: začátek věty nebo
        # za čárkou A ZÁROVEŇ následuje interpunkce/konec.
        # (Vokativ cizí osoby — „paní Jano" — se dál přepisuje kdekoli, viz `pat`
        # výš: to je oslovení nesprávného člověka a patří opravit.)
        for form in {str(pname), display_name(pname, config)}:
            if not form or len(form) < 3:
                continue
            pat2 = re.compile(
                r"(?:(?<=^)|(?<=,))(\s*)(?:pan[íi]|pane)\s+"
                + re.escape(form) + r"\b(?=\s*(?:[:,;!?]|$))",
                re.IGNORECASE | re.MULTILINE)
            # mezeru za čárkou vrátit zpět (jinak vznikne „Ano,Stando")
            text, cnt2 = pat2.subn(lambda m: m.group(1) + target, text)
            n += cnt2
    return text, n
