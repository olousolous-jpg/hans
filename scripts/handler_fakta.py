"""Metody třídy `OpenWebUIDirectHandler` přesunuté z `scripts/openwebui_direct_handler.py` (ROZDELENI_METOD_V1).

Text metod je beze změny; jména původního modulu se čtou přes `_h.` až při
volání. `OpenWebUIDirectHandler` má třídu `FaktaMixin` mezi předky, takže volání přes `self` platí dál.
Import původního modulu je na KONCI souboru (kruhový import oběma směry).
"""
from __future__ import annotations

import logging
import re


class FaktaMixin:
    """Část třídy `OpenWebUIDirectHandler` — viz hlavička modulu."""

    def _favorite_game(self, name: str):
        """HANS_GAME_LAUNCH_ATTRIB_V1 — nejčastěji spouštěná hra osoby (z deníku
        game_launched, posl. 90 dní). None když žádná."""
        if not name:
            return None
        try:
            import sqlite3 as _sq
            import time as _t
            db = (self.config.get("hans_idle", {}) or {}).get(
                "diary_db", "data/hans_diary.db")
            conn = _sq.connect("file:%s?mode=ro" % db, uri=True, timeout=3)
            row = conn.execute(
                "SELECT COALESCE(NULLIF(data,''),note), COUNT(*) c FROM diary "
                "WHERE event_type='game_launched' AND title=? AND ts>? "
                "GROUP BY 1 ORDER BY c DESC LIMIT 1",
                (name, _t.time() - 90 * 86400)).fetchone()
            conn.close()
            return row[0] if row and row[0] else None
        except Exception:
            return None

    def _build_card_fact(self, text: str) -> str:
        """Najdi v dotazu známou osobu → vrať tvrdý fakt z relationships DB.
        Adresuje kartu podle jména (NE embedding). Bez characterization
        (ta nese starý tón). Prázdné, když nikdo nebo modul chybí."""
        _rels = getattr(self, '_rels', None)
        if _rels is None or not text:
            return ''
        import re as _re_g5a
        _low = text.lower()
        # tokenizuj dotaz na slova (ať skloňovaný tvar matchne jako CELÉ slovo,
        # ne podřetězec — vyhne se falešným shodám)
        _words = set(_re_g5a.findall(r'[a-zěščřžýáíéúůďťňó]+', _low))
        _forms_map = (self.config.get("person_name_forms", {}) or {})  # PORTABILITY
        for _pid, _forms in _forms_map.items():
            if _words & set(_forms):
                try:
                    _c = _rels.get(_pid)
                except Exception:
                    _c = None
                if not _c:
                    continue
                # slož tvrdý fakt: role + rodina (z dict family_links)
                _parts = [f"{_c.display_name} je {_c.role}"]
                _fl = _c.family_links or {}
                _sp = _fl.get('spouse')
                _ch = _fl.get('children') or []
                _par = _fl.get('parents') or []
                if _sp:
                    _spc = _rels.get(_sp)
                    _parts.append(f"manžel(ka): {_spc.display_name if _spc else _sp}")
                if _ch:
                    _chn = []
                    for _k in _ch:
                        _kc = _rels.get(_k)
                        _chn.append(_kc.display_name if _kc else _k)
                    _parts.append("děti: " + ", ".join(_chn))
                if _par:
                    _pn = []
                    for _k in _par:
                        _kc = _rels.get(_k)
                        _pn.append(_kc.display_name if _kc else _k)
                    _parts.append("rodiče: " + ", ".join(_pn))
                return "Fakta o osobě " + _c.display_name + ": " + ", ".join(_parts) + "."
        return ''

    def _knowledge_fts_grounding(self, text: str) -> str:
        """HANS_KNOWLEDGE_FTS_V1 (6.8.) — poslední šance před abstinencí.

        Prohledá Hansovy VLASTNÍ zápisky (`study_note`, `study_mastery`,
        `reading_takeaway`, `web_read`) lexikálním FTS. Sémantický RAG míjí,
        když se dotaz a zápisek liší SLOVY — doloženo 6.8.: „řekni mi, jak se
        vyvíjely zbrojnice" → „nemám spolehlivý záznam", ačkoli `study_note`
        s přesně tím titulem existuje.

        Vrací GROUNDING (model odpoví ze zápisků), ne hotovou větu — obsah je
        Hansův vlastní, jen se k němu neuměl dostat. AND-only vyhledávání
        v `hans_convindex` drží riziko falešného nálezu nízko.

        ⚠️ Volá se ze DVOU míst: G3C (žádná shoda pod prahem) i #2 (RAG slabý).
        První verze patche mířila jen na to druhé a v provozu se NIKDY
        nespustila — reálná cesta vede přes G3C (ověřeno v logu, ne z kódu).
        """
        try:
            from scripts.hans_convindex import search as _kfts
            hits = _kfts(str(text), limit=3, kind='knowledge')
            if not hits:
                return ''
            # HANS_KODI_GLOSS_LABEL_V1 (13. 9.) — GLOSA K PORADU NENI CETBA.
            # `web_read` ma DVA puvody: Hansovo vlastni cteni a clanek, ktery
            # si precetl, protoze v televizi bezel nejaky porad
            # (MOVIE_GROUNDING_V1). Oboji sem doteka pod tymz labelem
            # „Z mych zapisku“, takze model druhy druh prodava za vlastni
            # cetbu. Dolozeno 13. 9.: „preceetl si take F. L. Vek od Aloise
            # Jiraska“ — pritom to byla glosa k poradu z 11:08, knihu necetl.
            # Zmereno: kodi glos je 1613 z 3078 vsech `web_read` (52 %).
            # ⛔ NEFILTRUJI se pryc (to dela `HANS_READING_KODI_SPLIT_V1` ve
            # VYPISU cetby, kam nepatri) — tady je ta znalost legitimni
            # a Hans na ni ma umet ukazat zdroj. Meni se JEN popisek.
            try:
                from scripts.hans_recall import _kodi_tituly as _kt
                import sqlite3 as _s3
                _c = _s3.connect(self.diary_db_path if hasattr(self, 'diary_db_path')
                                 else 'data/hans_diary.db')
                _kodi = _kt(_c); _c.close()
            except Exception:
                _kodi = set()
            # HANS_BOOK_PROGRESS_LABEL_V1 (23. 9.) — u kapitoly knihy rekni,
            # ODKDY ji Hans cte a jestli POPRVE. Doloženo testem 23. 9.:
            # „tu knihu od le guin ctes poprve?" → ze tri kapitol v podkladu
            # model vyvodil „cetl jsem ji jiz drive" — cte ji od 17. 9.
            # od kap. 1. Zmereno: 16 knih v deniku, zadna kapitola dvakrat.
            _prubeh = {}
            try:
                import sqlite3 as _s3b, datetime as _dtb
                _cb = _s3b.connect(self.diary_db_path if hasattr(self, 'diary_db_path')
                                   else 'data/hans_diary.db')
                for _z in {str(_t).split(' \u2014 kap.')[0] for _ts, _s, _p, _t, _x in hits
                           if _s == 'book_read' and ' \u2014 kap.' in str(_t or '')}:
                    _r = _cb.execute(
                        "SELECT MIN(ts), MAX(ts), COUNT(*), COUNT(DISTINCT title) "
                        "FROM diary WHERE event_type='book_read' AND title LIKE ?",
                        (_z + ' \u2014 kap.%',)).fetchone()
                    _mx = _cb.execute(
                        "SELECT title FROM diary WHERE event_type='book_read' "
                        "AND title LIKE ? ORDER BY ts DESC LIMIT 1",
                        (_z + ' \u2014 kap.%',)).fetchone()
                    if _r and _r[0]:
                        _d = lambda x: _dtb.datetime.fromtimestamp(x).strftime('%-d. %-m.')
                        _kap = str(_mx[0]).split('kap.')[-1].strip() if _mx else '?'
                        _prubeh[_z] = (
                            'knihu \u010dtu od %s, naposledy jsem \u010detl kap. %s (%s); '
                            '%s' % (_d(_r[0]), _kap, _d(_r[1]),
                                    'podle den\u00edku ji \u010dtu POPRV\u00c9'
                                    if _r[2] == _r[3] else
                                    '\u010d\u00e1st jsem \u010detl v\u00edckr\u00e1t'))
                _cb.close()
            except Exception:
                _prubeh = {}

            def _label(t, s):
                _n = (t or s or '')
                _z = str(_n).split(' \u2014 kap.')[0]
                if s == 'book_read' and _z in _prubeh:   # HANS_BOOK_PROGRESS_LABEL_V1
                    return '[Z knihy, kterou \u010dtu \u2014 %s; %s]' % (_n, _prubeh[_z])
                if _kodi and str(_n).strip().lower() in _kodi:
                    return ('[Cetl jsem si o tomhle clanek, kdyz v televizi bezel '
                            '\u201e%s\u201c — NENI to moje cetba te knihy/filmu]' % _n)
                return '[Z mých zápisků — %s]' % _n
            blk = '\n\n'.join(
                # HANS_NOTE_FULL_LENGTH_V1 (14. 9.) — 700 → 1800. Limit vznikl
                # 6. 8. bez zdůvodnění; hans-czech má num_ctx 16384 a system
                # prompt medián ~2 000 zn. Ořez usekl KAŽDOU studijní poznámku
                # (75/75, 796–1712 zn) a mistrovskou reflexi (14/14, do 1761)
                # zhruba v půlce — model i pojistka viděli jen úvod.
                '%s\n%s' % (_label(t, s), (x or '')[:1800])
                for _ts, s, _p, t, x in hits)
            logging.getLogger(_h.__name__).info(
                'HANS_KNOWLEDGE_FTS_V1: %d zápisků → grounding '
                '(RAG nic nenašel)', len(hits))
            return ('\n\n' + _h.ANTIKONFAB + '\n\nTOHLE MÁŠ VE SVÝCH ZÁPISCÍCH '
                    '(odpověz z toho; co v nich není, nedomýšlej):\n' + blk)
        except Exception as e:
            logging.getLogger(_h.__name__).debug('knowledge FTS: %s', e)
            return ''

    def _kniha_navazuje(self, text: str, klic: str, ted: float) -> bool:
        """HANS_BOOK_RECOMMEND_FOLLOWUP_V1 — navazuje veta na doporuceni
        cetby, ktere TEZ osobe padlo pred chvili?"""
        try:
            from scripts.hans_thread import _THREAD_TTL_S as _ttl
        except Exception:
            _ttl = 600.0
        _kdy = (getattr(self, '_kniha_posledni', None) or {}).get(klic)
        if not _kdy or ted - _kdy > _ttl:
            return False
        # rozresena veta z vlakna nese priveseny predmet — slova pocitej bez nej
        _holy = re.sub(r"\s*\(k t[eé]matu:.*\)\s*$", "", text or "")
        _slov = len(_holy.split())
        return (((_slov <= 8 and bool(self._KNIHA_NAVAZ_PAT.search(_holy)))
                 or (_slov <= 12 and bool(self._KNIHA_CESKEMU_PAT.search(_holy))))
                and not self._KNIHA_JINA_PAT.search(_holy))   # HANS_BOOK_FOLLOWUP_DATIVE_V1

    def _citace_ze_zapisku(self, podklad: str, dotaz: str):
        """HANS_GUARD_QUOTE_NOTE_V1/V2 — věta z VLASTNÍHO zápisku, která obsahuje
        VŠECHNO, na co se otázka ptá, nebo None.

        V2 (14. 9.): V1 vážila slova součtem a s CELOU poznámkou (1800 zn)
        vyhrála věta „uspořádání hradu mezi dvěma věžemi" místo „na dvou
        skalních věžích – Panna a Baba" — rozhodla slova „hrad" a „Trosky",
        která jsou v celé poznámce. V2 proto:
          • vyřadí slova z TITULKU zápisku (o čem zápisek je, to nerozlišuje),
          • chce POKRYTÍ: věta musí mít všechna zbylá slova otázky; při shodě
            vyhraje DŘÍVĚJŠÍ věta (poznámka má klíčová fakta na začátku),
          • když po vyřazení nezbude nic („co víš o hradu Kost?"), necituje.
        Češtině odpovídá porovnání bez diakritiky a se společným začátkem
        (≥ 3 znaky, ≥ 60 % kratšího slova): „věže" = „věžích".
        Jen bloky „[Z mých zápisků — …]" (glosa k TV pořadu se necituje).
        Změřeno 14. 9.: věže → Panna a Baba; nesouvisející zápisek → nic;
        obecný dotaz → nic; dvojí otázka („…a kdo je postavil?") → nic.
        """
        try:
            import unicodedata as _ud
            _fold = lambda s: "".join(c for c in _ud.normalize("NFKD", (s or "").lower())
                                      if not _ud.combining(c))
            _nevyznam = {"proc", "jake", "jaky", "jaka", "jak", "co", "je", "ma",
                         "mate", "mas", "znamo", "informace", "vite", "vis",
                         "dozvedel", "prave", "vlastne", "duvodem", "existence",
                         "ktery", "ktera", "nejvice", "zaujal", "muzete", "rict",
                         "rekni", "rici", "neco", "tom", "nich", "nej",
                         # číslovky — „dvě" z otázky jinak nesedne na „dvou"
                         "dva", "dve", "dvou", "tri", "ctyri", "jeden", "jedna"}
            _slova = lambda s: [w for w in re.findall(r"[a-z0-9]+", _fold(s))
                                if len(w) >= 3 and w not in _nevyznam]

            def _shoda(x, y):
                k = 0
                while k < min(len(x), len(y)) and x[k] == y[k]:
                    k += 1
                return k >= 3 and k >= 0.6 * min(len(x), len(y))

            tituly = re.findall(r"\[Z mých zápisků — ([^\]]*)\]\n", podklad or "")
            bloky = re.split(r"\[Z mých zápisků — [^\]]*\]\n", podklad or "")[1:]
            _tw = [w for ti in tituly for w in _slova(ti)]
            Q = [q for q in dict.fromkeys(_slova(dotaz))
                 if not any(_shoda(q, w) for w in _tw)]
            if not Q:
                return None
            nej, pokryti = None, 0.0
            for bl in bloky:
                for v in re.split(r"(?<=[.!?])\s+", bl.split("\n\n")[0].strip()):
                    v = v.strip()
                    if not (25 <= len(v) <= 400):
                        continue
                    s = _slova(v)
                    c = sum(1 for q in Q if any(_shoda(q, w) for w in s)) / len(Q)
                    if c > pokryti:
                        nej, pokryti = v, c
            prah = float((self.config.get("grounding_guard", {}) or {})
                         .get("citace_min", 0.99))
            return nej if (nej and pokryti >= prah) else None
        except Exception:
            return None

    def _agent_oslov(self, text, name):
        """HANS_AGENT_ADDRESSEE_V1 (14. 9.) — odpověď AGENTNÍ vrstvy šla uživateli
        rovnou, bez `fix_addressee`, takže majordomské „pane" ze šablon (39× v
        hans_agent) dorazilo i k cizímu člověku. Doloženo testem 14. 9.:
        „Koláč a já jsme se před chvílí bavili o „fotbal", pane." Týž krok, jaký
        dostává odpověď LLM (HANS_ADDRESSEE_V2) — žádná druhá pravda o oslovení."""
        try:
            from scripts.cz_names import fix_addressee
            _t, _n = fix_addressee(text, name, self.config)
            if _n:
                logging.getLogger(_h.__name__).info(
                    "HANS_AGENT_ADDRESSEE_V1: opraveno %d oslovení v odpovědi agenta "
                    "(partner=%s)", _n, name)
            return _t
        except Exception:
            return text

    def _knihovna_fact(self, text: str, name=None) -> str:
        """HANS_BOOK_RECOMMEND_GROUNDED_V1 (13. 9.) — na zadost o doporuceni
        cetby podstrc SKUTECNOU knihovnu.

        Dolozeno 13. 9.: Hans doporucil tri knihy a ani jedna neexistuje.
        Mechanismus: slepil tema z vlastniho cteni se jmenem z JINEHO clanku
        (\u201eSchutz\u201c z hesla o barokni hudbe). Pritom ma 8 doctenych knih —
        do promptu se ale nedostavaly vubec.

        Vraci '' kdyz veta o doporuceni neni nebo je knihovna prazdna
        → Hans se chova jako dosud, nic se nerozbije.
        """
        if not text:
            return ''
        import time as _time          # HANS_BOOK_RECOMMEND_FOLLOWUP_V1
        _klic, _ted = (name or ""), _time.time()
        _navazuje = False
        if not self._KNIHA_DOPORUC_PAT.search(str(text)):
            if not self._kniha_navazuje(str(text), _klic, _ted):
                return ''
            _navazuje = True
        try:
            import sqlite3 as _s3
            _db = ((self.config.get("paths", {}) or {}).get("diary_db")
                   or self.config.get("diary_db") or "data/hans_diary.db")
            _c = _s3.connect(_db)
            _r = _c.execute(
                "SELECT book_title, author, status FROM hans_library "
                "WHERE status IN ('finished','reading') "
                "ORDER BY CASE status WHEN 'reading' THEN 0 ELSE 1 END, id DESC"
            ).fetchall()
            _c.close()
        except Exception:
            return ''
        if not _r:
            return ''
        _radky = []
        for _t, _a, _st in _r[:20]:
            _kdo = (" — " + _a) if _a and _a != "nahr\u00e1no u\u017eivatelem" else ""
            _stav = "prave ctu" if _st == "reading" else "docteno"
            _radky.append("- %s%s (%s)" % (_t, _kdo, _stav))
        # HANS_BOOK_RECOMMEND_FOLLOWUP_V1 — zapamatuj, ze TEHLE osobe padlo
        # doporuceni (i navazujici dotaz okno prodlouzi).
        if not isinstance(getattr(self, '_kniha_posledni', None), dict):
            self._kniha_posledni = {}
        self._kniha_posledni[_klic] = _ted
        _navaz_veta = ("\n\nUZIVATEL NAVAZUJE NA TVE PREDCHOZI DOPORUCENI: kdyz chce "
                       "jinou knihu, vyber JINOU ze seznamu; kdyz se pta proc, vysvetli "
                       "to jen z toho, co o knize ze seznamu opravdu vis."
                       if _navazuje else "")
        return _navaz_veta + ("\n\nKNIHY, KTERE JSI SKUTECNE CETL (jen tyhle, nic jineho nemas):\n"
                + "\n".join(_radky)
                + "\n\nDOPORUC PRESNE JEDEN NAZEV Z TOHOHLE SEZNAMU, opsany "
                  "SLOVO OD SLOVA i s autorem (i kdyz je anglicky). NEPREKLADEJ ho, "
                  "NEZAMENUJ za jinou knihu tehoz autora a NEPRIDAVEJ nic, co v seznamu "
                  "neni. Rekni, proc prave tu. Kdyz se nic nehodi, PRIZNEJ, ze jsi zatim "
                  "nic vhodneho necetl. Slepit tema z jednoho zdroje se jmenem z jineho "
                  "je VYMYSL — 13. 9. tak vznikly tri neexistujici knihy.")

    def _obraz_fact(self, text: str) -> str:
        """Popis posledniho obrazu z deniku, nebo '' (Hans se chova jako dosud)."""
        if not text or not self._OBRAZ_OBSAH_PAT.search(self._bez_tazatele(text)):
            return ''
        try:
            import sqlite3 as _s3
            import json as _js_o
            _db = ((self.config.get("paths", {}) or {}).get("diary_db")
                   or self.config.get("diary_db") or "data/hans_diary.db")
            _c = _s3.connect(_db)
            _r = _c.execute(
                "SELECT ts, title, data FROM diary WHERE event_type='artwork' "
                "ORDER BY ts DESC LIMIT 1").fetchone()
            _c.close()
        except Exception:
            return ''
        if not _r:
            return ''
        try:
            _d = _js_o.loads(_r[2] or "{}") or {}
        except Exception:
            _d = {}
        _vis = str(_d.get("vision") or "").strip()[:900]
        _pr = str(_d.get("prompt") or "").strip()[:400]
        if not (_vis or _pr):
            return ''
        try:
            from scripts.hans_recall import _cz_when
            _kdy = _cz_when(_r[0])
        except Exception:
            _kdy = ""
        return ("\n\nOBRAZ, NA KTERY SE PTA — tvuj posledni obraz \u201e%s\u201c%s:\n"
                "CO JE NA HOTOVEM OBRAZE (popis, anglicky): %s\n"
                "ZADANI, PODLE KTEREHO VZNIKL (anglicky): %s\n\n"
                "POPIS OBRAZ JEN Z TOHO, co je tu napsane, CESKY a vlastnimi slovy. "
                "NEPRIDAVEJ postavy, predmety ani barvy, ktere tu nejsou. Kdyz se "
                "popis a zadani lisi, plati POPIS HOTOVEHO OBRAZU."
                % (_r[1] or "", (" (%s)" % _kdy) if _kdy else "",
                   _vis or "(nemam)", _pr or "(nemam)"))

    def _puvod_studia_fact(self, text: str) -> str:
        """HANS_STUDY_WHY_TOPIC_V1 — "proc zrovna X", kdyz X je tema studia.
        Prazdne = Hans se chova jako dosud."""
        try:
            from scripts.chat_commands import _puvod_tema_z_proc, _studium_puvod
        except Exception:
            return ''
        _holy = re.sub(r"\s*\(k t[eé]matu:.*\)\s*$", "", self._bez_tazatele(text or ""))
        _db = ((self.config.get("paths", {}) or {}).get("diary_db")
               or self.config.get("diary_db") or "data/hans_diary.db")
        _tema = _puvod_tema_z_proc(_holy, _db)
        if not _tema:
            return ''
        _puvod = re.sub(r",?\s*pane\b", "", _studium_puvod(None, _db, _tema) or "")
        _stav = ""
        try:
            import sqlite3 as _s3
            import json as _js_s
            _c = _s3.connect(_db)
            _r = _c.execute("SELECT status, current_index, curriculum FROM study_program "
                            "WHERE topic=? ORDER BY id DESC LIMIT 1", (_tema,)).fetchone()
            _c.close()
            if _r:
                _n = len(_js_s.loads(_r[2] or "[]") or [])
                _stav = ("program „%s“: %s, pod-téma %d z %d"
                         % (_tema, "právě ho studuji" if _r[0] == "active"
                            else "mám ho dostudovaný", min(int(_r[1] or 0) + 1, _n or 1), _n))
        except Exception:
            _stav = ""
        logging.getLogger(_h.__name__).info(
            "HANS_STUDY_WHY_TOPIC_V1: '%.40s' → původ tématu '%s'", _holy, _tema)
        return ("\n\nPROC STUDUJES TEMA „%s“ — fakta z tveho deniku:\n- %s%s\n\n"
                "ODPOVEZ JEN Z TECHTO FAKT, cesky, 2-3 vetami: ze tohle tema OPRAVDU "
                "studujes a jak jsi k nemu prisel. Studium NEZAPIREJ a duvod, ktery "
                "tu neni, si NEVYMYSLEJ."
                % (_tema, _puvod, ("\n- " + _stav) if _stav else ""))

    def _kodi_film_fact(self, raw: str, turns) -> str:
        """HANS_KODI_FILM_FACT_V1 (4. 10.) — film Z KNIHOVNY, o kterém je řeč:
        rok, žánr, režie, děj a herci s rolemi z Kodi. /tazatel 3. 10.: Hans
        doporučil „Road to Perdition“, na „je to tak dobrý?“ vymyslel role
        (Hanks „jako John Logan“, syn „Tyler James Williams“) i kameramana —
        `factual_nofacts`, protože HANS_KODI_CAST_FACT chytá jen otázku NA
        herce. Titul musí PŘESNĚ sedět s knihovnou (žádný fuzzy únos) a být
        ve zprávě, nebo v Hansově poslední replice při navazující otázce."""
        kodi = getattr(getattr(self, "_hans_idle", None), "kodi", None)
        if not kodi or not raw:
            return ''
        norm = getattr(kodi, "_norm_title", None) or (lambda s: (s or "").lower())
        posl = ""
        for role, txt in reversed(list(turns or [])):
            if role == "assistant":
                posl = str(txt or "")
                break
        _q = re.compile(r"[„\"*]([^„\"“*\n]{2,70})[\"“*]")
        nr = " %s " % norm(raw)
        kand = []
        for x in _q.findall(raw) + _q.findall(posl):
            if x.strip() and x.strip() not in kand:
                kand.append(x.strip())
        navaz = bool(re.search(r"\b(?:to|ten|ta|ho|ji|n[ěe]m|n[ěe]j|n[íi]|film\w*|tom|tam|"
                               r"hraje|hraj[íi]|obsazen\w*)\b", raw, re.I)) \
            and len(raw.split()) <= 25
        for k in kand:
            nk = norm(k)
            if len(nk) < 3:
                continue
            ve_zprave = (" %s " % nk) in nr
            if not (ve_zprave or (navaz and k in posl)):
                continue
            mv = kodi.find_movie(k)
            if not mv or nk not in (norm(mv.get("title")), norm(mv.get("originaltitle"))):
                continue
            d = kodi.movie_details(mv.get("movieid")) or {}
            radky = ["FILM Z MÉ KNIHOVNY — „%s“ (%s%s):" % (
                d.get("title") or mv.get("title"), d.get("year") or mv.get("year") or "?",
                (", " + ", ".join((d.get("genre") or [])[:3])) if d.get("genre") else "")]
            if d.get("director"):
                radky.append("Režie: %s" % ", ".join(d["director"][:3]))
            if d.get("plot"):
                radky.append("Děj: %s" % re.sub(r"\s+", " ", d["plot"])[:500])
            herci = ["%s%s" % (c.get("name"), (" jako %s" % c["role"]) if c.get("role") else "")
                     for c in (d.get("cast") or [])[:8] if c.get("name")]
            if herci:
                radky.append("Hrají: %s" % "; ".join(herci))
            radky.append("O ději, hercích, rolích a režii mluv JEN podle tohohle. Kameru, "
                         "ocenění, hudbu ani další údaje, které tu nejsou, si NEDOMÝŠLEJ; "
                         "vlastní dojem či doporučení smíš.")
            logging.getLogger(_h.__name__).info(
                'HANS_KODI_FILM_FACT_V1: film z knihovny %r (%d herců)', k[:40], len(herci))
            return '\n\n' + "\n".join(radky)
        return ''

    def _kodi_cast_fact(self, text: str) -> str:
        """Obsazení (a režie) toho, o čem je řeč — deterministicky z Kodi.

        Vrací '' když věta o obsazení není, titul se nepodařilo určit, nebo
        knihovna nic nemá. Prázdný výsledek = Hans dál abstinuje; NIC se
        nedomýšlí.
        """
        t = (text or "").strip()
        # HANS_CAST_NOT_ORDER_V1 — vzor bydlí v hans_intent (sdílí ho agent).
        try:
            from scripts.hans_intent import pta_se_na_obsazeni as _ptaji
            _je_to_ono = _ptaji(t)
        except Exception:
            _je_to_ono = bool(self._CAST_PAT.search(t))
        if not t or not _je_to_ono:
            return ''
        kodi = getattr(getattr(self, "_hans_idle", None), "kodi", None)
        if not kodi:
            return ''
        # 1) O ČEM je řeč: rozřešená věta z vlákna/F1 (holé „kdo tam hraje?"
        #    titul nenese), jinak to, co zrovna běží.
        kandidati = []
        _f1 = getattr(self, '_f1_query', None)
        if _f1:
            kandidati.append(str(_f1))
        try:
            _tc = getattr(self, '_thread_ctx', None)
            if _tc and _tc[1]:
                kandidati.append(str(_tc[1]))
            if _tc and len(_tc) > 2 and _tc[2]:
                kandidati.append(str(_tc[2]))
        except Exception:
            pass
        polozka, popis = None, ''
        for k in kandidati:
            try:
                ep = kodi.find_episode(k)
            except Exception:
                ep = None
            if ep:
                polozka = kodi.episode_details(ep.get("episodeid"))
                popis = kodi.episode_label(ep)
                break
            try:
                mv = kodi.find_movie(k)
            except Exception:
                mv = None
            if mv:
                polozka = kodi.movie_details(mv.get("movieid"))
                popis = mv.get("title") or ''
                break
        if polozka is None:
            # nikdo titul neřekl → ber, co běží na TV
            try:
                np = kodi.get_now_playing() or {}
            except Exception:
                np = {}
            nazev = (np.get("title") or np.get("label") or "").strip()
            if not nazev:
                return ''
            ep = kodi.find_episode(nazev)
            if ep:
                polozka = kodi.episode_details(ep.get("episodeid"))
                popis = kodi.episode_label(ep)
            else:
                mv = kodi.find_movie(nazev)
                if mv:
                    polozka = kodi.movie_details(mv.get("movieid"))
                    popis = mv.get("title") or nazev
        if not polozka:
            return ''
        herci = polozka.get("cast") or []
        if not herci:
            return ''
        radky = []
        for c in herci[:10]:
            jmeno = (c.get("name") or "").strip()
            role = (c.get("role") or "").strip()
            if not jmeno:
                continue
            radky.append("- %s%s" % (jmeno, (" jako %s" % role) if role else ""))
        if not radky:
            return ''
        rez = polozka.get("director") or []
        hlava = "OBSAZENÍ Z MÉ KNIHOVNY — „%s“:" % (popis or "tenhle titul")
        pata = ("Vyjmenuj POUZE tahle jména. Nikoho nepřidávej, role nedomýšlej; "
                "co tu není, o tom řekni, že to nevíš.")
        blok = [hlava] + radky
        if rez:
            blok.append("Režie: %s" % ", ".join(rez[:3]))
        if len(herci) > 10:
            blok.append("(v seznamu je celkem %d jmen, tohle je prvních %d)"
                        % (len(herci), len(radky)))
        blok.append(pata)
        logging.getLogger(_h.__name__).info(
            'HANS_KODI_CAST_FACT_V1: obsazení z knihovny pro %r (%d jmen)',
            popis[:40], len(herci))
        return '\n\n' + _h.ANTIKONFAB + '\n\n' + "\n".join(blok)

    def _dohledej_kotvu(self, veta: str, name: str = None,
                        mel_zapisky: bool = False):   # HANS_LOOKUP_HAD_NOTES_V1
        """HANS_ANCHOR_LOOKUP_V1 (22.8.) — dohledej PŘEDMĚT dotazu a vrať
        provizorní odpověď, nebo None (pak platí dosavadní chování).

        Téma bere z KOTVY (`hans_convindex.kotva_tematu`), ne z regexu
        „znáš X?" — ten míjel reálné formulace: ze čtyř skutečných vět
        o hradu Kost rozpoznal téma jen u jedné (změřeno 22.8.).
        Zápis jde JEN do čekárny `unverified_findings`; noční ověření
        a ranní oprava už běží ([[instant-lookup-verify-loop]]).
        """
        try:
            from scripts.hans_convindex import kotva_tematu
            from scripts.hans_findings import lookup_now
            _known = tuple((self.config.get("known_persons", {}) or {}).keys()) + \
                tuple(str(v.get("nom", "")) for v in
                      (self.config.get("known_persons", {}) or {}).values())
            # Vlastní jméno taky — v oslovení („Dobrý večer, Hansi") je to
            # kotva jako každá jiná a bez tohohle by se šel hledat na
            # Wikipedii sám. Skloňované tvary řeší `kotva_tematu`.
            try:
                from scripts.hans_persona import persona_name as _pn
                _known += (str(_pn(self.config) or ""),)
            except Exception:
                pass
            # HANS_LOOKUP_ANCHOR_REWRITE_V1 (14. 9.) — (a) jméno TAZATELE taky
            # není téma: „jmenuji se Marek…" → hledal se na Wikipedii „Marek";
            # (b) kotva z PŘEPSANÉHO dotazu (F1 doplní předmět z vlákna),
            # syrová věta až jako záloha: „…studoval Český ráj… proč má Trosky
            # dvě věže?" dávalo „Český ráj", přepis „Proč má hrad Trosky dvě
            # věže?" dá „hrad Trosky". Simulace na 3 větách z 14. 9.: 3/3.
            if name:
                _known += (str(name),)
            _prepis = getattr(self, '_f1_query', None)
            tema = (kotva_tematu(_prepis, vynech=_known) if _prepis else None) \
                or kotva_tematu(veta or "", vynech=_known)
            if not tema:
                # HANS_CONCEPT_ASK_V1 (7.9.) — ZÁLOHA pro OBECNÝ POJEM.
                # `kotva_tematu` pozná téma podle VELKÉHO písmene, protože je
                # stavěná na vlastní jména („hrad Kost"). Obecné slovo psané
                # malým písmenem („co to je karbunkule?") tedy z principu
                # nenajde a dohledání se nespustí. Predikát je SDÍLENÝ
                # (`hans_intent`), ať o tom, co je dotaz na pojem, existuje
                # jedna pravda. Jen záloha — když kotva téma najde, neptáme se.
                try:
                    from scripts.hans_intent import dotaz_na_pojem
                    tema = dotaz_na_pojem(veta or "") or None
                except Exception:
                    tema = None
            # HANS_ANCHOR_ENTITY_FIRST_V1 (20. 9.) — KDYŽ C1 ENTITU ZNÁ,
            # MÁ PŘEDNOST PŘED KOTVOU. Doloženo 20. 9. 13:37: uživatel napsal
            # „Grand Tour, to je zajimave…", entitní vrstva rozřešila
            # „Grand Tour (cyklistika)" (ev=2) — a dohledání přesto šlo podle
            # kotvy „Tour", protože `kotvy_ve_vete` bere velké písmeno UVNITŘ
            # věty a víceslovnému názvu NA ZAČÁTKU tím uřízne první slovo.
            # Týž tvar: „Icon of the Seas…" → kotva „Seas", „Le Guin…" → „Guin".
            # ⛔ Poziční oprava (přilepit první slovo věty) byla ZMĚŘENA
            # a zamítnuta: na 1 467 zprávách 8 změn, z toho 1 správná
            # a 7 škodlivých („Hrad hrad Gutštejn", „Je <Jméno>", „A Koláč").
            # 📏 Změřeno až na úrovni skutečného článku (`_wikipedia_search`):
            #   'Tour' → None          × 'Grand Tour'       → Grand Tour
            #   'Seas' → Oasis of the Seas × 'Icon of the Seas' → Icon of the Seas
            # Tedy 2 ze 4 doložených případů opraveno, 0 poškozeno;
            # „Ahoj Hansi" entitu nevrátí, takže se nic nemění.
            try:
                _es = self._entity_store()
                if _es is not None:
                    _e = _es.resolve(self._bez_tazatele(
                        _prepis or veta or ""))
                    _jm = str((_e or {}).get("name") or "").strip()
                    if (_e and _jm and not self._entita_je_tazatel(_e)
                            and _jm.lower() != str(tema or "").lower()):
                        logging.getLogger(_h.__name__).info(
                            'HANS_ANCHOR_ENTITY_FIRST_V1: kotva %r → entita %r',
                            tema, _jm)
                        tema = _jm
            except Exception as _efe:
                logging.getLogger(_h.__name__).debug(
                    'HANS_ANCHOR_ENTITY_FIRST_V1: %s', _efe)
            if not tema:
                return None
            _dbp = (self.config.get("hans_idle", {}) or {}).get(
                "diary_db") or self.config.get("diary_db") or "data/hans_diary.db"
            out = lookup_now(self.config, _dbp, tema, veta or "", asker=name,
                             mel_zapisky=mel_zapisky)
            logging.getLogger(_h.__name__).info(
                "HANS_ANCHOR_LOOKUP_V1: téma %r → %s", tema,
                "dohledáno" if out else "nic (platí dosavadní odpověď)")
            return out
        except Exception as e:
            logging.getLogger(_h.__name__).warning(
                "HANS_ANCHOR_LOOKUP_V1 selhalo: %s", e)
            return None

    def _capability_fact(self, text: str) -> str:
        """HANS_CAP_HOWTO_V1 (26.8.) — „kam/kde/jak" u KONKRÉTNÍ schopnosti.

        Doloženo: „kam mi pošleš ten snímek?" → Hans nejdřív nabídl hlídání
        zapnout, po opravě agenta odpověděl abstinencí — a přitom odpověď
        („na Matrix") je v `hans_capabilities` celou dobu. Celý VÝČET schopností
        v promptu je (`capabilities_context`), jenže se v něm ztratí a zafunguje
        abstinenční brzda. Tady se dodá JEDNA konkrétní jako tvrdý podklad.

        ⚠️ Doručuje se GROUNDINGEM, ne příkazem: vzor na „kam|kde|jak" jako
        `nl_patterns` KRADE routing — vyzkoušeno 26.8., „jak jde studium?"
        pak šlo mimo `/studium`. Grounding do routingu nesahá.
        Prázdné = nic jistého → dotaz jde normální cestou.
        """
        try:
            t = str(text or "")
            if not re.search(r"\b(kam|kde|jak|jakto)\b", t, re.IGNORECASE):
                return ''
            from scripts.hans_capabilities import capability_for
            popis = capability_for(t)
            if not popis:
                return ''
            logging.getLogger(_h.__name__).info(
                'HANS_CAP_HOWTO_V1: dotaz míří na schopnost → %.60s', popis)
            return "Ověřený fakt o mé schopnosti: " + popis
        except Exception:
            return ''

    def _entita_je_tazatel(self, ent) -> bool:
        """HANS_ENTITY_NOT_ASKER_V1 (15. 9.) — je entita JMENO TAZATELE?

        HANS_ENTITY_STRIP_ASKER_V1 umaze jen uvod "X se pta:". F1 prepis ale
        jmeno presune DOVNITR vety ("Co <Jmeno> mysli tim na tom obraze?")
        a C1 pak jako entitu vybral tazatele → model z ni vymyslel obsah
        obrazu (doloženo 15. 9.). Porovnava se CELE jmeno entity, takze
        entita "Jmeno Prijmeni" se shodnym krestnim jmenem zustava.
        """
        try:
            import unicodedata as _ud
            _f = lambda s: "".join(
                c for c in _ud.normalize("NFKD", str(s or "").strip().lower())
                if not _ud.combining(c))
            _kdo = getattr(self, "_tazatel_ted", "") or ""
            _jm = _f((ent or {}).get("name"))
            if not _kdo or not _jm:
                return False
            _formy = {_f(_kdo)}
            try:
                from scripts.cz_names import display_name
                _formy.add(_f(display_name(_kdo, self.config)))
            except Exception:
                pass
            if _jm in _formy:
                logging.getLogger(_h.__name__).info(
                    "HANS_ENTITY_NOT_ASKER_V1: entita %r je jméno tazatele — "
                    "nepoužiji", (ent or {}).get("name"))
                return True
        except Exception:
            pass
        return False

    def _f1_o_tazateli(self, novy: str, kdo: str = "") -> bool:
        """HANS_F1_NOT_ABOUT_ASKER_V1 (16. 9.) — prehodil prepis podmet na tazatele?

        Vraci True jen kdyz prepis (a) jmenuje tazatele nebo mluvi o
        „uzivateli“ a zaroven (b) NEOBSAHUJE zadny tvar 2. osoby. Druha
        podminka je podstatna: „Jak dlouho uz PRACUJES pro X…“ jmeno obsahuje,
        ale porad se pta Hanse — a ten prepis je spravny (zmereno na 35 vzorcich).
        Pri jakekoli pochybnosti False = prepis se ponecha (dnesni chovani).
        """
        try:
            import unicodedata as _ud
            _f = lambda s: "".join(
                c for c in _ud.normalize("NFKD", str(s or "").strip().lower())
                if not _ud.combining(c))
            _txt = _f(novy)
            if not _txt:
                return False
            _kdo = _f(kdo or getattr(self, "_tazatel_ted", "") or "")
            _formy = {x for x in (_kdo, "uzivatel") if x}
            try:
                from scripts.cz_names import display_name
                if _kdo:
                    _formy.add(_f(display_name(_kdo, self.config)))
            except Exception:
                pass
            _formy |= {x[:-1] for x in _formy
                       if len(x) >= 5 and x[-1] in "aeiouy"}    # V2: kmen jména
            _jmenuje = any(
                re.search(r"(?<![a-z])" + re.escape(x) + r"[a-z]{0,4}(?![a-z])",
                          _txt)
                for x in _formy if len(x) >= 4)
            if not _jmenuje:
                return False
            # V2: jméno tazatele v 1. pádě = podmět věty, i když vedlejší věta
            # mluví ve 2. osobě („Kolikrát musí <Jméno> čistit…, o kterém jsi mluvil?“)
            if _kdo and len(_kdo) >= 4 and re.search(
                    r"(?<![a-z])" + re.escape(_kdo) + r"(?![a-z])", _txt):
                return True
            if self._F1_2OS.search(_txt) or any(
                    m.group(0) not in self._F1_NE_SLOVESA
                    for m in self._F1_2OS_SLOVESA.finditer(_txt)):
                return False        # porad se pta Hanse → prepis je v poradku
            return True
        except Exception:
            return False

    def _bez_tazatele(self, text) -> str:
        return self._ASKER_PFX.sub("", str(text or ""))

    def _entity_facts_line(self, text: str) -> str:
        """HANS_ENTITY_FACTS_ALSO_WITH_NOTES_V1 — jen řádek strukturovaných
        faktů k entitě z dotazu (bez glosy). Prázdné, když entita není nebo
        fakta nemá — nic se nedomýšlí."""
        try:
            _es = self._entity_store()
            if _es is None:
                return ''
            _ent = _es.resolve(self._bez_tazatele(text))
            if not _ent or self._entita_je_tazatel(_ent):   # HANS_ENTITY_NOT_ASKER_V1
                return ''
            return _es._facts_line(_ent.get('id'))
        except Exception:
            return ''

    def _entity_fact(self, text: str) -> str:
        """HANS_ENTITY_STORE_C1_V1 — deterministicky resolvuj entitu z dotazu
        proti store známých entit (z Hansova čtení). Vrátí autoritativní fakt
        (definiční věta ze zdroje) nebo '' když nic. Zabíjí kolizi jmen
        (Sorge=skladatel, ne špión) i konfabulaci významu známých entit."""
        try:
            _es = self._entity_store()
            if _es is None:
                return ''
            _ent = _es.resolve(self._bez_tazatele(text))   # HANS_ENTITY_STRIP_ASKER_V1
            if not _ent or self._entita_je_tazatel(_ent):   # HANS_ENTITY_NOT_ASKER_V1
                return ''
            logging.getLogger(_h.__name__).info(
                'C1: entita resolvována z dotazu → %r (ev=%s)',
                _ent.get('name'), _ent.get('evidence_count'))
            return _es.fact_block(_ent)
        except Exception:
            return ''

    def _self_runtime_fact(self, text: str) -> str:
        """Deterministický blok o Hansově vlastním režimu (spánek/kamera/hlídání).

        Vrací '' když se věta režimu netýká. Jinak fakta + zákaz tvrdit, že
        něco přepíná — sám to neumí, mění se to na povel (`/sleep`, `/hlidej`).
        """
        t = (text or "").strip()
        # HANS_SELF_RUNTIME_NARROW_V1 — tři cesty k témuž: pojmenovaný režim,
        # vidění (SDÍLENÝ vzor, ne kopie) a holé „spíš“ v krátké otázce.
        if not t:
            return ''
        if not (self._SELF_RUNTIME_PAT.search(t)
                or self._VIDIS_ME_PAT.search(t)
                or (self._SPIS_PAT.search(t) and len(t) <= 25 and "?" in t)):
            return ''
        st = self._runtime_state()
        if not st:
            return ''
        lines = []
        if st.get("sleeping") is not None:
            lines.append("spím (noční režim)" if st["sleeping"]
                         else "jsem vzhůru, v běžném provozu")
        if st.get("guard") is not None:
            lines.append("hlídací režim je zapnutý" if st["guard"]
                         else "hlídací režim je vypnutý")
        if not lines:
            return ''
        logging.getLogger(_h.__name__).info(
            'HANS_SELF_STATE_AWAKE_V2: dotaz na vlastní režim → %s', lines)
        return ("MŮJ SKUTEČNÝ REŽIM PRÁVĚ TEĎ (odpověz POUZE podle tohohle):\n"
                + "\n".join("- %s" % x for x in lines)
                + "\nNikdy netvrď, že něco přepínáš nebo jsi přepnul — režim "
                  "sám měnit neumím, děje se to na povel uživatele.")

    def _runtime_state(self) -> dict:
        """HANS_SELF_STATE_AWAKE_V1 — skutečný provozní stav Hanse.
        ⚠️ Handler `_routine` NEMÁ — drží ho `hans_idle` (vzorec z
        TIME_AWARENESS_V1, ř. 1342); přímý `getattr(self, "_routine")` by
        tiše vracel None a stav by se nikam nedostal."""
        out = {}
        try:
            _hi = getattr(self, "_hans_idle", None)
            _rt = getattr(_hi, "_routine", None) if _hi else None
            if _rt is not None:
                out["sleeping"] = bool(getattr(_rt, "_sleeping", False))
            import json as _js_g
            import os as _os_g
            _gp = "data/.hans_guard"
            if _os_g.path.exists(_gp):
                with open(_gp, encoding="utf-8") as _gf:
                    out["guard"] = bool((_js_g.load(_gf) or {}).get("armed"))
            else:
                out["guard"] = False
        except Exception as _rse:
            logging.getLogger(_h.__name__).debug('runtime_state: %s', _rse)
        return out

    def _person_fact(self, text: str) -> str:
        """Deterministický fakt o členovi domácnosti z `relationships`.

        Vrací blok pro grounding, nebo '' když dotaz není na identitu osoby
        / osoba není známá. Shoda na PREFIX (4 znaky, bez diakritiky), aby
        prošlo skloňování (2.–7. pád) i psaní bez háčků — právě rozdíl
        „jméno bez diakritiky" × „s diakritikou" dnes rozhodoval mezi
        zapřením a výmyslem. Deaktivované záznamy (testovací osoby) se přeskakují.
        """
        t = (text or "").strip()
        if not t or not self._PERSON_Q_PAT.search(t):
            return ''
        try:
            import sqlite3 as _sql
            _dbp = (self.config.get("diary_db")
                    or (self.config.get("hans_idle", {}) or {}).get("diary_db")
                    or "data/hans_diary.db")
            db = _sql.connect("file:%s?mode=ro" % _dbp, uri=True, timeout=3.0)
            rows = db.execute(
                "SELECT person_id, display_name, role, family_links, "
                "characterization FROM relationships "
                "WHERE COALESCE(deactivated_at, 0) = 0").fetchall()
            db.close()
        except Exception as e:
            logging.getLogger(_h.__name__).debug('person_fact: %s', e)
            return ''
        toks = [w for w in re.split(r"[^\w]+", self._fold(t)) if len(w) >= 3]
        hit = None
        for pid, disp, role, links, charact in rows:
            for cand in (disp, pid):
                fc = self._fold(cand)
                # Shoda na TŘI znaky + strop na rozdíl délek. Čtyři znaky
                # nestačí — česká deklinace u krátkých jmen mění právě
                # 4. písmeno (dativ), takže 4-znakový prefix ty tvary zahodí
                # (změřeno). Tři znaky samy o sobě pouštějí i cizí slova,
                # proto délková pojistka: obojí musí být zhruba stejně dlouhé.
                # Zbylý falešný poplach stojí jen jeden blok navíc v groundingu
                # a spouští se výhradně u otázek na identitu — proto se loguje.
                if len(fc) < 3:
                    continue
                if any(w[:3] == fc[:3] and abs(len(w) - len(fc)) <= 3
                       for w in toks):
                    hit = (pid, disp, role, links, charact)
                    break
            if hit:
                break
        if not hit:
            return ''
        pid, disp, role, links, charact = hit
        parts = ["%s — %s" % (disp or pid, role or "člen domácnosti")]
        try:
            import json as _js
            fam = _js.loads(links or "{}") or {}
            names = {r[0]: (r[1] or r[0]) for r in rows}
            if fam.get("parents"):
                parts.append("rodiče: %s" % ", ".join(
                    names.get(p, p) for p in fam["parents"]))
            if fam.get("children"):
                parts.append("děti: %s" % ", ".join(
                    names.get(c, c) for c in fam["children"]))
            if fam.get("spouse"):
                parts.append("partner: %s" % names.get(fam["spouse"], fam["spouse"]))
        except Exception:
            pass
        if charact:
            parts.append((charact or "").strip().split(". ")[0].strip() + ".")
        logging.getLogger(_h.__name__).info(
            'HANS_PERSON_FACT_V1: osoba resolvována z dotazu → %r', disp or pid)
        return "ZÁZNAM O OSOBĚ (z mé evidence domácnosti):\n" + "\n".join(
            "- %s" % p for p in parts)

# ROZDELENI_METOD_V1 — až na konci, viz hlavička
from scripts import openwebui_direct_handler as _h  # noqa: E402
