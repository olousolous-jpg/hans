"""
Kolačovy případy — trvalé detektivní záhady.

Případ trvá 3-5 dní. Každý dialog přidá novou stopu.
Případy vznikají z reálných pozorování v deníku:
  - přesunutý objekt
  - neobvyklý příchod/odchod
  - zvláštní počasí
  - něco co Hans četl

Použití:
    cases = KolacCases(config, "data/hans_diary.db")
    
    # Získej aktivní případ (nebo vytvoř nový)
    case = cases.get_active_case()
    
    # Po dialogu — přidej stopu
    cases.add_clue(case.id, "Stopy u okna vedou ke knihovně.")
    
    # Pro LLM kontext v dialogu
    context = cases.get_case_context()
"""

import json
import logging
import random
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

_log = logging.getLogger("kolac_cases")

# Šablony případů — Kolač vymýšlí záhady z běžných pozorování
_CASE_TEMPLATES = [
    {
        "trigger": "object_moved",
        "names": [
            "Případ pohnutého {obj}",
            "Záhada přemístěného {obj}",
            "Kauza {obj} na nesprávném místě",
        ],
        "opening_clues": [
            "Všiml jsem si, že {obj} není na svém obvyklém místě.",
            "Někdo pohnul s {obj}. Otázka zní — kdo a proč?",
        ],
    },
    {
        "trigger": "unusual_time",
        "names": [
            "Záhada nočního světla",
            "Případ pozdního příchodu",
            "Kauza neobvyklé aktivity",
        ],
        "opening_clues": [
            "V neobvyklou hodinu byla zaznamenána aktivita v domě.",
            "Někdo se pohyboval po domě v nečekaný čas.",
        ],
    },
    {
        "trigger": "weather_anomaly",
        "names": [
            "Případ podezřelého počasí",
            "Záhada barometrického tlaku",
            "Kauza nečekaného mrazu",
        ],
        "opening_clues": [
            "Počasí se chová neobvykle. Kolač tvrdí, že to souvisí s případem.",
            "Barometr ukazuje podezřelé hodnoty.",
        ],
    },
    {
        "trigger": "reading_inspired",
        "names": [
            "Případ inspirovaný {topic}",
            "Záhada podle {topic}",
            "Kauza literárních paralel",
        ],
        "opening_clues": [
            "{name} četl o {topic} a Kolač v tom vidí paralelu s děním v domě.",
            "Podle Koláče není náhoda, že {name} právě četl o {topic}.",
        ],
    },
    {
        "trigger": "random",
        "names": [
            "Případ zmizelé ponožky",
            "Záhada prázdné šálku",
            "Kauza skřípajících dveří",
            "Případ stínu na chodbě",
            "Záhada tikajících hodin",
            "Kauza neznámého pachu",
            "Případ otevřeného okna",
            "Záhada mokré podlahy",
        ],
        "opening_clues": [
            "Kolač zaznamenal něco podezřelého a zahájil vyšetřování.",
            "Nový případ. Kolač tvrdí, že stopy jsou všude kolem nás.",
        ],
    },
]

# Fáze vyšetřování
PHASE_OPENING    = "opening"     # 1. den — nastolení záhady
PHASE_GATHERING  = "gathering"   # 2-3. den — sbírání stop
PHASE_THEORY     = "theory"      # 4. den — Kolač má teorii
PHASE_RESOLUTION = "resolution"  # 5. den — rozuzlení
PHASE_CLOSED     = "closed"


@dataclass
class Case:
    id: int = 0
    title: str = ""
    trigger: str = "random"
    phase: str = PHASE_OPENING
    opened_at: float = 0.0
    closed_at: Optional[float] = None
    clues: list = field(default_factory=list)
    theory: str = ""
    resolution: str = ""
    target_days: int = 4

    def age_days(self) -> int:
        return max(1, int((time.time() - self.opened_at) / 86400) + 1)

    def should_advance(self) -> bool:
        day = self.age_days()
        if self.phase == PHASE_OPENING and day >= 2:
            return True
        if self.phase == PHASE_GATHERING and day >= self.target_days - 1:
            return True
        if self.phase == PHASE_THEORY and day >= self.target_days:
            return True
        return False


class KolacCases:
    """Spravuje Kolačovy detektivní případy."""

    # Volitelný callback (mood shift) — nastaví ho hans_idle.
    on_mood_shift = None  # type: ignore[assignment]

    def __init__(self, config: dict, diary_db_path: str):
        self.config = config
        self._diary_path = diary_db_path
        self._lock = threading.Lock()
        self._init_db()

        cfg = config.get("kolac_cases", {})
        self._enabled = bool(cfg.get("enabled", True))
        self._min_days = int(cfg.get("min_days", 3))
        self._max_days = int(cfg.get("max_days", 5))
        self._max_active = int(cfg.get("max_active_cases", 1))

        _log.info("KolacCases ready (enabled=%s, days=%d-%d)",
                  self._enabled, self._min_days, self._max_days)

    def _init_db(self):
        db = sqlite3.connect(self._diary_path)
        db.execute("""
            CREATE TABLE IF NOT EXISTS kolac_cases (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                title       TEXT NOT NULL,
                trigger_type TEXT DEFAULT 'random',
                phase       TEXT DEFAULT 'opening',
                opened_at   REAL NOT NULL,
                closed_at   REAL,
                clues       TEXT DEFAULT '[]',
                theory      TEXT DEFAULT '',
                resolution  TEXT DEFAULT '',
                target_days INTEGER DEFAULT 4
            )
        """)
        db.commit()
        db.close()

    # ── Public API ───────────────────────────────────────────────────────────

    def get_active_case(self) -> Optional[Case]:
        """Vrátí aktivní případ, nebo None."""
        db = sqlite3.connect(self._diary_path)
        row = db.execute(
            "SELECT * FROM kolac_cases WHERE phase != ? ORDER BY opened_at DESC LIMIT 1",
            (PHASE_CLOSED,)
        ).fetchone()
        db.close()
        if row:
            return self._row_to_case(row)
        return None

    def get_or_create_case(self, context_parts: list = None) -> Case:
        """Vrátí aktivní případ. Pokud žádný není, vytvoří nový."""
        case = self.get_active_case()
        if case:
            # Zkontroluj jestli má postoupit do další fáze
            if case.should_advance():
                self._advance_phase(case)
            return case
        # Vytvoř nový
        return self._create_case(context_parts or [])

    def add_clue(self, case_id: int, clue: str):
        """Přidej stopu k případu."""
        with self._lock:
            db = sqlite3.connect(self._diary_path)
            row = db.execute(
                "SELECT clues FROM kolac_cases WHERE id=?", (case_id,)
            ).fetchone()
            if row:
                clues = json.loads(row[0] or "[]")
                clues.append({
                    "text": clue,
                    "ts": time.time(),
                    "day": datetime.now().strftime("%Y-%m-%d"),
                })
                db.execute(
                    "UPDATE kolac_cases SET clues=? WHERE id=?",
                    (json.dumps(clues, ensure_ascii=False), case_id))
                db.commit()
            db.close()
        _log.info("Clue added to case %d: %s", case_id, clue[:60])

    def set_theory(self, case_id: int, theory: str):
        db = sqlite3.connect(self._diary_path)
        db.execute("UPDATE kolac_cases SET theory=?, phase=? WHERE id=?",
                   (theory, PHASE_THEORY, case_id))
        db.commit()
        db.close()

    def close_case(self, case_id: int, resolution: str):
        db = sqlite3.connect(self._diary_path)
        # Vytáhni si titul před UPDATE pro deník + mood
        row = db.execute(
            "SELECT title FROM kolac_cases WHERE id=?", (case_id,)
        ).fetchone()
        title = (row[0] if row else f"#{case_id}")
        db.execute(
            "UPDATE kolac_cases SET resolution=?, phase=?, closed_at=? WHERE id=?",
            (resolution, PHASE_CLOSED, time.time(), case_id))
        db.commit()
        db.close()
        _log.info("Case %d closed: %s", case_id, resolution[:60])
        # Zápis do deníku
        try:
            self._diary_write("case_closed", title, resolution)
        except Exception as _e:
            _log.debug("diary close: %s", _e)
        # close_case mood shift — Hans je spokojený
        if callable(self.on_mood_shift):
            try:
                self.on_mood_shift("spokojenost", 0.4,
                                   f"případ uzavřen: {title}")
            except Exception:
                pass

    def get_case_context(self) -> str:
        """Pro LLM prompt — kontext aktivního případu."""
        case = self.get_active_case()
        if not case:
            return ""

        from scripts.hans_persona import persona_name as _pn  # PERSONA_NAME_CONFIGURABLE_V1
        _nm = _pn(self.config)
        clue_str = "\n".join(f"  - {c['text']}" for c in case.clues[-5:])
        phase_cz = {
            PHASE_OPENING:    "zahajovací fáze",
            PHASE_GATHERING:  "sbírání stop",
            PHASE_THEORY:     "Kolač má teorii",
            PHASE_RESOLUTION: "rozuzlení",
        }
        ctx = (
            f"\nKOLAČŮV AKTIVNÍ PŘÍPAD: {case.title}\n"
            f"Fáze: {phase_cz.get(case.phase, case.phase)} "
            f"(den {case.age_days()}/{case.target_days})\n"
        )
        if case.clues:
            ctx += f"Dosavadní stopy:\n{clue_str}\n"
        if case.theory:
            ctx += f"Kolačova teorie: {case.theory}\n"

        # Instrukce podle fáze
        if case.phase == PHASE_OPENING:
            ctx += ("Kolač by měl NASTOLÍT záhadu — popsat co ho znepokojuje "
                    "a prohlásit že zahajuje vyšetřování.\n")
        elif case.phase == PHASE_GATHERING:
            ctx += (f"Kolač by měl HLEDAT nové stopy — ptát se {_nm}, "
                    "spekulovat, analyzovat předchozí nálezy.\n")
        elif case.phase == PHASE_THEORY:
            ctx += (f"Kolač by měl PRESENTOVAT svou teorii — kdo, jak, proč. "
                    f"{_nm} může souhlasit nebo oponovat.\n")
        elif case.phase == PHASE_RESOLUTION:
            ctx += ("Kolač by měl UZAVŘÍT případ — vysvětlit řešení, "
                    "možná s překvapivým odhalením.\n")

        return ctx

    def get_dialog_directive(self) -> str:
        """Krátká replika: střídavě Hans a Koláč mluví o případu.

        Návrat: hotová věta připravená k TTS — buď Hansovo zamyšlení
        směrem ke Koláčovi, nebo Koláčova replika zpět. Střídá se
        podle parity počtu stop (sudé = Hans, liché = Koláč).
        """
        case = self.get_active_case()
        if not case:
            return ""
        n_clues = len(case.clues or [])
        speaker_is_hans = (n_clues % 2 == 0)
        last_clue = ""
        if case.clues:
            last = case.clues[-1]
            if isinstance(last, dict):
                last_clue = (last.get("text") or "")[:120]
        if speaker_is_hans:
            # Hans přemýšlí nahlas, oslovuje Koláče
            options = [
                f"Koláči, ten případ '{case.title}' mi nedá spát. "
                f"Co když je to úplně jinak?",
                f"Pane Koláči, nezdá se vám podivné, že '{case.title}' "
                f"se objevilo právě teď?",
                f"Koláči, vrátil jsem se v myšlenkách k tomu "
                f"'{case.title}'. Měli bychom prošetřit další stopu.",
            ]
        else:
            # Koláč odpovídá / teoretizuje
            options = [
                f"Hansi, dle mého úsudku '{case.title}' "
                f"souvisí s něčím, co jsme přehlédli. "
                f"Sleduji stopu: {last_clue}",
                f"Drahý Hansi, případ '{case.title}' "
                f"odhaluje vrstvy. Den {case.age_days()} "
                f"z {case.target_days} a já tuším motiv.",
                f"Zajímavé, Hansi. {last_clue} "
                f"To by mohlo být klíč k '{case.title}'.",
            ]
        return random.choice(options)

    # ── Interní ──────────────────────────────────────────────────────────────

    def _create_case(self, context_parts: list) -> Case:
        """Vytvoří nový případ z kontextu."""
        template = self._pick_template(context_parts)
        title = self._generate_title(template, context_parts)
        clue = self._generate_opening_clue(template, context_parts)
        target = random.randint(self._min_days, self._max_days)

        db = sqlite3.connect(self._diary_path)
        cur = db.execute(
            "INSERT INTO kolac_cases (title, trigger_type, phase, opened_at, "
            "clues, target_days) VALUES (?,?,?,?,?,?)",
            (title, template["trigger"], PHASE_OPENING, time.time(),
             json.dumps([{"text": clue, "ts": time.time(),
                          "day": datetime.now().strftime("%Y-%m-%d")}],
                        ensure_ascii=False),
             target))
        case_id = cur.lastrowid
        db.commit()
        db.close()

        # Zapiš do deníku
        self._diary_write("case_opened", title, clue)
        _log.info("Nový případ [%d]: %s (%d dní)", case_id, title, target)
        # Mood shift — Hans je teď zvědavý
        if callable(self.on_mood_shift):
            try:
                self.on_mood_shift("zvědavost", 0.5,
                                    f"nový případ: {title}")
            except Exception as _e:  # pragma: no cover
                _log.debug("mood shift open: %s", _e)

        return Case(id=case_id, title=title, trigger=template["trigger"],
                    phase=PHASE_OPENING, opened_at=time.time(),
                    clues=[{"text": clue}], target_days=target)

    def _advance_phase(self, case: Case):
        """Posuň případ do další fáze."""
        old = case.phase
        if old == PHASE_OPENING:
            new = PHASE_GATHERING
        elif old == PHASE_GATHERING:
            new = PHASE_THEORY
        elif old == PHASE_THEORY:
            new = PHASE_RESOLUTION
        else:
            return

        db = sqlite3.connect(self._diary_path)
        db.execute("UPDATE kolac_cases SET phase=? WHERE id=?",
                   (new, case.id))
        db.commit()
        db.close()

        case.phase = new
        self._diary_write("case_phase",
                          f"{case.title} — {new}",
                          f"Případ postoupil: {old} -> {new}")
        _log.info("Case %d: %s → %s", case.id, old, new)

    def _pick_template(self, context_parts: list) -> dict:
        """Vyber šablonu případu podle kontextu."""
        for part in context_parts:
            low = part.lower() if isinstance(part, str) else ""
            if "četl" in low or "reading" in low:
                return next(t for t in _CASE_TEMPLATES
                            if t["trigger"] == "reading_inspired")
            if "počasí" in low or "weather" in low:
                return next(t for t in _CASE_TEMPLATES
                            if t["trigger"] == "weather_anomaly")
        return random.choice(_CASE_TEMPLATES)

    def _generate_title(self, template: dict, context_parts: list) -> str:
        name = random.choice(template["names"])
        # Nahraď placeholdery
        obj = self._extract_object(context_parts)
        topic = self._extract_topic(context_parts)
        return name.format(obj=obj, topic=topic)

    def _generate_opening_clue(self, template: dict,
                                context_parts: list) -> str:
        clue = random.choice(template["opening_clues"])
        obj = self._extract_object(context_parts)
        topic = self._extract_topic(context_parts)
        from scripts.hans_persona import persona_name as _pn  # PERSONA_NAME_CONFIGURABLE_V1
        return clue.format(obj=obj, topic=topic, name=_pn(self.config))

    @staticmethod
    def _extract_object(context_parts: list) -> str:
        for part in context_parts:
            if isinstance(part, str) and "objekt" in part.lower():
                return part.split(":")[-1].strip()[:30]
        return random.choice(["vázy", "svíčky", "knihy", "hodin",
                              "deštníku", "šálku"])

    @staticmethod
    def _extract_topic(context_parts: list) -> str:
        # EXTRACT_TOPIC_PATCH
        # Hledá řádek "Četl jsem o: TITLE — summary" — bere jen TITLE část.
        # Předtím brala celou Hansovu reakci (split(":")[-1]) = ošklivý title.
        for part in context_parts:
            if not isinstance(part, str):
                continue
            low = part.lower()
            if "četl" not in low:
                continue
            # Část za prvním ':' (ne posledním!)
            after_colon = part.split(":", 1)[-1].strip()
            # Odřízni vše za prvním em-dash / hyphen oddělovačem
            for sep in (" — ", " - ", " – "):
                if sep in after_colon:
                    after_colon = after_colon.split(sep, 1)[0].strip()
                    break
            # Odřízni vše za první větnou pomlčkou nebo tečkou
            for sep in (".", "?", "!", ","):
                if sep in after_colon:
                    after_colon = after_colon.split(sep, 1)[0].strip()
                    break
            cleaned = after_colon[:30].strip()
            if cleaned:
                return cleaned
        return "záhadných okolností"

    def _diary_write(self, event_type: str, title: str, note: str = ""):
        try:
            db = sqlite3.connect(self._diary_path)
            db.execute(
                "INSERT INTO diary (ts, event_type, title, note) VALUES (?,?,?,?)",
                (time.time(), event_type, title, note))
            db.commit()
            db.close()
        except Exception as e:
            _log.error("Diary write: %s", e)

    @staticmethod
    def _row_to_case(row) -> Case:
        return Case(
            id=row[0], title=row[1], trigger=row[2], phase=row[3],
            opened_at=row[4], closed_at=row[5],
            clues=json.loads(row[6] or "[]"),
            theory=row[7] or "", resolution=row[8] or "",
            target_days=row[9] or 4,
        )

    def get_recent_closed(self, limit: int = 3) -> list[Case]:
        """Vrátí poslední uzavřené případy."""
        db = sqlite3.connect(self._diary_path)
        rows = db.execute(
            "SELECT * FROM kolac_cases WHERE phase=? ORDER BY closed_at DESC LIMIT ?",
            (PHASE_CLOSED, limit)).fetchall()
        db.close()
        return [self._row_to_case(r) for r in rows]