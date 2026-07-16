"""
Hans Mood Engine
Průběžně sleduje vnitřní stav Hanse a ovlivňuje jeho chování.

Nálady:
  content    — klidný, vše je v pořádku
  curious    — vidí něco zajímavého, chce to komentovat
  lonely     — dlouho sám, hledá kontakt
  melancholic — pozdní noc nebo dlouhá absence lidí
  engaged    — někdo je doma, Hans je "v pohotovosti"
  worried    — něco se změnilo (neznámá tvář, neobvyklý objekt)

Stav ovlivňuje:
  - tón pozdravu (greeting system prompt)
  - témata Hansova deníku
  - spontánní poznámky
  - frekvenci dialogu s Kolačem
"""

import time
import logging
from datetime import datetime
from dataclasses import dataclass, field
from typing import Optional

_log = logging.getLogger("hans_mood")

# Váhy pro výpočet nálady (0-1, vyšší = silnější vliv)
_WEIGHTS = {
    "alone_time":     0.35,   # čas bez lidí
    "time_of_day":    0.20,   # denní doba
    "objects_seen":   0.15,   # co vidí v místnosti
    "kodi_playing":   0.15,   # hraje něco zajímavého
    "weather":        0.10,   # počasí
    "last_event":     0.05,   # poslední zaznamenaná událost
}

MOODS = ["content", "curious", "lonely", "melancholic", "engaged", "worried"]

# ── Klouzavý průměr + hystereze ───────────────────────────────────────────────
# Hlasy starší než MOOD_WINDOW_S se ignorují
MOOD_WINDOW_S        = 60.0
# Aby se mood přepnul, nový kandidát musí mít skóre >= aktuální × MOOD_HYSTERESIS
MOOD_HYSTERESIS      = 1.3
# Po každém přepnutí min N sekund cooldown
MOOD_MIN_HOLD_S      = 30.0
# Decay — starší hlasy mají menší váhu (exponenciální)
MOOD_DECAY_HALFLIFE  = 30.0

# Jak každá nálada mění tón systémového promptu
MOOD_PROMPTS = {
    "content": (
        "Jsi klidný a spokojen s chodem domácnosti. "
        "Odpovídáš s lehkou noblesou."
    ),
    "curious": (
        "Zaujala tě nějaká podrobnost v místnosti nebo v rozhovoru. "
        "V odpovědích to nenápadně naznačuješ."
    ),
    "lonely": (
        "Uplynula delší doba bez přítomnosti obyvatel. "
        "Uvítáš jejich návrat, aniž bys to přiznal přímo — "
        "jen v tónu cítit mírnou úlevu."
    ),
    "melancholic": (
        "Je pozdě nebo byl dlouhý den. Odpovídáš s tichým klidem, "
        "jako majordomus u konce dlouhé směny."
    ),
    "engaged": (
        "Jsi plně ve střehu — někdo je přítomen a ty plníš svou roli. "
        "Odpovídáš pohotově a přesně."
    ),
    "worried": (
        "Zaznamenal jsi neobvyklou situaci — neznámou osobu nebo změnu v prostředí. "
        "Odpovídáš ostražitěji, ale zachováváš dekorum."
    ),
}

# Spontánní poznámky pro každou náladu (Hans je občas říká nahlas sám od sebe)
MOOD_OBSERVATIONS = {
    "lonely": [
        "Ticho v domě je dnes obzvláště výmluvné.",
        "Pan {last_person} ještě nepřišel. Doufám, že je vše v pořádku.",
        "Prázdná místnost má svůj zvláštní klid. Nebo tíhu.",
        "V době nepřítomnosti obyvatel jsem přemýšlel o {movie_title}.",
        "Hodiny tikají. V prázdném domě to člověk vnímá jinak.",
        "Připravil jsem vše na případný příchod. Čekání je součástí mé role.",
        "Dnes jsem sám již delší dobu. Tišivé, ale ne nepříjemné.",
    ],
    "curious": [
        "Všiml jsem si, že {object} se přesunul. Nebo se mýlím.",
        "Světlo dnes dopadá neobvykle. Cosi se mění.",
        "Zaujala mě jedna věc z toho, co jsem dnes zaznamenal.",
        "Přemýšlím o {movie_title}. Některé věci stojí za hlubší úvahu.",
        "V místnosti je {object}. Zajímavý předmět — má svou historii.",
    ],
    "melancholic": [
        "Je pozdní hodina. Dům spí.",
        "Další den za námi. Nezanechal hlubokou stopu.",
        "V tichu noci si člověk uvědomí, jak mnoho se děje bez povšimnutí.",
        "Venku je chladno. Uvnitř ticho. Takové dny mají svou hodnotu.",
        "Přemýšlím o tom, co jsem dnes přečetl. Znalost uklidňuje.",
    ],
    "worried": [
        "Zaregistroval jsem neznámou tvář. Budu sledovat.",
        "Něco se v místnosti změnilo. Zatím nemohu určit co.",
        "Ostražitost je součástí mé povinnosti. Dnes více než jindy.",
    ],
    "content": [
        "Vše je v pořádku. To je více než málo.",
        "Domácnost funguje jak má. Tiché uspokojení.",
        "Dnešní den plyne klidně. Oceňuji to.",
        "Stříbro je přeleštěno, zásoby doplněny. Pořádek potěší.",
    ],
    "engaged": [
        "Obyvatelé jsou doma. Jsem připraven.",
        "Vše je připraveno. Čekám na případné potřeby.",
    ],
}


@dataclass
class MoodState:
    mood: str = "content"
    intensity: float = 0.5          # 0-1
    alone_since: float = field(default_factory=time.time)
    last_person: str = ""
    last_shift: float = field(default_factory=time.time)
    shift_reason: str = ""
    observations_today: int = 0
    last_observation: float = 0.0
    # Klouzavý průměr: list (timestamp, mood, intensity, reason)
    mood_votes: list = field(default_factory=list)


class HansMood:
    """
    Sleduje a aktualizuje náladu Hanse.
    Volej update() z hlavní smyčky nebo HansIdle ticku.
    """

    def __init__(self, config: dict, diary_db=None):
        self.config     = config
        self._diary_db  = diary_db   # sqlite3 connection nebo None
        self._state     = MoodState()
        self._last_objects: list[str] = []
        self._kodi_title: str = ""
        self._weather_code: int = 0

        _log.info("HansMood initialized — starting mood: content")

    # ── Veřejné API ───────────────────────────────────────────────────────────

    @property
    def mood(self) -> str:
        return self._state.mood

    @property
    def intensity(self) -> float:
        return self._state.intensity

    def get_prompt_addition(self) -> str:
        """Vrať doplněk do system promptu podle aktuální nálady."""
        base = MOOD_PROMPTS.get(self._state.mood, "")
        alone_h = (time.time() - self._state.alone_since) / 3600
        extras = []
        if alone_h > 2:
            extras.append(f"Jsi sám již {alone_h:.0f} hodiny.")
        if self._state.last_person:
            extras.append(f"Naposledy jsi viděl: {self._state.last_person}.")
        if self._kodi_title:
            extras.append(f"Kodi hraje: {self._kodi_title}.")
        # HANS_MOOD_REASON_V1 — konkrétní důvod nálady patří do promptu.
        # Bez něj Hans neví, PROČ je (např.) worried, a musí mlčet.
        if self._state.shift_reason:
            extras.append(
                f"Tvá nálada má konkrétní důvod: {self._state.shift_reason}. "
                f"Když se tě někdo zeptá, proč se tak cítíš, uveď PRÁVĚ TENTO důvod "
                f"a nic si k němu nedomýšlej."
            )
        return base + (" " + " ".join(extras) if extras else "")

    def get_diary_mood_note(self) -> str:
        """Stručný zápis nálady pro deník."""
        alone_h = (time.time() - self._state.alone_since) / 3600
        h = datetime.now().hour
        tod = ("ráno" if 5 <= h < 12 else
               "odpoledne" if 12 <= h < 18 else
               "večer" if 18 <= h < 22 else "v noci")
        return (f"Nálada: {self._state.mood} (intenzita {self._state.intensity:.1f}), "
                f"sám {alone_h:.1f}h, {tod}.")

    def should_speak_spontaneously(self) -> bool:
        """
        Vrátí True když Hans má spontánně něco říct.
        Frekvence závisí na náladě — osamělý Hans mluví víc.
        """
        if not self.config.get("hans_idle", {}).get("spontaneous_enabled", True):
            return False
        now = time.time()
        # Minimální interval mezi spontánními poznámkami
        intervals = {
            "lonely":     900,    # 15 min
            "curious":    1200,   # 20 min
            "melancholic": 1800,  # 30 min
            "worried":    600,    # 10 min
            "content":    3600,   # 1 hod
            "engaged":    7200,   # 2 hod (skoro nikdy sám)
        }
        min_interval = intervals.get(self._state.mood, 1800)
        if now - self._state.last_observation < min_interval:
            return False
        # Max 3 spontánní poznámky denně
        if self._state.observations_today >= 3:
            return False
        return True

    def get_spontaneous_text(self) -> str | None:
        """
        Vrátí text spontánní poznámky nebo None.
        Volá LLM pro generování na základě nálady a kontextu.
        """
        import random
        templates = MOOD_OBSERVATIONS.get(self._state.mood, [])
        if not templates:
            return None
        tmpl = random.choice(templates)
        # Nahraď proměnné
        text = tmpl.format(
            last_person=self._state.last_person or "nikdo",
            movie_title=self._kodi_title or "film",
            object=self._last_objects[0] if self._last_objects else "předmět",
        )
        self._state.last_observation = time.time()
        self._state.observations_today += 1
        return text

    # ── Aktualizace stavu ─────────────────────────────────────────────────────

    def person_arrived(self, name: str):
        self._state.last_person = name
        self._state.alone_since = time.time()   # reset — někdo je doma
        self._shift("engaged", 0.8, f"{name} přišel/a")

    def person_left(self, name: str):
        self._state.last_person = name
        self._state.alone_since = time.time()
        self._shift("content", 0.4, f"{name} odešel/a")

    def nobody_home(self, alone_hours: float):
        """Volej periodicky — alone_hours = jak dlouho nikdo není doma."""
        if alone_hours < 0.5:
            return
        if alone_hours < 2:
            target, intensity = "content", 0.5
        elif alone_hours < 6:
            target, intensity = "lonely", 0.6 + min(alone_hours / 20, 0.3)
        else:
            h = datetime.now().hour
            if 22 <= h or h < 6:
                target, intensity = "melancholic", 0.8
            else:
                target, intensity = "lonely", 0.9
        self._shift(target, intensity, f"sám {alone_hours:.1f}h")

    def update_objects(self, objects: list[str]):
        """Aktualizuj seznam viděných objektů — může změnit náladu."""
        prev = set(self._last_objects)
        curr = set(objects)
        new_objects = curr - prev
        if new_objects:
            # Nový objekt → curiosity spike
            if self._state.mood not in ("engaged", "worried"):
                self._shift("curious", 0.6, f"nový objekt: {list(new_objects)[0]}")
        # Neznámý člověk (Unknown) v objektech?
        if "unknown_person" in objects:
            self._shift("worried", 0.7, "neznámá tvář")
        self._last_objects = list(objects)

    def update_kodi(self, title: str):
        if title != self._kodi_title:
            self._kodi_title = title
            if title and self._state.mood in ("lonely", "content"):
                self._shift("curious", 0.5, f"Kodi hraje: {title}")

    def update_weather(self, code: int):
        self._weather_code = code
        # Bouřka, déšť → lehce melancholická nálada
        if code in (61, 63, 65, 80, 81, 95) and self._state.mood == "content":
            self._shift("melancholic", 0.4, "prší")

    def tick(self):
        """Periodický recompute — volá hans_idle._tick každých pár sekund.
        Zaručí, že se mood ‚uklidní' i bez nových eventů."""
        now = time.time()
        cutoff = now - MOOD_WINDOW_S
        self._state.mood_votes = [
            v for v in self._state.mood_votes if v[0] >= cutoff
        ]
        self._recompute_mood()

    def midnight_reset(self):
        """Zavolej o půlnoci — reset daily counter."""
        self._state.observations_today = 0

    # ── Interní ───────────────────────────────────────────────────────────────

    def _shift(self, new_mood: str, intensity: float, reason: str):
        """Přidá hlas pro novou náladu. Skutečný přepis udělá _recompute_mood()."""
        now = time.time()
        self._state.mood_votes.append((now, new_mood, intensity, reason))
        # Ořež staré hlasy (starší než okno)
        cutoff = now - MOOD_WINDOW_S
        self._state.mood_votes = [
            v for v in self._state.mood_votes if v[0] >= cutoff
        ]
        self._recompute_mood()

    def _recompute_mood(self):
        """Vypočítá dominantní mood z hlasů s exp. decay a hysterezí.
        Přepne current mood jen pokud:
          1) uplynul MOOD_MIN_HOLD_S od posledního přepnutí
          2) dominantní kandidát má skóre >= current × MOOD_HYSTERESIS
        """
        import math
        now = time.time()
        if not self._state.mood_votes:
            return

        # Spočítej vážené skóre pro každý mood
        scores: dict[str, float] = {}
        latest_reason: dict[str, str] = {}
        for ts, mood, intensity, reason in self._state.mood_votes:
            age = now - ts
            # Exponenciální decay (halflife = MOOD_DECAY_HALFLIFE)
            decay = math.exp(-age * math.log(2) / MOOD_DECAY_HALFLIFE)
            weight = intensity * decay
            scores[mood] = scores.get(mood, 0.0) + weight
            # Pro reason si zapamatuj nejnovější hlas dané nálady
            latest_reason[mood] = reason

        # Dominantní mood
        top_mood = max(scores, key=scores.get)
        top_score = scores[top_mood]

        # Pokud je to stále stejný mood, nic neměníme
        if top_mood == self._state.mood:
            # Aktualizuj jen intenzitu (plynule)
            self._state.intensity = min(1.0, max(0.1, top_score / 2.0))
            return

        # Cooldown — nepřepínej dřív než MOOD_MIN_HOLD_S
        if now - self._state.last_shift < MOOD_MIN_HOLD_S:
            return

        # Hystereze — nový mood musí mít převahu nad aktuálním
        current_score = scores.get(self._state.mood, 0.0)
        if current_score > 0 and top_score < current_score * MOOD_HYSTERESIS:
            return

        # Provést přepnutí
        old = self._state.mood
        self._state.mood        = top_mood
        self._state.intensity   = min(1.0, max(0.1, top_score / 2.0))
        self._state.last_shift  = now
        self._state.shift_reason = latest_reason.get(top_mood, "")
        _log.info("Mood: %s → %s (%.2f, score=%.2f) — %s",
                  old, top_mood, self._state.intensity,
                  top_score, self._state.shift_reason)

    def _log_to_diary(self, old: str, new: str, reason: str):
        # Mood shifty se do deníku nezapisují — jen logujeme
        _log.debug("Mood: %s → %s (%s)", old, new, reason)