"""Metody třídy `HansIdle` přesunuté z `scripts/hans_idle.py` (ROZDELENI_METOD_V1).

Text metod je beze změny; jména původního modulu se čtou přes `_hi.` až při
volání. `HansIdle` má třídu `FilmMixin` mezi předky, takže volání přes `self` platí dál.
Import původního modulu je na KONCI souboru (kruhový import oběma směry).
"""
from __future__ import annotations

from datetime import datetime
import random
import time


class FilmMixin:
    """Část třídy `HansIdle` — viz hlavička modulu."""

    def _divaci(self, okno_s: float = 1800.0) -> list:
        """HANS_DIVACI_V1 (30. 10.) — kdo asi kouká: právě viditelní první,
        pak ostatní z domácnosti viděni v posledních `okno_s` (na gauči kamera
        vidí jen nejbližšího). Jen v paměti procesu, nic se nezapisuje."""
        now = time.time()
        ted = [n for n in (getattr(self, "_present_names", None) or [])
               if n and n not in ("Unknown", "?", "...", "")]
        ok = float((self.config.get("film_suggest", {}) or {}).get("divaci_okno_s", okno_s))
        dal = sorted(((ts, n) for n, ts in (getattr(self, "_videni", None) or {}).items()
                      if n not in ted and now - ts <= ok), reverse=True)
        return ted + [n for _ts, n in dal]

    def _tv_avatar_clip(self):  # AVATAR_KODI_V1
        """Cesta k talkloop videu pro mluvící tvář na TV; None když vypnuto/chybí.
        Gate `film_suggest.avatar_to_kodi` (default true), klip z
        `hans_avatar.tv_clip` (default talkloop)."""
        if not self._fcfg().get("avatar_to_kodi", True):
            return None
        import os
        av = self.config.get("hans_avatar", {}) or {}
        clip = av.get("tv_clip") or "data/avatar/clips/hans_talkloop_00001.mp4"
        return clip if os.path.exists(clip) else None

    def _tv_face_image(self):  # AVATAR_KODI_IMAGE_V1 / HANS_TV_FACE_LATEST_V1 (17.7.)
        """Cesta k Hansově tváři (PNG) pro dialog na TV; None když chybí.
        Když je `hans_avatar.face_image` v configu, použij ji. Jinak vezmi
        NEJNOVĚJŠÍ `data/avatar/vN/idle.png` (analogicky `_av_static_idle` v
        display_renderer). Dřív hard-coded na v1 → Kodi měl starou tvář, i
        když paint_self vyrobil v2, v3, …"""
        import os
        # HANS_SEASONAL_AVATAR_V1 — o svatku (Halloween) sezonni podoba i na TV.
        try:
            from scripts.hans_seasonal import seasonal_avatar
            _sez = seasonal_avatar(self.config)
            if _sez:
                return _sez
        except Exception:
            pass
        av = self.config.get("hans_avatar", {}) or {}
        img = av.get("face_image")
        if img:
            return img if os.path.exists(img) else None
        # dynamicky nejnovější verze avatara
        import glob
        vers = []
        for d in glob.glob("data/avatar/v*"):
            try:
                vers.append((int(os.path.basename(d)[1:]), d))
            except ValueError:
                pass
        if not vers:
            return None
        img = os.path.join(max(vers)[1], "idle.png")
        return img if os.path.exists(img) else None

    def _person_pref_genres(self, person: str) -> list:
        """Žánry odvozené ze zájmů osoby (person_interests). [] když nic."""
        try:
            store = self._pinterest_store()
            ints = store.interests_for(person, limit=10) if store else []
        except Exception:
            return []
        out = []
        for it in ints:
            txt = (getattr(it, "interest", "") or "").lower()
            for keys, genre in self._INTEREST_GENRE_MAP:
                if genre not in out and any(k in txt for k in keys):
                    out.append(genre)
        return out

    def _person_watch_genres(self, names, top: int = 6) -> list:
        """FILM_PERSON_PREF_V2 — REÁLNÉ žánry z historie sledování přítomných osob
        (kodi_sessions). Silnější signál než zájmy. [] když nic / DB chybí."""
        names = [n for n in (names or []) if n]
        if not names:
            return []
        try:
            import sqlite3
            import re
            from collections import Counter
            dbp = self.config.get("kodi", {}).get("monitor_db",
                                                  "data/kodi_monitor.db")
            conn = sqlite3.connect(dbp)
            c = Counter()
            for name in set(names):
                rows = conn.execute(
                    # HANS_FILM_ZANRY_BEZ_TV_V1 (30. 10.) — bez živé televize: přenosy
                    # dávaly „sport“ na 1. místo (156 sezení, 0 filmů), ale tady se
                    # vybírá film z knihovny; u ostatních osob pořadí skoro stejné
                    "SELECT genre FROM kodi_sessions "
                    "WHERE persons LIKE ? AND genre!='' AND media_type!='channel'",
                    ('%"' + name + '"%',)).fetchall()
                for (g,) in rows:
                    for part in re.split(r"[/,]", g or ""):
                        p = part.strip()
                        if p and p.lower() not in ("jiné", "neznámé",
                                                   "jiné / neznámé"):
                            c[self.kodi._canon_genre(p)] += 1
            conn.close()
            return [g for g, _ in c.most_common(top)]
        except Exception:
            return []

    def _person_manual_genres(self, names) -> list:
        """FILM_PERSON_GENRE_PREFS_V1 — ruční žánrové preference osob z webu
        (config film_suggest.genre_prefs, řádky 'jméno: žánr, žánr'). Pro přítomné
        osoby → kanonizované žánry. Nejvyšší priorita při výběru filmu. [] když nic."""
        raw = (self.config.get("film_suggest", {}) or {}).get("genre_prefs", "")
        if not raw or not names:
            return []
        present = {n.strip().lower() for n in names if n}
        out = []
        try:
            for line in str(raw).splitlines():
                if ":" not in line:
                    continue
                who, gpart = line.split(":", 1)
                if who.strip().lower() not in present:
                    continue
                for tok in gpart.split(","):
                    t = tok.strip()
                    if not t:
                        continue
                    g = self.kodi._canon_genre(t)
                    if g and g not in out:
                        out.append(g)
        except Exception:
            return []
        return out

    def _person_fav_films(self, names) -> list:
        """FILM_PERSON_FAVS_V1 — konkrétní oblíbené filmy osob z webu
        (config film_suggest.film_prefs, řádky 'jméno: Název, Název'). Pro přítomné
        osoby → seznam názvů (fuzzy match na knihovnu řeší kodi.pick_favorite). []."""
        raw = (self.config.get("film_suggest", {}) or {}).get("film_prefs", "")
        if not raw or not names:
            return []
        present = {n.strip().lower() for n in names if n}
        out = []
        try:
            for line in str(raw).splitlines():
                if ":" not in line:
                    continue
                who, titles = line.split(":", 1)
                if who.strip().lower() not in present:
                    continue
                for tok in titles.split(","):
                    t = tok.strip()
                    if t and t not in out:
                        out.append(t)
        except Exception:
            return []
        return out

    def _pick_next_film(self, names, cfg):
        """FILM_PICK_UNIFIED_V1 — vyber další FILM podle diváka: konkrétní oblíbené
        filmy osob (web) > žánry (ruční preference + zájmy + reálná historie +
        domácnost). S pravděpodobností rewatch_prob zkus oblíbený/vidaný film nehraný
        >N dní, jinak nevidaný; prázdný zdroj → další. Autoplay i studený návrh."""
        import random
        person = names[0] if names else ""
        genres = (self._person_manual_genres(names)
                  + self._person_pref_genres(person)
                  + self._person_watch_genres(names)
                  + self.kodi.favorite_genres())
        favs = self._person_fav_films(names)
        rprob = float(cfg.get("rewatch_prob", 0.3))
        rdays = int(cfg.get("rewatch_min_days", 4))
        rewatch_first = random.random() < rprob
        m = None
        if rewatch_first:
            # konkrétní oblíbený film osoby má přednost, pak žánrový rewatch, pak nevidaný
            m = self.kodi.pick_favorite(favs, min_days=rdays)
            if not m:
                m = self.kodi.pick_rewatch(prefer_genres=genres, min_days=rdays)
            if not m:
                m = self.kodi.pick_suggestion(prefer_genres=genres)
        else:
            m = self.kodi.pick_suggestion(prefer_genres=genres)
            if not m:
                m = self.kodi.pick_favorite(favs, min_days=rdays)
            if not m:
                m = self.kodi.pick_rewatch(prefer_genres=genres, min_days=rdays)
        return m

    def _maybe_suggest_film(self):  # KODI_FILM_SUGGEST_V1
        """Proaktivní návrh filmu přes Kodi dialog. Gate: nehraje ≥ idle_hours,
        osoba poblíž (≤10 min), obvyklý čas, throttle, TTS ticho."""
        cfg = self._fcfg()
        if not cfg.get("enabled", True) or self.kodi is None:
            return
        now = time.time()
        # poll Kodi stavu jen á 60s (šetři JSON-RPC)
        if (now - self._last_film_check) < 60:
            return
        self._last_film_check = now
        # osoba poblíž?
        names = [n for n in (getattr(self, "_present_names", None) or [])
                 if n and n not in ("Unknown", "?", "")]
        recency = float(cfg.get("present_recency_s", 600))
        if not names or (now - self._last_seen) > recency:
            return
        names = self._divaci() or names          # HANS_DIVACI_V1
        # nic nehraje + sleduj jak dlouho
        try:
            playing = self.kodi.is_playing()
        except Exception:
            return
        if playing:
            self._kodi_idle_since = 0.0
            return
        if self._kodi_idle_since == 0.0:
            self._kodi_idle_since = now
            return
        idle_need = float(cfg.get("idle_hours", 1.0)) * 3600.0
        if (now - self._kodi_idle_since) < idle_need:
            return
        # obvyklý čas koukání (rutina 2b; None/True propustí)
        try:
            rs = self._routine_store()
            if rs is not None and rs.is_typical_time(names[0]) is False:
                return
        except Exception:
            pass
        # throttle
        today = time.strftime("%Y-%m-%d")
        if today != self._film_suggest_day:
            self._film_suggest_day = today
            self._film_suggest_cnt = 0
        if self._film_suggest_cnt >= int(cfg.get("max_per_day", 3)):
            return
        if (now - self._last_film_suggest) < float(cfg.get("cooldown_h", 3)) * 3600.0:
            return
        # TTS nemluví
        _tts = getattr(getattr(self, "_hans_dialog", None), "tts", None)
        _isp = getattr(_tts, "is_speaking", None) if _tts else None
        if callable(_isp) and _isp():
            return
        # vyber film s preferencí žánrů (zájmy + reálná historie + domácnost; rewatch)
        try:
            movie = self._pick_next_film(names, cfg)  # FILM_PICK_UNIFIED_V1
        except Exception as e:
            _hi._log.warning("pick_suggestion selhal: %s", e)
            return
        if not movie or movie.get("movieid") is None:
            return
        # HANS_FILM_VOLBA_V1 — z několika kandidátů vybere Hans sám podle toho,
        # co ta osoba dřív přijala (`scripts/hans_film_volba.py`)
        _proc, _kand = "", []
        if cfg.get("hans_voli", True):
            try:
                from scripts import hans_film_volba as _fv
                movie, _proc, _kand = _fv.vyber_hansem(
                    self, self.config,
                    (self.config.get("hans_idle", {}) or {}).get("diary_db",
                                                                 "data/hans_diary.db"),
                    names, cfg, movie)
            except Exception as _fe:
                _hi._log.warning("HANS_FILM_VOLBA_V1: %s", _fe)
        title = movie.get("title", "film")
        person = names[0]
        countdown = int(cfg.get("countdown_s", 30))
        spoken = u"%s, co takhle se podívat na film %s?" % (person.capitalize(), title)
        # AVATAR_KODI_IMAGE_V1 — tvář (statický obrázek vlevo) + hlas (audio) ukáže
        # addon PŘÍMO U DIALOGU. Tvář NEzmizí (není video) a dialog vydrží celý odpočet.
        _vol = int(cfg.get("voice_volume", 90))
        _voice_local = None
        if cfg.get("voice_to_kodi", False) and _tts and self.kodi is not None:
            try:
                _mp3 = _tts._get_mp3(spoken)
                if _mp3:
                    _voice_local = str(_mp3)
            except Exception as _ve:
                _hi._log.warning("voice mp3 selhal: %s", _ve)
        _face = self._tv_face_image() if cfg.get("avatar_to_kodi", True) else None
        # když nemluvíme z TV, ale Pi TTS umí → řekni na Pi
        if not _voice_local and _tts and getattr(_tts, "enabled", False):
            try:
                _tts.speak(spoken)
            except Exception:
                pass
        line = u"Co takhle „%s\"? Pustím za %d s." % (title, countdown)
        ok = self.kodi.suggest_movie(movie, countdown=countdown, line=line,
                                     image_local=_face, voice_local=_voice_local,
                                     voice_volume=_vol,
                                     voice_lead_ms=int(cfg.get("voice_lead_ms", 900)))
        if not ok:
            return
        self._pending_film = (movie.get("movieid"), title, person, now)
        self._last_film_suggest = now
        self._film_suggest_cnt += 1
        self._log_entry("film_suggestion", title=person, note=u"Návrh: %s" % title,
                        data=__import__("json").dumps(   # HANS_FILM_VOLBA_V1
                            {"proc": _proc, "kandidati": _kand}, ensure_ascii=False))
        _hi._log.info("[FilmSuggest] %s ← %s", person, title)

    def _check_film_accepted(self):  # KODI_FILM_SUGGEST_V1
        """Sleduje, zda se navržený film spustil → mood↑ + deník + karta osoby."""
        pend = self._pending_film
        if not pend or self.kodi is None:
            return
        movieid, title, person, ts = pend
        now = time.time()
        window = float(self._fcfg().get("accept_window_min", 5)) * 60.0
        if (now - ts) > window:
            self._pending_film = None  # okno vypršelo, tiše nic
            return
        try:
            np = self.kodi.get_now_playing()
        except Exception:
            return
        if not np:
            return
        same = (np.get("id") == movieid) or (
            (np.get("title") or np.get("label") or "").strip().lower()
            == (title or "").strip().lower())
        if not same:
            return
        self._pending_film = None
        # mood ↑
        try:
            if hasattr(self, "_mood") and self._mood is not None:
                self._mood._shift("playful", 0.7,
                                  u"%s přijal můj návrh na film" % person)
        except Exception:
            pass
        # deník + karta osoby (film_suggestion_accepted → _MENTION_EVENT_TYPES)
        # HANS_VOCATIVE_CONSONANT_V1 — rod z profilu místo generického „/a"
        note = u"%s %s můj návrh na film „%s\"." % (
            _hi._cz_display(person), _hi._cz_past("přijal", _hi._cz_gender(person)), title)
        self._log_entry("film_suggestion_accepted", title=person, note=note)
        _hi._log.info("[FilmSuggest] PŘIJATO %s ← %s", person, title)

    # ── KODI_AUTOPLAY_V1 — autonomní pokračování zábavy ───────────────────────
    def _acfg(self) -> dict:
        return self.config.get("auto_continue", {}) or {}

    def _autoplay_ok_to_fire(self, cfg: dict, now: float) -> bool:
        """Společné mantinely: osoba přítomna, denní strop, cooldown, noční gate."""
        names = [n for n in (getattr(self, "_present_names", None) or [])
                 if n and n not in ("Unknown", "?", "")]
        if not names or (now - self._last_seen) > float(cfg.get("present_recency_s", 600)):
            return False
        today = datetime.now().strftime("%Y-%m-%d")
        if self._autoplay_day != today:
            self._autoplay_day = today
            self._autoplay_cnt = 0
        if self._autoplay_cnt >= int(cfg.get("max_per_day", 10)):
            return False
        if (now - self._last_autoplay_ts) < float(cfg.get("cooldown_min", 3)) * 60.0:
            return False
        qa = cfg.get("quiet_after_hour")  # např. 2 = po 2:00 už nepokračuj
        if qa is not None:
            h = datetime.now().hour
            if int(qa) <= h < int(cfg.get("quiet_until_hour", 9)):
                return False
        return True

    def _decide_next(self, cfg: dict, item: dict):
        """Vrať (next_item, line, streak_intent) co pustit po aktuálním titulu.
        Seriál → další díl (max N po sobě, pak film); film → další film (dle diváka)."""
        names = self._divaci() or [n for n in (getattr(self, "_present_names", None) or [])
                                   if n and n not in ("Unknown", "?", "")]   # HANS_DIVACI_V1
        mtype = item.get("type", "")
        is_series = (mtype == "episode" and item.get("tvshowid") is not None
                     and item.get("season") is not None
                     and item.get("episode") is not None)
        show = item.get("showtitle")
        streak_show, streak_n = self._autoplay_series_streak
        max_series = int(cfg.get("max_consecutive_series", 2))
        if is_series:
            # už N dílů téhož seriálu v řadě → přepni na něco jiného (film)
            if show and show == streak_show and streak_n >= max_series:
                m = self._pick_next_film(names, cfg)
                if m:
                    return ({"movieid": m.get("movieid"), "type": "movie",
                             "title": m.get("title")},
                            u"Dost dílů v kuse — co takhle film „%s\"?" % m.get("title"),
                            ("reset",))
                return (None, None, None)
            nxt = self.kodi.next_episode(item)
            if nxt:
                return (nxt,
                        u"Pustím další díl: „%s\" (S%02dE%02d)."
                        % (nxt.get("title") or "?",
                           int(nxt.get("season") or 0), int(nxt.get("episode") or 0)),
                        ("series", show))
            # seriál došel → film
            m = self._pick_next_film(names, cfg)
            if m:
                return ({"movieid": m.get("movieid"), "type": "movie",
                         "title": m.get("title")},
                        u"Seriál je u konce — co takhle film „%s\"?" % m.get("title"),
                        ("reset",))
            return (None, None, None)
        # film / neznámo → další film
        # HANS_AUTOPLAY_FILM_VOLBA_V1 (30. 10., uživatel: „navrhovat film, co se
        # pustí, až aktuální skončí“) — dřív náhodně (`pick_suggestion()` bez
        # ohledu na diváka); teď stejná cesta jako návrh filmu: kandidáti podle
        # přítomných osob (`_pick_next_film`) a vybere Hans (`hans_film_volba`)
        m, self._autoplay_proc = None, ""
        try:
            _fc = self._fcfg()
            m = self._pick_next_film(names, _fc) if names else None
            if m and _fc.get("hans_voli", True):
                from scripts import hans_film_volba as _fv
                m, self._autoplay_proc, _k = _fv.vyber_hansem(
                    self, self.config,
                    (self.config.get("hans_idle", {}) or {}).get("diary_db",
                                                                 "data/hans_diary.db"),
                    names, _fc, m)
        except Exception as _ae:
            _hi._log.warning("HANS_AUTOPLAY_FILM_VOLBA_V1: %s", _ae)
        m = m or self.kodi.pick_suggestion()
        if m:
            return ({"movieid": m.get("movieid"), "type": "movie",
                     "title": m.get("title")},
                    u"Co takhle pokračovat filmem „%s\"?" % m.get("title"),
                    ("reset",))
        return (None, None, None)

    def _apply_streak(self, intent):
        if not intent:
            return
        if intent[0] == "series":
            show = intent[1]
            cur_show, cur_n = self._autoplay_series_streak
            self._autoplay_series_streak = (
                show, (cur_n + 1 if show == cur_show else 1))
        elif intent[0] == "reset":
            self._autoplay_series_streak = (None, 0)

    def _maybe_autoplay_next(self):  # KODI_AUTOPLAY_V1
        """U konce běžícího titulu rozhodne další (díl/film); až aktuální DOHRAJE,
        na TV ukáže krátký odpočet (timeout = pustí, tlačítko = zruší). Nepřeruší
        konec/titulky — čeká na reálný konec přehrávání."""
        cfg = self._acfg()
        if not cfg.get("enabled", True) or self.kodi is None:
            return
        now = time.time()
        interval = 10.0 if self._autoplay_armed else 30.0
        if (now - self._last_autoplay_check) < interval:
            return
        self._last_autoplay_check = now
        try:
            st = self.kodi.get_play_state()
        except Exception:
            return

        # ── ARMED: čekáme, až aktuální dohraje, pak pustíme další ──
        if self._autoplay_armed:
            armed = self._autoplay_armed
            if (now - armed["ts"]) > float(cfg.get("arm_window_min", 12)) * 60.0:
                self._autoplay_armed = None          # vypršelo
                return
            if st is None:
                # aktuální dohrál → pusť další (s odpočtem na zrušení)
                if self._autoplay_ok_to_fire(cfg, now):
                    ok = self.kodi.autoplay_next(
                        armed["next"], countdown=int(cfg.get("countdown_s", 15)),
                        line=armed["line"], image_local=self._tv_face_image())
                    if ok:
                        self._last_autoplay_ts = now
                        self._autoplay_cnt += 1
                        self._apply_streak(armed.get("streak"))
                        self._log_entry("autoplay_next",
                                        title=(armed["next"].get("title") or "?"),
                                        note=u"Autoplay: %s" % (armed["next"].get("title")),
                                        data=__import__("json").dumps(   # HANS_AUTOPLAY_FILM_VOLBA_V1
                                            {"proc": armed.get("proc") or ""}, ensure_ascii=False))
                        _hi._log.info("[AutoPlay] → %s", armed["next"].get("title"))
                self._autoplay_armed = None
            elif (st.get("item") or {}).get("id") not in (None, armed.get("item_id")):
                # uživatel sám pustil něco jiného → zruš čekání
                self._autoplay_armed = None
            return

        # ── NEarmed: hledej blížící se konec a rozhodni další ──
        if st is None:
            return
        pct = st.get("percentage")
        item = st.get("item") or {}
        if item.get("type") not in ("movie", "episode"):
            return  # jen film/epizoda — živou TV (channel) neřeš
        if pct is None or pct < float(cfg.get("near_end_pct", 93.0)):
            return
        if st.get("speed") == 0:
            return  # pauza — nerozhoduj
        item_id = item.get("id")
        if item_id is not None and item_id == self._autoplay_last_item:
            return  # tenhle titul už máme rozhodnutý
        if not self._autoplay_ok_to_fire(cfg, now):
            return
        nxt, line, intent = self._decide_next(cfg, item)
        self._autoplay_last_item = item_id
        if not nxt or nxt.get("episodeid") is None and nxt.get("movieid") is None:
            return
        self._autoplay_armed = {
            "item_id": item_id, "next": nxt, "line": line,
            "streak": intent, "ts": now,
            "proc": getattr(self, "_autoplay_proc", ""),   # HANS_AUTOPLAY_FILM_VOLBA_V1
        }
        _hi._log.info("[AutoPlay] armed: po konci '%s' → %s",
                  item.get("title"), nxt.get("title"))

    def _load_movies(self):
        """Načti knihovnu filmů z Kodi."""
        if self._movies_cache:
            return
        try:
            result = self.kodi._call("VideoLibrary.GetMovies", {
                "properties": ["title", "year", "genre", "plot",
                               "director", "rating"],
            })
            if result and "result" in result:
                self._movies_cache = result["result"].get("movies", [])
                _hi._log.info("Loaded %d movies from Kodi", len(self._movies_cache))
        except Exception as e:
            _hi._log.error("Failed to load movies: %s", e)

    def _activity_movie(self):
        """Hans si 'přečte' o náhodném filmu."""
        if not self._movies_cache:
            self._load_movies()
        if not self._movies_cache:
            return

        movie = random.choice(self._movies_cache)
        title    = movie.get("title", "")
        year     = movie.get("year", "")
        genre    = ", ".join(movie.get("genre", []))
        # HANS_TEXT_LIMITS_V1 (25. 9.) — dřív natvrdo [:300]: 26 z 32 záznamů
        # končilo uprostřed věty a z uříznutého děje vznikal i názor na film.
        # Celý děj (pojistka 4 000 zn ukončená na konci věty).
        plot     = _hi._cely_dej(movie.get("plot", ""))
        rating   = movie.get("rating", 0)
        director = ", ".join(movie.get("director", []))

        data = (f"rok={year} zanr={genre} "
                f"rating={rating:.1f} reziser={director}")
        note = plot

        self._log_entry("movie_browsed", title=title, data=data, note=note)

    def get_movie_recommendation(self) -> dict | None:
        """Vrať film který Hans dnes 'viděl' — pro konverzaci."""
        with self._lock:   # HANS_DIARY_CTX_LOCK_V1
            row = self._db.execute("""
                SELECT title, data, note FROM diary
                WHERE event_type='movie_browsed'
                ORDER BY ts DESC LIMIT 1
            """).fetchone()
        if not row:
            return None
        return {"title": row[0], "data": row[1], "plot": row[2]}

# ROZDELENI_METOD_V1 — až na konci, viz hlavička
from scripts import hans_idle as _hi  # noqa: E402
