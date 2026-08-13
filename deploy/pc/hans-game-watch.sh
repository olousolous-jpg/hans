#!/usr/bin/env bash
# hans-game-watch.sh — HANS_GAME_AUTODETECT_V1
# PC-side watcher pro AUTO herní mód. Sleduje procesy a při BĚŽÍCÍ HŘE (ne jen
# otevřeném launcheru) přepne Hansův herní mód (uvolní VRAM z Ollamy pro hru).
# Pokrývá Steam i Heroic (Proton/Wine) JEDNOU službou, bez per-hra nastavení.
#
# Běží jako systemd USER služba s lingerem → startuje od bootu i bez přihlášení
# (proto funguje bez tvé přítomnosti u PC). Bez závislostí: bash + curl + ps.
# Instalace remote z Pi — viz hans-game-watch.service.
set -u

# ── Konfigurace (přepsatelná přes Environment= v .service) ───────────────────
HANS="${HANS:-http://192.168.1.50:7860}"   # web_admin na Raspberry (Hansovo tělo)
POLL_S="${POLL_S:-3}"      # jak často kontrolovat procesy (s)
GRACE_S="${GRACE_S:-20}"   # jak dlouho musí být hra PRYČ, než vrátíme mozek —
                           # kryje krátké mezery při načítání a dobíhající wineserver

# Signatury SKUTEČNÉ hry v cmdline procesů. Idle Steam/Heroic je NEMAJÍ; kernelové
# thready ([oom_reaper] apod.) jsou odfiltrované (řádky v hranatých závorkách).
# Native hru bez Wine přidej přes EXTRA_PAT v .service (např. její binárku).
GAME_PAT='SteamLaunch|AppId=[0-9]|[Pp]roton|wineserver|wine64|wine-preloader|pv-bwrap|gamescope'
EXTRA_PAT="${EXTRA_PAT:-}"
[ -n "$EXTRA_PAT" ] && GAME_PAT="${GAME_PAT}|${EXTRA_PAT}"

log() { logger -t hans-game-watch "$*" 2>/dev/null || printf 'hans-game-watch: %s\n' "$*"; }

game_running() {
    # POZOR: výpis procesů zachyť NEJDŘÍV do proměnné a matchuj až potom —
    # kdyby se matchovalo v pipe (ps | grep -qE "$GAME_PAT"), měl by ten grep
    # pattern ve svém vlastním cmdline a `ps` by ho viděl → watcher by „našel
    # hru" sám v sobě → trvalý herní mód. Odfiltruj i kernelové [thready].
    local procs
    procs=$(ps -eo args 2>/dev/null | grep -vE '^\[')
    printf '%s\n' "$procs" | grep -qE "$GAME_PAT"
}
brain_paused() {   # skutečný stav z Pi (kvůli úklidu po pádu) — 0=paused
    local s; s=$(curl -s -m 6 "$HANS/api/brain/status" 2>/dev/null)
    [[ "$s" == *'"game_mode":true'* || "$s" == *'"game_mode": true'* ]]
}
pause_brain()  { curl -s -m 45 -X POST "$HANS/api/brain/pause"  >/dev/null 2>&1; }
resume_brain() { curl -s -m 10 -X POST "$HANS/api/brain/resume" >/dev/null 2>&1; }

# HANS_GAME_LEFTOVER_V1 — herní/wine RUNTIME procesy, které po zavření hry NESMÍ
# přežít. Vědomě UŽŠÍ a jistější než GAME_PAT: jen jednoznačná herní rezidua
# (wineserver = řídící proces wine prefixu; herní binárky; steam-runtime kontejner).
# Systémové wine služby (winedevice/services.exe) sem NEDÁVÁME — zmizí s wineserverem.
LEFT_PAT='wineserver|GenshinImpact|YuanShen|[Mm]iHoYo|HoYoPlay|pressure-vessel|pv-bwrap|gamescope'
LEFT_GRACE_S="${LEFT_GRACE_S:-15}"   # extra grace po resume (nad GRACE_S) na doběhnutí cleanup

check_leftovers() {
    # ověř, že se hra po zavření opravdu uklidila; jinak nahlas Hansovi
    sleep "$LEFT_GRACE_S"
    game_running && return   # mezitím se rozjela další hra → neřeš
    local procs left n gpu names
    procs=$(ps -eo pid,stat,args 2>/dev/null | grep -vE '^\[')
    left=$(printf '%s\n' "$procs" \
           | grep -iE "$LEFT_PAT" \
           | grep -vE 'grep|hans-game-watch|/opt/Heroic|legendary|umu_run')
    if [ -z "$left" ]; then
        log "úklid po hře OK — nic nezůstalo viset"
        return
    fi
    n=$(printf '%s\n' "$left" | grep -c .)
    gpu=$(cat /sys/class/drm/card*/device/gpu_busy_percent 2>/dev/null | head -1)
    # jméno[stav] — D=zaseklý Z=zombie jsou zvlášť podezřelé
    names=$(printf '%s\n' "$left" | awk '{c=$3; sub(/.*[\\/]/,"",c); printf "%s[%s] ", c, $2}' | cut -c1-150)
    log "POZOR: po hře zůstalo $n proc: ${names}(GPU ${gpu:-?}%)"
    curl -s -m 8 -G "$HANS/api/game/leftover" \
         --data-urlencode "desc=zůstalo ${n} proc: ${names}(GPU ${gpu:-?}%)" >/dev/null 2>&1
}

log "start (HANS=$HANS poll=${POLL_S}s grace=${GRACE_S}s)"

# Úklid při startu: nic se nehraje, ale mozek je paused (zbytek po pádu hry /
# rebootu) → vrať ho. Zároveň kryje případ, kdy watcher spadl a systemd ho zvedl.
if ! game_running && brain_paused; then
    log "start: žádná hra, ale mozek je paused → resume (úklid)"
    resume_brain
fi

state="idle"; last_seen=0
while true; do
    now=$(date +%s)
    if game_running; then
        last_seen=$now
        if [ "$state" = idle ]; then
            state=playing
            log "HRA detekována → herní mód ZAP (uvolňuji VRAM)"
            pause_brain
        fi
    elif [ "$state" = playing ] && [ $((now - last_seen)) -ge "$GRACE_S" ]; then
        state=idle
        log "hra skončila (${GRACE_S}s klid) → herní mód VYP (vracím mozek)"
        resume_brain
        check_leftovers   # HANS_GAME_LEFTOVER_V1 — uklidila se hra opravdu?
    fi
    sleep "$POLL_S"
done
