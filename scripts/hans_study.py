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
  id, topic, curriculum(JSON), current_index, status, sessions_done,
  started_ts, updated_ts, last_session_ts

STAVY PROGRAMU (HANS_STUDY_UNIFY_V1, 18.8. — dřív tu stálo `abandoned`, které
v kódu NIKDY neexistovalo; `pending`/`blocked` naopak chyběly):
  active    — běží; `get_active_program` bere NEJSTARŠÍ (HANS_STUDY_SEQUENTIAL_V1)
  completed — kurikulum dojeté + mistrovská reflexe
  pending   — téma čeká na aktivaci (chat/agent přes `add_pending_topic`)
  blocked   — 3× se nepodařilo vygenerovat kurikulum (HANS_STUDY_PENDING_STUCK_V1)
Druhá tabulka v tomhle modulu: `deepen_proposals` (pending|approved|rejected|
expired) — návrhy na prohloubení dokončeného programu.

WIRING — TŘI vstupy, ne jeden (docstring dřív sliboval „1 session/noc"):
  1. noční okno `hans_routine._in_night_window()` = **22:00–06:00** (ne 2–6),
  2. brain_up catchup `hans_routine.study_catchup_async()` — 1×/den, když
     noční okno vyšlo naprázdno (HANS_STUDY_BRAIN_UP_CATCHUP_V1),
  3. ruční `/studium teď` z chatu (`chat_commands`).
Denní guard `_last_study_date` je PERZISTENTNÍ (`_save_routine_state`), takže
restart Hanse den neodemkne; po `deferred` se ZÁMĚRNĚ nenastaví → retry.
LLM části (kurikulum/poznámka/syntéza) běží v noci na base modelu keep_alive=0
(anti-konfabulace + VRAM tier; [[ollama-vram-tiers]]). Deferral-safe.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import time
from typing import List, Optional

# ── HANS_STUDY_UNIFY_V1 (18.8.) — JEDNA PRAVDA O VÝSLEDCÍCH ─────────────────
# Kódy se dřív porovnávaly řetězcem na třech místech a každé mělo jiný názor:
# docstring `run_study_session` jmenoval 4 kódy, reálně jich vzniká 6, routine
# testovala `!= "deferred"` a chat neznal `skipped` (uživatel se o něm nedozvěděl,
# ačkoli přesně kvůli tomu HANS_STUDY_NUDGE_V1 vznikl). Predikáty níž jsou
# jediné místo, kde se význam kódu rozhoduje.
RESULT_STUDIED   = "studied"     # nastudováno pod-téma
RESULT_COMPLETED = "completed"   # kurikulum dojeté (+ mistrovská reflexe)
RESULT_SKIPPED   = "skipped"     # pod-téma po max_subtopic_failures přeskočeno
RESULT_NOREAD    = "noread"      # k pod-tématu se nenašlo čtení (fail_count++)
RESULT_IDLE      = "idle"        # není co studovat / vypnuto
RESULT_DEFERRED  = "deferred"    # transientní výpadek (LLM/wiki) → retry

#: Kódy, po kterých se NESMÍ zapálit denní guard — nic se nestalo, zkus znovu.
_TRANSIENT = {RESULT_DEFERRED}
#: Kódy, kde se program pohnul kupředu (index nebo znalost) — `skipped` ANO,
#: protože kurikulum postoupilo na další pod-téma.
_PROGRESS = {RESULT_STUDIED, RESULT_COMPLETED, RESULT_SKIPPED}
#: Kódy, kde session opravdu NĚCO PŘINESLA. `skipped` schválně NE: přeskočení
#: mrtvého pod-tématu je pohyb, ale ne znalost — a kdyby se počítalo jako
#: úspěch, program, který jen přeskakuje, by rozvrhovému auditu hlásil „ok"
#: každou noc a slepé místo (HANS_SCHEDULE_LAST_OK_V1) by se vrátilo jinými
#: dveřmi. Proto má audit vlastní, PŘÍSNĚJŠÍ predikát.
_KNOWLEDGE = {RESULT_STUDIED, RESULT_COMPLETED}


def is_transient(code: str) -> bool:
    """True = přechodné selhání → guard nenastavovat, zkusit znovu."""
    return (code or "") in _TRANSIENT


def made_progress(code: str) -> bool:
    """True = session posunula program (nastudováno / dojeto / pod-téma
    přeskočeno). `noread` a `idle` progres NEJSOU — program stojí na místě."""
    return (code or "") in _PROGRESS


def produced_knowledge(code: str) -> bool:
    """True = session přinesla ZNALOST (studied/completed). Tohle chce
    rozvrhový audit — viz komentář u `_KNOWLEDGE`."""
    return (code or "") in _KNOWLEDGE

_log = logging.getLogger("hans_study")

_WS = re.compile(r"\s+")


def _norm(s: str) -> str:
    return _WS.sub(" ", (s or "").strip().lower())


def _sig_tokens(s: str) -> set:
    """Významná slova tématu (>2 znaky) pro měkký dedup blízkých témat."""
    return {w for w in _norm(s).split() if len(w) > 2}


def _stem_tokens(s: str) -> set:
    """Významná slova zkrácená na kmen — české koncovky bez slovníku."""
    import unicodedata
    out = set()
    for w in _norm(s).split():
        w = "".join(c for c in unicodedata.normalize("NFKD", w)
                    if not unicodedata.combining(c))
        if len(w) > 2:
            out.add(w[:5] if len(w) >= 6 else w)
    return out


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
    nt = _sig_tokens(topic)
    if not nt:
        return None
    ns = _stem_tokens(topic)
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
        _log.debug("already_studied: %s", e)
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
        if _already_covered(topic, [_norm(prog_topic or "")]):
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
            st = _sig_tokens(sub)
            if st and (nt <= st or st <= nt):
                return (sub, prog_topic)
            # Podmnožina je na češtinu moc přísná: „byzantská vojenská
            # TECHNIKA" × „Byzantská vojenská ARCHITEKTURA" se plně nekryjí,
            # a přesto jde o totéž pod-téma. Práh DVĚ shodná významová slova
            # (po ustřižení koncovky) — jedno by bralo i „hrady ve východních
            # Čechách" jako pokryté, což by bylo špatně (to nestudoval).
            if len(ns & _stem_tokens(sub)) >= 2:
                return (sub, prog_topic)
    return None


# ── HANS_DEEPEN_FEEDBACK_GATE_V1 (22.8.) ────────────────────────────────────
# Reakce na návrh prohloubení: schvaluje / zamítá / dává vlastní kritiku.
# ⚠️ Slovník je schválně ÚZKÝ. Širší (např. „chybí", „málo", „povrchní") by
# nabral běžný hovor, a chyba tímhle směrem je dražší: falešné SCHVALUJE
# reaktivuje DOKONČENÝ program (completed → active) a přeskládá studijní
# frontu, kdežto přehlédnutá kritika stojí jen jedno zopakování nebo
# „/prohloubit <kritika>".
# `schval(?!n)` — bez lookaheadu bere „schválně" (doloženo na reálné větě
# „schvalne jestli vic k cemu slouzil na hrade prevét").
_FB_RE = re.compile(
    r"\b(ano|jo|souhlas\w*|schval(?!n)\w*|dob[řr]e|prohlub|prohloub|"
    r"ne\b|nechci|nesouhlas\w*|zru[šs]|nech\s+to|špatn\w*|"
    r"slab\w*|m[ěe]l\s+bys|douč|dodělej)\b", re.I)

_OTAZKA_RE = re.compile(
    r"^\s*(kdo|co|kde|kdy|jak|pro[čc]|kolik|kter|[čc][íi]|zn[áa]|"
    r"vid[íi]|um[íi][šs]|m[áa][šs]|je\s|jsi\s|byl\s)", re.I)

_FB_MAX_SLOV = 10


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
    if not m or not _FB_RE.search(m):
        return False
    if "?" in m or _OTAZKA_RE.match(m):
        return False        # věta s vlastním dotazem není odpověď na návrh
    return len(m.split()) <= _FB_MAX_SLOV


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
    "nesmysly.\n\n"
    "PRAVIDLA PRO NÁZVY POD-TÉMAT (jinak encyklopedie nenajde článek):\n"
    "  1. MAX 5 slov (kratší lepší)\n"
    "  2. ŽÁDNÉ dvojtečky, středníky ani závorky s vysvětlením/'(např. …)'\n"
    "  3. Kanonický pojem, ne popisná věta\n"
    "  ŠPATNĚ: 'Gotická architektura: charakteristika a příklady (např. …)'\n"
    "  SPRÁVNĚ: 'Gotická architektura'\n\n"
    "Vrať VÝHRADNĚ JSON pole {n} řetězců (názvy pod-témat), nic víc."
)


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


_REPAIR_SYSTEM = (
    "Jsi knihovník. Dostaneš názvy studijních pod-témat, ke kterým encyklopedie "
    "NEMÁ článek. Ke KAŽDÉMU navrhni KANONICKÝ NÁZEV HESLA, pod kterým tu látku "
    "encyklopedie skutečně vede — tedy existující pojem, ne opis.\n"
    "PRAVIDLA: max 4 slova; žádné dvojtečky/závorky; zachovej PŘEDMĚT pod-tématu "
    "(nenahrazuj ho něčím jiným).\n"
    "Když tě k danému pod-tématu NIC věrohodného nenapadá, vrať prázdný řetězec — "
    "to je LEPŠÍ než vymyšlený název.\n"
    "Příklad: „Geologie Českého ráje\" → „Geopark Český ráj\"; "
    "„Románské stavebnictví\" → „Románská architektura\".\n"
    "Vrať VÝHRADNĚ JSON objekt {\"původní název\": \"navržené heslo\", …}."
)


def _findable(config: dict, w, sub: str, topic: str) -> bool:
    """Najde encyklopedie k pod-tématu vůbec nějaký článek? (včetně kotvy)"""
    lang = str(_cfg(config).get("wiki_lang", "cs"))
    for q in _search_queries(sub, topic):
        if w.last_transient:
            return True                      # výpadek → netvrď, že to nejde
        try:
            if w._wikipedia_search(q, lang):
                return True
        except Exception:
            return True                      # neznámo → radši ponech
    try:
        hit = bool(_anchor_pick(config, w, sub, topic, lang, set()))
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
    c = _cfg(config)
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
        if _findable(config, w, s, topic):
            good.append(s)
        else:
            bad.append(s)
        if w.last_transient:
            _log.info("study: validace kurikula přerušena (encyklopedie dole) "
                      "— beru návrh '%s' beze změny", topic)
            return subs
        if delay:
            time.sleep(delay)
    if not bad:
        _log.info("study: kurikulum '%s' — všech %d pod-témat dohledatelných",
                  topic, len(good))
        return subs
    _log.info("study: kurikulum '%s' — %d z %d pod-témat encyklopedie nezná: %s",
              topic, len(bad), len(subs), "; ".join(bad))
    fixed = _repair_subtopics(config, w, bad, topic) if c.get(
        "validate_repair", True) else {}
    out, seen = [], set()
    for s in subs:                            # zachovej PŮVODNÍ pořadí studia
        cand = s if s in good else fixed.get(s)
        if not cand:
            _log.info("study: pod-téma '%s' VYPUŠTĚNO z kurikula "
                      "(encyklopedie ho nezná a náhrada se nenašla)", s)
            continue
        if _norm(cand) in seen:
            continue
        seen.add(_norm(cand))
        out.append(cand)
    if not out:                               # radši původní než prázdné
        return subs
    return out


def _repair_subtopics(config: dict, w, bad: list, topic: str) -> dict:
    """Jedním LLM voláním navrhni kanonické náhrady; vrať jen ty OVĚŘENÉ."""
    lang = str(_cfg(config).get("wiki_lang", "cs"))
    try:
        from scripts.ollama_client import ollama_generate
        raw = ollama_generate(
            model=_model(config),
            prompt=("Studijní téma: %s\n\nPod-témata bez článku:\n%s\n\nJSON:"
                    % (topic, "\n".join("- %s" % b for b in bad))),
            system=_REPAIR_SYSTEM, config=config,
            timeout=int(_cfg(config).get("llm_timeout", 300)),
            keep_alive=0, options={"temperature": 0.2})
    except Exception as e:
        _log.debug("_repair_subtopics LLM: %s", e)
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
        cand = _normalize_subtopic(str(proposals.get(orig, "") or "").strip())
        if len(cand) < 3 or _norm(cand) == _norm(orig):
            continue
        # návrh modelu se NIKDY nebere na slovo — projde touž kontrolou
        if _findable(config, w, cand, topic):
            out[orig] = cand
            _log.info("study: pod-téma '%s' → '%s' (ověřená náhrada)", orig, cand)
        else:
            _log.info("study: náhrada '%s' za '%s' taky nedohledatelná — "
                      "zahazuji", cand, orig)
        if w.last_transient:
            break
    return out


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
        s = _normalize_subtopic(s)  # HANS_STUDY_CANON_TITLE_V1
        if len(s) >= 3 and _norm(s) not in seen:
            out.append(s)
            seen.add(_norm(s))
    # HANS_STUDY_CURRICULUM_VALIDATE_V1 — ověř dohledatelnost HNED, ať se
    # nedohledatelný název nevleče 3 nocemi stání (viz `_validate_curriculum`).
    return _validate_curriculum(config, out[:n], topic)


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


# ── HANS_STUDY_TOPIC_ANCHOR_V1 (4.8.) — kotva na téma programu ───────────────
# Kurikulum běžně vyrobí složené pod-téma tvaru „<aspekt> <tématu v genitivu>"
# („Geologie Českého ráje"). Takový článek na Wikipedii NEEXISTUJE, ale existují
# správné články pod JINÝM názvem („Geopark Český ráj"). Title-similarity gate je
# ale srazí na 0.33, protože se shoduje jen ta část z názvu programu, ne aspekt
# → `noread` → fail_count → program uvízne. (Doloženo 4.8.: program „Český ráj
# a okolní hrady" stál na „Geologie Českého ráje".)
#
# Klíčová úvaha: tokeny, které pod-téma zdědilo z NÁZVU PROGRAMU, nejsou
# rozlišovací — jsou to konstantní kulisy. Zato kandidát, který je OBSAHUJE, je
# z principu na správném předmětu. Tenhle pozitivní signál dnes zahazujeme.
# Proto: poslední záchrana = vezmi kandidáta, který (a) obsahuje VŠECHNY tokeny
# jádra tématu, (b) je krátký/fokusovaný, (c) má aspoň nízké skóre. Garbage typu
# „Pozemské technologie ve Hvězdné bráně" kotvu programu neobsahuje → neprojde.

def _topic_anchor_tokens(topic: str) -> list:
    """Jádro názvu programu jako tokeny: „Český ráj a okolní hrady" → [cesky, raj].
    Ořízne na první spojce/závorce/dvojtečce — zbytek jsou přílepky, ne předmět."""
    from scripts.web_reader import _title_tokens
    t = (topic or "").strip()
    t = re.split(r"\s+(?:a|i|nebo|se|v|na)\s+|[(:,–-]", t, maxsplit=1)[0]
    return _title_tokens(t)


# HANS_STUDY_ANCHOR_DECOMPOSE_V1 — české místní přípony: „Jičínska" → „Jičín".
_GEO_SUFFIXES = ("ského", "ském", "skou", "ska", "sko", "ské", "ský", "ská", "sky")


def _geo_base(word: str) -> str:
    """Ořízne místní příponu. Krátká slova nechá být (ať nevznikne pahýl)."""
    w = (word or "").strip(" .,;:")
    if len(w) < 7:
        return w
    for suf in _GEO_SUFFIXES:
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
        base = _geo_base(wd)
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
    c = _cfg(config)
    anchor = _topic_anchor_tokens(topic)
    if not anchor:
        return None
    min_score = float(c.get("anchor_min_score", 0.30))
    max_tok = int(c.get("anchor_max_tokens", 4))
    try:
        cands = w.wikipedia_search_candidates(sub, lang=lang, limit=6)
    except Exception as e:
        _log.debug("anchor candidates: %s", e)
        return None
    for title, score in cands:
        if _norm(title) in used_titles:
            continue                     # ať 2 pod-témata nečtou týž článek
        ttok = _title_tokens(title)
        if len(ttok) > max_tok:
            continue                     # dlouhý titul = tangenciální odbočka
        if not all(any(_token_match(a, tt) for tt in ttok) for a in anchor):
            continue                     # neobsahuje předmět programu → mimo
        if score < min_score:
            continue
        _log.info("study: '%s' — přesný článek neexistuje, kotvím na téma "
                  "programu → '%s' (skóre %.2f)", sub, title, score)
        return title
    # HANS_STUDY_ANCHOR_DECOMPOSE_V1 — druhý průchod: dotazy ze SLOŽEK fráze.
    # Pravidlo výše žádá titul obsahující VŠECHNY tokeny tématu, což u místních
    # a odborných článků („Jičín", „Kumburk") nikdy neprojde. Tady se ptáme
    # přímo na složky; o relevanci rozhoduje title-similarity gate uvnitř
    # `_wikipedia_search`, takže se nepřimyká nic nesouvisejícího.
    _maxq = int(c.get("anchor_decompose_max", 3))
    for q in _decomposed_anchor_queries(sub, topic, limit=_maxq):
        if getattr(w, "last_transient", False):
            break                        # rate-limit → nezhoršuj to dalšími dotazy
        try:
            t = w._wikipedia_search(q, lang)
        except Exception as e:
            _log.debug("anchor decompose '%s': %s", q, e)
            continue
        if not t or _norm(t) in used_titles:
            continue
        # ⚠️ DRUHÁ POJISTKA (17.8., z živého testu): práh podobnosti sám
        # nestačí — dotaz „Čechách" prošel na článek „Čechočovice" (cizí obec).
        # Titul proto musí NĚKTERÝM tokenem odpovídat dotazu, jinak ho zahoď.
        _qtok = _title_tokens(q)
        _ttok = _title_tokens(t)
        if _qtok and not any(_token_match(a, b) for a in _qtok for b in _ttok):
            _log.info("study: kotva '%s' pro dotaz '%s' ZAMÍTNUTA "
                      "(titul dotazu neodpovídá)", t, q)
            continue
        _log.info("study: '%s' — kotva ze složky fráze '%s' → '%s'", sub, q, t)
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
        pref = "wiki:%s:" % _norm(topic)
        rows = conn.execute(
            "SELECT title FROM study_seen_works WHERE work_id LIKE ?",
            (pref + "%",)).fetchall()
        conn.close()
        return {_norm(r[0]) for r in rows if r and r[0]}
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
                     ("wiki:%s:%s" % (_norm(topic), _norm(title)), title, _t.time()))
        conn.commit()
        conn.close()
    except Exception as e:
        _log.debug("mark_main_title: %s", e)


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
# HANS_WIKI_UA_CONTACT_V1 — viz web_reader (kontakt v UA = mírnější kvóta).
_UA = {"User-Agent": "HansStudyBot/1.0 "
                     "(+https://github.com/olousolous-jpg/hans)"}


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
        _log.debug("_en_title: throttle (%s)", e)
        return ""
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
    # HANS_WIKI_THROTTLE_WS_V1 (18.8.) — Wikisource je TÁŽ Wikimedia infrastruktura
    # a čerpá z téhož rozpočtu jako Wikipedia; studium sahá na obojí v jednom kole.
    from scripts import _wiki_throttle as _wt
    for lang in langs:
        api = f"https://{lang}.wikisource.org/w/api.php"
        try:
            _wt.acquire(api)
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
                       max_chars: int = 4000, db_path: str = None,
                       sink: list = None) -> str:
    """Vytáhne z OpenAlexu pár nejrelevantnějších NOVÝCH prací (název+rok+autoři+
    abstrakt) k tématu. DEDUP: práce použité dřív (study_seen_works) přeskočí, ať
    Hans necituje stejnou práci/autora opakovaně. '' při chybě/nic. Best-effort.

    HANS_RESEARCH_PAPER_TAKEAWAY_V1 — když je `sink` list, přidá do něj i
    STRUKTURU každé použité práce (title/year/authors/abstract/url), ať z ní
    volající umí udělat per-práce trvalý výpisek (ne jen titul v registru)."""
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
        if sink is not None:                    # HANS_RESEARCH_PAPER_TAKEAWAY_V1
            sink.append({"title": title, "year": year, "authors": authors,
                         "abstract": abstract, "url": wid})
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
    dtok = _dir_tokens(direction_text)
    if not dtok:
        return 0.0
    htok = _dir_tokens(name)
    for e in (examples or [])[:6]:
        htok |= _dir_tokens(str(e))
    if not htok:
        return 0.0
    matched = sum(1 for h in htok if any(_tok_match(h, d) for d in dtok))
    return matched / len(htok)


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
                     db_path: str = None, papers_sink: list = None):
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
    # HANS_STUDY_TOPIC_ANCHOR_V1 — všechny dotazy selhaly (přesný článek pro
    # složené pod-téma neexistuje). Poslední záchrana PŘED `noread`: článek
    # kotvený na téma programu. Dedup přes study_seen_works, ať dvě pod-témata
    # nečtou týž článek. Když ani to nic nedá, chová se to jako dřív.
    if not art or not (art.get("text") or "").strip():
        try:
            _anchor = _anchor_pick(config, w, sub, topic, lang,
                                   _used_main_titles(db_path, topic))
            if _anchor:
                art = w.wikipedia_article(_anchor, lang=lang, max_chars=art_max)
                if art and (art.get("text") or "").strip():
                    _mark_main_title(db_path, topic, art.get("page_title") or _anchor)
        except Exception as e:
            _log.debug("anchor fallback: %s", e)
    if not art or not (art.get("text") or "").strip():
        # HANS_WIKI_TRANSIENT_V1 — rozliš „článek neexistuje" od „Wikipedia
        # zrovna neodpovídá" (429/5xx). Druhé NESMÍ spálit pokus, jinak by
        # rate-limit po 3 nocích přeskočil i pod-téma, které článek MÁ.
        if getattr(w, "last_transient", False):
            _log.info("_gather_material: '%s' — Wikipedia dočasně nedostupná, "
                      "ODKLÁDÁM (pokus se nepočítá)", sub)
            return None, None, "__transient__"
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
                    max_chars=int(rc.get("max_chars", 4000)), db_path=db_path,
                    sink=papers_sink)  # HANS_RESEARCH_PAPER_TAKEAWAY_V1
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


# ── Per-práce výpisek z odborného abstraktu (HANS_RESEARCH_PAPER_TAKEAWAY_V1) ──
_PAPER_TAKEAWAY_SYSTEM = (
    "Jsi {persona_name}. Právě jsi přečetl ABSTRAKT jedné odborné práce. Napiš "
    "si k ní KRÁTKÝ výpisek v první osobě (2-3 věty): co konkrétně tato práce "
    "zjišťuje nebo přináší a co tě na tom zaujalo. Vyjdi VÝHRADNĚ z abstraktu — "
    "nic si nepřidávej a nevymýšlej výsledky, které v něm nejsou. Piš česky, "
    "souvisle, bez uvození typu „Abstrakt uvádí“."
)


def _distill_paper(config: dict, topic: str, paper: dict) -> str:
    """LLM napíše krátký výpisek z abstraktu JEDNÉ práce. '' při selhání —
    volající pak sáhne po deterministickém fallbacku (samotný abstrakt)."""
    abstract = (paper.get("abstract") or "").strip()
    if len(abstract) < 80:
        return ""
    c = _cfg(config)
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
        system = _PAPER_TAKEAWAY_SYSTEM.format(persona_name=_pn(config))
        raw = ollama_generate(model=_model(config), prompt=prompt, system=system,
                              config=config, timeout=timeout, keep_alive=0,
                              options={"temperature": 0.4, "num_ctx": 4096,
                                       "num_predict": 220})
        return (raw or "").strip()
    except Exception as e:
        _log.warning("_distill_paper LLM selhal: %s", e)
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


def _brain_available(config: dict) -> bool:
    """HANS_STUDY_BRAIN_GATE_V1 — je jazykové centrum (Ollama) dostupné?
    Krátká sonda /api/tags. Když je mozek dole (noční PC shutdown) NEBO herní
    mód, nemá smysl tahat materiál (OpenAlex/Wiki) — poznámku stejně
    nevygenerujeme → jen bychom v ~1×/min retry smyčce mlátili OpenAlex (429
    storm, nález 27.7.). Vrať False = study_next odloží (deferred), catchup
    dožene po brain_up. Nezáleží na latenci: study_next běží zřídka."""
    from scripts.ollama_client import brain_available  # HANS_BRAIN_GATE_V1
    return brain_available(config)


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
            # HANS_STUDY_SKIPPED_MARK_V1 — které indexy kurikula byly
            # PŘESKOČENY (nenašel se zdroj), ne nastudovány. Prázdné pole =
            # nic přeskočeno → staré programy se chovají přesně jako dosud.
            try:
                db.execute("ALTER TABLE study_program ADD COLUMN "
                           "skipped_idx TEXT NOT NULL DEFAULT '[]'")
            except sqlite3.OperationalError:
                pass
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
        allowed = {"current_index", "fail_count", "status", "sessions_done",
                   "skipped_idx"}
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
        # HANS_STUDY_SKIPPED_MARK_V1 — indexy, které se PŘESKOČILY.
        try:
            d["skipped_idx"] = set(json.loads(d.get("skipped_idx") or "[]"))
        except Exception:
            d["skipped_idx"] = set()
        return d

    def mark_skipped(self, pid: int, idx: int):
        """HANS_STUDY_SKIPPED_MARK_V1 — zapiš, že pod-téma bylo PŘESKOČENO.

        Bez tohohle se přeskočené tvářilo jako nastudované: stav se odvozoval
        jen z `current_index`, takže „prošel jsem kolem" a „nastudoval jsem"
        vypadaly stejně (nález uživatele 6.8.).
        """
        try:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT skipped_idx FROM study_program WHERE id=?",
                    (pid,)).fetchone()
                cur = set()
                if row:
                    try:
                        cur = set(json.loads(row["skipped_idx"] or "[]"))
                    except Exception:
                        cur = set()
                cur.add(int(idx))
                conn.execute(
                    "UPDATE study_program SET skipped_idx=?, updated_ts=? "
                    "WHERE id=?",
                    (json.dumps(sorted(cur)), time.time(), pid))
                conn.commit()
            finally:
                conn.close()
        except Exception as e:
            _log.warning("mark_skipped failed: %s", e)

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
                    "SELECT id, topic, fail_count, curriculum FROM study_program WHERE status='pending' "
                    "ORDER BY id ASC LIMIT 1").fetchone()
                # HANS_STUDY_PENDING_STUCK_V1 — fail_count musí projít dál,
                # jinak by se počítadlo pokusů vždy vracelo na nulu.
                if not row:
                    return None
                # HANS_STUDY_SEEDED_CURRICULUM_V1 — kurikulum musí projít dál,
                # aby aktivace poznala ručně naseedované téma.
                try:
                    _cur = json.loads(row["curriculum"] or "[]")
                except Exception:
                    _cur = []
                return {"id": row["id"], "topic": row["topic"],
                        "fail_count": row["fail_count"], "curriculum": _cur}
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
            # HANS_STUDY_SEEDED_CURRICULUM_V1 (17.8.) — když už pending téma
            # kurikulum MÁ (ručně naseedované, ověřené proti encyklopedii),
            # respektuj ho a negeneruj přes něj vlastní. Dřív se přepisovalo
            # vždycky, takže ruční seed neměl jak přežít aktivaci.
            curriculum = pend.get("curriculum") or []
            if len(curriculum) >= 3:
                _log.info("study: pending '%s' má připravené kurikulum "
                          "(%d pod-témat) — negeneruji nové",
                          pend["topic"], len(curriculum))
            else:
                curriculum = _generate_curriculum(config, pend["topic"], [])
            if len(curriculum) < 3:
                # HANS_STUDY_PENDING_STUCK_V1 — rozliš „mozek dole" (0 pod-témat,
                # pokus se nepočítá) od „téma je nedohledatelné" (něco přišlo,
                # ale validace to seškrtala). Druhé po 3 nocích uzavři, ať se to
                # neopakuje tiše navěky.
                _transient = not curriculum
                _fails = int(pend.get("fail_count") or 0)
                if not _transient:
                    _fails += 1
                _blocked = (not _transient) and _fails >= 3
                try:
                    conn = self._connect()
                    try:
                        conn.execute(
                            "UPDATE study_program SET fail_count=?, status=?, "
                            "updated_ts=? WHERE id=?",
                            (_fails, "blocked" if _blocked else "pending",
                             time.time(), pend["id"]))
                        conn.commit()
                    finally:
                        conn.close()
                except Exception as _pe:
                    _log.debug("pending fail_count: %s", _pe)
                if _blocked:
                    _log.warning("study: pending téma '%s' po %d pokusech "
                                 "BLOKOVÁNO — encyklopedie k němu nedá dost "
                                 "dohledatelných pod-témat", pend["topic"], _fails)
                    try:
                        self._write_diary(
                            "study_blocked",
                            "Studium '%s' nelze začít" % pend["topic"],
                            "Po %d pokusech se nepodařilo sestavit kurikulum "
                            "z dohledatelných zdrojů." % _fails)
                    except Exception:
                        pass
                else:
                    _log.info("study.ensure_program: kurikulum pending '%s' "
                              "se nevygenerovalo (%s) — pokus %d/3",
                              pend["topic"],
                              "LLM dole" if _transient else "nedohledatelné",
                              _fails)
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
            # HANS_DIRECTION_STUDY_BIAS_V1 — když má Hans vlastní zvolený SMĚR,
            # zvýhodni koníčky, které s ním ladí (afinita), ať studium slouží
            # jeho záměru. Konzervativně: boost = engagement × (1 + w×afinita),
            # takže reálný zájem zůstává hlavní, směr jen nakloní mezi blízkými.
            dir_text = ""
            try:
                from scripts.hans_direction import DirectionStore
                _cur = DirectionStore(config, self._diary_path).current_active()
                dir_text = (_cur or {}).get("direction", "") if _cur else ""
            except Exception:
                dir_text = ""
            w = float(c.get("direction_bias_weight", 0.5))

            def _score(h):
                eng = _topic_engagement(self._diary_path, h.examples)
                if dir_text:
                    aff = _direction_affinity(dir_text, h.name, h.examples)
                    return eng * (1.0 + w * aff)
                return eng
            candidates.sort(key=_score, reverse=True)
            if dir_text and candidates:
                _aff0 = _direction_affinity(dir_text, candidates[0].name,
                                            candidates[0].examples)
                if _aff0 > 0:
                    _log.info("study: výběr '%s' zohlednil směr (afinita %.2f)",
                              candidates[0].name, _aff0)
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
        # HANS_STUDY_BRAIN_GATE_V1 — mozek dole / herní mód → deferred,
        # netahej materiál (jinak noční OpenAlex 429 storm).
        if not _brain_available(config):
            _log.debug("study_next: mozek dole/herní mód — odloženo")
            return None
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
        research_papers = []  # HANS_RESEARCH_PAPER_TAKEAWAY_V1
        material, source_url, _main = _gather_material(
            config, sub, topic, deep=deep, db_path=self._diary_path,
            papers_sink=research_papers)
        # HANS_WIKI_TRANSIENT_V1 — Wikipedia dole (429/5xx) = výpadek zdroje,
        # NE „nenašel jsem". Odlož jako u výpadku LLM: žádný fail_count, žádný
        # skip; pod-téma se zkusí znovu, až API odpoví.
        if not material and _main == "__transient__":
            _log.info("Studijní session odložena: Wikipedia dočasně nedostupná")
            return None
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
                # HANS_STUDY_SKIPPED_MARK_V1 — ať se to ve /studium neukazuje
                # jako nastudované a `already_studied` to nebere za pokryté.
                self.mark_skipped(prog["id"], idx)
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

        # 3b) per-práce výpisky z odborných prací (HANS_RESEARCH_PAPER_TAKEAWAY_V1)
        try:
            self._record_paper_takeaways(config, topic, research_papers,
                                         knowledge, diary_writer)
        except Exception as e:
            _log.debug("record_paper_takeaways: %s", e)

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

    def _record_paper_takeaways(self, config, topic, papers, knowledge=None,
                                diary_writer=None):
        """HANS_RESEARCH_PAPER_TAKEAWAY_V1 — z každé odborné práce použité v
        research tieru udělá TRVALÝ per-práce výpisek (reading_takeaway + URL +
        RAG). Dřív po práci zůstal jen titul v study_seen_works → konkrétní
        vědecký přínos se ztrácel. Best-effort, nikdy neshodí studijní krok."""
        rc = _cfg(config).get("research_tier", {}) or {}
        if not papers or not rc.get("per_paper_takeaway", True):
            return
        for p in papers:
            try:
                title = (p.get("title") or "").strip()
                if not title:
                    continue
                takeaway = _distill_paper(config, topic, p)
                if not takeaway:
                    # fallback bez LLM: samotný abstrakt (lossless), ať se přínos
                    # neztratí ani když je mozek dole
                    ab = (p.get("abstract") or "").strip()
                    if len(ab) < 80:
                        continue
                    takeaway = ("Přečetl jsem odbornou práci a poznamenal si její "
                                "obsah: " + ab[:500])
                year = p.get("year") or ""
                d_title = f"{title} ({year})" if year else title
                url = p.get("url") or ""
                try:
                    conn = sqlite3.connect(self._diary_path)
                    conn.execute(
                        "INSERT INTO diary (ts, event_type, title, data, "
                        "source_url) VALUES (?,?,?,?,?)",
                        (time.time(), "reading_takeaway", d_title, takeaway, url))
                    conn.commit()
                    conn.close()
                except Exception as e:
                    _log.warning("paper takeaway zápis selhal: %s", e)
                    continue
                if knowledge is not None and getattr(knowledge, "enabled", False):
                    try:
                        coll = str(_cfg(config).get("rag_collection", "hans_cetba"))
                        knowledge.upload(
                            collection_key=coll,
                            doc_id="paper_" + hashlib.md5(
                                (url or d_title).encode("utf-8")).hexdigest()[:16],
                            title=d_title, text=takeaway,
                            metadata={"koníček": topic, "zdroj": url or "openalex",
                                      "typ": "research_paper"})
                    except Exception as e:
                        _log.debug("paper takeaway RAG: %s", e)
                _log.info("study: per-práce výpisek — „%.50s“", title)
            except Exception as e:
                _log.warning("_record_paper_takeaways položka selhala: %s", e)

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
            # HANS_STUDY_DEEPEN_TITLES_V1 — pod-témata MUSÍ být KRÁTKÉ KANONICKÉ
            # názvy (max 5 slov, žádné dvojtečky/závorky/vysvětlení), jinak
            # Wikipedia nenajde článek → study se zasekne na 3 nocích × pod-tématu.
            # Vzory: „WCAG 2.2", „Design tokens", „3D fotogrammetrie",
            #        „Micro-interactions", „Neuromarketing".
            sysp = (
                "Jsi kurátor studia. Autor nastudoval pod-témata níže a vytvořil "
                "z nich dílo. Buď KRITICKÝ: v 1 větě řekni, co dílu chybí do "
                "hloubky, a navrhni %d NOVÝCH pod-témat, která jdou VÍC DO "
                "HLOUBKY, STAVÍ na nastudovaném, ale ŽÁDNÉ z nastudovaných "
                "NEOPAKUJÍ.\n\n"
                "PRAVIDLA PRO NÁZVY POD-TÉMAT (jinak Wikipedia nenajde článek):\n"
                "  1. MAX 5 slov (kratší lepší)\n"
                "  2. ŽÁDNÉ dvojtečky, středníky, závorky s vysvětlením\n"
                "  3. Kanonický pojem, ne popisná věta\n"
                "  Vzory: \"WCAG 2.2\", \"Design tokens\", \"3D fotogrammetrie\","
                " \"Micro-interactions\", \"Neuromarketing\"\n"
                "  ŠPATNĚ: \"Analýza přístupnosti (WCAG) a aplikací: konkrétní "
                "techniky pro zajištění inkluzivity...\" (moc dlouhé)\n\n"
                "Vrať POUZE JSON: {\"critique\":\"…\",\"subtopics\":[\"…\"]} "
                "(česky)." % max_new)
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
                subs = [_normalize_subtopic(str(x))  # HANS_STUDY_CANON_TITLE_V1
                        for x in (d.get("subtopics") or []) if str(x).strip()]
                subs = [x for x in subs if len(x) >= 3][:max_new]
                # HANS_STUDY_CURRICULUM_VALIDATE_V1 — deepen trpí týmž neduhem
                # jako iniciální generování (doloženo Designem: „Analýza
                # přístupnosti…" → Wikipedia nenašla → 3 noci stání).
                subs = _validate_curriculum(config, subs, topic)
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
        # HANS_DEEPEN_TTL_V1 (7.8.) — prošlé návrhy nejdřív zavři.
        # Bez expirace ležel návrh ve frontě neomezeně a bral na sebe holé
        # „ano/ne" i po hodinách (7.8.: návrh z 03:26 spolkl v 11:23 odpověď
        # určenou nabídce filmu). Agentní návrh vyprší po 3 min; tenhle je
        # jiný žánr — uživatel se má rozmyslet — proto DEN.
        try:
            _ttl = float((self.config.get("study", {}) or {}).get(
                "deepen_ttl_h", 24)) * 3600.0
            if _ttl > 0:
                _c = self._connect()
                try:
                    _cur = _c.execute(
                        "UPDATE deepen_proposals SET status='expired' "
                        "WHERE status='pending' AND ts < ?",
                        (time.time() - _ttl,))
                    _c.commit()
                    if _cur.rowcount:
                        _log.info("HANS_DEEPEN_TTL_V1: %d návrhů prohloubení "
                                  "vypršelo (starší než %.0f h)",
                                  _cur.rowcount, _ttl / 3600.0)
                finally:
                    _c.close()
        except Exception as _te:
            _log.debug("deepen ttl: %s", _te)
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
        # HANS_STUDY_TODAY_SHARED_V1 — ořež NEJDŘÍV a teprve pak připoj dnešek,
        # jinak by ho strop `max_chars` uřízl — a je to ta část, kvůli které se
        # to dělá: bez ní si model z „posledních pár dní studuji" vyrobil
        # „dnes jsem studoval", ačkoli dnes studium neproběhlo (18.8.).
        out = out[:max_chars]
        _tl = today_line(diary_db_path)
        return (out + " " + _tl).strip() if _tl else out
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
    """Jedna studijní session. Vrací JEDEN ze ŠESTI kódů (HANS_STUDY_UNIFY_V1 —
    dřív jich docstring jmenoval jen 4, `skipped` a `noread` chyběly):
       'studied'   — nastudováno pod-téma
       'completed' — kurikulum dokončeno (mistrovská reflexe)
       'skipped'   — pod-téma po `max_subtopic_failures` přeskočeno (program se
                     POSUNUL, ale nic se nenaučil)
       'noread'    — k pod-tématu se nenašlo čtení; fail_count++, program STOJÍ
       'idle'      — nic ke studiu (žádný durable koníček / vše prostudováno)
       'deferred'  — transientní selhání (Ollama/wiki dole) → zkusit znovu
    Volající NEMÁ kódy porovnávat řetězcem — na to jsou `is_transient()`
    (nastavit denní guard?) a `made_progress()` (posunulo se to?)."""
    if not _cfg(config).get("enabled", True):
        return "idle"
    # HANS_STUDY_VRAM_HANDOFF_V1 — studium běží na base OpenEuroLLM (8GB), ale
    # hans-czech (8GB) je rezidentní a 8+8 > 16GB VRAM. Kroky:
    #  1) pause_warmup → oba keepalive (ping_model + ollama_warmup) přestanou
    #     re-pinovat hans-czech po dobu dávky.
    #  2) ollama_unload_all → AKTIVNĚ uvolni hans-czech HNED. Samotná pauza
    #     nestačí: hans-czech je nahraný s keep_alive=-1, který sám nevyprší,
    #     a Ollama ho neevictuje ani pro nový request → base model se nevejde
    #     → 300s timeout → deferred (přesně tenhle býval symptom). V noci to
    #     „projde" jen náhodou (hans-czech vyprší při klidu), ve dne/ránu ne.
    # Po session resume_warmup re-povolí keepalive → hans-czech se dotáhne.
    # Auto-expiry pauzy 20 min = cap, kdyby impl spadl bez resume.
    try:
        from scripts.ollama_client import (pause_warmup as _pw,
                                           ollama_unload_all as _ua)
        _pw(1200)
        _ua(config=config)
    except Exception:
        pass
    try:
        from scripts.ollama_client import resume_warmup as _rw
    except Exception:
        _rw = None
    try:
        return _run_study_session_impl(config, diary_db_path, knowledge,
                                       diary_writer)
    finally:
        if _rw is not None:
            try:
                _rw()
            except Exception:
                pass


def _run_study_session_impl(config: dict, diary_db_path: str, knowledge=None,
                            diary_writer=None) -> str:
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
    # HANS_SCHEDULE_V1 — razítko včetně důvodu skip (deferred/idle).
    # ok=True jen když session opravdu proběhla (studied/completed); jinak si
    # audit může všimnout, PROČ study visí (nejčastěji brain_down = deferred).
    try:
        from scripts import hans_schedule
        if res is None:
            hans_schedule.mark('study_tick', ok=False, skip_reason='deferred')
        else:
            rr = res.get("result", RESULT_STUDIED)
            if produced_knowledge(rr):   # přísnější než made_progress — viz _KNOWLEDGE
                hans_schedule.mark('study_tick', ok=True)
            else:
                hans_schedule.mark('study_tick', ok=False, skip_reason=rr)
    except Exception:
        pass
    if res is None:
        return RESULT_DEFERRED
    return res.get("result", RESULT_STUDIED)


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


def today_line(diary_db_path: str = "data/hans_diary.db") -> str:
    """HANS_STUDY_TODAY_SHARED_V1 (19.8.) — JEDNA věta o tom, jak dopadl DNEŠEK.

    Jedna pravda pro dvě místa: výpis `/studium` i kontext volného hovoru.
    Původně to bylo jen ve výpisu (HANS_STUDY_TODAY_LINE_V1) a hovor o dnešku
    nevěděl nic — doloženo 18.8., kdy Hans v jednom chatu řekl „Dnes jsem
    studoval do hloubky Český ráj" a o tři výměny později „Dnes se mi nic
    nastudovat nepodařilo". Druhá kopie logiky by se rozešla stejně.

    Zdroj je `hans_schedule.study_tick`, kde se od HANS_SCHEDULE_LAST_OK_V1
    rozlišuje „kdy to naposledy ZKUSILO" od „kdy naposledy USPĚLO".
    '' když se stav nedá zjistit (volající pak nic nepřidává).
    """
    try:
        import datetime as _dt
        from scripts.hans_schedule import ScheduleStore
        row = ScheduleStore(diary_db_path).get("study_tick") or {}
        today = _dt.date.today()

        def _is_today(ts):
            try:
                return bool(ts) and _dt.date.fromtimestamp(float(ts)) == today
            except Exception:
                return False

        if _is_today(row.get("last_ok_ts")):
            # HANS_STUDY_TODAY_TOPIC_V1 (19.8.) — říct i CO. Bez tématu si model
            # ve volném hovoru vzal starší téma z deníku: doloženo 19.8., kdy
            # Hans tvrdil „studoval jsem Český ráj", ačkoli dnes studoval
            # Cimrmana (Český ráj dokončil předchozí večer).
            try:
                import sqlite3 as _sq
                with _sq.connect("file:%s?mode=ro" % diary_db_path,
                                 uri=True, timeout=3.0) as _db:
                    _r = _db.execute(
                        "SELECT title FROM diary WHERE event_type='study_note' "
                        "AND ts >= ? ORDER BY ts DESC LIMIT 1",
                        (_dt.datetime.combine(today, _dt.time.min).timestamp(),)
                    ).fetchone()
                _t = (_r[0] if _r else "") or ""
                # „Studium: <téma> — <pod-téma>"
                _t = _t.replace("Studium:", "").strip()
                if " — " in _t:
                    _tema, _sub = [x.strip() for x in _t.split(" — ", 1)]
                    return ("Dnes se mi povedlo nastudovat pod-téma „%s“ "
                            "z tématu „%s“." % (_sub, _tema))
                if _t:
                    return "Dnes se mi povedlo nastudovat „%s“." % _t
            except Exception as _e2:
                _log.debug("today_line: téma nešlo zjistit (%s)", _e2)
            return "Dnes se mi povedlo nastudovat pod-téma."
        if _is_today(row.get("last_run_ts")):
            why = {"deferred": "encyklopedie nebo mozek neodpovídaly",
                   "noread": "k pod-tématu jsem nenašel zdroj",
                   "idle": "neměl jsem co studovat",
                   "skipped": "pod-téma jsem přeskočil"}
            r = (row.get("last_skip_reason") or "").strip()
            kdy = _dt.datetime.fromtimestamp(
                float(row["last_run_ts"])).strftime("%H:%M")
            txt = why.get(r, r)   # neznámý kód syrový, ne domyšlený
            return ("Dnes se mi nic nastudovat nepodařilo (poslední pokus %s%s)."
                    % (kdy, (", důvod: " + txt) if txt else ""))
        return "Dnes jsem se ke studiu ještě nedostal."
    except Exception as e:
        _log.debug("today_line: %s", e)
        return ""
