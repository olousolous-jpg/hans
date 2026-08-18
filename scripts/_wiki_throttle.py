"""HANS_WIKI_THROTTLE_V1 (18.8.) — rozestupy mezi dotazy na Wikimedii + respekt
`Retry-After`.

PROČ (doložený případ, ne teorie): cs.wikipedia řeže **9. dotazem během ~2 s**
(HTTP 429, `Retry-After: 36`). Jeden `_gather_material` ve studiu jich vypálí
9+ za sekundu (`_search_queries` × {prefixsearch, srsearch, článek} + anchor
fallback + `_en_title`). Výsledek 18.8.: 22 kol mezi 03:01 a 08:25, pokaždé
429 → `HANS_WIKI_TRANSIENT_V1` (správně) pokus nespálí → za minutu TÁŽ dávka
znovu → **livelock, 5 hodin, nula studia**. Sám guard proti spálení pokusu z
tvrdé chyby udělal nekonečnou smyčku; chybějící díl je TEMPO.

NENÍ to duplikát `_log_circuit` (ten jen tlumí ŠUM V LOGU z mrtvého endpointu,
requesty nezpomaluje). Tady jde o to dotazy vůbec nevypálit.

DVĚ VRSTVY:
  1. **token bucket** — krátkou dávku pustí, dlouhou srovná na udržitelné
     tempo. Drží nás pod kvótou, takže 429 většinou vůbec nenastane.
  2. **cooldown** — když 429 přece přijde, `Retry-After` se respektuje. Krátký
     zbytek se prospí, dlouhý se NEČEKÁ: volající dostane `WikiCooldown` a
     odloží se (vzor [[ollama-deferred-processing]]) — blokovat vlákno na
     desítky sekund je horší než zkusit to za minutu znovu.

Stav je MODULOVÝ (ne per-instance): `WebReader` se tvoří nový pro každé kolo
studia, takže per-instance cooldown by se pokaždé zapomněl. Thread-safe.

POUŽITÍ:
    from scripts import _wiki_throttle as _wt
    if _wt.is_wikimedia(url):
        _wt.acquire(url)          # zdrží; při dlouhém cooldownu vyhodí WikiCooldown
    r = sess.get(url, ...)
    _wt.note(r)                   # zaznamená 429/5xx + Retry-After
"""
from __future__ import annotations

import logging
import threading
import time
from urllib.parse import urlparse

_log = logging.getLogger("wiki_throttle")

# ZMĚŘENO 18.8. z Pi (tři sondy, ne odhad):
#   • 8 dotazů za 1,9 s → prošlo, 9. dostal 429   → burst má strop
#   • 2 dotazy/s        → 429 přišel u 14. po 29 s → kvóta ~13 v okně
#   • 1 dotaz/6 s       → 12× prošlo bez chyby     → tempo je udržitelné
# Limit tedy NENÍ „dotazů za sekundu", ale KVÓTA V OKNĚ. Proto token bucket:
# krátká dávka (interaktivní dohledání) projde svižně, dlouhá (noční sběr
# materiálu) se srovná na udržitelné tempo. Kapacita 8 < naměřených 13 a
# doplňování 1/3 s (0,33 dotazu/s) leží pod hranicí, která spadla (0,5/s),
# a nad tou, co bezpečně prošla (0,17/s).
CAPACITY = 8.0
REFILL_S = 3.0

# Zbytek cooldownu do téhle délky se vyplatí prospat (gather doběhne).
# Delší → odložit, ať nevisí vlákno.
MAX_BLOCK_S = 5.0
# Když server `Retry-After` nepošle.
DEFAULT_COOLDOWN_S = 40.0

_WMF_HOSTS = ("wikipedia.org", "wikisource.org", "wikidata.org",
              "wikimedia.org", "wiktionary.org")

_lock = threading.RLock()
_tokens: float = CAPACITY
_last_refill: float = time.time()
_cooldown_until: float = 0.0
_suppressed: int = 0


class WikiCooldown(Exception):
    """Wikimedia nás poslala do kouta a zbytek čekání je delší než MAX_BLOCK_S.
    Volající to má brát jako PŘECHODNÝ výpadek (odložit), ne jako
    „článek neexistuje"."""


def is_wikimedia(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return False
    return any(host == h or host.endswith("." + h) for h in _WMF_HOSTS)


def acquire(url: str = "") -> None:
    """Počkej, až se smí poslat další WMF dotaz. Vyhodí `WikiCooldown`, když nás
    server poslal do kouta a do konce okna je daleko (blokovat vlákno desítky
    sekund je horší než odložit a zkusit to za minutu)."""
    global _tokens, _last_refill, _suppressed
    with _lock:
        remain = _cooldown_until - time.time()
        if remain > 0:
            if remain > MAX_BLOCK_S:
                _suppressed += 1
                raise WikiCooldown("Wikimedia rate-limit, zbývá %.0f s" % remain)
            time.sleep(remain)
        while True:
            now = time.time()
            _tokens = min(CAPACITY, _tokens + (now - _last_refill) / REFILL_S)
            _last_refill = now
            if _tokens >= 1.0:
                _tokens -= 1.0
                return
            # Bucket prázdný → počkej na jeden token (max REFILL_S).
            time.sleep((1.0 - _tokens) * REFILL_S)


def note(resp) -> None:
    """Zaznamenej odpověď `requests`. Na 429 nastav cooldown z `Retry-After`."""
    try:
        code = int(getattr(resp, "status_code", 0) or 0)
    except Exception:
        return
    ra = (getattr(resp, "headers", {}) or {}).get("Retry-After")
    note_code(code, ra)


def note_code(code, retry_after=None) -> None:
    """Totéž pro volající, kteří nemají `requests` odpověď (urllib vyhodí
    HTTPError). Bez tohohle by jejich 429 zůstal buckets neviditelný."""
    global _cooldown_until, _suppressed
    try:
        code = int(code or 0)
    except (TypeError, ValueError):
        return
    if code != 429:
        return
    try:
        ra = float(retry_after or DEFAULT_COOLDOWN_S)
    except (TypeError, ValueError):
        ra = DEFAULT_COOLDOWN_S
    ra = max(1.0, min(ra, 300.0))
    with _lock:
        _cooldown_until = max(_cooldown_until, time.time() + ra)
        _log.warning("Wikimedia rate-limit (HTTP 429) → pauza %.0f s "
                     "(Retry-After)", ra)
        _suppressed = 0


def state() -> dict:
    """Pro diagnostiku / zdravotní kartu."""
    with _lock:
        return {"cooldown_s": max(0.0, _cooldown_until - time.time()),
                "suppressed": _suppressed,
                "tokens": round(_tokens, 2),
                "refill_s": REFILL_S}
