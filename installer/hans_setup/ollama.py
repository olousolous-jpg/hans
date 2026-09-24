"""Minimální klient Ollama API (jen urllib — funguje i bez venv)."""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Callable, Iterable, Optional


class OllamaError(RuntimeError):
    pass


class Ollama:
    def __init__(self, base_url: str, timeout: float = 600.0):
        self.base = base_url.rstrip("/")
        self.timeout = timeout

    # ── nízká úroveň ──────────────────────────────────────────────────────
    def _req(self, method: str, path: str, body: Optional[dict] = None,
             timeout: Optional[float] = None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            self.base + path, data=data, method=method,
            headers={"Content-Type": "application/json"})
        try:
            return urllib.request.urlopen(req, timeout=timeout or self.timeout)
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            raise OllamaError("%s %s → HTTP %s: %s" % (method, path, e.code, detail))
        except (urllib.error.URLError, OSError) as e:
            raise OllamaError("%s nedostupná: %s" % (self.base, e))

    def _json(self, method: str, path: str, body: Optional[dict] = None,
              timeout: Optional[float] = None) -> dict:
        with self._req(method, path, body, timeout) as r:
            return json.loads(r.read().decode("utf-8") or "{}")

    def _stream(self, path: str, body: dict) -> Iterable[dict]:
        with self._req("POST", path, body) as r:
            for line in r:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except ValueError:
                    continue

    # ── API ───────────────────────────────────────────────────────────────
    def version(self, timeout: float = 4.0) -> Optional[str]:
        try:
            return self._json("GET", "/api/version", timeout=timeout).get("version")
        except OllamaError:
            return None

    def models(self) -> list[str]:
        return [m.get("name") or m.get("model")
                for m in self._json("GET", "/api/tags", timeout=10).get("models", [])]

    def has(self, model: str, installed: Optional[list[str]] = None) -> bool:
        installed = self.models() if installed is None else installed
        return normalize(model) in {normalize(m) for m in installed}

    def chat(self, model: str, messages: list[dict], fmt=None,
             temperature: float = 0.7, num_ctx: int = 8192) -> str:
        body = {"model": model, "messages": messages, "stream": False,
                "options": {"temperature": temperature, "num_ctx": num_ctx}}
        if fmt is not None:
            body["format"] = fmt
        return self._json("POST", "/api/chat", body).get("message", {}).get("content", "")

    def pull(self, model: str, progress: Optional[Callable[[dict], None]] = None) -> None:
        last = {}
        for ev in self._stream("/api/pull", {"model": model, "name": model, "stream": True}):
            if "error" in ev:
                raise OllamaError("pull %s: %s" % (model, ev["error"]))
            last = ev
            if progress:
                progress(ev)
        if last.get("status") != "success":
            raise OllamaError("pull %s neskončil úspěchem (%s)" % (model, last.get("status")))

    def create_alias(self, name: str, base: str, parameters: Optional[dict] = None) -> None:
        """Vytvoří model `name` jako kopii `base` (bez vlastního SYSTEM promptu —
        identitu dodává Hans v každém dotazu z persona.core, viz hans_persona).
        Nové API (`from`) s pádem na staré (`modelfile`)."""
        body = {"model": name, "from": base, "stream": False}
        if parameters:
            body["parameters"] = parameters
        try:
            self._json("POST", "/api/create", body)
            return
        except OllamaError as e:
            first = e
        mf = "FROM %s\n" % base + "".join(
            "PARAMETER %s %s\n" % (k, v) for k, v in (parameters or {}).items())
        try:
            self._json("POST", "/api/create", {"name": name, "modelfile": mf, "stream": False})
        except OllamaError as e:
            raise OllamaError("create %s: %s / %s" % (name, first, e))


def normalize(model: str) -> str:
    """`bge-m3` a `bge-m3:latest` jsou týž model."""
    model = (model or "").strip()
    return model if ":" in model.split("/")[-1] else model + ":latest"


def pull_progress_printer():
    """Vrátí callback, který vypisuje průběh stahování na jeden řádek."""
    state = {"status": None}

    def cb(ev: dict):
        st = ev.get("status", "")
        total, done = ev.get("total"), ev.get("completed")
        if total and done is not None:
            pct = 100.0 * done / total if total else 0
            print("\r    %s  %5.1f %%  (%.1f / %.1f GB)   " % (
                st[:30], pct, done / 1e9, total / 1e9), end="", flush=True)
        elif st != state["status"]:
            print("\r    %s%s" % (st, " " * 40), flush=True)
        state["status"] = st
        if st == "success":
            print()
    return cb
