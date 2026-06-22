"""
Structured NDJSON debug logger.  # DEBUG_LOG_CLEANED_V1

Writes one JSON object per line to data/debug.log.
Rotates at 5 MB, keeps 3 backups.
Use for structured runtime events that you want to grep/jq later
(different from scripts/logger.py, which is the production text logger
used via Python's logging module).

Usage:
    from scripts.debug_log import dbg
    dbg("module.func", "Event description", data={"key": value})

Never log secrets (tokens, passwords, API keys) or sensitive personal data.
"""

from __future__ import annotations

import json
import time
import threading
from pathlib import Path


_LOG_PATH = Path("data/debug.log")
_MAX_BYTES = 5 * 1024 * 1024   # 5 MB
_BACKUP_COUNT = 3
_lock = threading.Lock()


def _rotate_if_needed() -> None:
    """Pokud log překročil MAX_BYTES, posune se .1 → .2 → .3 a začne čistý."""
    try:
        if not _LOG_PATH.exists():
            return
        if _LOG_PATH.stat().st_size < _MAX_BYTES:
            return
        # posun backupů odzadu
        for i in range(_BACKUP_COUNT, 0, -1):
            src = _LOG_PATH.with_suffix(f".log.{i}")
            if i == _BACKUP_COUNT and src.exists():
                src.unlink()
                continue
            dst = _LOG_PATH.with_suffix(f".log.{i + 1}")
            if src.exists():
                src.rename(dst)
        # aktivní log → .1
        _LOG_PATH.rename(_LOG_PATH.with_suffix(".log.1"))
    except Exception:
        pass  # rotation failure must never crash app


def dbg(location: str, message: str, data: dict | None = None) -> None:
    """Append one NDJSON event to data/debug.log.

    Args:
        location: where the event happens, e.g. "module.py:function"
        message:  short human-readable description
        data:     optional dict of structured fields
    """
    try:
        payload = {
            "ts_ms": int(time.time() * 1000),
            "location": location,
            "message": message,
            "data": data or {},
        }
        with _lock:
            _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            _rotate_if_needed()
            with open(_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception:
        # Never crash the app because of debug logging.
        pass
