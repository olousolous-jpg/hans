"""Metody třídy `HansIdle` přesunuté z `scripts/hans_idle.py` (ROZDELENI_METOD_V1).

Text metod je beze změny; jména původního modulu se čtou přes `_hi.` až při
volání. `HansIdle` má třídu `OznameniMixin` mezi předky, takže volání přes `self` platí dál.
Import původního modulu je na KONCI souboru (kruhový import oběma směry).
"""
from __future__ import annotations

import time


class OznameniMixin:
    """Část třídy `HansIdle` — viz hlavička modulu."""

    def _maybe_announce_corrections(self, known: list):
        """HANS_INSTANT_LOOKUP_V1 (4.8.) — ranní OPRAVA provizorní odpovědi.

        Když Hans na neznámé téma odpověděl z okamžitého dohledání a noční
        ověření nález ZAMÍTLO (jiné heslo / nesedící název / model řekl NE),
        do paměti se nic nezapsalo — ale uživatel tu odpověď už slyšel. Tohle
        mu to přijde říct. Idempotence přes DB flag `announced` (stejný vzor
        jako HANS_COMMIT_ANNOUNCE_V1), doručení přes Notifier (Matrix) + hlas.
        Jen osobě, které odpověď patřila, a jen v denním okně."""
        if not known:
            return
        hour = time.localtime().tm_hour
        if hour < 9 or hour >= 22:
            return  # tiché okno
        dbp = ((self.config.get('hans_idle', {}) or {})
               .get('diary_db', 'data/hans_diary.db'))
        try:
            from scripts.hans_findings import (unannounced_corrections,
                                               mark_announced, correction_text)
        except Exception:
            return
        # HANS_CORRECTION_PRESENT_FIRST_V1 (28. 9.) — dřív limit=5 PŘED filtrem
        # přítomných: 5 nejstarších zamítnutí patřilo „Uživatel“ a testovacím
        # identitám (nikdy doma) → opravy skutečné osoby (13. a 16. 9.) se na
        # řadu nedostaly nikdy (announced=0 u všech 9). Filtr až nad celou frontou.
        rows = unannounced_corrections(dbp, limit=200)
        if not rows:
            return
        present = {str(n).strip().lower() for n in known}
        mine = [r for r in rows
                if str(r.get('asker') or '').strip().lower() in present]
        if not mine:
            return  # oprava patří někomu, kdo tu není → počká na něj
        row = mine[0]                      # po jedné, ať to není litanie
        asker = str(row.get('asker') or '')
        msg = correction_text(row, asker, self.config)
        delivered = False
        try:
            tg = (getattr(self.chat, 'telegram', None)
                  if getattr(self, 'chat', None) else None)
            if tg is not None and getattr(tg, 'enabled', False):
                _send = getattr(tg, 'send_proactive', None) or getattr(tg, 'send', None)
                if _send:
                    _send(msg)
                    delivered = True
        except Exception as _te:
            _hi._log.debug('correction notify: %s', _te)
        try:
            _tts = getattr(getattr(self, "_hans_dialog", None), "tts", None)
            _isp = getattr(_tts, "is_speaking", None) if _tts else None
            if _tts and getattr(_tts, 'enabled', False) and not (
                    callable(_isp) and _isp()):
                _tts.speak(msg)
                delivered = True
        except Exception as _ve:
            _hi._log.debug('correction voice: %s', _ve)
        # Označ JEN když aspoň jeden kanál prošel — jinak se zkusí při dalším
        # usazení (oprava se nesmí ztratit, to je celý smysl smyčky).
        if delivered:
            mark_announced(dbp, [row['id']])
            _hi._log.info("instant_lookup: odeslána ranní oprava k '%s' (%s)",
                      row.get('topic'), asker)

    def _maybe_announce_commitments(self, known: list):
        """HANS_COMMIT_ANNOUNCE_V1 — když se člověk usadí, dej mu proaktivně
        vědět o slibech, které Hans zpracoval v noci (extrakce běží v noční
        reflexi). Kanály: Telegram (send_proactive) + hlas (TTS, když je někdo
        u kamery). Jen přes den (mimo tiché okno 22–9). Oznamuje POUZE sliby
        PŘÍTOMNÉ osoby (slib jedné osoby nedostane jiná). Idempotence přes DB flag
        `announced` → každý slib oznámí právě jednou (žádný denní gate)."""
        if not known:
            return
        hour = time.localtime().tm_hour
        if hour < 9 or hour >= 22:
            return  # tiché okno — počká na denní usazení
        dbp = ((self.config.get('hans_idle', {}) or {})
               .get('diary_db', 'data/hans_diary.db'))
        try:
            from scripts.hans_commitments import (unannounced_commitments,
                                                  mark_announced, _norm)
        except Exception:
            return
        rows = unannounced_commitments(dbp, limit=20)
        if not rows:
            return
        # jen sliby PŘÍTOMNÝCH osob (r = (id, person, text))
        present = {_norm(n) for n in known}
        # COMMIT_COND_V1 — připomínka s podmínkou `alone` se ozve, JEN když je
        # dotyčný v místnosti sám. Připomínka v nevhodnou chvíli je horší než
        # žádná: měření rozpoznávání se dá udělat jen s jedním člověkem doma
        # (s víc lidmi chybí ground truth — doloženo 11.8.).
        # COMMIT_COND_V1 — podmínka `alone` se ptá, kdo je DOMA, ne kdo je
        # zrovna v záběru. Původní verze brala jen aktuálně viditelné osoby:
        # 12.8. to vyšlo (druhá osoba dorazila o 6 minut později), ale kdyby byla
        # ve vedlejším pokoji, připomínka by padla taky — a měření, které má
        # smysl jen o samotě, by bylo znehodnocené.
        _alone = None
        if any(len(r) > 3 and r[3] == 'alone' for r in rows):
            try:
                import sqlite3 as _sq3
                _win = float((self.config.get('reminder', {}) or {}).get(
                    'alone_window_min', 45)) * 60.0
                _cx = _sq3.connect("file:%s?mode=ro" % dbp, uri=True, timeout=5.0)
                _seen = {str(x[0]).lower() for x in _cx.execute(
                    "SELECT DISTINCT title FROM diary WHERE event_type='person_seen' "
                    "AND ts > ?", (time.time() - _win,))}
                _cx.close()
                _alone = (len(_seen) <= 1)
                if not _alone:
                    _hi._log.info("COMMIT_COND_V1: připomínka 'jen o samotě' čeká — "
                              "za posledních %.0f min viděn(i) %s",
                              _win / 60, ", ".join(sorted(_seen)))
            except Exception as _ae:
                _hi._log.debug('alone check: %s', _ae)
                _alone = False        # nevím → raději nepřipomínat
        mine = [r for r in rows
                if _norm(r[1]) in present
                and not (len(r) > 3 and r[3] == 'alone'
                         and (len(present) != 1 or _alone is False))]
        if not mine:
            return  # sliby patří někomu, kdo tu teď není → počká na něj
        ids = [r[0] for r in mine]
        texts = [str(r[2]).strip().rstrip('.') for r in mine if r[2]]
        if not texts:
            mark_announced(dbp, ids)
            return
        name = str(mine[0][1]).capitalize()  # osoba, které sliby patří
        # Telegram: plný výčet
        bullets = "\n".join("• %s" % t for t in texts)
        tg_msg = ("%s, ještě k slibům z našeho rozhovoru — v noci jsem si "
                  "je zpracoval a dám vědět, jakmile je splním:\n%s" % (name, bullets))
        # Hlas: krátce (1–2 sliby), ať to není litanie
        if len(texts) == 1:
            spoken = ("%s, mám pro Vás poznámku — slíbil jsem, že %s. "
                      "Dám vědět, jakmile to budu mít." % (name, texts[0]))
        else:
            spoken = ("%s, mám pro Vás pár slibů z našeho rozhovoru, "
                      "například %s. Podrobnosti jsem Vám poslal na Telegram." %
                      (name, texts[0]))
        delivered = False
        # 1) Telegram (respektuje tiché okno uvnitř send_proactive)
        try:
            tg = (getattr(self.chat, 'telegram', None)
                  if getattr(self, 'chat', None) else None)
            if tg is not None and getattr(tg, 'enabled', False):
                _send = getattr(tg, 'send_proactive', None) or getattr(tg, 'send', None)
                if _send:
                    _send(tg_msg)
                    delivered = True
        except Exception as _te:
            _hi._log.debug('commit announce telegram: %s', _te)
        # 2) Hlas (jen když TTS zapnuté a zrovna nemluví)
        try:
            _tts = getattr(getattr(self, "_hans_dialog", None), "tts", None)
            _isp = getattr(_tts, "is_speaking", None) if _tts else None
            if _tts and getattr(_tts, 'enabled', False) and not (
                    callable(_isp) and _isp()):
                _tts.speak(spoken)
                delivered = True
        except Exception as _ve:
            _hi._log.debug('commit announce voice: %s', _ve)
        # Označ jako oznámené JEN když aspoň jeden kanál prošel; jinak nech
        # announced=0 → zkusí se při dalším usazení (žádný gate to neblokuje).
        if delivered:
            mark_announced(dbp, ids)
            _hi._log.info("commit announce: oznámeno %d slibů (%s)", len(ids), name)

    def _maybe_report_fulfilled(self, known: list):
        """HANS_COMMIT_FULFILL_V1 — uzavření smyčky: když se člověk usadí, sděl
        mu VÝSLEDEK slibů splněných AKCÍ (status='done', result, reported=0):
          research → grounded text; paint → hotový obraz (send_photo).
        Grounded — text je web_reader summary / honest hláška, obraz reálný
        soubor, NIKDY konfabulace. Filtr na osobu, den 9–22, idempotence
        přes DB flag `reported`."""
        import os as _os
        if not known:
            return
        hour = time.localtime().tm_hour
        if hour < 9 or hour >= 22:
            return
        dbp = ((self.config.get('hans_idle', {}) or {})
               .get('diary_db', 'data/hans_diary.db'))
        try:
            from scripts.hans_commitments import (unreported_results,
                                                  mark_reported, _norm)
        except Exception:
            return
        # HANS_COMMIT_QUEUE_PRESENT_FIRST_V1 (29. 9.) — táž past jako ranní
        # opravy (HANS_CORRECTION_PRESENT_FIRST_V1): malý LIMIT PŘED filtrem na
        # přítomné → položky nepřítomných/testovacích osob by frontu ucpaly.
        # Celá fronta, filtr, pak původní velikost dávky.
        rows = unreported_results(dbp, limit=200)  # (id,person,topic,text,result,kind)
        if not rows:
            return
        present = {_norm(n) for n in known}
        mine = [r for r in rows if _norm(r[1]) in present][:10]
        if not mine:
            return
        name = str(mine[0][1]).capitalize()
        tg = (getattr(self.chat, 'telegram', None)
              if getattr(self, 'chat', None) else None)
        tg_on = tg is not None and getattr(tg, 'enabled', False)
        _tts = getattr(getattr(self, "_hans_dialog", None), "tts", None)
        _isp = getattr(_tts, "is_speaking", None) if _tts else None
        tts_ok = (_tts and getattr(_tts, 'enabled', False)
                  and not (callable(_isp) and _isp()))
        # rozděl na OBRAZY (paint, soubor existuje) a TEXTY (research / hlášky)
        photos, texts, done_ids = [], [], []
        for _id, _p, topic, text, result, kind in mine:
            if kind == "paint" and result and _os.path.exists(result):
                photos.append((_id, topic, result))
            else:
                texts.append((_id, topic, result))
        delivered = False
        # 1) OBRAZY — send_photo s popiskem
        for _id, topic, path in photos:
            cap = ("%s, tady je obraz na téma „%s“, který jsem ti slíbil." %
                   (name, topic) if topic else
                   "%s, tady je slíbený obraz." % name)
            try:
                if tg_on and hasattr(tg, 'send_photo') and tg.send_photo(path, caption=cap):
                    delivered = True
                    done_ids.append(_id)
            except Exception as _pe:
                _hi._log.debug('commit report photo: %s', _pe)
        # 2) TEXTY — grounded výsledky do jedné zprávy
        if texts:
            blocks = []
            for _id, topic, result in texts:
                head = ("k tomu, co jsem slíbil zjistit o „%s“" % topic if topic
                        else "ke svému slibu")
                blocks.append("• %s: %s" % (head, result))
            tg_msg = ("%s, splnil jsem slib z našeho rozhovoru:\n%s" %
                      (name, "\n".join(blocks)))
            sent = False
            try:
                if tg_on:
                    _send = getattr(tg, 'send_proactive', None) or getattr(tg, 'send', None)
                    if _send:
                        _send(tg_msg)
                        sent = True
            except Exception as _te:
                _hi._log.debug('commit report telegram: %s', _te)
            # hlas: první textový výsledek zkráceně
            f_topic, f_result = texts[0][1], texts[0][2]
            f_short = f_result if len(f_result) <= 240 else f_result[:237] + "…"
            spoken = (("%s, slíbil jsem zjistit o %s — %s" % (name, f_topic, f_short))
                      if f_topic else "%s, ke svému slibu: %s" % (name, f_short))
            if tts_ok:
                try:
                    _tts.speak(spoken)
                    sent = True
                    tts_ok = False  # nemluv dvakrát v jednom kole
                except Exception as _ve:
                    _hi._log.debug('commit report voice: %s', _ve)
            if sent:
                delivered = True
                done_ids.extend(_id for _id, _t, _r in texts)
        # hlas pro OBRAZY (když nebyl text): krátká zmínka
        if photos and tts_ok:
            pt = photos[0][1]
            try:
                _tts.speak("%s, namaloval jsem ti obraz%s a poslal ti ho na "
                           "Telegram." % (name, (" na téma %s" % pt) if pt else ""))
            except Exception:
                pass
        if done_ids:
            mark_reported(dbp, list(set(done_ids)))
            _hi._log.info("commit report: sděleno %d splněných slibů (%s)",
                      len(set(done_ids)), name)

    def _maybe_deliver_reminders(self, known: list):
        """HANS_COMMIT_FULFILL_V1 — doruč PŘIPOMÍNKY, které nadešly (due_ts
        prošel, nebo bez termínu = při nejbližším setkání). Telegram + hlas,
        filtr na osobu, den 9–22, po doručení status='done'+reported=1."""
        if not known:
            return
        hour = time.localtime().tm_hour
        if hour < 9 or hour >= 22:
            return
        dbp = ((self.config.get('hans_idle', {}) or {})
               .get('diary_db', 'data/hans_diary.db'))
        try:
            from scripts.hans_commitments import (due_reminders,
                                                  mark_reminder_delivered, _norm)
        except Exception:
            return
        rows = due_reminders(dbp, limit=200)  # HANS_COMMIT_QUEUE_PRESENT_FIRST_V1
        if not rows:
            return
        present = {_norm(n) for n in known}
        mine = [r for r in rows if _norm(r[1]) in present][:5]
        if not mine:
            return
        name = str(mine[0][1]).capitalize()
        ids = [r[0] for r in mine]
        items = [(r[2] or r[3]) for r in mine]  # topic, jinak text slibu
        bullets = "\n".join("• %s" % it for it in items)
        tg_msg = "%s, slíbil jsem, že Vám připomenu:\n%s" % (name, bullets)
        spoken = ("%s, připomínám — %s." % (name, items[0]) if len(items) == 1
                  else "%s, mám pro Vás pár připomínek, například %s. Zbytek "
                       "máte na Telegramu." % (name, items[0]))
        delivered = False
        try:
            tg = (getattr(self.chat, 'telegram', None)
                  if getattr(self, 'chat', None) else None)
            if tg is not None and getattr(tg, 'enabled', False):
                _send = getattr(tg, 'send_proactive', None) or getattr(tg, 'send', None)
                if _send:
                    _send(tg_msg)
                    delivered = True
        except Exception as _te:
            _hi._log.debug('reminder telegram: %s', _te)
        try:
            _tts = getattr(getattr(self, "_hans_dialog", None), "tts", None)
            _isp = getattr(_tts, "is_speaking", None) if _tts else None
            if _tts and getattr(_tts, 'enabled', False) and not (
                    callable(_isp) and _isp()):
                _tts.speak(spoken)
                delivered = True
        except Exception as _ve:
            _hi._log.debug('reminder voice: %s', _ve)
        if delivered:
            mark_reminder_delivered(dbp, ids)
            _hi._log.info("reminder: doručeno %d připomínek (%s)", len(ids), name)

    def _timed_reminders_check(self):
        """HANS_COMMIT_FULFILL_V1 (timer) — připomínky s TERMÍNEM pošli
        `lead_minutes` (default 30) PŘED termínem, i když nikdo není doma.
        Telegram přes `send` (OKAMŽITĚ — časovou připomínku nesmí tiché okno
        odložit na 9:00); hlas jen když je adresát přítomen a je den (8–22).
        Po doručení status='done'+reported=1. Fallback pro zmeškané (Hans byl
        dole) je settling cesta `_maybe_deliver_reminders`."""
        dbp = ((self.config.get('hans_idle', {}) or {})
               .get('diary_db', 'data/hans_diary.db'))
        lead_min = int((self.config.get('reminder', {}) or {}).get(
            'lead_minutes', 30))
        try:
            from scripts.hans_commitments import (timed_reminders_due,
                                                  mark_reminder_delivered, _norm)
        except Exception:
            return
        rows = timed_reminders_due(dbp, lead_seconds=lead_min * 60, limit=5)
        if not rows:
            return
        present = {_norm(n) for n in (getattr(self, '_present_names', None) or [])}
        recent = (time.time() - getattr(self, '_last_seen', 0)) < \
            getattr(self, '_absence_gap_s', 90)
        hour = time.localtime().tm_hour
        tg = (getattr(self.chat, 'telegram', None)
              if getattr(self, 'chat', None) else None)
        tg_on = tg is not None and getattr(tg, 'enabled', False)
        _tts = getattr(getattr(self, "_hans_dialog", None), "tts", None)
        _isp = getattr(_tts, "is_speaking", None) if _tts else None
        done = []
        for _id, person, topic, text, due_ts in rows:
            what = topic or text
            when = time.strftime("%H:%M", time.localtime(due_ts)) if due_ts else ""
            name = str(person).capitalize()
            tg_msg = ("%s, připomínka: %s%s." %
                      (name, what, (" — termín %s" % when) if when else ""))
            delivered = False
            # Telegram OKAMŽITĚ (send, ne send_proactive — nesmí se odložit)
            try:
                if tg_on:
                    _s = getattr(tg, 'send', None)
                    if _s and _s(tg_msg):
                        delivered = True
            except Exception as _te:
                _hi._log.debug('timed reminder telegram: %s', _te)
            # hlas jen když je adresát přítomen a je den (nepípat v noci nahlas)
            speak_ok = (_norm(person) in present and recent
                        and 8 <= hour < 22
                        and _tts and getattr(_tts, 'enabled', False)
                        and not (callable(_isp) and _isp()))
            if speak_ok:
                try:
                    _tts.speak("%s, připomínka: %s%s." %
                               (name, what, (", termín %s" % when) if when else ""))
                    delivered = True
                except Exception as _ve:
                    _hi._log.debug('timed reminder voice: %s', _ve)
            if delivered:
                done.append(_id)
        if done:
            mark_reminder_delivered(dbp, done)
            _hi._log.info("timed reminder: odesláno %d (%d min předem)",
                      len(done), lead_min)

    def _maybe_proactive(self):  # HANS_PROACTIVE_V1
        """Osoba usazená a přítomná → zeptej se enginu na proaktivní
        příležitost (dozrálá nitka) a vyslov ji. Mantinely: usazení
        (settle_s), TTS nemluví, recency (>90s = odešla); throttle uvnitř
        enginu (cooldown_h, max_per_day)."""
        names = getattr(self, '_present_names', None)
        if not names:
            return
        now = time.time()
        settle_s = float(self.config.get('proactive', {}).get('settle_s', 600))
        if (now - getattr(self, '_present_since', now)) < settle_s:
            return  # ještě není usazená dost dlouho
        if (now - self._last_seen) > 90:
            return  # poslední detekce >90s → nejspíš odešla
        _tts = getattr(getattr(self, '_hans_dialog', None), 'tts', None)
        if not (_tts and getattr(_tts, 'enabled', False)):
            return
        _isp = getattr(_tts, 'is_speaking', None)
        if callable(_isp) and _isp():
            return  # nepřekrývej probíhající řeč
        # HANS_ROUTINE_PATTERNS_WIRING_V1 — timing: v netypický čas
        # přítomnosti proaktivitu potlač (None/True = propustit).
        rs = self._routine_store()
        if rs is not None:
            try:
                if rs.is_typical_time(names[0]) is False:
                    return
            except Exception as _re:
                _hi._log.warning("routine timing gate error: %s", _re)
        eng = self._proactive_engine()
        if eng is None:
            return
        opp = eng.next_opportunity(names)
        if not opp:
            return
        person, utterance, thread_id = opp
        self._log_entry('proactive', title=person, note=utterance)
        self._last_proactive = (utterance, time.time())  # ATTENTION_PUBLISH_V1
        _tts.speak(utterance)
        _hi._log.info('[Proactive] %s ← nitka #%s: %s', person, thread_id, utterance)
        # PROACTIVE_THREAD_POPUP_V1 — otevři popup, ať má osoba kam odpovědět
        _chat = getattr(self, 'chat', None)
        if _chat is not None and hasattr(_chat, 'open_thread_popup'):
            try:
                _chat.open_thread_popup(person, utterance)
            except Exception as _pe:
                _hi._log.warning('proactive popup failed: %s', _pe)
        self._publish_attention()

    def _proactive_engine(self):  # HANS_PROACTIVE_V1 — lazy singleton
        eng = getattr(self, '_proactive', None)
        if eng is not None:
            return eng
        try:
            from scripts.hans_proactive import ProactiveEngine
            _dbp = self.config.get('hans_idle', {}).get('diary_db', 'data/hans_diary.db')
            self._proactive = ProactiveEngine(self.config, _dbp)
        except Exception as e:
            _hi._log.warning('ProactiveEngine init failed: %s', e)
            self._proactive = None
        return self._proactive

# ROZDELENI_METOD_V1 — až na konci, viz hlavička
from scripts import hans_idle as _hi  # noqa: E402
