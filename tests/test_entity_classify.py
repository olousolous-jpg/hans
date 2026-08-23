#!/usr/bin/env python3
"""HANS_ENTITY_CLASSIFY_V3 — trvalý test klasifikace typu entity.

Vznikl z nálezu 18.–23.8.: v `entities` bylo 104 řádků `etype='osoba'`, ale
30 z nich byly filmy, písně, kluby a události („Kde domov můj?", „Zítra vstanu
a opařím se čajem", „Války růží"). Příčinou byl vzor, který bral HOLÉ „byl"
jako důkaz člověka a ptal se první. `person_card` se ptá `resolve(etype='osoba')`,
takže to bylo nastražené na „kdo je …?".

Věty jsou zkrácené definiční věty ze skutečných Wikipedia glosů v DB.
Spuštění:  python3 tests/test_entity_classify.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts.hans_entities import _classify

CASES = [
    # ── původní stížnost: díla vedená jako osoby ──────────────────────────
    ("Zítra vstanu a opařím se čajem je česká filmová sci-fi komedie "
     "natočená v roce 1977.", "dílo"),
    ("Kde domov můj je původně píseň o dvou slokách, složená pro postavu "
     "slepého houslisty.", "dílo"),
    ("Indiana Jones a království křišťálové lebky je název v pořadí "
     "čtvrtého filmu.", "dílo"),
    ("Rotherham Town FC byl anglický fotbalový klub, který sídlil ve "
     "městě Rotherham.", "organizace"),
    ("Války růží (1455–1487) byla série bitev občanské války v Anglii.", "událost"),
    ("Adidas Yeezy byla módní spolupráce mezi americkým rapperem a "
     "módním návrhářem.", "pojem"),
    ("Bauhaus byla škola výtvarného umění, která vznikla v roce 1919.", "organizace"),
    ("Pompeje byly starověké město v oblasti Kampánie.", "místo"),
    ("Public TV byla česká televizní stanice.", "organizace"),
    ("FalconSAT-2 byla družice postavená studenty akademie letectva.", "pojem"),

    # ── lidé musí zůstat lidmi (regrese opačným směrem) ───────────────────
    ("Arthur Ignatius Conan Doyle byl britský spisovatel.", "osoba"),
    ("John Howard Carpenter je americký filmový režisér, producent "
     "a skladatel.", "osoba"),
    ("Pavel Rímský je český divadelní, filmový, televizní, rozhlasový "
     "a dabingový herec.", "osoba"),
    ("Marie Kšajtová je česká redaktorka, scenáristka a dramaturgyně.", "osoba"),
    ("Richard Sorge byl sovětský špion v Japonsku za druhé světové války "
     "a Hrdina Sovětského svazu.", "osoba"),
    ("Gustav Thöni byl italský alpský lyžař a olympijský vítěz.", "osoba"),
    ("Lionel Andrés Messi je argentinský fotbalista.", "osoba"),

    # ── 2. pád mluví o díle, ne o člověku ─────────────────────────────────
    ("Pelíšky jsou česká filmová komedie režiséra Jana Hřebejka.", "dílo"),
    ("Pardubické krematorium bylo postaveno dle plánů architekta "
     "Pavla Janáka.", "pojem"),
    ("Avatar: The Way of Water je dobrodružný sci-fi film, který natočil "
     "režisér James Cameron.", "dílo"),

    # ── přídavné jméno není podstatné ─────────────────────────────────────
    ("Španělsko-portugalská státní hranice je hranice mezi dvěma státy.", "místo"),
    ("Dvůr Králové nad Labem je město v okrese Trutnov.", "místo"),
    ("Gotika je umělecký sloh navazující na sloh románský.", "pojem"),
    ("Skyfall je dvacátá třetí filmová bondovka produkovaná Eon "
     "Productions.", "dílo"),

    # ── fikce ──────────────────────────────────────────────────────────────
    ("Sherlock Holmes je literární postava, kterou vytvořil Arthur "
     "Conan Doyle.", "postava"),
    ("Hercule Poirot je nejznámější postava britské spisovatelky "
     "Agathy Christie.", "postava"),
    ("Loupežník Rumcajs je pohádková postava z Večerníčku.", "postava"),

    # ── další druhy ────────────────────────────────────────────────────────
    ("Plitvická jezera jsou nejznámější chorvatský národní park.", "místo"),
    ("Mistrovství světa ve fotbale je nejdůležitější mezinárodní "
     "soutěž v kopané.", "událost"),
    ("Vyprávěj je český televizní retroseriál.", "dílo"),
]


def main() -> int:
    ok = bad = 0
    for gloss, want in CASES:
        got = _classify(gloss)
        if got == want:
            ok += 1
            print(f"  ✓ {want:11s} | {gloss[:62]}")
        else:
            bad += 1
            print(f"  ✗ {got:11s} (čekáno {want}) | {gloss[:62]}")
    print(f"\nOK={ok}  CHYB={bad}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
