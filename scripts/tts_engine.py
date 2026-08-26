"""HANS_TTS_ENGINE_V1 — výroba české zvukové stopy z SRT, s výměnným motorem.

Rozhodnuto uživatelem 26.8. po poslechovém testu:
  primární `edge` (edge-tts, hlas Vlasta) — nejlepší kvalita, ale je to
  NEOFICIÁLNÍ rozhraní Microsoftu, které může kdykoli zmizet nebo zdražit;
  záložní `piper` (kasandra) — lokální, o třídu horší, ale nezávislý.

Proč to riziko jde přijmout: stopa se generuje JEDNOU a zůstane v MKV natrvalo,
takže cloud je potřeba jen při zpracování — zrušení služby nerozbije nic, co už
je hotové. A pád na `piper` znamená horší hlas, ne nefunkční systém.

Zamítnuté cesty (nezkoušet znovu, měřeno 26.8.):
  · Piper `jirka` — uživatelem označen za nepoužitelný.
  · XTTS-v2 klon — 0,46× realtime = 130 min na hodinu pořadu. Příčina je
    hardwarová (`hipBLASLt` nepodporuje gfx1030), ne v kódu.
  · Chatterbox — ČEŠTINU NEUMÍ (model vypíše 23 jazyků, `cs` mezi nimi není).
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor

from scripts import srt_track as st

log = logging.getLogger(__name__)

FLOOR = 0.85          # nejrychlejší přípustné tempo (níž už to drhne)
EDGE_MAX_RATE = 40    # edge-tts: strop zrychlení v procentech
_EDGE_WORKERS = 4     # víc paralelních requestů = rychleji, ale neplašit službu


def _cfg(config: dict) -> dict:
    return (config or {}).get("translate", {}) or {}


# ── motor: edge-tts (běží na Pi, kde už knihovna je) ──────────────────────────
def _edge_one(args) -> str | None:
    import asyncio
    import edge_tts
    text, voice, rate, out_wav = args
    mp3 = out_wav + ".mp3"
    try:
        r = f"+{rate}%" if rate >= 0 else f"{rate}%"
        asyncio.run(edge_tts.Communicate(text, voice, rate=r).save(mp3))
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", mp3,
                        "-ac", "1", "-ar", "22050", out_wav],
                       check=True, capture_output=True)
        return out_wav
    except Exception as e:
        log.warning("edge: replika selhala (%s): %s", text[:40], e)
        return None
    finally:
        if os.path.exists(mp3):
            os.unlink(mp3)


def _synth_edge(cues, workdir, cfg) -> dict:
    voice = cfg.get("edge_voice", "cs-CZ-VlastaNeural")
    paths = [os.path.join(workdir, f"{i:05d}.wav") for i in range(len(cues))]

    with ThreadPoolExecutor(max_workers=_EDGE_WORKERS) as ex:
        list(ex.map(_edge_one, [(c["text"], voice, 0, p) for c, p in zip(cues, paths)]))

    hotovo = [p for p in paths if os.path.exists(p)]
    if not hotovo:
        raise RuntimeError("edge-tts nevyrobil ANI JEDNU repliku")
    if len(hotovo) < len(cues) * 0.9:
        raise RuntimeError(f"edge-tts vyrobil jen {len(hotovo)}/{len(cues)} replik")

    # 2. průchod: co přeteče svůj čas, namluvit rychleji přesně na míru
    sl = st.slots(cues)
    znovu = []
    for i, p in enumerate(paths):
        if not os.path.exists(p):
            continue
        d = st.duration(p)
        if d > sl[i]:
            scale = st.fit_scale(sl[i], d, FLOOR)
            rate = min(EDGE_MAX_RATE, int(round((1.0 / scale - 1.0) * 100)))
            if rate > 0:
                znovu.append((cues[i]["text"], voice, rate, p))
    if znovu:
        with ThreadPoolExecutor(max_workers=_EDGE_WORKERS) as ex:
            list(ex.map(_edge_one, znovu))
    log.info("edge: %d replik, zrychleno %d", len(hotovo), len(znovu))
    return {"paths": paths, "zrychleno": len(znovu)}


# ── motor: Piper (běží na PC, kde je binárka i hlasy) ─────────────────────────
def _synth_piper_remote(srt_path, out_wav, cfg) -> None:
    host = cfg.get("pc_host", "192.168.1.10")
    user = cfg.get("pc_user", "user")
    key = os.path.expanduser(cfg.get("pc_key", "~/.ssh/hans_pc"))
    ssh = ["ssh", "-i", key, "-o", "ConnectTimeout=10",
           "-o", "StrictHostKeyChecking=no", f"{user}@{host}"]
    rem = "/tmp/hans_piper_track"

    subprocess.run(ssh + [f"mkdir -p {rem}"], check=True, capture_output=True)
    here = os.path.dirname(os.path.abspath(__file__))
    for f in (os.path.join(here, "srt_track.py"),
              os.path.join(os.path.dirname(here), "deploy", "pc", "hans_piper_track.py")):
        subprocess.run(["scp", "-q", "-i", key, f, f"{user}@{host}:{rem}/"],
                       check=True, capture_output=True)
    subprocess.run(["scp", "-q", "-i", key, srt_path, f"{user}@{host}:{rem}/in.srt"],
                   check=True, capture_output=True)

    voice = os.path.join(cfg.get("piper_voices_dir", "~/piper/voices"),
                         cfg.get("piper_voice", "cs_CZ-kasandra-medium") + ".onnx")
    cmd = (f"cd {rem} && python3 hans_piper_track.py in.srt out.wav "
           f"'{cfg.get('piper_bin', '~/piper/piper/piper')}' '{voice}'")
    r = subprocess.run(ssh + [cmd], capture_output=True, text=True, timeout=3600)
    if r.returncode != 0:
        raise RuntimeError(f"piper na PC selhal: {r.stderr[-400:]}")
    subprocess.run(["scp", "-q", "-i", key, f"{user}@{host}:{rem}/out.wav", out_wav],
                   check=True, capture_output=True)


# ── veřejné rozhraní ─────────────────────────────────────────────────────────
def synth_srt(srt_path: str, out_wav: str, config: dict) -> dict:
    """SRT → jedna souvislá stopa. Vrátí {'engine', 'stats'|'zrychleno'}.

    Primární motor selže → zkusí se záložní. Který nakonec zabral, je
    v návratové hodnotě I v logu (jinak by tichý pád nebyl poznat).
    """
    cfg = _cfg(config)
    cues = st.load_cues(srt_path)
    if not cues:
        raise ValueError(f"v {srt_path} nejsou žádné titulky")

    poradi = [cfg.get("primary", "edge"), cfg.get("fallback", "piper")]
    chyby = []
    for motor in [m for m in poradi if m]:
        wd = tempfile.mkdtemp(prefix=f"hans_tts_{motor}_")
        try:
            if motor == "edge":
                res = _synth_edge(cues, wd, cfg)
                paths = res["paths"]
                stats = st.assemble(cues, lambda i: paths[i] if os.path.exists(paths[i]) else None,
                                    out_wav)
                log.info("TTS motor `edge` uspěl: %s", stats)
                return {"engine": "edge", "stats": stats, "zrychleno": res["zrychleno"]}
            if motor == "piper":
                _synth_piper_remote(srt_path, out_wav, cfg)
                log.info("TTS motor `piper` uspěl (záloha)")
                return {"engine": "piper", "stats": {}}
            chyby.append(f"{motor}: neznámý motor")
        except Exception as e:
            log.warning("TTS motor `%s` selhal, zkouším další: %s", motor, e)
            chyby.append(f"{motor}: {e}")
        finally:
            shutil.rmtree(wd, ignore_errors=True)
    raise RuntimeError("žádný TTS motor neuspěl — " + " | ".join(chyby))
