"""Metody třídy `HansRoutine` přesunuté z `scripts/hans_routine.py` (ROZDELENI_METOD_V1).

Text metod je beze změny; jména původního modulu se čtou přes `_rt.` až při
volání. `HansRoutine` má třídu `ReflexeMixin` mezi předky, takže volání přes `self` platí dál.
Import původního modulu je na KONCI souboru (kruhový import oběma směry).
"""
from __future__ import annotations

import urllib.parse as _up
from datetime import datetime, timedelta


class ReflexeMixin:
    """Část třídy `HansRoutine` — viz hlavička modulu."""

    # ── G5D_VERIFY_BEFORE_DIARY_V1 ─────────────────────────────────────────
    def _g5d_verify_day(self, date_str):
        """Ověří dnešní human_chat faktická tvrzení proti Wikipedii.
        Opravy zapíše jako nový záznam 'fact_correction'. Defenzivní:
        cokoliv selže → zaloguj, NEzhroutí reflexi.
        Vrací počet zapsaných oprav.
        """
        written = 0
        try:
            import sqlite3 as _sql
            from scripts.chat_commands import _g5c_llm
            from scripts.web_reader import WebReader
        except Exception as e:
            _rt._log.warning("G5D: import selhal: %s", e)
            return 0
        # Lehký shim — _g5c_llm bere 'handler' jen kvůli config/model.
        # Vyrobím minimální objekt s .config (a model_name nemáme → fallback).
        class _Shim:
            pass
        shim = _Shim()
        shim.config = self.config
        try:
            wr = WebReader(self.config)
        except Exception as e:
            _rt._log.warning("G5D: WebReader init selhal: %s", e)
            return 0
        # Načti dnešní human_chat z deníku
        try:
            db = _sql.connect(self._diary_path)
            rows = db.execute(
                "SELECT title, note FROM diary WHERE event_type='human_chat' "
                "AND date(ts,'unixepoch','localtime')=? "
                "ORDER BY ts DESC LIMIT 6",
                (date_str,)).fetchall()
            db.close()
        except Exception as e:
            _rt._log.warning("G5D: čtení deníku selhalo: %s", e)
            return 0
        if not rows:
            _rt._log.info("G5D: žádné human_chat pro %s, nic k ověření", date_str)
            return 0
        _rt._log.info("G5D: ověřuji %d human_chat záznamů pro %s", len(rows), date_str)
        for title, note in rows:
            if not note:
                continue
            # Vytáhni JEN Hansovu část (řádky za 'Hans:')
            hans_lines = []
            for ln in str(note).splitlines():
                s = ln.strip()
                if s.lower().startswith("hans:"):
                    hans_lines.append(s[5:].strip())
            hans_text = (' '.join(hans_lines)).strip()
            if not hans_text or len(hans_text) < 20:
                continue
            # Extrakce tvrzení
            extract_sys = (
                "Jsi extraktor faktických tvrzení. Z textu vypiš ověřitelná "
                "faktická tvrzení o světě. Ke každému urči ENTITU = PŘEDMĚT "
                "tvrzení, který má vlastní heslo na Wikipedii (dílo, událost, "
                "místo, pojem) — NE vedlejší osobu. Např. tvrzení 'R.U.R. "
                "napsal Čapek' → ENTITA je 'R.U.R.' (dílo), NE 'Čapek'. "
                "Tvrzení 'Wells napsal Válku světů' → ENTITA 'Válka světů'. "
                "Ignoruj dojmy, zdvořilosti, názory. Každé na samostatný řádek "
                "ve tvaru 'ENTITA | tvrzení'. Max 2. Pokud žádné, napiš PRÁZDNÉ."
            )
            extract = _g5c_llm(shim, extract_sys, "Text: " + hans_text, num_predict=180)
            if not extract or "PRÁZDNÉ" in extract.upper():
                continue
            for line in [l.strip() for l in extract.splitlines() if l.strip()][:2]:
                entity = line.split("|", 1)[0].strip() if "|" in line else line
                claim = line.split("|", 1)[1].strip() if "|" in line else line
                if not entity:
                    continue
                if _rt._g5d_neoveritelne(entity, self.config):  # HANS_G5D_SKIP_UNVERIFIABLE_V1
                    _rt._log.info("G5D: [%s] přeskakuji (sám o sobě / osoba z domácnosti / datum — Wikipedie to neověří)", entity)
                    continue
                # G5F_VERIFY_FULLTEXT_V1 — najdi správný článek dle PŘEDMĚTU,
                # stáhni PLNÝ text (ne REST summary). Fallback na summary.
                wiki = ""
                try:
                    _title = wr._wikipedia_search(entity)
                    if _title:
                        _url = ('https://cs.wikipedia.org/wiki/'
                                + _up.quote(_title.replace(' ', '_')))
                        _full = wr.fetch_url(_url, topic='verify')
                        if _full and getattr(_full, 'raw_text', ''):
                            wiki = _full.raw_text[:2500]
                            _rt._log.info('G5D: [%s] plný článek %r (%d zn.)',
                                      entity, _title, len(_full.raw_text))
                    # Fallback: REST summary, když plný článek nevyšel
                    if not wiki:
                        _rr = wr.wikipedia(entity)
                        if _rr and getattr(_rr, 'raw_text', ''):
                            wiki = _rr.raw_text[:1200]
                            _rt._log.info('G5D: [%s] fallback summary', entity)
                except Exception as e:
                    _rt._log.warning("G5D: zdroj pro %r selhal: %s", entity, e)
                if not wiki:
                    _rt._log.info('G5D: [%s] Wikipedie nenašla, přeskakuji', entity)
                    continue
                cmp_sys = (
                    "Jsi ověřovatel faktů. Porovnej TVRZENÍ s textem z Wikipedie. "
                    "Odpověz PŘESNĚ jedním slovem na začátku: SHODA nebo ROZPOR "
                    "nebo NEOVĚŘITELNÉ, pak za pomlčkou stručně proč a správný "
                    "údaj (max 1 věta). Buď přísný na fakta."
                )
                cmp_user = "TVRZENI: " + claim + _rt.NL_RUNTIME + "WIKIPEDIE: " + wiki
                verdict = _g5c_llm(shim, cmp_sys, cmp_user, num_predict=120)
                _rt._log.info("G5D: [%s] %s", entity, verdict[:160])
                # G5H_VERDICT_PARSE_V1 — robustní klasifikace verdiktu podle
                # PRVNÍHO SLOVA (bez diakritiky, upper), ne startswith() syrového
                # textu. Bezpečné oběma směry: ROZPOR nepropadne, SHODA/NEOVĚŘ.
                # se omylem nevyhodnotí jako rozpor.
                _v_raw = (verdict or "").strip()
                _v_first = _v_raw.split()[0] if _v_raw.split() else ""
                _v_norm = _v_first.upper()
                for _a, _b in (('Á','A'),('Č','C'),('Ď','D'),('É','E'),('Ě','E'),('Í','I'),('Ň','N'),('Ó','O'),('Ř','R'),('Š','S'),('Ť','T'),('Ú','U'),('Ů','U'),('Ý','Y'),('Ž','Z')):
                    _v_norm = _v_norm.replace(_a, _b)
                _v_norm = _v_norm.rstrip(":.,-–—")
                _is_rozpor = (_v_norm == "ROZPOR")
                # Zápis opravy JEN při ROZPORU
                # G5E_VERIFY_LOG_ONLY_V1 — NEzapisuje (zápis byl předčasný,
                # verify konfabulovalo: entita≠téma, R.U.R. na stránce Čapka).
                # Jen diagnostika — co BY zapsalo. Sbíráme data o kvalitě.
                if _is_rozpor:
                    # G5K_VERIFY_WRITE_SPARSE_V1 — opatrný ostrý zápis.
                    # _G5K_DRYRUN=True → jen loguje (default). False → zapisuje.
                    _G5K_DRYRUN = False
                    _note = ("Ověřoval jsem '" + claim[:120] + "' proti "
                             "Wikipedii — nepotvrdilo se (sporné).")
                    if _G5K_DRYRUN:
                        _rt._log.info("G5D: BY zapsal opravu [%s] | tvrzení: %s | verdikt: %s",
                                  entity, claim[:80], verdict.strip()[:160])
                    else:
                        # idempotence: už dnes pro tohle tvrzení zapsáno?
                        _dup = False
                        try:
                            _db = _sql.connect(self._diary_path)
                            _row = _db.execute(
                                "SELECT COUNT(*) FROM diary WHERE event_type=? "
                                "AND note=? AND date(ts,'unixepoch','localtime')=?",
                                ("fact_correction", _note, date_str)).fetchone()
                            _dup = bool(_row and _row[0])
                            _db.close()
                        except Exception as _de:
                            _rt._log.warning("G5K: kontrola duplicity selhala: %s", _de)
                        if _dup:
                            _rt._log.info("G5K: [%s] oprava už dnes zapsána, přeskakuji", entity)
                        else:
                            self._diary_write("fact_correction",
                                              "Ověření faktu: " + entity[:60], _note)
                            written += 1
                            _rt._log.info("G5K: [%s] ZAPSÁNA oprava (sporné) | %s",
                                      entity, claim[:80])
                else:
                    _rt._log.info("G5D: [%s] bez zápisu (verdikt nezačíná ROZPOR)", entity)
        _rt._log.info("G5D: hotovo, %d oprav zapsáno pro %s", written, date_str)
        return written

    def run_evening_reflection(self, target_date=None):
        """Ručně spustí Hansovu večerní reflexi dne.

        Args:
            target_date: 'YYYY-MM-DD' nebo None (= dnes)

        Returns:
            Text reflexe nebo None.
        """
        if self._reflection is None:
            _rt._log.warning("Reflexe není inicializovaná "
                         "(synthesis nebyla předána do HansRoutine)")
            return None
        # G5D_VERIFY_BEFORE_DIARY_V1 — nejdřív ověř fakta, zapiš opravy,
        # pak teprve souhrn (run čte z deníku → opravy nabere)
        try:
            _date = target_date or datetime.now().strftime('%Y-%m-%d')
            self._g5d_verify_day(_date)
        except Exception as _ve:
            _rt._log.warning('G5D: verifikace selhala (reflexe pokračuje): %s', _ve)
        result = self._reflection.run(target_date)
        if result:
            # Označit pro dnešek splněno — aby auto-trigger v 22:00
            # neudělal duplikát stejné reflexe.
            today = datetime.now().strftime("%Y-%m-%d")
            self._last_reflection_date = today
            self._save_routine_state()  # ROUTINE_STATE_PERSIST_V1
            # HANS_TENDENCIES_V2 — tendency snapshot přesunut do
            # HansEveningReflection.run() (pokrývá i noční automatiku).
        return result

    # ── HANS_SEVERKA_V1 (3c) — týdenní sebereflexe identity ──────────────────
    def _severka_due(self, today: str) -> bool:
        """True když od poslední sebereflexe uplynul `severka.cadence_days`.

        SEVERKA_CADENCE_V1 (17. 9.) — výchozí 30 dní místo týdne (pokyn
        uživatele: „7 dní je asi moc málo“). Zrcadlí
        HANS_DIRECTION_CADENCE_V1, který totéž udělal pro směr.
        Důvod je měřený, ne dojmový: podklad se mezi týdny prakticky nemění
        (koníčky 96 dní beze změny, 39 ze 41 postojů už není živých), takže
        týdenní běh rozhodoval nad týmiž daty a rozdíl mezi návrhy dělal
        hlavně rozptyl modelu. Za 14 dní vznikly 3 návrhy, z toho 1 přijatý.
        ⚠️ Data-gate v `hans_severka` NENÍ brzda: projde 12 z 12 koníčků,
        takže podmínka „aspoň něco trvalého“ je splněná vždy."""
        last = self._last_severka_check
        if not last:
            return True
        try:
            d0 = datetime.strptime(last, "%Y-%m-%d").date()
            d1 = datetime.strptime(today, "%Y-%m-%d").date()
            _dni = int(((self.config.get("severka") or {}).get("cadence_days", 30)))
            return (d1 - d0).days >= _dni
        except Exception:
            return True

    def _severka_pending_ceka(self) -> bool:
        """SEVERKA_PENDING_GUARD_V1 (17. 9.) — True (= přeskoč běh), když už
        čeká nevyřízený návrh identity.

        `IdentityStore.propose` žádnou pojistku nemá a vkládá bezpodmínečně,
        takže bez tohohle by se pendingy hromadily. A protože `pending()` řadí
        `ts DESC` a `/severka schválit` bez čísla bere NEJNOVĚJŠÍ, dal by se
        odklepnout návrh, který uživatel nikdy nečetl.
        Guard `_last_severka_check` se přeskočením ZÁMĚRNĚ nenastavuje — po
        rozhodnutí uživatele tak Severka naskočí hned příští noc.
        Volá se až ZA `_night_throttled`, aby dotaz do DB nešel každý tik."""
        try:
            if self._identity is None:
                return False
            pend = self._identity.pending()
            if not pend:
                return False
            _rt._log.info("Severka: čeká nevyřízený návrh (pending id=%s) → "
                      "nový nevytvářím, rozhodne uživatel (/severka stav).",
                      ", ".join(str(p.id) for p in pend))
            return True
        except Exception as _e:
            _rt._log.warning("Severka pending guard selhal (%s) → běh nebrzdím", _e)
            return False

    def _night_throttled(self, key: str, min_s: float) -> bool:
        """HANS_NIGHT_RETRY_THROTTLE_V1 — True (přeskoč) když se `key` pokoušel
        naposledy před méně než min_s. Jinak zaznamená pokus a vrátí False.
        Chrání deferral-safe noční úlohy (narrative, toolscout) před retry á
        tick (~60s) celou noc, když LLM defere/thrashuje (doloženo 3.8.:
        toolscout 311× + narrative 149× za noc)."""
        import time as _t
        now = _t.time()
        last = self._night_throttle.get(key, 0)
        if now - last < min_s:
            return True
        self._night_throttle[key] = now
        return False

    def _direction_due(self, today: str) -> bool:
        """True když od poslední úvahy o směru uplynul interval `direction.cadence_days`.

        HANS_DIRECTION_CADENCE_V1 (14. 9.) — výchozí 14 dní místo týdne
        (pokyn uživatele: „to týdenní je moc časté"). Směr je dlouhodobá
        aspirace; týdenní návrhy ho přepisovaly dřív, než se stačil projevit."""
        last = self._last_direction_check
        if not last:
            return True
        try:
            d0 = datetime.strptime(last, "%Y-%m-%d").date()
            d1 = datetime.strptime(today, "%Y-%m-%d").date()
            _dni = int(((self.config.get("direction") or {}).get("cadence_days", 14)))
            return (d1 - d0).days >= _dni
        except Exception:
            return True

    def _run_direction_check(self, today: str) -> bool:
        """HANS_DIRECTION_V1 — týdenní úvaha o vlastním SMĚRU. Reasoning tier
        (qwen3, num_gpu:0 = CPU → žádný VRAM handoff). Při návrhu vznikne
        pending (ask-first) + Hans dá vědět. Vrací True když ODLOŽENO (LLM dole)
        → volající nenastaví guard, zkusí příště (NIGHT_DEFERRAL_SAFE_V1)."""
        try:
            from scripts.hans_direction import HansDirection
            res = HansDirection(self.config, self._diary_path).evaluate()
        except Exception as _e:
            _rt._log.warning("Direction check selhal: %s", _e)
            return True
        if res.get("deferred"):
            _rt._log.info("Direction: odloženo (LLM dole) → zkusím příště.")
            return True
        if res.get("decision") in ("propose", "evolve") and res.get("message"):
            _rt._log.info("Direction: NÁVRH směru (pending id=%s). Viz /smer.",
                      res.get("id"))
            if self._notifier:
                try:
                    self._notifier(res["message"])
                except Exception as _ne:
                    _rt._log.warning("Direction notifier selhal: %s", _ne)
        else:
            _rt._log.info("Direction: %s (drží se / gate).", res.get("decision"))
        return False

    def _run_severka_check(self, today: str) -> bool:
        """Severčino rozhodnutí. Při návrhu vznikne pending verze (nic se
        neaplikuje) — uživatel ji uvidí přes /severka stav a schválí/zamítne.
        NIGHT_DEFERRAL_SAFE_V1 — vrací True když ODLOŽENO (LLM dole) → volající
        NEnastaví týdenní guard a zkusí znovu příští noc."""
        res = self._severka.evaluate()
        if res.get("deferred"):
            _rt._log.info("Severka: odloženo (Ollama dole) → zkusím znovu příští noc.")
            return True
        d = res.get("decision")
        if d == "propose":
            _rt._log.info("Severka: NÁVRH změny identity, čeká na schválení "
                      "(pending id=%s). Viz /severka.", res.get("version_id"))
            # SEVERKA_PROACTIVE_NOTIFY_V1 — Hans dá sám vědět (Telegram), místo
            # aby návrh jen ležel v logu / čekal na /severka stav (pull).
            if self._notifier:
                try:
                    self._notifier(res.get("message") or
                                   "Pane, mám návrh, jak přehodnotit svou povahu "
                                   "— když budete chtít, řekněte /severka stav.")
                except Exception as _ne:
                    _rt._log.warning("Severka notifier selhal: %s", _ne)
        # SEVERKA_LOG_HONEST_V1 (24. 9.) — brzda, zamítnutá role a synonymum
        # se dřív hlásily jako „drift malý“; log má říct skutečný důvod.
        elif res.get("cooldown"):
            _rt._log.info("Severka: identita je mladší než min_days_since_change → nechávám ji uležet.")
        elif res.get("role_rejected"):
            _rt._log.info("Severka: návrh zahozen pojistkou role (%s) → držím roli.",
                      res.get("role_rejected"))
        elif res.get("role_same"):
            _rt._log.info("Severka: návrh je jen jiné pojmenování role (%s) → držím roli.",
                      res.get("role_same"))
        elif res.get("gate"):
            _rt._log.info("Severka: gate prošel, drift malý → držím roli.")
        else:
            _rt._log.info("Severka: žádná trvalá tendence (gate) → držím roli.")
        return False   # NIGHT_DEFERRAL_SAFE_V1 — proběhlo, guard se má nastavit

    # ── AUTOBIOGRAPHICAL_NARRATIVE_V1 (krok 3) — týdenní narativní kapitola ───
    def _narrative_due(self, today: str) -> bool:
        last = self._last_narrative
        if not last:
            return True
        try:
            d0 = datetime.strptime(last, "%Y-%m-%d").date()
            d1 = datetime.strptime(today, "%Y-%m-%d").date()
            return (d1 - d0).days >= 7
        except Exception:
            return True

    def _reflection_written(self, date_str: str) -> bool:
        """Existuje už reflexe za daný den? Ptáme se na TITULEK, ne na `ts` —
        `_write_to_diary` zapisuje čas ZÁPISU, takže dohnaná reflexe za včerejšek
        má dnešní `ts` a filtr přes `date(ts)` by ji nenašel."""
        import sqlite3 as _sql
        try:
            db = _sql.connect(self._diary_path, timeout=5.0)
            row = db.execute(
                "SELECT 1 FROM diary WHERE event_type='evening_reflection' "
                "AND title LIKE ? LIMIT 1", ("%" + date_str + "%",)).fetchone()
            db.close()
            return row is not None
        except Exception as _e:
            _rt._log.debug("reflection_written: %s", _e)
            return False

    def _reflection_catchup(self):
        """HANS_REFLECTION_BRAIN_UP_CATCHUP_V1 — dojeď VČEREJŠÍ reflexi.

        Večerní okno (22:00–23:59) je krátké a kolem 23:00 mizí mozek s PC.
        Reflexe proto nesmí viset jen na něm: ráno, jakmile je mozek zpět,
        se dopíše za včerejšek. Klíč `_last_reflection_date` se nastaví na
        VČEREJŠEK, takže dnešní noční reflexe (`!= today`) zůstává nedotčená.
        Jen jeden den zpět — reflexe stará dva dny už nemá komu co říct.
        """
        from datetime import timedelta as _td
        # HANS_REFLECTION_CATCHUP_LOCK_V1 (15. 9.) — catchup volají DVĚ cesty:
        # brain_up callback (hans_idle, vlastní vlákno) a tick(). Po návratu
        # mozku se potkají během vteřin, obě projdou `_reflection_written`
        # (reflexe běží desítky minut a do deníku se zapíše až na konci)
        # a za týž den vzniknou DVĚ reflexe — doloženo 10. 9. a 15. 9.
        # Druhý volající nečeká, jen odejde: reflexi už dělá první.
        if not self._catchup_lock.acquire(blocking=False):
            return
        try:
            self._reflection_catchup_locked(_td)
        finally:
            self._catchup_lock.release()

    def _reflection_catchup_locked(self, _td):
        try:
            if self._reflection is None:
                return
            now = datetime.now()
            if now.hour >= self._night_hour:
                return                      # večerní okno běží, patří nočnímu ticku
            target = (now - _td(days=1)).strftime("%Y-%m-%d")
            if self._last_reflection_date == target:
                return
            if self._reflection_written(target):
                self._last_reflection_date = target
                self._save_routine_state()
                return
            if not (self._brain_up() and self._chat_quiet_ok()):
                return
            result = self._reflection.run(target_date=target)
            if not result:
                _rt._log.debug("Večerní reflexe (catchup %s): odložena", target)
                return
            self._last_reflection_date = target
            self._save_routine_state()
            _rt._log.info("Večerní reflexe (catchup za %s): zapsána (%d znaků)",
                      target, len(result))
            try:
                from scripts.hans_schedule import mark as _sched_mark
                _sched_mark("evening_reflection", True)
            except Exception:
                pass
        except Exception as _e:
            _rt._log.warning("Večerní reflexe (catchup) selhala: %s", _e)

    def reflection_catchup_async(self):
        """Neblokující obal (volá se z brain_up callbacku)."""
        import threading
        threading.Thread(target=self._reflection_catchup, daemon=True).start()

# ROZDELENI_METOD_V1 — až na konci, viz hlavička
from scripts import hans_routine as _rt  # noqa: E402
