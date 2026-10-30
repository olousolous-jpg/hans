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


def _vygeneruj(config: dict, smer: str, hotovo: list, zadani=None):
    """Vrací {topic, why} | {} (model odpověděl nepoužitelně) | None (model dole)."""
    from scripts.ollama_client import ollama_generate
    model = (_hs._cfg(config).get("model")
             or (config.get("evening_reflection", {}) or {}).get("model")
             or "jobautomation/OpenEuroLLM-Czech:latest")
    sysp, dotaz = zadani or _zadani_tematu(smer, hotovo)
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


# ── HANS_STUDY_MIMO_V1 (29. 10.) — každé druhé téma MIMO koníčky ─────────────
# Rozbor 29. 10. (`SMERY_ROZVOJE_29_10`): témata studia rostou jen z koníčků
# a ze směru, který model odvodil z týchž koníčků (hrady × hudba × neurověda)
# → kruh, Severka pak pořád odvodí „archiváře“. Když dojde fronta, střídá se:
# po programu ze směru/koníčků navrhne Hans téma z OBORU, který se s koníčky
# ani s nastudovaným nepřekrývá (obor vybírá kód, model jen konkrétní téma).
# Schvaluje se stejně (`/studium schválit` · `/studium ne`); direction_id = -1.
_OBORY = (
    ("astronomie a vesmír", ("astro", "vesmi", "plane", "hvezd")),
    ("biologie a evoluce", ("biolo", "evolu", "zvira", "zvire")),
    ("chemie", ("chemi",)),
    ("fyzika", ("fyzik",)),
    ("medicína a lidské tělo", ("medic", "lekar", "zdrav")),
    ("psychologie", ("psych",)),
    ("filozofie", ("filoz",)),
    ("ekonomie a peníze", ("ekono", "penez", "finan")),
    ("matematika", ("matem",)),
    ("jazyky a lingvistika", ("jazyk", "lingv")),
    ("literatura a poezie", ("liter", "poezi", "basni", "roman")),
    ("výtvarné umění a malířství", ("vytva", "malir", "malba", "obraz")),
    ("technika a vynálezy", ("techn", "vynal", "stroj")),
    ("zeměpis a cestování", ("zemep", "geogr", "cesto")),
    ("oceány a mořský život", ("ocean", "morsk")),
    ("zemědělství a potraviny", ("zemed", "potra", "kuchy", "jidlo")),
    ("klima a meteorologie", ("klima", "meteo", "pocas")),
    ("archeologie", ("archeo",)),
    ("náboženství a mytologie", ("nabož", "naboz", "mytol")),
    ("divadlo a tanec", ("divad", "tanec", "balet")),
    ("informatika a umělá inteligence", ("infor", "pocit", "umela", "webov", "aplik")),
    ("ekologie a ochrana přírody", ("ekolo", "priro")),
)


def _fold(s: str) -> str:
    import unicodedata
    return "".join(ch for ch in unicodedata.normalize("NFD", (s or "").lower())
                   if unicodedata.category(ch) != "Mn")


def obory_mimo(znama: list) -> list:
    """Obory, jejichž kmeny se nevyskytují v koníčcích ani v nastudovaném.
    Čistá funkce kvůli regresi."""
    slova = set(re.findall(r"\w+", " ".join(_fold(z) for z in znama or ())))
    # začátek slova („katastrofy“ nejsou astronomie)
    return [o for o, kmeny in _OBORY
            if not any(s.startswith(_fold(k)) for k in kmeny for s in slova)]


def je_rada_mimo(db: str) -> bool:
    """Byl poslední program (dokončený či čekající) ze směru nebo z koníčků?
    Pak je řada na oboru mimo. Programy schválené z návrhu mimo nesou
    direction_id = -1 v `study_topic_proposals`."""
    c = _conn(db)
    try:
        r = c.execute("SELECT topic_norm FROM study_program ORDER BY id DESC "
                      "LIMIT 1").fetchone()
        if not r:
            return False
        mimo = {(_fold(x[0]) or "").strip() for x in c.execute(
            "SELECT topic FROM study_topic_proposals WHERE direction_id=-1 "
            "AND status='schvaleno'")}
    finally:
        c.close()
    return _fold(r[0]).strip() not in mimo


def _zadani_mimo(obor: str, hotovo: list) -> tuple:
    sysp = (
        "Jsi kurátor studia. Student chce poznat obor, se kterým dosud nepřišel "
        "do styku. Navrhni JEDNO úvodní téma studia z daného OBORU — zajímavé, "
        "vhodné pro začátečníka a dost široké na několik lekcí (podobor nebo "
        "klíčová myšlenka oboru, NE úzká specializace, jednotlivá nemoc či "
        "technický detail), které jde studovat z encyklopedických textů.\nPRAVIDLA: název tématu MAX 5 slov, skutečně existující pojem "
        "nebo podobor s vlastním heslem v encyklopedii (žádné nově složené "
        "názvy), nic z nastudovaného. K tomu 1 věta V 1. OSOBĚ, proč mě to "
        "láká.\nVrať POUZE JSON: {\"topic\":\"…\",\"why\":\"…\"} (česky).")
    dotaz = ("OBOR: %s\n\nUŽ NASTUDOVÁNO (NEOPAKUJ):\n%s\n\nJSON:"
             % (obor, "\n".join("- %s" % t for t in hotovo) or "(nic)"))
    return sysp, dotaz


def krok_mimo(config: dict, db: str):
    """Návrh tématu z oboru mimo koníčky. Vrací 'cekam' | None (nebylo na
    řadě nebo se nepovedlo → pokračuj směrem/koníčky)."""
    c = _hs._cfg(config)
    if not c.get("direction_topic_mimo", True) or not je_rada_mimo(db):
        return None
    conn = _conn(db)
    try:
        temata = conn.execute(
            "SELECT topic, topic_norm FROM study_program ORDER BY id").fetchall()
        try:
            konicky = [r[0] for r in conn.execute(
                "SELECT name FROM hobbies WHERE status='active'")]
        except Exception:
            konicky = []
        uz_navrzene = [r[0] for r in conn.execute(
            "SELECT topic FROM study_topic_proposals WHERE direction_id=-1")]
    finally:
        conn.close()
    obory = obory_mimo(konicky + [t["topic"] for t in temata] + uz_navrzene)
    if not obory:
        return None
    import random as _r
    obor = _r.choice(obory)
    sysp, dotaz = _zadani_mimo(obor, [t["topic"] for t in temata])
    gen = _vygeneruj(config, "", [], zadani=(sysp, dotaz))
    if gen is None:
        return "cekam"
    if not gen or _hs._already_covered(gen["topic"],
                                       [t["topic_norm"] for t in temata]):
        _vloz(db, (gen or {}).get("topic", "") or "(nic)", "", -1, "pokryto")
        _hs._log.info("HANS_STUDY_MIMO_V1: návrh z oboru '%s' nepoužitelný (%s)",
                      obor, (gen or {}).get("topic", ""))
        return None
    if c.get("direction_topic_auto", False):
        from scripts.hans_study_kurikulum import add_pending_topic
        res = add_pending_topic(db, gen["topic"])
        _vloz(db, gen["topic"], gen["why"], -1,
              "schvaleno" if res == "added" else "pokryto")
        _hs._log.info("HANS_STUDY_MIMO_V1: téma '%s' (obor %s) zařazeno bez ptaní (%s)",
                      gen["topic"], obor, res)
        return "cekam" if res == "added" else None
    pid = _vloz(db, gen["topic"], gen["why"], -1, "navrh")
    _hs._log.info("HANS_STUDY_MIMO_V1: NÁVRH tématu mimo koníčky [%d] '%s' (obor %s)",
                  pid, gen["topic"], obor)
    return "cekam"


def krok_smeru(config: dict, db: str) -> str:
    """Volá `ensure_program`, když neběží ani nečeká žádný program.
    Vrací 'cekam' (nezakládej nic) nebo 'dal' (pokračuj výběrem z koníčků)."""
    if not zapnuto(config):
        return "dal"
    # HANS_STUDY_MIMO_V1 — čekající návrh (i mimo) řeší společná logika níž;
    # nový návrh mimo koníčky jen když nic nečeká a neběží odklad
    if cekajici(db) is None:
        conn = _conn(db)
        try:
            _posl = conn.execute("SELECT max(decided_ts) FROM study_topic_proposals "
                                 "WHERE status<>'navrh'").fetchone()[0] or 0
        finally:
            conn.close()
        _cd = float(_hs._cfg(config).get("direction_topic_cooldown_days", 14))
        if not _posl or time.time() - _posl >= _cd * _DEN:
            if krok_mimo(config, db) == "cekam":
                return "cekam"
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
    sid = int(smer.get("id") or 0)  # (-1 = návrh mimo koníčky, HANS_STUDY_MIMO_V1)
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
    if int(nav.get("direction_id") or 0) == -1:      # HANS_STUDY_MIMO_V1
        return ("Došla mi fronta studia, pane. Tentokrát bych rád zkusil něco "
                "mimo své dosavadní zájmy: téma „%s“.%s Souhlasíte? (/studium "
                "schválit, /studium ne) Když se do %d dnů neozvete, vyberu si "
                "z koníčků." % (nav["topic"], (" " + nav["why"]) if nav.get("why")
                                else "", int(wait_days)))
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
