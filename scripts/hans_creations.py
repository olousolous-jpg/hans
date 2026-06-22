"""
HANS_CREATIONS_V1 — Fáze 2 sebeřízené tvorby: Hans SÁM volí, co vytvořit.

Ve volné noční chvíli `creative_impulse()` zváží dostupné tvůrčí akty a JEDEN
vybere (vážená ruleta + variace, ať neopakuje stejnou formu):
  • namalovat svůj SEN     → reuse hans_art.paint_dream (Fáze 1)
  • napsat nevyžádanou ÚVAHU o tom, co mu leží v hlavě (postoj / čtená kniha /
    výrazný moment dne) — nová textová modalita (levné, jen hans-czech).

Vše GROUNDOVANÉ v Hansově skutečném vnitřním životě (stances / library / deník)
→ žádná náhodnost. Throttle (impulse_interval_days) → tvoří zřídka. Deferral-safe.
"""

import json
import logging
import random
import sqlite3
import time
from typing import Optional

_log = logging.getLogger("hans_creations")


def _ccfg(config: dict) -> dict:
    return config.get("hans_creations", {}) or {}


# ── throttle + variace ──────────────────────────────────────────────────────
def _last_creation(db_path: str) -> tuple:
    """(ts, forma) posledního tvůrčího aktu (musing nebo namalovaný sen). Pro
    throttle (jak dávno) + variaci (neopakovat formu). (0.0, '') = nikdy."""
    try:
        con = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True, timeout=3.0)
        # HANS_DREAMS_PER_DREAM_V1 — sny už NEjsou součástí creative_impulse
        # (malují se per nový sen mimo throttle) → throttle/variace jen den+úvaha.
        rows = con.execute(
            "SELECT ts, event_type, data FROM diary "
            "WHERE event_type='musing' "
            "OR (event_type='artwork' AND data LIKE '%\"source\": \"day\"%') "
            "ORDER BY ts DESC LIMIT 1").fetchall()
        con.close()
        if rows:
            ts, et, data = rows[0]
            return float(ts), ("musing" if et == "musing" else "day")
    except Exception as e:
        _log.debug("creations: last_creation failed: %s", e)
    return 0.0, ""


# ── seed pro úvahu (o čem psát) ─────────────────────────────────────────────
def _musing_seed(db_path: str) -> Optional[dict]:
    """Vybere salientní téma pro úvahu z Hansova vnitřního života. Vrací
    {type, prompt} nebo None. Grounded read-only."""
    cands = []
    try:
        con = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True, timeout=3.0)
        # 1) silný aktivní postoj
        for (claim,) in con.execute(
                "SELECT claim FROM stances WHERE status='active' AND claim<>'' "
                "ORDER BY confidence DESC, last_seen DESC LIMIT 5").fetchall():
            cands.append(("stance", "Tvůj vlastní postoj, který v sobě neseš: „%s\"" % claim.strip()))
        # 2) kniha, kterou čte + poslední reflexe
        bk = con.execute("SELECT book_title FROM hans_library WHERE status='reading' "
                         "ORDER BY started_at DESC LIMIT 1").fetchone()
        if bk and bk[0]:
            refl = con.execute(
                "SELECT data FROM diary WHERE event_type='book_reflection' "
                "AND title LIKE ? AND data<>'' ORDER BY ts DESC LIMIT 1",
                (bk[0] + "%",)).fetchone()
            seed = "Kniha, kterou právě čteš: %s." % bk[0]
            if refl and refl[0]:
                seed += " Naposledy tě v ní zaujalo: %s" % refl[0].strip()[:200]
            cands.append(("book", seed))
        # 3) výrazný moment dne (vysoká importance, posledních ~2 dny)
        cutoff = time.time() - 2 * 86400
        for et, note in con.execute(
                "SELECT event_type, COALESCE(NULLIF(note,''),data) FROM diary "
                "WHERE COALESCE(importance,0)>=6 AND ts>=? "
                "AND COALESCE(NULLIF(note,''),data)<>'' "
                "AND event_type NOT IN ('human_chat') "
                "ORDER BY importance DESC, ts DESC LIMIT 4", (cutoff,)).fetchall():
            cands.append(("moment", "Nedávný zážitek, který ti utkvěl: %s" % note.strip()[:200]))
        con.close()
    except Exception as e:
        _log.debug("creations: musing_seed failed: %s", e)
    if not cands:
        return None
    t, p = random.choice(cands)
    return {"type": t, "prompt": p}


# ── forma: napsat úvahu ─────────────────────────────────────────────────────
def write_musing(config: dict, db_path: str, seed: Optional[dict] = None) -> bool:
    """Hans spontánně napíše krátkou osobní úvahu o tom, co mu leží v hlavě
    (NE z příkazu, NE analýza). hans-czech persona. Deník event 'musing'."""
    seed = seed or _musing_seed(db_path)
    if not seed:
        return False
    try:
        from scripts.ollama_client import ollama_generate
        from scripts.hans_persona import persona_core
    except Exception:
        return False
    try:
        core = persona_core(config, with_address=False)
    except Exception:
        core = ""
    model = (config.get("models", {}) or {}).get("dialog", "hans-czech:latest")
    system = (core + "\n\n" if core else "") + (
        "Máš tichou chvíli a chce se ti něco si SÁM PRO SEBE zapsat. Napiš krátkou "
        "osobní úvahu (4-6 vět, první osoba, česky) o tématu níže — svou upřímnou, "
        "nezávaznou myšlenku, jak ti právě teď leží v hlavě. NE rozbor, NE pro "
        "nikoho jiného, jen tvůj vnitřní hlas. Žádný nadpis, žádné uvozovky.")
    try:
        out = ollama_generate(model, seed["prompt"] + "\n\nNapiš tu úvahu.",
                              system=system, config=config,
                              timeout=int(_ccfg(config).get("musing_timeout", 120)))
    except Exception as e:
        _log.warning("creations: musing LLM failed: %s", e)
        return False
    text = (out or "").strip().strip('"')
    if len(text) < 40:
        return False
    try:
        db = sqlite3.connect(db_path, timeout=5.0)
        db.execute(
            "INSERT INTO diary (ts, event_type, title, note, data) VALUES (?,?,?,?,?)",
            (time.time(), "musing", "Úvaha", text[:1500],
             json.dumps({"seed": seed["type"]}, ensure_ascii=False)))
        db.commit()
        db.close()
    except Exception as e:
        _log.warning("creations: log musing failed: %s", e)
        return False
    _log.info("creations: Hans napsal úvahu (%s): %.110s", seed["type"], text)
    return True


# ── chooser: Hans sám volí tvůrčí akt ───────────────────────────────────────
def creative_impulse(config: dict, db_path: str) -> bool:
    """Hans ve volné chvíli zváží dostupné tvůrčí akty a JEDEN vybere (váženě +
    variace). Vrací True, když něco vytvořil. Throttle uvnitř. Nikdy nehází."""
    ccfg = _ccfg(config)
    if not ccfg.get("enabled", True):
        return False
    interval = float(ccfg.get("impulse_interval_days", 2))
    last_ts, last_form = _last_creation(db_path)
    if last_ts and (time.time() - last_ts) < interval * 86400:
        return False  # tvoří zřídka

    # dostupné formy s vahou
    # HANS_DREAMS_PER_DREAM_V1 — SEN tu UŽ NENÍ: maluje se per nový sen mimo
    # creative_impulse (hans_routine volá paint_dream zvlášť). Tady jen den+úvaha.
    forms = {}
    # DEN/NÁLADA (obraz) — ComfyUI + dnešní materiál + vlastní throttle
    try:
        from scripts import hans_art
        pcfg = (config.get("hans_art", {}) or {}).get("day_painting", {}) or {}
        day_iv = float(pcfg.get("min_interval_days", 4))
        last_day = hans_art._last_day_painting_ts(db_path)
        day_ok = (not last_day) or (time.time() - last_day) >= day_iv * 86400
        if (pcfg.get("enabled", True) and day_ok
                and len(hans_art._day_fragments(db_path)) >= 30
                and hans_art.comfy_available(config)):
            forms["day"] = 1.0
    except Exception as e:
        _log.debug("creations: day form check failed: %s", e)
    # ÚVAHA (text) — když je o čem psát
    seed = _musing_seed(db_path)
    if seed:
        forms["musing"] = 1.0

    if not forms:
        return False
    # variace: poslední formu zlevni (ať se střídá)
    if last_form in forms and len(forms) > 1:
        forms[last_form] *= 0.35

    keys = list(forms)
    pick = random.choices(keys, weights=[forms[k] for k in keys], k=1)[0]
    _log.info("creations: tvůrčí impuls → %s (z %s)", pick, "/".join(keys))
    if pick == "day":
        from scripts import hans_art
        return hans_art.paint_day(config, db_path)
    return write_musing(config, db_path, seed)


# ── ruční test: python3 -m scripts.hans_creations [musing|impulse] ──────────
if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    cfg = json.load(open("config.json"))
    DB = "data/hans_diary.db"
    mode = sys.argv[1] if len(sys.argv) > 1 else "seed"
    if mode == "seed":
        print("seed:", _musing_seed(DB))
        print("last_creation:", _last_creation(DB))
    elif mode == "musing":
        print("write_musing →", write_musing(cfg, DB))
    elif mode == "impulse":
        print("creative_impulse →", creative_impulse(cfg, DB))
