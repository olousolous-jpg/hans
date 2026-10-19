"""Tituly lokální Wikipedie BEZ DIAKRITIKY → data/wiki/tituly_fold.db (HANS_WIKI_TITUL_Z_VETY_V1).

Pro `hans_wiki_offline.titul_z_vety`: uživatel píše bez háčků („karluv most“),
titul článku je s nimi („Karlův most“). Hlavní DB (2,8 GB) se nepřepisuje;
tahle malá tabulka se staví znovu po každé obnově výpisu:
    python3 tools/wiki_tituly_fold.py
"""
import os, sqlite3, sys, time, unicodedata
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "data", "wiki", "cswiki.db")
DST = os.path.join(ROOT, "data", "wiki", "tituly_fold.db")


def fold(s):
    return "".join(c for c in unicodedata.normalize("NFD", (s or "").lower())
                   if unicodedata.category(c) != "Mn").replace("_", " ").strip()


def main():
    t0 = time.time()
    src = sqlite3.connect(f"file:{SRC}?mode=ro", uri=True)
    tmp = DST + ".tmp"
    if os.path.exists(tmp):
        os.remove(tmp)
    d = sqlite3.connect(tmp)
    d.execute("CREATE TABLE t(fold TEXT, titul TEXT, rozcestnik INTEGER, infobox TEXT)")
    rows = src.execute("SELECT titul, rozcestnik, COALESCE(infobox_druh,'') FROM clanek").fetchall()
    d.executemany("INSERT INTO t VALUES (?,?,?,?)", [(fold(a), a, r, i) for a, r, i in rows])
    info = {a: (r, i) for a, r, i in rows}
    red = src.execute("SELECT titul, cil FROM presmerovani").fetchall()
    d.executemany("INSERT INTO t VALUES (?,?,?,?)",
                  [(fold(a), c, *info[c]) for a, c in red if c in info])
    d.execute("CREATE INDEX ix_fold ON t(fold)")
    d.commit(); d.close(); src.close()
    os.replace(tmp, DST)
    print("hotovo: %d článků, %d přesměrování, %.0f s" % (len(rows), len(red), time.time() - t0))


if __name__ == "__main__":
    main()
