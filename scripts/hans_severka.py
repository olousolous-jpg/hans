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
            return [h.as_dict() for h in hs.durable_hobbies(
                self._min_evidence, self._min_age_days, self._min_recent_days)]
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
        hob_block = "\n".join(
            f"- {h['name']} [×{h['evidence_count']}, {h['age_days']} dní]"
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
        prompt = (f"DOSAVADNÍ ROLE (CORE):\n{core}\n\n"
                  f"TRVALÉ TENDENCE — hodnoty/postoje (filtr stálosti):\n{tnd_block}\n\n"
                  f"TRVALÉ KONÍČKY — dlouhodobé zájmy (filtr stálosti):\n{hob_block}\n\n"
                  f"HLOUBKOVÉ STUDIUM — co jsem do hloubky nastudoval (reálná "
                  f"znalost, ne jen zájem):\n{study_block}\n\n"
                  f"SEBE-DEFINUJÍCÍ VZPOMÍNKY — pivotní epizody (kontext pro koherenci):\n{mem_block}\n\n"
                  f"PŘÍBĚH — poslední kapitola (souvislé ohlédnutí za vývojem):\n{chapter}")

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
                options={"temperature": 0.2})
        except Exception as e:
            _log.warning("severka: LLM call failed: %s", e)
            return {"decision": "keep", "gate": True, "durable": durable,
                    "message": "", "error": str(e), "deferred": True}
        if raw is None:
            _log.info("severka: LLM vrátil None (Ollama dole/timeout) → odloženo")
            return {"decision": "keep", "gate": True, "durable": durable,
                    "message": "", "error": "llm None", "deferred": True}

        parsed = self._parse(raw)
        if not parsed or parsed.get("decision") != "propose":
            _log.info("severka %s: LLM rozhodl 'keep' (gate prošel, drift malý)",
                      date_str)
            return {"decision": "keep", "gate": True, "durable": durable,
                    "analysis": (parsed or {}).get("analysis", ""), "message": ""}

        new_core = (parsed.get("proposed_core") or "").strip()
        rationale = (parsed.get("rationale") or "").strip()
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
