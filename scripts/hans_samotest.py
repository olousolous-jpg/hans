"""HANS_SAMOTEST_V1 (23. 10.) — noční samotestování faktických odpovědí.

Koláč se přes den (10–12 h) ptá na osoby, díla a stavby, ke kterým mají Wikidata údaj
(narození, úmrtí, místo, autor, režie, architekt). Hans odpovídá BĚŽNOU
CHATOVOU CESTOU pod testovací identitou (jako Koláčova zkouška, historie se
po každé otázce maže). Správnou odpověď určí Wikidata, hodnocení je kódem:

    spravne  — očekávaný rok / jméno v odpovědi je
    nevi     — odpověď přizná, že neví, a údaj neuvede
    chybne   — údaj v odpovědi není (jiný rok, jiné jméno…)

Zdroje otázek půl na půl: co Hans zná (entity z četby) a náhodná hesla
lokální Wikipedie s infoboxem (osoba, film, budova…). Výsledky po řádcích do
`data/mereni/samotest/<datum>.jsonl`, ranní souhrn: `python3 -m
scripts.hans_samotest souhrn [datum]`.

Proč: ruční testovací sady míří jen na témata, která nás napadnou, a
opravy z nich jsou lokální (uživatel 23. 10.: „stále dokola testovací
rozhovory“). Config `samotest`: enabled, pocet (za noc), od, do (hodiny).
"""
from __future__ import annotations

import json
import logging
import os
import random
import re
import sqlite3
import time
import unicodedata
from datetime import datetime
from typing import Optional

_log = logging.getLogger(__name__)

ADRESAR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "data", "mereni", "samotest")
WIKI_DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "data", "wiki", "cswiki.db")

# infobox lokální Wikipedie → druh předmětu
_IB_OSOBA = ("osoba", "politik", "panovník", "umělec", "hudební umělec", "spisovatel",
             "vědec", "herec", "šlechtic", "duchovní", "sportovec", "fotbalista")
_IB_DILO = ("film", "kniha", "literární dílo", "opera")
_IB_STAVBA = ("budova", "kostel", "hrad", "zámek", "most", "stavba")

_NEVI = re.compile(r"nemám\s+(?:o\s+tom\s+)?spolehliv|nevím|nemám\s+(?:o\s+tom\s+)?(?:žádn|záznam)"
                   r"|nejsem\s+si\s+jist|nerad\s+bych\s+si\s+domýšlel|nenašel\s+jsem", re.I)


def _cfg(config: dict) -> dict:
    return (config or {}).get("samotest", {}) or {}


def _fold(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", (s or "").lower())
                   if unicodedata.category(c) != "Mn")


def _soubor(datum: Optional[str] = None) -> str:
    return os.path.join(ADRESAR, "%s.jsonl" % (datum or datetime.now().strftime("%Y-%m-%d")))


def dnes_pocet() -> int:
    try:
        with open(_soubor(), encoding="utf-8") as f:
            return sum(1 for _ in f)
    except FileNotFoundError:
        return 0


def je_na_rade(config: dict) -> bool:
    """V hodinách `od`–`do` (uživatel 23. 10.: přes den 10–12, noc je plná),
    mimo okno hodinového překladu zpráv (:13–:26, model by se přetahoval
    o grafiku), dokud dnes neproběhlo `pocet` otázek."""
    c = _cfg(config)
    if not bool(c.get("enabled", False)):
        return False
    n = datetime.now()
    od, do = int(c.get("od", 10)), int(c.get("do", 12))
    if not (od <= n.hour < do) or 13 <= n.minute <= 26:
        return False
    return dnes_pocet() < int(c.get("pocet", 12))


# ── výběr předmětu ──────────────────────────────────────────────────────────
def _zname(db_path: str) -> list:
    """(titul, druh) entit z Hansovy četby."""
    try:
        c = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True, timeout=5)
        try:
            rows = c.execute("SELECT COALESCE(NULLIF(source_title,''), name), etype FROM entities "
                             "WHERE etype IN ('osoba','dílo','místo')").fetchall()
        finally:
            c.close()
    except Exception as e:
        _log.debug("HANS_SAMOTEST_V1: entity: %s", e)
        return []
    druh = {"osoba": "osoba", "dílo": "dilo", "místo": "stavba"}
    return [(str(t), druh[e]) for t, e in rows if t]


def _nahodne(n: int = 30) -> list:
    """(titul, druh) náhodných hesel lokální Wikipedie s infoboxem."""
    ib = _IB_OSOBA + _IB_DILO + _IB_STAVBA
    try:
        c = sqlite3.connect("file:%s?mode=ro" % WIKI_DB, uri=True, timeout=5)
        try:
            mx = c.execute("SELECT max(id) FROM clanek").fetchone()[0] or 0
            out = []
            for _ in range(n * 40):
                if len(out) >= n:
                    break
                r = c.execute("SELECT titul, infobox_druh FROM clanek WHERE id >= ? AND "
                              "COALESCE(rozcestnik,0)=0 LIMIT 1",
                              (random.randint(1, mx),)).fetchone()
                if r and r[1] in ib:
                    out.append((r[0], "osoba" if r[1] in _IB_OSOBA
                                else "dilo" if r[1] in _IB_DILO else "stavba"))
            return out
        finally:
            c.close()
    except Exception as e:
        _log.debug("HANS_SAMOTEST_V1: wiki: %s", e)
        return []


def _jmeno(O, q: str) -> str:
    return (O._c().get("LC:" + q) or (O._jm(q) or [""])[0] or "").strip()


def _otazky(titul: str, druh: str) -> list:
    """[(otázka, vlastnost, očekávané hodnoty)] k předmětu podle Wikidat."""
    from scripts import hans_overeni as O
    O._nacti_wd([titul])
    P = (O._c().get(titul) or {}).get("p") or {}
    if not P:
        return []
    # u osob pryč „(fotbalista)“, u staveb a děl upřesnění zůstane
    # („Evangelický kostel (Krnov)“), jen druh díla ne („(film)“)
    jm = (re.sub(r"\s*\([^)]*\)$", "", titul) if druh == "osoba"
          else re.sub(r"\s*\((?:film|kniha|román|opera|seriál|album|píseň)[^)]*\)$", "", titul))
    out = []
    if druh == "osoba":
        zena = "Q6581072" in (P.get("P21") or [])
        a = "a" if zena else ""
        for p, sablona in (("P569", "Kdy se narodil%s %s?"), ("P570", "Kdy zemřel%s %s?")):
            roky = sorted({x[0] for x in P.get(p) or [] if isinstance(x, list)})
            if roky:
                out.append((sablona % (a, jm), p, [str(r) for r in roky]))
        for p, sablona in (("P19", "Kde se narodil%s %s?"), ("P20", "Kde zemřel%s %s?")):
            mista = [_jmeno(O, q) for q in P.get(p) or [] if isinstance(q, str)]
            mista = [m for m in mista if m]
            if mista:
                out.append((sablona % (a, jm), p, mista))
    else:
        # HANS_SAMOTEST_DRUH_V1 (24. 10.) — „Kdo režíroval film „Draci v hrnci“?“
        # u televizního pořadu; druh z infoboxu lokálního článku
        _ib = ""
        try:
            from scripts import hans_wiki_offline as _wo
            _ib = (_wo.infobox(titul)[0] or "").lower()
        except Exception:
            pass
        _rez = ("Kdo režíroval film „%s“?" if _ib == "film" else
                "Kdo režíroval pořad „%s“?" if "pořad" in _ib else
                "Kdo režíroval díl „%s“?" if "díl seriálu" in _ib else "Kdo režíroval „%s“?")
        for p, sablona in (("P50", "Kdo napsal dílo „%s“?"), ("P57", _rez),
                           ("P84", "Kdo navrhl stavbu „%s“?"), ("P86", "Kdo složil hudbu k dílu „%s“?")):
            jmena = [_jmeno(O, q) for q in P.get(p) or [] if isinstance(q, str)]
            jmena = [j for j in jmena if j]
            if jmena:
                out.append((sablona % jm, p, jmena))
        for p, sablona in (("P571", "Z kterého roku pochází „%s“?"), ("P577", "Kdy vyšlo dílo „%s“?")):
            roky = sorted({x[0] for x in P.get(p) or [] if isinstance(x, list)})
            if roky:
                out.append((sablona % jm, p, [str(r) for r in roky]))
    return out


def vyber(config: dict, db_path: str) -> Optional[dict]:
    """Jedna otázka: předmět střídavě ze známých entit a z náhodných hesel."""
    hotove = set()
    try:
        with open(_soubor(), encoding="utf-8") as f:
            hotove = {json.loads(l).get("titul") for l in f}
    except Exception:
        pass
    zdroj = "zname" if dnes_pocet() % 2 == 0 else "nahodne"
    kand = _zname(db_path) if zdroj == "zname" else _nahodne()
    random.shuffle(kand)
    for titul, druh in kand[:25]:
        if titul in hotove:
            continue
        try:
            q = _otazky(titul, druh)
        except Exception as e:
            _log.debug("HANS_SAMOTEST_V1: %r: %s", titul, e)
            continue
        if q:
            otazka, p, ocek = random.choice(q)
            return {"titul": titul, "druh": druh, "zdroj": zdroj, "otazka": otazka,
                    "vlastnost": p, "ocekavano": ocek}
    return None


# ── hodnocení ───────────────────────────────────────────────────────────────
def ohodnot(odpoved: str, polozka: dict) -> str:
    f = _fold(odpoved)
    ok = False
    for h in polozka.get("ocekavano") or []:
        if re.fullmatch(r"\d{3,4}", h):
            ok = ok or bool(re.search(r"(?<!\d)%s(?!\d)" % h, odpoved))
        else:
            # jméno / místo: příjmení (poslední slovo ≥ 4 zn.) nebo celé jméno, kmenem
            slova = [w for w in re.findall(r"\w+", _fold(re.sub(r"\(.*?\)", "", h))) if len(w) >= 4]
            if slova and polozka.get("vlastnost") in ("P19", "P20"):
                ok = ok or any(w[:5] in f for w in slova)   # místo: kterékoli slovo
            elif slova:
                ok = ok or slova[-1][:5] in f
    if ok:
        return "spravne"
    return "nevi" if _NEVI.search(odpoved or "") else "chybne"


def zapis(polozka: dict, odpoved: str, verdikt: str, cesta: str, cas: float) -> None:
    os.makedirs(ADRESAR, exist_ok=True)
    with open(_soubor(), "a", encoding="utf-8") as f:
        f.write(json.dumps(dict(polozka, odpoved=odpoved, verdikt=verdikt, cesta=cesta,
                                oprava="Ještě oprava:" in (odpoved or ""), cas=round(cas, 1),
                                t=datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
                           ensure_ascii=False) + "\n")


def proved(config: dict, handler, db_path: str) -> bool:
    """Jedna otázka samotestu. True = proběhla (místo dialogu)."""
    from scripts import kolac_exam as ke
    polozka = vyber(config, db_path)
    if not polozka:
        _log.info("HANS_SAMOTEST_V1: žádný předmět s údaji z Wikidat")
        return False
    jmeno = ke.identita(config)
    t0 = time.time()
    try:
        odpoved = handler.send_chat_message(jmeno, polozka["otazka"], channel="zkouska") or ""
    except Exception as e:
        _log.warning("HANS_SAMOTEST_V1: odpověď selhala: %s", e)
        return False
    finally:
        try:
            handler.conv_store.clear(jmeno)
        except Exception:
            pass
    if not odpoved.strip():
        _log.warning("HANS_SAMOTEST_V1: prázdná odpověď na %r", polozka["otazka"])
        return False
    verdikt = ohodnot(odpoved, polozka)
    cesta = "%s<%s" % (getattr(handler, "_grounding_outcome", "") or "",
                       getattr(handler, "_grounding_cesta", "") or "")
    zapis(polozka, odpoved, verdikt, cesta, time.time() - t0)
    _log.info("HANS_SAMOTEST_V1: [%s] %r → %s (%s)", polozka["zdroj"], polozka["otazka"],
              verdikt, cesta)
    return True


# ── souhrn ──────────────────────────────────────────────────────────────────
def souhrn(datum: Optional[str] = None) -> str:
    if not datum:      # poslední den, kdy samotest běžel
        try:
            dny = sorted(f[:-6] for f in os.listdir(ADRESAR) if f.endswith(".jsonl"))
            datum = dny[-1] if dny else None
        except FileNotFoundError:
            datum = None
    try:
        with open(_soubor(datum), encoding="utf-8") as f:
            R = [json.loads(l) for l in f if l.strip()]
    except FileNotFoundError:
        return "Samotest %s: žádný záznam." % (datum or "dnes")
    from collections import Counter
    v = Counter(r["verdikt"] for r in R)
    out = ["Samotest %s: %d otázek — správně %d, neví %d, chybně %d, opraveno dodatkem %d"
           % (datum or "dnes", len(R), v["spravne"], v["nevi"], v["chybne"],
              sum(1 for r in R if r.get("oprava")))]
    for klic in ("zdroj", "vlastnost"):
        c = Counter((r[klic], r["verdikt"]) for r in R)
        for k in sorted({r[klic] for r in R}):
            out.append("  %s %-8s správně %d · neví %d · chybně %d"
                       % (klic, k, c[(k, "spravne")], c[(k, "nevi")], c[(k, "chybne")]))
    cesty = Counter((r.get("cesta") or "?").split("<")[0] + "/" + r["verdikt"] for r in R)
    out.append("  cesty: " + ", ".join("%s %d" % kv for kv in sorted(cesty.items())))
    chyby = [r for r in R if r["verdikt"] == "chybne"]
    if chyby:
        out.append("Chybné:")
        for r in chyby:
            out.append("  - %s (čekáno %s; %s)\n      %s" % (
                r["otazka"], " / ".join(r["ocekavano"][:3]), r.get("cesta"),
                re.sub(r"\s+", " ", r["odpoved"])[:220]))
    return "\n".join(out)


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "souhrn":
        print(souhrn(sys.argv[2] if len(sys.argv) > 2 else None))
