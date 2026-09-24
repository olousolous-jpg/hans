"""Jazykové modely: výběr generátoru, náhrada `hans-czech`, kontrola dostupnosti.

`hans-czech` je vlastní model z původního PC a nedá se stáhnout. Na novém PC
se místo něj vytvoří alias `<persona>-czech` z veřejného základního modelu
(bez vlastního SYSTEM promptu — identitu Hans posílá v každém dotazu z
persona.core, takže ji Severka může měnit). Všechny výskyty v configu se
přepíšou na nový alias.
"""
from __future__ import annotations

import re
from typing import Optional

from . import cfg as C
from . import ui
from .ollama import Ollama, OllamaError, normalize, pull_progress_printer

LEGACY_CHAT = "hans-czech:latest"

# Kandidáti na generátor textů v pořadí preference (čeština!)
GEN_PREFERRED = ["jobautomation/OpenEuroLLM-Czech:latest", "gemma3:12b", "qwen2.5:14b",
                 "gemma3:4b", "qwen2.5:7b"]
GEN_DEFAULT_PC = "jobautomation/OpenEuroLLM-Czech:latest"
GEN_DEFAULT_LOCAL = "gemma3:4b"      # ~3,3 GB, na Pi 5 s 8 GB RAM použitelné

# Základ pro chatový model persony
CHAT_BASES = [
    ("1", "jobautomation/OpenEuroLLM-Czech:latest", "nejlepší čeština, ~8 GB VRAM (doporučeno)"),
    ("2", "qwen2.5:14b", "silnější uvažování, slabší čeština, ~9 GB VRAM"),
    ("3", "qwen2.5:7b", "pro slabší GPU, ~5 GB VRAM"),
]

# Náhrady, když model chybí a stáhnout nejde/nechce se
SUBSTITUTES = {
    "qwen3:30b": "jobautomation/OpenEuroLLM-Czech:latest",
    "translategemma:12b": "jobautomation/OpenEuroLLM-Czech:latest",
    "qwen2.5vl:7b": "llava:7b",
}

# Modely, které ve veřejné knihovně Ollamy nejsou (původní PC je importoval ručně)
CUSTOM_IMPORTS = {
    "chatmusician:latest": "ChatMusician (skládání not) se do Ollamy importuje ručně "
                           "z Hugging Face — viz poznámka u `music` v config.json",
}

_SKIP_SECTIONS = ("gemini", "openrouter")
_MODEL_KEY = re.compile(r"(^|_)model(_name)?$")


def model_paths(cfg: dict) -> dict[str, str]:
    """cesta → název Ollama modelu pro všechna místa v configu, kde je model."""
    out = {}

    def walk(d, pref=""):
        for k, v in d.items():
            p = pref + k
            if isinstance(v, dict):
                walk(v, p + ".")
            elif isinstance(v, str) and v and " " not in v:
                sec = p.split(".")[0]
                if sec in _SKIP_SECTIONS or "image_model" in k or k in ("camera_model", "wake_model"):
                    continue
                if sec == "models" or _MODEL_KEY.search(k):
                    out[p] = v
    walk(cfg)
    return out


# ── generátor ──────────────────────────────────────────────────────────────
def pick_generator(url: str, local: bool, dry_run: bool, preset: str = "") -> Optional[str]:
    """Vrátí název modelu pro generování textů (stáhne ho, když chybí)."""
    cl = Ollama(url)
    if not cl.version():
        ui.warn("Ollama na %s neodpovídá." % url)
        return None
    installed = cl.models()
    if preset:
        cands = [preset]
    else:
        cands = [m for m in GEN_PREFERRED if cl.has(m, installed)]
    if cands and cl.has(cands[0], installed):
        ui.ok("Generátor: %s" % cands[0])
        return normalize(cands[0])
    want = preset or (GEN_DEFAULT_LOCAL if local else GEN_DEFAULT_PC)
    ui.info("Na %s chybí vhodný model pro češtinu. Navrhuji stáhnout %s." % (url, want))
    if dry_run:
        ui.info("[dry-run] stahování přeskočeno")
        return None
    if not ui.confirm("Stáhnout %s?" % want, True):
        return None
    try:
        cl.pull(want, pull_progress_printer())
    except OllamaError as e:
        ui.warn(str(e))
        return None
    return normalize(want)


# ── chatový model persony ───────────────────────────────────────────────────
def step_chat_model(cfg: dict, url: str, persona_slug: str, dry_run: bool) -> None:
    ui.header("Chatový model persony")
    cl = Ollama(url)
    if not cl.version():
        ui.warn("Ollama na %s neodpovídá — krok přeskočen (spusť instalátor znovu)." % url)
        return
    installed = cl.models()
    current = normalize(C.get(cfg, "models.dialog", LEGACY_CHAT) or LEGACY_CHAT)
    if cl.has(current, installed):
        ui.ok("Chatový model %s na PC existuje." % current)
        if not ui.confirm("Ponechat ho?", True):
            current = ""
    else:
        ui.info("Model %s na PC není (je to vlastní model původní instalace)." % current)
        current = ""
    if current:
        return
    for key, name, desc in CHAT_BASES:
        mark = "  (už staženo)" if cl.has(name, installed) else ""
        print("    %s) %-42s %s%s" % (key, name, desc, mark))
    ch = ui.ask("Základní model (číslo, nebo vlastní název)", "1")
    base = next((n for k, n, _ in CHAT_BASES if k == ch), ch)
    alias = "%s-czech:latest" % persona_slug
    old = normalize(C.get(cfg, "models.dialog", LEGACY_CHAT) or LEGACY_CHAT)
    if dry_run:
        ui.info("[dry-run] vytvořil bych %s z %s a přepsal %s v configu" % (alias, base, old))
    else:
        try:
            if not cl.has(base, installed):
                ui.info("Stahuji %s…" % base)
                cl.pull(base, pull_progress_printer())
            cl.create_alias(alias, normalize(base), {"temperature": 0.7, "num_ctx": 8192})
            ui.ok("Vytvořen model %s (základ %s)" % (alias, base))
        except OllamaError as e:
            ui.warn("Alias se nepodařilo vytvořit (%s) — použiju přímo %s." % (e, base))
            alias = normalize(base)
    changed = set()
    for legacy in {old, LEGACY_CHAT, LEGACY_CHAT.split(":")[0]}:
        changed.update(C.replace_values(cfg, legacy, alias))
    for p in ("models.dialog", "models.utility", "models.voice"):
        C.set_(cfg, p, alias)
        changed.add(p)
    ui.ok("Model %s nastaven na %d místech v configu." % (alias, len(changed)))


# ── kontrola všech modelů z configu ─────────────────────────────────────────
def step_audit(cfg: dict, url: str, dry_run: bool) -> None:
    ui.header("Kontrola modelů na PC")
    cl = Ollama(url)
    if not cl.version():
        ui.warn("Ollama na %s neodpovídá — kontrola přeskočena." % url)
        return
    installed = cl.models()
    by_model: dict[str, list[str]] = {}
    for path, m in model_paths(cfg).items():
        by_model.setdefault(normalize(m), []).append(path)
    missing = [m for m in by_model if not cl.has(m, installed)]
    for m in sorted(by_model):
        state = "chybí" if m in missing else "OK"
        print("    %-45s %-6s (%d× v configu)" % (m, state, len(by_model[m])))
    if not missing:
        ui.ok("Všechny modely z configu jsou na PC.")
        return
    if dry_run:
        ui.info("[dry-run] chybějící modely nestahuji: %s" % ", ".join(missing))
        return
    for m in missing:
        if m in CUSTOM_IMPORTS:
            ui.warn("%s: %s. Přeskakuji." % (m, CUSTOM_IMPORTS[m]))
            continue
        sub = SUBSTITUTES.get(m)
        opts = [("s", "stáhnout %s" % m)]
        if sub:
            opts.append(("n", "nahradit modelem %s" % sub))
        opts.append(("p", "přeskočit (funkce, která ho používá, nepoběží)"))
        ch = ui.choose("Model %s chybí:" % m, opts, "s")
        if ch == "s":
            try:
                cl.pull(m, pull_progress_printer())
                continue
            except OllamaError as e:
                ui.warn(str(e))
                if not sub:
                    continue
                if not ui.confirm("Nahradit modelem %s?" % sub, True):
                    continue
                ch = "n"
        if ch == "n" and sub:
            if not cl.has(sub, cl.models()):
                try:
                    cl.pull(sub, pull_progress_printer())
                except OllamaError as e:
                    ui.warn(str(e))
                    continue
            for p in by_model[m]:
                C.set_(cfg, p, sub)
            ui.ok("%s → %s (%d míst)" % (m, sub, len(by_model[m])))
