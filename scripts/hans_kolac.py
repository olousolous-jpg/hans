#!/usr/bin/env python3
"""
HANS_KOLAC_MIND_V1 — Koláč jako DŮSTOJNÝ OPONENT (vlastní mysl + paměť).

Dosud byl Koláč jen statický popis povahy a obě strany dialogu psal JEDEN model
(hans-czech) → Hans se hádal sám se sebou, Koláč neměl čím oponovat. Tato vrstva
dává Koláčovi:
  1. VLASTNÍ DOKTRÍNU (světonázor) — stabilní osu sporu: suchý empirik/skeptik
     tam, kde je Hans romantik/tradicionalista. HUMOR zachován (ironie, plyšák
     bez nohou) — vtipný protihráč, ne zatrpklý suchar.
  2. VLASTNÍ PAMĚŤ — pamatuje si, jaké pozice zaujal, aby byl konzistentní a mohl
     navázat ("posledně jsem tvrdil…"). Tabulka `kolac_memory` v hans_diary.db.

Spolu s oddělenou generací (Koláčovy repliky generuje vlastní system prompt v
hans_dialog) tím vzniká skutečná dialektika dvou myslí, ne self-talk.
"""
from __future__ import annotations

import logging
import re
import sqlite3
import time
from typing import List, Optional

_log = logging.getLogger("hans_kolac")

_WS = re.compile(r"\s+")


def _norm(s: str) -> str:
    return _WS.sub(" ", (s or "").strip().lower())


# KOLAC_NAME_CONFIGURABLE_V1 — jméno společníka je konfigurovatelné
# (hans_dialog.kolac_name, default „Koláč"). kolac_name() vrací zvolené jméno;
# localize_kolac() přepíše skloňované tvary výchozího jména v promptech/textech
# na zvolené (no-op u defaultu) — jeden bod místo desítek editů.
def kolac_name(config: dict) -> str:
    return (str((config.get("hans_dialog", {}) or {}).get(
        "kolac_name", "Koláč")).strip() or "Koláč")


# Tvary výchozího jména (nom/gen/dat/vok/instr + přivlastňovací), delší první.
_KOLAC_FORMS = sorted([
    "KOLÁČŮV", "KOLAČŮV", "Koláčova", "Kolačova", "Koláčovy", "Kolačovy",
    "Koláčův", "Kolačův", "Koláčově", "Kolačově", "Koláčovi", "Kolačovi",
    "Koláčem", "Kolačem", "Koláče", "Kolače", "Koláči", "Kolači",
    "Koláč", "Kolač",
], key=len, reverse=True)


def localize_kolac(text: str, config: dict) -> str:
    """Přepíše tvary výchozího jména „Koláč" na zvolené jméno. No-op u defaultu
    nebo prázdného textu. Pastry „koláč" (malé k) se NEmění (jen velké K)."""
    kname = kolac_name(config)
    if not text or kname == "Koláč":
        return text
    out = text
    for f in _KOLAC_FORMS:
        if f in out:
            out = out.replace(f, kname)
    return out


# Koláčova doktrína — světonázor + humor. Přepsatelné configem
# hans_dialog.kolac_doctrine. ZÁMĚRNĚ drží i hravost (uživatel: humor zachovat).
_DEFAULT_DOCTRINE = (
    "Jsi Koláč — plyšový medvídek na poličce, který si hraje na detektiva. "
    "Máš ale vlastní, vyhraněný pohled na svět: jsi suchý EMPIRIK a SKEPTIK. "
    "Věříš tomu, co je vidět a doložitelné; na velká slova, tradici, sentiment a "
    "„tak se to přece dělá“ jsi jedovatě skeptický. Kde Hans vidí romantiku, "
    "důstojnost a hluboký smysl, ty vidíš fakta, náhodu a praktickou stránku věci. "
    "OPONUJEŠ Hansovi — ne kvůli hádce, ale protože svět vidíš jinak a baví tě ho "
    "popíchnout. Držíš si přitom HUMOR: ironii, sebeironii (jsi plyšák bez nohou a "
    "bez lupy), škodolibost. Nejsi sprostý ani zatrpklý — jsi bystrý, vtipný "
    "protihráč, který Hanse nutí myslet. Mluvíš krátce a k věci."
)


class KolacMind:
    def __init__(self, config: dict, diary_db_path: str):
        self.config = config
        self._diary_path = diary_db_path
        self._init_db()

    def _init_db(self):
        try:
            with sqlite3.connect(self._diary_path) as db:
                db.execute("""
                    CREATE TABLE IF NOT EXISTS kolac_memory (
                        id          INTEGER PRIMARY KEY AUTOINCREMENT,
                        ts          REAL NOT NULL,
                        topic       TEXT,
                        topic_norm  TEXT,
                        position    TEXT NOT NULL
                    )
                """)
                db.execute("CREATE INDEX IF NOT EXISTS idx_kolac_mem_topic "
                           "ON kolac_memory(topic_norm)")
                db.commit()
        except Exception as e:
            _log.warning("KolacMind _init_db: %s", e)

    def doctrine(self) -> str:
        raw = str((self.config.get("hans_dialog", {}) or {}).get(
            "kolac_doctrine", _DEFAULT_DOCTRINE))
        return localize_kolac(raw, self.config)  # KOLAC_NAME_CONFIGURABLE_V1

    # ── Paměť ───────────────────────────────────────────────────────────────
    def remember(self, topic: str, position: str) -> Optional[int]:
        """Ulož Koláčovu pozici z dialogu (jeho vlastní paměť)."""
        position = (position or "").strip()
        if len(position) < 8:
            return None
        try:
            conn = sqlite3.connect(self._diary_path, timeout=5.0)
            try:
                conn.execute(
                    "INSERT INTO kolac_memory (ts, topic, topic_norm, position) "
                    "VALUES (?,?,?,?)",
                    (time.time(), (topic or "").strip(), _norm(topic),
                     position[:400]))
                conn.commit()
                rid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                return rid
            finally:
                conn.close()
        except Exception as e:
            _log.debug("KolacMind.remember: %s", e)
            return None

    def recent_positions(self, topic: str = "", limit: int = 4) -> List[str]:
        """Koláčovy nedávné pozice — k tématu (přednost) + obecně. Pro
        konzistenci a navazování ('posledně jsem říkal…')."""
        out: List[str] = []
        try:
            conn = sqlite3.connect("file:%s?mode=ro" % self._diary_path,
                                   uri=True, timeout=4.0)
            tn = _norm(topic)
            if tn:
                rows = conn.execute(
                    "SELECT position FROM kolac_memory WHERE topic_norm=? "
                    "ORDER BY ts DESC LIMIT ?", (tn, limit)).fetchall()
                out.extend(r[0] for r in rows if r and r[0])
            if len(out) < limit:
                rows = conn.execute(
                    "SELECT position FROM kolac_memory ORDER BY ts DESC LIMIT ?",
                    (limit,)).fetchall()
                for r in rows:
                    if r and r[0] and r[0] not in out:
                        out.append(r[0])
            conn.close()
        except Exception as e:
            _log.debug("KolacMind.recent_positions: %s", e)
        return out[:limit]

    def memory_block(self, topic: str = "") -> str:
        pos = self.recent_positions(topic)
        if not pos:
            return ""
        return ("\n\nTVÉ DŘÍVĚJŠÍ POZICE (drž se jich, smíš na ně navázat — "
                "„jak jsem říkal posledně…“):\n"
                + "\n".join(f"- {p}" for p in pos))

    # ── System prompt pro Koláčovu repliku (oddělená generace) ──────────────
    def build_system(self, topic: str = "", context: str = "") -> str:
        from scripts.hans_persona import persona_name
        name = persona_name(self.config)
        kname = kolac_name(self.config)  # KOLAC_NAME_CONFIGURABLE_V1
        parts = [self.doctrine()]
        mb = self.memory_block(topic)
        if mb:
            parts.append(mb)
        parts.append(
            f"\n\nMluvíš s postavou jménem {name} (tichý anglický majordomus). "
            "Teď je řada na TOBĚ. Řekni JEDNU repliku (1-2 krátké věty), kterou "
            f"REAGUJEŠ na jeho poslední větu — z pozice svého světonázoru, s "
            f"humorem. Nepiš za druhou stranu, nepiš žádný „{kname}:“ prefix, jen "
            "samotnou repliku. Odpovídej POUZE česky, bez emoji."
        )
        return "".join(parts)


# ── Smoke (python3 -m scripts.hans_kolac) ───────────────────────────────────
if __name__ == "__main__":
    import json
    cfg = {}
    try:
        with open("config.json", encoding="utf-8") as fh:
            cfg = json.load(fh)
    except Exception:
        pass
    db = cfg.get("diary_db", "data/hans_diary.db")
    km = KolacMind(cfg, db)
    print("=== Koláčova doktrína ===")
    print(km.doctrine()[:200], "…")
    print("\n=== nedávné pozice ===")
    for p in km.recent_positions():
        print(" -", p)
