"""Metody třídy `HansIdle` přesunuté z `scripts/hans_idle.py` (ROZDELENI_METOD_V1).

Text metod je beze změny; jména původního modulu se čtou přes `_hi.` až při
volání. `HansIdle` má třídu `CinnostiMixin` mezi předky, takže volání přes `self` platí dál.
Import původního modulu je na KONCI souboru (kruhový import oběma směry).
"""
from __future__ import annotations

from datetime import datetime
import random
import re
import time


class CinnostiMixin:
    """Část třídy `HansIdle` — viz hlavička modulu."""

    def _goal_llm_caller(self, system, user):  # GOAL_CLOSE_2D6_V1
        """LLM pro auto-dilo — ekvivalent _g5c_llm bez chat handleru."""
        try:
            from scripts.ollama_client import ollama_chat
            cfg = self.config or {}
            ow = cfg.get("openwebui_chat", {}) or {}
            model = (cfg.get("models", {}).get("utility")
                     or cfg.get("models", {}).get("dialog")
                     or "hans-czech:latest")
            url = ow.get("base_url", "http://127.0.0.1:11434")
            out = ollama_chat(
                model,
                [{"role": "system", "content": system},
                 {"role": "user", "content": user}],
                ollama_url=url,
                options={"num_predict": 900, "temperature": 0.0},
            )
            return (out or "").strip()
        except Exception as e:
            _hi._log.error("goal work LLM selhal: %s", e)
            return ""

    def _do_close_goal(self, g):  # GOAL_CLOSE_2D6_V1 (2d + 6)
        """Vytvori dilo z tematu cile a uzavre cil podle vysledku."""
        from scripts.hans_goals import STATUS_ABANDONED, STATUS_COMPLETED
        try:
            out = self._create_work(g.topic, self._goal_llm_caller)
        except Exception as e:
            out = {'ok': False, 'error': 'work vyjimka: %s' % e}
        _exp = self._goals.get_expected(g.id)  # GOAL_EXPECTED_VS_ACTUAL_V1
        if out.get('ok'):
            self._goals.close_goal(
                g.id, STATUS_COMPLETED,
                work_path=out.get('path'),
                outcome_reason='dokončen-s-dílem')  # 6
            _hi._log.info('Cíl [%s] dotažen dílem: %s', g.id, out.get('path'))
            try:
                _note = 'Cíl dotažen dílem (%d slov).' % out.get('words', 0)
                if _exp:  # GOAL_EXPECTED_VS_ACTUAL_V1
                    _note += '\nOčekával jsem: %s\nVýsledek: dokončeno dílem.' % _exp
                self._log_entry('goal_completed', title=g.topic, note=_note)
            except Exception:
                pass
            # HANS_WORK_COMPLETION_V1 (B) — vlastní dokončené dílo smí formovat
            # postoje. Ollama je v tuto chvíli prokazatelně UP (esej právě vznikla
            # LLM voláním), takže inline reflexe bez deferralu. Gate try/except.
            try:
                _refl = getattr(getattr(self, '_routine', None), '_reflection', None)
                _essay = out.get('text', '')
                if _refl is not None and hasattr(_refl, 'reflect_on_work') and _essay:
                    _refl.reflect_on_work(g.topic, _essay)
            except Exception as _we:
                _hi._log.warning('work completion reflexe selhala (cíl OK): %s', _we)
        else:
            self._goals.close_goal(
                g.id, STATUS_ABANDONED,
                outcome_reason='vypršel-bez-díla: %s' % out.get('error', ''))  # 6
            _hi._log.info('Cíl [%s] opuštěn (dílo selhalo): %s',
                      g.id, out.get('error'))
            try:  # GOAL_EXPECTED_VS_ACTUAL_V1 — i abandoned do deníku, s porovnáním
                _note = 'Cíl opuštěn bez díla.'
                if _exp:
                    _note += '\nOčekával jsem: %s\nVýsledek: nedotaženo.' % _exp
                self._log_entry('goal_abandoned', title=g.topic, note=_note)
            except Exception:
                pass

    def _maybe_close_goal(self):  # GOAL_CLOSE_2D6_V1 (2d)
        """Z _decide_activity: cil na konci targetu -> dilo + uzaver.
        Bezi v threadu (dilo je pomale), guard proti dvojimu spusteni."""
        if getattr(self, '_closing_goal', False):
            return
        try:
            g = self._goals.get_active_goal() if self._goals else None
        except Exception:
            return
        if g is None or not g.should_complete():
            return
        self._closing_goal = True
        _hi._log.info('Cíl [%s] dosáhl targetu → spouštím dotažení dílem', g.id)
        import threading

        def _wrap():
            try:
                self._do_close_goal(g)
            finally:
                self._closing_goal = False
        threading.Thread(target=_wrap, daemon=True).start()

    def _decide_activity(self, dry_run=False):  # OODA_DECIDE_ACTIVITY_V1 + OODA_DRYRUN_V1
        """OODA: Observe kontext → Orient+Decide vážené skóre → vrátí akci.
        Konzervativní: základ = současné šance, kontext jen lehce přiklání.
        Při chybě fallback na původní pětici. Mění JEN výběr, NE akce."""
        import random as _r
        # GOAL_CLOSE_2D6_V1 — dotáhni cíl na konci targetu (dílo + uzávěr)
        # OODA_DRYRUN_V1 — při diagnostice (/ooda) NEkonat, jen rozhodovat
        if not dry_run:
            try:
                self._maybe_close_goal()
            except Exception as _e:
                _hi._log.debug('maybe_close_goal fallback: %s', _e)
        # ── Observe: posbírej kontext, vše s guardy (chybí → neutrální) ──
        has_case = False
        try:
            if hasattr(self, '_cases') and self._cases is not None:
                has_case = self._cases.get_active_case() is not None
        except Exception:
            has_case = False
        mood = ''
        try:
            if hasattr(self, '_mood') and self._mood is not None:
                mood = (self._mood.mood or '')
        except Exception:
            mood = ''
        alone_h = 0.0
        try:
            alone_h = (time.time() - self._last_seen) / 3600.0
        except Exception:
            alone_h = 0.0
        # ── Orient+Decide: základní váhy = současné šance + book(1) ──────
        # READING_PRIORITY_V1 — knihy = hlavní růstový motor osobnosti (completion
        # reflexe → stances → tendence → Severka). Dřív book=1.0 < movie=2.0 (film
        # NEgrounduje osobnost) = nelogické → čtení upřednostněno.
        w = {
            'movie':        1.5,   # bylo 2.0 — míň echo-chamber filleru
            'thought':      1.0,
            'read':         1.5,   # bylo 1.0 — čtenářská zvídavost podporuje knihu
            'case':         1.0,
            'book':         3.0,   # bylo 1.0 — přečte kapitolu = dominantní idle aktivita
            'relationship': 1.0,  # ACTIVITY_RELATIONSHIP_V1
        }
        # OODA_CASE_AGE_WEIGHT_V1 — bonus case roste se stářím případu (+2 čerstvý → +6 přetažený)
        # Princip: "dotáhni, co jsi začal" sílí, jak případ stárne.
        if has_case:
            _case_bonus = 3.0  # fallback (kdyby se nepodařilo načíst stáří)
            try:
                _ac = self._cases.get_active_case()
                if _ac is not None:
                    _age = _ac.age_days()          # min 1 (viz Case.age_days)
                    _tgt = max(1, _ac.target_days)  # ochrana proti dělení 0
                    _ratio = min(1.0, max(0.0, (_age - 1) / _tgt))
                    _case_bonus = 2.0 + _ratio * 4.0  # 2.0 .. 6.0
            except Exception as _e:
                _hi._log.debug('case age bonus fallback: %s', _e)
                _case_bonus = 3.0
            w['case'] += _case_bonus
        # GOAL_FOCUS_2C_V1 — aktivní cíl přiklání k soustředěnému čtení
        try:
            if getattr(self, '_goals', None) is not None:
                _ag = self._goals.get_active_goal()
                if _ag is not None:
                    w['read'] += 2.5
        except Exception as _e:
            _hi._log.debug('goal OODA bonus fallback: %s', _e)
        # Pravidlo 2: dlouho nikdo doma → víc solitérních, míň filmu
        if alone_h >= 4.0:
            w['thought'] += 1.0
            w['read']    += 1.0
            w['book']    += 1.0
            w['movie']    = max(0.5, w['movie'] - 1.0)
        # Pravidlo 3: nálada jen lehce (zatím opatrně, doladit z logů)
        if mood in ('smutny', 'unaveny', 'klidny'):
            w['thought'] += 0.5
            w['book']    += 0.5
        # ── mapování názvů na reálné metody (book přidán po ověření _library) ──
        funcs = {
            'movie':        self._activity_movie,
            'thought':      self._activity_thought,
            'read':         self._activity_read,
            'case':         self._activity_case,
            'book':         self._activity_book,
            'relationship': self._activity_relationship,  # ACTIVITY_RELATIONSHIP_V1
        }
        # ACTIVITY_RELATIONSHIP_V1 — dynamicky stáhnout 'relationship' z výběru,
        # když nikdo není >= 1 den pryč (jinak by aktivita padla do tichého return)
        try:
            if getattr(self, '_relationships', None) is not None:
                _has_old = False
                _now_ts = time.time()
                for _c in self._relationships.all_cards():
                    _ts = getattr(_c, 'last_seen_ts', None)
                    if _ts and (_now_ts - _ts) / 86400.0 >= 1.0:
                        _has_old = True
                        break
                if not _has_old:
                    w['relationship'] = 0.0
            else:
                w['relationship'] = 0.0
        except Exception:
            w['relationship'] = 0.0
        try:
            keys    = list(w.keys())
            weights = [w[k] for k in keys]
            chosen  = _r.choices(keys, weights=weights, k=1)[0]
            score_s = ' '.join('%s:%.0f' % (k, w[k]) for k in keys)
            _hi._log.info('OODA skóre: %s → vybráno %s', score_s, chosen)
            # OODA_SCORE_ATTR_V1 — sklad pro /ooda diagnostiku (čte z paměti)
            self._last_ooda_score = '%s → vybráno %s' % (score_s, chosen)
            # AVATAR_ACTIVITY_V1 — breadcrumb pro avatar (jako _mood.mood)
            self._current_activity = chosen
            self._current_activity_ts = time.time()
            return funcs[chosen]
        except Exception as _e:
            # Fallback: původní chování (pětice), OODA nikdy nezablokuje akci
            _hi._log.error('OODA decide selhalo (%s) → fallback random.choice', _e)
            return _r.choice([
                self._activity_movie,
                self._activity_movie,
                self._activity_thought,
                self._activity_read,
                self._activity_case,
            ])

    def current_activity_label(self):
        """AVATAR_ACTIVITY_V1 — co Hans zrovna „dělá", pro výběr avatara.
        CHEAP (žádná síť) — čte jen breadcrumb z _decide_activity. 'reading'
        (book/read), 'watching' (movie aktivita), jinak 'looking' (baseline mezi
        aktivitami). Čerstvost 10 min. Živý film řeší display přes kodi_monitor
        (perzistentní). Display fallbackuje na náladu, když avatar pro label chybí."""
        act = getattr(self, '_current_activity', None)
        ts  = getattr(self, '_current_activity_ts', 0)
        if act and (time.time() - ts) < 600:
            if act in ('book', 'read'):
                return 'reading'
            if act == 'movie':
                return 'watching'
        return 'looking'

    def _idle_activity(self):
        """Každých check_interval sekund Hans něco udělá."""
        # Každých 30 minut nová aktivita
        if not hasattr(self, '_last_activity'):
            self._last_activity = 0
        if time.time() - self._last_activity < 1800:
            return
        self._last_activity = time.time()

        # OODA_DECIDE_ACTIVITY_V1 — OODA: výběr přes vážené skóre místo random.choice
        activity = self._decide_activity()
        activity()

    def _activity_relationship(self):  # ACTIVITY_RELATIONSHIP_V1
        """Hans se zamyslí nad nejdéle neviděnou osobou (≥ 1 den).
        Šablona — pár variant. Když nikdo není 1+ den pryč, tichý return.
        LLM verze přijde později po ověření naživo."""
        if not getattr(self, '_relationships', None):
            return
        import random as _r
        try:
            cards = self._relationships.all_cards()
        except Exception as _e:
            _hi._log.warning('relationship aktivita: all_cards selhalo: %s', _e)
            return
        # Vyfiltrovat karty s platným last_seen_ts a spočítat dny
        now = time.time()
        candidates = []
        for c in cards:
            ts = getattr(c, 'last_seen_ts', None)
            if not ts:
                continue
            days = (now - ts) / 86400.0
            if days >= 1.0:
                candidates.append((days, c))
        if not candidates:
            _hi._log.debug('relationship aktivita: nikdo neviděn >= 1 den, přeskakuji')
            return
        # Nejstarší (nejdéle neviděný)
        candidates.sort(key=lambda x: x[0], reverse=True)
        days, card = candidates[0]
        name = getattr(card, 'display_name', '') or 'osobu'
        chrz = (getattr(card, 'characterization', '') or '').strip()
        # Slovní vyjádření doby
        d_int = int(days)
        if d_int <= 1:
            doba = 'celý den'
        elif d_int < 5:
            doba = '%d dny' % d_int
        else:
            doba = '%d dnů' % d_int
        # HANS_NAME_INFLECTION_V1: sloveso podle rodu osoby
        nebyl = _hi._cz_was(name, self.config, negate=True)   # nebyl / nebyla
        byl   = _hi._cz_was(name, self.config)                # byl / byla
        disp  = _hi._cz_display(name)
        akuz  = _hi._cz_acc(name, self.config)                # HANS_NAME_INFLECTION_V2: akuzativ „Janu"
        # Šablona — 5 variant Hansova hlasu (komorník). Hans („neviděl") je m.
        templates = [
            f'{akuz} jsem neviděl už {doba}. Doufám, že je v pořádku.',
            f'{disp} tu {nebyl} {doba} — přemýšlím, kde se asi nachází.',
            f'Vzpomněl jsem si na to, že {disp} tu {nebyl} {doba}.',
            f'{disp} mi schází — naposled tu {byl} před {doba}.',
            f'Uvažoval jsem o tom, že {disp} tu {nebyl} {doba}.',
        ]
        note = _r.choice(templates)
        if chrz:
            # Lehce přidat kontext z characterization (jen krátký zlomek)
            note += ' (' + chrz[:120].strip() + ')'
        self._log_entry('person_recollection', title=name, note=note)
        _hi._log.info('relationship aktivita: zápis o %s (%.1f dnů)', name, days)

    def _create_work(self, topic, llm_caller):  # WORK_REFACTOR_SHARED_V1
        """Vytvoří dílo (esej) z RAGu hans_cetba. Sdílené jádro /work + automatika.
        topic: téma. llm_caller: fn(system, user) -> str (destilace).
        Vrací dict {ok, error, words, path, rag}. Nikdy nevyhodí.
        Volá se z chatu (handler) i z idle (shim) — jedna destilace."""
        import os as _os
        import re as _re
        import time as _t
        from datetime import datetime as _dt
        out = {'ok': False, 'error': '', 'words': 0, 'path': '', 'rag': ''}
        topic = (topic or '').strip()
        if not topic:
            out['error'] = 'prázdné téma'; return out
        _kn = getattr(self, '_knowledge', None)
        if _kn is None or not getattr(_kn, 'enabled', False):
            out['error'] = 'HansKnowledge není aktivní'; return out
        # 1) Sběr z RAGu
        try:
            res = _kn.query('hans_cetba', topic, k=15)
        except Exception as _e:
            out['error'] = 'query selhalo: %s' % _e; return out
        if not getattr(res, 'found', False):
            out['error'] = 'k tématu nic v četbě nenalezeno'; return out
        chunks = getattr(res, 'chunks', []) or []
        material = (getattr(res, 'text', '') or '')[:5000]
        if not material.strip():
            material = chr(10).join(
                '- ' + str(c.get('text', ''))[:400]
                for c in chunks[:15] if isinstance(c, dict))[:5000]
        if not material.strip():
            out['error'] = 'prázdný materiál'; return out
        # GOAL_CONTINUITY_V1 — naváž na dřívější dílo na stejné téma (hans_dila)
        prior = ''
        try:
            pres = _kn.query('hans_dila', topic, k=2)
            if getattr(pres, 'found', False):
                prior = (getattr(pres, 'text', '') or '')[:2000].strip()
        except Exception as _pe:
            _hi._log.debug('goal continuity prior fetch: %s', _pe)
            prior = ''
        # 2) Destilace přes předaný llm_caller
        # PERSONA_REFACTOR_10 — identita z persona_core, task beze změny
        from scripts.hans_persona import persona_core
        sys_prompt = (
            persona_core(self.config, with_address=False) + ' '
            'Z níže uvedených útržků, které sis nastudoval, '
            'napiš souvislou esej na téma \'%s\'. NE výčet, NE odrážky — '
            'plynulý text vlastními slovy, 400 až 600 slov. Pokud útržky '
            'něco neobsahují, NEVYMÝŠLEJ si fakta. Piš svým hlasem.'
        ) % topic
        if prior:  # GOAL_CONTINUITY_V1
            sys_prompt += (' Na toto téma už máš dřívější úvahu (uvedena '
                           'níže). NAVAŽ na ni a prohlub ji, případně ji '
                           'reviduj — ale NEopakuj ji, posuň myšlenku dál.')
        user_prompt = 'Nastudované útržky:' + chr(10) + material
        if prior:  # GOAL_CONTINUITY_V1
            user_prompt += (chr(10) + chr(10)
                            + 'Tvá dřívější úvaha na toto téma:'
                            + chr(10) + prior)
        try:
            essay = llm_caller(sys_prompt, user_prompt)
        except Exception as _e:
            out['error'] = 'destilace selhala: %s' % _e; return out
        if not essay or not essay.strip():
            out['error'] = 'prázdná esej'; return out
        essay = essay.strip()
        out['words'] = len(essay.split())
        out['text'] = essay  # HANS_WORK_COMPLETION_V1 — esej pro reflexi díla
        # HANS_WORK_FIXPOINT_GUARD_V1 (28.8.) — NEZAPISOVAT TOTÉŽ ZNOVU.
        # Doloženo: ze 13 esejí o Designu je 5 různých těl a jedno se opakuje
        # OSMKRÁT (14.7.–24.8.); mezi 24.7. a 24.8. se liší jediný řádek — datum.
        # Příčina je fixní bod: materiál je RAG dotaz s pevným k=15 (proto pořád
        # „z 15 útržků"), jako „dřívější úvaha" se přiloží Hansova VLASTNÍ minulá
        # esej, a při shodném vstupu vyjde shodný výstup. Pokyn v promptu
        # („NEopakuj ji, posuň myšlenku dál") to přebít nemůže.
        # Škoda nebyla v té eseji, ale v EVIDENCI: deník hlásil 14 cílů
        # „dokončen-s-dílem" při jednom skutečném přírůstku, a `reflect_on_work`
        # tímtéž textem osmkrát krmil postoje.
        # Tohle je jen POJISTKA proti nafukování deníku — příčinu (nehybný
        # materiál) neřeší; ta patří k tomu, aby se RAG dotaz mezi koly posouval.
        _slug = _re.sub(r'[^a-z0-9]+', '_', topic.lower()).strip('_')[:40] or 'dilo'
        try:
            import difflib as _dl
            import glob as _gl

            def _telo(txt):
                # hlavička = '# Téma' + prázdný + '*Hansova esej · …*' + prázdný
                for i, r in enumerate(txt.split(chr(10))):
                    if r.startswith('*Hansova esej'):
                        return ' '.join(chr(10).join(
                            txt.split(chr(10))[i + 1:]).split())
                return ' '.join(txt.split())

            _drive = sorted(_gl.glob(_os.path.join(
                'data/hans_works', '*_%s.md' % _slug)))
            if _drive:
                _stary = open(_drive[-1], encoding='utf-8').read()
                _shoda = _dl.SequenceMatcher(
                    None, _telo(_stary), ' '.join(essay.split())).ratio()
                _prah = float((self.config.get('goals', {}) or {}).get(
                    'fixpoint_ratio', 0.95))
                if _shoda >= _prah:
                    out['error'] = (
                        'esej se neposunula (%.0f %% shoda s %s) — dílo '
                        'nezapisuji' % (_shoda * 100,
                                        _os.path.basename(_drive[-1])))
                    _hi._log.warning('work fixpoint: %s', out['error'])
                    return out
                _hi._log.info('work fixpoint: shoda s předchozím %.0f %% (práh %.0f %%)',
                          _shoda * 100, _prah * 100)
        except Exception as _fe:
            _hi._log.debug('kontrola fixního bodu selhala, jedu dál: %s', _fe)

        # 3) Ulož soubor
        works_dir = 'data/hans_works'
        try:
            _os.makedirs(works_dir, exist_ok=True)
            slug = _re.sub(r'[^a-z0-9]+', '_', topic.lower()).strip('_')[:40] or 'dilo'
            datum = _dt.now().strftime('%Y-%m-%d')
            fname = '%s_%s.md' % (datum, slug)
            fpath = _os.path.join(works_dir, fname)
            if _os.path.exists(fpath):
                fname = '%s_%s_%s.md' % (datum, slug, _dt.now().strftime('%H%M'))
                fpath = _os.path.join(works_dir, fname)
            header = ('# %s' + chr(10) + chr(10) +
                      '*Hansova esej · %s · z %d \u00fatr\u017ek\u016f \u010detby*'
                      + chr(10) + chr(10)) % (topic, datum, len(chunks))
            with open(fpath, 'w', encoding='utf-8') as _f:
                _f.write(header + essay + chr(10))
            out['path'] = fpath
        except Exception as _e:
            out['error'] = 'uložení selhalo: %s' % _e; return out
        # 4) Stopa do deníku
        try:
            if hasattr(self, '_log_entry'):
                self._log_entry('work_created', title=topic,
                                note='Napsal jsem esej o \'%s\' (%d slov, %s)'
                                % (topic, out['words'], fname))
        except Exception as _e:
            _hi._log.warning('work_created zápis selhal: %s', _e)
        # 5) Upload do hans_dila
        try:
            up = _kn.upload(
                'hans_dila',
                'dilo_%s_%d' % (slug, int(_t.time())),
                'Esej o %s' % topic,
                'Esej o %s:' % topic + chr(10) + essay,
            )
            out['rag'] = 'ano' if up else 'ne'
        except Exception as _e:
            out['rag'] = 'chyba: %s' % _e
        out['ok'] = True
        return out

    def _activity_book(self):
        """Hans si přečte kapitolu z aktivní knihy."""
        if not hasattr(self, '_library'):
            return
        # BOOK_COMPLETION_DEFERRED_V1 — retry odložené completion reflexe (po
        # dočtení se čte už nová kniha, takže else-větev níže by ji nezachytila).
        self._maybe_reflect_finished_book()
        try:
            ch = self._library.read_next_chapter()
            if ch:
                _hi._log.info('Hans precetl: %s — %s', ch.book_title, ch.title)
                # BOOK_GROUNDING_V1 - book_read zapisuje uz read_next_chapter
                # (dobry titul + grounded uryvek). Zdejsi duplicitni zapis zrusen.
            else:
                self._maybe_reflect_finished_book()  # HANS_BOOK_COMPLETION_V1
        except Exception as e:
            _hi._log.debug('Book read: %s', e)

    def _maybe_reflect_finished_book(self):
        """HANS_BOOK_COMPLETION_V1 + BOOK_COMPLETION_DEFERRED_V1 — dočtená kniha
        BEZ completion reflexe → reflexe (napojená na stances). Perzistentní:
        když je Ollama dole a reflexe selže, kniha zůstává pending a retry
        proběhne příště (žádná ztráta). Mark až PO úspěchu."""
        try:
            pend = self._library.get_pending_completion()
        except Exception:
            return
        if not pend or not pend.get("title"):
            return
        # HANS_BOOK_SUMMARY_V1 — úvahu o dočtené knize napíše noční úloha
        # `hans_book_summary` až ze souhrnu CELÉ knihy (dřív stála na prvních
        # ~7 názorech na kapitoly a děj si domýšlela)
        if ((self.config.get("book_summary", {}) or {}).get("enabled", True)):
            return
        refl = getattr(getattr(self, '_routine', None), '_reflection', None)
        if refl is None or not hasattr(refl, 'reflect_on_book'):
            _hi._log.debug('book completion: reflexe nedostupná (zůstává pending, retry příště)')
            return
        try:
            _hi._log.info('Book completion reflexe: %s', pend['title'])
            res = refl.reflect_on_book(pend['title'], [])
            if res:
                self._library.mark_completion_reflected(pend['book_id'])
                _hi._log.info('Book completion hotová + označená: %s', pend['title'])
            else:
                _hi._log.warning('book completion: prázdná reflexe (Ollama?) — '
                             'zůstává pending, retry příště: %s', pend['title'])
        except Exception as e:
            _hi._log.warning('book completion reflexe selhala (zůstává pending): %s', e)

    def _activity_read(self):
        """Hans si něco přečte na internetu podle svých zájmů."""
        if self._movies_cache:
            self._curiosity.add_kodi_movies(self._movies_cache)
        # CURIOSITY_RANDOM_WIRE_V1 — 60 % interest / 25 % news / 15 % random_wiki
        # GOAL_FOCUS_2C_V1 — aktivní cíl → 50 % čtení o tématu cíle
        _goal_topic = None
        try:
            if getattr(self, '_goals', None) is not None:
                _g = self._goals.get_active_goal()
                if _g is not None:
                    _goal_topic = (_g.topic or '').strip() or None
        except Exception:
            _goal_topic = None
        # HANS_GOAL_CURRICULUM_V1 (2.9.) — cil cte DALSI POLOZKU sveho kurikula,
        # ne porad dokola svuj nazev. Kdyz je kurikulum vycerpane, `_goal_next`
        # vrati None a cil uz 50 % cetby NEBERE — pusti ji do bezne zvidavosti.
        # Doloha, proc: 20x tyz clanek „Design" za tyden, spontanni cetba je
        # pritom pestra (91 ruznych clanku tydne).
        _goal_item, _goal_id = self._goal_next(_goal_topic)
        _roll = random.random()
        if _goal_item and _roll < 0.50:
            self._curiosity.trigger_topic(_goal_item)
            if _goal_id is not None:
                try:
                    self._goals.advance_curriculum(_goal_id)
                except Exception as _ae:
                    _hi._log.debug("advance_curriculum: %s", _ae)
        elif _roll < 0.60:
            self._curiosity.trigger_interest()
        elif _roll < 0.85:
            self._curiosity.trigger_news()
        else:
            self._curiosity.trigger_random_wiki()
        from scripts.hans_persona import persona_name as _pn  # PERSONA_NAME_CONFIGURABLE_V1
        self._log_entry("activity", note=f"{_pn(self.config)} si čte na internetu")

    def _goal_next(self, goal_topic):
        """HANS_GOAL_CURRICULUM_V1 — dalsi polozka kurikula aktivniho cile.

        Vraci (polozka, goal_id). (None, None) = cil neni, kurikulum je
        vycerpane, nebo se ho nepodarilo vyrobit — volajici pak necha cetbu
        bezne zvidavosti.

        ⚠️ Kurikulum se generuje LINE, az tady: `_generate_curriculum` bezi
        103-123 s a `open_goal` je v nocni destilaci, kterou nechci blokovat.
        ⚠️ KOTVA JE POVINNA. Zmereno 2.9.: bez ni generator tema mine —
        „Kde domov muj?" (ceska hymna z Tylovy hry) dalo kurikulum
        o ORNITOLOGII. S kotvou z Hansova vlastniho zapisu uz sedi
        (Tyl, Karel Strakaty, puvod pisne). [[prompt-category-invites-confabulation]]
        """
        if not goal_topic or getattr(self, "_goals", None) is None:
            return None, None
        try:
            g = self._goals.get_active_goal()
            if g is None:
                return None, None
            polozky, idx = self._goals.curriculum(g.id)
            if not polozky:
                polozky = self._vyrob_kurikulum(goal_topic)
                if not polozky:
                    # HANS_GOAL_KOTVA_VELIKOST_V1 — bez kurikula si cíl svůj název
                    # přečte nejvýš jednou za 24 h (první čtení dá kotvu), jinak
                    # běžná zvědavost; dřív četl týž článek při každé příležitosti
                    if self._cil_cten_nedavno(goal_topic):
                        return None, None
                    return goal_topic, None      # pad na dnesni chovani
                self._goals.set_curriculum(g.id, polozky)
                idx = 0
            if idx >= len(polozky):
                return None, None                # vycerpano → bezna zvidavost
            return polozky[idx], g.id
        except Exception as e:
            _hi._log.debug("_goal_next: %s", e)
            return goal_topic, None

    def _cil_cten_nedavno(self, tema: str, hodin: float = 24.0) -> bool:
        """HANS_GOAL_KOTVA_VELIKOST_V1 — četl Hans článek s názvem cíle za posledních N h?"""
        import sqlite3 as _sq
        try:
            _db = self.config.get("diary_db", "data/hans_diary.db")
            _var = list(dict.fromkeys([tema, tema[:1].upper() + tema[1:], tema.lower(),
                                       tema.capitalize()]))
            con = _sq.connect("file:%s?mode=ro" % _db, uri=True, timeout=4.0)
            try:
                r = con.execute(
                    "SELECT 1 FROM diary WHERE event_type='web_read' AND ts > ? AND title IN (%s) "
                    "LIMIT 1" % ",".join("?" * len(_var)),
                    [time.time() - hodin * 3600] + _var).fetchone()
            finally:
                con.close()
            return bool(r)
        except Exception as e:
            _hi._log.debug("_cil_cten_nedavno: %s", e)
            return False

    def _vyrob_kurikulum(self, tema: str) -> list:
        """Kurikulum pro cil, ukotvene tim, co Hans o tematu SAM precetl."""
        import re as _re
        import sqlite3 as _sq
        kotva = []
        try:
            _db = self.config.get("diary_db", "data/hans_diary.db")
            con = _sq.connect("file:%s?mode=ro" % _db, uri=True, timeout=4.0)
            try:
                # HANS_GOAL_KOTVA_VELIKOST_V1 (27. 10.) — cíl „sport“ × článek
                # „Sport“: přesná shoda titulu kotvu nenašla, kurikulum nevzniklo
                # a cíl četl pořád svůj název (11× za 3 dny). Titul bez ohledu na
                # velikost písmen (SQLite lower() neumí české znaky → varianty),
                # odložený záznam {"pending": …} → jeho raw_text.
                _var = list(dict.fromkeys([tema, tema[:1].upper() + tema[1:], tema.lower(),
                                           tema.capitalize()]))
                rows = con.execute(
                    "SELECT COALESCE(NULLIF(data,''),note) FROM diary "
                    "WHERE event_type='web_read' AND title IN (%s) "
                    "ORDER BY ts DESC LIMIT 10" % ",".join("?" * len(_var)), _var).fetchall()
            finally:
                con.close()
            txt = ""
            for (_d,) in rows:
                _d = (_d or "").strip()
                if _d.startswith("{"):
                    try:
                        import json as _js
                        _d = str((_js.loads(_d) or {}).get("raw_text") or "")
                    except Exception:
                        _d = ""
                _d = _re.sub(r"^\[\w+\]\s*", "", _d)
                if _d:
                    txt = _d
                    break
            kotva = [v.strip() for v in _re.split(r"(?<=[.!?])\s+", txt)
                     if len(v.strip()) > 20][:3]
        except Exception as _ke:
            _hi._log.debug("_vyrob_kurikulum kotva: %s", _ke)
        if not kotva:
            _hi._log.info("HANS_GOAL_CURRICULUM_V1: '%s' nema kotvu v deniku "
                      "→ kurikulum negeneruji (bez ni generator tema mine)", tema)
            return []
        try:
            from scripts.hans_study import _generate_curriculum
            return _generate_curriculum(self.config, tema, kotva) or []
        except Exception as e:
            _hi._log.warning("HANS_GOAL_CURRICULUM_V1: generovani selhalo (%s) "
                         "→ cil cte dal sve tema", e)
            return []

    def _activity_thought(self):
        """Hans zapíše variabilní pozorování o domácnosti."""
        import random as _r
        hour = datetime.now().hour
        if 6 <= hour < 12:    period = "dopoledne"
        elif 12 <= hour < 18: period = "odpoledne"
        elif 18 <= hour < 22: period = "večer"
        else:                  period = "v noci"
        thoughts = [
            f"Uspořádal jsem knihovnu a přemýšlel o správném pořadí svazků.",
            f"Zkontroloval jsem stav domácnosti {period} — vše je v pořádku.",
            f"V době nepřítomnosti obyvatel jsem věnoval čas péči o detaily.",
            f"Připravil jsem vše na případný příchod hostů.",
            f"Ticho {period} nabízí prostor k zamyšlení nad chodem domácnosti.",
            f"Prohlédl jsem zásoby a zaznamenal co bude třeba doplnit.",
            f"Věnoval jsem pozornost péči o detaily, na které jindy není čas.",
        ]
        self._log_entry("observation", note=_r.choice(thoughts))

    def _activity_case(self):
        """Hans+Koláč otevřou nový případ nebo posunou stávající o krok."""
        if not hasattr(self, '_cases'):
            return
        try:
            active = self._cases.get_active_case()
            if active is None:
                # Otevřít nový případ z aktuálního kontextu
                ctx = self._build_case_context()
                case = self._cases.get_or_create_case(ctx)
                _hi._log.info("Nový případ otevřen: %s", case.title)
                # CASE_RAG_ENRICH_V1 — clue do note, ať RAG reflexe není z holého titulku
                _case_clue = (case.clues[0].get("text", "")
                              if getattr(case, "clues", None) else "")
                self._log_entry("case_opened", title=case.title, note=_case_clue)
            else:
                # Posunout stávající: přidat stopu nebo teorii
                if active.age_days() >= active.target_days:
                    # Čas uzavřít
                    self._cases.close_case(
                        active.id,
                        resolution="Případ uzavřen po dostatečném prošetření.")
                    _hi._log.info("Případ uzavřen: %s", active.title)
                    # CASE_RESOLUTION_SUMMARY_V1 — NE firehose všech stop (dřív se
                    # slepily desítky náhodných „kodi: …" + prázdných „četl:" hlaviček
                    # + useknutých dialogů → zeď nesmyslů). Vyfiltruj balast, dedupuj,
                    # vezmi pár SMYSLUPLNÝCH stop.
                    _clue_texts = self._clean_case_clues(
                        getattr(active, "clues", None) or [])
                    _subject = _clue_texts[0] if _clue_texts else active.title
                    _rest = "; ".join(_clue_texts[1:6])   # max ~5 stop, ne firehose
                    _close_note = "Subjekt: " + _subject
                    if _rest:
                        _close_note += "\nStopy: " + _rest
                    self._log_entry("case_closed", title=active.title, note=_close_note)
                    # RAG upload zařídí HansSynthesisHooks asynchronně
                else:
                    # Přidat stopu z aktuálního kontextu
                    ctx = self._build_case_context()
                    if ctx:
                        clue = "; ".join(ctx[:2])[:200]
                        self._cases.add_clue(active.id, clue)
                        _hi._log.info("Stopa k případu '%s': %s",
                                  active.title, clue[:60])
        except Exception as e:
            _hi._log.debug("Activity case: %s", e)

    def _clean_case_clues(self, clues: list) -> list[str]:
        """CASE_RESOLUTION_SUMMARY_V1 — z nasbíraných stop nech jen SMYSLUPLNÉ:
        zahoď prázdné/hlavičkové („… nedávno přečetl:"), příliš krátké,
        useknuté (nekončí interpunkcí u dialogových vět) a duplicity."""
        out, seen = [], set()
        for c in clues:
            t = (c.get("text", "") if isinstance(c, dict) else str(c)).strip()
            if not t:
                continue
            tl = t.lower()
            # prázdná čtecí hlavička bez obsahu
            if "nedávno přečetl:" in tl and len(t) < 45:
                continue
            # holá „kodi: X; četl: <hlavička>" → nech jen část s obsahem
            # dedup na začátek textu
            key = re.sub(r"\s+", " ", tl)[:50]
            if key in seen:
                continue
            # dialogová věta useknutá uprostřed (nekončí . ! ? " ) → balast
            words = t.split()
            looks_dialog = any(t.startswith(p) for p in (
                "S tím", "Dovolím si", "Máte", "Ano,", "To je", "Tady", "A "))
            if looks_dialog and t[-1:] not in ".!?\"“”":
                continue
            if len(t) < 12 or len(words) < 3:
                continue
            seen.add(key)
            out.append(t)
        return out

    def _build_case_context(self) -> list[str]:
        """Posbírej kontext pro Koláčův případ — co Hans vidí/četl/sleduje."""
        parts: list[str] = []
        # Filmy z Kodi
        if self._movies_cache:
            import random as _r
            mv = _r.choice(self._movies_cache)
            t = mv.get("title", "")
            if t:
                parts.append(f"kodi: {t}")
        # Co Hans nedávno četl — POZOR: get_context_string vrací víceřádkový text,
        # 1. řádek je HLAVIČKA („… si nedávno přečetl:") → dřív se brala ta a stopa
        # byla prázdná „četl: Hans si nedávno přečetl:". Vezmi OBSAHOVÝ řádek.
        if hasattr(self, "_curiosity"):
            try:
                read = self._curiosity.get_context_string(max_items=1)
                _content = [l.strip(" -•") for l in (read or "").splitlines()[1:]
                            if l.strip()]
                if _content:
                    # „[dd.mm. HH:MM] Titul: shrnutí" → strhni časové razítko
                    _c = re.sub(r"^\[[^\]]*\]\s*", "", _content[0])
                    if _c:
                        parts.append(f"četl: {_c[:80]}")
            except Exception:
                pass
        # Aktuální kniha
        if hasattr(self, "_library"):
            try:
                book = self._library.get_current_book()
                if book and book.get("title"):
                    parts.append(f"kniha: {book['title'][:50]}")
            except Exception:
                pass
        return parts

# ROZDELENI_METOD_V1 — až na konci, viz hlavička
from scripts import hans_idle as _hi  # noqa: E402
