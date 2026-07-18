"""HANS_SELF_INSIGHT_V1 — Hans si SÁM z vlastních dat všímá vzorců
([[genuinni-sebe-odvozeni]]).

**LENS (perspektivy pohledu na sebe):**
  - `offline_game`: offline_windows × game_mode × brain_down/up × experimenty
    (výchozí, dnes vyhotoven)
  - `autonomy`: game_mode × commitments (slíbil vs splnil) × schedule stale
    (18.7.: „jak spolehlivý jsem?")

**Analytická pipeline (dvě fáze, EN→CZ podle [[reasoning-tier-when-to-use]]):**
  1. **Reasoning:** deepseek-r1:14b (EN prompt, EN evidence, EN výstup) —
     hledá vzorce, ale prompt NIKDY neobsahuje hotový závěr, jen data.
  2. **Voice:** hans-czech přeloží EN insight do CZ v Hansově hlase.

**Anti-konfab disciplína:** vhled musí vzejít z DAT, ne z předepsaného textu.
Prompt říká „napiš, čeho sis všiml", NIKDY „najdi kauzalitu / vzorec X→Y".

**Rotace:** `maybe_run` střídá lens (offline_game → autonomy → …) → Hans
dostává perspektivu, kterou dlouho neviděl. Kadence per-lens: 7 dní default.
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


# ── LENS: autonomy — game_mode × commitments × schedule stale ────────────────

def _collect_evidence_autonomy(diary_db_path: str, days: int = 30) -> dict:
    """AUTONOMY lens: „jak spolehlivý jsem" — game_mode aktivita, sliby (dané ×
    splněné × neplněné), rutiny které zaostávají. Kauzálně-agnostické.

    Struktura:
      {
        'lens': 'autonomy',
        'window_days': 30,
        'game_mode_windows': [(start_ts, end_ts, dur_s), ...],
        'commitments': [{status, text, person, created_ts, done_ts}, ...],
        'commitment_stats': {open, done, dropped, total},
        'stale_routines': [{name, late_s, expected_gap_s, last_skip_reason}, ...],
        'now_ts': …,
      }
    """
    since = time.time() - days * 86400.0
    out = {"lens": "autonomy", "window_days": days,
           "game_mode_windows": [], "commitments": [],
           "commitment_stats": {"open": 0, "done": 0, "dropped": 0, "total": 0},
           "stale_routines": [], "now_ts": time.time()}
    # game_mode windows (reuse existing pairer)
    gm = _game_mode_events(diary_db_path, since_ts=since)
    out["game_mode_windows"] = _pair_game_windows(gm)
    # commitments (celá tabulka, ne jen since — sliby mohou být staré)
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % diary_db_path, uri=True, timeout=3.0)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, status, text, person, created_ts, done_ts, due_text "
            "FROM commitments ORDER BY created_ts DESC LIMIT 40").fetchall()
        conn.close()
        for r in rows:
            out["commitments"].append(dict(r))
            st = r["status"]
            out["commitment_stats"][st] = out["commitment_stats"].get(st, 0) + 1
            out["commitment_stats"]["total"] += 1
    except Exception:
        pass
    # stale routines from hans_schedule (behaviorální kalendář)
    try:
        from scripts.hans_schedule import ScheduleStore
        st = ScheduleStore(diary_db_path)
        out["stale_routines"] = st.stale_list()
    except Exception:
        pass
    return out


def _format_evidence_autonomy_en(evidence: dict, max_items: int = 20) -> str:
    """EN format autonomy lens — bez interpretace, jen fakta."""
    lines = []
    lines.append("EVIDENCE FROM YOUR LAST %d DAYS — AUTONOMY LENS "
                 "(raw facts, no interpretation):"
                 % evidence.get("window_days", 30))
    lines.append("")
    # (1) game_mode
    gws = evidence.get("game_mode_windows", [])[:max_items]
    lines.append("(1) GAME_MODE ON→OFF INTERVALS — when you switched game "
                 "mode ON, later OFF:")
    if not gws:
        lines.append("  (none)")
    for gs, ge, gd in gws:
        s = _dt.datetime.fromtimestamp(gs).strftime("%Y-%m-%d %H:%M")
        e = _dt.datetime.fromtimestamp(ge).strftime("%H:%M")
        m = gd / 60
        lines.append(f"  • {s}–{e} ({m:.0f}min)")
    # (2) commitments
    stats = evidence.get("commitment_stats", {})
    lines.append("")
    lines.append("(2) COMMITMENTS — promises you made:")
    lines.append(f"  Totals: total={stats.get('total',0)}  "
                 f"open={stats.get('open',0)}  "
                 f"done={stats.get('done',0)}  "
                 f"dropped={stats.get('dropped',0)}")
    commits = evidence.get("commitments", [])[:max_items]
    for c in commits:
        ct = _dt.datetime.fromtimestamp(c["created_ts"]).strftime("%m-%d")
        due = (f" [due: {c['due_text']}]" if c.get("due_text") else "")
        txt = (c.get("text") or "")[:80]
        lines.append("  • [%s] %s  '%s'%s  (to %s)"
                     % (c['status'], ct, txt, due, c['person']))
    # (3) stale routines
    stale = evidence.get("stale_routines", [])[:max_items]
    lines.append("")
    lines.append("(3) STALE ROUTINES — autonomous routines that haven't run "
                 "within their expected gap:")
    if not stale:
        lines.append("  (none — all routines are running on schedule)")
    for s in stale:
        late_h = s["late_s"] / 3600
        gap_h = s["expected_gap_s"] / 3600
        reason = f" [last skip: {s['last_skip_reason']}]" if s.get("last_skip_reason") else ""
        lines.append(f"  • {s['name']}: {late_h:.1f}h overdue "
                     f"(max gap {gap_h:.1f}h){reason}")
    return "\n".join(lines)


# ── LENS registry ────────────────────────────────────────────────────────────
# Každý lens = (collect_evidence_fn, format_evidence_en_fn).
# Přidávat sem další (social/learning/creative/…) v budoucnu.
LENSES = {
    "offline_game": (collect_evidence, format_evidence_for_prompt_en),
    "autonomy":     (_collect_evidence_autonomy, _format_evidence_autonomy_en),
}
DEFAULT_LENS = "offline_game"


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
    # Idempotentní migrace — přidej lens_id pokud chybí (starší DB bez sloupce).
    try:
        db.execute("ALTER TABLE self_insights ADD COLUMN "
                   "lens_id TEXT NOT NULL DEFAULT 'offline_game'")
    except Exception:
        pass  # sloupec už existuje


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


def _evidence_hash(ev: dict, lens_id: str = "offline_game") -> str:
    """Hash klíčových čísel evidence + lens — dedup per lens."""
    import hashlib
    key = "%s|%d|%d|%d|%d|%d" % (
        lens_id,
        ev.get("window_days", 0),
        len(ev.get("offline_windows", []) or ev.get("game_mode_windows", [])),
        len(ev.get("game_mode_events", []) or ev.get("commitments", [])),
        len(ev.get("overlaps", []) or ev.get("stale_routines", [])),
        (ev.get("commitment_stats", {}) or {}).get("total", 0),
    )
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def run_analysis(diary_db_path: str, config: dict,
                 lens_id: str = DEFAULT_LENS,
                 days: int = 30, force: bool = False) -> Optional[str]:
    """FULL nightly pipeline pro daný LENS: evidence → EN reason → CZ voice → save.
    Vrátí CS insight (uloženo) nebo None (LLM dole / cache hit / unknown lens).

    - `lens_id` z `LENSES` (default 'offline_game'). Neznámý lens → None.
    - Dedup per (lens, evidence_hash): stejné data + stejný lens = cache hit.
    - Deferral-safe: LLM offline → None, retry příště.
    - VRAM: `keep_alive=0` → modely se uvolní hned.
    """
    if lens_id not in LENSES:
        _log.warning("self_insight: unknown lens %r", lens_id)
        return None
    collect_fn, format_fn = LENSES[lens_id]
    ev = collect_fn(diary_db_path, days=days)
    h = _evidence_hash(ev, lens_id)
    try:
        conn = sqlite3.connect(diary_db_path, timeout=5.0)
        _init_insights_table(conn)
        if not force:
            row = conn.execute(
                "SELECT id FROM self_insights WHERE evidence_hash=? AND lens_id=? "
                "ORDER BY id DESC LIMIT 1", (h, lens_id)).fetchone()
            if row:
                _log.info("self_insight[%s]: evidence beze změny (hash=%s), skip",
                          lens_id, h)
                conn.close()
                return None
        conn.close()
    except Exception as e:
        _log.warning("self_insight: DB init: %s", e)

    evidence_en = format_fn(ev)
    _log.info("self_insight[%s]: reasoning krok (deepseek-r1)…", lens_id)
    insight_en = _reason_en(config, evidence_en)
    if not insight_en:
        _log.info("self_insight[%s]: reasoning LLM offline / prázdný → deferred",
                  lens_id)
        return None
    _log.info("self_insight[%s]: voice krok (hans-czech)…", lens_id)
    insight_cs = _voice_cs(config, insight_en)
    if not insight_cs:
        _log.info("self_insight[%s]: voice LLM offline → uložím jen EN", lens_id)
        insight_cs = insight_en

    try:
        now = time.time()
        conn = sqlite3.connect(diary_db_path, timeout=5.0)
        _init_insights_table(conn)
        conn.execute(
            "INSERT INTO self_insights (ts, window_days, insight_cs, insight_en, "
            "source_model, evidence_hash, created_ts, lens_id) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (now, days, insight_cs, insight_en,
             (config.get("self_insight", {}) or {}).get(
                 "reasoning_model", "deepseek-r1:14b"),
             h, now, lens_id))
        conn.commit()
        conn.close()
        _log.info("self_insight[%s]: uložen (délka CS %d zn, hash=%s)",
                  lens_id, len(insight_cs), h)
    except Exception as e:
        _log.warning("self_insight: uložení selhalo: %s", e)
    return insight_cs


def _next_lens(diary_db_path: str, config: dict) -> str:
    """Rotace: vezme lens, který nejdéle neběžel (nejstarší `ts` v self_insights
    per lens). Když nikdy → první lens z configu `self_insight.lenses`."""
    lenses = (config.get("self_insight", {}) or {}).get(
        "lenses", list(LENSES.keys()))
    lenses = [l for l in lenses if l in LENSES]
    if not lenses:
        return DEFAULT_LENS
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % diary_db_path,
                               uri=True, timeout=3.0)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT lens_id, MAX(ts) as last_ts FROM self_insights "
            "GROUP BY lens_id").fetchall()
        conn.close()
        last = {r["lens_id"]: r["last_ts"] for r in rows}
    except Exception:
        last = {}
    # Lens co nikdy neproběhl → prioritně
    for l in lenses:
        if l not in last:
            return l
    # Jinak vezmi ten s nejstarším last_ts
    return min(lenses, key=lambda l: last.get(l, 0))


def latest_insights(diary_db_path: str, limit: int = 5,
                    lens_id: Optional[str] = None) -> list[dict]:
    """Čtecí API pro `/vhledy` / chat surfacing. `lens_id=None` = všechny."""
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % diary_db_path,
                               uri=True, timeout=3.0)
        conn.row_factory = sqlite3.Row
        if lens_id:
            rows = conn.execute(
                "SELECT ts, window_days, insight_cs, source_model, lens_id "
                "FROM self_insights WHERE lens_id=? ORDER BY ts DESC LIMIT ?",
                (lens_id, limit)).fetchall()
        else:
            rows = conn.execute(
                "SELECT ts, window_days, insight_cs, source_model, lens_id "
                "FROM self_insights ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


# ── entry pro noční tick (kadence per-lens, s rotací) ────────────────────────
def maybe_run(diary_db_path: str, config: dict) -> Optional[str]:
    """Nightly hook — vybere DALŠÍ lens v rotaci (podle _next_lens) a spustí
    JEN pokud od posledního běhu TOHO lens uplynulo ≥ `cadence_days` (default 7).
    Deferral-safe."""
    cfg = config.get("self_insight", {}) or {}
    if not cfg.get("enabled", False):
        return None
    try:
        from scripts.ollama_client import game_mode_on
        if game_mode_on():
            return None
    except Exception:
        pass
    lens_id = _next_lens(diary_db_path, config)
    cadence_days = int(cfg.get("cadence_days", 7))
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % diary_db_path,
                               uri=True, timeout=3.0)
        row = conn.execute(
            "SELECT ts FROM self_insights WHERE lens_id=? ORDER BY ts DESC LIMIT 1",
            (lens_id,)).fetchone()
        conn.close()
        if row and (time.time() - row[0]) < cadence_days * 86400:
            _log.debug("self_insight[%s]: dříve než %dd, skip", lens_id, cadence_days)
            return None
    except Exception:
        pass  # tabulka neexistuje → poprvé
    return run_analysis(diary_db_path, config, lens_id=lens_id,
                        days=int(cfg.get("window_days", 30)))


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    db = sys.argv[1] if len(sys.argv) > 1 else "data/hans_diary.db"
    if len(sys.argv) > 2 and sys.argv[2] == "run":
        lens = sys.argv[3] if len(sys.argv) > 3 else DEFAULT_LENS
        import json as _j
        cfg = _j.load(open("config.json"))
        cfg.setdefault("self_insight", {})["enabled"] = True
        insight = run_analysis(db, cfg, lens_id=lens, force=True)
        print(f"=== INSIGHT [{lens}] (CS, ULOŽENO) ===")
        print(insight or "(žádný)")
    elif len(sys.argv) > 2 and sys.argv[2] == "dump":
        lens = sys.argv[3] if len(sys.argv) > 3 else DEFAULT_LENS
        collect_fn, format_fn = LENSES[lens]
        ev = collect_fn(db, days=30)
        print(format_fn(ev))
    else:
        ev = collect_evidence(db, days=30)
        print(format_evidence_for_prompt(ev))
