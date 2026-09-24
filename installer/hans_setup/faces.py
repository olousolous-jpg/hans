"""Krok „obličeje": průvodce zápisem lidí z domácnosti do rozpoznávání tváří.

Používá jen existující HTTP API Hansova webadminu (nic v kódu se nemění):
  GET  /api/faces                 — kdo je zapsaný a kolik má vzorků
  POST /api/enroll/start          — {name, seconds:0} = vícefázový zápis
                                    (1 m → 2 m → 3 m, Hans vede hlasem, ~2 min)
  POST /api/enroll/quick_augment  — {name, session} = doplnění pro jiné světlo

Zápis běží v Hansově hlavní smyčce (kamera + Hailo), takže Hans MUSÍ běžet.
Na konci se na displeji Pi otevře okno s náhledy: tam se vyřadí špatné snímky
a do pole „Jméno osoby“ se napíše KLÍČ osoby (stejný jako v known_persons).
Obličejová data zůstávají jen na Pi (data/known_faces*.pkl, mimo git).
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Optional

from . import ui

ADMIN = os.environ.get("HANS_ADMIN_URL", "http://127.0.0.1:7860")
POLL_S = float(os.environ.get("HANS_FACES_POLL", "5"))
WAIT_S = float(os.environ.get("HANS_FACES_WAIT", "420"))   # vícefázový zápis ~2 min + okno

SESSIONS = (("morning", "ráno"), ("afternoon", "odpoledne"), ("evening", "večer"))


class AdminError(RuntimeError):
    pass


def _call(method: str, path: str, body: Optional[dict] = None, timeout: float = 10):
    req = urllib.request.Request(
        ADMIN.rstrip("/") + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8") or "null")
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode("utf-8")).get("detail")
        except Exception:
            detail = str(e)
        raise AdminError(str(detail))
    except (urllib.error.URLError, OSError) as e:
        raise AdminError("webadmin na %s neodpovídá (%s)" % (ADMIN, e))


def admin_up() -> bool:
    try:
        _call("GET", "/api/faces", timeout=4)
        return True
    except AdminError:
        return False


def enrolled() -> dict[str, int]:
    out = {}
    for r in _call("GET", "/api/faces") or []:
        if isinstance(r, dict) and r.get("name"):
            out[r["name"]] = int(r.get("samples") or 0)
    return out


def household(cfg: dict) -> list[tuple[str, str]]:
    """[(klíč, jméno v 1. pádě)] z known_persons."""
    return [(k, (v or {}).get("nom") or k) for k, v in (cfg.get("known_persons") or {}).items()]


def current_session(hour: Optional[int] = None) -> str:
    h = time.localtime().tm_hour if hour is None else hour
    return "morning" if h < 12 else ("afternoon" if h < 17 else "evening")


def _status(people, counts) -> None:
    print()
    for key, nom in people:
        n = counts.get(key, 0)
        mark = "\033[32mzapsán(a), %d vzorků\033[0m" % n if n else "\033[33mnezapsán(a)\033[0m"
        print("    %-12s %-16s %s" % (key, nom, mark))
    others = sorted(set(counts) - {k for k, _ in people})
    if others:
        print("    (v databázi tváří navíc: %s)" % ", ".join(others))
    print()


def enroll(key: str, nom: str, dry_run: bool) -> bool:
    ui.header("Zápis: %s" % nom)
    ui.info("Postup (asi 2 minuty):")
    ui.info("  1. %s bude před kamerou SÁM/SAMA, v dobrém světle, obličejem ke kameře." % nom)
    ui.info("  2. Hans hlasem navede: 1 metr → 2 metry → 3 metry. Pomalu otáčet hlavou.")
    ui.info("  3. Na displeji Pi se pak otevře okno s náhledy. Vyřaď rozmazané nebo cizí")
    ui.info("     snímky (✕), do pole „Jméno osoby“ napiš PŘESNĚ:  \033[1m%s\033[0m" % key)
    ui.info("     a potvrď „Enrollovat vybrané“.")
    if not ui.confirm("Je %s připraven(a) před kamerou?" % nom, True):
        return False
    if dry_run:
        ui.info("[dry-run] POST /api/enroll/start {name: %s, seconds: 0}" % key)
        return True
    before = enrolled().get(key, 0)
    try:
        r = _call("POST", "/api/enroll/start", {"name": key, "seconds": 0})
        ui.ok(r.get("message", "zápis spuštěn"))
    except AdminError as e:
        ui.warn("Zápis se nespustil: %s" % e)
        return False
    ui.info("Čekám, až v okně na Pi potvrdíš zápis (max %d min)…" % (WAIT_S // 60))
    deadline = time.time() + WAIT_S
    while time.time() < deadline:
        time.sleep(POLL_S)
        try:
            now = enrolled().get(key, 0)
        except AdminError:
            continue
        if now > before:
            ui.ok("%s zapsán(a): %d vzorků (dřív %d)" % (nom, now, before))
            return True
    ui.warn("Zápis se v databázi neobjevil. Časté příčiny: v záběru bylo víc lidí,")
    ui.warn("málo světla, okno na Pi bylo zavřeno bez potvrzení, nebo jiné jméno v okně.")
    ui.warn("Log: journalctl --user -u hans | grep -i enroll")
    return False


def augment(key: str, nom: str, dry_run: bool) -> bool:
    sess = current_session()
    label = dict(SESSIONS)[sess]
    ui.info("Doplnění vzorků pro současné světlo (%s): pár vteřin, %s se dívá do kamery." % (label, nom))
    if not ui.confirm("Spustit?", True):
        return False
    if dry_run:
        ui.info("[dry-run] POST /api/enroll/quick_augment {name: %s, session: %s}" % (key, sess))
        return True
    try:
        r = _call("POST", "/api/enroll/quick_augment", {"name": key, "session": sess})
        ui.ok(r.get("message", "doplnění spuštěno") if isinstance(r, dict) else "doplnění spuštěno")
        return True
    except AdminError as e:
        ui.warn("Doplnění se nespustilo: %s" % e)
        return False


def step(cfg: dict, dry_run: bool) -> None:
    ui.header("Rozpoznávání osob")
    people = household(cfg)
    if not people:
        ui.warn("V configu nejsou žádní lidé z domácnosti — nejdřív krok persona (domácnost).")
        return
    if not dry_run and not admin_up():
        ui.warn("Hans neběží (webadmin %s neodpovídá)." % ADMIN)
        ui.info("Spusť ho:  systemctl --user start hans   a zkus znovu:")
        ui.info("           bash installer/install.sh --only faces")
        return
    ui.info("Hans se naučí poznávat lidi podle obličeje. Každého zapiš zvlášť; potom")
    ui.info("je dobré doplnit vzorky i v jiném světle (ráno / odpoledne / večer).")
    while True:
        counts = {} if dry_run else enrolled()
        _status(people, counts)
        missing = [(k, n) for k, n in people if not counts.get(k)]
        opts = [(str(i + 1), "%s — %s" % (n, "doplnit světlo" if counts.get(k) else "zapsat"))
                for i, (k, n) in enumerate(people)]
        if missing:
            opts.insert(0, ("v", "zapsat postupně všechny nezapsané (%d)" % len(missing)))
        opts.append(("k", "konec"))
        ch = ui.choose("Co dál?", opts, "v" if missing else "k")
        if ch == "k":
            break
        if ch == "v":
            for k, n in missing:
                if not enroll(k, n, dry_run) and not ui.confirm("Pokračovat dalším?", True):
                    break
            if dry_run:
                break
            continue
        k, n = people[int(ch) - 1]
        if counts.get(k):
            augment(k, n, dry_run)
        else:
            enroll(k, n, dry_run)
        if dry_run:
            break
    ui.info("Nového člověka přidáš kdykoli:  python3 installer/wizard.py household")
    ui.info("a pak  python3 installer/wizard.py faces  (Hans musí běžet; po změně")
    ui.info("domácnosti ho restartuj: systemctl --user restart hans).")
