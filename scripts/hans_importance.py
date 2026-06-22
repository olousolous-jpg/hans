"""scripts/hans_importance.py

AUTOBIOGRAPHICAL_IMPORTANCE_V1 — importance scoring deníkových epizod.

Krok 1 autobiografické vrstvy (viz paměť autobiographical-layer-roadmap):
Generative Agents (Park 2023) retrieval = relevance + recency + IMPORTANCE.
Hansovi importance dimenze chyběla → deníkové eventy nešlo odlišit (pivotní
epizoda vs šum). Tahle vrstva ji doplňuje: base model (NE persona-finetune,
anti-konfabulace) ohodnotí každou EPIZODICKOU událost 0-10 podle toho, jak
moc vypovídá o tom, KDO Hans je / jak ho formuje.

Skóre je předpoklad pro krok 2 (self-defining memories — kurátor nejdůležitějších
epizod, které čte Severka) a krok 3 (narativní konsolidace vážená importance).

NEdrží stav, jen čte+píše sloupec diary.importance. Idempotentní (skóruje jen
WHERE importance IS NULL) → self-healing catch-up, když nějaká noc vynechá.

API:
  ensure_column(db_path)                              # idempotentní migrace
  score_unscored(config, db_path, model, timeout, limit=30) -> int
"""
from __future__ import annotations
import json
import logging
import re
import sqlite3

_log = logging.getLogger("hans_importance")

# Kurátorovaný EPIZODICKÝ set — co vypovídá o identitě / vnitřním životě.
# VĚDOMĚ vynecháno: perceptuální firehose (person_seen, teddy_arrived,
# teddy_dialog = surový dialog) a systém/rutina (idle_*, phase_change, body_*,
# brain_*, activity, face_enroll, sleep_*, movie_browsed). Šum se neskóruje.
SCORABLE_TYPES = (
    "book_reflection", "book_read", "book_started", "reading_takeaway",
    "book_completion_reflection",
    "movie_opinion",
    "case_opened", "case_closed", "case_resolution", "case_thought",
    "dream", "night_summary", "evening_reflection",
    "introspection", "spontaneous",
    "human_chat", "chat_reflection", "dialog_reflection",
    "observation",
    "work_created", "goal_completed",
    "fact_correction", "person_recollection", "characterization_update",
    "interest_update", "distillation_finding",
    "artwork",  # HANS_ART_VERDICT_V1 — Hansův verdikt o vlastním obrazu může
                # být self-defining (talent/vkus → formuje charakter přes Severku)
)

_SYSTEM = (
    "Jsi analytik autobiografické paměti. Dostaneš seznam deníkových epizod "
    "jedné postavy. Každou ohodnoť celým číslem 0-10 podle toho, JAK MOC ta "
    "epizoda vypovídá o tom, kdo postava JE, nebo jak ji formuje:\n"
    "- 8-10: pivotní/formující — vyhraněný názor, hluboká reflexe, dočtená "
    "kniha co zanechala stopu, zlom ve vztahu, splněný cíl, silná emoce.\n"
    "- 4-7: smysluplné, ale běžné — zajímavá myšlenka, dílčí pozorování, "
    "rozhovor bez zlomu.\n"
    "- 0-3: zapomenutelné — rutina, mělká poznámka, nahodilost.\n"
    "Hodnoť střízlivě a rozlišuj (ne všechno je 7). Vycházej VÝHRADNĚ z textu, "
    "nic si nedomýšlej. Vrať POUZE JSON pole objektů {\"id\": <int>, "
    "\"score\": <0-10>} pro KAŽDÉ uvedené id. Nic víc, žádný markdown."
)


def ensure_column(db_path: str) -> None:
    """Idempotentní migrace: diary.importance INTEGER (NULL = neoskórováno)."""
    conn = None
    try:
        conn = sqlite3.connect(db_path, timeout=5.0)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(diary)").fetchall()]
        if "importance" not in cols:
            conn.execute("ALTER TABLE diary ADD COLUMN importance INTEGER")
            conn.commit()
            _log.info("diary.importance sloupec přidán (AUTOBIOGRAPHICAL_IMPORTANCE_V1)")
    except Exception as _e:
        _log.warning("ensure_column selhal: %s", _e)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _parse_scores(raw: str) -> dict:
    """Z LLM odpovědi vytáhne {id: score}. Tolerantní (markdown, šum okolo)."""
    if not raw:
        return {}
    m = re.search(r"\[.*\]", raw, re.S)
    if not m:
        return {}
    try:
        arr = json.loads(m.group(0))
    except Exception:
        return {}
    out = {}
    for it in arr if isinstance(arr, list) else []:
        try:
            _id = int(it["id"])
            _sc = int(round(float(it["score"])))
            out[_id] = max(0, min(10, _sc))
        except Exception:
            continue
    return out


def score_unscored(config: dict, db_path: str, model: str,
                   timeout: int, limit: int = 30,
                   keep_alive=0) -> int:  # IMPORTANCE_NIGHTLY_CATCHUP_V1
    """Ohodnotí dávku neoskórovaných epizod (newest-first). Vrací počet
    oskórovaných. NIKDY nevyhazuje výjimku nahoru (smí selhat tiše)."""
    ensure_column(db_path)
    conn = None
    try:
        conn = sqlite3.connect(db_path, timeout=5.0)
        ph = ",".join("?" * len(SCORABLE_TYPES))
        rows = conn.execute(
            "SELECT id, event_type, title, COALESCE(data, note, '') "
            "FROM diary WHERE importance IS NULL AND event_type IN (%s) "
            "ORDER BY ts DESC LIMIT ?" % ph,
            (*SCORABLE_TYPES, int(limit))
        ).fetchall()
    except Exception as _e:
        _log.warning("score_unscored select selhal: %s", _e)
        if conn is not None:
            conn.close()
        return 0

    if not rows:
        if conn is not None:
            conn.close()
        return 0

    lines = []
    for _id, etype, title, body in rows:
        snippet = (str(title or "") + " — " + str(body or "")).strip()[:200]
        snippet = snippet.replace("\n", " ")
        lines.append(f"id={_id} [{etype}] {snippet}")
    prompt = "EPIZODY K OHODNOCENÍ:\n" + "\n".join(lines)

    try:
        from scripts.ollama_client import ollama_generate
    except ImportError:
        _log.warning("score_unscored: ollama_client nedostupný")
        conn.close()
        return 0

    try:
        raw = ollama_generate(
            model=model, prompt=prompt, system=_SYSTEM,
            config=config, timeout=timeout,
            keep_alive=keep_alive,  # MODEL_KEEPALIVE_TIERS_V1 / IMPORTANCE_NIGHTLY_CATCHUP_V1 (loop=warm)
            # num_predict: ~25 znaků/položku × limit → musí se vejít uzavřený JSON,
            # jinak se výstup uřízne a nejde parsovat (zjištěno při backfillu).
            options={"temperature": 0.1, "num_predict": 40 * max(int(limit), 1)})
    except Exception as _e:
        _log.warning("score_unscored: LLM selhal: %s", _e)
        conn.close()
        return 0

    scores = _parse_scores(raw)
    if not scores:
        _log.warning("score_unscored: LLM nevrátil parsovatelná skóre")
        conn.close()
        return 0

    n = 0
    valid_ids = {r[0] for r in rows}
    try:
        for _id, _sc in scores.items():
            if _id in valid_ids:
                conn.execute("UPDATE diary SET importance=? WHERE id=?", (_sc, _id))
                n += 1
        conn.commit()
    except Exception as _e:
        _log.warning("score_unscored: UPDATE selhal: %s", _e)
    finally:
        conn.close()

    _log.info("importance: oskórováno %d/%d epizod (model=%s)", n, len(rows), model)
    return n


# ── Smoke (python3 scripts/hans_importance.py) ──────────────────────────────
if __name__ == "__main__":
    import sys
    cfg = {}
    try:
        with open("config.json", encoding="utf-8") as fh:
            cfg = json.load(fh)
    except Exception as exc:  # noqa
        print("WARN: config.json nenačten (%s)" % exc)
    db = cfg.get("diary_db", "data/hans_diary.db")
    ensure_column(db)
    er = cfg.get("evening_reflection", {}) or {}
    model = str(er.get("model", "jobautomation/OpenEuroLLM-Czech:latest"))
    timeout = int(er.get("llm_timeout", 300))
    n_arg = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    print("scoring up to %d unscored episodes na modelu %s ..." % (n_arg, model))
    print("oskórováno:", score_unscored(cfg, db, model, timeout, limit=n_arg))
    # výpis
    conn = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
    rows = conn.execute(
        "SELECT importance, event_type, substr(COALESCE(title,data,note,''),1,50) "
        "FROM diary WHERE importance IS NOT NULL ORDER BY importance DESC, ts DESC LIMIT 20"
    ).fetchall()
    conn.close()
    print("\nTop oskórované:")
    for imp, et, t in rows:
        print(f"  {imp:2d}  [{et}] {t}")
    sys.exit(0)
