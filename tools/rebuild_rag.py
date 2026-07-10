#!/usr/bin/env python3
"""
rebuild_rag.py — idempotentní orchestrátor RAG vrstvy.

Spustit kdykoliv po:
  - enroll nové osoby (přidá její vztahovou kartu do RAGu)
  - vymazání + re-enroll všech osob (regeneruje karty pro všechny)
  - reset embedding modelu / vector DB / kolekcí (postaví znovu)
  - změně charakteristiky některé osoby přes večerní reflexi

Fáze:
  1. Ověř embedding model = bge-m3:latest (warning pokud ne)
  2. Pro každou kolekci v configu: existuje? pokud ne, vytvoř. Update config.json.
  3. Pro každou osobu v SQL relationships: re-build kartu a uploadni do hans_identita
     (HansKnowledge.upload je idempotentní: remove starou + add novou)
  4. Update hans-rag model preset s aktuálními KIDs (přes API)
  5. Sanity check: retrieval test "Co víš o <jméno>?" pro každou osobu

Bezpečnost:
  - Neprovádí destruktivní operace (žádné Reset Vector DB)
  - Soubory které už v kolekcích jsou ponechá (jen vztahové karty se přepisují)
  - Záloha config.json před změnou
  - Vrací non-zero exit code při fatal chybě (vhodné pro hooks)
"""
from __future__ import annotations
import json
import shutil
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent  # config.json je v rootu, ne v tools/
sys.path.insert(0, str(ROOT / "scripts"))

CFG_PATH = ROOT / "config.json"

# Definice kolekcí (stejné jako knowledge_setup.py)
COLLECTIONS = [
    ("hans_identita", "Kdo je Hans, kdo je Koláč, vztahy, hardware"),
    ("hans_filmy",    "Hansovy názory na filmy které 'shlédl'"),
    ("hans_cetba",    "Reflexe z článků, knih, kapitol"),
    ("hans_pripady",  "Uzavřené detektivní případy s Koláčem"),
    ("hans_denik",    "Denní shrnutí, večerní zápisky"),
]

EXPECTED_EMBED_MODEL = "bge-m3:latest"
HANS_RAG_MODEL_ID = "hans-rag"

# Globální stav
cfg: dict = {}
base_url: str = ""
headers_json: dict = {}
headers_auth: dict = {}


def log(msg: str = "", level: str = ""):
    prefix = {"OK": "✓", "WARN": "⚠", "FAIL": "✗", "INFO": "→", "": " "}.get(level, " ")
    print(f"  {prefix} {msg}" if msg else "")


def section(title: str):
    print()
    print("=" * 70)
    print(title)
    print("=" * 70)


# ── FÁZE 1: Embedding model check ────────────────────────────────────────
def phase_1_check_embedding() -> bool:
    section("FÁZE 1: Embedding model check")
    try:
        r = requests.get(f"{base_url}/api/v1/retrieval/embedding",
                         headers=headers_auth, timeout=10)
        if r.status_code != 200:
            # Endpoint může vrátit HTML při auth fail; nevadí, jdeme dál s warningem
            log(f"endpoint /retrieval/embedding HTTP {r.status_code} — přeskakuju check",
                "WARN")
            return True
        d = r.json()
        engine = d.get("embedding_engine", "?")
        model = d.get("embedding_model", "?")
        log(f"engine: {engine}", "")
        log(f"model:  {model}", "")
        if model != EXPECTED_EMBED_MODEL:
            log(f"OČEKÁVÁNO: {EXPECTED_EMBED_MODEL} — zkontroluj admin UI",
                "WARN")
        else:
            log("embedding model OK", "OK")
    except Exception as e:
        log(f"check selhal: {e} — pokračuji", "WARN")
    return True


# ── FÁZE 2: Kolekce + config.json ─────────────────────────────────────────
def phase_2_collections() -> bool:
    section("FÁZE 2: Knowledge kolekce")

    r = requests.get(f"{base_url}/api/v1/knowledge/",
                     headers=headers_json, timeout=10)
    if r.status_code != 200:
        log(f"listing kolekcí FAIL: HTTP {r.status_code}", "FAIL")
        return False
    existing = {item["name"]: item["id"]
                for item in r.json().get("items", [])
                if isinstance(item, dict)}
    log(f"{len(existing)} kolekcí už existuje na serveru")

    new_ids: dict[str, str] = {}
    for name, desc in COLLECTIONS:
        if name in existing:
            new_ids[name] = existing[name]
            log(f"{name:20s} už existuje  id={existing[name]}", "OK")
        else:
            cr = requests.post(
                f"{base_url}/api/v1/knowledge/create",
                headers=headers_json,
                json={"name": name, "description": desc,
                      "data": {}, "access_control": None},
                timeout=15,
            )
            if cr.status_code != 200:
                log(f"create {name} FAIL: HTTP {cr.status_code} "
                    f"{cr.text[:120]}", "FAIL")
                return False
            kid = cr.json()["id"]
            new_ids[name] = kid
            log(f"{name:20s} vytvořeno   id={kid}", "OK")

    # Update config.json (jen pokud se KIDs liší)
    current = cfg.get("knowledge", {}).get("collections", {})
    if current != new_ids:
        bak = CFG_PATH.with_suffix(f".json.bak.{int(time.time())}")
        shutil.copy2(CFG_PATH, bak)
        log(f"záloha config.json -> {bak.name}", "INFO")

        cfg.setdefault("knowledge", {})
        cfg["knowledge"]["base_url"] = base_url
        cfg["knowledge"]["collections"] = new_ids
        cfg["knowledge"].setdefault("enabled", True)
        CFG_PATH.write_text(
            json.dumps(cfg, indent=4, ensure_ascii=False),
            encoding="utf-8",
        )
        log("config.json aktualizován", "OK")
    else:
        log("config.json už má aktuální KIDs", "OK")

    return True


# ── FÁZE 3: Regenerace vztahových karet ──────────────────────────────────
def phase_3_relationships() -> bool:
    section("FÁZE 3: Vztahové karty z SQL")
    try:
        from hans_relationships import Relationships, RelationshipReflection
        from hans_knowledge import HansKnowledge
    except Exception as e:
        log(f"import FAIL: {e}", "FAIL")
        return False

    knowledge = HansKnowledge(cfg)
    if not knowledge.enabled:
        log("HansKnowledge disabled — přeskakuju", "WARN")
        return True

    rels = Relationships(cfg)
    cards = rels.all_cards()
    log(f"karet v SQL: {len(cards)}")

    # _build_rag_document je čistá fce (self nepoužívá) — voláme přes třídu.
    # RelationshipReflection se přesunul do hans_relationships.py.
    build_doc = RelationshipReflection._build_rag_document

    ok_count = 0
    skip_count = 0
    for card in cards:
        if not card.characterization:
            log(f"{card.person_id:15s} (charakteristika prázdná, přeskakuju)",
                "WARN")
            skip_count += 1
            continue

        text = build_doc(None, card)
        doc_id = f"relationship_{card.person_id}"
        title = f"Vztah — {card.display_name}"
        metadata = {
            "typ": "vztahová karta",
            "osoba": card.person_id,
            "jmeno": card.display_name,
            "role": card.role or "",
        }

        t0 = time.time()
        ok = knowledge.upload("hans_identita", doc_id, title, text, metadata)
        elapsed = time.time() - t0
        if ok:
            log(f"{card.person_id:15s} ({len(text)} znaků, "
                f"{elapsed:.1f}s)", "OK")
            ok_count += 1
        else:
            log(f"{card.person_id:15s} upload FAIL", "FAIL")

    knowledge.stop()
    log(f"souhrn: {ok_count} uploaded, {skip_count} skipped "
        f"(prázdná charakteristika)")
    return True


# ── FÁZE 4: Update hans-rag model preset ─────────────────────────────────
def phase_4_model_preset() -> bool:
    section("FÁZE 4: hans-rag model preset")

    r = requests.get(
        f"{base_url}/api/v1/models/model?id={HANS_RAG_MODEL_ID}",
        headers=headers_json, timeout=15,
    )
    if r.status_code != 200:
        log(f"GET hans-rag FAIL: HTTP {r.status_code}", "FAIL")
        return False
    model = r.json()
    log(f"načteno: {model.get('name')}")

    # Stáhni nové kolekce
    new_knowledge = []
    for ckey, kid in cfg["knowledge"]["collections"].items():
        cr = requests.get(f"{base_url}/api/v1/knowledge/{kid}",
                          headers=headers_json, timeout=10)
        if cr.status_code != 200:
            log(f"kolekce {ckey} GET FAIL: HTTP {cr.status_code}", "FAIL")
            return False
        col = cr.json()
        # Strip 'files' (může být velké)
        col_clean = {k: v for k, v in col.items() if k != "files"}
        col_clean["type"] = "collection"
        new_knowledge.append(col_clean)

    meta = model.get("meta") or {}
    old = meta.get("knowledge") or []
    old_ids = {k.get("id") for k in old if isinstance(k, dict)}
    new_ids = {k["id"] for k in new_knowledge}

    if old_ids == new_ids:
        log(f"hans-rag už ukazuje na správných {len(new_ids)} kolekcí", "OK")
        return True

    meta["knowledge"] = new_knowledge
    payload = {
        "id": model["id"],
        "base_model_id": model.get("base_model_id"),
        "name": model["name"],
        "meta": meta,
        "params": model.get("params") or {},
        "access_control": model.get("access_control"),
        "is_active": model.get("is_active", True),
    }
    pr = requests.post(
        f"{base_url}/api/v1/models/model/update?id={HANS_RAG_MODEL_ID}",
        headers=headers_json, json=payload, timeout=15,
    )
    if pr.status_code != 200:
        log(f"update FAIL: HTTP {pr.status_code} {pr.text[:200]}", "FAIL")
        return False
    log(f"hans-rag updated: {len(old_ids)} -> {len(new_ids)} kolekcí", "OK")
    for k in new_knowledge:
        log(f"  - {k.get('name','?'):20s} id={k.get('id','?')}")
    return True


# ── FÁZE 5: Retrieval sanity check ───────────────────────────────────────
def phase_5_sanity() -> bool:
    section("FÁZE 5: Retrieval sanity check")
    kid = cfg["knowledge"]["collections"]["hans_identita"]

    try:
        from hans_relationships import Relationships
        rels = Relationships(cfg)
        cards = rels.all_cards()
    except Exception as e:
        log(f"načtení karet FAIL: {e}", "FAIL")
        return False

    all_pass = True
    for card in cards:
        if not card.characterization:
            continue
        query = f"Co víš o {card.display_name}?"
        try:
            r = requests.post(
                f"{base_url}/api/v1/retrieval/query/collection",
                headers=headers_json,
                json={"collection_names": [kid], "query": query, "k": 3},
                timeout=20,
            )
            d = r.json()
            metas = (d.get("metadatas") or [[]])[0]
            distances = (d.get("distances") or [[]])[0]
            if not metas:
                log(f"{query}  -> (žádné výsledky)", "FAIL")
                all_pass = False
                continue
            top_name = metas[0].get("name", "?")
            top_dist = distances[0] if distances else "?"
            expected = f"relationship_{card.person_id}.txt"
            if top_name == expected:
                log(f"{query:30s} -> {top_name} (dist={top_dist:.3f})", "OK")
            else:
                log(f"{query:30s} -> {top_name} (čekáno {expected})",
                    "WARN")
        except Exception as e:
            log(f"{query} ERR: {e}", "FAIL")
            all_pass = False
    return all_pass


# ── Main ──────────────────────────────────────────────────────────────────
def main() -> int:
    global cfg, base_url, headers_json, headers_auth

    if not CFG_PATH.exists():
        print(f"✗ Config nenalezen: {CFG_PATH}")
        return 1
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))

    ow = cfg.get("openwebui_direct", {}) or {}
    token = ow.get("api_token", "")
    base_url = (cfg.get("knowledge", {}).get("base_url")
                or ow.get("base_url", "")).rstrip("/")
    if not base_url or not token:
        print("✗ Chybí base_url nebo api_token v configu")
        return 1

    headers_auth = {"Authorization": f"Bearer {token}"}
    headers_json = {**headers_auth, "Content-Type": "application/json"}

    print(f"→ OpenWebUI: {base_url}")
    print(f"→ Config:    {CFG_PATH}")

    phases = [
        ("embedding model check", phase_1_check_embedding),
        ("knowledge kolekce",     phase_2_collections),
        ("vztahové karty",        phase_3_relationships),
        ("hans-rag model preset", phase_4_model_preset),
        ("retrieval sanity",      phase_5_sanity),
    ]
    for fname, fn in phases:
        try:
            ok = fn()
        except Exception as e:
            print()
            print(f"✗ Fáze '{fname}' vyhodila výjimku: {e}")
            import traceback
            traceback.print_exc()
            return 1
        if not ok:
            print()
            print(f"✗ Fáze '{fname}' selhala — končím.")
            return 1

    print()
    print("=" * 70)
    print("HOTOVO. Hans má aktuální RAG vrstvu.")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())