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
# HANS_GAME_STARTUP_RECONCILE_V1 — kolikrát a jak často se po startu ptát Pi na
# stav mozku, než to vzdáme (20 x 15 s = 5 min; po bootu síť stojí dávno předtím).
START_TRIES="${START_TRIES:-20}"
START_WAIT_S="${START_WAIT_S:-15}"

# Signatury SKUTEČNÉ hry v cmdline procesů. Idle Steam/Heroic je NEMAJÍ; kernelové
# thready ([oom_reaper] apod.) jsou odfiltrované (řádky v hranatých závorkách).
# Native hru bez Wine přidej přes EXTRA_PAT v .service (např. její binárku).
GAME_PAT='SteamLaunch|AppId=[0-9]|[Pp]roton|wineserver|wine64|wine-preloader|pv-bwrap|gamescope'
EXTRA_PAT="${EXTRA_PAT:-}"
[ -n "$EXTRA_PAT" ] && GAME_PAT="${GAME_PAT}|${EXTRA_PAT}"

# HANS_GAME_PAT_NOT_VPN_V1 — procesy, ktere vypadaji jako Proton, ale hra to neni.
# Dolozeno 20.-23.8.: `[Pp]roton` v GAME_PAT chytal `python3 -m proton.vpn.daemon`
# (ProtonVPN startuje s bootem a bezi porad) -> watcher vesel do stavu "hraje se"
# ve stejnou vterinu jako start a UZ Z NEJ NIKDY NEVYSEL, takze SKUTECNA hra
# nevyvolala zadny prechod. Vzor `[Pp]roton` schvalne NEZUZUJEME (ladil se nazivo
# 16.7. na Heroic+GE-Proton) — jen odecteme jmenovite to, co hra neni.
# `protondrive` kryje planovanou zalohu pres rclone, ktera by tuhle chybu jinak
# za mesic vyrobila znovu, tentokrat bez zjevne souvislosti.
NOTGAME_PAT='proton\.vpn|protonvpn|protonmail|proton-bridge|protondrive|rclone'
GAME_MATCH=""              # cmdline procesu, ktery watcher povazuje za hru (do logu)

log() { logger -t hans-game-watch "$*" 2>/dev/null || printf 'hans-game-watch: %s\n' "$*"; }

game_running() {
    # POZOR: výpis procesů zachyť NEJDŘÍV do proměnné a matchuj až potom —
    # kdyby se matchovalo v pipe (ps | grep -qE "$GAME_PAT"), měl by ten grep
    # pattern ve svém vlastním cmdline a `ps` by ho viděl → watcher by „našel
    # hru" sám v sobě → trvalý herní mód. Odfiltruj i kernelové [thready].
    local procs
    procs=$(ps -eo args 2>/dev/null | grep -vE '^\[')
    # HANS_GAME_PAT_NOT_VPN_V1: nejdriv odecti ne-hry, teprve pak hledej hru.
    # Match si drz v GAME_MATCH — bez toho clovek v logu nepozna, ze "HRA" je VPN.
    GAME_MATCH=$(printf '%s\n' "$procs" | grep -viE "$NOTGAME_PAT" \
                 | grep -E "$GAME_PAT" | head -1 | cut -c1-120)
    [ -n "$GAME_MATCH" ]
}
# HANS_GAME_STARTUP_RECONCILE_V1 — vrací TŘI stavy, ne dva. Dřív `brain_paused`
# nerozlišilo „mozek běží" od „Pi neodpovědělo" (obojí = nenulový návrat), takže
# úklid po bootu TIŠE přeskočil, když ještě nestála síť. Doloženo 25.8.: PC reboot
# v 15:51, mozek pauznutý od 15:47, úklidová hláška se v journalu NIKDY neobjevila
# a Hans byl němý — každá odpověď v chatu skončila na „herní mód, VRAM patří hře".
# Prázdná/neočekávaná odpověď je ZÁMĚRNĚ `unreachable`, ne „je čisto".
brain_state() {   # echo: paused | free | unreachable
    local s
    s=$(curl -s -m 6 "$HANS/api/brain/status" 2>/dev/null)
    case "$s" in
        *'"game_mode":true'*|*'"game_mode": true'*)   echo paused ;;
        *'"game_mode":false'*|*'"game_mode": false'*) echo free ;;
        *)                                            echo unreachable ;;
    esac
}
# HANS_GAME_POST_VERIFY_V1 — driv se vysledek curlu zahazoval (`>/dev/null 2>&1`),
# takze se hlaska "herni mod ZAP" vypsala i kdyz Pi nic nedostalo. Dolozeno 20.8.:
# watcher po bootu "zapnul", ale na Pi po tom nezustala ani stopa (nestala sit).
post_brain() {   # $1 = pause|resume, $2 = timeout s -> 0 JEN kdyz Pi potvrdilo 200
    local code
    code=$(curl -s -o /dev/null -w '%{http_code}' -m "$2" \
           -X POST "$HANS/api/brain/$1" 2>/dev/null)
    [ "$code" = "200" ]
}
pause_brain()  { post_brain pause 45; }
resume_brain() { post_brain resume 10; }

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
# HANS_GAME_STARTUP_RECONCILE_V1: dřív to byl JEDEN pokus hned po startu — když Pi
# neodpovědělo, úklid se tiše přeskočil a nic se nezalogovalo. Teď se to zkouší
# opakovaně PŘÍMO V HLAVNÍ SMYČCE (aby detekce hry mezitím běžela dál) a když se
# to nepovede ani napodesáté, řekne se to NAHLAS — ticho vypadalo jako úspěch.
start_pending=1; start_tries=0; last_start_try=0

state="idle"; last_seen=0; last_fail_log=0
while true; do
    now=$(date +%s)
    if game_running; then
        last_seen=$now
        start_pending=0   # hra běží → startovní úklid je bezpředmětný
        if [ "$state" = idle ]; then
            # HANS_GAME_POST_VERIFY_V1: stav prepneme AZ kdyz Pi potvrdilo. Pri
            # selhani zustava "idle" -> zkusi se znovu pristi tick (typicky po
            # bootu, nez stoji sit). Log throttlovany na 1x/60 s, at nezaplavi journal.
            if pause_brain; then
                state=playing
                log "HRA detekována ($GAME_MATCH) → herní mód ZAP (uvolňuji VRAM)"
            elif [ $((now - last_fail_log)) -ge 60 ]; then
                last_fail_log=$now
                log "POZOR: hra běží ($GAME_MATCH), ale POST /brain/pause NEPROŠEL → zkouším dál"
            fi
        fi
    elif [ "$state" = playing ] && [ $((now - last_seen)) -ge "$GRACE_S" ]; then
        if resume_brain; then
            state=idle
            log "hra skončila (${GRACE_S}s klid) → herní mód VYP (vracím mozek)"
            check_leftovers   # HANS_GAME_LEFTOVER_V1 — uklidila se hra opravdu?
        elif [ $((now - last_fail_log)) -ge 60 ]; then
            last_fail_log=$now
            log "POZOR: hra skončila, ale POST /brain/resume NEPROŠEL → Hans je bez mozku, zkouším dál"
        fi
    elif [ "$start_pending" = 1 ] && [ "$state" = idle ] \
         && [ $((now - last_start_try)) -ge "$START_WAIT_S" ]; then
        # HANS_GAME_STARTUP_RECONCILE_V1 — dotahni úklid po bootu/pádu.
        last_start_try=$now; start_tries=$((start_tries + 1))
        case "$(brain_state)" in
            paused)
                start_pending=0
                if resume_brain; then
                    log "úklid po startu: nehraje se, ale mozek byl paused → VRÁCEN"
                else
                    log "POZOR: úklid po startu: resume NEPROŠEL — Pi neodpovědělo 200"
                fi ;;
            free)
                start_pending=0 ;;   # čisto — mlčky, ať se journal nezaplevelí
            *)
                if [ "$start_tries" -ge "$START_TRIES" ]; then
                    start_pending=0
                    log "POZOR: úklid po startu NEPROBĚHL — Pi neodpovědělo na /brain/status ani po $START_TRIES pokusech"
                fi ;;
        esac
    fi
    sleep "$POLL_S"
done
