"""Metody třídy `OpenWebUIDirectHandler` přesunuté z `scripts/openwebui_direct_handler.py` (ROZDELENI_METOD_V1).

Text metod je beze změny; jména původního modulu se čtou přes `_h.` až při
volání. `OpenWebUIDirectHandler` má třídu `GroundingMixin` mezi předky, takže volání přes `self` platí dál.
Import původního modulu je na KONCI souboru (kruhový import oběma směry).
"""
from __future__ import annotations

import logging


class GroundingMixin:
    """Část třídy `OpenWebUIDirectHandler` — viz hlavička modulu."""

    def _build_grounding(self, user, name=None) -> str:
        """HANS_KODI_FILM_FACT_V2 (4. 10.) — obal: fakta o filmu z knihovny se
        přidají k JAKÉKOLI cestě. /tazatel 4. 10.: V1 seděla uprostřed
        řetězce a „jsi ji viděl? je to dobrý film?“ vzala cesta názoru,
        „kdo tam hraje?“ četba (herci úplně jiného filmu) — obě dřív."""
        g = self._build_grounding_v1(user, name)
        try:
            _txt = user[1] if isinstance(user, tuple) and len(user) == 2 else user
            from scripts import hans_thread as _thr_ff
            try:
                _ch_ff = _h.get_current_channel()
            except Exception:
                _ch_ff = None
            _ff = self._kodi_film_fact(str(_txt or ""),
                                       _thr_ff.recent_turns(self, name, _ch_ff))
        except Exception as _fe:
            logging.getLogger(__name__).debug("film fakt: %s", _fe)
            _ff = ""
        if not _ff:
            return g
        try:
            from scripts.hans_intent import pta_se_na_obsazeni
            _obs = pta_se_na_obsazeni(str(_txt or ""))
        except Exception:
            _obs = False
        _bez_opory = getattr(self, "_grounding_outcome", "") == "factual_nofacts"
        if not g or _obs or _bez_opory:
            # bez opory (A1 by abstinovala — i když vnitřní cesta vrátila jen
            # anti-konfab pokyn) nebo otázka na herce → knihovna sama
            self._vysledek_groundingu('grounded', 'film_kodi')
            return _ff
        return g + _ff

    def _build_grounding_v1(self, user, name=None) -> str:
        """G3B_GROUNDING_V1 — vrátí grounding blok pro faktický dotaz.

        Faktická zpráva → intent → kolekce → query() pod prahem →
        anti-konfab prompt + fakta. Volná zpráva / nic nenalezeno → ''.
        Defenzivní: cokoliv chybí/selže → '' (grounding se tiše přeskočí).
        """
        import types as _types_nt
        ctx = _types_nt.SimpleNamespace(name=name)
        self._tazatel_ted = ctx.name or ""   # HANS_ENTITY_NOT_ASKER_V1
        # user může být tuple (system,user) nebo string — vytáhni text
        ctx._text = user
        if isinstance(user, tuple) and len(user) == 2:
            ctx._text = user[1]
        if not ctx._text or not str(ctx._text).strip():
            return ''

        # HANS_THREAD_V1 — navazující věta si nese předmět z předchozí
        # repliky, aby ji detektory neposuzovaly izolovaně. Guard na shodu
        # s originálem: _build_grounding se volá i mimo hlavní chat cestu.
        try:
            _tc = getattr(self, '_thread_ctx', None)
            if _tc and str(_tc[0]) == str(ctx._text) and _tc[1] != _tc[0]:
                ctx._text = _tc[1]
        except Exception as _tiche:
            _h.log_once(  # HANS_NO_SILENT_CTX_V1
                logging.getLogger(__name__), "_build_grounding(ř. 645)",
                "_build_grounding: blok kontextu selhal (ř. 645): %s", _tiche)

        # HANS_SELFCONSISTENCY_A1_V1 — zaznamenej výsledek groundingu pro
        # volajícího (A1 short-circuit běží jen u 'factual_nofacts').
        self._vysledek_groundingu('skip', 'start')

        # HANS_A1_THREAD_TEXT_V1 (21.8.) — sem si F1 odloží ROZŘEŠENOU podobu
        # dotazu, aby se podle ní mohla rozhodnout A1 brzda (viz gate níž).
        # Nulovat je NUTNÉ: bez toho by zvětralá věta z minulého tahu
        # klasifikovala tah další.
        self._f1_query = None
        _r = self._gr_znas(ctx)
        if _r is not _h._POKRACUJ:
            return _r
        _r = self._gr_nedavne(ctx)
        if _r is not _h._POKRACUJ:
            return _r
        _r = self._gr_zdroj(ctx)
        if _r is not _h._POKRACUJ:
            return _r

        ctx._intent = getattr(self, 'intent', None)
        ctx._knowledge = getattr(self, 'knowledge', None)
        if ctx._intent is None or ctx._knowledge is None:
            return ''   # nezapojeno → tiše nic
        _r = self._gr_nazor(ctx)
        if _r is not _h._POKRACUJ:
            return _r
        _r = self._gr_rozhovor(ctx)
        if _r is not _h._POKRACUJ:
            return _r
        _r = self._gr_sliby(ctx)
        if _r is not _h._POKRACUJ:
            return _r
        _r = self._gr_film(ctx)
        if _r is not _h._POKRACUJ:
            return _r
        _r = self._gr_studium_proc(ctx)
        if _r is not _h._POKRACUJ:
            return _r
        _r = self._gr_knihovna(ctx)
        if _r is not _h._POKRACUJ:
            return _r
        _r = self._gr_obraz(ctx)
        if _r is not _h._POKRACUJ:
            return _r
        _r = self._gr_rag(ctx)
        if _r is not _h._POKRACUJ:
            return _r

    def _gr_znas(self, ctx):
        # HANS_KNOWLEDGE_CHECK_V1 (18.7.) — „znáš X?" / „co víš o X?" když X
        # NENÍ v paměti (deník/entities). Bez tohoto hans-czech halucinuje
        # „mám v paměti záznamy" i pro věci, o kterých nikdy neslyšel (doložený
        # Červený trpaslík chat 21:15). System prompt klauzule V2 nezakázala;
        # grounding blok (G4B_POSITION_V1) má silnější slovo — sedí těsně před
        # user query, přebíjí persona finetune.
        try:
            from scripts.hans_recall import (
                is_knowledge_check_query, knowledge_check_answer,
                reading_recall_answer, person_card)
            if is_knowledge_check_query(str(ctx._text)):
                _dbp_kc = (self.config.get("diary_db")
                           or (self.config.get("hans_idle", {}) or {}).get("diary_db")
                           or "data/hans_diary.db")
                # HANS_PERSON_CARD_KC_V1 (18.8.) — OSOBA MÁ PŘEDNOST PŘED ČTENÍM.
                # Agentní akce `report_person` řeší jen tvar „kdo je X?"; „co víš
                # o X?" se sem odbočí DŘÍV a agenta vůbec nepotká. Doloženo živě
                # 18.8.: „a co víš o Janě?" → Hans popsal vesnici Henčov
                # u Jihlavy, protože v paměti nic nenašel a spustil dohledání.
                # Fakt o paní domu přitom leží v `relationships`. Proto se sem
                # vkládá první: vlastní pozorování domácnosti je autoritativnější
                # než cokoli dohledaného, a rovnou to ubírá práci near-miss
                # pravopisu ([[instant-lookup-verify-loop]]).
                # Neosobní dotaz („co víš o hradech?") vrátí prázdno → beze změny.
                # HANS_PERSON_CARD_KC_FIX_V1 (19.8.) — OPRAVA MÉ VLASTNÍ REGRESE
                # z 18.8. Zpráva sem chodí s prefixem „<jméno> se ptá:", takže
                # `find_known_person` našel TAZATELE a `person_card` vracel jeho
                # kartu jako grounding NA COKOLI. Změřeno: na „a co ses o tom
                # divadle dozvěděl?" dostal model 448 zn životopisu TAZATELE
                # místo 1 307 zn vlastního zápisku o divadle — a tak si rok
                # vzniku vymyslel. Postihovalo to KAŽDÝ znalostní dotaz v chatu.
                # Dvě pojistky: (1) prefix odstranit, (2) kartu pustit jen
                # u dotazu, který se OPRAVDU ptá na osobu.
                try:
                    import re as _pcre
                    _q_nopfx = _pcre.sub(r"^\s*\S+\s+se\s+pt[áa]:\s*", "",
                                         str(ctx._text))
                    from scripts.hans_recall import asks_about_person as _aap2
                    # HANS_PRAVA_V1 — tazatel jde dál, karta podle oprávnění.
                    # Cizímu (a bez jména) se neposílá, ať se tahle cesta
                    # nezmění pro vypnutá pravidla: odmítnutí řeší jiná brána.
                    try:
                        from scripts.cz_names import is_known_person as _ikp_pc
                        _asker_pc = ctx.name if (ctx.name and _ikp_pc(ctx.name, self.config)) else ""
                    except Exception:
                        _asker_pc = ""
                    _pc = (person_card(_dbp_kc, _q_nopfx, self.config,
                                       asker=_asker_pc)
                           if _aap2(_q_nopfx, self.config) else "")
                except Exception:
                    _pc = ""
                if _pc:
                    self._vysledek_groundingu('grounded', 'pc_stav')
                    return _pc
                # HANS_FILM_BEFORE_READING_V1 (26. 9.) — „co víš o FILMU X?“
                # patří filmovému záznamu, ne četbě. Doloženo živě: „co víš
                # o filmu Duna?“ → `reading_recall` (článek o Duně: Části
                # druhé) → „V paměti to nemám“, ač Hans film viděl 3×.
                # Změřeno: 3 reálné takové věty, u žádné filmový záznam není →
                # film_knowledge vrátí None a jde se dál beze změny.
                import re as _re_f
                if _re_f.search(r"\bfilm\w*|\bseri[aá]l\w*", str(ctx._text), _re_f.I):
                    try:
                        from scripts.hans_recall import film_knowledge_answer as _fka
                        _fr0 = _fka(_dbp_kc, self._bez_tazatele(ctx._text),
                                    asker=ctx.name or "")
                    except Exception:
                        _fr0 = None
                    if _fr0:
                        self._vysledek_groundingu('grounded', 'film_recall')
                        return _fr0
                # HANS_READING_RECALL_V1 — nejdřív deterministicky dohledej, co
                # si o tom Hans SÁM přečetl (declension-safe, obchází flaky RAG
                # na tenkých souhrnech). Má přednost před „nemám záznam".
                _rr = reading_recall_answer(_dbp_kc, str(ctx._text))
                if _rr:
                    self._vysledek_groundingu('grounded', 'reading_recall')
                    return _rr
                _kc = knowledge_check_answer(_dbp_kc, str(ctx._text))
                if _kc:
                    self._vysledek_groundingu('grounded', 'reading_recall_tema')
                    return _kc
                # None = topic JE v paměti → nech normální recall/RAG cestu
        except Exception as _tiche:
            _h.log_once(  # HANS_NO_SILENT_CTX_V1
                logging.getLogger(__name__), "_build_grounding(ř. 709)",
                "_build_grounding: blok kontextu selhal (ř. 709): %s", _tiche)
        return _h._POKRACUJ

    def _gr_nedavne(self, ctx):
        # HANS_RECENT_ACTIVITY_V1 (18.7.) — „co jsi se dnes dozvěděl / co sis
        # zapsal / co jsi dnes dělal"? Deterministický recall Hansovy vlastní
        # aktivity za posledních N dní (default 1). Opravuje false-negative
        # anti-konfab „nemám záznam" (doloženo chat 20:44/20:45 — Hans DNES
        # studoval, četl, maloval; ale intent 'udalost' + RAG žádný match →
        # G3C brzda). Deterministické fakta z deníku obchází.
        try:
            from scripts.hans_recall import (
                is_recent_activity_query, recent_activity_answer)
            if is_recent_activity_query(str(ctx._text)):
                _dbp_ra = (self.config.get("diary_db")
                           or (self.config.get("hans_idle", {}) or {}).get("diary_db")
                           or "data/hans_diary.db")
                # HANS_RECENT_ACTIVITY_YESTERDAY_V1 — text nese ČASOVÉ OKNO
                _ra = recent_activity_answer(_dbp_ra, days=1, text=str(ctx._text))
                if _ra:
                    self._vysledek_groundingu('grounded', 'nedavna_aktivita')
                    return _ra
        except Exception as _tiche:
            _h.log_once(  # HANS_NO_SILENT_CTX_V1
                logging.getLogger(__name__), "_build_grounding(ř. 729)",
                "_build_grounding: blok kontextu selhal (ř. 729): %s", _tiche)
        return _h._POKRACUJ

    def _gr_zdroj(self, ctx):
        # HANS_SOURCE_QUERY_V1 — „odkud to víš / kde jsi to četl / máš zdroj"?
        # MUSÍ BÝT PRVNÍ (dřív než _intent/_knowledge gate) — dotaz na
        # provenienci NEpotřebuje intent/RAG infrastrukturu; přebije obecnou
        # anti-konfab klauzuli (V2). Diagnóza 17.7. 12:07: můj předchozí
        # umístění za _intent gate způsobilo, že se do check nedostalo (intent
        # může být None u meta-dotazů).
        try:
            from scripts.hans_recall import is_source_query, sources_reply
            _log_dbg = logging.getLogger(__name__)
            if is_source_query(str(ctx._text)):
                _dbp_s = (self.config.get("diary_db")
                          or (self.config.get("hans_idle", {}) or {}).get("diary_db")
                          or "data/hans_diary.db")
                self._vysledek_groundingu('grounded', 'zdroje')
                _log_dbg.info('HANS_SOURCE_QUERY_V1: match → sources_reply grounding')
                # HANS_SOURCE_REFERENT_SCOPE_V1 — mluvčí musí dojít až dolů,
                # jinak fallback sáhne po replice dané NĚKOMU JINÉMU.
                return sources_reply(_dbp_s, user_text=str(ctx._text), asker=ctx.name)
        except Exception as _sqe:
            logging.getLogger(__name__).warning(
                'HANS_SOURCE_QUERY_V1 check selhal: %s', _sqe)
        return _h._POKRACUJ

    def _gr_nazor(self, ctx):
        # HANS_OPINION_GROUNDING_G1_V1 — názorový/filosofický dotaz NENÍ
        # faktický: patří do imaginativního registru (postoje, ne RAG/A1).
        # Musí PŘED intent klasifikací — „co si myslíš o X?" intent chybně
        # řadí jako faktické (otázkový signál) → bez tohohle by filosofii
        # hrozil ANTIKONFAB_NOFACTS + A1 abstinence.
        try:
            from scripts.hans_opinion import is_opinion_query as _ioq
            if _ioq(str(ctx._text)):
                self._vysledek_groundingu('opinion', 'nazor')
                return ''
        except Exception as _tiche:
            _h.log_once(  # HANS_NO_SILENT_CTX_V1
                logging.getLogger(__name__), "_build_grounding(ř. 767)",
                "_build_grounding: blok kontextu selhal (ř. 767): %s", _tiche)
        return _h._POKRACUJ

    def _gr_rozhovor(self, ctx):
        # HANS_CHAT_RECALL_V2 — recall PŘEDCHOZÍHO rozhovoru („pamatuješ na X",
        # „mluvili jsme o…", „co jsi navrhl"). Sémantický RAG vágní recall často
        # nedohledá (uložené repliky ≠ znění dotazu) → deterministicky prohledej
        # skutečný human_chat. PŘEDNOST (real data), obchází RAG práh + šum.
        try:
            from scripts.hans_recall import is_recall_query, conversation_recall
            if is_recall_query(str(ctx._text)):
                _dbp_r = (self.config.get("diary_db")
                          or (self.config.get("hans_idle", {}) or {}).get("diary_db")
                          or "data/hans_diary.db")
                _rc = conversation_recall(_dbp_r, str(ctx._text), person=ctx.name)
                if _rc:
                    self._vysledek_groundingu('grounded', 'chat_recall')
                    _blk = "\n\n".join("[Dřívější rozhovor — %s]\n%s" % (kdy, note)
                                       for kdy, note in _rc)
                    return ("\n\nSKUTEČNÝ ZÁZNAM dřívějšího rozhovoru (odpověz JEN "
                            "z něj, nevymýšlej datum ani detaily; na co v záznamu "
                            "není, přiznej „to si nevybavuji“):\n" + _blk)
        except Exception as _tiche:
            _h.log_once(  # HANS_NO_SILENT_CTX_V1
                logging.getLogger(__name__), "_build_grounding(ř. 788)",
                "_build_grounding: blok kontextu selhal (ř. 788): %s", _tiche)
        return _h._POKRACUJ

    def _gr_sliby(self, ctx):
        # HANS_COMMITMENTS_V1 — „co jsi mi slíbil?" → deterministicky z uložených
        # SLIBŮ (ne hledání v textu); prázdno → honestní „nic", NE výmysl.
        try:
            from scripts.hans_commitments import commitments_answer as _commit_ans
            _dbp_c = (self.config.get("diary_db")
                      or (self.config.get("hans_idle", {}) or {}).get("diary_db")
                      or "data/hans_diary.db")
            _cr = _commit_ans(_dbp_c, str(ctx._text), person=ctx.name)
            if _cr:
                self._vysledek_groundingu('grounded', 'zavazky')
                return _cr
        except Exception as _tiche:
            _h.log_once(  # HANS_NO_SILENT_CTX_V1
                logging.getLogger(__name__), "_build_grounding(ř. 802)",
                "_build_grounding: blok kontextu selhal (ř. 802): %s", _tiche)
        return _h._POKRACUJ

    def _gr_film(self, ctx):
        # HANS_FILM_RECALL_V1 — dotaz na FILM podle názvu: dohledej Hansovy
        # VLASTNÍ deníkové záznamy (movie_opinion/kodi_playing) o tom filmu, ať
        # nezapře, co ví (doložený případ „Proud krve"). RAG kolekce hans_filmy
        # ani conversation_recall tyhle eventy nenajdou. PŘED intent/RAG.
        try:
            from scripts.hans_recall import film_knowledge_answer as _film_recall
            _dbp_f = (self.config.get("diary_db")
                      or (self.config.get("hans_idle", {}) or {}).get("diary_db")
                      or "data/hans_diary.db")
            # HANS_FILM_ASKER_PREFIX_V1 (23. 9.) — bez prefixu "<jmeno> se pta:",
            # jinak jmeno tazatele trefi titul (test persona `zkouska` → film
            # „Zkouska“: 5 ze 7 spusteni film_recall za 4 dny). Tentyz odrez
            # jako u entit (HANS_ENTITY_STRIP_ASKER_V1). Zmereno na 91
            # filmovych vetach × 3 jmenech: 84 rozdilu proti hole vete → 0.
            _fr = _film_recall(_dbp_f, self._bez_tazatele(ctx._text),
                               asker=ctx.name or "")  # HANS_FILM_OPINION_PRIVACY_V1
            if _fr:
                self._vysledek_groundingu('grounded', 'film_recall')
                return _fr
        except Exception as _tiche:
            _h.log_once(  # HANS_NO_SILENT_CTX_V1
                logging.getLogger(__name__), "_build_grounding(ř. 818)",
                "_build_grounding: blok kontextu selhal (ř. 818): %s", _tiche)
        return _h._POKRACUJ

    def _gr_studium_proc(self, ctx):
        # HANS_BOOK_RECOMMEND_GROUNDED_V1 (13. 9.) — zadost o doporuceni cetby
        # dostane SKUTECNOU knihovnu, ne fantazii.
        # ⚠️ MUSI STAT PRED VETVI INTENTU: „co bys mi doporucil precist?" se
        # klasifikuje jako NEfakticky dotaz O HANSOVI, takze blok `self_state`
        # (HANS_SELF_STATE_V1) vratil driv a knihovna se nikdy nedostala ke slovu.
        # Doloženo 13. 9. zive: prvni umisteni (vedle `_kodi_cast_fact`) NEZABRALO
        # — v logu `GROUNDING: self_state ← self_state`. [[verify-it-actually-flows]]
        try:   # HANS_STUDY_WHY_TOPIC_V1
            _pu = self._puvod_studia_fact(str(ctx._text))
            if _pu:
                self._vysledek_groundingu('grounded', 'studium_puvod')
                return _pu
        except Exception as _tiche:
            _h.log_once(
                logging.getLogger(__name__), "_build_grounding(studium_puvod)",
                "_build_grounding: blok původu studia selhal: %s", _tiche)
        return _h._POKRACUJ

    def _gr_knihovna(self, ctx):
        try:
            _kn = self._knihovna_fact(str(ctx._text), ctx.name)  # HANS_BOOK_RECOMMEND_FOLLOWUP_V1
            if _kn:
                self._vysledek_groundingu('grounded', 'knihovna')
                return _kn
        except Exception as _tiche:
            _h.log_once(
                logging.getLogger(__name__), "_build_grounding(knihovna)",
                "_build_grounding: blok knihovny selhal: %s", _tiche)
        return _h._POKRACUJ

    def _gr_obraz(self, ctx):
        try:   # HANS_ARTWORK_CONTENT_GROUNDED_V1
            _ob = self._obraz_fact(str(ctx._text))
            if _ob:
                self._vysledek_groundingu('grounded', 'obraz')
                return _ob
        except Exception as _tiche:
            _h.log_once(
                logging.getLogger(__name__), "_build_grounding(obraz)",
                "_build_grounding: blok obrazu selhal: %s", _tiche)
        return _h._POKRACUJ

    def _gr_rag(self, ctx):
        try:
            # 1) intent — je dotaz faktický?
            ctx.res = ctx._intent.classify(str(ctx._text))
            _r = self._grr_nefakticky(ctx)
            if _r is not _h._POKRACUJ:
                return _r
            self._grr_prepis(ctx)

            # HANS_KODI_CAST_FACT_V2 (21.8.) — KDO V TOM HRAJE: odpověz
            # z KNIHOVNY, ne z hlavy. Doloženo 20.8.: „kdo tam hraje?" →
            # Hans vyjmenoval tři herce, o kterých nemá záznam; přitom Kodi
            # u dílu „Turecké náušnice" vrací šestnáct jmen včetně Josefa
            # Kemra, kterého uhodl správně a zbytek domyslel.
            # ⚠️ MUSÍ STÁT AŽ TADY, ZA PŘEPISEM DOTAZU. V1 seděla nad ním
            # a dostávala holou větu: `_thread_ctx` u „kdo tam hraje?" předmět
            # nenajde (změřeno: ('kdo tam hraje?','kdo tam hraje?','')),
            # kdežto F1 ho doplní na „Kdo hraje v Tureckých náušnicích?".
            # Blok byl celou dobu správný, jen stál před tím, kdo mu měl
            # název dodat — táž chyba jako HANS_A1_THREAD_TEXT_V1, kterou
            # jsem týž den opravoval o pár řádků výš.
            try:
                _cf = self._kodi_cast_fact(str(ctx._q_for_retrieval))
                if _cf:
                    self._vysledek_groundingu('grounded', 'obsazeni_kodi')
                    return _cf
            except Exception as _tiche:
                _h.log_once(  # HANS_NO_SILENT_CTX_V1
                    logging.getLogger(__name__), "_build_grounding(obsazeni)",
                    "_build_grounding: blok obsazení selhal: %s", _tiche)

            # C1: entity store — deterministické resolvování ZNÁMÉ entity
            # (z Hansova čtení) PŘED RAG. Autoritativní fakt (definiční věta
            # ze zdroje) → zabíjí kolizi jmen i konfabulaci významu.
            # HANS_PERSON_FACT_V1 — člen domácnosti má PŘEDNOST před obecnou
            # entitou i před RAG: je to tvrdý záznam, ne nález z četby.
            # HANS_SELF_STATE_AWAKE_V2 — vlastní režim má přednost úplně první:
            # je to tvrdý běhový fakt, ne nález z paměti.
            ctx._ent_fact = (self._self_runtime_fact(str(ctx._text))
                         or self._person_fact(str(ctx._text))
                         or self._person_fact(ctx._q_for_retrieval)
                         or self._entity_fact(ctx._q_for_retrieval)
                         or self._capability_fact(str(ctx._text)))

            # 2) vyber kolekce dle třídy (G3B_MULTICOLLECTION_V1 — list)
            ctx.collections = self._GROUNDING_COLLECTION.get(ctx.res.intent)
            if not ctx.collections:
                # C1 / HANS_PERSON_FACT_V1: i bez RAG kolekce máme-li tvrdý
                # fakt (entita nebo osoba), vrať ho
                if ctx._ent_fact:
                    self._vysledek_groundingu('grounded', 'karta_osoby')
                    return '\n\n' + _h.ANTIKONFAB + '\n\n' + ctx._ent_fact
                return ''
            _r = self._grr_hledani(ctx)
            if _r is not _h._POKRACUJ:
                return _r
            _r = self._grr_nic_pod_prahem(ctx)
            if _r is not _h._POKRACUJ:
                return _r

            # 5) seřaď VŠECHNY chunky napříč kolekcemi dle distance,
            #    vezmi nejlepší K (mix kolekcí). distance = společné
            #    měřítko (stejný embedding bge-m3) → férové porovnání.
            ctx.all_chunks.sort(
                key=lambda c: (c.get('distance') is None,
                               c.get('distance') if c.get('distance')
                               is not None else 9e9))
            top = ctx.all_chunks[:self._GROUNDING_K]
            _best_dist = top[0].get('distance') if top else None

            # HANS_RAGFIRST_STRICT_V1 (#2) — přísný TOP práh.
            # Když nejlepší chunk je NAD strict_max (borderline zóna
            # 0.70-0.75), RAG je slabý = neber ho jako grounding.
            # Autoritativní zdroje (entity/karta) zůstávají — mají vlastní
            # ověření (jméno v textu / definiční věta z Hansova čtení).
            _strict_max = float(
                (self.config.get('grounding', {}) or {})
                .get('strict_max_distance', self._GROUNDING_STRICT_MAX))
            _rag_weak = (_best_dist is None) or (_best_dist > _strict_max)
            if _rag_weak:
                top = []  # zahoď slabé chunky
                _facts_from_rag = ''
                logging.getLogger(__name__).info(
                    '#2: RAG slabý (best=%.3f > strict=%.3f) → chunky zahozeny',
                    _best_dist if _best_dist is not None else -1, _strict_max)
            else:
                # HANS_PROVENANCE_V1 — každý chunk dostane značku původu:
                # per-chunk provenance z metadata (přesné), fallback kolekce.
                # hans_denik → 'nejisté' (míchá prožitky se sny/úvahami) →
                # Hans to netvrdí jako jistou vzpomínku.
                try:
                    from scripts import hans_provenance as _prov
                    _prov_on = (self.config.get('provenance', {}) or {}).get(
                        'enabled', True)
                except Exception:
                    _prov_on = False
                    _prov = None
                _rag_lines = []
                for c in top:
                    _t = c.get('text')
                    if not _t:
                        continue
                    if _prov_on and _prov is not None:
                        _cls = c.get('provenance') or \
                            _prov.provenance_of_collection(c.get('collection'))
                        _rag_lines.append(f"{_prov.marker(_cls)} {_t}")
                    else:
                        _rag_lines.append(_t)
                _facts_from_rag = '\n\n'.join(_rag_lines)

            # G5A_IDENTITY_GROUNDING_V3 — vztahová karta z DB jako
            # PRIORITNÍ pravda. Adresujeme podle jména (NE embedding),
            # tvrdá data (role+rodina, BEZ characterization=starý tón).
            # F1 pomáhá: rewriter rozřeší 'kdo je on' → jméno v textu.
            _card_fact = self._build_card_fact(ctx._q_for_retrieval)
            if _card_fact:
                logging.getLogger(__name__).info(
                    'G5A: karta vstříknuta z DB → priorita')

            # Skládání priorit: entita (autoritativní) > karta > RAG chunky.
            _parts = []
            if ctx._ent_fact:
                _parts.append(ctx._ent_fact)
            if _card_fact:
                _parts.append(_card_fact)
            if _facts_from_rag:
                _parts.append(_facts_from_rag)
            facts = '\n\n'.join(_parts)

            if not facts.strip():
                # HANS_FTS_USES_REWRITE_V1 (21.8.) — hledej v zápiscích podle
                # OPRAVENÉ věty, ne syrové. Holé „kdo tu knihu napsal?" nenese
                # název a fulltext na něj trefí cizí knihu (změřeno: Murakami);
                # F1 ho doplní z vlákna. Potřetí týž vzorec za den.
                _kb = self._knowledge_fts_grounding(
                    str(ctx._q_for_retrieval or ctx._text))
                if _kb:
                    self._vysledek_groundingu('grounded', 'zapisky_fallback')
                    return _kb
                # RAG slabé A žádný autoritativní zdroj = jako by prázdné.
                logging.getLogger(__name__).info(
                    '#2: bez faktů (RAG slabý, žádná entita/karta) → factual_nofacts')
                self._vysledek_groundingu('factual_nofacts', 'bez_faktu')
                return '\n\n' + _h.ANTIKONFAB_NOFACTS

            _cols_used = sorted(set(c.get('collection', '?') for c in top))
            logging.getLogger(__name__).info(
                'G3B: grounding [%s] best=%.3f, %d chunků z %s, ent=%d card=%d → kontext',
                ctx.res.intent, _best_dist if _best_dist is not None else -1,
                len(top), '+'.join(_cols_used) if _cols_used else '-',
                1 if ctx._ent_fact else 0, 1 if _card_fact else 0)
            self._vysledek_groundingu('grounded', 'rag')
            return '\n\n' + _h.ANTIKONFAB + '\n\n' + facts

        except Exception as _ge:
            logging.getLogger(__name__).warning(
                'G3B: grounding selhalo (%s) — odpovídám bez fakt', _ge)
            return ''
        return _h._POKRACUJ

    def _grr_nefakticky(self, ctx):
        if not ctx.res.is_factual:
            # HANS_SELF_STATE_V1 (5.8.) — volná konverzace ještě NEZNAMENÁ
            # „bez faktů". Když se ptá NA HANSE („jak se máš?", „co jsi
            # dnes dělal?"), dej mu jeho VLASTNÍ dnešek z deníku. Bez toho
            # model plodil vatu („Službu plním, a to je pro mne
            # dostatečné") nebo komoleniny („historii zeleného, pana").
            # Nálada + její důvod už v promptu jsou (mood_ctx), tohle
            # dodává CO dnes reálně dělal. Detektor sdílený s agentem.
            try:
                from scripts.hans_intent import is_about_self
                if is_about_self(str(ctx._text), self.config):
                    from scripts.hans_recall import self_state_facts
                    _dbp_ss = (self.config.get("diary_db")
                               or (self.config.get("hans_idle", {}) or {}).get("diary_db")
                               or "data/hans_diary.db")
                    _mo = _mr = ""
                    try:
                        _hi_m = getattr(self, '_hans_idle', None)
                        _mobj = getattr(_hi_m, '_mood', None) if _hi_m else None
                        if _mobj is not None:
                            _mo = getattr(_mobj, 'mood', '') or ''
                            _mr = getattr(getattr(_mobj, '_state', None),
                                          'shift_reason', '') or ''
                    except Exception as _tiche:
                        _h.log_once(  # HANS_NO_SILENT_CTX_V1
                            logging.getLogger(__name__), "_build_grounding(ř. 847)",
                            "_build_grounding: blok kontextu selhal (ř. 847): %s", _tiche)
                    # HANS_SELF_STATE_AWAKE_V1 — dolož skutečný provozní
                    # stav (spánek/kamera/hlídání), ať si ho model nedomýšlí.
                    _rt_state = {}
                    try:
                        # ⚠️ Handler `_routine` NEMÁ — drží ho `hans_idle`
                        # (týž vzorec jako TIME_AWARENESS_V1 na ř. 1342).
                        # Přímé `getattr(self, "_routine")` by tiše vracelo
                        # None a stav by se do bloku nikdy nedostal.
                        _hi_rt = getattr(self, "_hans_idle", None)
                        _rt = getattr(_hi_rt, "_routine", None) if _hi_rt else None
                        if _rt is not None:
                            _rt_state["sleeping"] = bool(
                                getattr(_rt, "_sleeping", False))
                        import json as _js_g
                        import os as _os_g
                        _gp = "data/.hans_guard"
                        if _os_g.path.exists(_gp):
                            with open(_gp, encoding="utf-8") as _gf:
                                _rt_state["guard"] = bool(
                                    (_js_g.load(_gf) or {}).get("armed"))
                        else:
                            _rt_state["guard"] = False
                    except Exception as _rse:
                        logging.getLogger(__name__).debug(
                            'self_state runtime: %s', _rse)
                    # HANS_SELF_STATE_ASKER_VISIBLE_V1 (15. 9.) — "vidite me na
                    # kamere?" od cloveka v chatu: Hans rekl "vidim vas", ackoli
                    # o tah driv "nikoho tu nevidim". Blok o sobe nerikal, kdo
                    # pred kamerou stoji. Jen pri otazce na videni.
                    try:
                        if self._VIDIS_ME_PAT.search(str(ctx._text)):
                            _hi_pr = getattr(self, "_hans_idle", None)
                            _pritomni = [str(x).strip().lower() for x in
                                         (getattr(_hi_pr, "_present_names", None) or [])]
                            _rt_state["asker_visible"] = bool(
                                ctx.name and str(ctx.name).strip().lower() in _pritomni)
                    except Exception:
                        pass
                    # HANS_MOOD_CAMERA_STRANGER_V1 (24. 9.) — blok o sobe
                    # bral duvod nalady BEZ filtru, takze obchazel
                    # HANS_MOOD_REASON_PRIVACY_V1 (doloženo: cizimu „neznama
                    # tvar“). Tataz brana jako v prompt addition; cizimu
                    # ani zapnute hlidani (= dum je prazdny).
                    try:
                        from scripts.cz_names import is_known_person as _ikp_ss
                        if not (ctx.name and _ikp_ss(ctx.name, self.config)):
                            _rt_state.pop("guard", None)
                            if _mr and not _mobj._duvod_do_promptu(True):
                                _mr = ""
                                _mo = "content"   # HANS_MOOD_HIDDEN_NEUTRAL_V1
                    except Exception:
                        _mr = ""
                        _rt_state.pop("guard", None)
                    _ss = self_state_facts(_dbp_ss, mood=_mo, mood_reason=_mr,
                                           runtime=_rt_state or None)
                    if _ss:
                        logging.getLogger(__name__).info(
                            'HANS_SELF_STATE_V1 → blok o sobě (%d zn)', len(_ss))
                        self._vysledek_groundingu('self_state', 'self_state')
                        return '\n\n' + _ss
            except Exception as _sse:
                logging.getLogger(__name__).debug('self_state: %s', _sse)
            self._vysledek_groundingu('nonfactual', 'volny_hovor')
            return ''   # volná konverzace → osobnost, žádný retrieval
        return _h._POKRACUJ

    def _grr_prepis(self, ctx):
        # ── HANS_QUERY_REWRITER_F1_V1 ────────────────────────────────
        # Rewriter „člověk→počítač" na FAKTICKÉ CESTĚ: rozřeš odkazy,
        # oprav překlepy, strhni výplňky → vyčištěný explicitní dotaz
        # pro retrieval. Persona (chat generace) DÁL slyší raw text
        # výše ve volajícím — bytost, ne asistent. Deferral-safe: None
        # → drž se originálu (žádná změna chování).
        ctx._q_for_retrieval = str(ctx._text)
        try:
            from scripts.hans_rewriter import (
                rewrite_for_retrieval as _f1_rewrite,
                is_enabled as _f1_on)
            if _f1_on(self.config):
                _hist = []
                if ctx.name:
                    try:
                        # HANS_CHAT_CHANNEL_AWARE_V1 — měkký filtr
                        ctx._ch = _h.get_current_channel()
                        _hist = self.conv_store.get_history(ctx.name, channel=ctx._ch) or []
                    except Exception:
                        _hist = []
                _rw = _f1_rewrite(self.config, str(ctx._text),
                                  history=_hist, name=ctx.name)
                if _rw and _rw.strip() and _rw.strip() != str(ctx._text).strip():
                    # HANS_F1_NOT_ABOUT_ASKER_V1 (16. 9.) — prepis, ktery
                    # prehodil podmet z Hanse na TAZATELE, je pro retrieval
                    # k nicemu: FTS hleda, co vi tazatel, misto co delal
                    # Hans → prazdny podklad → falesne zapreni (15. 9.,
                    # „co si uz malovai?“ → „Co <tazatel> vi o obrazech?“).
                    if self._f1_o_tazateli(_rw, ctx.name):
                        logging.getLogger(__name__).info(
                            'HANS_F1_NOT_ABOUT_ASKER_V1: prepis %r prehodil '
                            'podmet na tazatele — drzim original', _rw[:60])
                    else:
                        logging.getLogger(__name__).info(
                            'F1: rewrite %r -> %r',
                            str(ctx._text)[:60], _rw[:60])
                        ctx._q_for_retrieval = _rw.strip()
                        # HANS_A1_THREAD_TEXT_V1 — schovej pro A1 gate
                        self._f1_query = ctx._q_for_retrieval
        except Exception as _f1e:
            logging.getLogger(__name__).debug(
                'F1: rewriter selhal (%s) — použit originál', _f1e)

    def _grr_hledani(self, ctx):
        # 3) query VŠECHNY kolekce PARALELNĚ (fakta roztroušená).
        #    ThreadPool — query je síťový hop, vlákna se překryjí.
        #    Celý sken v jednom timeoutu (ne timeout na kolekci).
        import concurrent.futures as _cf
        ctx.all_chunks = []
        _skipped_chatlogs = []   # HANS_CHATLOG_NOT_FACT_V1
        _skipped_own = []        # HANS_OWN_WORK_NOT_FACT_V1
        _skipped_mimo = []       # HANS_GROUNDING_ANCHOR_V1
        # HANS_GROUNDING_ANCHOR_V1 (22.8.) — OPORA MUSÍ MLUVIT O TOM,
        # NA CO SE PTÁM. Změřeno na 919 skutečných dotazech z deníku
        # proti živému RAGu s produkčními parametry: u dotazů, které
        # nesou vlastní jméno, obsahoval podklad to jméno **0×** —
        # ani v jednom ze tří chunků, které jdou do promptu:
        #   osobnost: 144 groundingů, 22 s vlastním jménem, 0 o něm
        #   film:      65 groundingů, 12 s vlastním jménem, 0 o něm
        #   „znáš Xqzybwrt Flurbex?" (smyšlené jméno) ← 0.622 „Kdo je Hans"
        #   „kdo byl Richard Sorge?"                  ← 0.681 reflexe Design
        #   „Co víš o Icon of the Seas?"              ← 0.694 Pád do Tichého oceánu
        # Práh to neuhlídá — všechno je POD strict 0.70; u bge-m3 se
        # relevantní pásmo se šumem překrývá (varování v hans_knowledge),
        # takže rozhodnout musí TÉMA, ne vzdálenost. Na takové opoře
        # pak model postaví celou smyšlenou biografii (Scott Eastwood).
        # Kotva se počítá z PŘEPSANÉHO dotazu (F1 doplní jméno z vlákna).
        # Prázdno po filtru = dnešní větev „RAG nic nenašel" → vlastní
        # zápisky → entita → přiznání. Ověřeno, že tudy přijde ta SPRÁVNÁ
        # opora: „co víš o filmu Avatar: The Way of Water?" → zápisek
        # o Avataru; „Co víš o Icon of the Seas?" → 3× zápisek o té lodi;
        # vztahové karty jdou mimo RAG (`_build_card_fact`), takže
        # dotaz na člena domácnosti („a co víš o Janě?") zůstává
        # nedotčený.
        try:
            from scripts.hans_convindex import (
                kotvy_ve_vete as _kv_fn, nese_kotvu as _nk_fn)
            _kotvy_dotazu = [_w for _i, _w in
                             _kv_fn(str(ctx._q_for_retrieval or ctx._text))]
        except Exception as _kve:
            logging.getLogger(__name__).debug(
                'HANS_GROUNDING_ANCHOR_V1: kotvy nedostupné: %s', _kve)
            _kotvy_dotazu, _nk_fn = [], None
        try:
            with _cf.ThreadPoolExecutor(
                    max_workers=len(ctx.collections)) as _ex:
                _futs = {
                    _ex.submit(ctx._knowledge.query, _c,
                               ctx._q_for_retrieval,
                               self._GROUNDING_K,
                               self._GROUNDING_MAX_DISTANCE): _c
                    for _c in ctx.collections
                }
                _done, _pending = _cf.wait(
                    _futs, timeout=self._GROUNDING_TIMEOUT_S)
                for _fut in _done:
                    try:
                        _b = _fut.result()
                        if _b and _b.found:
                            for ctx._ch in _b.chunks:
                                ctx._ch = dict(ctx._ch)
                                ctx._ch['collection'] = _futs[_fut]
                                # HANS_CHATLOG_NOT_FACT_V1 (19.8.) — CO JSEM
                                # ŘEKL NENÍ CO VÍM. Chatové výměny se ukládají
                                # do `hans_pripady` (HANS_CHAT_RECALL_V1) a
                                # faktická cesta je pak četla jako důkaz —
                                # tedy Hansův vlastní výrok se mu vracel jako
                                # znalost. Doloženo 19.8.: fabulovaný rok
                                # vzniku divadla se uložil 7× a vracel se.
                                # ⚠️ Pro `conversation_recall` zůstávají —
                                # tam JSOU na místě („o čem jsme mluvili").
                                # HANS_CHATLOG_NOT_FACT_V2 (22.8.) — `self.`
                                # ⚠️ `_CHATLOG_RE` je ATRIBUT TŘÍDY; holé
                                # jméno uvnitř metody je NameError, takže
                                # filtr z 19.8. NIKDY neběžel. A protože ho
                                # zdejší `except` spolkne, přišla o chunky
                                # celá kolekce → RAG „nic nenašel" → padalo
                                # se na FTS zápisky. Tudy přišel 21.8. do
                                # podkladu o hradu Kost Pátý element.
                                _txt = str(ctx._ch.get('text') or '')
                                if self._CHATLOG_RE.search(_txt[:200]):  # V3: okno 120→200,
                                    # sekce „## Rozhovor s …" leží
                                    # až za titulkem a datem
                                    _skipped_chatlogs.append(1)
                                    continue
                                # HANS_OWN_WORK_NOT_FACT_V1 (22.8.) —
                                # vlastní tvorba není doklad o světě
                                # (rozbor u predikátu na začátku modulu).
                                if _h.je_vlastni_tvorba(_txt):
                                    _skipped_own.append(1)
                                    continue
                                # HANS_GROUNDING_ANCHOR_V1 — chunk, který
                                # o předmětu dotazu nemluví, není opora.
                                if (_kotvy_dotazu and _nk_fn is not None
                                        and not _nk_fn(_txt,
                                                       _kotvy_dotazu)):
                                    _skipped_mimo.append(1)
                                    continue
                                ctx.all_chunks.append(ctx._ch)
                    except Exception as _tiche:
                        _h.log_once(  # HANS_NO_SILENT_CTX_V1
                            logging.getLogger(__name__), "_build_grounding(ř. 978)",
                            "_build_grounding: blok kontextu selhal (ř. 978): %s", _tiche)
                if _pending:
                    logging.getLogger(__name__).info(
                        'G3B: %d/%d kolekcí nestihlo timeout %ss',
                        len(_pending), len(ctx.collections),
                        self._GROUNDING_TIMEOUT_S)
        except Exception as _qe:
            logging.getLogger(__name__).warning(
                'G3B: multi-query selhalo: %s', _qe)
            return ''

        if _skipped_chatlogs:
            logging.getLogger(__name__).info(
                'HANS_CHATLOG_NOT_FACT_V1: %d kusů z chatu vyřazeno '
                'z faktického groundingu', len(_skipped_chatlogs))
        if _skipped_own:
            logging.getLogger(__name__).info(
                'HANS_OWN_WORK_NOT_FACT_V1: %d kusů vlastní tvorby '
                'vyřazeno z faktického groundingu', len(_skipped_own))
        if _skipped_mimo:
            logging.getLogger(__name__).info(
                'HANS_GROUNDING_ANCHOR_V1: %d kusů mimo téma (%s) '
                'vyřazeno z faktického groundingu', len(_skipped_mimo),
                ', '.join(_kotvy_dotazu[:3]))
        return _h._POKRACUJ

    def _grr_nic_pod_prahem(self, ctx):
        # 4) nic relevantního pod prahem → G3C: vrať aspoň anti-konfab
        #    (bez faktů). Faktický dotaz bez záznamů → Hans NESMÍ
        #    konfabulovat. Web ověření přijde post-hoc (G.5).
        if not ctx.all_chunks:
            # HANS_NOTES_BEFORE_ENTITY_V1 (21.8.) — VLASTNÍ ZÁPISKY MAJÍ
            # PŘEDNOST PŘED ENTITOU. Doloženo 20.8.: na dotaz o svatyni
            # u Nymburka (Hans o ní ráno četl a zapsal si ji) rozhodla
            # entitní větev a vrátila „ověřený fakt“ o SVATBĚ — entita se
            # trefila jen 4znakovým prefixem „svat“. Správný zápisek byl
            # přitom v FTS na prvním místě, ale FTS se volalo až POD tímhle
            # returnem, takže se k němu dotaz nikdy nedostal.
            # Pořadí je teď: co jsem sám četl a zapsal > slovníková glosa.
            # Změřeno: kde entita rozhoduje správně (Sorge, Jiří z Poděbrad,
            # Gotika), míří zápisky na tentýž předmět → žádná ztráta C1;
            # kde zápisky nejsou (Secese), rozhodne dál entita.
            # HANS_FTS_USES_REWRITE_V1 (21.8.) — hledej v zápiscích podle
            # OPRAVENÉ věty, ne syrové. Holé „kdo tu knihu napsal?" nenese
            # název a fulltext na něj trefí cizí knihu (změřeno: Murakami);
            # F1 ho doplní z vlákna. Potřetí týž vzorec za den.
            _kb = self._knowledge_fts_grounding(
                str(ctx._q_for_retrieval or ctx._text))
            if _kb:
                # HANS_ENTITY_FACTS_ALSO_WITH_NOTES_V1 (26.8.) — zápisky
                # mají přednost, ale STRUKTUROVANÁ FAKTA si s nimi
                # NEKONKURUJÍ: je to jeden krátký ověřený řádek z Wikidat,
                # ne konkurenční próza. Doloženo: na „v jakém slohu je
                # Cardiffský hrad" se entita RESOLVOVALA (ev=21), ale
                # vyhrály zápisky → fakt `sloh = novogotika` se zahodil
                # a Hans napsal „gotickou stavbou". Kost fungovala jen
                # proto, že žádné zápisky neměla.
                _fl = self._entity_facts_line(ctx._q_for_retrieval)
                if _fl:
                    _kb = _kb + '\n' + _fl
                self._vysledek_groundingu('grounded', 'zapisky_pred_entitou')
                return _kb
            # C1: RAG prázdné, ale entita ve store → autoritativní fakt
            # (Sorge není v RAG, ale Hans o něm četl → deterministický fakt).
            if ctx._ent_fact:
                logging.getLogger(__name__).info(
                    'C1: RAG prázdné, entita ze store → grounded pro %r',
                    str(ctx._text)[:40])
                # nálepka byla `chatlog_neni_fakt` — s filtrem chatlogů to
                # nemá nic společného a 20.8. to svedlo diagnózu na RAG.
                self._vysledek_groundingu('grounded', 'entita_c1')
                return '\n\n' + _h.ANTIKONFAB + '\n\n' + ctx._ent_fact
            # HANS_KNOWLEDGE_FTS_V1 — tudy vede REÁLNÁ cesta k abstinenci
            # (ověřeno v logu 6.8.: „žádná shoda pod prahem → G3C").
            # Původní patch mířil jen na druhé místo níž a NIC neopravil.
            # HANS_FTS_USES_REWRITE_V1 (21.8.) — hledej v zápiscích podle
            # OPRAVENÉ věty, ne syrové. Holé „kdo tu knihu napsal?" nenese
            # název a fulltext na něj trefí cizí knihu (změřeno: Murakami);
            # F1 ho doplní z vlákna. Potřetí týž vzorec za den.
            _kb = self._knowledge_fts_grounding(
                str(ctx._q_for_retrieval or ctx._text))
            if _kb:
                self._vysledek_groundingu('grounded', 'zapisky_fts')
                return _kb
            # HANS_REFLECTIVE_ASK_V2 (3.9.) — ÚVAHOVÁ otázka se sem nesmí
            # propadnout. `ANTIKONFAB_NOFACTS` říká modelu „nemáš fakta,
            # přiznej to", jenže dotaz na vlastní názor žádná fakta
            # nepotřebuje — odpovídá se z osobnosti. Doloženo testem 3.9.:
            # „co je podle vás na dokumentování světa to nejtěžší?" →
            # „K tomuhle nemám spolehlivý záznam a nerad bych si domýšlel."
            # V1 (30.8.) hlídal jen větev `_tenky`; sem, na
            # `zapisky_fts_prazdno`, nedosáhl — log to ukázal hned
            # (`GROUNDING: factual_nofacts ← zapisky_fts_prazdno`).
            # Týž predikát, žádný nový — a je ÚZKÝ: 0 shod z 1327 reálných
            # uživatelských replik, takže anti-konfabulaci nerozvolňuje.
            try:
                from scripts.hans_intent import is_reflective_ask as _ira2
                if _ira2(str(ctx._text)):
                    logging.getLogger(__name__).info(
                        'HANS_REFLECTIVE_ASK_V2: %r je úvahová otázka → '
                        'osobnost místo abstinence', str(ctx._text)[:50])
                    self._vysledek_groundingu('nonfactual', 'uvahova_otazka')
                    return ''
            except Exception:
                pass
            logging.getLogger(__name__).info(
                'G3B: žádná shoda pod prahem pro [%s] %r → anti-konfab bez fakt (G3C)',
                ctx.res.intent, str(ctx._text)[:40])
            self._vysledek_groundingu('factual_nofacts', 'zapisky_fts_prazdno')
            return '\n\n' + _h.ANTIKONFAB_NOFACTS
        return _h._POKRACUJ

# ROZDELENI_METOD_V1 — až na konci, viz hlavička
from scripts import openwebui_direct_handler as _h  # noqa: E402
