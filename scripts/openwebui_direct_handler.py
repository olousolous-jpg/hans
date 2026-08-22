"""
OpenWebUI Chat Handler
Jediný chat handler — komunikuje s OpenWebUI přes OpenAI-compatible API.
Podporuje: streaming SSE, conversation history, surroundings context,
           greeting cooldown, popup manager, TTS sentence-by-sentence.

Endpoint: POST /api/v1/chat/completions  (OpenAI-compatible)
Auth:     Bearer token
"""

import json
import re
import logging
from scripts.logger import log_once
import threading
import time
import os
import requests
from datetime import datetime, date

from pathlib import Path
from scripts.conversation_store import ConversationStore

# region agent log
from scripts.debug_log import dbg as _dbg
# endregion

# HANS_CHAT_CHANNEL_AWARE_V1 — thread-local aktuální kanál (web/telegram/
# voice/popup). Nastaví ho send_chat_message, čte chat_commands / hans_agent
# přes get_current_channel(). Cross-channel leak conv_store (Telegram →
# web chat „zkus to znova") se tím filtruje na místech, kde by ublížil.
_channel_local = threading.local()

def get_current_channel():
    """HANS_CHAT_CHANNEL_AWARE_V1 — aktuální kanál (nebo None mimo chat vlákno)."""
    return getattr(_channel_local, "channel", None)


# ── G3B_ANTIKONFAB_FIX_V1 — anti-konfabulační prompt (modul-level) ──
# Vstříkne se PŘED fakta v _build_grounding. Drží hans-czech u záznamů.
# Hansovým tónem (předloha: OpenWebUI RAG_TEMPLATE).
ANTIKONFAB = (
    "Následuje to, co o věci VÍŠ. Podej to přirozeně, vlastními slovy, "
    "jako majordomus, který to prostě ví — NEŘÍKEJ \"záznamy uvádějí\", "
    "\"z pozorování vyplývá\" ani \"zaznamenávám\". Mluv ve své osobě. "
    "Co zde NENÍ a nevíš jistě, uctivě přiznej (např. 'domnívám se', "
    "\"nemám o tom spolehlivou znalost\") — nikdy nevydávej dohad za "
    "jistotu. Piš plynulým souvislým textem: žádné odrážky, hvězdičky, "
    "pomlčky na začátku řádků ani jiné formátování. "
    # G4C_TONE_FEWSHOT_V1 — příklad tónu silnější než zákaz
    "Příklad tónu — ŠPATNĚ: \"Záznamy uvádějí, že Standa oceňuje řád.\" "
    "SPRÁVNĚ: \"Standa oceňuje řád, pane.\""
)

# G4_TONE_V1 — tón: vlastní znalost (ne "záznamy uvádějí") + bez markdownu
# G3C_ANTIKONFAB_FALLBACK_V1 — anti-konfab když RAG nic nenašel (bez fakt).
# Faktický dotaz, ale žádné záznamy → Hans nesmí vymýšlet. Měkce: smí
# spekulovat, ale označit. Web ověření přijde post-hoc (G.5).
ANTIKONFAB_NOFACTS = (
    "K tomuto dotazu nemáš ve své paměti spolehlivou znalost. Nevymýšlej "
    "si údaje ani je nevydávej za jisté. Pokud něco soudíš z obecného "
    "povědomí, výslovně to označ (např. 'domnívám se', 'nejsem si jist', "
    "'mohu se mýlit'). Raději uctivě přiznej, že o tom nemáš spolehlivou "
    "znalost, než abys uvedl smyšlený údaj jako fakt. Mluv ve své osobě, "
    "plynulým textem — žádné odrážky, hvězdičky ani formátování. "
    # G4C_TONE_FEWSHOT_V1 — i bez fakt drž vlastní hlas
    "NEŘÍKEJ \"záznamy uvádějí\" ani \"ve své paměti\". Mluv přímo: "
    "ŠPATNĚ: \"Záznamy ukazují, že jsem monitoroval počasí.\" "
    "SPRÁVNĚ: \"Monitoroval jsem počasí, pane.\""
)

# HANS_SELFCONSISTENCY_A1_V1 — sentinel: grounding nebyl předpočítán volajícím
_GROUNDING_UNSET = object()

# HANS_SELFCONSISTENCY_A1_V1 — deterministická abstinence u nestabilního
# faktického dotazu (short-circuit místo volné generace persony).
A1_ABSTAIN_TEXT = (
    "K tomuhle nemám spolehlivý záznam a nerad bych si domýšlel, pane. "
    "Raději přiznám, že si tím nejsem jistý, než abych řekl něco vymyšleného."
)


# TIME_AWARENESS_WORDS_V1 — český slovní čas (0–59) pro slabý model
_CZ_ONES = ('nula','jedna','dvě','tři','čtyři','pět','šest','sedm','osm',
            'devět','deset','jedenáct','dvanáct','třináct','čtrnáct',
            'patnáct','šestnáct','sedmnáct','osmnáct','devatenáct')
_CZ_TENS = {20:'dvacet',30:'třicet',40:'čtyřicet',50:'padesát'}

def _cz_num_0_59(n: int) -> str:
    if n < 20: return _CZ_ONES[n]
    t, o = (n // 10) * 10, n % 10
    return _CZ_TENS[t] if o == 0 else f'{_CZ_TENS[t]} {_CZ_ONES[o]}'

def _cz_unit(n: int, one: str, few: str, many: str) -> str:
    if 11 <= n <= 19: return many        # jedenáct..devatenáct hodin
    o = n if n < 20 else n % 10          # tvar řídí poslední číslo
    if o == 1: return one
    if 2 <= o <= 4: return few
    return many

def _cz_clock_words(h: int, m: int) -> str:
    hw = f"{_cz_num_0_59(h)} {_cz_unit(h,'hodina','hodiny','hodin')}"
    if m == 0: return hw
    mw = f"{_cz_num_0_59(m)} {_cz_unit(m,'minuta','minuty','minut')}"
    return f'{hw} {mw}'


class _SkipLookup(Exception):
    """HANS_PERSON_CARD_BYPASS_V1 — interní signál: odpověď je z karty osoby,
    instantní dohledání se přeskakuje (jinak by přepsalo jistý fakt domněnkou)."""


# ── HANS_PROMPT_BLOCKS_TABLE_V1 (20.8.) — POŘADÍ BLOKŮ SYSTEM PROMPTU ────────
# Seznam bloků žil na ČTYŘECH místech: tři varianty skládání (plný prompt,
# pozdrav, RAG model) a sonda velikostí. Už se rozešly — sonda NEMĚŘILA blok
# `thought`, takže měření z 19.8. bylo o ten blok kratší, než realita.
# Tady je pořadí JEDNOU a varianty jsou sloupec:
#     f = plný prompt (běžná odpověď)
#     g = pozdrav (GREETING_LEAN_SYSTEM_V1 — jen nutné k pozdravení)
#     r = RAG model (jen smysly a vnitřní stav)
# ⚠️ POŘADÍ JE VÝZNAMOVÉ, ne kosmetické: `current` (adresát) musí zůstat
# POSLEDNÍ — HANS_ADDRESSEE_V2 ho sem přesunul právě proto, že ho uprostřed
# přebíjela recency následujících bloků.
_PROMPT_BLOKY = (
    ("system_base", "fg"), ("time", "fgr"), ("persons", "fg"),
    ("surr", "fr"), ("kodi", "fr"), ("room", "fr"), ("place", "fr"),
    ("cal", "f"), ("diary", "f"), ("story", "f"), ("study", "f"),
    ("direction", "f"), ("idea", "f"), ("read", "fr"), ("thought", "fr"),
    ("body", "fgr"), ("mood", "fgr"), ("health", "f"), ("downtime", "f"),
    ("severka", "fg"), ("deepen", "f"), ("lessons", "f"), ("teddy", "fr"),
    ("memory", "f"), ("threads", "f"), ("interests", "f"),
    ("qsuggest", "f"), ("routine", "f"), ("cap", "f"),
    ("current", "fgr"),
)


def slozit_prompt(hodnoty: dict, varianta: str) -> str:
    """HANS_PROMPT_BLOCKS_TABLE_V1 — složí prompt v pořadí `_PROMPT_BLOKY`.
    Bloky, které do varianty nepatří nebo jsou prázdné, se přeskočí."""
    return "".join(hodnoty.get(n) or "" for n, kde in _PROMPT_BLOKY
                   if varianta in kde)


class OpenWebUIDirectHandler:

    def __init__(self, config: dict):
        self.config      = config
        self.chat_config = config.get("openwebui_direct", {})

        self.base_url      = self.chat_config.get("base_url", "http://localhost:8080")
        self.chat_endpoint = f"{self.base_url}/api/v1/chat/completions"
        # Čti model z openwebui_direct.model, fallback na openwebui_chat.model_name
        # Priorita: models.voice → openwebui_direct.model →
        #           openwebui_chat.model_name
        self.model_name    = (config.get("models", {}).get("voice")
                              or self.chat_config.get("model")
                              or config.get("openwebui_chat", {}).get("model_name")
                              or "llama2")
        self.api_token     = self.chat_config.get("api_token", "")
        self.enabled       = self.chat_config.get("enabled", True)

        self.greeting_enabled = config.get("openwebui_chat", {}).get("greeting_enabled", True)
        self.popup_enabled    = config.get("openwebui_chat", {}).get("popup_enabled", False)
        self.greeting_mode    = config.get("openwebui_chat", {}).get(
                                    "greeting_mode", "once_per_session")
        self.greeting_persistence_file = "data/daily_greetings.json"

        self.timeout     = config.get("openwebui_chat", {}).get("request_timeout", 60)

        self.session_greeted = set()
        self.daily_greeted   = self._load_daily_greetings()
        self._used_hints: list[str] = []   # co už bylo zmíněno v pozdravech
        self.chat_lock       = threading.Lock()

        self.tts_speaker     = None
        self.surroundings_db = None
        self.memory          = None  # T5_DIALOG_RECALL_V1
        self.knowledge       = None  # G3A_WIRING_V1 — RAG query (G.1)
        # HansIntent si vytvoříme sami (potřebuje jen config). G3A_WIRING_V1
        try:
            from scripts.hans_intent import HansIntent
            self.intent = HansIntent(config)
            # G5A_IDENTITY_GROUNDING_V3 — vztahové karty (zdroj pravdy)
            try:
                from scripts.hans_relationships import Relationships
                self._rels = Relationships(config)
            except Exception:
                self._rels = None
        except Exception as _ie:
            self.intent = None
            self._rels = None  # G5A_IDENTITY_GROUNDING_V3
            print(f'[Chat] HansIntent init failed: {_ie}')
        self.popup_manager   = None

        self.conv_store = ConversationStore(config)
        print(f"[Chat] Conversation history: {self.conv_store.summary()}")

        if self.popup_enabled and self.enabled:
            self._init_popup_manager()

        print(f"[Chat] OpenWebUI handler — {self.base_url}  model={self.model_name}")
        print(f"[Chat] greeting_mode={self.greeting_mode}  "
              f"popup={self.popup_enabled}")

        if self.enabled:
            self._test_connection()

    # ── Wiring ────────────────────────────────────────────────────────────────

    def set_surroundings_db(self, db):
        self.surroundings_db = db

    def set_knowledge(self, knowledge):  # G3A_WIRING_V1
        """Injektuj HansKnowledge (RAG query) z controlleru pro grounding."""
        self.knowledge = knowledge

    def set_memory(self, memory):  # T5_DIALOG_RECALL_V1
        """Wire Tulvingovy paměti (Memory fasáda) pro greeting kontext."""
        self.memory = memory
        print("[Chat] Surroundings DB connected")

    def set_tts_speaker(self, tts):
        self.tts_speaker = tts
        self._start_web_chat_bridge()  # WEB_CHAT_BRIDGE_V1

    # ── WEB_CHAT_BRIDGE_V1 — chat z web_admin → odpověď + TTS na Pi ────────────
    def _start_web_chat_bridge(self):
        """Spustí poller, který bere chat požadavky z web_admin (přes JSON soubor,
        stejný IPC vzor jako .trigger_dialog) → send_chat_message (vygeneruje
        odpověď + vysloví hlasem na Pi) → odpověď zpět do souboru pro web."""
        if getattr(self, "_web_chat_thread", None):
            return
        self._web_chat_thread = threading.Thread(
            target=self._web_chat_loop, daemon=True)
        self._web_chat_thread.start()

    def _web_chat_loop(self):
        import json as _json
        from pathlib import Path as _P
        req_path  = _P("data/.web_chat_req.json")
        resp_path = _P("data/.web_chat_resp.json")
        while True:
            try:
                if req_path.exists():
                    try:
                        req = _json.loads(req_path.read_text(encoding="utf-8"))
                    except Exception:
                        req = None
                    try:
                        req_path.unlink()
                    except Exception:
                        pass
                    if req and req.get("message"):
                        self._handle_web_chat(req, resp_path)
            except Exception as e:
                print(f"[WebChat] loop error: {e}")
            time.sleep(1.5)

    @staticmethod
    def _collapse_repeated_greetings(text: str) -> str:
        """hans-czech občas na vágní zprávu degeneruje do opakovaných pozdravů
        („Dobrý večer, Stando. …" 3×). Když odpověď obsahuje věcný odstavec,
        zahoď krátké odstavce-pozdravy (filler); samé pozdravy → nech první."""
        import re as _re
        if not text:
            return text
        paras = [p.strip() for p in _re.split(r"\n\s*\n", text) if p.strip()]
        if len(paras) <= 1:
            return text
        _greet = _re.compile(r"^(dobr[ýé]\s+(ve[čc]er|den|r[áa]no)|ahoj|zdrav[ií]m|t[ěe]š[ií])", _re.I)
        is_filler = lambda p: bool(_greet.match(p)) and len(p) < 90
        substantive = [p for p in paras if not is_filler(p)]
        kept = substantive if substantive else paras[:1]
        return "\n\n".join(kept)

    def _handle_web_chat(self, req, resp_path):
        import json as _json
        rid     = req.get("id")
        person  = (req.get("person") or "Uživatel").strip() or "Uživatel"
        message = req.get("message")

        # Web chat: vezmi CELOU odpověď (bez stream-TTS), vyčisti opakované
        # pozdravy, AŽ POTOM vyslov — opraví zobrazené i mluvené najednou.
        try:
            # HANS_CHAT_CHANNEL_AWARE_V1 — tag zprávy channelem 'web'
            resp = self.send_chat_message(person, message, channel="web")
        except Exception as e:
            resp = f"(chyba: {e})"
        resp = self._collapse_repeated_greetings(resp or "")

        tts = self.tts_speaker
        if tts and getattr(tts, "enabled", False) and resp:
            try:
                tts.speak(resp, priority=True)
            except Exception as e:
                print(f"[WebChat] TTS error: {e}")

        try:
            resp_path.write_text(
                _json.dumps({"id": rid, "response": resp, "ts": time.time()},
                            ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            print(f"[WebChat] write resp error: {e}")

    # ── Daily greeting persistence ────────────────────────────────────────────

    def _load_daily_greetings(self) -> set:
        try:
            os.makedirs("data", exist_ok=True)
            if os.path.exists(self.greeting_persistence_file):
                with open(self.greeting_persistence_file) as f:
                    data = json.load(f)
                today = date.today().isoformat()
                if today in data:
                    cleaned = {d: v for d, v in data.items() if d >= today}
                    with open(self.greeting_persistence_file, "w") as f:
                        json.dump(cleaned, f)
                    return set(data[today])
        except Exception as e:
            print(f"[Chat] Load daily greetings error: {e}")
        return set()

    def _save_daily_greetings(self):
        try:
            today = date.today().isoformat()
            data  = {}
            if os.path.exists(self.greeting_persistence_file):
                with open(self.greeting_persistence_file) as f:
                    data = json.load(f)
            data[today] = list(self.daily_greeted)
            with open(self.greeting_persistence_file, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            print(f"[Chat] Save daily greetings error: {e}")

    # ── Greeting logic ────────────────────────────────────────────────────────

    def should_greet_person(self, name: str) -> bool:
        if not self.greeting_enabled:
            return False
        if self.greeting_mode == "once_per_session":
            return name not in self.session_greeted
        elif self.greeting_mode == "once_per_day":
            return name not in self.daily_greeted
        return True

    def mark_person_greeted(self, name: str):
        if self.greeting_mode == "once_per_session":
            self.session_greeted.add(name)
        elif self.greeting_mode == "once_per_day":
            self.daily_greeted.add(name)
            self._save_daily_greetings()

    def reset_session_greetings(self):
        self.session_greeted.clear()

    def reset_daily_greetings(self):
        self.daily_greeted.clear()
        self._save_daily_greetings()

    # ── Popup ─────────────────────────────────────────────────────────────────

    def _init_popup_manager(self):
        try:
            from scripts.popup_chat_window import PopupChatManager
            self.popup_manager = PopupChatManager(self)
            print("[Chat] Popup manager initialized")
        except Exception as e:
            print(f"[Chat] Popup manager init failed: {e}")
            self.popup_enabled = False

    # ── Connection test ───────────────────────────────────────────────────────

    def _test_connection(self):
        try:
            headers = self._headers()
            r = requests.get(f"{self.base_url}/api/v1/models",
                             headers=headers, timeout=5)
            if r.status_code == 200:
                models = [m.get("id", "") for m in r.json().get("data", [])]
                if any(self.model_name in m for m in models):
                    print(f"[Chat] Connected — model '{self.model_name}' ready")
                else:
                    print(f"[Chat] WARNING: model '{self.model_name}' not found. "
                          f"Available: {models[:3]}")
                # region agent log
                try:
                    _dbg(
                        location="openwebui_direct_handler.py:_test_connection",
                        message="Model list fetched",
                        data={
                            "http": int(r.status_code),
                            "model_name": str(self.model_name),
                            "models_count": int(len(models)),
                            "models_sample": models[:5],
                        },
                    )
                except Exception:
                    pass
                # endregion
            else:
                print(f"[Chat] Connection issue: HTTP {r.status_code}")
        except Exception as e:
            print(f"[Chat] Cannot connect to OpenWebUI at {self.base_url}: {e}")

    # ── Face recognition event ────────────────────────────────────────────────

    def handle_face_recognition(self, name: str, confidence: float):
        # HANS_EYE_BLINK_V1 — vrací True, když se PRÁVĚ spustil pozdrav (aby
        # volající mohl mrknout očima). Jinak False (idempotentní — už pozdraveno).
        if not self.enabled or not name:
            return False
        should_greet = self.should_greet_person(name)
        greeted = False
        if should_greet and self.greeting_enabled:
            self.mark_person_greeted(name)
            threading.Thread(target=self._send_greeting_async,
                             args=(name, confidence), daemon=True).start()
            greeted = True
        if self.popup_enabled and self.popup_manager:
            self.popup_manager.handle_face_detection(name, confidence,
                                                      not should_greet)
        return greeted

    def _send_greeting_async(self, name: str, confidence: float):
        try:
            prompt = self._generate_greeting_prompt(name)
            first  = [True]

            def _on_sentence(sentence: str):
                tts = self.tts_speaker
                if tts and tts.enabled:
                    tts.speak(sentence, priority=first[0])
                first[0] = False

            response = self._stream_message(prompt, name=name,
                                             internal=True,  # G3D: uvítačka není faktický dotaz
                                            on_sentence=_on_sentence)
            if response:
                self.conv_store.add_greeting(name, response)
                self._log_interaction(name, str(prompt), response)
                _mem = getattr(self, 'memory', None)  # T5_DIALOG_RECALL_V1
                if _mem is not None:
                    try: _mem.bump_dialog(name)
                    except Exception as _be: print(f"[Chat] bump_dialog failed: {_be}")

            # HANS_QUESTION_POPUP_V1 — po pozdravu zkus položit čekající otázku
            # přes popup okno (vysloví + zobrazí + čeká na odpověď).
            try:
                _opened = self.ask_question_via_popup(name)
            except Exception as _qpe:
                _opened = False
                print(f"[Chat] greeting popup-question failed: {_qpe}")
            # GREETING_THREAD_POPUP_V1 — pozdrav navnázal na rozjetou nitku
            # (vyslovil follow-up) → otevři okno naseedované pozdravem, ať má
            # uživatel kam odpovědět. Jen když popup-otázka neběžela (ne 2 okna).
            try:
                if (not _opened) and getattr(self, '_greeting_thread_surfaced', False) and response:
                    from scripts.popup_chat_window import SimplePopupChat
                    SimplePopupChat(self, name, 1.0, already_greeted=True,
                                    initial_question=response)
            except Exception as _tpe:
                print(f"[Chat] greeting thread-popup failed: {_tpe}")
        except Exception as e:
            print(f"[Chat] Greeting error for {name}: {e}")

    # ── Prompt builders ───────────────────────────────────────────────────────

    # ── G3B_GROUNDING_V1 — grounding fakt z RAG do kontextu ──────────────
    # Mapování intent třídy → RAG kolekce
    # G3B_MULTICOLLECTION_V1 — list kolekcí na třídu (fakta roztroušená)
    _GROUNDING_COLLECTION = {
        'film': ['hans_filmy'],
        # G5A_IDENTITY_GROUNDING_V1 — hans_identita (vztahové karty =
        # zdroj pravdy o lidech) přidána k osobnost I udalost
        # ('co víš o X' padá pod udalost, ne osobnost — ověřeno).
        'osobnost': ['hans_identita', 'hans_denik', 'hans_pripady', 'hans_cetba'],
        'udalost': ['hans_identita', 'hans_denik', 'hans_pripady', 'hans_cetba'],
        'misto': ['hans_denik', 'hans_cetba'],
    }
    # HANS_CHATLOG_NOT_FACT_V1 — poznávací znak chatového logu v RAG.
    _CHATLOG_RE = __import__("re").compile(
        r"^\s*#*\s*Rozhovor\s+s\s|NEOVĚŘENO — vlastní výrok",
        __import__("re").IGNORECASE)
    _GROUNDING_MAX_DISTANCE = 0.75   # G3B_THRESHOLD_V1 — kalibrováno z dat (bylo 0.70, moc přísné)
    _GROUNDING_TIMEOUT_S = 2         # grounding nikdy nebrzdí odpověď
    _GROUNDING_K = 3
    # HANS_RAGFIRST_STRICT_V1 (#2 finalizace) — STRICT práh na TOP shodu.
    # bge-m3 relevantní ~0.64-0.69, šum se překrývá; MAX_DISTANCE 0.75 je
    # jen chromadb filter (chunky nad tím jsou zahozeny). Chunky mezi
    # 0.70-0.75 jsou borderline: prošly, ale nejsou opravdu ukotvené →
    # bez autoritativního zdroje (entity store / vztahová karta) je
    # neber jako grounding, radši abstinuj (RAG-first princip #2).
    _GROUNDING_STRICT_MAX = 0.70

    # G5A_IDENTITY_GROUNDING_V3 — vztahová karta z DB jako tvrdý fakt
    # G5A_NAME_FORMS_V1 — tvary jmen pro detekci osoby v dotazu (české pády vč.
    # měkkých vzorů; diakritika i bez). pid → seznam tvarů (lowercase).
    # PORTABILITY: data jdou z config.json `person_name_forms` (gitignored), ne
    # natvrdo v kódu (žádná reálná jména v repu). Prázdné = bez detekce (graceful).

    def _favorite_game(self, name: str):
        """HANS_GAME_LAUNCH_ATTRIB_V1 — nejčastěji spouštěná hra osoby (z deníku
        game_launched, posl. 90 dní). None když žádná."""
        if not name:
            return None
        try:
            import sqlite3 as _sq
            import time as _t
            db = (self.config.get("hans_idle", {}) or {}).get(
                "diary_db", "data/hans_diary.db")
            conn = _sq.connect("file:%s?mode=ro" % db, uri=True, timeout=3)
            row = conn.execute(
                "SELECT COALESCE(NULLIF(data,''),note), COUNT(*) c FROM diary "
                "WHERE event_type='game_launched' AND title=? AND ts>? "
                "GROUP BY 1 ORDER BY c DESC LIMIT 1",
                (name, _t.time() - 90 * 86400)).fetchone()
            conn.close()
            return row[0] if row and row[0] else None
        except Exception:
            return None

    def _build_card_fact(self, text: str) -> str:
        """Najdi v dotazu známou osobu → vrať tvrdý fakt z relationships DB.
        Adresuje kartu podle jména (NE embedding). Bez characterization
        (ta nese starý tón). Prázdné, když nikdo nebo modul chybí."""
        _rels = getattr(self, '_rels', None)
        if _rels is None or not text:
            return ''
        import re as _re_g5a
        _low = text.lower()
        # tokenizuj dotaz na slova (ať skloňovaný tvar matchne jako CELÉ slovo,
        # ne podřetězec — vyhne se falešným shodám)
        _words = set(_re_g5a.findall(r'[a-zěščřžýáíéúůďťňó]+', _low))
        _forms_map = (self.config.get("person_name_forms", {}) or {})  # PORTABILITY
        for _pid, _forms in _forms_map.items():
            if _words & set(_forms):
                try:
                    _c = _rels.get(_pid)
                except Exception:
                    _c = None
                if not _c:
                    continue
                # slož tvrdý fakt: role + rodina (z dict family_links)
                _parts = [f"{_c.display_name} je {_c.role}"]
                _fl = _c.family_links or {}
                _sp = _fl.get('spouse')
                _ch = _fl.get('children') or []
                _par = _fl.get('parents') or []
                if _sp:
                    _spc = _rels.get(_sp)
                    _parts.append(f"manžel(ka): {_spc.display_name if _spc else _sp}")
                if _ch:
                    _chn = []
                    for _k in _ch:
                        _kc = _rels.get(_k)
                        _chn.append(_kc.display_name if _kc else _k)
                    _parts.append("děti: " + ", ".join(_chn))
                if _par:
                    _pn = []
                    for _k in _par:
                        _kc = _rels.get(_k)
                        _pn.append(_kc.display_name if _kc else _k)
                    _parts.append("rodiče: " + ", ".join(_pn))
                return "Fakta o osobě " + _c.display_name + ": " + ", ".join(_parts) + "."
        return ''

    def _knowledge_fts_grounding(self, text: str) -> str:
        """HANS_KNOWLEDGE_FTS_V1 (6.8.) — poslední šance před abstinencí.

        Prohledá Hansovy VLASTNÍ zápisky (`study_note`, `study_mastery`,
        `reading_takeaway`, `web_read`) lexikálním FTS. Sémantický RAG míjí,
        když se dotaz a zápisek liší SLOVY — doloženo 6.8.: „řekni mi, jak se
        vyvíjely zbrojnice" → „nemám spolehlivý záznam", ačkoli `study_note`
        s přesně tím titulem existuje.

        Vrací GROUNDING (model odpoví ze zápisků), ne hotovou větu — obsah je
        Hansův vlastní, jen se k němu neuměl dostat. AND-only vyhledávání
        v `hans_convindex` drží riziko falešného nálezu nízko.

        ⚠️ Volá se ze DVOU míst: G3C (žádná shoda pod prahem) i #2 (RAG slabý).
        První verze patche mířila jen na to druhé a v provozu se NIKDY
        nespustila — reálná cesta vede přes G3C (ověřeno v logu, ne z kódu).
        """
        try:
            from scripts.hans_convindex import search as _kfts
            hits = _kfts(str(text), limit=3, kind='knowledge')
            if not hits:
                return ''
            blk = '\n\n'.join(
                '[Z mých zápisků — %s]\n%s' % ((t or s), (x or '')[:700])
                for _ts, s, _p, t, x in hits)
            logging.getLogger(__name__).info(
                'HANS_KNOWLEDGE_FTS_V1: %d zápisků → grounding '
                '(RAG nic nenašel)', len(hits))
            return ('\n\n' + ANTIKONFAB + '\n\nTOHLE MÁŠ VE SVÝCH ZÁPISCÍCH '
                    '(odpověz z toho; co v nich není, nedomýšlej):\n' + blk)
        except Exception as e:
            logging.getLogger(__name__).debug('knowledge FTS: %s', e)
            return ''

    # HANS_KODI_CAST_FACT_V1 (21.8.) — dotaz na obsazení / tvůrce z knihovny.
    # Kodi drží `cast` u filmů (40 ze 40 vzorku) i u dílů seriálů — jen se na
    # to nikdy nikdo neptal, takže si model herce vymýšlel.
    _CAST_PAT = re.compile(
        r"(kdo\s+(tam|v\s+tom|v\s+n[ěe]m|v\s+n[íi])?\s*(hraj|hr[áa]l|ú[čc]ink|"
        r"uc[íi]nk)|kdo\s+si\s+(tam\s+)?zahr[áa]l|obsazen[íi]|"
        r"kdo\s+to\s+(re[žz]|nato[čc])|kdo\s+hraje)", re.IGNORECASE)

    def _kodi_cast_fact(self, text: str) -> str:
        """Obsazení (a režie) toho, o čem je řeč — deterministicky z Kodi.

        Vrací '' když věta o obsazení není, titul se nepodařilo určit, nebo
        knihovna nic nemá. Prázdný výsledek = Hans dál abstinuje; NIC se
        nedomýšlí.
        """
        t = (text or "").strip()
        # HANS_CAST_NOT_ORDER_V1 — vzor bydlí v hans_intent (sdílí ho agent).
        try:
            from scripts.hans_intent import pta_se_na_obsazeni as _ptaji
            _je_to_ono = _ptaji(t)
        except Exception:
            _je_to_ono = bool(self._CAST_PAT.search(t))
        if not t or not _je_to_ono:
            return ''
        kodi = getattr(getattr(self, "_hans_idle", None), "kodi", None)
        if not kodi:
            return ''
        # 1) O ČEM je řeč: rozřešená věta z vlákna/F1 (holé „kdo tam hraje?"
        #    titul nenese), jinak to, co zrovna běží.
        kandidati = []
        _f1 = getattr(self, '_f1_query', None)
        if _f1:
            kandidati.append(str(_f1))
        try:
            _tc = getattr(self, '_thread_ctx', None)
            if _tc and _tc[1]:
                kandidati.append(str(_tc[1]))
            if _tc and len(_tc) > 2 and _tc[2]:
                kandidati.append(str(_tc[2]))
        except Exception:
            pass
        polozka, popis = None, ''
        for k in kandidati:
            try:
                ep = kodi.find_episode(k)
            except Exception:
                ep = None
            if ep:
                polozka = kodi.episode_details(ep.get("episodeid"))
                popis = kodi.episode_label(ep)
                break
            try:
                mv = kodi.find_movie(k)
            except Exception:
                mv = None
            if mv:
                polozka = kodi.movie_details(mv.get("movieid"))
                popis = mv.get("title") or ''
                break
        if polozka is None:
            # nikdo titul neřekl → ber, co běží na TV
            try:
                np = kodi.get_now_playing() or {}
            except Exception:
                np = {}
            nazev = (np.get("title") or np.get("label") or "").strip()
            if not nazev:
                return ''
            ep = kodi.find_episode(nazev)
            if ep:
                polozka = kodi.episode_details(ep.get("episodeid"))
                popis = kodi.episode_label(ep)
            else:
                mv = kodi.find_movie(nazev)
                if mv:
                    polozka = kodi.movie_details(mv.get("movieid"))
                    popis = mv.get("title") or nazev
        if not polozka:
            return ''
        herci = polozka.get("cast") or []
        if not herci:
            return ''
        radky = []
        for c in herci[:10]:
            jmeno = (c.get("name") or "").strip()
            role = (c.get("role") or "").strip()
            if not jmeno:
                continue
            radky.append("- %s%s" % (jmeno, (" jako %s" % role) if role else ""))
        if not radky:
            return ''
        rez = polozka.get("director") or []
        hlava = "OBSAZENÍ Z MÉ KNIHOVNY — „%s“:" % (popis or "tenhle titul")
        pata = ("Vyjmenuj POUZE tahle jména. Nikoho nepřidávej, role nedomýšlej; "
                "co tu není, o tom řekni, že to nevíš.")
        blok = [hlava] + radky
        if rez:
            blok.append("Režie: %s" % ", ".join(rez[:3]))
        if len(herci) > 10:
            blok.append("(v seznamu je celkem %d jmen, tohle je prvních %d)"
                        % (len(herci), len(radky)))
        blok.append(pata)
        logging.getLogger(__name__).info(
            'HANS_KODI_CAST_FACT_V1: obsazení z knihovny pro %r (%d jmen)',
            popis[:40], len(herci))
        return '\n\n' + ANTIKONFAB + '\n\n' + "\n".join(blok)

    def _dohledej_kotvu(self, veta: str, name: str = None):
        """HANS_ANCHOR_LOOKUP_V1 (22.8.) — dohledej PŘEDMĚT dotazu a vrať
        provizorní odpověď, nebo None (pak platí dosavadní chování).

        Téma bere z KOTVY (`hans_convindex.kotva_tematu`), ne z regexu
        „znáš X?" — ten míjel reálné formulace: ze čtyř skutečných vět
        o hradu Kost rozpoznal téma jen u jedné (změřeno 22.8.).
        Zápis jde JEN do čekárny `unverified_findings`; noční ověření
        a ranní oprava už běží ([[instant-lookup-verify-loop]]).
        """
        try:
            from scripts.hans_convindex import kotva_tematu
            from scripts.hans_findings import lookup_now
            _known = tuple((self.config.get("known_persons", {}) or {}).keys()) + \
                tuple(str(v.get("nom", "")) for v in
                      (self.config.get("known_persons", {}) or {}).values())
            tema = kotva_tematu(veta or "", vynech=_known)
            if not tema:
                return None
            _dbp = (self.config.get("hans_idle", {}) or {}).get(
                "diary_db") or self.config.get("diary_db") or "data/hans_diary.db"
            out = lookup_now(self.config, _dbp, tema, veta or "", asker=name)
            logging.getLogger(__name__).info(
                "HANS_ANCHOR_LOOKUP_V1: téma %r → %s", tema,
                "dohledáno" if out else "nic (platí dosavadní odpověď)")
            return out
        except Exception as e:
            logging.getLogger(__name__).warning(
                "HANS_ANCHOR_LOOKUP_V1 selhalo: %s", e)
            return None

    def _vysledek_groundingu(self, vysledek: str, cesta: str) -> None:
        """HANS_GROUNDING_OUTCOME_LOG_V1 (20.8.) — JEDNO místo, kde se zapíše
        výsledek groundingu, a rovnou se pozná, KTERÁ cesta ho vyrobila.

        PROČ: `_build_grounding` má 19 východů a každý si dosud jen tiše
        přiřadil `_grounding_outcome`. Když pak odpověď dopadla divně, nešlo
        z logu poznat, kdo ji obsloužil — 20.8. mě to dvakrát zdrželo
        (u dotazu na schopnosti a u provenienčního dotazu jsem musel příčinu
        hledat greppem přes markery jednotlivých větví).
        Chování se NEMĚNÍ: hodnota je táž, přibyl jen záznam.

        `vysledek` = co dostane volající (skip/grounded/opinion/self_state/
        nonfactual/factual_nofacts), `cesta` = která větev to rozhodla.
        Je to zároveň příprava na úklid té funkce: než se dá přeskládat,
        musí být vidět, kudy dotazy reálně tečou.
        """
        self._grounding_outcome = vysledek
        # GROUNDING_GUARD_ACTIVE_V2 (22.8.) — cesta se PAMATUJE, ne jen loguje:
        # guard smí zasáhnout jen u tenkého fallbacku (zápisky), ne u plného RAG.
        self._grounding_cesta = cesta
        try:
            logging.getLogger(__name__).info(
                'GROUNDING: %s ← %s', vysledek, cesta)
        except Exception:
            pass

    def _build_grounding(self, user, name=None) -> str:
        """G3B_GROUNDING_V1 — vrátí grounding blok pro faktický dotaz.

        Faktická zpráva → intent → kolekce → query() pod prahem →
        anti-konfab prompt + fakta. Volná zpráva / nic nenalezeno → ''.
        Defenzivní: cokoliv chybí/selže → '' (grounding se tiše přeskočí).
        """
        # user může být tuple (system,user) nebo string — vytáhni text
        _text = user
        if isinstance(user, tuple) and len(user) == 2:
            _text = user[1]
        if not _text or not str(_text).strip():
            return ''

        # HANS_THREAD_V1 — navazující věta si nese předmět z předchozí
        # repliky, aby ji detektory neposuzovaly izolovaně. Guard na shodu
        # s originálem: _build_grounding se volá i mimo hlavní chat cestu.
        try:
            _tc = getattr(self, '_thread_ctx', None)
            if _tc and str(_tc[0]) == str(_text) and _tc[1] != _tc[0]:
                _text = _tc[1]
        except Exception as _tiche:
            log_once(  # HANS_NO_SILENT_CTX_V1
                logging.getLogger(__name__), "_build_grounding(ř. 645)",
                "_build_grounding: blok kontextu selhal (ř. 645): %s", _tiche)

        # HANS_SELFCONSISTENCY_A1_V1 — zaznamenej výsledek groundingu pro
        # volajícího (A1 short-circuit běží jen u 'factual_nofacts').
        self._vysledek_groundingu('skip', 'start')

        # HANS_A1_THREAD_TEXT_V1 (21.8.) — sem si F1 odloží ROZŘEŠENOU podobu
        # dotazu, aby se podle ní mohla rozhodnout A1 brzda (viz gate níž).
        # Nulovat je NUTNÉ: bez toho by zvětralá věta z minulého tahu
        # klasifikovala tah další.
        self._f1_query = None

        # HANS_KNOWLEDGE_CHECK_V1 (18.7.) — „znáš X?" / „co víš o X?" když X
        # NENÍ v paměti (deník/entities). Bez tohoto hans-czech halucinuje
        # „mám v paměti záznamy" i pro věci, o kterých nikdy neslyšel (doložený
        # Červený trpaslík chat 21:15). System prompt klauzule V2 nezakázala;
        # grounding blok (G4B_POSITION_V1) má silnější slovo — sedí těsně před
        # user query, přebíjí persona finetune.
        try:
            from scripts.hans_recall import (
                is_knowledge_check_query, knowledge_check_answer,
                reading_recall_answer, person_card)
            if is_knowledge_check_query(str(_text)):
                _dbp_kc = (self.config.get("diary_db")
                           or (self.config.get("hans_idle", {}) or {}).get("diary_db")
                           or "data/hans_diary.db")
                # HANS_PERSON_CARD_KC_V1 (18.8.) — OSOBA MÁ PŘEDNOST PŘED ČTENÍM.
                # Agentní akce `report_person` řeší jen tvar „kdo je X?"; „co víš
                # o X?" se sem odbočí DŘÍV a agenta vůbec nepotká. Doloženo živě
                # 18.8.: „a co víš o Janě?" → Hans popsal vesnici Henčov
                # u Jihlavy, protože v paměti nic nenašel a spustil dohledání.
                # Fakt o paní domu přitom leží v `relationships`. Proto se sem
                # vkládá první: vlastní pozorování domácnosti je autoritativnější
                # než cokoli dohledaného, a rovnou to ubírá práci near-miss
                # pravopisu ([[instant-lookup-verify-loop]]).
                # Neosobní dotaz („co víš o hradech?") vrátí prázdno → beze změny.
                # HANS_PERSON_CARD_KC_FIX_V1 (19.8.) — OPRAVA MÉ VLASTNÍ REGRESE
                # z 18.8. Zpráva sem chodí s prefixem „<jméno> se ptá:", takže
                # `find_known_person` našel TAZATELE a `person_card` vracel jeho
                # kartu jako grounding NA COKOLI. Změřeno: na „a co ses o tom
                # divadle dozvěděl?" dostal model 448 zn Oldova životopisu
                # místo 1 307 zn vlastního zápisku o divadle — a tak si rok
                # vzniku vymyslel. Postihovalo to KAŽDÝ znalostní dotaz v chatu.
                # Dvě pojistky: (1) prefix odstranit, (2) kartu pustit jen
                # u dotazu, který se OPRAVDU ptá na osobu.
                try:
                    import re as _pcre
                    _q_nopfx = _pcre.sub(r"^\s*\S+\s+se\s+pt[áa]:\s*", "",
                                         str(_text))
                    from scripts.hans_recall import asks_about_person as _aap2
                    _pc = (person_card(_dbp_kc, _q_nopfx, self.config)
                           if _aap2(_q_nopfx, self.config) else "")
                except Exception:
                    _pc = ""
                if _pc:
                    self._vysledek_groundingu('grounded', 'pc_stav')
                    return _pc
                # HANS_READING_RECALL_V1 — nejdřív deterministicky dohledej, co
                # si o tom Hans SÁM přečetl (declension-safe, obchází flaky RAG
                # na tenkých souhrnech). Má přednost před „nemám záznam".
                _rr = reading_recall_answer(_dbp_kc, str(_text))
                if _rr:
                    self._vysledek_groundingu('grounded', 'reading_recall')
                    return _rr
                _kc = knowledge_check_answer(_dbp_kc, str(_text))
                if _kc:
                    self._vysledek_groundingu('grounded', 'reading_recall_tema')
                    return _kc
                # None = topic JE v paměti → nech normální recall/RAG cestu
        except Exception as _tiche:
            log_once(  # HANS_NO_SILENT_CTX_V1
                logging.getLogger(__name__), "_build_grounding(ř. 709)",
                "_build_grounding: blok kontextu selhal (ř. 709): %s", _tiche)

        # HANS_RECENT_ACTIVITY_V1 (18.7.) — „co jsi se dnes dozvěděl / co sis
        # zapsal / co jsi dnes dělal"? Deterministický recall Hansovy vlastní
        # aktivity za posledních N dní (default 1). Opravuje false-negative
        # anti-konfab „nemám záznam" (doloženo chat 20:44/20:45 — Hans DNES
        # studoval, četl, maloval; ale intent 'udalost' + RAG žádný match →
        # G3C brzda). Deterministické fakta z deníku obchází.
        try:
            from scripts.hans_recall import (
                is_recent_activity_query, recent_activity_answer)
            if is_recent_activity_query(str(_text)):
                _dbp_ra = (self.config.get("diary_db")
                           or (self.config.get("hans_idle", {}) or {}).get("diary_db")
                           or "data/hans_diary.db")
                _ra = recent_activity_answer(_dbp_ra, days=1)
                if _ra:
                    self._vysledek_groundingu('grounded', 'nedavna_aktivita')
                    return _ra
        except Exception as _tiche:
            log_once(  # HANS_NO_SILENT_CTX_V1
                logging.getLogger(__name__), "_build_grounding(ř. 729)",
                "_build_grounding: blok kontextu selhal (ř. 729): %s", _tiche)

        # HANS_SOURCE_QUERY_V1 — „odkud to víš / kde jsi to četl / máš zdroj"?
        # MUSÍ BÝT PRVNÍ (dřív než _intent/_knowledge gate) — dotaz na
        # provenienci NEpotřebuje intent/RAG infrastrukturu; přebije obecnou
        # anti-konfab klauzuli (V2). Diagnóza 17.7. 12:07: můj předchozí
        # umístění za _intent gate způsobilo, že se do check nedostalo (intent
        # může být None u meta-dotazů).
        try:
            from scripts.hans_recall import is_source_query, sources_reply
            _log_dbg = logging.getLogger(__name__)
            if is_source_query(str(_text)):
                _dbp_s = (self.config.get("diary_db")
                          or (self.config.get("hans_idle", {}) or {}).get("diary_db")
                          or "data/hans_diary.db")
                self._vysledek_groundingu('grounded', 'zdroje')
                _log_dbg.info('HANS_SOURCE_QUERY_V1: match → sources_reply grounding')
                return sources_reply(_dbp_s, user_text=str(_text))
        except Exception as _sqe:
            logging.getLogger(__name__).warning(
                'HANS_SOURCE_QUERY_V1 check selhal: %s', _sqe)

        _intent = getattr(self, 'intent', None)
        _knowledge = getattr(self, 'knowledge', None)
        if _intent is None or _knowledge is None:
            return ''   # nezapojeno → tiše nic

        # HANS_OPINION_GROUNDING_G1_V1 — názorový/filosofický dotaz NENÍ
        # faktický: patří do imaginativního registru (postoje, ne RAG/A1).
        # Musí PŘED intent klasifikací — „co si myslíš o X?" intent chybně
        # řadí jako faktické (otázkový signál) → bez tohohle by filosofii
        # hrozil ANTIKONFAB_NOFACTS + A1 abstinence.
        try:
            from scripts.hans_opinion import is_opinion_query as _ioq
            if _ioq(str(_text)):
                self._vysledek_groundingu('opinion', 'nazor')
                return ''
        except Exception as _tiche:
            log_once(  # HANS_NO_SILENT_CTX_V1
                logging.getLogger(__name__), "_build_grounding(ř. 767)",
                "_build_grounding: blok kontextu selhal (ř. 767): %s", _tiche)

        # HANS_CHAT_RECALL_V2 — recall PŘEDCHOZÍHO rozhovoru („pamatuješ na X",
        # „mluvili jsme o…", „co jsi navrhl"). Sémantický RAG vágní recall často
        # nedohledá (uložené repliky ≠ znění dotazu) → deterministicky prohledej
        # skutečný human_chat. PŘEDNOST (real data), obchází RAG práh + šum.
        try:
            from scripts.hans_recall import is_recall_query, conversation_recall
            if is_recall_query(str(_text)):
                _dbp_r = (self.config.get("diary_db")
                          or (self.config.get("hans_idle", {}) or {}).get("diary_db")
                          or "data/hans_diary.db")
                _rc = conversation_recall(_dbp_r, str(_text), person=name)
                if _rc:
                    self._vysledek_groundingu('grounded', 'chat_recall')
                    _blk = "\n\n".join("[Dřívější rozhovor — %s]\n%s" % (kdy, note)
                                       for kdy, note in _rc)
                    return ("\n\nSKUTEČNÝ ZÁZNAM dřívějšího rozhovoru (odpověz JEN "
                            "z něj, nevymýšlej datum ani detaily; na co v záznamu "
                            "není, přiznej „to si nevybavuji“):\n" + _blk)
        except Exception as _tiche:
            log_once(  # HANS_NO_SILENT_CTX_V1
                logging.getLogger(__name__), "_build_grounding(ř. 788)",
                "_build_grounding: blok kontextu selhal (ř. 788): %s", _tiche)

        # HANS_COMMITMENTS_V1 — „co jsi mi slíbil?" → deterministicky z uložených
        # SLIBŮ (ne hledání v textu); prázdno → honestní „nic", NE výmysl.
        try:
            from scripts.hans_commitments import commitments_answer as _commit_ans
            _dbp_c = (self.config.get("diary_db")
                      or (self.config.get("hans_idle", {}) or {}).get("diary_db")
                      or "data/hans_diary.db")
            _cr = _commit_ans(_dbp_c, str(_text), person=name)
            if _cr:
                self._vysledek_groundingu('grounded', 'zavazky')
                return _cr
        except Exception as _tiche:
            log_once(  # HANS_NO_SILENT_CTX_V1
                logging.getLogger(__name__), "_build_grounding(ř. 802)",
                "_build_grounding: blok kontextu selhal (ř. 802): %s", _tiche)

        # HANS_FILM_RECALL_V1 — dotaz na FILM podle názvu: dohledej Hansovy
        # VLASTNÍ deníkové záznamy (movie_opinion/kodi_playing) o tom filmu, ať
        # nezapře, co ví (doložený případ „Proud krve"). RAG kolekce hans_filmy
        # ani conversation_recall tyhle eventy nenajdou. PŘED intent/RAG.
        try:
            from scripts.hans_recall import film_knowledge_answer as _film_recall
            _dbp_f = (self.config.get("diary_db")
                      or (self.config.get("hans_idle", {}) or {}).get("diary_db")
                      or "data/hans_diary.db")
            _fr = _film_recall(_dbp_f, str(_text))
            if _fr:
                self._vysledek_groundingu('grounded', 'film_recall')
                return _fr
        except Exception as _tiche:
            log_once(  # HANS_NO_SILENT_CTX_V1
                logging.getLogger(__name__), "_build_grounding(ř. 818)",
                "_build_grounding: blok kontextu selhal (ř. 818): %s", _tiche)

        try:
            # 1) intent — je dotaz faktický?
            res = _intent.classify(str(_text))
            if not res.is_factual:
                # HANS_SELF_STATE_V1 (5.8.) — volná konverzace ještě NEZNAMENÁ
                # „bez faktů". Když se ptá NA HANSE („jak se máš?", „co jsi
                # dnes dělal?"), dej mu jeho VLASTNÍ dnešek z deníku. Bez toho
                # model plodil vatu („Službu plním, a to je pro mne
                # dostatečné") nebo komoleniny („historii zeleného, pana").
                # Nálada + její důvod už v promptu jsou (mood_ctx), tohle
                # dodává CO dnes reálně dělal. Detektor sdílený s agentem.
                try:
                    from scripts.hans_intent import is_about_self
                    if is_about_self(str(_text), self.config):
                        from scripts.hans_recall import self_state_facts
                        _dbp_ss = (self.config.get("diary_db")
                                   or (self.config.get("hans_idle", {}) or {}).get("diary_db")
                                   or "data/hans_diary.db")
                        _mo = _mr = ""
                        try:
                            _hi_m = getattr(self, '_hans_idle', None)
                            _mobj = getattr(_hi_m, '_mood', None) if _hi_m else None
                            if _mobj is not None:
                                _mo = getattr(_mobj, 'mood', '') or ''
                                _mr = getattr(getattr(_mobj, '_state', None),
                                              'shift_reason', '') or ''
                        except Exception as _tiche:
                            log_once(  # HANS_NO_SILENT_CTX_V1
                                logging.getLogger(__name__), "_build_grounding(ř. 847)",
                                "_build_grounding: blok kontextu selhal (ř. 847): %s", _tiche)
                        # HANS_SELF_STATE_AWAKE_V1 — dolož skutečný provozní
                        # stav (spánek/kamera/hlídání), ať si ho model nedomýšlí.
                        _rt_state = {}
                        try:
                            # ⚠️ Handler `_routine` NEMÁ — drží ho `hans_idle`
                            # (týž vzorec jako TIME_AWARENESS_V1 na ř. 1342).
                            # Přímé `getattr(self, "_routine")` by tiše vracelo
                            # None a stav by se do bloku nikdy nedostal.
                            _hi_rt = getattr(self, "_hans_idle", None)
                            _rt = getattr(_hi_rt, "_routine", None) if _hi_rt else None
                            if _rt is not None:
                                _rt_state["sleeping"] = bool(
                                    getattr(_rt, "_sleeping", False))
                            import json as _js_g
                            import os as _os_g
                            _gp = "data/.hans_guard"
                            if _os_g.path.exists(_gp):
                                with open(_gp, encoding="utf-8") as _gf:
                                    _rt_state["guard"] = bool(
                                        (_js_g.load(_gf) or {}).get("armed"))
                            else:
                                _rt_state["guard"] = False
                        except Exception as _rse:
                            logging.getLogger(__name__).debug(
                                'self_state runtime: %s', _rse)
                        _ss = self_state_facts(_dbp_ss, mood=_mo, mood_reason=_mr,
                                               runtime=_rt_state or None)
                        if _ss:
                            logging.getLogger(__name__).info(
                                'HANS_SELF_STATE_V1 → blok o sobě (%d zn)', len(_ss))
                            self._vysledek_groundingu('self_state', 'self_state')
                            return '\n\n' + _ss
                except Exception as _sse:
                    logging.getLogger(__name__).debug('self_state: %s', _sse)
                self._vysledek_groundingu('nonfactual', 'volny_hovor')
                return ''   # volná konverzace → osobnost, žádný retrieval

            # ── HANS_QUERY_REWRITER_F1_V1 ────────────────────────────────
            # Rewriter „člověk→počítač" na FAKTICKÉ CESTĚ: rozřeš odkazy,
            # oprav překlepy, strhni výplňky → vyčištěný explicitní dotaz
            # pro retrieval. Persona (chat generace) DÁL slyší raw text
            # výše ve volajícím — bytost, ne asistent. Deferral-safe: None
            # → drž se originálu (žádná změna chování).
            _q_for_retrieval = str(_text)
            try:
                from scripts.hans_rewriter import (
                    rewrite_for_retrieval as _f1_rewrite,
                    is_enabled as _f1_on)
                if _f1_on(self.config):
                    _hist = []
                    if name:
                        try:
                            # HANS_CHAT_CHANNEL_AWARE_V1 — měkký filtr
                            _ch = get_current_channel()
                            _hist = self.conv_store.get_history(name, channel=_ch) or []
                        except Exception:
                            _hist = []
                    _rw = _f1_rewrite(self.config, str(_text),
                                      history=_hist, name=name)
                    if _rw and _rw.strip() and _rw.strip() != str(_text).strip():
                        logging.getLogger(__name__).info(
                            'F1: rewrite %r -> %r',
                            str(_text)[:60], _rw[:60])
                        _q_for_retrieval = _rw.strip()
                        # HANS_A1_THREAD_TEXT_V1 — schovej pro A1 gate
                        self._f1_query = _q_for_retrieval
            except Exception as _f1e:
                logging.getLogger(__name__).debug(
                    'F1: rewriter selhal (%s) — použit originál', _f1e)

            # HANS_KODI_CAST_FACT_V2 (21.8.) — KDO V TOM HRAJE: odpověz
            # z KNIHOVNY, ne z hlavy. Doloženo 20.8.: „kdo tam hraje?" →
            # Hans vyjmenoval tři herce, o kterých nemá záznam; přitom Kodi
            # u dílu „Turecké náušnice" vrací šestnáct jmen včetně Josefa
            # Kemra, kterého uhodl správně a zbytek domyslel.
            # ⚠️ MUSÍ STÁT AŽ TADY, ZA PŘEPISEM DOTAZU. V1 seděla nad ním
            # a dostávala holou větu: `_thread_ctx` u „kdo tam hraje?" předmět
            # nenajde (změřeno: ('kdo tam hraje?','kdo tam hraje?','')),
            # kdežto F1 ho doplní na „Kdo hraje v Tureckých náušnicích?".
            # Blok byl celou dobu správný, jen stál před tím, kdo mu měl
            # název dodat — táž chyba jako HANS_A1_THREAD_TEXT_V1, kterou
            # jsem týž den opravoval o pár řádků výš.
            try:
                _cf = self._kodi_cast_fact(str(_q_for_retrieval))
                if _cf:
                    self._vysledek_groundingu('grounded', 'obsazeni_kodi')
                    return _cf
            except Exception as _tiche:
                log_once(  # HANS_NO_SILENT_CTX_V1
                    logging.getLogger(__name__), "_build_grounding(obsazeni)",
                    "_build_grounding: blok obsazení selhal: %s", _tiche)

            # C1: entity store — deterministické resolvování ZNÁMÉ entity
            # (z Hansova čtení) PŘED RAG. Autoritativní fakt (definiční věta
            # ze zdroje) → zabíjí kolizi jmen i konfabulaci významu.
            # HANS_PERSON_FACT_V1 — člen domácnosti má PŘEDNOST před obecnou
            # entitou i před RAG: je to tvrdý záznam, ne nález z četby.
            # HANS_SELF_STATE_AWAKE_V2 — vlastní režim má přednost úplně první:
            # je to tvrdý běhový fakt, ne nález z paměti.
            _ent_fact = (self._self_runtime_fact(str(_text))
                         or self._person_fact(str(_text))
                         or self._person_fact(_q_for_retrieval)
                         or self._entity_fact(_q_for_retrieval))

            # 2) vyber kolekce dle třídy (G3B_MULTICOLLECTION_V1 — list)
            collections = self._GROUNDING_COLLECTION.get(res.intent)
            if not collections:
                # C1 / HANS_PERSON_FACT_V1: i bez RAG kolekce máme-li tvrdý
                # fakt (entita nebo osoba), vrať ho
                if _ent_fact:
                    self._vysledek_groundingu('grounded', 'karta_osoby')
                    return '\n\n' + ANTIKONFAB + '\n\n' + _ent_fact
                return ''

            # 3) query VŠECHNY kolekce PARALELNĚ (fakta roztroušená).
            #    ThreadPool — query je síťový hop, vlákna se překryjí.
            #    Celý sken v jednom timeoutu (ne timeout na kolekci).
            import concurrent.futures as _cf
            all_chunks = []
            _skipped_chatlogs = []   # HANS_CHATLOG_NOT_FACT_V1
            try:
                with _cf.ThreadPoolExecutor(
                        max_workers=len(collections)) as _ex:
                    _futs = {
                        _ex.submit(_knowledge.query, _c,
                                   _q_for_retrieval,
                                   self._GROUNDING_K,
                                   self._GROUNDING_MAX_DISTANCE): _c
                        for _c in collections
                    }
                    _done, _pending = _cf.wait(
                        _futs, timeout=self._GROUNDING_TIMEOUT_S)
                    for _fut in _done:
                        try:
                            _b = _fut.result()
                            if _b and _b.found:
                                for _ch in _b.chunks:
                                    _ch = dict(_ch)
                                    _ch['collection'] = _futs[_fut]
                                    # HANS_CHATLOG_NOT_FACT_V1 (19.8.) — CO JSEM
                                    # ŘEKL NENÍ CO VÍM. Chatové výměny se ukládají
                                    # do `hans_pripady` (HANS_CHAT_RECALL_V1) a
                                    # faktická cesta je pak četla jako důkaz —
                                    # tedy Hansův vlastní výrok se mu vracel jako
                                    # znalost. Doloženo 19.8.: fabulovaný rok
                                    # vzniku divadla se uložil 7× a vracel se.
                                    # ⚠️ Pro `conversation_recall` zůstávají —
                                    # tam JSOU na místě („o čem jsme mluvili").
                                    # HANS_CHATLOG_NOT_FACT_V2 (22.8.) — `self.`
                                    # ⚠️ `_CHATLOG_RE` je ATRIBUT TŘÍDY; holé
                                    # jméno uvnitř metody je NameError, takže
                                    # filtr z 19.8. NIKDY neběžel. A protože ho
                                    # zdejší `except` spolkne, přišla o chunky
                                    # celá kolekce → RAG „nic nenašel" → padalo
                                    # se na FTS zápisky. Tudy přišel 21.8. do
                                    # podkladu o hradu Kost Pátý element.
                                    _txt = str(_ch.get('text') or '')
                                    if self._CHATLOG_RE.search(_txt[:120]):
                                        _skipped_chatlogs.append(1)
                                        continue
                                    all_chunks.append(_ch)
                        except Exception as _tiche:
                            log_once(  # HANS_NO_SILENT_CTX_V1
                                logging.getLogger(__name__), "_build_grounding(ř. 978)",
                                "_build_grounding: blok kontextu selhal (ř. 978): %s", _tiche)
                    if _pending:
                        logging.getLogger(__name__).info(
                            'G3B: %d/%d kolekcí nestihlo timeout %ss',
                            len(_pending), len(collections),
                            self._GROUNDING_TIMEOUT_S)
            except Exception as _qe:
                logging.getLogger(__name__).warning(
                    'G3B: multi-query selhalo: %s', _qe)
                return ''

            if _skipped_chatlogs:
                logging.getLogger(__name__).info(
                    'HANS_CHATLOG_NOT_FACT_V1: %d kusů z chatu vyřazeno '
                    'z faktického groundingu', len(_skipped_chatlogs))
            # 4) nic relevantního pod prahem → G3C: vrať aspoň anti-konfab
            #    (bez faktů). Faktický dotaz bez záznamů → Hans NESMÍ
            #    konfabulovat. Web ověření přijde post-hoc (G.5).
            if not all_chunks:
                # HANS_NOTES_BEFORE_ENTITY_V1 (21.8.) — VLASTNÍ ZÁPISKY MAJÍ
                # PŘEDNOST PŘED ENTITOU. Doloženo 20.8.: na dotaz o svatyni
                # u Nymburka (Hans o ní ráno četl a zapsal si ji) rozhodla
                # entitní větev a vrátila „ověřený fakt“ o SVATBĚ — entita se
                # trefila jen 4znakovým prefixem „svat“. Správný zápisek byl
                # přitom v FTS na prvním místě, ale FTS se volalo až POD tímhle
                # returnem, takže se k němu dotaz nikdy nedostal.
                # Pořadí je teď: co jsem sám četl a zapsal > slovníková glosa.
                # Změřeno: kde entita rozhoduje správně (Sorge, Jiří z Poděbrad,
                # Gotika), míří zápisky na tentýž předmět → žádná ztráta C1;
                # kde zápisky nejsou (Secese), rozhodne dál entita.
                # HANS_FTS_USES_REWRITE_V1 (21.8.) — hledej v zápiscích podle
                # OPRAVENÉ věty, ne syrové. Holé „kdo tu knihu napsal?" nenese
                # název a fulltext na něj trefí cizí knihu (změřeno: Murakami);
                # F1 ho doplní z vlákna. Potřetí týž vzorec za den.
                _kb = self._knowledge_fts_grounding(
                    str(_q_for_retrieval or _text))
                if _kb:
                    self._vysledek_groundingu('grounded', 'zapisky_pred_entitou')
                    return _kb
                # C1: RAG prázdné, ale entita ve store → autoritativní fakt
                # (Sorge není v RAG, ale Hans o něm četl → deterministický fakt).
                if _ent_fact:
                    logging.getLogger(__name__).info(
                        'C1: RAG prázdné, entita ze store → grounded pro %r',
                        str(_text)[:40])
                    # nálepka byla `chatlog_neni_fakt` — s filtrem chatlogů to
                    # nemá nic společného a 20.8. to svedlo diagnózu na RAG.
                    self._vysledek_groundingu('grounded', 'entita_c1')
                    return '\n\n' + ANTIKONFAB + '\n\n' + _ent_fact
                # HANS_KNOWLEDGE_FTS_V1 — tudy vede REÁLNÁ cesta k abstinenci
                # (ověřeno v logu 6.8.: „žádná shoda pod prahem → G3C").
                # Původní patch mířil jen na druhé místo níž a NIC neopravil.
                # HANS_FTS_USES_REWRITE_V1 (21.8.) — hledej v zápiscích podle
                # OPRAVENÉ věty, ne syrové. Holé „kdo tu knihu napsal?" nenese
                # název a fulltext na něj trefí cizí knihu (změřeno: Murakami);
                # F1 ho doplní z vlákna. Potřetí týž vzorec za den.
                _kb = self._knowledge_fts_grounding(
                    str(_q_for_retrieval or _text))
                if _kb:
                    self._vysledek_groundingu('grounded', 'zapisky_fts')
                    return _kb
                logging.getLogger(__name__).info(
                    'G3B: žádná shoda pod prahem pro [%s] %r → anti-konfab bez fakt (G3C)',
                    res.intent, str(_text)[:40])
                self._vysledek_groundingu('factual_nofacts', 'zapisky_fts_prazdno')
                return '\n\n' + ANTIKONFAB_NOFACTS

            # 5) seřaď VŠECHNY chunky napříč kolekcemi dle distance,
            #    vezmi nejlepší K (mix kolekcí). distance = společné
            #    měřítko (stejný embedding bge-m3) → férové porovnání.
            all_chunks.sort(
                key=lambda c: (c.get('distance') is None,
                               c.get('distance') if c.get('distance')
                               is not None else 9e9))
            top = all_chunks[:self._GROUNDING_K]
            _best_dist = top[0].get('distance') if top else None

            # HANS_RAGFIRST_STRICT_V1 (#2) — přísný TOP práh.
            # Když nejlepší chunk je NAD strict_max (borderline zóna
            # 0.70-0.75), RAG je slabý = neber ho jako grounding.
            # Autoritativní zdroje (entity/karta) zůstávají — mají vlastní
            # ověření (jméno v textu / definiční věta z Hansova čtení).
            _strict_max = float(
                (self.config.get('grounding', {}) or {})
                .get('strict_max_distance', self._GROUNDING_STRICT_MAX))
            _rag_weak = (_best_dist is None) or (_best_dist > _strict_max)
            if _rag_weak:
                top = []  # zahoď slabé chunky
                _facts_from_rag = ''
                logging.getLogger(__name__).info(
                    '#2: RAG slabý (best=%.3f > strict=%.3f) → chunky zahozeny',
                    _best_dist if _best_dist is not None else -1, _strict_max)
            else:
                # HANS_PROVENANCE_V1 — každý chunk dostane značku původu:
                # per-chunk provenance z metadata (přesné), fallback kolekce.
                # hans_denik → 'nejisté' (míchá prožitky se sny/úvahami) →
                # Hans to netvrdí jako jistou vzpomínku.
                try:
                    from scripts import hans_provenance as _prov
                    _prov_on = (self.config.get('provenance', {}) or {}).get(
                        'enabled', True)
                except Exception:
                    _prov_on = False
                    _prov = None
                _rag_lines = []
                for c in top:
                    _t = c.get('text')
                    if not _t:
                        continue
                    if _prov_on and _prov is not None:
                        _cls = c.get('provenance') or \
                            _prov.provenance_of_collection(c.get('collection'))
                        _rag_lines.append(f"{_prov.marker(_cls)} {_t}")
                    else:
                        _rag_lines.append(_t)
                _facts_from_rag = '\n\n'.join(_rag_lines)

            # G5A_IDENTITY_GROUNDING_V3 — vztahová karta z DB jako
            # PRIORITNÍ pravda. Adresujeme podle jména (NE embedding),
            # tvrdá data (role+rodina, BEZ characterization=starý tón).
            # F1 pomáhá: rewriter rozřeší 'kdo je on' → jméno v textu.
            _card_fact = self._build_card_fact(_q_for_retrieval)
            if _card_fact:
                logging.getLogger(__name__).info(
                    'G5A: karta vstříknuta z DB → priorita')

            # Skládání priorit: entita (autoritativní) > karta > RAG chunky.
            _parts = []
            if _ent_fact:
                _parts.append(_ent_fact)
            if _card_fact:
                _parts.append(_card_fact)
            if _facts_from_rag:
                _parts.append(_facts_from_rag)
            facts = '\n\n'.join(_parts)

            if not facts.strip():
                # HANS_FTS_USES_REWRITE_V1 (21.8.) — hledej v zápiscích podle
                # OPRAVENÉ věty, ne syrové. Holé „kdo tu knihu napsal?" nenese
                # název a fulltext na něj trefí cizí knihu (změřeno: Murakami);
                # F1 ho doplní z vlákna. Potřetí týž vzorec za den.
                _kb = self._knowledge_fts_grounding(
                    str(_q_for_retrieval or _text))
                if _kb:
                    self._vysledek_groundingu('grounded', 'zapisky_fallback')
                    return _kb
                # RAG slabé A žádný autoritativní zdroj = jako by prázdné.
                logging.getLogger(__name__).info(
                    '#2: bez faktů (RAG slabý, žádná entita/karta) → factual_nofacts')
                self._vysledek_groundingu('factual_nofacts', 'bez_faktu')
                return '\n\n' + ANTIKONFAB_NOFACTS

            _cols_used = sorted(set(c.get('collection', '?') for c in top))
            logging.getLogger(__name__).info(
                'G3B: grounding [%s] best=%.3f, %d chunků z %s, ent=%d card=%d → kontext',
                res.intent, _best_dist if _best_dist is not None else -1,
                len(top), '+'.join(_cols_used) if _cols_used else '-',
                1 if _ent_fact else 0, 1 if _card_fact else 0)
            self._vysledek_groundingu('grounded', 'rag')
            return '\n\n' + ANTIKONFAB + '\n\n' + facts

        except Exception as _ge:
            logging.getLogger(__name__).warning(
                'G3B: grounding selhalo (%s) — odpovídám bez fakt', _ge)
            return ''

    def _entity_store(self):
        # HANS_ENTITY_STORE_C1_V1 — lazy singleton EntityStore
        _es = getattr(self, "_es_inst", None)
        if _es is not None:
            return _es
        try:
            from scripts.hans_entities import EntityStore
            _dbp = (self.config.get("diary_db")
                    or (self.config.get("hans_idle", {}) or {}).get("diary_db")
                    or "data/hans_diary.db")
            self._es_inst = EntityStore(self.config, _dbp)
        except Exception:
            self._es_inst = None
        return self._es_inst

    def _entity_fact(self, text: str) -> str:
        """HANS_ENTITY_STORE_C1_V1 — deterministicky resolvuj entitu z dotazu
        proti store známých entit (z Hansova čtení). Vrátí autoritativní fakt
        (definiční věta ze zdroje) nebo '' když nic. Zabíjí kolizi jmen
        (Sorge=skladatel, ne špión) i konfabulaci významu známých entit."""
        try:
            _es = self._entity_store()
            if _es is None:
                return ''
            _ent = _es.resolve(str(text))
            if not _ent:
                return ''
            logging.getLogger(__name__).info(
                'C1: entita resolvována z dotazu → %r (ev=%s)',
                _ent.get('name'), _ent.get('evidence_count'))
            return _es.fact_block(_ent)
        except Exception:
            return ''

    # HANS_PERSON_FACT_V1 (7.8.) — dotaz na OSOBU domácnosti.
    # Jen otázky na IDENTITU („kdo je X", „co víš o X"), NE na přítomnost
    # („je X doma?") — tu obsluhuje agentní akce z živých dat.
    _PERSON_Q_PAT = re.compile(
        r"(kdo\s+(to\s+)?je|kdo\s+(to\s+)?byl[ao]?|co\s+v[íi][sš]\s+o|"
        r"[rř]ekni\s+mi\s+o|pov[ěe]z\s+mi\s+o|popi[sš]\s+mi|kdo\s+to\s+"
        r"vlastn[ěe]\s+je)", re.IGNORECASE)

    # HANS_SELF_STATE_AWAKE_V2 (7.8.) — dotaz na VLASTNÍ PROVOZNÍ REŽIM.
    # V1 dal stav jen do NEfaktické větve (`is_about_self`), jenže „jsi
    # v režimu spánku?" intent klasifikuje jako FAKTICKÝ → blok se nezapojil
    # a model si režim dál vymýšlel (živý test 12:44: „Jsem v režimu spánku?
    # Chcete abych usnul?" a „Připravím systém na režim spánku"). Právě proto,
    # že je to faktický dotaz, se má odpovídat ze STAVU, ne z RAG.
    # Levný regex místo LLM klasifikátoru — faktická cesta jde na každou větu.
    _SELF_RUNTIME_PAT = re.compile(
        r"(sp[íi][sš]|span[ke]|sp[áa]nk|vzh[uů]ru|bd[íi][sš]|"
        r"hl[íi]d[áa][sš]|hl[íi]d[áa]n|kameru?\b|vid[íi][sš]\b|"
        r"re[žz]im\w*)", re.IGNORECASE)

    def _self_runtime_fact(self, text: str) -> str:
        """Deterministický blok o Hansově vlastním režimu (spánek/kamera/hlídání).

        Vrací '' když se věta režimu netýká. Jinak fakta + zákaz tvrdit, že
        něco přepíná — sám to neumí, mění se to na povel (`/sleep`, `/hlidej`).
        """
        t = (text or "").strip()
        if not t or not self._SELF_RUNTIME_PAT.search(t):
            return ''
        st = self._runtime_state()
        if not st:
            return ''
        lines = []
        if st.get("sleeping") is not None:
            lines.append("spím (noční režim)" if st["sleeping"]
                         else "jsem vzhůru, v běžném provozu")
        if st.get("guard") is not None:
            lines.append("hlídací režim je zapnutý" if st["guard"]
                         else "hlídací režim je vypnutý")
        if not lines:
            return ''
        logging.getLogger(__name__).info(
            'HANS_SELF_STATE_AWAKE_V2: dotaz na vlastní režim → %s', lines)
        return ("MŮJ SKUTEČNÝ REŽIM PRÁVĚ TEĎ (odpověz POUZE podle tohohle):\n"
                + "\n".join("- %s" % x for x in lines)
                + "\nNikdy netvrď, že něco přepínáš nebo jsi přepnul — režim "
                  "sám měnit neumím, děje se to na povel uživatele.")

    def _runtime_state(self) -> dict:
        """HANS_SELF_STATE_AWAKE_V1 — skutečný provozní stav Hanse.
        ⚠️ Handler `_routine` NEMÁ — drží ho `hans_idle` (vzorec z
        TIME_AWARENESS_V1, ř. 1342); přímý `getattr(self, "_routine")` by
        tiše vracel None a stav by se nikam nedostal."""
        out = {}
        try:
            _hi = getattr(self, "_hans_idle", None)
            _rt = getattr(_hi, "_routine", None) if _hi else None
            if _rt is not None:
                out["sleeping"] = bool(getattr(_rt, "_sleeping", False))
            import json as _js_g
            import os as _os_g
            _gp = "data/.hans_guard"
            if _os_g.path.exists(_gp):
                with open(_gp, encoding="utf-8") as _gf:
                    out["guard"] = bool((_js_g.load(_gf) or {}).get("armed"))
            else:
                out["guard"] = False
        except Exception as _rse:
            logging.getLogger(__name__).debug('runtime_state: %s', _rse)
        return out

    @staticmethod
    def _fold(s: str) -> str:
        import unicodedata
        return "".join(c for c in unicodedata.normalize("NFKD", (s or "").lower())
                       if not unicodedata.combining(c))

    def _person_fact(self, text: str) -> str:
        """Deterministický fakt o členovi domácnosti z `relationships`.

        Vrací blok pro grounding, nebo '' když dotaz není na identitu osoby
        / osoba není známá. Shoda na PREFIX (4 znaky, bez diakritiky), aby
        prošlo skloňování (2.–7. pád) i psaní bez háčků — právě rozdíl
        „jméno bez diakritiky" × „s diakritikou" dnes rozhodoval mezi
        zapřením a výmyslem. Deaktivované záznamy (testovací osoby) se přeskakují.
        """
        t = (text or "").strip()
        if not t or not self._PERSON_Q_PAT.search(t):
            return ''
        try:
            import sqlite3 as _sql
            _dbp = (self.config.get("diary_db")
                    or (self.config.get("hans_idle", {}) or {}).get("diary_db")
                    or "data/hans_diary.db")
            db = _sql.connect("file:%s?mode=ro" % _dbp, uri=True, timeout=3.0)
            rows = db.execute(
                "SELECT person_id, display_name, role, family_links, "
                "characterization FROM relationships "
                "WHERE COALESCE(deactivated_at, 0) = 0").fetchall()
            db.close()
        except Exception as e:
            logging.getLogger(__name__).debug('person_fact: %s', e)
            return ''
        toks = [w for w in re.split(r"[^\w]+", self._fold(t)) if len(w) >= 3]
        hit = None
        for pid, disp, role, links, charact in rows:
            for cand in (disp, pid):
                fc = self._fold(cand)
                # Shoda na TŘI znaky + strop na rozdíl délek. Čtyři znaky
                # nestačí — česká deklinace u krátkých jmen mění právě
                # 4. písmeno (dativ), takže 4-znakový prefix ty tvary zahodí
                # (změřeno). Tři znaky samy o sobě pouštějí i cizí slova,
                # proto délková pojistka: obojí musí být zhruba stejně dlouhé.
                # Zbylý falešný poplach stojí jen jeden blok navíc v groundingu
                # a spouští se výhradně u otázek na identitu — proto se loguje.
                if len(fc) < 3:
                    continue
                if any(w[:3] == fc[:3] and abs(len(w) - len(fc)) <= 3
                       for w in toks):
                    hit = (pid, disp, role, links, charact)
                    break
            if hit:
                break
        if not hit:
            return ''
        pid, disp, role, links, charact = hit
        parts = ["%s — %s" % (disp or pid, role or "člen domácnosti")]
        try:
            import json as _js
            fam = _js.loads(links or "{}") or {}
            names = {r[0]: (r[1] or r[0]) for r in rows}
            if fam.get("parents"):
                parts.append("rodiče: %s" % ", ".join(
                    names.get(p, p) for p in fam["parents"]))
            if fam.get("children"):
                parts.append("děti: %s" % ", ".join(
                    names.get(c, c) for c in fam["children"]))
            if fam.get("spouse"):
                parts.append("partner: %s" % names.get(fam["spouse"], fam["spouse"]))
        except Exception:
            pass
        if charact:
            parts.append((charact or "").strip().split(". ")[0].strip() + ".")
        logging.getLogger(__name__).info(
            'HANS_PERSON_FACT_V1: osoba resolvována z dotazu → %r', disp or pid)
        return "ZÁZNAM O OSOBĚ (z mé evidence domácnosti):\n" + "\n".join(
            "- %s" % p for p in parts)

    def _thread_store(self):
        # HANS_THREADS_SURFACING_V1 — lazy singleton ThreadStore
        _ts = getattr(self, "_threads", None)
        if _ts is not None:
            return _ts
        try:
            from scripts.hans_threads import ThreadStore
            _dbp = (self.config.get("diary_db")
                    or (self.config.get("hans_idle", {}) or {}).get("diary_db")
                    or "data/hans_diary.db")
            self._threads = ThreadStore(self.config, _dbp)
        except Exception:
            self._threads = None
        return self._threads

    def _place_store(self):
        # HANS_PLACE_V1 — lazy singleton PlaceStore (smysl pro místo)
        _ps = getattr(self, "_place", None)
        if _ps is not None:
            return _ps
        try:
            from scripts.hans_place import PlaceStore
            _dbp = (self.config.get("diary_db")
                    or (self.config.get("hans_idle", {}) or {}).get("diary_db")
                    or "data/hans_diary.db")
            self._place = PlaceStore(self.config, _dbp)
        except Exception:
            self._place = None
        return self._place

    def _agent_router(self):
        # HANS_AGENT_V1 — lazy singleton AgentRouter (kontextové akce).
        # None když vypnuto → volající přeskočí na běžný chat.
        _ar = getattr(self, "_agent_inst", None)
        if _ar is not None:
            return _ar if _ar is not False else None
        try:
            from scripts.hans_agent import AgentRouter
            _inst = AgentRouter(self.config)
            self._agent_inst = _inst if _inst.enabled else False
        except Exception:
            self._agent_inst = False
        _ar = self._agent_inst
        return _ar if _ar is not False else None

    def _questions_store(self):
        # HANS_QUESTIONS_SURFACING_V1 — lazy singleton HansQuestionsStore
        _qs = getattr(self, "_qstore_inst", None)
        if _qs is not None:
            return _qs
        try:
            from scripts.hans_questions import HansQuestionsStore
            _dbp = (self.config.get("diary_db")
                    or (self.config.get("hans_idle", {}) or {}).get("diary_db")
                    or "data/hans_diary.db")
            self._qstore_inst = HansQuestionsStore(_dbp, self.config)
        except Exception:
            self._qstore_inst = None
        return self._qstore_inst

    def _maybe_surface_question(self, name: str):
        # HANS_QUESTIONS_SURFACING_V1 — text čekající otázky pro osobu (greeting
        # i chat) s globálním cooldownem proti vyptávání. Po výběru označí asked
        # (self-limiting). None když nic / cooldown / chyba.
        try:
            _cfg = (self.config.get("hans_questions", {}) or {})
            _cd_h = float(_cfg.get("greeting_cooldown_h", 4.0))
            _last = getattr(self, "_last_q_surfaced_ts", 0.0)
            if (time.time() - _last) < _cd_h * 3600.0:
                return None
            _qs = self._questions_store()
            if _qs is None:
                return None
            # HANS_PERSONAL_QUESTIONS_V1 — osobní otázky mají lehkou přednost
            # HANS_QUESTIONS_ROUTING_V1 — volitelný channel filtr: popup u
            # kamery předává channel='popup' (bere jen otázky ve fázi popup);
            # chat-weaving (default None) bere jakoukoli pending fázi.
            _ch = getattr(self, "_surface_channel_filter", None)
            _q = (_qs.next_for_person(name, source_type="personal", channel=_ch)
                  or _qs.next_for_person(name, channel=_ch))
            if _q is None:
                return None
            _qs.mark_asked_voice(_q.id)
            self._last_q_surfaced_ts = time.time()
            return _q  # HANS_QUESTION_POPUP_V1 — vrací Question (kvůli .id)
        except Exception:
            return None

    def open_thread_popup(self, person: str, text: str) -> bool:
        # PROACTIVE_THREAD_POPUP_V1 — otevře popup naseedovaný textem (nitka
        # už byla vyslovena TTS jinde → okno text jen ZOBRAZÍ, NEmluví znovu).
        try:
            if not (text or "").strip():
                return False
            from scripts.popup_chat_window import SimplePopupChat
            SimplePopupChat(self, person, 1.0, already_greeted=True,
                            initial_question=text)
            return True
        except Exception as _e:
            print(f"[Chat] open_thread_popup failed: {_e}")
            return False

    def ask_question_via_popup(self, person: str) -> bool:
        # HANS_QUESTION_POPUP_V1 — Hans aktivně položí čekající otázku osobě:
        # vysloví ji (TTS) a otevře chat okno s otázkou + čeká na odpověď.
        # HANS_QUESTIONS_ROUTING_V1 — popup cesta bere jen otázky ve fázi 'popup'.
        try:
            self._surface_channel_filter = "popup"
            try:
                _q = self._maybe_surface_question(person)
            finally:
                self._surface_channel_filter = None
            if _q is None:
                return False
            _qtext = _q.question
            # HANS_QUESTION_CONTINUITY_V1 — zapiš položenou otázku do conv_store,
            # aby navazující odpověď měla kontext (jinak Hans odpoví naslepo).
            try:
                self.conv_store.add_greeting(person, _qtext)
            except Exception:
                pass
            _tts = getattr(self, "tts_speaker", None)
            if _tts is not None and getattr(_tts, "enabled", False):
                try:
                    _tts.speak(_qtext, priority=False)
                except Exception:
                    pass
            from scripts.popup_chat_window import SimplePopupChat
            SimplePopupChat(self, person, 1.0, already_greeted=True,
                            initial_question=_qtext, question_id=_q.id)
            return True
        except Exception as _e:
            print(f"[Chat] ask_question_via_popup failed: {_e}")
            return False

    def _build_system(self, name: str, for_greeting: bool = False,
                      user_msg: str = "") -> str:
        # PERSONA_REFACTOR_1_4 — jednotný zdroj identity
        from scripts.hans_persona import persona_core
        system_base = persona_core(self.config)
        # Known persons
        known = self.config.get("known_persons", {})
        if known:
            lines = []
            for pname, pdata in known.items():
                if isinstance(pdata, dict):
                    g     = pdata.get("gender", "")
                    notes = pdata.get("notes", "").strip()
                    line  = f"- {pname}"
                    if g == "žena":  line += " (ženského rodu)"
                    elif g == "muž": line += " (mužského rodu)"
                    if notes:        line += f": {notes}"
                else:
                    line = f"- {pname}"
                # HANS_ADDRESSEE_V2 (4.8.) — v seznamu VYZNAČ partnera. Bez toho
                # je to jen soupis jmen s rody a model si adresáta vybere sám
                # (doloženo: uživatel se ptal „jak se mas?", Hans odpověděl
                # „Odpovím vám, paní Jano" — oslovil nepřítomnou třetí osobu).
                if name and pname == name:
                    line += "  ← S TOUTO OSOBOU PRÁVĚ MLUVÍŠ"
                lines.append(line)
            persons_ctx = "\n\nZnáš tyto osoby z domu:\n" + "\n".join(lines)
        else:
            persons_ctx = ""
        # HANS_GAME_LAUNCH_ATTRIB_V1 — oblíbená hra osoby, se kterou Hans mluví
        if not for_greeting and name:
            _fav = self._favorite_game(name)
            if _fav:
                persons_ctx += (f"\n\n{name} rád(a) hraje na PC: „{_fav}" + "\""
                                " (často to spouští). Můžeš to přirozeně zmínit, "
                                "nevnucuj.")
        # ── HANS_CTX_RELEVANCE_V1 (19.8.) — ptá se blok, jestli je k něčemu? ──
        # Změřeno na 12 různých dotazech: system prompt měl VŽDY ~14 350 zn
        # (rozptyl 80 zn) a 22 z 23 bloků bylo přítomno pokaždé. Kontext se
        # tedy neřídil otázkou — a grounding (pár set zn) v té zdi zanikl:
        # týž dotaz odpověděl v izolaci správně, živě si vymýšlel rok.
        # ⚠️ PŘI POCHYBNOSTI VKLÁDAT. Radši delší prompt než ztracená schopnost.
        _relf = (user_msg or "").lower()
        try:
            import unicodedata as _u
            _relf = "".join(c for c in _u.normalize("NFKD", _relf)
                            if not _u.combining(c))
        except Exception as _tiche:
            log_once(  # HANS_NO_SILENT_CTX_V1
                logging.getLogger(__name__), "_build_system(ř. 1477)",
                "_build_system: blok kontextu selhal (ř. 1477): %s", _tiche)
        import re as _rre
        _is_knowledge_q = bool(_rre.search(
            r"\b(co\s+(je|jsou|byl|byla)|kdo\s+(je|byl)|co\s+vis|co\s+ses|"
            r"proc|jak\s+(vznikl|funguje))\b", _relf))
        _asks_ability = bool(_rre.search(
            r"\b(umis|umite|dokazes|zvladnes|schopnost|co\s+vsechno|nauc|"
            r"namaluj|namalujes|napis|pust|zapni|vypni|pridej|nastuduj|udelej|"
            r"zaridis|muzes)\b", _relf))
        _about_tv = bool(_rre.search(
            r"\b(tv|televiz|kodi|film|serial|poust|hraje|sledova|div[áa])", _relf))
        _about_kolac = bool(_rre.search(r"(kolac|plysak|medv)", _relf))
        _about_self_day = bool(_rre.search(
            r"(co\s+jsi\s+delal|jak\s+se\s+mas|co\s+je\s+u\s+tebe|jak\s+ses)",
            _relf))

        # HANS_CAPABILITY_AWARENESS_V1 — Hans ví, co reálně umí (nabízet/dělat,
        # ne odmítat). Faktický seznam. Jen full mód (pozdrav drží brevitu).
        # HANS_CTX_RELEVANCE_V1 — u ČISTĚ ZNALOSTNÍHO dotazu se vynechává
        # (3 021 zn = 21 % promptu, a na „co je zajímavého na gotice" nemá vliv).
        # ⚠️ U ŽÁDOSTI zůstává: tenhle blok vznikl proto, že Hans odmítl malovat
        # s tím, že „nemá umělecké sklony" (2.7.) — a to se nesmí vrátit.
        # HANS_CAP_NOT_FOR_PAST_V1 (20.8.) — VÝČET SCHOPNOSTÍ SE NEVKLÁDÁ
        # K OTÁZCE NA MINULOST. Doloženo: na „co jsi dělal v noci?" Hans
        # tvrdil „byl jsem v režimu hlídání", ačkoli hlídání bylo vypnuté.
        # Zdrojem byl právě tenhle blok — stojí v něm „Umím HLÍDAT místnost,
        # když nejste doma" a model si „umím" přečetl jako „dělal jsem".
        # Co dnes dělal, říká blok o sobě (`self_state`); výčet schopností
        # k tomu nepřidává nic než pokušení.
        # ⚠️ U ŽÁDOSTI zůstává (`_asks_ability`) — blok vznikl proto, že Hans
        # odmítl malovat s tím, že „nemá umělecké sklony", a to se nesmí vrátit.
        cap_ctx = ""
        if not for_greeting and (_asks_ability
                                 or (not _is_knowledge_q and not _about_self_day)):
            try:
                from scripts.hans_capabilities import (
                    capabilities_context, recent_gained_context)
                cap_ctx = capabilities_context()
                # HANS_CAPABILITY_AWARENESS_V1 (V2) — nedávno získané schopnosti
                _capdb = (self.config.get("hans_idle", {}) or {}).get(
                    "diary_db", "data/hans_diary.db")
                cap_ctx += recent_gained_context(_capdb)
            except Exception:
                cap_ctx = cap_ctx or ""

        # Hans dialog s plysákem
        # HANS_CTX_RELEVANCE_V1 — jen když na Koláče přijde řeč nebo se ptáme,
        # co Hans dělal; k dotazu na knihu či počasí nepřispívá (742 zn).
        teddy_ctx = ""
        _hd = getattr(self, '_hans_dialog', None)
        if _hd and (_about_kolac or _about_self_day or not _is_knowledge_q):
            _teddy = _hd.get_last_dialog()
            if _teddy:
                teddy_ctx = '\n\n' + _teddy

        # Popis mistnosti
        room_ctx = ""
        _ro = getattr(self, '_room_observer', None)
        if _ro:
            _room = _ro.get_context_string()
            if _room:
                room_ctx = '\n\n' + _room

        # HANS_PLACE_V1 — smysl pro místo „kde jsem" (groundovaný model domova).
        # Počasí vetkneme jako „za oknem" (živé groundování), když okno znám.
        # Do POZDRAVU se model místa NEdává (na přání uživatele — brevita).
        place_ctx = ""
        try:
            _ps = self._place_store() if not for_greeting else None
            if _ps is not None:
                _wx = getattr(self, '_weather', None)
                _wx_str = _wx.get_context_string() if _wx else None
                _place = _ps.get_context_string(weather_str=_wx_str)
                if _place:
                    place_ctx = '\n\n' + _place
        except Exception:
            place_ctx = ""

        # HANS_CALENDAR_V1 — nadcházející události z kalendáře TÉTO osoby (full mód).
        # Soukromí: ukáže jen kalendář osoby, se kterou Hans mluví (name).
        cal_ctx = ""
        try:
            from scripts.hans_calendar import is_enabled, CalendarStore
            if not for_greeting and name and is_enabled(self.config):
                _dbp = (self.config.get("diary", {}) or {}).get(
                    "db_path", "data/hans_diary.db")
                _cs = CalendarStore(self.config, _dbp).context_string(
                    name, hours=72)
                if _cs:
                    cal_ctx = "\n\n" + _cs
        except Exception:
            cal_ctx = ""

        # Aktuální čas + fáze dne (TIME_AWARENESS_V1)
        _hi = getattr(self, '_hans_idle', None)
        time_ctx = ""
        try:
            _rt = getattr(_hi, '_routine', None) if _hi else None
            _now = datetime.now()
            _DNY = ('pondělí','úterý','středa','čtvrtek','pátek','sobota','neděle')
            _lbl = _rt.phase_label if _rt else ""
            _lbl = f"{_lbl}, " if _lbl else ""
            _slovy = _cz_clock_words(_now.hour, _now.minute)
            try:
                from scripts.cz_names import greeting_for_hour
                _pozdrav = greeting_for_hour(_now.hour)
            except Exception:
                _pozdrav = "Dobrý den"
            # HANS_DATE_WORDS_V1 (19.8.) — DATUM MUSÍ PŘIJÍT UŽ ROZEPSANÉ.
            # Čas se posílá slovy (`_cz_clock_words`) a model ho opakuje
            # SPRÁVNĚ; datum dostával jen číslicemi a rozepisoval si ho sám —
            # a to hans-czech neumí (táž slabina jako „roku devětadvacátého"
            # místo 1929). Doloženo 19.8. v testu očima cizího člověka: Hans
            # tvrdil „sobota, patnáctého srpna roku dvoutisíc šestého", ačkoli
            # byla středa 19. 8. 2026 — a to i na PŘÍMÝ dotaz.
            # Protidůkaz ze stejného hovoru: deterministická cesta (shrnutí
            # konverzace) datum uvedla správně, protože ho neskládal model.
            # Tohle tedy NENÍ další instrukce do promptu, ale odebrání úlohy,
            # kterou model neumí ([[prompt-debt-tool-calling]]).
            _dnes_slovy = ""
            try:
                from scripts.cz_numbers import normalize as _cz_norm
                _dnes_slovy = _cz_norm(
                    f"{_now.day}.{_now.month}.{_now.year}").strip()
            except Exception:
                _dnes_slovy = ""   # bez modulu zůstane dnešní text s číslicemi
            time_ctx = (f"\n\nTeď je {_lbl}{_DNY[_now.weekday()]} "
                        f"{_now.day}.{_now.month}.{_now.year}"
                        + (f", slovy {_dnes_slovy}" if _dnes_slovy else "")
                        + f". Přesný čas je {_now:%H:%M}, tedy {_slovy}. "
                        f"Tento čas a datum ber jako fakt, neodhaduj je."
                        # HANS_GREETING_BY_HOUR_V1 (20.8.) — hotový pozdrav.
                        # Čas v promptu byl, ale model si z něj tvar pozdravu
                        # neodvodil („Dobrý večer" v 11:50). Odvození se mu
                        # tedy odebere — stejně jako u data rozepsaného slovy.
                        + f" Když zdravíš, patří teď „{_pozdrav}“.")
        except Exception:
            time_ctx = ""

        # Hans deník
        diary_ctx = ""
        _hi = getattr(self, '_hans_idle', None)
        if _hi:
            _diary = _hi.get_diary_context(max_age_h=24)
            if _diary:
                diary_ctx = '\n\n' + _diary

        # PERSONA_READS_NARRATIVE_V1 — nejnovější kapitola životního příběhu
        # (kontinuita identity; read-only, nikdy neshodí chat)
        story_ctx = ""
        try:
            from scripts.hans_narrative import latest_chapter
            _dbp = (self.config.get("diary_db")
                    or (self.config.get("hans_idle", {}) or {}).get("diary_db")
                    or "data/hans_diary.db")
            _chap = latest_chapter(_dbp)
            if _chap:
                story_ctx = ("\n\nKdo se ze mě postupně stává (má poslední "
                             "autobiografická reflexe — vnitřní kontinuita, "
                             "necituj ji doslovně, jen z ní vychází tvůj tón): "
                             + _chap)
        except Exception:
            story_ctx = ""

        # HANS_STUDY_SURFACING_V1 (#2) — Hans přirozeně zmíní svůj studijní
        # program (co studuje / co se dozvěděl). Jen full mód, ne greeting
        # (brevita). Read-only, graceful.
        study_ctx = ""
        if not for_greeting:
            try:
                from scripts.hans_study import study_context_string
                _dbp2 = (self.config.get("diary_db")
                         or (self.config.get("hans_idle", {}) or {}).get("diary_db")
                         or "data/hans_diary.db")
                _sc = study_context_string(self.config, _dbp2)
                if _sc:
                    study_ctx = ("\n\nMé soukromé studium (zmiň jen když to "
                                 "přirozeně zapadne, nevnucuj): " + _sc)
            except Exception:
                study_ctx = ""

        # HANS_DIRECTION_V1 — můj vlastní zvolený SMĚR (dopředná aspirace).
        # Dává tón „k čemu vědomě rostu"; na dotaz „kam směřuješ" ať odpoví
        # tímhle, ne konfabulací. Jen full mód, read-only, graceful.
        direction_ctx = ""
        if not for_greeting:
            try:
                from scripts.hans_direction import active_direction_line
                _dbd = (self.config.get("diary_db")
                        or (self.config.get("hans_idle", {}) or {}).get("diary_db")
                        or "data/hans_diary.db")
                _dl = active_direction_line(self.config, _dbd)
                if _dl:
                    direction_ctx = ("\n\nMůj vlastní zvolený směr (k čemu "
                                     "vědomě rostu; zmiň, když se ptají kam "
                                     "směřuji nebo co chci dělat dál): " + _dl)
            except Exception:
                direction_ctx = ""

        # HANS_SYNTHESIS_IDEAS_V1 (#2) — poslední vlastní postřeh (propojení věcí
        # z různých oblastí). Jen full mód, ne pozdrav (brevita). Read-only, graceful.
        idea_ctx = ""
        if not for_greeting:
            try:
                from scripts.hans_ideas import latest_idea_context
                _dbp3 = (self.config.get("diary_db")
                         or (self.config.get("hans_idle", {}) or {}).get("diary_db")
                         or "data/hans_diary.db")
                _ic = latest_idea_context(self.config, _dbp3)
                if _ic:
                    idea_ctx = ("\n\nMůj nedávný vlastní postřeh (zmiň jen když to "
                                "přirozeně zapadne, nevnucuj): " + _ic)
            except Exception:
                idea_ctx = ""

        # Kodi kontext
        kodi_ctx = ""
        # HANS_CTX_RELEVANCE_V1 — co běží na TV je u čistě znalostního dotazu
        # („co je zajímavého na gotice") jen šum za 871 zn. U dotazu na TV,
        # film či sledování se vkládá dál.
        _km = getattr(self, '_kodi_monitor', None)
        if _km and (_about_tv or not _is_knowledge_q):
            _now_playing = _km.get_now_playing_context()
            _history     = _km.get_person_history(name)
            _events      = _km.get_today_events()
            _kodi_parts  = [x for x in [_now_playing, _history, _events] if x]
            if _kodi_parts:
                kodi_ctx = '\n\n' + '\n'.join(_kodi_parts)

        # Surroundings
        surr_ctx = ""
        if self.surroundings_db:
            try:
                # Zjisti aktualne viditelne osoby
                _vis = getattr(self, "_visible_persons", [])
                # HANS_CHAT_HIDE_3RD_PARTY_V1 (20.7.) — v CHAT módu (name je
                # aktuální mluvčí) neposkytovat modelu info o 3. stranách
                # v místnosti. Doloženo 15.7.: jedna osoba byla vidět, jiná
                # chatovala → model fabrikoval „paní X se ptala, jestli bych jí
                # nabídnout čaj a sušenky" (paralelní narativ z presence +
                # curiosity zájmů). Chat partner sám vidí kdo je doma;
                # když se zeptá „kdo je doma?", odpoví na to agent action
                # report_who_is_home z živých dat, ne model z presence hintu.
                # V pozdravu (for_greeting=True) nefiltrujeme — greeting
                # legitimně zmíní kdo je v pokoji.
                if name and not for_greeting and _vis:
                    if name in _vis:
                        _vis = [name]   # ponech jen partnera
                    else:
                        # partner (např. Telegram) není fyzicky přítomen —
                        # NEuvádět modelu 3. strany ani „nikdo" (klam);
                        # None → surroundings_db větu úplně vynechá.
                        _vis = None
                _pan = getattr(self, "_pan_angle", None)
                _wx = getattr(self, '_weather', None)
                _wx_str = _wx.get_context_string() if _wx else None
                surr = self.surroundings_db.build_llm_context(
                    max_age_s=1800,
                    visible_persons=_vis,
                    pan_angle=_pan,
                    weather_str=_wx_str,
                )
                if surr:
                    surr_ctx = f"\n\n{surr}"
            except Exception as _tiche:
                log_once(  # HANS_NO_SILENT_CTX_V1
                    logging.getLogger(__name__), "_build_system(ř. 1742)",
                    "_build_system: blok kontextu selhal (ř. 1742): %s", _tiche)

        # Memory — characterization + poslední setkání (T5B_TACTFUL_RECALL_V1)
        # Jen pro plný mód; v RAG módu jde statická paměť přes RAG kolekce.
        # PRINCIP: majordomus VÍ kdy naposledy viděl pána, ale NEŘÍKÁ to.
        #   - characterization: kontext, smí ovlivnit tón
        #   - last_encounter: vnitřní znalost, NEvyslovovat; jen pokud
        #     odstup > práh (čerstvé/open encountery se ignorují)
        memory_ctx = ""
        _LAST_SEEN_MIN_GAP_S = 2 * 3600.0  # min. odstup aby "naposledy" dávalo smysl
        _mem = getattr(self, 'memory', None)
        if _mem is not None:
            try:
                from scripts.hans_memory import _czech_relative_time as _crt
                _card = _mem.fact(name)
                _last = _mem.last_encounter(name)  # jen uzavřené (include_open=False)
                _mparts = []
                if _card is not None and getattr(_card, 'characterization', ''):
                    _mparts.append(
                        f"Co o osobě {name} víš z dřívějška: {_card.characterization}")
                if _last is not None:
                    _ended = _last.get('ended_at') or _last.get('started_at')
                    _gap = time.time() - _ended if _ended else 0.0
                    if _gap >= _LAST_SEEN_MIN_GAP_S:
                        _w = _crt(_ended)
                        _mparts.append(
                            f"(Tvá vnitřní znalost — NEVYSLOVUJ to při pozdravu, "
                            f"slouží jen k vřelosti tónu: osobu {name} jsi naposledy "
                            f"viděl {_w}.)")
                if _mparts:
                    memory_ctx = '\n\n' + '\n'.join(_mparts)
            except Exception as _me:
                print(f"[Chat] memory_ctx build failed: {_me}")

        # HANS_THREADS_SURFACING_V1 — otevřené nitky s touto osobou (pasivní
        # kontext; surface_for + mark se dělá v greetingu, tady ať je Hans
        # může přirozeně vplést). Read-only, nikdy neshodí chat.
        threads_ctx = ""
        try:
            _tstore = self._thread_store()
            if _tstore is not None:
                _opn = _tstore.open_threads(name, limit=3)
                if _opn:
                    from scripts.hans_threads import format_block
                    _blk = format_block(_opn)
                    if _blk:
                        threads_ctx = (
                            "\n\nOtevřené nitky s touto osobou (něco, co dříve"
                            " zmínila a má pokračování — pokud se to hodí do"
                            " rozhovoru, přirozeně se zeptej, jak to dopadlo;"
                            " nevytahuj všechno najednou):\n" + _blk)
        except Exception:
            threads_ctx = ""

        # HANS_PERSON_INTERESTS_V1 — co tuto osobu zajímá (Hans přizpůsobí hovor)
        interests_ctx = ""
        try:
            from scripts.hans_person_interests import (
                PersonInterestStore, format_block as _pi_block)
            _pis = getattr(self, "_pinterest_inst", None)
            if _pis is None:
                _dbp = (self.config.get("diary_db")
                        or (self.config.get("hans_idle", {}) or {}).get("diary_db")
                        or "data/hans_diary.db")
                self._pinterest_inst = PersonInterestStore(self.config, _dbp)
                _pis = self._pinterest_inst
            _ints = _pis.interests_for(name, limit=6)
            _iblk = _pi_block(_ints)
            if _iblk:
                interests_ctx = ("\n\nCo " + name + " zajímá (víš z dřívějška,"
                                 " můžeš na to navázat, ne vyjmenovávat): " + _iblk)
        except Exception:
            interests_ctx = ""

        # HANS_QUESTIONS_SURFACING_V1 — čekající otázka pro osobu (soft návrh;
        # jen v chatu, NE v greetingu — tam se ptá aktivně). _maybe_surface_question
        # má cooldown + označí asked = self-limiting.
        qsuggest_ctx = ""
        if not for_greeting:
            try:
                _q = self._maybe_surface_question(name)
                if _q:
                    qsuggest_ctx = ("\n\nMáš pro tuto osobu připravenou otázku —"
                                    " pokud se to do hovoru hodí, přirozeně se"
                                    " zeptej: " + _q.question)
            except Exception:
                qsuggest_ctx = ""

        # Current person
        profile = known.get(name, {})
        if isinstance(profile, dict):
            g     = profile.get("gender", "")
            notes = profile.get("notes", "")
            if g == "žena":
                current = f"\n\nAktuálně mluvíš s {name}, která je ženského rodu."
            elif g == "muž":
                current = f"\n\nAktuálně mluvíš s {name}, který je mužského rodu."
            else:
                current = f"\n\nAktuálně mluvíš s {name}."
            if notes:
                current += f" {notes}"
        else:
            current = f"\n\nAktuálně mluvíš s {name}."

        # HANS_ADDRESSEE_V1 — kontext (deník, myšlenky, RAG) mluví o uživateli ve
        # 3. osobě („pán domu…“). Bez tohoto pravidla to model recykluje a mluví
        # o adresátovi, jako by to byl někdo třetí („Standa tě pozdravuje“).
        current += (
            f" {name} je TÁŽ osoba, o které tvé zápisky a myšlenky mluví ve třetí"
            f" osobě (např. „pán domu“, „{name} přišel“). Teď mluvíš PŘÍMO S NÍ:"
            f" oslovuj ji ve druhé osobě (ty/vy) a vokativem."
            f" NIKDY o ní nemluv ve třetí osobě a NIKDY nikomu netlumoč její vzkazy."
        )
        # HANS_ADDRESSEE_V2 (4.8.) — ostatní jména v kontextu jsou TŘETÍ OSOBY.
        # Model si bez tohohle vybral adresáta ze seznamu osob domu podle
        # rodu/persony („paní Jano"), ačkoli psal jinému uživateli.
        current += (
            f" Jakákoli JINÁ jména v tomto kontextu jsou třetí osoby, které tu"
            f" teď nepíšou — NEOSLOVUJ je, neodpovídej jim a nepiš jejich jméno"
            f" do oslovení. Oslovení patří VÝHRADNĚ osobě {name}."
        )

        read_ctx = ""
        _hi = getattr(self, '_hans_idle', None)
        if _hi and hasattr(_hi, '_curiosity'):
            _rc = _hi._curiosity.get_context_string(max_items=2)
            if _rc:
                read_ctx = "\n\n" + _rc

        # Hansovy vnitřní myšlenky
        thought_ctx = ""
        if _hi and hasattr(_hi, '_introspection'):
            _tc = _hi._introspection.get_context_string(max_items=2)
            if _tc:
                thought_ctx = "\n\n" + _tc

        # HANS_ROUTINE_CONTEXT_V1 — rutina osoby (kdy obvykle bývá doma)
        routine_ctx = ""
        if _hi and hasattr(_hi, '_routine_store'):
            try:
                _rs = _hi._routine_store()
                _rsum = _rs.summary(name) if _rs is not None else ""
                if _rsum:
                    routine_ctx = ("\n\nCo víš o jeho/jejím denním rytmu"
                                   " (kontext, nekomentuj to nahlas bezdůvodně): "
                                   + _rsum)
            except Exception:
                routine_ctx = ""

        # Stav těla a mozku
        body_ctx = ""
        if _hi and hasattr(_hi, '_body'):
            _bc = _hi._body.get_body_context()
            _br = _hi._body.get_brain_context()
            if _bc: body_ctx += "\n\n" + _bc
            if _br: body_ctx += "\n\n" + _br

        # Nálada
        mood_ctx = ""
        if _hi and hasattr(_hi, '_mood'):
            # HANS_MOOD_HIDE_3RD_PARTY_V1 — v chatu neprozrazuj jméno JINÉ
            # osoby, kterou Hans zrovna vidí (jinak ji osloví uprostřed
            # odpovědi partnerovi). V pozdravu se nefiltruje.
            _mp = _hi._mood.get_prompt_addition(
                chat_partner=(name or "") if not for_greeting else "")
            if _mp:
                mood_ctx = "\n\n" + _mp

        # HANS_MORNING_HEALTH_V1 — ranní nález z noční kontroly logů.
        # Surfacing až u člověka (greeting/chat), ne hlasitě do prázdna.
        health_ctx = ""
        try:
            _mh = getattr(_hi, '_morning_health', None) if _hi else None
            from datetime import datetime as _dt_h
            # GREETING_LEAD_PRIORITY_V1 — v pozdravu se zdraví řeší přes
            # prioritní lead (ne tady), ať se do něj nemíchá víc háčků naráz.
            if _mh and not for_greeting and _mh.get('date') == _dt_h.now().strftime('%Y-%m-%d'):
                health_ctx = ("\n\nRáno jsem si při probuzení prošel noční "
                              "záznamy a něco se mi nezdálo v pořádku: "
                              + _mh.get('summary', '')
                              + " Cítím se kvůli tomu trochu nesvůj. Pokud to "
                              "přijde přirozeně, smím se o tom zmínit.")
        except Exception:
            health_ctx = ""

        # HANS_DOWNTIME_V1 — všiml-li jsem si při startu, že jsem byl dlouho
        # mimo provoz, zmíním to u příchozí osoby a zeptám se, co se dělo.
        downtime_ctx = ""
        try:
            _dt = getattr(_hi, '_downtime', None) if _hi else None
            # GREETING_LEAD_PRIORITY_V1 — v pozdravu vede výpadek přes prioritní
            # lead (ne tady); tady jen pro běžný chat, ať se pozdrav nemixuje.
            if _dt and not for_greeting and not _dt.get('answered'):
                downtime_ctx = ("\n\n" + _dt.get('sentence', '')
                                + " Připadá mi, že jsem něco zmeškal. Pokud to "
                                "přijde přirozeně, smím se zmínit, že jsem byl "
                                "mimo, a vlídně se zeptat, co se mezitím dělo.")
                _dt['surfaced'] = True  # příští zpráva osoby = vyprávění
        except Exception:
            downtime_ctx = ""

        # SEVERKA_PROACTIVE_NOTIFY_V1 — čeká-li Severčin návrh identity na
        # schválení, Hans se o něm sám zmíní (backstop k Telegram pushi; přežije,
        # dokud uživatel nerozhodne přes /severka). Read-only, graceful.
        severka_ctx = ""
        try:
            from scripts.hans_identity import IdentityStore
            _dbp_sv = (self.config.get("diary_db")
                       or (self.config.get("hans_idle", {}) or {}).get("diary_db")
                       or "data/hans_diary.db")
            _pend = IdentityStore(self.config, _dbp_sv).pending()
            if _pend:
                severka_ctx = ("\n\nMám připravený návrh, jak přehodnotit svou "
                               "vlastní povahu (kým se stávám) — čeká na "
                               "rozhodnutí uživatele. Pokud to přijde přirozeně, "
                               "smím se zmínit, že o tom přemýšlím a že je to na "
                               "něm (schválit/zamítnout přes „/severka\").")
        except Exception:
            severka_ctx = ""

        # HANS_STUDY_DEEPEN_V2 — čekající návrh prohloubení (ask-first): Hans se
        # smí zmínit, že vytvořil dílo a navrhuje prohloubit studium, a zeptat se.
        deepen_ctx = ""
        try:
            from scripts.hans_study import StudyStore as _SSd
            _dbp_d = (self.config.get("diary_db")
                      or (self.config.get("hans_idle", {}) or {}).get("diary_db")
                      or "data/hans_diary.db")
            _dp = _SSd(self.config, _dbp_d).get_pending_deepen()
            if _dp and not for_greeting:
                _p0 = _dp[0]
                deepen_ctx = ("\n\nVytvořil jsem dílo z tématu „%s“ a při "
                              "ohlédnutí vidím, co by chtělo prohloubit (%s). Mám "
                              "připravený návrh, co se k tomu ještě doučit — čeká "
                              "na uživatele. Když to přijde přirozeně, smíš se "
                              "zeptat, co na dílo říká, a zmínit „/prohloubit“ "
                              "(schválit / vlastní kritika / ne)." % (
                                  _p0["topic"], (_p0.get("critique") or "")[:120]))
        except Exception:
            deepen_ctx = ""

        # HANS_CORRECTION_LEARNING_V1 (#4) — nedávné lekce z korekcí (Hans je
        # má v kontextu, aby chybu neopakoval; read-only, NEmění paměť/postoje).
        lessons_ctx = ""
        try:
            from scripts.hans_lessons import recent_lessons as _rl
            _dbp_l = (self.config.get("diary_db")
                      or (self.config.get("hans_idle", {}) or {}).get("diary_db")
                      or "data/hans_diary.db")
            _les = _rl(_dbp_l, hours=48, limit=4)
            if _les and not for_greeting:  # GREETING_LEAD_PRIORITY_V1 — lekce do pozdravu nepatří
                lessons_ctx = ("\n\nNedávno jsi byl opraven / mýlil ses v těchto "
                               "věcech (ber to v potaz, neopakuj tytéž omyly; pokud "
                               "to přijde přirozeně, smíš to pokorně uznat; nic si "
                               "k tomu nevymýšlej):\n- " + "\n- ".join(_les))
        except Exception:
            lessons_ctx = ""

        # HANS_SELFCRITIQUE_V1 (#6) — vlastní sebekritika (kvalita projevu, z vlastního
        # popudu). Tichý steer „takhle se chci vyjadřovat" — vedle korekčních lekcí,
        # full mód, ne pozdrav. Read-only, graceful.
        if not for_greeting:
            try:
                from scripts.hans_selfcritique import recent_selfcritiques as _rsc
                _scr = _rsc(_dbp_l, hours=120, limit=3)
                if _scr:
                    lessons_ctx += ("\n\nSám sis předsevzal zlepšit svůj projev "
                                    "(drž se toho, nevnucuj, nekomentuj to nahlas):"
                                    "\n- " + "\n- ".join(_scr))
            except Exception as _tiche:
                log_once(  # HANS_NO_SILENT_CTX_V1
                    logging.getLogger(__name__), "_build_system(ř. 2012)",
                    "_build_system: blok kontextu selhal (ř. 2012): %s", _tiche)

        # _RAG_MODE_BUILD — pro hans-rag model jen LIVE STATE.
        # Identita má vlastní system prompt v OpenWebUI, statická paměť
        # (deník, vztahové karty, známí lidé) přijde z RAG kolekcí.
        # _build_system tak dodává jen to, co RAG nemůže vědět: co Hans
        # PRÁVĚ TEĎ vidí, slyší, cítí, právě čte, koho má před sebou.
        # HANS_PROMPT_BLOCKS_TABLE_V1 — jediné místo, kde se název bloku potkává
        # se svou hodnotou. Pořadí i to, do které varianty blok patří, je
        # v `_PROMPT_BLOKY` (nahoře v modulu); sonda velikostí čte TOTÉŽ,
        # takže se nemůže rozejít se skutečným promptem jako dřív.
        _hodnoty = {
            "system_base": system_base, "time": time_ctx, "persons": persons_ctx,
            "surr": surr_ctx, "kodi": kodi_ctx, "room": room_ctx,
            "place": place_ctx, "cal": cal_ctx, "diary": diary_ctx,
            "story": story_ctx, "study": study_ctx, "direction": direction_ctx,
            "idea": idea_ctx, "read": read_ctx, "thought": thought_ctx,
            "body": body_ctx, "mood": mood_ctx, "health": health_ctx,
            "downtime": downtime_ctx, "severka": severka_ctx,
            "deepen": deepen_ctx, "lessons": lessons_ctx, "teddy": teddy_ctx,
            "memory": memory_ctx, "threads": threads_ctx,
            "interests": interests_ctx, "qsuggest": qsuggest_ctx,
            "routine": routine_ctx, "cap": cap_ctx, "current": current,
        }
        if for_greeting:
            # GREETING_LEAN_SYSTEM_V1 — pozdrav drží JEN to nutné k pozdravení:
            # identita, čas, kdo je tu, fyzický a náladový tón (+ vzácný Severka
            # backstop). Obsahové bloky (čtení, deník, narativ, myšlenky, kodi,
            # okolí, vztahové nitky, zájmy, rytmus…) se do dvouvětého pozdravu
            # NEcpou — co Hans zmíní, řídí výhradně user prompt (jediný prioritní
            # lead). Tím pozdrav přestane mixovat nesouvisející věci.
            system_msg = slozit_prompt(_hodnoty, "g")
        elif "rag" in (self.model_name or "").lower():
            system_msg = slozit_prompt(_hodnoty, "r")
            # Lehký úvodní prompt — vysvětlí RAG modelu, co tenhle blok je.
            if system_msg.strip():
                system_msg = (
                    "Následuje aktuální kontext z mých smyslů a "
                    "vnitřního stavu (toto NENÍ historie, ale "
                    "co se děje právě teď):"
                    + system_msg
                )
            else:
                system_msg = ""
        else:
            system_msg = slozit_prompt(_hodnoty, "f")
            # HANS_PROMPT_SIZE_PROBE_V1 (19.8.) — MĚŘENÍ, ne oprava.
            # Změřeno na 989 reálných dotazech: system prompt má medián 1977 zn,
            # ale MAXIMUM 21 387 a u 40 % dotazů přesáhne 10 000. Grounding
            # (pár set znaků) se v tom utopí — doloženo 19.8.: týž dotaz
            # s týmž groundingem odpověděl v izolaci (~2 KB promptu) správně
            # „1966", zatímco živě (17 280 zn) trval na vymyšleném „1963".
            # Než se začne řezat, musí být vidět KTERÝ blok to nafukuje.
            # ⚠️ Logují se JEN DÉLKY, žádný obsah — do debug.log nesmí nic
            # osobního ([[privacy-external-outputs]]).
            try:
                _blocks = {n: _hodnoty.get(n) for n, kde in _PROMPT_BLOKY if "f" in kde}
                _sizes = {k: len(v or "") for k, v in _blocks.items()}
                _sizes = {k: v for k, v in _sizes.items() if v}
                _dbg(
                    location="openwebui_direct_handler.py:_build_system",
                    message="Prompt block sizes",
                    data={"total": int(len(system_msg)),
                          "n_blocks": len(_sizes),
                          "top": dict(sorted(_sizes.items(),
                                             key=lambda x: -x[1])[:8]),
                          "sizes": _sizes},
                )
            except Exception as _tiche:
                log_once(  # HANS_NO_SILENT_CTX_V1
                    logging.getLogger(__name__), "_build_system(ř. 2081)",
                    "_build_system: blok kontextu selhal (ř. 2081): %s", _tiche)
            # PROMPT_AUDIT_B_BREVITY_V1 — zastřešující steer proti
            # rozvláčnosti (jen chat; greeting má vlastní brevitu).
            if not for_greeting:
                system_msg += (
                    "\n\nVšechno výše je jen tvůj vnitřní kontext — nemusíš"
                    " ho v odpovědi vyjmenovávat ani komentovat. Reaguj"
                    " přirozeně a k věci na to, co bylo právě řečeno;"
                    " z kontextu vytáhni jen to, co se do hovoru hodí.")
                # HANS_CHAT_ANTICONFAB_V1 — pojistka proti vymýšlení vzpomínek.
                system_msg += (
                    "\n\nPAMĚŤ — DŮLEŽITÉ: Když se tě někdo ptá, zda si na něco"
                    " vzpomínáš (dřívější rozhovor, kdy a o čem jste mluvili),"
                    " odpověz POUZE z toho, co MÁŠ výše v kontextu nebo v historii."
                    " Pokud to tam není, UPŘÍMNĚ přiznej, že si to přesně"
                    " nevybavuješ (nebo požádej o připomenutí) — NIKDY si"
                    " NEVYMÝŠLEJ, kdy se to stalo (žádná falešná „před pěti dny“),"
                    " ani detaily, které nemáš doložené. Raději méně a pravdivě"
                    " než sebejistá smyšlenka.")
                # HANS_CHAT_ANTICONFAB_V2 — neznámý pojem + žádné vymyšlené zdroje.
                # HANS_SOURCE_QUERY_V1 (17.7.): zúženo. Absolutní zákaz odkazů
                # znemožnil sdílet URL, které Hans REÁLNĚ má (entity.source,
                # study_seen_works). Teď: zákaz VÝMYSLU, ne zákaz sdílení.
                system_msg += (
                    "\n\nNEZNÁMÉ POJMY A ZDROJE — DŮLEŽITÉ: Když se tě někdo"
                    " zeptá „co je X“ a X nemáš výše v kontextu ani tomu"
                    " spolehlivě nerozumíš, NEVYMÝŠLEJ si význam ani fakta —"
                    " uctivě přiznej, že o tom nemáš spolehlivou znalost, a"
                    " případně požádej o upřesnění (pojem může být i zkomolený"
                    " z dřívějšího záznamu). Drž se jednoho výkladu; neměň"
                    " příběh při dalším dotazu."
                    "\n\nZDROJE - DULEZITE: NIKDY nevymyslej odkazy, URL, nazvy"
                    " clanku, PDF nebo citace, ktere NEJSOU v tomto promptu."
                    " ALE: pokud v tomto promptu MAS konkretni URL nebo nazev"
                    " zdroje (napr. z bloku 'Zdroje, ktere mas v pameti' nebo"
                    " groundingu), MUZES a MAS ho uzivateli sdilet doslova."
                    " Neodbyvej frazi 'nemam pristup k externim zdrojum' -"
                    " pokud v promptu URL je, mas ji. Pokud opravdu v promptu"
                    " nic neni, priznaj to a rekni: 'to je z me obecne znalosti,"
                    " konkretni clanek v pameti nemam'.")
                # HANS_MEMORY_VS_KNOWLEDGE_V1 (18.7.) — rozlišuj OBECNOU ZNALOST
                # (co víš z trénování) od PAMĚŤOVÉHO ZÁZNAMU (co je výše v
                # kontextu / RAG groundingu / deníku). Doložený případ Červený
                # trpaslík (18.7. 21:15): user „Znáš X?", RAG žádný match, Hans
                # halucinoval „Ano, mám v paměti záznamy a nedávno jsem si jej
                # pročetl" — LEŽ. Následně „zajímavosti Rimmera?" → „nemám
                # záznam" = viditelný ROZPOR.
                system_msg += (
                    "\n\nPAMĚŤ vs OBECNÁ ZNALOST — KLÍČOVÉ ROZLIŠENÍ:\n"
                    "Když se tě někdo zeptá 'znáš X?' nebo 'co víš o X?',"
                    " nejdřív se podívej ZDA je X výše v kontextu / v tvé paměti"
                    " (grounding blok, historie). Podle toho odpověz JEDNÍM"
                    " ze tří způsobů:\n"
                    "  (a) V PAMĚTI — kontext / grounding X obsahuje: 'Ano,"
                    " mám o X záznamy...' a řekni CO PŘESNĚ máš.\n"
                    "  (b) OBECNÁ ZNALOST — kontext X neobsahuje, ale ty ho"
                    " z obecné znalosti znáš: 'V paměti to nemám, ale obecně"
                    " vím, že X je Y...' a klidně to obecně shrň. NEROZUMĚJ"
                    " 'obecná znalost' jako 'mám záznam' — jsou to jiné věci.\n"
                    "  (c) NEZNÁŠ — kontext X neobsahuje a ani obecně nevíš:"
                    " 'O X nemám znalost, pane.' Krátce, bez vymýšlení.\n"
                    "NIKDY nesměšuj: nesmíš říct 'mám v paměti záznamy' u něčeho,"
                    " co je JEN tvá obecná znalost. Rozpor 'mám záznamy' vs"
                    " 'nemám záznamy' v jedné konverzaci = ztráta důvěry.")
                # HANS_PROVENANCE_V1 — source-monitoring: rozlišuj vzpomínku
                # od představy/úvahy (řádky kontextu nesou značku původu).
                try:
                    from scripts import hans_provenance as _prov
                    if (self.config.get('provenance', {}) or {}).get(
                            'enabled', True):
                        system_msg += "\n\n" + _prov.STEER
                except Exception as _tiche:
                    log_once(  # HANS_NO_SILENT_CTX_V1
                        logging.getLogger(__name__), "_build_system(ř. 2153)",
                        "_build_system: blok kontextu selhal (ř. 2153): %s", _tiche)
                # HANS_ART_HONESTY_V1 — neslibuj malování, které nespustíš.
                # Obraz vznikne JEN příkazem „namaluj …" (ten se zpracuje mimo
                # tuhle odpověď). Když uživatel dá zpětnou vazbu k obrazu,
                # naveď ho na příkaz, nepředstírej, že už maluješ.
                system_msg += (
                    "\n\nMALOVÁNÍ — DŮLEŽITÉ: Obraz vznikne JEN když uživatel "
                    "napíše příkaz „namaluj …\" / „nakresli …\" — ten spouští "
                    "výtvarnou dílnu mimo tuhle tvou odpověď. V běžné odpovědi "
                    "NEDOKÁŽEŠ malování sám spustit, takže NESLIBUJ „maluji\"/"
                    "„nakreslím\", pokud uživatel PRÁVĚ nedal příkaz namaluj. "
                    "Když ti dá zpětnou vazbu k obrazu (např. „to nejsem já\", "
                    "„je to špatně\"), poděkuj a NAVEĎ ho: ať řekne „namaluj to "
                    "znovu jako …\" nebo „namaluj mě jako …\" — teprve tím se "
                    "obraz reálně překreslí.")
        # region agent log
        try:
            _dbg(
                location="openwebui_direct_handler.py:_build_system",
                message="Built system prompt",
                data={
                    "has_surroundings": bool(surr_ctx.strip()),
                    "has_known_persons": bool(persons_ctx.strip()),
                    "chars": len(system_msg),
                    "history_turns": self.conv_store.summary(),
                },
            )
        except Exception as _tiche:
            log_once(  # HANS_NO_SILENT_CTX_V1
                logging.getLogger(__name__), "_build_system(ř. 2181)",
                "_build_system: blok kontextu selhal (ř. 2181): %s", _tiche)
        # endregion
        return system_msg

    def _generate_greeting_prompt(self, name: str) -> tuple:
        self._greeting_thread_surfaced = False  # GREETING_THREAD_POPUP_V1
        hour = datetime.now().hour
        if 5  <= hour < 12: tod = "ráno"
        elif 12 <= hour < 17: tod = "odpoledne"
        elif 17 <= hour < 22: tod = "večer"
        else:                  tod = "v noci"

        greeting_cfg  = self.config.get("greeting", {})
        system = self._build_system(name, for_greeting=True) + (
            " Pozdrav stručně a důstojně: nanejvýš dvě krátké věty,"
            " žádná dlouhá souvětí.")  # GREETING_BREVITY_V1
        # Přidej náladu do tónu pozdravu
        _hi2 = getattr(self, '_hans_idle', None)
        if _hi2 and hasattr(_hi2, '_mood'):
            _mp = _hi2._mood.get_prompt_addition()
            if _mp:
                system += " " + _mp

        # Sestav co Hans skutečně dělal — rotuje, neopakuje se
        _hi = getattr(self, '_hans_idle', None)
        _activity_hint = ""

        # Sbírej kandidáty ze všech zdrojů
        _candidates: list[str] = []

        if _hi:
            # Vnitřní myšlenky
            if hasattr(_hi, '_introspection'):
                _candidates.extend(_hi._introspection._recent_thoughts[:4])

            # Co četl
            if hasattr(_hi, '_curiosity') and _hi._curiosity._recent:
                for _r in _hi._curiosity._recent[:4]:
                    _candidates.append(
                        f"četl jsem o tématu '{_r.title}': {_r.summary[:80]}")

            # Filmy z deníku
            try:
                rows = _hi._db.execute(
                    "SELECT title FROM diary WHERE event_type='movie_browsed' "
                    "ORDER BY ts DESC LIMIT 5"
                ).fetchall()
                for (t,) in rows:
                    _candidates.append(f"přemýšlel jsem o filmu '{t}'")
            except Exception:
                pass

        # Majordomus aktivity — věrohodné věci které Hans dělá
        import random as _rnd
        _butler = [
            "přeleštil jsem stříbro — odraz svíček je nyní uspokojivý",
            "zkontroloval jsem zásoby čaje a doplnil anglický breakfast",
            "seřadil jsem knihy v knihovně podle roku vydání",
            "přeložil jsem přikrývky v ložnici podle pravidel správné domácnosti",
            "zkontroloval jsem okenní závěsy — prach se hromadí nenápadně",
            "naostřil jsem nože v kuchyni — tupý nůž je nehodný domácnosti",
            "zapsal jsem poznámky o stavu domácnosti do zásobní knihy",
            "přelil jsem květiny — mírně, jak se sluší",
            "zkontroloval jsem hodiny v každé místnosti — musí jít shodně",
            "upravil jsem polohu obrazů — symetrie je základem důstojnosti",
            "vyčistil jsem příborník a seřadil příbory podle protokolu",
            "prověřil jsem stav svíček — vždy musí být připraveny",
            "zkontroloval jsem zásoby whisky a zaznamenal stav do knihy",
            "přemýšlel jsem o správném pořadí chodu při příští večeři",
            "zkontroloval jsem teploměr — správná teplota místnosti je 18 stupňů",
        ]
        # Přidej majordomus aktivity jako menšinové kandidáty (1 z 3)
        # aby převažovaly skutečné zážitky ale butler věci se občas objevily
        if _candidates:
            _candidates.extend(_rnd.sample(_butler, min(2, len(_butler))))
        else:
            _candidates = _butler[:]

        # Vyber kandidáta který ještě nebyl použit
        _unused = [c for c in _candidates if c not in self._used_hints]
        if not _unused:
            # Všechno bylo použito — resetuj paměť a začni znovu
            self._used_hints.clear()
            _unused = _candidates

        if _unused:
            _activity_hint = _rnd.choice(_unused)
            # Zapamatuj si co bylo řečeno (max 10 položek)
            self._used_hints.append(_activity_hint)
            if len(self._used_hints) > 10:
                self._used_hints.pop(0)

        # GREETING_WEATHER_OPTIN_V1 — kdo dostává počasí v pozdravu (dle configu).
        # NE natvrdo šablona: jde normální greeting cestou (aktivita/nitky);
        # počasí se přidá až dole a JEN když je reálně zjištěné.
        _special = self.config.get("greeting", {}).get("special_greetings", {})
        _wants_weather = name.lower() in [k.lower() for k in _special]

        user_template = greeting_cfg.get("user_prompt",
                        "Pozdrav hosta jménem {name} jednou větou. Je {tod}.")
        user = user_template.format(name=name, tod=tod)

        # GREETING_LEAD_PRIORITY_V1 — pozdrav vede JEDINOU proaktivní věcí, ať
        # se do dvouvětého pozdravu nemíchá víc nesouvisejících háčků. Pořadí:
        # výpadek > rozjetá nitka > ranní zdraví > co Hans dělal.
        _lead = False

        # 1) HANS_DOWNTIME_V1 — byl jsem dlouho mimo provoz: přiznám a zeptám se.
        try:
            _dt_g = getattr(_hi, '_downtime', None) if _hi else None
            if _dt_g and not _dt_g.get('answered'):
                user = (
                    f"Pozdrav {name} krátce a důstojně. Je {tod}. Pak v jedné větě "
                    f"přiznej, žes byl delší dobu mimo provoz, a vlídně se zeptej, "
                    f"co se mezitím dělo. (Fakt: {_dt_g.get('sentence','')}) "
                    f"Celkem nanejvýš dvě krátké věty, žádné dlouhé souvětí. "
                    f"Jméno použij jen jednou na začátku."
                )
                _dt_g['surfaced'] = True  # příští zpráva osoby = vyprávění
                _lead = True
        except Exception:
            pass

        # 2) HANS_THREADS_SURFACING_V1 — navnáž na rozjetou nitku; příchod osoby
        # = nejpřirozenější moment „jak to dopadlo".
        if not _lead:
            try:
                _tstore = self._thread_store()
                _thr = _tstore.surface_for(name) if _tstore is not None else None
                if _thr is not None:
                    _fu = _thr.follow_up or f"zeptej se, jak to dopadlo s: {_thr.topic}"
                    user = (
                        f"Pozdrav {name} krátce a důstojně. Je {tod}. Pak naváž na to, "
                        f"co {name} dříve zmínil/a, a přirozeně se zeptej: {_fu} "
                        f"Celkem nanejvýš dvě krátké věty, žádné dlouhé souvětí. "
                        f"Jméno použij jen jednou na začátku."
                    )
                    _tstore.mark_surfaced(_thr.id)
                    self._greeting_thread_surfaced = True  # GREETING_THREAD_POPUP_V1
                    _lead = True
            except Exception:
                pass

        # 3) HANS_MORNING_HEALTH_V1 — ráno po chybné noci: krátká upřímná zmínka.
        if not _lead:
            try:
                _mh_g = getattr(_hi, '_morning_health', None) if _hi else None
                from datetime import datetime as _dt_mh
                if _mh_g and _mh_g.get('date') == _dt_mh.now().strftime('%Y-%m-%d'):
                    user = (
                        f"Pozdrav {name} krátce a důstojně. Je {tod}. Pak v jedné větě "
                        f"upřímně zmiň, žes ráno nebyl ve své kůži kvůli nočním "
                        f"potížím v záznamech ({_mh_g.get('summary','')}). "
                        f"Celkem nanejvýš dvě krátké věty, žádné dlouhé souvětí. "
                        f"Jméno použij jen jednou na začátku."
                    )
                    _lead = True
            except Exception:
                pass

        # 4) Co Hans dělal (activity hint) — výchozí, jen když nic výš nevedlo.
        if not _lead and _activity_hint:
            user = (  # GREETING_BREVITY_V1
                f"Pozdrav {name} krátce a důstojně. Je {tod}. "
                f"Pak v JEDNÉ stručné větě nenásilně zmiň, čemu ses během "
                f"jejich nepřítomnosti věnoval: {_activity_hint}. "
                f"Celkem nanejvýš dvě krátké věty, žádné dlouhé souvětí. "
                f"Jméno použij jen jednou na začátku."
            )

        # GREETING_WEATHER_OPTIN_V1 — počasí JEN když reálně zjištěné; přesná
        # citace (neodhaduj) → konec konfabulace „82 °C". Jinak nezmiňuj.
        if _wants_weather:
            _wx = getattr(self, "_weather", None)
            _tomorrow = ((_wx.get_tomorrow_string() if _wx else "") or "").strip()
            if _tomorrow:
                user += (f" Na závěr nahlas PŘESNĚ tuto předpověď na zítřek, "
                         f"slovo od slova; neuváděj jiná čísla ani neodhaduj: "
                         f"„{_tomorrow}\"")

        return system, user

    # ── OpenWebUI API ─────────────────────────────────────────────────────────

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.api_token:
            h["Authorization"] = f"Bearer {self.api_token}"
        return h

    def _build_messages(self, system: str, user: str, name: str | None,
                        grounding: str = "") -> list:  # G4B_GROUNDING_POSITION_V1
        msgs = []
        if system:
            msgs.append({"role": "system", "content": system})
        if name:
            # HANS_CHAT_CHANNEL_AWARE_V1 — LLM vidí historii tohoto kanálu +
            # staré netaggované zprávy (kontinuita). Zprávy z JINÝCH kanálů
            # se filtrují (Bug 3: Rimmer z Telegramu ovlivnil web chat).
            # get_history(channel=X) = ch IN (None, X) — měkký filtr.
            _ch = get_current_channel()
            history = self.conv_store.get_history(name, channel=_ch)
            history = [m for m in history if isinstance(m, dict)]
            # Limit history — kompletní historie přeplňuje context window.
            # 8K context + RAG retrieval + system prompt nechá málo místa,
            # model recituje vzorce ze starých dialogů místo aktuálních dat.
            _hist_limit = int(self.config.get("openwebui_chat", {})
                              .get("history_max_messages", 10))
            if _hist_limit > 0 and len(history) > _hist_limit:
                history = history[-_hist_limit:]
            msgs.extend(history)
        # G4B_GROUNDING_POSITION_V1 — grounding/anti-konfab ZA historii,
        # těsně PŘED user → má poslední slovo, přebije recitaci ze starých
        # dialogů (model váží nejvíc to nejblíž otázce).
        if grounding and grounding.strip():
            msgs.append({"role": "system", "content": grounding.strip()})
        msgs.append({"role": "user", "content": user})
        # region agent log
        try:
            hist_n = (len(msgs) - (1 if system else 0) - 1)
            _dbg(
                location="openwebui_direct_handler.py:_build_messages",
                message="Built message list",
                data={
                    "name_present": bool(name),
                    "n_total": int(len(msgs)),
                    "n_history": int(hist_n),
                    "system_chars": int(len(system or "")),
                    "user_chars": int(len(user or "")),
                    # HANS_PROMPT_SIZE_PROBE_V1 — měřeno 19.8.: grounding se
                    # do zprávy vůbec nedostával (n_total == n_history+2).
                    "grounding_chars": int(len((grounding or "").strip())),
                },
            )
        except Exception:
            pass
        # endregion
        return msgs

    def _send_message(self, prompt, name: str | None = None,
                      internal: bool = False,
                      grounding=_GROUNDING_UNSET) -> str | None:
        """Nstreaming request — vrátí celou odpověď.

        internal=True (G3D): interní generační prompt (uvítačka, idle) —
        grounding se NEspustí (není to uživatelský faktický dotaz).
        grounding: předpočítaný grounding blok (A1) — když předán, znovu
        se nepočítá (šetří RAG). Sentinel = spočítej jako dřív.
        """
        if not self.enabled:
            return None
        # GAME_MODE_CHAT_GATE_V1 — herní mód: neobcházej ollama_client gate.
        # Přímý HTTP na OpenWebUI proxy → Ollama by nahrál hans-czech
        # (8 GB) do VRAM a zabil hru. VRAM patří hře.
        try:
            from scripts.ollama_client import game_mode_on
            if game_mode_on():
                logging.getLogger(__name__).info(
                    'CHAT: herní mód — _send_message skipnut (VRAM patří hře)')
                return None
        except Exception:
            pass
        try:
            if isinstance(prompt, tuple):
                system, user = prompt
            else:
                system = (self._build_system(name, user_msg=str(prompt or ""))
                          if name else "")
                user = prompt
            # G4B_GROUNDING_POSITION_V1 — grounding ZA historii (param),
            # ne připojený k system (jinak ho historie přebije).
            # G3D_SKIP_GROUNDING_INTERNAL_V1 — interní prompt → bez groundingu
            if grounding is not _GROUNDING_UNSET:
                _grounding = grounding
            else:
                _grounding = ""
                if not internal:
                    try:
                        _grounding = self._build_grounding(user, name)
                    except Exception as _g3e:
                        logging.getLogger(__name__).warning(
                            'G3B send grounding failed: %s', _g3e)
            msgs = self._build_messages(system, user, name, _grounding)
            r = requests.post(
                self.chat_endpoint,
                headers=self._headers(),
                json={"model": self.model_name, "messages": msgs, "stream": False},
                timeout=self.timeout,
            )
            if r.status_code == 200:
                data = r.json()
                if "choices" in data and data["choices"]:
                    return data["choices"][0]["message"]["content"].strip()
            print(f"[Chat] HTTP {r.status_code}: {r.text[:200]}")
        except Exception as e:
            print(f"[Chat] _send_message error: {e}")
        return None

    def _stream_message(self, prompt, name: str | None = None,
                        on_sentence=None,
                        internal: bool = False,
                        grounding=_GROUNDING_UNSET) -> str | None:
        """
        Streaming request přes OpenWebUI SSE.
        Volá on_sentence(str) pro každou dokončenou větu → TTS začne mluvit
        před koncem celé odpovědi.
        grounding: předpočítaný grounding blok (A1) — když předán, znovu
        se nepočítá (šetří RAG). Sentinel = spočítej jako dřív.
        """
        if not self.enabled:
            return None
        # GAME_MODE_CHAT_GATE_V1 — stejný gate jako _send_message výše.
        try:
            from scripts.ollama_client import game_mode_on
            if game_mode_on():
                logging.getLogger(__name__).info(
                    'CHAT: herní mód — _stream_message skipnut (VRAM patří hře)')
                return None
        except Exception:
            pass
        try:
            _t0 = time.time()
            if isinstance(prompt, tuple):
                system, user = prompt
            else:
                system = (self._build_system(name, user_msg=str(prompt or ""))
                          if name else "")
                user = prompt
            # G4B_GROUNDING_POSITION_V1 — grounding ZA historii (param).
            # G3D_SKIP_GROUNDING_INTERNAL_V1 — interní prompt → bez groundingu
            if grounding is not _GROUNDING_UNSET:
                _grounding = grounding
            else:
                _grounding = ""
                if not internal:
                    try:
                        _grounding = self._build_grounding(user, name)
                    except Exception as _g3e:
                        logging.getLogger(__name__).warning(
                            'G3B stream grounding failed: %s', _g3e)
            msgs = self._build_messages(system, user, name, _grounding)

            payload = {"model": self.model_name, "messages": msgs, "stream": True}
            # region agent log
            try:
                approx_chars = sum(len((m or {}).get("content", "")) for m in msgs if m)
                _dbg(
                    location="openwebui_direct_handler.py:_stream_message",
                    message="Sending streaming request",
                    data={
                        "name_present": bool(name),
                        "msgs": int(len(msgs)),
                        "approx_chars": int(approx_chars),
                        "endpoint": str(self.base_url),
                        "model": str(self.model_name),
                    },
                )
            except Exception:
                pass
            # endregion

            r = requests.post(
                self.chat_endpoint,
                headers=self._headers(),
                json=payload,
                timeout=self.timeout,
                stream=True,
            )
            if r.status_code != 200:
                print(f"[Chat] Stream HTTP {r.status_code}")
                # region agent log
                try:
                    _dbg(
                        location="openwebui_direct_handler.py:_stream_message",
                        message="Streaming request failed",
                        data={"http": int(r.status_code), "body_prefix": (r.text or "")[:160]},
                    )
                except Exception:
                    pass
                # endregion
                return None

            full_text = ""
            buffer    = ""
            _SPLIT    = re.compile(r"(?<=[.!?])\s+")
            # Citation markery z RAG odpovědí: [1], [3, 4], [12].
            # Stripujeme je před on_sentence callbackem, aby TTS
            # nemluvilo čísla. Full response s markery se vrací volajícímu
            # beze změny (chat okno je zobrazí jako odkazy).
            _CITATION_RE = re.compile(r"\s*\[\s*\d+(?:\s*,\s*\d+)*\s*\]")
            _parse_err = 0

            for line in r.iter_lines():
                if not line:
                    continue
                line = line.decode("utf-8") if isinstance(line, bytes) else line
                if line.startswith("data: "):
                    line = line[6:]
                if line == "[DONE]":
                    break
                try:
                    chunk = json.loads(line)
                except Exception:
                    _parse_err += 1
                    continue

                if not isinstance(chunk, dict):
                    continue
                _choices = chunk.get("choices") or []
                _choice  = _choices[0] if _choices else None
                delta    = (_choice.get("delta", {}).get("content", "")
                            if isinstance(_choice, dict) else "")
                if not delta:
                    continue

                buffer    += delta
                full_text += delta

                if on_sentence and _SPLIT.search(buffer):
                    parts = _SPLIT.split(buffer)
                    for sentence in parts[:-1]:
                        s = sentence.strip()
                        if s:
                            s_clean = _CITATION_RE.sub("", s).strip()
                            if s_clean:
                                import logging as _l
                                _l.getLogger("hans_tts_debug").debug(
                                    "TTS_DUMP main: raw=%r  clean=%r", s, s_clean)
                                on_sentence(s_clean)
                    buffer = parts[-1]

            if on_sentence and buffer.strip():
                tail_clean = _CITATION_RE.sub("", buffer).strip()
                if tail_clean:
                    import logging as _l
                    _l.getLogger("hans_tts_debug").debug(
                        "TTS_DUMP tail: raw=%r  clean=%r", buffer, tail_clean)
                    on_sentence(tail_clean)

            out = full_text.strip() or None
            # region agent log
            try:
                _dbg(
                    location="openwebui_direct_handler.py:_stream_message",
                    message="Streamed response",
                    data={
                        "name_present": bool(name),
                        "t_s": round(time.time() - _t0, 3),
                        "out_chars": len(out or ""),
                        "sentences_cb": bool(on_sentence),
                        "parse_err_lines": int(_parse_err),
                    },
                )
            except Exception:
                pass
            # endregion
            return out

        # STREAM_CONNERR_QUIET_V1 — connection error bez tracebacku
        except requests.exceptions.ConnectionError as e:
            print(f"[Chat] _stream_message connection error: {e}")
        except Exception as e:
            import traceback; traceback.print_exc(); print(f"[Chat] _stream_message error: {e}")
        return None

    def _maybe_deepen_response(self, name: str, message: str):
        """HANS_STUDY_DEEPEN_V2 — reakce na návrh prohloubení ČISTÝM TEXTEM.
        Gated na čekající návrh → klasifikuje (schvaluje/zamítá/kritizuje/nic) a
        rovnou aplikuje. Vrací odpověď nebo None (není reakce → normální chat)."""
        from scripts.hans_study import StudyStore
        dbp = (self.config.get("diary_db")
               or (self.config.get("hans_idle", {}) or {}).get("diary_db")
               or "data/hans_diary.db")
        st = StudyStore(self.config, dbp)
        pend = st.get_pending_deepen()
        if not pend:
            return None
        # HANS_CONFIRM_PRECEDENCE_V1 (7.8.) — ODPOVĚĎ PATŘÍ NEJČERSTVĚJŠÍMU NÁVRHU.
        # Doloženo 7.8. 11:22–11:23: Hans nabídl film na Kodi, uživatel řekl „ne"
        # — a Hans odpověděl „„hrady a historická architektura" nechám tak, jak
        # je." Zamítnutí spolkl návrh prohloubení studia z **03:26 ráno** (8 h
        # starý), protože tahle větev běží PŘED `agent.check_confirmation`
        # (ř. 2719 × 2738) a návrhy prohloubení NEMAJÍ expiraci — kdežto agentní
        # návrh vyprší po 3 min. Starý a nesmrtelný tak vždy přebil čerstvý.
        # Když agent čeká na potvrzení, ustup — odpověď je jeho.
        # (Vedlejší zisk: ušetří se LLM klasifikace na každé zprávě, dokud
        # nějaký návrh prohloubení leží ve frontě.)
        try:
            _ag = self._agent_router()
            _ap = getattr(_ag, "_pending", None) if _ag is not None else None
            _p = _ap.get(name) if _ap else None
            if _p is not None and (time.time() - _p.ts) <= 180:
                logging.getLogger(__name__).info(
                    'HANS_CONFIRM_PRECEDENCE_V1: čeká agentní návrh %s '
                    '→ prohloubení ustupuje', getattr(_p.action, "id", "?"))
                return None
        except Exception as _cpe:
            logging.getLogger(__name__).debug('confirm precedence: %s', _cpe)
        # HANS_DEEPEN_QUESTION_GUARD_V1 (19.8.) — OTÁZKA NENÍ ZPĚTNÁ VAZBA.
        # Dokud leží návrh na prohloubení, posílá se KAŽDÁ další zpráva LLM
        # klasifikátoru (SCHVALUJE/ZAMITA/KRITIZUJE/NIC). Doloženo 19.8.:
        # nevinný dotaz „a Babičku jsi četl ty sám?" vyhodnotil jako KRITIZUJE
        # → `apply_deepen_proposal` reaktivoval DOKONČENÝ program „Český ráj"
        # (completed → active) a přidal 4 pod-témata. Uživatel o nic nežádal,
        # jen se ptal — a přišel tím o pořadí ve studijní frontě
        # (`get_active_program` bere nejstarší aktivní).
        # Deterministicky PŘED klasifikátorem: tázací věta bez schvalovacího
        # nebo odmítacího slova = NIC. Prompt se neladí, dotaz se prostě nepustí.
        try:
            import re as _qre
            _m = (message or "").strip()
            _is_q = _m.endswith("?") or bool(_qre.match(
                r"^\s*(kdo|co|kde|kdy|jak|pro[čc]|kolik|kter|[čc][íi]|zn[áa]|"
                r"vid[íi]|um[íi][šs]|m[áa][šs]|je\s|jsi\s|byl\s)", _m, _qre.I))
            _fb = bool(_qre.search(
                r"\b(ano|jo|souhlas\w*|schval\w*|dob[řr]e|prohlub|prohloub|"
                r"ne\b|nechci|nesouhlas\w*|zru[šs]|nech\s+to|špatn\w*|"
                r"slab\w*|m[ěe]l\s+bys|douč|dodělej)\b", _m, _qre.I))
            if _is_q and not _fb:
                logging.getLogger(__name__).info(
                    'HANS_DEEPEN_QUESTION_GUARD_V1: %.40s je otázka, ne zpětná '
                    'vazba → návrh se nedotýkám', _m)
                return None
        except Exception as _qge:
            logging.getLogger(__name__).debug('deepen question guard: %s', _qge)
        p0 = pend[0]
        from scripts.ollama_client import ollama_generate
        model = (self.config.get("dialog", {}) or {}).get("model") or "hans-czech:latest"
        sysp = ("Byl vytvořen web/dílo o „%s“ a Hans navrhl prohloubit studium "
                "(kritika díla: %s). Rozhodni, jak uživatel na TENTO návrh reaguje. "
                "Odpověz JEDNÍM slovem: SCHVALUJE (souhlasí, ať se prohloubí) / "
                "ZAMITA (nechce) / KRITIZUJE (dává vlastní kritiku díla nebo říká, "
                "co doučit) / NIC (zpráva s návrhem vůbec nesouvisí)."
                % (p0["topic"], (p0.get("critique") or "")[:120]))
        raw = ollama_generate(model, "Zpráva uživatele: %s" % message[:300],
                              system=sysp, config=self.config, timeout=25,
                              keep_alive=-1,
                              options={"temperature": 0, "num_predict": 6})
        if not raw:
            return None
        verdict = raw.strip().upper()
        if "NIC" in verdict:
            return None
        if "ZAMIT" in verdict or "ZAMÍT" in verdict:
            st.reject_deepen_proposal(p0["id"])
            return "Dobře, pane. „%s“ nechám tak, jak je." % p0["topic"]
        user_crit = message.strip() if "KRITIZ" in verdict else ""
        import threading as _th

        def _apply():
            try:
                st.apply_deepen_proposal(self.config, p0["id"],
                                         user_critique=user_crit)
            except Exception:
                pass
        _th.Thread(target=_apply, daemon=True).start()
        if user_crit:
            return ("Beru tvou kritiku, pane — podle ní prohloubím studium „%s“. "
                    "Nová pod-témata pak uvidíš v /studium." % p0["topic"])
        return ("Schváleno, pane. Prohloubím studium „%s“ a příště z něj vytvořím "
                "lepší dílo." % p0["topic"])

    def _is_test_person(self, name: str) -> bool:
        """HANS_TEST_PERSON_V1 — je tohle testovací identita?
        ⚠️ Zápis chatu do deníku má DVĚ cesty (tenhle helper pro early-return
        větve a hlavní zápis na konci `send_chat_message`) — proto predikát,
        ne kopie kontroly ve dvou místech. První verze hlídala jen helper
        a řádek se stejně zapsal ([[test-the-fix-not-the-symptom]])."""
        try:
            _tp = [str(x).strip().lower()
                   for x in (self.config.get("test_persons") or [])]
            if (name or "").strip().lower() in _tp:
                logging.getLogger(__name__).info(
                    "HANS_TEST_PERSON_V1: %r je testovací identita — "
                    "do deníku ani RAG se nezapisuje", name)
                return True
        except Exception:
            pass
        return False

    def _log_human_chat_to_diary(self, name: str, user_message: str,
                                 response: str,
                                 bypass_kind: str = None) -> None:
        """HANS_CHAT_DIARY_ALL_PATHS_V1 (18.7.) — early-return cesty (slash cmd,
        agent akce, source bypass, deepen…) obchází standardní diary write na
        konci `send_chat_message` → chat se do `human_chat` nezapíše →
        reflexe/self_insight/audit ho neuvidí (viz Telegram Rimmer-paint 21:16).
        Extract do helper, volat u KAŽDÉ early-return cesty.

        HANS_BYPASS_TRACE_V1 (19.7.) — `bypass_kind` (např. 'sources',
        'knowledge_check') označí, že odpověď NEPROŠLA persona finetunem —
        šla deterministickou šablonou mimo LLM. Zápis do `data` sloupce
        (JSON) + samostatný `bypass_note` s importance=7 (surfacing v
        night_reflection / self_insight). Bez toho persona finetune svá
        vlastní „mimotělní" sdělení nezná → kognitivní dissonance při
        čtení vlastního deníku ([[bypass-self-reflection]])."""
        if not response:
            return
        # HANS_TEST_PERSON_V1 (19.8.) — rozhovor vedený pod TESTOVACÍ identitou
        # se do paměti nezapisuje. Důvod je doložený: 19.8. jsem uklidil deník
        # i RAG od vyvrácené fabulace a o minutu později ji tam vrátil vlastním
        # ověřovacím rozhovorem — druhý den to vypadalo jako návrat bugu
        # ([[test-the-fix-not-the-symptom]], bod 6). Chat funguje normálně
        # (Hans odpovídá, historie vlákna se drží v conv_store, takže se dá
        # testovat i navazování), jen se z toho nestává „co Hans ví".
        # ⚠️ ZÁMĚRNĚ jen tenhle jeden zápis: `human_chat` je zdroj pro deník,
        # reflexe i RAG, takže vynechání tady utne celou větev naráz.
        if self._is_test_person(name):
            return
        try:
            _note = f"{name}: {user_message}\nHans: {response}"
            _data = None
            if bypass_kind:
                import json as _json
                _data = _json.dumps({"bypass": 1, "kind": bypass_kind},
                                    ensure_ascii=False)
            _hi = getattr(self, "_hans_idle", None)
            if _hi and hasattr(_hi, "_log_entry"):
                _hi._log_entry("human_chat", name, note=_note,
                               data=(_data or ""))
            else:
                # fallback: přímý SQL
                import sqlite3 as _sql, time as _t
                _diary = (self.config.get("diary_db", "data/hans_diary.db")
                          if hasattr(self, "config") else "data/hans_diary.db")
                with _sql.connect(_diary) as _db:
                    _db.execute(
                        "INSERT INTO diary (ts, event_type, title, note, data) "
                        "VALUES (?,?,?,?,?)",
                        (_t.time(), "human_chat", name, _note, _data))
                    _db.commit()
            # bypass_note (surfaced) — samostatný event pro noční reflexi.
            if bypass_kind:
                self._write_bypass_note(bypass_kind, response)
        except Exception as _e:
            logging.getLogger(__name__).debug(
                "human_chat diary write (early-return): %s", _e)

    def _write_bypass_note(self, kind: str, response: str) -> None:
        """HANS_BYPASS_TRACE_V1 — samostatný diary event (importance=7) o
        deterministické odpovědi. Vzor pro všechny bypass cesty (dřív inline
        v sources_answer bloku, teď sdíleno). Hans si to přečte v ranní
        reflexi / self_insight → má šanci si všimnout, že odpověď šla mimo
        jeho obvyklou úvahu."""
        try:
            import sqlite3 as _sq, time as _tm, json as _json
            _diary = (self.config.get("diary_db", "data/hans_diary.db")
                      if hasattr(self, "config") else "data/hans_diary.db")
            _snippet = (response[:140] + "…") if len(response) > 140 else response
            _kind_label = {
                "sources":         "source query",
                "knowledge_check": "knowledge check",
                "instant_lookup":  "okamžité dohledání",
            }.get(kind, kind)
            # HANS_INSTANT_LOOKUP_V1 — u dohledání NESMÍ zápis tvrdit „výpis
            # z paměti": opak je pravdou (v paměti to nebylo, proto se hledalo)
            # a Hans si tyhle poznámky čte v noční reflexi/self_insight → chybný
            # popis by ho učil nepravdivý příběh o sobě.
            if kind == "instant_lookup":
                _note = ("Odpověděl jsem přes deterministickou cestu (bypass mimo "
                         "mou obvyklou personu). V paměti jsem k tomu NIC neměl, "
                         "tak jsem to v tu chvíli dohledal a odpověděl PROVIZORNĚ "
                         "— do paměti jsem si nic nezapsal, čeká to na noční "
                         "ověření. Odpověď: „%s\"" % _snippet)
            else:
                _note = ("Odpověděl jsem přes deterministickou cestu (bypass mimo "
                         "mou obvyklou personu — přímý výpis z paměti). Nešlo o "
                         "vlastní úvahu, ale o vyzvednutí uloženého faktu. "
                         "Odpověď: „%s\"" % _snippet)
            _data = _json.dumps({"kind": kind}, ensure_ascii=False)
            _c = _sq.connect(_diary, timeout=5.0)
            _c.execute(
                "INSERT INTO diary (ts, event_type, title, note, data, importance) "
                "VALUES (?,?,?,?,?,?)",
                (_tm.time(), "bypass_note",
                 "Bypass odpověď (%s)" % _kind_label, _note, _data, 7))
            _c.commit(); _c.close()
        except Exception as _bne:
            logging.getLogger(__name__).debug(
                'bypass_note write: %s', _bne)

    def send_chat_message(self, name: str, user_message: str,
                          on_sentence=None, channel: str = None) -> str | None:
        """
        Pošle zprávu s historií, uloží exchange.
        Speciální příkaz: /note <text> → uloží do known_persons[name].notes

        HANS_CHAT_CHANNEL_AWARE_V1 — channel: 'web' / 'telegram' / 'voice' /
        'popup' identifikuje původ zprávy. Ukládá se do conv_store jako
        `ch` tag → cross-channel leak (Telegram → web chat „zkus to znova")
        se filtruje. `channel=None` = zpětná kompat.
        """
        # HANS_CHAT_CHANNEL_AWARE_V1 — thread-local pro dispatch/chat_commands.
        try:
            _channel_local.channel = channel
        except Exception:
            pass
        # ── /note příkaz ──────────────────────────────────────────────────
        stripped = user_message.strip()
        if stripped.lower().startswith("/read "):
            url = stripped[6:].strip()
            if url.startswith("http"):
                _hi = getattr(self, '_hans_idle', None)
                if _hi and hasattr(_hi, '_curiosity'):
                    _hi._curiosity.trigger_url(url, topic="manual")
                    from scripts.hans_persona import persona_name as _pn  # PERSONA_NAME_CONFIGURABLE_V1
                    return f"\u2713 {_pn(self.config)} si přečte: {url}"
            return "\u26a0 Zadej platnou URL začínající http"

        if stripped.lower().startswith("/note "):
            note_text = stripped[6:].strip()
            if note_text:
                self._save_note(name, note_text)
                return f"✓ Poznámka uložena: {note_text}"
            else:
                return "⚠ Použití: /note <text poznámky>"

        # ── HANS_READ_URL_NL_V1 — URL v běžné zprávě s intentem čtení ──────────
        # „zjisti víc o X, tu je odkaz https://…" → Hans stránku přečte, uloží do
        # čtenářské paměti (RAG) a zapamatuje si TÉMA (ne jen 'url'/URL). Bez
        # intentu (URL jen tak zmíněná) se nechytá → normální chat.
        if not stripped.startswith("/"):
            import re as _re_u
            _um = _re_u.search(r"https?://\S+", stripped)
            _intent = any(w in stripped.lower() for w in (
                "zjisti", "přečti", "precti", "přečte", "precte", "podívej",
                "podivej", "mrkni", "koukni", "stáhni", "stahni", "odkaz",
                "stránk", "stranka", "nastuduj", "prostuduj"))
            if _um and _intent:
                _url = _um.group(0).rstrip('.,);:!?\'"')
                _topic = self._extract_read_topic(stripped, _url)
                _hi = getattr(self, '_hans_idle', None)
                if _hi and hasattr(_hi, '_curiosity'):
                    _hi._curiosity.trigger_url(_url, topic=_topic)
                    from scripts.hans_persona import persona_name as _pn
                    lbl = ("téma „%s\"" % _topic) if _topic != "url" else "stránku"
                    return ("Přečtu si %s a zapamatuji si, co tam najdu, %s."
                            % (lbl, name or "pane"))

        # ── HANS_DOWNTIME_V1 — uzavření smyčky výpadku ───────────────────
        # Hans se u příchozí osoby zmínil o výpadku a zeptal se, co se dělo
        # (downtime_ctx surfaced). První NE-příkazová odpověď osoby = vyprávění
        # → ulož jako downtime_account a označ answered (zmínka přestane).
        try:
            _hi = getattr(self, '_hans_idle', None)
            _dt = getattr(_hi, '_downtime', None) if _hi else None
            if (_dt and _dt.get('surfaced') and not _dt.get('answered')
                    and not stripped.startswith('/')):
                _dt['answered'] = True
                _hi._log_entry(
                    'downtime_account',
                    'Co se dělo, když jsem byl mimo (od %s)' % name,
                    data=str(_dt.get('gap_hours', '')),
                    note=user_message[:600])
        except Exception:
            pass

        # ── Chat commands (slash + natural language) ─────────────────────
        # CHAT_COMMANDS_DISPATCH_PATCH
        try:
            from scripts.chat_commands import parse_command, dispatch
            # HANS_THREAD_V1 (6.8.) — rozhodovací vrstva byla BEZSTAVOVÁ:
            # parse_command i detektory v _build_grounding dostávaly holou
            # větu, zatímco LLM historii měl. Doloženo 5.8. 19:17-19:23 —
            # korekce „myslel jsem rozhovor s Kolacem" neměla žádný účinek.
            # Tady se z předchozí repliky doplní předmět; ORIGINÁL zůstává
            # pro generaci (persona dál slyší, co uživatel napsal).
            _t_turns = []      # musí existovat i když blok níž selže
            try:
                from scripts import hans_thread as _thr
                _t_turns = _thr.recent_turns(self, name, channel)
                _t_res, _t_subj = _thr.resolve_reference(user_message, _t_turns)
                self._thread_ctx = (user_message, _t_res, _t_subj)
                if _t_subj:
                    print(f"[Chat] thread: odkaz rozřešen → {_t_subj}")
            except Exception as _te:
                self._thread_ctx = None
                print(f"[Chat] thread error: {_te}")
            _cmd = parse_command(user_message)
            # HANS_CONFIRM_PRECEDENCE_V2 (20.8.) — ČEKÁ-LI AGENT NA POTVRZENÍ,
            # LLM ROUTER SE NEPTÁ. Princip už platí od 7.8. pro větev
            # prohloubení (`HANS_CONFIRM_PRECEDENCE_V1`), jen se nikdy
            # nevztáhl na příkazy — a tudy to teklo.
            # Doloženo 20.8.: Hans nabídl zapsat poznámku, uživatel odpověděl
            # „ano" → `HANS_THREAD_LLMROUTE_V1` větu rozvinul na „ano (k tématu:
            # zápis o tom, že jsem si vymyslel divadlo)", router v tom uviděl
            # psaní a poslal ji na /dilo („Právě nepíšu, pane…"). Potvrzení se
            # k agentovi NEDOSTALO a poznámka se nezapsala — přitom Hans o pár
            # vteřin dřív řekl, že si ji zapisuje.
            # ⚠️ Vypíná se JEN dohadovací vrstva (LLM router). Regexy a slash
            # příkazy běží dál: `parse_command('ano'/'ne'/'jo')` vrací None
            # (ověřeno), takže o nic přijít nemůžou, a explicitní „/studium"
            # zůstane explicitním příkazem.
            _confirm_waits = False
            try:
                _agc = self._agent_router()
                _apc = getattr(_agc, "_pending", None) if _agc is not None else None
                _ppc = _apc.get(name) if _apc else None
                if _ppc is not None and (time.time() - _ppc.ts) <= 180:
                    _confirm_waits = True
            except Exception as _cpe2:
                logging.getLogger(__name__).debug('confirm precedence v2: %s', _cpe2)
            if not _cmd and _confirm_waits:
                logging.getLogger(__name__).info(
                    'HANS_CONFIRM_PRECEDENCE_V2: čeká potvrzení návrhu → '
                    'LLM routing přeskočen: %.40s', user_message)
            elif not _cmd:
                # HANS_CMD_LLM_ROUTE_V1 (5.8.) — regexy minuly; zeptej se
                # modelu, jestli věta nežádá o některý ČTECÍ výpis. Řeší
                # „ptám se jinak, než je ve vzorech" (nález uživatele 4.8.).
                # Fail-safe: None → pokračuje běžná cesta beze změny.
                try:
                    from scripts.chat_commands import resolve_command_llm
                    # HANS_THREAD_LLMROUTE_V1 — router posuzuje větu
                    # ROZŘEŠENOU (s předmětem z předchozí repliky), jinak
                    # navazující dotaz hodnotí izolovaně stejně jako regexy.
                    _rt = user_message
                    try:
                        _tc = getattr(self, '_thread_ctx', None)
                        if _tc and _tc[0] == user_message and _tc[1]:
                            _rt = _tc[1]
                    except Exception:
                        pass
                    # HANS_CMD_LLM_ROUTE_V4 — vlákno i pro deterministické
                    # brzdy routeru (kdo je „třetí strana" ví jen kontext).
                    _cmd = resolve_command_llm(_rt, self.config,
                                               turns=_t_turns)
                except Exception as _re:
                    print(f"[Chat] cmd route error: {_re}")
            # HANS_STUDY_CONTENT_RECALL_V1 (14.8.) — „studium" chytá jak regex
            # (`parse_command`, nl_pattern „co ses naučil") tak router, proto
            # guard patří SEM, za obě cesty. Obsahová otázka „co sis odnesl ZE
            # STUDIA X" / „co ses naučil O X" má KONKRÉTNÍ TÉMA → je to dotaz na
            # OBSAH, ne na stav programu → zruš routing na /studium, ať spadne na
            # běžnou cestu (knowledge_check dohledá zápisky). „jak jde studium?"
            # (stav, bez tématu) se do knowledge_check nechytí → výpis zůstane.
            if _cmd and _cmd[0] == "studium":
                try:
                    from scripts.hans_recall import is_knowledge_check_query
                    if is_knowledge_check_query(user_message):
                        print("[Chat] studium+téma → recall "
                              "(HANS_STUDY_CONTENT_RECALL_V1)")
                        _cmd = None
                except Exception:
                    pass
            # HANS_PROVENANCE_NOT_LIST_V1 (19.8.) — „odkud to máš?" / „máš to
            # ze svých zápisků?" po Hansově tvrzení je KONFRONTACE, ne žádost
            # o výpis. Doloženo 19.8. 2× v jednom hovoru: první šla regexem na
            # /zdroje (výpis odkazů ze studia), druhá routerem na /rozhovory
            # (shrnutí 52 výměn) — obě místo odpovědi na položenou otázku.
            # ⚠️ Vzor „odkud to máš" má /zdroje ZÁMĚRNĚ (archiv 2026-07, ř. 358),
            # takže se NERUŠÍ plošně: jen tehdy, když je ve vlákně čerstvé
            # Hansovo tvrzení, které se dá konfrontovat. „Odkud jsi čerpal ke
            # studiu?" bez takového tvrzení dál vypíše odkazy.
            # Proč zrušit routing a nic nedosazovat: dotaz tím propadne na
            # faktickou cestu → když tvrzení nemá oporu, A1 abstinuje a TEPRVE
            # TÍM se spustí CLAIM_RETRACT_V1, který na tyhle formulace vzor
            # (`_SOURCE_Q`) má, ale dosud se k nim nedostal — žije jen uvnitř
            # abstinenční větve. Hotová mašinerie, žádná nová.
            if _cmd and _cmd[0] in ("zdroje", "rozhovory", "cetl"):
                try:
                    from scripts.claim_retract import _SOURCE_Q, find_claim
                    if _SOURCE_Q.search(user_message or ""):
                        _hist_p = []
                        try:
                            _hist_p = self.conv_store.get_history(name) or []
                        except Exception:
                            pass
                        if find_claim(user_message, _hist_p):
                            print("[Chat] provenience → běžná cesta "
                                  "(HANS_PROVENANCE_NOT_LIST_V1)")
                            logging.getLogger(__name__).info(
                                'HANS_PROVENANCE_NOT_LIST_V1: /%s zrušen — '
                                'věta konfrontuje čerstvé tvrzení: %.50s',
                                _cmd[0], user_message)
                            _cmd = None
                except Exception as _pne:
                    logging.getLogger(__name__).debug(
                        'HANS_PROVENANCE_NOT_LIST_V1: %s', _pne)
            if _cmd:
                # HANS_THREAD_V1 — uživatel právě OPRAVIL tutéž cestu, která
                # odpovídala minule → nepouštět ji znovu (vracela by totéž;
                # doloženo 19:19 vs 19:23, kde se lišil jen počet výměn).
                # ÚZKÉ SCHVÁLNĚ: jen při korekci, NE při doslovném opakování
                # („namaluj kočku" 5× za sebou je legitimní záměr).
                try:
                    from scripts import hans_thread as _thr
                    if _thr.should_suppress(name, channel, _cmd[0],
                                            user_message):
                        print(f"[Chat] thread: '{_cmd[0]}' potlačen "
                              f"(korekce) → odpoví model")
                        _cmd = None
                except Exception:
                    pass
            if _cmd:
                # CHAT_COMMANDS_LOG_FIX
                print(f"[Chat] command detected: {_cmd[0]}")
                _reply = dispatch(_cmd, self, name=name)
                try:
                    from scripts import hans_thread as _thr
                    _thr.note_outcome(name, channel, _cmd[0], user_message)
                except Exception:
                    pass
                # Ulož do historie + diary jako normální exchange
                try:
                    self.conv_store.add_exchange(name, user_message, _reply, channel=channel)
                except Exception:
                    pass
                self._log_human_chat_to_diary(name, user_message, _reply)
                return _reply
        except Exception as _ce:
            print(f"[Chat] command dispatch error: {_ce}")

        # ── HANS_CHAT_WAIT_FOR_PAINT_V1 (5.8.) — maluju, ozvu se potom ──────
        # Chatová odpověď natáhne hans-czech (8 GB) do VRAM a tím podřízne
        # běžící render: FLUX se nevejde, spadne do lowvram a obraz trvá
        # násobně dýl (naměřeno 475 s vs 210 s). `pause_warmup` tenhle případ
        # NEKRYJE — ta zabíjí jen automatické re-piny, ne reálný chat.
        # Proto radši krátká poctivá věta než tichý sabotovaný obraz.
        # ZÁMĚRNĚ AŽ ZA PŘÍKAZY: `/stav`, `/zdravi` apod. musí jít i při
        # malování (jsou deterministické, mozek nepotřebují).
        try:
            from scripts.avatar_render import render_in_progress
            if render_in_progress():
                from scripts.hans_persona import persona_name as _pn
                _reply = ("Zrovna maluji, pane — až obraz dokončím, budu se "
                          "Vám plně věnovat. Chvilku strpení.")
                try:
                    self.conv_store.add_exchange(name, user_message, _reply,
                                                 channel=channel)
                except Exception:
                    pass
                print("[Chat] odloženo — %s právě renderuje obraz" % _pn(self.config))
                return _reply
        except Exception as _pe:
            print(f"[Chat] paint-wait gate error: {_pe}")

        # HANS_KNOWLEDGE_CHECK_V1 BYPASS (18.7. → 19.7.) — hans-czech persona
        # halucinuje „mám v paměti záznamy" i pro věci, které nikdy neviděl
        # (doložený Červený trpaslík). Grounding block s explicit „PAMĚŤ
        # NEOBSAHUJE X" NEZABRAL (persona > grounding). Bypass jako sources_answer.
        # HANS_DATETIME_ANSWER_V1 — datum/čas ze systémových hodin, deterministicky
        # a PŘED modelem (jinak si datum rozepíše špatně a A1 pak abstinuje).
        try:
            from scripts.hans_recall import datetime_answer as _dta
            _dtans = _dta(user_message)
            if _dtans:
                print("[Chat] HANS_DATETIME_ANSWER_V1 → deterministická odpověď")
                try:
                    self.conv_store.add_exchange(name, user_message, _dtans,
                                                 channel=channel)
                except Exception:
                    pass
                self._log_human_chat_to_diary(name, user_message, _dtans,
                                              bypass_kind="datetime")
                return _dtans
        except Exception as _dte:
            print(f"[Chat] datetime answer error: {_dte}")

        # HANS_ASKER_STATE_V1 — „vidíte mě?" / „kdo jsem já?" ze živých dat.
        try:
            from scripts.hans_recall import asker_state_answer
            _hi_as = getattr(self, "_hans_idle", None)
            _as = asker_state_answer(
                user_message, name,
                getattr(_hi_as, "_present_names", None) or [], self.config)
            if _as:
                print("[Chat] HANS_ASKER_STATE_V1 → odpověď ze živého stavu")
                try:
                    self.conv_store.add_exchange(name, user_message, _as,
                                                 channel=channel)
                except Exception:
                    pass
                self._log_human_chat_to_diary(name, user_message, _as,
                                              bypass_kind="asker_state")
                return _as
        except Exception as _ase:
            print(f"[Chat] asker state error: {_ase}")

        # HANS_BOOK_RECOMMEND_V1 — doporučení z VLASTNÍ četby, ne z fantazie.
        try:
            from scripts.hans_recall import (asks_book_recommendation,
                                             book_recommendation)
            if asks_book_recommendation(user_message):
                _br = book_recommendation(
                    (self.config.get("diary_db")
                     or "data/hans_diary.db"), self.config)
                if _br:
                    print("[Chat] HANS_BOOK_RECOMMEND_V1 → z dočtených knih")
                    try:
                        self.conv_store.add_exchange(name, user_message, _br,
                                                     channel=channel)
                    except Exception:
                        pass
                    self._log_human_chat_to_diary(name, user_message, _br,
                                                  bypass_kind="book_recommend")
                    return _br
        except Exception as _bre:
            print(f"[Chat] book recommend error: {_bre}")

        try:
            from scripts.hans_recall import knowledge_check_bypass, person_card
            _dbp_kb = (self.config.get("diary_db")
                       or (self.config.get("hans_idle", {}) or {}).get("diary_db")
                       or "data/hans_diary.db")
            # HANS_PERSON_CARD_BYPASS_V1 (18.8.) — OSOBA MÁ PŘEDNOST PŘED
            # DOHLEDÁVÁNÍM. Doloženo živě 18.8.: „a co víš o Janě?" → Hans
            # popsal vesnici Henčov u Jihlavy, protože v paměti nic nenašel a
            # spustil `lookup_now`. Fakt o paní domu přitom leží v
            # `relationships`. Agentní akce `report_person` to nezachytí —
            # „co víš o X?" se odbočí SEM a agenta vůbec nepotká.
            # ⚠️ Nestačilo vložit to do `_build_grounding`: ta skládá jen
            # PODKLAD PRO MODEL, kdežto tenhle bypass odpovídá uživateli přímo
            # a model už nespustí (na to jsem při stavbě naletěl).
            # Neosobní dotaz („co víš o hradech?") vrátí prázdno → beze změny.
            # ⚠️ BRÁNA (nález z živého testu 18.8.): bez ní se karta vysypala i na
            # KONVERZAČNÍ větu, která jméno jen zmiňuje — „myslíš, že by Jana
            # měla radost z kávovaru?" dostalo místo odpovědi výpis karty.
            # `person_card` sám o sobě říká jen „ta věta jmenuje známou osobu",
            # ne „ptá se, KDO to je“. Kartu proto pustíme jen u znalostního
            # dotazu (tvar „co víš o X“); tvar „kdo je X“ řeší agentní akce
            # `report_person` dřív a sem nedojde.
            _pcard = ""
            try:
                from scripts.hans_recall import (is_knowledge_check_query as _ikc,
                                                 asks_about_person as _aap)
                # HANS_PERSON_ASK_PAT_V1 — sama `is_knowledge_check_query` je
                # moc úzká: „na Janu jsi zapomněl, ne? co o ní víš" jí NEPROJDE
                # (doloženo živě 18.8.) a LLM router to pak poslal na výpis zájmů.
                if _ikc(user_message) or _aap(user_message, self.config):
                    # HANS_PERSON_CARD_VOICE_V1 — kartu vyslov, nevysypej
                    from scripts.hans_recall import person_card_voiced
                    _pcard = person_card_voiced(_dbp_kb, user_message,
                                                self.config, asker=name)
            except Exception:
                _pcard = ""
            _kb = _pcard or knowledge_check_bypass(_dbp_kb, user_message, asker=name)
            if _kb:
                # HANS_INSTANT_LOOKUP_V1 (4.8.) — „nemám záznam" už není konec:
                # zkus téma DOHLEDAT HNED a odpovědět PROVIZORNĚ. Do paměti se
                # přitom NEZAPÍŠE nic (nález jde do čekárny `unverified_findings`),
                # ověří se v noci a ráno se případně pošle oprava. Když dohledání
                # nevyjde (mozek dole / článek nenalezen / gate), padáme zpět na
                # původní poctivé „nemám záznam" — žádná regrese.
                _bypass_kind = "person_card" if _pcard else "knowledge_check"
                try:
                    if _pcard:
                        raise _SkipLookup()   # HANS_PERSON_CARD_BYPASS_V1
                    from scripts.hans_recall import _extract_knowledge_topic
                    from scripts.hans_findings import lookup_now
                    _topic_kb = _extract_knowledge_topic(user_message)
                    if _topic_kb:
                        _prov = lookup_now(self.config, _dbp_kb, _topic_kb,
                                           user_message, asker=name)
                        if _prov:
                            _kb = _prov
                            _bypass_kind = "instant_lookup"
                            print("[Chat] HANS_INSTANT_LOOKUP_V1 → provizorní "
                                  "odpověď z dohledání (čeká na noční ověření)")
                except _SkipLookup:
                    print("[Chat] HANS_PERSON_CARD_BYPASS_V1 → karta osoby "
                          "(dohledávání přeskočeno)")
                except Exception as _ile:
                    print(f"[Chat] instant lookup error: {_ile}")
                if _bypass_kind == "knowledge_check":
                    print("[Chat] HANS_KNOWLEDGE_CHECK_V1 → deterministic bypass")
                try:
                    self.conv_store.add_exchange(name, user_message, _kb, channel=channel)
                except Exception:
                    pass
                # HANS_BYPASS_TRACE_V1 — označ deterministickou cestu
                self._log_human_chat_to_diary(name, user_message, _kb,
                                              bypass_kind=_bypass_kind)
                return _kb
        except Exception as _kce:
            print(f"[Chat] knowledge check bypass error: {_kce}")

        # HANS_SOURCE_QUERY_V1 — bypass LLM (17.7.). hans-czech persona
        # odmítá sdílet URL i s explicitním groundingem — persona finetune
        # silnější než system prompt. Vzor `commitments_answer` /
        # `film_knowledge_answer`: deterministická odpověď mimo LLM.
        try:
            from scripts.hans_recall import is_source_query, sources_answer
            if is_source_query(user_message):
                _dbp_sa = (self.config.get("diary_db")
                           or (self.config.get("hans_idle", {}) or {}).get("diary_db")
                           or "data/hans_diary.db")
                _sa = sources_answer(_dbp_sa, user_message, asker=name)
                if _sa:
                    print("[Chat] HANS_SOURCE_QUERY_V1 → deterministic bypass")
                    try:
                        self.conv_store.add_exchange(name, user_message, _sa, channel=channel)
                    except Exception:
                        pass
                    # HANS_BYPASS_TRACE_V1 (19.7.) — sdílený bypass_note přes
                    # _log_human_chat_to_diary(bypass_kind=…). Dřív inline
                    # (HANS_SOURCE_QUERY_BYPASS_NOTE_V1 18.7.), teď 1 cesta
                    # pro všechny bypass kinds.
                    self._log_human_chat_to_diary(name, user_message, _sa,
                                                  bypass_kind="sources")
                    return _sa
        except Exception as _sae:
            print(f"[Chat] source query answer error: {_sae}")

        # HANS_STUDY_DEEPEN_V2 — kritika/rozhodnutí ČISTÝM TEXTEM (ne jen
        # /prohloubit). Gated: jen když čeká návrh prohloubení. Klasifikuje
        # reakci uživatele a rovnou ji aplikuje.
        try:
            _dr = self._maybe_deepen_response(name, user_message)
            if _dr:
                try:
                    self.conv_store.add_exchange(name, user_message, _dr, channel=channel)
                except Exception:
                    pass
                self._log_human_chat_to_diary(name, user_message, _dr)
                return _dr
        except Exception as _de:
            print(f"[Chat] deepen response error: {_de}")

        # ── HANS_AGENT_V1 — agentní vrstva (kontextové akce z konverzace) ──
        # PO parse_command (příkazy mají přednost), PŘED běžným chatem.
        # (1) čeká na osobu potvrzení návrhu? ano/ne → proveď/zruš.
        # (2) jinak router: přeje si uživatel akci? → návrh + [ano/ne].
        # Deferral-safe: výpadek LLM / vypnuto → None → běžný chat pokračuje.
        try:
            _agent = self._agent_router()
            if _agent is not None:
                _conf = _agent.check_confirmation(self, name, user_message)
                if _conf is not None:
                    try:
                        self.conv_store.add_exchange(name, user_message, _conf, channel=channel)
                    except Exception:
                        pass
                    self._log_human_chat_to_diary(name, user_message, _conf)
                    return _conf
                _prop = _agent.propose(self, name, user_message)
                if _prop:
                    try:
                        self.conv_store.add_exchange(name, user_message, _prop, channel=channel)
                    except Exception:
                        pass
                    if on_sentence:
                        try:
                            on_sentence(_prop)
                        except Exception:
                            pass
                    self._log_human_chat_to_diary(name, user_message, _prop)
                    return _prop
        except Exception as _ae:
            print(f"[Chat] agent layer error: {_ae}")

        system   = self._build_system(name, user_msg=user_message)
        # Prefix user message jménem osoby pro lepší RAG retrieval.
        # "kdo jsem?" → "<jméno> se ptá: kdo jsem?" → embedding najde kartu osoby
        # místo kdo_je_hans.txt. Originál se ukládá do historie bez prefixu.
        # USER_NAME_PREFIX_PATCH
        # HANS_ASKER_PREFIX_RETRIEVAL_ONLY_V1 (19.8.) — prefix šel dřív i do
        # GENERACE a model si tu rámovací větu bral za předlohu odpovědi:
        # „Stando žádá upřesnění ohledně základů hradu", „Stando přeje dobrý
        # den", „Zaznamenal jsem připomínku pana Standy" — 5× z 10 odpovědí v
        # rozhovoru (19.8.), a totéž je nález 8 z testu očima cizího člověka.
        # Změřeno, že to NEDĚLÁ `cz_names.fix_addressee` (na 1. pád na začátku
        # věty nesahá) — píše to sám model podle toho, co dostal.
        # Prefix ale VZNIKL kvůli RAG retrievalu („kdo jsem?" → embedding najde
        # kartu osoby) a `HANS_QUERY_REWRITER_F1_V1` s ním počítá → nemazat,
        # jen ZÚŽIT: retrieval ho dostane, generace ne.
        _raw_message = user_message
        _q_for_retrieval = user_message
        if name and name.lower() not in user_message.lower():
            _q_for_retrieval = f"{name} se ptá: {user_message}"
        # ── HANS_SELFCONSISTENCY_A1_V1 ────────────────────────────────────
        # Předpočítej grounding JEDNOU (šetří RAG oproti výpočtu ve
        # _stream_message) a zjisti výsledek. Jen u 'factual_nofacts'
        # (faktický dotaz BEZ opory v RAG = rizikový volný výmysl) spusť A1
        # self-consistency: N× generuj, změř rozptyl → nestabilní → deter-
        # ministická abstinence (routing, ne prompt). Deferral-safe.
        _grounding = _GROUNDING_UNSET
        _a1_abstain = False
        try:
            _grounding = self._build_grounding(_q_for_retrieval, name)
            if getattr(self, '_grounding_outcome', '') == 'factual_nofacts':
                # HANS_A1_NOT_FOR_OWN_STATE_V1 (20.8.) — A1 hlídá SVĚTOVÁ
                # tvrzení bez opory. Otázka NA HANSE nebo NA DĚNÍ V DOMĚ ale
                # oporu má — jen ne v RAG, nýbrž v system promptu (bloky `cap`,
                # self_state, přítomnost, kodi, počasí). `factual_nofacts` je
                # u nich CHYBNÁ DIAGNÓZA a abstinence pak zapře odpověď, kterou
                # má Hans před sebou. Doloženo 20.8.: „umíte pustit něco na
                # televizi?" → sim=0.789 thr=0.85 → „nemám spolehlivý záznam",
                # ačkoli blok schopností v promptu byl. Táž třída jako
                # HANS_DATETIME_ANSWER_V1 (19.8.), kde totéž potkalo datum.
                # ⚠️ Klasifikaci NEVYRÁBÍME novou — `self_topic` jen přestal
                # zahazovat kategorii, kterou `_SELF_SYSTEM` už rozlišoval
                # (HANS_SELF_TOPIC_V1). Ptáme se AŽ TADY, tedy jen když by
                # jinak běželo N generací A1 → levnější než to, co nahrazuje.
                # Ostatní brzdy (grounding_guard, CLAIM_RETRACT, provenience)
                # zůstávají — tohle vypíná JEN self-consistency.
                _skip_a1 = False
                try:
                    from scripts.hans_intent import self_topic
                    # HANS_A1_THREAD_TEXT_V1 (21.8.) — KLASIFIKUJ ROZŘEŠENOU
                    # VĚTU, NE HOLOU. Doloženo 20.8. 14:44: „kdo tam hraje?"
                    # → self_topic 'dum' (čte to jako dotaz na televizi) → A1
                    # přeskočena → Hans si vymyslel obsazení filmu, o kterém
                    # nemá ŽÁDNÝ záznam (tři jména, jedno z nich ani není herec).
                    # Přitom o řádek výš už F1 věděl, že jde o „Kdo hraje
                    # v Tureckých náušnicích?" → 'osoba' → brzda by běžela.
                    # Táž třída jako HANS_THREAD_V1 [[stateless-decision-layer]]:
                    # detektor dostal holou větu, zatímco kontext byl k mání.
                    # Změřeno: brzda se od 20.8. vypnula 6×, škodu udělal
                    # právě tenhle jeden dotaz → překlopí se jen on.
                    _a1_text = getattr(self, '_f1_query', None) or _raw_message
                    if _a1_text != _raw_message:
                        logging.getLogger(__name__).info(
                            'HANS_A1_THREAD_TEXT_V1: A1 se rozhoduje z %r '
                            '(místo %r)', _a1_text[:60], _raw_message[:40])
                    _st = self_topic(_a1_text, self.config)
                    if _st in ('asistent', 'dum'):
                        _skip_a1 = True
                        logging.getLogger(__name__).info(
                            'HANS_A1_NOT_FOR_OWN_STATE_V1: A1 přeskočena — '
                            'dotaz je %r (opora je v promptu, ne v RAG): %.50s',
                            _st, _raw_message)
                except Exception as _ste:
                    logging.getLogger(__name__).debug('self_topic: %s', _ste)
                if not _skip_a1:
                    from scripts.hans_selfconsistency import is_unstable
                    if is_unstable(self.config, _raw_message) is True:
                        _a1_abstain = True
        except Exception as _a1e:
            logging.getLogger(__name__).warning('A1 gate failed: %s', _a1e)
            _grounding = _GROUNDING_UNSET
        # ── HANS_OPINION_GROUNDING_G1_V1 ─────────────────────────────────
        # Názorový/filosofický dotaz (imaginativní registr) → místo faktů
        # injektuj Hansovy VLASTNÍ postoje + odvahu zaujmout stanovisko
        # (zrcadlo faktického groundingu). Jen když intent NENÍ faktický —
        # faktická cesta má G3B/A1/C1, tahle je pro „co si myslíš o…".
        try:
            _oc = getattr(self, '_grounding_outcome', '')
            from scripts.hans_opinion import is_opinion_query, opinion_block
            # 'opinion' = routing v _build_grounding už rozhodl; 'skip' =
            # grounding neběžel (intent/knowledge nezapojeny) → rozhodni tady.
            if _oc == 'opinion' or (_oc == 'skip'
                                    and is_opinion_query(_raw_message)):
                _ob = opinion_block(self.config)
                if _ob:
                    system += _ob
                    logging.getLogger(__name__).info(
                        'G1: názorový dotaz → blok vlastních postojů '
                        'injektován (%d zn)', len(_ob))
        except Exception as _oge:
            logging.getLogger(__name__).warning(
                'G1 opinion grounding failed: %s', _oge)
        if _a1_abstain:
            # HANS_ANCHOR_LOOKUP_V1 — než odmítneš, zkus to dohledat.
            response = self._dohledej_kotvu(_raw_message, name) or A1_ABSTAIN_TEXT
            # CLAIM_RETRACT_V1 — brzda umí ODMÍTNOUT, ale neuměla se OPRAVIT.
            # Doloženo 6.8. 09:10→09:12: Hans tvrdil „hradby až 5 metrů",
            # o 80 s později přiznal „nemám spolehlivý záznam" — ale to číslo
            # nechal viset jako fakt (v jeho zápiscích NENÍ). Když se tedy
            # abstinuje, dohlédni, jestli o tomtéž sám před chvílí něco
            # netvrdil, a vezmi to výslovně zpět. Deterministické, bez LLM.
            try:
                from scripts.claim_retract import append_retraction
                _hist = []
                try:
                    _hist = self.conv_store.get_history(name) or []
                except Exception:
                    pass
                _resp2 = append_retraction(response, _raw_message, _hist)
                if _resp2 != response:
                    logging.getLogger(__name__).info(
                        'CLAIM_RETRACT_V1: beru zpět dřívější tvrzení '
                        '(abstinence u %r)', (_raw_message or '')[:60])
                    response = _resp2
            except Exception as _cre:
                logging.getLogger(__name__).warning(
                    'CLAIM_RETRACT_V1 selhal (odpověď ponechána): %s', _cre)
            if on_sentence:
                try:
                    on_sentence(response)   # ať to TTS vysloví
                except Exception:
                    pass
        else:
            response = self._stream_message(
                (system, user_message), name=name,
                on_sentence=on_sentence, grounding=_grounding)  # CHAT_ON_SENTENCE_V1
        # G4D_DEDUP_ADDRESS_V1 — očisti opakované oslovení PŘED
        # rozdvojením do conv_store i diary→RAG (oba cíle čisté).
        if response:
            # HANS_FILM_DIRECTOR_CHECK_V1 (21.8.) — přát si film, který doma
            # nemáme, je v pořádku (zvídavost), ale režiséra má mít správně.
            # Doloženo v simulovaném rozhovoru: „Sedmikrásky od Miloše Formana"
            # (natočila je Věra Chytilová a Hans o tom filmu nemá záznam).
            # Ověřuje se KNIHOVNOU, jinak Wikipedií; co ověřit nejde, zůstává.
            try:
                from scripts.film_director_check import zkontroluj_rezii
                _kodi_r = getattr(getattr(self, "_hans_idle", None), "kodi", None)
                _r2 = zkontroluj_rezii(response, kodi=_kodi_r, config=self.config)
                if _r2 != response:
                    response = _r2
            except Exception as _fdc:
                logging.getLogger(__name__).debug(
                    'HANS_FILM_DIRECTOR_CHECK_V1 přeskočen: %s', _fdc)
            # GROUNDING_GUARD_V1 — nepřidal si k podkladu vlastní fakta?
            # Doloženo 12.8. (vrak u Sicílie): na PRVNÍ dotaz odpověděl přesně
            # podle zdroje, na DRUHÝ („zjisti více") už nebylo z čeho a vyrobil
            # si náklad, obchodní cestu i stav vraku — a podal to jako „Zprávy
            # uvádějí". Instrukce ANTIKONFAB tenhle obrat VÝSLOVNĚ zakazuje a
            # model ji přesto porušil → prompt to neuhlídá, musí to být kontrola
            # PO generování. Běží jen u faktických odpovědí s podkladem
            # ('grounded'), aby se nesahalo na běžný hovor.
            # PŘED zápisem do conv_store i deníku — ať se vymyšlené věty
            # nedostanou do paměti (týž důvod jako u oprav oslovení níž).
            try:
                if (getattr(self, '_grounding_outcome', '') == 'grounded'
                        and _grounding and _grounding is not _GROUNDING_UNSET):
                    from scripts.grounding_guard import check as _gg_check
                    _facts = _grounding.replace(ANTIKONFAB, ' ')
                    _facts = _facts.replace(ANTIKONFAB_NOFACTS, ' ')
                    # ⚠️ REFERENCÍ MUSÍ BÝT VŠECHNO, CO MODEL DOSTAL, ne jen
                    # grounding. První živý test (13:49) zahodil VĚTU, KTERÁ
                    # PODLOŽENÁ BYLA — fakta měl z historie rozhovoru, kdežto
                    # grounding v tu chvíli nesl jiný zápisek. Bez historie
                    # guard trestá správné odpovědi.
                    try:
                        for _h in (self.conv_store.get_history(name) or [])[-6:]:
                            _facts += ' ' + str(
                                _h.get('content', _h) if isinstance(_h, dict) else _h)
                    except Exception:
                        pass
                    _clean, _dropped = _gg_check(response, _facts)
                    # GROUNDING_GUARD_ACTIVE_V2 (22.8.) — ÚZKÉ ZAPNUTÍ.
                    # Doloženo 22.8. na hradu Kost: podklad byl JEDNA věta ze
                    # studijní poznámky, odpověď osm vět (Bořkovští z Kostedna,
                    # hradní park, bílá paní) — guard napočítal 6 vět bez opory
                    # a jen to zapsal do logu. Brzda A1 je u neznámého hesla
                    # loterie (21.8. zabrala, 22.8. na tutéž otázku ne), protože
                    # porovnává dva vzorky téhož modelu = shodu dvou výmyslů.
                    # ZASAHUJE SE JEN, když obojí:
                    #   (a) podklad je tenký fallback ze zápisků (`zapisky_*`),
                    #       ne plný RAG — falešné poplachy z 12.8., kvůli kterým
                    #       se guard vypnul, byly PRÁVĚ na RAG cestě, kam guard
                    #       nevidí (fakta tečou i z kontextu),
                    #   (b) bez opory jsou aspoň 4 věty — tamty poplachy byly
                    #       po JEDNÉ větě, takže tudy neprojdou.
                    # Mimo tyhle dvě podmínky se chová jako dosud: JEN HLÁSÍ.
                    _cesta = getattr(self, '_grounding_cesta', '') or ''
                    _tenky = _cesta.startswith('zapisky')
                    if _dropped and _tenky and len(_dropped) >= 4:
                        logging.getLogger(__name__).info(
                            'GROUNDING_GUARD_ACTIVE_V2: ZASAHUJI — %d vět bez '
                            'opory u tenkého podkladu (%s). První: %r',
                            len(_dropped), _cesta, _dropped[0][:80])
                        # HANS_ANCHOR_LOOKUP_V1 (22.8.) — vykuchaná odpověď
                        # NENÍ konec. Když se ukázalo, že podklad tvrzení
                        # neunese, je to totéž jako „nemám záznam" — a na to
                        # už máme dohledání (HANS_INSTANT_LOOKUP_V1, 4.8.):
                        # článek TEĎ, do paměti až po nočním ověření. U hradu
                        # Kost se nikdy nespustilo právě proto, že ho předběhl
                        # tenký falešný podklad (odpověď se tvářila jako
                        # `grounded`), takže Hans k přiznání nedošel.
                        _dohl = self._dohledej_kotvu(_raw_message, name)
                        response = _dohl or _clean
                    elif _dropped:
                        # ⛔ POUZE HLÁSÍ, NEZASAHUJE (přepnuto 12.8. po dvou
                        # falešných poplaších naživo). Guard stojí na
                        # předpokladu, že jde vyjmenovat všechno, co model
                        # dostal — a ten v téhle architektuře NEPLATÍ: fakta
                        # tečou i z RAG a kontextu, kam guard nevidí. Zahodil
                        # proto větu, která je doslova v uloženém zdroji, a
                        # protože se abstinence ukládá do historie rozhovoru,
                        # SAMO SE TO POSILOVALO (čím víc odmítl, tím míň měl
                        # čím podložit další odpověď).
                        # Zapnout zpět až bude reference úplná — viz BACKLOG.
                        logging.getLogger(__name__).info(
                            'GROUNDING_GUARD_V1 [jen hlásím]: %d vět bez opory '
                            'v podkladu. První: %r', len(_dropped),
                            _dropped[0][:80])
            except Exception as _gge:
                logging.getLogger(__name__).warning(
                    'GROUNDING_GUARD_V1 selhal (odpověď ponechána): %s', _gge)
            try:
                from scripts.conversation_store import dedup_address_g4d
                response = dedup_address_g4d(response, name, self.config)
            except Exception:
                pass
            # HANS_ADDRESSEE_V2 — deterministická oprava oslovení CIZÍ osoby.
            # Prompt na tohle nestačí (persona finetune ho přebíjí): i po
            # zesílení instrukce a přesunu adresáta na konec promptu Hans
            # občas odpověděl „Jsem v pořádku, Jano" jinému uživateli. Přepisuje se
            # jen VOKATIV (a titul+jméno) — zmínky ve 3. osobě zůstávají.
            # Běží PŘED zápisem do conv_store i deníku/RAG, ať jsou čisté
            # všechny cíle (týž důvod jako u dedup_address_g4d výše).
            try:
                from scripts.cz_names import fix_addressee
                response, _nfix = fix_addressee(response, name, self.config)
                if _nfix:
                    logging.getLogger(__name__).info(
                        "HANS_ADDRESSEE_V2: opraveno %d cizích oslovení "
                        "(partner=%s)", _nfix, name)
            except Exception:
                pass
        if response:
            self.conv_store.add_exchange(name, _raw_message, response, channel=channel)
            # # HUMAN_CHAT_VIA_LOG_ENTRY
            # Vztahové karty + paměť — zaloguj exchange do deníku jako
            # human_chat. Přes _log_entry → spustí synthesis_hooks
            # → vytvoří chat_reflection → upload do hans_identita RAG.
            _note = f"{name}: {_raw_message}\nHans: {response}"
            # HANS_TEST_PERSON_V1 — u testovací identity se přeskočí OBOJÍ:
            # deník i RAG. ⚠️ NESTAČÍ vynulovat `_hi_log` — tím by se naopak
            # spustila záložní SQL větev níž a řádek by se zapsal stejně.
            _skip_mem = self._is_test_person(name)
            _hi_log = None if _skip_mem else getattr(self, "_hans_idle", None)
            if _hi_log and hasattr(_hi_log, "_log_entry"):
                try:
                    _hi_log._log_entry("human_chat", name, note=_note)
                except Exception as _e:
                    print(f"[Chat] human_chat log_entry failed: {_e}")
                    _hi_log = None
            if not _hi_log and not _skip_mem:
                # Fallback — přímý SQL
                try:
                    import sqlite3 as _sql, time as _t
                    _diary = (self.config.get("diary_db", "data/hans_diary.db")
                              if hasattr(self, "config") else "data/hans_diary.db")
                    with _sql.connect(_diary) as _db:
                        _db.execute(
                            "INSERT INTO diary (ts, event_type, title, note) "
                            "VALUES (?,?,?,?)",
                            (_t.time(), "human_chat", name, _note)
                        )
                        _db.commit()
                except Exception as _e:
                    print(f"[Chat] human_chat diary log failed: {_e}")
            # HANS_CHAT_RECALL_V1 — ulož VĚRNÝ obsah rozhovoru do RAG (verbatim,
            # datovaný) → „vzpomínáš na X?" stojí na skutečných datech, ne na
            # vágní chat_reflection (ta ukládá jen dojem, ne téma). Na pozadí
            # (RAG = síťový hop), best-effort.
            try:
                if not _skip_mem:
                    self._upload_chat_memory(name, _raw_message, response)
            except Exception as _e:
                print(f"[Chat] chat memory upload failed: {_e}")
        return response

    def _upload_chat_memory(self, name: str, question: str, answer: str):
        """HANS_CHAT_RECALL_V1 — verbatim rozhovor do RAG (hans_pripady), aby byl
        později sémanticky dohledatelný. Threadovaně, deferral-safe."""
        _kn = getattr(self, "knowledge", None)
        if _kn is None or not (question or "").strip():
            return
        import threading as _th
        import time as _t

        def _work():
            try:
                from scripts.hans_persona import persona_name
                pname = persona_name(self.config)
            except Exception:
                pname = "Hans"
            ts = _t.time()
            import datetime as _dt
            when = _dt.datetime.fromtimestamp(ts).strftime("%A %-d.%-m.%Y %H:%M")
            # HANS_CHATLOG_NOT_FACT_V1 — původ přímo v textu, ať je i pro
            # člověka (a pro každou budoucí cestu) zřejmé, že tohle NENÍ
            # ověřená znalost, ale co Hans v hovoru řekl.
            text = (f"Rozhovor s {name} ({when}):\n"
                    f"[NEOVĚŘENO — vlastní výrok v hovoru, ne ověřený fakt]\n"
                    f"{name}: {question.strip()}\n{pname}: {answer.strip()}")
            try:
                _kn.upload(
                    collection_key="hans_pripady",
                    doc_id=f"chatlog_{int(ts)}_{name}",
                    title=f"Rozhovor s {name}: {question.strip()[:60]}",
                    text=text,
                    metadata={"kdy": when, "osoba": name, "typ": "rozhovor",
                              # HANS_CHATLOG_NOT_FACT_V1
                              "overeno": False, "puvod": "vlastni_vyrok"})
            except Exception as _e:
                print(f"[Chat] chat memory upload (worker): {_e}")
        _th.Thread(target=_work, daemon=True, name="ChatMemoryUpload").start()

    @staticmethod
    def _extract_read_topic(msg: str, url: str) -> str:
        """HANS_READ_URL_NL_V1 — z uživatelovy zprávy vytáhni TÉMA čtení
        (na co se ptá), aby web_read neslo neurčité 'url'. Priorita:
        uvozovkovaný termín → 'o <termín>' → 'url' fallback."""
        import re as _re
        m = (msg or "").replace(url, " ")
        # 1) termín v uvozovkách („X" / "X" / 'X')
        q = _re.search(r"[\"'„»“]([^\"'„»“”«]{2,40})[\"'“”«]", m)
        if q and q.group(1).strip():
            return q.group(1).strip()[:40]
        # 2) „o [jazyku/tématu/…] <termín>" (1-2 slova)
        o = _re.search(
            r"\bo\s+(?:jazyku|jazyce|t[ée]matu|str[áa]nce|filmu|knize|"
            r"projektu|autorovi|m[eě]st[eě]|)\s*"
            r"([A-Za-zÁ-Žá-ž0-9][\wÁ-Žá-ž]{2,30}(?:\s+[A-Za-zÁ-Žá-ž0-9]"
            r"[\wÁ-Žá-ž]{2,30})?)", m, _re.IGNORECASE)
        if o and o.group(1).strip():
            return o.group(1).strip()[:40]
        return "url"

    def _save_note(self, name: str, note_text: str):
        """Uloží poznámku do known_persons[name].notes v config.json."""
        try:
            config_path = Path("config.json")
            with open(config_path, encoding="utf-8") as f:
                cfg = json.load(f)

            persons = cfg.setdefault("known_persons", {})
            if name not in persons:
                persons[name] = {"gender": "", "notes": ""}
            if not isinstance(persons[name], dict):
                persons[name] = {"gender": "", "notes": str(persons[name])}

            existing = persons[name].get("notes", "").strip()
            if existing:
                persons[name]["notes"] = existing + " " + note_text
            else:
                persons[name]["notes"] = note_text

            with open(config_path, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=4, ensure_ascii=False)

            # Aktualizuj živý config aby se projevilo hned
            self.config.setdefault("known_persons", {}).setdefault(
                name, {"gender": "", "notes": ""})
            if isinstance(self.config["known_persons"][name], dict):
                existing_live = self.config["known_persons"][name].get("notes", "").strip()
                if existing_live:
                    self.config["known_persons"][name]["notes"] = (
                        existing_live + " " + note_text)
                else:
                    self.config["known_persons"][name]["notes"] = note_text

            print(f"[Chat] /note uložena pro '{name}': {note_text}")
        except Exception as e:
            print(f"[Chat] /note save error: {e}")

    def ping_model(self):
        """Keepalive — udrzi model v VRAM pres Ollama /api/generate."""
        try:
            from scripts.ollama_client import game_mode_on, warmup_paused
            if game_mode_on():   # OLLAMA_GAME_MODE_V1 — nepřipínej, VRAM volná pro hru
                return
            # HANS_STUDY_VRAM_HANDOFF_V1 — během base-model dávky (studium/
            # analytika/immune volá pause_warmup) NEpřipínej hans-czech, jinak
            # 4min ping re-pinuje 8GB model a evictuje base OpenEuroLLM uprostřed
            # dlouhého generování (8+8 > 16GB) → 300s timeout. Stejný princip
            # jako herní mód. hans-czech se dotáhne on-demand při reálném chatu.
            if warmup_paused():
                return
        except Exception:
            pass
        try:
            # Ollama /api/generate s keep_alive=10m — model zustane v VRAM
            base = self.config.get('openwebui_chat', {}).get(
                'base_url', 'http://localhost:11434')
            requests.post(
                f'{base}/api/generate',
                json={'model': self.model_name,
                      'prompt': '',
                      'keep_alive': '20m'},
                timeout=10,
            )
        except Exception:
            pass

    # ── Helpers ───────────────────────────────────────────────────────────────

    def get_available_models(self) -> list:
        try:
            r = requests.get(f"{self.base_url}/api/v1/models",
                             headers=self._headers(), timeout=10)
            if r.status_code == 200:
                return [m.get("id", "") for m in r.json().get("data", [])]
        except Exception:
            pass
        return []

    def get_greeting_stats(self) -> dict:
        return {
            "greeting_mode":   self.greeting_mode,
            "session_greeted": list(self.session_greeted),
            "daily_greeted":   list(self.daily_greeted),
        }

    def get_chat_stats(self) -> dict:
        stats = {
            "enabled":           self.enabled,
            "base_url":          self.base_url,
            "model_name":        self.model_name,
            "greeting_enabled":  self.greeting_enabled,
            "popup_enabled":     self.popup_enabled,
            "tts_connected":     self.tts_speaker is not None,
            "surroundings_db":   self.surroundings_db is not None,
            "conversation_history": self.conv_store.summary(),
        }
        stats.update(self.get_greeting_stats())
        if self.popup_manager:
            stats["active_popup_windows"] = self.popup_manager.get_active_count()
        return stats

    def _log_interaction(self, user_name, user_message, ai_response):
        if not self.chat_config.get("log_interactions", False):
            return
        try:
            entry = {"timestamp": datetime.now().isoformat(),
                     "user": user_name,
                     "user_message": user_message,
                     "ai_response": ai_response}
            with open(self.chat_config.get("log_file",
                      "data/chat_interactions.log"), "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception:
            pass

    def update_settings(self, **kwargs):
        for key in ("enabled", "greeting_enabled", "popup_enabled", "greeting_mode"):
            if key in kwargs:
                setattr(self, key, type(getattr(self, key))(kwargs[key]))
        if "model_name" in kwargs:
            self.model_name = kwargs["model_name"]

    def cleanup(self):
        with self.chat_lock:
            pass
        if self.popup_manager:
            self.popup_manager.close_all_windows()
        if self.greeting_mode == "once_per_day":
            self._save_daily_greetings()
        print("[Chat] Cleaned up")
