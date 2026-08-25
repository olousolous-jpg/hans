# -*- coding: utf-8 -*-
"""HANS_READING_KODI_SPLIT_V1 — článek k přehrávanému filmu není Hansova četba.

Nález uživatele 24.8.: „dotaz na četbu — do ní míchá filmy."
Řetěz: `kodi_playing` → MOVIE_GROUNDING_V1 si o filmu přečte Wikipedii →
`web_read` + `reading_takeaway`. Zápis je správný, jen to není JEHO četba.

Dvě různá chování, obě se tu hlídají:
  • výpis „co jsi četl" (bez tématu) → filmy VEN;
  • cílený dotaz „četl jsi o X?"      → ZŮSTANE, jen se označí „(k filmu)".
    Vyloučit i tady by bylo FALEŠNÉ ZAPŘENÍ.

⚠️ Hlídá i to, proč nestačí `json_extract(data,'$.topic')='kodi'`: příznak
nese jen `web_read`, `reading_takeaway` se pozná až podle SHODNÉHO TITULU.

Deterministický (žádný LLM). Spuštění: python3 tests/test_reading_kodi_split.py
"""
import json
import os
import sqlite3
import sys
import tempfile
import time

sys.path.insert(0, ".")

from scripts.hans_recall import reading_answer

NOW = time.time()

# (odstup_hodin, event_type, title, note, data)
FIXTURES = [
    # ── film: článek NESE topic=kodi, reflexe NE (pozná se podle titulu) ──
    (5, "web_read", "Jakubův žebřík", "[kodi] Jakubův žebřík je americký film.",
     json.dumps({"pending": 0, "topic": "kodi", "raw_text": "..."})),
    (4, "reading_takeaway", "Jakubův žebřík", "", "Článek mě zaujal filmovou symbolikou."),
    (7, "web_read", "Na sever severozápadní linkou", "[kodi] Thriller z roku 1959.",
     json.dumps({"pending": 0, "topic": "kodi", "raw_text": "..."})),
    (6, "reading_takeaway", "Na sever severozápadní linkou", "", "Hitchcockovo tempo je pozoruhodné."),
    # ── vlastní četba: musí zůstat ──
    (3, "web_read", "Gotické umění", "[interest] Gotika vznikla ve Francii.",
     json.dumps({"pending": 0, "topic": "interest", "raw_text": "..."})),
    (2, "book_read", "Pride and Prejudice — kap. 61", "", "Poslední kapitola."),
    (8, "study_note", "Studium: Jára Cimrman", "", "Poznámka ze studia."),
    # ── past: KNIHA se stejným názvem jako film se schovat NESMÍ ──
    (9, "book_reflection", "Jakubův žebřík", "", "Kniha mě dojala."),
    # ── past: starší web_read s PRÁZDNÝM `data` (1892 takových v DB) ──
    #    `json_extract` na nich shodí dotaz, pokud chybí `json_valid` gate.
    (10, "web_read", "Starý článek bez dat", "[interest] Cokoliv.", ""),
]


def _fixture_db(path):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE diary (id INTEGER PRIMARY KEY, ts REAL, "
                 "event_type TEXT, title TEXT, note TEXT, data TEXT)")
    for hod, et, title, note, data in FIXTURES:
        conn.execute("INSERT INTO diary (ts, event_type, title, note, data) "
                     "VALUES (?,?,?,?,?)",
                     (NOW - hod * 3600, et, title, note, data))
    conn.commit()
    conn.close()


CASES = [
    # (popis, dotaz, musí_obsahovat, nesmí_obsahovat)
    ("výpis četby: film s topic=kodi je pryč",
     "", [], ["Na sever severozápadní linkou"]),
    ("výpis četby: reflexe k filmu (BEZ topic) je taky pryč",
     "", [], ["Hitchcock"]),
    ("výpis četby: vlastní čtení zůstává",
     "", ["Gotické umění", "Pride and Prejudice"], []),
    ("výpis četby: studium zůstává",
     "", ["Cimrman"], []),
    ("výpis četby: KNIHA se jménem filmu se schovat nesmí",
     "", ["Jakubův žebřík"], []),
    ("cílený dotaz na film NEZAPÍRÁ a označí (k filmu)",
     "četl jsi o Jakubově žebříku?", ["Jakubův žebřík", "(k filmu)"], []),
    ("cílený dotaz na vlastní četbu se neoznačuje",
     "co víš o gotickém umění?", ["Gotické umění"], ["(k filmu)"]),
]


def main():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        _fixture_db(path)
        ok = bad = 0
        for popis, dotaz, musi, nesmi in CASES:
            out = reading_answer(path, dotaz, limit=10) or ""
            chyby = [f"chybí {m!r}" for m in musi if m not in out]
            chyby += [f"nemá tam být {n!r}" for n in nesmi if n in out]
            if chyby:
                bad += 1
                print("   CHYBA  %s → %s" % (popis, "; ".join(chyby)))
                print("          výstup: %s" % out.replace("\n", " | ")[:220])
            else:
                ok += 1
                print("   OK     %s" % popis)
        print("\n%d/%d prošlo" % (ok, ok + bad))
        return 1 if bad else 0
    finally:
        os.unlink(path)


if __name__ == "__main__":
    sys.exit(main())
