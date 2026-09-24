#!/usr/bin/env python3
"""Průvodce nastavením Hanse na novém zařízení (volá ho installer/install.sh).

Použití (z kořene repozitáře):
    python3 installer/wizard.py network            # IP PC, tokeny, Kodi, WOL
    python3 installer/wizard.py llm [--url U] [--model M] [--local] [--manual]
    python3 installer/wizard.py persona            # domácnost, persona, společník
    python3 installer/wizard.py models             # náhrada hans-czech + kontrola modelů
    python3 installer/wizard.py knowledge          # RAG kolekce + dokumenty identity
    python3 installer/wizard.py household          # jen přidat/upravit lidi v domácnosti
    python3 installer/wizard.py faces              # zápis obličejů (Hans musí běžet)
    python3 installer/wizard.py show               # co je nastavené

Společné přepínače:
    --dry-run   nic nezapisuje do skutečného configu ani nestahuje modely;
                výsledky jdou do data/installer/dryrun/
    --fresh     chovej se jako na novém zařízení (ignoruj config.private.json)
    --yes       neinteraktivně, všude výchozí odpovědi

Průvodce z Hansova kódu jen čte (scripts/config_io, cz_names, hans_knowledge).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from hans_setup import cfg as C            # noqa: E402
from hans_setup import faces, household, knowledge, models, network, persona, ui  # noqa: E402
from hans_setup.gen import ManualGen, OllamaGen  # noqa: E402

LOCAL_OLLAMA = "http://127.0.0.1:11434"


def make_gen(answers: dict, manual: bool = False):
    g = answers.get("generator") or {}
    if manual or g.get("mode") == "manual" or not g.get("model"):
        if not manual and not g:
            ui.warn("Generátor není nastaven (spusť krok llm) — použiju ruční režim.")
        return ManualGen()
    return OllamaGen(g["url"], g["model"])


def cmd_network(cfg, answers, a):
    network.step_pc(cfg)
    network.step_rest(cfg)
    return True


def cmd_llm(cfg, answers, a):
    ui.header("Jazykový model pro generování textů")
    if a.manual:
        answers["generator"] = {"mode": "manual"}
        ui.ok("Ruční režim: prompty zkopíruješ do Claude / ChatGPT.")
        return False
    url = a.url or (LOCAL_OLLAMA if a.local else network.ollama_url(cfg))
    if not url:
        ui.warn("Není známá adresa Ollamy (spusť nejdřív krok network, nebo --url).")
        return False
    model = models.pick_generator(url, a.local, a.dry_run, a.model or "")
    if not model:
        ui.warn("Generátor nenastaven — persona půjde vytvořit jen ručně.")
        answers["generator"] = {"mode": "manual"}
        return False
    answers["generator"] = {"mode": "local" if a.local else "pc", "url": url, "model": model}
    # rychlá zkouška, že model odpovídá česky a JSONem
    gen = OllamaGen(url, model)
    ui.info("Zkouším model…")
    try:
        r = gen.json('Vrať JSON {"pozdrav": "<krátký český pozdrav>"}.',
                     {"type": "object", "properties": {"pozdrav": {"type": "string"}},
                      "required": ["pozdrav"]}, temperature=0.2)
    except Exception as e:
        r = None
        ui.warn("Zkouška selhala: %s" % e)
    if r and r.get("pozdrav"):
        ui.ok("Model odpovídá: %s" % r["pozdrav"])
    return False  # config se nemění, jen answers


def cmd_persona(cfg, answers, a):
    gen = make_gen(answers, a.manual)
    people = household.step(cfg, gen if isinstance(gen, OllamaGen) else None, answers)
    persona.step(cfg, gen, answers, people)
    return True


def cmd_models(cfg, answers, a):
    url = network.ollama_url(cfg)
    if not url:
        ui.warn("Není nastavena adresa Ollamy na PC (krok network).")
        return False
    name = (answers.get("basics") or {}).get("name") or C.get(cfg, "persona.name", "hans")
    models.step_chat_model(cfg, url, persona.slug(name), a.dry_run)
    models.step_audit(cfg, url, a.dry_run)
    return True


def cmd_knowledge(cfg, answers, a):
    ui.header("Paměť (OpenWebUI)")
    if a.dry_run:
        ui.info("[dry-run] kolekce: %s" % ", ".join(n for n, _ in knowledge.collections()))
        for d in answers.get("identity_docs") or []:
            ui.info("[dry-run] nahrál bych %s (%d znaků)" % (d["doc_id"], len(d["text"])))
        return False
    if not knowledge.ensure_collections(cfg):
        return False
    # kolekce musí být v configu dřív, než se nahrává (HansKnowledge je čte z něj)
    if not C.save(cfg, False):
        return False
    knowledge.upload_identity(cfg, answers.get("identity_docs") or [])
    return False  # uloženo výš


def cmd_show(cfg, answers, a):
    ui.header("Stav nastavení")
    print("  persona:      %s" % C.get(cfg, "persona.name"))
    core = str(C.get(cfg, "persona.core", "") or "")
    print("  core:         %s…" % core[:100])
    print("  domácnost:    %s" % ", ".join(cfg.get("known_persons", {}).keys()))
    print("  společník:    %s" % C.get(cfg, "hans_dialog.kolac_name"))
    print("  Ollama (PC):  %s" % (network.ollama_url(cfg) or "-"))
    print("  OpenWebUI:    %s" % (C.get(cfg, "openwebui_direct.base_url") or "-"))
    print("  chat model:   %s" % C.get(cfg, "models.dialog"))
    print("  generátor:    %s" % (answers.get("generator") or {}))
    print("  paměť:        %s" % ("zapnutá" if C.get(cfg, "knowledge.collections") else "nenastavená"))
    return False


def cmd_household(cfg, answers, a):
    """Jen domácnost (přidání/úprava lidí) bez nového generování persony."""
    gen = make_gen(answers, a.manual)
    household.step(cfg, gen if isinstance(gen, OllamaGen) else None, answers)
    return True


def cmd_faces(cfg, answers, a):
    faces.step(cfg, a.dry_run)
    return False


def cmd_get(cfg, answers, a):
    """Vypíše hodnotu z configu (pro install.sh), např. `get pc_remote.user`."""
    v = C.get(cfg, a.key, "")
    print("" if v is None else v)
    return False


COMMANDS = {"network": cmd_network, "llm": cmd_llm, "persona": cmd_persona,
            "models": cmd_models, "knowledge": cmd_knowledge, "show": cmd_show,
            "household": cmd_household, "faces": cmd_faces, "get": cmd_get}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Průvodce nastavením Hanse")
    ap.add_argument("command", choices=sorted(COMMANDS))
    ap.add_argument("key", nargs="?", default="", help="jen pro příkaz get")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--fresh", action="store_true")
    ap.add_argument("--yes", action="store_true")
    ap.add_argument("--url", default="")
    ap.add_argument("--model", default="")
    ap.add_argument("--local", action="store_true")
    ap.add_argument("--manual", action="store_true")
    a = ap.parse_args(argv)
    ui.ASSUME_YES = a.yes

    cfg, fresh = C.load(a.dry_run, a.fresh)
    if a.command == "get":
        return COMMANDS["get"](cfg, {}, a) or 0
    if fresh and a.command == "network":
        ui.info("Čistá instalace: config.private.json vznikne ze vzoru (bez ukázkových hodnot).")
    answers = C.load_answers(a.dry_run)
    if a.dry_run and a.command in ("llm", "persona") and not a.manual:
        ui.warn("Dry-run nic nezapisuje, ale generování OPRAVDU volá Ollamu na PC —")
        ui.warn("načte model do VRAM a může dočasně vytlačit chatový model běžícího Hanse.")
    try:
        changed = COMMANDS[a.command](cfg, answers, a)
    except KeyboardInterrupt:
        print("\n  Přerušeno — nic se neuložilo.")
        return 130
    C.save_answers(answers, a.dry_run)
    if changed and not C.save(cfg, a.dry_run):
        return 1
    if a.command == "llm" and not a.manual and \
            (answers.get("generator") or {}).get("mode") == "manual":
        return 3   # generátor se nepodařilo nastavit — install.sh nabídne jinou cestu
    return 0


if __name__ == "__main__":
    sys.exit(main())
