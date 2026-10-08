"""HANS_HLAVNI_ZPRAVA_V1 (8. 10.) — hlavní zpráva posledních dvou dnů, sama na Matrix.

Proč: zprávy se sbírají každou hodinu, ale všechno kolem nich bylo jen na dotaz
(za 17 dní logu 20 odpovědí z podkladu, v deníku nic). Tady Hans jednou za dva
dny sám vybere příběh, který se nejvíc držel na špici ve víc médiích, a krátce
napíše, co se stalo, jak se to vyvíjelo a v čem se média liší. Bez verdiktu,
co je pravda.

Výběr (zpětné měření 8. 10. na čtyřech oknech, `data/mereni/hlavni_zprava_08_10/`):
- skóre = součet hodin, po které mělo KAŽDÉ médium událost na úzké špici
  (nejlepší pozice ≤ `pozice`), jen události aspoň ze `min_medii` médií.
  ⛔ „hodiny na špici × počet médií“ nerozlišuje — velká událost je na špici
  celé okno a vyhrávají pořád tytéž vleklé příběhy.
- dřívější vítěz smí vyhrát znovu jen s novým vývojem (aspoň `novych_titulku`
  nových titulků z `novych_medii` médií od minulé výhry).

Text: úzké zadání nad titulky z okna + kontrola věrnosti po větách (číslo nebo
jméno, které v podkladu není → věta pryč); když nezbude dost, pošlou se titulky
samotné. Odkazy přikládá kód.
"""
from __future__ import annotations

import logging
import os
import re
import sqlite3
import time
from datetime import datetime as _dt

from scripts import hans_zpravy as z

_log = logging.getLogger("hans_hlavni_zprava")


def _cfg(config: dict) -> dict:
    return ((config or {}).get("zpravy", {}) or {}).get("hlavni_zprava", {}) or {}


def _tabulka(c) -> None:
    c.execute("""CREATE TABLE IF NOT EXISTS hlavni_zpravy (
        id INTEGER PRIMARY KEY, ts REAL, okno_od REAL, okno_do REAL, udalost_id INTEGER,
        titulek TEXT, skore REAL, medii INTEGER, text TEXT, zpusob TEXT, odeslano INTEGER DEFAULT 0)""")


def _sjednoceni_h(useky: list) -> float:
    s, konec = 0.0, None
    for a, b in sorted(u for u in useky if u[1] > u[0]):
        if konec is None or a > konec:
            s += b - a
            konec = b
        elif b > konec:
            s += b - konec
            konec = b
    return s / 3600.0


def vyber(config: dict, ted: float | None = None, path: str = z.DB) -> list:
    """Kandidáti okna seřazení podle skóre; první s `smi` = True je vítěz.
    → [{uid, skore, medii, novych, novych_medii, smi, titulek}]"""
    k = _cfg(config)
    ted = ted or time.time()
    a = ted - float(k.get("okno_h", 48)) * 3600.0
    poz = int(k.get("pozice", 3))
    c = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=5)
    try:
        try:
            vyhry = dict(c.execute("SELECT udalost_id, MAX(ts) FROM hlavni_zpravy "
                                    "WHERE odeslano = 1 GROUP BY 1"))
        except sqlite3.OperationalError:
            vyhry = {}
        ud = {}
        for uid, zdroj, p, q, npoz, prvni in c.execute(
                """SELECT tu.udalost_id, t.zdroj, t.prvni_na_spici_ts, t.posledni_na_spici_ts,
                          t.nejlepsi_pozice, t.prvni_ts
                   FROM titulky t JOIN titulek_udalost tu ON tu.titulek_id = t.id
                   WHERE t.posledni_ts >= ? AND t.prvni_ts < ?""", (a, ted)):
            d = ud.setdefault(uid, {"m": {}, "nove": 0, "nmed": set()})
            if p and q and (npoz or 99) <= poz:
                d["m"].setdefault(zdroj, []).append((max(p, a), min(q + 3600, ted)))
            if prvni > max(a, vyhry.get(uid, 0)):
                d["nove"] += 1
                d["nmed"].add(zdroj)
        out = []
        for uid, d in ud.items():
            hod = [_sjednoceni_h(u) for u in d["m"].values()]
            medii = sum(1 for h in hod if h > 0)
            if medii < int(k.get("min_medii", 3)):
                continue
            smi = uid not in vyhry or (d["nove"] >= int(k.get("novych_titulku", 5))
                                       and len(d["nmed"]) >= int(k.get("novych_medii", 3)))
            out.append({"uid": uid, "skore": round(sum(hod), 1), "medii": medii,
                        "novych": d["nove"], "novych_medii": len(d["nmed"]), "smi": smi})
        out.sort(key=lambda r: -r["skore"])
        out = out[:8]
        for r in out:
            cs = c.execute(
                """SELECT t.titulek FROM titulky t JOIN titulek_udalost tu ON tu.titulek_id = t.id
                   WHERE tu.udalost_id = ? AND t.jazyk = 'cs' AND t.prvni_ts < ?
                   ORDER BY t.prvni_ts DESC LIMIT 1""", (r["uid"], ted)).fetchone()
            r["titulek"] = (cs[0] if cs else
                            (c.execute("SELECT titulek FROM udalosti WHERE id = ?",
                                       (r["uid"],)).fetchone() or [""])[0]) or ""
        return out
    finally:
        c.close()


def podklad(uid: int, od: float, do: float, path: str = z.DB) -> tuple:
    """(české řádky, zahraniční řádky) — „den, Médium: titulek — perex“, jen kanály
    médií (bez souhrnů Google News a bez bulváru), rovnoměrně přes okno."""
    nazvy = {x[0]: x[1] for x in z.ZDROJE}
    osa = [x for x in z.casova_osa(uid, path)
           if od <= (x.get("cas") or 0) < do and x.get("zdroj") in nazvy
           and z._f(nazvy[x["zdroj"]]) not in z.BULVAR]

    def radky(cesky: bool, n: int) -> list:
        videno, r = set(), []
        for x in osa:
            if (x.get("jazyk") == "cs") != cesky:
                continue
            klic = (x["zdroj"], (x.get("titulek") or "")[:40])
            if klic in videno or not x.get("titulek"):
                continue
            videno.add(klic)
            r.append("- %s, %s: %s%s" % (
                _dt.fromtimestamp(x["cas"]).strftime("%d. %m."), nazvy[x["zdroj"]], x["titulek"],
                (" — " + x["perex"][:160]) if cesky and x.get("perex") else ""))
        if len(r) > n:
            krok = len(r) / float(n)
            r = [r[int(i * krok)] for i in range(n)]
        return r
    return radky(True, 12), radky(False, 8)


_SYS = (
    "Jsi {jmeno}, zdvořilý a přemýšlivý společník. Píšeš česky krátkou zprávu pro domácnost.\n"
    "Dostaneš TITULKY jedné události za poslední dva dny: česká média a zahraniční média "
    "(v původním jazyce).\n"
    "Napiš 5 až 7 vět: (1) co se stalo, (2) jak se to během těch dvou dnů vyvíjelo, "
    "(3) v čem se média liší nebo co uvádí jen jedno z nich, a jestli zahraniční tisk "
    "zdůrazňuje něco jiného než český.\n"
    "PRAVIDLA: Jen to, co stojí v titulcích. Jména a čísla opiš přesně a u čísla uveď médium. "
    "Nehodnoť, co je pravda. Co je jen tvrzení jedné strany, tak označ. Bez oslovení, bez "
    "úvodní zdvořilosti, bez dat a časů v závorkách, žádné odkazy, žádné nabídky.")

_VETY = re.compile(r"(?<=[.!?…])\s+(?=[A-ZÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ„\"(])")
_CISLO = re.compile(r"\d[\d  .,]*\d|\d")
_JMENO = re.compile(r"\b[A-ZÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ][a-záčďéěíňóřšťúůýž]{3,}")


def vernost(text: str, zaklad: str) -> tuple:
    """Věty, které drží podklad, a počet vyřazených. Vyřazuje se věta s číslem,
    které v podkladu není, nebo se jménem (velké písmeno uvnitř věty), jehož
    kmen v podkladu není."""
    f = z._f(zaklad)
    cisla = {re.sub(r"[  ]", "", m) for m in _CISLO.findall(zaklad)}
    dobre, pryc = [], 0
    for v in _VETY.split((text or "").strip()):
        v = v.strip()
        if not v:
            continue
        spatne = any(re.sub(r"[  ]", "", m).rstrip(".,") not in cisla
                     and re.sub(r"[  ]", "", m) not in cisla for m in _CISLO.findall(v))
        if not spatne:
            for m in _JMENO.finditer(v):
                if m.start() == 0:
                    continue                      # první slovo věty má velké písmeno vždy
                if z._f(m.group(0))[:5] not in f:
                    spatne = True
                    break
        if spatne:
            pryc += 1
        else:
            dobre.append(v)
    return dobre, pryc


def napis(config: dict, uid: int, od: float, do: float, titulek: str = "",
          path: str = z.DB) -> tuple:
    """(text, 'hlasem' | 'titulky' | None). None = není z čeho psát."""
    cs, ci = podklad(uid, od, do, path)
    if not cs and not ci:
        return "", None
    zaloha = "Píšou o tom: " + " ".join(
        (re.sub(r"^- \d\d\. \d\d\., ", "", x).split(" — ")[0].rstrip(".") + ".")
        for x in (cs or ci)[:4])
    zaklad = "ČESKÁ MÉDIA:\n%s\n\nZAHRANIČNÍ MÉDIA:\n%s" % ("\n".join(cs) or "(nic)",
                                                           "\n".join(ci) or "(nic)")
    try:
        from scripts.ollama_client import ollama_generate
        from scripts.hans_persona import persona_name
        k = _cfg(config)
        model = str((config.get("models", {}) or {}).get("voice")
                    or (config.get("dialog", {}) or {}).get("model") or "hans-czech:latest")
        r = (ollama_generate(
            model, zaklad + "\n\nTvoje zpráva:",
            system=_SYS.format(jmeno=persona_name(config) or "Hans"), config=config,
            timeout=int(k.get("timeout_s", 120)),
            options={"temperature": 0.3, "num_predict": int(k.get("num_predict", 420)),
                     "num_ctx": int(k.get("num_ctx", 8192))}) or "").strip()
        r = re.sub(r"\s*\[[^\]]{0,60}\]", "", r)          # opsané časy v závorkách
        dobre, pryc = vernost(r, zaklad + " " + titulek)
        if len(dobre) >= int(k.get("min_vet", 3)):
            if pryc:
                _log.info("HANS_HLAVNI_ZPRAVA_V1: vyřazeno vět mimo podklad: %d", pryc)
            return " ".join(dobre), "hlasem"
        _log.info("HANS_HLAVNI_ZPRAVA_V1: text neprošel kontrolou (zbylo %d vět, vyřazeno %d) "
                  "→ titulky", len(dobre), pryc)
    except Exception as e:
        _log.info("HANS_HLAVNI_ZPRAVA_V1: psaní nejde (%s) → titulky", str(e)[:80])
    return zaloha, "titulky"


def posledni(path: str = z.DB) -> dict | None:
    """Naposledy vybraná hlavní zpráva (i neodeslaná), nebo None."""
    if not os.path.exists(path):
        return None
    c = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=5)
    try:
        r = c.execute("SELECT ts, udalost_id, titulek, text, medii FROM hlavni_zpravy "
                      "WHERE text IS NOT NULL AND text != '' ORDER BY ts DESC LIMIT 1").fetchone()
    except sqlite3.OperationalError:
        return None
    finally:
        c.close()
    return dict(zip(("ts", "uid", "titulek", "text", "medii"), r)) if r else None


def sestav_zpravu(titulek: str, text: str, medii: int, odkazy: list) -> str:
    return ("📰 Hlavní zpráva posledních dvou dnů (na špici v %d médiích): %s\n\n%s%s"
            % (medii, titulek.rstrip("."), text,
               ("\n\n" + "\n".join(odkazy)) if odkazy else ""))


def tick(config: dict, notifier=None, diary_path: str | None = None,
         ted: float | None = None, path: str = z.DB) -> str | None:
    """Zavolat z rutiny. Když je čas a je mozek, vybere, napíše, pošle a zapíše.
    Vrací odeslaný text, jinak None."""
    k = _cfg(config)
    if not k.get("enabled", True) or not os.path.exists(path):
        return None
    ted = ted or time.time()
    h = _dt.fromtimestamp(ted).hour
    if not (int(k.get("hodina_od", 10)) <= h < int(k.get("hodina_do", 20))):
        return None
    c = z._db(path)
    try:
        _tabulka(c)
        c.commit()
        naposled = (c.execute("SELECT MAX(ts) FROM hlavni_zpravy WHERE odeslano = 1").fetchone()
                    or [None])[0] or 0
        pokus = (c.execute("SELECT MAX(ts) FROM hlavni_zpravy WHERE odeslano = 0").fetchone()
                 or [None])[0] or 0
    finally:
        c.close()
    # o dvě hodiny kratší než perioda, ať se čas odeslání den po dni neposouvá
    if ted - naposled < float(k.get("kazdych_dni", 2)) * 86400.0 - 7200.0:
        return None
    if ted - pokus < float(k.get("opakovat_po_h", 2)) * 3600.0:
        return None                               # nedoručeno → zkusit znovu až za chvíli
    from scripts.ollama_client import brain_available, game_mode_on
    if game_mode_on() or not brain_available(config):
        return None
    kand = vyber(config, ted, path)
    vitez = next((r for r in kand if r["smi"]), None)
    if not vitez:
        _log.info("HANS_HLAVNI_ZPRAVA_V1: žádný kandidát (v okně %d událostí se skóre)", len(kand))
        return None
    od = ted - float(k.get("okno_h", 48)) * 3600.0
    text, zpusob = napis(config, vitez["uid"], od, ted, vitez["titulek"], path)
    if not zpusob:
        _log.info("HANS_HLAVNI_ZPRAVA_V1: událost %d nemá v okně titulky z kanálů médií",
                  vitez["uid"])
        return None
    try:
        odkazy = [o["url"] for o in z.zpravy_odkazy(vitez["uid"], path, limit=2)]
    except Exception:
        odkazy = []
    zprava = sestav_zpravu(vitez["titulek"], text, vitez["medii"], odkazy)
    ok = True
    if notifier:
        try:
            ok = notifier(zprava) is not False
        except Exception as e:
            ok = False
            _log.warning("HANS_HLAVNI_ZPRAVA_V1: odeslání selhalo: %s", e)
    c = z._db(path)
    try:
        _tabulka(c)
        c.execute("INSERT INTO hlavni_zpravy (ts, okno_od, okno_do, udalost_id, titulek, skore, "
                  "medii, text, zpusob, odeslano) VALUES (?,?,?,?,?,?,?,?,?,?)",
                  (ted, od, ted, vitez["uid"], vitez["titulek"], vitez["skore"], vitez["medii"],
                   text, zpusob, 1 if ok else 0))
        c.commit()
    finally:
        c.close()
    if ok and diary_path:
        try:
            d = sqlite3.connect(diary_path, timeout=5.0)
            d.execute("INSERT INTO diary (ts, event_type, title, note) VALUES (?,?,?,?)",
                      (ted, "news_check", "Hlavní zpráva: " + vitez["titulek"][:120], text))
            d.commit()
            d.close()
        except Exception as e:
            _log.warning("HANS_HLAVNI_ZPRAVA_V1: zápis do deníku selhal: %s", e)
    _log.info("HANS_HLAVNI_ZPRAVA_V1: událost %d (%.0f médium-hodin, %d médií, %s) → %s",
              vitez["uid"], vitez["skore"], vitez["medii"], zpusob,
              "odesláno" if ok else "NEodesláno")
    return zprava if ok else None
