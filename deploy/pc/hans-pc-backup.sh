#!/usr/bin/env bash
# HANS_PC_RESTIC_V1 (29. 9.) — týdenní rozdílová záloha PC na NAS (restic).
# Navazuje na ruční obnovovací sadu z 9. 7. (NAS D/hans_pc_2026-07-09_*):
# ta zůstává výchozím bodem pro obnovu celého PC, tohle drží průběžné změny
# jen toho, co se nedá znovu stáhnout (konfigurace, data OpenWebUI/RAG, domov).
# Timeshift ZAMÍTNUT: neumí cíl na SMB a vynechává domov/data aplikací.
# Běží jako root (systemd timer): /etc a docker volume. Repozitář bez hesla
# (--insecure-no-password, domácí NAS; rozhodnutí uživatele).
set -euo pipefail

REPO="${HANS_PC_RESTIC_REPO:-/mnt/D/hans_pc_restic}"
HOME_U="${HANS_PC_HOME:-$(getent passwd 1000 | cut -d: -f6)}"
META="/var/backups/hans_pc_meta"
R=(restic -r "$REPO" --insecure-no-password)
export RESTIC_CACHE_DIR=/var/cache/restic

# /mnt/D je automount: přístup ho připojí; když NAS nejde, skončí chybou
ls "$(dirname "$REPO")/" >/dev/null 2>&1 || { echo "NAS nepřipojen: $(dirname "$REPO")"; exit 2; }
[ -f "$REPO/config" ] || "${R[@]}" init

# --- metadata (seznamy balíčků, služby, modely) ---
mkdir -p "$META"
pacman -Qqe  > "$META/pkglist-explicit.txt"
pacman -Qqem > "$META/pkglist-aur.txt" || true
systemctl list-unit-files --state=enabled --no-legend > "$META/systemd-enabled-system.txt" || true
sudo -u "$(stat -c %U "$HOME_U")" XDG_RUNTIME_DIR="/run/user/$(stat -c %u "$HOME_U")" \
    systemctl --user list-unit-files --state=enabled --no-legend > "$META/systemd-enabled-user.txt" 2>/dev/null || true
ollama list > "$META/ollama-list.txt" 2>/dev/null || true
docker inspect open-webui > "$META/docker-openwebui-inspect.json" 2>/dev/null || true
crontab -u "$(stat -c %U "$HOME_U")" -l > "$META/crontab-user.txt" 2>/dev/null || true

NICE=(nice -n 19 ionice -c3)

# --- 1) systém: bez limitu velikosti ---
SYS=(/etc "$META")
OW=$(docker volume inspect -f '{{.Mountpoint}}' open-webui 2>/dev/null || true)
[ -n "$OW" ] && [ -d "$OW" ] && SYS+=("$OW")
"${NICE[@]}" "${R[@]}" backup --tag system --one-file-system "${SYS[@]}"

# --- 2) domov: jen vybrané, soubory nad 200 MB (váhy modelů) vynechány ---
H=()
for p in .ssh .config .local/share/cyberpunk-rezim hans hans_mereni hans_mask Plocha Obrázky \
         Dokumenty piper ComfyUI notagen stt_test hans_wake; do
    [ -e "$HOME_U/$p" ] && H+=("$HOME_U/$p")
done
while IFS= read -r f; do H+=("$f"); done < <(find "$HOME_U" -maxdepth 1 -type f -name '.*')
"${NICE[@]}" "${R[@]}" backup --tag home --exclude-larger-than 200M \
    --exclude "$HOME_U/.config/heroic/tools" \
    --exclude "$HOME_U/.config/BraveSoftware/*/*/Cache" \
    --exclude "$HOME_U/.config/BraveSoftware/*/*/Code Cache" \
    --exclude "$HOME_U/ComfyUI/models" --exclude "$HOME_U/ComfyUI/.venv" \
    --exclude "$HOME_U/ComfyUI/gpucore.*" --exclude "$HOME_U/ComfyUI/.git" \
    --exclude '**/.venv' --exclude '**/venv' --exclude '**/__pycache__' \
    --exclude '**/node_modules' --exclude "$HOME_U/stt_test/prefix" \
    --exclude "$HOME_U/stt_test/*/build" \
    --exclude "$HOME_U/hans_wake/training" \
    "${H[@]}"

# --- 3) historie ---
"${NICE[@]}" "${R[@]}" forget --keep-weekly 8 --keep-monthly 6 --prune
date +%s > "$REPO/POSLEDNI_OK"
echo "hans-pc-backup: OK"
