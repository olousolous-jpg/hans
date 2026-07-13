#!/usr/bin/env bash
# CAMERA_DIAG_V1 — jeden pohled na zdraví kamery/CSI linky.
#
# Hypotéza (12.7.): vadná CSI linka. v3 jede na 900 Mbps → padá;
# v2 na 437 Mbps → jen pruhy. Když snížením toku (fps/rozlišení) v3 zestabilní,
# je viník KABEL, ne kamera.
#
# Použití:  ./tools/camera_diag.sh
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "══ KAMERA ═══════════════════════════════════════════"
python3 - <<'EOF'
import json, pathlib
try:
    c = json.loads(pathlib.Path("config.json").read_text())
    cam = c.get("camera", {})
    print(f"  model      : {c.get('camera_model')}")
    print(f"  rozlišení  : {cam.get('main_width')}×{cam.get('main_height')}"
          f"  (lores {cam.get('lores_width')}×{cam.get('lores_height')})")
    print(f"  framerate  : {cam.get('framerate')}")
except Exception as e:
    print("  config nečitelný:", e)
EOF

echo
echo "══ CSI LINKA (klíč hypotézy) ════════════════════════"
rate="$(sudo dmesg 2>/dev/null | grep -a 'link rate' | tail -1)"
echo "  ${rate:-  (žádný záznam)}"
echo "  → v3 zdravá ≈ 900 Mbps · v2 ≈ 437 Mbps"
echo "  → padá-li jen při vysoké rychlosti = VADNÝ KABEL, ne senzor"

echo
echo "══ CHYBY LINKY / SENZORU ════════════════════════════"
sudo dmesg -T 2>/dev/null \
  | grep -aiE "rp1-cfe|csi|imx708|imx219|pisp|overflow|fifo|corrupt|Input/output|Dequeue" \
  | grep -avi "Modules linked in" | tail -8 \
  | sed 's/^/  /' || true
[ -z "$(sudo dmesg 2>/dev/null | grep -aiE 'Input/output|Dequeue|overflow')" ] \
  && echo "  ✅ žádné I/O ani přetečení bufferu"

echo
echo "══ ZRAK HANSE (živě) ════════════════════════════════"
hb="$ROOT/data/.hans_heartbeat"
if [ -f "$hb" ]; then
    age=$(( $(date +%s) - $(stat -c %Y "$hb") ))
    echo "  tep: před ${age}s $([ "$age" -lt 30 ] && echo '(žije)' || echo '(⚠ zatuhlý?)')"
    echo "  stav: $(cat "$hb")"
else
    echo "  (Hans neběží / starý build)"
fi

echo
echo "══ VÝPADKY A OBNOVY (Hansův log) ════════════════════"
grep -a "camera:" "$ROOT/data/system.log" 2>/dev/null | tail -6 | sed 's/^/  /' \
  || echo "  (zatím žádné)"
echo
