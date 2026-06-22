#!/usr/bin/env python3
"""HANS_BOOK_MENTIONS_V1 — detekce zmínek knih v chatu → Gutenberg → wishlist.

Noční krok (vedle nitek/zájmů, sdílí _gather_dialogs). Z denních dialogů
vytáhne názvy knih, které někdo VÝSLOVNĚ zmínil, zkusí je najít na Gutenbergu
(public domain plain-text) a uloží do wishlistu S URL → reader je pak může
přečíst (nízká priorita). Anti-konfabulace: jen tituly reálně padlé v textu.

extract_book_mentions(config, diary_db_path, window_hours=26) -> int
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import time

_log = logging.getLogger("hans_book_mentions")

_SYSTEM = (
    "Jsi extraktor zmínek o KNIHÁCH z přepisu rozhovorů. Tvým úkolem je najít "
    "názvy knih, které člověk VÝSLOVNĚ zmínil (četl, chce číst, mluvil o nich). "
    "PŘÍSNĚ jen to, co v textu reálně padlo — NIC si nevymýšlej, nedoplňuj "
    "z obecné znalosti. Když žádná kniha nezazněla, vrať prázdné pole.\n"
    "Vrať POUZE JSON pole objektů: [{{\"title\": \"název knihy\", "
    "\"author\": \"autor pokud zazněl, jinak prázdné\"}}]. Žádný další text."
)


def _parse_list(raw: str) -> list:
    """Robustně vytáhni JSON pole z odpovědi LLM."""
    if not raw:
        return []
    s = raw.strip()
    # vykousni první [...] blok
    m = re.search(r"\[.*\]", s, re.DOTALL)
    if m:
        s = m.group(0)
    try:
        out = json.loads(s)
        return out if isinstance(out, list) else []
    except Exception:
        return []


def _known_titles(diary_db_path: str) -> set:
    """Tituly už v knihovně (jakýkoliv status) — nepřidávej znovu."""
    try:
        con = sqlite3.connect("file:%s?mode=ro" % diary_db_path, uri=True, timeout=3.0)
        rows = con.execute("SELECT book_title FROM hans_library").fetchall()
        con.close()
        return {(_norm_title(r[0])) for r in rows if r and r[0]}
    except Exception:
        return set()


def _norm_title(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def extract_book_mentions(config: dict, diary_db_path: str,
                          window_hours: float = 26.0) -> int:
    """Najdi zmíněné knihy → Gutenberg → wishlist. Vrací počet přidaných."""
    cfg = (config.get("book_mentions", {}) or {})
    if not cfg.get("enabled", True):
        return 0
    try:
        from scripts.hans_threads import _gather_dialogs
    except Exception as e:
        _log.warning("book_mentions: _gather_dialogs nedostupný: %s", e)
        return 0
    window_hours = float(cfg.get("window_hours", window_hours))
    since = time.time() - window_hours * 3600.0
    dialogs = _gather_dialogs(diary_db_path, since)
    if not dialogs:
        _log.info("book_mentions: žádné dialogy v okně, skip")
        return 0

    # slij všechny dialogy (zmínka knihy není vázaná na osobu)
    notes = []
    for v in dialogs.values():
        notes.extend(v)
    transcript = "\n---\n".join(notes[-40:])
    if not transcript.strip():
        return 0

    er = config.get("evening_reflection", {}) or {}
    model = str(cfg.get("model", er.get("model",
                "jobautomation/OpenEuroLLM-Czech:latest")))
    timeout = int(cfg.get("llm_timeout", 300))
    max_new = int(cfg.get("max_new_per_night", 3))
    lang_pref = tuple(cfg.get("lang_pref", ["cs", "en", "de"]))

    try:
        from scripts.ollama_client import ollama_generate
    except ImportError:
        _log.warning("book_mentions: ollama_client nedostupný, skip")
        return 0
    try:
        raw = ollama_generate(model=model, prompt="PŘEPIS ROZHOVORŮ:\n" + transcript,
                              system=_SYSTEM, config=config, timeout=timeout,
                              keep_alive=0,  # MODEL_KEEPALIVE_TIERS_V1
                              options={"temperature": 0.1})
    except Exception as e:
        _log.warning("book_mentions: LLM failed: %s", e)
        return 0

    items = _parse_list(raw)
    if not items:
        _log.info("book_mentions: žádná kniha nezmíněna")
        return 0

    from scripts.gutendex import resolve_book
    from scripts.hans_art import add_to_wishlist
    known = _known_titles(diary_db_path)
    added = 0
    seen_norm = set()
    for it in items:
        if added >= max_new:
            break
        title = ((it.get("title") if isinstance(it, dict) else str(it)) or "").strip()
        if not title or len(title) < 2:
            continue
        tn = _norm_title(title)
        if tn in known or tn in seen_norm:
            continue
        seen_norm.add(tn)
        # Gutenberg lookup (síť) — najdi public-domain text
        try:
            found = resolve_book(title, lang_pref=lang_pref)
        except Exception as e:
            _log.warning("book_mentions: resolve '%s' selhal: %s", title, e)
            continue
        if not found:
            _log.info("book_mentions: '%s' na Gutenbergu nenalezeno", title)
            continue
        res = add_to_wishlist(diary_db_path, found["title"],
                              url=found["url"], author=found.get("author", ""),
                              lang=found.get("lang", ""), book_id=found["id"])
        if res == "added":
            added += 1
            _log.info("book_mentions: '%s' → wishlist (%s, %s)",
                      title, found["id"], found["lang"])
    _log.info("book_mentions: přidáno %d knih na wishlist", added)
    return added


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    db = sys.argv[1] if len(sys.argv) > 1 else "data/hans_diary.db"
    print("přidáno:", extract_book_mentions(json.load(open("config.json")), db))
