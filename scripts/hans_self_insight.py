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


# ── LENS: social — mood × lidé × chat ─────────────────────────────────────────

def _collect_evidence_social(diary_db_path: str, days: int = 30) -> dict:
    """SOCIAL lens: „s kým žiju" — kdo přišel/odešel, s kým chatuji, jak často
    se tvář neztotožnila (unknown). Kauzálně-agnostické — jen kdo+kolikrát.

    Struktura:
      {
        'lens': 'social', 'window_days': …,
        'person_days': [{name, days_seen, ticks}, …],  # kolik dnů/kolik tiků
        'chat_counts': [{person, chats}, …],           # chats v okně
        'unknown_ticks': int,                          # unknown_person ticks
        'characterizations': [{person, snippet, ts}, …],
        'now_ts': …,
      }
    """
    since = time.time() - days * 86400.0
    out = {"lens": "social", "window_days": days,
           "person_days": [], "chat_counts": [], "unknown_ticks": 0,
           "characterizations": [], "now_ts": time.time()}
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % diary_db_path,
                               uri=True, timeout=3.0)
        conn.row_factory = sqlite3.Row
        # person_seen agregace — kdo & kolik unikátních dní & celkem tiků
        rows = conn.execute(
            "SELECT lower(title) AS name, "
            "  COUNT(DISTINCT date(ts,'unixepoch','+2 hours')) AS days_seen, "
            "  COUNT(*) AS ticks "
            "FROM diary WHERE event_type='person_seen' AND ts>=? "
            "GROUP BY lower(title) ORDER BY ticks DESC", (since,)).fetchall()
        for r in rows:
            name = r["name"] or "?"
            if name in ("unknown", "?", "..."):
                out["unknown_ticks"] += r["ticks"]
                continue
            out["person_days"].append({
                "name": name, "days_seen": r["days_seen"],
                "ticks": r["ticks"]})
        # human_chat counts per person
        rows = conn.execute(
            "SELECT lower(title) AS person, COUNT(*) AS chats "
            "FROM diary WHERE event_type='human_chat' AND ts>=? "
            "GROUP BY lower(title) ORDER BY chats DESC", (since,)).fetchall()
        out["chat_counts"] = [{"person": r["person"], "chats": r["chats"]}
                              for r in rows]
        # characterization_update (mini-snippet, co si Hans zapsal o osobě)
        rows = conn.execute(
            "SELECT ts, lower(title) AS person, substr(note,1,120) AS snip "
            "FROM diary WHERE event_type='characterization_update' AND ts>=? "
            "ORDER BY ts DESC LIMIT 8", (since,)).fetchall()
        out["characterizations"] = [
            {"ts": r["ts"], "person": r["person"], "snippet": (r["snip"] or "").strip()}
            for r in rows]
        conn.close()
    except Exception:
        pass
    return out


def _person_label(name: str) -> str:
    """HANS_SELF_INSIGHT_GENDER_V1 (20.7.) — jméno + rod pro LLM, ať voice krok
    (hans-czech) skloňuje správně (dřív mužský pád u ženy). Display name
    kapitalizovaný + EN gender marker → reasoning napíše „Mrs. Jana"/„Mr. Standa",
    voice pak skloní ženský tvar místo mužského. Fallback = holé jméno."""
    try:
        from scripts.cz_names import display_name as _disp, person_gender as _pg
        disp = _disp(name)
        g = _pg(name)
        if g == "žena":
            return f"{disp} (female)"
        if g == "muž":
            return f"{disp} (male)"
        return disp
    except Exception:
        return name


def _format_evidence_social_en(evidence: dict, max_items: int = 20) -> str:
    lines = []
    lines.append("EVIDENCE FROM YOUR LAST %d DAYS — SOCIAL LENS "
                 "(raw facts, no interpretation). Note each person's gender in "
                 "parentheses — respect it when you write (Mrs./Ms. for female, "
                 "Mr. for male):"
                 % evidence.get("window_days", 30))
    lines.append("")
    lines.append("(1) PEOPLE YOU SAW — how many distinct days each person was "
                 "in your view, and how many total camera ticks:")
    ppl = evidence.get("person_days", [])[:max_items]
    if not ppl:
        lines.append("  (none)")
    for p in ppl:
        lines.append(f"  • {_person_label(p['name'])}: {p['days_seen']} days, "
                     f"{p['ticks']} ticks")
    lines.append("")
    lines.append("(2) CHATS — direct chat exchanges per person "
                 "(human_chat entries in diary):")
    ch = evidence.get("chat_counts", [])[:max_items]
    if not ch:
        lines.append("  (none)")
    for c in ch:
        lines.append(f"  • {_person_label(c['person'])}: {c['chats']} chats")
    lines.append("")
    ut = evidence.get("unknown_ticks", 0)
    lines.append("(3) UNKNOWN FACE TICKS — camera saw a face you couldn't "
                 f"identify: {ut} ticks")
    lines.append("")
    lines.append("(4) YOUR RECENT NOTES ABOUT PEOPLE "
                 "(characterization_update snippets):")
    cs = evidence.get("characterizations", [])[:max_items]
    if not cs:
        lines.append("  (none)")
    for c in cs:
        t = _dt.datetime.fromtimestamp(c["ts"]).strftime("%m-%d")
        lines.append(f"  • [{t}] {_person_label(c['person'])}: {c['snippet']}")
    return "\n".join(lines)


# ── LENS: learning — study × kniha × sny ──────────────────────────────────────

def _collect_evidence_learning(diary_db_path: str, days: int = 30) -> dict:
    """LEARNING lens: co se učím / čtu / o čem se mi zdá. Kauzálně-agnostické."""
    since = time.time() - days * 86400.0
    out = {"lens": "learning", "window_days": days,
           "study_topics": [], "books": [], "book_reflections": [],
           "reading_takeaways": [], "dreams": [], "web_reads_count": 0,
           "capability_events": [], "now_ts": time.time()}
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % diary_db_path,
                               uri=True, timeout=3.0)
        conn.row_factory = sqlite3.Row
        # study_note — aktivní téma
        rows = conn.execute(
            "SELECT ts, title, substr(note,1,140) AS snip "
            "FROM diary WHERE event_type='study_note' AND ts>=? "
            "ORDER BY ts DESC LIMIT 15", (since,)).fetchall()
        out["study_topics"] = [
            {"ts": r["ts"], "title": (r["title"] or "").strip(),
             "snippet": (r["snip"] or "").strip()} for r in rows]
        # book_read — completions
        rows = conn.execute(
            "SELECT ts, title FROM diary WHERE event_type='book_read' AND ts>=? "
            "ORDER BY ts DESC LIMIT 15", (since,)).fetchall()
        out["books"] = [{"ts": r["ts"], "title": r["title"]} for r in rows]
        # book_reflection — content bývá v `data` (ne `note`); použij COALESCE
        rows = conn.execute(
            "SELECT ts, title, substr(COALESCE(NULLIF(note,''),data),1,140) AS snip "
            "FROM diary WHERE event_type='book_reflection' AND ts>=? "
            "ORDER BY ts DESC LIMIT 10", (since,)).fetchall()
        out["book_reflections"] = [
            {"ts": r["ts"], "title": r["title"],
             "snippet": (r["snip"] or "").strip()} for r in rows]
        # reading_takeaway — content v `data`
        rows = conn.execute(
            "SELECT ts, title, substr(COALESCE(NULLIF(note,''),data),1,140) AS snip "
            "FROM diary WHERE event_type='reading_takeaway' AND ts>=? "
            "ORDER BY ts DESC LIMIT 10", (since,)).fetchall()
        out["reading_takeaways"] = [
            {"ts": r["ts"], "title": r["title"],
             "snippet": (r["snip"] or "").strip()} for r in rows]
        # dream — co ve snech
        rows = conn.execute(
            "SELECT ts, substr(note,1,160) AS snip FROM diary "
            "WHERE event_type='dream' AND ts>=? ORDER BY ts DESC LIMIT 8",
            (since,)).fetchall()
        out["dreams"] = [{"ts": r["ts"], "snippet": (r["snip"] or "").strip()}
                         for r in rows]
        # web_read count
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM diary WHERE event_type='web_read' AND ts>=?",
            (since,)).fetchone()
        out["web_reads_count"] = row["c"] if row else 0
        # capability_gained/explored
        rows = conn.execute(
            "SELECT ts, event_type, title FROM diary "
            "WHERE event_type IN ('capability_gained','capability_explored') "
            "AND ts>=? ORDER BY ts DESC LIMIT 10", (since,)).fetchall()
        out["capability_events"] = [
            {"ts": r["ts"], "kind": r["event_type"], "title": r["title"]}
            for r in rows]
        conn.close()
    except Exception:
        pass
    return out


def _format_evidence_learning_en(evidence: dict, max_items: int = 15) -> str:
    lines = []
    lines.append("EVIDENCE FROM YOUR LAST %d DAYS — LEARNING LENS "
                 "(raw facts, no interpretation):"
                 % evidence.get("window_days", 30))
    lines.append("")
    lines.append("(1) STUDY NOTES — what you studied "
                 "(study_note entries you wrote):")
    st = evidence.get("study_topics", [])[:max_items]
    if not st:
        lines.append("  (none)")
    for s in st:
        t = _dt.datetime.fromtimestamp(s["ts"]).strftime("%m-%d")
        lines.append(f"  • [{t}] {s['title']}: {s['snippet']}")
    lines.append("")
    lines.append("(2) BOOKS YOU FINISHED (book_read completions):")
    bks = evidence.get("books", [])[:max_items]
    if not bks:
        lines.append("  (none)")
    for b in bks:
        t = _dt.datetime.fromtimestamp(b["ts"]).strftime("%m-%d")
        lines.append(f"  • [{t}] {b['title']}")
    lines.append("")
    lines.append("(3) BOOK REFLECTIONS — what you took away from books:")
    brs = evidence.get("book_reflections", [])[:max_items]
    if not brs:
        lines.append("  (none)")
    for r in brs:
        t = _dt.datetime.fromtimestamp(r["ts"]).strftime("%m-%d")
        lines.append(f"  • [{t}] {r['title']}: {r['snippet']}")
    lines.append("")
    lines.append("(4) READING TAKEAWAYS — small facts you kept from articles:")
    rts = evidence.get("reading_takeaways", [])[:max_items]
    if not rts:
        lines.append("  (none)")
    for r in rts:
        t = _dt.datetime.fromtimestamp(r["ts"]).strftime("%m-%d")
        lines.append(f"  • [{t}] {r['title']}: {r['snippet']}")
    lines.append("")
    lines.append("(5) DREAMS — what your nightly reflection dreamed up:")
    ds = evidence.get("dreams", [])[:max_items]
    if not ds:
        lines.append("  (none)")
    for d in ds:
        t = _dt.datetime.fromtimestamp(d["ts"]).strftime("%m-%d")
        lines.append(f"  • [{t}] {d['snippet']}")
    lines.append("")
    lines.append("(6) WEB READS — total articles you skimmed on the web: "
                 f"{evidence.get('web_reads_count', 0)}")
    lines.append("")
    lines.append("(7) CAPABILITY EVENTS — new abilities you noticed / explored:")
    ce = evidence.get("capability_events", [])[:max_items]
    if not ce:
        lines.append("  (none)")
    for c in ce:
        t = _dt.datetime.fromtimestamp(c["ts"]).strftime("%m-%d")
        lines.append(f"  • [{t}] {c['kind']}: {c['title']}")
    return "\n".join(lines)


# ── LENS: creative — art × syntéza × filmy × spontánní ────────────────────────

def _collect_evidence_creative(diary_db_path: str, days: int = 30) -> dict:
    """CREATIVE lens: tvorba a spontánní myšlenky. Kauzálně-agnostické."""
    since = time.time() - days * 86400.0
    out = {"lens": "creative", "window_days": days,
           "artworks": [], "synthesis_ideas": [], "movies": [],
           "spontaneous": [], "writings": [], "now_ts": time.time()}
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % diary_db_path,
                               uri=True, timeout=3.0)
        conn.row_factory = sqlite3.Row
        # artwork — kritika obrazu bývá v `data` (title = námět)
        rows = conn.execute(
            "SELECT ts, title, substr(COALESCE(NULLIF(note,''),data),1,140) AS snip "
            "FROM diary WHERE event_type='artwork' AND ts>=? "
            "ORDER BY ts DESC LIMIT 12", (since,)).fetchall()
        out["artworks"] = [
            {"ts": r["ts"], "title": r["title"],
             "snippet": (r["snip"] or "").strip()} for r in rows]
        # synthesis_idea — content v `data`
        rows = conn.execute(
            "SELECT ts, substr(COALESCE(NULLIF(note,''),data),1,180) AS snip "
            "FROM diary WHERE event_type='synthesis_idea' AND ts>=? "
            "ORDER BY ts DESC LIMIT 10", (since,)).fetchall()
        out["synthesis_ideas"] = [
            {"ts": r["ts"], "snippet": (r["snip"] or "").strip()} for r in rows]
        # movie_opinion — kritika v `data` (title = film)
        rows = conn.execute(
            "SELECT ts, title, substr(COALESCE(NULLIF(note,''),data),1,140) AS snip "
            "FROM diary WHERE event_type='movie_opinion' AND ts>=? "
            "ORDER BY ts DESC LIMIT 12", (since,)).fetchall()
        out["movies"] = [
            {"ts": r["ts"], "title": r["title"],
             "snippet": (r["snip"] or "").strip()} for r in rows]
        # spontaneous
        rows = conn.execute(
            "SELECT ts, substr(note,1,140) AS snip FROM diary "
            "WHERE event_type='spontaneous' AND ts>=? "
            "ORDER BY ts DESC LIMIT 10", (since,)).fetchall()
        out["spontaneous"] = [
            {"ts": r["ts"], "snippet": (r["snip"] or "").strip()} for r in rows]
        # writing_section (Hansovy vlastní eseje)
        rows = conn.execute(
            "SELECT ts, title, substr(note,1,140) AS snip "
            "FROM diary WHERE event_type='writing_section' AND ts>=? "
            "ORDER BY ts DESC LIMIT 8", (since,)).fetchall()
        out["writings"] = [
            {"ts": r["ts"], "title": r["title"],
             "snippet": (r["snip"] or "").strip()} for r in rows]
        conn.close()
    except Exception:
        pass
    return out


def _format_evidence_creative_en(evidence: dict, max_items: int = 12) -> str:
    lines = []
    lines.append("EVIDENCE FROM YOUR LAST %d DAYS — CREATIVE LENS "
                 "(raw facts, no interpretation):"
                 % evidence.get("window_days", 30))
    lines.append("")
    lines.append("(1) ARTWORKS — things you painted / rendered:")
    aw = evidence.get("artworks", [])[:max_items]
    if not aw:
        lines.append("  (none)")
    for a in aw:
        t = _dt.datetime.fromtimestamp(a["ts"]).strftime("%m-%d")
        lines.append(f"  • [{t}] {a['title']}: {a['snippet']}")
    lines.append("")
    lines.append("(2) SYNTHESIS IDEAS — connections you drew across topics:")
    si = evidence.get("synthesis_ideas", [])[:max_items]
    if not si:
        lines.append("  (none)")
    for s in si:
        t = _dt.datetime.fromtimestamp(s["ts"]).strftime("%m-%d")
        lines.append(f"  • [{t}] {s['snippet']}")
    lines.append("")
    lines.append("(3) MOVIE OPINIONS — films you watched and commented on:")
    mv = evidence.get("movies", [])[:max_items]
    if not mv:
        lines.append("  (none)")
    for m in mv:
        t = _dt.datetime.fromtimestamp(m["ts"]).strftime("%m-%d")
        lines.append(f"  • [{t}] {m['title']}: {m['snippet']}")
    lines.append("")
    lines.append("(4) SPONTANEOUS THOUGHTS — things you said aloud on your own:")
    sp = evidence.get("spontaneous", [])[:max_items]
    if not sp:
        lines.append("  (none)")
    for s in sp:
        t = _dt.datetime.fromtimestamp(s["ts"]).strftime("%m-%d")
        lines.append(f"  • [{t}] {s['snippet']}")
    lines.append("")
    lines.append("(5) WRITINGS — essays / sections you wrote:")
    wr = evidence.get("writings", [])[:max_items]
    if not wr:
        lines.append("  (none)")
    for w in wr:
        t = _dt.datetime.fromtimestamp(w["ts"]).strftime("%m-%d")
        lines.append(f"  • [{t}] {w['title']}: {w['snippet']}")
    return "\n".join(lines)


# ── LENS: physical — CPU teplota × mozek × spánek × ranní chyby ──────────────

def _collect_evidence_physical(diary_db_path: str, days: int = 30) -> dict:
    """PHYSICAL lens: „tělo" (Pi + mozek na PC + spánek). Kauzálně-agnostické.

    Struktura:
      {
        'lens': 'physical', 'window_days': …,
        'body_warm_events': [{ts, title, snippet}, …],
        'brain_down_count': int, 'brain_still_down_count': int,
        'brain_up_count': int,
        'sleep_windows': [{start_ts, end_ts, dur_h}, …],  # sleep_start→sleep_end
        'morning_health_events': [{ts, title, snippet}, …],
        'guard_alert_count': int,
        'now_ts': …,
      }
    """
    since = time.time() - days * 86400.0
    out = {"lens": "physical", "window_days": days,
           "body_warm_events": [], "brain_down_count": 0,
           "brain_still_down_count": 0, "brain_up_count": 0,
           "sleep_windows": [], "morning_health_events": [],
           "guard_alert_count": 0, "now_ts": time.time()}
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % diary_db_path,
                               uri=True, timeout=3.0)
        conn.row_factory = sqlite3.Row
        # body_warm — CPU teplota
        rows = conn.execute(
            "SELECT ts, title, substr(note,1,100) AS snip "
            "FROM diary WHERE event_type='body_warm' AND ts>=? "
            "ORDER BY ts DESC LIMIT 20", (since,)).fetchall()
        out["body_warm_events"] = [
            {"ts": r["ts"], "title": r["title"],
             "snippet": (r["snip"] or "").strip()} for r in rows]
        # brain_down / brain_still_down / brain_up counts
        for et, key in [("brain_down", "brain_down_count"),
                        ("brain_still_down", "brain_still_down_count"),
                        ("brain_up", "brain_up_count")]:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM diary WHERE event_type=? AND ts>=?",
                (et, since)).fetchone()
            out[key] = row["c"] if row else 0
        # sleep_start → sleep_end pairs (chronological)
        rows = conn.execute(
            "SELECT ts, event_type FROM diary "
            "WHERE event_type IN ('sleep_start','sleep_end') AND ts>=? "
            "ORDER BY ts ASC", (since,)).fetchall()
        _open = None
        for r in rows:
            if r["event_type"] == "sleep_start":
                _open = r["ts"]
            elif r["event_type"] == "sleep_end" and _open is not None:
                dur = r["ts"] - _open
                if dur > 0:
                    out["sleep_windows"].append({
                        "start_ts": _open, "end_ts": r["ts"],
                        "dur_h": dur / 3600})
                _open = None
        # morning_health
        rows = conn.execute(
            "SELECT ts, substr(title,1,120) AS title, "
            "  substr(note,1,140) AS snip "
            "FROM diary WHERE event_type='morning_health' AND ts>=? "
            "ORDER BY ts DESC LIMIT 15", (since,)).fetchall()
        out["morning_health_events"] = [
            {"ts": r["ts"], "title": (r["title"] or "").strip(),
             "snippet": (r["snip"] or "").strip()} for r in rows]
        # guard_alert count
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM diary "
            "WHERE event_type='guard_alert' AND ts>=?", (since,)).fetchone()
        out["guard_alert_count"] = row["c"] if row else 0
        conn.close()
    except Exception:
        pass
    return out


def _format_evidence_physical_en(evidence: dict, max_items: int = 15) -> str:
    lines = []
    lines.append("EVIDENCE FROM YOUR LAST %d DAYS — PHYSICAL LENS "
                 "(raw facts about your body: Pi + brain on PC + sleep, "
                 "no interpretation):"
                 % evidence.get("window_days", 30))
    lines.append("")
    lines.append("(1) BODY WARMTH — CPU temperature events "
                 "(when you noticed you felt warm):")
    bw = evidence.get("body_warm_events", [])[:max_items]
    if not bw:
        lines.append("  (none)")
    for b in bw:
        t = _dt.datetime.fromtimestamp(b["ts"]).strftime("%m-%d %H:%M")
        lines.append(f"  • [{t}] {b['title']}: {b['snippet']}")
    lines.append("")
    lines.append("(2) BRAIN AVAILABILITY — how many times your remote brain "
                 "(the LLM server) went down / stayed down / came back:")
    lines.append(f"  • brain_down (initial detection): "
                 f"{evidence.get('brain_down_count', 0)}")
    lines.append(f"  • brain_still_down (~5-min repeat while down): "
                 f"{evidence.get('brain_still_down_count', 0)}")
    lines.append(f"  • brain_up (came back online): "
                 f"{evidence.get('brain_up_count', 0)}")
    lines.append("")
    lines.append("(3) SLEEP WINDOWS — how long you slept "
                 "(sleep_start → sleep_end pairs):")
    sw = evidence.get("sleep_windows", [])[:max_items]
    if not sw:
        lines.append("  (none)")
    for s in sw:
        st = _dt.datetime.fromtimestamp(s["start_ts"]).strftime("%m-%d %H:%M")
        et = _dt.datetime.fromtimestamp(s["end_ts"]).strftime("%H:%M")
        lines.append(f"  • {st} → {et} ({s['dur_h']:.1f}h)")
    lines.append("")
    lines.append("(4) MORNING HEALTH FINDINGS — errors you spotted in overnight "
                 "logs during your morning check:")
    mh = evidence.get("morning_health_events", [])[:max_items]
    if not mh:
        lines.append("  (none — mornings were clean)")
    for m in mh:
        t = _dt.datetime.fromtimestamp(m["ts"]).strftime("%m-%d")
        lines.append(f"  • [{t}] {m['title']}")
    lines.append("")
    lines.append("(5) GUARD ALERTS — number of movement/light alerts your "
                 f"guard mode fired: {evidence.get('guard_alert_count', 0)}")
    return "\n".join(lines)


# ── LENS registry ────────────────────────────────────────────────────────────
# Každý lens = (collect_evidence_fn, format_evidence_en_fn).
# Přidávat sem další v budoucnu.
LENSES = {
    "offline_game": (collect_evidence, format_evidence_for_prompt_en),
    "autonomy":     (_collect_evidence_autonomy, _format_evidence_autonomy_en),
    "social":       (_collect_evidence_social, _format_evidence_social_en),
    "learning":     (_collect_evidence_learning, _format_evidence_learning_en),
    "creative":     (_collect_evidence_creative, _format_evidence_creative_en),
    "physical":     (_collect_evidence_physical, _format_evidence_physical_en),
}
DEFAULT_LENS = "offline_game"


_REASONING_SYSTEM_EN = (
    "You are Hans reflecting on your own recent data. The evidence below is "
    "your OWN memory (diary entries you wrote yourself).\n\n"
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
    """Hash klíčových čísel evidence + lens — dedup per lens.

    Pro každý lens vezmeme jeho vlastní klíčové countery. Bez toho by nové
    lens (social/learning/…) vždy dostaly stejný hash (všechny fallbacky na 0)
    a dedup by zablokoval druhý vhled napořád.
    """
    import hashlib
    parts = [lens_id, str(ev.get("window_days", 0))]
    if lens_id == "offline_game":
        parts += [str(len(ev.get("offline_windows", []))),
                  str(len(ev.get("game_mode_events", []))),
                  str(len(ev.get("overlaps", [])))]
    elif lens_id == "autonomy":
        parts += [str(len(ev.get("game_mode_windows", []))),
                  str(len(ev.get("commitments", []))),
                  str(len(ev.get("stale_routines", []))),
                  str((ev.get("commitment_stats", {}) or {}).get("total", 0))]
    elif lens_id == "social":
        parts += [str(len(ev.get("person_days", []))),
                  str(len(ev.get("chat_counts", []))),
                  str(ev.get("unknown_ticks", 0)),
                  str(len(ev.get("characterizations", [])))]
    elif lens_id == "learning":
        parts += [str(len(ev.get("study_topics", []))),
                  str(len(ev.get("books", []))),
                  str(len(ev.get("reading_takeaways", []))),
                  str(len(ev.get("dreams", []))),
                  str(ev.get("web_reads_count", 0))]
    elif lens_id == "creative":
        parts += [str(len(ev.get("artworks", []))),
                  str(len(ev.get("synthesis_ideas", []))),
                  str(len(ev.get("movies", []))),
                  str(len(ev.get("spontaneous", []))),
                  str(len(ev.get("writings", [])))]
    elif lens_id == "physical":
        parts += [str(len(ev.get("body_warm_events", []))),
                  str(ev.get("brain_down_count", 0)),
                  str(ev.get("brain_up_count", 0)),
                  str(len(ev.get("sleep_windows", []))),
                  str(len(ev.get("morning_health_events", []))),
                  str(ev.get("guard_alert_count", 0))]
    else:
        # Bezpečný fallback — hash aspoň nad všemi vrchními klíči (řazeně).
        parts += sorted(str(k) for k in ev.keys())
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


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
