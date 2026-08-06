"""HANS_CONVINDEX_V1 (6.8.) — jeden vyhledávatelný index nad VŠEMI rozhovory.

PROČ: Hansovy rozhovory jsou uložené (deník je má všechny), ale hledat v nich
umí jen `hans_recall` přes `LIKE` + ručně psané synonymové shluky — a jen
v `human_chat`. Rozhovory s Koláčem (`teddy_dialog`, 7497 kusů) jsou dokonce
v `hans_recall._DIARY_NOISE`, tedy vyřazené jako vjemový šum. Doloženo
5.8. 19:19–19:23: uživatel se ptal na rozhovor s Koláčem a dostal sumář
rozhovoru se sebou, protože jinam se recall podívat neuměl.

ŘEŠENÍ: SQLite **FTS5** nad sjednoceným pohledem na všechny druhy rozhovorů.
Ověřeno na tomhle stroji (sqlite 3.46.1) — `unicode61 remove_diacritics 2`
plus prefixový dotaz řeší obojí, na čem čeština obvykle padá:

    „malovani" → najde „malování"      (uživatel píše bez háčků)
    „kentaur*" → najde „kentaura"      (skloňování bez slovníku)

Index je SAMOSTATNÝ soubor (`data/hans_conv_index.db`) — hlavní deník se
nesahá, a kdyby se index poškodil, smaže se a postaví znovu z deníku.

Žádný LLM, žádný démon: synchronizace je LAZY (při dotazu se dojede, co
v deníku přibylo od posledního `max(id)`). Funguje i s vypnutým PC.
"""
from __future__ import annotations

import logging
import os
import re
import sqlite3
import time
import unicodedata
from typing import Optional

_log = logging.getLogger(__name__)

INDEX_PATH = "data/hans_conv_index.db"

# Druhy deníkových událostí, které JSOU rozhovor. `dialog_reflection` sem
# ZÁMĚRNĚ nepatří — to je Hansova úvaha O dialogu, ne dialog sám (jinak by
# vyhledávání vracelo dvakrát totéž, jednou zprostředkovaně).
SOURCES = {
    "human_chat":      {"label": "s vámi",    "text": "note"},
    "teddy_dialog":    {"label": "s Koláčem", "text": "note"},
    "chat_reflection": {"label": "úvaha po rozhovoru", "text": "data"},
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS conv_doc (
    id      INTEGER PRIMARY KEY,   -- = diary.id (idempotentní sync)
    ts      REAL NOT NULL,
    source  TEXT NOT NULL,
    partner TEXT,
    topic   TEXT,
    -- Text drží TAHLE tabulka, ne FTS: `conv_fts` je contentless
    -- (`content=''`), takže sloupce při čtení vrací NULL — jen indexuje.
    -- Odhaleno testem 6.8.: nález byl správný, ale text přišel prázdný.
    text    TEXT
);
CREATE INDEX IF NOT EXISTS idx_conv_ts ON conv_doc(ts);
CREATE INDEX IF NOT EXISTS idx_conv_src ON conv_doc(source);
CREATE VIRTUAL TABLE IF NOT EXISTS conv_fts USING fts5(
    text, topic, partner,
    content='',
    tokenize='unicode61 remove_diacritics 2'
);
CREATE TABLE IF NOT EXISTS conv_meta (k TEXT PRIMARY KEY, v TEXT);
"""


def _fold(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(c for c in s if not unicodedata.combining(c)).lower()


def _connect(path: str = INDEX_PATH) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.executescript(_SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Doplní schéma staršího indexu NA MÍSTĚ (žádné mazání souboru).

    `CREATE TABLE IF NOT EXISTS` chybějící sloupec nepřidá, takže index
    postavený před opravou 6.8. nemá `conv_doc.text`. Sloupec se doplní
    a chybějící texty dojede `sync` (řádky s `text IS NULL`) — index je
    odvozený z deníku, takže se to vždycky dá dohnat.
    """
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(conv_doc)")}
        if "text" not in cols:
            conn.execute("ALTER TABLE conv_doc ADD COLUMN text TEXT")
            conn.commit()
            _log.info("HANS_CONVINDEX_V1: schéma doplněno o conv_doc.text")
    except Exception as e:
        _log.warning("convindex migrace selhala: %s", e)


# ── plnění indexu ────────────────────────────────────────────────────────

_TOPIC_PAT = re.compile(r"^\s*T[ée]ma:\s*(.+)$", re.M)


def _row_to_doc(diary_id, ts, event_type, title, data, note):
    """Deníkový řádek → (text, topic, partner) nebo None."""
    spec = SOURCES.get(event_type)
    if not spec:
        return None
    text = (note if spec["text"] == "note" else data) or ""
    if not text.strip():
        # chat_reflection má obsah v `data`, ale note bývá prázdné a naopak —
        # ber, co je k dispozici, ať se řádek neztratí.
        text = (data or note or "")
    text = text.strip()
    if not text:
        return None
    topic = ""
    m = _TOPIC_PAT.search(text)
    if m:
        topic = m.group(1).strip()[:120]
    partner = (title or "").strip()
    if event_type == "teddy_dialog":
        partner = "Koláč"
    return text, topic, partner


def sync(diary_path: str = "data/hans_diary.db",
         index_path: str = INDEX_PATH, limit: int = 0) -> int:
    """Dojede, co v deníku přibylo. Vrací počet nově zaindexovaných.

    Idempotentní: `conv_doc.id` = `diary.id`, takže opakovaný běh nic
    nezduplikuje. `limit` (>0) omezí dávku — pro první backfill po částech.
    """
    added = 0
    conn = _connect(index_path)
    try:
        # Řádky bez textu (starší schéma) dojeď dřív, než se řeší nové —
        # jinak by v indexu navždy zůstaly nálezy s prázdným obsahem.
        try:
            missing = [r[0] for r in conn.execute(
                "SELECT id FROM conv_doc WHERE text IS NULL OR text=''")]
        except Exception:
            missing = []
        if missing:
            src0 = sqlite3.connect("file:%s?mode=ro" % diary_path, uri=True,
                                   timeout=10)
            try:
                for i in range(0, len(missing), 500):
                    chunk = missing[i:i + 500]
                    q0 = ("SELECT id, ts, event_type, title, data, note FROM "
                          "diary WHERE id IN (%s)" % ",".join("?" * len(chunk)))
                    for did, ts, et, title, data, note in src0.execute(q0, chunk):
                        doc = _row_to_doc(did, ts, et, title, data, note)
                        if doc:
                            conn.execute(
                                "UPDATE conv_doc SET text=? WHERE id=?",
                                (doc[0], did))
            finally:
                src0.close()
            conn.commit()
            _log.info("HANS_CONVINDEX_V1: doplněn text u %d řádků",
                      len(missing))

        row = conn.execute("SELECT COALESCE(MAX(id), 0) FROM conv_doc").fetchone()
        last = int(row[0] or 0)
        src = sqlite3.connect("file:%s?mode=ro" % diary_path, uri=True, timeout=10)
        try:
            q = ("SELECT id, ts, event_type, title, data, note FROM diary "
                 "WHERE id > ? AND event_type IN (%s) ORDER BY id"
                 % ",".join("?" * len(SOURCES)))
            args = [last] + list(SOURCES.keys())
            if limit > 0:
                q += " LIMIT ?"
                args.append(limit)
            rows = src.execute(q, args).fetchall()
        finally:
            src.close()
        for did, ts, et, title, data, note in rows:
            doc = _row_to_doc(did, ts, et, title, data, note)
            if not doc:
                continue
            text, topic, partner = doc
            conn.execute(
                "INSERT OR REPLACE INTO conv_doc"
                "(id, ts, source, partner, topic, text)"
                " VALUES (?,?,?,?,?,?)", (did, ts, et, partner, topic, text))
            conn.execute("DELETE FROM conv_fts WHERE rowid=?", (did,))
            conn.execute(
                "INSERT INTO conv_fts(rowid, text, topic, partner) "
                "VALUES (?,?,?,?)", (did, text, topic, partner))
            added += 1
        conn.execute("INSERT OR REPLACE INTO conv_meta(k,v) VALUES('synced',?)",
                     (str(time.time()),))
        conn.commit()
    except Exception as e:
        _log.warning("convindex sync selhal: %s", e)
    finally:
        conn.close()
    if added:
        _log.info("HANS_CONVINDEX_V1: zaindexováno %d nových rozhovorů", added)
    return added


# ── dotaz ────────────────────────────────────────────────────────────────

# FTS5 má vlastní syntaxi; uživatelský text se do ní NESMÍ dostat syrový
# (uvozovky, závorky, „-" a hlavně slova jako AND/OR/NOT by shodila dotaz).
_WORD = re.compile(r"[0-9a-zá-žA-ZÁ-Ž]{2,}")
# Slova, která by dotaz jen rozmělnila (jsou skoro v každé replice).
_STOP = {
    "co", "kdo", "kde", "kdy", "jak", "jaky", "jaka", "jake", "proc", "cem",
    "jsme", "jste", "jsem", "jsi", "byl", "byla", "bylo", "bavili", "bavil",
    "mluvili", "mluvil", "povidali", "rozhovor", "rozhovoru", "rozhovory",
    "konverzace", "tema", "tematu", "pamatujes", "vzpominas", "rikal",
    "ten", "ta", "to", "toho", "tom", "tim", "se", "si", "na", "do", "od",
    "pro", "pri", "ale", "nebo", "ze", "kdyz", "uz", "jen", "take", "taky",
    "prosim", "pane", "pani", "hans", "hansi", "mi", "me", "ti", "vam",
    "kontext", "kontextu", "souvislosti", "jakem", "tom", "toho",
}


def _stem(tok: str) -> str:
    """Hrubý kmen useknutím koncovky — bez slovníku, bez závislosti.

    Prefix sám nestačí, když se mění KMEN, ne jen koncovka: „Bratřích“
    složí `bratrich*`, což na „Bratři“ (`bratri`) nesedí. Useknutí dvou
    znaků dá `bratri*` a trefí obojí. Doloženo živým testem 6.8.
    """
    if len(tok) >= 7:
        return tok[:-2]
    if len(tok) >= 5:
        return tok[:-1]
    return tok


def _fts_query(text: str, stem: bool = False) -> str:
    """Uživatelská věta → bezpečný FTS5 výraz s prefixy (kvůli skloňování).

    `stem=True` navíc ustřihne koncovku — volnější síť pro druhý pokus.
    """
    toks = []
    for w in _WORD.findall(text or ""):
        f = _fold(w)
        if len(f) < 3 or f in _STOP:
            continue
        if stem:
            f = _stem(f)
        if f and f not in toks:
            toks.append(f)
    # Prefix zvládne české koncovky bez slovníku: kentaur* → kentaura.
    return " ".join('"%s"*' % t for t in toks[:8])


def search(query: str, limit: int = 8, source: Optional[str] = None,
           partner: Optional[str] = None, index_path: str = INDEX_PATH,
           diary_path: str = "data/hans_diary.db",
           auto_sync: bool = True) -> list:
    """Najdi rozhovory k dotazu. Vrací [(ts, source, partner, topic, text)].

    Nejdřív AND (všechna slova) — přesnější; když nic, spadne na OR, ať
    dotaz s jedním přebytečným slovem nevrátí prázdno.
    """
    if auto_sync:
        try:
            sync(diary_path, index_path)
        except Exception:
            pass
    expr = _fts_query(query)
    if not expr:
        return []
    expr_stem = _fts_query(query, stem=True)
    conn = None
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % index_path, uri=True,
                               timeout=10)
        where = []
        args_tail = []
        if source:
            where.append("d.source = ?")
            args_tail.append(source)
        if partner:
            where.append("lower(d.partner) = ?")
            args_tail.append(partner.lower())
        extra = (" AND " + " AND ".join(where)) if where else ""
        sql = ("SELECT d.ts, d.source, d.partner, d.topic, d.text "
               "FROM conv_fts f JOIN conv_doc d ON d.id = f.rowid "
               "WHERE conv_fts MATCH ?" + extra +
               " ORDER BY rank, d.ts DESC LIMIT ?")
        # Kaskáda: všechna slova přesně → všechna slova po kmeni. VŽDY AND.
        #
        # ⚠️ OR fallback tu BYL a je ZÁMĚRNĚ pryč (test 6.8.): na dotaz
        # „uplne vymysleny nesmysl xyzzy“ vracel dva rozhovory s Koláčem,
        # protože stačilo, aby matchlo jediné náhodné slovo. U recallu je
        # falešný nález nejhorší možná chyba — Hans by tvrdil, že jsme
        # o něčem mluvili, a doložil by to nesouvisejícím záznamem.
        # Prázdný výsledek a poctivé „nemám zapsáno“ je vždycky lepší
        # ([[anticonfabulation-guiding-principle]]).
        attempts = [expr]
        if expr_stem and expr_stem != expr:
            attempts.append(expr_stem)
        rows = []
        for e in attempts:
            rows = conn.execute(sql, [e] + args_tail + [limit]).fetchall()
            if rows:
                break
        return [tuple(r) for r in rows]
    except Exception as e:
        _log.warning("convindex search selhal: %s", e)
        return []
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def topic_tokens(query: str, exclude: tuple = ()) -> list:
    """Obsahová slova dotazu po odečtení jmen v `exclude`.

    Slouží k rozlišení dvou různých otázek, které vypadají podobně:
        „co dělal Koláč?“              → dotaz na STAV, žádné téma
        „o čem jste se bavili o Bratřích“ → dotaz na TÉMA (Bratři)
    Bez tohohle vrátí FTS na první z nich náhodné staré dialogy, protože
    „Kolač“ je v každém z nich (doloženo živě 6.8.).
    """
    # Funkční slova sdílíme s `hans_thread` — jeden seznam, ne dvě kopie,
    # co se rozejdou (boy scout rule z CLAUDE.md). Bez nich projde „co DĚLAL
    # Koláč?" jako téma „delal" a hledání se spustí, i když téma není.
    try:
        from scripts.hans_thread import _FUNCTION_WORDS as _FW
    except Exception:
        _FW = frozenset()
    ex = {_fold(e)[:5] for e in exclude if e}
    out = []
    for w in _WORD.findall(query or ""):
        f = _fold(w)
        if len(f) < 3 or f in _STOP or f in _FW:
            continue
        if any(f.startswith(e) for e in ex if e):
            continue
        out.append(f)
    return out


def format_hits(rows: list, limit: int = 3, max_chars: int = 420) -> str:
    """Nálezy → DOSLOVNÝ výpis pro chat.

    Nic se nepřevypravuje: vypisuje se, co je zapsáno. Přeformulovat to
    modelem by znamenalo pustit konfabulaci přesně tam, kde má být záznam
    ([[anticonfabulation-guiding-principle]]).
    """
    out = []
    for ts, source, partner, topic, text in (rows or [])[:limit]:
        when = time.strftime("%d.%m.%Y v %H:%M", time.localtime(ts or 0))
        lbl = SOURCES.get(source, {}).get("label", source)
        head = "– %s (%s%s)" % (when, lbl,
                                (", téma: %s" % topic) if topic else "")
        body = (text or "").strip()
        if len(body) > max_chars:
            body = body[:max_chars].rstrip() + " …"
        out.append("%s\n%s" % (head, "\n".join(
            "   " + ln for ln in body.splitlines() if ln.strip())))
    return "\n\n".join(out)


def answer_about(query: str, source: Optional[str] = None,
                 partner: Optional[str] = None, limit: int = 3,
                 index_path: str = INDEX_PATH,
                 diary_path: str = "data/hans_diary.db") -> str:
    """Hotová odpověď „o tomhle jsme mluvili tehdy a tehdy", nebo ''."""
    rows = search(query, limit=limit, source=source, partner=partner,
                  index_path=index_path, diary_path=diary_path)
    if not rows:
        return ""
    return format_hits(rows, limit=limit)


def stats(index_path: str = INDEX_PATH) -> dict:
    conn = None
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % index_path, uri=True,
                               timeout=10)
        out = {"total": conn.execute(
            "SELECT COUNT(*) FROM conv_doc").fetchone()[0]}
        for s, c in conn.execute(
                "SELECT source, COUNT(*) FROM conv_doc GROUP BY source"):
            out[s] = c
        r = conn.execute("SELECT MIN(ts), MAX(ts) FROM conv_doc").fetchone()
        out["from"], out["to"] = (r or (None, None))
        return out
    except Exception as e:
        return {"error": str(e)}
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        for ts, src, partner, topic, text in search(" ".join(sys.argv[1:])):
            when = time.strftime("%d.%m.%Y %H:%M", time.localtime(ts))
            lbl = SOURCES.get(src, {}).get("label", src)
            print(f"\n── {when} ({lbl}{', téma: ' + topic if topic else ''})")
            print(text[:400])
    else:
        print(sync(), "nových;", stats())
