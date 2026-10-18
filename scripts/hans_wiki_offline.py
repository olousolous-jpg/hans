"""HANS_WIKI_OFFLINE_V1 — lokální česká Wikipedie (17. 10. 2026).

Čte data/wiki/cswiki.db (sestavuje tools/wiki_offline_build.py z výpisu
cswiki). Náhrada TŘÍ síťových primitiv WebReaderu, brány nad nimi zůstávají:
  prefixsearch(q)  ~ list=prefixsearch (začátky titulů, přesměrování → cíl)
  srsearch(q)      ~ list=search (FTS5, titul váží víc než text)
  extrakt(titul)   ~ prop=extracts explaintext (+ příznak rozcestníku)
Navíc infobox(titul) — řádky „klíč: hodnota“, které online výtah nemá.

Jen pro čtení. Chybí-li databáze, `dostupna()` je False a volající jde na síť.
"""
import logging
import os
import re
import sqlite3
import threading

_log = logging.getLogger(__name__)
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.environ.get("HANS_WIKI_DB") or os.path.join(_ROOT, "data", "wiki", "cswiki.db")
_tls = threading.local()


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").replace("_", " ")).strip().lower()


def _db():
    c = getattr(_tls, "db", None)
    if c is None:
        c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True,
                            check_same_thread=False)
        _tls.db = c
    return c


def dostupna() -> bool:
    try:
        if not os.path.exists(DB):
            return False
        return bool(_db().execute(
            "SELECT 1 FROM meta WHERE klic='sestaveno'").fetchone())
    except Exception as e:
        _log.debug("wiki offline nedostupná: %s", e)
        return False


def vypis() -> str:
    """Název výpisu a čas sestavení (do logu a pro stáří dat)."""
    try:
        r = dict(_db().execute("SELECT klic, hodnota FROM meta").fetchall())
        return f"{r.get('vypis', '?')} (sestaveno {r.get('sestaveno', '?')})"
    except Exception:
        return "?"


def _cil(titul: str):
    """Titul → řádek článku (id, titul); přesměrování se rozbalí (max 2×)."""
    db = _db()
    t = titul
    for _ in range(3):
        r = db.execute("SELECT id, titul FROM clanek WHERE titul_norm=? "
                       "ORDER BY length(text) DESC LIMIT 1", (_norm(t),)).fetchone()
        if r:
            return r
        p = db.execute("SELECT cil FROM presmerovani WHERE titul_norm=?",
                       (_norm(t),)).fetchone()
        if not p:
            return None
        t = p[0].split("#", 1)[0]
    return None


def _prvni_pad(q: str) -> list:
    """Jedno slovo velkým písmenem v jiném pádě → kandidáti 1. pádu
    („Babičku“ → „Babička“). Jen pro PŘESNOU shodu s titulem."""
    sl = (q or "").split()
    if len(sl) != 1 or not sl[0][:1].isupper():
        return []
    w, out = sl[0], []
    for a_, b_ in (("ku", "ka"), ("hu", "ha"), ("ou", "a"), ("u", "a"), ("y", "a"),
                   ("ě", "a"), ("i", "e"),
                   ("em", ""), ("a", ""), ("u", ""), ("ě", ""), ("e", "")):   # mužský rod
        if w.endswith(a_) and len(w) > 4:
            out.append(w[:-len(a_)] + b_)
    return list(dict.fromkeys(out))


_UPRESNENI = {"kniha": ("kniha", "román", "novela"), "film": ("film",), "seriál": ("seriál",),
              "hora": ("hora",), "hrad": ("hrad",), "zámek": ("zámek",), "řeka": ("řeka",)}

_DRUHY_SLOVA = {"hora", "hrad", "zámek", "řeka", "město", "jezero", "ostrov", "film",
                "kniha", "seriál", "kostel", "katedrála", "chrám", "muzeum", "kapela", "loď"}


def _presna_zaloha(q: str):
    """HANS_WIKI_OFFLINE_DIRY_V1 — přesný titul i (a) bez druhového slova
    („hora Mount Everest“, „kniha Krakatit“ → „Krakatit (kniha)“/„Krakatit“),
    (b) v 1. pádě u jednoho slova („Babičku“ → „Babička“). Jen přesná shoda,
    rozcestník ne. → (titul dotazu, řádek článku) nebo None."""
    db = _db()
    sl = q.split()
    zkus = []
    if len(sl) >= 2 and sl[0].lower() in _DRUHY_SLOVA and sl[1][:1].isupper():
        zbytek = " ".join(sl[1:])
        zkus += ["%s (%s)" % (zbytek, u) for u in _UPRESNENI.get(sl[0].lower(), ())]
        zkus.append(zbytek)
    zkus += _prvni_pad(q)
    for z in zkus:
        r = _cil(z)
        if r and not db.execute("SELECT rozcestnik FROM clanek WHERE id=?",
                                (r[0],)).fetchone()[0]:
            return z, r
        # HANS_WIKI_DRUH_ROK_V1 (18. 10.) — „film Titanic“: „Titanic (film)“ je
        # rozcestník a článek se jmenuje „Titanic (film, 1997)“ → hledání
        # propadlo až na „Titan (film)“. Jediná varianta „X (druh, …)“ se vezme
        # HNED (před holým „X“ = loď); víc variant (Duna 1984 × 2021) → beze
        # změny. 📏 13 logovaných dohledání s druhovým slovem: změna jen Titanic.
        if z.endswith(")") and " (" in z:
            n = _norm(z[:-1] + ", ")
            var = [t for t, roz in db.execute(
                "SELECT titul, rozcestnik FROM clanek WHERE titul_norm >= ? "
                "AND titul_norm < ? LIMIT 5", (n, n + "\U0010ffff")) if not roz]
            if len(var) == 1:
                r = _cil(var[0])
                if r:
                    return var[0], r
    return None


def prefixsearch(q: str, limit: int = 3) -> list:
    """Tituly začínající dotazem (bez ohledu na velikost písmen). Přesná
    shoda první, pak delší články (náhrada popularity). Přesměrování → cíl."""
    n = _norm(q)
    if not n:
        return []
    db = _db()
    # rozsah nad indexem (LIKE … ESCAPE index nepoužije → 1,4 s průchod)
    do = n + "\U0010ffff"
    kand = {}
    for tit, delka in db.execute(
            "SELECT titul, length(text) FROM clanek WHERE titul_norm >= ? "
            "AND titul_norm < ? ORDER BY length(text) DESC LIMIT 40", (n, do)):
        kand.setdefault(tit, delka)
    # přesměrování vrací SVŮJ titul jako online API („Offside“, ne „Ofsajd“) —
    # brány porovnávají titul s dotazem, článek se dočte přes přesměrování
    for (tit, cil) in db.execute(
            "SELECT titul, cil FROM presmerovani WHERE titul_norm >= ? "
            "AND titul_norm < ? LIMIT 40", (n, do)):
        r = _cil(cil)
        if r:
            d = db.execute("SELECT length(text) FROM clanek WHERE id=?",
                           (r[0],)).fetchone()
            kand.setdefault(tit, d[0] if d else 0)
    ex = _cil(q)
    if not ex:
        z = _presna_zaloha(q)
        if z:
            q, ex = z
    out = []
    if ex:
        p = db.execute("SELECT titul FROM presmerovani WHERE titul_norm=?",
                       (_norm(q),)).fetchone()
        jako_clanek = db.execute("SELECT 1 FROM clanek WHERE titul_norm=?",
                                 (_norm(q),)).fetchone()
        out = [p[0] if (p and not jako_clanek) else ex[1]]
    for t, _ in sorted(kand.items(), key=lambda x: -x[1]):
        if t not in out:
            out.append(t)
    return out[:limit]


_SLOVO = re.compile(r"\w+", re.U)
_STOP = {"a", "i", "v", "ve", "s", "se", "si", "z", "ze", "o", "k", "ke", "na",
         "do", "od", "po", "u", "za", "pro", "při", "the", "of", "and"}


def _slova(q: str) -> list:
    return [w for w in _SLOVO.findall(q or "") if w.lower() not in _STOP]


def _kmen(w: str, kratke: bool = False) -> str:
    """Skloňování: bez koncovky, aspoň 4 znaky (Čechách → Čech, Jiřím → Jiř,
    Bacha → Bach); kratší slova a čísla celá. `kratke` (víceslovný dotaz,
    ostatní slova ho zúží): i 3–4písmenná slova bez koncovky („Lva“ → „Lv“,
    „Richarda Lva Srdce“ → „Richard I. Lví srdce“)."""
    if len(w) >= 5 and not w.isdigit():
        return w[:max(4 if not kratke else 3, len(w) - 2)]
    if kratke and 3 <= len(w) <= 4 and not w.isdigit() and not w.isupper():
        return w[:len(w) - 1]
    return w


def _fts_dotaz(q: str, spojka: str) -> str:
    casti = []
    _vic = len(_slova(q)) >= 3
    for w in _slova(q):
        k = _kmen(w, _vic).replace('"', '')
        casti.append('"%s"*' % k if k != w else '"%s"' % k)
    return f" {spojka} ".join(casti)


def _fold(s: str) -> str:
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFD", s.lower())
                   if unicodedata.category(c) != "Mn")


def srsearch(q: str, limit: int = 5) -> list:
    """Fulltext: všechna slova (AND), když nic, aspoň některá (OR); rozcestníky
    vynechány. bm25 (titul 10× těžší) vybere 50 kandidátů, pořadí pak:
    přesná shoda titulu (i přes přesměrování) → titul obsahuje všechna slova
    dotazu → zbytek podle bm25 (bm25 sám trestá dlouhé články, takže by heslo
    „Praha“ prohrálo se „Slavia Praha“)."""
    db = _db()
    slova = {w.lower() for w in _slova(q)}
    for spojka in ("AND", "OR"):
        dotaz = _fts_dotaz(q, spojka)
        if not dotaz:
            return []
        try:
            rows = db.execute(
                "SELECT c.titul FROM clanek_fts f JOIN clanek c ON c.id=f.rowid "
                "WHERE clanek_fts MATCH ? AND c.rozcestnik=0 "
                "ORDER BY bm25(clanek_fts, 10.0, 1.0) LIMIT 50",
                (dotaz,)).fetchall()
        except sqlite3.OperationalError as e:
            _log.debug("wiki offline srsearch %r: %s", dotaz, e)
            rows = []
        if rows:
            tituly = [r[0] for r in rows]
            ex = _cil(q)
            if not ex:
                _z = _presna_zaloha(q)
                if _z:
                    ex = _z[1]
            if ex and ex[1] not in tituly:
                roz = db.execute("SELECT rozcestnik FROM clanek WHERE id=?",
                                 (ex[0],)).fetchone()
                if roz and not roz[0]:
                    tituly.insert(0, ex[1])

            kmeny = [_fold(_kmen(w, len(_slova(q)) >= 3)) for w in _slova(q)]

            # HANS_WIKI_OFFLINE_DIRY_V1 — „Babičku“: tituly se základem v 1. pádě
            # („Babička (kniha)“) dopředu, ať nepropadne „Babice (Kbel)“
            _pp = {_norm(x) for x in _prvni_pad(q)}
            if _pp:
                for (t2,) in db.execute(
                        "SELECT titul FROM clanek WHERE rozcestnik=0 AND (" +
                        " OR ".join(["titul_norm=? OR titul_norm LIKE ?"] * len(_pp)) + ")",
                        [x for v in _pp for x in (v, v + " (%")]).fetchall():
                    if t2 not in tituly:
                        tituly.append(t2)

            def _rad(ti):
                # přesný titul → titul pokrývá kmeny všech slov dotazu (méně
                # slov navíc dřív: „Jiří z Poděbrad“ před „Náměstí Jiřího z
                # Poděbrad“) → zbytek podle bm25
                i, t = ti
                if ex and t == ex[1]:
                    return (0, 0, i)
                if _pp and _norm(t) in _pp:
                    return (0, 1, i)            # přesně 1. pád („Karlštejn“)
                if _pp and _norm(re.sub(r"\s*\(.*?\)", "", t)) in _pp:
                    return (0, 2, i)            # s upřesněním („Babička (kniha)“)
                tw = [_fold(w) for w in _SLOVO.findall(re.sub(r"\(.*?\)", "", t))
                      if w.lower() not in _STOP]
                if kmeny and all(any(w.startswith(k) for w in tw) for k in kmeny):
                    navic = sum(1 for w in tw if not any(w.startswith(k) for k in kmeny))
                    return (1, navic, i)
                return (2, 0, i)
            return [t for _, t in sorted(enumerate(tituly), key=_rad)][:int(limit)]
    return []


_JAZYK = {"de": "německy", "en": "anglicky", "ru": "rusky", "fr": "francouzsky",
          "hu": "maďarsky", "pl": "polsky", "cs": "česky", "uk": "ukrajinsky",
          "la": "latinsky", "it": "italsky", "sk": "slovensky", "bg": "bulharsky",
          "es": "španělsky", "hsb": "hornolužickosrbsky", "dsb": "dolnolužickosrbsky",
          "grc": "starořecky", "el": "řecky", "sv": "švédsky", "sl": "slovinsky",
          "mk": "makedonsky", "hr": "chorvatsky", "sr": "srbsky", "pt": "portugalsky",
          "nl": "nizozemsky", "ja": "japonsky", "zh": "čínsky", "ar": "arabsky"}
_HATNOTE = re.compile(r"^(?:Tento článek (?:je o|obsahuje)|Na tento článek je přesměrováno|"
                      r"Na je heslo|Další významy jsou uvedeny|Související informace "
                      r"naleznete|Podrobnější informace naleznete|O .{1,80} pojednává)")
_NADPIS = re.compile(r"^([A-ZÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ0-9][^.!?\n]{0,70})\.$")


def _jako_online(text: str) -> str:
    """Tvar jako `prop=extracts explaintext`: bez odkazů na rozcestník a „viz
    též“, nadpisy „== X ==“ (studium podle nich řeže Reference/Odkazy), jazyková
    šablona „v jazyce de "X")“ → „německy X“."""
    text = re.sub(r'v jazyce (\w+) "([^"\n]*?)(\)?)"',
                  lambda m: f'{_JAZYK.get(m.group(1), m.group(1))} {m.group(2)}{m.group(3)}',
                  text)
    out = []
    for i, l in enumerate(text.split("\n")):
        if _HATNOTE.match(l.strip()):
            continue
        m = _NADPIS.match(l.strip()) if i else None
        # nadpis je krátký (≤ 6 slov mimo závorku); delší je jednovětý odstavec
        if m and len(re.sub(r"\(.*?\)", "", m.group(1)).split()) > 6:
            m = None
        out.append(f"\n\n== {m.group(1)} ==" if m else l)
    return re.sub(r"\n{4,}", "\n\n\n", "\n".join(out)).strip()


def extrakt(titul: str, intro_only: bool = False):
    """(text, je_rozcestnik, kanonický titul) nebo ("", False, None)."""
    r = _cil(titul)
    if not r:
        return "", False, None
    text, roz, uvod = _db().execute(
        "SELECT text, rozcestnik, uvod FROM clanek WHERE id=?", (r[0],)).fetchone()
    if roz:
        return "", True, r[1]
    if intro_only:
        text = text[:uvod]
    return _jako_online(text), False, r[1]


def infobox(titul: str):
    """(druh, text „klíč: hodnota“ po řádcích) nebo ("", "")."""
    r = _cil(titul)
    if not r:
        return "", ""
    row = _db().execute("SELECT infobox_druh, infobox FROM clanek WHERE id=?",
                        (r[0],)).fetchone()
    return (row[0] or "", row[1] or "") if row else ("", "")
