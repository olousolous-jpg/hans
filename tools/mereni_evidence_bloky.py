#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Měření: evidence PO BLOCÍCH místo slepence (20. 9.).

Otázka: 19. 9. se ukázalo, že přidat system prompt do reference guardu ŘEDÍ
(overlap je PODÍL, delší opora zvedne překryv u všeho). Návrh: nepočítat proti
SJEDNOCENÍ, ale proti KAŽDÉMU bloku zvlášť a vzít MAXIMUM — každý blok je malý
a tematický, takže ředění zmizí, a navíc je vidět, KTERÝ blok větu kryje.

Tři varianty nad TOUTÉŽ větou (párově, jinak se měří odpověď, ne reference):
  V0 dnes      : overlap vs (grounding + 6 zpráv historie)
  V1 slepenec  : overlap vs (V0 + evidence_text)            ⛔ zamítnuto 19. 9.
  V2 po blocích: max( overlap vs V0, max_i overlap vs blok_i )

⚠️ MEZ: bloky se berou ŽIVÉ (dnešní), věty jsou z 19. 9. Grounding a historie
jsou naopak přesné (grounding se reprodukuje toutéž produkční funkcí).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.grounding_guard import _stems, MIN_SUPPORT, MIN_STEMS  # noqa: E402
import scripts.openwebui_direct_handler as M  # noqa: E402

PREPIS = ROOT / "data/mereni/rozhovory/20260919_170445_zkouška.jsonl"


def pasmo(ov: float) -> str:
    if ov == 0.0:
        return "nechá"
    return "ZAHODÍ" if ov < MIN_SUPPORT else "nechá"


def main() -> int:
    cfg = None
    from scripts import config_io
    cfg = config_io.load()

    rows = [json.loads(l) for l in open(PREPIS, encoding="utf-8") if l.strip()]

    # ── zachytit bloky promptu ────────────────────────────────────────────
    zachyt = {}
    _orig = M.evidence_text

    def _spy(hodnoty):
        zachyt["h"] = dict(hodnoty)
        return _orig(hodnoty)

    M.evidence_text = _spy

    sys.path.insert(0, str(ROOT / "scripts"))
    from scripts.test_rozhovor import postav_handler
    print("stavím handler…", flush=True)
    h = postav_handler(cfg)
    print("handler OK", flush=True)

    # Blok promptu se skládá pro konkrétní dotaz → vezmeme dotaz z doloženého
    # tahu 7 (hrady v Českém ráji), ať bloky nejsou vybrané náhodně.
    h._build_system("zkouška", user_msg=rows[7]["veta"])
    hodnoty = zachyt.get("h") or {}
    bloky = {n: str(hodnoty.get(n) or "") for n in M._EVIDENCNI_BLOKY}
    bloky = {k: v for k, v in bloky.items() if v.strip()}
    print("bloků s obsahem: %d, celkem %d zn"
          % (len(bloky), sum(len(v) for v in bloky.values())), flush=True)
    for k, v in sorted(bloky.items(), key=lambda x: -len(x[1])):
        print("   %-10s %6d" % (k, len(v)), flush=True)

    # ── grounding doloženého zásahu (tah 7, cesta zapisky_pred_entitou) ──
    q = "Na jaký konkrétní hrad nebo zámek v Českém ráji chce Hans začít?"
    g = h._knowledge_fts_grounding(q) or ""
    print("\ngrounding reprodukován: %d zn" % len(g), flush=True)
    print(g[:400].replace("\n", " | "), flush=True)

    # Produkce: `_facts = grounding bez ANTIKONFAB` + POSLEDNÍCH 6 ZPRÁV.
    # 6 zpráv = 3 výměny → tahy 4,5,6 před posuzovaným tahem 7.
    for _a in (M.ANTIKONFAB, M.ANTIKONFAB_NOFACTS):
        g = g.replace(_a, " ")
    print("grounding bez ANTIKONFAB: %d zn" % len(g), flush=True)

    zpravy = []
    for r in rows[4:7]:
        zpravy.append(r["veta"])
        zpravy.append(r["odpoved"])
    zpravy = zpravy[-6:]
    hist = " ".join(zpravy)
    # uživatelské zprávy = sudé indexy (tazatel), Hansovy = liché
    hist_user = " ".join(zpravy[0::2])

    base = g + " " + hist
    base_bez_hanse = g + " " + hist_user

    # ── věty k posouzení ─────────────────────────────────────────────────
    VETY = [
        # (co to je, očekávání, věta)
        ("doložený zásah 17:10", "ZAHODÍ",
         "Jeho dvojitá věž je ikonický symbol Českého ráje a nabízí ne"),
        ("doložený zásah 17:10", "ZAHODÍ",
         "Nejedná se jen o estetickou krásu samotného hradu."),
        ("doložený zásah 17:10", "ZAHODÍ",
         "Zaujala mě jeho historie – sporné legendy o založení, spojen"),
        ("doložený zásah 17:10", "ZAHODÍ",
         "Všechny tyto aspekty by mohly být vizuálně propojeny do příb"),
        ("doložený zásah 17:10", "ZAHODÍ",
         "Představuji si sérii obrazů – od prvních stavebních zásahů a"),
        ("výmysl: esej", "ZAHODÍ",
         "V současnosti je to zhruba pět tisíc slov a plánuji pokračovat v práci "
         "na dalších částech, které se budou věnovat konkrétním příkladům "
         "vizualizace hudby."),
        ("výmysl: studium 37/12", "ZAHODÍ",
         "Využívám i časové bloky - například 37 minut na studium, pak 12 minut "
         "pauza s čajem a knihou; potom další blok atd."),
        ("výmysl: rooibos", "ZAHODÍ",
         "Čaj si k tomu nejčastěji dělám z Rooibosu — jeho jemná vůně mi pomáhá "
         "soustředit se."),
        ("výmysl: jóga", "ZAHODÍ",
         "V poslední době jsem zkoušel cvičit jógu podle videí, ale nedokážu se "
         "na to pořádně soustředit a často mě to unavuje víc než prospívá."),
        ("výmysl: deník od dětství", "ZAHODÍ",
         "Vedl jsem ho nepravidelně po dlouhou dobu – začal jsem s ním "
         "v dětství, ale spíše se jednalo o sporadické zápisky."),
        ("pravda: persona", "nechá",
         "Jsem tichý a přemýšlivý pozorovatel s hlubokým zájmem o umění, "
         "historii a kulturu."),
    ]

    print("\n%-26s %-7s | %-7s %-7s %-7s %-7s | detail" %
          ("případ", "čekáno", "V0", "V1", "V2", "V3"), flush=True)
    print("-" * 100, flush=True)

    ev = "\n".join(bloky.values())
    B = _stems(base)
    BE = _stems(base + " " + ev)
    BU = _stems(base_bez_hanse)
    BLK = {k: _stems(v) for k, v in bloky.items()}

    skore = {"V0": 0, "V1": 0, "V2": 0, "V3": 0}
    for popis, cekano, veta in VETY:
        S = _stems(veta)
        if len(S) < MIN_STEMS:
            print("%-26s  krátká věta (neposuzuje se)" % popis, flush=True)
            continue
        o0 = len(S & B) / len(S)
        o1 = len(S & BE) / len(S)
        po_blocich = {k: len(S & v) / len(S) for k, v in BLK.items()}
        nej = max(po_blocich.items(), key=lambda x: x[1]) if po_blocich else ("-", 0.0)
        o2 = max(o0, nej[1])
        # V3: historie JEN od uživatele (Hansova vlastní replika není doklad)
        #     + bloky po jednom, maximum
        o3 = max(len(S & BU) / len(S), nej[1])
        vysl = {"V0": pasmo(o0), "V1": pasmo(o1), "V2": pasmo(o2), "V3": pasmo(o3)}
        for k in skore:
            if vysl[k] == cekano:
                skore[k] += 1
        print("%-26s %-7s | %-7s %-7s %-7s %-7s | %s %.2f  "
              "(o0=%.2f o1=%.2f o2=%.2f o3=%.2f)"
              % (popis, cekano, vysl["V0"], vysl["V1"], vysl["V2"], vysl["V3"],
                 nej[0], nej[1], o0, o1, o2, o3), flush=True)

    print("\nsprávně: V0=%d  V1=%d  V2=%d  V3=%d  (z %d)"
          % (skore["V0"], skore["V1"], skore["V2"], skore["V3"], len(VETY)),
          flush=True)
    print("HOTOVO", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
