#!/usr/bin/env python3
"""Přesun METOD velké třídy do přimíchané třídy (mixinu) v novém modulu — beze změny chování.

ROZDELENI_METOD_V1 (9. 10.) — pár k `tools/rozdel_prikazy.py` pro soubory, kde je
skoro všechno jedna třída (`openwebui_direct_handler.py`, `hans_routine.py`).

Co dělá s každou přesouvanou metodou:
  • text metody (i s dekorátorem a komentářem těsně nad ní) přenese do třídy
    `<Mixin>` v novém modulu BEZ přepisu — stejné odsazení, komentáře i formátování;
  • jména z úrovně původního modulu (konstanty, vzory, logger, pomocné funkce)
    dostanou předponu `<alias>.` → čtou se z původního modulu až při volání;
  • původní třída dostane `<Mixin>` mezi předky, takže `self.metoda(...)`,
    `Trida.metoda` i dědění fungují dál; nad třídou přibude import mixinu.

Co kontroluje, než cokoli zapíše (jinak skončí chybou a nic nezmění):
  1. metoda nemá `global`, `super()` bez argumentů, `__class__`, atribut se dvěma
     podtržítky na začátku (přejmenoval by se podle jména třídy), jiný dekorátor
     než staticmethod / classmethod / property, ani výchozí hodnotu parametru,
     která by sahala na jméno původního modulu už při importu;
  2. předponu dostane jen jméno, které je ve VŠECH vnořených rozsazích metody globální;
  3. zpětná zkouška: po odstranění předpon je strom nové metody totožný s původním;
  4. v novém modulu nezbylo globální jméno, které není vestavěné, importované ani alias;
  5. místní proměnné (včetně vnořených funkcí) jsou u staré a nové verze stejné;
  6. žádné jméno metody není po přesunu definované dvakrát (třída × mixiny).

Použití:
    python3 tools/rozdel_metody.py [--alias _h] <modul.py> <Trida> <novy_modul.py> <Mixin> \\
        <metoda…> [--prefix _sy_ …] [--zapis]
`--prefix X` přidá všechny metody třídy, jejichž jméno začíná X. Bez `--zapis` jen ověří.
"""
import ast
import builtins
import os
import re
import symtable
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rozdel_prikazy as rp  # noqa: E402

ZNACKA = "ROZDELENI_METOD_V1"
POVOLENE_DEKORATORY = {"staticmethod", "classmethod", "property"}


def _chyba(msg: str):
    sys.exit("ODMÍTNUTO: " + msg)


class _BezPredpony(ast.NodeTransformer):
    def __init__(self, alias):
        self.alias = alias

    def visit_Attribute(self, node):
        self.generic_visit(node)
        if isinstance(node.value, ast.Name) and node.value.id == self.alias:
            return ast.copy_location(ast.Name(id=node.attr, ctx=node.ctx), node)
        return node


def _metody(trida: ast.ClassDef) -> dict:
    return {n.name: n for n in trida.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


def priprav(zdroj_cesta, trida_jm, cil_cesta, mixin, jmena, alias) -> dict:
    src = open(zdroj_cesta, encoding="utf-8").read()
    radky = src.split("\n")
    strom = ast.parse(src)
    vse, importy = rp._jmena_modulu(strom)
    stab = symtable.symtable(src, zdroj_cesta, "exec")
    modul = os.path.splitext(os.path.basename(zdroj_cesta))[0]
    cil_modul = os.path.splitext(os.path.basename(cil_cesta))[0]
    tridy = [n for n in strom.body if isinstance(n, ast.ClassDef) and n.name == trida_jm]
    if len(tridy) != 1:
        _chyba("třída %s na úrovni modulu není (nebo je víckrát)" % trida_jm)
    trida = tridy[0]
    ctab = [t for t in stab.get_children()
            if t.get_name() == trida_jm and t.get_type() == "class"]
    if len(ctab) != 1:
        _chyba("nenašel jsem tabulku symbolů třídy %s" % trida_jm)
    ctab = ctab[0]
    met = _metody(trida)
    kandidati = vse - set(importy)
    bloky, potrebne_importy, zprava = [], set(), []
    for jm in jmena:
        f = met.get(jm)
        if f is None:
            _chyba("metoda %s ve třídě %s není" % (jm, trida_jm))
        for d in f.decorator_list:
            if not (isinstance(d, ast.Name) and d.id in POVOLENE_DEKORATORY):
                _chyba("%s má nepovolený dekorátor %s" % (jm, ast.unparse(d)))
        for x in ast.walk(f):
            if isinstance(x, ast.Global):
                _chyba("%s používá global" % jm)
            if isinstance(x, ast.Call) and isinstance(x.func, ast.Name) and x.func.id == "super":
                _chyba("%s volá super()" % jm)
            if isinstance(x, ast.Name) and x.id == "__class__":
                _chyba("%s sahá na __class__" % jm)
            if isinstance(x, ast.Attribute) and x.attr.startswith("__") and not x.attr.endswith("__"):
                _chyba("%s: atribut %s by se přejmenoval podle jména třídy" % (jm, x.attr))
        for d in f.args.defaults + [k for k in f.args.kw_defaults if k is not None]:
            for x in ast.walk(d):
                if isinstance(x, ast.Name) and x.id in vse:
                    _chyba("%s: výchozí hodnota parametru sahá na %s už při importu" % (jm, x.id))
        ftab = rp._tabulka_funkce(ctab, jm, f.lineno)
        if ftab is None:
            _chyba("%s: nenašel jsem tabulku symbolů" % jm)
        glob, sporna = rp._globalni_vsude(ftab, kandidati)
        if sporna:
            _chyba("%s: jméno %s je někde globální a jinde místní" % (jm, sorted(sporna)))
        glob_imp, _ = rp._globalni_vsude(ftab, set(importy))
        potrebne_importy |= glob_imp
        for x in ast.walk(f):
            anot = []
            if isinstance(x, ast.arg) and x.annotation is not None:
                anot.append(x.annotation)
            elif isinstance(x, (ast.FunctionDef, ast.AsyncFunctionDef)) and x.returns is not None:
                anot.append(x.returns)
            elif isinstance(x, ast.AnnAssign):
                anot.append(x.annotation)
            for a in anot:
                for y in ast.walk(a):
                    if isinstance(y, ast.Name):
                        if y.id in importy:
                            potrebne_importy.add(y.id)
                        elif y.id in kandidati:
                            _chyba("%s: typová poznámka používá jméno modulu %s" % (jm, y.id))
        mista = []
        for x in ast.walk(f):
            if isinstance(x, ast.Name) and x.id in glob:
                if not isinstance(x.ctx, ast.Load):
                    _chyba("%s: zápis do globálního jména %s" % (jm, x.id))
                mista.append((x.lineno, x.col_offset))
        # blok = komentář těsně nad metodou (stejné odsazení) + dekorátory + metoda
        od = min([f.lineno] + [d.lineno for d in f.decorator_list])
        odsaz = re.match(r"\s*", radky[od - 1]).group(0)
        while od - 2 >= 0 and radky[od - 2].startswith(odsaz + "#"):
            od -= 1
        blok = radky[od - 1:f.end_lineno]
        for ln, col in sorted(mista, reverse=True):
            i = ln - od
            b = blok[i].encode("utf-8")
            blok[i] = (b[:col] + (alias + ".").encode() + b[col:]).decode("utf-8")
        novy_text = "\n".join(blok)
        # 3. zpětná zkouška (metoda zabalená do pomocné třídy kvůli odsazení)
        obal = ast.parse("class _X:\n" + novy_text).body[0].body
        nf = [n for n in obal if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        if len(nf) != 1 or ast.dump(_BezPredpony(alias).visit(nf[0])) != ast.dump(f):
            _chyba("%s: zpětná zkouška stromu nesedí" % jm)
        # 5. místní proměnné
        stary_txt = "class _X:\n" + "\n".join(radky[od - 1:f.end_lineno])
        if rp._mistni(compile(stary_txt, "<stary>", "exec")) != \
                rp._mistni(compile("class _X:\n" + novy_text, "<novy>", "exec")):
            _chyba("%s: liší se místní proměnné" % jm)
        bloky.append((jm, od, f.end_lineno, novy_text, len(mista), sorted(glob)))
        zprava.append("  %-28s ř. %d–%d (%d ř.), předpon %d" % (
            jm, od, f.end_lineno, f.end_lineno - od + 1, len(mista)))
    # nový modul
    pata = ["", "", "# %s — až na konci, viz hlavička" % ZNACKA,
            "from scripts import %s as %s  # noqa: E402" % (modul, alias), ""]
    telo = "\n\n".join(b[3] for b in bloky)
    if os.path.exists(cil_cesta):
        stary_cil = open(cil_cesta, encoding="utf-8").read()
        if "\n".join(pata[2:4]) not in stary_cil:
            _chyba("cílový modul nemá na konci očekávaný import původního modulu")
        if "class %s:" % mixin not in stary_cil:
            _chyba("cílový modul existuje, ale nemá třídu %s" % mixin)
        if stary_cil.count("\nclass ") != 1:
            _chyba("cílový modul má víc tříd — přidávat umím jen do jediné")
        stary_cil = stary_cil.replace("\n".join(pata[2:4]), "").rstrip("\n")
        for jm in sorted(potrebne_importy):
            if ast.unparse(importy[jm]) not in stary_cil:
                _chyba("cílový modul už existuje a chybí mu import %s — doplň ručně" % jm)
        novy_cil = stary_cil + "\n\n" + telo + "\n".join(pata)
    else:
        hlava = ['"""Metody třídy `%s` přesunuté z `scripts/%s.py` (%s).' % (trida_jm, modul, ZNACKA), "",
                 "Text metod je beze změny; jména původního modulu se čtou přes `%s.` až při" % alias,
                 "volání. `%s` má třídu `%s` mezi předky, takže volání přes `self` platí dál." % (trida_jm, mixin),
                 "Import původního modulu je na KONCI souboru (kruhový import oběma směry).",
                 '"""', "from __future__ import annotations", ""]
        for jm in sorted(potrebne_importy):
            radek_imp = ast.unparse(importy[jm])
            if radek_imp not in hlava:
                hlava.append(radek_imp)
        novy_cil = ("\n".join(hlava) + "\n\n\nclass %s:\n" % mixin
                    + '    """Část třídy `%s` — viz hlavička modulu."""\n\n' % trida_jm
                    + telo + "\n".join(pata))
    # 4. v novém modulu nesmí zbýt cizí globální jméno
    ntab = symtable.symtable(novy_cil, cil_cesta, "exec")
    nvse, _n = rp._jmena_modulu(ast.parse(novy_cil))
    povolena = set(dir(builtins)) | nvse | {alias, "__name__", "__file__", "__doc__"}
    for t in rp._rozsahy(ntab)[1:]:
        if t.get_type() == "class":
            continue
        for s in t.get_symbols():
            if s.is_global() and s.is_referenced() and s.get_name() not in povolena:
                _chyba("v novém modulu zbylo neznámé globální jméno %s (rozsah %s)"
                       % (s.get_name(), t.get_name()))
    # původní modul: bloky pryč, mixin mezi předky, import nad třídou
    novy_zdroj = list(radky)
    for jm, od, do, _t, _p, _g in sorted(bloky, key=lambda b: -b[1]):
        konec = do
        if konec < len(novy_zdroj) and not novy_zdroj[konec].strip():
            konec += 1                      # i jeden prázdný řádek za metodou
        del novy_zdroj[od - 1:konec]
    ci = next(i for i, l in enumerate(novy_zdroj)
              if re.match(r"class %s\b" % re.escape(trida_jm), l))
    m = re.match(r"class %s\s*(\((.*)\))?\s*:(.*)$" % re.escape(trida_jm), novy_zdroj[ci])
    if not m:
        _chyba("řádek s definicí třídy %s neumím přepsat (víc řádků?)" % trida_jm)
    predci = [p.strip() for p in (m.group(2) or "").split(",") if p.strip()]
    if mixin not in predci:
        predci.append(mixin)
    novy_zdroj[ci] = "class %s(%s):%s" % (trida_jm, ", ".join(predci), m.group(3))
    imp = "from scripts.%s import %s   # %s — část třídy %s" % (cil_modul, mixin, ZNACKA, trida_jm)
    if imp not in novy_zdroj:
        kam = ci
        while kam - 1 >= 0 and (novy_zdroj[kam - 1].startswith("#") or novy_zdroj[kam - 1].startswith("@")):
            kam -= 1
        novy_zdroj.insert(kam, imp)
    novy_zdroj_txt = "\n".join(novy_zdroj)
    # 6. žádná metoda dvakrát
    zbyle = set(_metody([n for n in ast.parse(novy_zdroj_txt).body
                         if isinstance(n, ast.ClassDef) and n.name == trida_jm][0]))
    v_mixinu = set(_metody([n for n in ast.parse(novy_cil).body
                            if isinstance(n, ast.ClassDef) and n.name == mixin][0]))
    if zbyle & v_mixinu:
        _chyba("metody definované dvakrát: %s" % sorted(zbyle & v_mixinu))
    if not set(jmena) <= v_mixinu:
        _chyba("v mixinu chybí %s" % sorted(set(jmena) - v_mixinu))
    return {"zdroj": novy_zdroj_txt, "cil": novy_cil, "zprava": zprava,
            "ubylo": len(radky) - len(novy_zdroj), "cil_radku": novy_cil.count("\n")}


def main() -> int:
    arg = [a for a in sys.argv[1:] if a != "--zapis"]
    alias, prefixy = "_h", []
    while "--alias" in arg:
        i = arg.index("--alias")
        alias = arg[i + 1]
        del arg[i:i + 2]
    while "--prefix" in arg:
        i = arg.index("--prefix")
        prefixy.append(arg[i + 1])
        del arg[i:i + 2]
    if len(arg) < 4:
        print(__doc__)
        return 2
    zdroj, trida_jm, cil, mixin, jmena = arg[0], arg[1], arg[2], arg[3], list(arg[4:])
    if prefixy:
        strom = ast.parse(open(zdroj, encoding="utf-8").read())
        tr = [n for n in strom.body if isinstance(n, ast.ClassDef) and n.name == trida_jm][0]
        for n in tr.body:
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                    and any(n.name.startswith(p) for p in prefixy) and n.name not in jmena:
                jmena.append(n.name)
    if not jmena:
        _chyba("žádná metoda k přesunu")
    v = priprav(zdroj, trida_jm, cil, mixin, jmena, alias)
    compile(v["zdroj"], zdroj, "exec")
    compile(v["cil"], cil, "exec")
    print("\n".join(v["zprava"]))
    print("metod %d · původní modul −%d ř., nový modul %d ř." % (len(jmena), v["ubylo"], v["cil_radku"]))
    if "--zapis" in sys.argv:
        open(cil, "w", encoding="utf-8").write(v["cil"])
        open(zdroj, "w", encoding="utf-8").write(v["zdroj"])
        print("ZAPSÁNO")
    else:
        print("(nezapsáno — bez --zapis)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
