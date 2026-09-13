"""Jednorázový backfill diary.importance (AUTOBIOGRAPHICAL_IMPORTANCE_V1).

Gentle: dávky, sleep mezi nimi, break na 0 (Ollama down → bezpečně skončí,
neoskórované zůstanou NULL = doženou se nočním hookem). Re-spustitelný.
Spouštět z rootu: python3 -m scripts.backfill_importance_oneshot [cap]

BACKFILL_KEEPALIVE_V1 (29.8.) — dvě opravy po nálezu z 29.8., kdy bylo potřeba
dohnat 18 017 epizod:
  (a) `keep_alive="5m"` — bez něj platil default 0, takže se model po KAŽDÉ
      dávce uvolnil z VRAM a znovu načítal. U tisíců epizod to je rozdíl mezi
      hodinami a dny. Noční hook to dělá stejně (IMPORTANCE_NIGHTLY_CATCHUP_V1).
  (b) dávka i strop jsou parametry, ne konstanty — starý strop 150×20 = 3 000
      by tenhle backlog ani nepokryl a skončil by potichu uprostřed.
⚠️ Velikost dávky NEZVEDAT bez zvednutí `importance.num_ctx`: prompt roste
s ní a při přetečení okna se skórování TIŠE rozbije (přesně to stálo 26.6.–29.8.).
"""
import json, time, sys
from scripts.hans_importance import score_unscored

try:                                   # HANS_MAIN_CONFIG_IO_V1
    from scripts.config_io import load as _cfg_load
except ImportError:                    # spusteno jako skript, root chybi
    import sys as _s, os as _o
    _s.path.insert(0, _o.path.dirname(_o.path.dirname(_o.path.abspath(__file__))))
    from scripts.config_io import load as _cfg_load

# BACKFILL_MAIN_GUARD_V1 (13. 9.) — VYKONNY KOD AZ POD `__main__`.
# Soubor ho dosud mel na urovni modulu, takze pouhy `import scripts.
# backfill_importance_oneshot` SPUSTIL cely backfill. Doloženo 13. 9.:
# importni test po hromadne uprave configu rozjel skorovani a oskoroval
# 144 epizod (datova skoda zadna — bere jen `importance IS NULL`, tedy
# tutez praci jako nocni hook — ale zabral VRAM a cas).
# ⚠️ Docstring uz spravne rika `python3 -m scripts.backfill_...`, guard
# to jen konecne vynucuje.
if __name__ == "__main__":
    cfg = _cfg_load()
    db = cfg.get("diary_db", "data/hans_diary.db")
    er = cfg.get("evening_reflection", {}) or {}
    ic = cfg.get("importance", {}) or {}
    model = str(er.get("model", "jobautomation/OpenEuroLLM-Czech:latest"))
    timeout = int(er.get("llm_timeout", 300))
    batch = int(ic.get("max_per_run", 30))
    cap = int(sys.argv[1]) if len(sys.argv) > 1 else 100000

    t0 = time.time()
    total = 0
    batches = 0
    while total < cap:
        n = score_unscored(cfg, db, model, timeout,
                           limit=min(batch, cap - total), keep_alive="5m")
        total += n
        batches += 1
        if n == 0:
            print(f"[backfill] dávka {batches}: 0 → končím "
                  f"(buď je hotovo, nebo Ollama neodpovídá)", flush=True)
            break
        if batches % 10 == 0 or batches < 3:
            up = time.time() - t0
            print(f"[backfill] dávka {batches}: +{n} (celkem {total}) | "
                  f"{total/max(up,1)*60:.0f} epizod/min | běží {up/60:.1f} min",
                  flush=True)
        time.sleep(1)
    up = time.time() - t0
    print(f"BACKFILL DONE: oskórováno {total} epizod v {batches} dávkách "
          f"za {up/60:.1f} min", flush=True)
    sys.exit(0)
