#!/usr/bin/env python3
"""Ověření po přesunu metod do mixinů: stará třída (z gitu) × nová třída za běhu.

ROZDELENI_METOD_V1 (9. 10.) — nezávislá kontrola k `tools/rozdel_metody.py`:
čte soubory tak, jak leží na disku, ne stav nástroje.

Porovnává:
  1. množinu metod: nic nesmí zmizet ani přibýt, žádná není definovaná dvakrát;
  2. každou metodu zvlášť: strom (AST) metody nalezené ZA BĚHU přes třídu
     (tedy i z mixinu) je po odstranění předpony aliasu totožný se starým;
  3. každé jméno čtené přes alias v původním modulu existuje;
  4. ostatní obsah třídy (atributy v těle) a zbytek modulu se nezměnil
     (kromě řádku `class`, importů mixinů a přesunutých metod).

Použití:
    python3 tools/rozdel_overeni_metody.py <modul> <Trida> <alias> [--proti HEAD]
"""
import ast
import importlib
import inspect
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)


class _BezPredpony(ast.NodeTransformer):
    def __init__(self, alias):
        self.alias = alias

    def visit_Attribute(self, node):
        self.generic_visit(node)
        if isinstance(node.value, ast.Name) and node.value.id == self.alias:
            return ast.copy_location(ast.Name(id=node.attr, ctx=node.ctx), node)
        return node


def _trida(strom, jm):
    return [n for n in strom.body if isinstance(n, ast.ClassDef) and n.name == jm][0]


def _metody(tr):
    return {n.name: n for n in tr.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


def main() -> int:
    arg = list(sys.argv[1:])
    proti = "HEAD"
    if "--proti" in arg:
        i = arg.index("--proti")
        proti = arg[i + 1]
        del arg[i:i + 2]
    modul, trida_jm, alias = arg[0], arg[1], arg[2]
    stary_src = subprocess.run(["git", "show", "%s:scripts/%s.py" % (proti, modul)],
                               capture_output=True, text=True, check=True).stdout
    stary = ast.parse(stary_src)
    st_tr = _trida(stary, trida_jm)
    st_met = _metody(st_tr)
    m = importlib.import_module("scripts." + modul)
    cls = getattr(m, trida_jm)
    chyby = []
    # 1. množina metod + dvojí definice
    kde = {}
    for k in cls.__mro__:
        if k is object:
            continue
        for jm, v in vars(k).items():
            f = v.__func__ if isinstance(v, (staticmethod, classmethod)) else (v.fget if isinstance(v, property) else v)
            if inspect.isfunction(f):
                kde.setdefault(jm, []).append((k, f))
    nove = set(kde)
    if nove != set(st_met):
        chyby.append("množina metod se liší: chybí %s, navíc %s"
                     % (sorted(set(st_met) - nove), sorted(nove - set(st_met))))
    dvakrat = sorted(j for j, v in kde.items() if len(v) > 1)
    if dvakrat:
        chyby.append("definováno dvakrát: %s" % dvakrat)
    # 2. každá metoda: strom shodný po odstranění předpony
    presunuto, zdrojaky = 0, {}
    for jm, st in st_met.items():
        if jm not in kde:
            continue
        k, f = kde[jm][0]
        soubor = inspect.getsourcefile(f)
        if soubor not in zdrojaky:
            zdrojaky[soubor] = ast.parse(open(soubor, encoding="utf-8").read())
        kandid = [n for c in zdrojaky[soubor].body if isinstance(c, ast.ClassDef) and c.name == k.__name__
                  for n in c.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == jm]
        if len(kandid) != 1:
            chyby.append("%s: ve zdrojáku %s nenalezena právě jednou" % (jm, os.path.basename(soubor)))
            continue
        novy = kandid[0]
        if k is not cls:
            presunuto += 1
            novy = _BezPredpony(alias).visit(ast.parse(ast.unparse(novy)).body[0])
            st = ast.parse(ast.unparse(st)).body[0]
        if ast.dump(novy) != ast.dump(st):
            chyby.append("%s: strom se liší (%s)" % (jm, k.__name__))
    # 3. jména přes alias existují
    chybi = set()
    for soubor, strom in zdrojaky.items():
        if os.path.basename(soubor) == modul + ".py":
            continue
        for n in ast.walk(strom):
            if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == alias \
                    and not hasattr(m, n.attr):
                chybi.add(n.attr)
    if chybi:
        chyby.append("jména přes %s. v modulu chybí: %s" % (alias, sorted(chybi)))
    # 4. zbytek třídy a modulu
    novy_src = open(os.path.join("scripts", modul + ".py"), encoding="utf-8").read()
    novy = ast.parse(novy_src)
    no_tr = _trida(novy, trida_jm)
    ostatni = lambda tr: [ast.dump(n) for n in tr.body
                          if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    if ostatni(st_tr) != ostatni(no_tr):
        chyby.append("tělo třídy mimo metody se změnilo")
    mixin_moduly = {k.__module__.split(".")[-1] for k in cls.__mro__[1:] if k is not object}

    def zbytek(strom):
        out = []
        for n in strom.body:
            if isinstance(n, ast.ClassDef) and n.name == trida_jm:
                continue
            if isinstance(n, ast.ImportFrom) and (n.module or "").split(".")[-1] in mixin_moduly:
                continue
            out.append(ast.dump(n))
        return out
    if zbytek(stary) != zbytek(novy):
        chyby.append("zbytek modulu (mimo třídu a importy mixinů) se změnil")
    print("metod %d (stará třída %d) · přesunuto do mixinů %d · předci: %s"
          % (len(nove), len(st_met), presunuto, [k.__name__ for k in cls.__mro__[1:-1]]))
    for c in chyby:
        print("CHYBA:", c)
    print("VÝSLEDEK:", "V POŘÁDKU" if not chyby else "NESEDÍ")
    return 0 if not chyby else 1


if __name__ == "__main__":
    sys.exit(main())
