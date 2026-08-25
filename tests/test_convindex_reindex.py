# -*- coding: utf-8 -*-
"""HANS_CONVINDEX_REINDEX_V1 — změněný deníkový řádek se promítne do indexu.

Proč to existuje (25.8.): `conv_fts` je contentless FTS5 a `sync()` bere jen
`id > MAX(id)` per zdroj → PŘEPSANÝ deníkový řádek se do indexu nikdy nedostal.
Doloženo: 19 `web_read` drželo v indexu marker „(nezpracováno…)" jako Hansovu
znalost, zatímco v deníku už bylo skutečné shrnutí.

Hlídá i past, kvůli které to nešlo opravit naivně: nad EXISTUJÍCÍM řádkem
`DELETE FROM conv_fts` v contentless tabulce vyhodí výjimku.

Deterministický (žádný LLM). Spuštění: python3 tests/test_convindex_reindex.py
"""
import json
import os
import sqlite3
import sys
import tempfile
import time

sys.path.insert(0, ".")

import scripts.hans_convindex as ci

# ⚠️ `search()` má `auto_sync=True` a VÝCHOZÍ `diary_path` míří na PRODUKČNÍ
# deník — bez `auto_sync=False` si test natáhne celou ostrou databázi do své
# dočasné a měří něco úplně jiného (stálo mě to jedno kolo 25.8.).

NOW = time.time()
MARKER = "[interest] (nezpracováno — mozek byl mimo, doženu to)"
SKUTECNY = "[interest] Bandoneon jest hudební nástroj, druh knoflíkové harmoniky."


def main():
    tmp = tempfile.mkdtemp()
    diary = os.path.join(tmp, "d.db")
    index = os.path.join(tmp, "i.db")
    puvodni = ci.INDEX_PATH
    ci.INDEX_PATH = index
    ok = bad = 0

    def zkouska(popis, podminka):
        nonlocal ok, bad
        if podminka:
            ok += 1; print("   OK     %s" % popis)
        else:
            bad += 1; print("   CHYBA  %s" % popis)

    try:
        d = sqlite3.connect(diary)
        d.execute("CREATE TABLE diary (id INTEGER PRIMARY KEY, ts REAL, "
                  "event_type TEXT, title TEXT, note TEXT, data TEXT)")
        d.execute("INSERT INTO diary (id, ts, event_type, title, note, data) "
                  "VALUES (1,?,'web_read','Bandoneon',?,?)",
                  (NOW, MARKER, json.dumps({"pending": 1, "raw_text": "x"})))
        d.commit(); d.close()

        # 1) prvni sync zaindexuje marker (tak to v praxi vzniklo)
        ci.sync(diary_path=diary, index_path=index)
        cv = sqlite3.connect(index)
        txt = cv.execute("SELECT text FROM conv_doc WHERE id=1").fetchone()
        cv.close()
        zkouska("první sync zaindexuje řádek", txt is not None)
        zkouska("index zprvu drží marker", txt and "nezpracováno" in txt[0])
        zkouska("fulltext marker najde", len(ci.search("nezpracováno", index_path=index,
                                       diary_path=diary, auto_sync=False)) > 0)

        # 2) catchup prepise denikovy radek
        d = sqlite3.connect(diary)
        d.execute("UPDATE diary SET note=?, data=? WHERE id=1",
                  (SKUTECNY, json.dumps({"pending": 0, "raw_text": "x"})))
        d.commit(); d.close()

        # 3) samotny sync to NEZACHYTI (watermark bere jen nova id)
        ci.sync(diary_path=diary, index_path=index)
        cv = sqlite3.connect(index)
        po_syncu = cv.execute("SELECT text FROM conv_doc WHERE id=1").fetchone()[0]
        cv.close()
        zkouska("sync sám změnu nezachytí (doklad příčiny)",
                "nezpracováno" in po_syncu)

        # 4) reindex ANO — a to je pointa opravy
        n = ci.reindex([1], diary)
        zkouska("reindex ohlásí 1 přepsaný řádek", n == 1)
        cv = sqlite3.connect(index)
        po = cv.execute("SELECT text FROM conv_doc WHERE id=1").fetchone()[0]
        cv.close()
        zkouska("conv_doc má skutečné shrnutí", "Bandoneon jest" in po)
        zkouska("marker je z conv_doc pryč", "nezpracováno" not in po)

        # 5) FTS musi jit S NIM — jinak by index dal vracel stary text
        zkouska("fulltext už marker NEnajde",
                len(ci.search("nezpracováno", index_path=index,
                          diary_path=diary, auto_sync=False)) == 0)
        zkouska("fulltext najde nový obsah",
                len(ci.search("harmoniky", index_path=index,
                          diary_path=diary, auto_sync=False)) > 0)

        # 6) idempotence — druhy beh nesmi nic delat ani duplikovat
        n2 = ci.reindex([1], diary)
        zkouska("opakovaný reindex je no-op", n2 == 0)
        zkouska("v FTS není duplikát",
                len(ci.search("harmoniky", index_path=index,
                          diary_path=diary, auto_sync=False)) == 1)

        print("\n%d/%d prošlo" % (ok, ok + bad))
        return 1 if bad else 0
    finally:
        ci.INDEX_PATH = puvodni
        for f in (diary, index):
            try: os.unlink(f)
            except OSError: pass
        try: os.rmdir(tmp)
        except OSError: pass


if __name__ == "__main__":
    sys.exit(main())
