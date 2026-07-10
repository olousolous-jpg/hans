#!/usr/bin/env bash
# ============================================================
#  backup_hans.sh — konzistentní záloha Hansovy paměti (Pi)
#
#  Zálohuje NENAHRADITELNÁ data: deník (primární paměť), znalost,
#  enrollnuté tváře, config (secrets+jména), konverzace, stav.
#  SQLite DB se zálohují přes `.backup` (bezpečné na ŽIVÉ DB — Hans
#  do nich zapisuje; prosté cp by dalo poškozenou kopii).
#
#  Použití:
#    tools/backup_hans.sh            # kritická data (malé, ~15 MB)
#    tools/backup_hans.sh --full     # + objemné (kodi, art, avatar)
#
#  Výstup: data/backups/hans_backup_<full|core>_<datum>.tar.gz
#  Rotace: nechá posledních KEEP archivů daného druhu.
#
#  NAS/Proton push: nastav proměnné níže nebo přes prostředí
#  (NAS_DEST, RCLONE_REMOTE). Prázdné = jen lokální záloha.
# ============================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

KEEP="${KEEP:-14}"                 # kolik archivů nechat
NAS_DEST="${NAS_DEST:-}"           # např. /mnt/nas/hans nebo user@nas:/path (rsync)
RCLONE_REMOTE="${RCLONE_REMOTE:-}" # např. proton:Hans/backups
GPG_PASSFILE="${GPG_PASSFILE:-}"   # soubor s heslem → šifrovat archiv (pro cloud)

FULL=0
[ "${1:-}" = "--full" ] && FULL=1
KIND=$([ "$FULL" = 1 ] && echo full || echo core)

STAMP="$(date +%Y%m%d_%H%M%S)"
OUT_DIR="data/backups"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
mkdir -p "$OUT_DIR" "$STAGE/data"

echo "== Hans backup ($KIND) $STAMP =="

# --- 1) SQLite DB konzistentně přes .backup ---
CORE_DBS=(hans_diary.db hans_knowledge.db surroundings.db)
FULL_DBS=(kodi_monitor.db unknown_tracker.db)
DBS=("${CORE_DBS[@]}")
[ "$FULL" = 1 ] && DBS+=("${FULL_DBS[@]}")

for db in "${DBS[@]}"; do
    src="data/$db"
    [ -f "$src" ] || { echo "  (přeskakuji chybějící $db)"; continue; }
    if sqlite3 "$src" ".backup '$STAGE/data/$db'" 2>/dev/null; then
        ic=$(sqlite3 "$STAGE/data/$db" "PRAGMA integrity_check;" 2>/dev/null | head -1)
        echo "  DB $db: $ic ($(du -h "$STAGE/data/$db" | cut -f1))"
    else
        echo "  !!! $db: .backup SELHAL — přeskakuji"
    fi
done

# --- 2) Soubory (tváře, config, konverzace, stav) ---
CORE_FILES=(config.json data/known_faces.pkl data/known_faces_cluster.pkl
            data/known_faces_personface.pkl data/gesture_model.pkl
            data/routine_state.json data/hans_known_capabilities.json
            eye_calibration.json)
for f in "${CORE_FILES[@]}"; do
    [ -f "$f" ] && { mkdir -p "$STAGE/$(dirname "$f")"; cp -p "$f" "$STAGE/$f"; }
done
[ -d data/conversations ] && cp -a data/conversations "$STAGE/data/"

if [ "$FULL" = 1 ]; then
    [ -d data/hans_art ] && cp -a data/hans_art "$STAGE/data/"
    [ -d data/avatar ]   && cp -a data/avatar   "$STAGE/data/"
fi

# --- 2b) Kód (scripts/tools/templates/deploy + root textové soubory) ---
# Zachytí i NECOMMITNUTÉ změny + CLAUDE.md (handoff, gitignored). Balast ven
# (venv/.git na GitHubu, archive/pycache/data regenerovatelné/jinde).
for d in scripts tools templates deploy; do
    [ -d "$d" ] && rsync -a --exclude='__pycache__' --exclude='*.pyc' \
        "$d" "$STAGE/" 2>/dev/null
done
find . -maxdepth 1 -type f \( -name '*.py' -o -name '*.sh' -o -name '*.json' \
    -o -name '*.md' -o -name '*.txt' -o -name '*.service' \) \
    -exec cp -p {} "$STAGE/" \; 2>/dev/null
code_n=$(find "$STAGE/scripts" "$STAGE/tools" -type f 2>/dev/null | wc -l)
echo "  kód: $code_n souborů (scripts+tools) + templates/deploy/root"

# --- 3) Archiv ---
ARCHIVE="$OUT_DIR/hans_backup_${KIND}_${STAMP}.tar.gz"
tar -czf "$ARCHIVE" -C "$STAGE" .
echo "== archiv: $ARCHIVE ($(du -h "$ARCHIVE" | cut -f1)) =="

# --- 3b) Volitelné šifrování pro cloud ---
UPLOAD="$ARCHIVE"
if [ -n "$GPG_PASSFILE" ] && [ -f "$GPG_PASSFILE" ]; then
    gpg --batch --yes --passphrase-file "$GPG_PASSFILE" \
        -c --cipher-algo AES256 -o "$ARCHIVE.gpg" "$ARCHIVE"
    UPLOAD="$ARCHIVE.gpg"
    echo "== šifrováno: $UPLOAD =="
fi

# --- 4) NAS push (rsync — mount i user@host:cesta) ---
if [ -n "$NAS_DEST" ]; then
    if rsync -a "$UPLOAD" "$NAS_DEST/" 2>&1; then
        echo "== NAS OK: $NAS_DEST =="
    else
        echo "!!! NAS push selhal ($NAS_DEST)"
    fi
fi

# --- 5) Proton (rclone) push ---
if [ -n "$RCLONE_REMOTE" ] && command -v rclone >/dev/null; then
    if rclone copy "$UPLOAD" "$RCLONE_REMOTE/" 2>&1; then
        echo "== Proton OK: $RCLONE_REMOTE =="
    else
        echo "!!! Proton push selhal ($RCLONE_REMOTE)"
    fi
fi

# --- 6) Rotace (lokálně, per druh) ---
ls -1t "$OUT_DIR"/hans_backup_${KIND}_*.tar.gz* 2>/dev/null | tail -n +$((KEEP+1)) | \
    while read -r old; do rm -f "$old" && echo "  rotace: smazán $(basename "$old")"; done

echo "== HOTOVO =="
