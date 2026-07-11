"""HANS_BRIEF_V1 — destilér „studium → nejlepší prompt/brief pro tvorbu díla".

Chybějící most mezi tím, CO se Hans naučil, a tím, jak to POUŽIJE při tvorbě
díla. Dosavadní esej (hans_authorship) je abstraktní, protože grounding je tenký
(RAG k=3) a píše se Hansovým reflexivním hlasem → konkrétní nastudovaná látka
(kerning, teorie barev, UX) se do díla skoro nedostane.

KLÍČOVÝ PRINCIP (rozhodnutí uživatele 11.7.): tvorba artefaktu NENÍ o Hansově
hlasu. Je o tom mít NEJLEPŠÍ MOŽNÝ PROMPT z toho, co se naučil. Persona se tu
vypouští — cílem je technicky přesný, strukturovaný, GROUNDOVANÝ brief, který
dotlačí nástroj (coder LLM / SDXL / esej) k dobrému výsledku.

Vzor: F1 rewriter (člověk→počítač), reasoning tier (EN kognice → CZ hlas) — tady
„studium → tool prompt", hlas se vypouští. Anti-konfabulace: brief se staví
VÝHRADNĚ z reálných studijních poznámek (deník study_note + study_mastery), model
nesmí přidat princip, který v látce není.

V1 = JEN destilér + `/brief` k prohlédnutí. Exekuce (esej/coder/obraz) = další krok.
"""
from __future__ import annotations

import json
import sqlite3
import time
from typing import Optional

from scripts.logger import get_logger

_log = get_logger("hans_brief")

# per-cíl: jazyk + jak strukturovat brief (nástroj, ne persona)
_TARGETS = {
    "coder": {
        "lang": "ENGLISH",
        "desc": "a code-generation model that will build a real artifact "
                "(e.g. a web page / dashboard / UI applying the design)",
        "shape": "GOAL, APPLIED PRINCIPLES (bullet list — each MUST come from "
                 "the notes), REQUIREMENTS, CONSTRAINTS, suggested tech "
                 "(HTML/CSS unless notes imply otherwise)",
    },
    "image": {
        "lang": "ENGLISH",
        "desc": "an image-generation model (SDXL) that will render a visual "
                "applying the learned aesthetic principles",
        "shape": "one dense visual prompt: subject, composition, palette, "
                 "typography/style cues (all drawn from the notes), plus a "
                 "short NEGATIVE list",
    },
    "essay": {
        "lang": "ČESKY",
        "desc": "autor, který napíše KONKRÉTNÍ, aplikační text (ne abstraktní "
                "úvahu) opřený o nastudované",
        "shape": "konkrétní osnova sekcí; u KAŽDÉ sekce vypíše, které "
                 "nastudované pojmy/principy má pokrýt (z poznámek)",
    },
}
_DEFAULT_TARGET = "coder"


def _cfg(config: dict) -> dict:
    return (config or {}).get("brief", {}) or {}


def enabled(config: dict) -> bool:
    return bool(_cfg(config).get("enabled", True))


# ── sběr studijní látky (KOMPLETNÍ, deterministický, BEZ LLM) ─────────────────
def gather_study_material(db_path: str, topic: str,
                          max_chars: int = 12000) -> dict:
    """Vytáhne VŠECHNY study_note pro téma (ne RAG k=3) + study_mastery z deníku.
    Vrací {topic, notes:[{sub,text}], mastery, note_count, chars, titles[]}."""
    out = {"topic": topic, "notes": [], "mastery": "", "note_count": 0,
           "chars": 0, "titles": []}
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True)
        rows = conn.execute(
            "SELECT title, COALESCE(NULLIF(data,''),note) FROM diary "
            "WHERE event_type='study_note' AND title LIKE ? "
            "ORDER BY ts", ("Studium: %s%%" % topic,)).fetchall()
        total = 0
        for title, text in rows:
            text = (text or "").strip()
            if not text:
                continue
            sub = title.split("—", 1)[1].strip() if "—" in (title or "") else title
            out["notes"].append({"sub": sub, "text": text})
            out["titles"].append(sub)
            total += len(text)
            if total >= max_chars:
                break
        # mistrovská reflexe (nejnovější k tématu)
        m = conn.execute(
            "SELECT COALESCE(NULLIF(data,''),note) FROM diary WHERE "
            "event_type='study_mastery' AND (title LIKE ? OR note LIKE ? OR "
            "data LIKE ?) ORDER BY ts DESC LIMIT 1",
            ("%%%s%%" % topic, "%%%s%%" % topic, "%%%s%%" % topic)).fetchone()
        if m and m[0]:
            out["mastery"] = m[0].strip()
        conn.close()
        out["note_count"] = len(out["notes"])
        out["chars"] = total + len(out["mastery"])
    except Exception as e:
        _log.warning("brief gather: %s", e)
    return out


def _material_block(mat: dict) -> str:
    parts = []
    for n in mat["notes"]:
        parts.append("• %s:\n%s" % (n["sub"], n["text"]))
    if mat["mastery"]:
        parts.append("• Souhrn (co jsem zvládl):\n%s" % mat["mastery"])
    return "\n\n".join(parts)


# ── destilace do briefu (analytický model, NE persona; grounded) ─────────────
def build_brief(config: dict, db_path: str, topic: str,
                target: str = _DEFAULT_TARGET) -> dict:
    """Studium → nejlepší tool prompt. Vrací {status, topic, target, brief,
    note_count, titles}. status: built/idle/deferred. Deferral-safe."""
    if not enabled(config):
        return {"status": "idle", "reason": "vypnuto"}
    tgt = target if target in _TARGETS else _DEFAULT_TARGET
    mat = gather_study_material(db_path, topic,
                                int(_cfg(config).get("max_material_chars", 12000)))
    if mat["note_count"] == 0:
        return {"status": "idle", "reason": "žádné studijní poznámky pro téma"}
    spec = _TARGETS[tgt]
    system = (
        "Jsi prompt engineer. Níže jsou VLASTNÍ studijní poznámky jednoho člověka "
        "k dané doméně (česky). Destiluj je do NEJLEPŠÍHO možného BRIEFU/PROMPTU "
        "pro %s.\n"
        "PRAVIDLA (dělba práce autor × nástroj):\n"
        "- ROZSAH ber z poznámek: zahrň JEN principy a témata, která si člověk "
        "skutečně nastudoval (jsou v poznámkách). Nepřidávej domény ani principy, "
        "které nestudoval.\n"
        "- Jakmile je princip v poznámkách JMENOVÁN (např. zlatý řez, pravidlo "
        "třetin, teorie barev, vizuální hierarchie), uveď ho jako PEVNÝ POŽADAVEK. "
        "NEoznačuj ho za volitelný jen proto, že poznámka neobsahuje jeho definici, "
        "vzorec ani přesné hodnoty — JAK ho realizovat je úkol NÁSTROJE, který "
        "brief vykoná (ten definice a implementaci zná). TY určuješ, CO se má "
        "aplikovat; nástroj ví JAK.\n"
        "- Nevymýšlej jen KONKRÉTNÍ hodnoty (hex barvy, px, názvy fontů), které v "
        "poznámkách nejsou — ať je zvolí nástroj podle jmenovaných principů.\n"
        "- Výstup v jazyce %s, technicky, bez persony a bez „já“. Struktura: %s."
        % (spec["desc"], spec["lang"], spec["shape"])
    )
    user = ("Doména: %s\n\nMÉ STUDIJNÍ POZNÁMKY:\n%s\n\nNAPIŠ BRIEF:"
            % (topic, _material_block(mat)))
    brief = None
    try:
        from scripts.ollama_client import ollama_generate
        model = (_cfg(config).get("model")
                 or (config.get("evening_reflection", {}) or {}).get("model")
                 or "jobautomation/OpenEuroLLM-Czech:latest")
        brief = ollama_generate(
            model, user, system=system, config=config,
            timeout=int(_cfg(config).get("llm_timeout", 180)), keep_alive=0,
            options={"temperature": float(_cfg(config).get("temperature", 0.3)),
                     "num_ctx": int(_cfg(config).get("num_ctx", 8192)),
                     "num_predict": int(_cfg(config).get("num_predict", 700))})
    except Exception as e:
        _log.debug("brief llm: %s", e)
    if not brief or not brief.strip():
        return {"status": "deferred", "reason": "LLM nedostupný"}
    brief = brief.strip()
    BriefStore(db_path).save(topic, tgt, brief, mat["titles"])
    _log.info("brief: '%s' (%s) z %d poznámek → %d zn", topic, tgt,
              mat["note_count"], len(brief))
    return {"status": "built", "topic": topic, "target": tgt, "brief": brief,
            "note_count": mat["note_count"], "titles": mat["titles"]}


# ── store (aby šel brief prohlédnout a později spustit) ──────────────────────
class BriefStore:
    def __init__(self, db_path: str):
        self.db_path = db_path
        try:
            c = sqlite3.connect(db_path, timeout=10)
            c.execute("""CREATE TABLE IF NOT EXISTS work_briefs (
                id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, topic TEXT,
                target TEXT, brief TEXT, source_notes TEXT)""")
            c.commit()
            c.close()
        except Exception as e:
            _log.warning("brief store ensure: %s", e)

    def save(self, topic: str, target: str, brief: str, titles: list) -> int:
        c = sqlite3.connect(self.db_path, timeout=10)
        cur = c.execute(
            "INSERT INTO work_briefs (ts, topic, target, brief, source_notes) "
            "VALUES (?,?,?,?,?)",
            (time.time(), topic, target, brief, " | ".join(titles or [])))
        c.commit()
        pid = cur.lastrowid
        c.close()
        return pid

    def latest(self, topic: str = None) -> Optional[dict]:
        c = sqlite3.connect("file:%s?mode=ro" % self.db_path, uri=True)
        if topic:
            r = c.execute("SELECT id,ts,topic,target,brief,source_notes FROM "
                          "work_briefs WHERE topic=? ORDER BY id DESC LIMIT 1",
                          (topic,)).fetchone()
        else:
            r = c.execute("SELECT id,ts,topic,target,brief,source_notes FROM "
                          "work_briefs ORDER BY id DESC LIMIT 1").fetchone()
        c.close()
        if not r:
            return None
        return dict(zip(("id", "ts", "topic", "target", "brief", "source_notes"),
                        r))


def completed_study_topics(db_path: str) -> list:
    """Témata s dokončeným studijním programem (kandidáti na brief)."""
    try:
        c = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True)
        rows = c.execute("SELECT topic FROM study_program WHERE "
                         "status='completed' ORDER BY id DESC").fetchall()
        c.close()
        return [r[0] for r in rows]
    except Exception:
        return []


if __name__ == "__main__":
    import sys
    cfg = json.loads(open("config.json", encoding="utf-8").read())
    db = "data/hans_diary.db"
    topic = sys.argv[1] if len(sys.argv) > 1 else "Design"
    target = sys.argv[2] if len(sys.argv) > 2 else _DEFAULT_TARGET
    print("=== látka pro '%s' ===" % topic)
    mat = gather_study_material(db, topic)
    print("poznámek: %d | znaků: %d | mastery: %s" % (
        mat["note_count"], mat["chars"], "ano" if mat["mastery"] else "ne"))
    print("témata:", ", ".join(mat["titles"]))
    print("\n=== brief (%s) ===" % target)
    r = build_brief(cfg, db, topic, target)
    print("status:", r["status"])
    if r.get("brief"):
        print(r["brief"])
