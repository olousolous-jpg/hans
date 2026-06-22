"""scripts/avatar_descriptor.py

AVATAR_DESCRIPTOR_V1 — odvození vizuálního descriptoru Hanse z jeho IDENTITY.

Pomalá (levná) větev avatara: charakter → strukturovaný čitelný popis vzhledu
(JSON). Render (SDXL/ComfyUI) je SAMOSTATNÁ drahá úloha, která tenhle descriptor
spotřebuje (zatím NEimplementováno — viz handoff log.txt). Tady jen descriptor:
levné, deferral-safe (jen LLM), ukládá se a verzuje.

Napojení na Severku (zdroj pravdy o charakteru):
  - CORE   = aktuální identita z IdentityStore.current() (Severka ji verzuje/přepisuje),
             fallback config.persona.core.
  - TENDENCE = hans_tendencies.derive_tendencies (pevně držené / v napětí / klíčící).
  - TÉMATA   = nejsilnější aktivní koníčky (hans_hobbies) → recent_themes / vocational.
Vzhled se tak posouvá SYNCHRONNĚ s vnitřní evolucí (avatar = vizuál Severky).

Neutrální model (NE hans-czech finetune — ten táhne zpět k butlerovi a brání
evoluci; viz [[hans-czech-is-openeurollm-finetune]]). keep_alive=0 (on-demand,
[[ollama-vram-tiers]]). Inkrementální update: dostane předchozí descriptor +
co se posunulo → upraví jen ospravedlněné; identity_anchor a build ZAMČENÉ
(pořád rozpoznatelně tentýž člověk).

API:
  maybe_update_descriptor(config, diary_db_path) -> dict | None   # entry point (volá routine za Severkou)
  generate_descriptor(config, diary_db_path, prev=None) -> dict | None
  derive_character(config, diary_db_path) -> dict
  needs_rerender(old, new) -> bool
  render_signature(d) -> str
  latest_descriptor(diary_db_path) -> dict | None
"""
from __future__ import annotations
import json
import logging
import re
import sqlite3
import time
from typing import Optional

_log = logging.getLogger("avatar_descriptor")

# Pole, která se NIKDY/téměř nemění — drží rozpoznatelnost napříč evolucí.
LOCKED_FIELDS = ("build", "identity_anchor")
# Pole, jejichž změna vyžaduje nový render (render_signature).
SIGNATURE_FIELDS = ("role", "attire", "age_look", "demeanor", "setting", "palette")
# Všechna pole descriptoru.
ALL_FIELDS = ("role", "attire", "age_look", "build", "demeanor",
              "setting", "palette", "identity_anchor", "change_note")

# Počáteční podoba (butler) — fallback seed, když ani config ani DB nemá nic.
DEFAULT_DESCRIPTOR = {
    "version": 0,
    "role": "english butler",
    "attire": "black tailcoat, white gloves, high collar",
    "age_look": "late 50s",
    "build": "tall, slim",
    "demeanor": "formal and reserved",
    "setting": "wood-panelled hall",
    "palette": "black, white, dark wood",
    "identity_anchor": "same man, consistent face, grey hair, kind eyes",
    "change_note": "počáteční podoba",
}

def _seed(config: dict) -> dict:
    """AVATAR_CONFIG_OVERRIDES_V1 — výchozí podoba: config hans_avatar.seed přes
    DEFAULT_DESCRIPTOR (uživatel edituje základ vzhledu v configu)."""
    out = dict(DEFAULT_DESCRIPTOR)
    seed = (config.get("hans_avatar", {}) or {}).get("seed", {}) or {}
    for f in ALL_FIELDS:
        if seed.get(f):
            out[f] = seed[f]
    return out


def _locked_fields(config: dict) -> set:
    """LOCKED_FIELDS + uživatelská config hans_avatar.extra_locked_fields."""
    extra = (config.get("hans_avatar", {}) or {}).get("extra_locked_fields", []) or []
    return set(LOCKED_FIELDS) | {str(f).strip() for f in extra if str(f).strip()}


def _overrides(config: dict) -> dict:
    """Pole, která VŽDY přebijí LLM i evoluci (uživatelská pevná volba vzhledu)."""
    ov = (config.get("hans_avatar", {}) or {}).get("overrides", {}) or {}
    return {f: str(v).strip() for f, v in ov.items()
            if f in ALL_FIELDS and str(v).strip()}


_SYSTEM = (
    "Jsi návrhář vizuální podoby fiktivní postavy pro obrazový generátor. "
    "Dostaneš popis CHARAKTERU postavy (kdo je, jak se vyvíjí) a její PŘEDCHOZÍ "
    "vzhled. Uprav vzhled tak, aby odpovídal charakteru — ale POUZE v rozsahu, "
    "který je posunem charakteru ospravedlněný. Drobný posun = drobná úprava.\n"
    "PRAVIDLA:\n"
    "- Pole 'identity_anchor' a 'build' jsou ZAMČENÁ — NIKDY je neměň (pořád "
    "tentýž rozpoznatelný člověk).\n"
    "- Pole vzhledu (role, attire, age_look, build, demeanor, setting, palette, "
    "identity_anchor) piš ANGLICKY, stručně, konkrétně (vhodné pro image prompt).\n"
    "- 'change_note' piš ČESKY — jednou větou co a proč ses změnil(o).\n"
    "- Vrať POUZE JSON objekt s klíči: role, attire, age_look, build, demeanor, "
    "setting, palette, identity_anchor, change_note. Žádný markdown, nic navíc."
)


# ── Perzistence (tabulka avatar_descriptors v hans_diary.db) ────────────────
def _ensure_table(db_path: str) -> None:
    conn = None
    try:
        conn = sqlite3.connect(db_path, timeout=5.0)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS avatar_descriptors (
                version    INTEGER PRIMARY KEY,
                ts         REAL NOT NULL,
                signature  TEXT NOT NULL,
                descriptor TEXT NOT NULL,
                rendered   INTEGER DEFAULT 0
            )
        """)
        conn.commit()
    except Exception as _e:
        _log.warning("ensure_table selhal: %s", _e)
    finally:
        if conn is not None:
            conn.close()


def latest_descriptor(diary_db_path: str) -> Optional[dict]:
    """Poslední uložený descriptor jako dict (vč. 'version'), nebo None."""
    _ensure_table(diary_db_path)
    conn = None
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % diary_db_path, uri=True, timeout=3.0)
        row = conn.execute(
            "SELECT version, descriptor FROM avatar_descriptors "
            "ORDER BY version DESC LIMIT 1").fetchone()
        if not row:
            return None
        d = json.loads(row[1])
        d["version"] = row[0]
        return d
    except Exception as _e:
        _log.debug("latest_descriptor: %s", _e)
        return None
    finally:
        if conn is not None:
            conn.close()


def _save_descriptor(diary_db_path: str, descriptor: dict) -> None:
    _ensure_table(diary_db_path)
    conn = None
    try:
        conn = sqlite3.connect(diary_db_path, timeout=5.0)
        conn.execute(
            "INSERT OR REPLACE INTO avatar_descriptors "
            "(version, ts, signature, descriptor, rendered) VALUES (?,?,?,?,0)",
            (int(descriptor.get("version", 1)), time.time(),
             render_signature(descriptor),
             json.dumps(descriptor, ensure_ascii=False)))
        conn.commit()
        _log.info("avatar descriptor v%d uložen (rendered=0): %s",
                  descriptor.get("version"), descriptor.get("change_note", ""))
    except Exception as _e:
        _log.warning("save_descriptor selhal: %s", _e)
    finally:
        if conn is not None:
            conn.close()


# ── Zdroj charakteru (napojení na Severku/identitu, read-only) ──────────────
def _current_core(config: dict, diary_db_path: str) -> str:
    """CORE z aktuální identity (Severka), fallback config.persona.core."""
    try:
        from scripts.hans_identity import IdentityStore
        cur = IdentityStore(config, diary_db_path).current()
        if cur and getattr(cur, "core", ""):
            return cur.core
    except Exception as _e:
        _log.debug("IdentityStore.current selhal: %s", _e)
    # fallback: config core (s dosazeným jménem persony)
    try:
        from scripts.hans_persona import apply_name
        return apply_name((config.get("persona", {}) or {}).get("core", ""), config)
    except Exception:
        return (config.get("persona", {}) or {}).get("core", "")


def _top_themes(diary_db_path: str, limit: int = 6) -> list:
    """Nejsilnější aktivní koníčky jako témata (durable gate je pro avatar moc
    přísný v rané fázi → bereme top podle evidence)."""
    conn = None
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % diary_db_path, uri=True, timeout=3.0)
        rows = conn.execute(
            "SELECT name FROM hobbies WHERE status='active' "
            "ORDER BY evidence_count DESC LIMIT ?", (int(limit),)).fetchall()
        return [r[0] for r in rows if r and r[0]]
    except Exception as _e:
        _log.debug("_top_themes: %s", _e)
        return []
    finally:
        if conn is not None:
            conn.close()


def derive_character(config: dict, diary_db_path: str) -> dict:
    """Deterministicky složí charakter ze Severky + tendencí + koníčků."""
    core = _current_core(config, diary_db_path)
    tend = ""
    try:
        from scripts.hans_tendencies import derive_tendencies
        tend = derive_tendencies(config, diary_db_path).as_text()
    except Exception as _e:
        _log.debug("derive_tendencies selhal: %s", _e)
    themes = _top_themes(diary_db_path)
    return {"core": core, "tendencies": tend, "themes": themes}


# ── Descriptor: signatura + brána ───────────────────────────────────────────
def render_signature(d: dict) -> str:
    return "|".join(str(d.get(f, "")).strip().lower() for f in SIGNATURE_FIELDS)


def needs_rerender(old: Optional[dict], new: dict) -> bool:
    """True = vzhled se posunul natolik, že je třeba nový render."""
    if not old:
        return True
    return render_signature(old) != render_signature(new)


# ── Generování descriptoru (neutrální LLM, inkrementálně) ───────────────────
def _parse_descriptor(raw: str) -> Optional[dict]:
    if not raw:
        return None
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
        return d if isinstance(d, dict) else None
    except Exception:
        return None


def generate_descriptor(config: dict, diary_db_path: str,
                        prev: Optional[dict] = None) -> Optional[dict]:
    """Vyrobí nový descriptor z charakteru (inkrementálně vůči prev). None při chybě."""
    acfg = config.get("hans_avatar", {}) or {}
    model = str(acfg.get("descriptor_model", "qwen2.5:7b"))
    timeout = int(acfg.get("llm_timeout", 120))
    temp = float(acfg.get("temperature", 0.2))

    prev = prev or _seed(config)
    char = derive_character(config, diary_db_path)
    if not char.get("core"):
        _log.warning("avatar: prázdný CORE, descriptor přeskočen")
        return None

    prev_view = {k: prev.get(k, "") for k in ALL_FIELDS}
    user = (
        "CHARAKTER POSTAVY (zdroj pravdy):\n"
        f"Role/identita (CORE):\n{char['core']}\n\n"
        f"Tendence charakteru:\n{char['tendencies'] or '(zatím žádné)'}\n\n"
        f"Hlavní zájmy/témata: {', '.join(char['themes']) or '(žádná)'}\n\n"
        "PŘEDCHOZÍ VZHLED (uprav jen co charakter ospravedlňuje, "
        "identity_anchor a build NECHEJ beze změny):\n"
        f"{json.dumps(prev_view, ensure_ascii=False, indent=2)}"
    )

    try:
        from scripts.ollama_client import ollama_generate
    except ImportError:
        _log.warning("avatar: ollama_client nedostupný")
        return None

    raw = ollama_generate(
        model, user, system=_SYSTEM, config=config, timeout=timeout,
        keep_alive=0,  # MODEL_KEEPALIVE_TIERS_V1 — descriptor model on-demand
        options={"temperature": temp})
    d = _parse_descriptor(raw)
    if not d:
        _log.warning("avatar: LLM nevrátil parsovatelný descriptor")
        return None

    # Slož finální descriptor:
    #   overrides → vždy přebijí (uživatelská pevná volba, AVATAR_CONFIG_OVERRIDES_V1)
    #   locked    → drž z prev (LLM je nesmí měnit)
    #   jinak     → LLM výstup, fallback prev
    locked = _locked_fields(config)
    overrides = _overrides(config)
    out = {}
    for f in ALL_FIELDS:
        if f in overrides:
            out[f] = overrides[f]
        elif f in locked:
            out[f] = prev.get(f, _seed(config).get(f, ""))
        else:
            out[f] = str(d.get(f, "")).strip() or prev.get(f, "")
    out["version"] = int(prev.get("version", 0)) + 1
    return out


def maybe_update_descriptor(config: dict, diary_db_path: str) -> Optional[dict]:
    """Entry point (volá routine ZA Severčiným checkem). Vyrobí descriptor z
    aktuální identity; uloží JEN když se vzhled posunul (needs_rerender) nebo je
    první. Vrací nový descriptor (k renderu) nebo None (beze změny). Nikdy nehází."""
    try:
        prev = latest_descriptor(diary_db_path)
        new = generate_descriptor(config, diary_db_path, prev=prev)
        if not new:
            return None
        if needs_rerender(prev, new):
            _save_descriptor(diary_db_path, new)  # rendered=0 → render větev dožene
            _log.info("avatar: nová podoba v%d (sig změněna) — čeká na render",
                      new["version"])
            return new
        _log.info("avatar: charakter beze změny vzhledu (v%d drží)",
                  (prev or {}).get("version", 0))
        return None
    except Exception as _e:
        _log.warning("maybe_update_descriptor selhal: %s", _e)
        return None


# ── Smoke (python3 -m scripts.avatar_descriptor) ────────────────────────────
if __name__ == "__main__":
    import sys
    cfg = json.load(open("config.json", encoding="utf-8"))
    db = cfg.get("diary_db", "data/hans_diary.db")
    print("=== charakter (ze Severky/tendencí/koníčků) ===")
    ch = derive_character(cfg, db)
    print("CORE:", (ch["core"] or "")[:120])
    print("témata:", ch["themes"])
    print("tendence:", (ch["tendencies"] or "(žádné)")[:160])
    print("\n=== generuji descriptor (qwen, keep_alive=0) ===")
    prev = latest_descriptor(db)
    print("předchozí verze:", (prev or {}).get("version", "žádná"))
    d = generate_descriptor(cfg, db, prev=prev)
    if d:
        print(json.dumps(d, ensure_ascii=False, indent=2))
        print("render_signature:", render_signature(d))
        print("needs_rerender vs prev:", needs_rerender(prev, d))
    else:
        print("descriptor se nevygeneroval (viz log)")
    sys.exit(0)
