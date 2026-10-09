"""Funkce přesunuté z `scripts/hans_study.py` (ROZDELENI_PRIKAZU_V1).

Text funkcí je beze změny; jména původního modulu se čtou přes `_hs.` až při
volání. Původní modul si funkce bere zpět importem, takže dosavadní importy platí.
Import původního modulu je na KONCI souboru (kruhový import oběma směry).
"""
from __future__ import annotations

from typing import List, Optional
import json
import re
import sqlite3
import time

def _already_covered(topic: str, studied_norms) -> Optional[str]:
    """HANS_STUDY_NEAR_DUP_V1 — je téma už POKRYTÉ existujícím programem? Vrátí
    norm shodného programu, nebo None. Kryje přesnou shodu I blízká synonyma:
    když se významná slova jednoho tématu plně kryjí s druhým (jedno je
    podmnožina druhého), je to překryv („design" ⊂ „web design", „grafický
    design" ⊃ „design") → nezakládej duplicitní program, jen by se přestudovalo
    totéž. Konzervativní: musí jít o ÚPLNé krytí významných slov, ne jen průnik."""
    tn = _hs._norm(topic)
    nt = _hs._sig_tokens(topic)
    for sn in studied_norms:
        if sn == tn:
            return sn
        st = _hs._sig_tokens(sn)
        if nt and st and (nt <= st or st <= nt):
            return sn
    return None


def already_studied(topic: str, db_path: str = "data/hans_diary.db"):
    """HANS_STUDY_KNOWN_TOPIC_V1 (6.8.) — studoval už Hans tohle téma?

    Vrací (co_to_pokrývá, celé_téma_programu) nebo None. Kryje DVĚ úrovně:
      • celý program        („hrady a historická architektura")
      • DOKONČENÉ pod-téma  („Křižácké hrady v Levantě")
    Druhá úroveň je ta podstatná — `_already_covered` uměl jen programy,
    kdežto uživatel se ptá právě na pod-témata.

    PROČ: 6.8. 08:48–09:06 Hans **5× po sobě** nabídl „mám si to nastudovat?"
    na témata, která má odškrtnutá jako hotová (`/studium` hlásilo 12 z 12).
    Pravidlo proti tomu v router promptu JE (`HANS_CHAT_STUDY_BRIDGE_V1`)
    a nefunguje; regexový guard `_looks_like_recall` má díry ve vzorech
    („co mi můžeš říct o…", „pověz mi něco o…"). Odpověď na to není další
    fráze v seznamu, ale **ověření proti datům** — nezávislé na formulaci.
    """
    nt = _hs._sig_tokens(topic)
    if not nt:
        return None
    ns = _hs._stem_tokens(topic)
    conn = None
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True, timeout=5)
        # Tahle funkce čte DB READ-ONLY a NEVOLÁ `_init_db`, takže sloupec
        # `skipped_idx` tu ještě nemusí být (starší DB, Hans běží na starším
        # kódu). Bez fallbacku spadl celý dotaz a `already_studied` vracelo
        # None na VŠECHNO — tichá regrese chycená testem 6.8.
        _sql = ("SELECT topic, curriculum, current_index, %s "
                "FROM study_program WHERE status IN ('active','completed')")
        try:
            rows = conn.execute(_sql % "COALESCE(skipped_idx,'[]')").fetchall()
        except sqlite3.OperationalError:
            rows = conn.execute(_sql % "'[]'").fetchall()
    except Exception as e:
        _hs._log.debug("already_studied: %s", e)
        return None
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    for prog_topic, curriculum, idx, skipped_raw in rows:
        # HANS_STUDY_SKIPPED_MARK_V1 — přeskočené pod-téma NENÍ nastudované;
        # kdyby se počítalo jako pokryté, Hans by odmítl nabídnout studium
        # tématu, které reálně nestudoval.
        try:
            skipped = set(json.loads(skipped_raw or "[]"))
        except Exception:
            skipped = set()
        if _hs._already_covered(topic, [_hs._norm(prog_topic or "")]):
            return (prog_topic, prog_topic)
        try:
            subs = json.loads(curriculum or "[]")
        except Exception:
            subs = []
        # JEN dokončená pod-témata (před current_index) — na nenastudované
        # se studium nabídnout SMÍ, to je legitimní.
        for _i, sub in enumerate(subs[:max(0, int(idx or 0))]):
            if _i in skipped:
                continue
            st = _hs._sig_tokens(sub)
            if st and (nt <= st or st <= nt):
                return (sub, prog_topic)
            # Podmnožina je na češtinu moc přísná: „byzantská vojenská
            # TECHNIKA" × „Byzantská vojenská ARCHITEKTURA" se plně nekryjí,
            # a přesto jde o totéž pod-téma. Práh DVĚ shodná významová slova
            # (po ustřižení koncovky) — jedno by bralo i „hrady ve východních
            # Čechách" jako pokryté, což by bylo špatně (to nestudoval).
            if len(ns & _hs._stem_tokens(sub)) >= 2:
                return (sub, prog_topic)
    return None


def je_reakce_na_navrh(zprava: str) -> bool:
    """HANS_DEEPEN_FEEDBACK_GATE_V1 — smí tahle věta k LLM klasifikátoru?

    PROČ (22.8.): dokud leží návrh na prohloubení, posílala se klasifikátoru
    (SCHVALUJE/ZAMITA/KRITIZUJE/NIC) KAŽDÁ zpráva, která nemá tvar otázky —
    a ten si verdikt vymyslel. Doloženo: „rekni vice o zameckem parku u hradu
    Kost" → „Beru tvou kritiku, pane — prohloubím studium Český ráj". Návrh
    přitom vznikl v 00:30 v tichém okně a uživateli nikdy nedorazil.
    Předchůdce `HANS_DEEPEN_QUESTION_GUARD_V1` (19.8.) zavíral jen tázací
    věty — díra byla ve všem ostatním, což je většina hovoru.

    Rozhodnutí je OTOČENÉ: neptáme se „je to otázka?", ale „nese to vůbec
    souhlas, nesouhlas nebo kritiku?". Odpověď na návrh je krátká reakce,
    ne věta s vlastním dotazem.

    Změřeno na 500 skutečných uživatelských replikách z deníku: ke
    klasifikátoru projde 18 (3,6 %) — samé „ano/ne/ne, děkuji" —, kdežto
    dosavadní pravidlo pouštělo všechno kromě otázek. Osm reálných
    formulací souhlasu/zamítnutí/kritiky („ano", „ne, nech to být",
    „to je slabé, dodělej k tomu víc", …) projde dál.
    """
    m = (zprava or "").strip()
    if not m or not _hs._FB_RE.search(m):
        return False
    if "?" in m or _hs._OTAZKA_RE.match(m):
        return False        # věta s vlastním dotazem není odpověď na návrh
    # HANS_DEEPEN_ZAVER_NENI_SOUHLAS_V1 — rozloučení/poděkování bez výslovného
    # verdiktu je konec hovoru, ne schválení. Viz komentář u `_ZAVER_RE`.
    if _hs._ZAVER_RE.search(m) and not _hs._SILNY_VERDIKT_RE.search(m):
        return False
    return len(m.split()) <= _hs._FB_MAX_SLOV


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


def _normalize_subtopic(s: str) -> str:
    """HANS_STUDY_CANON_TITLE_V1 — srazí popisnou větu na kanonický pojem, aby
    ji encyklopedie našla (malý model prompt ignoruje → dorovnat kódem).
    Useknout za dvojtečkou/pomlčkou, pryč '(…)' vysvětlení, ≤6 slov."""
    s = (s or "").strip()
    s = re.split(r"[:–—]", s, 1)[0]                # useknout ": popis" / " – popis"
    s = re.sub(r"\s*\([^)]*\)", "", s)           # pryč "(např. …)"
    s = re.sub(r"\s+", " ", s).strip(" .,;–-")
    w = s.split()
    if len(w) > 6:
        s = " ".join(w[:6])
    return s.strip()


def _findable(config: dict, w, sub: str, topic: str) -> bool:
    """Najde encyklopedie k pod-tématu vůbec nějaký článek? (včetně kotvy)"""
    lang = str(_hs._cfg(config).get("wiki_lang", "cs"))
    for q in _hs._search_queries(sub, topic):
        if w.last_transient:
            return True                      # výpadek → netvrď, že to nejde
        try:
            if w._wikipedia_search(q, lang):
                return True
        except Exception:
            return True                      # neznámo → radši ponech
    try:
        hit = bool(_hs._anchor_pick(config, w, sub, topic, lang, set()))
    except Exception:
        return True
    # Výpadek mohl přijít až u POSLEDNÍHO dotazu nebo uvnitř kotvy — pak „nenašel
    # jsem" znamená „nemohl jsem hledat". Nikdy z toho nedělej „neexistuje";
    # volající si podle `last_transient` stejně vyžádá kurikulum beze změny.
    return True if (not hit and w.last_transient) else hit


def _validate_curriculum(config: dict, subs: list, topic: str) -> list:
    """HANS_STUDY_CURRICULUM_VALIDATE_V1 (4.8.) — ověř pod-témata UŽ PŘI
    GENEROVÁNÍ, ne až třemi nocemi stání na každém.

    Model si vymýšlí názvy, které encyklopedie nezná („Geologie Českého ráje",
    „Folklór Českého ráje"): study je pak zkouší 3 noci, než je přeskočí — u
    programu z 4.8. se neresolvovalo 5 z 8 pod-témat. Tady se každé jednou
    ověří; co neprojde, dostane šanci na KANONICKOU náhradu od modelu, a ta
    se ověřuje TOUTÉŽ kontrolou (návrh se nikdy nebere na slovo).

    ⚠️ POJISTKA: když je encyklopedie dočasně dole (HTTP 429/5xx), validace se
    NEPROVÁDÍ a kurikulum se vrátí BEZE ZMĚNY. Jinak by jeden rate-limit
    prohlásil všechna pod-témata za nedohledatelná a kurikulum zdecimoval —
    tichá ztráta by byla horší než chyba, kterou léčíme.
    """
    c = _hs._cfg(config)
    if not c.get("validate_curriculum", True) or not subs:
        return subs
    try:
        from scripts.web_reader import WebReader
        w = WebReader(config)
    except Exception:
        return subs
    delay = float(c.get("validate_delay_s", 0.7))
    good, bad = [], []
    for s in subs:
        if _hs._findable(config, w, s, topic):
            good.append(s)
        else:
            bad.append(s)
        if w.last_transient:
            _hs._log.info("study: validace kurikula přerušena (encyklopedie dole) "
                      "— beru návrh '%s' beze změny", topic)
            return subs
        if delay:
            time.sleep(delay)
    if not bad:
        _hs._log.info("study: kurikulum '%s' — všech %d pod-témat dohledatelných",
                  topic, len(good))
        return subs
    _hs._log.info("study: kurikulum '%s' — %d z %d pod-témat encyklopedie nezná: %s",
              topic, len(bad), len(subs), "; ".join(bad))
    fixed = _hs._repair_subtopics(config, w, bad, topic) if c.get(
        "validate_repair", True) else {}
    out, seen = [], set()
    for s in subs:                            # zachovej PŮVODNÍ pořadí studia
        cand = s if s in good else fixed.get(s)
        if not cand:
            _hs._log.info("study: pod-téma '%s' VYPUŠTĚNO z kurikula "
                      "(encyklopedie ho nezná a náhrada se nenašla)", s)
            continue
        if _hs._norm(cand) in seen:
            continue
        seen.add(_hs._norm(cand))
        out.append(cand)
    if not out:                               # radši původní než prázdné
        return subs
    return out


def _repair_subtopics(config: dict, w, bad: list, topic: str) -> dict:
    """Jedním LLM voláním navrhni kanonické náhrady; vrať jen ty OVĚŘENÉ."""
    lang = str(_hs._cfg(config).get("wiki_lang", "cs"))
    try:
        from scripts.ollama_client import ollama_generate
        raw = ollama_generate(
            model=_hs._model(config),
            prompt=("Studijní téma: %s\n\nPod-témata bez článku:\n%s\n\nJSON:"
                    % (topic, "\n".join("- %s" % b for b in bad))),
            system=_hs._REPAIR_SYSTEM, config=config,
            timeout=int(_hs._cfg(config).get("llm_timeout", 300)),
            keep_alive=0, options={"temperature": 0.2})
    except Exception as e:
        _hs._log.debug("_repair_subtopics LLM: %s", e)
        return {}
    if not raw:
        return {}
    try:
        m = re.search(r"\{.*\}", raw, re.S)
        proposals = json.loads(m.group(0)) if m else {}
    except Exception:
        return {}
    out = {}
    for orig in bad:
        cand = _hs._normalize_subtopic(str(proposals.get(orig, "") or "").strip())
        if len(cand) < 3 or _hs._norm(cand) == _hs._norm(orig):
            continue
        # návrh modelu se NIKDY nebere na slovo — projde touž kontrolou
        if _hs._findable(config, w, cand, topic):
            out[orig] = cand
            _hs._log.info("study: pod-téma '%s' → '%s' (ověřená náhrada)", orig, cand)
        else:
            _hs._log.info("study: náhrada '%s' za '%s' taky nedohledatelná — "
                      "zahazuji", cand, orig)
        if w.last_transient:
            break
    return out


def _generate_curriculum(config: dict, topic: str, examples: list) -> list:
    """Base LLM vygeneruje uspořádané kurikulum pod-témat. [] při selhání."""
    n = int(_hs._cfg(config).get("curriculum_size", 8))
    n = max(4, min(12, n))
    ex = ", ".join(str(e) for e in (examples or [])[:8])
    prompt = (f"Koníček: {topic}\n"
              f"Příklady, které pod něj spadají: {ex or '(žádné)'}\n\n"
              f"Sestav kurikulum {n} pod-témat v pořadí ke studiu.")
    timeout = int(_hs._cfg(config).get("llm_timeout", 300))
    try:
        from scripts.ollama_client import ollama_generate
        from scripts.hans_persona import persona_name as _pn
    except ImportError:
        _hs._log.warning("_generate_curriculum: moduly nedostupné, skip")
        return []
    try:
        system = _hs._CURRICULUM_SYSTEM.format(persona_name=_pn(config), n=n)
        raw = ollama_generate(model=_hs._model(config), prompt=prompt, system=system,
                              config=config, timeout=timeout,
                              keep_alive=0,  # MODEL_KEEPALIVE_TIERS_V1
                              options={"temperature": 0.3})
    except Exception as e:
        _hs._log.warning("_generate_curriculum LLM selhal: %s", e)
        return []
    items = _hs._parse_json_list(raw)
    out = []
    seen = set()
    for it in items:
        s = str(it).strip().lstrip("0123456789.) -").strip()
        s = _hs._normalize_subtopic(s)  # HANS_STUDY_CANON_TITLE_V1
        if len(s) >= 3 and _hs._norm(s) not in seen:
            out.append(s)
            seen.add(_hs._norm(s))
    # HANS_STUDY_CURRICULUM_VALIDATE_V1 — ověř dohledatelnost HNED, ať se
    # nedohledatelný název nevleče 3 nocemi stání (viz `_validate_curriculum`).
    return _hs._validate_curriculum(config, out[:n], topic)


def add_pending_topic(diary_db_path: str, topic: str) -> str:
    """HANS_AGENT_V1 — zařadí téma z chatu do studijní fronty (status='pending').
    Aktivuje se v ensure_program s PŘEDNOSTÍ před durable koníčky, jakmile
    dokončí současný program. Idempotentní (topic_norm ve všech stavech).
    Vrací 'added' | 'exists' | 'error'."""
    t = (topic or "").strip()
    if len(t) < 2:
        return "error"
    tn = _hs._norm(t)
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
            if _hs._already_covered(t, norms):
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
        _hs._log.warning("add_pending_topic: %s", e)
        return "error"

# ROZDELENI_PRIKAZU_V1 — až na konci, viz hlavička
from scripts import hans_study as _hs  # noqa: E402
