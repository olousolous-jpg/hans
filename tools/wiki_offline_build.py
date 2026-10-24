#!/usr/bin/env python3
"""WIKIPEDIE_OFFLINE — sestavení lokální české Wikipedie (17. 10. 2026).

Výpis cswiki → SQLite s FTS5 v data/wiki/cswiki.db. Tři kroky:
  pruchod   vlastní průchod XML: přesměrování + infobox („klíč: hodnota“)
  extrahuj  wikiextractor → čistý text článků (json, bez šablon)
  index     SQLite: clanek, presmerovani, FTS5 nad titulem a textem

Nespouští Hanse ani model. Pouštět s nice (Pi sdílí jádra s Hansem):
  nice -n 15 python3 tools/wiki_offline_build.py vse
"""
import bz2, glob, html, json, os, re, sqlite3, subprocess, sys, time
import xml.etree.ElementTree as ET

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
W = os.path.join(ROOT, "data", "wiki")
DUMP = os.path.join(W, "cswiki-20261001-pages-articles.xml.bz2")
REDIR = os.path.join(W, "presmerovani.tsv")
INFO = os.path.join(W, "infobox.jsonl")
META = os.path.join(W, "meta.jsonl")
_ROZCESTNIK = re.compile(r"\{\{\s*(Rozcestník|Rozcestnik|Disambig)", re.I)
# WIKI_SEZNAMY_V1 (24. 10.) — přestavba vedle běžící DB: WIKI_EXTR / WIKI_DB
EXTR = os.environ.get("WIKI_EXTR") or os.path.join(W, "extrakt")
DB = os.environ.get("WIKI_DB") or os.path.join(W, "cswiki.db")
STARA_DB = os.path.join(W, "cswiki.db")


def norm(s: str) -> str:
    """Klíč titulu: malá písmena, podtržítka → mezery, bez krajních mezer.
    Diakritiku NECHÁVÁ (Cech × Čech je jiný článek)."""
    return re.sub(r"\s+", " ", (s or "").replace("_", " ")).strip().lower()


def _vycisti_hodnotu(v: str) -> str:
    v = re.sub(r"<ref[^>]*/>", "", v)
    v = re.sub(r"<ref[^>]*>.*?</ref>", "", v, flags=re.S)
    v = re.sub(r"<br\s*/?>", ", ", v)
    v = re.sub(r"<[^>]+>", "", v)
    v = re.sub(r"\[\[(?:[^\]|]*\|)?([^\]]*)\]\]", r"\1", v)
    v = re.sub(r"\[https?://\S+\s+([^\]]*)\]", r"\1", v)
    v = re.sub(r"'{2,}", "", v)
    # jednoduché šablony data {{Datum narození|1879|3|14}} → 1879-3-14
    v = re.sub(r"\{\{[^{}|]*\|([^{}]*)\}\}",
               lambda m: "-".join(x for x in m.group(1).split("|")
                                  if x and "=" not in x), v)
    v = re.sub(r"\{\{[^{}]*\}\}", "", v)
    return re.sub(r"\s+", " ", v).strip(" ,;")


_SKIP_KLIC = re.compile(r"obrázek|logo|soubor|velikost|popisek|mapa|šířka|"
                        r"commons|web|souřadnice|image|caption", re.I)


def infobox(text: str):
    """První {{Infobox …}} → (druh, [(klíč, hodnota)]) s vyvážením závorek."""
    i = text.find("{{Infobox")
    if i < 0:
        return None
    hl, j, n = 0, i, len(text)
    while j < n - 1:
        if text.startswith("{{", j):
            hl += 1; j += 2; continue
        if text.startswith("}}", j):
            hl -= 1; j += 2
            if hl == 0:
                break
            continue
        j += 1
    telo = text[i + 2:j - 2]
    druh = telo.split("\n", 1)[0].replace("Infobox", "").strip(" -|")
    # rozdělit na parametry jen v hloubce 0 (uvnitř [[a|b]] a {{…}} ne)
    casti, akt, h = [], [], 0
    k = 0
    while k < len(telo):
        c2 = telo[k:k + 2]
        if c2 in ("{{", "[["):
            h += 1; akt.append(c2); k += 2; continue
        if c2 in ("}}", "]]"):
            h -= 1; akt.append(c2); k += 2; continue
        if telo[k] == "|" and h == 0:
            casti.append("".join(akt)); akt = []; k += 1; continue
        akt.append(telo[k]); k += 1
    casti.append("".join(akt))
    pary = []
    for c in casti[1:]:
        if "=" not in c:
            continue
        kl, v = c.split("=", 1)
        kl = kl.strip()
        if not kl or _SKIP_KLIC.search(kl):
            continue
        v = _vycisti_hodnotu(v)
        if v and len(v) < 400:
            pary.append((kl, v))
    return druh, pary


def _konec_uvodu(text: str, nadpis: str) -> int:
    """Pozice prvního nadpisu v extraktu (řádek „Nadpis.“); bez nadpisu celý text."""
    if nadpis:
        nad = re.sub(r"\[\[(?:[^\]|]*\|)?([^\]]*)\]\]", r"\1", nadpis)
        nad = re.sub(r"'{2,}|<[^>]+>", "", nad).strip()
        i = text.find("\n" + nad + ".")
        if i >= 0:
            return i
    return len(text)


def pruchod():
    t0 = time.time()
    nr = ni = 0
    with bz2.open(DUMP, "rb") as f, open(REDIR, "w", encoding="utf-8") as fr, \
            open(INFO, "w", encoding="utf-8") as fi, \
            open(META, "w", encoding="utf-8") as fm:
        titul = pid = ns = redir = None
        for ev, el in ET.iterparse(f, events=("end",)):
            tag = el.tag.rsplit("}", 1)[-1]
            if tag == "title":
                titul = el.text
            elif tag == "ns":
                ns = el.text
            elif tag == "id" and pid is None:
                pid = el.text
            elif tag == "redirect":
                redir = el.get("title")
            elif tag == "text":
                if ns == "0" and not redir and el.text:
                    # rozcestník (šablona zmizí v extraktu) a první nadpis
                    # (= konec úvodu; extrakt nadpis drží jako řádek „Nadpis.“)
                    _roz = bool(_ROZCESTNIK.search(el.text))
                    _m = re.search(r"^==+\s*(.+?)\s*==+\s*$", el.text, re.M)
                    fm.write(json.dumps({"id": int(pid), "roz": _roz,
                                         "nadpis": _m.group(1) if _m else ""},
                                        ensure_ascii=False) + "\n")
                    ib = infobox(el.text)
                    if ib and ib[1]:
                        fi.write(json.dumps({"id": int(pid), "druh": ib[0],
                                             "pary": ib[1]},
                                            ensure_ascii=False) + "\n")
                        ni += 1
            elif tag == "page":
                if ns == "0" and redir:
                    fr.write(f"{titul}\t{redir}\n")
                    nr += 1
                titul = pid = ns = redir = None
                el.clear()
            if tag == "revision":
                el.clear()
    print(f"pruchod: přesměrování {nr}, infoboxů {ni}, {time.time()-t0:.0f} s",
          flush=True)


def extrahuj():
    t0 = time.time()
    # WIKI_SEZNAMY_V1 — obal, který nezahazuje odrážkové seznamy
    subprocess.run([sys.executable, os.path.join(ROOT, "tools", "wiki_extrakt_seznamy.py"),
                    "--json", "-o", EXTR, "-b", "100M", "--processes", "2",
                    "-q", DUMP], check=True)
    print(f"extrahuj: {time.time()-t0:.0f} s", flush=True)


def index():
    t0 = time.time()
    if os.path.exists(DB):
        os.remove(DB)
    db = sqlite3.connect(DB)
    db.executescript("""
      PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;
      CREATE TABLE clanek(id INTEGER PRIMARY KEY, titul TEXT, titul_norm TEXT,
                          text TEXT, infobox_druh TEXT, infobox TEXT,
                          rozcestnik INTEGER, uvod INTEGER);
      CREATE TABLE presmerovani(titul_norm TEXT PRIMARY KEY, titul TEXT, cil TEXT);
      CREATE TABLE meta(klic TEXT PRIMARY KEY, hodnota TEXT);
    """)
    ib = {}
    with open(INFO, encoding="utf-8") as f:
        for l in f:
            d = json.loads(l)
            ib[d["id"]] = (d["druh"], "\n".join(f"{k}: {v}" for k, v in d["pary"]))
    meta = {}
    with open(META, encoding="utf-8") as f:
        for l in f:
            d = json.loads(l)
            meta[d["id"]] = (d["roz"], d["nadpis"])
    n = 0
    for p in sorted(glob.glob(os.path.join(EXTR, "*", "wiki_*"))):
        rows = []
        with open(p, encoding="utf-8") as f:
            for l in f:
                d = json.loads(l)
                if not d.get("text"):
                    continue
                i = int(d["id"])
                druh, box = ib.get(i, ("", ""))
                text = html.unescape(d["text"])
                roz, nadpis = meta.get(i, (False, ""))
                rows.append((i, d["title"], norm(d["title"]), text, druh, box,
                             int(roz), _konec_uvodu(text, nadpis)))
        db.executemany("INSERT OR REPLACE INTO clanek VALUES (?,?,?,?,?,?,?,?)",
                       rows)
        n += len(rows)
    db.execute("CREATE INDEX ix_titul ON clanek(titul_norm)")
    if os.path.exists(REDIR):
        with open(REDIR, encoding="utf-8") as f:
            db.executemany("INSERT OR IGNORE INTO presmerovani VALUES (?,?,?)",
                           ((norm(a), a, b) for a, b in
                            (l.rstrip("\n").split("\t", 1) for l in f if "\t" in l)))
    elif os.path.abspath(DB) != os.path.abspath(STARA_DB) and os.path.exists(STARA_DB):
        # WIKI_SEZNAMY_V1 — TSV smazáno; přesměrování z téhož výpisu ve staré DB
        db.execute("ATTACH DATABASE ? AS st", (STARA_DB,))
        db.execute("INSERT OR IGNORE INTO presmerovani SELECT * FROM st.presmerovani")
        db.commit()
        db.execute("DETACH DATABASE st")
    else:
        sys.exit("chybí %s" % REDIR)
    db.executescript("""
      CREATE VIRTUAL TABLE clanek_fts USING fts5(titul, text, content='clanek',
          content_rowid='id', tokenize='unicode61 remove_diacritics 2');
      INSERT INTO clanek_fts(rowid, titul, text) SELECT id, titul, text FROM clanek;
      INSERT INTO clanek_fts(clanek_fts) VALUES('optimize');
    """)
    db.execute("INSERT INTO meta VALUES ('vypis', ?)", (os.path.basename(DUMP),))
    db.execute("INSERT INTO meta VALUES ('sestaveno', ?)",
               (time.strftime("%Y-%m-%d %H:%M"),))
    db.commit()
    db.close()
    print(f"index: článků {n}, {time.time()-t0:.0f} s, "
          f"{os.path.getsize(DB)/1e9:.1f} GB", flush=True)


if __name__ == "__main__":
    krok = sys.argv[1] if len(sys.argv) > 1 else "vse"
    for k in (["pruchod", "extrahuj", "index"] if krok == "vse" else [krok]):
        globals()[k]()
    print("HOTOVO", flush=True)
