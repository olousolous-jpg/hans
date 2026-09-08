"""config_io.py — HANS_CONFIG_SPLIT_V1 (8. 9.)

JEDEN zdroj pravdy o tom, CO je config a KDE leží.

Proč vzniklo: `config.json` byl celý gitignored, protože v něm vedle sebe
leží laděné prahy a skutečná tajemství. Důsledek — **1 209 laděných hodnot
(93 % configu) nebylo verzovaných** a držela je jen ručně udržovaná kopie
`config.example.json`, která 8. 9. zaostávala o 116 klíčů. Po přeinstalaci
by se hodnoty musely nastavit znovu a nikdo by nevěděl které, protože se
ladily měřením.

Rozdělení:
    config.json          — VEŘEJNÝ, v gitu. Prahy a chování Hanse.
    config.private.json  — gitignored. Osoby, tokeny, IP, UUID, MAC.

Privátní část PŘEBÍJÍ veřejnou (deep merge), takže veřejný soubor smí nést
neutrální zástupnou hodnotu a nikdo se nedozví tu skutečnou.

⚠️ TŘI VĚCI, KTERÉ TENHLE MODUL DRŽÍ POHROMADĚ — nerozdělovat:
 1. `load()` — merge. Používají ho VŠECHNY cesty (8 loaderů, dva ve
    vlastních procesech). Dokud četly `config.json` samy, byla by tohle
    osmá příležitost k rozejití.
 2. `save()` — split zpět. **Nebezpečnější než čtení:**
    `web_admin.save_config(body)` ukládá CELÝ sloučený objekt; bez splitu
    by tajemství skončilo ve veřejném souboru a odešlo prvním commitem.
 3. `zkontroluj_verejny()` — strážce. Nový klíč s tajemstvím se chytí sám,
    i když ho nikdo nepřidá do seznamu níž.

⚠️ CHYBĚJÍCÍ PRIVÁTNÍ SOUBOR SE HLÁSÍ NAHLAS. Tiché pokračování s defaulty
je přesně vzorec, kvůli kterému byl VRAM handoff renderu roky mrtvý
(neexistující klíč + `or "127.0.0.1"`); tady by se navíc projevil jako
„Hans nikoho nezná" bez jediné chyby v logu.
"""
from __future__ import annotations

import json
import logging
import re
import unicodedata
from pathlib import Path
from typing import Optional

_log = logging.getLogger("config_io")

VEREJNY = "config.json"
PRIVATNI = "config.private.json"

# ── Co je privátní ────────────────────────────────────────────────────────
# Celé sekce. Vše pod nimi jde do privátního souboru.
PRIVATNI_SEKCE = (
    "known_persons",            # jména, pády, poznámky o členech domácnosti
    "relationship_seed",        # rodinné vazby
    "person_name_forms",
    "calendar.people",          # Proton ICS odkazy — nesou CacheKey/PassphraseKey
    "greeting.special_greetings",
    "face_recognition.known_faces",
)

# Jednotlivé cesty (sekce jako celek veřejná zůstat má).
PRIVATNI_CESTY = {
    "wol_pc_mac",
    "hailo.recog_hef",          # absolutní cesta s uživatelským jménem
    "matrix.as_person",
    "pc_remote.user",
    "translate.pc_user",
    "voice.default_speaker",
    "film_suggest.film_prefs",
    "film_suggest.genre_prefs",
    "persona.address_rules",    # obsahuje vokativy členů domácnosti
    "recognition_tuning.negatives_path",
    "recognition_tuning.pc_clusters",
}

# Vzory nad NÁZVEM klíče.
# ⚠️ `username` sem přibylo až po NEZÁVISLÉM skenu 8. 9.: `subtitles.username`
# prošlo, protože vzor znal jen `user_id`. Vlastnímu detektoru se nedá věřit —
# proto se výsledek ověřuje ještě grepem na konkrétní známé hodnoty.
_PRIVATNI_NAZEV = re.compile(
    r"token|passw|secret|\bkey\b|_key|key_path|room_id|chat_id|user_id|"
    r"username|\buser\b|login|credential|api_key|access_key|webhook|"
    r"\bmail\b|account|handle", re.I)

# Identifikátory, které bývají ČÍSLO a přesto jsou citlivé.
_CISELNE_ID = re.compile(r"chat_id|room_id|user_id|account_id|group_id", re.I)

# Vzory nad HODNOTOU — strážce. Chytí i klíč, který nikdo neohlásil.
_PODEZRELA_HODNOTA = (
    ("MAC adresa",       re.compile(r"\b([0-9a-f]{2}:){5}[0-9a-f]{2}\b", re.I)),
    ("IP vnitřní sítě",  re.compile(r"\b(?:10|172|192)\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")),
    ("UUID",             re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-")),
    ("adresa ven",       re.compile(r"https?://(?!127\.0\.0\.1|localhost)")),
    ("e-mail",           re.compile(r"[\w.+-]+@[\w-]+\.\w{2,}")),
    ("domácí cesta",     re.compile(r"/home/[a-z]")),
    ("cesta ke klíči",   re.compile(r"\.ssh/")),
)

# Jména členů domácnosti se sem NEPÍŠOU (soubor je v gitu). Strážce je bere
# z privátního configu za běhu — kdo v něm je, ten se ve veřejné části
# vyskytovat nesmí.
_JMENA_ODKUD = ("known_persons", "person_name_forms", "relationship_seed")


def koren(start: Optional[Path] = None) -> Path:
    """Najdi kořen projektu (nese `config.json`). Hledá se nahoru, protože
    `hailo_inference_server` i `gesture_server` běží z `scripts/`."""
    p = (start or Path(__file__)).resolve()
    for _ in range(5):
        p = p.parent
        if (p / VEREJNY).exists():
            return p
    return Path(__file__).resolve().parent.parent


def _slouc(zaklad: dict, vrch: dict) -> dict:
    """Deep merge — `vrch` (privátní) přebíjí `zaklad` (veřejný)."""
    out = dict(zaklad)
    for k, v in vrch.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _slouc(out[k], v)
        else:
            out[k] = v
    return out


def _ploche(d: dict, pref: str = "") -> dict:
    """Zploští config na `cesta -> hodnota`.

    ⚠️ PRÁZDNÝ objekt se nese jako hodnota `{}`. Bez toho by sekce bez klíčů
    (8. 9. `mediapipe`) zmizela úplně: nemá co zploštit, takže by se do žádné
    z obou částí nedostala. Kód, který na ni sahá přes `cfg["mediapipe"]`,
    by pak dostal KeyError místo prázdna."""
    o = {}
    for k, v in d.items():
        c = pref + k
        if isinstance(v, dict) and v:
            o.update(_ploche(v, c + "."))
        else:
            o[c] = v
    return o


def _jmena_z(cfg: dict) -> set:
    """Jména členů domácnosti přímo z configu — do tohohle souboru se psát
    NESMĚJÍ (je v gitu) a nový člen musí být hlídaný bez zásahu do kódu.

    ⚠️ Bere i PÁDOVÉ TVARY, ne jen klíč. Čeština jméno ohýbá a texty v configu
    ho nesou skloňované: `greeting.user_prompt` obsahoval příklad „Dobrý večer,
    **<vokativ>**" a prošel prvním skenem, protože klíč je 1. pád a vokativ
    na něj nesedí (u vzoru „Jana" by to bylo „Jano"). Tvary jsou v `known_persons.<kdo>.{nom,gen,dat,acc,loc,voc}`
    — přesně proto tam jsou (HANS_VOCATIVE_CONSONANT_V1)."""
    jm = set()
    for sekce in _JMENA_ODKUD:
        obsah = cfg.get(sekce) or {}
        for kdo, udaje in obsah.items():
            if isinstance(kdo, str) and len(kdo) >= 3:
                jm.add(kdo.lower())
            if isinstance(udaje, dict):
                for pole in ("nom", "gen", "dat", "acc", "loc", "voc", "full",
                             "display_name"):
                    tvar = udaje.get(pole)
                    if isinstance(tvar, str) and len(tvar) >= 3:
                        jm.update(t.lower() for t in tvar.split() if len(t) >= 3)
            elif isinstance(udaje, str) and len(udaje) >= 3:
                jm.update(t.lower() for t in udaje.split() if len(t) >= 3)
    return jm


def bez_diakritiky(s: str) -> str:
    """Sundá diakritiku: „Žofie" → „zofie". Bez tohohle projde jméno psané
    s háčky — v configu je klíč bez diakritiky, ale v poznámce stojí s ní,
    a přesně tak 8. 9. unikl `recognition_tuning._gallery_note` prvnímu
    skenu."""
    return "".join(c for c in unicodedata.normalize("NFKD", s)
                   if not unicodedata.combining(c)).lower()


def _vzor_jmen(jmena: set):
    if not jmena:
        return None
    holá = {bez_diakritiky(j) for j in jmena}
    return re.compile(r"\b(%s)\w*" % "|".join(re.escape(j) for j in sorted(holá)))


def podezrela_hodnota(hodnota, jm_re=None) -> Optional[str]:
    """Vrať důvod, proč hodnota nepatří do veřejného souboru (nebo None)."""
    s = json.dumps(hodnota, ensure_ascii=False)
    for popis, vzor in _PODEZRELA_HODNOTA:
        if vzor.search(s):
            return popis
    if jm_re and jm_re.search(bez_diakritiky(s)):
        return "jméno člena domácnosti"
    return None


def je_privatni(cesta: str, hodnota=None, jm_re=None) -> bool:
    """HANS_CONFIG_SPLIT_V1 — o privátnosti rozhoduje CESTA i HODNOTA.

    Ruční seznam cest sám nestačil: sken 8. 9. našel MAC adresu, Matrix ID
    uživatele, sudoers řádek se jménem a 15 vnitřních IP roztroušených po
    configu — mezi nimi i v poznámkách `_note`, kam by je nikdo nehledal.
    Seznam by se navíc musel doplňovat u každého nového klíče a **první
    zapomenutý klíč je leak do veřejného repa**. Proto o hodnotě rozhoduje
    týž detektor, který dělá strážce: co vypadá citlivě, jde do privátní
    části samo."""
    if cesta in PRIVATNI_CESTY:
        return True
    if any(cesta == s or cesta.startswith(s + ".") for s in PRIVATNI_SEKCE):
        return True
    if _PRIVATNI_NAZEV.search(cesta.split(".")[-1]):
        # ⚠️ Název sám nestačí: `min_key_len`, `anchor_max_tokens`
        # a `token_ftl_s` obsahují „key"/„token", ale jsou to LADĚNÉ MEZE,
        # ne tajemství — a v privátní části by přestaly být verzované,
        # tedy přesně to, co má rozdělení configu vyřešit (nález 8. 9. při
        # kontrole před commitem). Tajemství je vždycky TEXT; výjimkou jsou
        # číselné identifikátory (Telegram `chat_id` je číslo a citlivý je).
        if isinstance(hodnota, str) or hodnota is None:
            return True
        if _CISELNE_ID.search(cesta.split(".")[-1]):
            return True
    if hodnota is not None and podezrela_hodnota(hodnota, jm_re):
        return True
    return False


def _vloz(cil: dict, cesta: str, hodnota) -> None:
    kusy = cesta.split(".")
    d = cil
    for k in kusy[:-1]:
        d = d.setdefault(k, {})
    d[kusy[-1]] = hodnota


def rozdel(cfg: dict) -> tuple[dict, dict]:
    """Rozděl sloučený config na (veřejný, privátní)."""
    ver: dict = {}
    priv: dict = {}
    jm_re = _vzor_jmen(_jmena_z(cfg))
    for cesta, hodnota in _ploche(cfg).items():
        _vloz(priv if je_privatni(cesta, hodnota, jm_re) else ver, cesta, hodnota)
    return ver, priv


def load(root: Optional[Path] = None, hlasit: bool = True) -> dict:
    """Načti sloučený config. Privátní část přebíjí veřejnou."""
    r = root or koren()
    try:
        ver = json.loads((r / VEREJNY).read_text(encoding="utf-8"))
    except Exception as e:
        _log.error("config: %s nejde načíst: %s", VEREJNY, e)
        return {}
    pcesta = r / PRIVATNI
    if not pcesta.exists():
        if hlasit:
            # NAHLAS: bez tohohle by Hans běžel dál a jen by nikoho neznal
            _log.error(
                "config: %s CHYBÍ — Hans poběží bez jmen domácnosti, tokenů "
                "a adres. Zkopíruj %s.example a vyplň.", PRIVATNI, PRIVATNI)
        return ver
    try:
        priv = json.loads(pcesta.read_text(encoding="utf-8"))
    except Exception as e:
        _log.error("config: %s je rozbitý (%s) — běžím jen s veřejnou částí", PRIVATNI, e)
        return ver
    return _slouc(ver, priv)


def zkontroluj_verejny(ver: dict, priv: Optional[dict] = None) -> list[str]:
    """Vrať seznam nálezů, které do VEŘEJNÉ části nepatří. Prázdný = čisté.

    Bere i jména osob z privátní části, takže nový člen domácnosti je
    hlídaný, aniž by se jeho jméno muselo psát do tohohle souboru."""
    nalezy = []
    jm_re = _vzor_jmen(_jmena_z(priv or {}))
    for cesta, hodnota in _ploche(ver).items():
        # hodnota se předává ZÁMĚRNĚ: bez ní by `je_privatni` u názvů typu
        # `min_key_len` viděl `None` a hlásil je jako tajemství (viz komentář
        # tamtéž). Strážce musí soudit stejně jako rozdělení, jinak hlásí
        # nálezy, které sám nezpůsobil.
        if je_privatni(cesta, hodnota, jm_re):
            nalezy.append("%s — patří do privátní části" % cesta)
            continue
        duvod = podezrela_hodnota(hodnota, jm_re)
        if duvod:
            nalezy.append("%s — %s v hodnotě" % (cesta, duvod))
    return nalezy


def save(cfg: dict, root: Optional[Path] = None) -> bool:
    """Ulož sloučený config zpět do dvou souborů.

    ⚠️ Volá se i z `web_admin`, kam přijde CELÝ objekt z UI. Split tady je
    jediné, co brání tomu, aby token skončil ve veřejném souboru."""
    r = root or koren()
    ver, priv = rozdel(cfg)
    spatne = zkontroluj_verejny(ver, priv)
    if spatne:
        _log.error("config: ve veřejné části zůstalo %d podezřelých hodnot, "
                   "NEUKLÁDÁM: %s", len(spatne), "; ".join(spatne[:5]))
        return False
    try:
        (r / VEREJNY).write_text(
            json.dumps(ver, indent=4, ensure_ascii=False) + "\n", encoding="utf-8")
        if priv:
            (r / PRIVATNI).write_text(
                json.dumps(priv, indent=4, ensure_ascii=False) + "\n", encoding="utf-8")
        return True
    except Exception as e:
        _log.error("config: zápis selhal: %s", e)
        return False
