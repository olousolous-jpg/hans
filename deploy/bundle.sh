#!/bin/bash
# bundle.sh — zabalí kompletního Hanse pro přenos na jiné Raspberry Pi 5 + AI Kit.
# Zahrnuje: kód, config, kritická data (paměť/identita/faces), HEF modely,
# requirements, systemd službu. Vylučuje: venv (relikt), logy, cache, archiv,
# snapshoty, dočasné DB. Výstup: hans_bundle_<datum>.tar.gz v ~/.
set -euo pipefail
cd "$(dirname "$0")/.."          # → kořen projektu
ROOT="$(pwd)"
STAMP="$(date +%Y%m%d_%H%M%S)"
OUT="$HOME/hans_bundle_${STAMP}.tar.gz"

echo "[bundle] kořen: $ROOT"

# systemd služba: bereme verzi v repu (deploy/_systemd/hans.service s %h —
# přenositelný default; install.sh ji na cíli přepíše na skutečný $ROOT).
# ŽIVOU ~/.config/systemd/... ZÁMĚRNĚ nekopírujeme — má natvrdo absolutní domácí cestu.
[ -f deploy/_systemd/hans.service ] \
  && echo "[bundle] přibalím deploy/_systemd/hans.service (%h verze z repa)" \
  || echo "[bundle] ⚠ deploy/_systemd/hans.service chybí — balík bude bez služby"

tar -czf "$OUT" \
  --exclude='venv' \
  --exclude='.git' \
  --exclude='archive' \
  --exclude='__pycache__' \
  --exclude='*.pyc' \
  --exclude='data/recognition.log*' \
  --exclude='data/debug.log*' \
  --exclude='data/system.log*' \
  --exclude='data/tts_cache' \
  --exclude='data/patch_snapshots' \
  --exclude='data/backups' \
  --exclude='data/unknown_tracker.db' \
  --exclude='data/unknown_faces' \
  --exclude='data/hans_video' \
  --exclude='data/gesture_landmarks.jsonl' \
  --exclude='*.bak' \
  --exclude='*.bak_*' \
  --exclude='data/*.bak' \
  --exclude='*.tmp' \
  -C "$ROOT" \
  main.py web_admin.py run.sh config.json \
  scripts templates resources \
  data \
  deploy/requirements.txt deploy/install.sh deploy/README.md deploy/SETUP_PC.md deploy/_systemd \
  2>/dev/null

SIZE="$(du -h "$OUT" | cut -f1)"
echo "[bundle] HOTOVO → $OUT  ($SIZE)"
echo "[bundle] Přenes na cílové Pi a tam: tar -xzf $(basename "$OUT") && cd <složka> && bash deploy/install.sh"
