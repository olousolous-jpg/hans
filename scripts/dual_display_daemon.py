#!/usr/bin/env python3
"""
DUAL_DISPLAY_DAEMON — pohání oba 160×160 displeje (Fáze B).
  LEVÝ  (CS2=GPIO6)  = Hansova TVÁŘ (zrcadlí web preview přes avatar_state.json)
  PRAVÝ (CS1=GPIO13) = kontextová POZORNOST (AttentionRenderer)

Samostatný proces — běží VEDLE Hanse (naše piny nekolidují s robot_hat/audiem,
ověřeno). Čte stav ze sdílených souborů:
  data/avatar/avatar_state.json      ← píše display_renderer (mode+clip) = tvář
  data/avatar/attention_context.json ← (volitelně) publikuje Hans = bohatá pozornost
  data/system.log                    ← fallback nálada (poslední hans_mood přechod)

Spuštění:  python3 -m scripts.dual_display_daemon   (Ctrl-C ukončí)
"""
from __future__ import annotations

import glob
import json
import os
import re
import signal
import threading
import time

import cv2
import lgpio
import numpy as np
import spidev
from PIL import Image

from scripts.Eye_sphere import GC9A01, DC, CS1, CS2, RST1, RST2, W, H
from scripts.attention_display import AttentionRenderer

AV_DIR = "data/avatar"
STATE_F = os.path.join(AV_DIR, "avatar_state.json")
CTX_F = os.path.join(AV_DIR, "attention_context.json")
LOG_F = "data/system.log"
_PHASE = {"ráno": "ráno", "dopoledne": "dopoledne", "poledne": "poledne",
          "odpoledne": "odpoledne", "večer": "večer", "noc": "noc"}


def to_rgb565(img, rot90=0) -> bytes:
    """PIL/np RGB → RGB565 big-endian bytes (GC9A01). Panel je BGR (R↔B swap).
    rot90 = počet 90° CCW otočení (orientace per displej; ověřeno na HW)."""
    arr = np.asarray(img.convert("RGB") if isinstance(img, Image.Image) else img)
    if rot90:
        arr = np.rot90(arr, rot90)
    arr = np.ascontiguousarray(arr, dtype=np.uint16)
    r, g, b = arr[..., 0], arr[..., 1], arr[..., 2]
    v = ((b & 0xF8) << 8) | ((g & 0xFC) << 3) | (r >> 3)   # BGR panel
    return v.astype(">u2").tobytes()


class FaceSource:
    """Tvář = zrcadlo Pi avataru: idle.png pro klidné stavy, jinak frame klipu."""
    def __init__(self):
        self._clip_cache: dict = {}
        self._idle = self._load_idle()
        self._cur_clip = None
        self._frames = None
        self._idx = 0

    def _load_idle(self):
        vers = []
        for d in glob.glob(os.path.join(AV_DIR, "v*")):
            try:
                vers.append((int(os.path.basename(d)[1:]), d))
            except ValueError:
                pass
        path = None
        if vers:
            path = os.path.join(max(vers)[1], "idle.png")
        if not path or not os.path.exists(path):
            path = os.path.join(AV_DIR, "idle.png")
        if os.path.exists(path):
            im = cv2.cvtColor(cv2.imread(path), cv2.COLOR_BGR2RGB)
            return cv2.resize(im, (W, H))
        return np.zeros((H, W, 3), np.uint8)

    def _clip_frames(self, name):
        if name in self._clip_cache:
            return self._clip_cache[name]
        path = os.path.join(AV_DIR, "clips", name)
        out = []
        if os.path.exists(path):
            cap = cv2.VideoCapture(path)
            while True:
                ok, fr = cap.read()
                if not ok:
                    break
                out.append(cv2.resize(cv2.cvtColor(fr, cv2.COLOR_BGR2RGB), (W, H)))
            cap.release()
        self._clip_cache[name] = out
        return out

    def frame(self):
        st = _read_json(STATE_F) or {}
        mode, clip = st.get("mode"), st.get("clip")
        if mode in ("talk", "extra", "idleanim") and clip:
            if clip != self._cur_clip:
                self._cur_clip, self._frames, self._idx = clip, self._clip_frames(clip), 0
            if self._frames:
                fr = self._frames[self._idx % len(self._frames)]
                self._idx += 1
                return fr
        self._cur_clip = None
        return self._idle


def _read_json(path):
    try:
        if os.path.exists(path):
            return json.loads(open(path, encoding="utf-8").read())
    except Exception:
        pass
    return None


def _mood_from_log():
    try:
        with open(LOG_F, "rb") as f:
            f.seek(0, 2); sz = f.tell(); f.seek(max(0, sz - 120000))
            txt = f.read().decode("utf-8", "ignore")
        m = re.findall(r"hans_mood: Mood: \S+ → (\S+)", txt)
        return m[-1] if m else None
    except Exception:
        return None


def _is_sleeping():
    """Hans spí? Flag soubor od Hanse má přednost, jinak čas dle configu
    (sleep_start_hour..sleep_end_hour, přes půlnoc) = matchuje noční spánek."""
    if os.path.exists("data/.hans_sleeping"):
        return True
    cfg = _read_json("config.json") or {}
    sh = int(cfg.get("sleep_start_hour", 23))
    eh = int(cfg.get("sleep_end_hour", 9))
    hr = time.localtime().tm_hour
    return (hr >= sh or hr < eh) if sh > eh else (sh <= hr < eh)


def _cpu_temp():
    """ATTENTION_CYCLE_WIRING_V1 — teplota CPU (°C) z Pi, None když nelze."""
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            return round(int(f.read().strip()) / 1000.0, 1)
    except Exception:
        return None


# ── PC_TELEMETRY_DISPLAY_V1 — telemetrie PC na oba displeje za herního módu ──
def _game_mode():
    """Herní mód aktivní? (flag `data/.ollama_paused` od Ollama klienta)."""
    return os.path.exists("data/.ollama_paused")


_PC_TEL = {"data": None}          # sdílená cache (plní poller, čte render loop)
_PC_STOP = False


def _pc_poller(poll_s: float):
    """Na pozadí: když je herní mód, polluj telemetrii PC přes SSH (pc_remote).
    Blokující SSH tak NEZDRŽUJE render loop displejů."""
    try:
        from scripts import pc_remote as pcr
    except Exception as e:
        print("pc_remote nedostupný:", e)
        return
    while not _PC_STOP:
        if _game_mode():
            cfg = _read_json("config.json") or {}
            try:
                _PC_TEL["data"] = pcr.telemetry(cfg)
            except Exception:
                _PC_TEL["data"] = None
            time.sleep(poll_s)
        else:
            _PC_TEL["data"] = None
            time.sleep(1.0)


def gather_ctx():
    """Bohatý ctx z attention_context.json (publikuje Hans), jinak fallback nálada.
    ATTENTION_CYCLE_WIRING_V1: vždy doplní clock + cpu_temp (Pi-lokální)."""
    ctx = _read_json(CTX_F)
    if not (isinstance(ctx, dict) and ctx):
        ctx = {"mood": _mood_from_log() or "content", "phase": ""}
    ctx.setdefault("clock", time.strftime("%H:%M"))
    ctx["cpu_temp"] = _cpu_temp()
    return ctx


def main():
    # DAEMON_BLANK_ON_TERM_V1 — run.sh ukončuje daemon přes `kill` (SIGTERM),
    # na který Python neproběhne finally → displeje zůstanou zamrzlé na
    # posledním snímku. Převedeme SIGTERM na KeyboardInterrupt, ať se spustí
    # úklid (černá + DISPOFF).
    def _on_term(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, _on_term)

    h = lgpio.gpiochip_open(0)
    for pin in (DC, RST1, RST2, CS1, CS2):
        lgpio.gpio_claim_output(h, pin, 0)
    spi = spidev.SpiDev(); spi.open(0, 0); spi.max_speed_hz = 40_000_000; spi.mode = 0
    face = GC9A01(h, spi, CS2, RST2, rotation=2)   # LEVÝ = tvář (rot90 v SW: k=3)
    attn = GC9A01(h, spi, CS1, RST1, rotation=2)   # PRAVÝ = pozornost (rot90 SW: k=1)
    face.init(); attn.init()
    print("Dual-display daemon: LEVÝ=tvář, PRAVÝ=pozornost. Ctrl-C ukončí.")

    fs = FaceSource()
    ar = AttentionRenderer(asset_dir=AV_DIR)
    fps = 10.0
    last_attn = 0.0
    last_sleep_chk = 0.0
    attn_cache = None
    # ATTENTION_CYCLE_WIRING_V1 — rotace karet á cycle_s
    _adcfg = (_read_json("config.json") or {}).get("attention_display", {})
    cycle_s = float(_adcfg.get("cycle_s", 10))
    cycle_idx = 0
    last_switch = 0.0
    sleeping = None        # None = ještě nezjištěno
    # PC_TELEMETRY_DISPLAY_V1 — herní mód → oba displeje telemetrie PC
    tel_poll_s = float(_adcfg.get("telemetry_poll_s", 3))
    tel_cycle_s = float(_adcfg.get("telemetry_cycle_s", 4))
    threading.Thread(target=_pc_poller, args=(tel_poll_s,), daemon=True).start()
    gaming = None
    last_game_chk = 0.0
    tel_idx = 0
    tel_switch = 0.0
    tel_sig = None        # podpis zobrazené telemetrie → překresli jen při změně
    try:
        while True:
            t0 = time.time()
            # spánek — kontrola á 5s (off/on přes GC9A01 0x28/0x29)
            if t0 - last_sleep_chk >= 5.0 or sleeping is None:
                last_sleep_chk = t0
                slp = _is_sleeping()
                if slp != sleeping:
                    sleeping = slp
                    for d in (face, attn):
                        try:
                            d._write_cmd(0x28 if slp else 0x29)   # DISPOFF / DISPON
                        except Exception:
                            pass
                    print("Hans spí → displeje OFF" if slp else "Hans vzhůru → displeje ON")
            if sleeping:
                time.sleep(2.0)   # spí → nekreslíme
                continue
            # PC_TELEMETRY_DISPLAY_V1 — herní mód: oba displeje = telemetrie PC
            if t0 - last_game_chk >= 2.0 or gaming is None:
                last_game_chk = t0
                ng = _game_mode()
                if ng != gaming:
                    gaming = ng
                    tel_sig = None   # při přepnutí vždy překresli
                    print("Herní mód → displeje = telemetrie PC" if ng
                          else "Herní mód konec → normál (avatar + Pi)")
            if gaming:
                tel = _PC_TEL.get("data")
                if t0 - tel_switch >= tel_cycle_s:
                    tel_idx += 1
                    tel_switch = t0
                metrics = ["cpu", "gpu_t", "vram", "ram", "fan"]
                li = metrics[tel_idx % len(metrics)]
                ri = metrics[(tel_idx + 2) % len(metrics)]
                # podpis ze ZOBRAZENÝCH (zaokrouhlených) hodnot → překresli jen
                # když se reálně změní číslo na displeji (ne 10×/s, žádné blikání)
                def _rnd(k, nd=0):
                    v = (tel or {}).get(k)
                    return None if v is None else round(v, nd)
                sig = (li, ri, _rnd("cpu_temp_c"), _rnd("gpu_hotspot_c"),
                       _rnd("gpu_power_w"), _rnd("vram_used_gb"),
                       _rnd("ram_used_gb"), _rnd("gpu_fan_rpm"), tel is None)
                if sig != tel_sig:
                    tel_sig = sig
                    try:
                        if tel:
                            face.send_frame(to_rgb565(ar.render_telemetry_card(tel, li), rot90=3))
                            attn.send_frame(to_rgb565(ar.render_telemetry_card(tel, ri), rot90=1))
                        else:
                            face.send_frame(to_rgb565(ar.render_telemetry_placeholder("PC…"), rot90=3))
                            attn.send_frame(to_rgb565(ar.render_telemetry_placeholder("PC…"), rot90=1))
                    except Exception as e:
                        print("telemetry render err:", e)
                time.sleep(0.2)   # statické mezi změnami → nízká zátěž
                continue
            # tvář (LEVÝ) — každý frame (animace klipů), rot90 k=3
            try:
                face.send_frame(to_rgb565(fs.frame(), rot90=3))
            except Exception as e:
                print("face err:", e)
            # pozornost (PRAVÝ) — cyklus karet á cycle_s, re-render á 0.5s, rot90 k=1
            if t0 - last_attn >= 0.5 or attn_cache is None:
                try:
                    ctx = gather_ctx()
                    ov = ar.override_card(ctx)  # proactive/kolac přeruší cyklus
                    if ov:
                        card = ov
                        cycle_idx, last_switch = 0, t0
                    else:
                        cards = ar.cycle_cards(ctx) or ["mood"]
                        if t0 - last_switch >= cycle_s:
                            cycle_idx += 1
                            last_switch = t0
                        card = cards[cycle_idx % len(cards)]
                    attn_cache = to_rgb565(ar.render_card(ctx, card), rot90=1)
                except Exception as e:
                    print("attn render err:", e)
                last_attn = t0
            if attn_cache:
                try:
                    attn.send_frame(attn_cache)
                except Exception as e:
                    print("attn err:", e)
            dt = time.time() - t0
            time.sleep(max(0, 1.0 / fps - dt))
    except KeyboardInterrupt:
        print("\nKončím…")
    finally:
        # DAEMON_BLANK_ON_TERM_V1 — černá + vypnutí panelu (DISPOFF), ať po
        # konci programu nezůstane zamrzlý poslední snímek.
        black = to_rgb565(np.zeros((H, W, 3), np.uint8))
        for d in (face, attn):
            try:
                d.send_frame(black)
                d._write_cmd(0x28)   # DISPOFF
            except Exception:
                pass
        spi.close(); lgpio.gpiochip_close(h)
        print("Hotovo.")


if __name__ == "__main__":
    main()
