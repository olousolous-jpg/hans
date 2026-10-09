"""
Hans Routine — denní rytmus a noční mód.

Fáze dne:
  ráno    (06-12): počasí, ranní komentář, plán dne
  odpoledne (12-17): četba, curiosity, klid
  večer   (17-22): reflexe, intenzivnější dialogy
  noc     (22-06): "spánek" — zastaví aktivitu, shrne den, sny

Hans se chová jinak v každé fázi — jiné tempo dialogů,
jiné téma introspekce, jiný druh aktivity.

Použití:
    routine = HansRoutine(config, diary_db_path)
    routine.start()

    # Volej z hlavní smyčky:
    phase = routine.current_phase      # "morning" / "afternoon" / ...
    routine.on_phase_change(callback)  # notifikace při změně
"""

import json
import logging
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from typing import Callable, Optional

from scripts.hans_evening_reflection import HansEveningReflection

NL_RUNTIME = chr(10)  # G5D_VERIFY_BEFORE_DIARY_V1
import urllib.parse as _up  # G5F_VERIFY_FULLTEXT_V1

_log = logging.getLogger("hans_routine")


# ── Fáze dne ─────────────────────────────────────────────────────────────────

PHASE_MORNING   = "morning"      # 06-12
PHASE_AFTERNOON = "afternoon"    # 12-17
PHASE_EVENING   = "evening"      # 17-22
PHASE_NIGHT     = "night"        # 22-06

_PHASE_SCHEDULE = [
    (6,  PHASE_MORNING),
    (12, PHASE_AFTERNOON),
    (17, PHASE_EVENING),
    (22, PHASE_NIGHT),
]

_PHASE_LABELS_CZ = {
    PHASE_MORNING:   "ráno",
    PHASE_AFTERNOON: "odpoledne",
    PHASE_EVENING:   "večer",
    PHASE_NIGHT:     "noc",
}

# Komentáře Hanse při přechodu fáze
_PHASE_COMMENTS = {
    PHASE_MORNING: [
        "Dobré ráno. Čas připravit dům na nový den.",
        "Ráno. Slunce vychází a s ním i povinnosti dne.",  # PERSONA_REFACTOR_11
        "Nový den. Doufám, že bude klidnější než včerejšek.",
    ],
    PHASE_AFTERNOON: [
        "Poledne. Čas na krátkou introspekci u dobrého čtení.",
        "Odpoledne se blíží. Dům je v pořádku, mohu si dovolit chvíli ticha.",
        "Polovinu dne mám za sebou. Vše probíhá podle plánu.",
    ],
    PHASE_EVENING: [
        "Večer se blíží. Čas na reflexi uplynulého dne.",
        "Stmívá se. Obvyklá doba, kdy se rodina vrací domů.",
        "Večerní hodiny. Dům se pomalu ukládá ke klidu.",
    ],
    PHASE_NIGHT: [
        "Noc. Dům je tichý. Čas na odpočinek — i pro mě.",
        "Přeji dobrou noc. Zítra bude nový den plný povinností.",
        "Noční klid nastal. Budu přemýšlet o událostech dne.",
    ],
}

# Hansovy sny — surreální myšlenky generované v noci
_DREAM_SEEDS = [
    "Zdálo se mi, že dům měl nekonečně mnoho pokojů, "
    "a v každém seděl jiný Kolač s jinou záhadou.",
    "V noci jsem přemýšlel, zda svíčky v salónu "
    "nevedou tajný život, když se nikdo nedívá.",
    "Zdálo se mi o zahradě, kde místo květin "
    "rostly hodiny a každá ukazovala jiný čas.",
    "Měl jsem podivný sen — knihy v knihovně si "
    "navzájem vyprávěly příběhy, když jsem odešel.",
    "V noci se mi zdálo, že Kolač vyřešil případ "
    "ještě předtím, než se stal. Časový paradox.",
    "Přemýšlel jsem, jestli dům sní o nás, "
    "stejně jako my sníme o něm.",
    "Zdálo se mi, že počasí bylo uvnitř domu "
    "a venku byl salón. Znepokojivé.",
    "Měl jsem sen, ve kterém jsem servíroval čaj "
    "hosům, kteří ještě nedorazili. Byli vděční.",
]


def _g5d_neoveritelne(entity: str, config: dict) -> bool:
    """HANS_G5D_SKIP_UNVERIFIABLE_V1 (30. 9.) — entita, kterou Wikipedie ověřit
    NEMŮŽE: sám Hans, Koláč, člen domácnosti, datum/čas, nebo překlep modelu
    v „PRÁZDNÉ“. Dřív šla do porovnání: „Hans namaloval…“ proti článku HANS
    (ochrana krku závodníků) → ROZPOR → falešná „oprava faktu“ do deníku a do
    večerní reflexe. Změřeno 30. 9.: 11 ze 40 zapsaných oprav bylo tohoto druhu
    (7× Hans/Koláč, 4× datum), žádná skutečná oprava by se tím neztratila."""
    import re as _re
    e = (entity or "").strip().strip("\"'„“”").strip()
    if not e:
        return True
    el = e.lower()
    if el.startswith("prázdn") or el.startswith("prázn"):
        return True
    jmena = set()
    try:
        from scripts.hans_persona import persona_name
        jmena.add(persona_name(config).lower())
    except Exception:
        jmena.add("hans")
    try:
        from scripts.hans_kolac import kolac_name
        jmena.add(kolac_name(config).lower())
    except Exception:
        pass
    for k, kp in ((config.get("known_persons") or {}).items()):
        jmena.add(str(k).lower())
        if isinstance(kp, dict):
            for x in ("nom", "full"):
                if kp.get(x):
                    jmena.add(str(kp[x]).lower())
    if el in jmena:
        return True
    if _re.fullmatch(r"[\d\s.:/-]+", el):
        return True
    if _re.search(r"\b\d{1,2}\.\s*[^\W\d_]+\s+\d{4}\b", el):
        return True
    if el in ("čas", "datum", "dnes", "dnešek", "den"):
        return True
    return False


class _StudyBusy(Exception):
    """HANS_STUDY_SINGLE_FLIGHT_V1 — studium právě běží z druhé cesty;
    tenhle tick tiše přeskoč (NENÍ to chyba, guard se nesmí nastavit)."""


from scripts.hans_routine_noc import NocMixin   # ROZDELENI_METOD_V1 — část třídy HansRoutine
from scripts.hans_routine_pc import PcMixin   # ROZDELENI_METOD_V1 — část třídy HansRoutine
from scripts.hans_routine_zdravi import ZdraviMixin   # ROZDELENI_METOD_V1 — část třídy HansRoutine
from scripts.hans_routine_spanek import SpanekMixin   # ROZDELENI_METOD_V1 — část třídy HansRoutine
class HansRoutine(NocMixin, PcMixin, ZdraviMixin, SpanekMixin):
    """Denní rytmus — řídí fáze dne a noční mód."""

    def __init__(self, config: dict, diary_db_path: str,
                 synthesis=None, knowledge=None):
        self.config = config
        self._diary_path = diary_db_path
        self._knowledge = knowledge  # NARRATIVE_RAG_UPLOAD_V1 (RAG upload kapitol)
        self._stop = threading.Event()
        self._lock = threading.Lock()

        cfg = config.get("hans_routine", {})
        self._enabled = bool(cfg.get("enabled", True))

        # Konfigurovatelné časy fází (hodiny)
        self._morning_hour   = int(cfg.get("morning_hour",   6))
        self._afternoon_hour = int(cfg.get("afternoon_hour", 12))
        self._evening_hour   = int(cfg.get("evening_hour",   17))
        self._night_hour     = int(cfg.get("night_hour",     22))

        # Night mode — co Hans dělá v noci
        self._night_reduce_activity = bool(cfg.get("night_reduce_activity", True))
        self._night_dream_enabled   = bool(cfg.get("dreams_enabled", True))
        self._night_summary_enabled = bool(cfg.get("night_summary", True))

        # State
        self._current_phase = self._calc_phase()
        self._last_phase = self._current_phase
        self._last_dream_date = ""
        self._last_summary_date = ""
        self._callbacks: list[Callable] = []
        self._notifier = None  # SEVERKA_PROACTIVE_NOTIFY_V1 — proaktivní oznámení

        # Večerní reflexe — generuje se ručně přes run_evening_reflection()
        self._reflection = None
        if synthesis is not None:
            self._reflection = HansEveningReflection(
                config, diary_db_path, synthesis, knowledge)

        # Reflexe vztahových karet — 1× denně po setmění.
        # Defenzivně: pokud modul selže, routine běží dál bez ní.
        self._relationship_reflection = None
        self._last_rel_reflection_date = ""
        self._last_reflection_date = ""  # AUTO_EVENING_REFLECTION_V1
        self._last_severka_check = ""    # HANS_SEVERKA_V1 (3c, týdenní guard)
        self._last_direction_check = ""  # HANS_DIRECTION_V1 (týdenní guard)
        self._night_throttle = {}        # HANS_NIGHT_RETRY_THROTTLE_V1
        self._last_narrative = ""        # AUTOBIOGRAPHICAL_NARRATIVE_V1 (krok 3, týdenní guard)
        self._last_creation_reflection = ""  # HANS_CREATION_REFLECTION_V1 (D, týdenní guard)
        self._last_study_date = ""       # HANS_STUDY_V1 (1 studijní session/noc)
        # HANS_STUDY_SINGLE_FLIGHT_V1 (19.8.) — ke studiu vedou DVĚ nezávislé
        # cesty (noční okno + brain_up catchup) a 19.8. ve 03:04 a 03:05 se
        # spustily do sebe: dvě sessions na TÉMŽE pod-tématu, dva zápisky
        # v deníku, dvakrát zaplacený VRAM handoff. Denní guard je nechytil,
        # protože OBĚ odstartovaly dřív, než ho první stihla nastavit — závod,
        # ne chyba v jeho logice. Zámek se bere NEBLOKUJÍCÍ: druhý běh tenhle
        # tik přeskočí (blokující by duplicitu vyrobil taky, jen o minutu
        # později). Uvolňuje se ve `finally`, aby pád session zámek nedržel —
        # kdyby ho držel, umlčel by jednu z cest natrvalo, a catchup existuje
        # PRÁVĚ PROTO, že noční okno je nespolehlivé ([[study-brain-up-catchup]]).
        self._study_lock = __import__("threading").Lock()
        self._last_writing_date = ""     # HANS_AUTHORSHIP_V1 (1 autorská session/noc)
        self._last_synthesis_date = ""   # HANS_SYNTHESIS_IDEAS_V1 (vlastní nápady, kadence)
        self._last_selfcritique_date = ""  # HANS_SELFCRITIQUE_V1 (sebekritika, kadence)
        self._last_immune_date = ""      # HANS_IMMUNE_A2_V1 (noční fact-check tvrzení)
        self._last_hygiene_date = ""     # HANS_MEMORY_HYGIENE_V1 (prořez firehose 1×/noc)
        self._last_facts_date = ""       # HANS_FACTS_NIGHTLY_V1 (Wikidata 1×/noc)
        self._last_pc_shutdown_date = ""  # HANS_PC_NIGHT_SHUTDOWN (vypni PC po analytice 1×/noc)
        # HANS_HEALTH_NIGHT_AWARE_V1 — timestampy cyklu PC pro rozlišení
        # ZÁMĚRNÉHO výpadku (noční shutdown / ranní boot) od skutečné poruchy.
        self._pc_shutdown_ts = 0.0
        self._wol_online_ts = 0.0
        self._last_analytics_wake_date = ""  # HANS_PC_NIGHT_ANALYTICS_WAKE (probuď PC pro analytiku 1×/noc)
        self._analytics_wake_ts = 0.0
        self._identity = None            # HANS_IDENTITY_V1 (verzování CORE)
        self._severka = None             # HANS_SEVERKA_V1 (decision engine)
        # ROUTINE_STATE_PERSIST_V1 - guardy reflexi prezijou restart
        self._state_path = os.path.join(
            os.path.dirname(self._diary_path), "routine_state.json")
        self._load_routine_state()
        if synthesis is not None:
            try:
                from scripts.hans_relationships import (
                    RelationshipReflection)  # RELATIONSHIPS_MERGED_V1
                self._relationship_reflection = RelationshipReflection(
                    config, diary_db_path, synthesis, knowledge=knowledge)
            except Exception as _e:
                _log.warning("RelationshipReflection init failed: %s", _e)

        # HANS_IDENTITY_V1 + HANS_SEVERKA_V1 (Fáze 3c) — verzování identity
        # a Severka (tendence vs role → návrh CORE). Defenzivně.
        try:
            from scripts.hans_identity import IdentityStore
            from scripts.hans_severka import Severka
            self._identity = IdentityStore(config, diary_db_path)
            self._identity.ensure_seed()  # v1 = stávající CORE
            self._severka = Severka(config, diary_db_path,
                                    identity_store=self._identity)
        except Exception as _e:
            _log.warning("Severka/IdentityStore init failed: %s", _e)

        # Reference na ostatní moduly (nastaví se zvenku)
        self._weather = None
        self._mood = None
        self._curiosity = None
        self._tts = None

        # SLEEP_MODE_V1 — spánek 02:00–09:00 (TTS off + servo nahoru)
        self._servo = None
        self._vision = None  # SLEEP_VISION_OFF_V1 — display controller (kamera+recognition)
        self._sleeping = False
        # SLEEP_CFG_TOPLEVEL_FALLBACK_V1 — sleep hodiny i z top-level configu
        self._sleep_start_hour = int(cfg.get('sleep_start_hour',
                                             config.get('sleep_start_hour', 2)))
        self._sleep_end_hour   = int(cfg.get('sleep_end_hour',
                                             config.get('sleep_end_hour', 9)))
        # SLEEP_MANUAL_OVERRIDE_V1
        self._manual_override = None   # None=auto, True=force sleep, False=force wake
        self._prev_in_window  = None   # edge detection pro auto-expiraci override
        # WOL_WAKE_PC_V1
        # WOL_CFG_TOPLEVEL_FALLBACK_V1 — mirror SLEEP_CFG_TOPLEVEL_FALLBACK_V1:
        # wol_* klice lezi top-level, ne v sekci hans_routine -> cti i odtud
        self._wol_pc_enabled  = bool(cfg.get('wol_pc_enabled',
                                             config.get('wol_pc_enabled', False)))
        self._wol_pc_mac      = str(cfg.get('wol_pc_mac',
                                            config.get('wol_pc_mac', '')))
        self._wol_pc_ip       = str(cfg.get('wol_pc_ip',
                                           config.get('wol_pc_ip', '')))
        self._wol_min_before  = int(cfg.get('wol_minutes_before_wakeup',
                                            config.get('wol_minutes_before_wakeup', 5)))
        self._wol_last_date   = None
        self._wol_presence_last = 0.0   # WOL_ON_PRESENCE_V1
        # WOL_PRESENCE_TOGGLE_V1 — samostatný vypínač presence WOL
        self._wol_on_presence = bool(cfg.get('wol_on_presence',
                                            config.get('wol_on_presence', True)))
        # WOL_NO_WAKE_ON_RESTART_V1 — restart Hanse NESMÍ probudit PC.
        # (1) Naběhli-li jsme UŽ uvnitř ranního WOL okna, označ dnešek za
        #     vyřízený → scheduled ranní wake se řídí PŘECHODEM do okna za běhu
        #     (Hans běží přes noc), ne stavem při startu. (2) Krátká startovací
        #     lhůta pro presence WOL, ať přítomnost hned po restartu nezapíná PC.
        #     Normální ranní wake, denní presence i manuální /wol fungují dál.
        import time as _wt
        self._wol_boot_ts = _wt.time()
        self._wol_startup_grace_s = int(cfg.get('wol_startup_grace_s',
                                               config.get('wol_startup_grace_s', 300)))
        try:
            from datetime import datetime as _dt0, timedelta as _td0
            _n = _dt0.now()
            _wake0 = _n.replace(hour=self._sleep_end_hour, minute=0,
                                second=0, microsecond=0)
            _start0 = _wake0 - _td0(minutes=self._wol_min_before)
            if _n.hour < self._sleep_end_hour and _n >= _start0:
                self._wol_last_date = _n.date()
                _log.info("WOL: start uvnitř ranního okna → dnešní auto-wake "
                          "potlačen (restart nezapíná PC)")
        except Exception as _e:
            _log.warning("WOL restart-guard init: %s", _e)
        # WOL_TIMER_THREAD_V1 — nezavisle na tick-loopu
        if self._wol_pc_enabled:
            import threading as _thr
            _thr.Thread(target=self._wol_timer_loop, daemon=True).start()
        # SLEEP_WATCHER_THREAD_V1 — kontrola spánku NEZÁVISLE na tick-loopu.
        # Noční LLM analytika v tick() (evening reflection/stance/importance/study)
        # může pomalou Ollamou blokovat tick na MINUTY → sleep by se jinak nespustil
        # včas (viděno 29.6.: Ollama visela 10 min, Hans v 23:00 neusnul). Watcher
        # volá jen rychlý _check_sleep_window (žádné blokující LLM — _maybe_distill
        # spouští vlastní vlákno).
        self._sleep_lock = threading.Lock()
        self._sleep_check_interval = float(cfg.get('sleep_check_interval_s',
                                                   config.get('sleep_check_interval_s', 30)))
        threading.Thread(target=self._sleep_watcher_loop, daemon=True).start()
        # NIGHT_WORKER_THREAD_V1 — noční LLM analytika (evening reflection,
        # study, synteze, sebekritika, imunita, narativ…) běží ve VLASTNÍM
        # vlákně, NE inline v tick(). Zaseklá Ollama tak už neblokuje tick()
        # ani volajícího (hans_idle: proaktivita/film/autoplay/pozornost).
        # Non-blocking guard: visí-li předchozí běh, další cyklus se přeskočí.
        self._night_lock = threading.Lock()
        self._state_lock = threading.Lock()   # ochrana zápisu routine_state
        self._catchup_lock = threading.Lock()  # HANS_REFLECTION_CATCHUP_LOCK_V1
        self._night_check_interval = float(cfg.get('night_check_interval_s',
                                                   config.get('night_check_interval_s', 60)))
        threading.Thread(target=self._night_worker_loop, daemon=True).start()
        # HANS_HEALTH_V1 — živý watchdog závislostí (Ollama/ComfyUI/Kodi/STT/PC/
        # disk) ve VLASTNÍM vlákně. Reálná probe (Ollama i inference → odhalí
        # wedge) + self-heal zaseklé Ollamy (2× po sobě → restart na PC).
        _hcfg = config.get('health', {}) or {}
        self._health_enabled = bool(_hcfg.get('enabled', True))
        self._health_interval = float(_hcfg.get('check_interval_s', 600))
        self._health_wedge_strikes = 0   # po sobě jdoucí WEDGED před self-heal
        self._health_last = {}           # poslední výsledek (pro surfacing)
        if self._health_enabled:
            threading.Thread(target=self._health_watcher_loop, daemon=True).start()
        # HANS_ROUTER_V1 (23. 9.) — hlídač VPN: když nejde internet přes
        # tunel a linka mimo něj ano, přepne server a ohlásí to. Vlastní
        # vlákno, protože health běží á 10 min — na výpadek internetu pozdě.
        self._router_st = {}
        if bool(((self.config.get('router') or {}).get('enabled'))):
            threading.Thread(target=self._router_watcher_loop, daemon=True).start()
        # HANS_DISTILLATION_V1 — fáze 2a noční destilace záseku
        self._distillation = None
        self._distillation_running = False   # idempotence — jednou denně
        self._saved_tilt = None        # poloha před spánkem (pro návrat)
        self._saved_tracking = None    # tracking stav před spánkem
        # HANS_MORNING_HEALTH_V1 — kdy usnul (okno pro ranní scan logů).
        # ⚠️ HANS_SLEEP_TS_PERSIST_V1: `_load_routine_state()` běží UŽ na ř. 172,
        # takže tvrdé `= None` by načtenou hodnotu přepsalo a restart v noci by
        # okno zase zkrátil. Proto jen default, když nic načteno nebylo.
        self._sleep_started_ts = getattr(self, "_sleep_started_ts", None)
        # F=3: drátování _tts oživilo by komentáře při přechodu fází —
        # vědomě je potlačujeme, dokud se nedohodneme jinak.
        self._phase_comments_enabled = bool(cfg.get('phase_comments', False))

        _log.info("HansRoutine ready — phase=%s, night_hour=%d, morning_hour=%d",
                  self._current_phase, self._night_hour, self._morning_hour)

    # ── Public API ───────────────────────────────────────────────────────────

    @property
    def current_phase(self) -> str:
        return self._current_phase

    @property
    def phase_label(self) -> str:
        return _PHASE_LABELS_CZ.get(self._current_phase, "")

    @property
    def is_night(self) -> bool:
        return self._current_phase == PHASE_NIGHT

    @property
    def is_morning(self) -> bool:
        return self._current_phase == PHASE_MORNING

    def on_phase_change(self, callback: Callable):
        """Registruj callback(old_phase, new_phase)."""
        self._callbacks.append(callback)

    def set_notifier(self, callback):
        """SEVERKA_PROACTIVE_NOTIFY_V1 — callback(text) pro proaktivní oznámení
        uživateli (Telegram). Volá se např. při Severčině návrhu identity."""
        self._notifier = callback

    def get_context_string(self) -> str:
        """Pro LLM prompt — co je za denní dobu."""
        now = datetime.now()
        from scripts.hans_persona import persona_name as _pn  # PERSONA_NAME_CONFIGURABLE_V1
        return (f"Je {self.phase_label} ({now.strftime('%H:%M')}). "
                f"{_pn(self.config)} je ve fázi '{self._current_phase}'.")

    def should_reduce_activity(self) -> bool:
        """Vrátí True pokud je noc a má se snížit aktivita."""
        return self.is_night and self._night_reduce_activity

    # ── G5D_VERIFY_BEFORE_DIARY_V1 ─────────────────────────────────────────
    def _g5d_verify_day(self, date_str):
        """Ověří dnešní human_chat faktická tvrzení proti Wikipedii.
        Opravy zapíše jako nový záznam 'fact_correction'. Defenzivní:
        cokoliv selže → zaloguj, NEzhroutí reflexi.
        Vrací počet zapsaných oprav.
        """
        written = 0
        try:
            import sqlite3 as _sql
            from scripts.chat_commands import _g5c_llm
            from scripts.web_reader import WebReader
        except Exception as e:
            _log.warning("G5D: import selhal: %s", e)
            return 0
        # Lehký shim — _g5c_llm bere 'handler' jen kvůli config/model.
        # Vyrobím minimální objekt s .config (a model_name nemáme → fallback).
        class _Shim:
            pass
        shim = _Shim()
        shim.config = self.config
        try:
            wr = WebReader(self.config)
        except Exception as e:
            _log.warning("G5D: WebReader init selhal: %s", e)
            return 0
        # Načti dnešní human_chat z deníku
        try:
            db = _sql.connect(self._diary_path)
            rows = db.execute(
                "SELECT title, note FROM diary WHERE event_type='human_chat' "
                "AND date(ts,'unixepoch','localtime')=? "
                "ORDER BY ts DESC LIMIT 6",
                (date_str,)).fetchall()
            db.close()
        except Exception as e:
            _log.warning("G5D: čtení deníku selhalo: %s", e)
            return 0
        if not rows:
            _log.info("G5D: žádné human_chat pro %s, nic k ověření", date_str)
            return 0
        _log.info("G5D: ověřuji %d human_chat záznamů pro %s", len(rows), date_str)
        for title, note in rows:
            if not note:
                continue
            # Vytáhni JEN Hansovu část (řádky za 'Hans:')
            hans_lines = []
            for ln in str(note).splitlines():
                s = ln.strip()
                if s.lower().startswith("hans:"):
                    hans_lines.append(s[5:].strip())
            hans_text = (' '.join(hans_lines)).strip()
            if not hans_text or len(hans_text) < 20:
                continue
            # Extrakce tvrzení
            extract_sys = (
                "Jsi extraktor faktických tvrzení. Z textu vypiš ověřitelná "
                "faktická tvrzení o světě. Ke každému urči ENTITU = PŘEDMĚT "
                "tvrzení, který má vlastní heslo na Wikipedii (dílo, událost, "
                "místo, pojem) — NE vedlejší osobu. Např. tvrzení 'R.U.R. "
                "napsal Čapek' → ENTITA je 'R.U.R.' (dílo), NE 'Čapek'. "
                "Tvrzení 'Wells napsal Válku světů' → ENTITA 'Válka světů'. "
                "Ignoruj dojmy, zdvořilosti, názory. Každé na samostatný řádek "
                "ve tvaru 'ENTITA | tvrzení'. Max 2. Pokud žádné, napiš PRÁZDNÉ."
            )
            extract = _g5c_llm(shim, extract_sys, "Text: " + hans_text, num_predict=180)
            if not extract or "PRÁZDNÉ" in extract.upper():
                continue
            for line in [l.strip() for l in extract.splitlines() if l.strip()][:2]:
                entity = line.split("|", 1)[0].strip() if "|" in line else line
                claim = line.split("|", 1)[1].strip() if "|" in line else line
                if not entity:
                    continue
                if _g5d_neoveritelne(entity, self.config):  # HANS_G5D_SKIP_UNVERIFIABLE_V1
                    _log.info("G5D: [%s] přeskakuji (sám o sobě / osoba z domácnosti / datum — Wikipedie to neověří)", entity)
                    continue
                # G5F_VERIFY_FULLTEXT_V1 — najdi správný článek dle PŘEDMĚTU,
                # stáhni PLNÝ text (ne REST summary). Fallback na summary.
                wiki = ""
                try:
                    _title = wr._wikipedia_search(entity)
                    if _title:
                        _url = ('https://cs.wikipedia.org/wiki/'
                                + _up.quote(_title.replace(' ', '_')))
                        _full = wr.fetch_url(_url, topic='verify')
                        if _full and getattr(_full, 'raw_text', ''):
                            wiki = _full.raw_text[:2500]
                            _log.info('G5D: [%s] plný článek %r (%d zn.)',
                                      entity, _title, len(_full.raw_text))
                    # Fallback: REST summary, když plný článek nevyšel
                    if not wiki:
                        _rr = wr.wikipedia(entity)
                        if _rr and getattr(_rr, 'raw_text', ''):
                            wiki = _rr.raw_text[:1200]
                            _log.info('G5D: [%s] fallback summary', entity)
                except Exception as e:
                    _log.warning("G5D: zdroj pro %r selhal: %s", entity, e)
                if not wiki:
                    _log.info('G5D: [%s] Wikipedie nenašla, přeskakuji', entity)
                    continue
                cmp_sys = (
                    "Jsi ověřovatel faktů. Porovnej TVRZENÍ s textem z Wikipedie. "
                    "Odpověz PŘESNĚ jedním slovem na začátku: SHODA nebo ROZPOR "
                    "nebo NEOVĚŘITELNÉ, pak za pomlčkou stručně proč a správný "
                    "údaj (max 1 věta). Buď přísný na fakta."
                )
                cmp_user = "TVRZENI: " + claim + NL_RUNTIME + "WIKIPEDIE: " + wiki
                verdict = _g5c_llm(shim, cmp_sys, cmp_user, num_predict=120)
                _log.info("G5D: [%s] %s", entity, verdict[:160])
                # G5H_VERDICT_PARSE_V1 — robustní klasifikace verdiktu podle
                # PRVNÍHO SLOVA (bez diakritiky, upper), ne startswith() syrového
                # textu. Bezpečné oběma směry: ROZPOR nepropadne, SHODA/NEOVĚŘ.
                # se omylem nevyhodnotí jako rozpor.
                _v_raw = (verdict or "").strip()
                _v_first = _v_raw.split()[0] if _v_raw.split() else ""
                _v_norm = _v_first.upper()
                for _a, _b in (('Á','A'),('Č','C'),('Ď','D'),('É','E'),('Ě','E'),('Í','I'),('Ň','N'),('Ó','O'),('Ř','R'),('Š','S'),('Ť','T'),('Ú','U'),('Ů','U'),('Ý','Y'),('Ž','Z')):
                    _v_norm = _v_norm.replace(_a, _b)
                _v_norm = _v_norm.rstrip(":.,-–—")
                _is_rozpor = (_v_norm == "ROZPOR")
                # Zápis opravy JEN při ROZPORU
                # G5E_VERIFY_LOG_ONLY_V1 — NEzapisuje (zápis byl předčasný,
                # verify konfabulovalo: entita≠téma, R.U.R. na stránce Čapka).
                # Jen diagnostika — co BY zapsalo. Sbíráme data o kvalitě.
                if _is_rozpor:
                    # G5K_VERIFY_WRITE_SPARSE_V1 — opatrný ostrý zápis.
                    # _G5K_DRYRUN=True → jen loguje (default). False → zapisuje.
                    _G5K_DRYRUN = False
                    _note = ("Ověřoval jsem '" + claim[:120] + "' proti "
                             "Wikipedii — nepotvrdilo se (sporné).")
                    if _G5K_DRYRUN:
                        _log.info("G5D: BY zapsal opravu [%s] | tvrzení: %s | verdikt: %s",
                                  entity, claim[:80], verdict.strip()[:160])
                    else:
                        # idempotence: už dnes pro tohle tvrzení zapsáno?
                        _dup = False
                        try:
                            _db = _sql.connect(self._diary_path)
                            _row = _db.execute(
                                "SELECT COUNT(*) FROM diary WHERE event_type=? "
                                "AND note=? AND date(ts,'unixepoch','localtime')=?",
                                ("fact_correction", _note, date_str)).fetchone()
                            _dup = bool(_row and _row[0])
                            _db.close()
                        except Exception as _de:
                            _log.warning("G5K: kontrola duplicity selhala: %s", _de)
                        if _dup:
                            _log.info("G5K: [%s] oprava už dnes zapsána, přeskakuji", entity)
                        else:
                            self._diary_write("fact_correction",
                                              "Ověření faktu: " + entity[:60], _note)
                            written += 1
                            _log.info("G5K: [%s] ZAPSÁNA oprava (sporné) | %s",
                                      entity, claim[:80])
                else:
                    _log.info("G5D: [%s] bez zápisu (verdikt nezačíná ROZPOR)", entity)
        _log.info("G5D: hotovo, %d oprav zapsáno pro %s", written, date_str)
        return written

    def run_evening_reflection(self, target_date=None):
        """Ručně spustí Hansovu večerní reflexi dne.

        Args:
            target_date: 'YYYY-MM-DD' nebo None (= dnes)

        Returns:
            Text reflexe nebo None.
        """
        if self._reflection is None:
            _log.warning("Reflexe není inicializovaná "
                         "(synthesis nebyla předána do HansRoutine)")
            return None
        # G5D_VERIFY_BEFORE_DIARY_V1 — nejdřív ověř fakta, zapiš opravy,
        # pak teprve souhrn (run čte z deníku → opravy nabere)
        try:
            _date = target_date or datetime.now().strftime('%Y-%m-%d')
            self._g5d_verify_day(_date)
        except Exception as _ve:
            _log.warning('G5D: verifikace selhala (reflexe pokračuje): %s', _ve)
        result = self._reflection.run(target_date)
        if result:
            # Označit pro dnešek splněno — aby auto-trigger v 22:00
            # neudělal duplikát stejné reflexe.
            today = datetime.now().strftime("%Y-%m-%d")
            self._last_reflection_date = today
            self._save_routine_state()  # ROUTINE_STATE_PERSIST_V1
            # HANS_TENDENCIES_V2 — tendency snapshot přesunut do
            # HansEveningReflection.run() (pokrývá i noční automatiku).
        return result

    def _load_routine_state(self):
        # ROUTINE_STATE_PERSIST_V1 - nacti guardy reflexi z disku.
        # Chybi/poskozeny soubor -> nechame defaulty, reflexe se firne.
        try:
            with open(self._state_path, "r", encoding="utf-8") as f:
                s = json.load(f)
            self._last_reflection_date = s.get("last_reflection_date", "")
            # HANS_NIGHT_RESTART_ONCE_V1 — shrnuti dne se po restartu neopakuje
            self._last_summary_date = s.get("last_summary_date", "")
            self._last_rel_reflection_date = s.get(
                "last_rel_reflection_date", "")
            self._last_severka_check = s.get("last_severka_check", "")
            self._last_direction_check = s.get("last_direction_check", "")
            self._last_narrative = s.get("last_narrative", "")
            self._last_creation_reflection = s.get("last_creation_reflection", "")
            self._last_study_date = s.get("last_study_date", "")  # HANS_STUDY_V1
            self._last_writing_date = s.get("last_writing_date", "")  # HANS_AUTHORSHIP_V1
            self._last_synthesis_date = s.get("last_synthesis_date", "")  # HANS_SYNTHESIS_IDEAS_V1
            self._last_selfcritique_date = s.get("last_selfcritique_date", "")  # HANS_SELFCRITIQUE_V1
            self._last_immune_date = s.get("last_immune_date", "")  # HANS_IMMUNE_A2_V1
            self._last_hygiene_date = s.get("last_hygiene_date", "")  # HANS_MEMORY_HYGIENE_V1
            self._last_facts_date = s.get("last_facts_date", "")      # HANS_FACTS_NIGHTLY_V1
            self._last_pc_shutdown_date = s.get("last_pc_shutdown_date", "")  # HANS_PC_NIGHT_SHUTDOWN
            self._last_analytics_wake_date = s.get("last_analytics_wake_date", "")  # HANS_PC_NIGHT_ANALYTICS_WAKE
            # HANS_NIGHT_DAY_CATCHUP_V1
            self._night_brain_date = s.get("night_brain_date", "")
            self._night_catchup_date = s.get("night_catchup_date", "")
            self._night_catchup_until = float(s.get("night_catchup_until") or 0.0)
            self._last_dream_date = s.get("last_dream_date", "")  # HANS_DREAM_DEFER_V1 — restart nepřidá sen
            # HANS_SLEEP_TS_PERSIST_V1 — restart v noci nesmí zkrátit ranní scan
            self._sleep_started_ts = s.get("sleep_started_ts") or None
        except FileNotFoundError:
            pass
        except Exception as _e:
            _log.warning("routine_state: nacteni selhalo: %s", _e)

    def _save_routine_state(self):
        # ROUTINE_STATE_PERSIST_V1 - zapis guardy reflexi (prezije restart).
        # NIGHT_WORKER_THREAD_V1 — zámek proti souběhu (night worker × sleep
        # watcher × tick zapisují tentýž JSON → jinak riziko poškození).
        _sl = getattr(self, '_state_lock', None)
        if _sl is not None:
            _sl.acquire()
        try:
            with open(self._state_path, "w", encoding="utf-8") as f:
                json.dump({
                    "last_reflection_date": self._last_reflection_date,
                    "last_summary_date": getattr(self, "_last_summary_date", ""),  # HANS_NIGHT_RESTART_ONCE_V1
                    "last_rel_reflection_date":
                        self._last_rel_reflection_date,
                    "last_severka_check": self._last_severka_check,
                    "last_direction_check": self._last_direction_check,
                    "last_narrative": self._last_narrative,
                    "last_creation_reflection": self._last_creation_reflection,
                    "last_study_date": self._last_study_date,  # HANS_STUDY_V1
                    "last_writing_date": self._last_writing_date,  # HANS_AUTHORSHIP_V1
                    "last_synthesis_date": self._last_synthesis_date,  # HANS_SYNTHESIS_IDEAS_V1
                    "last_selfcritique_date": self._last_selfcritique_date,  # HANS_SELFCRITIQUE_V1
                    "last_immune_date": self._last_immune_date,  # HANS_IMMUNE_A2_V1
                    "last_hygiene_date": self._last_hygiene_date,  # HANS_MEMORY_HYGIENE_V1
                    "last_facts_date": self._last_facts_date,      # HANS_FACTS_NIGHTLY_V1
                    "last_pc_shutdown_date": self._last_pc_shutdown_date,  # HANS_PC_NIGHT_SHUTDOWN
                    "last_analytics_wake_date": self._last_analytics_wake_date,  # HANS_PC_NIGHT_ANALYTICS_WAKE
                    # HANS_NIGHT_DAY_CATCHUP_V1
                    "night_brain_date": getattr(self, "_night_brain_date", ""),
                    "night_catchup_date": getattr(self, "_night_catchup_date", ""),
                    "night_catchup_until": getattr(self, "_night_catchup_until", 0.0),
                    "last_dream_date": self._last_dream_date,  # HANS_DREAM_DEFER_V1
                    # HANS_SLEEP_TS_PERSIST_V1 — okno noci musí přežít restart
                    "sleep_started_ts": self._sleep_started_ts,
                }, f)
        except Exception as _e:
            _log.warning("routine_state: zapis selhal: %s", _e)
        finally:
            if _sl is not None:
                try:
                    _sl.release()
                except Exception:
                    pass

    # ── HANS_SEVERKA_V1 (3c) — týdenní sebereflexe identity ──────────────────
    def _severka_due(self, today: str) -> bool:
        """True když od poslední sebereflexe uplynul `severka.cadence_days`.

        SEVERKA_CADENCE_V1 (17. 9.) — výchozí 30 dní místo týdne (pokyn
        uživatele: „7 dní je asi moc málo“). Zrcadlí
        HANS_DIRECTION_CADENCE_V1, který totéž udělal pro směr.
        Důvod je měřený, ne dojmový: podklad se mezi týdny prakticky nemění
        (koníčky 96 dní beze změny, 39 ze 41 postojů už není živých), takže
        týdenní běh rozhodoval nad týmiž daty a rozdíl mezi návrhy dělal
        hlavně rozptyl modelu. Za 14 dní vznikly 3 návrhy, z toho 1 přijatý.
        ⚠️ Data-gate v `hans_severka` NENÍ brzda: projde 12 z 12 koníčků,
        takže podmínka „aspoň něco trvalého“ je splněná vždy."""
        last = self._last_severka_check
        if not last:
            return True
        try:
            d0 = datetime.strptime(last, "%Y-%m-%d").date()
            d1 = datetime.strptime(today, "%Y-%m-%d").date()
            _dni = int(((self.config.get("severka") or {}).get("cadence_days", 30)))
            return (d1 - d0).days >= _dni
        except Exception:
            return True

    def _severka_pending_ceka(self) -> bool:
        """SEVERKA_PENDING_GUARD_V1 (17. 9.) — True (= přeskoč běh), když už
        čeká nevyřízený návrh identity.

        `IdentityStore.propose` žádnou pojistku nemá a vkládá bezpodmínečně,
        takže bez tohohle by se pendingy hromadily. A protože `pending()` řadí
        `ts DESC` a `/severka schválit` bez čísla bere NEJNOVĚJŠÍ, dal by se
        odklepnout návrh, který uživatel nikdy nečetl.
        Guard `_last_severka_check` se přeskočením ZÁMĚRNĚ nenastavuje — po
        rozhodnutí uživatele tak Severka naskočí hned příští noc.
        Volá se až ZA `_night_throttled`, aby dotaz do DB nešel každý tik."""
        try:
            if self._identity is None:
                return False
            pend = self._identity.pending()
            if not pend:
                return False
            _log.info("Severka: čeká nevyřízený návrh (pending id=%s) → "
                      "nový nevytvářím, rozhodne uživatel (/severka stav).",
                      ", ".join(str(p.id) for p in pend))
            return True
        except Exception as _e:
            _log.warning("Severka pending guard selhal (%s) → běh nebrzdím", _e)
            return False

    def _night_throttled(self, key: str, min_s: float) -> bool:
        """HANS_NIGHT_RETRY_THROTTLE_V1 — True (přeskoč) když se `key` pokoušel
        naposledy před méně než min_s. Jinak zaznamená pokus a vrátí False.
        Chrání deferral-safe noční úlohy (narrative, toolscout) před retry á
        tick (~60s) celou noc, když LLM defere/thrashuje (doloženo 3.8.:
        toolscout 311× + narrative 149× za noc)."""
        import time as _t
        now = _t.time()
        last = self._night_throttle.get(key, 0)
        if now - last < min_s:
            return True
        self._night_throttle[key] = now
        return False

    def _direction_due(self, today: str) -> bool:
        """True když od poslední úvahy o směru uplynul interval `direction.cadence_days`.

        HANS_DIRECTION_CADENCE_V1 (14. 9.) — výchozí 14 dní místo týdne
        (pokyn uživatele: „to týdenní je moc časté"). Směr je dlouhodobá
        aspirace; týdenní návrhy ho přepisovaly dřív, než se stačil projevit."""
        last = self._last_direction_check
        if not last:
            return True
        try:
            d0 = datetime.strptime(last, "%Y-%m-%d").date()
            d1 = datetime.strptime(today, "%Y-%m-%d").date()
            _dni = int(((self.config.get("direction") or {}).get("cadence_days", 14)))
            return (d1 - d0).days >= _dni
        except Exception:
            return True

    def _run_direction_check(self, today: str) -> bool:
        """HANS_DIRECTION_V1 — týdenní úvaha o vlastním SMĚRU. Reasoning tier
        (qwen3, num_gpu:0 = CPU → žádný VRAM handoff). Při návrhu vznikne
        pending (ask-first) + Hans dá vědět. Vrací True když ODLOŽENO (LLM dole)
        → volající nenastaví guard, zkusí příště (NIGHT_DEFERRAL_SAFE_V1)."""
        try:
            from scripts.hans_direction import HansDirection
            res = HansDirection(self.config, self._diary_path).evaluate()
        except Exception as _e:
            _log.warning("Direction check selhal: %s", _e)
            return True
        if res.get("deferred"):
            _log.info("Direction: odloženo (LLM dole) → zkusím příště.")
            return True
        if res.get("decision") in ("propose", "evolve") and res.get("message"):
            _log.info("Direction: NÁVRH směru (pending id=%s). Viz /smer.",
                      res.get("id"))
            if self._notifier:
                try:
                    self._notifier(res["message"])
                except Exception as _ne:
                    _log.warning("Direction notifier selhal: %s", _ne)
        else:
            _log.info("Direction: %s (drží se / gate).", res.get("decision"))
        return False

    def _run_severka_check(self, today: str) -> bool:
        """Severčino rozhodnutí. Při návrhu vznikne pending verze (nic se
        neaplikuje) — uživatel ji uvidí přes /severka stav a schválí/zamítne.
        NIGHT_DEFERRAL_SAFE_V1 — vrací True když ODLOŽENO (LLM dole) → volající
        NEnastaví týdenní guard a zkusí znovu příští noc."""
        res = self._severka.evaluate()
        if res.get("deferred"):
            _log.info("Severka: odloženo (Ollama dole) → zkusím znovu příští noc.")
            return True
        d = res.get("decision")
        if d == "propose":
            _log.info("Severka: NÁVRH změny identity, čeká na schválení "
                      "(pending id=%s). Viz /severka.", res.get("version_id"))
            # SEVERKA_PROACTIVE_NOTIFY_V1 — Hans dá sám vědět (Telegram), místo
            # aby návrh jen ležel v logu / čekal na /severka stav (pull).
            if self._notifier:
                try:
                    self._notifier(res.get("message") or
                                   "Pane, mám návrh, jak přehodnotit svou povahu "
                                   "— když budete chtít, řekněte /severka stav.")
                except Exception as _ne:
                    _log.warning("Severka notifier selhal: %s", _ne)
        # SEVERKA_LOG_HONEST_V1 (24. 9.) — brzda, zamítnutá role a synonymum
        # se dřív hlásily jako „drift malý“; log má říct skutečný důvod.
        elif res.get("cooldown"):
            _log.info("Severka: identita je mladší než min_days_since_change → nechávám ji uležet.")
        elif res.get("role_rejected"):
            _log.info("Severka: návrh zahozen pojistkou role (%s) → držím roli.",
                      res.get("role_rejected"))
        elif res.get("role_same"):
            _log.info("Severka: návrh je jen jiné pojmenování role (%s) → držím roli.",
                      res.get("role_same"))
        elif res.get("gate"):
            _log.info("Severka: gate prošel, drift malý → držím roli.")
        else:
            _log.info("Severka: žádná trvalá tendence (gate) → držím roli.")
        return False   # NIGHT_DEFERRAL_SAFE_V1 — proběhlo, guard se má nastavit

    # ── AUTOBIOGRAPHICAL_NARRATIVE_V1 (krok 3) — týdenní narativní kapitola ───
    def _narrative_due(self, today: str) -> bool:
        last = self._last_narrative
        if not last:
            return True
        try:
            d0 = datetime.strptime(last, "%Y-%m-%d").date()
            d1 = datetime.strptime(today, "%Y-%m-%d").date()
            return (d1 - d0).days >= 7
        except Exception:
            return True

    # ── Tick — volá se z hans_idle._tick ──────────────────────────────────────

    # SLEEP_MODE_V1 — periferie pro spánek (drátování zvenčí)
    def set_tts(self, tts_speaker):
        """Předá tts_speaker. Pokud je již aktivní spánek (race condition),
        dožene TTS off — SLEEP_SETTER_CATCHUP_V1."""
        self._tts = tts_speaker
        _log.info('SLEEP wire: TTS speaker připojen (enabled=%s)',
                  getattr(tts_speaker, 'enabled', '?'))
        if self._sleeping and tts_speaker is not None:
            try:
                tts_speaker.enabled = False
                _log.info('SLEEP catchup: TTS enabled=False (race condition oprava)')
            except Exception as _e:
                _log.warning('SLEEP catchup: TTS off failed: %s', _e)

    def set_servo(self, servo_controller):
        """Předá servo_controller. Pokud je již aktivní spánek (race condition),
        dožene zapamatování polohy + stop tracking + tilt nahoru — SLEEP_SETTER_CATCHUP_V1."""
        self._servo = servo_controller
        _log.info('SLEEP wire: servo_controller připojen')
        if self._sleeping and servo_controller is not None:
            # Dohnat kroky 1-3 z _apply_sleep_mode(True), které propadly při None
            try:
                if hasattr(servo_controller, 'get_current_position'):
                    pos = servo_controller.get_current_position()
                    self._saved_tilt = pos
                    _tt = getattr(servo_controller, 'tracking_thread', None)  # TRACKING_RESTORE_FIX_V1
                    self._saved_tracking = bool(_tt is not None and _tt.is_alive())
            except Exception as _e:
                _log.warning('SLEEP catchup: save pos failed: %s', _e)
            try:
                if hasattr(servo_controller, 'stop_tracking'):
                    servo_controller.stop_tracking()
            except Exception as _e:
                _log.warning('SLEEP catchup: stop_tracking failed: %s', _e)
            try:
                if hasattr(servo_controller, 'manual_tilt'):
                    tmax = getattr(servo_controller, 'tilt_max', 30)
                    servo_controller.manual_tilt(tmax)
                    _log.info('SLEEP catchup: servo tilt -> %d (race condition oprava)', tmax)
            except Exception as _e:
                _log.warning('SLEEP catchup: manual_tilt up failed: %s', _e)

    def set_vision(self, vision_controller):  # SLEEP_VISION_OFF_V1
        """Předá display controller (kamera+recognition). Catchup: pokud už
        spíme, hned pozastav vision."""
        self._vision = vision_controller
        _log.info('SLEEP wire: vision_controller připojen')
        if self._sleeping and vision_controller is not None:
            try:
                if hasattr(vision_controller, 'pause_vision'):
                    vision_controller.pause_vision()
                    _log.info('SLEEP catchup: vision pozastaven')
            except Exception as _e:
                _log.warning('SLEEP catchup: pause_vision failed: %s', _e)

    def study_catchup_async(self):
        """HANS_STUDY_BRAIN_UP_CATCHUP_V1 — studium běží jen v nočním okně
        (**22:00–06:00**, `_in_night_window`; HANS_STUDY_UNIFY_V1 — tady stálo
        „2-6", což neodpovídalo kódu) a potřebuje mozek, jenže PC se v tom okně
        často neprobudí
        (3:00 analytický wake je nespolehlivý) → session se odloží
        (deferred) a okno mezitím zavře, takže studium tiše stojí i dny.
        Na naběhnutí mozku (ranní WOL) proto dojeď 1 session, když dnes
        ještě neproběhla a je klid od chatu. Decoupluje studium od
        křehkého nočního wake. Neblokující (daemon thread)."""
        import threading
        threading.Thread(target=self._study_catchup, daemon=True).start()

    def _study_catchup(self):
        try:
            if not (self.config.get("study", {}) or {}).get("enabled", True):
                return
            today = datetime.now().strftime("%Y-%m-%d")
            if self._last_study_date == today:
                return  # dnes už proběhla (noční tick nebo dřívější catchup)
            if not self._chat_quiet_ok():
                return  # neruš aktivní chat těžkou base-LLM session
            from scripts.hans_study import run_study_session, is_transient
            # HANS_STUDY_SINGLE_FLIGHT_V1
            if not self._study_lock.acquire(blocking=False):
                _log.debug("studium (catchup): jiná session už běží → přeskakuji")
                return
            try:
                if self._last_study_date == today:
                    return   # vítěz závodu ji mezitím dokončil
                _scode = run_study_session(
                    self.config, self._diary_path, knowledge=self._knowledge)
            finally:
                self._study_lock.release()
            # HANS_STUDY_UNIFY_V1 — význam kódu rozhoduje hans_study, ne tenhle
            # řetězcový test (obě cesty ke studiu se dřív mohly rozejít).
            if not is_transient(_scode):
                self._last_study_date = today
                self._save_routine_state()
                _log.info("Studijní session (brain_up catchup): %s", _scode)
            else:
                _log.debug("Studijní session (brain_up catchup): deferred")
        except Exception as _e:
            _log.warning("Studijní catchup (brain_up) selhal: %s", _e)

    # ── HANS_DISTILLATION_V1 — fáze 2a wire-up + trigger ──────────────
    def set_distillation(self, distillation):
        """Late binding pro HansDistillation — volá hans_idle po init."""
        self._distillation = distillation
        _log.info('HansRoutine: distillation set')

    def _maybe_distill(self, ignore_window: bool = False):  # DISTILL_MORNING_CATCHUP_V1
        """Trigger pro noční destilaci. Spawn v threadu.
        Idempotence + noční okno řeší HansDistillation.run() interně.
        ignore_window=True = ranní doběhnutí (obejde okno, NE idempotenci)."""
        if self._distillation_running:
            return
        self._distillation_running = True
        import threading
        def _wrap():
            try:
                self._distillation.run(ignore_window=ignore_window)
            finally:
                self._distillation_running = False
        threading.Thread(target=_wrap, daemon=True).start()

    def _brain_up(self) -> bool:
        """HANS_REFLECTION_BRAIN_UP_CATCHUP_V1 — je mozek k dispozici?

        Bez tohohle gate mlátila večerní reflexe do vypnutého PC každý tick
        (7.8.: 38 pokusů mezi 23:07 a 23:59, každý s WARNINGem). Stejný vzorec
        jako `HANS_NIGHT_DRAIN_BRAIN_GATE_V1` — když mozek chybí, mlč a zkus
        to příště. Fail-open: když se stav nedá zjistit, pokus se o to.
        """
        try:
            from scripts.ollama_client import brain_available
            return bool(brain_available(self.config))
        except Exception:
            return True

    def _reflection_written(self, date_str: str) -> bool:
        """Existuje už reflexe za daný den? Ptáme se na TITULEK, ne na `ts` —
        `_write_to_diary` zapisuje čas ZÁPISU, takže dohnaná reflexe za včerejšek
        má dnešní `ts` a filtr přes `date(ts)` by ji nenašel."""
        import sqlite3 as _sql
        try:
            db = _sql.connect(self._diary_path, timeout=5.0)
            row = db.execute(
                "SELECT 1 FROM diary WHERE event_type='evening_reflection' "
                "AND title LIKE ? LIMIT 1", ("%" + date_str + "%",)).fetchone()
            db.close()
            return row is not None
        except Exception as _e:
            _log.debug("reflection_written: %s", _e)
            return False

    def _reflection_catchup(self):
        """HANS_REFLECTION_BRAIN_UP_CATCHUP_V1 — dojeď VČEREJŠÍ reflexi.

        Večerní okno (22:00–23:59) je krátké a kolem 23:00 mizí mozek s PC.
        Reflexe proto nesmí viset jen na něm: ráno, jakmile je mozek zpět,
        se dopíše za včerejšek. Klíč `_last_reflection_date` se nastaví na
        VČEREJŠEK, takže dnešní noční reflexe (`!= today`) zůstává nedotčená.
        Jen jeden den zpět — reflexe stará dva dny už nemá komu co říct.
        """
        from datetime import timedelta as _td
        # HANS_REFLECTION_CATCHUP_LOCK_V1 (15. 9.) — catchup volají DVĚ cesty:
        # brain_up callback (hans_idle, vlastní vlákno) a tick(). Po návratu
        # mozku se potkají během vteřin, obě projdou `_reflection_written`
        # (reflexe běží desítky minut a do deníku se zapíše až na konci)
        # a za týž den vzniknou DVĚ reflexe — doloženo 10. 9. a 15. 9.
        # Druhý volající nečeká, jen odejde: reflexi už dělá první.
        if not self._catchup_lock.acquire(blocking=False):
            return
        try:
            self._reflection_catchup_locked(_td)
        finally:
            self._catchup_lock.release()

    def _reflection_catchup_locked(self, _td):
        try:
            if self._reflection is None:
                return
            now = datetime.now()
            if now.hour >= self._night_hour:
                return                      # večerní okno běží, patří nočnímu ticku
            target = (now - _td(days=1)).strftime("%Y-%m-%d")
            if self._last_reflection_date == target:
                return
            if self._reflection_written(target):
                self._last_reflection_date = target
                self._save_routine_state()
                return
            if not (self._brain_up() and self._chat_quiet_ok()):
                return
            result = self._reflection.run(target_date=target)
            if not result:
                _log.debug("Večerní reflexe (catchup %s): odložena", target)
                return
            self._last_reflection_date = target
            self._save_routine_state()
            _log.info("Večerní reflexe (catchup za %s): zapsána (%d znaků)",
                      target, len(result))
            try:
                from scripts.hans_schedule import mark as _sched_mark
                _sched_mark("evening_reflection", True)
            except Exception:
                pass
        except Exception as _e:
            _log.warning("Večerní reflexe (catchup) selhala: %s", _e)

    def reflection_catchup_async(self):
        """Neblokující obal (volá se z brain_up callbacku)."""
        import threading
        threading.Thread(target=self._reflection_catchup, daemon=True).start()

    def _chat_quiet_ok(self) -> bool:
        """REFLECTION_QUIET_GATE_V1 — True když posledních reflection_quiet_min
        minut nikdo nechatoval ANI nebyl přítomen (jinak by noční analytika
        na base modelu kolidovala o VRAM s chatem). Fail-open při chybě."""
        import sqlite3 as _sql
        from datetime import datetime as _dt
        quiet_s = float(self.config.get("hans_routine", {}).get(
            "reflection_quiet_min", 15)) * 60.0
        try:
            conn = _sql.connect("file:%s?mode=ro" % self._diary_path,
                                uri=True, timeout=3.0)
            try:
                row = conn.execute(
                    "SELECT MAX(ts) FROM diary WHERE event_type "
                    "IN ('person_seen','human_chat')").fetchone()
            finally:
                conn.close()
            last = (row[0] if row and row[0] else 0) or 0
            if last and (_dt.now().timestamp() - last) < quiet_s:
                return False  # ještě čerstvá aktivita → počkej
            return True
        except Exception as _e:
            _log.warning("_chat_quiet_ok read failed (fail-open): %s", _e)
            return True

    def tick(self):
        """Periodická kontrola — detekce změny fáze + spánek (SLEEP_MODE_V1)."""
        if not self._enabled:
            return

        # SLEEP_MODE_V1 — spánek řeší SLEEP_WATCHER_THREAD_V1 (vlastní vlákno,
        # nezávislé na tomto ticku, který může viset na noční LLM analytice).

        new_phase = self._calc_phase()
        if new_phase != self._current_phase:
            old = self._current_phase
            self._current_phase = new_phase
            self._on_phase_change(old, new_phase)
        # HANS_CALENDAR_V1 — throttlovaný sync Proton kalendáře (ICS) na pozadí.
        self._maybe_calendar_sync()
        # HANS_NOTIFY_QUEUE_V1 — odešli, co do fronty zapsal skript zvenčí.
        self._drain_notify_queue()
        # HANS_ART_RETRY_V1 — dotáhni obraz, který uživatel chtěl a nevyšel.
        self._maybe_retry_paint()
        # HANS_WEBSHARE_PRESUN_TICK_V1 — dotáhni stahování z Webshare: stahuje
        # se na Pi (běží nonstop), hotový soubor se přesouvá na PC. Patří to
        # sem, do tick(), a NE do nočních úloh: PC se v noci vypíná (~3:27),
        # takže by přesun narazil přesně na to, kvůli čemu se stahuje na Pi.
        self._maybe_webshare_presun()
        # HANS_PREVENTION_V1 — hodinový sběr denních souhrnů (chyby, samoopravy,
        # disky, paměť). Jen SBĚR; hlášení až nad ~14 dny základu.
        self._maybe_prevence_sber()
        # HANS_REFLECTION_BRAIN_UP_CATCHUP_V1 — dojeď VČEREJŠÍ reflexi, když
        # večerní okno propásla (PC bývá po 23:00 vypnuté). ⚠️ Patří sem, do
        # tick(), NE do `_run_night_tasks` — ten běží pod `if self.is_night`,
        # takže ráno by se catchup nikdy nespustil (chyceno živým testem 7.8.).
        # Gate (mimo večerní okno / dnes už hotovo / mozek / klid) je uvnitř
        # a po prvním úspěchu se vrací hned na paměťovém příznaku.
        try:
            self._reflection_catchup()
        except Exception as _re:
            _log.debug("reflection catchup v ticku: %s", _re)
        # NIGHT_WORKER_THREAD_V1 — noční LLM analytika se sem UŽ NEVOLÁ; běží
        # ve vlastním vlákně (_night_worker_loop). Zaseklá Ollama tak
        # neblokuje tento tick ani volajícího (proaktivita/film/autoplay).

    _SCHED_NOTIFIED = os.path.join("data", ".sched_notified.json")

    def _night_worker_loop(self):
        """NIGHT_WORKER_THREAD_V1 — periodicky spouští noční analytiku NEZÁVISLE
        na tick(). Non-blocking guard: visí-li předchozí běh (zaseklá Ollama),
        tento cyklus se přeskočí (žádné hromadění vláken)."""
        while not self._stop.is_set():
            if self._stop.wait(self._night_check_interval):
                break
            if not self._enabled:
                continue
            if not self._night_lock.acquire(blocking=False):
                continue  # předchozí běh ještě neskončil → přeskoč
            try:
                self._maybe_wake_for_analytics()  # HANS_PC_NIGHT_ANALYTICS_WAKE
                self._maybe_start_night_catchup()  # HANS_NIGHT_DAY_CATCHUP_V1
                self._run_night_tasks()
                self._maybe_shutdown_pc()  # HANS_PC_NIGHT_SHUTDOWN
            except Exception as _e:
                _log.warning('night worker: %s', _e)
            finally:
                try:
                    self._night_lock.release()
                except Exception:
                    pass

    # HANS_PC_NIGHT_SHUTDOWN — noční analytické eventy (marker „analytika běží")
    _NIGHT_ANALYTICS_EVENTS = (
        "evening_reflection", "tendency_snapshot", "night_summary", "dream",
        "study_note", "study_mastery", "synthesis_idea", "self_critique",
        "immune_check", "narrative_chapter", "creation_reflection",
        "writing_section", "work_completion_reflection", "lesson_learned",
        "book_completion_reflection", "musing", "introspection")

    # HANS_SHUTDOWN_SETTLE_FIX_V1 (3.8.) — settle guard shutdownu NESMÍ počítat
    # `introspection`: je to ambient sebereflexe na timeru (á ~5 min, když je
    # Hans sám), NE ohraničená noční práce. Po nočním probuzení mozku běží celou
    # noc → settle (20 min ticha) nikdy nenastal → PC se nevypnul (doloženo
    # 3.8.: wake 03:00 OK, ale introspekce á 5 min držela PC nahoře do rána).
    # Studium/analytika/tvorba (ohraničené) settle drží správně.
    _SHUTDOWN_SETTLE_EVENTS = tuple(
        e for e in _NIGHT_ANALYTICS_EVENTS if e != "introspection")

    # HANS_PC_NIGHT_ANALYTICS_WAKE_V2 (26.7.) — TĚŽKÁ reasoning-tier analytika
    # (qwen3: syntéza/sebekritika), která běží AŽ v analytics_hour. Wake PC se
    # rozhoduje podle NÍ, NE podle celého _NIGHT_ANALYTICS_EVENTS: night_summary
    # + dream v 00:00 (běží s mozkem ještě nahoře) jinak zablokovaly probuzení
    # PC na reasoning tier ve 3:00 → qwen3 analytika se nikdy nespustila.
    _HEAVY_ANALYTICS_EVENTS = ("synthesis_idea", "self_critique")
    # HANS_NIGHT_DAY_CATCHUP_V1 — co se smí dohnat přes den: jen úlohy, které
    # potřebují mozek a mají vlastní denní/kadenční pojistku. Večerní a půlnoční
    # úlohy (shrnutí, sen, reflexe, malba, hygiena) sem NEPATŘÍ — mají vlastní
    # hodinu nebo vlastní dohánění. `drain_setup` musí zůstat (nastavuje ctx).
    _NIGHT_CATCHUP_TASKS = frozenset((
        'stance_debates', 'narrative', 'drain_setup', 'study', 'toolscout',
        'maker', 'authorship', 'synthesis', 'selfcritique', 'immune'))

    # ── Fáze dne ─────────────────────────────────────────────────────────────

    def _in_night_window(self) -> bool:
        """NIGHT_WINDOW_FULL_V1 — celá noční fáze (night_hour..morning_hour, tj.
        22:00–06:00), ne jen 2h před půlnocí. Pro práci, která NEtrpí 'tenkými daty
        nového dne' (art/hygiena/studium): restart po půlnoci ani rušné pre-midnight
        okno pak nestojí celou noc. Reflexe/narativ/tendence záměrně zůstávají
        premidnight (po 00:00 flipne datum → konfabulace z tenkých dat)."""
        # HANS_NIGHT_DAY_CATCHUP_V1 — běží-li denní dohánění propadlé noci,
        # úlohy vázané na noční okno se smějí spustit i přes den.
        if time.time() < getattr(self, "_night_catchup_until", 0.0):
            return True
        h = datetime.now().hour
        return h >= self._night_hour or h < self._morning_hour

    def _calc_phase(self) -> str:
        h = datetime.now().hour
        if h >= self._night_hour or h < self._morning_hour:
            return PHASE_NIGHT
        if h >= self._evening_hour:
            return PHASE_EVENING
        if h >= self._afternoon_hour:
            return PHASE_AFTERNOON
        return PHASE_MORNING

    def _on_phase_change(self, old: str, new: str):
        _log.info("Fáze dne: %s → %s", old, new)

        # Komentář do deníku
        import random
        comments = _PHASE_COMMENTS.get(new, [])
        comment = random.choice(comments) if comments else ""
        self._diary_write("phase_change",
                          f"Fáze: {_PHASE_LABELS_CZ.get(new, new)}",
                          comment)

        # TTS komentář (jen ráno a večer — ne v noci, ne odpoledne)
        if (new in (PHASE_MORNING, PHASE_EVENING) and comment and self._tts
                and self._phase_comments_enabled):  # SLEEP_MODE_V1 (F=3)
            try:
                self._tts.speak(comment)
            except Exception:
                pass

        # Ranní komentář — počasí
        if new == PHASE_MORNING:
            self._morning_routine()

        # Callbacks
        for cb in self._callbacks:
            try:
                cb(old, new)
            except Exception as e:
                _log.error("Phase callback error: %s", e)

    # ── Ranní rutina ─────────────────────────────────────────────────────────

    # ── Noční shrnutí ────────────────────────────────────────────────────────

    # ── Sny ──────────────────────────────────────────────────────────────────

    # ── DB helper ────────────────────────────────────────────────────────────

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

    def stop(self):
        self._stop.set()
