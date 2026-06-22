#!/usr/bin/env python3
"""HANS_GUTENDEX_V1 — resolver knižních titulů na Project Gutenberg.

Přes veřejné Gutendex API (gutendex.com) najde k zadanému názvu knihy
plain-text URL (public domain). Preferuje jazyk, kterým Hans čte
(cs > en > de), protože Gutenberg je hlavně anglický a čeština chudá.

resolve_book(title) -> dict|None:
    {"id": "gut_<gutenberg_id>", "title", "author", "url", "lang"}
"""
from __future__ import annotations

import logging
import re
import urllib.parse
import urllib.request

_log = logging.getLogger("gutendex")

_API = "https://gutendex.com/books?search="
# Gutendex/Gutenberg blokuje requesty bez User-Agent (HTTP 403).
_UA = {"User-Agent": "Mozilla/5.0 (Hans book resolver; +local)"}
_LANG_PREF = ("cs", "en", "de")


def _norm(s: str) -> str:
    """Normalizace názvu pro porovnání (lowercase, bez diakritiky/interpunkce)."""
    s = (s or "").lower()
    repl = (("á", "a"), ("č", "c"), ("ď", "d"), ("é", "e"), ("ě", "e"),
            ("í", "i"), ("ň", "n"), ("ó", "o"), ("ř", "r"), ("š", "s"),
            ("ť", "t"), ("ú", "u"), ("ů", "u"), ("ý", "y"), ("ž", "z"))
    for a, b in repl:
        s = s.replace(a, b)
    return re.sub(r"[^a-z0-9 ]+", " ", s).strip()


def _plain_text_url(formats: dict) -> str:
    """Vrať text/plain URL knihy z formats. Vyhazuje readme/license/.zip,
    preferuje kanonický .txt.utf-8 / pgNNN.txt."""
    cands = []
    for k, u in formats.items():
        u = str(u)
        if "text/plain" not in k:
            continue
        low = u.lower()
        if low.endswith(".zip") or "readme" in low or "license" in low:
            continue
        cands.append(u)
    if not cands:
        return ""
    # utf-8 verze přednostně, pak pgNNN.txt, pak cokoliv
    for u in cands:
        if u.lower().endswith(".txt.utf-8") or "utf-8" in u.lower():
            return u
    for u in cands:
        if u.lower().endswith(".txt"):
            return u
    return cands[0]


def _score(result: dict, qnorm: str, lang_pref) -> tuple:
    """Řadicí klíč: (lang_rank, title_match, -popularita). Menší = lepší."""
    langs = result.get("languages", []) or []
    lang_rank = min((lang_pref.index(l) for l in langs if l in lang_pref),
                    default=len(lang_pref))
    tnorm = _norm(result.get("title", ""))
    # title match: 0=přesná shoda, 1=qnorm je podřetězec, 2=ostatní
    if tnorm == qnorm:
        tmatch = 0
    elif qnorm and (qnorm in tnorm or tnorm in qnorm):
        tmatch = 1
    else:
        tmatch = 2
    pop = result.get("download_count", 0) or 0
    return (lang_rank, tmatch, -pop)


def resolve_book(title: str, lang_pref=_LANG_PREF, timeout: int = 12) -> dict | None:
    """Najdi knihu na Gutenbergu. None když nenalezeno / bez plain-textu / chyba."""
    t = (title or "").strip()
    if not t:
        return None
    try:
        req = urllib.request.Request(
            _API + urllib.parse.quote(t), headers=_UA)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            import json
            data = json.load(r)
    except Exception as e:
        _log.warning("gutendex: dotaz '%s' selhal: %s", t, e)
        return None

    results = data.get("results", []) or []
    qnorm = _norm(t)
    # jen tituly s plain-text formátem
    cands = [b for b in results if _plain_text_url(b.get("formats", {}))]
    if not cands:
        _log.info("gutendex: '%s' bez plain-text výsledku (count=%s)",
                  t, data.get("count"))
        return None

    best = min(cands, key=lambda b: _score(b, qnorm, lang_pref))
    # odmítni úplně mimo (žádný překryv názvu) — chrání před náhodnou knihou
    if _score(best, qnorm, lang_pref)[1] >= 2:
        _log.info("gutendex: '%s' → nejbližší '%s' je mimo, odmítám",
                  t, best.get("title"))
        return None

    gid = best.get("id")
    authors = best.get("authors", []) or []
    langs = best.get("languages", []) or []
    return {
        "id": f"gut_{gid}",
        "title": best.get("title", t),
        "author": authors[0].get("name", "") if authors else "",
        "url": _plain_text_url(best.get("formats", {})),
        "lang": langs[0] if langs else "",
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    for q in ("Pride and Prejudice", "Ivanhoe", "Moby Dick", "neexistuje xyz123"):
        print(f"{q!r:30s} -> {resolve_book(q)}")
