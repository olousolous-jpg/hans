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
            eye_calibration.json
            data/BACKLOG.md)   # BACKLOG_IN_BACKUP_V1 (22.8.) — aktivní handoff
            # a pracovní seznam. Je gitignorovaný jako CLAUDE.md, ale ten se
            # bral přes kódový sweep níž (root *.md), zatímco tenhle leží
            # v data/ → propadal oběma síty a nebyl NIKDE.
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

# --- 2c) systemd user jednotky (BACKUP_SYSTEMD_UNITS_V1, 9. 9.) ---
# Bez nich je obnova NEUPLNA: zive bezi 9 jednotek (hans.service,
# hans-backup.*, hans-watchdog.*, tunely), ale v repu jsou jen dve —
# a `hans.service` se od zive verze LISI. `hans-backup.timer` neni nikde,
# takze po preinstalaci by zmizela i sama zaloha a nikdo by si toho nevsiml.
# Tataz trida ticheho selhani jako 45denni vypadek offsite pushe.
# ⚠️ Do ARCHIVU ano, do GITU ne — mohou nest IP a cesty.
UNITS="$HOME/.config/systemd/user"
if [ -d "$UNITS" ]; then
    mkdir -p "$STAGE/systemd_user"
    cp -p "$UNITS"/*.service "$UNITS"/*.timer "$STAGE/systemd_user/" 2>/dev/null || true
    # a prostredi zalohy (NAS_DEST, WOL MAC) — bez nej se nastaveni obnovuje slepe
    [ -f "$HOME/.config/hans-backup.env" ] && \
        cp -p "$HOME/.config/hans-backup.env" "$STAGE/systemd_user/" 2>/dev/null || true
    echo "  systemd: $(ls -1 "$STAGE/systemd_user" 2>/dev/null | wc -l) souborů"
fi

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
# BACKUP_OFFSITE_LOUD_V1 (9. 9.) — selhani offsite pushe uz NESMI byt tiche.
# Doloženo: `/etc/fstab` mel od vymeny desky v NASu starou IP, rsync 45 DNI
# hlasil „No such device", skript presto koncil USPECHEM a
# `systemctl status hans-backup` byl zeleny. Z pravidla 3-2-1 tak zbyla
# JEDNA kopie na Pi a nikdo se to nedozvedel.
OFFSITE_FAIL=0
if [ -n "$NAS_DEST" ]; then
    # BACKUP_NAS_WAKE_V1 (9. 9.) — NAS SE USPAVA a disky se roztacej az na
    # vyzadani (upresneni uzivatele). Zaloha bezi v 03:33, tedy do spiciho
    # stroje: jeden pokus by selhal a po BACKUP_OFFSITE_LOUD_V1 by sluzba
    # padala do `failed` KAZDOU NOC — hlaseni by se stalo sumem a prestalo
    # by se cist, coz je presne ta vada, kterou mel loud rezim odstranit.
    # Proto: (1) magic packet, kdyz je znamy MAC, (2) nekolik pokusu
    # s prodlevou, at maji plotny cas se roztocit. Az kdyz selze i posledni,
    # je to skutecne selhani.
    # ⚠️ WOL sdili `pc_remote.wake` (HANS_WOL_SHARED_V1) — zadna druha
    # implementace magic packetu.
    if [ -n "${NAS_WOL_MAC:-}" ]; then
        python3 -c "import sys; sys.path.insert(0,'.')
from scripts.pc_remote import wake
print('  WOL na NAS: %s' % ('odeslano' if wake(mac='${NAS_WOL_MAC}') else 'SELHALO'))" 2>/dev/null \
            || echo "  (WOL nedostupny)"
    fi
    NAS_TRIES="${NAS_TRIES:-6}"
    NAS_WAIT="${NAS_WAIT:-20}"
    OFFSITE_FAIL=1
    for i in $(seq 1 "$NAS_TRIES"); do
        if rsync -a --timeout=180 "$UPLOAD" "$NAS_DEST/" 2>&1; then
            echo "== NAS OK: $NAS_DEST (pokus $i/$NAS_TRIES) =="
            OFFSITE_FAIL=0
            break
        fi
        if [ "$i" -lt "$NAS_TRIES" ]; then
            echo "  NAS zatim neodpovida (pokus $i/$NAS_TRIES) — cekam ${NAS_WAIT}s (spici disky?)"
            sleep "$NAS_WAIT"
        fi
    done
    [ "$OFFSITE_FAIL" = 1 ] && echo "!!! NAS push selhal po $NAS_TRIES pokusech ($NAS_DEST)"
    # BACKUP_NAS_RELEASE_V1 (9. 9.) — po zaloze PUSTIT NAS, at muze zase
    # usnout (prani uzivatele: uspat, NE vypnout).
    # ⚠️ Uspat ho PRIKAZEM NELZE: na 192.168.88.x ma otevreny JEN port 445
    # (SMB) — zadne SSH, RDP ani WinRM, tedy zadny kanal pro vzdalene
    # spusteni. Jedine, co je z Pi v moci, je UVOLNIT RELACI, aby ji NAS
    # nepocital jako aktivitu a rozbehl si vlastni idle casovac.
    # Automount (`x-systemd.idle-timeout=60`) by odpojil sam az za minutu
    # necinnosti; tohle to udela HNED po dokoncene zaloze.
    # 📌 Kdyby se ukazalo, ze NAS presto neusina, chce to na NEM povolit
    # kanal (SSH/WinRM) nebo naplanovanou ulohu — z Pi uz to nejde.
    case "$NAS_DEST" in
        /*) if mountpoint -q "$(dirname "$NAS_DEST")" 2>/dev/null; then
                if sudo -n umount "$(dirname "$NAS_DEST")" 2>/dev/null; then
                    echo "  NAS uvolnen (odpojeno) — muze zase usnout"
                else
                    echo "  (NAS se nepodarilo odpojit; automount ho puste sam do 60 s)"
                fi
            fi ;;
    esac
fi

# --- 5) Proton (rclone) push ---
if [ -n "$RCLONE_REMOTE" ] && command -v rclone >/dev/null; then
    if rclone copy "$UPLOAD" "$RCLONE_REMOTE/" 2>&1; then
        echo "== Proton OK: $RCLONE_REMOTE =="
    else
        echo "!!! Proton push selhal ($RCLONE_REMOTE)"
        OFFSITE_FAIL=1
    fi
fi

# --- 6) Rotace (lokálně, per druh) ---
ls -1t "$OUT_DIR"/hans_backup_${KIND}_*.tar.gz* 2>/dev/null | tail -n +$((KEEP+1)) | \
    while read -r old; do rm -f "$old" && echo "  rotace: smazán $(basename "$old")"; done

if [ "${OFFSITE_FAIL:-0}" = 1 ]; then
    # Lokalni archiv JE hotovy (rotace probehla vyse) — nenulovy konec hlasi
    # jen to, ze OFFSITE kopie chybi. `hans-backup.service` tim spadne do
    # `failed`, takze se to pozna z `systemctl --user status hans-backup`.
    echo "== HOTOVO (lokalne), ale OFFSITE KOPIE CHYBI =="
    exit 2
fi
echo "== HOTOVO =="
