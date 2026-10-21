"""HANS_BOOK_SUMMARY_V1 (16. 10.) — souhrn děje přečtené knihy.

Proč: Hans z knihy dosud měl jen svůj NÁZOR na kapitolu, a to z prvních
~1 500 znaků úseku (`hans_library.read_next_chapter` → `book_read` → hook
`book_reflection`), tedy 16–50 % knihy. Závěrečná úvaha (`reflect_on_book`)
pak brala jen prvních 3 000 znaků těch názorů (u Zeměmoří ~7 ze 109) a děj si
domýšlela („náhlá ztráta Kargova“, Shylock v Ivanhoe). Na „o čem ta kniha je?“
neměl z čeho odpovědět. 📏 backlog `KNIHY_SOUHRN_16_10`: souhrn z CELÝCH úseků
Baskervillského psa správný až na 2 z 15; z ořezu chybí rozuzlení.

Noční úloha (`hans_routine_noc._nt_book_summary`), po kouscích s časovým
rozpočtem, deferral-safe:
  1. ke každému přečtenému úseku souhrn děje z CELÉHO textu (qwen3 anglicky,
     druhá Ollama jen na CPU — qwen3 a hans-czech střídavě v jedné Ollamě
     14. 10. zasekly plánovač) → `hans_library_chapters.summary`;
  2. dočtená kniha se všemi souhrny → souhrn knihy po patrech → česky
     (hans-czech) → deník `book_summary` (čte ho /cetl i fulltext);
  3. nová závěrečná úvaha ze souhrnu knihy a z názorů rozložených přes celou
     knihu; stará úvaha (stála na začátku knihy) dostane opravný záznam
     `correction` a její kopie v RAG se odebere (historie v deníku zůstává).
"""
import json
import logging
import re
import sqlite3
import time
from pathlib import Path
from typing import Optional

_log = logging.getLogger(__name__)

_KAP_SYS = ("You summarize chapters of books. State only what happens in the given text: "
            "who appears, what they do, what is revealed. No opinions, nothing beyond the text.")
_KAP_P = ("BOOK: {b}\nPART {i} of {n}:\n{t}\n\n"
          "Summarize what happens in this part in 2-3 sentences.")
_PATRO_P = ("Below are summaries of consecutive parts of the book \"{b}\". Write ONE summary of "
            "these parts in 4-6 sentences, keeping names and key events in order. Use only what "
            "the summaries say.\n\n{s}")
_KNIHA_P = ("Below are summaries of consecutive sections of the book \"{b}\". Write a summary of "
            "the WHOLE book in 8-10 sentences: main characters, what happens, how it ends. "
            "Use only what the summaries say; do not add anything you know from elsewhere.\n\n{s}")
_CS_P = ("Přelož do češtiny tento souhrn knihy „{b}“. Zachovej jména a události, nic "
         "nepřidávej ani nevynechávej, piš souvislý text bez odrážek.\n\n{t}")
_SCHEMA = {"type": "object", "properties": {"summary": {"type": "string"}},
           "required": ["summary"]}


def _cfg(config: dict) -> dict:
    return (config or {}).get("book_summary", {}) or {}


def _cpu_url(config: dict) -> str:
    """Druhá Ollama jen na CPU (port 11435) na témž PC jako hlavní."""
    u = _cfg(config).get("ollama_url")
    if u:
        return str(u).rstrip("/")
    base = str(((config or {}).get("openwebui_chat", {}) or {}).get("base_url") or "")
    return re.sub(r":\d+/?$", ":11435", base.rstrip("/")) if base else ""


# HANS_BOOK_SUMMARY_RAM_V1 (17. 10.) — noc 16.→17. 10.: qwen3 na CPU (~19,5 GB
# RAM) souběžně se studijním modelem na hlavní Ollamě → PC 32 GB do swapu,
# zamrzlo. Před každým voláním: na hlavní Ollamě jen hlasový/vektorový model
# a dost volné RAM (víc, když se qwen3 teprve načítá).
_POVOLENE_HLAVNI = ("hans-czech", "bge-m3")


def _ps(url: str) -> list:
    import requests
    try:
        r = requests.get(url.rstrip("/") + "/api/ps", timeout=8)
        return [str(m.get("name") or "") for m in (r.json().get("models") or [])]
    except Exception:
        return []


def _pamet_ok(config: dict):
    """(True, '') nebo (False, důvod)."""
    c = _cfg(config)
    hlavni = str(((config or {}).get("openwebui_chat", {}) or {}).get("base_url") or "")
    cizi = [m for m in _ps(hlavni) if not m.startswith(_POVOLENE_HLAVNI)]
    if cizi:
        return False, "na hlavní Ollamě běží %s" % ", ".join(cizi)
    nacteny = any(m.startswith(str(c.get("model", "qwen3:30b")).split(":")[0])
                  for m in _ps(_cpu_url(config)))
    try:
        from scripts import pc_remote
        t = pc_remote.telemetry(config) or {}
    except Exception:
        t = {}
    volno = t.get("ram_avail_gb")
    if volno is None:
        return False, "volnou RAM na PC nejde změřit"
    need = float(c.get("min_ram_gb", 4) if nacteny else c.get("min_ram_gb_nacteni", 22))
    if volno < need:
        return False, "volná RAM %.1f GB < %.0f GB%s" % (volno, need,
                                                       "" if nacteny else " (qwen3 se načítá)")
    return True, ""


_ram_hlaseno = [0.0]


def _odloz_pro_pamet(config: dict) -> bool:
    ok, proc = _pamet_ok(config)
    if ok:
        return False
    if time.time() - _ram_hlaseno[0] > 3600:      # do logu nejvýš 1× za hodinu
        _ram_hlaseno[0] = time.time()
        _log.info("HANS_BOOK_SUMMARY_RAM_V1: odkládám — %s", proc)
    return True


def _qwen(config: dict, prompt: str, system: str = _KAP_SYS,
          num_predict: int = 600) -> Optional[str]:
    from scripts.ollama_client import ollama_generate
    c = _cfg(config)
    raw = ollama_generate(
        str(c.get("model", "qwen3:30b")), prompt, system=system,
        ollama_url=_cpu_url(config), config=config,
        timeout=int(c.get("timeout_s", 600)), keep_alive="10m",
        options={"num_gpu": 0, "temperature": 0.2, "num_ctx": 8192,
                 "num_predict": num_predict},
        format=_SCHEMA, think=False)
    if not raw:
        return None
    try:
        s = json.loads(re.sub(r"<think>.*?</think>", "", raw, flags=re.S)).get("summary")
    except Exception:
        _log.warning("HANS_BOOK_SUMMARY_V1: nečitelný výstup %.120r", raw)
        return None
    s = str(s or "").strip()
    return s or None


def _knihy(db) -> list:
    return db.execute(
        "SELECT book_id, book_title, total_chapters, current_chapter, status, "
        "completion_reflected FROM hans_library "
        "WHERE status IN ('reading','finished') AND current_chapter > 0 "
        # rozečtená napřed (drží krok s četbou), pak dočtené od nejkratší —
        # opravy starých úvah tak přibývají co nejdřív
        "ORDER BY (status='reading') DESC, total_chapters, id").fetchall()


def _ensure(db) -> None:
    cols = {r[1] for r in db.execute("PRAGMA table_info(hans_library)")}
    for c, t in (("summary_en", "TEXT DEFAULT ''"), ("summary_cs", "TEXT DEFAULT ''"),
                 ("summary_ts", "REAL")):
        if c not in cols:
            db.execute("ALTER TABLE hans_library ADD COLUMN %s %s" % (c, t))
    db.commit()


def _text_useku(books_dir: Path, book_id: str, n: int) -> str:
    p = books_dir / book_id / ("ch%03d.txt" % n)
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""


def _chybejici(db, book_id: str, do_kap: int) -> list:
    hotove = {r[0] for r in db.execute(
        "SELECT chapter_num FROM hans_library_chapters WHERE book_id=? "
        "AND COALESCE(summary,'')!=''", (book_id,))}
    return [n for n in range(1, int(do_kap or 0) + 1) if n not in hotove]


def _uloz_usek(db, book_id: str, n: int, s: str) -> None:
    cur = db.execute("UPDATE hans_library_chapters SET summary=? WHERE book_id=? "
                     "AND chapter_num=?", (s, book_id, n))
    if not cur.rowcount:
        db.execute("INSERT INTO hans_library_chapters (book_id, chapter_num, title, "
                   "summary, read_at) VALUES (?,?,?,?,?)",
                   (book_id, n, "Kapitola %d" % n, s, time.time()))
    db.commit()


def _souhrn_knihy(config: dict, title: str, souhrny: list) -> Optional[str]:
    """Po patrech: skupiny po `patro` souhrnech → souhrn skupiny → souhrn knihy."""
    patro = int(_cfg(config).get("patro", 12))
    uroven = ["Part %d: %s" % (i, s) for i, s in souhrny]
    while len(uroven) > patro:
        nove = []
        for k in range(0, len(uroven), patro):
            s = _qwen(config, _PATRO_P.format(b=title, s="\n".join(uroven[k:k + patro])),
                      num_predict=700)
            if not s:
                return None
            nove.append("Section %d: %s" % (len(nove) + 1, s))
        uroven = nove
    return _qwen(config, _KNIHA_P.format(b=title, s="\n".join(uroven)), num_predict=1000)


def _cesky(config: dict, title: str, text: str) -> Optional[str]:
    from scripts.ollama_client import ollama_generate
    model = str(((config or {}).get("hans_dialog", {}) or {}).get(
        "ollama_model", "hans-czech:latest"))
    out = ollama_generate(model, _CS_P.format(b=title, t=text), config=config,
                          timeout=300, options={"temperature": 0.2, "num_ctx": 4096,
                                                "num_predict": 1200})
    return (out or "").strip() or None


def _nazory_rozlozene(db, title: str, n: int = 4) -> list:
    rows = [r[0].strip() for r in db.execute(
        "SELECT data FROM diary WHERE event_type='book_reflection' AND title LIKE ? "
        "AND COALESCE(data,'')!='' ORDER BY ts", (title + " —%",)) if r[0]]
    if len(rows) <= n:
        return rows
    krok = len(rows) / float(n)
    return [rows[int(i * krok)] for i in range(n)]


def _oprav_starou_uvahu(config: dict, db, title: str, nova_id: Optional[int]) -> None:
    """Starší závěrečné úvahy ke knize: opravný záznam + odebrat jejich kopii z RAG."""
    stare = db.execute(
        "SELECT id, ts FROM diary WHERE event_type='book_completion_reflection' "
        "AND title=? AND id!=? ORDER BY ts", ("Dočtená kniha — %s" % title, nova_id or -1)).fetchall()
    if not stare:
        return
    try:
        from scripts.hans_knowledge import HansKnowledge, _KNOWLEDGE_DB
        kn = HansKnowledge(config)
        kdb = sqlite3.connect(_KNOWLEDGE_DB, timeout=5)
    except Exception:
        kn, kdb = None, None
    for sid, sts in stare:
        smazano = []
        if kn is not None and kdb is not None:
            den = time.strftime("%Y%m%d", time.localtime(sts))
            for coll, doc in kdb.execute(
                    "SELECT collection_key, doc_id FROM uploads WHERE doc_id LIKE ? "
                    "AND uploaded_at BETWEEN ? AND ?", ("book_%s_%%" % den, sts - 5, sts + 600)):
                try:
                    if kn.delete(coll, doc):
                        smazano.append(doc)
                except Exception:
                    pass
        db.execute(
            "INSERT INTO diary (ts, event_type, title, note, data) VALUES (?,?,?,?,?)",
            (time.time(), "correction", "Oprava úvahy o knize — %s" % title,
             "Moje dřívější úvaha o dočtené knize stála jen na začátku knihy a děj si "
             "domýšlela; nahradil jsem ji novou, napsanou podle souhrnu celé knihy.",
             json.dumps({"opraveno_id": sid, "nove_id": nova_id, "rag_odebrano": smazano,
                         "marker": "HANS_BOOK_SUMMARY_V1"}, ensure_ascii=False)))
        db.commit()
        _log.info("HANS_BOOK_SUMMARY_V1: úvaha %d o %r opravena (RAG odebráno %s)",
                  sid, title, smazano or "nic")
    if kdb is not None:
        kdb.close()


# HANS_BOOK_SUMMARY_SLOT_V1 (21. 10.) — noc 20.→21. 10.: PC probuzené ve 3:01,
# souhrny knih prošly pojistkou RAM (prázdná hlavní Ollama, volná RAM) a hned
# po nich se načetlo studium (OpenEuroLLM), hans-czech a qwen2.5 → PC zamrzlo
# do ranního ručního restartu (stejně jako 16.→17. 10.). Pojistka RAM se ptá
# jen v okamžiku startu, na pozdější příchozí nevidí. Proto souhrny drží
# po celý běh sdílený slot velkého modelu (studium se při obsazeném slotu
# odloží, reflexe a Koláč ustoupí) a po běhu qwen3 z RAM uvolní, ať se
# další úloha nenačítá vedle něj (keep_alive 10 min).
def run_night(config: dict, diary_path: str, reflection=None,
              budget_s: Optional[float] = None) -> str:
    """Jeden noční krok. Vrací 'idle' | 'deferred' | popis práce."""
    c = _cfg(config)
    if not c.get("enabled", True):
        return "idle"
    _h = time.localtime().tm_hour
    if not (int(c.get("od_hodiny", 2)) <= _h < 12):
        return "deferred"
    from scripts import ollama_client as _oc
    if _oc.base_slot_busy():
        if time.time() - _slot_hlaseno[0] > 3600:
            _slot_hlaseno[0] = time.time()
            _log.info("HANS_BOOK_SUMMARY_SLOT_V1: odkládám — slot drží '%s'",
                      _oc.base_slot_label())
        return "deferred"
    budget = float(budget_s if budget_s is not None else c.get("budget_s", 1500))
    _tok = _oc.acquire_base_slot("souhrny knih",
                                 budget + 2 * float(c.get("timeout_s", 600)) + 300,
                                 wait_s=0)
    if _tok is None:
        return "deferred"
    try:
        return _run_night_body(config, diary_path, reflection, budget_s)
    finally:
        _oc.release_base_slot(_tok)
        try:
            _cpu = _cpu_url(config)
            if _cpu and _ps(_cpu):
                _n = _oc.ollama_unload_all(_cpu, config)
                _log.info("HANS_BOOK_SUMMARY_SLOT_V1: konec běhu — z CPU Ollamy "
                          "uvolněno %d modelů", _n)
        except Exception as _ue:
            _log.debug("book_summary unload: %s", _ue)


_slot_hlaseno = [0.0]


def _run_night_body(config: dict, diary_path: str, reflection=None,
                    budget_s: Optional[float] = None) -> str:
    c = _cfg(config)
    if not c.get("enabled", True):
        return "idle"
    # od 2:00 (uživatel 16. 10.: PC večer kolem 22:00 potřebuje volné); qwen3
    # na CPU vytíží procesor na desítky minut
    _h = time.localtime().tm_hour
    if not (int(c.get("od_hodiny", 2)) <= _h < 12):
        return "deferred"
    from scripts.ollama_client import brain_available
    if not brain_available(config, ollama_url=_cpu_url(config)) or not brain_available(config):
        return "deferred"
    budget = float(budget_s if budget_s is not None else c.get("budget_s", 1500))
    konec = time.time() + budget
    books_dir = Path(str(((config or {}).get("library", {}) or {}).get("books_dir")
                         or "data/library"))
    db = sqlite3.connect(diary_path, timeout=10)
    try:
        _ensure(db)
        hotovo_useku, knihy_hotove = 0, []
        for book_id, title, total, cur, status, _refl in _knihy(db):
            if time.time() > konec:
                break
            chybi = _chybejici(db, book_id, cur)
            for n in chybi:
                if time.time() > konec:
                    break
                t = _text_useku(books_dir, book_id, n)
                if not t.strip():
                    _uloz_usek(db, book_id, n, "(prázdný úsek)")
                    continue
                if _odloz_pro_pamet(config):
                    return "deferred" if not hotovo_useku else "úseků %d, pak málo RAM" % hotovo_useku
                s = _qwen(config, _KAP_P.format(b=title, i=n, n=total, t=t[:24000]))
                if not s:
                    return "deferred" if not hotovo_useku else "úseků %d, pak mozek dole" % hotovo_useku
                _uloz_usek(db, book_id, n, s)
                hotovo_useku += 1
            if status != "finished" or _chybejici(db, book_id, total):
                continue
            row = db.execute("SELECT COALESCE(summary_cs,'') FROM hans_library WHERE book_id=?",
                             (book_id,)).fetchone()
            if row and row[0]:
                continue
            if time.time() > konec:
                break
            souhrny = [(r[0], r[1]) for r in db.execute(
                "SELECT chapter_num, summary FROM hans_library_chapters WHERE book_id=? "
                "AND COALESCE(summary,'') NOT IN ('', '(prázdný úsek)') "
                "GROUP BY chapter_num ORDER BY chapter_num", (book_id,))]
            if _odloz_pro_pamet(config):
                return "deferred"
            en = _souhrn_knihy(config, title, souhrny)
            cs = _cesky(config, title, en) if en else None
            if not (en and cs):
                return "deferred"
            db.execute("UPDATE hans_library SET summary_en=?, summary_cs=?, summary_ts=? "
                       "WHERE book_id=?", (en, cs, time.time(), book_id))
            db.execute("INSERT INTO diary (ts, event_type, title, note, data) VALUES (?,?,?,?,?)",
                       (time.time(), "book_summary", "Souhrn knihy — %s" % title, cs,
                        json.dumps({"book_id": book_id, "marker": "HANS_BOOK_SUMMARY_V1"})))
            db.commit()
            _log.info("HANS_BOOK_SUMMARY_V1: souhrn knihy %r (%d úseků, %d zn)",
                      title, len(souhrny), len(cs))
            # nová závěrečná úvaha ze souhrnu celé knihy
            nova_id = None
            if reflection is not None and hasattr(reflection, "reflect_on_book"):
                mats = ["Souhrn celé knihy: " + cs] + _nazory_rozlozene(db, title)
                txt = reflection.reflect_on_book(title, mats)
                if txt:
                    r = db.execute("SELECT id FROM diary WHERE event_type="
                                   "'book_completion_reflection' AND title=? ORDER BY ts DESC "
                                   "LIMIT 1", ("Dočtená kniha — %s" % title,)).fetchone()
                    nova_id = r[0] if r else None
                    db.execute("UPDATE hans_library SET completion_reflected=1 WHERE book_id=?",
                               (book_id,))
                    db.commit()
                    _oprav_starou_uvahu(config, db, title, nova_id)
            knihy_hotove.append(title)
        if not hotovo_useku and not knihy_hotove:
            return "idle"
        return "úseků %d, knih %d" % (hotovo_useku, len(knihy_hotove))
    finally:
        db.close()
