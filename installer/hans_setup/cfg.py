"""Načtení a zápis Hansova configu.

Config je rozdělený na veřejný `config.json` (v gitu) a `config.private.json`
(jména, tokeny, IP). Dělicí čáru zná jen `scripts/config_io` — tady se jen
používá, nic se v něm nemění.

Na novém zařízení `config.private.json` neexistuje a vzor
`config.private.example.json` je ANONYMIZOVANÝ: URL mají port `0000`,
uživatelská jména jsou `uzivatel`, UUID kolekcí nuly, a některé texty
(`greeting.user_prompt`) jsou nahrazené zástupným slovem. Kdyby se vzor
jen zkopíroval, Hans by dostal nesmyslné hodnoty, které přebijí rozumné
výchozí hodnoty v kódu (např. prázdná cesta k ArcFace HEF). Proto se ze
vzoru vezme jen STRUKTURA a zástupné hodnoty se zahodí.
"""
from __future__ import annotations

import copy
import json
import shutil
import sys
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any

INSTALLER = Path(__file__).resolve().parent.parent
ROOT = INSTALLER.parent
DATA = ROOT / "data" / "installer"          # data/ je v .gitignore
PUBLIC = ROOT / "config.json"
PRIVATE = ROOT / "config.private.json"
EXAMPLE = ROOT / "config.private.example.json"
ANSWERS = DATA / "answers.json"

# Sekce, které ve vzoru popisují CIZÍ (ukázkovou) domácnost — na novém
# zařízení se začíná s prázdnou a vyplní ji průvodce.
_HOUSEHOLD = ("known_persons", "relationship_seed", "person_name_forms")

_PLACEHOLDER_VALUES = {"", "uzivatel", "00000000-0000-0000-0000-000000000000",
                       "00:00:00:00:00:00"}


def config_io():
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from scripts import config_io as cio   # jen čtení API, kód se nemění
    return cio


def _is_placeholder(v: Any) -> bool:
    if isinstance(v, str):
        return v in _PLACEHOLDER_VALUES or ":0000" in v
    return False


def _strip(d: dict) -> dict:
    out = OrderedDict()
    for k, v in d.items():
        if k.startswith("_"):
            continue
        if isinstance(v, dict):
            v = _strip(v)
            if not v:
                continue
        elif _is_placeholder(v):
            continue
        out[k] = v
    return out


def fresh_private_from_example() -> dict:
    """Privátní část pro čistou instalaci: vzor bez zástupných hodnot."""
    if not EXAMPLE.exists():
        return OrderedDict()
    ex = json.loads(EXAMPLE.read_text(encoding="utf-8"), object_pairs_hook=OrderedDict)
    for sec in _HOUSEHOLD:
        ex.pop(sec, None)
    return _strip(ex)


def load(dry_run: bool = False, force_fresh: bool = False) -> tuple[dict, bool]:
    """Vrátí (sloučený config, je_to_čistá_instalace).

    V dry-run se navazuje na předchozí dry-run kroky (data/installer/dryrun/),
    ať jde celý průvodce projít bez zápisu do skutečného configu.
    force_fresh = chovej se jako na novém zařízení (ignoruj config.private.json)."""
    if not PUBLIC.exists():
        raise SystemExit("Nenalezen %s — spouštěj instalátor z Hansova repozitáře." % PUBLIC)
    dr = DATA / "dryrun"
    pub_path = dr / "config.json" if dry_run and (dr / "config.json").exists() else PUBLIC
    priv_path = dr / "config.private.json" if pub_path != PUBLIC else PRIVATE
    pub = json.loads(pub_path.read_text(encoding="utf-8"), object_pairs_hook=OrderedDict)
    if priv_path.exists() and not (force_fresh and priv_path == PRIVATE):
        priv = json.loads(priv_path.read_text(encoding="utf-8"), object_pairs_hook=OrderedDict)
        fresh = False
    else:
        priv = fresh_private_from_example()
        fresh = True
    merged = config_io()._slouc(pub, priv)
    for sec in _HOUSEHOLD:
        merged.setdefault(sec, OrderedDict())
    return merged, fresh


def get(cfg: dict, path: str, default=None):
    d = cfg
    for k in path.split("."):
        if not isinstance(d, dict) or k not in d:
            return default
        d = d[k]
    return d


def set_(cfg: dict, path: str, value) -> None:
    keys = path.split(".")
    d = cfg
    for k in keys[:-1]:
        if not isinstance(d.get(k), dict):
            d[k] = OrderedDict()
        d = d[k]
    d[keys[-1]] = value


def replace_values(cfg: dict, old: str, new: str) -> list[str]:
    """Nahradí všude hodnotu `old` hodnotou `new` (přesná shoda řetězce).
    Vrací seznam změněných cest."""
    changed = []

    def walk(d, pref=""):
        for k, v in d.items():
            p = pref + k
            if isinstance(v, dict):
                walk(v, p + ".")
            elif isinstance(v, str) and v == old:
                d[k] = new
                changed.append(p)
    walk(cfg)
    return changed


def save(cfg: dict, dry_run: bool) -> bool:
    """Zapíše config rozděleně přes config_io. V dry-run jen do data/installer/dryrun/."""
    cio = config_io()
    if dry_run:
        out = DATA / "dryrun"
        out.mkdir(parents=True, exist_ok=True)
        ver, priv = cio.rozdel(copy.deepcopy(cfg))
        bad = cio.zkontroluj_verejny(ver, priv)
        (out / "config.json").write_text(
            json.dumps(ver, indent=4, ensure_ascii=False) + "\n", encoding="utf-8")
        (out / "config.private.json").write_text(
            json.dumps(priv, indent=4, ensure_ascii=False) + "\n", encoding="utf-8")
        print("  [dry-run] config zapsán jen do %s (skutečný config netknutý)" % out)
        if bad:
            print("  [dry-run] ⚠ strážce by zápis odmítl: %s" % "; ".join(bad[:5]))
            return False
        return True
    stamp = time.strftime("%Y%m%d-%H%M%S")
    bak = DATA / ("backup-" + stamp)
    bak.mkdir(parents=True, exist_ok=True)
    for f in (PUBLIC, PRIVATE):
        if f.exists():
            shutil.copy2(f, bak / f.name)
    if not cio.save(cfg, root=ROOT):
        print("  ✗ Zápis configu odmítnut (config_io: ve veřejné části zůstalo něco citlivého).")
        print("    Záloha původního stavu: %s" % bak)
        return False
    print("  ✓ config uložen (záloha předchozího stavu: %s)" % bak.relative_to(ROOT))
    return True


def load_answers(dry_run: bool = False) -> dict:
    path = ANSWERS
    if dry_run and (DATA / "dryrun" / "answers.json").exists():
        path = DATA / "dryrun" / "answers.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_answers(ans: dict, dry_run: bool) -> None:
    path = (DATA / "dryrun" / "answers.json") if dry_run else ANSWERS
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(ans, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
