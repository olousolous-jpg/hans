"""HANS_OVERENI_HRY_V1 (6. 11., /tazatel 5. 11.) — čísla o Hansových hrách po odpovědi.

Blok o sobě nese skutečné počty (šachy, žetony), ale model je pod tlakem tazatele
přepíše („Jen jednu?“ → 2 → 4 partie, „27 partií“, žetonů 7 / 12 / 1). Stejně jako
oprava z Wikidat: kód porovná číslo v odpovědi se záznamem a při rozporu připíše
„Ještě oprava: podle mých záznamů …“. Odpověď sama zůstává (živá mluva + ověření).
Jen když se věta týká Hansovy hry (otázka na hry, nebo Koláč v odpovědi)."""
import logging
import re
import sqlite3

_log = logging.getLogger(__name__)

_CISLA = {"jedna": 1, "jednu": 1, "jeden": 1, "jedné": 1, "jediná": 1, "jedinou": 1,
          "dvě": 2, "dva": 2, "dvou": 2, "tři": 3, "třech": 3, "čtyři": 4, "čtyřech": 4,
          "pět": 5, "šest": 6, "sedm": 7, "osm": 8, "devět": 9, "deset": 10,
          "jedenáct": 11, "dvanáct": 12, "třináct": 13, "patnáct": 15, "dvacet": 20}
_NUM = r"(\d{1,4}|" + "|".join(sorted(_CISLA, key=len, reverse=True)) + r")"
# číslo nejvýš 3 slova před „partie/partií/partii“ (ne „partie 2“ = pořadí)
_PARTIE = re.compile(r"\b" + _NUM + r"(?:\s+\w+){0,3}?\s+parti(?:e|í|i|emi)\b", re.I)
_ZETONY = re.compile(r"\b" + _NUM + r"(?:\s+\w+){0,2}?\s+žeton\w*", re.I)
_ZETONY2 = re.compile(r"\bžeton\w*\s+(?:mám\s+)?(?:\w+\s+){0,2}?" + _NUM + r"\b", re.I)
_PORADI = re.compile(r"\b(?:první|druh|třetí|čtvrt|posledn|dnešn|včerejš)\w*\s+parti", re.I)


def _n(s):
    s = (s or "").lower()
    return int(s) if s.isdigit() else _CISLA.get(s)


def _stav(db_path):
    c = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True, timeout=5)
    try:
        v, r, p, n = c.execute(
            "SELECT coalesce(sum(vysledek='výhra'),0), coalesce(sum(vysledek='remíza'),0), "
            "coalesce(sum(vysledek='prohra'),0), count(*) FROM sachy_partie WHERE stav='konec'"
        ).fetchone()
        b = c.execute("SELECT count(*) FROM sachy_partie WHERE stav='bezi'").fetchone()[0]
        try:
            z = c.execute("SELECT coalesce(sum(zetony),0) FROM motivace_udalosti").fetchone()[0]
            u = c.execute("SELECT count(*) FROM motivace_vysady").fetchone()[0]
            zet = (z or 0) - (u or 0)
        except sqlite3.Error:
            zet = None
        return {"v": v, "r": r, "p": p, "n": n, "b": b, "zet": zet}
    finally:
        c.close()


def _tvar_partii(k):
    return "partii" if k == 1 else "partie" if 2 <= k <= 4 else "partií"


def oprava(config, otazka, prepis, odpoved):
    """Text opravy, nebo None."""
    try:
        from scripts.hans_intent import pta_se_na_hry_hanse
        o = odpoved or ""
        if not (pta_se_na_hry_hanse(otazka or "") or pta_se_na_hry_hanse(prepis or "")
                or re.search(r"\bKol[áa][čc]\w*", o)):
            return None
        st = _stav((config or {}).get("diary_db") or "data/hans_diary.db")
        out = []
        if st["n"] or st["b"]:
            spatne = []
            for m in _PARTIE.finditer(o):
                k = _n(m.group(1))
                # „5 partií vyhrál“ apod. je dílčí počet — kontroluje se jen celek:
                # číslo, které není dohraných, celkem ani žádný z výsledků
                if k is None or k in {st["n"], st["n"] + st["b"], st["v"], st["r"], st["p"]}:
                    continue
                if _PORADI.search(m.group(0)):
                    continue
                spatne.append(k)
            if spatne:
                t = ("Ještě oprava: podle mých záznamů jsem s Koláčem dohrál %d %s "
                     "(výhry %d, remízy %d, prohry %d)" % (st["n"], _tvar_partii(st["n"]),
                                                          st["v"], st["r"], st["p"]))
                if st["b"]:
                    t += " a jednu mám rozehranou"
                out.append(t + ".")
        if st["zet"] is not None:
            for rx in (_ZETONY, _ZETONY2):
                ks = [_n(m.group(1)) for m in rx.finditer(o)]
                if any(k is not None and k != st["zet"] for k in ks):
                    out.append("Ještě oprava: podle mých záznamů mám žetonů %d." % st["zet"])
                    break
        if out:
            _log.info("HANS_OVERENI_HRY_V1: %s", " ".join(out))
        return " ".join(out) or None
    except Exception as e:
        _log.debug("HANS_OVERENI_HRY_V1: %s", e)
        return None
