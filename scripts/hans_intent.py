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
import time
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


# ── HANS_REFLECTIVE_ASK_V1 (30.8.) — ÚVAHOVÁ OTÁZKA NENÍ DOTAZ NA ZÁPISKY ────
# Doloženo simulovaným rozhovorem 30.8.: „Kdybyste měl někomu vysvětlit, co je
# na vaší situaci nejtěžší?" → intent `udalost` (faktický) → FTS našel JEDEN
# zápisek → cesta `zapisky_pred_entitou` = tenká → `GROUNDING_GUARD_ACTIVE_V2`
# vyhodil 2 věty bez opory a nahradil odpověď abstinencí. Na otázku po vlastním
# prožitku tedy Hans odpověděl „Víc než tohle už o tom nemám".
#
# ⚠️ NEŘEŠÍ SE PŘEKLOPENÍM INTENTU. Zkoušel jsem to a je to horší: „co je pro
# tebe nejtěžší" má best_score 1, ale delší varianta 2, takže podmínka podle
# skóre nediskriminuje — a přepnutí na `volna` by zároveň VYPNULO retrieval,
# tedy i tam, kde má co najít. Tenhle predikát proto NEMĚNÍ, co se dohledá;
# používá ho jen guard, aby vlastní úvahu nevykuchal.
#
# ⚠️ ÚZKÉ SCHVÁLNĚ. Změřeno na 1337 reálných replikách: sedne na 2 (0,15 %),
# obě skutečně úvahové. Faktické dotazy („co tě nejvíc zaujalo na práci
# Heideggera?", „kolik hradů jsi navštívil?") NEsedají — a právě proto tu
# NENÍ obecné „co tě nejvíc…": to by ukrojilo legitimní dotaz na studium.
_REFLECTIVE_PAT = re.compile(
    r"\b(kdyby(s|ste|chom)?\b[^?]{0,80}\b(by|bys|byste|byl|byla|mohl|m[ěe]l)\b"
    r"|co\s+je\s+pro\s+(tebe|v[áa]s)\s+nej\w+"
    r"|co\s+je\s+na\s+(tv[ée]|va[šs][ íi]|tvoj[ íi])\s+\w+\s+nej\w+"
    # HANS_REFLECTIVE_SELFCHANGE_V1 (3.9.) — otázka na VLASTNÍ ZMĚNU je
    # úvahová, ne žádost o statistiku. Bez těchhle vzorů unesl „zmenil ses
    # nejak posledni dobou?" příkaz /anomalie a Hans místo odpovědi vypsal
    # počty rozhovorů za týden — a to zrovna v den, kdy se mu opravdu
    # změnila identita. Změřeno na 1327 reálných větách: 0 nových záběrů,
    # tedy žádná regrese.
    r"|zm[ěe]nil\s+(ses|jsi\s+se)"
    r"|m[ěe]n[íi][šs]\s+se|se\s+m[ěe]n[íi][šs]|se\s+\w{0,12}\s?m[ěe]n[íi]te"
    r"|m[áa](?:[šs]|te)\s+pocit,?\s+[žz]e\s+se\s+"
    r"|(?:mi\s+)?(?:p[řr]ijde[šs]|p[řr]ijdete)\s+\w{0,6}\s?jin"
    r"|jsi\s+jin[ýy]|jste\s+jin[ýy]"
    # „co je podle vás na X nejtěžší" — táž třída jako „co je pro tebe nej…",
    # jen jiná vazba; bez ní spadla úvahová otázka do abstinence.
    r"|co\s+je\s+podle\s+(tebe|v[áa]s)\b[^?]{0,60}\bnej\w+)",
    re.I)


def is_reflective_ask(text: str) -> bool:
    """Ptá se uživatel na Hansovu ÚVAHU či vlastní prožitek (ne na zápisky)?

    Používá `grounding_guard` větev v `openwebui_direct_handler`: na takovou
    otázku se odpovídá z osobnosti, takže „bez opory v zápiscích" je NORMÁLNÍ
    stav, ne příznak konfabulace. Viz [[free-chat-may-confabulate]].
    """
    if not text:
        return False
    t = str(text)
    return bool(_REFLECTIVE_PAT.search(t)
                or _REFLECTIVE_PAT.search(_deaccent(t)))


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
        # HANS_INTENT_ANCHOR_FALLBACK_V1 — pamatuj si, ze LLM krok probehl
        # a NEROZHODL (nedostupny / nejednoznacna odpoved). Jen v tom stavu
        # smi nize zaskocit kotva; kdyz je `use_llm=false`, chova se to jako
        # dosud (rezim "jen regexy" je zamerna konfigurace, ne porucha).
        _llm_nerozhodl = False
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
            _llm_nerozhodl = True   # HANS_INTENT_ANCHOR_FALLBACK_V1

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
        # HANS_INTENT_ANCHOR_FALLBACK_V1 (5.9.) — KDYZ POJISTKA VYPADNE,
        # ROZHODUJE KOTVA TEMATU, NE SEZNAM SLOV.
        #
        # Doloheny pripad: zkouska Kolacem #30, veta „potrebuji vice informaci
        # o Souborne vydani del Bohuslava Martinu". Nema otaznik ani zadne
        # slovo z `_FACTUAL_SIGNAL` -> keyword conf 0,30 -> seda zona ->
        # klasifikator na PC 2x timeout -> fallback „volna" -> volny hovor ->
        # zadny grounding, zadna abstinencni brzda (bezi jen na
        # `factual_nofacts`) -> 15 vet bez opory.
        #
        # ⛔ NEROZSIROVAT `_FACTUAL_SIGNAL` o „potrebuji informace" — to je
        # whack-a-mole na POVRCH textu (prompt debt). Kotva se pta na neco
        # jineho: MA TA VETA PREDMET, o kterem se da neco dohledat?
        # Zmereno retro na 1 359 realnych zpravach z `human_chat`:
        #   seda zona 1 034 · z toho fallback -> volna bez signalu 374
        #   z tech 374 ma kotvu 62 (17 %) — „nastuduj vice o Jara Cimrman",
        #   „ukaz mi zdroj o Icon of the Seas", `namaluj X` (ty chytne prikaz
        #   driv, takze neskodi); zbylych 312 je skutecny small talk
        #   („to je pravda.", „ano, dame si kafe") a zustava volny.
        # Nejhorsi mozny vysledek teto vetve je ABSTINENCE, ne konfabulace.
        #
        # ⚠️ Prefix „<jmeno> se pta:" MUSI pryc — jinak by se kotvou stal
        # TAZATEL (tataz past jako HANS_PERSON_CARD_KC_FIX_V1, 19.8.).
        # `kotva_tematu` je cisty regex bez DB: zmereno 37 µs/volani.
        if _llm_nerozhodl:
            try:
                from scripts.hans_convindex import kotva_tematu as _kotva
                _cfg = self._config or {}
                _kp = (_cfg.get("known_persons", {}) or {})
                _vyn = tuple(_kp.keys()) + tuple(
                    str(v.get("nom", "")) for v in _kp.values())
                try:
                    from scripts.hans_persona import persona_name as _pn
                    _vyn += (str(_pn(_cfg) or ""),)
                except Exception:
                    pass
                _cista = re.sub(r"^\s*\S+\s+se\s+pt[áa]:\s*", "", msg)
                _t = _kotva(_cista, vynech=_vyn)
                if _t:
                    _log.info(
                        "intent: klasifikator nerozhodl, ale veta ma kotvu "
                        "%r → FAKTICKA (radsi grounding nez tichy vymysl)", _t)
                    return IntentResult(intent="udalost", confidence=0.4,
                                        source="fallback_kotva")
            except Exception as _e:
                _log.debug("fallback_kotva selhal (%s) → volna jako dosud", _e)
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

        ⚠️ POZOR, TENHLE ODSTAVEC DŘÍV LHAL: stálo tu „běží na MALÉM modelu
        PŘÍMO NA PI … musí fungovat i když je PC vypnuté". Od 5.8.
        (`HANS_INTENT_PC_V1`) to NEPLATÍ — `_ask_classifier` míří na
        `hans-czech` NA PC. Když je PC dole nebo zahlcené, tenhle detektor
        MLČÍ (None) a rozhoduje fallback v `classify`; přesně to 5.9. vyrobilo
        konfabulaci ve zkoušce #30. `is_about_self` má tutéž opravu.
        ⚠️ ZÁMĚRNĚ NEJDE přes `ollama_client.ollama_chat`: ten má vlastní
        retry/timeout politiku pro chat; klasifikace musí být krátká."""
        out = _ask_classifier(self._config, self._LLM_SYSTEM, msg)
        if out is None:
            return None
        low = (out or "").strip().lower()
        if low.startswith("volna") or low.startswith("volná"):
            return False
        if low.startswith("fakt"):
            return True
        _log.debug("intent LLM: nejednoznačné %r → keyword fallback", low[:30])
        return None


# ── HANS_SELF_STATE_V1 (5.8.) — „ptá se na MĚ?" jako sdílený detektor ────────
# Vzniklo v `hans_agent` jako brána proti tomu, aby router odpověděl na „jak se
# máš?" hlášením domácnosti. Teď to potřebuje i chat (grounded blok o sobě), tak
# je to tady na JEDNOM místě — druhé použití = čas vytáhnout to ze zdroje
# (jinak vzniknou dvě kopie s mírně jiným promptem).
#
# Proč vlastní prompt a ne obecné „faktický × volný": změřeno 4.8., že obecný
# klasifikátor „co se děje doma?" označí za volnou konverzaci → potlačil by
# legitimní dotaz na domácnost. Rozdíl „na MĚ × na DŮM" je jiná otázka a chce
# vlastní příklady — s nimi 12/12.
_SELF_SYSTEM = (
    "Rozhodni, \u010deho se t\u00fdk\u00e1 \u010desk\u00e1 ot\u00e1zka polo\u017een\u00e1 dom\u00e1c\u00edmu asistentovi.\n"
    "ASISTENT = pt\u00e1 se na N\u011aJ samotn\u00e9ho (jak se m\u00e1, co d\u011bl\u00e1, jeho n\u00e1lada, "
    "co je u n\u011bj nov\u00e9ho, co dnes d\u011blal).\n"
    "DUM = pt\u00e1 se na d\u011bn\u00ed v dom\u00e1cnosti (kdo je doma, co b\u011b\u017e\u00ed v televizi, "
    "co se d\u011bje doma).\n"
    # HANS_INTENT_THIRD_PERSON_V1 (18.8.) — T\u0158ET\u00cd KATEGORIE, ne dal\u0161\u00ed p\u0159\u00edklad.
    # Volba byla bin\u00e1rn\u00ed, tak\u017ee „kdo je Klára?“ nebyla ANI JEDNA mo\u017enost
    # a model ji tla\u010dil do ASISTENT. Zm\u011b\u0159eno 18.8.: 11/16, a chybovalo
    # p\u0159esn\u011b t\u011bch 5 dotaz\u016f na identitu t\u0159et\u00ed osoby. Soci\u00e1ln\u00ed guard pak
    # potla\u010dil stavovou akci u dotazu, kter\u00fd se Hanse v\u016fbec net\u00fdkal (C4, 7.8.).
    "OSOBA = pt\u00e1 se na KONKR\u00c9TN\u00cdHO \u010cLOV\u011aKA jin\u00e9ho ne\u017e asistent \u2014 kdo to je, "
    "co o n\u011bm v\u00ed, jak\u00fd je.\n\n"
    "P\u0159\u00edklady:\n„jak se máš?“ -> asistent\n„co děláš?“ -> asistent\n"
    "„co je u tebe nového?“ -> asistent\n„nudíš se?“ -> asistent\n"
    "„co jsi dnes dělal?“ -> asistent\n"
    "„kdo je doma?“ -> dum\n„co hraje na TV?“ -> dum\n"
    "„je někdo v pokoji?“ -> dum\n"
    "„co se děje doma?“ -> dum\n"
    "„kdo je Klára?“ -> osoba\n„co víš o Janě?“ -> osoba\n"
    "„kdo je Bud Spencer?“ -> osoba\n\n"
    "Odpov\u011bz JEDN\u00cdM slovem: asistent, dum nebo osoba."
)

_self_cache: dict = {}


# ── HANS_INTENT_CLF_DOWN_LOG_V1 (5.9.) ──────────────────────────────────────
# Selhani klasifikatoru se logovalo na DEBUG, takze v produkci NEBYLA ANI
# STOPA. Zkouska Kolacem #30 (5.9. 00:27) ukazala, proc to vadi: pod zatezi
# PC vypadne LEVNA POJISTKA (klasifikace, timeout 20 s) driv nez DRAHE
# GENEROVANI (chat, 120 s) — Hans odpovi, ale bez opory. Bezpecnost tedy
# degraduje PRED schopnosti a nikdo se to nedozvi.
# Throttle podle vzoru LOG_CIRCUIT_V1: prvni selhani hned, dalsi nejvys
# 1x/5 min s poctem, a po navratu jeden radek "OBNOVEN". Nocni vypnuti PC
# tim log nezaplavi.
_CLF_FAIL_N: int = 0
_CLF_FAIL_LAST_LOG: float = 0.0
_CLF_DOWN: bool = False
_CLF_LOG_EVERY_S: float = 300.0


def _clf_selhalo(exc) -> None:
    global _CLF_FAIL_N, _CLF_FAIL_LAST_LOG, _CLF_DOWN
    _CLF_FAIL_N += 1
    _CLF_DOWN = True
    now = time.time()
    if now - _CLF_FAIL_LAST_LOG >= _CLF_LOG_EVERY_S:
        _log.warning(
            "intent: klasifikator NEDOSTUPNY (%s) — %d selhani od posledniho "
            "hlaseni; dotazy v sede zone jedou na fallback", exc, _CLF_FAIL_N)
        _CLF_FAIL_LAST_LOG = now
        _CLF_FAIL_N = 0


def _clf_ok() -> None:
    global _CLF_FAIL_N, _CLF_DOWN
    if _CLF_DOWN:
        _log.info("intent: klasifikator OBNOVEN (%d tichych selhani predtim)",
                  _CLF_FAIL_N)
        _CLF_DOWN = False
        _CLF_FAIL_N = 0


def _ask_classifier(config: dict, system: str, message: str) -> Optional[str]:
    """HANS_INTENT_PC_V1 (5.8.) — jedno místo, kudy jdou klasifikační dotazy.

    Od 5.8. míří na `hans-czech` NA PC (dřív mini model na Pi). Změřeno:
    18/18 vs 11/16 (routing příkazů) a 15/15 vs 9/15 (dotaz na zdroj), a to
    2× rychleji (1,0–1,2 s vs 2,7 s). Původní obava „musí to jet i s vypnutým
    PC" na CHAT cestu NEPLATÍ — když je mozek dole, Hans neodpovídá tak jako
    tak. Noční cesty (ověřování nálezů) na Pi zůstávají.

    ⚠️ `keep_alive` MUSÍ být -1: hans-czech je rezidentní (KEEPALIVE_FIX_V2) a
    jakákoli hodnota v requestu mu keep_alive PŘEPÍŠE → model by po vypršení
    spadl z VRAM a chat by ho pak dotahoval znovu. Klasifikace tedy nesmí
    sáhnout na jeho residenci.

    ⚠️ Herní mód: teď jedeme po GPU na PC, takže respektuj `game_mode_on()`
    (na Pi to bylo jedno). V herním módu je chat stejně vypnutý.
    """
    ic = (config or {}).get("intent", {}) or {}
    try:
        from scripts.ollama_client import game_mode_on
        if game_mode_on():
            return None
    except Exception:
        pass
    import json as _json
    import urllib.request as _url
    body = _json.dumps({
        "model": ic.get("model", "hans-czech:latest"), "stream": False,
        "keep_alive": ic.get("keep_alive", -1),
        "options": {"num_predict": 5, "temperature": 0.0},
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": message}],
    }).encode("utf-8")
    try:
        url = str(ic.get("base_url", "http://127.0.0.1:11434")).rstrip("/")
        req = _url.Request(url + "/api/chat", body,
                           {"Content-Type": "application/json"})
        with _url.urlopen(req, timeout=int(ic.get("timeout", 20))) as r:
            _out = _json.loads(r.read())["message"]["content"]
        _clf_ok()          # HANS_INTENT_CLF_DOWN_LOG_V1
        return _out
    except Exception as e:
        _clf_selhalo(e)    # HANS_INTENT_CLF_DOWN_LOG_V1 (drive tichy DEBUG)
        return None


def self_topic(message: str, config: dict):
    """HANS_SELF_TOPIC_V1 (20.8.) — vrať KATEGORII, ne jen ano/ne.

    `_SELF_SYSTEM` rozlišuje `asistent` / `dum` / `osoba`, ale `is_about_self`
    z toho dělalo `bool` a zbylé dvě kategorie ZAHAZOVALO. Systém tu informaci
    už měl spočítanou a nikdo se jí nemohl zeptat — a právě to lámalo A1:
    „umíte pustit něco na televizi?" je `dum` (zmiňuje televizi), takže dotaz
    propadl na faktickou cestu, kde nemá oporu v RAG (má ji v PROMPTU) →
    self-consistency se rozešla → deterministická abstinence u otázky, jejíž
    odpověď měl Hans před sebou. Doloženo 20.8. (sim=0.789 thr=0.85).

    Vrací 'asistent' | 'dum' | 'osoba' | None (neznámo/vypnuto/selhání).
    """
    msg = (message or "").strip()
    if not msg:
        return None
    ic = (config or {}).get("intent", {}) or {}
    if not ic.get("use_llm", False):
        return None
    if msg in _self_cache:
        return _self_cache[msg]
    out = _ask_classifier(config, _SELF_SYSTEM, msg)
    if out is None:
        return None
    w = (out or "").strip().lower()
    res = ('asistent' if w.startswith('asist')
           else 'dum' if w.startswith('dum') or w.startswith('dům')
           else 'osoba' if w.startswith('osob') else None)
    if len(_self_cache) < 256:
        _self_cache[msg] = res
    return res


def is_about_self(message: str, config: dict) -> bool:
    """Ptá se zpráva na HANSE (jeho stav/náladu/činnost)?

    Klasifikuje `hans-czech` NA PC (od 5.8., viz `_ask_classifier`) — dřív tu stálo
    "mini model na Pi", což už neplatí a plete při ladění (herní mód klasifikaci
    shodí, protože PC je pak mimo). Vrací True JEN pro kategorii asistent —
    `dum` i `osoba` (HANS_INTENT_THIRD_PERSON_V1) jsou False.

    Selhání / nejednoznačná odpověď → False (chovej se jako dosud). Vypnutý
    `intent.use_llm` → False, žádný fallback na seznam slov: tenhle detektor
    JE ta náhrada za seznam slov."""
    msg = (message or "").strip()
    if not msg:
        return False
    ic = (config or {}).get("intent", {}) or {}
    if not ic.get("use_llm", False):
        return False
    # HANS_SELF_TOPIC_V1 — tenký obal nad `self_topic`, ať existuje JEDNA
    # pravda o téhle klasifikaci (a jedna cache). Chování beze změny:
    # True jen pro `asistent`.
    return self_topic(message, config) == 'asistent'


# HANS_CAST_NOT_ORDER_V1 (21.8.) — „kdo tam hraje?" je dotaz na FAKT, ne pokyn
# něco pustit. Vzor bydlí tady, protože ho potřebují DVĚ vrstvy: grounding
# (odpověz z knihovny) i agent (nenavrhuj akci) — a dvě kopie by se rozešly.
_OBSAZENI_PAT = __import__("re").compile(
    r"(kdo\s+(tam|v\s+tom|v\s+n[ěe]m|v\s+n[íi])?\s*(hraj|hr[áa]l|ú[čc]ink|"
    r"uc[íi]nk)|kdo\s+si\s+(tam\s+)?zahr[áa]l|obsazen[íi]|"
    r"kdo\s+to\s+(re[žz]|nato[čc])|kdo\s+hraje|kdo\s+re[žz][íi]roval)",
    __import__("re").IGNORECASE)


# ── HANS_SPEAKER_FACT_V1 / HANS_FRONTED_TOPIC_V1 (4.9.) ────────────────────
# Dva predikaty pro tutez tridu chyby: uzivatel rekne TEMA nebo FAKT O SOBE
# a Hans na to odpovi VYPISEM, ktery s tim nesouvisi. Bydli tady, protoze je
# potrebuji DVE ruzne vrstvy (agent + chatovy router) a dve kopie by se
# rozesly — stejny duvod jako u `pta_se_na_obsazeni` vyse.

# HANS_SPEAKER_FACT_V1 — veta rika, co ma rad MLUVCI (ne co ma Hans nastudovat).
# Doloženo 4.9. 3/3: „zapamatuj si ze mam rada mangu" -> agentni akce
# `add_study_topic` -> „Mam si manga zaradit ke studiu?". Router tema vytahne
# spravne, spatny je CIL: patri to do `person_interests`, ne do studia.
# ⚠️ ZAMERNE UZKE. Zmereno na 1294 realnych uzivatelskych replikach:
#   • siroka verze (i „zajima me", „miluju", „nesnasim") -> 10 shod a mezi nimi
#     „to mne zajima, zjisi vice." = SKUTECNA zadost o dohledani; veto by byla
#     regrese. „zajima me X" je dvojznacne (mam to rad x zjisti mi to), proto
#     se sem NEBERE — a rozsirovat to zpet bez noveho mereni NENI bezpecne.
#   • tahle verze -> 4 shody z 1294, vsechny jsou skutecne preference mluvciho
#     a ani jedna zadost o nastudovani.
# ⚠️ ZAPOR se zachytne schvalne NE: „nemam rad horory" je take fakt o mluvcim,
# ale zapsat ho jako ZAJEM by bylo obracene. (`\b` pred `mam` uz „nemam"
# vylucuje — je to vlastnost vzoru, ne nahoda, tak at se nezmeni omylem.)
_FAKT_O_MLUVCIM = re.compile(
    r"\bm[aá]m\s+(?:hodn[eě]\s+|moc\s+|nejv[ií]c\s+|opravdu\s+)?"
    r"(?:r[aá]d[aoy]?|nejrad[sš]i)\b"
    r"|\b(?:m[eě]|mn[eě])\b[^.?!]{0,24}\bbav[ií]\b"
    r"|\bbav[ií]\b[^.?!]{0,24}\b(?:m[eě]|mn[eě])\b",
    re.IGNORECASE)


def je_fakt_o_mluvcim(text: str) -> bool:
    """Rika veta, co ma rad / co bavi MLUVCIHO? (ne pokyn Hansovi)"""
    return bool(_FAKT_O_MLUVCIM.search(text or ""))


# HANS_REMEMBER_HONEST_V1 (5.9.) — VYSLOVNA ZADOST „pamatuj si, ze ...".
# Sdileny predikat (leaf modul) — obe vrstvy si ho importuji, dve kopie by se
# rozesly [[command-preempts-agent-same-message]].
# ⚠️ ZAMERNE UZKE: jen explicitni pokyn k zapamatovani, ne „mam rad X" samo
# o sobe. Zmereno na 1 359 realnych zpravach: 2 shody, a ANI JEDNA nebyla fakt
# o mluvcim (technicky fakt o HW a nakupni pripominka). Sirsi verze by
# unesla prave tyhle dva pripady, kam nepatri.
_ZADOST_ZAPAMATOVAT = re.compile(
    r"\b(?:za)?pamatuj\s+si\b"
    r"|\bzapi[šs]\s+si\b"
    r"|\bpoznamenej\s+si\b"
    r"|\bzapamatuj\s+(?:si\s+)?[,:]?\s*[žz]e\b",
    re.IGNORECASE)


def zada_o_zapamatovani(text: str) -> bool:
    """Zada uzivatel VYSLOVNE, aby si to Hans zapamatoval?"""
    return bool(_ZADOST_ZAPAMATOVAT.search(text or ""))


# HANS_FRONTED_TOPIC_V1 — VYTCENE TEMA (topikalizace): „a co detektivky, ty
# ctes?", „a rukodelna prace, cetl jsi o tom neco?". Tema stoji PRED carkou,
# zbytek vety uz je jen otazka — takze zadny extraktor tematu ho nenajde
# a vypisove prikazy vysypou vsechno bez ohledu na to, nac se ptal.
# ⛔ NEDELAT z toho obecne pravidlo „cokoliv pred carkou": ZMERENO na 1294
# realnych replikach -> 139 shod, drtiva vetsina UVODNI POZDRAV („Ahoj, koho
# jsi dneska videl?"). To je tataz past, na ktere 4.9. zkrachovala „ano/ne
# sonda za carkou" (viz HANDOFF_04_09). Proto jen DVA uzke tvary:
#   A) „[a] co TEMA," — otevrene vytceni
#   B) „TEMA, ... o tom/o ni/o nem" — vytceni s anaforou zpet na tema;
#      anafora musi byt JEDINY predmet „o ..." ve zbytku, jinak se odkazuje
#      jinam („ahoj, o knizce o designu jsme mluvili. co mi o ni reknes?").
# Plus filtry vynucene merenim: pozdrav/citoslovce, funkcni slova a slovesny
# tvar 2. osoby/rozkazu („vice nezjistuj,", „no vidis,").
# Vysledek: 6 shod z 1298 a vsech 6 je skutecna topikalizace.
_VYTK_CO = re.compile(
    r"^(?:a\s+|no\s+|tak\s+)?co\s+"
    r"((?:[\w\u00c0-\u017f]+\s*){1,3}?)\s*,\s*(.+)$", re.IGNORECASE)
_VYTK_HOLE = re.compile(
    r"^(?:a\s+|no\s+|tak\s+)?"
    r"((?:[\w\u00c0-\u017f]+\s*){1,3}?)\s*,\s*(.+)$", re.IGNORECASE)
_VYTK_ANAFORA = re.compile(
    r"\bo\s+(tom|t[eé]|n[eě][mj]|n[ií]|nich|t[eě]ch)\b", re.IGNORECASE)
_VYTK_POZDRAV = re.compile(
    r"^(ahoj|[cč]au|zdar|nazdar|zdrav[ií]m|dobr[yý]\s+(den|ve[cč]er)|"
    r"dobr[eé]\s+(r[aá]no|odpoledne)|hel[eé]|posly[sš]|hej|no|"
    r"dobr[yý]|dobr[eé]|zaj[ií]mav[eé]|d[ií]ky|d[eě]kuji|po[cč]kej)"
    r"(\s+\w+)?$", re.IGNORECASE)
_VYTK_SLOVESNY = re.compile(r"(?:[sš]|te|[uůaeií]j|li)$", re.IGNORECASE)
_VYTK_FUNKCE = re.compile(
    r"\b(se|si|jsi|jsem|jste|jsme|to|u[zž]|ne|ano|jen|m[eě]|mne)\b",
    re.IGNORECASE)


def _vytk_prijatelne(t: str) -> bool:
    sl = t.split()
    if not (1 <= len(sl) <= 3) or not any(len(w) >= 4 for w in sl):
        return False
    if _VYTK_POZDRAV.match(t) or _VYTK_FUNKCE.search(t):
        return False
    return not any(_VYTK_SLOVESNY.search(w) for w in sl)


def vytcene_tema(text: str) -> str:
    """Tema vytcene pred carkou („a co detektivky, ty ctes?" -> „detektivky").
    '' kdyz veta topikalizaci nema. Nikdy nevyhodi vyjimku."""
    s = (text or "").strip()
    if not s:
        return ""
    try:
        m = _VYTK_CO.match(s)
        if m and _vytk_prijatelne(m.group(1).strip()):
            return m.group(1).strip()
        m = _VYTK_HOLE.match(s)
        if not m:
            return ""
        t, zbytek = m.group(1).strip(), m.group(2).strip()
        if not _vytk_prijatelne(t) or not _VYTK_ANAFORA.search(zbytek):
            return ""
        if len(re.findall(r"\bo\s+\w+", zbytek)) != 1:
            return ""
        return t
    except Exception:
        return ""


def pta_se_na_obsazeni(text: str) -> bool:
    """Ptá se věta, KDO ve filmu hraje / kdo ho režíroval?"""
    return bool(_OBSAZENI_PAT.search(text or ""))
