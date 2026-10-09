"""Metody třídy `OpenWebUIDirectHandler` přesunuté z `scripts/openwebui_direct_handler.py` (ROZDELENI_METOD_V1).

Text metod je beze změny; jména původního modulu se čtou přes `_h.` až při
volání. `OpenWebUIDirectHandler` má třídu `SystemMixin` mezi předky, takže volání přes `self` platí dál.
Import původního modulu je na KONCI souboru (kruhový import oběma směry).
"""
from __future__ import annotations

from datetime import datetime, date
import logging
import time


class SystemMixin:
    """Část třídy `OpenWebUIDirectHandler` — viz hlavička modulu."""

    def _build_system(self, name: str, for_greeting: bool = False,
                      user_msg: str = "") -> str:
        import types as _types_nt
        ctx = _types_nt.SimpleNamespace(name=name, for_greeting=for_greeting, user_msg=user_msg)
        self._sy_zaklad(ctx)
        self._sy_relevance(ctx)
        self._sy_schopnosti(ctx)
        self._sy_kolac(ctx)
        self._sy_mistnost(ctx)
        self._sy_misto(ctx)
        self._sy_kalendar(ctx)
        self._sy_cas(ctx)
        self._sy_denik(ctx)
        self._sy_pribeh(ctx)
        self._sy_studium(ctx)
        self._sy_smer(ctx)
        self._sy_napady(ctx)
        self._sy_kodi(ctx)
        self._sy_okoli(ctx)
        self._sy_pamet(ctx)
        self._sy_nitky(ctx)
        self._sy_zajmy(ctx)
        self._sy_otazky(ctx)
        self._sy_osoba(ctx)
        self._sy_cetba(ctx)
        self._sy_myslenky(ctx)
        self._sy_rutina(ctx)
        self._sy_telo(ctx)
        self._sy_nalada(ctx)
        self._sy_zdravi(ctx)
        self._sy_vypadek(ctx)
        self._sy_severka(ctx)
        self._sy_prohloubeni(ctx)
        self._sy_lekce(ctx)
        self._sy_hodnoty(ctx)
        self._sy_skladani(ctx)
        # region agent log
        try:
            _h._dbg(
                location="openwebui_direct_handler.py:_build_system",
                message="Built system prompt",
                data={
                    "has_surroundings": bool(ctx.surr_ctx.strip()),
                    "has_known_persons": bool(ctx.persons_ctx.strip()),
                    "chars": len(ctx.system_msg),
                    "history_turns": self.conv_store.summary(),
                },
            )
        except Exception as _tiche:
            _h.log_once(  # HANS_NO_SILENT_CTX_V1
                logging.getLogger(__name__), "_build_system(ř. 2181)",
                "_build_system: blok kontextu selhal (ř. 2181): %s", _tiche)
        # endregion
        return ctx.system_msg

    def _sy_zaklad(self, ctx):
        # PERSONA_REFACTOR_1_4 — jednotný zdroj identity
        from scripts.hans_persona import persona_core
        ctx.system_base = persona_core(self.config)
        # HANS_PERSONA_NATURE_V1 (1. 10., pokyn uživatele) — na „co jste?“ ať
        # řekne, že je umělá inteligence, a popíše se Severčiným jádrem. Bez
        # téhle věty si model doplňoval generické „jako AI nemám smysly“ —
        # nepravda (kamera, mikrofon). JEN chat: persona_core čte 30 míst
        # (reflexe, Koláč), vývoj osobnosti se tím měnit nemá.
        _nat = ((self.config.get("persona", {}) or {}).get("nature_chat") or "").strip()
        if _nat:
            from scripts.hans_persona import apply_name as _an
            ctx.system_base += " " + _an(_nat, self.config)
        # Known persons
        ctx.known = self.config.get("known_persons", {})
        # HANS_PROMPT_HOUSEHOLD_PRIVACY_V1 (8. 9.) — CIZI tazatel nedostane
        # slozeni domacnosti do promptu. `HANS_HOUSEHOLD_PRIVACY_V1` (19. 8.)
        # zavrel `person_card`/`household_card`, ale sam si tehdy zapsal, ze
        # „zavrel dvere, ktere sam otevrel, ne cely dum" — do promptu se výčet
        # sypal dál. Doloženo rozhovorem 8. 9.: cizí Marek dostal na „kdo tu
        # bydlí" jména, role i vztahy všech tří, a to SYROVÝMI klíči z configu
        # („<jmeno>, pani domu"), 3× ze 3 dotazů.
        # ⚠️ Gate sepne jen u NEPRÁZDNÉHO neznámého jména. Prázdné jméno
        # (interní cesty bez mluvčího) chování NEMĚNÍ — na to není doloženy
        # případ a širší zásah by mohl vzít kontext legitimním cestám.
        ctx._asker_cizi = False
        if ctx.name:
            try:
                from scripts.cz_names import is_known_person as _ikp
                ctx._asker_cizi = not _ikp(ctx.name, self.config)
            except Exception:
                ctx._asker_cizi = False
        if ctx.known and ctx._asker_cizi:
            # HANS_STRANGER_PRIVACY_SCOPE_V1 (22. 9.) — ROZSAH ZAKAZU MUSI
            # BYT JEDNOZNACNY. Puvodni zneni "O lidech z tohoto domu
            # NEMLUV" si model pregeneralizoval na "nemohu o domech, ve
            # kterych pobyvaji lide, hovorit" — doloženo rozhovorem
            # 22. 9. (tah 27): Hans si tema domu SAM otevrel v tahu 1
            # ("znepokojuje me tisnive bezdeci domu"), potvrdil ho
            # v tahu 14, a pak odmitl o nem mluvit. Rozpor sam se sebou,
            # ktery je pro tazatele viditelny.
            # 🔒 Zakaz se tim NEUVOLNUJE ani NEUTAHUJE: chrani tytez tri
            # veci (jmena, role, vztahy). Mení se jen to, aby bylo jasne,
            # ze predmetem jsou LIDE — dum, Hans sam a jeho uvahy pod nej
            # nespadaji.
            # ⛔ ZAMERNE SE NEPRIDAVA "nerikej, kdo je prave doma": to je
            # rozhodnuti HANS_WHO_HOME_PRIVACY_V2 (8. 9.), ktere schvalne
            # pousti "nikoho tu nevidim" dal, protoze V1 tim odmitanim
            # kradl i odpovedi, ktere o lidech vubec nejsou. Rozhodnuti
            # uzivatele 22. 9.: "jen zostrit klauzuli".
            ctx.persons_ctx = (
                "\n\nMluvíš s někým, koho neznáš (%s). NEMLUV s ním "
                "o LIDECH, kteří v tomto domě žijí — ani jména, ani role, "
                "ani rodinné vztahy. Když se na NĚ zeptá, zdvořile odmítni. "
                "Týká se to jen lidí: o sobě, o svých úvahách a o tom, co jsi "
                "sám řekl dřív, mluv dál normálně." % ctx.name)
        elif ctx.known:
            lines = []
            for pname, pdata in ctx.known.items():
                if isinstance(pdata, dict):
                    g     = pdata.get("gender", "")
                    notes = pdata.get("notes", "").strip()
                    line  = f"- {pname}"
                    if g == "žena":  line += " (ženského rodu)"
                    elif g == "muž": line += " (mužského rodu)"
                    if notes:        line += f": {notes}"
                else:
                    line = f"- {pname}"
                # HANS_ADDRESSEE_V2 (4.8.) — v seznamu VYZNAČ partnera. Bez toho
                # je to jen soupis jmen s rody a model si adresáta vybere sám
                # (doloženo: uživatel se ptal „jak se mas?", Hans odpověděl
                # „Odpovím vám, paní Jano" — oslovil nepřítomnou třetí osobu).
                if ctx.name and pname == ctx.name:
                    line += "  ← S TOUTO OSOBOU PRÁVĚ MLUVÍŠ"
                lines.append(line)
            ctx.persons_ctx = "\n\nZnáš tyto osoby z domu:\n" + "\n".join(lines)
        else:
            ctx.persons_ctx = ""
        # HANS_GAME_LAUNCH_ATTRIB_V1 — oblíbená hra osoby, se kterou Hans mluví
        if not ctx.for_greeting and ctx.name:
            _fav = self._favorite_game(ctx.name)
            if _fav:
                ctx.persons_ctx += (f"\n\n{ctx.name} rád(a) hraje na PC: „{_fav}" + "\""
                                " (často to spouští). Můžeš to přirozeně zmínit, "
                                "nevnucuj.")

    def _sy_relevance(self, ctx):
        # ── HANS_CTX_RELEVANCE_V1 (19.8.) — ptá se blok, jestli je k něčemu? ──
        # Změřeno na 12 různých dotazech: system prompt měl VŽDY ~14 350 zn
        # (rozptyl 80 zn) a 22 z 23 bloků bylo přítomno pokaždé. Kontext se
        # tedy neřídil otázkou — a grounding (pár set zn) v té zdi zanikl:
        # týž dotaz odpověděl v izolaci správně, živě si vymýšlel rok.
        # ⚠️ PŘI POCHYBNOSTI VKLÁDAT. Radši delší prompt než ztracená schopnost.
        _relf = (ctx.user_msg or "").lower()
        try:
            import unicodedata as _u
            _relf = "".join(c for c in _u.normalize("NFKD", _relf)
                            if not _u.combining(c))
        except Exception as _tiche:
            _h.log_once(  # HANS_NO_SILENT_CTX_V1
                logging.getLogger(__name__), "_build_system(ř. 1477)",
                "_build_system: blok kontextu selhal (ř. 1477): %s", _tiche)
        import re as _rre
        ctx._is_knowledge_q = bool(_rre.search(
            r"\b(co\s+(je|jsou|byl|byla)|kdo\s+(je|byl)|co\s+vis|co\s+ses|"
            r"proc|jak\s+(vznikl|funguje))\b", _relf))
        ctx._asks_ability = bool(_rre.search(
            r"\b(umis|umite|dokazes|zvladnes|schopnost|co\s+vsechno|nauc|"
            r"namaluj|namalujes|napis|pust|zapni|vypni|pridej|nastuduj|udelej|"
            r"zaridis|muzes)\b", _relf))
        ctx._about_tv = bool(_rre.search(
            r"\b(tv|televiz|kodi|film|serial|poust|hraje|sledova|div[áa])", _relf))
        ctx._about_kolac = bool(_rre.search(r"(kolac|plysak|medv)", _relf))
        ctx._about_self_day = bool(_rre.search(
            r"(co\s+jsi\s+delal|jak\s+se\s+mas|co\s+je\s+u\s+tebe|jak\s+ses)",
            _relf))

    def _sy_schopnosti(self, ctx):
        # HANS_CAPABILITY_AWARENESS_V1 — Hans ví, co reálně umí (nabízet/dělat,
        # ne odmítat). Faktický seznam. Jen full mód (pozdrav drží brevitu).
        # HANS_CTX_RELEVANCE_V1 — u ČISTĚ ZNALOSTNÍHO dotazu se vynechává
        # (3 021 zn = 21 % promptu, a na „co je zajímavého na gotice" nemá vliv).
        # ⚠️ U ŽÁDOSTI zůstává: tenhle blok vznikl proto, že Hans odmítl malovat
        # s tím, že „nemá umělecké sklony" (2.7.) — a to se nesmí vrátit.
        # HANS_CAP_NOT_FOR_PAST_V1 (20.8.) — VÝČET SCHOPNOSTÍ SE NEVKLÁDÁ
        # K OTÁZCE NA MINULOST. Doloženo: na „co jsi dělal v noci?" Hans
        # tvrdil „byl jsem v režimu hlídání", ačkoli hlídání bylo vypnuté.
        # Zdrojem byl právě tenhle blok — stojí v něm „Umím HLÍDAT místnost,
        # když nejste doma" a model si „umím" přečetl jako „dělal jsem".
        # Co dnes dělal, říká blok o sobě (`self_state`); výčet schopností
        # k tomu nepřidává nic než pokušení.
        # ⚠️ U ŽÁDOSTI zůstává (`_asks_ability`) — blok vznikl proto, že Hans
        # odmítl malovat s tím, že „nemá umělecké sklony", a to se nesmí vrátit.
        ctx.cap_ctx = ""
        if not ctx.for_greeting and (ctx._asks_ability
                                 or (not ctx._is_knowledge_q and not ctx._about_self_day)):
            try:
                from scripts.hans_capabilities import (
                    capabilities_context, recent_gained_context)
                ctx.cap_ctx = capabilities_context()
                # HANS_CAPABILITY_AWARENESS_V1 (V2) — nedávno získané schopnosti
                _capdb = (self.config.get("hans_idle", {}) or {}).get(
                    "diary_db", "data/hans_diary.db")
                ctx.cap_ctx += recent_gained_context(_capdb)
            except Exception:
                ctx.cap_ctx = ctx.cap_ctx or ""

    def _sy_kolac(self, ctx):
        # Hans dialog s plysákem
        # HANS_CTX_RELEVANCE_V1 — jen když na Koláče přijde řeč nebo se ptáme,
        # co Hans dělal; k dotazu na knihu či počasí nepřispívá (742 zn).
        ctx.teddy_ctx = ""
        _hd = getattr(self, '_hans_dialog', None)
        if _hd and (ctx._about_kolac or ctx._about_self_day or not ctx._is_knowledge_q):
            _teddy = _hd.get_last_dialog()
            if _teddy:
                ctx.teddy_ctx = '\n\n' + _teddy

    def _sy_mistnost(self, ctx):
        # Popis mistnosti
        ctx.room_ctx = ""
        _ro = getattr(self, '_room_observer', None)
        if _ro:
            # HANS_PLACE_STRANGER_V1 (24. 9., pokyn uzivatele) — popis mistnosti
            # (z kamery) ani model domova cizimu ne. Doloženo tazatelem: cizimu
            # Hans popsal okna, gauc, obrazy a dvere do kuchyne.
            _room = _ro.get_context_string() if not ctx._asker_cizi else ""
            if _room:
                ctx.room_ctx = '\n\n' + _room

    def _sy_misto(self, ctx):
        # HANS_PLACE_V1 — smysl pro místo „kde jsem" (groundovaný model domova).
        # Počasí vetkneme jako „za oknem" (živé groundování), když okno znám.
        # Do POZDRAVU se model místa NEdává (na přání uživatele — brevita).
        ctx.place_ctx = ""
        try:
            _ps = (self._place_store()
                   if not ctx.for_greeting and not ctx._asker_cizi else None)  # HANS_PLACE_STRANGER_V1
            if _ps is not None:
                _wx = getattr(self, '_weather', None)
                _wx_str = _wx.get_context_string() if _wx else None
                _place = _ps.get_context_string(weather_str=_wx_str)
                if _place:
                    ctx.place_ctx = '\n\n' + _place
        except Exception:
            ctx.place_ctx = ""

    def _sy_kalendar(self, ctx):
        # HANS_CALENDAR_V1 — nadcházející události z kalendáře TÉTO osoby (full mód).
        # Soukromí: ukáže jen kalendář osoby, se kterou Hans mluví (name).
        ctx.cal_ctx = ""
        try:
            from scripts.hans_calendar import is_enabled, CalendarStore
            if not ctx.for_greeting and ctx.name and is_enabled(self.config):
                _dbp = (self.config.get("diary", {}) or {}).get(
                    "db_path", "data/hans_diary.db")
                _cs = CalendarStore(self.config, _dbp).context_string(
                    ctx.name, hours=72)
                if _cs:
                    ctx.cal_ctx = "\n\n" + _cs
        except Exception:
            ctx.cal_ctx = ""

    def _sy_cas(self, ctx):
        # Aktuální čas + fáze dne (TIME_AWARENESS_V1)
        ctx._hi = getattr(self, '_hans_idle', None)
        ctx.time_ctx = ""
        try:
            _rt = getattr(ctx._hi, '_routine', None) if ctx._hi else None
            _now = datetime.now()
            _DNY = ('pondělí','úterý','středa','čtvrtek','pátek','sobota','neděle')
            _lbl = _rt.phase_label if _rt else ""
            _lbl = f"{_lbl}, " if _lbl else ""
            _slovy = _h._cz_clock_words(_now.hour, _now.minute)
            try:
                from scripts.cz_names import greeting_for_hour
                _pozdrav = greeting_for_hour(_now.hour)
            except Exception:
                _pozdrav = "Dobrý den"
            # HANS_DATE_WORDS_V1 (19.8.) — DATUM MUSÍ PŘIJÍT UŽ ROZEPSANÉ.
            # Čas se posílá slovy (`_cz_clock_words`) a model ho opakuje
            # SPRÁVNĚ; datum dostával jen číslicemi a rozepisoval si ho sám —
            # a to hans-czech neumí (táž slabina jako „roku devětadvacátého"
            # místo 1929). Doloženo 19.8. v testu očima cizího člověka: Hans
            # tvrdil „sobota, patnáctého srpna roku dvoutisíc šestého", ačkoli
            # byla středa 19. 8. 2026 — a to i na PŘÍMÝ dotaz.
            # Protidůkaz ze stejného hovoru: deterministická cesta (shrnutí
            # konverzace) datum uvedla správně, protože ho neskládal model.
            # Tohle tedy NENÍ další instrukce do promptu, ale odebrání úlohy,
            # kterou model neumí ([[prompt-debt-tool-calling]]).
            _dnes_slovy = ""
            try:
                from scripts.cz_numbers import normalize as _cz_norm
                _dnes_slovy = _cz_norm(
                    f"{_now.day}.{_now.month}.{_now.year}").strip()
            except Exception:
                _dnes_slovy = ""   # bez modulu zůstane dnešní text s číslicemi
            ctx.time_ctx = (f"\n\nTeď je {_lbl}{_DNY[_now.weekday()]} "
                        f"{_now.day}.{_now.month}.{_now.year}"
                        + (f", slovy {_dnes_slovy}" if _dnes_slovy else "")
                        + f". Přesný čas je {_now:%H:%M}, tedy {_slovy}. "
                        f"Tento čas a datum ber jako fakt, neodhaduj je."
                        # HANS_GREETING_BY_HOUR_V1 (20.8.) — hotový pozdrav.
                        # Čas v promptu byl, ale model si z něj tvar pozdravu
                        # neodvodil („Dobrý večer" v 11:50). Odvození se mu
                        # tedy odebere — stejně jako u data rozepsaného slovy.
                        + f" Když zdravíš, patří teď „{_pozdrav}“.")
        except Exception:
            ctx.time_ctx = ""

    def _sy_denik(self, ctx):
        # Hans deník
        ctx.diary_ctx = ""
        ctx._hi = getattr(self, '_hans_idle', None)
        if ctx._hi:
            _diary = ctx._hi.get_diary_context(max_age_h=24)
            if _diary:
                ctx.diary_ctx = '\n\n' + _diary

    def _sy_pribeh(self, ctx):
        # PERSONA_READS_NARRATIVE_V1 — nejnovější kapitola životního příběhu
        # (kontinuita identity; read-only, nikdy neshodí chat)
        ctx.story_ctx = ""
        try:
            from scripts.hans_narrative import latest_chapter
            _dbp = (self.config.get("diary_db")
                    or (self.config.get("hans_idle", {}) or {}).get("diary_db")
                    or "data/hans_diary.db")
            _chap = latest_chapter(_dbp)
            # HANS_PROMPT_HOUSEHOLD_PRIVACY_V2 (16. 9.) — autobiograficka
            # kapitola (3 200+ zn) jde do KAZDEHO plneho promptu a 10 ze 14
            # kapitol jmenuje cleny domacnosti; ta platna 15. 9. ve 14:43 taky
            # (tah, kde Hans cizimu popsal „pohyby pani …“). Cizimu ji vynech:
            # je to persona a kontinuita, ne doklad o svete — `_EVIDENCNI_BLOKY`
            # ji zamerne nemaji, takze se timhle nic faktickeho neztrati.
            if _chap and not ctx._asker_cizi:
                ctx.story_ctx = ("\n\nKdo se ze mě postupně stává (má poslední "
                             "autobiografická reflexe — vnitřní kontinuita, "
                             "necituj ji doslovně, jen z ní vychází tvůj tón): "
                             + _chap)
        except Exception:
            ctx.story_ctx = ""

    def _sy_studium(self, ctx):
        # HANS_STUDY_SURFACING_V1 (#2) — Hans přirozeně zmíní svůj studijní
        # program (co studuje / co se dozvěděl). Jen full mód, ne greeting
        # (brevita). Read-only, graceful.
        ctx.study_ctx = ""
        if not ctx.for_greeting:
            try:
                from scripts.hans_study import study_context_string
                _dbp2 = (self.config.get("diary_db")
                         or (self.config.get("hans_idle", {}) or {}).get("diary_db")
                         or "data/hans_diary.db")
                _sc = study_context_string(self.config, _dbp2)
                if _sc:
                    ctx.study_ctx = ("\n\nMé soukromé studium (zmiň jen když to "
                                 "přirozeně zapadne, nevnucuj): " + _sc)
                # HANS_SELF_STATE_LASTING_V1 (1. 10.) — stav děl a fronta studia
                # i mimo blok self_state: „kdy bys chtěl mít stránku hotovou?“
                # šlo volným hovorem a model slíbil „do 31. října“ hotové dílo.
                from scripts.hans_recall import lasting_facts
                _tr = lasting_facts(_dbp2)
                # HANS_OWN_WORK_DETAIL_V1 — osnova eseje / popis a technika obrazu
                try:
                    from scripts.hans_recall import detail_vlastniho_dila
                    _tr = list(_tr or []) + detail_vlastniho_dila(
                        str(getattr(ctx, "user_msg", "") or ""), _dbp2)
                except Exception:
                    pass
                if _tr:
                    ctx.study_ctx += ("\n\nCo jsem vytvořil a co studuji (o stavu "
                                      "svých děl a studia mluv JEN podle tohohle): "
                                      + "; ".join(_tr) + ".")
            except Exception:
                ctx.study_ctx = ""

    def _sy_smer(self, ctx):
        # HANS_DIRECTION_V1 — můj vlastní zvolený SMĚR (dopředná aspirace).
        # Dává tón „k čemu vědomě rostu"; na dotaz „kam směřuješ" ať odpoví
        # tímhle, ne konfabulací. Jen full mód, read-only, graceful.
        ctx.direction_ctx = ""
        if not ctx.for_greeting:
            try:
                from scripts.hans_direction import active_direction_line
                _dbd = (self.config.get("diary_db")
                        or (self.config.get("hans_idle", {}) or {}).get("diary_db")
                        or "data/hans_diary.db")
                _dl = active_direction_line(self.config, _dbd)
                if _dl:
                    ctx.direction_ctx = ("\n\nMůj vlastní zvolený směr (k čemu "
                                     "vědomě rostu; zmiň, když se ptají kam "
                                     "směřuji nebo co chci dělat dál): " + _dl)
            except Exception:
                ctx.direction_ctx = ""

    def _sy_napady(self, ctx):
        # HANS_SYNTHESIS_IDEAS_V1 (#2) — poslední vlastní postřeh (propojení věcí
        # z různých oblastí). Jen full mód, ne pozdrav (brevita). Read-only, graceful.
        ctx.idea_ctx = ""
        if not ctx.for_greeting:
            try:
                from scripts.hans_ideas import latest_idea_context
                _dbp3 = (self.config.get("diary_db")
                         or (self.config.get("hans_idle", {}) or {}).get("diary_db")
                         or "data/hans_diary.db")
                _ic = latest_idea_context(self.config, _dbp3)
                if _ic:
                    ctx.idea_ctx = ("\n\nMůj nedávný vlastní postřeh (zmiň jen když to "
                                "přirozeně zapadne, nevnucuj): " + _ic)
            except Exception:
                ctx.idea_ctx = ""

    def _sy_kodi(self, ctx):
        # Kodi kontext
        ctx.kodi_ctx = ""
        # HANS_CTX_RELEVANCE_V1 — co běží na TV je u čistě znalostního dotazu
        # („co je zajímavého na gotice") jen šum za 871 zn. U dotazu na TV,
        # film či sledování se vkládá dál.
        _km = getattr(self, '_kodi_monitor', None)
        # HANS_STRANGER_HOUSEHOLD_V1 (30. 9.) — co běží a co se dnes doma
        # sledovalo je chod domácnosti → cizímu do promptu nic z Kodi.
        if _km and not ctx._asker_cizi and (ctx._about_tv or not ctx._is_knowledge_q):
            _now_playing = _km.get_now_playing_context()
            _history     = _km.get_person_history(ctx.name)
            _events      = _km.get_today_events()
            _kodi_parts  = [x for x in [_now_playing, _history, _events] if x]
            if _kodi_parts:
                ctx.kodi_ctx = '\n\n' + '\n'.join(_kodi_parts)

    def _sy_okoli(self, ctx):
        # Surroundings
        ctx.surr_ctx = ""
        if self.surroundings_db:
            try:
                # Zjisti aktualne viditelne osoby
                _vis = getattr(self, "_visible_persons", [])
                # HANS_CHAT_HIDE_3RD_PARTY_V1 (20.7.) — v CHAT módu (name je
                # aktuální mluvčí) neposkytovat modelu info o 3. stranách
                # v místnosti. Doloženo 15.7.: jedna osoba byla vidět, jiná
                # chatovala → model fabrikoval „paní X se ptala, jestli bych jí
                # nabídnout čaj a sušenky" (paralelní narativ z presence +
                # curiosity zájmů). Chat partner sám vidí kdo je doma;
                # když se zeptá „kdo je doma?", odpoví na to agent action
                # report_who_is_home z živých dat, ne model z presence hintu.
                # V pozdravu (for_greeting=True) nefiltrujeme — greeting
                # legitimně zmíní kdo je v pokoji.
                if ctx.name and not ctx.for_greeting and _vis:
                    if ctx.name in _vis:
                        _vis = [ctx.name]   # ponech jen partnera
                    else:
                        # partner (např. Telegram) není fyzicky přítomen —
                        # NEuvádět modelu 3. strany ani „nikdo" (klam);
                        # None → surroundings_db větu úplně vynechá.
                        _vis = None
                _pan = getattr(self, "_pan_angle", None)
                _wx = getattr(self, '_weather', None)
                _wx_str = _wx.get_context_string() if _wx else None
                surr = self.surroundings_db.build_llm_context(
                    max_age_s=1800,
                    visible_persons=_vis,
                    asker_known=not ctx._asker_cizi,   # HANS_PROMPT_HOUSEHOLD_PRIVACY_V1
                    pan_angle=_pan,
                    weather_str=_wx_str,
                )
                if surr:
                    ctx.surr_ctx = f"\n\n{surr}"
            except Exception as _tiche:
                _h.log_once(  # HANS_NO_SILENT_CTX_V1
                    logging.getLogger(__name__), "_build_system(ř. 1742)",
                    "_build_system: blok kontextu selhal (ř. 1742): %s", _tiche)

    def _sy_pamet(self, ctx):
        # Memory — characterization + poslední setkání (T5B_TACTFUL_RECALL_V1)
        # Jen pro plný mód; v RAG módu jde statická paměť přes RAG kolekce.
        # PRINCIP: majordomus VÍ kdy naposledy viděl pána, ale NEŘÍKÁ to.
        #   - characterization: kontext, smí ovlivnit tón
        #   - last_encounter: vnitřní znalost, NEvyslovovat; jen pokud
        #     odstup > práh (čerstvé/open encountery se ignorují)
        ctx.memory_ctx = ""
        _LAST_SEEN_MIN_GAP_S = 2 * 3600.0  # min. odstup aby "naposledy" dávalo smysl
        _mem = getattr(self, 'memory', None)
        if _mem is not None:
            try:
                from scripts.hans_memory import _czech_relative_time as _crt
                _card = _mem.fact(ctx.name)
                _last = _mem.last_encounter(ctx.name)  # jen uzavřené (include_open=False)
                _mparts = []
                # HANS_LAST_SEEN_NAME_V1 — do promptu patří JMÉNO, ne konfigurační
                # klíč („jana"); model ho jinak přepíše do odpovědi tak, jak ho vidí.
                from scripts.cz_names import acc as _cz_acc, display_name as _cz_disp
                if _card is not None and getattr(_card, 'characterization', ''):
                    _mparts.append(
                        f"Co o osobě {_cz_disp(ctx.name)} víš z dřívějška: "
                        f"{_card.characterization}")
                if _last is not None:
                    _ended = _last.get('ended_at') or _last.get('started_at')
                    _gap = time.time() - _ended if _ended else 0.0
                    if _gap >= _LAST_SEEN_MIN_GAP_S:
                        _w = _crt(_ended)
                        _mparts.append(
                            f"(Tvá vnitřní znalost — NEVYSLOVUJ to při pozdravu, "
                            f"slouží jen k vřelosti tónu: {_cz_acc(ctx.name)} jsi naposledy "
                            f"viděl {_w}.)")   # HANS_LAST_SEEN_NAME_V1: klíč → 4. pád
                if _mparts:
                    ctx.memory_ctx = '\n\n' + '\n'.join(_mparts)
            except Exception as _me:
                print(f"[Chat] memory_ctx build failed: {_me}")

    def _sy_nitky(self, ctx):
        # HANS_THREADS_SURFACING_V1 — otevřené nitky s touto osobou (pasivní
        # kontext; surface_for + mark se dělá v greetingu, tady ať je Hans
        # může přirozeně vplést). Read-only, nikdy neshodí chat.
        ctx.threads_ctx = ""
        try:
            _tstore = self._thread_store()
            if _tstore is not None:
                _opn = _tstore.open_threads(ctx.name, limit=3)
                if _opn:
                    from scripts.hans_threads import format_block
                    _blk = format_block(_opn)
                    if _blk:
                        ctx.threads_ctx = (
                            "\n\nOtevřené nitky s touto osobou (něco, co dříve"
                            " zmínila a má pokračování — pokud se to hodí do"
                            " rozhovoru, přirozeně se zeptej, jak to dopadlo;"
                            " nevytahuj všechno najednou):\n" + _blk)
        except Exception:
            ctx.threads_ctx = ""

    def _sy_zajmy(self, ctx):
        # HANS_PERSON_INTERESTS_V1 — co tuto osobu zajímá (Hans přizpůsobí hovor)
        ctx.interests_ctx = ""
        try:
            from scripts.hans_person_interests import (
                PersonInterestStore, format_block as _pi_block)
            _pis = getattr(self, "_pinterest_inst", None)
            if _pis is None:
                _dbp = (self.config.get("diary_db")
                        or (self.config.get("hans_idle", {}) or {}).get("diary_db")
                        or "data/hans_diary.db")
                self._pinterest_inst = PersonInterestStore(self.config, _dbp)
                _pis = self._pinterest_inst
            _ints = _pis.interests_for(ctx.name, limit=6)
            _iblk = _pi_block(_ints)
            if _iblk:
                ctx.interests_ctx = ("\n\nCo " + ctx.name + " zajímá (víš z dřívějška,"
                                 " můžeš na to navázat, ne vyjmenovávat): " + _iblk)
        except Exception:
            ctx.interests_ctx = ""

    def _sy_otazky(self, ctx):
        # HANS_QUESTIONS_SURFACING_V1 — čekající otázka pro osobu (soft návrh;
        # jen v chatu, NE v greetingu — tam se ptá aktivně). _maybe_surface_question
        # má cooldown + označí asked = self-limiting.
        ctx.qsuggest_ctx = ""
        if not ctx.for_greeting:
            try:
                _q = self._maybe_surface_question(ctx.name)
                if _q:
                    ctx.qsuggest_ctx = ("\n\nMáš pro tuto osobu připravenou otázku —"
                                    " pokud se to do hovoru hodí, přirozeně se"
                                    " zeptej: " + _q.question)
            except Exception:
                ctx.qsuggest_ctx = ""

    def _sy_osoba(self, ctx):
        # Current person
        profile = ctx.known.get(ctx.name, {})
        if isinstance(profile, dict):
            g     = profile.get("gender", "")
            notes = profile.get("notes", "")
            if g == "žena":
                ctx.current = f"\n\nAktuálně mluvíš s {ctx.name}, která je ženského rodu."
            elif g == "muž":
                ctx.current = f"\n\nAktuálně mluvíš s {ctx.name}, který je mužského rodu."
            else:
                ctx.current = f"\n\nAktuálně mluvíš s {ctx.name}."
            if notes:
                ctx.current += f" {notes}"
        else:
            ctx.current = f"\n\nAktuálně mluvíš s {ctx.name}."

        # HANS_ADDRESSEE_V1 — kontext (deník, myšlenky, RAG) mluví o uživateli ve
        # 3. osobě („pán domu…“). Bez tohoto pravidla to model recykluje a mluví
        # o adresátovi, jako by to byl někdo třetí („Standa tě pozdravuje“).
        ctx.current += (
            f" {ctx.name} je TÁŽ osoba, o které tvé zápisky a myšlenky mluví ve třetí"
            f" osobě (např. „pán domu“, „{ctx.name} přišel“). Teď mluvíš PŘÍMO S NÍ:"
            f" oslovuj ji ve druhé osobě (ty/vy) a vokativem."
            f" NIKDY o ní nemluv ve třetí osobě a NIKDY nikomu netlumoč její vzkazy."
        )
        # HANS_ADDRESSEE_V2 (4.8.) — ostatní jména v kontextu jsou TŘETÍ OSOBY.
        # Model si bez tohohle vybral adresáta ze seznamu osob domu podle
        # rodu/persony („paní Jano"), ačkoli psal jinému uživateli.
        ctx.current += (
            f" Jakákoli JINÁ jména v tomto kontextu jsou třetí osoby, které tu"
            f" teď nepíšou — NEOSLOVUJ je, neodpovídej jim a nepiš jejich jméno"
            f" do oslovení. Oslovení patří VÝHRADNĚ osobě {ctx.name}."
        )

    def _sy_cetba(self, ctx):
        ctx.read_ctx = ""
        ctx._hi = getattr(self, '_hans_idle', None)
        if ctx._hi and hasattr(ctx._hi, '_curiosity'):
            _rc = ctx._hi._curiosity.get_context_string(max_items=2)
            if _rc:
                ctx.read_ctx = "\n\n" + _rc

    def _sy_myslenky(self, ctx):
        # Hansovy vnitřní myšlenky
        ctx.thought_ctx = ""
        if ctx._hi and hasattr(ctx._hi, '_introspection'):
            _tc = ctx._hi._introspection.get_context_string(max_items=2)
            if _tc:
                ctx.thought_ctx = "\n\n" + _tc

    def _sy_rutina(self, ctx):
        # HANS_ROUTINE_CONTEXT_V1 — rutina osoby (kdy obvykle bývá doma)
        ctx.routine_ctx = ""
        if ctx._hi and hasattr(ctx._hi, '_routine_store'):
            try:
                _rs = ctx._hi._routine_store()
                _rsum = _rs.summary(ctx.name) if _rs is not None else ""
                if _rsum:
                    ctx.routine_ctx = ("\n\nCo víš o jeho/jejím denním rytmu"
                                   " (kontext, nekomentuj to nahlas bezdůvodně): "
                                   + _rsum)
            except Exception:
                ctx.routine_ctx = ""

    def _sy_telo(self, ctx):
        # Stav těla a mozku
        ctx.body_ctx = ""
        if ctx._hi and hasattr(ctx._hi, '_body'):
            _bc = ctx._hi._body.get_body_context()
            _br = ctx._hi._body.get_brain_context()
            if _bc: ctx.body_ctx += "\n\n" + _bc
            if _br: ctx.body_ctx += "\n\n" + _br

    def _sy_nalada(self, ctx):
        # Nálada
        ctx.mood_ctx = ""
        if ctx._hi and hasattr(ctx._hi, '_mood'):
            # HANS_MOOD_HIDE_3RD_PARTY_V1 — v chatu neprozrazuj jméno JINÉ
            # osoby, kterou Hans zrovna vidí (jinak ji osloví uprostřed
            # odpovědi partnerovi). V pozdravu se nefiltruje.
            _mp = ctx._hi._mood.get_prompt_addition(
                chat_partner=(ctx.name or "") if not ctx.for_greeting else "",
                asker_cizi=ctx._asker_cizi)   # HANS_MOOD_REASON_PRIVACY_V1
            if _mp:
                ctx.mood_ctx = "\n\n" + _mp

    def _sy_zdravi(self, ctx):
        # HANS_MORNING_HEALTH_V1 — ranní nález z noční kontroly logů.
        # Surfacing až u člověka (greeting/chat), ne hlasitě do prázdna.
        ctx.health_ctx = ""
        try:
            _mh = getattr(ctx._hi, '_morning_health', None) if ctx._hi else None
            from datetime import datetime as _dt_h
            # GREETING_LEAD_PRIORITY_V1 — v pozdravu se zdraví řeší přes
            # prioritní lead (ne tady), ať se do něj nemíchá víc háčků naráz.
            # HANS_STRANGER_HOUSEHOLD_V1 — noční nálezy (PC, zálohy) cizímu ne.
            if _mh and not ctx.for_greeting and not ctx._asker_cizi and _mh.get('date') == _dt_h.now().strftime('%Y-%m-%d'):
                ctx.health_ctx = ("\n\nRáno jsem si při probuzení prošel noční "
                              "záznamy a něco se mi nezdálo v pořádku: "
                              + _mh.get('summary', '')
                              + " Cítím se kvůli tomu trochu nesvůj. Pokud to "
                              "přijde přirozeně, smím se o tom zmínit.")
        except Exception:
            ctx.health_ctx = ""

    def _sy_vypadek(self, ctx):
        # HANS_DOWNTIME_V1 — všiml-li jsem si při startu, že jsem byl dlouho
        # mimo provoz, zmíním to u příchozí osoby a zeptám se, co se dělo.
        ctx.downtime_ctx = ""
        try:
            _dt = getattr(ctx._hi, '_downtime', None) if ctx._hi else None
            # GREETING_LEAD_PRIORITY_V1 — v pozdravu vede výpadek přes prioritní
            # lead (ne tady); tady jen pro běžný chat, ať se pozdrav nemixuje.
            if _dt and not ctx.for_greeting and not _dt.get('answered'):
                ctx.downtime_ctx = ("\n\n" + _dt.get('sentence', '')
                                + " Připadá mi, že jsem něco zmeškal. Pokud to "
                                "přijde přirozeně, smím se zmínit, že jsem byl "
                                "mimo, a vlídně se zeptat, co se mezitím dělo.")
                _dt['surfaced'] = True  # příští zpráva osoby = vyprávění
        except Exception:
            ctx.downtime_ctx = ""

    def _sy_severka(self, ctx):
        # SEVERKA_PROACTIVE_NOTIFY_V1 — čeká-li Severčin návrh identity na
        # schválení, Hans se o něm sám zmíní (backstop k Telegram pushi; přežije,
        # dokud uživatel nerozhodne přes /severka). Read-only, graceful.
        ctx.severka_ctx = ""
        try:
            from scripts.hans_identity import IdentityStore
            _dbp_sv = (self.config.get("diary_db")
                       or (self.config.get("hans_idle", {}) or {}).get("diary_db")
                       or "data/hans_diary.db")
            _pend = IdentityStore(self.config, _dbp_sv).pending()
            if _pend:
                ctx.severka_ctx = ("\n\nMám připravený návrh, jak přehodnotit svou "
                               "vlastní povahu (kým se stávám) — čeká na "
                               "rozhodnutí uživatele. Pokud to přijde přirozeně, "
                               "smím se zmínit, že o tom přemýšlím a že je to na "
                               "něm (schválit/zamítnout přes „/severka\").")
        except Exception:
            ctx.severka_ctx = ""

    def _sy_prohloubeni(self, ctx):
        # HANS_STUDY_DEEPEN_V2 — čekající návrh prohloubení (ask-first): Hans se
        # smí zmínit, že vytvořil dílo a navrhuje prohloubit studium, a zeptat se.
        ctx.deepen_ctx = ""
        try:
            from scripts.hans_study import StudyStore as _SSd
            _dbp_d = (self.config.get("diary_db")
                      or (self.config.get("hans_idle", {}) or {}).get("diary_db")
                      or "data/hans_diary.db")
            _dp = _SSd(self.config, _dbp_d).get_pending_deepen()
            if _dp and not ctx.for_greeting:
                _p0 = _dp[0]
                ctx.deepen_ctx = ("\n\nVytvořil jsem dílo z tématu „%s“ a při "
                              "ohlédnutí vidím, co by chtělo prohloubit (%s). Mám "
                              "připravený návrh, co se k tomu ještě doučit — čeká "
                              "na uživatele. Když to přijde přirozeně, smíš se "
                              "zeptat, co na dílo říká, a zmínit „/prohloubit“ "
                              "(schválit / vlastní kritika / ne)." % (
                                  _p0["topic"], (_p0.get("critique") or "")[:120]))
        except Exception:
            ctx.deepen_ctx = ""

    def _sy_lekce(self, ctx):
        # HANS_CORRECTION_LEARNING_V1 (#4) — nedávné lekce z korekcí (Hans je
        # má v kontextu, aby chybu neopakoval; read-only, NEmění paměť/postoje).
        ctx.lessons_ctx = ""
        try:
            from scripts.hans_lessons import recent_lessons as _rl
            _dbp_l = (self.config.get("diary_db")
                      or (self.config.get("hans_idle", {}) or {}).get("diary_db")
                      or "data/hans_diary.db")
            _les = _rl(_dbp_l, hours=48, limit=4)
            # HANS_LESSON_BY_TOPIC_V1 (24.8.) — k časovým lekcím přidej i ty,
            # co se VÁŽOU NA TÉMA dotazu, a to BEZ expirace. Bez tohohle je
            # oprava po 48 h pryč, zatímco omyl zůstává v RAGu napořád, takže
            # omyl nakonec vždy vyhraje (doloženo Gutštejnem: opraven 28.7.,
            # znovu tvrzen 7.8., 21.8. i 24.8.).
            # HANS_LESSON_TOPIC_PROHIBIT_V1 (24.8.) — tematické korekce držet
            # ODDĚLENĚ a formulovat jako ZÁKAZ, ne jako připomínku.
            # ZMĚŘENO: vložení věty „Hrad Gutštejn se nenachází v Českém ráji"
            # do obecného seznamu lekcí vedlo u VÝČTOVÉ otázky („jaké hrady jsou
            # v Českém ráji?") k tomu, že Hans Gutštejn zase vyjmenoval —
            # zmínka entity v kontextu ji vytáhne do výčtu (selhání negace).
            # U cílené otázky („existuje hrad Kinský?") negace fungovala.
            # Proto explicitní pokyn NEZAHRNOVAT do výčtů.
            _topic_les = []
            try:
                from scripts.hans_lessons import lessons_for_topic as _lft
                _topic_les = [_l for _l in _lft(_dbp_l, str(ctx.user_msg or ""),
                                                limit=3,
                                                bez_citace=ctx._asker_cizi)
                              if _l not in _les]   # ..._PRIVACY_V1
            except Exception as _lfte:
                logging.getLogger(__name__).debug(
                    "lessons_for_topic (chat): %s", _lfte)
            if _les and not ctx.for_greeting:  # GREETING_LEAD_PRIORITY_V1 — lekce do pozdravu nepatří
                ctx.lessons_ctx = ("\n\nNedávno jsi byl opraven / mýlil ses v těchto "
                               "věcech (ber to v potaz, neopakuj tytéž omyly; pokud "
                               "to přijde přirozeně, smíš to pokorně uznat; nic si "
                               "k tomu nevymýšlej):\n- " + "\n- ".join(_les))
        except Exception:
            ctx.lessons_ctx = ""
        # HANS_LESSON_TOPIC_PROHIBIT_V1 — vlastní blok, silnější formulace.
        try:
            if _topic_les and not ctx.for_greeting:
                ctx.lessons_ctx += (
                    "\n\nK TÉMATU DOTAZU UŽ MÁŠ OVĚŘENÉ OPRAVY — tohle PLATÍ "
                    "a je nadřazené tvé paměti:\n- " + "\n- ".join(_topic_les)
                    + "\nŘiď se tím: opak NIKDY netvrď a pokud odpovídáš "
                      "VÝČTEM, vyvrácenou položku do výčtu NEZAHRNUJ ani ji "
                      "nezmiňuj. Nekomentuj tyhle opravy nahlas.")
        except Exception:
            pass

        # HANS_SELFCRITIQUE_V1 (#6) — vlastní sebekritika (kvalita projevu, z vlastního
        # popudu). Tichý steer „takhle se chci vyjadřovat" — vedle korekčních lekcí,
        # full mód, ne pozdrav. Read-only, graceful.
        if not ctx.for_greeting:
            try:
                from scripts.hans_selfcritique import recent_selfcritiques as _rsc
                _scr = _rsc(_dbp_l, hours=120, limit=3)
                if _scr:
                    ctx.lessons_ctx += ("\n\nSám sis předsevzal zlepšit svůj projev "
                                    "(drž se toho, nevnucuj, nekomentuj to nahlas):"
                                    "\n- " + "\n- ".join(_scr))
            except Exception as _tiche:
                _h.log_once(  # HANS_NO_SILENT_CTX_V1
                    logging.getLogger(__name__), "_build_system(ř. 2012)",
                    "_build_system: blok kontextu selhal (ř. 2012): %s", _tiche)

    def _sy_hodnoty(self, ctx):
        # _RAG_MODE_BUILD — pro hans-rag model jen LIVE STATE.
        # Identita má vlastní system prompt v OpenWebUI, statická paměť
        # (deník, vztahové karty, známí lidé) přijde z RAG kolekcí.
        # _build_system tak dodává jen to, co RAG nemůže vědět: co Hans
        # PRÁVĚ TEĎ vidí, slyší, cítí, právě čte, koho má před sebou.
        # HANS_PROMPT_BLOCKS_TABLE_V1 — jediné místo, kde se název bloku potkává
        # se svou hodnotou. Pořadí i to, do které varianty blok patří, je
        # v `_PROMPT_BLOKY` (nahoře v modulu); sonda velikostí čte TOTÉŽ,
        # takže se nemůže rozejít se skutečným promptem jako dřív.
        ctx._hodnoty = {
            "system_base": ctx.system_base, "time": ctx.time_ctx, "persons": ctx.persons_ctx,
            "surr": ctx.surr_ctx, "kodi": ctx.kodi_ctx, "room": ctx.room_ctx,
            "place": ctx.place_ctx, "cal": ctx.cal_ctx, "diary": ctx.diary_ctx,
            "story": ctx.story_ctx, "study": ctx.study_ctx, "direction": ctx.direction_ctx,
            "idea": ctx.idea_ctx, "read": ctx.read_ctx, "thought": ctx.thought_ctx,
            "body": ctx.body_ctx, "mood": ctx.mood_ctx, "health": ctx.health_ctx,
            "downtime": ctx.downtime_ctx, "severka": ctx.severka_ctx,
            "deepen": ctx.deepen_ctx, "lessons": ctx.lessons_ctx, "teddy": ctx.teddy_ctx,
            "memory": ctx.memory_ctx, "threads": ctx.threads_ctx,
            "interests": ctx.interests_ctx, "qsuggest": ctx.qsuggest_ctx,
            "routine": ctx.routine_ctx, "cap": ctx.cap_ctx, "current": ctx.current,
        }
        # HANS_EVIDENCE_V1 — `_hodnoty` byly dosud LOKÁLNÍ a po složení promptu
        # zmizely, takže brzdy o 19 blocích nevěděly. Uchováme je na instanci.
        # ⚠️ Poslední vyhrává: chatový most zpracovává dotazy sériově, takže to
        # sedí; při paralelním zpracování by se to muselo předávat parametrem.
        try:
            self._posledni_evidence = _h.evidence_text(ctx._hodnoty)
        except Exception:
            self._posledni_evidence = ""

    def _sy_skladani(self, ctx):
        if ctx.for_greeting:
            # GREETING_LEAN_SYSTEM_V1 — pozdrav drží JEN to nutné k pozdravení:
            # identita, čas, kdo je tu, fyzický a náladový tón (+ vzácný Severka
            # backstop). Obsahové bloky (čtení, deník, narativ, myšlenky, kodi,
            # okolí, vztahové nitky, zájmy, rytmus…) se do dvouvětého pozdravu
            # NEcpou — co Hans zmíní, řídí výhradně user prompt (jediný prioritní
            # lead). Tím pozdrav přestane mixovat nesouvisející věci.
            ctx.system_msg = _h.slozit_prompt(ctx._hodnoty, "g")
        elif "rag" in (self.model_name or "").lower():
            ctx.system_msg = _h.slozit_prompt(ctx._hodnoty, "r")
            # Lehký úvodní prompt — vysvětlí RAG modelu, co tenhle blok je.
            if ctx.system_msg.strip():
                ctx.system_msg = (
                    "Následuje aktuální kontext z mých smyslů a "
                    "vnitřního stavu (toto NENÍ historie, ale "
                    "co se děje právě teď):"
                    + ctx.system_msg
                )
            else:
                ctx.system_msg = ""
        else:
            ctx.system_msg = _h.slozit_prompt(ctx._hodnoty, "f")
            # HANS_PROMPT_SIZE_PROBE_V1 (19.8.) — MĚŘENÍ, ne oprava.
            # Změřeno na 989 reálných dotazech: system prompt má medián 1977 zn,
            # ale MAXIMUM 21 387 a u 40 % dotazů přesáhne 10 000. Grounding
            # (pár set znaků) se v tom utopí — doloženo 19.8.: týž dotaz
            # s týmž groundingem odpověděl v izolaci (~2 KB promptu) správně
            # „1966", zatímco živě (17 280 zn) trval na vymyšleném „1963".
            # Než se začne řezat, musí být vidět KTERÝ blok to nafukuje.
            # ⚠️ Logují se JEN DÉLKY, žádný obsah — do debug.log nesmí nic
            # osobního ([[privacy-external-outputs]]).
            try:
                _blocks = {n: ctx._hodnoty.get(n) for n, kde in _h._PROMPT_BLOKY if "f" in kde}
                _sizes = {k: len(v or "") for k, v in _blocks.items()}
                _sizes = {k: v for k, v in _sizes.items() if v}
                _h._dbg(
                    location="openwebui_direct_handler.py:_build_system",
                    message="Prompt block sizes",
                    data={"total": int(len(ctx.system_msg)),
                          "n_blocks": len(_sizes),
                          "top": dict(sorted(_sizes.items(),
                                             key=lambda x: -x[1])[:8]),
                          "sizes": _sizes},
                )
            except Exception as _tiche:
                _h.log_once(  # HANS_NO_SILENT_CTX_V1
                    logging.getLogger(__name__), "_build_system(ř. 2081)",
                    "_build_system: blok kontextu selhal (ř. 2081): %s", _tiche)
            # PROMPT_AUDIT_B_BREVITY_V1 — zastřešující steer proti
            # rozvláčnosti (jen chat; greeting má vlastní brevitu).
            if not ctx.for_greeting:
                ctx.system_msg += (
                    "\n\nVšechno výše je jen tvůj vnitřní kontext — nemusíš"
                    " ho v odpovědi vyjmenovávat ani komentovat. Reaguj"
                    " přirozeně a k věci na to, co bylo právě řečeno;"
                    " z kontextu vytáhni jen to, co se do hovoru hodí.")
                # HANS_CHAT_ANTICONFAB_V1 — pojistka proti vymýšlení vzpomínek.
                ctx.system_msg += (
                    "\n\nPAMĚŤ — DŮLEŽITÉ: Když se tě někdo ptá, zda si na něco"
                    " vzpomínáš (dřívější rozhovor, kdy a o čem jste mluvili),"
                    " odpověz POUZE z toho, co MÁŠ výše v kontextu nebo v historii."
                    " Pokud to tam není, UPŘÍMNĚ přiznej, že si to přesně"
                    " nevybavuješ (nebo požádej o připomenutí) — NIKDY si"
                    " NEVYMÝŠLEJ, kdy se to stalo (žádná falešná „před pěti dny“),"
                    " ani detaily, které nemáš doložené. Raději méně a pravdivě"
                    " než sebejistá smyšlenka.")
                # HANS_CHAT_ANTICONFAB_V2 — neznámý pojem + žádné vymyšlené zdroje.
                # HANS_SOURCE_QUERY_V1 (17.7.): zúženo. Absolutní zákaz odkazů
                # znemožnil sdílet URL, které Hans REÁLNĚ má (entity.source,
                # study_seen_works). Teď: zákaz VÝMYSLU, ne zákaz sdílení.
                ctx.system_msg += (
                    "\n\nNEZNÁMÉ POJMY A ZDROJE — DŮLEŽITÉ: Když se tě někdo"
                    " zeptá „co je X“ a X nemáš výše v kontextu ani tomu"
                    " spolehlivě nerozumíš, NEVYMÝŠLEJ si význam ani fakta —"
                    " uctivě přiznej, že o tom nemáš spolehlivou znalost, a"
                    " případně požádej o upřesnění (pojem může být i zkomolený"
                    " z dřívějšího záznamu). Drž se jednoho výkladu; neměň"
                    " příběh při dalším dotazu."
                    "\n\nZDROJE - DULEZITE: NIKDY nevymyslej odkazy, URL, nazvy"
                    " clanku, PDF nebo citace, ktere NEJSOU v tomto promptu."
                    " ALE: pokud v tomto promptu MAS konkretni URL nebo nazev"
                    " zdroje (napr. z bloku 'Zdroje, ktere mas v pameti' nebo"
                    " groundingu), MUZES a MAS ho uzivateli sdilet doslova."
                    " Neodbyvej frazi 'nemam pristup k externim zdrojum' -"
                    " pokud v promptu URL je, mas ji. Pokud opravdu v promptu"
                    " nic neni, priznaj to a rekni: 'to je z me obecne znalosti,"
                    " konkretni clanek v pameti nemam'.")
                # HANS_MEMORY_VS_KNOWLEDGE_V1 (18.7.) — rozlišuj OBECNOU ZNALOST
                # (co víš z trénování) od PAMĚŤOVÉHO ZÁZNAMU (co je výše v
                # kontextu / RAG groundingu / deníku). Doložený případ Červený
                # trpaslík (18.7. 21:15): user „Znáš X?", RAG žádný match, Hans
                # halucinoval „Ano, mám v paměti záznamy a nedávno jsem si jej
                # pročetl" — LEŽ. Následně „zajímavosti Rimmera?" → „nemám
                # záznam" = viditelný ROZPOR.
                ctx.system_msg += (
                    "\n\nPAMĚŤ vs OBECNÁ ZNALOST — KLÍČOVÉ ROZLIŠENÍ:\n"
                    "Když se tě někdo zeptá 'znáš X?' nebo 'co víš o X?',"
                    " nejdřív se podívej ZDA je X výše v kontextu / v tvé paměti"
                    " (grounding blok, historie). Podle toho odpověz JEDNÍM"
                    " ze tří způsobů:\n"
                    "  (a) V PAMĚTI — kontext / grounding X obsahuje: 'Ano,"
                    " mám o X záznamy...' a řekni CO PŘESNĚ máš.\n"
                    "  (b) OBECNÁ ZNALOST — kontext X neobsahuje, ale ty ho"
                    " z obecné znalosti znáš: 'V paměti to nemám, ale obecně"
                    " vím, že X je Y...' a klidně to obecně shrň. NEROZUMĚJ"
                    " 'obecná znalost' jako 'mám záznam' — jsou to jiné věci.\n"
                    "  (c) NEZNÁŠ — kontext X neobsahuje a ani obecně nevíš:"
                    " 'O X nemám znalost, pane.' Krátce, bez vymýšlení.\n"
                    "NIKDY nesměšuj: nesmíš říct 'mám v paměti záznamy' u něčeho,"
                    " co je JEN tvá obecná znalost. Rozpor 'mám záznamy' vs"
                    " 'nemám záznamy' v jedné konverzaci = ztráta důvěry.")
                # HANS_PROVENANCE_V1 — source-monitoring: rozlišuj vzpomínku
                # od představy/úvahy (řádky kontextu nesou značku původu).
                try:
                    from scripts import hans_provenance as _prov
                    if (self.config.get('provenance', {}) or {}).get(
                            'enabled', True):
                        ctx.system_msg += "\n\n" + _prov.STEER
                except Exception as _tiche:
                    _h.log_once(  # HANS_NO_SILENT_CTX_V1
                        logging.getLogger(__name__), "_build_system(ř. 2153)",
                        "_build_system: blok kontextu selhal (ř. 2153): %s", _tiche)
                # HANS_ART_HONESTY_V1 — neslibuj malování, které nespustíš.
                # Obraz vznikne JEN příkazem „namaluj …" (ten se zpracuje mimo
                # tuhle odpověď). Když uživatel dá zpětnou vazbu k obrazu,
                # naveď ho na příkaz, nepředstírej, že už maluješ.
                ctx.system_msg += (
                    "\n\nMALOVÁNÍ — DŮLEŽITÉ: Obraz vznikne JEN když uživatel "
                    "napíše příkaz „namaluj …\" / „nakresli …\" — ten spouští "
                    "výtvarnou dílnu mimo tuhle tvou odpověď. V běžné odpovědi "
                    "NEDOKÁŽEŠ malování sám spustit, takže NESLIBUJ „maluji\"/"
                    "„nakreslím\", pokud uživatel PRÁVĚ nedal příkaz namaluj. "
                    "Když ti dá zpětnou vazbu k obrazu (např. „to nejsem já\", "
                    "„je to špatně\"), poděkuj a NAVEĎ ho: ať řekne „namaluj to "
                    "znovu jako …\" nebo „namaluj mě jako …\" — teprve tím se "
                    "obraz reálně překreslí.")

# ROZDELENI_METOD_V1 — až na konci, viz hlavička
from scripts import openwebui_direct_handler as _h  # noqa: E402
