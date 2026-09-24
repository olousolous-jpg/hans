#!/bin/bash
# installer/pc/install_pc.sh — příprava PC (Linux s GPU) pro Hanse:
#   Ollama (jazykové modely) + stažení modelů + OpenWebUI (chat, paměť, přepis řeči)
#   + síťové nastavení, aby na ně Pi dosáhlo (+ volitelně SSH a Wake-on-LAN).
#
# Na PC: zkopíruj celou složku installer/ (nebo naklonuj repozitář) a spusť
#   bash installer/pc/install_pc.sh            # celé
#   bash installer/pc/install_pc.sh --dry-run  # jen ukáže, co by udělal
#
# ComfyUI (avatar) je volitelné a ruční — viz deploy/SETUP_PC.md, kapitola 3.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[ -f "$HERE/../lib/common.sh" ] || { echo "Chybí installer/lib/common.sh — zkopíruj celou složku installer/."; exit 1; }
# shellcheck source=../lib/common.sh
source "$HERE/../lib/common.sh"

while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run) DRY_RUN=1 ;;
        --yes|-y) ASSUME_YES=1 ;;
        -h|--help) sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) die "Neznámý přepínač: $1" ;;
    esac
    shift
done
export DRY_RUN ASSUME_YES

# Modely, které Hans v configu používá (viz installer/hans_setup/models.py).
MODELS_BASE=(bge-m3 jobautomation/OpenEuroLLM-Czech llava:7b qwen2.5:7b qwen2.5vl:7b)
MODELS_FULL=(qwen3:30b translategemma:12b)

header "HANS — příprava PC"

# ── 1. Přehled ───────────────────────────────────────────────────────────────
# shellcheck disable=SC1091
info "systém: $(. /etc/os-release 2>/dev/null; echo "${PRETTY_NAME:-?}")"
VRAM_GB=0
if have nvidia-smi; then
    VRAM_GB="$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits 2>/dev/null | awk 'NR==1 {print int($1/1024)}')"
    info "GPU: NVIDIA, ${VRAM_GB} GB VRAM"
elif have rocm-smi; then
    VRAM_GB="$(rocm-smi --showmeminfo vram --csv 2>/dev/null | awk -F, 'NR==2 {print int($3/1024/1024/1024)}')"
    info "GPU: AMD (ROCm), ${VRAM_GB} GB VRAM"
elif lspci 2>/dev/null | grep -qiE 'vga.*(amd|ati|radeon)'; then
    warn "GPU AMD bez ROCm — Ollama poběží na CPU, dokud nenainstaluješ ROCm (návod AMD)."
elif lspci 2>/dev/null | grep -qi 'vga.*nvidia'; then
    warn "GPU NVIDIA bez ovladače — nainstaluj ovladač NVIDIA, jinak Ollama poběží na CPU."
else
    warn "Samostatnou GPU jsem nenašel — modely poběží pomalu na CPU."
fi
VRAM_GB="${VRAM_GB:-0}"; [[ "$VRAM_GB" =~ ^[0-9]+$ ]] || VRAM_GB=0
PC_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
info "IP adresa tohoto PC: ${PC_IP:-?}"

# ── 2. Ollama ────────────────────────────────────────────────────────────────
header "Ollama"
if have ollama; then
    ok "Ollama už je nainstalovaná ($(ollama --version 2>/dev/null | tail -n1))"
else
    run_sh "curl -fsSL https://ollama.com/install.sh | sh" || die "Instalace Ollamy selhala."
fi
# Výchozí Ollama poslouchá jen na 127.0.0.1 — Pi by se k ní nedostalo.
OVR=/etc/systemd/system/ollama.service.d/hans.conf
if [ ! -f "$OVR" ]; then
    info "Nastavuji Ollamu, aby poslouchala v síti (OLLAMA_HOST=0.0.0.0)…"
    run sudo mkdir -p "$(dirname "$OVR")"
    run_sh "printf '[Service]\nEnvironment=OLLAMA_HOST=0.0.0.0:11434\n' | sudo tee $OVR >/dev/null"
    run sudo systemctl daemon-reload
    run sudo systemctl restart ollama
fi
for _ in $(seq 1 20); do http_ok "http://127.0.0.1:11434/api/version" && break; [ "$DRY_RUN" = "1" ] && break; sleep 1; done
http_ok "http://127.0.0.1:11434/api/version" && ok "Ollama odpovídá na :11434" || [ "$DRY_RUN" = "1" ] || warn "Ollama neodpovídá."

# ── 3. Modely ────────────────────────────────────────────────────────────────
header "Jazykové modely"
info "Základ (~25 GB): ${MODELS_BASE[*]}"
info "Plná sada navíc (~27 GB, pro GPU s 16+ GB VRAM): ${MODELS_FULL[*]}"
info "Chatový model persony (místo hans-czech) vytvoří až průvodce na Pi."
tier="$(ask "Stáhnout: 1) základ  2) plnou sadu  3) nic" "$([ "$VRAM_GB" -ge 16 ] && echo 2 || echo 1)")"
models=()
case "$tier" in
    1) models=("${MODELS_BASE[@]}") ;;
    2) models=("${MODELS_BASE[@]}" "${MODELS_FULL[@]}") ;;
esac
for m in "${models[@]}"; do
    run ollama pull "$m" || warn "model $m se nestáhl (zkus později: ollama pull $m)"
done

# ── 4. OpenWebUI (Docker) ────────────────────────────────────────────────────
header "OpenWebUI"
if ! have docker; then
    if confirm "Docker chybí. Nainstalovat (oficiální skript get.docker.com)?" a; then
        run_sh "curl -fsSL https://get.docker.com | sh" || die "Instalace Dockeru selhala."
        run sudo usermod -aG docker "$(whoami)"
    else
        die "OpenWebUI bez Dockeru tenhle skript neumí — viz deploy/SETUP_PC.md."
    fi
fi
DOCKER="docker"
docker info >/dev/null 2>&1 || DOCKER="sudo docker"   # skupina docker platí až po přihlášení
if $DOCKER ps -a --format '{{.Names}}' 2>/dev/null | grep -qx open-webui; then
    ok "kontejner open-webui už existuje"
    if confirm "Aktualizovat OpenWebUI na nejnovější verzi (data zůstanou)?" n; then
        run $DOCKER pull ghcr.io/open-webui/open-webui:main
        run $DOCKER rm -f open-webui
    fi
fi
if ! $DOCKER ps -a --format '{{.Names}}' 2>/dev/null | grep -qx open-webui; then
    run $DOCKER run -d --network=host \
        -v open-webui:/app/backend/data \
        -e OLLAMA_BASE_URL=http://127.0.0.1:11434 \
        --name open-webui --restart always \
        ghcr.io/open-webui/open-webui:main
fi
info "Čekám, až OpenWebUI naběhne (první start stahuje modely pro přepis řeči)…"
for _ in $(seq 1 90); do http_ok "http://127.0.0.1:8080" && break; [ "$DRY_RUN" = "1" ] && break; sleep 2; done
http_ok "http://127.0.0.1:8080" && ok "OpenWebUI běží na :8080" || [ "$DRY_RUN" = "1" ] || warn "OpenWebUI zatím neodpovídá (docker logs open-webui)."

# ── 5. Síť: firewall, SSH, Wake-on-LAN ───────────────────────────────────────
header "Síť"
if have ufw && sudo ufw status 2>/dev/null | grep -q "Status: active"; then
    src="$(ask "IP adresa Pi (Enter = povolit celé místní síti)" "")"
    for port in 11434 8080 8188; do
        if [ -n "$src" ]; then run sudo ufw allow from "$src" to any port "$port" proto tcp
        else run sudo ufw allow "$port/tcp"; fi
    done
else
    info "ufw není aktivní — porty 11434 (Ollama), 8080 (OpenWebUI), 8188 (ComfyUI) nejsou blokované."
fi

if confirm "Povolit SSH na PC (Hans přes něj PC uspává/vypíná a hlídá hry)?" a; then
    if ! systemctl is-active --quiet ssh 2>/dev/null && ! systemctl is-active --quiet sshd 2>/dev/null; then
        if have apt-get; then run sudo apt-get install -y openssh-server
        elif have dnf; then run sudo dnf install -y openssh-server
        fi
        run_sh "sudo systemctl enable --now ssh 2>/dev/null || sudo systemctl enable --now sshd"
    fi
    ok "SSH: uživatel $(whoami)"
fi

IFACE="$(ip route show default 2>/dev/null | awk '{print $5; exit}')"
MAC="$(cat "/sys/class/net/$IFACE/address" 2>/dev/null || true)"
if [ -n "$IFACE" ] && confirm "Zapnout Wake-on-LAN na $IFACE (Hans umí PC probudit)?" a; then
    if have nmcli; then
        con="$(nmcli -t -f NAME,DEVICE connection show --active | awk -F: -v d="$IFACE" '$2==d {print $1; exit}')"
        [ -n "$con" ] && run sudo nmcli connection modify "$con" 802-3-ethernet.wake-on-lan magic
    elif have ethtool; then
        run sudo ethtool -s "$IFACE" wol g
        warn "ethtool nastavení nepřežije restart — nastav WOL i v síťovém manažeru."
    fi
    info "Wake-on-LAN musí být povolené i v BIOSu/UEFI."
fi

# ── 6. Shrnutí ───────────────────────────────────────────────────────────────
header "Hotovo"
cat <<EOF
  Do průvodce na Pi zadej:
    IP adresa PC      : ${PC_IP:-?}
    MAC adresa PC     : ${MAC:-?}
    uživatel na PC    : $(whoami)

  ${C_BLD}Ještě ručně (jednou):${C_RST}
    1. Otevři v prohlížeči http://${PC_IP:-localhost}:8080 a vytvoř účet (první účet je admin).
    2. Nastavení → Účet → API klíče → vytvoř klíč (sk-…). Ten zadáš průvodci na Pi.
    3. Pokud máš starý PC s modelem hans-czech a chceš ho zachovat:
         starý PC:  ollama show --modelfile hans-czech > hans-czech.Modelfile
         tento PC:  ollama create hans-czech -f hans-czech.Modelfile
       (jinak průvodce na Pi vytvoří nový model persony z veřejného základu)
EOF
