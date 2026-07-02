#!/usr/bin/env bash
# hans-heroic-wrapper.sh — Heroic „Wrapper command"
# Uvolní VRAM (Hansův herní mód ZAP) PŘED spuštěním hry a vrátí Hansovi mozek
# (herní mód VYP) PO jejím skončení. Trap zajistí návrat i při pádu / Ctrl-C.
#
# NASTAVENÍ v Heroicu (per-hra NEBO globálně):
#   Nastavení → "Advanced" → "Wrapper command":
#     /home/<user>/hans-heroic-wrapper.sh
#   (Heroic pak spustí:  hans-heroic-wrapper.sh <příkaz hry...>)
#
# HANS = adresa web_adminu na Raspberry (uprav na svou IP).
HANS="${HANS:-http://192.168.1.50:7860}"

_resume() { curl -s -m 8 -X POST "$HANS/api/brain/resume" >/dev/null 2>&1; }
trap _resume EXIT INT TERM

# Herní mód ZAP — endpoint interně počká, až je VRAM reálně volná (rocm-smi settle)
resp=$(curl -s -m 40 -X POST "$HANS/api/brain/pause" 2>/dev/null)
logger -t hans-heroic "pause: $resp" 2>/dev/null || true

# Spusť hru (Heroic předá celý příkaz jako argumenty) a počkej na ni.
# _resume pak zavolá trap EXIT.
"$@"
