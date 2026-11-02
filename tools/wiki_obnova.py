#!/usr/bin/env python3
"""WIKI_OBNOVA_V1 (2. 11. 2026) — měsíční obnova lokální české Wikipedie.

Hans dohledává z `data/wiki/cswiki.db` (výpis cswiki). Výpis stárne (nové články,
úmrtí, „kdo je teď…“), proto jednou za měsíc:

  1. zjistí nejnovější HOTOVÝ výpis (dumpstatus.json, úloha articlesdumprecombine);
     když už je nainstalovaný (meta `vypis`), skončí hned;
  2. stáhne ho (s pokračováním) a ověří SHA-1;
  3. sestaví novou DB VEDLE běžící (`tools/wiki_offline_build.py vse` s proměnnými
     WIKI_DUMP / WIKI_EXTR / WIKI_DB) a k ní tituly bez diakritiky
     (`tools/wiki_tituly_fold.py`, WIKI_DB / WIKI_FOLD_DB);
  4. zkontroluje ji: počet článků ≥ 97 % staré, vzorové tituly, fulltext;
  5. teprve pak vymění soubory (`os.replace`); stará DB zůstane jako JEDNA záloha
     `cswiki_vypis_<datum>.db`, starší zálohy z obnovy a mezivýsledky se smažou.

Běžící Hans si výměny všimne sám (`hans_wiki_offline`, HANS_WIKI_OBNOVA_RECONNECT_V1).
Výsledek do `data/wiki/obnova_stav.json`; chyba = návratový kód 1 (služba `failed`)
a hlídač záloh (`hans_zalohy`) ji nahlásí. Nespouští Hanse ani model.

    nice -n 15 python3 tools/wiki_obnova.py            # běžná obnova
    python3 tools/wiki_obnova.py --zjisti              # jen co je nového
"""
import glob, hashlib, json, os, shutil, sqlite3, subprocess, sys, time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
W = os.path.join(ROOT, "data", "wiki")
DB = os.path.join(W, "cswiki.db")
FOLD = os.path.join(W, "tituly_fold.db")
NOVA_DB = os.path.join(W, "cswiki_nova.db")
NOVY_FOLD = os.path.join(W, "tituly_fold_nova.db")
EXTR = os.path.join(W, "extrakt_obnova")
STAV = os.path.join(W, "obnova_stav.json")
BASE = "https://dumps.wikimedia.org/cswiki/"
UA = {"User-Agent": "HansHomeAssistant/1.0 (monthly cswiki refresh; personal use)"}
VZOROVE = ("Praha", "Česko", "Karel IV.", "Karlův most", "Antonín Dvořák")
MIN_POMER = 0.97


def _log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def _stav(**k):
    k["cas"] = time.strftime("%Y-%m-%d %H:%M")
    with open(STAV + ".tmp", "w", encoding="utf-8") as f:
        json.dump(k, f, ensure_ascii=False, indent=1)
    os.replace(STAV + ".tmp", STAV)


def _get(url, timeout=60):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
        return r.read()


def nejnovejsi():
    """(datum, jméno souboru, velikost, sha1) nejnovějšího hotového výpisu, nebo None."""
    import re
    idx = _get(BASE).decode("utf-8", "replace")
    data = sorted(set(re.findall(r'href="(\d{8})/"', idx)), reverse=True)
    for d in data[:3]:
        try:
            st = json.loads(_get(BASE + d + "/dumpstatus.json"))
        except Exception:
            continue
        j = st.get("jobs", {}).get("articlesdumprecombine", {})
        if j.get("status") != "done":
            continue
        for fn, v in (j.get("files") or {}).items():
            if fn.endswith("-pages-articles.xml.bz2"):
                return d, fn, int(v.get("size") or 0), v.get("sha1") or ""
    return None


def nainstalovany():
    try:
        c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        r = c.execute("SELECT hodnota FROM meta WHERE klic='vypis'").fetchone()
        c.close()
        return r[0] if r else ""
    except Exception:
        return ""


def stahni(datum, fn, velikost, sha1):
    cil = os.path.join(W, fn)
    if os.path.exists(cil) and os.path.getsize(cil) == velikost:
        _log("výpis už je stažený:", fn)
    else:
        url = BASE + datum + "/" + fn
        _log("stahuji", url, "(%.2f GB)" % (velikost / 1e9))
        # curl umí pokračovat (-C -) a opakovat; Wikimedia povoluje 2 spojení
        subprocess.run(["curl", "-sS", "-L", "-C", "-", "--retry", "5", "--retry-delay", "30",
                        "-A", UA["User-Agent"], "-o", cil, url], check=True)
    if os.path.getsize(cil) != velikost:
        raise RuntimeError("velikost výpisu nesedí (%d × %d)" % (os.path.getsize(cil), velikost))
    if sha1:
        h = hashlib.sha1()
        with open(cil, "rb") as f:
            for blok in iter(lambda: f.read(1 << 22), b""):
                h.update(blok)
        if h.hexdigest() != sha1:
            raise RuntimeError("SHA-1 výpisu nesedí")
    _log("výpis ověřen")
    return cil


def sestav(vypis):
    env = dict(os.environ, WIKI_DUMP=vypis, WIKI_EXTR=EXTR, WIKI_DB=NOVA_DB)
    for p in (NOVA_DB, NOVY_FOLD):
        if os.path.exists(p):
            os.remove(p)
    shutil.rmtree(EXTR, ignore_errors=True)
    _log("sestavuji DB (průchod, extrakce, index; ~3,5 h)")
    subprocess.run(["nice", "-n", "15", sys.executable,
                    os.path.join(ROOT, "tools", "wiki_offline_build.py"), "vse"],
                   check=True, env=env, cwd=ROOT)
    _log("tituly bez diakritiky")
    subprocess.run(["nice", "-n", "15", sys.executable,
                    os.path.join(ROOT, "tools", "wiki_tituly_fold.py")],
                   check=True, env=dict(env, WIKI_FOLD_DB=NOVY_FOLD), cwd=ROOT)


def zkontroluj():
    """Nová DB musí být srovnatelná se starou; jinak výjimka (stará zůstává)."""
    n = sqlite3.connect(f"file:{NOVA_DB}?mode=ro", uri=True)
    pocet = n.execute("SELECT count(*) FROM clanek").fetchone()[0]
    if not n.execute("SELECT 1 FROM meta WHERE klic='sestaveno'").fetchone():
        raise RuntimeError("nová DB nemá značku sestavení")
    chybi = [t for t in VZOROVE
             if not n.execute("SELECT 1 FROM clanek WHERE titul=? AND length(text)>500", (t,)).fetchone()]
    if chybi:
        raise RuntimeError("v nové DB chybí vzorové články: %s" % ", ".join(chybi))
    fts = n.execute("SELECT count(*) FROM clanek_fts WHERE clanek_fts MATCH 'karluv AND most'").fetchone()[0]
    if fts < 5:
        raise RuntimeError("fulltext nové DB nic nenajde (%d)" % fts)
    n.close()
    stary = 0
    if os.path.exists(DB):
        s = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        stary = s.execute("SELECT count(*) FROM clanek").fetchone()[0]
        s.close()
    if stary and pocet < MIN_POMER * stary:
        raise RuntimeError("nová DB má málo článků (%d × dosavadních %d)" % (pocet, stary))
    f = sqlite3.connect(f"file:{NOVY_FOLD}?mode=ro", uri=True)
    nf = f.execute("SELECT count(*) FROM t").fetchone()[0]
    f.close()
    if nf < pocet:
        raise RuntimeError("tabulka titulů bez diakritiky je neúplná (%d)" % nf)
    _log("kontrola v pořádku: článků %d (dosud %d), titulů bez diakritiky %d" % (pocet, stary, nf))
    return pocet, stary


def vymen(stary_vypis, novy_vypis):
    """Stará DB → jediná záloha; nová na její místo. Hans si všimne sám."""
    import re
    m = re.search(r"(\d{8})", stary_vypis or "")
    zaloha = os.path.join(W, "cswiki_vypis_%s.db" % (m.group(1) if m else "stary"))
    for p in glob.glob(os.path.join(W, "cswiki_vypis_*.db")):
        if p != zaloha:
            os.remove(p)          # jen zálohy z dřívějších obnov (ruční zálohy zůstávají)
    if os.path.exists(DB):
        os.replace(DB, zaloha)
    os.replace(NOVA_DB, DB)
    if os.path.exists(FOLD):
        os.replace(FOLD, FOLD + ".pred_obnovou")
    os.replace(NOVY_FOLD, FOLD)
    shutil.rmtree(EXTR, ignore_errors=True)
    for p in glob.glob(os.path.join(W, "cswiki-*-pages-articles.xml.bz2")):
        if os.path.basename(p) != novy_vypis:
            os.remove(p)          # starší výpis; poslední zůstává pro případné přestavění
    _log("vyměněno; záloha", os.path.basename(zaloha))
    return zaloha


def main():
    os.makedirs(W, exist_ok=True)
    try:
        nove = nejnovejsi()
    except Exception as e:
        _stav(vysledek="chyba", krok="zjištění", chyba=str(e)[:300], vypis=nainstalovany())
        _log("nejde zjistit nejnovější výpis:", e)
        return 1
    ted = nainstalovany()
    if not nove:
        _log("žádný hotový výpis"); return 0
    datum, fn, vel, sha1 = nove
    if "--zjisti" in sys.argv:
        print("nainstalováno:", ted or "-", "| nejnovější:", fn, "(%.2f GB)" % (vel / 1e9))
        return 0
    if ted == fn:
        _log("už je nainstalovaný", fn)
        return 0
    krok = "stažení"
    try:
        vypis = stahni(datum, fn, vel, sha1)
        krok = "sestavení"
        sestav(vypis)
        krok = "kontrola"
        pocet, stary = zkontroluj()
        krok = "výměna"
        zaloha = vymen(ted, fn)
    except Exception as e:
        for p in (NOVA_DB, NOVY_FOLD):
            if os.path.exists(p):
                os.remove(p)
        shutil.rmtree(EXTR, ignore_errors=True)
        _stav(vysledek="chyba", krok=krok, chyba=str(e)[:300], vypis=ted, novy=fn)
        _log("OBNOVA SELHALA (%s): %s — Hans dál používá %s" % (krok, e, ted))
        return 1
    _stav(vysledek="ok", vypis=fn, predchozi=ted, clanku=pocet, dosud=stary,
          zaloha=os.path.basename(zaloha))
    _log("HOTOVO", fn)
    return 0


if __name__ == "__main__":
    sys.exit(main())
