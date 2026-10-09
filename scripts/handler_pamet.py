"""Metody třídy `OpenWebUIDirectHandler` přesunuté z `scripts/openwebui_direct_handler.py` (ROZDELENI_METOD_V1).

Text metod je beze změny; jména původního modulu se čtou přes `_h.` až při
volání. `OpenWebUIDirectHandler` má třídu `PametMixin` mezi předky, takže volání přes `self` platí dál.
Import původního modulu je na KONCI souboru (kruhový import oběma směry).
"""
from __future__ import annotations

from pathlib import Path
import json
import logging


class PametMixin:
    """Část třídy `OpenWebUIDirectHandler` — viz hlavička modulu."""

    def _skip_memory(self, name: str) -> bool:
        """HANS_VOICE_NO_MEMORY_V1 (28. 9.) — výměna nejde do deníku, reflexe
        ani RAG: testovací identita, NEBO hlas, dokud je přepis nespolehlivý.
        Změřeno 28. 9.: z 9 hlasových vět v historii 8 nesmysl z přepisu
        (Whisper base) — „Půst film prelátor“ → chat_reflection „zájem o film
        Prelátor“. Rozhovor v conv_store (vlákno) zůstává. Přepnout
        `voice.remember` na true po nasazení přesnějšího přepisu."""
        if self._is_test_person(name):
            return True
        try:
            if _h.get_current_channel() == "voice":
                if not bool((self.config.get("voice", {}) or {}).get("remember", False)):
                    return True
                # HANS_STT_TURBO_V1 (29. 9.) — přepis ze zálohy (base) je
                # nespolehlivý (70 % chyb) → do paměti ne, jen do vlákna.
                from scripts.voice_listener import posledni_prepis_zalohou
                if posledni_prepis_zalohou():
                    return True
        except Exception:
            pass
        return False

    def _is_test_person(self, name: str) -> bool:
        """HANS_TEST_PERSON_V1 — je tohle testovací identita?
        ⚠️ Zápis chatu do deníku má DVĚ cesty (tenhle helper pro early-return
        větve a hlavní zápis na konci `send_chat_message`) — proto predikát,
        ne kopie kontroly ve dvou místech. První verze hlídala jen helper
        a řádek se stejně zapsal ([[test-the-fix-not-the-symptom]])."""
        try:
            _tp = [str(x).strip().lower()
                   for x in (self.config.get("test_persons") or [])]
            if (name or "").strip().lower() in _tp:
                logging.getLogger(_h.__name__).info(
                    "HANS_TEST_PERSON_V1: %r je testovací identita — "
                    "do deníku ani RAG se nezapisuje", name)
                return True
        except Exception:
            pass
        return False

    def _log_human_chat_to_diary(self, name: str, user_message: str,
                                 response: str,
                                 bypass_kind: str = None) -> None:
        """HANS_CHAT_DIARY_ALL_PATHS_V1 (18.7.) — early-return cesty (slash cmd,
        agent akce, source bypass, deepen…) obchází standardní diary write na
        konci `send_chat_message` → chat se do `human_chat` nezapíše →
        reflexe/self_insight/audit ho neuvidí (viz Telegram Rimmer-paint 21:16).
        Extract do helper, volat u KAŽDÉ early-return cesty.

        HANS_BYPASS_TRACE_V1 (19.7.) — `bypass_kind` (např. 'sources',
        'knowledge_check') označí, že odpověď NEPROŠLA persona finetunem —
        šla deterministickou šablonou mimo LLM. Zápis do `data` sloupce
        (JSON) + samostatný `bypass_note` s importance=7 (surfacing v
        night_reflection / self_insight). Bez toho persona finetune svá
        vlastní „mimotělní" sdělení nezná → kognitivní dissonance při
        čtení vlastního deníku ([[bypass-self-reflection]])."""
        if not response:
            return
        # HANS_TEST_PERSON_V1 (19.8.) — rozhovor vedený pod TESTOVACÍ identitou
        # se do paměti nezapisuje. Důvod je doložený: 19.8. jsem uklidil deník
        # i RAG od vyvrácené fabulace a o minutu později ji tam vrátil vlastním
        # ověřovacím rozhovorem — druhý den to vypadalo jako návrat bugu
        # ([[test-the-fix-not-the-symptom]], bod 6). Chat funguje normálně
        # (Hans odpovídá, historie vlákna se drží v conv_store, takže se dá
        # testovat i navazování), jen se z toho nestává „co Hans ví".
        # ⚠️ ZÁMĚRNĚ jen tenhle jeden zápis: `human_chat` je zdroj pro deník,
        # reflexe i RAG, takže vynechání tady utne celou větev naráz.
        if self._skip_memory(name):                    # HANS_VOICE_NO_MEMORY_V1
            return
        try:
            _note = f"{name}: {user_message}\nHans: {response}"
            _data = None
            if bypass_kind:
                import json as _json
                _data = _json.dumps({"bypass": 1, "kind": bypass_kind},
                                    ensure_ascii=False)
            _hi = getattr(self, "_hans_idle", None)
            if _hi and hasattr(_hi, "_log_entry"):
                _hi._log_entry("human_chat", name, note=_note,
                               data=(_data or ""))
            else:
                # fallback: přímý SQL
                import sqlite3 as _sql, time as _t
                _diary = (self.config.get("diary_db", "data/hans_diary.db")
                          if hasattr(self, "config") else "data/hans_diary.db")
                with _sql.connect(_diary) as _db:
                    _db.execute(
                        "INSERT INTO diary (ts, event_type, title, note, data) "
                        "VALUES (?,?,?,?,?)",
                        (_t.time(), "human_chat", name, _note, _data))
                    _db.commit()
            # bypass_note (surfaced) — samostatný event pro noční reflexi.
            if bypass_kind:
                self._write_bypass_note(bypass_kind, response)
        except Exception as _e:
            logging.getLogger(_h.__name__).debug(
                "human_chat diary write (early-return): %s", _e)

    def _write_bypass_note(self, kind: str, response: str) -> None:
        """HANS_BYPASS_TRACE_V1 — samostatný diary event (importance=7) o
        deterministické odpovědi. Vzor pro všechny bypass cesty (dřív inline
        v sources_answer bloku, teď sdíleno). Hans si to přečte v ranní
        reflexi / self_insight → má šanci si všimnout, že odpověď šla mimo
        jeho obvyklou úvahu."""
        try:
            import sqlite3 as _sq, time as _tm, json as _json
            _diary = (self.config.get("diary_db", "data/hans_diary.db")
                      if hasattr(self, "config") else "data/hans_diary.db")
            _snippet = (response[:140] + "…") if len(response) > 140 else response
            _kind_label = {
                "sources":         "source query",
                "knowledge_check": "knowledge check",
                "instant_lookup":  "okamžité dohledání",
            }.get(kind, kind)
            # HANS_INSTANT_LOOKUP_V1 — u dohledání NESMÍ zápis tvrdit „výpis
            # z paměti": opak je pravdou (v paměti to nebylo, proto se hledalo)
            # a Hans si tyhle poznámky čte v noční reflexi/self_insight → chybný
            # popis by ho učil nepravdivý příběh o sobě.
            if kind == "instant_lookup":
                _note = ("Odpověděl jsem přes deterministickou cestu (bypass mimo "
                         "mou obvyklou personu). V paměti jsem k tomu NIC neměl, "
                         "tak jsem to v tu chvíli dohledal a odpověděl PROVIZORNĚ "
                         "— do paměti jsem si nic nezapsal, čeká to na noční "
                         "ověření. Odpověď: „%s\"" % _snippet)
            else:
                _note = ("Odpověděl jsem přes deterministickou cestu (bypass mimo "
                         "mou obvyklou personu — přímý výpis z paměti). Nešlo o "
                         "vlastní úvahu, ale o vyzvednutí uloženého faktu. "
                         "Odpověď: „%s\"" % _snippet)
            _data = _json.dumps({"kind": kind}, ensure_ascii=False)
            _c = _sq.connect(_diary, timeout=5.0)
            _c.execute(
                "INSERT INTO diary (ts, event_type, title, note, data, importance) "
                "VALUES (?,?,?,?,?,?)",
                (_tm.time(), "bypass_note",
                 "Bypass odpověď (%s)" % _kind_label, _note, _data, 7))
            _c.commit(); _c.close()
        except Exception as _bne:
            logging.getLogger(_h.__name__).debug(
                'bypass_note write: %s', _bne)

    def _upload_chat_memory(self, name: str, question: str, answer: str):
        """HANS_CHAT_RECALL_V1 — verbatim rozhovor do RAG (hans_pripady), aby byl
        později sémanticky dohledatelný. Threadovaně, deferral-safe."""
        _kn = getattr(self, "knowledge", None)
        if _kn is None or not (question or "").strip():
            return
        import threading as _th
        import time as _t
        ts = _t.time()           # HANS_CLAIM_CHECK_V1 — id záznamu známé hned
        doc_id = f"chatlog_{int(ts)}_{name}"

        def _work():
            try:
                from scripts.hans_persona import persona_name
                pname = persona_name(self.config)
            except Exception:
                pname = "Hans"
            import datetime as _dt
            when = _dt.datetime.fromtimestamp(ts).strftime("%A %-d.%-m.%Y %H:%M")
            # HANS_CHATLOG_NOT_FACT_V1 — původ přímo v textu, ať je i pro
            # člověka (a pro každou budoucí cestu) zřejmé, že tohle NENÍ
            # ověřená znalost, ale co Hans v hovoru řekl.
            text = (f"Rozhovor s {name} ({when}):\n"
                    f"[NEOVĚŘENO — vlastní výrok v hovoru, ne ověřený fakt]\n"
                    f"{name}: {question.strip()}\n{pname}: {answer.strip()}")
            try:
                _kn.upload(
                    collection_key="hans_pripady",
                    doc_id=doc_id,
                    title=f"Rozhovor s {name}: {question.strip()[:60]}",
                    text=text,
                    metadata={"kdy": when, "osoba": name, "typ": "rozhovor",
                              # HANS_CHATLOG_NOT_FACT_V1
                              "overeno": False, "puvod": "vlastni_vyrok"})
            except Exception as _e:
                print(f"[Chat] chat memory upload (worker): {_e}")
        _th.Thread(target=_work, daemon=True, name="ChatMemoryUpload").start()
        return doc_id

    @staticmethod
    def _extract_read_topic(msg: str, url: str) -> str:
        """HANS_READ_URL_NL_V1 — z uživatelovy zprávy vytáhni TÉMA čtení
        (na co se ptá), aby web_read neslo neurčité 'url'. Priorita:
        uvozovkovaný termín → 'o <termín>' → 'url' fallback."""
        import re as _re
        m = (msg or "").replace(url, " ")
        # 1) termín v uvozovkách („X" / "X" / 'X')
        q = _re.search(r"[\"'„»“]([^\"'„»“”«]{2,40})[\"'“”«]", m)
        if q and q.group(1).strip():
            return q.group(1).strip()[:40]
        # 2) „o [jazyku/tématu/…] <termín>" (1-2 slova)
        o = _re.search(
            r"\bo\s+(?:jazyku|jazyce|t[ée]matu|str[áa]nce|filmu|knize|"
            r"projektu|autorovi|m[eě]st[eě]|)\s*"
            r"([A-Za-zÁ-Žá-ž0-9][\wÁ-Žá-ž]{2,30}(?:\s+[A-Za-zÁ-Žá-ž0-9]"
            r"[\wÁ-Žá-ž]{2,30})?)", m, _re.IGNORECASE)
        if o and o.group(1).strip():
            return o.group(1).strip()[:40]
        return "url"

    def _save_note(self, name: str, note_text: str):
        """Uloží poznámku do known_persons[name].notes v config.json."""
        try:
            config_path = Path("config.json")
            with open(config_path, encoding="utf-8") as f:
                cfg = json.load(f)

            persons = cfg.setdefault("known_persons", {})
            if name not in persons:
                persons[name] = {"gender": "", "notes": ""}
            if not isinstance(persons[name], dict):
                persons[name] = {"gender": "", "notes": str(persons[name])}

            existing = persons[name].get("notes", "").strip()
            if existing:
                persons[name]["notes"] = existing + " " + note_text
            else:
                persons[name]["notes"] = note_text

            with open(config_path, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=4, ensure_ascii=False)

            # Aktualizuj živý config aby se projevilo hned
            self.config.setdefault("known_persons", {}).setdefault(
                name, {"gender": "", "notes": ""})
            if isinstance(self.config["known_persons"][name], dict):
                existing_live = self.config["known_persons"][name].get("notes", "").strip()
                if existing_live:
                    self.config["known_persons"][name]["notes"] = (
                        existing_live + " " + note_text)
                else:
                    self.config["known_persons"][name]["notes"] = note_text

            print(f"[Chat] /note uložena pro '{name}': {note_text}")
        except Exception as e:
            print(f"[Chat] /note save error: {e}")

# ROZDELENI_METOD_V1 — až na konci, viz hlavička
from scripts import openwebui_direct_handler as _h  # noqa: E402
