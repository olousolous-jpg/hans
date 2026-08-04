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
    r"dobrou\s+noc|měj\s+se|"
    # HANS_INTENT_WELLBEING_V1 (4.8.) — dřív jen „jak se máš": vsunuté zájmeno
    # nebo příslovce vzor rozbilo („jak se TI daří", „jakPAK se ti DNES daří"),
    # dotaz spadl na `udalost` → grounding → žádná fakta → ANTI-KONFAB ABSTINENCE.
    # Doloženo 4.8. 11:58 a 12:01: na „jakpak se ti dnes daří?" Hans odpověděl
    # „K tomuhle nemám spolehlivý záznam a nerad bych si domýšlel." Zdvořilostní
    # dotaz na VLASTNÍ stav není faktický dotaz na svět — patří do volné
    # konverzace, kde Hans mluví z nálady a z toho, co dnes dělal.
    # `ja[kmn]` = tolerance na překlep od sousední klávesy („jam se ti dari?",
    # reálný vstup z mobilu 4.8.). Zbytek vzoru je dost specifický, aby to
    # nechytalo nic jiného; rewriter F1 opravuje až NA faktické cestě, tedy
    # pozdě — klasifikace musí překlep přežít sama.
    r"ja[kmn](pak)?\s+se\s+(ti\s+|v[áa]m\s+)?(dnes(ka)?\s+)?(m[áa][šs]|vede|da[řr][íi])|"
    r"co(pak)?\s+(te[ďd]\s+|pr[áa]v[ěe]\s+)?(d[ěe]l[áa][šs]|delas)|"
    r"jak\s+je|díky|děkuj|prosím|promiň|"
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
    # HANS_INTENT_LLM_PI_V1 (4.8.) — „co je …" je široký vzor a chytal i
    # zdvořilostní „co je u tebe nového?" / „co je s tebou?". Tím vyrobil
    # FALEŠNOU pozitivní evidenci (skóre 1) → dotaz se nedostal do šedé zóny,
    # mini model ho nesměl zachránit a Hans na běžnou frázi abstinoval.
    # Negativní lookahead vyřadí obraty mířené NA HANSE.
    r"co\s+(je|znamená|to\s+je)\s+(?!u\s+tebe|s\s+tebou|s\s+t[ěe]bou|nov[éěe])\w+|"
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
        # HANS_INTENT_LLM_PI_V1 — model drž nahraný, ať se neplatí 15-20 s
        # cold start při každém prvním dotazu (na Pi je load nejdražší část).
        self._keep_alive: str = str(ic.get("keep_alive", "30m"))
        self._llm_cache: dict = {}
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

        # 2) ŠEDÁ ZÓNA → mini model na Pi (HANS_INTENT_LLM_PI_V1)
        #
        # Proč to má cenu: seznam vzorů pokryje JEN formulace, které jsme
        # viděli. Jiný člověk se zeptá jinak („máš se dobře?", „nudíš se?",
        # „co je u tebe nového?") → vzor nesedne → dotaz projde jako faktický
        # → grounding nenajde fakta → ANTI-KONFABULAČNÍ ABSTINENCE na obyčejnou
        # zdvořilost. Model tuhle rodinu pokrývá bez vyjmenovávání (10/10 na
        # neviděných formulacích).
        #
        # ASYMETRIE (bezpečnostní jádro): eskaluje se JEN tam, kde keyword
        # NEMÁ žádnou pozitivní shodu s faktickou třídou a rozhodl se pouze
        # podle otazníku. Model tak může dotaz posunout z „abstinoval bych"
        # na „popovídám si", ALE NIKDY nevezme dobře doložený faktický dotaz
        # a neudělá z něj nezakotvené povídání — ty totiž mají skóre ≥1
        # a do šedé zóny se vůbec nedostanou.
        if self._use_llm and self._enabled:
            _cached = self._llm_cache.get(msg)
            if _cached is None:
                _fact = self._llm_is_factual(msg)
                if _fact is not None and len(self._llm_cache) < 256:
                    self._llm_cache[msg] = _fact
            else:
                _fact = _cached
            if _fact is False:
                _log.info("intent: mini model → VOLNÁ (%.40s)", msg)
                return IntentResult(intent="volna", confidence=0.75,
                                    source="llm")
            if _fact is True:
                return IntentResult(
                    intent=kw.intent if kw.intent != "volna" else "udalost",
                    confidence=0.7, source="llm")
            # None → model nedostupný/nejednoznačný → keyword jako dosud

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
            # HANS_INTENT_LLM_PI_V1 — ROZLIŠ doloženou faktickou otázku od
            # pouhého otazníku. best_score==0 znamená, že žádná faktická třída
            # nesedla a rozhodujeme jen podle „?"/„jak" — to je přesně šedá
            # zóna, kde dosud vznikala falešná abstinence („jak se ti daří?").
            # Nižší confidence → eskaluje na mini model; když není, fallback
            # níže vrátí `udalost` jako dřív (žádná regrese).
            if best_score == 0:
                return IntentResult(intent="udalost", confidence=0.5,
                                    source="keyword")
            return IntentResult(intent="udalost", confidence=0.65,
                                source="keyword")

        # Pozdrav + něco → mírně volná, ať to případně rozhodne LLM
        if volna_hit:
            return IntentResult(intent="volna", confidence=0.65, source="keyword")

        # Nic jasného → šedá zóna (nízká confidence → eskaluje na LLM)
        return IntentResult(intent="volna", confidence=0.3, source="keyword")

    # ── LLM vrstva (šedá zóna) ──────────────────────────────────────────────────

    # HANS_INTENT_LLM_PI_V1 (4.8.) — few-shot BINÁRNÍ prompt. Změřeno na
    # qwen2.5:1.5b (Pi, CPU): 5-třídní prompt bez příkladů model degeneruje
    # (0.5b říká na všechno „fakticky", 1.5b na všechno „volna"); s příklady
    # a JEN dvěma třídami dá 12/12 na trénovacích a 14/16 na neviděných
    # formulacích — a 10/10 v rodině „jak se máš", kde vznikala ta falešná
    # abstinence. Třídu (film/misto/…) proto NEURČUJE model, ale keyword
    # vrstva: ptáme se ho jen na to, co prokazatelně umí.
    _LLM_SYSTEM = (
        "Klasifikuj českou zprávu do jedné ze dvou tříd.\n"
        "VOLNA = pozdrav, poděkování, zdvořilost, nebo dotaz na TEBE "
        "(tvůj stav, náladu, co děláš, co bys chtěl).\n"
        "FAKTICKY = dotaz na informaci o světě mimo tebe (lidé, filmy, místa, "
        "počasí, události, pojmy).\n\n"
        "Příklady:\n"
        "„jak se máš?\" -> volna\n"
        "„co děláš?\" -> volna\n"
        "„díky moc\" -> volna\n"
        "„kdo je Karel IV?\" -> fakticky\n"
        "„jaké je počasí?\" -> fakticky\n"
        "„co běží v televizi?\" -> fakticky\n\n"
        "Odpověz JEDNÍM slovem: volna nebo fakticky."
    )

    def _llm_is_factual(self, msg: str) -> Optional[bool]:
        """Zeptej se mini modelu: je to faktický dotaz? None = nedostupný.

        ⚠️ ZÁMĚRNĚ NEJDE přes `ollama_client.ollama_chat`: ten má globální
        `game_mode_on()` gate a míří na PC endpoint. Tenhle klasifikátor běží
        na MALÉM modelu PŘÍMO NA PI (CPU, ~1 GB) — s VRAM na PC nemá nic
        společného a musí fungovat i když je PC vypnuté nebo se hraje. Právě
        proto byla LLM vrstva dosud vypnutá (qwen2.5:7b se k hans-czech do
        16 GB nevešel) — mini model na Pi tenhle spor ruší."""
        import json as _json
        import urllib.request as _url
        body = _json.dumps({
            "model": self._model, "stream": False,
            "keep_alive": self._keep_alive,
            "options": {"num_predict": 4, "temperature": 0.0},
            "messages": [{"role": "system", "content": self._LLM_SYSTEM},
                         {"role": "user", "content": msg}],
        }).encode("utf-8")
        try:
            req = _url.Request("%s/api/chat" % self._base_url.rstrip("/"),
                               body, {"Content-Type": "application/json"})
            with _url.urlopen(req, timeout=self._timeout) as r:
                out = _json.loads(r.read())["message"]["content"]
        except Exception as e:
            _log.debug("intent LLM nedostupný (%s) → keyword fallback", e)
            return None
        low = (out or "").strip().lower()
        if low.startswith("volna") or low.startswith("volná"):
            return False
        if low.startswith("fakt"):
            return True
        _log.debug("intent LLM: nejednoznačné %r → keyword fallback", low[:30])
        return None
