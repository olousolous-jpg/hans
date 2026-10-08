#!/usr/bin/env python3
"""Ověření po přesunu funkcí z libovolného modulu: stará verze (z gitu) × nová.

ROZDELENI_PRIKAZU_V1 (8. 10.) — obecný pár k `tools/rozdel_prikazy.py`
(`rozdel_overeni.py` je šitý na registr příkazů v chat_commands).

Porovnává:
  1. jména na úrovni modulu — nic nesmí zmizet,
  2. výsledky vyjmenovaných funkcí na vzorku reálných vět (deník + přepisy
     tazatele): táž věta, tatáž databáze, stará i nová verze hned po sobě.

Stará verze se dočasně uloží VEDLE původního modulu (`scripts/_stary_<modul>_tmp.py`),
aby jí seděly cesty odvozené z umístění souboru; po běhu se smaže. (První
verze ji dávala do dočasné složky a funkce čtoucí databázi v ní padaly.)

Funkce, které by mohly volat model (ve zdrojáku „ollama“, „generate(“,
„_summarize“), se přeskakují — běh je bez modelu a smí běžet i při herním módu.

Použití:
    python3 tools/rozdel_overeni_modul.py hans_recall funkce1 funkce2 … [--vet 150] [--proti HEAD]
"""
import ast
import datetime
import glob
import importlib
import inspect
import json
import os
import random
import re
import sqlite3
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)
DB = os.path.join(ROOT, "data", "hans_diary.db")
TEXT = ("question", "user_text", "text", "query", "msg", "message", "veta", "topic", "title", "s")
PEVNY_CAS = datetime.datetime(2026, 10, 8, 12, 0, 0)


def _vety(n: int) -> list:
    v = set()
    c = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)
    for (note,) in c.execute("SELECT note FROM diary WHERE event_type='human_chat'"):
        m = re.match(r"\s*([^:\n]{1,20}):\s*(.+)", (note or "").split("\nHans:")[0], re.S)
        if m:
            v.add(m.group(2).strip())
    for f in glob.glob("data/mereni/rozhovory/*.jsonl"):
        for l in open(f, encoding="utf-8"):
            try:
                v.add(json.loads(l)["veta"])
            except Exception:
                pass
    v = sorted(v)
    random.Random(8).shuffle(v)
    return v[:n]


def _argumenty(fn, veta: str, config: dict):
    """Slovník argumentů podle jmen parametrů, nebo None (funkci neumím zavolat)."""
    kw = {}
    for p in inspect.signature(fn).parameters.values():
        if p.name == "db_path":
            kw[p.name] = DB
        elif p.name in TEXT:
            kw[p.name] = veta
        elif p.name == "config":
            kw[p.name] = config
        elif p.name in ("asker", "person", "name"):
            kw[p.name] = "zkouška"
        elif p.name in ("now", "dt_now"):
            kw[p.name] = PEVNY_CAS
        elif p.default is not inspect.Parameter.empty:
            continue
        else:
            return None
    return kw


def _kanon(x):
    """Množiny seřadit (jejich pořadí při výpisu není dané), zbytek nechat."""
    if isinstance(x, (set, frozenset)):
        return ["<množina>"] + sorted((_kanon(i) for i in x), key=repr)
    if isinstance(x, dict):
        return {k: _kanon(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return type(x)(_kanon(i) for i in x)
    return x


def _zavolej(fn, kw):
    try:
        return repr(_kanon(fn(**kw)))
    except Exception as e:
        return "VÝJIMKA %s: %s" % (type(e).__name__, str(e)[:120])


def main() -> int:
    arg = sys.argv[1:]
    rev, n_vet = "HEAD", 150
    for flag in ("--proti", "--vet"):
        if flag in arg:
            i = arg.index(flag)
            if flag == "--proti":
                rev = arg[i + 1]
            else:
                n_vet = int(arg[i + 1])
            del arg[i:i + 2]
    modul, funkce = arg[0], arg[1:]
    src = subprocess.run(["git", "show", "%s:scripts/%s.py" % (rev, modul)],
                         capture_output=True, text=True, check=True).stdout
    tmp = os.path.join(ROOT, "scripts", "_stary_%s_tmp.py" % modul)
    open(tmp, "w", encoding="utf-8").write(src)
    ok = True
    try:
        stary = importlib.import_module("scripts._stary_%s_tmp" % modul)
        novy = importlib.import_module("scripts.%s" % modul)
        chybi = sorted(set(x for x in dir(stary) if not x.startswith("__")) - set(dir(novy)))
        print("1. jména modulu: starý %d, nový %d, chybí %s" % (len(dir(stary)), len(dir(novy)), chybi or "nic"))
        ok &= not chybi
        from scripts import config_io
        config = config_io.load()
        vety = _vety(n_vet)
        strom = {n.name: n for n in ast.parse(src).body if isinstance(n, ast.FunctionDef)}
        radky = src.split("\n")
        volani = shod = 0
        preskoceno, bez_textu = [], []
        for jm in funkce:
            f_s, f_n = getattr(stary, jm), getattr(novy, jm)
            telo = "\n".join(radky[strom[jm].lineno - 1:strom[jm].end_lineno]) if jm in strom else ""
            if re.search(r"ollama|generate\(|_summarize|requests\.|urlopen", telo):
                preskoceno.append(jm + " (model/síť)")
                continue
            if _argumenty(f_s, "x", config) is None:
                preskoceno.append(jm + " (neznámé parametry)")
                continue
            ma_text = any(p in TEXT for p in inspect.signature(f_s).parameters)
            vzorek = vety if ma_text else vety[:1]
            if not ma_text:
                bez_textu.append(jm)
            ruzne = 0
            for v in vzorek:
                a = _zavolej(f_s, _argumenty(f_s, v, config))
                b = _zavolej(f_n, _argumenty(f_n, v, config))
                volani += 1
                if a == b:
                    shod += 1
                else:
                    ruzne += 1
                    if ruzne <= 2:
                        print("   ROZDÍL %s(%r):\n     starý: %s\n     nový:  %s" % (jm, v[:50], a[:160], b[:160]))
            ok &= ruzne == 0
        print("2. volání: %d, shodných %d · funkcí porovnáno %d z %d" % (
            volani, shod, len(funkce) - len(preskoceno), len(funkce)))
        if preskoceno:
            print("   přeskočeno: " + ", ".join(preskoceno))
    finally:
        try:
            os.remove(tmp)
        except Exception:
            pass
    print("VÝSLEDEK:", "V POŘÁDKU" if ok else "ROZDÍLY")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
