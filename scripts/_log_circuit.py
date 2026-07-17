"""LOG_CIRCUIT_V1 — potlačí spam ze zjevně mrtvého HTTP endpointu.

Vzor: PC v noci vypnutý → Ollama warmup/RAG query kolo → stovky identických
`No route to host` řádků/hod. To zaneřáďuje log a schovává reálné chyby.

Řešení: per-endpoint state „down since". První connection-related pád zaloguj
NORMÁLNĚ, další tiše počítej. První úspěch → INFO „obnoveno po Xs, potlačeno M".

POUŽITÍ (příklad):
    from scripts._log_circuit import for_url, is_conn_error
    br = for_url(url)
    try:
        r = requests.post(url, ...)
        r.raise_for_status()
        br.note_success(_log)
        ...
    except Exception as exc:
        if is_conn_error(exc):
            if br.should_log(exc):
                _log.error("Ollama connection error: %s — %s", url, exc)
        else:
            _log.error("Ollama request error: %s — %s", url, exc)

Non-connection chyby breaker IGNORUJE (logují se dál). Thread-safe.
"""
from __future__ import annotations

import threading
import time
from typing import Optional
from urllib.parse import urlparse

import requests as _rq

_lock = threading.RLock()
_breakers: dict[str, "_Breaker"] = {}


def is_conn_error(exc: BaseException) -> bool:
    """True pro chyby zjevně z nedostupné sítě (nemá smysl spamovat).
    Timeout NEbereme jako conn-error — jinak by se schovaly reálné pomalé cesty."""
    if isinstance(exc, _rq.exceptions.ConnectionError):
        return True
    # requests někdy zabalí OSError přímo — chyť „No route to host" apod.
    msg = str(exc).lower()
    return any(x in msg for x in (
        "no route to host",
        "connection refused",
        "network is unreachable",
        "name or service not known",
        "temporary failure in name resolution",
        "failed to establish a new connection",
    ))


def _endpoint_key(url: str) -> str:
    """Klíč = host:port (různé cesty na téže Ollama sdílí jeden breaker)."""
    try:
        u = urlparse(url)
        host = u.hostname or url
        port = u.port or (443 if u.scheme == "https" else 80)
        return f"{host}:{port}"
    except Exception:
        return url


def for_url(url: str) -> "_Breaker":
    """Vrátí (a případně vytvoří) breaker pro daný endpoint."""
    key = _endpoint_key(url)
    with _lock:
        br = _breakers.get(key)
        if br is None:
            br = _Breaker(key)
            _breakers[key] = br
        return br


class _Breaker:
    __slots__ = ("key", "_down_since", "_suppressed", "_last_exc_msg", "_lk")

    def __init__(self, key: str):
        self.key = key
        self._down_since: Optional[float] = None
        self._suppressed: int = 0
        self._last_exc_msg: str = ""
        self._lk = threading.Lock()

    def should_log(self, exc: BaseException) -> bool:
        """Zavolej při conn-error. True = logni normálně, False = potlač.
        Ne-conn chyby si ošetři sám (logni vždy)."""
        with self._lk:
            if self._down_since is None:
                self._down_since = time.time()
                self._suppressed = 0
                self._last_exc_msg = str(exc)[:200]
                return True
            self._suppressed += 1
            return False

    def note_success(self, log) -> None:
        """Zavolej po každém úspěšném requestu. Pokud byl endpoint down,
        zaloguj obnovu (INFO) a resetuj stav."""
        with self._lk:
            if self._down_since is None:
                return
            down_s = time.time() - self._down_since
            supp = self._suppressed
            self._down_since = None
            self._suppressed = 0
            last = self._last_exc_msg
            self._last_exc_msg = ""
        # log MIMO zámek (může blokovat)
        try:
            log.info("endpoint %s OBNOVEN po %.0fs (potlačeno %d dalších chyb; "
                     "první: %s)", self.key, down_s, supp, last)
        except Exception:
            pass

    def snapshot(self) -> dict:
        """Diagnostika: {down_since, down_s, suppressed}. down_since=None = OK."""
        with self._lk:
            if self._down_since is None:
                return {"down": False, "suppressed": 0}
            return {
                "down": True,
                "down_since": self._down_since,
                "down_s": time.time() - self._down_since,
                "suppressed": self._suppressed,
                "last_exc": self._last_exc_msg,
            }


def snapshot_all() -> dict:
    """{endpoint: snapshot()} pro všechny breakery — dashboard/diagnostika."""
    with _lock:
        return {k: br.snapshot() for k, br in _breakers.items()}
