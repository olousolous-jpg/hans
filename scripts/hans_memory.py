"""
hans_memory.py — Tulvingovská paměťová fasáda (T.1)

Marker: T1_MEMORY_SKELETON_V1

Read-only sjednocení přístupu k existující paměti:
  - Sémantická: Relationships (RelationshipCard) — characterization, family, role
  - Epizodická: KodiMonitor, ConversationStore, SurroundingsDB, diary tabulka
  - Cross-source recall: recall_diary() — nový generic dotaz nad diary

NEDUPLIKUJE data. Pouze deleguje na existující moduly a přidává jeden
sjednocený diary read který dosud žádný modul neměl.

Write API + EncounterTracker přicházejí v T.2-T.3.
Konsolidační facade (kolem HansEveningReflection + RelationshipReflection)
přichází v T.6.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from typing import Optional, Any
from dataclasses import dataclass, field
from scripts.encounter_tracker import EncounterTracker  # T3_ENCOUNTER_TRACKER_V1

_log = logging.getLogger("hans_memory")


# ─── T7_SALIENCE_V1 — salience scoring ───────────────────────────────────────

# Base salience podle typu události. Postaveno na reálných datech:
#   - person_seen (2587×, 0 note): čistý šum
#   - teddy_arrived (1734×): SYSTÉMOVÝ ARTEFAKT z "Force Kolač visible"
#     (Hans mluví s Kolačem i bez kamery kvůli sběru konverzací) — ne reálná
#     událost, proto stejně nízko jako person_seen
#   - evening_reflection (avg 1185 zn.): nejbohatší
#   - teddy_dialog (920×, avg 581): SBÍRANÉ konverzace s Kolačem — cenné
_SALIENCE_BASE = {
    # Vysoká — Hans přemýšlel, mluvil, dozvěděl se, reflektoval
    "evening_reflection":      0.95,
    "human_chat":              0.90,
    "characterization_update": 0.90,
    "case_resolution":         0.88,
    "case_closed":             0.85,
    "dream":                   0.82,
    "reading_takeaway":        0.80,
    "movie_opinion":           0.80,
    "web_read":                0.78,
    "introspection":           0.75,
    "dialog_reflection":       0.75,
    "case_opened":             0.75,
    "case_thought":            0.72,
    "night_summary":           0.70,
    "chat_reflection":         0.70,
    "teddy_dialog":            0.65,
    "observation":             0.60,
    # Střední — kontext, aktivita
    "face_enroll":             0.55,  # vzácné, ale významné (nová osoba)
    "room_description":        0.50,
    "movie_browsed":           0.48,
    "kodi_playing":            0.45,
    "spontaneous":             0.45,
    "activity":                0.40,
    "phase_change":            0.35,
    "body_warm":               0.30,
    # Nízká — rutina, šum, opakující se stavy
    "idle_start":              0.20,
    "idle_end":                0.20,
    "brain_up":                0.20,
    "brain_down":              0.20,
    "brain_still_down":        0.15,
    "teddy_arrived":           0.10,  # Force visible artefakt — ne reálná událost
    "person_seen":             0.10,
}
_SALIENCE_DEFAULT = 0.40  # neznámý typ — střední, ať se neztratí


def salience_score(event: dict, now: Optional[float] = None,
                   person: Optional[str] = None) -> float:
    """
    Ohodnotí významnost diary události skóre 0..1. Čistá funkce.

    Skóre = base(event_type) + modifikátory:
      + 0.10 pokud má smysluplný note (obsah, ne holý záznam)
      + 0.10 pokud person zadán a je zmíněn v title/note
      - mírný útlum podle stáří (ne mazání, jen nižší priorita při recall)

    event: dict s klíči {event_type, title, note, ts, ...} (z recall_diary).
    """
    import time as _t
    import math
    et = event.get("event_type", "")
    score = _SALIENCE_BASE.get(et, _SALIENCE_DEFAULT)

    note = (event.get("note") or "").strip()
    data = (event.get("data") or "").strip()
    if len(note) > 20 or len(data) > 20:
        score += 0.10

    if person:
        title = (event.get("title") or "").lower()
        pl = person.lower()
        if pl in title or pl in note.lower():
            score += 0.10

    ts_ev = event.get("ts")
    if ts_ev:
        now = now if now is not None else _t.time()
        age_days = max(0.0, (now - ts_ev) / 86400.0)
        decay = min(0.15, 0.05 * math.log1p(age_days / 7.0))
        score -= decay

    return max(0.0, min(1.0, score))


# ─── T4_MEMORY_RECALL_V1 — RecallBundle + helpers ────────────────────────────

_DOW_CZ = ["v pondělí", "v úterý", "ve středu", "ve čtvrtek",
           "v pátek", "v sobotu", "v neděli"]


def _czech_relative_time(ts: Optional[float], now: Optional[float] = None) -> str:
    """Lidsky čitelný relativní čas v češtině z unix timestampu."""
    if ts is None:
        return "nikdy"
    import time as _t
    now = now if now is not None else _t.time()
    diff = now - ts
    if diff < 0:
        return "v budoucnu"
    if diff < 90:
        return "právě teď"
    if diff < 3600:
        m = int(diff / 60)
        return f"před {m} min"
    if diff < 6 * 3600:
        h = int(diff / 3600)
        # české skloňování hodin
        if h == 1:
            return "před hodinou"
        elif h < 5:
            return f"před {h} hodinami"
        return f"před {h} hodinami"
    # Dnes / včera / předevčírem / den v týdnu
    lt_now = _t.localtime(now)
    lt_ts = _t.localtime(ts)
    day_now = (lt_now.tm_year, lt_now.tm_yday)
    day_ts = (lt_ts.tm_year, lt_ts.tm_yday)
    days_apart = (
        _t.mktime(_t.struct_time((lt_now.tm_year, lt_now.tm_mon, lt_now.tm_mday,
                                  0, 0, 0, 0, 0, -1)))
        - _t.mktime(_t.struct_time((lt_ts.tm_year, lt_ts.tm_mon, lt_ts.tm_mday,
                                    0, 0, 0, 0, 0, -1)))
    ) / 86400.0
    days_apart = round(days_apart)
    hhmm = _t.strftime("%H:%M", lt_ts)
    if days_apart <= 0:
        return f"dnes v {hhmm}"
    if days_apart == 1:
        return f"včera v {hhmm}"
    if days_apart == 2:
        return f"předevčírem v {hhmm}"
    if days_apart < 7:
        return f"{_DOW_CZ[lt_ts.tm_wday]} v {hhmm}"
    if days_apart < 14:
        return "minulý týden"
    if days_apart < 31:
        w = int(days_apart / 7)
        return f"před {w} týdny"
    if days_apart < 365:
        mo = int(days_apart / 30)
        return f"před {mo} měsíci" if mo > 1 else "před měsícem"
    return _t.strftime("%d.%m.%Y", lt_ts)


def _encounter_duration_cz(enc: dict) -> str:
    """Délka encounteru lidsky. enc musí mít started_at, ended_at."""
    if not enc:
        return ""
    start = enc.get("started_at")
    end = enc.get("ended_at")
    if start is None:
        return ""
    if end is None:
        return "stále probíhá"
    dur_min = (end - start) / 60.0
    if dur_min < 1:
        return "krátce"
    if dur_min < 60:
        return f"~{int(dur_min)} min"
    h = dur_min / 60.0
    return f"~{h:.1f} h"


@dataclass
class RecallBundle:
    """
    Cross-source agregát paměti o osobě. Vrací Memory.recall().

    Pole jsou None/prázdná pokud zdroj nebyl v include nebo nemá data.
    to_prompt() generuje strukturovaný podklad pro LLM (NE hotové věty —
    formulaci dělá LLM ve svém hlase).
    """
    person: str
    window_hours: float
    facts: Optional[Any] = None                 # RelationshipCard
    current_encounter: Optional[dict] = None
    last_encounter: Optional[dict] = None
    diary_episodes: list = field(default_factory=list)
    kodi_history: str = ""
    dialog_tail: list = field(default_factory=list)

    def is_empty(self) -> bool:
        """True pokud nemáme o osobě vůbec nic."""
        return (
            self.facts is None
            and self.current_encounter is None
            and self.last_encounter is None
            and not self.diary_episodes
            and not self.kodi_history
            and not self.dialog_tail
        )

    def to_prompt(self, style: str = "full") -> str:
        """
        Strukturovaný podklad pro LLM prompt.
        style: 'greeting' (stručné), 'full' (vše), 'compact' (jen fakta+encounter).
        Vrací český text s fakty, NE hotové věty — LLM formuluje sám.
        """
        if self.is_empty():
            return f"NEZNÁMÁ OSOBA: {self.person} (žádný záznam v paměti)"

        lines = []
        name = self.person
        # Display name z facts pokud je
        if self.facts is not None:
            disp = getattr(self.facts, "display_name", None) or name
            role = getattr(self.facts, "role", "") or ""
            header = f"ZNÁMÁ OSOBA: {disp}"
            if role:
                header += f" ({role})"
            lines.append(header)
            char = getattr(self.facts, "characterization", "") or ""
            if char:
                lines.append(f"Charakteristika: {char}")
            n_sight = getattr(self.facts, "sightings_count", 0) or 0
            if n_sight:
                lines.append(f"Celkem setkání: {n_sight}×")
        else:
            lines.append(f"OSOBA: {name} (není ve vztahových kartách)")

        # Aktuální stav
        if self.current_encounter is not None:
            ce = self.current_encounter
            since = _czech_relative_time(ce.get("started_at"))
            extra = []
            if ce.get("n_dialogs"):
                extra.append(f"{ce['n_dialogs']} výměn")
            extra_s = f" ({', '.join(extra)})" if extra else ""
            lines.append(f"Aktuální stav: právě přítomen, od {since}{extra_s}")

        # Poslední setkání (jen pokud není právě tady)
        if style != "greeting" or self.current_encounter is None:
            if self.last_encounter is not None:
                le = self.last_encounter
                when = _czech_relative_time(le.get("ended_at") or le.get("started_at"))
                dur = _encounter_duration_cz(le)
                parts = [f"Poslední setkání: {when}"]
                if dur:
                    parts.append(f"trvalo {dur}")
                if le.get("n_dialogs"):
                    parts.append(f"{le['n_dialogs']} výměn v dialogu")
                if le.get("summary"):
                    parts.append(f"shrnutí: {le['summary']}")
                lines.append(", ".join(parts))

        # Kodi historie
        if style == "full" and self.kodi_history:
            lines.append(f"Společné sledování (Kodi): {self.kodi_history}")

        # Diary epizody (komprimovaně)
        if style == "full" and self.diary_episodes:
            ep_lines = []
            for ep in self.diary_episodes[:5]:
                when = _czech_relative_time(ep.get("ts"))
                title = (ep.get("title") or ep.get("event_type") or "")[:50]
                ep_lines.append(f"  - {when}: {title}")
            if ep_lines:
                lines.append("Nedávné události:\n" + "\n".join(ep_lines))

        # Dialog tail
        if style == "full" and self.dialog_tail:
            n = len(self.dialog_tail)
            lines.append(f"Posledních {n} replik dialogu k dispozici.")

        return "\n".join(lines)


class Memory:
    """
    Read-only fasáda nad existujícími paměťovými moduly.

    Použití (typicky v display_controller_picam.py):
        self._memory = Memory(
            config,
            relationships=self._relationships,
            kodi_monitor=self._kodi_monitor,
            surroundings_db=self._surroundings_db,
            conversation_store=self._conversation_store,
        )

        card = self._memory.fact("alice")            # → RelationshipCard | None
        hist = self._memory.kodi_history("alice")    # → str
        rows = self._memory.recall_diary(person="alice", window_hours=24)
    """

    def __init__(
        self,
        config: dict,
        *,
        relationships=None,
        kodi_monitor=None,
        surroundings_db=None,
        conversation_store=None,
        diary_db_path: Optional[str] = None,
    ):
        self._config = config
        self._rel = relationships
        self._kodi = kodi_monitor
        self._surr = surroundings_db
        self._conv = conversation_store
        self._diary_path = diary_db_path or config.get(
            "diary_db", "data/hans_diary.db"
        )
        _log.info(
            "Memory ready (rel=%s, kodi=%s, surr=%s, conv=%s, diary=%s)",
            self._rel is not None,
            self._kodi is not None,
            self._surr is not None,
            self._conv is not None,
            self._diary_path,
        )

        # T3_ENCOUNTER_TRACKER_V1 — encounter lifecycle
        try:
            self._tracker = EncounterTracker(self._diary_path)
            if self._kodi is not None:
                self._kodi.on_arrived = self._on_person_arrived
                self._kodi.on_left = self._on_person_left
                _log.info("Memory: encounter callbacks wired into KodiMonitor")
        except Exception as e:
            _log.error("EncounterTracker init failed: %s", e)
            self._tracker = None

        # T6_CONSOLIDATE_V1 — reference na HansRoutine (drží reflexe).
        # Wireuje se zvenčí přes set_routine() (controller _init_memory),
        # protože routine vzniká hluboko v hans_idle a Memory ji nevlastní.
        self._routine = None
        self._synthesis = None  # T6B_ENCOUNTER_SUMMARY_V1 — wire přes set_synthesis()
        # Práh významnosti encounteru pro summary
        self._summary_min_sightings = 200


    # ── Sémantická paměť (delegate na Relationships) ─────────────────

    def fact(self, person: str) -> Optional[Any]:
        """
        Sémantický záznam o osobě (RelationshipCard).
        Vrací None pokud osoba neexistuje nebo Relationships není přítomna.
        """
        if self._rel is None:
            return None
        return self._rel.get(person)

    def all_people(self, include_inactive: bool = False) -> list:
        """Všechny osoby známé v Relationships."""
        if self._rel is None:
            return []
        return self._rel.all_cards(include_inactive=include_inactive)

    def last_seen(self, person: str) -> Optional[float]:
        """Shortcut: timestamp posledního sighting (z RelationshipCard)."""
        card = self.fact(person)
        if card is None:
            return None
        return card.last_seen_ts

    # ── Epizodická paměť — delegate wrappery ─────────────────────────

    def kodi_history(self, person: str, limit: int = 5) -> str:
        """Co osoba sledovala v Kodi (formátovaný text)."""
        if self._kodi is None:
            return ""
        return self._kodi.get_person_history(person, limit=limit)

    def kodi_today(self) -> str:
        """Co se dnes hrálo v Kodi (formátovaný text)."""
        if self._kodi is None:
            return ""
        return self._kodi.get_today_events()

    def now_playing(self) -> str:
        """Co se hraje právě teď (formátovaný text pro LLM kontext)."""
        if self._kodi is None:
            return ""
        return self._kodi.get_now_playing_context()

    def dialog_history(self, person: str, limit: int = 10) -> list:
        """Dialog s osobou — list of {role, content}. Limit poslední N zpráv."""
        if self._conv is None:
            return []
        full = self._conv.get_history(person)
        return full[-limit:] if limit > 0 else full

    def recent_objects(self, max_age_s: int = 1800) -> list:
        """Objekty nedávno viděné v okolí."""
        if self._surr is None:
            return []
        return self._surr.get_recent_objects(max_age_s=max_age_s)

    # ── NOVÉ: Generic recall nad diary tabulkou ──────────────────────

    def recall_diary(
        self,
        person: Optional[str] = None,
        window_hours: float = 24.0,
        event_types: Optional[list] = None,
        limit: int = 50,
        min_salience: float = 0.0,
        sort_by_salience: bool = False,
    ) -> list[dict]:
        """
        Generický dotaz nad diary tabulkou — sjednocený filtr přes okno,
        event_types a osobu.

        Args:
            person: pokud zadáno, filtruje řádky kde title/data obsahuje jméno
                    (loose match, perfektní filtrace přijde v T.2 s encounters)
            window_hours: kolik hodin zpět hledat (default 24h)
            event_types: whitelist event_typů (např. ['person_seen', 'teddy_dialog'])
            limit: max počet vrácených řádků (DESC podle ts)

        Returns:
            list dictů: {id, ts, event_type, title, data, note}
        """
        now = time.time()
        since = now - window_hours * 3600.0

        sql = (
            "SELECT id, ts, event_type, title, data, note "
            "FROM diary WHERE ts >= ?"
        )
        params: list = [since]

        if event_types:
            placeholders = ",".join(["?"] * len(event_types))
            sql += f" AND event_type IN ({placeholders})"
            params.extend(event_types)

        if person:
            # Loose match — person může být v title nebo data (JSON-like string).
            # T.2 to udělá přesněji přes encounters tabulku.
            sql += " AND (title LIKE ? OR data LIKE ?)"
            pat = f"%{person}%"
            params.extend([pat, pat])

        sql += " ORDER BY ts DESC LIMIT ?"
        params.append(int(limit))

        try:
            with sqlite3.connect(self._diary_path) as db:
                db.row_factory = sqlite3.Row
                rows = [dict(r) for r in db.execute(sql, params).fetchall()]
        except Exception as e:
            _log.error("recall_diary failed: %s", e)
            return []

        # T7_SALIENCE_V1 — skóre, filtr, volitelné řazení
        if min_salience > 0.0 or sort_by_salience:
            _now = time.time()
            for r in rows:
                r["_salience"] = salience_score(r, now=_now, person=person)
            if min_salience > 0.0:
                rows = [r for r in rows if r["_salience"] >= min_salience]
            if sort_by_salience:
                rows.sort(key=lambda r: r.get("_salience", 0.0), reverse=True)
        return rows

    def recall_diary_window(
        self,
        start_ts: float,
        end_ts: float,
        person: Optional[str] = None,
        event_types: Optional[list] = None,
        limit: int = 100,
    ) -> list[dict]:
        """T6B — diary v ABSOLUTNÍM okně [start_ts, end_ts] (pro encounter summary)."""
        sql = ("SELECT id, ts, event_type, title, data, note FROM diary "
               "WHERE ts >= ? AND ts <= ?")
        params: list = [start_ts, end_ts]
        if event_types:
            ph = ",".join(["?"] * len(event_types))
            sql += f" AND event_type IN ({ph})"
            params.extend(event_types)
        if person:
            sql += " AND (title LIKE ? OR data LIKE ? OR note LIKE ?)"
            pat = f"%{person}%"
            params.extend([pat, pat, pat])
        sql += " ORDER BY ts ASC LIMIT ?"
        params.append(int(limit))
        try:
            with sqlite3.connect(self._diary_path) as db:
                db.row_factory = sqlite3.Row
                return [dict(r) for r in db.execute(sql, params).fetchall()]
        except Exception as e:
            _log.error("recall_diary_window failed: %s", e)
            return []

    # ── Encounters API (T.3) ─────────────────────────────────────────

    def _on_person_arrived(self, name: str, ts: float):
        """KodiMonitor callback. Otevři/reuse encounter."""
        if self._tracker is not None:
            self._tracker.on_arrived(name, ts)
            self._tracker.bump_sighting(name)

    def _on_person_left(self, name: str, ts: float):
        """KodiMonitor callback. Zavři encounter + (async) summary významných."""
        if self._tracker is None:
            return
        _eid = self._tracker.on_left(name, ts)
        # T6B — významný encounter? spusť summary v daemon threadu (mimo frame loop)
        if _eid:
            import threading
            threading.Thread(
                target=self._maybe_summarize_encounter,
                args=(int(_eid),), daemon=True,
            ).start()

    def current_encounter(self, person: str) -> Optional[dict]:
        """Aktivní encounter pro osobu, nebo None."""
        if self._tracker is None:
            return None
        return self._tracker.current(person)

    def last_encounter(self, person: str, include_open: bool = False) -> Optional[dict]:
        """Poslední encounter pro osobu."""
        if self._tracker is None:
            return None
        return self._tracker.last(person, include_open=include_open)

    def all_encounters(self, person: str, limit: int = 20) -> list:
        """Posledních N encounterů pro osobu (DESC podle started_at)."""
        if self._tracker is None:
            return []
        return self._tracker.all_for(person, limit=limit)

    def bump_dialog(self, person: str) -> bool:
        """Volat když Hans mluví s osobou (zvyšuje n_dialogs aktivního encounteru)."""
        if self._tracker is None:
            return False
        return self._tracker.bump_dialog(person)

    # ── T4_MEMORY_RECALL_V1 — cross-source recall ────────────────────

    def recall(
        self,
        person: str,
        window_hours: float = 168.0,
        include: Optional[set] = None,
    ) -> "RecallBundle":
        """
        Sjednocený dotaz přes všechny paměťové zdroje.

        include: podmnožina {'facts','encounters','diary','kodi','dialog'}.
                 None = vše. Zdroje mimo include se nedotazují (perf).

        Vrací RecallBundle (vždy aspoň s person vyplněným).
        """
        if include is None:
            include = {"facts", "encounters", "diary", "kodi", "dialog"}

        bundle = RecallBundle(person=person, window_hours=window_hours)

        if "facts" in include:
            try:
                bundle.facts = self.fact(person)
            except Exception as e:
                _log.warning("recall facts(%s) failed: %s", person, e)

        if "encounters" in include:
            try:
                bundle.current_encounter = self.current_encounter(person)
                bundle.last_encounter = self.last_encounter(person)
            except Exception as e:
                _log.warning("recall encounters(%s) failed: %s", person, e)

        if "diary" in include:
            try:
                # T7_SALIENCE_V1 — řadit epizody podle významnosti, ne jen času
                bundle.diary_episodes = self.recall_diary(
                    person=person, window_hours=window_hours, limit=20,
                    sort_by_salience=True,
                )
            except Exception as e:
                _log.warning("recall diary(%s) failed: %s", person, e)

        if "kodi" in include:
            try:
                bundle.kodi_history = self.kodi_history(person, limit=5)
            except Exception as e:
                _log.warning("recall kodi(%s) failed: %s", person, e)

        if "dialog" in include:
            try:
                bundle.dialog_tail = self.dialog_history(person, limit=6)
            except Exception as e:
                _log.warning("recall dialog(%s) failed: %s", person, e)

        return bundle

    def recall_for_greeting(self, person: str) -> "RecallBundle":
        """Preset pro pozdrav: kdo to je + kdy naposledy + co tehdy. Bez diary balastu."""
        return self.recall(
            person,
            window_hours=24 * 30,  # měsíc zpět pro 'naposledy'
            include={"facts", "encounters", "kodi"},
        )

    def recall_for_dialog(self, person: str) -> "RecallBundle":
        """Preset pro konverzaci: vše včetně dialog tail a nedávných epizod."""
        return self.recall(
            person,
            window_hours=24 * 7,
            include={"facts", "encounters", "diary", "kodi", "dialog"},
        )

    # ── Stubs pro T.6+ ────────────────────────────────────────────────

    def record_episode(self, *args, **kwargs):
        raise NotImplementedError(
            "Volný record_episode přichází později — pro encounters použij bump_*."
        )

    def set_routine(self, routine):
        """T6_CONSOLIDATE_V1 — wire HansRoutine (drží reflexe pro consolidate)."""
        self._routine = routine

    def set_synthesis(self, synthesis):
        """T6B_ENCOUNTER_SUMMARY_V1 — wire HansSynthesis (pro encounter summary)."""
        self._synthesis = synthesis

    def _maybe_summarize_encounter(self, encounter_id: int):
        """T6B — pokud je encounter významný, vygeneruj summary z diary okna.

        Běží v daemon threadu (volá se z _on_person_left). Tichý — chyby
        jen loguje, nikdy nezhavaruje volajícího.
        """
        try:
            if self._tracker is None or self._synthesis is None:
                return
            enc = self._tracker.get_by_id(encounter_id)
            if enc is None:
                return
            # Práh významnosti: měl dialog NEBO dlouhá přítomnost
            n_dlg = enc.get("n_dialogs", 0) or 0
            n_sight = enc.get("n_sightings", 0) or 0
            if not (n_dlg > 0 or n_sight >= self._summary_min_sightings):
                _log.debug("encounter %d nevýznamný (dlg=%d sight=%d) — skip summary",
                           encounter_id, n_dlg, n_sight)
                return
            # Už má summary? (orphan cleanup nebo dřívější běh) — nepřepisuj
            if (enc.get("summary") or "").strip():
                return

            person = enc.get("person_id", "")
            start = enc.get("started_at")
            end = enc.get("ended_at") or time.time()

            # Sesbírej diary epizody v okně (mimo person_seen — to je šum)
            rows = self.recall_diary_window(
                start_ts=start, end_ts=end, person=person, limit=40
            )
            interesting = [
                r for r in rows
                if r.get("event_type") not in ("person_seen",)
            ]
            if not interesting:
                _log.debug("encounter %d: žádné zajímavé epizody v okně — skip",
                           encounter_id)
                return

            # Sestav fakta pro synthesize
            facts_lines = []
            for r in interesting[:15]:
                t = (r.get("title") or "").strip()
                n = (r.get("note") or "").strip()
                et = r.get("event_type", "")
                line = f"[{et}] {t}"
                if n:
                    line += f": {n[:100]}"
                facts_lines.append(line)
            facts = "\n".join(facts_lines)

            dur_min = int((end - start) / 60.0) if start else 0
            topic = f"setkání s {person} ({dur_min} min, {n_dlg} výměn)"

            summary = self._synthesis.synthesize(
                topic=topic,
                facts=facts,
                style="encounter_summary",
                max_tokens=120,
                max_chars=300,
            )
            if summary:
                self._tracker.set_summary(encounter_id, summary)
                _log.info("encounter %d summary: %s", encounter_id, summary[:80])
        except Exception as e:
            _log.error("_maybe_summarize_encounter(%d) failed: %s",
                       encounter_id, e)

    def consolidate(self, kind: str = "all") -> dict:
        """
        Konsolidační fasáda — deleguje na existující reflexe v hans_routine.

        kind:
          'daily'         → večerní reflexe dne (run_evening_reflection, guarded)
          'relationships' → reflexe vztahových karet (interval-guarded per karta)
          'all'           → obě

        Vrací dict s výsledky: {'daily': <text|None>, 'relationships': <int|None>}.
        Hodnota None = krok nebyl spuštěn (routine chybí / kind nezahrnoval).

        NEVYTVÁŘÍ reflexe — pokud routine není wired (set_routine),
        vrací prázdné výsledky a zaloguje warning.
        """
        results = {"daily": None, "relationships": None}

        if self._routine is None:
            _log.warning("consolidate(%s): routine není wired — nelze konsolidovat", kind)
            return results

        if kind in ("all", "daily"):
            try:
                # run_evening_reflection má vlastní denní guard (_last_reflection_date)
                results["daily"] = self._routine.run_evening_reflection()
                _log.info("consolidate: daily reflection → %s",
                          "text" if results["daily"] else "None (nic k reflexi / guard)")
            except Exception as e:
                _log.error("consolidate daily failed: %s", e)

        if kind in ("all", "relationships"):
            try:
                _rr = getattr(self._routine, "_relationship_reflection", None)
                if _rr is not None:
                    # reflect_due_persons je interval-guarded per karta —
                    # opakované volání je no-op (nic není due).
                    results["relationships"] = _rr.reflect_due_persons()
                    _log.info("consolidate: relationship reflection → %d karet",
                              results["relationships"] or 0)
                else:
                    _log.info("consolidate: relationship_reflection není v routine")
            except Exception as e:
                _log.error("consolidate relationships failed: %s", e)

        return results
