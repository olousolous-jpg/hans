"""HANS_SPOTREBA_V1 (6. 11. 2026) — zásoby potravin s datem spotřeby.

Zadání uživatele (backlog SPOTREBA_APP_6_11): Android aplikace skenuje čárový kód
a datum (fotka, jinak ručně) i MIMO domácí síť a offline; s Hansem se synchronizuje,
když je telefon doma. Funguje i bez Hanse. Hans drží společné zásoby (víc telefonů
později), doplní názvy výrobků z Open Food Facts, ráno pošle na Matrix, co brzy
končí, a ukáže přehled na webu.

Synchronizace: každá položka má id z telefonu (UUID) a `zmeneno` (ms); platí novější
změna. Telefon pošle změny od posledního spojení, dostane změny od ostatních.
Data: `data/hans_spotreba.db` (gitignored).
"""
from __future__ import annotations

import datetime as _dt
import logging
import re
import sqlite3
import threading
import time

_log = logging.getLogger(__name__)
_DB = "data/hans_spotreba.db"
_zamek = threading.Lock()


def _cfg(config: dict) -> dict:
    return (config or {}).get("spotreba", {}) or {}


def _db():
    c = sqlite3.connect(_DB, timeout=10.0)
    c.row_factory = sqlite3.Row
    c.execute("""CREATE TABLE IF NOT EXISTS polozky (
        id TEXT PRIMARY KEY, kod TEXT, nazev TEXT, datum TEXT, mnozstvi TEXT,
        stav TEXT DEFAULT 'zasoba', zarizeni TEXT, vytvoreno INTEGER, zmeneno INTEGER,
        prijato INTEGER)""")
    c.execute("CREATE INDEX IF NOT EXISTS ix_pol_prijato ON polozky(prijato)")
    # HANS_SPOTREBA_FOTO_V1 (7. 11.) — číslo kusu (uživatel ho píše fixem na obal: víc
    # kusů téhož výrobku s jiným datem) a příznak fotky (data/spotreba_foto/<id>.jpg)
    for _sl in ("cislo INTEGER DEFAULT 0", "ma_foto INTEGER DEFAULT 0"):
        try:
            c.execute("ALTER TABLE polozky ADD COLUMN " + _sl)
        except sqlite3.OperationalError:
            pass
    c.execute("""CREATE TABLE IF NOT EXISTS produkty (
        kod TEXT PRIMARY KEY, nazev TEXT, zdroj TEXT, ts INTEGER)""")
    c.execute("""CREATE TABLE IF NOT EXISTS zarizeni (
        id TEXT PRIMARY KEY, jmeno TEXT, posledni INTEGER)""")
    return c


_STAVY = {"zasoba", "snedeno", "vyhozeno", "smazano"}
_DATUM_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _ms() -> int:
    return int(time.time() * 1000)


# ── Open Food Facts (čtení; 15 dotazů/min/IP → my nejvýš 6/min) ──────────────
_off_casy: list = []


def nazev_produktu(kod: str, smi_sit: bool = True):
    """Název výrobku podle čárového kódu: vlastní paměť → Open Food Facts. None = nevím."""
    if not kod or not re.fullmatch(r"\d{6,14}", kod):
        return None
    c = _db()
    try:
        r = c.execute("SELECT nazev FROM produkty WHERE kod=?", (kod,)).fetchone()
        if r and r["nazev"]:
            return r["nazev"]
        if r and not r["nazev"] and not smi_sit:
            return None
    finally:
        c.close()
    n_off = off_nazev(kod)               # HANS_SPOTREBA_OFF_TABULKA_V1
    if n_off:
        return n_off
    if not smi_sit:
        return None
    ted = time.time()
    _off_casy[:] = [x for x in _off_casy if ted - x < 60]
    if len(_off_casy) >= 6:
        return None
    _off_casy.append(ted)
    nazev = None
    try:
        import requests
        r = requests.get("https://world.openfoodfacts.org/api/v2/product/%s.json" % kod,
                         params={"fields": "product_name_cs,product_name,brands"},
                         headers={"User-Agent": "Hans-Spotreba/1.0 (osobni aplikace)"}, timeout=10)
        if r.status_code == 200:
            p = (r.json() or {}).get("product") or {}
            n = (p.get("product_name_cs") or p.get("product_name") or "").strip()
            b = (p.get("brands") or "").split(",")[0].strip()
            if n:
                nazev = n if (not b or b.lower() in n.lower()) else "%s (%s)" % (n, b)
        elif r.status_code != 404:
            return None              # výpadek / limit — zkusí se příště
    except Exception as e:
        _log.info("HANS_SPOTREBA_V1: Open Food Facts %s: %s", kod, str(e)[:80])
        return None
    c = _db()
    try:
        c.execute("INSERT OR REPLACE INTO produkty VALUES (?,?,?,?)",
                  (kod, nazev or "", "off", int(time.time())))
        c.commit()
    finally:
        c.close()
    return nazev


# ── HANS_SPOTREBA_OFF_TABULKA_V1 (6. 11.) — offline tabulka výrobků z Open Food Facts ──
# Jednou za `off_dni` (30) v noci se stáhne denní export Open Food Facts (CSV, ~1,5 GB gz;
# čte se proudem, nic se neukládá) a vyberou se výrobky prodávané v Česku a na Slovensku
# (kód → název + značka). Telefon si tabulku stáhne při synchronizaci a hledá offline.
# Data © Open Food Facts, licence ODbL (uvedeno v aplikaci).
_OFF_DB = "data/spotreba_off.db"
_OFF_GZ = "data/spotreba_off.db.gz"
_OFF_URL = "https://static.openfoodfacts.org/data/en.openfoodfacts.org.products.csv.gz"
_OFF_ZEME = ("en:czech-republic", "en:slovakia")
_off_bezi = threading.Event()


def _jmeno_vyrobku(n: str, b: str) -> str:
    n = (n or "").strip()
    b = (b or "").split(",")[0].strip()
    if not n:
        return ""
    return n if (not b or b.lower() in n.lower()) else "%s (%s)" % (n, b)


def off_verze() -> str:
    """Verze tabulky (datum sestavení) nebo ""."""
    import os
    if not os.path.exists(_OFF_DB) or not os.path.exists(_OFF_GZ):
        return ""
    try:
        c = sqlite3.connect(_OFF_DB, timeout=10.0)
        try:
            r = c.execute("SELECT hodnota FROM meta WHERE klic='verze'").fetchone()
            return r[0] if r else ""
        finally:
            c.close()
    except Exception:
        return ""


def off_nazev(kod: str):
    import os
    if not os.path.exists(_OFF_DB):
        return None
    try:
        c = sqlite3.connect(_OFF_DB, timeout=10.0)
        try:
            r = c.execute("SELECT nazev FROM p WHERE kod=?", (kod,)).fetchone()
            return r[0] if r and r[0] else None
        finally:
            c.close()
    except Exception:
        return None


def sestav_off_tabulku() -> dict:
    """Stáhne export proudem a sestaví `_OFF_DB` (+ `.gz` pro telefon). Vrátí počty."""
    import csv
    import gzip
    import io
    import os
    import shutil
    import requests
    csv.field_size_limit(1 << 30)
    tmp = _OFF_DB + ".nova"
    if os.path.exists(tmp):
        os.remove(tmp)
    t0 = time.time()
    n_vse = n_vyb = 0
    with requests.get(_OFF_URL, stream=True, timeout=60,
                      headers={"User-Agent": "Hans-Spotreba/1.0 (osobni aplikace)"}) as r:
        r.raise_for_status()
        r.raw.decode_content = False
        txt = io.TextIOWrapper(gzip.GzipFile(fileobj=r.raw), encoding="utf-8", errors="replace", newline="")
        rd = csv.reader(txt, delimiter="\t", quoting=csv.QUOTE_NONE)
        hl = next(rd)
        ix = {k: hl.index(k) for k in ("code", "product_name", "abbreviated_product_name",
                                        "generic_name", "brands", "countries_tags")}
        mx = max(ix.values())
        c = sqlite3.connect(tmp)
        c.execute("CREATE TABLE p (kod TEXT PRIMARY KEY, nazev TEXT)")
        c.execute("CREATE TABLE meta (klic TEXT PRIMARY KEY, hodnota TEXT)")
        davka = []
        for row in rd:
            n_vse += 1
            if len(row) <= mx:
                continue
            z = row[ix["countries_tags"]]
            if not any(x in z for x in _OFF_ZEME):
                continue
            kod = row[ix["code"]].strip()
            if not re.fullmatch(r"\d{6,14}", kod):
                continue
            nm = _jmeno_vyrobku(row[ix["product_name"]] or row[ix["abbreviated_product_name"]]
                                or row[ix["generic_name"]], row[ix["brands"]])
            if not nm:
                continue
            davka.append((kod, nm[:200]))
            n_vyb += 1
            if len(davka) >= 5000:
                c.executemany("INSERT OR REPLACE INTO p VALUES (?,?)", davka)
                davka = []
        if davka:
            c.executemany("INSERT OR REPLACE INTO p VALUES (?,?)", davka)
        verze = _dt.date.today().isoformat()
        c.execute("INSERT INTO meta VALUES ('verze', ?)", (verze,))
        c.commit()
        c.execute("VACUUM")
        c.close()
    os.replace(tmp, _OFF_DB)
    with open(_OFF_DB, "rb") as f, gzip.open(_OFF_GZ + ".nova", "wb", compresslevel=9) as g:
        shutil.copyfileobj(f, g)
    os.replace(_OFF_GZ + ".nova", _OFF_GZ)
    res = {"radku": n_vse, "vybrano": n_vyb, "verze": verze, "s": int(time.time() - t0),
           "mb": round(os.path.getsize(_OFF_GZ) / 1e6, 1)}
    _log.info("HANS_SPOTREBA_OFF_TABULKA_V1: sestaveno %s", res)
    return res


def obnov_off(config: dict, ted: float | None = None) -> bool:
    """Z rutiny: v noci (2–5 h), když je tabulka starší než `off_dni`, spustí sestavení na pozadí."""
    import os
    k = _cfg(config)
    if not k.get("enabled", True) or not k.get("off_tabulka", True) or _off_bezi.is_set():
        return False
    d = _dt.datetime.fromtimestamp(ted or time.time())
    if not (2 <= d.hour < 5):
        return False
    if os.path.exists(_OFF_GZ) and time.time() - os.path.getmtime(_OFF_GZ) < float(k.get("off_dni", 30)) * 86400:
        return False
    _off_bezi.set()

    def _prace():
        try:
            sestav_off_tabulku()
        except Exception as e:
            _log.warning("HANS_SPOTREBA_OFF_TABULKA_V1: sestavení selhalo: %s", e)
        finally:
            _off_bezi.clear()
    threading.Thread(target=_prace, name="spotreba-off", daemon=True).start()
    return True


def _zapamatuj_nazev(c, kod: str, nazev: str) -> None:
    """Název, který člověk zadal v telefonu, platí pro kód i příště."""
    if kod and nazev:
        c.execute("INSERT INTO produkty VALUES (?,?,?,?) ON CONFLICT(kod) DO UPDATE SET "
                  "nazev=excluded.nazev, zdroj=excluded.zdroj, ts=excluded.ts",
                  (kod, nazev, "telefon", int(time.time())))


# ── synchronizace ───────────────────────────────────────────────────────────
def ladeni(zarizeni: str, texty: list) -> None:
    """Texty z kamery telefonu, ve kterých aplikace datum nenašla → `data/spotreba_ocr.log`
    (repr: vidět i neobvyklé znaky Unicode). Jen pro ladění čtení data."""
    if not texty:
        return
    try:
        with open("data/spotreba_ocr.log", "a", encoding="utf-8") as f:
            for x in texty[:20]:
                f.write("%s %s %r\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), zarizeni[:12], str(x)[:500]))
        _log.info("HANS_SPOTREBA_V1: %d nepřečtených textů z kamery → data/spotreba_ocr.log", len(texty))
    except OSError as e:
        _log.info("HANS_SPOTREBA_V1: ladění: %s", e)


def sync(zarizeni: str, polozky: list, od: int = 0, jmeno: str = "") -> dict:
    """Změny z telefonu → DB (platí novější `zmeneno`); vrátí změny od `od` (podle času
    přijetí na Hansovi) a čas pro příští dotaz. Položka bez názvu dostane název z paměti."""
    ted = _ms()
    prijato, zamitnuto = 0, 0
    with _zamek:
        c = _db()
        try:
            for p in polozky or []:
                pid = str(p.get("id") or "").strip()
                if not re.fullmatch(r"[\w-]{8,64}", pid):
                    zamitnuto += 1
                    continue
                stav = str(p.get("stav") or "zasoba")
                datum = str(p.get("datum") or "")
                if stav not in _STAVY or (datum and not _DATUM_RE.match(datum)):
                    zamitnuto += 1
                    continue
                zm = int(p.get("zmeneno") or 0)
                r = c.execute("SELECT zmeneno FROM polozky WHERE id=?", (pid,)).fetchone()
                if r and int(r["zmeneno"] or 0) >= zm:
                    continue
                kod = re.sub(r"\D", "", str(p.get("kod") or ""))[:14]
                nazev = str(p.get("nazev") or "").strip()[:120]
                try:
                    cislo = max(0, min(9999, int(p.get("cislo") or 0)))
                except (TypeError, ValueError):
                    cislo = 0
                # HANS_SPOTREBA_FOTO_V1 — pojmenované sloupce; příznak fotky zůstává
                c.execute("INSERT INTO polozky (id, kod, nazev, datum, mnozstvi, stav, zarizeni, "
                          "vytvoreno, zmeneno, prijato, cislo) VALUES (?,?,?,?,?,?,?,?,?,?,?) "
                          "ON CONFLICT(id) DO UPDATE SET kod=excluded.kod, nazev=excluded.nazev, "
                          "datum=excluded.datum, mnozstvi=excluded.mnozstvi, stav=excluded.stav, "
                          "zarizeni=excluded.zarizeni, vytvoreno=excluded.vytvoreno, "
                          "zmeneno=excluded.zmeneno, prijato=excluded.prijato, cislo=excluded.cislo",
                          (pid, kod, nazev, datum, str(p.get("mnozstvi") or "")[:40], stav,
                           zarizeni[:64], int(p.get("vytvoreno") or zm), zm, ted, cislo))
                if nazev and p.get("nazev_rucne"):
                    _zapamatuj_nazev(c, kod, nazev)
                prijato += 1
            c.execute("INSERT INTO zarizeni VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET "
                      "posledni=excluded.posledni, jmeno=coalesce(nullif(excluded.jmeno,''), jmeno)",
                      (zarizeni[:64], jmeno[:40], ted))
            c.commit()
            bez = [r["kod"] for r in c.execute(
                "SELECT DISTINCT kod FROM polozky WHERE (nazev IS NULL OR nazev='') AND kod!='' "
                "AND stav='zasoba' LIMIT 10")]
        finally:
            c.close()
    # názvy pro položky bez názvu (mimo zámek — síť)
    for kod in bez:
        n = nazev_produktu(kod)
        if n:
            with _zamek:
                c = _db()
                try:
                    c.execute("UPDATE polozky SET nazev=?, zmeneno=?, prijato=? WHERE kod=? AND "
                              "(nazev IS NULL OR nazev='')", (n, _ms(), _ms(), kod))
                    c.commit()
                finally:
                    c.close()
    c = _db()
    try:
        zpet = [dict(r) for r in c.execute("SELECT id, kod, nazev, datum, mnozstvi, stav, zarizeni, "
                                          "vytvoreno, zmeneno, cislo, ma_foto FROM polozky WHERE prijato > ?",
                                          (int(od or 0),))]
        produkty = {r["kod"]: r["nazev"] for r in c.execute(
            "SELECT kod, nazev FROM produkty WHERE nazev!='' AND ts*1000 > ?", (int(od or 0) - 1,))}
    finally:
        c.close()
    _log.info("HANS_SPOTREBA_V1: sync %s — přijato %d, zamítnuto %d, zpět %d",
              zarizeni[:12], prijato, zamitnuto, len(zpet))
    return {"cas": _ms(), "polozky": zpet, "produkty": produkty,
            "prijato": prijato, "zamitnuto": zamitnuto}


FOTO_DIR = "data/spotreba_foto"


def _foto_cesta(pid: str):
    import os
    if not re.fullmatch(r"[\w-]{8,64}", pid or ""):
        return None
    return os.path.join(FOTO_DIR, pid + ".jpg")


def uloz_foto(pid: str, data: bytes) -> str:
    """HANS_SPOTREBA_FOTO_V1 — fotka položky z telefonu (zmenšený JPEG). Uloží a označí
    položku `ma_foto=1` s novým `prijato` (ostatní telefony se o ní dozví). '' = OK."""
    import os
    cesta = _foto_cesta(pid)
    if not cesta:
        return "špatné id"
    if not data or len(data) > 4 * 1024 * 1024 or not data.startswith(b"\xff\xd8"):
        return "není to JPEG do 4 MB"
    os.makedirs(FOTO_DIR, exist_ok=True)
    tmp = cesta + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, cesta)
    with _zamek:
        c = _db()
        try:
            c.execute("UPDATE polozky SET ma_foto=1, prijato=? WHERE id=?", (_ms(), pid))
            c.commit()
        finally:
            c.close()
    _log.info("HANS_SPOTREBA_FOTO_V1: fotka %s (%d kB)", pid[:8], len(data) // 1024)
    return ""


def foto(pid: str):
    """Cesta k fotce položky, nebo None."""
    import os
    cesta = _foto_cesta(pid)
    return cesta if cesta and os.path.exists(cesta) else None


def nazev_s_cislem(p: dict) -> str:
    """„Kinder bueno č. 2“ (HANS_SPOTREBA_FOTO_V1 — číslo napsané fixem na obalu)."""
    n = p.get("nazev") or "výrobek %s" % (p.get("kod") or "?")
    return n + (" č. %d" % int(p["cislo"]) if p.get("cislo") else "")


def zasoby(jen_zasoba: bool = True) -> list:
    c = _db()
    try:
        q = "SELECT * FROM polozky" + (" WHERE stav='zasoba'" if jen_zasoba else
                                       " WHERE stav!='smazano'")
        return [dict(r) for r in c.execute(q + " ORDER BY coalesce(nullif(datum,''),'9999') ")]
    finally:
        c.close()


def _dny(datum: str, dnes: _dt.date) -> int | None:
    try:
        return (_dt.date.fromisoformat(datum) - dnes).days
    except Exception:
        return None


def koncici(dni: int = 3, dnes: _dt.date = None) -> list:
    """[(polozka, dni_do_konce)] — v zásobě a končí do `dni` (i prošlé)."""
    dnes = dnes or _dt.date.today()
    out = []
    for p in zasoby():
        d = _dny(p.get("datum") or "", dnes)
        if d is not None and d <= dni:
            out.append((p, d))
    return out


def _kdy(d: int) -> str:
    return ("prošlo" if d < 0 else "dnes" if d == 0 else "zítra" if d == 1 else
            "za %d dny" % d if d < 5 else "za %d dní" % d)


def zprava(config: dict) -> str:
    k = _cfg(config)
    kon = koncici(int(k.get("dni", 3)))
    if not kon:
        return ""
    radky = ["Ze zásob brzy končí:"]
    for p, d in kon[:15]:
        radky.append("• %s — %s (%s)" % (nazev_s_cislem(p),
                                          _kdy(d), _dt.date.fromisoformat(p["datum"]).strftime("%-d. %-m.")))
    if len(kon) > 15:
        radky.append("… a dalších %d — celý přehled na mém webu (Domů › Zásoby)." % (len(kon) - 15))
    return "\n".join(radky)


def tick(config: dict, notifier=None, ted: float | None = None) -> str | None:
    """Z rutiny (á 10 min): jednou denně od `hodina` pošle na Matrix, co brzy končí."""
    k = _cfg(config)
    if not k.get("enabled", True) or not notifier:
        return None
    d = _dt.datetime.fromtimestamp(ted or time.time())
    if d.hour < int(k.get("hodina", 9)):
        return None
    c = _db()
    try:
        c.execute("CREATE TABLE IF NOT EXISTS stav (klic TEXT PRIMARY KEY, hodnota TEXT)")
        r = c.execute("SELECT hodnota FROM stav WHERE klic='upozorneno'").fetchone()
        if r and r[0] == d.date().isoformat():
            return None
        text = zprava(config)
        c.execute("INSERT OR REPLACE INTO stav VALUES ('upozorneno', ?)", (d.date().isoformat(),))
        c.commit()
    finally:
        c.close()
    if not text:
        return None
    try:
        notifier(text)
        _log.info("HANS_SPOTREBA_V1: upozornění odesláno (%d řádků)", text.count("\n"))
    except Exception as e:
        _log.warning("HANS_SPOTREBA_V1: upozornění: %s", e)
    return text
