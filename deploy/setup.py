#!/usr/bin/env python3
"""
HANS_SETUP_WIZARD_V1 — interaktivní průvodce nastavením prostředí.

Provede nového uživatele vyplněním IP adres, přihlášení a API klíčů a vygeneruje
`config.json`. Hodnoty, které byly dřív zadrátované, jsou teď otázky s rozumnými
defaulty (Enter = ponechat současné).

Spuštění z kořene projektu:
    python3 deploy/setup.py

Základ bere z existujícího config.json (ponechá všechna ostatní nastavení), nebo
z config.example.json (pro čistou instalaci z GitHubu). Před zápisem zazálohuje.
"""

import json
import os
import re
import shutil
import sys
from collections import OrderedDict
from fnmatch import fnmatch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = os.path.join(ROOT, "config.json")
EXAMPLE = os.path.join(ROOT, "config.example.json")
_IP_RE = re.compile(r"\d{1,3}(?:\.\d{1,3}){3}")

# ── logické otázky → kam se v configu dosadí ─────────────────────────────────
# kind: 'ip' (nahradí IP ve všech 'fills' URL), 'text', 'secret', '?'=volitelné
QUESTIONS = [
    {"id": "pc_ip", "kind": "ip",
     "prompt": "IP adresa PC (Ollama / OpenWebUI / ComfyUI / Whisper)",
     "ref": "wol_pc_ip",
     "fills": ["openwebui_chat.base_url", "openwebui_direct.base_url",
               "voice.stt_url", "hans_avatar.comfyui_url",
               "knowledge.base_url", "wol_pc_ip"]},
    {"id": "kodi_ip", "kind": "ip",
     "prompt": "IP adresa Kodi / OSMC (media centrum)",
     "ref": "kodi.host", "fills": ["kodi.host"]},
    {"id": "owui_user", "kind": "text",
     "prompt": "OpenWebUI — uživatelské jméno", "path": "openwebui_direct.username"},
    {"id": "owui_pass", "kind": "secret",
     "prompt": "OpenWebUI — heslo", "path": "openwebui_direct.password"},
    {"id": "owui_token", "kind": "secret",
     "prompt": "OpenWebUI — API token (sk-…)", "path": "openwebui_direct.api_token"},
    {"id": "stt_token", "kind": "secret",
     "prompt": "Whisper/STT token (často stejný jako OpenWebUI token)",
     "path": "voice.stt_token"},
    {"id": "kodi_user", "kind": "text",
     "prompt": "Kodi — uživatel (výchozí osmc)", "path": "kodi.user"},
    {"id": "kodi_pass", "kind": "secret",
     "prompt": "Kodi — heslo", "path": "kodi.password"},
    {"id": "wol_mac", "kind": "text",
     "prompt": "MAC adresa PC pro Wake-on-LAN (xx:xx:xx:xx:xx:xx)",
     "path": "wol_pc_mac"},
    {"id": "gemini", "kind": "secret", "optional": True,
     "prompt": "Gemini API klíč (volitelné — Enter přeskočí)", "path": "gemini.api_key"},
    {"id": "openrouter", "kind": "secret", "optional": True,
     "prompt": "OpenRouter API klíč (volitelný fallback — Enter přeskočí)",
     "path": "openrouter.api_key"},
]


# ── helpery pro vnořené cesty ────────────────────────────────────────────────
def _get(cfg, path):
    cur = cfg
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _set(cfg, path, value):
    parts = path.split(".")
    cur = cfg
    for part in parts[:-1]:
        cur = cur.setdefault(part, OrderedDict())
    cur[parts[-1]] = value


def _mask(val):
    s = str(val or "")
    if not s:
        return "(prázdné)"
    return s[:3] + "…" + s[-2:] if len(s) > 6 else "***"


def _current_ip(cfg, q):
    v = str(_get(cfg, q["ref"]) or "")
    m = _IP_RE.search(v)
    return m.group(0) if m else ""


# ── migrace do nového adresáře (klon Hanse + nový config) ────────────────────
# Vynech: venv/cache/historii/logy/zálohy (regenerují se / nepatří do klonu).
_SKIP_NAMES = {"venv", ".venv", "__pycache__", "archive", ".git",
               "config.json", "config.json.bak", "patch_snapshots",
               "system.log"}
_SKIP_GLOBS = ("*.pyc", "*.bak", "recognition.log*", ".lgd-*")


def _skip(_dir, names):
    import stat as _st
    out = set()
    for n in names:
        if n in _SKIP_NAMES or any(fnmatch(n, g) for g in _SKIP_GLOBS):
            out.add(n)
            continue
        try:  # přeskoč speciální soubory (named pipe / socket / device)
            mode = os.lstat(os.path.join(_dir, n)).st_mode
            if (_st.S_ISFIFO(mode) or _st.S_ISSOCK(mode)
                    or _st.S_ISBLK(mode) or _st.S_ISCHR(mode)):
                out.add(n)
        except OSError:
            pass
    return out


def _migrate(cfg, target):
    """Zkopíruje celého Hanse (kód + data: deník/tváře/avatar, BEZ venv/logů/
    archivu) do target a zapíše tam nakonfigurovaný config.json. Originál netknut."""
    target = os.path.abspath(os.path.expanduser(target))
    if os.path.exists(target) and os.listdir(target):
        print(f"CHYBA: {target} už existuje a není prázdný — přeskočeno.")
        return False
    print(f"Kopíruji Hanse → {target}  (bez venv/cache/archivu/logů)…")
    try:
        shutil.copytree(ROOT, target, ignore=_skip, dirs_exist_ok=True)
    except Exception as e:
        print(f"CHYBA při kopírování: {e}")
        return False
    json.dump(cfg, open(os.path.join(target, "config.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    print(f"✓ Nový Hans v {target}")
    print("  Další kroky na novém místě:")
    print("    1) nainstaluj závislosti: pip install --break-system-packages "
          "-r deploy/requirements.txt")
    print("    2) uprav systemd službu (cestu k run.sh) nebo spusť ./run.sh")
    return True


def main():
    if os.path.exists(CONFIG):
        base = CONFIG
    elif os.path.exists(EXAMPLE):
        base = EXAMPLE
        print("config.json nenalezen → vycházím z config.example.json")
    else:
        print("CHYBA: nenalezen config.json ani config.example.json"); sys.exit(1)
    cfg = json.load(open(base, encoding="utf-8"), object_pairs_hook=OrderedDict)

    print("\n=== Hans — průvodce nastavením ===")
    print("Enter = ponechat současnou hodnotu (v závorkách). Ctrl+C = konec.\n")

    for q in QUESTIONS:
        if q["kind"] == "ip":
            cur = _current_ip(cfg, q)
            ans = input(f"{q['prompt']}\n  [{cur or 'nenastaveno'}]: ").strip()
            new_ip = ans or cur
            if not new_ip:
                continue
            for path in q["fills"]:
                old = str(_get(cfg, path) or "")
                if path == "wol_pc_ip" or not old:
                    _set(cfg, path, new_ip)
                else:  # URL → nahraď jen IP, ponech port/cestu
                    _set(cfg, path, _IP_RE.sub(new_ip, old, count=1))
        else:
            cur = _get(cfg, q["path"])
            shown = _mask(cur) if q["kind"] == "secret" else (cur or "(prázdné)")
            ans = input(f"{q['prompt']}\n  [{shown}]: ").strip()
            if ans:
                _set(cfg, q["path"], ans)
            elif cur is None:
                _set(cfg, q["path"], "")
        print()

    print("Migrovat celého Hanse (kód + data) do NOVÉHO adresáře s tímto configem?")
    target = input("  Zadej cestu k novému adresáři, nebo Enter = upravit na místě: ").strip()
    if target:
        if _migrate(cfg, target):
            print("  (Původní instalace zůstala beze změny.)")
        return

    if os.path.exists(CONFIG):
        bak = CONFIG + ".bak"
        json.dump(json.load(open(CONFIG, encoding="utf-8"), object_pairs_hook=OrderedDict),
                  open(bak, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        print(f"Záloha současného configu → {bak}")
    json.dump(cfg, open(CONFIG, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"✓ Hotovo — zapsáno do {CONFIG}")
    print("  Zkontroluj/uprav ostatní nastavení přes webadmin (localhost:7860) "
          "nebo ručně v config.json.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nZrušeno.")
