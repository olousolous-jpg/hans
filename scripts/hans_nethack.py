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
        ("_pray", A.Command.PRAY), ("_quaff", A.Command.QUAFF),
        ("_wear", A.Command.WEAR), ("_puton", A.Command.PUTON), ("_read", A.Command.READ),
        ("_zap", A.Command.ZAP), ("_throw", A.Command.THROW), ("_pay", A.Command.PAY),
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
        here_door = "door" in D(y, x) or (nav is not None and
                                          ("D", int(bl[nh.NLE_BL_DEPTH]), y, x) in nav)
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
            if dy and dx and (here_door or "door" in d or (
                    nav is not None and ("D", int(bl[nh.NLE_BL_DEPTH]), yy, xx) in nav)):
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
            # HANS_NETHACK_DVERE_V1 (6. 11.) — dveře si pamatuje kód: stojí-li na
            # nich postava, popis pole je „Agent“ → cesta šla šikmo z dveří,
            # krok selhal a sousední pole se označila za neprůchozí (hra 4 uvízla)
            if "door" in D(yy, xx):
                nav.add(("D", hl, yy, xx))
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
    # HANS_NETHACK_DVERE_V1 — zamčené dveře (zapamatované) jdou vykopnout
    zam = [(dy, dx) for (dy, dx), _ in sous if "closed door" in D(y + dy, x + dx)
           and ("L", hl, y + dy, x + dx) in nav]
    if zam:
        v["vykopnout dveře"] = ("vykopnout zamčené dveře vedle tebe", ("KICK", zam[0]))
    # jíst jen s jídlem v batohu (jinak „jíst“ dokola bez účinku)
    # NLE_BL_HUNGER: 0 nasycen, 1 bez hladu, 2 hlad, 3 slabost, 4 omdlévá
    if bl[nh.NLE_BL_HUNGER] >= 2 and any(int(o) == int(nh.FOOD_CLASS)
                                         for o in obs["inv_oclasses"]):
        v["jíst"] = ("najíst se (máš hlad a jídlo v batohu)", "EAT")
    # HANS_NETHACK_VIC_V1 (5. 11.) — lektvar, modlitba, ústup
    hp, hpmax = int(bl[nh.NLE_BL_HP]), max(1, int(bl[nh.NLE_BL_HPMAX]))
    cas = int(bl[nh.NLE_BL_TIME])
    if hp < 0.5 * hpmax and _pismeno(obs, lambda s, o: o == int(nh.POTION_CLASS)
                                     and "healing" in s):
        v["vypít lektvar"] = ("vypít léčivý lektvar (máš %d z %d životů)" % (hp, hpmax), "QUAFF")
    _posl_modl = max([a[1] for a in nav if a[0] == "P"] or [-10 ** 6])
    if (hp < max(6, hpmax / 7)) and cas - _posl_modl > 1000 and cas > 300:
        v["modlit se"] = ("pomodlit se k bohu o záchranu (životy kriticky nízko; "
                          "smí se jen zřídka)", "PRAY")
    if "zaútočit" in v and hp < 0.4 * hpmax:
        mon = [(dy, dx) for (dy, dx), g in sous if nh.glyph_is_monster(g)
               and not nh.glyph_is_pet(g)]
        if mon:
            my, mx_ = mon[0]
            kam = [(dy, dx) for (dy, dx) in _SMER
                   if 0 <= y + dy < H and 0 <= x + dx < W
                   and _pruchozi(D(y + dy, x + dx), 0)
                   and not nh.glyph_is_monster(int(gl[y + dy][x + dx]))
                   and max(abs(y + dy - (y + my)), abs(x + dx - (x + mx_))) >= 2]
            if kam:
                v["ustoupit"] = ("ustoupit od příšery (máš málo životů)", ("DIR", kam[0]))
    # HANS_NETHACK_VRSTVA1_V1 (5. 11.) — batoh: obléct, prsten, svitek, hůlka, hodit, zaplatit
    pris = [(dy, dx) for (dy, dx), g in sous if nh.glyph_is_monster(g) and not nh.glyph_is_pet(g)]
    if not pris:
        p = _pismeno(obs, lambda s, o: o == int(nh.ARMOR_CLASS) and "being worn" not in s
                     and any(k in s for k in _MNICH_ZBROJ))
        if p:
            v["obléct"] = ("obléct si zbroj z batohu (%s)" % _jmeno_veci(obs, p), ("USE", "_wear", p, None))
        p = _pismeno(obs, lambda s, o: o == int(nh.RING_CLASS) and "on " not in s.split("(")[-1])
        if p and hp >= hpmax:
            v["nasadit prsten"] = ("nasadit prsten (%s) — neznámý prsten může i škodit"
                                   % _jmeno_veci(obs, p), ("USE", "_puton", p, None))
        p = _pismeno(obs, lambda s, o: o == int(nh.SCROLL_CLASS) and "scare monster" not in s)
        if p:
            v["přečíst svitek"] = ("přečíst svitek (%s) — neznámý svitek může i škodit"
                                   % _jmeno_veci(obs, p), ("USE", "_read", p, None))
    if pris:
        p = _pismeno(obs, lambda s, o: o == int(nh.WAND_CLASS))
        if p:
            v["použít hůlku"] = ("použít kouzelnou hůlku na příšeru vedle (%s)" % _jmeno_veci(obs, p),
                                 ("USE", "_zap", p, pris[0]))
        p = _pismeno(obs, lambda s, o: o == int(nh.WEAPON_CLASS) and "in hand" not in s
                     and any(k in s for k in ("dagger", "shuriken", "dart", "knife")))
        if p:
            v["hodit"] = ("hodit po příšeře (%s)" % _jmeno_veci(obs, p), ("USE", "_throw", p, pris[0]))
    if any("unpaid" in bytes(s).decode("latin1") for s in obs["inv_strs"]):
        v["zaplatit"] = ("zaplatit obchodníkovi za věci, které neseš (jinak tě napadne)",
                         ("USE", "_pay", None, None))
    # HANS_NETHACK_VOLBY_V1 — pasivní volby jen tam, kde dávají smysl (s modelem
    # 5. 11.: jinak „hledat/odpočinout“ dokola a stál na místě)
    if "prozkoumat" not in v and "ke schodům" not in v and "dolů" not in v:
        # HANS_NETHACK_DVERE_V1 — nemá kam jít a má pole označená za neprůchozí →
        # jednou za 100 herních tahů je zapomenout a zkusit znovu (označení umí být
        # omyl: příšera zmizela, dveře se otevřely)
        blok = {a for a in nav if a[0] == "B" and a[1] == hl}
        _posl = max([a[2] for a in nav if a[0] == "R" and a[1] == hl] or [-10 ** 6])
        if blok and cas - _posl >= 100:
            nav -= blok
            nav.add(("R", hl, cas))
            return volby(obs, nav)
        # HANS_NETHACK_HLEDANI_V1 (6. 11.) — skryté chodby se hledají u slepého
        # konce chodby nebo u zdi, ne uprostřed místnosti (hra 4: 120× na jednom poli)
        def _orth(yy, xx):
            return [(yy + a, xx + b) for a, b in ((-1, 0), (1, 0), (0, -1), (0, 1))
                    if 0 <= yy + a < H and 0 <= xx + b < W]
        def _misto(yy, xx, slepy):
            if ("H", hl, yy, xx) in nav or not _pruchozi(D(yy, xx), 0) or "door" in D(yy, xx):
                return False
            sous_ = _orth(yy, xx)
            if slepy:
                return ("corridor" in D(yy, xx)
                        and sum(_pruchozi(D(a, b), 0) for a, b in sous_) <= 1)
            return any(not D(a, b) or D(a, b).endswith("wall") for a, b in sous_)
        cil_s = None
        for slepy in (True, False):
            f = (lambda yy, xx, s=slepy: _misto(yy, xx, s))
            if f(y, x) or _bfs(obs, f, nav=nav) is not None:
                cil_s = f
                break
        if cil_s is None and any(a[0] == "H" and a[1] == hl for a in nav):
            nav -= {a for a in nav if a[0] == "H" and a[1] == hl}   # další kolo
        v["hledat"] = ("hledat skryté dveře a chodby (není kam jít)",
                       ("SEARCH", cil_s) if cil_s else "SEARCH")
    if bl[nh.NLE_BL_HP] < 0.5 * max(1, bl[nh.NLE_BL_HPMAX]) and "zaútočit" not in v:
        v["odpočinout"] = ("chvíli odpočívat (máš málo životů)", "REST")
    return v


_MNICH_ZBROJ = ("boots", "helm", "helmet", "hat", "cap", "gloves", "gauntlets", "cloak",
                "robe", "fedora", "cornuthaum")   # mnich: bez brnění na tělo a štítu


def _jmeno_veci(obs, pis) -> str:
    for s, l in zip(obs["inv_strs"], obs["inv_letters"]):
        if int(l) and chr(int(l)) == pis:
            return bytes(s).decode("latin1").strip("\x00")[:50]
    return pis


def _pismeno(obs, pred):
    """Písmeno první věci v batohu, pro kterou pred(popis, třída) platí."""
    for s, l, o in zip(obs["inv_strs"], obs["inv_letters"], obs["inv_oclasses"]):
        if int(l) and pred(bytes(s).decode("latin1").strip("\x00"), int(o)):
            return chr(int(l))
    return None


_POUCENI = {"hlad": "Když máš hlad, najez se — minule jsi umřel hlady.",
            "zabit": "Při málo životech ustup nebo vypij léčivý lektvar — minule tě zabila příšera.",
            "utopil": "Pozor na vodu — minule ses utopil."}


def _hp(stav: str):
    import re
    m = re.search(r"Životy (-?\d+)/(\d+)", stav or "")
    return (int(m.group(1)), int(m.group(2))) if m else (None, None)


def kniha_zapis(c, hid: int, umrel: bool) -> None:
    """HANS_NETHACK_KNIHA_V1 — po hře sečte z deníku tahů výsledky každé volby
    (počet, body, ztracené životy, smrt do 3 rozhodnutí) do `nethack_volby`."""
    import os as _os
    c.execute("CREATE TABLE IF NOT EXISTS nethack_volby (volba TEXT PRIMARY KEY, pocet INTEGER, "
              "body REAL, ztrata_hp INTEGER, smrti INTEGER)")
    p = _os.path.join("data", "nethack", "hry", "%d.jsonl" % hid)
    try:
        recs = [json.loads(x) for x in open(p, encoding="utf-8") if x.strip()]
    except Exception:
        return
    agg = {}
    for i, r in enumerate(recs):
        a = agg.setdefault(r.get("volba"), [0, 0.0, 0, 0])
        a[0] += 1
        a[1] += float(r.get("body") or 0)
        h0, _ = _hp(r.get("stav"))
        h1, _ = _hp(r.get("po_stav"))
        if h0 is not None and h1 is not None and h1 < h0:
            a[2] += h0 - h1
        if umrel and i >= len(recs) - 3:
            a[3] += 1
    for vb, (n, b, z, s) in agg.items():
        if not vb:
            continue
        c.execute("INSERT INTO nethack_volby VALUES (?,?,?,?,?) ON CONFLICT(volba) DO UPDATE SET "
                  "pocet=pocet+excluded.pocet, body=body+excluded.body, "
                  "ztrata_hp=ztrata_hp+excluded.ztrata_hp, smrti=smrti+excluded.smrti",
                  (vb, n, b, z, s))
    c.commit()


def kniha(c, kolik: int = 3) -> list:
    """Věty pro zadání: co se Hansovi vyplácí a co ho stojí životy (počítá kód)."""
    try:
        rows = c.execute("SELECT volba, pocet, body, ztrata_hp, smrti FROM nethack_volby "
                         "WHERE pocet >= 5").fetchall()
    except sqlite3.OperationalError:
        return []
    if not rows:
        return []
    out = []
    nej = max(rows, key=lambda r: r[2] / r[1])
    if nej[2] > 0:
        out.append("„%s“ ti zatím přineslo nejvíc bodů (průměrně %.1f za rozhodnutí)."
                   % (nej[0], nej[2] / nej[1]))
    zl = max(rows, key=lambda r: r[3] / r[1])
    if zl[3] > 0:
        out.append("Při „%s“ ztrácíš nejvíc životů (průměrně %.1f)." % (zl[0], zl[3] / zl[1]))
    sm = [r for r in rows if r[4]]
    if sm:
        s = max(sm, key=lambda r: r[4])
        out.append("Těsně před koncem hry jsi nejčastěji volil „%s“ (%d×)." % (s[0], s[4]))
    return out[:kolik]


def ze_studia(c) -> list:
    """HANS_NETHACK_STUDIUM_V1 — výtah z Hansových studijních poznámek o NetHacku."""
    try:
        rows = c.execute("SELECT coalesce(data, note, '') FROM diary WHERE event_type IN "
                         "('study_mastery','study_note') AND (title LIKE '%NetHack%' OR "
                         "title LIKE '%nethack%') ORDER BY ts DESC LIMIT 2").fetchall()
    except sqlite3.OperationalError:
        return []
    return ["Ze svého studia víš: " + " ".join((r[0] or "").split())[:350]
            for r in rows if (r[0] or "").strip()][:1]


def pouceni(c) -> list:
    """HANS_NETHACK_POUCENI_V1 — připomínky z příčin konce posledních 5 her (kód)."""
    rows = [r[0] or "" for r in c.execute(
        "SELECT duvod FROM nethack_hry WHERE stav='konec' ORDER BY id DESC LIMIT 5")]
    out = []
    for k, veta in _POUCENI.items():
        if any(k in r for r in rows) and veta not in out:
            out.append(veta)
    return out[:2] + kniha(c) + ze_studia(c)


def _hans_akce(config, obs, historie, stoji, nav=None, zakaz=None, pouceni=None) -> tuple:
    from scripts.ollama_client import ollama_generate
    v = volby(obs, nav)
    # HANS_NETHACK_ZAKAZ_V1 — volba, která 3× po sobě nic nezměnila, na chvíli pryč
    for s in list(zakaz or ()):
        if s in v and len(v) > 1:
            del v[s]
    stitky = list(v)
    if len(stitky) == 1:     # HANS_NETHACK_DVERE_V1 — není z čeho volit, model netřeba
        return stitky[0], True, v
    sysp = ("Hraješ hru NetHack: procházíš podzemí, sbíráš věci, bojuješ s příšerami a "
            "hledáš schody dolů, abys šel hlouběji. Rozhoduješ, CO udělat; cestu najdeš "
            "sám. Odpověz JEDNOU volbou přesně ze seznamu (jen její název).")
    pr = "%s\nZpráva hry: %s\n%s\n" % (_stav(obs), _zprava(obs) or "-", popis(obs))
    inv = [bytes(s).decode("latin1").strip("\x00") for s, l in
           zip(obs["inv_strs"], obs["inv_letters"]) if int(l)]
    if inv:
        pr += "Máš u sebe: %s\n" % "; ".join(inv[:12])
    if pouceni:
        pr += "Z minulých her víš: %s\n" % " ".join(pouceni)
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
    # nevyřízená otázka z minula (allow_all_yn_questions) → odklepnout / zrušit
    for _ in range(6):
        tt = _tty(obs)
        if "--More--" in tt:
            a = idx["_more"]
        elif "[yn" in tt or "What do you want" in tt or "In what direction" in tt:
            a = idx["_esc"]
        else:
            break
        obs, r, d, tr, _ = env.step(a)
        sk += float(r or 0)
        if d or tr:
            return obs, sk, True

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
    # HANS_NETHACK_HLEDANI_V1 — na místě k hledání hledat 15 tahů, jinak k němu
    # dojít stejnou chůzí jako průzkum (šikmo ze dveří, příšera v cestě…)
    if isinstance(co, tuple) and co[0] == "SEARCH":
        _b = obs["blstats"]
        yy, xx, hh = int(_b[nh.NLE_BL_Y]), int(_b[nh.NLE_BL_X]), int(_b[nh.NLE_BL_DEPTH])
        if co[1](yy, xx):
            for _ in range(15):
                krok(idx["hledat"])
            if nav is not None:
                nav.add(("H", hh, yy, xx))
            return obs, sk, konec
        co = co[1]
    if co == "DOWN":
        krok(idx["dolů"])
    elif isinstance(co, tuple) and co[0] == "USE":
        # HANS_NETHACK_VRSTVA1_V1 — příkaz → písmeno věci → směr; ostatní otázky ESC
        _, prikaz, pis, smer = co
        obs, r, d, tr, _ = env.step(idx[prikaz])
        sk += float(r or 0)
        konec = konec or d or tr
        for _i in range(4):
            if konec:
                break
            tt = _tty(obs)
            if pis and "What do you want to" in tt and ("_k_" + pis) in idx:
                a, pis = idx["_k_" + pis], None
            elif smer and "In what direction" in tt:
                a, smer = dir_akce[smer], None
            elif "--More--" in tt:
                a = idx["_more"]
            elif "[yn" in tt and prikaz == "_pay":
                a = idx["_k_y"]
            else:
                break
            obs, r, d, tr, _ = env.step(a)
            sk += float(r or 0)
            konec = konec or d or tr
        krok(idx["_esc"]) if not konec and ("What do you want" in _tty(obs)
                                           or "In what direction" in _tty(obs)) else None
    elif co == "QUAFF":
        obs, r, d, tr, _ = env.step(idx["_quaff"])
        konec = konec or d or tr
        p = _pismeno(obs, lambda s, o: o == int(nh.POTION_CLASS) and "healing" in s)
        if not konec and p and ("_k_" + p) in idx and "drink" in _tty(obs):
            krok(idx["_k_" + p])
        else:
            krok(idx["_esc"])
    elif co == "PRAY":
        obs, r, d, tr, _ = env.step(idx["_pray"])
        konec = konec or d or tr
        if not konec and "Are you sure you want to pray" in _tty(obs):
            krok(idx["_k_y"])
        else:
            krok(idx["_more"]) if "--More--" in _tty(obs) else None
        if nav is not None:
            nav.add(("P", int(obs["blstats"][nh.NLE_BL_TIME])))
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
                if nav is not None and "locked" in _zprava(obs):
                    nav.add(("L", int(obs["blstats"][nh.NLE_BL_DEPTH]), y + d[0], x + d[1]))
                break
    elif isinstance(co, tuple) and co[0] == "DIR":
        krok(dir_akce[co[1]])
    elif isinstance(co, tuple) and co[0] == "KICK":
        obs, r, dn, tr, _ = env.step(idx["vykopnout"])
        sk += float(r or 0)
        konec = konec or dn or tr
        if not konec and "In what direction" in _tty(obs):
            krok(dir_akce[co[1]])
        else:
            krok(idx["_esc"])
    elif callable(co):
        hp0 = int(obs["blstats"][nh.NLE_BL_HP])
        for _ in range(max_kroku):
            d = _bfs(obs, co, nav=nav)
            if d is None or konec:
                break
            _b0 = obs["blstats"]
            p0 = (int(_b0[nh.NLE_BL_Y]), int(_b0[nh.NLE_BL_X]), int(_b0[nh.NLE_BL_DEPTH]))
            if nav is not None and "door" in bytes(
                    obs["screen_descriptions"][p0[0] + d[0]][p0[1] + d[1]]).decode("latin1"):
                nav.add(("D", p0[2], p0[0] + d[0], p0[1] + d[1]))
            krok(dir_akce[d])
            _b1 = obs["blstats"]
            if nav is not None and (int(_b1[nh.NLE_BL_Y]), int(_b1[nh.NLE_BL_X]),
                                    int(_b1[nh.NLE_BL_DEPTH])) == p0:
                # zeď/balvan ano, příšera (boj) ne
                _g = int(obs["glyphs"][p0[0] + d[0]][p0[1] + d[1]])
                _cd = bytes(obs["screen_descriptions"][p0[0] + d[0]][p0[1] + d[1]]).decode("latin1")
                if "trap" in _zprava(obs) or "stuck" in _zprava(obs):
                    pass               # past drží postavu, pole je průchozí
                elif "door" in _cd:      # dveře ≠ zeď: zamčené zapamatovat, jinak nic
                    if "locked" in _zprava(obs):
                        nav.add(("L", p0[2], p0[0] + d[0], p0[1] + d[1]))
                    if "diagonally" in _zprava(obs):
                        nav.add(("D", p0[2], p0[0] + d[0], p0[1] + d[1]))
                elif "diagonally into" in _zprava(obs):
                    nav.add(("D", p0[2], p0[0] + d[0], p0[1] + d[1]))
                elif "diagonally" in _zprava(obs):
                    nav.add(("D", p0[2], p0[0], p0[1]))   # stojí ve dveřích
                elif not nh.glyph_is_monster(_g) and (
                        not _zprava(obs) or "boulder" in _zprava(obs) or "vain" in _zprava(obs)):
                    # jen zeď (hra mlčí) nebo balvan; „dog is in the way“ apod. ne
                    nav.add(("B", p0[2], p0[0] + d[0], p0[1] + d[1]))
                break
            _zp = _zprava(obs)
            if "swap places" in _zp:     # výměna se psem chůzi nepřerušuje
                _zp = ""
            if (_zp or int(obs["blstats"][nh.NLE_BL_HP]) < hp0
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
    try:
        kniha_zapis(c, hid, duvod not in ("přerušeno restartem",))
    except Exception as e:
        _log.warning("HANS_NETHACK_KNIHA_V1: %s", e)
    _log.info("HANS_NETHACK_V1: hra %d skončila — %s; skóre %d, patro %d, %d rozhodnutí",
              hid, duvod, skore, int(h.get("max_hloubka", 1)), h.get("kroky", 0))
    try:
        h["env"].close()
    except Exception:
        pass
    _HRA.clear()


def _duvod_smrti(obs, zpravy=None) -> str:
    t = _tty(obs) + "\n" + "\n".join(zpravy or [])
    import re
    zl = " ".join(zpravy or []).lower()
    if "starv" in zl or "faint" in zl:
        return "umřel hlady"
    if "drown" in zl:
        return "utopil se"
    _ut = [z for z in (zpravy or []) if re.search(r"\b(bites|hits|stings|kicks|claws|"
                                                    r"touches|butts|strikes)\b", z)]
    if _ut:
        return "zabit (%s)" % _ut[-1][:60]
    m = re.search(r"killed by ([^\n]{3,60})", t)
    if m:
        return "zabit (%s)" % m.group(1).strip()
    for k, v in (("starv", "umřel hlady"), ("drown", "utopil se"), ("quit", "vzdal")):
        if k in t.lower():
            return v
    return "smrt"


def mapa(obs) -> str:
    """Mapa jako text (řádky 1–21 obrazovky) + stavový řádek."""
    r = _tty(obs).splitlines()
    return "\n".join(x.rstrip() for x in r[1:24])


def _denik_tahu(h, volba, ok, volby_, pred, obs, r) -> None:
    """HANS_NETHACK_ZAZNAM_V1 — data/nethack/hry/<id>.jsonl: každé rozhodnutí
    (co viděl, z čeho volil, co zvolil, jak dopadlo); mapa každé 10. rozhodnutí."""
    import os as _os
    try:
        d = _os.path.join("data", "nethack", "hry")
        _os.makedirs(d, exist_ok=True)
        rec = {"ts": round(time.time(), 1), "n": h.get("kroky", 0), "stav": pred[0],
               "zprava": pred[1], "volby": volby_, "volba": volba, "platna": ok,
               "po_zprava": _zprava(obs), "po_stav": _stav(obs), "body": float(r or 0)}
        if h.get("kroky", 0) % 10 == 0:
            rec["mapa"] = mapa(obs)
        h["posledni_mapa"] = mapa(obs)
        # web běží v jiném procesu → poslední stav i do souboru
        _tmp = _os.path.join("data", "nethack", "posledni.json.tmp")
        json.dump({"id": h["id"], "ts": time.time(), "mapa": h["posledni_mapa"],
                   "historie": list(h.get("historie", []))[-8:] + [volba],
                   "zprava": _zprava(obs), "stav": _stav(obs)},
                  open(_tmp, "w", encoding="utf-8"), ensure_ascii=False)
        _os.replace(_tmp, _os.path.join("data", "nethack", "posledni.json"))
        with open(_os.path.join(d, "%d.jsonl" % h["id"]), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:
        _log.debug("HANS_NETHACK_ZAZNAM_V1: %s", e)


def stav_ted() -> dict:
    """Pro web: rozehraná hra v paměti (mapa, posledních 8 rozhodnutí)."""
    if not _HRA:
        return {}
    return {"id": _HRA.get("id"), "mapa": _HRA.get("posledni_mapa") or "",
            "historie": list(_HRA.get("historie", []))[-8:],
            "zpravy": list(_HRA.get("zpravy", []))[-4:]}


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
            # přerušená (restart) pauzu nespouští — jen dohraná
            k = c.execute("SELECT max(konec_ts) FROM nethack_hry WHERE stav='konec'").fetchone()[0]
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
            # HANS_NETHACK_ZAZNAM_V1 — celý záznam hry (ttyrec, přehratelný) + deník tahů
            import os as _os
            _tdir = _os.path.join("data", "nethack", "ttyrec")
            _os.makedirs(_tdir, exist_ok=True)
            env = gym.make("NetHackScore-v0", actions=tuple(a for _, a in akce),
                           save_ttyrec_every=1, savedir=_tdir, max_episode_steps=200000,
                           # jinak NLE samo ruší „What do you want to read/wear/…?“
                           allow_all_yn_questions=True)
            obs, _ = env.reset()
            cur = c.execute("INSERT INTO nethack_hry (start_ts, posledni_ts, stav, sezeni, "
                            "kroky, neplatne) VALUES (?,?,?,0,0,0)", (ted, ted, "bezi"))
            c.commit()
            _HRA.update({"pouceni": pouceni(c), "env": env, "obs": obs, "id": cur.lastrowid,
                         "akce": akce,
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
                                      h["zakaz"], h.get("pouceni"))
            _stav0 = (_zprava(obs), int(obs["blstats"][nh.NLE_BL_TIME]))
            if volba is None:
                break     # model nedostupný → pokračuje příště
            h["neplatne"] += 0 if ok else 1
            _pred = (_stav(obs), _zprava(obs))
            obs, r, done = _proved(env, idx, obs, volba, v, nav=h["nav"])
            _denik_tahu(h, volba, ok, list(v), _pred, obs, r)
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
            _zp = _zprava(obs)
            if _zp:
                h["zpravy"] = (h.get("zpravy", []) + [_zp])[-6:]
            if done:
                break
        h["posledni"] = time.time()
        if done:
            _konec_hry(config, c, h["id"], h["obs"], _duvod_smrti(h["obs"], h.get("zpravy")))
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
