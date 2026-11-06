"""HANS_SPOTREBA_V1 (6. 11. 2026) — web a synchronizace zásob potravin.

`POST /api/spotreba/sync` — telefon (Android aplikace) pošle změny a dostane změny
ostatních; hlavička `X-Hans-Klic` = `spotreba.token` z config.private.json (web je
v LAN bez hesla, ale zásoby nemá měnit kdokoli v síti).
`GET /api/spotreba` — přehled pro kartu na úvodní straně (jen čtení).
"""
from __future__ import annotations

import hmac
import time

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse

router = APIRouter()
_cfg_cache = {"t": 0.0, "c": {}}


def _config() -> dict:
    if time.time() - _cfg_cache["t"] > 60:
        from scripts import config_io
        _cfg_cache["c"] = config_io.load()
        _cfg_cache["t"] = time.time()
    return _cfg_cache["c"]


@router.post("/api/spotreba/sync")
async def api_spotreba_sync(request: Request, x_hans_klic: str = Header(default="")):
    tok = str(((_config().get("spotreba") or {}).get("token")) or "")
    if not tok or not hmac.compare_digest(tok, x_hans_klic or ""):
        return JSONResponse({"chyba": "neplatný klíč"}, status_code=403)
    try:
        d = await request.json()
    except Exception:
        return JSONResponse({"chyba": "špatný formát"}, status_code=400)
    from scripts import hans_spotreba as hs
    zar = str(d.get("zarizeni") or "")[:64]
    if not zar:
        return JSONResponse({"chyba": "chybí zařízení"}, status_code=400)
    import asyncio
    hs.ladeni(zar, list(d.get("ladeni") or []))
    res = await asyncio.to_thread(hs.sync, zar, list(d.get("polozky") or [])[:2000],
                                  int(d.get("od") or 0), str(d.get("jmeno") or ""))
    return res


@router.get("/api/spotreba/off")
def api_spotreba_off(verze: str = "", x_hans_klic: str = Header(default="")):
    """HANS_SPOTREBA_OFF_TABULKA_V1 — offline tabulka výrobků (gz SQLite) pro telefon.
    S `verze` = ta, kterou telefon má → jen {"aktualni": true}."""
    tok = str(((_config().get("spotreba") or {}).get("token")) or "")
    if not tok or not hmac.compare_digest(tok, x_hans_klic or ""):
        return JSONResponse({"chyba": "neplatný klíč"}, status_code=403)
    from scripts import hans_spotreba as hs
    v = hs.off_verze()
    if not v:
        return JSONResponse({"chyba": "tabulka zatím není"}, status_code=404)
    import logging
    logging.getLogger(__name__).info("HANS_SPOTREBA_OFF_TABULKA_V1: telefon má %r, Hans %s → %s",
                                     verze, v, "aktuální" if verze == v else "posílám")
    if verze == v:
        return {"aktualni": True, "verze": v}
    from fastapi.responses import FileResponse
    return FileResponse(hs._OFF_GZ, media_type="application/gzip", headers={"X-Verze": v})


@router.get("/api/spotreba")
def api_spotreba():
    import datetime as _dt
    from scripts import hans_spotreba as hs
    dnes = _dt.date.today()
    out = []
    for p in hs.zasoby():
        d = hs._dny(p.get("datum") or "", dnes)
        out.append({"nazev": p.get("nazev") or "", "kod": p.get("kod") or "",
                    "datum": p.get("datum") or "", "dni": d,
                    "mnozstvi": p.get("mnozstvi") or ""})
    return {"polozky": out}
