"""Obsluhy příkazů přesunuté z `scripts/chat_commands.py` (ROZDELENI_PRIKAZU_V1).

Text funkcí je beze změny; jména původního modulu se čtou přes `_cc.` až při
volání. Registrace příkazů (`register(...)`) zůstává v původním modulu.
Import původního modulu je na KONCI souboru (kruhový import oběma směry).
"""
from __future__ import annotations


# ─── /art — Hans namaluje obraz k aktuální knize (HANS_ART_V1) ────────────
def _cmd_art(handler, name, args) -> str:
    """/art [název knihy] — Hans hned namaluje obraz k (zadané/aktuální) knize.
    Render běží na pozadí (chat odpoví hned), VRAM orchestrace uvnitř."""
    import threading as _t
    cfg = getattr(handler, "config", {}) or {}
    db = cfg.get("diary_db") or "data/hans_diary.db"
    title = (args or "").strip()
    try:
        from scripts import hans_art
    except Exception as e:
        return "Malování není dostupné: %s" % e
    # HANS_ART_UNREAD_WISHLIST_V1 — nečtenou knihu nemaluj naslepo; přiznej + přidej k přečtení
    if title and not hans_art.book_is_read(db, title):
        res = hans_art.add_to_wishlist(db, title)
        if res == "exists":
            return ("Knihu „%s\" jsem ještě nečetl, pane — a už ji mám na seznamu "
                    "k přečtení. Až ji poznám, rád k ní namaluji obraz." % title)
        return ("Přiznám se, pane — knihu „%s\" jsem ještě nečetl, takže bych jen "
                "hádal, oč v ní jde. Přidal jsem si ji na seznam k přečtení (nízká "
                "priorita); až ji přečtu, rád k ní namaluji obraz." % title)

    if not hans_art.comfy_available(cfg):
        return ("Bohužel, pane — výtvarná dílna (ComfyUI na PC) teď neběží, "
                "tak nemohu malovat. Zkuste to, až bude PC vzhůru.")
    book = title or hans_art._current_book_title(db)

    def _worker():
        try:
            hans_art.render_now(cfg, db, title)
        except Exception as _e:
            _cc._log.warning("/art render selhal: %s", _e)
    _t.Thread(target=_worker, daemon=True).start()
    return ("Dám se do toho, pane — maluji obraz inspirovaný knihou „%s\". "
            "Za chvíli se objeví na nástěnce (Co Hans namaloval). "
            "Chat může být asi minutu zaneprázdněný." % book)


# ─── namaluj/nakresli <téma> — obraz na LIBOVOLNÉ téma (HANS_CAPABILITY_AWARENESS_V1) ──
def _cmd_namaluj(handler, name, args) -> str:
    """namaluj/nakresli <téma> — Hans namaluje obraz na libovolné téma, nebo
    dojem z nedávného rozhovoru (když téma neurčíš). Render na pozadí."""
    import threading as _t
    import re as _re
    cfg = getattr(handler, "config", {}) or {}
    db = (cfg.get("diary_db")
          or (cfg.get("hans_idle", {}) or {}).get("diary_db")
          or "data/hans_diary.db")
    try:
        from scripts import hans_art
    except Exception as e:
        return "Malování není dostupné: %s" % e

    # HANS_ART_SELF_V1 — „namaluj sebe / svůj avatar / jak vypadáš" = Hans maluje
    # SÁM SEBE (ne uživatele!). Musí PŘED strip sloves (ten „se" spolkne jako
    # spojku) i před distill (ten „mě=tazatel" by „sebe" zmapoval na uživatele).
    _raw = (args or "").lower()
    if _re.search(r"\bsebe\b|s[áa]m\s+sebe|\bsv[ůu][jě]?\s+avatar|sv[ée]ho\s+avatar"
                  r"|jak\s+(ty\s+)?vypad[áa]|autoportr[ée]t|namaluj\s+se\b|"
                  r"nakresli\s+se\b", _raw):
        _full = bool(_re.search(
            r"post(av|avu|avě|avou)|cel(ou|é|ého)\s*(t[ěe]lo|postav)?|"
            r"full\s*body|od\s+hlavy", _raw))
        _style_self = ""
        _ss = _re.search(
            r"(?i)(?:ve?\s+stylu|stylem|jako\s+od|po\s+vzoru)\s+(.+)$", _raw)
        if _ss:
            _style_self = _ss.group(1).strip(" ?.!,")

        def _self_render():
            try:
                r = hans_art.paint_self(cfg, db, full_figure=_full,
                                        style=_style_self)
                _cc._log.info("namaluj SEBE (full=%s) → %s", _full,
                          "ok" if r else "nevyšlo")
            except Exception as _e:
                _cc._log.warning("paint_self: %s", _e)
        _t.Thread(target=_self_render, daemon=True).start()
        return ("Namaluji sám sebe, pane — %s ze své avatarové podoby. Chvíli "
                "to potrvá, pak se podívej do galerie." %
                ("celou postavu" if _full else "podobiznu"))

    # HANS_ART_TV_V1 — „namaluj co dávají v TV / co běží / co hraje" → ŽIVÝ Kodi
    # stav (ne konverzace!). Namaluje aktuálně běžící pořad/film.
    if _re.search(r"co\s+(d[áa]v|b[ěe][žz]|hraj|je)\w*\s+(pr[áa]v[ěe]\s+)?"
                  r"(v\s+)?(tv|telev|kin[eě]|obrazovc)|"
                  r"co\s+(se\s+)?(pr[áa]v[ěe]\s+)?(hraje|d[áa]v[áa]|b[ěe][žz][íi]|"
                  r"koukám|d[íi]v[áa]m)|(film|po[řr]ad|seri[áa]l)\s+co\s+(hraje|"
                  r"b[ěe][žz]|d[áa]v)", _raw):
        try:
            from scripts.kodi_client import KodiClient
            _np = KodiClient(cfg).get_now_playing()
        except Exception as _ke:
            _cc._log.debug("namaluj TV kodi: %s", _ke)
            _np = None
        if _np and (_np.get("title") or _np.get("label")):
            # HANS_ART_TV_GROUNDING_V1 — námět z POPISU děje (Kodi plot), při
            # chybějícím popisku dohledej na internetu; teprve pak jen název.
            _disp = (_np.get("title") or _np.get("label") or "").split(",")[0].strip()
            try:
                _subj, _src = hans_art.tv_paint_subject(cfg, db, _np)
            except Exception as _te:
                _cc._log.debug("tv_paint_subject: %s", _te)
                _subj, _src = _disp, "jen podle názvu"
            _t.Thread(target=lambda: hans_art.paint_subject(cfg, db, _subj,
                                                            zadal=name or ""),
                      daemon=True).start()
            _cc._log.info("namaluj CO V TV → '%s' (%s)", _subj[:60], _src)
            _note = {"z popisu pořadu": "podle jeho děje",
                     "z internetu (popisek u pořadu chyběl)":
                        "u pořadu chyběl popisek, tak jsem si děj dohledal na internetu",
                     "jen podle názvu":
                        "popisek chyběl a nedohledal jsem víc, takže jen podle názvu"
                     }.get(_src, "")
            return ("Namaluji, co právě běží na obrazovce, pane — „%s“%s. Chvíli "
                    "to potrvá, pak se podívej do galerie." %
                    (_disp, (" (%s)" % _note) if _note else ""))
        return ("V tuto chvíli na televizi nic nehraje, pane — nemám co "
                "namalovat z obrazovky.")

    # HANS_ART_HOME_ROUTE_V1 — „namaluj (svůj/můj/náš) domov / dům / byt / kde
    # bydlím" = Hans maluje SVŮJ obývák z modelu místa (place_facts, pohled z
    # jeho vantage pointu) přes paint_home, NE generický paint_subject — ten
    # „domov" mis-groundne na entitu „Kde domov můj?" (hymna) a maluje osobu
    # (smyšlenou osobu), navíc person-render timeoutuje (doloženo 30.7.).
    if _re.search(r"\b(sv[ůu]j|m[ůu]j|n[áa][šs])\s+(domov|d[ůu]m|byt)\b"
                  r"|\bdomov\b|\bkde\s+(bydl|[žz]ij)", _raw):
        def _home_render():
            try:
                r = hans_art.paint_home(cfg, db)
                _cc._log.info("namaluj DOMOV → %s", "ok" if r else "nevyšlo/odloženo")
            except Exception as _e:
                _cc._log.warning("paint_home: %s", _e)
        _t.Thread(target=_home_render, daemon=True).start()
        return ("Namaluji svůj domov, pane — obývák z místa, kde stojím. Chvíli "
                "to potrvá, pak se podívej do galerie.")

    # vytáhni téma z požadavku (odřízni sloveso a spojky)
    subj = (args or "").strip()
    # \w* za kmenem slovesa pokryje ČASOVANÉ tvary: „namaluješ/namaluje/namaloval
    # bys/nakreslíš" (dřív se ořízl jen „namaluj" → zbylo „eš o tom obraz").
    subj = _re.sub(r"(?i)^\s*(prosím\s+|můžeš\s+|mohl\s+bys\s+|nemohl\s+bys\s+)?"
                   r"(mi\s+)?(namaluj\w*|namalovat|namaloval\w*|nakresl\w*|"
                   r"vytvoř\w*|přemaluj\w*|překresl\w*)"
                   r"(\s+bys?|\s+byste)?"
                   r"\s*(mi\s+)?(prosím\s+)?(obraz|obrázek)?\s*"
                   r"(o\s+|s\s+|se\s+|na\s+t[eé]ma\s+|ohledně\s+|podle\s+|"
                   r"toho\s+jak\s+)?", "", subj).strip(" ?.!,")

    # HANS_ART_SUBJECT_MIDSENTENCE_V1 (8. 9.) — sloveso malování UPROSTŘED věty.
    # Regex výše je ukotvený na `^`, takže odřízne jen „namaluj X". Když ale
    # žádost začne jinak („Zmínil jste malování — dokázal byste NAMALOVAT
    # obraz hradu Karlštejn"), zůstane jako námět CELÁ VĚTA. Doloženo 8. 9.
    # a není to ojedinělé: **16 z 265 obrazů** má v názvu instrukční šum
    # („a_ted_namaluj_jean_luc_picard", „supr_muzes_namalovat_obraz_…").
    # ⚠️ Samotný render tím netrpěl — `hans_art` si námět vytáhne znovu
    # („místo 'Karlštejn' nese scénu"). Špatně byl NÁZEV souboru a hláška,
    # kterou vidí uživatel („maluji obraz na téma <celá věta>").
    # Uplatní se JEN když kotva na začátku nesedla (subj se nezměnil), takže
    # dnes fungující tvary („namaluj kočku") zůstávají nedotčené.
    if subj == (args or "").strip().strip(" ?.!,"):
        _mid = _re.sub(
            r"(?i).*\b(?:namaluj\w*|namalovat|namaloval\w*|nakresl\w*|"
            r"vytvoř\w*|přemaluj\w*|překresl\w*)(?:\s+bys?|\s+byste)?"
            r"\s*(?:mi\s+)?(?:prosím\s+)?(?:obraz|obrázek)?\s*"
            r"(?:o\s+|s\s+|se\s+|na\s+t[eé]ma\s+|ohledně\s+)?",
            "", subj).strip(" ?.!,")
        if _mid and _mid != subj:
            _cc._log.info("HANS_ART_SUBJECT_MIDSENTENCE_V1: námět %r → %r",
                      subj[:60], _mid[:60])
            subj = _mid

    # HANS_ART_STYLE_V4 — odděl STYL od námětu („X ve stylu Y" / „stylem Y")
    style = ""
    _sm = _re.search(
        r"(?i)[\s,]+(?:ve?\s+stylu|stylem|jako\s+od|po\s+vzoru|[àa]\s+la)\s+(.+)$",
        subj)
    if _sm:
        style = _sm.group(1).strip(" ?.!,")
        subj = subj[:_sm.start()].strip(" ?.!,")

    # odkaz na rozhovor → sestav téma z posledních uživatelských zpráv
    if not subj or _re.search(r"(?i)bavili|mluvili|povídali|rozhovor|o\s+čem", subj):
        try:
            # HANS_CHAT_CHANNEL_AWARE_V1 — paint kontext JEN z tohoto kanálu.
            # Bug 3 (18.7.): „zkus to znova" ve web chatu vzalo Rimmera z
            # Telegram konverzace 7 min zpět. Cross-channel history je leak.
            _ch = _cc._current_channel()
            hist = (handler.conv_store.get_history_scoped(name, _ch)
                    if _ch else handler.conv_store.get_history(name)) or []
            ux = [m["content"] for m in hist if m.get("role") == "user"][-4:]
            ux = [u for u in ux if not u.strip().lower().startswith(
                ("namaluj", "nakresli", "vytvoř"))]
            if ux:
                subj = "náš rozhovor: " + " • ".join(u[:80] for u in ux[-3:])
        except Exception:
            pass
    if not subj:
        subj = "dojem z našeho nedávného rozhovoru"

    # HANS_ART_SUBJECT_DISTILL_V1 — messy požadavek / odkaz na rozhovor
    # („tu kočku", „zkus znovu", „o čem jsme se bavili") → destiluj JEDEN
    # čistý námět přes LLM + kontext historie (řeší reference, ořeže instrukce).
    subj = _cc._distill_paint_subject(cfg, name, handler, subj)

    # HANS_ART_DISTILL_REJECT_INSTRUCTION_V1 (20.7.) — destilace vrátila None
    # = ani po LLM není konkrétní námět (např. „to znova prosim" v prázdném
    # kanálu). Radši poprosit než malovat nesmysl.
    if not subj:
        return ("Nedokázal jsem z Vašeho požadavku určit, co mám namalovat, "
                "pane. Prosím upřesněte téma — třeba „namaluj kočku na zdi\" "
                "nebo „namaluj japonskou zahradu\".")

    # HANS_HEAVY_QUEUE_V1 (28. 9.) — malba na požádání jde do FRONTY náročných
    # úloh (jedna naráz, jen při volném PC; s úpravou fotky se nepotká).
    # Nahrazuje obě dřívější větve: okamžité vlákno i „dlužný obraz“ při spícím
    # PC (HANS_ART_RETRY_V1 dál dojede staré sliby, nové už nevznikají).
    _st = (" ve stylu „%s\"" % style) if style else ""
    try:
        from scripts import hans_heavy_queue as _hq
        _jid, _ahead = _hq.enqueue(db, "paint", name or "", {
            "subject": subj, "style": style or "", "person": name or ""})
    except Exception as _qe:
        _cc._log.warning("namaluj: fronta selhala: %s", _qe)
        return ("Omlouvám se, pane — obraz teď nemohu zařadit k namalování. "
                "Zkuste to prosím za chvíli znovu.")
    return ("S radostí, pane — obraz na téma „%s\"%s jsem zařadil k namalování. "
            "%s Hotový se objeví na nástěnce (Co Hans namaloval)."
            % (subj[:70], _st, _hq.poradi_text(_ahead)))


def _cmd_obrazy(handler, name, args) -> str:  # HANS_ARTWORK_RECALL_V1
    from scripts.hans_recall import artwork_answer
    # HANS_COUNT_ANSWER_V1 — „kolik“ chce POCET, ne vypis.
    if _cc._KOLIK_RE.search(str(args or "")):
        _p = _cc._pocet_obrazu(handler)
        if _p:
            return _p
    out = artwork_answer(_cc._recall_db(handler), args or "")
    return out or "Nepoda\u0159ilo se mi te\u010f nahl\u00e9dnout do den\u00edku, pane."


# ── HANS_KODI_PLAYCTL_V1 (5.8.) — zastavení/pauza přehrávání ─────────────────
# Nález uživatele: „ještě film zastav" — a zjistilo se, že to NEJDE. Hans umí
# film PUSTIT (agentní akce kodi_play_film), ale zastavit ne; uživatel zkusil
# „/film stop" a dostal výpis, co Hans viděl (`film` je čtecí příkaz, `stop`
# jen ignorovaný argument). Kodi to přitom umí (`stop_playback`/`pause`).
def _cmd_zrusmalbu(handler, name, args) -> str:
    """HANS_PAINT_CANCEL_V1 — zruš moje čekající i běžící malování."""
    cfg = getattr(handler, "config", {}) or {}
    try:
        from scripts import hans_heavy_queue as _hq
        r = _hq.zrus_malovani(cfg, _cc._recall_db(handler), name or "")
    except Exception as e:
        _cc._log.warning("zrušení malby selhalo: %s", e)
        return "Malování se mi teď zrušit nepodařilo, pane."
    vse = ([r["bezici"]] if r["bezici"] else []) + r["cekajici"]
    if not vse:
        return "Teď pro vás nic nemaluji ani nemám ve frontě, pane."
    return ("Zrušeno, pane — %s %s." % (
        "nebudu malovat" if len(vse) > 1 or not r["bezici"] else "přestávám malovat",
        ", ".join("„%s“" % x[:60] for x in vse)))


# ─── /dilo — autorský projekt (HANS_AUTHORSHIP_V1) ───────────────────────────
def _cmd_dilo(handler, name, args) -> str:
    """/dilo — stav autorského projektu; /dilo vše = všechny; /dilo teď = napiš
    další sekci na pozadí (jinak noční práce)."""
    cfg = getattr(handler, "config", {}) or {}
    db = cfg.get("diary_db", "data/hans_diary.db")
    try:
        from scripts.hans_authorship import AuthorshipStore, run_writing_session
    except Exception as e:
        return "Autorský modul nedostupný: %s" % e
    store = AuthorshipStore(cfg, db)
    sub = (args or "").strip().lower()

    if sub in {"teď", "ted", "now", "piš", "pis", "session"}:
        import threading as _th
        kn = getattr(handler, "_knowledge", None) or getattr(handler, "knowledge", None)

        def _run():
            try:
                _cc._log.info("/dilo teď → %s", run_writing_session(cfg, db, knowledge=kn))
            except Exception as _e:
                _cc._log.warning("/dilo teď selhalo: %s", _e)
        _th.Thread(target=_run, daemon=True, name="WriteNow").start()
        return ("Pustil jsem se do psaní, pane — napíšu další sekci svého díla. "
                "Chvíli to potrvá, výsledek pak uvidíte v /dilo a v deníku.")

    if sub in {"vše", "vse", "all", "projekty"}:
        projs = store.all_projects()
        if not projs:
            return "Zatím jsem nezačal žádné dílo, pane."
        out = ["Má díla:"]
        for p in projs:
            out.append("  [%d] „%s\" (%s) — %s (%d/%d sekcí)" % (
                p["id"], p["title"], p["kind"], p["status"],
                p["current_index"], len(p["outline"])))
        return _cc.NL_RUNTIME.join(out)

    ap = store.get_active()
    if not ap:
        projs = store.all_projects()
        if projs:
            last = projs[0]
            # HANS_DILO_PATH_PRIVACY_V1 (18. 9.) — vnitrni cestu k souborum
            # rikej jen ZNAME osobe. Doloženo 15. 9.: cizi tazatel dostal
            # „Najdete ho v data/works/." Protahuje se hotovy predikat
            # `cz_names.is_known_person` (tentyz, jaky uz pouziva
            # `hans_recall` u karty osoby), ne novy mechanismus.
            try:
                from scripts.cz_names import is_known_person as _ikp
                _znamy = bool(name) and _ikp(name, cfg)
            except Exception:
                _znamy = False      # fail-safe: radeji cestu neuvadet
            _kde = "Najdete ho v data/works/. " if _znamy else ""
            # HANS_DILO_STUDY_WORK_V1 (30. 9.) — /dilo znal jen ESEJE
            # (AuthorshipStore). Dilo ze studia (`work_artifact`: webove
            # stranky, skladba) v nem chybelo, takze na „skladas hudbu k tomu
            # projektu?" Hans hlasil jen posledni esej, zatimco ve volnem
            # hovoru o webu vedel. Protahuje se hotovy popis
            # `hans_recall._popis_dila` (HANS_SELF_STATE_WORKS_V1), bez cesty.
            _studium = ""
            try:
                import sqlite3 as _sq
                from scripts.hans_recall import _popis_dila, _cz_when
                _c = _sq.connect("file:%s?mode=ro" % db, uri=True)
                try:
                    _r = _c.execute(
                        "SELECT ts, data FROM diary WHERE event_type='work_artifact' "
                        "ORDER BY ts DESC LIMIT 1").fetchone()
                finally:
                    _c.close()
                if _r and _popis_dila(_r[1] or ""):
                    _studium = ("Poslední dílo ze studia jsem dokončil %s: %s. "
                                % (_cz_when(_r[0]), _popis_dila(_r[1] or "")))
            except Exception as _e:
                _cc._log.debug("%s: %s", "HANS_DILO_STUDY_WORK_V1", _e)
            _stav = {"completed": "dokončeno", "active": "rozepsáno",
                     "abandoned": "odloženo"}.get(last["status"], last["status"])
            return ("Právě nepíšu, pane. Poslední esej: „%s\" (%s). %s%s"
                    "Další dílo si vyberu z trvalého koníčku. "
                    "(/dilo vše, /dilo teď)"
                    % (last["title"], _stav, _studium, _kde))
        return ("Zatím jsem nezačal psát, pane — vyberu si trvalý koníček a "
                "navrhnu dílo. (/dilo teď to spustí ručně)")

    cur, total = ap["current_index"], len(ap["outline"])
    out = ["Píšu: „%s\" (%s) — sekce %d z %d:" % (
        ap["title"], ap["kind"], cur + 1 if cur < total else total, total)]
    if ap.get("premise"):
        out.append("   námět: %s" % ap["premise"])
    for i, s in enumerate(ap["outline"]):
        mark = "✓" if i < cur else ("→" if i == cur else " ")
        out.append("   %s %s" % (mark, s))
    # HANS_DILO_STUDY_WORK_V2 (1. 10.) — dílo ze studia i při rozepsané eseji.
    # V1 ho ukazovala jen ve větvi „právě nepíšu“; od noci 30. 9. se píše esej,
    # takže /dilo o hotovém webu mlčelo a Hans tvrdil, že je „v rané fázi“.
    try:
        import sqlite3 as _sq
        from scripts.hans_recall import _popis_dila, _cz_when
        _c = _sq.connect("file:%s?mode=ro" % db, uri=True)
        try:
            _r = _c.execute("SELECT ts, data FROM diary WHERE event_type='work_artifact' "
                            "ORDER BY ts DESC LIMIT 1").fetchone()
        finally:
            _c.close()
        if _r and _popis_dila(_r[1] or ""):
            out.append("")
            out.append("Poslední dílo ze studia jsem dokončil %s: %s."
                       % (_cz_when(_r[0]), _popis_dila(_r[1] or "")))
    except Exception as _e:
        _cc._log.debug("%s: %s", "HANS_DILO_STUDY_WORK_V2", _e)
    out.append("")
    out.append("Sessions: %d  |  ručně: /dilo teď" % ap["sessions_done"])
    return _cc.NL_RUNTIME.join(out)


# ─── /napad — vlastní nápady / synteze (HANS_SYNTHESIS_IDEAS_V1, #2) ──────────
def _cmd_napad(handler, name, args) -> str:
    """/napad — poslední Hansův postřeh; /napad vše = všechny; /napad teď =
    propoj věci z různých oblastí do nového postřehu (na pozadí, jinak v noci)."""
    cfg = getattr(handler, "config", {}) or {}
    db = cfg.get("diary_db", "data/hans_diary.db")
    try:
        from scripts.hans_ideas import IdeaStore, run_synthesis_session
    except Exception as e:
        return "Modul nápadů nedostupný: %s" % e
    store = IdeaStore(cfg, db)
    sub = (args or "").strip().lower()

    if sub in {"teď", "ted", "now", "synteze", "syntéza"}:
        import threading as _th
        kn = getattr(handler, "_knowledge", None) or getattr(handler, "knowledge", None)

        def _run():
            try:
                _cc._log.info("/napad teď → %s",
                          run_synthesis_session(cfg, db, knowledge=kn))
            except Exception as _e:
                _cc._log.warning("/napad teď selhalo: %s", _e)
        _th.Thread(target=_run, daemon=True, name="SynthNow").start()
        return ("Zkusím propojit pár věcí, co jsem se dozvěděl, pane — chvíli to "
                "potrvá. Postřeh pak najdete v /napad a v deníku.")

    if sub in {"vše", "vse", "all"}:
        ideas = store.all_ideas()
        if not ideas:
            return "Zatím mě nic nového nenapadlo, pane."
        import time as _t
        out = ["Mé postřehy:"]
        for it in ideas:
            day = _t.strftime("%-d.%-m.", _t.localtime(it["ts"]))
            out.append("  [%s] %s" % (day, (it["topics"] or "—")))
            out.append("     %s" % (it["insight"] or ""))
        return _cc.NL_RUNTIME.join(out)

    last = store.latest()
    if not last:
        return ("Zatím mě nic nového nenapadlo, pane — propojím věci z různých "
                "oblastí, které jsem si přečetl. (/napad teď to spustí ručně)")
    import time as _t
    day = _t.strftime("%-d.%-m.", _t.localtime(last["ts"]))
    return _cc.NL_RUNTIME.join([
        "Poslední postřeh (%s) — propojil jsem: %s" % (day, last["topics"] or "—"),
        "", last["insight"] or "",
        "", "ručně: /napad teď"])


# ─── /kritika — sebekritika z vlastního popudu (HANS_SELFCRITIQUE_V1, #6) ─────
def _cmd_kritika(handler, name, args) -> str:
    """/kritika — nedávná Hansova ponaučení o kvalitě vlastního projevu;
    /kritika teď = projdi své poslední repliky a vezmi si ponaučení (na pozadí)."""
    cfg = getattr(handler, "config", {}) or {}
    db = cfg.get("diary_db", "data/hans_diary.db")
    try:
        from scripts.hans_selfcritique import (
            recent_selfcritiques, run_self_critique)
    except Exception as e:
        return "Modul sebekritiky nedostupný: %s" % e
    sub = (args or "").strip().lower()

    if sub in {"teď", "ted", "now"}:
        import threading as _th

        def _run():
            try:
                _cc._log.info("/kritika teď → %s", run_self_critique(cfg, db))
            except Exception as _e:
                _cc._log.warning("/kritika teď selhalo: %s", _e)
        _th.Thread(target=_run, daemon=True, name="SelfCritique").start()
        return ("Projdu si své poslední odpovědi, pane, a vezmu si z nich "
                "ponaučení. Chvíli to potrvá — pak je uvidíte v /kritika.")

    crits = recent_selfcritiques(db, hours=24 * 30, limit=10)
    if not crits:
        return ("Zatím jsem si žádné ponaučení o vlastním projevu nevzal, pane. "
                "(/kritika teď to spustí ručně)")
    out = ["Co u sebe chci zlepšit:"]
    for c in crits:
        out.append("  • %s" % c)
    return _cc.NL_RUNTIME.join(out)


# ─── /dashboard — Hansův návrh vlastní nástěnky (HANS_DASHBOARD_PROPOSAL_V1) ──
def _cmd_dashboard(handler, name, args) -> str:
    """/dashboard — Hansova designová kritika + návrh vlastní nástěnky;
    /dashboard teď = vygeneruj hned (i bez dokončeného studia, na pozadí)."""
    cfg = getattr(handler, "config", {}) or {}
    db = cfg.get("diary_db", "data/hans_diary.db")
    try:
        from scripts.hans_dashboard import latest_proposal, run_dashboard_proposal
    except Exception as e:
        return "Modul návrhu nástěnky nedostupný: %s" % e
    sub = (args or "").strip().lower()

    if sub in {"teď", "ted", "now"}:
        import threading as _th

        def _run():
            try:
                _cc._log.info("/dashboard teď → %s",
                          run_dashboard_proposal(cfg, db, force=True))
            except Exception as _e:
                _cc._log.warning("/dashboard teď selhalo: %s", _e)
        _th.Thread(target=_run, daemon=True, name="DashboardProposal").start()
        return ("Zamyslím se nad podobou své nástěnky, pane — kritika i návrh "
                "chvíli potrvají (a zkusím i obrazový mockup). Pak /dashboard.")

    p = latest_proposal(db)
    if not p:
        return ("Návrh své nástěnky jsem zatím nesepsal — přijde sám po "
                "dostudování designu, nebo ho vyžádejte přes /dashboard teď.")
    import datetime as _dt
    when = _dt.datetime.fromtimestamp(p["ts"]).strftime("%d.%m. %H:%M")
    out = f"Můj návrh nástěnky ({when}):\n\n{p['text']}"
    if p.get("path"):
        out += f"\n\n(Mockup: {p['path']} — najdete v galerii.)"
    return out


def _cmd_avatar(handler, name, args) -> str:
    """/avatar — vizuální descriptor: bez argumentu ukáže aktuální, 'gen' přegeneruje
    z identity (CORE + tendence + koníčky). Render obrázku = fáze 3 (zatím TBD)."""
    cfg = getattr(handler, "config", {}) or {}
    db = cfg.get("diary_db", "data/hans_diary.db")
    try:
        from scripts.avatar_descriptor import (
            latest_descriptor, generate_descriptor, _save_descriptor,
            render_signature, needs_rerender, ALL_FIELDS)
    except Exception as e:
        return "Avatar modul nedostupný: %s" % e

    sub = (args or "").strip().lower()
    _RENDER_NOTE = ("⚠ Obrázek se zatím negeneruje — render (fáze 3, ComfyUI) "
                    "není postaven. Tohle je jen popis vzhledu.")

    def _fmt(d):
        lines = ["Podoba v%d:" % d.get("version", 0)]
        for f in ALL_FIELDS:
            lines.append("  %s: %s" % (f, d.get(f, "")))
        lines.append("  signature: %s" % render_signature(d))
        return _cc.NL_RUNTIME.join(lines)

    if sub in _cc._AVATAR_GEN:
        prev = latest_descriptor(db)
        new = generate_descriptor(cfg, db, prev=prev)
        if not new:
            return "Nepodařilo se vygenerovat descriptor (qwen/Ollama nedostupná? viz log)."
        if needs_rerender(prev, new):
            _save_descriptor(db, new)
            tail = "Uloženo jako v%d (čeká na render)." % new["version"]
        else:
            tail = "Vzhled se neposunul (charakter stejný) — neukládám novou verzi."
        return _cc.NL_RUNTIME.join([_fmt(new), "", tail, _RENDER_NOTE])

    cur = latest_descriptor(db)
    if not cur:
        return ("Zatím žádná podoba. /avatar gen ji vygeneruje z aktuální identity "
                "(CORE + tendence + koníčky). " + _RENDER_NOTE)
    return _cc.NL_RUNTIME.join([_fmt(cur), "",
                            "/avatar gen = přegeneruj z aktuální identity.", _RENDER_NOTE])


# ─── /brief — destilát studia do prompt/briefu pro dílo (HANS_BRIEF_V1) ───────
def _cmd_brief(handler, name, args) -> str:  # HANS_BRIEF_V1
    """/brief — poslední brief; /brief <téma> [coder|esej|obraz] = destiluj
    studium do nejlepšího promptu pro tvorbu díla (bez persony, grounded)."""
    import threading as _th
    from scripts import hans_brief as hb
    cfg = getattr(handler, "config", {}) or {}
    db = _cc._recall_db(handler)
    a = (args or "").strip()

    # bez argumentu → poslední brief
    if not a:
        last = hb.BriefStore(db).latest()
        if not last:
            done = hb.completed_study_topics(db)
            hint = (" Dostudoval jsem: %s." % ", ".join(done)) if done else ""
            return ("Zatím jsem žádný brief nedestiloval, pane. Napiš "
                    "/brief <téma> a připravím prompt z nastudovaného.%s" % hint)
        return ("Poslední brief — %s (%s), z poznámek: %s\n\n%s" % (
            last["topic"], last["target"],
            (last.get("source_notes") or "")[:120], last["brief"]))

    # /brief <téma> [cíl]
    parts = a.rsplit(None, 1)
    target = "coder"
    topic = a
    if len(parts) == 2 and parts[1].lower() in (
            "coder", "kod", "kód", "esej", "essay", "obraz", "image"):
        topic = parts[0]
        t = parts[1].lower()
        target = ("essay" if t in ("esej", "essay")
                  else "image" if t in ("obraz", "image") else "coder")

    def _build():
        try:
            r = hb.build_brief(cfg, db, topic, target)
            _cc._log.info("/brief %s (%s) → %s", topic, target, r.get("status"))
        except Exception as _e:
            _cc._log.warning("/brief selhal: %s", _e)
    _th.Thread(target=_build, daemon=True).start()
    return ("Destiluji, co jsem se naučil o „%s“, do promptu pro dílo (%s), "
            "pane — chvíli to potrvá. Pak zadej /brief a ukážu ho." % (
                topic, target))


# ─── /vytvor — celá smyčka studium → brief → nástroj → artefakt (HANS_MAKER_V1) ─
def _cmd_vytvor(handler, name, args) -> str:  # HANS_MAKER_V1
    """/vytvor <téma> [coder|obraz] — Hans z nastudovaného vyrobí reálný
    artefakt (coder→HTML/CSS, obraz→SDXL). Běží na pozadí (pomalé)."""
    import threading as _th
    from scripts import hans_maker as hm
    cfg = getattr(handler, "config", {}) or {}
    db = _cc._recall_db(handler)
    a = (args or "").strip()
    if not a:
        arts = hm.latest_artifacts(db, 3)
        if arts:
            out = ["Má poslední díla, pane:"]
            for x in arts:
                out.append("• %s (%s) → %s" % (x.get("topic", x.get("title")),
                           x.get("target", "?"), x.get("path", "")))
            return "\n".join(out)
        return ("Zatím jsem žádné dílo nevytvořil, pane. Napiš /vytvor <téma> "
                "a z nastudovaného vyrobím artefakt (např. /vytvor Design).")
    parts = a.rsplit(None, 1)
    target, topic = "coder", a
    if len(parts) == 2 and parts[1].lower() in ("coder", "obraz", "image"):
        topic = parts[0]
        target = "image" if parts[1].lower() in ("obraz", "image") else "coder"

    def _make():
        try:
            r = hm.make_from_study(cfg, db, topic, target)
            _cc._log.info("/vytvor %s (%s) → %s (%s)", topic, target,
                      r.get("status"), r.get("path", r.get("reason", "")))
        except Exception as _e:
            _cc._log.warning("/vytvor selhal: %s", _e)
    _th.Thread(target=_make, daemon=True).start()
    return ("Pouštím se do díla k „%s“ (%s) z toho, co jsem nastudoval, pane. "
            "Vyrobí to nástroj podle mého briefu — chvíli to potrvá (i pár "
            "minut). Pak zadej /vytvor a ukážu, co vzniklo." % (topic, target))


# ─── /prohloubit — schválení/kritika návrhu prohloubení studia (DEEPEN_V2) ────
def _cmd_prohloubit(handler, name, args) -> str:  # HANS_STUDY_DEEPEN_V2
    """/prohloubit — Hansovy návrhy prohloubení; /prohloubit schválit = přijmi;
    /prohloubit <vlastní kritika> = prohluť podle tebe; /prohloubit ne = zamítni."""
    import threading as _th
    from scripts.hans_study import StudyStore
    cfg = getattr(handler, "config", {}) or {}
    db = _cc._recall_db(handler)
    st = StudyStore(cfg, db)
    a = (args or "").strip()
    low = a.lower()

    pend = st.get_pending_deepen()
    # bez argumentu → výpis návrhů
    if not a:
        if not pend:
            return ("Teď nemám žádný návrh na prohloubení, pane. Vytvořím ho, "
                    "až dokončím dílo z nastudovaného.")
        out = ["Mé návrhy na prohloubení studia, pane:"]
        for p in pend:
            out.append("• %s (kolo %d)\n  Kritika: %s\n  Doučil bych se: %s" % (
                p["topic"], p["round"], p["critique"] or "—",
                "; ".join(p["subtopics"])))
        out.append("\n/prohloubit schválit  ·  /prohloubit <vlastní kritika>  ·  "
                   "/prohloubit ne")
        return "\n".join(out)

    if not pend:
        return "Nemám čekající návrh, pane."

    # zamítnout
    if low in ("ne", "zamítnout", "zamitnout", "odmítnout", "odmitnout", "ignoruj"):
        st.reject_deepen_proposal(pend[0]["id"])
        return "Dobře, pane. Prohloubení „%s“ nechám být." % pend[0]["topic"]

    # schválit (dle Hansova návrhu) NEBO vlastní kritika
    is_approve = low in ("schválit", "schvalit", "ano", "ok", "jo", "souhlasím",
                         "souhlasim", "schvaluji")
    user_crit = "" if is_approve else a

    def _apply():
        try:
            r = st.apply_deepen_proposal(cfg, pend[0]["id"], user_critique=user_crit)
            _cc._log.info("/prohloubit → %s (+%s)", r.get("status"),
                      len(r.get("added", [])))
        except Exception as _e:
            _cc._log.warning("/prohloubit selhalo: %s", _e)
    _th.Thread(target=_apply, daemon=True).start()
    if user_crit:
        return ("Beru tvou kritiku, pane, a podle ní prohloubím studium „%s“. "
                "Chvíli to potrvá; pak uvidíš nová pod-témata v /studium." %
                pend[0]["topic"])
    return ("Schváleno, pane. Prohloubím studium „%s“ podle svého návrhu — "
            "nová pod-témata uvidíš v /studium a příště z nich vytvořím lepší "
            "dílo." % pend[0]["topic"])


# ─── /sen — co se Hansovi zdálo (HANS_DREAM_RECALL_V1) ────────────────────
def _cmd_sen(handler, name, args) -> str:
    """HANS_DREAM_RECALL_V1 (5.9.) — dotaz na sen z deníku, ne z fantazie."""
    from scripts.hans_recall import dream_answer
    q = (args or "").strip()
    # Týž důvod jako u `_cmd_cetl`: LLM router předává args="" (pojistka proti
    # mutujícím podpříkazům), takže by se ztratilo TÉMA i slovo „dnes".
    # Příkaz je čistě ČTECÍ, takže se původní věta vezme z vlákna.
    if not q:
        try:
            _tc = getattr(handler, "_thread_ctx", None)
            if _tc and _tc[0]:
                q = str(_tc[0])
        except Exception:
            pass
    out = dream_answer(_cc._recall_db(handler), q, asker=name)
    return out or "Nepodařilo se mi teď nahlédnout do deníku, pane."

# ROZDELENI_PRIKAZU_V1 — až na konci, viz hlavička
from scripts import chat_commands as _cc  # noqa: E402
