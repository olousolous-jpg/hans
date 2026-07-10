#!/usr/bin/env python3
"""hans_capabilities.py — HANS_CAPABILITY_AWARENESS_V1 (+ V2 self-detekce novinek)

Faktické povědomí Hanse o tom, CO REÁLNĚ DOKÁŽE — aby to v komunikaci NABÍZEL
a POUŽÍVAL, místo aby to odmítal (bug: požádán o obraz odpověděl, že „nemá
umělecké sklony", přestože má plnou malířskou pipeline).

V2: Hans si SÁM VŠIMNE, když mu přibude nová schopnost. Pamatuje si, co „znal"
(data/hans_known_capabilities.json); při přidání položky do manifestu vznikne
deníkový event `capability_gained` a Hans to v chatu přirozeně zmíní.

ANTI-KONFABULACE: zdroj je RUČNĚ KURÁTOROVANÝ seznam skutečných schopností
(každá = existující modul/příkaz). NE volná LLM domněnka. Přidáš-li Hansovi
novou schopnost, přidej sem řádek (id + text + jak) — Hans si toho pak všimne.
"""
from __future__ import annotations

import os
import json
import time
import logging

_log = logging.getLogger("hans_capabilities")

_KNOWN_FILE = "data/hans_known_capabilities.json"

# (stabilní id, schopnost v 1. osobě, jak ji vyvolat / kde se projeví)
# id = trvalý klíč (text lze přepsat bez falešného „nová schopnost").
_CAPABILITIES = [
    ("paint", "Umím MALOVAT obrazy (výtvarná pipeline SDXL) — své sny, dojmy ze "
     "dne, svůj domov, obrazy ke knihám, i na libovolné téma či dojem z rozhovoru",
     "namaluj <téma> / nakresli <téma>; /art <kniha>; galerie „Co Hans namaloval\""),
    ("study", "Umím STUDOVAT téma do hloubky přes týdny (Wikipedie, akademický "
     "výzkum, primární texty, knihy) a psát si poznámky", "/studium"),
    ("authorship", "Píšu VLASTNÍ dílo na pokračování (esej/povídku)", "/dilo"),
    ("synthesis", "Propojuji naučené z různých oborů do vlastních POSTŘEHŮ", "/napad"),
    ("selfcritique", "Kriticky se ohlížím za svým projevem a beru si PONAUČENÍ", "/kritika"),
    ("kolac", "Vedu DIALOG s Koláčem (mám vlastní i jeho mysl)", "probíhá sám"),
    ("memory", "PAMATUJI si — deník, dlouhodobá paměť (RAG), vztahové karty k "
     "lidem, vlastní postoje a jejich vývoj", "/denik, /nitky, /zajmy"),
    ("vision", "Vidím a poznávám LIDI přes kameru a reaguji na to, kdo je "
     "přítomen", "automaticky"),
    ("films", "Navrhuji FILMY a sám pokračuji v přehrávání na TV (Kodi); umím "
     "říct, co se právě přehrává", "co hraje?; automaticky z klidu"),
    ("pc_health", "Vidím reálný STAV svého PC — teplotu GPU/CPU, paměť — přes SSH",
     "/stav"),
    ("game_mode", "Umím uvolnit grafiku pro HRU (herní mód) a ukázat telemetrii "
     "na displejích", "/herni"),
    ("telegram", "Komunikuji přes TELEGRAM (odpovídám i sám píšu)", "Telegram most"),
    ("avatar", "Vytvářím si vizuální PODOBU (avatar), která se vyvíjí s povahou",
     "/avatar"),
    ("place", "Mám smysl pro MÍSTO — model domova, vím kde jsem a co je za oknem",
     "/misto"),
    ("dashboard", "Umím navrhnout vlastní PODOBU své nástěnky (z toho, co jsem "
     "nastudoval o designu)", "/dashboard"),
    ("recall", "Umím PŘESNĚ odpovědět na dotazy o vlastní paměti přímo z deníku "
     "— má první vzpomínka, co a kdy jsem četl, kdy jsem koho viděl (žádný "
     "odhad, jen skutečné záznamy)",
     "jaká je tvá první vzpomínka?; co jsi četl?; kdy jsi mě viděl?"),
]


def capabilities_list() -> list[str]:
    return [t for _, t, _ in _CAPABILITIES]


def capabilities_context() -> str:
    """Blok do chat system promptu: co Hans REÁLNĚ umí + pokyn to používat."""
    lines = "\n".join(f"- {t}" for _, t, _ in _CAPABILITIES)
    return (
        "\n\nCO REÁLNĚ DOKÁŽEŠ (tvé skutečné schopnosti — NABÍZEJ je a POUŽÍVEJ, "
        "NIKDY je neodmítej jako bys je neměl):\n" + lines +
        "\n\nKdyž tě někdo požádá o něco z tohoto seznamu (např. namalovat obraz), "
        "SKUTEČNĚ to udělej nebo nabídni — máš to k dispozici. Nikdy netvrď, že "
        "něco neumíš, pokud to je na seznamu (např. neříkej, že „nemáš umělecké "
        "sklony“ — umíš malovat). Naopak NESLIBUJ schopnosti, které tu nejsou.")


def capabilities_report() -> str:
    """Text pro chat /schopnosti."""
    out = ["Co dokážu, pane:"]
    for _, cap, how in _CAPABILITIES:
        out.append(f"• {cap}  ({how})")
    return "\n".join(out)


# ── V2: self-detekce nově přidaných schopností ───────────────────────────────
def _load_known() -> set:
    try:
        with open(_KNOWN_FILE, encoding="utf-8") as f:
            return set(json.load(f))
    except Exception:
        return set()


def _save_known(ids: set) -> None:
    try:
        os.makedirs(os.path.dirname(_KNOWN_FILE), exist_ok=True)
        with open(_KNOWN_FILE, "w", encoding="utf-8") as f:
            json.dump(sorted(ids), f, ensure_ascii=False)
    except Exception as e:
        _log.warning("_save_known: %s", e)


def detect_new_capabilities(diary_db_path: str = "data/hans_diary.db") -> list:
    """Porovnej manifest s tím, co Hans „znal". NOVÉ položky → deníkový event
    `capability_gained` (1. osoba) + aktualizuj známé. Vrátí seznam (id, text).
    PRVNÍ běh (žádný soubor): seedne vše TIŠE (baseline, neohlašuje staré)."""
    cur_ids = {cid for cid, _, _ in _CAPABILITIES}
    known = _load_known()
    first_run = not os.path.exists(_KNOWN_FILE)
    if first_run:
        _save_known(cur_ids)
        _log.info("capabilities: baseline seedován (%d schopností)", len(cur_ids))
        return []
    new = [(cid, t) for cid, t, _ in _CAPABILITIES if cid not in known]
    if not new:
        return []
    import sqlite3
    for cid, text in new:
        note = "Zjistil jsem u sebe novou schopnost: " + text
        try:
            db = sqlite3.connect(diary_db_path, timeout=5.0)
            db.execute(
                "INSERT INTO diary (ts, event_type, title, note, data) "
                "VALUES (?,?,?,?,?)",
                (time.time(), "capability_gained", cid, note, cid))
            db.commit()
            db.close()
        except Exception as e:
            _log.warning("capability_gained zápis: %s", e)
    _save_known(known | cur_ids)
    _log.info("capabilities: NOVÉ schopnosti (%d): %s",
              len(new), ", ".join(c for c, _ in new))
    return new


# ── V3: zvědavost — Hans si novou schopnost SÁM vyzkouší a zjistí, co umí ────
def _text_for(cap_id: str) -> str:
    for cid, t, _ in _CAPABILITIES:
        if cid == cap_id:
            return t
    return cap_id


def _how_for(cap_id: str) -> str:
    for cid, _, how in _CAPABILITIES:
        if cid == cap_id:
            return how
    return ""


def _trial_paint(config: dict, diary_db_path: str) -> str:
    """Zkušební úkon pro schopnost malovat: Hans namaluje impresi něčeho, co ho
    v poslední době zaujalo (z četby), nebo obecnou. Vrátí krátký popis výsledku."""
    try:
        from scripts.hans_art import paint_subject, comfy_available
    except Exception:
        return ""
    if not comfy_available(config):
        return ""
    subject = "dojem z něčeho, co mě v poslední době zaujalo"
    try:
        import sqlite3
        conn = sqlite3.connect("file:%s?mode=ro" % diary_db_path, uri=True,
                               timeout=3.0)
        row = conn.execute(
            "SELECT title FROM diary WHERE event_type IN "
            "('reading_takeaway','study_note') AND title IS NOT NULL "
            "AND title != '' ORDER BY ts DESC LIMIT 1").fetchone()
        conn.close()
        if row and row[0]:
            subject = row[0].strip()
    except Exception:
        pass
    res = paint_subject(config, diary_db_path, subject)
    if res:
        return "Zkusmo jsem namaloval obraz na téma „%s“." % subject[:60]
    return ""


# schopnosti, které si Hans může BEZPEČNĚ sám vyzkoušet (kreativní/read-only).
# Ostatní (telegram/game_mode/avatar/pc_health…) = jen reflexe, ne auto-invokace.
_TRIALS = {
    "paint": _trial_paint,
}


def _explored_ids(diary_db_path: str) -> set:
    import sqlite3
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % diary_db_path, uri=True,
                               timeout=3.0)
        rows = conn.execute(
            "SELECT data FROM diary WHERE event_type='capability_explored'"
        ).fetchall()
        conn.close()
        return {r[0] for r in rows if r and r[0]}
    except Exception:
        return set()


def pending_explorations(diary_db_path: str = "data/hans_diary.db") -> list:
    """id schopností, které Hans objevil (capability_gained), ale ještě je
    zvědavě neprozkoumal (capability_explored)."""
    import sqlite3
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % diary_db_path, uri=True,
                               timeout=3.0)
        rows = conn.execute(
            "SELECT DISTINCT data FROM diary WHERE event_type='capability_gained'"
        ).fetchall()
        conn.close()
    except Exception:
        return []
    gained = [r[0] for r in rows if r and r[0]]
    done = _explored_ids(diary_db_path)
    return [g for g in gained if g not in done]


def explore_capability(config: dict, diary_db_path: str,
                       cap_id: str = "") -> str:
    """Hans si novou schopnost ZVĚDAVĚ prozkoumá: (1) zkušební úkon (u paint
    reálně namaluje), (2) reflexe 1. os. — co ho na tom láká, co zkusil, co
    zjistil → deník `capability_explored` (data=cap_id). Kódy: 'explored' /
    'idle' (nic nečeká) / 'deferred' (LLM dole → retry, guard se nenastaví)."""
    if not cap_id:
        pend = pending_explorations(diary_db_path)
        if not pend:
            return "idle"
        cap_id = pend[0]
    text, how = _text_for(cap_id), _how_for(cap_id)
    # zkušební úkon (jen bezpečné kreativní schopnosti)
    trial_note = ""
    trial = _TRIALS.get(cap_id)
    if trial:
        try:
            trial_note = trial(config, diary_db_path) or ""
        except Exception as e:
            _log.warning("explore trial %s: %s", cap_id, e)
    # zvědavá reflexe (hans-czech)
    try:
        from scripts.ollama_client import ollama_chat
        from scripts.hans_persona import persona_name
        name = persona_name(config)
    except Exception:
        return "deferred"
    system = (
        f"Jsi {name}. Nedávno jsi u sebe objevil NOVOU schopnost a jsi na ni "
        "zvědavý — chceš zjistit, co s ní vlastně svedeš. Napiš krátkou reflexi "
        "v první osobě (3-5 vět): co tě na té nové schopnosti láká, co bys s ní "
        "rád zkusil a jak ti to rozšiřuje možnosti. Buď konkrétní k té schopnosti, "
        "upřímně zvídavý, bez emoji, česky.")
    user = f"Nová schopnost: {text}\nJak ji používám: {how}"
    if trial_note:
        user += (f"\n\nUž jsem si ji zkusmo vyzkoušel: {trial_note} "
                 "Zmiň, že jsi to zkusil, a co z toho máš za dojem.")
    try:
        raw = ollama_chat(
            str((config.get("dashboard_proposal", {}) or {}).get("model")
                or config.get("models", {}).get("dialog", "hans-czech:latest")),
            [{"role": "system", "content": system},
             {"role": "user", "content": user}],
            config=config, options={"temperature": 0.75, "num_ctx": 4096,
                                    "num_predict": 300})
    except Exception as e:
        _log.warning("explore_capability LLM: %s", e)
        return "deferred"
    refl = (raw or "").strip()
    if not refl or len(refl) < 40:
        return "deferred"
    try:
        import sqlite3
        db = sqlite3.connect(diary_db_path, timeout=5.0)
        db.execute(
            "INSERT INTO diary (ts, event_type, title, note, data) VALUES (?,?,?,?,?)",
            (time.time(), "capability_explored", text[:60], refl, cap_id))
        db.commit()
        db.close()
    except Exception as e:
        _log.warning("capability_explored zápis: %s", e)
        return "deferred"
    _log.info("capabilities: prozkoumáno „%s\"%s", cap_id,
              " (+ trial)" if trial_note else "")
    return "explored"


def recent_gained_context(diary_db_path: str = "data/hans_diary.db",
                          days: int = 10) -> str:
    """Chat kontext: nedávno získané schopnosti (Hans o nich sám ví). '' když nic."""
    import sqlite3
    since = time.time() - days * 86400
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % diary_db_path, uri=True,
                               timeout=3.0)
        rows = conn.execute(
            "SELECT note FROM diary WHERE event_type='capability_gained' "
            "AND ts > ? ORDER BY ts DESC LIMIT 4", (since,)).fetchall()
        conn.close()
    except Exception:
        return ""
    caps = [r[0] for r in rows if r and r[0]]
    if not caps:
        return ""
    return ("\n\nNEDÁVNO ses u sebe naučil nové schopnosti (klidně to nadšeně "
            "zmiň, když se to hodí):\n" + "\n".join("- " + c for c in caps))


if __name__ == "__main__":
    print(capabilities_report())
    print("\ndetect (baseline/nové):", detect_new_capabilities("data/hans_diary.db"))
