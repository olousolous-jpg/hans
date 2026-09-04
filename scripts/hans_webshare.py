# -*- coding: utf-8 -*-
"""HANS_WEBSHARE_V1 (4.9.) — hledání na webshare.cz, odhad kvality, stažení na PC.

PROČ VLASTNÍ MODUL: Hans uměl sáhnout jen do Kodi knihovny (`kodi_client`),
takže film mimo ni pro něj neexistoval — otevřený bod „film mimo knihovnu“
z backlogu. Tohle je datová cesta k němu.

CO JE ODKUD:
- API tvar (salt → login → search → file_link) sedí s oficiální dokumentací
  https://webshare.cz/apidoc/ a s archivovaným MIT projektem `bettercinema`
  (Seraphim-Solutions). Kód je psaný nově — bettercinema pouští do VLC
  a credentials drží v SQLite, což se sem nehodí.

⚠️ TŘI VĚCI, KTERÉ SE PŘI STAVBĚ UKÁZALY A PLATÍ DÁL:
1. `crypt` byl z Pythonu 3.13 ODSTRANĚN (Pi jede 3.13.5), takže MD5-crypt
   dělá `passlib` (1.7.4, user-level v ~/.local). Ověřeno, že na 3.13 běží
   bez modulu `crypt`. Bez passlibu modul jen ohlásí chybu, nespadne.
2. STAHUJE SE NA PC, NE NA PI. Filmová knihovna je na NASu namountovaném
   na PC (`/mnt/D`, `/mnt/F`); Pi má jen `/mnt/nas-hans`, který namountovaný
   NENÍ. Vícegigový soubor na Pi nemá kam a odtud by se stejně musel kopírovat.
   Jede se přes SDÍLENÝ `pc_remote.run` — třetí vlastní `_ssh_base` se sem
   ZÁMĚRNĚ nepíše (`hans_translate` má svůj, to je duplikát dost).
3. KVALITA SE POZNÁ JEN Z NÁZVU SOUBORU. Webshare nic strukturovaného
   nevrací, takže `kvalita()` je ODHAD z názvu a tak se to i formuluje.
   Nevydávat za měření.

⚠️ Heslo se nikam neloguje ani nezapisuje — na disk jde jen WST token.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil          # HANS_WEBSHARE_MISTO_GUARD_FIX_V1 — bez něj `_volne_misto`
                       # tiše padalo do except a vracelo 0 → pojistka na místo
                       # byla mrtvá. Chyceno testem, ne čtením.
import time
import xml.etree.ElementTree as ET
from typing import Optional

log = logging.getLogger(__name__)

_API = "https://webshare.cz/api/%s/"
_TOKEN_FILE = "data/.webshare_token"
_UA = "Hans/1.0"


# ── nízkoúrovňové volání API ────────────────────────────────────────────────

def _post(endpoint: str, data: dict, timeout: int = 20) -> dict:
    """POST na Webshare API. Vrátí rozparsovaný XML kořen jako dict.

    Webshare vrací XML VŽDY, i u chyby — rozhoduje element `status`
    (`OK` / `FATAL`), doprovodný `code` a `message`. HTTP kód je 200 i pro
    odmítnutí, takže spoléhat na `raise_for_status` NESTAČÍ.
    """
    import requests
    r = requests.post(_API % endpoint, data=data,
                      headers={"Accept": "text/xml", "User-Agent": _UA},
                      timeout=timeout)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    out = {"_root": root}
    for ch in root:
        if len(ch) == 0:
            out[ch.tag] = (ch.text or "").strip()
    return out


class WebshareError(RuntimeError):
    """Webshare odmítl požadavek (špatné heslo, vypršelý token, není VIP…)."""


def _zkontroluj(d: dict, co: str) -> dict:
    if (d.get("status") or "").upper() != "OK":
        raise WebshareError("%s: %s (%s)" % (
            co, d.get("message") or "bez zprávy", d.get("code") or "?"))
    return d


# ── přihlášení ──────────────────────────────────────────────────────────────

def _cfg(config: dict) -> dict:
    return (config or {}).get("webshare", {}) or {}


def nastaveno(config: dict) -> bool:
    """Jsou v configu vyplněné přihlašovací údaje?"""
    c = _cfg(config)
    return bool(c.get("username") and c.get("password"))


def _hash_hesla(heslo: str, salt: str) -> str:
    """SHA1(MD5_CRYPT(heslo, salt)) — tvar, který Webshare u loginu čeká."""
    from passlib.hash import md5_crypt
    mc = md5_crypt.using(salt=salt).hash(heslo)
    return hashlib.sha1(mc.encode("utf-8")).hexdigest()


def _nacti_token(uzivatel: str, ttl_s: int) -> Optional[str]:
    try:
        with open(_TOKEN_FILE, encoding="utf-8") as f:
            d = json.load(f)
        if d.get("username") != uzivatel:
            return None                       # jiný účet → token neplatí
        if time.time() - float(d.get("ts", 0)) > ttl_s:
            return None
        return d.get("token") or None
    except Exception:
        return None


def _uloz_token(uzivatel: str, token: str) -> None:
    try:
        os.makedirs(os.path.dirname(_TOKEN_FILE) or ".", exist_ok=True)
        tmp = _TOKEN_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"username": uzivatel, "token": token,
                       "ts": time.time()}, f)
        os.replace(tmp, _TOKEN_FILE)
        os.chmod(_TOKEN_FILE, 0o600)          # token je přístup k účtu
    except Exception as e:
        log.debug("webshare: token se nepodařilo uložit: %s", e)


def token(config: dict, force: bool = False) -> str:
    """Vrátí platný WST token (z cache, jinak se přihlásí).

    ⚠️ `force=True` obchází cache — volá se, až když Webshare token odmítne.
    Přihlašovat se pokaždé znovu je zbytečné a Webshare to nemá rád.
    """
    c = _cfg(config)
    uziv, heslo = str(c.get("username") or ""), str(c.get("password") or "")
    if not uziv or not heslo:
        raise WebshareError("přihlašovací údaje k Webshare nejsou v configu")
    ttl = int(c.get("token_ttl_s", 7 * 24 * 3600))
    if not force:
        t = _nacti_token(uziv, ttl)
        if t:
            return t
    s = _zkontroluj(_post("salt", {"username_or_email": uziv}), "salt")
    ph = _hash_hesla(heslo, s.get("salt") or "")
    digest = hashlib.md5(("%s:Webshare:%s" % (uziv, ph)).encode("utf-8")).hexdigest()
    d = _zkontroluj(_post("login", {
        "username_or_email": uziv, "password": ph, "digest": digest,
        "keep_logged_in": 1}), "login")
    t = d.get("token") or ""
    if not t:
        raise WebshareError("login prošel, ale token nepřišel")
    _uloz_token(uziv, t)
    log.info("webshare: přihlášen jako %s", uziv)
    return t


def _s_tokenem(config: dict, endpoint: str, data: dict, co: str) -> dict:
    """Zavolej endpoint s WST; při vypršelém tokenu se JEDNOU přihlas znovu.

    Doložený tvar selhání: token po čase zneplatní a API vrátí FATAL —
    bez tohohle by Hans hlásil „chyba“ místo aby se tiše přihlásil.
    """
    for pokus in (0, 1):
        d = _post(endpoint, dict(data, wst=token(config, force=bool(pokus))))
        if (d.get("status") or "").upper() == "OK":
            return d
        if pokus == 0 and _vyprsel(d):
            log.info("webshare: token vypršel → přihlašuji se znovu")
            continue
        return _zkontroluj(d, co)
    return {}


def _vyprsel(d: dict) -> bool:
    txt = "%s %s" % (d.get("code") or "", d.get("message") or "")
    return bool(re.search(r"token|login|p[řr]ihl[áa][šs]", txt, re.IGNORECASE))


# ── odhad kvality z NÁZVU souboru ───────────────────────────────────────────
# ⚠️ Webshare o kvalitě nic strukturovaného nevrací. Tohle je ODHAD z názvu:
#    co uploader napsal, to se dozvíme — nic víc. Formulovat opatrně.

_ROZLISENI = (
    ("2160p", r"2160p|\b4k\b|uhd|ultrahd"),
    ("1080p", r"1080[pi]|fullhd|\bfhd\b"),
    ("720p",  r"720[pi]|\bhd\b(?!r)"),
    ("576p",  r"576[pi]|\bpal\b"),
    ("480p",  r"480[pi]|\bsd\b"),
)
_ZDROJ = (
    ("BluRay", r"blu-?ray|\bbdrip\b|\bbrrip\b|\bbdremux\b|\bremux\b"),
    ("WEB",    r"web-?dl|\bwebrip\b|\bweb\b|amzn|netflix|\bnf\b|disney"),
    ("HDTV",   r"\bhdtv\b|\bdvbt?\b|\btvrip\b"),
    ("DVD",    r"\bdvdrip\b|\bdvd\b|\bdvd5\b|\bdvd9\b"),
    ("CAM",    r"\bcam\b|\bts\b|telesync|\bhdts\b|\bkinorip\b|\bscreener\b|\bts-?rip\b"),
)
_KODEK = (
    ("AV1",  r"\bav1\b"),
    ("H265", r"x\.?265|h\.?265|hevc"),
    ("H264", r"x\.?264|h\.?264|\bavc\b"),
    ("XviD", r"\bxvid\b|\bdivx\b"),
)
_HDR = r"\bhdr10?\+?\b|dolby\s*vision|\bdv\b"
# HANS_WEBSHARE_DABING_JAZYK_V1 (4.9.) — holé „dabing“ NEURČUJE JAZYK.
# Odhaleno vlastním testem parseru: „Interstellar 1080p BluRay HEVC SK dabing“
# se označilo jako CZ, protože `_CESKY` bralo samotné slovo „dabing“. Dabing
# je způsob zpracování, ne jazyk — o jazyku rozhoduje jen značka cz/sk.
# Soubor s „dabing“ bez značky se tedy NEOZNAČÍ za český: nevíme to.
_CESKY = r"\bcz\b|\bczech\b|[čc]esk|\bcz-?dab\w*"
_SLOVENSKY = r"\bsk\b|slovensk|\bsk-?dab\w*"
_DABING = r"\bdabing\b|\bdab\b|\bdabovan\w*"
_TITULKY = r"titulk|\bsub(s|titles)?\b|\btit\b"

# Pořadí = kvalita. Slouží k řazení, ne k soudu o obsahu.
_PORADI_ROZL = {"2160p": 5, "1080p": 4, "720p": 3, "576p": 2, "480p": 1}
_PORADI_ZDROJ = {"BluRay": 4, "WEB": 3, "HDTV": 2, "DVD": 1, "CAM": -3}


def kvalita(nazev: str) -> dict:
    """Odhadni kvalitu z názvu souboru. Nic z toho není zaručené."""
    t = " %s " % (nazev or "").lower().replace(".", " ").replace("_", " ")
    def _prvni(tabulka):
        for jmeno, vzor in tabulka:
            if re.search(vzor, t, re.IGNORECASE):
                return jmeno
        return None
    return {
        "rozliseni": _prvni(_ROZLISENI),
        "zdroj":     _prvni(_ZDROJ),
        "kodek":     _prvni(_KODEK),
        "hdr":       bool(re.search(_HDR, t, re.IGNORECASE)),
        # ⚠️ SK má PŘEDNOST: „CZ/SK dabing“ se občas píše obojí, ale slovenská
        # značka je konkrétnější signál než obecná zmínka o češtině.
        "cesky":     bool(re.search(_CESKY, t, re.IGNORECASE))
                     and not re.search(_SLOVENSKY, t, re.IGNORECASE),
        "slovensky": bool(re.search(_SLOVENSKY, t, re.IGNORECASE)),
        "dabing":    bool(re.search(_DABING, t, re.IGNORECASE)),
        "titulky":   bool(re.search(_TITULKY, t, re.IGNORECASE)),
    }


def _skore(polozka: dict) -> float:
    """Řadicí skóre: rozlišení + zdroj + poměr hlasů. Vyšší = nahoru.

    ⚠️ Hlasy jsou jediný signál o tom, jestli soubor NENÍ vadný — proto váží
    tolik co rozlišení. Soubor s 200 zápornými hlasy je k ničemu, i kdyby
    byl 4K. CAM dostává mínus, protože „2160p CAM“ je pořád CAM.
    """
    k = polozka.get("kvalita") or {}
    s = float(_PORADI_ROZL.get(k.get("rozliseni") or "", 0)) * 2.0
    s += float(_PORADI_ZDROJ.get(k.get("zdroj") or "", 0))
    kladne = float(polozka.get("kladne") or 0)
    zaporne = float(polozka.get("zaporne") or 0)
    celkem = kladne + zaporne
    if celkem >= 3:
        s += 6.0 * (kladne / celkem) - 3.0        # −3 (samé mínus) … +3
    if k.get("cesky"):
        s += 1.5
    elif k.get("slovensky") or k.get("titulky"):
        s += 0.5
    return s


# ── hledání ─────────────────────────────────────────────────────────────────

def _int(s, vych=0):
    try:
        return int(str(s).strip())
    except Exception:
        return vych


# HANS_WEBSHARE_JMENO_GATE_V1 — pomocný kanál pro počet přeskočených.
# ⚠️ Záměrně NE globální stav sdílený mezi vlákny na dlouho: `hledej` ho hned
# na začátku nuluje a čte se bezprostředně po návratu.
_preskoceno = [0]


def preskoceno() -> int:
    """Kolik nálezů poslední `hledej` zahodil pro nesmyslný název."""
    return int(_preskoceno[0])


# Přípony, které samy o sobě znamenají „tohle je pojmenovaný soubor“.
_PRIPONY = (".mkv", ".mp4", ".avi", ".mov", ".m4v", ".ts", ".mpg", ".mpeg",
            ".wmv", ".flv", ".webm", ".iso", ".srt", ".rar", ".zip", ".7z")


def ma_smysluplne_jmeno(nazev: str) -> bool:
    """Dá se z názvu vůbec něco poznat?

    Doložený tvar nesmyslu (Webshare ho vrací sám, není to chyba parseru):
    `du41756g5f4df7d7d744d4d4d`, `du30b04jac`, `du45g0sak` — jeden shluk bez
    oddělovačů, bez přípony, písmena jen po dvou třech mezi číslicemi.

    ⚠️ PREDIKÁT JE ZÁMĚRNĚ PŘÍSNÝ NA TO, CO PROHLÁSÍ ZA NESMYSL. Skrýt skutečný
    film je horší chyba než ukázat blob, takže při jakékoli pochybnosti vrací
    True. Za nesmysl se považuje jen shoda VŠECH podmínek naráz.
    """
    n = (nazev or "").strip()
    if not n:
        return False
    low = n.lower()
    if low.endswith(_PRIPONY):
        return True                      # má příponu → někdo ho pojmenoval
    if re.search(r"[\s()\[\]]", n):
        return True                      # mezery/závorky = lidské psaní
    # nejdelší souvislý běh písmen (včetně diakritiky)
    behy = re.findall(r"[^\W\d_]+", n, re.UNICODE)
    if behy and max(len(b) for b in behy) >= 4:
        return True                      # obsahuje aspoň jedno slovo
    if not re.search(r"\d", n):
        return True                      # bez číslic to na náhodný shluk nevypadá
    if len(n) > 40:
        return True                      # dlouhý řetězec bývá popisný
    return False


def hledej(config: dict, dotaz: str, limit: int = 25, kategorie: str = "video",
           razeni: str = "relevance", offset: int = 0) -> list:
    """Vrátí seznam nálezů, seřazený podle velikosti (viz `razeni_vypisu`)."""
    _preskoceno[0] = 0
    dotaz = (dotaz or "").strip()
    if not dotaz:
        return []
    c = _cfg(config)
    d = _s_tokenem(config, "search", {
        "what": dotaz, "category": kategorie, "sort": razeni,
        "limit": int(limit), "offset": int(offset)}, "hledání")
    nalezy = []
    for f in d["_root"].findall("file"):
        def _t(tag):
            el = f.find(tag)
            return (el.text or "").strip() if el is not None else ""
        nazev = _t("name")
        p = {"ident": _t("ident"), "nazev": nazev,
             "velikost": _int(_t("size")),
             "kladne": _int(_t("positive_votes")),
             "zaporne": _int(_t("negative_votes")),
             "typ": _t("type"),
             "kvalita": kvalita(nazev)}
        p["skore"] = _skore(p)
        nalezy.append(p)
    # HANS_WEBSHARE_JMENO_GATE_V1 (4.9., pokyn uživatele) — zahoď nálezy, jejichž
    # název nic neříká (`du49g84gij`). Bez tohohle je řazení podle velikosti
    # kontraproduktivní: právě ty bloby jsou největší, takže si sedly na špici.
    # ⚠️ NESMÍ MIZET TIŠE — kolik jich vypadlo, se vrací volajícímu (`_preskoceno`)
    # a Hans to v odpovědi řekne. Skrytý filtr je horší než šum ve výpisu.
    if bool(c.get("preskoc_bez_jmena", True)):
        pred = len(nalezy)
        nalezy = [p for p in nalezy if ma_smysluplne_jmeno(p["nazev"])]
        _preskoceno[0] = pred - len(nalezy)
        if _preskoceno[0]:
            log.info("webshare: %d nálezů přeskočeno pro nesmyslný název",
                     _preskoceno[0])
    min_hodnoceni = float(c.get("min_rating", 0.0) or 0.0)
    if min_hodnoceni > 0:
        pred = len(nalezy)
        nalezy = [p for p in nalezy
                  if (p["kladne"] + p["zaporne"]) < 3
                  or p["kladne"] / float(p["kladne"] + p["zaporne"]) >= min_hodnoceni]
        if pred != len(nalezy):
            log.info("webshare: %d nálezů odfiltrováno hodnocením < %.2f",
                     pred - len(nalezy), min_hodnoceni)
    # HANS_WEBSHARE_RAZENI_VELIKOST_V1 (4.9., pokyn uživatele) — řadí se podle
    # VELIKOSTI, ne podle odhadnutého skóre. Důvod je doložený: část souborů má
    # na Webshare nesmyslné názvy — v XML doslova
    # `<name>du41756g5f4df7d7d744d4d4d</name>` — takže `kvalita()` z nich nemá
    # z čeho vyjít a velikost je jediný čitelný signál. (⚠️ Ta jména vrací
    # Webshare, není to chyba parseru; ověřeno na syrovém XML.)
    # Skóre se POŘÁD počítá: rozhoduje mezi soubory STEJNĚ VELKÝMI a živí filtr
    # `min_rating`. Zpět na kvalitu: `webshare.razeni_vypisu` = "kvalita".
    zpusob = str(c.get("razeni_vypisu", "velikost")).strip().lower()
    if zpusob.startswith("kval"):
        nalezy.sort(key=lambda p: (-p["skore"], -p["velikost"]))
    else:
        nalezy.sort(key=lambda p: (-p["velikost"], -p["skore"]))
    log.info("webshare: '%s' → %d nálezů (řazeno podle %s)", dotaz[:60],
             len(nalezy), "kvality" if zpusob.startswith("kval") else "velikosti")
    return nalezy


def filtruj(nalezy: list, filtr: str) -> list:
    """Zúží nálezy podle slovního filtru: „1080p“, „4k“, „cz“, „bluray“…

    Neznámý filtr NEVYHAZUJE nic — radši vrátit vše než tiše prázdno.
    """
    f = (filtr or "").strip().lower()
    if not f:
        return nalezy
    for jmeno, _ in _ROZLISENI:
        if f in (jmeno, jmeno.rstrip("p")) or (f in ("4k", "uhd") and jmeno == "2160p"):
            return [p for p in nalezy if (p["kvalita"] or {}).get("rozliseni") == jmeno]
    if f in ("cz", "cesky", "česky", "dabing", "cesky dabing"):
        return [p for p in nalezy if (p["kvalita"] or {}).get("cesky")]
    if f in ("sk", "slovensky"):
        return [p for p in nalezy if (p["kvalita"] or {}).get("slovensky")]
    if f in ("tit", "titulky"):
        return [p for p in nalezy if (p["kvalita"] or {}).get("titulky")]
    for jmeno, _ in _ZDROJ:
        if f == jmeno.lower():
            return [p for p in nalezy if (p["kvalita"] or {}).get("zdroj") == jmeno]
    log.info("webshare: filtr '%s' neznám → nefiltruji", f[:30])
    return nalezy


def odkaz(config: dict, ident: str) -> str:
    """Přímý odkaz ke stažení. ⚠️ Webshare ho bez VIP nevydá."""
    d = _s_tokenem(config, "file_link", {
        "ident": ident, "download_type": "video_stream",
        "force_https": 1}, "získání odkazu")
    u = d.get("link") or ""
    if not u:
        raise WebshareError("odkaz nepřišel (bývá to chybějící VIP)")
    return u


# ── formátování pro chat ────────────────────────────────────────────────────

def velikost_str(b: int) -> str:
    b = float(b or 0)
    for jed in ("B", "kB", "MB", "GB", "TB"):
        if b < 1024 or jed == "TB":
            return ("%.0f %s" % (b, jed)) if jed in ("B", "kB") else ("%.1f %s" % (b, jed))
        b /= 1024.0
    return "?"


def popis_kvality(k: dict) -> str:
    """Krátký štítek: „1080p BluRay H265 HDR, CZ“. Prázdno = z názvu nic."""
    if not k:
        return ""
    casti = [x for x in (k.get("rozliseni"), k.get("zdroj"), k.get("kodek")) if x]
    if k.get("hdr"):
        casti.append("HDR")
    jazyk = "CZ" if k.get("cesky") else ("SK" if k.get("slovensky") else
                                         ("tit." if k.get("titulky") else ""))
    s = " ".join(casti)
    return (s + (", " + jazyk if jazyk and s else jazyk)) if (s or jazyk) else ""


def vypis(nalezy: list, kolik: int = 8) -> str:
    """Číslovaná tabulka pro chat. Čísla jsou to, čím se pak vybírá."""
    if not nalezy:
        return "Nic jsem nenašel."
    r = []
    for i, p in enumerate(nalezy[:kolik], 1):
        hlasy = ""
        if (p["kladne"] + p["zaporne"]) >= 3:
            hlasy = " · %d/%d hlasů" % (p["kladne"], p["kladne"] + p["zaporne"])
        kv = popis_kvality(p.get("kvalita"))
        r.append("%d. %s\n   %s%s%s" % (
            i, p["nazev"][:90], velikost_str(p["velikost"]),
            (" · " + kv) if kv else "", hlasy))
    if len(nalezy) > kolik:
        r.append("…a další %d." % (len(nalezy) - kolik))
    return "\n".join(r)


# ── stažení NA PC (tam je NAS) ──────────────────────────────────────────────

def _q(s: str) -> str:
    return "'" + str(s).replace("'", "'\\''") + "'"


def _bezpecny_nazev(s: str) -> str:
    """Název souboru bez věcí, které v shellu nebo na FS zlobí."""
    s = re.sub(r"[\\/:*?\"<>|\r\n\t]+", "_", (s or "").strip())
    s = re.sub(r"\s+", " ", s).strip(" .")
    return (s or "soubor")[:180]


def _pi_dir(c: dict) -> str:
    """Adresář na Pi. ⚠️ ZÁMĚRNĚ MIMO REPO (výchozí `~/webshare_stazene`).

    Vícegigové filmy nepatří do `data/`: zálohovací skript sice dnes jede přes
    allowlist, ale stačilo by ho jednou rozšířit a zálohy by nafoukly filmy.
    Mimo strom projektu ta otázka nevznikne.
    """
    return os.path.expanduser(str(c.get("pi_dir") or "~/webshare_stazene"))


def _volne_misto(cesta: str) -> int:
    """Volné bajty na svazku, kde `cesta` leží (adresář nemusí existovat)."""
    p = cesta
    while p and not os.path.isdir(p):
        rodic = os.path.dirname(p)
        if rodic == p:
            break
        p = rodic
    try:
        return int(shutil.disk_usage(p or "/").free)
    except Exception:
        return 0


def stahni_na_pi(config: dict, ident: str, nazev: str, velikost: int = 0) -> dict:
    """Stáhni na PI na pozadí. Přesun na PC obstará `presun_na_pc` až potom.

    ⚠️ Stahuje DETACHED SHELL (`nohup curl`), ne vlákno v Hansovi — u dvanácti-
    hodinového stahování je restart Hanse jistota, ne výjimka, a vlákno by ho
    nepřežilo. Shell přežije i pád Hanse.
    ⚠️ MÍSTO SE KONTROLUJE PŘEDEM. Zaplnit Pi systémový disk by shodilo celého
    Hanse, ne jen stahování — to je nejdražší chyba, kterou tahle funkce může
    udělat, proto radši odmítne, než aby to zkusila.
    """
    import subprocess
    c = _cfg(config)
    cil_dir = _pi_dir(c)
    jmeno = _bezpecny_nazev(nazev)
    cesta = os.path.join(cil_dir, jmeno)
    rezerva = int(float(c.get("rezerva_gb", 5)) * 1024 ** 3)
    volno = _volne_misto(cil_dir)
    # HANS_WEBSHARE_MISTO_GUARD_FIX_V1 — původní podmínka zněla
    # `if velikost and volno and …`, takže `volno == 0` pojistku VYPNULO.
    # Jenže nula znamená buď „disk je plný“, nebo „nepodařilo se to zjistit“ —
    # obojí je důvod NESTAHOVAT, ne důvod pokračovat. Selhání detekce nesmí
    # vypnout pojistku, kterou má detekce živit.
    if volno <= 0:
        raise WebshareError("nezjistil jsem volné místo na Pi, radši nestahuji")
    if velikost and (velikost + rezerva) > volno:
        raise WebshareError(
            "na Pi by to nevyšlo: soubor má %s, volno je %s a %s si nechávám "
            "jako rezervu" % (velikost_str(velikost), velikost_str(volno),
                              velikost_str(rezerva)))
    url = odkaz(config, ident)              # ⚠️ do logu NESMÍ (nese token)
    os.makedirs(cil_dir, exist_ok=True)
    logf = cesta + ".log"
    vnitrni = ("curl -sSL --retry 5 --retry-delay 30 -C - -o %s %s >>%s 2>&1 "
               "&& mv -f %s %s" % (_q(cesta + ".part"), _q(url), _q(logf),
                                   _q(cesta + ".part"), _q(cesta)))
    subprocess.Popen(["nohup", "sh", "-c", vnitrni],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     stdin=subprocess.DEVNULL, start_new_session=True)
    cil_pc = "%s/%s" % (str(c.get("out_dir") or "/mnt/D/Hans_stazene").rstrip("/"),
                        jmeno)
    uloha = {"ident": ident, "nazev": jmeno, "cesta": cesta, "kde": "pi",
             "cil_pc": cil_pc, "velikost": int(velikost or 0),
             "start": time.time(), "presunuto": 0}
    _uloz_ulohu(uloha)
    log.info("webshare: stahuji na Pi → %s (volno %s)", cesta, velikost_str(volno))
    return uloha


def stahni(config: dict, ident: str, nazev: str, velikost: int = 0) -> dict:
    """Rozcestník podle `webshare.stahovat_na` — „pi“ (výchozí) nebo „pc“."""
    kam = str(_cfg(config).get("stahovat_na", "pi")).strip().lower()
    if kam.startswith("pc"):
        return stahni_na_pc(config, ident, nazev)
    return stahni_na_pi(config, ident, nazev, velikost)


def stahni_na_pc(config: dict, ident: str, nazev: str) -> dict:
    """Spusť stažení na PC NA POZADÍ a hned se vrať.

    ⚠️ SSH session se NEDRŽÍ po celou dobu stahování — vícegigový soubor by
    blokoval Hansovu smyčku a jakýkoli výpadek spojení by stahování zabil.
    Na PC se proto pustí `nohup curl` a průběh se pak dopočítává z velikosti
    `.part` souboru (`stav_stahovani`). Týž vzor jako u `_Recorder` u hlídání:
    dlouhá práce nepatří na hlavní vlákno.
    """
    from scripts import pc_remote
    if not pc_remote.enabled(config):
        raise WebshareError("pc_remote je vypnutý, nemám kam stahovat")
    c = _cfg(config)
    cil_dir = str(c.get("out_dir") or "/mnt/D/Hans_stazene")
    jmeno = _bezpecny_nazev(nazev)
    url = odkaz(config, ident)                # ⚠️ do logu se NESMÍ (nese token)
    cesta = "%s/%s" % (cil_dir.rstrip("/"), jmeno)
    logf = "%s.log" % cesta
    # `curl -C -` naváže na rozdělaný .part, kdyby stahování spadlo.
    cmd = ("mkdir -p %s && nohup sh -c %s >/dev/null 2>&1 & echo START" % (
        _q(cil_dir),
        _q("curl -sSL --retry 3 -C - -o %s.part %s >>%s 2>&1 && mv -f %s.part %s"
           % (_q(cesta), _q(url), _q(logf), _q(cesta), _q(cesta)))))
    out = pc_remote.run(config, cmd, timeout=30)
    if out is None:
        raise WebshareError("PC neodpověděl (spí?), stahování jsem nespustil")
    uloha = {"ident": ident, "nazev": jmeno, "cesta": cesta,
             "start": time.time()}
    _uloz_ulohu(uloha)
    log.info("webshare: stahuji na PC → %s", cesta)
    return uloha


_JOBS = "data/.webshare_jobs.json"


def _uloz_ulohu(u: dict) -> None:
    try:
        try:
            with open(_JOBS, encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            d = []
        d = [x for x in d if x.get("cesta") != u.get("cesta")][-19:]
        d.append(u)
        with open(_JOBS, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
    except Exception as e:
        log.debug("webshare: úlohu se nepodařilo zapsat: %s", e)


def ulohy() -> list:
    try:
        with open(_JOBS, encoding="utf-8") as f:
            return json.load(f) or []
    except Exception:
        return []


def _aktualizuj_ulohu(cesta: str, **zmeny) -> None:
    try:
        d = ulohy()
        for x in d:
            if x.get("cesta") == cesta:
                x.update(zmeny)
        with open(_JOBS, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
    except Exception as e:
        log.debug("webshare: úlohu se nepodařilo aktualizovat: %s", e)


def hotove_ke_presunu() -> list:
    """Úlohy stažené na Pi, které ještě nejsou na PC.

    Hotovo = cílový soubor existuje a `.part` už ne (přejmenování dělá až
    `curl && mv`, takže je to spolehlivý signál dokončení, ne odhad velikosti).
    """
    out = []
    for u in ulohy():
        if u.get("kde") != "pi" or u.get("presunuto"):
            continue
        c = u.get("cesta") or ""
        if c and os.path.exists(c) and not os.path.exists(c + ".part"):
            out.append(u)
    return out


def presun_na_pc(config: dict, uloha: dict) -> str:
    """Přesuň hotový soubor z Pi na PC. Vrátí popis výsledku pro člověka.

    ⚠️ POŘADÍ JE ZÁVAZNÉ: kopie → OVĚŘENÍ VELIKOSTI na PC → teprve pak smazat
    z Pi. Obráceně (nebo bez ověření) by přerušený `scp` znamenal, že soubor
    není ani tady, ani tam. Radši ať leží dvakrát než nikde.
    ⚠️ PC se kvůli přesunu NEBUDÍ — když spí, úloha počká na příště.
    """
    import subprocess
    from scripts import pc_remote
    c = _cfg(config)
    zdroj = uloha.get("cesta") or ""
    cil = uloha.get("cil_pc") or ""
    if not (zdroj and cil and os.path.exists(zdroj)):
        return ""
    if not pc_remote.enabled(config):
        return ""
    pr = (config or {}).get("pc_remote", {}) or {}
    if pc_remote.run(config, "mkdir -p %s && echo ok" % _q(os.path.dirname(cil)),
                     timeout=20) is None:
        log.info("webshare: PC nedostupný, přesun „%s“ odkládám", uloha.get("nazev"))
        return ""
    klic = os.path.expanduser(str(pr.get("key_path", "~/.ssh/hans_pc")))
    cil_ssh = "%s@%s:%s" % (pr.get("user", "user"), pr.get("host", ""), cil)
    mistni = int(os.path.getsize(zdroj))
    t0 = time.time()
    r = subprocess.run(["scp", "-q", "-i", klic, "-o", "BatchMode=yes",
                        "-o", "StrictHostKeyChecking=accept-new", zdroj, cil_ssh],
                       capture_output=True, text=True,
                       timeout=int(c.get("presun_timeout_s", 3600)))
    if r.returncode != 0:
        log.warning("webshare: scp selhal (%s), soubor nechávám na Pi: %s",
                    r.returncode, (r.stderr or "")[-160:])
        return ""
    # OVĚŘENÍ, teprve pak mazání
    # HANS_WEBSHARE_STAT_FIX_V1 (4.9.) — bylo tu `stat -c %%s`, jenže v `stat`
    # znamená `%%` LITERÁLNÍ procento, takže příkaz vracel doslova „%s“ a
    # ověření porovnávalo 0 proti skutečné velikosti → soubor se po přesunu
    # nikdy nesmazal z Pi. Změřeno na PC: `stat -c %%s` → '%s',
    # `stat -c %s` → '44325606'. Použit `wc -c`, který procento neobsahuje
    # vůbec, takže ho nerozbije ani budoucí formátování řetězce.
    # ⚠️ Chyba selhala BEZPEČNÝM směrem (radši nechat dvakrát než smazat) —
    # proto ji odhalil až test, ne ztráta dat.
    out = pc_remote.run(config, "wc -c < " + _q(cil) + " 2>/dev/null || echo 0",
                        timeout=25)
    na_pc = _int((out or "0").strip().splitlines()[-1] if (out or "").strip() else 0)
    if na_pc != mistni:
        log.warning("webshare: velikost na PC nesedí (%s vs %s) — NEMAŽU z Pi",
                    na_pc, mistni)
        return ""
    try:
        os.remove(zdroj)
        for pripona in (".log", ".part"):
            if os.path.exists(zdroj + pripona):
                os.remove(zdroj + pripona)
    except Exception as e:
        log.debug("webshare: úklid na Pi: %s", e)
    _aktualizuj_ulohu(zdroj, presunuto=time.time(), kde="pc_hotovo")
    dt = time.time() - t0
    log.info("webshare: přesunuto na PC → %s (%s za %.0f s)",
             cil, velikost_str(mistni), dt)
    return "Přesunul jsem „%s“ (%s) na počítač do %s." % (
        uloha.get("nazev"), velikost_str(mistni), os.path.dirname(cil))


def presun_hotove(config: dict) -> list:
    """Přesuň vše, co je stažené a čeká. Vrátí hlášky o přesunutém."""
    zpravy = []
    for u in hotove_ke_presunu():
        try:
            z = presun_na_pc(config, u)
            if z:
                zpravy.append(z)
        except Exception as e:
            log.warning("webshare: přesun „%s“ selhal: %s", u.get("nazev"), e)
    return zpravy


def stav_stahovani(config: dict, uloha: dict = None) -> str:
    """Jak daleko je stahování?

    ⚠️ Odpovídá i na dotaz „jak jde to stahování“ — a NESMÍ přitom nic
    spouštět. Ta past je doložená u `/preloz`, kde dotaz na stav práci
    rozjížděl místo hlášení.
    ⚠️ HANS_WEBSHARE_PI_DOWNLOAD_V1 — rozlišuje TŘI stavy, ne dva: stahuji na
    Pi · staženo a čeká na přesun · na PC hotovo. Sloučit je by znamenalo hlásit
    „hotovo“ o souboru, který na PC ještě není.
    """
    from scripts import pc_remote
    u = uloha or (ulohy() or [None])[-1]
    if not u:
        return "Nic jsem nestahoval."
    cesta = u.get("cesta") or ""
    if u.get("kde") in ("pi", "pc_hotovo"):
        if u.get("presunuto"):
            return "Hotovo: „%s“ je na počítači (%s)." % (
                u.get("nazev"), u.get("cil_pc") or "")
        hotovo = os.path.exists(cesta) and not os.path.exists(cesta + ".part")
        try:
            mam = os.path.getsize(cesta if hotovo else cesta + ".part")
        except Exception:
            mam = 0
        celkem = int(u.get("velikost") or 0)
        if hotovo:
            return ("Staženo na Pi: „%s“ (%s) — přesunu na počítač, až bude "
                    "vzhůru." % (u.get("nazev"), velikost_str(mam)))
        bezi_min = (time.time() - float(u.get("start") or time.time())) / 60.0
        pct = (" (%.0f %%)" % (100.0 * mam / celkem)) if celkem else ""
        zbyva = ""
        if celkem and mam > 0 and bezi_min > 0.5:
            rych = mam / (bezi_min * 60.0)
            if rych > 0:
                zbyva = ", zbývá ~%.1f h" % ((celkem - mam) / rych / 3600.0)
        return "Stahuji „%s“ na Pi — zatím %s%s, běží %d min%s." % (
            u.get("nazev"), velikost_str(mam), pct, int(bezi_min), zbyva)
    # HANS_WEBSHARE_STAT_FIX_V1 — viz `presun_na_pc`: `stat -c %%s` vrací „%s“.
    out = pc_remote.run(config, "wc -c < " + _q(cesta) + " 2>/dev/null || "
                        "wc -c < " + _q(cesta + ".part") + " 2>/dev/null || echo 0",
                        timeout=20)
    if out is None:
        return "PC teď neodpovídá, o stahování „%s“ nemám zprávy." % u.get("nazev", "")
    mam = _int((out or "0").strip().splitlines()[-1] if out.strip() else 0)
    hotovo = pc_remote.run(config, "test -f " + _q(cesta) + " && echo HOTOVO || echo BEZI",
                           timeout=20)
    stav = (hotovo or "").strip()
    if stav == "HOTOVO":
        return "Staženo: %s (%s)." % (u.get("nazev"), velikost_str(mam))
    kolik = (time.time() - float(u.get("start") or time.time())) / 60.0
    return "Stahuji „%s“ — zatím %s, běží %d min." % (
        u.get("nazev"), velikost_str(mam), int(kolik))
