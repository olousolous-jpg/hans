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


from scripts.hans_study_kurikulum import _already_covered   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.hans_study_kurikulum import already_studied   # ROZDELENI_PRIKAZU_V1 — přesunuto


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

# HANS_DEEPEN_ZAVER_NENI_SOUHLAS_V1 (4.9.) — ZAVŘENÍ HOVORU NENÍ VERDIKT.
# Doloženo testovacím rozhovorem: „dobre, diky" (2 slova, bez otazníku, pod
# limitem) prošlo bránou, klasifikátor z něj udělal SCHVALUJE a Hans odpověděl
# „Schváleno, pane. Prohloubím studium Norimberský proces." — a SKUTEČNĚ TO
# PROVEDL: program se z `completed` vrátil na `active`, kurikulum 8 → 12 témat,
# deepen_round 0 → 1 (log 15:16:20). Poděkování na konci hovoru je přitom
# nejběžnější věta vůbec.
# Rozlišovač: „dobře" je v závěrečné frázi VÝPLŇ, ne souhlas. Když věta nese
# rozloučení nebo poděkování a ŽÁDNÝ silný verdikt, není to reakce na návrh.
# ⚠️ „ano, díky" / „souhlasím, díky" projdou dál — poděkování samo o sobě
# souhlas neruší, ruší ho jen NEPŘÍTOMNOST výslovného verdiktu.
# Změřeno na 1231 reálných replikách: neubere ANI JEDEN dosavadní záchyt
# (28 → 28), a 12/12 kontrolních případů sedí.
_ZAVER_RE = re.compile(
    r"\b(d[íi]ky|d[ěe]kuj\w*|dik|na\s+shledanou|nashle|m[ěe]j\s+se|"
    r"dobrou\s+noc|hezk[ýy]\s+(den|ve[čc]er)|zat[íi]m\s+ahoj|tak\s+zat[íi]m)\b",
    re.I)
# Výslovný verdikt — tenhle poděkování přebije. („dobře“ a „jo“ tu ZÁMĚRNĚ
# nejsou: samy o sobě verdikt nesou, ale v rozloučení jsou jen výplň.)
_SILNY_VERDIKT_RE = re.compile(
    r"\b(ano|souhlas\w*|schval(?!n)\w*|prohlub|prohloub|ne\b|nechci|"
    r"nesouhlas\w*|zru[šs]|nech\s+to|špatn\w*|slab\w*|m[ěe]l\s+bys|"
    r"douč|dodělej)\b", re.I)


from scripts.hans_study_kurikulum import je_reakce_na_navrh   # ROZDELENI_PRIKAZU_V1 — přesunuto


def _cfg(config: dict) -> dict:
    return (config.get("study", {}) or {})


def _model(config: dict) -> str:
    c = _cfg(config)
    er = config.get("evening_reflection", {}) or {}
    return str(c.get("model", er.get("model",
                     "jobautomation/OpenEuroLLM-Czech:latest")))


from scripts.hans_study_kurikulum import _parse_json_list   # ROZDELENI_PRIKAZU_V1 — přesunuto


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


from scripts.hans_study_kurikulum import _normalize_subtopic   # ROZDELENI_PRIKAZU_V1 — přesunuto


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


from scripts.hans_study_kurikulum import _findable   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.hans_study_kurikulum import _validate_curriculum   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.hans_study_kurikulum import _repair_subtopics   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.hans_study_kurikulum import _generate_curriculum   # ROZDELENI_PRIKAZU_V1 — přesunuto


# ── Hloubkové čtení: plný článek + intro pododkazů (HANS_STUDY_DEEP_V1) ──────
_GENERIC_LEADIN = re.compile(
    r"^(základy|úvod do|úvod|práce s|práce se|principy|teorie|tvorba|"
    r"co je|historie|vývoj)\s+", re.IGNORECASE)


from scripts.hans_study_zdroje import _search_queries   # ROZDELENI_PRIKAZU_V1 — přesunuto


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

from scripts.hans_study_zdroje import _topic_anchor_tokens   # ROZDELENI_PRIKAZU_V1 — přesunuto


# HANS_STUDY_ANCHOR_DECOMPOSE_V1 — české místní přípony: „Jičínska" → „Jičín".
_GEO_SUFFIXES = ("ského", "ském", "skou", "ska", "sko", "ské", "ský", "ská", "sky")


from scripts.hans_study_zdroje import _geo_base   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.hans_study_zdroje import _decomposed_anchor_queries   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.hans_study_zdroje import _anchor_pick   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.hans_study_zdroje import _used_main_titles   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.hans_study_zdroje import _mark_main_title   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.hans_study_zdroje import _reconstruct_abstract   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.hans_study_zdroje import _seen_work_ids   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.hans_study_zdroje import _record_works   # ROZDELENI_PRIKAZU_V1 — přesunuto


# ── HANS_STUDY_SOURCES_V2 — Wikisource (primární texty) + Internet Archive ──
# HANS_WIKI_UA_CONTACT_V1 — viz web_reader (kontakt v UA = mírnější kvóta).
_UA = {"User-Agent": "HansStudyBot/1.0 "
                     "(+https://github.com/olousolous-jpg/hans)"}


from scripts.hans_study_zdroje import _en_title   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.hans_study_zdroje import _wikisource_read   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.hans_study_zdroje import _ia_research   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.hans_study_zdroje import _openalex_research   # ROZDELENI_PRIKAZU_V1 — přesunuto


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
                    if _norm(tema_dialogu(n)) in _stud:
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


from scripts.hans_study_zdroje import _gather_material   # ROZDELENI_PRIKAZU_V1 — přesunuto


# ── HANS_STUDY_ARTICLE_JUDGE_V1 (26. 9.) — JE ČLÁNEK O POD-TÉMATU? ──────────
# Změřeno na 87 nastudovaných pod-tématech: title-similarity gate pustí obecný
# nebo cizí článek („Taktika ve fotbale" → MS 1962, „Pověsti a mytologie"
# (Český ráj) → Inuitská mytologie, „Akustická ekologie" → Ekologie). Soudce
# nad ÚVODEM článku řekl „ne" u 43/87, z toho ~35 právem. Výběr jen podle
# NÁZVU (deterministicky i malým LLM) zkoušen a ZAMÍTNUT — mění stejně
# k horšímu jako k lepšímu. Náhrada z kandidátů cs + en (úvod, soudce) zlepšila
# ~22 z 34 změn; horší byly hlavně ŽIVOTOPISY → ty se vyřazují podle data
# narození. Nenajde-li se nic lepšího, ZŮSTÁVÁ dnešní volba (žádné zhoršení).
# Model qwen2.5:7b (4,7 GB) se vejde do VRAM vedle base i hans-czech.
_JUDGE_SYSTEM = (
    "Posuzuješ, jestli se z článku dá nastudovat zadané pod-téma. 'ano' = článek "
    "je přímo o pod-tématu; 'castecne' = obecnější nadřazený článek, kde pod-téma "
    "tvoří podstatnou část; 'ne' = článek je o něčem jiném (jiná věc se stejným "
    "slovem, jiná země, jen okrajová zmínka).")
_JUDGE_SCHEMA = {"type": "object", "properties": {"verdikt": {
    "type": "string", "enum": ["ano", "castecne", "ne"]}}, "required": ["verdikt"]}
_ZIVOTOPIS = re.compile(
    r"\(\s*\*\s*\d|\bnarozen[aý]?\b|\(born\b|\bborn \d|\(\d{4}\s*[–-]\s*\d{4}\)"
    r"|\(\s*\d{1,2}\.\s*\w+\s+\d{3,4}"            # (27. ledna 1814 – …)
    r"|\(\s*\d{1,2}\s+[A-Z][a-z]+\s+\d{3,4}"         # (27 January 1814 – …)
    r"|\(\s*[A-Z][a-z]+\s+\d{1,2},\s*\d{3,4}")       # (January 27, 1814 – …)


from scripts.hans_study_zdroje import _soudce_clanku   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.hans_study_zdroje import _overeny_clanek   # ROZDELENI_PRIKAZU_V1 — přesunuto


# ── HANS_STUDY_MDN_V1 (26. 9.) — WEBOVÁ TÉMATA Z MDN ─────────────────────────
# Pilot 26. 9.: z Wikipedie vzešlo na 3 web pod-témata 6 pravidel, ověřitelné
# ~1 („Základy HTML a CSS“ → článek *HTML editor*, „Typografie pro web“ →
# *Plochý design*). MDN (Mozilla, CC-BY-SA) na 3 články 19 pravidel, konkrétních
# (kontrast 4,5:1, max-width obrázků, relativní breakpointy). Wikipedie popisuje,
# CO web design je; MDN, JAK se dělá. Vybírá týž soudce (nad shrnutím z MDN).
_WEB_TEMA = re.compile(
    r"\b(web\w*|html\w*|css|javascript\w*|js|ux|ui|wcag|responziv\w*|"
    r"pristupnost\w*|frontend|front-end|prohlizec\w*|design system\w*|"
    r"designov\w* system\w*)\b", re.I)


from scripts.hans_study_zdroje import _je_web_tema   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.hans_study_zdroje import _en_dotaz   # ROZDELENI_PRIKAZU_V1 — přesunuto


# HANS_STUDY_WCAG_V1 (26. 9.) — MĚŘITELNÉ HRANICE jsou ve WCAG, ne na MDN.
# Test 26. 9.: 4 web pod-témata z MDN = 7 rozumných pravidel, měřitelných 0
# (MDN vrací přehledové stránky). Kritéria WCAG 2.2 „Understanding“ čísla nesou
# (kontrast 4,5:1, řádkování 1,5, řádek ≤ 80 znaků, reflow 320 px, cíl 24 px).
# Sada pro vizuální design je malá a stálá → katalog podle klíčových slov;
# bere se ZAČÁTEK stránky (znění kritéria + účel), čísla jsou tam.
_WCAG = "https://www.w3.org/WAI/WCAG22/Understanding/%s.html"
_WCAG_MAPA = (
    (r"kontrast|barv|barev|color|colour", ("contrast-minimum", "use-of-color", "non-text-contrast")),
    (r"typograf|pism|font|text|citel|radk", ("visual-presentation", "text-spacing", "resize-text")),
    (r"responziv|mobil|rozvrz|layout|mrizk", ("reflow", "target-size-minimum")),
    (r"pristupn|wcag|ux|ui|ovlad|navigac|formular|interakt|tlacit",
     ("contrast-minimum", "text-spacing", "target-size-minimum", "visual-presentation")),
)


from scripts.hans_study_zdroje import _wcag_casti   # ROZDELENI_PRIKAZU_V1 — přesunuto


_MDN = "https://developer.mozilla.org"


from scripts.hans_study_zdroje import _mdn_text   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.hans_study_zdroje import _mdn_clanek   # ROZDELENI_PRIKAZU_V1 — přesunuto


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


# ── HANS_STUDY_ARTICLE_REST_V1 (5. 10.) — dočítání dlouhého článku ───────────
# Hlavní čtení bere prvních `article_max_chars` (12 000) znaků článku; dlouhá
# hesla (anglická odborná mají 24–55 tisíc) tak Hans četl jen z části — v logu
# 42 ze 123 session na stropu. Pouhé zvednutí stropu by přeteklo okno modelu
# (`num_ctx`), proto se ZBYTEK článku dělí na části a z každé vzniká vlastní
# poznámka `study_note_part`. Hlavní poznámka `study_note` zůstává jedna na
# pod-téma (mistrovská reflexe a další čtenáři s tím počítají).
# Měření: `data/mereni/studium_citace/` (dělení 4 dlouhých hesel, ~70 s/část).
_REST_KONEC = re.compile(
    r"\n+==\s*(References|Notes|See also|External links|Further reading|Bibliography|"
    r"Citations|Sources|Footnotes|Reference|Odkazy|Poznámky|Literatura|Externí odkazy|"
    r"Související články)\s*==\s*\n", re.I)
_REST_NADPIS = re.compile(r"\n+(==+[^=\n]+==+)\s*\n")


def _article_rest_parts(full: str, art_max: int, part_max: int, max_parts: int):
    """Vrátí (části, nepřečteno_znaků): text ZA stropem hlavního čtení, dělený
    na hranicích sekcí/odstavců; koncové sekce s odkazy se vynechají."""
    full = full or ""
    m = _REST_KONEC.search(full)
    telo = full[:m.start()] if m else full
    if len(telo) <= art_max or max_parts <= 0:
        return [], 0
    start = telo.rfind("\n", 0, art_max)
    rest = telo[start if start > art_max * 0.8 else art_max:]
    kusy, pos = [], 0
    for mm in _REST_NADPIS.finditer(rest):
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


_PART_SYSTEM = (
    "Jsi {persona_name}. Dočítáš DALŠÍ ČÁST článku, jehož začátek sis už "
    "prostudoval a zapsal. Napiš si STUDIJNÍ POZNÁMKU v první osobě (5-8 vět): "
    "co podstatného tato část přináší a co tě zaujalo či překvapilo. Tato část "
    "nemusí souviset s tvým pod-tématem — nepropojuj ji s ním uměle a nedomýšlej "
    "souvislosti, které v textu nejsou. Drž se FAKTŮ z textu — nic si "
    "nepřimýšlej, nehádej, nedoplňuj z vlastní paměti a nevkládej žádné značky "
    "v hranatých závorkách. Piš česky, souvisle, bez nadpisů a odrážek."
)


def _generate_part_note(config: dict, topic: str, sub: str, title: str,
                        part: str, i: int, n: int) -> str:
    """Poznámka z jedné dočítané části článku. '' při selhání (LLM dole)."""
    c = _cfg(config)
    prompt = (f"Koníček: {topic}\nPod-téma, kvůli kterému článek čtu: {sub}\n\n"
              f"Článek: {title} — pokračování, část {i} z {n}:\n{part}\n\n"
              f"Napiš si poznámku k této části článku.")
    try:
        from scripts.ollama_client import ollama_generate
        from scripts.hans_persona import persona_name as _pn
    except ImportError:
        return ""
    try:
        raw = ollama_generate(model=_model(config), prompt=prompt,
                              system=_PART_SYSTEM.format(persona_name=_pn(config)),
                              config=config, timeout=int(c.get("llm_timeout", 300)),
                              keep_alive=0,
                              options={"temperature": 0.4,
                                       "num_ctx": int(c.get("num_ctx", 8192)),
                                       "num_predict": 600})
        return (raw or "").strip()
    except Exception as e:
        _log.warning("_generate_part_note LLM selhal: %s", e)
        return ""


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


from scripts.hans_study_kurikulum import add_pending_topic   # ROZDELENI_PRIKAZU_V1 — přesunuto


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
            # HANS_STUDY_SOURCES_KEEP_V1 (26. 9.) — CELÝ studijní materiál.
            # Dřív se z ~12–16 tis. znaků článku uložila jen poznámka o 6–9
            # větách (~8 %) a zdroj se zahodil (87 poznámek, 0 s odkazem).
            # Web z díla pak mohl stavět jen z poznámky. Poznámka zůstává
            # Hansovým zápiskem; tohle je podklad pro text díla a ověření faktů.
            db.execute("""CREATE TABLE IF NOT EXISTS study_sources (
                id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL,
                program_id INTEGER, idx INTEGER, deepen_round INTEGER,
                topic TEXT, sub TEXT, main_title TEXT, url TEXT,
                material TEXT, chars INTEGER)""")
            db.execute("CREATE INDEX IF NOT EXISTS idx_study_sources_sub "
                       "ON study_sources(topic, sub)")
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
            # HANS_LEARNING_PROGRESS_V1 — přednost koníčku, kde se Hans
            # z četby ještě učí (násobek 0,5 + LP; málo dat = 1,0)
            from scripts import hans_learning as _hl
            _lp = (_hl.learning_progress(config, self._diary_path)
                   if _hl.zapnuto(config) else {})

            def _score(h):
                eng = _topic_engagement(self._diary_path, h.examples)
                eng *= _hl.nasobek(_lp, h.name)
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
        if not material and _main == "__odmitnuto__":
            # HANS_STUDY_SKIP_REJECTED_V1 — soudce článek odmítl a lepší není:
            # další noci by dopadly stejně, přeskoč hned (bez 3 prázdných pokusů)
            prog["fail_count"] = max_fail - 1
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

        # 3a) HANS_STUDY_SOURCES_KEEP_V1 — ulož celý materiál i odkaz
        self._save_source(prog, idx, topic, sub, _main, source_url, material)
        # 3a2) HANS_STUDY_FACTS_V1 — výpis faktů z materiálu jako běžné čtení
        try:
            self._write_facts(config, prog, idx, sub, _main, source_url,
                              material, knowledge)
        except Exception as e:
            _log.warning("study: výpis faktů '%s' selhal: %s", sub, e)
        # HANS_PRIRUCKA_V1 — u webového tématu si z nastudovaného vytáhni pravidla
        if _je_web_tema(sub, topic) and _cfg(config).get("prirucka_enabled", True):
            try:
                from scripts import hans_prirucka as _hp
                _hp.vytahni(config, topic, sub, material, source_url, self._diary_path)
            except Exception as e:
                _log.warning("study: příručka '%s' selhala: %s", sub, e)

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

        # 5a) HANS_STUDY_ARTICLE_REST_V1 — dočti zbytek dlouhého článku po
        # částech (až po posunu: selže-li dočítání, pod-téma se neopakuje)
        try:
            self._read_article_rest(config, prog, idx, topic, sub, _main,
                                    source_url, knowledge, diary_writer)
        except Exception as e:
            _log.warning("study: dočítání článku '%s' selhalo: %s", _main, e)

        # 6) dokončení?
        if new_idx >= len(curriculum):
            prog["current_index"] = new_idx
            self._complete_program(config, prog, knowledge, diary_writer)
            return {"result": "completed", "topic": topic, "sub": sub}
        return {"result": "studied", "topic": topic, "sub": sub,
                "index": new_idx, "total": len(curriculum)}

    def _read_article_rest(self, config: dict, prog: dict, idx: int, topic: str,
                           sub: str, main_title, url, knowledge=None,
                           diary_writer=None) -> int:
        """HANS_STUDY_ARTICLE_REST_V1 — z každé části článku za stropem hlavního
        čtení zapíše poznámku `study_note_part` (deník + RAG). Vrací počet
        zapsaných částí. Jen články Wikipedie; `article_rest_parts` 0 = vypnuto."""
        c = _cfg(config)
        max_parts = int(c.get("article_rest_parts", 4))
        m = re.match(r"https://(\w+)\.wikipedia\.org/wiki/", url or "")
        if max_parts <= 0 or not m or not main_title:
            return 0
        from scripts.web_reader import WebReader
        full = WebReader(config)._wiki_extract(main_title, m.group(1),
                                               intro_only=False)
        parts, zbyva = _article_rest_parts(
            full, int(c.get("article_max_chars", 12000)),
            int(c.get("article_part_chars", 12000)), max_parts)
        if not parts:
            return 0
        hotovo = 0
        for i, part in enumerate(parts, 1):
            note = _generate_part_note(config, topic, sub, main_title, part,
                                       i, len(parts))
            if not note:
                break                      # LLM dole → zbytek se nedočte
            title = (f"Studium: {topic} — {sub} · dočítání článku "
                     f"{main_title} ({i}/{len(parts)})")
            self._write_diary("study_note_part", title, note, diary_writer)
            if knowledge is not None and getattr(knowledge, "enabled", False):
                try:
                    knowledge.upload(
                        collection_key=str(c.get("rag_collection", "hans_cetba")),
                        doc_id=f"study_{prog['id']}_{idx}_p{i}",
                        title=title, text=note,
                        metadata={"koníček": topic, "pod-téma": sub,
                                  "zdroj": url, "typ": "study_note_part"})
                except Exception as e:
                    _log.debug("study part RAG upload: %s", e)
            hotovo += 1
        _log.info("study: HANS_STUDY_ARTICLE_REST_V1 '%s' — dočteno %d/%d částí "
                  "(článek %d zn, nepřečteno %d zn)", main_title, hotovo,
                  len(parts), len(full or ""), zbyva)
        return hotovo

    def _write_facts(self, config: dict, prog: dict, idx: int, sub: str,
                     main_title, url, material, knowledge=None) -> int:
        """HANS_STUDY_FACTS_V1 (9. 10.) — studijní poznámka je Hansův zápisek a
        fakta skoro nenese (11 lekcí: z 84 letopočtů a čísel materiálu jich má
        poznámka 5). Z téhož materiálu se proto navíc uloží VÝPIS FAKTŮ jako
        běžné čtení (`web_read` + RAG), stejnou funkcí jako u dohledání
        (`hans_findings.zapis_fakta`). `facts_parts` 0 = vypnuto."""
        c = _cfg(config)
        max_parts = int(c.get("facts_parts", 3))
        if max_parts <= 0 or not (material or "").strip():
            return 0
        from scripts.hans_findings import zapis_fakta, _casti_textu
        parts = _casti_textu(material, int(c.get("article_part_chars", 12000)),
                             max_parts)
        if not parts:
            return 0
        title = main_title if main_title and main_title != "__transient__" else sub
        hotovo, vet = zapis_fakta(
            config, self._diary_path, knowledge, str(title), sub, url or "", parts,
            "study_fakta_%s_%s" % (prog.get("id"), idx), zdroj="studium",
            popis="ze studijního materiálu")
        _log.info("study: HANS_STUDY_FACTS_V1 '%s' — fakta z %d/%d částí, %d vět "
                  "(materiál %d zn)", sub, hotovo, len(parts), vet, len(material))
        return hotovo

    def _save_source(self, prog: dict, idx: int, topic: str, sub: str,
                     main_title, url, material) -> None:
        """HANS_STUDY_SOURCES_KEEP_V1 — selhání zápisu studium nezastaví."""
        try:
            conn = sqlite3.connect(self._diary_path, timeout=5.0)
            try:
                conn.execute(
                    "INSERT INTO study_sources (ts, program_id, idx, "
                    "deepen_round, topic, sub, main_title, url, material, chars) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (time.time(), prog.get("id"), idx,
                     int(prog.get("deepen_round") or 0), topic, sub,
                     main_title if main_title != "__transient__" else None,
                     url, material, len(material or "")))
                conn.commit()
            finally:
                conn.close()
        except Exception as e:
            _log.warning("study: zdroj se neuložil (%s): %s", sub, e)

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

    # ── HANS_PAPER_TAKEAWAY_DEFERRED_V1 (9. 9.) ─────────────────────────────
    # Fallback ,,radsi surovy abstrakt nez nic'' zapisoval do deniku i do RAG
    # ANGLICKY ABSTRAKT jako Hansuv vypisek (3 radky za 8.-9. 9., 2 z nich i
    # v RAG) — Hans si to pozdeji vybavi jako SVOU cetbu. Presne tuhle tridu
    # uzavrel HANS_DEFERRED_SUMMARY_V1 (15. 7.) u `web_read`; jeho plosny audit
    # tehdy rekl "web_reader._summarize byl JEDINY fabrikujici fallback" —
    # o mesic pozdeji pribyl druhy. Navazuje, neduplikuje: tam `web_read` pres
    # hans_curiosity, tady `reading_takeaway` z research tieru.
    #
    # Pending payload ZAMERNE NEJDE do deniku (na rozdil od `web_read`):
    # `reading_takeaway` ma obsah v `data`, ale konzumenty maji OBA sloupce
    # (chat_commands filtruje `note`, hans_idle zobrazuje `note`, zbytek cte
    # `data`) — v obou variantach by pulhotovy zaznam nekomu prosakl jako
    # Hansova znalost. Vlastni studijni tabulka deníkové konzumenty nema.
    _PENDING_DDL = ("CREATE TABLE IF NOT EXISTS study_pending_papers ("
                    "work_id TEXT PRIMARY KEY, topic TEXT, title TEXT, "
                    "year TEXT, url TEXT, abstract TEXT, ts REAL)")

    def _pending_conn(self):
        conn = sqlite3.connect(self._diary_path, timeout=5.0)
        conn.execute(self._PENDING_DDL)
        return conn

    def _park_pending_paper(self, topic, p):
        """Mozek mlci -> praci PODRZ (lossless), ale NEVYDAVEJ za vypisek."""
        ab = (p.get("abstract") or "").strip()
        if len(ab) < 80:
            return False
        wid = (p.get("url") or p.get("title") or "").strip()
        if not wid:
            return False
        try:
            conn = self._pending_conn()
            conn.execute(
                "INSERT OR REPLACE INTO study_pending_papers "
                "(work_id, topic, title, year, url, abstract, ts) "
                "VALUES (?,?,?,?,?,?,?)",
                (wid, topic, (p.get("title") or "").strip(),
                 str(p.get("year") or ""), (p.get("url") or ""),
                 ab[:8000], time.time()))
            conn.commit()
            conn.close()
            _log.info("study: prace ODLOZENA (mozek mimo) — %s", wid)
            return True
        except Exception as e:
            _log.warning("park_pending_paper: %s", e)
            return False

    def _emit_paper_takeaway(self, config, topic, d_title, url, takeaway,
                             knowledge=None):
        """Jedna pravda pro zapis hotoveho vypisku — zivá cesta i dobeh."""
        try:
            conn = sqlite3.connect(self._diary_path)
            conn.execute(
                "INSERT INTO diary (ts, event_type, title, data, "
                "source_url) VALUES (?,?,?,?,?)",
                (time.time(), "reading_takeaway", d_title, takeaway, url))
            conn.commit()
            conn.close()
        except Exception as e:
            _log.warning("paper takeaway zapis selhal: %s", e)
            return False
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
        return True

    def _drain_pending_papers(self, config, knowledge=None, limit=12):
        """Dobeh odlozenych praci. Bezi UVNITR studijniho kroku, tedy uvnitr
        `base_model_batch` (slot uz drzi) — zadny novy kolider na `brain_up`.
        Pri PRVNIM neuspechu koncí: mozek zas mlci, dalsi by selhaly taky."""
        try:
            conn = self._pending_conn()
            rows = conn.execute(
                "SELECT work_id, topic, title, year, url, abstract "
                "FROM study_pending_papers ORDER BY ts ASC LIMIT ?",
                (int(limit),)).fetchall()
        except Exception as e:
            _log.debug("drain_pending_papers query: %s", e)
            return 0
        done = 0
        for wid, ptopic, ptitle, pyear, purl, pab in rows:
            p = {"title": ptitle, "year": pyear, "url": purl,
                 "abstract": pab, "authors": ""}
            takeaway = _distill_paper(config, ptopic or "", p)
            if not takeaway:
                break
            d_title = "%s (%s)" % (ptitle, pyear) if pyear else ptitle
            if not self._emit_paper_takeaway(config, ptopic or "", d_title,
                                             purl, takeaway, knowledge):
                break
            try:
                conn.execute("DELETE FROM study_pending_papers WHERE work_id=?",
                             (wid,))
                conn.commit()
            except Exception as e:
                _log.debug("drain delete %s: %s", wid, e)
            done += 1
        try:
            conn.close()
        except Exception:
            pass
        if done:
            _log.info("study: dobeh odlozenych praci — %d vypisku doplneno", done)
        return done

    def _record_paper_takeaways(self, config, topic, papers, knowledge=None,
                                diary_writer=None):
        """HANS_RESEARCH_PAPER_TAKEAWAY_V1 — z každé odborné práce použité v
        research tieru udělá TRVALÝ per-práce výpisek (reading_takeaway + URL +
        RAG). Dřív po práci zůstal jen titul v study_seen_works → konkrétní
        vědecký přínos se ztrácel. Best-effort, nikdy neshodí studijní krok.
        HANS_PAPER_TAKEAWAY_DEFERRED_V1: když mozek mlčí, práce se ODLOŽÍ
        (podrží se abstrakt) a výpisek se dopíše při dalším studijním kroku —
        surový abstrakt se NIKDY nevydá za Hansův výpisek."""
        rc = _cfg(config).get("research_tier", {}) or {}
        if not rc.get("per_paper_takeaway", True):
            return
        try:
            self._drain_pending_papers(config, knowledge)
        except Exception as e:
            _log.debug("drain_pending_papers: %s", e)
        if not papers:
            return
        for p in papers:
            try:
                title = (p.get("title") or "").strip()
                if not title:
                    continue
                takeaway = _distill_paper(config, topic, p)
                if not takeaway:
                    # mozek mlci -> PODRZ, nefabrikuj (viz komentar vyse)
                    self._park_pending_paper(topic, p)
                    continue
                year = p.get("year") or ""
                d_title = f"{title} ({year})" if year else title
                url = p.get("url") or ""
                if not self._emit_paper_takeaway(config, topic, d_title, url,
                                                 takeaway, knowledge):
                    continue
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
    # HANS_BASE_SLOT_V1 (8.9.) — inline pause+unload nahrazen SDÍLENÝM
    # `base_model_batch`, protože samotný handoff nestačil: studium a další
    # base-model dávky se navzájem nevidí. Doloženo 8.9. — studium jelo cestou
    # „brain_up catchup", immune cestou `_night_tick`; `_creative_busy` je
    # lokální proměnná jednoho ticku, takže mezi těmito dvěma cestami
    # NEEXISTOVALO vzájemné vyloučení. Context manager dělá totéž co dosavadní
    # inline kód (pause_warmup + aktivní unload, resume ve finally) a navíc
    # zabere slot, takže druhá dávka počká místo souběhu.
    try:
        from scripts.ollama_client import base_model_batch as _bmb
    except Exception:
        _bmb = None
    if _bmb is None:
        return _run_study_session_impl(config, diary_db_path, knowledge,
                                       diary_writer)
    with _bmb(config, pause_s=1200, label="studium"):
        return _run_study_session_impl(config, diary_db_path, knowledge,
                                       diary_writer)


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
        try:                                   # HANS_MAIN_CONFIG_IO_V1
            from scripts.config_io import load as _cfg_load
        except ImportError:                    # spusteno jako skript, root chybi
            import sys as _s, os as _o
            _s.path.insert(0, _o.path.dirname(_o.path.dirname(_o.path.abspath(__file__))))
            from scripts.config_io import load as _cfg_load
        cfg = _cfg_load()
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
