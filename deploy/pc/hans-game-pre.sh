#!/usr/bin/env bash
# Heroic → "Select a script to run BEFORE the game is launched"
# 1) Uvolní VRAM (Hansův herní mód ZAP), počká až je grafika volná.
# 2) Nahlásí Hansovi, JAKÁ hra se spustila → přiřadí ji poslednímu člověku u PC.
HANS="${HANS:-http://192.168.1.50:7860}"

resp=$(curl -s -m 40 -X POST "$HANS/api/brain/pause" 2>/dev/null)
logger -t hans-heroic "pre pause: $resp" 2>/dev/null || true

# Název hry z Heroic env (zkus víc kandidátů) + zaloguj env pro objevení
TITLE="${HEROIC_APP_TITLE:-${GAME_TITLE:-${HEROIC_APP_NAME:-}}}"
env | grep -iE 'heroic|game|title|app_|gameid' > /tmp/hans-heroic-env.log 2>/dev/null
if [ -n "$TITLE" ]; then
    curl -s -m 6 --get "$HANS/api/game/launched" --data-urlencode "title=$TITLE" >/dev/null 2>&1
fi
exit 0
