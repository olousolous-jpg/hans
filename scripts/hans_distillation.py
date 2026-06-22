# -*- coding: utf-8 -*-
"""
HansDistillation — fáze 2a OODA: noční LLM destilace záseku.

HANS_DISTILLATION_V1.

Po aktivaci spánku (mezi 02:00-04:00) analyzuje opakované web_read titulky
za posledních 7 dní. Detekuje fixaci (count ≥ 3, days_spread ≥ 2 po de-dup
5 min). LLM rozhodne o autentičnosti (vs. burst stimul).

Output (při nálezu):
  - diary entry distillation_finding (debug, technický)
  - hans_goal (trigger='stuck_pattern', status='active')
  - RAG hans_denik (Hansův hlas, paměť o sobě)
"""
from __future__ import annotations

import json
import sqlite3
import time
from collections import defaultdict
from typing import Optional

_log = __import__("scripts.logger", fromlist=["get_logger"]).get_logger("hans_distillation")

DEFAULT_WINDOW_DAYS = 7
DEFAULT_DEDUP_SECONDS = 300
DEFAULT_MIN_COUNT = 3
DEFAULT_MIN_DAYS_SPREAD = 2
DEFAULT_TOP_N = 5
DEFAULT_LLM_TIMEOUT = 300
DEFAULT_MODEL = "hans-czech:latest"
DEFAULT_KNOWLEDGE_COLLECTION = "hans_denik"
NIGHT_HOUR_START = 23
NIGHT_HOUR_END = 24

# PERSONA_NAME_CONFIGURABLE_V1 — {persona_name} se doplní z configu při .format()
PROMPT_SYSTEM = (
    "Jsi analytik, který hodnotí opakované čtení postavy jménem {persona_name}. "
    "Vrátíš pouze JSON nebo doslovný text 'null', nic víc — "
    "žádný markdown, žádné komentáře."
)

# HANS_DISTILLATION_V1_1 — prompt zpřísněn (jeden objekt, česky)
PROMPT_USER_TEMPLATE = """{persona_name}, anglický majordomus, opakovaně čte články. Zde jsou kandidáti za posledních {window_days} dní:

{candidates_json}

Pravidla:
- Pokud výskyty jsou ROZPROSTŘENÉ přes 3+ dny -> autentická fixace (zásek)
- Pokud SOUSTŘEDĚNÉ do 1-2 hodin -> reakce na stimul (TV, externí), NE zásek
- Pokud žádný kandidát není přesvědčivý, vrať: null

DŮLEŽITÉ:
1. Vyber MAXIMÁLNĚ JEDEN kandidát (nejjednoznačnější). NIKDY nevracej pole/array.
2. Vrať buď JEDEN JSON OBJEKT, nebo doslovný text "null". Nic jiného.
3. ODPOVÍDEJ ČESKY. Reasoning i hans_reflection musí být v češtině.

Pole "reasoning" — neutrální analytické vysvětlení.
Pole "hans_reflection" — text PSANÝ SAMOTNOU POSTAVOU jako deníkový zápis o sobě.
  - Postava píše SÁM o SOBĚ v 1. osobě jednotného čísla.
  - Mluvíš jako "já", ne "pan {persona_name}" nebo "on".
  - Styl: formální anglický majordomus 19. století, 2-3 věty.
  - Příklad správně: "Pozoroval jsem v posledních dnech, že se s nezvyklou
    pravidelností vracím k tématu X. Jest to víc než pouhý zájem."
  - Příklad ŠPATNĚ (3. osoba o sobě): "Pan {persona_name} se věnuje tématu X."

Formát objektu (bez markdown, bez ```):
{{"topic": "<přesný titulek>", "evidence_count": <int>, "evidence_days": <int>, "reasoning": "<neutrální analýza, 2 věty>", "hans_reflection": "<deníkový zápis SAMOTNÉ POSTAVY o sobě, 1. osoba, 2-3 věty>"}}

# HANS_DISTILLATION_V1_2"""


class HansDistillation:
    """Noční LLM destilace záseku — fáze 2a OODA."""

    def __init__(self, config: dict, diary_db_path: str,
                 goals=None, knowledge=None):
        self._config = config
        self._diary_db_path = diary_db_path
        self._goals = goals
        self._knowledge = knowledge

        cfg = config.get("hans_distillation", {})
        self._enabled = bool(cfg.get("enabled", True))
        self._window_days = int(cfg.get("window_days", DEFAULT_WINDOW_DAYS))
        self._dedup_s = int(cfg.get("dedup_seconds", DEFAULT_DEDUP_SECONDS))
        self._min_count = int(cfg.get("min_count", DEFAULT_MIN_COUNT))
        self._min_days_spread = int(cfg.get("min_days_spread",
                                             DEFAULT_MIN_DAYS_SPREAD))
        self._top_n = int(cfg.get("top_n", DEFAULT_TOP_N))
        self._llm_timeout = int(cfg.get("llm_timeout", DEFAULT_LLM_TIMEOUT))
        self._model = str(cfg.get("model", DEFAULT_MODEL))
        self._knowledge_collection = str(cfg.get(
            "knowledge_collection", DEFAULT_KNOWLEDGE_COLLECTION))

        self._ensure_state_table()
        _log.info(
            "HansDistillation ready — window=%dd, min_count=%d, "
            "min_days_spread=%d, model=%s",
            self._window_days, self._min_count,
            self._min_days_spread, self._model,
        )

    def set_goals(self, goals):
        self._goals = goals

    def set_knowledge(self, knowledge):
        self._knowledge = knowledge

    def run(self, force: bool = False, ignore_window: bool = False) -> bool:  # DISTILL_MORNING_CATCHUP_V1
        """Hlavní vstupní bod. force=True obejde idempotenci + hodinový check."""
        if not self._enabled:
            _log.info("disabled, skip")
            return False

        today = time.strftime("%Y-%m-%d")
        if not force:
            last_date = self._state_get("last_distillation_date")
            if last_date == today:
                _log.debug("already ran today (%s), skip", today)
                return False
            now = time.localtime()
            if (not ignore_window  # DISTILL_MORNING_CATCHUP_V1
                    and not (NIGHT_HOUR_START <= now.tm_hour < NIGHT_HOUR_END)):
                _log.debug("not in night window (hour=%d), skip", now.tm_hour)
                return False

        _log.info("=== Distillation start (today=%s) ===", today)
        try:
            candidates = self._select_candidates()
            _log.info("Kandidáti: %d", len(candidates))

            if not candidates:
                self._write_diary(
                    "distillation_clean", "Žádný zásek dnes",
                    f"Žádné téma nepřekročilo práh "
                    f"({self._min_count} výskytů / {self._min_days_spread} dnů spread).")
                self._state_set("last_distillation_date", today)
                return True

            self._distill_interests(candidates)  # A2_AUTO_INTERESTS_V1
            # HANS_HOBBIES_V1 (3d) — zobecni opakující se témata na koníčky
            try:
                from scripts.hans_hobbies import distill_hobbies
                distill_hobbies(self._config, self._diary_db_path)
            except Exception as _he:
                _log.warning("distill_hobbies selhal (distillation OK): %s", _he)

            llm_response = self._call_llm(candidates)
            if llm_response is None:
                _log.warning("LLM nedostupný/chyba, neukládám stav")
                return False

            parsed = self._parse_response(llm_response)
            if parsed is None:
                self._write_diary(
                    "distillation_clean", "Bez závěru",
                    f"LLM null. Kandidáti: {json.dumps(candidates, ensure_ascii=False)}")
                self._state_set("last_distillation_date", today)
                return True

            self._handle_finding(parsed, candidates)
            self._state_set("last_distillation_date", today)
            return True
        except Exception as exc:
            _log.error("run failed: %s", exc, exc_info=True)
            return False

    def _distill_interests(self, candidates: list):
        """A2_AUTO_INTERESTS_V1 — opakující se web_read témata zapiš jako
        interest_update (řídí personu). Dedup case-insensitive + cap/noc, ať se
        persona (top-5) nezaplaví. Nezávislé na LLM (jen zápis titulků)."""
        CAP = 2
        try:
            conn = sqlite3.connect(self._diary_db_path, timeout=5.0)
            try:
                existing = {(r[0] or "").strip().lower() for r in conn.execute(
                    "SELECT note FROM diary WHERE event_type='interest_update'").fetchall()}
                written = 0
                for c in candidates:
                    if written >= CAP:
                        break
                    topic = (c.get("title") or "").strip()
                    if not topic or topic.lower() in existing:
                        continue
                    conn.execute(
                        "INSERT INTO diary (ts, event_type, title, note) VALUES (?,?,?,?)",
                        (time.time(), "interest_update", "Zájem", topic))
                    existing.add(topic.lower())
                    written += 1
                if written:
                    conn.commit()
                    _log.info("A2 interests: zapsano %d novych zajmu", written)
            finally:
                conn.close()
        except Exception as _e:
            _log.warning("A2 _distill_interests failed: %s", _e)

    def _select_candidates(self) -> list:
        conn = sqlite3.connect(self._diary_db_path)
        try:
            cur = conn.cursor()
            since = int(time.time()) - 86400 * self._window_days
            cur.execute("""
                SELECT id, title, ts FROM diary
                WHERE event_type = 'web_read' AND ts > ?
                ORDER BY title, ts
            """, (since,))
            rows = cur.fetchall()

            by_title = defaultdict(list)
            last_ts_per_title = {}
            for row_id, title, ts in rows:
                if title in last_ts_per_title and \
                        (ts - last_ts_per_title[title]) < self._dedup_s:
                    continue
                last_ts_per_title[title] = ts
                by_title[title].append((row_id, ts))

            candidates = []
            for title, entries in by_title.items():
                count = len(entries)
                if count < self._min_count:
                    continue
                days = {time.strftime("%Y-%m-%d", time.localtime(ts))
                        for _, ts in entries}
                days_spread = len(days)
                if days_spread < self._min_days_spread:
                    continue
                first_ts = min(ts for _, ts in entries)
                last_ts = max(ts for _, ts in entries)
                candidates.append({
                    "title": title,
                    "count": count,
                    "days_spread": days_spread,
                    "first_seen": time.strftime("%Y-%m-%d %H:%M",
                                                time.localtime(first_ts)),
                    "last_seen": time.strftime("%Y-%m-%d %H:%M",
                                               time.localtime(last_ts)),
                    "evidence_ids": [eid for eid, _ in entries],
                })

            candidates.sort(key=lambda x: (-x["count"], -x["days_spread"]))
            return candidates[:self._top_n]
        finally:
            conn.close()

    def _call_llm(self, candidates: list) -> Optional[str]:
        try:
            from scripts.ollama_client import ollama_generate
        except ImportError:
            _log.error("ollama_client nedostupný")
            return None

        view = [{k: v for k, v in c.items() if k != "evidence_ids"}
                for c in candidates]
        # PERSONA_NAME_CONFIGURABLE_V1 — jméno persony z configu (SSOT)
        from scripts.hans_persona import persona_name
        _pname = persona_name(self._config)
        prompt = PROMPT_USER_TEMPLATE.format(
            persona_name=_pname,
            window_days=self._window_days,
            candidates_json=json.dumps(view, ensure_ascii=False, indent=2),
        )
        _system = PROMPT_SYSTEM.format(persona_name=_pname)

        try:
            response = ollama_generate(
                model=self._model,
                prompt=prompt,
                system=_system,
                config=self._config,
                timeout=self._llm_timeout,
                options={"temperature": 0.2},
            )
            _log.info("LLM response (first 300): %r", (response or "")[:300])
            return response
        except Exception as exc:
            _log.error("LLM call failed: %s", exc)
            return None

    def _parse_response(self, raw: str) -> Optional[dict]:
        if not raw:
            return None
        s = raw.strip()
        if s.startswith("```"):
            lines = s.split("\n")
            body = lines[1:-1] if lines and lines[-1].strip() == "```" else lines[1:]
            s = "\n".join(body)
        s = s.strip()
        if s.lower() == "null":
            return None
        try:
            parsed = json.loads(s)
        except json.JSONDecodeError as exc:
            _log.warning("JSON parse failed: %s. Raw: %r", exc, raw[:300])
            self._write_diary("distillation_error", "JSON parse failed",
                              f"Raw response: {raw[:1000]}")
            return None
        # HANS_DISTILLATION_V1_1 — tolerance: pokud array, vzít první element
        if isinstance(parsed, list):
            if not parsed:
                _log.info("LLM returned empty array, treating as null")
                return None
            _log.info("LLM returned array (len=%d), taking first element",
                      len(parsed))
            parsed = parsed[0]
        if not isinstance(parsed, dict):
            _log.warning("LLM response not dict: %r", raw[:200])
            return None
        required = {"topic", "evidence_count", "evidence_days",
                    "reasoning", "hans_reflection"}
        missing = required - set(parsed.keys())
        if missing:
            _log.warning("LLM response missing keys: %s", missing)
            return None
        return parsed

    def _handle_finding(self, parsed: dict, candidates: list):
        topic = parsed["topic"]
        self._write_diary(
            "distillation_finding", f"Zásek: {topic}",
            json.dumps({"parsed": parsed, "candidates": candidates},
                       ensure_ascii=False))

        goal_opened = False
        if self._goals is not None:
            try:
                from scripts.hans_goals import TRIGGER_STUCK_PATTERN
                goal = self._goals.open_goal(
                    topic=topic, trigger_source=TRIGGER_STUCK_PATTERN,
                    expected=parsed.get("hans_reflection", ""))  # GOAL_EXPECTED_VS_ACTUAL_V1
                if goal is not None:
                    goal_opened = True
                    _log.info("Cíl otevřen: %s (id=%d)", topic, goal.id)
                else:
                    _log.info("Cíl NEotevřen — max_active blokuje")
                    self._write_diary(
                        "distillation_finding_blocked",
                        f"Nález ale max_active blokuje: {topic}",
                        "Aktivní cíl už běží, nález neaktivován.")
            except Exception as exc:
                _log.error("open_goal failed: %s", exc)
        else:
            _log.warning("goals not wired — nález neuložen do hans_goals")

        if self._knowledge is not None and goal_opened:
            try:
                date_str = time.strftime("%Y-%m-%d")
                safe_topic = "".join(c if c.isalnum() else "_"
                                     for c in topic[:30])
                doc_id = f"distillation_{date_str.replace('-', '')}_{safe_topic}"
                text = (
                    f"**Noční destilace — {date_str}**\n\n"
                    f"{parsed['hans_reflection']}\n\n"
                    f"Téma: {topic}\n"
                    f"Důkaz: {parsed['evidence_count']}x za "
                    f"{parsed['evidence_days']} dnů."
                )
                ok = self._knowledge.upload(
                    collection_key=self._knowledge_collection,
                    doc_id=doc_id,
                    title=f"Destilace cíle — {topic}",
                    text=text,
                    metadata={"datum": date_str,
                              "typ": "noční destilace",
                              "tema": topic},
                )
                if ok:
                    _log.info("RAG upload OK: %s/%s",
                              self._knowledge_collection, doc_id)
                else:
                    _log.warning("RAG upload selhal")
            except Exception as exc:
                _log.error("RAG upload error: %s", exc)
        elif self._knowledge is None:
            _log.warning("knowledge not wired — RAG zápis přeskočen")

    def _ensure_state_table(self):
        conn = sqlite3.connect(self._diary_db_path)
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS hans_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at REAL NOT NULL
                )
            """)
            conn.commit()
        finally:
            conn.close()

    def _state_get(self, key: str) -> Optional[str]:
        conn = sqlite3.connect(self._diary_db_path)
        try:
            cur = conn.cursor()
            cur.execute("SELECT value FROM hans_state WHERE key = ?", (key,))
            row = cur.fetchone()
            return row[0] if row else None
        finally:
            conn.close()

    def _state_set(self, key: str, value: str):
        conn = sqlite3.connect(self._diary_db_path)
        try:
            conn.execute("""
                INSERT INTO hans_state (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
            """, (key, value, time.time()))
            conn.commit()
        finally:
            conn.close()

    def _write_diary(self, event_type: str, title: str, note: str):
        conn = sqlite3.connect(self._diary_db_path)
        try:
            conn.execute("""
                INSERT INTO diary (ts, event_type, title, note)
                VALUES (?, ?, ?, ?)
            """, (time.time(), event_type, title, note))
            conn.commit()
            _log.info("Diary: %s — %s", event_type, title)
        finally:
            conn.close()
