# -*- coding: utf-8 -*-
"""HANS_LESSON_BY_TOPIC_V1 — lekce se hledají podle TÉMATU a bez expirace.

Deterministický (žádný LLM): dočasná DB s fixture lekcemi.
Spuštění:  python3 tests/test_lessons_by_topic.py
"""
import json
import os
import sqlite3
import sys
import tempfile
import time

sys.path.insert(0, ".")

from scripts.hans_lessons import lessons_for_topic

# (note, claim, correction)
FIXTURES = [
    ("Měl jsem si ověřit polohu hradu Gutštejna.",
     "Hrad Gutštejn je součástí Českého ráje.",
     "Hrad Gutštejn není v Českém ráji."),
    ("Musím ověřovat informace, než je sdělím.",
     "",
     "MS ve fotbale 2026 pořádají USA, Kanada a Mexiko, ne Jižní Korea."),
    ("Nemám si domýšlet podobu parku.",
     "Zámecký park u hradu Kost je rozsáhlý areál.",
     "Hrad Kost nemá zámecký park."),
    ("Musím si ověřit, kdo je kdo v rodině.",
     "Klára je matka.",
     "Klára je dcera, Jana je partnerka Standy."),
    # šum: obecná ponaučení bez konkrétní entity se nesmí vracet na cokoliv
    ("Nemám být příliš rozvláčný.", "", "Mluv stručněji, pane Hansi."),
    ("Nemám soudit lidské preference.", "", "Můj pohled je omezený."),
]

CASES = [
    # (dotaz, podřetězec, který MUSÍ být v první trefě | None = nesmí nic vrátit)
    ("jake hrady se nachazeji v ceskem raji?", "Gutštejn"),
    ("co mi muzes rici o ceskem raji?", "Gutštejn"),
    ("kde lezi hrad Gutstejn?", "Gutštejn"),
    ("ma hrad Kost zamecky park?", "Kost"),
    ("kdo porada MS ve fotbale 2026?", "MS ve fotbale"),
    ("kdo je Klara?", "Klára"),
    ("uvar mi kafe", None),
    ("kolik je hodin?", None),
    ("zahraj hudbu", None),
]


def build_db(path: str) -> None:
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE diary (id INTEGER PRIMARY KEY AUTOINCREMENT, "
               "ts REAL, event_type TEXT, title TEXT, note TEXT, data TEXT)")
    # ts hluboko v minulosti — dokazuje, že tahle cesta NEexpiruje
    old = time.time() - 90 * 86400
    for i, (note, claim, corr) in enumerate(FIXTURES):
        db.execute("INSERT INTO diary (ts, event_type, title, note, data) "
                   "VALUES (?,?,?,?,?)",
                   (old + i, "lesson_learned", "standa", note,
                    json.dumps({"claim": claim, "correction": corr},
                               ensure_ascii=False)))
    db.commit()
    db.close()


def main() -> int:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        build_db(path)
        ok = 0
        for query, want in CASES:
            got = lessons_for_topic(path, query, limit=2)
            if want is None:
                good = (len(got) == 0)
            else:
                good = bool(got) and want in got[0]
            ok += good
            print("%s  %-42s -> %s"
                  % ("OK  " if good else "CHYBA", query[:42],
                     (got[0][:60] if got else "(nic)")))
        print("\n%d/%d" % (ok, len(CASES)))
        return 0 if ok == len(CASES) else 1
    finally:
        os.unlink(path)


if __name__ == "__main__":
    sys.exit(main())
