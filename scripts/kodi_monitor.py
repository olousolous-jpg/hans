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
    display_name as _cz_display  # HANS_NAME_INFLECTION_V1

_log = logging.getLogger("kodi_monitor")


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
        return f"Co {name} naposledy sledoval/a:\n" + "\n".join(lines)

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