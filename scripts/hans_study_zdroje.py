"""Funkce přesunuté z `scripts/hans_study.py` (ROZDELENI_PRIKAZU_V1).

Text funkcí je beze změny; jména původního modulu se čtou přes `_hs.` až při
volání. Původní modul si funkce bere zpět importem, takže dosavadní importy platí.
Import původního modulu je na KONCI souboru (kruhový import oběma směry).
"""
from __future__ import annotations

from typing import List, Optional
import json
import re
import time

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
    core = _hs._GENERIC_LEADIN.sub("", head).strip()        # bez "Základy/Teorie…"
    core = re.sub(r"\s+v\s+\w+$", "", core).strip()     # "Kompozice v designu"→"Kompozice"

    # HANS_STUDY_SEARCH_ABBREV_V1 — LLM deepen generuje popisné věty s kanonickým
    # pojmem v ZÁVORCE („Analýza přístupnosti webových stránek (WCAG) a aplikací:…").
    # Wikipedia dlouhou frázi nenajde; zkratka JE článek. Zkratky (2-10 zn, velká
    # písmena / číslice / lomítka) mají PŘEDNOST před ostatními kandidáty.
    for m in re.finditer(r"\(([A-Z][A-Z0-9/.-]{1,10})\)", s):
        _add(m.group(1))

    # HANS_STUDY_SEARCH_SHORT_V1 — když core je moc dlouhý (>4 slova), přidej
    # KRÁTKÉ jádro = první 3 obsahová slova (bez závorek). Trefí kanonický článek
    # dřív než rozvláčná fráze („Analýza přístupnosti webových stránek" místo
    # celé věty).
    core_short = re.sub(r"\([^)]*\)", "", core).strip()
    core_short = re.sub(r"\s+", " ", core_short)
    words = core_short.split()
    if len(words) > 4:
        _add(" ".join(words[:3]))

    # JÁDRO nejdřív = nejkanoničtější článek (full-text search dá u dlouhé popisné
    # fráze často nesmysl-ale-neprázdný výsledek → stopne se na něm; čisté jádro
    # trefí správný článek). Pak širší fallbacky.
    _add(core)
    _add(head)
    _add(s)
    _add(f"{core} {topic}".strip() if core else f"{s} {topic}".strip())
    return out


def _topic_anchor_tokens(topic: str) -> list:
    """Jádro názvu programu jako tokeny: „Český ráj a okolní hrady" → [cesky, raj].
    Ořízne na první spojce/závorce/dvojtečce — zbytek jsou přílepky, ne předmět."""
    from scripts.web_reader import _title_tokens
    t = (topic or "").strip()
    t = re.split(r"\s+(?:a|i|nebo|se|v|na)\s+|[(:,–-]", t, maxsplit=1)[0]
    return _title_tokens(t)


def _geo_base(word: str) -> str:
    """Ořízne místní příponu. Krátká slova nechá být (ať nevznikne pahýl)."""
    w = (word or "").strip(" .,;:")
    if len(w) < 7:
        return w
    for suf in _hs._GEO_SUFFIXES:
        if w.lower().endswith(suf) and len(w) - len(suf) >= 4:
            return w[:-len(suf)]
    return w


def _decomposed_anchor_queries(sub: str, topic: str, limit: int = 3) -> list:
    """Dotazy na kotvu ze SLOŽEK fráze, od nejkonkrétnější k nejobecnější.

    „Hrady a zříceniny Jičínska" / „Český ráj a okolní hrady"
        → ['Jičín', 'Český ráj', 'zříceniny']
    Vlastní jména pod-tématu jdou první (nesou místo/osobu), pak jádro tématu
    programu, teprve nakonec obecné pojmy.
    """
    out, seen = [], set()

    def _add(q):
        q = (q or "").strip(" .,;:–-")
        if len(q) >= 4 and q.lower() not in seen:
            seen.add(q.lower())
            out.append(q)

    s = (sub or "").split(":")[0]
    import re as _re0
    # 0) POD-TÉMA BEZ KONCOVÉ PŘEDLOŽKOVÉ VAZBY — nejlepší kandidát, protože
    #    zůstane vlastní předmět: „Gotická architektura v Čechách" → „Gotická
    #    architektura" (což článek JE). Bez tohohle padalo studium až na obecnou
    #    kotvu typu „Historie", která k pod-tématu nemá co říct.
    _trim = _re0.sub(r"\s+(?:v|ve|na|o|u|za|pro|při|po|do|od|s|se|k|ke)\s+\S+.*$",
                     "", s).strip()
    if _trim and _trim.lower() != s.strip().lower() and len(_trim.split()) >= 2:
        _add(_trim)
    # 0b) první dvě slova pod-tématu (když druhé není spojka/předložka) —
    #     „Archeologické metody a datování" → „Archeologické metody"
    _w = s.split()
    if len(_w) >= 2 and _w[1].lower() not in (
            "a", "i", "nebo", "v", "ve", "na", "o", "u", "za", "pro", "při",
            "po", "do", "od", "s", "se", "k", "ke"):
        _add(" ".join(_w[:2]))
    # 1) vlastní jména pod-tématu (velké písmeno uvnitř věty) + geo-normalizace.
    #    Genitivní/lokálová přídavná jména („Českého", „Broumovském") samostatný
    #    článek nikdy nejsou — jen by spálila dotaz proti rate-limitu.
    for wd in s.split()[1:]:
        if not wd[:1].isupper():
            continue
        base = _hs._geo_base(wd)
        if base.lower() != wd.lower():
            _add(base)               # „Broumovském" → „Broumov" = dobrý kandidát
        elif not wd.lower().endswith(
                ("ého", "ému", "ém", "ých", "ým", "ými", "ou",
                 # ⚠️ 17.8. živý test: „Čechách" (lokál) našel „Čechočovice" —
                 # tvary v nepřímém pádě nejsou názvy článků a přes práh
                 # podobnosti propašují CIZÍ obec. Radši je nezkoušet vůbec.
                 "ách", "ích", "ám", "ím", "emi", "ami")):
            _add(wd)                 # „Českého" sem nepatří, článek to není
    # 2) jádro tématu programu jako FRÁZE („Český ráj a okolní hrady" → „Český ráj")
    import re as _re
    core = _re.split(r"\s+(?:a|i|nebo|se|v|na)\s+|[(:,–-]", (topic or "").strip(),
                     maxsplit=1)[0]
    _add(core)
    # 3) obecná podstatná jména pod-tématu (poslední záchrana)
    for wd in s.split():
        if len(wd) >= 6 and not wd[:1].isupper():
            _add(wd)
    return out[:limit]


def _anchor_pick(config: dict, w, sub: str, topic: str, lang: str,
                 used_titles: set) -> Optional[str]:
    """Vyber článek kotvený na téma programu. None = nic vhodného."""
    from scripts.web_reader import _title_tokens, _token_match
    c = _hs._cfg(config)
    anchor = _hs._topic_anchor_tokens(topic)
    if not anchor:
        return None
    min_score = float(c.get("anchor_min_score", 0.30))
    max_tok = int(c.get("anchor_max_tokens", 4))
    try:
        cands = w.wikipedia_search_candidates(sub, lang=lang, limit=6)
    except Exception as e:
        _hs._log.debug("anchor candidates: %s", e)
        return None
    for title, score in cands:
        if _hs._norm(title) in used_titles:
            continue                     # ať 2 pod-témata nečtou týž článek
        ttok = _title_tokens(title)
        if len(ttok) > max_tok:
            continue                     # dlouhý titul = tangenciální odbočka
        if not all(any(_token_match(a, tt) for tt in ttok) for a in anchor):
            continue                     # neobsahuje předmět programu → mimo
        if score < min_score:
            continue
        _hs._log.info("study: '%s' — přesný článek neexistuje, kotvím na téma "
                  "programu → '%s' (skóre %.2f)", sub, title, score)
        return title
    # HANS_STUDY_ANCHOR_DECOMPOSE_V1 — druhý průchod: dotazy ze SLOŽEK fráze.
    # Pravidlo výše žádá titul obsahující VŠECHNY tokeny tématu, což u místních
    # a odborných článků („Jičín", „Kumburk") nikdy neprojde. Tady se ptáme
    # přímo na složky; o relevanci rozhoduje title-similarity gate uvnitř
    # `_wikipedia_search`, takže se nepřimyká nic nesouvisejícího.
    _maxq = int(c.get("anchor_decompose_max", 3))
    for q in _hs._decomposed_anchor_queries(sub, topic, limit=_maxq):
        if getattr(w, "last_transient", False):
            break                        # rate-limit → nezhoršuj to dalšími dotazy
        try:
            t = w._wikipedia_search(q, lang)
        except Exception as e:
            _hs._log.debug("anchor decompose '%s': %s", q, e)
            continue
        if not t or _hs._norm(t) in used_titles:
            continue
        # ⚠️ DRUHÁ POJISTKA (17.8., z živého testu): práh podobnosti sám
        # nestačí — dotaz „Čechách" prošel na článek „Čechočovice" (cizí obec).
        # Titul proto musí NĚKTERÝM tokenem odpovídat dotazu, jinak ho zahoď.
        _qtok = _title_tokens(q)
        _ttok = _title_tokens(t)
        if _qtok and not any(_token_match(a, b) for a in _qtok for b in _ttok):
            _hs._log.info("study: kotva '%s' pro dotaz '%s' ZAMÍTNUTA "
                      "(titul dotazu neodpovídá)", t, q)
            continue
        _hs._log.info("study: '%s' — kotva ze složky fráze '%s' → '%s'", sub, q, t)
        return t
    return None


def _used_main_titles(db_path: str, topic: str) -> set:
    """Hlavní články, které už tenhle program v kotvené větvi použil."""
    if not db_path:
        return set()
    try:
        import sqlite3 as _s
        conn = _s.connect(db_path, timeout=5.0)
        conn.execute("CREATE TABLE IF NOT EXISTS study_seen_works "
                     "(work_id TEXT PRIMARY KEY, title TEXT, ts REAL)")
        pref = "wiki:%s:" % _hs._norm(topic)
        rows = conn.execute(
            "SELECT title FROM study_seen_works WHERE work_id LIKE ?",
            (pref + "%",)).fetchall()
        conn.close()
        return {_hs._norm(r[0]) for r in rows if r and r[0]}
    except Exception:
        return set()


def _mark_main_title(db_path: str, topic: str, title: str) -> None:
    if not (db_path and title):
        return
    try:
        import sqlite3 as _s, time as _t
        conn = _s.connect(db_path, timeout=5.0)
        conn.execute("CREATE TABLE IF NOT EXISTS study_seen_works "
                     "(work_id TEXT PRIMARY KEY, title TEXT, ts REAL)")
        conn.execute("INSERT OR IGNORE INTO study_seen_works "
                     "(work_id, title, ts) VALUES (?,?,?)",
                     ("wiki:%s:%s" % (_hs._norm(topic), _hs._norm(title)), title, _t.time()))
        conn.commit()
        conn.close()
    except Exception as e:
        _hs._log.debug("mark_main_title: %s", e)


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
        _hs._log.debug("_record_works: %s", e)


def _en_title(cs_title: str, lang: str = "cs") -> str:
    """ANGLICKÝ název tématu přes mezijazyčný odkaz Wikipedie (deterministicky,
    'Cardiffský hrad'→'Cardiff Castle'). EN zdroje (IA, en.wikisource, OpenAlex)
    na český/skloňovaný název nic nenajdou. '' když link není/chyba."""
    if lang == "en" or not cs_title:
        return cs_title or ""
    import requests as _rq
    # HANS_WIKI_THROTTLE_V1 — tenhle dotaz jde MIMO WebReader session, ale do
    # TÉHOŽ rate-limit rozpočtu; bez rozestupu prodlužoval dávku, která si 429
    # vyrobila. Cooldown = přechodný výpadek → prázdný název (volající to už umí).
    try:
        from scripts import _wiki_throttle as _wt
        _wt.acquire(f"https://{lang}.wikipedia.org/w/api.php")
    except Exception as e:
        _hs._log.debug("_en_title: throttle (%s)", e)
        return ""
    try:
        r = _rq.get(f"https://{lang}.wikipedia.org/w/api.php", params={
            "action": "query", "prop": "langlinks", "titles": cs_title,
            "lllang": "en", "format": "json", "formatversion": 2},
            headers=_hs._UA, timeout=12)
        r.raise_for_status()
        pages = (r.json().get("query", {}) or {}).get("pages", []) or []
        for p in pages:
            ll = p.get("langlinks") or []
            if ll:
                return ll[0].get("title") or ""
    except Exception as e:
        _hs._log.debug("_en_title(%s): %s", cs_title, e)
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
    seen = _hs._seen_work_ids(db_path)
    # HANS_WIKI_THROTTLE_WS_V1 (18.8.) — Wikisource je TÁŽ Wikimedia infrastruktura
    # a čerpá z téhož rozpočtu jako Wikipedia; studium sahá na obojí v jednom kole.
    from scripts import _wiki_throttle as _wt
    for lang in langs:
        api = f"https://{lang}.wikisource.org/w/api.php"
        try:
            _wt.acquire(api)
            r = _rq.get(api, params={
                "action": "query", "list": "search", "srsearch": query,
                "srlimit": 4, "format": "json"}, headers=_hs._UA, timeout=15)
            r.raise_for_status()
            hits = (r.json().get("query", {}) or {}).get("search", []) or []
        except Exception as e:
            _hs._log.debug("_wikisource_read search (%s): %s", lang, e)
            continue
        qwords = {w for w in _hs._norm(query).split() if len(w) > 3}
        for h in hits:
            title = h.get("title") or ""
            wid = f"ws_{lang}_{_hs._norm(title)}"
            if not title or wid in seen:
                continue
            # relevance: aspoň jedno slovo dotazu v názvu (jinak search vrací
            # svazky slovníků/rozcestníky, kde je dotaz jen zmíněn v obsahu)
            tnorm = _hs._norm(title)
            if qwords and not any(w in tnorm for w in qwords):
                continue
            try:
                r = _rq.get(api, params={
                    "action": "parse", "page": title, "prop": "text",
                    "format": "json", "formatversion": 2},
                    headers=_hs._UA, timeout=20)
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
            _hs._record_works(db_path, [(wid, title)])
            _hs._log.info("study: Wikisource(%s) '%s' → %d zn", lang, title,
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
    seen = _hs._seen_work_ids(db_path)
    try:
        r = _rq.get("https://archive.org/advancedsearch.php", params={
            "q": f"({query}) AND mediatype:texts AND year:[1500 TO 1929]",
            "fl[]": ["identifier", "title", "year"],
            "rows": 6, "output": "json", "sort[]": "downloads desc"},
            headers=_hs._UA, timeout=20)
        r.raise_for_status()
        docs = (r.json().get("response", {}) or {}).get("docs", []) or []
    except Exception as e:
        _hs._log.debug("_ia_research search: %s", e)
        return ""
    for d in docs:
        ident = d.get("identifier") or ""
        wid = f"ia_{ident}"
        if not ident or wid in seen:
            continue
        try:
            r = _rq.get(f"https://archive.org/download/{ident}/{ident}_djvu.txt",
                        headers=_hs._UA, timeout=30, allow_redirects=True)
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
        _hs._record_works(db_path, [(wid, str(d.get("title") or ident))])
        year = d.get("year") or "?"
        _hs._log.info("study: InternetArchive '%s' (%s) → %d zn",
                  str(d.get("title"))[:60], year, min(len(txt), max_chars))
        return (f"[Kniha (Internet Archive): {d.get('title')} ({year})]\n"
                + txt[:max_chars])
    return ""


def _openalex_research(config: dict, query: str, n: int = 3,
                       max_chars: int = 4000, db_path: str = None,
                       sink: list = None) -> str:
    """Vytáhne z OpenAlexu pár nejrelevantnějších NOVÝCH prací (název+rok+autoři+
    abstrakt) k tématu. DEDUP: práce použité dřív (study_seen_works) přeskočí, ať
    Hans necituje stejnou práci/autora opakovaně. '' při chybě/nic. Best-effort.

    HANS_RESEARCH_PAPER_TAKEAWAY_V1 — když je `sink` list, přidá do něj i
    STRUKTURU každé použité práce (title/year/authors/abstract/url), ať z ní
    volající umí udělat per-práce trvalý výpisek (ne jen titul v registru)."""
    import requests
    rc = _hs._cfg(config).get("research_tier", {}) or {}
    mailto = rc.get("mailto", "hans@local")
    seen = _hs._seen_work_ids(db_path)
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
        _hs._log.warning("research tier OpenAlex selhal (%s): %s", query, e)
        return ""
    blocks = []
    new_items = []
    skipped = 0
    for w in works:
        wid = w.get("id") or ""
        title = (w.get("title") or "").strip()
        abstract = _hs._reconstruct_abstract(w.get("abstract_inverted_index"))
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
        if sink is not None:                    # HANS_RESEARCH_PAPER_TAKEAWAY_V1
            sink.append({"title": title, "year": year, "authors": authors,
                         "abstract": abstract, "url": wid})
        if len(blocks) >= int(n):
            break
    _hs._record_works(db_path, new_items)
    out = "\n\n".join(blocks)
    if out:
        _hs._log.info("study: research tier — %d nových prací pro '%s' (%d již viděných)",
                  len(blocks), query, skipped)
    return out[:max_chars]


def _gather_material(config: dict, sub: str, topic: str, deep: bool = False,
                     db_path: str = None, papers_sink: list = None):
    """Nastuduj pod-téma do hloubky: PLNÝ hlavní článek (ne jen lead) + úvody
    několika nejrelevantnějších pododkazů z úvodní sekce (v pořadí výskytu).
    deep=True (HANS_STUDY_RESEARCH_TIER_V1) → navíc abstrakty skutečného výzkumu
    z OpenAlexu (odemčeno u velmi silného koníčku).
    Vrací (material_text, source_url, main_title) nebo (None, None, None)."""
    c = _hs._cfg(config)
    lang = str(c.get("wiki_lang", "cs"))
    art_max = int(c.get("article_max_chars", 12000))
    sub_n = int(c.get("sublink_count", 3))
    sub_max = int(c.get("sublink_max_chars", 2500))
    try:
        from scripts.web_reader import WebReader
    except ImportError:
        _hs._log.warning("_gather_material: WebReader nedostupný")
        return None, None, None
    w = WebReader(config)
    art = None
    # HANS_STUDY_MDN_V1 — webová témata nejdřív z MDN (praktický zdroj)
    if _hs._je_web_tema(sub, topic) and _hs._cfg(config).get("mdn_enabled", True):
        art = _hs._mdn_clanek(config, sub, topic, art_max)
    try:
        for q in ([] if art else _hs._search_queries(sub, topic)):
            art = w.wikipedia_article(q, lang=lang, max_chars=art_max)
            if art and (art.get("text") or "").strip():
                if q != sub:
                    _hs._log.info("_gather_material: '%s' → článek přes dotaz '%s'", sub, q)
                break
    except Exception as e:
        _hs._log.warning("_gather_material čtení selhalo (%s): %s", sub, e)
        return None, None, None
    # HANS_STUDY_TOPIC_ANCHOR_V1 — všechny dotazy selhaly (přesný článek pro
    # složené pod-téma neexistuje). Poslední záchrana PŘED `noread`: článek
    # kotvený na téma programu. Dedup přes study_seen_works, ať dvě pod-témata
    # nečtou týž článek. Když ani to nic nedá, chová se to jako dřív.
    if not art or not (art.get("text") or "").strip():
        try:
            _anchor = _hs._anchor_pick(config, w, sub, topic, lang,
                                   _hs._used_main_titles(db_path, topic))
            if _anchor:
                art = w.wikipedia_article(_anchor, lang=lang, max_chars=art_max)
                if art and (art.get("text") or "").strip():
                    _hs._mark_main_title(db_path, topic, art.get("page_title") or _anchor)
        except Exception as e:
            _hs._log.debug("anchor fallback: %s", e)
    if not art or not (art.get("text") or "").strip():
        # HANS_WIKI_TRANSIENT_V1 — rozliš „článek neexistuje" od „Wikipedia
        # zrovna neodpovídá" (429/5xx). Druhé NESMÍ spálit pokus, jinak by
        # rate-limit po 3 nocích přeskočil i pod-téma, které článek MÁ.
        if getattr(w, "last_transient", False):
            _hs._log.info("_gather_material: '%s' — Wikipedia dočasně nedostupná, "
                      "ODKLÁDÁM (pokus se nepočítá)", sub)
            return None, None, "__transient__"
        return None, None, None

    if art.get("zdroj") != "mdn":   # MDN už soudce vybral
        art = _hs._overeny_clanek(config, w, sub, topic, art, art_max)  # HANS_STUDY_ARTICLE_JUDGE_V1
        if art.get("_odmitnut"):          # HANS_STUDY_SKIP_REJECTED_V1
            return None, None, "__odmitnuto__"
    used_lang = art.get("lang", lang)
    parts = [f"[Hlavní článek: {art['page_title']}]\n{art['text']}"]
    if _hs._je_web_tema(sub, topic) and _hs._cfg(config).get("wcag_enabled", True):
        parts += _hs._wcag_casti(sub, topic)       # HANS_STUDY_WCAG_V1
    if sub_n > 0 and art.get("zdroj") != "mdn":   # pododkazy jsou z Wikipedie
        try:
            links = w.wikipedia_lead_links(art["page_title"], lang=used_lang,
                                           limit=sub_n + 3)
        except Exception:
            links = []
        added = 0
        seen = {_hs._norm(art["page_title"])}
        for lt in links:
            if added >= sub_n:
                break
            if _hs._norm(lt) in seen:
                continue
            seen.add(_hs._norm(lt))
            try:
                intro = w.wikipedia_intro(lt, lang=used_lang, max_chars=sub_max)
            except Exception:
                intro = ""
            if intro and len(intro) > 120:
                parts.append(f"[Související pojem: {lt}]\n{intro}")
                added += 1
        _hs._log.info("study: materiál '%s' = článek %d zn + %d pododkazů",
                  sub, len(art["text"]), added)
    if deep:
        # HANS_STUDY_RESEARCH_TIER_V1 — přidej abstrakty skutečného výzkumu.
        # Dotaz = STRUČNÝ vyřešený název článku (ne ukecané pod-téma z kurikula —
        # OpenAlex na dlouhou frázi nic nevrátí). Zkus název článku, fallback jádro.
        # HANS_STUDY_SOURCES_V2 — EN název přes mezijazyčný link (EN zdroje na
        # český/skloňovaný název nic nenajdou; zlepší i trefnost OpenAlexu).
        en = _hs._en_title(art["page_title"], lang=used_lang)
        try:
            rc = _hs._cfg(config).get("research_tier", {}) or {}
            _oa_queries = ([en] if en and en != art["page_title"] else []) + \
                [art["page_title"], _hs._search_queries(sub, topic)[0]]
            for rq in _oa_queries:
                research = _hs._openalex_research(
                    config, rq, n=int(rc.get("results", 3)),
                    max_chars=int(rc.get("max_chars", 4000)), db_path=db_path,
                    sink=papers_sink)  # HANS_RESEARCH_PAPER_TAKEAWAY_V1
                if research:
                    parts.append(research)
                    break
        except Exception as e:
            _hs._log.debug("research tier selhal: %s", e)
        # primární texty (Wikisource cs→en) + knihy (Internet Archive, EN)
        try:
            rc = _hs._cfg(config).get("research_tier", {}) or {}
            if rc.get("wikisource_enabled", True):
                ws = _hs._wikisource_read(
                    config, art["page_title"],
                    max_chars=int(rc.get("wikisource_max_chars", 3000)),
                    db_path=db_path)
                if not ws and en and en != art["page_title"]:
                    ws = _hs._wikisource_read(
                        config, en, langs=("en",),
                        max_chars=int(rc.get("wikisource_max_chars", 3000)),
                        db_path=db_path)
                if ws:
                    parts.append(ws)
            if rc.get("archive_enabled", True):
                ia = _hs._ia_research(
                    config, (en or art["page_title"]),
                    max_chars=int(rc.get("archive_max_chars", 2500)),
                    db_path=db_path)
                if ia:
                    parts.append(ia)
        except Exception as e:
            _hs._log.debug("sources V2 selhaly: %s", e)
    return "\n\n".join(parts), art.get("url", ""), art["page_title"]


def _soudce_clanku(config: dict, topic: str, sub: str, title: str, lead: str,
                   lang: str) -> str:
    """'ano' / 'castecne' / 'ne'; '' = nerozhodnuto (LLM dole, herní mód)."""
    try:
        from scripts.ollama_client import ollama_generate
        raw = ollama_generate(
            str(_hs._cfg(config).get("judge_model", "qwen2.5:7b")),
            "Téma: %s\nPod-téma: %s\nČlánek: %s (%s Wikipedia)\nÚvod článku: %s"
            % (topic, sub, title, lang, (lead or "")[:700]),
            system=_hs._JUDGE_SYSTEM, config=config, timeout=120, keep_alive=300,
            format=_hs._JUDGE_SCHEMA,
            options={"temperature": 0, "num_ctx": 4096, "num_predict": 60})
        return (json.loads(raw or "{}").get("verdikt") or "")
    except Exception as e:
        _hs._log.debug("soudce článku: %s", e)
        return ""


def _overeny_clanek(config: dict, w, sub: str, topic: str, art: dict,
                    art_max: int) -> dict:
    if not _hs._cfg(config).get("judge_article", True):
        return art
    title = art.get("page_title") or ""
    if _hs._norm(title) == _hs._norm(sub):
        return art
    lang0 = art.get("lang", "cs")
    v0 = _hs._soudce_clanku(config, topic, sub, title, art.get("text", "")[:700], lang0)
    if v0 != "ne":
        return art
    kand = []
    head = sub.split(":")[0].strip()
    for q in dict.fromkeys([sub, head]):
        try:
            r = w._get("https://cs.wikipedia.org/w/api.php", params={
                "action": "query", "list": "search", "srsearch": q,
                "format": "json", "srlimit": 5, "srnamespace": 0}, timeout=15)
            kand += [("cs", h["title"]) for h in
                     r.json().get("query", {}).get("search", [])]
        except Exception:
            pass
    try:
        from scripts.ollama_client import ollama_generate
        en = _hs._en_dotaz(config, topic, sub)
        if en:
            r = w._get("https://en.wikipedia.org/w/api.php", params={
                "action": "query", "list": "search", "srsearch": en,
                "format": "json", "srlimit": 3, "srnamespace": 0}, timeout=15)
            kand += [("en", h["title"]) for h in
                     r.json().get("query", {}).get("search", [])]
    except Exception as e:
        _hs._log.debug("soudce: en kandidáti: %s", e)
    vyber, stopa = None, []
    for lg, t in list(dict.fromkeys(kand))[:8]:
        if t == title:
            continue
        try:
            lead = w.wikipedia_intro(t, lang=lg, max_chars=700) or ""
        except Exception:
            lead = ""
        if not lead or (_hs._ZIVOTOPIS.search(lead[:200]) and not _hs._ZIVOTOPIS.search(sub)):
            continue
        v = _hs._soudce_clanku(config, topic, sub, t, lead, lg)
        stopa.append("%s:%s=%s" % (lg, t, v))
        if v == "ano":
            vyber = (lg, t); break
        if v == "castecne" and not vyber:
            vyber = (lg, t)
    if not vyber:
        # HANS_STUDY_SKIP_REJECTED_V1 (27. 9.) — odmítnutý článek se už
        # NESTUDUJE. Doloženo: „Auditorní kortex“ → „Kortizol“ (soudce ne,
        # nic lepšího) a Hans pak tvrdil, že vnímání hudby souvisí s produkcí
        # kortizolu. Změřeno 26. 9.: ~15–25 % pod-témat končilo takhle.
        if _hs._cfg(config).get("skip_rejected_article", True):
            _hs._log.info("study: '%s' → článek '%s' soudce odmítl, lepší se nenašel "
                      "→ PŘESKAKUJI (nestuduji z nesouvisejícího) %s", sub, title, stopa)
            return dict(art, _odmitnut=True)
        _hs._log.info("study: '%s' → článek '%s' soudce odmítl, lepší se nenašel "
                  "(nechávám) %s", sub, title, stopa)
        return art
    try:
        text = w._wiki_extract(vyber[1], vyber[0], intro_only=False) or ""
    except Exception:
        text = ""
    if len(text) < 400:
        return art
    import requests as _rq
    _hs._log.info("study: '%s' → článek '%s' soudce odmítl, beru %s:%s %s",
              sub, title, vyber[0], vyber[1], stopa)
    return {"page_title": vyber[1], "title": vyber[1], "lang": vyber[0],
            "url": "https://%s.wikipedia.org/wiki/%s" % (
                vyber[0], _rq.utils.quote(vyber[1].replace(" ", "_"))),
            "text": text[:art_max]}


def _je_web_tema(sub: str, topic: str) -> bool:
    import unicodedata as _ud
    t = "".join(c for c in _ud.normalize("NFKD", "%s %s" % (sub, topic))
                if not _ud.combining(c))
    return bool(_hs._WEB_TEMA.search(t))


def _en_dotaz(config: dict, topic: str, sub: str) -> str:
    """Krátký anglický vyhledávací dotaz k pod-tématu ('' při selhání)."""
    try:
        from scripts.ollama_client import ollama_generate
        return (json.loads(ollama_generate(
            str(_hs._cfg(config).get("judge_model", "qwen2.5:7b")),
            "Topic: %s\nSubtopic: %s" % (topic, sub),
            system="Translate the Czech study subtopic into a short English "
                   "search query (2-5 words).",
            config=config, timeout=60, keep_alive=300,
            format={"type": "object", "properties": {"en": {"type": "string"}},
                    "required": ["en"]},
            options={"temperature": 0, "num_predict": 40}) or "{}").get("en", "")
            or "").strip()
    except Exception as e:
        _hs._log.debug("_en_dotaz: %s", e)
        return ""


def _wcag_casti(sub: str, topic: str, max_chars: int = 4000) -> list:
    import html as _h
    import requests as _rq
    import unicodedata as _ud
    t = "".join(c for c in _ud.normalize("NFKD", sub.lower())
                if not _ud.combining(c))
    slugy = []
    for vzor, ss in _hs._WCAG_MAPA:
        if re.search(vzor, t):
            slugy += [x for x in ss if x not in slugy]
    out = []
    for sl in slugy[:4]:
        try:
            r = _rq.get(_hs._WCAG % sl, headers=_hs._UA, timeout=20)
            m = re.search(r"<main[\s\S]*?</main>", r.text)
            x = re.sub(r"<(script|style)[\s\S]*?</\1>", "", m.group(0) if m else r.text)
            x = re.sub(r"\s+", " ", _h.unescape(re.sub(r"<[^>]+>", " ", x))).strip()
            if len(x) > 400:
                out.append("[WCAG 2.2: %s]\n%s" % (sl, x[:max_chars]))
        except Exception as e:
            _hs._log.debug("WCAG %s: %s", sl, e)
    if out:
        _hs._log.info("study: '%s' + WCAG %s", sub, slugy[:4])
    return out


def _mdn_text(doc: dict) -> str:
    """Tělo MDN článku (index.json) jako prostý text s mezititulky."""
    import html as _h
    out = []
    for b in doc.get("body") or []:
        v = b.get("value") or {}
        if v.get("title"):
            out.append("\n## %s\n" % v["title"])
        c = v.get("content") or ""
        if c:
            c = re.sub(r"<(script|style)[\s\S]*?</\1>", "", c)
            out.append(_h.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", c))).strip())
    return "\n".join(x for x in out if x.strip())


def _mdn_clanek(config: dict, sub: str, topic: str, art_max: int):
    """Nejlepší MDN článek k pod-tématu (soudce nad shrnutím), nebo None."""
    import requests as _rq
    en = _hs._en_dotaz(config, topic, sub)
    if not en:
        return None
    try:
        r = _rq.get(_hs._MDN + "/api/v1/search", params={"q": en, "locale": "en-US",
                                                     "size": 6},
                    headers=_hs._UA, timeout=15)
        docs = (r.json() or {}).get("documents") or []
    except Exception as e:
        _hs._log.info("study: MDN hledání '%s' selhalo: %s", en, e)
        return None
    # až N schválených článků: přehledová stránka čísla nemá, konkrétní
    # („Color contrast“) ano — pilot 26. 9.: 1 článek = 0 měřitelných pravidel
    ano, cast, stopa = [], [], []
    for d in docs[:6]:
        url = d.get("mdn_url") or ""
        if not url or "/Glossary/" in url:      # slovníček = jedna věta, málo látky
            continue
        v = _hs._soudce_clanku(config, topic, sub, d.get("title", ""),
                           d.get("summary", ""), "MDN")
        stopa.append("%s=%s" % (d.get("title"), v))
        (ano if v == "ano" else cast if v == "castecne" else []).append(d)
    vybrane = (ano + cast)[:int(_hs._cfg(config).get("mdn_articles", 3))]
    if not vybrane:
        _hs._log.info("study: '%s' — MDN (%s) nic vhodného %s", sub, en, stopa)
        return None
    casti = []
    for d in vybrane:
        try:
            doc = _rq.get(_hs._MDN + d["mdn_url"] + "/index.json", headers=_hs._UA,
                          timeout=20).json().get("doc") or {}
            t = _hs._mdn_text(doc)
        except Exception as e:
            _hs._log.info("study: MDN článek %s nestažen: %s", d.get("mdn_url"), e)
            continue
        if len(t) >= 400:
            casti.append("[MDN: %s]\n%s" % (d.get("title"), t))
    text = "\n\n".join(casti)
    if len(text) < 800:
        return None
    hlavni = vybrane[0]
    _hs._log.info("study: '%s' → MDN %s (%d zn) %s", sub,
              [d.get("title") for d in vybrane], len(text), stopa)
    return {"page_title": hlavni.get("title"), "title": hlavni.get("title"),
            "url": _hs._MDN + hlavni["mdn_url"], "text": text[:max(art_max, 20000)],
            "lang": "en", "zdroj": "mdn"}

# ROZDELENI_PRIKAZU_V1 — až na konci, viz hlavička
from scripts import hans_study as _hs  # noqa: E402
