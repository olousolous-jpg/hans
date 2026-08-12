"""HANS_HEALTH_V1 — živý watchdog závislostí + sebe-uzdravení.

Hans je bohatý na featury, ale KŘEHKÝ na infrastruktuře: Ollama se zasekne
(chat 120s timeout), ComfyUI spadne (malování mlčky nefunguje), qwen-VL/llava
hodí EOF (zrak tiše rozbitý), PC usne. Dosud to uživatel odhaloval RUČNĚ.

Tento modul dává Hansovi VĚDOMÍ o zdraví vlastních nástrojů:
  - `probe_all(config)` — reálně proboduje závislosti (deferral-safe, nikdy nehází)
  - klíč = ROZLIŠENÍ Ollama stavů: ok / paused(herní mód) / wedged / down.
    „wedged" = server žije (/api/tags odpoví), ale INFERENCE visí → `/api/tags`
    to NEodhalí, proto zkoušíme malou generaci. To je jediný self-heal kandidát.
  - `heal_ollama(config)` — restart zaseklé Ollamy na PC přes SSH (scoped sudo).
  - `run_health_check(config, ...)` — probe → volitelně heal → shrnutí + stav JSON.

BEZ LLM (kromě 1 triviální ping-generace k detekci wedge). Vzor pc_night_shutdown.
"""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path
from typing import Optional

from scripts.logger import get_logger

_log = get_logger("hans_health")

_ROOT = Path(__file__).resolve().parent.parent
_STATE_PATH = _ROOT / "data" / "health_state.json"

# stavy služby
OK = "ok"
PAUSED = "paused"      # záměrně vypnuto (herní mód)
WEDGED = "wedged"      # server žije, ale visí → self-heal kandidát
DOWN = "down"          # nedostupné (PC spí / služba neběží)
UNKNOWN = "unknown"
WARN = "warn"          # behaviorální varování (rozvrh zaostává) — NEspouští heal


def _cfg(config: dict) -> dict:
    return (config or {}).get("health", {}) or {}


def enabled(config: dict) -> bool:
    return bool(_cfg(config).get("enabled", True))


# ── Ollama: ok / paused / wedged / down ──────────────────────────────────────
def probe_ollama(config: dict) -> dict:
    """Reálná probe: nejdřív zkus TRIVIÁLNÍ inference (odhalí wedge). Když selže,
    rozliš herní mód (paused) vs. server žije-ale-visí (wedged) vs. mrtvý (down)."""
    t0 = time.time()
    try:
        from scripts.ollama_client import game_mode_on
        if game_mode_on():
            return {"status": PAUSED, "detail": "herní mód", "latency_s": 0.0}
    except Exception:
        pass
    # 1) malá generace = reálný test, že engine odpovídá (ne jen HTTP server)
    raw = None
    try:
        from scripts.ollama_client import ollama_generate
        model = ((config.get("dialog", {}) or {}).get("model")
                 or "hans-czech:latest")
        to = int(_cfg(config).get("ollama_probe_timeout", 25))
        raw = ollama_generate(model, "ok", config=config, timeout=to,
                              keep_alive=-1, options={"num_predict": 1,
                                                      "temperature": 0})
    except Exception as e:
        _log.debug("probe_ollama generate: %s", e)
    lat = round(time.time() - t0, 1)
    if raw is not None and str(raw).strip() != "":
        return {"status": OK, "detail": "inference ok", "latency_s": lat}
    # 2) inference selhala — žije aspoň HTTP server? (rozliš wedged vs down)
    try:
        import requests
        from scripts.ollama_client import _resolve_url
        url = _resolve_url(None, config)
        r = requests.get(f"{url}/api/tags", timeout=6)
        if r.ok:
            return {"status": WEDGED, "detail": "server žije, inference visí",
                    "latency_s": lat}
    except Exception as e:
        _log.debug("probe_ollama tags: %s", e)
    return {"status": DOWN, "detail": "Ollama nedostupná", "latency_s": lat}


# ── ComfyUI (malování/avatar) ────────────────────────────────────────────────
def comfy_alive(config: dict, timeout: float = 6.0) -> bool:
    """HANS_COMFY_WEDGE_V1 — odpovídá ComfyUI HTTP? (rychlá brána před renderem)"""
    try:
        import requests
        from scripts.avatar_render import _comfy_url
        r = requests.get("%s/system_stats" % _comfy_url(config), timeout=timeout)
        return bool(r.ok)
    except Exception:
        return False


def probe_comfyui(config: dict) -> dict:
    """HANS_COMFY_WEDGE_V1 (4.8.) — rozliš ZATUHLÝ ComfyUI od vypnutého.

    Doloženo 4.8.: port 8188 přijímal TCP spojení, ale HTTP neodpovídalo →
    render se nepovažoval za chybu a čekalo se celý `render_timeout` (nejdřív
    600 s, po zvednutí 900 s) → obraz „se nevyrenderoval". Zvyšování timeoutu
    tenhle případ NEŘEŠÍ, protože server nebyl pomalý, ale zaseklý.
    Rozlišení: TCP se spojí, ale HTTP mlčí = WEDGED (kandidát na self-heal);
    TCP se nespojí = DOWN (PC spí / služba neběží)."""
    try:
        import requests
        from scripts.avatar_render import _comfy_url
        url = _comfy_url(config)
        r = requests.get(f"{url}/system_stats", timeout=6)
        if r.ok:
            return {"status": OK, "detail": "system_stats ok"}
        return {"status": WEDGED, "detail": "HTTP %s" % r.status_code}
    except Exception as e:
        if _tcp_open(config):
            return {"status": WEDGED,
                    "detail": "port žije, HTTP neodpovídá (%s)" % str(e)[:50]}
        return {"status": DOWN, "detail": str(e)[:80]}


def _tcp_open(config: dict, timeout: float = 3.0) -> bool:
    """Přijímá ComfyUI aspoň TCP spojení? (rozlišuje wedge od vypnuté služby)"""
    try:
        import socket
        from urllib.parse import urlparse
        from scripts.avatar_render import _comfy_url
        u = urlparse(_comfy_url(config))
        with socket.create_connection((u.hostname, u.port or 8188), timeout):
            return True
    except Exception:
        return False


# ── Kodi (media) ─────────────────────────────────────────────────────────────
def probe_kodi(config: dict) -> dict:
    try:
        from scripts.kodi_client import KodiClient
        kc = KodiClient(config)
        res = kc._call("JSONRPC.Ping")
        if res is not None:
            return {"status": OK, "detail": "ping ok"}
        return {"status": DOWN, "detail": "bez odpovědi"}
    except Exception as e:
        return {"status": DOWN, "detail": str(e)[:80]}


# ── STT / Whisper (hlas) ─────────────────────────────────────────────────────
def probe_stt(config: dict) -> dict:
    url = (config.get("voice", {}) or {}).get("stt_url")
    if not url:
        return {"status": UNKNOWN, "detail": "stt_url nenastaveno"}
    try:
        import requests
        from urllib.parse import urlparse
        p = urlparse(url)
        base = "%s://%s" % (p.scheme, p.netloc)
        # /health je lehký; endpoint samotný vyžaduje audio → jen dostupnost hostu
        r = requests.get(f"{base}/health", timeout=6)
        if r.ok:
            return {"status": OK, "detail": "host ok"}
        return {"status": DOWN, "detail": "HTTP %s" % r.status_code}
    except Exception as e:
        return {"status": DOWN, "detail": str(e)[:80]}


# ── PC (SSH) ─────────────────────────────────────────────────────────────────
def probe_pc(config: dict) -> dict:
    try:
        from scripts import pc_remote
        if not pc_remote.enabled(config):
            return {"status": UNKNOWN, "detail": "pc_remote vypnut"}
        out = pc_remote.run(config, "echo ok", timeout=8)
        if out is not None and "ok" in str(out):
            return {"status": OK, "detail": "ssh ok"}
        return {"status": DOWN, "detail": "PC spí / SSH neodpovídá"}
    except Exception as e:
        return {"status": DOWN, "detail": str(e)[:80]}


# ── Disk (Pi) ────────────────────────────────────────────────────────────────
def probe_disk(config: dict) -> dict:
    try:
        total, used, free = shutil.disk_usage(str(_ROOT))
        free_gb = round(free / 1e9, 1)
        min_gb = float(_cfg(config).get("min_disk_gb", 2.0))
        st = OK if free_gb >= min_gb else DOWN
        return {"status": st, "detail": "%.1f GB volných" % free_gb,
                "free_gb": free_gb}
    except Exception as e:
        return {"status": UNKNOWN, "detail": str(e)[:80]}


# ── Kamera (zrak) ────────────────────────────────────────────────────────────
def probe_camera(config: dict) -> dict:
    """CAMERA_STALL_RECOVERY_V1 — zrak. Čte živý heartbeat z main loopu
    (data/.hans_heartbeat: {ts, camera, stall_s, recover_fails}).

    Senzor imx708 umí odpadnout z I2C (slunce/přehřátí) — pak Hans běží dál,
    ale je slepý. Bez tohohle to nikdo nepozná (dřív navíc celý zamrzl)."""
    hb = _ROOT / "data" / ".hans_heartbeat"
    try:
        if not hb.exists():
            return {"status": UNKNOWN, "detail": "Hans neběží / starý build"}
        raw = hb.read_text().strip()
        age = time.time() - hb.stat().st_mtime
        if age > 60:
            return {"status": UNKNOWN,
                    "detail": "Hans neběží (tep před %d s)" % int(age)}
        try:
            st = json.loads(raw)
        except Exception:
            return {"status": UNKNOWN, "detail": "starý formát tepu"}

        cam   = st.get("camera", "unknown")
        fails = int(st.get("recover_fails", 0) or 0)
        stall = int(st.get("stall_s", 0) or 0)
        if cam == "ok":
            return {"status": OK, "detail": "vidí"}
        if cam == "paused":
            return {"status": PAUSED, "detail": "spánek — zrak vypnutý"}
        if cam == "stalled":
            return {"status": WEDGED,
                    "detail": "výpadek %d s — zkouším obnovit" % stall}
        if cam == "dead":
            return {"status": DOWN,
                    "detail": "senzor odpadl (slunce/přehřátí) — %d× marná obnova"
                              % fails}
        return {"status": UNKNOWN, "detail": str(cam)[:60]}
    except Exception as e:
        return {"status": UNKNOWN, "detail": str(e)[:80]}


# ── behaviorální sebe-audit ──────────────────────────────────────────────────
def probe_schedule(config: dict) -> dict:
    """HANS_SCHEDULE_V1 — behaviorální sebe-audit: prošly autonomní rutiny?
    Status WARN (ne WEDGED/DOWN → nespouští heal), obsahuje seznam stale.
    Odchytí tiché selhání typu „studium visí 14 dní" (Design 8/12 → 1.7.)."""
    try:
        from scripts.hans_schedule import ScheduleStore
        import os
        db = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "data", "hans_diary.db")
        st = ScheduleStore(db)
        stale = st.stale_list()
        if not stale:
            return {"status": OK, "detail": "všechny rutiny běží podle plánu",
                    "stale": []}
        # WARN, ne DOWN — audit hlásí, ne restartuje
        worst = stale[0]
        return {"status": WARN,
                "detail": f"{len(stale)} rutin zaostává (nejhorší: "
                          f"{worst['name']} o {worst['late_s']/3600:.1f}h)",
                "stale": stale}
    except Exception as e:
        return {"status": UNKNOWN, "detail": str(e)[:80], "stale": []}


def probe_pc_reboots(config: dict) -> dict:
    """PC_REBOOT_WATCH_V1 — restartuje se PC častěji, než má?

    PROČ (uživatel 12.8.): zapnuli jsme na PC hardwarový watchdog (`sp5100_tco`)
    kvůli nočnímu zamrznutí. Jenže implementace watchdogu bývá na některých
    deskách vadná a umí resetovat BEZ PŘÍČINY — a takový planý reset by jinak
    nikdo nezaznamenal, protože stroj se vždycky vrátí.

    ⚠️ PROČ NE `brain_down`: ten je zašuměný. Měřeno na 292 spárovaných
    výpadcích — medián 3 denně, z toho 129 kratších než 2 minuty (výpadky
    Ollamy, ne restarty stroje). Ani počet, ani délka výpadku mozku restart
    desky nerozliší. **Jednoznačný je počet BOOTŮ**, a ten se dá přečíst
    přímo z PC.

    Bere se `journalctl --list-boots`, kde je u každého bootu první i poslední
    záznam → naráz vyjde POČET bootů za 24 h i jejich DÉLKA.
    Normálně jsou 2 (buzení ve 3:00 na analytiku + ranní), takže:
      • víc než `max_boots_24h` (výchozí 4)  → něco stroj restartuje
      • boot kratší než 5 minut             → nabootoval a hned umřel
        (přesně signatura noci 12.8.: boot v 03:01:05, konec v 03:01:19)

    Status WARN — hlásí, NEspouští heal. Restartovat PC kvůli tomu, že se
    restartuje, by bylo směšné.

    Dotaz jde přes SSH, takže se drží cache (`_reboot_cache`) a ptá se
    nanejvýš jednou za hodinu — health check běží mnohem častěji.
    """
    import time as _t
    global _reboot_cache
    now = _t.time()
    if _reboot_cache and now - _reboot_cache[0] < 3600:
        return _reboot_cache[1]
    try:
        from scripts import pc_remote
        if not pc_remote.enabled(config):
            return {"status": UNKNOWN, "detail": "pc_remote vypnut"}
        out = pc_remote.run(
            config, "journalctl --list-boots --no-pager 2>/dev/null | tail -12",
            timeout=15)
        if not out:
            # PC spí — to není chyba, jen o něm teď nic nevíme
            return {"status": UNKNOWN, "detail": "PC nedostupné"}

        import re
        from datetime import datetime as _dt
        pat = re.compile(r"([A-Z][a-z]{2}\s+\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})")
        recent, short = 0, []
        for line in str(out).splitlines():
            times = pat.findall(line)
            if len(times) < 2:
                continue
            try:
                t0 = _dt.strptime(times[0][4:], "%Y-%m-%d %H:%M:%S")
                t1 = _dt.strptime(times[1][4:], "%Y-%m-%d %H:%M:%S")
            except Exception:
                continue
            age_h = (now - t0.timestamp()) / 3600.0
            if 0 <= age_h <= 24:
                recent += 1
                mins = (t1 - t0).total_seconds() / 60.0
                if mins < 5:
                    short.append(round(mins, 1))

        cfg = _cfg(config)
        mx = int(cfg.get("max_boots_24h", 4))
        if short:
            res = {"status": WARN, "boots_24h": recent, "short": short,
                   "detail": f"PC nabootovalo a do {short[0]} min umřelo "
                             f"({len(short)}× za 24 h) — zamrznutí nebo planý reset"}
        elif recent > mx:
            res = {"status": WARN, "boots_24h": recent, "short": [],
                   "detail": f"PC se za 24 h restartovalo {recent}× (běžně 2) "
                             f"— podezření na planý reset watchdogu"}
        else:
            res = {"status": OK, "boots_24h": recent, "short": [],
                   "detail": f"{recent} bootů za 24 h (normál)"}
        _reboot_cache = (now, res)
        return res
    except Exception as e:
        return {"status": UNKNOWN, "detail": str(e)[:80]}


_reboot_cache = None


# ── agregace ─────────────────────────────────────────────────────────────────
def probe_all(config: dict) -> dict:
    """Proboduje všechny závislosti. Vrací {service: {status, detail, ...}}.
    Deferral-safe — každá probe je try/except, žádná neshodí celek."""
    checks = {
        "ollama": probe_ollama,
        "camera": probe_camera,
        "comfyui": probe_comfyui,
        "kodi": probe_kodi,
        "stt": probe_stt,
        "pc": probe_pc,
        "disk": probe_disk,
        "schedule": probe_schedule,  # HANS_SCHEDULE_V1 (behaviorální)
        "pc_reboots": probe_pc_reboots,  # PC_REBOOT_WATCH_V1 (planý reset watchdogu)
    }
    only = _cfg(config).get("probes")  # volitelně podmnožina
    out = {}
    for name, fn in checks.items():
        if only and name not in only:
            continue
        try:
            out[name] = fn(config)
        except Exception as e:
            out[name] = {"status": UNKNOWN, "detail": str(e)[:80]}
    return out


def degraded_services(health: dict) -> list:
    """Služby, které jsou reálně rozbité (wedged/down) — ne paused/unknown.
    paused = záměr (herní mód), unknown = nemáme jak změřit → nehlásíme jako vadu."""
    return [n for n, s in (health or {}).items()
            if s.get("status") in (WEDGED, DOWN)]


# ── self-heal ────────────────────────────────────────────────────────────────
def heal_ollama(config: dict) -> bool:
    """Restart zaseklé Ollamy na PC přes SSH. Vyžaduje na PC scoped sudoers:
      <user> ALL=(ALL) NOPASSWD: /usr/bin/systemctl restart ollama
    Vrací True když příkaz prošel. Konzervativní — volá se JEN na status WEDGED."""
    if not _cfg(config).get("self_heal_ollama", True):
        _log.info("health: self-heal Ollamy vypnut configem")
        return False
    try:
        from scripts import pc_remote
        if not pc_remote.enabled(config):
            return False
        cmd = _cfg(config).get("ollama_restart_cmd",
                               "sudo -n systemctl restart ollama")
        _log.warning("health: Ollama WEDGED → restartuji na PC (%s)", cmd)
        out = pc_remote.run(config, cmd, timeout=30)
        _log.info("health: restart Ollamy odeslán (out=%r)", out)
        return out is not None
    except Exception as e:
        _log.warning("health: self-heal Ollamy selhal: %s", e)
        return False


def heal_comfyui(config: dict) -> bool:
    """HANS_COMFY_WEDGE_V1 — restart zatuhlého ComfyUI na PC přes SSH.

    Na rozdíl od Ollamy je to systemd USER služba (`comfyui.service`), takže
    NEPOTŘEBUJE sudo ani sudoers záznam. Volá se JEN na status WEDGED."""
    if not _cfg(config).get("self_heal_comfyui", True):
        _log.info("health: self-heal ComfyUI vypnut configem")
        return False
    try:
        from scripts import pc_remote
        if not pc_remote.enabled(config):
            return False
        cmd = _cfg(config).get("comfyui_restart_cmd",
                               "systemctl --user restart comfyui")
        _log.warning("health: ComfyUI WEDGED → restartuji na PC (%s)", cmd)
        out = pc_remote.run(config, cmd, timeout=30)
        _log.info("health: restart ComfyUI odeslán (out=%r)", out)
        return out is not None
    except Exception as e:
        _log.warning("health: self-heal ComfyUI selhal: %s", e)
        return False


# ── stav pro dashboard / surfacing ───────────────────────────────────────────
def _write_state(health: dict, healed: list) -> None:
    try:
        _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = {"ts": time.time(), "services": health,
                   "degraded": degraded_services(health), "healed": healed}
        tmp = _STATE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(_STATE_PATH)
    except Exception as e:
        _log.debug("health: zápis stavu selhal: %s", e)


def read_state() -> Optional[dict]:
    try:
        return json.loads(_STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None


def _schedule_sentence(health: dict) -> str:
    """HANS_SCHEDULE_V1 — behaviorální varování (1. osoba, konkrétní)."""
    sch = (health or {}).get("schedule") or {}
    stale = sch.get("stale") or []
    if not stale:
        return ""
    if len(stale) == 1:
        s = stale[0]
        return "Rozvrh: rutina '%s' zaostává o %.0fh." % (
            s['name'], s['late_s'] / 3600)
    return "Rozvrh: %d rutin zaostává (nejhůř '%s' o %.0fh)." % (
        len(stale), stale[0]['name'], stale[0]['late_s'] / 3600)


def _reboot_sentence(health: dict) -> str:
    """PC_REBOOT_WATCH_V1 — planý reset se pozná jen tím, že si ho někdo všimne."""
    r = (health or {}).get("pc_reboots") or {}
    if r.get("status") != WARN:
        return ""
    if r.get("short"):
        return ("Počítač po zapnutí do %s minuty spadl (%d× za den) — "
                "buď zamrzá, nebo ho zbytečně resetuje watchdog."
                % (r["short"][0], len(r["short"])))
    return ("Počítač se za den restartoval %dkrát, obvykle stačí dvakrát — "
            "možná plane spouští watchdog." % r.get("boots_24h", 0))


def summary_sentence(health: dict, healed: list) -> str:
    """Krátká věta pro Hansovo surfacing (1. osoba, upřímně)."""
    bad = degraded_services(health)
    sched = _schedule_sentence(health)
    reb = _reboot_sentence(health)      # PC_REBOOT_WATCH_V1
    if reb:
        sched = (sched + " " + reb).strip()
    if not bad:
        return sched  # čistá dependency, jen rozvrh/restarty (nebo prázdno)
    labels = {"ollama": "můj mozek (Ollama)", "comfyui": "malování (ComfyUI)",
              "kodi": "televize (Kodi)", "stt": "sluch (přepis řeči)",
              "pc": "počítač", "disk": "místo na disku"}
    parts = [labels.get(b, b) for b in bad]
    s = "Zaznamenal jsem potíž: " + ", ".join(parts) + "."
    if "ollama" in healed:
        s += " Zkusil jsem svůj mozek restartovat."
    if sched:
        s += " " + sched
    return s


def run_health_check(config: dict, heal: bool = True) -> dict:
    """Hlavní vstup: probe → (volitelně) heal wedged Ollamu → zapiš stav.
    Vrací {health, healed, degraded}. Deferral-safe."""
    if not enabled(config):
        return {"health": {}, "healed": [], "degraded": []}
    health = probe_all(config)
    healed = []
    if heal and health.get("ollama", {}).get("status") == WEDGED:
        if heal_ollama(config):
            healed.append("ollama")
    # HANS_COMFY_WEDGE_V1 — zatuhlý ComfyUI jinak blokuje malování až do ručního
    # zásahu (render čeká celý timeout a spadne na horší fallback).
    if heal and health.get("comfyui", {}).get("status") == WEDGED:
        if heal_comfyui(config):
            healed.append("comfyui")
    _write_state(health, healed)
    bad = degraded_services(health)
    if bad:
        _log.warning("health: degradované služby: %s (healed=%s)", bad, healed)
    else:
        _log.info("health: vše ok")
    return {"health": health, "healed": healed, "degraded": bad}


if __name__ == "__main__":
    import sys
    cfg = json.loads((_ROOT / "config.json").read_text(encoding="utf-8"))
    do_heal = "--heal" in sys.argv
    res = run_health_check(cfg, heal=do_heal)
    for name, st in res["health"].items():
        print("%-9s %-8s %s" % (name, st.get("status"), st.get("detail", "")))
    if res["degraded"]:
        print("\ndegradováno:", res["degraded"], "| healed:", res["healed"])
        print("věta:", summary_sentence(res["health"], res["healed"]))
    else:
        print("\nvše ok")
