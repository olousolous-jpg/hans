# Kodi box (OSMC) — Hansův omezený přístup

`hans-log-cist` patří do `/home/osmc/hans-log-cist` (chmod 755). Hansův klíč z Pi
(`~/.ssh/hans_kodi_svc.pub`) se do `/home/osmc/.ssh/authorized_keys` přidá jako:

    command="/home/osmc/hans-log-cist",restrict ssh-ed25519 AAAA… hans-kodi-log-ro

Klíč pak smí jen tohle: číst chyby z `kodi.log` (sběr pro prevenci, `scripts/hans_prevence.py`)
a vydat zálohu pevného seznamu souborů (`tools/backup_hans.sh`, krok 2d). Shell nedostane.
