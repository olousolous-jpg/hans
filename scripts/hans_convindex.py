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

# Co se indexuje. `kind` dělí dva různé světy, které se nesmí míchat:
#   talk      = co jsme si ŘEKLI  (rozhovory)
#   knowledge = co Hans VÍ        (studium, četba)
# `dialog_reflection` sem ZÁMĚRNĚ nepatří — to je úvaha O dialogu, ne dialog
# sám (vyhledávání by vracelo dvakrát totéž, jednou zprostředkovaně).
#
# HANS_CONVINDEX_KNOWLEDGE_V1 (6.8.) — znalostní zdroje přibyly proto, že Hans
# zapíral, co má nastudované: „co mi můžeš říct o křižáckých stavebních
# technikách?" → „nemám spolehlivý záznam", ačkoli `study_note` z téhož dne
# mluví o křižáckých státech a jejich hradní architektuře. RAG i
# `already_studied` minuly na lexikální neshodě (1 společné slovo);
# FTS s kmeny a prefixy má šanci výrazně vyšší.
SOURCES = {
    # ── co jsme si řekli ──────────────────────────────────────────────
    "human_chat":       {"label": "s vámi", "text": "note", "kind": "talk"},
    "teddy_dialog":     {"label": "s Koláčem", "text": "note", "kind": "talk"},
    "chat_reflection":  {"label": "úvaha po rozhovoru", "text": "data",
                         "kind": "talk"},
    # ── co Hans ví ────────────────────────────────────────────────────
    # Pořadí = klesající hodnota obsahu: destilát studia > shrnutí >
    # poznámka z četby > surový výcuc článku.
    "study_note":       {"label": "z mého studia", "text": "data",
                         "kind": "knowledge"},
    "study_mastery":    {"label": "shrnutí studia", "text": "data",
                         "kind": "knowledge"},
    "reading_takeaway": {"label": "z četby", "text": "data",
                         "kind": "knowledge"},
    "web_read":         {"label": "z článku", "text": "note",
                         "kind": "knowledge"},
    # HANS_CONVINDEX_BOOKS_V1 (21.8.) — ČETBA KNIH TU CHYBĚLA.
    # Doloženo v simulovaném rozhovoru: na „kdo tu knihu napsal?" Hans
    # u Pride and Prejudice odpověděl, že nemá spolehlivý záznam — přitom má
    # 21 zápisků, kde píše „paní Austen…", jenže všechny jsou typu
    # `book_reflection` / `book_read`, a ty index neznal. Proto „Austen"
    # tři shody vrátí, ale „Pride and Prejudice" ani jednu.
    # ⚠️ TÁŽ VÝJIMKA se opravovala 15.7. v `_READ_TYPES` pro /cetl — tady
    # na ni nikdo nesáhl. Když se přidává nový typ čtení, patří do OBOU.
    "book_reflection":  {"label": "z četby knihy", "text": "data",
                         "kind": "knowledge"},
    "book_read":        {"label": "přečtená kapitola", "text": "data",
                         "kind": "knowledge"},
}


def sources_of_kind(kind: str) -> list:
    """Názvy event_type daného druhu ('talk' / 'knowledge')."""
    return [k for k, v in SOURCES.items() if v.get("kind") == kind]

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
    # U znalostních zdrojů je TITLE to nejcennější pro vyhledávání — nese
    # název pod-tématu („Studium: hrady… — Křižácké hrady v Levantě"), který
    # se v samotném textu nemusí objevit ani jednou. Jde tedy do `topic`
    # (indexovaný sloupec), ne do `partner` (to je pro rozhovory).
    if spec.get("kind") == "knowledge":
        topic = (title or topic or "").strip()[:160]
        partner = ""
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

        # Watermark PER ZDROJ, ne globální. Globální `id > MAX(id)` znamená,
        # že nově přidaný zdroj se NIKDY nedojede — jeho záznamy mají nižší
        # id než poslední zaindexovaný rozhovor. Doloženo 6.8. při přidání
        # znalostních zdrojů: sync ohlásil „0 nových", ačkoli v deníku čekalo
        # ~4800 záznamů.
        marks = {s: 0 for s in SOURCES}
        for s, mx in conn.execute(
                "SELECT source, MAX(id) FROM conv_doc GROUP BY source"):
            marks[s] = int(mx or 0)
        src = sqlite3.connect("file:%s?mode=ro" % diary_path, uri=True, timeout=10)
        rows = []
        try:
            for s, mark in marks.items():
                q = ("SELECT id, ts, event_type, title, data, note FROM diary "
                     "WHERE event_type = ? AND id > ? ORDER BY id")
                args = [s, mark]
                if limit > 0:
                    q += " LIMIT ?"
                    args.append(limit)
                rows.extend(src.execute(q, args).fetchall())
        finally:
            src.close()
        rows.sort(key=lambda r: r[0])
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
    # Pevná délka kmene, ne useknutí koncovky: „ceskem" (6) a „cesky" (5) by
    # useknutím daly „ceske" × „cesk" a minuly se. Prefix na PEVNOU délku dá
    # obojí „cesk" a FTS `cesk*` trefí i „Česko", „česká".
    # Délky voleny podle reálných dotazů: „hradech"→„hrad" musí trefit
    # „hrady", „zbrojnice"→„zbro" musí trefit „zbrojnic". Delší prefix
    # (5) obojí minul kvůli české alternaci koncovek (6.8.).
    if len(tok) >= 7:
        return tok[:4]
    if len(tok) >= 5:
        return tok[:4]
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


_KONEC_VETY = ".!?…:;"


def kotvy_ve_vete(veta: str) -> list:
    """Slova s velkým písmenem UVNITŘ věty = předmět dotazu („hrad Kost").

    HANS_CONVINDEX_ANCHOR_SENTENCE_V1 (22.8.) — ZAČÁTEK DALŠÍ VĚTY KOTVA NENÍ.
    Původní pravidlo („velké písmeno a není to první slovo") bralo i slovo za
    tečkou. Změřeno na 500 skutečných replikách: ve 20 z nich (4 %) vznikla
    kotva ze slova jako „Rád", „Myslím", „Půjdu" — a kotva se z relaxace
    NEUBÍRÁ, takže by takový balast zůstal viset ve všech stupních a hledání
    by nenašlo nic. To je přesně ta falešná absence, proti které relaxace
    vznikla (6.8.). Když jméno stojí na začátku věty, kotva prostě nebude
    a chová se to jako dřív — raději nic než balast.

    Vrací dvojice (pořadí ve slovech, slovo).
    """
    out = []
    text = veta or ""
    for i, m in enumerate(_WORD.finditer(text)):
        w = m.group(0)
        if i == 0 or not w[:1].isupper() or w.isupper():
            continue
        pred = text[:m.start()].rstrip()
        if not pred or pred[-1] in _KONEC_VETY:
            continue
        out.append((i, w))
    return out


def relax_attempts(query: str):
    """HANS_CONVINDEX_RELAX_FN_V1 (22.8.) — postupné ubírání slov z dotazu.

    Vytaženo ze `search()`, aby šlo TESTOVAT bez FTS indexu (v regresní sadě)
    a aby žebřík měl jedno místo. Vrací (kroky, uzky):
      kroky — postupně volnější AND výrazy (bez prvních dvou, ty staví search),
      uzky  — nejužší stupeň, který se smí hledat jen v TITULU kurátorovaných
              zdrojů.
    Chování se extrakcí nemění.
    """
    kroky, narrow = [], []
    raw = [t for t in _WORD.findall(query or "")
           if len(_fold(t)) >= 3 and _fold(t) not in _STOP]
    raw.sort(key=len, reverse=True)
    toks = [_stem(_fold(t)) for t in raw]
    # HANS_CONVINDEX_ANCHOR_V1 (22.8.) — KOTVA DOTAZU SE NEUBÍRÁ.
    # Doloženo 21.8. („hrad Kost má zámecký park"): žebřík ubírá
    # NEJKRATŠÍ token, jenže u českých vlastních jmen je nejkratší
    # slovo právě to jediné specifické. „Potřebuji více informací
    # o hradě Kost" ztratilo `kost` hned v prvním stupni, zbyl
    # balast `potřebuji`+`informací` → vrátilo to zápisky o Pátém
    # elementu, ty se poslaly do promptu pod hlavičkou „tohle máš
    # ve svých zápiscích" → Hans o hradu napsal celý smyšlený
    # článek. („co víš o hradu Kost?" totéž s Cardiffským hradem.)
    # Kotva = slovo s velkým písmenem UVNITŘ věty (první slovo se
    # nepočítá, to je velké vždycky). Ta zůstává ve všech stupních.
    # Druhá půlka opravy: krátká kotva se hledá jako CELÉ SLOVO.
    # Prefix `kost*` trefí kostýmů/kostel/kosti — Pátý element se
    # do podkladu dostal i touhle druhou cestou, takže samotné
    # zachování tokenu by nestačilo (změřeno).
    _anchor_f = {_fold(_w) for _, _w in kotvy_ve_vete(query)}
    _orig = {}          # kmen -> (složené slovo, délka originálu)
    for _t in raw:
        _orig.setdefault(_stem(_fold(_t)), (_fold(_t), len(_t)))
    _anchor = {_s for _s, (_f, _l) in _orig.items()
               if _f in _anchor_f}

    def _term(_s):
        _f, _l = _orig.get(_s, (_s, len(_s)))
        if _s in _anchor and _l <= 5:
            return '"%s"' % _f
        return '"%s"*' % _s

    while len(toks) > 1:
        _drop = [_i for _i, _t in enumerate(toks) if _t not in _anchor]
        if not _drop:
            break       # zbyly samé kotvy — dál se ubírat nesmí
        toks.pop(_drop[-1])
        # Na JEDINÉ slovo se smí zúžit jen výraz, který je sám o sobě
        # dost specifický (≥7 znaků v originále). Bez téhle podmínky
        # spadlo „co víš o českém ráji" na „1. česká fotbalová liga" —
        # zbylo obecné „cesk*" a trefilo cokoli českého (6.8.).
        if len(toks) == 1:
            # Délka se bere ze SKUTEČNĚ zbylého tokenu (dřív `raw[0]`,
            # tj. nejdelší slovo — s kotvou už to nemusí být totéž).
            # Kotva smí zůstat sama i když je krátká: je to předmět
            # dotazu, ne obecné slovo (a hledá se na celé slovo).
            if (_orig.get(toks[0], ("", 0))[1] < 7
                    and toks[0] not in _anchor):
                break
            # Nejužší stupeň má dvě pojistky, obě doložené 6.8.:
            #  (a) hledá POUZE V TITULU — ten říká, o čem zápisek JE,
            #      kdežto text zmiňuje kdeco okrajově;
            #  (b) jen ve zdrojích, které Hans SYSTEMATICKY studoval
            #      (kurátorované tituly „Studium: X — Y"), ne v surové
            #      četbě. Bez (b) trefila „kvantová teleportace
            #      mravenců" článek „Seznam majitelů televizních práv"
            #      (kmen „tele*" sedl na „televizních").
            narrow.append('topic:%s' % _term(toks[0]))
            break
        kroky.append(" ".join(_term(t) for t in toks))
    return kroky, narrow


# HANS_ANCHOR_LOOKUP_V1 (22.8.) — druhové slovo ke kotvě. Samotné „Kost" vede
# na Wikipedii na KOST JAKO TKÁŇ (změřeno); „hrad Kost" na správný hrad. Klíč =
# tvary, jak je lidé píšou v dotazu, hodnota = 1. pád do vyhledávání.
_DRUH = {}
for _n, _tvary in {
        "hrad": "hrad hradu hradě hrade hradem hrady hradech",
        "zámek": "zámek zámku zámkem zámky",
        "kostel": "kostel kostela kostele kostelem",
        "klášter": "klášter kláštera klášteře klášterem",
        "město": "město města městě městem",
        "obec": "obec obce obci",
        "řeka": "řeka řeky řece řeku řekou",
        "hora": "hora hory hoře horu horou",
        "kniha": "kniha knihy knize knihu knihou",
        "film": "film filmu filmem filmy",
        "seriál": "seriál seriálu seriálem",
        "kapela": "kapela kapely kapele kapelu",
        "jezero": "jezero jezera jezeře",
        "ostrov": "ostrov ostrova ostrově",
        "muzeum": "muzeum muzea muzeu",
}.items():
    for _t in _tvary.split():
        _DRUH[_t] = _n


def kotva_tematu(veta: str, vynech: tuple = ()) -> Optional[str]:
    """HANS_ANCHOR_LOOKUP_V1 — předmět dotazu jako téma k DOHLEDÁNÍ.

    „co vix muzes rici o hradu Kost?" → „hrad Kost".
    Kotva se pozná stejně jako v `relax_attempts` (velké písmeno UVNITŘ věty),
    aby měl systém na „o čem ta otázka je" jedno místo, ne dvě rozcházející se.
    Vrací None, když kotva není nebo je to jméno člověka (`vynech`) — o lidech
    z domácnosti se na Wikipedii nehledá.
    """
    words = _WORD.findall(veta or "")
    if len(words) < 2:
        return None
    vyn = {_fold(v) for v in (vynech or ()) if v}
    kotvy = kotvy_ve_vete(veta)
    idx = {i for i, _ in kotvy}

    def _je_vyloucene(slovo: str) -> bool:
        # Jméno v dotazu bývá SKLONĚNÉ („Hansi", „Standovi", „Standy") —
        # holá shoda by vyloučení minula a Hans by šel hledat domácí (nebo
        # sám sebe) na Wikipedii. Porovnává se proto kmen bez koncové
        # samohlásky („standa" → „stand"), s dovolenými třemi znaky navíc,
        # ať se z toho nestane prefixové síto na cokoli.
        f = _fold(slovo)
        for v in vyn:
            if f == v:
                return True
            kmen = v[:-1] if v[-1:] in "aeiouy" else v
            if len(kmen) >= 3 and f.startswith(kmen) and len(f) - len(kmen) <= 3:
                return True
        return False

    preskoc_do = 0
    for i, w in kotvy:
        if i < preskoc_do:
            continue        # druhé slovo téhož jména („Sherlock Holmes")
        jmeno = [w]
        j = i + 1
        # Pokračování jména jen po dalších KOTVÁCH — jinak by se „…, Hansi.
        # Četl jsi…" slepilo na téma „Hansi Četl" (věta mezi tím skončila).
        while j in idx:
            jmeno.append(words[j]); j += 1
        preskoc_do = j
        if any(_je_vyloucene(x) for x in jmeno):
            continue
        druh = _DRUH.get(_fold(words[i - 1]), "")
        return ((druh + " ") if druh else "") + " ".join(jmeno)
    return None


def search(query: str, limit: int = 8, source: Optional[str] = None,
           partner: Optional[str] = None, index_path: str = INDEX_PATH,
           diary_path: str = "data/hans_diary.db",
           auto_sync: bool = True, kind: Optional[str] = None) -> list:
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
        elif kind:
            # „co jsme si řekli" × „co vím" se nesmí míchat — dotaz na
            # znalost nemá vracet útržky rozhovorů a naopak.
            srcs = sources_of_kind(kind)
            if not srcs:
                return []
            where.append("d.source IN (%s)" % ",".join("?" * len(srcs)))
            args_tail.extend(srcs)
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
        narrow = []      # stupně omezené na systematicky studovaná témata
        if expr_stem and expr_stem != expr:
            attempts.append(expr_stem)
        # RELAXACE — jen pro znalosti. Věta „řekni mi, jak se vyvíjely
        # zbrojnice" nese balast („rekni", „vyvijely"), který AND zabije,
        # ačkoli `study_note` „Vývoj zbrojnic" existuje (doloženo 6.8.).
        # Postupně se odebírá NEJKRATŠÍ token — kratší slovo bývá obecnější,
        # delší specifičtější. U rozhovorů se NErelaxuje: tam by falešný
        # nález znamenal „mluvili jsme o tom", což je horší než mlčet.
        if kind == "knowledge":
            _kroky, _uzky = relax_attempts(query)
            attempts.extend(_kroky)
            narrow.extend(_uzky)
        rows = []
        for e in attempts:
            rows = conn.execute(sql, [e] + args_tail + [limit]).fetchall()
            if rows:
                break
        if not rows and narrow:
            # HANS_CONVINDEX_BOOKS_V1 (21.8.) — knižní reflexe patří mezi
            # kurátorované zdroje: je to Hansovo VLASTNÍ psaní o knize, kterou
            # čte (titul „Pride and Prejudice — kap. 12" říká, o čem zápisek
            # je), ne surový výcuc z webu, kvůli kterému výčet vznikl.
            # Bez toho končí relaxace naprázdno a Hans zapře i autorku, o níž
            # má 21 zápisků (doloženo 21.8.).
            curated = ("study_note", "study_mastery", "book_reflection")
            sql_n = ("SELECT d.ts, d.source, d.partner, d.topic, d.text "
                     "FROM conv_fts f JOIN conv_doc d ON d.id = f.rowid "
                     "WHERE conv_fts MATCH ? AND d.source IN (" +
                     ",".join("?" * len(curated)) +
                     ") ORDER BY rank, d.ts DESC LIMIT ?")
            for e in narrow:
                rows = conn.execute(
                    sql_n, [e] + list(curated) + [limit]).fetchall()
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
                 diary_path: str = "data/hans_diary.db",
                 kind: Optional[str] = None) -> str:
    """Hotová odpověď „o tomhle jsme mluvili tehdy a tehdy", nebo ''."""
    rows = search(query, limit=limit, source=source, partner=partner,
                  index_path=index_path, diary_path=diary_path, kind=kind)
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
