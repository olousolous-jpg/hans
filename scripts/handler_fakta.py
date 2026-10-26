"""Metody třídy `OpenWebUIDirectHandler` přesunuté z `scripts/openwebui_direct_handler.py` (ROZDELENI_METOD_V1).

Text metod je beze změny; jména původního modulu se čtou přes `_h.` až při
volání. `OpenWebUIDirectHandler` má třídu `FaktaMixin` mezi předky, takže volání přes `self` platí dál.
Import původního modulu je na KONCI souboru (kruhový import oběma směry).
"""
from __future__ import annotations

import logging
import re

# HANS_NOTES_TITLE_SUPPORT_V1 — slova, která do titulu zápisku přidává Hans
# sám (druh zápisku), ne jeho předmět
_ZAPISKY_TITUL_OBAL = {"studium", "mistrovska", "reflexe", "zpravy", "kap"}

# HANS_LOOKUP_OBSAHEM_F1_SLOVO_V1 — plnovýznamové slovo vstupu bez háčků, které
# v přepisu F1 nemá protějšek (prvních 4 písmen bez diakritiky): „mena“ →
# „jména“, „mloky“ → „mývaly“, „pelisky“ → „Markety Lazarové“
_F1_SLOVO_STOP = set("""jaky jaka jake jakou jakem ktery ktera ktere kterou kolik proc
kdyz jestli jsou bylo byla byli mate mame jsem jste jsme tvoje tvuj vase vas muzes muzete
rekni reknete prosim nejaky nejaka nejake neco nekdo vlastne zrovna jeste hele tedy takze
umis umite vis vite znas znate mohl mohla bych bys byste dneska dnes""".split())


def _f1_ztratil_slovo(veta: str, prepis: str) -> bool:
    import unicodedata as _ud

    def _f(x):
        return "".join(c for c in _ud.normalize("NFD", (x or "").lower())
                       if _ud.category(c) != "Mn")
    holy = re.sub(r"^\S+\s+se\s+pt[áa]:\s*", "", veta or "")
    if not holy or _f(holy) != holy.lower():
        return False          # jen vstup bez diakritiky (F1 doplňuje háčky)
    pf = [w for w in re.findall(r"\w+", _f(prepis))]
    # delší slovo smí v přepisu být i se změněnou předponou („umrel“ → „zemřel“)
    return any(not any(w[:4] == y[:4] or (len(w) >= 5 and w[1:5] in y) for y in pf)
               for w in re.findall(r"[a-z]{4,}", holy.lower()) if w not in _F1_SLOVO_STOP)


# HANS_LOOKUP_F1_SLOVESO_V1 (25. 10.) — přepis F1 nahradil sloveso otázky
# „autorem“ („Kdo složil Prodanou nevěstu?“ → „Kdo je autorem divadelní hry
# Prodaná nevěsta?“) a shrnutí podle přepisu vybralo libretistu. „napsal“ →
# „autorem“ smysl nemění, proto se nehlídá. 📏 1 511 přepisů: ztracené sloveso
# 17×, z toho jiné než „napsal“ 5× (složil, režíroval, hrálo, vydal, vymyslel).
_F1_SLOVESA = re.compile(r"\b(slozil\w*|zkomponoval\w*|nazpival\w*|reziroval\w*|natocil\w*|"
                         r"namaloval\w*|postavil\w*|navrhl\w*|zalozil\w*|objevil\w*|vynalezl\w*)\b")


def _f1_ztratil_sloveso(veta: str, prepis: str) -> bool:
    import unicodedata as _ud

    def _f(x):
        return "".join(c for c in _ud.normalize("NFD", (x or "").lower())
                       if _ud.category(c) != "Mn")
    holy = re.sub(r"^\S+\s+se\s+pt[áa]:\s*", "", veta or "")
    pf = re.findall(r"\w+", _f(prepis))
    return any(not any(w[:4] in y for y in pf) for w in _F1_SLOVESA.findall(_f(holy)))


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

    def _zapisky_k_veci(self, text: str) -> str:
        """HANS_NOTES_TITLE_SUPPORT_V1 (12. 10.) — zápisky jako OPORA jen tehdy,
        když aspoň jedno hledané slovo sedí kmenem v TITULU nalezeného zápisku.

        Hledání v zápiscích vyžaduje všechna slova dotazu KDEKOLI v textu;
        u hovorové věty se sejdou náhodou v dlouhém zápisku o něčem jiném
        („jak ses dnes měl?“ → článek o Senegalu, „kdo trénuje Slovensko na MS
        v hokeji“ → zápisek o designu). Podklad se pak označil jako opora,
        takže se nespustila abstinence a model odpovídal z cizího textu.
        Změřeno na korpusu (199 vět na téhle cestě): 110 nemá v titulu žádné
        slovo dotazu a jsou mimo věc; 89 zůstává. V provozu od 22. 9. cesta
        sepnula 131× (5× u člena domácnosti: 1 výmysl s falešným zdrojem,
        2 odpovědi mimo). Vrací podklad, nebo '' (věta pak pokračuje stejně,
        jako kdyby zápisky nic nenašly). Vypínač `grounding.notes_need_title`.
        """
        text = self._bez_tazatele(text)     # HANS_FTS_NO_ASKER_V1
        kb = self._knowledge_fts_grounding(text)
        if not kb:
            return kb
        try:
            if not ((self.config.get("grounding") or {})
                    .get("notes_need_title", True)):
                return kb
            from scripts import hans_convindex as _ci
            q = [_ci._fold(w) for w in _ci._WORD.findall(str(text or ""))]
            q = [w for w in q if len(w) >= 3 and w not in _ci._STOP]
            for tit in (getattr(self, "_kfts_tituly", None) or []):
                for b in (_ci._fold(w) for w in _ci._WORD.findall(tit)):
                    if b in _ZAPISKY_TITUL_OBAL:
                        continue
                    for a in q:
                        if a == b or (len(a) >= 4 and len(b) >= 4
                                      and a[:4] == b[:4]):
                            return kb
            logging.getLogger(_h.__name__).info(
                "HANS_NOTES_TITLE_SUPPORT_V1: zápisky %r nemají v titulu žádné "
                "slovo dotazu %r → nepočítám jako oporu",
                [x[:40] for x in (getattr(self, "_kfts_tituly", None) or [])][:3],
                str(text)[:50])
            return ''
        except Exception as _e:
            logging.getLogger(_h.__name__).debug("notes title support: %s", _e)
            return kb

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
            # HANS_FTS_NO_ASKER_V1 (19. 10.) — bez „<jméno> se ptá:“. Hledání
            # chce VŠECHNA slova, takže jméno tazatele a „ptá“ nález zabily
            # („a drakulu z 92?“ → nic, ačkoli zápisek o Drákulovi je).
            # 📏 log ~50 dní: 565 z 1 116 prázdných hledání neslo prefix;
            # vzorek 200 vět přepisů: bez prefixu nově 5 nálezů (2 správné,
            # 1 obecný, 2 šum — ty dál hlídá kontrola významu a titulu).
            text = self._bez_tazatele(text)
            hits = _kfts(str(text), limit=3, kind='knowledge')
            # HANS_NOTES_SEMANTIC_ALL_V1 (18. 10.) — kontrola významu
            # (HANS_RELAX_SEMANTIC_V1, bge-m3, práh 0,48) i pro NErelaxované
            # nálezy. Zápisek se slovem dotazu v titulu bývá o něčem jiném,
            # stačí jedno obecné slovo: ~13 % opor ze zápisků je cizích
            # (Karel IV. → Karel Sabina, telefon → Televizní studio Brno).
            # 📏 dvě ručně označené sady: zahodí ~13/33 cizích, ~5/125
            # správných. Vlastní četba knihy (kapitoly, úvahy) se nekontroluje
            # — text kapitoly otázce o knize významem nesedí (0,31) a je to
            # Hansův zážitek. Nedostupný model / herní mód → beze změny.
            # Vypínač grounding.notes_semantic.
            if hits and bool((self.config.get("grounding") or {})
                             .get("notes_semantic", True)):
                try:
                    from scripts.hans_convindex import _vyznamove_souvisi
                    _cetba = ('book_read', 'book_reflection')
                    _ost = [_x for _x in hits if _x[1] not in _cetba]
                    _nech = _vyznamove_souvisi(str(text), _ost) if _ost else []
                    if len(_nech) < len(_ost):
                        logging.getLogger(_h.__name__).info(
                            'HANS_NOTES_SEMANTIC_ALL_V1: %d z %d zápisků významem '
                            'mimo otázku %r → vyřazeno', len(_ost) - len(_nech),
                            len(hits), str(text)[:50])
                    hits = [_x for _x in hits if _x[1] in _cetba or _x in _nech]
                except Exception as _nse:
                    logging.getLogger(_h.__name__).debug(
                        'HANS_NOTES_SEMANTIC_ALL_V1: %s', _nse)
            self._kfts_tituly = [str(_x[3] or '') for _x in (hits or [])]
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
            # HANS_NOTES_YEAR_WORDS_V1 — letopočet slovy v zápisku: číslicemi,
            # zkomolený jako nečitelný (data se nemění, jen podklad)
            try:
                from scripts.cz_names import necitelne_roky as _nr
                hits = [(_a, _b, _c, _d, _nr(_e or '')[0]) for _a, _b, _c, _d, _e in hits]
            except Exception as _nre:
                logging.getLogger(_h.__name__).debug('HANS_NOTES_YEAR_WORDS_V1: %s', _nre)
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

    _HESLO_DOHLEDANI = re.compile(r"Wikipedie — heslo „([^\"“\n]{2,90})[\"“]")
    _NAVAZUJE_DOHLEDANI = re.compile(
        r"\b(?:v[íi]c\w*|d[áa]l\w*|je[šs]t[ěe]|podrobn\w*|n[ěe]m|n[íi]|n[ěe]j|"
        r"tom|ho|ji|to|ten|ta)\b", re.I)

    def _dohledani_navazuje(self, raw: str, turns) -> str:
        """HANS_LOOKUP_FOLLOWUP_V1 (12. 10.) — navazující otázka hned po
        provizorním dohledání („víc o něm nemáš?“) dostane text článku, který
        Hans před chvílí stáhl (noční čekárna), místo odpovědi z hlavy.
        Doloženo 12. 10.: po dohledání fyzika model na „víc“ vymyslel místo
        narození i univerzitu a uvedl to slovy „z Wikipedie“. Vrací blok
        podkladu, nebo '' (věta jmenuje něco jiného, článek v čekárně není —
        např. u zkušební identity, která do čekárny nezapisuje)."""
        try:
            posl = ""
            for role, txt in reversed(list(turns or [])):
                if role == "assistant":
                    posl = str(txt or "")
                    break
            m = self._HESLO_DOHLEDANI.search(posl)
            if not m or not raw or len(raw.split()) > 14:
                return ''
            heslo = m.group(1).strip()
            from scripts.hans_intent import jiny_predmet
            if not self._NAVAZUJE_DOHLEDANI.search(raw) or jiny_predmet(raw, heslo):
                return ''
            import sqlite3 as _sq
            import time as _tm
            _dbp = (self.config.get("diary_db")
                    or (self.config.get("hans_idle", {}) or {}).get("diary_db")
                    or "data/hans_diary.db")
            _c = _sq.connect("file:%s?mode=ro" % _dbp, uri=True, timeout=4)
            try:
                r = _c.execute(
                    "SELECT raw_text, url FROM unverified_findings WHERE "
                    "resolved_title=? AND ts>? AND COALESCE(raw_text,'')<>'' "
                    "ORDER BY id DESC LIMIT 1",
                    (heslo, _tm.time() - 86400)).fetchone()
            finally:
                _c.close()
            if not r:
                return ''
            logging.getLogger(_h.__name__).info(
                'HANS_LOOKUP_FOLLOWUP_V1: navazující otázka po dohledání → '
                'text článku %r (%d zn)', heslo[:40], len(r[0]))
            return ("\n\nČLÁNEK, KTERÝ JSEM PŘED CHVÍLÍ DOHLEDAL — Wikipedie, heslo "
                    "„%s“ (zatím neověřeno a nezapsáno do paměti):\n%s\n"
                    "Odpovídej JEN z tohoto textu a řekni, že je to z dohledaného "
                    "článku, ne z paměti. Co v něm není, přiznej, že v článku "
                    "není — nic si nedomýšlej. Do paměti si článek i s "
                    "podrobnostmi zapíšu sám po nočním ověření; nenabízej, že "
                    "si ho zapamatuješ na požádání, a neříkej, že jsi hledal "
                    "znovu." % (heslo, str(r[0])[:3000]))
        except Exception as _dne:
            logging.getLogger(_h.__name__).debug('lookup followup: %s', _dne)
            return ''

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
        # HANS_KODI_FILM_FACT_SUBJECT_V1 — předchozí věta tazatele (ne tahle)
        _pred = ""
        for role, txt in reversed(list(turns or [])):
            if role == "user" and str(txt or "").strip() != raw.strip():
                _pred = str(txt or "")
                break
        for k in kand:
            nk = norm(k)
            if len(nk) < 3:
                continue
            ve_zprave = (" %s " % nk) in nr
            if not (ve_zprave or (navaz and k in posl)):
                continue
            if not ve_zprave and self._jiny_film_ve_vete(raw, k):
                logging.getLogger(_h.__name__).info(
                    'HANS_KODI_FILM_FACT_OTHER_V1: věta jmenuje jiný film než %r '
                    '→ fakta z předchozí repliky nepřidávám', k[:40])
                continue
            if not ve_zprave:
                try:
                    from scripts.hans_intent import mluvi_o_filmu, jiny_predmet
                    from scripts.hans_persona import persona_name as _pn_ff
                    _vyn = (str(_pn_ff(self.config) or ""),) + tuple(
                        (self.config.get("known_persons", {}) or {}).keys())
                    if not mluvi_o_filmu(raw) and (
                            jiny_predmet(raw, k, _vyn)
                            or jiny_predmet(_pred, k, _vyn)):
                        logging.getLogger(_h.__name__).info(
                            'HANS_KODI_FILM_FACT_SUBJECT_V1: navazující věta '
                            'není o filmu %r → fakta z knihovny nepřidávám', k[:40])
                        continue
                except Exception as _fse:
                    logging.getLogger(_h.__name__).debug('film subject: %s', _fse)
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

    def _jmenuje_dilo(self, text: str, kandidati=()) -> bool:
        """HANS_KODI_CAST_NOWPLAYING_GUARD_V1 — jmenuje věta (nebo přepis)
        konkrétní dílo? Titul s výpisem faktů, přesný titul článku z věty,
        nebo „v článku / ve filmu / v seriálu / v díle <Název>“."""
        for s in [text] + list(kandidati or []):
            s = self._bez_tazatele(str(s or ""))
            if re.search(r"\b(?:v|ve)\s+(?:článku|clanku|filmu|seriálu|serialu|díle|dile|"
                         r"pořadu|poradu|knize|románu|romanu)\s+[„\"']?[A-ZÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ]", s):
                return True
            try:
                from scripts.hans_recall import fakta_tituly_ve_vete
                db = ((self.config.get("hans_idle", {}) or {}).get("diary_db")
                      or "data/hans_diary.db")
                if fakta_tituly_ve_vete(db, s):
                    return True
            except Exception:
                pass
        return False

    def _jiny_film_ve_vete(self, raw: str, titul: str) -> bool:
        """HANS_KODI_FILM_FACT_OTHER_V1 (19. 10.) — jmenuje věta po slovech
        JINÝ film z Hansova deníku než `titul`? Doloženo 19. 10.: po odpovědi
        o „Agent, který mne miloval“ přišlo „jack the ripper z 88 – kdo to
        režíroval?“ → blok faktů o Agentovi (`jiny_predmet` čte jen velká
        písmena). Párování týmž `_titul_po_slovech` jako film recall."""
        try:
            from scripts.hans_recall_filmy import _titul_po_slovech
            from scripts.hans_recall import _ro
            db = ((self.config.get("hans_idle", {}) or {}).get("diary_db")
                  or "data/hans_diary.db")
            c = _ro(db)
            try:
                tituly = [r[0] for r in c.execute(
                    "SELECT DISTINCT title FROM diary WHERE event_type IN "
                    "('movie_opinion','kodi_playing') AND length(title) >= 4").fetchall()]
            finally:
                c.close()
            jiny = _titul_po_slovech(raw, tituly)
            n = lambda s: re.sub(r"\s*\(.*$", "", str(s or "")).strip().lower()
            if jiny:
                return n(jiny) != n(titul)
            # HANS_KODI_FILM_FACT_OTHER_V2 (24. 10.) — i film, který Hans neviděl:
            # „Večer jsme koukali na film o Titanicu“ po řeči o Casablance → blok
            # faktů o Casablance. Kotva „film X“ (druh + název), jejíž název
            # nesdílí kmen s `titul` = jiný film.
            from scripts.hans_convindex import kotva_tematu
            _k = kotva_tematu(raw or "") or ""
            _m = re.match(r"(?:film\w*|seri[aá]l\w*|snímk\w*)\s+(.+)$", _k, re.I)
            if _m:
                _f = lambda s: re.findall(r"\w{3,}", re.sub(r"[^\w\s]", " ", n(s)))
                _a, _b = _f(_m.group(1)), _f(titul)
                if _a and not any(x[:4] == y[:4] for x in _a for y in _b):
                    return True
            return False
        except Exception:
            return False

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
        if polozka is None and self._jmenuje_dilo(t, kandidati):
            # HANS_KODI_CAST_NOWPLAYING_GUARD_V1 (19. 10.) — otázka JMENUJE
            # dílo, které knihovna nemá → ne obsazení toho, co běží na TV.
            # Koláčova zkouška #88: „Kdo hraje Kendyho v … Jak svět přichází
            # o básníky?“ → obsazení Harryho Pottera (běžel) → výmysl.
            logging.getLogger(_h.__name__).info(
                'HANS_KODI_CAST_NOWPLAYING_GUARD_V1: otázka jmenuje jiné dílo '
                '→ obsazení z TV nedávám: %.60s', t)
            return ''
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
        # HANS_KODI_CAST_YEAR_V1 (19. 10.) — rok v otázce musí sedět s rokem
        # položky. Doloženo: „jack the ripper z 88 – kdo to režíroval?“ → film
        # 1976 z knihovny (Franco, Kinski) vydaný za verzi 1988. Jiný rok =
        # jiné dílo → radši bez obsazení než cizí obsazení jako fakt.
        try:
            _rok_q = None
            for _k in [t] + kandidati:
                _mr = re.search(r"\b(1[89]\d\d|20\d\d)\b", str(_k))
                if not _mr:
                    _mr2 = re.search(r"\bz\s+(?:roku\s+)?(\d\d)\b", str(_k))
                    if _mr2:
                        _rok_q = 1900 + int(_mr2.group(1))
                        break
                else:
                    _rok_q = int(_mr.group(1))
                    break
            _rok_p = int(polozka.get("year") or 0)
            if _rok_q and _rok_p and abs(_rok_q - _rok_p) > 1:
                logging.getLogger(_h.__name__).info(
                    'HANS_KODI_CAST_YEAR_V1: %r je z roku %d, otázka chce %d '
                    '→ obsazení nedávám', (popis or '')[:40], _rok_p, _rok_q)
                return ''
        except Exception:
            pass
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
        # HANS_KODI_CAST_DEJ_V1 (24. 10.) — „a o čem je?“ po obsazení: děj z hlavy
        # („ikonická scéna v obytném voze na mostě“ u Hitchcocka); knihovna ho má
        _plot = str(polozka.get("plot") or "").strip()
        if _plot and re.search(r"o\s+(?:čem|cem)\b|děj|dej\b|příběh|pribeh|obsah|vypráv|vyprav|"
                                r"pojednáv|pojednav|zápletk|zapletk", " ".join([t] + kandidati), re.I):
            blok.append("Děj (z knihovny): %s" % _plot[:900])
            pata = pata.replace("co tu není", "o ději jen to, co stojí v popisu; co tu není")
        if len(herci) > 10:
            blok.append("(v seznamu je celkem %d jmen, tohle je prvních %d)"
                        % (len(herci), len(radky)))
        blok.append(pata)
        self._kodi_cast_titul = popis   # HANS_LOOKUP_PRESNY_PODKLAD_V1
        logging.getLogger(_h.__name__).info(
            'HANS_KODI_CAST_FACT_V1: obsazení z knihovny pro %r (%d jmen)',
            popis[:40], len(herci))
        return '\n\n' + _h.ANTIKONFAB + '\n\n' + "\n".join(blok)

    # HANS_LOOKUP_PRESNY_V1 (17. 10.) — „dohledat vždy“, ale JEN když předmět
    # otázky vede na článek přesně o něm (lokální Wikipedie, bez sítě).
    # 📏 korpus 281 faktických vět bez opory: libovolný článek 108 (≈⅓ cizí),
    # přesná shoda 39 (cizí ≈2); 18 otázek (8. 10. + /tazatel 17. 10.): 14
    # projde, 12 se správným údajem. ⛔ „dohledat vždy“ bez omezení (8. 10.)
    # platí dál — tohle je úzká varianta, rozhodl uživatel 17. 10.
    _PRESNY_DRUH = {"hrad", "zamek", "film", "kniha", "mesto", "kostel", "katedrala", "chram",
                    "roman", "novela", "povidka",   # HANS_WIKI_TITUL_Z_VETY_V1 (Saturnin (román))
                    "bazilika", "kaple", "hora", "serial", "bitva", "na", "u", "sv", "svateho",
                    "hra", "reka", "jezero", "ostrov", "muzeum", "klaster"}

    def _presny_predmet(self, veta: str, name: str = None):
        """Téma k dohledání, jen když lokální článek je PŘESNĚ o předmětu; jinak None."""
        import unicodedata as _ud

        def _f(x):
            return "".join(c for c in _ud.normalize("NFD", (x or "").lower())
                           if _ud.category(c) != "Mn")

        def _tok(x):
            return [w for w in re.findall(r"\w{2,}", _f(re.sub(r"\(.*?\)", "", x or "")))
                    if w not in self._PRESNY_DRUH]

        def _kryje(a, b):
            return all(any(x[:max(3, min(len(x), len(y)) - 2)] == y[:max(3, min(len(x), len(y)) - 2)]
                           for y in b) for x in a)
        self._presny_z_vety = None     # HANS_WIKI_TITUL_Z_VETY_V1 — jen pro tuto větu
        try:
            if not bool((self.config.get("instant_lookup", {}) or {}).get("presny_predmet", True)):
                return None
            # HANS_YEAR_EVENTS_V1 (19. 10.) — otázka na události / vládce / válku
            # v roce → článek o roce (Události, Hlavy států), ne odpověď z hlavy
            from scripts.hans_intent import rok_udalosti
            _rok = (rok_udalosti(getattr(self, '_f1_query', None) or "") or rok_udalosti(veta or "")
                    or getattr(self, '_rok_kontext', None))   # HANS_YEAR_CONTEXT_V1
            if _rok:
                logging.getLogger(__name__).info(
                    "HANS_YEAR_EVENTS_V1: otázka na rok %s → dohledám článek o roce", _rok)
                self._presny_z_vety = _rok
                return _rok
            from scripts.hans_intent import pta_se_na_zazitek_hanse, dotaz_na_pojem
            from scripts.hans_convindex import kotva_tematu
            from scripts import hans_wiki_offline as _wo
            if not _wo.dostupna() or pta_se_na_zazitek_hanse(veta):
                return None
            _kp = self.config.get("known_persons", {}) or {}
            _known = tuple(_kp.keys()) + tuple(str(v.get("nom", "")) for v in _kp.values())
            try:
                from scripts.hans_persona import persona_name as _pn
                _known += (str(_pn(self.config) or ""),)
            except Exception:
                pass
            if name:
                _known += (str(name),)
            _prepis = getattr(self, '_f1_query', None)
            tema = ((kotva_tematu(_prepis, vynech=_known) if _prepis else None)
                    or kotva_tematu(veta or "", vynech=_known))
            # HANS_WIKI_TITUL_Z_VETY_V1 (19. 10.) — přesný titul článku přímo
            # z holé věty, když kotva chybí, v holé větě není (přepis F1 si ji
            # vymyslel: „babicku“ → „Babí léto“, „karluv“ → „Karlov“), nebo je
            # jen začátkem delšího titulu („Válku“ × „Válka s Mloky“)
            try:
                _tzv = _wo.titul_z_vety(re.sub(r"^\S+\s+se\s+pt[áa]:\s*", "", veta or ""))
            except Exception:
                _tzv = None
            if _tzv:
                _tv = _tok(tema) if tema else []
                _vf = re.findall(r"\w+", _f(veta))
                _ve_vete = bool(_tv) and all(any(x[:4] == y[:4] for y in _vf) for x in _tv)
                if (not tema or not _ve_vete
                        # HANS_WIKI_ZAJMENO_V1 — kotva je tentýž název v jiném pádě
                        # („Mou vlast“ × „Má vlast“) → přesný titul
                        or (len(_tok(_tzv)) == len(_tv) >= 2 and _tok(_tzv)[1:] == _tv[1:]
                            and _wo._ZAJMENO_1P.get(_tv[0]) == _tok(_tzv)[0])
                        or (len(_tok(_tzv)) > len(_tv) and _kryje(_tv, _tok(_tzv)))
                        # tatáž věc upřesněná druhem („Babičku“ → rozcestník,
                        # „Babička (kniha)“ → článek)
                        or ("(" in _tzv and _kryje(_tv, _tok(_tzv))
                            and _kryje(_tok(_tzv), _tv))):
                    logging.getLogger(__name__).info(
                        "HANS_WIKI_TITUL_Z_VETY_V1: předmět %r → %r (titul z holé věty)",
                        tema, _tzv)
                    tema = _tzv
                    self._presny_z_vety = _tzv
            if not tema:
                return None
            from scripts.web_reader import WebReader
            from scripts.hans_overeni import _jen_lokalne
            _wr = WebReader(self.config)
            _wr._get = _jen_lokalne        # jen lokálně: rozhoduje se u každé otázky
            titul = _wr._wikipedia_search(tema)
            self._presny_titul = titul     # HANS_LOOKUP_PRESNY_PODKLAD_V1
            if not titul or _wo.extrakt(titul)[1]:
                return None
            a, b = _tok(tema), _tok(titul)
            if not a or not b or not (_kryje(a, b) and _kryje(b, a)):
                return None
            # HANS_WIKI_DILO_ROZCESTNIK_V1 (25. 10.) — zkratka velkými („ROMEA“)
            # není slovo psané normálně („Romea a Julii“ → sdružení ROMEA)
            _hol = re.sub(r"\s*\(.*?\)", "", titul).strip()
            if (len(_hol) >= 3 and _hol.isupper()
                    and not re.sub(r"\s*\(.*?\)", "", tema).strip().isupper()):
                return None
            # HANS_LOOKUP_PRESNY_JINY_DRUH_V1 (18. 10.) — „Karlova mostu“ →
            # „Muzeum Karlova mostu“: druhové slovo se z porovnání vynechá,
            # takže článek o JINÉ věci (muzeum, pomník, nádraží…) prošel.
            # /tazatel 18. 10.: 1 z 25 přesných dohledání.
            _jiny = {"muzeum", "pomnik", "socha", "nadrazi", "stanice", "ulice",
                     "letiste", "stadion", "galerie", "divadlo", "skola", "nemocnice"}
            if (set(re.findall(r"\w+", _f(re.sub(r"\(.*?\)", "", titul)))) & _jiny) - set(
                    re.findall(r"\w+", _f(tema))):
                return None
            silny = len(a) >= 2 or any(w in self._PRESNY_DRUH for w in re.findall(r"\w{2,}", _f(tema)))
            # „kdo napsal Krakatit?“ — jednoslovný název díla s autorským
            # slovesem, jen když titul článku je PŘESNĚ to slovo (/tazatel 17. 10.)
            # HANS_WIKI_DILO_ROZCESTNIK_V1 — téma s upřesněním („Máj (Karel Hynek
            # Mácha)“) se porovná bez závorky
            if (not silny and _f(re.sub(r"\s*\(.*?\)", "", titul)) == _f(re.sub(r"\s*\(.*?\)", "", tema))
                    and re.search(r"\bkdo\s+(?:napsal|re[zž][ií]roval|slo[zž]il|nato[cč]il|"
                                  r"namaloval|zkomponoval|nazp[ií]val)\b"
                                  # HANS_LOOKUP_PRESNY_VYDANI_V1 (18. 10.) — „kdy vyšel
                                  # Krakatit?“ → z hlavy „Karel Poláček“ (živě 18. 10.)
                                  r"|\bkdy\s+(?:vy[sš]el|vy[sš]l[aoiy]|byl[aoiy]?\s+vyd[aá]n\w*|"
                                  r"vznikl\w*|m[eě]l[aoiy]?\s+premi[eé]r\w*)\b",
                                  _f(veta) + " " + _f(_prepis or ""))):
                silny = True
            return tema if silny else None
        except Exception as _e:
            logging.getLogger(__name__).debug("HANS_LOOKUP_PRESNY_V1: %s", _e)
            return None

    # HANS_LOOKUP_PRIBUZNY_V1 (24. 10.) — otázka na PŘÍBUZNÉHO osoby („co víš
    # o manželce Karla IV.?“, „kdy se narodila manželka TGM?“, „jak se jmenovala
    # Mozartova matka?“): podklad bývá o osobě samé (zápisky, četba, entita) a
    # model si příbuzného domyslí (/tazatel 24. 10.: Blanka „Lucemburská“ †1349,
    # Mozartova matka „Nannerl“ *1734, Masaryková *1878). Wikidata osoby
    # příbuzného znají → jediný s českým článkem = dohledá se JEHO článek, víc
    # (4 manželky Karla IV.) = článek osoby s blokem Wikidat, kde jsou všichni.
    _PRIBUZNY_VZTAH = [
        (r"\bmanzel(?:ka|ky|ce|ku|kou|kami|kach)\b|\bchot\w*", "P26", "f"),
        (r"\bmanzel(?:a|e|i|em|ove|u)?\b", "P26", "m"),
        (r"\bmat(?:ka|ky|ce|ku|kou)\b|\bmaminc\w*", "P25", None),
        (r"\botc(?:e|i|em)\b|\botec\b|\btatin\w*", "P22", None),
        (r"\bsyn(?:a|ovi|em|ove|u|ech|y|u|um)?\b", "P40", "m"),
        (r"\bdcer\w*", "P40", "f"),
        (r"\bdet[ietm]?\b|\bdetmi\b|\bdetech\b|\bpotom\w*", "P40", None),   # „děti s Annou Falckou“
    ]

    def _pribuzny_predmet(self, veta: str, name: str = None):
        """(téma k dohledání, popis) nebo None."""
        import unicodedata as _ud
        self._pribuzny_otazka = None

        def _f(x):
            return "".join(c for c in _ud.normalize("NFD", (x or "").lower())
                           if _ud.category(c) != "Mn")
        try:
            if not bool((self.config.get("instant_lookup", {}) or {}).get("pribuzny", True)):
                return None
            from scripts.hans_intent import pta_se_na_zazitek_hanse
            from scripts.hans_convindex import kotva_tematu
            from scripts import hans_wiki_offline as _wo
            from scripts import hans_overeni as _O
            if not _wo.dostupna() or pta_se_na_zazitek_hanse(veta):
                return None
            _kp = self.config.get("known_persons", {}) or {}
            _known = tuple(_kp.keys()) + tuple(str(v.get("nom", "")) for v in _kp.values())
            try:
                from scripts.hans_persona import persona_name as _pn
                _known += (str(_pn(self.config) or ""),)
            except Exception:
                pass
            if name:
                _known += (str(name),)
            _prepis = getattr(self, '_f1_query', None)
            for text in [x for x in (_prepis, re.sub(r"^\S+\s+se\s+pt[áa]:\s*", "", veta or "")) if x]:
                tema = kotva_tematu(text, vynech=_known)
                if not tema:
                    continue
                tf = _f(text)
                k0 = _f(tema).split()[0][:3]      # „Karel“ × „Karla“
                for rx, prop, rod in self._PRIBUZNY_VZTAH:
                    # „Jaké děti měl X a s kterými manželkami?“ se ptá na děti,
                    # manželka je jen upřesnění (živě 24. 10.: vyjmenoval manželky)
                    _deti_q = prop == "P26" and re.search(r"\bdet[ietm]\w*|\bpotom\w*", tf)
                    for m in re.finditer(rx, tf):
                        # vztah patří osobě z kotvy: za ním buď její jméno
                        # („manželka Karla IV.“), nebo žádné jméno („…Mozarta
                        # a jak se jmenovala jeho matka?“); JINÉ jméno za vztahem
                        # = vztah jiné osoby („Blanka z Valois, manželka Karla IV.“);
                        # „moje / vaše matka“ není o osobě z otázky
                        _za = text[m.end():m.end() + 40] if len(text) == len(tf) else ""
                        _cap = re.search(r"\b([A-ZÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ][\wáčďéěíňóřšťúůýž]+)",
                                         re.split(r"[?.!;]", _za)[0])
                        # „děti s Annou Falckou“ — jméno za „s/se“ je partner
                        if _cap and re.match(r"\s*(?:s|se)\s+$", _za[:_cap.start()]):
                            _cap = None
                        if _cap and not _f(_cap.group(1)).startswith(k0):
                            continue
                        # přístavek „jeho druhá manželka, Žofie Bavorská“ = vztah
                        # POPISUJE osobu z kotvy, ptá se na ni → bez skoku (živě
                        # 24. 10.: data Václava IV. místo Žofie). Za čárkou, nebo
                        # jméno přesně v 1. pádě jako kotva (genitiv „Karla“ ≠ „Karel“).
                        _appo_carka = bool(_cap and re.match(r"\s*,", _za))
                        _appo_txt = _za[_cap.start():] if _cap else ""
                        if not _cap and k0 not in tf[:m.start()]:
                            continue
                        if re.search(r"\b(?:moj|tv|vas|nas|sv)\w*\s+$|\bma\s+$"
                                     r"|\bma(?:m|me|s|te)\b[^.?!]{0,15}$", tf[:m.start()]):
                            continue
                        from scripts.web_reader import WebReader
                        _wr = WebReader(self.config)
                        _wr._get = _O._jen_lokalne
                        titul = _wr._wikipedia_search(tema)
                        if not titul or _wo.extrakt(titul)[1]:
                            return None
                        _r = _wo._cil(titul)          # přesměrování („Lenin“)
                        titul = _r[1] if _r else titul
                        if _appo_carka or (_appo_txt and _f(_appo_txt).startswith(
                                _f(re.sub(r"\s*\(.*$", "", titul)))):
                            return None      # přístavek: vztah popisuje osobu z kotvy
                        _O._nacti_wd([titul])
                        C = _O._c()
                        P = (C.get(titul) or {}).get("p") or {}
                        if not P.get("P21"):
                            return None          # není osoba
                        kand = [q for q in (P.get(prop) or []) if isinstance(q, str)
                                and (prop != "P40" or rod is None or C.get("R:" + q) == rod)]
                        if _deti_q and len(kand) != 1:
                            break        # víc manželek → ptá se na děti (níž P40)
                        if not kand:
                            return None
                        # zadání shrnutí: o kom článek je a v jakém je vztahu
                        # (živě 24. 10.: „Kdo byl otcem Karla IV.?“ nad článkem
                        # o Janu Lucemburském → „Jindřich VII.“ = otec Jana)
                        _vz = {"P26": "manželka" if rod == "f" else "manžel", "P25": "matka",
                               "P22": "otec", "P40": "syn" if rod == "m" else "dcera"}[prop]
                        _vzm = {"P26": "manželky" if rod == "f" else "manželé", "P25": "matka",
                                "P22": "otec", "P40": "děti"}[prop]
                        _q = (_prepis or re.sub(r"^\S+\s+se\s+pt[áa]:\s*", "", veta or "")).strip()
                        # jen KDO ten příbuzný je („kdo byl otcem X“, „jak se
                        # jmenovala manželka X“) → stačí článek X s řádkem Wikidat;
                        # v článku příbuzného si model spletl role (otec otce)
                        _jen_kdo = (re.search(r"\bkdo\s+(?:byl|byla|je|jsou|byli)\b|\bjak\s+se\s+"
                                              r"jmenoval\w*|\bjak\w+\s+(?:manzel|otc|matk|syn|dcer)\w*", tf)
                                    and not re.search(r"\bkdy\b|\bkde\b|\bnarod|\bzemr|\bcim\b|"
                                                      r"\bco\s+vi|\bjaky\s+byl\w*\s+zivot|\bproc\b|\bkolik\b", tf))
                        if len(kand) == 1 and C.get("S:" + kand[0]) and not _jen_kdo:
                            self._pribuzny_otazka = (
                                "%s (Článek je o osobě %s, která je %s osoby %s. Odpovídej o ní.)"
                                % (_q, C["S:" + kand[0]], _vz, titul))
                            return C["S:" + kand[0]], "%s osoby %s" % (prop, titul)
                        from scripts.hans_findings import _konkretni_otazka
                        if len(kand) == 1:
                            _vzm = _vz
                        if not _konkretni_otazka(self.config, _q):
                            _q = "Jak se %s %s osoby %s?" % (
                                "jmenovali" if _vzm == "manželé" else "jmenoval" if _vzm in ("otec", "syn", "manžel")
                                else "jmenovala" if _vzm in ("matka", "dcera", "manželka") else "jmenovaly",
                                _vzm, titul)
                        # děti: seznam Wikidat bývá neúplný (Karel IV. bez Václava
                        # 1350) → bez pokynu, ať výběr sekcí najde „Potomci“
                        self._pribuzny_otazka = (_q if prop == "P40" else
                            "%s (Odpověz podle řádku „%s“ v ověřených údajích.)"
                            % (_q, _vzm if prop == "P26" else _vz))
                        return titul, "%s osoby %s (%d)" % (prop, titul, len(kand))
            return None
        except Exception as _e:
            logging.getLogger(__name__).debug("HANS_LOOKUP_PRIBUZNY_V1: %s", _e)
            return None

    # HANS_FILM_NEVIDEL_V1 (24. 10.) — „Viděl jste (také) film Titanic?“ u filmu,
    # který Hans v deníku nemá: film_recall nic, cesta bez faktů, A1 „stabilní“
    # → zmatek („nechápem, o koho se jedná“). Zhlédnutí je JEHO zážitek → poctivě
    # z deníku: neviděl. Jen otázka na zhlédnutí + název filmu (kotva „film X“
    # nebo článek s infoboxem filmu).
    _NEVIDEL_RE = re.compile(r"\b(?:vid[eě]l|koukal|d[ií]val|sledoval|zhl[eé]dl)\w*\s+(?:jsi|jste|sis|ses)\b"
                             r"|\b(?:jsi|jste|sis|ses)\s+(?:\w+\s+){0,2}(?:vid[eě]l|koukal|d[ií]val|sledoval|zhl[eé]dl)",
                             re.I)

    def _film_nevidel(self, veta: str, name: str = None):
        self._film_videl = None        # HANS_FILM_VIDEL_ZAZNAM_V1 — jen pro tento tah
        try:
            _prepis = getattr(self, '_f1_query', None) or ""
            text = _prepis or re.sub(r"^\S+\s+se\s+pt[áa]:\s*", "", veta or "")
            if not (self._NEVIDEL_RE.search(text) or self._NEVIDEL_RE.search(veta or "")):
                return None
            if re.search(r"kamer|venku|z\s+okna|za\s+oknem|na\s+ulic", text + " " + (veta or ""), re.I):
                return None
            # HANS_FILM_VIDEL_ZAZNAM_V1 (24. 10.) — film v deníku JE („Takže jsi četl
            # Hobita… a koukal jsi i na film?“ → „nemám možnost sledovat filmy“,
            # ačkoli ho Hans týž den viděl): záznam z deníku do podkladu
            try:
                import unicodedata as _ud0
                _f0 = lambda s: "".join(ch for ch in _ud0.normalize("NFD", str(s or "").lower())
                                        if _ud0.category(ch) != "Mn")
                from scripts.hans_recall import _ro as _ro0
                _db0 = ((self.config.get("hans_idle", {}) or {}).get("diary_db") or "data/hans_diary.db")
                _c0 = _ro0(_db0)
                try:
                    _rows0 = _c0.execute(
                        "SELECT title, max(ts) FROM diary WHERE event_type='kodi_playing' "
                        "AND length(title) >= 3 GROUP BY title ORDER BY 2 DESC LIMIT 400").fetchall()
                finally:
                    _c0.close()
                # jen vlastní jména (velké písmeno uvnitř věty), ne „filmy“, „poslední“
                _caps0 = set()
                for _tx in (text, re.sub(r"^\S+\s+se\s+pt[áa]:\s*", "", veta or "")):
                    for _mm in re.finditer(r"(?<![.!?—–-]\s)(?<!^)\b([A-ZÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ][a-záčďéěíňóřšťúůýž]{3,})", _tx):
                        _caps0.add(_f0(_mm.group(1))[:5])
                _slova0 = _caps0 - {"dobry", "dekuj", "rozum", "chapu", "prosi", "zajim"}
                _shoda0 = [(ti, ts) for ti, ts in _rows0
                           if any(w[:5] in _slova0 for w in re.findall(r"\w{4,}", _f0(str(ti).split(":")[0]))
                                  if w not in ("film", "filmu", "serial"))]
                if _shoda0:
                    self._film_videl = _shoda0[:4]
            except Exception as _e0:
                logging.getLogger(__name__).debug("HANS_FILM_VIDEL_ZAZNAM_V1: %s", _e0)
            from scripts.hans_convindex import kotva_tematu
            from scripts import hans_wiki_offline as _wo
            k = kotva_tematu(text) or ""
            m = re.match(r"(?:film\w*|seri[aá]l\w*|sn[ií]mk\w*)\s+(.+)$", k, re.I)
            nazev = m.group(1).strip() if m else None
            if not nazev and k:
                from scripts.web_reader import WebReader
                from scripts.hans_overeni import _jen_lokalne
                _wr = WebReader(self.config)
                _wr._get = _jen_lokalne
                _t = _wr._wikipedia_search(k)
                # bez slova „film“ jen PŘESNÝ název článku („Paříži“ ↛ „Paříži, miluji tě“)
                if (_t and (_wo.infobox(_t)[0] or "").lower() == "film"
                        and re.sub(r"\s*\(.*$", "", _t).strip().lower() == k.strip().lower()):
                    nazev = re.sub(r"\s*\(.*$", "", _t)
            if not nazev:
                return None
            from scripts.hans_recall_filmy import _titul_po_slovech
            from scripts.hans_recall import _ro
            db = ((self.config.get("hans_idle", {}) or {}).get("diary_db") or "data/hans_diary.db")
            c = _ro(db)
            try:
                tituly = [r[0] for r in c.execute(
                    "SELECT DISTINCT title FROM diary WHERE event_type IN "
                    "('movie_opinion','kodi_playing') AND length(title) >= 3").fetchall()]
            finally:
                c.close()
            import unicodedata as _ud
            _n = lambda s: "".join(ch for ch in _ud.normalize("NFD", str(s or "").lower())
                                   if _ud.category(ch) != "Mn").strip(" .,:!?\"“„")
            _var = set()
            for x in tituly:
                _var |= {_n(x), _n(re.sub(r"\s*\(.*$", "", x)), _n(x.split(":", 1)[-1])}
            if (_n(nazev) in _var or _titul_po_slovech(text, tituly)
                    or _titul_po_slovech("film " + nazev, tituly)):
                return None          # záznam má → odpoví cesta vzpomínky
            try:
                from scripts.cz_names import vocative as _voc
                osl = (", %s" % _voc(name)) if name else ""
            except Exception:
                osl = ""
            # název ve skloněném tvaru z otázky („Titanicu“) → tvar z článku
            try:
                from scripts.web_reader import WebReader
                from scripts.hans_overeni import _jen_lokalne
                _wr = WebReader(self.config)
                _wr._get = _jen_lokalne
                _t = _wr._wikipedia_search("film " + nazev) or ""
                if _t and (_wo.infobox(_t)[0] or "").lower() == "film":
                    nazev = re.sub(r"\s*\(.*$", "", _t)
            except Exception:
                pass
            if self._film_videl:
                return None          # podobný titul v deníku je → rozhodne model se záznamem
            logging.getLogger(__name__).info(
                "HANS_FILM_NEVIDEL_V1: %r v deníku není → poctivě: neviděl", nazev)
            return ("Film „%s“ jsem podle svého deníku neviděl%s — nemám o tom, že bych ho "
                    "sledoval, žádný záznam." % (nazev, osl))
        except Exception as _e:
            logging.getLogger(__name__).debug("HANS_FILM_NEVIDEL_V1: %s", _e)
            return None

    # HANS_LOOKUP_OBSAZENI_V1 (24. 10.) — „a kdo v tom hrál?“ mimo knihovnu Kodi
    # (cesta zápisků z četby o filmu): obsazení v podkladu není a model si ho
    # vymyslel a připsal Wikipedii (Markéta Lazarová: „Jelena Matušková, Ivan
    # Plevěc…“). Film z přepisu / vlákna / předchozí otázky → jeho článek
    # (lokální Wikipedie má od 24. 10. seznamy obsazení).
    def _obsazeni_film(self, veta: str, name: str = None):
        try:
            # jen herci (režie se dohledává dobře i dnes)
            _q = (veta or "") + " " + (getattr(self, '_f1_query', None) or "")
            if not re.search(r"\bhr(?:á|a)l\w*|\bhraj\w*|\bherc\w*|\bherečk\w*|\bherack\w*|"
                             r"\bobsazen\w*|\bhlavn\w+\s+rol", _q, re.I):
                return None
            from scripts.hans_convindex import kotva_tematu
            from scripts import hans_wiki_offline as _wo
            from scripts.web_reader import WebReader
            from scripts.hans_overeni import _jen_lokalne
            _wr = WebReader(self.config)
            _wr._get = _jen_lokalne
            kand = [getattr(self, '_f1_query', None) or "", veta or ""]
            _tc = getattr(self, '_thread_ctx', None)
            if _tc:
                kand += [str(x) for x in list(_tc)[1:3] if x]
            try:
                _hist = self.conv_store.get_history(name) or []
                kand += [str(m.get("content") or "") for m in reversed(_hist)
                         if m.get("role") == "user"][:2]
            except Exception:
                pass
            for k0 in kand:
                k0 = re.sub(r"^\S+\s+se\s+pt[áa]:\s*", "", k0)
                try:
                    _tz = _wo.titul_z_vety(k0)     # i malými a bez háčků
                    if _tz and "film" in (_wo.infobox(_tz)[0] or "").lower():
                        return _tz
                except Exception:
                    pass
                k = kotva_tematu(k0) or ""
                if not k:
                    continue
                m = re.match(r"(?:film\w*|seri[aá]l\w*|sn[ií]mk\w*)\s+(.+)$", k, re.I)
                for dotaz in ([("film " + m.group(1)), m.group(1)] if m else [k]):
                    _t = _wr._wikipedia_search(dotaz)
                    if _t and not _wo.extrakt(_t)[1] and \
                            "film" in (_wo.infobox(_t)[0] or "").lower():
                        return _t
            return None
        except Exception as _e:
            logging.getLogger(__name__).debug("HANS_LOOKUP_OBSAZENI_V1: %s", _e)
            return None

    @staticmethod
    def _podklad_kryje(titul: str, podklad: str) -> bool:
        """HANS_LOOKUP_PRESNY_PODKLAD_V1 — je podklad (entita C1 / titul z Kodi)
        o témž, co přesný článek předmětu? Shoda titulu; konkrétnější entita
        („X (film, 2024)“ × článek „X“); jinak tokeny oběma směry (prefix
        o 1 znak kratší, čísla se počítají). Entita se závorkou a jiným
        základem je cizí (Midway (1976 film) × Bitva u Midway)."""
        import unicodedata as _ud
        if not titul or not podklad:
            return True     # nevíme → nic neměnit

        def _f(x):
            return "".join(c for c in _ud.normalize("NFD", (x or "").lower())
                           if _ud.category(c) != "Mn").strip()

        def _bez(x):
            return re.sub(r"\s*\(.*?\)", "", x or "").strip()

        def _tok(x):
            return re.findall(r"\d+|\w{2,}", _f(_bez(x)))

        def _kryje(a, b):
            return all(any(x[:max(4, min(len(x), len(y)) - 1)]
                           == y[:max(4, min(len(x), len(y)) - 1)] for y in b) for x in a)
        if _f(titul) == _f(podklad):
            return True
        if _f(_bez(titul)) == _f(_bez(podklad)) and "(" not in titul:
            return True
        if "(" in podklad:
            return False
        a, b = _tok(titul), _tok(podklad)
        return bool(a and b and _kryje(a, b) and _kryje(b, a))

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
        # HANS_ASK_HANS_EXPERIENCE_V1 (17. 10.) — otázka na Hansův zážitek či
        # dílo („Byl jste někdy v Paříži?“) není dotaz na fakt o světě →
        # nedohledávat (dřív film „Paříži, miluji tě“)
        try:
            from scripts.hans_intent import pta_se_na_zazitek_hanse
            if pta_se_na_zazitek_hanse(veta):
                logging.getLogger(__name__).info(
                    "HANS_ASK_HANS_EXPERIENCE_V1: otázka na můj zážitek/dílo → "
                    "nedohledávám: %.60s", veta or "")
                return None
        except Exception:
            pass
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
            # HANS_PODKLAD_PREPNUTI_V1 — téma určil společný krok podkladu
            # (Kolumbus, ne sopka Kolumbo; Shakespeare, ne K.Will)
            _pt = getattr(self, '_podklad_tema', None)
            self._podklad_tema = None   # jen pro tuto větu, ne pro další volání
            if _pt:
                if _pt != tema:
                    logging.getLogger(_h.__name__).info(
                        'HANS_PODKLAD_PREPNUTI_V1: téma dohledání %r → %r', tema, _pt)
                tema = _pt
            # HANS_WIKI_TITUL_Z_VETY_V1 — přesný titul z holé věty (určil ho
            # `_presny_predmet`) má přednost: kotva z přepisu F1 by šla na
            # „Karlov“/„Válku“, ačkoli předmět je „Karlův most“/„Válka s Mloky“
            _pz = getattr(self, '_presny_z_vety', None)
            self._presny_z_vety = None
            if _pz and _pz != tema:
                logging.getLogger(_h.__name__).info(
                    'HANS_WIKI_TITUL_Z_VETY_V1: téma dohledání %r → %r', tema, _pz)
                tema = _pz
            if not tema:
                # HANS_LOOKUP_NAVAZ_HESLO_V1 (23. 10.) — doptávka na heslo, které
                # se téže osobě právě dohledalo („kdo objevil Higgsův boson“ →
                # „a kdy ho potvrdili?“; přepis F1 bez háčků → kotva nic → A1
                # „nemám spolehlivý záznam“)
                try:
                    from scripts.hans_findings import posledni_heslo
                    tema = posledni_heslo(name or "", veta or "", _prepis or "") or None
                    if tema:
                        logging.getLogger(_h.__name__).info(
                            'HANS_LOOKUP_NAVAZ_HESLO_V1: doptávka → heslo %r', tema)
                except Exception:
                    tema = None
            if not tema:
                return None
            _dbp = (self.config.get("hans_idle", {}) or {}).get(
                "diary_db") or self.config.get("diary_db") or "data/hans_diary.db"
            _pq = getattr(self, '_pribuzny_otazka', None)   # HANS_LOOKUP_PRIBUZNY_V1
            self._pribuzny_otazka = None
            if _prepis and _f1_ztratil_sloveso(veta, _prepis):   # HANS_LOOKUP_F1_SLOVESO_V1
                logging.getLogger(_h.__name__).info(
                    "HANS_LOOKUP_F1_SLOVESO_V1: přepis %r ztratil sloveso věty %r → "
                    "shrnutí podle holé věty", _prepis, veta)
                _prepis = re.sub(r"^\S+\s+se\s+pt[áa]:\s*", "", veta or "") or _prepis
            out = lookup_now(self.config, _dbp, tema, veta or "", asker=name,
                             otazka=_pq or _prepis or veta,   # HANS_LOOKUP_QUESTION_SUMMARY_V1
                             mel_zapisky=mel_zapisky)
            logging.getLogger(_h.__name__).info(
                "HANS_ANCHOR_LOOKUP_V1: téma %r → %s", tema,
                "dohledáno" if out else "nic (platí dosavadní odpověď)")
            return out
        except Exception as e:
            logging.getLogger(_h.__name__).warning(
                "HANS_ANCHOR_LOOKUP_V1 selhalo: %s", e)
            return None

    def _dohledej_obsahem(self, veta: str, name: str = None):
        """HANS_LOOKUP_OBSAHEM_V1 (25. 10.) — obecná otázka bez vlastního jména,
        kterou kotva nedohledala: článek podle obsahu (`clanek_obsahem`), pak
        běžné dohledání. Jen místo „nemám spolehlivý záznam“ (uživatel 25. 10.).
        📏 48 otázek z 20 oborů: dobrý článek 45 (po výběru z rozcestníku,
        přesné velikosti písmen a skóre všech kandidátů; dřív 38 — Slunce u
        největší planety, SONET u sonetu, Atletika u maratonu), obecnější 1
        (jaderná elektrárna → Elektrárna), nic 3; 8 kontrolních (na Hanse,
        vázané na čas, nesmysl) bez dohledání."""
        try:
            from scripts.hans_intent import pta_se_na_zazitek_hanse
            if pta_se_na_zazitek_hanse(veta):
                return None
            from scripts.hans_findings import clanek_obsahem, lookup_now
            _prepis = getattr(self, '_f1_query', None)
            # HANS_LOOKUP_OBSAHEM_F1_SLOVO_V1 (25. 10., úspěšnost: „jaka je mena
            # v japonsku?“ → F1 „Jaká jsou jména v Japonsku?“ → Japonské jméno) —
            # přepis vstupu bez háčků, který ztratil plnovýznamové slovo věty,
            # se pro hledání podle obsahu nebere
            if _prepis and _f1_ztratil_slovo(veta, _prepis):
                logging.getLogger(_h.__name__).info(
                    "HANS_LOOKUP_OBSAHEM_F1_SLOVO_V1: přepis %r ztratil slovo věty %r → holá věta",
                    _prepis, veta)
                _prepis = None
            titul = clanek_obsahem(self.config, _prepis or veta)
            if not titul and _prepis:
                titul = clanek_obsahem(self.config, veta)
            if not titul:
                return None
            _dbp = (self.config.get("hans_idle", {}) or {}).get(
                "diary_db") or self.config.get("diary_db") or "data/hans_diary.db"
            out = lookup_now(self.config, _dbp, titul, veta or "", asker=name,
                             otazka=_prepis or veta)
            logging.getLogger(_h.__name__).info(
                "HANS_LOOKUP_OBSAHEM_V1: článek %r → %s", titul,
                "dohledáno" if out else "nic (platí přiznání)")
            return out
        except Exception as e:
            logging.getLogger(_h.__name__).warning("HANS_LOOKUP_OBSAHEM_V1 selhalo: %s", e)
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

    # HANS_F1_PREDMET_V1 (20. 10.) — přepis F1 si občas vymyslí nebo přenese
    # předmět z jiného tahu: „co je to NDR“ → „Kolik her má Divadlo Járy
    # Cimrmana?“, „a kolik her tam je?“ (o divadle) → „Kolik her má Messi?“,
    # „a smetana? kdy se narodil?“ → „…Vítězslav Nezval…“. Přepis se pak
    # zahodí (platí původní věta), když (1) otázková část věty jmenuje silný
    # předmět (přesný titul z věty, zkratka velkými) a přepis z něj nemá nic,
    # nebo (2) přepis přidá jméno, které není ve větě ani v posledním tahu
    # (otázka + odpověď), mimo jméno persony a tazatele. Hlásková shoda
    # (c=k, ae=e, ph=f, w=v, y=i), kmen 3 znaky.
    # 📏 (1) 456 dvojic věta+přepis ze stínu podkladu: zásah jen „NDR“ ×2;
    # (2) 867 přepisů z přepisů /tazatel s historií: 8 zásahů, 6 výmyslů, 2 neškodné.
    @staticmethod
    def _f1_hlasky(s) -> set:
        from scripts.hans_convindex import _fold
        s = (_fold(str(s or "")).replace("ch", "#").replace("ph", "f").replace("th", "t")
             .replace("ae", "e").replace("c", "k").replace("w", "v").replace("y", "i")
             .replace("#", "ch"))
        return {w[:3] for w in re.findall(r"\w{3,}", s)}

    def _f1_ztratil_predmet(self, veta: str, novy: str, hist=None, kdo: str = "") -> str:
        """HANS_F1_PREDMET_V1 — důvod k zahození přepisu, nebo ''."""
        try:
            from scripts import hans_wiki_offline as _wo
            from scripts.hans_podklad import otazkova_cast, kotvy_vsechny
            veta = self._bez_tazatele(veta)
            q = otazkova_cast(veta) or veta
            kn = self._f1_hlasky(novy)
            silne = set()
            _t = _wo.titul_z_vety(q) if _wo.dostupna() else None
            if _t:
                silne.add(_t)
                _r = _wo._cil(_t)
                if _r:
                    silne.add(_r[1])
            for w in re.findall(r"\b[A-ZÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ]{2,}(?:\.[A-Z]+)*\.?", q):
                if not re.fullmatch(r"[IVXLC]+\.?", w):
                    silne.add(w)
            if silne and not any(self._f1_hlasky(x) & kn for x in silne):
                return "předmět %r v přepisu chybí" % sorted(silne)[0]
            posl = ""
            if hist:
                posl = " ".join(str(m.get("content") or "") for m in list(hist)[-2:]
                                if isinstance(m, dict))
            persona = str((self.config.get("persona", {}) or {}).get("name") or "Hans")
            zn = (self._f1_hlasky(veta) | self._f1_hlasky(posl) | self._f1_hlasky(persona)
                  | self._f1_hlasky(kdo))
            nova = [k for k in kotvy_vsechny(novy)
                    if self._f1_hlasky(k) and not (self._f1_hlasky(k) & zn)]
            if nova:
                return "přidal jméno %r mimo větu i poslední tah" % nova[0]
        except Exception as e:
            logging.getLogger(_h.__name__).debug("HANS_F1_PREDMET_V1: %s", e)
        return ""

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
            # HANS_F1_ASKER_TVARY_V1 (26. 10.) — pády jména s pohyblivým „e“
            # („Marek“ → „Marka“, „Markovi“) a tvary z `known_persons` (bez
            # 5. pádu: oslovení na konci přepisu neškodí). Dřív prošlo „Jak
            # uvidí pozorovatel Marka pohybujícího se…“ nebo „Co zaujalo Marka
            # na práci Heideggera?“. Jen PŘESNÉ tvary („Markéta“ ≠ Marek).
            # 📏 1 820 přepisů z logu: 6 nově zahozeno (5 se špatným podmětem, 1 neutrální).
            if not _jmenuje and _kdo:
                _tv = set()
                _pe = re.match(r"(.*[^aeiouy])e([klrcnz])$", _kdo)
                if _pe and len(_kdo) >= 4:
                    _km = _pe.group(1) + _pe.group(2)
                    _tv |= {_km + k for k in ("a", "ovi", "em", "ova", "ovo", "ove", "ovy",
                                              "ovu", "ovym", "ovych", "uv")}
                _kp = (self.config.get("known_persons", {}) or {}).get(kdo or "") or {}
                if isinstance(_kp, dict):
                    _tv |= {_f(_kp[k]) for k in ("acc", "gen", "dat", "ins", "loc")
                            if _kp.get(k)}
                _jmenuje = any(len(x) >= 4 and re.search(
                    r"(?<![a-z])" + re.escape(x) + r"(?![a-z])", _txt) for x in _tv - {_kdo})
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
        _p = getattr(self, "_ASKER_PFX", None) or re.compile(r"^\s*\S+\s+se\s+pt[áa]:\s*")
        return _p.sub("", str(text or ""))

    def _fakta_podle_titulu(self, text: str) -> str:
        """HANS_FACTS_BY_TITLE_V1 — blok faktů z článku, jehož titul věta
        jmenuje; prázdné, když žádný. Vypínač `grounding.facts_by_title`."""
        try:
            if not bool((self.config.get("grounding", {}) or {})
                        .get("facts_by_title", True)):
                return ''
            from scripts.hans_recall import fakta_podle_titulu
            db = (self.config.get("hans_idle", {}) or {}).get(
                "diary_db", "data/hans_diary.db")
            tituly, blok = fakta_podle_titulu(db, self._bez_tazatele(text))
            if not blok:
                return ''
            logging.getLogger(_h.__name__).info(
                'HANS_FACTS_BY_TITLE_V1: %s → %d řádků faktů pro %r',
                ', '.join(tituly)[:80], blok.count('\n') + 1, str(text)[:50])
            # HANS_FACTS_WIKIDATA_V1 (19. 10.) — k výpisu faktů ověřené údaje
            # z Wikidat (data, režie, autor, hudba…); výpis je z modelu a údaje
            # často vynechá. 📏 30 otázek z paměti na údaj, který výpis nemá:
            # 3 → 28 (`data/mereni/vytah_18_10/wd_vypis.py`). Označení neutrální —
            # „(Wikidata)“ model vplétal do odpovědi. Uložená data se nemění.
            # Vypínač `grounding.facts_wikidata`.
            _wd = ''
            if bool((self.config.get("grounding", {}) or {}).get("facts_wikidata", True)):
                try:
                    from scripts.hans_findings import _wikidata_blok
                    for _t in tituly[:2]:
                        _b = _wikidata_blok(_t)
                        if _b:
                            _wd += ('\nOvěřené údaje k „%s“:\n' % _t
                                    + "\n".join("- " + r for r in _b.split("\n")[1:]))
                except Exception:
                    _wd = ''
            return ('Fakta z článku, který jsi o tom četl („%s“):\n%s%s'
                    % (', '.join(tituly), blok, _wd))
        except Exception:
            return ''

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

    def _entita_je_predmet(self, ent: dict, text: str) -> bool:
        """HANS_ENTITY_SUBJECT_SUPPORT_V1 (12. 10.) — JEDNOSLOVNÁ entita je
        oporou jen ve větě, která se točí kolem ní.

        Jedno obecné slovo se v dlouhé větě trefí náhodou: „byla nějaká
        událost, kterou bys chtěl zdůraznit?“ → pořad „Události“, „hmotný
        objekt ve vesmíru ohýbá světlo…“ → „Objektiv“. Glosa se pak stala
        oporou a vypnula abstinenci. Víceslovná jména (osoby, hrady, díla)
        takhle náhodou nesednou, těch se pravidlo netýká.
        Platí jen když jméno ve větě není doslova (jen skloněné nebo
        příbuzné slovo) a věta má víc než 3 jiná významová slova; „co víš
        o secesi?“, „Bauhaus? co to je?“ zůstávají. Čísla z korpusu jsou
        v backlogu (`FAZE5_ENTITY_12_10`). ⚠️ Odmítne i skloněné jednoslovné
        jméno v dlouhé větě („…něco o Gotice, zajímá mě kvůli…“) — pak
        rozhodnou zápisky. Vypínač `grounding.entity_subject_only`."""
        try:
            if not ((self.config.get("grounding") or {})
                    .get("entity_subject_only", True)):
                return True
            from scripts import hans_convindex as _ci
            jm = [_ci._fold(w) for w in _ci._WORD.findall(str(ent.get("name") or ""))]
            if len(jm) != 1:
                return True      # i „F. L. Věk“ nebo „LED TV“ je víceslovné jméno
            q = [_ci._fold(w) for w in _ci._WORD.findall(str(text or ""))]
            if jm[0] in q:
                return True      # jméno ve větě doslova (Ivanhoe, design)
            q = [w for w in q if len(w) >= 3 and w not in _ci._STOP]
            jine = [w for w in q if not any(
                w == j or (len(w) >= 4 and len(j) >= 4 and w[:4] == j[:4])
                for j in jm)]
            if len(jine) <= 3:
                return True
            logging.getLogger(_h.__name__).info(
                "HANS_ENTITY_SUBJECT_SUPPORT_V1: jednoslovná entita %r ve větě "
                "s %d dalšími slovy → nepočítám jako oporu (%r)",
                ent.get("name"), len(jine), str(text)[:50])
            return False
        except Exception as _e:
            logging.getLogger(_h.__name__).debug("entity subject: %s", _e)
            return True

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
            if not self._entita_je_predmet(_ent, self._bez_tazatele(text)):
                return ''
            self._c1_entita = _ent.get('name')   # HANS_LOOKUP_PRESNY_PODKLAD_V1
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
