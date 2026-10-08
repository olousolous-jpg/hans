#!/usr/bin/env python3
"""Přesun funkcí z velkého modulu do menšího — beze změny chování.

ROZDELENI_PRIKAZU_V1 (8. 10.) — nástroj k dělení `scripts/chat_commands.py`
(7 459 ř.) po skupinách obsluh příkazů. Navazuje na `HANS_HANDLER_SPLIT_V1`
(29.–30. 9.), kde se dělily funkce uvnitř jednoho souboru; tehdejší nástroje
ležely v dočasné složce a nepřežily, proto je tenhle v repu.

Co dělá s každou přesouvanou funkcí:
  • text funkce (i s komentářem těsně nad ní) přenese do nového modulu BEZ
    přepisu — zůstávají komentáře i formátování;
  • jména z úrovně původního modulu (pomocné funkce, vzory, logger, jiné
    obsluhy) dostanou předponu `_cc.` → čtou se z původního modulu až při
    volání. Logger tedy zůstává tentýž a kdo přepíše atribut původního
    modulu, ovlivní i přesunutou funkci;
  • na původním místě zůstane `from scripts.<nový> import <jméno>`, takže
    registrace příkazů, pořadí i všechny dosavadní importy odjinud platí dál.

Co kontroluje, než cokoli zapíše (jinak skončí chybou a nic nezmění):
  1. funkce nemá `global`/`nonlocal`, dekorátor ani výchozí hodnotu parametru,
     která by sahala na jméno původního modulu už při importu;
  2. předponu dostane jen jméno, které je ve VŠECH vnořených rozsazích funkce
     globální (jinak odmítne — ruční práce);
  3. zpětná zkouška: po odstranění předpon je strom nové funkce totožný se
     stromem původní;
  4. v novém modulu nezbylo globální jméno, které není vestavěné, importované
     ani `_cc`;
  5. místní proměnné (včetně vnořených funkcí) jsou u staré a nové verze stejné.

Použití:
    python3 tools/rozdel_prikazy.py scripts/chat_commands.py scripts/chat_cmd_zpravy.py \\
        _cmd_zpravy _cmd_demagog [--zapis]
Bez `--zapis` jen ověří a vypíše, co by udělal.
"""
import ast
import builtins
import os
import symtable
import sys

ZNACKA = "ROZDELENI_PRIKAZU_V1"


def _chyba(msg: str):
    sys.exit("ODMÍTNUTO: " + msg)


def _jmena_modulu(strom: ast.Module) -> tuple:
    """(všechna jména vázaná na úrovni modulu, z nich jména z importů → řádek importu)."""
    vse, importy = set(), {}
    for n in strom.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            vse.add(n.name)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            if isinstance(n, ast.ImportFrom) and n.module == "__future__":
                continue
            # Import z vlastního projektu (hlavně už přesunuté obsluhy
            # `from scripts.chat_cmd_x import …`) se v novém modulu NEOPAKUJE —
            # bere se přes `_cc.` jako každé jiné jméno modulu. Opakování vyrobilo
            # 8. 10. kruhový import mezi dvěma novými moduly (zachytila zkouška
            # přímého importu, nic nenasazeno).
            vlastni = isinstance(n, ast.ImportFrom) and (n.module or "").startswith("scripts")
            for a in n.names:
                jm = (a.asname or a.name).split(".")[0]
                vse.add(jm)
                if not vlastni:
                    importy[jm] = n
        else:
            for x in ast.walk(n):
                if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Store):
                    vse.add(x.id)
    return vse, importy


def _tabulka_funkce(tab: symtable.SymbolTable, jmeno: str, radek: int):
    for ch in tab.get_children():
        if ch.get_name() == jmeno and ch.get_lineno() == radek:
            return ch
    return None


def _rozsahy(tab) -> list:
    out = [tab]
    for ch in tab.get_children():
        out.extend(_rozsahy(ch))
    return out


def _globalni_vsude(ftab, kandidati: set) -> tuple:
    """(jména z `kandidati` globální ve všech rozsazích funkce, jména sporná)."""
    glob, jinak = set(), set()
    for t in _rozsahy(ftab):
        for s in t.get_symbols():
            jm = s.get_name()
            if jm not in kandidati or not s.is_referenced() and not s.is_assigned():
                continue
            if s.is_global() and not s.is_declared_global():
                glob.add(jm)
            else:
                jinak.add(jm)
    return glob - jinak, glob & jinak


def _mistni(kod) -> list:
    out = [(kod.co_name, kod.co_varnames, kod.co_cellvars, kod.co_freevars)]
    for c in kod.co_consts:
        if hasattr(c, "co_varnames"):
            out.extend(_mistni(c))
    return out


ALIAS = "_cc"       # jméno, pod kterým nový modul vidí původní (lze změnit --alias)


class _BezPredpony(ast.NodeTransformer):
    def visit_Attribute(self, node):
        self.generic_visit(node)
        if isinstance(node.value, ast.Name) and node.value.id == ALIAS:
            return ast.copy_location(ast.Name(id=node.attr, ctx=node.ctx), node)
        return node


def priprav(zdroj_cesta: str, cil_cesta: str, jmena: list) -> dict:
    src = open(zdroj_cesta, encoding="utf-8").read()
    radky = src.split("\n")
    strom = ast.parse(src)
    vse, importy = _jmena_modulu(strom)
    stab = symtable.symtable(src, zdroj_cesta, "exec")
    modul = os.path.splitext(os.path.basename(zdroj_cesta))[0]
    cil_modul = os.path.splitext(os.path.basename(cil_cesta))[0]
    funkce = {n.name: n for n in strom.body if isinstance(n, ast.FunctionDef)}
    kandidati = vse - set(importy)
    bloky, potrebne_importy, zprava = [], set(), []
    for jm in jmena:
        f = funkce.get(jm)
        if f is None:
            _chyba("funkce %s na úrovni modulu není" % jm)
        if f.decorator_list:
            _chyba("%s má dekorátor" % jm)
        if any(isinstance(x, ast.Global) for x in ast.walk(f)):
            _chyba("%s používá global" % jm)      # nonlocal ve vnořené funkci nevadí
        for d in f.args.defaults + [k for k in f.args.kw_defaults if k is not None]:
            for x in ast.walk(d):
                if isinstance(x, ast.Name) and x.id in vse:
                    _chyba("%s: výchozí hodnota parametru sahá na %s už při importu" % (jm, x.id))
        ftab = _tabulka_funkce(stab, jm, f.lineno)
        if ftab is None:
            _chyba("%s: nenašel jsem tabulku symbolů" % jm)
        glob, sporna = _globalni_vsude(ftab, kandidati)
        if sporna:
            _chyba("%s: jméno %s je někde globální a jinde místní" % (jm, sorted(sporna)))
        # importovaná jména použitá ve funkci → zopakovat import v novém modulu
        glob_imp, _ = _globalni_vsude(ftab, set(importy))
        potrebne_importy |= glob_imp
        # Jména jen v typových poznámkách (`Optional[str]`) tabulka symbolů nevidí —
        # s odloženými anotacemi se za běhu nevyhodnocují. Import se přesto zopakuje,
        # ať je nový modul čistý; anotace jménem z původního modulu se odmítne.
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
        # místa k přepsání (řádek, sloupec v bajtech)
        mista = []
        for x in ast.walk(f):
            if isinstance(x, ast.Name) and x.id in glob:
                if not isinstance(x.ctx, ast.Load):
                    _chyba("%s: zápis do globálního jména %s" % (jm, x.id))
                mista.append((x.lineno, x.col_offset))
        # blok = komentář těsně nad funkcí + funkce
        od = f.lineno
        while od - 2 >= 0 and radky[od - 2].lstrip().startswith("#") and not radky[od - 2].startswith(" "):
            od -= 1
        blok = radky[od - 1:f.end_lineno]
        for ln, col in sorted(mista, reverse=True):
            i = ln - od
            b = blok[i].encode("utf-8")
            blok[i] = (b[:col] + (ALIAS + ".").encode() + b[col:]).decode("utf-8")
        novy_text = "\n".join(blok)
        # 3. zpětná zkouška
        nf = ast.parse(novy_text).body[0]
        if ast.dump(_BezPredpony().visit(nf)) != ast.dump(f):
            _chyba("%s: zpětná zkouška stromu nesedí" % jm)
        # 5. místní proměnné
        stary_kod = compile(ast.Module(body=[f], type_ignores=[]), "<stary>", "exec")
        novy_kod = compile(novy_text, "<novy>", "exec")
        if _mistni(stary_kod) != _mistni(novy_kod):
            _chyba("%s: liší se místní proměnné" % jm)
        bloky.append((jm, od, f.end_lineno, novy_text, len(mista), sorted(glob)))
        zprava.append("  %-22s ř. %d–%d (%d ř.), předpon %d, jména: %s" % (
            jm, od, f.end_lineno, f.end_lineno - od + 1, len(mista), ", ".join(sorted(glob)[:8])
            + (" …" if len(glob) > 8 else "")))
    # nový modul
    hlava = ['"""Funkce přesunuté z `scripts/%s.py` (%s).' % (modul, ZNACKA), "",
             "Text funkcí je beze změny; jména původního modulu se čtou přes `%s.` až při" % ALIAS,
             "volání. Původní modul si funkce bere zpět importem, takže dosavadní importy platí.",
             "Import původního modulu je na KONCI souboru (kruhový import oběma směry).",
             '"""', "from __future__ import annotations", ""]
    for jm in sorted(potrebne_importy):
        radek_imp = ast.unparse(importy[jm])
        if radek_imp not in hlava:
            hlava.append(radek_imp)
    # Import původního modulu je ZÁMĚRNĚ až na konci souboru: ať se importuje dřív
    # kterýkoli z obou modulů, funkce tady už existují, když si je původní modul
    # bere (8. 10.: s importem nahoře přímý import nového modulu spadl na kruh).
    pata = ["", "", "# %s — až na konci, viz hlavička" % ZNACKA,
            "from scripts import %s as %s  # noqa: E402" % (modul, ALIAS), ""]
    existuje = os.path.exists(cil_cesta)
    if existuje:
        stary_cil = open(cil_cesta, encoding="utf-8").read()
        if "\n".join(pata[2:4]) not in stary_cil:
            _chyba("cílový modul nemá na konci očekávaný import původního modulu")
        stary_cil = stary_cil.replace("\n".join(pata[2:4]), "").rstrip("\n")
        for jm in sorted(potrebne_importy):
            if ast.unparse(importy[jm]) not in stary_cil:
                _chyba("cílový modul už existuje a chybí mu import %s — doplň ručně" % jm)
        novy_cil = stary_cil + "\n\n\n" + "\n\n\n".join(b[3] for b in bloky) + "\n".join(pata) 
    else:
        novy_cil = "\n".join(hlava) + "\n\n" + "\n\n\n".join(b[3] for b in bloky) + "\n".join(pata)
    # 4. v novém modulu nesmí zbýt cizí globální jméno
    ntab = symtable.symtable(novy_cil, cil_cesta, "exec")
    nstrom = ast.parse(novy_cil)
    nvse, _nimp = _jmena_modulu(nstrom)
    povolena = set(dir(builtins)) | nvse | {ALIAS, "__name__", "__file__", "__doc__"}
    for t in _rozsahy(ntab)[1:]:
        for s in t.get_symbols():
            if s.is_global() and s.is_referenced() and s.get_name() not in povolena:
                _chyba("v novém modulu zbylo neznámé globální jméno %s (rozsah %s)"
                       % (s.get_name(), t.get_name()))
    # původní modul: bloky pryč, na jejich místě import
    novy_zdroj = list(radky)
    for jm, od, do, _t, _n, _g in sorted(bloky, key=lambda b: -b[1]):
        novy_zdroj[od - 1:do] = ["from scripts.%s import %s   # %s — přesunuto"
                                 % (cil_modul, jm, ZNACKA)]
    return {"zdroj": "\n".join(novy_zdroj), "cil": novy_cil, "zprava": zprava,
            "ubylo": len(radky) - len(novy_zdroj), "cil_radku": novy_cil.count("\n")}


def main() -> int:
    global ALIAS
    arg = [a for a in sys.argv[1:] if a != "--zapis"]
    if "--alias" in arg:
        i = arg.index("--alias")
        ALIAS = arg[i + 1]
        del arg[i:i + 2]
    if len(arg) < 3:
        print(__doc__)
        return 2
    zdroj, cil, jmena = arg[0], arg[1], arg[2:]
    v = priprav(zdroj, cil, jmena)
    compile(v["zdroj"], zdroj, "exec")
    compile(v["cil"], cil, "exec")
    print("\n".join(v["zprava"]))
    print("původní modul −%d ř., nový modul %d ř." % (v["ubylo"], v["cil_radku"]))
    if "--zapis" in sys.argv:
        open(cil, "w", encoding="utf-8").write(v["cil"])
        open(zdroj, "w", encoding="utf-8").write(v["zdroj"])
        print("ZAPSÁNO")
    else:
        print("(nezapsáno — bez --zapis)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
