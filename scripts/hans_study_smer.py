"""HANS_STUDY_DIRECTION_TOPIC_V1 (12. 10.) — směr navrhne další téma studia.

Jediné rozhodnutí, které směr dostal: když dojde fronta studia (žádný aktivní
ani čekající program), Hans podle svého aktivního směru navrhne JEDNO další
téma a zeptá se. Schválené téma jde do fronty stejnou cestou jako téma z chatu
(`add_pending_topic`), takže má přednost před koníčky. Bez odpovědi do
`study.direction_topic_wait_days` (3) návrh vyprší a vybírá se z koníčků jako
dřív; po zamítnutí nebo vypršení se `direction_topic_cooldown_days` (14)
nenavrhuje znovu. `study.direction_topic_auto` (výchozí False) = bez ptaní.

Proč: revize 12. 10. — směr četl jen výběr z koníčků a změnit ho nemohl
(shoda jen u už nastudovaných), od srpna žádný program podle směru.
"""
import json
import re
import sqlite3
import time

_SCHEMA = """CREATE TABLE IF NOT EXISTS study_topic_proposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, topic TEXT NOT NULL,
    why TEXT NOT NULL DEFAULT '', direction_id INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'navrh',  -- navrh|schvaleno|zamitnuto|vyprsel|pokryto
    notified_ts REAL NOT NULL DEFAULT 0, decided_ts REAL NOT NULL DEFAULT 0)"""
_DEN = 86400.0


def _conn(db: str):
    c = sqlite3.connect(db, timeout=10)
    c.row_factory = sqlite3.Row
    c.execute(_SCHEMA)
    return c


def zapnuto(config: dict) -> bool:
    return bool(_hs._cfg(config).get("direction_topic", True))


def _zadani_tematu(smer: str, hotovo: list) -> tuple:
    """Zadání pro model: (systém, dotaz). Čistá funkce kvůli regresi."""
    sysp = (
        "Jsi kurátor studia. Autor má vlastní zvolený SMĚR (k čemu chce růst) a "
        "seznam toho, co už nastudoval. Navrhni JEDNO další téma studia, které "
        "ho ke směru posune o krok dál a NEOPAKUJE nic z nastudovaného.\n"
        "PRAVIDLA: název tématu MAX 5 slov, skutečně existující obor nebo "
        "pojem, o kterém existují encyklopedická hesla (žádné nově složené "
        "názvy); téma musí jít studovat z textů — žádné laboratorní metody ani "
        "měření. K tomu 1 věta V 1. OSOBĚ (já, můj směr), proč právě tohle.\n"
        "Vrať POUZE JSON: {\"topic\":\"…\",\"why\":\"…\"} (česky).")
    dotaz = ("MŮJ SMĚR:\n%s\n\nUŽ NASTUDOVÁNO NEBO VE FRONTĚ (NEOPAKUJ):\n%s"
             "\n\nJSON:" % ((smer or "").strip(),
                            "\n".join("- %s" % t for t in hotovo) or "(nic)"))
    return sysp, dotaz


def stav_kroku(navrh_od: float, posledni_rozhodnuti: float, now: float,
               wait_days: float, cooldown_days: float) -> str:
    """Co udělat, když došla fronta. `navrh_od` = kdy čekající návrh začal
    čekat (0 = žádný nečeká), `posledni_rozhodnuti` = kdy se naposledy návrh
    uzavřel (0 = nikdy). Vrací cekam | vyprsel | dal | navrhni."""
    if navrh_od:
        return "cekam" if now - navrh_od < wait_days * _DEN else "vyprsel"
    if posledni_rozhodnuti and now - posledni_rozhodnuti < cooldown_days * _DEN:
        return "dal"
    return "navrhni"


def cekajici(db: str):
    """Čekající návrh tématu (dict) nebo None."""
    try:
        c = _conn(db)
        try:
            r = c.execute("SELECT * FROM study_topic_proposals WHERE "
                          "status='navrh' ORDER BY id DESC LIMIT 1").fetchone()
        finally:
            c.close()
        return dict(r) if r else None
    except Exception as e:
        _hs._log.warning("study směr: čtení návrhu: %s", e)
        return None


def _uzavri(db: str, pid: int, status: str) -> None:
    c = _conn(db)
    try:
        c.execute("UPDATE study_topic_proposals SET status=?, decided_ts=? "
                  "WHERE id=?", (status, time.time(), pid))
        c.commit()
    finally:
        c.close()


def _vloz(db: str, topic: str, why: str, direction_id: int, status: str) -> int:
    c = _conn(db)
    try:
        now = time.time()
        cur = c.execute(
            "INSERT INTO study_topic_proposals (ts, topic, why, direction_id, "
            "status, decided_ts) VALUES (?,?,?,?,?,?)",
            (now, topic, why, direction_id, status,
             0 if status == "navrh" else now))
        c.commit()
        return cur.lastrowid
    finally:
        c.close()


def _vygeneruj(config: dict, smer: str, hotovo: list):
    """Vrací {topic, why} | {} (model odpověděl nepoužitelně) | None (model dole)."""
    from scripts.ollama_client import ollama_generate
    model = (_hs._cfg(config).get("model")
             or (config.get("evening_reflection", {}) or {}).get("model")
             or "jobautomation/OpenEuroLLM-Czech:latest")
    sysp, dotaz = _zadani_tematu(smer, hotovo)
    raw = ollama_generate(model, dotaz, system=sysp, config=config, timeout=150,
                          keep_alive=0, options={"temperature": 0.5,
                                                 "num_ctx": 4096,
                                                 "num_predict": 200})
    if not raw:
        return None
    m = re.search(r"\{.*\}", raw, re.S)
    try:
        d = json.loads(m.group(0)) if m else {}
    except Exception:
        d = {}
    topic = re.sub(r"\s+", " ", str(d.get("topic", ""))).strip(" .\"„“")[:70]
    if len(topic) < 3:
        return {}
    return {"topic": topic, "why": str(d.get("why", "")).strip()[:400]}


def krok_smeru(config: dict, db: str) -> str:
    """Volá `ensure_program`, když neběží ani nečeká žádný program.
    Vrací 'cekam' (nezakládej nic) nebo 'dal' (pokračuj výběrem z koníčků)."""
    if not zapnuto(config):
        return "dal"
    from scripts.hans_direction import DirectionStore
    smer = DirectionStore(config, db).current_active()
    if not smer or not (smer.get("direction") or "").strip():
        return "dal"
    c = _hs._cfg(config)
    nav = cekajici(db)
    conn = _conn(db)
    try:
        posl = conn.execute("SELECT max(decided_ts) FROM study_topic_proposals "
                            "WHERE status<>'navrh'").fetchone()[0] or 0
        temata = conn.execute(
            "SELECT topic, topic_norm FROM study_program ORDER BY id").fetchall()
    finally:
        conn.close()
    krok = stav_kroku((nav["notified_ts"] or nav["ts"]) if nav else 0, posl,
                      time.time(), float(c.get("direction_topic_wait_days", 3)),
                      float(c.get("direction_topic_cooldown_days", 14)))
    if krok == "cekam":
        return "cekam"
    if krok == "vyprsel":
        _uzavri(db, nav["id"], "vyprsel")
        _hs._log.info("HANS_STUDY_DIRECTION_TOPIC_V1: návrh '%s' vypršel bez "
                      "odpovědi → výběr z koníčků", nav["topic"])
        return "dal"
    if krok == "dal":
        return "dal"
    gen = _vygeneruj(config, smer["direction"], [t["topic"] for t in temata])
    if gen is None:
        return "cekam"          # model dole → zkusit příště, nesahat na koníčky
    sid = int(smer.get("id") or 0)
    if not gen or _hs._already_covered(gen["topic"],
                                       [t["topic_norm"] for t in temata]):
        _vloz(db, (gen or {}).get("topic", "") or "(nic)", "", sid, "pokryto")
        _hs._log.info("HANS_STUDY_DIRECTION_TOPIC_V1: návrh ze směru nepoužitelný "
                      "nebo už nastudovaný (%s) → výběr z koníčků",
                      (gen or {}).get("topic", ""))
        return "dal"
    if c.get("direction_topic_auto", False):
        from scripts.hans_study_kurikulum import add_pending_topic
        res = add_pending_topic(db, gen["topic"])
        _vloz(db, gen["topic"], gen["why"], sid,
              "schvaleno" if res == "added" else "pokryto")
        _hs._log.info("HANS_STUDY_DIRECTION_TOPIC_V1: téma '%s' ze směru "
                      "zařazeno bez ptaní (%s)", gen["topic"], res)
        return "cekam" if res == "added" else "dal"
    pid = _vloz(db, gen["topic"], gen["why"], sid, "navrh")
    _hs._log.info("HANS_STUDY_DIRECTION_TOPIC_V1: NÁVRH tématu ze směru [%d] '%s'",
                  pid, gen["topic"])
    return "cekam"


def text_oznameni(nav: dict, wait_days: float) -> str:
    return ("Došla mi fronta studia, pane. Podle svého směru bych se chtěl "
            "pustit do tématu „%s“.%s Souhlasíte? (/studium schválit, "
            "/studium ne) Když se do %d dnů neozvete, vyberu si z koníčků."
            % (nav["topic"], (" " + nav["why"]) if nav.get("why") else "",
               int(wait_days)))


def oznam_cekajici(config: dict, db: str, notifier) -> bool:
    """Doruč čekající návrh uživateli (jednou). Lhůta běží až od doručení."""
    nav = cekajici(db)
    if not nav or nav["notified_ts"] or not notifier:
        return False
    notifier(text_oznameni(
        nav, float(_hs._cfg(config).get("direction_topic_wait_days", 3))))
    c = _conn(db)
    try:
        c.execute("UPDATE study_topic_proposals SET notified_ts=? WHERE id=?",
                  (time.time(), nav["id"]))
        c.commit()
    finally:
        c.close()
    _hs._log.info("HANS_STUDY_DIRECTION_TOPIC_V1: návrh '%s' oznámen", nav["topic"])
    return True


def rozhodni(db: str, schvalit: bool) -> dict:
    """Uživatel rozhodl o čekajícím návrhu. Vrací {status, topic}:
    nic | zamitnuto | schvaleno | pokryto."""
    nav = cekajici(db)
    if not nav:
        return {"status": "nic", "topic": ""}
    if not schvalit:
        _uzavri(db, nav["id"], "zamitnuto")
        return {"status": "zamitnuto", "topic": nav["topic"]}
    from scripts.hans_study_kurikulum import add_pending_topic
    res = add_pending_topic(db, nav["topic"])
    st = "schvaleno" if res == "added" else "pokryto"
    _uzavri(db, nav["id"], st)
    return {"status": st, "topic": nav["topic"]}


# ROZDELENI_PRIKAZU_V1 — až na konci (kruhový import)
from scripts import hans_study as _hs  # noqa: E402
