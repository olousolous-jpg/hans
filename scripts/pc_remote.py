#!/usr/bin/env python3
"""pc_remote.py — PC_REMOTE_SSH_V1

SSH most z Pi na PC (254) pro REÁLNOU telemetrii (sensors + rocm-smi) a
řízené akce (restart Ollamy). Klíč na Pi `~/.ssh/hans_pc`, user/host z configu.

Deferral-safe: PC spí / SSH selže → funkce vrací None (žádná výjimka ven).
`rocm-smi` NENÍ v PATH neinteraktivního SSH → voláme plnou cestu /opt/rocm/bin.
"""
from __future__ import annotations

import os
import re
import shlex
import logging
import subprocess

_log = logging.getLogger("pc_remote")

_ROCM = "/opt/rocm/bin/rocm-smi"


def _cfg(config: dict) -> dict:
    return (config or {}).get("pc_remote", {}) or {}


def enabled(config: dict) -> bool:
    return bool(_cfg(config).get("enabled", True))


def _ssh_base(config: dict) -> list:
    c = _cfg(config)
    key = os.path.expanduser(str(c.get("key_path", "~/.ssh/hans_pc")))
    host = str(c.get("host", "192.168.1.10"))
    user = str(c.get("user", "user"))
    to = int(c.get("ssh_timeout", 8))
    return ["ssh", "-i", key, "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", f"ConnectTimeout={to}", f"{user}@{host}"]


def wake(config: dict = None, mac: str = None) -> bool:
    """HANS_WOL_SHARED_V1 — Wake-on-LAN: pošle magic packet (UDP broadcast :9).
    Jediná pravda pro WOL (sdílí telegram most i noční rutina). MAC vezme z
    argumentu, jinak z config['wol_pc_mac']. Vrací True, když se packet odeslal
    (NEověřuje, že PC naběhl — to řeší ping u volajícího)."""
    import socket
    if not mac:
        mac = str((config or {}).get("wol_pc_mac", "") or "")
    m = str(mac).replace(":", "").replace("-", "").lower()
    if len(m) != 12 or not all(c in "0123456789abcdef" for c in m):
        return False
    packet = bytes.fromhex("FF" * 6 + m * 16)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.sendto(packet, ("255.255.255.255", 9))
        return True
    except Exception:
        return False
    finally:
        s.close()


def run(config: dict, remote_cmd: str, timeout: int | None = None):
    """Spusť příkaz na PC přes SSH. Vrátí stdout (str) nebo None při selhání
    (PC spí / SSH chyba / nenulový exit). Deferral-safe."""
    if not enabled(config):
        return None
    to = int(timeout or _cfg(config).get("ssh_timeout", 8))
    try:
        r = subprocess.run(_ssh_base(config) + [remote_cmd],
                           capture_output=True, text=True, timeout=to + 4)
    except Exception as e:
        _log.info("pc_remote.run selhal (PC spí?): %s", e)
        return None
    if r.returncode != 0 and not (r.stdout or "").strip():
        _log.info("pc_remote.run exit=%s: %s", r.returncode,
                  (r.stderr or "").strip()[:120])
        return None
    return r.stdout


# ── telemetrie ───────────────────────────────────────────────────────────────
_TELE_CMD = (
    "sensors 2>/dev/null | grep -iE 'Tctl|edge:|junction:|^mem:|fan1:|PPT:'; "
    "echo '===VRAM==='; "
    f"{_ROCM} --showmeminfo vram 2>&1 | grep -i 'VRAM Total'; "
    "echo '===RAM==='; free -b | grep '^Mem'"
)


def _f(pattern: str, text: str):
    m = re.search(pattern, text, re.IGNORECASE | re.MULTILINE)
    return float(m.group(1)) if m else None


def telemetry(config: dict):
    """Reálná telemetrie PC. Vrátí dict, nebo None když PC nedostupné.
    Klíče (co je k dispozici): cpu_temp_c, gpu_edge_c, gpu_hotspot_c,
    gpu_mem_c, gpu_fan_rpm, gpu_power_w, gpu_power_cap_w, vram_total_gb,
    vram_used_gb, vram_free_gb, ram_total_gb, ram_used_gb, ram_avail_gb."""
    out = run(config, _TELE_CMD, timeout=int(_cfg(config).get("ssh_timeout", 8)))
    if not out:
        return None
    parts = out.split("===VRAM===")
    sens = parts[0]
    rest = parts[1] if len(parts) > 1 else ""
    vram_txt, ram_txt = (rest.split("===RAM===") + [""])[:2]
    t: dict = {}
    t["cpu_temp_c"] = _f(r"Tctl:\s*\+?([\d.]+)", sens)
    t["gpu_edge_c"] = _f(r"edge:\s*\+?([\d.]+)", sens)
    t["gpu_hotspot_c"] = _f(r"junction:\s*\+?([\d.]+)", sens)
    t["gpu_mem_c"] = _f(r"^mem:\s*\+?([\d.]+)", sens)
    t["gpu_fan_rpm"] = _f(r"fan1:\s*([\d.]+)", sens)
    t["gpu_power_w"] = _f(r"PPT:\s*([\d.]+)", sens)
    t["gpu_power_cap_w"] = _f(r"cap\s*=\s*([\d.]+)", sens)
    vt = _f(r"VRAM Total Memory \(B\):\s*(\d+)", vram_txt)
    vu = _f(r"VRAM Total Used Memory \(B\):\s*(\d+)", vram_txt)
    if vt:
        t["vram_total_gb"] = round(vt / 1e9, 1)
    if vu is not None:
        t["vram_used_gb"] = round(vu / 1e9, 1)
    if vt and vu is not None:
        t["vram_free_gb"] = round((vt - vu) / 1e9, 1)
    m = re.search(r"^Mem:\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)",
                  ram_txt, re.MULTILINE)
    if m:
        tot, used, _free, _sh, _bc, avail = (int(x) for x in m.groups())
        t["ram_total_gb"] = round(tot / 1e9, 1)
        t["ram_used_gb"] = round(used / 1e9, 1)
        t["ram_avail_gb"] = round(avail / 1e9, 1)
    # None když nic neparsováno (SSH vrátil junk)
    return t if any(v is not None for v in t.values()) else None


def gpu_vram(config: dict):
    """Jen GPU VRAM z rocm-smi (pro ověření herního módu). {total,used,free}_gb
    nebo None."""
    out = run(config, f"{_ROCM} --showmeminfo vram 2>&1 | grep -i 'VRAM Total'")
    if not out:
        return None
    vt = _f(r"VRAM Total Memory \(B\):\s*(\d+)", out)
    vu = _f(r"VRAM Total Used Memory \(B\):\s*(\d+)", out)
    if not vt or vu is None:
        return None
    return {"total_gb": round(vt / 1e9, 1), "used_gb": round(vu / 1e9, 1),
            "free_gb": round((vt - vu) / 1e9, 1)}


def display_lines(config: dict):
    """Krátké řádky pro cyklení na displeji. [] když PC nedostupné."""
    t = telemetry(config)
    if not t:
        return []
    L = []
    if t.get("cpu_temp_c") is not None:
        L.append(f"CPU {t['cpu_temp_c']:.0f}°C")
    if t.get("gpu_hotspot_c") is not None:
        p = f" {t['gpu_power_w']:.0f}W" if t.get("gpu_power_w") is not None else ""
        L.append(f"GPU {t['gpu_hotspot_c']:.0f}°C{p}")
    if t.get("vram_total_gb"):
        L.append(f"VRAM {t.get('vram_used_gb', 0):.1f}/{t['vram_total_gb']:.0f}GB")
    if t.get("ram_total_gb"):
        L.append(f"RAM {t.get('ram_used_gb', 0):.1f}/{t['ram_total_gb']:.0f}GB")
    if t.get("gpu_fan_rpm") is not None:
        L.append(f"Fan {t['gpu_fan_rpm']:.0f}rpm")
    return L


if __name__ == "__main__":
    import json
    cfg = {}
    try:
        cfg = json.load(open(os.path.join(
            os.path.dirname(__file__), "..", "config.json")))
    except Exception:
        pass
    print("telemetry:", json.dumps(telemetry(cfg), ensure_ascii=False))
    print("display_lines:", display_lines(cfg))
    print("gpu_vram:", gpu_vram(cfg))
