# -*- coding: utf-8 -*-
"""KOLAC_EXAM_TRIDA_V1 (28. 10.) — třídění neúspěšných Koláčových zkoušek.

Zkouška z faktů se ptá na údaj, který si Hans SÁM uložil (`kolac_fakta`), takže
neúspěch je skoro vždy chyba cesty, ne neznalost (ruční rozbor 34 neúspěchů
za 30 dní: model 1, zbytek cesta, data nebo soudce). Třídič jen ČTE: tabulku
`kolac_exam`, strom myšlení zkoušky (`data/zkousky_stopy/<id>.json`,
KOLAC_EXAM_STOPA_V1) a u starších zkoušek provozní log.

Třídy:
  D  soudce — odpověď údaj obsahuje (číslo slovy, jiný tvar)
  B1 cesta — otázka šla mimo faktickou cestu (příkaz, názor, volný hovor, karta osoby…)
  B2 článek — dohledal se jiný článek, než o kterém je zkouška
  S  klíč/soud k posouzení — klíč v podkladu byl, odpověď se s ním shoduje jen zčásti
  C  model — klíč v podkladu byl, odpověď ho nemá
  B3 podklad — fakt je uložen, do podkladu se nedostal
  A  data — klíč není ani v uložených faktech (zkouška nebo fakt jsou vadné)

Použití: python3 -m scripts.kolac_exam_trida [dni] [--porovnej pravda.json]
"""
import datetime as _dt
import glob
import json
import os
import re
import sqlite3
import time
import unicodedata

DB = "data/hans_diary.db"
STOPY_DIR = "data/zkousky_stopy"
POPIS = {
    "D": "soudce: odpověď údaj má, verdikt chybný",
    "B1": "cesta: otázka šla mimo faktickou cestu",
    "B2": "článek: dohledal se jiný článek",
    "S": "klíč/soud k posouzení: shoda jen zčásti",
    "C": "model: klíč v podkladu byl, odpověď ho nemá",
    "B3": "podklad: uložený fakt se do podkladu nedostal",
    "A": "data: klíč není ani v uložených faktech",
}
_NEFAKT = ("nonfactual", "opinion", "self_state", "skip", None, "")
_NEFAKT_CESTY = ("volny_hovor", "nazor", "self_state", "person_card", "karta")


def _fold(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", (s or "").lower())
                   if unicodedata.category(c) != "Mn")


_RADOVE = {"prvni": 1, "druhy": 2, "treti": 3, "ctvrty": 4, "paty": 5, "sesty": 6,
           "sedmy": 7, "osmy": 8, "devaty": 9, "desaty": 10}
_DESITKY = {"dvacat": 20, "tricat": 30, "ctyricat": 40, "padesat": 50, "sedesat": 60,
            "sedmdesat": 70, "osmdesat": 80, "devadesat": 90}


def _cisla_slovy(text: str) -> str:
    """„třicátý druhý“ → „32“ (jen řadové desítky + jednotky; stačí na soud)."""
    f = _fold(text)

    def rep(m):
        d = next((v for k, v in _DESITKY.items() if m.group(1).startswith(k)), 0)
        j = next((v for k, v in _RADOVE.items() if (m.group(2) or "").startswith(k[:-1])), 0)
        return str(d + j) if d else m.group(0)
    return re.sub(r"\b(\w+?(?:at|ti))\w*(?:\s+(\w+))?", rep, f)


def _soud(text: str, klic: str, otazka: str = "") -> str:
    from scripts import kolac_fakta as _kf
    return _kf.soud(text or "", klic or "", otazka or "")


def _podil_klice(text: str, klic: str) -> float:
    o = _fold(text)
    toks = [t for t in re.findall(r"\w+", _fold(klic)) if t.isdigit() or len(t) >= 4]
    if not toks:
        return 0.0
    return sum(1 for t in toks if (t in o if t.isdigit() else t[:5] in o)) / len(toks)


def _stopa(exam_id: int):
    p = os.path.join(STOPY_DIR, "%d.json" % exam_id)
    if not os.path.exists(p):
        return None
    try:
        d = json.load(open(p, encoding="utf-8"))
    except Exception:
        return None
    v = d.get("volby") or {}
    pk = v.get("podklad") or {}
    return {"zdroj": "strom", "vysledek": pk.get("vysledek"), "cesta": pk.get("cesta") or "",
            "text": pk.get("text") or "", "prikaz": v.get("prikaz"),
            "agent": (v.get("agent") or {}).get("akce"), "f1": pk.get("f1"),
            "log": [str(x.get("zprava") if isinstance(x, dict) else x)
                    for k in (d.get("kroky") or []) for x in (k.get("log") or [])]
            + [str(x.get("zprava") if isinstance(x, dict) else x) for x in (d.get("log_mimo") or [])]}


_LOGY = None


def _logy():
    """Provozní log (i rotace) — řádky, které o cestě rozhodují, podle minuty."""
    global _LOGY
    if _LOGY is not None:
        return _LOGY
    klic = re.compile(r"GROUNDING: |HANS_ANCHOR_LOOKUP_V1: téma|HANS_FACTS_BY_TITLE_V1|"
                      r"HANS_CMD_LLM_ROUTE|HANS_LOOKUP_PRESNY|HANS_LOOKUP_OBSAHEM_V1: článek|"
                      r"HANS_PERSON_CARD|person_card")
    _LOGY = {}
    for f in sorted(glob.glob("data/logs/*")) + ["data/system.log"]:
        try:
            for ln in open(f, encoding="utf-8", errors="ignore"):
                if ln[:4] == "2026" and klic.search(ln):
                    _LOGY.setdefault(ln[:16], []).append(ln.rstrip())
        except Exception:
            pass
    return _LOGY


def _z_logu(ts: float, otazka: str):
    """Záloha bez stromu: řádky z okna dotazu (~150 s před zápisem zkoušky)."""
    t0 = _dt.datetime.fromtimestamp(ts)
    radky = []
    for dm in (-3, -2, -1, 0):
        for ln in _logy().get((t0 + _dt.timedelta(minutes=dm)).strftime("%Y-%m-%d %H:%M"), []):
            try:
                t = _dt.datetime.strptime(ln[:19], "%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue
            if t0 - _dt.timedelta(seconds=150) <= t <= t0 + _dt.timedelta(seconds=2):
                radky.append(ln)
    # jen poslední tah: od posledního „GROUNDING: skip ← start“ (začátek dotazu)
    _st = [i for i, ln in enumerate(radky) if "GROUNDING: skip ← start" in ln
           and any("GROUNDING: " in x and "skip" not in x for x in radky[i + 1:])]
    if _st:
        radky = radky[_st[-1]:]
    radky = [ln for ln in radky if "GROUNDING: skip" not in ln or ln is radky[0]]
    q40 = (otazka or "")[:40]
    # příkazový router cituje otázku — jistá vazba na tuto zkoušku
    prikaz = None
    for ln in radky:
        if "HANS_CMD_LLM_ROUTE" in ln and q40[:30] in ln and "→ /" in ln \
                and "ZAMÍTNUTO" not in ln and "ODEBRÁNO" not in ln:
            prikaz = re.search(r"→ /(\w+)", ln).group(1)
    gr = [ln for ln in radky if "GROUNDING: " in ln]
    vysl, cesta = None, ""
    if gr:
        m = re.search(r"GROUNDING: (\w+) ← (\S+)", gr[-1])
        if m:
            vysl, cesta = m.group(1), m.group(2)
    return {"zdroj": "log", "vysledek": vysl, "cesta": cesta, "text": None,
            "prikaz": prikaz, "agent": None, "f1": None, "log": radky}


def _dohledany_titul(cesta_info: dict, odpoved: str):
    m = re.search(r"heslo „([^“\"]+)[“\"]", odpoved or "")
    if m:
        return m.group(1)
    for ln in reversed(cesta_info.get("log") or []):
        m = re.search(r"HANS_ANCHOR_LOOKUP_V1: téma '([^']+)' → dohledáno", ln)
        if m:
            return m.group(1)
    return None


def _kryje(tema: str, titul: str) -> bool:
    a = [w[:4] for w in re.findall(r"\w{3,}", _fold(re.sub(r"\(.*?\)", "", tema or "")))]
    b = [w[:4] for w in re.findall(r"\w{3,}", _fold(re.sub(r"\(.*?\)", "", titul or "")))]
    return bool(a and b) and (all(x in b for x in a) or all(x in a for x in b))


def zarad(z: dict) -> dict:
    """z = řádek kolac_exam (dict). → {trida, proc, cesta, zdroj, ...}"""
    from scripts.hans_recall_znalost import fakta_podle_titulu
    odp, klic, q = z.get("odpoved") or "", z.get("klic") or "", z.get("otazka") or ""
    ci = _stopa(z["id"]) or _z_logu(z["ts"], q)
    out = {"id": z["id"], "zdroj": ci["zdroj"], "vysledek": ci["vysledek"], "cesta": ci["cesta"],
           "prikaz": ci["prikaz"]}
    try:
        _, fakta = fakta_podle_titulu(DB, q, 40)
    except Exception:
        fakta = ""
    out["fakt_ulozen"] = _soud(fakta or "", klic, q) == "ok" or _podil_klice(fakta or "", klic) >= 0.99
    # D — odpověď údaj má, jen soudce ho nepoznal (číslo slovy)
    if _soud(_cisla_slovy(odp), klic, q) == "ok":
        return dict(out, trida="D", proc="po převodu čísel slovy odpověď klíč obsahuje")
    # B1 — mimo faktickou cestu
    if ci["prikaz"]:
        return dict(out, trida="B1", proc="příkaz /%s" % ci["prikaz"])
    if ci.get("agent"):
        return dict(out, trida="B1", proc="agentní akce %s" % ci["agent"])
    if ci["vysledek"] in _NEFAKT or any(c in (ci["cesta"] or "") for c in _NEFAKT_CESTY):
        return dict(out, trida="B1", proc="cesta %s ← %s" % (ci["vysledek"], ci["cesta"]))
    if not out["fakt_ulozen"]:
        return dict(out, trida="A", proc="klíč není v uložených faktech k otázce")
    # B2 — dohledaný článek o jiném předmětu
    tit = _dohledany_titul(ci, odp)
    if tit and not _kryje(z.get("tema") or "", tit):
        return dict(out, trida="B2", proc="dohledáno %r, zkouška je o %r" % (tit, z.get("tema")))
    # klíč v podkladu?
    if ci["text"] is not None:
        v_podkladu = _soud(ci["text"], klic, q) == "ok"
    else:   # log: blok faktů podle titulu se do podkladu dostal
        v_podkladu = any("HANS_FACTS_BY_TITLE_V1" in ln and _kryje(z.get("tema") or "", ln.split(": ", 2)[-1].split(" → ")[0])
                         for ln in ci["log"])
    if v_podkladu:
        if _podil_klice(odp, klic) >= 0.3:
            return dict(out, trida="S", proc="klíč v podkladu, odpověď se shoduje jen zčásti")
        return dict(out, trida="C", proc="klíč v podkladu, odpověď ho nemá")
    return dict(out, trida="B3", proc="fakt uložen, do podkladu se nedostal (cesta %s)" % ci["cesta"])


def neuspechy(dni: int = 30) -> list:
    c = sqlite3.connect("file:%s?mode=ro" % DB, uri=True, timeout=5)
    c.row_factory = sqlite3.Row
    try:
        r = c.execute("SELECT id, ts, verdikt, tema, otazka, klic, odpoved FROM kolac_exam "
                      "WHERE verdikt IN ('vedle','nevedel') AND ts > ? AND "
                      "COALESCE(potvrzeno, 1) != 0 ORDER BY id",
                      (__import__("time").time() - dni * 86400,)).fetchall()
        return [dict(x) for x in r]
    finally:
        c.close()


def roztrid(dni: int = 30) -> list:
    return [dict(zarad(z), otazka=z["otazka"], klic=z["klic"], tema=z["tema"],
                 odpoved=z["odpoved"], ts=z["ts"]) for z in neuspechy(dni)]


def dnes(vysl: list) -> dict:
    """Přehrání dnešní cestou (zkušební handler, bez vláken provozu): finální
    odpověď modelu se NEGENERUJE — zachytí se zadání a podklad, které by model
    dostal. Pomocné kroky (F1, A1, shrnutí dohledání) běží normálně.
    → {otázka: {"prosel": bool, "kde": "podklad"|"odpověď"|"", "cesta": …}}
    ⚠️ Volá model (F1, A1, shrnutí) — nepouštět souběžně s jiným měřením."""
    from scripts import config_io
    from scripts.test_rozhovor import postav_handler
    h = postav_handler(config_io.load())
    zachyceno = {}
    puvodni = h._stream_message

    def _zachyt(prompt, name=None, on_sentence=None, internal=False, grounding=None, **kw):
        if internal:
            return puvodni(prompt, name=name, on_sentence=on_sentence, internal=internal,
                           grounding=grounding, **kw)
        sysp = prompt[0] if isinstance(prompt, (tuple, list)) else str(prompt)
        zachyceno["zadani"] = "%s\n%s" % (sysp, grounding if isinstance(grounding, str) else "")
        return ""
    h._stream_message = _zachyt
    jmeno = "zkouška"
    out = {}
    for v in vysl:
        q = v["otazka"]
        if q in out:
            continue
        zachyceno.clear()
        try:
            odp = h.send_chat_message(jmeno, q, channel="zkouska") or ""
        except Exception as e:
            odp = "(chyba: %s)" % e
        finally:
            try:
                h.conv_store.clear(jmeno)
            except Exception:
                pass
        if _soud(_cisla_slovy(odp), v["klic"], q) == "ok":
            out[q] = {"prosel": True, "kde": "odpověď"}
        elif zachyceno.get("zadani") and _soud(zachyceno["zadani"], v["klic"], q) == "ok":
            out[q] = {"prosel": True, "kde": "podklad"}
        else:
            out[q] = {"prosel": False, "kde": ""}
        out[q]["cesta"] = "%s ← %s" % (getattr(h, "_grounding_outcome", ""),
                                       getattr(h, "_grounding_cesta", ""))
    return out


BALICKY_DIR = "data/mereni/zkousky_balicky"


def balicky(vysl: list, dn: dict) -> list:
    """Balíček k opravě pro každou otázku, která DNES pořád neprojde
    (třídy B a C). Vrací cesty k souborům."""
    from collections import defaultdict
    from scripts.hans_recall_znalost import fakta_podle_titulu
    os.makedirs(BALICKY_DIR, exist_ok=True)
    skup = defaultdict(list)
    for v in vysl:
        if v["trida"] in ("B1", "B2", "B3", "C") and not (dn.get(v["otazka"]) or {}).get("prosel", True):
            skup[v["otazka"]].append(v)
    cesty = []
    for q, vv in skup.items():
        v0 = vv[-1]
        try:
            tituly, fakta = fakta_podle_titulu(DB, q, 40)
        except Exception:
            tituly, fakta = [], ""
        kmeny = [w[:5] for w in re.findall(r"\w{4,}", _fold(v0["klic"]))]
        fakt = [ln for ln in (fakta or "").split("\n") if kmeny and all(k in _fold(ln) for k in kmeny)]
        ci = _stopa(v0["id"]) or _z_logu(v0["ts"], q)
        radky = [
            "# Zkouška #%s — %s" % (",".join(str(x["id"]) for x in vv), POPIS[v0["trida"]]),
            "",
            "- **Otázka:** %s" % q,
            "- **Klíč:** %s" % v0["klic"],
            "- **Uložený fakt:** %s" % ((fakt[0].lstrip("- ") if fakt else "(nenalezen řádek s klíčem)")
                                        + (" — článek %s" % ", ".join(tituly) if tituly else "")),
            "- **Třída:** %s — %s" % (v0["trida"], v0["proc"]),
            "- **Opakování:** %d× (%s)" % (len(vv), ", ".join(
                _dt.datetime.fromtimestamp(x["ts"]).strftime("%-d. %-m. %H:%M") for x in vv)),
            "- **Dnes:** %s (%s)" % ("neprojde", (dn.get(q) or {}).get("cesta", "")),
            "",
            "## Odpověď Hanse (poslední)",
            "", (v0.get("odpoved") or "")[:800], "",
            "## Cesta v době zkoušky (%s)" % ci["zdroj"],
            "", "- výsledek: %s ← %s" % (ci["vysledek"], ci["cesta"]),
            "- příkaz: %s · F1: %s" % (ci["prikaz"], ci.get("f1")), "",
            "```", *[ln[:220] for ln in (ci["log"] or [])[-25:]], "```", "",
            "## Přehrání",
            "",
            "```",
            "python3 -m scripts.kolac_exam_trida 30 --dnes      # celé přehrání (volá model)",
            "python3 scripts/test_rozhovor.py -m %r --uklid" % q,
            "python3 tools/sada_variant.py --predmet %r --tvary \"…\" --klic %r"
            % ((tituly or [v0.get("tema") or ""])[0], v0["klic"]),
            "```",
        ]
        cesta = os.path.join(BALICKY_DIR, "%d.md" % vv[0]["id"])
        open(cesta, "w", encoding="utf-8").write("\n".join(radky) + "\n")
        cesty.append(cesta)
    return cesty


def souhrn(vysl: list, prosla=()) -> str:
    from collections import Counter, defaultdict
    if not vysl:
        return "Koláčovy zkoušky: žádný neúspěch."
    tr = Counter(v["trida"] for v in vysl)
    otazky = defaultdict(list)
    for v in vysl:
        otazky[(v["trida"], _fold(v["otazka"])[:80])].append(v)
    radky = ["Koláčovy zkoušky — neúspěchy podle tříd (%d, různých otázek %d):"
             % (len(vysl), len(otazky))]
    for t in ("B1", "B2", "B3", "C", "S", "D", "A"):
        if tr.get(t):
            radky.append("  %s %2d× — %s" % (t, tr[t], POPIS[t]))
            for (tt, _), vv in sorted(otazky.items(), key=lambda x: -len(x[1])):
                if tt == t:
                    radky.append("    %s #%s%s: %s" % ("✓" if vv[0]["otazka"] in prosla else " ",
                                                      ",".join(str(v["id"]) for v in vv),
                                                      " (%d×)" % len(vv) if len(vv) > 1 else "",
                                                      vv[0]["otazka"][:90]))
    return "\n".join(radky)


STAV = "data/mereni/zkousky_trideni/stav.json"


def rano(dni: int = 30) -> str:
    """Ranní kontrola: souhrn tříd + přehrání JEN dosud neprošlých otázek
    (prošlé si pamatuje `STAV`), balíčky k opravě pro ty, co trvají."""
    try:
        stav = json.load(open(STAV, encoding="utf-8"))
    except Exception:
        stav = {"prosla": {}}
    vysl = roztrid(dni)
    k_overeni = [v for v in vysl if v["trida"] in ("B1", "B2", "B3", "C")
                 and v["otazka"] not in stav["prosla"]]
    dn = dnes(k_overeni) if k_overeni else {}
    nove = []
    for q, r in dn.items():
        if r["prosel"]:
            stav["prosla"][q] = time.strftime("%Y-%m-%d")
            nove.append(q)
    os.makedirs(os.path.dirname(STAV), exist_ok=True)
    json.dump(stav, open(STAV, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    cesty = balicky(vysl, dn)
    otevrene = sorted({v["otazka"] for v in k_overeni} - set(nove))
    out = [souhrn(vysl, stav["prosla"]), "  (✓ = dnešní cestou už prochází)", ""]
    out.append("Dnešní cestou už prochází (opravila to pozdější změna): %d" % (
        len({v["otazka"] for v in vysl if v["otazka"] in stav["prosla"]})))
    for q in nove:
        out.append("  nově ✓ %s" % q[:90])
    sd = [v for v in vysl if v["trida"] in ("S", "D")]
    if sd:
        out.append("Klíč nebo soud k posouzení (ručně přečíst; špatnou zkoušku zamítne "
                   "uživatel na Matrixu „/nalez N ne“): %s" % ", ".join("#%d" % v["id"] for v in sd))
    out.append("Trvá (balíček k opravě): %d" % len(otevrene))
    for c in cesty:
        out.append("  %s" % c)
    return "\n".join(out)


if __name__ == "__main__":
    import sys
    os.environ.setdefault("HANS_LOG_FILE", "data/mereni/zkusebni.log")
    dni = int(next((a for a in sys.argv[1:] if a.isdigit()), 30))
    if "--rano" in sys.argv:
        print(rano(dni))
        sys.exit(0)
    vysl = roztrid(dni)
    print(souhrn(vysl))
    if "--dnes" in sys.argv:
        dn = dnes([v for v in vysl if v["trida"] in ("B1", "B2", "B3", "C")])
        print("\nDnešní cestou (klíč v podkladu nebo v odpovědi):")
        for q, r in dn.items():
            print("  %s %s | %s" % ("✓ opraveno" if r["prosel"] else "✗ trvá   ", r["cesta"][:40], q[:80]))
        for c in balicky(vysl, dn):
            print("  balíček k opravě: %s" % c)
    if "--porovnej" in sys.argv:
        P = json.load(open(sys.argv[sys.argv.index("--porovnej") + 1], encoding="utf-8"))["pravda"]
        sl = {"A": "S", "D": "D"}   # A+D v ruční pravdě = S/D (klíč nebo soud)
        shoda = 0
        for v in vysl:
            p = P.get(str(v["id"]))
            ok = p == v["trida"] or (p in ("A", "D") and v["trida"] in ("S", "D", "A"))
            shoda += ok
            if not ok:
                print("NESEDÍ #%d: ručně %s, třídič %s (%s) [%s]" % (v["id"], p, v["trida"], v["proc"], v["zdroj"]))
        print("shoda %d/%d" % (shoda, len(vysl)))
