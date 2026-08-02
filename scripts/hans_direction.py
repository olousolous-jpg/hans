"""scripts/hans_direction.py

HANS_DIRECTION_V1 — intencionální vrstva: sebou zvolený SMĚR/aspirace.

Motiv: Hans má bohatou mašinerii na seberozvoj (studium, tvorba, Severka,
self-insight), ale skoro celá je REAKTIVNÍ — témata krystalizují z toho, co
kolem propluje (Kodi/chat → koníčky → studium). Chybí volní, DOPŘEDU hledící
vrstva: „Tohle jsem se naučil, tohle jsem vytvořil, tímhle se stávám — co teď
CHCI dělat dál?" — sebou zvolený záměr, ke kterému se vrací a který pak řídí,
co studuje a tvoří. To je rozdíl mezi *učí se, co je před ním* a *rozvíjí sám
sebe*.

Vzor zrcadlí Severku (`hans_severka`), ale liší se VÝSTUPEM i ÚČINKEM:
  - Severka = kdo JSEM (persona/naturel/CORE) — zpětně krystalizovaná identita.
  - Direction = k čemu SMĚŘUJI (dopředná aspirace) — biasuje výběr studia +
    dává dopředný sebe-narativ.

Tok (jako Severka): data-gate (deterministicky) → úsudek (reasoning tier, BASE
model — hans-czech konfabuluje) → propose PENDING → ask-first schválení → apply.

Anti-konfabulace: směr MUSÍ být grounded v reálných signálech (studium, díla,
tendence, self-insighty) a MUSÍ je citovat; prázdná data = „drž, nevymýšlej".
Nic se neaplikuje bez schválení uživatelem.

API:
  d = HansDirection(config, diary_db_path)
  d.gather_signals() -> dict            # co Hans reálně má (read-only)
  d.evaluate() -> dict                  # rozhodnutí; při propose vytvoří pending
  DirectionStore(config, db).current_active() -> dict|None
  active_direction_line(config, db) -> str   # věta do system-prompt groundingu
"""
from __future__ import annotations

import json
import re as _re
import time
from datetime import datetime
from typing import List, Optional

from scripts.logger import get_logger

_log = get_logger("hans_direction")

_THINK_RE = _re.compile(r"<think>.*?</think>", _re.IGNORECASE | _re.DOTALL)
_THINK_OPEN_RE = _re.compile(r"<think>.*", _re.IGNORECASE | _re.DOTALL)


def _strip_think(text: str) -> str:
    t = _THINK_RE.sub("", text or "")
    t = _THINK_OPEN_RE.sub("", t)   # neuzavřený <think> = uříznuto → zahodit
    return t.strip()

# gate: kolik substance je potřeba, aby vůbec šlo o „směr" (ne z prázdna)
MIN_STUDY_DONE = 1        # aspoň 1 dostudovaná doména
MIN_SIGNALS = 3           # aspoň tolik nenprázdných signálních bloků

_DIRECTION_SYSTEM = (
    "Jsi {persona_name} — přemýšlíš sám o sobě. Níže jsou REÁLNÉ signály o tom, "
    "co ses do hloubky naučil, co jsi vytvořil, jaké máš dlouhodobé tendence a "
    "zájmy, a čeho sis u sebe všiml. Tvým úkolem je zvážit, jestli z nich "
    "vyplývá SMĚR — sebou zvolený záměr, ke kterému chceš vědomě směřovat "
    "(např. propojit dvě oblasti, prohloubit řemeslo, vytvořit určitý druh "
    "díla). NE persona ani charakter — to řeší jiná vrstva; TADY jde o to, k "
    "čemu chceš růst.\n"
    "PRAVIDLA:\n"
    "- Směr MUSÍ vyrůstat z předložených signálů a MUSÍ je konkrétně jmenovat "
    "(evidence). Nevymýšlej oblasti, které tam nejsou.\n"
    "- Hledej TEMATICKOU LINKU napříč studiem, díly a zájmy (např. opakující se "
    "oblast, řemeslo nebo estetika). Když ji najdeš, POJMENUJ ji jako směr — "
    "právě proto tu jsi. Nečekej na dokonalý signál.\n"
    "- Máš-li už AKTIVNÍ SMĚR: 'keep' pokud pořád sedí; 'evolve' jen když se "
    "signály zřetelně posunuly. Nemáš-li žádný a je tu zřetelná tematická linka: "
    "'propose'.\n"
    "- Když navrhuješ: 'proposed_direction' je JEDNA až DVĚ věty v první osobě, "
    "tvým hlasem, konkrétní a vykonatelná (dá se podle ní vybrat téma / dílo). "
    "NE mlhavá aspirace typu 'chci být lepší'.\n"
    "- Slabé/rozporné signály → 'keep' a prázdný návrh. Radši nic než vymyšlený "
    "směr.\n"
    "Vrať VÝHRADNĚ JSON objekt s klíči: decision ('keep'|'propose'|'evolve'), "
    "analysis (krátký rozbor), proposed_direction (jen při propose/evolve), "
    "evidence (které signály směr podpírají — jen při propose/evolve), "
    "rationale (proč — jen při propose/evolve)."
)

# STANCE_REASONING_TIER_V1 vzor: reasoning model (qwen3) poslouchá EN líp než CZ.
# Úsudek anglicky, český HLAS směru pak přes voice krok (hans-czech).
_DIRECTION_SYSTEM_EN = (
    "You are {persona_name}, reflecting on yourself. Below are REAL signals about "
    "what you have studied in depth, what you have created, your long-term "
    "tendencies and interests. Decide whether they imply a DIRECTION — a "
    "self-chosen intention you want to deliberately grow toward (e.g. combining "
    "two areas, deepening a craft, making a certain kind of work). NOT persona "
    "or character — that is handled elsewhere; HERE it is about what you want to "
    "grow toward.\n"
    "RULES:\n"
    "- Look for a THEMATIC LINK across studies, works and interests (a recurring "
    "area, craft or aesthetic). When you find one, NAME it as a direction — that "
    "is exactly your job. Do not wait for a perfect signal.\n"
    "- If you already have an ACTIVE DIRECTION: 'keep' if it still fits; 'evolve' "
    "only if signals clearly shifted. If none and there is a clear thematic link: "
    "'propose'.\n"
    "- proposed_direction: ONE or TWO concrete, actionable sentences (a topic or "
    "work can be chosen from it). Not vague ('I want to be better').\n"
    "- Weak/contradictory signals → 'keep' and empty proposal. Better nothing "
    "than an invented direction.\n"
    "Return ONLY a JSON object with keys: decision ('keep'|'propose'|'evolve'), "
    "analysis (short), proposed_direction (only on propose/evolve), evidence (a "
    "list of the concrete signals that support it — only on propose/evolve), "
    "rationale (why — only on propose/evolve). Write the JSON values in English."
)


# ── Storage ─────────────────────────────────────────────────────────────────
class DirectionStore:
    def __init__(self, config: dict, diary_db_path: str):
        self._config = config or {}
        self._db = diary_db_path
        self._ensure()

    def _connect(self):
        import sqlite3
        c = sqlite3.connect(self._db, timeout=10)
        c.row_factory = sqlite3.Row
        return c

    def _ensure(self):
        try:
            c = self._connect()
            c.execute(
                "CREATE TABLE IF NOT EXISTS hans_directions ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, "
                "direction TEXT NOT NULL, evidence TEXT NOT NULL DEFAULT '', "
                "rationale TEXT NOT NULL DEFAULT '', "
                "status TEXT NOT NULL DEFAULT 'pending', "  # pending|active|rejected|superseded
                "source_model TEXT NOT NULL DEFAULT '', "
                "decided_ts REAL NOT NULL DEFAULT 0)")
            c.commit()
            c.close()
        except Exception as e:
            _log.warning("hans_directions ensure: %s", e)

    def current_active(self) -> Optional[dict]:
        try:
            c = self._connect()
            r = c.execute("SELECT * FROM hans_directions WHERE status='active' "
                          "ORDER BY id DESC LIMIT 1").fetchone()
            c.close()
            return dict(r) if r else None
        except Exception as e:
            _log.warning("current_active: %s", e)
            return None

    def pending(self) -> Optional[dict]:
        try:
            c = self._connect()
            r = c.execute("SELECT * FROM hans_directions WHERE status='pending' "
                          "ORDER BY id DESC LIMIT 1").fetchone()
            c.close()
            return dict(r) if r else None
        except Exception:
            return None

    def all(self, limit: int = 20) -> List[dict]:
        try:
            c = self._connect()
            rows = c.execute("SELECT * FROM hans_directions ORDER BY id DESC "
                             "LIMIT ?", (int(limit),)).fetchall()
            c.close()
            return [dict(r) for r in rows]
        except Exception:
            return []

    def propose(self, direction: str, evidence: str, rationale: str,
                source_model: str = "") -> Optional[int]:
        """Ulož PENDING návrh. Nahradí předchozí pending (jen jeden čeká)."""
        try:
            c = self._connect()
            c.execute("UPDATE hans_directions SET status='superseded' "
                      "WHERE status='pending'")
            cur = c.execute(
                "INSERT INTO hans_directions (ts, direction, evidence, rationale, "
                "status, source_model) VALUES (?,?,?,?,'pending',?)",
                (time.time(), direction, evidence, rationale, source_model))
            c.commit()
            pid = cur.lastrowid
            c.close()
            return pid
        except Exception as e:
            _log.warning("propose: %s", e)
            return None

    def approve(self, pid: Optional[int] = None) -> Optional[dict]:
        """Schval pending (default nejnovější). Předchozí active → superseded."""
        try:
            c = self._connect()
            row = (c.execute("SELECT * FROM hans_directions WHERE id=?", (pid,))
                   if pid is not None else
                   c.execute("SELECT * FROM hans_directions WHERE status='pending' "
                             "ORDER BY id DESC LIMIT 1")).fetchone()
            if not row:
                c.close()
                return None
            c.execute("UPDATE hans_directions SET status='superseded' "
                      "WHERE status='active'")
            c.execute("UPDATE hans_directions SET status='active', decided_ts=? "
                      "WHERE id=?", (time.time(), row["id"]))
            c.commit()
            out = dict(c.execute("SELECT * FROM hans_directions WHERE id=?",
                                 (row["id"],)).fetchone())
            c.close()
            return out
        except Exception as e:
            _log.warning("approve: %s", e)
            return None

    def reject(self, pid: Optional[int] = None) -> bool:
        try:
            c = self._connect()
            if pid is not None:
                c.execute("UPDATE hans_directions SET status='rejected', "
                          "decided_ts=? WHERE id=?", (time.time(), pid))
            else:
                c.execute("UPDATE hans_directions SET status='rejected', "
                          "decided_ts=? WHERE status='pending'", (time.time(),))
            c.commit()
            c.close()
            return True
        except Exception as e:
            _log.warning("reject: %s", e)
            return False


# ── HansDirection ───────────────────────────────────────────────────────────
class HansDirection:
    def __init__(self, config: dict, diary_db_path: str):
        self._config = config or {}
        self._diary_path = diary_db_path
        cfg = (self._config.get("direction", {}) or {})
        self._cfg = cfg
        # analytický model (legacy 1-call fallback): base z evening_reflection
        er = self._config.get("evening_reflection", {}) or {}
        self._model = str(cfg.get("model",
                                  er.get("model", "jobautomation/OpenEuroLLM-Czech:latest")))
        self._timeout = int(cfg.get("llm_timeout", 300))
        # STANCE_REASONING_TIER_V1 — 2-call: reasoning (qwen3, EN úsudek, num_gpu:0)
        # → voice (hans-czech, CZ hlas směru). reasoning_model='' → legacy 1-call.
        self._reasoning_model = str(cfg.get("reasoning_model", "") or "").strip()
        self._voice_model = str(cfg.get("voice_model", "hans-czech:latest"))
        self._store = DirectionStore(self._config, self._diary_path)

    # ── DATA-GATE (deterministicky, read-only) ──────────────────────────────
    def gather_signals(self) -> dict:
        """Posbírá REÁLNÉ signály pro směr. Nikdy nevyhazuje výjimku.
        Vrací {studies_done, studies_active, works, tendencies, hobbies,
        insights, signal_count}."""
        import sqlite3
        out = {"studies_done": [], "studies_active": [], "works": [],
               "tendencies": [], "hobbies": [], "insights": [],
               "competence": [], "signal_count": 0}
        try:
            c = sqlite3.connect("file:%s?mode=ro" % self._diary_path, uri=True,
                                timeout=5)
            out["studies_done"] = [r[0] for r in c.execute(
                "SELECT topic FROM study_program WHERE status='completed'"
                " ORDER BY id").fetchall()]
            out["studies_active"] = [r[0] for r in c.execute(
                "SELECT topic FROM study_program WHERE status='active'"
                " ORDER BY id").fetchall()]
            # díla: témata briefů + názvy work_created (dedup, zachovej pořadí)
            seen = set()
            for (t,) in c.execute("SELECT DISTINCT topic FROM work_briefs"):
                if t and t not in seen:
                    seen.add(t); out["works"].append(t)
            for (t,) in c.execute(
                    "SELECT title FROM diary WHERE event_type='work_created' "
                    "ORDER BY id DESC LIMIT 12"):
                t = (t or "").strip()
                if t and t not in seen:
                    seen.add(t); out["works"].append(t)
            out["insights"] = [r[0] for r in c.execute(
                "SELECT insight_cs FROM self_insights WHERE insight_cs<>'' "
                "ORDER BY id DESC LIMIT 5").fetchall()]
            # HANS_DIRECTION_COMPETENCE_V1 — sebehodnocení: co Hansova díla dle
            # jeho VLASTNÍ deepen-kritiky postrádají = růstové hrany, kam by
            # směr mohl mířit (varianta se sebehodnocením).
            try:
                seen_c = set()
                for (cr,) in c.execute(
                        "SELECT critique FROM deepen_proposals WHERE critique<>'' "
                        "ORDER BY id DESC LIMIT 8"):
                    cr = (cr or "").strip()
                    key = cr[:40].lower()
                    if cr and key not in seen_c:
                        seen_c.add(key)
                        out["competence"].append(cr[:220])
                    if len(out["competence"]) >= 3:
                        break
            except Exception:
                pass
            c.close()
        except Exception as e:
            _log.warning("gather_signals db: %s", e)
        # tendence (Severčin gate) + koníčky
        try:
            from scripts.hans_severka import Severka
            out["tendencies"] = [t.get("claim", "") for t in
                                 Severka(self._config, self._diary_path)
                                 .durable_tendencies()][:6]
        except Exception as e:
            _log.debug("gather tendencies: %s", e)
        try:
            from scripts.hans_hobbies import HobbyStore
            hc = (self._config.get("study", {}) or {})
            out["hobbies"] = [h.name for h in HobbyStore(
                self._config, self._diary_path).durable_hobbies(
                min_evidence=int(hc.get("min_evidence", 8)),
                min_age_days=int(hc.get("min_age_days", 21)),
                min_recent_days=int(hc.get("min_recent_days", 14)))][:8]
        except Exception as e:
            _log.debug("gather hobbies: %s", e)
        out["signal_count"] = sum(1 for k in
                                  ("studies_done", "works", "tendencies",
                                   "hobbies", "insights", "competence")
                                  if out[k])
        return out

    def _gate_ok(self, sig: dict) -> bool:
        return (len(sig.get("studies_done", [])) >= MIN_STUDY_DONE
                and sig.get("signal_count", 0) >= MIN_SIGNALS)

    # ── ÚSUDEK ──────────────────────────────────────────────────────────────
    def evaluate(self) -> dict:
        """Rozhodne o směru. Gate neprošel → {'decision':'keep', gate:False}.
        LLM navrhne → PENDING (nic neaplikuje). Deferral-safe: 'deferred':True
        když LLM neběžel (volající pak nenastaví týdenní guard)."""
        date_str = datetime.now().strftime("%Y-%m-%d")
        sig = self.gather_signals()
        if not self._gate_ok(sig):
            _log.info("direction %s: gate neprošel (málo signálů: %d), držím",
                      date_str, sig.get("signal_count", 0))
            return {"decision": "keep", "gate": False, "signals": sig}

        cur = self._store.current_active()
        parsed, deferred = self._decide(sig, cur)
        if deferred:
            _log.info("direction %s: LLM neběžel/None → odloženo", date_str)
            return {"decision": "keep", "gate": True, "signals": sig,
                    "deferred": True}

        decision = (parsed or {}).get("decision", "keep")
        if not parsed or decision not in ("propose", "evolve"):
            # HANS_DIRECTION_DEGENERATE_RETRY_V1 — model občas u téhle syntézy
            # kolabuje na holé {"decision":"keep"} bez analýzy (nespolehlivost,
            # ne úsudek). Bez aktivního směru = deferred (zkus příště), ať to
            # jeden špatný běh nezablokuje týdenním guardem. S aktivním směrem
            # je keep legitimní (drží dosavadní).
            analysis = (parsed or {}).get("analysis", "")
            degenerate = (not analysis.strip()) and cur is None
            _log.info("direction %s: LLM 'keep'%s", date_str,
                      " (degenerativní → deferred)" if degenerate else "")
            return {"decision": "keep", "gate": True, "signals": sig,
                    "analysis": analysis, "deferred": degenerate}

        direction_raw = (parsed.get("proposed_direction") or "").strip()
        if not direction_raw:
            _log.info("direction %s: '%s' bez proposed_direction → držím",
                      date_str, decision)
            return {"decision": "keep", "gate": True, "signals": sig}
        evidence = parsed.get("evidence")
        evidence = ("; ".join(str(x) for x in evidence)
                    if isinstance(evidence, list) else str(evidence or "")).strip()
        rationale = (parsed.get("rationale") or "").strip()

        # 2-call: EN úsudek → CZ hlas směru + krátké „proč" (jen u reasoning
        # tieru; legacy base už mluví česky). Voice selže → nech surový.
        direction, reason_cz = direction_raw, ""
        if self._reasoning_model:
            v_dir, v_reason = self._voice_direction(direction_raw, rationale)
            if v_dir:
                direction = v_dir
            reason_cz = v_reason
        rationale_disp = reason_cz or rationale  # co ukázat uživateli (CZ)

        model_used = self._reasoning_model or self._model
        pid = self._store.propose(direction, evidence, rationale_disp, model_used)
        self._write_proposal_diary(pid, direction, evidence, rationale_disp,
                                   parsed.get("analysis", ""), date_str)
        _log.info("direction %s: NÁVRH směru (pending id=%s, %s): %.60s",
                  date_str, pid, model_used, direction)
        return {"decision": decision, "gate": True, "signals": sig,
                "id": pid, "proposed_direction": direction, "evidence": evidence,
                "rationale": rationale_disp, "analysis": parsed.get("analysis", ""),
                "message": self._user_message(direction, reason_cz, cur)}

    def _decide(self, sig: dict, cur: Optional[dict]):
        """LLM úsudek → (parsed_dict|None, deferred_bool). Reasoning tier
        (qwen3, EN, num_gpu:0) když je nastaven, jinak legacy base model (CZ)."""
        try:
            from scripts.ollama_client import ollama_generate, ollama_chat
            from scripts.hans_persona import persona_name as _pn
        except ImportError:
            _log.warning("direction: ollama nedostupný, skip")
            return None, True
        prompt = self._build_prompt(sig, cur)
        try:
            if self._reasoning_model:
                cfg = self._cfg
                raw = ollama_chat(
                    self._reasoning_model,
                    [{"role": "system",
                      "content": _DIRECTION_SYSTEM_EN.format(
                          persona_name=_pn(self._config))},
                     {"role": "user", "content": prompt}],
                    config=self._config, keep_alive=0,
                    timeout=int(cfg.get("reasoning_timeout", 900)),
                    options={"temperature": float(
                        cfg.get("reasoning_temperature", 0.4)),
                        "num_ctx": int(cfg.get("reasoning_num_ctx", 12288)),
                        "num_predict": int(cfg.get("reasoning_num_predict", 6144)),
                        "num_gpu": int(cfg.get("reasoning_num_gpu", 0))})
                raw = _strip_think(raw or "")
            else:
                raw = ollama_generate(
                    model=self._model, prompt=prompt,
                    system=_DIRECTION_SYSTEM.format(
                        persona_name=_pn(self._config)),
                    config=self._config, timeout=self._timeout,
                    keep_alive=0, options={"temperature": 0.2})
        except Exception as e:
            _log.warning("direction: LLM failed: %s", e)
            return None, True
        if not raw:
            return None, True
        return self._parse(raw), False

    def _voice_direction(self, direction_en: str, rationale_en: str):
        """Krok 2 — hans-czech přebásní EN směr + „proč" do češtiny Hansovým
        hlasem. Vrací (smer_cz, proc_cz); prázdné stringy při selhání."""
        try:
            from scripts.ollama_client import ollama_chat
            from scripts.hans_persona import persona_name as _pn
        except ImportError:
            return "", ""
        system = (f"Jsi {_pn(self._config)}. Dostáváš svůj vlastní tvůrčí SMĚR a "
                  "jeho zdůvodnění, vyjádřené anglicky. Přepiš je do PŘIROZENÉ "
                  "češtiny, SVÝM hlasem, v 1. osobě. Zachovej konkrétní obsah "
                  "(oblasti, řemeslo), nepřidávej nic nového. Vrať VÝHRADNĚ "
                  "tyto dva řádky:\n"
                  "SMER: <směr jako 1-2 věty>\n"
                  "PROC: <proč, 1 věta>")
        user = ("Můj směr (anglicky): %s\nZdůvodnění (anglicky): %s\n\n"
                "Napiš SMER a PROC česky, svým hlasem:"
                % (direction_en, rationale_en or "(žádné)"))
        try:
            raw = ollama_chat(
                self._voice_model,
                [{"role": "system", "content": system},
                 {"role": "user", "content": user}],
                config=self._config,
                options={"temperature": float(
                    self._cfg.get("voice_temperature", 0.4)),
                    "num_ctx": 4096, "num_predict": 260})
        except Exception as e:
            _log.warning("_voice_direction selhal: %s", e)
            return "", ""
        out = _strip_think(raw or "")
        smer = _re.search(r"SMER:\s*(.+)", out, _re.IGNORECASE)
        proc = _re.search(r"PROC:\s*(.+)", out, _re.IGNORECASE)
        smer_cz = (smer.group(1).strip().strip('„""').strip() if smer else "")
        proc_cz = (proc.group(1).strip().strip('„""').strip() if proc else "")
        if not smer_cz:  # bez markeru: první delší řádek jako směr
            for line in out.splitlines():
                s = line.strip().strip('„""').strip()
                if len(s) > 15:
                    smer_cz = s; break
        return smer_cz[:400], proc_cz[:300]

    # ── Pomocné ─────────────────────────────────────────────────────────────
    def _build_prompt(self, sig: dict, cur: Optional[dict]) -> str:
        def _lst(xs):
            return "\n".join("- %s" % x for x in xs) or "(žádné)"
        parts = []
        if cur and cur.get("direction"):
            parts.append("MŮJ AKTUÁLNÍ SMĚR:\n%s" % cur["direction"])
        parts += [
            "HLOUBKOVĚ NASTUDOVANÉ DOMÉNY (reálná znalost):\n%s"
            % _lst(sig["studies_done"]),
            "PRÁVĚ STUDUJI:\n%s" % _lst(sig["studies_active"]),
            "CO JSEM VYTVOŘIL (díla):\n%s" % _lst(sig["works"]),
            "DLOUHODOBÉ TENDENCE (jak přistupuji ke světu):\n%s"
            % _lst(sig["tendencies"]),
            "DLOUHODOBÉ ZÁJMY:\n%s" % _lst(sig["hobbies"]),
        ]
        if sig.get("competence"):
            parts.append(
                "CO MÝM DÍLŮM DOSUD CHYBĚLO (má vlastní kritika — růstové "
                "hrany, kam bych se mohl posunout):\n%s"
                % _lst(sig["competence"]))
        # insighty jsou často provozní anomálie (šum pro směr) → do promptu
        # jen když vypadají jako růstové/tematické postřehy, ne o datech/logu.
        growth = [i for i in sig["insights"]
                  if not any(w in i.lower() for w in
                             ("záznam", "herní", "provoz", "rutin", "log",
                              "teplot", "data", "položk"))]
        if growth:
            parts.append("ČEHO JSEM SI U SEBE VŠIML:\n%s" % _lst(growth[:3]))
        return "\n\n".join(parts)

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

    def _user_message(self, direction: str, reason_cz: str,
                      cur: Optional[dict]) -> str:
        lead = ("Pane, něco jsem u sebe zvážil. " if not cur else
                "Pane, můj směr se, zdá se, posunul. ")
        out = (lead + "Když se ohlédnu za tím, co jsem nastudoval a vytvořil, "
               "chtěl bych vědomě směřovat k tomuhle:\n\n"
               f"„{direction}\"")
        if reason_cz:
            out += f"\n\n{reason_cz}"
        out += ("\n\nNic tím neřídím bez Vašeho svolení. Schválit „/smer "
                "schválit\", zamítnout „/smer ne\", nebo napsat vlastní směr "
                "„/smer <text>\".")
        return out

    def _write_proposal_diary(self, pid, direction, evidence, rationale,
                              analysis, date_str):
        try:
            import sqlite3
            note = (f"Návrh vlastního směru (id {pid}).\n"
                    f"Směr: {direction}\nVychází z: {evidence}\n"
                    f"Proč: {rationale}\nRozbor: {analysis}")
            c = sqlite3.connect(self._diary_path, timeout=10)
            c.execute("INSERT INTO diary (ts, event_type, title, note, "
                      "importance, provenance) VALUES (?,?,?,?,?,?)",
                      (time.time(), "direction_proposal",
                       "Návrh směru", note, 7, "hans_direction"))
            c.commit()
            c.close()
        except Exception as e:
            _log.debug("write_proposal_diary: %s", e)


# ── Surfacing helper (system-prompt grounding) ──────────────────────────────
def active_direction_line(config: dict, diary_db_path: str) -> str:
    """Věta o aktivním směru pro system-prompt grounding (dopředný narativ).
    Prázdný string když žádný aktivní směr není."""
    try:
        cur = DirectionStore(config, diary_db_path).current_active()
        if cur and cur.get("direction"):
            return "Momentálně vědomě směřuji k: %s" % cur["direction"].strip()
    except Exception as e:
        _log.debug("active_direction_line: %s", e)
    return ""


# ── Smoke (python3 -m scripts.hans_direction [signals|eval]) ────────────────
if __name__ == "__main__":
    import sys
    cfg = {}
    try:
        with open("config.json", encoding="utf-8") as fh:
            cfg = json.load(fh)
    except Exception as exc:
        print("WARN: config.json:", exc)
    db = (cfg.get("diary", {}) or {}).get("db_path", "data/hans_diary.db")
    d = HansDirection(cfg, db)
    mode = sys.argv[1] if len(sys.argv) > 1 else "signals"
    if mode == "signals":
        sig = d.gather_signals()
        print("=== SIGNÁLY (gate ok: %s) ===" % d._gate_ok(sig))
        for k in ("studies_done", "studies_active", "works", "tendencies",
                  "hobbies", "insights"):
            print(f"\n[{k}] ({len(sig[k])})")
            for x in sig[k]:
                print("  -", str(x)[:80])
    elif mode == "eval":
        import json as _j
        print(_j.dumps(d.evaluate(), ensure_ascii=False, indent=1)[:2000])
