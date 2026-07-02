#!/usr/bin/env python3
"""hans_dashboard.py — HANS_DASHBOARD_PROPOSAL_V1 (Tier 1)

Hans po dostudování Designu NAVRHNE VLASTNÍ podobu svého dashboardu:
  (a) psaná designová kritika + návrh (paleta / whitespace / hierarchie),
      groundovaná ve FAKTECH o současném designu (extrahovaných z
      templates/index.html — CSS proměnné, font, layout) a v jeho VLASTNÍCH
      studijních poznámkách (study_note Design + study_mastery);
  (b) SDXL mockup přes existující art pipeline (hans_art._render_image,
      vlastní scene system pro UI koncept) → galerie source='dashboard'.

Uzavírá oblouk studium → vkus → aplikace na sebe. NIC neaplikuje na reálný
web (Tier 2 = CSS/HTML diff se schválením, s coder tierem — later).

Kódy (deferral-safe): 'proposed' / 'idle' (vypnuto / studium nedokončeno /
už navrženo) / 'deferred' (LLM/ComfyUI dole → retry).
"""
from __future__ import annotations

import os
import re
import json
import time
import logging
import sqlite3
from typing import Optional

_log = logging.getLogger("hans_dashboard")

_TEMPLATE = "templates/index.html"


def _cfg(config: dict) -> dict:
    return config.get("dashboard_proposal", {}) or {}


def _persona_name(config: dict) -> str:
    try:
        from scripts.hans_persona import persona_name
        return persona_name(config)
    except Exception:
        return "Hans"


# ── grounding 1: fakta o SOUČASNÉM designu (z šablony, ne z paměti LLM) ──────
def design_facts(template_path: str = _TEMPLATE) -> str:
    """Extrahuj z index.html reálná fakta: CSS proměnné (paleta), font,
    layout kostru. '' když šablona chybí."""
    try:
        html = open(template_path, encoding="utf-8").read()
    except Exception as e:
        _log.warning("design_facts: šablona nedostupná: %s", e)
        return ""
    facts = []
    vars_ = re.findall(r"(--[a-z0-9]+):\s*([^;]+);", html)
    if vars_:
        pal = ", ".join(f"{k}={v.strip()}" for k, v in vars_[:14])
        facts.append(f"Barevná paleta (CSS proměnné): {pal}")
    m = re.search(r"font-family:\s*([^;]+);", html)
    if m:
        facts.append(f"Písmo: {m.group(1).strip()}")
    m = re.search(r"background:\s*(linear-gradient[^;]+);", html)
    if m:
        facts.append(f"Pozadí: {m.group(1).strip()[:120]}")
    # kostra: sidebar + karty + grid
    if "app-side" in html:
        facts.append("Layout: levý sidebar (menu + avatar + status), "
                     "hlavní sloupec s kartami (max-width ~1100px)")
    grids = len(re.findall(r"class=\"card", html))
    if grids:
        facts.append(f"Obsah: ~{grids} karet (deník, galerie, stav, Kodi, …), "
                     "gridy 2-3 sloupce, sklo/glass efekt")
    return "\n".join(f"- {f}" for f in facts)


# ── grounding 2: Hansovy studijní poznámky o designu ─────────────────────────
def _study_material(diary_db_path: str, topic_like: str,
                    max_chars: int = 7000) -> str:
    """study_note (data) k tématu + případná study_mastery reflexe."""
    out, total = [], 0
    try:
        db = sqlite3.connect("file:%s?mode=ro" % diary_db_path, uri=True,
                             timeout=5.0)
        rows = db.execute(
            "SELECT title, COALESCE(NULLIF(data,''),note) FROM diary "
            "WHERE event_type IN ('study_note','study_mastery') "
            "AND lower(title) LIKE ? ORDER BY ts ASC LIMIT 20",
            (f"%{topic_like.lower()}%",)).fetchall()
        db.close()
    except Exception as e:
        _log.warning("_study_material: %s", e)
        return ""
    for title, content in rows:
        c = (content or "").strip()
        if not c:
            continue
        if total + len(c) > max_chars:
            c = c[: max(0, max_chars - total)]
        out.append(f"[{title}]\n{c}")
        total += len(c)
        if total >= max_chars:
            break
    return "\n\n".join(out)


def _study_completed(diary_db_path: str, topic_like: str) -> bool:
    """Dokončil Hans studijní program na téma?"""
    try:
        db = sqlite3.connect("file:%s?mode=ro" % diary_db_path, uri=True,
                             timeout=5.0)
        row = db.execute(
            "SELECT 1 FROM study_program WHERE status='completed' "
            "AND lower(topic) LIKE ? LIMIT 1",
            (f"%{topic_like.lower()}%",)).fetchone()
        db.close()
        return bool(row)
    except Exception:
        return False


def latest_proposal(diary_db_path: str) -> Optional[dict]:
    try:
        db = sqlite3.connect("file:%s?mode=ro" % diary_db_path, uri=True,
                             timeout=5.0)
        row = db.execute(
            "SELECT ts, note, data FROM diary "
            "WHERE event_type='dashboard_proposal' "
            "ORDER BY ts DESC LIMIT 1").fetchone()
        db.close()
    except Exception:
        return None
    if not row:
        return None
    d = {}
    try:
        d = json.loads(row[2] or "{}")
    except Exception:
        pass
    return {"ts": row[0], "text": row[1] or "", "path": d.get("path", "")}


# ── kritika + návrh (hans-czech = Hansův vkus a hlas) ────────────────────────
def _generate_proposal(config: dict, facts: str, study: str) -> str:
    from scripts.ollama_client import ollama_chat
    c = _cfg(config)
    name = _persona_name(config)
    system = (
        f"Jsi {name}. Dostudoval jsi obor DESIGN (poznámky níže jsou tvé "
        "vlastní) a teď máš poprvé příležitost APLIKOVAT svůj vkus na sebe: "
        "navrhnout novou podobu své vlastní webové nástěnky (dashboardu). "
        "Níže máš FAKTA o její současné podobě.\n"
        "Napiš svým hlasem, česky:\n"
        "1) KRITIKA (1 odstavec) — co na současném designu funguje a co ne "
        "(paleta, bílé místo, hierarchie, typografie). Opři se o to, co ses "
        "naučil — kde to jde, zmiň princip ze svých poznámek.\n"
        "2) NÁVRH (1-2 odstavce) — jak by nástěnka měla vypadat podle tebe: "
        "paleta (konkrétní ladění), typografie, rozvržení, atmosféra. "
        "Buď konkrétní, ale drž se proveditelného (webová stránka).\n"
        "Vyjdi POUZE z faktů a svých poznámek — nevymýšlej prvky, které "
        "nástěnka nemá. Bez emoji, bez nadpisů typu 'KRITIKA:' — plynulý text, "
        "3-4 odstavce celkem.")
    user = (f"FAKTA O SOUČASNÉ NÁSTĚNCE:\n{facts}\n\n"
            f"TVÉ STUDIJNÍ POZNÁMKY (Design):\n{study}\n\n"
            "Napiš kritiku a svůj návrh.")
    try:
        raw = ollama_chat(
            str(c.get("model") or config.get("models", {}).get(
                "dialog", "hans-czech:latest")),
            [{"role": "system", "content": system},
             {"role": "user", "content": user}],
            config=config,
            timeout=int(c.get("llm_timeout", 300)),
            options={"temperature": float(c.get("temperature", 0.7)),
                     "num_ctx": int(c.get("num_ctx", 8192)),
                     "num_predict": int(c.get("num_predict", 700))})
    except Exception as e:
        _log.warning("_generate_proposal: %s", e)
        return ""
    return (raw or "").strip()


# ── SDXL mockup (reuse art pipeline, vlastní scene system) ───────────────────
_MOCKUP_SCENE_SYSTEM = (
    "You are an art director writing ONE Stable Diffusion XL prompt in ENGLISH "
    "for a UI DESIGN CONCEPT image: a modern smart-home assistant web dashboard, "
    "shown as a clean flat screen mockup (browser window on subtle background). "
    "Derive palette, mood and layout hints from the designer's proposal below "
    "(Czech). Include: 'UI design, web dashboard interface mockup, flat design, "
    "clean layout'. NO people, no hands, no photo of a room. Reply in ENGLISH "
    "ONLY — no Chinese/Japanese. Output ONLY the prompt, one paragraph."
)


def run_dashboard_proposal(config: dict, diary_db_path: str,
                           force: bool = False) -> str:
    """Jedna session návrhu dashboardu. Kódy:
       'proposed' — kritika+návrh (a pokud šlo, i mockup) uloženy
       'idle'     — vypnuto / studium nedokončeno / už navrženo (a ne force)
       'deferred' — LLM dole → retry."""
    c = _cfg(config)
    if not c.get("enabled", True):
        return "idle"
    topic = str(c.get("study_topic", "design"))
    if not force:
        if not _study_completed(diary_db_path, topic):
            return "idle"
        if latest_proposal(diary_db_path):
            return "idle"     # jednorázově; znovu jen ručně (/dashboard teď)
    facts = design_facts()
    study = _study_material(diary_db_path, topic,
                            int(c.get("study_max_chars", 7000)))
    if not facts or not study:
        _log.info("dashboard: chybí grounding (facts=%s, study=%s zn) → idle",
                  bool(facts), len(study))
        return "idle"
    text = _generate_proposal(config, facts, study)
    if not text or len(text) < 120:
        _log.info("dashboard: návrh se nevygeneroval (LLM dole?) — retry")
        return "deferred"
    # mockup — best effort (ComfyUI dole → návrh se uloží i bez obrázku)
    rel_path = ""
    try:
        from scripts.hans_art import _render_image, _log_artwork
        res = _render_image(config, "Návrh mé nástěnky", text, diary_db_path,
                            scene_system=_MOCKUP_SCENE_SYSTEM,
                            scene_intro="")
        if res:
            rel_path, prompt, _vd = res
            try:
                db = sqlite3.connect(diary_db_path, timeout=5.0)
                db.execute(
                    "INSERT INTO diary (ts,event_type,title,note,data) "
                    "VALUES (?,?,?,?,?)",
                    (time.time(), "artwork", "Návrh mé nástěnky",
                     "Mockup mého vlastního dashboardu — navržený z toho, "
                     "co jsem o designu nastudoval.",
                     json.dumps({"path": rel_path, "prompt": prompt,
                                 "source": "dashboard",
                                 "painted_ts": time.time()},
                                ensure_ascii=False)))
                db.commit()
                db.close()
            except Exception as e:
                _log.warning("dashboard: log artwork failed: %s", e)
    except Exception as e:
        _log.warning("dashboard: mockup selhal (%s) — ukládám jen text", e)
    try:
        db = sqlite3.connect(diary_db_path, timeout=5.0)
        db.execute(
            "INSERT INTO diary (ts,event_type,title,note,data) VALUES (?,?,?,?,?)",
            (time.time(), "dashboard_proposal", "Návrh mé nástěnky", text,
             json.dumps({"path": rel_path}, ensure_ascii=False)))
        db.commit()
        db.close()
    except Exception as e:
        _log.warning("dashboard: diary write failed: %s", e)
        return "deferred"
    _log.info("dashboard: Hans navrhl svou nástěnku (%d zn%s)",
              len(text), ", + mockup" if rel_path else ", bez mockupu")
    return "proposed"


if __name__ == "__main__":
    print("=== design_facts ===")
    print(design_facts())
    print("\n=== study material (prvních 300 zn) ===")
    print(_study_material("data/hans_diary.db", "design")[:300])
    print("\n=== completed? ===",
          _study_completed("data/hans_diary.db", "design"))
