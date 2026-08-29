"""HANS_TRANSLATE_V1 — česká zvuková stopa k cizojazyčnému dokumentu.

Zadání uživatele (26.8.): pustí na Kodi dokument, zjistí, že není česky,
pauzne ho a řekne Hansovi, ať ho přeloží. Hans si z Kodi zjistí, o který
soubor jde, připraví stopu a ozve se, že je hotovo. Soubor si uživatel
pustí sám.

Kde co běží: soubory leží na NASu, který je namountovaný NA PC (`/mnt/F/...`),
ne na Pi. Všechno, co sahá na video, se proto dělá na PC přes SSH; Pi řídí,
stahuje titulky (má přihlášení) a vyrábí zvuk (má edge-tts).

Pořadí zdrojů textu (změřeno 26.8.): české titulky = hotovo bez překladu ·
anglické titulky = překlad z čistého textu · STT = poslední možnost, protože
komolí vlastní jména („Jim Al-Khalili" → „Jim Alkelele") a chyba se protáhne
do překladu i do hlasu.
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import tempfile
import time
import urllib.request

from scripts import hans_youtube as hy
from scripts import srt_track as st
from scripts import tts_engine
from scripts.hans_subtitles import OpenSubtitles

log = logging.getLogger(__name__)

_CZ = re.compile(r"[řěůščžýáíéúťďň]", re.I)


def _cfg(config):
    return (config or {}).get("translate", {}) or {}


# ── PC přes SSH ──────────────────────────────────────────────────────────────
def _ssh_base(cfg):
    return ["ssh", "-i", os.path.expanduser(cfg.get("pc_key", "~/.ssh/hans_pc")),
            "-o", "ConnectTimeout=10", "-o", "StrictHostKeyChecking=no",
            f"{cfg.get('pc_user','user')}@{cfg.get('pc_host','192.168.1.10')}"]


def _pc(cfg, cmd: str, timeout=600) -> str:
    r = subprocess.run(_ssh_base(cfg) + [cmd], capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"PC: {cmd[:60]}… selhalo: {r.stderr[-300:]}")
    return r.stdout


def _pc_put(cfg, local, remote, timeout=None):
    subprocess.run(["scp", "-q", "-i", os.path.expanduser(cfg.get("pc_key", "~/.ssh/hans_pc")),
                    local, f"{cfg.get('pc_user','user')}@{cfg.get('pc_host','192.168.1.10')}:{remote}"],
                   check=True, capture_output=True, timeout=timeout)


def _pc_get(cfg, remote, local):
    subprocess.run(["scp", "-q", "-i", os.path.expanduser(cfg.get("pc_key", "~/.ssh/hans_pc")),
                    f"{cfg.get('pc_user','user')}@{cfg.get('pc_host','192.168.1.10')}:{remote}", local],
                   check=True, capture_output=True)


def _q(p: str) -> str:
    return "'" + p.replace("'", "'\\''") + "'"


# ── co hraje ─────────────────────────────────────────────────────────────────
def kodi_now_file(config) -> dict:
    """Vrátí {'ok':True,'pc_path':…,'title':…} nebo {'ok':False,'duvod':…}."""
    from scripts.kodi_client import KodiClient
    cfg = _cfg(config)
    try:
        np = KodiClient(config).get_now_playing()
    except Exception as e:
        return {"ok": False, "duvod": f"Kodi neodpovídá: {e}"}
    if not np:
        return {"ok": False, "duvod": "V Kodi teď nic neběží."}
    f = np.get("file")
    if not f:
        # živé vysílání nebo stream — nemá soubor, není co překládat
        return {"ok": False, "duvod": f"„{np.get('label') or np.get('title')}“ nemá soubor "
                                      "(nejspíš živé vysílání) — to přeložit nejde."}
    # HANS_YT_TRANSLATE_V1: YouTube hraje Kodi přes plugin, takže `file` není
    # cesta k souboru, ale `plugin://plugin.video.youtube/...`. Musí se to
    # poznat TADY — jinak to propadne níž a Hans ohlásí nesmysl o úložišti.
    vid = hy.video_id(f)
    if vid:
        return {"ok": True, "pc_path": None, "yt_id": vid,
                "title": np.get("title") or np.get("label") or "video z YouTube"}
    smb = cfg.get("smb_prefix", "smb://192.168.1.10/")
    if not f.startswith(smb):
        return {"ok": False, "duvod": f"Soubor {f} není na známém úložišti."}
    pc = _najdi_na_pc(cfg, f[len(smb):])
    if not pc:
        share = f[len(smb):].split("/", 1)[0]
        return {"ok": False,
                "duvod": f"„{np.get('label') or np.get('title')}“ leží na sdílení "
                         f"„{share}“, které PC nemá namontované — nedostanu se k souboru."}
    return {"ok": True, "pc_path": pc,
            "title": np.get("showtitle") or np.get("title") or np.get("label"),
            "season": np.get("season"), "episode": np.get("episode")}


def _mounty(cfg) -> list[str]:
    try:
        return [x.strip() for x in _pc(cfg, "ls /mnt", 60).split() if x.strip()]
    except Exception as e:
        log.warning("seznam mountů na PC se nepodařilo přečíst: %s", e)
        return []


def _najdi_na_pc(cfg, zbytek: str) -> str | None:
    """Cesta z Kodi → skutečná cesta na PC.

    ⚠️ Prostý převod prefixu NESTAČÍ. NAS může vystavovat víc sdílení, než má
    PC namontováno, a část z nich bývají ALIASY na složku uvnitř disku:
        <slozka><disk>   → /mnt/<disk>/<slozka>     (bez mezery)
        <slozka> <disk>  → /mnt/<disk>/<slozka>     (s mezerou)
        <nazev s mezerou> → /mnt/<nazev_s_podtrzitky>
        <slozka>         → písmeno disku v názvu nemá, hledá se na všech
    Doloženo 26.8. reálným pádem: sdílení bylo alias a `/mnt/<sdileni>/…`
    neexistovalo — a chyba vyskočila až o tři kroky dál z ffprobe.
    Kandidáti se proto OVĚŘUJÍ na PC a bere se první, který existuje.
    (CIFS je case-insensitive, takže na velikosti písmen nezáleží.)
    """
    zbytek = zbytek.lstrip("/")
    if "/" not in zbytek:
        return None
    share, rest = zbytek.split("/", 1)
    mounty = _mounty(cfg)
    disky = [m for m in mounty if len(m) == 1]

    kand = [f"/mnt/{share}/{rest}", f"/mnt/{share.replace(' ', '_')}/{rest}"]
    m = re.match(r"^(.*?)[ _]?([A-Za-z])$", share)      # alias „<slozka><disk>"
    if m and m.group(2).upper() in [d.upper() for d in disky] and m.group(1).strip():
        kand.append(f"/mnt/{m.group(2).upper()}/{m.group(1).strip()}/{rest}")
    for d in mounty:                                    # „hry", „Pohadky" — bez písmene
        kand.append(f"/mnt/{d}/{share}/{rest}")

    videno, unik = set(), []
    for k in kand:
        if k not in videno:
            videno.add(k); unik.append(k)

    # HANS_MOUNT_RETRY_V1 (28.8.) — sdílení nemusí být po ruce HNED.
    # Doloženo: PC nabootovalo v 08:32, systemd zkusil navázat šest sdílení
    # naráz a Windows je odmítly (STATUS_REQUEST_NOT_ACCEPTED). Za pár minut
    # už přihlášení procházelo, ale `x-systemd.automount` mountuje až PŘI
    # PŘÍSTUPU — takže adresáře zůstaly prázdné, dokud do nich někdo nesáhl,
    # a Hans mezitím hlásil, že sdílení není namontované. Jeden pokus navíc
    # s prodlevou tuhle třídu výpadků pokryje: samotný `test -f` automount
    # spustí, jen mu chvíli trvá, než sezení naváže.
    pokusy = max(1, int(cfg.get("mount_retry", 3)))
    prodleva = float(cfg.get("mount_retry_delay_s", 4.0))
    skript = "; ".join(f"test -f {_q(k)} && {{ echo {_q(k)}; exit 0; }}" for k in unik)
    for pokus in range(pokusy):
        try:
            hit = _pc(cfg, skript + "; exit 0", 120).strip().splitlines()
            if hit:
                if pokus:
                    log.info("soubor se našel až na %d. pokus (mount naskočil se zpožděním)",
                             pokus + 1)
                return hit[0]
        except Exception as e:
            log.warning("hledání souboru na PC selhalo (pokus %d/%d): %s",
                        pokus + 1, pokusy, e)
        if pokus < pokusy - 1:
            _probud_mounty(cfg, unik)
            time.sleep(prodleva)
    return None


def _probud_mounty(cfg, cesty) -> None:
    """Sáhne na přípojné body, aby se rozjel automount.

    `test -f` na hlubokou cestu automount spustí taky, ale když se mount
    nestihne, vrátí rovnou false. Tohle na kořeny sáhne zvlášť a chybu spolkne
    — jde jen o to dát systemd podnět, ne o výsledek."""
    koreny = sorted({"/".join(c.split("/")[:3]) for c in cesty if c.startswith("/mnt/")})
    if not koreny:
        return
    try:
        _pc(cfg, "; ".join(f"ls {_q(k)} >/dev/null 2>&1" for k in koreny) + "; exit 0", 90)
    except Exception as e:
        log.debug("probuzení mountů selhalo, jedu dál: %s", e)


def has_czech_audio(cfg, pc_path) -> bool:
    out = _pc(cfg, f"ffprobe -v error -select_streams a "
                   f"-show_entries stream_tags=language -of csv=p=0 {_q(pc_path)}", 120)
    return any(x.strip().lower() in ("ces", "cze", "cs") for x in out.splitlines())


# ── zdroj textu ──────────────────────────────────────────────────────────────
def _is_czech(path) -> bool:
    txt = st.read_text(path)[0][:4000]
    return len(_CZ.findall(txt)) > 20


def _pc_hash(cfg, pc_path) -> str:
    code = ("import struct,sys;p=sys.argv[1];bs=65536;import os;"
            "sz=os.path.getsize(p);h=sz;f=open(p,'rb');"
            "\nfor _ in range(bs//8): h=(h+struct.unpack('<q',f.read(8))[0])&0xFFFFFFFFFFFFFFFF"
            "\nf.seek(max(0,sz-bs),0)"
            "\nfor _ in range(bs//8): h=(h+struct.unpack('<q',f.read(8))[0])&0xFFFFFFFFFFFFFFFF"
            "\nprint('%016x'%h)")
    return _pc(cfg, f"python3 -c {_q(code)} {_q(pc_path)}", 180).strip()


def _stt(cfg, config, pc_path, workdir) -> str:
    """Poslední možnost. Whisper běží na PC, zvuk se tam i extrahuje."""
    v = (config or {}).get("voice", {}) or {}
    url, tok = v.get("stt_url"), v.get("stt_token")
    if not url:
        raise RuntimeError("STT není nastaveno (voice.stt_url)")
    _pc(cfg, f"ffmpeg -v error -y -i {_q(pc_path)} -ac 1 -ar 16000 /tmp/hans_stt.wav", 900)
    hdr = f"-H 'Authorization: Bearer {tok}' " if tok else ""
    out = _pc(cfg, f"curl -s -X POST {url} {hdr}"
                   "-F 'file=@/tmp/hans_stt.wav;type=audio/wav;filename=speech.wav' "
                   "-F 'model=whisper-1'", 3600)
    text = (json.loads(out) or {}).get("text", "").strip()
    if not text:
        raise RuntimeError("STT nevrátil text")
    # STT dá souvislý text bez časů → rozsekat na věty a rozprostřít po délce
    dur = float(_pc(cfg, f"ffprobe -v error -show_entries format=duration -of csv=p=0 "
                         f"{_q(pc_path)}", 120).strip())
    vety = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]
    krok = dur / max(1, len(vety))
    srt = os.path.join(workdir, "stt.srt")
    with open(srt, "w", encoding="utf-8") as f:
        for i, v_ in enumerate(vety, 1):
            a, b = (i - 1) * krok, i * krok
            f.write(f"{i}\n{_ts(a)} --> {_ts(b)}\n{v_}\n\n")
    return srt


def _ts(x: float) -> str:
    h, m, s = int(x // 3600), int(x % 3600 // 60), x % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}".replace(".", ",")


# ⚠️ Jen TEXTOVÉ titulky. Obrazové (PGS/VobSub z Blu-ray a DVD) jsou obrázky —
# do SRT je ffmpeg nepřevede a bez OCR z nich text nedostaneme.
_TEXT_SUB = {"subrip", "srt", "ass", "ssa", "mov_text", "webvtt", "text"}


def _vnorene_titulky(cfg, pc_path, jazyk, workdir) -> str | None:
    """Titulky uvnitř kontejneru. Zadarmo a psané přímo k tomuhle souboru,
    takže mají přednost před stahováním i před přepisem ze zvuku."""
    try:
        out = _pc(cfg, "ffprobe -v error -select_streams s -show_entries "
                       "stream=index,codec_name:stream_tags=language "
                       f"-of csv=p=0 {_q(pc_path)}", 180)
    except Exception as e:
        log.warning("vnořené titulky se nepodařilo vypsat: %s", e)
        return None
    kod = {"cs": ("cze", "ces", "cs"), "en": ("eng", "en")}[jazyk]
    for radek in out.splitlines():
        c = [x.strip() for x in radek.split(",")]
        if len(c) < 3 or c[1].lower() not in _TEXT_SUB:
            continue
        if c[2].lower() not in kod:
            continue
        loc = os.path.join(workdir, f"vnorene_{jazyk}.srt")
        try:
            _pc(cfg, f"ffmpeg -v error -y -i {_q(pc_path)} -map 0:{int(c[0])} "
                     f"-c:s srt /tmp/hans_vnorene.srt", 900)
            _pc_get(cfg, "/tmp/hans_vnorene.srt", loc)
        except Exception as e:
            log.warning("vytažení vnořených titulků (%s) selhalo: %s", jazyk, e)
            return None
        if os.path.getsize(loc) > 200:
            return loc
        log.warning("vnořené titulky (%s) vyšly prázdné", jazyk)
        return None
    return None


def obstarej_titulky(config, pc_path, meta, workdir) -> dict:
    """→ {'srt':cesta, 'jazyk':'cs'|'en', 'zdroj':…}"""
    cfg = _cfg(config)
    # 1) soubor vedle videa
    vedle = os.path.splitext(pc_path)[0] + ".srt"
    if _pc(cfg, f"test -f {_q(vedle)} && echo ANO || echo NE", 60).strip() == "ANO":
        loc = os.path.join(workdir, "vedle.srt")
        _pc_get(cfg, vedle, loc)
        jazyk = "cs" if _is_czech(loc) else "en"
        return {"srt": loc, "jazyk": jazyk, "zdroj": "titulky vedle souboru"}
    # 2) vnořené titulky v kontejneru + 3) OpenSubtitles.
    #    ČEŠTINA MÁ PŘEDNOST PŘED ANGLIČTINOU z obou zdrojů — česká stopa
    #    přeskočí celý překlad. V rámci jazyka jde levnější zdroj první:
    #    vnořené jsou zadarmo, stažení ukrajuje z denního limitu (20/den).
    osub = OpenSubtitles(config)
    otisk = None
    for jazyk in ("cs", "en"):
        loc = _vnorene_titulky(cfg, pc_path, jazyk, workdir)
        if loc:
            return {"srt": loc, "jazyk": jazyk, "zdroj": f"vnořené titulky ({jazyk})"}
        if osub.enabled:
            try:
                if otisk is None:
                    otisk = _pc_hash(cfg, pc_path)
                hits = osub.by_hash(otisk, jazyk)
                fid = OpenSubtitles.file_id(hits[0]) if hits else None
                if fid:
                    loc = os.path.join(workdir, f"os_{jazyk}.srt")
                    osub.download(fid, loc)
                    return {"srt": loc, "jazyk": jazyk,
                            "zdroj": f"OpenSubtitles ({jazyk}, shoda otisku)"}
            except Exception as e:
                log.warning("OpenSubtitles (%s) selhalo, jdu dál: %s", jazyk, e)
    # 4) přepis ze zvuku
    return {"srt": _stt(cfg, config, pc_path, workdir), "jazyk": "en", "zdroj": "přepis ze zvuku"}


# ── notičky pro neslyšící (HANS_SUB_CLEAN_V1) ────────────────────────────────
# Nález uživatele 27.8.: „v titulkách jsou notičky pro hluché, jako hudba".
# Hans je četl nahlas — uprostřed dokumentu tedy lektor prohlásil „muž".
#
# ZMĚŘENO na reálných titulcích uživatele: značí se, KDO mluví nebo co je
# slyšet, kulatou závorkou ve dvou pozicích — celý řádek („(zpěv)",
# „(žena, šeptem)") nebo začátek řádku, za kterým následuje řeč („(muž) …",
# „(chlapec) …"). Anglické automatické titulky mají totéž hranatě:
# [Music] 185×, [Applause] 14×, [Laughter] 6×.
#
# ⚠️ ROZLIŠUJE SE POZICÍ, NE VÝZNAMEM. Uprostřed věty jsou závorky běžný text
# („(USA)", „(však víte)") a sahat se na ně NESMÍ — plošné mazání závorek by
# ubíralo obsah. Proto jen celý řádek a začátek řádku.
_HRANATA = re.compile(r"\[[^\]]{0,40}\]")     # [music], [Applause], [Officer] — vždy popis
_SIPKY = re.compile(r"&gt;&gt;|>>")             # značka střídání mluvčího v CC


def _unescape(t: str) -> str:
    """HTML entity → znaky. Bez toho jde do namlouvání doslovné `&gt;&gt;`."""
    import html as _h
    return _h.unescape(t or "")


def _ocisti_repliku(t: str) -> str:
    """HANS_SUB_CLEAN_V3 (29.8.) — marker zvuku a střídání mluvčího ven.

    Vytaženo ze `_bez_noticek`, protože totéž musí proběhnout i PO překladu:
    model si `[hudba] >>` vyrobí sám i z čistého vstupu (změřeno 29.8. na
    replikách s `[music]` — prosáklo 4 z 5, jedna doslova `>> [hudba] >>`).
    Jedna funkce schválně: dvě kopie téhož pravidla by se časem rozešly.
    """
    t = _unescape(t or "")
    t = _HRANATA.sub(" ", t)
    t = _SIPKY.sub(" ", t)
    return " ".join(t.split()).strip(" -–—")


_NOTICKA_CELA = re.compile(r"^[\(\[][^\)\]]{0,40}[\)\]]$")
_NOTICKA_ZACATEK = re.compile(r"^[\(\[][^\)\]]{0,40}[\)\]]\s*")
_KREDIT = re.compile(r"^(titulky|p[řr]eklad|translated|subtitles|korekce|"
                     r"[čc]asov[áa]n[íi]|sync)\b\s*[:\-]", re.I)


def _bez_noticek(src: str, dst: str) -> dict:
    """Očistí ZDROJOVÉ titulky. Běží před překladem, takže se notičky ani
    nepřekládají, ani nenamlouvají — a platí to pro OBĚ cesty (soubor i YouTube),
    protože se to volá v místě, kde se obě potkávají."""
    cues = st.load_cues(src)
    if not cues:
        return {"zmeneno": False, "zahozeno": 0, "ocisteno": 0}
    ven, zahozeno, ocisteno, upraveno = [], 0, 0, 0
    for c in cues:
        t = (c["text"] or "").strip()
        if not t:
            continue
        # HANS_SUB_CLEAN_V2 (28.8.) — nález uživatele po poslechu: „hudba" se
        # pořád ozývala. Důvod: `[music]` nesedí na vlastním řádku, ale UPROSTŘED
        # věty mezi značkami střídání mluvčích:
        #     …in a truly original fashion. &gt;&gt; [music] &gt;&gt; I…
        # V1 řešila jen celý řádek a začátek řádku → 24 replik z 86 prošlo.
        # ⚠️ Hranaté závorky se mažou KDEKOLI, kulaté NE. Není to nedůslednost:
        # ověřeno na 25 115 řádcích uživatelovy knihovny — hranatá závorka
        # uprostřed věty se tam nevyskytuje ANI JEDNOU (vždy je to zvuk nebo
        # jmenovka mluvčího), kdežto kulatá ano 18× a je to běžný text.
        _puv = t
        t = _ocisti_repliku(t)          # „>>" = střídání mluvčího, ne řeč
        if t != _puv:
            # ⚠️ MUSÍ se počítat: `zmeneno` rozhoduje, jestli volající vyčištěný
            # soubor vůbec použije. Bez tohohle se úklid udělal a zahodil.
            upraveno += 1
        if not t:
            zahozeno += 1
            continue
        if _NOTICKA_CELA.match(t) or _KREDIT.match(t):
            zahozeno += 1
            continue
        nove = _NOTICKA_ZACATEK.sub("", t, count=1).strip()
        if nove != t:
            if not nove:
                zahozeno += 1
                continue
            ocisteno += 1
            t = nove
        ven.append({"start": c["start"], "end": c["end"], "text": t})
    # Pojistka: kdyby vzor na neobvyklém souboru zdivočel, radši nechat
    # původní titulky být, než vyrobit stopu s dírami.
    if not ven or zahozeno > len(cues) * 0.30:
        log.warning("čištění notiček by zahodilo %d z %d replik — nechávám "
                    "titulky beze změny", zahozeno, len(cues))
        return {"zmeneno": False, "zahozeno": 0, "ocisteno": 0}
    with open(dst, "w", encoding="utf-8") as f:
        for i, c in enumerate(ven, 1):
            f.write(f"{i}\n{_ts(c['start'])} --> {_ts(c['end'])}\n{c['text']}\n\n")
    return {"zmeneno": bool(zahozeno or ocisteno or upraveno),
            "zahozeno": zahozeno, "ocisteno": ocisteno,
            "upraveno": upraveno, "replik": len(ven)}


# ── překlad ──────────────────────────────────────────────────────────────────
_HEAD = ("You are a professional English (en) to Czech (cs) translator. "
         "Produce only the Czech translation, without any additional explanations "
         "or commentary. The input is numbered subtitle lines. Return EXACTLY the "
         "same number of lines, each starting with its original number and a pipe, "
         "like: 1|text")


def _ollama(config, cfg, prompt, npred=3000) -> str:
    host = (config.get("ollama", {}) or {}).get("host") or f"http://{cfg.get('pc_host')}:11434"
    body = {"model": cfg.get("model", "translategemma:12b"), "prompt": prompt,
            "stream": False, "keep_alive": 300,
            # num_gpu je KLÍČOVÝ: bez něj si Ollama rozvrhne vrstvy na CPU (6× pomalejší)
            "options": {"temperature": 0.2, "num_ctx": 8192, "num_predict": npred,
                        "num_gpu": int(cfg.get("num_gpu", 99))}}
    r = urllib.request.urlopen(urllib.request.Request(
        host.rstrip("/") + "/api/generate", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}),
        # HANS_TRANSLATE_TIMEOUT_V1 (28.8.) — 900 s bylo na blok 40 titulku
        # nesmyslne dlouho. Doloženo: 28.8. se jeden blok zasekl a cekalo se
        # na nej CELYCH 15 MINUT (12:06 → 12:21), pak se jeho repliky dozadaly
        # po jedne a cely preklad 26minutoveho videa trval 62 min misto ~14.
        # Kratsi timeout selze rychle a preskoci na dozadani.
        timeout=float(cfg.get("llm_timeout_s", 180)))
    return json.loads(r.read()).get("response", "")


# HANS_TRANSLATE_CZ_CHECK_V1 — pozná repliku, která NEPROŠLA překladem.
# ⚠️ Práh je schválně nesymetrický: diakritika NEBO české slovo stačí k „česky",
# kdežto k „anglicky" jsou potřeba DVĚ anglická funkční slova. Krátká česká věta
# („Absolutně ne.") diakritiku mít nemusí a nesmí spadnout do falešného poplachu;
# anglická věta naproti tomu funkční slova skoro vždy má. Ověřeno na 172 reálných
# přeložených replikách — ani jeden falešný poplach.
# ⛔ ZÁMĚRNĚ TU NEJSOU „a", „to", „by", „v" ani „pro" — jsou to zároveň běžná
# ANGLICKÁ slova, takže by anglickou větu prohlásila za českou. Změřeno: s nimi
# detektor odhalil jen 10 z 86 anglických replik, bez nich 79.
_CZ_SLOVA = re.compile(r"\b(ale|je|se|na|[žz]e|jsou|byl[ao]?|nen[íi]|kdy[žz]|"
                       r"jako|tak|nebo|u[žz]|si|kter[áýé]|jeho|jej[íi]|tady|"
                       r"velmi|mezi|proto[žz]e|v[šs]ak|jsem|jsi|jsme|jste|bude|"
                       r"budou|mohl|toho|tom|tomu|jen|nic|kde|kdo|pak|tedy|moc)\b", re.I)
# ⛔ ZÁMĚRNĚ TU NEJSOU „to", „on", „my", „i", „do", „ten" ani „a" — anglicky
# běžná, ale zároveň to jsou ČESKÁ slova, takže by dělala falešné poplachy.
_EN_SLOVA = re.compile(r"\b(the|and|that|with|this|which|there|about|would|they|"
                       r"have|has|had|from|what|when|were|was|been|being|their|"
                       r"could|should|because|it|its|is|are|you|your|we|our|us|"
                       r"he|she|his|her|him|not|but|for|all|can|will|does|did|"
                       r"who|why|how|very|just|only|also|then|than|more|into|"
                       r"through|between)\b", re.I)


def _zni_cesky(t: str) -> bool:
    if not (t or "").strip():
        return False
    if _CZ.search(t) or _CZ_SLOVA.search(t):
        return True
    # Práh 1 (ne 2): změřeno na 172 českých + 86 anglických replikách — odhalí
    # 86/86 anglických a NEshodí ani jednu českou. Falešný poplach navíc je
    # levný (replika se jen dožádá znovu), propuštěná angličtina drahá (uslyší
    # ji divák uprostřed pořadu).
    return len(_EN_SLOVA.findall(t)) < 1


_HEAD_JEDNA = ("You are a professional English (en) to Czech (cs) translator. "
               "Produce only the Czech translation.\n\n\n")


def _dozadej(config, cfg, veta: str, pokusu: int = 3) -> str | None:
    """Jedna replika zvlášť. Vrací JEN text, o kterém je doloženo, že zní česky —
    jinak None. ⚠️ Jediný pokus nestačil: 28.8. se volání zasekávala a replika
    pak zůstala v ANGLICKÉM originále natrvalo, protože se kód po prvním pádu
    vzdal."""
    for k in range(max(1, pokusu)):
        try:
            t = _ollama(config, cfg, _HEAD_JEDNA + veta, 300).strip().split("\n")[0]
        except Exception as e:
            log.debug("dožádání %d/%d selhalo: %s", k + 1, pokusu, e)
            time.sleep(1.5 * (k + 1))
            continue
        t = _ocisti_repliku(t)
        if t and _zni_cesky(t):
            return t
        time.sleep(1.0)
    return None


def prelozit_srt(config, src_srt, dst_srt, progress=None) -> dict:
    cfg = _cfg(config)
    cues = st.load_cues(src_srt)
    chunk = int(cfg.get("chunk_cues", 40))
    hotovo, dozadano, nepreloz, neceskych = [None] * len(cues), 0, 0, 0
    for a in range(0, len(cues), chunk):
        blk = cues[a:a + chunk]
        body = "\n".join(f"{i+1}|{c['text']}" for i, c in enumerate(blk))
        got = {}
        try:
            for ln in _ollama(config, cfg, _HEAD + "\n\n\n" + body).strip().split("\n"):
                m = re.match(r"\s*(\d+)\s*\|\s*(.+)", ln)
                if m:
                    got[int(m.group(1))] = m.group(2).strip()
        except Exception as e:
            log.warning("překlad bloku selhal: %s", e)
        for i, c in enumerate(blk):
            t = got.get(i + 1)
            # HANS_TRANSLATE_CZ_CHECK_V1 — dva důvody k dožádání, ne jeden:
            # (a) replika chybí (zarovnání se rozešlo) — to řešil kód i dřív;
            # (b) replika JE, ale není česky — model vrátil originál. Tuhle
            # možnost dřív nikdo neověřoval, takže angličtina prošla do dabingu
            # a `nepreloz` u toho hlásilo nulu.
            duvod = "" if t else "chybí"
            if t and not _zni_cesky(t):
                duvod = "anglicky"
                neceskych += 1
            if duvod:
                dozadano += 1
                t2 = _dozadej(config, cfg, c["text"])
                if t2:
                    t = t2
                elif not t:
                    # ⚠️ Původní ANGLICKÁ věta jde do české stopy a Vlasta ji
                    # přečte česky. Radši to než díra — ale MUSÍ se to spočítat
                    # a říct, jinak na to uživatel narazí až uprostřed sledování.
                    t = c["text"]
                    nepreloz += 1
                else:
                    # zůstává, co vrátil blok, ale česky to nevypadá → přiznat
                    nepreloz += 1
            hotovo[a + i] = t
        if progress:
            progress(min(a + chunk, len(cues)), len(cues))
    # HANS_SUB_CLEAN_V3 — pojistka na VÝSTUPU. Vyčištěný vstup nestačí: model
    # marker přeloží („[music]" → „[hudba]") nebo si šipky doplní sám. Tady je
    # to poslední místo před namlouváním, takže co projde, to je slyšet.
    pocisteno = 0
    for i, t in enumerate(hotovo):
        t2 = _ocisti_repliku(t or "")
        if t2 != (t or ""):
            pocisteno += 1
            hotovo[i] = t2
    if pocisteno:
        log.info("po překladu očištěno %d replik (marker zvuku / šipky)", pocisteno)
    with open(dst_srt, "w", encoding="utf-8") as f:
        for i, (c, t) in enumerate(zip(cues, hotovo), 1):
            f.write(f"{i}\n{_ts(c['start'])} --> {_ts(c['end'])}\n{t}\n\n")
    if neceskych:
        log.warning("model vrátil %d replik, které nezněly česky — dožádány", neceskych)
    if nepreloz:
        log.warning("nepřeloženo %d z %d replik — zůstaly v originále",
                    nepreloz, len(cues))
    return {"replik": len(cues), "dozadano": dozadano, "nepreloz": nepreloz,
            "pocisteno": pocisteno, "neceskych": neceskych}


# ── sestavení výsledku ───────────────────────────────────────────────────────
def zamichat(config, pc_path, wav_local, out_name, rezim=None) -> str:
    """Video se jen KOPÍRUJE. Výsledek má DVĚ stopy: české lektorské čtení
    (originál ztlumený pod ní) a originál.

    ⚠️ Samostatná čistá čeština se ZÁMĚRNĚ nepřidává — uživatel ji po
    poslechovém testu 26.8. výslovně nechtěl ("přidej tam jen jednu stopu,
    tu lektorskou"). Není to opomenutí, nevracet."""
    cfg = _cfg(config)
    # HANS_TRANSLATE_OUT_V1 (26.8., pokyn uživatele): nové MKV vzniká VEDLE
    # originálu, originál zůstává. Až uživatel zhlédne pár přeložených pořadů,
    # přepne se na `nahradit` — proto je to volba v configu, ne natvrdo.
    # `rezim` zvenčí přebíjí config: u videa staženého z YouTube nemá `vedle`
    # smysl (žádný originál v knihovně tam neleží) → volající pošle `stranou`.
    rezim = rezim or cfg.get("out_mode", "vedle")
    if rezim == "vedle":
        outdir = os.path.dirname(pc_path)
    elif rezim == "stranou":
        outdir = cfg.get("out_dir", "/mnt/D/Hans_preklad")
    elif rezim == "nahradit":
        # ⛔ ZÁMĚRNĚ NEPOSTAVENO. Nahrazení je nevratné a musí jít v pořadí
        # „vyrob stranou → OVĚŘ (velikost, délka, stopy) → teprve pak smaž
        # originál a přejmenuj". Dokud to není napsané a odzkoušené, radši
        # srozumitelná chyba než smazaná předloha.
        raise RuntimeError("out_mode 'nahradit' zatím není hotové — "
                           "nechte 'vedle', jinak hrozí ztráta originálu")
    else:
        raise RuntimeError(f"neznámý out_mode '{rezim}'")
    out = f"{outdir}/{out_name}"
    _pc(cfg, f"mkdir -p {_q(outdir)}", 60)
    _pc_put(cfg, wav_local, "/tmp/hans_cz_track.wav")
    vol = float(cfg.get("orig_volume", 0.22))
    # ⚠️ -fflags +genpts: AVI nenese časové značky použitelné pro MKV a mux
    #    by spadl na „Can't write packet with unknown timestamp" — a vyrobil
    #    přitom 14kB zmetek, na kterém ffprobe vypíše všechny stopy správně.
    cmd = (f"ffmpeg -v error -y -fflags +genpts -i {_q(pc_path)} -i /tmp/hans_cz_track.wav "
           f"-filter_complex \"[0:a]aresample=48000,volume={vol}[o];"
           f"[1:a]aresample=48000[c];[o][c]amix=inputs=2:duration=longest:normalize=0[lekt]\" "
           f"-map 0:v:0 -map \"[lekt]\" -map 0:a:0 "
           f"-c:v copy -c:a aac -b:a 160k "
           f"-metadata:s:a:0 language=ces -metadata:s:a:0 title=\"Cesky (lektorske)\" "
           f"-metadata:s:a:1 language=eng -metadata:s:a:1 title=\"Original\" "
           # HANS_TRACK_DEFAULT_V1 (29.8.) — `-disposition:a:1 0` NENÍ zbytečné:
           # ffmpeg kopíruje dispozici ze vstupu, takže originál si `default`
           # přinesl s sebou a stopy byly default OBĚ. Přehrávač si pak vybíral
           # sám — doloženo na hotových souborech (ffprobe: default=1 u obou),
           # uživatel slyšel angličtinu místo lektora.
           f"-disposition:a:0 default -disposition:a:1 0 "
           f"-avoid_negative_ts make_zero {_q(out)}")
    _pc(cfg, cmd, 3600)
    velikost = int(_pc(cfg, f"stat -c%s {_q(out)}", 60).strip())
    if velikost < 1_000_000:      # zmetek z rozbitých časových značek
        raise RuntimeError(f"výsledek má jen {velikost} B — mux se nepovedl")
    return out


# ── YouTube jako zdroj (HANS_YT_TRANSLATE_V1) ────────────────────────────────
def _nahraj_zdroj_na_pc(cfg, lokalni_video: str, vid: str) -> str:
    """Stažené video z Pi na PC. Všechno, co sahá na video (ffprobe, mux),
    běží na PC — Pi jen stahuje, protože na PC blokuje nové programy
    OpenSnitch a chybí tam pip."""
    zdrojdir = (cfg.get("out_dir") or "/mnt/D/Hans_preklad").rstrip("/") + "/_zdroj"
    _pc(cfg, f"mkdir -p {_q(zdrojdir)}", 60)
    remote = f"{zdrojdir}/yt_{vid}{os.path.splitext(lokalni_video)[1]}"
    _pc_put(cfg, lokalni_video, remote,
            timeout=int(((cfg.get("youtube") or {}).get("upload_timeout_s", 1800))))
    return remote


def _uklid_zdroje(cfg, pc_path: str) -> None:
    """Stažený zdroj po ÚSPĚŠNÉM složení smazat — jeho obraz i zvuk jsou
    beze změny uvnitř výsledku, takže by to byla jen druhá kopie téhož.
    Při jakémkoli selhání zůstane ležet, aby šel běh zopakovat bez stahování.
    Kdo chce originál podržet, přepne `translate.youtube.keep_source`."""
    if (cfg.get("youtube") or {}).get("keep_source", False):
        return
    try:
        _pc(cfg, f"rm -f {_q(pc_path)}", 60)
    except Exception as e:
        log.warning("stažený zdroj se nepodařilo uklidit: %s", e)


def _delka_videa(cfg, pc_path: str) -> float:
    return float(_pc(cfg, f"ffprobe -v error -show_entries format=duration "
                          f"-of csv=p=0 {_q(pc_path)}", 180).strip())


def zamichat_zpomalene(config, pc_path, wav_local, out_name, f: float) -> str:
    """Jako `zamichat`, ale obraz I originální zvuk se ZPOMALÍ faktorem `f`.

    Nápad uživatele 27.8. Důvod je početní: český text potřeboval o 18 % víc
    času než video, a stlačit řeč jde nejvýš o 15 % (FLOOR 0.85). Chybějící
    čas se tedy musí VYROBIT — prodloužením pořadu. 6 % je na obraze
    neznatelných a řeči pak stačí zbylých ~11 %.

    ⚠️ ZPOMALIT SE MUSÍ OBOJÍ, jinak zvuk uteče obrazu (na to se ptal uživatel
    a je to reálná past — `duration` kontejneru je jen `max()` přes stopy,
    takže při zpomalení SAMOTNÉHO zvuku ukáže TÝŽ výsledek a vypadá to dobře).
    Průkazné jsou až časové značky posledního paketu: ověřeno 27.8. na reálném
    souboru — obraz 901,03 → 955,09 s (poměr přesně 1,0600), zvuk 955,14 s,
    rozdíl 0,05 s po šestnácti minutách.

    Obraz se NEPŘEKÓDOVÁVÁ: `-itsscale` přenásobí časové značky a `-c:v copy`
    nechá snímky být (ověřeno: 27 032 snímků před i po). Překóduje se jen zvuk.

    ⛔ Tohle je YOUTUBE cesta. `zamichat` zůstává beze změny pro dokumenty ze
    souborů, kterým dnešní chování vyhovuje (pokyn uživatele 27.8.).
    """
    cfg = _cfg(config)
    outdir = cfg.get("out_dir", "/mnt/D/Hans_preklad")
    out = f"{outdir}/{out_name}"
    _pc(cfg, f"mkdir -p {_q(outdir)}", 60)
    _pc_put(cfg, wav_local, "/tmp/hans_cz_track.wav",
            timeout=int((cfg.get("youtube") or {}).get("upload_timeout_s", 1800)))
    vol = float(cfg.get("orig_volume", 0.22))
    inv = 1.0 / f
    cmd = (f"ffmpeg -v error -y -itsscale {f:.6f} -i {_q(pc_path)} -i {_q(pc_path)} "
           f"-i /tmp/hans_cz_track.wav -filter_complex "
           f"\"[1:a:0]atempo={inv:.6f},aresample=48000[orig];"
           f"[orig]asplit=2[o1][o2];[o1]volume={vol}[oq];"
           f"[2:a]aresample=48000[cz];"
           f"[oq][cz]amix=inputs=2:duration=longest:normalize=0[lekt]\" "
           f"-map 0:v:0 -map \"[lekt]\" -map \"[o2]\" "
           f"-c:v copy -c:a aac -b:a 160k "
           f"-metadata:s:a:0 language=ces -metadata:s:a:0 title=\"Cesky (lektorske)\" "
           f"-metadata:s:a:1 language=eng -metadata:s:a:1 title=\"Original\" "
           # HANS_TRACK_DEFAULT_V1 — viz `zamichat`: bez sundání příznaku
           # z originálu jsou default obě stopy a přehrávač bere angličtinu.
           f"-disposition:a:0 default -disposition:a:1 0 {_q(out)}")
    _pc(cfg, cmd, 3600)
    velikost = int(_pc(cfg, f"stat -c%s {_q(out)}", 60).strip())
    if velikost < 1_000_000:
        raise RuntimeError(f"výsledek má jen {velikost} B — mux se nepovedl")
    return out


# ── celý běh ─────────────────────────────────────────────────────────────────
def preloz(config, pc_path=None, meta=None, progress=None) -> dict:
    cfg = _cfg(config)
    t0 = time.time()
    # ⚠️ `pc_path is None` NEZNAMENÁ „zeptej se Kodi". U YouTube je None správný
    # stav — soubor ještě neexistuje — a zadání nese `yt_id`. Bez téhle podmínky
    # se Kodi doptá podruhé a přeloží se to, co běží TEĎ, ne to, co volající
    # zadal. (Odhaleno testem 27.8.: běh dostal id videa a pustil se do pořadu,
    # který zrovna hrál.)
    if pc_path is None and not (meta or {}).get("yt_id"):
        info = kodi_now_file(config)
        if not info["ok"]:
            return {"ok": False, "duvod": info["duvod"]}
        pc_path, meta = info.get("pc_path"), info
    meta = meta or {}
    say = progress or (lambda *_a, **_k: None)
    yt_id = meta.get("yt_id")

    # U YouTube soubor zatím neexistuje → kontrola české stopy až po stažení.
    if pc_path and has_czech_audio(cfg, pc_path):
        return {"ok": False, "duvod": "Tenhle pořad už českou zvukovou stopu má."}

    with tempfile.TemporaryDirectory(prefix="hans_preklad_") as wd:
        yt = None
        if yt_id and not pc_path:
            yt = hy.stahni(config, yt_id, wd, say)
            say("nahrávám video na PC")
            pc_path = _nahraj_zdroj_na_pc(cfg, yt["video"], yt_id)
            meta = {**meta, "title": yt["titul"]}

        say("hledám titulky")
        if yt and yt.get("srt"):
            # yt-dlp přinesl titulky rovnou s časováním → přepis ze zvuku,
            # největší zdroj chyb u dokumentů, se u YouTube vůbec nepoužije.
            # HANS_YT_SILENCE_V1: u automatického přepisu se švy přesadí na
            # skutečné pauzy ve zvuku. Časy rolujících titulků totiž navazují
            # i tam, kde mluvčí mlčí, takže skládání stopy nemá kde srovnat
            # skluz — bez tohohle kroku byl na patnáctiminutovém pořadu 41 s.
            yt_cfg = (cfg.get("youtube") or {})
            if yt.get("rolujici") and yt.get("vtt") and yt_cfg.get("use_silence", True):
                say("hledám pauzy ve zvuku")
                ticho = hy.zjisti_ticho(cfg, pc_path, lambda c, t: _pc(cfg, c, t),
                                        float(yt_cfg.get("silence_db", -35)),
                                        float(yt_cfg.get("min_pauza_s", 0.35)))
                if ticho:
                    s2 = os.path.join(wd, "yt_ticho.srt")
                    try:
                        n = hy.srt_s_tichem(yt["vtt"], s2, ticho, yt_cfg)
                        if n > 0:
                            yt["srt"] = s2
                            log.info("švy přesazeny na pauzy ve zvuku: %d frází", n)
                    except Exception as e:
                        log.warning("přesazení švů selhalo, beru původní: %s", e)
            zdroj = {"srt": yt["srt"], "jazyk": yt["jazyk"], "zdroj": yt["zdroj"]}
        else:
            zdroj = obstarej_titulky(config, pc_path, meta, wd)
        log.info("zdroj textu: %s (%s)", zdroj["zdroj"], zdroj["jazyk"])

        srt = zdroj["srt"]
        # HANS_SUB_CLEAN_V1 — jedno místo pro obě cesty, PŘED překladem
        _cist = os.path.join(wd, "bez_noticek.srt")
        _u = _bez_noticek(srt, _cist)
        if _u["zmeneno"]:
            srt = _cist
            # ⚠️ Vypsat i `upraveno` — bez něj hláška tvrdila „0 zahozeno,
            # 0 očištěno", i když se upravilo 53 replik z 86, a vypadalo to,
            # že čištění neběží.
            log.info("notičky pro neslyšící: %d zahozeno, %d očištěno, "
                     "%d upraveno (z %d replik)",
                     _u["zahozeno"], _u["ocisteno"], _u.get("upraveno", 0),
                     _u.get("replik", 0))
        prel = {}
        if zdroj["jazyk"] != "cs":
            say("překládám")
            _vstup = srt                      # ← drží se PŘED přepsáním na cíl
            srt = os.path.join(wd, "cz.srt")
            # ⚠️ MUSÍ to být `_vstup`, ne `zdroj["srt"]`. Řádek nad tímhle
            # přepsal `srt` na CÍLOVOU cestu, takže původní kód sáhl zpátky do
            # `zdroj` — a tím zahodil právě vyčištěný soubor (HANS_SUB_CLEAN_V3,
            # doloženo 29.8.: 53 z 86 replik neslo „[music]"/„&gt;&gt;", model
            # z nich vyrobil „[hudba]" a to se ozývalo v dabingu). Česká větev
            # čištění dostávala, překladová ne.
            prel = prelozit_srt(config, _vstup, srt,
                                lambda a, b: say(f"překládám {a}/{b}"))

        wav = os.path.join(wd, "cz.wav")
        rp = None
        if yt:
            # HANS_YT_TIMEBUDGET_V1: čeština je delší než originál a musí se do
            # pořadu vejít. Rozpočet rozdělí schodek mezi zpomalení obrazu
            # a zrychlení řeči — a když ani to nestačí, radši to řekne, než aby
            # tiše vyrobil soubor, který se ke konci rozejde o minutu.
            rp = hy.rozpocet(st.load_cues(srt), _delka_videa(cfg, pc_path),
                             cfg.get("youtube") or {})
            log.info("rozpočet času: %s", rp)
            if rp["verdikt"] == "odmitnout":
                return {"ok": False, "duvod": rp["hlaska"]}
            if rp["f_video"] > 1.001:
                s2 = os.path.join(wd, "cz_zpomaleno.srt")
                hy.preskaluj_srt(srt, s2, rp["f_video"])
                srt = s2
            say("namlouvám")
            hlas = hy.namluv_s_tempem(srt, wav, config, rp["zaklad_pct"])
        else:
            say("namlouvám")
            hlas = tts_engine.synth_srt(srt, wav, config)

        say("skládám soubor")
        if yt:
            jmeno = hy.nazev_souboru(yt["titul"], yt_id)
            out = (zamichat_zpomalene(config, pc_path, wav, jmeno, rp["f_video"])
                   if rp["f_video"] > 1.001
                   else zamichat(config, pc_path, wav, jmeno, rezim="stranou"))
        else:
            jmeno = os.path.splitext(os.path.basename(pc_path))[0] + " [CZ].mkv"
            out = zamichat(config, pc_path, wav, jmeno)

    if yt:
        _uklid_zdroje(cfg, pc_path)
    _zapis_do_deniku(config, (yt or {}).get("url") or pc_path, out,
                     {"zdroj": zdroj["zdroj"], "motor": hlas["engine"],
                      "trvalo_s": round(time.time() - t0),
                      "youtube": (yt or {}).get("url"),
                      "zpomaleni": (rp or {}).get("f_video"),
                      "rec_pct": (rp or {}).get("zaklad_pct"),
                      "dozadano": prel.get("dozadano"),
                      "nepreloz": prel.get("nepreloz"),
                      "posun_s": (hlas.get("stats") or {}).get("max_opozdeni_s")},
                     popis=(yt or {}).get("titul"))
    return {"ok": True, "soubor": out, "zdroj": zdroj["zdroj"], "motor": hlas["engine"],
            "posun_s": (hlas.get("stats") or {}).get("max_opozdeni_s"),
            "zpomaleni": (rp or {}).get("f_video"), "rec_pct": (rp or {}).get("zaklad_pct"),
            "upozorneni": (rp or {}).get("hlaska") or None,
            "dozadano": prel.get("dozadano"), "nepreloz": prel.get("nepreloz"),
            "replik": prel.get("replik"), "trvalo_s": round(time.time() - t0)}


# ── evidence hotových překladů ───────────────────────────────────────────────
# Pokyn uživatele 26.8.: „bude nutné držet přeložené dokumenty s cestou, ať je
# pak při mazání můžeme rychle dohledat." Duplikát zabírá ~+90 MB/hod a `/mnt/F`
# je z 96 % plný → bez evidence by se to za pár měsíců hledalo po složkách ručně.
# Píše se do DENÍKU, ne do vlastního souboru — jeden zdroj pravdy a chodí to
# do záloh spolu se zbytkem Hansovy paměti.

def _zapis_do_deniku(config, orig: str, out: str, extra: dict, popis=None) -> None:
    try:
        import sqlite3
        db = (config.get("diary_db")
              or (config.get("hans_idle", {}) or {}).get("diary_db")
              or "data/hans_diary.db")
        data = json.dumps({"original": orig, "vystup": out, **extra}, ensure_ascii=False)
        c = sqlite3.connect(db, timeout=5.0)
        c.execute("INSERT INTO diary (ts, event_type, title, data, note) VALUES (?,?,?,?,?)",
                  (time.time(), "translate_done", os.path.basename(out), data,
                   ("Připravil jsem českou stopu k „%s“ z YouTube." % popis) if popis
                   else ("Připravil jsem českou stopu k „%s“. Originál jsem nechal "
                         "na místě." % os.path.basename(orig))))
        c.commit()
        c.close()
    except Exception as e:
        log.warning("evidence překladu se nezapsala: %s", e)


def seznam_prekladu(config, limit: int = 50) -> list[dict]:
    """Hotové překlady i s cestami. `existuje` se OVĚŘUJE na PC — záznam
    v deníku zůstane, i když soubor někdo mezitím smaže."""
    import sqlite3
    db = (config.get("diary_db")
          or (config.get("hans_idle", {}) or {}).get("diary_db")
          or "data/hans_diary.db")
    c = sqlite3.connect(db, timeout=5.0)
    rows = c.execute("SELECT ts, data FROM diary WHERE event_type='translate_done' "
                     "ORDER BY ts DESC LIMIT ?", (int(limit),)).fetchall()
    c.close()
    out = []
    for ts, data in rows:
        try:
            d = json.loads(data or "{}")
        except Exception:
            continue
        d["ts"] = ts
        out.append(d)
    if out:
        cfg = _cfg(config)
        try:
            cesty = " ".join(_q(d["vystup"]) for d in out if d.get("vystup"))
            zive = set(_pc(cfg, f"for f in {cesty}; do test -f \"$f\" && echo \"$f\"; done",
                           120).splitlines())
            for d in out:
                d["existuje"] = d.get("vystup") in zive
        except Exception as e:
            log.warning("existenci souborů se nepodařilo ověřit: %s", e)
    return out


def seznam_text(config) -> str:
    try:
        polozky = seznam_prekladu(config)
    except Exception as e:
        return f"Seznam překladů se mi nepodařilo přečíst, pane: {e}"
    if not polozky:
        return "Zatím jsem nepřeložil žádný dokument, pane."
    r = ["Přeložené dokumenty (originály jsem nechal na místě):"]
    chybi = 0
    for d in polozky[:20]:
        den = time.strftime("%-d.%-m.", time.localtime(d.get("ts", 0)))
        znak = "" if d.get("existuje", True) else "  ⚠️ soubor už tam není"
        if not d.get("existuje", True):
            chybi += 1
        r.append(f"· {den}  {d.get('vystup')}{znak}")
    if chybi:
        r.append(f"({chybi} z nich už na disku není — záznam nechávám, "
                 f"ať je vidět, co bylo.)")
    return "\n".join(r)


# ── běh na pozadí ────────────────────────────────────────────────────────────
# Řetěz trvá minuty až desítky minut. Blokovat na něm chat nejde, a nejhorší
# možný stav je, že se nestane nic a uživatel neví, jestli to běží nebo umřelo
# → proto se hlásí i NEÚSPĚCH, a stav jde kdykoli doptat.
_job = {"bezi": False, "stav": "", "od": 0.0, "vysledek": None, "titul": ""}
_job_lock = __import__("threading").Lock()


def stav_text() -> str:
    j = _job
    if j["bezi"]:
        m = int((time.time() - j["od"]) // 60)
        kde = j["stav"] or "pracuji"
        return (f"Překládám „{j['titul']}“ — {kde}. "
                f"Běží to {m} min, pane; ozvu se, až bude hotovo.")
    v = j["vysledek"]
    if not v:
        return "Žádný překlad teď neběží, pane."
    if v.get("ok"):
        return f"Poslední překlad je hotový: {os.path.basename(v['soubor'])}"
    return f"Poslední překlad se nepovedl: {v.get('duvod')}"


def _hlas(handler):
    """Kanál pro hlášení. ⚠️ `send`, NE `send_proactive` — tohle není proaktivní
    šťouchnutí, ale odpověď na vyžádaný úkol, takže NESMÍ spadnout do tichého
    okna (jinak by se uživatel v 8:00 nedozvěděl, že je hotovo)."""
    n = getattr(handler, "telegram", None) or getattr(handler, "notifier", None)
    return n if getattr(n, "enabled", False) else None


def spust_na_pozadi(config, handler) -> str:
    """Vrátí hlášku pro uživatele HNED; výsledek dorazí zprávou."""
    import threading
    with _job_lock:
        if _job["bezi"]:
            return stav_text()
        info = kodi_now_file(config)
        if not info["ok"]:
            return info["duvod"]
        _job.update({"bezi": True, "stav": "začínám", "od": time.time(),
                     "vysledek": None, "titul": info.get("title") or "dokument"})

    def beh():
        try:
            r = preloz(config, pc_path=info.get("pc_path"), meta=info,
                       progress=lambda m, *a: _job.__setitem__("stav", m))
        except Exception as e:
            log.exception("překlad spadl")
            r = {"ok": False, "duvod": f"{type(e).__name__}: {e}"}
        with _job_lock:
            _job.update({"bezi": False, "vysledek": r, "stav": ""})
        n = _hlas(handler)
        if not n:
            return
        if r.get("ok"):
            mins = round(r.get("trvalo_s", 0) / 60)
            pozn = "" if r.get("motor") == "edge" else " (záložním hlasem — cloud nebyl k dispozici)"
            # Nepřeložené repliky se PŘIZNÁVAJÍ. Zůstala v nich angličtina,
            # kterou český hlas přečte — bez téhle věty by to uživatel zjistil
            # až uprostřed sledování a neměl by tušení proč.
            nep = r.get("nepreloz") or 0
            vada = ""
            if nep:
                z_kolika = r.get("replik") or 0
                vada = (f"\n⚠️ {nep} z {z_kolika} replik se mi přeložit nepodařilo — "
                        f"zůstaly v angličtině a hlas je přečte tak, jak jsou.")
            n.send(f"Překlad je hotový, pane{pozn}. Trvalo to {mins} min.\n"
                   f"Soubor: {r['soubor']}\n"
                   f"Zdroj textu: {r.get('zdroj')}{vada}")
        else:
            n.send(f"Překlad se nepovedl, pane: {r.get('duvod')}")

    threading.Thread(target=beh, name="hans-preklad", daemon=True).start()
    return (f"Dám se do toho, pane — „{_job['titul']}“. "
            "Chvíli to potrvá; ozvu se, až bude hotovo.")
