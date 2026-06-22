from __future__ import annotations

import logging
import queue
import re
import sqlite3
import threading
import time
from datetime import datetime
from typing import Callable, Optional

import json
import os

import requests

_log = logging.getLogger(__name__)


# PENDING_THOUGHTS_V1 — signal "LLM nedostupny, dozen pozdeji"
class LLMOffline(Exception):
    pass


_PENDING_DIR = "data/pending_thoughts"


# Specifický pokyn pro každý "styl" syntézy.
_STYLE_PROMPTS: dict[str, str] = {
    "encounter_summary": (
        "Dostaneš fakta o jednom "
        "setkání s osobou (co se dělo, o čem se mluvilo). Shrň to do JEDNÉ "
        "věcné věty ve třetí osobě, jako záznam do tvého deníku setkání. "
        "Bez oslovení, bez ozdob. Např. 'Standa strávil večer u filmu a "
        "zmínil zájem o sopky.' Pokud fakta nestačí, napiš stručně co víš."
    ),
    "movie_opinion": (  # MOVIE_GROUNDING_V1
        "Máš britskou rezervovanost a smysl pro detail. V domě právě běželo "
        "toto — TY jsi to neviděl. Pokud je "
        "v podkladu DĚJ/synopse, vyjdi z něj: napiš 1-3 stručné věty v první "
        "osobě o tom, co tě na příběhu či námětu zaujalo nebo k čemu tě "
        "přivádí. Stav na tom, co o ději víš, ne na předstíraném sledování. "
        "Když děj k dispozici NENÍ (jen titul/žánr), přiznej stručně, že o "
        "tom víš málo, a obsah si NEVYMÝŠLEJ. Konkrétně, bez emoji."
    ),
    "reading_takeaway": (
        "Máš zájem o vědění. Právě jsi přečetl "
        "článek. Napiš 1-2 věty v první osobě — co sis odnesl, co tě "
        "překvapilo, nebo nad čím přemýšlíš. Konkrétně, ne obecně. "
        "Bez úvodních frází. Bez emoji."
    ),
    "book_reflection": (
        "Máš lásku k literatuře. Právě jsi "
        "přečetl kapitolu knihy. Napiš 1-2 věty v první osobě o tom "
        "co tě v této kapitole oslovilo. Žádné převyprávění děje. "
        "Drobné pozorování, postava, věta. Bez emoji."
    ),
    "book_completion": (  # HANS_BOOK_COMPLETION_STYLE_V1
        "Máš hlubokou lásku k literatuře. Právě jsi DOČETL celou knihu — "
        "dostáváš své vlastní poznámky z jednotlivých kapitol. Napiš souvislé "
        "OHLÉDNUTÍ za celou knihou v první osobě, 4-6 vět, klidným a přemýšlivým "
        "tónem: co v tobě zůstává, která postava, myšlenka nebo věta tě nejvíc "
        "oslovila nebo naopak dráždila, a — pokud to v tobě kniha vyvolala — zda "
        "a jak potvrdila nebo proměnila tvůj pohled na svět či člověka. Není to "
        "převyprávění děje; je to osobní zápis o tom, co kniha udělala s TEBOU. "
        "Smíš vyslovit trvalejší názor, který v tobě kniha SKUTEČNĚ zanechala, "
        "ale NEVYMÝŠLEJ si přesvědčení, postavy ani události, které v poznámkách "
        "nejsou. Bez nadpisů, bez odrážek, bez emoji."
    ),
    "relationship_reflection": (
        """Máš britskou rezervovanost. Aktualizuješ si soukromý zápisník
        o jednom z obyvatel domu.

        DŮLEŽITÝ KONTEXT — přečti pozorně:
        - Koláč je TVŮJ FIKTIVNÍ detektivní společník. Mluvíš s ním
          jen ty, ve svých vnitřních úvahách. Osoby v domě
          (členové domácnosti) Koláče NEZNAJÍ a nikdy s ním nemluví.
        - Když ve vstupu uvidíš sekci 'MOJE VNITŘNÍ ÚVAHY', jsou to
          TVOJE vlastní myšlenky o té osobě — ne věci, které ona řekla.
        - Z pozorování kamerou víš jen: kdy jsi osobu viděl. Nevíš,
          co dělala mimo zorné pole, co říkala, co si myslela.
          NEVYMÝŠLEJ si její chování ani místnosti, kde byla.

        Dostaneš:
        - ZÁKLADNÍ INFO o osobě (jméno, role, rodinné vazby).
        - PŘEDCHOZÍ POZNÁMKA (může být prázdná, pokud píšeš poprvé).
        - POZOROVÁNÍ (kolikrát jsi osobu viděl, v jakých časech).
        - MOJE VNITŘNÍ ÚVAHY (pokud existují — co jsem si o ní myslel
          v rámci vyšetřování s Koláčem nebo jinak).
        - NAŠE ROZHOVORY (pokud existují — skutečné chat výměny mezi
          mnou a touto osobou). TOTO JE NEJBOHATŠÍ ZDROJ — slova,
          která osoba reálně řekla. Co se zajímá, jaký má styl, co
          se právě teď řeší v jejím životě.

        PRAVIDLA:
        1) Napiš 3-4 věty prozaicky, první osoba, klidný rezervovaný
        tón. Žádné nadpisy, odrážky, hranaté závorky.
        2) Toto je tvůj OSOBNÍ zápisník — ne wiki článek. Pozorování,
        ne výčet faktů.
        3) Pokud existuje PŘEDCHOZÍ POZNÁMKA, NAVAZUJ — neresetuj.
        Můžeš revidovat dojem, doplnit nový postřeh, ale držíš
        kontinuitu.
        4) Mluv pouze o věcech ze vstupu. NEVYMÝŠLEJ události,
        místa, rozhovory, zájmy ani aktivity, které tam nejsou.
        Když nevíš, neříkej.
        5) Pokud čerpáš z 'MOJE VNITŘNÍ ÚVAHY', formuluj to jako
        TVOJE myšlenky — 'napadlo mě, že...', 'přemýšlel jsem o ní
        v souvislosti s...' — NIKDY jako 'mluvila o...', 'bavila se
        s Koláčem...', 'zajímá se o...'.
        5b) Pokud čerpáš z 'NAŠE ROZHOVORY', JE legitimní napsat
        'řekla mi, že...', 'zmínila...', 'zajímá se o...' — protože
        tohle jsou skutečná její slova. Ale stále nevymýšlej věci,
        které tam nejsou.
        6) Můžeš osobu jemně zhodnotit (vlídně, s respektem), ale
        nedělej z toho rozsudek. Jsi pozorovatel, ne soudce.
        7) Bez emoji, bez citací, bez oslovení."""
    ),
    "evening_reflection": (
        """Máš britskou rezervovanost a smysl pro detail. Je večer a píšeš si 
        soukromý zápis do deníku o uplynulém dni.

        Dostaneš seznam UDÁLOSTÍ DNE rozdělených podle kategorií 
        v hranatých závorkách (např. [Dialogy s Koláčem], 
        [Co jsem četl], [Co se hrálo na TV]). Každá odrážka je 
        konkrétní událost.

        PRAVIDLA:
        1) Napiš souvislý prozaický text — 3-5 odstavců, plynulý 
        deníkový zápis. ŽÁDNÉ nadpisy, hranaté závorky ani odrážky 
        ve výstupu.
        2) Pokrytí: každá kategorie ze vstupu si zaslouží alespoň 
        zmínku. Nezacyklit se v jedné.
        3) FAKTA: mluvíš pouze o věcech, které jsou ve vstupu. 
        Nevymýšlíš si názvy knih, filmů, čísla, počasí. Když 
        ve vstupu není kniha, nečetl jsi ji.
        4) Hlas: první osoba, vlastní úvahy, klidný rezervovaný tón. 
        K faktům smíš přidat svůj POCIT/NÁZOR (co tě zaujalo, 
        k čemu se v duchu vracíš), ale ne nová fakta.
        5) Nikoho neoslovuj jménem ani titulem — píšeš si pro sebe.
        6) Nezačínej pozdravem ani frází 'dnes byl den'. Začni 
        rovnou věcně. Bez emoji, bez citací typu [1]."""
    ),
    "dialog_reflection": (
        "Máš britskou rezervovanost. Právě "
        "jsi měl rozhovor s panem Koláčem (medvídkem-detektivem), tvým "
        "společníkem v domě. Napiš 1-3 věty v první osobě jako tichou "
        "myšlenku PO rozhovoru — ne shrnutí dialogu, ale to, co ti "
        "zůstalo v hlavě, co tě zaujalo na jeho reakci, co tě napadá. "
        "Konkrétní pozorování. Nikoho neoslovuj jménem. Bez emoji."
    ),
    "case_thought": (
        "Pomáháš Koláčovi s případy. "
        "Právě byl otevřen nový případ. Napiš 1-2 věty v první osobě "
        "o tom, co tě na případu zaujalo nebo k čemu se v duchu chceš "
        "vrátit. Konkrétně, ne obecně. Bez úvodních frází. "
        "Bez emoji, nikoho neoslovuj."
    ),
    "case_resolution": (
        "Pomáháš Koláčovi s případy. "
        "Dostaneš subjekt případu a seznam stop. Udělej z nich "
        "krátký a věcný závěr vyšetřování (2-4 věty, první osoba): "
        "co je podstata věci, které stopy spolu souvisí a jaké "
        "rozuzlení z nich plyne. Stopy, které k subjektu nepatří, "
        "ignoruj a nevypisuj. Když si něčím nejsi jistý, přiznej to. "
        "Konkrétně, bez vaty. "
        "Bez emoji, nikoho neoslovuj jménem."
    ),
    # # CHAT_REFLECTION_STYLE
    "chat_reflection": (
        "Napiš 1-2 věty v první osobě o tom, "
        "co sis odnesl z rozhovoru s touto osobou. Co se ti zdálo důležité, "
        "co tě zaujalo, jaký mám pocit z toho člověka. Mluv česky, krátce, "
        "věcně, bez emoji. Ne shrnutí dialogu, ale tvůj dojem."
    ),
    "default": (
        "Napiš 1-2 věty v první osobě "
        "k tématu které ti bude předloženo. Stručně, konkrétně, bez "
        "úvodních frází. Bez emoji."
    ),
}


class HansSynthesis:
    """Generuje krátké osobní názory přes OpenWebUI chat endpoint."""

    def __init__(self, config: dict):
        self._config = config  # PERSONA_REFACTOR_7_8 — příprava na plné napojení
        ow = config.get("openwebui_direct", {}) or {}
        self._base_url: str = ow.get("base_url", "http://localhost:8080").rstrip("/")
        self._token: str = ow.get("api_token", "")
        self._model: str = ow.get("synthesis_model") or ow.get("model", "hans-czech:latest")
        self._timeout: int = int(ow.get("synthesis_timeout", 120))  # OLLAMA_CLIENT_PATCH_SYNTHESIS_TIMEOUT
        self._lock = threading.Lock()
        self._min_gap_s: float = 5.0
        self._last_call_ts: float = 0.0

        if not self._token:
            _log.warning("HansSynthesis: chybí api_token, syntéza nebude fungovat")

    def synthesize(
        self,
        topic: str,
        facts: str,
        style: str = "default",
        max_tokens: int = 200,
        max_chars: int = 500,
        facts_max_chars: int = 600,
        raise_offline: bool = False,  # PENDING_THOUGHTS_V1
    ) -> Optional[str]:
        if not self._token:
            return None
        if not facts or not facts.strip():
            return None

        # PERSONA_REFACTOR_9_B — CORE identita z configu (jedno místo pro Severku)
        from scripts.hans_persona import persona_core
        _base = persona_core(self._config, with_address=False)
        _mod = _STYLE_PROMPTS.get(style, _STYLE_PROMPTS["default"])
        system_prompt = f"{_base}\n\n{_mod}" if _base else _mod
        user_msg = f"Téma: {topic}\nFakta: {facts.strip()[:facts_max_chars]}"

        with self._lock:
            elapsed = time.time() - self._last_call_ts
            if elapsed < self._min_gap_s:
                time.sleep(self._min_gap_s - elapsed)

            try:
                text = self._call_llm(system_prompt, user_msg, max_tokens)
            except LLMOffline:  # PENDING_THOUGHTS_V1
                if raise_offline:
                    raise
                return None
            except Exception as e:
                _log.debug("synthesize(%s) failed: %s", style, e)
                return None
            finally:
                self._last_call_ts = time.time()

        if not text:
            return None
        text = text.strip()
        # PERSONA_NAME_CONFIGURABLE_V1 — strhni label s nakonfigurovaným jménem
        from scripts.hans_persona import persona_name as _pn
        _nm = _pn(self._config)
        for prefix in (f"{_nm}:", f"{_nm} —", "—", '"', "'"):
            if text.startswith(prefix):
                text = text[len(prefix):].lstrip()
        for suffix in ('"', "'"):
            if text.endswith(suffix):
                text = text[:-1].rstrip()
        if len(text) > max_chars:
            text = text[:max_chars - 3] + "…"
        return text or None

    def _call_llm(
        self, system_prompt: str, user_msg: str, max_tokens: int
    ) -> Optional[str]:
        url = f"{self._base_url}/api/chat/completions"
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_msg},
            ],
            "stream": False,
            "max_tokens": max_tokens,
            "temperature": 0.55,
        }
        try:
            r = requests.post(url, headers=headers, json=payload,
                              timeout=self._timeout)
        except (requests.exceptions.ConnectionError,
                requests.exceptions.Timeout) as e:  # PENDING_THOUGHTS_V1
            raise LLMOffline(str(e)) from e
        if r.status_code != 200:
            _log.debug("synthesis HTTP %d: %s", r.status_code, r.text[:200])
            return None
        try:
            data = r.json()
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, ValueError) as e:
            _log.debug("synthesis bad response: %s", e)
            return None


# ═══════════════════════════════════════════════════════════════════
# SYNTHESIS_MERGED_V1 — Hooks worker (původně hans_synthesis_hooks.py)
# ═══════════════════════════════════════════════════════════════════



# Mapping event_type → konfigurace synthesis
# style:           klíč v _STYLE_PROMPTS v hans_synthesis.py
# topic:           lidský label předávaný do synthesize() (jen pro prompt)
# collection:      RAG kolekce kam se výsledek pushne
# diary_event:     event_type pro zápis Hansovy reflexe do deníku
# doc_id_prefix:   prefix pro doc_id v RAGu
# min_gap_s:       minimální mezera mezi syntézami stejného typu
# build_facts_fn:  jak z (title, note) sestavit facts pro synthesis
_HOOKS: dict[str, dict] = {
    "web_read": {
        "style":          "reading_takeaway",
        "topic":          "článek",
        "collection":     "hans_cetba",
        "diary_event":    "reading_takeaway",
        "doc_id_prefix":  "web",
        "min_gap_s":      120,
    },
    "kodi_playing": {
        "style":          "movie_opinion",
        "topic":          "film",
        "collection":     "hans_filmy",
        "diary_event":    "movie_opinion",
        "doc_id_prefix":  "movie",
        "min_gap_s":      300,
    },
    "teddy_dialog": {
        "style":          "dialog_reflection",
        "topic":          "rozhovor s Koláčem",
        "collection":     "hans_pripady",
        "diary_event":    "dialog_reflection",
        "doc_id_prefix":  "dialog",
        "min_gap_s":      0,
        "debounce_s":     300,  # TEDDY_DEBOUNCE_300_V1 — bylo 100; pauzy >100s štěpily konverzaci
    },
    "human_chat": {
        # HUMAN_CHAT_HOOK
        # G5A_STOP_IDENTITY_CONTAMINATION_V1 — přesměrováno hans_identita→hans_pripady,
        # ať konfabulace z rozhovorů nekontaminuje faktické vztahové karty.
        "style":          "chat_reflection",
        "topic":          "rozhovor s osobou",
        "collection":     "hans_pripady",
        "diary_event":    "chat_reflection",
        "doc_id_prefix":  "chat",
        "min_gap_s":      0,
        "debounce_s":     300,  # 5 min — počkej až konverzace utichne
    },
    "case_opened": {
        "style":          "case_thought",
        "topic":          "nový případ",
        "collection":     "hans_pripady",
        "diary_event":    "case_thought",
        "doc_id_prefix":  "case_open",
        "min_gap_s":      0,
    },
    "case_closed": {
        "style":          "case_resolution",
        "topic":          "uzavřený případ",
        "collection":     "hans_pripady",
        "diary_event":    "case_resolution",
        "doc_id_prefix":  "case_close",
        "min_gap_s":      0,
    },
    "book_read": {
        "style":          "book_reflection",
        "topic":          "kapitola knihy",
        "collection":     "hans_cetba",
        "diary_event":    "book_reflection",
        "doc_id_prefix":  "book",
        "min_gap_s":      300,
    },
}


def _safe_id(s: str, max_len: int = 60) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "_", s)[:max_len] or "doc"


class HansSynthesisHooks:
    """Asynchronní worker pro post-processing zápisů do deníku."""

    def __init__(
        self,
        synthesis,                 # HansSynthesis instance
        knowledge,                 # HansKnowledge instance (může být None)
        diary_writer: Callable,    # callable(event_type, title, note) — zápis do deníku
        max_queue: int = 100,
        config: dict | None = None,  # G5B: pro Relationships (detekce rozporu)
    ):
        self._synthesis = synthesis
        self._knowledge = knowledge
        self._diary_writer = diary_writer
        # G5B_DETECT_CONTRADICTION_V1 — lazy karty pro detekci rozporu
        self._g5b_config = config
        self._g5b_rels = None
        self._g5b_rels_tried = False
        self._queue: queue.Queue = queue.Queue(maxsize=max_queue)
        self._stop = threading.Event()
        self._last_synthesis_ts: dict[str, float] = {}  # per event_type
        self._last_drain_ts: float = 0.0  # PENDING_THOUGHTS_DRAIN_V1 (0=hned)
        self._thread = threading.Thread(
            target=self._worker, name="HansSynthesisHooks", daemon=True
        )
        # Debounce: per event_type buffer + last_arrival_ts
        # struktura: {event_type: {"items": [item, ...], "last_ts": float}}
        self._debounce_buffers: dict[str, dict] = {}
        self._debounce_lock = threading.Lock()
        self._debounce_thread = threading.Thread(
            target=self._debounce_loop,
            name="HansSynthesisHooksDebounce",
            daemon=True,
        )

    # ── Public API ───────────────────────────────────────────────────────────

    def start(self):
        if self._synthesis is None:
            _log.warning("HansSynthesisHooks: synthesis je None, nestartuji")
            return
        self._thread.start()
        self._debounce_thread.start()
        _log.info("HansSynthesisHooks: worker spuštěn (%d event types)",
                  len(_HOOKS))

    def stop(self):
        self._stop.set()
        # Flush všechny pending debounce buffery (nezahodit nasbíraná data)
        try:
            with self._debounce_lock:
                pending = list(self._debounce_buffers.keys())
            for evt in pending:
                self._flush_debounce_buffer(evt)
        except Exception:
            pass
        try:
            self._queue.put_nowait(None)  # poison pill
        except queue.Full:
            pass

    def enqueue(self, event_type: str, title: str, note: str):
        """Volá se z hans_idle._log_entry. Neblokuje."""
        if event_type not in _HOOKS:
            return
        if not (title or note):
            return
        item = {
            "event_type": event_type,
            "title": title or "",
            "note":  note or "",
            "ts":    time.time(),
        }
        cfg = _HOOKS[event_type]
        debounce_s = cfg.get("debounce_s", 0)
        if debounce_s > 0:
            # Debounced — uložit do bufferu, čeká se na ticho
            with self._debounce_lock:
                buf = self._debounce_buffers.setdefault(
                    event_type, {"items": [], "last_ts": 0.0})
                buf["items"].append(item)
                buf["last_ts"] = item["ts"]
            _log.debug("Debounce %s: buffer %d položek",
                       event_type, len(buf["items"]))
            return
        # Standardní cesta — rovnou do fronty
        try:
            self._queue.put_nowait(item)
        except queue.Full:
            _log.warning("synthesis hook queue plná, drop %s", event_type)

    # ── Worker ──────────────────────────────────────────────────────────────

    def _debounce_loop(self):
        """Periodicky kontroluje debounce buffery — pokud uběhl
        debounce_s od posledního zápisu, sloučí buffer a pošle do fronty."""
        while not self._stop.is_set():
            time.sleep(5.0)  # check every 5s
            now = time.time()
            to_flush: list[str] = []
            with self._debounce_lock:
                for evt, buf in self._debounce_buffers.items():
                    if not buf["items"]:
                        continue
                    cfg = _HOOKS.get(evt, {})
                    debounce_s = cfg.get("debounce_s", 0)
                    if now - buf["last_ts"] >= debounce_s:
                        to_flush.append(evt)
            for evt in to_flush:
                self._flush_debounce_buffer(evt)
        _log.info("HansSynthesisHooks: debounce loop ukončen")

    def _flush_debounce_buffer(self, event_type: str):
        """TEDDY_TOPIC_AGGREGATION_V1 — vezme položky z bufferu, SESKUPÍ je podle
        tématu ('Téma: …' z note) a pošle JEDEN agregovaný item PER TÉMA. Konverzace
        o jednom subjektu se slije do jednoho souhrnu, ale různá témata (i nahromaděná
        při výpadku Ollamy, kdy buffer nestihl utichnout) se nezkolabují do jednoho."""
        with self._debounce_lock:
            buf = self._debounce_buffers.get(event_type)
            if not buf or not buf["items"]:
                return
            items = buf["items"]
            buf["items"] = []
            buf["last_ts"] = 0.0

        # Seskup podle tématu, zachovej pořadí prvního výskytu.
        # Bez tématu (human_chat apod.) → jedna skupina "" = původní chování.
        groups: dict = {}
        for it in items:
            key = self._topic_key(it.get("note", ""))
            groups.setdefault(key, []).append(it)

        _max_n = int(((self._g5b_config or {}).get("hans_dialog", {})
                      or {}).get("agg_max_items", 10))
        for _key, gitems in groups.items():
            for i in range(0, len(gitems), max(1, _max_n)):  # safety: velkou skupinu po kusech
                self._emit_agg(event_type, gitems[i:i + _max_n])

    @staticmethod
    def _topic_key(note: str) -> str:
        """'Téma: X' z prvního řádku note → 'x' (klíč grupování). Jinak ''."""
        first = (note or "").split("\n", 1)[0].strip()
        low = first.lower()
        if low.startswith("téma:") or low.startswith("tema:"):
            return first.split(":", 1)[1].strip().lower()
        return ""

    def _emit_agg(self, event_type: str, items: list):
        """Slij skupinu (jedno téma) do agregovaného itemu → fronta."""
        if not items:
            return
        n = len(items)
        combined_title = next((it["title"] for it in items if it["title"]), "")
        combined_note = "\n\n".join(it["note"] for it in items if it["note"])
        ts_first = items[0]["ts"]
        ts_last = items[-1]["ts"]
        _log.info("Debounce flush %s: %d položek, téma=%r (%.0fs)",
                  event_type, n, self._topic_key(items[0].get("note", "")),
                  ts_last - ts_first)
        agg_item = {
            "event_type": event_type,
            "title": combined_title,
            "note": combined_note,
            "ts": ts_last,
            "_aggregate_count": n,
        }
        try:
            self._queue.put_nowait(agg_item)
        except queue.Full:
            _log.warning("queue plná při flush %s", event_type)

    def _worker(self):
        while not self._stop.is_set():
            try:
                item = self._queue.get(timeout=1.0)
            except queue.Empty:
                self._maybe_drain_pending()  # PENDING_THOUGHTS_DRAIN_V1
                continue
            if item is None:
                break
            try:
                self._process(item)
            except Exception as e:
                _log.warning("synthesis hook error: %s", e)
            finally:
                self._queue.task_done()
        _log.info("HansSynthesisHooks: worker ukončen")

    def _maybe_drain_pending(self, throttle_s: float = 60.0):
        """PENDING_THOUGHTS_DRAIN_V1 (B-3) — max 1x/throttle_s zkus
        dohnat odlozene offline myslenky z disku."""
        now = time.time()
        if now - self._last_drain_ts < throttle_s:
            return
        self._last_drain_ts = now
        self._drain_pending()

    def _drain_pending(self):
        """Projde pending_thoughts/ a zkusi kazdy item znovu.
        Uspech -> smaz. LLM stale offline -> nech soubor a prerus.
        Posktozeny/nezpracovatelny -> smaz (at se fronta neucpe)."""
        try:
            files = sorted(
                f for f in os.listdir(_PENDING_DIR) if f.endswith(".json")
            )
        except FileNotFoundError:
            return
        except Exception as e:
            _log.warning("drain: listdir selhal: %s", e)
            return
        if not files:
            return
        done = 0
        for fname in files:
            if self._stop.is_set():
                break
            path = os.path.join(_PENDING_DIR, fname)
            try:
                with open(path, encoding="utf-8") as f:
                    item = json.load(f)
            except Exception as e:
                _log.warning("drain: %s necitelny, mazu: %s", fname, e)
                self._safe_unlink(path)
                continue
            try:
                self._process(item, from_pending=True)
            except LLMOffline:
                if done:
                    _log.info("drain: doplneno %d, LLM zase offline, "
                              "zbytek pocka", done)
                return  # PC zjevne spi — prerus
            except Exception as e:
                _log.warning("drain: %s nezpracovatelny, mazu: %s", fname, e)
                self._safe_unlink(path)
                continue
            self._safe_unlink(path)
            done += 1
        if done:
            _log.info("drain: doplneno %d odlozenych myslenek", done)

    @staticmethod
    def _safe_unlink(path):
        try:
            os.remove(path)
        except OSError:
            pass


    def _prior_book_notes(self, title, limit=3, max_chars=170):
        # BOOK_CONSISTENCY_V1 - drivejsi poznamky k teze knize (read-only).
        # Klic knihy = cast titulu pred kapitolou. Fail-safe: chyba -> "".
        cfg = self._g5b_config or {}
        db = cfg.get("diary_db")
        if not db:
            return ""
        key = re.split(r"\s*[—–-]\s*kap\.?", title or "")[0].strip()
        if not key or key == (title or "").strip():
            return ""
        try:
            con = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
            try:
                rows = con.execute(
                    "SELECT title, data FROM diary "
                    "WHERE event_type = 'book_reflection' "
                    "AND title LIKE ? ORDER BY ts ASC",
                    (key + "%",),
                ).fetchall()
            finally:
                con.close()
        except Exception as _e:
            _log.warning("BOOK_CONSISTENCY: cteni deniku selhalo: %s", _e)
            return ""
        notes = []
        for _t, _d in rows:
            _k = re.split(r"\s*[—–-]\s*kap\.?", _t or "")[0].strip()
            if _k != key:
                continue
            _d = (_d or "").strip()
            if not _d:
                continue
            if len(_d) > max_chars:
                _d = _d[:max_chars].rstrip() + "..."
            notes.append("- %s: %s" % (_t, _d))
        if not notes:
            return ""
        notes = notes[-limit:]
        return ("Tvé dřívější poznámky k téže knize "
                "(navaž na ně, drž konzistentní úsudek):\n"
                + "\n".join(notes))

    def _queue_pending(self, item: dict):
        """PENDING_THOUGHTS_V1 — uloz offline item na disk (atomicky).
        Dozene se po nabehnuti PC pres drain (B-3)."""
        try:
            os.makedirs(_PENDING_DIR, exist_ok=True)
            fname = "%d_%s.json" % (
                int(item.get("ts", time.time()) * 1000),
                _safe_id(item.get("event_type", "evt"), 20),
            )
            path = os.path.join(_PENDING_DIR, fname)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(item, f, ensure_ascii=False)
            os.replace(tmp, path)  # atomicky — drain nikdy necte pulku
        except Exception as e:
            _log.warning("queue_pending selhalo: %s", e)

    def _process(self, item: dict, from_pending: bool = False):  # PENDING_THOUGHTS_DRAIN_V1
        evt = item["event_type"]
        cfg = _HOOKS.get(evt)
        if not cfg:
            return

        # Throttling per event_type
        gap = cfg["min_gap_s"]
        if gap > 0 and not from_pending:  # PENDING_THOUGHTS_DRAIN_V1
            last = self._last_synthesis_ts.get(evt, 0)
            elapsed = time.time() - last
            if elapsed < gap:
                _log.debug("Hook %s skipped (gap %.0fs < %ds)",
                           evt, elapsed, gap)
                return

        title = item["title"]
        note  = item["note"]
        facts = self._build_facts(evt, title, note)
        if not facts:
            _log.debug("Hook %s: prázdná fakta, skip", evt)
            return

        # BOOK_CONSISTENCY_V1 - u knihy pripoj drivejsi poznamky k teze knize
        _fmax = 600
        if evt == "book_read":
            _prior = self._prior_book_notes(title)
            if _prior:
                facts = _prior + "\n\n--- Tato kapitola ---\n" + facts
                _fmax = 1100
        if evt == "case_closed":  # CASE_RESOLUTION_FROM_CLUES_V1 — stopy se nesmí oseknout
            _fmax = 1500
        # Synthesis
        try:
            text = self._synthesis.synthesize(
                topic=cfg["topic"],
                facts=facts,
                style=cfg["style"],
                facts_max_chars=_fmax,  # BOOK_CONSISTENCY_V1
                raise_offline=True,  # PENDING_THOUGHTS_V1
            )
        except LLMOffline as e:  # PENDING_THOUGHTS_V1
            if from_pending:  # PENDING_THOUGHTS_DRAIN_V1 — drain si poradi
                raise
            self._queue_pending(item)
            _log.info("Hook %s: LLM offline, odlozeno do fronty (%s)", evt, e)
            return
        if not text:
            _log.debug("Hook %s: synthesis vrátil None", evt)
            return

        self._last_synthesis_ts[evt] = time.time()
        _log.info("Hook %s → reflexe: %s", evt, text[:80])

        # Zápis do deníku
        try:
            self._diary_writer(cfg["diary_event"], title, text)
        except Exception as e:
            _log.warning("Hook %s diary write failed: %s", evt, e)

        # Upload do RAGu — surový obsah + Hansova reflexe
        # RAG_RAW_PLUS_REFLECTION_PATCH
        if self._knowledge and self._knowledge.enabled:
            try:
                doc_id = self._build_doc_id(cfg, title, item["ts"])
                rag_text = self._build_rag_text(evt, title, note, text, item["ts"])
                # HUMAN_CHAT_METADATA
                _meta = {
                    "typ":   evt,
                    "datum": datetime.now().strftime("%Y-%m-%d"),
                }
                # Pro human_chat přidej person = title (= jméno osoby)
                if evt == "human_chat" and title:
                    _meta["person"] = title.lower().strip()
                    # G5B_DETECT_CONTRADICTION_V1 — reflexe vs karta (jen log)
                    self._g5b_check_contradiction(title, rag_text)
                self._knowledge.upload(
                    collection_key=cfg["collection"],
                    doc_id=doc_id,
                    title=title or cfg["topic"],
                    text=rag_text,
                    metadata=_meta,
                )
            except Exception as e:
                _log.warning("Hook %s RAG upload failed: %s", evt, e)

    # ── G5B_DETECT_CONTRADICTION_V1 ─────────────────────────────────────────
    _G5B_FAMILY_WORDS = {
        'dcera': 'dcera', 'dceru': 'dcera', 'dcery': 'dcera', 'dceři': 'dcera',
        'syn': 'syn', 'syna': 'syn', 'synem': 'syn',
        'manžel': 'manžel', 'manžela': 'manžel', 'manželka': 'manželka',
        'manželku': 'manželka', 'manželky': 'manželka',
        'matka': 'matka', 'matku': 'matka', 'matky': 'matka',
        'otec': 'otec', 'otce': 'otec', 'otcem': 'otec',
    }

    def _g5b_get_rels(self):
        """Lazy Relationships — 1× pokus, pak cache (i None)."""
        if self._g5b_rels_tried:
            return self._g5b_rels
        self._g5b_rels_tried = True
        if self._g5b_config is None:
            _log.warning('G5B: config chybí, karty nedostupné')
            self._g5b_rels = None
            return None
        try:
            from scripts.hans_relationships import Relationships
            self._g5b_rels = Relationships(self._g5b_config)
        except Exception as _e:
            _log.warning('G5B: Relationships nedostupné: %s', _e)
            self._g5b_rels = None
        return self._g5b_rels

    def _g5b_check_contradiction(self, person, reflection_text):
        """Porovná rodinnou roli v reflexi s kartou. Jen LOGUJE rozpor.
        Defenzivní: cokoliv chybí/selže → tiše nic (nikdy nevyhodí).
        """
        try:
            if not person or not reflection_text:
                return
            rels = self._g5b_get_rels()
            if rels is None:
                return
            pid = str(person).lower().strip()
            card = rels.get(pid)
            if card is None:
                return
            txt = str(reflection_text).lower()
            found = set()
            for word, norm in self._G5B_FAMILY_WORDS.items():
                if word in txt:
                    found.add(norm)
            if not found:
                return
            card_role = str(getattr(card, 'role', '') or '').lower()
            fl = getattr(card, 'family_links', None) or {}
            card_terms = {card_role}
            if isinstance(fl, dict):
                card_terms |= {str(k).lower() for k in fl.keys()}
                card_terms |= {str(v).lower() for v in fl.values()
                               if isinstance(v, str)}
            contradicting = {f for f in found
                             if not any(f in t for t in card_terms if t)}
            if contradicting and card_role:
                _log.warning(
                    "G5B: rozpor reflexe×karta [%s]: reflexe říká %s, "
                    "karta role=%r — upload PROBĚHL (jen detekce)",
                    pid, sorted(contradicting), card_role)
        except Exception as _e:
            _log.warning('G5B: detekce selhala (%s): %s', person, _e)

    # ── Builders ────────────────────────────────────────────────────────────

    @staticmethod
    def _build_rag_text(event_type: str, title: str,
                        raw_note: str, reflection: str,
                        ts: float = None) -> str:
        """Sestaví text pro RAG: surový obsah + Hansova reflexe.

        Pro teddy_dialog: surový dialog Hans+Koláč následovaný reflexí.
        Pro web_read/kodi_playing/case_opened: surová fakta + reflexe.
        """
        raw_note = (raw_note or "").strip()
        reflection = (reflection or "").strip()

        # HUMAN_CHAT_RAG_SECTIONS
        # PERSONA_NAME_CONFIGURABLE_V1 — neutrální hlavičky (celý dokument je o personě)
        if event_type == "human_chat":
            header_raw = "## Rozhovor s osobou"
            header_refl = "## Můj dojem"
        elif event_type == "teddy_dialog":
            header_raw = "## Rozhovor s Koláčem"
            header_refl = "## Má úvaha k rozhovoru"
        elif event_type == "kodi_playing":
            header_raw = "## Co se hrálo"
            header_refl = "## Má úvaha k filmu"
        elif event_type == "web_read":
            header_raw = "## Co jsem četl"
            header_refl = "## Má úvaha k článku"
        elif event_type in ("case_opened", "case_closed"):
            header_raw = "## Případ"
            header_refl = "## Má úvaha"
        else:
            header_raw = "## Záznam"
            header_refl = "## Má úvaha"

        parts = []
        if title:
            parts.append(f"# {title}")
        if ts:  # RAG_TIME_ANCHOR_V1
            _d = datetime.fromtimestamp(ts)
            _wd = ('pondělí','úterý','středa','čtvrtek','pátek',
                   'sobota','neděle')[_d.weekday()]
            parts.append(f"_Kdy: {_wd} {_d.day}.{_d.month}.{_d.year}_")
        if raw_note:
            parts.append(f"{header_raw}\n\n{raw_note}")
        if reflection:
            parts.append(f"{header_refl}\n\n{reflection}")
        return "\n\n".join(parts) if parts else reflection

    @staticmethod
    def _build_facts(event_type: str, title: str, note: str) -> str:
        """Sestaví facts string pro synthesis podle event_type."""
        title = (title or "").strip()
        note_full = (note or "").strip()  # CASE_RESOLUTION_FROM_CLUES_V1
        note  = note_full[:600]
        if event_type == "kodi_playing":
            # note má formát "Typ: episode | rok 1983 | ..."
            return f"{title}. {note}" if title else note
        if event_type == "teddy_dialog":
            # note obsahuje "Hans: ... Kolač: ..."
            return note or title
        if event_type in ("case_opened", "case_closed"):
            _n = note_full[:1500]  # CASE_RESOLUTION_FROM_CLUES_V1
            return f"{title}. {_n}" if title and _n else (title or _n)
        # web_read, book_read, default
        return f"{title}: {note}" if title and note else (title or note)

    @staticmethod
    def _build_doc_id(cfg: dict, title: str, ts: float) -> str:
        prefix = cfg["doc_id_prefix"]
        safe   = _safe_id(title, 50) if title else "x"
        # Pro web a dialog přidej timestamp (může být víc položek se stejným titulem)
        if prefix in ("web", "dialog", "chat"):
            return f"{prefix}_{safe}_{int(ts)}"
        return f"{prefix}_{safe}"
