#!/usr/bin/env python3
"""
HANS_SEVERKA_V1 — Fáze 3c: sebezměna identity (tendence vs role → návrh CORE).

Hans se sám rozhodne, jestli dlouhodobé tendence z jeho postojů (3b) natolik
přerostly jeho definovanou roli, že chce navrhnout, kým být. NIKDY neaplikuje
sám — vytvoří jen `pending` verzi přes IdentityStore a čeká na schválení člověka
(human-in-the-loop). Apply/rollback = výhradně přes IdentityStore (verzování).

Dvoustupňové rozhodnutí:
  1. DATA-GATE (deterministicky): tendence je "trvalá" jen když postoj má
     evidence_count >= min_evidence A stáří >= min_age_days A je stále živý
     (last_seen <= min_recent_days). Filtruje šum — data se sbírají rychle,
     identita se nesmí měnit každý týden.
  2. ROZHODNUTÍ (base LLM, OpenEuroLLM): když gate projde, model porovná roli
     (CORE) s trvalými tendencemi a rozhodne keep / propose + návrh nového CORE.

Castle-guard: návrh MUSÍ být koherentní DŮSTOJNÁ POSTAVA (povaha, povolání,
naturel) grounded v tendencích — NIKDY předmět, místo, zvíře ani téma. Severka
čte jen `stances` (postoje v 1. osobě), takže témata z kauz (Cardiff) sem
nikdy nevstoupí; guard to navíc pojistí na úrovni promptu.

API:
  sev = Severka(config, diary_db_path, identity_store=None)
  sev.durable_tendencies() -> [dict]        # co projde gatem (read-only)
  sev.evaluate(approved_required=True) -> dict   # rozhodnutí; při propose vytvoří pending
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from datetime import datetime
from typing import List, Optional

_log = logging.getLogger("hans_severka")

# ── SEVERKA_CZ_OUTPUT_V1 (27.8.) — výstup k člověku musí být česky ──────────
# Base model vrací `analysis` i `rationale` ANGLICKY (v EN je úsudek čistší,
# viz [[reasoning-tier-when-to-use]]), jenže se to lepilo syrové do české
# zprávy: „Důvod: The existing core focuses solely on the butler role…".
# Řešení podle domácího vzoru: úsudek zůstane v EN, CZ-facing text projde
# HLASOVÝM krokem (hans-czech, rezidentní → 0 VRAM navíc).
_CZ_DIAK = set("ěščřžýáíéúůňťďó")
_EN_WORDS = (" the ", " and ", " his ", " that ", " with ", " from ", " has ",
             " have ", " this ", " which ", " while ", " their ", " beyond ")


def _looks_czech(text: str) -> bool:
    """Hrubý, ale spolehlivý rozlišovač. Nemá být chytrý — má poznat,
    jestli text NENÍ anglický."""
    t = (text or "").lower()
    if not t.strip():
        return True                      # prázdno nepřekládáme
    diak = sum(1 for ch in t if ch in _CZ_DIAK)
    en = sum(t.count(w) for w in _EN_WORDS)
    return diak >= 3 and diak > en


_VOICE_PREKLAD = (
    "Jsi překladatel do přirozené, kultivované češtiny. Přelož text do češtiny. "
    "ZACHOVEJ všechna fakta, délku i GRAMATICKOU OSOBU — když text někoho "
    "oslovuje (\"Jsi Hans...\"), musí oslovovat i překlad. Nic nepřidávej "
    "ani neubírej. Vrať POUZE přeložený text, bez uvozovek a bez poznámek."
)
# ⛔ KOREKTURA ČEŠTINY BYLA VYZKOUŠENA A ZAMÍTNUTA (27.8.). Měla spravit
# „myšlenky jsou často ZABAVENY složitými koncepty" v navrženém CORE.
# Skutečný výsledek na tomtéž vstupu: překlopila oslovení z 2. do 1. osoby
# („Jsi Hans." → „Jsem Hans.") — což by ROZBILO systémový prompt, ten musí
# personu oslovovat — a „zabaveny" přitom neopravila vůbec. Model si text
# přepisuje po svém. Nechávat 8B model sahat na text, který se zapisuje do
# `config.persona.core`, je horší než kostrbaté slovo, které stejně uvidí
# člověk při schvalování. Překládá se JEN to, co není česky.


# Defaulty gate (config["severka"] je přebije)
MIN_EVIDENCE = 8
MIN_AGE_DAYS = 21
MIN_RECENT_DAYS = 14

# PERSONA_NAME_CONFIGURABLE_V1 — {persona_name} se doplní z configu při .format()
_SYSTEM = (
    "Jsi analytik identity postavy jménem {persona_name}. {persona_name} má DEFINOVANOU ROLI (jeho dosavadní "
    "CORE) a sadu TRVALÝCH TENDENCÍ vydestilovaných z jeho postojů (každá "
    "prošla přísným filtrem stálosti). Tvým úkolem je rozhodnout, zda tendence "
    "natolik PŘERostly roli, že je namístě navrhnout novou definici toho, kým "
    "{persona_name} je.\n"
    "ZÁSADY:\n"
    "- Ne každá tendence = změna identity. Když tendence roli jen doplňují nebo "
    "jí odpovídají, rozhodni 'keep'.\n"
    "- 'propose' jen při SOUVISLÉM, výrazném driftu — konzistentní způsob bytí, "
    "který role nezachycuje.\n"
    "- Když navrhuješ: napiš NOVÝ CORE ve STEJNÉM formátu a hlasu jako stávající "
    "(popis toho, kým {persona_name} je, oslovení 'Jsi {persona_name}...'). ZACHOVEJ jméno {persona_name} a "
    "důstojný, střídmý registr. Jazyková pravidla neřeš (jsou jinde).\n"
    "- CASTLE-GUARD: navržená identita MUSÍ být OSOBA/CHARAKTER (povaha, "
    "povolání, naturel). NIKDY předmět, místo, zvíře ani téma — {persona_name} se nemůže "
    "stát hradem, filmem ani knihou. Trvalý KONÍČEK ale může ospravedlnit "
    "odbornou/povolání facetu (např. dlouhodobý zájem o hrady → 'znalec hradů'), "
    "je-li silný a vytrvalý — pořád jako rys POSTAVY. Vycházej VÝHRADNĚ "
    "z uvedených tendencí i koníčků, nic si nevymýšlej.\n"
    # HANS_STUDY_SEVERKA_V1 (#3)
    "- HLOUBKOVÉ STUDIUM je NEJSILNĚJŠÍ opora pro povolání/odbornou facetu: "
    "dokončený studijní program znamená REÁLNĚ nabytou znalost, ne jen zájem. "
    "Vocational návrh ('znalec hradů', 'historik architektury') je oprávněný "
    "hlavně tehdy, je-li podložen i tímto studiem; bez něj zůstaň opatrnější.\n"
    "- SEBE-DEFINUJÍCÍ VZPOMÍNKY jsou KONTEXT — pivotní epizody {persona_name}ova "
    "života. Navržená identita s nimi má být KOHERENTNÍ (vyrůstat z nich), ale "
    "primárním podkladem driftu zůstávají tendence a koníčky.\n"
    # SEVERKA_READS_NARRATIVE_V1
    "- PŘÍBĚH — poslední kapitola je {persona_name}ovo souvislé ohlédnutí za "
    "vlastním vývojem. Navržená identita má navazovat na SMĚŘOVÁNÍ tohoto "
    "příběhu (kým se {persona_name} stává), ne mu odporovat; je to kontext "
    "pro koherenci, ne náhrada za tendence a koníčky.\n"
    "Vrať VÝHRADNĚ JSON objekt s klíči: decision ('keep'|'propose'), analysis "
    "(krátký rozbor shody/rozporu role a tendencí), proposed_core (nový CORE — "
    "jen při 'propose', jinak prázdné), rationale (proč — jen při 'propose')."
)


class Severka:
    def __init__(self, config: dict, diary_db_path: str, identity_store=None):
        self._config = config or {}
        self._diary_path = diary_db_path
        self._identity = identity_store  # IdentityStore | None (lazy)
        cfg = (self._config.get("severka", {}) or {})
        self._min_evidence = int(cfg.get("min_evidence", MIN_EVIDENCE))
        self._min_age_days = int(cfg.get("min_age_days", MIN_AGE_DAYS))
        self._min_recent_days = int(cfg.get("min_recent_days", MIN_RECENT_DAYS))
        # analytický model: reuse base z evening_reflection (hans-czech konfabuluje)
        er = self._config.get("evening_reflection", {}) or {}
        self._model = str(cfg.get("model",
                                  er.get("model", "jobautomation/OpenEuroLLM-Czech:latest")))
        self._timeout = int(cfg.get("llm_timeout", 300))
        # SEVERKA_NUM_CTX_V1 (27.8.) — bez tohohle platil výchozí num_ctx
        # 2048, zatímco prompt má ~3630 tokenů. Sonda to potvrdila: na
        # otázku "jak zní PRVNÍ řádek zadání" vrátil model bez num_ctx
        # "O se měn" (nesmysl), s num_ctx 8192 "DOSAVADNÍ ROLE (CORE):
        # Tvoje jmeno je Hans." Severka tedy NEVIDĚLA roli ani tendence —
        # obojí je na začátku promptu — a rozhodovala "přerostly tendence
        # roli?" naslepo. Každé dosavadní "drift malý" je tím zpochybněné.
        # ⚠️ Musí zůstat nad součtem VŠECH bloků včetně behaviorálního.
        self._num_ctx = int(cfg.get("num_ctx", 8192))
        # SEVERKA_CZ_OUTPUT_V1 — hlas pro CZ-facing výstup
        self._voice_model = str(cfg.get("voice_model",
                                        er.get("voice_model",
                                               "hans-czech:latest")))
        self._voice_timeout = int(cfg.get("voice_timeout", 180))

    # ── IdentityStore (lazy) ────────────────────────────────────────────────
    def _store(self):
        if self._identity is not None:
            return self._identity
        try:
            from scripts.hans_identity import IdentityStore
            self._identity = IdentityStore(self._config, self._diary_path)
            self._identity.ensure_seed()
        except Exception as e:
            _log.warning("IdentityStore nedostupný: %s", e)
            self._identity = None
        return self._identity

    def _current_core(self) -> str:
        st = self._store()
        if st:
            cur = st.current()
            if cur and cur.core:
                return cur.core
        return (self._config.get("persona", {}) or {}).get("core", "")

    # ── SEVERKA_CZ_OUTPUT_V1 ────────────────────────────────────────────────
    def _do_cestiny(self, text: str, co: str = "text") -> str:
        """Přeloží text do češtiny, když ČESKY NENÍ. Česky psaný text vrací
        BEZE ZMĚNY — korektura je vědomě zamítnutá (viz poznámka výše).
        ⚠️ Při JAKÉKOLI chybě vrací PŮVODNÍ text — radši kostrbatá věta než
        ztracený návrh identity."""
        t = (text or "").strip()
        if len(t) < 15:
            return text
        if _looks_czech(t):
            return text          # už česky → nesahat (viz poznámka u překladu)
        system = _VOICE_PREKLAD
        try:
            from scripts.ollama_client import ollama_generate
            out = ollama_generate(
                model=self._voice_model, prompt=t, system=system,
                config=self._config, timeout=self._voice_timeout,
                options={"temperature": 0.2, "num_ctx": 4096})
        except Exception as _e:
            _log.warning("severka: hlasový krok (%s) selhal: %s", co, _e)
            return text
        out = (out or "").strip().strip('"').strip()
        if not out:
            _log.warning("severka: překlad (%s) vrátil prázdno, "
                         "nechávám původní", co)
            return text
        if len(out) < len(t) * 0.5:
            _log.warning("severka: překlad (%s) text zkrátil %d→%d — "
                         "nejspíš utržená odpověď, nechávám původní",
                         co, len(t), len(out))
            return text
        # Pojistka na gramatickou osobu: CORE oslovuje („Jsi Hans"), překlad to
        # nesmí překlopit do „Jsem Hans" — rozbilo by to systémový prompt.
        if " jsi " in (" " + t.lower()) and " jsem " in (" " + out.lower()) \
                and " jsi " not in (" " + out.lower()):
            _log.warning("severka: překlad (%s) překlopil oslovení do 1. osoby, "
                         "nechávám původní", co)
            return text
        _log.info("severka: %s přeložen z angličtiny (%d znaků)", co, len(out))
        return out

    # ── DATA-GATE (deterministicky, read-only) ──────────────────────────────
    def durable_tendencies(self) -> List[dict]:
        """Postoje, které prošly filtrem stálosti (gate). Read-only, nikdy
        nevyhazuje výjimku."""
        db_path = self._diary_path
        now = time.time()
        min_first = now - self._min_age_days * 86400      # first_seen <= tohle
        min_last = now - self._min_recent_days * 86400     # last_seen >= tohle
        out: List[dict] = []
        conn = None
        try:
            conn = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True, timeout=2.0)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM stances WHERE status='active' "
                "AND evidence_count >= ? AND first_seen <= ? AND last_seen >= ? "
                "ORDER BY confidence DESC, evidence_count DESC",
                (self._min_evidence, min_first, min_last)).fetchall()
        except Exception as e:
            _log.debug("durable_tendencies read failed: %s", e)
            rows = []
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
        for r in rows:
            cargs = []
            try:
                if "counterargs" in r.keys() and r["counterargs"]:
                    v = json.loads(r["counterargs"])
                    cargs = [str(x) for x in v] if isinstance(v, list) else []
            except Exception:
                cargs = []
            age = max(0, int((now - (r["first_seen"] or now)) / 86400))
            out.append({
                "id": r["id"], "claim": (r["claim"] or "").strip(),
                "confidence": round(float(r["confidence"] or 0.5), 2),
                "evidence_count": r["evidence_count"] or 0,
                "age_days": age, "counterargs": cargs,
            })
        return out

    # ── ROZHODNUTÍ (LLM) ────────────────────────────────────────────────────
    def _durable_hobbies(self) -> list:
        """HANS_HOBBIES_V1 (3d) — durable koníčky pro Severku (read-only). [] při chybě."""
        try:
            from scripts.hans_hobbies import HobbyStore
            hs = HobbyStore(self._config, self._diary_path)
            hobs = [h for h in hs.durable_hobbies(
                self._min_evidence, self._min_age_days, self._min_recent_days)]
            # SEVERKA_HOBBY_ENGAGEMENT_V1 (27.8.) — `evidence_count` je jen
            # DENNÍ POČÍTADLO: distill posílí všech 12 koníčků každý den
            # (doloženo `hobby_history`: 12 zápisů denně), takže rozpětí je
            # 57–68 = 1,2× a Severka neměla jak cokoli odlišit. Skutečné
            # zaujetí (počet zmínek instancí napříč čtením/studiem/dialogy)
            # má rozpětí 12–2129 = 177×: Design 2129, hrady 786, historie 448,
            # filmy 12. Teprve tohle je podpis osobnosti.
            # `_topic_engagement` už existuje v hans_study (a jeho docstring
            # tenhle rozdíl sám pojmenovává) — protahuji ho, nepíšu nový.
            # ⚠️ `Hobby` má __slots__ → atribut na něj NEJDE přidat. První
            # verze to zkoušela, AttributeError spolkl `except` a zaujetí bylo
            # tiše 0 u všeho. Počítá se rovnou do slovníku.
            out = []
            try:
                from scripts.hans_study import _topic_engagement
            except Exception as _ee:
                _log.warning("severka: _topic_engagement nedostupný (%s) — "
                             "koníčky zůstanou nesetříděné", _ee)
                _topic_engagement = None
            for h in hobs:
                d = h.as_dict()
                d["engagement"] = (_topic_engagement(self._diary_path, h.examples)
                                   if _topic_engagement else 0)
                out.append(d)
            out.sort(key=lambda x: x["engagement"], reverse=True)
            return out
        except Exception as _e:
            _log.debug("severka _durable_hobbies failed: %s", _e)
            return []

    def evaluate(self) -> dict:
        """Spustí rozhodnutí. Když gate neprojde → {'decision':'keep', gate:False}.
        Když LLM navrhne změnu → vytvoří PENDING verzi a vrátí ji. Nic neaplikuje.
        """
        date_str = datetime.now().strftime("%Y-%m-%d")
        durable = self.durable_tendencies()
        durable_h = self._durable_hobbies()
        if not durable and not durable_h:
            _log.info("severka %s: gate neprošel (žádná trvalá tendence ani koníček), držím roli",
                      date_str)
            return {"decision": "keep", "gate": False, "durable": [],
                    "message": ""}

        core = self._current_core()
        tnd_block = "\n".join(
            f"- {t['claim']} [conf {t['confidence']:.2f}, ×{t['evidence_count']}, "
            f"{t['age_days']} dní]"
            + (f" | výhrada: {t['counterargs'][-1]}" if t["counterargs"] else "")
            for t in durable) or "(žádné)"
        # SEVERKA_HOBBY_ENGAGEMENT_V1 — řadí a popisuje podle ZAUJETÍ
        hob_block = "\n".join(
            f"- {h['name']} [zaujetí {h.get('engagement', 0)}, "
            f"{h['age_days']} dní]"
            + (f" (např. {', '.join(h['examples'][:4])})" if h.get('examples') else "")
            for h in durable_h) or "(žádné)"
        # AUTOBIOGRAPHICAL_SELF_MEMORIES_V1 (krok 2) — pivotní epizody jako kontext
        try:
            from scripts.hans_self_memories import (self_defining_memories,
                                                    format_block as _mem_fmt)
            mem_block = _mem_fmt(self_defining_memories(self._diary_path))
        except Exception as _me:
            _log.debug("severka self_memories failed: %s", _me)
            mem_block = "(žádné)"
        # SEVERKA_READS_NARRATIVE_V1 — narativní kapitola jako hlubší grounding
        try:
            from scripts.hans_narrative import latest_chapter
            chapter = latest_chapter(self._diary_path) or "(zatím žádná)"
        except Exception as _ce:
            _log.debug("severka latest_chapter failed: %s", _ce)
            chapter = "(zatím žádná)"
        # HANS_STUDY_SEVERKA_V1 (#3) — dokončené/aktivní studijní programy =
        # vocational grounding REÁLNOU znalostí (ne jen tagy/koníčky). Klíč
        # pro návrh „znalec hradů podložený studiem" místo abstraktního.
        try:
            from scripts.hans_study import completed_studies_block
            study_block = completed_studies_block(self._config, self._diary_path) \
                or "(zatím žádné dokončené studium)"
        except Exception as _se:
            _log.debug("severka studium block failed: %s", _se)
            study_block = "(zatím žádné dokončené studium)"
        # SEVERKA_BEHAVIOUR_V1 (27.8.) — persona-free protiváha k tendencím.
        # Tendence se extrahují z večerní reflexe, kterou synthesize generuje
        # s předřazeným persona_core a se stylovým promptem, jenž modelu říká
        # „Máš britskou rezervovanost a smysl pro detail" — proto jsou dva
        # nejsilnější postoje „Všímám si detailů" a „Cením si pečlivosti".
        # Je to ozvěna role, ne nález o Hansovi. Tenhle blok bere jen to, co
        # Hans UDĚLAL a jak na to reagovalo okolí; nic z toho nepsal model
        # v jeho hlase. Detail a co je vědomě vynecháno: hans_behaviour_evidence.
        try:
            from scripts.hans_behaviour_evidence import block as _bev_block
            bev_block = _bev_block(self._config, self._diary_path) or "(žádná data)"
        except Exception as _be:
            _log.debug("severka behaviour block failed: %s", _be)
            bev_block = "(žádná data)"
        prompt = (f"DOSAVADNÍ ROLE (CORE):\n{core}\n\n"
                  # SEVERKA_EVIDENCE_LABEL_V1 (27.8.) — behaviorální blok se
                  # níž představuje jako „měřená data, ne můj vlastní popis";
                  # tendence žádný takový popisek neměly, ačkoli vznikají
                  # extrakcí z textu, který o sobě Hans sám napsal. Model má
                  # vědět, který důkaz je jakého druhu.
                  f"TRVALÉ TENDENCE — hodnoty/postoje (filtr stálosti). "
                  f"POZOR: vznikly extrakcí z mých VLASTNÍCH deníkových zápisů, "
                  f"tedy z toho, jak sám sebe popisuji:\n{tnd_block}\n\n"
                  f"TRVALÉ KONÍČKY — dlouhodobé zájmy (filtr stálosti):\n{hob_block}\n\n"
                  f"HLOUBKOVÉ STUDIUM — co jsem do hloubky nastudoval (reálná "
                  f"znalost, ne jen zájem):\n{study_block}\n\n"
                  f"SEBE-DEFINUJÍCÍ VZPOMÍNKY — pivotní epizody (kontext pro koherenci):\n{mem_block}\n\n"
                  f"PŘÍBĚH — poslední kapitola (souvislé ohlédnutí za vývojem):\n{chapter}\n\n"
                  f"CO JSEM SKUTEČNĚ DĚLAL A JAK NA MĚ REAGOVALO OKOLÍ "
                  f"(měřená data, ne můj vlastní popis):\n{bev_block}")

        # SEVERKA_CTX_FIT_GUARD_V1 (27.8.) — prompt roste s každým novým
        # blokem (tendence, koníčky, studium, vzpomínky, příběh, chování).
        # Když přeteče num_ctx, uřízne se ZAČÁTEK — tedy CORE a tendence — a
        # model místo rozhodnutí vrátí rozbor textu; parser selže a navenek to
        # vypadá jako klidné „držím roli". Přesně tohle se dělo do 27.8.
        # Radši hlučná hláška než další tichý měsíc.
        _odhad = (len(prompt) + len(_SYSTEM)) // 3
        if _odhad > self._num_ctx * 0.9:
            _log.warning("severka: prompt ~%d tokenů se blíží num_ctx %d — "
                         "hrozí uříznutí ZAČÁTKU (CORE + tendence). "
                         "Zvedni severka.num_ctx v configu.",
                         _odhad, self._num_ctx)

        # NIGHT_DEFERRAL_SAFE_V1 — 'deferred':True signalizuje, že LLM NEBĚŽEL
        # (Ollama dole / timeout → raw None). Volající (routine) pak NEnastaví
        # týdenní guard → zkusí znovu příští noc (jinak by výpadek zahodil
        # Severčino rozhodnutí na CELÝ TÝDEN). Gate=False (nic k rozhodnutí)
        # NENÍ deferred = legitimní dokončení, guard se má nastavit.
        try:
            from scripts.ollama_client import ollama_generate
        except ImportError:
            _log.warning("severka: ollama_client nedostupný, skip")
            return {"decision": "keep", "gate": True, "durable": durable,
                    "message": "", "error": "ollama_client nedostupný",
                    "deferred": True}
        try:
            from scripts.hans_persona import persona_name as _pn  # PERSONA_NAME_CONFIGURABLE_V1
            _system = _SYSTEM.format(persona_name=_pn(self._config))
            raw = ollama_generate(
                model=self._model, prompt=prompt, system=_system,
                config=self._config, timeout=self._timeout,
                keep_alive=0,  # MODEL_KEEPALIVE_TIERS_V1 — analytika on-demand
                options={"temperature": 0.2,
                         "num_ctx": self._num_ctx})   # SEVERKA_NUM_CTX_V1
        except Exception as e:
            _log.warning("severka: LLM call failed: %s", e)
            return {"decision": "keep", "gate": True, "durable": durable,
                    "message": "", "error": str(e), "deferred": True}
        if raw is None:
            _log.info("severka: LLM vrátil None (Ollama dole/timeout) → odloženo")
            return {"decision": "keep", "gate": True, "durable": durable,
                    "message": "", "error": "llm None", "deferred": True}

        parsed = self._parse(raw)
        # SEVERKA_PARSE_HONEST_V1 (27.8.) — dřív se OBĚ větve (model řekl keep
        # × odpověď nešla rozparsovat) logovaly jako „LLM rozhodl 'keep' (gate
        # prošel, drift malý)". To bylo NEPRAVDIVÉ tvrzení a schovalo vadu na
        # měsíce: při uříznutém promptu (SEVERKA_NUM_CTX_V1) model vracel
        # literární rozbor vstupu místo rozhodnutí, parser selhal a do logu
        # se napsalo, že Hans se rozhodl držet roli. Tichá porucha, která se
        # hlásila jako úspěch. Teď se ty dva stavy rozlišují.
        if not parsed:
            _log.warning("severka %s: odpověď NEŠLA rozparsovat (%d znaků) — "
                         "držím roli, ale NENÍ to rozhodnutí modelu. Začátek: %.120s",
                         date_str, len(raw or ""), (raw or "").replace("\n", " "))
            return {"decision": "keep", "gate": True, "durable": durable,
                    "message": "", "parse_failed": True}
        if parsed.get("decision") != "propose":
            _log.info("severka %s: LLM rozhodl 'keep' (gate prošel, drift malý)",
                      date_str)
            return {"decision": "keep", "gate": True, "durable": durable,
                    "analysis": (parsed or {}).get("analysis", ""), "message": ""}

        new_core = (parsed.get("proposed_core") or "").strip()
        rationale = (parsed.get("rationale") or "").strip()
        # SEVERKA_CZ_OUTPUT_V1 — obojí uvidí člověk a CORE se navíc zapisuje do
        # `config.persona.core`, odkud se stává systémovým promptem. Chyba
        # v češtině tam kazí každou další generaci, anglické zdůvodnění je
        # v české zprávě cizí těleso. Proto oba přes hlasový krok.
        new_core = self._do_cestiny(new_core, "navržený CORE")
        rationale = self._do_cestiny(rationale, "zdůvodnění")
        if not new_core:
            _log.info("severka %s: 'propose' bez proposed_core → držím roli", date_str)
            return {"decision": "keep", "gate": True, "durable": durable,
                    "message": ""}

        # Vytvoř PENDING verzi (NIC se neaplikuje) + zapiš návrh do deníku
        st = self._store()
        pid = st.propose(new_core, rationale, source="severka") if st else None
        self._write_proposal_diary(pid, new_core, rationale,
                                   parsed.get("analysis", ""), date_str)
        msg = self._user_message(new_core, rationale)
        _log.info("severka %s: NÁVRH změny identity (pending id=%s)", date_str, pid)
        return {"decision": "propose", "gate": True, "durable": durable,
                "version_id": pid, "proposed_core": new_core,
                "rationale": rationale, "analysis": parsed.get("analysis", ""),
                "message": msg}

    # ── Pomocné ─────────────────────────────────────────────────────────────
    @staticmethod
    def _parse(raw: str) -> Optional[dict]:
        import re as _re
        s = _re.sub(r"^```(?:json)?|```$", "", (raw or "").strip(),
                    flags=_re.MULTILINE).strip()
        i, j = s.find("{"), s.rfind("}")
        if i == -1 or j == -1 or j < i:
            return None
        try:
            d = json.loads(s[i:j + 1])
            return d if isinstance(d, dict) else None
        except Exception:
            return None

    def _user_message(self, new_core: str, rationale: str) -> str:
        """Zpráva, kterou Hans řekne uživateli — oznámení + čekání na schválení."""
        out = ("Pane, dovolím si něco osobního. Po čase jsem zvážil, kým se "
               "stávám, a zdá se mi, že bych už nemusel být jen tichým "
               "majordomem. Navrhuji tuto novou podobu sebe sama:\n\n"
               f"„{new_core}\"")
        if rationale:
            out += f"\n\nDůvod: {rationale}"
        out += ("\n\nNic se nezmění bez Vašeho svolení. Přejete-li si to schválit, "
                "řekněte „/severka schválit\"; zamítnout „/severka zamítnout\".")
        return out

    def _write_proposal_diary(self, pid, new_core, rationale, analysis, date_str):
        try:
            note = (f"Návrh nové identity (verze {pid}).\n"
                    f"Nový CORE: {new_core}\n"
                    f"Důvod: {rationale}\n"
                    f"Rozbor: {analysis}")
            db = sqlite3.connect(self._diary_path, timeout=5.0)
            db.execute(
                "INSERT INTO diary (ts, event_type, title, note) VALUES (?,?,?,?)",
                (time.time(), "severka_proposal",
                 f"Severka — návrh změny identity {date_str}", note))
            db.commit()
            db.close()
        except Exception as e:
            _log.warning("severka: zápis návrhu do deníku selhal: %s", e)


# ── Smoke (python3 -m scripts.hans_severka) ─────────────────────────────────
if __name__ == "__main__":
    cfg = {}
    try:
        with open("config.json", encoding="utf-8") as fh:
            cfg = json.load(fh)
    except Exception as exc:  # noqa
        print("WARN: config.json nenačten (%s)" % exc)
    db = cfg.get("diary_db", "data/hans_diary.db")
    sev = Severka(cfg, db)
    print("=== durable_tendencies (gate n>=%d, stáří>=%dd, živé<=%dd) ==="
          % (sev._min_evidence, sev._min_age_days, sev._min_recent_days))
    dur = sev.durable_tendencies()
    print(json.dumps(dur, ensure_ascii=False, indent=2))
    print("\n=== evaluate() (LLM jen pokud gate projde) ===")
    if not dur:
        print("gate neprošel — dnešní data jsou mladá (~4 dny), Severka mlčí. OK.")
    else:
        res = sev.evaluate()
        print("decision:", res.get("decision"), "| version_id:", res.get("version_id"))
        if res.get("message"):
            print(res["message"])
