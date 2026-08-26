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


def _pc_put(cfg, local, remote):
    subprocess.run(["scp", "-q", "-i", os.path.expanduser(cfg.get("pc_key", "~/.ssh/hans_pc")),
                    local, f"{cfg.get('pc_user','user')}@{cfg.get('pc_host','192.168.1.10')}:{remote}"],
                   check=True, capture_output=True)


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
    smb, mnt = cfg.get("smb_prefix", "smb://192.168.1.10/"), cfg.get("mnt_prefix", "/mnt/")
    if not f.startswith(smb):
        return {"ok": False, "duvod": f"Soubor {f} není na známém úložišti."}
    return {"ok": True, "pc_path": mnt + f[len(smb):],
            "title": np.get("showtitle") or np.get("title") or np.get("label"),
            "season": np.get("season"), "episode": np.get("episode")}


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
    # 2) OpenSubtitles podle otisku
    osub = OpenSubtitles(config)
    if osub.enabled:
        try:
            h = _pc_hash(cfg, pc_path)
            for jazyk in ("cs", "en"):
                hits = osub.by_hash(h, jazyk)
                if hits:
                    fid = OpenSubtitles.file_id(hits[0])
                    if fid:
                        loc = os.path.join(workdir, f"os_{jazyk}.srt")
                        osub.download(fid, loc)
                        return {"srt": loc, "jazyk": jazyk,
                                "zdroj": f"OpenSubtitles ({jazyk}, shoda otisku)"}
        except Exception as e:
            log.warning("OpenSubtitles selhalo, jdu dál: %s", e)
    # 3) přepis ze zvuku
    return {"srt": _stt(cfg, config, pc_path, workdir), "jazyk": "en", "zdroj": "přepis ze zvuku"}


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
        headers={"Content-Type": "application/json"}), timeout=900)
    return json.loads(r.read()).get("response", "")


def prelozit_srt(config, src_srt, dst_srt, progress=None) -> dict:
    cfg = _cfg(config)
    cues = st.load_cues(src_srt)
    chunk = int(cfg.get("chunk_cues", 40))
    hotovo, dozadano = [None] * len(cues), 0
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
            if not t:   # zarovnání se rozešlo → dožádat samostatně
                dozadano += 1
                try:
                    t = _ollama(config, cfg, "You are a professional English (en) to Czech (cs) "
                                "translator. Produce only the Czech translation.\n\n\n"
                                + c["text"], 300).strip().split("\n")[0]
                except Exception:
                    t = c["text"]     # radši původní než díra ve stopě
            hotovo[a + i] = t
        if progress:
            progress(min(a + chunk, len(cues)), len(cues))
    with open(dst_srt, "w", encoding="utf-8") as f:
        for i, (c, t) in enumerate(zip(cues, hotovo), 1):
            f.write(f"{i}\n{_ts(c['start'])} --> {_ts(c['end'])}\n{t}\n\n")
    return {"replik": len(cues), "dozadano": dozadano}


# ── sestavení výsledku ───────────────────────────────────────────────────────
def zamichat(config, pc_path, wav_local, out_name) -> str:
    """Video se jen KOPÍRUJE. Výsledek má DVĚ stopy: české lektorské čtení
    (originál ztlumený pod ní) a originál.

    ⚠️ Samostatná čistá čeština se ZÁMĚRNĚ nepřidává — uživatel ji po
    poslechovém testu 26.8. výslovně nechtěl ("přidej tam jen jednu stopu,
    tu lektorskou"). Není to opomenutí, nevracet."""
    cfg = _cfg(config)
    # HANS_TRANSLATE_OUT_V1 (26.8., pokyn uživatele): nové MKV vzniká VEDLE
    # originálu, originál zůstává. Až uživatel zhlédne pár přeložených pořadů,
    # přepne se na `nahradit` — proto je to volba v configu, ne natvrdo.
    rezim = cfg.get("out_mode", "vedle")
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
           f"-disposition:a:0 default -avoid_negative_ts make_zero {_q(out)}")
    _pc(cfg, cmd, 3600)
    velikost = int(_pc(cfg, f"stat -c%s {_q(out)}", 60).strip())
    if velikost < 1_000_000:      # zmetek z rozbitých časových značek
        raise RuntimeError(f"výsledek má jen {velikost} B — mux se nepovedl")
    return out


# ── celý běh ─────────────────────────────────────────────────────────────────
def preloz(config, pc_path=None, meta=None, progress=None) -> dict:
    cfg = _cfg(config)
    t0 = time.time()
    if pc_path is None:
        info = kodi_now_file(config)
        if not info["ok"]:
            return {"ok": False, "duvod": info["duvod"]}
        pc_path, meta = info["pc_path"], info
    meta = meta or {}
    say = progress or (lambda *_a, **_k: None)

    if has_czech_audio(cfg, pc_path):
        return {"ok": False, "duvod": "Tenhle pořad už českou zvukovou stopu má."}

    with tempfile.TemporaryDirectory(prefix="hans_preklad_") as wd:
        say("hledám titulky")
        zdroj = obstarej_titulky(config, pc_path, meta, wd)
        log.info("zdroj textu: %s (%s)", zdroj["zdroj"], zdroj["jazyk"])

        srt = zdroj["srt"]
        prel = {}
        if zdroj["jazyk"] != "cs":
            say("překládám")
            srt = os.path.join(wd, "cz.srt")
            prel = prelozit_srt(config, zdroj["srt"], srt,
                                lambda a, b: say(f"překládám {a}/{b}"))

        say("namlouvám")
        wav = os.path.join(wd, "cz.wav")
        hlas = tts_engine.synth_srt(srt, wav, config)

        say("skládám soubor")
        jmeno = os.path.splitext(os.path.basename(pc_path))[0] + " [CZ].mkv"
        out = zamichat(config, pc_path, wav, jmeno)

    _zapis_do_deniku(config, pc_path, out,
                     {"zdroj": zdroj["zdroj"], "motor": hlas["engine"],
                      "trvalo_s": round(time.time() - t0),
                      "posun_s": (hlas.get("stats") or {}).get("max_opozdeni_s")})
    return {"ok": True, "soubor": out, "zdroj": zdroj["zdroj"], "motor": hlas["engine"],
            "posun_s": (hlas.get("stats") or {}).get("max_opozdeni_s"),
            "dozadano": prel.get("dozadano"), "trvalo_s": round(time.time() - t0)}


# ── evidence hotových překladů ───────────────────────────────────────────────
# Pokyn uživatele 26.8.: „bude nutné držet přeložené dokumenty s cestou, ať je
# pak při mazání můžeme rychle dohledat." Duplikát zabírá ~+90 MB/hod a `/mnt/F`
# je z 96 % plný → bez evidence by se to za pár měsíců hledalo po složkách ručně.
# Píše se do DENÍKU, ne do vlastního souboru — jeden zdroj pravdy a chodí to
# do záloh spolu se zbytkem Hansovy paměti.

def _zapis_do_deniku(config, orig: str, out: str, extra: dict) -> None:
    try:
        import sqlite3
        db = (config.get("diary_db")
              or (config.get("hans_idle", {}) or {}).get("diary_db")
              or "data/hans_diary.db")
        data = json.dumps({"original": orig, "vystup": out, **extra}, ensure_ascii=False)
        c = sqlite3.connect(db, timeout=5.0)
        c.execute("INSERT INTO diary (ts, event_type, title, data, note) VALUES (?,?,?,?,?)",
                  (time.time(), "translate_done", os.path.basename(out), data,
                   "Připravil jsem českou stopu k „%s“. Originál jsem nechal na místě."
                   % os.path.basename(orig)))
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
            r = preloz(config, pc_path=info["pc_path"], meta=info,
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
            n.send(f"Překlad je hotový, pane{pozn}. Trvalo to {mins} min.\n"
                   f"Soubor: {r['soubor']}\n"
                   f"Zdroj textu: {r.get('zdroj')}")
        else:
            n.send(f"Překlad se nepovedl, pane: {r.get('duvod')}")

    threading.Thread(target=beh, name="hans-preklad", daemon=True).start()
    return (f"Dám se do toho, pane — „{_job['titul']}“. "
            "Chvíli to potrvá; ozvu se, až bude hotovo.")
