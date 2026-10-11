#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Mapa rozhodovacích míst — KDO VŠECHNO se o větu hlásí.

PREPIS_ROZHODOVANI (fáze 1, 11. 10.). Z BĚŽÍCÍHO kódu (ne z poznámek) vytáhne
všechna místa, která rozhodují, co se s větou stane, v pořadí priority:
kroky handleru, registr příkazů, agentní akce, cesty podkladu a pravidlové
predikáty. Na korpusu reálných vět pak spočítá, o které věty se hlásí víc
vrstev a které predikáty odpovídají skoro stejně (kandidáti na sloučení).

Chování Hanse nemění. Běží v téže izolované kopii jako snímek rozhodnutí
(`tools/snimek_rozhodnuti.py`) — bez sítě, procesů a modelu.

Použití:
  python3 tools/mapa_rozhodovani.py [--snimek zaklad_a]
Výstupy: data/mereni/snimek_rozhodnuti/mapa.md, mapa.json
"""
import ast
import collections
import inspect
import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import snimek_rozhodnuti as SR  # noqa: E402

# jméno vypadá jako otázka ano/ne nad větou
_PREDIKAT = re.compile(
    r"^_?(?:je_|is_|asks?_|looks_like|ma_|zminuje|soft_|capability_|dotaz_|"
    r"mentions_|navazuje|film_bez|thread_film)|_question$|_ask$|_talk$|"
    r"_about_past$|^_hint_match$|^_actionable$")


def beh(snimek: str):
    if not os.path.isdir(SR.VZOR):
        sys.exit("chybí kopie — nejdřív `tools/snimek_rozhodnuti.py priprav`")
    if os.path.exists(SR.BEH):
        shutil.rmtree(SR.BEH)
    subprocess.run(["cp", "-a", SR.VZOR, SR.BEH], check=True)
    for f in ("snimek_rozhodnuti.py", "mapa_rozhodovani.py"):
        shutil.copy2(os.path.join(ROOT, "tools", f), os.path.join(SR.BEH, "tools", f))
    shutil.copy2(os.path.join(SR.DIR, "vety.json"), os.path.join(SR.BEH, "vety.json"))
    env = dict(os.environ, HOME=os.path.join(SR.BEH, "home"), PYTHONDONTWRITEBYTECODE="1")
    with open(os.path.join(SR.DIR, "mapa.log"), "w", encoding="utf-8") as lg:
        r = subprocess.run(["nice", "-n", "10", sys.executable, "-u",
                            "tools/mapa_rozhodovani.py", "_pracuj"],
                           cwd=SR.BEH, env=env, stdout=lg, stderr=subprocess.STDOUT)
    src = os.path.join(SR.BEH, "mapa.json")
    if r.returncode or not os.path.exists(src):
        sys.exit("sběr selhal (návrat %d) — viz %s" % (r.returncode,
                                                      os.path.join(SR.DIR, "mapa.log")))
    shutil.copy2(src, os.path.join(SR.DIR, "mapa.json"))
    zprava(snimek)


# ── pracovní proces (UVNITŘ kopie) ───────────────────────────────────────────
def _volani(fn) -> list:
    """Jména všeho, co funkce volá (v pořadí prvního výskytu)."""
    try:
        strom = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    except Exception:
        return []
    out = []
    for n in ast.walk(strom):
        if isinstance(n, ast.Call):
            f = n.func
            j = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
            if j and j not in out:
                out.append(j)
    return out


def _vzoru(fn) -> tuple:
    """(počet regexových dotazů, počet větvení `if`) v těle funkce — míra
    toho, kolik se rozhoduje přímo v kroku, mimo pojmenované predikáty."""
    try:
        strom = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    except Exception:
        return (0, 0)
    rx = sum(1 for n in ast.walk(strom) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Attribute)
             and n.func.attr in ("search", "match", "fullmatch", "findall", "finditer"))
    return (rx, sum(1 for n in ast.walk(strom) if isinstance(n, ast.If)))


def _kroky(fn, predpona: str) -> list:
    """Kroky `self.<predpona>*` v pořadí, jak je funkce volá; `konci` = krok
    smí odpovědět a ukončit zpracování (výsledek se přiřazuje)."""
    strom = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    out = []
    for n in ast.walk(strom):
        if isinstance(n, (ast.Assign, ast.Expr)) and isinstance(n.value, ast.Call):
            f = n.value.func
            if isinstance(f, ast.Attribute) and f.attr.startswith(predpona):
                out.append((n.lineno, f.attr, isinstance(n, ast.Assign)))
    return [(j, k) for _, j, k in sorted(out)]


def _pracuj():
    import tempfile
    tady = os.path.realpath(os.getcwd())
    assert os.path.basename(tady) == "sandbox_beh", tady
    sys.path.insert(0, tady)
    SR._hlidac((tady + os.sep, os.path.realpath(tempfile.gettempdir()) + os.sep,
                "/dev/", "/proc/"))
    from scripts import config_io
    cfg = config_io.load()
    from scripts import ollama_client as OC
    OC._post_with_retry = lambda *a, **k: None
    OC.game_mode_on = lambda *a, **k: False
    OC.game_pause_on = lambda *a, **k: False
    from scripts import chat_commands as CC
    from scripts import hans_agent as AG
    from scripts.test_rozhovor import postav_handler
    h = postav_handler(cfg)
    ag = AG.AgentRouter(cfg)

    # predikáty: funkce modulů, které provoz opravdu načetl, + metody routeru
    pred = {}
    for mj, m in sorted(sys.modules.items()):
        if not mj.startswith("scripts.") or m is None:
            continue
        for j, f in vars(m).items():
            if not (inspect.isfunction(f) and f.__module__ == mj and _PREDIKAT.search(j)):
                continue
            try:
                par = list(inspect.signature(f).parameters.values())
            except Exception:
                continue
            povinne = [p for p in par if p.default is p.empty
                       and p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
            if len(povinne) == 1 and par and par[0] is povinne[0]:
                pred["%s.%s" % (mj.split(".", 1)[1], j)] = f
    for j in dir(ag):
        f = getattr(ag, j)
        if inspect.ismethod(f) and _PREDIKAT.search(j):
            par = [p for p in inspect.signature(f).parameters.values()
                   if p.default is p.empty]
            if len(par) == 1:
                pred["hans_agent.AgentRouter.%s" % j] = f

    typ = type(h)
    kroky = _kroky(typ.send_chat_message, "_sc_")
    gr_fn = next(f for j, f in inspect.getmembers(typ, inspect.isfunction)
                 if any(k.startswith("_gr_") for k in _volani(f))
                 and not j.startswith("_gr_"))
    cesty = _kroky(gr_fn, "_gr_")
    kratke = {k.rsplit(".", 1)[1] for k in pred}

    def popis(jm):
        f = getattr(typ, jm)
        src = inspect.getsource(f)
        rx, ifs = _vzoru(f)
        return {"jmeno": jm, "radku": src.count("\n"), "regexu": rx, "vetveni": ifs,
                "modul": f.__module__.split(".")[-1],
                "predikaty": [v for v in _volani(f) if v in kratke],
                "cesty": re.findall(r"_vysledek_groundingu\('(\w+)', '(\w+)'\)", src)}

    mapa = {
        "kroky": [dict(popis(j), konci=k) for j, k in kroky],
        "podklad_vstup": gr_fn.__name__,
        "podklad": [dict(popis(j), konci=k) for j, k in cesty],
        "prikazy": [{"id": c, "vzoru": len(s["nl"]), "lomitek": len(s["slash"])}
                    for c, s in CC._COMMANDS.items()],
        "akce": [{"id": a, "napoved": len(x.hints),
                  "potvrzeni": bool(getattr(x, "needs_confirm", False))}
                 for a, x in AG.ACTIONS.items()],
        "predikaty": sorted(pred),
    }
    # výsledky podkladu hlášené z pomocných metod (mimo kroky `_gr_*`)
    hotove = {j for j, _ in cesty}   # vstupní funkce sama pokračuje za kroky
    mapa["podklad_pomocne"] = [
        popis(j) for j, f in inspect.getmembers(typ, inspect.isfunction)
        if j not in hotove and j != "_vysledek_groundingu"
        and "_vysledek_groundingu('" in inspect.getsource(f)]

    vety = json.load(open("vety.json", encoding="utf-8"))
    radky, chyby, cas = [], collections.Counter(), collections.Counter()
    for v in vety:
        fold = CC._fold_diacritics(v.strip())
        sedi = []
        if not v.strip().startswith("/"):
            for cid, s in CC._COMMANDS.items():
                if any(p.search(v.strip()) for p in s["nl"]) or \
                        any(p.search(fold) for p in s.get("nl_fold", [])):
                    sedi.append(cid)
        try:
            pc = CC.parse_command(v)
        except Exception:
            pc = None
        ano = []
        for k, f in pred.items():
            t0 = time.time()
            try:
                if f(v):
                    ano.append(k)
            except Exception as e:
                chyby[k + ": " + type(e).__name__] += 1
            cas[k] += time.time() - t0
        radky.append({"veta": v, "sedi": sedi, "parse": pc[0] if pc else None,
                      "ano": ano})
    mapa["vety"] = radky
    mapa["chyby"] = chyby.most_common(40)
    mapa["cas"] = [(k, round(s, 1)) for k, s in cas.most_common(8)]
    json.dump(mapa, open("mapa.json", "w", encoding="utf-8"), ensure_ascii=False)
    print("HOTOVO: kroků %d, cest %d, příkazů %d, akcí %d, predikátů %d, vět %d"
          % (len(kroky), len(cesty), len(mapa["prikazy"]), len(mapa["akce"]),
             len(pred), len(vety)))


# ── zpráva ───────────────────────────────────────────────────────────────────
def zprava(snimek: str):
    M = json.load(open(os.path.join(SR.DIR, "mapa.json"), encoding="utf-8"))
    S = {}
    p = os.path.join(SR.DIR, snimek + ".jsonl")
    if os.path.exists(p):
        for l in open(p, encoding="utf-8"):
            d = json.loads(l)
            if d["kdo"] == "z":
                S[d["veta"]] = d
    V = M["vety"]
    n = len(V)
    o = []
    w = o.append
    w("# Mapa rozhodovacích míst (%s, vět %d, snímek `%s`)\n"
      % (time.strftime("%-d. %-m. %Y %H:%M"), n, snimek))

    rozhodl = collections.Counter((S.get(r["veta"]) or {}).get("krok") or "model" for r in V)
    w("## 1. Kroky handleru v pořadí priority\n")
    w("| # | krok | smí ukončit | řádků | regexů přímo v kroku | větvení | rozhodl u vět | pojmenované predikáty uvnitř |")
    w("|---|---|---|---|---|---|---|---|")
    for i, k in enumerate(M["kroky"], 1):
        w("| %d | `%s` | %s | %d | %d | %d | %s | %s |" % (
            i, k["jmeno"], "ano" if k["konci"] else "ne", k["radku"],
            k["regexu"], k["vetveni"],
            rozhodl.get(k["jmeno"], 0) if k["konci"] else "–",
            ", ".join("`%s`" % x for x in k["predikaty"]) or "–"))
    w("\nK odpovědi modelu došlo %d vět.\n" % rozhodl.get("model", 0))

    gr = collections.Counter()
    for r in V:
        for g in (S.get(r["veta"]) or {}).get("gr") or []:
            gr[g.split("<")[1]] += 1
    w("## 2. Cesty podkladu v pořadí (vstup `%s`)\n" % M["podklad_vstup"])
    w("| # | krok | řádků | regexů | větvení | výsledky, které umí vrátit (kolikrát ve snímku) | predikáty uvnitř |")
    w("|---|---|---|---|---|---|---|")
    for i, k in list(enumerate(M["podklad"], 1)) + [("pomocná", k) for k in M["podklad_pomocne"]]:
        w("| %s | `%s` | %d | %d | %d | %s | %s |" % (
            i, k["jmeno"], k["radku"], k["regexu"], k["vetveni"],
            ", ".join("%s←%s (%d)" % (a, b, gr.get(b, 0)) for a, b in k["cesty"]) or "–",
            ", ".join("`%s`" % x for x in k["predikaty"]) or "–"))

    w("\n## 3. Příkazy: věty, na které sedí vzor víc příkazů\n")
    vic = [r for r in V if len(r["sedi"]) > 1]
    jeden = sum(1 for r in V if len(r["sedi"]) == 1)
    w("Vzor aspoň jednoho příkazu sedí na %d vět z %d; právě jednoho na %d, "
      "víc příkazů na **%d** (rozhoduje pořadí registrace).\n"
      % (jeden + len(vic), n, jeden, len(vic)))
    pary = collections.Counter()
    priklad = {}
    for r in vic:
        for dalsi in r["sedi"][1:]:
            k = (r["sedi"][0], dalsi)
            pary[k] += 1
            priklad.setdefault(k, r["veta"])
    w("| vyhrál | sedí také | vět | příklad |")
    w("|---|---|---|---|")
    for (a, b), c in pary.most_common(25):
        w("| `%s` | `%s` | %d | %s |" % (a, b, c, priklad[(a, b)][:70].replace("|", "/")))
    nikdy = [c["id"] for c in M["prikazy"] if c["vzoru"]
             and not any(c["id"] in r["sedi"] for r in V)]
    w("\nPříkazy se vzory, které na korpusu nesedly ani jednou (%d): %s\n"
      % (len(nikdy), ", ".join("`%s`" % x for x in nikdy) or "–"))
    jinak = [r for r in V if r["sedi"] and r["parse"] != r["sedi"][0]]
    w("Vzor sedí, ale `parse_command` vrátil něco jiného (brzdy a přesměrování "
      "uvnitř parseru): %d vět.\n" % len(jinak))
    nerozh = [r for r in V if r["parse"] and S.get(r["veta"])
              and S[r["veta"]]["krok"] != "_sc_prikazy"]
    w("`parse_command` příkaz našel, ale o větě nakonec rozhodl jiný krok nebo "
      "model: **%d** vět. Podle příkazu: %s\n" % (
          len(nerozh), ", ".join("`%s` %d" % x for x in collections.Counter(
              r["parse"] for r in nerozh).most_common(12))))

    bez = [r for r in V if (S.get(r["veta"]) or {}).get("krok") == "_sc_prikazy"
           and not r["parse"]]
    w("O větě rozhodl krok příkazů, ačkoli `parse_command` nic nenašel (navázání "
      "na vlákno a další větve uvnitř kroku): **%d** vět; z toho s lomítkem %d.\n"
      % (len(bez), sum(1 for r in bez if r["veta"].strip().startswith("/"))))
    w("## 4. Kolik vrstev se o větu hlásí\n")
    A = "hans_agent.AgentRouter._hint_match"
    R = "hans_agent.AgentRouter._actionable"
    tab = collections.Counter()
    for r in V:
        s = S.get(r["veta"]) or {}
        g = (s.get("gr") or ["-"])[-1]
        tab[(bool(r["sedi"]), A in r["ano"],
             g.startswith("grounded") or g.startswith("opinion"))] += 1
    w("| vzor příkazu | nápověda agentní akce | cesta podkladu s oporou | vět |")
    w("|---|---|---|---|")
    for k, c in sorted(tab.items(), key=lambda x: -x[1]):
        w("| %s | %s | %s | %d |" % tuple(["ano" if x else "–" for x in k] + [c]))
    w("\nVíc než jedna vrstva: **%d** vět z %d. Brána agenta (`_actionable` = "
      "nápověda NEBO tvar otázky/žádosti) pouští %d vět, samotná nápověda %d.\n"
      % (sum(c for k, c in tab.items() if sum(k) > 1), n,
         sum(1 for r in V if R in r["ano"]), sum(1 for r in V if A in r["ano"])))
    kp = [r for r in V if (S.get(r["veta"]) or {}).get("krok") == "_sc_prikazy"
          and A in r["ano"]]
    w("Rozhodl příkaz a zároveň sedí nápověda agentní akce: %d vět.\n" % len(kp))

    w("## 5. Pravidlové predikáty nad holou větou (%d)\n" % len(M["predikaty"]))
    kolik = collections.Counter(k for r in V for k in r["ano"])
    mn = collections.defaultdict(set)
    for i, r in enumerate(V):
        for k in r["ano"]:
            mn[k].add(i)
    w("| predikát | ano u vět |")
    w("|---|---|")
    for k in sorted(M["predikaty"], key=lambda x: -kolik[x]):
        w("| `%s` | %d |" % (k, kolik[k]))
    w("\n### Dvojice, které odpovídají skoro stejně (kandidáti na sloučení)\n")
    w("| A | B | A ano | B ano | oba | shoda |")
    w("|---|---|---|---|---|---|")
    dv = []
    ks = [k for k in M["predikaty"] if kolik[k] >= 5]
    for i, a in enumerate(ks):
        for b in ks[i + 1:]:
            spol = len(mn[a] & mn[b])
            if spol >= 5:
                dv.append((spol / len(mn[a] | mn[b]), a, b, spol))
    for j, a, b, spol in sorted(dv, reverse=True)[:30]:
        if j < 0.4:
            break
        w("| `%s` | `%s` | %d | %d | %d | %.2f |" % (a, b, kolik[a], kolik[b], spol, j))
    w("\n### Podmnožiny (B říká ano skoro vždy, když A)\n")
    w("| A (užší) | B (širší) | A ano | B ano | A i B |")
    w("|---|---|---|---|---|")
    pod = []
    for a in ks:
        for b in ks:
            if a != b and kolik[a] < kolik[b] <= n * 0.6:
                spol = len(mn[a] & mn[b])
                if spol >= 0.9 * kolik[a]:
                    pod.append((kolik[a], a, b, spol))
    for c, a, b, spol in sorted(pod, reverse=True)[:25]:
        w("| `%s` | `%s` | %d | %d | %d |" % (a, b, c, kolik[b], spol))
    if M.get("chyby"):
        w("\nPredikáty, které při volání s holou větou spadly: %s\n"
          % ", ".join("%s ×%d" % (k, c) for k, c in M["chyby"]))
    open(os.path.join(SR.DIR, "mapa.md"), "w", encoding="utf-8").write("\n".join(o) + "\n")
    print("zapsáno: %s (%d řádků)" % (os.path.join(SR.DIR, "mapa.md"), len(o)))


if __name__ == "__main__":
    a = sys.argv[1:]
    sn = a[a.index("--snimek") + 1] if "--snimek" in a else "zaklad_a"
    if a and a[0] == "_pracuj":
        _pracuj()
    elif a and a[0] == "zprava":
        zprava(sn)
    else:
        beh(sn)
