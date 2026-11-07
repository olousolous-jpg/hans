"""HANS_SACHY_V1 (5. 11. 2026) — Hans hraje šachy s Koláčem ve volném čase.

Proč: Severka (identita) vidí skoro jen četbu → role pořád „archivář“. Šachy jsou
činnost s měřitelným výsledkem, který si Hans nemůže vymyslet (výsledek partie
a chyby počítá engine).

Jak:
- Hans táhne jazykovým modelem (hans-czech, rezidentní → bez VRAM navíc): dostane
  pozici, poslední tahy, seznam povolených tahů a připomínky ze svých chyb.
- Koláč = Stockfish se silou `p`: s pravděpodobností p tah enginu, jinak náhodný
  povolený tah. Po prohře Hanse p klesne, po výhře stoupne → vývoj p = Hansův pokrok.
- Po každém Hansově tahu engine spočítá ztrátu; velké chyby kód roztřídí
  (přehlédnutý mat, nekrytá figura, přehlédnuté braní) a nejčastější se Hansovi
  připomenou v další partii.
- Sezení má strop (`max_minut`, `max_tahu`); nedohraná partie se uloží a pokračuje
  v dalším volném čase, dokud neskončí (uživatel 5. 11.).
- Konec partie → deník `sachy_partie` (čte reflexe a Severka).
Vypínač `sachy.enabled`. Volá se ze smyčky Koláčova dialogu (`hans_dialog`).
"""
from __future__ import annotations

import json
import logging
import random
import sqlite3
import threading
import time

_log = logging.getLogger("hans_sachy")
_LOCK = threading.Lock()

_DEF = {
    "enabled": True,
    "engine": "/usr/games/stockfish",
    "max_minut": 4,            # jedno sezení
    "max_tahu": 30,            # Hansových tahů za sezení
    "pauza_min": 30,           # mezi sezeními rozehrané partie
    "nova_po_h": 6,            # nová partie po dohrané
    "max_pultahu": 300,        # pak rozhodne hodnocení enginu
    "kolac_sila_start": 0.3,
    "kolac_sila_krok": 0.05,
    "chyba_cp": 200,           # ztráta, od které je tah chyba
}

_MOTIV_VETA = {
    "mat": "Před tahem zkontroluj, jestli tím soupeři neotevřeš mat.",
    "kryti": "Nenechávej své figury napadené a nekryté.",
    "brani": "Podívej se, jestli nemůžeš soupeři něco sebrat zadarmo.",
    "slaby": "Hraj klidněji — vyvíjej figury a chraň krále.",
}
_MOTIV_POPIS = {"mat": "přehlédnutý mat", "kryti": "nekrytá figura",
                "brani": "přehlédnuté braní", "slaby": "slabý tah"}
_HODNOTA = {1: 1, 2: 3, 3: 3, 4: 5, 5: 9, 6: 0}


def _cfg(config: dict) -> dict:
    d = dict(_DEF)
    d.update((config or {}).get("sachy", {}) or {})
    return d


def _db(config: dict) -> sqlite3.Connection:
    c = sqlite3.connect((config or {}).get("diary_db") or "data/hans_diary.db", timeout=10.0)
    c.execute("CREATE TABLE IF NOT EXISTS sachy_partie (id INTEGER PRIMARY KEY, "
              "start_ts REAL, posledni_ts REAL, konec_ts REAL, hans_barva TEXT, "
              "kolac_sila REAL, tahy TEXT, stav TEXT, vysledek TEXT, duvod TEXT, "
              "sezeni INTEGER DEFAULT 0, neplatne INTEGER DEFAULT 0)")
    c.execute("CREATE TABLE IF NOT EXISTS sachy_chyby (id INTEGER PRIMARY KEY, "
              "partie INTEGER, pultah INTEGER, tah TEXT, lepsi TEXT, ztrata INTEGER, "
              "motiv TEXT, ts REAL)")
    return c


def _model(config: dict) -> str:
    return str(((config or {}).get("sachy", {}) or {}).get("model")
               or ((config or {}).get("evening_reflection", {}) or {}).get("voice_model")
               or "hans-czech:latest")


def kolac_sila(c) -> float:
    """Síla pro další partii (po výsledku se posune), jinak síla poslední partie."""
    try:
        r = c.execute("SELECT hodnota FROM sachy_stav WHERE klic='kolac_sila'").fetchone()
        if r:
            return float(r[0])
    except sqlite3.OperationalError:
        pass
    r = c.execute("SELECT kolac_sila FROM sachy_partie ORDER BY id DESC LIMIT 1").fetchone()
    return float(r[0]) if r and r[0] is not None else None


def pripominky(c, n_partii: int = 5, kolik: int = 2) -> list:
    """Nejčastější motivy chyb z posledních partií → věty do zadání."""
    rows = c.execute(
        "SELECT motiv, count(*) FROM sachy_chyby WHERE partie IN (SELECT id FROM "
        "sachy_partie ORDER BY id DESC LIMIT ?) GROUP BY 1 ORDER BY 2 DESC", (n_partii,)
    ).fetchall()
    return [_MOTIV_VETA[m] for m, _n in rows if m in _MOTIV_VETA][:kolik]


def _skore(eng, board, chess, hloubka=8):
    info = eng.analyse(board, chess.engine.Limit(depth=hloubka))
    return info["score"], info.get("pv") or []


def _motiv(chess, pred, po, best_move, tah, board_po, barva) -> str:
    """Kód roztřídí chybu (ne model)."""
    s_po = po.pov(barva)
    if s_po.is_mate() and (s_po.mate() or 0) < 0:
        return "mat"
    for sq, pc in board_po.piece_map().items():
        if pc.color == barva and pc.piece_type not in (chess.PAWN, chess.KING):
            if board_po.is_attacked_by(not barva, sq) and not board_po.is_attacked_by(barva, sq):
                return "kryti"
    if best_move is not None and best_move != tah:
        b0 = board_po.copy()
        b0.pop()
        if b0.is_capture(best_move):
            return "brani"
    return "slaby"


def _hans_tah(config, board, chess, pripom, cfg) -> tuple:
    """Vrátí (tah, platny). None = model nedostupný (sezení skončí)."""
    from scripts.ollama_client import ollama_generate
    legal = [board.san(m) for m in board.legal_moves]
    hist = chess.Board().variation_san(board.move_stack) if board.move_stack else "-"
    hist = " ".join(hist.split()[-20:])
    barva = "bílými" if board.turn == chess.WHITE else "černými"
    sysp = ("Hraješ šachy %s proti Koláčovi. Dostaneš pozici a seznam povolených tahů. "
            "Odpověz JEDINÝM tahem přesně ze seznamu, nic jiného." % barva)
    pr = "Pozice (FEN): %s\nPoslední tahy: %s\n" % (board.fen(), hist)
    if pripom:
        pr += "Připomínky z tvých minulých partií: %s\n" % " ".join(pripom)
    pr += "Povolené tahy: %s\nTvůj tah:" % " ".join(legal)
    for pokus in range(2):
        raw = ollama_generate(_model(config), pr, system=sysp, config=config, timeout=60,
                              options={"temperature": 0.3 + 0.3 * pokus,
                                       "num_predict": 12, "num_ctx": 2048})
        if raw is None:
            return None, False
        for w in raw.replace("\n", " ").split():
            w = w.strip(" .,:;*`\"'()")
            if w in legal:
                return board.parse_san(w), True
    return random.choice(list(board.legal_moves)), False


def _kolac_tah(eng, board, chess, sila: float):
    if random.random() < sila:
        return eng.play(board, chess.engine.Limit(time=0.1)).move
    return random.choice(list(board.legal_moves))


def _vysledek(board, chess, hans_barva, eng=None, max_pultahu=300) -> tuple:
    """(vysledek z pohledu Hanse, duvod) nebo (None, None) když se hraje dál."""
    if board.is_game_over(claim_draw=True):
        o = board.outcome(claim_draw=True)
        if o.winner is None:
            return "remíza", o.termination.name.lower()
        return ("výhra" if o.winner == hans_barva else "prohra"), o.termination.name.lower()
    if len(board.move_stack) >= max_pultahu and eng is not None:
        sc, _ = _skore(eng, board, chess, 12)
        cp = sc.pov(hans_barva).score(mate_score=10000)
        if cp > 300:
            return "výhra", "rozhodnuto hodnocením"
        if cp < -300:
            return "prohra", "rozhodnuto hodnocením"
        return "remíza", "rozhodnuto hodnocením"
    return None, None


def _do_deniku(config, c, pid, board, chess):
    r = c.execute("SELECT hans_barva, vysledek, duvod, kolac_sila, sezeni, neplatne "
                  "FROM sachy_partie WHERE id=?", (pid,)).fetchone()
    barva, vys, duvod, sila, sez, nepl = r
    chyb = c.execute("SELECT motiv, count(*) FROM sachy_chyby WHERE partie=? GROUP BY 1 "
                     "ORDER BY 2 DESC", (pid,)).fetchall()
    tahu = (len(board.move_stack) + 1) // 2
    _duv = {"checkmate": "matem", "stalemate": "patem", "insufficient_material":
            "pro nedostatek materiálu", "threefold_repetition": "opakováním pozice",
            "fivefold_repetition": "opakováním pozice", "fifty_moves":
            "pravidlem 50 tahů", "seventyfive_moves": "pravidlem 75 tahů"}.get(duvod, duvod)
    note = ("Hrál jsem %s. Partie skončila po %d tazích: %s (%s). Hráli jsme ji ve %d "
            "sezeních. Koláčova síla v té partii: %d %%." % (
                "bílými" if barva == "white" else "černými", tahu, vys, _duv, sez,
                round(100 * (sila or 0))))
    if chyb:
        note += " Moje velké chyby: " + ", ".join(
            "%s %d×" % (_MOTIV_POPIS.get(m, m), n) for m, n in chyb) + "."
    if nepl:
        note += " Neplatných tahů (zahrál za mě náhodný tah): %d." % nepl
    try:
        c.execute("INSERT INTO diary (ts, event_type, title, note, data) VALUES (?,?,?,?,?)",
                  (time.time(), "sachy_partie", "Šachy s Koláčem: %s po %d tazích" % (vys, tahu),
                   note, json.dumps({"partie": pid, "pgn": chess.Board().variation_san(
                       board.move_stack)}, ensure_ascii=False)))
    except Exception as e:
        _log.warning("HANS_SACHY_V1: zápis do deníku selhal: %s", e)


def _je_cas(c, cfg, ted, config=None) -> tuple:
    """(id rozehrané partie nebo 0 pro novou, nebo None když není čas)."""
    r = c.execute("SELECT id, posledni_ts FROM sachy_partie WHERE stav='bezi' "
                  "ORDER BY id DESC LIMIT 1").fetchone()
    if r:
        return r[0] if ted - float(r[1] or 0) >= cfg["pauza_min"] * 60 else None
    k = c.execute("SELECT max(konec_ts) FROM sachy_partie").fetchone()[0]
    # HANS_MOTIVACE_V1 — kde se Hans zlepšuje, dostane víc času (0,5–2×)
    try:
        from scripts.hans_motivace import vaha as _vaha
        _m = _vaha(config, "sachy")
    except Exception:
        _m = 1.0
    if k and ted - float(k) < cfg["nova_po_h"] * 3600 / _m:
        return None
    return 0


def tick(config: dict, ted: float = None) -> str:
    """Zavolat z volného času. Odehraje jedno sezení, když je čas. Vrací stav."""
    cfg = _cfg(config)
    if not cfg.get("enabled"):
        return "vypnuto"
    try:
        from scripts.ollama_client import game_mode_on
        if game_mode_on():
            return "hra"
    except Exception:
        pass
    if not _LOCK.acquire(blocking=False):
        return "běží"
    try:
        return _sezeni(config, cfg, ted or time.time())
    except Exception as e:
        _log.warning("HANS_SACHY_V1: sezení selhalo: %s", e)
        return "chyba"
    finally:
        _LOCK.release()


def _sezeni(config, cfg, ted) -> str:
    c = _db(config)
    try:
        pid = _je_cas(c, cfg, ted, config)
        if pid is None:
            return "není čas"
        try:
            import chess
            import chess.engine
        except ImportError:
            _log.warning("HANS_SACHY_V1: chybí knihovna python-chess")
            return "chybí chess"
        if pid == 0:
            pred = c.execute("SELECT hans_barva FROM sachy_partie ORDER BY id DESC "
                             "LIMIT 1").fetchone()
            barva = "black" if (pred and pred[0] == "white") else "white"
            sila = kolac_sila(c)
            sila = cfg["kolac_sila_start"] if sila is None else sila
            cur = c.execute("INSERT INTO sachy_partie (start_ts, posledni_ts, hans_barva, "
                            "kolac_sila, tahy, stav) VALUES (?,?,?,?,?,?)",
                            (ted, ted, barva, sila, "", "bezi"))
            pid = cur.lastrowid
            c.commit()
            _log.info("HANS_SACHY_V1: nová partie %d, Hans hraje %s, Koláčova síla %.2f",
                      pid, "bílými" if barva == "white" else "černými", sila)
        barva_s, sila, tahy_s, sez, nepl = c.execute(
            "SELECT hans_barva, kolac_sila, tahy, sezeni, neplatne FROM sachy_partie "
            "WHERE id=?", (pid,)).fetchone()
        hb = chess.WHITE if barva_s == "white" else chess.BLACK
        board = chess.Board()
        for u in (tahy_s or "").split():
            board.push_uci(u)
        pripom = pripominky(c)
        eng = chess.engine.SimpleEngine.popen_uci(cfg["engine"])
        try:
            eng.configure({"Threads": 1, "Hash": 32})
            t0, nh, dostupny = time.time(), 0, True
            while True:
                vys, duvod = _vysledek(board, chess, hb, eng, cfg["max_pultahu"])
                if vys:
                    break
                if nh >= cfg["max_tahu"] or time.time() - t0 > cfg["max_minut"] * 60:
                    break
                if board.turn == hb:
                    pred, pv = _skore(eng, board, chess)
                    tah, platny = _hans_tah(config, board, chess, pripom, cfg)
                    if tah is None:
                        dostupny = False
                        break
                    nepl += 0 if platny else 1
                    san = board.san(tah)
                    board.push(tah)
                    po, _ = _skore(eng, board, chess)
                    ztr = (pred.pov(hb).score(mate_score=10000)
                           - po.pov(hb).score(mate_score=10000))
                    if ztr >= cfg["chyba_cp"]:
                        best = pv[0] if pv else None
                        mot = _motiv(chess, pred, po, best, tah, board, hb)
                        b0 = board.copy()
                        b0.pop()
                        c.execute("INSERT INTO sachy_chyby (partie, pultah, tah, lepsi, "
                                  "ztrata, motiv, ts) VALUES (?,?,?,?,?,?,?)",
                                  (pid, len(board.move_stack), san,
                                   b0.san(best) if best else "", int(ztr), mot, time.time()))
                        # HANS_SACHY_COMMIT_V1 (6. 11.) — uložit hned: otevřený zápis
                        # přes volání modelu zamykal deník ostatním (23:01 9× locked)
                        c.commit()
                    nh += 1
                else:
                    board.push(_kolac_tah(eng, board, chess, float(sila)))
            tahy = " ".join(m.uci() for m in board.move_stack)
            if vys:
                krok = cfg["kolac_sila_krok"]
                nova = float(sila) + (krok if vys == "výhra" else -krok if vys == "prohra" else 0)
                c.execute("UPDATE sachy_partie SET tahy=?, posledni_ts=?, konec_ts=?, stav=?, "
                          "vysledek=?, duvod=?, sezeni=?, neplatne=? WHERE id=?",
                          (tahy, time.time(), time.time(), "konec", vys, duvod, sez + 1,
                           nepl, pid))
                c.commit()
                _do_deniku(config, c, pid, board, chess)
                _log.info("HANS_SACHY_V1: partie %d skončila — %s (%s), %d tahů; "
                          "Koláčova síla příště %.2f", pid, vys, duvod,
                          (len(board.move_stack) + 1) // 2, max(0.0, min(1.0, nova)))
                _uloz_silu(c, max(0.0, min(1.0, nova)))
                return "konec:" + vys
            c.execute("UPDATE sachy_partie SET tahy=?, posledni_ts=?, sezeni=?, neplatne=? "
                      "WHERE id=?", (tahy, time.time(), sez + 1, nepl, pid))
            c.commit()
            _log.info("HANS_SACHY_V1: partie %d pokračuje příště (%d půltahů, sezení %d%s)",
                      pid, len(board.move_stack), sez + 1,
                      "" if dostupny else ", model nedostupný")
            return "pokračuje"
        finally:
            eng.quit()
    finally:
        c.close()


def _uloz_silu(c, sila: float) -> None:
    """Síla pro další partii se drží v tabulce stavu (sachy_stav)."""
    c.execute("CREATE TABLE IF NOT EXISTS sachy_stav (klic TEXT PRIMARY KEY, hodnota TEXT)")
    c.execute("INSERT OR REPLACE INTO sachy_stav VALUES ('kolac_sila', ?)", (str(sila),))
    c.commit()


def prehled(config: dict) -> str:
    """Pro příkaz /sachy a pro Severku: bilance a vývoj."""
    c = _db(config)
    try:
        rows = c.execute("SELECT vysledek, count(*) FROM sachy_partie WHERE stav='konec' "
                         "GROUP BY 1").fetchall()
        d = dict(rows)
        bezi = c.execute("SELECT id, tahy, hans_barva FROM sachy_partie WHERE stav='bezi' "
                         "ORDER BY id DESC LIMIT 1").fetchone()
        sily = [r[0] for r in c.execute("SELECT kolac_sila FROM sachy_partie "
                                        "ORDER BY id").fetchall()]
        pr = pripominky(c, kolik=3)
        _od = c.execute("SELECT min(start_ts) FROM sachy_partie").fetchone()[0]   # HANS_SELF_STATE_HRY_OD_V1
    finally:
        c.close()
    if not d and not bezi:
        return "S Koláčem jsem zatím žádnou partii šachů nehrál."
    out = ["Šachy s Koláčem%s — výhry %d, remízy %d, prohry %d." % (
        (" (hraji od %s)" % time.strftime("%-d. %-m. %Y", time.localtime(_od))) if _od else "",
        d.get("výhra", 0), d.get("remíza", 0), d.get("prohra", 0))]
    if sily:   # HANS_SACHY_PREHLED_V1 — „teď“ = síla pro další partii
        c = _db(config)
        try:
            ted = kolac_sila(c)
        finally:
            c.close()
        out.append("Koláčova síla: na začátku %d %%, teď %d %% (když vyhrávám, zesílí; "
                   "po mé prohře zeslábne)." % (
            round(100 * sily[0]), round(100 * (ted if ted is not None else sily[-1]))))
    if bezi:
        out.append("Rozehranou partii mám po %d tazích, hraji %s; dohrajeme ji ve volném "
                   "čase." % ((len((bezi[1] or "").split()) + 1) // 2,
                              "bílými" if bezi[2] == "white" else "černými"))
    if pr:
        out.append("Na co si mám dávat pozor: " + " ".join(pr))
    return "\n".join(out)
