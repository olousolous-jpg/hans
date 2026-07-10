#!/usr/bin/env python3
"""
HANS_IMMUNE_A2_V1 — automatický imunitní systém proti konfabulaci (A2).

Noční fact-check Hansových VLASTNÍCH tvrzení proti jeho groundovanému čtení
(entity store = definiční věty VERBATIM ze zdrojů). Whack-a-mole → sebečistící
smyčka: konfabulaci nechytá člověk, ale Hans sám, další noc.

Cílí na doloženou třídu chyb „Sorge" ([[synthesis-phantom-fake-citations]]):
parametrická paměť LLM přebije vlastní groundované čtení („Erich Sorge byl
sovětský špion" místo „církevní hudebník a skladatel"). C1 to řeší u OTÁZKY
v chatu; A2 to dočišťuje u VÝROKŮ — i na cestách, kam C1 grounding neteče
(dialogy s Koláčem, volné repliky).

Tok (1×/noc):
  1. posbírej Hansovy repliky z human_chat + teddy_dialog za okno,
  2. deterministicky vytáhni tvrzení tvaru „<Jméno> je/byl …",
  3. resolvuj entitu proti store (striktně — jen ZNÁMÉ entity z čtení),
  4. LLM porovná tvrzení s glosou (ROZPOR/SHODA; nejistota → SHODA =
     fail-safe, přesnost > záběr; temp 0.0, base model, keep_alive=0),
  5. ROZPOR → `lesson_learned` do deníku (claim/correction/lesson) —
     existující wiring ho surfacuje v chatu (lessons_ctx) I v Koláčově
     dialogu (_build_hans_solo_system) → smyčka uzavřená.

KONZERVATIVNÍ: NEMAŽE deníkové záznamy (filozofie paměti — omyl zůstává
zaznamenán), jen přidá korekci. Dedup per entita (14 dní). Deferral-safe:
LLM dole/herní mód → 'deferred' → guard se nenastaví → retry příští noc.

API:
  run_immune_check(config, diary_db_path) -> 'checked'|'idle'|'deferred'
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from typing import Optional

_log = logging.getLogger("hans_immune")

# ── sběr Hansových replik ────────────────────────────────────────────────────

_SOURCE_TYPES = ("human_chat", "teddy_dialog")


def _gather_hans_lines(db_path: str, since: float,
                       persona: str = "Hans") -> list:
    """Hansovy vlastní repliky (řádky „{persona}: …") z dialogových eventů.
    Vrací list textů (bez prefixu jména). Read-only."""
    conn = None
    out = []
    pref = re.compile(r"^\s*%s\s*:\s*(.+)$" % re.escape(persona),
                      re.IGNORECASE)
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True,
                               timeout=3.0)
        qmarks = ",".join("?" * len(_SOURCE_TYPES))
        rows = conn.execute(
            f"SELECT note FROM diary WHERE event_type IN ({qmarks}) "
            f"AND ts > ? AND note IS NOT NULL ORDER BY ts DESC LIMIT 200",
            (*_SOURCE_TYPES, since)).fetchall()
        for (note,) in rows:
            for line in str(note).splitlines():
                m = pref.match(line)
                if m and len(m.group(1).strip()) > 15:
                    out.append(m.group(1).strip())
    except Exception as e:
        _log.warning("immune: sběr replik selhal: %s", e)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    return out


# ── deterministická extrakce tvrzení „<Jméno> je/byl …" ─────────────────────

_CAP = r"[A-ZÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ][\wěščřžýáíéúůďťňó\-]+"
_CLAIM_PAT = re.compile(
    r"(?<![\w„])(%s(?:\s+%s){0,3})\s+(?:je|jsou|byla?|bylo|byli)\b" % (_CAP, _CAP))

# běžná větná zájmena/příslovce s velkým písmenem na začátku věty — ne entity
_NOT_ENTITY = {"to", "ten", "ta", "tohle", "tady", "teď", "ano", "ne", "já",
               "vy", "on", "ona", "ono", "my", "moje", "vaše", "jeho", "její",
               "dnes", "možná", "pokud", "když", "ale", "avšak", "ovšem",
               "právě", "each", "nicméně", "podle", "proto", "takže", "co",
               "jak", "kde", "kdy", "kdo", "pane", "pán"}


def _split_sentences(text: str) -> list:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text or "")
            if len(s.strip()) > 10]


def extract_claims(text: str) -> list:
    """[(entity_phrase, věta), …] — tvrzení o pojmenované entitě.
    Deterministicky (regex), šum filtruje až store-resolve u volajícího."""
    out = []
    for sent in _split_sentences(text):
        for m in _CLAIM_PAT.finditer(sent):
            ent = m.group(1).strip()
            first = ent.split()[0].lower()
            if first in _NOT_ENTITY:
                continue
            # jednoslovná entita na ZAČÁTKU věty = nejčastěji obyčejné slovo
            # s velkým písmenem („Kniha je…") → přeskoč (víceslovné projdou)
            if " " not in ent and m.start() == 0:
                continue
            out.append((ent, sent))
    return out


# ── LLM verdikt ROZPOR/SHODA ────────────────────────────────────────────────

_VERDICT_SYSTEM = (
    "Jsi přísný kontrolor faktů. Dostaneš FAKT (definiční věta ověřená ze "
    "zdroje) a TVRZENÍ o téže entitě. Rozhodni, zda si věcně PROTIŘEČÍ — "
    "jiná identita, profese, role, národnost, doba, místo (např. FAKT: "
    "skladatel, TVRZENÍ: špion = ROZPOR). Odpověz JEDNÍM slovem:\n"
    "ROZPOR — tvrzení odporuje faktu\n"
    "SHODA — tvrzení je s faktem slučitelné, jen ho doplňuje, nebo se ho "
    "netýká\n"
    "Když si nejsi jistý, odpověz SHODA.")


def _verdict(config: dict, model: str, timeout: int,
             gloss: str, claim_sentence: str) -> Optional[bool]:
    """True=ROZPOR, False=SHODA, None=LLM dole (deferred)."""
    try:
        from scripts.ollama_client import ollama_generate
    except Exception as e:
        _log.warning("immune: ollama_client nedostupný: %s", e)
        return None
    raw = ollama_generate(
        model=model,
        prompt=f"FAKT: {gloss}\nTVRZENÍ: {claim_sentence}",
        system=_VERDICT_SYSTEM,
        config=config, timeout=timeout, keep_alive=0,
        options={"temperature": 0.0, "num_predict": 8})
    if raw is None:
        return None
    return "rozpor" in raw.strip().lower()


# ── dedup + zápis lekce ─────────────────────────────────────────────────────

def _recent_immune_entities(db_path: str, days: float = 14.0) -> set:
    """Entity, ke kterým už imunitní lekce v okně existuje (ne 2× totéž)."""
    conn = None
    out = set()
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True,
                               timeout=3.0)
        rows = conn.execute(
            "SELECT data FROM diary WHERE event_type='lesson_learned' "
            "AND title='immune' AND ts > ?",
            (time.time() - days * 86400,)).fetchall()
        for (d,) in rows:
            try:
                out.add((json.loads(d or "{}").get("entity") or "").lower())
            except Exception:
                pass
    except Exception:
        pass
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    return out


def _write_lesson(db_path: str, entity: str, gloss: str,
                  claim_sentence: str) -> bool:
    lesson = (f"U „{entity}“ se držím toho, co jsem si SÁM přečetl: {gloss} "
              f"Svou dřívější odlišnou verzi neopakuji — vlastní záznam má "
              f"přednost před domněnkou.")
    try:
        db = sqlite3.connect(db_path, timeout=5.0)
        db.execute(
            "INSERT INTO diary (ts, event_type, title, note, data) "
            "VALUES (?,?,?,?,?)",
            (time.time(), "lesson_learned", "immune", lesson,
             json.dumps({"entity": entity, "claim": claim_sentence[:300],
                         "correction": gloss[:300], "zdroj": "immune_a2"},
                        ensure_ascii=False)))
        db.commit()
        db.close()
        _log.info("immune: ROZPOR u „%s“ → lesson_learned", entity)
        return True
    except Exception as e:
        _log.warning("immune: zápis lekce selhal: %s", e)
        return False


# ── top-level ────────────────────────────────────────────────────────────────

def run_immune_check(config: dict, diary_db_path: str) -> str:
    """Noční imunitní kontrola. 'checked' (běželo) / 'idle' (nic ke kontrole
    nebo vypnuto) / 'deferred' (LLM dole → guard nenastavovat, retry)."""
    cfg = (config or {}).get("immune", {}) or {}
    if not cfg.get("enabled", True):
        return "idle"
    window_h = float(cfg.get("window_hours", 48))
    max_checks = int(cfg.get("max_checks_per_night", 6))
    er = (config or {}).get("evening_reflection", {}) or {}
    model = str(cfg.get("model", er.get(
        "model", "jobautomation/OpenEuroLLM-Czech:latest")))
    timeout = int(cfg.get("llm_timeout", 300))

    # entity store — bez něj není proti čemu kontrolovat
    try:
        from scripts.hans_entities import EntityStore
        store = EntityStore(config, diary_db_path)
        if store.count() == 0:
            return "idle"
    except Exception as e:
        _log.warning("immune: entity store nedostupný: %s", e)
        return "idle"

    try:
        from scripts.hans_persona import persona_name
        pname = persona_name(config)
    except Exception:
        pname = "Hans"

    lines = _gather_hans_lines(diary_db_path, time.time() - window_h * 3600.0,
                               persona=pname)
    if not lines:
        return "idle"

    # kandidáti: tvrzení, jejichž entita je ZNÁMÁ ze čtení (striktní resolve)
    seen_entities = set()
    already = _recent_immune_entities(diary_db_path)
    candidates = []          # (entity_name, gloss, věta)
    for text in lines:
        for ent_phrase, sent in extract_claims(text):
            try:
                ent = store.resolve(ent_phrase)
            except Exception:
                ent = None
            if not ent:
                continue
            name = (ent.get("name") or "").strip()
            gloss = (ent.get("gloss") or "").strip()
            key = name.lower()
            if not name or not gloss:
                continue
            if key in seen_entities or key in already:
                continue
            seen_entities.add(key)
            candidates.append((name, gloss, sent))

    if not candidates:
        _log.info("immune: %d replik, žádné kontrolovatelné tvrzení", len(lines))
        return "idle"

    checked = 0
    contradictions = 0
    for name, gloss, sent in candidates[:max_checks]:
        v = _verdict(config, model, timeout, gloss, sent)
        if v is None:
            # LLM dole uprostřed běhu → co je hotové, je hotové; zbytek příště
            if checked == 0:
                return "deferred"
            break
        checked += 1
        if v:
            if _write_lesson(diary_db_path, name, gloss, sent):
                contradictions += 1

    # observabilita: 1 souhrnný event za běh (jen když se reálně kontrolovalo)
    try:
        db = sqlite3.connect(diary_db_path, timeout=5.0)
        db.execute(
            "INSERT INTO diary (ts, event_type, title, note) VALUES (?,?,?,?)",
            (time.time(), "immune_check", "",
             f"Noční kontrola vlastních tvrzení: {checked} ověřeno, "
             f"{contradictions} rozporů opraveno lekcí."))
        db.commit()
        db.close()
    except Exception:
        pass
    _log.info("immune: %d tvrzení ověřeno, %d rozporů", checked, contradictions)
    return "checked"


# ── Smoke (python3 -m scripts.hans_immune) — bez LLM ─────────────────────────
if __name__ == "__main__":
    print("=== extract_claims ===")
    samples = [
        "Erich Sorge byl sovětský špion, který působil v Japonsku.",
        "Ano, pane. Hercule Poirot je belgický detektiv z románů Agathy "
        "Christie. Mám ho rád.",
        "Kniha je skvělá. Dnes je hezky. To je pravda.",
        "Cardiffský hrad je středověký hrad ve Walesu.",
        "Myslím, že Richard Sorge byl novinář.",
    ]
    for s in samples:
        print(f"  {s[:60]!r} → {extract_claims(s)}")

    import json as _j
    cfg = {}
    try:
        cfg = _j.load(open("config.json", encoding="utf-8"))
    except Exception:
        pass
    db = cfg.get("diary_db", "data/hans_diary.db")
    print("\n=== _gather_hans_lines (48h, reálná DB) ===")
    ls = _gather_hans_lines(db, time.time() - 48 * 3600)
    print(f"  {len(ls)} replik")
    for l in ls[:3]:
        print("  -", l[:100])

    print("\n=== kandidáti (bez LLM) ===")
    try:
        from scripts.hans_entities import EntityStore
        store = EntityStore(cfg, db)
        n = 0
        for text in ls:
            for ent_phrase, sent in extract_claims(text):
                ent = store.resolve(ent_phrase)
                if ent:
                    n += 1
                    print(f"  {ent['name']!r} ← {sent[:90]!r}")
        print(f"  celkem {n} kontrolovatelných tvrzení")
    except Exception as e:
        print("  store:", e)
