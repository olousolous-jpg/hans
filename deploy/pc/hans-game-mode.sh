#!/usr/bin/env bash
# hans-game-mode.sh — OLLAMA_GAME_MODE_V1
# Před hrou řekne Hansovi, ať uvolní modely z VRAM a přestane používat Ollamu
# (volná grafika pro hru). Po skončení hry mozek zase zapne.
#
# INSTALACE (na PC s Ollamou, Linux):
#   1) zkopíruj sem, např. /usr/local/bin/hra
#        sudo cp hans-game-mode.sh /usr/local/bin/hra && sudo chmod +x /usr/local/bin/hra
#   2) (volitelně) uprav HANS níže, pokud má Raspberry jinou IP
#
# POUŽITÍ:
#   hra <příkaz hry> [argumenty...]
#     hra steam
#     hra /cesta/ke/hre
#   Steam (per-game): Properties → Launch Options:
#     /usr/local/bin/hra %command%
#
# Funguje i ručně bez hry (uprav IP na svůj Raspberry):
#   curl -X POST http://192.168.1.50:7860/api/brain/pause     # uvolni
#   curl -X POST http://192.168.1.50:7860/api/brain/resume    # vrať mozek

HANS="${HANS:-http://192.168.1.50:7860}"   # web_admin na Raspberry (uprav na svou IP nebo přes env HANS=)

_pause() { curl -s -m 8 -X POST "$HANS/api/brain/pause"  >/dev/null 2>&1 && echo "[hra] Hans uvolnil grafiku."; }
_resume(){ curl -s -m 8 -X POST "$HANS/api/brain/resume" >/dev/null 2>&1 && echo "[hra] Hans má zpět mozek."; }

# vrať mozek i kdyby hra spadla / Ctrl+C
trap _resume EXIT INT TERM

_pause
sleep 2            # nech doběhnout případné rozdělané volání + uvolnit VRAM

if [ "$#" -gt 0 ]; then
    "$@"           # spusť hru a počkej na ni
else
    echo "[hra] Bez argumentu — grafika uvolněna. Stiskni Enter pro vrácení mozku."
    read -r _
fi
# _resume zavolá trap EXIT
