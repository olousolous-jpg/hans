#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Snímek rozhodnutí — KDO o větě rozhodl, bez modelu, sítě a následků.

PREPIS_ROZHODOVANI (fáze 0, 11. 10.) — záchranná síť pro přestavbu rozhodovací
vrstvy. Každou reálnou větu pošle celou produkční cestou `send_chat_message`
v IZOLOVANÉ KOPII projektu a zapíše, který krok rozhodl: krok handleru, příkaz
a argumenty, cesta podkladu, zda došlo na router s modelem. Odpovědi modelu
se neměří — model je nahrazen pevnou větou.

Izolace (proč je to bezpečné pustit vedle běžícího Hanse):
  • běží nad kopií kódu a dat (`sandbox_beh/`), pracovní složka i HOME jsou tam,
  • hlídač (`sys.addaudithook`) odmítne každé síťové spojení, každý podproces
    a každý zápis nebo databázi mimo kopii — a zapíše, že se o to kód pokusil
    (to je zároveň seznam vět, které mají v provozu následky),
  • model, router příkazů a agentní router jsou nahrazeny záznamem „došlo to
    sem“.

Použití:
  python3 tools/snimek_rozhodnuti.py priprav            # čerstvá kopie (jednou)
  python3 tools/snimek_rozhodnuti.py beh <jmeno> [--vet N]
  python3 tools/snimek_rozhodnuti.py porovnej <a> <b>
Výstupy: data/mereni/snimek_rozhodnuti/<jmeno>.jsonl
"""
import glob
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIR = os.path.join(ROOT, "data", "mereni", "snimek_rozhodnuti")
VZOR = os.path.join(DIR, "sandbox_vzor")
BEH = os.path.join(DIR, "sandbox_beh")
CIZI = "Navsteva"

VYNECHAT = [
    ".git", "__pycache__", "archive", "installer", "docs", "tests",
    "data/mereni", "data/patch_snapshots", "data/backups", "data/models",
    "data/hans_art", "data/guard", "data/works", "data/harvest", "data/logs",
    "data/room_photos", "data/avatar", "data/gesta", "data/gesta_ne",
    "data/foto", "data/tts_cache", "data/entity_images", "data/conversations",
    "data/stopy", "data/n8n", "data/library", "data/kodi_addon",
    "data/recognition.log*", "data/system.log*", "data/*.db-wal",
    "data/*.db-shm", "data/.web_chat_*", "data/.ollama_paused",
]


# ── příprava kopie ───────────────────────────────────────────────────────────
def _vety() -> list:
    """Reálné věty lidí: deník rozhovorů + přepisy tazatele (jako
    tools/rozdel_overeni.py). Seřazeno, ať je pořadí stálé."""
    v = set()
    c = sqlite3.connect("file:%s?mode=ro" % os.path.join(ROOT, "data/hans_diary.db"),
                        uri=True)
    for (n,) in c.execute("SELECT note FROM diary WHERE event_type='human_chat'"):
        m = re.match(r"\s*([^:\n]{1,20}):\s*(.+)", (n or "").split("\nHans:")[0], re.S)
        if m:
            v.add(m.group(2).strip())
    c.close()
    for f in glob.glob(os.path.join(ROOT, "data/mereni/rozhovory/*.jsonl")):
        for l in open(f, encoding="utf-8"):
            try:
                d = json.loads(l)
            except Exception:
                continue
            for k in ("otazka", "q", "zprava", "message", "user"):
                if isinstance(d.get(k), str) and d[k].strip():
                    v.add(d[k].strip())
    return sorted(x for x in v if 0 < len(x) <= 600)


def priprav():
    os.makedirs(DIR, exist_ok=True)
    if os.path.exists(VZOR):
        shutil.rmtree(VZOR)
    cmd = ["rsync", "-a", "--exclude=*.pyc"]
    for x in VYNECHAT:
        cmd.append("--exclude=/%s" % x if "/" in x or x.startswith(".") else
                   "--exclude=%s" % x)
    cmd += [ROOT + "/", VZOR + "/"]
    subprocess.run(cmd, check=True)
    # databáze znovu přes zálohovací rozhraní (běžící Hans do nich píše)
    for db in glob.glob(os.path.join(ROOT, "data", "*.db")):
        cil = os.path.join(VZOR, "data", os.path.basename(db))
        if os.path.exists(cil):
            os.remove(cil)
        a = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
        b = sqlite3.connect(cil)
        a.backup(b)
        b.close(); a.close()
    os.makedirs(os.path.join(VZOR, "data", "conversations"), exist_ok=True)
    os.makedirs(os.path.join(VZOR, "home"), exist_ok=True)
    vety = _vety()
    json.dump(vety, open(os.path.join(DIR, "vety.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=0)
    vel = subprocess.run(["du", "-sh", VZOR], capture_output=True, text=True).stdout.split()[0]
    print("kopie hotová: %s (%s), vět %d" % (VZOR, vel, len(vety)))


def beh(jmeno: str, n_vet: int = 0):
    if not os.path.isdir(VZOR):
        sys.exit("chybí kopie — nejdřív `priprav`")
    if os.path.exists(BEH):
        shutil.rmtree(BEH)
    subprocess.run(["cp", "-a", VZOR, BEH], check=True)
    shutil.copy2(os.path.abspath(__file__), os.path.join(BEH, "tools", "snimek_rozhodnuti.py"))
    shutil.copy2(os.path.join(DIR, "vety.json"), os.path.join(BEH, "vety.json"))
    env = dict(os.environ, HOME=os.path.join(BEH, "home"), PYTHONDONTWRITEBYTECODE="1",
               SNIMEK_VET=str(n_vet))
    t0 = time.time()
    with open(os.path.join(DIR, jmeno + ".log"), "w", encoding="utf-8") as lg:
        r = subprocess.run(["nice", "-n", "10", sys.executable, "-u",
                            "tools/snimek_rozhodnuti.py", "_pracuj"],
                           cwd=BEH, env=env, stdout=lg, stderr=subprocess.STDOUT)
    src = os.path.join(BEH, "snimek.jsonl")
    if os.path.exists(src):
        shutil.copy2(src, os.path.join(DIR, jmeno + ".jsonl"))
    n = sum(1 for _ in open(os.path.join(DIR, jmeno + ".jsonl"), encoding="utf-8")) \
        if os.path.exists(os.path.join(DIR, jmeno + ".jsonl")) else 0
    print("běh %s: návrat %d, řádků %d, %.0f s" % (jmeno, r.returncode, n, time.time() - t0))


# ── pracovní proces (běží UVNITŘ kopie) ──────────────────────────────────────
_ZAZNAM = {"blok": []}
_VIDENO = set()


def _hlidac(povoleno: tuple):
    import os as _os

    def _uvnitr(p) -> bool:
        if isinstance(p, int):
            return True
        try:
            if isinstance(p, bytes):
                p = p.decode("utf-8", "replace")
            p = str(p)
            if p.startswith("file:"):
                p = p[5:].split("?")[0]
            if p in (":memory:", ""):
                return True
            rp = _os.path.realpath(p)
        except Exception:
            return False
        return (rp + _os.sep).startswith(povoleno)

    def hook(ev, args):
        if ev in ("socket.connect", "socket.getaddrinfo", "socket.bind",
                  "socket.sendto"):
            _ZAZNAM["blok"].append("síť")
            raise ConnectionRefusedError("snimek: síť zakázána")
        if ev in ("subprocess.Popen", "os.system", "os.exec", "os.posix_spawn",
                  "os.spawn", "os.fork", "os.forkpty"):
            try:
                co = args[0] if ev != "subprocess.Popen" else (args[0] or args[1])
                co = _os.path.basename(str(co[0] if isinstance(co, (list, tuple)) else co).split()[0])
            except Exception:
                co = "?"
            _ZAZNAM["blok"].append("proces:" + co)
            raise PermissionError("snimek: podproces zakázán")
        if ev == "open":
            p, mode, flags = args[0], args[1], args[2]
            pise = (isinstance(mode, str) and any(c in mode for c in "wax+")) or \
                   (isinstance(flags, int) and flags & (_os.O_WRONLY | _os.O_RDWR |
                                                        _os.O_CREAT | _os.O_TRUNC |
                                                        _os.O_APPEND))
            if pise and not _uvnitr(p):
                _ZAZNAM["blok"].append("zápis:" + str(p)[:60])
                if len(_VIDENO) < 40 and str(p) not in _VIDENO:
                    _VIDENO.add(str(p))
                    sys.__stdout__.write("ZAPIS_MIMO %r\n" % (p,))
                raise PermissionError("snimek: zápis mimo kopii")
        elif ev == "sqlite3.connect":
            if not _uvnitr(args[0]):
                _ZAZNAM["blok"].append("db:" + str(args[0])[:60])
                raise PermissionError("snimek: databáze mimo kopii")
        elif ev in ("os.remove", "os.rmdir", "os.mkdir", "os.truncate",
                    "os.chmod", "os.chown", "shutil.rmtree"):
            if not _uvnitr(args[0]):
                _ZAZNAM["blok"].append("zásah:" + str(args[0])[:60])
                raise PermissionError("snimek: zásah mimo kopii")
        elif ev in ("os.rename", "os.link", "os.symlink", "shutil.move",
                    "shutil.copyfile", "shutil.copytree"):
            if not _uvnitr(args[1]) or (ev in ("os.rename", "shutil.move")
                                        and not _uvnitr(args[0])):
                _ZAZNAM["blok"].append("zásah:" + str(args[1])[:60])
                raise PermissionError("snimek: zásah mimo kopii")
    sys.addaudithook(hook)


def _pracuj():
    import signal
    import tempfile
    tady = os.path.realpath(os.getcwd())
    assert os.path.basename(tady) == "sandbox_beh", tady
    sys.path.insert(0, tady)
    _hlidac((tady + os.sep, os.path.realpath(tempfile.gettempdir()) + os.sep,
             "/dev/", "/proc/"))
    import scripts
    assert os.path.realpath(list(scripts.__path__)[0]).startswith(tady), scripts.__path__

    from scripts import config_io
    cfg = config_io.load()
    znamy = next(iter(cfg.get("known_persons") or {}), None)
    assert znamy, "chybí known_persons"

    # model a routery s modelem → jen záznam
    from scripts import ollama_client as OC

    def _bez_modelu(url, payload, *a, **k):
        _ZAZNAM["ollama"].append(str((payload or {}).get("model"))[:30])
        return None
    OC._post_with_retry = _bez_modelu
    OC.game_mode_on = lambda *a, **k: False
    OC.game_pause_on = lambda *a, **k: False

    from scripts import chat_commands as CC
    _dispatch = CC.dispatch

    def dispatch(command, handler, name=None, **k):
        try:
            _ZAZNAM["prikaz"].append([str(command[0]), str(command[1])[:80]])
        except Exception:
            _ZAZNAM["prikaz"].append(["?", ""])
        return _dispatch(command, handler, name=name, **k)
    CC.dispatch = dispatch

    def resolve_command_llm(message, config, turns=None):
        _ZAZNAM["cmd_llm"] = True
        return None
    CC.resolve_command_llm = resolve_command_llm

    from scripts import hans_agent as AG

    def _route(self, handler, name, message):
        _ZAZNAM["agent_llm"] = True
        return None
    AG.AgentRouter._route = _route

    from scripts.test_rozhovor import postav_handler
    from scripts import openwebui_direct_handler as OH
    h = postav_handler(cfg)

    def _model(*a, **k):
        _ZAZNAM["model"] += 1
        return "Toto je zkušební odpověď modelu."
    h._stream_message = _model
    h._send_message = _model

    for jm in [x for x in dir(h) if x.startswith("_sc_")]:
        puv = getattr(h, jm)

        def obal(ctx, _p=puv, _j=jm):
            r = _p(ctx)
            if r is not OH._POKRACUJ and _ZAZNAM["krok"] is None and _j not in _BEZ_NAVRATU:
                _ZAZNAM["krok"] = _j
            return r
        setattr(h, jm, obal)
    _gr = h._vysledek_groundingu

    def gr(vysledek, cesta):
        _ZAZNAM["gr"].append("%s<%s" % (vysledek, cesta))
        return _gr(vysledek, cesta)
    h._vysledek_groundingu = gr

    from scripts import hans_thread as TH

    def _cisto(jm):
        try:
            p = os.path.join("data", "conversations", jm + ".json")
            if os.path.exists(p):
                os.remove(p)
        except Exception:
            pass
        try:
            TH.reset()
        except Exception:
            pass
        try:
            CC._llm_route_cache.clear()
        except Exception:
            pass
        try:
            ag = h._agent_router()
            if ag is not None and getattr(ag, "_pending", None):
                ag._pending.clear()
        except Exception:
            pass

    def _cas(sig, frm):
        raise TimeoutError("věta trvá přes 90 s")
    signal.signal(signal.SIGALRM, _cas)

    vety = json.load(open("vety.json", encoding="utf-8"))
    n = int(os.environ.get("SNIMEK_VET") or 0)
    if n:
        krok = max(1, len(vety) // n)
        vety = vety[::krok][:n]
    ven = open("snimek.jsonl", "w", encoding="utf-8", buffering=1)
    print("START vět=%d známý=<první z configu> cizí=%s" % (len(vety), CIZI), flush=True)
    t_start = time.time()
    for i, veta in enumerate(vety):
        for druh, jm in (("z", znamy), ("c", CIZI)):
            _cisto(jm)
            _ZAZNAM.clear()
            _ZAZNAM.update(blok=[], ollama=[], prikaz=[], gr=[], krok=None,
                           cmd_llm=False, agent_llm=False, model=0)
            t0 = time.time()
            vyj = ""
            odp = None
            signal.alarm(90)
            try:
                odp = h.send_chat_message(jm, veta, channel="web")
            except BaseException as e:      # i TimeoutError a SystemExit
                vyj = "%s: %s" % (type(e).__name__, str(e)[:120])
            finally:
                signal.alarm(0)
            z = dict(_ZAZNAM)
            ven.write(json.dumps({
                "i": i, "kdo": druh, "veta": veta,
                "krok": z["krok"] or ("model" if z["model"] else "nic"),
                "prikaz": z["prikaz"], "gr": z["gr"],
                "cmd_llm": z["cmd_llm"], "agent_llm": z["agent_llm"],
                "model": z["model"], "ollama": sorted(set(z["ollama"])),
                "blok": sorted(set(re.sub(r":.*", "", b) if b.startswith(("zápis", "db", "zásah"))
                                   else b for b in z["blok"])),
                "odp": " ".join(str(odp or "").split())[:160],
                "vyjimka": vyj, "ms": int((time.time() - t0) * 1000),
            }, ensure_ascii=False) + "\n")
        if i % 100 == 0:
            print("  %d/%d  %.0f s" % (i, len(vety), time.time() - t_start), flush=True)
    print("HOTOVO %.0f s" % (time.time() - t_start), flush=True)
    os._exit(0)      # vlákna na pozadí (stahování apod.) nečekat


# kroky, které nevracejí rozhodnutí (běží vždy, až za modelem)
_BEZ_NAVRATU = ("_sc_vypadek", "_sc_podklad", "_sc_nazor", "_sc_a1",
                "_sc_pojistky", "_sc_overeni_tvrzeni", "_sc_ulozeni")


# ── porovnání ────────────────────────────────────────────────────────────────
def _klic(d: dict) -> tuple:
    return (d["krok"], json.dumps(d["prikaz"], ensure_ascii=False),
            "|".join(d["gr"]), d["cmd_llm"], d["agent_llm"], bool(d["model"]),
            d["vyjimka"].split(":")[0])
    # `blok` (zachycené pokusy o síť a procesy) do klíče NEPATŘÍ: vlákna na
    # pozadí (ping, ssh, opakované spojení) se připíšou větě, která zrovna
    # běží — dva běhy téhož kódu se v něm lišily u 244 z 2 720 záznamů.


def _nacti(jmeno: str) -> dict:
    p = jmeno if os.path.exists(jmeno) else os.path.join(DIR, jmeno + ".jsonl")
    out = {}
    for l in open(p, encoding="utf-8"):
        d = json.loads(l)
        out[(d["veta"], d["kdo"])] = d
    return out


def porovnej(a: str, b: str, ukaz: int = 25) -> int:
    A, B = _nacti(a), _nacti(b)
    spolecne = [k for k in A if k in B]
    ruzne = [k for k in spolecne if _klic(A[k]) != _klic(B[k])]
    text = sum(1 for k in spolecne if _klic(A[k]) == _klic(B[k])
               and A[k]["odp"] != B[k]["odp"])
    print("záznamů %d × %d, společných %d" % (len(A), len(B), len(spolecne)))
    print("ROZDÍL V ROZHODNUTÍ: %d   (jen jiný text odpovědi: %d)" % (len(ruzne), text))
    print("jen jiné zachycené pokusy o síť/proces (časování, nepočítá se): %d"
          % sum(1 for k in spolecne if _klic(A[k]) == _klic(B[k])
                and A[k]["blok"] != B[k]["blok"]))
    for k in ruzne[:ukaz]:
        x, y = A[k], B[k]
        print("─ [%s] %s" % (k[1], k[0][:90]))
        for pole in ("krok", "prikaz", "gr", "cmd_llm", "agent_llm", "model", "blok", "vyjimka"):
            if x[pole] != y[pole]:
                print("    %s: %s  →  %s" % (pole, x[pole], y[pole]))
    return len(ruzne)


def souhrn(a: str):
    import collections
    A = _nacti(a)
    for kdo, popis in (("z", "ZNÁMÝ"), ("c", "CIZÍ")):
        r = [d for d in A.values() if d["kdo"] == kdo]
        if not r:
            continue
        print("%s — vět %d" % (popis, len(r)))
        c = collections.Counter(d["krok"] for d in r)
        for k, v in c.most_common():
            print("   %-22s %5d  %4.1f %%" % (k, v, 100.0 * v / len(r)))
        print("   došlo na router příkazů s modelem: %d · na agentní router: %d · "
              "k odpovědi modelu: %d" % (sum(d["cmd_llm"] for d in r),
                                         sum(d["agent_llm"] for d in r),
                                         sum(1 for d in r if d["model"])))
        print("   cesty podkladu:", collections.Counter(
            (d["gr"][-1] if d["gr"] else "-") for d in r).most_common(14))
        print("   pokus o následek mimo kopii:", collections.Counter(
            b for d in r for b in d["blok"]).most_common(10))
        print("   výjimky:", collections.Counter(
            d["vyjimka"].split(":")[0] for d in r if d["vyjimka"]).most_common(6))
        ms = sorted(d["ms"] for d in r)
        print("   čas: medián %d ms, p95 %d ms" % (ms[len(ms) // 2], ms[int(len(ms) * .95)]))


if __name__ == "__main__":
    a = sys.argv[1:]
    if not a:
        sys.exit(__doc__)
    if a[0] == "priprav":
        priprav()
    elif a[0] == "beh":
        beh(a[1], int(a[a.index("--vet") + 1]) if "--vet" in a else 0)
    elif a[0] == "_pracuj":
        _pracuj()
    elif a[0] == "porovnej":
        sys.exit(1 if porovnej(a[1], a[2]) else 0)
    elif a[0] == "souhrn":
        souhrn(a[1])
    else:
        sys.exit(__doc__)
