# -*- coding: utf-8 -*-
"""HANS_TOOLSCOUT_NO_MATCH_V1 — prázdný výsledek je ODPOVĚĎ, ne důvod k opakování.

Doloženo 25.8.: téma „historie a památky" dávalo klíč `history`, ollama.com
takový model nemá → 0 kandidátů → nic se neuložilo → `has_for_topic` zůstalo
false → Toolscout to zkoušel ~20× za noc a `break` po prvním tématu bez návrhu
BLOKOVAL frontu dalších dostudovaných témat (3 a 4 se nikdy nedostaly na řadu).

⚠️ Klíčové je rozlišení: výpadek sítě se MUSÍ odkládat dál, kdežto „knihovna
nic nemá" je uzavřený výsledek. Dřív vypadaly obě situace stejně (prázdný list).

Deterministický (žádná síť, žádný LLM). Spuštění:
    python3 tests/test_toolscout_no_match.py
"""
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, ".")

from scripts import hans_toolscout as ts

CFG = {"toolscout": {"enabled": True, "gpu_total_gb": 16.0,
                     "chat_resident_gb": 10.8, "safety_gb": 1.0,
                     "search_limit": 12}}

KANDIDAT = [{"name": "qwen2.5-coder", "sizes": ["7b"], "sizes_b": [7.0],
             "pulls": "20M", "capabilities": ["tools"],
             "url": "https://ollama.com/library/qwen2.5-coder"}]


def _db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    c = sqlite3.connect(path)
    c.execute("""CREATE TABLE tool_proposals (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, topic TEXT,
        tool_name TEXT, size_tag TEXT, est_gb REAL, fit TEXT, pulls TEXT,
        capabilities TEXT, rationale TEXT, url TEXT,
        status TEXT DEFAULT 'pending')""")
    c.commit(); c.close()
    return path


def main():
    ok = bad = 0

    def zk(popis, podm):
        nonlocal ok, bad
        if podm:
            ok += 1; print("   OK     %s" % popis)
        else:
            bad += 1; print("   CHYBA  %s" % popis)

    puv = (ts.search_library_ex, ts._derive_keyword, ts._rationale,
           ts.scout_tools_for_topic, ts.installed_models)
    ts._derive_keyword = lambda cfg, topic: "klic"
    ts._rationale = lambda cfg, topic, cands: "důvod"
    path = _db()
    try:
        # ── A) síť OK, knihovna nic nemá → terminální 'none' ──────────
        ts.search_library_ex = lambda q, limit=12, timeout=15: ([], True)
        r = ts.propose_tool(CFG, path, "historie a památky")
        zk("prázdný výsledek při živé síti → status 'none'",
           r.get("status") == "none")
        store = ts.ToolStore(path)
        zk("téma je uzavřené → fronta se pohne dál",
           store.has_for_topic("historie a památky") is True)
        c = sqlite3.connect(path)
        row = c.execute("SELECT tool_name, status, rationale FROM "
                        "tool_proposals WHERE topic='historie a památky'"
                        ).fetchone()
        c.close()
        zk("neuložil se ŽÁDNÝ náhradní nástroj (konfabulace)", row[0] == "")
        zk("odůvodnění přiznává, že se nic nenašlo",
           "nenašel" in (row[2] or ""))
        zk("'none' se nedá omylem schválit (mimo výpis pendingů)",
           all(p["topic"] != "historie a památky"
               for p in store.list("pending")))

        # ── B) výpadek sítě → dál se odkládá, NIC se nezapisuje ───────
        ts.search_library_ex = lambda q, limit=12, timeout=15: ([], False)
        r2 = ts.propose_tool(CFG, path, "hrady a historická architektura")
        zk("výpadek sítě → 'deferred' (ne 'none')",
           r2.get("status") == "deferred")
        zk("při výpadku se téma NEuzavře (zkusí se znovu)",
           store.has_for_topic("hrady a historická architektura") is False)

        # ── C) normální cesta se nerozbila ───────────────────────────
        ts.search_library_ex = lambda q, limit=12, timeout=15: (KANDIDAT, True)
        r3 = ts.propose_tool(CFG, path, "Design")
        zk("reálný kandidát → 'proposed'", r3.get("status") == "proposed")
        zk("návrh je v pendingu ke schválení",
           any(p["topic"] == "Design" for p in store.list("pending")))

        # ── D) idempotence ───────────────────────────────────────────
        ts.search_library_ex = lambda q, limit=12, timeout=15: ([], True)
        r4 = ts.propose_tool(CFG, path, "historie a památky")
        zk("uzavřené téma se znovu nezkouší", r4.get("status") == "idle")
        c = sqlite3.connect(path)
        n = c.execute("SELECT count(*) FROM tool_proposals WHERE "
                      "topic='historie a památky'").fetchone()[0]
        c.close()
        zk("nevznikl druhý 'none' řádek", n == 1)

        # ── E) HANS_TOOLSCOUT_RANK_V2: popularita PŘED „vejde se vedle" ──
        VELKY = {"name": "qwen3-vl", "size_tag": "8b", "est_gb": 6.0,
                 "fit": "on_demand", "note": "", "pulls": "5.4M",
                 "capabilities": ["vision"], "url": "u1"}
        MALY = {"name": "moondream", "size_tag": "1.8b", "est_gb": 2.0,
                "fit": "coexist", "note": "", "pulls": "1.7M",
                "capabilities": ["vision"], "url": "u2"}
        ts.scout_tools_for_topic = lambda cfg, t, kw="": {
            "topic": t, "keyword": kw, "fetch_ok": True,
            "candidates": [dict(MALY), dict(VELKY)]}
        ts.installed_models = lambda cfg: set()
        r5 = ts.propose_tool(CFG, path, "hrady", max_props=1)
        zk("populárnější vyhraje nad menším, co se vejde vedle chatu",
           (r5.get("proposals") or [{}])[0].get("name") == "qwen3-vl")

        # ── F) nenavrhuj, co UŽ NA PC JE (jinak hrozí downgrade) ─────
        ts.installed_models = lambda cfg: {"qwen3-vl"}
        r6 = ts.propose_tool(CFG, path, "hrady 2", max_props=1)
        zk("nainstalovaný kandidát se vynechá",
           (r6.get("proposals") or [{}])[0].get("name") == "moondream")
        ts.installed_models = lambda cfg: {"qwen3-vl", "moondream"}
        r7 = ts.propose_tool(CFG, path, "hrady 3")
        zk("když už mám všechno → 'none', ne prázdný návrh",
           r7.get("status") == "none")
        ts.installed_models = lambda cfg: set()      # PC dole = NEVÍME
        r8 = ts.propose_tool(CFG, path, "hrady 4", max_props=1)
        zk("PC dole → nefiltruje se (z neznalosti se nedělá závěr)",
           r8.get("status") == "proposed")

        # ── G) HANS_TOOLSCOUT_VERIFY_V1: 'installed' až po ověření ───
        c = sqlite3.connect(path)
        c.execute("INSERT INTO tool_proposals (ts, topic, tool_name, status) "
                  "VALUES (0,'X','deepseek-coder','approved')")
        c.commit(); c.close()
        ts.installed_models = lambda cfg: set()
        zk("PC dole → schválené se NErazítkuje",
           ts.verify_approved(CFG, path) == 0)
        ts.installed_models = lambda cfg: {"deepseek-coder"}
        zk("model dorazil → approved povýší na installed",
           ts.verify_approved(CFG, path) == 1)
        zk("podruhé už není co povyšovat",
           ts.verify_approved(CFG, path) == 0)

        print("\n%d/%d prošlo" % (ok, ok + bad))
        return 1 if bad else 0
    finally:
        (ts.search_library_ex, ts._derive_keyword, ts._rationale,
         ts.scout_tools_for_topic, ts.installed_models) = puv
        os.unlink(path)


if __name__ == "__main__":
    sys.exit(main())
