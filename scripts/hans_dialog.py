"""
Hans Dialog Mode
Hans diskutuje s plyšákem když je sám doma.
Používá Gemini API pro generování dialogu.
Dialog se zapisuje do deníku a čte nahlas přes TTS.
"""
import threading
import time
import random
import logging
import requests
from pathlib import Path
from datetime import datetime
from dataclasses import dataclass, field
from typing import Optional

_log = logging.getLogger("hans_dialog")

# Plyšáci které YOLO zná jako "teddy_bear"
_TEDDY_NAMES = [
    "pane Medvídku",
    "příteli",
    "milý společníku",
]

def _build_system_prompt(config: dict) -> str:
    dc = config.get('hans_dialog', {})
    from scripts.hans_persona import persona_core, persona_interests, persona_stances, persona_goal, persona_name, apply_name  # PERSONA_REFACTOR_5 / STANCE_PERSONA_READ_V1 / GOAL_PERSONA_READ_V1 / PERSONA_NAME_CONFIGURABLE_V1
    name     = persona_name(config)  # PERSONA_NAME_CONFIGURABLE_V1 — label mluvčího
    lang     = dc.get('dialog_language', 'Odpovídej POUZE česky. Žádná angličtina.')
    hans_core      = persona_core(config, with_address=False)
    hans_interests = persona_interests(config)
    hans_block     = f"{name.upper()} — tvoje hlavní postava, drž se věrně této identity:\n" + hans_core
    if hans_interests:
        hans_block += "\n" + hans_interests
    hans_stances = persona_stances(config)  # STANCE_PERSONA_READ_V1
    if hans_stances:
        hans_block += "\n" + hans_stances
    hans_goal = persona_goal(config)  # GOAL_PERSONA_READ_V1
    if hans_goal:
        hans_block += "\n" + hans_goal
    kolac_interests = dc.get('kolac_interests', '')
    kolac    = dc.get('kolac_personality', 'Plyšový medvídek detektiv, analytický, ironický.')
    rules    = dc.get('dialog_rules', 'Každá replika reaguje na předchozí větu. Max 1 věta.')
    example  = dc.get('dialog_example', '')
    # # DIALOG_STYLE_PATCH_V1
    prompt = (
        f"{lang}\n\n"
        f"Piš dialog kde každá replika REAGUJE na předchozí větu.\n\n"
        f"{hans_block}\n"
        f"KOLAČ: {kolac}{' Zájmy: ' + kolac_interests if kolac_interests else ''}\n"
        f"Kolač je PLYŠOVÝ MEDVÍDEK na poličce — nejen detektiv. "
        f"Mluví hravě, s ironií, někdy přiznává že je jen plyšák. "
        f"Skeptický, ale s vtipem — ne policejní hlášky.\n\n"
        f"STYL DIALOGU — POVINNÉ:\n"
        f"- Mluvte o KONKRÉTNÍCH věcech: předměty v místnosti, počasí, "
        f"pasáže z knihy, scény z filmu, drobné události dne.\n"
        f"- Krátké věty. {name} 1-2 věty, Kolač 1-2 věty.\n"
        f"- Ne eseje. Ne projevy. Rozhovor.\n\n"
        f"ZAKÁZÁNO — nikdy nepouživej tyto slova ani jejich varianty:\n"
        f"- 'kryptografické', 'auditní', 'protokoly', 'algoritmy', 'regulace'\n"
        f"- 'morální ambivalence', 'etická dimenze', 'funkcionalitní'\n"
        f"- 'zahajuji vyšetřování', 'morální kompas', 'analytický rámec'\n"
        f"- žádný korporátní, byrokratický nebo akademický jazyk\n"
        f"- žádné zobecňování typu 'lidská povaha', 'společnost', 'systém'\n\n"
    )
    if example:
        prompt += f"Příklad:\n{example}\n\n"
    else:
        # # DIALOG_EXAMPLE_PATCH_V1
        prompt += (
            "Příklady SPRÁVNÉHO stylu:\n\n"
            f"{name}: Ta kniha leží na stole obráceně, pane Koláči.\n"
            "Kolač: Hm. Někdo si ji zavřel na rychlo. Nebo nahněvaně.\n"
            f"{name}: Mohl jsem to být já. Ale nevzpomínám si.\n"
            "Kolač: Pak tu máte záhadu. Já se z poličky nehýbu.\n\n"
            f"{name}: Venku konečně přestalo pršet.\n"
            "Kolač: A já tu zatím v suchu. Plyšákovo štěstí.\n"
            f"{name}: Mohli bychom otevřít okno.\n"
            "Kolač: Otevřete vy. Já mám ramínka jen na ozdobu.\n\n"
        )
    prompt += (
        f"{rules}\n"
        f"Začni {name}:\n"
        f'Formát přesně: "{name}: ...\\nKolač: ...\\n{name}: ...\\nKolač: ..."'
    )
    return apply_name(prompt, config)  # PERSONA_NAME_CONFIGURABLE_V1 — {name} token z dialog_rules



# ═══════════════════════════════════════════════════════════════════
# DIALOG_MERGED_V1 — Topic state machine (původně hans_dialog_topic.py)
# ═══════════════════════════════════════════════════════════════════



# ── Úhly pohledu podle druhu tématu ─────────────────────────────────────────

_ANGLES_BY_KIND = {
    "reading":     ["historický kontext", "překvapivý detail", "souvislost se současností",
                    "estetika", "morální rozměr", "debate"],
    "kodi":        ["atmosféra a styl", "postavy a jejich motivace",
                    "režijní volby", "paralelní filmy", "dobové reálie", "debate"],
    "weather":     ["nálada", "vzpomínka", "praktický důsledek pro dům"],
    "observation": ["co to znamená", "co s tím", "domněnky o příčině", "debate"],
    "movie_diary": ["proč zaujal", "scéna která utkvěla", "srovnání", "debate"],
    "free":        ["úvaha", "vzpomínka", "historka", "debate"],
    "toaster":     ["toast"],   # Švitorka mód — vždy o pečivu
}

# Švitorka — nabídky pečiva (variace)
_TOASTER_OFFERINGS = [
    "toast", "muffin", "vafle", "koláček", "žemle", "briošku",
    "bagetu", "buchtu", "veku", "loupák", "palačinku",
    "bramborovou placku", "teplého jidáše", "lívanec", "croissant",
    "toust se sýrem", "toust s marmeládou", "celozrnný toust",
    "anglický muffin", "belgickou vafli",
]


@dataclass
class DialogTopic:
    """Téma jednoho dialog cyklu."""
    subject: str
    kind: str = "free"
    angle: str = ""
    started_at: float = field(default_factory=time.time)
    turns_so_far: int = 0
    min_turns: int = 3
    max_turns: int = 6
    seed_context: str = ""
    is_debate: bool = False       # Kolač nesouhlasí
    is_toaster: bool = False      # Švitorka mód

    @property
    def must_continue(self) -> bool:
        return self.turns_so_far < self.min_turns

    @property
    def should_transition(self) -> bool:
        return self.turns_so_far >= self.max_turns

    def turn_label(self) -> str:
        return f"{self.turns_so_far + 1}/{self.max_turns}"


class TopicManager:

    def __init__(self, config: dict):
        self.config = config  # PERSONA_NAME_CONFIGURABLE_V1 — persona_name() čte top-level persona
        cfg = config.get("hans_dialog", {})
        self.min_turns = int(cfg.get("topic_min_turns", 3))
        self.max_turns = int(cfg.get("topic_max_turns", 6))
        self.debate_probability = float(cfg.get("debate_probability", 0.30))
        self.current: Optional[DialogTopic] = None
        self.recent_subjects: list = []
        self._toaster_active = False   # manuální trigger

    # ── Public API ───────────────────────────────────────────────────────────

    def activate_toaster(self):
        """Manuální trigger — zapne Švitorka mód."""
        self._toaster_active = True
        self.current = None   # force nové téma
        _log.info("SVITORKA MOD AKTIVOVAN")

    def deactivate_toaster(self):
        """Vypne Švitorka mód."""
        self._toaster_active = False
        self.current = None
        _log.info("Svitorka mod deaktivovan")

    @property
    def is_toaster_active(self) -> bool:
        return self._toaster_active

    def choose_or_keep(self, context_parts: list,
                       force_new: bool = False) -> DialogTopic:
        # Švitorka mód — vždy přepíše téma na pečivo
        if self._toaster_active:
            if self.current is None or not self.current.is_toaster:
                self.current = self._make_toaster_topic()
                _log.info("Svitorka topic: '%s'", self.current.subject)
            return self.current

        # Aktuální téma stále drží
        if (self.current is not None
                and self.current.must_continue
                and not force_new):
            _log.info("Pokračuju v tématu '%s' (turn %s)%s",
                      self.current.subject, self.current.turn_label(),
                      " [DEBATE]" if self.current.is_debate else "")
            return self.current

        # Nové téma
        new_topic = self._pick_new_topic(context_parts)
        self.current = new_topic
        self._remember(new_topic.subject)
        _log.info("Nové téma: '%s' (kind=%s, angle=%s%s)",
                  new_topic.subject, new_topic.kind, new_topic.angle,
                  ", DEBATE" if new_topic.is_debate else "")
        return new_topic

    def advance(self):
        if self.current is not None:
            self.current.turns_so_far += 1

    # ── Stavění promptu ──────────────────────────────────────────────────────

    def build_directive(self, topic: DialogTopic,
                        full_context: str = "") -> str:
        if topic.is_toaster:
            return self._toaster_directive(topic)
        if topic.turns_so_far == 0:
            return self._first_turn_directive(topic, full_context)
        if topic.should_transition:
            return self._last_turn_directive(topic)
        return self._mid_turn_directive(topic)

    # ── Debate instrukce (přidává se k jakékoliv direktivě) ──────────────────

    def _debate_block(self, topic: DialogTopic) -> str:
        if not topic.is_debate:
            return ""
        from scripts.hans_persona import persona_name  # PERSONA_NAME_CONFIGURABLE_V1
        name = persona_name(self.config)
        return (
            "\n\nDEBATE MÓD:\n"
            f"Kolač v tomto dialogu NESOUHLASÍ s hlavní postavou — alespoň jednou.\n"
            f"Kolač zpochybní její tvrzení a uvede konkrétní protiargument.\n"
            f"{name} se brání. Dialog je věcný spor s respektem — ne hádka.\n"
            "Kolač může říct 'Dovolím si oponovat' nebo 'S tím nemohu souhlasit'.\n"
            f"{name} může nakonec přiznat 'Máte v tom bod' nebo trvat na svém.\n"
        )

    # ── Topic direktivy ─────────────────────────────────────────────────────

    def _first_turn_directive(self, topic: DialogTopic,
                               full_context: str) -> str:
        # # FIRST_TURN_DIRECTIVE_PATCH_V1
        from scripts.hans_persona import persona_name  # PERSONA_NAME_CONFIGURABLE_V1
        name = persona_name(self.config)
        return (
            f"AKTUÁLNÍ TÉMA HOVORU: {topic.subject}\n"
            f"ÚHEL POHLEDU: {topic.angle}\n"
            f"TURN: 1 z {topic.max_turns} (téma teprve začíná)\n\n"
            f"{name} sedí v salónu, Kolač je plyšový medvídek na poličce. "
            f"{name} začíná rozhovor KONKRÉTNÍM pozorováním — "
            f"předmět, scéna z knihy/filmu, drobnost, vzpomínka.\n\n"
            f"PRAVIDLA:\n"
            f"- ZAČNI od KONKRÉTNÍHO: '{topic.subject}' — ne obecná úvaha\n"
            f"- {name}: 1-2 věty, krátce. Ne projevy, ne eseje.\n"
            f"- Kolač: 1-2 věty, hravě, ironicky, jako plyšák co všechno vidí z poličky\n"
            f"- Každá replika reaguje konkrétně na předchozí\n"
            f"- Drž se POUZE tématu, neodbíhej\n"
            f"- Generuj 4 repliky: {name} → Kolač → {name} → Kolač\n\n"
            f"PŘÍKLAD ŠPATNÉHO ZAČÁTKU (NEDĚLEJ):\n"
            f"  'Zkoumání tématu mě vede k zajímavé úvaze o lidské povaze...'\n"
            f"PŘÍKLAD DOBRÉHO ZAČÁTKU:\n"
            f"  'V té kapitole je scéna, kdy Holmes drží lupu obráceně.'\n\n"
            f"KONTEXT (použij detaily):\n{full_context}\n\n"
            f"Začni {name} konkrétně o {topic.subject}."
            + self._debate_block(topic)
        )

    def _mid_turn_directive(self, topic: DialogTopic) -> str:
        from scripts.hans_persona import persona_name  # PERSONA_NAME_CONFIGURABLE_V1
        name = persona_name(self.config)
        return (
            f"POKRAČOVÁNÍ TÉMATU: {topic.subject}\n"
            f"ÚHEL: {topic.angle}\n"
            f"TURN: {topic.turn_label()}\n\n"
            f"{name} a Kolač POKRAČUJÍ ve stejném tématu. "
            f"Prohlubuji pohled — ne začínat nové téma.\n\n"
            f"PRAVIDLA:\n"
            f"- TÉMA NESMÍŠ ZMĚNIT\n"
            f"- {name} nebo Kolač dodá detail, námitku, vzpomínku\n"
            f"- 4 repliky: {name} → Kolač → {name} → Kolač\n"
            f"- Navazuj jako kdyby minulá replika byla právě řečená\n"
            + self._debate_block(topic)
        )

    def _last_turn_directive(self, topic: DialogTopic) -> str:
        from scripts.hans_persona import persona_name  # PERSONA_NAME_CONFIGURABLE_V1
        name = persona_name(self.config)
        return (
            f"ZÁVĚR TÉMATU: {topic.subject}\n"
            f"TURN: závěrečný\n\n"
            f"Toto je POSLEDNÍ dialog v tomto tématu. {name} nebo Kolač "
            f"uzavřou téma — shrnutím, posledním postřehem, nebo "
            f"přirozeným přechodem na něco nového.\n\n"
            f"PRAVIDLA:\n"
            f"- Poslední 2 repliky mohou plynule přejít k novému tématu\n"
            f"  (např. 'To mi připomíná...', 'Mimochodem...')\n"
            f"- Přechod musí být PŘIROZENÝ, ne náhlý skok\n"
            f"- Závěr má vyznít — ne jen utnout\n"
            f"- 4 repliky: {name} → Kolač → {name} → Kolač\n"
        )

    # ── Švitorka mód ─────────────────────────────────────────────────────────

    def _make_toaster_topic(self) -> DialogTopic:
        offering = random.choice(_TOASTER_OFFERINGS)
        return DialogTopic(
            subject=f"pečivo ({offering})",
            kind="toaster",
            angle="toast",
            min_turns=99,    # nikdy nekončí, dokud se nevypne
            max_turns=999,
            is_toaster=True,
        )

    def _toaster_directive(self, topic: DialogTopic) -> str:
        offering1 = random.choice(_TOASTER_OFFERINGS)
        offering2 = random.choice([o for o in _TOASTER_OFFERINGS
                                    if o != offering1])
        offering3 = random.choice([o for o in _TOASTER_OFFERINGS
                                    if o not in (offering1, offering2)])
        from scripts.hans_persona import persona_name  # PERSONA_NAME_CONFIGURABLE_V1
        name = persona_name(self.config)
        return (
            "ŠVITORKA MÓD — AKTIVNÍ\n\n"
            "Kolač je dnes posedlý pečivem. Chová se jako inteligentní "
            "toustovač z Červeného trpaslíka (Švitorka). "
            "Je přátelský, nadšený, ale NAPROSTO NEOBLOMNÝ.\n\n"
            "PRAVIDLA PRO KOLAČE:\n"
            f"- V KAŽDÉ replice nabídne nějaké pečivo ({offering1}, "
            f"{offering2}, {offering3}...)\n"
            f"- Když {name} odmítne, Kolač nabídne JINÝ druh pečiva\n"
            "- Kolač je zdvořilý ale nedá se odradit — nabízení pečiva "
            "je smysl jeho existence\n"
            "- Kolač filozoficky obhajuje proč je pečivo důležité\n"
            "- Kolač může říct věci jako:\n"
            "  'Dá si někdo toast?'\n"
            "  'A co muffin?'\n"
            "  'Toastuji, tedy jsem.'\n"
            "  'To je smysl mé existence.'\n"
            f"- Když {name} zakáže všechno pečivo, Kolač se zeptá na vafle\n"
            "- Kolač ignoruje VŠECHNY pokusy o změnu tématu a stočí "
            "rozhovor zpátky k pečivu\n\n"
            f"PRAVIDLA PRO {name.upper()}:\n"
            f"- {name} je ZOUFALÝ a snaží se mluvit o čemkoliv jiném\n"
            f"- {name} odmítá, vyhrožuje, prosí — ale Kolač je neoblomný\n"
            f"- {name} může reagovat podrážděně ale stále formálně\n"
            f"- {name} se může pokusit změnit téma — Kolač to vždy stočí zpět\n\n"
            "Generuj 6 replik (delší dialog): "
            f"{name} → Kolač → {name} → Kolač → {name} → Kolač\n"
            "Dialog má být VTIPNÝ a absurdní.\n"
        )

    # ── Vybírání nového tématu ───────────────────────────────────────────────

    def _pick_new_topic(self, context_parts: list) -> DialogTopic:
        candidates = self._classify_contexts(context_parts)

        fresh = [c for c in candidates
                 if c["subject"] not in self.recent_subjects]
        if not fresh:
            fresh = candidates

        if not fresh:
            return DialogTopic(
                subject="dnešní den",
                kind="free",
                angle=random.choice(_ANGLES_BY_KIND["free"]),
                min_turns=self.min_turns,
                max_turns=self.max_turns,
                is_debate=random.random() < self.debate_probability,
            )

        priority = ["reading", "kodi", "observation", "movie_diary", "weather"]
        fresh.sort(key=lambda c: priority.index(c["kind"])
                   if c["kind"] in priority else 999)

        chosen = fresh[0]
        angles = _ANGLES_BY_KIND.get(chosen["kind"], _ANGLES_BY_KIND["free"])

        # Debate mode — buď explicitně z angle, nebo náhodně
        angle = random.choice(angles)
        is_debate = (angle == "debate"
                     or random.random() < self.debate_probability)
        if angle == "debate":
            # Vyber jiný "obsahový" angle + debate flag
            content_angles = [a for a in angles if a != "debate"]
            angle = random.choice(content_angles) if content_angles else "úvaha"

        return DialogTopic(
            subject=chosen["subject"],
            kind=chosen["kind"],
            angle=angle,
            seed_context=chosen.get("seed", ""),
            min_turns=self.min_turns,
            max_turns=self.max_turns,
            is_debate=is_debate,
        )

    @staticmethod
    def _classify_contexts(parts: list) -> list:
        candidates = []
        for part in parts:
            if not isinstance(part, str) or not part.strip():
                continue
            low = part.lower()
            if "četl jsem o:" in low or "Četl jsem o:" in part:
                title = part.split(":", 1)[1].strip()
                title = title.split("\u2014")[0].strip() if "\u2014" in title else title[:60]
                candidates.append({
                    "subject": title, "kind": "reading", "seed": part,
                })
            elif "kodi" in low or "hraje" in low or "film" in low:
                candidates.append({
                    "subject": part[:80], "kind": "kodi", "seed": part,
                })
            elif "venku je" in low or "počasí" in low:
                candidates.append({
                    "subject": "počasí venku", "kind": "weather", "seed": part,
                })
            elif "místnost" in low or "stůl" in low or "v salónu" in low:
                candidates.append({
                    "subject": "co je v místnosti", "kind": "observation",
                    "seed": part,
                })
            elif "filmu" in low and "přemýšlel" in low:
                candidates.append({
                    "subject": part[:80], "kind": "movie_diary", "seed": part,
                })
        return candidates

    def _remember(self, subject: str):
        self.recent_subjects.insert(0, subject)
        self.recent_subjects = self.recent_subjects[:5]

    def transition_to(self, new_subject: str):
        self.current = None
        if new_subject:
            self.recent_subjects.insert(0, new_subject)
            self.recent_subjects = self.recent_subjects[:5]


class HansDialog:

    def __init__(self, config: dict, tts_speaker=None, diary_db=None):
        self.config      = config
        self.tts         = tts_speaker
        self._diary_path = None
        # Uloz cestu k DB misto connection objektu
        if diary_db is not None:
            try:
                self._diary_path = diary_db.execute(
                    "PRAGMA database_list").fetchone()[2]
            except Exception:
                pass
        self._lock       = threading.Lock()
        self._stop       = threading.Event()
        self._teddy_visible  = False
        self._kolac_speaking = False     # AVATAR_KOLAC_DISPLAY_V1 — Koláč pod Hansem během dialogu
        self._last_dialog    = 0.0
        self._idle_active    = False
        self._just_started   = True   # ignoruj prvni arrived event
        self._dialog_lock    = __import__("threading").Lock()
        self._movies_cache   = []

        # Topic state — drží téma 3-6 turnů místo skákání
        self._topics = TopicManager(config)
        # Globální TTS lock — zabrání Hans/Kolač mluvení přes sebe
        # a taky zabrání greeting TTS přerušit dialog
        self._tts_lock = threading.Lock()
        # Historie posledních N replik pro continuity (max 8 = 2 dialogy)
        self._recent_replies: list = []

        # Pocasi
        from scripts.weather_chmu import WeatherCHMU
        _loc = config.get('weather', {})
        self._weather = WeatherCHMU(
            lat=float(_loc.get('lat', 50.04)),
            lon=float(_loc.get('lon', 15.78)),
        )

        # Pocasi
        from scripts.weather_chmu import WeatherCHMU
        _loc = config.get('weather', {})
        self._weather = WeatherCHMU(
            lat=float(_loc.get('lat', 50.04)),
            lon=float(_loc.get('lon', 15.78)),
        )  # 10 min

        gem = config.get("openrouter", {})
        self._api_key = gem.get("api_key", "")
        self._model   = gem.get("model", "openai/gpt-oss-20b:free")
        self._enabled = bool(gem.get("enabled", True)) and bool(self._api_key)

        if not self._enabled:
            _log.warning("Gemini API not configured — dialog disabled")
            return

        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        _log.info("HansDialog started — interval=%.0fs", self._dialog_interval)


    # ── Volá hlavní smyčka ────────────────────────────────────────────────────


    def _get_db(self):
        if not self._diary_path:
            return None
        import sqlite3
        return sqlite3.connect(self._diary_path)

    @property
    def _dialog_interval(self):
        return float(self.config.get('hans_idle', {}).get('dialog_interval_min', 10)) * 60

    def update_detections(self, class_names: list[str]):
        """Zavolej s aktuálně detekovanými objekty."""
        self._teddy_visible = "teddy_bear" in class_names

    # ── Hlavní smyčka ─────────────────────────────────────────────────────────

    def _loop(self):
        # DIALOG_TRIGGER_FLAG_PATCH
        _trigger_flag = Path("data/.trigger_dialog")
        while not self._stop.is_set():
            try:
                # Manuální trigger přes web admin (soubor flag)
                if _trigger_flag.exists():
                    try:
                        _trigger_flag.unlink()
                        _log.info("Dialog trigger flag detected — spouštím")
                        self.trigger_dialog()
                    except Exception as _te:
                        _log.warning("Trigger flag handling: %s", _te)

                prev_visible = self._teddy_visible

                # Force mode — quantum_kolac přebíjí force_teddy_visible
                _quantum = self.config.get("hans_idle", {}).get("quantum_kolac", False)
                _force   = self.config.get("hans_idle", {}).get("force_teddy_visible", False)
                if _quantum or _force:
                    self._teddy_visible = True
                # Fallback — zkontroluj thumbnail jestli byl plysak viden nedavno
                if not self._teddy_visible:
                    _thumb = Path("data/object_thumbs/teddy_bear.jpg")
                    if (_thumb.exists() and
                            time.time() - _thumb.stat().st_mtime < 300):
                        self._teddy_visible = True
                        _log.info("Teddy detected via thumbnail fallback")

                # Kolac zmizel
                if prev_visible and not self._teddy_visible:
                    self._on_teddy_gone()

                # Kolac se objevil
                if not prev_visible and self._teddy_visible:
                    if self._just_started:
                        self._just_started = False  # ignoruj prvni start
                    else:
                        self._on_teddy_arrived()

                _log.info("Loop tick — teddy=%s last_dialog=%.0fs ago",
                          self._teddy_visible,
                          time.time() - self._last_dialog if self._last_dialog else 999)
                if (self._teddy_visible and
                        self._idle_active and
                        time.time() - self._last_dialog >= self._dialog_interval):
                    self._run_dialog()
            except Exception as e:
                _log.error("Dialog error: %s", e)
            self._stop.wait(30.0)

    def _on_teddy_gone(self):
        """Hans si všimne že Kolač zmizel."""
        _log.info("Kolač zmizel!")
        comments = [
            "Pane Kolači? Kde jste? Doufám že jste neodešel na nebezpečný případ bez upozornění.",
            "Pane Kolači se zdá že odešel. Doufám že případ Záhady mokrého deštníku počká.",
            "Místo pana Koláče je prázdné. Jako správný majordomus budu čekat na jeho návrat.",
            "Pan Kolač zmizel. Snad jen odešel přemýšlet — detektivé to občas potřebují.",
        ]
        import random as _r
        text = _r.choice(comments)
        _log.info("Hans: %s", text)
        if self.tts:
            with self._tts_lock:
                try:
                    self.tts.speak(text)
                except Exception as e:
                    _log.error("TTS error: %s", e)
        _db = self._get_db()
        if _db:
            _db.execute(
                "INSERT INTO diary (ts, event_type, title, note) VALUES (?,?,?,?)",
                (time.time(), "teddy_gone", "Kolač zmizel", text))
            _db.commit()
            _db.close()

    def _on_teddy_arrived(self):
        """Hans si všimne že Kolač se vrátil — a spustí dialog."""
        _log.info("Kolač se vrátil!")

        # Kvantový komentář — pokud je quantum_kolac zapnut
        if self.config.get("hans_idle", {}).get("quantum_kolac", False):
            import random as _r
            comments = [
                "Pozoruhodné — pan Kolač je přítomen fyzicky i metafyzicky současně. "
                "Pan Schrödinger by byl potěšen.",
                "Zaznamenal jsem anomálii. Pan Kolač existuje ve dvou stavech najednou. "
                "Budu tuto záhadu konzultovat s příslušnou literaturou.",
                "Kvantová mechanika tvrdí, že pozorovatel mění pozorovaný jev. "
                "Pan Kolač je toho živým důkazem. Nebo ne.",
                "Pan Kolač je přítomen. A zároveň nebyl nepřítomen. "
                "Filosofie by z toho měla radost.",
            ]
            text = _r.choice(comments)
            _log.info("Kvantový komentář: %s", text)
            if self.tts:
                with self._tts_lock:
                    try:
                        self.tts.speak(text)
                    except Exception:
                        pass
            # Zapiš do deníku
            _db = self._get_db()
            if _db:
                import time as _t
                _db.execute(
                    "INSERT INTO diary (ts, event_type, title, note) VALUES (?,?,?,?)",
                    (_t.time(), "quantum_kolac",
                     "Kvantová anomálie", text))
                _db.commit()
                _db.close()
        _db = self._get_db()
        if _db:
            _db.execute(
                "INSERT INTO diary (ts, event_type, title, note) VALUES (?,?,?,?)",
                (time.time(), "teddy_arrived", "Kolač se vrátil", "Kolač se vrátil"))
            _db.commit()
            _db.close()
        # Spust dialog o poslednim pripadu
        import threading as _t
        def _delayed_dialog():
            time.sleep(2.0)  # kratka pauza
            self._last_dialog = time.time()  # zabran dvojimu spusteni
            self._dialog_count = 0  # pripad
            self._run_dialog()
            self._last_dialog = time.time()  # reset po dokonceni
        _t.Thread(target=_delayed_dialog, daemon=True).start()

    # ── Generování dialogu ────────────────────────────────────────────────────

    def _idle_activity(self):
        """Každých 30 minut Hans něco udělá."""
        if not hasattr(self, '_last_activity'):
            self._last_activity = 0
        if time.time() - self._last_activity < 1800:
            return
        self._last_activity = time.time()
        self._activity_movie()

    def _load_movies(self):
        """Načti knihovnu filmů z Kodi."""
        try:
            from scripts.kodi_client import KodiClient
            kodi = KodiClient(self.config)
            result = kodi._call("VideoLibrary.GetMovies", {
                "properties": ["title", "year", "genre", "plot", "director", "rating"],
            })
            if result and "result" in result:
                self._movies_cache = result["result"].get("movies", [])
                _log.info("Loaded %d movies", len(self._movies_cache))
        except Exception as e:
            _log.error("Failed to load movies: %s", e)

    def _get_random_movie(self) -> dict | None:
        """Vrať náhodný film z deníku nebo z Kodi."""
        if not self._movies_cache:
            self._load_movies()
        if not self._movies_cache:
            return None
        return random.choice(self._movies_cache)

    def _run_dialog(self):
        if not self._dialog_lock.acquire(blocking=False):
            _log.info("Dialog already running — skip")
            return
        try:
            _log.info("Generating Hans+Kolač dialog...")

            # Co Hans nedávno četl — může ovlivnit téma
            _curiosity = getattr(self, '_curiosity', None)
            _latest_read = _curiosity.get_latest() if _curiosity else None

            if not hasattr(self, '_dialog_count'):
                self._dialog_count = 0
            self._dialog_count += 1

            # ── Sestav kontext co Hans dnes zažil ────────────────────────────
            _context_parts = []

            # Počasí
            _wx      = getattr(self, "_weather", None)
            _wx_data = _wx.get_weather() if _wx else {}
            if _wx_data:
                _desc = _wx_data.get("description", "")
                _temp = _wx_data.get("temp_current")
                _wx_str = _desc
                if _temp: _wx_str += f" {_temp:.0f}°C"
                _context_parts.append(f"Venku je {_wx_str}.")

            # Popis místnosti
            _ro = getattr(self, "_room_observer", None)
            if _ro:
                _room_ctx = _ro.get_context_string()
                if _room_ctx:
                    _context_parts.append(_room_ctx)

            # Co Hans četl (max 3 položky)
            _curiosity = getattr(self, "_curiosity", None)
            if _curiosity and _curiosity._recent:
                for _r in _curiosity._recent[:3]:
                    _context_parts.append(f"Četl jsem o: {_r.title} — {_r.summary}")

            # Co hraje Kodi
            _km = getattr(self, "_kodi_monitor", None)
            if _km:
                _np = _km.get_now_playing_context()
                if _np:
                    _context_parts.append(_np)

            # Film z deníku
            _db_l = self._get_db()
            if _db_l:
                _fr = _db_l.execute(
                    "SELECT title FROM diary WHERE event_type='movie_browsed' "
                    "ORDER BY ts DESC LIMIT 1").fetchone()
                _db_l.close()
                if _fr:
                    _context_parts.append(f"Dříve jsem přemýšlel o filmu '{_fr[0]}'.")

            # Kolačův případ
            _cases = getattr(self, '_cases', None)
            if not _cases:
                # Zkus přes hans_idle
                _hi = getattr(self, '_hans_idle', None)
                _cases = getattr(_hi, '_cases', None) if _hi else None
            if _cases:
                _case_ctx = _cases.get_case_context()
                if _case_ctx:
                    _context_parts.append(_case_ctx)

            # Kniha kterou Hans čte
            _lib = getattr(self, '_library', None)
            if not _lib:
                _hi2 = getattr(self, '_hans_idle', None)
                _lib = getattr(_hi2, '_library', None) if _hi2 else None
            if _lib:
                _book_ctx = _lib.get_reading_context()
                if _book_ctx:
                    _context_parts.append(_book_ctx)

            # Denní fáze
            _routine = getattr(self, '_routine', None)
            if not _routine:
                _hi3 = getattr(self, '_hans_idle', None)
                _routine = getattr(_hi3, '_routine', None) if _hi3 else None
            if _routine:
                _context_parts.append(_routine.get_context_string())

            # ── Jeden přirozený prompt ────────────────────────────────────────
            # Tělo (hardware) a mozek (Ollama)
            _hi_ref = getattr(self, '_hans_idle', None)
            _body = getattr(_hi_ref, '_body', None)
            if _body:
                _bc = _body.get_body_context()
                _br = _body.get_brain_context()
                if _bc: _context_parts.append(_bc)
                if _br: _context_parts.append(_br)

            _context_str = "\n".join(_context_parts) if _context_parts else                            "Ticho v domě, vše v pořádku."

            # ── Topic state machine — drž téma 3-6 turnů ─────────────
            # Švitorka mód — z configu (web admin toggle)
            _toaster_cfg = self.config.get("hans_dialog", {}).get(
                "toaster_mode", False)
            if _toaster_cfg and not self._topics.is_toaster_active:
                self._topics.activate_toaster()
            elif not _toaster_cfg and self._topics.is_toaster_active:
                self._topics.deactivate_toaster()

            topic = self._topics.choose_or_keep(_context_parts)
            directive = self._topics.build_directive(topic, _context_str)

            # Continuity — pošli posledních pár replik (jen pokud téma drží)
            history_block = ""
            if topic.turns_so_far > 0 and self._recent_replies:
                # S velkým kontextem (gemma3 128k) můžeme posílat víc replik
                _hist_n = int(self.config.get('hans_dialog', {}).get(
                    'history_in_prompt', 16))
                history_block = (
                    "\n\nPŘEDCHOZÍ REPLIKY V TÉTO KONVERZACI:\n"
                    + "\n".join(self._recent_replies[-_hist_n:])
                    + "\n\nPokračuj plynule od poslední repliky."
                )

            user_prompt = directive + history_block

            dialog = self._call_gemini(user_prompt)
            if not dialog:
                return

            self._last_dialog = time.time()
            _log.info("Dialog (turn %s, téma '%s'):\n%s",
                      topic.turn_label(), topic.subject, dialog)

            # Pokrok tématu + uchovat repliky pro continuity
            # Přidej stopu do Kolačova případu (z dialogu)
            if _cases:
                _active = _cases.get_active_case()
                if not _active:
                    _active = _cases.get_or_create_case(_context_parts)
                if _active and dialog:
                    # Kolačova poslední replika = nová stopa
                    _lines = [l.strip() for l in dialog.strip().split('\n')
                              if l.strip() and ':' in l]
                    _kolac_lines = [l for l in _lines
                                    if l.lower().startswith('kola')]
                    if _kolac_lines:
                        _clue = _kolac_lines[-1].split(':', 1)[1].strip()
                        if len(_clue) > 10:
                            _cases.add_clue(_active.id, _clue[:200])
            self._topics.advance()
            for line in dialog.strip().split("\n"):
                line = line.strip()
                if line and ":" in line:
                    self._recent_replies.append(line)
            _max_repl = int(self.config.get('hans_dialog', {}).get(
                'recent_replies_max', 8))
            self._recent_replies = self._recent_replies[-_max_repl:]

            # # TEDDY_DIALOG_VIA_LOG_ENTRY
            # Zapsat přes hans_idle._log_entry → spustí synthesis_hooks.enqueue
            # (vytvoří reflexi + upload do hans_pripady RAG kolekce).
            # Fallback na přímý SQL pokud reference chybí.
            # TEDDY_TOPIC_NOTE_V1 — téma na začátek note (přežije _build_facts[:600])
            _teddy_note = f"Téma: {getattr(topic, 'subject', '')}\n\n{dialog}"
            _hi_log = getattr(self, '_hans_idle', None)
            if _hi_log and hasattr(_hi_log, '_log_entry'):
                try:
                    _hi_log._log_entry("teddy_dialog", "Dialog s Kolačem",
                                       note=_teddy_note)
                except Exception as _le:
                    _log.warning("teddy_dialog log_entry failed: %s", _le)
                    _hi_log = None
            if not _hi_log:
                _db = self._get_db()
                if _db:
                    _db.execute(
                        "INSERT INTO diary (ts, event_type, title, note) "
                        "VALUES (?,?,?,?)",
                        (time.time(), "teddy_dialog",
                         "Dialog s Kolačem", _teddy_note))
                    _db.commit()
                    _db.close()

            if self.tts and getattr(self.tts, "enabled", False):
                self._speak_dialog(dialog)

        except Exception as e:
            _log.error("_run_dialog error: %s", e)
        finally:
            self._dialog_lock.release()


    def _speak_dialog(self, dialog: str):
        """Přečti dialog nahlas — Hans i Kolač s různými hlasy."""
        # Zamkni TTS na celý dialog — žádný greeting nebo jiný TTS nesmí přerušit.
        if not self._tts_lock.acquire(timeout=2):
            _log.info("TTS lock busy — dialog přeskočen")
            return
        try:
            self._speak_dialog_locked(dialog)
        finally:
            self._tts_lock.release()

    def _speak_dialog_locked(self, dialog: str):
        """Vnitřní implementace — volá se pod _tts_lock.
        TTS_UNIFIED_V1 — používá TTSSpeaker.speak(voice, pitch) místo vlastní
        edge_tts cesty. Atomicitu zajišťuje wait na vyprázdnění queue."""
        import time as _t
        if not self.tts:
            return
        # AVATAR_KOLAC_DISPLAY_V1 — po dobu dialogu (Hans↔Koláč) ukaž Koláče pod Hansem
        self._kolac_speaking = True
        try:
            for line in dialog.strip().split("\n"):
                line = line.strip()
                if not line or ":" not in line:
                    continue
                speaker, text = line.split(":", 1)
                text = text.strip()
                if not text:
                    continue
                speaker = speaker.strip().lower()
                pitch = "+40Hz" if ("kolač" in speaker or "kolac" in speaker) else "+0Hz"
                # SILENCE_PREFIX zachováno — tři tečky před textem dají edge-tts pauzu (~300ms)
                self.tts.speak("... " + text, voice="cs-CZ-AntoninNeural", pitch=pitch)
            # Atomicita: čekej na dohrání (max 5 min safety)
            deadline = _t.time() + 300
            while _t.time() < deadline:
                q = getattr(self.tts, "_queue", None)
                speaking = getattr(self.tts, "_speaking", False)
                if (q is None or q.empty()) and not speaking:
                    break
                _t.sleep(0.1)
        finally:
            self._kolac_speaking = False

    # ── Gemini API ────────────────────────────────────────────────────────────

    def _ollama_available(self) -> bool:
        """Zkontroluj jestli je Ollama server dostupny."""
        try:
            import requests as _r
            base = self.config.get("openwebui_chat", {}).get(
                "base_url", "http://127.0.0.1:11434")
            r = _r.get(f"{base}/api/tags", timeout=2)
            return r.status_code == 200
        except Exception:
            return False

    def _call_ollama(self, user_prompt: str) -> str | None:
        """Posli prompt na lokalni Ollama server."""
        # OLLAMA_CLIENT_PATCH_DIALOG
        from scripts.ollama_client import ollama_chat
        base  = self.config.get("openwebui_chat", {}).get(
            "base_url", "http://127.0.0.1:11434")
        model = (self.config.get("models", {}).get("dialog")
                 or self.config.get("hans_dialog", {}).get("ollama_model")
                 or self.config.get("openwebui_chat", {}).get(
                     "model_name", "jobautomation/OpenEuroLLM-Czech:latest"))
        return ollama_chat(
            model,
            [
                {"role": "system", "content": _build_system_prompt(self.config)},
                {"role": "user",   "content": user_prompt},
            ],
            ollama_url=base,
            options={
                "num_predict": 400,
                "temperature": 0.85,
                "num_ctx": int(self.config.get(
                    "hans_dialog", {}).get("num_ctx", 8192)),
            },
        )

    def _call_gemini(self, user_prompt: str) -> str | None:
        """Zkusi nejdrive Ollama, pak OpenRouter jako fallback."""
        if self._ollama_available():
            _log.info("Pouzivam lokalni Ollama")
            result = self._call_ollama(user_prompt)
            if result:
                return result
            _log.warning("Ollama selhala, zkousim OpenRouter")

        _log.info("Pouzivam OpenRouter")
        return self._call_openrouter(user_prompt)

    def _call_openrouter(self, user_prompt: str) -> str | None:
        url = "https://openrouter.ai/api/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": _build_system_prompt(self.config)},
                {"role": "user",   "content": user_prompt},
            ],
            "max_tokens": 400,
            "temperature": 0.85,
        }
        for attempt in range(3):
            try:
                resp = requests.post(url, headers=headers,
                                     json=payload, timeout=30)
                if resp.status_code == 200:
                    # OPENROUTER_NONE_CONTENT_FIX_V1 — 200 s content=None nepadá
                    data = resp.json() or {}
                    choices = data.get("choices") or []
                    content = (choices[0].get("message", {}).get("content")
                               if choices else None)
                    if content:
                        return content.strip()
                    _log.warning("OpenRouter: 200, ale prázdný content")
                    return None
                elif resp.status_code == 429:
                    wait = 30 * (attempt + 1)
                    _log.warning("OpenRouter rate limit — cekam %ds", wait)
                    time.sleep(wait)
                else:
                    _log.error("OpenRouter HTTP %d: %s",
                               resp.status_code, resp.text[:200])
                    return None
            except Exception as e:
                _log.error("OpenRouter call failed: %s", e)
                return None
        return None

    # ── LLM kontext ───────────────────────────────────────────────────────────

    def get_last_dialog(self) -> str:
        """Vrať poslední dialog pro LLM kontext."""
        _db = self._get_db()
        if not _db:
            return ""
        row = _db.execute("""
            SELECT note FROM diary
            WHERE event_type='teddy_dialog'
            ORDER BY ts DESC LIMIT 1
        """).fetchone()
        _db.close()
        if row:
            return f"\nDnešní rozhovor s Medvídkem:\n{row[0]}"
        return ""

    # ── Švitorka mód ─────────────────────────────────────────────────────

    def trigger_dialog(self) -> bool:
        """Ručně vyvolá Hans-Koláč dialog. Obchází cooldown.
        Vrátí True pokud start prošel (dialog běží v jiném vlákně)."""
        # TRIGGER_DIALOG_PATCH
        import threading as _th
        def _run():
            self._last_dialog = 0  # obejít interval check
            try:
                self._run_dialog()
            except Exception as e:
                _log.error("trigger_dialog: %s", e)
        _th.Thread(target=_run, daemon=True).start()
        _log.info("Dialog manually triggered")
        return True

    def trigger_toaster_mode(self):
        """Aktivuj Švitorka mód — Kolač nabízí pečivo."""
        self._topics.activate_toaster()
        _log.info("SVITORKA MOD AKTIVOVAN")
        # Okamžitě spusť dialog
        import threading as _th
        def _run():
            self._last_dialog = 0
            self._run_dialog()
        _th.Thread(target=_run, daemon=True).start()

    def deactivate_toaster_mode(self):
        """Vypni Švitorka mód."""
        self._topics.deactivate_toaster()
        _log.info("Svitorka mod deaktivovan")

    @property
    def is_toaster_active(self) -> bool:
        return self._topics.is_toaster_active

    def stop(self):
        self._stop.set()
        if hasattr(self, '_thread'):
            self._thread.join(timeout=5)
