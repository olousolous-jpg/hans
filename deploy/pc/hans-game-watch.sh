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

# HANS_GAME_LEFTOVER_STEAMCLIENT_V1 — klient Steamu běží ve VLASTNÍM kontejneru
# Steam Runtime (srt-bwrap + pv-adverb + steamwebhelper), jehož cmdline obsahuje
# „pressure-vessel" stejně jako herní kontejnery. Doloženo 23. 9.: 6 hlášení
# „zůstalo srt-bwrap pv-adverb" za 1 h, pokaždé tentýž kontejner běžícího klienta.
# Vyřazuje se jen kořen `Steam/steamrt64/pv-runtime` a jeho potomci; herní
# kontejnery (umu steamrt4, SteamLinuxRuntime_*) se hlídají dál, i když je spustil Steam.
steam_client_pids() {   # stdin: "pid ppid args" → PID kontejneru klienta Steamu + potomků
    awk '{ pp[$1] = $2; if ($0 ~ /\/steamrt64\/pv-runtime\//) root[$1] = 1 }
         END { for (p in pp) { q = p; n = 0
                   while ((q in pp) && n < 64) { if (q in root) { print p; break }
                                                 q = pp[q]; n++ } } }'
}

check_leftovers() {
    # ověř, že se hra po zavření opravdu uklidila; jinak nahlas Hansovi
    sleep "$LEFT_GRACE_S"
    game_running && return   # mezitím se rozjela další hra → neřeš
    local procs left n gpu names skip
    procs=$(ps -eo pid,stat,args 2>/dev/null | grep -vE '^\[')
    skip=" $(ps -eo pid=,ppid=,args= 2>/dev/null | steam_client_pids | tr '\n' ' ') "
    left=$(printf '%s\n' "$procs" \
           | grep -iE "$LEFT_PAT" \
           | grep -vE 'grep|hans-game-watch|/opt/Heroic|legendary|umu_run' \
           | awk -v skip="$skip" 'index(skip, " " $1 " ") == 0')
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

# HANS_GAME_STUCK_V1 (27. 9.) — ZASEKLÉ ZBYTKY PO HŘE. Doloženo 26. 9.: hra
# detekována 00:39 (pressure-vessel), konec se NIKDY neohlásil a herní mód držel
# až do restartu PC 01:55 — po zavření Cyberpunku zůstaly viset procesy, které
# vzor GAME_PAT chytá stejně jako hru. Rozlišení: skutečná hra i v menu či na
# načítací obrazovce PÁLÍ procesor; zaseklý zbytek ne. Když procesy hry za
# STUCK_WIN_S spotřebují méně než STUCK_CPU_S sekund procesoru, bere se hra za
# skončenou: mozek se vrátí a Hansovi se nahlásí, co zůstalo (nic se nezabíjí).
STUCK_WIN_S="${STUCK_WIN_S:-60}"
STUCK_CPU_S="${STUCK_CPU_S:-1}"     # 1 s CPU za minutu ≈ 1,7 % jednoho jádra
TCK=$(getconf CLK_TCK 2>/dev/null || echo 100)

# HANS_GAME_STUCK_KILL_V1 (28. 9., pokyn uživatele) — zaseklé zbytky po hře se
# nejdřív NAHLÁSÍ (jako dosud) a za KILL_AFTER_S je watcher UKONČÍ: celý strom
# procesů hry (kořeny podle GAME_PAT + potomci), SIGTERM, po 10 s SIGKILL.
# Ožijí-li mezitím (hra zase pracuje) nebo zmizí samy, ukončení se ruší.
KILL_AFTER_S="${KILL_AFTER_S:-120}"
kill_at=0

game_pids() {        # PID procesů hry a všech jejich potomků
    local procs roots
    procs=$(ps -eo pid=,args= 2>/dev/null)
    roots=" $(printf '%s\n' "$procs" | grep -vE '^ *[0-9]+ \[' | grep -viE "$NOTGAME_PAT" \
             | grep -E "$GAME_PAT" | awk '{print $1}' | tr '\n' ' ') "
    ps -eo pid=,ppid= 2>/dev/null | awk -v roots="$roots" -v me="$$" '
        { pp[$1] = $2 }
        END { for (p in pp) { if (p == me) continue; q = p; n = 0
                  while (q != "" && n < 64) { if (index(roots, " " q " ")) { print p; break }
                                              if (!(q in pp)) break; q = pp[q]; n++ } } }'
}
kill_leftovers() {
    local pids names left
    names=$(game_names)
    pids=$(game_pids | tr '\n' ' ')
    [ -z "${pids// /}" ] && return 0
    kill -TERM $pids 2>/dev/null
    sleep 10
    left=$(game_pids | tr '\n' ' ')
    [ -n "${left// /}" ] && kill -KILL $left 2>/dev/null
    sleep 1
    left=$(game_pids | wc -l)
    log "zaseklé zbytky po hře UKONČENY (${names}) — zbývá ${left} proc"
    curl -s -m 8 -G "$HANS/api/game/leftover" --data-urlencode "stav=ukonceno" \
         --data-urlencode "desc=${names}$( [ "$left" -gt 0 ] && echo "(${left} procesů se ukončit nepodařilo)")" \
         >/dev/null 2>&1
}

game_cpu_ticks() {   # utime+stime procesů hry I VŠECH jejich potomků
    # Potomci nutně: samotná herní binárka (…\\Cyberpunk2077.exe pod Wine)
    # vzor GAME_PAT mít nemusí — bez ní by skutečné hraní vypadalo jako klid.
    local procs roots all p s=0 t
    procs=$(ps -eo pid=,args= 2>/dev/null)
    roots=" $(printf '%s\n' "$procs" | grep -vE '^ *[0-9]+ \[' | grep -viE "$NOTGAME_PAT" \
             | grep -E "$GAME_PAT" | awk '{print $1}' | tr '\n' ' ') "
    all=$(ps -eo pid=,ppid= 2>/dev/null | awk -v roots="$roots" '
        { pp[$1] = $2 }
        END { for (p in pp) { q = p; n = 0
                  while (q != "" && n < 64) { if (index(roots, " " q " ")) { print p; break }
                                              if (!(q in pp)) break; q = pp[q]; n++ } } }')
    for p in $all; do
        t=$(sed 's/.*) //' "/proc/$p/stat" 2>/dev/null | awk '{print $12+$13}')
        [ -n "$t" ] && s=$((s + t))
    done
    echo "$s"
}
game_names() {       # jména procesů, které vypadají jako hru (do hlášení)
    local procs
    procs=$(ps -eo stat=,args= 2>/dev/null)
    printf '%s\n' "$procs" | grep -vE '^\S+ \[' | grep -viE "$NOTGAME_PAT" | grep -E "$GAME_PAT" \
        | awk '{c=$2; sub(/.*[\\/]/,"",c); printf "%s[%s] ", c, $1}' | cut -c1-200
}
stuck_mark_t=0; stuck_mark_cpu=0

log "start (HANS=$HANS poll=${POLL_S}s grace=${GRACE_S}s stuck=${STUCK_CPU_S}s/${STUCK_WIN_S}s)"

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
        # HANS_GAME_STUCK_KILL_V1 — lhůta po nahlášení vypršela → ukončit
        if [ "$state" = stuck ] && [ "$kill_at" -gt 0 ] && [ "$now" -ge "$kill_at" ]; then
            kill_at=0
            kill_leftovers
        fi
        # HANS_GAME_STUCK_V1 — běží hra doopravdy, nebo jen visí zbytky?
        if [ "$state" != idle ] && [ $((now - stuck_mark_t)) -ge "$STUCK_WIN_S" ]; then
            cpu=$(game_cpu_ticks)
            if [ "$stuck_mark_t" -gt 0 ]; then
                d=$((cpu - stuck_mark_cpu))
                if [ "$state" = playing ] && [ "$d" -lt $((STUCK_CPU_S * TCK)) ] && [ "$d" -ge 0 ]; then
                    if resume_brain; then
                        state=stuck
                        names=$(game_names)
                        log "hra NEBĚŽÍ, jen visí zbytky (CPU ${d}/${TCK} s za ${STUCK_WIN_S}s): ${names}→ herní mód VYP"
                        curl -s -m 8 -G "$HANS/api/game/leftover" \
                             --data-urlencode "desc=po hře visí: ${names}(herní mód jsem vypnul; za $((KILL_AFTER_S / 60)) minuty je ukončím, pokud znovu neožijí)" >/dev/null 2>&1
                        kill_at=$((now + KILL_AFTER_S))   # HANS_GAME_STUCK_KILL_V1
                    fi
                elif [ "$state" = stuck ] && [ "$d" -ge $((STUCK_CPU_S * TCK * 5)) ]; then
                    # zbytky ožily nebo se spustila další hra ve stejném prostředí
                    if pause_brain; then
                        state=playing; kill_at=0   # HANS_GAME_STUCK_KILL_V1 — ožily → neukončovat
                        log "zbytky po hře zase pracují (CPU ${d}/${TCK} s) → herní mód ZAP"
                    fi
                fi
            fi
            stuck_mark_t=$now; stuck_mark_cpu=$cpu
        fi
        if [ "$state" = idle ]; then
            stuck_mark_t=0
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
    elif [ "$state" = stuck ]; then
        # HANS_GAME_STUCK_V1 — zbytky konečně zmizely (mozek už je vrácený)
        state=idle; stuck_mark_t=0; kill_at=0
        log "zaseklé zbytky po hře zmizely"
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
