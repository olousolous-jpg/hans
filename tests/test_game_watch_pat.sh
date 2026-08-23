#!/usr/bin/env bash
# test_game_watch_pat.sh — HANS_GAME_PAT_NOT_VPN_V1
# Trvaly test signatur herniho watcheru (`deploy/pc/hans-game-watch.sh`).
# Vznikl z realneho naleze 20.-23.8.: `[Pp]roton` v GAME_PAT matchoval ProtonVPN
# daemon -> watcher byl od bootu trvale ve stavu "hraje se" a skutecna hra uz
# zadny prechod nevyvolala. Mesic to nikdo nepoznal, proto to tady zustava.
# Spusteni:  bash tests/test_game_watch_pat.sh   (nepotrebuje PC ani sit)
# Fixture test matchovaci logiky watcheru. Vzory se BEROU ZE SKRIPTU (ne z hlavy),
# aby test nemohl zezelenat proti jinemu textu, nez je nasazeny.
set -u
S="$(cd "$(dirname "$0")/.." && pwd)/deploy/pc/hans-game-watch.sh"
eval "$(grep -E "^GAME_PAT=|^NOTGAME_PAT=" "$S")"

match() {  # stdin = blob "ps -eo args"; vraci 0 kdyz to watcher povazuje za hru
    local m
    m=$(grep -vE '^\[' | grep -viE "$NOTGAME_PAT" | grep -E "$GAME_PAT" | head -1 | cut -c1-120)
    [ -n "$m" ] && { printf '%s' "$m"; return 0; }
    return 1
}

ok=0; bad=0
t() {  # t <ocekavano: hra|nic> <popis> <radek>
    local exp="$1" desc="$2" line="$3" got
    if got=$(printf '%s\n' "$line" | match); then got_r=hra; else got_r=nic; fi
    if [ "$exp" = "$got_r" ]; then ok=$((ok+1)); printf '  ✓ %-46s → %s\n' "$desc" "$got_r"
    else bad=$((bad+1)); printf '  ✗ %-46s → %s (čekáno %s)\n' "$desc" "$got_r" "$exp"; fi
}

echo "── NESMÍ platit za hru ──"
t nic "ProtonVPN daemon (skutečný viník)" '/usr/bin/python3 -m proton.vpn.daemon'
t nic "ProtonVPN GUI"                     '/usr/bin/protonvpn-app'
t nic "ProtonMail bridge"                 '/usr/bin/protonmail-bridge --noninteractive'
t nic "rclone záloha na Proton Drive"     'rclone copy /home/olda/zaloha protondrive:hans'
t nic "kernel thread"                     '[oom_reaper]'
t nic "běžný desktop proces"              '/usr/lib/firefox/firefox'
t nic "idle Steam (bez hry)"              '/home/olda/.local/share/Steam/ubuntu12_32/steamwebhelper'

echo "── MUSÍ platit za hru (regrese detekce) ──"
t hra "Heroic + GE-Proton"                '/home/olda/.config/heroic/tools/proton/GE-Proton9-20/proton waitforexitandrun /games/007/game.exe'
t hra "Steam launch reaper"               '/home/olda/.local/share/Steam/ubuntu12_32/reaper SteamLaunch AppId=1174180 -- /usr/bin/pv-bwrap'
t hra "AppId v cmdline"                   'steamwebhelper AppId=730'
t hra "wineserver"                        'C:\\windows\\system32\\wineserver.exe'
t hra "pressure-vessel / pv-bwrap"        '/usr/share/steam/pressure-vessel/bin/pv-bwrap --args 42'
t hra "gamescope"                         'gamescope -W 2560 -H 1440 -- %command%'
t hra "wine-preloader"                    '/usr/bin/wine-preloader z:\\games\\hra.exe'

echo "── směs: VPN + hra naráz (VPN nesmí zastínit ani vyrobit hru) ──"
mix=$'/usr/bin/python3 -m proton.vpn.daemon\n/usr/lib/firefox/firefox\n/home/olda/.config/heroic/tools/proton/GE-Proton9-20/proton run hra.exe'
if got=$(printf '%s\n' "$mix" | match); then
    case "$got" in *GE-Proton*) ok=$((ok+1)); echo "  ✓ vybrán herní proces, ne VPN → ${got:0:60}…";;
                   *) bad=$((bad+1)); echo "  ✗ vybráno špatně: $got";; esac
else bad=$((bad+1)); echo "  ✗ hru nenašel vůbec"; fi

echo; echo "OK=$ok  CHYB=$bad"; [ "$bad" -eq 0 ]
