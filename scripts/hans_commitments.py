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


def _is_operational(promise: str) -> bool:
    pl = (promise or "").lower()
    if any(k in pl for k in _OPERATIONAL):
        return True
    # info-lookup akce = ne budoucí slib
    return bool(_INFO_LOOKUP_RE.search(promise or ""))


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
    "- sliby, které dal ČLOVĚK, ne {persona_name}.\n\n"
    "Když žádný skutečný závazek není, vrať prázdné pole [].\n"
    "U každého slibu vyplň i „due“ = TERMÍN, POKUD ho slib zmiňuje (např. "
    "„do neděle“, „zítra“, „za týden“, „do konce týdne“); když termín uveden "
    "NENÍ, dej prázdný řetězec.\n"
    "Vrať POUZE JSON pole objektů, nic jiného:\n"
    "[{{\"promise\": \"<slib slovy {persona_name}a, 1. osoba, krátce a "
    "konkrétně>\", \"due\": \"<termín nebo prázdné>\"}}]"
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
                db.execute(
                    "INSERT INTO commitments (person, text, text_norm, status, "
                    "source_ts, created_ts, due_ts, due_text) "
                    "VALUES (?,?,?,'open',?,?,?,?)",
                    (person, promise, pn, since, time.time(), due_ts, due_text))
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
