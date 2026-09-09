"""llm_trace.py — HANS_LLM_TRACE_V1 (9. 9.)

Měřicí zápis KAŽDÉHO volání LLM: kdy, jaký MODEL, který VOLAJÍCÍ, jak dlouho.

Proč vzniklo: v noci se studium (base OpenEuroLLM) pere o VRAM s hans-czech
konzumenty a z logu se nedá zjistit, KTERÝ krok který model vytáhl — hlášky
`ollama_client` nesou jen URL a timeout. Bez toho by se pořadí oprav volilo
podle dojmu, ne podle čísel.

⚠️ Zapisuje do `data/mereni/`, NE do `system.log` — ten se točí po 2 dnech
a měření by zmizelo dřív, než se vyhodnotí.

⚠️ Nikdy nesmí shodit volajícího: všechno je v `try/except`, a když se zápis
nepovede, volání LLM proběhne normálně dál.

Vypnout: `config.json` → `ollama.trace_calls: false`.
"""
from __future__ import annotations

import os
import threading
import time
import traceback

CESTA = "data/mereni/llm_calls.log"
_zamek = threading.Lock()
_HLAVICKA = "# ts\tmodel\tvolajici\ttrvani_s\tvysledek\turl\n"

# Soubory, které jsou jen převodní páka — volajícího hledáme AŽ ZA nimi.
_PRUCHOZI = ("llm_trace.py", "ollama_client.py")


def _volajici() -> str:
    """Prvni ramec mimo prevodni pater = kdo si o model reálně řekl."""
    try:
        for ram in reversed(traceback.extract_stack()[:-1]):
            zaklad = os.path.basename(ram.filename)
            if zaklad not in _PRUCHOZI:
                return "%s:%s" % (zaklad.replace(".py", ""), ram.name)
    except Exception:
        pass
    return "?"


def zapnuto(config=None) -> bool:
    try:
        return bool(((config or {}).get("ollama", {}) or {})
                    .get("trace_calls", True))
    except Exception:
        return True


def zapis(model: str, url: str = "", trvani_s: float = 0.0,
          vysledek: str = "ok", volajici: str | None = None,
          config=None) -> None:
    """Jeden řádek TSV. Best-effort — chyba zápisu se nikam nepropaguje."""
    if not zapnuto(config):
        return
    try:
        radek = "%s\t%s\t%s\t%.1f\t%s\t%s\n" % (
            time.strftime("%Y-%m-%d %H:%M:%S"),
            (model or "?").replace("\t", " "),
            volajici or _volajici(),
            float(trvani_s), vysledek,
            (url or "").replace("\t", " "))
        with _zamek:
            novy = not os.path.exists(CESTA)
            os.makedirs(os.path.dirname(CESTA), exist_ok=True)
            with open(CESTA, "a", encoding="utf-8") as f:
                if novy:
                    f.write(_HLAVICKA)
                f.write(radek)
    except Exception:
        pass
