"""Obsluhy příkazů přesunuté z `scripts/chat_commands.py` (ROZDELENI_PRIKAZU_V1).

Text funkcí je beze změny; jména původního modulu se čtou přes `_cc.` až při
volání. Registrace příkazů (`register(...)`) zůstává v původním modulu.
Import původního modulu je na KONCI souboru (kruhový import oběma směry).
"""
from __future__ import annotations

import re
import threading

def _cmd_denik(handler, name, args) -> str:
    """Spustí evening reflection v jiném threadu."""
    _hi = getattr(handler, "_hans_idle", None)
    _routine = getattr(_hi, "_routine", None) if _hi else None
    if not _routine or not hasattr(_routine, "run_evening_reflection"):
        return "Omlouvám se, večerní reflexe není dostupná."

    def _run():
        try:
            _cc._log.info("Chat command: spouštím evening reflection")
            result = _routine.run_evening_reflection()
            if result:
                _cc._log.info("Evening reflection done: %s", result[:80])
            else:
                _cc._log.warning("Evening reflection vrátil None")
        except Exception as e:
            _cc._log.error("Evening reflection failed: %s", e)

    threading.Thread(target=_run, daemon=True).start()
    return "Připravuji dnešní deník, pane. Bude to chvíli trvat."


def _cmd_zapomen(handler, name, args) -> str:
    """Smaže conversation history aktuální osoby."""
    if not name:
        return "Nevím, čí historii mám smazat."
    store = getattr(handler, "conv_store", None)
    if not store or not hasattr(store, "clear"):
        return "Conversation store není dostupný."
    try:
        store.clear(name)
        return f"Vymazal jsem naše předchozí hovory, {name}."
    except Exception as e:
        return f"Nepodařilo se smazat historii: {e}"


def _cmd_nitky(handler, name, args) -> str:
    """/nitky — výpis otevřených nitek; /nitky zavři <id>; /nitky vše."""
    import sqlite3 as _s
    cfg = getattr(handler, "config", {}) or {}
    db = cfg.get("diary_db") or "data/hans_diary.db"
    parts = (args or "").strip().split(maxsplit=1)
    cmd = parts[0].lower() if parts else ""
    rest = parts[1].strip() if len(parts) > 1 else ""

    # HANS_PRAVA_V1 (27. 9.) — nitky jiných osob jen s oprávněním.
    try:
        from scripts.hans_prava import muze as _pm
        _jen_ja = not _pm(cfg, name or "", "nitky_zajmy", o_kom="")
    except Exception:
        _jen_ja = False
    _ja = str(name or "").strip().lower()
    if cmd in _cc._NITKY_CLOSE:
        if not rest.isdigit():
            return "Uveďte id: /nitky zavři <id> (viz /nitky)."
        if _jen_ja:
            try:
                _c0 = _s.connect("file:%s?mode=ro" % db, uri=True, timeout=3.0)
                _r0 = _c0.execute("SELECT lower(person) FROM person_threads "
                                  "WHERE id=?", (int(rest),)).fetchone()
                _c0.close()
            except Exception:
                _r0 = None
            if not _r0 or _r0[0] != _ja:
                from scripts.hans_prava import ODMITNUTI
                return ODMITNUTI
        try:
            from scripts.hans_threads import ThreadStore
            ok = ThreadStore(cfg, db).close(int(rest), resolution="ručně uzavřeno")
            return ("Nitku %s jsem uzavřel, pane." % rest if ok
                    else "Tu nitku se nepodařilo uzavřít (už uzavřená?).")
        except Exception as e:
            return "Chyba při uzavírání: %s" % e

    include_closed = cmd in _cc._NITKY_ALL
    try:
        conn = _s.connect("file:%s?mode=ro" % db, uri=True, timeout=3.0)
        conn.row_factory = _s.Row
        _podm = [] if include_closed else ["status='open'"]
        _par = ()
        if _jen_ja:                     # HANS_PRAVA_V1
            _podm.append("lower(person)=?")
            _par = (_ja,)
        sql = ("SELECT id,person,topic,follow_up,status,times_surfaced "
               "FROM person_threads "
               + (("WHERE " + " AND ".join(_podm) + " ") if _podm else "")
               + "ORDER BY person, updated_ts DESC")
        rows = conn.execute(sql, _par).fetchall()
        conn.close()
    except Exception as e:
        return "Nitky nedostupné: %s" % e
    if not rows:
        return "Zatím žádné rozjeté nitky, pane."
    out = ["Rozjeté nitky%s:" % (" (vč. uzavřených)" if include_closed else "")]
    cur_person = None
    for r in rows:
        if r["person"] != cur_person:
            cur_person = r["person"]
            out.append("")
            out.append("• %s:" % cur_person)
        mark = "" if r["status"] == "open" else " [%s]" % r["status"]
        out.append("   [%d] %s%s → „%s\" (×%d)" % (
            r["id"], r["topic"], mark, r["follow_up"] or "", r["times_surfaced"]))
    out.append("")
    out.append("Uzavřít: /nitky zavři <id>  |  vše vč. uzavřených: /nitky vše")
    return _cc.NL_RUNTIME.join(out)


def _cmd_zajmy(handler, name, args) -> str:
    """/zajmy [jméno] — co kterou osobu zajímá."""
    import sqlite3 as _s
    cfg = getattr(handler, "config", {}) or {}
    db = cfg.get("diary_db") or "data/hans_diary.db"
    # HANS_ZAJMY_O_HANSOVI_V1 — „tvuj zajem“ miri na HANSE, ne na cloveka.
    try:
        _tc = getattr(handler, "_thread_ctx", None)
        _veta = str(_tc[0]) if (_tc and _tc[0]) else str(args or "")
    except Exception:
        _veta = str(args or "")
    if _veta and _cc._ZAJMY_NA_HANSE.search(_veta):
        _k = _cc._hansovy_konicky(db)
        if _k:
            return _k
    who = (args or "").strip().lower()
    # HANS_LLM_ROUTE_ARGS_V2 — `zajmy` má nl_patterns=[] → chodí sem VÝHRADNĚ
    # přes LLM router, který dává args="" → „co zajímá Janu?" vypsalo VŠECHNY
    # (doloženo 13.8. voláním handleru). Příkaz je read-only (mode=ro), takže
    # vzít původní větu z vlákna je bezpečné. Jméno rozřeší `_resolve_person`
    # (sdílený helper z hans_recall, používá ho i `videl`) — volá se s asker=None,
    # aby se NEuplatnil jeho fallback na tazatele: „jaké zájmy mají lidi doma?"
    # musí dál vypsat všechny, ne jen tazatele. „mě/mne" se dořeší zvlášť.
    if not who:
        try:
            _tc = getattr(handler, "_thread_ctx", None)
            _q = str(_tc[0]) if (_tc and _tc[0]) else ""
        except Exception:
            _q = ""
        if _q:
            try:
                from scripts.hans_recall import _resolve_person
                _p = _resolve_person(_q, cfg, None)
                if not _p and re.search(r"\bm[ěe]\b|\bmne\b", _q.lower()):
                    _p = name
                if _p:
                    who = str(_p).strip().lower()
            except Exception:
                pass
        # HANS_ZAJMY_JEN_TAZATEL_V1 (4.9.) — VÝCHOZÍ JE TAZATEL, NE VŠICHNI.
        # Doloženo testovacím rozhovorem: členka domácnosti se zeptala
        # „a co detektivky, ty čteš?" a dostala profily VŠECH ostatních
        # členů i fantomové osoby „uživatel". Ptala se přitom o SOBĚ.
        # Mezi členy domácnosti to je únik:
        # `HANS_HOUSEHOLD_PRIVACY_V1` hlídá jen CIZÍ tazatele, sem nedosáhne.
        # ⚠️ NEREVERTUJE `HANS_LLM_ROUTE_ARGS_V2` výše: ten chtěl, aby „jaké
        # zájmy mají lidi doma?" vypsalo všechny — a to dál platí. Mění se jen
        # případ, kdy věta NEURČUJE NIKOHO; tam je tazatel jediný, o kom je
        # jisté, že se na sebe ptát smí.
        if not who and not _cc._PTA_SE_NA_VSECHNY.search(_q or ""):
            who = str(name or "").strip().lower()
    # HANS_PRAVA_V1 (27. 9.) — zájmy jiných osob jen s oprávněním; výpis
    # „všech“ se bez něj zúží na tazatele.
    try:
        from scripts.hans_prava import muze as _pm, ODMITNUTI as _odm
        if who and not _pm(cfg, name or "", "nitky_zajmy", o_kom=who):
            return _odm
        if not who and not _pm(cfg, name or "", "nitky_zajmy", o_kom=""):
            who = str(name or "").strip().lower()
    except Exception:
        pass
    try:
        conn = _s.connect("file:%s?mode=ro" % db, uri=True, timeout=3.0)
        conn.row_factory = _s.Row
        if who:
            rows = conn.execute(
                "SELECT person,interest,evidence_count FROM person_interests "
                "WHERE status='active' AND person=? ORDER BY evidence_count DESC",
                (who,)).fetchall()
        else:
            rows = conn.execute(
                "SELECT person,interest,evidence_count FROM person_interests "
                "WHERE status='active' ORDER BY person, evidence_count DESC").fetchall()
        conn.close()
    except Exception as e:
        return "Zájmy nedostupné: %s" % e
    # HANS_ZAJMY_DISPLAY_NAME_V1 (9. 9.) — `person` je KLÍČ z configu
    # (malým písmem, bez diakritiky), ne jméno k vyslovení. Doloženo reprodukčním
    # rozhovorem: „O zájmech osoby **marek** zatím nic nevím." Táž třída,
    # kterou u `/videl` řeší `HANS_LAST_SEEN_NAME_V1`; `cz_names.display_name`
    # existuje, jen se tu nepoužívalo. ⚠️ U známé osoby by to vypsalo syrový
    # klíč člena domácnosti.
    def _jm(k):
        try:
            from scripts import cz_names as _czn
            return _czn.display_name(k, cfg) or k
        except Exception:
            return k
    if not rows:
        return (("O zájmech osoby %s zatím nic nevím, pane." % _jm(who)) if who
                else "Zatím neznám zájmy žádné osoby, pane.")
    out = ["Zájmy%s:" % ((" — " + _jm(who)) if who else "")]
    cur_p = None
    for r in rows:
        if r["person"] != cur_p:
            cur_p = r["person"]
            out.append("")
            out.append("• %s:" % _jm(cur_p))
        out.append("   %s (×%d)" % (r["interest"], r["evidence_count"]))
    return _cc.NL_RUNTIME.join(out)


def _cmd_vycet(handler, name, args) -> str:
    slovo = _cc._vycet_dotaz(args or "") or (args or "").strip()
    # KMEN NA 4 ZNAKY, ne 5: české skloňování mění koncovku a „mesta" se do
    # „mesto" netrefí. „hrad", „film", „mest", „knih" projdou.
    kmen = _cc._norm_veta(slovo)[:4]
    if len(kmen) < 4:
        return ""                       # příliš krátké → radši nic netvrdit
    cfg = getattr(handler, "config", {}) or {}
    db = cfg.get("diary_db", "data/hans_diary.db")
    try:
        import sqlite3 as _sq
        con = _sq.connect(db, timeout=10)
        try:
            rows = con.execute(
                "SELECT e.name, f.hodnota FROM entity_facts f "
                "JOIN entities e ON e.id = f.entity_id "
                "WHERE f.klic='je to' ORDER BY e.name").fetchall()
        finally:
            con.close()
    except Exception as e:
        _cc._log.debug("HANS_FACTS_ENUM_V1: %s", e)
        return ""
    nalez = [n for n, h in rows if kmen in _cc._norm_veta(h)]
    if not nalez:
        return ""                       # nic → propadni do běžného hovoru
    if len(nalez) > 25:
        vypis = ", ".join(nalez[:25])
        return ("V ověřených faktech jich mám %d, pane. Prvních pětadvacet: %s."
                % (len(nalez), vypis))
    return ("V ověřených faktech mám tyto, pane: %s. Můžu o nich vědět i víc "
            "z četby, tohle je jen to, co mám doložené." % ", ".join(nalez))


def _cmd_vzpominka(handler, name, args) -> str:
    from scripts.hans_recall import first_memory_answer
    out = first_memory_answer(_cc._recall_db(handler))
    return out or "Nepodařilo se mi teď nahlédnout do deníku, pane."


def _cmd_cetl(handler, name, args) -> str:
    from scripts.hans_recall import reading_answer
    q = (args or "").strip()
    # HANS_LLM_ROUTE_ARGS_V2 — LLM router předává args="" (záměrná pojistka proti
    # mutujícím podpříkazům), takže routovaný dotaz by ztratil TÉMA a spadl na
    # „poslední čtení". Příkaz je čistě ČTECÍ → původní věta se dá vzít z vlákna.
    # `reading_answer` si téma vytáhne samo (`_extract_topic`), proto celá věta.
    # Týž vzor jako `_cmd_rozhovory` (6.8.) a `_cmd_videl` (7.8.).
    if not q:
        try:
            _tc = getattr(handler, "_thread_ctx", None)
            if _tc and _tc[0]:
                q = str(_tc[0])
        except Exception:
            pass
    # HANS_BOOK_PROGRESS_ANSWER_V1 — průběh čtení z dat, ne výpisky.
    if _cc._PRUBEH_CTENI_PAT.search(q) or _cc._PRUBEH_CTENI_PAT.search(_cc._fold_diacritics(q)):
        try:
            from scripts.hans_recall import book_progress_answer
            from scripts.hans_thread import recent_turns as _rt
            _pr = book_progress_answer(
                _cc._recall_db(handler), q, [t for _r, t in _rt(handler, name)[-4:]])
            if _pr:
                _cc._log.info("HANS_BOOK_PROGRESS_ANSWER_V1: průběh čtení z dat")
                return _pr
        except Exception as _pe:
            _cc._log.debug("prubeh cteni selhal: %s", _pe)
    # HANS_COUNT_FILMS_BOOKS_V1 — „kolik knih“ chce POCET, ne posledni cteni.
    if _cc._KOLIK_RE.search(q):
        _p = _cc._pocet_knih(handler)
        if _p:
            return _p
    out = reading_answer(_cc._recall_db(handler), q, asker=name)  # HANS_READING_ASKER_V1
    return out or "Nepodařilo se mi teď nahlédnout do deníku, pane."


def _cmd_zdroje(handler, name, args) -> str:
    """Vypíše odkazy na to, co Hans četl. Deterministicky z deníku.

    Bez argumentu: posledních pár čtení. S argumentem: filtr na téma
    („zdroje vrak"). Co odkaz nemá, se přizná — nikdy se nedomýšlí.
    """
    import sqlite3 as _sq
    from datetime import datetime as _dt
    db = _cc._recall_db(handler)
    # HANS_SENSOR_SOURCE_SHARED_V1 (6.9.) — CIDLO MA PREDNOST PRED ZAPISKY.
    # Bez tohohle odpovedel prikaz na "odkud cerpas informace o pocasi?"
    # vypisem PRECTENYCH CLANKU. Pocasi je z ČHMÚ a teplota z cidel, ne
    # z cetby — a falesne popreni vlastniho zdroje je horsi nez mlceni.
    # Predikat se NEKOPIRUJE: je to tataz funkce, kterou vola i
    # `sources_answer` na LLM ceste, aby nevznikly dve pravdy.
    try:
        from scripts.hans_recall import sensor_source_answer
        _cidlo = sensor_source_answer(db, (args or ""), asker=name or "")
        if _cidlo:
            return _cidlo
    except Exception:
        pass
    # ⚠️ U NL vzorů přijde jako `args` CELÁ zpráva (parse_command vrací msg),
    # takže „odkud jsi vlastně čerpal?" by se hledalo jako téma a nic nenašlo.
    # Téma se proto tahá týmž extraktorem jako u /cetl; prázdné = vypiš poslední.
    raw = (args or "").strip()
    q = ""
    try:
        from scripts.hans_recall import _extract_topic
        q = (_extract_topic(raw) or "").strip().lower()
    except Exception:
        pass
    if not q and raw and len(raw.split()) <= 3 and "?" not in raw:
        q = raw.lower()          # slash forma: /zdroje vrak
    if not q and raw:
        q = _cc._tema_ze_zdrojoveho_dotazu(raw)
    # HANS_BOOK_ORIGIN_V1 (23. 9.) — „kde jsi ji vzal?“ o čtené KNIZE není
    # dotaz na webové zdroje. Doloženo sadou A: vypsaly se přečtené články.
    # Jen holý dotaz bez tématu a jen když vlákno jmenuje knihu z knihovny.
    if not q and raw and _cc._PUVOD_KNIHY_PAT.search(raw):
        try:
            from scripts.hans_thread import recent_turns as _rt
            from scripts.hans_recall import book_from_thread, book_origin_answer
            _kn = book_from_thread(db, [t for _r, t in _rt(handler, name)[-4:]])
            if _kn:
                _cc._log.info("HANS_BOOK_ORIGIN_V1: původ knihy %s", _kn.get("id"))
                return book_origin_answer(_kn)
        except Exception as _be:
            _cc._log.debug("puvod knihy selhal: %s", _be)
    try:
        cx = _sq.connect("file:%s?mode=ro" % db, uri=True, timeout=5.0)
        # HANS_SOURCES_ALL_WORDS_V1 (7. 10.) — VÍCESLOVNÉ TÉMA CHCE VŠECHNA SLOVA.
        # Dosud se hledalo jen podle NEJDELŠÍHO slova tématu (`_topic_stems`):
        # „hradu kost“ → „Tajemství hradu v Karpatech“, „Harry Potter…“ (čtení
        # „Hrad Kost“ s odkazem přitom v deníku je), „kvantové počítače“ →
        # článek o rakovině. Teď musí KAŽDÉ slovo (≥ 4 znaky) začínat některé
        # slovo titulu nebo textu téhož záznamu; shoda všech slov v titulu jde
        # první. Jednoslovná témata jdou původní cestou níž.
        # 📏 7 víceslovných témat: 2 správně místo špatně (hrad Kost, Icon of
        # the Seas), 1 beze změny (japonské zahrady), 1 poctivé „nemám“ místo
        # 7 cizích řádků (kvantové počítače), 3 prázdná jako dřív.
        _vice = _cc._zdroje_vsechna_slova(cx, q) if q else None
        if _vice is not None:
            rows = [(r[0], r[1], r[2]) for r in _vice][:8]
        elif q:
            # HANS_SOURCES_TOPIC_V1 (21.8.) — hledat přes PAHÝLY, ne přes holé
            # téma. `_topic_stems` (české skloňování) tu existuje odjakživa,
            # jen je /zdroje jako jediné nepoužívalo → „normalizaci" by nikdy
            # nesedlo na zapsané „normalizace" a dotaz by spadl na obecný výpis.
            try:
                from scripts.hans_recall import _topic_stems
                _stems = _topic_stems(q) or [q]
            except Exception:
                _stems = [q]
            # HANS_SOURCES_STEM_MIN_V1 (11. 9.) — PAHYL POD 5 ZNAKU JE DIVOKY
            # ZNAK. `_topic_stems` zkracuje az na 3 znaky, takze `ces`
            # (z „ceskem raji“) sedlo na „Cesta do stredu Zeme“
            # i na „nvrhuje vedec cestu k nove lecbe“ a Hans nabidl jako
            # ZDROJ studii o rakovine. `sec` (ze „secesi“) na
            # „Secret Service“. Ochrana `_predpony` brani jen shode
            # UPROSTRED slova, ne kratkemu pahylu na ZACATKU.
            # Kdyz je samo tema kratsi nez 5, bere se cele („Rip“).
            # Zmereno na 10 realnych tematech: cizi nalezy zmizi u tri,
            # „asimovovi“ i „designu“ se drzi.
            _stems = [s for s in _stems if len(s) >= 5] or [q]
            # ⚠️ Volné `LIKE %pahýl%` je pro češtinu PAST: „Říp" → pahýl
            # „říp" sedne doprostřed slova „případ" → na dotaz o Řípu vyšel
            # Retrográdní pohyb a Rozsudky soudce Ooky (změřeno při stavbě).
            # Skloňování mění KONEC slova, ne začátek → shoda musí začínat
            # na hranici slova (začátek textu nebo běžný oddělovač).
            _predpony = ("", " ", "„", "(", "\"", "\n")
            # HANS_SOURCES_BODY_V1 (9. 9.) — hledat i v `data`.
            # `web_read` má obsah v `note` (3024 řádků), ale `reading_takeaway`
            # (2209) a `study_note` (70) ho mají v `data` a `note` prázdné —
            # tělo 43 % záznamů tedy bylo pro filtr NEVIDITELNÉ. Přesně past
            # popsaná v CLAUDE.md („obsah bývá ve sloupci data, ne note").
            # 📏 Změřeno na 19 reálných tématech: +53 % shod.
            # ⚠️ SHODA V TITULU MÁ PŘEDNOST. Bez toho je to ZHORŠENÍ, ne
            # zlepšení: změřeno, že samotné rozšíření vytlačí 4–7 z osmi
            # dnešních výsledků — slabá zmínka v těle by přebila silnou shodu
            # v názvu. S přednostním řazením se špička naopak vyčistí
            # („Design" → „User experience design" místo „Skotská whisky").
            # [[corpus-measurement-misses-regressions]]
            _casti, _args = [], []          # tělo i titul
            _t_casti, _t_args = [], []      # JEN titul (pro řazení)
            for _s in _stems:
                _sl = _s.lower()
                for _p in _predpony:
                    _vzor = ("%s%%" % _sl) if _p == "" else ("%%%s%s%%" % (_p, _sl))
                    for _col in ("title", "COALESCE(note,'')",
                                 "COALESCE(data,'')"):
                        _casti.append("lower(%s) LIKE ?" % _col)
                        _args.append(_vzor)
                    _t_casti.append("lower(title) LIKE ?")
                    _t_args.append(_vzor)
            _kde = " OR ".join(_casti)
            _kde_t = " OR ".join(_t_casti)
            rows = cx.execute(
                "SELECT ts, title, source_url, "
                "COALESCE(NULLIF(note,''), data, '') FROM diary "
                "WHERE event_type IN ('web_read','reading_takeaway','study_note') "
                "AND (" + _kde + ") "
                "ORDER BY (CASE WHEN (" + _kde_t + ") THEN 0 ELSE 1 END), "
                "ts DESC LIMIT 12", _args + _t_args).fetchall()
            # HANS_TOPIC_ENTITY_AWARE_V1 (21.8.) — je téma ZNÁMÁ OSOBA? Pak
            # nestačí pahýl jména: „svobod" sedne na „Svobodné zednářství"
            # i na „svobodou projevu". U osoby se žádá CELÉ jméno (změřeno:
            # dotaz na Václava Svobodu vracel i zednářství, hymnu a Kajínka).
            try:
                from scripts.hans_recall import (tema_entita, jmeno_entity,
                                                 osoba_sedi)
                _osoba = jmeno_entity(tema_entita(q))
                if _osoba:
                    rows = [r for r in rows
                            if osoba_sedi("%s %s" % (r[1] or "", r[3] or ""),
                                          _osoba)]
            except Exception:
                pass
            rows = [(r[0], r[1], r[2]) for r in rows][:8]
        else:
            rows = cx.execute(
                "SELECT ts, title, source_url FROM diary "
                "WHERE event_type IN ('web_read','reading_takeaway','study_note') "
                "ORDER BY ts DESC LIMIT 6").fetchall()
        cx.close()
    except Exception:
        return "Nepodařilo se mi teď nahlédnout do zápisků, pane."

    if not rows and q:
        # HANS_SOURCES_STEM_MIN_V1 — po zpřísnění pahýlů se může stát, že
        # hledání v zápiscích nenajde nic, ačkoli zdroj EXISTUJE jako entita
        # („Českém ráji“ → „Český ráj“ má u sebe `source`).
        # Zkusit ji, ať se z přísnějšího filtru nestane falešné mlčení.
        try:
            from scripts.config_io import load as _cio_load
            from scripts.hans_entities import EntityStore as _ES
            _e = _ES(_cio_load(), db).resolve(q)
            _src = (_e.get("source") or "").strip() if _e else ""
            if _src:
                return ("K tomuhle mám zapsaný zdroj, pane: %s — %s"
                        % (_e.get("name"), _src))
        except Exception:
            pass

    if not rows:
        # HANS_SOURCES_TOPIC_V1 — u pojmenovaného tématu přiznat i to, co
        # z prázdného výpisu plyne: řečené na žádném zapsaném zdroji nestojí.
        return ("K tomuhle nemám v zápiscích žádné čtení, pane — co jsem "
                "o tom říkal, tedy nestojí na žádném mém zdroji."
                if q else "Zatím jsem si nic nezapsal, pane.")

    # HANS_SOURCES_DEDUP_V1 (19.8.) — týž článek má v deníku `web_read`
    # I `reading_takeaway`, takže jedno čtení vyšlo dvakrát; a když se čtení
    # opakovalo (viz HANS_CURIOSITY_COOLDOWN_PERSIST_V1), vypsal se výpis
    # třikrát tentýž řádek. Doloženo 19.8.: 3× „Třetí skoba pro Kocoura"
    # v obou sekcích. Dedup na (titul, url), nejnovější výskyt vyhrává.
    # HANS_SOURCES_DEDUP_V2 — dedup na (titul, url) NESTAČIL: týž článek má
    # `web_read` S odkazem i `reading_takeaway` BEZ něj, takže vyšel v OBOU
    # sekcích naráz („mám odkaz" i „odkaz jsem si neuložil" o tomtéž).
    # Klíč je proto SAMOTNÝ TITUL a vyhrává výskyt S ODKAZEM.
    _best = {}
    for ts, title, url in rows:
        t = str(title or "")[:70]
        k = t.lower()
        prev = _best.get(k)
        if prev is None or (url and not prev[2]):
            _best[k] = (ts, t, url)
    s_url, s_bez = [], []
    for ts, t, url in sorted(_best.values(), key=lambda x: -x[0]):
        d = _dt.fromtimestamp(ts).strftime("%d.%m.")
        (s_url if url else s_bez).append((d, t, url))

    out = []
    if s_url:
        # HANS_SOURCES_TOPIC_V1 — „Četl jsem tohle" u tématického dotazu
        # tvrdí PŘÍČINU (odtud to mám), kterou Hans vědět nemůže: generace
        # si původ nenese. U tématu se proto tvrdí jen fakt — tohle čtení
        # k tématu mám zapsané. Bez tématu je původní znění v pořádku.
        out.append("K tomuhle mám v zápiscích tohle čtení, pane:" if q
                   else "Četl jsem tohle, pane:")
        for d, t, u in s_url:
            out.append("• %s %s — %s" % (d, t, u))
    if s_bez:
        if s_url:
            out.append("")
        out.append("U tohohle mám zápisek, ale odkaz jsem si tehdy neuložil "
                   "(ukládám ho až od 12. srpna) — nerad bych ho domýšlel:")
        for d, t, _ in s_bez:
            out.append("• %s %s" % (d, t))
    return "\n".join(out)


def _cmd_videl(handler, name, args) -> str:
    cfg = getattr(handler, "config", {}) or {}
    from scripts.hans_recall import last_seen_answer
    q = args or ""
    # HANS_LLM_ROUTE_SUBJECT_V1 (7.8.) — když příkaz vybral LLM router, args
    # jsou PRÁZDNÉ schválně (aby nemohl spustit mutující podpříkaz). Tady tím
    # ale zmizí OSOBA, na kterou se uživatel ptá, a `_resolve_person` spadne
    # na tazatele → Hans odpoví o někom jiném, a sebejistě.
    # Doloženo 7.8. 11:29: „<jméno> doma neni?" → router vybral `videl`
    # s args='' → „Naposledy jsem VÁS viděl ve čtvrtek…". Totéž u „kdy jsi
    # viděl <jméno>?" — dotaz na jinou osobu odpoví o tazateli.
    # Řešení je TÝŽ vzorec, jaký už 6.8. dostal `_cmd_rozhovory` (tehdy
    # „co delal Kolac?" → sumář rozhovoru s tazatelem) — jen sem nebyl
    # protažen. Příkaz je čistě ČTECÍ, takže vzít původní větu je bezpečné.
    # ⚠️ NEDĚLAT plošně: `smer`, `studium`, `seznam`, `zdravi`, `nitky`
    # a `kalendar` mají mutující podpříkazy a prázdné args je před nimi chrání.
    # HANS_VIDEL_KOHO_V1 (9. 9.) — „KOHO jste dnes videl?" NEMA PODMET, takze
    # pad na tazatele je u ni vzdy spatne: doloženo naživo „V deníku nemám
    # žádný záznam, že bych VÁS viděl" na otázku mířenou na kohokoli.
    # Je to jina otazka nez „kdy jsi videl X" — odpovida se seznamem lidi,
    # ktere Hans dnes videl, PRES TYZ privacy gate jako `/dnes`.
    # ⚠️ MUSI byt PRED padem na `_thread_ctx` nize: ten do `q` vlozi CELOU
    # VETU (ne jmeno), takze pozdeji uz `not q` nikdy neplati a vetev by byla
    # mrtva. Doloženo 9. 9. — prvni pokus presne takhle nesepnul.
    # HANS_VIDEL_KOHO_V2 (9. 9.) — V1 klíčovala na SLOVO „koho" a byla tím
    # příliš úzká: „vidíte teď někoho ve svém okolí?" je TÁŽ otázka a spadla
    # zpět na tazatele („nemám záznam, že bych VÁS viděl"). Doloženo
    # reprodukčním rozhovorem 9. 9. (tah 2 × 3 si přímo protiřečí) —
    # to je třída C ze `ROZHOVORY_08_09`. 📏 Změřeno: V1 pokryla 2 z 9
    # přirozených formulací.
    # Rozhoduje se proto podle toho, KOHO věta určuje, ne jakým slovem:
    #   1. míří na tazatele (mě|mne|nás|mi|nám) → o tazateli,
    #   2. jmenuje osobu (i z vlákna) → o té osobě,
    #   3. jinak nemá podmět → koho jsem dnes viděl (přes týž privacy gate).
    # Simulace PŘED zásahem: 10/10 včetně navazujícího „a kdy naposledy?".
    _syrova = (args or "").strip() or _cc._route_msg() or ""
    _miri_na_tazatele = bool(_cc._NA_TAZATELE.search(_syrova))
    _kdo_ve_vete = None
    if not _miri_na_tazatele:
        try:
            from scripts.hans_recall import _resolve_person as _rp
            # ⚠️ asker=None SCHVÁLNĚ — s tazatelem by `_resolve_person`
            # spadl zpět na něj a vetev by nikdy nesepnula.
            _kdo_ve_vete = _rp(_syrova, cfg, None)
            if not _kdo_ve_vete:
                _tc0 = getattr(handler, "_thread_ctx", None)
                if _tc0 and _tc0[0]:
                    _kdo_ve_vete = _rp(str(_tc0[0]), cfg, None)
        except Exception:
            _kdo_ve_vete = None
    if not _miri_na_tazatele and not _kdo_ve_vete:
        from scripts.hans_recall import day_facts, day_fact_lines
        _f = day_facts(_cc._recall_db(handler))
        _l = day_fact_lines(_f, cfg, asker=name)
        # ⚠️ Odmítnutí se hlasovým krokem NEPOUŠTÍ — je to závazná věta
        # o soukromí, ne fakt k převyprávění; model by ji odvedl jinam.
        for _r in _l:
            if _r == _cc._PRIVACY_REFUSAL_TXT():
                return _r
        _lide = _f.get("people") or []
        if not _lide:
            return "Dnes jsem v domě nikoho neviděl, pane."
        # HANS_HLAS_NAD_FAKTY_V1 — deterministická věta se SPRÁVNÝM PÁDEM
        # (nález uživatele 9. 9.: „viděl jsem: paní <Jméno>" — 1. pád po
        # slovese, které žádá 4.). `cz_names.acc` už tvary má z configu,
        # nic se tu nevymýšlí — týž zdroj jako `last_seen_answer`.
        import time as _t
        from scripts.cz_names import acc as _acc, person_gender as _rod
        _c = []
        for _n, _t0, _t1 in _lide:
            try:
                _osl = {"žena": "paní ", "muž": "pana "}.get(_rod(_n, cfg), "")
                _jm = _osl + _acc(_n, cfg)
            except Exception:
                _jm = _n
            _od, _do = (_t.strftime("%H:%M", _t.localtime(_t0)),
                        _t.strftime("%H:%M", _t.localtime(_t1)))
            _c.append("%s (%s–%s)" % (_jm, _od, _do) if _t1 - _t0 > 300
                      else "%s (%s)" % (_jm, _od))
        _veta = "Dnes jsem v domě viděl " + ", ".join(_c) + "."
        _hlas = _cc._hlas_nad_fakty(
            cfg, [_veta],
            "Pán domu se ptá, koho jsi dnes v domě viděl. Odpověz JEDINOU "
            "větou v první osobě, svým hlasem. Jména i časy zachovej "
            "přesně, nikoho nepřidávej a nic dalšího nekomentuj.",
            "FAKTA — koho jsem dnes viděl:" + _cc.NL_RUNTIME,
            min_len=25, timeout=45)
        return _hlas or _veta
    # HANS_LLM_ROUTE_SUBJECT_V1 (7. 8.) — pád na větu z vlákna, aby se
    # nezratila OSOBA, na kterou se člověk ptá. Běží AŽ ZA koho-větví:
    # `_thread_ctx[0]` je celá věta, takže by ji jinak zneviditelnil.
    if not q:
        try:
            _tc = getattr(handler, "_thread_ctx", None)
            if _tc and _tc[0]:
                q = str(_tc[0])
        except Exception:
            pass
    out = last_seen_answer(_cc._recall_db(handler), cfg, q, name)
    return out or "Nepodařilo se mi teď nahlédnout do deníku, pane."


# ─── /dnes — co se dnes dělo v domě (HANS_DAY_AT_HOME_V1) ───────────────────
def _cmd_dnes(handler, name, args) -> str:
    """Shrnutí dneška z deníku Hansovým hlasem. Fakta deterministicky,
    hlas je jen formuluje; bez mozku se vypíšou fakta holá."""
    cfg = getattr(handler, "config", {}) or {}
    from scripts.hans_recall import day_facts, day_fact_lines
    f = day_facts(_cc._recall_db(handler))
    # HANS_DAY_FACTS_PRIVACY_V1 (9. 9.) — tazatel MUSI dovnitr. Bez nej vypsal
    # `/dnes` cizimu clovekovi jmeno i casy pritomnosti clena domacnosti
    # (ctvrty zdroj tehoz uniku, ktery se 8. 9. zaviral na trech mistech).
    lines = day_fact_lines(f, cfg, asker=name)
    if not f.get("n_events"):
        return "K dnešku nemám v deníku zatím žádný záznam, pane."
    plain = _cc.NL_RUNTIME.join("• " + l for l in lines)

    # Bez mozku (herní mód / PC dole) se fakta vrátí HOLÁ — deferral-safe,
    # vzor `_night_reflection` → statistika. Kontrolu `brain_available` dělá
    # `_hlas_nad_fakty` (vrátí '' a spadne se na `plain` níž).
    # HANS_DNES_NO_PERSON_VACUUM_V1 (9. 9.) — pokyn „kdo tu byl a kdy" žádal
    # OSOBU i tehdy, když ji fakta neobsahují (cizí tazatel ji nedostane, viz
    # HANS_DAY_FACTS_PRIVACY_V1) — a model ji poslušně VYMYSLEL:
    # „Pan Nes se zdržoval zde od 10:03 do 10:15."
    # 🔴 A ten čas byl DOSLOVA PŘÍKLAD Z TÉHOŽ PROMPTU („10:03–10:15"
    # u HANS_DAY_AT_HOME_EXACT_V1) — ilustrace se propsala do odpovědi jako
    # obsah. Když jsou fakta úplná, model příklad neopíše; kopíruje ho až
    # do PRÁZDNA. Rubrika o lidech se proto zadá jen tehdy, když o lidech
    # nějaký fakt opravdu je. [[prompt-category-invites-confabulation]]
    # ⚠️ Příklad časů z pokynu ZMIZEL — je v `_hlas_nad_fakty` bez konkrétního
    # údaje, právě aby nebylo co opsat.
    _ma_osoby = any(l.startswith("V domě jsem dnes viděl") for l in lines)
    _pokyn = ("Pán domu se ptá, co se dnes v domě dělo. Odpověz souvisle "
              "(3-5 vět, první osoba, tvým hlasem) — "
              + ("kdo tu byl a kdy, " if _ma_osoby else "")
              + "co běželo na televizi, co stálo za zmínku. "
              + ("" if _ma_osoby else
                 "O LIDECH v domě nepiš vůbec nic — žádná jména, žádné časy "
                 "příchodu; ve faktech o nich nic není a nesmíš si je "
                 "domýšlet. ")
              + "Nic nedomýšlej o důvodech.")
    _hlas = _cc._hlas_nad_fakty(cfg, lines, _pokyn,
                            "FAKTA DNEŠNÍHO DNE:" + _cc.NL_RUNTIME)
    if _hlas:
        return _hlas
    return "Dnešek podle mého deníku, pane:" + _cc.NL_RUNTIME + plain


def _cmd_rozhovory(handler, name, args) -> str:  # HANS_CHAT_SUMMARY_V1
    """Sumář toho, o čem se TAZATEL s Hansem bavil (deterministicky z deníku).
    Časová reference v dotazu („v pátek", „27. dubna 2026", „minulý týden")
    zúží období; bez ní = poslední den, kdy spolu mluvili. Delší období →
    témata; „připomeň rozhovor o X" → doslovné vybavení té výměny."""
    from scripts.hans_recall import (chat_summary, topic_conversation,
                                     _extract_conv_topic)
    cfg = getattr(handler, "config", {}) or {}
    q = args or ""
    # HANS_THREAD_V1 — když příkaz vybral LLM router, args jsou PRÁZDNÉ
    # (`resolve_command_llm` vrací `(cid, "")` schválně, aby se nespustil
    # mutující podpříkaz). Sumář rozhovorů tím ale ztratí celý dotaz a vždy
    # spadne na „poslední den" — doloženo živě 6.8.: „co delal Kolac?"
    # vrátilo sumář rozhovoru s TAZATELEM. Tenhle příkaz je čistě ČTECÍ,
    # takže původní věta se dá bezpečně vzít zpět z vlákna.
    if not q:
        try:
            _tc = getattr(handler, "_thread_ctx", None)
            if _tc and _tc[0]:
                q = str(_tc[0])
        except Exception:
            pass
    # HANS_THREAD_V1 — dotaz může mířit na rozhovor s TŘETÍ stranou (Koláč).
    # chat_summary umí jen `human_chat` (tazatel↔Hans) a `hans_recall`
    # dokonce vyřazuje `teddy_dialog` jako šum (_DIARY_NOISE) → vrátit místo
    # toho sumář JINÉHO rozhovoru je horší než přiznat, že to zatím neumím.
    # Doloženo 5.8. 19:23. Odpadne s vrstvou B (FTS nad všemi rozhovory).
    try:
        from scripts.hans_thread import third_party_scope, recent_turns
        # Vlákno je součást vstupu: „jste se o TOM bavili" neřekne, s KÝM —
        # to ví jen předchozí replika (doloženo živě 6.8.).
        _turns = recent_turns(handler, name)
        _tp = third_party_scope(q, cfg, turns=_turns)
        if _tp and _tp != "?":
            # HANS_CONVINDEX_V1 (6.8.) — hledej v ROZHOVORECH S KOLÁČEM
            # (`teddy_dialog`). Do 6.8. to nešlo vůbec: `hans_recall` ten
            # typ vyřazuje jako šum (_DIARY_NOISE) a `chat_summary` umí jen
            # `human_chat` → na „myslel jsem rozhovor s Kolacem" vracel
            # sumář rozhovoru s TAZATELEM (doloženo 5.8. 19:23).
            from scripts.hans_convindex import answer_about, topic_tokens
            # Hledej ROZŘEŠENOU větou — „v jakem kontextu jste se o tom
            # bavili" sama žádné téma nenese, to je v předchozí replice.
            _sq = q
            try:
                _tc = getattr(handler, "_thread_ctx", None)
                if _tc and _tc[2]:
                    _sq = "%s %s" % (q, _tc[2])
            except Exception:
                pass
            # Bez TÉMATU se nehledá: „co dělal Koláč?" je dotaz na STAV, ne
            # na rozhovor (jinak FTS vrátí náhodné staré dialogy — jméno je
            # v každém z nich, doloženo živě 6.8.). Odpoví na to ale TÁŽ
            # funkce jako agentní akce `kolac_status`, ne sumář rozhovoru
            # s tazatelem — jinak by dotaz na Koláče končil u výpisu „spolu
            # jsme vedli N výměn" (vzor HANS_UNIFY_ACTIONS_V1: jeden kód).
            if not topic_tokens(_sq, exclude=(_tp,)):
                from scripts.hans_agent import _run_kolac_status
                _st = _run_kolac_status(handler, {})
                if _st:
                    return _st
            if topic_tokens(_sq, exclude=(_tp,)):
                _hits = answer_about(_sq, source="teddy_dialog")
                if _hits:
                    # Jméno v 1. pádu — `cz_names` instrumentál neumí.
                    return ("%s a já jsme se o tom bavili, pane. Tady je, co "
                            "mám zapsáno:\n\n%s" % (_tp, _hits))
                return ("%s a já spolu rozprávíme, ale k tomuhle nemám "
                        "zapsaný žádný náš rozhovor, pane — a nebudu si ho "
                        "vymýšlet." % _tp)
    except Exception:
        pass
    # HANS_PRAVA_V1 (27. 9.) — „o čem jsi mluvil s <jiná známá osoba>?“
    # Dřív se vrátil rozhovor TAZATELE (špatná odpověď); teď rozhovor té
    # osoby, smí-li tazatel do cizích rozhovorů, jinak odmítnutí.
    try:
        from scripts.hans_prava import zapnuto as _pz
        _jiny = _cc._partner_rozhovoru(q, cfg, name) if _pz(cfg) else ""
    except Exception:
        _jiny = ""
    if _jiny:   # vypnutá pravidla = beze změny (dřívější chování)
        from scripts.hans_prava import muze as _pm, ODMITNUTI as _odm
        if not _pm(cfg, name or "", "cizi_rozhovory", o_kom=_jiny):
            return _odm
        try:
            from scripts.cz_names import display_name as _dn
            _jm = _dn(_jiny, cfg) or _jiny
        except Exception:
            _jm = _jiny
        _o = chat_summary(_cc._recall_db(handler), _jiny, q, config=cfg)
        return ("Rozhovory s osobou %s:\n%s" % (_jm, _o)) if _o else (
            "S osobou %s nemám zapsaný žádný rozhovor." % _jm)
    topic = _extract_conv_topic(q)
    if topic:
        out = topic_conversation(_cc._recall_db(handler), name, topic)
        if out:
            return out
    out = chat_summary(_cc._recall_db(handler), name, q, config=cfg)
    return out or "Nepodařilo se mi teď nahlédnout do deníku, pane."


def _cmd_jakto(handler, name, args) -> str:
    t = (args or "").strip()
    if not t or not _cc._HOWTO_PAT.search(t):
        return ""
    try:
        from scripts.hans_capabilities import capability_for
        popis = capability_for(t)
    except Exception as e:
        _cc._log.debug("HANS_CAP_HOWTO_V1: %s", e)
        return ""
    return ("%s, pane." % popis.rstrip(" .")) if popis else ""

# ROZDELENI_PRIKAZU_V1 — až na konci, viz hlavička
from scripts import chat_commands as _cc  # noqa: E402
