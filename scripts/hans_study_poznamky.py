"""Funkce přesunuté z `scripts/hans_study.py` (ROZDELENI_PRIKAZU_V1).

Text funkcí je beze změny; jména původního modulu se čtou přes `_hs.` až při
volání. Původní modul si funkce bere zpět importem, takže dosavadní importy platí.
Import původního modulu je na KONCI souboru (kruhový import oběma směry).
"""
from __future__ import annotations

import json
import re
import time

def _topic_engagement(diary_db_path: str, examples, polocas_dni: float = None) -> int:
    """OBJEM zájmu o koníček = počet zmínek jeho konkrétních instancí (examples)
    napříč čtenými/dialogovými/studijními eventy. Na rozdíl od evidence_count
    (= jen délka trvání) zachytí, jak moc Hanse téma reálně zaměstnává.

    HANS_ENGAGEMENT_DEDUP_V1 (2.9.) — ČTENÍ SE DEDUPLIKUJE NA (titul, den).
    Měřeno 2.9.: koníček 'Design' měl zaujetí 2037, z toho 678 řádků web_read
    + 481 reading_takeaway byl JEDEN A TÝŽ článek Wikipedie, přečtený znovu
    a znovu na objednávku aktivního cíle (113 různých dnů, duben-září).
    Metrika tedy neměřila zaujetí, ale kolikrát si ho cíl objednal - a přesně
    tahle čísla vybírají koníčky Severce (SEVERKA_HOBBY_ENGAGEMENT_V1)
    a odemykají výzkumnou úroveň studia (_is_strong_topic, min_engagement).
    Souvisí s GOAL_FOCUS_2C_V1 / HANS_GOAL_SELF_EVIDENCE_V1, kde je táž smyčka
    popsaná pro detektor cílů - tam ji uzavřel štítek [goal], sem ale
    nedosáhl, protože se tu počítají řádky bez ohledu na štítek.

    ⛔ Dialogy a studijní poznámky se NEDEDUPLIKUJÍ: teddy_dialog má konstantní
    titul 'Dialog s Kolačem' (2857 řádků), takže by dedup na titul smazal
    celou kategorii. Dedup míří jen na opakované čtení téhož článku.

    Dopad změřený na živé DB: Design 2037 -> 986, hrady 762 -> 512,
    historie a památky 537 -> 219, lední hokej 246 -> 26. Pořadí na špici
    se nemění (Design > hrady > historie), jen poměr klesl z 2,7x na 1,9x.
    ⚠️ Práh min_engagement (500) se ZÁMĚRNĚ neposouval - posunout ho zpět
    by vrátilo přesně to, co tahle oprava odstraňuje.
    ⚠️ Na _is_strong_topic ta změna ALE NEDOPADÁ VŮBEC (změřeno po patchi,
    ne odhadnuto): ten má dvě cesty spojené OR a první je
    evidence_count >= min_evidence (20). Všech 12 trvalých koníčků má 63-75,
    takže projdou dřív, než se na zaujetí vůbec dojde - gate dle OBJEMU je
    pro ně mrtvá větev. Reálný dopad má tahle oprava jen na POŘADÍ
    (hans_severka:286 a výběr studijního programu), ne na deep tier.
    """
    exs = [str(e).strip() for e in (examples or []) if len(str(e).strip()) >= 4][:8]
    if not exs:
        return 0
    total = 0
    try:
        import sqlite3 as _s
        conn = _s.connect("file:%s?mode=ro" % diary_db_path, uri=True, timeout=5.0)
        try:
            # HANS_ENGAGEMENT_DEDUP_V1 — čtení dedup na (titul, den), dialogy
            # a studijní poznámky po řádcích. Důvod v docstringu funkce.
            _blob = ("(coalesce(title,'')||coalesce(note,'')||coalesce(data,''))")
            # HANS_INTEREST_NO_ECHO_V1 (26. 9.) — bez vlastních study_note
            # (studium dokládalo zájem o sebe sama), dialog s tématem ze
            # studia se nepočítá, dialog 1× za (téma, den), četba spuštěná
            # televizí `[kodi]` ×0,5. Důvod u KODI_VAHA v hans_hobbies.
            from scripts.hans_hobbies import (studijni_temata, tema_dialogu,
                                              KODI_VAHA)
            _stud = studijni_temata(conn)
            # (titul, den) JEDNOU — přečtení `[kodi]` i výpisek bez značky
            # jsou tentýž podnět (jinak 0,5 + 1 a zaujetí by stouplo)
            _cist = ("SELECT coalesce(title,''), "
                     "date(ts,'unixepoch','localtime'), "
                     "MAX(CASE WHEN coalesce(note,'') LIKE '[kodi]%' THEN 1 ELSE 0 END), "
                     "MAX(ts) "
                     "FROM diary WHERE event_type IN ('web_read','reading_takeaway') "
                     "AND " + _blob + " LIKE ? GROUP BY 1, 2")
            _dial = ("SELECT note, date(ts,'unixepoch','localtime'), ts FROM diary "
                     "WHERE event_type='teddy_dialog' AND " + _blob + " LIKE ?")
            # HANS_HOBBY_SILA_V1 — `polocas_dni` = podnět starý N dní má
            # poloviční váhu (síla koníčku); None = bez vyhasínání (dosavadní
            # zaujetí pro výběr studia a Severku).
            _ted = time.time()

            def _vh(t):
                if not polocas_dni:
                    return 1.0
                return 0.5 ** (max(0.0, _ted - float(t or _ted)) / 86400.0 / polocas_dni)
            vazene = 0.0
            for ex in exs:
                _like = '%' + ex + '%'
                for _t, _d, _kodi, _ts in conn.execute(_cist, (_like,)):
                    vazene += (KODI_VAHA if _kodi else 1.0) * _vh(_ts)
                _dny = {}
                for n, d, _ts in conn.execute(_dial, (_like,)):
                    if _hs._norm(tema_dialogu(n)) in _stud:
                        continue
                    _k = (tema_dialogu(n).lower(), d)
                    _dny[_k] = max(_dny.get(_k, 0.0), float(_ts or 0))
                vazene += sum(_vh(t) for t in _dny.values())
            total = (round(vazene, 2) if polocas_dni else int(round(vazene)))
        finally:
            conn.close()
    except Exception:
        return 0
    return total


def _dir_tokens(s: str) -> set:
    """Normalizované tokeny (bez diakritiky, min. 4 znaky) pro afinitu."""
    import unicodedata
    s = "".join(c for c in unicodedata.normalize("NFKD", (s or "").lower())
                if not unicodedata.combining(c))
    return {w for w in re.split(r"[^a-z0-9]+", s) if len(w) >= 4}


def _tok_match(a: str, b: str) -> bool:
    """Shoda dvou tokenů přes PREFIX (české skloňování: hrady↔hradů,
    architektura↔architekturu). Sdílený prefix ≥5 znaků nebo jeden je prefix
    druhého (u kratších)."""
    n = min(len(a), len(b))
    if n < 4:
        return a == b
    p = 5 if n >= 5 else n
    return a[:p] == b[:p]


def _direction_affinity(direction_text: str, name: str, examples) -> float:
    """HANS_DIRECTION_STUDY_BIAS_V1 — jak moc koníček ladí s aktivním směrem.
    Podíl tokenů koníčku (název+příklady), které mají PREFIXOVOU shodu se
    směrem (řeší CZ skloňování). Konzervativní: jen nudge, reálný zájem
    (engagement) zůstává hlavní."""
    dtok = _hs._dir_tokens(direction_text)
    if not dtok:
        return 0.0
    htok = _hs._dir_tokens(name)
    for e in (examples or [])[:6]:
        htok |= _hs._dir_tokens(str(e))
    if not htok:
        return 0.0
    matched = sum(1 for h in htok if any(_hs._tok_match(h, d) for d in dtok))
    return matched / len(htok)


def _is_strong_topic(config: dict, diary_db_path: str, topic: str) -> bool:
    """Deep tier (skutečný výzkum) se odemkne u VELMI silného koníčku. Dvě cesty:
    (1) evidence_count >= min_evidence (délka trvání), NEBO (2) chytrý gate dle
    OBJEMU zájmu — engagement examples >= min_engagement (tak projde jen koníček,
    co Hanse opravdu hodně zaměstnává, jako Cardiff/hrady). False = jen Wikipedia."""
    rc = _hs._cfg(config).get("research_tier", {}) or {}
    if not rc.get("enabled", True):
        return False
    try:
        import sqlite3 as _s
        conn = _s.connect("file:%s?mode=ro" % diary_db_path, uri=True, timeout=4.0)
        try:
            row = conn.execute("SELECT evidence_count, examples FROM hobbies "
                               "WHERE name_norm=?", (_hs._norm(topic),)).fetchone()
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
    eng = _hs._topic_engagement(diary_db_path, examples)
    strong = eng >= int(rc.get("min_engagement", 500))
    if strong:
        _hs._log.info("study: deep tier ODEMČEN pro '%s' (objem zájmu %d)", topic, eng)
    return strong


def _article_rest_parts(full: str, art_max: int, part_max: int, max_parts: int):
    """Vrátí (části, nepřečteno_znaků): text ZA stropem hlavního čtení, dělený
    na hranicích sekcí/odstavců; koncové sekce s odkazy se vynechají."""
    full = full or ""
    m = _hs._REST_KONEC.search(full)
    telo = full[:m.start()] if m else full
    if len(telo) <= art_max or max_parts <= 0:
        return [], 0
    start = telo.rfind("\n", 0, art_max)
    rest = telo[start if start > art_max * 0.8 else art_max:]
    kusy, pos = [], 0
    for mm in _hs._REST_NADPIS.finditer(rest):
        if mm.start() > pos:
            kusy.append(rest[pos:mm.start()])
        pos = mm.start()
    kusy.append(rest[pos:])
    drobne = []
    for k in kusy:                      # sekce delší než část → po odstavcích
        while len(k) > part_max:
            cut = k.rfind("\n", 0, part_max)
            cut = cut if cut > part_max * 0.5 else part_max
            drobne.append(k[:cut])
            k = k[cut:]
        drobne.append(k)
    parts, cur = [], ""
    for k in drobne:
        if cur and len(cur) + len(k) > part_max:
            parts.append(cur)
            cur = ""
        cur += k
    if cur.strip():
        if parts and len(cur) < 800:
            parts[-1] += cur
        else:
            parts.append(cur)
    parts = [p.strip() for p in parts if len(p.strip()) >= 300]
    return parts[:max_parts], sum(len(p) for p in parts[max_parts:])


def _generate_part_note(config: dict, topic: str, sub: str, title: str,
                        part: str, i: int, n: int) -> str:
    """Poznámka z jedné dočítané části článku. '' při selhání (LLM dole)."""
    c = _hs._cfg(config)
    prompt = (f"Koníček: {topic}\nPod-téma, kvůli kterému článek čtu: {sub}\n\n"
              f"Článek: {title} — pokračování, část {i} z {n}:\n{part}\n\n"
              f"Napiš si poznámku k této části článku.")
    try:
        from scripts.ollama_client import ollama_generate
        from scripts.hans_persona import persona_name as _pn
    except ImportError:
        return ""
    try:
        raw = ollama_generate(model=_hs._model(config), prompt=prompt,
                              system=_hs._PART_SYSTEM.format(persona_name=_pn(config)),
                              config=config, timeout=int(c.get("llm_timeout", 300)),
                              keep_alive=0,
                              options={"temperature": 0.4,
                                       "num_ctx": int(c.get("num_ctx", 8192)),
                                       "num_predict": 600})
        return (raw or "").strip()
    except Exception as e:
        _hs._log.warning("_generate_part_note LLM selhal: %s", e)
        return ""


def _generate_note(config: dict, topic: str, sub: str, material: str) -> str:
    """Base LLM napíše studijní poznámku z materiálu. '' při selhání.
    num_ctx zvednut (default Ollama je 2048 → tichý ořez) ať se vejde plný
    článek + pododkazy; model (Gemma3) zvládne 128k, omezuje VRAM/num_ctx."""
    c = _hs._cfg(config)
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
        system = _hs._NOTE_SYSTEM.format(persona_name=_pn(config))
        raw = ollama_generate(model=_hs._model(config), prompt=prompt, system=system,
                              config=config, timeout=timeout,
                              keep_alive=0,
                              options={"temperature": 0.4, "num_ctx": num_ctx,
                                       "num_predict": 600})
        return (raw or "").strip()
    except Exception as e:
        _hs._log.warning("_generate_note LLM selhal: %s", e)
        return ""


def _distill_paper(config: dict, topic: str, paper: dict) -> str:
    """LLM napíše krátký výpisek z abstraktu JEDNÉ práce. '' při selhání —
    volající pak sáhne po deterministickém fallbacku (samotný abstrakt)."""
    abstract = (paper.get("abstract") or "").strip()
    if len(abstract) < 80:
        return ""
    c = _hs._cfg(config)
    timeout = int(c.get("llm_timeout", 300))
    prompt = (f"Téma studia: {topic}\n"
              f"Práce: {paper.get('title', '')} "
              f"({paper.get('year', '')}; {paper.get('authors', '')})\n\n"
              f"Abstrakt:\n{abstract[:2000]}")
    try:
        from scripts.ollama_client import ollama_generate
        from scripts.hans_persona import persona_name as _pn
    except ImportError:
        return ""
    try:
        system = _hs._PAPER_TAKEAWAY_SYSTEM.format(persona_name=_pn(config))
        raw = ollama_generate(model=_hs._model(config), prompt=prompt, system=system,
                              config=config, timeout=timeout, keep_alive=0,
                              options={"temperature": 0.4, "num_ctx": 4096,
                                       "num_predict": 220})
        return (raw or "").strip()
    except Exception as e:
        _hs._log.warning("_distill_paper LLM selhal: %s", e)
        return ""


def _generate_mastery(config: dict, topic: str, subs: list, notes: list) -> str:
    c = _hs._cfg(config)
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
        system = _hs._MASTERY_SYSTEM.format(persona_name=_pn(config))
        raw = ollama_generate(model=_hs._model(config), prompt=prompt, system=system,
                              config=config, timeout=timeout,
                              keep_alive=0,
                              options={"temperature": 0.4, "num_ctx": num_ctx,
                                       "num_predict": 700})
        return (raw or "").strip()
    except Exception as e:
        _hs._log.warning("_generate_mastery LLM selhal: %s", e)
        return ""

# ROZDELENI_PRIKAZU_V1 — až na konci, viz hlavička
from scripts import hans_study as _hs  # noqa: E402
