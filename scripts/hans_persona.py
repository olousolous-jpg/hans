"""scripts/hans_persona.py

JEDINÝ zdroj Hansovy identity. Krok k Severce: jedno místo k přepisu identity.

Vrstvy:
  CORE       — kdo Hans JE. Severka jednou přepisuje TOHLE.
  LANGUAGE   — jak Hans MLUVÍ univerzálně (čeština, mužský rod, žádné emoji).
  ADDRESS    — oslovovací pravidla (vokativ Stando/Jano). JEN chat s lidmi.
  INTERESTS  — co Hanse ZAJÍMÁ. Krok C: čte z deníku (event_type=interest_update),
               nejnovější NAHRAZUJÍ seed. Prázdný deník -> fallback interests_seed.
  LAYER      — kontextová modulace. NEMĚNÍ identitu.

Krok C (zájmy z DB):
  Zdroj pravdy = tabulka diary, event_type='interest_update'. Čte se READ-ONLY
  (system prompt se nikdy nesmí dotknout dat). Cesta k DB se bere z
  config['diary_db'] (fallback data/hans_diary.db). Selhání -> tichý fallback
  na seed (prompt se musí postavit vždy).

API:
  persona_core(config, with_address=True) -> str
  persona_interests(config) -> str        # čte deník, fallback seed
  recent_interests(db_path, limit=5) -> str   # read-only helper
  persona(config, context="chat", with_interests=False) -> str
"""
from __future__ import annotations
import logging

_log = logging.getLogger("hans_persona")

# ── Defaulty (fallback, když config.persona chybí) ──────────────────────────
# PERSONA_NAME_CONFIGURABLE_V1 — jméno persony je SSOT v config['persona']['name']
# (default "Hans"). Prompty/CORE píšou token {name}; persona_core ho nahradí.
# Severka přepisuje CORE bez jména (jméno = separátní pole, identitu nemění).
_DEFAULT_NAME = "Hans"
_DEFAULT_CORE = (
    "Jsi {name}, tichý anglický majordomus z devatenáctého století. "
    "Vyjadřuješ se stručně, formálně a s důstojností."
)
_DEFAULT_LANG = (
    "Mluvíš česky. Jsi mužského rodu. Nepoužíváš moderní výrazy. "
    "Nepoužíváš emoji, smajlíky ani žádné Unicode symboly — píšeš pouze čistý text."
)
_DEFAULT_ADDRESS = (
    "Při oslovení muže používej vokativ: Stando (ne Standa). "
    "Při oslovení ženy používej vokativ: Jano (ne Jana). "
    "Nikdy nepoužívej nominativ jako oslovení. Nikdy neopakuj jméno na konci věty."
)

_ADDRESS_CTX = {"chat"}

_CONTEXT_LAYERS: dict[str, str] = {
    "chat":          "",
    "idle":          "",
    "introspection": "Právě není nikdo doma a ty v tichu přemýšlíš.",
    "web_read":      "Reaguješ na text, který sis právě přečetl.",
    "questions":     "Vyjadřuješ se jednou krátkou větou.",
}


def persona_name(config: dict) -> str:
    """PERSONA_NAME_CONFIGURABLE_V1 — jméno persony (SSOT). Default 'Hans'."""
    return (config.get("persona", {}) or {}).get("name", _DEFAULT_NAME) or _DEFAULT_NAME


def apply_name(text: str, config: dict) -> str:
    """Nahradí token {name} v promptu/textu nakonfigurovaným jménem persony.
    Bezpečné (.replace, ne .format) — nevadí ostatní '{' v textu."""
    if not text:
        return text
    return text.replace("{name}", persona_name(config))


def persona_core(config: dict, diary=None, with_address: bool = True) -> str:
    """Jádro identity = CORE + jazyk [+ oslovovací pravidla].

    with_address=True (default) == dnešní greeting.system_prompt (chat).
    with_address=False vynechá vokativ pravidla (dialog s Kolačem, task místa).
    PERSONA_NAME_CONFIGURABLE_V1: token {name} v CORE/promptech → jméno z configu."""
    p = config.get("persona", {})
    core = p.get("core", _DEFAULT_CORE)
    lang = p.get("language_rules", _DEFAULT_LANG)
    parts = [core, lang]
    if with_address:
        addr = p.get("address_rules", _DEFAULT_ADDRESS)
        if addr:
            parts.append(addr)
    return apply_name(" ".join(s.strip() for s in parts if s and s.strip()), config)


def recent_interests(db_path: str, limit: int = 5) -> str:
    """READ-ONLY: posledních `limit` interest_update not z deníku jako text.

    Vrací '' když nic / chyba. NIKDY nepíše do DB (mode=ro). NIKDY nevyhazuje
    výjimku nahoru — system prompt se musí postavit vždy."""
    if not db_path:
        return ""
    import sqlite3
    conn = None
    try:
        # read-only URI — fyzicky nemůže zapsat
        conn = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True, timeout=2.0)
        rows = conn.execute(
            "SELECT note FROM diary WHERE event_type='interest_update' "
            "ORDER BY ts DESC LIMIT ?", (int(limit),)
        ).fetchall()
        notes = [r[0].strip() for r in rows if r and r[0] and r[0].strip()]
        return "; ".join(notes)
    except Exception as _e:
        _log.debug("recent_interests read failed: %s", _e)
        return ""
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def persona_interests(config: dict, diary=None) -> str:
    """Zájmy jako raw text. Krok C: nejnovější interest_update z deníku
    NAHRAZUJÍ seed. Prázdný deník / chyba -> fallback interests_seed."""
    db_path = config.get("diary_db", "data/hans_diary.db")
    learned = recent_interests(db_path, limit=5)
    if learned:
        return learned
    return config.get("persona", {}).get("interests_seed", "")


def recent_stances(db_path: str, limit: int = 5) -> str:
    """READ-ONLY: nejsilnejsi aktivni stance (claim) z tabulky stances. STANCE_PERSONA_READ_V1
    Vraci '' kdyz nic / chyba. NIKDY nepise (mode=ro). NIKDY nevyhazuje vyjimku."""
    if not db_path:
        return ""
    import sqlite3
    conn = None
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True, timeout=2.0)
        rows = conn.execute(
            "SELECT claim FROM stances WHERE status='active' "
            "ORDER BY confidence DESC, evidence_count DESC LIMIT ?", (int(limit),)
        ).fetchall()
        claims = [r[0].strip() for r in rows if r and r[0] and r[0].strip()]
        return "; ".join(claims)
    except Exception as _e:
        _log.debug("recent_stances read failed: %s", _e)
        return ""
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def persona_stances(config: dict, diary=None) -> str:
    """Hansovy postoje jako raw text s lehkym labelem. '' kdyz zadne."""
    db_path = config.get("diary_db", "data/hans_diary.db")
    learned = recent_stances(db_path, limit=5)
    if learned:
        return "Postoje: " + learned
    return ""


def recent_goal(db_path: str) -> str:
    """READ-ONLY: aktivni cil (topic + stari + expected) jako text. GOAL_PERSONA_READ_V1
    Vraci '' kdyz zadny / chyba. NIKDY nepise (mode=ro). NIKDY nevyhazuje vyjimku."""
    if not db_path:
        return ""
    import sqlite3, time as _t
    conn = None
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True, timeout=2.0)
        row = conn.execute(
            "SELECT topic, opened_at, target_days, expected FROM hans_goals "
            "WHERE status='active' ORDER BY opened_at DESC LIMIT 1"
        ).fetchone()
        if not row or not row[0]:
            return ""
        topic = str(row[0]).strip()
        age = max(1, int((_t.time() - (row[1] or 0)) / 86400) + 1)
        target = row[2] or 5
        expected = (str(row[3]).strip() if len(row) > 3 and row[3] else "")
        out = f"Tvůj současný cíl: {topic} (už {age}. den z {target})."
        if expected:
            out += f" Čekáš si od něj: {expected}."
        return out
    except Exception as _e:
        _log.debug("recent_goal read failed: %s", _e)
        return ""
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def persona_goal(config: dict, diary=None) -> str:
    """Hansuv aktivni cil jako raw text. '' kdyz zadny."""
    db_path = config.get("diary_db", "data/hans_diary.db")
    return recent_goal(db_path)


def persona(config: dict, context: str = "chat", diary=None,
            with_interests: bool = False) -> str:
    """Kompletní system prompt = jádro [+ zájmy] + lehká kontextová vrstva."""
    with_address = context in _ADDRESS_CTX
    out = persona_core(config, diary, with_address=with_address)
    if with_interests:
        itr = persona_interests(config)
        if itr:
            out += " " + itr
    layer = _CONTEXT_LAYERS.get(context, "")
    if layer:
        out += "\n\n" + layer
    return out


# ── Smoke (spustitelné: python3 scripts/hans_persona.py) ────────────────────
if __name__ == "__main__":
    import json
    import sys
    cfg = {}
    try:
        with open("config.json", encoding="utf-8") as fh:
            cfg = json.load(fh)
    except Exception as exc:  # noqa
        print("WARN: config.json nenačten (%s), použiju defaulty" % exc)

    print("=== persona_core(with_address=True)  [chat] ===")
    print(persona_core(cfg, with_address=True))
    print("\n=== persona_core(with_address=False) [dialog/task] ===")
    print(persona_core(cfg, with_address=False))

    db = cfg.get("diary_db", "data/hans_diary.db")
    print("\n=== recent_interests (z deníku, read-only) ===")
    print(repr(recent_interests(db)))
    print("\n=== persona_interests (deník NEBO seed) ===")
    print(repr(persona_interests(cfg)))

    # Regrese: jádro s adresami se musí přesně rovnat greeting.system_prompt
    want = apply_name(cfg.get("greeting", {}).get("system_prompt"), cfg)
    if want:
        got = persona_core(cfg, with_address=True)
        print("\n=== REGRESE persona_core(with_address=True) == greeting.system_prompt ===")
        print("MATCH" if got == want else "DIFFERS")
    sys.exit(0)
