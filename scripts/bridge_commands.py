"""HANS_BRIDGE_COMMANDS_V1 — transport-agnostické příkazy pro notifikační mosty.

Vytaženo z `hans_telegram` (příkazy + NL intenty), aby je mohl použít i Matrix
most (a po smazání Telegramu zůstane jedna pravda). Logika je stejná; místo
`self.send(t, chat_id=cid)` volá `ctx.send(t)` atd. — transport dodá most.

`ctx` (BridgeCtx) poskytuje:
  send(text)->bool, send_photo(path, caption)->bool, person, is_full,
  handler (chat handler s send_chat_message), config, state (per-most persistentní
  dict pro čekající potvrzení vypnutí PC / čekající obraz).

`handle(text, ctx)` vrací True = obslouženo (most NEpošle do chatu), False = ať
padne do chatu (běžná zpráva NEBO „namaluj" — nastaví state['paint'], obraz doručí
proaktivní vrstva mostu později).
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import threading
import time

_log = logging.getLogger("hans.bridge_cmd")

try:
    import requests
except Exception:
    requests = None


class BridgeCtx:
    def __init__(self, send, send_photo, person, is_full, handler, config, state):
        self.send = send
        self.send_photo = send_photo
        self.person = person
        self.is_full = is_full
        self.handler = handler
        self.config = config or {}
        self.state = state if state is not None else {}


def _diary_path(config: dict) -> str:
    return ((config.get("hans_idle", {}) or {}).get("diary_db")
            or config.get("diary_db") or "data/hans_diary.db")


# ── inspect/mutating whitelist (sdíleno s Telegramem, HANS_TELEGRAM_INSPECT_V1) ─
_INSPECT_CMDS = frozenset({"studium", "dilo", "napad", "kritika", "nitky",
                           "zajmy", "seznam", "kalendar", "zdravi", "nastroj",
                           "brief", "vytvor", "prohloubit", "rozvrh", "vhledy",
                           "anomalie"})
_MUTATING_CMDS = frozenset({"hlidej", "experiment"})


# ── top-level ────────────────────────────────────────────────────────────────
def handle(text: str, ctx: BridgeCtx) -> bool:
    """Orchestrace jako v Telegramu (pro role 'full'). True = obslouženo."""
    if ctx.is_full:
        if resolve_pending_pcoff(text, ctx):
            return True
        if handle_command(text, ctx):
            return True
        intent = detect_intent(text)
        if intent == "paint":
            # namaluj NOVÝ obraz → padni do chatu (render na pozadí), obraz
            # doručí proaktivní vrstva mostu (state['paint']).
            ctx.state.setdefault("paint", {})["pending"] = time.time()
            return False
        if intent == "artwork":
            _cmd_artwork(ctx, _pick_mode(text)); return True
        if intent == "diary":
            _cmd_diary(ctx); return True
        if intent == "status":
            _cmd_status(ctx); return True
        if intent == "musing":
            _cmd_musing(ctx); return True
        if intent == "wol":
            _cmd_wol(ctx); return True
        if intent == "pcoff":
            _request_pcoff(ctx); return True
    return False


# ── slash příkazy ────────────────────────────────────────────────────────────
def handle_command(text: str, ctx: BridgeCtx) -> bool:
    cmd = (text or "").lower().lstrip("/").split()[0] if text else ""
    if cmd in ("obraz", "foto", "malba", "obrázek", "obrazek"):
        _cmd_artwork(ctx, _pick_mode(text)); return True
    if cmd in ("denik", "deník", "diary"):
        _cmd_diary(ctx); return True
    if cmd in ("uvaha", "úvaha", "myslenka", "myšlenka"):
        _cmd_musing(ctx); return True
    if cmd in ("stav", "status"):
        _cmd_status(ctx); return True
    if cmd in ("wol", "probudit", "wakeup", "probud", "probuď"):
        _cmd_wol(ctx); return True
    if cmd in ("vypnipc", "shutdown", "pcoff", "vypnout"):
        _cmd_pcoff(ctx); return True
    if cmd in ("herni", "herní", "hra", "game", "hrani", "hraní"):
        _cmd_game(ctx, text); return True
    if _route_inspect_command(text, cmd, ctx):
        return True
    if cmd in ("help", "pomoc", "napoveda", "nápověda", "start"):
        ctx.send(
            "Většinu věcí mi můžete říct i normální řečí — sám poznám, "
            "co chcete, a nabídnu to (s potvrzením). Např.:\n"
            "  „pusť Smrtonosnou past\" · „přidej na nákup mléko\" ·\n"
            "  „nastuduj něco o hradech\" · „jaké je počasí\" ·\n"
            "  „kdo je doma\" · „co hraje\" · „běž spát\"\n\n"
            "Příkazy:\n"
            "/kalendar — nadcházející události (z vašeho Proton kalendáře)\n"
            "/rozvrh — můj rozvrh (autonomní rutiny — kdy naposled tikly)\n"
            "/vhledy — co jsem si všiml ve vlastních datech (/vhledy teď = spusť rozbor)\n"
            "/anomalie — týdenní odchylky v mém chování (/anomalie teď = spusť detekci)\n"
            "/experiment [minut] — zapnu si herní mód na N minut, pak auto-resume\n"
            "/seznam — můj seznam poznámek/úkolů (/seznam hotovo N, /seznam smaz N)\n"
            "/obraz — pošlu poslední obraz (napiš „náhodný obraz\" pro náhodný)\n"
            "/denik — výpis z mého deníku\n"
            "/uvaha — má poslední úvaha\n"
            "/stav — stav systému (teplota, RAM, disk, mozek)\n"
            "/studium — můj studijní program (co studuji)\n"
            "/dilo — mé autorské dílo na pokračování\n"
            "/napad — mé vlastní postřehy (synteze)\n"
            "/kritika — co u sebe chci zlepšit (sebekritika)\n"
            "/wol — probudím počítač (PC) přes síť\n"
            "/vypnipc — vypnu počítač (PC)\n"
            "/herni — herní mód: uvolním grafiku pro hru (/herni vyp = zpět)\n"
            "/hlidej — hlídám dům: při pohybu nebo změně světla pošlu "
            "snímek (/hlidej stop = konec, /hlidej stav = jak jsem na tom)\n"
            "/help — tato nápověda")
        return True
    return False


def _route_inspect_command(text: str, cmd: str, ctx: BridgeCtx) -> bool:
    try:
        from scripts.chat_commands import parse_command, dispatch
    except Exception:
        return False
    resolved = parse_command(text)
    if not resolved:
        return False
    _cid_ = resolved[0]
    if _cid_ not in _INSPECT_CMDS and _cid_ not in _MUTATING_CMDS:
        return False
    if _cid_ in _MUTATING_CMDS and not ctx.is_full:
        ctx.send("Tenhle příkaz vám bohužel provést nemohu, pane.")
        return True
    if ctx.handler is None:
        ctx.send("Tenhle příkaz teď neumím obsloužit (chybí spojení s mozkem).")
        return True
    try:
        out = dispatch(resolved, ctx.handler, ctx.person)
    except Exception as e:
        _log.warning("bridge inspect dispatch selhal: %s", e)
        out = None
    ctx.send(out or "Zatím k tomu nic nemám, pane.")
    return True


# ── NL intenty ───────────────────────────────────────────────────────────────
_ASK = r"(pošl|posl|ukaž|ukaz|zobraz|poslat|uvid|vidět|videt|mrkn|dej|chci|" \
       r"můžeš|muzes|máš.*\bobraz|nějak)"


def detect_intent(text: str):
    t = (text or "").lower()
    if not t:
        return None
    if re.search(r"namaluj|nakresli|namalovat|nakreslit|vytvoř\w*\s+obr", t):
        return "paint"
    if re.search(r"obraz|obrázek|obrazek|namaloval|malb", t) and re.search(_ASK, t):
        return "artwork"
    if re.search(r"den[ií]k|deníč|zápis", t) and re.search(_ASK, t):
        return "diary"
    if re.search(r"\bstav\b|teplot|kolik.*ram|\bcpu\b|vyt[íi]ž|jak.*ti to běž", t):
        return "status"
    if re.search(r"úvah|uvah|myšlenk|myslenk", t) and re.search(_ASK, t):
        return "musing"
    if re.search(r"(vypni|vypnout|zhasni).*(pc|počítač|pocitac|mozek)|"
                 r"\bshutdown\b", t):
        return "pcoff"
    if re.search(r"probu[ďd]|\bwol\b|(zapni|nastartuj|nahoď|nahod).*(pc|počítač|pocitac|mozek)", t):
        return "wol"
    return None


def _pick_mode(text: str) -> str:
    t = (text or "").lower()
    if re.search(r"náhod|nahod|nějak|nejak|random|jin[ýé]|jakýkoliv|jakykoliv", t):
        return "random"
    return "latest"


# ── obsah ────────────────────────────────────────────────────────────────────
def _cmd_artwork(ctx: BridgeCtx, pick: str = "latest"):
    row = None
    dp = _diary_path(ctx.config)
    order = "RANDOM()" if pick == "random" else "ts DESC"
    try:
        con = sqlite3.connect("file:%s?mode=ro" % dp, uri=True, timeout=3.0)
        row = con.execute(
            "SELECT note, data FROM diary WHERE event_type='artwork' "
            "ORDER BY " + order + " LIMIT 1").fetchone()
        con.close()
    except Exception as e:
        _log.warning("bridge artwork query: %s", e)
    if not row:
        ctx.send("Zatím jsem nic nenamaloval.")
        return
    note, data = row
    path = ""
    try:
        path = ((json.loads(data or "{}") or {}).get("path", "") or "")
    except Exception:
        path = ""
    if path and os.path.exists(path):
        cap = (note or "Můj obraz").strip()
        try:
            from scripts.hans_art import origin_line as _origin
            con2 = sqlite3.connect("file:%s?mode=ro" % dp, uri=True, timeout=3.0)
            _t = con2.execute(
                "SELECT title FROM diary WHERE event_type='artwork' "
                "AND data = ? ORDER BY ts DESC LIMIT 1", (data,)).fetchone()
            con2.close()
            _o = _origin(_t[0] if _t else "", data)
        except Exception:
            _o = ""
        if _o:
            cap = f"{_o}\n\n{cap}"
        if not ctx.send_photo(path, cap):
            ctx.send("Obraz se mi nepodařilo odeslat.")
    else:
        ctx.send("Obraz teď nemůžu najít.")


def _cmd_diary(ctx: BridgeCtx, limit: int = 10):
    _TYPES = ("musing", "book_reflection", "book_completion_reflection",
              "narrative_chapter", "work_completion_reflection",
              "creation_reflection", "movie_opinion", "web_read",
              "lesson_learned", "reading_takeaway", "spontaneous",
              "introspection")
    lines = []
    try:
        con = sqlite3.connect("file:%s?mode=ro" % _diary_path(ctx.config),
                              uri=True, timeout=3.0)
        q = ("SELECT datetime(ts,'unixepoch','localtime'), COALESCE(title,''), "
             "COALESCE(note,'') FROM diary WHERE event_type IN (%s) "
             "ORDER BY ts DESC LIMIT ?" % ",".join("?" * len(_TYPES)))
        rows = con.execute(q, _TYPES + (int(limit),)).fetchall()
        con.close()
        for t, ti, no in rows:
            body = (no or ti).strip().replace("\n", " ")
            if body:
                day = (t or "")[5:16]
                lines.append("• %s — %s" % (day, body[:160]))
    except Exception as e:
        _log.warning("bridge diary query: %s", e)
    txt = "Z mého deníku:\n" + "\n".join(lines) if lines else "Deník je zatím prázdný."
    ctx.send(txt[:4000])


def _cmd_musing(ctx: BridgeCtx):
    row = None
    try:
        con = sqlite3.connect("file:%s?mode=ro" % _diary_path(ctx.config),
                              uri=True, timeout=3.0)
        row = con.execute(
            "SELECT COALESCE(note,'') FROM diary WHERE event_type='musing' "
            "ORDER BY ts DESC LIMIT 1").fetchone()
        con.close()
    except Exception as e:
        _log.warning("bridge musing query: %s", e)
    txt = (row[0].strip() if row and row[0] else "")
    ctx.send(txt[:4000] if txt else "Zatím jsem si nic nezapsal.")


def _cmd_status(ctx: BridgeCtx):
    lines = ["Stav systému:"]
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            lines.append("Teplota CPU: %.0f °C" % (int(f.read().strip()) / 1000.0))
    except Exception:
        pass
    try:
        import psutil
        cpu = psutil.cpu_percent(interval=0.5)
        vm = psutil.virtual_memory()
        du = psutil.disk_usage("/")
        lines.append("Zátěž CPU: %.0f %%" % cpu)
        lines.append("RAM: %.1f / %.1f GB (volné %.1f GB)" % (
            (vm.total - vm.available) / 1e9, vm.total / 1e9, vm.available / 1e9))
        lines.append("Disk: volných %.0f GB z %.0f GB" % (
            du.free / 1e9, du.total / 1e9))
    except Exception:
        pass
    try:
        la = os.getloadavg()
        lines.append("Load: %.2f %.2f %.2f" % la)
    except Exception:
        pass
    try:
        with open("/proc/uptime") as f:
            up = float(f.read().split()[0])
        lines.append("Běžím: %dd %dh %dm" % (
            int(up // 86400), int((up % 86400) // 3600), int((up % 3600) // 60)))
    except Exception:
        pass
    try:
        from scripts.ollama_client import game_mode_on
        lines.append("Herní mód: " + ("ZAPNUT (grafika uvolněna pro hru)"
                                       if game_mode_on() else "vypnut"))
    except Exception:
        pass
    try:
        url = ((ctx.config.get("openwebui_chat", {}) or {}).get("base_url", "")
               or "").rstrip("/")
        if url and requests is not None:
            r = requests.get(url + "/api/tags", timeout=4)
            lines.append("Mozek (LLM): " + ("online" if r.ok else "nedostupný"))
    except Exception:
        lines.append("Mozek (LLM): spí / nedostupný")
    ctx.send("\n".join(lines))


# ── herní mód ────────────────────────────────────────────────────────────────
def _cmd_game(ctx: BridgeCtx, text: str = ""):
    try:
        from scripts.ollama_client import set_game_mode, game_mode_on
    except Exception as e:
        ctx.send("Herní mód není dostupný: %s" % e); return
    parts = (text or "").lower().split()
    arg = parts[1] if len(parts) > 1 else ""
    if arg in ("stav", "status"):
        ctx.send("Herní mód je ZAPNUTÝ (grafika volná pro hru)."
                 if game_mode_on() else "Herní mód je vypnutý.")
        return
    if arg in ("zap", "on", "1", "ano", "start"):
        target = True
    elif arg in ("vyp", "off", "0", "ne", "stop", "konec"):
        target = False
    else:
        target = not game_mode_on()
    res = set_game_mode(target, config=ctx.config)
    if isinstance(res, dict) and "error" in res:
        ctx.send("Herní mód selhal: %s" % res["error"]); return
    if target:
        ctx.send("Herní mód ZAPNUT — uvolnil jsem %d model(ů) z grafické "
                 "paměti, grafika je volná pro hru. Až dohraješ, pošli "
                 "/herni vyp." % (res.get("unloaded", 0) if isinstance(res, dict) else 0))
    else:
        ctx.send("Herní mód VYPNUT — mozek je zase k dispozici.")


# ── PC power (WOL / shutdown, HANS_WOL_SHARED_V1 / HANS_SHUTDOWN_CONTEXT_V1) ───
def _cmd_wol(ctx: BridgeCtx):
    mac = str(ctx.config.get("wol_pc_mac", "") or "")
    if not mac:
        ctx.send("Nemám nastavenou MAC adresu PC (wol_pc_mac).")
        return
    try:
        from scripts import pc_remote
        if not pc_remote.wake(mac=mac):
            raise ValueError("neplatná MAC")
        _log.info("bridge /wol → magic packet to %s", mac)
    except Exception as e:
        ctx.send("Probuzení se nezdařilo: %s" % e)
        return
    ctx.send("Posílám počítači probouzecí signál. Za chvíli ověřím, zda naběhl…")
    ip = str(ctx.config.get("wol_pc_ip", "") or "")
    if ip:
        threading.Thread(target=_wol_verify, args=(ip, ctx), daemon=True).start()


def _wol_verify(ip: str, ctx: BridgeCtx):
    import subprocess
    for _ in range(8):
        time.sleep(15)
        try:
            ok = subprocess.run(["ping", "-c", "1", "-W", "2", ip],
                                capture_output=True, timeout=5).returncode == 0
        except Exception:
            ok = False
        if ok:
            ctx.send("Počítač je online. Mozek se za chvíli probudí.")
            return
    ctx.send("Počítač se zatím neprobudil (možná je úplně vypnutý nebo "
             "WOL v BIOSu vypnutý).")


def _request_pcoff(ctx: BridgeCtx):
    try:
        from scripts.hans_agent import _shutdown_confirm_text
        body = _shutdown_confirm_text(ctx.handler)
    except Exception:
        body = "Opravdu mám vypnout počítač, pane?"
    ctx.state["pcoff"] = time.time()
    ctx.send(body + "\n\n(napište ano pro potvrzení, nebo ne)")


def resolve_pending_pcoff(text: str, ctx: BridgeCtx) -> bool:
    if "pcoff" not in ctx.state:
        return False
    if time.time() - ctx.state.get("pcoff", 0) > 120:
        ctx.state.pop("pcoff", None)
        return False
    ctx.state.pop("pcoff", None)
    try:
        from scripts.hans_agent import _YES, _NO
    except Exception:
        _YES, _NO = {"ano", "jo", "jasně", "ok", "vypni"}, {"ne", "nevypínej"}
    t = text.strip().lower().strip("!?.,")
    first = t.split()[0] if t.split() else ""
    if t in _YES or first in _YES:
        _cmd_pcoff(ctx)
        return True
    if t in _NO or first in _NO:
        ctx.send("Dobře, počítač nechám běžet, pane.")
        return True
    return False


def _cmd_pcoff(ctx: BridgeCtx):
    try:
        from scripts.chat_commands import _cmd_vypnipc
    except Exception:
        ctx.send("Na počítač teď nedosáhnu.")
        return
    ctx.send("Posílám počítači povel k vypnutí. Ověřím, zda zhasl…")

    def _work():
        try:
            msg = _cmd_vypnipc(ctx.handler, None, "")
        except Exception as e:
            msg = "Vypnutí se nezdařilo: %s" % e
        ctx.send(msg)

    threading.Thread(target=_work, daemon=True).start()
