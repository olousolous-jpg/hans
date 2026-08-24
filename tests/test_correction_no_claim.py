# -*- coding: utf-8 -*-
"""HANS_CORRECTION_NO_CLAIM_V1 — regresní sada k uvolnění `claim` na nepovinný.

⚠️ POTŘEBUJE ŽIVÝ LLM (base OpenEuroLLM na PC) — proto NENÍ v tichém CI běhounu.
Spouštět ručně:  python3 tests/test_correction_no_claim.py

Hlídá OBĚ strany zásahu:
  • pozitivní — oprava s odstupem (Hansovo tvrzení mimo okno) se ZACHYTÍ,
  • negativní — názor/vkus/otázka/nesouhlas lekci NEVYROBÍ.
Druhá půlka je důležitější: uvolnění promptu smí přidat záchyt, ne konfabulaci.
"""
import json
import sys

sys.path.insert(0, ".")

from scripts import hans_lessons as hl
from scripts.hans_persona import persona_name
from scripts.ollama_client import ollama_generate

# (jméno, přepis, očekává se aspoň jedna lekce?)
CASES = [
    ("oprava s odstupem — Gutštejn (reálný případ 28.7.)", """osoba: copak jsi studoval?
Hans: Studoval jsem Český ráj a okolní hrady. Záznamy uvádějí, že Český ráj je chráněná krajinná oblast na severovýchodě Čech.

osoba: az na ten hrad Gutštejn na soutokem ohre a knajny je to dobre. Hrad gutstejn neni v ceykem raji.
Hans: K tomuhle nemám spolehlivý záznam a nerad bych si domýšlel, pane.""", True),
    ("oprava s odstupem — MS 2026, claim vůbec není v přepisu", """osoba: mimochodem, MS ve fotbale 2026 nehraje Jizni Korea, poradaji to USA, Kanada a Mexiko.
Hans: Děkuji za upřesnění, Stando. Poznamenal jsem si to.""", True),
    ("jiný názor + vkus", """osoba: mne se ten film vubec nelibil, byl nudny.
Hans: Chápu, Stando. Mně naopak přišel pozoruhodný svým tempem.
osoba: to je asi vec vkusu. radsi mam akcni filmy nez pomale drama.
Hans: Rozumím. Zaznamenal jsem si Vaši preferenci.""", False),
    ("běžná otázka", """osoba: jake je dneska pocasi?
Hans: Dnes je oblačno, Stando, kolem sedmnácti stupňů.
osoba: aha, tak to si vezmu bundu.
Hans: Rozumná volba, pane.""", False),
    ("nesouhlas bez opravy faktu", """osoba: mel bys byt strucnejsi.
Hans: Omlouvám se, Stando. Pokusím se odpovídat kratčeji.
osoba: dobre.
Hans: Děkuji za zpětnou vazbu.""", False),
]


def main() -> int:
    cfg = json.load(open("config.json"))
    system = hl._SYSTEM.format(persona_name=persona_name(cfg))
    model = str((cfg.get("corrections", {}) or {}).get(
        "model", "jobautomation/OpenEuroLLM-Czech:latest"))
    ok = 0
    for name, transcript, want in CASES:
        raw = ollama_generate(model=model, prompt=transcript, system=system,
                              config=cfg, timeout=280, keep_alive=0,
                              options={"temperature": 0.1})
        items = [i for i in hl._extract_json_array(raw)
                 if isinstance(i, dict)
                 and len(str(i.get("lesson", "") or "").strip()) >= 8]
        got = bool(items)
        good = (got == want)
        ok += good
        print("%s  %-52s ceka=%-5s dostal=%s"
              % ("OK  " if good else "CHYBA", name[:52], want, got))
        for i in items:
            print("        ", json.dumps(i, ensure_ascii=False))
    print("\n%d/%d" % (ok, len(CASES)))
    return 0 if ok == len(CASES) else 1


if __name__ == "__main__":
    sys.exit(main())
