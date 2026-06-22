#!/bin/bash
# install.sh — nainstaluje Hanse na CÍLOVÉM Raspberry Pi 5 + AI Kit.
# Spouštět z rozbalené složky bundlu: bash deploy/install.sh
# Předpoklad cíle: Raspberry Pi OS (Bookworm, 64-bit), uživatel se sudo,
#   připojený Hailo-8L (AI Kit), Pi kamera, příp. dual-eye displeje/servo.
set -uo pipefail
cd "$(dirname "$0")/.."
ROOT="$(pwd)"
USER_NAME="$(whoami)"
echo "================ HANS INSTALL ================"
echo "kořen: $ROOT   uživatel: $USER_NAME"

# ── 1. Systémové (apt) závislosti — HW vrstvy ────────────────────────────────
echo
echo "[1/5] Systémové balíčky (apt) — Hailo, kamera, GPIO, audio, ffmpeg…"
echo "      (vyžaduje sudo; HW balíčky nejdou přes pip)"
sudo apt-get update
sudo apt-get install -y \
  python3 python3-pip python3-venv \
  hailo-all \
  python3-picamera2 libcamera-apps python3-libcamera \
  python3-lgpio python3-rpi.gpio python3-spidev \
  ffmpeg libatlas-base-dev \
  || echo "[!] Některé apt balíčky selhaly — viz výše (hailo-all vyžaduje Raspberry Pi repo / AI Kit setup)."

# ── 2. Python balíčky (pip, systémový python3.13) ────────────────────────────
echo
echo "[2/5] Python balíčky (pip → systémový python3.13)…"
PY=python3.13
command -v $PY >/dev/null 2>&1 || PY=python3
echo "      interpret: $($PY --version 2>&1)"
# Bookworm PEP 668 → --break-system-packages (Hans běží systémově, ne ve venv).
# HW balíčky (hailort/picamera2/lgpio) už dodal apt → pip je přeskočí/nechá.
$PY -m pip install --break-system-packages -r deploy/requirements.txt \
  || echo "[!] pip část selhala u některých balíčků (často HW — ty řeší apt). Zkontroluj výše."
# venv NETŘEBA — Hans běží systémově (python3.13); run.sh venv source-uje podmíněně.

# ── 3. Konfigurace IP adres (PC s Ollama/OpenWebUI, Kodi) ─────────────────────
echo
echo "[3/5] Konfigurace — IP adresy externích strojů v config.json"
echo "      Aktuální:"
$PY - <<'EOF'
import json
try:
    c=json.load(open("config.json"))
    print("        kodi.host       :", c.get("kodi",{}).get("host"))
    print("        wol_pc_ip       :", c.get("hans_routine",{}).get("wol_pc_ip", c.get("wol_pc_ip")))
    ow=c.get("openwebui_direct",{}) or c.get("openwebui_chat",{})
    print("        OpenWebUI/Ollama: viz openwebui_* / kodi v config.json")
except Exception as e:
    print("        (nelze přečíst config.json:", e, ")")
EOF
echo "      → Po instalaci uprav config.json (Kodi IP, PC IP pro WOL/Ollama/OpenWebUI),"
echo "        pokud se na cílové síti liší. (Ručně — IP jsou specifické pro síť.)"

# ── 4. systemd user služba (autostart po bootu) ──────────────────────────────
echo
echo "[4/5] systemd user služba hans.service…"
mkdir -p "$HOME/.config/systemd/user"
if [ -f deploy/_systemd/hans.service ]; then
    # přepiš WorkingDirectory na aktuální kořen
    sed "s#WorkingDirectory=.*#WorkingDirectory=$ROOT#; s#ExecStart=/bin/bash .*/run.sh#ExecStart=/bin/bash $ROOT/run.sh#; s#XAUTHORITY=.*#XAUTHORITY=$HOME/.Xauthority#" \
        deploy/_systemd/hans.service > "$HOME/.config/systemd/user/hans.service"
    systemctl --user daemon-reload
    systemctl --user enable hans.service
    echo "      hans.service nainstalována + enabled (autostart po bootu)."
else
    echo "      [!] deploy/_systemd/hans.service nenalezena — službu nastav ručně."
fi

# ── 5. Závěr ─────────────────────────────────────────────────────────────────
echo
echo "[5/5] HOTOVO (s výhradami níže)."
echo "──────────────────────────────────────────────"
echo "RUČNÍ KROKY NA CÍLI (HW-specifické, nelze automatizovat):"
echo "  • Hailo AI Kit: ověř 'hailortd' + /dev/hailo0 (dle oficiálního Pi AI Kit návodu)."
echo "  • Pi kamera: ověř 'libcamera-hello' (kamera připojená + povolená)."
echo "  • Dual-eye displeje / servo: zapojit dle pinmapy (viz CLAUDE.md / Eye_sphere.py)."
echo "  • Audio (robot_hat/hifiberry): nakonfigurovat výstup (viz handoff)."
echo "  • passwordless sudo pro 'systemctl stop hailortd' (run.sh) — doporučeno."
echo "  • Externí: Ollama+OpenWebUI (PC) a Kodi (OSMC) na síti + správné IP v config.json."
echo "  • Skupiny uživatele: video,audio,spi,i2c,gpio,render,input (usermod -aG …)."
echo
echo "Spuštění Hanse:  systemctl --user start hans   (logy: journalctl --user -u hans -f)"
echo "Test bez služby: ./run.sh"
