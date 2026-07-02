#!/usr/bin/env bash
# Heroic → "Select a script to run AFTER the game is closed"
# Vrátí Hansovi mozek (herní mód VYP).
HANS="${HANS:-http://192.168.1.50:7860}"
curl -s -m 8 -X POST "$HANS/api/brain/resume" >/dev/null 2>&1
exit 0
