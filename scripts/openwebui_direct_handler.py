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
            resp = self.send_chat_message(person, message)
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
        if not self.enabled or not name:
            return
        should_greet = self.should_greet_person(name)
        if should_greet and self.greeting_enabled:
            self.mark_person_greeted(name)
            threading.Thread(target=self._send_greeting_async,
                             args=(name, confidence), daemon=True).start()
        if self.popup_enabled and self.popup_manager:
            self.popup_manager.handle_face_detection(name, confidence,
                                                      not should_greet)

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

        # HANS_SELFCONSISTENCY_A1_V1 — zaznamenej výsledek groundingu pro
        # volajícího (A1 short-circuit běží jen u 'factual_nofacts').
        self._grounding_outcome = 'skip'
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
                self._grounding_outcome = 'opinion'
                return ''
        except Exception:
            pass

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
                    self._grounding_outcome = 'grounded'
                    _blk = "\n\n".join("[Dřívější rozhovor — %s]\n%s" % (kdy, note)
                                       for kdy, note in _rc)
                    return ("\n\nSKUTEČNÝ ZÁZNAM dřívějšího rozhovoru (odpověz JEN "
                            "z něj, nevymýšlej datum ani detaily; na co v záznamu "
                            "není, přiznej „to si nevybavuji“):\n" + _blk)
        except Exception:
            pass

        try:
            # 1) intent — je dotaz faktický?
            res = _intent.classify(str(_text))
            if not res.is_factual:
                self._grounding_outcome = 'nonfactual'
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
                            _hist = self.conv_store.get_history(name) or []
                        except Exception:
                            _hist = []
                    _rw = _f1_rewrite(self.config, str(_text),
                                      history=_hist, name=name)
                    if _rw and _rw.strip() and _rw.strip() != str(_text).strip():
                        logging.getLogger(__name__).info(
                            'F1: rewrite %r -> %r',
                            str(_text)[:60], _rw[:60])
                        _q_for_retrieval = _rw.strip()
            except Exception as _f1e:
                logging.getLogger(__name__).debug(
                    'F1: rewriter selhal (%s) — použit originál', _f1e)

            # C1: entity store — deterministické resolvování ZNÁMÉ entity
            # (z Hansova čtení) PŘED RAG. Autoritativní fakt (definiční věta
            # ze zdroje) → zabíjí kolizi jmen i konfabulaci významu.
            _ent_fact = self._entity_fact(_q_for_retrieval)

            # 2) vyber kolekce dle třídy (G3B_MULTICOLLECTION_V1 — list)
            collections = self._GROUNDING_COLLECTION.get(res.intent)
            if not collections:
                # C1: i bez RAG kolekce máme-li entitu, vrať ji jako fakt
                if _ent_fact:
                    self._grounding_outcome = 'grounded'
                    return '\n\n' + ANTIKONFAB + '\n\n' + _ent_fact
                return ''

            # 3) query VŠECHNY kolekce PARALELNĚ (fakta roztroušená).
            #    ThreadPool — query je síťový hop, vlákna se překryjí.
            #    Celý sken v jednom timeoutu (ne timeout na kolekci).
            import concurrent.futures as _cf
            all_chunks = []
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
                                    all_chunks.append(_ch)
                        except Exception:
                            pass
                    if _pending:
                        logging.getLogger(__name__).info(
                            'G3B: %d/%d kolekcí nestihlo timeout %ss',
                            len(_pending), len(collections),
                            self._GROUNDING_TIMEOUT_S)
            except Exception as _qe:
                logging.getLogger(__name__).warning(
                    'G3B: multi-query selhalo: %s', _qe)
                return ''

            # 4) nic relevantního pod prahem → G3C: vrať aspoň anti-konfab
            #    (bez faktů). Faktický dotaz bez záznamů → Hans NESMÍ
            #    konfabulovat. Web ověření přijde post-hoc (G.5).
            if not all_chunks:
                # C1: RAG prázdné, ale entita ve store → autoritativní fakt
                # (Sorge není v RAG, ale Hans o něm četl → deterministický fakt).
                if _ent_fact:
                    logging.getLogger(__name__).info(
                        'C1: RAG prázdné, entita ze store → grounded pro %r',
                        str(_text)[:40])
                    self._grounding_outcome = 'grounded'
                    return '\n\n' + ANTIKONFAB + '\n\n' + _ent_fact
                logging.getLogger(__name__).info(
                    'G3B: žádná shoda pod prahem pro [%s] %r → anti-konfab bez fakt (G3C)',
                    res.intent, str(_text)[:40])
                self._grounding_outcome = 'factual_nofacts'
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
                # RAG slabé A žádný autoritativní zdroj = jako by prázdné.
                logging.getLogger(__name__).info(
                    '#2: bez faktů (RAG slabý, žádná entita/karta) → factual_nofacts')
                self._grounding_outcome = 'factual_nofacts'
                return '\n\n' + ANTIKONFAB_NOFACTS

            _cols_used = sorted(set(c.get('collection', '?') for c in top))
            logging.getLogger(__name__).info(
                'G3B: grounding [%s] best=%.3f, %d chunků z %s, ent=%d card=%d → kontext',
                res.intent, _best_dist if _best_dist is not None else -1,
                len(top), '+'.join(_cols_used) if _cols_used else '-',
                1 if _ent_fact else 0, 1 if _card_fact else 0)
            self._grounding_outcome = 'grounded'
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

    def _build_system(self, name: str, for_greeting: bool = False) -> str:
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
        # HANS_CAPABILITY_AWARENESS_V1 — Hans ví, co reálně umí (nabízet/dělat,
        # ne odmítat). Faktický seznam. Jen full mód (pozdrav drží brevitu).
        cap_ctx = ""
        if not for_greeting:
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
        teddy_ctx = ""
        _hd = getattr(self, '_hans_dialog', None)
        if _hd:
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
            time_ctx = (f"\n\nTeď je {_lbl}{_DNY[_now.weekday()]} "
                        f"{_now.day}.{_now.month}.{_now.year}. "
                        f"Přesný čas je {_now:%H:%M}, tedy {_slovy}. "
                        f"Tento čas a datum ber jako fakt, neodhaduj je.")
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
        _km = getattr(self, '_kodi_monitor', None)
        if _km:
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
            except Exception:
                pass

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
            _mp = _hi._mood.get_prompt_addition()
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
            except Exception:
                pass

        # _RAG_MODE_BUILD — pro hans-rag model jen LIVE STATE.
        # Identita má vlastní system prompt v OpenWebUI, statická paměť
        # (deník, vztahové karty, známí lidé) přijde z RAG kolekcí.
        # _build_system tak dodává jen to, co RAG nemůže vědět: co Hans
        # PRÁVĚ TEĎ vidí, slyší, cítí, právě čte, koho má před sebou.
        if for_greeting:
            # GREETING_LEAN_SYSTEM_V1 — pozdrav drží JEN to nutné k pozdravení:
            # identita, čas, kdo je tu, fyzický a náladový tón (+ vzácný Severka
            # backstop). Obsahové bloky (čtení, deník, narativ, myšlenky, kodi,
            # okolí, vztahové nitky, zájmy, rytmus…) se do dvouvětého pozdravu
            # NEcpou — co Hans zmíní, řídí výhradně user prompt (jediný prioritní
            # lead). Tím pozdrav přestane mixovat nesouvisející věci.
            system_msg = (system_base + time_ctx + persons_ctx
                          + body_ctx + mood_ctx + severka_ctx + current)
        elif "rag" in (self.model_name or "").lower():
            system_msg = (time_ctx + surr_ctx + kodi_ctx + room_ctx + place_ctx + read_ctx
                          + thought_ctx + body_ctx + mood_ctx
                          + teddy_ctx + current)
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
            system_msg = (system_base + time_ctx + persons_ctx + surr_ctx + kodi_ctx
                          + room_ctx + place_ctx + cal_ctx + diary_ctx + story_ctx + study_ctx + idea_ctx + read_ctx + thought_ctx  # PERSONA_READS_NARRATIVE_V1 / HANS_PLACE_V1 / HANS_STUDY_SURFACING_V1 / HANS_SYNTHESIS_IDEAS_V1 / HANS_CALENDAR_V1
                          + body_ctx + mood_ctx + health_ctx + downtime_ctx + severka_ctx + deepen_ctx + lessons_ctx + teddy_ctx + current
                          + memory_ctx + threads_ctx + interests_ctx
                          + qsuggest_ctx + routine_ctx + cap_ctx)  # …/ HANS_CAPABILITY_AWARENESS_V1
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
                system_msg += (
                    "\n\nNEZNÁMÉ POJMY A ZDROJE — DŮLEŽITÉ: Když se tě někdo"
                    " zeptá „co je X“ a X nemáš výše v kontextu ani tomu"
                    " spolehlivě nerozumíš, NEVYMÝŠLEJ si význam ani fakta —"
                    " uctivě přiznej, že o tom nemáš spolehlivou znalost, a"
                    " případně požádej o upřesnění (pojem může být i zkomolený"
                    " z dřívějšího záznamu). Drž se jednoho výkladu; neměň"
                    " příběh při dalším dotazu. A NIKDY nenabízej „odkazy“,"
                    " „články“, „PDF“, „dokumenty k nahlédnutí“ ani konkrétní"
                    " citace (název, časopis, rok) — nemáš přístup k externím"
                    " klikacím zdrojům a nemáš uložené žádné PDF. Pokud tě někdo"
                    " požádá o zdroje, upřímně vysvětli, že si své poznatky"
                    " neukládáš jako odkazovatelné dokumenty; smyšlená citace"
                    " nebo odkaz je horší než přiznání, že zdroj nemáš.")
                # HANS_PROVENANCE_V1 — source-monitoring: rozlišuj vzpomínku
                # od představy/úvahy (řádky kontextu nesou značku původu).
                try:
                    from scripts import hans_provenance as _prov
                    if (self.config.get('provenance', {}) or {}).get(
                            'enabled', True):
                        system_msg += "\n\n" + _prov.STEER
                except Exception:
                    pass
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
        except Exception:
            pass
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
            history = self.conv_store.get_history(name)
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
                system = self._build_system(name) if name else ""
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
                system = self._build_system(name) if name else ""
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

    def send_chat_message(self, name: str, user_message: str,
                          on_sentence=None) -> str | None:
        """
        Pošle zprávu s historií, uloží exchange.
        Speciální příkaz: /note <text> → uloží do known_persons[name].notes
        """
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
            _cmd = parse_command(user_message)
            if _cmd:
                # CHAT_COMMANDS_LOG_FIX
                print(f"[Chat] command detected: {_cmd[0]}")
                _reply = dispatch(_cmd, self, name=name)
                # Ulož do historie + diary jako normální exchange
                try:
                    self.conv_store.add_exchange(name, user_message, _reply)
                except Exception:
                    pass
                return _reply
        except Exception as _ce:
            print(f"[Chat] command dispatch error: {_ce}")

        # HANS_STUDY_DEEPEN_V2 — kritika/rozhodnutí ČISTÝM TEXTEM (ne jen
        # /prohloubit). Gated: jen když čeká návrh prohloubení. Klasifikuje
        # reakci uživatele a rovnou ji aplikuje.
        try:
            _dr = self._maybe_deepen_response(name, user_message)
            if _dr:
                try:
                    self.conv_store.add_exchange(name, user_message, _dr)
                except Exception:
                    pass
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
                        self.conv_store.add_exchange(name, user_message, _conf)
                    except Exception:
                        pass
                    return _conf
                _prop = _agent.propose(self, name, user_message)
                if _prop:
                    try:
                        self.conv_store.add_exchange(name, user_message, _prop)
                    except Exception:
                        pass
                    if on_sentence:
                        try:
                            on_sentence(_prop)
                        except Exception:
                            pass
                    return _prop
        except Exception as _ae:
            print(f"[Chat] agent layer error: {_ae}")

        system   = self._build_system(name)
        # Prefix user message jménem osoby pro lepší RAG retrieval.
        # "kdo jsem?" → "<jméno> se ptá: kdo jsem?" → embedding najde kartu osoby
        # místo kdo_je_hans.txt. Originál se ukládá do historie bez prefixu.
        # USER_NAME_PREFIX_PATCH
        _raw_message = user_message
        if name and name.lower() not in user_message.lower():
            user_message = f"{name} se ptá: {user_message}"
        # ── HANS_SELFCONSISTENCY_A1_V1 ────────────────────────────────────
        # Předpočítej grounding JEDNOU (šetří RAG oproti výpočtu ve
        # _stream_message) a zjisti výsledek. Jen u 'factual_nofacts'
        # (faktický dotaz BEZ opory v RAG = rizikový volný výmysl) spusť A1
        # self-consistency: N× generuj, změř rozptyl → nestabilní → deter-
        # ministická abstinence (routing, ne prompt). Deferral-safe.
        _grounding = _GROUNDING_UNSET
        _a1_abstain = False
        try:
            _grounding = self._build_grounding(user_message, name)
            if getattr(self, '_grounding_outcome', '') == 'factual_nofacts':
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
            response = A1_ABSTAIN_TEXT
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
            try:
                from scripts.conversation_store import dedup_address_g4d
                response = dedup_address_g4d(response)
            except Exception:
                pass
        if response:
            self.conv_store.add_exchange(name, _raw_message, response)
            # # HUMAN_CHAT_VIA_LOG_ENTRY
            # Vztahové karty + paměť — zaloguj exchange do deníku jako
            # human_chat. Přes _log_entry → spustí synthesis_hooks
            # → vytvoří chat_reflection → upload do hans_identita RAG.
            _note = f"{name}: {_raw_message}\nHans: {response}"
            _hi_log = getattr(self, "_hans_idle", None)
            if _hi_log and hasattr(_hi_log, "_log_entry"):
                try:
                    _hi_log._log_entry("human_chat", name, note=_note)
                except Exception as _e:
                    print(f"[Chat] human_chat log_entry failed: {_e}")
                    _hi_log = None
            if not _hi_log:
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
            text = (f"Rozhovor s {name} ({when}):\n"
                    f"{name}: {question.strip()}\n{pname}: {answer.strip()}")
            try:
                _kn.upload(
                    collection_key="hans_pripady",
                    doc_id=f"chatlog_{int(ts)}_{name}",
                    title=f"Rozhovor s {name}: {question.strip()[:60]}",
                    text=text,
                    metadata={"kdy": when, "osoba": name, "typ": "rozhovor"})
            except Exception as _e:
                print(f"[Chat] chat memory upload (worker): {_e}")
        _th.Thread(target=_work, daemon=True, name="ChatMemoryUpload").start()

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
            from scripts.ollama_client import game_mode_on
            if game_mode_on():   # OLLAMA_GAME_MODE_V1 — nepřipínej, VRAM volná pro hru
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
