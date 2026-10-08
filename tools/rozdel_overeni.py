#!/usr/bin/env python3
"""Ověření po přesunu funkcí: stará verze modulu (z gitu) × nová (pracovní strom).

ROZDELENI_PRIKAZU_V1 (8. 10.) — pár k `tools/rozdel_prikazy.py`. Bez modelu a bez
sítě; smí běžet i při herním módu. Porovnává:
  1. jména na úrovni modulu (nic nesmí zmizet — odjinud se importují),
  2. registr příkazů: pořadí, lomítkové názvy, vzory, nápověda, jméno obsluhy,
  3. `parse_command` na všech reálných větách z deníku a přepisů tazatele,
  4. výstup vybraných obsluh na stejných vstupech (jen ty, které nic nemění).

Použití:
    python3 tools/rozdel_overeni.py [--proti HEAD] [--obsluhy "_cmd_x:argument" ...]
"""
import glob
import importlib.util
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)


def _stary_modul(rev: str):
    src = subprocess.run(["git", "show", "%s:scripts/chat_commands.py" % rev],
                         capture_output=True, text=True, check=True).stdout
    d = tempfile.mkdtemp(prefix="rozdel_")
    p = os.path.join(d, "chat_commands_stary.py")
    open(p, "w", encoding="utf-8").write(src)
    spec = importlib.util.spec_from_file_location("chat_commands_stary", p)
    m = importlib.util.module_from_spec(spec)
    sys.modules["chat_commands_stary"] = m
    spec.loader.exec_module(m)
    return m


def _registr(m) -> list:
    out = []
    for cid, s in m._COMMANDS.items():
        out.append((cid, tuple(s["slash"]), tuple((p.pattern, p.flags) for p in s["nl"]),
                    tuple((p.pattern, p.flags) for p in s["nl_fold"]),
                    s["help"], getattr(s["handler"], "__name__", "?")))
    return out


def _vety() -> list:
    v = set()
    c = sqlite3.connect("file:data/hans_diary.db?mode=ro", uri=True)
    for (n,) in c.execute("SELECT note FROM diary WHERE event_type='human_chat'"):
        m = re.match(r"\s*([^:\n]{1,20}):\s*(.+)", (n or "").split("\nHans:")[0], re.S)
        if m:
            v.add(m.group(2).strip())
    for f in glob.glob("data/mereni/rozhovory/*.jsonl"):
        for l in open(f, encoding="utf-8"):
            try:
                v.add(json.loads(l)["veta"])
            except Exception:
                pass
    try:
        d = json.load(open("data/regrese_cases.json", encoding="utf-8"))
        for p in d["pripady"]:
            if p.get("funkce", "").endswith("parse_command") and p.get("argumenty"):
                v.add(str(p["argumenty"][0]))
    except Exception:
        pass
    return sorted(v)


def main() -> int:
    arg = sys.argv[1:]
    rev = arg[arg.index("--proti") + 1] if "--proti" in arg else "HEAD"
    obsluhy = arg[arg.index("--obsluhy") + 1:] if "--obsluhy" in arg else []
    stary = _stary_modul(rev)
    from scripts import chat_commands as novy
    ok = True

    chybi = sorted(set(n for n in dir(stary) if not n.startswith("__")) - set(dir(novy)))
    print("1. jména modulu: starý %d, nový %d, chybí %s" % (
        len(dir(stary)), len(dir(novy)), chybi or "nic"))
    ok &= not chybi

    rs, rn = _registr(stary), _registr(novy)
    print("2. registr: %d příkazů, shodný %s" % (len(rn), rs == rn))
    if rs != rn:
        ok = False
        for a, b in zip(rs, rn):
            if a != b:
                print("   ROZDÍL u", a[0], "×", b[0])
                break

    vety = _vety()
    ruzne = [(v, stary.parse_command(v), novy.parse_command(v)) for v in vety
             if stary.parse_command(v) != novy.parse_command(v)]
    print("3. parse_command: %d vět, rozdílů %d" % (len(vety), len(ruzne)))
    for r in ruzne[:5]:
        print("   ", r)
    ok &= not ruzne

    n = sh = 0
    for o in obsluhy:
        jm, _, a = o.partition(":")
        try:
            x = getattr(stary, jm)(None, "zkouška", a)
        except Exception as e:
            x = "VÝJIMKA %s: %s" % (type(e).__name__, e)
        try:
            y = getattr(novy, jm)(None, "zkouška", a)
        except Exception as e:
            y = "VÝJIMKA %s: %s" % (type(e).__name__, e)
        n += 1
        sh += x == y
        if x != y:
            ok = False
            print("   ROZDÍL %s(%r):\n     starý: %s\n     nový:  %s" % (jm, a, str(x)[:200], str(y)[:200]))
        else:
            print("   = %s(%r) → %s" % (jm, a, str(x)[:90].replace("\n", " ")))
    print("4. obsluhy: %d volání, shodných %d" % (n, sh))
    print("VÝSLEDEK:", "V POŘÁDKU" if ok else "ROZDÍLY — nezapisovat / vrátit")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
