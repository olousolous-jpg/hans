#!/usr/bin/env python3
"""HANS_WIKI_SOURADNICE_V1 (3. 11. 2026) — souřadnice článků české Wikipedie offline.

Ze souboru `cswiki-latest-geo_tags.sql.gz` (≈ 6 MB, Wikimedia dumps) vezme HLAVNÍ
souřadnici (gt_primary=1, Země) každého článku a uloží ji do `data/wiki/souradnice.db`:
    s(id = číslo stránky = clanek.id v cswiki.db, lat, lon, typ)
Používá `scripts/hans_mista.py` (místa podle názvu i okolí bez Wikidat).

    python3 tools/wiki_souradnice.py            # stáhne nejnovější a sestaví
    python3 tools/wiki_souradnice.py --soubor X  # sestaví z daného souboru
Volá ho i měsíční obnova Wikipedie (`tools/wiki_obnova.py`).
"""
import gzip
import os
import re
import sqlite3
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
URL = "https://dumps.wikimedia.org/cswiki/latest/cswiki-latest-geo_tags.sql.gz"
SOUBOR = os.path.join(ROOT, "data", "wiki", "geo", "cswiki-latest-geo_tags.sql.gz")
DB = os.environ.get("WIKI_SOURADNICE_DB") or os.path.join(ROOT, "data", "wiki", "souradnice.db")

# (gt_id, gt_page_id, 'globe', primary, lat, lon, dim, 'type' | NULL, …
_N = re.compile(r"\((\d+),(\d+),'([^']*)',(\d),(-?[\d.]+|NULL),(-?[\d.]+|NULL),(-?\d+|NULL),"
                r"('(?:[^'\\]|\\.)*'|NULL),")


def stahni(cil: str) -> None:
    os.makedirs(os.path.dirname(cil), exist_ok=True)
    subprocess.run(["curl", "-sfL", "-A", "Hans/1.0 (offline souřadnice)", "-o", cil + ".tmp", URL],
                   check=True, timeout=600)
    os.replace(cil + ".tmp", cil)


def sestav(soubor: str, db: str) -> int:
    tmp = db + ".nova"
    if os.path.exists(tmp):
        os.remove(tmp)
    c = sqlite3.connect(tmp)
    c.execute("CREATE TABLE s(id INTEGER PRIMARY KEY, lat REAL, lon REAL, typ TEXT)")
    n = 0
    with gzip.open(soubor, "rt", encoding="utf-8", errors="replace") as fh:
        for radek in fh:     # n-tice na řádku za INSERT INTO (nový formát) i na něm samém
            if not radek.startswith(("INSERT INTO", "(")):
                continue
            rows = []
            for m in _N.finditer(radek):
                if m.group(3) != "earth" or m.group(4) != "1" or "NULL" in (m.group(5), m.group(6)):
                    continue
                typ = None if m.group(8) == "NULL" else m.group(8)[1:-1]
                rows.append((int(m.group(2)), float(m.group(5)), float(m.group(6)), typ))
            c.executemany("INSERT OR REPLACE INTO s VALUES (?,?,?,?)", rows)
            n += len(rows)
    c.execute("CREATE INDEX ix_lat ON s(lat)")
    c.commit()
    c.close()
    os.replace(tmp, db)
    return n


if __name__ == "__main__":
    s = SOUBOR
    if "--soubor" in sys.argv:
        s = sys.argv[sys.argv.index("--soubor") + 1]
    else:
        stahni(s)
    print("souřadnic:", sestav(s, DB), "→", DB)
