"""HANS_QUERY_REWRITER_F1_V1 — překlad „člověk→počítač" na faktické cestě.

Vůdčí princip [[anticonfabulation-guiding-principle]]: faktický registr
groundovaný, imaginativní volný. Rewriter sedí NA FAKTICKÉ CESTĚ mezi
uživatelem a retrievalem (entity store + RAG). Persona (hans-czech)
DÁL slyší SYROVÝ vstup uživatele — bytost, ne asistent — ale „počítač"
(vyhledávání) čte VYČIŠTĚNÝ, explicitní dotaz.

Co dělá:
    * opraví překlepy / zjevný neduh v dotazu
    * rozřeší odkazy („ten spisovatel co jsem zmiňoval", „namaluj HO",
      „o čem jsme se bavili") přes conv historii → doplní konkrétní jméno
    * strhne výplňkové fráze („prosím řekni mi", „zajímalo by mě")
    * vytáhne JÁDRO otázky

Co NEdělá:
    * nepřidává fakta, která v dotazu ani historii NEJSOU (další místo
      generace → musí drženo krátce, omezeno num_predict + prompt)
    * neuniverzalizuje překlad celé konverzace (to by zabilo personu)
    * nemění text, který jde do persony (viz volající — ten dostane raw)

Kdy běží:
    * jen když volající (openwebui_direct_handler._build_grounding) už
      rozhodl, že intent JE faktický (a NENÍ opinion)
    * +1 LLM volání jen na faktický dotaz (opinion/volná konverzace bez
      overhead)

Deferral-safe: LLM dole / prázdný výstup / delší než rozumné → vrátí
None → volající použije originál (bez rewriteru = starý chod).
"""
from __future__ import annotations

import logging
import re
from typing import Optional

_log = logging.getLogger(__name__)


def _cfg(config: dict) -> dict:
    return (config.get("rewriter", {}) or {})


def is_enabled(config: dict) -> bool:
    return bool(_cfg(config).get("enabled", False))


# ── Deterministický pre-cleanup ──────────────────────────────────────────────
# Krátké výplňkové otevřeníčka (case-insensitive, na začátku dotazu).
# Rozšíření není třeba být kompletní — LLM krok dorazí zbytek.
_FILLER_LEAD = re.compile(
    r"^\s*(prosím\s+|prosim\s+|hele\s+|hej\s+|"
    r"(mohl(a)?\s+bys|můž(eš|es)|mohl(a)?\s+byste)\s+(mi\s+)?"
    r"(prosím\s+|prosim\s+)?(říct?|rict|povědět|povedet|říci|říci\s+mi)\s+"
    r"|řekni\s+mi\s+|rekni\s+mi\s+|"
    r"zajímalo\s+by\s+mě\s+|zajimalo\s+by\s+me\s+|"
    r"chci\s+vědět\s+|chci\s+vedet\s+"
    r")",
    re.IGNORECASE,
)


def _pre_clean(text: str) -> str:
    """Rychlé deterministické oříznutí výplňkových otevřeníček. Neruší
    obsah, jen slupku. Bezpečné (nesází fakta). Vrátí PŘESNĚ originál,
    když nic k odříznutí není (žádná kapitalizace / normalizace)."""
    t0 = (text or "").strip()
    t = t0
    prev = None
    # opakovaně — někdo napíše „prosím řekni mi zajímalo by mě…"
    while t and t != prev:
        prev = t
        t = _FILLER_LEAD.sub("", t, count=1).strip()
    if t == t0:
        return t0
    # něco jsme odřízli — kapitalizuj první písmeno pro čistý vzhled
    if t and t[0].islower():
        t = t[0].upper() + t[1:]
    return t


# ── Historie do promptu ──────────────────────────────────────────────────────
def _compact_history(history: list, max_msgs: int, max_chars: int) -> str:
    """Vezmi posledních N msg z conv historie, oříznout každou na max_chars.
    Formát: 'osoba: …' / 'Hans: …' po řádcích. Prázdné → ''.
    """
    if not history or max_msgs <= 0:
        return ""
    msgs = [m for m in history if isinstance(m, dict) and m.get("content")]
    if not msgs:
        return ""
    tail = msgs[-max_msgs:]
    lines = []
    for m in tail:
        role = m.get("role", "")
        content = str(m.get("content", "")).strip()
        if not content:
            continue
        if len(content) > max_chars:
            content = content[:max_chars].rstrip() + "…"
        label = "Hans" if role == "assistant" else "Uživatel"
        lines.append(f"{label}: {content}")
    return "\n".join(lines)


# ── LLM prompt ───────────────────────────────────────────────────────────────
_SYSTEM = (
    "Jsi tichá vrstva mezi uživatelem a vyhledáváním. "
    "Přepíšeš uživatelovu otázku do JEDNÉ explicitní věty vhodné pro "
    "vyhledávání ve znalostní bázi.\n\n"
    "PRAVIDLA (dodrž všechna):\n"
    "1) Rozřeš odkazy jako 'ten X co jsem zmiňoval', 'on/ona', 'to co "
    "jsi říkal', 'o čem jsme se bavili' — dosaď KONKRÉTNÍ jméno/téma "
    "z KONTEXTU HISTORIE, JEN pokud tam jednoznačný referent JE.\n"
    "2) Když referent v historii JEDNOZNAČNĚ NENÍ, otázku NECH TAK JAK "
    "JE (bez odkazu). Radši nepřepsat, než vymyslet cíl.\n"
    "3) Oprav zjevné překlepy. Zbav výplňků ('prosím', 'řekni mi', "
    "'zajímalo by mě').\n"
    "4) NEPŘIDÁVEJ žádná fakta, tvrzení ani domněnky. Jenom přepis.\n"
    "5) Zachovej JAZYK vstupu (typicky česky) a otázkovou formu, "
    "pokud vstup byl otázka.\n"
    "6) Vrať POUZE přepsanou otázku, jednu větu, bez uvozovek, "
    "bez komentářů, bez 'OTÁZKA:', bez markdownu."
)


def _build_user_prompt(history_block: str, question: str) -> str:
    parts = []
    if history_block:
        parts.append("KONTEXT HISTORIE (poslední výměny):\n" + history_block)
    parts.append("OTÁZKA:\n" + question)
    return "\n\n".join(parts)


# ── Sanitace výstupu ─────────────────────────────────────────────────────────
_STRIP_PREFIX = re.compile(
    r"^\s*(otázka|otazka|přepis|prepis|dotaz|query)\s*[:\-–]\s*",
    re.IGNORECASE,
)


def _sanitize_output(raw: Optional[str], original: str,
                     max_expand: float) -> Optional[str]:
    """Očisti LLM výstup na 1 řádek bez ozdob. Vrať None při podezření na
    drift (moc dlouhé, prázdné, komentářové).
    """
    if not raw:
        return None
    t = raw.strip()
    # vyhoď „přemýšlecí" bloky a odezvy typu Q/A
    t = re.sub(r"<think>.*?</think>", "", t, flags=re.DOTALL | re.IGNORECASE)
    t = _STRIP_PREFIX.sub("", t.strip()).strip()
    # jen první řádek (LLM občas přidá komentář na 2. řádek)
    t = t.splitlines()[0].strip() if t else ""
    # strip krajní uvozovky
    t = t.strip("„""'\"'`")
    if not t:
        return None
    # ochrana proti divokému rozšíření — bezpečná horní hranice
    max_len = max(int(len(original) * max_expand) + 60, 120)
    if len(t) > max_len:
        return None
    # ochrana proti odpovědění na otázku místo přepisu (drift persony)
    if _looks_like_answer(t, original):
        return None
    return t


def _looks_like_answer(candidate: str, original: str) -> bool:
    """Hrubý detektor, že LLM MÍSTO PŘEPISU otázku ZODPOVĚDĚL.
    Signály: (a) původ byl otázka, přepis ne, (b) přepis obsahuje typické
    „odpovědní" formulky. Konzervativní (radši false-negative → propustí)."""
    orig = (original or "").strip()
    cand = (candidate or "").strip()
    if not cand:
        return False
    orig_q = orig.endswith("?")
    cand_q = cand.endswith("?")
    if orig_q and not cand_q:
        low = cand.lower()
        answer_markers = (
            "je ", "byl ", "byla ", "narodil", "žil ", "působ",
            "znamená", "jedná se ", "jde o ",
        )
        if any(low.startswith(m) or (" " + m) in low[:40]
               for m in answer_markers):
            return True
    return False


# ── Volání LLM ───────────────────────────────────────────────────────────────
def _llm_rewrite(config: dict, question: str, history_block: str) -> Optional[str]:
    """Zavolej ollama_generate s constrained promptem. Deferral-safe: None
    znamená „nepřepisuj, použij originál"."""
    try:
        from scripts.ollama_client import ollama_generate
    except Exception as e:
        _log.debug("F1: ollama_client import selhal (%s)", e)
        return None

    c = _cfg(config)
    model = c.get("model") or "hans-czech:latest"
    timeout = int(c.get("timeout", 30))
    num_predict = int(c.get("num_predict", 60))
    num_ctx = int(c.get("num_ctx", 2048))
    temperature = float(c.get("temperature", 0.1))

    try:
        raw = ollama_generate(
            model,
            _build_user_prompt(history_block, question),
            system=_SYSTEM, config=config,
            timeout=timeout, keep_alive=0,
            options={"temperature": temperature,
                     "num_predict": num_predict,
                     "num_ctx": num_ctx})
    except Exception as e:
        _log.debug("F1: LLM volání selhalo (%s)", e)
        return None
    return raw


# ── Public API ───────────────────────────────────────────────────────────────
def rewrite_for_retrieval(config: dict, text: str,
                          history: Optional[list] = None,
                          name: Optional[str] = None) -> Optional[str]:
    """Přepiš uživatelův dotaz na explicitní retrieval dotaz.

    Vrací:
        str  — přepsaný dotaz (použij pro entity store + RAG)
        None — nepoužívej rewriter, drž se originálu (výpadek / edge case /
               podezřelý výstup / disabled)

    Volající JE ZODPOVĚDNÝ za fallback na originál při None. Persona
    (chat generace) DOSTÁVÁ VŽDY ORIGINÁL, ne přepis.
    """
    if not is_enabled(config):
        return None

    raw_text = (text or "").strip()
    if not raw_text:
        return None

    c = _cfg(config)
    max_input = int(c.get("max_input_chars", 300))
    if len(raw_text) > max_input:
        # dlouhý text = konverzace nebo esej, ne krátký faktický dotaz —
        # rewriter tady nedává smysl a stojí LLM čas
        return None

    # 1) deterministický pre-cleanup (jistý zisk bez rizika konfabulace)
    pre = _pre_clean(raw_text)

    # 2) historie do promptu (pro coreference resolution)
    history_msgs = int(c.get("history_msgs", 4))
    history_chars = int(c.get("history_max_chars", 200))
    hist_block = _compact_history(history or [], history_msgs, history_chars)

    # 3) LLM přepis — constrained
    llm_raw = _llm_rewrite(config, pre, hist_block)
    if not llm_raw:
        # LLM dole/timeout → vrať aspoň pre-cleaned (když se od raw liší)
        if pre and pre != raw_text:
            return pre
        return None

    max_expand = float(c.get("max_expand_ratio", 2.5))
    clean = _sanitize_output(llm_raw, pre, max_expand)
    if not clean:
        if pre and pre != raw_text:
            return pre
        return None

    # pokud je LLM výstup identický (case-insens.) s originálem, není co hlásit
    if clean.lower().strip("?. ") == raw_text.lower().strip("?. "):
        # ale pre-cleaned varianta se od raw může lišit (výplňky pryč) →
        # pošli pre-cleaned pro nižší latenci downstreamu
        if pre and pre != raw_text:
            return pre
        return None

    return clean


__all__ = ["is_enabled", "rewrite_for_retrieval"]
