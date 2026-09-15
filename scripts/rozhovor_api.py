#!/usr/bin/env python3
"""Rozhovor s BĚŽÍCÍM Hansem přes web chat API — jen pod testovací identitou.

TAZATEL_RUNNER_V1 (15. 9.) — nástroj pro agenta `tazatel` (a pro ruční test).
`test_rozhovor.py` staví Hanse ve VLASTNÍM procesu; tenhle mluví s instancí,
která běží uživateli (most `api/chat/send` → `data/.web_chat_req.json`).

Použití:
    python3 scripts/rozhovor_api.py --osoba zkouška --zacni
    python3 scripts/rozhovor_api.py --osoba zkouška -m "Dobrý den, jak se máte?"
    python3 scripts/rozhovor_api.py --osoba zkouška --konec

Pojistky:
  • Jen jméno z `config.test_persons` — do deníku ani RAG se nezapisuje
    (HANS_TEST_PERSON_V1, HANS_AGENT_LOG_TEST_PERSON_V1). Jiné jméno = odmítnuto.
  • `--zacni` zazálohuje konverzaci a začne čistou; `--konec` ji vrátí.
    Běžící rozhovor (existuje záloha) se nepřepíše — nejdřív `--konec`.
  • Přepis jde do `data/mereni/rozhovory/` (ne /tmp: měření nesmí zmizet).
"""
import argparse
import json
import os
import re
import shutil
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
API = "http://127.0.0.1:7860"
DIR = os.path.join(ROOT, "data", "mereni", "rozhovory")
LOG = os.path.join(ROOT, "data", "system.log")
# Co z logu k tahu patří: rozhodnutí o cestě, ne šum kolem.
KLIC = re.compile(r"GROUNDING: |LLM_ROUTE|ZAMÍTNUTO|agent: instant|C1: |F1: "
                  r"|GUARD|HANS_[A-Z0-9_]+_V\d+:")
SUM = re.compile(r"HANS_TEST_PERSON_V1|HANS_CONVINDEX_V1|HANS_DEEPEN_FEEDBACK_GATE")


def _test_osoby() -> list:
    from scripts import config_io
    return [str(x) for x in (config_io.load(hlasit=False).get("test_persons") or [])]


def _api(path: str, data=None) -> dict:
    req = urllib.request.Request(
        API + path, data=json.dumps(data).encode() if data is not None else None,
        headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=30).read())


def _cesty(osoba: str):
    konv = os.path.join(ROOT, "data", "conversations", "%s.json" % osoba)
    zal = os.path.join(DIR, ".zaloha_%s.json" % osoba)
    ukaz = os.path.join(DIR, ".aktivni_%s" % osoba)
    return konv, zal, ukaz


def zacni(osoba: str) -> int:
    konv, zal, ukaz = _cesty(osoba)
    os.makedirs(DIR, exist_ok=True)
    if os.path.exists(ukaz):
        print("CHYBA: rozhovor pod '%s' už běží — nejdřív --konec." % osoba)
        return 2
    if os.path.exists(konv):
        shutil.copy2(konv, zal)
        os.remove(konv)
    prepis = os.path.join(DIR, "%s_%s.jsonl" % (time.strftime("%Y%m%d_%H%M%S"), osoba))
    with open(ukaz, "w", encoding="utf-8") as f:
        json.dump({"prepis": prepis, "zaloha": os.path.exists(zal)}, f)
    open(prepis, "w", encoding="utf-8").close()
    print("ZAČÁTEK: čistý rozhovor pod '%s', přepis %s" % (osoba, prepis))
    return 0


def rekni(osoba: str, veta: str) -> int:
    _, _, ukaz = _cesty(osoba)
    if not os.path.exists(ukaz):
        print("CHYBA: nejdřív --zacni.")
        return 2
    stav = json.load(open(ukaz, encoding="utf-8"))
    od = time.strftime("%Y-%m-%d %H:%M:%S")
    t0 = time.time()
    try:
        rid = _api("/api/chat/send", {"person": osoba, "message": veta}).get("id")
    except Exception as e:
        print("CHYBA: Hans neodpovídá na API (%s)." % e)
        return 3
    odp = ""
    while time.time() - t0 < 300:
        time.sleep(2)
        try:
            r = _api("/api/chat/poll?id=%s" % rid)
        except Exception:
            continue
        if r.get("response"):
            odp = r["response"]
            break
    trvani = time.time() - t0
    radky = []
    try:
        for l in open(LOG, encoding="utf-8", errors="ignore"):
            if l[:19] >= od and KLIC.search(l) and not SUM.search(l):
                radky.append(l[11:19] + " " + l.split("] ", 1)[-1][:180].rstrip())
    except Exception:
        pass
    with open(stav["prepis"], "a", encoding="utf-8") as f:
        f.write(json.dumps({"cas": od, "trvani_s": round(trvani), "veta": veta,
                            "odpoved": odp or "(TIMEOUT)", "log": radky[:15]},
                           ensure_ascii=False) + "\n")
    print("HANS (%.0f s): %s" % (trvani, odp or "(TIMEOUT — bez odpovědi do 300 s)"))
    for r in radky[:15]:
        print("  CESTA: " + r)
    return 0


def konec(osoba: str) -> int:
    konv, zal, ukaz = _cesty(osoba)
    if not os.path.exists(ukaz):
        print("Nic neběží pod '%s'." % osoba)
        return 0
    stav = json.load(open(ukaz, encoding="utf-8"))
    if stav.get("zaloha") and os.path.exists(zal):
        shutil.copy2(zal, konv)
        os.remove(zal)
    elif os.path.exists(konv):
        os.remove(konv)          # před testem konverzace nebyla
    os.remove(ukaz)
    n = sum(1 for _ in open(stav["prepis"], encoding="utf-8"))
    print("KONEC: konverzace '%s' vrácena, %d tahů v %s" % (osoba, n, stav["prepis"]))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--osoba", required=True, help="testovací identita z config.test_persons")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--zacni", action="store_true")
    g.add_argument("-m", "--zprava")
    g.add_argument("--konec", action="store_true")
    a = ap.parse_args()
    povolene = _test_osoby()
    if a.osoba not in povolene:
        print("ODMÍTNUTO: '%s' není testovací identita (povolené: %s). "
              "Pod skutečným jménem by se rozhovor zapsal do paměti." % (a.osoba, ", ".join(povolene)))
        return 2
    if a.zacni:
        return zacni(a.osoba)
    if a.konec:
        return konec(a.osoba)
    return rekni(a.osoba, a.zprava)


if __name__ == "__main__":
    sys.exit(main())
