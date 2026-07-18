"""HANS_ANOMALY_V1 — algoritmický detektor odchylek v Hansově chování.

Doplněk k `hans_self_insight` ([[genuinni-sebe-odvozeni]] C-varianta):
- **A (lenses):** předepsané perspektivy, LLM analytika (drahé, weekly)
- **C (anomaly):** algoritmické „něco tento týden vybočuje" (levné, weekly)

**Metrika = počet eventů daného event_type za posledních 7 dní × týdenní
průměr z předchozích 4 týdnů** (28 dní). Flag když ratio ≥ 1.5 nebo ≤ 0.5
(default; konfigurovatelné). Minimální baseline count (default 3), aby se
neflagovaly zřídkavé jevy s náhodnými skoky (2 vs 5 = ratio 2.5, ale statisticky
nevýznamné).

**Anti-konfab:** detekce je čistě algoritmická (žádný LLM), fakta jsou pravá.
LLM se použije JEN pro CS formulaci výsledků (voice krok jako v self_insight).
Hans nedostane hotový závěr, dostane fakt „X je tento týden 2× víc než obvykle"
a musí sám rozhodnout, co s tím.

Výstup: (1) chat/Telegram `/anomalie`, (2) zápis do `diary(event_type='anomaly_note',
importance=7)` → dostane se do `_night_reflection.moments`.
"""
from __future__ import annotations

import datetime as _dt
import logging
import sqlite3
import time
from typing import Optional

_log = logging.getLogger(__name__)


# Metriky, které stojí za sledování. Klíč = user-friendly název, hodnota =
# event_type v diary. Pokud budeš chtít víc (např. commitments, mood_change),
# přidej sem.
_METRICS = {
    "myšlenky (spontaneous)":      "spontaneous",
    "dialog s Koláčem":            "teddy_dialog",
    "rozhovory s vámi":            "human_chat",
    "čtení webu":                  "web_read",
    "studijní poznámky":           "study_note",
    "sledování osob":              "person_seen",
    "úvahy (introspection)":       "introspection",
    "reflexe knihy":               "book_reflection",
    "názor na film":               "movie_opinion",
    "výpadky mozku":               "brain_down",
}


def _count_in_window(conn, event_type: str, since_ts: float,
                     until_ts: float) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM diary WHERE event_type=? AND ts >= ? AND ts < ?",
        (event_type, since_ts, until_ts)).fetchone()
    return int(row[0]) if row else 0


def detect_anomalies(diary_db_path: str,
                     window_days: int = 7,
                     baseline_weeks: int = 4,
                     ratio_high: float = 1.5,
                     ratio_low: float = 0.5,
                     min_baseline_count: int = 3) -> list[dict]:
    """Vrátí seznam odchylek — čistě algoritmicky, žádný LLM.

    Struktura každého záznamu:
      {
        'metric': 'úvahy (introspection)',
        'event_type': 'introspection',
        'this_week': 12,
        'baseline_weekly_avg': 4.5,
        'ratio': 2.67,
        'direction': 'up' | 'down',
      }
    """
    now = time.time()
    week_ago = now - window_days * 86400.0
    baseline_start = now - (window_days + baseline_weeks * 7) * 86400.0
    baseline_end = week_ago
    baseline_days = baseline_weeks * 7
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % diary_db_path,
                               uri=True, timeout=5.0)
    except Exception as e:
        _log.warning("anomaly: DB open selhalo: %s", e)
        return []
    out = []
    try:
        for label, etype in _METRICS.items():
            this_week = _count_in_window(conn, etype, week_ago, now)
            base_total = _count_in_window(conn, etype, baseline_start, baseline_end)
            base_weekly = base_total * (window_days / baseline_days) if base_total else 0
            # ratio: vyhni se dělení nulou. Když baseline < min → přeskoč.
            if base_weekly < min_baseline_count:
                continue
            ratio = this_week / base_weekly if base_weekly > 0 else 0
            if ratio >= ratio_high or ratio <= ratio_low:
                out.append({
                    "metric": label,
                    "event_type": etype,
                    "this_week": this_week,
                    "baseline_weekly_avg": round(base_weekly, 1),
                    "ratio": round(ratio, 2),
                    "direction": "up" if ratio >= ratio_high else "down",
                })
    finally:
        conn.close()
    # Nejsilnější odchylky nahoru
    out.sort(key=lambda a: abs(a["ratio"] - 1), reverse=True)
    return out


def format_anomalies_facts(anomalies: list[dict]) -> str:
    """Formátování v jazyce faktů (bez interpretace — pro voice krok / prompt)."""
    if not anomalies:
        return ("V posledních 7 dnech nic neobvyklého — všechny sledované "
                "metriky se drží v rozsahu, na jaký jsem zvyklý.")
    lines = ["V posledních 7 dnech jsem si všiml těchto odchylek oproti "
             "měsíčnímu průměru:"]
    for a in anomalies:
        direction = "více" if a["direction"] == "up" else "méně"
        lines.append(
            "  • %s: tento týden %d, obvykle %.1f týdně (%.1f× %s než "
            "průměr)" % (a["metric"], a["this_week"],
                         a["baseline_weekly_avg"], a["ratio"], direction))
    return "\n".join(lines)


_VOICE_SYSTEM_CS = (
    "Jsi Hans — anglický majordomus z 19. století, který mluví česky. "
    "Dostaneš faktický seznam odchylek ve svém vlastním chování za poslední "
    "týden (proti měsíčnímu průměru). Napiš 3-5 vět v první osobě, klidným "
    "tónem, jako svou vlastní úvahu: co sis všiml, co ti přijde překvapivé, "
    "co ti přijde všední. NEVymýšlej si vysvětlení, které z čísel neplynou "
    "(vyhni se frázi 'to je proto že...' pokud to není očividné). Když je "
    "odchylka málo — napiš to. Bez emoji, bez odrážek, prostě text."
)


def format_anomalies_cs(config: dict, anomalies: list[dict]) -> Optional[str]:
    """Voice krok — přeloží faktický seznam do Hansovy CS úvahy.
    None když LLM offline; fallback = surová fakta (still užitečné)."""
    facts = format_anomalies_facts(anomalies)
    if not anomalies:
        return facts  # bez odchylek není o čem přemýšlet — vrátit tak jak je
    try:
        from scripts.ollama_client import ollama_chat
        cfg = (config.get("self_insight", {}) or {})
        model = cfg.get("voice_model", "hans-czech:latest")
        url = cfg.get("voice_url", cfg.get("reasoning_url",
                                            "http://192.168.1.100:11434"))
        raw = ollama_chat(
            model,
            [
                {"role": "system", "content": _VOICE_SYSTEM_CS},
                {"role": "user", "content": facts},
            ],
            ollama_url=url,
            options={"num_ctx": 4096, "temperature": 0.4, "num_predict": 500},
        )
        return (raw or "").strip() or facts
    except Exception as e:
        _log.debug("anomaly voice krok selhal: %s", e)
        return facts


def write_anomaly_note(diary_db_path: str, cs_text: str,
                       n_anomalies: int) -> bool:
    """Zápis do deníku jako anomaly_note (importance=7 → do night_reflection)."""
    try:
        conn = sqlite3.connect(diary_db_path, timeout=5.0)
        conn.execute(
            "INSERT INTO diary (ts, event_type, title, note, importance) "
            "VALUES (?,?,?,?,?)",
            (time.time(), "anomaly_note",
             f"Týdenní odchylky ({n_anomalies})", cs_text, 7))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        _log.warning("anomaly: write failed: %s", e)
        return False


def latest_anomaly_note(diary_db_path: str) -> Optional[dict]:
    """Poslední anomaly_note — pro `/anomalie` chat command."""
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % diary_db_path,
                               uri=True, timeout=3.0)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT ts, title, note FROM diary WHERE event_type='anomaly_note' "
            "ORDER BY ts DESC LIMIT 1").fetchone()
        conn.close()
        return dict(row) if row else None
    except Exception:
        return None


def maybe_run(diary_db_path: str, config: dict) -> Optional[str]:
    """Nightly hook — spustí detekci JEN pokud od minulého uplynulo
    ≥ `cadence_days` (default 7). Deferral-safe. Vrátí CS text nebo None."""
    cfg = (config.get("anomaly", {}) or {})
    if not cfg.get("enabled", False):
        return None
    try:
        from scripts.ollama_client import game_mode_on
        if game_mode_on():
            return None
    except Exception:
        pass
    cadence_days = int(cfg.get("cadence_days", 7))
    # kadence per anomaly_note
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % diary_db_path,
                               uri=True, timeout=3.0)
        row = conn.execute(
            "SELECT ts FROM diary WHERE event_type='anomaly_note' "
            "ORDER BY ts DESC LIMIT 1").fetchone()
        conn.close()
        if row and (time.time() - row[0]) < cadence_days * 86400:
            _log.debug("anomaly: dříve než %dd, skip", cadence_days)
            return None
    except Exception:
        pass
    return run_once(diary_db_path, config)


def run_once(diary_db_path: str, config: dict) -> Optional[str]:
    """Jednorázový detect+format+write. Vrátí CS text (co bylo zapsáno) nebo None."""
    cfg = (config.get("anomaly", {}) or {})
    anomalies = detect_anomalies(
        diary_db_path,
        window_days=int(cfg.get("window_days", 7)),
        baseline_weeks=int(cfg.get("baseline_weeks", 4)),
        ratio_high=float(cfg.get("ratio_high", 1.5)),
        ratio_low=float(cfg.get("ratio_low", 0.5)),
        min_baseline_count=int(cfg.get("min_baseline_count", 3)),
    )
    _log.info("anomaly: detekováno %d odchylek", len(anomalies))
    cs = format_anomalies_cs(config, anomalies)
    if cs and anomalies:  # zapíšeme JEN když jsou odchylky (jinak by deník rostl)
        write_anomaly_note(diary_db_path, cs, len(anomalies))
    return cs


if __name__ == "__main__":
    import sys, json
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    db = sys.argv[1] if len(sys.argv) > 1 else "data/hans_diary.db"
    cfg = json.load(open("config.json"))
    cfg.setdefault("anomaly", {})["enabled"] = True
    if len(sys.argv) > 2 and sys.argv[2] == "detect":
        # jen algoritmicky, žádný LLM
        anomalies = detect_anomalies(db)
        print("=== ANOMÁLIE (algoritmicky, %d) ===" % len(anomalies))
        print(format_anomalies_facts(anomalies))
    else:
        # full run: detect + voice + write
        print("=== FULL RUN (write + voice) ===")
        cs = run_once(db, cfg)
        print(cs or "(nic)")
