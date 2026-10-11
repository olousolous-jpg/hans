#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Oddělení POPISU od MECHANIKY: registrace příkazů a seznam agentních akcí
do vlastních souborů (PREPIS_ROZHODOVANI, fáze 2, 11. 10.).

Čistě přesun textu, pořadí zůstává (pořadí registrace = priorita příkazů):
  scripts/chat_commands.py  → 69× `register(...)`  → scripts/chat_registr.py
  scripts/hans_agent.py     → `ACTIONS = {...}`    → scripts/hans_agent_akce.py
Nové soubory nic neimportují: popis je ve funkci, která dostane původní modul
a jména z něj bere jako `_cc.jmeno` / `_ag.jmeno` — žádný kruhový import.

Použití:
  python3 tools/oddel_registr.py otisk <soubor.json>   # stav registru a akcí
  python3 tools/oddel_registr.py [--zapis]             # bez --zapis jen výpis
"""
import ast
import builtins
import io
import json
import os
import sys
import tokenize

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def otisk(cil: str):
    """Úplný stav registru příkazů a akcí — před přesunem a po něm musí být
    soubory totožné."""
    sys.path.insert(0, ROOT)
    os.chdir(ROOT)
    from scripts import chat_commands as CC, hans_agent as AG

    def hod(v):
        if callable(v):
            return "<%s.%s>" % (getattr(v, "__module__", "?"), getattr(v, "__name__", "?"))
        if isinstance(v, (list, tuple)):
            return [hod(x) for x in v]
        if isinstance(v, dict):
            return {str(k): hod(x) for k, x in v.items()}
        return getattr(v, "pattern", v) if not isinstance(v, (str, int, float, bool, type(None))) else v
    d = {"prikazy": [[c, hod({k: ([p.pattern for p in v] if k in ("nl", "nl_fold") else v)
                                 for k, v in s.items()})] for c, s in CC._COMMANDS.items()],
         "akce": [[a, hod(vars(x))] for a, x in AG.ACTIONS.items()],
         "katalog": "\n".join("- %s: %s Argumenty: %s." % (a.id, a.desc, a.args or "žádné")
                              for a in AG.ACTIONS.values())}
    json.dump(d, open(cil, "w", encoding="utf-8"), ensure_ascii=False, indent=1, default=str)
    print("otisk: %d příkazů, %d akcí → %s" % (len(d["prikazy"]), len(d["akce"]), cil))


def _blok(src: str, uzel, alias: str) -> str:
    """Zdroják uzlu se jmény modulu přepsanými na `alias.jmeno`, odsazený o 4."""
    radky = src.split("\n")[uzel.lineno - 1:uzel.end_lineno]
    text = "\n".join(radky)
    for t in tokenize.generate_tokens(io.StringIO(text + "\n").readline):
        if t.type == tokenize.STRING and t.start[0] != t.end[0]:
            sys.exit("ABORT: víceřádkový řetězec na ř. %d — odsazení by ho změnilo"
                     % (uzel.lineno + t.start[0] - 1))
    mista = sorted(((n.lineno - uzel.lineno, n.col_offset) for n in ast.walk(uzel)
                    if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
                    and not hasattr(builtins, n.id)), reverse=True)
    for r, c in mista:   # zprava doleva, ať se sloupce neposunou
        b = radky[r].encode("utf-8")   # col_offset je v bajtech
        radky[r] = (b[:c] + (alias + ".").encode() + b[c:]).decode("utf-8")
    return "\n".join(("    " + x) if x.strip() else x for x in radky)


def main(zapis: bool):
    pc = os.path.join(ROOT, "scripts/chat_commands.py")
    pa = os.path.join(ROOT, "scripts/hans_agent.py")
    sc, sa = open(pc, encoding="utf-8").read(), open(pa, encoding="utf-8").read()
    if "chat_registr" in sc or "hans_agent_akce" in sa:
        sys.exit("už je odděleno")

    # ── příkazy ──────────────────────────────────────────────────────────────
    reg = [n for n in ast.parse(sc).body if isinstance(n, ast.Expr)
           and isinstance(n.value, ast.Call) and getattr(n.value.func, "id", "") == "register"]
    bloky = [_blok(sc, n, "_cc") for n in reg]
    novy_c = ('"""Popis chatových příkazů: lomítkové názvy, vzory vět, obsluha a nápověda.\n\n'
              'Odděleno z `scripts/chat_commands.py` (PREPIS_ROZHODOVANI, fáze 2). Tady je\n'
              'jen POPIS; mechanika (`register`, `parse_command`, `dispatch`) zůstala tam.\n'
              '⚠️ POŘADÍ JE PRIORITA: věta dostane první příkaz, jehož vzor na ni sedne.\n'
              'Nový příkaz se zapisuje sem, na místo podle priority. Jména z původního\n'
              'modulu se píší `_cc.jmeno`; soubor sám nic neimportuje.\n"""\n\n\n'
              'def zaregistruj(_cc):\n' + "\n\n".join(bloky) + "\n")
    L = sc.split("\n")
    for n in reversed(reg):
        del L[n.lineno - 1:n.end_lineno]
    zbytek = "\n".join(L).rstrip("\n") + (
        "\n\n\n# PREPIS_ROZHODOVANI (fáze 2, 11. 10.) — popis příkazů (lomítka, vzory,\n"
        "# obsluha, nápověda) je v `scripts/chat_registr.py`. Volá se až TADY na konci,\n"
        "# kdy už existují všechny obsluhy i vzory, na které se popis odkazuje.\n"
        "import sys as _sys\n"
        "from scripts.chat_registr import zaregistruj as _zaregistruj\n"
        "_zaregistruj(_sys.modules[__name__])\n")

    # ── akce ─────────────────────────────────────────────────────────────────
    akce = [n for n in ast.parse(sa).body if isinstance(n, ast.AnnAssign)
            and getattr(n.target, "id", "") == "ACTIONS"]
    assert len(akce) == 1, len(akce)
    u = akce[0]
    telo = _blok(sa, u.value, "_ag")
    prvni = sa.split("\n")[u.lineno - 1]
    assert prvni == "ACTIONS: dict[str, Action] = {", prvni
    telo = "    return {" + telo.split("\n", 1)[1] if telo.split("\n")[0].strip().endswith("{") else None
    assert telo
    novy_a = ('"""Seznam agentních akcí: popis pro router, nápovědná slova, argumenty,\n'
              'obsluha, potvrzení a odstup.\n\n'
              'Odděleno z `scripts/hans_agent.py` (PREPIS_ROZHODOVANI, fáze 2). Tady je jen\n'
              'POPIS; router, pravidla a potvrzování zůstaly tam.\n'
              '⚠️ Popis akce jde doslova do zadání routeru — jeho změna je zásah do\n'
              'rozhodování a chce přeměřit. Jména z původního modulu se píší `_ag.jmeno`;\n'
              'soubor sám nic neimportuje.\n"""\n\n\n'
              'def postav_akce(_ag):\n' + telo + "\n")
    LA = sa.split("\n")
    LA[u.lineno - 1:u.end_lineno] = [
        "# PREPIS_ROZHODOVANI (fáze 2, 11. 10.) — seznam akcí je v",
        "# `scripts/hans_agent_akce.py`; staví se tady, kde už existují všechny obsluhy.",
        "import sys as _sys",
        "from scripts.hans_agent_akce import postav_akce as _postav_akce",
        "ACTIONS: dict[str, Action] = _postav_akce(_sys.modules[__name__])",
    ]
    print("příkazů %d (%d řádků) · akce: %d řádků" % (
        len(reg), novy_c.count("\n"), novy_a.count("\n")))
    print("chat_commands.py %d → %d ř. · hans_agent.py %d → %d ř." % (
        sc.count("\n"), zbytek.count("\n"), sa.count("\n"), len(LA) - 1))
    for kod, jm in ((novy_c, "chat_registr"), (novy_a, "hans_agent_akce"),
                    (zbytek, "chat_commands"), ("\n".join(LA), "hans_agent")):
        compile(kod, jm, "exec")
    if not zapis:
        print("(nezapsáno — přidej --zapis)")
        return
    open(os.path.join(ROOT, "scripts/chat_registr.py"), "w", encoding="utf-8").write(novy_c)
    open(os.path.join(ROOT, "scripts/hans_agent_akce.py"), "w", encoding="utf-8").write(novy_a)
    open(pc, "w", encoding="utf-8").write(zbytek)
    open(pa, "w", encoding="utf-8").write("\n".join(LA))
    print("zapsáno")


if __name__ == "__main__":
    a = sys.argv[1:]
    if a and a[0] == "otisk":
        otisk(a[1])
    else:
        main("--zapis" in a)
