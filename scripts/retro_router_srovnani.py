# -*- coding: utf-8 -*-
"""Retro srovnání starého LLM routeru a tool-callingu na REÁLNÝCH větách.

Měřicí lešení, ne produkční kód. Přehraje historické uživatelské repliky
z deníku oběma cestami a zapíše rozdíly. Nic nevykonává, do deníku nepíše.

⚠️ ASYMETRIE, kterou je nutné číst spolu s výsledkem: starý router dostává
KONTEXT (situace dne, stav TV), `hans_tools.rozhodni` dostává HOLOU VĚTU —
tak je ten modul postavený. Když nový vyhraje i tak, je to silný výsledek;
když prohraje, část rozdílu jde na vrub chybějícímu kontextu.
"""
import json, os, re, sqlite3, sys, time

sys.path.insert(0, os.path.abspath("."))
from scripts.hans_agent import AgentRouter, ACTIONS
from scripts import hans_tools
from scripts.ollama_client import game_mode_on

VYSTUP = "data/mereni/retro_vysledky.json"
LIMIT = int(os.environ.get("LIMIT", "0"))          # 0 = vše


class _Rutina:
    phase_label = "dopoledne"


class _Idle:
    _routine = _Rutina()
    kodi = None                                    # → „Na TV teď nic nehraje." se nepřidá


class _ConvStore:
    def get_history(self, name):        return []
    def get_history_scoped(self, n, c): return []


class _Handler:
    """Minimální náhrada za živý handler. Historie je ZÁMĚRNĚ prázdná —
    rekonstruovat ji pro každou větu nejde a jako konstanta obě ramena
    neznevýhodňuje nerovnoměrně."""
    _hans_idle = _Idle()
    conv_store = _ConvStore()


def vety(db="data/hans_diary.db"):
    c = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
    rows = c.execute("SELECT title, COALESCE(NULLIF(data,''),note) FROM diary "
                     "WHERE event_type='human_chat' ORDER BY ts").fetchall()
    c.close()
    out, videno = [], set()
    for jmeno, blob in rows:
        prvni = (blob or "").split("\n", 1)[0]
        m = re.match(r"^\s*[^:]{1,30}:\s*(.+)$", prvni)
        v = (m.group(1) if m else prvni).strip()
        if len(v) >= 3 and v not in videno:
            videno.add(v)
            out.append((jmeno or "uzivatel", v))   # generické jméno — repo je veřejné
    return out


def main():
    cfg = json.load(open("config.json"))
    r = AgentRouter(cfg)
    h = _Handler()
    vsechny = vety()
    kand = [(n, v) for n, v in vsechny if r._actionable(v)]
    if LIMIT:
        kand = kand[-LIMIT:]
    print("vet celkem %d · projde branou %d · meri se %d"
          % (len(vsechny), sum(1 for n, v in vsechny if r._actionable(v)), len(kand)),
          flush=True)

    vysl, t0 = [], time.time()
    for i, (jmeno, v) in enumerate(kand, 1):
        if game_mode_on():
            print("game/preklad drzi Ollamu — koncim na %d/%d" % (i, len(kand)), flush=True)
            break
        try:
            d = r._route(h, jmeno, v)
        except Exception as e:
            d = None
            print("  stary spadl na %r: %s" % (v[:40], e), flush=True)
        stary = (d or {}).get("action")
        konf = float((d or {}).get("confidence", 0) or 0)
        # stejný práh jako v produkci: pod ním by se návrh nepodal
        if stary and konf < r.threshold:
            stary = None
        try:
            n = hans_tools.rozhodni(cfg, v)
        except Exception as e:
            n = None
            print("  novy spadl na %r: %s" % (v[:40], e), flush=True)
        novy = n[0] if n else None
        vysl.append({"jmeno": jmeno, "veta": v, "stary": stary,
                     "konf": round(konf, 2), "novy": novy,
                     "shoda": stary == novy})
        if i % 25 == 0:
            u = time.time() - t0
            print("  %d/%d · %.0f s · %.1f s/vetu · zbyva ~%.0f min"
                  % (i, len(kand), u, u / i, (len(kand) - i) * u / i / 60), flush=True)
            json.dump(vysl, open(VYSTUP, "w"), ensure_ascii=False, indent=1)
    json.dump(vysl, open(VYSTUP, "w"), ensure_ascii=False, indent=1)
    print("HOTOVO: %d vysledku → %s" % (len(vysl), VYSTUP), flush=True)


if __name__ == "__main__":
    main()
