"""
config_schema.py — JEDEN zdroj pravdy pro editovatelný config.

Obě UI generují svá pole z tohoto schématu místo ručního drátování:
  - klávesa S ve video streamu (Tkinter ConfigGUI)  → scripts/config_gui_tabs._tab_schema
  - webadmin                                         → /api/config/schema + renderSchema() v index.html

Přidání pole = jeden záznam tady → objeví se v OBOU UI naráz (konec driftu).

Úrovně (tier):
  "basic"  — věci, co se reálně ladí; vždy viditelné.
  "expert" — detaily, sahá se zřídka; schované pod rozbalovací sekcí "Expert".
  (co tu NENÍ uvedeno = přes UI se needituje; mění se přímo v config.json)

Typy polí: "bool" | "int" | "float" | "text" | "textarea" | "choice"
  int/float  → min, max, step
  choice     → choices: [...]
  textarea   → rows (výška)

PILOT: zatím jen skupina 'identity' (persona.*). Další sekce se migrují
postupně z natvrdo psaných tabů (config_gui_tabs.py) a HTML (index.html).
"""

# ── Schéma ──────────────────────────────────────────────────────────────────

GROUPS = [
    {
        "id": "identity",
        "title": "Hans / Identita",
        "icon": "🎩",
        "intro": (
            "Hansova osobnost. Jádro (CORE) spravuje rozhodovací engine Severka — "
            "ruční úpravy ber jako výjimku (přepíšou i to, co Severka navrhla). "
            "Stav a historii verzí viz /severka."
        ),
        "fields": [
            {
                "path": "persona.core",
                "label": "Identita (CORE)",
                "type": "textarea",
                "tier": "basic",
                "rows": 4,
                "restart": False,
                "managed_by": "severka",
                "tip": ("Jádro osobnosti — kdo Hans je. Mění ho Severka; schvaluj "
                        "přes /severka. Projeví se hned (persona čte config za běhu)."),
            },
            {
                "path": "persona.language_rules",
                "label": "Jazyková pravidla",
                "type": "textarea",
                "tier": "basic",
                "rows": 3,
                "restart": False,
                "tip": "Jak Hans mluví — jazyk, zákaz emoji/Unicode, mluvnický rod.",
            },
            {
                "path": "persona.interests_seed",
                "label": "Počáteční zájmy (seed)",
                "type": "textarea",
                "tier": "basic",
                "rows": 3,
                "restart": False,
                "tip": ("Výchozí zájmy do persony. Živé koníčky si Hans destiluje "
                        "sám (vrstva koníčků) — tohle je jen startovní seed."),
            },
            {
                "path": "persona.address_rules",
                "label": "Oslovování (vokativ)",
                "type": "textarea",
                "tier": "expert",
                "rows": 3,
                "restart": False,
                "tip": "Pravidla vokativu při oslovení (Stando/Jano, ne nominativ).",
            },
        ],
    },

    # ── Vlna 1a — Hansova duše ────────────────────────────────────────────────
    {
        "id": "hans_dialog",
        "title": "Hans / Dialog",
        "icon": "🗣",
        "intro": "Osobnosti a pravidla vnitřního dialogu Hans ↔ Koláč (medvídek).",
        "fields": [
            {"path": "hans_dialog.hans_personality", "label": "Hans — osobnost", "type": "textarea", "rows": 3,
             "tip": "Jak vystupuje Hans v dialogu s Koláčem."},
            {"path": "hans_dialog.kolac_personality", "label": "Koláč — osobnost", "type": "textarea", "rows": 4,
             "tip": "Osobnost medvídka Koláče (dialogový partner)."},
            {"path": "hans_dialog.hans_interests", "label": "Hans — zájmy", "type": "textarea", "rows": 2},
            {"path": "hans_dialog.kolac_interests", "label": "Koláč — zájmy", "type": "textarea", "rows": 2},
            {"path": "hans_dialog.dialog_rules", "label": "Pravidla dialogu", "type": "textarea", "rows": 3},
            {"path": "hans_dialog.dialog_language", "label": "Jazyk dialogu", "type": "textarea", "rows": 2},
            {"path": "hans_dialog.ollama_model", "label": "Ollama model dialogu", "type": "text", "tier": "expert",
             "tip": "Model pro generování dialogových replik."},
            {"path": "hans_dialog.topic_min_turns", "label": "Min. tahů na téma", "type": "int", "tier": "expert", "min": 1, "max": 12},
            {"path": "hans_dialog.topic_max_turns", "label": "Max. tahů na téma", "type": "int", "tier": "expert", "min": 1, "max": 20},
            {"path": "hans_dialog.toaster_mode", "label": "Svítorka mod — Koláč nabízí pečivo a nedá se odradit", "type": "bool",
             "tip": ("Inspirováno inteligentním toustovačem z Červeného trpaslíka. Koláč začne v každém "
                     "dialogu nabízet tousty, muffiny a vafle. Hans se marně snaží mluvit o něčem jiném.")},
        ],
    },
    {
        "id": "hans_models",
        "title": "Hans / Modely",
        "icon": "🧠",
        "intro": ("Které Ollama modely Hans používá. Split osobnost (finetune hans-czech) "
                  "vs analytický mozek (base OpenEuroLLM) — base je čistší na extrakci/Severku."),
        "fields": [
            {"path": "models.dialog", "label": "Model — dialog/osobnost", "type": "text",
             "tip": "Persona finetune (hans-czech) pro chat a dialog."},
            {"path": "models.utility", "label": "Model — utility", "type": "text"},
            {"path": "models.voice", "label": "Model — hlas", "type": "text"},
            {"path": "evening_reflection.model", "label": "Model — večerní reflexe / extrakce postojů", "type": "text",
             "tip": "Analytická extrakce stances. Doporučeno base (OpenEuroLLM), ne finetune — finetune konfabuluje."},
        ],
    },
    {
        "id": "hans_activity",
        "title": "Hans / Aktivita",
        "icon": "⏱",
        "intro": "Kdy a jak často je Hans aktivní v nečinnosti (idle dialogy, rutina).",
        "fields": [
            {"path": "hans_routine.enabled", "label": "Rutina aktivní", "type": "bool",
             "tip": "Noční reflexe, destilace, Severka check, denní fáze."},
            {"path": "hans_idle.check_interval_s", "label": "Interval kontroly (s)", "type": "int", "min": 5, "max": 300, "step": 5},
            {"path": "hans_idle.idle_timeout_min", "label": "Idle timeout (min)", "type": "int", "min": 1, "max": 60},
            {"path": "hans_idle.dialog_interval_min", "label": "Interval idle dialogu (min)", "type": "int", "min": 1, "max": 60,
             "tip": "Jak často Hans↔Koláč dialog při nečinnosti. (Dočasně 5 kvůli sběru dat — kandidát na revert na 20.)"},
            {"path": "hans_idle.force_teddy_visible", "label": "Vynutit viditelnost Koláče", "type": "bool", "tier": "expert"},
            {"path": "hans_idle.quantum_kolac", "label": "Kvantový Koláč", "type": "bool", "tier": "expert"},
            {"path": "hans_idle.dialog_only_when_idle", "label": "Dialog jen při idle", "type": "bool", "tier": "expert"},
        ],
    },
    {
        "id": "room_observer",
        "title": "Hans / Pozorování",
        "icon": "👁",
        "intro": "Periodický popis místnosti (vision model) — vstup do Hansova vnímání.",
        "fields": [
            {"path": "room_observer.enabled", "label": "Pozorování aktivní", "type": "bool"},
            {"path": "room_observer.interval_hours", "label": "Interval (hodiny)", "type": "int", "min": 1, "max": 24},
            {"path": "room_observer.model", "label": "Vision model", "type": "text"},
            {"path": "room_observer.prompt", "label": "Prompt popisu", "type": "textarea", "rows": 3, "tier": "expert"},
            {"path": "room_observer.translate", "label": "Překládat popis", "type": "bool", "tier": "expert"},
        ],
    },
    {
        "id": "relationships",
        "title": "Hans / Vztahy",
        "icon": "🤝",
        "intro": "Vztahové karty osob — jak často Hans přehodnocuje charakteristiku lidí.",
        "fields": [
            {"path": "relationships.enabled", "label": "Vztahy aktivní", "type": "bool"},
            {"path": "relationships.reflection_interval_days", "label": "Interval reflexe (dny)", "type": "int", "min": 1, "max": 30,
             "tip": "Po kolika dnech přehodnotit charakteristiku osoby."},
            {"path": "relationships.sighting_throttle_s", "label": "Throttle spatření (s)", "type": "int", "min": 0, "max": 600, "tier": "expert"},
        ],
    },
    {
        "id": "hans_library",
        "title": "Hans / Četba",
        "icon": "📚",
        "intro": "Čtenářský program (knihy = růstový motor charakteru).",
        "fields": [
            {"path": "hans_library.enabled", "label": "Četba aktivní", "type": "bool"},
        ],
    },
    {
        "id": "hans_questions",
        "title": "Hans / Otázky",
        "icon": "❓",
        "intro": "Fronta Hansových dotazů obyvatelům (limity a pravděpodobnosti). Vše laděné = expert.",
        "fields": [
            {"path": "hans_questions.expires_days", "label": "Expirace otázky (dny)", "type": "int", "min": 1, "max": 90, "tier": "expert"},
            {"path": "hans_questions.max_pending_per_person", "label": "Max. čekajících / osoba", "type": "int", "min": 1, "max": 20, "tier": "expert"},
            {"path": "hans_questions.max_new_per_day_per_source", "label": "Max. nových / den / zdroj", "type": "int", "min": 1, "max": 20, "tier": "expert"},
            {"path": "hans_questions.min_age_before_voice_h", "label": "Min. stáří před hlasem (h)", "type": "int", "min": 0, "max": 72, "tier": "expert"},
            {"path": "hans_questions.voice_ask_probability", "label": "Pravděpodobnost dotazu hlasem", "type": "float", "min": 0.0, "max": 1.0, "step": 0.05, "tier": "expert"},
        ],
    },
    {
        "id": "kolac_cases",
        "title": "Hans / Kauzy",
        "icon": "🔍",
        "intro": "Detektivní kauzy Koláče (záhady, které Hans řeší ze stop).",
        "fields": [
            {"path": "kolac_cases.enabled", "label": "Kauzy aktivní", "type": "bool"},
            {"path": "kolac_cases.min_days", "label": "Min. délka kauzy (dny)", "type": "int", "min": 1, "max": 30, "tier": "expert"},
            {"path": "kolac_cases.max_days", "label": "Max. délka kauzy (dny)", "type": "int", "min": 1, "max": 60, "tier": "expert"},
            {"path": "kolac_cases.max_active_cases", "label": "Max. aktivních kauz", "type": "int", "min": 1, "max": 10, "tier": "expert"},
        ],
    },

    # ── Vlna 2 — Chat / LLM konektivita ───────────────────────────────────────
    {
        "id": "chat_direct",
        "title": "Chat / Připojení",
        "icon": "💬",
        "intro": "Hlavní chat backend (OpenWebUI direct) — připojení Hanse k LLM pro rozhovor s lidmi.",
        "fields": [
            {"path": "openwebui_direct.enabled", "label": "Aktivní", "type": "bool"},
            {"path": "openwebui_direct.base_url", "label": "Base URL", "type": "text"},
            {"path": "openwebui_direct.model", "label": "Model (embedding/chat)", "type": "text"},
            {"path": "openwebui_direct.api_token", "label": "API token", "type": "text",
             "tip": "Přístupový token OpenWebUI."},
            {"path": "openwebui_direct.synthesis_model", "label": "Syntéza model", "type": "text",
             "tip": "Model pro finální generování odpovědi (persona — hans-czech)."},
            {"path": "openwebui_direct.greeting_mode", "label": "Režim pozdravu", "type": "choice",
             "choices": ["once_per_session", "once_per_day"], "tier": "expert"},
            {"path": "openwebui_direct.min_confidence", "label": "Min. jistota rozpoznání", "type": "float",
             "min": 0.0, "max": 1.0, "step": 0.05, "tier": "expert"},
            {"path": "openwebui_direct.popup_enabled", "label": "Popup chat okno", "type": "bool", "tier": "expert"},
            {"path": "openwebui_direct.username", "label": "Uživatel", "type": "text", "tier": "expert"},
            {"path": "openwebui_direct.password", "label": "Heslo", "type": "text", "tier": "expert"},
        ],
    },
    {
        "id": "chat_openwebui",
        "title": "Chat / OpenWebUI (alt)",
        "icon": "💬",
        "intro": "Alternativní OpenWebUI chat vrstva (greeting/popup). Nested popup_chat zatím přes legacy záložku.",
        "fields": [
            {"path": "openwebui_chat.enabled", "label": "Aktivní", "type": "bool"},
            {"path": "openwebui_chat.base_url", "label": "Base URL", "type": "text"},
            {"path": "openwebui_chat.model_name", "label": "Model", "type": "text"},
            {"path": "openwebui_chat.greeting_enabled", "label": "Pozdrav aktivní", "type": "bool"},
            {"path": "openwebui_chat.greeting_mode", "label": "Režim pozdravu", "type": "choice",
             "choices": ["once_per_session", "once_per_day"], "tier": "expert"},
            {"path": "openwebui_chat.popup_enabled", "label": "Popup chat okno", "type": "bool", "tier": "expert"},
            {"path": "openwebui_chat.request_timeout", "label": "Timeout požadavku (s)", "type": "int",
             "min": 5, "max": 180, "step": 5, "tier": "expert"},
            {"path": "openwebui_chat.min_confidence", "label": "Min. jistota rozpoznání", "type": "float",
             "min": 0.0, "max": 1.0, "step": 0.05, "tier": "expert"},
            {"path": "openwebui_chat.history_max_messages", "label": "Max. zpráv historie", "type": "int",
             "min": 1, "max": 50, "tier": "expert"},
        ],
    },
    {
        "id": "chat_cloud",
        "title": "Chat / Cloud fallback",
        "icon": "☁",
        "intro": "Cloudové LLM jako záloha při výpadku lokální Ollamy (OpenRouter primárně, Gemini legacy).",
        "fields": [
            {"path": "openrouter.enabled", "label": "OpenRouter aktivní", "type": "bool"},
            {"path": "openrouter.api_key", "label": "OpenRouter API klíč", "type": "text"},
            {"path": "openrouter.model", "label": "OpenRouter model", "type": "text"},
            {"path": "gemini.api_key", "label": "Gemini API klíč", "type": "text", "tier": "expert"},
            {"path": "gemini.model", "label": "Gemini model", "type": "text", "tier": "expert"},
        ],
    },

    # ── Vlna 3 — Hlas & zvuk ──────────────────────────────────────────────────
    {
        "id": "tts",
        "title": "TTS (řeč)",
        "icon": "🔊",
        "intro": "Syntéza řeči — jak Hans mluví nahlas.",
        "fields": [
            {"path": "tts.enabled", "label": "TTS aktivní", "type": "bool"},
            {"path": "tts.voice", "label": "Hlas", "type": "text", "tip": "Název hlasu (např. cs-CZ-AntoninNeural)."},
            {"path": "tts.volume", "label": "Hlasitost", "type": "int", "min": 0, "max": 100},
            {"path": "tts.cache_enabled", "label": "Cache řeči", "type": "bool"},
            {"path": "tts.max_length", "label": "Max. délka (znaků)", "type": "int", "min": 100, "max": 1000, "tier": "expert"},
            {"path": "tts.alsa_device", "label": "ALSA výstup", "type": "text", "tier": "expert"},
            {"path": "tts.alsa_control", "label": "ALSA mixer", "type": "text", "tier": "expert"},
        ],
    },
    {
        "id": "voice",
        "title": "Voice (poslech)",
        "icon": "🎙",
        "intro": "Rozpoznávání řeči (STT) a hlasová aktivace — jak Hans poslouchá.",
        "fields": [
            {"path": "voice.enabled", "label": "Voice aktivní", "type": "bool"},
            {"path": "voice.stt_url", "label": "STT URL", "type": "text"},
            {"path": "voice.default_speaker", "label": "Výchozí mluvčí", "type": "text"},
            {"path": "voice.vad_aggressiveness", "label": "Agresivita VAD", "type": "int", "min": 0, "max": 3,
             "tip": "Detekce řeči: 0=citlivá, 3=přísná."},
            {"path": "voice.max_speech_seconds", "label": "Max. délka řeči (s)", "type": "int", "min": 1, "max": 60},
            {"path": "voice.silence_seconds", "label": "Ticho do konce (s)", "type": "int", "min": 1, "max": 10},
            {"path": "voice.stt_token", "label": "STT token", "type": "text", "tier": "expert"},
            {"path": "voice.min_speech_seconds", "label": "Min. délka řeči (s)", "type": "float", "min": 0.0, "max": 5.0, "step": 0.1, "tier": "expert"},
            {"path": "voice.wake_chunk_seconds", "label": "Wake chunk (s)", "type": "float", "min": 0.5, "max": 5.0, "step": 0.5, "tier": "expert"},
            {"path": "voice.min_recording_seconds", "label": "Min. nahrávka (s)", "type": "float", "min": 0.5, "max": 10.0, "step": 0.5, "tier": "expert"},
        ],
    },

    # ── Vlna 4 — Rozpoznávání obličejů ────────────────────────────────────────
    {
        "id": "recognition",
        "title": "Rozpoznávání — prahy",
        "icon": "🔍",
        "intro": "ArcFace prahy a EMA vyhlazování — jádro rozhodování kdo je kdo. Basic = co se reálně ladí.",
        "fields": [
            {"path": "recognition_tuning.arcface_thresh", "label": "ArcFace práh", "type": "float", "min": 0.30, "max": 0.90, "step": 0.01,
             "tip": "Min. cosine podobnost pro přijetí shody."},
            {"path": "recognition_tuning.margin", "label": "Margin", "type": "float", "min": 0.02, "max": 0.20, "step": 0.01,
             "tip": "Min. odstup 1. a 2. nejlepšího skóre."},
            {"path": "recognition_tuning.min_norm", "label": "Min. norma (kvalita)", "type": "float", "min": 0.10, "max": 0.80, "step": 0.01},
            {"path": "recognition_tuning.ema_alpha", "label": "EMA alpha", "type": "float", "min": 0.05, "max": 1.00, "step": 0.01},
            {"path": "recognition_tuning.ema_thresh", "label": "EMA práh", "type": "float", "min": 0.20, "max": 0.80, "step": 0.01},
            {"path": "recognition_tuning.ema_margin", "label": "EMA margin", "type": "float", "min": 0.01, "max": 0.15, "step": 0.01},
            {"path": "recognition_tuning.iou_match", "label": "IoU shoda", "type": "float", "min": 0.10, "max": 0.70, "step": 0.01},
            {"path": "recognition.recognition_threshold", "label": "Cosine práh (legacy/cluster)", "type": "float", "min": 0.0, "max": 1.0, "step": 0.01,
             "tip": "Starší cosine práh — cluster fallback."},
            {"path": "recognition.auto_enrollment", "label": "Auto cluster-enroll", "type": "bool",
             "tip": "Automatické přidávání clusterů ke známým osobám."},
            {"path": "recognition_tuning.ema_headroom", "label": "EMA headroom", "type": "float", "min": 0.0, "max": 0.10, "step": 0.01, "tier": "expert"},
            {"path": "recognition_tuning.max_samples_per_person", "label": "Max. vzorků / osoba", "type": "int", "min": 10, "max": 200, "tier": "expert"},
            {"path": "recognition_tuning.min_sample_diversity", "label": "Min. diverzita vzorků", "type": "float", "min": 0.0, "max": 1.0, "step": 0.05, "tier": "expert"},
            {"path": "recognition_tuning.track_stale_s", "label": "Track stale (s)", "type": "int", "min": 1, "max": 30, "tier": "expert"},
            {"path": "recognition_tuning.decision_after", "label": "Rozhodnout po (snímcích)", "type": "int", "min": 1, "max": 20, "tier": "expert"},
            {"path": "recognition_tuning.max_track_emb", "label": "Max. track embeddingů", "type": "int", "min": 5, "max": 50, "tier": "expert"},
            {"path": "recognition_tuning.max_clusters", "label": "Max. clusterů", "type": "int", "min": 1, "max": 20, "tier": "expert"},
            {"path": "recognition_tuning.cluster_thresh", "label": "Cluster práh", "type": "float", "min": 0.05, "max": 0.60, "step": 0.01, "tier": "expert"},
            {"path": "recognition_tuning.ema_decay_factor", "label": "EMA decay", "type": "float", "min": 0.0, "max": 1.0, "step": 0.05, "tier": "expert"},
            {"path": "recognition.strict_mode_threshold", "label": "Strict práh", "type": "float", "min": 0.0, "max": 1.0, "step": 0.01, "tier": "expert"},
            {"path": "recognition.min_feature_similarity", "label": "Min. feature podobnost", "type": "float", "min": 0.0, "max": 1.0, "step": 0.01, "tier": "expert"},
            {"path": "recognition.caching_enabled", "label": "Cache rozpoznávání", "type": "bool", "tier": "expert"},
        ],
    },
    {
        "id": "face_preprocess",
        "title": "Předzpracování obličeje",
        "icon": "🖼",
        "intro": "CLAHE / gamma úpravy před rozpoznáním — pomáhají při špatném světle.",
        "fields": [
            {"path": "face_preprocess.enabled", "label": "Předzpracování aktivní", "type": "bool"},
            {"path": "face_preprocess.clahe_lores", "label": "CLAHE na lores", "type": "bool"},
            {"path": "face_preprocess.clahe_clip", "label": "CLAHE clip", "type": "float", "min": 0.5, "max": 4.0, "step": 0.1},
            {"path": "face_preprocess.clahe_tile", "label": "CLAHE tile", "type": "int", "min": 4, "max": 32},
            {"path": "face_preprocess.lores_threshold", "label": "Lores práh jasu", "type": "int", "min": 0, "max": 255},
            {"path": "face_preprocess.gamma_crop", "label": "Gamma na crop", "type": "bool"},
            {"path": "face_preprocess.gamma_auto", "label": "Gamma auto", "type": "bool"},
            {"path": "face_preprocess.gamma_fixed", "label": "Gamma pevná", "type": "float", "min": 0.5, "max": 3.0, "step": 0.1},
            {"path": "face_preprocess.gamma_threshold", "label": "Gamma práh jasu", "type": "int", "min": 0, "max": 255},
            {"path": "face_preprocess.clahe_crop", "label": "CLAHE na crop", "type": "bool", "tier": "expert"},
            {"path": "face_preprocess.clahe_crop_clip", "label": "CLAHE crop clip", "type": "float", "min": 0.5, "max": 4.0, "step": 0.1, "tier": "expert"},
        ],
    },
    {
        "id": "face_quality",
        "title": "Kvalita & zápis obličejů",
        "icon": "🎚",
        "intro": "Výběr nejlepšího obličeje a parametry zápisu (enrollment).",
        "fields": [
            {"path": "enrollment.enrollment_frames", "label": "Snímků při zápisu", "type": "int", "min": 5, "max": 60,
             "tip": "Kolik vzorků se sebere na osobu."},
            {"path": "face_processing.max_faces", "label": "Max. obličejů naráz", "type": "int", "min": 1, "max": 10},
            {"path": "face_processing.min_quality_threshold", "label": "Min. kvalita", "type": "float", "min": 0.0, "max": 1.0, "step": 0.05},
            {"path": "face_processing.size_ratio_min", "label": "Min. poměr velikosti", "type": "float", "min": 0.0, "max": 0.1, "step": 0.001, "tier": "expert"},
            {"path": "face_processing.quality_size_weight", "label": "Váha velikosti", "type": "float", "min": 0.0, "max": 1.0, "step": 0.05, "tier": "expert"},
            {"path": "face_processing.quality_centering_weight", "label": "Váha vycentrování", "type": "float", "min": 0.0, "max": 1.0, "step": 0.05, "tier": "expert"},
            {"path": "async_recognizer.min_face_area", "label": "Async min. plocha", "type": "float", "min": 0.0, "max": 0.1, "step": 0.005, "tier": "expert"},
        ],
    },
    {
        "id": "unknown_enrollment",
        "title": "Auto-zápis neznámých",
        "icon": "➕",
        "intro": "Automatické zachycení neznámých obličejů k pozdějšímu zápisu.",
        "fields": [
            {"path": "unknown_enrollment.enabled", "label": "Aktivní", "type": "bool"},
            {"path": "unknown_enrollment.target_count", "label": "Cílový počet vzorků", "type": "int", "min": 10, "max": 200},
            {"path": "unknown_enrollment.min_face_area", "label": "Min. plocha obličeje", "type": "float", "min": 0.0, "max": 0.2, "step": 0.005},
            {"path": "unknown_enrollment.open_after", "label": "Otevřít po (vzorcích)", "type": "int", "min": 1, "max": 20},
            {"path": "unknown_enrollment.cooldown_s", "label": "Cooldown (s)", "type": "int", "min": 0, "max": 600, "tier": "expert"},
            {"path": "unknown_enrollment.capture_interval_s", "label": "Interval zachycení (s)", "type": "float", "min": 0.1, "max": 5.0, "step": 0.1, "tier": "expert"},
        ],
    },
    {
        "id": "unknown_tracker",
        "title": "Sledování neznámých",
        "icon": "👤",
        "intro": "Trvalé sledování neznámých tváří napříč snímky (gate proti šumu).",
        "fields": [
            {"path": "unknown_tracker.enabled", "label": "Aktivní", "type": "bool"},
            {"path": "unknown_tracker.target_count", "label": "Cílový počet vzorků", "type": "int", "min": 1, "max": 100},
            {"path": "unknown_tracker.min_face_area", "label": "Min. plocha obličeje", "type": "float", "min": 0.0, "max": 0.3, "step": 0.005},
            {"path": "unknown_tracker.max_unknowns", "label": "Max. neznámých", "type": "int", "min": 1, "max": 50, "tier": "expert"},
            {"path": "unknown_tracker.match_thresh", "label": "Práh shody", "type": "float", "min": 0.0, "max": 1.0, "step": 0.01, "tier": "expert"},
            {"path": "unknown_tracker.cooldown_s", "label": "Cooldown (s)", "type": "int", "min": 0, "max": 600, "tier": "expert"},
            {"path": "unknown_tracker.capture_interval_s", "label": "Interval zachycení (s)", "type": "int", "min": 1, "max": 30, "tier": "expert"},
            {"path": "unknown_tracker.open_after", "label": "Otevřít po (vzorcích)", "type": "int", "min": 1, "max": 30, "tier": "expert"},
            {"path": "unknown_tracker.stale_timeout_min", "label": "Stale timeout (min)", "type": "int", "min": 1, "max": 120, "tier": "expert"},
            {"path": "unknown_tracker.gate_min_size_px", "label": "Gate min. velikost (px)", "type": "int", "min": 0, "max": 200, "tier": "expert"},
            {"path": "unknown_tracker.gate_min_blur_var", "label": "Gate min. ostrost", "type": "int", "min": 0, "max": 100, "tier": "expert"},
            {"path": "unknown_tracker.gate_min_emb_norm", "label": "Gate min. norma", "type": "float", "min": 0.0, "max": 1.0, "step": 0.05, "tier": "expert"},
        ],
    },

    # ── Vlna 8 — Kodi / počasí ────────────────────────────────────────────────
    {
        "id": "kodi",
        "title": "Kodi (přehrávač)",
        "icon": "🎬",
        "intro": "Připojení k Kodi/OSMC — Hans sleduje co se přehrává a tvoří názory na filmy.",
        "fields": [
            {"path": "kodi.enabled", "label": "Kodi aktivní", "type": "bool"},
            {"path": "kodi.host", "label": "Host (IP)", "type": "text"},
            {"path": "kodi.port", "label": "Port", "type": "number"},
            {"path": "kodi.user", "label": "Uživatel", "type": "text"},
            {"path": "kodi.password", "label": "Heslo", "type": "text"},
            {"path": "kodi.monitor_interval", "label": "Interval monitoringu (s)", "type": "int", "min": 5, "max": 300, "step": 5, "tier": "expert"},
        ],
    },
    {
        "id": "weather",
        "title": "Počasí",
        "icon": "☀",
        "intro": "Poloha pro počasí (ČHMÚ) — vstup do Hansova vnímání prostředí.",
        "fields": [
            {"path": "weather.lat", "label": "Zeměpisná šířka", "type": "number", "float": True},
            {"path": "weather.lon", "label": "Zeměpisná délka", "type": "number", "float": True},
        ],
    },

    # ── Vlna 5 — Detekce / Hailo / objekty / gesta ────────────────────────────
    {
        "id": "hailo",
        "title": "Hailo (detekce obličejů)",
        "icon": "🧠",
        "intro": "Hailo NPU — detekční prahy a cesty k modelům (HEF). Cesty/mode vyžadují restart.",
        "fields": [
            {"path": "hailo_server.mode", "label": "Režim serveru", "type": "choice",
             "choices": ["scrfd", "personface"], "restart": True,
             "tip": "scrfd = jen obličej | personface = obličej + osoba."},
            {"path": "hailo.scrfd_score_thresh", "label": "SCRFD práh detekce", "type": "float", "min": 0.20, "max": 0.80, "step": 0.01},
            {"path": "hailo.scrfd_nms_thresh", "label": "SCRFD NMS práh", "type": "float", "min": 0.20, "max": 0.80, "step": 0.01},
            {"path": "hailo_server.score_thresh", "label": "Personface práh", "type": "float", "min": 0.15, "max": 0.70, "step": 0.01},
            {"path": "hailo.detect_hef", "label": "Detekční HEF", "type": "text", "tier": "expert", "restart": True},
            {"path": "hailo.recog_hef", "label": "Rozpoznávací HEF (ArcFace)", "type": "text", "tier": "expert", "restart": True},
            {"path": "hailo.model_path", "label": "Model path", "type": "text", "tier": "expert", "restart": True},
            {"path": "hailo_server.personface_hef", "label": "Personface HEF", "type": "text", "tier": "expert", "restart": True},
            {"path": "hailo.function_name", "label": "Function name", "type": "text", "tier": "expert", "restart": True},
            {"path": "hailo.batch_size", "label": "Batch size", "type": "int", "min": 1, "max": 8, "tier": "expert", "restart": True},
            {"path": "hailo.force_writable", "label": "Force writable", "type": "bool", "tier": "expert", "restart": True},
        ],
    },
    {
        "id": "objects",
        "title": "Objekty (detekce)",
        "icon": "📦",
        "intro": "Detekce objektů (YOLOv8) — co Hans vidí v místnosti kromě lidí.",
        "fields": [
            {"path": "object_detection.min_confidence", "label": "Min. jistota detekce", "type": "float", "min": 0.0, "max": 1.0, "step": 0.01},
            {"path": "objects.score_thresh", "label": "YOLOv8 práh", "type": "float", "min": 0.30, "max": 0.90, "step": 0.01},
            {"path": "object_detection.min_box_area", "label": "Min. plocha boxu", "type": "float", "min": 0.0, "max": 0.05, "step": 0.001, "tier": "expert"},
            {"path": "objects.hef", "label": "YOLOv8 HEF", "type": "text", "tier": "expert", "restart": True},
        ],
    },
    {
        "id": "gesture",
        "title": "Gesta",
        "icon": "✋",
        "intro": "Rozpoznávání gest rukou (palm/landmark) — ovládání pohybem.",
        "fields": [
            {"path": "gesture.enabled", "label": "Gesta aktivní", "type": "bool"},
            {"path": "gesture.cooldown_s", "label": "Cooldown (s)", "type": "int", "min": 0, "max": 30},
            {"path": "gesture.show_landmarks", "label": "Zobrazit landmarky", "type": "bool"},
            {"path": "gesture.min_confidence", "label": "Min. jistota", "type": "float", "min": 0.0, "max": 1.0, "step": 0.05},
            {"path": "gesture.presence_threshold", "label": "Práh přítomnosti ruky", "type": "float", "min": 0.0, "max": 1.0, "step": 0.05, "tier": "expert"},
            {"path": "gesture.palm_score_threshold", "label": "Práh dlaně", "type": "float", "min": 0.0, "max": 1.0, "step": 0.05, "tier": "expert"},
            {"path": "gesture.palm_padding", "label": "Padding dlaně", "type": "float", "min": 0.5, "max": 3.0, "step": 0.1, "tier": "expert"},
            {"path": "gesture.thumbs_up_hold_s", "label": "Palec nahoru — držet (s)", "type": "float", "min": 0.0, "max": 5.0, "step": 0.5, "tier": "expert"},
            {"path": "gesture.fist_hold_s", "label": "Pěst — držet (s)", "type": "float", "min": 0.0, "max": 5.0, "step": 0.5, "tier": "expert"},
            {"path": "gesture.open_hand_hold_s", "label": "Otevřená dlaň — držet (s)", "type": "int", "min": 0, "max": 10, "tier": "expert"},
            {"path": "gesture.hold_frames", "label": "Hold frames", "type": "int", "min": 1, "max": 30, "tier": "expert"},
            {"path": "gesture.hand_landmark_hef", "label": "Hand landmark HEF", "type": "text", "tier": "expert", "restart": True},
            {"path": "gesture.palm_detection_hef", "label": "Palm detection HEF", "type": "text", "tier": "expert", "restart": True},
        ],
    },
    {
        "id": "scheduler",
        "title": "Scheduler (snímkování)",
        "icon": "⏱",
        "intro": "Jak často běží jednotlivé inference vrstvy (každý N-tý snímek). Detailní ladění výkonu.",
        "fields": [
            {"path": "scheduler.hailo_every_n_frames", "label": "Hailo každý N-tý snímek", "type": "int", "min": 1, "max": 10, "tier": "expert"},
            {"path": "scheduler.mesh_every_n_frames", "label": "Mesh každý N-tý snímek", "type": "int", "min": 1, "max": 10, "tier": "expert"},
        ],
    },

    # ── Vlna 6 — Kamera / obraz ───────────────────────────────────────────────
    # Pozn.: camera_model / autofocus_mode / hdr_mode / camera_presets = custom widgety → odloženo (rozšíření #2).
    {
        "id": "camera",
        "title": "Kamera",
        "icon": "📷",
        "intro": "Rozlišení a snímkování kamery. Změny rozměrů vyžadují restart. (Lores musí být < main.)",
        "fields": [
            {"path": "camera.main_width", "label": "Main šířka", "type": "number", "restart": True},
            {"path": "camera.main_height", "label": "Main výška", "type": "number", "restart": True},
            {"path": "camera.lores_width", "label": "Lores šířka", "type": "number", "restart": True},
            {"path": "camera.lores_height", "label": "Lores výška", "type": "number", "restart": True},
            {"path": "camera.framerate", "label": "Snímků/s", "type": "int", "min": 1, "max": 60, "restart": True},
            {"path": "camera.high_res_width", "label": "High-res šířka", "type": "number", "tier": "expert", "restart": True},
            {"path": "camera.high_res_height", "label": "High-res výška", "type": "number", "tier": "expert", "restart": True},
            {"path": "camera.buffer_count", "label": "Buffer count", "type": "int", "min": 1, "max": 8, "tier": "expert", "restart": True},
            {"path": "camera.rotation", "label": "Rotace (°)", "type": "number", "tier": "expert", "restart": True},
        ],
    },
    {
        "id": "fisheye",
        "title": "Fisheye korekce",
        "icon": "🐟",
        "intro": "Korekce rybího oka u širokoúhlého objektivu.",
        "fields": [
            {"path": "fisheye.enabled", "label": "Korekce aktivní", "type": "bool"},
            {"path": "fisheye.fov_degrees", "label": "FOV (°)", "type": "int", "min": 60, "max": 200},
            {"path": "fisheye.balance", "label": "Balance", "type": "float", "min": 0.0, "max": 1.0, "step": 0.05},
            {"path": "fisheye.apply_to_lores", "label": "Aplikovat na lores", "type": "bool", "tier": "expert"},
            {"path": "fisheye.apply_to_main", "label": "Aplikovat na main", "type": "bool", "tier": "expert"},
        ],
    },
    {
        "id": "hq_zoom",
        "title": "HQ Zoom",
        "icon": "🔎",
        "intro": "Detailní přiblížení obličejů/objektů (picture-in-picture).",
        "fields": [
            {"path": "hq_zoom.enabled", "label": "Zoom aktivní", "type": "bool"},
            {"path": "hq_zoom.trigger_area", "label": "Spouštěcí plocha", "type": "float", "min": 0.0, "max": 1.0, "step": 0.05},
            {"path": "hq_zoom.padding", "label": "Padding", "type": "float", "min": 0.0, "max": 1.0, "step": 0.05},
            {"path": "hq_zoom.zoom_faces", "label": "Zoom obličeje", "type": "bool"},
            {"path": "hq_zoom.zoom_objects", "label": "Zoom objekty", "type": "bool"},
            {"path": "hq_zoom.servo_settle_ms", "label": "Servo settle (ms)", "type": "number", "tier": "expert"},
            {"path": "hq_zoom.pip_size", "label": "PiP velikost (px)", "type": "number", "tier": "expert"},
            {"path": "hq_zoom.pip_timeout", "label": "PiP timeout (s)", "type": "int", "min": 1, "max": 30, "tier": "expert"},
            {"path": "hq_zoom.pip_refresh_interval", "label": "PiP refresh (s)", "type": "int", "min": 1, "max": 30, "tier": "expert"},
            {"path": "hq_zoom.af_obj_settle_ms", "label": "AF objekt settle (ms)", "type": "number", "tier": "expert"},
        ],
    },
    {
        "id": "display",
        "title": "Displej",
        "icon": "🖥",
        "intro": "Zobrazení a kontroler náhledu.",
        "fields": [
            {"path": "display.headless", "label": "Headless (bez okna)", "type": "bool", "restart": True},
            {"path": "display_controller.detect_every", "label": "Detekce každý N-tý snímek", "type": "int", "min": 1, "max": 10},
            {"path": "display.window_name", "label": "Název okna", "type": "text", "tier": "expert"},
            {"path": "display_controller.lores_width", "label": "Kontroler lores šířka", "type": "number", "tier": "expert"},
            {"path": "display_controller.lores_height", "label": "Kontroler lores výška", "type": "number", "tier": "expert"},
            {"path": "display_controller.enroll_countdown", "label": "Odpočet zápisu (s)", "type": "int", "min": 1, "max": 30, "tier": "expert"},
            {"path": "display_controller.enroll_auto_interval", "label": "Auto interval zápisu (s)", "type": "int", "min": 1, "max": 30, "tier": "expert"},
        ],
    },
    {
        "id": "eyes",
        "title": "Oči (animace)",
        "icon": "👁",
        "intro": "Animované oči na displeji (mrkání, pohled).",
        "fields": [
            {"path": "eyes.enabled", "label": "Oči aktivní", "type": "bool"},
            {"path": "eyes.fps", "label": "FPS", "type": "int", "min": 1, "max": 60},
            {"path": "eyes.blink_interval_min", "label": "Mrkání min (s)", "type": "int", "min": 1, "max": 30},
            {"path": "eyes.blink_interval_max", "label": "Mrkání max (s)", "type": "number", "float": True, "tier": "expert"},
            {"path": "eyes.look_delay", "label": "Zpoždění pohledu (s)", "type": "float", "min": 0.0, "max": 5.0, "step": 0.05, "tier": "expert"},
            {"path": "eyes.texture", "label": "Textura", "type": "text", "tier": "expert"},
        ],
    },

    # ── Vlna 7 — Servo (sledování pohybu) ─────────────────────────────────────
    {
        "id": "servo",
        "title": "Servo — sledování",
        "icon": "🎯",
        "intro": "Sledování obličeje servomotory (pan/tilt) a skenování místnosti.",
        "fields": [
            {"path": "features.servo_tracking", "label": "Servo tracking (hlavní)", "type": "bool",
             "tip": "Hlavní vypínač sledování."},
            {"path": "servo_tracking.enable_tracking", "label": "Sledování aktivní", "type": "bool"},
            {"path": "servo_tracking.center_tolerance", "label": "Tolerance středu (px)", "type": "int", "min": 0, "max": 100},
            {"path": "servo_tracking.tracking_sensitivity", "label": "Citlivost sledování", "type": "int", "min": 1, "max": 10},
            {"path": "servo_tracking.smoothing_factor", "label": "Vyhlazení", "type": "float", "min": 0.0, "max": 1.0, "step": 0.05},
            {"path": "servo_tracking.max_step_degrees", "label": "Max. krok (°)", "type": "int", "min": 1, "max": 30},
            {"path": "servo_tracking.face_lost_timeout", "label": "Timeout ztráty obličeje (s)", "type": "int", "min": 1, "max": 30},
            {"path": "servo_tracking.scanning_enabled", "label": "Skenování aktivní", "type": "bool"},
            {"path": "servo_tracking.scanning_speed", "label": "Rychlost skenování", "type": "int", "min": 1, "max": 50},
            {"path": "servo_tracking.tracking_delay", "label": "Zpoždění sledování (s)", "type": "float", "min": 0.0, "max": 1.0, "step": 0.01, "tier": "expert"},
        ],
    },
    {
        "id": "servo_range",
        "title": "Servo — rozsahy",
        "icon": "🔧",
        "intro": "Meze pohybu, offsety a zapojení kanálů. Kalibrované meze se zapisují automaticky (nejsou zde).",
        "fields": [
            {"path": "servo_tracking.default_pan_offset", "label": "Pan offset (°)", "type": "int", "min": -45, "max": 45},
            {"path": "servo_tracking.default_tilt_offset", "label": "Tilt offset (°)", "type": "int", "min": -45, "max": 45},
            {"path": "servo_tracking.pan_min", "label": "Pan min (°)", "type": "int", "min": -180, "max": 0, "tier": "expert"},
            {"path": "servo_tracking.pan_max", "label": "Pan max (°)", "type": "int", "min": 0, "max": 180, "tier": "expert"},
            {"path": "servo_tracking.tilt_min", "label": "Tilt min (°)", "type": "int", "min": -90, "max": 0, "tier": "expert"},
            {"path": "servo_tracking.tilt_max", "label": "Tilt max (°)", "type": "int", "min": 0, "max": 90, "tier": "expert"},
            {"path": "servo_tracking.scanning_pan_min", "label": "Sken pan min (°)", "type": "int", "min": -90, "max": 0, "tier": "expert"},
            {"path": "servo_tracking.scanning_pan_max", "label": "Sken pan max (°)", "type": "int", "min": 0, "max": 90, "tier": "expert"},
            {"path": "servo_tracking.pan_channel", "label": "Pan kanál", "type": "text", "tier": "expert", "restart": True},
            {"path": "servo_tracking.tilt_channel", "label": "Tilt kanál", "type": "text", "tier": "expert", "restart": True},
        ],
    },

    # ── Vlna 1b — Denní režim (top-level skaláry, TOP_LEVEL_SCALARS_V1) ────────
    {
        "id": "daily",
        "title": "Hans / Denní režim",
        "icon": "🌙",
        "intro": "Hodiny denních fází a noční chování. Ovlivňuje rutinu, reflexi a spánek (recognition off).",
        "fields": [
            {"path": "morning_hour", "label": "Ráno (h)", "type": "number"},
            {"path": "afternoon_hour", "label": "Odpoledne (h)", "type": "number"},
            {"path": "evening_hour", "label": "Večer (h)", "type": "number"},
            {"path": "night_hour", "label": "Noc (h)", "type": "number"},
            {"path": "sleep_start_hour", "label": "Spánek začátek (h)", "type": "number",
             "tip": "Od této hodiny spánek — recognition off, servo nahoru, TTS off."},
            {"path": "sleep_end_hour", "label": "Spánek konec (h)", "type": "number"},
            {"path": "dreams_enabled", "label": "Sny aktivní", "type": "bool"},
            {"path": "night_summary", "label": "Noční souhrn", "type": "bool"},
            {"path": "night_reduce_activity", "label": "Noc — omezit aktivitu", "type": "bool", "tier": "expert"},
        ],
    },

    # ── Vlna 9 — Systém / infra ───────────────────────────────────────────────
    {
        "id": "wol",
        "title": "Systém / WOL",
        "icon": "🔌",
        "intro": "Wake-on-LAN — Hans budí PC magic packetem před plánovaným probuzením.",
        "fields": [
            {"path": "wol_pc_enabled", "label": "WOL aktivní", "type": "bool"},
            {"path": "wol_pc_ip", "label": "IP PC", "type": "text"},
            {"path": "wol_pc_mac", "label": "MAC PC", "type": "text"},
            {"path": "wol_minutes_before_wakeup", "label": "Minut před probuzením", "type": "number"},
        ],
    },
    {
        "id": "system",
        "title": "Systém / Debug & UI",
        "icon": "🛠",
        "intro": "Ladicí přepínače, náhledové UI a výkon. Většina jen pro vývoj.",
        "fields": [
            {"path": "debug", "label": "Debug", "type": "bool"},
            {"path": "debug_verbose", "label": "Debug verbose", "type": "bool"},
            {"path": "ui.show_fps", "label": "Zobrazit FPS", "type": "bool"},
            {"path": "ui.show_landmarks", "label": "Zobrazit landmarky", "type": "bool"},
            {"path": "ui.greeting_cooldown", "label": "Cooldown pozdravu (s)", "type": "int", "min": 0, "max": 300},
            {"path": "ui.flash_duration", "label": "Délka bliknutí (s)", "type": "float", "min": 0.0, "max": 5.0, "step": 0.1, "tier": "expert"},
            {"path": "ui.enrollment_prompt_cooldown", "label": "Cooldown výzvy k zápisu (s)", "type": "int", "min": 0, "max": 300, "tier": "expert"},
            {"path": "ui.enable_debug_output", "label": "Debug výstup UI", "type": "bool", "tier": "expert"},
            {"path": "performance.target_fps", "label": "Cílové FPS", "type": "int", "min": 1, "max": 60, "tier": "expert"},
            {"path": "performance.processing_sleep", "label": "Processing sleep (s)", "type": "float", "min": 0.0, "max": 0.5, "step": 0.005, "tier": "expert"},
            {"path": "performance.frame_drop_threshold", "label": "Frame drop práh", "type": "int", "min": 1, "max": 100, "tier": "expert"},
            {"path": "performance.memory_cleanup_interval", "label": "Interval úklidu paměti", "type": "number", "tier": "expert"},
        ],
    },

    # ── Vlna 1c — Pozdravy (greeting, +special_greetings jako JSON) ────────────
    {
        "id": "greeting",
        "title": "Hans / Pozdravy",
        "icon": "👋",
        "intro": "Jak Hans zdraví příchozí. Per-osoba pozdravy jako JSON (klíč = jméno, hodnota = instrukce).",
        "fields": [
            {"path": "greeting.system_prompt", "label": "System prompt", "type": "textarea", "rows": 4,
             "tip": "Identita pro generování pozdravu."},
            {"path": "greeting.user_prompt", "label": "User prompt (obecný)", "type": "textarea", "rows": 3,
             "tip": "Šablona pozdravu. Proměnné: {name}, {tod} (denní doba)."},
            {"path": "greeting.special_greetings", "label": "Speciální pozdravy (per osoba)", "type": "json", "rows": 6,
             "tip": "JSON: {\"jmeno\": \"instrukce pozdravu\"}. Lze přidat/odebrat osobu úpravou JSONu."},
        ],
    },

    # ── Vlna custom — režimové dropdowny + JSON editory ───────────────────────
    {
        "id": "camera_mode",
        "title": "Kamera — režim & ostření",
        "icon": "📷",
        "intro": "Model kamery, autofokus a HDR. Změna modelu/HDR vyžaduje restart.",
        "fields": [
            {"path": "camera_model", "label": "Model kamery", "type": "choice", "choices": ["v2", "v3_wide"], "restart": True,
             "tip": "v2 = fixní ostření ~62° FOV | v3_wide = autofokus ~120° FOV."},
            {"path": "autofocus_mode", "label": "Autofokus režim", "type": "choice", "choices": ["triggered", "continuous"],
             "tip": "triggered = zaostři na obličej a zamkni | continuous = ostři každý snímek (může rozmazávat)."},
            {"path": "hdr_mode", "label": "HDR režim", "type": "number", "restart": True,
             "tip": "Camera Module v3. 0=Off, 1=MultiExposure, 2=SingleExposure, 3=Night, 4=Auto."},
            {"path": "autofocus_lens_position", "label": "Pozice čočky (manuál)", "type": "number", "float": True, "tier": "expert"},
            {"path": "autofocus.retrigger_s", "label": "Re-trigger AF (s)", "type": "int", "min": 1, "max": 30, "tier": "expert"},
            {"path": "autofocus.size_change_thresh", "label": "Práh změny velikosti", "type": "float", "min": 0.0, "max": 1.0, "step": 0.05, "tier": "expert"},
        ],
    },
    {
        "id": "advanced_json",
        "title": "Pokročilé (JSON)",
        "icon": "🧩",
        "intro": "Komplexní struktury editované přímo jako JSON. Pozor na validní syntaxi (nevalidní = neuloží se).",
        "fields": [
            {"path": "known_persons", "label": "Známé osoby", "type": "json", "rows": 8,
             "tip": "JSON: {\"jmeno\": {\"gender\": \"muž/žena\", \"notes\": \"...\"}}. Jméno musí přesně odpovídat databázi obličejů."},
            {"path": "object_remapping", "label": "Remapping objektů", "type": "json", "rows": 6,
             "tip": "JSON: {\"coco_třída\": \"vlastní název\"} nebo null pro skrytí. Smaže staré záznamy dané třídy."},
            {"path": "camera_presets", "label": "Presety kamery", "type": "json", "rows": 8, "tier": "expert",
             "tip": "JSON presety rozlišení per model kamery (v2/v3_wide)."},
        ],
    },
]


# ── Kategorie (sidebar navigace, SCHEMA_CATEGORIES_V1) ───────────────────────
# Mapování group.id → kategorie. Jedno místo (ne 38 editů ve skupinách).
CATEGORY_OF = {
    "identity": "Hans", "hans_dialog": "Hans", "hans_models": "Hans",
    "greeting": "Hans", "daily": "Hans",
    "hans_activity": "Chování", "room_observer": "Chování", "relationships": "Chování",
    "hans_library": "Chování", "hans_questions": "Chování", "kolac_cases": "Chování",
    "chat_direct": "Chat", "chat_openwebui": "Chat", "chat_cloud": "Chat",
    "tts": "Hlas", "voice": "Hlas",
    "recognition": "Rozpoznávání", "face_preprocess": "Rozpoznávání",
    "face_quality": "Rozpoznávání", "unknown_enrollment": "Rozpoznávání",
    "unknown_tracker": "Rozpoznávání",
    "hailo": "Detekce", "objects": "Detekce", "gesture": "Detekce", "scheduler": "Detekce",
    "camera": "Kamera", "camera_mode": "Kamera", "fisheye": "Kamera",
    "hq_zoom": "Kamera", "display": "Kamera", "eyes": "Kamera",
    "servo": "Servo", "servo_range": "Servo",
    "kodi": "Systém", "weather": "Systém", "wol": "Systém",
    "system": "Systém", "advanced_json": "Systém",
}
CATEGORY_ORDER = ["Hans", "Chování", "Chat", "Hlas", "Rozpoznávání",
                  "Detekce", "Kamera", "Servo", "Systém"]
CATEGORY_ICON = {
    "Hans": "🎩", "Chování": "🧠", "Chat": "💬", "Hlas": "🔊",
    "Rozpoznávání": "🔍", "Detekce": "📦", "Kamera": "📷",
    "Servo": "🎯", "Systém": "🛠",
}


def categories():
    """Uspořádaný seznam kategorií (jen ty, co mají skupiny)."""
    present = {CATEGORY_OF.get(g["id"], "Ostatní") for g in GROUPS}
    out = [c for c in CATEGORY_ORDER if c in present]
    out += [c for c in sorted(present) if c not in CATEGORY_ORDER]
    return [{"name": c, "icon": CATEGORY_ICON.get(c, "⚙")} for c in out]


# ── API pro generátory ──────────────────────────────────────────────────────

def _normalize(f: dict) -> dict:
    """Doplň defaulty + odvoď section/key z dot-path."""
    f = dict(f)
    f.setdefault("tier", "basic")
    f.setdefault("restart", False)
    f.setdefault("tip", "")
    f.setdefault("managed_by", None)
    # TOP_LEVEL_SCALARS_V1: path bez tečky = top-level skalár (section=celý klíč, key=None)
    if "." in f["path"]:
        section, key = f["path"].split(".", 1)
    else:
        section, key = f["path"], None
    f["section"] = section
    f["key"] = key
    return f


def groups(tier=None):
    """Skupiny s normalizovanými poli. tier=None → vše; jinak jen daný tier."""
    out = []
    for g in GROUPS:
        fields = [_normalize(f) for f in g["fields"]]
        if tier:
            fields = [f for f in fields if f["tier"] == tier]
        if not fields:
            continue
        gg = dict(g)
        gg["fields"] = fields
        gg["category"] = CATEGORY_OF.get(g["id"], "Ostatní")   # SCHEMA_CATEGORIES_V1
        out.append(gg)
    return out


def sections():
    """Top-level config sekce, kterých se schéma dotýká (pro uložení po sekcích)."""
    secs = set()
    for g in GROUPS:
        for f in g["fields"]:
            secs.add(f["path"].split(".", 1)[0])
    return sorted(secs)


def web_groups():
    """JSON-serializovatelná podoba pro webadmin frontend."""
    return groups()
