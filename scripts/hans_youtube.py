"""HANS_YT_TRANSLATE_V1 — YouTube jako ZDROJ pro překladový řetěz.

Zadání uživatele (26.8.): „na telefonu mám Yatse. Pustím na YouTube video
a nasdílím ho do Yatse a ten ho přehraje na Kodi. Dalo by se to nadabovat také?"

Mění se JEN zdroj. Překlad, hlas, mux i evidence zůstávají beze změny —
tenhle modul dodá soubor na disku a titulky, dál to jede jako u dokumentu.

KDE TO BĚŽÍ: `yt-dlp` je na **Pi**, ne na PC. Důvod není libovůle:
na PC chybí `pip` (Python 3.14, externally-managed) a hlavně tam běží
**OpenSnitch** — nový program se ven nedostane, dokud to někdo neodklikne
u stroje. Pi má síť volnou a titulky odtud stahuje už dnes. Cena je přenos
staženého videa na PC (scp, ~1–2 min na hodinový pořad).

⚠️ ROLUJÍCÍ TITULKY (změřeno 27.8. na reálném pořadu, 1958 replik):
automatické titulky YouTube NEJSOU normální titulky. Každá replika nese
NAHOŘE zopakovaný předchozí řádek a DOLE nový text, mezi nimi jsou
desetimilisekundové „usazovací" repliky, slova mají vlastní časové značky
v `<c>` tazích a text NEMÁ INTERPUNKCI ani velká písmena:

    00:00:09.920 --> 00:00:12.230
    (mezera)
    it<00:00:10.080><c> was</c><00:00:10.240><c> the</c>… sun
    00:00:12.230 --> 00:00:12.240        ← usazovací, 10 ms
    it was the empire on which the sun
    00:00:12.240 --> 00:00:15.910
    it was the empire on which the sun   ← zopakováno, NENÍ to nový text
    never<00:00:12.639><c> set</c>…

Proto: zahodit repliky kratší než 50 ms, z ostatních vzít JEN POSLEDNÍ řádek
a útržky slepit do frází. Bez toho by Hans každou větu namluvil dvakrát.

⚠️ ROLOVÁNÍ SE SMÍ ODSTRAŇOVAT JEN U AUTOMATICKÝCH TITULKŮ. Ruční titulky
mají v replice běžně dva řádky dialogu („– Ahoj.\\n– Nazdar.") a braním
posledního řádku by se půlka textu ztratila. Proto se ruční a automatické
titulky stahují ZVLÁŠŤ a každé jdou svou cestou.

⚠️ Stahování z YouTube je proti podmínkám služby. Uživatel na to byl
upozorněn 27.8. a s domácím použitím souhlasil.
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess

log = logging.getLogger(__name__)

# Kodi hraje YouTube přes plugin → `Player.GetItem` nevrátí cestu k souboru,
# ale `plugin://plugin.video.youtube/play/?video_id=…`. Bereme i holé odkazy,
# kdyby se video dostalo do Kodi jinudy (strm soubor, jiný doplněk).
_VZORY = (
    # ⚠️ NEJDŮLEŽITĚJŠÍ TVAR, a backlog ho neuhodl: doplněk YouTube s DASH
    # (InputStream Adaptive) NEHRAJE přes `plugin://`, ale přes LOKÁLNÍ PROXY
    # na Kodi boxu — `http://127.0.0.1:50152/youtube/manifest/dash?file=<ID>.mpd`.
    # Id je v parametru `file=` jako `<ID>.mpd`. Doloženo naživo 27.8. videem
    # nasdíleným z Yatse; předtím Hans hlásil „není na známém úložišti".
    re.compile(r"[?&]file=([A-Za-z0-9_-]{6,})\.(?:mpd|m3u8)"),
    re.compile(r"/youtube/(?:manifest|video)/[^?]*[?&]video_id=([A-Za-z0-9_-]{6,})"),
    re.compile(r"plugin://plugin\.video\.youtube/.*?[?&]video_id=([A-Za-z0-9_-]{6,})"),
    re.compile(r"[?&]v=([A-Za-z0-9_-]{6,})"),
    re.compile(r"youtu\.be/([A-Za-z0-9_-]{6,})"),
    re.compile(r"youtube\.com/(?:embed|shorts|v)/([A-Za-z0-9_-]{6,})"),
)


def video_id(file_url: str) -> str | None:
    """Z toho, co hlásí Kodi, vytáhne id videa. None = není to YouTube."""
    if not file_url:
        return None
    u = str(file_url)
    if "youtube" not in u.lower() and "youtu.be" not in u.lower():
        return None
    for v in _VZORY:
        m = v.search(u)
        if m:
            return m.group(1)
    return None


def _q(p: str) -> str:
    """Uvozovkování cesty pro shell na PC (týž tvar jako v hans_translate —
    cesty k pořadům běžně obsahují mezery i apostrofy)."""
    return "'" + str(p).replace("'", "'\\''") + "'"


def _bin(cfg) -> str:
    return os.path.expanduser(cfg.get("ytdlp_bin", "~/.local/bin/yt-dlp"))


def _yt(cfg, args: list[str], timeout: int) -> str:
    r = subprocess.run([_bin(cfg)] + args, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"yt-dlp selhal: {(r.stderr or r.stdout)[-400:]}")
    return r.stdout


# ── VTT → SRT ────────────────────────────────────────────────────────────────
_TAG = re.compile(r"<[^>]*>")
_TS = re.compile(r"(\d+):(\d\d):(\d\d)[.,](\d\d\d)\s*-->\s*(\d+):(\d\d):(\d\d)[.,](\d\d\d)")
_ZVUK = re.compile(r"^[\[\(][^\]\)]*[\]\)]$")     # [Music], (applause) — nečíst nahlas


def _sec(h, m, s, ms) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000


def _ts(x: float) -> str:
    h, m, s = int(x // 3600), int(x % 3600 // 60), x % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}".replace(".", ",")


def parse_vtt(path: str) -> list[dict]:
    """Repliky z VTT. Vrací {'start','end','lines'}.

    ⚠️ Blok se ukončuje POUZE prázdným řádkem. YouTube píše jako výplň řádek
    s JEDNOU MEZEROU a `strip()` by ho za prázdný považoval → replika by se
    uřízla hned za časem a přišla o text. Změřeno 27.8.: tahle jediná záměna
    tiše ubrala 17 % přepisu (23 634 → 28 617 znaků) a chyběla hned první věta.
    """
    bloky, blok = [], []
    text = open(path, encoding="utf-8", errors="replace").read()
    for radek in text.splitlines() + [""]:
        if radek.rstrip("\r") == "":
            if blok:
                bloky.append(blok)
            blok = []
        else:
            blok.append(radek)
    out = []
    for b in bloky:
        m, i = None, 0
        for i, r in enumerate(b):
            m = _TS.search(r)
            if m:
                break
        if not m:
            continue
        radky = [_TAG.sub("", x).strip() for x in b[i + 1:]]
        radky = [x for x in radky if x and not _ZVUK.match(x)]
        if not radky:
            continue
        out.append({"start": _sec(*m.group(1, 2, 3, 4)),
                    "end": _sec(*m.group(5, 6, 7, 8)), "lines": radky})
    return out


def _odval_rolovani(cues: list[dict]) -> list[dict]:
    """Rolující automatické titulky → jedna replika = jen NOVÝ text."""
    res = []
    for c in cues:
        if c["end"] - c["start"] < 0.05:      # usazovací replika
            continue
        t = c["lines"][-1].strip()            # nový je jen poslední řádek
        if not t or (res and t == res[-1]["text"]):
            continue
        res.append({"start": c["start"], "end": c["end"], "text": t})
    return res


def _slep(cues: list[dict], pauza=1.0, strop=220) -> list[dict]:
    """Útržky do frází.

    Automatický přepis nemá interpunkci a je rozsekaný po pár slovech
    („blood" / „never dried"). Překládat to po kusech dá v češtině nesmysl
    — slovosled se nemá o co opřít.

    ⚠️ Podle pauz to samo nestačí: mezery mezi útržky jsou dvouvrcholové
    (změřeno 27.8. — 90 % má 0,010 s, skutečných pauz bylo v celém pořadu
    jen 84, medián těch reálných přes 4 s). Uvnitř souvislé řeči tedy není
    kde lámat a musí se řezat podle délky. Šev uprostřed věty tolik nevadí:
    překlad běží po blocích, takže model vidí i sousední repliku.
    """
    out = []
    for c in cues:
        if out and c["start"] - out[-1]["end"] <= pauza and len(out[-1]["text"]) < strop:
            out[-1]["text"] += " " + c["text"]
            out[-1]["end"] = c["end"]
        else:
            out.append(dict(c))
    return out


_SLOVNI_CAS = re.compile(r"<\d\d:\d\d:\d\d\.\d\d\d>")


def je_rolujici(text: str, cues: list[dict]) -> bool:
    """Pozná automatický přepis PODLE OBSAHU, ne podle přepínače.

    ⚠️ NUTNÉ, a málem mi to uteklo: `--write-subs` (ruční titulky) vrátil
    u testovaného videa TÝŽ soubor jako `--write-auto-subs` (ověřeno 27.8.,
    440 B oba) — YouTube podsune automatické titulky i pod ručním přepínačem.
    Kdyby se rolující soubor prohlásil za ruční, odvalení by se přeskočilo
    a Hans by každou větu namluvil DVAKRÁT.

    Poznávací znamení má jen automatický přepis: časy jednotlivých slov
    v `<…>` tazích a hromada usazovacích replik kratších než 50 ms.
    Ruční titulky nemají ani jedno — dvouřádkový dialog tedy nespadne do
    odvalení a nepřijde o půlku textu.
    """
    if _SLOVNI_CAS.search(text):
        return True
    if not cues:
        return False
    kratke = sum(1 for c in cues if c["end"] - c["start"] < 0.05)
    return kratke >= max(3, len(cues) * 0.1)


def vtt_na_srt(vtt: str, srt: str, cfg=None, vnutit=None) -> tuple[int, bool]:
    """VTT → SRT. Vrátí (počet replik, bylo_to_rolující).

    O způsobu zpracování rozhoduje OBSAH souboru (`je_rolujici`); `vnutit`
    je jen pro testy, provoz ho nepoužívá.
    """
    text = open(vtt, encoding="utf-8", errors="replace").read()
    cues = parse_vtt(vtt)
    rol = je_rolujici(text, cues) if vnutit is None else vnutit
    if rol:
        c = _slep(_odval_rolovani(cues),
                  float((cfg or {}).get("merge_pause_s", 1.0)),
                  int((cfg or {}).get("merge_max_chars", 220)))
    else:
        c = [{"start": x["start"], "end": x["end"], "text": " ".join(x["lines"])}
             for x in cues]
    with open(srt, "w", encoding="utf-8") as f:
        for i, x in enumerate(c, 1):
            f.write(f"{i}\n{_ts(x['start'])} --> {_ts(x['end'])}\n{x['text']}\n\n")
    return len(c), rol


# ── ticho ve SKUTEČNÉM zvuku (HANS_YT_SILENCE_V1) ────────────────────────────
# Proč to vůbec je: časy titulků LŽOU o hustotě řeči. Rolující titulky na sebe
# navazují i tam, kde mluvčí mlčí — u testovaného videa hlásily pokrytí řečí
# 100 % (902 s z 901 s) a součet mezer 1 s. Zvuk téhož videa má ale 122 s ticha
# ve 202 úsecích. Bez téhle korekce nemá skládání stopy JEDINÉ místo, kde by
# srovnalo nabraný skluz, a ten se jen sčítá: naměřeno 41 s na čtvrthodinovém
# pořadu (`posunuto` 54 z 58 replik).
#
# ⛔ NESAHAT kvůli tomu na `srt_track.assemble` ani na `tts_engine` — jedou po
# nich hotové překlady dokumentů ze souborů, které fungují (pokyn uživatele
# 27.8.). YouTube má proto vlastní cestu, sdílí se až výsledné SRT.

def zjisti_ticho(cfg, pc_path: str, pc_fn, prah=-35, min_s=0.30) -> list[tuple]:
    """Úseky ticha v audiu → [(start, konec), …]. Běží na PC, kde leží video.

    ⚠️ `ffmpeg -v error` NEFUNGUJE: `silencedetect` i `volumedetect` hlásí na
    úrovni `info`, takže by se výpis zahodil a vyšla by NULA úseků — což
    vypadá jako „žádné ticho", ne jako rozbité měření. (Málem mě to 27.8.
    svedlo k závěru, že se tahle cesta nedá postavit.)
    """
    cmd = (f"ffmpeg -hide_banner -nostats -i {_q(pc_path)} -map 0:a:0 "
           f"-af silencedetect=noise={prah}dB:d={min_s} -f null - 2>&1")
    try:
        out = pc_fn(cmd, 1800)
    except Exception as e:
        log.warning("detekce ticha selhala, jedu bez ní: %s", e)
        return []
    ti, zac = [], None
    for radek in out.splitlines():
        m = re.search(r"silence_start: ([0-9.]+)", radek)
        if m:
            zac = float(m.group(1))
        m = re.search(r"silence_end: ([0-9.]+)", radek)
        if m and zac is not None:
            ti.append((zac, float(m.group(1))))
            zac = None
    log.info("ticho v originále: %d úseků, celkem %.0f s",
             len(ti), sum(e - z for z, e in ti))
    return ti


def _slep_s_tichem(cues, ticho, pauza=1.0, strop=220, min_pauza=0.35):
    """Jako `_slep`, ale šev se dělá i tam, kde v AUDIU je pauza.

    Tím dostane skládání stopy body, ve kterých smí srovnat skluz. Na
    testovaném videu: dnešní dělení má na švech 2 s ticha, tohle 37 s.
    Zbytek ticha leží uvnitř útržků, kde se lámat nedá — útržek je nejmenší
    jednotka, kterou rolující titulky dávají.
    """
    ti = [(z, e) for z, e in ticho if e - z >= min_pauza]

    def je_pauza(a, b):
        return any(z < b + 0.25 and e > a - 0.25 for z, e in ti)

    out = []
    for c in cues:
        if out:
            lom = (c["start"] - out[-1]["end"] > pauza
                   or len(out[-1]["text"]) >= strop
                   or je_pauza(out[-1]["end"], c["start"]))
            if not lom:
                out[-1]["text"] += " " + c["text"]
                out[-1]["end"] = c["end"]
                continue
        out.append(dict(c))
    return out


def _posun_na_konec_ticha(fraze, ticho):
    """Začíná-li fráze uprostřed ticha, posune se na okamžik, kdy mluvčí
    doopravdy začne. Předchozí fráze tím dostane víc místa a česká věta
    nezačne dřív než ta anglická."""
    for i, f in enumerate(fraze):
        for z, e in ticho:
            if z <= f["start"] < e:
                f["start"] = min(e, f["end"] - 0.3)
                break
        if i and f["start"] < fraze[i - 1]["start"]:
            f["start"] = fraze[i - 1]["start"]
    return fraze


def srt_s_tichem(vtt: str, srt: str, ticho: list, cfg=None) -> int:
    """VTT → SRT se švy zarovnanými na skutečné pauzy v audiu."""
    text = open(vtt, encoding="utf-8", errors="replace").read()
    cues = parse_vtt(vtt)
    if not je_rolujici(text, cues):
        # ruční titulky mají vlastní rozumné dělení, do toho se nevrtá
        return vtt_na_srt(vtt, srt, cfg=cfg)[0]
    c = _slep_s_tichem(_odval_rolovani(cues), ticho,
                       float((cfg or {}).get("merge_pause_s", 1.0)),
                       int((cfg or {}).get("merge_max_chars", 220)),
                       float((cfg or {}).get("min_pauza_s", 0.35)))
    c = _posun_na_konec_ticha(c, ticho)
    with open(srt, "w", encoding="utf-8") as f:
        for i, x in enumerate(c, 1):
            f.write(f"{i}\n{_ts(x['start'])} --> {_ts(x['end'])}\n{x['text']}\n\n")
    return len(c)


# ── rozpočet času (HANS_YT_TIMEBUDGET_V1) ────────────────────────────────────
# Nápad uživatele 27.8.: „ještě můžeme trošku zpomalit video." Není to ozdoba,
# je to NUTNOST — a čísla to ukázala teprve po změření skutečného tempa hlasu.
#
# Změřeno na patnáctiminutovém pořadu:
#   český text 13 985 znaků · Vlasta mluví 13,15 zn/s (⚠️ NE 14, jak jsem
#   odhadoval) → přirozeně 1 063 s řeči na 901 s videa = potřeba +18 %.
#   Strop stlačení řeči je 15 % (FLOOR 0.85, níž to drhne) → SAMOTNÁ řeč to
#   nedožene. Zpomalení obrazu o 6 % dá 955 s, na řeč pak zbyde 11 % = vejde se.
#
# Proč tohle a ne jemnější dělení: ⛔ přesazení švů na pauzy ve zvuku je
# VYZKOUŠENÉ A ZAMÍTNUTÉ (posun 41 s → 68 s) — ticho není čas navíc, titulky
# pokrývají celou osu. Čas se dá jen VYROBIT (delší video), ne přeskládat.

def zmer_tempo_hlasu(cues, cfg, synth_fn, vzorku=10) -> float:
    """Znaků za sekundu při přirozeném tempu. Měří se na vzorku NAPŘÍČ pořadem,
    ne od začátku — tempo se liší replika od repliky (naměřeno 11,3–14,5)."""
    import tempfile
    vz = [cues[i] for i in range(0, len(cues), max(1, len(cues) // vzorku))][:vzorku]
    wd = tempfile.mkdtemp(prefix="hans_tempo_")
    zn, sek = 0, 0.0
    for i, c in enumerate(vz):
        p = os.path.join(wd, f"{i}.wav")
        try:
            if synth_fn(c["text"], p):
                from scripts import srt_track as _st
                d = _st.duration(p)
                if d > 0.2:
                    zn += len(c["text"]); sek += d
        except Exception as e:
            log.warning("vzorek tempa selhal: %s", e)
    import shutil as _sh
    _sh.rmtree(wd, ignore_errors=True)
    return (zn / sek) if sek > 1.0 else 0.0


def rozpocet(cz_cues, delka_videa: float, cfg=None, tempo=None) -> dict:
    """Kolik zpomalit obraz, aby se česká stopa vešla.

    Zbytek schodku doběhne stávající stlačování řeči v `tts_engine`, které tu
    zůstává jako pojistka — odhad ze znaků má rozptyl (tempo replik 11,3–14,5).
    """
    c = cfg or {}
    rychlost = float(tempo or c.get("rate_chars_s", 13.15)) or 13.15
    znaku = sum(len(x["text"]) for x in cz_cues)
    potreba = znaku / rychlost
    f_nutne = potreba / max(1.0, delka_videa)
    cil_reci = float(c.get("speech_speedup_target", 1.10))
    strop = float(c.get("max_video_stretch", 1.12))
    f_video = min(max(f_nutne / cil_reci, 1.0), strop)
    rec = f_nutne / f_video

    # Meze podle toho, kdy uz to drhne (uzivatelem overeny FLOOR 0.85 = ~18 %).
    # ⚠️ Kdyz se to nevejde, MUSI to Hans RICT — dnes by tise vyrobil soubor,
    # ktery se ke konci rozejde o minutu. Zmereno 27.8. na peti ruznych poradech:
    # f_nutne kolisa 0,85–1,47 (prednaska ma 15 % casu NAVIC, zpravodajstvi by
    # potrebovalo rec o 39 % rychlejsi) — jedno nastaveni tedy vsechno nepokryje
    # a rozdil se pozna az z textu konkretniho videa.
    ok = float(c.get("rec_ok", 1.18))
    varovat = float(c.get("rec_warn", 1.30))
    if rec <= ok:
        verdikt, hlaska = "ok", ""
    elif rec <= varovat:
        verdikt = "upozornit"
        hlaska = ("Mluví se tam rychle, pane — česká stopa bude znít trochu "
                  "uspěchaně (o %d %% rychleji než obvykle)." % round((rec - 1) * 100))
    else:
        verdikt = "odmitnout"
        hlaska = ("Tenhle pořad se do češtiny v původní délce nevejde, pane. "
                  "Mluví se v něm %.1f znaku za vteřinu a česká stopa by musela "
                  "běžet o %d %% rychleji, což už by nešlo poslouchat. "
                  "Kdybyste chtěl, mohu udělat aspoň titulky."
                  % (znaku / max(1.0, delka_videa), round((rec - 1) * 100)))
    return {"znaku": znaku, "tempo": round(rychlost, 2),
            "potreba_s": round(potreba), "video_s": round(delka_videa),
            "f_nutne": round(f_nutne, 3), "f_video": round(f_video, 4),
            "zbyva_na_rec": round(rec, 3), "zaklad_pct": max(0, round((rec - 1) * 100)),
            "verdikt": verdikt, "hlaska": hlaska}


def preskaluj_srt(src: str, dst: str, f: float) -> int:
    """Časy titulků × f — na zpomalený obraz. Text se nemění."""
    from scripts import srt_track as _st
    cues = _st.load_cues(src)
    with open(dst, "w", encoding="utf-8") as fh:
        for i, c in enumerate(cues, 1):
            fh.write(f"{i}\n{_ts(c['start'] * f)} --> {_ts(c['end'] * f)}\n{c['text']}\n\n")
    return len(cues)


def namluv_s_tempem(srt_path: str, out_wav: str, config: dict, zaklad_pct: int) -> dict:
    """Namluví stopu tak, že se JIŽ PRVNÍ průchod dělá zrychleně o `zaklad_pct`.

    Proč vlastní funkce a ne úprava `tts_engine`: to stlačuje REAKTIVNĚ —
    přirozeným tempem, a zrychlí až repliku, která přetekla svůj slot, nejvýš
    o 15 %. Replika, které by 15 % nestačilo, zbytek předá dál, a repliky,
    které se vejdou, žádnou rezervu nevytvoří → skluz jde jen jedním směrem.
    Změřeno: 67,8 s posunu; po pouhém roztažení osy pořád 40,8 s.
    Tady se proto zrychlí VŠECHNY repliky rovnou a stávající dorovnání
    přetečených zůstává jen jako pojistka na zbytek.

    ⛔ `tts_engine.synth_srt` se ZÁMĚRNĚ nemění — jedou po něm hotové překlady
    dokumentů ze souborů, kterým reaktivní chování vyhovuje (pokyn uživatele
    27.8.). Sdílí se jen `_edge_one` a skládání stopy.
    """
    import tempfile
    from concurrent.futures import ThreadPoolExecutor
    from scripts import srt_track as st, tts_engine

    cfg = (config or {}).get("translate", {}) or {}
    hlas = cfg.get("edge_voice", "cs-CZ-VlastaNeural")
    cues = st.load_cues(srt_path)
    if not cues:
        raise ValueError(f"v {srt_path} nejsou žádné titulky")
    wd = tempfile.mkdtemp(prefix="hans_yt_tts_")
    try:
        paths = [os.path.join(wd, f"{i:05d}.wav") for i in range(len(cues))]
        zaklad = max(0, min(int(tts_engine.EDGE_MAX_RATE), int(zaklad_pct)))
        with ThreadPoolExecutor(max_workers=4) as ex:
            list(ex.map(tts_engine._edge_one,
                        [(c["text"], hlas, zaklad, p) for c, p in zip(cues, paths)]))
        hotovo = [p for p in paths if os.path.exists(p)]
        if len(hotovo) < len(cues) * 0.9:
            # ⚠️ Bez tohohle by výpadek cloudu shodil CELÝ youtubový dabing,
            # zatímco dokumentová cesta má zálohu v Piperu. Rovnoměrné tempo
            # se tím ztratí (Piper si rychlost neřídí), ale horší hlas je pořád
            # lepší než žádný soubor — a stopa se stejně skládá na zpomalenou
            # osu, takže část schodku už je vyřešená.
            log.warning("edge-tts vyrobil jen %d/%d replik — beru záložní motor",
                        len(hotovo), len(cues))
            from scripts import tts_engine as _te
            h = _te.synth_srt(srt_path, out_wav, config)
            h["zaklad_pct"] = 0
            h["dorovnano"] = 0
            return h

        # pojistka: co i po základním zrychlení přeteče, dorovnat
        sl = st.slots(cues)
        znovu = []
        for i, p in enumerate(paths):
            if not os.path.exists(p):
                continue
            d = st.duration(p)
            if d > sl[i]:
                scale = st.fit_scale(sl[i], d, tts_engine.FLOOR)
                extra = int(round((1.0 / scale - 1.0) * 100))
                r = min(int(tts_engine.EDGE_MAX_RATE), zaklad + max(0, extra))
                if r > zaklad:
                    znovu.append((cues[i]["text"], hlas, r, p))
        if znovu:
            with ThreadPoolExecutor(max_workers=4) as ex:
                list(ex.map(tts_engine._edge_one, znovu))

        stats = st.assemble(cues, lambda i: paths[i] if os.path.exists(paths[i]) else None,
                            out_wav)
        log.info("YT hlas: %d replik, základ +%d %%, dorovnáno %d, %s",
                 len(hotovo), zaklad, len(znovu), stats)
        return {"engine": "edge", "stats": stats, "zaklad_pct": zaklad,
                "dorovnano": len(znovu)}
    finally:
        import shutil as _sh
        _sh.rmtree(wd, ignore_errors=True)


# ── stažení ──────────────────────────────────────────────────────────────────
def _najdi(wd: str, *konce: str) -> str | None:
    for f in sorted(os.listdir(wd)):
        if any(f.endswith(k) for k in konce):
            return os.path.join(wd, f)
    return None


def stahni(config, vid: str, wd: str, say=None) -> dict:
    """Stáhne video a titulky NA PI. Vrátí cesty; nahrání na PC dělá volající.

    → {'video','titul','srt'|None,'jazyk','zdroj','url'}
    """
    cfg = ((config or {}).get("translate", {}) or {})
    yt = cfg.get("youtube", {}) or {}
    url = f"https://www.youtube.com/watch?v={vid}"
    vyska = int(yt.get("max_height", 720))
    limit = int(yt.get("timeout_s", 3600))
    hlas = say or (lambda *_a: None)

    # 1) video + RUČNÍ titulky + metadata jedním průchodem
    hlas("stahuji z YouTube")
    _yt(cfg, ["-f", f"bv*[height<={vyska}]+ba/b[height<={vyska}]",
              "--merge-output-format", "mp4", "--no-playlist",
              "--write-subs", "--sub-langs", "cs,en", "--sub-format", "vtt",
              "--write-info-json", "--no-warnings",
              "-o", os.path.join(wd, "yt.%(ext)s"), url], limit)

    video = _najdi(wd, ".mp4", ".mkv", ".webm")
    if not video:
        raise RuntimeError("yt-dlp doběhl, ale video na disku není")

    titul = vid
    info = _najdi(wd, ".info.json")
    if info:
        try:
            titul = (json.load(open(info, encoding="utf-8")).get("title") or vid).strip()
        except Exception as e:
            log.warning("název videa se nepodařilo přečíst: %s", e)

    # 2) zdroj textu. Ruční titulky mají PŘEDNOST (interpunkce, správné dělení),
    #    a čeština před angličtinou — česká přeskočí celý překlad.
    for jazyk in ("cs", "en"):
        v = _najdi(wd, f".{jazyk}.vtt")
        if v:
            s = os.path.join(wd, f"yt_{jazyk}.srt")
            n, rol = vtt_na_srt(v, s, cfg=yt)
            if n > 0:
                # Štítek popisuje TVAR, ne původ. Že jsou titulky „ruční",
                # se dokázat nedá — YouTube podstrčí týž soubor i pod ručním
                # přepínačem (ověřeno 27.8.) — tak to Hans netvrdí.
                zdroj = (f"automatický přepis YouTube ({jazyk})" if rol
                         else f"titulky z YouTube ({jazyk})")
                log.info("%s: %d replik", zdroj, n)
                return {"video": video, "titul": titul, "srt": s, "jazyk": jazyk,
                        "zdroj": zdroj, "url": url, "vtt": v, "rolujici": rol}

    # 3) až teprve automatické (jen anglické — česká automatická je strojový
    #    překlad strojového přepisu, vlastní model to přeloží líp)
    hlas("stahuji automatické titulky")
    auto = os.path.join(wd, "auto")
    os.makedirs(auto, exist_ok=True)
    try:
        _yt(cfg, ["--skip-download", "--write-auto-subs", "--sub-langs", "en",
                  "--sub-format", "vtt", "--no-playlist", "--no-warnings",
                  "-o", os.path.join(auto, "yt.%(ext)s"), url], 600)
    except Exception as e:
        log.warning("automatické titulky se nestáhly: %s", e)
    v = _najdi(auto, ".en.vtt")
    if v:
        s = os.path.join(wd, "yt_auto.srt")
        n, _rol = vtt_na_srt(v, s, cfg=yt)
        if n > 0:
            log.info("automatické titulky YouTube: %d frází", n)
            return {"video": video, "titul": titul, "srt": s, "jazyk": "en",
                    "zdroj": "automatický přepis YouTube (en)", "url": url,
                    "vtt": v, "rolujici": True}

    # 4) titulky nejsou — dál se použije přepis ze zvuku (Whisper na PC)
    return {"video": video, "titul": titul, "srt": None, "jazyk": None,
            "zdroj": None, "url": url, "vtt": None, "rolujici": False}


_NEPOVOLENE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def nazev_souboru(titul: str, vid: str) -> str:
    """Název videa → bezpečné jméno souboru. Id se přidává, aby se dvě videa
    se stejným titulkem nepřepsala."""
    t = _NEPOVOLENE.sub("", titul or "").strip().rstrip(".")
    t = re.sub(r"\s+", " ", t)[:110].strip()
    return f"{t or 'youtube'} [{vid}] [CZ].mkv"
