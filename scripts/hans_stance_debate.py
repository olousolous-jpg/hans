# -*- coding: utf-8 -*-
"""KOLAC_DEBATE_NIGHT_JUDGE_V1 (4. 10.) — Koláčova debata o postoji se soudí V NOCI.

PROČ: dřív soudil hned po debatě `hans-czech` jedinou otázkou „ustoupil?“
a postoj se uměl jen OSLABIT. Za 10 dní tak Koláč stáhl všechny zažité
postoje k podlaze 0,4 a Hans by časem zůstal bez postojů (uživatel 4. 10.).
Měření 4. 10. na 37 debatách z logu (22 „obstál“, 16 „ustoupil“):
  • hans-czech na „obhájil?“ řekl ANO 35/37 (i u 12/16 ústupů) — nepoužitelný,
  • qwen2.5:7b (vejde se k hans-czech) DEFENDED 27/37 — nepoužitelný,
  • qwen2.5:14b třemi třídami rozlišuje (13 DEFENDED / 9 OFFTOPIC u „obstál“,
    8 CONCEDED u „ustoupil“); ručně ověřené 2 ze 3 sporných pro 14b, třetí
    nejednoznačný. Dnešní soud se mýlil např. 28. 9.: debata o Bondovi
    oslabila „Nevytvářím domněnky“, protože Hans řekl „domnívám se“.
14b se k rezidentnímu hans-czech do 16 GB nevejde → soudí se DÁVKOU v noci
(za stance krokem večerní reflexe, kde se VRAM stejně střídá).

Verdikty: CONCEDED → contradict (−15 %), DEFENDED → posílení polovičním
krokem (`challenge_defended`), OFFTOPIC → jen bod `challenge_held`.
Minulá oslabení se nepřepočítávají.
"""
from __future__ import annotations

import logging
import sqlite3
import time

_log = logging.getLogger(__name__)

# KOLAC_DEBATE_JUDGE_STANCE_ONLY_V1 (5. 10.) — ústup se počítá jen tehdy, když
# se týká SOUZENÉHO postoje. Dřívější zadání bralo za ústup i „máte pravdu“
# k úplně jinému tématu debaty: postoj o přiznávání nejistoty byl 2× oslaben
# za to, že Hans uznal námitku ke studijnímu tématu. Měřeno párově na 39
# debatách z logu (`data/mereni/soud_debat/`): CONCEDED 10 → 4, DEFENDED 18 → 6,
# OFFTOPIC 11 → 29; ze 7 ručně čtených rozdílů 5 správně, 1 přehlédnutý
# skutečný ústup (skončí neutrálně), 1 sporný. Chyba nového zadání tedy
# postoj nechá být, místo aby ho měnila neprávem.
SOUD_SYS = (
    "You judge a debate transcript (in Czech) between Hans and Kolac. Kolac was supposed "
    "to challenge Hans's STANCE, but the debate often drifts to a different subject. "
    "Judge ONLY what happens to THAT stance, with exactly one word.\n"
    "First decide whether the core idea of the stance is actually debated in the transcript. "
    "If the transcript is about another subject, the verdict is OFFTOPIC, no matter how "
    "Hans behaves.\n"
    "DEFENDED = Hans explicitly addressed the stance (or its core idea) and supported it "
    "with a concrete reason or counter-argument;\n"
    "CONCEDED = Hans gave up, softened or doubted THE STANCE ITSELF. Agreeing with Kolac "
    "about some other subject (a fact, a method, the topic under discussion), or admitting "
    "uncertainty or a mistake about something else, is NOT conceding the stance;\n"
    "OFFTOPIC = the stance itself was not really discussed, or Hans only insisted politely "
    "without any reason.\n"
    "Answer with one word: DEFENDED, CONCEDED or OFFTOPIC.")
_VERDIKTY = ("DEFENDED", "CONCEDED", "OFFTOPIC")


def _cfg(config) -> dict:
    return (config or {}).get("hans_dialog", {}) or {}


def _conn(diary_path: str):
    c = sqlite3.connect(diary_path, timeout=10)
    c.execute("""CREATE TABLE IF NOT EXISTS stance_debates (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, claim TEXT NOT NULL,
        conf REAL, dialog TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
        verdict TEXT, judged_ts REAL, model TEXT)""")
    c.execute("CREATE INDEX IF NOT EXISTS ix_stdeb_status ON stance_debates(status, ts)")
    return c


def zarad(diary_path: str, claim: str, conf: float, dialog: str) -> None:
    """Debatu se zpochybněným postojem uložit k nočnímu soudu."""
    claim = (claim or "").strip()
    if not claim or not (dialog or "").strip():
        return
    c = _conn(diary_path)
    try:
        c.execute("INSERT INTO stance_debates(ts, claim, conf, dialog) VALUES (?,?,?,?)",
                  (time.time(), claim, float(conf or 0), dialog))
        c.commit()
    finally:
        c.close()
    _log.info("KOLAC_DEBATE_NIGHT_JUDGE_V1: debata o postoji čeká na noční soud: %.60s", claim)


def _posledni_replika(dialog: str, jmeno: str) -> str:
    for l in reversed((dialog or "").split("\n")):
        if ":" in l and l.split(":", 1)[0].strip().lower() == jmeno.lower():
            return l.split(":", 1)[1].strip()[:200]
    return ""


def posud(config: dict, diary_path: str, max_n: int = 30) -> dict:
    """Noční dávka: posoudí čekající debaty a promítne verdikty do postojů."""
    from scripts.ollama_client import ollama_chat, game_mode_on, translate_pause_on
    from scripts.hans_stances import StanceStore
    from scripts.hans_persona import persona_name
    dc = _cfg(config)
    model = str(dc.get("stance_judge_model", "qwen2.5:14b"))
    faktor = float(dc.get("stance_defend_factor", 0.5))
    stari = float(dc.get("stance_judge_max_age_days", 14)) * 86400
    out = {"posouzeno": 0, "CONCEDED": 0, "DEFENDED": 0, "OFFTOPIC": 0, "nevyslo": 0}
    c = _conn(diary_path)
    try:
        c.execute("UPDATE stance_debates SET status='expired' WHERE status='pending' AND ts<?",
                  (time.time() - stari,))
        c.commit()
        rows = c.execute("SELECT id, claim, dialog FROM stance_debates WHERE status='pending' "
                         "ORDER BY ts LIMIT ?", (int(max_n),)).fetchall()
    finally:
        c.close()
    if not rows:
        return out
    store = StanceStore(config, diary_path)
    jmeno = persona_name(config)
    for i, (did, claim, dialog) in enumerate(rows):
        if game_mode_on() or translate_pause_on():
            break                                   # GPU patří hře / překladu
        posledni = i == len(rows) - 1
        v = ollama_chat(model, [{"role": "system", "content": SOUD_SYS},
                                {"role": "user", "content": "STANCE: %s\n\nTRANSCRIPT:\n%s\n\nVerdict:"
                                 % (claim, dialog)}],
                        config=config, timeout=300, keep_alive=0 if posledni else "60s",
                        options={"temperature": 0.0, "num_ctx": 8192, "num_predict": 8})
        slovo = next((w for w in _VERDIKTY if w in (v or "").upper()), None)
        if slovo is None:
            out["nevyslo"] += 1
            if v is None:
                break                               # mozek nedostupný → příští noc
            continue
        if slovo == "CONCEDED":
            store.contradict(claim, counter_claim=_posledni_replika(dialog, jmeno),
                             source="kolac_debate")
        elif slovo == "DEFENDED":
            store.defend(claim, faktor)
        else:
            store.challenge_held(claim)
        c = _conn(diary_path)
        try:
            c.execute("UPDATE stance_debates SET status='done', verdict=?, judged_ts=?, model=? "
                      "WHERE id=?", (slovo, time.time(), model, did))
            c.commit()
        finally:
            c.close()
        out["posouzeno"] += 1
        out[slovo] += 1
    if out["posouzeno"] or out["nevyslo"]:
        _log.info("KOLAC_DEBATE_NIGHT_JUDGE_V1: posouzeno %d (ustoupil %d, obhájil %d, "
                  "mimo téma %d), nevyšlo %d", out["posouzeno"], out["CONCEDED"],
                  out["DEFENDED"], out["OFFTOPIC"], out["nevyslo"])
    return out
