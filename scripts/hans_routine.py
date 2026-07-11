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
        "Ráno. Slunce vychází a s ním i povinnosti majordoma.",
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
        "Noc. Dům je tichý. Čas na odpočinek — i pro majordoma.",
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


class HansRoutine:
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
        self._last_narrative = ""        # AUTOBIOGRAPHICAL_NARRATIVE_V1 (krok 3, týdenní guard)
        self._last_creation_reflection = ""  # HANS_CREATION_REFLECTION_V1 (D, týdenní guard)
        self._last_study_date = ""       # HANS_STUDY_V1 (1 studijní session/noc)
        self._last_writing_date = ""     # HANS_AUTHORSHIP_V1 (1 autorská session/noc)
        self._last_synthesis_date = ""   # HANS_SYNTHESIS_IDEAS_V1 (vlastní nápady, kadence)
        self._last_selfcritique_date = ""  # HANS_SELFCRITIQUE_V1 (sebekritika, kadence)
        self._last_immune_date = ""      # HANS_IMMUNE_A2_V1 (noční fact-check tvrzení)
        self._last_hygiene_date = ""     # HANS_MEMORY_HYGIENE_V1 (prořez firehose 1×/noc)
        self._last_pc_shutdown_date = ""  # HANS_PC_NIGHT_SHUTDOWN (vypni PC po analytice 1×/noc)
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
        # HANS_DISTILLATION_V1 — fáze 2a noční destilace záseku
        self._distillation = None
        self._distillation_running = False   # idempotence — jednou denně
        self._saved_tilt = None        # poloha před spánkem (pro návrat)
        self._saved_tracking = None    # tracking stav před spánkem
        self._sleep_started_ts = None  # HANS_MORNING_HEALTH_V1 — kdy usnul (okno pro ranní scan logů)
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
            self._last_rel_reflection_date = s.get(
                "last_rel_reflection_date", "")
            self._last_severka_check = s.get("last_severka_check", "")
            self._last_narrative = s.get("last_narrative", "")
            self._last_creation_reflection = s.get("last_creation_reflection", "")
            self._last_study_date = s.get("last_study_date", "")  # HANS_STUDY_V1
            self._last_writing_date = s.get("last_writing_date", "")  # HANS_AUTHORSHIP_V1
            self._last_synthesis_date = s.get("last_synthesis_date", "")  # HANS_SYNTHESIS_IDEAS_V1
            self._last_selfcritique_date = s.get("last_selfcritique_date", "")  # HANS_SELFCRITIQUE_V1
            self._last_immune_date = s.get("last_immune_date", "")  # HANS_IMMUNE_A2_V1
            self._last_hygiene_date = s.get("last_hygiene_date", "")  # HANS_MEMORY_HYGIENE_V1
            self._last_pc_shutdown_date = s.get("last_pc_shutdown_date", "")  # HANS_PC_NIGHT_SHUTDOWN
            self._last_analytics_wake_date = s.get("last_analytics_wake_date", "")  # HANS_PC_NIGHT_ANALYTICS_WAKE
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
                    "last_rel_reflection_date":
                        self._last_rel_reflection_date,
                    "last_severka_check": self._last_severka_check,
                    "last_narrative": self._last_narrative,
                    "last_creation_reflection": self._last_creation_reflection,
                    "last_study_date": self._last_study_date,  # HANS_STUDY_V1
                    "last_writing_date": self._last_writing_date,  # HANS_AUTHORSHIP_V1
                    "last_synthesis_date": self._last_synthesis_date,  # HANS_SYNTHESIS_IDEAS_V1
                    "last_selfcritique_date": self._last_selfcritique_date,  # HANS_SELFCRITIQUE_V1
                    "last_immune_date": self._last_immune_date,  # HANS_IMMUNE_A2_V1
                    "last_hygiene_date": self._last_hygiene_date,  # HANS_MEMORY_HYGIENE_V1
                    "last_pc_shutdown_date": self._last_pc_shutdown_date,  # HANS_PC_NIGHT_SHUTDOWN
                    "last_analytics_wake_date": self._last_analytics_wake_date,  # HANS_PC_NIGHT_ANALYTICS_WAKE
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
        """True když uplynul aspoň týden od posledního checku (cadence guard)."""
        last = self._last_severka_check
        if not last:
            return True
        try:
            d0 = datetime.strptime(last, "%Y-%m-%d").date()
            d1 = datetime.strptime(today, "%Y-%m-%d").date()
            return (d1 - d0).days >= 7
        except Exception:
            return True

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

    # ─── WOL_ON_PRESENCE_V1 ───────────────────────────────────────────
    def wake_pc_on_presence(self):
        """Osoba přišla → pokud PC spí, vzbuď ho (teplá Ollama na chat).
        Throttle (wol_presence_cooldown_min) + neblokující. Volá person_seen."""
        if not self._wol_pc_enabled or not self._wol_on_presence or not self._wol_pc_mac:
            return
        import time as _t
        now = _t.time()
        cd = float(self.config.get("hans_routine", {}).get(
            "wol_presence_cooldown_min",
            self.config.get("wol_presence_cooldown_min", 15))) * 60.0
        if (now - self._wol_presence_last) < cd:
            return
        self._wol_presence_last = now
        import threading
        threading.Thread(target=self._wol_presence_flow, daemon=True).start()

    def _wol_presence_flow(self):
        """Ping PC; offline → packet + verify. Daemon thread."""
        import time as _t
        try:
            ip, mac = self._wol_pc_ip, self._wol_pc_mac
            if self._ping_host(ip):
                return  # PC běží
            _log.info("WOL(presence): %s offline → packet (osoba přišla)", ip)
            self._send_wol_packet(mac)
            for _i in range(8):
                _t.sleep(15)
                if self._ping_host(ip):
                    _log.info("WOL(presence): %s ONLINE %ds po packetu",
                              ip, (_i + 1) * 15)
                    break
        except Exception as _e:
            _log.warning("WOL(presence) flow: %s", _e)

    # ─── WOL_WAKE_PC_V1 ───────────────────────────────────────────────
    def _wol_timer_loop(self):
        """WOL_POLL_LOOP_V1 — robustni poller (nahradil krehky one-shot sleep,
        ktery tise nefiroval). Kazdych POLL_S rano (pred sleep_end_hour) zavola
        _maybe_wake_pc; ta sama hlida okno [start_dt, ..) + 1x/den guard."""
        import time as _t
        from datetime import datetime as _dt
        _log.info("WOL: poll thread started (sleep_end_hour=%s, lead=%dm)",
                  self._sleep_end_hour, self._wol_min_before)
        POLL_S = 600  # 10 min
        while True:
            try:
                now = _dt.now()
                if now.hour < self._sleep_end_hour:
                    self._maybe_wake_pc(now)
            except Exception as _e:
                _log.warning("WOL poll loop: %s", _e)
            _t.sleep(POLL_S)

    def _maybe_wake_pc(self, now):
        """Triggered z _check_sleep_window. Spawn WOL flow asynchronně."""
        from datetime import timedelta as _td
        wake_dt = now.replace(hour=self._sleep_end_hour,
                              minute=0, second=0, microsecond=0)
        start_dt = wake_dt - _td(minutes=self._wol_min_before)
        # WOL_WINDOW_FIX_V1 — dropnuta horní mez (now < wake_dt): řídký sleep-tik
        # úzké okno míjel. Fírni na 1. tiku po start_dt (1x/den guard níž;
        # caller hlídá _sleeping → po probuzení už nefírne).
        if now < start_dt:
            return
        today = now.date()
        if self._wol_last_date == today:
            return  # už dnes spuštěno
        self._wol_last_date = today
        _log.info('WOL: trigger in window — spawning wake_pc')
        import threading
        threading.Thread(target=self._wol_wake_pc, daemon=True).start()

    def _wol_wake_pc(self):
        """Ping → wait → ping → WOL packet. Běží v daemon thread."""
        import time as _t
        try:
            ip = self._wol_pc_ip
            mac = self._wol_pc_mac
            if not mac:
                _log.warning('WOL: MAC nenastaveno, skip')
                return
            if self._ping_host(ip):
                _log.info('WOL: %s online (ping 1) — skip', ip)
                return
            _log.info('WOL: %s offline (ping 1), čekám 60s', ip)
            _t.sleep(60)
            if self._ping_host(ip):
                _log.info('WOL: %s online (ping 2) — skip', ip)
                return
            _log.info('WOL: %s pořád offline, posílám magic packet', ip)
            self._send_wol_packet(mac)
            # WOL_VERIFY_V1 — po packetu ověř probuzení (poll ~2 min), ať je WOL z logů verifikovatelný
            for _i in range(8):
                _t.sleep(15)
                if self._ping_host(ip):
                    _log.info('WOL: %s ONLINE %ds po packetu — probuzeno', ip, (_i + 1) * 15)
                    break
            else:
                _log.warning('WOL: %s pořád offline 120s po packetu — probuzení se nezdařilo?', ip)
        except Exception as _e:
            _log.error('WOL: wake_pc failed: %s', _e)

    @staticmethod
    def _ping_host(ip, timeout=2):
        """True pokud ping prošel."""
        import subprocess
        if not ip:
            return False
        try:
            r = subprocess.run(['ping', '-c', '1', '-W', str(timeout), ip],
                               capture_output=True, timeout=timeout + 1)
            return r.returncode == 0
        except Exception:
            return False

    @staticmethod
    def _send_wol_packet(mac):
        """Pošli WOL magic packet (UDP broadcast :9)."""
        import socket
        mac_clean = mac.replace(':', '').replace('-', '').lower()
        if len(mac_clean) != 12 or not all(c in '0123456789abcdef' for c in mac_clean):
            raise ValueError('Invalid MAC: %r' % mac)
        # Magic: 6×0xFF + 16×MAC
        packet = bytes.fromhex('FF' * 6 + mac_clean * 16)
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            s.sendto(packet, ('255.255.255.255', 9))
            _log.info('WOL: magic packet sent to %s', mac)
        finally:
            s.close()

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

    def set_manual_sleep(self, state: bool):
        """Manuální override z chat /sleep. Toggle proběhne ve volajícím.
        Override platí přes okno a expiruje na opačné hraně přirozeně."""
        self._manual_override = bool(state)
        _log.info('SLEEP: manual override set to %s', state)
        self._apply_sleep_mode(state)

    def _apply_sleep_mode(self, active: bool):
        """Aktivuje/deaktivuje spánkový režim. Defenzivně: každý
        krok ve try/except, selhání jedné periferie nepoloží zbytek.
        Volá se jednou denně z tick() na hraně hodiny."""
        if active:
            # SLEEP_FLAG_EARLY_V1 — _sleeping=True PŘED akcemi, aby settery viděly True
            self._sleeping = True
            # HANS_MORNING_HEALTH_V1 — zaznamenej čas usnutí (okno pro ranní
            # sebe-kontrolu logů; hans_idle scanuje od této chvíle do probuzení)
            import time as _t_mh
            self._sleep_started_ts = _t_mh.time()
            _log.info('SLEEP: aktivuji (TTS off + kamera nahoru)')
            # 1) Zapamatuj polohu serva + tracking stav
            try:
                if self._servo is not None and hasattr(self._servo, 'get_current_position'):
                    pos = self._servo.get_current_position()
                    # get_current_position vrací nějakou strukturu — ulož celé
                    self._saved_tilt = pos
                    _tt = getattr(self._servo, 'tracking_thread', None)  # TRACKING_RESTORE_FIX_V1
                    self._saved_tracking = bool(_tt is not None and _tt.is_alive())
            except Exception as _e:
                _log.warning('SLEEP: save pos failed: %s', _e)
            # 2) Stop tracking (aby se servo nevracelo za tváří)
            try:
                if self._servo is not None and hasattr(self._servo, 'stop_tracking'):
                    self._servo.stop_tracking()
            except Exception as _e:
                _log.warning('SLEEP: stop_tracking failed: %s', _e)
            # 3) Pohni kamerou nahoru — kamera je fyzicky obráceně, takže tilt_min = fyzicky nahoru
            # (SLEEP_TILT_FLIP_V1 — z naživo testu 22:00: +tilt = dolů, -tilt = nahoru)
            try:
                if self._servo is not None and hasattr(self._servo, 'manual_tilt'):
                    tmin = getattr(self._servo, 'tilt_min', -30)
                    self._servo.manual_tilt(tmin)
                    _log.info('SLEEP: servo tilt -> %d (strop, kamera obráceně)', tmin)
            except Exception as _e:
                _log.warning('SLEEP: manual_tilt up failed: %s', _e)
            # 4) TTS off — nejdůležitější (Hans/Koláč v noci nevybafnou)
            try:
                if self._tts is not None:
                    self._tts.enabled = False
                    _log.info('SLEEP: TTS enabled=False')
            except Exception as _e:
                _log.warning('SLEEP: TTS off failed: %s', _e)
            # 4b) Vypni kameru + recognition (SLEEP_VISION_OFF_V1)
            try:
                if self._vision is not None and hasattr(self._vision, 'pause_vision'):
                    self._vision.pause_vision()
                    _log.info('SLEEP: vision off (kamera+recognition)')
            except Exception as _e:
                _log.warning('SLEEP: pause_vision failed: %s', _e)
            # 5) Stopa do deníku
            try:
                from scripts.hans_persona import persona_name as _pn  # PERSONA_NAME_CONFIGURABLE_V1
                self._diary_write('sleep_start',
                                  f'{_pn(self.config)} usnul',
                                  'TTS vypnuto, kamera otočena ke stropu.')
            except Exception as _e:
                _log.debug('SLEEP: diary sleep_start failed: %s', _e)
            # SLEEP_FLAG_EARLY_V1 — self._sleeping=True přesunut na začátek if-active
        else:
            _log.info('SLEEP: deaktivuji (probuzení)')
            # 1) TTS zpět
            try:
                if self._tts is not None:
                    self._tts.enabled = True
                    _log.info('SLEEP: TTS enabled=True')
            except Exception as _e:
                _log.warning('SLEEP: TTS on failed: %s', _e)
            # 1b) Nahoď kameru + recognition (SLEEP_VISION_OFF_V1)
            try:
                if self._vision is not None and hasattr(self._vision, 'resume_vision'):
                    self._vision.resume_vision()
                    _log.info('SLEEP: vision on (kamera+recognition)')
            except Exception as _e:
                _log.warning('SLEEP: resume_vision failed: %s', _e)
            # 2) Vrátit polohu serva (zapamatovaná → fallback move_to_center)
            try:
                if self._servo is not None:
                    restored = False
                    if self._saved_tilt is not None:
                        # Zkus získat tilt číslo z různých možných tvarů (tuple/dict/scalar)
                        try:
                            t_val = None
                            sv = self._saved_tilt
                            if isinstance(sv, (int, float)):
                                t_val = sv
                            elif isinstance(sv, dict):
                                t_val = sv.get('tilt', sv.get('current_tilt'))
                            elif isinstance(sv, (tuple, list)) and len(sv) >= 2:
                                t_val = sv[1]   # předpokládáme (pan, tilt)
                            if t_val is not None and hasattr(self._servo, 'manual_tilt'):
                                self._servo.manual_tilt(float(t_val))
                                restored = True
                                _log.info('SLEEP: servo tilt -> %s (návrat)', t_val)
                        except Exception as _ee:
                            _log.debug('SLEEP: parse saved tilt failed: %s', _ee)
                    if not restored and hasattr(self._servo, 'move_to_center'):
                        self._servo.move_to_center()
                        _log.info('SLEEP: servo -> center (fallback)')
            except Exception as _e:
                _log.warning('SLEEP: restore servo failed: %s', _e)
            # 3) Tracking zpět (pokud byl)
            try:
                if (self._servo is not None and self._saved_tracking
                        and hasattr(self._servo, 'start_tracking')):
                    self._servo.start_tracking()
                    _log.info('SLEEP: tracking obnoveno')
            except Exception as _e:
                _log.warning('SLEEP: start_tracking failed: %s', _e)
            # 4) Stopa do deníku
            try:
                from scripts.hans_persona import persona_name as _pn  # PERSONA_NAME_CONFIGURABLE_V1
                self._diary_write('sleep_end',
                                  f'{_pn(self.config)} se probudil',
                                  'TTS zapnuto, kamera vrácena.')
            except Exception as _e:
                _log.debug('SLEEP: diary sleep_end failed: %s', _e)
            self._saved_tilt = None
            self._saved_tracking = None
            self._sleeping = False

    def _sleep_watcher_loop(self):
        """SLEEP_WATCHER_THREAD_V1 — periodicky kontroluje spánkové okno NEZÁVISLE
        na tick() (který může viset na noční LLM analytice). _check_sleep_window je
        rychlý (LLM v něm neběží), takže usínání proběhne včas i při zaseklé Ollamě."""
        while not self._stop.is_set():
            # wait-first: dej startu čas navázat serva/TTS/kameru, ať první
            # _apply_sleep_mode nepropadne na None periferiích
            if self._stop.wait(self._sleep_check_interval):
                break
            try:
                if self._enabled:
                    self._check_sleep_window()
            except Exception as _e:
                _log.debug('sleep watcher: %s', _e)

    def _check_sleep_window(self):
        """Idempotentně přepíná _sleeping podle hodiny. Volá SLEEP_WATCHER_THREAD_V1
        (vlastní vlákno). Zámek (non-blocking) chrání před souběhem s manuálním /sleep."""
        lock = getattr(self, '_sleep_lock', None)
        if lock is not None and not lock.acquire(blocking=False):
            return  # už běží v druhém vlákně → přeskoč (idempotentní)
        try:
            from datetime import datetime as _dt
            h = _dt.now().hour
            # Spánek: od sleep_start_hour (vč.) do sleep_end_hour (vyl.)
            # SLEEP_WRAP_WINDOW_V1 - okno muze pretekat pres pulnoc (napr. 23->9)
            if self._sleep_start_hour <= self._sleep_end_hour:
                in_window = (h >= self._sleep_start_hour and h < self._sleep_end_hour)
            else:
                in_window = (h >= self._sleep_start_hour or h < self._sleep_end_hour)

            # SLEEP_MANUAL_OVERRIDE_V1 — edge detection pro auto-expiraci
            if self._prev_in_window is None:
                self._prev_in_window = in_window
            window_started = (not self._prev_in_window) and in_window
            window_ended   = self._prev_in_window and (not in_window)
            self._prev_in_window = in_window

            # Auto-expirace override na opačné hraně okna
            if self._manual_override is True and window_ended:
                _log.info('SLEEP: manual override (sleep) expired at natural wake')
                self._manual_override = None
            elif self._manual_override is False and window_started:
                _log.info('SLEEP: manual override (wake) expired at natural sleep')
                self._manual_override = None

            # Výpočet should_sleep — override má přednost
            if self._manual_override is not None:
                should_sleep = self._manual_override
            else:
                should_sleep = in_window

            # SLEEP_DEBUG_CLEANUP_V1 — SLEEP CHK ztlumeno na debug (provozni sum)
            _log.debug('SLEEP CHK: h=%s start=%s end=%s in_window=%s override=%s should_sleep=%s _sleeping=%s',
                       h, self._sleep_start_hour, self._sleep_end_hour, in_window, self._manual_override, should_sleep, self._sleeping)

            # WOL_TICKLOOP_REMOVED_V1 — tick-loop WOL cesta odstranena; WOL resi
            # vyhradne WOL_POLL_LOOP_V1 (hodinovy gate hour<sleep_end_hour). Stara
            # cesta volala _maybe_wake_pc bez horni casove meze -> pri vecer uspanem
            # PC hrozilo nocni probuzeni (videno 23:03 trigger, skip jen diky online).

            # HANS_DISTILLATION_V1 — fáze 2a noční destilace (po usnutí, jen 02-04)
            if (self._distillation is not None
                    and self._sleeping
                    and self._manual_override is None):
                self._maybe_distill()

            if should_sleep and not self._sleeping:
                self._apply_sleep_mode(True)
            elif (not should_sleep) and self._sleeping:
                self._apply_sleep_mode(False)
                # DISTILL_MORNING_CATCHUP_V1 — dozen destilaci po probuzeni,
                # pokud v noci padla. Idempotenci (1x/den) hlida run() sam.
                if (self._distillation is not None
                        and self._manual_override is None):
                    self._maybe_distill(ignore_window=True)
        except Exception as _e:
            _log.warning('SLEEP: window check failed: %s', _e)
        finally:
            if lock is not None:
                try:
                    lock.release()
                except Exception:
                    pass

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
        # NIGHT_WORKER_THREAD_V1 — noční LLM analytika se sem UŽ NEVOLÁ; běží
        # ve vlastním vlákně (_night_worker_loop). Zaseklá Ollama tak
        # neblokuje tento tick ani volajícího (proaktivita/film/autoplay).

    def _maybe_calendar_sync(self):
        """HANS_CALENDAR_V1 — stáhne ICS feed (throttle sync_interval_min).
        Běží ve vlákně, ať síťový fetch neblokuje tick. No-op když vypnuto."""
        try:
            cc = (self.config.get("calendar", {}) or {})
            from scripts.hans_calendar import is_enabled as _cal_enabled
            if not _cal_enabled(self.config):  # per-osoba (people map)
                return
            interval = int(cc.get("sync_interval_min", 30)) * 60
            last = getattr(self, "_last_cal_sync", 0.0)
            now = time.time()
            if now - last < interval:
                return
            self._last_cal_sync = now
            import threading

            def _do():
                try:
                    from scripts.hans_calendar import CalendarStore
                    CalendarStore(self.config, self._diary_path).sync()
                except Exception as e:
                    _log.warning("calendar sync selhal: %s", e)
            threading.Thread(target=_do, daemon=True).start()
        except Exception:
            pass

    def _health_watcher_loop(self):
        """HANS_HEALTH_V1 — periodická probe závislostí + self-heal zaseklé
        Ollamy. Vlastní vlákno (nezávislé na tick). Self-heal AŽ po N po sobě
        jdoucích WEDGED (transientní timeout nerestartuje); DOWN (PC spí / služba
        neběží) se NEheal-uje — to není zásek, ale legitimní nedostupnost."""
        # po startu nech služby usadit
        if self._stop.wait(min(120.0, self._health_interval)):
            return
        while not self._stop.is_set():
            try:
                from scripts import hans_health
                health = hans_health.probe_all(self.config)
                self._health_last = health
                healed = []
                oll = (health.get('ollama', {}) or {}).get('status')
                if oll == hans_health.WEDGED:
                    self._health_wedge_strikes += 1
                    need = int((self.config.get('health', {}) or {}).get(
                        'wedge_strikes', 2))
                    if self._health_wedge_strikes >= need:
                        if hans_health.heal_ollama(self.config):
                            healed.append('ollama')
                            self._health_wedge_strikes = 0
                            if self._notifier:
                                try:
                                    self._notifier("Můj mozek se zasekl, "
                                                   "restartoval jsem ho.")
                                except Exception:
                                    pass
                else:
                    self._health_wedge_strikes = 0
                hans_health._write_state(health, healed)
                bad = hans_health.degraded_services(health)
                if bad:
                    _log.warning('health: degradováno %s (healed=%s, strikes=%d)',
                                 bad, healed, self._health_wedge_strikes)
            except Exception as _e:
                _log.debug('health watcher: %s', _e)
            if self._stop.wait(self._health_interval):
                break

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

    def _pc_up(self):
        """Rychlá kontrola, jestli PC běží (ping, ~2s)."""
        try:
            return self._ping_host(self._wol_pc_ip)
        except Exception:
            return False

    def _maybe_wake_for_analytics(self):
        """HANS_PC_NIGHT_ANALYTICS_WAKE — v noční hodinu probuď PC (když je
        vypnutý) pro noční analytiku. Analytika pak proběhne (_run_night_tasks)
        a _maybe_shutdown_pc PC zase vypne. 1×/noc. Když PC běží / analytika
        dnes už proběhla → nebudí."""
        try:
            c = (self.config.get("pc_night_shutdown", {}) or {})
            if not c.get("enabled") or not c.get("wake_for_analytics", True):
                return
            now = datetime.now()
            today = now.strftime("%Y-%m-%d")
            if self._last_analytics_wake_date == today:
                return
            if now.hour != int(c.get("analytics_hour", 3)):
                return
            import sqlite3 as _sql
            import time as _t
            nowts = _t.time()
            # analytika dnes v noci/večer už proběhla? → netřeba budit
            conn = _sql.connect("file:%s?mode=ro" % self._diary_path,
                                uri=True, timeout=3.0)
            ph = ",".join("?" * len(self._NIGHT_ANALYTICS_EVENTS))
            ra = conn.execute(
                "SELECT MAX(ts) FROM diary WHERE event_type IN (%s) AND ts >= ?"
                % ph, (*self._NIGHT_ANALYTICS_EVENTS, nowts - 16 * 3600)
            ).fetchone()
            conn.close()
            self._last_analytics_wake_date = today
            self._analytics_wake_ts = nowts
            self._save_routine_state()
            if ra and ra[0] and nowts - ra[0] < 6 * 3600:
                return  # analytika už dnes běžela
            if self._pc_up():
                return  # PC běží → analytika proběhne sama
            _log.info("PC night wake: budím PC pro noční analytiku")
            self._send_wol_packet(self._wol_pc_mac)
            for _ in range(9):  # čekej na náběh z S5 (~40-90s)
                _t.sleep(10)
                if self._pc_up():
                    _log.info("PC night wake: PC naběhl → analytika poběží")
                    return
            _log.warning("PC night wake: PC nenaběhl do 90s")
        except Exception as _e:
            _log.warning("pc_night_wake: %s", _e)

    def _maybe_shutdown_pc(self):
        """HANS_PC_NIGHT_SHUTDOWN — po dokončení noční analytiky vypni PC.
        S3 suspend je na téhle desce rozbitý (reboot); čistý poweroff + ranní
        WOL. Guardy: hluboké noční okno, analytika usazená (žádný event N min),
        žádný recent chat, PC vzhůru, 1× za noc."""
        try:
            c = (self.config.get("pc_night_shutdown", {}) or {})
            if not c.get("enabled"):
                return
            from scripts import pc_remote
            if not pc_remote.enabled(self.config):
                return
            now = datetime.now()
            today = now.strftime("%Y-%m-%d")
            if self._last_pc_shutdown_date == today:
                return
            h0 = int(c.get("night_start_hour", 2))
            h1 = int(c.get("night_end_hour", 6))
            if not (h0 <= now.hour < h1):
                return
            import sqlite3 as _sql
            import time as _t
            nowts = _t.time()
            conn = _sql.connect("file:%s?mode=ro" % self._diary_path,
                                uri=True, timeout=3.0)
            # recent chat → nevypínej
            chat_q = int(c.get("chat_quiet_minutes", 30)) * 60
            rc = conn.execute(
                "SELECT MAX(ts) FROM diary WHERE event_type='human_chat'"
            ).fetchone()
            if rc and rc[0] and nowts - rc[0] < chat_q:
                conn.close()
                return
            # analytika usazená? poslední noční event > settle_minutes
            settle = int(c.get("settle_minutes", 20)) * 60
            ph = ",".join("?" * len(self._NIGHT_ANALYTICS_EVENTS))
            ra = conn.execute(
                "SELECT MAX(ts) FROM diary WHERE event_type IN (%s) "
                "AND ts >= ?" % ph,
                (*self._NIGHT_ANALYTICS_EVENTS, nowts - 16 * 3600)).fetchone()
            conn.close()
            if ra and ra[0] and nowts - ra[0] < settle:
                return  # analytika ještě běží
            # NEvypni PŘED analytikou: povol shutdown jen když analytika DNES
            # proběhla, NEBO jsme kvůli ní budili a dali jí čas (settle) doběhnout.
            had_today = bool(ra and ra[0] and nowts - ra[0] < 16 * 3600)
            woke_settled = (self._last_analytics_wake_date == today
                            and self._analytics_wake_ts
                            and nowts - self._analytics_wake_ts >= settle)
            if not (had_today or woke_settled):
                return  # analytika ještě neproběhla (čekáme na wake+běh)
            # PC vzhůru? (rychlý ping — když dole, není co vypínat; guard NEnastavuj)
            if not self._pc_up():
                return
            # vypni
            pc_remote.run(self.config, "sudo -n systemctl poweroff", timeout=10)
            self._last_pc_shutdown_date = today
            self._save_routine_state()
            _log.info("PC night shutdown: analytika hotová → PC vypnut "
                      "(ranní WOL probudí)")
            try:
                conn2 = _sql.connect(self._diary_path, timeout=5.0)
                conn2.execute(
                    "INSERT INTO diary (ts, event_type, title, note) "
                    "VALUES (?,?,?,?)",
                    (nowts, "pc_shutdown", "Vypnutí PC na noc",
                     "Noční analytika dokončena — vypnul jsem počítač; "
                     "ráno ho probudím."))
                conn2.commit()
                conn2.close()
            except Exception:
                pass
        except Exception as _e:
            _log.warning("pc_night_shutdown: %s", _e)

    def _run_night_tasks(self):
        """NIGHT_WORKER_THREAD_V1 — všechny noční LLM/analytické úlohy (dříve
        inline v tick()). Každá je idempotentní (date guard) + deferral-safe,
        takže opakované volání z workeru je bezpečné."""
        # Noční aktivity (jednou za noc)
        today = datetime.now().strftime("%Y-%m-%d")
        if self.is_night:
            if self._night_summary_enabled and self._last_summary_date != today:
                self._last_summary_date = today
                self._write_night_summary()
            if self._night_dream_enabled and self._last_dream_date != today:
                self._last_dream_date = today
                self._write_dream()

            # Reflexe vztahových karet — 1× denně v nočním okně.
            # Spouští se po 22:30, ať to nepadne přesně se začátkem
            # noci a nezasahuje do night_summary.
            if (self._relationship_reflection is not None
                    and self._last_rel_reflection_date != today):
                now_dt = datetime.now()
                if now_dt.hour > 22 or (now_dt.hour == 22 and now_dt.minute >= 30):
                    self._last_rel_reflection_date = today
                    self._save_routine_state()  # ROUTINE_STATE_PERSIST_V1
                    try:
                        n = self._relationship_reflection.reflect_due_persons()
                        _log.info("Reflexe vztahových karet: %d updatováno", n)
                    except Exception as _e:
                        _log.error("Reflexe vztahových karet selhala: %s", _e)

            # Večerní reflexe dne — 1× za noc, jakmile je noc (22:00+).
            # Když run() vrátí None (Ollama nedostupná), flag se nenastaví
            # a další tick to zkusí znovu (každých check_interval_s).
            # Ruční spuštění (run_evening_reflection) také nastaví flag,
            # takže když uživatel napsal /denik dřív, auto v noci přeskočí.
            # REFLECTION_PREMIDNIGHT_ONLY_V1 - jen vecer pred pulnoci (hour>=night_hour);
            # po 00:00 today flipne a guard by firnul reflexi pro sotva zacaty
            # novy den (thin data -> konfabulace). Mirror gate u relationship refl.
            if (self._reflection is not None
                    and self._last_reflection_date != today
                    and datetime.now().hour >= self._night_hour
                    and self._chat_quiet_ok()):  # REFLECTION_QUIET_GATE_V1
                try:
                    # G5G_VERIFY_IN_NIGHTTICK_V1 — ověř fakta i v noční
                    # automatice (stejně jako /denik). Log-only (G5E).
                    try:
                        self._g5d_verify_day(today)
                    except Exception as _ve:
                        _log.warning('G5D: noční verifikace selhala (reflexe pokračuje): %s', _ve)
                    result = self._reflection.run()
                    if result:
                        self._last_reflection_date = today
                        self._save_routine_state()  # ROUTINE_STATE_PERSIST_V1
                        _log.info("Večerní reflexe: zapsána (%d znaků)",
                                  len(result))
                    else:
                        _log.info("Večerní reflexe: odložena "
                                  "(Ollama nedostupná, zkusím znovu)")
                except Exception as _e:
                    _log.error("Večerní reflexe selhala: %s", _e)

            # HANS_PLACE_V1 — smysl pro místo: zpracuj nové širší fotky místnosti
            # z drop-folderu data/room_photos/ na mentální mapu (qwen-VL, VRAM
            # tanec uvnitř). Idempotentní (sidecar) — když nic nového, vrátí 0
            # PŘED jakýmkoliv LLM voláním (levné). Gate night+quiet (jako art).
            if (datetime.now().hour >= self._night_hour and self._chat_quiet_ok()):
                try:
                    from scripts.hans_place import PlaceStore
                    _n = PlaceStore(self.config, self._diary_path).ingest_photos(self.config)
                    if _n:
                        _log.info("hans_place: zpracováno %d nových fotek místnosti", _n)
                except Exception as _pe:
                    _log.warning("hans_place: ingest fotek selhal: %s", _pe)

            # HANS_ART_WIRING_V1 — ve volné chvíli (v noci) namaluj obraz k
            # dočtené knize (1 obraz/knihu, SDXL přes ComfyUI). VRAM orchestrace
            # uvnitř (unload LLM → render → warm). Deferral-safe (ComfyUI dole →
            # retry příští noc). In-memory cooldown 30 min proti hammeru. Nikdy
            # neshodí tick. Běží každou noc (gen vrátí brzo, když nic k malování).
            if (self._in_night_window() and self._chat_quiet_ok()
                    and (time.time() - getattr(self, "_last_art_attempt", 0.0)) > 1800):
                self._last_art_attempt = time.time()
                _painted = False
                try:
                    from scripts.hans_art import generate_pending_artwork
                    _painted = generate_pending_artwork(self.config, self._diary_path)
                except Exception as _arte:
                    _log.warning("hans_art: noční render selhal: %s", _arte)
                # HANS_DREAMS_PER_DREAM_V1 — sen se maluje za KAŽDÝ nový sen (priorita,
                # mimo 2denní throttle; idempotence dle dream_ts + krátký odstup uvnitř).
                if not _painted:
                    try:
                        from scripts.hans_art import paint_dream
                        _painted = paint_dream(self.config, self._diary_path)
                    except Exception as _de:
                        _log.warning("hans_art: malování snu selhalo: %s", _de)
                # HANS_CREATIONS_V1 (Fáze 2) — když nebyl ani sen, Hans SÁM zváží den/úvahu
                # (2denní throttle + variace uvnitř creative_impulse).
                if not _painted:
                    try:
                        from scripts.hans_creations import creative_impulse
                        creative_impulse(self.config, self._diary_path)
                    except Exception as _de:
                        _log.warning("hans_creations: tvůrčí impuls selhal: %s", _de)

            # HANS_SEVERKA_V1 (3c) — týdenní check identity. Gate uvnitř
            # evaluate() drží, že navrhne jen při trvalé tendenci.
            # NIGHT_DEFERRAL_SAFE_V1 — guard NASTAV AŽ po ne-odloženém běhu
            # (dřív set-before → výpadek Ollamy zahodil check na CELÝ TÝDEN).
            if self._severka is not None and self._severka_due(today):
                try:
                    _sv_deferred = self._run_severka_check(today)
                    if not _sv_deferred:
                        self._last_severka_check = today
                        self._save_routine_state()
                except Exception as _se:
                    _log.error("Severka check selhal: %s", _se)
                # AVATAR_DESCRIPTOR_V1 — vzhled se posune s identitou (za Severkou,
                # čerstvý CORE). Uloží novou podobu jen když needs_rerender; render
                # (SDXL) je odd. úloha co dožene pending. Selhání nesmí shodit tick.
                try:
                    from scripts.avatar_descriptor import maybe_update_descriptor
                    maybe_update_descriptor(self.config, self._diary_path)
                except Exception as _ae:
                    _log.warning("avatar descriptor update selhal (Severka OK): %s", _ae)
                # AVATAR_RENDER_WIRING_V1 — dožeň render pending descriptoru (SDXL přes
                # ComfyUI). Běží v nočním ticku za Severkou (VRAM orchestrace uvnitř:
                # unload LLM → render → warm hans-czech). Deferral-safe, nikdy nehází.
                try:
                    from scripts.avatar_render import render_pending
                    render_pending(self.config, self._diary_path)
                except Exception as _re:
                    _log.warning("avatar render pending selhal (Severka OK): %s", _re)

            # AUTOBIOGRAPHICAL_NARRATIVE_V1 (krok 3) — týdenní narativní kapitola
            # (samostatná kadence, nezávislá na Severce). Base LLM, deferral-safe.
            # NIGHT_DEFERRAL_SAFE_V1 — guard NASTAV AŽ po úspěšném consolidate
            # (dřív set-before → výpadek Ollamy zahodil kapitolu na CELÝ TÝDEN).
            if self._narrative_due(today):
                try:
                    from scripts.hans_narrative import consolidate
                    _chap = consolidate(self.config, self._diary_path)
                    if _chap:
                        self._last_narrative = today
                        self._save_routine_state()
                        # NARRATIVE_RAG_UPLOAD_V1 — kapitola do RAG (identita)
                        if self._knowledge is not None:
                            try:
                                self._knowledge.upload(
                                    "hans_identita",
                                    "narrative_%d" % int(time.time()),
                                    "Kapitola životního příběhu (%s)" % today,
                                    _chap,
                                    metadata={"kdy": today,
                                              "typ": "narrative_chapter"})
                            except Exception as _ue:
                                _log.debug("narrative RAG upload: %s", _ue)
                    else:
                        _log.info("narrativní kapitola: odložena "
                                  "(Ollama nedostupná, zkusím znovu)")
                except Exception as _ne:
                    _log.warning("narrative konsolidace selhala: %s", _ne)

            # HANS_SYNTHESIS_IDEAS_V1 — within-tick guard: studium/autorství/synteze
            # jsou těžké LLM tasky; ať v JEDNOM ticku neběží víc než jeden (tick by
            # zbytečně dlouho visel). Kdo z nich fírne, zvedne flag a další počká
            # na příští tick.
            _creative_busy = False

            # HANS_STUDY_V1 — studijní program: 1 noční session = nastuduj další
            # pod-téma durable koníčku (Wikipedia → poznámka → deník+RAG). Po
            # dokončení kurikula mistrovská reflexe (grounduje vocational identitu).
            # Base LLM keep_alive=0 (VRAM tier), jen v noci. Deferral-safe:
            # 'deferred' (Ollama/wiki dole) → guard se NEnastaví, zkusí se znovu.
            if (self._last_study_date != today
                    and self._in_night_window()
                    and self._chat_quiet_ok()):
                _creative_busy = True
                try:
                    from scripts.hans_study import run_study_session
                    # diary_writer záměrně NEpředáváme: _diary_write píše do
                    # sloupce `note`, ale studijní poznámky musí do `data`
                    # (odkud je čte _gather_notes pro mistrovskou reflexi).
                    _scode = run_study_session(
                        self.config, self._diary_path,
                        knowledge=self._knowledge)
                    if _scode != "deferred":
                        self._last_study_date = today
                        self._save_routine_state()
                    _log.info("Studijní session: %s", _scode)
                except Exception as _stue:
                    _log.warning("Studijní session selhala: %s", _stue)

            # HANS_TOOLSCOUT_V1 — po dostudování domény (study_program completed)
            # navrhni nástroj (LLM) pro finální dílo. Idempotentní per téma
            # (has_for_topic). Lehké (1 search + krátký resident LLM), deferral-safe.
            if self._in_night_window() and self._chat_quiet_ok():
                try:
                    from scripts import hans_toolscout as _ts
                    if _ts.enabled(self.config):
                        _c = sqlite3.connect(self._diary_path, timeout=10)
                        _done = [r[0] for r in _c.execute(
                            "SELECT topic FROM study_program WHERE "
                            "status='completed'").fetchall()]
                        _c.close()
                        _store = _ts.ToolStore(self._diary_path)
                        for _tp in _done:
                            if _store.has_for_topic(_tp):
                                continue
                            _r = _ts.propose_tool(self.config, self._diary_path, _tp)
                            if _r.get("status") == "proposed" and self._notifier:
                                _top = (_r.get("proposals") or [{}])[0]
                                self._notifier(
                                    "Dostudoval jsem %s. Pro finální dílo navrhuji "
                                    "nástroj %s — mrkni na /nastroj." % (
                                        _tp, _top.get("tool_name", "?")))
                            _log.info("Toolscout '%s': %s", _tp, _r.get("status"))
                            break  # jeden návrh za noc
                except Exception as _tse:
                    _log.warning("Toolscout selhal: %s", _tse)

            # HANS_MAKER_V1 + HANS_STUDY_DEEPEN_V1 — spirála studium→dílo→kritika:
            # dostudované téma → vyrob artefakt pro aktuální kolo (B); až artefakt
            # je → kriticky prohluť studium o hlubší pod-témata (C, pod capem) →
            # znovu se nastuduje (jen NOVÉ) → příště lepší dílo. 1 těžký krok/noc.
            _mk_auto = (self.config.get("maker", {}) or {}).get("auto", True)
            if (_mk_auto and not _creative_busy and self._in_night_window()
                    and self._chat_quiet_ok()):
                try:
                    from scripts import hans_maker as _mk
                    _mc = sqlite3.connect(self._diary_path, timeout=10)
                    _mc.row_factory = sqlite3.Row
                    _comp = _mc.execute("SELECT topic, deepen_round FROM "
                                        "study_program WHERE status='completed'"
                                        ).fetchall()
                    _mc.close()
                    for _pr in _comp:
                        _tp = _pr["topic"]
                        _rnd = int(_pr["deepen_round"] or 0)
                        if not _mk.has_artifact_for_round(self._diary_path, _tp, _rnd):
                            # B) vyrob dílo pro aktuální kolo (těžké → jeden/noc)
                            _creative_busy = True
                            _res = _mk.make_from_study(self.config,
                                                       self._diary_path, _tp,
                                                       "coder", _rnd)
                            if _res.get("status") == "made" and self._notifier:
                                self._notifier("Z toho, co jsem nastudoval o %s, "
                                               "jsem vytvořil dílo." % _tp)
                            _log.info("Maker '%s' kolo %d: %s", _tp, _rnd,
                                      _res.get("status"))
                            break
                        else:
                            # C) dílo hotové → NAVRHNI prohloubení (kritika +
                            # hlubší témata), ulož jako pending a ZEPTEJ SE
                            # uživatele (ask-first). Aplikuje se až na schválení
                            # přes /prohloubit. HANS_STUDY_DEEPEN_V2.
                            from scripts.hans_study import StudyStore as _SS
                            _dres = _SS(self.config, self._diary_path
                                        ).create_deepen_proposal(self.config, _tp)
                            if _dres.get("status") == "proposed":
                                if self._notifier:
                                    _subs = "; ".join(_dres["subtopics"][:4])
                                    self._notifier(
                                        "Vytvořil jsem dílo o %s. Sám vidím, že "
                                        "mu chybí: %s Navrhuji doučit se: %s. Co "
                                        "na to říkáš? (/prohloubit schválit, "
                                        "/prohloubit <vlastní kritika>, nebo "
                                        "/prohloubit ne)" % (
                                            _tp, _dres.get("critique", ""), _subs))
                                _log.info("Deepen NÁVRH '%s' kolo %d (+%d, pending)",
                                          _tp, _dres["round"],
                                          len(_dres["subtopics"]))
                                break
                except Exception as _mke:
                    _log.warning("Maker/deepen selhal: %s", _mke)

            # HANS_AUTHORSHIP_V1 — autorský projekt: 1 noční session = napiš další
            # sekci díla na pokračování (grounded v RAG čtení/studia). Po dokončení
            # osnovy dovětek + složení do data/works/. Deferral-safe (deferred=retry).
            # Gate: jiná noc než studium PROBĚHLO (ať se nestřetnou 2 těžké LLM tasky
            # v jednu noc) — autorství běží, jen když studium dnes nebylo potřeba.
            if (not _creative_busy
                    and self._last_writing_date != today
                    and self._last_study_date == today
                    and self._in_night_window()
                    and self._chat_quiet_ok()):
                _creative_busy = True
                try:
                    from scripts.hans_authorship import run_writing_session
                    _wcode = run_writing_session(
                        self.config, self._diary_path, knowledge=self._knowledge)
                    if _wcode != "deferred":
                        self._last_writing_date = today
                        self._save_routine_state()
                    _log.info("Autorská session: %s", _wcode)
                except Exception as _aue:
                    _log.warning("Autorská session selhala: %s", _aue)

            # HANS_SYNTHESIS_IDEAS_V1 (#2) — vlastní nápady / synteze: propojí věci
            # z RŮZNÝCH oblastí (reading_takeaway/study_note za 30 dní) do JEDNOHO
            # nového postřehu (Hansův hlas, grounded). Kadence `cadence_days` (default
            # 3) — ne každou noc. Lehčí než studium (1 LLM volání), ale stejně těžké
            # na VRAM → within-tick guard, jen v noci, deferral-safe (deferred=retry).
            if (not _creative_busy
                    and self._synthesis_due(today)
                    and self._in_night_window()
                    and self._chat_quiet_ok()):
                _creative_busy = True
                try:
                    from scripts.hans_ideas import run_synthesis_session
                    _ycode = run_synthesis_session(
                        self.config, self._diary_path, knowledge=self._knowledge)
                    if _ycode != "deferred":
                        self._last_synthesis_date = today
                        self._save_routine_state()
                    _log.info("Synteze nápadů: %s", _ycode)
                except Exception as _yue:
                    _log.warning("Synteze nápadů selhala: %s", _yue)

            # HANS_SELFCRITIQUE_V1 (#6) — sebekritika z vlastního popudu: z Hansových
            # nedávných replik (human_chat/teddy_dialog) najde slabé místo KVALITY
            # projevu (rozvláčnost/opakování/fráze) a uloží ponaučení `self_critique`.
            # NEMĚNÍ paměť/postoje; příště to má v chat kontextu vedle korekčních lekcí.
            # Base LLM keep_alive=0, kadence `selfcritique.cadence_days` (default 2),
            # within-tick guard. Deferral-safe: 'deferred' (LLM dole) → guard se
            # NEnastaví, retry; 'idle'/'critiqued' (LLM běžel) → kadence drží odstup.
            if (not _creative_busy
                    and self._selfcritique_due(today)
                    and self._in_night_window()
                    and self._chat_quiet_ok()):
                _creative_busy = True
                try:
                    from scripts.hans_selfcritique import run_self_critique
                    _ccode = run_self_critique(self.config, self._diary_path)
                    if _ccode != "deferred":
                        self._last_selfcritique_date = today
                        self._save_routine_state()
                    _log.info("Sebekritika: %s", _ccode)
                except Exception as _cue:
                    _log.warning("Sebekritika selhala: %s", _cue)

            # HANS_IMMUNE_A2_V1 — imunitní systém: noční fact-check Hansových
            # VLASTNÍCH tvrzení („X je/byl Y") proti entity store (verbatim
            # glosy z jeho čtení). Rozpor → lesson_learned (surfacuje se
            # v chatu i Koláčově dialogu existujícím wiringem). NEMAŽE záznamy.
            # Lehké LLM (base, num_predict 8, max 6 volání), kadence
            # `immune.cadence_days` (default 1). Deferral-safe: 'deferred'
            # (LLM dole) → guard se NEnastaví → retry příští tick.
            if (not _creative_busy
                    and self._immune_due(today)
                    and self._in_night_window()
                    and self._chat_quiet_ok()):
                _creative_busy = True
                try:
                    from scripts.hans_immune import run_immune_check
                    _icode = run_immune_check(self.config, self._diary_path)
                    if _icode != "deferred":
                        self._last_immune_date = today
                        self._save_routine_state()
                    _log.info("Imunitní kontrola: %s", _icode)
                except Exception as _iue:
                    _log.warning("Imunitní kontrola selhala: %s", _iue)

            # HANS_DASHBOARD_PROPOSAL_V1 (Tier 1) — JEDNORÁZOVĚ po dostudování
            # Designu: Hans napíše designovou kritiku + návrh vlastní nástěnky
            # (grounding = fakta z šablony + jeho studijní poznámky) + SDXL
            # mockup. Gate uvnitř run_ (completed studium && žádný proposal).
            # Deferral-safe ('deferred' → retry příští tick), within-tick guard.
            if (not _creative_busy
                    and self._in_night_window()
                    and self._chat_quiet_ok()):
                try:
                    from scripts.hans_dashboard import run_dashboard_proposal
                    _dcode = run_dashboard_proposal(self.config, self._diary_path)
                    if _dcode == "proposed":
                        _creative_busy = True
                        _log.info("Návrh nástěnky: proposed")
                    elif _dcode == "deferred":
                        _log.info("Návrh nástěnky: deferred (retry)")
                except Exception as _dbe:
                    _log.warning("Návrh nástěnky selhal: %s", _dbe)

            # HANS_CAPABILITY_CURIOSITY_V1 — Hans si nově objevenou schopnost
            # ZVĚDAVĚ vyzkouší (u paint reálně namaluje) + reflexe „co svedu, co
            # jsem zjistil". Gate: čeká pending exploration. Deferral-safe.
            if (not _creative_busy
                    and self._in_night_window()
                    and self._chat_quiet_ok()):
                try:
                    from scripts.hans_capabilities import (
                        pending_explorations, explore_capability)
                    if pending_explorations(self._diary_path):
                        _ecode = explore_capability(self.config, self._diary_path)
                        if _ecode == "explored":
                            _creative_busy = True
                            _log.info("Zvědavost na schopnost: explored")
                        elif _ecode == "deferred":
                            _log.info("Zvědavost na schopnost: deferred (retry)")
                except Exception as _ece:
                    _log.warning("Zvědavost na schopnost selhala: %s", _ece)

            # HANS_MEMORY_HYGIENE_V1 (#2) — retenční prořez deníkového firehose
            # (person_seen/teddy_* starší než per-typ okno). Whitelist (smysluplné
            # eventy netknuté). Čistě SQL → rychlé, 1×/noc, gate night+quiet
            # (DELETE krátce zamkne diary DB — proto když je ticho).
            if (self._last_hygiene_date != today
                    and self._in_night_window()
                    and self._chat_quiet_ok()):
                self._last_hygiene_date = today
                self._save_routine_state()
                try:
                    from scripts.hans_memory_hygiene import prune_diary
                    _pruned = prune_diary(self.config, self._diary_path)
                    if _pruned:
                        _log.info("memory_hygiene: prořezáno %d řádků",
                                  sum(_pruned.values()))
                except Exception as _hye:
                    _log.warning("memory_hygiene prořez selhal: %s", _hye)

            # HANS_CREATION_REFLECTION_V1 (D) — týdně: reflexe vlastní tvorby
            # (sebepoznání, NE postoje). Samostatná kadence. Deferral-safe.
            try:
                _last_cr = self._last_creation_reflection
                _due_cr = True
                if _last_cr:
                    try:
                        _d0 = datetime.strptime(_last_cr, "%Y-%m-%d").date()
                        _d1 = datetime.strptime(today, "%Y-%m-%d").date()
                        _due_cr = (_d1 - _d0).days >= 7
                    except Exception:
                        _due_cr = True
                if (_due_cr and self._reflection is not None
                        and hasattr(self._reflection, 'reflect_on_creations')):
                    self._last_creation_reflection = today
                    self._save_routine_state()
                    self._reflection.reflect_on_creations()
            except Exception as _cre:
                _log.warning("creation reflection selhala: %s", _cre)

    def _synthesis_due(self, today: str) -> bool:
        """HANS_SYNTHESIS_IDEAS_V1 — kadenční guard: synteze ne každou noc,
        ale po `synthesis.cadence_days` (default 3). Prázdný guard = due."""
        last = self._last_synthesis_date
        if not last:
            return True
        try:
            cad = int(self.config.get("synthesis", {}).get("cadence_days", 3))
            d0 = datetime.strptime(last, "%Y-%m-%d").date()
            d1 = datetime.strptime(today, "%Y-%m-%d").date()
            return (d1 - d0).days >= cad
        except Exception:
            return True

    def _immune_due(self, today: str) -> bool:
        """HANS_IMMUNE_A2_V1 — kadenční guard: imunitní kontrola po
        `immune.cadence_days` (default 1). Prázdný guard = due."""
        last = self._last_immune_date
        if not last:
            return True
        try:
            cad = int(self.config.get("immune", {}).get("cadence_days", 1))
            d0 = datetime.strptime(last, "%Y-%m-%d").date()
            d1 = datetime.strptime(today, "%Y-%m-%d").date()
            return (d1 - d0).days >= cad
        except Exception:
            return True

    def _selfcritique_due(self, today: str) -> bool:
        """HANS_SELFCRITIQUE_V1 — kadenční guard: sebekritika po
        `selfcritique.cadence_days` (default 2). Prázdný guard = due."""
        last = self._last_selfcritique_date
        if not last:
            return True
        try:
            cad = int(self.config.get("selfcritique", {}).get("cadence_days", 2))
            d0 = datetime.strptime(last, "%Y-%m-%d").date()
            d1 = datetime.strptime(today, "%Y-%m-%d").date()
            return (d1 - d0).days >= cad
        except Exception:
            return True

    # ── Fáze dne ─────────────────────────────────────────────────────────────

    def _in_night_window(self) -> bool:
        """NIGHT_WINDOW_FULL_V1 — celá noční fáze (night_hour..morning_hour, tj.
        22:00–06:00), ne jen 2h před půlnocí. Pro práci, která NEtrpí 'tenkými daty
        nového dne' (art/hygiena/studium): restart po půlnoci ani rušné pre-midnight
        okno pak nestojí celou noc. Reflexe/narativ/tendence záměrně zůstávají
        premidnight (po 00:00 flipne datum → konfabulace z tenkých dat)."""
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

    def _morning_routine(self):
        """Ráno — počasí + co se stalo v noci."""
        # Počasí
        if self._weather:
            try:
                wx = self._weather.get_weather()
                if wx:
                    desc = wx.get("description", "")
                    temp = wx.get("temp_current")
                    wx_str = f"{desc}, {temp:.0f} C" if temp else desc
                    self._diary_write("morning_weather",
                                      "Ranní počasí", wx_str)
                    _log.info("Ranní počasí: %s", wx_str)
            except Exception as e:
                _log.debug("Weather error: %s", e)

    # ── Noční shrnutí ────────────────────────────────────────────────────────

    def _write_night_summary(self):
        """NIGHT_SUMMARY_REFLECTIVE_V1 — REFLEKTIVNÍ shrnutí dne (Hansovým hlasem,
        groundované ve faktech dne). Fallback na statistiku, když je Ollama dole
        (deferral-safe — shrnutí se neztratí)."""
        try:
            db = sqlite3.connect(self._diary_path)
            # NIGHT_SUMMARY_DATE_FIX_V1 — po půlnoci shrň KONČÍCÍ den (včera).
            _now = datetime.now()
            if _now.hour < self._morning_hour:
                today = datetime.fromtimestamp(_now.timestamp() - 86400).strftime("%Y-%m-%d")
            else:
                today = _now.strftime("%Y-%m-%d")

            def _q(sql):
                try:
                    return db.execute(sql, (today,)).fetchall()
                except Exception:
                    return []
            D = "date(ts,'unixepoch','localtime')=?"

            n_events = (_q(f"SELECT COUNT(*) FROM diary WHERE {D}") or [[0]])[0][0]
            n_dialogs = (_q(f"SELECT COUNT(*) FROM diary WHERE event_type='teddy_dialog' AND {D}") or [[0]])[0][0]
            types = _q(f"SELECT event_type, COUNT(*) FROM diary WHERE {D} "
                       "GROUP BY event_type ORDER BY COUNT(*) DESC LIMIT 5")
            reads = [r[0] for r in _q(f"SELECT DISTINCT title FROM diary WHERE event_type='web_read' AND {D} AND title<>'' ORDER BY ts DESC LIMIT 4")]
            people = [r[0] for r in _q(f"SELECT DISTINCT title FROM diary WHERE event_type='person_seen' AND {D} AND title NOT IN ('','Unknown','?')")]
            takeaways = [r[0] for r in _q(f"SELECT coalesce(data,note) FROM diary WHERE event_type='reading_takeaway' AND {D} AND coalesce(data,note)<>'' ORDER BY ts DESC LIMIT 2")]
            films = [r[0] for r in _q(f"SELECT DISTINCT title FROM diary WHERE event_type IN ('kodi_playing','movie_opinion') AND {D} AND title<>'' ORDER BY ts DESC LIMIT 3")]
            moments = [r[0] for r in _q(f"SELECT coalesce(NULLIF(note,''),data) FROM diary WHERE COALESCE(importance,0)>=6 AND {D} AND coalesce(NULLIF(note,''),data)<>'' AND event_type NOT IN ('human_chat','night_summary') ORDER BY importance DESC, ts DESC LIMIT 3")]
            db.close()

            stats = f"({n_events} událostí, {n_dialogs} dialogů s Kolačem)"
            facts = []
            if people:    facts.append("Dnes tu byli: " + ", ".join(dict.fromkeys(people)) + ".")
            if reads:     facts.append("Četl jsem: " + ", ".join(reads) + ".")
            if takeaways: facts.append("Z četby mě zaujalo: " + " / ".join(t.strip()[:180] for t in takeaways))
            if films:     facts.append("Na obrazovce běželo: " + ", ".join(films) + ".")
            if moments:   facts.append("Výrazné chvíle dne: " + " / ".join(m.strip()[:180] for m in moments))

            reflective = self._night_reflection(facts) if facts else None
            if reflective:
                summary = reflective + " " + stats
            else:
                type_str = ", ".join(f"{t}({n})" for t, n in types) if types else "nic"
                read_str = ", ".join(reads) if reads else "nic"
                summary = (f"Denní shrnutí: {n_events} událostí, {n_dialogs} dialogů "
                           f"s Kolačem. Typy: {type_str}. Četl: {read_str}.")

            self._diary_write("night_summary", "Shrnutí dne", summary)
            _log.info("Noční shrnutí (%s): %.100s",
                      "reflexe" if reflective else "statistika", summary)
        except Exception as e:
            _log.error("Night summary error: %s", e)

    def _night_reflection(self, facts) -> Optional[str]:
        """LLM ohlédnutí za dnem z faktů (Hansův hlas). None = Ollama dole / krátké
        → volající spadne na statistiku."""
        if not facts:
            return None
        try:
            from scripts.ollama_client import ollama_generate
            from scripts.hans_persona import persona_core
        except Exception:
            return None
        try:
            core = persona_core(self.config, with_address=False)
        except Exception:
            core = ""
        model = (self.config.get("models", {}) or {}).get("dialog", "hans-czech:latest")
        system = (core + "\n\n" if core else "") + (
            "Než usneš, ohlédni se do svého deníku za DNEŠNÍM dnem — krátká osobní "
            "reflexe (4-6 vět, první osoba, tvým hlasem). Souvislé ohlédnutí, ne výčet: "
            "co se dělo, kdo tu byl, co tě zaujalo, jak na tebe den působil. Vyjdi "
            "POUZE z faktů níže — nic si nepřimýšlej. Žádný nadpis, žádné uvozovky.")
        try:
            out = ollama_generate(
                model,
                "FAKTA DNEŠNÍHO DNE:\n" + "\n".join(facts) + "\n\nNapiš reflexi.",
                system=system, config=self.config, timeout=120)
        except Exception as e:
            _log.warning("night reflection LLM failed: %s", e)
            return None
        text = (out or "").strip().strip('"')
        return text[:1200] if len(text) >= 60 else None

    # ── Sny ──────────────────────────────────────────────────────────────────

    def _dream_fragments(self) -> str:
        """DREAM_LLM_V1 — sesbírá útržky dneška z deníku pro grounding snu."""
        import sqlite3 as _sql
        from datetime import datetime as _dt
        today = _dt.now().strftime("%Y-%m-%d")
        bits = []
        try:
            db = _sql.connect(self._diary_path)
            rows = db.execute(
                "SELECT title, COALESCE(NULLIF(note,''), data) FROM diary "
                "WHERE date(ts,'unixepoch','localtime')=? "
                "AND event_type IN ('movie_opinion','web_read','reading_takeaway',"
                "'human_chat','case_opened','case_closed','room_description',"
                "'introspection') "
                "AND (title<>'' OR note<>'' OR data<>'') "
                "ORDER BY RANDOM() LIMIT 6", (today,)).fetchall()
            bk = db.execute(
                "SELECT book_title, author FROM hans_library WHERE status='reading' "
                "ORDER BY started_at DESC LIMIT 1").fetchone()
            db.close()
            for t, n in rows:
                frag = (t or "").strip()
                if n:
                    frag = (frag + ": " + n.strip()) if frag else n.strip()
                if frag:
                    bits.append("- " + frag[:120])
            if bk and bk[0]:
                bits.append(f"- čte knihu {bk[0]}" + (f" od {bk[1]}" if bk[1] else ""))
        except Exception:
            return ""
        return "\n".join(bits[:6])

    def _write_dream(self):
        """DREAM_LLM_V1 — Hans 'sní' surreální sen GROUNDOVANÝ v dnešních zážitcích
        (LLM, vysoká teplota → varieta). Fallback na seed když LLM/data selžou
        (deferral-safe). Běží nočně 1×/den → hans-czech rezidentní (keep_alive def)."""
        import random
        dream = None
        try:
            frags = self._dream_fragments()
            if frags:
                from scripts.ollama_client import ollama_chat
                from scripts.hans_persona import persona_name
                cfg = self.config
                ow = cfg.get("openwebui_chat", {}) or {}
                model = (cfg.get("models", {}).get("dialog") or "hans-czech:latest")
                url = ow.get("base_url", "http://127.0.0.1:11434")
                nm = persona_name(cfg)
                system = (
                    f"Jsi {nm}, důstojný majordom. Napiš KRÁTKÝ surreální SEN "
                    "(1–2 věty, první osoba, česky), volně inspirovaný útržky z dneška. "
                    "Sen je symbolický a snový, NE doslovný popis dne. Žádné vysvětlování "
                    "ani úvod. Začni přirozeně, např. 'Zdálo se mi…' nebo 'V noci…'.")
                out = ollama_chat(
                    model,
                    [{"role": "system", "content": system},
                     {"role": "user", "content": "Útržky z dneška:\n" + frags}],
                    ollama_url=url,
                    options={"num_predict": 110, "temperature": 0.95})
                out = (out or "").strip().strip('"').strip()
                if out and len(out) > 15:
                    dream = out
        except Exception as e:
            _log.warning("LLM sen selhal, fallback seed: %s", e)
        if not dream:
            dream = random.choice(_DREAM_SEEDS)
        self._diary_write("dream", "Sen", dream)
        _log.info("Hans sní: %s", dream[:80])

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
