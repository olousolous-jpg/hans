"""HANS_CALENDAR_V1 — Hans čte kalendáře přes sdílené ICS odkazy (per osoba).

Proton NEMÁ CalDAV (caldav.proton.me neexistuje — ověřeno) → jediná reálná
cesta je JEDNOSMĚRNÉ čtení: každá osoba v Proton Calendar sdílí SVŮJ kalendář
tajným odkazem (Nastavení → Kalendáře → Sdílet → odkaz „pro kohokoli") =
veřejný ICS feed. Hans ho pravidelně stáhne, rozbalí (i RRULE) a ví o
událostech → připomene JEN TÉ OSOBĚ přes Telegram + zmíní jí to v chatu.

DŮLEŽITÉ (soukromí): kalendáře jsou PER OSOBA. Připomínka jde jen majiteli
kalendáře, ne ostatním Telegram uživatelům. Config `calendar.people` mapuje
jméno osoby → její ICS odkaz. Read-only; Hansovy vlastní sliby jdou Telegram
pushem, ne do Protonu.
"""
from __future__ import annotations

import datetime as _dt
import logging
import sqlite3
import time
from typing import Optional, List
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

_DEFAULT_TZ = "Europe/Prague"

# HANS_CALENDAR_URL_SCRUB_V1 — chybové hlášky z requests nesou CELÝ ICS odkaz
# (…CacheKey=…&PassphraseKey=…) = tajný přístup ke kalendáři. Do logu smí jen
# host + druh chyby, NIKDY klíče.
import re as _re

_SECRET_KEY_RE = _re.compile(r'(CacheKey|PassphraseKey)=[^&\s\'"]+', _re.I)
_PROTON_URL_RE = _re.compile(r'https?://[^\s\'"]*calendar\.proton\.me[^\s\'"]*',
                             _re.I)
# requests hlášku URL rozděluje (host zvlášť, cesta zvlášť) → path token
# /…/url/<TOKEN>/ zůstane i po smazání query klíčů; zamaskuj i ten.
_PROTON_PATH_RE = _re.compile(r'(/url/)[^/\s\'"]+', _re.I)


def _scrub_secret(err, url: str = "") -> str:
    """Odstraní tajný ICS odkaz z chybové hlášky (host+chyba ano, klíče ne)."""
    msg = str(err)
    if url:
        msg = msg.replace(url, "<ics_url>")
    msg = _PROTON_URL_RE.sub("<ics_url>", msg)
    msg = _SECRET_KEY_RE.sub(r"\1=<redacted>", msg)
    msg = _PROTON_PATH_RE.sub(r"\1<token>", msg)
    return msg


def _cfg(config: dict) -> dict:
    return (config.get("calendar", {}) or {})


def _tz(config: dict) -> ZoneInfo:
    try:
        return ZoneInfo(_cfg(config).get("tz", _DEFAULT_TZ))
    except Exception:
        return ZoneInfo(_DEFAULT_TZ)


def people_map(config: dict) -> dict:
    """{osoba: ics_url}. Podpora nové `people` mapy i staré `ics_url`
    (fallback pod `owner`, default 'me')."""
    c = _cfg(config)
    out = {}
    ppl = c.get("people") or {}
    if isinstance(ppl, dict):
        for person, url in ppl.items():
            u = (url or "").strip() if isinstance(url, str) else \
                (url or {}).get("ics_url", "").strip()
            if person and u:
                out[str(person).lower()] = u
    legacy = (c.get("ics_url") or "").strip()
    if legacy:
        owner = str(c.get("owner", "me")).lower()
        out.setdefault(owner, legacy)
    return out


def is_enabled(config: dict) -> bool:
    return bool(_cfg(config).get("enabled")) and bool(people_map(config))


def _to_epoch(v, tz: ZoneInfo) -> tuple[float, bool]:
    """DTSTART/DTEND → (epoch, all_day). Naive datetime bere jako lokální."""
    if isinstance(v, _dt.datetime):
        if v.tzinfo is None:
            v = v.replace(tzinfo=tz)
        return v.timestamp(), False
    if isinstance(v, _dt.date):
        d = _dt.datetime(v.year, v.month, v.day, tzinfo=tz)
        return d.timestamp(), True
    return 0.0, False


class CalendarStore:
    def __init__(self, config: dict, diary_db_path: str):
        self.config = config
        self._path = diary_db_path
        self._tz = _tz(config)
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self._path) as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS calendar_events (
                    person     TEXT NOT NULL DEFAULT '',
                    uid        TEXT NOT NULL,
                    start_ts   REAL NOT NULL,
                    end_ts     REAL NOT NULL DEFAULT 0,
                    summary    TEXT NOT NULL DEFAULT '',
                    location   TEXT NOT NULL DEFAULT '',
                    all_day    INTEGER NOT NULL DEFAULT 0,
                    reminded   INTEGER NOT NULL DEFAULT 0,
                    updated_ts REAL NOT NULL DEFAULT 0,
                    PRIMARY KEY (person, uid, start_ts)
                )
            """)
            # Migrace ze starého schématu bez `person` (PK uid+start_ts).
            cols = [r[1] for r in db.execute(
                "PRAGMA table_info(calendar_events)").fetchall()]
            if "person" not in cols:
                db.execute("DROP TABLE calendar_events")
                db.execute("""
                    CREATE TABLE calendar_events (
                        person TEXT NOT NULL DEFAULT '', uid TEXT NOT NULL,
                        start_ts REAL NOT NULL, end_ts REAL NOT NULL DEFAULT 0,
                        summary TEXT NOT NULL DEFAULT '',
                        location TEXT NOT NULL DEFAULT '',
                        all_day INTEGER NOT NULL DEFAULT 0,
                        reminded INTEGER NOT NULL DEFAULT 0,
                        updated_ts REAL NOT NULL DEFAULT 0,
                        PRIMARY KEY (person, uid, start_ts))
                """)
            db.execute("CREATE INDEX IF NOT EXISTS idx_cal_start "
                       "ON calendar_events(start_ts)")
            db.commit()

    # ── sync ─────────────────────────────────────────────────────────────────
    def sync(self) -> int:
        """Stáhne ICS feedy VŠECH osob, rozbalí, upsertne. Vrací počet událostí
        celkem (-1 = žádná osoba/vypnuto)."""
        if not _cfg(self.config).get("enabled"):
            return -1
        people = people_map(self.config)
        if not people:
            return -1
        total = 0
        for person, url in people.items():
            n = self._sync_person(person, url)
            if n >= 0:
                total += n
        return total

    def _sync_person(self, person: str, url: str) -> int:
        try:
            import requests
            import icalendar
            import recurring_ical_events
        except Exception as e:
            log.warning("calendar: knihovny nedostupné (%s)", e)
            return -1
        try:
            r = requests.get(url, timeout=15)
            if not r.ok:
                log.warning("calendar[%s]: fetch %s", person, r.status_code)
                return -1
            cal = icalendar.Calendar.from_ical(r.text)
        except Exception as e:
            log.warning("calendar[%s]: fetch/parse selhal: %s", person,
                        _scrub_secret(e, url))
            return -1
        horizon = int(_cfg(self.config).get("horizon_days", 60))
        now = _dt.datetime.now(self._tz)
        start = now - _dt.timedelta(days=1)
        end = now + _dt.timedelta(days=horizon)
        try:
            occurrences = recurring_ical_events.of(cal).between(start, end)
        except Exception as e:
            log.warning("calendar[%s]: RRULE selhalo: %s", person, e)
            return -1
        rows = []
        for ev in occurrences:
            try:
                ds = ev.get("DTSTART")
                if ds is None:
                    continue
                uid = str(ev.get("UID", "")) or str(ev.get("SUMMARY", ""))
                s_ep, all_day = _to_epoch(ds.dt, self._tz)
                de = ev.get("DTEND")
                e_ep = _to_epoch(de.dt, self._tz)[0] if de is not None else s_ep
                rows.append((uid, s_ep, e_ep,
                             str(ev.get("SUMMARY", "")).strip(),
                             str(ev.get("LOCATION", "")).strip(),
                             1 if all_day else 0))
            except Exception:
                continue
        try:
            conn = sqlite3.connect(self._path, timeout=5.0)
            keep = {(u, round(s, 0)) for (u, s, *_ ) in rows}
            cur = conn.execute(
                "SELECT uid, start_ts FROM calendar_events WHERE person=? "
                "AND start_ts > ?", (person, time.time() - 86400)).fetchall()
            for u, s in cur:
                if (u, round(s, 0)) not in keep:
                    conn.execute("DELETE FROM calendar_events WHERE person=? "
                                 "AND uid=? AND start_ts=?", (person, u, s))
            nowts = time.time()
            for uid, s_ep, e_ep, summary, loc, ad in rows:
                ex = conn.execute(
                    "SELECT reminded FROM calendar_events WHERE person=? AND "
                    "uid=? AND start_ts=?", (person, uid, s_ep)).fetchone()
                if ex:
                    conn.execute(
                        "UPDATE calendar_events SET end_ts=?, summary=?, "
                        "location=?, all_day=?, updated_ts=? WHERE person=? "
                        "AND uid=? AND start_ts=?",
                        (e_ep, summary, loc, ad, nowts, person, uid, s_ep))
                else:
                    conn.execute(
                        "INSERT INTO calendar_events (person, uid, start_ts, "
                        "end_ts, summary, location, all_day, reminded, "
                        "updated_ts) VALUES (?,?,?,?,?,?,?,0,?)",
                        (person, uid, s_ep, e_ep, summary, loc, ad, nowts))
            conn.commit()
            conn.close()
        except Exception as e:
            log.warning("calendar[%s]: uložení selhalo: %s", person, e)
            return -1
        log.info("calendar[%s]: sync OK — %d událostí", person, len(rows))
        return len(rows)

    # ── čtení (per osoba) ────────────────────────────────────────────────────
    def _rows(self, where: str, params: tuple) -> List[dict]:
        try:
            conn = sqlite3.connect("file:%s?mode=ro" % self._path, uri=True,
                                   timeout=3.0)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM calendar_events WHERE %s ORDER BY start_ts ASC"
                % where, params).fetchall()
            conn.close()
            return [dict(r) for r in rows]
        except Exception:
            return []

    def upcoming(self, person: str, hours: int = 48,
                 limit: int = 20) -> List[dict]:
        now = time.time()
        rows = self._rows(
            "person=? AND start_ts >= ? AND start_ts <= ?",
            ((person or "").lower(), now - 3600, now + hours * 3600))
        return rows[:limit]

    def _fmt_when(self, ev: dict) -> str:
        d = _dt.datetime.fromtimestamp(ev["start_ts"], self._tz)
        _now = _dt.datetime.now(self._tz)
        delta = (d.date() - _now.date()).days
        hm = "" if ev.get("all_day") else d.strftime("%H:%M")
        if delta == 0:
            day = "dnes"
        elif delta == 1:
            day = "zítra"
        elif 2 <= delta <= 6:
            day = ("v pondělí", "v úterý", "ve středu", "ve čtvrtek",
                   "v pátek", "v sobotu", "v neděli")[d.weekday()]
        else:
            day = f"{d.day}.{d.month}."
        return f"{day} {hm}".strip()

    def context_string(self, person: str, hours: int = 48) -> str:
        evs = self.upcoming(person, hours=hours, limit=6)
        if not evs:
            return ""
        parts = []
        for e in evs:
            loc = f" ({e['location']})" if e.get("location") else ""
            parts.append(f"{self._fmt_when(e)} — {e['summary']}{loc}")
        return ("Nadcházející události v kalendáři této osoby "
                "(vím o nich z jejího Proton kalendáře): "
                + "; ".join(parts) + ".")

    # ── připomínky (nesou person → adresují jen jemu) ────────────────────────
    def due_reminders(self, lead_hours: float = 2.0) -> List[dict]:
        now = time.time()
        return self._rows(
            "reminded=0 AND start_ts > ? AND start_ts <= ?",
            (now, now + lead_hours * 3600))

    def mark_reminded(self, person: str, uid: str, start_ts: float):
        try:
            conn = sqlite3.connect(self._path, timeout=5.0)
            conn.execute("UPDATE calendar_events SET reminded=1 WHERE person=? "
                         "AND uid=? AND start_ts=?", (person, uid, start_ts))
            conn.commit()
            conn.close()
        except Exception:
            pass

    def reminder_text(self, ev: dict) -> str:
        loc = f" ({ev['location']})" if ev.get("location") else ""
        return f"Připomínám: {self._fmt_when(ev)} máte {ev['summary']}{loc}."


if __name__ == "__main__":
    import json
    import sys
    logging.basicConfig(level=logging.INFO)
    cfg = json.load(open("config.json"))
    st = CalendarStore(cfg, "data/hans_diary.db")
    if len(sys.argv) > 1 and sys.argv[1] == "sync":
        print("sync:", st.sync())
    for person in people_map(cfg):
        print(f"— {person} —")
        for e in st.upcoming(person, hours=24 * 14):
            print("  ", st._fmt_when(e), "—", e["summary"])
