"""HANS_OVERENI_STIN_V1 — ověření údajů v odpovědi KÓDEM, jen ve stínu (17. 10.).

Po odeslání odpovědi (vlákno na pozadí) projde věty s rokem nebo vztahovým
slovem a porovná je s lokální Wikipedií (text článku + infobox) a s Wikidaty.
Odpověď se NEMĚNÍ; výsledek jde do `data/mereni/overeni_stin.jsonl` a řádku
logu. Bez modelu. Wikipedie jen lokálně (stín nebere kvótu), Wikidata síťově
s mezipamětí `data/wiki/wd_cache.json`.

Logika je přenesená z měření `data/mereni/overeni_kodem_17_10/` (87 vět:
text+Wikidata chytí 6/18 chyb, planě 2/69). Vypínač `overeni.stin`.
"""
import json
import logging
import os
import re
import threading
import time
import unicodedata
import urllib.parse

_log = logging.getLogger(__name__)
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOUBOR = os.path.join(_ROOT, "data", "mereni", "overeni_stin.jsonl")
CACHE = os.path.join(_ROOT, "data", "wiki", "wd_cache.json")
API = "https://www.wikidata.org/w/api.php"
_zamek = threading.Lock()

# ── kandidáti: jména ve větě ────────────────────────────────────────────────
_JMENO = re.compile(r"(?:[A-ZÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ][\wáčďéěíňóřšťúůýž.\-]+(?:\s+(?:[A-ZÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ]"
                    r"[\wáčďéěíňóřšťúůýž.\-]+|IV\.|II\.|I\.|of|the|na|nad|upon|de|da|z|ze))*)")
_NEJM = {"Dobrý", "Děkuji", "Ahoj", "Prosím", "Mohl", "Mohla", "Zajímalo", "Rád", "Ráda",
         "Velice", "Byla", "Byl", "Byli", "Je", "Jeho", "Její", "Podle", "Tento", "Tato",
         "Dále", "Když", "Vzhledem", "Nicméně", "Film", "Vím", "Kniha", "Zápis", "Jak",
         "Získal", "Leží", "Začala", "Přesné", "Žil", "Režie", "Režírovala", "Spolupracoval",
         "Vedle", "Z", "V", "Ve", "Na", "Dnes", "Datum", "Činnost", "Existují", "Organizace",
         "Stavba", "Hrad", "Narodil", "Zemřel", "Omlouvám", "Především", "Klíčové", "Druhá",
         "Největší", "Záznamy", "Můžu", "To", "Ten", "Ta", "Tak", "Ano", "Ne", "Pane", "Paní",
         "Autor", "Autorem", "Autorkou"}


def jmena(veta: str) -> list:
    k = []
    for m in _JMENO.finditer(veta):
        w = m.group(0).strip(" .,").split()
        while w and w[0].strip(",") in _NEJM:
            w = w[1:]
        s = " ".join(w).strip(" .,*")
        if len(s) >= 4 and s not in k and ". " not in s:
            k.append(s)
    k.sort(key=lambda x: -len(x.split()))
    return k


# ── údaje ve větě a v článku ────────────────────────────────────────────────
_DRUHY = {
    "narozeni": r"narodil|narozen|rodák|datum narození|světlo světa|přišel na svět|přišla na svět",
    "umrti": r"zemřel|zesnul|úmrtí|datum úmrtí|smrt|skonal|zahynul|naposledy vydechl",
    "zalozeni": r"založen|zbudován|postaven|vznik|stavb[ay]|zahájen|položen základní",
    "otevreni": r"otevřen|zprovozněn|slavnostně",
    "nataceni": r"natáčení|natáčel|natočen",
    "premiera": r"premiér|uveden do kin|měl premiéru",
    "vlada": r"vládl|panoval|korunován|nastoupil na trůn|králem|císařem",
    "smrt_bitva": r"bitv[aěu]",
}
_DR = {k: re.compile(v, re.I) for k, v in _DRUHY.items()}
_MESICE = ["ledna", "února", "března", "dubna", "května", "června", "července",
           "srpna", "září", "října", "listopadu", "prosince"]
_DATUM = re.compile(r"(?:(\d{1,2})\.\s*(" + "|".join(_MESICE) + r")\s+)?\b(1\d{3}|20\d\d|[5-9]\d\d)\b")


def _vety(text: str) -> list:
    return re.split(r"(?<=[.!?])\s+(?=[A-ZÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ\d])", text or "")


def _druh(okoli: str):
    """Druh podle klíčového slova NEJBLÍŽ konci okolí (= nejblíž číslu)."""
    best, pos = None, -1
    for k, rx in _DR.items():
        for m in rx.finditer(okoli):
            if m.end() > pos:
                best, pos = k, m.end()
    return best


_VEK = re.compile(r"(?:dožil\w*\s+se\s+(?:\w+\s+){0,2}?|ve\s+věku\s+|v\s+)(\d{1,3})\s+let", re.I)


def udaje_vety(veta: str, otazka: str = "") -> list:
    """[(druh, rok, den|None, měsíc|None, kus)] — druh z ±60 zn před číslem;
    věta bez druhu s jediným datem bere druh z otázky („Kdy se narodil X?“ →
    „X jsme přivítali na světě 2. března 1820“). Věk → („vek“, N, …)."""
    out = []
    for m in _VEK.finditer(veta):
        if re.search(r"zemřel|dožil|zesnul|skonal|věku", veta, re.I):
            out.append(("vek", int(m.group(1)), None, None, m.group(0)))
    m2 = re.search(r"(?:žil|žila)\s+v\s+letech\s+(\d{3,4})\s*(?:až|-|–)\s*(\d{3,4})", veta, re.I)
    if m2:
        out += [("narozeni", int(m2.group(1)), None, None, m2.group(0)),
                ("umrti", int(m2.group(2)), None, None, m2.group(0))]
    cisla = [m for m in _DATUM.finditer(veta)
             if not (m2 and m2.start() <= m.start() < m2.end())]
    for m in cisla:
        okoli = veta[max(0, m.start() - 60): m.start()]
        # celá věta jako záloha jen u jediného údaje (jinak by všechny roky
        # dostaly druh jednoho slova: „narodil … 1543 … 1642“)
        d = _druh(okoli) or (_druh(veta) if len(cisla) == 1 else None)
        if not d and len(cisla) == 1 and otazka:
            d = _druh(otazka)
        out.append((d, int(m.group(3)), int(m.group(1)) if m.group(1) else None,
                    _MESICE.index(m.group(2)) + 1 if m.group(2) else None, m.group(0)))
    return out


def udaje_clanku(text: str, box: str, titul: str = "") -> dict:
    """{druh: {(rok, den, měsíc)}} z vět článku, závorky úvodu a infoboxu."""
    U = {}

    def pridej(d, r, den=None, mes=None):
        U.setdefault(d, set()).add((r, den, mes))
    m = re.search(r"\(([^()]{4,200}?)\s[–-]\s([^()]{4,200}?)\)", text[:600])
    if m:
        for d, kus in (("narozeni", m.group(1)), ("umrti", m.group(2))):
            for x in _DATUM.finditer(kus):
                pridej(d, int(x.group(3)), int(x.group(1)) if x.group(1) else None,
                       _MESICE.index(x.group(2)) + 1 if x.group(2) else None)
    # věta článku jen z úvodu, nebo když jmenuje předmět článku; narození
    # a úmrtí jen ze závorky a infoboxu (jinde bývá křest, jiná osoba…)
    klic = [w[:5].lower() for w in re.findall(r"\w{4,}", re.sub(r"\(.*?\)", "", titul))]
    pos = 0
    for v in _vety(text):
        uvod = pos < 1500
        pos += len(v)
        if not (uvod or any(k in v.lower() for k in klic)):
            continue
        for x in _DATUM.finditer(v):
            d = _druh(v[max(0, x.start() - 80): x.end() + 40])
            if d and d not in ("narozeni", "umrti"):
                pridej(d, int(x.group(3)), int(x.group(1)) if x.group(1) else None,
                       _MESICE.index(x.group(2)) + 1 if x.group(2) else None)
    for radek in (box or "").split("\n"):
        if ":" not in radek:
            continue
        kl, hod = radek.split(":", 1)
        d = _druh(kl)
        if d:
            for x in _DATUM.finditer(hod):
                pridej(d, int(x.group(3)))
            for x in re.finditer(r"\b(1\d{3}|20\d\d)-(\d{1,2})-(\d{1,2})\b", hod):
                pridej(d, int(x.group(1)), int(x.group(3)), int(x.group(2)))
    return U


def porovnej(ud, U, text):
    """Rozpor jen podle ROKU a jen když hlášený rok v článku není vůbec."""
    druh, rok = ud[0], ud[1]
    if not druh or druh not in U:
        return "nevim", None
    roky = {r for r, _, _ in U[druh]}
    if rok in roky:
        return "doloženo", None
    if re.search(r"\b%d\b" % rok, text):
        return "nevim", sorted(roky)
    return "rozpor", sorted(roky)


# ── Wikidata ────────────────────────────────────────────────────────────────
_DATA_P = {"narozeni": ["P569"], "umrti": ["P570"], "zalozeni": ["P571"],
           "premiera": ["P577"], "otevreni": ["P1619", "P571"]}
_VZTAHY = [
    (r"režír\w*|režisér\w*|režie", ["P57"]),
    (r"\bsyn(?:em)?\b|\bdcer\w*|\botc\w*|\botec\b", ["P22", "P25"]),
    (r"\bmatk\w*", ["P25"]),
    (r"narodil\w*\s+(?:se\s+)?(?:\S+\s+){0,4}?(?:ve?|ve městě)\b|rodák", ["P19"]),
    (r"zemřel\w*\s+(?:\S+\s+){0,4}?(?:ve?)\b", ["P20"]),
    (r"\bkraj\w*|ve správě|leží\s+v\w*|nachází\s+se\s+v\w*", ["P131"]),
    (r"\bautor\w*|napsal\w*", ["P50"]),
    (r"manžel\w*|\bžen(?:ou|a)\b", ["P26"]),
    (r"architekt\w*|stavitel\w*|mistr\w*|řízen\w*|navrhl\w*|návrhář\w*|projektoval\w*", ["P84"]),
    (r"založil\w*|zakladatel\w*", ["P112"]),
    (r"složil\w*|zkomponoval\w*|skladatel\w*", ["P86"]),
    (r"adaptac\w*|předloh\w*|na motivy|podle\s+(?:stejnojmenn\w+\s+)?(?:román\w*|knih\w*|povídk\w*)",
     ["P144"]),
    (r"\brodu\b|dynasti\w*|Přemyslovc\w*|Lucemburk\w*|Habsburk\w*", ["P53"]),
    (r"\bhrál\w*|\bhrají\b|v hlavní roli|obsazen\w*", ["P161"]),
]
# HANS_OVERENI_ROZSIRENI_V1 (17. 10.) — číselné údaje s tolerancí
_MNOZSTVI = [   # (klíčové slovo, vlastnost, relativní tolerance)
    (r"vysok\w*|výšk\w*|nadmořsk\w*|měří", "P2044", 0.02),
    (r"obyvatel\w*", "P1082", 0.15),
    (r"rozloh\w*", "P2046", 0.05),
    (r"dlouh\w*|délk\w*", "P2043", 0.05),
]
_VSE_P = sorted({p for ps in _DATA_P.values() for p in ps} | {p for _, ps in _VZTAHY for p in ps}
                | {p for _, p, _ in _MNOZSTVI})
_MISTA_P = ("P131", "P19", "P20")
_cache = None


def _fold(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", (s or "").lower())
                   if unicodedata.category(c) != "Mn")


def _c() -> dict:
    global _cache
    if _cache is None:
        try:
            _cache = json.load(open(CACHE, encoding="utf-8"))
        except Exception:
            _cache = {}
        if _cache.get("_v") != _CACHE_V:      # nové vlastnosti → načíst znovu
            _cache = {"_v": _CACHE_V}
    return _cache


_CACHE_V = 2   # HANS_OVERENI_ROZSIRENI_V1: P112, P86, P144(+P50), množství


def _uloz():
    try:
        tmp = CACHE + ".tmp"
        json.dump(_c(), open(tmp, "w", encoding="utf-8"), ensure_ascii=False)
        os.replace(tmp, CACHE)
    except Exception as e:
        _log.debug("HANS_OVERENI_STIN_V1: mezipaměť: %s", e)


def _wd(url: str) -> dict:
    from scripts.hans_facts import _get
    return _get(url) or {}


def _nacti_wd(tituly: list) -> None:
    """Tvrzení k cs titulům + řetěz nadřazených míst + jména odkazů."""
    C = _c()
    chybi = [t for t in dict.fromkeys(tituly) if t and t not in C]
    odkazy = set()
    if chybi:
        d = _wd(API + "?action=wbgetentities&sites=cswiki&props=claims|sitelinks"
                "&sitefilter=cswiki&format=json&titles=" + urllib.parse.quote("|".join(chybi)))
        nalez = {}
        for q, e in (d.get("entities") or {}).items():
            if not q.startswith("Q"):
                continue
            t = ((e.get("sitelinks") or {}).get("cswiki") or {}).get("title")
            P = {}
            for p in _VSE_P:
                for cl in (e.get("claims") or {}).get(p, []):
                    if (cl.get("rank") or "normal") == "deprecated":
                        continue
                    v = ((cl.get("mainsnak") or {}).get("datavalue") or {}).get("value")
                    if isinstance(v, dict) and v.get("id"):
                        P.setdefault(p, []).append(v["id"])
                        odkazy.add(v["id"])
                    elif isinstance(v, dict) and v.get("amount"):
                        try:
                            P.setdefault(p, []).append(float(v["amount"]))
                        except ValueError:
                            pass
                    elif isinstance(v, dict) and v.get("time"):
                        m = re.match(r"[+-]0*(\d+)-(\d\d)-(\d\d)", v["time"])
                        if m:
                            P.setdefault(p, []).append([int(m.group(1)), int(m.group(2)),
                                                        int(m.group(3))])
            nalez[t] = {"qid": q, "p": P}
        for t in chybi:
            C[t] = nalez.get(t, {"qid": None, "p": {}})
    predlohy = [q for t in tituly for q in ((C.get(t) or {}).get("p") or {}).get("P144", [])
                if isinstance(q, str) and "A:" + q not in C][:45]
    if predlohy:
        d = _wd(API + "?action=wbgetentities&props=claims&format=json&ids=" + "|".join(predlohy))
        for q, e in (d.get("entities") or {}).items():
            C["A:" + q] = [((cl.get("mainsnak") or {}).get("datavalue") or {}).get("value", {}).get("id")
                           for cl in (e.get("claims") or {}).get("P50", [])
                           if isinstance(((cl.get("mainsnak") or {}).get("datavalue") or {}).get("value"), dict)]
            C["A:" + q] = [x for x in C["A:" + q] if x]
            odkazy |= set(C["A:" + q])
        for q in predlohy:
            C.setdefault("A:" + q, [])
    mista = {q for t in tituly for p in _MISTA_P
             for q in ((C.get(t) or {}).get("p") or {}).get(p, []) if isinstance(q, str)}
    for _ in range(4):
        nova = [q for q in mista if "U:" + q not in C][:45]
        if not nova:
            break
        d = _wd(API + "?action=wbgetentities&props=claims&format=json&ids=" + "|".join(nova))
        for q, e in (d.get("entities") or {}).items():
            C["U:" + q] = [((cl.get("mainsnak") or {}).get("datavalue") or {}).get("value", {}).get("id")
                           for cl in (e.get("claims") or {}).get("P131", [])
                           if isinstance(((cl.get("mainsnak") or {}).get("datavalue") or {}).get("value"), dict)]
            C["U:" + q] = [x for x in C["U:" + q] if x]
        for q in nova:
            C.setdefault("U:" + q, [])
        mista = {r for q in nova for r in C.get("U:" + q, [])}
    for t in tituly:
        for p in _MISTA_P:
            for q in ((C.get(t) or {}).get("p") or {}).get(p, []):
                if isinstance(q, str):
                    odkazy |= set(_predci(q))
    odkazy = [q for q in odkazy if "L:" + q not in C]
    for i in range(0, len(odkazy), 45):
        d = _wd(API + "?action=wbgetentities&props=labels|aliases&languages=cs|en"
                "&format=json&ids=" + "|".join(odkazy[i:i + 45]))
        for q, e in (d.get("entities") or {}).items():
            jm = [x.get("value") for x in (e.get("labels") or {}).values()]
            for al in (e.get("aliases") or {}).values():
                jm += [x.get("value") for x in al]
            C["L:" + q] = [j for j in jm if j]
    _uloz()


def _predci(q, hloubka=5):
    C = _c()
    out, akt = [q], [q]
    for _ in range(hloubka):
        akt = [r for x in akt for r in C.get("U:" + x, []) if r not in out]
        out += akt
    return out


def _jm(q):
    """Jména entity; má-li víceslovné, jednoslovné aliasy („Matyáš“) se
    vynechají — samo křestní jméno doložilo „Matyáš z Kolova“."""
    j = _c().get("L:" + q) or []
    vic = [x for x in j if len(x.split()) > 1]
    return vic or j


def _shoda_jmena(ve_vete, jmena_wd, misto: bool = False) -> bool:
    """Shoda začátků slov (5 zn); u víceslovného jména osoby rozhoduje poslední
    slovo (Martina Campbellová ≠ Martin Campbell), u místa jméno bez přívěsku
    („Všebořice u Dolních Kralovic“ → všechna slova před „u“); „-ová“ ×
    mužské jméno neplatí."""
    sv = [w for w in re.findall(r"\w+", _fold(ve_vete)) if len(w) >= 3]
    if not sv:
        return None   # HANS_OVERENI_ODECET_V1 — useknuté jméno („Josef Ab“): nevím
    import difflib
    for j in jmena_wd:
        if misto:
            j = re.split(r"\s+(?:u|nad|pod|na|v|ve)\s+|\s*\(", j)[0]
        jw = [w for w in re.findall(r"\w+", _fold(j)) if len(w) >= 3]
        jw = jw if misto else jw[-1:]
        for a in sv:
            for b in jw:
                n = min(5, len(a), len(b))
                if a[:n] == b[:n] and not (a.endswith("ova") and not b.endswith("ova")):
                    return True
                # HANS_OVERENI_ODECET_V1 (19. 10.) — místo v jiném pádě
                # („Praze“ × Praha, „Vídni“ × Vídeň, „Woli“ × Wola) a překlep
                # jména („Parronaud“ × Paronnaud)
                if misto and len(a) >= 4 and len(b) >= 4:
                    k = max(3, min(len(a), len(b)) - 2)
                    if a[:k] == b[:k]:
                        return True
                if (len(a) >= 6 and len(b) >= 6
                        and difflib.SequenceMatcher(None, a, b).ratio() >= 0.85):
                    return True
    return False


_JMENA_ZA = re.compile(r"((?:[A-ZÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ][\wáčďéěíňóřšťúůýž.\-]+[ ,]*(?:a |z |ze |of |von |de )?){1,5})")
_RODY = re.compile(r"Přemyslovc\w*|Lucemburk\w*|Habsburk\w*")


def over_wd(veta, udaje, tituly) -> list:
    det = []
    for t in tituly:
        e = _c().get(t) or {}
        P = e.get("p") or {}
        if not e.get("qid"):
            continue
        for u in udaje:
            if u[0] == "vek" and P.get("P569") and P.get("P570"):
                vek = {d[0] - n[0] for d in P["P570"] for n in P["P569"]}
                ok = any(abs(u[1] - v) <= 1 for v in vek)
                det.append({"clanek": t, "p": "P570-P569", "udaj": u[4],
                            "verdikt": "doloženo" if ok else "rozpor", "wd": sorted(vek)})
                continue
            for p in _DATA_P.get(u[0] or "", []):
                if p in P:
                    roky = {x[0] for x in P[p]}
                    det.append({"clanek": t, "p": p, "udaj": u[4],
                                "verdikt": "doloženo" if u[1] in roky else "rozpor",
                                "wd": sorted(roky)})
                    break
        for vzor, props in _VZTAHY:
            for m in re.finditer(vzor, veta, re.I):
                if not any(p in P for p in props):
                    continue
                jm = (_JMENA_ZA.search(veta[m.end(): m.end() + 70])
                      if not _RODY.fullmatch(m.group(0)) else None)
                ve_vete = m.group(0) if jm is None and props == ["P53"] else (jm.group(1) if jm else "")
                if not ve_vete.strip():
                    continue
                # podmět, ne předmět: entita jmenovaná ve vztahu se nekontroluje
                if _fold(t.split(" (")[0])[:5] in _fold(ve_vete):
                    continue
                # HANS_OVERENI_ODECET_V1 — článek o jménu samém („Přemyslovců“ →
                # Anežka Přemyslovna) není podmět věty
                if any(w[:5] in _fold(ve_vete)
                       for w in re.findall(r"\w{5,}", _fold(t.split(" (")[0]))):
                    continue
                # HANS_OVERENI_ODECET_V1 — u místa VŠECHNA jména (jen víceslovná
                # nechala z Vídně „Vienna, Austria“ a „ve Vídni“ byl rozpor)
                hodnoty = [j for p in props for q0 in P.get(p, []) if isinstance(q0, str)
                           for q in (_predci(q0) if p in _MISTA_P
                                     else [q0] + _c().get("A:" + q0, []) if p == "P144"
                                     else [q0])
                           for j in ((_c().get("L:" + q) or []) if p in _MISTA_P else _jm(q))]
                if not hodnoty:
                    continue
                _misto = any(p in _MISTA_P for p in props)
                _sh = _shoda_jmena(ve_vete, hodnoty, _misto)
                # HANS_OVERENI_ODECET_V1 — správní celek (P131) neodliší pohoří,
                # geopark ani přívlastek → u něj jen doložení, rozpor ne
                if _sh is None or (not _sh and props == ["P131"]):
                    continue
                det.append({"clanek": t, "p": props, "udaj": ve_vete.strip(" ,"),
                            "verdikt": ("doloženo" if _sh else "rozpor"),
                            "wd": [_jm(q)[0] for p in props for q in P.get(p, [])
                                   if isinstance(q, str) and _jm(q)][:4]})
        for vzor, p, tol in _MNOZSTVI:
            if not P.get(p) or not re.search(vzor, veta, re.I):
                continue
            for x in _CISLA.finditer(veta):
                n = _cislo(x)
                if n is None or 1000 <= n <= 2100 and not x.group(3):
                    continue       # letopočet, ne údaj
                vals = [float(v) for v in P[p] if isinstance(v, (int, float))]
                ok = any(abs(n * k - v) <= tol * v for v in vals for k in (1, 1000, 0.001))
                det.append({"clanek": t, "p": p, "udaj": x.group(0).strip(),
                            "verdikt": "doloženo" if ok else "rozpor", "wd": vals[:3]})
    return det


_CISLA = re.compile(r"(\d{1,3}(?:[ \u00a0]\d{3})+|\d+)(?:,(\d+))?\s*"
                    r"(tisíc\w*|milion\w*|metr\w*|m\b|km\w*|kilometr\w*)?", re.I)


def _cislo(m):
    try:
        n = float(re.sub(r"[ \u00a0]", "", m.group(1)) + ("." + m.group(2) if m.group(2) else ""))
    except ValueError:
        return None
    jed = (m.group(3) or "").lower()
    if jed.startswith("tisíc"):
        n *= 1000
    elif jed.startswith("milion"):
        n *= 1_000_000
    return n


# ── lokální vyhledání přes produkční brány, bez sítě ─────────────────────────
class _Odp:
    status_code = 200

    def __init__(self, d):
        self._d = d

    def json(self):
        return self._d


def _jen_lokalne(url, params=None, **kw):
    from scripts import hans_wiki_offline as wo
    p = params or {}
    if "cs.wikipedia.org" in url and p.get("list") == "prefixsearch":
        return _Odp({"query": {"prefixsearch": [
            {"title": t} for t in wo.prefixsearch(p.get("pssearch") or "", int(p.get("pslimit") or 3))]}})
    if "cs.wikipedia.org" in url and p.get("list") == "search":
        return _Odp({"query": {"search": [
            {"title": t} for t in wo.srsearch(p.get("srsearch") or "", int(p.get("srlimit") or 5))]}})
    raise RuntimeError("stín ověřování nejde na síť")


_OPRAVA_POPIS = {"P569": "uveden rok narození", "P570": "uveden rok úmrtí",
                 "P571": "uveden rok založení či začátku stavby", "P50": "uveden autor",
                 "P57": "uvedena režie"}
_UVOD_DOHL = re.compile(r"Podle toho, co jsem te\u010f na\u0161el \(Wikipedie \u2014 heslo "
                        r"[\u201e\"]([^\u201c\"]{2,90})[\u201c\"]\):?")
_DRUH_ZAVORKA = {"film", "kniha", "roman", "serial", "novela", "povidka", "album", "pisen",
                 "opera", "hrad", "zamek", "mesto", "obec", "hora", "reka", "hudebnik",
                 "spisovatel", "malir", "herec", "politik", "skladatel", "kostel", "chram"}
_TRIGGER = re.compile("|".join([v for v, _ in _VZTAHY] + [v for v, _, _ in _MNOZSTVI]), re.I)
_UVOZ = re.compile(r"[„\"]([^“\"]{3,90})[“\"]")


def _overit(h, otazka, prepis, odpoved) -> list:
    from scripts import hans_wiki_offline as wo
    from scripts import hans_podklad as P
    from scripts.web_reader import WebReader
    vety = [v.strip() for v in _vety(odpoved)
            if v.strip() and (_DATUM.search(v) or _TRIGGER.search(v) or _VEK.search(v))]
    if not vety:
        return []
    try:
        pr = P.predmet(h, otazka, prepis)[1]
    except Exception:
        pr = None
    wr = WebReader(h.config)
    wr._get = _jen_lokalne
    clanky = {}

    def _clanek(k):
        if k not in clanky:
            t = None
            try:
                t = wr._wikipedia_search(k)
            except Exception:
                pass
            text, roz, kan = wo.extrakt(t) if t else ("", False, None)
            clanky[k] = (kan, text) if text else None
        return clanky[k]
    radky = []
    for v0 in vety:
        # HANS_OVERENI_ODECET_V1 (19. 10.) — úvod dohledání „Podle toho, co jsem
        # teď našel (Wikipedie — heslo „X“):“ není údaj; X je článek předmětu,
        # „Wikipedie“ kandidát není (dřív „autor Spalovače = Jimmy Wales“)
        _uv = _UVOD_DOHL.search(v0)
        v = v0[_uv.end():].strip() if _uv else v0
        _hes = [_uv.group(1)] if _uv else []
        kand = list(dict.fromkeys(_hes + ([pr] if pr else []) + _UVOZ.findall(v) + [
            k for k in jmena(v) if not P.osloveni(k, v) and k != "Wikipedie"]))[:3]
        cl = [c for c in (_clanek(k) for k in kand) if c and not c[0].startswith("Seznam ")]
        cl = list({c[0]: c for c in cl}.values())
        radky.append({"veta": v0, "kand": kand, "cl": cl,
                      "ud": udaje_vety(v, prepis or otazka)})
    try:
        _nacti_wd([c[0] for r in radky for c in r["cl"]])
    except Exception as e:
        _log.debug("HANS_OVERENI_STIN_V1: Wikidata nedostupná: %s", e)
    out = []
    for r in radky:
        txt = []
        for kan, text in r["cl"]:
            U = udaje_clanku(text, wo.infobox(kan)[1], kan)
            for u in r["ud"]:
                vd, hod = porovnej(u, U, text)
                txt.append({"clanek": kan, "udaj": u[4], "druh": u[0], "verdikt": vd, "pramen": hod})
        wd = over_wd(r["veta"], r["ud"], [c[0] for c in r["cl"]])
        # článek s číslem v názvu, které věta nemá („Praha 4“ z „v Praze“),
        # je jiný předmět → jeho rozpor se nepočítá (/tazatel 17. 10.)
        cizi = {kan for kan, _ in r["cl"]
                if any(c not in r["veta"] for c in re.findall(r"\b\d+\b", kan))}
        # datum/číslo smí dát rozpor jen článek, jehož předmět věta nebo otázka
        # jmenuje („Byla založena 1348…“ × vznik Česka, otázka na univerzitu)
        _kontext = _fold(r["veta"] + " " + otazka + " " + prepis)
        for kan, _ in r["cl"]:
            sl = [w for w in re.findall(r"\w{4,}", _fold(re.sub(r"\(.*?\)", "", kan)))]
            # HANS_OVERENI_ODECET_V1 — datum jen z článku, jehož titul věta či
            # otázka jmenuje VĚTŠINOU slov („Marie Tereza Luisa Savojská“ ×
            # Marie Terezie: 2/4 → cizí); dřív stačilo jedno slovo
            # (výčet za čárkou se nepočítá: „Katedrála svatého Víta, Václava
            # a Vojtěcha“ × „katedrála sv. Víta“)
            sl1 = [w for w in re.findall(r"\w{4,}", _fold(re.sub(r"\(.*?\)", "", kan).split(",")[0]))]
            if sl1 and 2 * sum(w[:5] in _kontext for w in sl1) <= len(sl1):
                cizi.add(kan + "\x00datum")
            # upřesnění v závorce, které věta ani otázka nezná („(Kazaň)“ u
            # chrámu v Londýně, „Národní (Praha)“ u národního parku) = jiný předmět
            _zav = re.findall(r"\w{4,}", _fold(" ".join(re.findall(r"\((.*?)\)", kan))))
            if any(w not in _DRUH_ZAVORKA and w[:5] not in _kontext for w in _zav):
                cizi.add(kan)
        _dat = lambda x: not isinstance(x.get("p"), list)      # datum/číslo, ne jméno
        txt = [x for x in txt if not (x["verdikt"] == "rozpor" and (
            x["clanek"] in cizi or x["clanek"] + "\x00datum" in cizi))]
        # HANS_OVERENI_ODECET_V1 — článek, jehož titul věta ani otázka většinou
        # nejmenuje, nedá rozpor ani u jména (Marie Tereza Savojská × „ve Vídni“)
        wd = [x for x in wd if not (x["verdikt"] == "rozpor" and (
            x["clanek"] in cizi or x["clanek"] + "\x00datum" in cizi))]
        dol = {x["udaj"] for x in txt + wd if x["verdikt"] == "doloženo"}

        def _v(det):
            if any(x["verdikt"] == "rozpor" and x["udaj"] not in dol for x in det):
                return "rozpor"
            return "doloženo" if any(x["verdikt"] == "doloženo" for x in det) else "nevim"
        # ⛔ signál „jméno z věty chybí v článku o předmětu“ ZMĚŘEN 17. 10. a
        # vyřazen: 0 skutečných chyb, 3 plané (oslovení za dvojtečkou,
        # „Středočeském“, pravdivý Bernini, kterého článek nezmiňuje)
        # HANS_OVERENI_OPRAVA_V1 (19. 10.) — návrh opravy jen z článku o PŘEDMĚTU
        # (první kandidát = heslo dohledání / předmět otázky), jen úzký výčet
        # údajů a jen jednoznačná hodnota Wikidat
        _hl = clanky.get(r["kand"][0]) if r["kand"] else None
        _hl = _hl[0] if _hl else None
        oprava = None
        for x in wd:
            if x["verdikt"] != "rozpor" or x["clanek"] != _hl or x["udaj"] in dol:
                continue
            _p = x["p"] if isinstance(x["p"], str) else (x["p"][0] if len(x["p"]) == 1 else "")
            if _p in _OPRAVA_POPIS and len(x.get("wd") or []) == 1:
                oprava = {"p": _p, "clanek": _hl, "udaj": str(x["udaj"]).strip(" .,"),
                          "spravne": str(x["wd"][0]),
                          "text": "Ještě oprava: podle Wikidat je u „%s“ %s %s, ne %s." % (
                              _hl, _OPRAVA_POPIS[_p], x["wd"][0], str(x["udaj"]).strip(" .,"))}
                break
        out.append({"veta": r["veta"], "kandidati": r["kand"], "clanky": [c[0] for c in r["cl"]],
                    "oprava": oprava,
                    "text": _v(txt), "wikidata": _v(wd), "obe": _v(txt + wd),
                    "detail": [x for x in txt + wd if x["verdikt"] != "nevim"]})
    return out


def _beh(h, kdo, otazka, prepis, odpoved, cesta):
    from scripts import hans_wiki_offline as wo
    if not wo.dostupna():
        return
    t0 = time.time()
    with _zamek:
        try:
            vysl = _overit(h, otazka, prepis, odpoved)
        except Exception as e:
            _log.debug("HANS_OVERENI_STIN_V1: selhalo: %s", e)
            return
    if not vysl:
        return
    testy = {str(x).lower() for x in (h.config.get("test_persons") or [])}
    kp = {str(k).lower() for k in (h.config.get("known_persons") or {})}
    trida = ("test" if kdo.lower() in testy else "domacnost" if kdo.lower() in kp else "jiny")
    roz = [v for v in vysl if v["obe"] == "rozpor"]
    # HANS_OVERENI_OPRAVA_V1 — zatím JEN do logu (týden sledovat plané), nic
    # se neposílá (vypíná se s celým stínem `overeni.stin`). 📏 označené sady
    # 19. a 16. 10.: 5 návrhů, všech 5 u skutečné chyby, 0 u pravdy
    for v in vysl:
        if v.get("oprava"):
            _log.info("HANS_OVERENI_OPRAVA_V1 [%s, jen log]: %s", trida, v["oprava"]["text"])
    _log.info("HANS_OVERENI_STIN_V1: %d vět, rozpor %d, doloženo %d (%s, %.1f s)%s",
              len(vysl), len(roz), sum(v["obe"] == "doloženo" for v in vysl), cesta,
              time.time() - t0, (" — " + roz[0]["veta"][:80]) if roz else "")
    try:
        with open(SOUBOR, "a", encoding="utf-8") as f:
            f.write(json.dumps({"t": time.strftime("%Y-%m-%d %H:%M:%S"), "trida": trida,
                                "cesta": cesta, "otazka": otazka, "prepis": prepis,
                                "vety": vysl}, ensure_ascii=False) + "\n")
    except Exception as e:
        _log.debug("HANS_OVERENI_STIN_V1: zápis: %s", e)


def stin(h, ctx) -> None:
    """Spusť ověření odpovědi na pozadí; odpověď ani rozhovor nijak nemění."""
    if not bool((h.config.get("overeni", {}) or {}).get("stin", True)):
        return
    odpoved = str(getattr(ctx, "response", "") or "")
    if not odpoved:
        return
    otazka = str(getattr(ctx, "_raw_message", "") or getattr(ctx, "user_message", "") or "")
    prepis = str(getattr(h, "_f1_query", None) or "")
    cesta = "%s<%s" % (getattr(h, "_grounding_outcome", "") or "",
                       getattr(h, "_grounding_cesta", "") or "")
    threading.Thread(target=_beh, args=(h, str(getattr(ctx, "name", "") or ""), otazka,
                                        prepis, odpoved, cesta),
                     daemon=True, name="overeni_stin").start()
