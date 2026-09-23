#!/usr/bin/env python3
"""hans_router.py — HANS_ROUTER_V1 (23. 9.)

Diagnostika domácího routeru a přepnutí VPN serveru, když tunnel zlobí.

⚠️ VĚDOMÁ ZMĚNA HRANICE: do 23. 9. platilo „Hans na router nesmí“
(rozhodnutí uživatele 23. 8.). Uživatel ji 23. 9. otevřel — ale úzce:
- vlastní účet na routeru se skupinou JEN pro čtení a ping (bez zápisu),
  přihlášení jen z Pi a jen klíčem (`router.key_path`);
- jediná změna, kterou smí udělat, je spustit skript `hans-vpn-dalsi`
  (na routeru má `dont-require-permissions`) — přepne na DALŠÍ server
  téhož státu. Nic jiného účet změnit nemůže (ověřeno: `identity set`
  i `script set` vrací `not enough permissions`);
- přepínat smí jen ZNÁMÉ osoby (příkaz) a automatika (`watch_tick`).

🔒 V poznámkách peerů na routeru leží PRIVÁTNÍ KLÍČE WireGuardu (`název|klíč`).
Tenhle modul poznámky NEČTE — název serveru vrací skript `hans-vpn-stav`,
který klíč odřízne. Pro jistotu se každý výstup z routeru ještě maskuje
(`_mask`), aby se klíč nedostal do odpovědi, logu ani deníku.

Deferral-safe: router nedosažitelný → funkce vrací None / prázdný stav,
žádná výjimka ven.
"""
from __future__ import annotations

import os
import re
import time
import logging
import subprocess
from typing import Optional

_log = logging.getLogger("hans_router")

# WireGuard klíč = 32 B v base64 → 43 znaků + „=“
_KEY_RE = re.compile(r"[A-Za-z0-9+/]{43}=")


def _cfg(config: dict) -> dict:
    return (config or {}).get("router", {}) or {}


def enabled(config: dict) -> bool:
    return bool(_cfg(config).get("enabled", False))


def _mask(text: str) -> str:
    return _KEY_RE.sub("<klíč>", text or "")


def _ssh(config: dict, cmd: str, timeout: float = 20.0) -> Optional[str]:
    c = _cfg(config)
    key = os.path.expanduser(str(c.get("key_path", "~/.ssh/hans_router_svc")))
    host = str(c.get("host", "192.168.1.1"))
    user = str(c.get("user", "hans"))
    args = ["ssh", "-i", key, "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", "ConnectTimeout=%d" % int(c.get("ssh_timeout", 6)),
            "%s@%s" % (user, host), cmd]
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except Exception as e:
        _log.debug("router ssh: %s", e)
        return None
    if r.returncode != 0 and not r.stdout:
        _log.debug("router ssh rc=%s: %s", r.returncode, _mask(r.stderr)[:200])
        return None
    return _mask(r.stdout)


def _pi_ping(config: dict) -> bool:
    """Jde internet z Pi? Pi chodí ven TÍMŽ tunelem jako celá domácnost,
    takže tohle je přesně to, co cítí uživatel."""
    targets = _cfg(config).get("ping_targets") or ["1.1.1.1", "9.9.9.9"]
    for t in targets:
        try:
            r = subprocess.run(["ping", "-c", "2", "-W", "2", "-i", "0.3", str(t)],
                               capture_output=True, timeout=10)
            if r.returncode == 0:
                return True
        except Exception:
            pass
    return False


def server_label(name: str) -> str:
    """„wg-CZ-36.conf“ → „CZ-36“."""
    n = (name or "").strip()
    if n.startswith("wg-"):
        n = n[3:]
    if n.endswith(".conf"):
        n = n[:-5]
    return n


def _parse_stav(out: str) -> dict:
    res = {"active": None, "handshake": None, "servers": []}
    for line in (out or "").splitlines():
        line = line.strip()
        if line.startswith("aktivni="):
            for part in line.split(";"):
                k, _, v = part.partition("=")
                if k == "aktivni":
                    res["active"] = server_label(v)
                elif k == "handshake":
                    res["handshake"] = v
        elif line.startswith("server="):
            res["servers"].append(server_label(line[7:]))
    return res


def _hs_s(v: Optional[str]) -> Optional[int]:
    """„00:01:05“ / „1m5s“ → sekundy."""
    if not v:
        return None
    m = re.fullmatch(r"(?:(\d+)d)?(\d+):(\d+):(\d+)", v)
    if m:
        d, h, mi, s = (int(x or 0) for x in m.groups())
        return ((d * 24 + h) * 60 + mi) * 60 + s
    tot, ok = 0, False
    for num, unit in re.findall(r"(\d+)([wdhms])", v):
        tot += int(num) * {"w": 604800, "d": 86400, "h": 3600, "m": 60, "s": 1}[unit]
        ok = True
    return tot if ok else None


def _dns_whoami(server: str, timeout: float = 3.0) -> Optional[str]:
    """Z jaké IP se ptá resolver, na který se obracíme (Akamai vrací IP
    tazatele u dotazu na `whoami.akamai.net`). Syrový UDP dotaz — na Pi není
    `dig` a nová závislost kvůli jednomu dotazu nestojí za to."""
    import random
    import socket
    import struct
    name = "whoami.akamai.net"
    tid = random.randint(0, 65535)
    pkt = (struct.pack(">HHHHHH", tid, 0x0100, 1, 0, 0, 0)
           + b"".join(bytes([len(x)]) + x.encode() for x in name.split("."))
           + b"\0" + struct.pack(">HH", 1, 1))
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    try:
        s.sendto(pkt, (server, 53))
        d, _ = s.recvfrom(4096)
        if struct.unpack(">H", d[:2])[0] != tid:
            return None
        an = struct.unpack(">H", d[6:8])[0]
        i = 12
        while d[i]:
            i += d[i] + 1
        i += 5
        for _ in range(an):
            if d[i] & 0xC0 == 0xC0:
                i += 2
            else:
                while d[i]:
                    i += d[i] + 1
                i += 1
            ty, _cl, _ttl, rl = struct.unpack(">HHIH", d[i:i + 10])
            i += 10
            if ty == 1 and rl == 4:
                return ".".join(str(b) for b in d[i:i + 4])
            i += rl
    except Exception:
        return None
    finally:
        s.close()
    return None


def _net24(ip: Optional[str]) -> Optional[str]:
    return ".".join(ip.split(".")[:3]) if ip and ip.count(".") == 3 else None


def dns_state(config: dict, public: Optional[str]) -> str:
    """'vpn' = domácnost se ptá přes Proton DNS v tunelu; 'leak' = resolver
    vychází z domácí IP poskytovatele; 'backup' = odpovídá záložní server
    (mimo tunel); 'unknown' = nešlo zjistit.
    Zjištěno 23. 9.: router se ptal `10.2.0.1` přes WAN (pravidla VPN platí
    jen pro LAN), padal na Google a ten viděl podsíť poskytovatele."""
    host = str(_cfg(config).get("host", "192.168.1.1"))
    via_router = _dns_whoami(host)
    if not via_router:
        return "unknown"
    proton = _dns_whoami(str(_cfg(config).get("vpn_dns", "10.2.0.1")))
    if proton and _net24(proton) == _net24(via_router):
        return "vpn"
    if public and _net24(public) == _net24(via_router):
        return "leak"
    return "backup"


_DNS_TXT = {"vpn": "přes VPN (Proton)",
            "backup": "záložní server mimo VPN — Proton DNS neodpovídá",
            "leak": "⚠️ MIMO VPN z domácí IP — poskytovatel vidí, kam se chodí",
            "unknown": "nezjištěno"}


_DIAG_CMD = (
    ':put ("uptime=" . [/system resource get uptime]);'
    ':put ("public=" . [/ip cloud get public-address]);'
    ':put ("wan=" . [/ping 1.1.1.1 count=3 interval=300ms]);'
    ':put ("wifi=" . [:len [/interface wireless registration-table find]]);'
    ':foreach i in=[/log find where message~"rebooted without proper shutdown"]'
    ' do={:put ("unclean=" . [/log get $i time])};'
    ':foreach i in=[/log find where message~"Handshake for peer did not complete"]'
    ' do={:put ("hsf=" . [/log get $i time])};'
    '/system script run hans-vpn-stav'
)


def _ts(v: str) -> Optional[float]:
    for fmt in ("%Y-%m-%d %H:%M:%S",):
        try:
            return time.mktime(time.strptime(v.strip(), fmt))
        except Exception:
            pass
    return None


def diagnose(config: dict) -> dict:
    """Stav linky a tunelu. `reachable=False` = na router se nedostanu."""
    tunnel = _pi_ping(config)
    out = _ssh(config, _DIAG_CMD, timeout=30)
    if out is None:
        return {"reachable": False, "tunnel_ok": tunnel}
    d = {"reachable": True, "tunnel_ok": tunnel, "wan_ok": None,
         "uptime": None, "wifi": None, "unclean": [], "hs_fail_1h": 0}
    now = time.time()
    for line in out.splitlines():
        k, _, v = line.strip().partition("=")
        if k == "uptime":
            d["uptime"] = v
        elif k == "public":
            d["public"] = v.strip() or None
        elif k == "wan":
            d["wan_ok"] = v.strip().isdigit() and int(v) > 0
        elif k == "wifi" and v.strip().isdigit():
            d["wifi"] = int(v)
        elif k == "unclean":
            d["unclean"].append(v)
        elif k == "hsf":
            t = _ts(v)
            if t and now - t <= 3600:
                d["hs_fail_1h"] += 1
    d.update(_parse_stav(out))
    d["dns"] = dns_state(config, d.get("public"))
    return d


def verdict(d: dict) -> str:
    if not d.get("reachable"):
        return ("Na router se teď nedostanu." +
                (" Internet ale jde." if d.get("tunnel_ok") else
                 " A internet nejde ani mně."))
    srv = d.get("active") or "?"
    if d.get("tunnel_ok"):
        return "Internet jde, VPN server %s drží." % srv
    if d.get("wan_ok"):
        return ("Linka od poskytovatele jde, ale VPN server %s neodpovídá — "
                "pomůže přepnout server." % srv)
    return ("Nejde ani linka mimo VPN, takže je to u poskytovatele. "
            "Přepnutí serveru nepomůže.")


def summary(config: dict) -> str:
    d = diagnose(config)
    lines = [verdict(d)]
    if d.get("reachable"):
        lines.append("")
        lines.append("• linka mimo VPN: %s" % ("jde" if d.get("wan_ok") else "NEJDE"))
        lines.append("• internet přes VPN: %s" % ("jde" if d.get("tunnel_ok") else "NEJDE"))
        hs = _hs_s(d.get("handshake"))
        lines.append("• VPN server: %s%s" % (
            d.get("active") or "žádný aktivní",
            (" (poslední spojení před %d s)" % hs) if hs is not None else ""))
        lines.append("• DNS: %s" % _DNS_TXT.get(d.get("dns"), "nezjištěno"))
        lines.append("• nepovedená spojení tunelu za hodinu: %d" % d.get("hs_fail_1h", 0))
        if d.get("uptime"):
            lines.append("• router běží: %s" % d["uptime"])
        if d.get("unclean"):
            lines.append("• poslední nečistý restart: %s" % d["unclean"][-1])
        if d.get("wifi") is not None:
            lines.append("• připojeno přes WiFi: %d" % d["wifi"])
    return "\n".join(lines)


def switch_next(config: dict, reason: str = "") -> dict:
    """Přepne na další server téhož státu a počká, jestli internet naskočí."""
    before = _parse_stav(_ssh(config, "/system script run hans-vpn-stav") or "")
    out = _ssh(config, "/system script run hans-vpn-dalsi", timeout=30)
    if out is None:
        return {"ok": False, "error": "router nedostupný"}
    m = re.search(r"OK\s+(\S+)\s*->\s*(\S+)", out)
    if not m:
        _log.warning("HANS_ROUTER_V1: přepnutí VPN selhalo: %s", out.strip()[:200])
        return {"ok": False, "error": out.strip()[:200] or "bez odpovědi"}
    frm, to = server_label(m.group(1)), server_label(m.group(2))
    _log.info("HANS_ROUTER_V1: VPN přepnuta %s → %s (%s)", frm, to, reason or "?")
    internet = False
    deadline = time.time() + float(_cfg(config).get("switch_verify_s", 30))
    while time.time() < deadline:
        time.sleep(3)
        if _pi_ping(config):
            internet = True
            break
    return {"ok": True, "from": frm or before.get("active"), "to": to,
            "internet": internet}


def switch_text(res: dict) -> str:
    if not res.get("ok"):
        return "VPN server se přepnout nepovedlo: %s" % res.get("error", "?")
    s = "Přepnul jsem VPN z %s na %s." % (res["from"], res["to"])
    return s + (" Internet jde." if res.get("internet") else
                " Internet ale zatím nejde.")


def watch_tick(config: dict, st: dict, notify=None) -> None:
    """Jeden krok automatiky. `st` drží stav mezi voláními.

    Přepíná JEN když nejde internet přes VPN a zároveň jde linka mimo ni
    (jinak by přepnutí nepomohlo). Pojistky: `fail_strikes` po sobě jdoucích
    selhání, `cooldown_s` mezi přepnutími a `max_switches_h` za hodinu —
    ať Hans při výpadku u Protonu neprotočí dokola všechny servery."""
    c = _cfg(config)
    if not enabled(config) or not c.get("auto_switch", True):
        return
    if _pi_ping(config):
        if st.get("strikes"):
            _log.info("HANS_ROUTER_V1: internet zase jde (po %d selháních)",
                      st["strikes"])
        st["strikes"] = 0
        st["state"] = "ok"
        return
    out = _ssh(config, ':put ("wan=" . [/ping 1.1.1.1 count=3 interval=300ms])')
    wan = None
    if out:
        m = re.search(r"wan=(\d+)", out)
        wan = bool(m and int(m.group(1)) > 0)
    if wan is None:
        if st.get("state") != "router_down":
            _log.warning("HANS_ROUTER_V1: internet nejde a router neodpovídá")
        st["state"] = "router_down"
        st["strikes"] = 0
        return
    if not wan:
        if st.get("state") != "isp_down":
            _log.warning("HANS_ROUTER_V1: nejde ani linka mimo VPN — "
                         "výpadek u poskytovatele, nepřepínám")
        st["state"] = "isp_down"
        st["strikes"] = 0
        return
    st["strikes"] = int(st.get("strikes", 0)) + 1
    st["state"] = "vpn_down"
    need = int(c.get("fail_strikes", 2))
    if st["strikes"] <= need:
        _log.info("HANS_ROUTER_V1: VPN neodpovídá, linka jde (%d/%d)",
                  st["strikes"], need)
    if st["strikes"] < need:
        return
    now = time.time()
    if now - float(st.get("last_switch", 0)) < float(c.get("cooldown_s", 600)):
        return
    hist = [t for t in st.get("history", []) if now - t < 3600]
    if len(hist) >= int(c.get("max_switches_h", 3)):
        if not st.get("gave_up"):
            st["gave_up"] = True
            msg = ("VPN nejde ani po %d přepnutích za poslední hodinu, linka "
                   "mimo VPN přitom jde. Dál to nechávám být — bude potřeba "
                   "se na to podívat ručně." % len(hist))
            _log.warning("HANS_ROUTER_V1: %s", msg)
            if notify:
                try:
                    notify(msg)
                except Exception:
                    pass
        return
    st["gave_up"] = False
    res = switch_next(config, reason="automatika")
    st["last_switch"] = now
    hist.append(now)
    st["history"] = hist
    st["strikes"] = 0
    if res.get("ok"):
        msg = ("Internet přes VPN nešel (linka mimo VPN ano), tak jsem "
               "přepnul server z %s na %s. %s" % (
                   res["from"], res["to"],
                   "Internet zase jde." if res.get("internet") else
                   "Internet ale zatím nejde — zkusím to znovu za %d min." %
                   (int(c.get("cooldown_s", 600)) // 60)))
    else:
        msg = ("Internet přes VPN nejde a server se přepnout nepovedlo: %s"
               % res.get("error", "?"))
    if notify:
        try:
            notify(msg)
        except Exception as e:
            _log.warning("HANS_ROUTER_V1: oznámení selhalo: %s", e)
