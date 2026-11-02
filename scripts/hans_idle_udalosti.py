"""Metody třídy `HansIdle` přesunuté z `scripts/hans_idle.py` (ROZDELENI_METOD_V1).

Text metod je beze změny; jména původního modulu se čtou přes `_hi.` až při
volání. `HansIdle` má třídu `UdalostiMixin` mezi předky, takže volání přes `self` platí dál.
Import původního modulu je na KONCI souboru (kruhový import oběma směry).
"""
from __future__ import annotations

import os
import time


class UdalostiMixin:
    """Část třídy `HansIdle` — viz hlavička modulu."""

    def person_seen(self, names: list[str]):
        """Zavolej když kamera vidí osobu."""
        known = [n for n in names if n not in ("Unknown", "...", "?", "")]
        if known:
            # HANS_PROACTIVE_V1 — sleduj kdo je přítomen (empty→present = nové usazení)
            # ATTENTION_PRESENCE_RESET_V1 — resetuj arrival clock i po reálné
            # nepřítomnosti (mezera od posledního spatření > _absence_gap_s),
            # ne jen při prázdném seznamu. Jinak počítadlo „vidím N min" běží
            # dál i po návratu, protože presence visí až do idle (30 min).
            if (not self._present_names
                    or (time.time() - self._last_seen) > self._absence_gap_s):
                self._present_since = time.time()
                # HANS_COMMIT_ANNOUNCE_V1 — někdo se nově usadil → vhodná chvíle
                # dát vědět o slibech zpracovaných v noci (Telegram + hlas).
                # Nejdřív VÝSLEDKY splněných (cennější), pak nové sliby.
                try:
                    self._maybe_report_fulfilled(known)     # HANS_COMMIT_FULFILL_V1
                except Exception as _cre:
                    _hi._log.debug('commit report: %s', _cre)
                try:
                    self._maybe_deliver_reminders(known)    # HANS_COMMIT_FULFILL_V1
                except Exception as _cde:
                    _hi._log.debug('reminder deliver: %s', _cde)
                try:
                    self._maybe_announce_commitments(known)
                except Exception as _cae:
                    _hi._log.debug('commit announce: %s', _cae)
                # HANS_INSTANT_LOOKUP_V1 — oprava toho, co Hans odpověděl
                # provizorně a noční ověření to neuznalo. Jde PŘED sliby v
                # důležitosti, ale posílá se až tady, ať se dopoledne nesejde
                # všechno naráz.
                try:
                    self._maybe_announce_corrections(known)
                except Exception as _coe:
                    _hi._log.debug('lookup correction: %s', _coe)
            self._present_names = known
            # HANS_DIVACI_V1 — kdy koho kamera viděla naposledy (na gauči je
            # vidět jen nejbližší, ostatní za ním ne — uživatel 30. 10.)
            _vid = getattr(self, "_videni", None)
            if _vid is None:
                _vid = self._videni = {}
            for _n in known:
                _vid[_n] = time.time()
            # Mood: příchod osoby
            if hasattr(self, '_mood'):
                for name in known:
                    self._mood.person_arrived(name)
            self._last_seen = time.time()
            # WOL_ON_PRESENCE_V1 — osoba přišla → probuď PC (throttle uvnitř)
            if hasattr(self, '_routine') and hasattr(self._routine, 'wake_pc_on_presence'):
                try:
                    self._routine.wake_pc_on_presence()
                except Exception:
                    pass
            if self._idle_active:
                self._end_idle()
            # ATTENTION_PUBLISH_V1 — rychlý refresh pozornosti při detekci
            self._publish_attention()

    def tvare_poloha(self, boxes, names, postavy=None) -> None:
        """HANS_GAUC_POLOHA_V1 (30. 10.) — poslední normalizované boxy tváří
        (x1, y1, x2, y2) + měřicí záznam jednou za minutu do
        data/mereni/gauc/poloha.jsonl (vypínač hans_idle.gauc_mereni).
        Jen poloha a velikost — žádný obraz, žádný zápis do deníku."""
        now = time.time()
        self._tvare_boxy = (now, [tuple(round(float(v), 3) for v in b[:4]) for b in (boxes or [])])
        if not (boxes or postavy) or not bool((self.config.get("hans_idle", {}) or {}).get("gauc_mereni", True)):
            return
        if now - getattr(self, "_tvare_log_ts", 0.0) < 60:
            return
        self._tvare_log_ts = now
        try:
            import json as _j
            os.makedirs("data/mereni/gauc", exist_ok=True)
            with open("data/mereni/gauc/poloha.jsonl", "a", encoding="utf-8") as f:
                # štítky pro shluky (uživatel 30. 10.): herní mód = někdo u PC;
                # Kodi přehrává (z posledního dotazu návrhu filmu) = spíš gauč
                try:
                    from scripts.ollama_client import game_mode_on as _gm
                    _hra = bool(_gm())
                except Exception:
                    _hra = None
                # přímý dotaz á 60 s (stav z návrhu filmu se při neznámé tváři
                # nezjišťuje → hlásil „hraje“ i nad stojícím Kodi)
                try:
                    _kodi = bool(self.kodi.is_playing()) if self.kodi is not None else None
                except Exception:
                    _kodi = None
                f.write(_j.dumps({"ts": round(now), "hra": _hra, "kodi_hraje": _kodi,
                                  "jmena": [n for n in (names or []) if n],
                                  "tvare": [[round((b[0] + b[2]) / 2, 3), round((b[1] + b[3]) / 2, 3),
                                             round(b[2] - b[0], 3)] for b in self._tvare_boxy[1]],
                                  # postavy: [x1, y1, x2, y2] normalizovaně
                                  "postavy": [[round(float(v), 3) for v in b[:4]] for b in (postavy or [])]},
                                 ensure_ascii=False) + "\n")
        except Exception as e:
            _hi._log.debug("HANS_GAUC_POLOHA_V1: %s", e)

    def event_objects_seen(self, class_names: list, curiosity: bool = True):
        """Hans vidí objekty v záběru (list).

        Mood reaguje na celou scénu (všechny objekty),
        Curiosity reaguje jen na první objekt (zajímavý fakt o jedné věci).

        HANS_OBJDET_UNGATE_V1 (7.9.): `curiosity=False` nechá jen NÁLADU.
        Důvod je datový, ne výkonový — `trigger_object` volá
        `wikipedia_read` a cooldown je 2 h NA OBJEKT, takže po odgatování
        detekce (á 6 s) by šest tříd v pokoji vyrobilo až 72 čtení denně
        proti dnešním 30–60 celkem, a pořád o tomtéž nábytku. To teče do
        zaujetí → koníčků → Severky; tutéž patologii už jednou řešil
        HANS_ENGAGEMENT_DEDUP_V1. Výchozí True drží chování volajících,
        kteří parametr nepředávají.
        """
        if not class_names:
            return
        # Curiosity — jen první objekt, ať se Hans nepřehlcuje
        if curiosity and hasattr(self, '_curiosity') and self._curiosity is not None:
            try:
                first = class_names[0]
                if first:
                    self._curiosity.trigger_object(first)
            except Exception as _e:
                _hi._log.error("event_objects_seen.curiosity: %s", _e)
        # Mood — celá scéna
        if hasattr(self, '_mood') and self._mood is not None:
            try:
                self._mood.update_objects(class_names)
            except Exception as _e:
                _hi._log.error("event_objects_seen.mood: %s", _e)

    def event_unknown_person(self):
        """Hans viděl neznámou tvář. Posune mood k 'worried'.

        HANS_UNKNOWN_FACE_DEBOUNCE_V1 — ZNÁMÝ člověk občas na pár framů spadne
        do „unknown" (spodní úhel kamery, pohyb, světlo) → náladu to křečovitě
        překlápělo do worried, i když je před kamerou trvale ZNÁMÝ (doloženo:
        známý člen domácnosti viděn každých ~40 s, přesto 3× „neznámá tvář").
        Potlač „neznámou tvář" jako PŘECHODOVÝ výpadek rozpoznání, když byl
        známý člověk viděn nedávno. Reálný cizinec (nikdo známý přítomen, nebo
        dávno) projde. Bez potřeby kamery/přetrénování; laditelné z logů."""
        _win = float((self.config.get('hans_idle', {}) or {})
                     .get('unknown_face_suppress_s', 45))
        _last = getattr(self, '_last_seen', 0)
        _present = getattr(self, '_present_names', None)
        if _present and _win > 0 and (time.time() - _last) < _win:
            # HANS_FLICKER_LOG_THROTTLE_V1 — potlačení fírovalo per-frame
            # (~14k řádků/den → rotovalo pryč veškerou historii logu za ~1,5h).
            # Throttle: max 1 řádek / 60 s s počtem potlačených framů.
            self._flicker_suppressed = getattr(self, '_flicker_suppressed', 0) + 1
            _now = time.time()
            _last_log = getattr(self, '_flicker_log_ts', 0.0)
            if _now - _last_log >= 60.0:
                _hi._log.info("unknown_person POTLAČEN (flicker) — %d framů/60s, "
                          "známý %s přítomen", self._flicker_suppressed, _present)
                self._flicker_log_ts = _now
                self._flicker_suppressed = 0
            return
        if hasattr(self, '_mood') and self._mood is not None:
            try:
                self._mood.update_objects(["unknown_person"])
            except Exception as _e:
                _hi._log.error("event_unknown_person.mood: %s", _e)

    def _climate_now(self):
        """HANS_MOOD_CLIMATE_V1 — poslední teplota z čidel, nebo None.

        ⚠️ Stará hodnota se NEPOUŽIJE: mlčící uzel neznamená, že teplota drží.
        Okno je 1 h, stejné jako u agentní akce `_run_climate`.
        Průměr přes čidla (jsou dvě) a zaokrouhlení na celé stupně — DHT11 má
        ±2 °C, desetiny by byly vymyšlená přesnost. Hodnoty už jsou kalibrované
        (`HANS_CLIMATE_OFFSET_V1` se aplikuje při zápisu).
        """
        import sqlite3 as _sq
        cesta = "data/surroundings.db"
        if not os.path.exists(cesta):
            return None
        try:
            con = _sq.connect("file:%s?mode=ro" % cesta, uri=True, timeout=3.0)
            try:
                row = con.execute(
                    "SELECT AVG(teplota) FROM climate WHERE ts > ? "
                    "AND teplota IS NOT NULL", (time.time() - 3600.0,)).fetchone()
            finally:
                con.close()
        except Exception:
            return None
        if not row or row[0] is None:
            return None
        return int(round(row[0]))

    def event_weather_changed(self, code):
        """Update počasí — code je int podle wmo conv (viz HansMood.update_weather)."""
        if hasattr(self, '_mood') and self._mood is not None:
            try:
                self._mood.update_weather(code)
            except Exception as _e:
                _hi._log.error("event_weather_changed: %s", _e)

    def event_observation_context(self, context: str,
                                  source_type: str = "observation"):
        """Hans má kontext (popis scény) → curiosity může vygenerovat otázku."""
        if not context:
            return
        if hasattr(self, '_curiosity') and self._curiosity is not None:
            try:
                self._curiosity.trigger_question(context, source_type=source_type)
            except Exception as _e:
                _hi._log.error("event_observation_context: %s", _e)

    def event_face_enroll(self, name: str, session: str, samples_added: int):
        """Zaznamenat úspěšný quick augment do deníku.

        Voláno po confirmed augmentu (samples_added > 0).
        Diary entry vede session info pro tracking historie světelných podmínek.
        """
        if not name or samples_added <= 0:
            return
        try:
            import json as _json
            self._log_entry(
                "face_enroll",
                title=name,
                data=_json.dumps({
                    "session": session,
                    "samples_added": samples_added,
                }),
                note=f"Quick augment {name} ({session}): +{samples_added} vzorků")
        except Exception as _e:
            _hi._log.error("event_face_enroll: %s", _e)

    def _publish_attention(self):
        """ATTENTION_PUBLISH_V1 — publikuj bohatý kontext pozornosti pro
        dual_display_daemon (pravý displej). Cheap: paměťové atributy + 1
        levný DB read (titul knihy). Atomický JSON zápis. Renderer priorita:
        proactive > kolac > person > activity > mood."""
        try:
            import json as _json
            import os
            now = time.time()
            ctx = {}
            if hasattr(self, '_mood') and self._mood is not None:
                ctx['mood'] = self._mood.mood or 'content'
            ctx['clock'] = time.strftime('%H:%M')
            if hasattr(self, '_routine'):
                try:
                    ctx['phase'] = self._routine.phase_label
                except Exception:
                    ctx['phase'] = ''
            # proaktivní podnět — krátké okno po vyslovení
            pro = getattr(self, '_last_proactive', None)
            if pro and (now - pro[1]) < 25:
                ctx['proactive'] = pro[0]
            # Koláč dialog
            kd = getattr(self, '_hans_dialog', None)
            if kd is not None and getattr(kd, '_kolac_speaking', False):
                ctx['kolac'] = True
                ctx['kolac_speaking'] = True
            # osoba — jen přítomná, nedávno viděná, mimo idle
            names = getattr(self, '_present_names', None)
            if names and (now - self._last_seen) < 90 and not self._idle_active:
                ctx['person'] = names[0]
                ctx['person_min'] = int((now - getattr(self, '_present_since', now)) / 60)
            # aktivita (reading/watching; 'looking' baseline neukazujeme)
            try:
                act = self.current_activity_label()
            except Exception:
                act = None
            if act and act != 'looking':
                ctx['activity'] = act
                if act == 'reading' and hasattr(self, '_library'):
                    try:
                        bk = self._library.get_current_book()
                        if bk and bk.get('title'):
                            ctx['activity_label'] = bk['title']
                    except Exception:
                        pass
            path = 'data/avatar/attention_context.json'
            os.makedirs('data/avatar', exist_ok=True)
            # ATTENTION_PUBLISH_TMP_RACE_V1 — _publish_attention volá víc threadů
            # (person_seen z recognition, _tick z idle loopu); sdílený *.tmp +
            # os.replace závodily → Errno 2. Unikátní tmp per thread.
            import threading as _th
            tmp = '%s.%d.tmp' % (path, _th.get_ident())
            with open(tmp, 'w', encoding='utf-8') as f:
                _json.dump(ctx, f, ensure_ascii=False)
            os.replace(tmp, path)
        except Exception as _e:
            _hi._log.error('publish_attention: %s', _e)

# ROZDELENI_METOD_V1 — až na konci, viz hlavička
from scripts import hans_idle as _hi  # noqa: E402
