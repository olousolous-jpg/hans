"""hans_commitments.py — HANS_COMMITMENTS_V1

Hans si PAMATUJE sliby, které dal v chatu. Doteď o závazcích jen mluvil, ale
nikam se neukládaly → zapomínal, co slíbil, a na dotaz „co jsi mi slíbil?"
neuměl odpovědět (nebo by konfabuloval).

- extract_commitments(): noční — z human_chat vytáhne GENUINNÍ sliby, které dal
  HANS ve SVÝCH replikách. Filtruje provozní/systémové akce (WOL, hlídání) i
  sliby, které dal ČLOVĚK. Vzor jako hans_lessons.extract_corrections.
- commitments_answer(): deterministický recall na „co jsi mi slíbil" (grounded
  ze store, ne hledání v textu; prázdno → honestní „nic", NE výmysl).
- open_commitments()/mark_done(): správa (fáze 2 = proaktivní dotažení).
"""
import json
import re
import sqlite3
import time
import logging
from typing import Optional, List

_log = logging.getLogger("hans")

_WS = re.compile(r"\s+")


def _norm(s: str) -> str:
    return _WS.sub(" ", (s or "").strip().lower())


_WEEKDAYS = {
    "pondělí": 0, "pondeli": 0, "pondělek": 0, "pondělku": 0,
    "úterý": 1, "utery": 1, "úterku": 1, "uterku": 1,
    "středa": 2, "streda": 2, "středu": 2, "stredu": 2,
    "čtvrtek": 3, "ctvrtek": 3,
    "pátek": 4, "patek": 4,
    "sobota": 5, "sobotu": 5, "sobotě": 5,
    "neděle": 6, "nedele": 6, "neděli": 6, "nedeli": 6,
}


def _parse_due(phrase: str, ref_ts: Optional[float] = None) -> float:
    """HANS_COMMITMENTS_DUE_V1 — český relativní termín → timestamp (0 = nešlo).
    Deadline míří na 20:00 daného dne (připomínka padne v bdělou dobu). Ref =
    kdy slib padl (≈ teď při noční extrakci)."""
    import datetime as _dt
    if not phrase:
        return 0.0
    p = _norm(phrase)
    ref = _dt.datetime.fromtimestamp(ref_ts or time.time())

    def _eod(d):
        return d.replace(hour=20, minute=0, second=0, microsecond=0).timestamp()

    m = re.search(r"za\s+(\d+)\s+d(?:en|ny|ní|nu|nů|ne)", p)
    if m:
        return _eod(ref + _dt.timedelta(days=int(m.group(1))))
    if "pozítří" in p or "pozitri" in p:
        return _eod(ref + _dt.timedelta(days=2))
    if "zítra" in p or "zitra" in p:
        return _eod(ref + _dt.timedelta(days=1))
    if "za týden" in p or "za tyden" in p or "příští týden" in p \
            or "pristi tyden" in p:
        return _eod(ref + _dt.timedelta(days=7))
    if "konce týdne" in p or "konce tydne" in p:
        days = (6 - ref.weekday()) % 7 or 7
        return _eod(ref + _dt.timedelta(days=days))
    if "dnes" in p or "večer" in p or "vecer" in p:
        return _eod(ref)
    for name, wd in _WEEKDAYS.items():
        if name in p:
            days = (wd - ref.weekday()) % 7 or 7   # „v neděli" = příští výskyt
            return _eod(ref + _dt.timedelta(days=days))
    return 0.0


# Kódová pojistka: malý extrakční model občas pustí PROVOZNÍ narraci (render
# obrazu, buzení PC) jako „slib". Tyhle markery = zahodit, ať se store nezanáší.
_OPERATIONAL = (
    "cca ", "sekund", "v procesu", "prezentován", "bude doručen", "render",
    "maluji znovu", "právě malu", "posílám", "ověřím, zda", "naběhl",
    "probouz", "vypínám", "hlídám",
)

# HANS_COMMIT_INFO_LOOKUP_V1 (18.7.) — info-dotazy které se řeší HNED z uložených
# dat (ne v budoucnu). Doložený false-positive: „Zjistím, zda máte něco
# naplánovaného v kalendáři." — Hans se podíval do kalendáře a odpověděl
# okamžitě, žádný budoucí závazek. Filtr pokrývá lookup akce („zjistím zda /
# v kalendáři / v deníku / v seznamu / v mých záznamech / podívám se / vyhledám").
import re as _re
# Sloveso info-lookup (zjistím, podívám se, vyhledám, zkontroluji, nahlédnu — CZ ohýbání)
_LOOKUP_VERB = r"(zjist[íi]m?|pod[ií]v[áa]m\s+se|vyhled[áa]m?|zkontroluj[iíeu]|nahl[ée]dnu)"
# Zdroj info (kalendář, deník, seznam, záznamy, databáze) — kdekoli za slovesem
_LOOKUP_SOURCE = (r"kalend[áa][řr]|den[ií]k|seznam|z[áa]znam|datab[áa]z|"
                  r"zda\s+m[áa](te|[šs]))")
_INFO_LOOKUP_RE = _re.compile(
    _LOOKUP_VERB + r".{0,60}?(" + _LOOKUP_SOURCE,
    _re.I,
)


# HANS_COMMIT_SELF_NARRATION_V1 (22.7.) — Hansův POPIS vlastního studia/zájmů
# v přítomném čase NENÍ slib člověku. Doložené false-positives ze store:
# „Zaměřuji se na pochopení principů designu.", „Čerpám z japonských zahrad.",
# „Zabývám se tématem Design.". Rozlišení: přítomné pokračovací sloveso bez
# adresáta (≠ „nastuduji ti…" = budoucí slib s deliverable pro osobu).
# POZOR: jen DISTINKTIVNÍ víceslovné fráze — jednoslovné (např. „studuji ")
# jsou podřetězcem budoucích slibů („naSTUDUJI ti") → over-block.
_SELF_NARRATION = (
    "zaměřuji se", "zaměřuju se", "čerpám z", "čerpám ze", "zabývám se",
    "věnuji se", "věnuju se", "soustředím se", "zajímám se",
    "pracuji na pochopení",
)


def _is_operational(promise: str) -> bool:
    pl = (promise or "").lower()
    if any(k in pl for k in _OPERATIONAL):
        return True
    if any(k in pl for k in _SELF_NARRATION):  # HANS_COMMIT_SELF_NARRATION_V1
        return True
    # info-lookup akce = ne budoucí slib
    return bool(_INFO_LOOKUP_RE.search(promise or ""))


# HANS_COMMIT_FULFILL_V1 — druh splnitelného slibu (řídí, JAK se dotáhne):
#  research = dohledej na webu a řekni závěr;
#  paint    = namaluj obraz na námět a pošli;
#  reminder = v čas připomeň.
# research odliš od info-lookupu (řeší se HNED z vlastních dat = _is_operational).
_RESEARCH_RE = _re.compile(
    r"(zjist[íi]|zkontroluj|nastuduj|dohled[áa]|ov[ěe][řr][íi]m|prozkoum|"
    r"vyhled[áa]m|pod[ií]v[áa]m\s+se|najdu|dozv[íi]m)", _re.I)
_PAINT_RE = _re.compile(
    r"(namaluj|namal[uú]j|nakresl|obraz\b|obr[áa]zek|portr[ée]t|"
    r"vytvo[řr][íi]m?\s+(ti\s+)?obraz)", _re.I)
_REMINDER_RE = _re.compile(r"(p[řr]ipomen|p[řr]ipom[íi]nk)", _re.I)


def _promise_kind(promise: str, topic: str = "") -> str:
    """Druh splnitelného slibu, nebo '' (nedotažitelný automaticky). Pořadí:
    paint > research > reminder (lookup má přednost před pouhou připomínkou).
    paint/research potřebují NÁMĚT (topic); reminder ne (připomene text slibu)."""
    p = promise or ""
    has_topic = bool((topic or "").strip())
    if _PAINT_RE.search(p):
        return "paint" if has_topic else ""
    if _RESEARCH_RE.search(p):
        return "research" if has_topic else ""
    if _REMINDER_RE.search(p):
        return "reminder"
    return ""


def _init(db) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS commitments (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        person      TEXT NOT NULL,
        text        TEXT NOT NULL,
        text_norm   TEXT NOT NULL,
        status      TEXT NOT NULL DEFAULT 'open',
        source_ts   REAL NOT NULL DEFAULT 0,
        created_ts  REAL NOT NULL,
        done_ts     REAL NOT NULL DEFAULT 0,
        due_ts      REAL NOT NULL DEFAULT 0,
        due_text    TEXT NOT NULL DEFAULT '')""")
    db.execute("CREATE INDEX IF NOT EXISTS idx_commit_status "
               "ON commitments(status, person)")
    # idempotentní přidání termínových sloupců do STARÉ tabulky (bez due_*)
    for ddl in ("due_ts REAL NOT NULL DEFAULT 0",
                "due_text TEXT NOT NULL DEFAULT ''"):
        try:
            db.execute("ALTER TABLE commitments ADD COLUMN " + ddl)
        except Exception:
            pass  # sloupec už existuje
    # HANS_COMMIT_ANNOUNCE_V1 — flag „už jsem o slibu dal vědět". ALTER uspěje
    # jen při PRVNÍM přidání sloupce → existující backlog označ jako oznámený
    # (jednorázově, ať ranní oznámení nezaspamuje staré/false-positive sliby).
    try:
        db.execute("ALTER TABLE commitments ADD COLUMN "
                   "announced INTEGER NOT NULL DEFAULT 0")
        db.execute("UPDATE commitments SET announced=1")
    except Exception:
        pass  # sloupec už existuje → UPDATE se NEspustí (chrání nové sliby)
    # HANS_COMMIT_FULFILL_V1 — uzavření smyčky: topic (CO dohledat), kind
    # ('research' = slib dohledat a říct), result (GROUNDED nález z webu),
    # reported (řekl jsem už výsledek majiteli?).
    for ddl in ("topic TEXT NOT NULL DEFAULT ''",
                "kind TEXT NOT NULL DEFAULT ''",
                "result TEXT NOT NULL DEFAULT ''",
                "reported INTEGER NOT NULL DEFAULT 0",
                "tries INTEGER NOT NULL DEFAULT 0"):
        try:
            db.execute("ALTER TABLE commitments ADD COLUMN " + ddl)
        except Exception:
            pass  # sloupec už existuje


# {persona_name} se doplní z configu při .format()
_SYSTEM = (
    "Jsi pečlivý analytik závazků postavy jménem {persona_name}. Dostaneš PŘEPIS "
    "rozhovorů — řádky „<osoba>:“ jsou co řekl člověk, řádky „{persona_name}:“ "
    "jsou co odpověděl {persona_name}.\n\n"
    "Úkol: najdi KONKRÉTNÍ SLIBY / ZÁVAZKY do budoucna, které dal {persona_name} "
    "ve SVÝCH replikách — věci, které se zavázal UDĚLAT (např. „nastuduji…“, "
    "„namaluji ti…“, „připomenu ti…“, „zjistím to a řeknu ti…“, „příště se "
    "podívám na…“).\n\n"
    "DŮLEŽITÉ: slib je něco, co se NEDOKONČÍ hned v tomhle rozhovoru, ale AŽ "
    "POZDĚJI (nastudování tématu, vytvoření díla na později, připomenutí, "
    "dohledání a pozdější sdělení závěru).\n\n"
    "NEEXTRAHUJ:\n"
    "- zdvořilostní fráze bez obsahu („rád pomohu“, „jsem k službám“);\n"
    "- provozní/systémové akce (probuzení či vypnutí počítače, hlídání domu, "
    "„ověřím, zda naběhl“, „posílám signál“);\n"
    "- věci, které se dokončí HNED teď v tomto rozhovoru — zejména PRÁVĚ "
    "PROBÍHAJÍCÍ malování/render obrazu („maluji…“, „obraz se vytváří“, "
    "„cca 20 sekund“, „bude vám prezentován po dokončení“); to NENÍ slib;\n"
    "- věci, které {persona_name} UŽ udělal (minulý čas);\n"
    "- POPIS vlastního studia/zájmů v PŘÍTOMNÉM čase — čím se {persona_name} "
    "zrovna zabývá, na co se zaměřuje, z čeho čerpá („zaměřuji se na…“, "
    "„čerpám z…“, „zabývám se tématem…“, „věnuji se…“); to je popis činnosti, "
    "NE slib člověku. (Slib je „nastuduji ti…“ / „zjistím a řeknu ti…“ v "
    "BUDOUCÍM čase s adresátem.);\n"
    "- sliby, které dal ČLOVĚK, ne {persona_name}.\n\n"
    "Když žádný skutečný závazek není, vrať prázdné pole [].\n"
    "U každého slibu vyplň i „due“ = TERMÍN, POKUD ho slib zmiňuje (např. "
    "„do neděle“, „zítra“, „za týden“, „do konce týdne“); když termín uveden "
    "NENÍ, dej prázdný řetězec.\n"
    "U každého slibu vyplň i „topic“ = KONKRÉTNÍ VĚC/POJEM/NÁMĚT, kterého se slib "
    "týká — co má {persona_name} dohledat, nastudovat, NAMALOVAT nebo připomenout "
    "(např. „Golem“, „bitva u Slavkova“, „hrad Karlštejn“). "
    "Rozveď zájmena z kontextu rozhovoru — když {persona_name} řekl „zjistím to“ "
    "nebo „namaluji ti ho“, dosaď za „to/ho“ SKUTEČNÝ předmět z předchozích "
    "replik. Když námět nelze určit, dej prázdný řetězec.\n"
    "Vrať POUZE JSON pole objektů, nic jiného:\n"
    "[{{\"promise\": \"<slib slovy {persona_name}a, 1. osoba, krátce a "
    "konkrétně>\", \"due\": \"<termín nebo prázdné>\", "
    "\"topic\": \"<věc k dohledání nebo prázdné>\"}}]"
)


def extract_commitments(config: dict, diary_db_path: str,
                        window_hours: float = 26.0) -> int:
    """Noční krok: z human_chat vytáhne Hansovy vlastní sliby → tabulka
    commitments (status='open'). LLM offline / žádné dialogy → 0 (tichý skip).
    Idempotentní (dedup na text_norm proti otevřeným slibům téže osoby)."""
    cfg = (config.get("commitments", {}) or {})
    if not cfg.get("enabled", True):
        return 0
    window_hours = float(cfg.get("window_hours", window_hours))
    since = time.time() - window_hours * 3600.0
    try:
        from scripts.hans_threads import _gather_dialogs
        from scripts.hans_lessons import _extract_json_array
        from scripts.ollama_client import ollama_generate
    except Exception as e:
        _log.warning("extract_commitments: import: %s", e)
        return 0
    dialogs = _gather_dialogs(diary_db_path, since)
    if not dialogs:
        return 0
    er = config.get("evening_reflection", {}) or {}
    model = str(cfg.get("model", er.get("model",
                "jobautomation/OpenEuroLLM-Czech:latest")))
    timeout = int(cfg.get("llm_timeout", 300))
    max_per_night = int(cfg.get("max_per_night", 6))
    try:
        from scripts.hans_persona import persona_name as _pn
        pname = _pn(config)
    except Exception:
        pname = "Hans"
    system = _SYSTEM.format(persona_name=pname)

    written = 0
    for person, notes in dialogs.items():
        if written >= max_per_night:
            break
        transcript = "\n\n".join(notes)[:4000]
        if not transcript.strip():
            continue
        try:
            raw = ollama_generate(model=model, prompt=transcript, system=system,
                                  config=config, timeout=timeout, keep_alive=0,
                                  options={"temperature": 0.1})
        except Exception as e:
            _log.warning("extract_commitments LLM (%s): %s", person, e)
            continue
        if not raw:
            continue
        items = _extract_json_array(raw)
        if not items:
            continue
        try:
            db = sqlite3.connect(diary_db_path, timeout=5.0)
            _init(db)
            existing = {r[0] for r in db.execute(
                "SELECT text_norm FROM commitments WHERE person=? "
                "AND status='open'", (person,)).fetchall()}
            for it in items:
                if written >= max_per_night:
                    break
                if not isinstance(it, dict):
                    continue
                promise = str(it.get("promise", "") or "").strip()
                if len(promise) < 8:
                    continue
                if _is_operational(promise):
                    _log.info("commitment odfiltrován (provozní): %.60s", promise)
                    continue
                pn = _norm(promise)
                if pn in existing:
                    continue
                existing.add(pn)
                due_text = str(it.get("due", "") or "").strip()
                due_ts = _parse_due(due_text) if due_text else 0.0
                # HANS_COMMIT_FULFILL_V1 — topic (námět) + kind (research/paint/reminder)
                topic = str(it.get("topic", "") or "").strip()[:120]
                kind = _promise_kind(promise, topic)
                db.execute(
                    "INSERT INTO commitments (person, text, text_norm, status, "
                    "source_ts, created_ts, due_ts, due_text, topic, kind) "
                    "VALUES (?,?,?,'open',?,?,?,?,?,?)",
                    (person, promise, pn, since, time.time(), due_ts, due_text,
                     topic, kind))
                written += 1
            db.commit()
            db.close()
        except Exception as e:
            _log.warning("extract_commitments write (%s): %s", person, e)
    if written:
        _log.info("extract_commitments: uloženo %d slibů", written)
    return written


# ── recall ────────────────────────────────────────────────────────────────
_Q = re.compile(
    r"co\s+jsi\s+(mi\s+)?slíbil|cos\s+(mi\s+)?slíbil|slíbils|slíbil\s+jsi|"
    r"sliboval\s+jsi|co\s+jsi\s+(mi\s+)?sliboval|tv[éeoá]\w*\s+slib|"
    r"na\s+co\s+jsi\s+zapomn|co\s+jsi\s+mi\s+měl|dlužíš\s+mi|"
    r"co\s+mi\s+dlužíš|\bsliby\b", re.I)


def is_commitment_query(text: str) -> bool:
    return bool(_Q.search(text or ""))


def open_commitments(diary_db_path: str, person: Optional[str] = None,
                     limit: int = 20) -> List[tuple]:
    """[(text, source_ts, due_ts, due_text), …] otevřených slibů (per osoba,
    jinak všech). due_ts/due_text můžou být 0/'' (bez termínu)."""
    _cols = "text, source_ts, due_ts, due_text"
    try:
        db = sqlite3.connect("file:%s?mode=ro" % diary_db_path, uri=True,
                             timeout=5.0)
    except Exception:
        return []
    try:
        if person:
            rows = db.execute(
                "SELECT " + _cols + " FROM commitments WHERE status='open' "
                "AND person=? ORDER BY created_ts DESC LIMIT ?",
                (_norm(person), limit)).fetchall()
            if rows:
                return rows
        return db.execute(
            "SELECT " + _cols + " FROM commitments WHERE status='open' "
            "ORDER BY created_ts DESC LIMIT ?", (limit,)).fetchall()
    except Exception:
        return []
    finally:
        try:
            db.close()
        except Exception:
            pass


def unannounced_commitments(diary_db_path: str,
                            limit: int = 5) -> List[tuple]:
    """HANS_COMMIT_ANNOUNCE_V1 — [(id, person, text), …] otevřených slibů, o
    kterých Hans ještě NEDAL vědět (announced=0). Nejstarší napřed."""
    try:
        db = sqlite3.connect("file:%s?mode=ro" % diary_db_path, uri=True,
                             timeout=5.0)
    except Exception:
        return []
    try:
        return db.execute(
            "SELECT id, person, text FROM commitments WHERE status='open' "
            "AND announced=0 ORDER BY created_ts ASC LIMIT ?",
            (limit,)).fetchall()
    except Exception:
        return []  # sloupec announced ještě neexistuje (před 1. _init) → nic
    finally:
        try:
            db.close()
        except Exception:
            pass


def mark_announced(diary_db_path: str, ids: List[int]) -> int:
    """Označ sliby jako oznámené. Vrátí počet."""
    if not ids:
        return 0
    try:
        db = sqlite3.connect(diary_db_path, timeout=5.0)
    except Exception:
        return 0
    try:
        ph = ",".join("?" * len(ids))
        cur = db.execute(
            "UPDATE commitments SET announced=1 WHERE id IN (%s)" % ph,
            tuple(int(i) for i in ids))
        db.commit()
        return cur.rowcount
    except Exception as e:
        _log.warning("mark_announced: %s", e)
        return 0
    finally:
        try:
            db.close()
        except Exception:
            pass


def fulfill_commitments(config: dict, diary_db_path: str, limit: int = 3) -> int:
    """HANS_COMMIT_FULFILL_V1 — dotáhni sliby, které lze splnit AKCÍ:
      research → dohledej topic na Wikipedii (GROUNDED result);
      paint    → namaluj obraz na námět (cesta k obrázku v result).
    Uloží result + status='done' (reported=0 → doručí se při usazení).
    Bezpečné: mozek dole/render selhal → nech otevřené, zkusí příště; research
    nenašel po 3 pokusech → honest „nenašel jsem zdroj"; result NIKDY
    konfabulace. reminder se tu NEřeší (doručí se v čas). Vrátí počet uzavřených.
    Paint je těžký (SDXL) → max 1 render na běh."""
    try:
        from scripts.ollama_client import game_mode_on
        if game_mode_on():
            return 0  # VRAM patří hře
    except Exception:
        pass
    try:
        db = sqlite3.connect(diary_db_path, timeout=5.0)
        _init(db)
        rows = db.execute(
            "SELECT id, topic, tries, kind FROM commitments WHERE status='open' "
            "AND kind IN ('research','paint') AND topic!='' AND result='' "
            "ORDER BY created_ts ASC LIMIT ?", (int(limit),)).fetchall()
        db.close()
    except Exception as e:
        _log.warning("fulfill: select: %s", e)
        return 0
    if not rows:
        return 0
    closed = 0
    painted = 0
    for cid, topic, tries, kind in rows:
        if kind == "paint":
            if painted >= 1:        # 1 render/běh (SDXL těžký)
                continue
            if _fulfill_paint(config, diary_db_path, cid, topic, int(tries or 0)):
                closed += 1
            painted += 1
        else:
            if _fulfill_research(config, diary_db_path, cid, topic,
                                 int(tries or 0)):
                closed += 1
    return closed


def _fulfill_research(config, diary_db_path, cid, topic, tries) -> bool:
    """Dohledej topic na Wikipedii → grounded result + done. True když uzavřeno."""
    try:
        from scripts.web_reader import WebReader
        res = WebReader(config).wikipedia_read(topic)
    except Exception as e:
        _log.warning("fulfill: read '%s': %s", topic, e)
        res = None
    # mozek dole → summary se nevyrobila → deferred, NEpočítej jako pokus
    if res is not None and getattr(res, 'pending', False):
        return False
    summary = (res.summary.strip() if (res is not None and res.summary) else "")
    if summary:
        _close_fulfilled(diary_db_path, cid, summary[:1500],
                         topic=topic, grounded=True)
        _log.info("fulfill: slib #%d '%s' → grounded (%d zn)",
                  cid, topic, len(summary))
        return True
    new_tries = tries + 1
    if new_tries >= 3:
        _close_fulfilled(diary_db_path, cid,
                         "K tomuhle jsem bohužel nenašel spolehlivý zdroj, tak "
                         "si raději nic nevymýšlím.")
        _log.info("fulfill: slib #%d '%s' → nenašel (po %d)", cid, topic, new_tries)
        return True
    _bump_tries(diary_db_path, cid, new_tries)
    _log.info("fulfill: slib #%d '%s' bez zdroje (pokus %d/3)", cid, topic, new_tries)
    return False


def _fulfill_paint(config, diary_db_path, cid, topic, tries) -> bool:
    """Namaluj obraz na námět `topic` → cesta k obrázku do result + done.
    Render selhal → počítej pokus, po 3 uzavři honest hláškou. True když uzavřeno."""
    rel = None
    try:
        from scripts import hans_art
        r = hans_art.paint_subject(config, diary_db_path, topic)
        if r:
            rel = r[0]  # (rel_path, caption)
    except Exception as e:
        _log.warning("fulfill: paint '%s': %s", topic, e)
    if rel:
        # result = cesta k obrázku (marker pro doručení send_photo). NENÍ text.
        _close_fulfilled(diary_db_path, cid, str(rel), topic=topic,
                         grounded=False)
        _log.info("fulfill: slib #%d '%s' → obraz %s", cid, topic, rel)
        return True
    new_tries = tries + 1
    if new_tries >= 3:
        _close_fulfilled(diary_db_path, cid,
                         "Obraz se mi bohužel nepodařilo vytvořit.")
        _log.info("fulfill: slib #%d '%s' → obraz nevyšel (po %d)", cid, topic,
                  new_tries)
        return True
    _bump_tries(diary_db_path, cid, new_tries)
    _log.info("fulfill: slib #%d '%s' render nevyšel (pokus %d/3)", cid, topic,
              new_tries)
    return False


def _close_fulfilled(diary_db_path: str, cid: int, result: str,
                     topic: str = "", grounded: bool = False) -> None:
    try:
        db = sqlite3.connect(diary_db_path, timeout=5.0)
        db.execute("UPDATE commitments SET result=?, status='done', "
                   "done_ts=?, reported=0 WHERE id=?",
                   (result, time.time(), int(cid)))
        # HANS_COMMIT_FULFILL_V1 — grounded nález ať vstoupí i do Hansovy
        # PAMĚTI (deník web_read → narativ/importance/recall), ne jen do slibu.
        # „Nenašel jsem" NEukládat — není to znalost.
        if grounded and topic:
            note = "Zjistil jsem o tématu %s (na základě slibu): %s" % (
                topic, result)
            db.execute(
                "INSERT INTO diary (ts, event_type, title, note) VALUES (?,?,?,?)",
                (time.time(), "web_read", topic, note))
        db.commit(); db.close()
    except Exception as e:
        _log.warning("_close_fulfilled #%s: %s", cid, e)


def _bump_tries(diary_db_path: str, cid: int, tries: int) -> None:
    try:
        db = sqlite3.connect(diary_db_path, timeout=5.0)
        db.execute("UPDATE commitments SET tries=? WHERE id=?",
                   (int(tries), int(cid)))
        db.commit(); db.close()
    except Exception as e:
        _log.warning("_bump_tries #%s: %s", cid, e)


def unreported_results(diary_db_path: str, limit: int = 5) -> List[tuple]:
    """HANS_COMMIT_FULFILL_V1 — [(id, person, topic, text, result, kind), …]
    slibů, které Hans splnil AKCÍ (research/paint, result je), ale ještě je
    majiteli NEŘEKL (reported=0). kind rozliší text (research) vs foto (paint)."""
    try:
        db = sqlite3.connect("file:%s?mode=ro" % diary_db_path, uri=True,
                             timeout=5.0)
    except Exception:
        return []
    try:
        return db.execute(
            "SELECT id, person, topic, text, result, kind FROM commitments WHERE "
            "status='done' AND result!='' AND reported=0 "
            "ORDER BY done_ts ASC LIMIT ?", (int(limit),)).fetchall()
    except Exception:
        return []
    finally:
        try:
            db.close()
        except Exception:
            pass


def due_reminders(diary_db_path: str, limit: int = 5) -> List[tuple]:
    """HANS_COMMIT_FULFILL_V1 — [(id, person, topic, text), …] připomínek, které
    NADEŠLY (due_ts prošel, nebo bez termínu = připomeň při nejbližším setkání)
    a ještě nebyly doručeny. reminder = doručení textu, žádná noční akce."""
    now = time.time()
    try:
        db = sqlite3.connect("file:%s?mode=ro" % diary_db_path, uri=True,
                             timeout=5.0)
    except Exception:
        return []
    try:
        return db.execute(
            "SELECT id, person, topic, text FROM commitments WHERE "
            "status='open' AND kind='reminder' AND reported=0 "
            "AND (due_ts=0 OR due_ts<=?) ORDER BY created_ts ASC LIMIT ?",
            (now, int(limit))).fetchall()
    except Exception:
        return []
    finally:
        try:
            db.close()
        except Exception:
            pass


def timed_reminders_due(diary_db_path: str, lead_seconds: float = 1800.0,
                        limit: int = 5) -> List[tuple]:
    """HANS_COMMIT_FULFILL_V1 (timer) — připomínky S TERMÍNEM, do jejichž
    termínu zbývá ≤ `lead_seconds` (default 30 min) → poslat TEĎ, i bez
    přítomnosti (Telegram). [(id, person, topic, text, due_ts), …]."""
    threshold = time.time() + float(lead_seconds)
    try:
        db = sqlite3.connect("file:%s?mode=ro" % diary_db_path, uri=True,
                             timeout=5.0)
    except Exception:
        return []
    try:
        return db.execute(
            "SELECT id, person, topic, text, due_ts FROM commitments WHERE "
            "status='open' AND kind='reminder' AND reported=0 AND due_ts>0 "
            "AND due_ts<=? ORDER BY due_ts ASC LIMIT ?",
            (threshold, int(limit))).fetchall()
    except Exception:
        return []
    finally:
        try:
            db.close()
        except Exception:
            pass


def mark_reminder_delivered(diary_db_path: str, ids: List[int]) -> int:
    """Připomínka doručena → status='done' + reported=1."""
    if not ids:
        return 0
    try:
        db = sqlite3.connect(diary_db_path, timeout=5.0)
    except Exception:
        return 0
    try:
        ph = ",".join("?" * len(ids))
        cur = db.execute(
            "UPDATE commitments SET status='done', reported=1, done_ts=? "
            "WHERE id IN (%s)" % ph,
            tuple([time.time()] + [int(i) for i in ids]))
        db.commit()
        return cur.rowcount
    except Exception as e:
        _log.warning("mark_reminder_delivered: %s", e)
        return 0
    finally:
        try:
            db.close()
        except Exception:
            pass


def mark_reported(diary_db_path: str, ids: List[int]) -> int:
    """Označ splněné sliby jako sdělené majiteli."""
    if not ids:
        return 0
    try:
        db = sqlite3.connect(diary_db_path, timeout=5.0)
    except Exception:
        return 0
    try:
        ph = ",".join("?" * len(ids))
        cur = db.execute(
            "UPDATE commitments SET reported=1 WHERE id IN (%s)" % ph,
            tuple(int(i) for i in ids))
        db.commit()
        return cur.rowcount
    except Exception as e:
        _log.warning("mark_reported: %s", e)
        return 0
    finally:
        try:
            db.close()
        except Exception:
            pass


def _fmt_commit(row) -> str:
    text = row[0]
    due_ts = row[2] if len(row) > 2 else 0
    due_text = row[3] if len(row) > 3 else ""
    if due_ts and due_ts > 0:
        import datetime as _dt
        d = _dt.datetime.fromtimestamp(due_ts)
        return "- %s (termín: %d.%d. %02d:%02d)" % (
            text, d.day, d.month, d.hour, d.minute)
    if due_text:
        return "- %s (termín: %s)" % (text, due_text)
    return "- %s" % text


def commitments_answer(diary_db_path: str, text: str,
                       person: Optional[str] = None) -> str:
    """Grounded blok pro „co jsi mi slíbil". Prázdný string, když to není
    slibový dotaz. Když je, ale žádný slib není → honestní pokyn „nic
    nevymýšlej" (uzavírá konfabulaci pro tuhle třídu dotazů)."""
    if not is_commitment_query(text):
        return ""
    rows = open_commitments(diary_db_path, person=person)
    if not rows:
        return ("\n\nSKUTEČNÝ ZÁZNAM tvých otevřených slibů této osobě: ŽÁDNÝ. "
                "Řekni upřímně, že si nejsi vědom žádného nesplněného slibu v "
                "záznamu — NEVYMÝŠLEJ žádný slib.")
    lst = "\n".join(_fmt_commit(r) for r in rows)
    return ("\n\nSKUTEČNÝ SEZNAM tvých otevřených slibů (odpověz JEN z něj, "
            "nevymýšlej další ani detaily; termín uveď, jen když je uvedený):\n"
            + lst)


def mark_done(diary_db_path: str, commitment_id: int) -> bool:
    try:
        db = sqlite3.connect(diary_db_path, timeout=5.0)
        _init(db)
        cur = db.execute(
            "UPDATE commitments SET status='done', done_ts=? WHERE id=? "
            "AND status='open'", (time.time(), int(commitment_id)))
        db.commit()
        ok = cur.rowcount > 0
        db.close()
        return ok
    except Exception as e:
        _log.warning("mark_done: %s", e)
        return False
