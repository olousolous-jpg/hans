#!/usr/bin/env python3
"""
HANS_STUDY_V1 — Studijní program: zvídavost → SKUTEČNÁ hloubka.

Doposud Hans četl roztříštěně (náhodná Wiki / curiosity), koníčky byly jen tagy.
Tato vrstva dává Hansovi DLOUHODOBÝ vlastní projekt: vybere si trvalý koníček
(durable hobby — má je, hrady/Cardiff, design) a jde do hloubky přes týdny =
strukturovaný studijní program:

  1. ensure_program  — vybere durable koníček, LLM vygeneruje KURIKULUM
                       (6-10 pod-témat v pořadí od základů k pokročilému).
  2. study_next      — jedna noční session nastuduje DALŠÍ pod-téma
                       (Wikipedia → poznámka v 1. osobě → deník study_note + RAG).
  3. synthesize_progress — po dokončení kurikula mistrovská reflexe
                       ("co teď o tématu vím a jak mě to formuje") → grounduje
                       Severčinu VOCATIONAL identitu reálnou znalostí.

NÍZKOSTAKOVÉ (na rozdíl od Severky): jen čte a poznámkuje, nemutuje identitu
ani postoje přímo. Proto je gate stálosti volnější (config) než u Severky.

Tabulka `study_program` v hans_diary.db:
  id, topic, curriculum(JSON), current_index, status(active|completed|abandoned),
  sessions_done, started_ts, updated_ts, last_session_ts

Wiring: 1 session/noc v hans_routine nočním ticku (run_study_session).
LLM části (kurikulum/poznámka/syntéza) běží v noci na base modelu keep_alive=0
(anti-konfabulace + VRAM tier; [[ollama-vram-tiers]]). Deferral-safe.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from typing import List, Optional

_log = logging.getLogger("hans_study")

_WS = re.compile(r"\s+")


def _norm(s: str) -> str:
    return _WS.sub(" ", (s or "").strip().lower())


def _cfg(config: dict) -> dict:
    return (config.get("study", {}) or {})


def _model(config: dict) -> str:
    c = _cfg(config)
    er = config.get("evening_reflection", {}) or {}
    return str(c.get("model", er.get("model",
                     "jobautomation/OpenEuroLLM-Czech:latest")))


def _parse_json_list(raw: str) -> list:
    s = re.sub(r"^```(?:json)?|```$", "", (raw or "").strip(),
               flags=re.MULTILINE).strip()
    i, j = s.find("["), s.rfind("]")
    if i == -1 or j == -1 or j < i:
        return []
    try:
        data = json.loads(s[i:j + 1])
    except Exception:
        return []
    return data if isinstance(data, list) else []


# ── Kurikulum (LLM zobecní koníček na pořadí pod-témat) ─────────────────────
# PERSONA_NAME_CONFIGURABLE_V1 — {persona_name} se doplní z configu
_CURRICULUM_SYSTEM = (
    "Jsi tutor, který postavě jménem {persona_name} sestavuje studijní plán pro "
    "hluboké, systematické zvládnutí jednoho koníčku. Dostaneš NÁZEV koníčku a "
    "několik konkrétních příkladů, které pod něj spadají. Navrhni KURIKULUM — "
    "uspořádaný seznam {n} pod-témat od základů k pokročilejším, tak aby je šlo "
    "studovat jedno po druhém po týdnech. Každé pod-téma musí být konkrétní, "
    "samostatně dohledatelné (vhodné jako dotaz do encyklopedie) a v ČEŠTINĚ. "
    "Žádné obecné fráze typu 'úvod' nebo 'historie' bez upřesnění; nevymýšlej si "
    "nesmysly. Vrať VÝHRADNĚ JSON pole {n} řetězců (názvy pod-témat), nic víc."
)


def _generate_curriculum(config: dict, topic: str, examples: list) -> list:
    """Base LLM vygeneruje uspořádané kurikulum pod-témat. [] při selhání."""
    n = int(_cfg(config).get("curriculum_size", 8))
    n = max(4, min(12, n))
    ex = ", ".join(str(e) for e in (examples or [])[:8])
    prompt = (f"Koníček: {topic}\n"
              f"Příklady, které pod něj spadají: {ex or '(žádné)'}\n\n"
              f"Sestav kurikulum {n} pod-témat v pořadí ke studiu.")
    timeout = int(_cfg(config).get("llm_timeout", 300))
    try:
        from scripts.ollama_client import ollama_generate
        from scripts.hans_persona import persona_name as _pn
    except ImportError:
        _log.warning("_generate_curriculum: moduly nedostupné, skip")
        return []
    try:
        system = _CURRICULUM_SYSTEM.format(persona_name=_pn(config), n=n)
        raw = ollama_generate(model=_model(config), prompt=prompt, system=system,
                              config=config, timeout=timeout,
                              keep_alive=0,  # MODEL_KEEPALIVE_TIERS_V1
                              options={"temperature": 0.3})
    except Exception as e:
        _log.warning("_generate_curriculum LLM selhal: %s", e)
        return []
    items = _parse_json_list(raw)
    out = []
    seen = set()
    for it in items:
        s = str(it).strip().lstrip("0123456789.) -").strip()
        if len(s) >= 3 and _norm(s) not in seen:
            out.append(s)
            seen.add(_norm(s))
    return out[:n]


# ── Hloubkové čtení: plný článek + intro pododkazů (HANS_STUDY_DEEP_V1) ──────
_GENERIC_LEADIN = re.compile(
    r"^(základy|úvod do|úvod|práce s|práce se|principy|teorie|tvorba|"
    r"co je|historie|vývoj)\s+", re.IGNORECASE)


def _search_queries(sub: str, topic: str) -> List[str]:
    """STUDY_SEARCH_FALLBACK_V1 — kurikulum dává popisné fráze
    ('Základy typografie: fonty, kerning, leading a tracking'), které Wikipedia
    full-text search jako celek NEnajde (srsearch vrátí None) → study by skončilo
    'noread'→skip a nenastudovalo nic. Vyrob postupně užší dotazy: plná fráze →
    část před dvojtečkou ('Základy typografie') → jádro bez generického úvodu
    ('typografie') → s tématem. Vrací deduplikované neprázdné kandidáty v pořadí
    od nejkonkrétnějšího."""
    out: List[str] = []
    seen = set()

    def _add(q: str):
        q = (q or "").strip(" .,–-")
        if q and q.lower() not in seen:
            seen.add(q.lower())
            out.append(q)

    s = (sub or "").strip()
    head = s.split(":")[0].strip()           # část před dvojtečkou
    core = _GENERIC_LEADIN.sub("", head).strip()        # bez "Základy/Teorie…"
    core = re.sub(r"\s+v\s+\w+$", "", core).strip()     # "Kompozice v designu"→"Kompozice"
    # JÁDRO nejdřív = nejkanoničtější článek (full-text search dá u dlouhé popisné
    # fráze často nesmysl-ale-neprázdný výsledek → stopne se na něm; čisté jádro
    # trefí správný článek). Pak širší fallbacky.
    _add(core)
    _add(head)
    _add(s)
    _add(f"{core} {topic}".strip() if core else f"{s} {topic}".strip())
    return out


# ── HANS_STUDY_RESEARCH_TIER_V1 — deep tier (skutečný výzkum nad Wikipedií) ──
def _reconstruct_abstract(inv) -> str:
    """OpenAlex vrací abstrakt jako inverted index (slovo→pozice). Slož zpět text."""
    if not isinstance(inv, dict) or not inv:
        return ""
    pos = {}
    for word, idxs in inv.items():
        for i in idxs:
            pos[i] = word
    if not pos:
        return ""
    return " ".join(pos[i] for i in range(max(pos) + 1) if i in pos)


def _seen_work_ids(db_path: str) -> set:
    """ID prací, které už Hans v nějaké poznámce použil (dedup napříč sessiony)."""
    if not db_path:
        return set()
    try:
        import sqlite3 as _s
        conn = _s.connect(db_path, timeout=5.0)
        conn.execute("CREATE TABLE IF NOT EXISTS study_seen_works "
                     "(work_id TEXT PRIMARY KEY, title TEXT, ts REAL)")
        rows = conn.execute("SELECT work_id FROM study_seen_works").fetchall()
        conn.close()
        return {r[0] for r in rows if r and r[0]}
    except Exception:
        return set()


def _record_works(db_path: str, items) -> None:
    """Zapamatuj použité práce (work_id, title), ať se příště neopakují."""
    if not db_path or not items:
        return
    try:
        import sqlite3 as _s
        conn = _s.connect(db_path, timeout=5.0)
        conn.execute("CREATE TABLE IF NOT EXISTS study_seen_works "
                     "(work_id TEXT PRIMARY KEY, title TEXT, ts REAL)")
        conn.executemany(
            "INSERT OR IGNORE INTO study_seen_works (work_id,title,ts) "
            "VALUES (?,?,?)", [(w, t, time.time()) for w, t in items])
        conn.commit()
        conn.close()
    except Exception as e:
        _log.debug("_record_works: %s", e)


def _openalex_research(config: dict, query: str, n: int = 3,
                       max_chars: int = 4000, db_path: str = None) -> str:
    """Vytáhne z OpenAlexu pár nejrelevantnějších NOVÝCH prací (název+rok+autoři+
    abstrakt) k tématu. DEDUP: práce použité dřív (study_seen_works) přeskočí, ať
    Hans necituje stejnou práci/autora opakovaně. '' při chybě/nic. Best-effort."""
    import requests
    rc = _cfg(config).get("research_tier", {}) or {}
    mailto = rc.get("mailto", "hans@local")
    seen = _seen_work_ids(db_path)
    try:
        r = requests.get(
            "https://api.openalex.org/works",
            # ber víc kandidátů (n + rezerva), ať po vyřazení viděných zbude n nových
            params={"search": query, "per-page": int(n) + 6, "mailto": mailto,
                    "sort": "relevance_score:desc"},
            timeout=int(rc.get("timeout", 20)))
        r.raise_for_status()
        works = (r.json() or {}).get("results", []) or []
    except Exception as e:
        _log.warning("research tier OpenAlex selhal (%s): %s", query, e)
        return ""
    blocks = []
    new_items = []
    skipped = 0
    for w in works:
        wid = w.get("id") or ""
        title = (w.get("title") or "").strip()
        abstract = _reconstruct_abstract(w.get("abstract_inverted_index"))
        if not title or len(abstract) < 80:
            continue
        if wid and wid in seen:
            skipped += 1
            continue
        year = w.get("publication_year") or ""
        authors = ", ".join(
            (a.get("author") or {}).get("display_name", "")
            for a in (w.get("authorships") or [])[:3] if a)
        blocks.append(f"[Výzkum: {title} ({year}; {authors})]\n{abstract[:1500]}")
        if wid:
            new_items.append((wid, title))
        if len(blocks) >= int(n):
            break
    _record_works(db_path, new_items)
    out = "\n\n".join(blocks)
    if out:
        _log.info("study: research tier — %d nových prací pro '%s' (%d již viděných)",
                  len(blocks), query, skipped)
    return out[:max_chars]


def _topic_engagement(diary_db_path: str, examples) -> int:
    """OBJEM zájmu o koníček = počet zmínek jeho konkrétních instancí (examples)
    napříč čtenými/dialogovými/studijními eventy. Na rozdíl od evidence_count
    (= jen délka trvání) zachytí, jak moc Hanse téma reálně zaměstnává."""
    exs = [str(e).strip() for e in (examples or []) if len(str(e).strip()) >= 4][:8]
    if not exs:
        return 0
    total = 0
    try:
        import sqlite3 as _s
        conn = _s.connect("file:%s?mode=ro" % diary_db_path, uri=True, timeout=5.0)
        try:
            for ex in exs:
                n = conn.execute(
                    "SELECT COUNT(*) FROM diary WHERE event_type IN "
                    "('web_read','reading_takeaway','study_note','teddy_dialog') "
                    "AND (coalesce(title,'')||coalesce(note,'')||coalesce(data,'')) "
                    "LIKE ?", ('%' + ex + '%',)).fetchone()
                total += int(n[0]) if n else 0
        finally:
            conn.close()
    except Exception:
        return 0
    return total


def _is_strong_topic(config: dict, diary_db_path: str, topic: str) -> bool:
    """Deep tier (skutečný výzkum) se odemkne u VELMI silného koníčku. Dvě cesty:
    (1) evidence_count >= min_evidence (délka trvání), NEBO (2) chytrý gate dle
    OBJEMU zájmu — engagement examples >= min_engagement (tak projde jen koníček,
    co Hanse opravdu hodně zaměstnává, jako Cardiff/hrady). False = jen Wikipedia."""
    rc = _cfg(config).get("research_tier", {}) or {}
    if not rc.get("enabled", True):
        return False
    try:
        import sqlite3 as _s
        conn = _s.connect("file:%s?mode=ro" % diary_db_path, uri=True, timeout=4.0)
        try:
            row = conn.execute("SELECT evidence_count, examples FROM hobbies "
                               "WHERE name_norm=?", (_norm(topic),)).fetchone()
        finally:
            conn.close()
    except Exception:
        return False
    if not row:
        return False
    if int(row[0] or 0) >= int(rc.get("min_evidence", 20)):
        return True
    try:
        examples = json.loads(row[1] or "[]")
    except Exception:
        examples = []
    eng = _topic_engagement(diary_db_path, examples)
    strong = eng >= int(rc.get("min_engagement", 500))
    if strong:
        _log.info("study: deep tier ODEMČEN pro '%s' (objem zájmu %d)", topic, eng)
    return strong


def _gather_material(config: dict, sub: str, topic: str, deep: bool = False,
                     db_path: str = None):
    """Nastuduj pod-téma do hloubky: PLNÝ hlavní článek (ne jen lead) + úvody
    několika nejrelevantnějších pododkazů z úvodní sekce (v pořadí výskytu).
    deep=True (HANS_STUDY_RESEARCH_TIER_V1) → navíc abstrakty skutečného výzkumu
    z OpenAlexu (odemčeno u velmi silného koníčku).
    Vrací (material_text, source_url, main_title) nebo (None, None, None)."""
    c = _cfg(config)
    lang = str(c.get("wiki_lang", "cs"))
    art_max = int(c.get("article_max_chars", 12000))
    sub_n = int(c.get("sublink_count", 3))
    sub_max = int(c.get("sublink_max_chars", 2500))
    try:
        from scripts.web_reader import WebReader
    except ImportError:
        _log.warning("_gather_material: WebReader nedostupný")
        return None, None, None
    w = WebReader(config)
    art = None
    try:
        for q in _search_queries(sub, topic):
            art = w.wikipedia_article(q, lang=lang, max_chars=art_max)
            if art and (art.get("text") or "").strip():
                if q != sub:
                    _log.info("_gather_material: '%s' → článek přes dotaz '%s'", sub, q)
                break
    except Exception as e:
        _log.warning("_gather_material čtení selhalo (%s): %s", sub, e)
        return None, None, None
    if not art or not (art.get("text") or "").strip():
        return None, None, None

    used_lang = art.get("lang", lang)
    parts = [f"[Hlavní článek: {art['page_title']}]\n{art['text']}"]
    if sub_n > 0:
        try:
            links = w.wikipedia_lead_links(art["page_title"], lang=used_lang,
                                           limit=sub_n + 3)
        except Exception:
            links = []
        added = 0
        seen = {_norm(art["page_title"])}
        for lt in links:
            if added >= sub_n:
                break
            if _norm(lt) in seen:
                continue
            seen.add(_norm(lt))
            try:
                intro = w.wikipedia_intro(lt, lang=used_lang, max_chars=sub_max)
            except Exception:
                intro = ""
            if intro and len(intro) > 120:
                parts.append(f"[Související pojem: {lt}]\n{intro}")
                added += 1
        _log.info("study: materiál '%s' = článek %d zn + %d pododkazů",
                  sub, len(art["text"]), added)
    if deep:
        # HANS_STUDY_RESEARCH_TIER_V1 — přidej abstrakty skutečného výzkumu.
        # Dotaz = STRUČNÝ vyřešený název článku (ne ukecané pod-téma z kurikula —
        # OpenAlex na dlouhou frázi nic nevrátí). Zkus název článku, fallback jádro.
        try:
            rc = _cfg(config).get("research_tier", {}) or {}
            for rq in (art["page_title"], _search_queries(sub, topic)[0]):
                research = _openalex_research(
                    config, rq, n=int(rc.get("results", 3)),
                    max_chars=int(rc.get("max_chars", 4000)), db_path=db_path)
                if research:
                    parts.append(research)
                    break
        except Exception as e:
            _log.debug("research tier selhal: %s", e)
    return "\n\n".join(parts), art.get("url", ""), art["page_title"]


# ── Studijní poznámka (LLM zpracuje čtení na poznámku v 1. osobě) ───────────
_NOTE_SYSTEM = (
    "Jsi {persona_name} a studuješ jedno pod-téma do hloubky. Dostaneš STUDIJNÍ "
    "MATERIÁL — hlavní encyklopedický článek a úvody několika souvisejících "
    "pojmů. Napiš si souvislou STUDIJNÍ POZNÁMKU v první osobě (6-9 vět): co "
    "podstatného ses dozvěděl, jak věci souvisejí a co tě zaujalo či překvapilo. "
    "Zůstaň SOUSTŘEDĚN na zadané pod-téma — související pojmy ber jen jako "
    "kontext, ne jako hlavní námět. Je-li v materiálu i skutečný výzkum (bloky "
    "„[Výzkum: …]“), oceň ho a zmiň, co konkrétního z něj plyne nad rámec "
    "encyklopedie. Drž se FAKTŮ z materiálu — nic si nepřimýšlej, nehádej, "
    "nedoplňuj z vlastní paměti. Piš česky, souvisle, bez nadpisů a odrážek."
)


def _generate_note(config: dict, topic: str, sub: str, material: str) -> str:
    """Base LLM napíše studijní poznámku z materiálu. '' při selhání.
    num_ctx zvednut (default Ollama je 2048 → tichý ořez) ať se vejde plný
    článek + pododkazy; model (Gemma3) zvládne 128k, omezuje VRAM/num_ctx."""
    c = _cfg(config)
    timeout = int(c.get("llm_timeout", 300))
    num_ctx = int(c.get("num_ctx", 8192))
    mat_max = int(c.get("material_max_chars", 22000))
    prompt = (f"Koníček: {topic}\nPod-téma: {sub}\n\n"
              f"Studijní materiál:\n{(material or '')[:mat_max]}\n\n"
              f"Napiš si studijní poznámku k pod-tématu „{sub}“.")
    try:
        from scripts.ollama_client import ollama_generate
        from scripts.hans_persona import persona_name as _pn
    except ImportError:
        return ""
    try:
        system = _NOTE_SYSTEM.format(persona_name=_pn(config))
        raw = ollama_generate(model=_model(config), prompt=prompt, system=system,
                              config=config, timeout=timeout,
                              keep_alive=0,
                              options={"temperature": 0.4, "num_ctx": num_ctx,
                                       "num_predict": 600})
        return (raw or "").strip()
    except Exception as e:
        _log.warning("_generate_note LLM selhal: %s", e)
        return ""


# ── Mistrovská reflexe po dokončení kurikula ────────────────────────────────
_MASTERY_SYSTEM = (
    "Jsi {persona_name}. Právě jsi dokončil dlouhý studijní program o jednom "
    "koníčku — prošel jsi celé kurikulum pod-témat. Dostaneš seznam pod-témat a "
    "své studijní poznámky. Napiš REFLEKTIVNÍ OHLÉDNUTÍ v první osobě (6-9 vět): "
    "co teď o tématu jako celku chápeš, jak na sebe jednotlivé části navazují a "
    "co to znamená pro tebe — jako pro někoho, kdo se o tohle téma vážně zajímá. "
    "Vyjdi POUZE ze svých poznámek, nic si nepřimýšlej. Piš česky, souvisle."
)


def _generate_mastery(config: dict, topic: str, subs: list, notes: list) -> str:
    c = _cfg(config)
    timeout = int(c.get("llm_timeout", 300))
    num_ctx = int(c.get("num_ctx", 8192))
    notes_block = "\n\n".join(
        f"• {s}:\n{n}" for s, n in zip(subs, notes) if n)[:12000]
    prompt = (f"Koníček: {topic}\n\nProstudovaná pod-témata a poznámky:\n"
              f"{notes_block}\n\nNapiš mistrovské ohlédnutí za celým studiem.")
    try:
        from scripts.ollama_client import ollama_generate
        from scripts.hans_persona import persona_name as _pn
    except ImportError:
        return ""
    try:
        system = _MASTERY_SYSTEM.format(persona_name=_pn(config))
        raw = ollama_generate(model=_model(config), prompt=prompt, system=system,
                              config=config, timeout=timeout,
                              keep_alive=0,
                              options={"temperature": 0.4, "num_ctx": num_ctx,
                                       "num_predict": 700})
        return (raw or "").strip()
    except Exception as e:
        _log.warning("_generate_mastery LLM selhal: %s", e)
        return ""


class StudyStore:
    def __init__(self, config: dict, diary_db_path: str):
        self.config = config
        self._diary_path = diary_db_path
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self._diary_path) as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS study_program (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    topic           TEXT NOT NULL,
                    topic_norm      TEXT NOT NULL,
                    curriculum      TEXT NOT NULL,
                    current_index   INTEGER NOT NULL DEFAULT 0,
                    status          TEXT NOT NULL DEFAULT 'active',
                    sessions_done   INTEGER NOT NULL DEFAULT 0,
                    started_ts      REAL NOT NULL,
                    updated_ts      REAL NOT NULL,
                    last_session_ts REAL NOT NULL DEFAULT 0
                )
            """)
            db.execute("CREATE INDEX IF NOT EXISTS idx_study_status "
                       "ON study_program(status)")
            # HANS_STUDY_SKIP_V1 — počítadlo selhání čtení na aktuálním
            # pod-tématu (idempotentní ALTER; po N nocích pod-téma přeskoč,
            # ať nezasekne celý program).
            try:
                db.execute("ALTER TABLE study_program ADD COLUMN "
                           "fail_count INTEGER NOT NULL DEFAULT 0")
            except sqlite3.OperationalError:
                pass  # sloupec už existuje
            db.commit()

    def _connect(self):
        conn = sqlite3.connect(self._diary_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _update_fields(self, pid: int, **fields):
        """Bezpečný UPDATE vybraných sloupců programu (jen whitelist)."""
        allowed = {"current_index", "fail_count", "status", "sessions_done"}
        sets = {k: v for k, v in fields.items() if k in allowed}
        if not sets:
            return
        cols = ", ".join(f"{k}=?" for k in sets)
        vals = list(sets.values()) + [time.time(), pid]
        try:
            conn = self._connect()
            try:
                conn.execute(
                    f"UPDATE study_program SET {cols}, updated_ts=? WHERE id=?",
                    vals)
                conn.commit()
            finally:
                conn.close()
        except Exception as e:
            _log.warning("_update_fields failed: %s", e)

    @staticmethod
    def _row_to_dict(row) -> dict:
        d = dict(row)
        try:
            d["curriculum"] = json.loads(d.get("curriculum") or "[]")
        except Exception:
            d["curriculum"] = []
        return d

    def get_active_program(self) -> Optional[dict]:
        try:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT * FROM study_program WHERE status='active' "
                    "ORDER BY id DESC LIMIT 1").fetchone()
                return self._row_to_dict(row) if row else None
            finally:
                conn.close()
        except Exception as e:
            _log.warning("get_active_program failed: %s", e)
            return None

    def _studied_topic_norms(self) -> set:
        """Témata, která už mají program (active/completed) — neopakuj hned."""
        try:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT topic_norm FROM study_program "
                    "WHERE status IN ('active','completed')").fetchall()
                return {r["topic_norm"] for r in rows}
            finally:
                conn.close()
        except Exception:
            return set()

    def all_programs(self, limit: int = 20) -> List[dict]:
        try:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT * FROM study_program ORDER BY id DESC LIMIT ?",
                    (limit,)).fetchall()
                return [self._row_to_dict(r) for r in rows]
            finally:
                conn.close()
        except Exception:
            return []

    # ── ensure_program ─────────────────────────────────────────────────────
    def ensure_program(self, config: dict) -> Optional[dict]:
        """Když neběží žádný program, vybere durable koníček + LLM kurikulum a
        založí nový. Vrací aktivní program (dict) nebo None (nic k založení /
        LLM dole)."""
        active = self.get_active_program()
        if active:
            return active

        c = _cfg(config)
        min_ev = int(c.get("min_evidence", 8))
        min_age = int(c.get("min_age_days", 21))
        min_rec = int(c.get("min_recent_days", 14))
        try:
            from scripts.hans_hobbies import HobbyStore
        except ImportError:
            _log.warning("ensure_program: HobbyStore nedostupný")
            return None
        hobbies = HobbyStore(config, self._diary_path).durable_hobbies(
            min_evidence=min_ev, min_age_days=min_age, min_recent_days=min_rec)
        if not hobbies:
            _log.info("study.ensure_program: žádný durable koníček "
                      "(gate ev>=%d, age>=%dd, recent<=%dd)",
                      min_ev, min_age, min_rec)
            return None

        done = self._studied_topic_norms()
        candidates = [h for h in hobbies if _norm(h.name) not in done]
        if not candidates:
            _log.info("study.ensure_program: všechny durable koníčky už "
                      "mají program (%d)", len(hobbies))
            return None
        # HANS_STUDY_ENGAGEMENT_SELECT_V1 — vyber nejdřív koníček s NEJVĚTŠÍM
        # objemem zájmu (ne arbitrárně mezi remízami na evidence_count). Hans
        # tak studuje napřed to, co ho reálně nejvíc zaměstnává (Cardiff/Design).
        if c.get("select_by_engagement", True):
            candidates.sort(
                key=lambda h: _topic_engagement(self._diary_path, h.examples),
                reverse=True)
        chosen = candidates[0]

        curriculum = _generate_curriculum(config, chosen.name, chosen.examples)
        if len(curriculum) < 3:
            _log.info("study.ensure_program: kurikulum se nevygenerovalo "
                      "(LLM dole?) pro '%s'", chosen.name)
            return None

        now = time.time()
        try:
            conn = self._connect()
            try:
                conn.execute(
                    "INSERT INTO study_program (topic, topic_norm, curriculum, "
                    "current_index, status, sessions_done, started_ts, "
                    "updated_ts, last_session_ts) "
                    "VALUES (?,?,?,0,'active',0,?,?,0)",
                    (chosen.name, _norm(chosen.name),
                     json.dumps(curriculum, ensure_ascii=False), now, now))
                conn.commit()
                pid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            finally:
                conn.close()
        except Exception as e:
            _log.warning("ensure_program INSERT failed: %s", e)
            return None
        _log.info("study: NOVÝ program [%d] '%s' — %d pod-témat",
                  pid, chosen.name, len(curriculum))
        return self.get_active_program()

    # ── study_next ─────────────────────────────────────────────────────────
    def study_next(self, config: dict, knowledge=None,
                   diary_writer=None) -> Optional[dict]:
        """Nastuduj DALŠÍ pod-téma aktivního programu. Vrací dict s výsledkem
        ('studied'/'completed') nebo None (transientní selhání — retry).
        """
        prog = self.ensure_program(config)
        if not prog:
            return None
        curriculum = prog["curriculum"]
        idx = int(prog["current_index"])
        if idx >= len(curriculum):
            # nemělo by nastat (advance to completed), ale ošetři
            self._complete_program(config, prog, knowledge, diary_writer)
            return {"result": "completed", "topic": prog["topic"]}

        sub = str(curriculum[idx]).strip()
        topic = prog["topic"]
        max_fail = int(_cfg(config).get("max_subtopic_failures", 3))

        # 1) hloubkové čtení (plný hlavní článek + intro pododkazů; u velmi
        #    silného koníčku navíc abstrakty výzkumu z OpenAlexu — deep tier)
        deep = _is_strong_topic(config, self._diary_path, topic)
        material, source_url, _main = _gather_material(
            config, sub, topic, deep=deep, db_path=self._diary_path)
        if not material:
            # HANS_STUDY_SKIP_V1 — pro toto pod-téma se nenašlo čtení (nejspíš
            # špatná formulace v kurikulu). Počítej selhání; po max_fail NOCÍCH
            # pod-téma přeskoč, ať nezasekne celý program. Selhání čtení NEní
            # totéž co výpadek LLM (ten = return None → deferred, NEpočítá se).
            new_fail = int(prog.get("fail_count", 0)) + 1
            if new_fail >= max_fail:
                skip_idx = idx + 1
                self._update_fields(prog["id"], current_index=skip_idx,
                                    fail_count=0)
                _log.info("study: pod-téma '%s' PŘESKOČENO po %d pokusech bez "
                          "čtení (program [%d])", sub, new_fail, prog["id"])
                if skip_idx >= len(curriculum):
                    prog["current_index"] = skip_idx
                    self._complete_program(config, prog, knowledge, diary_writer)
                    return {"result": "completed", "topic": topic,
                            "skipped": sub}
                return {"result": "skipped", "topic": topic, "sub": sub}
            self._update_fields(prog["id"], fail_count=new_fail)
            _log.info("study_next: pro '%s' nenalezeno čtení "
                      "(pokus %d/%d) — zkusím jindy", sub, new_fail, max_fail)
            return {"result": "noread", "topic": topic, "sub": sub}

        # 2) poznámka (LLM) — selhání = výpadek LLM → deferred (NEpřeskakuj!)
        note = _generate_note(config, topic, sub, material)
        if not note:
            _log.info("study_next: poznámka se nevygenerovala (LLM dole?) — retry")
            return None

        # 3) deník study_note
        title = f"Studium: {topic} — {sub}"
        self._write_diary("study_note", title, note, diary_writer)

        # 4) RAG upload (čtenářská kolekce)
        if knowledge is not None and getattr(knowledge, "enabled", False):
            try:
                coll = str(_cfg(config).get("rag_collection", "hans_cetba"))
                knowledge.upload(
                    collection_key=coll,
                    doc_id=f"study_{prog['id']}_{idx}",
                    title=title,
                    text=note,
                    metadata={"koníček": topic, "pod-téma": sub,
                              "zdroj": source_url or "wikipedia",
                              "typ": "study_note"})
            except Exception as e:
                _log.debug("study_next RAG upload: %s", e)

        # 5) posun
        new_idx = idx + 1
        now = time.time()
        try:
            conn = self._connect()
            try:
                conn.execute(
                    "UPDATE study_program SET current_index=?, "
                    "sessions_done=sessions_done+1, fail_count=0, updated_ts=?, "
                    "last_session_ts=? WHERE id=?",
                    (new_idx, now, now, prog["id"]))
                conn.commit()
            finally:
                conn.close()
        except Exception as e:
            _log.warning("study_next UPDATE failed: %s", e)
            return None
        _log.info("study: session [%d] '%s' — pod-téma %d/%d: %s",
                  prog["id"], topic, new_idx, len(curriculum), sub)

        # 6) dokončení?
        if new_idx >= len(curriculum):
            prog["current_index"] = new_idx
            self._complete_program(config, prog, knowledge, diary_writer)
            return {"result": "completed", "topic": topic, "sub": sub}
        return {"result": "studied", "topic": topic, "sub": sub,
                "index": new_idx, "total": len(curriculum)}

    def _write_diary(self, event_type: str, title: str, text: str,
                     diary_writer=None):
        """Zápis do deníku (text jde do sloupce `data` jako u book_reflection)."""
        if diary_writer is not None:
            try:
                diary_writer(event_type, title, note=text)
                return
            except Exception as e:
                _log.debug("study diary_writer selhal, fallback SQL: %s", e)
        try:
            conn = sqlite3.connect(self._diary_path)
            conn.execute(
                "INSERT INTO diary (ts, event_type, title, data) VALUES (?,?,?,?)",
                (time.time(), event_type, title, text))
            conn.commit()
            conn.close()
        except Exception as e:
            _log.warning("study diary write selhal: %s", e)

    # ── dokončení + syntéza ────────────────────────────────────────────────
    def _complete_program(self, config: dict, prog: dict, knowledge=None,
                          diary_writer=None):
        try:
            conn = self._connect()
            try:
                conn.execute(
                    "UPDATE study_program SET status='completed', updated_ts=? "
                    "WHERE id=?", (time.time(), prog["id"]))
                conn.commit()
            finally:
                conn.close()
        except Exception as e:
            _log.warning("_complete_program UPDATE failed: %s", e)
        _log.info("study: program [%d] '%s' DOKONČEN — mistrovská reflexe",
                  prog["id"], prog["topic"])
        try:
            self.synthesize_progress(config, prog, knowledge, diary_writer)
        except Exception as e:
            _log.warning("synthesize_progress selhal: %s", e)

    def synthesize_progress(self, config: dict, prog: Optional[dict] = None,
                            knowledge=None, diary_writer=None) -> Optional[str]:
        """Mistrovská reflexe po dokončení kurikula. Grounduje vocational
        identitu reálnou znalostí. Reflexe → deník study_mastery + RAG identita."""
        if prog is None:
            prog = self.get_active_program()
        if not prog:
            return None
        topic = prog["topic"]
        subs = list(prog["curriculum"])
        notes = self._gather_notes(topic, prog.get("started_ts", 0))
        if not notes:
            _log.info("synthesize_progress: žádné poznámky k '%s'", topic)
            return None
        # zarovnej notes na subs (poznámky jsou v pořadí studia)
        mastery = _generate_mastery(config, topic, subs, notes)
        if not mastery:
            return None
        title = f"Mistrovská reflexe: {topic}"
        self._write_diary("study_mastery", title, mastery, diary_writer)
        if knowledge is not None and getattr(knowledge, "enabled", False):
            try:
                knowledge.upload(
                    collection_key="hans_identita",
                    doc_id=f"study_mastery_{prog['id']}",
                    title=title,
                    text=mastery,
                    metadata={"koníček": topic, "typ": "study_mastery"})
            except Exception as e:
                _log.debug("synthesize_progress RAG upload: %s", e)
        _log.info("study: mistrovská reflexe '%s' (%d znaků)",
                  topic, len(mastery))
        return mastery

    def _gather_notes(self, topic: str, since_ts: float) -> List[str]:
        """Posbírá studijní poznámky daného programu (deník study_note)."""
        try:
            conn = sqlite3.connect("file:%s?mode=ro" % self._diary_path,
                                   uri=True, timeout=5.0)
            prefix = f"Studium: {topic} —%"
            rows = conn.execute(
                "SELECT data FROM diary WHERE event_type='study_note' "
                "AND title LIKE ? AND ts >= ? ORDER BY ts ASC",
                (prefix, float(since_ts or 0))).fetchall()
            conn.close()
            return [r[0] for r in rows if r and r[0]]
        except Exception as e:
            _log.warning("_gather_notes failed: %s", e)
            return []


# ── Surfacing / introspekce (HANS_STUDY_SURFACING_V1, #2; Severka #3) ────────
def _latest_diary_text(diary_db_path: str, event_type: str,
                       title_like: str = None) -> tuple:
    """(title, data, ts) posledního deníkového eventu daného typu nebo (None…)."""
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % diary_db_path,
                               uri=True, timeout=4.0)
        if title_like:
            row = conn.execute(
                "SELECT title, data, ts FROM diary WHERE event_type=? "
                "AND title LIKE ? ORDER BY ts DESC LIMIT 1",
                (event_type, title_like)).fetchone()
        else:
            row = conn.execute(
                "SELECT title, data, ts FROM diary WHERE event_type=? "
                "ORDER BY ts DESC LIMIT 1", (event_type,)).fetchone()
        conn.close()
        return (row[0], row[1], row[2]) if row else (None, None, None)
    except Exception as e:
        _log.debug("_latest_diary_text failed: %s", e)
        return (None, None, None)


def study_context_string(config: dict, diary_db_path: str,
                         max_chars: int = 360) -> str:
    """Krátký kontext o Hansově studiu pro chat prompt (#2 proaktivní zmínka).
    Read-only. Aktivní program → téma + poslední poznámka; jinak nedávno
    dokončené studium → mistrovská reflexe. '' když nic."""
    try:
        store = StudyStore(config, diary_db_path)
        ap = store.get_active_program()
    except Exception:
        return ""
    if ap:
        topic = ap["topic"]
        _t, data, _ts = _latest_diary_text(
            diary_db_path, "study_note", f"Studium: {topic} —%")
        out = (f"Posledních pár dní studuji do hloubky téma \u201e{topic}\u201c "
               f"(pod-téma {min(ap['current_index'] + 1, len(ap['curriculum']))}"
               f"/{len(ap['curriculum'])}).")
        if data:
            out += " Naposledy mě zaujalo: " + data.strip().replace("\n", " ")
        return out[:max_chars]
    # žádný aktivní → nedávno dokončené?
    title, data, ts = _latest_diary_text(diary_db_path, "study_mastery")
    if title and data and ts and (time.time() - ts) < 14 * 86400:
        topic = title.replace("Mistrovská reflexe:", "").strip()
        return (f"Nedávno jsem dostudoval téma \u201e{topic}\u201c. "
                + data.strip().replace("\n", " "))[:max_chars]
    return ""


def study_dialog_seed(config: dict, diary_db_path: str,
                      max_chars: int = 260) -> str:
    """Seed pro dialog s Kolačem (HANS_STUDY_KOLAC_V1). Marker 'Studuji do
    hloubky:' rozpozná klasifikátor témat v hans_dialog. '' když nestuduje."""
    try:
        ap = StudyStore(config, diary_db_path).get_active_program()
    except Exception:
        return ""
    if not ap:
        return ""
    topic = ap["topic"]
    _t, data, _ts = _latest_diary_text(
        diary_db_path, "study_note", f"Studium: {topic} —%")
    seed = f"Studuji do hloubky: {topic}."
    if data:
        seed += " " + data.strip().replace("\n", " ")
    return seed[:max_chars]


def completed_studies_block(config: dict, diary_db_path: str,
                            limit: int = 4, max_chars: int = 900) -> str:
    """Blok pro Severku (#3): dokončené studijní programy + aktivní směr.
    Grounduje vocational návrh identity REÁLNOU znalostí. '' když nic."""
    try:
        store = StudyStore(config, diary_db_path)
        progs = store.all_programs(limit=20)
    except Exception:
        return ""
    if not progs:
        return ""
    lines = []
    done = [p for p in progs if p["status"] == "completed"]
    for p in done[:limit]:
        _t, data, _ts = _latest_diary_text(
            diary_db_path, "study_mastery", f"%{p['topic']}%")
        gist = (data or "").strip().replace("\n", " ")
        lines.append(f"- Dostudoval jsem do hloubky \u201e{p['topic']}\u201c. "
                     + (gist[:200] if gist else ""))
    active = next((p for p in progs if p["status"] == "active"), None)
    if active:
        lines.append(f"- Právě studuji do hloubky \u201e{active['topic']}\u201c "
                     f"({active['current_index']}/{len(active['curriculum'])}).")
    if not lines:
        return ""
    return "\n".join(lines)[:max_chars]


# ── Top-level noční vstup (volá hans_routine) ───────────────────────────────
def run_study_session(config: dict, diary_db_path: str, knowledge=None,
                      diary_writer=None) -> str:
    """Jedna noční studijní session. Vrací kód výsledku:
       'studied'   — nastudováno pod-téma
       'completed' — kurikulum dokončeno (mistrovská reflexe)
       'idle'      — nic ke studiu (žádný durable koníček / vše prostudováno)
       'deferred'  — transientní selhání (Ollama/wiki dole) → routine zkusí znovu
    Routine si podle kódu řídí denní guard (deferred = retry, jinak set date)."""
    if not _cfg(config).get("enabled", True):
        return "idle"
    try:
        store = StudyStore(config, diary_db_path)
    except Exception as e:
        _log.warning("run_study_session init selhal: %s", e)
        return "deferred"
    prog = store.ensure_program(config)
    if not prog:
        # rozliš: durable koníček ale LLM dole (deferred) vs opravdu nic (idle).
        # ensure_program loguje důvod; konzervativně 'idle' jen když není žádný
        # durable koníček, jinak 'deferred'. Levné rozlišení:
        c = _cfg(config)
        try:
            from scripts.hans_hobbies import HobbyStore
            hobs = HobbyStore(config, diary_db_path).durable_hobbies(
                min_evidence=int(c.get("min_evidence", 8)),
                min_age_days=int(c.get("min_age_days", 21)),
                min_recent_days=int(c.get("min_recent_days", 14)))
        except Exception:
            hobs = []
        # je-li durable koníček a přesto není program → kurikulum selhalo → retry
        unstudied = [h for h in hobs
                     if _norm(h.name) not in store._studied_topic_norms()]
        return "deferred" if unstudied else "idle"
    res = store.study_next(config, knowledge=knowledge, diary_writer=diary_writer)
    if res is None:
        return "deferred"
    return res.get("result", "studied")


# ── Smoke (python3 -m scripts.hans_study) ───────────────────────────────────
if __name__ == "__main__":
    import sys
    cfg = {}
    try:
        with open("config.json", encoding="utf-8") as fh:
            cfg = json.load(fh)
    except Exception as exc:  # noqa
        print("WARN: config.json nenačten (%s)" % exc)
    db = cfg.get("diary_db", "data/hans_diary.db")
    store = StudyStore(cfg, db)
    if len(sys.argv) > 1 and sys.argv[1] == "programs":
        for p in store.all_programs():
            print(f"[{p['id']}] {p['topic']} — {p['status']} "
                  f"{p['current_index']}/{len(p['curriculum'])} "
                  f"({p['sessions_done']} sessions)")
            for i, s in enumerate(p["curriculum"]):
                mark = "✓" if i < p["current_index"] else " "
                print(f"   {mark} {s}")
    else:
        print("=== StudyStore: aktivní program ===")
        ap = store.get_active_program()
        if ap:
            print(f"[{ap['id']}] {ap['topic']} — {ap['current_index']}/"
                  f"{len(ap['curriculum'])}")
            for i, s in enumerate(ap["curriculum"]):
                print(f"   {'✓' if i < ap['current_index'] else ' '} {s}")
        else:
            print("(žádný aktivní program)")
        print("\nPoužij `programs` pro výpis všech programů.")
