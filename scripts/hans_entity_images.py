"""HANS_ENTITY_IMAGE_V1 (29. 9.) — hlavní obrázek článku k entitě.

Přání uživatele: když se Hans dozví o osobě (typicky „namaluj <osoba>“), ať si
k informaci uloží i obrázek — příště ho má po ruce pro malování i pro dotaz
v chatu. Dřív `hans_art._fetch_person_ref` portrét stáhl do /tmp, použil pro
jednu malbu a smazal.

Jen pro typy, kde obrázek dává smysl (osoba = portrét, místo = foto, dílo =
plakát/obálka); u pojmu/organizace/události bývá schéma nebo logo.
Stahuje se LÍNĚ (když ho něco potřebuje) + v noci malá dávka (`backfill`) —
ne při každém přečteném článku: studium čte desítky článků za noc a Wikipedie
rychle vrací 429.

Tabulka `entity_images` je ZVLÁŠŤ (entities se nemění): status 'ok' = soubor
v `data/entity_images/<id>.jpg`, 'none' = článek obrázek nemá (znovu až za
`RETRY_NONE_DAYS`). Síťová chyba se NEzapisuje (zkusí se příště).
"""
import json
import logging
import os
import re
import sqlite3
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

_log = logging.getLogger(__name__)

ETYPES = ("osoba", "místo", "dílo")
DIR = Path(__file__).resolve().parent.parent / "data" / "entity_images"
MAX_SIDE = 1024
RETRY_NONE_DAYS = 30
_UA = {"User-Agent": "HansBot/1.0 (home assistant)"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS entity_images (
    entity_id  INTEGER PRIMARY KEY,
    status     TEXT NOT NULL,
    path       TEXT,
    url        TEXT,
    fetched_ts REAL
);
"""


def _conn(db: str):
    c = sqlite3.connect(db, timeout=10)
    c.executescript(_SCHEMA)
    return c


def _record(db: str, entity_id: int, status: str, path: str = "", url: str = ""):
    with _conn(db) as c:
        c.execute("INSERT OR REPLACE INTO entity_images (entity_id, status, path, url, "
                  "fetched_ts) VALUES (?,?,?,?,?)",
                  (int(entity_id), status, path, url, time.time()))


def cached(db: str, entity_id) -> Optional[str]:
    """Cesta k uloženému obrázku, nebo None."""
    try:
        with _conn(db) as c:
            r = c.execute("SELECT status, path FROM entity_images WHERE entity_id=?",
                          (int(entity_id),)).fetchone()
        if r and r[0] == "ok" and r[1] and os.path.exists(r[1]):
            return r[1]
    except Exception as e:
        _log.debug("entity_images cached: %s", e)
    return None


def _household_names(config: dict) -> set:
    """Jména domácnosti — k nim se obrázek z Wikipedie NIKDY nestahuje
    (shoda jména s cizí osobou by přinesla fotku někoho jiného)."""
    out = set()
    for k, kp in ((config or {}).get("known_persons") or {}).items():
        out.add(str(k).lower())
        if isinstance(kp, dict):
            out |= {str(kp[x]).lower() for x in ("nom", "full") if kp.get(x)}
    return out


def _lead_image_url(title: str, lang: str) -> tuple:
    """(url | None, jiste) — jiste=False při síťové chybě (nic nezapisovat)."""
    api = "https://%s.wikipedia.org/w/api.php?%s" % (lang, urllib.parse.urlencode({
        "action": "query", "titles": title, "prop": "pageimages",
        "piprop": "thumbnail", "pithumbsize": MAX_SIDE, "redirects": 1,
        "format": "json", "formatversion": 2}))
    try:
        with urllib.request.urlopen(urllib.request.Request(api, headers=_UA),
                                    timeout=20) as r:
            pages = json.loads(r.read().decode("utf-8")).get("query", {}).get("pages", [])
    except Exception as e:
        _log.debug("entity_images: dotaz na obrázek '%s' selhal: %s", title, e)
        return None, False
    if pages and isinstance(pages, list):
        src = (pages[0].get("thumbnail") or {}).get("source")
        if src:
            return src, True
    return None, True


def ensure_image(config: dict, db: str, ent: dict) -> Optional[str]:
    """Uložený obrázek entity; když chybí, stáhne ho (jednou). None = nemá."""
    if not ent or ent.get("etype") not in ETYPES or not ent.get("id"):
        return None
    if not ((config or {}).get("entity_images", {}) or {}).get("enabled", True):
        return None
    eid = int(ent["id"])
    hotovo = cached(db, eid)
    if hotovo:
        return hotovo
    try:
        with _conn(db) as c:
            r = c.execute("SELECT status, fetched_ts FROM entity_images WHERE entity_id=?",
                          (eid,)).fetchone()
        if r and r[0] == "none" and time.time() - float(r[1] or 0) < RETRY_NONE_DAYS * 86400:
            return None
    except Exception:
        pass
    name = str(ent.get("name") or "").strip()
    if name.lower() in _household_names(config):
        return None
    title = ent.get("source_title") or name
    m = re.search(r"https?://([a-z]{2})\.wikipedia", ent.get("source") or "")
    if not (title and m):
        return None                       # obrázek jen k entitě z Wikipedie
    url, jiste = _lead_image_url(title, m.group(1))
    if not url:
        if jiste:
            _record(db, eid, "none")
        return None
    try:
        from PIL import Image
        import io
        with urllib.request.urlopen(urllib.request.Request(url, headers=_UA),
                                    timeout=30) as r:
            data = r.read()
        im = Image.open(io.BytesIO(data)).convert("RGB")
        im.thumbnail((MAX_SIDE, MAX_SIDE))
        DIR.mkdir(parents=True, exist_ok=True)
        path = str(DIR / ("%d.jpg" % eid))
        im.save(path, "JPEG", quality=88)
    except Exception as e:
        _log.debug("entity_images: stažení '%s' selhalo: %s", title, e)
        return None
    _record(db, eid, "ok", path, url)
    _log.info("entity_images: uložen obrázek k '%s' (%s)", name, ent.get("etype"))
    return path


def backfill(config: dict, db: str, limit: int = 10) -> dict:
    """Noční dávka: entitám osoba/místo/dílo bez záznamu doplní obrázek.
    Nejdřív ty s nejvíc zmínkami. Pauza mezi dotazy kvůli Wikipedii."""
    stat = {"zkouseno": 0, "ulozeno": 0}
    try:
        with _conn(db) as c:
            rows = c.execute(
                "SELECT e.id, e.name, e.etype, e.source, e.source_title FROM entities e "
                "LEFT JOIN entity_images i ON i.entity_id = e.id "
                "WHERE e.etype IN (%s) AND i.entity_id IS NULL "
                "AND e.source LIKE '%%wikipedia%%' "
                "ORDER BY e.evidence_count DESC LIMIT ?" % ",".join("?" * len(ETYPES)),
                (*ETYPES, int(limit))).fetchall()
    except Exception as e:
        _log.warning("entity_images backfill: %s", e)
        return stat
    for eid, name, et, src, st in rows:
        stat["zkouseno"] += 1
        if ensure_image(config, db, {"id": eid, "name": name, "etype": et,
                                     "source": src, "source_title": st}):
            stat["ulozeno"] += 1
        time.sleep(1.5)
    return stat
