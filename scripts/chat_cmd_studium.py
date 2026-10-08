"""Obsluhy příkazů přesunuté z `scripts/chat_commands.py` (ROZDELENI_PRIKAZU_V1).

Text funkcí je beze změny; jména původního modulu se čtou přes `_cc.` až při
volání. Registrace příkazů (`register(...)`) zůstává v původním modulu.
Import původního modulu je na KONCI souboru (kruhový import oběma směry).
"""
from __future__ import annotations

import re
import threading

def _cmd_interest(handler, name, args) -> str:  # INTEREST_CMD_C2 / INTEREST_DEL
    """/interest                -> výpis naučených zájmů
    /interest <téma>       -> zapíše nový zájem
    /interest del <téma>   -> smaže zájem(y) s daným textem
    /interest reset ano    -> smaže VŠECHNY naučené (Hans spadne na seed)
    Tytéž řádky (event_type=interest_update) řídí personu."""
    import sqlite3
    import time
    cfg = getattr(handler, "config", {}) or {}
    db_path = cfg.get("diary_db", "data/hans_diary.db")
    raw = (args or "").strip()
    parts = raw.split(None, 1)
    sub = parts[0].lower() if parts else ""
    rest = parts[1].strip() if len(parts) > 1 else ""

    # ── /interest del <téma> ────────────────────────────────────────────
    if sub == "del":
        if not rest:
            return "Použití: /interest del <téma>  (smaže zájem s tímto textem)"
        try:
            conn = sqlite3.connect(db_path, timeout=5.0)
            try:
                cur = conn.execute(
                    "SELECT id, note FROM diary WHERE event_type='interest_update' "
                    "AND lower(note)=lower(?)", (rest,)).fetchall()
                if not cur:
                    return "Žádný naučený zájem '%s' jsem nenašel." % rest
                conn.execute(
                    "DELETE FROM diary WHERE event_type='interest_update' "
                    "AND lower(note)=lower(?)", (rest,))
                conn.commit()
            finally:
                conn.close()
        except Exception as _e:
            return "INTEREST: mazání selhalo: %s" % _e
        n = len(cur)
        return ("Smazal jsem zájem '%s'%s." %
                (rest, (" (%dx)" % n) if n > 1 else ""))

    # ── /interest reset [ano] ───────────────────────────────────────────
    if sub == "reset":
        if rest.lower() != "ano":
            from scripts.hans_persona import persona_name as _pn  # PERSONA_NAME_CONFIGURABLE_V1
            return (f"Reset smaže VŠECHNY naučené zájmy a {_pn(cfg)} spadne zpět "
                    "na základní. Pro potvrzení napiš: /interest reset ano")
        try:
            conn = sqlite3.connect(db_path, timeout=5.0)
            try:
                cur = conn.execute(
                    "SELECT COUNT(*) FROM diary "
                    "WHERE event_type='interest_update'").fetchone()
                n = cur[0] if cur else 0
                conn.execute(
                    "DELETE FROM diary WHERE event_type='interest_update'")
                conn.commit()
            finally:
                conn.close()
        except Exception as _e:
            return "INTEREST: reset selhal: %s" % _e
        from scripts.hans_persona import persona_name as _pn  # PERSONA_NAME_CONFIGURABLE_V1
        return ("Smazal jsem všechny naučené zájmy (%d). "
                "%s se vrací k základním." % (n, _pn(cfg)))

    # ── /interest (výpis) ───────────────────────────────────────────────
    if not raw:
        try:
            from scripts.hans_persona import recent_interests
            cur = recent_interests(db_path, limit=5)
        except Exception as _e:
            return "INTEREST: čtení selhalo: %s" % _e
        if not cur:
            seed = cfg.get("persona", {}).get("interests_seed", "")
            from scripts.hans_persona import persona_name as _pn  # PERSONA_NAME_CONFIGURABLE_V1
            return ("Žádné naučené zájmy zatím nejsou. "
                    "%s vychází ze základních: %s" % (_pn(cfg), seed or "(žádné)"))
        return ("Aktuální naučené zájmy (nejnovější první): %s\n"
                "Smazat: /interest del <téma>" % cur)

    # ── /interest <téma> (zápis) ────────────────────────────────────────
    try:
        conn = sqlite3.connect(db_path, timeout=5.0)
        try:
            conn.execute(
                "INSERT INTO diary (ts, event_type, title, note) VALUES (?,?,?,?)",
                (time.time(), "interest_update", "Zájem", raw))
            conn.commit()
        finally:
            conn.close()
    except Exception as _e:
        return "INTEREST: zápis selhal: %s" % _e
    return ("Zaznamenal jsem nový zájem: '%s'. "
            "Promítne se do Hansovy osobnosti." % raw)


def _cmd_work(handler, name, args) -> str:  # WORK_CMD_V1 + WORK_REFACTOR_SHARED_V1
    """/work <téma> — tenký wrapper. Jádro je v hans_idle._create_work,
    aby ho mohla volat i automatika (idle smyčka) přes shim."""
    topic = (args or '').strip()
    if not topic:
        return 'Použití: /work <téma>  (např. /work sopky)'
    _hi = getattr(handler, '_hans_idle', None)
    if _hi is None:
        return 'WORK: idle objekt (handler._hans_idle) není dostupný.'
    if not hasattr(_hi, '_create_work'):
        return 'WORK: _create_work chybí na idle (refaktor patch nasazen?).'
    # llm_caller: zabal _g5c_llm s reálným handlerem (num_predict=900 jak dřív)
    _caller = lambda s, u: _cc._g5c_llm(handler, s, u, num_predict=900)
    r = _hi._create_work(topic, _caller)
    if not r.get('ok'):
        return 'WORK: %s' % r.get('error', 'neznámá chyba')
    return (('Hotovo. Napsal jsem esej o \'%s\' (%d slov). '
             'Uloženo: %s · RAG: %s')
            % (topic, r.get('words', 0), r.get('path', '?'), r.get('rag', '?')))


def _cmd_severka(handler, name, args) -> str:
    """/severka — stav / schválit / zamítnout / historie / rollback / teď."""
    _hi = getattr(handler, "_hans_idle", None)
    _rt = getattr(_hi, "_routine", None) if _hi else None
    if not _rt:
        return "Severka není dostupná (routine chybí)."
    ident = getattr(_rt, "_identity", None)
    sev = getattr(_rt, "_severka", None)
    if ident is None:
        return "Verzování identity není dostupné."
    # SEVERKA_KNOWN_ONLY_V1 (24. 9.) — identita je věc domácnosti: cizí nesmí
    # schvalovat, vracet ani spouštět změnu a nemá ani číst historii verzí.
    # Přísně: prázdné jméno se tu NEpočítá za známé (jinde to konvence je).
    try:
        from scripts.cz_names import is_known_person as _ikp
        _znamy = bool(name) and _ikp(name)
    except Exception:
        _znamy = False
    if not _znamy:
        _cc._log.info("SEVERKA_KNOWN_ONLY_V1: /severka od neznámého (%s) odmítnuto", name)
        return "O své identitě mluvím jen se svou domácností."
    parts = (args or "").strip().split(maxsplit=1)
    cmd = parts[0].lower() if parts else "stav"
    rest = parts[1].strip() if len(parts) > 1 else ""
    by = name or "user"

    pend = ident.pending()
    # SEVERKA_DISPLAY_NAME_V1 — CORE je uložený s tokenem {name}
    # (SEVERKA_NAME_TOKEN_V1); do výpisu pro člověka patří jméno.
    from scripts.hans_persona import apply_name as _an
    _jm = lambda t: _an(t or "", getattr(ident, "_config", None) or {})

    if cmd in _cc._SEVERKA_APPROVE:
        target = int(rest) if rest.isdigit() else (pend[0].id if pend else None)
        if target is None:
            return "Není co schvalovat — žádný čekající návrh, pane."
        if ident.approve(target, approved_by=by):
            cur = ident.current()
            core = cur.core if cur else ""
            return ("Děkuji za důvěru, pane. Přijal jsem novou podobu sebe sama. "
                    "Od této chvíle jsem:" + _cc.NL_RUNTIME + _cc.NL_RUNTIME + "„" + _jm(core) + "\"")
        return "Schválení se nezdařilo (verze %s není čekající?)." % target

    if cmd in _cc._SEVERKA_REJECT:
        target = int(rest) if rest.isdigit() else (pend[0].id if pend else None)
        if target is None:
            return "Není co zamítat, pane."
        if ident.reject(target, approved_by=by):
            return "Rozumím, pane. Zůstávám, kým jsem byl."
        return "Zamítnutí se nezdařilo."

    if cmd in _cc._SEVERKA_ROLLBACK:
        if not rest.isdigit():
            return "Uveďte verzi: /severka rollback <id> (viz /severka historie)."
        if ident.rollback(int(rest), approved_by=by):
            cur = ident.current()
            return ("Vrátil jsem se k dřívější podobě:" + _cc.NL_RUNTIME + _cc.NL_RUNTIME
                    + "„" + _jm(cur.core if cur else "") + "\"")
        return "Rollback se nezdařil."

    if cmd in _cc._SEVERKA_HISTORY:
        hist = ident.history(limit=15)
        if not hist:
            return "Historie identity je prázdná."
        out = ["Historie mé identity, pane:"]
        for v in hist:
            out.append("  [%d] %s — %s: %.70s" % (v.id, v.status, v.source, _jm(v.core)))
        return _cc.NL_RUNTIME.join(out)

    if cmd in _cc._SEVERKA_RUN:
        if sev is None:
            return "Rozhodovací mechanismus není dostupný."
        def _run():
            try:
                sev.evaluate(force=True)  # SEVERKA_CHANGE_COOLDOWN_V1: ruční = bez odstupu
            except Exception as _e:
                _cc._log.error("severka manual run: %s", _e)
        threading.Thread(target=_run, daemon=True).start()
        return ("Zamýšlím se nad tím, kým se stávám, pane. Chvíli to potrvá; "
                "výsledek pak najdete v /severka stav.")

    # default: stav
    cur = ident.current()
    out = []
    if cur:
        out.append("Současná identita (verze %d, zdroj %s):" % (cur.id, cur.source))
        out.append("„" + _jm(cur.core) + "\"")
    if pend:
        out.append("")
        out.append("Čekající návrh změny:")
        for p in pend:
            out.append("  [verze %d] „%s\"" % (p.id, _jm(p.core)))
            if p.rationale:
                out.append("    důvod: %s" % p.rationale)
        out.append("")
        out.append("Schválit: /severka schválit  |  zamítnout: /severka zamítnout")
    else:
        out.append("")
        out.append("Žádný čekající návrh změny, pane.")
    return _cc.NL_RUNTIME.join(out)


# ─── /uzamceni — HANS_LOCK_AUDIT_V1 (24. 9.) ─────────────────────────────
def _cmd_uzamceni(handler, name, args) -> str:
    """Týdenní míry uzamčení (self-locking) — pro lidi, ne pro Hanse."""
    cfg = getattr(handler, "config", {}) or {}
    db = (cfg.get("diary_db") or (cfg.get("diary", {}) or {}).get("db_path")
          or "data/hans_diary.db")
    try:
        from scripts.hans_uzamceni import format_report
        return format_report(db)
    except Exception as e:
        _cc._log.warning("/uzamceni selhalo: %s", e)
        return "Měření uzamčení se teď nepovedlo."


def _cmd_studium(handler, name, args) -> str:
    """/studium — stav studijního programu; /studium programy = všechny;
    /studium teď = spustí jednu studijní session na pozadí (noční práce ručně)."""
    cfg = getattr(handler, "config", {}) or {}
    db = cfg.get("diary_db", "data/hans_diary.db")
    try:
        from scripts.hans_study import StudyStore, run_study_session
    except Exception as e:
        return "Studijní modul nedostupný: %s" % e
    store = StudyStore(cfg, db)
    sub = (args or "").strip().lower()

    if sub in _cc._STUDY_NOW:
        import threading as _th
        kn = getattr(handler, "_knowledge", None) or getattr(handler, "knowledge", None)

        # HANS_STUDY_NUDGE_V1 — session běží na pozadí; když skončí JINAK než
        # úspěchem, uživatel se to dosud NEDOZVĚDĚL (odpověď zněla „výsledek
        # uvidíte v /studium" a pak ticho). Doloženo 4.8.: „Geologie Českého
        # ráje" → noread, program stál a nic to nehlásilo. Teď se výsledek
        # ohlásí zpět — u `noread` i s nabídkou ruční zkratky.
        _prev = store.get_active_program()
        _prev_sub = ""
        try:
            _prev_sub = str(_prev["curriculum"][_prev["current_index"]])
        except Exception:
            pass

        def _run():
            try:
                code = run_study_session(cfg, db, knowledge=kn)
                _cc._log.info("/studium teď → %s", code)
                _msg = None
                if code == "noread":
                    _msg = ("K pod-tématu „%s\" jsem nenašel žádný použitelný "
                            "zdroj, pane — encyklopedie ho zřejmě nezná. "
                            "Program tím pádem stojí. Můžete mi říct "
                            "„/studium přeskoč\" a pustím se do dalšího."
                            % (_prev_sub or "aktuální"))
                elif code == "deferred":
                    _msg = ("Studium jsem musel odložit, pane — buď mi nebyl "
                            "dostupný mozek, nebo encyklopedie neodpovídala. "
                            "Zkusím to znovu sám.")
                elif code == "skipped":
                    # HANS_STUDY_UNIFY_V1 — `skipped` sem dosud nepropadl, takže
                    # uživatel po ručním „/studium teď" NEDOSTAL žádnou zprávu,
                    # ačkoli právě kvůli tomu HANS_STUDY_NUDGE_V1 vznikl.
                    _msg = ("Pod-téma „%s\" jsem po opakovaných pokusech "
                            "přeskočil, pane — encyklopedie k němu nic nemá. "
                            "Pokračuji dalším v pořadí."
                            % (_prev_sub or "aktuální"))
                elif code == "idle":
                    _msg = "Teď nemám co studovat, pane — vše z kurikula je hotové."
                if _msg:
                    _cc._notify_user(handler, _msg)
            except Exception as _e:
                _cc._log.warning("/studium teď selhalo: %s", _e)
        _th.Thread(target=_run, daemon=True, name="StudyNow").start()
        return ("Pustil jsem se do studia, pane — nastuduji další pod-téma. "
                "Chvíli to potrvá (čtení + zápis poznámky), výsledek pak "
                "uvidíte v /studium a v deníku. Kdyby se nedařilo, ozvu se.")

    if sub in _cc._STUDY_SKIP:
        # HANS_STUDY_NUDGE_V1 — ruční přeskočení zaseklého pod-tématu. Automatika
        # ho přeskočí až po `max_subtopic_failures` nocích; tohle je zkratka,
        # když uživatel VIDÍ, že na tom program vázne.
        ap = store.get_active_program()
        if not ap:
            return "Teď nestuduji žádný program, pane — není co přeskočit."
        curriculum = ap["curriculum"]
        idx = int(ap["current_index"])
        if idx >= len(curriculum):
            return "Kurikulum už je u konce, pane — není co přeskočit."
        skipped = str(curriculum[idx])
        nxt = idx + 1
        store._update_fields(ap["id"], current_index=nxt, fail_count=0)
        # HANS_STUDY_SKIPPED_MARK_V1 — i RUČNÍ přeskočení se musí zapsat,
        # jinak by se ve /studium ukázalo jako nastudované (nález 6.8.).
        try:
            store.mark_skipped(ap["id"], idx)
        except Exception as _mse:
            _cc._log.warning("mark_skipped (ruční): %s", _mse)
        _cc._log.info("/studium přeskoč → '%s' (program [%d], %d→%d)",
                  skipped, ap["id"], idx, nxt)
        if nxt >= len(curriculum):
            return ("Přeskočil jsem „%s\", pane — a tím je kurikulum „%s\" "
                    "u konce. Mistrovskou reflexi sepíšu v noci."
                    % (skipped, ap["topic"]))
        return ("Přeskočil jsem „%s\", pane. Další na řadě: „%s\" "
                "(%d z %d). Nastuduji ho v noci — nebo hned, řeknete-li "
                "„/studium teď\"."
                % (skipped, curriculum[nxt], nxt + 1, len(curriculum)))

    if sub in _cc._STUDY_ORIGIN or _cc._je_dotaz_na_puvod(args):
        return _cc._studium_puvod(store, db, args)

    if sub in {"programy", "programs", "vše", "vse", "all"}:
        progs = store.all_programs()
        if not progs:
            return "Zatím jsem nezačal žádný studijní program, pane."
        out = ["Studijní programy:"]
        for p in progs:
            out.append("  [%d] %s — %s (%d/%d, %d sessions)" % (
                p["id"], p["topic"], p["status"], p["current_index"],
                len(p["curriculum"]), p["sessions_done"]))
        return _cc.NL_RUNTIME.join(out)

    ap = store.get_active_program()
    if not ap:
        progs = store.all_programs()
        if progs:
            # HANS_STUDIUM_LAST_DONE_V1 (30. 9.) — „naposledy“ = naposledy
            # DOKONČENÝ program, ne nejnovější řádek. Test 30. 9.: hudba
            # dokončena 7/7 v 08:07, ale „co jsi teď dostudoval?“ dostalo
            # „Naposledy: dynamické weby (pending, 0/0)“ — čekající na řadě.
            hotove = [p for p in progs if p.get("status") == "completed"]
            cekaji = [p for p in progs if p.get("status") == "pending"]
            if hotove:
                last = max(hotove, key=lambda p: (p.get("updated_ts") or 0, p["id"]))
                veta = ("Právě nestuduji, pane. Naposledy jsem dokončil „%s\" "
                        "(%d/%d)." % (last["topic"], len(last["curriculum"]),
                                      len(last["curriculum"])))
            else:
                last = progs[0]
                veta = ("Právě nestuduji, pane. Naposledy: „%s\" (%s, %d/%d)."
                        % (last["topic"], last["status"], last["current_index"],
                           len(last["curriculum"])))
            if cekaji:
                dalsi = min(cekaji, key=lambda p: p["id"])
                veta += " Další na řadě: „%s\"." % dalsi["topic"]
            else:
                veta += " Další program si vyberu z trvalého koníčku."
            return veta + " (/studium programy, /studium teď)"
        return ("Zatím jsem nezačal studijní program, pane — vyberu si trvalý "
                "koníček a sestavím kurikulum. (/studium teď to spustí ručně)")

    cur = ap["current_index"]
    total = len(ap["curriculum"])
    out = ["Studuji: „%s\" — pod-téma %d z %d:" % (ap["topic"], cur + 1 if cur < total else total, total)]
    # HANS_STUDY_SKIPPED_MARK_V1 — přeskočené se NESMÍ kreslit jako ✓
    # (nález uživatele 6.8.: „ukazuje jako nastudováno").
    _skipped = ap.get("skipped_idx") or set()
    _n_skip = 0
    for i, s in enumerate(ap["curriculum"]):
        if i in _skipped:
            mark = "⤼"
            _n_skip += 1
        elif i < cur:
            mark = "✓"
        elif i == cur:
            mark = "→"
        else:
            mark = " "
        out.append("   %s %s" % (mark, s))
    if _n_skip:
        out.append("   (⤼ = přeskočeno, nenašel jsem k tomu zdroj)")
    out.append("")
    # HANS_STUDY_NUDGE_V1 — bez tohohle nebylo z výpisu poznat, že program
    # VÁZNE (jen že stojí na pod-tématu). fail_count = kolik nocí po sobě se
    # k němu nenašel zdroj; po `max_subtopic_failures` ho automatika přeskočí.
    _fc = int(ap.get("fail_count", 0) or 0)
    if _fc:
        _maxf = int((cfg.get("study", {}) or {}).get("max_subtopic_failures", 3))
        out.append("⚠ K tomuhle pod-tématu se mi %d× nepodařilo najít zdroj "
                   "(z %d pokusů, pak ho přeskočím sám). "
                   "Chcete-li hned: /studium přeskoč" % (_fc, _maxf))
        out.append("")
    # HANS_STUDY_TODAY_LINE_V1 (18.8.) — DNEŠEK. Výpis dosud ukazoval jen stav
    # kurikula, takže na „jak ti dneska šlo studium?" i na přímé „povedlo se ti
    # dneska něco nastudovat?" chodila TÁŽ statická šablona (doloženo dialogem
    # 18.8.). Poctivá odpověď přitom v datech JE — `hans_schedule.study_tick`
    # od HANS_SCHEDULE_LAST_OK_V1 rozlišuje „kdy to naposledy zkusilo" od
    # „kdy naposledy USPĚLO". Bez tohohle řádku Hans o dnešku buď mlčel, nebo
    # si ho přisvojil („dnes jsem prohluboval znalosti…“, ač studium neproběhlo).
    # HANS_STUDY_TODAY_SHARED_V1 — věta o dnešku má JEDNU implementaci
    # (`hans_study.today_line`), ať se výpis a volný hovor nerozejdou.
    try:
        from scripts.hans_study import today_line as _today_line
        _tl = _today_line(_cc._recall_db(handler))
        if _tl:
            out.append(_tl)
            out.append("")
    except Exception as _te:
        _cc._log.debug("/studium: dnešní řádek nešel sestavit (%s)", _te)
    out.append("Sessions: %d  |  ručně: /studium teď, /studium přeskoč"
               % ap["sessions_done"])
    # HANS_STUDY_RECALL_V1 — fronta pending (aby bylo jasné, co JEŠTĚ NENÍ
    # nastudováno; jinak by se dalo splést zařazené s hotovým).
    try:
        pend = [p for p in store.all_programs() if p.get("status") == "pending"]
        if pend:
            out.append("Ve frontě ke studiu (zatím nenastudováno): %s"
                       % ", ".join(p["topic"] for p in pend))
    except Exception:
        pass
    return _cc.NL_RUNTIME.join(out)


def _cmd_smer(handler, name, args) -> str:
    """/smer — aktivní směr + čekající návrh; /smer schválit|ne; /smer teď =
    zvaž směr na pozadí; /smer <text> = zadej vlastní směr."""
    cfg = getattr(handler, "config", {}) or {}
    db = (cfg.get("diary_db") or (cfg.get("diary", {}) or {}).get("db_path")
          or "data/hans_diary.db")
    try:
        from scripts.hans_direction import HansDirection, DirectionStore
    except Exception as e:
        return "Modul směru nedostupný: %s" % e
    st = DirectionStore(cfg, db)
    sub = (args or "").strip()
    low = sub.lower()

    # schválit / zamítnout čekající návrh
    if low in {"schválit", "schvalit", "schval", "ano", "ok", "approve"}:
        a = st.approve()
        if not a:
            return "Žádný čekající návrh směru ke schválení."
        return "Přijato za svůj směr: „%s\"" % a["direction"]
    if low in {"ne", "zamítnout", "zamitnout", "zamítni", "zamitni", "reject"}:
        if st.pending():
            st.reject()
            return "Návrh směru zamítnut. Zůstávám u dosavadního (pokud nějaký byl)."
        return "Žádný čekající návrh směru."

    # spustit úvahu o směru na pozadí
    if low in {"teď", "ted", "now", "zvaž", "zvaz"}:
        import threading as _th

        def _run():
            try:
                r = HansDirection(cfg, db).evaluate()
                if r.get("decision") in ("propose", "evolve") and r.get("message"):
                    tg = getattr(handler, "telegram", None)
                    if tg is not None and hasattr(tg, "send_proactive"):
                        try:
                            tg.send_proactive(r["message"])
                        except Exception as _tiche:
                            from scripts.logger import tichy_zapis as _tz  # HANS_SILENT_WRITE_LOG_V1
                            _tz('chat_commands:_cmd_smer', _tiche)
            except Exception as _e:
                _cc._log.warning("/smer teď selhalo: %s", _e)
        _th.Thread(target=_run, daemon=True).start()
        return ("Zamýšlím se nad svým směrem — ohlédnu se za studiem a tvorbou. "
                "Když z toho vzejde záměr, dám vědět (chvíli to potrvá).")

    # zadat vlastní směr (uživatelem autorizovaný → rovnou aktivní).
    # HANS_DIRECTION_NL_ARG_GUARD_V1: jen oznamovací věta, ne otázka z NL shody.
    if sub and low not in {"stav", "status"} and _cc._smer_is_custom(sub):
        pid = st.propose(sub, "zadáno uživatelem", "", "user")
        st.approve(pid)
        return "Nastaven tvůj směr: „%s\"" % sub

    # výpis (default / stav)
    cur = st.current_active()
    pend = st.pending()
    out = []
    if cur:
        out.append("🧭 Můj směr: „%s\"" % cur["direction"])
        if cur.get("rationale"):
            out.append("   %s" % cur["rationale"])
    else:
        out.append("Zatím nemám vědomě zvolený směr.")
    if pend:
        out.append("")
        out.append("⏳ Čeká na tvé rozhodnutí: „%s\"" % pend["direction"])
        out.append("   (/smer schválit — /smer ne — /smer <vlastní text>)")
    elif not cur:
        out.append("(/smer teď — zvážím ho ze studia a tvorby)")
    # HANS_ART_INTENT_V1 (5.8.) — trvalé tvůrčí záměry patří ke směru: destiluje
    # je reflexe tvorby z reálných děl, tak ať jsou vidět a dají se ověřit.
    try:
        from scripts.hans_art_intent import active_intentions as _ai
        _ints = _ai(db)
    except Exception:
        _ints = []
    if _ints:
        out.append("")
        out.append("🎨 V tvorbě sleduju:")
        for _t in _ints:
            out.append("   • %s" % _t)
    return _cc.NL_RUNTIME.join(out)


# ─── /nalez — co Koláč u Hanse našel (KOLAC_EXAM_CONFIRM_V1) ─────────────────
def _cmd_nalez(handler, name, args) -> str:
    """/nalez — neposouzené nálezy ze zkoušení; /nalez N = udělej z toho
    trvalou zkušební otázku; /nalez N ne = zamítni.

    Posuzuje ČLOVĚK. Stroj nález jen předloží a po potvrzení z něj udělá
    trvalý test — automatické učení z vlastní vymyšlené odpovědi by byla
    přesně ta otrava paměti, kvůli které zkoušení běží pod testovací identitou.
    """
    from scripts import kolac_exam as _ke
    db = _cc._recall_db(handler) or "data/hans_diary.db"
    cfg = getattr(handler, "config", {}) or {}
    a = " ".join((args or "").split())
    _c = a.split()
    # Číslo nálezu bereme, JEN když je to číslo. Přirozený dotaz („co u tebe
    # našel Koláč?") dorazí sem s celou větou v argumentech — ta má vypsat
    # frontu, ne skončit na hlášce, že chybí číslo.
    if _c and _c[0].lstrip("#").isdigit():
        _id = _c[0].lstrip("#")
        _ne = len(_c) > 1 and _c[1].lower() in ("ne", "zamitnout", "zamítnout",
                                                "nic", "smaz", "smaž")
        return (_ke.zamitni(db, int(_id)) if _ne
                else _ke.potvrd(db, int(_id), cfg))
    polozky = _ke.nalezy(db)
    if not polozky:
        return ("Ze zkoušení nemám nic nevyřízeného, pane.")
    import time as _t
    radky = ["Co u mě Koláč našel a čeká na vaše posouzení:"]
    for p in polozky:
        radky.append("  #%d [%s] %s — %s (%s)" % (
            p["id"], _t.strftime("%-d.%-m. %H:%M", _t.localtime(p["ts"])),
            _ke._POPIS.get(p["verdikt"], p["verdikt"]),
            (p["tema"] or "")[:60], p["zdroj"]))
    radky.append("")
    radky.append("Potvrdit jako trvalou otázku: /nalez <číslo>. "
                 "Zamítnout: /nalez <číslo> ne.")
    return "\n".join(radky)


# ─── /nastroj — Hans si najde LLM nástroj pro dílo (HANS_TOOLSCOUT_V1) ────────
def _cmd_nastroj(handler, name, args) -> str:  # HANS_TOOLSCOUT_V1
    """/nastroj — stav návrhů; /nastroj <téma> = najdi nástroj pro doménu;
    /nastroj schválit N = schval + stáhni; /nastroj zamítnout N."""
    import threading as _th
    from scripts import hans_toolscout as ts
    cfg = getattr(handler, "config", {}) or {}
    db = _cc._recall_db(handler)
    a = (args or "").strip()
    low = a.lower()

    def _fmt(props) -> str:
        if not props:
            return ""
        _F = {"coexist": "vejde se vedle chatu", "on_demand": "jen samostatně",
              "too_big": "nevejde se", "unknown": "?"}
        out = []
        for p in props:
            out.append("#%d [%s] %s %s (~%s GB, %s, %s stažení)\n   %s\n   %s" % (
                p["id"], p["status"], p["tool_name"], p["size_tag"], p["est_gb"],
                _F.get(p["fit"], p["fit"]), p["pulls"],
                (p.get("rationale") or "")[:180], p["url"]))
        return "\n".join(out)

    # schválit / zamítnout
    m = re.match(r"(schv[áa]l\w*|zam[íi]t\w*|odm[íi]t\w*)\s+(\d+)", low)
    if m:
        pid = int(m.group(2))
        store = ts.ToolStore(db)
        p = store.get(pid)
        if not p:
            return "Návrh č. %d neznám, pane." % pid
        if m.group(1).startswith(("zam", "odm")):
            store.set_status(pid, "rejected")
            return "Zamítnuto, pane. %s nebudu stahovat." % p["tool_name"]
        # schválit → pull na PC. HANS_TOOLSCOUT_PULL_TAG_V1: stáhni s KONKRÉTNÍ
        # velikostí (tool_name:size_tag), jinak `ollama pull qwen2.5-coder` vezme
        # default (7b) místo navržených 14b.
        store.set_status(pid, "approved")
        _pull = p["tool_name"] + (":" + p["size_tag"]
                                  if p.get("size_tag") else "")
        res = ts.pull_model(cfg, _pull)
        if res.get("ok"):
            # HANS_TOOLSCOUT_VERIFY_V1 — stav zůstává `approved`. `pull_model`
            # spouští stahování ODPOJENĚ a vrací ok už při „started", takže
            # `installed` by tu byla domněnka, ne fakt. Povýší ho až noční
            # `verify_approved` podle `ollama list`.
            return ("Schváleno, pane. Stahuji %s na počítač — %s. Až doběhne, "
                    "ověřím si, že skutečně dorazil, a pak ho použiji pro "
                    "dílo." % (_pull, res["detail"]))
        return ("Schválil jsem %s, ale stažení jsem nespustil: %s"
                % (_pull, res.get("detail", "")))

    # stav / výpis
    if not a or low in ("stav", "status", "seznam"):
        store = ts.ToolStore(db)
        pend = store.list("pending")
        if pend:
            return "Mé návrhy nástrojů, pane:\n" + _fmt(pend) + \
                "\n\n(/nastroj schválit N nebo zamítnout N)"
        allp = store.list()
        if allp:
            return "Aktuálně nemám čekající návrh. Poslední:\n" + _fmt(allp[:3])
        return ("Zatím jsem žádný nástroj nenavrhl, pane. Napiš /nastroj <téma> "
                "a prozkoumám vhodné modely (např. /nastroj Design).")

    # /nastroj <téma> → scout na pozadí (síť + LLM)
    topic = a

    def _scout():
        try:
            r = ts.propose_tool(cfg, db, topic)
            _cc._log.info("/nastroj %s → %s", topic, r.get("status"))
        except Exception as _e:
            _cc._log.warning("/nastroj scout selhal: %s", _e)
    _th.Thread(target=_scout, daemon=True).start()
    return ("Prozkoumám vhodné nástroje pro „%s“, pane, chvíli to potrvá. "
            "Pak zadej /nastroj a ukážu, co jsem našel." % topic)


# ─── /vhledy — Hansovy sebe-vhledy (HANS_SELF_INSIGHT_V1) ────────────────────
def _cmd_vhledy(handler, name, args) -> str:  # HANS_SELF_INSIGHT_V1
    """/vhledy — co si Hans všiml ve vlastních datech (offline_windows,
    game_mode). Podklady = nightly LLM analýza (deepseek-r1 → hans-czech).
    /vhledy teď = spusť run hned (bez ohledu na kadenci)."""
    try:
        from scripts.hans_self_insight import latest_insights, run_analysis
    except Exception as _e:
        return "Sebe-vhledy nejsou dostupné, pane. (%s)" % _e
    cfg = getattr(handler, "config", {}) or {}
    dbp = (cfg.get("diary_db")
           or (cfg.get("hans_idle", {}) or {}).get("diary_db")
           or "data/hans_diary.db")
    args_s = (args or "").strip().lower()

    if args_s in ("ted", "teď", "run", "nyní", "nyni"):
        # Vyžádaný okamžitý run (mimo weekly kadenci). Blokuje ~1-2 min.
        # HANS_SELF_INSIGHT_ROTATE_MANUAL_V1 (20.7.) — dřív běžel VŽDY
        # DEFAULT_LENS (offline_game) → nové lens (social/learning/creative/
        # physical) byly ručně nedosažitelné. Teď vybere DALŠÍ v rotaci
        # (nejdéle neběžel jde první) → opakované /vhledy teď procyklí všechny.
        import threading as _th
        try:
            from scripts.hans_self_insight import _next_lens
            _lens = _next_lens(dbp, cfg)
        except Exception:
            _lens = None
        def _bg(_l=_lens):
            try:
                if _l:
                    run_analysis(dbp, cfg, lens_id=_l, force=True)
                else:
                    run_analysis(dbp, cfg, force=True)
            except Exception:
                pass
        _th.Thread(target=_bg, daemon=True).start()
        _lbl = (" (perspektiva: %s)" % _lens) if _lens else ""
        return ("Spouštím rozbor svých vlastních dat na pozadí, pane%s. "
                "Za pár minut zkuste /vhledy znovu — objeví se v seznamu."
                % _lbl)

    ins = latest_insights(dbp, limit=int(args_s) if args_s.isdigit() else 3)
    if not ins:
        return ("Zatím jsem si o svých vlastních vzorcích nic nezapsal, pane. "
                "Zkuste /vhledy teď — spustím rozbor.")
    import datetime as _dt
    lines = ["Co jsem si v poslední době všiml ve vlastních datech:"]
    for i, r in enumerate(ins, 1):
        when = _dt.datetime.fromtimestamp(r["ts"]).strftime("%d.%m. %H:%M")
        lines.append("")
        lines.append("── %s (%dd okno) ──" % (when, r["window_days"]))
        lines.append(r["insight_cs"])
    return "\n".join(lines)


# ─── /experiment — footgun s auto-resume (HANS_FOOTGUN_V1) ───────────────────
def _cmd_experiment(handler, name, args) -> str:
    """/experiment [minut] — spusť experiment: Hans si zapne herní mód na
    N minut (default 5), pak auto-resume. Neutrální deník záznam. Config
    gate `hans_experiment.enabled`."""
    try:
        from scripts.hans_footgun import experiment_run, status, is_running
    except Exception as _e:
        return "Experiment modul nedostupný, pane. (%s)" % _e
    cfg = getattr(handler, "config", {}) or {}
    args_s = (args or "").strip().lower()
    if args_s in ("stav", "status"):
        s = status()
        if s.get("active"):
            return ("Experiment běží: %d minut, zbývá %d minut."
                    % (s["duration_s"] // 60, s.get("remaining_s", 0) // 60))
        return "Aktuálně žádný experiment neběží."
    if is_running():
        s = status()
        return ("Experiment už běží — zbývá %d minut. Počkej na auto-resume."
                % (s.get("remaining_s", 0) // 60))
    # default 5 min
    dur_min = 5
    try:
        if args_s and args_s.isdigit():
            dur_min = int(args_s)
    except Exception:
        pass
    r = experiment_run(cfg, duration_s=dur_min * 60)
    if not r.get("ok"):
        return "Experiment nespuštěn: %s" % r.get("message", "?")
    return ("Spouštím experiment: zapínám si herní mód na %d minut. "
            "Auto-resume je zajištěn — i kdybych se během toho nemohl "
            "vyjádřit, systém mě vrátí." % dur_min)


# ─── /anomalie — týdenní algoritmické odchylky (HANS_ANOMALY_V1) ─────────────
def _cmd_anomalie(handler, name, args) -> str:
    """/anomalie — poslední detekované odchylky ve tvém chování; /anomalie teď = spusť."""
    try:
        from scripts.hans_anomaly import latest_anomaly_note, run_once
    except Exception as _e:
        return "Anomaly modul nedostupný. (%s)" % _e
    cfg = getattr(handler, "config", {}) or {}
    dbp = (cfg.get("diary_db")
           or (cfg.get("hans_idle", {}) or {}).get("diary_db")
           or "data/hans_diary.db")
    args_s = (args or "").strip().lower()
    if args_s in ("ted", "teď", "run", "nyní", "nyni"):
        import threading as _th
        def _bg():
            try:
                run_once(dbp, cfg)
            except Exception:
                pass
        _th.Thread(target=_bg, daemon=True).start()
        return ("Spouštím detekci odchylek na pozadí, pane. Za pár desítek "
                "sekund zkuste /anomalie znovu.")
    row = latest_anomaly_note(dbp)
    if not row:
        return ("Zatím jsem si v posledních týdnech ničeho neobvyklého "
                "nevšiml, pane. /anomalie teď spustí kontrolu.")
    import datetime as _dt
    when = _dt.datetime.fromtimestamp(row["ts"]).strftime("%d.%m. %H:%M")
    return "Poslední odchylky (%s):\n\n%s" % (when, row["note"])

# ROZDELENI_PRIKAZU_V1 — až na konci, viz hlavička
from scripts import chat_commands as _cc  # noqa: E402
