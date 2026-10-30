"""HANS_FILM_VOLBA_V1 (29. 10.) — film navrhuje Hans sám a učí se z odezvy.

Proč: rozbor 29. 10. (`SMERY_ROZVOJE_29_10`) — osobnost podle výzkumu nemění
reflexe sama, ale ROLE se závazkem a skutečná odezva okolí. Návrh filmu
(`KODI_FILM_SUGGEST_V1`) byl jediná činnost s odezvou od lidí (220 návrhů,
20 přijato), jenže film vybíral program podle žánrů a Hans se z výsledku
nedozvěděl nic. Teď:
  • program dál nasbírá 2–4 kandidáty (`_pick_next_film`, stejná pravidla),
  • Hans z nich vybere jeden a řekne proč — vidí, co ta osoba dřív přijala
    a co ne, a co se v poslední době hrálo,
  • úspěšnost se počítá PROGRAMEM po osobách a jde do večerní reflexe
    a do přehledu chování pro Severku.
Model: hlas (`evening_reflection.voice_model`, rezidentní hans-czech).
Vypínač `film_suggest.hans_voli`. Výpadek modelu / herní mód → první
kandidát jako dřív.
"""
import json
import logging
import re
import sqlite3
import time

_log = logging.getLogger("hans_film_volba")
_OKNO_PRIJETI_S = 3600.0


def _ro(db: str):
    return sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=5.0)


def _titul_navrhu(note: str) -> str:
    return re.sub(r"^Návrh:\s*", "", (note or "").strip())


def historie(db: str, osoba: str, n: int = 12, dni: int = 120) -> list:
    """[{titul, prijato, ts}] posledních návrhů pro osobu (nejnovější první).
    Přijetí = záznam `film_suggestion_accepted` téže osoby s tím titulem
    do hodiny po návrhu."""
    od = time.time() - dni * 86400
    con = _ro(db)
    try:
        nav = con.execute(
            "SELECT ts, note FROM diary WHERE event_type='film_suggestion' "
            "AND title=? AND ts>=? ORDER BY ts DESC LIMIT ?", (osoba, od, n)).fetchall()
        pri = con.execute(
            "SELECT ts, note FROM diary WHERE event_type='film_suggestion_accepted' "
            "AND title=? AND ts>=?", (osoba, od)).fetchall()
    finally:
        con.close()
    out = []
    for ts, note in nav:
        t = _titul_navrhu(note)
        ok = any(0 <= pts - ts <= _OKNO_PRIJETI_S and ("„%s" % t) in (pn or "")
                 for pts, pn in pri)
        out.append({"titul": t, "prijato": ok, "ts": ts})
    return out


def uspesnost(db: str, dni: int = 60) -> dict:
    """{osoba: (prijato, navrzeno)} za posledních `dni`."""
    od = time.time() - dni * 86400
    con = _ro(db)
    try:
        osoby = [r[0] for r in con.execute(
            "SELECT DISTINCT title FROM diary WHERE event_type='film_suggestion' "
            "AND ts>=? AND title<>''", (od,))]
    finally:
        con.close()
    out = {}
    for o in osoby:
        h = historie(db, o, n=500, dni=dni)
        if h:
            out[o] = (sum(1 for x in h if x["prijato"]), len(h))
    return out


def nedavno_hrane(db: str, n: int = 6) -> list:
    con = _ro(db)
    try:
        rows = con.execute(
            "SELECT title FROM diary WHERE event_type='kodi_playing' AND title<>'' "
            "ORDER BY ts DESC LIMIT 40").fetchall()
    finally:
        con.close()
    out = []
    for (t,) in rows:
        if t not in out:
            out.append(t)
        if len(out) >= n:
            break
    return out


def _norm(s: str) -> str:
    import unicodedata
    return "".join(ch for ch in unicodedata.normalize("NFD", (s or "").lower())
                   if unicodedata.category(ch) != "Mn" and (ch.isalnum() or ch == " ")).strip()


def odmitane(hist: list, dni: int = 30, kolikrat: int = 2) -> set:
    """Normalizované tituly, které osoba za `dni` aspoň `kolikrat` nepřijala
    (a ani jednou nepřijala). Čistá funkce."""
    od = time.time() - dni * 86400
    pocet, prij = {}, set()
    for x in hist:
        if x["ts"] < od:
            continue
        k = _norm(x["titul"])
        if x["prijato"]:
            prij.add(k)
        else:
            pocet[k] = pocet.get(k, 0) + 1
    return {k for k, n in pocet.items() if n >= kolikrat and k not in prij}


def zadani(jmeno: str, kandidati: list, hist: list, hrane: list,
           rod: str = "", oblibene: list = (), zanry: list = ()) -> tuple:
    """(systém, dotaz) — čistá funkce kvůli regresi."""
    sysp = (
        "Vybíráš film, který nabídneš konkrétnímu člověku z domácnosti. Máš "
        "kandidáty z knihovny, své dřívější návrhy pro něj (co přijal a co ne) "
        "a co se v poslední době hrálo. Uč se z toho, co dřív zabralo a co ne. "
        "Nevymýšlej si nic o filmech ani o člověku mimo to, co dostaneš — "
        "o jeho vkusu víš JEN to, co stojí v části CO O NĚM VÍM a v jeho "
        "přijatých návrzích.\n"
        "Vrať POUZE JSON: {\"volba\": číslo kandidáta, \"proc\": \"1 krátká "
        "věta v 1. osobě česky, proč právě tenhle — opři ji jen o uvedené "
        "údaje\"}.")
    k = []
    obl = {_norm(o) for o in oblibene or ()}
    for i, m in enumerate(kandidati, 1):
        nt = _norm(m.get("title", ""))
        drive = [x for x in hist if _norm(x["titul"]) == nt]
        pozn = []
        if int(m.get("playcount") or 0) > 0:
            pozn.append("už viděný")
        if any(o and (o in nt or nt in o) for o in obl):
            pozn.append("uvedl ho jako oblíbený")
        if drive:
            pozn.append("už jsem ho nabídl %d× (přijato %d×)" % (
                len(drive), sum(1 for x in drive if x["prijato"])))
        k.append("%d. %s%s — %s%s" % (
            i, m.get("title", "?"), (" (%s)" % m["year"]) if m.get("year") else "",
            ", ".join(m.get("genre") or []) or "žánr neznámý",
            ("; " + "; ".join(pozn)) if pozn else ""))
    h = ["- %s: %s" % (x["titul"], "PŘIJAL" if x["prijato"] else "nepřijal")
         for x in hist[:10]]
    pr = sum(1 for x in hist if x["prijato"])
    vim = []
    if zanry:
        vim.append("- žánry, které podle záznamů z televize sleduje: %s"
                   % ", ".join(zanry[:6]))
    if oblibene:
        vim.append("- sám uvedl jako oblíbené: %s" % ", ".join(oblibene[:6]))
    _pri = [x["titul"] for x in hist if x["prijato"]]
    if _pri:
        vim.append("- z mých návrhů přijal: %s" % ", ".join(dict.fromkeys(_pri)))
    dotaz = ("ČLOVĚK: %s%s\n\nCO O NĚM VÍM:\n%s\n\nKANDIDÁTI:\n%s\n\nMÉ DŘÍVĚJŠÍ NÁVRHY PRO NĚJ "
             "(nejnovější nahoře; přijato %d z %d):\n%s\n\nV POSLEDNÍ DOBĚ SE "
             "HRÁLO:\n%s\n\nJSON:" % (
                 jmeno, (" (%s)" % rod) if rod else "", "\n".join(vim) or "(nic)",
                 "\n".join(k), pr, len(hist), "\n".join(h) or "(zatím nic)",
                 "\n".join("- %s" % t for t in hrane) or "(nic)"))
    return sysp, dotaz


def parsuj(raw: str, pocet: int):
    """(index 0.., proc) nebo None. Čistá funkce."""
    m = re.search(r"\{.*\}", raw or "", re.S)
    try:
        d = json.loads(m.group(0)) if m else None
    except Exception:
        d = None
    if not isinstance(d, dict):
        return None
    try:
        i = int(str(d.get("volba")).strip().rstrip("."))
    except Exception:
        return None
    if not 1 <= i <= pocet:
        return None
    return i - 1, re.sub(r"\s+", " ", str(d.get("proc") or "")).strip()[:240]


def kandidati(idle, names, cfg, prvni: dict, kolik: int = 4) -> list:
    """Prvního kandidáta vybral program jako dřív; doplní další stejnými
    pravidly (náhoda uvnitř `_pick_next_film`)."""
    out, ids = [prvni], {prvni.get("movieid")}
    for _ in range(kolik * 3):
        if len(out) >= kolik:
            break
        try:
            m = idle._pick_next_film(names, cfg)
        except Exception:
            m = None
        if m and m.get("movieid") is not None and m["movieid"] not in ids:
            ids.add(m["movieid"])
            out.append(m)
    return out


def vyber_hansem(idle, config: dict, db: str, names: list, cfg: dict, prvni: dict):
    """(film, proc, kandidáti_tituly). Selhání → (prvni, '', [...])."""
    kand = kandidati(idle, names, cfg, prvni, int(cfg.get("hans_kandidatu", 4)))
    # film, který osoba za 30 dní 2× nepřijala, se nenabízí (dřív se Blade
    # Runner / Zodiac opakovaly dokola) — když by nezbylo nic, nech všechny
    try:
        _odm = odmitane(historie(db, names[0], n=60, dni=30))
        _zbyle = [m for m in kand if _norm(m.get("title", "")) not in _odm]
        if _zbyle and len(_zbyle) < len(kand):
            _log.info("HANS_FILM_VOLBA_V1: vyřazeno opakovaně odmítané %s",
                      [m.get("title") for m in kand if m not in _zbyle])
            kand = _zbyle
            prvni = kand[0]
    except Exception:
        pass
    import random as _r
    _r.shuffle(kand)          # model má sklon brát prvního kandidáta
    tituly = [m.get("title", "") for m in kand]
    if len(kand) < 2:
        return prvni, "", tituly
    try:  # (po zamíchání vrací selhání PROGRAMOVÝ výběr `prvni`)
        from scripts.ollama_client import game_mode_on, ollama_generate
        if game_mode_on():
            return prvni, "", tituly
        from scripts.cz_names import display_name, person_gender
        osoba = names[0]
        _rod = {"muž": "muž", "žena": "žena"}.get(person_gender(osoba) or "", "")
        _obl = []
        try:
            _obl = idle._person_fav_films([osoba])
        except Exception:
            pass
        _zanry = []
        try:
            _zanry = idle._person_watch_genres([osoba])
        except Exception:
            pass
        sysp, dotaz = zadani(display_name(osoba), kand, historie(db, osoba),
                             nedavno_hrane(db), rod=_rod, oblibene=_obl,
                             zanry=_zanry)
        model = ((config.get("evening_reflection", {}) or {}).get("voice_model")
                 or "hans-czech:latest")
        raw = ollama_generate(model, dotaz, system=sysp, config=config, timeout=60,
                              options={"temperature": 0.5, "num_ctx": 4096,
                                       "num_predict": 150})
        v = parsuj(raw, len(kand))
        if not v:
            _log.info("HANS_FILM_VOLBA_V1: nepoužitelná volba (%.100s) → první kandidát",
                      raw or "")
            return prvni, "", tituly
        i, proc = v
        _log.info("HANS_FILM_VOLBA_V1: %s ← „%s“ z %s — %s", osoba,
                  kand[i].get("title"), tituly, proc)
        return kand[i], proc, tituly
    except Exception as e:
        _log.warning("HANS_FILM_VOLBA_V1 selhalo: %s", e)
        return prvni, "", tituly


# ── fakta pro reflexi a Severku (počítá program) ────────────────────────────
def veta_dne(db: str, date_str: str, s_duvodem: bool = True) -> list:
    """Věty o návrzích filmů za den (pro večerní reflexi), [] = nic.
    `s_duvodem=False` pro extrakci postojů: důvod psal Hans sám (ozvěna,
    STANCE_FACTS_NOT_ECHO_V1), výsledek je fakt."""
    con = _ro(db)
    try:
        nav = con.execute(
            "SELECT ts, title, note, data FROM diary WHERE event_type='film_suggestion' "
            "AND date(ts,'unixepoch','localtime')=? ORDER BY ts", (date_str,)).fetchall()
    finally:
        con.close()
    out = []
    from scripts.cz_names import display_name, past_verb, person_gender
    for ts, osoba, note, data in nav:
        pr = past_verb("přijal", person_gender(osoba))
        t = _titul_navrhu(note)
        h = historie(db, osoba, n=500, dni=60)
        prij = any(x["prijato"] and abs(x["ts"] - ts) < 1 for x in h)
        proc = ""
        try:
            proc = (json.loads(data or "{}") or {}).get("proc", "") if s_duvodem else ""
        except Exception:
            pass
        celkem = (sum(1 for x in h if x["prijato"]), len(h))
        out.append(("Návrh filmu", "%s: nabídl jsem „%s“%s — %s. (Za 60 dní mi %s "
                    "%s %d z %d návrhů.)" % (
                        display_name(osoba), t, (" (%s)" % proc) if proc else "",
                        pr if prij else "ne" + pr, display_name(osoba), pr,
                        celkem[0], celkem[1])))
    return out
