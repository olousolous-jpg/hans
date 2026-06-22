#!/usr/bin/env python3
"""
HANS_PROACTIVE_V1 — Proaktivní majordomus, FÁZE 1: engine proaktivní iniciace.

Inverze dosavadního chování: Hans dosud jedná JEN na spouštěč (vidí tvář /
zeptáš se). Tady se z PAMĚTI (rozjeté nitky) rozhodne promluvit SÁM OD SEBE,
nevyžádaně, k PŘÍTOMNÉ osobě — mimo čerstvý greeting.

Persona = diskrétní majordomus: promluví ZŘÍDKA, KRÁTCE, jen když to má cenu.
Proto SILNÉ mantinely (config `proactive`):
  - globální cooldown mezi proaktivními promluvami (cooldown_h, default 3h)
  - strop za den (max_per_day, default 2)
  - osoba musí být USAZENÁ (gate řeší volající přes settle_s)
  - jen 1 promluva za tick, jen když TTS nemluví (řeší volající)

Zdroj příležitostí V1 = DOZRÁLÉ NITKY (reuse ThreadStore.surface_for): vrátí
nejvhodnější otevřenou nitku přítomné osoby (per-nitka cap 3 + cooldown 12h
uvnitř surface_for → nepřekrývá se s greeting surfacingem téže nitky). V1
surface = nejdéle čekající otevřená nitka; literal date-maturation (parsování
„zkouška byla DNES") je až Fáze 2.

Doručení (V1, dle uživatele 16.6.): nitky HLASEM (TTS) — volající vezme text
a vysloví. Otázky (popup) nejsou součást V1 (zdroj = jen nitky).

Throttle stav je IN-MEMORY (reset na restartu) — ztráta není data-loss
([[ollama-deferred-processing]] se týká dat, ne throttlu): po restartu může
Hans promluvit nejvýš o jednu navíc, ne ztratit nitku (ta zůstává open v DB).

API:
  eng = ProactiveEngine(config, diary_db_path)
  eng.next_opportunity(present_names) -> (person, utterance, thread_id) | None
"""
from __future__ import annotations

import logging
import time
from datetime import date
from typing import List, Optional, Tuple

_log = logging.getLogger("hans_proactive")

_SKIP = {"Unknown", "...", "?", ""}


class ProactiveEngine:
    def __init__(self, config: dict, diary_db_path: str):
        self.config = config or {}
        self._diary_path = diary_db_path
        self._threads = None          # lazy ThreadStore
        # in-memory throttle stav
        self._last_fired_ts: float = 0.0
        self._fired_today: int = 0
        self._today: str = date.today().isoformat()

    # ── config ────────────────────────────────────────────────────────────────
    def _cfg(self) -> dict:
        return self.config.get("proactive", {}) or {}

    @property
    def enabled(self) -> bool:
        return bool(self._cfg().get("enabled", True))

    @property
    def cooldown_s(self) -> float:
        return float(self._cfg().get("cooldown_h", 3.0)) * 3600.0

    @property
    def max_per_day(self) -> int:
        return int(self._cfg().get("max_per_day", 2))

    # ── thread store (lazy) ─────────────────────────────────────────────────────
    def _thread_store(self):
        if self._threads is not None:
            return self._threads
        try:
            from scripts.hans_threads import ThreadStore
            self._threads = ThreadStore(self.config, self._diary_path)
        except Exception as e:
            _log.warning("ThreadStore init failed: %s", e)
            self._threads = None
        return self._threads

    # ── throttle ────────────────────────────────────────────────────────────────
    def _reset_daily_if_needed(self) -> None:
        today = date.today().isoformat()
        if today != self._today:
            self._today = today
            self._fired_today = 0

    def _throttle_allows(self) -> bool:
        self._reset_daily_if_needed()
        if not self.enabled:
            return False
        if self._fired_today >= self.max_per_day:
            return False
        if (time.time() - self._last_fired_ts) < self.cooldown_s:
            return False
        return True

    def _record_fired(self) -> None:
        self._last_fired_ts = time.time()
        self._fired_today += 1

    # ── hlavní rozhodnutí ─────────────────────────────────────────────────────
    def next_opportunity(
        self, present_names: List[str]
    ) -> Optional[Tuple[str, str, int]]:
        """Vrátí (person, utterance, thread_id) pro JEDNU proaktivní promluvu,
        nebo None. Po vrácení je throttle už započítán + nitka mark_surfaced —
        volající MUSÍ promluvu doručit (jinak se promarní okno)."""
        if not self._throttle_allows():
            return None
        store = self._thread_store()
        if store is None:
            return None
        known = [n for n in (present_names or []) if n and n not in _SKIP]
        for person in known:
            try:
                t = store.surface_for(person)
            except Exception as e:
                _log.warning("surface_for(%s) failed: %s", person, e)
                continue
            if not t:
                continue
            # THREAD_FOLLOWUP_CONTEXT_V1 — follow_up bývá vágní bez referentu
            # („co tě na tom napadlo?"); předřadíme TÉMA, ať otázka dává smysl
            # izolovaně. (Kořenově řeší kvalitu THREAD_EXTRACT_QUALITY_V1.)
            fu = (t.follow_up or "").strip()
            topic = (t.topic or "").strip()
            if topic and fu:
                utterance = u"Ještě k tématu „%s\". %s" % (topic, fu)
            elif fu:
                utterance = fu
            elif topic:
                utterance = u"Chtěl jsem se tě zeptat na %s." % topic
            else:
                continue
            try:
                store.mark_surfaced(t.id)
            except Exception as e:
                _log.warning("mark_surfaced(%s) failed: %s", t.id, e)
            self._record_fired()
            _log.info("Proaktivní příležitost pro %s: nitka #%s → %r",
                      person, t.id, utterance[:60])
            return (person, utterance, t.id)
        return None


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    dbp = sys.argv[1] if len(sys.argv) > 1 else "data/hans_diary.db"
    eng = ProactiveEngine({"proactive": {"cooldown_h": 0, "max_per_day": 99}}, dbp)
    names = sys.argv[2:] or ["alice"]
    print("next_opportunity:", eng.next_opportunity(names))
