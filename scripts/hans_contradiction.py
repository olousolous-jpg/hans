"""HANS_CONTRADICTION_A3_V1 — detekce rozporu PŘI ZÁPISU nového tvrzení.

Vůdčí princip [[anticonfabulation-guiding-principle]]: každé nové FAKTICKÉ
tvrzení, které Hans vygeneruje (syntéza, reflexe…) a chce si uložit do své
paměti, by mělo být PORONÁNO s tím, co už z groundovaných zdrojů ví. Když
tvrzení protiřečí (např. syntéza řekne „Sorge byl špion", ale entity store
má z Wikipedie „skladatel") → **flag** (informativní záznam v deníku), aby
se šum nešířil dál jako pravda.

Komplementární k **A2 imunitnímu systému**:
    * A2 běží NOČNÍ na Hansových REPLIKÁCH (human_chat, teddy_dialog);
      chytá tvrzení, která už existují a šíří se — a přidává korekci.
    * A3 běží PŘI ZÁPISU (syntéza, možná reflexe/reading takeaway); chytá
      NOVĚ vznikající tvrzení HNED, než se rozteče dál.

Reuse A2 primitiv:
    * `extract_claims(text)` — deterministický regex „<Jméno> je/byl …"
    * `_verdict(config, model, timeout, gloss, claim)` — LLM ROZPOR/SHODA
      (temp 0.0, fail-safe SHODA při nejistotě → přesnost > záběr)

Anti-konfabulace flagu samotného:
    * Kontroluje se JEN proti entity store (glosa = definiční věta VERBATIM
      ze zdroje; 0 % LLM úsudek). NE proti RAG chunkům (šum) ani parametrické
      znalosti LLM (to je kořen problému).
    * Neznámá entita → skip (nemáme co porovnat).
    * LLM verdikt nejistý → SHODA (jako A2).
    * LLM dole → None (žádný flag, deferred; A2 dožene v noci).

Konzervativní: A3 NEBLOKUJE zápis. Insight se stejně uloží. Flag jen
zaznamená rozpor pro pozdější revizi (v ideálním případě si Hans té
korekce sám všimne, případně A2 později syrupem přidá lesson_learned).

API:
    check_claim(config, db_path, text, source) -> Optional[dict]
    flag_to_diary(diary_writer, flag)
"""
from __future__ import annotations

import json
import logging
from typing import Optional

_log = logging.getLogger("hans_contradiction")


def _cfg(config: dict) -> dict:
    return (config.get("contradiction", {}) or {})


def is_enabled(config: dict) -> bool:
    return bool(_cfg(config).get("enabled", True))


def check_claim(config: dict, db_path: str, text: str,
                source: str = "unknown") -> Optional[dict]:
    """Zkontroluj, jestli text obsahuje tvrzení, které rozporuje ZNÁMOU
    entitu z Hansova čtení.

    Argumenty:
        config: hlavní config dict
        db_path: cesta k hans_diary.db (entity_store čte odsud)
        text: kandidátní text (syntézní insight, reflexe, …)
        source: krátký label kanálu (např. 'synthesis_idea'), zapíše se
                do flagu jako provenance

    Vrací:
        dict {entity, existing, new, source} — první nalezený rozpor,
                                                nebo None (bez rozporu /
                                                nelze zkontrolovat).

    NEmutuje entity store ani deník. Volající zapíše flag přes
    `flag_to_diary`, pokud chce.
    """
    if not is_enabled(config):
        return None
    if not text or not str(text).strip():
        return None

    try:
        from scripts.hans_immune import extract_claims, _verdict
    except Exception as e:
        _log.debug("A3: hans_immune import selhal: %s", e)
        return None
    try:
        from scripts.hans_entities import EntityStore
    except Exception as e:
        _log.debug("A3: hans_entities import selhal: %s", e)
        return None

    claims = extract_claims(str(text))
    if not claims:
        return None

    try:
        store = EntityStore(config, db_path)
    except Exception as e:
        _log.debug("A3: EntityStore init selhal: %s", e)
        return None

    c = _cfg(config)
    model = (c.get("model")
             or (config.get("evening_reflection", {}) or {}).get("model")
             or "jobautomation/OpenEuroLLM-Czech:latest")
    timeout = int(c.get("llm_timeout", 30))
    max_claims = int(c.get("max_claims_per_check", 6))

    checked = 0
    for ent_phrase, sentence in claims:
        if checked >= max_claims:
            break
        entity = store.resolve(ent_phrase)
        if not entity:
            continue  # neznámá entita — A2 to snad chytí později
        gloss = (entity.get("gloss") or "").strip()
        if not gloss:
            continue
        checked += 1
        rozpor = _verdict(config, model, timeout, gloss, sentence)
        if rozpor is True:
            _log.info(
                "A3: ROZPOR entita=%r existing=%r new=%r source=%s",
                entity.get("name"), gloss[:80], sentence[:80], source)
            return {
                "entity": entity.get("name"),
                "existing": gloss,
                "new": sentence,
                "source": source,
            }
    return None


def flag_to_diary(diary_writer, flag: dict) -> bool:
    """Zapiš rozpor jako event 'contradiction_flag' do deníku.

    `diary_writer` = callable `(event_type, title, *, note=None, data=None)`
    (viz `hans_idle._log_entry` / `hans_diary_writer`). Vrací True když
    zápis proběhl, False při selhání (nebo když flag=None).
    """
    if not flag or not diary_writer:
        return False
    try:
        data = json.dumps({
            "existing": flag.get("existing", ""),
            "new": flag.get("new", ""),
            "source": flag.get("source", ""),
        }, ensure_ascii=False)
        diary_writer("contradiction_flag",
                     flag.get("entity", "") or "",
                     data=data)
        return True
    except Exception as e:
        _log.debug("A3: flag_to_diary selhal: %s", e)
        return False


__all__ = ["is_enabled", "check_claim", "flag_to_diary"]
