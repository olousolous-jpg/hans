"""
Shared logger — writes INFO+ to data/system.log (rotating, max 2MB × 3 files).
Terminal gets WARNING+ only so the console stays clean.

Usage:
    from scripts.logger import get_logger
    log = get_logger(__name__)
    log.info("Face enrolled: %s", name)
    log.warning("Low quality embedding rejected")
"""

import logging
import logging.handlers
from pathlib import Path

_LOG_PATH    = Path("data/system.log")
_MAX_BYTES   = 2 * 1024 * 1024   # 2 MB per file
_BACKUP_COUNT = 3
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
