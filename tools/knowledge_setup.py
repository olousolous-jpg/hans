#!/usr/bin/env python3
"""Vytvoří 5 produkčních Knowledge kolekcí v OpenWebUI a zapíše jejich
ID do config.json pod klíč 'knowledge.collections'.

Idempotentní: pokud už kolekce existuje (podle name), použije ji a nic
nepřidává. Bezpečné spustit opakovaně.

Spuštění:
    python3 knowledge_setup.py
"""
from __future__ import annotations
import json
import shutil
import sys
from pathlib import Path

import requests

# tools/ je podadresář — config.json je o úroveň výš (kořen projektu)
ROOT = Path(__file__).resolve().parent.parent
CFG_PATH = ROOT / "config.json"

# Definice produkčních kolekcí (musí pokrývat všechny collection_key v kódu)
COLLECTIONS = [
    ("hans_identita", "Kdo je Hans, kdo je Koláč, vztahy, hardware"),
    ("hans_filmy",    "Hansovy názory na filmy které 'shlédl'"),
    ("hans_cetba",    "Reflexe z článků, knih, kapitol"),
    ("hans_pripady",  "Uzavřené detektivní případy s Koláčem"),
    ("hans_denik",    "Denní shrnutí, večerní zápisky"),
    ("hans_dila",     "Hansova vytvořená díla — eseje z cílů"),
]


def main() -> int:
    if not CFG_PATH.exists():
        print(f"✗ Config nenalezen: {CFG_PATH}")
        return 1

    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    ow = cfg.get("openwebui_direct", {})
    base_url = ow.get("base_url", "").rstrip("/")
    token = ow.get("api_token", "")

    if not base_url or not token:
        print("✗ Chybí base_url nebo api_token v config.openwebui_direct")
        return 1

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    # Načti existující kolekce
    print(f"→ Listing existujících kolekcí na {base_url}...")
    r = requests.get(f"{base_url}/api/v1/knowledge/",
                     headers=headers, timeout=10)
    if r.status_code != 200:
        print(f"✗ Listing selhal: {r.status_code} {r.text[:200]}")
        return 1

    existing = {item["name"]: item["id"]
                for item in r.json().get("items", [])}
    print(f"  ({len(existing)} kolekcí už existuje)")

    # Vytvoř / použij
    result_ids: dict[str, str] = {}
    for name, desc in COLLECTIONS:
        if name in existing:
            kid = existing[name]
            print(f"  ✓ {name} už existuje: {kid}")
        else:
            payload = {
                "name": name,
                "description": desc,
                "data": {},
                "access_control": None,
            }
            cr = requests.post(f"{base_url}/api/v1/knowledge/create",
                               headers=headers, json=payload, timeout=10)
            if cr.status_code != 200:
                print(f"  ✗ Vytvoření {name} selhalo: "
                      f"{cr.status_code} {cr.text[:200]}")
                continue
            kid = cr.json()["id"]
            print(f"  + {name} vytvořeno: {kid}")
        result_ids[name] = kid

    if len(result_ids) != len(COLLECTIONS):
        print(f"✗ Vytvořeno jen {len(result_ids)}/{len(COLLECTIONS)}")
        return 1

    # Zapiš do configu
    bak = CFG_PATH.with_suffix(".json.bak.knowledge")
    if not bak.exists():
        shutil.copy2(CFG_PATH, bak)
        print(f"  (záloha config.json → {bak.name})")

    cfg.setdefault("knowledge", {})
    cfg["knowledge"]["base_url"] = base_url
    cfg["knowledge"]["collections"] = result_ids
    cfg["knowledge"]["enabled"] = True

    CFG_PATH.write_text(
        json.dumps(cfg, indent=4, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"\n✓ config.json aktualizován — knowledge.collections:")
    for name, kid in result_ids.items():
        print(f"    {name:20s} → {kid}")
    return 0


if __name__ == "__main__":
    sys.exit(main())