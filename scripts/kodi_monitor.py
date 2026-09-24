"""
Kodi Monitor
Sleduje co Kodi hraje a ukládá do SQLite s vazbou na přítomné osoby.
Běží jako daemon vlákno, neblokuje hlavní smyčku.

Tabulky:
  kodi_sessions  — každé přehrávání (film/seriál/hudba)
  person_events  — příchody a odchody osob
"""
import sqlite3
import threading
import time
import logging
from pathlib import Path
from datetime import datetime

from scripts.cz_names import came as _cz_came, left as _cz_left, \
    display_name as _cz_display, person_gender as _cz_gender, \
    past_verb as _cz_past  # HANS_NAME_INFLECTION_V1

_log = logging.getLogger("kodi_monitor")


# ── HANS_KODI_SEEN_BEFORE_V1 (24. 9.) ────────────────────────────────────────
# „Tenhle film jsme videli v pondeli (pred 3 dny).“ Zdroj je `kodi_sessions`
# (zacatek i konec sledovani), ne denik — jen tak jde poznat, ze se film
# opravdu DIVAL: tretina sezeni trva do 10 min (film se jen otevrel).
# Sezeni do 6 h od sebe = jedno sledovani (pauza, pokracovani vecer).
# Zmereno prehranim historie od 25. 4.: 7 hlasek na 629 sezeni filmu.
def naposledy_videno(conn, title: str, now: float, min_s: float = 1800,
                     max_days: float = 7.0, merge_s: float = 6 * 3600):
    """Konec posledniho sledovani filmu (>= min_s), pokud skoncilo pred mene
    nez max_days. Probihajici sledovani (do merge_s) se nepocita. Jinak None."""
    t = (title or "").strip()
    if not t:
        return None
    # trim(title)=? — NE lower(): SQLite lower() nemeni ne-ASCII (Č, Ó)
    rows = conn.execute(
        "SELECT started_at, updated_at FROM kodi_sessions "
        "WHERE media_type='movie' AND trim(title)=? AND started_at < ? "
        "ORDER BY started_at", (t, now - 1)).fetchall()
    views = []
    for s, u in rows:
        u = max(float(u or s), float(s))
        if views and s - views[-1][1] < merge_s:
            views[-1][1] = max(views[-1][1], u)
            views[-1][2] += u - s
            continue
        views.append([float(s), u, u - s])
    if not views:
        return None
    if now - views[-1][1] < merge_s:
        return None          # jde o pokracovani tehoz sledovani
    for s, u, d in reversed(views):
        if now - u > max_days * 86400:
            return None
        if d >= min_s:
            return u
    return None


def videno_text(konec: float, now: float) -> str:
    """„Tenhle film jsme videli v pondeli 21. 9. (pred 3 dny).“"""
    d0 = datetime.fromtimestamp(konec)
    dni = (datetime.fromtimestamp(now).date() - d0.date()).days
    _dny = ["v pondělí", "v úterý", "ve středu", "ve čtvrtek", "v pátek",
            "v sobotu", "v neděli"]
    if dni <= 0:
        return "Tenhle film jsme viděli už dnes."
    if dni == 1:
        return "Tenhle film jsme viděli včera."
    kdy = "%s %d. %d." % (_dny[d0.weekday()], d0.day, d0.month)
    return "Tenhle film jsme viděli %s (před %d dny)." % (kdy, dni)


class KodiMonitor:
    # T3_ENCOUNTER_TRACKER_V1 — optional callbacks pro EncounterTracker.
    # Pokud nastaveno (Memory wiring), volá se v update_visible při arrived/left.
    on_arrived = None  # callable(name: str, ts: float) | None
    on_left    = None  # callable(name: str, ts: float) | None


    def __init__(self, kodi_client, db_path: str,
                 poll_interval: float = 30.0,
                 diary_path: str = "data/hans_diary.db",
                 diary_writer=None):
        # DIARY_WRITER_PATCH_KODI
        self._diary_writer = diary_writer
        self.kodi          = kodi_client
        self.db_path       = Path(db_path)
        self.diary_path    = Path(diary_path)
        self.poll_interval = poll_interval
        self._lock         = threading.Lock()
        self._stop         = threading.Event()
        self._visible: list[str] = []   # aktualizováno z hlavní smyčky
        self._current_session_id = None
        self._current_item_id    = None
        # KODI_TITLE_THROTTLE_V1: title -> ts posledního fírnutí (anti re-fire)
        self._last_fired       = {}
        self._fire_throttle_s  = 3600   # stejný titul nefíruj znovu dřív
        self._conn = None
        self._init_db()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        _log.info("KodiMonitor started — poll every %.0fs", poll_interval)

    # ── DB ────────────────────────────────────────────────────────────────────

    def _init_db(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path),
                                     check_same_thread=False)
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS kodi_sessions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                media_type  TEXT,
                title       TEXT,
                year        INTEGER,
                genre       TEXT,
                director    TEXT,
                persons     TEXT,       -- JSON list jmen
                started_at  REAL,
                updated_at  REAL,
                finished    INTEGER DEFAULT 0
            )""")
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS person_events (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                name       TEXT NOT NULL,
                event      TEXT NOT NULL,  -- 'arrived' | 'left'
                ts         REAL NOT NULL
            )""")
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_pe_name ON person_events(name)")
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_ks_started ON kodi_sessions(started_at)")
        self._conn.commit()

    # ── Visible persons update (volá hlavní smyčka) ───────────────────────────

    def update_visible(self, persons: list[str]):
        """Aktualizuj seznam viditelných osob a loguj příchody/odchody."""
        with self._lock:
            prev = set(self._visible)
            curr = set(p for p in persons
                       if p not in ("Unknown", "...", "?", ""))
            now  = time.time()
            for name in curr - prev:
                self._conn.execute(
                    "INSERT INTO person_events (name, event, ts) VALUES (?,?,?)",
                    (name, "arrived", now))
                _log.info("→ arrived: %s", name)
                if self.on_arrived is not None:  # T3_ENCOUNTER_TRACKER_V1
                    try: self.on_arrived(name, now)
                    except Exception as _e: _log.warning("on_arrived hook failed: %s", _e)
            for name in prev - curr:
                self._conn.execute(
                    "INSERT INTO person_events (name, event, ts) VALUES (?,?,?)",
                    (name, "left", now))
                _log.info("← left: %s", name)
                if self.on_left is not None:  # T3_ENCOUNTER_TRACKER_V1
                    try: self.on_left(name, now)
                    except Exception as _e: _log.warning("on_left hook failed: %s", _e)
            if curr != prev:
                self._conn.commit()
            self._visible = list(curr)

    # ── Poll loop ─────────────────────────────────────────────────────────────

    def _loop(self):
        while not self._stop.is_set():
            try:
                self._poll()
            except Exception as e:
                _log.error("Poll error: %s", e)
            self._stop.wait(self.poll_interval)

    def _poll(self):
        item = self.kodi.get_now_playing()
        now  = time.time()

        if item is None:
            # Nic nehraje — uzavři session
            if self._current_session_id is not None:
                with self._lock:
                    self._conn.execute(
                        "UPDATE kodi_sessions SET finished=1, updated_at=? WHERE id=?",
                        (now, self._current_session_id))
                    self._conn.commit()
                _log.info("Session closed: id=%d", self._current_session_id)
                self._current_session_id = None
                self._current_item_id    = None
            return

        item_id = item.get("id")

        if item_id != self._current_item_id:
            # Nový titul — uzavři předchozí session
            if self._current_session_id is not None:
                with self._lock:
                    self._conn.execute(
                        "UPDATE kodi_sessions SET finished=1, updated_at=? WHERE id=?",
                        (now, self._current_session_id))
                    self._conn.commit()

            # Otevři novou session
            import json
            with self._lock:
                persons_json = json.dumps(list(self._visible),
                                          ensure_ascii=False)
                cur = self._conn.execute("""
                    INSERT INTO kodi_sessions
                        (media_type, title, year, genre, director,
                         persons, started_at, updated_at)
                    VALUES (?,?,?,?,?,?,?,?)
                """, (
                    item.get("type", ""),
                    item.get("title", ""),
                    item.get("year"),
                    ", ".join(item.get("genre", [])),
                    ", ".join(item.get("director", [])),
                    persons_json,
                    now, now,
                ))
                self._conn.commit()
                self._current_session_id = cur.lastrowid
                self._current_item_id    = item_id
                # KODI_TITLE_THROTTLE_V1: per-title guard — stejný titul (i po
                # None-blipu / nestabilním id) nefíruj curiosity+diár
                # znovu dřív než _fire_throttle_s. Session běží dál.
                import time as _tt
                _now_s = _tt.time()
                _fkey = (item.get("title") or item.get("label")
                         or item.get("channel") or "").strip().lower()
                _should_fire = (not _fkey) or (
                    _now_s - self._last_fired.get(_fkey, 0) >= self._fire_throttle_s)
                if _fkey and _should_fire:
                    self._last_fired[_fkey] = _now_s
                if _should_fire and item.get("title") and hasattr(self, '_curiosity'):
                    self._curiosity.trigger_kodi(
                        title      = item["title"],
                        media_type = item.get("type", "movie"),
                        year       = item.get("year") or None,   # HANS_FILM_ARTICLE_V1
                        imdb       = (item.get("uniqueid") or {}).get("imdb", ""),     # HANS_FILM_IMDB_V1
                        qid        = (item.get("uniqueid") or {}).get("wikidata", ""),
                    )
                if item.get("title") and hasattr(self, '_mood'):
                    self._mood.update_kodi(item["title"])
                # Self-question: Hans chce vědět víc o tom co hraje
                if _should_fire and item.get("title") and hasattr(self, '_curiosity'):
                    import random as _rnd
                    if _rnd.random() < 0.4:   # 40% šance — ne každý titul
                        _ctx = (
                            f"Kodi hraje: {item.get('title','')} "
                            f"({item.get('type','')}, "
                            f"{', '.join(item.get('genre',[]))})"
                        )
                        self._curiosity.trigger_question(_ctx, source_type='kodi')
            _log.info("New session: '%s' (%s) — watchers: %s",
                      item.get("title"), item.get("type"), self._visible)
            if _should_fire and item.get("type") == "movie" and item.get("title"):
                self._ohlas_videno(item["title"], now)   # HANS_KODI_SEEN_BEFORE_V1
            # Zapiš do Hansova deníku
            if _should_fire:   # KODI_TITLE_THROTTLE_V1
                self._diary_log(item)
        else:
            # Stejný titul — jen aktualizuj čas a osoby
            import json
            with self._lock:
                persons_json = json.dumps(list(self._visible),
                                          ensure_ascii=False)
                self._conn.execute("""
                    UPDATE kodi_sessions
                    SET updated_at=?, persons=?
                    WHERE id=?
                """, (now, persons_json, self._current_session_id))
                self._conn.commit()

    def _ohlas_videno(self, title: str, now: float) -> None:
        """HANS_KODI_SEEN_BEFORE_V1 — film, ktery jsme videli za posledni tyden,
        ohlasi kratce na TV. Nic starsiho (pokyn uzivatele 24. 9.)."""
        try:
            kc = getattr(self.kodi, "_kcfg", {}) or {}
            if not kc.get("seen_before_notify", True):
                return
            with self._lock:
                konec = naposledy_videno(
                    self._conn, title, now,
                    min_s=float(kc.get("seen_before_min_minutes", 30)) * 60,
                    max_days=float(kc.get("seen_before_max_days", 7)))
            if konec is None:
                return
            if self._hans_nabidl(title, now):
                _log.info("HANS_KODI_SEEN_BEFORE_V1: '%s' nabidl sam Hans → neohlasuji", title)
                return
            text = videno_text(konec, now)
            # Hansova tvar: soubor, ktery na OSMC udrzuje dialog nabidky filmu
            # (KodiClient._scp_face, AVATAR_KODI_IMAGE_V1); overeno na TV 24. 9.
            ok = self.kodi.notify(getattr(self.kodi, "_persona", "Hans"), text,
                                  float(kc.get("seen_before_display_s", 15)),
                                  image=kc.get("seen_before_image",
                                               "special://home/addons/service.hans.suggest"
                                               "/media/hans_face.png"))
            _log.info("HANS_KODI_SEEN_BEFORE_V1: '%s' → %s (%s)", title, text,
                      "ukazano" if ok else "Kodi neodpovedel")
        except Exception as e:
            _log.warning("HANS_KODI_SEEN_BEFORE_V1 selhal: %s", e)

    def _hans_nabidl(self, title: str, now: float) -> bool:
        """Nabidl ten film Hans sam v poslednich 15 min? (KODI_FILM_SUGGEST_V1)"""
        try:
            c = sqlite3.connect("file:%s?mode=ro" % self.diary_path, uri=True, timeout=3)
            try:
                r = c.execute(
                    "SELECT 1 FROM diary WHERE event_type='film_suggestion' "
                    "AND ts>? AND note=?",
                    (now - 900, "Návrh: %s" % title)).fetchone()
            finally:
                c.close()
            return bool(r)
        except Exception:
            return False

    def _diary_log(self, item: dict):
        """Zapiš aktuálně hraný titul do Hansova deníku."""
        if not self.diary_path.exists() and not self.diary_path.parent.exists():
            return
        try:
            import sqlite3, time as _t
            # KODI_TITLE_FALLBACK_PATCH
            # Fallback: title → label (kanál) → channel. Některé IPTV pořady
            # mezi vysíláním nemají title, ale mají alespoň jméno kanálu.
            title    = (item.get("title")
                        or item.get("label")
                        or item.get("channel")
                        or "neznámý pořad").strip()
            mtype    = item.get("type", "")
            year     = item.get("year", "")
            genre    = ", ".join(item.get("genre", []))
            director = ", ".join(item.get("director", []))
            plot     = (item.get("plot") or item.get("plotoutline") or "").strip()  # MOVIE_GROUNDING_V1
            watchers = list(self._visible)

            # Sestavení poznámky
            parts = [f"Typ: {mtype}"]
            if year:     parts.append(f"rok {year}")
            if genre:    parts.append(f"žánr: {genre}")
            if director: parts.append(f"režie: {director}")
            if watchers: parts.append(f"sledují: {', '.join(watchers)}")
            if plot:     parts.append(f"děj: {plot[:1000]}")
            note = " | ".join(parts)

            conn = sqlite3.connect(str(self.diary_path))
            # Vytvoř tabulku pokud neexistuje
            conn.execute("""
                CREATE TABLE IF NOT EXISTS diary (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts REAL NOT NULL,
                    event_type TEXT NOT NULL,
                    title TEXT,
                    data TEXT,
                    note TEXT
                )""")
            if self._diary_writer:
                try:
                    self._diary_writer("kodi_playing", title, note=note)
                    _log.info("Diary: Kodi hraje '%s' — %s", title,
                              f"sledují: {watchers}" if watchers else "nikdo nesleduje")
                    conn.close()
                    return
                except Exception as _de:
                    _log.warning("Diary writer (kodi) failed: %s", _de)
                    # fallthrough na SQL
            conn.execute(
                "INSERT INTO diary (ts, event_type, title, note) VALUES (?,?,?,?)",
                (_t.time(), "kodi_playing", title, note))
            conn.commit()
            conn.close()
            _log.info("Diary: Kodi hraje '%s' — %s", title,
                      f"sledují: {watchers}" if watchers else "nikdo nesleduje")
        except Exception as e:
            _log.warning("Diary log error: %s", e)

    # ── LLM kontext ───────────────────────────────────────────────────────────

    def get_now_playing_context(self) -> str:
        """Vrať string pro LLM — co Kodi hraje teď."""
        item = self.kodi.get_now_playing()
        if not item:
            return ""
        title    = item.get("title", "neznámý titul")
        year     = item.get("year")
        genre    = ", ".join(item.get("genre", []))
        director = ", ".join(item.get("director", []))
        parts = [f"Kodi právě hraje: {title}"]
        if year:     parts.append(f"({year})")
        if genre:    parts.append(f"žánr: {genre}")
        if director: parts.append(f"režie: {director}")
        return " ".join(parts) + "."

    def get_person_history(self, name: str, limit: int = 5) -> str:
        """Co daná osoba naposledy sledovala."""
        import json
        rows = self._conn.execute("""
            SELECT title, media_type, year, genre, started_at
            FROM kodi_sessions
            WHERE persons LIKE ? AND finished=1
            ORDER BY started_at DESC LIMIT ?
        """, (f'%"{name}"%', limit)).fetchall()
        if not rows:
            return ""
        lines = []
        for title, mtype, year, genre, ts in rows:
            dt  = datetime.fromtimestamp(ts).strftime("%d.%m. %H:%M")
            line = f"- {title}"
            if year:  line += f" ({year})"
            if genre: line += f" [{genre}]"
            line += f" — {dt}"
            lines.append(line)
        # HANS_VOCATIVE_CONSONANT_V1 — rod z profilu místo generického „/a"
        _sled = _cz_past("sledoval", _cz_gender(name))
        return f"Co {_cz_display(name)} naposledy {_sled}:\n" + "\n".join(lines)

    def get_today_events(self) -> str:
        """Příchody a odchody osob dnes."""
        midnight = datetime.now().replace(
            hour=0, minute=0, second=0, microsecond=0).timestamp()
        rows = self._conn.execute("""
            SELECT name, event, ts FROM person_events
            WHERE ts >= ? ORDER BY ts DESC LIMIT 20
        """, (midnight,)).fetchall()
        if not rows:
            return ""
        lines = []
        for name, event, ts in rows:
            dt  = datetime.fromtimestamp(ts).strftime("%H:%M")
            verb = _cz_came(name) if event == "arrived" else _cz_left(name)
            lines.append(f"- {_cz_display(name)} {verb} v {dt}")
        return "Dnešní pohyb v místnosti:\n" + "\n".join(lines)

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=5)
        if self._conn:
            self._conn.close()