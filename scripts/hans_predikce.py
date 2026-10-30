"""HANS_PREDIKCE_V1 (29. 10.) — Hans si tipuje zítřek a druhý den se přepočítá.

Proč: rozbor 29. 10. (`SMERY_ROZVOJE_29_10` v backlogu) — všechno, z čeho Hans
roste, je jeho vlastní historie (čte → třídí → shrnuje), a Severka z toho
poctivě odvodí „archiváře“. Jiný směr musí přijít jako JINÁ ČINNOST. Tahle je
„předpověz → ověř“: výsledek počítá KÓD, takže se nedá přikrášlit, a Hans
dostává zpětnou vazbu na vlastní úsudek (uživatel 29. 10. schválil body A, B,
C, E).

Jak:
  • večer (`od_hodiny`–`do_hodiny`, výchozí 18–23) Hans tipne zítřejší maximum,
    minimum a déšť (≥ 1 mm). Podklad: skutečné počasí posledních 7 dní
    a JEHO VLASTNÍCH posledních 5 tipů i s chybami a průměrnou odchylkou —
    tím se uzavírá smyčka učení. Oficiální předpověď NEVIDÍ; uloží se jen jako
    měřítko.
  • další den kód stáhne skutečnost (Open-Meteo `past_days`, analýza modelu
    blízká měření) a spočítá chybu Hanse i oficiální předpovědi; výsledek
    jde do deníku jako fakt (`prediction_result`).
  • `/predikce` ukáže poslední výsledky a skóre.
Model: hlas (`evening_reflection.voice_model`, rezidentní hans-czech) — žádný
druhý model do VRAM. Vypínač `predikce.enabled`.
"""
import json
import logging
import re
import sqlite3
import time
from datetime import date, datetime, timedelta

_log = logging.getLogger("hans_predikce")

_SCHEMA = """CREATE TABLE IF NOT EXISTS predikce (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL,
    druh TEXT NOT NULL DEFAULT 'pocasi', cil_datum TEXT NOT NULL,
    hans TEXT NOT NULL DEFAULT '{}', proc TEXT NOT NULL DEFAULT '',
    oficialni TEXT NOT NULL DEFAULT '{}', skutecnost TEXT NOT NULL DEFAULT '{}',
    vysledek TEXT NOT NULL DEFAULT '{}',
    stav TEXT NOT NULL DEFAULT 'ceka',      -- ceka|vyhodnoceno
    UNIQUE(druh, cil_datum))"""
_DEST_MM = 1.0
_URL = ("https://api.open-meteo.com/v1/forecast?latitude=%s&longitude=%s"
        "&daily=temperature_2m_max,temperature_2m_min,precipitation_sum"
        "&timezone=Europe/Prague&past_days=%d&forecast_days=%d")


def _cfg(config: dict) -> dict:
    return (config or {}).get("predikce", {}) or {}


def _conn(db: str):
    c = sqlite3.connect(db, timeout=10)
    c.row_factory = sqlite3.Row
    c.execute(_SCHEMA)
    return c


def _cislo(x) -> str:
    """12.3 → „12,3“ (čeština)."""
    try:
        return ("%.1f" % float(x)).replace(".", ",")
    except Exception:
        return "?"


def _znam(x) -> str:
    """0.3 → „+0,3“."""
    try:
        return ("%+.1f" % float(x)).replace(".", ",")
    except Exception:
        return "?"


def _den_txt(iso: str) -> str:
    d = date.fromisoformat(iso)
    return "%d. %d." % (d.day, d.month)


def denni(config: dict, past: int = 8, fwd: int = 2) -> dict:
    """{iso_datum: {max, min, srazky}} z Open-Meteo; {} při chybě."""
    import requests
    w = (config or {}).get("weather", {}) or {}
    try:
        r = requests.get(_URL % (w.get("lat", 50.08), w.get("lon", 14.42), past, fwd),
                         timeout=10)
        if r.status_code != 200:
            _log.warning("HANS_PREDIKCE_V1: Open-Meteo HTTP %d", r.status_code)
            return {}
        d = r.json().get("daily", {}) or {}
    except Exception as e:
        _log.warning("HANS_PREDIKCE_V1: Open-Meteo: %s", e)
        return {}
    out = {}
    for i, t in enumerate(d.get("time", []) or []):
        try:
            out[t] = {"max": float(d["temperature_2m_max"][i]),
                      "min": float(d["temperature_2m_min"][i]),
                      "srazky": float(d["precipitation_sum"][i] or 0.0)}
        except Exception:
            continue
    return out


# ── výsledky (čisté funkce kvůli regresi) ────────────────────────────────────
def chyby(tip: dict, skut: dict) -> dict:
    """Odchylka tipu od skutečnosti: max/min se znaménkem (tip − skutečnost),
    déšť trefa ano/ne."""
    return {"max": round(float(tip["max"]) - skut["max"], 1),
            "min": round(float(tip["min"]) - skut["min"], 1),
            "dest": bool(tip.get("dest")) == (skut["srazky"] >= _DEST_MM)}


def body(ch: dict) -> float:
    """Jedno číslo k porovnání: průměrná absolutní chyba teplot + 2 °C za
    netrefený déšť (menší = lepší)."""
    return round((abs(ch["max"]) + abs(ch["min"])) / 2 + (0 if ch["dest"] else 2), 1)


def veta_vysledku(cil: str, tip: dict, skut: dict, ch: dict, ch_of) -> str:
    s = ("Můj tip na %s: maximum %s °C (skutečnost %s), minimum %s °C "
         "(skutečnost %s), %s (skutečně %s)."
         % (_den_txt(cil), _cislo(tip["max"]), _cislo(skut["max"]),
            _cislo(tip["min"]), _cislo(skut["min"]),
            "déšť" if tip.get("dest") else "bez deště",
            "pršelo %s mm" % _cislo(skut["srazky"]) if skut["srazky"] >= _DEST_MM
            else "nepršelo"))
    s += " Spletl jsem se o %s °C u maxima a o %s °C u minima%s." % (
        _cislo(abs(ch["max"])), _cislo(abs(ch["min"])),
        "" if ch["dest"] else ", déšť jsem netrefil")
    if ch_of:
        bh, bo = body(ch), body(ch_of)
        s += " Oficiální předpověď: %s body, já %s — %s." % (
            _cislo(bo), _cislo(bh),
            "byl jsem lepší" if bh < bo else ("stejně" if bh == bo else "byla lepší"))
    return s


# ── podklad a zadání ─────────────────────────────────────────────────────────
def moje_minule(db: str, n: int = 5) -> list:
    c = _conn(db)
    try:
        rows = c.execute("SELECT * FROM predikce WHERE stav='vyhodnoceno' "
                         "ORDER BY cil_datum DESC LIMIT ?", (n,)).fetchall()
    finally:
        c.close()
    return [dict(r) for r in rows]


def zadani(cil: str, dny: dict, minule: list) -> tuple:
    """(systém, dotaz) pro tip na den `cil`. Čistá funkce."""
    sysp = (
        "Tipuješ zítřejší počasí v místě, kde bydlíš, jen podle vlastního úsudku "
        "(žádnou předpověď nemáš). Dostaneš skutečné počasí posledních dní "
        "a své dřívější tipy i s tím, o kolik ses spletl — poučíš se z nich. "
        "Vrať POUZE JSON: {\"max\": číslo °C, \"min\": číslo °C, \"dest\": "
        "true/false (aspoň 1 mm srážek), \"proc\": \"1 věta v 1. osobě česky, "
        "z čeho vycházíš\"}.")
    radky = []
    for iso in sorted(dny):
        if iso >= cil:
            continue
        v = dny[iso]
        radky.append("- %s: max %s °C, min %s °C, srážky %s mm" % (
            _den_txt(iso), _cislo(v["max"]), _cislo(v["min"]), _cislo(v["srazky"])))
    moje = []
    for m in minule:
        try:
            ch = json.loads(m["vysledek"])
            t = json.loads(m["hans"])
        except Exception:
            continue
        moje.append("- na %s: tip max %s / min %s → chyba %s / %s °C%s" % (
            _den_txt(m["cil_datum"]), _cislo(t.get("max")), _cislo(t.get("min")),
            _znam(ch.get("max", 0)), _znam(ch.get("min", 0)),
            "" if ch.get("dest") else ", déšť netrefen"))
    odch = ""
    if minule:
        try:
            mx = [json.loads(m["vysledek"])["max"] for m in minule]
            mn = [json.loads(m["vysledek"])["min"] for m in minule]
            odch = ("\nPrůměrně jsem tipoval maximum o %s °C a minimum o %s °C "
                    "oproti skutečnosti." % (_znam(sum(mx) / len(mx)),
                                             _znam(sum(mn) / len(mn))))
        except Exception:
            odch = ""
    dotaz = ("Zítra je %s.\n\nSKUTEČNÉ POČASÍ POSLEDNÍCH DNÍ:\n%s\n\nMOJE DŘÍVĚJŠÍ TIPY:\n%s%s"
             "\n\nJSON:" % (_den_txt(cil) + " " + str(date.fromisoformat(cil).year),
                            "\n".join(radky[-7:]) or "(nic)",
                            "\n".join(moje) or "(zatím žádné)", odch))
    return sysp, dotaz


def parsuj(raw: str):
    """{max, min, dest, proc} nebo None. Čistá funkce."""
    m = re.search(r"\{.*\}", raw or "", re.S)
    try:
        d = json.loads(m.group(0)) if m else None
    except Exception:
        d = None
    if not isinstance(d, dict):
        return None
    try:
        mx = float(str(d.get("max")).replace(",", "."))
        mn = float(str(d.get("min")).replace(",", "."))
    except Exception:
        return None
    if mn > mx:
        mx, mn = mn, mx
    if not (-40 <= mn <= 45 and -40 <= mx <= 45):
        return None
    dest = d.get("dest")
    if isinstance(dest, str):
        dest = dest.strip().lower() in ("true", "ano", "yes", "1")
    return {"max": round(mx, 1), "min": round(mn, 1), "dest": bool(dest),
            "proc": re.sub(r"\s+", " ", str(d.get("proc") or "")).strip()[:300]}


# ── kroky ────────────────────────────────────────────────────────────────────
def _zapis_denik(db: str, typ: str, titul: str, note: str, data: dict) -> None:
    try:
        with sqlite3.connect(db, timeout=10) as c:
            c.execute("INSERT INTO diary (ts, event_type, title, note, data) "
                      "VALUES (?,?,?,?,?)", (time.time(), typ, titul, note,
                                             json.dumps(data, ensure_ascii=False)))
            c.commit()
    except Exception as e:
        _log.warning("HANS_PREDIKCE_V1: zápis do deníku: %s", e)


def predpovez(config: dict, db: str, dnes: date = None):
    """Tip na zítřek. Vrací dict tipu, False (už je / nejde), None (model dole)."""
    dnes = dnes or date.today()
    cil = (dnes + timedelta(days=1)).isoformat()
    c = _conn(db)
    try:
        if c.execute("SELECT 1 FROM predikce WHERE druh='pocasi' AND cil_datum=?",
                     (cil,)).fetchone():
            return False
    finally:
        c.close()
    dny = denni(config, past=8, fwd=2)
    if not dny or cil not in dny:
        return False
    from scripts.ollama_client import ollama_generate
    model = ((config.get("evening_reflection", {}) or {}).get("voice_model")
             or "hans-czech:latest")
    sysp, dotaz = zadani(cil, dny, moje_minule(db))
    raw = ollama_generate(model, dotaz, system=sysp, config=config, timeout=120,
                          options={"temperature": 0.4, "num_ctx": 4096,
                                   "num_predict": 200})
    if not raw:
        return None
    tip = parsuj(raw)
    if not tip:
        _log.info("HANS_PREDIKCE_V1: nepoužitelná odpověď modelu: %.120s", raw)
        return False
    oficialni = dny[cil]
    c = _conn(db)
    try:
        c.execute("INSERT OR IGNORE INTO predikce (ts, druh, cil_datum, hans, proc, "
                  "oficialni) VALUES (?,?,?,?,?,?)",
                  (time.time(), "pocasi", cil,
                   json.dumps({k: tip[k] for k in ("max", "min", "dest")}),
                   tip["proc"], json.dumps(oficialni)))
        c.commit()
    finally:
        c.close()
    note = ("Tipuji počasí na %s: maximum %s °C, minimum %s °C, %s. %s"
            % (_den_txt(cil), _cislo(tip["max"]), _cislo(tip["min"]),
               "bude pršet" if tip["dest"] else "bez deště", tip["proc"])).strip()
    _zapis_denik(db, "prediction", "Můj tip na počasí %s" % _den_txt(cil), note,
                 {"cil_datum": cil, "tip": tip})
    _log.info("HANS_PREDIKCE_V1: tip na %s → %s", cil, tip)
    return tip


def vyhodnot(config: dict, db: str, dnes: date = None) -> int:
    """Vyhodnoť tipy na dny před dneškem. Vrací počet vyhodnocených."""
    dnes = (dnes or date.today()).isoformat()
    c = _conn(db)
    try:
        cekaji = [dict(r) for r in c.execute(
            "SELECT * FROM predikce WHERE stav='ceka' AND cil_datum<? "
            "ORDER BY cil_datum", (dnes,))]
    finally:
        c.close()
    if not cekaji:
        return 0
    dny = denni(config, past=10, fwd=1)
    n = 0
    for p in cekaji:
        skut = dny.get(p["cil_datum"])
        if not skut:
            continue
        tip = json.loads(p["hans"])
        of = json.loads(p["oficialni"] or "{}")
        ch = chyby(tip, skut)
        ch_of = chyby({"max": of["max"], "min": of["min"],
                       "dest": of.get("srazky", 0) >= _DEST_MM}, skut) if of else None
        vys = dict(ch, body=body(ch))
        if ch_of:
            vys["oficialni"] = dict(ch_of, body=body(ch_of))
        c = _conn(db)
        try:
            c.execute("UPDATE predikce SET skutecnost=?, vysledek=?, stav='vyhodnoceno' "
                      "WHERE id=?", (json.dumps(skut), json.dumps(vys), p["id"]))
            c.commit()
        finally:
            c.close()
        veta = veta_vysledku(p["cil_datum"], tip, skut, ch, ch_of)
        _zapis_denik(db, "prediction_result",
                     "Jak jsem tipoval počasí na %s" % _den_txt(p["cil_datum"]),
                     veta, {"cil_datum": p["cil_datum"], "vysledek": vys})
        _log.info("HANS_PREDIKCE_V1: vyhodnoceno %s → %s", p["cil_datum"], vys)
        n += 1
    return n


def krok(config: dict, db: str, mozek: bool, ted: datetime = None) -> None:
    """Voláno z rutiny periodicky. Vyhodnocení kdykoli (jen síť), tip jen
    večer a s mozkem. Nikdy nehází."""
    c = _cfg(config)
    if not c.get("enabled", True):
        return
    ted = ted or datetime.now()
    try:
        vyhodnot(config, db, ted.date())
    except Exception as e:
        _log.warning("HANS_PREDIKCE_V1: vyhodnocení selhalo: %s", e)
    if not mozek:
        return
    if not (int(c.get("od_hodiny", 18)) <= ted.hour < int(c.get("do_hodiny", 23))):
        return
    try:
        predpovez(config, db, ted.date())
    except Exception as e:
        _log.warning("HANS_PREDIKCE_V1: tip selhal: %s", e)


def souhrn(db: str, n: int = 7) -> str:
    """Text pro /predikce."""
    c = _conn(db)
    try:
        hot = [dict(r) for r in c.execute(
            "SELECT * FROM predikce WHERE stav='vyhodnoceno' ORDER BY cil_datum DESC")]
        cek = [dict(r) for r in c.execute(
            "SELECT * FROM predikce WHERE stav='ceka' ORDER BY cil_datum")]
    finally:
        c.close()
    if not hot and not cek:
        return ("Zatím jsem si žádný tip na počasí neudělal. Tipuji každý večer "
                "na další den a druhý den si spočítám, jak jsem se trefil.")
    out = []
    for p in cek:
        t = json.loads(p["hans"])
        out.append("Čeká: tip na %s — max %s °C, min %s °C, %s." % (
            _den_txt(p["cil_datum"]), _cislo(t["max"]), _cislo(t["min"]),
            "déšť" if t.get("dest") else "bez deště"))
    if hot:
        lepsi = sum(1 for p in hot if "oficialni" in json.loads(p["vysledek"])
                    and json.loads(p["vysledek"])["body"]
                    < json.loads(p["vysledek"])["oficialni"]["body"])
        bh = [json.loads(p["vysledek"])["body"] for p in hot]
        bo = [json.loads(p["vysledek"])["oficialni"]["body"] for p in hot
              if "oficialni" in json.loads(p["vysledek"])]
        out.append("Vyhodnocených tipů: %d. Průměrná chyba %s bodu (oficiální "
                   "předpověď %s); lépe než ona jsem tipoval %d×. Menší číslo = lepší." % (
                       len(hot), _cislo(sum(bh) / len(bh)),
                       _cislo(sum(bo) / len(bo)) if bo else "?", lepsi))
        for p in hot[:n]:
            out.append("• " + veta_vysledku(
                p["cil_datum"], json.loads(p["hans"]), json.loads(p["skutecnost"]),
                json.loads(p["vysledek"]),
                json.loads(p["vysledek"]).get("oficialni")))
    return "\n".join(out)
