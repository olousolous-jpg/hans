"""Krok „připojení": IP adresy PC a Kodi, přihlašovací údaje, tokeny."""
from __future__ import annotations

import re
import urllib.parse

from . import cfg as C
from . import ui

_IP = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")

# Kam se dosadí IP PC. Porty jsou výchozí porty služeb z installer/pc/install_pc.sh.
# Hodnoty sedí na to, co jednotlivé moduly čtou (a jaké mají výchozí hodnoty
# v kódu): openwebui_chat.base_url je ve skutečnosti adresa OLLAMY (room_observer,
# hans_dialog, ollama_client), openwebui_direct.base_url je OpenWebUI.
PC_URLS = {
    "openwebui_chat.base_url":        "http://{ip}:11434",
    "openwebui_direct.base_url":      "http://{ip}:8080",
    "knowledge.base_url":             "http://{ip}:8080",
    "voice.stt_url":                  "http://{ip}:8080/api/v1/audio/transcriptions",
    "self_insight.reasoning_url":     "http://{ip}:11434",
    "intent.base_url":                "http://{ip}:11434",
    "hans_avatar.comfyui_url":        "http://{ip}:8188",
    "hans_avatar.animate.comfy_url":  "http://{ip}:8188",
    "hans_avatar.animate.ollama_url": "http://{ip}:11434",
    "pc_remote.host":                 "{ip}",
    "translate.pc_host":              "{ip}",
    "translate.smb_prefix":           "smb://{ip}/",
    "wol_pc_ip":                      "{ip}",
}


def _valid_ip(s: str) -> bool:
    return bool(_IP.match(s)) and all(0 <= int(p) <= 255 for p in s.split("."))


def _host_of(value: str) -> str:
    if not value:
        return ""
    if "://" in value:
        return urllib.parse.urlparse(value).hostname or ""
    return value.strip("/")


def current_pc_ip(cfg: dict) -> str:
    for path in ("wol_pc_ip", "openwebui_direct.base_url", "openwebui_chat.base_url"):
        h = _host_of(str(C.get(cfg, path, "") or ""))
        if _valid_ip(h):
            return h
    return ""


def ollama_url(cfg: dict) -> str:
    return str(C.get(cfg, "openwebui_chat.base_url", "") or "")


def apply_pc_ip(cfg: dict, ip: str) -> None:
    for path, tmpl in PC_URLS.items():
        old = str(C.get(cfg, path, "") or "")
        new = tmpl.format(ip=ip)
        host = _host_of(old)
        # existující platnou URL (vlastní port/cesta) zachovej, vyměň jen IP
        if old and host and ":0000" not in old and host != ip and "://" in old:
            new = old.replace(host, ip, 1)
        elif old and host == ip:
            new = old
        C.set_(cfg, path, new)


def ask_ip(prompt: str, default: str, required: bool = True) -> str:
    while True:
        ip = ui.ask(prompt, default, required=required)
        if not ip and not required:
            return ""
        if _valid_ip(ip):
            return ip
        ui.warn("To není IPv4 adresa (např. 192.168.1.20).")
        if not ui._tty():
            return default


def step_pc(cfg: dict) -> str:
    ui.header("Připojení k PC (Ollama, OpenWebUI)")
    ui.info("Hans potřebuje PC v téže síti, na kterém běží Ollama (jazykové modely)")
    ui.info("a OpenWebUI (chat, paměť, přepis řeči). Na PC spusť installer/pc/install_pc.sh.")
    ui.info("IP adresu PC zjistíš na PC příkazem:  hostname -I")
    ip = ask_ip("IP adresa PC", current_pc_ip(cfg))
    if not ip:
        ui.warn("IP adresa PC nezadána — adresy služeb zůstávají beze změny.")
        return ""
    apply_pc_ip(cfg, ip)
    ui.ok("PC %s → Ollama :11434, OpenWebUI :8080, ComfyUI :8188" % ip)
    return ip


def step_rest(cfg: dict) -> None:
    ui.header("Přihlášení a další zařízení")

    # OpenWebUI
    ui.info("OpenWebUI: API klíč najdeš v OpenWebUI → Nastavení → Účet → API klíče (sk-…).")
    tok = ui.ask("OpenWebUI API klíč", str(C.get(cfg, "openwebui_direct.api_token", "") or ""), secret=True)
    if tok:
        C.set_(cfg, "openwebui_direct.api_token", tok)
        if not C.get(cfg, "voice.stt_token"):
            C.set_(cfg, "voice.stt_token", tok)
    user = ui.ask("OpenWebUI uživatel (e-mail účtu)", str(C.get(cfg, "openwebui_direct.username", "") or ""))
    if user:
        C.set_(cfg, "openwebui_direct.username", user)

    # SSH na PC (vzdálené vypnutí, herní režim, překlady)
    pc_user = ui.ask("Uživatelské jméno na PC (pro SSH; Enter = přeskočit)",
                     str(C.get(cfg, "pc_remote.user", "") or ""))
    if pc_user:
        C.set_(cfg, "pc_remote.user", pc_user)
        C.set_(cfg, "translate.pc_user", pc_user)
        C.set_(cfg, "pc_remote.key_path", "~/.ssh/hans_pc")
        C.set_(cfg, "translate.pc_key", "~/.ssh/hans_pc")
    else:
        C.set_(cfg, "pc_remote.enabled", False)

    # Wake-on-LAN
    ui.info("Wake-on-LAN: Hans umí PC probudit. MAC adresu zjistíš na PC: ip link")
    mac = ui.ask("MAC adresa PC (Enter = bez probouzení)", str(C.get(cfg, "wol_pc_mac", "") or ""))
    if re.match(r"^([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}$", mac or ""):
        C.set_(cfg, "wol_pc_mac", mac.lower().replace("-", ":"))
        C.set_(cfg, "wol_pc_enabled", True)
    else:
        if mac:
            ui.warn("Neplatná MAC — probouzení vypnuto.")
        C.set_(cfg, "wol_pc_enabled", False)

    # Kodi
    cur_kodi = _host_of(str(C.get(cfg, "kodi.host", "") or ""))
    kodi = ask_ip("IP adresa Kodi / OSMC (Enter = nemám Kodi)",
                  cur_kodi if _valid_ip(cur_kodi) else "", required=False)
    if kodi:
        C.set_(cfg, "kodi.host", kodi)
        C.set_(cfg, "kodi.enabled", True)
        C.set_(cfg, "kodi.user", ui.ask("Kodi uživatel", str(C.get(cfg, "kodi.user", "") or "osmc")))
        pw = ui.ask("Kodi heslo", str(C.get(cfg, "kodi.password", "") or "osmc"), secret=True)
        C.set_(cfg, "kodi.password", pw)
    else:
        C.set_(cfg, "kodi.enabled", False)

    # Volitelné cloudové zálohy
    for path, label in (("gemini.api_key", "Gemini API klíč (volitelné)"),
                        ("openrouter.api_key", "OpenRouter API klíč (volitelné)")):
        v = ui.ask(label + " — Enter = přeskočit", str(C.get(cfg, path, "") or ""), secret=True)
        if v:
            C.set_(cfg, path, v)
