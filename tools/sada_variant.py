#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SADA_VARIANT_V1 (13. 10.) — ověření opravy v rozhodovací vrstvě na VARIANTÁCH
téže otázky, ne na jedné větě.

Proč: 13. 10. dvakrát vyšlo, že oprava ověřená jednou větou padla na jiné
formulaci (jméno malým písmenem, bez háčků, dlouhá zdvořilá věta, vykání).
Nástroj vezme předmět (a případně jeho skloněné tvary), vyrobí z něj sadu vět
v obou osobách a vypíše, kudy která jde: který krok před modelem si ji vezme,
jakou cestou se staví podklad, jestli se přiložil výpis faktů a jestli je
v podkladu hledaný údaj.

Nic neprovádí a do paměti nezapisuje: volá jen `hans_rozhodnuti.rozhodni`
a `_build_grounding` ve zkušebním handleru bez vláken provozu. Klasifikace
záměru a přepis věty z vlákna volají model na PC — bez něj jedou záložní
pravidla a výsledek se může lišit (nástroj to vypíše).

Použití:
  python3 tools/sada_variant.py --predmet "Kip Thorne" --tvary "Kipa Thorna,Kipu Thorneovi" --klic "Princeton"
  python3 tools/sada_variant.py --predmet "Bauhaus" --druh vec
  python3 tools/sada_variant.py --veta "kdy měl premiéru film Zlaté oko?" --klic 1995
  python3 tools/sada_variant.py --predmet "hrad Kost" --json vystup.json
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import unicodedata
from pathlib import Path

# ZKUSEBNI_LOG_V1 (13. 10.) — zkušební běh nepíše do provozního logu: řádky
# pravidel z testů se pletly do odečtů (`grep` nad data/system.log) a druhý
# proces sdílel rotovaný soubor s běžícím Hansem. `scripts/logger.py` čte
# HANS_LOG_FILE při importu, proto se nastavuje PŘED importem čehokoli ze scripts.
import os
os.environ.setdefault("HANS_LOG_FILE", "data/mereni/zkusebni.log")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def bez_hacku(s: str) -> str:
    return "".join(ch for ch in unicodedata.normalize("NFD", s or "")
                   if unicodedata.category(ch) != "Mn")


# (štítek, šablona) — {X} = předmět v základním tvaru, {x} = týž malým písmem
_SABLONY = {
    "osoba": [
        ("tykání, znalost", "co víš o {X}?"),
        ("tykání, kdo", "kdo je {X}?"),
        ("tykání, žádost o dohledání", "můžeš mi zjistit něco o {X}?"),
        ("tykání, rozkaz", "řekni mi něco o {X}"),
        ("vykání, znalost", "Co víte o {X}?"),
        ("vykání, znáte", "Znáte {X}? Co byste mi o tom mohl říci?"),
        ("vykání, žádost o dohledání", "Mohl byste mi zjistit něco o {X}?"),
        ("vykání, dlouhá zdvořilá", "To je zajímavé, děkuji. A teď bych se vás "
         "ráda zeptala na něco jiného – mohl byste mi říci něco o {X}? "
         "Zajímá mě to už delší dobu."),
        ("vykání, žádost uprostřed věty", "Rozumím. A ještě jedna věc, pokud "
         "dovolíte – dokázal byste mi dohledat, kdo je {X}? Moc by mi to pomohlo."),
    ],
}
_SABLONY["vec"] = [(s, v.replace("kdo je {X}", "co je {X}")) for s, v in _SABLONY["osoba"]]


def varianty(predmet: str, tvary: list, druh: str) -> list:
    """[(štítek, věta)] — šablony × (jak napsáno, malým, bez háčků) + skloněné tvary."""
    out, videne = [], set()

    def pridej(stitek, veta):
        if veta not in videne:
            videne.add(veta)
            out.append((stitek, veta))

    for stitek, sab in _SABLONY[druh]:
        pridej(stitek, sab.format(X=predmet))
    for stitek, sab in _SABLONY[druh][:3] + _SABLONY[druh][6:7]:
        pridej(stitek + " · malým", sab.format(X=predmet.lower()))
        pridej(stitek + " · malým bez háčků", bez_hacku(sab.format(X=predmet.lower())))
    for tv in tvary:
        pridej("skloněný tvar", "co víš o %s?" % tv)
        pridej("skloněný tvar · malým bez háčků", bez_hacku("slyšel jsi o %s?" % tv.lower()))
    return out


def varianty_vety(veta: str) -> list:
    out = [("jak napsáno", veta)]
    for stitek, v in (("malým", veta.lower()), ("bez háčků", bez_hacku(veta)),
                      ("malým bez háčků", bez_hacku(veta.lower())),
                      ("bez otazníku", veta.rstrip("?! .")),
                      ("po zdvořilém úvodu", "To je zajímavé, děkuji. A ještě bych se "
                       "chtěla zeptat: " + veta)):
        if v != veta and v not in [x[1] for x in out]:
            out.append((stitek, v))
    return out


def projdi(vety: list, osoba: str, klic: str) -> list:
    from scripts import config_io
    from scripts.test_rozhovor import postav_handler
    from scripts import hans_rozhodnuti
    from scripts import hans_recall
    cfg = config_io.load()
    db = (cfg.get("hans_idle", {}) or {}).get("diary_db", "data/hans_diary.db")
    h = postav_handler(cfg)
    cesta = {}
    puv = h._vysledek_groundingu

    def zachyt(v, c):
        if v != "skip":
            cesta["c"] = "%s ← %s" % (v, c)
        return puv(v, c)
    h._vysledek_groundingu = zachyt

    fk = bez_hacku(klic).lower() if klic else ""
    radky = []
    for stitek, veta in vety:
        r = {"stitek": stitek, "veta": veta}
        try:
            rz = hans_rozhodnuti.rozhodni(h, osoba, veta, None) or {}
            if rz.get("prikaz"):
                r["krok"] = "příkaz /%s" % (rz["prikaz"][0] if isinstance(rz["prikaz"], (list, tuple)) else rz["prikaz"])
            elif rz.get("akce"):
                a = rz["akce"]
                r["krok"] = "agent: %s" % (a[1] if isinstance(a, (list, tuple)) and len(a) > 1 else a)
            else:
                r["krok"] = (rz.get("krok") or "model").replace("_sc_", "")
        except Exception as e:
            r["krok"] = "CHYBA %s" % e
        cesta.clear()
        try:
            g = h._build_grounding(veta, osoba) or ""
        except Exception as e:
            g = ""
            cesta["c"] = "CHYBA %s" % e
        r["cesta"] = cesta.get("c", "—")
        # když si větu vezme krok před modelem (příkaz, znalosti, agent…),
        # podklad se v provozu nestaví — cesta je jen „co by bylo, kdyby“
        r["k_modelu"] = r["krok"] == "model"
        try:
            r["tituly"] = hans_recall.fakta_tituly_ve_vete(db, veta)
        except Exception:
            r["tituly"] = []
        r["fakta_v_podkladu"] = "Fakta z článku" in g
        r["delka_podkladu"] = len(g)
        if fk:
            r["klic"] = fk in bez_hacku(g).lower()
        radky.append(r)
    return radky


def tabulka(radky: list, klic: str) -> str:
    def zkrat(s, n):
        return s if len(s) <= n else s[:n - 1] + "…"
    out = []
    hl = "%-34s | %-22s | %-38s | fakta | %s" % (
        "varianta", "krok před modelem", "cesta podkladu", "klíč" if klic else "")
    out.append(hl)
    out.append("-" * len(hl))
    for r in radky:
        out.append("%-34s | %-22s | %-38s | %-5s | %s" % (
            zkrat(r["stitek"], 34), zkrat(r["krok"], 22),
            zkrat(r["cesta"] if r["k_modelu"] else "(%s)" % r["cesta"], 38),
            "ano" if r["fakta_v_podkladu"] else "ne",
            ("ano" if r["klic"] else "NE") if klic else ""))
        out.append("    " + zkrat(r["veta"], 110))
    return "\n".join(out)


def souhrn(radky: list, klic: str) -> str:
    import collections
    c = collections.Counter(r["cesta"] for r in radky)
    k = collections.Counter(r["krok"] for r in radky)
    s = ["", "SOUHRN: %d variant" % len(radky),
         "  kroky před modelem: " + ", ".join("%s %d×" % x for x in k.most_common()),
         "  cesty podkladu:     " + ", ".join("%s %d×" % x for x in c.most_common()),
         "  výpis faktů přiložen: %d z %d" % (sum(r["fakta_v_podkladu"] for r in radky), len(radky))]
    if klic:
        s.append("  hledaný údaj „%s“ v podkladu: %d z %d"
                 % (klic, sum(bool(r.get("klic")) for r in radky), len(radky)))
    m = [r for r in radky if r["k_modelu"]]
    s.append("  z toho k modelu dojde %d: fakta přiložena u %d%s" % (
        len(m), sum(r["fakta_v_podkladu"] for r in m),
        (", údaj v podkladu u %d" % sum(bool(r.get("klic")) for r in m)) if klic else ""))
    s.append("  (cesta v závorce = větu si vezme krok před modelem, podklad se nestaví; "
             "zvolený příkaz nebo akce ale můžou skončit naprázdno a věta pak jde k modelu)")
    if len(c) > 1 or len(k) > 1:
        s.append("  ⚠️ varianty téže otázky jdou RŮZNÝMI cestami — oprava na jedné "
                 "cestě ostatní nepokryje.")
    return "\n".join(s)


def main() -> int:
    ap = argparse.ArgumentParser(description="Sada variant téže otázky proti rozhodovací vrstvě.")
    ap.add_argument("--predmet", help="předmět v základním tvaru, jak se píše (např. název článku)")
    ap.add_argument("--tvary", default="", help="skloněné tvary oddělené čárkou")
    ap.add_argument("--druh", choices=("osoba", "vec"), default="osoba")
    ap.add_argument("--veta", help="místo šablon obměňovat tuhle větu")
    ap.add_argument("--klic", default="", help="údaj, který má být v podkladu")
    ap.add_argument("--osoba", default="zkouška", help="jméno tazatele (výchozí zkušební identita)")
    ap.add_argument("--json", help="uložit výsledek do souboru")
    a = ap.parse_args()
    if not (a.predmet or a.veta):
        ap.error("zadej --predmet nebo --veta")
    if (ROOT / "data/.ollama_paused").exists():
        print("⚠️  herní mód — klasifikace pojede bez modelu, výsledek se může lišit")
    vety = (varianty_vety(a.veta) if a.veta else
            varianty(a.predmet, [t.strip() for t in a.tvary.split(",") if t.strip()], a.druh))
    t0 = time.time()
    radky = projdi(vety, a.osoba, a.klic)
    print(tabulka(radky, a.klic))
    print(souhrn(radky, a.klic))
    print("  (%.0f s)" % (time.time() - t0))
    if a.json:
        Path(a.json).write_text(json.dumps(radky, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
