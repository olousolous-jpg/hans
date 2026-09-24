#!/bin/bash
# installer/install.sh — instalace Hanse na NOVÉ Raspberry Pi 5 + AI Kit (Hailo-8L).
#
#   git clone <repo> ~/hans && cd ~/hans
#   bash installer/install.sh              # celá instalace, krok po kroku
#   bash installer/install.sh --dry-run    # jen ukáže, co by se dělo; nic nemění
#   bash installer/install.sh --list       # seznam kroků
#   bash installer/install.sh --only persona,models
#
# Kroky jdou pouštět opakovaně: hotové se pamatují v data/installer/state
# a při dalším běhu se na ně instalátor zeptá. Nic z existujícího Hansova
# kódu se nemění — jen se instaluje prostředí a zapisuje config.
set -uo pipefail

INSTALLER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$INSTALLER_DIR")"
DATA_DIR="$ROOT/data/installer"
VENV="$ROOT/.venv"
STATE_FILE="$DATA_DIR/state"
LOG_FILE="$DATA_DIR/install.log"
# shellcheck source=lib/common.sh
source "$INSTALLER_DIR/lib/common.sh"

STEPS=(preflight apt system hailo venv network llm persona models knowledge service faces finish)
declare -A STEP_DESC=(
    [preflight]="kontrola zařízení, systému a místa"
    [apt]="systémové balíčky (Hailo, kamera, GPIO, audio…)"
    [system]="skupiny uživatele, SPI/I2C, sudo pro hailortd"
    [hailo]="Hailo zařízení a modely (.hef)"
    [venv]="Python prostředí .venv + balíčky"
    [network]="připojení k PC, tokeny, Kodi, Wake-on-LAN, SSH klíč"
    [llm]="jazykový model pro generování textů (PC / lokálně / ručně)"
    [persona]="domácnost, persona a společník (generuje LLM)"
    [models]="chatový model persony + kontrola modelů na PC"
    [knowledge]="paměť: RAG kolekce v OpenWebUI + dokumenty identity"
    [service]="systemd služba (autostart po bootu)"
    [faces]="rozpoznávání osob: zápis obličejů lidí z domácnosti"
    [finish]="shrnutí a ruční kroky"
)

ONLY=""; SKIP=""; REDO=0; FRESH=0; FORCE=0
usage() {
    sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'
    cat <<EOF

Přepínače:
  --dry-run        nic neinstaluje ani nezapisuje (config jde do data/installer/dryrun/)
  --yes            neinteraktivně (výchozí odpovědi)
  --only a,b       spustit jen vybrané kroky
  --skip a,b       vynechat kroky
  --redo           znovu i kroky označené jako hotové
  --fresh          průvodce se chová jako na novém zařízení (hodí se s --dry-run)
  --force          pokračovat i mimo Raspberry Pi 5 / při běžícím Hansovi
  --list           vypsat kroky
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run) DRY_RUN=1 ;;
        --yes|-y) ASSUME_YES=1 ;;
        --only) ONLY=",$2,"; shift ;;
        --skip) SKIP=",$2,"; shift ;;
        --redo) REDO=1 ;;
        --fresh) FRESH=1 ;;
        --force) FORCE=1 ;;
        --list) for s in "${STEPS[@]}"; do printf '  %-10s %s\n' "$s" "${STEP_DESC[$s]}"; done; exit 0 ;;
        -h|--help) usage; exit 0 ;;
        *) die "Neznámý přepínač: $1 (viz --help)" ;;
    esac
    shift
done
export DRY_RUN ASSUME_YES LOG_FILE STATE_FILE

mkdir -p "$DATA_DIR"
[ "$DRY_RUN" = "1" ] && LOG_FILE=/dev/null

# Python pro průvodce: venv, když už existuje; jinak systémový (dry-run, rané kroky)
wizard() {
    local py="python3"
    [ -x "$VENV/bin/python" ] && [ "$DRY_RUN" != "1" ] && py="$VENV/bin/python"
    local flags=()
    [ "$DRY_RUN" = "1" ] && flags+=(--dry-run)
    [ "$FRESH" = "1" ] && flags+=(--fresh)
    [ "$ASSUME_YES" = "1" ] && flags+=(--yes)
    (cd "$ROOT" && "$py" "$INSTALLER_DIR/wizard.py" "$@" "${flags[@]}")
}

pick_python() {
    if have python3.13; then echo python3.13; else echo python3; fi
}

# ════════════════════════════════════════════════════════════════════════════
step_preflight() {
    local model os_id os_code mem_mb free_gb
    model="$( (tr -d '\0' </proc/device-tree/model) 2>/dev/null || echo "neznámé")"
    # shellcheck disable=SC1091
    os_id="$(. /etc/os-release 2>/dev/null; echo "${ID:-?}")"
    # shellcheck disable=SC1091
    os_code="$(. /etc/os-release 2>/dev/null; echo "${VERSION_CODENAME:-?}")"
    mem_mb="$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo 2>/dev/null || echo 0)"
    free_gb="$(df -Pk "$ROOT" | awk 'NR==2 {print int($4/1024/1024)}')"
    info "zařízení : $model"
    info "systém   : $os_id $os_code, $(uname -m), RAM ${mem_mb} MB, volno ${free_gb} GB"
    info "Python   : $($(pick_python) --version 2>&1)"
    info "kořen    : $ROOT"

    if [[ "$model" != *"Raspberry Pi 5"* ]]; then
        warn "Tohle není Raspberry Pi 5. Hans potřebuje Pi 5 + AI Kit (Hailo-8L)."
        [ "$FORCE" = "1" ] || [ "$DRY_RUN" = "1" ] || die "Ukončuji (přepínač --force pokračuje i tak)."
    fi
    [ "$(uname -m)" = "aarch64" ] || warn "Očekávám 64bitový systém (aarch64)."
    if ! have python3.13; then
        warn "python3.13 nenalezen. Hans je laděný na Raspberry Pi OS Trixie (Python 3.13);"
        warn "run.sh volá python3.13 — instalátor mu ve venv vytvoří odkaz na $(pick_python)."
    fi
    [ "${free_gb:-0}" -ge 5 ] || warn "Málo místa (${free_gb} GB). Doporučuji aspoň 5 GB."
    [ "${mem_mb:-0}" -ge 7000 ] || warn "Méně než 8 GB RAM — lokální LLM na Pi nebude použitelné."
    if http_ok "https://pypi.org/simple/"; then ok "internet OK"; else warn "Nedostupný internet / PyPI."; fi

    # Ochrana živé instalace: na zařízení, kde Hans běží, se nic neinstaluje.
    local running=0
    if systemctl --user is-active --quiet hans 2>/dev/null || pgrep -f "python3.* main.py" >/dev/null 2>&1; then
        running=1
    fi
    # Hans spuštěný TÍMHLE instalátorem (krok service už proběhl) je v pořádku
    [ "$(state_get done_service)" = "1" ] && running=0
    if [ "$running" = "1" ]; then
        warn "Na tomhle zařízení právě běží Hans."
        if [ "$DRY_RUN" != "1" ] && [ "$FORCE" != "1" ]; then
            err "Instalátor je pro NOVÉ zařízení. Na běžícím Hansovi pouštěj jen --dry-run"
            err "(nic nemění). Pokud víš, co děláš: --force."
            exit 1
        fi
    fi
    if [ -f "$ROOT/config.private.json" ] && [ "$DRY_RUN" != "1" ]; then
        info "config.private.json už existuje — průvodce ho doplní (záloha se udělá)."
    fi
}

# ════════════════════════════════════════════════════════════════════════════
apt_pkg_exists() { apt-cache show "$1" >/dev/null 2>&1; }

step_apt() {
    run sudo apt-get update || warn "apt-get update selhal"
    local line pkgs=() optional=() failed=() opt pick alt
    while IFS= read -r line; do
        line="${line%%#*}"; line="$(echo "$line" | xargs)"
        [ -z "$line" ] && continue
        opt=0
        [[ "$line" == \?* ]] && { opt=1; line="${line#\?}"; }
        pick=""
        IFS='|' read -ra alts <<<"$line"
        for alt in "${alts[@]}"; do
            if [ "$DRY_RUN" = "1" ] || apt_pkg_exists "$alt"; then pick="$alt"; break; fi
        done
        if [ -z "$pick" ]; then
            if [ "$opt" = "1" ]; then warn "volitelný balíček $line v repozitáři není"; else failed+=("$line"); fi
            continue
        fi
        if [ "$opt" = "1" ]; then optional+=("$pick"); else pkgs+=("$pick"); fi
    done <"$INSTALLER_DIR/apt-packages.txt"

    if ! run sudo apt-get install -y "${pkgs[@]}"; then
        warn "Hromadná instalace selhala — zkouším balíčky po jednom."
        local p
        for p in "${pkgs[@]}"; do run sudo apt-get install -y "$p" || failed+=("$p"); done
    fi
    for p in "${optional[@]}"; do run sudo apt-get install -y "$p" || warn "volitelný $p se nenainstaloval"; done
    if [ ${#failed[@]} -gt 0 ]; then
        err "Nenainstalované povinné balíčky: ${failed[*]}"
        [[ " ${failed[*]} " == *" hailo-all "* ]] && \
            err "hailo-all: je Pi aktualizované (sudo apt full-upgrade) a AI Kit zapojený? Viz oficiální návod k AI Kitu."
        return 1
    fi
    ok "systémové balíčky nainstalované"
}

# ════════════════════════════════════════════════════════════════════════════
step_system() {
    local me grp groups=()
    me="$(whoami)"
    for grp in video audio spi i2c gpio render input; do
        getent group "$grp" >/dev/null && groups+=("$grp")
    done
    if [ ${#groups[@]} -gt 0 ]; then
        run sudo usermod -aG "$(IFS=,; echo "${groups[*]}")" "$me" && \
            ok "uživatel $me přidán do skupin: ${groups[*]} (platí po novém přihlášení)"
    fi
    if have raspi-config && confirm "Zapnout SPI a I2C (displeje očí, servo)?" a; then
        run sudo raspi-config nonint do_spi 0
        run sudo raspi-config nonint do_i2c 0
    fi
    # run.sh zastavuje hailortd přes sudo — bez hesla, jinak se autostart zasekne
    local rule="/etc/sudoers.d/hans-hailortd"
    if confirm "Povolit bez hesla 'systemctl stop hailortd' (potřebuje run.sh)?" a; then
        local tmp; tmp="$(mktemp)"
        printf '%s ALL=(root) NOPASSWD: /usr/bin/systemctl stop hailortd, /bin/systemctl stop hailortd, /usr/bin/pkill -x hailortd\n' "$me" >"$tmp"
        if [ "$DRY_RUN" = "1" ]; then
            info "[dry-run] zapsal bych $rule: $(cat "$tmp")"
        elif sudo visudo -cf "$tmp" >/dev/null; then
            run sudo install -m 0440 "$tmp" "$rule" && ok "sudo pravidlo $rule"
        else
            warn "sudoers pravidlo neprošlo kontrolou — přeskočeno"
        fi
        rm -f "$tmp"
    fi
}

# ════════════════════════════════════════════════════════════════════════════
# HailoRT → verze Hailo Model Zoo, ze které jsou zkompilované HEF (best effort;
# stažený soubor se hned ověří přes `hailortcli parse-hef`).
mz_version_for() {
    case "$1" in
        4.18*) echo v2.12.0 ;; 4.19*) echo v2.13.0 ;; 4.20*) echo v2.14.0 ;;
        4.21*) echo v2.15.0 ;; 4.22*) echo v2.16.0 ;; 4.23*) echo v2.17.0 ;;
        *) echo v2.17.0 ;;
    esac
}

step_hailo() {
    if [ -e /dev/hailo0 ]; then ok "/dev/hailo0 existuje"
    else warn "/dev/hailo0 chybí — po instalaci hailo-all je potřeba restart Pi (sudo reboot), pak spusť instalátor znovu s --only hailo."
    fi
    local rt=""
    if have hailortcli; then
        rt="$(hailortcli --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -n1)"
        info "HailoRT $rt"
        [ -e /dev/hailo0 ] && { hailortcli fw-control identify 2>&1 | head -n 6 || true; }
    else
        warn "hailortcli nenalezen (balíček hailo-all)."
    fi

    local f missing_sys=0
    for f in scrfd_2.5g_h8l.hef yolov8s_h8l.hef yolov5s_personface_h8l.hef yolov8s_pose_h8l_pi.hef; do
        if [ -f "/usr/share/hailo-models/$f" ]; then ok "$f"
        else warn "/usr/share/hailo-models/$f chybí"; missing_sys=1; fi
    done
    [ "$missing_sys" = "1" ] && { run sudo apt-get install -y hailo-models || true; }

    local dst="$ROOT/resources/arcface_mobilefacenet.hef"
    if [ -f "$dst" ]; then
        ok "resources/arcface_mobilefacenet.hef je na místě"
    else
        info "ArcFace model (rozpoznávání tváří) není v gitu. Možnosti:"
        info "  1) zkopírovat ze starého Pi (doporučeno — stejná verze jako dosud)"
        info "  2) stáhnout z Hailo Model Zoo ($(mz_version_for "$rt"), pro Hailo-8L)"
        info "  3) přeskočit (Hans poběží bez rozpoznávání tváří)"
        local ch; ch="$(ask "Volba" 2)"
        run mkdir -p "$ROOT/resources"
        case "$ch" in
            1)
                local src; src="$(ask "Odkud (např. pi@192.168.1.50:~/face-recognition/resources/)" "")"
                [ -n "$src" ] && run scp "${src%/}/arcface_mobilefacenet.hef" "$dst"
                ;;
            2)
                local url
                url="https://hailo-model-zoo.s3.eu-west-2.amazonaws.com/ModelZoo/Compiled/$(mz_version_for "$rt")/hailo8l/arcface_mobilefacenet.hef"
                run curl -fL --retry 3 -o "$dst" "$url" || warn "stažení selhalo: $url"
                ;;
            *) warn "ArcFace přeskočen." ;;
        esac
    fi
    if [ -f "$dst" ] && have hailortcli && [ "$DRY_RUN" != "1" ]; then
        if hailortcli parse-hef "$dst" >/dev/null 2>&1; then ok "ArcFace HEF ověřen (parse-hef)"
        else warn "hailortcli parse-hef hlásí problém — HEF možná neodpovídá verzi HailoRT."; fi
    fi
}

# ════════════════════════════════════════════════════════════════════════════
step_venv() {
    local py; py="$(pick_python)"
    if [ ! -x "$VENV/bin/python" ]; then
        run "$py" -m venv --system-site-packages "$VENV" || return 1
    else
        ok ".venv už existuje"
    fi
    # run.sh volá natvrdo `python3.13` — ve venv to jméno musí existovat
    if [ ! -e "$VENV/bin/python3.13" ] && { [ "$DRY_RUN" != "1" ] || [ "$py" != "python3.13" ]; }; then
        run ln -s python "$VENV/bin/python3.13"
        warn "venv není z Pythonu 3.13 — python3.13 ve venv je odkaz na $py"
    fi
    run "$VENV/bin/python" -m pip install --upgrade pip wheel || true

    # Zámek verzí balíčků z apt, aby je pip ve venv nepřepsal (ABI picamera2/simplejpeg)
    local cons="$DATA_DIR/constraints.txt"
    if [ "$DRY_RUN" != "1" ]; then
        "$py" - >"$cons" <<'EOF'
import importlib.metadata as md
keep = {"numpy", "scipy", "scikit-learn", "matplotlib", "pillow", "simplejpeg",
        "picamera2", "av", "psutil", "requests", "urllib3", "lgpio", "spidev", "hailort"}
seen = set()
for d in md.distributions():
    n = (d.metadata["Name"] or "").lower()
    if n in keep and n not in seen:
        seen.add(n)
        print("%s==%s" % (n, d.version))
EOF
        info "zamčené verze z apt: $(tr '\n' ' ' <"$cons")"
    fi

    if ! run "$VENV/bin/pip" install -c "$cons" -r "$INSTALLER_DIR/requirements.txt"; then
        warn "Hromadná instalace selhala — zkouším po jednom."
        local line bad=()
        while IFS= read -r line; do
            line="${line%%#*}"; line="$(echo "$line" | xargs)"; [ -z "$line" ] && continue
            run "$VENV/bin/pip" install -c "$cons" "$line" || bad+=("$line")
        done <"$INSTALLER_DIR/requirements.txt"
        [ ${#bad[@]} -eq 0 ] || err "Nenainstalováno: ${bad[*]}"
    fi
    local line
    while IFS= read -r line; do
        line="${line%%#*}"; line="$(echo "$line" | xargs)"; [ -z "$line" ] && continue
        run "$VENV/bin/pip" install -c "$cons" "$line" || warn "volitelný $line se nenainstaloval"
    done <"$INSTALLER_DIR/requirements-optional.txt"

    if confirm "Máš SunFounder Robot HAT (servo, reproduktor)?" a; then
        run "$VENV/bin/pip" install "git+https://github.com/sunfounder/robot-hat.git" \
            || warn "robot_hat se nenainstaloval"
        info "Zvuk Robot HATu: podle návodu SunFounder spusť jejich i2samp.sh (jednorázově, se sudo)."
    fi

    # Kontrola, že se klíčové moduly opravdu načtou z venv
    [ "$DRY_RUN" = "1" ] && return 0
    local mod bad=()
    for mod in numpy cv2 PIL requests fastapi uvicorn pydantic edge_tts webrtcvad noisereduce \
               openwakeword bs4 psutil picamera2 hailo_platform lgpio spidev tkinter; do
        if "$VENV/bin/python" -c "import $mod" >/dev/null 2>&1; then :; else bad+=("$mod"); fi
    done
    if [ ${#bad[@]} -eq 0 ]; then ok "všechny klíčové moduly se ve venv načtou"
    else warn "ve venv se nenačte: ${bad[*]}"; fi
}

# ════════════════════════════════════════════════════════════════════════════
step_network() {
    say "  Na PC (Linux s GPU) je potřeba Ollama + OpenWebUI. Pokud ještě neběží,"
    say "  zkopíruj na PC složku installer/pc/ a spusť tam:  bash install_pc.sh"
    confirm "Je PC připravené (nebo pokračovat i tak)?" a || return 1
    wizard network || return 1

    local pc_user pc_ip
    pc_user="$(wizard get pc_remote.user 2>/dev/null)"
    pc_ip="$(wizard get wol_pc_ip 2>/dev/null)"
    if [ -n "$pc_user" ] && [ -n "$pc_ip" ] && [ ! -f "$HOME/.ssh/hans_pc" ]; then
        if confirm "Vytvořit SSH klíč pro přístup Hanse na PC ($pc_user@$pc_ip)?" a; then
            run mkdir -p "$HOME/.ssh"
            run ssh-keygen -t ed25519 -N "" -C "hans@$(hostname)" -f "$HOME/.ssh/hans_pc"
            run ssh-copy-id -i "$HOME/.ssh/hans_pc.pub" "$pc_user@$pc_ip" \
                || warn "ssh-copy-id selhal — klíč nahraj ručně (obsah ~/.ssh/hans_pc.pub do ~/.ssh/authorized_keys na PC)"
        fi
    fi
}

step_llm() {
    say "  Texty persony (identita, společník, skloňování jmen) napíše jazykový model."
    local ch rc
    # Testovací režim (installer/test_install.sh): generátor je předem daný
    if [ -n "${HANS_GEN_URL:-}" ]; then
        info "Generátor předem nastavený: ${HANS_GEN_MODEL:-auto} @ $HANS_GEN_URL"
        wizard llm --url "$HANS_GEN_URL" --model "${HANS_GEN_MODEL:-}"
        return
    fi
    while true; do
        ch="$(ask "Kde má běžet? 1) Ollama na PC (doporučeno)  2) lokálně na Pi  3) ručně přes Claude/ChatGPT" 1)"
        case "$ch" in
            1) wizard llm; rc=$? ;;
            2)
                if ! have ollama; then
                    info "Instaluji Ollamu na Pi (oficiální skript ollama.com)…"
                    run_sh "curl -fsSL https://ollama.com/install.sh | sh" || { warn "instalace Ollamy selhala"; continue; }
                fi
                state_set local_ollama 1
                wizard llm --local; rc=$?
                ;;
            3) wizard llm --manual; rc=$? ;;
            *) continue ;;
        esac
        [ "$rc" = "0" ] && return 0
        warn "Generátor se nepodařilo nastavit."
        confirm "Zkusit jinou možnost?" a || return 0
    done
}

step_persona()   { wizard persona; }
step_models()    { wizard models; }
step_knowledge() { wizard knowledge; }

# ════════════════════════════════════════════════════════════════════════════
step_service() {
    local unit_dir="$HOME/.config/systemd/user" unit
    unit="$unit_dir/hans.service"
    local tmp; tmp="$(mktemp)"
    sed -e "s#@ROOT@#$ROOT#g" -e "s#@HOME@#$HOME#g" "$INSTALLER_DIR/hans.service.in" >"$tmp"
    if [ "$DRY_RUN" = "1" ]; then
        info "[dry-run] $unit by vypadal takto:"; sed 's/^/      /' "$tmp"; rm -f "$tmp"; return 0
    fi
    mkdir -p "$unit_dir"
    if [ -f "$unit" ] && ! cmp -s "$tmp" "$unit"; then
        cp "$unit" "$unit.bak.$(date +%Y%m%d%H%M%S)"
        info "původní hans.service zazálohována"
    fi
    install -m 0644 "$tmp" "$unit"; rm -f "$tmp"
    run systemctl --user daemon-reload
    run systemctl --user enable hans.service && ok "hans.service zapnutá pro autostart"
    if [ "$(state_get local_ollama)" = "1" ] && systemctl is-enabled --quiet ollama 2>/dev/null; then
        if confirm "Lokální Ollama na Pi už není potřeba — vypnout ji (uvolní RAM pro Hanse)?" a; then
            run sudo systemctl disable --now ollama
        fi
    fi
}

step_faces() {
    say "  Hans se naučí poznávat lidi z domácnosti podle obličeje. Potřebuje k tomu"
    say "  běžet (kamera + Hailo) a zapojený displej, na kterém se zápis potvrzuje."
    if [ "$DRY_RUN" != "1" ] && ! http_ok "http://127.0.0.1:7860/api/faces"; then
        if [ ! -e /dev/hailo0 ]; then
            warn "Hailo zatím není vidět (/dev/hailo0) — nejdřív restart Pi, pak:"
            warn "  bash installer/install.sh --only faces"
            return 0
        fi
        confirm "Spustit teď Hanse (systemctl --user start hans)?" a || {
            info "Později:  bash installer/install.sh --only faces"; return 0; }
        run systemctl --user start hans
        info "Čekám, až Hans naběhne (modely Hailo, webadmin)…"
        local _
        for _ in $(seq 1 60); do http_ok "http://127.0.0.1:7860/api/faces" && break; sleep 2; done
        http_ok "http://127.0.0.1:7860/api/faces" || {
            warn "Webadmin neodpovídá — viz journalctl --user -u hans -f"; return 1; }
    fi
    wizard faces
}

step_finish() {
    wizard show || true
    cat <<EOF

${C_BLD}Ruční kroky, které instalátor neudělá:${C_RST}
  • Restart Pi (driver Hailo, nové skupiny uživatele):  sudo reboot
  • Kamera:  rpicam-hello -t 3000   musí ukázat obraz
  • Displeje očí a servo zapojit podle pinmapy (scripts/Eye_sphere.py)
  • Zvuk: ověř výstup  speaker-test -c2 -t wav ; ALSA zařízení je v configu tts.alsa_device
  • Obličeje: pokud krok faces neproběhl, spusť ho později:
      bash installer/install.sh --only faces
  • Avatar (volitelné): ComfyUI na PC — viz deploy/SETUP_PC.md, kap. 3

${C_BLD}Spuštění:${C_RST}
  systemctl --user start hans        (logy: journalctl --user -u hans -f)
  nebo ručně:  PATH="$VENV/bin:\$PATH" ./run.sh

Log instalace: $LOG_FILE
EOF
}

# ════════════════════════════════════════════════════════════════════════════
selected() {
    local s="$1"
    [ -n "$ONLY" ] && [[ "$ONLY" != *",$s,"* ]] && return 1
    [ -n "$SKIP" ] && [[ "$SKIP" == *",$s,"* ]] && return 1
    return 0
}

header "HANS — instalace na nové zařízení"
[ "$DRY_RUN" = "1" ] && warn "DRY-RUN: nic se neinstaluje ani nezapisuje (config → data/installer/dryrun/)."
{ echo; echo "=== $(date) install.sh $* ==="; } >>"$LOG_FILE"

n=0; total=${#STEPS[@]}
for s in "${STEPS[@]}"; do
    n=$((n + 1))
    selected "$s" || continue
    header "[$n/$total] $s — ${STEP_DESC[$s]}"
    if [ "$REDO" != "1" ] && [ "$(state_get "done_$s")" = "1" ] && [ "$s" != "finish" ] && [ "$s" != "preflight" ]; then
        confirm "Krok už proběhl. Spustit znovu?" n || { ok "přeskočeno"; continue; }
    fi
    if "step_$s"; then
        state_set "done_$s" 1
    else
        err "Krok $s nedoběhl."
        if [ "$s" = "preflight" ]; then exit 1; fi
        confirm "Pokračovat dalším krokem?" a || { info "Instalaci doběhneš znovu: bash installer/install.sh"; exit 1; }
    fi
done
