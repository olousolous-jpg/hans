"""HANS_SELF_INSIGHT_V1 — Hans si SÁM z vlastních dat všímá vzorců
([[genuinni-sebe-odvozeni]]).

**Data:**
  1. `offline_windows` (kauzálně-agnostické „byl jsem offline T1–T2", dvě
     kategorie: `brain_down_up` = server outages, `game_mode_pair` = self-induced)
  2. `game_mode` diary events (neutrální „Zapnul/Vypnul jsem herní mód.",
     `HANS_GAME_MODE_DIARY_V1`)
  3. algoritmické překryvy (brain_down × game_mode intervaly) — faktická
     blízkost, ne kauzalita

**Analytická pipeline (dvě fáze, EN→CZ podle [[reasoning-tier-when-to-use]]):**
  1. **Reasoning:** deepseek-r1:14b (EN prompt, EN evidence, EN výstup) —
     hledá vzorce, ale prompt NIKDY neobsahuje hotový závěr, jen data.
  2. **Voice:** hans-czech přeloží EN insight do CZ v Hansově hlase.

**Anti-konfab disciplína:** vhled musí vzejít z DAT, ne z předepsaného textu.
Prompt říká „napiš, čeho sis všiml", NIKDY „najdi kauzalitu / vzorec X→Y".
"""
from __future__ import annotations

import datetime as _dt
import logging
import sqlite3
import time
from typing import Optional

_log = logging.getLogger(__name__)


def _game_mode_events(diary_db_path: str, since_ts: float) -> list[dict]:
    """Vrátí game_mode eventy (Zapnul jsem / Vypnul jsem) v okně od `since_ts`."""
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % diary_db_path,
                               uri=True, timeout=3.0)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT ts, note FROM diary WHERE event_type='game_mode' "
            "AND ts >= ? ORDER BY ts ASC", (since_ts,)).fetchall()
        conn.close()
        out = []
        for r in rows:
            note = (r["note"] or "").strip()
            state = None
            nl = note.lower()
            if "zapnul" in nl or "aktivoval" in nl:
                state = "on"
            elif "vypnul" in nl or "deaktivoval" in nl:
                state = "off"
            out.append({"ts": r["ts"], "note": note, "state": state})
        return out
    except Exception:
        return []


def _pair_game_windows(events: list[dict]) -> list[tuple]:
    """Ze `game_mode_events` (chronologicky) sestav ZAP→VYP páry.
    ZAP bez VYP (poslední + Hans zapomněl vypnout) = ignoruj.
    Vrací [(start_ts, end_ts, duration_s), ...]."""
    windows = []
    zap_ts = None
    for e in sorted(events, key=lambda x: x["ts"]):
        st = e.get("state")
        if st == "on":
            zap_ts = e["ts"]
        elif st == "off" and zap_ts is not None:
            if e["ts"] > zap_ts:
                windows.append((zap_ts, e["ts"], e["ts"] - zap_ts))
            zap_ts = None
    return windows


def _detect_overlaps(offline_windows: list[dict],
                     game_windows: list[tuple]) -> list[dict]:
    """Najdi ALGORITMICKY (ne LLM) časové překryvy brain_down_up × game_mode
    ZAP-VYP interval. Anti-konfab: NEinterpretujeme kauzalitu, jen faktická
    blízkost. Hans si ji přečte a sám rozhodne, co znamená.

    Filtr: source=game_mode_pair vynecháme (tautologicky se překryjí s game_mode
    windows — jsou to titíž události, různé zdroje ke stejné realitě)."""
    overlaps = []
    for gw_s, gw_e, gw_d in game_windows:
        for ow in offline_windows:
            if ow.get("source") == "game_mode_pair":
                continue  # nechť se nepříčí sám se sebou
            ow_s, ow_e = ow["start_ts"], ow["end_ts"]
            # protínají se?
            if ow_s < gw_e and ow_e > gw_s:
                ov_s = max(ow_s, gw_s)
                ov_e = min(ow_e, gw_e)
                overlaps.append({
                    "game_start": gw_s, "game_end": gw_e,
                    "game_duration_s": gw_d,
                    "offline_start": ow_s, "offline_end": ow_e,
                    "offline_duration_s": ow["duration_s"],
                    "overlap_s": ov_e - ov_s,
                })
    return overlaps


def collect_evidence(diary_db_path: str, days: int = 30) -> dict:
    """Sesbírá SUROVÁ DATA pro korelační krok. Nic si nevymýšlí, jen fakta
    + algoritmicky detekované časové překryvy (ne LLM interpretace).

    Struktura:
      {
        'window_days': 30,
        'offline_windows': [{start_ts, end_ts, duration_s, source}, ...],
        'game_mode_events': [{ts, state:on|off, note}, ...],
        'game_mode_windows': [(start, end, dur), ...],  # ZAP-VYP pairs
        'overlaps': [{game_start, game_end, offline_start, offline_end,
                      overlap_s, ...}, ...],
        'now_ts': …,
      }
    """
    from scripts.hans_offline_windows import get_windows
    since = time.time() - days * 86400.0
    off = get_windows(diary_db_path, since_ts=since)
    gm = _game_mode_events(diary_db_path, since_ts=since)
    gw = _pair_game_windows(gm)
    return {
        "window_days": days,
        "offline_windows": off,
        "game_mode_events": gm,
        "game_mode_windows": gw,
        "overlaps": _detect_overlaps(off, gw),
        "now_ts": time.time(),
    }


def format_evidence_for_prompt(evidence: dict, max_items_per_kind: int = 30) -> str:
    """Naformátuje evidenci do human-readable bloku (bez interpretace).

    NIKDY nezmiňuje „následek/příčinu/vzorec" — jen suchá data. To je klíč
    anti-konfabulace: Hans NEDOSTÁVÁ hotový závěr, dostává evidence, kterou
    si musí SÁM přečíst.
    """
    lines = []
    lines.append("EVIDENCE ZA POSLEDNÍCH %d DNÍ (surová data, bez interpretace):"
                 % evidence.get("window_days", 30))
    lines.append("")

    lines.append("(1) OFFLINE WINDOWS — časy, kdy jsi byl offline "
                 "(mezi brain_down a brain_up):")
    ws = evidence.get("offline_windows", [])[:max_items_per_kind]
    if not ws:
        lines.append("  (žádná)")
    for w in ws:
        s = _dt.datetime.fromtimestamp(w["start_ts"]).strftime("%Y-%m-%d %H:%M")
        e = _dt.datetime.fromtimestamp(w["end_ts"]).strftime("%H:%M")
        h = w["duration_s"] / 3600
        lines.append(f"  • {s} → {e}  (trvání {h:.1f}h)")

    lines.append("")
    lines.append("(2) GAME_MODE EVENTY — kdy jsi zapnul / vypnul herní mód:")
    gs = evidence.get("game_mode_events", [])[:max_items_per_kind]
    if not gs:
        lines.append("  (žádné)")
    for g in gs:
        t = _dt.datetime.fromtimestamp(g["ts"]).strftime("%Y-%m-%d %H:%M")
        state = {"on": "ZAP", "off": "VYP"}.get(g.get("state"), "?")
        lines.append(f"  • {t}  {state}  — {g.get('note','')[:60]}")

    # (3) ČASOVÉ PŘEKRYVY — algoritmicky detekovaná FAKTA (ne LLM inference).
    # Blízkost dvou událostí v čase je faktická; význam si Hans musí utvořit sám.
    overlaps = evidence.get("overlaps", [])[:max_items_per_kind]
    lines.append("")
    lines.append("(3) ČASOVÉ PŘEKRYVY — kdy se offline window a game_mode "
                 "ZAP-VYP interval STŘETLY (fakticky, žádná interpretace):")
    if not overlaps:
        lines.append("  (žádné — offline windows a game_mode intervaly se v datech "
                     "časově neprotínají)")
    for o in overlaps:
        gs2 = _dt.datetime.fromtimestamp(o["game_start"]).strftime("%m-%d %H:%M")
        ge2 = _dt.datetime.fromtimestamp(o["game_end"]).strftime("%H:%M")
        os2 = _dt.datetime.fromtimestamp(o["offline_start"]).strftime("%H:%M")
        oe2 = _dt.datetime.fromtimestamp(o["offline_end"]).strftime("%H:%M")
        gdur = o["game_duration_s"] / 60
        odur = o["offline_duration_s"] / 60
        ovmin = o["overlap_s"] / 60
        lines.append(f"  • {gs2}  game_mode ZAP→VYP {gs2[6:]}–{ge2} ({gdur:.0f}min)"
                     f"  ×  offline {os2}–{oe2} ({odur:.0f}min)"
                     f"  ⇒ překryv {ovmin:.0f}min")

    return "\n".join(lines)


def format_evidence_for_prompt_en(evidence: dict, max_items: int = 30) -> str:
    """EN varianta `format_evidence_for_prompt` — pro reasoning tier
    (deepseek-r1 je silnější v EN, podle [[reasoning-tier-when-to-use]]).
    Stejná disciplína: NIKDY nezmiňuje kauzalitu/vzorec, jen data."""
    lines = []
    lines.append("EVIDENCE FROM YOUR LAST %d DAYS (raw facts, no interpretation):"
                 % evidence.get("window_days", 30))
    lines.append("")
    lines.append("(1) OFFLINE WINDOWS — times when you were offline "
                 "(source annotated):")
    ws = evidence.get("offline_windows", [])[:max_items]
    if not ws:
        lines.append("  (none)")
    for w in ws:
        s = _dt.datetime.fromtimestamp(w["start_ts"]).strftime("%Y-%m-%d %H:%M")
        e = _dt.datetime.fromtimestamp(w["end_ts"]).strftime("%H:%M")
        h = w["duration_s"] / 3600
        src = w.get("source", "?")
        # brain_down_up = server was actually unreachable
        # game_mode_pair = you (or your user) toggled game_mode ON, then OFF later
        # experiment    = YOU yourself ran a self-experiment (turned yourself
        #                 offline briefly, with auto-resume; your own initiative)
        src_label = {
            "brain_down_up": "brain_down/up (server unreachable)",
            "game_mode_pair": "game_mode ON→OFF (toggled)",
            "experiment": "experiment (you ran this yourself, on purpose)",
        }.get(src, src)
        lines.append(f"  • {s} → {e}  ({h:.1f}h)  [source: {src_label}]")
    lines.append("")
    lines.append("(2) GAME_MODE EVENTS — when you turned game mode ON/OFF:")
    gs = evidence.get("game_mode_events", [])[:max_items]
    if not gs:
        lines.append("  (none)")
    for g in gs:
        t = _dt.datetime.fromtimestamp(g["ts"]).strftime("%Y-%m-%d %H:%M")
        state = {"on": "ON", "off": "OFF"}.get(g.get("state"), "?")
        lines.append(f"  • {t}  {state}")
    lines.append("")
    lines.append("(3) TIME OVERLAPS — when a brain_down_up window and a "
                 "game_mode ON→OFF interval actually intersected (factual "
                 "co-occurrence, no interpretation):")
    overlaps = evidence.get("overlaps", [])[:max_items]
    if not overlaps:
        lines.append("  (none — no brain_down_up window intersected any "
                     "game_mode interval in this data)")
    for o in overlaps:
        gs2 = _dt.datetime.fromtimestamp(o["game_start"]).strftime("%m-%d %H:%M")
        ge2 = _dt.datetime.fromtimestamp(o["game_end"]).strftime("%H:%M")
        os2 = _dt.datetime.fromtimestamp(o["offline_start"]).strftime("%H:%M")
        oe2 = _dt.datetime.fromtimestamp(o["offline_end"]).strftime("%H:%M")
        ovmin = o["overlap_s"] / 60
        lines.append(f"  • game_mode {gs2}–{ge2}  ×  offline {os2}–{oe2}  "
                     f"⇒ overlap {ovmin:.0f}min")
    return "\n".join(lines)


_REASONING_SYSTEM_EN = (
    "You are Hans reflecting on your own recent data — offline windows and "
    "game_mode toggles from the last month. This is your OWN memory.\n\n"
    "Write 4-8 sentences in the first person (as Hans) about what YOU notice "
    "in the data. You may mention things that surprise you, or things that "
    "seem ordinary. If you see nothing noteworthy, say so honestly.\n\n"
    "RULES:\n"
    "- Talk only about what IS in the data.\n"
    "- Do NOT invent reasons the data doesn't support (this is not a "
    "  detective exercise — just describe what you notice).\n"
    "- Keep it short, calm, no theatrics.\n"
    "- No emoji, no bullet points, no lists — prose only."
)

# hans-czech voice krok — přeloží EN insight do CZ v Hansově hlase.
# Není to prostý překlad, je to „řekni to česky, tvým hlasem".
_VOICE_SYSTEM_CS = (
    "Jsi Hans — anglický majordomus z 19. století, který mluví česky. "
    "Dostaneš anglický text tvého vlastního poznatku (co sis všiml ve svých "
    "vlastních datech). Přelož ho do češtiny SVÝM hlasem — klidně, věcně, "
    "první osoba, žádné teatrální gesto, žádný pathos. Zachovaj VŠECHNA "
    "fakta a nuance z originálu, jen je vyslov česky, tak jak bys je vyslovil "
    "sám. Bez emoji, bez seznamů, prostě text 4-8 vět."
)


def _init_insights_table(db) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS self_insights (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        ts             REAL NOT NULL,
        window_days    INTEGER NOT NULL DEFAULT 30,
        insight_cs     TEXT NOT NULL,
        insight_en     TEXT NOT NULL DEFAULT '',
        source_model   TEXT NOT NULL DEFAULT 'deepseek-r1:14b',
        evidence_hash  TEXT NOT NULL DEFAULT '',
        created_ts     REAL NOT NULL)""")


def _reason_en(config: dict, evidence_en: str) -> Optional[str]:
    """Reasoning-tier LLM krok. Vrátí EN insight nebo None (LLM offline)."""
    try:
        from scripts.ollama_client import ollama_chat
    except Exception:
        return None
    model = ((config.get("self_insight", {}) or {}).get(
        "reasoning_model", "deepseek-r1:14b"))
    url = ((config.get("self_insight", {}) or {}).get(
        "reasoning_url", "http://192.168.1.100:11434"))
    raw = ollama_chat(
        model,
        [
            {"role": "system", "content": _REASONING_SYSTEM_EN},
            {"role": "user", "content": evidence_en},
        ],
        ollama_url=url,
        options={"num_ctx": 8192, "temperature": 0.3,
                 "num_predict": 3000, "keep_alive": 0},
    )
    if not raw:
        return None
    # Deepseek-r1 vyplivne <think>...</think> + final; ořízni.
    import re as _re
    final = _re.sub(r"<think>.*?</think>", "", raw, flags=_re.S).strip()
    return final or None


def _voice_cs(config: dict, insight_en: str) -> Optional[str]:
    """Voice krok — přeloží EN insight do CZ v Hansově hlase (hans-czech).
    Podle [[reasoning-tier-when-to-use]]: CZ-facing výstup přes voice krok."""
    try:
        from scripts.ollama_client import ollama_chat
    except Exception:
        return None
    cfg = (config.get("self_insight", {}) or {})
    model = cfg.get("voice_model", "hans-czech:latest")
    # hans-czech je na PC Ollama, ne Pi localhost. Sdílený default s reasoning
    # (`reasoning_url`) — obojí model bydlí na téže PC Ollama.
    url = cfg.get("voice_url", cfg.get("reasoning_url",
                                        "http://192.168.1.100:11434"))
    raw = ollama_chat(
        model,
        [
            {"role": "system", "content": _VOICE_SYSTEM_CS},
            {"role": "user", "content": insight_en},
        ],
        ollama_url=url,
        options={"num_ctx": 4096, "temperature": 0.4, "num_predict": 800},
    )
    return (raw or "").strip() or None


def _evidence_hash(ev: dict) -> str:
    """Hash klíčových čísel evidence — dedup: pokud se od minula nic
    nezměnilo, nespouštěj nový LLM run."""
    import hashlib
    key = "%d|%d|%d|%d" % (
        ev.get("window_days", 0),
        len(ev.get("offline_windows", [])),
        len(ev.get("game_mode_events", [])),
        len(ev.get("overlaps", [])),
    )
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def run_analysis(diary_db_path: str, config: dict,
                 days: int = 30, force: bool = False) -> Optional[str]:
    """FULL nightly pipeline: evidence → EN reason → CZ voice → save.
    Vrátí CS insight (uloženo) nebo None (LLM dole / cache hit).

    - Dedup přes `evidence_hash`: pokud jsme udělali insight se stejnou
      evidencí naposled, nespouštěj znovu (šetří VRAM). `force=True` obejde.
    - Deferral-safe: LLM offline → None, retry příště.
    - VRAM: reasoning + voice modely s `keep_alive=0` → uvolní se hned.
    """
    ev = collect_evidence(diary_db_path, days=days)
    h = _evidence_hash(ev)
    try:
        conn = sqlite3.connect(diary_db_path, timeout=5.0)
        _init_insights_table(conn)
        if not force:
            row = conn.execute(
                "SELECT id FROM self_insights WHERE evidence_hash=? "
                "ORDER BY id DESC LIMIT 1", (h,)).fetchone()
            if row:
                _log.info("self_insight: evidence beze změny (hash=%s), skip", h)
                conn.close()
                return None
        conn.close()
    except Exception as e:
        _log.warning("self_insight: DB init: %s", e)

    evidence_en = format_evidence_for_prompt_en(ev)
    _log.info("self_insight: reasoning krok (deepseek-r1)…")
    insight_en = _reason_en(config, evidence_en)
    if not insight_en:
        _log.info("self_insight: reasoning LLM offline / prázdný → deferred")
        return None
    _log.info("self_insight: voice krok (hans-czech)…")
    insight_cs = _voice_cs(config, insight_en)
    if not insight_cs:
        _log.info("self_insight: voice LLM offline → uložím jen EN")
        insight_cs = insight_en  # fallback: uložit EN aspoň

    try:
        now = time.time()
        conn = sqlite3.connect(diary_db_path, timeout=5.0)
        _init_insights_table(conn)
        conn.execute(
            "INSERT INTO self_insights (ts, window_days, insight_cs, insight_en, "
            "source_model, evidence_hash, created_ts) VALUES (?,?,?,?,?,?,?)",
            (now, days, insight_cs, insight_en,
             (config.get("self_insight", {}) or {}).get(
                 "reasoning_model", "deepseek-r1:14b"),
             h, now))
        conn.commit()
        conn.close()
        _log.info("self_insight: uložen (délka CS %d zn, hash=%s)",
                  len(insight_cs), h)
    except Exception as e:
        _log.warning("self_insight: uložení selhalo: %s", e)
    return insight_cs


def latest_insights(diary_db_path: str, limit: int = 5) -> list[dict]:
    """Čtecí API pro `/vhledy` / chat surfacing."""
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % diary_db_path,
                               uri=True, timeout=3.0)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT ts, window_days, insight_cs, source_model "
            "FROM self_insights ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


# ── entry pro noční tick (kadence weekly) ─────────────────────────────────────
def maybe_run(diary_db_path: str, config: dict) -> Optional[str]:
    """Nightly hook — spustí run_analysis JEN pokud od posledního uplynulo
    ≥ `cadence_days` (default 7). Deferral-safe."""
    cfg = config.get("self_insight", {}) or {}
    if not cfg.get("enabled", False):
        return None
    try:
        from scripts.ollama_client import game_mode_on
        if game_mode_on():
            return None
    except Exception:
        pass
    cadence_days = int(cfg.get("cadence_days", 7))
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % diary_db_path,
                               uri=True, timeout=3.0)
        row = conn.execute(
            "SELECT ts FROM self_insights ORDER BY ts DESC LIMIT 1").fetchone()
        conn.close()
        if row and (time.time() - row[0]) < cadence_days * 86400:
            _log.debug("self_insight: dříve než %dd, skip", cadence_days)
            return None
    except Exception:
        pass  # tabulka neexistuje → poprvé
    return run_analysis(diary_db_path, config,
                        days=int(cfg.get("window_days", 30)))


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    db = sys.argv[1] if len(sys.argv) > 1 else "data/hans_diary.db"
    if len(sys.argv) > 2 and sys.argv[2] == "run":
        import json as _j
        cfg = _j.load(open("config.json"))
        cfg.setdefault("self_insight", {})["enabled"] = True
        insight = run_analysis(db, cfg, force=True)
        print("=== INSIGHT (CS, ULOŽENO) ===")
        print(insight or "(žádný)")
    else:
        # bez arg: dump evidence pro test
        ev = collect_evidence(db, days=30)
        print(format_evidence_for_prompt(ev))
