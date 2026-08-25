#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Vrátí do deníku dva agent_action řádky, které 25.8. omylem smazal
`test_rozhovor.py --uklid` (mazal agent_action podle ČASOVÉHO OKNA, takže
spolkl i reálnou uživatelovu výměnu z Matrixu vedle testovacích).

Rekonstrukce je z data/system.log (časy, akce, confidence) + z tvaru, který
píše hans_agent (_confirm_question / _run_add_study) — ne z hlavy.
Idempotentní: podruhé nic nevloží.
"""
import sqlite3, datetime, json, sys

DB = "data/hans_diary.db"
TZ = datetime.timezone(datetime.timedelta(hours=2))  # CEST
TEMA = "Norimberský proces"


def epoch(hhmmss):
    h, m, s = (int(x) for x in hhmmss.split(":"))
    return datetime.datetime(2026, 8, 25, h, m, s, tzinfo=TZ).timestamp()


RADKY = [
    (epoch("16:00:45"), "add_study_topic → proposed", "proposed",
     "Mám si „%s“ zařadit ke studiu?" % TEMA),
    (epoch("16:01:30"), "add_study_topic → accepted", "accepted",
     "Dobře — „%s“ jsem si zařadil do studijního plánu. "
     "Pustím se do něj, jakmile dokončím současné studium." % TEMA),
]

con = sqlite3.connect(DB)
vlozeno = 0
for ts, title, outcome, note in RADKY:
    uz = con.execute(
        "SELECT id FROM diary WHERE event_type='agent_action' AND title=? "
        "AND ts BETWEEN ? AND ?", (title, ts - 90, ts + 90)).fetchone()
    if uz:
        print("   už tam je (id=%s): %s" % (uz[0], title))
        continue
    data = json.dumps({"action": "add_study_topic", "args": {"tema": TEMA},
                       "outcome": outcome, "confidence": 0.95},
                      ensure_ascii=False)
    cur = con.execute(
        "INSERT INTO diary (ts, event_type, title, data, note) "
        "VALUES (?, 'agent_action', ?, ?, ?)", (ts, title, data, note))
    print("   vloženo id=%s  %s  %s" % (
        cur.lastrowid,
        datetime.datetime.fromtimestamp(ts, TZ).strftime("%Y-%m-%d %H:%M:%S"),
        title))
    vlozeno += 1
con.commit()
con.close()
print("Vloženo %d řádků." % vlozeno)
