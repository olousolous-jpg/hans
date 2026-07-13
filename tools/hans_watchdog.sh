#!/usr/bin/env bash
# HANS_WATCHDOG_V1 — pojistka proti zatuhlému Hansovi.
#
# Hansův hlavní loop tepe do data/.hans_heartbeat (á 5 s, CAMERA_STALL_RECOVERY_V1).
# Když srdce přestane bít, ale proces "žije" (zaseklý loop / deadlock v libcamera),
# nikdo to nepozná — Hans jen mlčí a menu nereaguje. Tenhle skript to pozná a restartuje ho.
#
# Spouští hans-watchdog.timer každou minutu.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HB="$ROOT/data/.hans_heartbeat"
LOG="$ROOT/data/watchdog.log"

STALE_S="${HANS_WD_STALE_S:-120}"   # srdce mlčí déle → zásah
GRACE_S="${HANS_WD_GRACE_S:-120}"   # po startu dej čas na init kamery

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >> "$LOG"; }

# Hans neběží (vypnutý ručně) → watchdog do toho nemluví.
systemctl --user is-active --quiet hans || exit 0

# Čerstvě nastartovaný → kamera se ještě probouzí, nech ho být.
started_us="$(systemctl --user show hans -p ActiveEnterTimestampMonotonic --value 2>/dev/null || echo 0)"
now_us="$(awk '{printf "%d", $1 * 1000000}' /proc/uptime)"
if [ "${started_us:-0}" -gt 0 ]; then
    up_s=$(( (now_us - started_us) / 1000000 ))
    [ "$up_s" -lt "$GRACE_S" ] && exit 0
fi

# Srdce vůbec netepe → ještě nedoběhl init (starý build bez heartbeatu) → nezasahuj.
[ -f "$HB" ] || exit 0

age=$(( $(date +%s) - $(stat -c %Y "$HB") ))
[ "$age" -lt "$STALE_S" ] && exit 0

log "ZÁSAH: heartbeat mlčí ${age}s (práh ${STALE_S}s) → restartuji hans"
systemctl --user restart hans && log "restart OK" || log "restart SELHAL"
