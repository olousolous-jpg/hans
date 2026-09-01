"""
HANS_INSTANT_LOOKUP_V1 (4.8.2026) — okamžité dohledání s UZAVŘENOU smyčkou.

Problém: na dotaz o tématu, které Hans nemá v paměti, dosud odpověděl poctivé
„nemám záznam, můžu si to nastudovat" (`hans_recall.knowledge_check_bypass`) a
uživatel čekal na noční studium. Instant read byl 19.7. ZAMÍTNUT, protože
Wikipedia resolution má false positives („Zootropium" → „Zootropic") a zápis
rovnou do paměti = tichá kontaminace.

Tenhle modul ten spor řeší tím, že ROZPOJÍ „odpovědět" a „zapamatovat si":

  1. HNED   — dohledá a odpoví PROVIZORNĚ („podle toho, co jsem právě našel").
              Do deníku/entit/RAG se NEZAPÍŠE NIC. Nález (dotaz + raw_text +
              provizorní odpověď) jde do čekárny `unverified_findings`.
  2. V NOCI — `verify_pending()` ověří, že článek reálně odpovídá dotazu
              (deterministický re-resolve + title-similarity gate + krátký
              EN úsudek). Teprve TEĎ se zapíše do paměti (přes `curiosity._store`
              = deník + entity + RAG). Když neprojde → NEZAPÍŠE se a označí se
              k ranní opravě.
  3. RÁNO   — `unannounced_corrections()` dá `hans_idle` seznam nálezů, které
              neprošly → proaktivní oprava uživateli.

Pravidlo, které tenhle modul drží: **do paměti se nezapisuje nic, co neprošlo
nočním ověřením.** Provizorní odpověď je označená jako provizorní i tónem.

⚠️ DĚLBA PRÁCE MEZI GATY (ověřeno testem, nepřehánět si deterministiku):
  • (a) re-resolve a (b) title-similarity chytí *nestabilní* resolution a
    *hrubý* nesoulad („Architektonické vlivy" → „Sursockovo muzeum").
  • **Near-miss pravopis NEROZLIŠÍ** — „Zootropium" vs „Zootropic" se liší jen
    koncovkou, tedy přesně tak, jak vypadá české skloňování; jakýkoli práh, co
    zamítne tohle, zamítne i „Dadština"/„dadštině". Proto o něm rozhoduje až
    (c) úsudek modelu.
  • Když model není k dispozici (mozek dole, nejednoznačná odpověď), nález
    zůstane *pending* / *rejected* — **nikdy se nezapíše naslepo**. Bezpečnost
    smyčky tedy nestojí na chytrosti gatů, ale na tom, že nerozhodnuto ≠ ano.

Vzor: [[anticonfabulation-guiding-principle]], [[ollama-deferred-processing]],
[[learning-must-close-the-loop]].
"""

from __future__ import annotations

import logging
import re
import sqlite3
import time
from typing import Optional

_log = logging.getLogger(__name__)

# Provizorní odpověď = deterministický rámec (bypass mimo LLM persona vrstvu),
# uvnitř je jen shrnutí z reálného článku. Tón MUSÍ říct „teď jsem to našel",
# ne „vím" — jinak by se z provizorního nálezu stala zdánlivá vzpomínka.
_PROVISIONAL_TMPL = (
    "V paměti jsem o tom nic neměl, %(oslov)s, tak jsem se právě podíval. "
    "Podle toho, co jsem teď našel (%(source)s):\n\n%(summary)s\n\n"
    "Berte to zatím s rezervou — ještě jsem si to neověřil a nezapsal do paměti. "
    "Udělám to v noci a kdyby to nesedělo, ráno se ozvu."
)

_CORRECTION_TMPL = (
    "%(oslov)s, ještě k tomu, na co jste se ptal — „%(topic)s\". "
    "Odpověděl jsem tehdy z toho, co jsem narychlo našel, ale při nočním "
    "ověření to neobstálo (%(reason)s). Do paměti jsem si to proto NEZAPSAL "
    "a raději to neberte jako platné. Když budete chtít, můžu si téma zařadit "
    "do studia — stačí říct „nastuduj %(topic)s\"."
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS unverified_findings (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            REAL,
    asker         TEXT,
    query         TEXT,
    topic         TEXT,
    source        TEXT,
    resolved_title TEXT,
    url           TEXT,
    summary       TEXT,
    raw_text      TEXT,
    status        TEXT DEFAULT 'pending',
    verdict       TEXT,
    verified_ts   REAL,
    announced     INTEGER DEFAULT 0,
    announced_ts  REAL,
    created_ts    REAL
)
"""


def _cfg(config: dict) -> dict:
    return (config or {}).get("instant_lookup", {}) or {}


def ensure_schema(db_path: str) -> None:
    """Idempotentní vytvoření tabulky čekárny."""
    conn = sqlite3.connect(db_path, timeout=5.0)
    try:
        conn.execute(_SCHEMA)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_uf_status "
                     "ON unverified_findings(status)")
        conn.commit()
    finally:
        conn.close()


# ── čekárna: zápis a čtení ───────────────────────────────────────────────────

def add_finding(db_path: str, *, asker: str, query: str, topic: str,
                source: str, resolved_title: str, url: str,
                summary: str, raw_text: str) -> Optional[int]:
    ensure_schema(db_path)
    now = time.time()
    conn = sqlite3.connect(db_path, timeout=5.0)
    try:
        cur = conn.execute(
            "INSERT INTO unverified_findings (ts, asker, query, topic, source,"
            " resolved_title, url, summary, raw_text, status, created_ts) "
            "VALUES (?,?,?,?,?,?,?,?,?, 'pending', ?)",
            (now, asker or "", query or "", topic or "", source or "",
             resolved_title or "", url or "", summary or "", raw_text or "",
             now))
        conn.commit()
        return int(cur.lastrowid)
    except Exception as e:
        _log.warning("instant_lookup: zápis nálezu selhal: %s", e)
        return None
    finally:
        conn.close()


def recent_pending_for_topic(db_path: str, topic: str,
                             max_age_s: float = 86400) -> Optional[dict]:
    """Už na tohle téma čeká nález? (aby opakovaný dotaz netahal článek znovu)"""
    ensure_schema(db_path)
    conn = sqlite3.connect(db_path, timeout=5.0)
    try:
        conn.row_factory = sqlite3.Row
        r = conn.execute(
            "SELECT * FROM unverified_findings WHERE status='pending' "
            "AND LOWER(topic)=LOWER(?) AND ts > ? ORDER BY id DESC LIMIT 1",
            (topic, time.time() - max_age_s)).fetchone()
        return dict(r) if r else None
    except Exception:
        return None
    finally:
        conn.close()


def pending_findings(db_path: str, limit: int = 5) -> list:
    ensure_schema(db_path)
    conn = sqlite3.connect(db_path, timeout=5.0)
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM unverified_findings WHERE status='pending' "
            "ORDER BY id ASC LIMIT ?", (int(limit),)).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        _log.debug("pending_findings: %s", e)
        return []
    finally:
        conn.close()


def _set_status(db_path: str, fid: int, status: str, verdict: str) -> None:
    conn = sqlite3.connect(db_path, timeout=5.0)
    try:
        conn.execute(
            "UPDATE unverified_findings SET status=?, verdict=?, verified_ts=? "
            "WHERE id=?", (status, verdict[:400], time.time(), int(fid)))
        conn.commit()
    except Exception as e:
        _log.warning("instant_lookup: status update selhal: %s", e)
    finally:
        conn.close()


def unannounced_corrections(db_path: str, limit: int = 10) -> list:
    """Zamítnuté nálezy, o kterých uživatel ještě neví (→ ranní oprava)."""
    ensure_schema(db_path)
    conn = sqlite3.connect(db_path, timeout=5.0)
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM unverified_findings WHERE status='rejected' "
            "AND COALESCE(announced,0)=0 ORDER BY id ASC LIMIT ?",
            (int(limit),)).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        _log.debug("unannounced_corrections: %s", e)
        return []
    finally:
        conn.close()


def mark_announced(db_path: str, ids: list) -> None:
    if not ids:
        return
    conn = sqlite3.connect(db_path, timeout=5.0)
    try:
        conn.executemany(
            "UPDATE unverified_findings SET announced=1, announced_ts=? "
            "WHERE id=?", [(time.time(), int(i)) for i in ids])
        conn.commit()
    except Exception as e:
        _log.warning("instant_lookup: mark_announced: %s", e)
    finally:
        conn.close()


_PREDMET = re.compile(r"\bo\s+(.{2,80}?)\s*[?!.]*\s*$",
                      re.IGNORECASE | re.DOTALL)


def _delsi_nazev(topic, query):
    """HANS_ANCHOR_TRUNCATED_NAME_V1 — vrátí delší tvar tématu, když kotva
    usekla víceslovný NÁZEV na první slovo. Jinak None.

    `kotva_tematu` skládá téma z velkých písmen, jenže české názvy mají další
    slova malá. Změřeno 1. 9. na reálných zkouškách:
        „Nemocnice na kraji města" → „Nemocnice"  (dohledal definici špitálu)
        „Jeden svět nestačí"       → „Jeden"
        „Bolek a Lolek"            → „Bolek"
        „Heineken Open 2012"       → „Heineken Open"  (ztratil ročník)
    ⛔ Kotvu samotnou měnit NELZE — sdílí ji `relax_attempts` a umí věci, které
    tenhle predikát ne: nominativ („hradu Trosky" → „hrad Trosky"), závorky
    a věty bez předložky („kdy vzniklo Divadlo Járy Cimrmana?"), kde předmět
    vyjde prázdný. Proto se delší tvar jen ZKUSÍ a rozhodne o něm wiki gate.

    Sahá se sem jen při doloženém useknutí = kotva je PREFIX předmětu na
    hranici slova. „hrad Trosky" × „hradu Trosky" prefix není → beze změny.
    """
    if not topic or not query:
        return None
    m = _PREDMET.search((query or "").strip())
    if not m:
        return None
    pred = m.group(1).strip().rstrip("?!.,;:").strip()
    if not pred or len(pred) <= len(topic):
        return None
    a, b = pred.lower(), topic.strip().lower()
    if a.startswith(b) and a[len(b):len(b) + 1] in (" ", "-"):
        return pred
    return None


def _oslov(asker, config=None) -> str:
    """HANS_FINDINGS_TEST_PERSON_OSLOV_V1 — jak Hansa osloví protějšek v šablonách nálezů.

    ⚠️ Testovací identita NENÍ osoba. `kolac_exam.identita()` vrací technické
    jméno (dnes „zkouška"), které MUSÍ být v `config.test_persons`, aby se
    sebetest nezapsal do deníku ani RAG. Bez tohohle helperu z něj
    `cz_names.address` udělal vokativ a Hans Koláčovi odpovídal
    „…, Zkouško" (10 z 23 zkoušek, doloženo 1. 9.).

    Protahuje se predikát z `HANS_TEST_PERSON_V1`; sdílený helper proto, že
    šablony jsou DVĚ (provizorní odpověď + ranní oprava) a druhá by se na
    kopii kontroly dřív nebo později rozešla ([[test-the-fix-not-the-symptom]]).
    """
    try:
        _tp = [str(x).strip().lower()
               for x in ((config or {}).get("test_persons") or [])]
        if (asker or "").strip().lower() in _tp:
            return "pane"          # neutrální — zkouška má měřit odpověď, ne oslovení
    except Exception:
        pass
    try:
        from scripts.cz_names import address as _addr
        return _addr(asker) if asker else "pane"
    except Exception:
        return (asker or "pane")


def correction_text(row: dict, asker: Optional[str] = None,
                    config: Optional[dict] = None) -> str:
    oslov = _oslov(asker, config)
    return _CORRECTION_TMPL % {
        "oslov": oslov,
        "topic": row.get("topic") or "to téma",
        "reason": row.get("verdict") or "nenašel jsem spolehlivý zdroj",
    }


# ── 1) OKAMŽITÉ DOHLEDÁNÍ ────────────────────────────────────────────────────

def lookup_now(config: dict, db_path: str, topic: str, query: str,
               asker: Optional[str] = None) -> Optional[str]:
    """Dohledej téma HNED a vrať PROVIZORNÍ odpověď (nebo None → volající
    použije poctivé „nemám záznam").

    Do paměti (deník/entity/RAG) NEZAPISUJE NIC — jen do čekárny.
    Deferral-safe: mozek dole / herní mód / článek nenalezen → None.
    """
    c = _cfg(config)
    if not c.get("enabled", True):
        return None
    topic = (topic or "").strip()
    if not topic:
        return None

    # mozek dole → provizorní shrnutí by stejně nevzniklo; nech honest bypass
    try:
        from scripts.ollama_client import brain_available
        if not brain_available(config):
            return None
    except Exception:
        return None

    # už na to čeká nález z dneška → neopakuj stahování ani zápis
    try:
        prev = recent_pending_for_topic(db_path, topic)
    except Exception:
        prev = None
    if prev and (prev.get("summary") or "").strip():
        return _render_provisional(prev, asker, config)

    try:
        from scripts.web_reader import WebReader
        wr = WebReader(config)
        _lang = str(config.get("curiosity", {}).get("wiki_lang", "cs"))
        _max = int(c.get("max_chars", 3000))
        # HANS_ANCHOR_TRUNCATED_NAME_V1 — kotva mohla useknout víceslovný název.
        # O delším tvaru rozhoduje EXISTUJÍCÍ wiki gate, ne nová heuristika:
        # změřeno 1. 9., že název najde („Nemocnice na kraji města", „Bolek
        # a Lolek", „Heineken Open 2012") a popisnou frázi zamítne („Vývoj
        # zbrojnic a jejich dopad…" → None).
        _alt = _delsi_nazev(topic, query)
        if _alt:
            art = wr.wikipedia_article(_alt, lang=_lang, max_chars=_max)
            if not art:
                # Delší tvar článek nemá → dotaz je popisná fráze, ne název.
                # Dohledat podle USEKNUTÉ kotvy je horší než nedohledat nic:
                # doloženo „Vývoj zbrojnic…" → heslo „Vývoj" (definice slova).
                _log.info("HANS_ANCHOR_TRUNCATED_NAME_V1: %r neexistuje a %r je "
                          "useknuté → nedohledávám (raději nic než cizí heslo)",
                          _alt, topic)
                return None
            topic = _alt
        else:
            art = wr.wikipedia_article(topic, lang=_lang, max_chars=_max)
    except Exception as e:
        _log.debug("instant_lookup: fetch selhal: %s", e)
        return None
    if not art or not (art.get("text") or "").strip():
        return None

    summary = _summarize_for_user(config, topic, art)
    if not summary:
        return None  # mozek mimo uprostřed → honest bypass

    row = {
        "topic": topic, "source": "wikipedia",
        "resolved_title": art.get("title") or topic,
        "url": art.get("url") or "", "summary": summary,
    }
    try:
        add_finding(db_path, asker=asker or "", query=query, topic=topic,
                    source="wikipedia", resolved_title=row["resolved_title"],
                    url=row["url"], summary=summary, raw_text=art.get("text") or "")
    except Exception as e:
        _log.warning("instant_lookup: čekárna nezapsala: %s", e)
        return None
    _log.info("instant_lookup: '%s' → '%s' (provizorně, čeká na ověření)",
              topic, row["resolved_title"])
    return _render_provisional(row, asker, config)


def _render_provisional(row: dict, asker: Optional[str],
                        config: Optional[dict] = None) -> str:
    oslov = _oslov(asker, config)
    src = row.get("resolved_title") or "Wikipedie"
    summary = (row.get("summary") or "").strip()
    # Heslo se nejmenuje jako dotaz → řekni to ROVNOU, ať to uživatel pozná sám
    # a nemusí čekat na noční ověření. Doloženo živě: „Zootropium" → heslo
    # „Zootropic" (přes redirect článek o heterotrofech) = úplně jiná věc.
    if not _titles_align(row.get("topic") or "", src):
        summary += ("\n\n(Poznámka: heslo přesně na „%s\" jsem nenašel, tohle je "
                    "nejbližší nález „%s\" — může jít o něco úplně jiného.)"
                    % (row.get("topic") or "", src))
    return _PROVISIONAL_TMPL % {
        "oslov": oslov,
        "source": "Wikipedie — heslo „%s\"" % src,
        "summary": summary,
    }


def _titles_align(topic: str, title: str) -> bool:
    """Sedí název hesla na dotaz aspoň hrubě? (jen pro varování v odpovědi —
    o platnosti nálezu rozhoduje až noční ověření, viz docstring modulu.)"""
    def _fold(s: str) -> str:
        import unicodedata
        s = unicodedata.normalize("NFKD", (s or "").lower())
        return "".join(ch for ch in s if not unicodedata.combining(ch))
    a, b = _fold(topic), _fold(title)
    if not a or not b:
        return True
    return a in b or b in a


def _summarize_for_user(config: dict, topic: str, art: dict) -> Optional[str]:
    """Shrnutí článku HANS-CZECH modelem (rezidentní, žádný VRAM handoff).

    ⚠️ ZÁMĚRNĚ NEPOUŽÍVÁ `web_reader._summarize` — ten jede na BASE modelu
    (8 GB) a v interaktivním chatu by evictoval rezidentní hans-czech
    (8+8 > 16 GB VRAM) = thrashing uprostřed rozhovoru. Viz [[ollama-vram-tiers]],
    [[study-vram-handoff]].
    """
    c = _cfg(config)
    model = str((config.get("hans_dialog", {}) or {}).get(
        "ollama_model", "hans-czech:latest"))
    text = (art.get("text") or "")[:int(c.get("max_chars", 3000))]
    prompt = (
        "Níže je text encyklopedického článku „%s\". Shrň ve 2 až 4 větách, "
        "co je téma „%s\" — nejdřív jednou větou KDO/CO to je, pak konkrétní "
        "fakta z textu. Drž se VÝHRADNĚ textu, nic si nepřidávej. Bez uvozovek.\n\n"
        "Text:\n%s" % (art.get("title") or topic, topic, text))
    try:
        from scripts.ollama_client import ollama_chat
        out = ollama_chat(
            model,
            [{"role": "user", "content": prompt}],
            config=config,
            timeout=int(c.get("llm_timeout", 45)),
            options={"num_predict": int(c.get("num_predict", 220)),
                     "num_ctx": int(c.get("num_ctx", 4096)),
                     "temperature": 0.2},
        )
    except Exception as e:
        _log.debug("instant_lookup: summarize: %s", e)
        return None
    out = (out or "").strip()
    return out or None


# ── 2) NOČNÍ OVĚŘENÍ ─────────────────────────────────────────────────────────

def verify_pending(config: dict, db_path: str, curiosity=None,
                   limit: Optional[int] = None) -> str:
    """Ověř čekající nálezy. Vrací kód: 'deferred' | 'idle' | 'done:N/M'.

    Ověřené → TEPRVE TEĎ se zapíšou do paměti. Neověřené → zůstanou mimo paměť
    a jdou do ranní opravy. Deferral-safe: mozek dole → 'deferred' (nic se
    nezmění, zkusí se příště).
    """
    c = _cfg(config)
    if not c.get("enabled", True):
        return "idle"
    rows = pending_findings(db_path, limit=int(limit or c.get("verify_batch", 5)))
    if not rows:
        return "idle"
    try:
        from scripts.ollama_client import brain_available
        if not brain_available(config):
            return "deferred"
    except Exception:
        return "deferred"

    ok = 0
    for row in rows:
        try:
            verdict_ok, reason = _verify_one(config, row)
        except Exception as e:
            _log.warning("instant_lookup: ověření '%s' selhalo: %s",
                         row.get("topic"), e)
            continue  # nech pending → zkusí se příště
        if verdict_ok is None:
            return "deferred"  # mozek spadl uprostřed dávky
        if verdict_ok:
            if _commit_to_memory(config, row, curiosity):
                _set_status(db_path, row["id"], "verified", reason)
                ok += 1
                _log.info("instant_lookup: OVĚŘENO '%s' → zapsáno do paměti",
                          row.get("topic"))
            else:
                _log.warning("instant_lookup: '%s' ověřeno, ale zápis do paměti "
                             "selhal — zůstává pending", row.get("topic"))
        else:
            _set_status(db_path, row["id"], "rejected", reason)
            _log.info("instant_lookup: ZAMÍTNUTO '%s' (%s) → ranní oprava",
                      row.get("topic"), reason)
    return "done:%d/%d" % (ok, len(rows))


def _verify_one(config: dict, row: dict):
    """(True|False|None, důvod). None = mozek dole → neroz­hodnuto."""
    topic = (row.get("topic") or "").strip()
    title = (row.get("resolved_title") or "").strip()
    c = _cfg(config)

    # (a) deterministicky: opakuj resolution — musí vyjít TÝŽ článek
    try:
        from scripts.web_reader import WebReader, _title_similarity
        wr = WebReader(config)
        again = wr._wikipedia_search(
            topic, str(config.get("curiosity", {}).get("wiki_lang", "cs")))
    except Exception as e:
        _log.debug("instant_lookup verify search: %s", e)
        again, _title_similarity = None, None
    if again and title and again.strip().lower() != title.lower():
        return False, ("hledání téhož dotazu teď vede na jiné heslo („%s\" "
                       "místo „%s\")" % (again, title))

    # (b) přísnější title-similarity než při čtení (tam 0.6)
    try:
        sim = _title_similarity(topic, title) if _title_similarity else 1.0
    except Exception:
        sim = 1.0
    min_sim = float(c.get("verify_min_similarity", 0.75))
    substring = topic.lower() in title.lower() or title.lower() in topic.lower()
    if not substring and sim < min_sim:
        return False, ("název hesla „%s\" neodpovídá dost přesně dotazu „%s\""
                       % (title, topic))

    # (c) krátký úsudek modelu — ANGLICKY (vzor [[reasoning-tier-when-to-use]]),
    #     na BASE modelu uvnitř VRAM handoffu (v noci je hans-czech odpojený).
    return _llm_judge(config, topic, title, row.get("raw_text") or "")


def _llm_judge(config: dict, topic: str, title: str, raw_text: str):
    """YES/NO: popisuje článek reálně to, na co se uživatel ptal?"""
    c = _cfg(config)
    model = str(c.get("judge_model")
                or (config.get("hans_dialog", {}) or {}).get(
                    "ollama_model", "hans-czech:latest"))
    prompt = (
        "A user asked about the topic: \"%s\".\n"
        "An encyclopedia article titled \"%s\" was retrieved. Its beginning:\n"
        "---\n%s\n---\n"
        "Question: does this article actually describe the thing the user asked "
        "about? Answer with a single word: YES or NO.\n"
        "Answer NO if the article is about a different (merely similar-sounding "
        "or broader) subject." % (topic, title, raw_text[:900]))
    try:
        from scripts.ollama_client import ollama_chat
        out = ollama_chat(
            model, [{"role": "user", "content": prompt}], config=config,
            timeout=int(c.get("judge_timeout", 90)),
            options={"num_predict": 8, "temperature": 0.0})
    except Exception as e:
        _log.debug("instant_lookup judge: %s", e)
        return None, "úsudek se nepodařilo získat"
    if out is None:
        return None, "mozek nedostupný"
    ans = (out or "").strip().upper()
    if ans.startswith("YES") or ans.startswith("ANO"):
        return True, "ověřeno (heslo „%s\" odpovídá dotazu)" % title
    if ans.startswith("NO") or ans.startswith("NE"):
        return False, ("heslo „%s\" popisuje něco jiného, než na co jste se ptal"
                       % title)
    # nejednoznačná odpověď = NEověřeno (radši opatrně)
    return False, "ověření nebylo jednoznačné"


def _commit_to_memory(config: dict, row: dict, curiosity) -> bool:
    """Zápis do paměti AŽ PO ověření — přes `curiosity._store` (deník + entity
    + RAG + synthesis hook), ať je nález nerozeznatelný od běžného čtení."""
    if curiosity is None:
        _log.debug("instant_lookup: curiosity není napojená → nelze zapsat")
        return False
    try:
        from scripts.web_reader import ReadResult
        res = ReadResult(
            source="wikipedia",
            title=row.get("resolved_title") or row.get("topic") or "",
            url=row.get("url") or "",
            raw_text=row.get("raw_text") or "",
            summary=row.get("summary") or "",
            topic=row.get("topic") or "dotaz",
            pending=False,
        )
        curiosity._store(res)
        return True
    except Exception as e:
        _log.warning("instant_lookup: commit do paměti selhal: %s", e)
        return False


# ── údržba ───────────────────────────────────────────────────────────────────

def purge_old(db_path: str, keep_days: int = 30) -> int:
    """Uklidí staré vyřízené nálezy (pending se NEMAŽE — čeká na mozek)."""
    ensure_schema(db_path)
    conn = sqlite3.connect(db_path, timeout=5.0)
    try:
        cur = conn.execute(
            "DELETE FROM unverified_findings WHERE status IN "
            "('verified','rejected') AND COALESCE(verified_ts,0) < ? "
            "AND COALESCE(announced,0)=1",
            (time.time() - keep_days * 86400,))
        conn.commit()
        return cur.rowcount or 0
    except Exception:
        return 0
    finally:
        conn.close()
