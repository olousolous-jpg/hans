"""
Kodi JSON-RPC Client
Sends commands to Kodi via its HTTP JSON-RPC API.

Config keys (under "kodi"):
    host     : str  — Kodi hostname or IP (default "localhost")
    port     : int  — Kodi HTTP port (default 8080)
    user     : str  — username if auth enabled (default "")
    password : str  — password if auth enabled (default "")
    enabled  : bool — master switch (default true)
"""

import requests
import logging
import time

_log = logging.getLogger("kodi")


class KodiClient:

    def __init__(self, config: dict):
        cfg           = config.get("kodi", {})
        self.enabled  = bool(cfg.get("enabled", True))
        self.host     = cfg.get("host", "localhost")
        self.port     = int(cfg.get("port", 8080))
        self.user     = cfg.get("user", "")
        self.password = cfg.get("password", "")
        self._url     = f"http://{self.host}:{self.port}/jsonrpc"
        self._id      = 1

        if self.enabled:
            _log.info("KodiClient ready — %s", self._url)

    def _call(self, method: str, params: dict = None) -> dict | None:
        if not self.enabled:
            return None
        payload = {
            "jsonrpc": "2.0",
            "method":  method,
            "params":  params or {},
            "id":      self._id,
        }
        self._id += 1
        try:
            auth = (self.user, self.password) if self.user else None
            resp = requests.post(
                self._url, json=payload,
                auth=auth, timeout=3,
                headers={"Content-Type": "application/json"},
            )
            if resp.status_code == 200:
                return resp.json()
            _log.warning("Kodi HTTP %d for %s", resp.status_code, method)
        except requests.exceptions.ConnectionError:
            _log.warning("Cannot connect to Kodi at %s", self._url)
        except Exception as e:
            _log.error("Kodi call error: %s", e)
        return None

    # ── Playback helpers ──────────────────────────────────────────────────────

    def _active_player_id(self) -> int | None:
        """Return the ID of the currently active player, or None."""
        result = self._call("Player.GetActivePlayers")
        if result and "result" in result and result["result"]:
            return result["result"][0]["playerid"]
        return None

    def pause(self) -> bool:
        """Pause active player. Returns True on success."""
        pid = self._active_player_id()
        if pid is None:
            _log.info("Kodi pause: no active player")
            return False
        result = self._call("Player.PlayPause",
                            {"playerid": pid, "play": False})
        if result and "result" in result:
            _log.info("Kodi paused (player %d)", pid)
            return True
        return False

    def play(self) -> bool:
        """Resume active player. Returns True on success."""
        pid = self._active_player_id()
        if pid is None:
            _log.info("Kodi play: no active player")
            return False
        result = self._call("Player.PlayPause",
                            {"playerid": pid, "play": True})
        if result and "result" in result:
            _log.info("Kodi resumed (player %d)", pid)
            return True
        return False

    def toggle_pause(self) -> bool:
        """Toggle play/pause."""
        pid = self._active_player_id()
        if pid is None:
            return False
        result = self._call("Player.PlayPause", {"playerid": pid})
        return result is not None

    def volume_up(self, step: int = 5) -> bool:
        result = self._call("Application.GetProperties",
                            {"properties": ["volume"]})
        if result and "result" in result:
            vol = result["result"].get("volume", 50)
            self._call("Application.SetVolume",
                       {"volume": min(100, vol + step)})
            return True
        return False

    def volume_down(self, step: int = 5) -> bool:
        result = self._call("Application.GetProperties",
                            {"properties": ["volume"]})
        if result and "result" in result:
            vol = result["result"].get("volume", 50)
            self._call("Application.SetVolume",
                       {"volume": max(0, vol - step)})
            return True
        return False

    def send_action(self, action: str) -> bool:
        """Send any Kodi Input.ExecuteAction command."""
        result = self._call("Input.ExecuteAction", {"action": action})
        return result is not None

    def is_playing(self) -> bool:
        return self._active_player_id() is not None

    def get_now_playing(self) -> dict | None:
        """Vrať info o aktualne hranem titulu nebo None."""
        pid = self._active_player_id()
        if pid is None:
            return None
        result = self._call('Player.GetItem', {
            'playerid': pid,
            'properties': ['title', 'year', 'genre', 'director',
                           'artist', 'album', 'showtitle', 'season',
                           'episode', 'plot', 'plotoutline'],  # MOVIE_GROUNDING_V1
        })
        if result and 'result' in result:
            item = result['result'].get('item', {})
            if item.get('title') or item.get('label'):
                return item
        return None

    # ── KODI_SUGGEST_V1 — proaktivní návrh filmu (Fáze 3) ──────────────────────
    # Kodi míchá CZ i EN názvy žánrů (scrapery) → normalizace na společný kanon,
    # ať překryv viděné↔nevidené funguje.
    _GENRE_CANON = {
        "akční": "action", "action": "action",
        "dobrodružný": "adventure", "adventure": "adventure",
        "sci-fi": "scifi", "science fiction": "scifi", "scifi": "scifi",
        "drama": "drama", "western": "western",
        "komedie": "comedy", "comedy": "comedy",
        "válečný": "war", "war": "war", "thriller": "thriller",
        "fantasy": "fantasy", "horor": "horror", "horror": "horror",
        "krimi": "crime", "kriminální": "crime", "crime": "crime",
        "mysteriózní": "mystery", "mystery": "mystery",
        "romantický": "romance", "romance": "romance",
        "animovaný": "animation", "animation": "animation",
        "rodinný": "family", "family": "family",
        "dokumentární": "documentary", "documentary": "documentary",
        "historický": "history", "history": "history",
        "hudební": "music", "music": "music",
        "životopisný": "biography", "biography": "biography",
    }

    @classmethod
    def _canon_genre(cls, g: str) -> str:
        s = (g or "").strip().lower()
        return cls._GENRE_CANON.get(s, s)

    def favorite_genres(self, top: int = 6) -> list:
        """Oblíbené žánry z REÁLNĚ sledovaných filmů (playcount>0), vážené
        sledovaností. Kanonizované. Read-only. Signál 'co tahle domácnost kouká'."""
        r = self._call("VideoLibrary.GetMovies", {
            "filter": {"field": "playcount", "operator": "greaterthan", "value": "0"},
            "properties": ["genre", "playcount"],
        })
        movies = (r or {}).get("result", {}).get("movies", []) if r else []
        from collections import Counter
        c = Counter()
        for m in movies:
            w = max(1, int(m.get("playcount", 1)))
            for g in (m.get("genre") or []):
                c[self._canon_genre(g)] += w
        return [g for g, _ in c.most_common(top)]

    def pick_suggestion(self, prefer_genres=None, limit: int = 25) -> dict | None:
        """Vybere NEVIDĚNÝ film (playcount=0) z nejnovějších přidaných, s
        PREFERENCÍ žánrů (prefer_genres; default = favorite_genres z historie).
        Vážená náhoda: preferovaný žánr má přednost, ale i ostatní mají šanci
        (explorace). Read-only. Vrátí dict nebo None."""
        r = self._call("VideoLibrary.GetMovies", {
            "filter": {"field": "playcount", "operator": "is", "value": "0"},
            "properties": ["title", "year", "art", "plot", "runtime", "genre"],
            "sort": {"method": "dateadded", "order": "descending"},
            "limits": {"start": 0, "end": int(limit)},
        })
        movies = (r or {}).get("result", {}).get("movies", []) if r else []
        if not movies:
            return None
        if prefer_genres is None:
            prefer_genres = self.favorite_genres()
        prefer = {self._canon_genre(g) for g in (prefer_genres or [])}
        import random
        weighted = []
        for m in movies:
            gset = {self._canon_genre(g) for g in (m.get("genre") or [])}
            score = len(gset & prefer)
            weighted.append((m, 1 + 2 * score))   # 1 = explorace, +2/shoda žánru
        total = sum(w for _, w in weighted)
        pick = random.uniform(0, total)
        acc = 0.0
        for m, w in weighted:
            acc += w
            if pick <= acc:
                return m
        return weighted[-1][0]

    def _speak_file_from_idle(self, local_file: str, voice_volume: int = 90,
                              remote_name: str = "hans_voice.mp3") -> bool:
        """Sdílené jádro (HANS_FILM_VOICE_V1 / AVATAR_KODI_V1) — přehraj lokální
        soubor (MP3 hlas NEBO MP4 mluvící tvář) přes Kodi Z KLIDU. scp na box →
        dočasně zvedne hlasitost → Player.Open → po délce klipu vrátí hlasitost.
        NEpřeruší, když něco hraje (skip → False). Passthrough HDMI: playSFX
        nefunguje, Player.Open z klidu ano. Funguje pro MP3 i MP4 (ffprobe čte
        délku z obou). Vrací True když přehráno."""
        if not self.enabled:
            return False
        # 1) jen z klidu — nikdy nepřerušuj běžící přehrávání
        try:
            ap = self._call("Player.GetActivePlayers")
            if ap and ap.get("result"):
                _log.info("speak_from_idle: něco hraje → skip (nepřerušuju)")
                return False
        except Exception:
            return False
        # 2) scp na box (osmc/osmc = JSON-RPC i SSH creds)
        import subprocess
        remote = "/home/%s/%s" % (self.user or "osmc", remote_name)
        try:
            subprocess.run(
                ["sshpass", "-p", str(self.password), "scp",
                 "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=8",
                 str(local_file), "%s@%s:%s" % (self.user, self.host, remote)],
                check=True, capture_output=True, timeout=30)
        except Exception as e:
            _log.warning("speak_from_idle: scp selhal: %s", e)
            return False
        # 3) délka klipu (ffprobe; fallback 6s)
        dur = 6.0
        try:
            r = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=nw=1:nk=1", str(local_file)],
                capture_output=True, text=True, timeout=8)
            dur = float((r.stdout or "").strip() or 6.0)
        except Exception:
            dur = 6.0
        # 4) ulož hlasitost, zvedni
        saved = None
        try:
            g = self._call("Application.GetProperties", {"properties": ["volume"]})
            saved = (g or {}).get("result", {}).get("volume")
        except Exception:
            saved = None
        try:
            self._call("Application.SetVolume", {"volume": int(voice_volume)})
        except Exception:
            pass
        # 5) přehraj
        self._call("Player.Open", {"item": {"file": remote}})
        # 6) po klipu vrať hlasitost (daemon thread, ať neblokujeme)
        import threading
        def _restore():
            time.sleep(dur + 1.0)
            if saved is not None:
                try:
                    self._call("Application.SetVolume", {"volume": int(saved)})
                except Exception:
                    pass
        threading.Thread(target=_restore, daemon=True).start()
        _log.info("speak_from_idle: přehráno %s (%.1fs, vol %s→%s)",
                  remote_name, dur, saved, voice_volume)
        return True

    @staticmethod
    def _parse_lastplayed(s: str):
        """Kodi 'lastplayed' ('YYYY-MM-DD HH:MM:SS') → epoch, nebo None."""
        if not s:
            return None
        import time as _t
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                return _t.mktime(_t.strptime(s, fmt))
            except Exception:
                pass
        return None

    def pick_rewatch(self, prefer_genres=None, min_days: int = 4,
                     limit: int = 40) -> dict | None:
        """REWATCH_FILM_V1 — oblíbený VIDANÝ film (playcount>0) nehraný posledních
        min_days dní. Vážená náhoda dle shody žánru + oblíbenosti (playcount).
        None když žádný nevyhovuje. Read-only."""
        import time as _t
        import random
        r = self._call("VideoLibrary.GetMovies", {
            "filter": {"field": "playcount", "operator": "greaterthan", "value": "0"},
            "properties": ["title", "year", "art", "plot", "runtime", "genre",
                           "playcount", "lastplayed"],
            "sort": {"method": "lastplayed", "order": "descending"},
            "limits": {"start": 0, "end": int(limit)},
        })
        movies = (r or {}).get("result", {}).get("movies", []) if r else []
        if not movies:
            return None
        cutoff = _t.time() - int(min_days) * 86400
        elig = []
        for m in movies:
            lp = self._parse_lastplayed(m.get("lastplayed"))
            if lp is not None and lp > cutoff:
                continue  # hrané nedávno → přeskoč
            elig.append(m)
        if not elig:
            return None
        if prefer_genres is None:
            prefer_genres = self.favorite_genres()
        prefer = {self._canon_genre(g) for g in (prefer_genres or [])}
        weighted = []
        for m in elig:
            gset = {self._canon_genre(g) for g in (m.get("genre") or [])}
            score = len(gset & prefer)
            pc = max(1, int(m.get("playcount", 1)))
            weighted.append((m, 1 + 2 * score + (pc - 1)))  # +oblíbenost
        total = sum(w for _, w in weighted)
        pick = random.uniform(0, total)
        acc = 0.0
        for m, w in weighted:
            acc += w
            if pick <= acc:
                return m
        return weighted[-1][0]

    @staticmethod
    def _norm_title(s: str) -> str:
        """Normalizace názvu pro fuzzy match (bez diakritiky, malá, jen alnum+mezery)."""
        import unicodedata
        import re
        s = unicodedata.normalize("NFKD", s or "")
        s = "".join(c for c in s if not unicodedata.combining(c))
        return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()

    def find_movie(self, title: str, limit: int = 800) -> dict | None:
        """HANS_AGENT_V1 — najdi film v knihovně dle názvu (fuzzy, bez diakritiky,
        oboustranný substring). Vrací dict {movieid,title,year,...} nebo None.
        Read-only — grounding pro agentní akci kodi_play_film."""
        w = self._norm_title(title)
        if not w:
            return None
        r = self._call("VideoLibrary.GetMovies", {
            "properties": ["title", "year", "genre", "playcount"],
            "limits": {"start": 0, "end": int(limit)},
        })
        movies = (r or {}).get("result", {}).get("movies", []) if r else []
        best = None
        for m in movies:
            nm = self._norm_title(m.get("title"))
            if not nm:
                continue
            if nm == w:            # přesná shoda = přednost
                return m
            if w in nm or nm in w:  # substring (série „…2/3")
                best = best or m
        return best

    def play_movie(self, movieid: int) -> bool:
        """HANS_AGENT_V1 — pusť film podle movieid (Player.Open). Vrací úspěch."""
        try:
            r = self._call("Player.Open", {"item": {"movieid": int(movieid)}})
            return bool(r) and "error" not in (r or {})
        except Exception:
            return False

    def _active_player_id(self) -> int | None:
        """ID aktivního video přehrávače (nebo None)."""
        try:
            r = self._call("Player.GetActivePlayers", {})
            for p in (r or {}).get("result", []) or []:
                if p.get("type") in ("video", "audio"):
                    return p.get("playerid")
        except Exception:
            pass
        return None

    def pause_playback(self) -> bool:
        """HANS_AGENT_V1 — pauza/pokračování aktivního přehrávače (toggle)."""
        pid = self._active_player_id()
        if pid is None:
            return False
        try:
            r = self._call("Player.PlayPause", {"playerid": pid})
            return bool(r) and "error" not in (r or {})
        except Exception:
            return False

    def stop_playback(self) -> bool:
        """HANS_AGENT_V1 — úplně zastav aktivní přehrávač."""
        pid = self._active_player_id()
        if pid is None:
            return False
        try:
            r = self._call("Player.Stop", {"playerid": pid})
            return bool(r) and "error" not in (r or {})
        except Exception:
            return False

    def pick_favorite(self, titles, min_days: int = 0,
                      limit: int = 800) -> dict | None:
        """FILM_PERSON_FAVS_V1 — vyber konkrétní OBLÍBENÝ film osoby (dle názvu)
        z knihovny, nehraný posledních min_days dní. Fuzzy match názvu (bez
        diakritiky, oboustranný substring → 'Smrtonosná past' sedne i na '...2').
        None když žádný oblíbený nesedí / všechny hrané nedávno. Read-only."""
        titles = [t for t in (titles or []) if t and str(t).strip()]
        if not titles:
            return None
        import time as _t
        import random
        r = self._call("VideoLibrary.GetMovies", {
            "properties": ["title", "year", "genre", "playcount", "lastplayed",
                           "art", "plot", "runtime"],
            "limits": {"start": 0, "end": int(limit)},
        })
        movies = (r or {}).get("result", {}).get("movies", []) if r else []
        if not movies:
            return None
        wanted = [w for w in (self._norm_title(t) for t in titles) if w]
        cutoff = _t.time() - int(min_days) * 86400
        elig = []
        for m in movies:
            nm = self._norm_title(m.get("title"))
            if not nm:
                continue
            if not any(w in nm or nm in w for w in wanted):
                continue
            lp = self._parse_lastplayed(m.get("lastplayed"))
            if lp is not None and lp > cutoff:
                continue  # hraný nedávno → přeskoč
            elig.append(m)
        if not elig:
            return None
        return random.choice(elig)

    def speak_clip(self, local_mp3: str, voice_volume: int = 90) -> bool:
        """HANS_FILM_VOICE_V1 — Hansův hlas (MP3) přes Kodi z klidu."""
        return self._speak_file_from_idle(local_mp3, voice_volume, "hans_voice.mp3")

    def speak_avatar(self, local_mp3: str, clip_path: str,
                     voice_volume: int = 90) -> bool:
        """AVATAR_KODI_V1 — Hansova MLUVÍCÍ TVÁŘ na TV. Lokálně přimixuje hlas (MP3)
        na talkloop video (smyčka na délku hlasu) → jedno MP4 → Player.Open z klidu
        (reuse speak_clip pipeline). Vrací False, když clip/MP3 chybí nebo mux selže
        → volající spadne na speak_clip / Pi TTS."""
        import os
        import subprocess
        import tempfile
        if not self.enabled:
            return False
        if not (clip_path and os.path.exists(str(clip_path))
                and local_mp3 and os.path.exists(str(local_mp3))):
            return False
        out = os.path.join(tempfile.gettempdir(), "hans_tv_avatar.mp4")
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-stream_loop", "-1", "-i", str(clip_path),
                 "-i", str(local_mp3),
                 "-map", "0:v:0", "-map", "1:a:0", "-shortest",
                 "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "veryfast",
                 "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", out],
                check=True, capture_output=True, timeout=60)
        except Exception as e:
            _log.warning("speak_avatar: mux selhal: %s", e)
            return False
        return self._speak_file_from_idle(out, voice_volume, "hans_tv_avatar.mp4")

    def _scp_abs(self, local: str, remote_full: str) -> str | None:
        """scp lokální soubor na OSMC na konkrétní vzdálenou cestu; vrátí ji / None."""
        if not local:
            return None
        import os
        import subprocess
        if not os.path.exists(str(local)):
            return None
        try:
            subprocess.run(
                ["sshpass", "-p", str(self.password), "scp",
                 "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=8",
                 str(local), "%s@%s:%s" % (self.user, self.host, remote_full)],
                check=True, capture_output=True, timeout=30)
            return remote_full
        except Exception as e:
            _log.warning("_scp_abs(%s) selhal: %s", remote_full, e)
            return None

    def _scp_to_box(self, local: str, remote_name: str) -> str | None:
        """scp do domovského adresáře OSMC (pro hlas)."""
        return self._scp_abs(local, "/home/%s/%s" % (self.user or "osmc", remote_name))

    def _scp_face(self, local: str) -> str | None:
        """scp Hansovy tváře do MEDIA adresáře addonu na OSMC. Kodi ControlImage
        načítá obrázky spolehlivě z adresáře addonu; /home/osmc/ bývá mimo whitelist."""
        rd = "/home/%s/.kodi/addons/service.hans.suggest/media" % (self.user or "osmc")
        return self._scp_abs(local, rd + "/hans_face.png")

    def _pad_audio_lead(self, local_mp3: str, lead_ms: int = 900) -> str:
        """Předřadí ticho na začátek mp3 → HDMI zvuk po probuzení uťne jen pauzu,
        ne první slovo. Vrátí cestu k novému souboru (fallback: původní)."""
        import os
        import subprocess
        import tempfile
        if not local_mp3 or lead_ms <= 0 or not os.path.exists(str(local_mp3)):
            return local_mp3
        out = os.path.join(tempfile.gettempdir(), "hans_voice_pad.mp3")
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-i", str(local_mp3),
                 "-af", "adelay=%d:all=1" % int(lead_ms), out],
                check=True, capture_output=True, timeout=30)
            return out
        except Exception as e:
            _log.warning("_pad_audio_lead selhal: %s", e)
            return local_mp3

    def suggest_movie(self, movie: dict, countdown: int = 30,
                      line: str | None = None, image_local: str | None = None,
                      voice_local: str | None = None,
                      voice_volume: int = 90, voice_lead_ms: int = 900) -> bool:
        """Pošle návrh do Kodi (addon service.hans.suggest) → dialog ano/ne
        s odpočtem; po potvrzení addon film pustí. AVATAR_KODI_IMAGE_V1: image_local
        (Hansova tvář) + voice_local (MP3 hlas) se scp na OSMC a addon je ukáže/přehraje
        u dialogu (tvář vlevo, hlas audio). voice_lead_ms = ticho na začátek proti
        uťatému začátku na HDMI. Vrátí True když signál odešel."""
        if not movie:
            return False
        title = movie.get("title", "film")
        mid = movie.get("movieid")
        if mid is None:
            return False
        if not line:
            line = u'Co takhle „%s"? Pustím za %d s.' % (title, int(countdown))
        data = {"title": title, "movieid": mid,
                "countdown": int(countdown), "line": line,
                "voice_volume": int(voice_volume)}
        img_remote = self._scp_face(image_local) if image_local else None
        if img_remote:
            data["image"] = img_remote
        voice_remote = None
        if voice_local:
            voice_remote = self._scp_to_box(
                self._pad_audio_lead(voice_local, voice_lead_ms), "hans_voice.mp3")
        if voice_remote:
            data["voice_file"] = voice_remote
        r = self._call("JSONRPC.NotifyAll", {
            "sender": "hans", "message": "hans_suggest", "data": data})
        ok = bool(r and r.get("result") == "OK")
        if ok:
            _log.info("suggest_movie → %s (movieid=%s, %ds, tvář=%s, hlas=%s)",
                      title, mid, countdown, bool(img_remote), bool(voice_remote))
        return ok

    # ── KODI_AUTOPLAY_V1 — pokračování zábavy u konce titulu ───────────────────
    def get_play_state(self) -> dict | None:
        """Stav běžícího videa: {percentage, speed, totaltime, item} nebo None.
        item nese type/title/showtitle/season/episode/tvshowid/id (pro rozhodnutí,
        co pustit dál a jak daleko je přehrávání)."""
        pid = self._active_player_id()
        if pid is None:
            return None
        pr = self._call("Player.GetProperties", {
            "playerid": pid,
            "properties": ["percentage", "speed", "time", "totaltime"]})
        res = (pr or {}).get("result") or {}
        it = self._call("Player.GetItem", {
            "playerid": pid,
            "properties": ["title", "showtitle", "season", "episode",
                           "tvshowid"]})
        item = ((it or {}).get("result") or {}).get("item") or {}
        if not (item.get("title") or item.get("label")):
            return None
        return {"percentage": res.get("percentage"), "speed": res.get("speed"),
                "totaltime": res.get("totaltime"), "item": item}

    def next_episode(self, item: dict) -> dict | None:
        """Další epizoda téhož seriálu po aktuální (dle season+episode). None když
        je to poslední díl / chybí metadata."""
        tvid = item.get("tvshowid")
        cs, ce = item.get("season"), item.get("episode")
        if tvid is None or cs is None or ce is None:
            return None
        r = self._call("VideoLibrary.GetEpisodes", {
            "tvshowid": int(tvid),
            "properties": ["title", "season", "episode"],
            "sort": {"method": "episode", "order": "ascending"}})
        eps = (r or {}).get("result", {}).get("episodes", []) if r else []
        cur = (int(cs), int(ce))
        later = [e for e in eps
                 if (int(e.get("season", 0)), int(e.get("episode", 0))) > cur]
        if not later:
            return None
        later.sort(key=lambda e: (int(e.get("season", 0)), int(e.get("episode", 0))))
        e = later[0]
        return {"episodeid": e.get("episodeid"), "type": "episode",
                "title": e.get("title"), "season": e.get("season"),
                "episode": e.get("episode"), "showtitle": item.get("showtitle")}

    def autoplay_next(self, nxt: dict, countdown: int = 15,
                      line: str | None = None,
                      image_local: str | None = None) -> bool:
        """Pošle do addonu signál hans_autoplay → na TV odpočet, kde TIMEOUT pustí
        a tlačítko zruší (auto-play s možností zrušit). Podporuje film i epizodu.
        image_local (Hansova tvář) se scp na OSMC a addon ji ukáže u dialogu."""
        if not nxt:
            return False
        data = {"countdown": int(countdown),
                "line": line or u"Pustím další.",
                "media_type": nxt.get("type")}
        if nxt.get("episodeid") is not None:
            data["episodeid"] = nxt["episodeid"]
        if nxt.get("movieid") is not None:
            data["movieid"] = nxt["movieid"]
        if "episodeid" not in data and "movieid" not in data:
            return False
        img_remote = self._scp_face(image_local) if image_local else None
        if img_remote:
            data["image"] = img_remote
        r = self._call("JSONRPC.NotifyAll", {
            "sender": "hans", "message": "hans_autoplay", "data": data})
        ok = bool(r and r.get("result") == "OK")
        if ok:
            _log.info("autoplay_next → %s (%s, %ds)", nxt.get("title"),
                      nxt.get("type"), countdown)
        return ok

