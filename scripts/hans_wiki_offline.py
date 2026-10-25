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


_UPRESNENI = {"kniha": ("kniha", "román", "novela", "povídka"), "film": ("film",), "seriál": ("seriál",),
              # HANS_WIKI_DILO_DRUH_V1 — druh díla přímo v kotvě („novela Proměna“)
              "román": ("román", "novela", "povídka", "kniha"),
              "novela": ("novela", "povídka", "román", "kniha"),
              "povídka": ("povídka", "novela", "román", "kniha"),
              "hora": ("hora",), "hrad": ("hrad",), "zámek": ("zámek",), "řeka": ("řeka",)}

_DRUHY_SLOVA = {"hora", "hrad", "zámek", "řeka", "město", "jezero", "ostrov", "film",
                "kniha", "seriál", "kostel", "katedrála", "chrám", "muzeum", "kapela", "loď",
                "román", "novela", "povídka"}   # HANS_WIKI_DILO_DRUH_V1
# HANS_WIKI_DILO_DRUH_V1 — druh kotvy → druh infoboxu holého titulu („Psycho“
# je film 1960 s infoboxem filmu; remake má „(film, 1998)“)
_DRUH_INFOBOX = {"film": "film"}
# „Karlova mostu“ → „Karlův most“ (přivlastňovací 2. pád; /tazatel 18. 10.)
_PRIVL_GEN = re.compile(r"^(\w+?)(ova|ovy|ina|iny) (\w+?)(u|a|e|y)$")


# ── HANS_WIKI_PADY_VIC_V1 (20. 10.) — víceslovný název v jiném pádě → 1. pád ──
# `_prvni_pad` umí jen jedno slovo, takže kotva ze skloněné věty hledala
# cizí heslo: „Jana Lucemburského“ → tituly velkovévody, „Marii Terezii“ →
# Tereza Marinovová, „Karla IV.“ → Sady Karla IV., „Karlově mostě“ → nic
# (/tazatel 20. 10., 3× za den). Každé slovo dostane kandidáty 1. pádu,
# bere se jen PŘESNÁ shoda s titulem (ne rozcestník); slova za předložkou
# („z Poděbrad“) se neskloňují.
_PADY_PRIDAVNA = (("ého", "ý"), ("ému", "ý"), ("ém", "ý"), ("ým", "ý"), ("ými", "ý"),
                  ("ou", "á"), ("é", "á"), ("é", "ý"),
                  ("ově", "ův"), ("ovu", "ův"), ("ova", "ův"), ("ovým", "ův"),
                  ("ině", "in"), ("inu", "in"), ("ina", "in"))
_PADY_PODSTATNA = (("ovi", ""), ("em", ""), ("ii", "ie"), ("ií", "ie"), ("ie", "ie"),
                   ("la", "el"), ("lu", "el"), ("lovi", "el"), ("ra", "er"), ("ru", "er"),
                   ("ka", "ek"), ("ku", "ek"),
                   ("y", "a"), ("i", "a"), ("u", "a"), ("ou", "a"), ("e", "a"), ("ě", "a"),
                   ("a", ""), ("u", ""), ("e", ""), ("ě", ""), ("i", ""),
                   # HANS_WIKI_PADY_A_E_V1 (22. 10.) — cizí příjmení na -e mají
                   # 2. pád na -a: „Tomáše Garrigua Masaryka“ → „Garrigu“, takže
                   # přesný titul chyběl, hledání sklouzlo na „Řád Tomáše
                   # Garrigua Masaryka“, dohledání se nespustilo a odpověď byla
                   # z hlavy („Charlotte Jeřábková“); „Kipa Thorna“ → nic.
                   # 📏 153 kotev z korpusu: 2 změny (Masaryk, Kip Thorne), jinak 0.
                   ("a", "e"))
_PREDLOZKY = {"z", "ze", "na", "nad", "pod", "u", "v", "ve", "von", "van", "de", "da", "di",
              "of", "the", "a", "aneb", "and"}


def _tvary_slova(w: str) -> list:
    if re.fullmatch(r"[IVXLC]+\.?", w) or len(w) < 3:
        return [w]   # malá písmena ano: „Karlově mostě“ → „Karlův most“
    out = [w]
    for a_, b_ in _PADY_PRIDAVNA + _PADY_PODSTATNA:
        if w.endswith(a_) and len(w) - len(a_) >= 2:
            out.append(w[:-len(a_)] + b_)
    return list(dict.fromkeys(out))


def _prvni_pad_vic(q: str, strop: int = 200) -> list:
    """Kandidáti 1. pádu víceslovného názvu (2–4 slova)."""
    sl = (q or "").split()
    if not (2 <= len(sl) <= 4):
        return []
    varianty = []
    za_predlozkou = False
    for w in sl:
        if w.lower() in _PREDLOZKY:
            za_predlozkou = True
        varianty.append([w] if za_predlozkou else _tvary_slova(w))
    out = [[]]
    for v in varianty:
        out = [o + [x] for o in out for x in v][:strop]
    return [" ".join(o) for o in out if " ".join(o) != q]


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
        # HANS_WIKI_DILO_DRUH_V1 — holé „X“, jehož infobox je téhož druhu
        # („film Psycho“ → „Psycho“ = film 1960), má přednost před variantou
        # s rokem („Psycho (film, 1998)“, remake)
        _ib = _DRUH_INFOBOX.get(sl[0].lower())
        if _ib:
            r = _cil(zbytek)
            if r and db.execute("SELECT rozcestnik=0 AND infobox_druh=? FROM clanek "
                                "WHERE id=?", (_ib, r[0])).fetchone()[0]:
                return zbytek, r
        zkus += ["%s (%s)" % (zbytek, u) for u in _UPRESNENI.get(sl[0].lower(), ())]
        zkus.append(zbytek)
        zkus += _prvni_pad(zbytek)   # HANS_WIKI_PADY_VIC_V1 — „město Korsiky“ → Korsika
    _pg = _PRIVL_GEN.match(q.strip())
    if _pg:   # HANS_WIKI_DILO_DRUH_V1 — „Karlova mostu“ → „Karlův most“
        zkus.append("%sův %s" % (_pg.group(1), _pg.group(3)) if _pg.group(2)[0] == "o"
                    else "%sin %s" % (_pg.group(1), _pg.group(3)))
    zkus += _prvni_pad(q)
    zkus += _prvni_pad_vic(q)   # HANS_WIKI_PADY_VIC_V1
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


def film_holy_titul(q: str):
    """HANS_WIKI_DILO_DRUH_V1 — „film Psycho“ → „Psycho“, když holý titul je
    článek s infoboxem filmu (originál 1960; remake má „(film, 1998)“). Brána
    shody v `web_reader` holý titul jinak zamítne (chybí v něm slovo „film“)
    a srsearch najde remake. Jinak None."""
    try:
        sl = (q or "").split()
        if len(sl) < 2 or sl[0].lower() not in _DRUH_INFOBOX or not sl[1][:1].isupper():
            return None
        db = _db()
        zb = " ".join(sl[1:])
        r = _cil(zb)
        if r and db.execute("SELECT rozcestnik=0 AND infobox_druh=? FROM clanek WHERE id=?",
                            (_DRUH_INFOBOX[sl[0].lower()], r[0])).fetchone()[0]:
            return r[1]
    except Exception as e:
        _log.debug("film_holy_titul %r: %s", q, e)
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


# ── HANS_WIKI_TITUL_Z_VETY_V1 (19. 10.) — přesný titul článku přímo z holé věty ─
# Kotva bere jen slova s velkým písmenem a přepis F1 umí název zkomolit:
# „kdo navrhl karluv most?“ → F1 „Karlov most“ → rozcestník Karlov;
# „kdo napsal valku s mloky?“ → kotva „Válku“ → heslo Válka; „kdo napsal babicku
# a kdy vysla?“ → F1 „Babí léto“. V holé větě název JE, jen bez háčků a v jiném
# pádě. Hledá se nejdelší úsek věty, který je přesně titulem (bez diakritiky,
# první slovo i v 1. pádě); jednoslovný jen s infoboxem nebo velkým písmenem;
# rozcestník jen s druhem podle slovesa (napsal → kniha…, režíroval → film).
# Tituly bez diakritiky: data/wiki/tituly_fold.db (tools/wiki_tituly_fold.py).
_FOLD_DB = os.path.join(os.path.dirname(DB), "tituly_fold.db")
_TZV_STOP = set("""kdo co jak kdy kde proc je jsi jste byl byla bylo byli a i s v ve na o z ze
do od po pro za u k ke se si mi ti to ten ta tu ty jeho jeji jaky jaka jake jakou kolik ktery
ktera ktere kterou napsal napsala navrhl navrhla reziroval rezirovala slozil slozila zalozil
zalozila vysel vysla vyslo vysly postavil postavili ma mel mela mela znas znate vis vite
rekni reknete prosim muzes muzete mohl mohla byste bys dobry den hele tak ted uz jeste
nejaky nejake neco nekdo prvni posledni nebo ale aby kdyz jestli tam tady dobre rano
dekuji diky ahoj vidim jsem mate mam velice poprve naposled moc hodne velmi docela
zajimave zajimavy krasne krasna pekne pekny vas vase vasi tvuj tvoje svuj sve muj moje""".split())
_TZV_DRUH_SLOVO = {"roman": "román", "romanu": "román", "novela": "novela", "novelu": "novela",
                   "povidka": "povídka", "povidku": "povídka", "kniha": "kniha", "knihu": "kniha",
                   "film": "film", "filmu": "film", "serial": "seriál", "opera": "opera",
                   "operu": "opera", "hrad": "hrad", "hora": "hora", "zamek": "zámek",
                   # HANS_WIKI_POHAD_SERIAL_V1 (25. 10., samotest: „pořad Hledání
                   # ztraceného času“ → román) — televizní pořad má upřesnění „(seriál)“
                   "porad": "seriál", "poradu": "seriál", "serialu": "seriál"}
_TZV_DRUH = [(re.compile(r"\b(?:napsal\w*|autor\w*|vysl[aoiy]|vysel|vydal\w*)\b"),
              ("kniha", "román", "novela", "povídka")),
             (re.compile(r"\b(?:rezir\w*|natocil\w*)\b"), ("film",)),
             (re.compile(r"\b(?:slozil\w*|zkomponoval\w*)\b"), ("opera", "píseň", "album"))]


def _fold_tzv(s: str) -> str:
    import unicodedata as _u
    return "".join(c for c in _u.normalize("NFD", (s or "").lower())
                   if _u.category(c) != "Mn")


def _fold_db():
    c = getattr(_tls, "fold_db", None)
    if c is None:
        c = sqlite3.connect(f"file:{_FOLD_DB}?mode=ro", uri=True, check_same_thread=False)
        _tls.fold_db = c
    return c


def _pady(w: str) -> list:
    out = [w]
    for a_, b_ in (("ku", "ka"), ("hu", "ha"), ("ou", "a"), ("u", "a"), ("y", "a"),
                   ("e", "a"), ("i", "e"), ("em", ""), ("a", ""), ("u", ""),
                   ("e", "")):   # „Spalovače“ → „Spalovač“ (sada 40 otázek 19. 10.)
        if w.endswith(a_) and len(w) > 4:
            out.append(w[:-len(a_)] + b_)
    return list(dict.fromkeys(out))


def titul_z_vety(veta: str):
    """Nejdelší úsek věty, který je PŘESNĚ titulem článku → titul, jinak None."""
    try:
        if not os.path.exists(_FOLD_DB):
            return None
        slova = re.findall(r"[^\W\d_]+(?:[.-][^\W\d_]+)*\.?|\d+\.?", veta or "")
        fs = [_fold_tzv(w).rstrip(".") for w in slova]
        vf = " ".join(fs)
        druhy, _sl_poz = [], len(fs)
        for vz, ds in _TZV_DRUH:
            m = vz.search(vf)
            if m:
                druhy += list(ds)
                _sl_poz = min(_sl_poz, vf[:m.start()].count(" "))
        db = _fold_db()
        # začátky vět (první slovo a slovo po . ! ? :) — velké písmeno tam nic neznamená
        _zac = {0} | {j + 1 for j, w in enumerate(slova) if j + 1 < len(slova) and re.search(
            re.escape(w) + r"\s*[.!?:]\s+" + re.escape(slova[j + 1]), veta or "")}

        def _velke(i, n):
            # velké písmeno uprostřed věty = vlastní jméno
            return any(slova[j][:1].isupper() and j not in _zac for j in range(i, i + n))

        # HANS_WIKI_TITUL_CISLO_V1 (25. 10., samotest: „hudbu k filmu Iron Man 2“
        # → „Iron Man (film, 2008)“) — úsek končící číslem / římskou číslicí se
        # dřív vůbec nezkusil (kraj kratší než 4 znaky) a vyhrál kratší „film
        # Iron Man“. Přesný titul s číslem na konci (vlastní jméno velkým
        # písmenem uvnitř) má přednost.
        for n in range(min(6, len(fs)), 1, -1):
            for i in range(0, len(fs) - n + 1):
                usek = fs[i:i + n]
                if not re.fullmatch(r"\d{1,3}|[ivx]{1,4}", usek[-1]) or not _velke(i, n - 1):
                    continue
                if usek[0] in _TZV_STOP or usek[0] in _TZV_DRUH_SLOVO:
                    continue
                r4 = db.execute("SELECT titul FROM t WHERE fold=? AND rozcestnik=0",
                                (" ".join(usek),)).fetchone()
                if r4:
                    return r4[0]
        for n in range(min(6, len(fs)), 0, -1):
            for i in range(0, len(fs) - n + 1):
                usek = fs[i:i + n]
                if usek[0] in _TZV_STOP or usek[-1] in _TZV_STOP:
                    continue
                # druhové slovo na začátku → upřesnění („román Saturnin“ →
                # „Saturnin (román)“); samo druhové slovo titul není
                if usek[0] in _TZV_DRUH_SLOVO:
                    if n >= 2:
                        r3 = db.execute("SELECT titul FROM t WHERE fold=? AND rozcestnik=0",
                                        ("%s (%s)" % (" ".join(usek[1:]),
                                                      _fold_tzv(_TZV_DRUH_SLOVO[usek[0]])),)).fetchone()
                        if r3:
                            return r3[0]
                    continue
                if usek[-1] in _TZV_DRUH_SLOVO or len(usek[0]) < 4 or len(usek[-1]) < 4:
                    continue
                cela = (n == len(fs))
                # druh podle autorského slovesa („kdo napsal babicku“ → „Babička
                # (kniha)“, „kdo reziroval obecnou skolu“ → film): oba kraje
                # smí být v jiném pádě
                if druhy and i > _sl_poz and (n >= 2 or len(usek[0]) >= 5):
                    for prvni in _pady(usek[0]):
                        for posl in (_pady(usek[-1]) if n >= 2 else [None]):
                            klic = " ".join([prvni] + usek[1:-1] + [posl]) if posl else prvni
                            for d in druhy:
                                r2 = db.execute("SELECT titul FROM t WHERE fold=? AND rozcestnik=0",
                                                ("%s (%s)" % (klic, _fold_tzv(d)),)).fetchone()
                                if r2:
                                    return r2[0]
                if n < 2 or (cela and not _velke(i, n)):
                    continue
                # holý titul: jen jeden kraj v jiném pádě; obecné pojmy bez
                # infoboxu jen s velkým písmenem („webove stranky“ ≠ téma)
                klice = [" ".join([p] + usek[1:]) for p in _pady(usek[0])] + \
                        [" ".join(usek[:-1] + [p]) for p in _pady(usek[-1])[1:]]
                for klic in dict.fromkeys(klice):
                    for titul, roz, ib in db.execute(
                            "SELECT titul, rozcestnik, infobox FROM t WHERE fold=?", (klic,)):
                        if roz or titul.startswith("Seznam "):
                            continue
                        if ib or _velke(i, n):
                            return titul
        return None
    except Exception as e:
        _log.debug("titul_z_vety %r: %s", veta, e)
        return None
