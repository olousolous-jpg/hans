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

    def suggest_movie(self, movie: dict, countdown: int = 30,
                      line: str | None = None) -> bool:
        """Pošle návrh do Kodi (addon service.hans.suggest) → dialog ano/ne
        s odpočtem; po potvrzení addon film pustí. Vrátí True když signál odešel."""
        if not movie:
            return False
        title = movie.get("title", "film")
        mid = movie.get("movieid")
        if mid is None:
            return False
        if not line:
            line = u'Co takhle „%s"? Pustím za %d s.' % (title, int(countdown))
        r = self._call("JSONRPC.NotifyAll", {
            "sender": "hans",
            "message": "hans_suggest",
            "data": {"title": title, "movieid": mid,
                     "countdown": int(countdown), "line": line},
        })
        ok = bool(r and r.get("result") == "OK")
        if ok:
            _log.info("suggest_movie → %s (movieid=%s, %ds)", title, mid, countdown)
        return ok

