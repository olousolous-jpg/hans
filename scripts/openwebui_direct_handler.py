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
    # PERSONA_REFACTOR_11 — role ven, pokyn je stylový (mluv přirozeně)
    "jako někdo, kdo to prostě ví — NEŘÍKEJ \"záznamy uvádějí\", "
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

# HANS_GREETING_OUTPUT_TRIM_V2 (19. 9.) — vzor pozdravu SDILENY.
# Dosud zil jako lokalni promenna uvnitr `_collapse_repeated_greetings`,
# takze ho vystupni orez nemohl pouzit a hrozila treti kopie vzoru.
# Pouziva se na DVA ucely: (a) poznat filler-odstavec v degenerovane
# odpovedi, (b) poznat, ze POZDRAVIL UZIVATEL — pak je pozdrav
# v odpovedi legitimni a neorezava se.
_POZDRAV_UZIVATEL_RE = re.compile(
    r"^(dobr[ýé]\s+(ve[čc]er|den|r[áa]no)|ahoj|zdrav[ií]m|t[ěe]š[ií])",
    re.I)

# HANS_SELFCONSISTENCY_A1_V1 — deterministická abstinence u nestabilního
# faktického dotazu (short-circuit místo volné generace persony).
A1_ABSTAIN_TEXT = (
    "K tomuhle nemám spolehlivý záznam a nerad bych si domýšlel, pane. "
    "Raději přiznám, že si tím nejsem jistý, než abych řekl něco vymyšleného."
)


# TIME_AWARENESS_WORDS_V1 — český slovní čas; od 26. 9. sdílený v cz_numbers
# (HANS_TIME_WORDS_SHARED_V1 — potřebuje ho i recall filmu).
from scripts.cz_numbers import cz_clock_words as _cz_clock_words  # noqa: E402


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


# ── HANS_EVIDENCE_V1 (7.9.) — CO MODEL DOSTAL JAKO FAKTA ───────────────────
# Brzdy (grounding_guard, A1) dosud soudily odpověď proti `grounding` + 6
# zprávám historie, ale model dostal 19 bloků o ~16 000 znacích. Věta
# podložená blokem `kodi` nebo `room` proto vypadala jako výmysl — doloženo
# 7. 9.: guard zahodil pravdivé „Poslední dobou jsem sledoval *For Your Eyes
# Only*" (kodi_playing týž den v 09:54).
#
# ⚠️ ROZDĚLENÍ NENÍ ODHAD — navazuje na HANS_OWN_WORK_NOT_FACT_V1 (22.8.),
# kde je na 444 reálných faktických dotazech změřeno, že Hansova VLASTNÍ
# TVORBA (eseje, postřehy, kapitoly životního příběhu) se v retrievalu
# prosazuje, ale doklad o světě NENÍ. Táž hranice platí tady:
#   • EVIDENCE = co Hans naměřil, viděl nebo si zapsal (smysly, deník, paměť)
#   • NE-evidence = kdo Hans JE a co si MYSLÍ (persona, autobiografie,
#     syntetické nápady, nálada, ponaučení, vlastní směr)
# Kdyby se do evidence pustil `story` (3 140 zn autobiografie) nebo `idea`,
# stala by se z Hansovy vlastní prózy „opora" pro tvrzení o světě — přesně
# ta třída, kvůli které guard existuje.
_EVIDENCNI_BLOKY = (
    "time", "persons", "surr", "kodi", "room", "place", "cal",
    "diary", "read", "study", "health", "teddy", "memory",
    "threads", "interests", "cap", "current",
)


def evidence_text(hodnoty: dict) -> str:
    """Text, který model dostal JAKO FAKTA (bez persony a vlastní tvorby)."""
    return "\n".join(str(hodnoty.get(n) or "") for n in _EVIDENCNI_BLOKY).strip()


def slozit_prompt(hodnoty: dict, varianta: str) -> str:
    """HANS_PROMPT_BLOCKS_TABLE_V1 — složí prompt v pořadí `_PROMPT_BLOKY`.
    Bloky, které do varianty nepatří nebo jsou prázdné, se přeskočí."""
    return "".join(hodnoty.get(n) or "" for n, kde in _PROMPT_BLOKY
                   if varianta in kde)


_A1_VYPLN = {"a", "ale", "tak", "no", "ok", "hmm", "hm", "dobre", "jo", "aha", "takze",
             "hele", "fajn", "jasne", "pak", "i"}
_A1_TAZACI = {"kdo", "co", "kdy", "kde", "proc", "jak", "kolik", "ktery", "ktera", "ktere",
              "kterou", "jaky", "jaka", "jake", "jakou", "odkud", "kam", "cim", "koho",
              "komu", "ci"}


def _a1_otazka_bez_otazniku(veta: str) -> bool:
    """HANS_A1_QUESTION_NO_MARK_V1 — tázací slovo na začátku věty po výplňových
    slovech („a kdo ho postavil“, „tak proc je to v nejistote“)."""
    import unicodedata as _ud
    t = "".join(c for c in _ud.normalize("NFD", (veta or "").lower())
                if _ud.category(c) != "Mn")
    w = [x.strip(".,;:!") for x in t.split()]
    while w and w[0] in _A1_VYPLN:
        w = w[1:]
    return bool(w) and w[0] in _A1_TAZACI


# ── HANS_NO_FALSE_MEMORY_CLAIM_V1 (7. 10.) — vzory, viz `_sc_pojistky` ──────
_FALESNA_PAMET_A = re.compile(
    r"\b([Vv]) pam[ěe]ti m[áa]m (?:\w+ ){0,2}?z[áa]znamy?\b")
_FALESNA_PAMET_B = re.compile(
    r"\bm[áa]m (?:pom[ěe]rn[ěe] |dost |velmi )?(?:obs[áa]hl[ée] |podrobn[ée] |rozs[áa]hl[ée] )"
    r"z[áa]znamy\b")
_VLASTNI_DILO_TVRZENI = re.compile(
    r"\b(?:vytvo[řr]il|namaloval|nakreslil|napsal|zhotovil)\s+jsem\s+(?:si\s+)?[^.!?\n]{0,70}?"
    r"\b(?:obraz\w*|malb\w+|kresb\w+|skic\w+|esej\w*|ilustrac\w+|portr[ée]t\w*)", re.I)
_NABIDKA_POSLAT = re.compile(r"\b(?:poslat|po[šs]lu|uk[áa]zat|uk[áa][žz]u)\b", re.I)


def _falesna_pamet_nahrada(m):
    return ("Z" if m.group(1) == "V" else "z") + " obecných znalostí vím"


# ── HANS_OWN_WORK_NOT_FACT_V1 (22.8.) — VLASTNÍ TVORBA NENÍ DOKLAD O SVĚTĚ ──
# Kolekce `hans_identita` je ve faktické cestě ZÁMĚRNĚ: vztahové karty jsou
# zdroj pravdy o lidech (`G5A_IDENTITY_GROUNDING_V1`). Míchá ale karty
# s Hansovou autobiografií a tvorbou — a ta se v retrievalu prosazuje.
#
# ZMĚŘENO 22.8. na 444 skutečných faktických dotazech z deníku (produkční
# parametry, 4 kolekce, k=3, max_distance 0.75, strict 0.70):
#   • grounding vznikne u 87 dotazů, u 84 z nich je nejlepší shoda z identity
#     (67 dokumentů) — ne z deníku (1 961), případů (6 108) ani četby (5 658),
#   • nejčastější typ opory: dokončené dílo 22×, vlastní postřeh 18×,
#     kapitola životního příběhu 7×, úvaha o tvorbě 2×.
# Prošel jsem všech 25 dotazů, které tímhle filtrem o oporu přijdou, a ani
# jeden o ni přijít nemá:
#   „jaké je venku počasí?"         ← esej o japonské zahradě
#   „vis kdo to byl Arnold Rimmer?" ← postřeh Mauna Loa × Subdukce × Titanic
#   „jake je vlastne dnes datum?"   ← postřeh Rudé gardy × Nenapravitelní
# Vztahové karty („# <jméno> / ## Rodina / ## Údaje") typ nenesou a procházejí
# dál — kolekce tedy zůstává, mizí jen eseje a postřehy vydávané za doklad.
#
# Dlouhý vyprávěcí text leží v embeddingu blízko čemukoli; u bge-m3 se
# relevantní pásmo (0.64-0.69) se šumem PŘEKRÝVÁ (varování v hans_knowledge).
# Práh to tedy neuhlídá a rozhodnout musí DRUH dokumentu.
_NEFAKT_TYP_RE = __import__("re").compile(
    r"typ:\s*(narrative_chapter|d[íi]lo-dokon[čc]eno|creation_reflection|"
    r"n[áa]pad)\b", __import__("re").IGNORECASE)


def je_vlastni_tvorba(text: str) -> bool:
    """Je tenhle RAG chunk Hansovo dílo/úvaha, ne doklad o světě?

    Rozhoduje hlavička dokumentu (`typ:` v prvních ~300 znacích), ne obsah —
    typ zapisuje `hans_synthesis` při uploadu, takže je to tvrdý údaj.
    """
    return bool(_NEFAKT_TYP_RE.search(str(text or "")[:300]))


# HANS_HANDLER_SPLIT_V1 — značka „skupina doběhla bez return, pokračuj“
_POKRACUJ = object()


from scripts.handler_system import SystemMixin   # ROZDELENI_METOD_V1 — část třídy OpenWebUIDirectHandler
from scripts.handler_grounding import GroundingMixin   # ROZDELENI_METOD_V1 — část třídy OpenWebUIDirectHandler
from scripts.handler_fakta import FaktaMixin   # ROZDELENI_METOD_V1 — část třídy OpenWebUIDirectHandler
from scripts.handler_model import ModelMixin   # ROZDELENI_METOD_V1 — část třídy OpenWebUIDirectHandler
class OpenWebUIDirectHandler(SystemMixin, GroundingMixin, FaktaMixin, ModelMixin):

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
        # HANS_GREETING_OUTPUT_TRIM_V2 — vzor je ted modulovy (sdileny
        # s vystupnim orezem), aby nevznikla druha kopie.
        _greet = _POZDRAV_UZIVATEL_RE
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
        _t0_nab = time.time()   # HANS_OFFER_TO_PENDING_V1
        try:
            # HANS_CHAT_CHANNEL_AWARE_V1 — tag zprávy channelem 'web'
            # HANS_STOPA_V1 — stopa dotazu pro vizualizaci (web admin /mysleni);
            # jakákoli chyba stopy = dotaz proběhne jako bez ní
            if (self.config.get("stopa", {}) or {}).get("enabled", True):
                from scripts import hans_stopa as _hs
                resp = _hs.spust(self.send_chat_message, person, message,
                                 channel="web", rid=rid, kanal="web",
                                 osoba=person, zprava=message)
            else:
                resp = self.send_chat_message(person, message, channel="web")
        except Exception as e:
            resp = f"(chyba: {e})"
        # HANS_OFFER_TO_PENDING_V1 — nabídka akce → čekající návrh (ano = splní)
        try:
            from scripts.hans_offer import zpracuj as _nab
            resp, _ = _nab(self, person, message, resp or "", _t0_nab)
        except Exception:
            pass
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

    def _send_greeting_async(self, name: str, confidence: float, duvod=None):
        # HANS_GESTURE_WAVE_GREET_V1 — `duvod` rekne, PROC se zdravi (dnes
        # zamavani gestem). Bez nej by pozdrav na mavnuti znel stejne jako
        # pozdrav pri rozpoznani tvare a neslo by je od sebe odlisit.
        try:
            prompt = self._generate_greeting_prompt(name, duvod=duvod)
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
    # HANS_CHATLOG_NOT_FACT_V3 (24.8.) — V1 chytal JEN formát z
    # `_upload_chat_memory` (text ZAČÍNAJÍCÍ „Rozhovor s <jméno> (datum):").
    # Dokumenty skládané `hans_synthesis._build_rag_text` ale začínají titulkem
    # a datem, takže sekce „## Rozhovor s Koláčem" / „## Rozhovor s osobou"
    # ležela ZA kotvou `^` → Koláčovy dialogy i chaty procházely do faktického
    # groundingu. ZMĚŘENO na skutečném tvaru dokumentů (viz
    # `tests/test_chatlog_filter.py`). Právě touhle cestou se šířil Gutštejn:
    # dialogy s Koláčem 7.8. a 21.8. ho zopakovaly a nahrály do `hans_pripady`.
    # ⚠️ Platí JEN pro faktickou cestu (`_build_grounding`);
    # `conversation_recall` chatové kusy používá dál — tam PATŘÍ (rozhodnutí
    # u V1). web_read / kodi_playing / book_read / case_* procházejí beze změny.
    _CHATLOG_RE = __import__("re").compile(
        r"^\s*#*\s*Rozhovor\s+s\s|NEOVĚŘENO — vlastní výrok"
        r"|##\s*Rozhovor\s+s\s",
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

    # HANS_KODI_CAST_FACT_V1 (21.8.) — dotaz na obsazení / tvůrce z knihovny.
    # Kodi drží `cast` u filmů (40 ze 40 vzorku) i u dílů seriálů — jen se na
    # to nikdy nikdo neptal, takže si model herce vymýšlel.
    _CAST_PAT = re.compile(
        r"(kdo\s+(tam|v\s+tom|v\s+n[ěe]m|v\s+n[íi])?\s*(hraj|hr[áa]l|ú[čc]ink|"
        r"uc[íi]nk)|kdo\s+si\s+(tam\s+)?zahr[áa]l|obsazen[íi]|"
        r"kdo\s+to\s+(re[žz]|nato[čc])|kdo\s+hraje)", re.IGNORECASE)

    _KNIHA_DOPORUC_PAT = re.compile(
        r"(doporu[c\u010d]\w*)[^.?!]{0,40}?(?:k\s*(?:p[\u0159r]e)?[c\u010d]ten[i\u00ed]|"
        r"[c\u010d][i\u00ed]st|knih|[c\u010d]etb|na\s+[c\u010d]ten[i\u00ed]|ke\s+[c\u010d]ten[i\u00ed])"
        r"|(?:co|n[e\u011b]co)\s+(?:by\w{0,3}\s+)?(?:si\s+)?(?:\w+\s+){0,2}(?:p[\u0159r]e)?[c\u010d][i\u00ed]st"
        r"|(?:m[a\u00e1][s\u0161]|m[a\u00e1]te)\s+\w{0,10}\s*tip\s+na\s+knih",
        re.IGNORECASE)

    # HANS_BOOK_RECOMMEND_FOLLOWUP_V1 (14. 9.) — kratky NAVAZUJICI dotaz po
    # doporuceni cetby („a neco jineho?“, „a proc zrovna tohle?“)
    # vzor doporuceni nesedne, takze Hans odpovidal z hlavy a vymyslel knihu.
    # Dolozeno 19. 8. („a mas neco ceskeho?“ -> kniha, kterou necetl) a 13. 9.
    # Sam o sobe je vzor siroky (sedne na 41 z 1 490 vet), proto plati JEN do
    # 10 min po doporuceni TEZ osobe (okno jako hans_thread._THREAD_TTL_S).
    # Zmereno na cele historii: 5 spravnych sepnuti, 0 falesnych; jina
    # kategorie („a co film?“) se vylucuje.
    _KNIHA_NAVAZ_PAT = re.compile(
        r"^\s*(?:a|a\s+co|tak)\s+(?:n[eě]co|n[eě]jak\w*|co|m[aá][sš]|m[aá]te|jin\w*|"
        r"dal[sš]\w*|je[sš]t[eě]|pro[cč]|kter[aáýy]?\w*)\b"
        r"|\b(?:jin[eé]ho|jinou|dal[sš][ií]|je[sš]t[eě]\s+n[eě]co|[cč]esk[eé]ho|"
        r"[cč]eskou|pro[cč]\s+(?:zrovna|pr[aá]v[eě]))\b", re.IGNORECASE)
    _KNIHA_JINA_PAT = re.compile(
        r"film|seri[aá]l|hudb|p[ií]s[eň]|p[ií]sn|obraz|maluj|hr[aá]t|hru\b|"
        r"recept|j[ií]dl", re.IGNORECASE)

    # HANS_BOOK_FOLLOWUP_DATIVE_V1 (15. 9.) — "A kdybych dal prednost necemu
    # ceskemu, co byste doporucil?" (9 slov, 3. pad). Obecny strop 8 slov ZUSTAVA:
    # zvednuti na 10 by pridalo 7 falesnych sepnuti na 837 vetach. Jen tvar
    # "ceskemu" ma vlastni strop 12 — v korpusu 0 shod.
    _KNIHA_CESKEMU_PAT = re.compile(r"[c\u010d]esk[e\u00e9]mu\b", re.IGNORECASE)

    # HANS_ARTWORK_CONTENT_GROUNDED_V1 (15. 9.) — dotaz na OBSAH obrazu.
    # Doloženo 15. 9.: "co bylo na tom obraze" → Hans popsal "dva muze u okna",
    # ktere na obraze nejsou. Kazde dilo ma v deniku `vision` (popis hotoveho
    # obrazu) i `prompt` — do chatu se ale nedostavaly vubec.
    # Korpus 837 realnych vet: 0 takovych dotazu, takze se nic stavajiciho nemeni.
    _OBRAZ_OBSAH_PAT = re.compile(
        r"na\s+(?:tom|t[\u00e9e]m|posledn[\u00edi]m|tv[\u00e9e]m|va[\u0161s]em|sv[\u00e9e]m|nov[\u00e9e]m)\s+obraz"
        r"|co\s+(?:je|bylo|zobrazuje|zachycuje)\b[^?.!]{0,25}\bobraz"
        r"|popi[\u0161s]\w*\b[^?.!]{0,15}\bobraz"
        r"|obraz\w*\b[^?.!]{0,15}\b(?:zobrazuje|zachycuje|je\s+vid[\u011be]t)",
        re.IGNORECASE)

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

    # HANS_ENTITY_STRIP_ASKER_V1 (5.9.) — na entitni cestu NESMI jit prefix
    # „<jmeno> se pta:". Je to TATAZ past, kterou u karet osob resil
    # HANS_PERSON_CARD_KC_FIX_V1 (19.8.) — jen se tehdy neopravila i tady.
    # Doloheno 5.9.: „zkouska se pta: kdy vzniklo Divadlo Jary Cimrmana?"
    # resolvovalo entitu „Zkouska" (id 80, clanek z 5.7., PRAZDNA glosa) ->
    # fact_block vrati „o tomto pojmu mam zaznam, ale bez blizsi definice" ->
    # vysledek 'grounded' -> abstinencni brzda se NESPUSTI. Zkouseni Kolacem
    # jede prave pod jmenem `zkouska`, takze si tim travilo vlastni mereni.
    # ⚠️ `_q_for_retrieval` si prefix drzi ZAMERNE (embedding jim najde kartu
    # osoby u „kdo jsem?") — proto se strhava az tady, ne u zdroje.
    _ASKER_PFX = re.compile(r"^\s*\S+\s+se\s+pt[áa]:\s*")

    _VIDIS_ME_PAT = re.compile(   # HANS_SELF_STATE_ASKER_VISIBLE_V1
        r"vid[\u00edi](?:\u0161|s|te)\s+m[\u011be]|kame[r\u0159]|z[\u00e1a]b[\u011be]r|"
        r"pozoruje(?:\u0161|s|te)\s+m", re.IGNORECASE)


    # HANS_F1_NOT_ABOUT_ASKER_V1 — tvary 2. osoby (otazka porad miri na Hanse).
    _F1_2OS = re.compile(
        r"(?<![a-z])(jsi|jste|sis|sves|tvuj|tvoje|tve|tvych|tvym|tvemu|"
        r"vas|vase|vasi|vasem|vasich)(?![a-z])")
    _F1_2OS_SLOVESA = re.compile(
        r"(?<![a-z])\w{2,}(?:ujes|ujete|es|is|as|ys|ite|ate|ete)(?![a-z])")
    # HANS_F1_NOT_ABOUT_ASKER_V2 (6. 10.) — slova, která na ten vzor sednou
    # a slovesem ve 2. osobě nejsou („dnes“, „čas“): kvůli nim pojistka pustila
    # „Kolik času <Jméno> musí věnovat…“. A jméno na samohlásku se hledá podle
    # kmene (5. pád a další pády kmen mění). Z 327 puštěných přepisů v logu
    # nově zachytí 13, všechny s přehozeným podmětem.
    _F1_NE_SLOVESA = frozenset((
        "dnes", "cas", "vcas", "zas", "hlas", "les", "pes", "ples", "napis",
        "zapis", "popis", "rozpis", "spis", "kdys", "pas", "tenis", "servis",
        "adres", "proces", "kongres", "stres", "kompromis", "rukopis",
        "casopis", "predpis", "dopis", "zivotopis"))

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
    # HANS_SELF_RUNTIME_NARROW_V1 (20. 9.) — VZOR BYL SLEPÝ K VÝZNAMU SLOV.
    # Změřeno na 1 556 skutečných zprávách z deníku: z 31 shod bylo
    # 12 FALEŠNÝCH, a nejčastější viník nebyl „vidíš“, ale „spíš“ — české
    # příslovce je totéž slovo jako sloveso, takže věta o knize („píše to
    # spíš s posměchem“) vyrobila tvrdý blok o režimu spánku. Dál sem padal
    # film „patema vzhůru nohama“ a citoslovce „no vidíš“.
    # Doložený případ 19. 9.: na dotaz o OKOLÍ („co vidíš, co se děje
    # v místnosti?“) se přilepil fakt o vlastním režimu, grounding tím
    # přestal být prázdný, cesta se označila jako `grounded` a abstinence
    # se nespustila → Hans vymyslel ulici, galerii i malíře a připsal to
    # „místním zpravodajským zdrojům“.
    # Ověřeno PRODUKČNÍ cestou, ne jen regexem: `_build_grounding` nad touž
    # větou přešel z `grounded ← entita_c1` na `factual_nofacts`, tedy do
    # větve, kde běží brzda A1 (a `self_topic` ji tam nepřeskočí).
    # Bilance: −12 falešných · 0 ztracených legitimních (19 → 19) ·
    # +5 nově chycených, mj. vykání „vidíte mě?“, které starý vzor míjel.
    # ⚠️ Vidění a kamera se NEOPISUJÍ — sdílí se `_VIDIS_ME_PAT`
    # (HANS_SELF_STATE_ASKER_VISIBLE_V1), ať nevzniknou dvě pravdy o tomtéž.
    _SELF_RUNTIME_PAT = re.compile(
        r"(re[žz]im\w*|sp[áa]nk|span[ke]|hl[íi]d[áa]n|bd[íi][sš]|"
        r"hl[íi]d[áa][sš]|js[ie][sš]\s+vzh[uů]ru|vzh[uů]ru\s*\?)",
        re.IGNORECASE)
    # Holé „spíš“ je sloveso jen v KRÁTKÉ otázce („spíš?“, „už spíš?“).
    # Hranice je změřená, ne odhadnutá: všech šest výskytů v korpusu, které
    # jsou příslovce, stojí v dlouhém souvětí.
    _SPIS_PAT = re.compile(r"sp[íi][sš]\b", re.IGNORECASE)

    @staticmethod
    def _fold(s: str) -> str:
        import unicodedata
        return "".join(c for c in unicodedata.normalize("NFKD", (s or "").lower())
                       if not unicodedata.combining(c))

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


    def _generate_greeting_prompt(self, name: str, duvod=None) -> tuple:
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
        if duvod:  # HANS_GESTURE_WAVE_GREET_V1
            system += (" Zdravíš proto, že ti %s. Odpověz JEN krátkým"
                       " pozdravem, jedinou větou." % duvod)
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

        # 0) HANS_GREET_REASON_LEAD_V1 (6.9.) — POZDRAV NA VYZADANI (dnes
        # zamavani gestem) vede pred vsemi ostatnimi duvody.
        # Zmereno 6.9.: `duvod` pridany jen do `system` NEZABRAL — vetev (4)
        # nize rika v `user` doslova "zmin, cemu ses venoval behem jejich
        # nepritomnosti", a model poslechne tu konkretnejsi a blizsi
        # instrukci. Vsech 8 pozdravu na mavnuti zacinalo "Behem Vasi
        # nepritomnosti...". Prompt debt: veta v promptu prohrava se
        # soupericí vetou, proto to musi byt VETEV, ne dodatek.
        if duvod:
            # Pokyn uzivatele 6.9.: na mavnuti JEN KRATKY POZDRAV. Delsi
            # uvitani (cemu se venoval, nitky, pocasi) zustava u rozpoznani
            # tvare — tam ma smysl, protoze clovek prave prisel.
            user = (
                f"Pozdrav {name} JEDNOU krátkou větou — právě {duvod}. "
                f"Je {tod}. Nic víc nepřidávej: žádnou zmínku o jeho "
                f"nepřítomnosti, o tom čemu ses věnoval, ani o počasí."
            )
            _lead = True

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
            except Exception as _tiche:
                from scripts.logger import tichy_zapis as _tz  # HANS_SILENT_WRITE_LOG_V1
                _tz('openwebui_direct_handler:_generate_greeting_prompt', _tiche)

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
        if _wants_weather and not duvod:   # HANS_GREET_REASON_LEAD_V1
            _wx = getattr(self, "_weather", None)
            _tomorrow = ((_wx.get_tomorrow_string() if _wx else "") or "").strip()
            if _tomorrow:
                user += (f" Na závěr nahlas PŘESNĚ tuto předpověď na zítřek, "
                         f"slovo od slova; neuváděj jiná čísla ani neodhaduj: "
                         f"„{_tomorrow}\"")

        return system, user

    # ── OpenWebUI API ─────────────────────────────────────────────────────────

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
            # HANS_LLM_TRACE_V1 (9. 9.) — přímý kanál na OpenWebUI mimo
            # `ollama_client`, hrdlo v `_post_with_retry` ho nevidí. Tohle je
            # HLAVNÍ spotřebitel hans-czech; bez něj by měření nočního souběhu
            # mělo díru přesně tam, kde se model pinuje do VRAM.
            _t0 = time.time()
            r = requests.post(
                self.chat_endpoint,
                headers=self._headers(),
                json={"model": self.model_name, "messages": msgs, "stream": False},
                timeout=self.timeout,
            )
            self._trace(_t0, "send_message", r.status_code)
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

            _t0 = time.time()          # HANS_LLM_TRACE_V1
            r = requests.post(
                self.chat_endpoint,
                headers=self._headers(),
                json=payload,
                timeout=self.timeout,
                stream=True,
            )
            self._trace(_t0, "_stream_message", r.status_code)
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

    def _skip_memory(self, name: str) -> bool:
        """HANS_VOICE_NO_MEMORY_V1 (28. 9.) — výměna nejde do deníku, reflexe
        ani RAG: testovací identita, NEBO hlas, dokud je přepis nespolehlivý.
        Změřeno 28. 9.: z 9 hlasových vět v historii 8 nesmysl z přepisu
        (Whisper base) — „Půst film prelátor“ → chat_reflection „zájem o film
        Prelátor“. Rozhovor v conv_store (vlákno) zůstává. Přepnout
        `voice.remember` na true po nasazení přesnějšího přepisu."""
        if self._is_test_person(name):
            return True
        try:
            if get_current_channel() == "voice":
                if not bool((self.config.get("voice", {}) or {}).get("remember", False)):
                    return True
                # HANS_STT_TURBO_V1 (29. 9.) — přepis ze zálohy (base) je
                # nespolehlivý (70 % chyb) → do paměti ne, jen do vlákna.
                from scripts.voice_listener import posledni_prepis_zalohou
                if posledni_prepis_zalohou():
                    return True
        except Exception:
            pass
        return False

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
        if self._skip_memory(name):                    # HANS_VOICE_NO_MEMORY_V1
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
        import types as _types_nt
        ctx = _types_nt.SimpleNamespace(name=name, user_message=user_message, on_sentence=on_sentence, channel=channel)
        # HANS_CHAT_CHANNEL_AWARE_V1 — thread-local pro dispatch/chat_commands.
        try:
            _channel_local.channel = ctx.channel
        except Exception:
            pass
        # ── /note příkaz ──────────────────────────────────────────────────
        ctx.stripped = ctx.user_message.strip()
        _r = self._sc_read(ctx)
        if _r is not _POKRACUJ:
            return _r
        _r = self._sc_note(ctx)
        if _r is not _POKRACUJ:
            return _r
        _r = self._sc_url(ctx)
        if _r is not _POKRACUJ:
            return _r
        self._sc_vypadek(ctx)
        _r = self._sc_prikazy(ctx)
        if _r is not _POKRACUJ:
            return _r
        _r = self._sc_malovani_brana(ctx)
        if _r is not _POKRACUJ:
            return _r
        _r = self._sc_stav_tazatele(ctx)
        if _r is not _POKRACUJ:
            return _r
        _r = self._sc_kniha(ctx)
        if _r is not _POKRACUJ:
            return _r
        _r = self._sc_znalosti(ctx)
        if _r is not _POKRACUJ:
            return _r
        _r = self._sc_pamatujes(ctx)
        if _r is not _POKRACUJ:
            return _r
        _r = self._sc_zdroje(ctx)
        if _r is not _POKRACUJ:
            return _r
        _r = self._sc_prohloubeni(ctx)
        if _r is not _POKRACUJ:
            return _r
        _r = self._sc_ceka_malba(ctx)
        if _r is not _POKRACUJ:
            return _r
        _r = self._sc_agent(ctx)
        if _r is not _POKRACUJ:
            return _r
        self._sc_podklad(ctx)
        self._sc_nazor(ctx)
        self._sc_a1(ctx)
        self._sc_pojistky(ctx)
        self._sc_overeni_tvrzeni(ctx)
        self._sc_ulozeni(ctx)
        return ctx.response

    def _sc_read(self, ctx):
        if ctx.stripped.lower().startswith("/read "):
            url = ctx.stripped[6:].strip()
            if url.startswith("http"):
                _hi = getattr(self, '_hans_idle', None)
                if _hi and hasattr(_hi, '_curiosity'):
                    _hi._curiosity.trigger_url(url, topic="manual")
                    from scripts.hans_persona import persona_name as _pn  # PERSONA_NAME_CONFIGURABLE_V1
                    return f"\u2713 {_pn(self.config)} si přečte: {url}"
            return "\u26a0 Zadej platnou URL začínající http"
        return _POKRACUJ

    def _sc_note(self, ctx):
        if ctx.stripped.lower().startswith("/note "):
            note_text = ctx.stripped[6:].strip()
            if note_text:
                self._save_note(ctx.name, note_text)
                return f"✓ Poznámka uložena: {note_text}"
            else:
                return "⚠ Použití: /note <text poznámky>"
        return _POKRACUJ

    def _sc_url(self, ctx):
        # ── HANS_READ_URL_NL_V1 — URL v běžné zprávě s intentem čtení ──────────
        # „zjisti víc o X, tu je odkaz https://…" → Hans stránku přečte, uloží do
        # čtenářské paměti (RAG) a zapamatuje si TÉMA (ne jen 'url'/URL). Bez
        # intentu (URL jen tak zmíněná) se nechytá → normální chat.
        if not ctx.stripped.startswith("/"):
            import re as _re_u
            _um = _re_u.search(r"https?://\S+", ctx.stripped)
            _intent = any(w in ctx.stripped.lower() for w in (
                "zjisti", "přečti", "precti", "přečte", "precte", "podívej",
                "podivej", "mrkni", "koukni", "stáhni", "stahni", "odkaz",
                "stránk", "stranka", "nastuduj", "prostuduj"))
            if _um and _intent:
                _url = _um.group(0).rstrip('.,);:!?\'"')
                _topic = self._extract_read_topic(ctx.stripped, _url)
                _hi = getattr(self, '_hans_idle', None)
                if _hi and hasattr(_hi, '_curiosity'):
                    _hi._curiosity.trigger_url(_url, topic=_topic)
                    from scripts.hans_persona import persona_name as _pn
                    lbl = ("téma „%s\"" % _topic) if _topic != "url" else "stránku"
                    return ("Přečtu si %s a zapamatuji si, co tam najdu, %s."
                            % (lbl, ctx.name or "pane"))
        return _POKRACUJ

    def _sc_vypadek(self, ctx):
        # ── HANS_DOWNTIME_V1 — uzavření smyčky výpadku ───────────────────
        # Hans se u příchozí osoby zmínil o výpadku a zeptal se, co se dělo
        # (downtime_ctx surfaced). První NE-příkazová odpověď osoby = vyprávění
        # → ulož jako downtime_account a označ answered (zmínka přestane).
        try:
            _hi = getattr(self, '_hans_idle', None)
            _dt = getattr(_hi, '_downtime', None) if _hi else None
            if (_dt and _dt.get('surfaced') and not _dt.get('answered')
                    and not ctx.stripped.startswith('/')):
                _dt['answered'] = True
                _hi._log_entry(
                    'downtime_account',
                    'Co se dělo, když jsem byl mimo (od %s)' % ctx.name,
                    data=str(_dt.get('gap_hours', '')),
                    note=ctx.user_message[:600])
        except Exception:
            pass

    def _sc_prikazy(self, ctx):
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
                _t_turns = _thr.recent_turns(self, ctx.name, ctx.channel)
                _t_res, _t_subj = _thr.resolve_reference(ctx.user_message, _t_turns)
                self._thread_ctx = (ctx.user_message, _t_res, _t_subj)
                if _t_subj:
                    print(f"[Chat] thread: odkaz rozřešen → {_t_subj}")
            except Exception as _te:
                self._thread_ctx = None
                print(f"[Chat] thread error: {_te}")
            _cmd = parse_command(ctx.user_message)
            # HANS_FILM_OPINION_ANAFORA_V1 (23. 9.) — anafora obliby po výpisu
            # filmů („a který se ti z nich líbil nejvíc?“) nenese slovo „film“;
            # o /film rozhodne předchozí replika ve vlákně.
            if not _cmd:
                try:
                    from scripts.chat_commands import thread_film_opinion
                    if thread_film_opinion(ctx.user_message, _t_turns):
                        _cmd = ("film", ctx.user_message)
                        logging.getLogger(__name__).info(
                            'HANS_FILM_OPINION_ANAFORA_V1: vlákno → /film')
                except Exception as _fae:
                    logging.getLogger(__name__).debug('anafora film: %s', _fae)
            # HANS_DEMAGOG_FOLLOWUP_V1 (3. 10.) — pokračování po výpisu z Demagogu
            # („a co ostatní výroky?“) nenese slovo Demagog; rozhodne vlákno.
            if not _cmd:
                try:
                    from scripts.chat_commands import thread_demagog
                    _dma = thread_demagog(ctx.user_message, _t_turns)
                    if _dma:
                        _cmd = ("demagog", _dma)
                        logging.getLogger(__name__).info(
                            'HANS_DEMAGOG_FOLLOWUP_V1: vlákno → /demagog (%.60s)', _dma)
                except Exception as _dme:
                    logging.getLogger(__name__).debug('demagog vlákno: %s', _dme)
            # HANS_ZPRAVY_ODKAZY_V1 (4. 10.) — „pošli odkaz…“ po výpisu zpráv;
            # předbíhá i /zdroje (ten by vypsal zdroje Hansovy četby, ne zpráv)
            if not _cmd or _cmd[0] == "zdroje":
                try:
                    from scripts.chat_commands import thread_zpravy
                    _zpo = thread_zpravy(ctx.user_message, _t_turns)
                    if _zpo:
                        _cmd = ("odkazy_zprav", _zpo)
                        logging.getLogger(__name__).info(
                            'HANS_ZPRAVY_ODKAZY_V1: vlákno → /odkazy_zprav')
                except Exception as _zpe:
                    logging.getLogger(__name__).debug('zpravy vlákno: %s', _zpe)
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
                _ppc = _apc.get(ctx.name) if _apc else None
                if _ppc is not None and (time.time() - _ppc.ts) <= 180:
                    _confirm_waits = True
            except Exception as _cpe2:
                logging.getLogger(__name__).debug('confirm precedence v2: %s', _cpe2)
            if not _cmd and _confirm_waits:
                logging.getLogger(__name__).info(
                    'HANS_CONFIRM_PRECEDENCE_V2: čeká potvrzení návrhu → '
                    'LLM routing přeskočen: %.40s', ctx.user_message)
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
                    _rt = ctx.user_message
                    try:
                        _tc = getattr(self, '_thread_ctx', None)
                        if _tc and _tc[0] == ctx.user_message and _tc[1]:
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
                    if is_knowledge_check_query(ctx.user_message):
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
                    if _SOURCE_Q.search(ctx.user_message or ""):
                        _hist_p = []
                        try:
                            _hist_p = self.conv_store.get_history(ctx.name) or []
                        except Exception:
                            pass
                        if find_claim(ctx.user_message, _hist_p):
                            print("[Chat] provenience → běžná cesta "
                                  "(HANS_PROVENANCE_NOT_LIST_V1)")
                            logging.getLogger(__name__).info(
                                'HANS_PROVENANCE_NOT_LIST_V1: /%s zrušen — '
                                'věta konfrontuje čerstvé tvrzení: %.50s',
                                _cmd[0], ctx.user_message)
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
                    if _thr.should_suppress(ctx.name, ctx.channel, _cmd[0],
                                            ctx.user_message):
                        print(f"[Chat] thread: '{_cmd[0]}' potlačen "
                              f"(korekce) → odpoví model")
                        _cmd = None
                except Exception:
                    pass
            # HANS_CLAIM_HOLD_V1 (23.8.) — uživatel tvrdí opak toho, co Hans
            # před chvílí přečetl z deníku („před chvílí jsi říkal 12:15,
            # která odpověď platí?") → model ustoupil a MOU nepravdu vydával
            # za údaj z vlastního deníku. Řešení bez nové vrstvy: poslat dotaz
            # na TÝŽ deterministický příkaz, který odpověď vyrobil poprvé.
            # Musí to být AŽ ZA `should_suppress` — ta by to sejmula jako
            # „uživatel opravil tutéž cestu, nepouštěj ji znovu", což je přesně
            # opačné rozhodnutí, než tady potřebujeme.
            _hold_claim = None
            if not _cmd:
                try:
                    from scripts.claim_hold import disputed_last_seen
                    _hold_claim = disputed_last_seen(
                        ctx.user_message, self.conv_store.get_history(ctx.name) or [])
                    if _hold_claim:
                        _cmd = ("videl", _hold_claim)   # jméno nese tvrzení
                        logging.getLogger(__name__).info(
                            "HANS_CLAIM_HOLD_V1: spor o čerstvé tvrzení "
                            "→ znovu z deníku (%.60s)", _hold_claim)
                except Exception as _che:
                    logging.getLogger(__name__).debug(
                        "HANS_CLAIM_HOLD_V1: %s", _che)
            _reply = None
            if _cmd:
                # CHAT_COMMANDS_LOG_FIX
                print(f"[Chat] command detected: {_cmd[0]}")
                _reply = dispatch(_cmd, self, name=ctx.name)
                # HANS_CMD_EMPTY_FALLTHROUGH_V1 (6. 10.) — obsluha, ktera nic
                # nenasla, vraci prazdno se zamerem „propadni do bezneho
                # hovoru“ (/vycet od 26. 8.). Tady se ale prazdno VRATILO
                # jako odpoved: clovek dostal prazdnou zpravu, v logu ani
                # radek a prazdna vymena sla do historie. Ted veta pokracuje
                # beznou cestou, jako by prikaz nesepnul.
                if not (_reply or "").strip():
                    logging.getLogger(__name__).info(
                        "HANS_CMD_EMPTY_FALLTHROUGH_V1: /%s vratil prazdno "
                        "→ bezna cesta (%.60s)", _cmd[0], ctx.user_message)
                    _cmd = None
                    _hold_claim = None
            if _cmd:
                if _hold_claim:
                    from scripts.claim_hold import hold
                    # Bez uvození by odpověď vypadala jako přeslechnutá
                    # otázka — z deníku přijde slovo od slova táž věta.
                    _reply = hold(_reply, ctx.user_message)
                try:
                    from scripts import hans_thread as _thr
                    _thr.note_outcome(ctx.name, ctx.channel, _cmd[0], ctx.user_message)
                except Exception:
                    pass
                # Ulož do historie + diary jako normální exchange
                try:
                    self.conv_store.add_exchange(ctx.name, ctx.user_message, _reply, channel=ctx.channel)
                except Exception:
                    pass
                self._log_human_chat_to_diary(ctx.name, ctx.user_message, _reply)
                return _reply
        except Exception as _ce:
            print(f"[Chat] command dispatch error: {_ce}")
        return _POKRACUJ

    def _sc_malovani_brana(self, ctx):
        # ── HANS_PAINT_GATE_AFTER_BYPASS_V1 (5.9.) — BRÁNA SE PŘESUNULA NÍŽ ──
        # Stála tady, hned za příkazy, s odůvodněním „příkazy jsou
        # deterministické, mozek nepotřebují". To odůvodnění platí — jenže
        # POD tímhle místem je dalších SEDM deterministických bypassů
        # (datum/čas, kdo se ptá, doporučení knihy, karta osoby, nárok na
        # paměť, dotaz na zdroj, prohloubení studia) a ty brána brala s sebou.
        # Doloženo testovacím rozhovorem 5. 9.: render 17:17→17:22 spolkl
        # ŠEST z deseti tahů, mezi nimi „zapamatuj si to" — což je čistá
        # šablona bez modelu.
        # Brána proto sedí až TĚSNĚ PŘED AGENTNÍ VRSTVOU, tedy před prvním
        # místem, které mozek opravdu potřebuje. Jediná věc mezi tím, která
        # ho potřebuje taky, je dohledání (`lookup_now`) — to má vlastní
        # hlídku o pár desítek řádků níž.

        # HANS_KNOWLEDGE_CHECK_V1 BYPASS (18.7. → 19.7.) — hans-czech persona
        # halucinuje „mám v paměti záznamy" i pro věci, které nikdy neviděl
        # (doložený Červený trpaslík). Grounding block s explicit „PAMĚŤ
        # NEOBSAHUJE X" NEZABRAL (persona > grounding). Bypass jako sources_answer.
        # HANS_DATETIME_ANSWER_V1 — datum/čas ze systémových hodin, deterministicky
        # a PŘED modelem (jinak si datum rozepíše špatně a A1 pak abstinuje).
        try:
            from scripts.hans_recall import datetime_answer as _dta
            _dtans = _dta(ctx.user_message)
            if _dtans:
                print("[Chat] HANS_DATETIME_ANSWER_V1 → deterministická odpověď")
                try:
                    self.conv_store.add_exchange(ctx.name, ctx.user_message, _dtans,
                                                 channel=ctx.channel)
                except Exception:
                    pass
                self._log_human_chat_to_diary(ctx.name, ctx.user_message, _dtans,
                                              bypass_kind="datetime")
                return _dtans
        except Exception as _dte:
            print(f"[Chat] datetime answer error: {_dte}")
        return _POKRACUJ

    def _sc_stav_tazatele(self, ctx):
        # HANS_ASKER_STATE_V1 — „vidíte mě?" / „kdo jsem já?" ze živých dat.
        try:
            from scripts.hans_recall import asker_state_answer
            _hi_as = getattr(self, "_hans_idle", None)
            _as = asker_state_answer(
                ctx.user_message, ctx.name,
                getattr(_hi_as, "_present_names", None) or [], self.config)
            if _as:
                print("[Chat] HANS_ASKER_STATE_V1 → odpověď ze živého stavu")
                try:
                    self.conv_store.add_exchange(ctx.name, ctx.user_message, _as,
                                                 channel=ctx.channel)
                except Exception:
                    pass
                self._log_human_chat_to_diary(ctx.name, ctx.user_message, _as,
                                              bypass_kind="asker_state")
                return _as
        except Exception as _ase:
            print(f"[Chat] asker state error: {_ase}")
        return _POKRACUJ

    def _sc_kniha(self, ctx):
        # HANS_BOOK_RECOMMEND_V1 — doporučení z VLASTNÍ četby, ne z fantazie.
        try:
            from scripts.hans_recall import (asks_book_recommendation,
                                             book_recommendation)
            if asks_book_recommendation(ctx.user_message):
                _br = book_recommendation(
                    (self.config.get("diary_db")
                     or "data/hans_diary.db"), self.config)
                if _br:
                    print("[Chat] HANS_BOOK_RECOMMEND_V1 → z dočtených knih")
                    try:
                        self.conv_store.add_exchange(ctx.name, ctx.user_message, _br,
                                                     channel=ctx.channel)
                    except Exception:
                        pass
                    self._log_human_chat_to_diary(ctx.name, ctx.user_message, _br,
                                                  bypass_kind="book_recommend")
                    return _br
        except Exception as _bre:
            print(f"[Chat] book recommend error: {_bre}")
        return _POKRACUJ

    def _sc_znalosti(self, ctx):
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
                if _ikc(ctx.user_message) or _aap(ctx.user_message, self.config):
                    # HANS_PERSON_CARD_VOICE_V1 — kartu vyslov, nevysypej
                    from scripts.hans_recall import person_card_voiced
                    _pcard = person_card_voiced(_dbp_kb, ctx.user_message,
                                                self.config, asker=ctx.name)
            except Exception:
                _pcard = ""
            _kb = _pcard or knowledge_check_bypass(_dbp_kb, ctx.user_message, asker=ctx.name)
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
                    _topic_kb = _extract_knowledge_topic(ctx.user_message)
                    # HANS_PAINT_GATE_AFTER_BYPASS_V1 — dohledání je JEDINÁ věc
                    # nad přesunutou bránou, která potřebuje mozek (shrnuje
                    # článek LLM). Při běžícím renderu se přeskočí, ať nesebere
                    # VRAM obrazu; odpověď pak spadne na poctivé „nemám
                    # záznam" nebo na hlášku o malování níž.
                    # ⚠️ `lookup_now` si sám kontroluje jen `brain_available`,
                    # a ta je při renderu PRAVDA (Ollama běží, jen se o GPU
                    # dělí) — proto tahle hlídka navíc.
                    if _topic_kb:
                        try:
                            from scripts.avatar_render import render_in_progress
                            if render_in_progress():
                                _topic_kb = ""
                                print("[Chat] dohledání odloženo — právě se maluje")
                        except Exception:
                            pass
                    if _topic_kb:
                        _prov = lookup_now(self.config, _dbp_kb, _topic_kb,
                                           ctx.user_message, asker=ctx.name)
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
                    self.conv_store.add_exchange(ctx.name, ctx.user_message, _kb, channel=ctx.channel)
                except Exception:
                    pass
                # HANS_BYPASS_TRACE_V1 — označ deterministickou cestu
                self._log_human_chat_to_diary(ctx.name, ctx.user_message, _kb,
                                              bypass_kind=_bypass_kind)
                return _kb
        except Exception as _kce:
            print(f"[Chat] knowledge check bypass error: {_kce}")
        return _POKRACUJ

    def _sc_pamatujes(self, ctx):
        # HANS_REMEMBER_HONEST_V1 (5.9.) — NEROB NAROK NA PAMET, KTERY NEPLATI.
        # Doloheno: „pamatuj si ze me bavi priroda" -> „Ano, pamatuji si, ze
        # vas bavi priroda." Jenze v tu chvili ulozeno NENI nic; zajem doplni
        # az nocni `extract_person_interests` nad `human_chat`.
        # Volny hovor smi fabulovat [[free-chat-may-confabulate]], ale NAROK
        # NA VLASTNI PAMET je jina trida — kdyby se uzivatel za deset minut
        # zeptal „co me bavi?", Hans by to jeste nevypsal.
        #
        # ⛔ ZAPSAT ZAJEM ROVNOU SE NESMI (rozhodnuto 4.9.): vedlejsi ucinek
        # nesmi viset na jedne ceste, protoze chatovy prikaz agentni vrstvu
        # preskoci. Pokryva to nocni extrakce, ktera routovani nevidi.
        # Odpoved se proto jen srovna s pravdou: Hans slibi, ze si to zapise.
        # Slib je splnitelny — vymena UZ JE v `human_chat`, odkud nocni pass cte.
        #
        # 🟡 POCTIVE K CETNOSTI: na 1 359 realnych zpravach by tahle vetev
        # nezabrala ANI JEDNOU (2 zadosti o zapamatovani, zadna z nich fakt
        # o mluvcim). Je to oprava tridy, ne caste vady.
        # ⚠️ Kdyz LLM router posle tutez vetu na `/zajmy`, dostane uzivatel
        # VYPIS zajmu — to je neuzitecne, ale NENI to falesny narok na pamet,
        # takze se tim tahle oprava nemine.
        try:
            from scripts.hans_intent import (je_fakt_o_mluvcim,
                                             zada_o_zapamatovani)
            if (zada_o_zapamatovani(ctx.user_message)
                    and je_fakt_o_mluvcim(ctx.user_message)):
                from scripts.cz_names import address as _adr
                _os = _adr(ctx.name, self.config) if ctx.name else "pane"
                _rh = ("Zapíšu si to k Vašim zájmům, %s. Ještě to v paměti "
                       "nemám — projdu si dnešní hovor večer a doplním to." % _os)
                print("[Chat] HANS_REMEMBER_HONEST_V1 → deterministic bypass")
                try:
                    self.conv_store.add_exchange(ctx.name, ctx.user_message, _rh,
                                                 channel=ctx.channel)
                except Exception:
                    pass
                self._log_human_chat_to_diary(ctx.name, ctx.user_message, _rh)
                return _rh
        except Exception as _rhe:
            logging.getLogger(__name__).debug(
                'HANS_REMEMBER_HONEST_V1 preskocen: %s', _rhe)
        return _POKRACUJ

    def _sc_zdroje(self, ctx):
        # HANS_SOURCE_QUERY_V1 — bypass LLM (17.7.). hans-czech persona
        # odmítá sdílet URL i s explicitním groundingem — persona finetune
        # silnější než system prompt. Vzor `commitments_answer` /
        # `film_knowledge_answer`: deterministická odpověď mimo LLM.
        try:
            from scripts.hans_recall import is_source_query, sources_answer
            if is_source_query(ctx.user_message):
                _dbp_sa = (self.config.get("diary_db")
                           or (self.config.get("hans_idle", {}) or {}).get("diary_db")
                           or "data/hans_diary.db")
                _sa = sources_answer(_dbp_sa, ctx.user_message, asker=ctx.name)
                if _sa:
                    print("[Chat] HANS_SOURCE_QUERY_V1 → deterministic bypass")
                    try:
                        self.conv_store.add_exchange(ctx.name, ctx.user_message, _sa, channel=ctx.channel)
                    except Exception:
                        pass
                    # HANS_BYPASS_TRACE_V1 (19.7.) — sdílený bypass_note přes
                    # _log_human_chat_to_diary(bypass_kind=…). Dřív inline
                    # (HANS_SOURCE_QUERY_BYPASS_NOTE_V1 18.7.), teď 1 cesta
                    # pro všechny bypass kinds.
                    self._log_human_chat_to_diary(ctx.name, ctx.user_message, _sa,
                                                  bypass_kind="sources")
                    return _sa
        except Exception as _sae:
            print(f"[Chat] source query answer error: {_sae}")
        return _POKRACUJ

    def _sc_prohloubeni(self, ctx):
        # HANS_STUDY_DEEPEN_V2 — kritika/rozhodnutí ČISTÝM TEXTEM (ne jen
        # /prohloubit). Gated: jen když čeká návrh prohloubení. Klasifikuje
        # reakci uživatele a rovnou ji aplikuje.
        try:
            _dr = self._maybe_deepen_response(ctx.name, ctx.user_message)
            if _dr:
                try:
                    self.conv_store.add_exchange(ctx.name, ctx.user_message, _dr, channel=ctx.channel)
                except Exception:
                    pass
                self._log_human_chat_to_diary(ctx.name, ctx.user_message, _dr)
                return _dr
        except Exception as _de:
            print(f"[Chat] deepen response error: {_de}")
        return _POKRACUJ

    def _sc_ceka_malba(self, ctx):
        # ── HANS_CHAT_WAIT_FOR_PAINT_V1 (5.8.) — maluju, ozvu se potom ──────
        # Chatová odpověď natáhne hans-czech (8 GB) do VRAM a tím podřízne
        # běžící render: FLUX se nevejde, spadne do lowvram a obraz trvá
        # násobně dýl (naměřeno 475 s vs 210 s). `pause_warmup` tenhle případ
        # NEKRYJE — ta zabíjí jen automatické re-piny, ne reálný chat.
        # Proto radši krátká poctivá věta než tichý sabotovaný obraz.
        # ⚠️ HANS_PAINT_GATE_AFTER_BYPASS_V1 (5.9.) — MÍSTO JE PODSTATNÉ.
        # Nesmí se posunout zpátky nahoru za příkazy: mezi tímto řádkem
        # a příkazy leží sedm deterministických bypassů, které mozek
        # nepotřebují, a nahoře je brána brala s sebou.
        try:
            from scripts.avatar_render import render_in_progress
            if render_in_progress():
                from scripts.hans_persona import persona_name as _pn
                # HANS_CMD_ADDRESSEE_V1 — i tahle hláška oslovovala ženu
                # „pane"; je to jediná natvrdo psaná v celé `send_chat_message`
                # a ukládá se rovnou do `conv_store`, takže se srovnává TADY,
                # ne až za návratem.
                from scripts.cz_names import address as _adr_paint
                _reply = ("Zrovna maluji, %s — až obraz dokončím, budu se "
                          "Vám plně věnovat. Chvilku strpení."
                          % (_adr_paint(ctx.name, self.config) if ctx.name else "pane"))
                try:
                    self.conv_store.add_exchange(ctx.name, ctx.user_message, _reply,
                                                 channel=ctx.channel)
                except Exception:
                    pass
                self._log_human_chat_to_diary(ctx.name, ctx.user_message, _reply,
                                              bypass_kind="paint_wait")
                print("[Chat] odloženo — %s právě renderuje obraz" % _pn(self.config))
                return _reply
        except Exception as _pe:
            print(f"[Chat] paint-wait gate error: {_pe}")
        return _POKRACUJ

    def _sc_agent(self, ctx):
        # ── HANS_AGENT_V1 — agentní vrstva (kontextové akce z konverzace) ──
        # PO parse_command (příkazy mají přednost), PŘED běžným chatem.
        # (1) čeká na osobu potvrzení návrhu? ano/ne → proveď/zruš.
        # (2) jinak router: přeje si uživatel akci? → návrh + [ano/ne].
        # Deferral-safe: výpadek LLM / vypnuto → None → běžný chat pokračuje.
        try:
            _agent = self._agent_router()
            if _agent is not None:
                _conf = _agent.check_confirmation(self, ctx.name, ctx.user_message)
                if _conf is not None:
                    _conf = self._agent_oslov(_conf, ctx.name)   # HANS_AGENT_ADDRESSEE_V1
                    try:
                        self.conv_store.add_exchange(ctx.name, ctx.user_message, _conf, channel=ctx.channel)
                    except Exception:
                        pass
                    self._log_human_chat_to_diary(ctx.name, ctx.user_message, _conf)
                    return _conf
                _prop = _agent.propose(self, ctx.name, ctx.user_message)
                if _prop:
                    _prop = self._agent_oslov(_prop, ctx.name)   # HANS_AGENT_ADDRESSEE_V1
                    try:
                        self.conv_store.add_exchange(ctx.name, ctx.user_message, _prop, channel=ctx.channel)
                    except Exception:
                        pass
                    if ctx.on_sentence:
                        try:
                            ctx.on_sentence(_prop)
                        except Exception:
                            pass
                    self._log_human_chat_to_diary(ctx.name, ctx.user_message, _prop)
                    return _prop
        except Exception as _ae:
            print(f"[Chat] agent layer error: {_ae}")
        return _POKRACUJ

    def _sc_podklad(self, ctx):
        ctx.system   = self._build_system(ctx.name, user_msg=ctx.user_message)
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
        ctx._raw_message = ctx.user_message
        _q_for_retrieval = ctx.user_message
        if ctx.name and ctx.name.lower() not in ctx.user_message.lower():
            _q_for_retrieval = f"{ctx.name} se ptá: {ctx.user_message}"
        # ── HANS_SELFCONSISTENCY_A1_V1 ────────────────────────────────────
        # Předpočítej grounding JEDNOU (šetří RAG oproti výpočtu ve
        # _stream_message) a zjisti výsledek. Jen u 'factual_nofacts'
        # (faktický dotaz BEZ opory v RAG = rizikový volný výmysl) spusť A1
        # self-consistency: N× generuj, změř rozptyl → nestabilní → deter-
        # ministická abstinence (routing, ne prompt). Deferral-safe.
        ctx._grounding = _GROUNDING_UNSET
        ctx._a1_abstain = False
        try:
            ctx._grounding = self._build_grounding(_q_for_retrieval, ctx.name)
            # HANS_ZPRAVY_PODKLAD_V1 (3. 10.) — otázka na DĚNÍ ve světě dostane
            # sebrané zprávy (Matrix 3. 10.: Francie vymyšlená a připsaná Demagogu).
            # Ne u otázek na Hanse samotného a u názorů (tam zprávy nepatří).
            # HANS_ZPRAVY_NE_FILOZOFIE_V1 (7. 10.) — „…ženou odsouzenou k trestu SMRTI,
            # která přežila popravu?“ je dotaz na ZPRÁVU, ne na smysl smrti; názorová
            # větev ji vzala podle filozofického slova, podklad se přeskočil a model
            # si případ vymyslel (jméno, zemi, rok). Přímá žádost o názor („co si
            # myslíš o…“) zprávy dál nedostává; věta zařazená jen podle tématu ano,
            # když pro ni sebrané zprávy shodu MAJÍ.
            # 📏 2 613 vět: jen podle slova 37 (4 reálné), podklad by dostala 0.
            _nazor_jen_slovem = False
            if getattr(self, '_grounding_outcome', '') == 'opinion':
                try:
                    from scripts.hans_opinion import _ASK_PAT as _op_ask
                    _nazor_jen_slovem = not _op_ask.search(str(ctx.user_message or ""))
                except Exception:
                    _nazor_jen_slovem = False
            if (getattr(self, '_grounding_outcome', '') not in ('self_state', 'opinion')
                    or _nazor_jen_slovem):
                try:
                    from scripts.hans_zpravy import zpravy_podklad
                    # HANS_ZPRAVY_PODKLAD_F1_V1 (8. 10.) — navazující otázka („tak proč
                    # je to v nejistotě“, „a kolik lidí je nakažených“) téma nenese;
                    # nese ho PŘEPIS z vlákna, který už existuje pro abstinenční
                    # brzdu. Rozhovory 8. 10.: 8 takových otázek bez podkladu →
                    # výmysl nebo falešné „nemám záznam“, 1× cizí událost.
                    # 📏 428 párů věta × přepis z logu: jen z přepisu 17 (všechny
                    # správná událost), jen ze syrové věty 0, jiná událost 1
                    # (přepis správně). Přepis má proto přednost, věta je záloha.
                    _zp_dotaz = getattr(self, '_f1_query', None) or ctx.user_message
                    # HANS_ZPRAVY_JMENO_JEN_BEZ_OPORY_V1 — slabší shoda podle jména
                    # jen když otázka jinou oporu nemá
                    _zp_jm = getattr(self, '_grounding_outcome', '') == 'factual_nofacts'
                    _zpb = zpravy_podklad(_zp_dotaz, self.config, jmeno=_zp_jm)
                    if not _zpb and _zp_dotaz != ctx.user_message:
                        _zp_dotaz = ctx.user_message
                        _zpb = zpravy_podklad(_zp_dotaz, self.config, jmeno=_zp_jm)
                    elif _zpb and _zp_dotaz != ctx.user_message:
                        logging.getLogger(__name__).info(
                            'HANS_ZPRAVY_PODKLAD_F1_V1: podklad ze zpráv podle přepisu %r',
                            str(_zp_dotaz)[:70])
                    if _zpb:
                        _stary = (ctx._grounding if isinstance(ctx._grounding, str)
                                  and ctx._grounding is not _GROUNDING_UNSET else "")
                        _stary = _stary.replace(ANTIKONFAB, "").strip()
                        ctx._grounding = ANTIKONFAB + "\n\n" + _zpb + (
                            ("\n\n" + _stary) if _stary else "")
                        self._vysledek_groundingu('grounded', 'zpravy')
                        try:   # HANS_ZPRAVY_RETELL_V1 — titulky pro převyprávění
                            from scripts.hans_zpravy import posledni_podklad
                            ctx._zpravy = posledni_podklad(_zp_dotaz)
                            if ctx._zpravy:
                                ctx._zpravy["dotaz"] = _zp_dotaz
                        except Exception:
                            ctx._zpravy = None
                except Exception as _zpe:
                    logging.getLogger(__name__).debug('zprávy podklad: %s', _zpe)
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
                    _a1_text = getattr(self, '_f1_query', None) or ctx._raw_message
                    if _a1_text != ctx._raw_message:
                        logging.getLogger(__name__).info(
                            'HANS_A1_THREAD_TEXT_V1: A1 se rozhoduje z %r '
                            '(místo %r)', _a1_text[:60], ctx._raw_message[:40])
                    _st = self_topic(_a1_text, self.config)
                    # HANS_A1_PERSONA_NAME_IS_SELF_V1 (20. 9.) — JMÉNO PERSONY
                    # JE „ON SÁM“. Doloženo 19. 9. 17:02: F1 přepsal větu
                    # „na co se tebe nejvíc těšíme“ na „Na co se Hans těší
                    # v následujících dnech?“ — a klasifikátor ji pak četl jako
                    # dotaz na OSOBU (změřeno 3/3 běhy, deterministicky), takže
                    # výjimka níž nesepnula, A1 rozhodla a Hans na obyčejnou
                    # otázku odpověděl „K tomuhle nemám spolehlivý záznam“.
                    # HANS_A1_THREAD_TEXT_V1 (21. 8.) přitom rozhoduje
                    # z ROZŘEŠENÉ věty schválně — obě opravy si tím navzájem
                    # braly účinek.
                    # ⚠️ Text se NEPŘEPISUJE: náhrada jména zájmenem rozbije
                    # shodu podmětu se slovesem („Na co se ty těší“).
                    # ⚠️ A NEPATŘÍ to do `self_topic` — ten sdílí i grounding
                    # guard (`_o_sobe`), kde by se tím ROZŠÍŘILA výjimka
                    # HANS_GUARD_SELF_TOPIC_V1, tedy opačným směrem, než
                    # ukazuje měření z 20. 9. rána.
                    # 📏 Změřeno na 32 reálných přepisech F1 z logů a na 19
                    # zprávách se jménem: překlopí 3 + 2 věty, všechny správně
                    # a deterministicky, 0 falešných; kontrolní „kdo tam
                    # hraje?“ zůstává `dum`, takže vada, kvůli které vznikl
                    # HANS_A1_THREAD_TEXT_V1, se nevrací.
                    if _st == 'osoba':
                        try:
                            from scripts.hans_persona import persona_name
                            _jm = (persona_name(self.config) or '').strip()
                            if _jm and re.search(
                                    r"(?<![0-9A-Za-zá-žÁ-Ž])"
                                    + re.escape(_jm)
                                    + r"[a-zá-ž]{0,3}(?![0-9A-Za-zá-ž])",
                                    _a1_text, re.IGNORECASE):
                                logging.getLogger(__name__).info(
                                    'HANS_A1_PERSONA_NAME_IS_SELF_V1: %r nese '
                                    'jméno persony → čtu jako dotaz na sebe',
                                    _a1_text[:60])
                                _st = 'asistent'
                        except Exception as _pne:
                            logging.getLogger(__name__).debug(
                                'HANS_A1_PERSONA_NAME_IS_SELF_V1: %s', _pne)
                    if _st in ('asistent', 'dum'):
                        _skip_a1 = True
                        logging.getLogger(__name__).info(
                            'HANS_A1_NOT_FOR_OWN_STATE_V1: A1 přeskočena — '
                            'dotaz je %r (opora je v promptu, ne v RAG): %.50s',
                            _st, ctx._raw_message)
                    # HANS_A1_OWN_WORK_IN_PROMPT_V1 (1. 10.) — „Proč jsi si vybral
                    # Johann Sebastian Bacha?“ klasifikátor 4/4 čte jako OSOBU
                    # (Bach), A1 abstinovala a dohledání vrátilo článek o Bachovi
                    # místo odpovědi o vlastním díle. Přeskočit jen když předmět
                    # doslova stojí v přehledu děl a studia v promptu.
                    if not _skip_a1:
                        try:
                            from scripts.hans_recall import predmet_vlastniho_dila
                            _dbp_vd = (self.config.get("diary_db")
                                       or "data/hans_diary.db")
                            # přepis F1 kolísá („Proč JSEM si vybral…“, změřeno
                            # 1. 10.) → i původní věta
                            _pv = (predmet_vlastniho_dila(_a1_text, _dbp_vd)
                                   or predmet_vlastniho_dila(ctx._raw_message, _dbp_vd))
                            # HANS_OWN_WORK_A1_SKIP_V1 (4. 10.) — vlastní obraz /
                            # esej: opora je v promptu (HANS_OWN_WORK_DETAIL_V1/V2)
                            if not _pv:
                                from scripts.hans_recall import je_dotaz_na_vlastni_dilo
                                if je_dotaz_na_vlastni_dilo(ctx._raw_message, _dbp_vd):
                                    _pv = "vlastní obraz/psaní"
                            if _pv:
                                _skip_a1 = True
                                ctx._vlastni_dilo = True
                                logging.getLogger(__name__).info(
                                    'HANS_A1_OWN_WORK_IN_PROMPT_V1: A1 přeskočena — '
                                    'vlastní dílo, %r je v přehledu děl: %.50s',
                                    _pv, ctx._raw_message)
                        except Exception as _vde:
                            logging.getLogger(__name__).debug(
                                'HANS_A1_OWN_WORK_IN_PROMPT_V1: %s', _vde)
                except Exception as _ste:
                    logging.getLogger(__name__).debug('self_topic: %s', _ste)
                # HANS_A1_ONLY_FOR_QUESTIONS_V1 (20. 9.) — ROZKAZ NENÍ DOTAZ.
                # Doloženo 16. 9. 13:20: na pokyn „nemaluj stále dokola
                # zapadající slunce." Hans odpověděl „K tomuhle nemám
                # spolehlivý záznam a nerad bych si domýšlel" — A1 posoudila
                # stabilitu odpovědi na větu, která se na nic neptá.
                # Ze čtyř dnů provozu rozhodla A1 pětkrát a tohle byl
                # jeden z nich; obě její abstinence byly falešné.
                # ⛔ Predikát `_je_dotaz_ne_zadost` (ten, co chrání studium)
                # sem NEJDE: změřeno, že „pověz mi něco o filmu X" označí
                # jako NE-dotaz, takže by A1 vypnul přesně u faktické žádosti,
                # kvůli které existuje.
                # ✅ Proto sdílíme `_looks_like_request` z agentní vrstvy —
                # ta zná i imperativy typu „pověz / popiš / zjisti / najdi"
                # (`_REQUEST_OPENERS`). Změřeno: „nemaluj…" False, „pověz mi
                # něco o filmu…" True, faktické otázky True.
                # ⚠️ Rozhoduje se z RAW zprávy, ne z přepisu F1: jestli je
                # věta rozkaz, je vlastnost toho, co člověk NAPSAL. Přepis
                # (HANS_A1_THREAD_TEXT_V1) slouží ke klasifikaci TÉMATU.
                if not _skip_a1:
                    try:
                        _ar = self._agent_router()
                        # HANS_A1_QUESTION_NO_MARK_V1 (8. 10.) — „a kdo ho postavil“
                        # bez otazníku je otázka: tázací slovo na začátku po výplni
                        # („a“, „tak“, „ok“). Sdílený predikát agenta se nemění.
                        # 📏 1 356 reálných vět: 1 nová (otázka); 1 317 vět
                        # tazatele: 21 nových, všechny otázky; rozkazy beze změny.
                        if (_ar is not None and not _ar._looks_like_request(
                                ctx._raw_message)
                                and not _a1_otazka_bez_otazniku(ctx._raw_message)):
                            _skip_a1 = True
                            logging.getLogger(__name__).info(
                                'HANS_A1_ONLY_FOR_QUESTIONS_V1: A1 přeskočena '
                                '— věta se na nic neptá: %.50s', ctx._raw_message)
                    except Exception as _lre:
                        logging.getLogger(__name__).debug(
                            'HANS_A1_ONLY_FOR_QUESTIONS_V1: %s', _lre)
                if not _skip_a1:
                    from scripts.hans_selfconsistency import is_unstable
                    _a1_vysl = is_unstable(self.config, ctx._raw_message)
                    if _a1_vysl is True:
                        ctx._a1_abstain = True
                    elif _a1_vysl is False:
                        # HANS_CLAIM_FILTER_V1 — stabilní vzorky poslouží jako měřítko
                        # pro hotovou odpověď (viz `_sc_pojistky`)
                        try:
                            from scripts.hans_selfconsistency import last_samples
                            ctx._a1_samples = last_samples()
                        except Exception:
                            ctx._a1_samples = []
        except Exception as _a1e:
            logging.getLogger(__name__).warning('A1 gate failed: %s', _a1e)
            ctx._grounding = _GROUNDING_UNSET

    def _sc_nazor(self, ctx):
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
                                    and is_opinion_query(ctx._raw_message)):
                _ob = opinion_block(self.config)
                if _ob:
                    ctx.system += _ob
                    logging.getLogger(__name__).info(
                        'G1: názorový dotaz → blok vlastních postojů '
                        'injektován (%d zn)', len(_ob))
        except Exception as _oge:
            logging.getLogger(__name__).warning(
                'G1 opinion grounding failed: %s', _oge)

    def _sc_a1(self, ctx):
        ctx._dohledano = False   # HANS_ANCHOR_LOOKUP_ON_ADMIT_V1 — ať se nehledá 2×
        if ctx._a1_abstain:
            # HANS_ANCHOR_LOOKUP_V1 — než odmítneš, zkus to dohledat.
            ctx.response = self._dohledej_kotvu(ctx._raw_message, ctx.name) or A1_ABSTAIN_TEXT
            ctx._dohledano = True
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
                    _hist = self.conv_store.get_history(ctx.name) or []
                except Exception:
                    pass
                # CLAIM_RETRACT_GATE_V1 (13. 9.) — VYPINATELNE, default VYPNUTO.
                # Zmereno na cele historii: mechanismus se spustil 3x a vsechny
                # tri odvolane vety byly odvolane NEPRAVEM —
                #   19. 8. doporuceni knihy („z doporuceni bych navrhl…“),
                #   13. 9. nabidka („mohu si o tom neco precist“),
                #   13. 9. DOSLOVNA CITACE z deniku (artwork id 151259).
                # Presnost 0 ze 3. Jadro vady: abstinence na NOVOU otazku se
                # bere jako dukaz, ze STARA odpoved byla vymysl — to jsou dve
                # ruzne veci, a odvolavat dolozena data je horsi nez neodvolat
                # nic. Puvodni zamer (CLAIM_RETRACT_V1, 6. 8.) je spravny, jen
                # predikat neumi odlisit tvrzeni od nabidky ani overit oporu.
                # ⚠️ Kod se NEMAZE — az to predikat umi, staci prepnout klic.
                _resp2 = (append_retraction(ctx.response, ctx._raw_message, _hist)
                          if ((self.config.get('chat', {}) or {})
                              .get('claim_retract_enabled', False))
                          else ctx.response)
                if _resp2 != ctx.response:
                    logging.getLogger(__name__).info(
                        'CLAIM_RETRACT_V1: beru zpět dřívější tvrzení '
                        '(abstinence u %r)', (ctx._raw_message or '')[:60])
                    ctx.response = _resp2
            except Exception as _cre:
                logging.getLogger(__name__).warning(
                    'CLAIM_RETRACT_V1 selhal (odpověď ponechána): %s', _cre)
            if ctx.on_sentence:
                try:
                    ctx.on_sentence(ctx.response)   # ať to TTS vysloví
                except Exception:
                    pass
        elif (getattr(ctx, "_zpravy", None)
              and (self.config.get("zpravy", {}) or {}).get("prevypraveni", True)):
            # HANS_ZPRAVY_RETELL_V1 (7. 10.) — otázka má podklad ze zpráv → místo
            # volné odpovědi krátké převyprávění titulků (viz `hans_zpravy.prevypravej`)
            # + skutečné odkazy přiložené kódem (hlas adresy nečte).
            try:
                from scripts.hans_zpravy import prevypravej, zpravy_odkazy
                from scripts.cz_names import vocative as _zr_voc
                from scripts.hans_persona import persona_name as _zr_pn
                _zr_text, _zr_jak = prevypravej(
                    self.config, ctx._zpravy.get("dotaz") or ctx._raw_message,
                    ctx._zpravy["radky"],
                    _zr_voc(ctx.name) if ctx.name else "", _zr_pn(self.config))
                if ctx.on_sentence:
                    try:
                        ctx.on_sentence(_zr_text)      # hlas řekne už ověřený text
                    except Exception:
                        pass
                _zr_odk = []
                try:
                    _zr_odk = [o_["url"] for o_ in zpravy_odkazy(ctx._zpravy["uid"], limit=2)]
                except Exception:
                    pass
                ctx.response = _zr_text + ("\n" + "\n".join(_zr_odk) if _zr_odk else "")
                logging.getLogger(__name__).info(
                    "HANS_ZPRAVY_RETELL_V1: odpověď z titulků (%s, %d řádků, odkazů %d)",
                    _zr_jak, len(ctx._zpravy["radky"]), len(_zr_odk))
            except Exception as _zre:
                logging.getLogger(__name__).warning(
                    "HANS_ZPRAVY_RETELL_V1 selhalo (%s) → běžná odpověď", _zre)
                ctx.response = self._stream_message(
                    (ctx.system, ctx.user_message), name=ctx.name,
                    on_sentence=ctx.on_sentence, grounding=ctx._grounding)
        else:
            ctx.response = self._stream_message(
                (ctx.system, ctx.user_message), name=ctx.name,
                on_sentence=ctx.on_sentence, grounding=ctx._grounding)  # CHAT_ON_SENTENCE_V1

    def _sc_pojistky(self, ctx):
        # G4D_DEDUP_ADDRESS_V1 — očisti opakované oslovení PŘED
        # rozdvojením do conv_store i diary→RAG (oba cíle čisté).
        if ctx.response:
            # HANS_ZPRAVY_URL_GUARD_V1 (4. 10.) — vymyšlený odkaz na zpravodajský
            # web (není v nasbíraných titulcích) → věty s ním pryč + poctivá věta.
            # ⚠️ Hlas URL nečte a mluví po větách; opraví se hlavně zápis a Matrix.
            try:
                from scripts.hans_zpravy import odkazy_vymyslene
                _vym = odkazy_vymyslene(ctx.response)
                if _vym:
                    _vety = re.split(r"(?<=[.!?])\s+|\n+", ctx.response)
                    _zbyt = [v for v in _vety if not any(u in v for u in _vym)
                             and not re.search(r"odkaz\w*\s*(?:na\s+čl[áa]nek)?\s*:\s*$", v)]
                    ctx.response = (" ".join(x for x in _zbyt if x.strip()).strip()
                                    + " Odkaz si ale nevymýšlím — jestli chcete, vypíšu "
                                    "skutečné zprávy i s odkazy.").strip()
                    logging.getLogger(__name__).info(
                        "HANS_ZPRAVY_URL_GUARD_V1: vyřazen vymyšlený odkaz %s", _vym[0][:80])
            except Exception as _zue:
                logging.getLogger(__name__).debug("url guard: %s", _zue)
            # HANS_NO_FALSE_MEMORY_CLAIM_V1 (7. 10.) — na cestě BEZ FAKT (v zápiscích
            # nic nebylo) odpověď začínala „V paměti mám záznamy o…“ a pokračovala
            # obecnou znalostí; jednou i „vytvořil jsem si k tomu obraz“ (v deníku
            # žádný). Tvrzení o vlastní paměti je údaj o DATECH, ne tón → opravuje
            # se kódem: fráze se přepíše na obecnou znalost, věta o vlastním díle
            # (na téhle cestě nemá čím být podložená) vypadne i s nabídkou poslání.
            # 📏 Přepisy: 171 odpovědí bez fakt, 7× fráze o záznamech, 1× dílo;
            # na cestách S podkladem se nic nemění.
            try:
                if getattr(self, "_grounding_outcome", "") == "factual_nofacts":
                    _puv = ctx.response
                    _nov = _FALESNA_PAMET_A.sub(_falesna_pamet_nahrada, _puv)
                    _nov = _FALESNA_PAMET_B.sub("vím jen z obecných znalostí", _nov)
                    _vety = re.split(r"(?<=[.!?])\s+", _nov)
                    _bez = []
                    _vyp = False
                    for _v in _vety:
                        if _VLASTNI_DILO_TVRZENI.search(_v):
                            _vyp = True
                            continue
                        if _vyp and _NABIDKA_POSLAT.search(_v):
                            continue
                        _bez.append(_v)
                    if _vyp and "".join(_bez).strip():
                        _nov = " ".join(_bez)
                    if _nov != _puv:
                        ctx.response = _nov
                        logging.getLogger(__name__).info(
                            "HANS_NO_FALSE_MEMORY_CLAIM_V1: opraveno tvrzení o vlastní "
                            "paměti%s (cesta bez fakt)", " a vlastním díle" if _vyp else "")
                    # HANS_CLAIM_FILTER_V1 (7. 10.) — jen když A1 OPRAVDU běžela
                    # a prošla (otázka na svět, ne na Hanse): odpověď se drží
                    # toho, co se opakuje ve vzorcích; věty o vlastním zdroji pryč.
                    _vz = getattr(ctx, "_a1_samples", None) or []
                    if (len(_vz) >= 3 and not getattr(ctx, "_dohledano", False)
                            and (self.config.get("selfconsistency", {}) or {}).get(
                                "claim_filter", True)):
                        from scripts.hans_claim_filter import filtruj as _cf_filtruj
                        from scripts.cz_names import vocative as _cf_voc
                        _cf_new, _cf_st = _cf_filtruj(
                            ctx.response, ctx._raw_message, _vz,
                            (ctx.name or "", _cf_voc(ctx.name) if ctx.name else ""))
                        # HANS_CLAIM_RESTYLE_V1 — strohý vzorek zkusit říct Hansovým
                        # hlasem (krátké volání + kontrola věrnosti); když neprojde,
                        # zůstane strohý vzorek.
                        if _cf_st.get("nahrazeno"):
                            try:
                                from scripts.hans_claim_filter import prestyluj as _cf_styl
                                from scripts.hans_persona import persona_name as _cf_pn
                                _cf_hlas = _cf_styl(self.config, ctx._raw_message, _cf_new,
                                                    _cf_voc(ctx.name) if ctx.name else "",
                                                    _cf_pn(self.config))
                                if _cf_hlas:
                                    _cf_new = _cf_hlas
                                    _cf_st["hlasem"] = True
                            except Exception as _cse:
                                logging.getLogger(__name__).debug("claim restyle: %s", _cse)
                        if _cf_new != ctx.response:
                            logging.getLogger(__name__).info(
                                "HANS_CLAIM_FILTER_V1: vyřazeno vět o vlastním zdroji %d, "
                                "s údajem mimo vzorky %d%s", _cf_st["zdroj"], _cf_st["tvrzeni"],
                                (" → nahrazeno stabilním vzorkem"
                                 + (" (Hansovým hlasem)" if _cf_st.get("hlasem") else " (strohý)"))
                                if _cf_st["nahrazeno"] else "")
                            ctx.response = _cf_new
            except Exception as _fme:
                logging.getLogger(__name__).debug("false memory claim: %s", _fme)
            # HANS_DEMAGOG_GUARD_V1 (3. 10.) — model si vymyslel „ověřené výroky“
            # (tvar zkopírovaný z historie, 3× v /tazatel) → nahradit skutečným
            # výpisem z Demagogu. Výstup příkazu sem nedojde (vrací se dřív).
            # ⚠️ Hlas mluví po větách, takže řečené už nevrátí; opraví se zápis.
            try:
                from scripts.hans_zpravy import demagog_vymysleno
                if demagog_vymysleno(ctx.response):
                    from scripts.chat_commands import _cmd_demagog, thread_demagog, parse_command
                    from scripts import hans_thread as _thr_dm
                    _arg = thread_demagog(ctx.user_message, _thr_dm.recent_turns(
                        self, ctx.name, ctx.channel))
                    _pc = parse_command(ctx.user_message)
                    if _arg or (_pc and _pc[0] == "demagog"):
                        # otázka MÍŘÍ na ověřené výroky → skutečný výpis
                        ctx.response = ("Ověřené výroky beru jen přímo z Demagog.cz, nic si "
                                        "k nim nedomýšlím:\n" + _cmd_demagog(
                                            self, ctx.name, _arg or ctx.user_message))
                    else:
                        # HANS_DEMAGOG_GUARD_V2 — jiná otázka (Francie 3. 10.):
                        # vyhodit věty s vymyšleným Demagogem, zbytek nechat
                        # celá odpověď je nespolehlivá (obsah i zdroj vymyšlené) →
                        # sebrané zprávy, když k tématu něco mají, jinak poctivě
                        from scripts.chat_commands import _cmd_zpravy
                        _zp = _cmd_zpravy(self, ctx.name, ctx.user_message)
                        ctx.response = (_zp if _zp.startswith("Ve zprávách k tomu mám")
                                        else "K tomu nemám spolehlivý podklad a nerad bych "
                                             "si něco domýšlel.")
                    logging.getLogger(__name__).warning(
                        'HANS_DEMAGOG_GUARD_V1: vymyšlený Demagog v odpovědi opraven (%.60s)',
                        ctx.user_message)
            except Exception as _dge:
                logging.getLogger(__name__).debug('demagog pojistka: %s', _dge)
            # HANS_FILM_DIRECTOR_CHECK_V1 (21.8.) — přát si film, který doma
            # nemáme, je v pořádku (zvídavost), ale režiséra má mít správně.
            # Doloženo v simulovaném rozhovoru: „Sedmikrásky od Miloše Formana"
            # (natočila je Věra Chytilová a Hans o tom filmu nemá záznam).
            # Ověřuje se KNIHOVNOU, jinak Wikipedií; co ověřit nejde, zůstává.
            try:
                from scripts.film_director_check import zkontroluj_rezii
                _kodi_r = getattr(getattr(self, "_hans_idle", None), "kodi", None)
                # HANS_DIRECTOR_SAME_FILM_V1 — podklad a prompt nesou rok verze filmu
                _kx = "\n".join(str(x) for x in (
                    getattr(ctx, "_grounding", "") if isinstance(getattr(ctx, "_grounding", ""), str) else "",
                    getattr(ctx, "system", "") if isinstance(getattr(ctx, "system", ""), str) else ""))
                _r2 = zkontroluj_rezii(ctx.response, kodi=_kodi_r, config=self.config,
                                       kontext=_kx)
                if _r2 != ctx.response:
                    ctx.response = _r2
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
                        and ctx._grounding and ctx._grounding is not _GROUNDING_UNSET):
                    from scripts.grounding_guard import check as _gg_check
                    _facts = ctx._grounding.replace(ANTIKONFAB, ' ')
                    _facts = _facts.replace(ANTIKONFAB_NOFACTS, ' ')
                    # ⚠️ REFERENCÍ MUSÍ BÝT VŠECHNO, CO MODEL DOSTAL, ne jen
                    # grounding. První živý test (13:49) zahodil VĚTU, KTERÁ
                    # PODLOŽENÁ BYLA — fakta měl z historie rozhovoru, kdežto
                    # grounding v tu chvíli nesl jiný zápisek. Bez historie
                    # guard trestá správné odpovědi.
                    try:
                        for _h in (self.conv_store.get_history(ctx.name) or [])[-6:]:
                            _facts += ' ' + str(
                                _h.get('content', _h) if isinstance(_h, dict) else _h)
                    except Exception:
                        pass
                    # HANS_EVIDENCE_V1 — guard soudil proti `grounding` + 6
                    # zprávám, ale model dostal 19 bloků (~16 000 zn). Věta
                    # podložená blokem `kodi`/`room`/`diary` proto vypadala
                    # jako výmysl. Přidáváme JEN evidenční bloky — persona,
                    # autobiografie, nápady a nálada se do opory NEPOČÍTAJÍ
                    # (viz `_EVIDENCNI_BLOKY` a HANS_OWN_WORK_NOT_FACT_V1).
                    # Klíč je kvůli A/B měření a rychlému návratu.
                    _ev = getattr(self, "_posledni_evidence", "") or ""
                    _use_ev = bool((self.config.get("grounding_guard", {}) or {})
                                   .get("use_evidence", True))
                    # HANS_EVIDENCE_AB_V1 (7.9.) — PÁROVÉ MĚŘENÍ, dočasné.
                    # A/B přes restart NEFUNGUJE: odpověď se mezi běhy liší,
                    # takže počet zahozených vět je funkcí ODPOVĚDI, ne jen
                    # reference. Proto se guard spočítá DVAKRÁT nad TOUTÉŽ
                    # odpovědí — s evidencí i bez — a do logu jde rozdíl.
                    # Použije se varianta podle `use_evidence`. Druhý výpočet
                    # je jen množinová operace nad kmeny, řádově zdarma.
                    try:
                        _d_bez = _gg_check(ctx.response, _facts)[1]
                        _d_s = _gg_check(ctx.response, _facts + ' ' + _ev)[1] if _ev else _d_bez
                        logging.getLogger(__name__).info(
                            "HANS_EVIDENCE_AB_V1: bez evidence %d vět bez opory, "
                            "s evidencí %d (evidence %d zn, aktivní=%s)",
                            len(_d_bez), len(_d_s), len(_ev), _use_ev)
                        # ⚠️ `system.log` se rotuje a 4 soubory pokrývají jen
                        # ~2 DNY — měření, které má běžet déle, by se ztratilo.
                        # Píšeme proto i do trvalého souboru. JEN ČÍSLA,
                        # žádný obsah odpovědi ani promptu.
                        try:
                            import time as _t
                            _mp = Path("data/mereni"); _mp.mkdir(parents=True, exist_ok=True)
                            with open(_mp / "evidence_ab.log", "a",
                                      encoding="utf-8") as _f:
                                _f.write("%s\t%d\t%d\t%d\t%s\n" % (
                                    _t.strftime("%Y-%m-%d %H:%M:%S"),
                                    len(_d_bez), len(_d_s), len(_ev), _use_ev))
                        except Exception:
                            pass
                    except Exception:
                        pass
                    if _use_ev and _ev:
                        _facts += ' ' + _ev
                    _clean, _dropped = _gg_check(ctx.response, _facts)
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
                    # GROUNDING_GUARD_ACTIVE_V3 (24.8.) — práh 4 → 2 na TENKÉ
                    # cestě. ZMĚŘENO na výčtové otázce „jaké hrady jsou v Českém
                    # ráji?": 11:36 guard ZASÁHL (5 vět bez opory) → Hans přiznal,
                    # že víc neví. Jakmile odpověď zplynněla, spadl počet na 2-3
                    # (11:44, 11:46) → guard MLČEL a Hans vyjmenoval Gutštejn,
                    # Neuschwanstein a Opočno jako hrady Českého ráje.
                    # Práh 4 tedy propouštěl přesně tu třídu, kvůli které guard je.
                    # ⚠️ Věrné původnímu zdůvodnění: falešné poplachy z 12.8. byly
                    # „po JEDNÉ větě" a ty práh 2 dál propustí; a podmínka (a)
                    # `_tenky` beze změny drží guard mimo plnou RAG cestu, kde
                    # tamty poplachy vznikly.
                    #
                    # 🔴 GROUNDING_GUARD_ACTIVE_V4 (20. 9.) — PRÁH 2 → 5.
                    # Tohle VĚDOMĚ mění rozhodnutí V3 výš. Změřeno na provozním
                    # logu za 4 dny (`GUARD_MERENI_20_09`, ruční štítky
                    # v `tools/mereni_guard_stitky.py`):
                    #   • 16 zásahů na 162 chatových tahů = 10 %,
                    #   • z 59 zahozených vět bylo tvrzením o světě jen 25 %;
                    #     zbytek byly úvahy, řeč o vlastní činnosti, omluvy,
                    #     upřesňující otázky — a 3 věty, kterými Hans PŘIZNÁVAL,
                    #     že něco neví, tedy přímo ta anti-konfabulace,
                    #     kvůli které guard existuje,
                    #   • 9 z 16 zásahů bylo CELÝCH falešných (ani jedno
                    #     tvrzení o světě mezi zahozenými větami).
                    # Při prahu 5 klesnou falešné zásahy 9 → 1 a užitečné 7 → 4.
                    # ⛔ Rozlišovač „věta o mluvčím / otázka se neposuzuje" byl
                    # postaven a ZMĚŘEN jako slabší (ušetří 19 ze 44 falešných
                    # vět, ale dvě tvrzení o světě nově pustí), a v kombinaci
                    # s prahem vychází hůř než práh sám → nestaví se.
                    # 💬 Rozhodnutí uživatele 20. 9.: „radši ukecaný" než uťatý.
                    # CENA, kterou to platí: krátké výčty (2–4 věty bez opory)
                    # guard pustí — přesně případ hradů z V3.
                    _prah = int((self.config.get("grounding_guard", {}) or {})
                                .get("min_dropped_thin", 5))
                    # HANS_REFLECTIVE_ASK_V1 (30.8.) — na ÚVAHOVOU otázku
                    # („kdybyste měl…", „co je pro vás nejtěžší") je odpověď
                    # bez opory v zápiscích NORMÁLNÍ: Hans odpovídá z osobnosti,
                    # ne z deníku. Guard ji vykuchal a nahradil abstinencí,
                    # takže na dotaz po vlastním prožitku říkal „víc než tohle
                    # už o tom nemám" (doloženo 30.8. 13:36).
                    # Predikát je ÚZKÝ (2 shody z 1337 reálných replik) a sedí
                    # jen na otázku; retrieval ani intent se tím NEMĚNÍ.
                    _uvaha = False
                    try:
                        from scripts.hans_intent import is_reflective_ask
                        # HANS_SOURCE_META_MEMORY_V1 (30.8.) — metaotázka na
                        # spolehlivost paměti je TAKÉ úvaha. Doloženo: po
                        # potlačení špatné šablony spadla na abstinenci
                        # („Víc než tohle už o tom nemám"), tedy z jedné vadné
                        # odpovědi na druhou. Predikát je týž, který používá
                        # `is_source_query` a agentní guard — třetí místo téže
                        # pravdy, schválně sdílené.
                        from scripts.hans_recall import is_memory_meta_query
                        _uvaha = (is_reflective_ask(ctx._raw_message)
                                  or is_memory_meta_query(ctx._raw_message))
                    except Exception:
                        pass
                    # HANS_GUARD_SELF_TOPIC_V1 (7.9.) — DOTAZ NA HANSE SAMÉHO
                    # NEMÁ OPORU V ZÁPISCÍCH, ALE V SYSTEM PROMPTU.
                    # Doloženo 7. 9. testem: na "jaké filmy jste v poslední
                    # době viděl" guard zahodil větu "Poslední dobou jsem
                    # sledoval *For Your Eyes Only*" — a ta je PRAVDIVÁ,
                    # `kodi_playing` téhož dne v 09:54. Guard tedy zahodil
                    # pravdu a přilepil za zbytek popření, že ji Hans má.
                    # Reálný případ 5. 9. 21:37: na "zkus to jeste jednou"
                    # správně odpověděl, že maluje Auroru, a dostal za to
                    # tutéž frázi.
                    #
                    # Je to TÁŽ třída, kterou u brzdy A1 řeší
                    # HANS_A1_NOT_FOR_OWN_STATE_V1 (20.8.) — jeho komentář
                    # výslovně říká, že fakta o Hansovi a domě tečou z bloků
                    # system promptu (cap, self_state, přítomnost, kodi,
                    # počasí), kam guard NEVIDÍ; guard tam ale výjimku
                    # nedostal. Sdílíme tedy TÝŽ predikát `self_topic`,
                    # nevyrábíme druhý.
                    #
                    # Navazuje i na vlastní komentář hlásící větve (12.8.):
                    # "Guard stojí na předpokladu, že jde vyjmenovat všechno,
                    # co model dostal — a ten v téhle architektuře NEPLATÍ."
                    # Přesně proto se mimo tenkou cestu jen hlásí; tohle tutéž
                    # úvahu dotahuje na dotazy o Hansovi i NA tenké cestě.
                    #
                    # 📏 Změřeno na větách z testu 7. 9.: 5/5 falešných zásahů
                    # má self_topic='asistent'; kontrolní SVĚTOVÉ dotazy
                    # ("jake hrady jsou v ceskem raji?", "co vis o tom vraku
                    # u sicilie?") i "co to je karbunkule?" mají 'osoba',
                    # takže tudy guard běží dál a podtřídy (a) se to netýká.
                    _o_sobe = False
                    try:
                        from scripts.hans_intent import self_topic as _self_topic
                        _o_sobe = (_self_topic(ctx._raw_message, self.config)
                                   == 'asistent')
                    except Exception:
                        pass
                    if _dropped and _tenky and _uvaha:
                        logging.getLogger(__name__).info(
                            'HANS_REFLECTIVE_ASK_V1: guard NEZASAHUJE — '
                            'úvahová otázka (%d vět bez opory)', len(_dropped))
                    elif _dropped and _tenky and _o_sobe:
                        logging.getLogger(__name__).info(
                            'HANS_GUARD_SELF_TOPIC_V1: guard NEZASAHUJE — '
                            'dotaz na Hanse (opora je v system promptu, '
                            'ne v zápiscích; %d vět bez opory)', len(_dropped))
                    if (_dropped and _tenky and not _uvaha and not _o_sobe
                            and len(_dropped) >= _prah):
                        logging.getLogger(__name__).info(
                            'GROUNDING_GUARD_ACTIVE_V2: ZASAHUJI — %d vět bez '
                            'opory u tenkého podkladu (%s). Věty: %s',
                            len(_dropped), _cesta,
                            # GROUNDING_GUARD_LOG_ALL_V1 (14. 9.) — VŠECHNY, ne
                            # první. S jedinou větou nešlo poznat, jestli guard
                            # zahodil výmysl, nebo omluvu a zdvořilost (14. 9.:
                            # „Omlouvám se za mou zmatenost" jako první ze 7).
                            ' | '.join(repr(_d[:60]) for _d in _dropped[:7]))
                        # HANS_ANCHOR_LOOKUP_V1 (22.8.) — vykuchaná odpověď
                        # NENÍ konec. Když se ukázalo, že podklad tvrzení
                        # neunese, je to totéž jako „nemám záznam" — a na to
                        # už máme dohledání (HANS_INSTANT_LOOKUP_V1, 4.8.):
                        # článek TEĎ, do paměti až po nočním ověření. U hradu
                        # Kost se nikdy nespustilo právě proto, že ho předběhl
                        # tenký falešný podklad (odpověď se tvářila jako
                        # `grounded`), takže Hans k přiznání nedošel.
                        # HANS_LOOKUP_HAD_NOTES_V1 — sem se jde od ZÁPISKŮ,
                        # které nestačily; „nic jsem neměl" by byla nepravda.
                        # HANS_GUARD_QUOTE_NOTE_V1 (14. 9.) — napřed CITACE
                        # z vlastního zápisku, když věta na otázku sedí.
                        # Doloženo 14. 9.: na „proč má Trosky dvě věže?" měl
                        # Hans ve studijní poznámce „na dvou skalních věžích –
                        # Panna a Baba", model přesto vymyslel „Ptačí…", guard
                        # to správně zahodil a dohledané heslo o věžích mlčelo.
                        # Citace je doslovná → nic se nedomýšlí; když žádná věta
                        # nepřekročí práh, platí dohledání jako dosud.
                        _cit = self._citace_ze_zapisku(
                            _facts, getattr(self, '_f1_query', None) or ctx._raw_message)
                        if _cit:
                            logging.getLogger(__name__).info(
                                'HANS_GUARD_QUOTE_NOTE_V1: odpovídám citací ze '
                                'zápisku místo dohledání (%.60s)', _cit)
                            _dohl = ("Ve svých zápiscích k tomu mám tohle: "
                                     "\u201e%s\u201c Víc podrobností tam nemám "
                                     "a nerad bych si domýšlel." % _cit)
                        else:
                            _dohl = self._dohledej_kotvu(ctx._raw_message, ctx.name,
                                                         mel_zapisky=True)
                        ctx._dohledano = True
                        ctx.response = _dohl or _clean
                    elif _dropped:
                        # GROUNDING_GUARD_ACTIVE_V3 — do hlásícího logu i CESTA,
                        # ať se dá příště ladit z dat (dřív nešlo poznat, jestli
                        # hlášení přišlo z tenkého fallbacku, nebo z RAG).
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
                            'GROUNDING_GUARD_V1 [jen hlásím, cesta=%s]: %d vět bez opory '
                            'v podkladu. První: %r', _cesta or '?',
                            len(_dropped), _dropped[0][:80])
            except Exception as _gge:
                logging.getLogger(__name__).warning(
                    'GROUNDING_GUARD_V1 selhal (odpověď ponechána): %s', _gge)
            # HANS_ANCHOR_LOOKUP_ON_ADMIT_V1 (22.8.) — TŘETÍ spouštěč
            # dohledání: neznalost přizná SÁM MODEL uvnitř odpovědi.
            # Doloženo zkoušením (15:12): „potřebuji více informací o Scott
            # Eastwood" → A1 vyšla `stabilni` (sim 0.857 vs práh 0.85),
            # grounding `factual_nofacts`, takže ani jeden z dosavadních dvou
            # spouštěčů (A1 abstinence, vykuchání guardem) nenastal. Hans
            # slíbil „zkusím si to ověřit" a neověřil nic, ačkoli heslo na
            # Wikipedii je. Tohle ten slib plní.
            # Úzké schválně: (a) v tomhle tahu se ještě nedohledávalo,
            # (b) šlo o FAKTICKÝ dotaz BEZ podkladu (`factual_nofacts`) — u
            # `grounded` by se přepisovala odpověď, která oporu má, u
            # `self_state`/`opinion` se na Wikipedii nemá co hledat,
            # (c) odpověď je CELÁ jen přiznáním (`je_ciste_odrikani`).
            # Brány se drží FAKTICKÉ cesty: `self_state` (o sobě samém),
            # `opinion` a `nonfactual` se na Wikipedii dohledávat nemají.
            # `grounded` naopak ANO — doloženo 16:28, kdy týž dotaz dostal
            # „oporu" z nesouvisejících chunků (0.651) a výsledek byl přesto
            # bez obsahu; a když je odpověď CELÁ jen přiznáním, není co ztratit.
            try:
                # HANS_A1_OWN_WORK_IN_PROMPT_V1 — na vlastní dílo se na Wikipedii
                # nedohledává (1. 10.: „proč sis vybral Bacha“ → model poctivě
                # přiznal, že důvod nemá zapsaný, a odpověď přepsal článek o Bachovi)
                if (not ctx._dohledano
                        and not getattr(ctx, '_vlastni_dilo', False)
                        and getattr(self, '_grounding_outcome', '')
                        in ('factual_nofacts', 'grounded')):
                    from scripts.hans_thread import je_ciste_odrikani
                    if je_ciste_odrikani(ctx.response):
                        _dohl2 = self._dohledej_kotvu(ctx._raw_message, ctx.name)
                        if _dohl2:
                            logging.getLogger(__name__).info(
                                'HANS_ANCHOR_LOOKUP_ON_ADMIT_V1: přiznal '
                                'neznalost sám → dohledáno (%.60s)',
                                ctx._raw_message or '')
                            ctx.response = _dohl2
                        else:
                            logging.getLogger(__name__).info(
                                'HANS_ANCHOR_LOOKUP_ON_ADMIT_V1: přiznání bez '
                                'dohledání (platí původní odpověď)')
            except Exception as _aloe:
                logging.getLogger(__name__).warning(
                    'HANS_ANCHOR_LOOKUP_ON_ADMIT_V1 selhalo: %s', _aloe)
            try:
                from scripts.conversation_store import dedup_address_g4d
                ctx.response = dedup_address_g4d(ctx.response, ctx.name, self.config)
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
                ctx.response, _nfix = fix_addressee(ctx.response, ctx.name, self.config)
                if _nfix:
                    logging.getLogger(__name__).info(
                        "HANS_ADDRESSEE_V2: opraveno %d cizích oslovení "
                        "(partner=%s)", _nfix, ctx.name)
            except Exception:
                pass
            # HANS_WEEKDAY_FIX_V1 (14. 9.) — den v tydnu vedle dneska opravi
            # program, ne model (zive: „Dnes je ctvrtek" v pondeli). Bezi PRED
            # zapisem do conv_store/deniku/RAG, stejne jako fix_addressee.
            try:
                from scripts.cz_names import fix_weekday
                ctx.response, _nden = fix_weekday(ctx.response)
                if _nden:
                    logging.getLogger(__name__).info(
                        "HANS_WEEKDAY_FIX_V1: opraven den v tydnu (%d×)", _nden)
            except Exception:
                pass
            # HANS_GREETING_OUTPUT_TRIM_V1 (19. 9.) — nadbytecny pozdrav
            # v NAVAZUJICI replice. Vstupni cisteni okna (`_orez_pozdravy`)
            # na to NESTACI: meni jen pohled do promptu, ne odpoved —
            # zmereno, ze cetnost nesnizilo (61 % -> 68,3 %).
            # Bezi PRED zapisem do conv_store/deniku/RAG, jako sousedi vys.
            # ⚠️ Prvni replika rozhovoru se NEDOTYKA (pozdrav je tam
            # legitimni: 85 z 92 prvnich replik ho ma). Kdyz by po orezu
            # zbylo prazdno, replika zustava beze zmeny (u 89 z 218 je
            # pozdrav CELA replika — tam orez nema co delat).
            try:
                _gcfg = (self.config.get("openwebui_chat", {}) or {})
                _mez = float(_gcfg.get("greeting_trim_gap_s", 21600))
                _posl = self.conv_store.posledni_ts(ctx.name)
                # HANS_GREETING_OUTPUT_TRIM_V2 — kdyz clovek SAM pozdravil,
                # je pozdrav v odpovedi legitimni (zmereno: 5 z 218
                # neprvnich replik; uzivatel zdravi ve 12 ze 149 zprav).
                _clovek_pozdravil = bool(
                    _POZDRAV_UZIVATEL_RE.search((ctx._raw_message or "").strip()))
                if (_posl and (time.time() - _posl) < _mez
                        and not _clovek_pozdravil):
                    from scripts.conversation_store import ConversationStore as _CS
                    _bez = _CS._POZDRAV_RE.sub("", ctx.response, count=1)
                    if _bez.strip() and _bez != ctx.response:
                        logging.getLogger(__name__).info(
                            "HANS_GREETING_OUTPUT_TRIM_V1: odriznut nadbytecny "
                            "pozdrav (rozhovor bezi %.0f min)",
                            (time.time() - _posl) / 60.0)
                        ctx.response = _bez
            except Exception as _gte:
                logging.getLogger(__name__).debug(
                    "HANS_GREETING_OUTPUT_TRIM_V1: %s", _gte)

    def _sc_overeni_tvrzeni(self, ctx):
        # HANS_CLAIM_CHECK_V1 (28. 9.) — úvahová odpověď bez opory, která ale
        # tvrdí něco o světě (jména, díla, letopočty) → slib nočního ověření
        # + řádek do čekárny (zapíše se níž, až bude znát id záznamu v RAG).
        ctx._claim_names = []
        if (ctx.response and getattr(self, '_grounding_cesta', '') == 'uvahova_otazka'
                and not self._skip_memory(ctx.name)
                and (self.config.get("instant_lookup", {}) or {}).get(
                    "claim_check", True)):
            try:
                from scripts.hans_findings import claim_names, CLAIM_NOTE
                from scripts.cz_names import vocative as _voc, _known_person_forms
                from scripts.hans_persona import persona_name as _pn
                _vyn = {_pn(self.config), ctx.name, _voc(ctx.name)} | set(
                    _known_person_forms(self.config))
                ctx._claim_names = claim_names(ctx.response, _vyn)
                if ctx._claim_names:
                    ctx.response = ctx.response.rstrip() + "\n\n" + CLAIM_NOTE
                    logging.getLogger(__name__).info(
                        'HANS_CLAIM_CHECK_V1: úvaha s tvrzeními %s → v noci ověřím',
                        ctx._claim_names[:6])
            except Exception as _cce:
                logging.getLogger(__name__).debug('HANS_CLAIM_CHECK_V1: %s', _cce)
                ctx._claim_names = []

    def _sc_ulozeni(self, ctx):
        if ctx.response:
            self.conv_store.add_exchange(ctx.name, ctx._raw_message, ctx.response, channel=ctx.channel)
            # # HUMAN_CHAT_VIA_LOG_ENTRY
            # Vztahové karty + paměť — zaloguj exchange do deníku jako
            # human_chat. Přes _log_entry → spustí synthesis_hooks
            # → vytvoří chat_reflection → upload do hans_identita RAG.
            _note = f"{ctx.name}: {ctx._raw_message}\nHans: {ctx.response}"
            # HANS_TEST_PERSON_V1 — u testovací identity se přeskočí OBOJÍ:
            # deník i RAG. ⚠️ NESTAČÍ vynulovat `_hi_log` — tím by se naopak
            # spustila záložní SQL větev níž a řádek by se zapsal stejně.
            _skip_mem = self._skip_memory(ctx.name)          # HANS_VOICE_NO_MEMORY_V1
            _hi_log = None if _skip_mem else getattr(self, "_hans_idle", None)
            if _hi_log and hasattr(_hi_log, "_log_entry"):
                try:
                    _hi_log._log_entry("human_chat", ctx.name, note=_note)
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
                            (_t.time(), "human_chat", ctx.name, _note)
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
                    _chatlog = self._upload_chat_memory(ctx.name, ctx._raw_message, ctx.response)
                    if ctx._claim_names:                    # HANS_CLAIM_CHECK_V1
                        from scripts.hans_findings import add_claim_check
                        add_claim_check(
                            self.config.get("diary_db", "data/hans_diary.db"),
                            asker=ctx.name, query=ctx._raw_message, answer=ctx.response,
                            names=ctx._claim_names, chatlog_id=_chatlog or "")
            except Exception as _e:
                print(f"[Chat] chat memory upload failed: {_e}")


    def _upload_chat_memory(self, name: str, question: str, answer: str):
        """HANS_CHAT_RECALL_V1 — verbatim rozhovor do RAG (hans_pripady), aby byl
        později sémanticky dohledatelný. Threadovaně, deferral-safe."""
        _kn = getattr(self, "knowledge", None)
        if _kn is None or not (question or "").strip():
            return
        import threading as _th
        import time as _t
        ts = _t.time()           # HANS_CLAIM_CHECK_V1 — id záznamu známé hned
        doc_id = f"chatlog_{int(ts)}_{name}"

        def _work():
            try:
                from scripts.hans_persona import persona_name
                pname = persona_name(self.config)
            except Exception:
                pname = "Hans"
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
                    doc_id=doc_id,
                    title=f"Rozhovor s {name}: {question.strip()[:60]}",
                    text=text,
                    metadata={"kdy": when, "osoba": name, "typ": "rozhovor",
                              # HANS_CHATLOG_NOT_FACT_V1
                              "overeno": False, "puvod": "vlastni_vyrok"})
            except Exception as _e:
                print(f"[Chat] chat memory upload (worker): {_e}")
        _th.Thread(target=_work, daemon=True, name="ChatMemoryUpload").start()
        return doc_id

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

    def _trace(self, t0, volajici, status=0, url=None):
        """HANS_LLM_TRACE_V1 — best-effort zápis do data/mereni/llm_calls.log.

        `url` se predava VYSLOVNE: keepalive nejde na `chat_endpoint`, ale na
        `{base}/api/generate`, a sloupec, ktery by hlasil neco jineho, nez kam
        se opravdu slo, by pri diagnostice svedl stejne jako kdysi hlaska
        o READ mezi u ConnectTimeoutu (OLLAMA_CONNECT_TIMEOUT_LOG_V1)."""
        try:
            from scripts.llm_trace import zapis as _z
            _z(getattr(self, "model_name", "?"),
               url if url is not None else getattr(self, "chat_endpoint", ""),
               time.time() - t0, "http_%s" % status,
               volajici="openwebui_direct_handler:%s" % volajici)
        except Exception:
            pass

    # ── Helpers ───────────────────────────────────────────────────────────────

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
