"""Krok „paměť": RAG kolekce v OpenWebUI + úvodní dokumenty identity.

Proč ne rovnou tools/knowledge_setup.py a tools/bootstrap_identity.py: oba čtou
jen veřejný `config.json`, ale token a adresa OpenWebUI jsou od rozdělení
configu v `config.private.json` — na čisté instalaci by skončily hláškou
„chybí api_token". knowledge_setup navíc zapisuje do veřejného souboru.
Tady se config čte i zapisuje přes config_io. Seznam kolekcí se bere přímo
z tools/knowledge_setup.py (aby se nerozešel), dokumenty identity z průvodce.
"""
from __future__ import annotations

import ast
import json
import sys
import urllib.error
import urllib.request

from . import cfg as C
from . import ui


def collections() -> list[tuple[str, str]]:
    src = (C.ROOT / "tools" / "knowledge_setup.py").read_text(encoding="utf-8")
    for node in ast.parse(src).body:
        if isinstance(node, ast.Assign) and any(
                getattr(t, "id", "") == "COLLECTIONS" for t in node.targets):
            return [tuple(x) for x in ast.literal_eval(node.value)]
    raise RuntimeError("COLLECTIONS v tools/knowledge_setup.py nenalezeno")


def _call(method: str, url: str, token: str, body=None):
    req = urllib.request.Request(
        url, method=method, data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode("utf-8") or "{}")


def ensure_collections(cfg: dict) -> bool:
    base = str(C.get(cfg, "openwebui_direct.base_url", "") or "").rstrip("/")
    token = str(C.get(cfg, "openwebui_direct.api_token", "") or "")
    if not base or not token:
        ui.warn("Chybí adresa nebo API klíč OpenWebUI — paměť přeskočena.")
        return False
    try:
        data = _call("GET", base + "/api/v1/knowledge/", token)
    except (urllib.error.URLError, OSError, ValueError) as e:
        ui.warn("OpenWebUI na %s neodpovídá nebo odmítl klíč: %s" % (base, e))
        return False
    items = data.get("items", data) if isinstance(data, dict) else data
    existing = {i["name"]: i["id"] for i in (items or []) if isinstance(i, dict)}
    ids = {}
    for name, desc in collections():
        if name in existing:
            ids[name] = existing[name]
            ui.ok("%s už existuje" % name)
            continue
        try:
            r = _call("POST", base + "/api/v1/knowledge/create", token,
                      {"name": name, "description": desc, "data": {}, "access_control": None})
            ids[name] = r["id"]
            ui.ok("%s vytvořena" % name)
        except (urllib.error.URLError, OSError, ValueError, KeyError) as e:
            ui.warn("Kolekci %s se nepodařilo vytvořit: %s" % (name, e))
    if len(ids) != len(collections()):
        return False
    C.set_(cfg, "knowledge.base_url", base)
    C.set_(cfg, "knowledge.collections", ids)
    C.set_(cfg, "knowledge.enabled", True)
    return True


def upload_identity(cfg: dict, docs: list[dict]) -> None:
    if not docs:
        ui.info("Žádné vygenerované dokumenty identity — přeskočeno.")
        return
    try:
        if str(C.ROOT) not in sys.path:
            sys.path.insert(0, str(C.ROOT))
        from scripts.hans_knowledge import HansKnowledge
    except Exception as e:
        ui.warn("Nelze načíst scripts.hans_knowledge (%s) — běží průvodce ve venv?" % e)
        return
    kn = HansKnowledge(cfg)
    if not kn.enabled:
        ui.warn("Paměť není zapnutá (kolekce/token) — dokumenty nenahrány.")
        return
    try:
        for d in docs:
            okk = kn.upload(collection_key="hans_identita", doc_id=d["doc_id"],
                            title=d["title"], text=d["text"],
                            metadata={"typ": "identita", "verze": "1", "zdroj": "installer"})
            (ui.ok if okk else ui.warn)("%s  %s" % (d["doc_id"], d["title"]))
    finally:
        try:
            kn.stop()
        except Exception:
            pass
