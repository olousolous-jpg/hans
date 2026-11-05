"""HANS_NETHACK_V1 (5. 11. 2026) — Hans hraje NetHack ve volném čase.

Proč: činnost mimo četbu s výsledkem, který si Hans nemůže vymyslet (skóre,
patro a příčinu konce počítá hra). Uživatel 5. 11.: „dáme nethack a uvidíme“.

Jak:
- NetHack Learning Environment (`pip nle`, běží na Pi). Hra drží JEN V PAMĚTI
  procesu (NLE neumí uložit rozehranou hru) → restart Hanse = hra „přerušena“
  se skóre do té chvíle.
- Model mapu ze znaků nepřečte → okolí popíše kód slovy (sousední pole, nejbližší
  zajímavé věci se směrem a vzdáleností), Hans vybere CÍL ze seznamu (HANS_NETHACK_CILE_V1: schody, věc,
  útok, prozkoumat…) a cestu k němu najde kód — tahy po jednom poli bloudily
  (zkouška 5. 11.: 1 200 rozhodnutí, patro 1, skóre 3).
- „--More--“ odklepne kód, otázky ano/ne zruší (ESC); Hans řeší jen tahy.
- Sezení má strop (`max_kroku`, `max_minut`), hra pokračuje po `pauza_min`;
  nová hra po `nova_po_h`. Konec hry → deník `nethack_hra`.
Vypínač `nethack.enabled`. Volá se ze smyčky Koláčova dialogu (`hans_dialog`).
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time

_log = logging.getLogger("hans_nethack")
_LOCK = threading.Lock()
_HRA = {}            # rozehraná hra v paměti procesu

_DEF = {"enabled": True, "max_kroku": 60, "max_minut": 4, "pauza_min": 20,
        "nova_po_h": 2}

# akce: (štítek pro model, akce NLE)
def _akce():
    from nle.nethack import actions as A
    return [
        ("sever", A.CompassDirection.N), ("jih", A.CompassDirection.S),
        ("východ", A.CompassDirection.E), ("západ", A.CompassDirection.W),
        ("severovýchod", A.CompassDirection.NE), ("severozápad", A.CompassDirection.NW),
        ("jihovýchod", A.CompassDirection.SE), ("jihozápad", A.CompassDirection.SW),
        ("dolů", A.MiscDirection.DOWN), ("nahoru", A.MiscDirection.UP),
        ("počkat", A.MiscDirection.WAIT), ("hledat", A.Command.SEARCH),
        ("sebrat", A.Command.PICKUP), ("jíst", A.Command.EAT),
        ("vykopnout", A.Command.KICK), ("otevřít", A.Command.OPEN),
        ("_more", A.MiscAction.MORE), ("_esc", A.Command.ESC),
    ] + _pismena()


def _pismena():
    """Akce pro písmena a–z (výběr věci z batohu: „What do you want to eat?“)."""
    from nle.nethack import ACTIONS
    out = []
    for ch in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ":
        a = next((x for x in ACTIONS if int(x) == ord(ch)), None)
        if a is not None:
            out.append(("_k_" + ch, a))
    return out


_SMER = {(-1, 0): "sever", (1, 0): "jih", (0, 1): "východ", (0, -1): "západ",
         (-1, 1): "severovýchod", (-1, -1): "severozápad", (1, 1): "jihovýchod",
         (1, -1): "jihozápad"}
_NUDNE = {"", "floor of a room", "dark part of a room", "stone", "wall", "corridor",
          "lit corridor", "dark room"}


def _cfg(config):
    d = dict(_DEF)
    d.update((config or {}).get("nethack", {}) or {})
    return d


def _db(config):
    c = sqlite3.connect((config or {}).get("diary_db") or "data/hans_diary.db", timeout=10.0)
    c.execute("CREATE TABLE IF NOT EXISTS nethack_hry (id INTEGER PRIMARY KEY, start_ts REAL, "
              "posledni_ts REAL, konec_ts REAL, stav TEXT, skore INTEGER, hloubka INTEGER, "
              "max_hloubka INTEGER, herni_tahy INTEGER, kroky INTEGER, sezeni INTEGER, "
              "neplatne INTEGER, duvod TEXT)")
    return c


def _model(config):
    return str(((config or {}).get("nethack", {}) or {}).get("model")
               or ((config or {}).get("evening_reflection", {}) or {}).get("voice_model")
               or "hans-czech:latest")


def _tty(obs) -> str:
    return "\n".join(bytes(r).decode("latin1") for r in obs["tty_chars"])


def popis(obs) -> str:
    """Okolí postavy slovy (kód, ne model)."""
    import nle.nethack as nh
    bl = obs["blstats"]
    x, y = int(bl[nh.NLE_BL_X]), int(bl[nh.NLE_BL_Y])
    sd = obs["screen_descriptions"]
    H, W = sd.shape[0], sd.shape[1]

    def d(yy, xx):
        if 0 <= yy < H and 0 <= xx < W:
            return bytes(sd[yy][xx]).decode("latin1").strip("\x00").strip()
        return ""
    sous = []
    for (dy, dx), s in _SMER.items():
        sous.append("%s: %s" % (s, d(y + dy, x + dx) or "nic"))
    zaj = {}
    for yy in range(H):
        for xx in range(W):
            t = d(yy, xx)
            if t in _NUDNE or (yy == y and xx == x) or "called Agent" in t:
                continue
            dist = max(abs(yy - y), abs(xx - x))
            if t not in zaj or dist < zaj[t][0]:
                zaj[t] = (dist, yy - y, xx - x)
    items = sorted(zaj.items(), key=lambda kv: kv[1][0])[:8]
    def smer(dy, dx):
        return _SMER.get(((dy > 0) - (dy < 0), (dx > 0) - (dx < 0)), "tady")
    vzd = ["%s — %d pole na %s" % (t, dist, smer(dy, dx)) for t, (dist, dy, dx) in items]
    return ("Sousední pole: " + "; ".join(sous) + "\nV dohledu: "
            + ("; ".join(vzd) if vzd else "nic zajímavého"))


def _stav(obs) -> str:
    import nle.nethack as nh
    bl = obs["blstats"]
    return ("Životy %d/%d, patro %d, zlato %d, hlad %s, herní tah %d" % (
        bl[nh.NLE_BL_HP], bl[nh.NLE_BL_HPMAX], bl[nh.NLE_BL_DEPTH], bl[nh.NLE_BL_GOLD],
        "ano" if bl[nh.NLE_BL_HUNGER] >= 2 else "ne", bl[nh.NLE_BL_TIME]))


def _zprava(obs) -> str:
    return bytes(obs["message"]).decode("latin1").strip("\x00").strip()


_STENA = ("wall", "stone", "tree", "iron bars", "boulder", "dark part of a room?")


def _pruchozi(desc: str, glyph: int) -> bool:
    if not desc:
        return False
    return not any(desc == s or desc.endswith(" wall") for s in _STENA) and desc != "wall"


def _bfs(obs, cil, max_d=200, nav=None):
    """HANS_NETHACK_CILE_V1 — první krok nejkratší cesty k poli, kde cil(y,x) platí.
    Diagonála do/z dveří se nesmí (pravidlo NetHacku). (dy,dx) nebo None."""
    import nle.nethack as nh
    bl = obs["blstats"]
    sx, sy = int(bl[nh.NLE_BL_X]), int(bl[nh.NLE_BL_Y])
    sd, gl = obs["screen_descriptions"], obs["glyphs"]
    H, W = sd.shape[0], sd.shape[1]
    D = lambda y, x: bytes(sd[y][x]).decode("latin1").strip("\x00").strip()
    from collections import deque
    prvni = {(sy, sx): None}
    q = deque([(sy, sx)])
    while q:
        y, x = q.popleft()
        if (y, x) != (sy, sx) and cil(y, x):
            return prvni[(y, x)]
        here_door = "door" in D(y, x)
        for (dy, dx) in _SMER:
            yy, xx = y + dy, x + dx
            if not (0 <= yy < H and 0 <= xx < W) or (yy, xx) in prvni:
                continue
            d = D(yy, xx)
            # HANS_NETHACK_BLOK_V1 — pole, kam se krok nepovedl (balvan…), obejít
            if nav is not None and ("B", int(bl[nh.NLE_BL_DEPTH]), yy, xx) in nav:
                continue
            if not (_pruchozi(d, int(gl[yy][xx])) or cil(yy, xx)):
                continue
            if dy and dx and (here_door or "door" in d):
                continue
            if "closed door" in d and not cil(yy, xx):
                continue
            prvni[(yy, xx)] = prvni[(y, x)] or (dy, dx)
            q.append((yy, xx))
    return None


def volby(obs, nav=None) -> dict:
    """{štítek: (popis pro model, cíl-funkce nebo akce)} podle situace."""
    import nle.nethack as nh
    bl = obs["blstats"]
    x, y = int(bl[nh.NLE_BL_X]), int(bl[nh.NLE_BL_Y])
    sd, gl = obs["screen_descriptions"], obs["glyphs"]
    H, W = sd.shape[0], sd.shape[1]
    D = lambda yy, xx: bytes(sd[yy][xx]).decode("latin1").strip("\x00").strip()
    v = {}
    hl = int(bl[nh.NLE_BL_DEPTH])
    nav = nav if nav is not None else set()
    # HANS_NETHACK_SCHODY_V1 — na schodech stojí postava, popis pole je „Agent“
    # → kde schody jsou, si pamatuje kód (dřív se „dolů“ nikdy nenabídlo)
    for yy in range(H):
        for xx in range(W):
            if "staircase down" in D(yy, xx):
                nav.add(("S", hl, yy, xx))
    schody = {(a[2], a[3]) for a in nav if a[0] == "S" and a[1] == hl}
    if (y, x) in schody:
        v["dolů"] = ("sejít po schodech dolů (stojíš na nich)", "DOWN")
    elif schody:
        v["ke schodům"] = ("dojít ke schodům dolů", lambda yy, xx: (yy, xx) in schody)
    sous = [((dy, dx), int(gl[y + dy][x + dx])) for (dy, dx) in _SMER
            if 0 <= y + dy < H and 0 <= x + dx < W]
    for (dy, dx), g in sous:
        if nh.glyph_is_monster(g) and not nh.glyph_is_pet(g):
            v["zaútočit"] = ("zaútočit na %s vedle tebe" % D(y + dy, x + dx), ("DIR", (dy, dx)))
            break
    def _vec(yy, xx):
        return ((yy, xx) != (y, x) and nh.glyph_is_object(int(gl[yy][xx]))
                and "boulder" not in D(yy, xx))
    if _bfs(obs, _vec, nav=nav) is not None:     # jen dosažitelná věc, ne balvan
        v["k věci"] = ("dojít k nejbližší věci na zemi", _vec)
    if nh.glyph_is_object(int(gl[y][x])) or "You see here" in _zprava(obs):
        v["sebrat"] = ("sebrat věc, na které stojíš", "PICKUP")

    # HANS_NETHACK_NAVSTIVENO_V1 — chodby mají neznámou skálu kolem napořád →
    # „neznámé místo“ = pole u neznámého, kde postava ještě NEBYLA

    def hranice(yy, xx):
        if (hl, yy, xx) in nav or not _pruchozi(D(yy, xx), 0):
            return False
        return any(0 <= yy + a < H and 0 <= xx + b < W and not D(yy + a, xx + b)
                   for (a, b) in _SMER)
    if _bfs(obs, hranice, nav=nav) is not None:
        v["prozkoumat"] = ("jít prozkoumat nejbližší neznámé místo", hranice)
    if any("closed door" in D(y + dy, x + dx) for (dy, dx), _ in sous):
        v["otevřít dveře"] = ("otevřít zavřené dveře vedle tebe", "OPENDOOR")
    # jíst jen s jídlem v batohu (jinak „jíst“ dokola bez účinku)
    if bl[nh.NLE_BL_HUNGER] >= 1 and any(int(o) == int(nh.FOOD_CLASS)
                                         for o in obs["inv_oclasses"]):
        v["jíst"] = ("najíst se (máš hlad a jídlo v batohu)", "EAT")
    # HANS_NETHACK_VOLBY_V1 — pasivní volby jen tam, kde dávají smysl (s modelem
    # 5. 11.: jinak „hledat/odpočinout“ dokola a stál na místě)
    if "prozkoumat" not in v and "ke schodům" not in v and "dolů" not in v:
        v["hledat"] = ("hledat skryté dveře a chodby (není kam jít)", "SEARCH")
    if bl[nh.NLE_BL_HP] < 0.5 * max(1, bl[nh.NLE_BL_HPMAX]) and "zaútočit" not in v:
        v["odpočinout"] = ("chvíli odpočívat (máš málo životů)", "REST")
    return v


def _hans_akce(config, obs, historie, stoji, nav=None, zakaz=None) -> tuple:
    from scripts.ollama_client import ollama_generate
    v = volby(obs, nav)
    # HANS_NETHACK_ZAKAZ_V1 — volba, která 3× po sobě nic nezměnila, na chvíli pryč
    for s in list(zakaz or ()):
        if s in v and len(v) > 1:
            del v[s]
    stitky = list(v)
    sysp = ("Hraješ hru NetHack: procházíš podzemí, sbíráš věci, bojuješ s příšerami a "
            "hledáš schody dolů, abys šel hlouběji. Rozhoduješ, CO udělat; cestu najdeš "
            "sám. Odpověz JEDNOU volbou přesně ze seznamu (jen její název).")
    pr = "%s\nZpráva hry: %s\n%s\n" % (_stav(obs), _zprava(obs) or "-", popis(obs))
    if historie:
        pr += "Tvá poslední rozhodnutí: %s\n" % ", ".join(historie[-6:])
    if stoji:
        pr += "POZOR: posledních %d rozhodnutí ses nepohnul — zvol něco jiného.\n" % stoji
    pr += "Možnosti:\n" + "\n".join("- %s: %s" % (s, v[s][0]) for s in stitky) + "\nTvoje volba:"
    for pokus in range(2):
        raw = ollama_generate(_model(config), pr, system=sysp, config=config, timeout=60,
                              options={"temperature": 0.4 + 0.3 * pokus, "num_predict": 10,
                                       "num_ctx": 2048})
        if raw is None:
            return None, False, v
        r = raw.lower().strip(" .,:;*`\"'()\n-")
        for s in sorted(stitky, key=len, reverse=True):
            if r.startswith(s) or (" " + s) in (" " + r):
                return s, True, v
        # stačí první slovo volby („otevřít“ → „otevřít dveře“, „schody“ → „ke schodům“)
        prvni = r.split()[0] if r.split() else ""
        for s in stitky:
            if prvni and (s.split()[0] == prvni or (prvni[:5] in s and len(prvni) >= 5)):
                return s, True, v
    return next(iter(v)), False, v


def _proved(env, idx, obs, volba, v, max_kroku=10, nav=None):
    """Provede volbu (makro až `max_kroku` kroků). Vrací (obs, odměna, konec)."""
    import nle.nethack as nh
    co = v[volba][1]
    dir_akce = {d: idx[s] for d, s in _SMER.items()}
    sk, konec = 0.0, False

    def krok(a):
        nonlocal obs, sk, konec
        if konec:      # po konci hry už NLE krok nepřijme
            return
        obs, r, d, tr, _ = env.step(a)
        sk += float(r or 0)
        konec = konec or d or tr
        if nav is not None:
            _b = obs["blstats"]
            nav.add((int(_b[nh.NLE_BL_DEPTH]), int(_b[nh.NLE_BL_Y]), int(_b[nh.NLE_BL_X])))
        # odklepnout --More-- a zrušit otázky
        for _ in range(8):
            if konec:
                break
            tt = _tty(obs)
            if "--More--" in tt:
                obs, r, d, tr, _ = env.step(idx["_more"])
            elif "[yn" in tt or "What do you want" in tt or "In what direction" in tt:
                obs, r, d, tr, _ = env.step(idx["_esc"])
            else:
                break
            sk += float(r or 0)
            konec = konec or d or tr
    if co == "DOWN":
        krok(idx["dolů"])
    elif co == "PICKUP":
        krok(idx["sebrat"])
    elif co == "EAT":
        # HANS_NETHACK_JIDLO_V1 — na „What do you want to eat?“ kód vybere první
        # jídlo z batohu (dřív otázku zrušil → „jíst“ dokola bez účinku)
        obs, r, d, tr, _ = env.step(idx["jíst"])
        sk += float(r or 0)
        konec = konec or d or tr
        if not konec and "want to eat" in _tty(obs):
            pis = [chr(int(l)) for l, o in zip(obs["inv_letters"], obs["inv_oclasses"])
                   if int(o) == int(nh.FOOD_CLASS) and int(l)]
            if pis and ("_k_" + pis[0]) in idx:
                krok(idx["_k_" + pis[0]])
            else:
                krok(idx["_esc"])
        else:
            krok(idx["_more"]) if "--More--" in _tty(obs) else None
    elif co == "SEARCH":
        for _ in range(5):
            krok(idx["hledat"])
    elif co == "REST":
        for _ in range(10):
            krok(idx["hledat"])
    elif co == "OPENDOOR":
        bl = obs["blstats"]
        x, y = int(bl[nh.NLE_BL_X]), int(bl[nh.NLE_BL_Y])
        sd = obs["screen_descriptions"]
        for d in _SMER:
            if "closed door" in bytes(sd[y + d[0]][x + d[1]]).decode("latin1"):
                obs, r, dn, tr, _ = env.step(idx["otevřít"])
                if not (dn or tr):
                    obs, r, dn, tr, _ = env.step(dir_akce[d])
                sk += float(r or 0)
                konec = konec or dn or tr
                break
    elif isinstance(co, tuple) and co[0] == "DIR":
        krok(dir_akce[co[1]])
    elif callable(co):
        hp0 = int(obs["blstats"][nh.NLE_BL_HP])
        for _ in range(max_kroku):
            d = _bfs(obs, co, nav=nav)
            if d is None or konec:
                break
            _b0 = obs["blstats"]
            p0 = (int(_b0[nh.NLE_BL_Y]), int(_b0[nh.NLE_BL_X]), int(_b0[nh.NLE_BL_DEPTH]))
            krok(dir_akce[d])
            _b1 = obs["blstats"]
            if nav is not None and (int(_b1[nh.NLE_BL_Y]), int(_b1[nh.NLE_BL_X]),
                                    int(_b1[nh.NLE_BL_DEPTH])) == p0:
                # zeď/balvan ano, příšera (boj) ne
                _g = int(obs["glyphs"][p0[0] + d[0]][p0[1] + d[1]])
                if not nh.glyph_is_monster(_g):
                    nav.add(("B", p0[2], p0[0] + d[0], p0[1] + d[1]))
                break
            if (_zprava(obs) or int(obs["blstats"][nh.NLE_BL_HP]) < hp0
                    or co(int(obs["blstats"][nh.NLE_BL_Y]), int(obs["blstats"][nh.NLE_BL_X]))):
                break
    return obs, sk, konec


def _konec_hry(config, c, hid, obs, duvod):
    import nle.nethack as nh
    h = _HRA
    bl = obs["blstats"]
    skore = int(h.get("skore", 0))
    c.execute("UPDATE nethack_hry SET posledni_ts=?, konec_ts=?, stav=?, skore=?, hloubka=?, "
              "max_hloubka=?, herni_tahy=?, kroky=?, sezeni=?, neplatne=?, duvod=? WHERE id=?",
              (time.time(), time.time(), "konec", skore, int(bl[nh.NLE_BL_DEPTH]),
               int(h.get("max_hloubka", 1)), int(h.get("cas", 0)), h.get("kroky", 0),
               h.get("sezeni", 0) + 1, h.get("neplatne", 0), duvod, hid))
    c.commit()
    note = ("Hrál jsem NetHack. Hra skončila: %s. Skóre %d, nejhlubší patro %d, herních tahů "
            "%d, mých rozhodnutí %d (ve %d sezeních)." % (
                duvod, skore, int(h.get("max_hloubka", 1)), int(h.get("cas", 0)),
                h.get("kroky", 0), h.get("sezeni", 0) + 1))
    if h.get("neplatne"):
        note += " Neplatných odpovědí (zahrál se náhodný směr): %d." % h["neplatne"]
    try:
        c.execute("INSERT INTO diary (ts, event_type, title, note, data) VALUES (?,?,?,?,?)",
                  (time.time(), "nethack_hra", "NetHack: skóre %d, patro %d" % (
                      skore, int(h.get("max_hloubka", 1))), note,
                   json.dumps({"hra": hid, "duvod": duvod}, ensure_ascii=False)))
        c.commit()
    except Exception as e:
        _log.warning("HANS_NETHACK_V1: zápis do deníku: %s", e)
    _log.info("HANS_NETHACK_V1: hra %d skončila — %s; skóre %d, patro %d, %d rozhodnutí",
              hid, duvod, skore, int(h.get("max_hloubka", 1)), h.get("kroky", 0))
    try:
        h["env"].close()
    except Exception:
        pass
    _HRA.clear()


def _duvod_smrti(obs) -> str:
    t = _tty(obs)
    import re
    m = re.search(r"killed by ([^\n]{3,60})", t)
    if m:
        return "zabit (%s)" % m.group(1).strip()
    for k, v in (("starv", "umřel hlady"), ("drown", "utopil se"), ("quit", "vzdal")):
        if k in t.lower():
            return v
    return "smrt"


def tick(config: dict, ted: float = None) -> str:
    cfg = _cfg(config)
    if not cfg.get("enabled"):
        return "vypnuto"
    try:
        from scripts.ollama_client import game_mode_on
        if game_mode_on():
            return "hra na PC"
    except Exception:
        pass
    if not _LOCK.acquire(blocking=False):
        return "běží"
    try:
        return _sezeni(config, cfg, ted or time.time())
    except Exception as e:
        _log.warning("HANS_NETHACK_V1: sezení selhalo: %s", e)
        return "chyba"
    finally:
        _LOCK.release()


def _sezeni(config, cfg, ted) -> str:
    c = _db(config)
    try:
        if not _HRA:
            # hra ze zaniklého procesu (restart) = přerušená
            c.execute("UPDATE nethack_hry SET stav='prerusena', konec_ts=?, duvod="
                      "'přerušeno restartem' WHERE stav='bezi'", (ted,))
            c.commit()
            k = c.execute("SELECT max(konec_ts) FROM nethack_hry").fetchone()[0]
            try:   # HANS_MOTIVACE_V1 — víc času, když se zlepšuje
                from scripts.hans_motivace import vaha as _vaha
                _m = _vaha(config, "nethack")
            except Exception:
                _m = 1.0
            if k and ted - float(k) < cfg["nova_po_h"] * 3600 / _m:
                return "není čas"
            try:
                import gymnasium as gym
                import nle  # noqa: F401
            except ImportError:
                return "chybí nle"
            akce = _akce()
            env = gym.make("NetHackScore-v0", actions=tuple(a for _, a in akce))
            obs, _ = env.reset()
            cur = c.execute("INSERT INTO nethack_hry (start_ts, posledni_ts, stav, sezeni, "
                            "kroky, neplatne) VALUES (?,?,?,0,0,0)", (ted, ted, "bezi"))
            c.commit()
            _HRA.update({"env": env, "obs": obs, "id": cur.lastrowid, "akce": akce,
                         "skore": 0, "kroky": 0, "sezeni": 0, "neplatne": 0,
                         "max_hloubka": 1, "historie": [], "posledni": 0.0, "stoji": 0,
                         "pozice": None, "nav": set()})
            _log.info("HANS_NETHACK_V1: nová hra %d", cur.lastrowid)
        elif ted - _HRA.get("posledni", 0) < cfg["pauza_min"] * 60:
            return "není čas"
        import nle.nethack as nh
        h = _HRA
        env, akce = h["env"], h["akce"]
        idx = {s: i for i, (s, _) in enumerate(akce)}
        t0, n, done = time.time(), 0, False
        while n < cfg["max_kroku"] and time.time() - t0 < cfg["max_minut"] * 60:
            obs = h["obs"]
            h.setdefault("zakaz", {})
            h["zakaz"] = {s: k for s, k in h["zakaz"].items() if k > h["kroky"]}
            volba, ok, v = _hans_akce(config, obs, h["historie"], h["stoji"], h["nav"],
                                      h["zakaz"])
            _stav0 = (_zprava(obs), int(obs["blstats"][nh.NLE_BL_TIME]))
            if volba is None:
                break     # model nedostupný → pokračuje příště
            h["neplatne"] += 0 if ok else 1
            obs, r, done = _proved(env, idx, obs, volba, v, nav=h["nav"])
            h["kroky"] += 1
            n += 1
            h["historie"] = (h["historie"] + [volba])[-12:]
            bl = obs["blstats"]
            poz = (int(bl[nh.NLE_BL_X]), int(bl[nh.NLE_BL_Y]), int(bl[nh.NLE_BL_DEPTH]))
            h["stoji"] = h["stoji"] + 1 if poz == h["pozice"] else 0
            h["pozice"] = poz
            if h["stoji"] >= 3 and h["historie"][-3:] == [volba] * 3:
                h["zakaz"][volba] = h["kroky"] + 20
            h["max_hloubka"] = max(h["max_hloubka"], poz[2])
            h["cas"] = max(h.get("cas", 0), int(bl[nh.NLE_BL_TIME]))
            h["skore"] += r
            h["obs"] = obs
            if done:
                break
        h["posledni"] = time.time()
        if done:
            _konec_hry(config, c, h["id"], h["obs"], _duvod_smrti(h["obs"]))
            return "konec"
        h["sezeni"] += 1
        c.execute("UPDATE nethack_hry SET posledni_ts=?, skore=?, max_hloubka=?, kroky=?, "
                  "sezeni=?, neplatne=? WHERE id=?",
                  (time.time(), int(h["skore"]), h["max_hloubka"], h["kroky"], h["sezeni"],
                   h["neplatne"], h["id"]))
        c.commit()
        _log.info("HANS_NETHACK_V1: hra %d pokračuje příště (skóre %d, patro %d, %d rozhodnutí)",
                  h["id"], int(h["skore"]), h["max_hloubka"], h["kroky"])
        return "pokračuje"
    finally:
        c.close()


def prehled(config: dict) -> str:
    c = _db(config)
    try:
        rows = c.execute("SELECT skore, max_hloubka, duvod FROM nethack_hry WHERE "
                         "stav IN ('konec','prerusena') ORDER BY id").fetchall()
        bezi = c.execute("SELECT skore, max_hloubka, kroky FROM nethack_hry WHERE "
                         "stav='bezi' ORDER BY id DESC LIMIT 1").fetchone()
    finally:
        c.close()
    if not rows and not bezi:
        return "NetHack jsem zatím nehrál."
    out = []
    if rows:
        sk = [r[0] or 0 for r in rows]
        out.append("NetHack: odehraných her %d, nejlepší skóre %d, nejhlubší patro %d, "
                   "poslední hra skóre %d (%s)." % (len(rows), max(sk),
                                                    max(r[1] or 1 for r in rows), sk[-1],
                                                    rows[-1][2] or "?"))
    if bezi:
        out.append("Rozehranou hru mám: skóre %d, patro %d, %d rozhodnutí; pokračuji ve "
                   "volném čase." % (bezi[0] or 0, bezi[1] or 1, bezi[2] or 0))
    return "\n".join(out)
