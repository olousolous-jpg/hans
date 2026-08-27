#!/usr/bin/env python3
"""
HANS_BEHAVIOUR_EVIDENCE_V1 — persona-free evidence o tom, co Hans DĚLAL.

PROČ existuje: Severčin vstup byl z devíti desetin ozvěna. `stances` se
extrahují z večerní reflexe, kterou `hans_synthesis.synthesize` generuje
s předřazeným `persona_core` a se stylovým promptem, který modelu doslova
říká „Máš britskou rezervovanost a smysl pro detail". Nejsilnější Hansovy
postoje jsou pak „Všímám si detailů" (×48) a „Cením si pečlivosti" (×44) —
tedy ta věta zpátky. Severka pak porovnává ROLI s tvrzeními, která z té role
vznikla, a nutně dojde k „drift malý". Koníčky jsou navíc saturované
(13 koníčků, 12 z nich mezi 57–68) → nulová rozlišovací síla.

CO tenhle modul dělá jinak: nebere NIC, co napsal model v Hansově hlase.
Tři vrstvy, každá tvrdší než ta předchozí:

  1. VLASTNÍ POZOROVÁNÍ (`self_insights.insight_en`) — jediný existující
     rozbor, jehož reasoning prompt personu NEOBSAHUJE (persona vstupuje až
     v překladovém kroku, proto se bere ANGLICKÝ originál, ne `insight_cs`).
  2. TVRDÁ ČÍSLA — čím reálně trávil čas. Počítané ze SQL, ne vyprávěné.
  3. ODEZVA SVĚTA — co Hans NEMOHL napsat: co uživatel přijal a co odmítl,
     kolikrát ho opravil, co Hans vzdal, kde mu Koláč postoj oslabil.
     Tahle vrstva je jediná úplně mimo jeho dosah, a proto nejcennější.

⚠️ Vědomě NEOBSAHUJE `spontaneous` (šablony — `HANS_SPONTANEOUS_TEMPLATE_MARK_V1`),
`evening_reflection`, `introspection` ani dialogy s Koláčem: všechno to píše
model v hlase persony. Archiv 10.6. navíc explicitně rozhodl, že Koláč není
zdroj postojů („ten kecá").

API:  block(config, diary_db_path, window_days=60) -> str   ('' když nic)
"""
from __future__ import annotations

import sqlite3
import time

_log = __import__("scripts.logger", fromlist=["get_logger"]).get_logger(
    "hans_behaviour_evidence")

WINDOW_DAYS = 60
_MAX_INSIGHTS = 6


def _ro(path: str):
    return sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=3.0)


def _q1(conn, sql, args=()):
    try:
        r = conn.execute(sql, args).fetchone()
        return (r[0] if r and r[0] is not None else 0)
    except Exception:
        return 0


def _insights(conn, since: float) -> list:
    """Vlastní pozorování — ANGLICKÝ originál (persona-free krok)."""
    try:
        rows = conn.execute(
            "SELECT lens_id, insight_en FROM self_insights "
            "WHERE length(insight_en) > 50 AND ts >= ? "
            "ORDER BY ts DESC LIMIT ?", (since, _MAX_INSIGHTS)).fetchall()
    except Exception:
        return []
    seen, out = set(), []
    for lens, txt in rows:                     # jeden nejnovější na lens
        if lens in seen:
            continue
        seen.add(lens)
        out.append((lens, " ".join((txt or "").split())[:420]))
    return out


def _cinnost(conn, since: float) -> list:
    """Tvrdá čísla — čím trávil čas."""
    d = [
        ("studijních sezení", _q1(conn, "SELECT count(*) FROM diary WHERE "
            "event_type='study_note' AND ts>=?", (since,))),
        ("dostudovaných témat do hloubky", _q1(conn, "SELECT count(*) FROM "
            "study_program WHERE status='completed'")),
        ("namalovaných obrazů", _q1(conn, "SELECT count(*) FROM diary WHERE "
            "event_type='artwork' AND ts>=?", (since,))),
        ("napsaných sekcí díla", _q1(conn, "SELECT count(*) FROM diary WHERE "
            "event_type='writing_section' AND ts>=?", (since,))),
        ("dočtených knih", _q1(conn, "SELECT count(*) FROM diary WHERE "
            "event_type='book_finished' AND ts>=?", (since,))),
        ("uzavřených případů", _q1(conn, "SELECT count(*) FROM diary WHERE "
            "event_type='case_closed' AND ts>=?", (since,))),
        ("přečtených článků", _q1(conn, "SELECT count(*) FROM diary WHERE "
            "event_type='web_read' AND ts>=?", (since,))),
        ("zápisků k filmům", _q1(conn, "SELECT count(*) FROM diary WHERE "
            "event_type='movie_opinion' AND ts>=?", (since,))),
    ]
    return [(k, v) for k, v in d if v]


def _odezva(conn, since: float) -> list:
    """Odezva světa — co Hans nemohl napsat sám."""
    # `agent_action` nese výsledek v TITULKU za šipkou („… → accepted")
    def _agent(stav):
        return _q1(conn, "SELECT count(*) FROM diary WHERE event_type="
                   "'agent_action' AND title LIKE ? AND ts>=?",
                   ("%→ " + stav, since))
    prijato, odmitnuto = _agent("accepted"), _agent("rejected") + _agent("ignored")
    out = []
    if prijato or odmitnuto:
        out.append(("mých návrhů pán přijal / odmítl nebo nechal být",
                    "%d / %d" % (prijato, odmitnuto)))
    n = _q1(conn, "SELECT count(*) FROM diary WHERE event_type IN "
            "('lesson_learned','fact_correction') AND ts>=?", (since,))
    if n:
        out.append(("kolikrát mě pán opravil", str(n)))
    n = _q1(conn, "SELECT count(*) FROM stance_history WHERE event='contradict' "
            "AND ts>=?", (since,))
    if n:
        out.append(("kolikrát jsem v rozepři ustoupil ze svého postoje", str(n)))
    hotovo = _q1(conn, "SELECT count(*) FROM hans_goals WHERE status='completed'")
    vzdano = _q1(conn, "SELECT count(*) FROM hans_goals WHERE status='abandoned'")
    if hotovo or vzdano:
        out.append(("cílů dotažených / vzdaných", "%d / %d" % (hotovo, vzdano)))
    n = _q1(conn, "SELECT count(*) FROM deepen_proposals WHERE status IN "
            "('rejected','expired')")
    if n:
        out.append(("kolikrát pán zamítl, abych šel v tématu hlouběji", str(n)))
    return out


def block(config: dict, diary_db_path: str, window_days: int = None) -> str:
    """Sestaví blok pro Severku. Nikdy nevyhazuje výjimku; '' = není z čeho."""
    if window_days is None:
        window_days = int((config.get("severka", {}) or {}).get(
            "behaviour_window_days", WINDOW_DAYS))
    since = time.time() - window_days * 86400
    conn = None
    try:
        conn = _ro(diary_db_path)
        ins, cin, odz = (_insights(conn, since), _cinnost(conn, since),
                         _odezva(conn, since))
    except Exception as e:
        _log.debug("behaviour block failed: %s", e)
        return ""
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    if not (ins or cin or odz):
        return ""
    p = []
    if cin:
        p.append("Čím jsem strávil posledních %d dní:" % window_days)
        p += ["- %s: %d" % (k, v) for k, v in cin]
    if odz:
        p.append("\nJak na mě reagovalo okolí (tohle jsem si nenapsal sám):")
        p += ["- %s: %s" % (k, v) for k, v in odz]
    if ins:
        p.append("\nCo jsem si sám všiml ve svých datech (vlastní rozbor):")
        p += ["- [%s] %s" % (l, t) for l, t in ins]
    return "\n".join(p)


if __name__ == "__main__":
    import json
    _c = json.load(open("config.json", encoding="utf-8"))
    print(block(_c, _c.get("diary_db", "data/hans_diary.db")) or "(prázdné)")
