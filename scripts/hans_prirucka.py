"""HANS_PRIRUCKA_V1 (26. 9.) — Hansova příručka webového designu z VLASTNÍHO studia.

Zadání uživatele: kritika díla má vzejít od Hanse — nastuduje web design,
estetiku a dynamické weby a MÁ z toho vycházet při tvorbě.

Z uloženého zdroje (`study_sources`, u webových témat MDN) se vytáhnou
pravidla. Pravidlo se přijme, jen když:
  - jeho citace je DOSLOVA ve zdroji (pilot 26. 9.: 14/19 u MDN),
  - je to doporučení, ne popis („vlastnost color nastavuje barvu“),
  - má-li měřitelnou hranici, to ČÍSLO stojí v citaci (jinak ho model
    vymyslel).
Pravidla s měřením (`METRIKY` — umí je sonda v `hans_webdilo`) se na díle
KONTROLUJÍ; ostatní jdou coderu jen jako pokyn. Kritika pak zní „porušil jsem
své pravidlo X (zdroj Y)“, ne „myslím, že se to povedlo“ (to je ozvěna).
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from typing import Optional

from scripts.logger import get_logger

_log = get_logger("hans_prirucka")

# měření, která umí sonda: klíč → (co se měří, jednotka, jak se porovná)
METRIKY = {
    "kontrast": "minimální kontrast textu vůči pozadí (poměr, např. 4.5)",
    "velikosti_pisma": "počet různých velikostí písma na stránce (max)",
    "delka_radku": "délka řádku odstavce ve znacích (max, případně min)",
    "radkovani": "řádkování odstavce jako násobek velikosti písma (min)",
    "velikost_cilu": "nejmenší výška/šířka odkazu či tlačítka v px (min)",
    "polozky_navigace": "počet odkazů v navigaci (max)",
    "obecne_pismo": "seznam písem končí obecnou rodinou (serif/sans-serif…), 1 = ano",
    "velikost_pisma_textu": "velikost písma odstavců v px (min)",
    "zadna": "neměřitelné — jen pokyn",
}

_SCHEMA = {"type": "object", "properties": {"pravidla": {"type": "array", "items": {
    "type": "object", "properties": {
        "pravidlo": {"type": "string"},
        "citace": {"type": "string"},
        "je_doporuceni": {"type": "boolean"},
        "metrika": {"type": "string", "enum": list(METRIKY)},
        "prah": {"type": ["number", "null"]},
        "smer": {"type": "string", "enum": ["min", "max", "rovno", "zadny"]}},
    "required": ["pravidlo", "citace", "je_doporuceni", "metrika", "prah", "smer"]}}},
    "required": ["pravidla"]}

_SYSTEM = (
    "Z MATERIÁLU vypiš DOPORUČENÍ, podle kterých se navrhuje nebo kontroluje "
    "webová stránka (rozvržení, typografie, barvy, kontrast, ovládání, "
    "přístupnost, responzivita, interaktivita). 'pravidlo' napiš česky jako "
    "pokyn („Používej…“, „Nepřekračuj…“). Do 'citace' zkopíruj DOSLOVA větu "
    "z materiálu, která pravidlo dokládá; obsahuje-li pravidlo ČÍSLO, citace "
    "musí být věta, ve které to číslo stojí, a pravidlo převezme číslo i jeho "
    "význam přesně (nepřepočítávej, nezaměňuj řádkování s mezerou mezi "
    "odstavci). 'je_doporuceni' = false, pokud věta "
    "jen POPISUJE, co vlastnost dělá (např. „color sets the text colour“). "
    "'metrika' vyber z nabídky, jen když jde pravidlo změřit na hotové stránce; "
    "jinak 'zadna'. 'prah' = číslo z citace (např. 4.5), jinak null; 'smer' = "
    "min/max/rovno/zadny. Obecné rady bez opory v materiálu nevypisuj. Nabídka "
    "metrik: " + "; ".join("%s = %s" % kv for kv in METRIKY.items()))


def _db(db_path: str):
    con = sqlite3.connect(db_path, timeout=10)
    con.execute("""CREATE TABLE IF NOT EXISTS design_rules (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, topic TEXT, sub TEXT,
        pravidlo TEXT, citace TEXT, metrika TEXT, prah REAL, smer TEXT,
        zdroj TEXT, stav TEXT DEFAULT 'aktivni')""")
    return con


def _norm(t: str) -> str:
    return re.sub(r"\s+", " ", (t or "")).strip().lower()


def _cislo_v(prah, text: str) -> bool:
    if prah is None:
        return True
    for v in {("%g" % float(prah)), ("%g" % float(prah)).replace(".", ",")}:
        if re.search(r"(?<![\d.,])" + re.escape(v) + r"(?![\d])", text):
            return True
    return False


_RADA = re.compile(
    r"\b(should|must|ensure|avoid|recommend\w*|better off|best practice|"
    r"make sure|don't|do not|never|always|need to|prefer|consider|try to|"
    r"at least|no more than|minimum|maximum|měl\w* by|musí|nesmí|"
    r"doporuč\w*|vyhn\w*|vždy|nikdy|alespoň|nejvýše)\b", re.I)


def over(pravidla: list, material: str) -> tuple:
    """Deterministická kontrola. Vrací (přijatá, [(pravidlo, důvod)] odmítnutá)."""
    mat = _norm(material)
    ok, pryc = [], []
    for p in pravidla or []:
        c = _norm(p.get("citace"))
        if not c or c[:80] not in mat:
            pryc.append((p, "citace není ve zdroji")); continue
        # příznak modelu NESTAČÍ: 26. 9. označil za „popis“ i „ensure you have
        # enough color contrast…“ a „you are better off using the smallest
        # number of colors“. Rozhoduje i RADA v citaci.
        if not p.get("je_doporuceni") and not _RADA.search(p.get("citace") or ""):
            pryc.append((p, "popis, ne doporučení")); continue
        m = p.get("metrika") if p.get("metrika") in METRIKY else "zadna"
        if m != "zadna" and p.get("prah") is not None and not _cislo_v(p["prah"], p["citace"]):
            pryc.append((p, "práh %s není v citaci" % p.get("prah"))); continue
        if m != "zadna" and p.get("prah") is None and m != "obecne_pismo":
            m = "zadna"            # měřitelné bez čísla → jen pokyn
        ok.append({**p, "metrika": m})
    return ok, pryc


def _zdroj_citace(material: str, citace: str, vychozi: str) -> str:
    """Oddíl materiálu, ve kterém citace leží (WCAG / MDN článek / hlavní)."""
    mat = material or ""
    i = _norm(mat).find(_norm(citace)[:80])
    if i < 0:
        return vychozi
    # _norm slučuje mezery → pozici hledej v normalizovaném textu hlaviček
    hlavicky = [(m.start(), m.group(1)) for m in
                re.finditer(r"\[(WCAG 2\.2: [\w-]+|MDN: [^\]]+|Hlavní článek: [^\]]+)\]",
                            _norm(mat), re.I)]
    pred = [h for pos, h in hlavicky if pos <= i]
    if not pred:
        return vychozi
    h = pred[-1]
    if h.lower().startswith("wcag"):
        return "https://www.w3.org/WAI/WCAG22/Understanding/%s.html" % h.split(":", 1)[1].strip()
    if h.lower().startswith("mdn"):
        return "MDN: " + h.split(":", 1)[1].strip()
    return vychozi


def vytahni(config: dict, topic: str, sub: str, material: str, zdroj: str,
            db_path: str, model: str = "") -> dict:
    """Vytáhne a uloží pravidla ze zdroje. Vrací statistiku."""
    from scripts.ollama_client import ollama_generate
    if not model:
        from scripts import hans_study as hs
        model = hs._model(config)
    raw = ollama_generate(model, "Pod-téma: %s\n\nMATERIÁL:\n%s" % (sub, (material or "")[:16000]),
                          system=_SYSTEM, config=config, timeout=900, keep_alive=0,
                          format=_SCHEMA,
                          options={"temperature": 0.2, "num_ctx": 16384, "num_predict": 2500})
    try:
        pr = (json.loads(raw or "{}") or {}).get("pravidla") or []
    except Exception:
        pr = []
    ok, pryc = over(pr, material)
    con = _db(db_path)
    try:
        for p in ok:
            # HANS_PRIRUCKA_DEDUP_V1 — měřené pravidlo = (metrika, práh, směr)
            dup = con.execute("SELECT 1 FROM design_rules WHERE lower(pravidlo)=lower(?)"
                              " OR (metrika=? AND metrika!='zadna' AND prah IS ? AND smer=?)",
                              (p["pravidlo"], p["metrika"], p.get("prah"),
                               p.get("smer"))).fetchone()
            if not dup:
                con.execute("INSERT INTO design_rules (ts, topic, sub, pravidlo, citace, "
                            "metrika, prah, smer, zdroj) VALUES (?,?,?,?,?,?,?,?,?)",
                            (time.time(), topic, sub, p["pravidlo"], p["citace"],
                             p["metrika"], p.get("prah"), p.get("smer"),
                             _zdroj_citace(material, p["citace"], zdroj)))
        con.commit()
    finally:
        con.close()
    st = {"navrzeno": len(pr), "prijato": len(ok),
          "merena": sum(1 for p in ok if p["metrika"] != "zadna"),
          "odmitnuto": [d for _, d in pryc]}
    _log.info("příručka: '%s' — %s", sub, st)
    return st


def pravidla(db_path: str) -> list:
    try:
        con = _db(db_path)
        try:
            rows = con.execute("SELECT id, pravidlo, citace, metrika, prah, smer, zdroj "
                               "FROM design_rules WHERE stav='aktivni' ORDER BY id").fetchall()
        finally:
            con.close()
    except Exception:
        return []
    return [dict(zip(("id", "pravidlo", "citace", "metrika", "prah", "smer", "zdroj"), r))
            for r in rows]


def pro_stavbu(db_path: str, limit: int = 25) -> str:
    """Příručka jako pokyn pro coder (anglicky stručně, pravidla česky)."""
    pr = pravidla(db_path)[:limit]
    if not pr:
        return ""
    return ("HANS'S OWN DESIGN HANDBOOK (rules he learned; follow them):\n"
            + "\n".join("- %s" % p["pravidlo"] for p in pr))


def zkontroluj(db_path: str, mereni: dict) -> list:
    """Porušení MĚŘENÝCH pravidel. `mereni` = {metrika: hodnota} ze sondy
    (nejhorší případ na stránce). Vrací [{pravidlo, citace, zdroj, namereno}]."""
    out = []
    for p in pravidla(db_path):
        m = p["metrika"]
        if m == "zadna" or m not in mereni or mereni[m] is None:
            continue
        v, prah, smer = mereni[m], p["prah"], p["smer"]
        spatne = False
        if m == "obecne_pismo":
            spatne = not v
        elif prah is not None and smer == "min":
            spatne = v < prah
        elif prah is not None and smer == "max":
            spatne = v > prah
        if spatne:
            out.append({"pravidlo": p["pravidlo"], "citace": p["citace"],
                        "zdroj": p["zdroj"], "metrika": m, "namereno": v, "prah": prah})
    return out
