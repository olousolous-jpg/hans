"""
HansIntent — klasifikace intentu uživatelovy zprávy pro grounding (fáze G).

Marker: G2_INTENT_CLASSIFY_V1

Rozhoduje: je zpráva FAKTICKÁ (potřebuje grounding) nebo VOLNÁ konverzace?
A pokud faktická, do jaké třídy — to určí v G.3, kterou kolekci dotázat.

HYBRID: levná keyword/heuristika první, malý LLM jen v šedé zóně.
Robustní pro paměť: v nejistotě eskaluje na LLM (a při selhání LLM padá na
faktickou třídu), protože tichá konfabulace je dražší než retrieval navíc.

Třídy:
    'film'     — dotaz na film/seriál (→ hans_filmy)
    'misto'    — dotaz na místo/lokaci (→ Wikipedia, hans_denik)
    'osobnost' — dotaz na osobu/osobnost (→ hans_denik, Wikipedia)
    'udalost'  — dotaz na událost/fakt/co se stalo (→ hans_denik, Wikipedia)
    'volna'    — volná konverzace, emoce, "o tobě" (→ ŽÁDNÝ grounding)

FAKTICKÉ třídy = vše kromě 'volna'.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass
from typing import Optional

_log = logging.getLogger(__name__)


# G2_DEACCENT_V1 — český vstup na mobilu/Telegramu často BEZ diakritiky
# („rekni mi o nem vic"). Regexy jsou psané s diakritikou → nechytnou.
# Řešení: normalizuj obě strany — vstup i vzory — a spusť matching na obou
# variantách (přebijí sebe, ne konfliktní). Bezpečné: NFD strip Mn zachová
# regex meta chars (\b, |, [], ()); jen zbaví akcentů.
def _deaccent(s: str) -> str:
    if not s:
        return s
    return unicodedata.normalize("NFD", s).encode("ascii", "ignore").decode("ascii")


def _ascii_pat(p: re.Pattern) -> re.Pattern:
    """Auto-deakcentovaná verze regex vzoru (import-time)."""
    return re.compile(_deaccent(p.pattern), p.flags)

# Faktické třídy (vše krom volna). Pořadí = priorita při keyword shodě.
FACTUAL_CLASSES = ("film", "misto", "osobnost", "udalost")
ALL_CLASSES = FACTUAL_CLASSES + ("volna",)


@dataclass
class IntentResult:
    """Výsledek klasifikace intentu.

    intent: jedna z ALL_CLASSES
    confidence: 0..1 (heuristická jistota; LLM výsledky = 0.7 default)
    source: 'keyword' | 'llm' | 'fallback'
    """
    intent: str = "volna"
    confidence: float = 0.0
    source: str = "keyword"

    @property
    def is_factual(self) -> bool:
        """True když zpráva potřebuje grounding (cokoliv krom volna)."""
        return self.intent in FACTUAL_CLASSES


# ── Keyword/heuristické vzory ────────────────────────────────────────────────
# VOLNÁ konverzace — pozdravy, emoce, "o tobě", společenské fráze.
_VOLNA_PAT = re.compile(
    r"\b("
    r"ahoj|čau|čus|nazdar|dobr[ýé]\s+(ráno|den|večer|odpoledne)|"
    r"dobrou\s+noc|měj\s+se|jak\s+se\s+(máš|maš|vede|daří)|"
    r"co\s+(děláš|delas)|jak\s+je|díky|děkuj|prosím|promiň|"
    r"jsi\s+(chytr|hodn|milý|skvěl|fajn|dobr|super|úžasn)|"
    r"jak[ýáé]\s+jsi|kdo\s+jsi|líbí\s+se\s+ti|"
    r"těší\s+mě|rád\s+tě|mám\s+tě\s+rád|chybíš"
    r")",
    re.IGNORECASE,
)

# FILM — explicitní filmová slovní zásoba
_FILM_PAT = re.compile(
    r"\b("
    r"film|filmu|filmy|filmů|snímek|snímku|seriál|seriálu|"
    r"režisér|režie|herec|herečk|hraje\s+v|natočil|natočen|"
    r"komedie|drama|thriller|sci-?fi|dokument|kino|"
    r"viděl\s+jsi\s+film|znáš\s+film|o\s+čem\s+je"
    r")",
    re.IGNORECASE,
)

# MÍSTO — lokace
_MISTO_PAT = re.compile(
    r"\b("
    r"kde\s+(je|leží|se\s+nachází)|"
    r"město|měst[ao]|vesnice|hrad|zámek|palác|paláce|"
    r"ulice|náměstí|řeka|hora|země|stát|hlavní\s+město|"
    r"jak\s+se\s+dostanu|kudy"
    r")",
    re.IGNORECASE,
)

# OSOBNOST — známé osoby (ne lidé v místnosti, to řeší Memory)
_OSOBNOST_PAT = re.compile(
    r"\b("
    r"kdo\s+(je|byl|to\s+je)|"
    r"prezident|spisovatel|vědec|herec|herečk|zpěvák|zpěvačk|"
    r"politik|král|císař|filozof|malíř|skladatel|"
    r"narodil\s+se|zemřel|slavný|známý"
    r")",
    re.IGNORECASE,
)

# UDÁLOST / obecný fakt — kdy, co se stalo, faktické dotazy
_UDALOST_PAT = re.compile(
    r"\b("
    r"kdy\s+(se|byl|byla|bylo|proběhl|došlo|začal|skončil)|"
    r"co\s+se\s+stalo|v\s+kolik|kolik\s+(je|bylo|stojí|má|měří|váží)|"
    r"co\s+(je|znamená|to\s+je)\s+\w+|"
    r"válka|revoluce|bitva|objev|vynález|historie|dějiny|"
    r"vysvětli|řekni\s+mi\s+(o|něco\s+o)|pověz\s+mi\s+o|"
    r"který\s+rok|kterého\s+roku|letopočet"
    r")",
    re.IGNORECASE,
)

# Signál FAKTICKÉHO dotazu obecně (otázka na vnější svět)
_FACTUAL_SIGNAL = re.compile(
    r"(\?|"
    r"\b(kdo|co|kde|kdy|kolik|jak[ýáéí]|který|kterého|proč|"
    r"vysvětli|řekni|pověz|znáš|víš)\b)",
    re.IGNORECASE,
)

# G2_COREF_V1 — pronomenální reference („o něm", „o něj", „o ní", „o nich",
# „ho", „mu", „ji") = navazovací dotaz na předchozí obrat. Sám o sobě SLABÝ
# signál (i „mám ho rád" = emoce), proto se použije JEN v kombinaci:
# faktický signál nebo continuation slovo („víc/více/dál/dál/další") → přesto
# faktické. Rewriter (F1) pak z historie doplní jméno.
_COREF_PAT = re.compile(
    r"\b(o\s+n[ěe]m|o\s+n[ěe]j|o\s+n[ií]|o\s+nich|ho|mu|j[ií])\b",
    re.IGNORECASE,
)

# Continuation slova („řekni víc", „pověz dál") — v kombinaci s _COREF_PAT
# nebo předchozí historií naznačují follow-up dotaz.
_CONTINUATION_PAT = re.compile(
    r"\b(v[ií]c|v[ií]ce|d[áa]l|dal[šs][íi]|jeste|je[šs]t[ěe]|"
    r"pokra[čc]uj|pov[ěe]z|[řr]ekni)\b",
    re.IGNORECASE,
)

# Deakcentované varianty (auto-generované) — chytnou vstup bez diakritiky.
_VOLNA_PAT_A = _ascii_pat(_VOLNA_PAT)
_FILM_PAT_A = _ascii_pat(_FILM_PAT)
_MISTO_PAT_A = _ascii_pat(_MISTO_PAT)
_OSOBNOST_PAT_A = _ascii_pat(_OSOBNOST_PAT)
_UDALOST_PAT_A = _ascii_pat(_UDALOST_PAT)
_FACTUAL_SIGNAL_A = _ascii_pat(_FACTUAL_SIGNAL)
_COREF_PAT_A = _ascii_pat(_COREF_PAT)
_CONTINUATION_PAT_A = _ascii_pat(_CONTINUATION_PAT)


def _any_match(pat, pat_a, msg, msg_a) -> bool:
    """True když regex (nebo jeho deacc varianta) matchne."""
    return bool(pat.search(msg) or pat_a.search(msg_a))


def _sum_matches(pat, pat_a, msg, msg_a) -> int:
    """Max findall shod (orig vs deacc). Ne součet — chráníme před 2×
    započítáním téhož výskytu."""
    return max(len(pat.findall(msg)), len(pat_a.findall(msg_a)))


class HansIntent:
    """Hybrid intent klasifikátor pro grounding."""

    def __init__(self, config: dict):
        ic = config.get("intent", {}) or {}
        self._enabled: bool = bool(ic.get("enabled", True))
        # BEZPEČNÝ default — NE openwebui_chat.model_name (tam je bge-m3!)
        self._model: str = ic.get("model", "qwen2.5:7b")
        self._base_url: str = (
            ic.get("base_url")
            or config.get("openwebui_chat", {}).get(
                "base_url", "http://127.0.0.1:11434")
        )
        self._timeout: int = int(ic.get("timeout", 15))
        # G2_KEYWORD_TUNE_V1 — LLM VYPNUTÝ defaultně (VRAM: qwen2.5:7b
        # se nevejde k hans-czech+bge-m3 do 16GB). Keyword + fallback stačí.
        self._use_llm: bool = bool(ic.get("use_llm", False))
        self._config = config
        # práh confidence, pod kterým keyword eskaluje na LLM (šedá zóna)
        self._gray_zone: float = float(ic.get("gray_zone_threshold", 0.6))

    # ── Public API ───────────────────────────────────────────────────────────

    def classify(self, message: str) -> IntentResult:
        """Klasifikuj zprávu. Vždy vrátí IntentResult (nikdy nevyhodí)."""
        if not message or not message.strip():
            return IntentResult(intent="volna", confidence=1.0, source="keyword")

        msg = message.strip()

        # 1) KEYWORD vrstva (levná, bez sítě)
        kw = self._classify_keyword(msg)
        if kw.confidence >= self._gray_zone:
            return kw  # jasný případ — hotovo levně

        # 2) ŠEDÁ ZÓNA → LLM (jen když je povolený a dostupný)
        if self._use_llm and self._enabled:
            llm = self._classify_llm(msg)
            if llm is not None:
                return llm

        # 3) FALLBACK — keyword nejistý + LLM nedostupný.
        # Když keyword aspoň něco naznačil, vrať to. Jinak BEZPEČNĚ faktická
        # ('udalost') pokud to vypadá jako otázka, jinak volná.
        if kw.intent != "volna":
            return IntentResult(intent=kw.intent,
                                confidence=kw.confidence,
                                source="keyword")
        if _FACTUAL_SIGNAL.search(msg):
            # vypadá to jako otázka na svět, ale keyword nechytil třídu →
            # radši grounding (událost) než tichá konfabulace
            return IntentResult(intent="udalost", confidence=0.4,
                                source="fallback")
        return IntentResult(intent="volna", confidence=0.5, source="fallback")

    # ── Keyword vrstva ─────────────────────────────────────────────────────────

    def _classify_keyword(self, msg: str) -> IntentResult:
        """Heuristická klasifikace. Vrátí intent + confidence."""
        # G2_DEACCENT_V1 — matchuj i vstup BEZ diakritiky (Telegram/mobil).
        msg_a = _deaccent(msg)

        # VOLNÁ má prioritu — pozdrav/emoce jsou silný signál i v otázce
        # ("ahoj, jak se máš?"). Ale jen když NENÍ zároveň faktický dotaz.
        volna_hit = _any_match(_VOLNA_PAT, _VOLNA_PAT_A, msg, msg_a)
        factual_signal = _any_match(_FACTUAL_SIGNAL, _FACTUAL_SIGNAL_A,
                                    msg, msg_a)

        # G2_COREF_V1 — coreference booster: pronomenální reference +
        # continuation slovo („o něm víc") = follow-up dotaz na PŘEDCHOZÍ
        # obrat → faktický (rewriter F1 pak jméno doplní z historie).
        coref = _any_match(_COREF_PAT, _COREF_PAT_A, msg, msg_a)
        continuation = _any_match(_CONTINUATION_PAT, _CONTINUATION_PAT_A,
                                  msg, msg_a)
        if coref and continuation and not volna_hit:
            factual_signal = True

        # spočti shody faktických tříd (max orig vs deacc — ne součet)
        scores = {
            "film":     _sum_matches(_FILM_PAT, _FILM_PAT_A, msg, msg_a),
            "misto":    _sum_matches(_MISTO_PAT, _MISTO_PAT_A, msg, msg_a),
            "osobnost": _sum_matches(_OSOBNOST_PAT, _OSOBNOST_PAT_A,
                                     msg, msg_a),
            "udalost":  _sum_matches(_UDALOST_PAT, _UDALOST_PAT_A,
                                     msg, msg_a),
        }
        best_class = max(scores, key=scores.get)
        best_score = scores[best_class]

        # Čistý pozdrav/emoce bez faktického signálu → volná (vysoká jistota)
        if volna_hit and not factual_signal and best_score == 0:
            return IntentResult(intent="volna", confidence=0.9, source="keyword")

        # Silná shoda faktické třídy → vrať ji
        if best_score >= 2:
            return IntentResult(intent=best_class, confidence=0.85,
                                source="keyword")
        if best_score == 1:
            # jedna shoda — střední jistota (může eskalovat na LLM)
            conf = 0.7 if factual_signal else 0.55
            return IntentResult(intent=best_class, confidence=conf,
                                source="keyword")

        # Žádná faktická třída, ale je tu otázkový signál → faktická
        # G2_KEYWORD_TUNE_V1 — zvednuto 0.5→0.65 (klasifikuj PŘÍMO,
        # ne přes fallback; LLM je vypnutý, tak ať je to čisté)
        if factual_signal and not volna_hit:
            return IntentResult(intent="udalost", confidence=0.65,
                                source="keyword")

        # Pozdrav + něco → mírně volná, ať to případně rozhodne LLM
        if volna_hit:
            return IntentResult(intent="volna", confidence=0.65, source="keyword")

        # Nic jasného → šedá zóna (nízká confidence → eskaluje na LLM)
        return IntentResult(intent="volna", confidence=0.3, source="keyword")

    # ── LLM vrstva (šedá zóna) ──────────────────────────────────────────────────

    def _classify_llm(self, msg: str) -> Optional[IntentResult]:
        """Zeptej se malého modelu. Vrátí IntentResult nebo None při selhání."""
        try:
            from scripts.ollama_client import ollama_chat
        except Exception as e:
            _log.warning("intent LLM: import ollama_chat selhal: %s", e)
            return None

        system = (
            "Jsi klasifikátor. Rozhodni, do které kategorie patří uživatelova "
            "zpráva. Odpověz JEDNÍM slovem z: film, misto, osobnost, udalost, volna.\n"
            "- film = dotaz na film, seriál, režiséra, herce\n"
            "- misto = dotaz na místo, město, lokaci\n"
            "- osobnost = dotaz na známou osobu (ne pozdrav)\n"
            "- udalost = dotaz na fakt, událost, co/kdy se stalo, co něco znamená\n"
            "- volna = pozdrav, emoce, dotaz na tebe, společenská konverzace\n"
            "Odpověz POUZE jedním slovem, nic víc."
        )
        result = ollama_chat(
            self._model,
            [{"role": "system", "content": system},
             {"role": "user", "content": msg}],
            ollama_url=self._base_url,
            timeout=self._timeout,
            options={"num_predict": 8, "temperature": 0.0},
        )
        if result is None:
            _log.warning("intent LLM: ollama_chat vrátil None (model=%s)",
                         self._model)
            return None

        # parsuj — najdi první platnou třídu v odpovědi
        low = result.strip().lower()
        for cls in ALL_CLASSES:
            if cls in low:
                return IntentResult(intent=cls, confidence=0.7, source="llm")

        # LLM vrátil nesmysl → bezpečně faktická (radši grounding)
        _log.warning("intent LLM: neočekávaná odpověď %r → fallback udalost",
                     result[:50])
        return IntentResult(intent="udalost", confidence=0.4, source="fallback")
