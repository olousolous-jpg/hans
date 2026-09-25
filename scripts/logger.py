"""
Shared logger — writes INFO+ to data/system.log (rotating, 2 MB × 60 files).
Rotated files live in data/logs/ (system.log.1 … .60), the active one stays in data/.
Terminal gets WARNING+ only so the console stays clean.

Usage:
    from scripts.logger import get_logger
    log = get_logger(__name__)
    log.info("Face enrolled: %s", name)
    log.warning("Low quality embedding rejected")
"""

import logging
import logging.handlers
import os
from pathlib import Path

# HANS_LOG_SINGLE_WRITER_V1 (15. 9.) — cestu smí přepsat samostatný proces.
# RotatingFileHandler neumí víc zapisovatelů: kdo první přetočí, přejmenuje
# soubor ostatním pod rukama. 14. 9. to ve 21:13 udělal dual_display_daemon
# a celá noc Hanse skončila v system.log.2, zatímco ranní kontrola
# i hans_health_check čtou system.log + .1. Soubor system.log točí JEN Hans;
# jiný proces si nastaví HANS_LOG_FILE PŘED prvním importem scripts.logger.
_LOG_PATH    = Path(os.environ.get("HANS_LOG_FILE") or "data/system.log")
_MAX_BYTES   = 2 * 1024 * 1024   # 2 MB per file
# HANS_LOG_RETENTION_V1 (25. 9.) — 3 → 60 souborů. Logy držely jen ~3 dny, takže
# trend („roste to?“) nešel spočítat vůbec (`hans_prevence`). Tempo ~2,3 MB/den
# → 60 × 2 MB ≈ 120 MB ≈ 50 dní; na Pi místa dost (rozhodnutí uživatele).
# Velikost souboru ZÁMĚRNĚ beze změny: hlídač zdraví čte aktuální + .1 a čekal
# by jinak na delší soubory.
_BACKUP_COUNT = 60
# Rotované soubory do vlastního adresáře (přání uživatele) — aktivní system.log
# zůstává v data/, protože ho čte řada míst (ranní kontrola, rozhovor_api, …).
_ROT_DIR = "logs"


def _rot_namer(name: str) -> str:
    """HANS_LOG_RETENTION_V1 — data/system.log.N → data/logs/system.log.N."""
    p = Path(name)
    d = p.parent / _ROT_DIR
    d.mkdir(parents=True, exist_ok=True)
    return str(d / p.name)
_initialized  = False


def get_logger(name: str = "facerecog") -> logging.Logger:
    global _initialized
    logger = logging.getLogger(name)

    if not _initialized:
        _initialized = True
        Path("data").mkdir(exist_ok=True)

        fmt = logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S"
        )

        # File handler — INFO and above → data/system.log
        fh = logging.handlers.RotatingFileHandler(
            _LOG_PATH, maxBytes=_MAX_BYTES,
            backupCount=_BACKUP_COUNT, encoding="utf-8"
        )
        fh.namer = _rot_namer   # HANS_LOG_RETENTION_V1
        fh.setLevel(logging.INFO)
        fh.setFormatter(fmt)

        # Console handler — WARNING and above only
        ch = logging.StreamHandler()
        ch.setLevel(logging.WARNING)
        ch.setFormatter(fmt)

        root = logging.getLogger()
        root.setLevel(logging.DEBUG)
        root.addHandler(fh)
        root.addHandler(ch)

    return logger


# ── HANS_NO_SILENT_CTX_V1 (20.8.) — hlas JEDNOU za běh ──────────────────────
# Tiché `except: pass` ve skládání promptu schovalo chybu, která se roky dála
# při KAŽDÉ zprávě (volání @property se závorkami). Debug hlášky by nepomohly
# — souborový handler je na INFO, takže by nešly nikam. INFO na každou zprávu
# by zas zaplavilo log (poučení z LOG_CIRCUIT_V1). Kompromis: první výskyt na
# daném místě se zaloguje, další už ne. Na odhalení „tohle selhává pořád"
# stačí jeden řádek; na jeho utopení stačí tisíc.
_once_seen: set = set()


def log_once(logger, key: str, msg: str, *args) -> None:
    """Zaloguje INFO jen při PRVNÍM výskytu daného `key` v tomhle běhu."""
    if key in _once_seen:
        return
    _once_seen.add(key)
    try:
        logger.info(msg, *args)
    except Exception:
        pass
