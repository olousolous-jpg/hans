"""Metody třídy `HansIdle` přesunuté z `scripts/hans_idle.py` (ROZDELENI_METOD_V1).

Text metod je beze změny; jména původního modulu se čtou přes `_hi.` až při
volání. `HansIdle` má třídu `KontrolyMixin` mezi předky, takže volání přes `self` platí dál.
Import původního modulu je na KONCI souboru (kruhový import oběma směry).
"""
from __future__ import annotations

import time


class KontrolyMixin:
    """Část třídy `HansIdle` — viz hlavička modulu."""

    def _capability_check(self):
        """HANS_CAPABILITY_AWARENESS_V1 — všiml jsem si nové schopnosti? (manifest
        diff proti tomu, co jsem „znal"). Volá se při startu + ranním probuzení."""
        try:
            from scripts.hans_capabilities import detect_new_capabilities
            _capdb = self.config.get("diary_db", "data/hans_diary.db")
            _newcaps = detect_new_capabilities(_capdb)
            if _newcaps:
                _hi._log.info('capabilities: Hans zaznamenal %d nových schopností',
                          len(_newcaps))
                if hasattr(self, '_mood'):
                    try:
                        self._mood._shift('engaged', 0.4, 'nová schopnost')
                    except Exception:
                        pass
        except Exception as _cce:
            _hi._log.debug('capability check failed: %s', _cce)

    def _morning_health_check(self):
        """HANS_MORNING_HEALTH_V1 — po probuzení (a po deferred catchupu)
        projdi noční logy. Reálné chyby → nálada 'worried' + nález uložený
        pro greeting/chat surfacing + deníkový záznam. 1×/den."""
        from datetime import datetime as _dt
        today = _dt.now().strftime('%Y-%m-%d')
        if self._last_health_check_date == today:
            return
        self._last_health_check_date = today
        if not self.config.get('morning_health', {}).get('enabled', True):
            return
        try:
            from scripts import hans_health_check as _hc
        except Exception as _e:
            _hi._log.warning('morning_health: import selhal: %s', _e)
            return
        import time as _t
        since = (getattr(self._routine, '_sleep_started_ts', None)
                 or (_t.time() - 12 * 3600))
        try:
            res = _hc.scan_overnight_errors(since)
        except Exception as _e:
            _hi._log.warning('morning_health: scan selhal: %s', _e)
            return
        n = res.get('count', 0)
        _hi._log.info('morning_health: %d reálných chyb, %d benigních (okno od %s)',
                  n, res.get('benign', 0),
                  _dt.fromtimestamp(since).strftime('%H:%M'))
        if n <= 0:
            self._morning_health = None
            return
        summary = _hc.summary_sentence(res)
        # Nálada → worried (groundovaná ve vlastním provozním zdraví)
        try:
            self._mood._shift('worried', _hc.intensity_for(n),
                              'ranní kontrola: chyby v nočních logech')
        except Exception as _e:
            _hi._log.debug('morning_health: mood shift failed: %s', _e)
        # Nález pro surfacing v pozdravu/chatu (přežije do příchodu osoby)
        self._morning_health = {
            'date': today, 'count': n,
            'modules': res.get('modules', {}),
            'summary': summary,
        }
        # Deník = úložiště + grounding nálady (ne hlasitý report)
        try:
            self._log_entry('morning_health', summary,
                            data=str(res.get('modules', {})),
                            note='; '.join(res.get('samples', []))[:500])
        except Exception as _e:
            _hi._log.debug('morning_health: diary write failed: %s', _e)

    def _downtime_check(self):
        """HANS_DOWNTIME_V1 — při startu zjisti, jestli jsem byl dlouho mimo
        provoz (mezera od poslední aktivity > práh). Pokud ano: deník
        `downtime_noticed` + jemná zvědavá nálada + nález pro surfacing (Hans
        se u příchozí osoby zmíní a zeptá, co se dělo). 1× za běh procesu."""
        cfg = self.config.get('downtime', {})
        if not cfg.get('enabled', True):
            return
        try:
            from scripts import hans_downtime as _dt
        except Exception as _e:
            _hi._log.warning('downtime: import selhal: %s', _e)
            return
        min_h = float(cfg.get('min_gap_hours', 14))
        with self._lock:
            row = self._db.execute("SELECT MAX(ts) FROM diary").fetchone()
        last = row[0] if row else None
        info = _dt.analyze(last, time.time(), min_h)
        if not info:
            return
        sent = _dt.downtime_sentence(info)
        _hi._log.info('downtime: %s (mezera %.1f h)', sent, info['gap_hours'])
        # Jemná zvědavost — groundovaná ve faktu, že něco zmeškal
        try:
            self._mood._shift('curious', 0.4, 'všiml jsem si výpadku provozu')
        except Exception as _e:
            _hi._log.debug('downtime: mood shift failed: %s', _e)
        # Nález pro greeting/chat surfacing (přežije do příchodu osoby)
        self._downtime = {
            'sentence': sent,
            'gap_hours': info['gap_hours'],
            'surfaced': False,   # nastaví handler, až to Hans skutečně zmíní
            'answered': False,   # nastaví handler, až osoba odpoví
        }
        # Deník = úložiště + grounding (importance ho oskóruje → narativ/identita)
        try:
            self._log_entry('downtime_noticed', sent,
                            data=str(info['gap_hours']))
        except Exception as _e:
            _hi._log.debug('downtime: diary write failed: %s', _e)
        # Proaktivní Telegram push — JEN PŘES DEN. send_proactive respektuje
        # tiché okno 22–9 (noční zprávu odloží na 9:00 = doručí přes den).
        # Při výpadku je uživatel typicky pryč → telefon je nejlepší kanál.
        if cfg.get('telegram_push', True):
            try:
                tg = (getattr(self.chat, 'telegram', None)
                      if getattr(self, 'chat', None) else None)
                if tg is not None and getattr(tg, 'enabled', False):
                    _send = getattr(tg, 'send_proactive', None) or getattr(tg, 'send', None)
                    if _send:
                        _send(sent + " Nevíte, co se mezitím doma dělo?")
                        self._downtime['surfaced'] = True  # odpověď půjde do downtime_account
            except Exception as _e:
                _hi._log.debug('downtime: telegram push selhal: %s', _e)

    def _morning_lessons_check(self):
        """HANS_CORRECTION_LEARNING_V1 (#4) — ráno: pokud v noci vznikly lekce
        z korekcí, pocítí jemnou pokoru (mírná nálada). 1x/den. Vědomí lekcí pro
        chat řeší lessons_ctx (čte deník přímo). KONZERVATIVNÍ: jen nálada."""
        from datetime import datetime as _dt
        today = _dt.now().strftime('%Y-%m-%d')
        if getattr(self, '_last_lessons_check_date', None) == today:
            return
        self._last_lessons_check_date = today
        if not self.config.get('corrections', {}).get('enabled', True):
            return
        import time as _t
        _dbp = (self.config.get('hans_idle', {}) or {}).get('diary_db',
                                                            'data/hans_diary.db')
        since = (getattr(self._routine, '_sleep_started_ts', None)
                 or (_t.time() - 12 * 3600))
        try:
            from scripts.hans_lessons import scan_overnight_lessons as _sol
            n = _sol(_dbp, since)
        except Exception as _e:
            _hi._log.debug('morning_lessons: scan selhal: %s', _e)
            return
        if n <= 0:
            return
        _hi._log.info('morning_lessons: %d lekci z nocnich korekci', n)
        try:
            self._mood._shift('worried', min(0.4 + 0.1 * n, 0.6),
                              'ranni pokora: vcerejsi korekce')
        except Exception as _e:
            _hi._log.debug('morning_lessons: mood shift failed: %s', _e)

# ROZDELENI_METOD_V1 — až na konci, viz hlavička
from scripts import hans_idle as _hi  # noqa: E402
