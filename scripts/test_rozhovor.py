#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_rozhovor.py — simulovaný rozhovor s Hansem proti ŽIVÉMU stacku.

PROČ EXISTUJE (21.8.): testovací rozhovor je nejlepší způsob, jak zjistit, jak
si Hans po opravách stojí — jenže postavit ho zabere pokaždé půl hodiny, než
se doklepe, co všechno musí být napojené. Tenhle skript to napojení drží
jednou provždy, a hlavně po sobě UKLIDÍ: bez úklidu si Hans testovací hovor
zapíše do deníku jako skutečný a vybavuje si ho pak jako vzpomínku.

CO DĚLÁ:
  • napojí to, co potřebuje chatová cesta v provozu (HansIdle + Kodi, RAG,
    paměť) — atrapa NESTAČÍ, chybějící metody se projeví až uprostřed testu,
  • pošle zprávy pod ČERSTVÝM jménem (jinak předchozí běh ovlivní router —
    doloženo 21.8.: podruhé pod týmž jménem šel dotaz na obsazení na akci),
  • vypíše odpovědi i časy,
  • `--uklid` smaže, co test v deníku vyrobil, a Hansovo VLASTNÍ dění z té
    doby nechá být (maže se po položkách, ne plošným návratem zálohy).

⚠️ ÚKLID MAŽE JEN SVÉ (TEST_ROZHOVOR_UKLID_PERSON_V1, 26.8.): `agent_action` se
dřív mazal podle ČASOVÉHO OKNA, bez ohledu na to, čí je — a 25.8. tím spolkl
reálné potvrzení, které uživatel poslal Hansovi z Matrixu, protože náhodou
spadlo doprostřed testu. Teď se maže jen řádek, jehož `data.person` se shoduje
s testovacím mluvčím. Starší řádky (bez `person`) se NEMAŽOU — jen se vypíšou,
ať je vidět, že tam byly.

POUŽITÍ:
  python3 scripts/test_rozhovor.py -m "co jsi dnes cetl?" -m "a jak se ti to libi?"
  python3 scripts/test_rozhovor.py -f otazky.txt --uklid
  python3 scripts/test_rozhovor.py -f otazky.txt --jmeno Host9   # bez úklidu

⚠️ Rozhovor běží proti PC (model) — když je PC vypnuté, testy se nedočkají.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _cesta_db(cfg: dict) -> str:
    return (cfg.get("diary_db")
            or (cfg.get("hans_idle", {}) or {}).get("diary_db")
            or "data/hans_diary.db")


def postav_handler(cfg: dict):
    """Napojení jako v provozu. Co se nepovede, hlásí se nahlas — tichý
    výpadek RAG by z testu udělal měření něčeho jiného."""
    from scripts.openwebui_direct_handler import OpenWebUIDirectHandler
    from scripts.kodi_client import KodiClient
    from scripts.hans_idle import HansIdle

    h = OpenWebUIDirectHandler(cfg)
    h._hans_idle = HansIdle(cfg, KodiClient(cfg), h)
    try:
        from scripts.hans_knowledge import HansKnowledge
        h.set_knowledge(HansKnowledge(cfg))
    except Exception as e:
        print("⚠️  RAG nenapojen: %s" % e)
    try:
        from scripts.hans_memory import Memory
        h.set_memory(Memory(cfg))
    except Exception as e:
        print("⚠️  paměť nenapojena: %s" % e)
    return h


def _je_muj(data_json: str, jmeno: str) -> bool:
    """TEST_ROZHOVOR_UKLID_PERSON_V1 — vyrobil tenhle `agent_action` test?

    Rozhoduje `data.person` (HANS_AGENT_LOG_PERSON_V1). Když klíč chybí (řádky
    z doby před 26.8.) nebo se data nedají přečíst, vrací False = NEMAZAT.
    Radši nechat cizí řádek ležet než smazat uživatelovu skutečnou akci."""
    try:
        return json.loads(data_json or "{}").get("person") == jmeno
    except Exception:
        return False


def uklid(db: str, jmeno: str, znacka: int, od_ts: float, do_ts: float,
          jen_ukazat: bool = False) -> int:
    """Smaž, co test vyrobil. Hansovo vlastní dění z té doby ZŮSTÁVÁ.

    U `agent_action` nestačí časové okno — do něj spadne i to, co mezitím udělal
    skutečný uživatel (25.8. tak zmizelo potvrzení poslané z Matrixu). Proto se
    maže jen řádek s odpovídajícím `data.person`."""
    con = sqlite3.connect(db)
    radky = con.execute(
        "SELECT id, datetime(ts,'unixepoch','localtime'), event_type, title, "
        "       coalesce(data,'') "
        "FROM diary WHERE id > ? AND ("
        "  (event_type='human_chat' AND title=?) "
        "  OR (event_type='agent_action' AND ts BETWEEN ? AND ?)) ORDER BY id",
        (znacka, jmeno, od_ts, do_ts)).fetchall()

    vyber, cizi = [], []
    for r in radky:
        if r[2] == "agent_action" and not _je_muj(r[4], jmeno):
            cizi.append(r)
        else:
            vyber.append(r)

    for r in vyber:
        print("   %s  %-14s %s" % (r[1], r[2], (r[3] or "")[:44]))
    if cizi:
        print("   — v okně, ale NENÍ z testu (nechávám): —")
        for r in cizi:
            print("   %s  %-14s %s" % (r[1], r[2], (r[3] or "")[:44]))

    if not jen_ukazat and vyber:
        con.execute("DELETE FROM diary WHERE id IN (%s)"
                    % ",".join(str(r[0]) for r in vyber))
        con.commit()
    con.close()

    # HANS_CONVINDEX_FORGET_V1 (26.8.) — smazat z DENÍKU nestačí. Rozhovor je
    # mezitím zaindexovaný ve fulltextu, takže testovací hovor zůstával
    # VYHLEDATELNÝ a Hans si ho vybavoval jako skutečný (doloženo: dotaz
    # „severnim kridle" vrátil hodinu starý smazaný test i s konfabulací).
    # Vzor „nová věc patří do OBOU cest" z CLAUDE.md platí i na mazání.
    if not jen_ukazat and vyber:
        try:
            from scripts.hans_convindex import forget
            n_idx = forget([r[0] for r in vyber])
            if n_idx:
                print("   z fulltextu odstraněno %d řádků" % n_idx)
        except Exception as e:
            print("   ⚠️  index se vyčistit nepodařilo (%s) — testovací hovor "
                  "zůstal vyhledatelný!" % e)
    p = Path("data/conversations/%s.json" % jmeno)
    if not jen_ukazat and p.exists():
        p.unlink()
        print("   smazán %s" % p)
    return len(vyber)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("-m", "--zprava", action="append", default=[],
                    help="zpráva (lze opakovat)")
    ap.add_argument("-f", "--soubor", help="soubor se zprávami (řádek = tah)")
    ap.add_argument("--jmeno", help="jméno mluvčího (výchozí: čerstvé Test<čas>)")
    ap.add_argument("--uklid", action="store_true",
                    help="po testu smazat, co vyrobil v deníku")
    a = ap.parse_args()

    zpravy = list(a.zprava)
    if a.soubor:
        zpravy += [r.strip() for r in Path(a.soubor).read_text(
            encoding="utf-8").splitlines() if r.strip()
            and not r.strip().startswith("#")]
    if not zpravy:
        ap.error("žádné zprávy — použij -m nebo -f")

    # ČERSTVÉ jméno: historie předchozího běhu mění chování routeru.
    jmeno = a.jmeno or ("Test%s" % time.strftime("%H%M%S"))
    cfg = json.loads(Path("config.json").read_text(encoding="utf-8"))
    db = _cesta_db(cfg)
    znacka = sqlite3.connect(db).execute(
        "SELECT MAX(id) FROM diary").fetchone()[0] or 0
    od_ts = time.time()

    print("═" * 78)
    print("mluvčí: %s   zpráv: %d   značka v deníku: %d"
          % (jmeno, len(zpravy), znacka))
    print("═" * 78)
    h = postav_handler(cfg)
    for i, msg in enumerate(zpravy, 1):
        print("─" * 78)
        print("[%d/%d] %s: %s" % (i, len(zpravy), jmeno, msg))
        t0 = time.time()
        try:
            odp = h.send_chat_message(jmeno, msg, channel="web")
        except Exception as e:
            import traceback
            odp = "!! VÝJIMKA %s: %s\n%s" % (type(e).__name__, e,
                                             traceback.format_exc()[-400:])
        print("Hans (%.0fs): %s" % (time.time() - t0, odp or "(nic)"))

    print("═" * 78)
    print("V deníku po testu přibylo (test = human_chat/%s + agent_action):"
          % jmeno)
    n = uklid(db, jmeno, znacka, od_ts, time.time() + 5,
              jen_ukazat=not a.uklid)
    print("%s %d řádků." % ("Smazáno" if a.uklid else
                            "NEUKLIZENO (spusť s --uklid):", n))
    return 0


if __name__ == "__main__":
    sys.exit(main())
