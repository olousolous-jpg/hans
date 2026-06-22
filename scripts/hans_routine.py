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
        # WOL_TIMER_THREAD_V1 — nezavisle na tick-loopu
        if self._wol_pc_enabled:
            import threading as _thr
            _thr.Thread(target=self._wol_timer_loop, daemon=True).start()
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
        except FileNotFoundError:
            pass
        except Exception as _e:
            _log.warning("routine_state: nacteni selhalo: %s", _e)

    def _save_routine_state(self):
        # ROUTINE_STATE_PERSIST_V1 - zapis guardy reflexi (prezije restart).
        try:
            with open(self._state_path, "w", encoding="utf-8") as f:
                json.dump({
                    "last_reflection_date": self._last_reflection_date,
                    "last_rel_reflection_date":
                        self._last_rel_reflection_date,
                    "last_severka_check": self._last_severka_check,
                    "last_narrative": self._last_narrative,
                }, f)
        except Exception as _e:
            _log.warning("routine_state: zapis selhal: %s", _e)

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

    def _run_severka_check(self, today: str):
        """Severčino rozhodnutí. Při návrhu vznikne pending verze (nic se
        neaplikuje) — uživatel ji uvidí přes /severka stav a schválí/zamítne."""
        res = self._severka.evaluate()
        d = res.get("decision")
        if d == "propose":
            _log.info("Severka: NÁVRH změny identity, čeká na schválení "
                      "(pending id=%s). Viz /severka.", res.get("version_id"))
        elif res.get("gate"):
            _log.info("Severka: gate prošel, drift malý → držím roli.")
        else:
            _log.info("Severka: žádná trvalá tendence (gate) → držím roli.")

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
        if not self._wol_pc_enabled or not self._wol_pc_mac:
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

    def _check_sleep_window(self):
        """Zavolat z tick(). Idempotentně přepíná _sleeping podle hodiny."""
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

        # SLEEP_MODE_V1 — spánek 02:00–09:00 (idempotentní přepnutí podle hodiny)
        self._check_sleep_window()

        new_phase = self._calc_phase()
        if new_phase != self._current_phase:
            old = self._current_phase
            self._current_phase = new_phase
            self._on_phase_change(old, new_phase)

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

            # HANS_ART_WIRING_V1 — ve volné chvíli (v noci) namaluj obraz k
            # dočtené knize (1 obraz/knihu, SDXL přes ComfyUI). VRAM orchestrace
            # uvnitř (unload LLM → render → warm). Deferral-safe (ComfyUI dole →
            # retry příští noc). In-memory cooldown 30 min proti hammeru. Nikdy
            # neshodí tick. Běží každou noc (gen vrátí brzo, když nic k malování).
            if (datetime.now().hour >= self._night_hour and self._chat_quiet_ok()
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
            if self._severka is not None and self._severka_due(today):
                self._last_severka_check = today
                self._save_routine_state()
                try:
                    self._run_severka_check(today)
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
            if self._narrative_due(today):
                self._last_narrative = today
                self._save_routine_state()
                try:
                    from scripts.hans_narrative import consolidate
                    _chap = consolidate(self.config, self._diary_path)
                    # NARRATIVE_RAG_UPLOAD_V1 — kapitola do RAG (identita)
                    if _chap and self._knowledge is not None:
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
                except Exception as _ne:
                    _log.warning("narrative konsolidace selhala: %s", _ne)

    # ── Fáze dne ─────────────────────────────────────────────────────────────

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
        """Shrne den do deníku — co se stalo, kdo přišel, co Hans četl."""
        try:
            db = sqlite3.connect(self._diary_path)
            # NIGHT_SUMMARY_DATE_FIX_V1 — po půlnoci (noční okno) shrň KONČÍCÍ den
            # (včera), ne nový prázdný kalendářní den. Jinak souhrn v 00:0x hlásil
            # „1 událostí, Četl: nic".
            _now = datetime.now()
            if _now.hour < self._morning_hour:
                today = datetime.fromtimestamp(_now.timestamp() - 86400).strftime("%Y-%m-%d")
            else:
                today = _now.strftime("%Y-%m-%d")

            # Počet událostí ve shrnovaném dni
            n_events = db.execute(
                "SELECT COUNT(*) FROM diary WHERE date(ts, 'unixepoch', 'localtime') = ?",
                (today,)).fetchone()[0]

            # Jaké typy událostí
            types = db.execute(
                "SELECT event_type, COUNT(*) FROM diary "
                "WHERE date(ts, 'unixepoch', 'localtime') = ? "
                "GROUP BY event_type ORDER BY COUNT(*) DESC LIMIT 5",
                (today,)).fetchall()

            # Dialogy dnes
            n_dialogs = db.execute(
                "SELECT COUNT(*) FROM diary "
                "WHERE event_type='teddy_dialog' "
                "AND date(ts, 'unixepoch', 'localtime') = ?",
                (today,)).fetchone()[0]

            # Co Hans četl
            reads = db.execute(
                "SELECT title FROM diary "
                "WHERE event_type='web_read' "
                "AND date(ts, 'unixepoch', 'localtime') = ? "
                "ORDER BY ts DESC LIMIT 3",
                (today,)).fetchall()

            db.close()

            type_str = ", ".join(f"{t}({n})" for t, n in types) if types else "nic"
            read_str = ", ".join(r[0] for r in reads) if reads else "nic"

            summary = (f"Denní shrnutí: {n_events} událostí, "
                       f"{n_dialogs} dialogů s Kolačem. "
                       f"Typy: {type_str}. "
                       f"Četl: {read_str}.")

            self._diary_write("night_summary", "Shrnutí dne", summary)
            _log.info("Noční shrnutí: %s", summary[:100])

        except Exception as e:
            _log.error("Night summary error: %s", e)

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
