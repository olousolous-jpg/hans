"""
HANS_STATUS_UNIFIED_V1 (5.8.2026) — JEDEN `/stav`.

Proč vzniklo: `/stav` měl DVĚ nezávislé implementace a uživatel dostal jinou
odpověď podle toho, odkud se zeptal:
  • web chat → `chat_commands._cmd_info` — „Mám v paměti N zpráv…"
  • most/Matrix → `bridge_commands._cmd_status` — systémový výpis (teplota,
    RAM, herní mód, hlídání, mozek)
`bridge_commands.handle_command` chytá „stav" dřív než inspect whitelist, takže
z telefonu se ta chatová verze NIKDY neukázala. Doloženo 5.8.: přidal jsem
informaci o běžícím renderu jen do chatové větve, uživatel se zeptal z mobilu
a nic nového neviděl — a mít dvě cesty ke stejnému příkazu je matoucí i pro
člověka, který to psal.

Tady je tedy jediný stavitel textu; obě cesty ho volají.
  • `handler=None` (most) → systémová část
  • `handler` (chat) → navíc paměť hovoru a s kým se mluví

Modul je ZÁMĚRNĚ samostatný, ne v `chat_commands` (2 800 řádků, god-object
z backlogu) a ne v `bridge_commands` (ten už `chat_commands` importuje →
kruhový import). Souvisí úklidová poznámka „boy scout rule" v backlogu.
"""

from __future__ import annotations

import logging
import os

_log = logging.getLogger("hans_status")


def _line_cpu_temp(lines: list) -> None:
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            lines.append("Teplota CPU: %.0f °C" % (int(f.read().strip()) / 1000.0))
    except Exception:
        pass


def _line_resources(lines: list) -> None:
    try:
        import psutil
        cpu = psutil.cpu_percent(interval=0.5)
        vm = psutil.virtual_memory()
        du = psutil.disk_usage("/")
        lines.append("Zátěž CPU: %.0f %%" % cpu)
        lines.append("RAM: %.1f / %.1f GB (volné %.1f GB)" % (
            (vm.total - vm.available) / 1e9, vm.total / 1e9, vm.available / 1e9))
        lines.append("Disk: volných %.0f GB z %.0f GB" % (
            du.free / 1e9, du.total / 1e9))
    except Exception:
        pass
    try:
        lines.append("Load: %.2f %.2f %.2f" % os.getloadavg())
    except Exception:
        pass
    try:
        with open("/proc/uptime") as f:
            up = float(f.read().split()[0])
        lines.append("Běžím: %dd %dh %dm" % (
            int(up // 86400), int((up % 86400) // 3600), int((up % 3600) // 60)))
    except Exception:
        pass


def _line_game(lines: list) -> None:
    try:
        from scripts.ollama_client import game_mode_on
        lines.append("Herní mód: " + ("ZAPNUT (grafika uvolněna pro hru)"
                                      if game_mode_on() else "vypnut"))
    except Exception:
        pass


def _line_guard(lines: list) -> None:
    """HANS_GUARD_STATUS_V2 — stav hlídacího přepínače (/hlidej)."""
    try:
        import time as _t
        from scripts import hans_guard as _g
        _gs = _g.state()
        if _gs.get("armed"):
            _since = _gs.get("since") or 0
            _od = _t.strftime("%H:%M", _t.localtime(_since)) if _since else "?"
            lines.append("Hlídání: ZAPNUTO (od %s, dnes %d snímků)"
                         % (_od, int(_gs.get("sent_today", 0))))
        else:
            lines.append("Hlídání: vypnuto")
    except Exception:
        pass


def _line_brain(lines: list, config: dict) -> None:
    try:
        import requests
        url = ((config.get("openwebui_chat", {}) or {}).get("base_url", "")
               or "").rstrip("/")
        if url:
            r = requests.get(url + "/api/tags", timeout=4)
            lines.append("Mozek (LLM): " + ("online" if r.ok else "nedostupný"))
    except Exception:
        lines.append("Mozek (LLM): spí / nedostupný")


def _line_render(lines: list, config: dict) -> None:
    """HANS_RENDER_STATUS_V1 — běží právě malba? Ptáme se fronty ComfyUI,
    ne vlastního stavu v procesu (render spouští hans_art, hans_maker i avatar
    a Hans se mezitím mohl restartovat). None = NEVÍM → mlčíme; tvrdit
    „nemaluji" by bylo lživé přesně v okamžiku dokončení renderu, kdy ComfyUI
    na pár sekund přestane odbavovat HTTP."""
    try:
        from scripts.avatar_render import render_status
        rs = render_status(config)
    except Exception:
        rs = None
    if rs is None:
        return
    if not rs.get("running"):
        lines.append("Malování: nic neběží")
        return
    q = rs.get("pending") or 0
    lines.append("Malování: PRÁVĚ RENDERUJI%s"
                 % ((" (ve frontě další %d)" % q) if q else ""))
    p = (rs.get("prompt") or "").strip()
    if p:
        lines.append("  námět: %s" % (p[:110] + ("…" if len(p) > 110 else "")))


def _lines_conversation(lines: list, handler, person: str) -> None:
    """Chatová část — jen když voláme z chatu (most osobu/paměť neřeší)."""
    store = getattr(handler, "conv_store", None)
    if store is not None and hasattr(store, "get_history") and person:
        try:
            lines.append("V paměti mám %d zpráv z našich hovorů."
                         % len(store.get_history(person)))
        except Exception:
            pass
    if getattr(handler, "_hans_idle", None):
        try:
            from scripts.hans_persona import persona_name as _pn
            lines.append("%s idle modul běží."
                         % _pn(getattr(handler, "config", {}) or {}))
        except Exception:
            pass
    if person:
        lines.append("Mluvím s: %s." % person)


def status_text(config: dict, handler=None, person: str = "") -> str:
    """Jediný text `/stav` — stejný z chatu i z mostu."""
    config = config or {}
    lines = ["Stav systému:"]
    _line_cpu_temp(lines)
    _line_resources(lines)
    _line_game(lines)
    _line_guard(lines)
    _line_brain(lines, config)
    _line_render(lines, config)
    if handler is not None:
        _lines_conversation(lines, handler, person)
    return "\n".join(lines)
