"""HANS_AKCE_V1 (3. 11. 2026) — kulturní akce v okolí z webů obcí.

Celostátní otevřený kalendář akcí neexistuje (Kudy z nudy jen „při splnění podmínek“,
Mapy.com placeně, weby akcí bez API) → akce bere Hans z webů obcí:
  1. poloha → nejbližší české obce (offline souřadnice + infobox „česká obec“),
  2. oficiální web obce z Wikidat (P856) → odkaz na kalendář akcí na titulní stránce,
  3. text kalendáře → model vypíše akce; KÓD každou akci ověří proti textu stránky
     (název i datum tam musí stát — model si nesmí nic vymyslet),
  4. odpověď na Matrix.
📏 průzkum 3. 11.: kalendář má 5 z 6 obcí (Litomyšl, Sobotka, Jičín, Pardubice,
Mnichovo Hradiště; Kutná Hora jen skriptem v prohlížeči), strojový formát (schema.org,
iCal) nikdo, RSS jen Turnov. Stahuje se jen pár stránek na dotaz, s ohledem na robots.txt.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import math
import os
import re
import time
from urllib.parse import urljoin, urlparse

_log = logging.getLogger(__name__)
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_WEBY = os.path.join(_ROOT, "data", "akce_weby.json")      # obec → web, kalendář (veřejné údaje)
_STATUS_VAHA = {"město": 3.0, "statutární město": 4.0, "městys": 1.6, "obec": 1.0}
_KAL_RE = re.compile(r"kalend|akce|ud[aá]lost|event|program", re.I)


def _ua():
    from scripts.hans_facts import USER_AGENT
    return {"User-Agent": USER_AGENT}


# ── 1. obce v okolí ─────────────────────────────────────────────────────────
def obce_v_okoli(lat: float, lon: float, r_km: float = 12.0, kolik: int = 3) -> list:
    """[(titul, km, status)] — nejbližší české obce, města mají přednost."""
    from scripts import hans_wiki_offline as wo
    from scripts.hans_mista import _sdb, _vzdal
    g = _sdb()
    if g is None:
        return []
    try:
        dl = r_km / 111.0
        do = r_km / (111.0 * max(0.2, math.cos(math.radians(lat))))
        body = g.execute("SELECT id, lat, lon FROM s WHERE typ='city' AND lat BETWEEN ? AND ? "
                         "AND lon BETWEEN ? AND ?", (lat - dl, lat + dl, lon - do, lon + do)).fetchall()
    finally:
        g.close()
    out = []
    for i, a, b in body:
        km = _vzdal(lat, lon, a, b)
        if km > r_km:
            continue
        r = wo._db().execute("SELECT titul, infobox FROM clanek WHERE id=? AND infobox_druh="
                             "'česká obec'", (i,)).fetchone()
        if not r:
            continue
        m = re.search(r"(?m)^status:\s*([^\n]+)", r[1] or "")
        st = (m.group(1).strip().lower() if m else "obec")
        w = _STATUS_VAHA.get(st, 1.0)
        out.append((r[0], km, st, w / (1 + km / 4)))
    out.sort(key=lambda x: -x[3])
    return [(t, km, st) for t, km, st, _ in out[:kolik]]


# ── 2. web obce a kalendář ─────────────────────────────────────────────────
def _weby() -> dict:
    try:
        return json.load(open(_WEBY, encoding="utf-8"))
    except Exception:
        return {}


def _uloz_weby(d: dict) -> None:
    try:
        json.dump(d, open(_WEBY + ".tmp", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        os.replace(_WEBY + ".tmp", _WEBY)
    except Exception:
        pass


def web_obce(titul: str):
    """Oficiální web obce z Wikidat (P856), mezipaměť `data/akce_weby.json`."""
    d = _weby()
    if titul in d and d[titul].get("web") is not None:
        return d[titul]["web"] or None
    import requests
    web = ""
    try:
        r = requests.get("https://www.wikidata.org/w/api.php", headers=_ua(), timeout=10,
                         params={"action": "wbgetentities", "sites": "cswiki", "titles": titul,
                                 "props": "claims", "format": "json"})
        for e in (r.json().get("entities") or {}).values():
            for c in ((e.get("claims") or {}).get("P856") or []):
                v = ((c.get("mainsnak") or {}).get("datavalue") or {}).get("value")
                if v:
                    web = v
                    break
    except Exception as e:
        _log.info("HANS_AKCE_V1: web %r z Wikidat: %s", titul, str(e)[:80])
        return None
    d.setdefault(titul, {})["web"] = web
    _uloz_weby(d)
    return web or None


def _smi(url: str, na_pozadani: bool = False) -> bool:
    """Smí Hans stáhnout `url`?
    HANS_AKCE_ROBOTS_V1 (rozhodnutí uživatele 3. 11.): `robots.txt` je pro prohledávače —
    jednu stránku na výslovný dotaz člověka (jako prohlížeč, se jménem HansBot a denní
    pamětí výsledku) stáhnout smí i přes zákaz (obce mají šablonu Vismo „všem robotům
    kromě vyhledávačů zakázáno“). Stahování NA POZADÍ (bez dotazu) robots.txt dodržuje.
    Výslovný zákaz v podmínkách webu platí vždy (jako Moltbook)."""
    if not na_pozadani:
        return True
    from urllib import robotparser
    try:
        p = urlparse(url)
        rp = robotparser.RobotFileParser()
        import requests
        r = requests.get("%s://%s/robots.txt" % (p.scheme, p.netloc), headers=_ua(), timeout=8)
        if r.status_code != 200:
            return True
        rp.parse(r.text.splitlines())
        return rp.can_fetch(_ua()["User-Agent"], url)
    except Exception:
        return True


def _html(url: str) -> str:
    import requests
    r = requests.get(url, headers=_ua(), timeout=20)
    r.raise_for_status()
    m = re.search(rb'charset=["\']?([\w-]+)', r.content[:3000], re.I)
    for kod in ([m.group(1).decode()] if m else []) + ["utf-8", "cp1250"]:
        try:
            return r.content.decode(kod)
        except (LookupError, UnicodeDecodeError):
            continue
    return r.content.decode("utf-8", "replace")


def kalendar_obce(titul: str):
    """URL stránky s kalendářem akcí na webu obce (mezipaměť), nebo None."""
    d = _weby()
    if d.get(titul, {}).get("kalendar") is not None:
        return d[titul]["kalendar"] or None
    web = web_obce(titul)
    if not web:
        return None
    kal = ""
    try:
        if _smi(web):
            s = _html(web)
            kand = []
            for m in re.finditer(r'<a\b[^>]*href="([^"#]+)"[^>]*>(.*?)</a>', s, re.I | re.S):
                h, txt = m.group(1), re.sub(r"<[^>]+>|\s+", " ", m.group(2)).strip().lower()
                cesta = urlparse(urljoin(web, h))
                if cesta.netloc and urlparse(web).netloc.split(".")[-2:] != cesta.netloc.split(".")[-2:]:
                    continue                      # cizí web (vstupenky, kraj)
                if not (_KAL_RE.search(cesta.path) or re.search(r"kalendář|akce|události|program", txt)):
                    continue
                if re.search(r"detail|\d{4}-\d\d-\d\d|_\d{3,}|\.(css|js|pdf|jpg|png)$", cesta.path, re.I):
                    continue                      # jedna akce / soubor, ne kalendář
                skore = (len(cesta.path) + (40 if cesta.query else 0)
                         - (30 if re.search(r"kalend", cesta.path + txt) else 0))
                kand.append((skore, urljoin(web, h).split("#")[0]))
            if kand:
                kal = sorted(kand)[0][1]
    except Exception as e:
        _log.info("HANS_AKCE_V1: kalendář %r (%s): %s", titul, web, str(e)[:80])
        return None
    d.setdefault(titul, {})["kalendar"] = kal
    _uloz_weby(d)
    return kal or None


_text_ram: dict = {}             # url → (ts, text) — každý web nejvýš 1× denně

# HANS_AKCE_PROHLIZEC_V1 (3. 11.) — kalendář, který akce dotahuje skriptem (Kutná Hora):
# Chromium bez okna. 📏 Pi: ~15 s, špička ~1,3 GB RAM → jen záloha, NEJVÝŠ JEDEN
# najednou, 1× na dotaz, 6× za hodinu (uživatel: „opatrně, ne moc dotazů najednou“).
import threading as _th
_prohlizec_zamek = _th.Lock()
_prohlizec_casy: list = []


def _html_prohlizec(url: str, timeout: int = 45) -> str:
    import shutil
    import subprocess
    import tempfile
    b = shutil.which("chromium") or shutil.which("chromium-browser")
    if not b:
        return ""
    ted = time.time()
    _prohlizec_casy[:] = [x for x in _prohlizec_casy if ted - x < 3600]
    if len(_prohlizec_casy) >= 6:
        _log.info("HANS_AKCE_PROHLIZEC_V1: limit 6/h — %s vynechán", url)
        return ""
    if not _prohlizec_zamek.acquire(timeout=60):
        return ""
    prof = tempfile.mkdtemp(prefix="hans_chromium_")
    try:
        _prohlizec_casy.append(time.time())
        r = subprocess.run([b, "--headless=new", "--disable-gpu", "--no-sandbox",
                            "--user-data-dir=" + prof, "--virtual-time-budget=8000",
                            "--user-agent=" + _ua()["User-Agent"], "--dump-dom", url],
                           capture_output=True, timeout=timeout)
        _log.info("HANS_AKCE_PROHLIZEC_V1: %s → %d zn za %.0f s", url, len(r.stdout),
                  time.time() - _prohlizec_casy[-1])
        return r.stdout.decode("utf-8", "replace")
    except Exception as e:
        _log.info("HANS_AKCE_PROHLIZEC_V1: %s: %s", url, str(e)[:80])
        return ""
    finally:
        shutil.rmtree(prof, ignore_errors=True)
        _prohlizec_zamek.release()


def text_stranky(url: str, strop: int = 14000, prohlizec: bool = False) -> str:
    c = _text_ram.get((url, prohlizec))
    if c and time.time() - c[0] < 24 * 3600:
        return c[1]
    s = _html_prohlizec(url) if prohlizec else _html(url)
    s = re.sub(r"(?is)<(script|style|noscript|nav|footer|header)\b.*?</\1>", " ", s)
    s = re.sub(r"(?i)<br\s*/?>|</(p|div|li|tr|h\d)>", "\n", s)
    s = re.sub(r"<[^>]+>", " ", s)
    import html as _h
    s = _h.unescape(s)
    s = re.sub(r"[ \t\xa0]+", " ", s)
    s = re.sub(r"\n\s*\n+", "\n", s)
    _text_ram[(url, prohlizec)] = (time.time(), s.strip()[:strop])
    return _text_ram[(url, prohlizec)][1]


# ── 3. akce z textu (model) + ověření kódem ─────────────────────────────────
class ModelDole(RuntimeError):
    pass


_DATUM = re.compile(r"\b(\d{1,2})\.\s?(\d{1,2})\.(?:\s?(20\d\d))?")
_ZADANI = (
    "Níže je text stránky s kalendářem akcí obce %s. Dnes je %s. Vypiš akce, které se konají "
    "od dneška do %s. Každou na samostatný řádek ve tvaru:\n"
    "DATUM | NÁZEV | MÍSTO\n"
    "DATUM piš přesně tak, jak stojí v textu (např. 5. 11. nebo 5.11.2026), NÁZEV doslova "
    "z textu, MÍSTO jen když ho text uvádí, jinak nech prázdné. Nic nevymýšlej, nic "
    "nepřidávej, žádný úvod. Když tam žádná taková akce není, napiš jen: ŽÁDNÉ\n\nText:\n%s")


def _f(s: str) -> str:
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFD", (s or "").lower())
                   if unicodedata.category(c) != "Mn")


def over_akce(radky: list, text: str, od: _dt.date, do: _dt.date) -> list:
    """Akce od modelu → jen ty, jejichž datum je v rozsahu a název (aspoň 2/3 slov)
    i datum stojí v textu stránky. [(datum, název, místo)]"""
    ft = _f(text)
    dat_v_textu = {(int(m.group(1)), int(m.group(2))) for m in _DATUM.finditer(text)}
    out = []
    for r in radky:
        casti = [x.strip() for x in r.split("|")]
        if len(casti) < 2:
            continue
        m = _DATUM.search(casti[0])
        if not m:
            continue
        d, mes = int(m.group(1)), int(m.group(2))
        if (d, mes) not in dat_v_textu:
            continue
        try:
            rok = int(m.group(3)) if m.group(3) else od.year
            datum = _dt.date(rok, mes, d)
            if datum < od and not m.group(3):
                datum = _dt.date(rok + 1, mes, d)
        except ValueError:
            continue
        if not (od <= datum <= do):
            continue
        nazev = casti[1].strip(" .\"„“")
        sl = [w for w in re.findall(r"\w{3,}", _f(nazev))]
        if not sl or sum(w in ft for w in sl) < max(1, math.ceil(len(sl) * 2 / 3)):
            continue
        # název a datum musí stát BLÍZKO sebe (≤ 300 zn) — jinak model spojil cizí datum
        pos_d = [x.start() for x in _DATUM.finditer(text)
                 if (int(x.group(1)), int(x.group(2))) == (d, mes)]
        pos_n = [x.start() for x in re.finditer(re.escape(sl[0]), ft)]
        if not any(abs(a - b) <= 300 for a in pos_d for b in pos_n):
            continue
        misto = casti[2] if len(casti) > 2 else ""
        if misto and not all(w in ft for w in re.findall(r"\w{4,}", _f(misto))):
            misto = ""
        out.append((datum, nazev, misto))
    return sorted(set(out))


def akce_obce(config: dict, titul: str, dni: int = 7, smi_prohlizec: bool = False) -> list:
    """[(datum, název, místo)] z kalendáře obce. Prázdné = nic / nejde."""
    kal = kalendar_obce(titul)
    if not kal or not _smi(kal):
        return []
    text = text_stranky(kal)
    if len(_DATUM.findall(text)) < 2 and smi_prohlizec:
        _log.info("HANS_AKCE_V1: %r — kalendář bez dat, zkusím prohlížeč", titul)
        text = text_stranky(kal, prohlizec=True)
    if len(_DATUM.findall(text)) < 2:
        _log.info("HANS_AKCE_V1: %r — kalendář bez dat v textu (skript v prohlížeči?)", titul)
        return []
    od = _dt.date.today()
    do = od + _dt.timedelta(days=dni)
    from scripts.ollama_client import ollama_chat
    model = str((config.get("hans_dialog", {}) or {}).get("ollama_model", "hans-czech:latest"))
    out = ollama_chat(model, [{"role": "user", "content": _ZADANI % (
        titul, od.strftime("%-d. %-m. %Y"), do.strftime("%-d. %-m. %Y"), text)}],
        config=config, timeout=90, options={"temperature": 0, "num_ctx": 8192, "num_predict": 700})
    if not (out or "").strip():
        raise ModelDole("model nedostupný")
    radky = [r for r in (out or "").splitlines() if "|" in r]
    ok = over_akce(radky, text, od, do)
    _log.info("HANS_AKCE_V1: %r — model %d řádků, ověřeno %d", titul, len(radky), len(ok))
    return ok


# ── 4. odpověď ─────────────────────────────────────────────────────────────
def zprava(config: dict, lat: float, lon: float, dni: int = 7) -> str:
    try:
        obce = obce_v_okoli(lat, lon)
    except Exception as e:
        _log.warning("HANS_AKCE_V1: obce: %s", e)
        obce = []
    if not obce:
        return "V okolí jsem nenašel žádnou obec, jejíž akce bych uměl dohledat."
    radky, bez = [], []
    prohlizec = [True]          # HANS_AKCE_PROHLIZEC_V1 — nejvýš jednou na dotaz
    for titul, km, st in obce:
        try:
            _p = prohlizec[0]
            ak = akce_obce(config, titul, dni, smi_prohlizec=_p)
            if _p and any(k[1] for k in _text_ram if k[0] == kalendar_obce(titul)):
                prohlizec[0] = False
            time.sleep(0.5)
        except ModelDole:
            return ("Akce čtu z webů obcí a k jejich vypsání potřebuji svůj mozek na počítači, "
                    "který teď neběží. Zkuste to prosím později.")
        except Exception as e:
            _log.info("HANS_AKCE_V1: %r: %s", titul, str(e)[:100])
            ak = None
        jm = re.sub(r"\s*\(.*?\)$", "", titul)
        if not ak:
            bez.append(jm)
            continue
        radky.append("%s (%s km):" % (jm, ("%.1f" % km).replace(".", ",")))
        for d, n, m in ak[:6]:
            radky.append("  • %s — %s%s" % (d.strftime("%-d. %-m."), n, (", " + m) if m else ""))
    if not radky:
        return ("Na webech obcí %s jsem na nejbližších %d dní žádné akce nenašel (nebo je "
                "jejich kalendář nejde přečíst)." % (", ".join(bez), dni))
    uvod = "Akce v okolí na nejbližších %d dní (z webů obcí):" % dni
    if bez:
        radky.append("(%s: nic nenalezeno)" % ", ".join(bez))
    return uvod + "\n" + "\n".join(radky)


# ── dotaz z Matrixu ─────────────────────────────────────────────────────────
_DOTAZ_AKCE = re.compile(
    r"\b(?:akc[ei]\w*|kultur\w*|koncert\w*|program\w*|co\s+se\s+(?:d[eě]je|kon[aá]))\b"
    r"[^?!.]{0,30}?(?:\b(?:v\s+oko[a-zíi]{1,3}|okolo|kolem|pobl[ií][zž]|nabl[ií]zku|tady)\b"
    r"|\b(?:u|v|ve|na)\s+(?=(?-i:[A-ZÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ])))\s*"
    r"(?P<misto>(?-i:[A-ZÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ])[^?!.,]{1,40})?", re.I)


def je_dotaz(text: str):
    """None = není dotaz na akce; jinak název místa nebo ''."""
    m = _DOTAZ_AKCE.search(text or "")
    if not m or re.search(r"\bzaj[ií]mav", text or "", re.I) and not re.search(
            r"akc|kultur|koncert|d[eě]je|kon[aá]", text or "", re.I):
        return None
    return (m.group("misto") or "").strip()


def odpoved_na_dotaz(config: dict, text: str, osoba: str):
    """Text odpovědi, nebo None (není dotaz / místo bez souřadnic → běžný hovor)."""
    jm = je_dotaz(text)
    if jm is None:
        return None
    from scripts import hans_mista as hm
    if jm:
        s = hm.souradnice(jm)
        if not s:
            return None
        return zprava(config, s[0], s[1])
    p = hm.posledni.get(osoba)
    if p and time.time() - p[2] < hm._CERSTVA_S:
        return zprava(config, p[0], p[1])
    return ("Polohu si sám vyžádat neumím — pošlete mi ji prosím (+ → Poloha), nebo napište "
            "název místa, třeba „jaké jsou akce v Litomyšli?“")
