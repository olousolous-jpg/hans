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


def _sig_tokens(s: str) -> set:
    """Významná slova tématu (>2 znaky) pro měkký dedup blízkých témat."""
    return {w for w in _norm(s).split() if len(w) > 2}


def _already_covered(topic: str, studied_norms) -> Optional[str]:
    """HANS_STUDY_NEAR_DUP_V1 — je téma už POKRYTÉ existujícím programem? Vrátí
    norm shodného programu, nebo None. Kryje přesnou shodu I blízká synonyma:
    když se významná slova jednoho tématu plně kryjí s druhým (jedno je
    podmnožina druhého), je to překryv („design" ⊂ „web design", „grafický
    design" ⊃ „design") → nezakládej duplicitní program, jen by se přestudovalo
    totéž. Konzervativní: musí jít o ÚPLNé krytí významných slov, ne jen průnik."""
    tn = _norm(topic)
    nt = _sig_tokens(topic)
    for sn in studied_norms:
        if sn == tn:
            return sn
        st = _sig_tokens(sn)
        if nt and st and (nt <= st or st <= nt):
            return sn
    return None


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


# ── HANS_STUDY_SOURCES_V2 — Wikisource (primární texty) + Internet Archive ──
_UA = {"User-Agent": "HansStudyBot/1.0 (home assistant; contact via GitHub)"}


def _en_title(cs_title: str, lang: str = "cs") -> str:
    """ANGLICKÝ název tématu přes mezijazyčný odkaz Wikipedie (deterministicky,
    'Cardiffský hrad'→'Cardiff Castle'). EN zdroje (IA, en.wikisource, OpenAlex)
    na český/skloňovaný název nic nenajdou. '' když link není/chyba."""
    if lang == "en" or not cs_title:
        return cs_title or ""
    import requests as _rq
    try:
        r = _rq.get(f"https://{lang}.wikipedia.org/w/api.php", params={
            "action": "query", "prop": "langlinks", "titles": cs_title,
            "lllang": "en", "format": "json", "formatversion": 2},
            headers=_UA, timeout=12)
        r.raise_for_status()
        pages = (r.json().get("query", {}) or {}).get("pages", []) or []
        for p in pages:
            ll = p.get("langlinks") or []
            if ll:
                return ll[0].get("title") or ""
    except Exception as e:
        _log.debug("_en_title(%s): %s", cs_title, e)
    return ""


def _wikisource_read(config: dict, query: str, langs=("cs", "en"),
                     max_chars: int = 3000, db_path: str = None) -> str:
    """Primární text z Wikisource (MediaWiki API): search → action=parse →
    plain text výňatek. Preferuje češtinu. DEDUP přes study_seen_works
    (work_id 'ws_<lang>_<title>'). '' při chybě/nic. Best-effort.
    Pozn.: prop=extracts na Wikisource NEfunguje (vrací prázdno) → parse HTML."""
    import re as _re
    import html as _html
    import requests as _rq
    seen = _seen_work_ids(db_path)
    for lang in langs:
        api = f"https://{lang}.wikisource.org/w/api.php"
        try:
            r = _rq.get(api, params={
                "action": "query", "list": "search", "srsearch": query,
                "srlimit": 4, "format": "json"}, headers=_UA, timeout=15)
            r.raise_for_status()
            hits = (r.json().get("query", {}) or {}).get("search", []) or []
        except Exception as e:
            _log.debug("_wikisource_read search (%s): %s", lang, e)
            continue
        qwords = {w for w in _norm(query).split() if len(w) > 3}
        for h in hits:
            title = h.get("title") or ""
            wid = f"ws_{lang}_{_norm(title)}"
            if not title or wid in seen:
                continue
            # relevance: aspoň jedno slovo dotazu v názvu (jinak search vrací
            # svazky slovníků/rozcestníky, kde je dotaz jen zmíněn v obsahu)
            tnorm = _norm(title)
            if qwords and not any(w in tnorm for w in qwords):
                continue
            try:
                r = _rq.get(api, params={
                    "action": "parse", "page": title, "prop": "text",
                    "format": "json", "formatversion": 2},
                    headers=_UA, timeout=20)
                r.raise_for_status()
                raw_html = (r.json().get("parse", {}) or {}).get("text", "")
            except Exception:
                continue
            # style/script bloky PŘED strip tagů (jinak CSS unikne do textu)
            txt = _re.sub(r"<(style|script)[^>]*>.*?</\1>", " ",
                          raw_html or "", flags=_re.DOTALL | _re.IGNORECASE)
            txt = _re.sub(r"<[^>]+>", " ", txt)
            txt = _html.unescape(_re.sub(r"\s+", " ", txt)).strip()
            if len(txt) < 400:      # pahýl/rozcestník
                continue
            _record_works(db_path, [(wid, title)])
            _log.info("study: Wikisource(%s) '%s' → %d zn", lang, title,
                      min(len(txt), max_chars))
            return (f"[Primární text (Wikisource): {title}]\n"
                    + txt[:max_chars])
    return ""


def _ia_research(config: dict, query: str, max_chars: int = 2500,
                 db_path: str = None) -> str:
    """Plný text KNIHY z Internet Archive: advancedsearch (texts, preferuje
    starší/public-domain) → OCR djvu.txt → výňatek. Lending knihy vrací 401 →
    přeskočí. Google-scan boilerplate na začátku se odřízne. DEDUP work_id
    'ia_<identifier>'. '' při chybě/nic. Best-effort (texty EN — base model
    čte EN dobře, poznámka vzniká česky)."""
    import requests as _rq
    seen = _seen_work_ids(db_path)
    try:
        r = _rq.get("https://archive.org/advancedsearch.php", params={
            "q": f"({query}) AND mediatype:texts AND year:[1500 TO 1929]",
            "fl[]": ["identifier", "title", "year"],
            "rows": 6, "output": "json", "sort[]": "downloads desc"},
            headers=_UA, timeout=20)
        r.raise_for_status()
        docs = (r.json().get("response", {}) or {}).get("docs", []) or []
    except Exception as e:
        _log.debug("_ia_research search: %s", e)
        return ""
    for d in docs:
        ident = d.get("identifier") or ""
        wid = f"ia_{ident}"
        if not ident or wid in seen:
            continue
        try:
            r = _rq.get(f"https://archive.org/download/{ident}/{ident}_djvu.txt",
                        headers=_UA, timeout=30, allow_redirects=True)
            if r.status_code != 200:
                continue          # 401 = lending-restricted
            txt = r.text
        except Exception:
            continue
        if len(txt) < 3000:       # foto/pahýl, ne kniha
            continue
        # odřízni Google-scan boilerplate na začátku: hlavička je „google"-hustá,
        # tělo knihy už google nezmiňuje → řízni za POSLEDNÍM výskytem slova
        # google v prvních ~13k znacích (+ dojeď na konec věty)
        head = txt[:13000].lower()
        if "google" in head:
            m = head.rfind("google")
            cut = m + 6
            dot = txt.find(".", cut)
            txt = txt[(dot + 1) if (dot != -1 and dot < cut + 400) else cut:]
        txt = re.sub(r"\s+", " ", txt).strip()
        if len(txt) < 1500:
            continue
        _record_works(db_path, [(wid, str(d.get("title") or ident))])
        year = d.get("year") or "?"
        _log.info("study: InternetArchive '%s' (%s) → %d zn",
                  str(d.get("title"))[:60], year, min(len(txt), max_chars))
        return (f"[Kniha (Internet Archive): {d.get('title')} ({year})]\n"
                + txt[:max_chars])
    return ""


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
        # HANS_STUDY_SOURCES_V2 — EN název přes mezijazyčný link (EN zdroje na
        # český/skloňovaný název nic nenajdou; zlepší i trefnost OpenAlexu).
        en = _en_title(art["page_title"], lang=used_lang)
        try:
            rc = _cfg(config).get("research_tier", {}) or {}
            _oa_queries = ([en] if en and en != art["page_title"] else []) + \
                [art["page_title"], _search_queries(sub, topic)[0]]
            for rq in _oa_queries:
                research = _openalex_research(
                    config, rq, n=int(rc.get("results", 3)),
                    max_chars=int(rc.get("max_chars", 4000)), db_path=db_path)
                if research:
                    parts.append(research)
                    break
        except Exception as e:
            _log.debug("research tier selhal: %s", e)
        # primární texty (Wikisource cs→en) + knihy (Internet Archive, EN)
        try:
            rc = _cfg(config).get("research_tier", {}) or {}
            if rc.get("wikisource_enabled", True):
                ws = _wikisource_read(
                    config, art["page_title"],
                    max_chars=int(rc.get("wikisource_max_chars", 3000)),
                    db_path=db_path)
                if not ws and en and en != art["page_title"]:
                    ws = _wikisource_read(
                        config, en, langs=("en",),
                        max_chars=int(rc.get("wikisource_max_chars", 3000)),
                        db_path=db_path)
                if ws:
                    parts.append(ws)
            if rc.get("archive_enabled", True):
                ia = _ia_research(
                    config, (en or art["page_title"]),
                    max_chars=int(rc.get("archive_max_chars", 2500)),
                    db_path=db_path)
                if ia:
                    parts.append(ia)
        except Exception as e:
            _log.debug("sources V2 selhaly: %s", e)
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


def add_pending_topic(diary_db_path: str, topic: str) -> str:
    """HANS_AGENT_V1 — zařadí téma z chatu do studijní fronty (status='pending').
    Aktivuje se v ensure_program s PŘEDNOSTÍ před durable koníčky, jakmile
    dokončí současný program. Idempotentní (topic_norm ve všech stavech).
    Vrací 'added' | 'exists' | 'error'."""
    t = (topic or "").strip()
    if len(t) < 2:
        return "error"
    tn = _norm(t)
    try:
        db = sqlite3.connect(diary_db_path, timeout=5.0)
        try:
            db.execute("""CREATE TABLE IF NOT EXISTS study_program (
                id INTEGER PRIMARY KEY AUTOINCREMENT, topic TEXT NOT NULL,
                topic_norm TEXT NOT NULL, curriculum TEXT NOT NULL,
                current_index INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'active',
                sessions_done INTEGER NOT NULL DEFAULT 0,
                started_ts REAL NOT NULL, updated_ts REAL NOT NULL,
                last_session_ts REAL NOT NULL DEFAULT 0)""")
            # HANS_STUDY_NEAR_DUP_V1 — přesná shoda I blízké synonymum (přesah
            # slov) → nezakládej duplicitní téma z chatu, jen by se přestudovalo.
            norms = [r[0] for r in db.execute(
                "SELECT topic_norm FROM study_program").fetchall()]
            if _already_covered(t, norms):
                return "exists"
            now = time.time()
            db.execute(
                "INSERT INTO study_program (topic, topic_norm, curriculum, "
                "current_index, status, sessions_done, started_ts, updated_ts, "
                "last_session_ts) VALUES (?,?,?,0,'pending',0,?,?,0)",
                (t, tn, "[]", now, now))
            db.commit()
            return "added"
        finally:
            db.close()
    except Exception as e:
        _log.warning("add_pending_topic: %s", e)
        return "error"


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
            # HANS_STUDY_DEEPEN_V1 — kolo prohloubení (spirála studium→dílo→kritika)
            try:
                db.execute("ALTER TABLE study_program ADD COLUMN "
                           "deepen_round INTEGER NOT NULL DEFAULT 0")
            except sqlite3.OperationalError:
                pass
            # HANS_STUDY_DEEPEN_V2 — ask-first: návrhy prohloubení čekají na schválení
            db.execute("""CREATE TABLE IF NOT EXISTS deepen_proposals (
                id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, topic TEXT,
                topic_norm TEXT, round INTEGER, critique TEXT, subtopics TEXT,
                status TEXT DEFAULT 'pending')""")
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
                # HANS_STUDY_SEQUENTIAL_V1 — DOKONČI JEDNO PŘED DALŠÍM: ber
                # NEJSTARŠÍ aktivní (id ASC), ne nejnovější. Dřív id DESC →
                # nový/reaktivovaný (prohloubený) program vždy předběhl starší
                # → Design uvízl na 8/12 za novějším studiem architektury. Teď
                # se fronta aktivních dojíždí od nejstaršího = sekvenčně.
                row = conn.execute(
                    "SELECT * FROM study_program WHERE status='active' "
                    "ORDER BY id ASC LIMIT 1").fetchone()
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

    def _next_pending_topic(self) -> Optional[dict]:
        """HANS_AGENT_V1 — nejstarší pending téma z chatu (FIFO) nebo None."""
        try:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT id, topic FROM study_program WHERE status='pending' "
                    "ORDER BY id ASC LIMIT 1").fetchone()
                return {"id": row["id"], "topic": row["topic"]} if row else None
            finally:
                conn.close()
        except Exception:
            return None

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

        # HANS_AGENT_V1 — PENDING téma z chatu má PŘEDNOST před durable koníčky
        # (Hans/uživatel se k němu zavázal). Vygeneruj kurikulum a aktivuj.
        pend = self._next_pending_topic()
        if pend:
            curriculum = _generate_curriculum(config, pend["topic"], [])
            if len(curriculum) < 3:
                _log.info("study.ensure_program: kurikulum pending '%s' "
                          "se nevygenerovalo (LLM dole?) — zkusím příště",
                          pend["topic"])
                return None
            try:
                conn = self._connect()
                try:
                    conn.execute(
                        "UPDATE study_program SET curriculum=?, status='active', "
                        "updated_ts=? WHERE id=?",
                        (json.dumps(curriculum, ensure_ascii=False),
                         time.time(), pend["id"]))
                    conn.commit()
                finally:
                    conn.close()
                _log.info("study: pending téma '%s' aktivováno (%d pod-témat)",
                          pend["topic"], len(curriculum))
                return self.get_active_program()
            except Exception as e:
                _log.warning("aktivace pending tématu selhala: %s", e)
                return None

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
        # HANS_STUDY_NEAR_DUP_V1 — vynech nejen PŘESNĚ studované, ale i blízká
        # synonyma (přesah slov) → Hans nezaloží „web design", když už studoval
        # „design", a nepřestuduje totéž.
        candidates = []
        for h in hobbies:
            cov = _already_covered(h.name, done)
            if cov:
                _log.info("study.ensure_program: '%s' už pokryto programem "
                          "'%s' (blízké téma) → nezakládám duplicitní",
                          h.name, cov)
                continue
            candidates.append(h)
        if not candidates:
            _log.info("study.ensure_program: všechny durable koníčky už "
                      "mají (nebo pokrývá blízký) program (%d)", len(hobbies))
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

    # ── HANS_STUDY_DEEPEN_V2 — ask-first prohloubení (kritika → schválení) ────
    def _generate_deepening(self, config: dict, topic: str, studied: list,
                            work_gap: str, max_new: int):
        """LLM: KRÁTKÁ kritika díla (co mu chybí do hloubky) + NOVÁ hlubší
        pod-témata (konkrétní, bez opakování nastudovaného). Když je zadán
        `work_gap` (kritika od uživatele), řídí se JÍ. Vrací {critique, subtopics}
        nebo None (LLM dole)."""
        try:
            from scripts.ollama_client import ollama_generate
            model = (_cfg(config).get("model")
                     or (config.get("evening_reflection", {}) or {}).get("model")
                     or "jobautomation/OpenEuroLLM-Czech:latest")
            sysp = (
                "Jsi kurátor studia. Autor nastudoval pod-témata níže a vytvořil "
                "z nich dílo. Buď KRITICKÝ: v 1 větě řekni, co dílu chybí do "
                "hloubky, a navrhni %d NOVÝCH pod-témat, která jdou VÍC DO HLOUBKY "
                "a do konkrétna (specifické techniky, postupy, hodnoty, příklady), "
                "STAVÍ na nastudovaném, ale ŽÁDNÉ z nastudovaných NEOPAKUJÍ. Vrať "
                "POUZE JSON: {\"critique\":\"…\",\"subtopics\":[\"…\"]} (česky)."
                % max_new)
            studied_txt = "\n".join("- %s" % s for s in studied)
            gap = ("\n\nSměr kritiky (řiď se jím): %s" % work_gap) if work_gap else ""
            raw = ollama_generate(
                model, "Téma: %s\n\nUŽ NASTUDOVÁNO (NEOPAKUJ):\n%s%s\n\nJSON:"
                % (topic, studied_txt, gap),
                system=sysp, config=config, timeout=150, keep_alive=0,
                options={"temperature": 0.4, "num_ctx": 4096, "num_predict": 450})
            if not raw:
                return None
            m = re.search(r"\{.*\}", raw, re.S)
            if m:
                d = json.loads(m.group(0))
                subs = [str(x).strip() for x in (d.get("subtopics") or [])
                        if str(x).strip()][:max_new]
                if subs:
                    return {"critique": str(d.get("critique", "")).strip(),
                            "subtopics": subs}
        except Exception as e:
            _log.debug("_generate_deepening: %s", e)
        return None

    def create_deepen_proposal(self, config: dict, topic: str,
                               max_new: int = 4) -> dict:
        """Po vytvoření díla vygeneruj NÁVRH prohloubení (kritika + hlubší
        pod-témata) a ulož ho jako PENDING — NEAPLIKUJE (čeká na schválení
        uživatelem). Cap `study.max_deepen_rounds`. Idempotentní per (téma,kolo).
        Vrací {status: proposed/idle/deferred, id, critique, subtopics, round}."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT curriculum, deepen_round, topic FROM study_program "
                "WHERE topic_norm=? AND status='completed' ORDER BY id DESC "
                "LIMIT 1", (_norm(topic),)).fetchone()
        finally:
            conn.close()
        if not row:
            return {"status": "idle", "reason": "žádný dokončený program"}
        cap = int(_cfg(config).get("max_deepen_rounds", 2))
        cur_round = int(row["deepen_round"] or 0)
        if cur_round >= cap:
            return {"status": "idle", "reason": "strop prohloubení (%d)" % cap}
        # už existuje návrh pro tohle kolo? (idempotence)
        conn = self._connect()
        try:
            ex = conn.execute(
                "SELECT 1 FROM deepen_proposals WHERE topic_norm=? AND round=? "
                "LIMIT 1", (_norm(topic), cur_round)).fetchone()
        finally:
            conn.close()
        if ex:
            return {"status": "idle", "reason": "návrh pro toto kolo už existuje"}
        studied = json.loads(row["curriculum"] or "[]")
        gen = self._generate_deepening(config, row["topic"], studied, "", max_new)
        if gen is None:
            return {"status": "deferred", "reason": "LLM nedostupný"}
        subs = [s for s in gen["subtopics"]
                if _norm(s) not in {_norm(x) for x in studied}]
        if not subs:
            return {"status": "idle", "reason": "žádné nové pod-téma"}
        conn = self._connect()
        try:
            cur = conn.execute(
                "INSERT INTO deepen_proposals (ts, topic, topic_norm, round, "
                "critique, subtopics, status) VALUES (?,?,?,?,?,?,'pending')",
                (time.time(), row["topic"], _norm(topic), cur_round,
                 gen["critique"], json.dumps(subs, ensure_ascii=False)))
            conn.commit()
            pid = cur.lastrowid
        finally:
            conn.close()
        _log.info("deepen návrh [%d] '%s' kolo %d: %d témat", pid, topic,
                  cur_round, len(subs))
        return {"status": "proposed", "id": pid, "critique": gen["critique"],
                "subtopics": subs, "round": cur_round, "topic": row["topic"]}

    def get_pending_deepen(self, topic: str = None) -> list:
        conn = self._connect()
        try:
            if topic:
                rows = conn.execute(
                    "SELECT id, topic, round, critique, subtopics FROM "
                    "deepen_proposals WHERE status='pending' AND topic_norm=? "
                    "ORDER BY id DESC", (_norm(topic),)).fetchall()
            else:
                rows = conn.execute(
                    "SELECT id, topic, round, critique, subtopics FROM "
                    "deepen_proposals WHERE status='pending' ORDER BY id DESC"
                ).fetchall()
        finally:
            conn.close()
        out = []
        for r in rows:
            out.append({"id": r["id"], "topic": r["topic"], "round": r["round"],
                        "critique": r["critique"],
                        "subtopics": json.loads(r["subtopics"] or "[]")})
        return out

    def reject_deepen_proposal(self, prop_id: int) -> bool:
        conn = self._connect()
        try:
            cur = conn.execute("UPDATE deepen_proposals SET status='rejected' "
                               "WHERE id=? AND status='pending'", (prop_id,))
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    def apply_deepen_proposal(self, config: dict, prop_id: int = None,
                              user_critique: str = "") -> dict:
        """Schválení: znovu otevře studijní program s hlubšími pod-tématy.
        prop_id=None → nejnovější pending. user_critique → přegeneruje pod-témata
        podle KRITIKY UŽIVATELE (má přednost před původním návrhem). Vrací
        {status: deepened/idle/deferred, added, round, topic}."""
        pend = self.get_pending_deepen()
        if not pend:
            return {"status": "idle", "reason": "žádný čekající návrh"}
        prop = next((p for p in pend if p["id"] == prop_id), None) if prop_id \
            else pend[0]
        if not prop:
            return {"status": "idle", "reason": "návrh nenalezen"}
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT id, curriculum, deepen_round, topic FROM study_program "
                "WHERE topic_norm=? AND status='completed' ORDER BY id DESC "
                "LIMIT 1", (_norm(prop["topic"]),)).fetchone()
        finally:
            conn.close()
        if not row:
            return {"status": "idle", "reason": "program není dokončený"}
        studied = json.loads(row["curriculum"] or "[]")
        # kritika od uživatele → přegeneruj témata podle ní; jinak z návrhu
        if user_critique.strip():
            gen = self._generate_deepening(config, row["topic"], studied,
                                           user_critique.strip(), 4)
            if gen is None:
                return {"status": "deferred", "reason": "LLM nedostupný"}
            subs = gen["subtopics"]
        else:
            subs = prop["subtopics"]
        added = [s for s in subs if _norm(s) not in {_norm(x) for x in studied}]
        if not added:
            return {"status": "idle", "reason": "žádné nové pod-téma"}
        new_round = int(row["deepen_round"] or 0) + 1
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE study_program SET curriculum=?, status='active', "
                "deepen_round=?, updated_ts=? WHERE id=?",
                (json.dumps(studied + added, ensure_ascii=False), new_round,
                 time.time(), row["id"]))
            conn.execute("UPDATE deepen_proposals SET status='approved' WHERE id=?",
                         (prop["id"],))
            conn.commit()
        finally:
            conn.close()
        _log.info("deepen SCHVÁLENO '%s' +%d témat → kolo %d%s", prop["topic"],
                  len(added), new_round, " (kritika uživatele)" if user_critique
                  else "")
        return {"status": "deepened", "added": added, "round": new_round,
                "topic": prop["topic"]}

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
