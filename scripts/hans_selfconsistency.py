"""HANS_SELFCONSISTENCY_A1_V1 — sémantická entropie / self-consistency detektor.

Faktický dotaz vygeneruj N× (mírně vyšší temp), změř sémantickou shodu
odpovědí (bge-m3 kosinus). Rozcházejí se (průměrná párová podobnost pod
prahem) → nestabilní = pravděpodobná konfabulace fantomu / neznámého pojmu
→ ABSTINUJ (deterministicky, ne prompt).

Vůdčí princip [[anticonfabulation-guiding-principle]]: ROUTING, ne prompt —
u nestabilního faktického dotazu se volná generace persony NEpustí, odpoví
se deterministickou abstinencí. KOMPLEMENTÁRNÍ k RAG-first (#2) a entity
store (C1): A1 chytá NESTABILNÍ výmysl (fantom AJ II), #2/C1 sebejistě-
špatný (kolize jmen Sorge). De-risking 4.7. empiricky (hans-czech, N=5,
bge-m3): „Co je AJ II?" 0.627 (flag), „Kdo napsal Ivanhoe?" 0.963 (pass),
„Kdo byl Erich Sorge?" 0.898 (slepé místo = sebejistá nepravda). Práh ~0.85.

Deferral-safe: Ollama/embed dole nebo herní mód → None → přeskoč
(nikdy neblokuj a nekonfabuluj kvůli výpadku detektoru).
"""
from __future__ import annotations

import logging
import math
from typing import List, Optional

_log = logging.getLogger(__name__)


def _cfg(config: dict) -> dict:
    return (config.get("selfconsistency", {}) or {})


def _mean_pairwise_cosine(embs: List[list]) -> Optional[float]:
    """Průměr párové kosinové podobnosti napříč vektory. None při <2."""
    n = len(embs)
    if n < 2:
        return None

    def _dot(a, b):
        return sum(x * y for x, y in zip(a, b))

    def _norm(a):
        return math.sqrt(sum(x * x for x in a)) or 1e-9

    norms = [_norm(e) for e in embs]
    sims = []
    for i in range(n):
        for j in range(i + 1, n):
            sims.append(_dot(embs[i], embs[j]) / (norms[i] * norms[j]))
    return (sum(sims) / len(sims)) if sims else None


def _sample_answers(config: dict, question: str, model: str, n: int,
                    temperature: float, timeout: int,
                    num_predict: int) -> List[str]:
    """Vygeneruj N odpovědí na dotaz PARALELNĚ (Ollama serializuje na GPU,
    ale souběžné requesty neuškodí a zkrátí režii). Prázdné/None se zahodí."""
    try:
        from scripts.ollama_client import ollama_generate
    except Exception:
        return []
    _sys = ("Odpověz stručně a věcně na faktickou otázku. "
            "Pokud to nevíš, řekni že nevíš — nevymýšlej si.")

    def _one(_i):
        try:
            return ollama_generate(
                model, question, system=_sys, config=config,
                timeout=timeout, keep_alive=0,
                options={"temperature": temperature,
                         "num_predict": num_predict})
        except Exception:
            return None

    answers: List[str] = []
    try:
        import concurrent.futures as _cf
        with _cf.ThreadPoolExecutor(max_workers=min(n, 5)) as _ex:
            for _r in _ex.map(_one, range(n)):
                if _r and _r.strip():
                    answers.append(_r.strip())
    except Exception:
        for _i in range(n):
            _r = _one(_i)
            if _r and _r.strip():
                answers.append(_r.strip())
    return answers


def factual_stability(config: dict, question: str,
                      n: Optional[int] = None,
                      temperature: Optional[float] = None) -> Optional[float]:
    """Vrať průměrnou párovou kosinovou podobnost N odpovědí na faktický
    dotaz (bge-m3). None = nedostupné / příliš málo vzorků (deferral-safe)."""
    c = _cfg(config)
    q = (question or "").strip()
    if not q:
        return None
    max_chars = int(c.get("max_question_chars", 300))
    if len(q) > max_chars:
        return None  # dlouhý text = konverzace, ne krátký faktický dotaz
    n = int(n if n is not None else c.get("n_samples", 5))
    temperature = float(temperature if temperature is not None
                        else c.get("temperature", 0.7))
    timeout = int(c.get("timeout", 60))
    num_predict = int(c.get("num_predict", 120))
    model = str(c.get("model")
                or (config.get("models", {}) or {}).get("voice")
                or "hans-czech:latest")

    # herní mód / výpadek → skip (deferral-safe)
    try:
        from scripts.ollama_client import game_mode_on
        if game_mode_on():
            return None
    except Exception:
        pass

    answers = _sample_answers(config, q, model, n, temperature, timeout,
                              num_predict)
    if len(answers) < max(2, n - 1):
        return None  # moc málo vzorků (výpadek) → nespolehlivé → skip

    try:
        from scripts.hans_ideas import _embed_texts
        embs = _embed_texts(config, answers)
    except Exception:
        embs = None
    if not embs or len(embs) < 2:
        return None
    return _mean_pairwise_cosine(embs)


def is_unstable(config: dict, question: str) -> Optional[bool]:
    """True = nestabilní faktický dotaz (konfabulace → abstinuj),
    False = stabilní (nech projít), None = detektor nedostupný (skip)."""
    c = _cfg(config)
    if not c.get("enabled", False):
        return None
    thr = float(c.get("threshold", 0.85))
    sim = factual_stability(config, question)
    if sim is None:
        return None
    unstable = sim < thr
    _log.info("A1 self-consistency: sim=%.3f thr=%.2f → %s | %r",
              sim, thr,
              "NESTABILNI->abstinuj" if unstable else "stabilni",
              (question or "")[:60])
    return unstable
