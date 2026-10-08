"""Obsluhy příkazů přesunuté z `scripts/chat_commands.py` (ROZDELENI_PRIKAZU_V1).

Text funkcí je beze změny; jména původního modulu se čtou přes `_cc.` až při
volání. Registrace příkazů (`register(...)`) zůstává v původním modulu.
Import původního modulu je na KONCI souboru (kruhový import oběma směry).
"""
from __future__ import annotations

import re
import time

def _cmd_kalendar(handler, name, args) -> str:  # HANS_CALENDAR_V1
    """/kalendar — nadcházející události z Proton kalendáře; /kalendar sync."""
    cfg = getattr(handler, "config", {}) or {}
    try:
        from scripts.hans_calendar import CalendarStore, is_enabled, people_map
    except Exception:
        return "/kalendar: modul nedostupný."
    person = (name or "").lower()
    if not is_enabled(cfg) or person not in people_map(cfg):
        # HANS_CALENDAR_NO_CONFIG_HINT_V1 (15. 9.) — vetu o nastaveni dostaval
        # KAZDY bez napojeneho kalendare, i cizi clovek ("přidejte ho do
        # config.calendar.people"). Navod patri spravci, ne tazateli.
        return "Váš kalendář zatím nemám napojený, pane."
    try:
        dbp = (cfg.get("diary", {}) or {}).get("db_path", "data/hans_diary.db")
        st = CalendarStore(cfg, dbp)
        if (args or "").strip().lower().startswith("sync"):
            n = st.sync()
            return (f"✓ Kalendář synchronizován — {n} událostí." if n >= 0
                    else "⚠ Synchronizace se nezdařila (síť/odkaz).")
        evs = st.upcoming(person, hours=24 * 14, limit=15)
        if not evs:
            return "V nejbližších dvou týdnech nemám ve vašem kalendáři žádnou událost."
        lines = ["📅 Nadcházející:"]
        for e in evs:
            loc = f" ({e['location']})" if e.get("location") else ""
            lines.append(f"  • {st._fmt_when(e)} — {e['summary']}{loc}")
        return "\n".join(lines)
    except Exception as ex:
        return f"/kalendar: chyba ({ex})"


# ─── /rozvrh — Hansův behaviorální rozvrh (HANS_SCHEDULE_V1) ─────────────────
def _cmd_rozvrh(handler, name, args) -> str:  # HANS_SCHEDULE_V1
    """/rozvrh — kompletní Hansův rozvrh autonomních rutin (kdy naposledy tikly,
    zaostávají-li). Doplněk k /zdravi, který ukazuje jen zaostávající."""
    try:
        from scripts.hans_schedule import ScheduleStore
        import os, time
        db = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "data", "hans_diary.db")
        st = ScheduleStore(db)
        rows = st.all()
        if not rows:
            return "Rozvrh je prázdný, pane."
    except Exception as e:
        return f"/rozvrh: chyba ({e})"

    labels = {
        "nightly_analytics":  "Noční analytika",
        "morning_reflection": "Ranní reflexe",
        "study_tick":         "Studijní tick",
        "curiosity_tick":     "Zvědavý tick",
        "calendar_sync":      "Sync Proton kalendáře",
        "catchup_drain":      "Dohnání odložených čtení",
    }
    now = time.time()
    lines = ["📋 Můj rozvrh (autonomní rutiny):"]
    stale_list = st.stale_list(now)
    stale_names = {s["name"] for s in stale_list}
    for r in rows:
        lbl = labels.get(r["name"], r["name"])
        last_ts = r["last_run_ts"]
        if not last_ts:
            when = "ještě neproběhla"
        else:
            age_h = (now - last_ts) / 3600
            if age_h < 1:
                when = f"před {age_h*60:.0f} min"
            elif age_h < 24:
                when = f"před {age_h:.1f}h"
            else:
                when = f"před {age_h/24:.1f} dny"
        gap_h = r["expected_gap_s"] / 3600
        marker = "⚠️ " if r["name"] in stale_names else "  "
        skip = ""
        if r["last_skip_reason"] and not r["last_run_ok"]:
            skip = f" [posl. skip: {r['last_skip_reason']}]"
        # HANS_SCHEDULE_LAST_OK_UI_V1 (18.8.) — „kdy naposledy BĚŽELA" a „kdy
        # naposledy USPĚLA" se u zaseklé rutiny rozcházejí: studium se 18.8.
        # hlásilo každou minutu, ale 5 h nic nenastudovalo. Bez tohohle by řádek
        # tvrdil „před 1 min" a vedle něj svítilo ⚠️, což mate.
        _ok_ts = r.get("last_ok_ts") or 0
        if _ok_ts and last_ts and (last_ts - _ok_ts) > 60:
            _oh = (now - _ok_ts) / 3600
            _ow = (f"před {_oh*60:.0f} min" if _oh < 1 else
                   (f"před {_oh:.1f}h" if _oh < 24 else f"před {_oh/24:.1f} dny"))
            skip += f" [posl. ÚSPĚCH: {_ow}]"
        elif not _ok_ts and last_ts:
            # Ne „nikdy neuspěla" — úspěchy se sledují teprve od migrace
            # (HANS_SCHEDULE_LAST_OK_V1), starší běhy o sobě data nemají.
            skip += " [bez zaznamenaného úspěchu]"
        enabled = "" if r["enabled"] else " (vypnuto)"
        lines.append(f"{marker}• {lbl} — {when} (max gap {gap_h:.0f}h){skip}{enabled}")
    if stale_list:
        lines.append("")
        lines.append(f"⚠️ Zaostává: {len(stale_list)} z {len(rows)} rutin.")
    else:
        lines.append("")
        lines.append("✅ Vše běží podle plánu.")
    return "\n".join(lines)


# ─── /sleep — manuální override spánku (SLEEP_TOGGLE_V1) ──────────────
def _cmd_sleep(handler, name, args) -> str:
    """/sleep — toggle Hansova spánku.
    Vzhůru → uspat (tichý). Spí → probudit (mluvící).
    Override drží přes okno, expiruje na přirozené opačné hraně."""
    _hi = getattr(handler, "_hans_idle", None)
    if _hi is None:
        return "/sleep: idle objekt není dostupný."
    _rt = getattr(_hi, "_routine", None)
    if _rt is None:
        return "/sleep: routine objekt není dostupný."
    if not hasattr(_rt, "set_manual_sleep"):
        return "/sleep: set_manual_sleep chybí (patch aplikován?)."
    try:
        currently_sleeping = bool(getattr(_rt, "_sleeping", False))
        new_state = not currently_sleeping
        _rt.set_manual_sleep(new_state)
        return "Usínám." if new_state else "Probudil jsem se."
    except Exception as _e:
        return "/sleep selhal: %s" % _e


def _cmd_herni(handler, name, args) -> str:
    """/herni [zap|vyp] — herní mód. ZAP: Hans uvolní modely z VRAM a přestane
    používat Ollamu (volná grafika pro hru). VYP: mozek zase k dispozici. Bez
    argumentu přepíná."""
    from scripts.ollama_client import set_game_mode, game_mode_on
    _hi = getattr(handler, "_hans_idle", None)
    cfg = getattr(_hi, "config", None) if _hi else None
    a = (args or "").strip().lower()
    if a in ("stav", "status"):
        return ("Herní mód je ZAPNUTÝ (grafika volná, mozek nepoužívám)."
                if game_mode_on() else "Herní mód je vypnutý.")
    if a in _cc._HERNI_ON:
        target = True
    elif a in _cc._HERNI_OFF:
        target = False
    else:
        target = not game_mode_on()   # toggle
    res = set_game_mode(target, config=cfg)
    if "error" in res:
        return "/herni selhal: %s" % res["error"]
    if target:
        return ("Herní mód ZAP — uvolnil jsem %d model(ů) z grafické paměti a "
                "mozek teď nepoužívám. Až dohraješ: /herni vyp."
                % res.get("unloaded", 0))
    return "Herní mód VYP — mozek je zase k dispozici."


# ─── /blink — mrknutí animatronickými víčky (HANS_EYE_BLINK_V1) ──────────────
def _cmd_blink(handler, name, args) -> str:
    fn = getattr(handler, "_blink_eyes", None)
    if not callable(fn):
        return "Oči teď nemám po ruce, pane."
    try:
        ok = fn()
    except Exception as e:
        return "Mrknutí se nepovedlo, pane: %s" % e
    return "*mrkl jsem*" if ok else "Víčka teď nejsou aktivní, pane."


# ─── co hraje? — ŽIVÁ kontrola Kodi (HANS_LIVE_PLAYBACK_QUERY_V1) ────────────
def _cmd_hraje(handler, name, args) -> str:
    """co hraje / co se přehrává — Hans zkontroluje ŽIVÝ stav Kodi (ne deník);
    nic nehraje → navrhne film. Funguje i přes Telegram (jde přes send_chat_message).
    Pozn.: Kodi (252) je samostatné zařízení, funguje i když PC spí."""
    cfg = getattr(handler, "config", {}) or {}
    _hi = getattr(handler, "_hans_idle", None)
    kodi = getattr(_hi, "kodi", None) if _hi is not None else None
    if kodi is None:
        try:
            from scripts.kodi_client import KodiClient
            kodi = KodiClient(cfg)
        except Exception:
            return "Bohužel se teď nemohu spojit s přehrávačem, pane."
    # 1) živý stav
    try:
        now = kodi.get_now_playing()
    except Exception as e:
        _cc._log.warning("hraje: get_now_playing selhal: %s", e)
        now = None
    if now:
        title = (now.get("title") or now.get("label") or "").strip()
        show = (now.get("showtitle") or "").strip()
        ep, se = now.get("episode"), now.get("season")
        if show and ep:
            base = f"seriál „{show}\""
            if se:
                base += f" (řada {se}, díl {ep})"
            elif ep:
                base += f" (díl {ep})"
            if title and title != show:
                base += f" – {title}"
            desc = base
        else:
            yr = now.get("year")
            desc = f"„{title}\"" + (f" ({yr})" if yr else "")
        return f"Právě se přehrává {desc}, pane."
    # 2) nic nehraje → návrh filmu
    names = list(getattr(_hi, "_present_names", []) or []) if _hi is not None else []
    if not names and name:
        names = [name]
    m = None
    if _hi is not None:
        try:
            m = _hi._pick_next_film(names, cfg.get("film_suggest", {}) or {})
        except Exception as e:
            _cc._log.warning("hraje: _pick_next_film selhal: %s", e)
    if m is None:
        try:
            m = kodi.pick_suggestion(prefer_genres=kodi.favorite_genres())
        except Exception:
            m = None
    if m:
        mt = (m.get("title") or m.get("label") or "").strip()
        yr = m.get("year")
        return (f"Teď nic nehraje, pane. Mohl bych navrhnout „{mt}\""
                + (f" ({yr})" if yr else "")
                + " — stačí říct a pustím to.")
    return "Teď se nic nepřehrává, pane, a vhodný návrh se mi teď nepodařilo najít."


def _cmd_misto(handler, name, args) -> str:
    """/misto — model domova (kde Hans je). Bez argumentu vypíše model.
    /misto okno <text> | dvere <text> | vedle <text> | mistnost <text> |
    rozlozeni <text> | pozn <text> = přidá fakt. /misto smaz <id> = odebere."""
    cfg = getattr(handler, "config", {}) or {}
    db = (cfg.get("diary_db")
          or (cfg.get("hans_idle", {}) or {}).get("diary_db")
          or "data/hans_diary.db")
    try:
        from scripts.hans_place import PlaceStore
        store = PlaceStore(cfg, db)
    except Exception as e:
        return "Modul místa nedostupný: %s" % e

    parts = (args or "").strip().split(maxsplit=1)
    sub = parts[0].lower() if parts else ""
    rest = parts[1].strip() if len(parts) > 1 else ""

    # namaluj domov (HANS_PLACE_PAINT_V1) — Hans vyrenderuje, jak si dům představuje
    if sub in {"obraz", "namaluj", "render", "nakresli"}:
        import threading as _t
        try:
            from scripts import hans_art
        except Exception as e:
            return "Malování není dostupné: %s" % e
        if not hans_art.comfy_available(cfg):
            return ("Bohužel, pane — výtvarná dílna (ComfyUI na PC) teď neběží, "
                    "tak domov namalovat nemohu. Zkuste to, až bude PC vzhůru.")
        if not store.get_facts():
            return ("Nemám zatím žádný model domova, pane — nejdřív mi ho popište "
                    "(/misto …) nebo nechte fotky v data/room_photos/.")

        def _worker():
            try:
                # Textový render (hezčí výsledek než img2img z reálné fotky).
                # paint_home_from_photo zůstává v hans_art pro budoucí použití.
                hans_art.render_home_now(cfg, db)
            except Exception as _e:
                _cc._log.warning("/misto obraz render selhal: %s", _e)
        _t.Thread(target=_worker, daemon=True).start()
        return ("Dám se do toho, pane — maluji, jak si představuji svůj domov. "
                "Za chvíli se objeví na nástěnce (Co Hans namaloval). "
                "Chat může být asi minutu zaneprázdněný.")

    # smazání faktu
    if sub in _cc._MISTO_DEL:
        if not rest.isdigit():
            return "Uveďte id: /misto smaz <id> (viz /misto)."
        ok = store.remove_fact(int(rest))
        return ("Fakt %s jsem odebral, pane." % rest if ok
                else "Ten fakt se nepodařilo odebrat (špatné id?).")

    # přidání faktu
    if sub in _cc._MISTO_SUBS:
        if not rest:
            return "Doplňte text: /misto %s <popis>." % sub
        cat = _cc._MISTO_SUBS[sub]
        fid = store.add_fact(cat, rest, source="user")
        if cat == "room":
            return "Zapsal jsem, že jsem v místnosti: %s." % rest
        return "Zapsal jsem fakt o místě [%s] (id %s)." % (
            _cc._MISTO_CAT_LABEL.get(cat, cat), fid)

    if sub:
        return ("Neznámá část. Použij: /misto [mistnost|okno|dvere|vedle|"
                "rozlozeni|pozn] <text>, /misto smaz <id>, nebo /misto pro výpis.")

    # výpis modelu
    facts = store.get_facts()
    if not facts:
        return ("O svém místě zatím nic nevím, pane. Můžete mi to popsat: "
                "/misto mistnost <text>, /misto okno <text>, /misto vedle <text> … "
                "Nebo nechte širší fotku místnosti ve složce data/room_photos/ "
                "(udělám si z ní představu při startu).")
    by_cat: dict = {}
    for f in facts:
        by_cat.setdefault(f["category"], []).append(f)
    out = ["Můj model domova (kde jsem):"]
    order = ["room", "window", "door", "neighbor", "layout", "note", "mental_map"]
    for cat in order:
        for f in by_cat.get(cat, []):
            out.append("   [%d] %s: %s" % (
                f["id"], _cc._MISTO_CAT_LABEL.get(cat, cat), f["content"]))
    out.append("")
    out.append("Přidat: /misto okno <text> (i dvere/vedle/mistnost/rozlozeni/pozn)  "
               "|  smazat: /misto smaz <id>  |  namalovat domov: /misto obraz")
    return _cc.NL_RUNTIME.join(out)


# ─── /rezim — vlastní provozní stav (HANS_REZIM_SHORTCIRCUIT_V1) ────────────
def _cmd_rezim(handler, name, args) -> str:
    """Spím/bdím + hlídání — přímo ze stavu, ŽÁDNÝ LLM.

    Doloženo 7.8.: i když měl model fakt „teď: jsem vzhůru" v promptu (blok
    894 zn), odpověděl „Ano, jsem v režimu spánku." Fakta v promptu tenhle
    případ neuhlídají — proto deterministická odpověď, vzor `/vzpominka`.
    """
    _sleeping = None
    try:
        # ⚠️ `_routine` drží `hans_idle`, ne handler (vzorec TIME_AWARENESS_V1).
        _hi = getattr(handler, "_hans_idle", None)
        _rt = getattr(_hi, "_routine", None) if _hi else None
        if _rt is not None:
            _sleeping = bool(getattr(_rt, "_sleeping", False))
    except Exception:
        pass
    _guard = None
    try:
        import json as _js
        import os as _os
        _gp = "data/.hans_guard"
        if _os.path.exists(_gp):
            with open(_gp, encoding="utf-8") as _f:
                _guard = bool((_js.load(_f) or {}).get("armed"))
        else:
            _guard = False
    except Exception:
        pass
    if _sleeping is None and _guard is None:
        return "Svůj stav teď nedokážu spolehlivě zjistit, pane."
    out = []
    if _sleeping is not None:
        out.append("Ne, pane, nespím — jsem vzhůru a v běžném provozu."
                   if not _sleeping else
                   "Ano, pane, jsem v nočním režimu (spánek).")
    # HANS_STRANGER_HOUSEHOLD_V1 — cizímu neříkat, zda je dům hlídaný.
    try:
        from scripts.cz_names import is_known_person as _ikp_r
        if not (bool(name) and _ikp_r(name)):
            _guard = None
    except Exception:
        _guard = None
    if _guard is not None:
        out.append("Hlídací režim mám %s." % ("zapnutý" if _guard else "vypnutý"))
    return " ".join(out)


def _cmd_film(handler, name, args) -> str:  # HANS_RECALL_FILM_V1
    from scripts.hans_recall import films_watched_answer, films_liked_answer
    # HANS_CAMERA_STRANGER_V1 — cizimu kameru nenabizet: rovnou vypis filmu.
    _znamy = True
    try:
        from scripts.cz_names import is_known_person as _ikp
        _znamy = (not name) or _ikp(name)
    except Exception:
        pass
    if _znamy and _cc._VIDEL_HOLY_PAT.search(str(args or "")):
        _cc._log.info("HANS_VIDEL_UPRESNI_V1: hole 'videl' \u2192 upresnujici otazka")
        return "Myslíte film, který jsem viděl, nebo co jsem zahlédl kamerou?"
    # HANS_FILM_OPINION_ANAFORA_V1 — „který z nich…“ po výpisu filmů:
    # názor jen k filmům z TOHO výpisu, ne obecný žebříček.
    if _cc.je_anafora_obliby(args or "") and not _cc._je_dotaz_na_oblibu_filmu(args or ""):
        try:
            from scripts.hans_thread import recent_turns as _rt
            from scripts.hans_recall import films_liked_among
            _tit = _cc.posledni_vypis_filmu(_rt(handler, name))
            if _tit:
                _cc._log.info("HANS_FILM_OPINION_ANAFORA_V1: anafora obliby → "
                          "názor k %d filmům z výpisu", len(_tit))
                return films_liked_among(_cc._recall_db(handler), _tit)
        except Exception as _ae:
            _cc._log.debug("anafora obliby selhala: %s", _ae)
    # HANS_FILM_SIMILAR_V1 — „něco podobného jako X“ je užší než doporučení
    # („doporuč mi film podobný Duně“ sedí na oba vzory) → jde první.
    if _cc._je_zadost_o_podobny_film(args or ""):
        return _cc._podobny_film(handler, name, _znamy, args or "")
    # HANS_FILM_RECOMMEND_V1 — žádost o doporučení má přednost před oblibou
    # i výpisem (výpis zhlédnutých filmů na ni neodpovídá).
    if _cc._je_zadost_o_doporuceni_filmu(args or ""):
        _dop = _cc._doporuc_film(handler, name, _znamy)
        if _dop:
            return _dop
    # HANS_FILM_OPINION_ANSWER_V1 — nejdriv obliba, teprve pak vypis.
    if _cc._je_dotaz_na_oblibu_filmu(args or ""):
        _ob = films_liked_answer(_cc._recall_db(handler))
        if _ob:
            _cc._log.info("HANS_FILM_OPINION_ANSWER_V1: dotaz na oblibu \u2192 "
                      "odpovidam z vlastnich nazoru, ne vypisem")
            return _ob
    # HANS_STRANGER_HOUSEHOLD_V1 — co se doma sledovalo (výpis i počet) je
    # chod domácnosti. Názor na filmy a doporučení výš zůstávají otevřené.
    if not _znamy:
        _cc._log.info("HANS_STRANGER_HOUSEHOLD_V1: výpis filmů od neznámého (%s) "
                  "odmítnut", name)
        return "O tom mluvím jen se svou domácností."
    # HANS_COUNT_FILMS_BOOKS_V1 — „kolik“ chce POCET, ne vypis.
    if _cc._KOLIK_RE.search(str(args or "")):
        _p = _cc._pocet_filmu(handler)
        if _p:
            return _p
    out = films_watched_answer(_cc._recall_db(handler), args or "")
    return out or "Nepodařilo se mi teď nahlédnout do deníku, pane."


def _cmd_hlidej(handler, name, args) -> str:
    from scripts import hans_guard as g
    a = (args or "").strip().lower()
    # NL cesta posílá CELOU větu jako args → „vypni hlídání" nesmí režim
    # omylem ZAPNOUT. Rozhoduje záměr ve větě, ne přesná shoda.
    if re.search(r"\b(stop|vypni|vypnout|konec|off|zru[šs])\b", a):
        g.disarm()
        return "Hlídání jsem vypnul, pane. Snímky už posílat nebudu."
    if re.search(r"\b(stav|status)\b", a):
        return g.status_text()

    cfg = getattr(handler, "config", {}) or {}
    tg = getattr(handler, "telegram", None)
    if tg is None or not getattr(tg, "enabled", False):
        return ("Hlídat mohu, ale nemám kam poslat snímky — Matrix není "
                "zapojený, pane. Bez něj by poplach nikdo neviděl.")
    g.arm(by=name or "")
    _cc._guard_camera_down(handler)
    c = (cfg.get("guard", {}) or {})
    return ("Hlídám, pane. Při pohybu nebo náhlé změně světla pošlu snímek "
            "na Matrix (nejvýš jednou za %d s, do %d snímků denně). "
            "Postupné rozednívání poplach nespustí. Kamera zůstane namířená "
            "do místnosti i v noci. Vypnout: /hlidej stop."
            % (int(c.get("cooldown_s", 60)), int(c.get("max_per_day", 60))))


def _cmd_preloz(handler, name, args) -> str:
    from scripts import hans_translate as ht
    a = (args or "").strip().lower()
    # NL cesta posílá CELOU větu → „jak jde ten překlad" se nesmí tvářit
    # jako povel spustit další (vzor _cmd_hlidej).
    if re.search(r"\b(seznam|v[ýy]pis|p[řr]elo[žz]en[éeý])\b"
                 r"|co\s+(jsi|u[žz]|v[šs]echno)\s+[\w\s]{0,25}?p[řr]elo[žz]"
                 r"|kter[ée]\s+[\w\s]{0,25}?p[řr]elo[žz]", a):
        return ht.seznam_text(_cc.cfg_of(handler))
    # ⚠️ Vzory MUSÍ počítat s psaním BEZ DIAKRITIKY — uživatel píše z mobilu.
    # Doloženo 28.8.: „uz mas hotovy preklad?" se k obsluze dostalo, ale
    # `hotov[oý]` neobsahovalo prosté „y", takže by to místo hlášení stavu
    # SPUSTILO DALŠÍ PŘEKLAD. nl_patterns se skládají i bez diakritiky
    # (nl_fold), tahle větev uvnitř obsluhy ne — proto tu jsou obě podoby.
    if re.search(r"\b(stav|status|hotovo|hotov[oýy])\b|jak\s+(to\s+)?(jde|pokra[čc]uje|dopadl)"
                 r"|u[žz]\s+(to\s+)?(je\s+)?(hotov|dod[ěe]l)"
                 r"|(m[áa][šs]|je)\s+[\w\s]{0,12}?hotov", a):
        return ht.stav_text()
    cfg = getattr(handler, "config", {}) or {}
    if not (cfg.get("translate", {}) or {}).get("enabled", True):
        return "Překládání dokumentů mám vypnuté, pane."
    return ht.spust_na_pozadi(cfg, handler)


def _cmd_vypnipc(handler, name, args) -> str:
    cfg = getattr(handler, "config", {}) or {}
    try:
        from scripts import pc_remote
    except Exception:
        return "Na počítač teď nedosáhnu, pane."
    if not pc_remote.enabled(cfg):
        return ("Vzdálený přístup k počítači nemám povolený "
                "(pc_remote.enabled), pane — vypnout ho neumím.")
    if not _cc._pc_ping(cfg):
        return "Počítač je už teď vypnutý (neodpovídá), pane. Nic nedělám."
    out = pc_remote.run(cfg, "sudo -n systemctl poweroff", timeout=10)
    if out is None:
        # poweroff často utne SSH spojení dřív, než stihne vrátit výstup —
        # proto None NEznamená selhání; rozhodne až ping.
        _cc._log.info("/vypnipc: poweroff bez výstupu (SSH nejspíš utnuto)")
    import time as _t
    for _ in range(10):
        _t.sleep(3)
        if not _cc._pc_ping(cfg):
            try:
                db = _cc._recall_db(handler)
                import sqlite3 as _sql
                c = _sql.connect(db, timeout=5.0)
                c.execute("INSERT INTO diary (ts, event_type, title, note) "
                          "VALUES (?,?,?,?)",
                          (_t.time(), "pc_shutdown", "Vypnutí PC na povel",
                           "Na požádání jsem vypnul počítač."))
                c.commit()
                c.close()
            except Exception:
                pass
            return ("Počítač je vypnutý, pane. Můj mozek tím usnul — "
                    "ráno ho probudím, nebo si řekněte o /wol.")
    return ("Poslal jsem počítači povel k vypnutí, ale ještě odpovídá, pane. "
            "Možná se vypíná pomalu — nebo se něco vzpírá.")


def _cmd_vypnipc_slash(handler, name, args) -> str:
    """HANS_SHUTDOWN_WAIT_WORK_V1 (14. 9.) — `/vypnipc` počká, až PC dodělá
    rozpracované, a vypne ho tiše; `/vypnipc hned` vypne okamžitě.
    `_cmd_vypnipc` zůstává vykonavatelem OKAMŽITÉHO vypnutí — volá ho
    i `pc_deferred_shutdown.tick`, až nastane klid."""
    if "hned" in (args or "").lower():
        return _cc._cmd_vypnipc(handler, name, args)
    cfg = getattr(handler, "config", {}) or {}
    try:
        from scripts import pc_remote
        if not pc_remote.enabled(cfg):
            return _cc._cmd_vypnipc(handler, name, args)   # táž hláška o zákazu
    except Exception:
        return "Na počítač teď nedosáhnu."
    if not _cc._pc_ping(cfg):
        return "Počítač je už teď vypnutý (neodpovídá). Nic nedělám."
    from scripts.pc_deferred_shutdown import request, ACK
    request(person=name or "", note="/vypnipc")
    return ACK


def _cmd_router(handler, name, args) -> str:
    if not _cc._router_smi(name):
        return "Stav domácí sítě vám bohužel říct nemohu."
    cfg = getattr(handler, "config", {}) or {}
    try:
        from scripts import hans_router
    except Exception as e:
        return "Na router teď nedosáhnu. (%s)" % e
    if not hans_router.enabled(cfg):
        return "Přístup na router mám vypnutý."
    return hans_router.summary(cfg)


def _cmd_vpnprepni(handler, name, args) -> str:
    if not _cc._router_smi(name):
        return "VPN server smí přepnout jen někdo z domácnosti."
    cfg = getattr(handler, "config", {}) or {}
    try:
        from scripts import hans_router
    except Exception as e:
        return "Na router teď nedosáhnu. (%s)" % e
    if not hans_router.enabled(cfg):
        return "Přístup na router mám vypnutý."
    return hans_router.switch_text(hans_router.switch_next(cfg, reason="povel"))


# ─── /zdravi — zdraví závislostí (HANS_HEALTH_V1) ────────────────────────────
def _cmd_zdravi(handler, name, args) -> str:  # HANS_HEALTH_V1
    """Živá probe závislostí (Ollama/ComfyUI/Kodi/STT/PC/disk). /zdravi vylec =
    zkusí self-heal zaseklé Ollamy."""
    try:
        from scripts import hans_health
    except Exception as _e:
        return "Nemohu teď zkontrolovat své zdraví, pane. (%s)" % _e
    cfg = getattr(handler, "config", {}) or {}
    do_heal = bool(args) and args.strip().lower() in (
        "vylec", "vyléč", "heal", "restart", "oprav")
    res = hans_health.run_health_check(cfg, heal=do_heal)
    health = res.get("health", {})
    if not health:
        return "Kontrola zdraví je vypnutá, pane."
    _lbl = {"ollama": "Mozek (Ollama)", "comfyui": "Malování (ComfyUI)",
            "kodi": "Televize (Kodi)", "stt": "Sluch (přepis)",
            "pc": "Počítač", "disk": "Disk", "camera": "Kamera",
            "schedule": "Rozvrh (autonomní rutiny)",
            # MATRIX_SYNC_HEARTBEAT_V1 — bez popisku by Hans hlásil holé
            # „matrix"; výpis jde přes health.items(), takže klíč bez labelu
            # projde, jen se nepřeloží.
            "matrix": "Most na telefon (Matrix)"}
    _ico = {"ok": "✅", "paused": "⏸️", "wedged": "⚠️", "down": "❌",
            "unknown": "❔", "warn": "⚠️"}
    lines = ["Stav mých systémů, pane:"]
    for k, s in health.items():
        st = s.get("status", "unknown")
        lines.append("%s %s — %s" % (_ico.get(st, "❔"), _lbl.get(k, k),
                                     s.get("detail", st)))
    # HANS_SCHEDULE_V1 — zvlášť vypsat KTERÉ rutiny zaostávají (detail)
    sched_stale = ((health.get("schedule") or {}).get("stale")) or []
    if sched_stale:
        lines.append("")
        lines.append("Zaostávající rutiny:")
        for x in sched_stale:
            reason = " [%s]" % x["last_skip_reason"] if x["last_skip_reason"] else ""
            lines.append("  • %s — %.1fh po termínu (max %.1fh)%s"
                         % (x["name"], x["late_s"] / 3600,
                            x["expected_gap_s"] / 3600, reason))
    if res.get("healed"):
        lines.append("(Zaseklý mozek jsem zkusil restartovat.)")
    # KOLAC_EXAM_V1 (22.8.) — jak jsem obstál ve zkoušení. Patří to sem, ne
    # do zvláštního příkazu: je to údaj o vlastním stavu, jako mozek či disk.
    try:
        from scripts import kolac_exam as _ke
        _souhrn = _ke.souhrn(_cc._recall_db(handler) or "data/hans_diary.db")
        if _souhrn:
            lines.append("")
            lines.append(_souhrn)
    except Exception:
        pass
    return "\n".join(lines)


def _cmd_stop(handler, name, args) -> str:
    cfg = getattr(handler, "config", {}) or {}
    # HANS_PAINT_CANCEL_V1 — „stop“ do tří minut po zadání malby patří malbě
    # (doloženo 6. 10.: „stop“ po omylem zadaném obrazu zastavilo film v Kodi).
    try:
        from scripts import hans_heavy_queue as _hq
        if _hq.cerstva_malba(_cc._recall_db(handler), name or ""):
            return _cc._cmd_zrusmalbu(handler, name, args)
    except Exception as _ze:
        _cc._log.debug("stop → malba: %s", _ze)
    try:
        from scripts.kodi_client import KodiClient
        k = KodiClient(cfg)
        if not k.is_playing():
            return "Teď nic neběží, pane."
        now = ""
        try:
            np = k.get_now_playing()
            now = (np or {}).get("title") or ""
        except Exception:
            pass
        ok = k.stop_playback()
        if ok:
            return ("Zastaveno, pane%s." % ((" — „%s\"" % now) if now else ""))
        return "Zastavit se to nepodařilo, pane."
    except Exception as e:
        _cc._log.warning("/stop selhal: %s", e)
        return "K televizi se teď nedostanu, pane."


def _cmd_pauza(handler, name, args) -> str:
    cfg = getattr(handler, "config", {}) or {}
    try:
        from scripts.kodi_client import KodiClient
        k = KodiClient(cfg)
        if not k.is_playing():
            return "Teď nic neběží, pane."
        k.toggle_pause()
        return "Hotovo, pane."
    except Exception as e:
        _cc._log.warning("/pauza selhal: %s", e)
        return "K televizi se teď nedostanu, pane."


def _cmd_hledani(handler, name, args) -> str:   # HANS_WEBSHARE_CMD_V1
    """/hledani <film> [1080p|cz|…] — hledej na Webshare;
    /hledani stahni N — stáhni N-tý nález na PC; /hledani stav — jak jde stahování."""
    from scripts import hans_webshare as ws
    cfg = getattr(handler, "config", {}) or {}
    a = (args or "").strip()
    low = _cc._fold_diacritics(a.lower())

    if not (cfg.get("webshare", {}) or {}).get("enabled", True):
        return "Hledání na Webshare mám vypnuté, pane."

    # ── dotaz na STAV — NIC nespouští ──
    if re.search(r"^stav\b|jak\s+(to\s+)?(jde|pokracuje|vypada)|"
                 r"u[zs]\s+(je|to)\s+(stazen|hotov)|stahuje[sš]?\s*\?*$", low):
        try:
            return ws.stav_stahovani(cfg)
        except Exception as e:
            return "Ke stavu stahování se teď nedostanu, pane (%s)." % e

    if not ws.nastaveno(cfg):
        return ("K Webshare nemám přihlašovací údaje, pane — doplňte je v config.json "
                "do sekce `webshare` (username a password). Pak už budu hledat.")

    # ── stáhnout N-tý nález ──
    m = re.search(r"^(?:stahni|stah|vezmi|chci)\s*(?:c\.|cislo\s*)?(\d+)\s*$"
                  r"|^(\d+)\s*$", low)
    if m:
        cislo = int(m.group(1) or m.group(2))
        st = _cc._WS_STAV.get(name or "")
        if not st or (time.time() - st.get("ts", 0)) > _cc._WS_TTL_S:
            return ("Nemám čerstvý výpis, pane — zadejte nejdřív "
                    "/hledani <název> a pak číslo.")
        nalezy = st.get("nalezy") or []
        if not (1 <= cislo <= len(nalezy)):
            return "Pod číslem %d nic nemám, pane — vybral jsem %d nálezů." % (
                cislo, len(nalezy))
        p = nalezy[cislo - 1]
        try:
            # HANS_WEBSHARE_CMD_PI_V1 (4.9.) — přes rozcestník `stahni()`,
            # ne napřímo na PC. Velikost se PŘEDÁVÁ, protože bez ní nemá
            # kontrola volného místa na Pi co porovnávat a stahování by mohlo
            # zaplnit systémový disk Hanse.
            u = ws.stahni(cfg, p["ident"], p["nazev"], p.get("velikost") or 0)
        except Exception as e:
            return "Stáhnout se to nepodařilo, pane: %s" % e
        _cc._ws_zapis_denik(cfg, p, u)
        if u.get("kde") == "pi":
            return ("Stahuji „%s“ (%s) k sobě, pane — až to doběhne, přesunu "
                    "to na počítač. Volné připojení je pomalé, tak to nějakou "
                    "dobu potrvá; „jak jde to stahování“ vám řekne, kde jsem."
                    % (p["nazev"][:80], ws.velikost_str(p["velikost"])))
        return ("Stahuji „%s“ (%s) na počítač, pane. Zeptáte-li se "
                "„jak jde to stahování“, řeknu, kde to je." % (
                    p["nazev"][:80], ws.velikost_str(p["velikost"])))

    dotaz, filtr = _cc._ws_rozeber(a)
    if not dotaz:
        st = _cc._WS_STAV.get(name or "")
        if st and (time.time() - st.get("ts", 0)) <= _cc._WS_TTL_S:
            return "Naposledy jsem hledal „%s“:\n%s" % (
                st.get("dotaz"), ws.vypis(st["nalezy"], _cc._ws_kolik(cfg)))
        return ("Co mám na Webshare najít, pane? Např. /hledani Duna 2 1080p; "
                "pak /hledani stahni 1.")

    c = cfg.get("webshare", {}) or {}
    try:
        nalezy = ws.hledej(cfg, dotaz, limit=int(c.get("limit", 25)),
                           kategorie=str(c.get("kategorie", "video")),
                           razeni=str(c.get("razeni", "relevance")))
    except Exception as e:
        return "Na Webshare jsem se teď nedostal, pane: %s" % e
    if filtr:
        pred = len(nalezy)
        nalezy = ws.filtruj(nalezy, filtr)
        if not nalezy:
            return ("Na „%s“ jsem našel %d souborů, ale žádný ve filtru „%s“, pane. "
                    "Zkuste to bez něj." % (dotaz, pred, filtr))
    if not nalezy:
        # HANS_WEBSHARE_PRAZDNO_ROZLISIT_V1 (4.9.) — ROZLIŠIT dva různé důvody
        # prázdna. „Nic jsem nenašel“ je nepravda, když nálezy byly a jen
        # všechny vypadly na filtru nesmyslných názvů. Doloženo hned při
        # zapnutí filtru: dotaz „Duna“ → 25 nálezů, 25 přeskočeno, tedy
        # prázdný výpis a klamavá věta. Falešné „nic tam není“ je horší než
        # šum — uživatel podle něj přestane hledat.
        _psk0 = 0
        try:
            _psk0 = ws.preskoceno()
        except Exception:
            pass
        if _psk0:
            return ("Na „%s“ jsem našel %d souborů, ale u všech je název "
                    "nesmyslný (třeba „du49g84gij“), takže jsem je přeskočil, "
                    "pane. Chcete-li je přesto vidět, přepněte "
                    "webshare.preskoc_bez_jmena na false — nebo zkuste "
                    "přesnější název." % (dotaz, _psk0))
        return "Na „%s“ jsem na Webshare nic nenašel, pane." % dotaz
    _cc.zapamatuj_nalezy(name, dotaz, nalezy)
    # HANS_WEBSHARE_PRESKOCENO_HLASI_V1 (4.9.) — přeskočené nálezy se PŘIZNÁVAJÍ.
    # Filtr na nesmyslné názvy je užitečný, ale kdyby mazal tiše, uživatel by
    # nikdy nezjistil, že mu něco chybí — a u hledání je to zrovna ten druh
    # poruchy, který se pozná až po dlouhé době. Vypisuje se i to, jak filtr
    # vypnout, ať se k zahozeným dá dostat.
    _psk = 0
    try:
        _psk = ws.preskoceno()
    except Exception:
        pass
    hlava = "Našel jsem k „%s“%s (kvalitu odhaduji z názvu souboru):" % (
        dotaz, (" — filtr %s" % filtr) if filtr else "")
    _pozn = ("\n(%d nálezů jsem přeskočil, protože jejich název nic neříká — "
             "vypnout jde klíčem webshare.preskoc_bez_jmena.)" % _psk) if _psk else ""
    return "%s\n%s%s\n\nStáhnu který? Stačí /hledani stahni <číslo>." % (
        hlava, ws.vypis(nalezy, _cc._ws_kolik(cfg)), _pozn)

# ROZDELENI_PRIKAZU_V1 — až na konci, viz hlavička
from scripts import chat_commands as _cc  # noqa: E402
