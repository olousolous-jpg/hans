"""HANS_LEARNING_PROGRESS_V1 (27. 9.) — zájem tam, kde se Hans ještě učí.

Learning progress koníčku = NOVOST pojmů v tom, co k němu Hans nedávno ČETL:
podíl kmenů článku, které v četbě téhož koníčku dosud nebyly. Zvládnuté nebo
omílané téma se blíží nule, čerstvé drží kolem 0,5 a výš.

Rozhodnutí uživatele 27. 9.: učení = JEN četba a studium (filmy a TV ne);
působí na SÍLU koníčku (`HobbyStore.prepocitej_silu`) i na VÝBĚR STUDIA
(`StudyStore.ensure_program`), obojí jako násobek (0,5 + LP). Málo dat
(< min_n článků v okně) = neutrální násobek 1,0.

Čte se `web_read.note` = CIZÍ text článku, ne Hansova próza (jeho úvahy by
byly ozvěna). Ke koníčku se článek přiřadí podle významu (bge-m3, název +
příklady koníčku); přiřazení se ukládá do `hobby_reading` jednou provždy,
takže noc embeduje jen nové články.

📏 Retro 27. 9. (`data/mereni/learning_progress/`): Design 0,73 → 0,02 (tentýž
článek 660×), japonská kultura 0,36 → 0,13, filmy a krimi stabilně ~0,5.
Dnes mění málo (zvládnutá témata už stáhlo vyhasínání síly); chrání před
opakovanou četbou bez nového, kterou vyhasínání nechytí. Backlog
`LEARNING_PROGRESS_27_09`.
⚠️ Přiřazení se nepřepočítává: nový nebo přejmenovaný koníček dostane jen
články přečtené po svém vzniku.
"""
from __future__ import annotations

import collections
import json
import logging
import os
import re
import sqlite3
import time
import urllib.request

_log = logging.getLogger("hans_learning")

_SLOVO = re.compile(r"[a-zA-ZÀ-ž]{5,}")
_STITEK = re.compile(r"^\[\w+\]\s*")


def _cfg(config: dict) -> dict:
    return ((config.get("hobbies", {}) or {}).get("learning_progress", {}) or {})


def zapnuto(config: dict) -> bool:
    return bool(_cfg(config).get("enabled", False))


def _init(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS hobby_reading (
        diary_id INTEGER PRIMARY KEY,
        ts       REAL NOT NULL,
        hobby    TEXT,          -- NULL = článek nepatří k žádnému koníčku
        sim      REAL,
        margin   REAL)""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_hobby_reading_h "
                 "ON hobby_reading(hobby, ts)")


def _kmeny(text: str) -> set:
    return {w.lower()[:6] for w in _SLOVO.findall(_STITEK.sub("", text or ""))}


def _embed(config: dict, texts: list):
    from scripts.ollama_client import game_mode_on
    if game_mode_on():
        return None
    import numpy as np
    url = config.get("openwebui_chat", {}).get("base_url", "http://127.0.0.1:11434")
    model = (config.get("hans_library", {}).get("embed_model") or "bge-m3:latest")
    req = urllib.request.Request(
        url + "/api/embed",
        data=json.dumps({"model": model, "input": texts}).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        e = np.array(json.load(r)["embeddings"], dtype=np.float32)
    return e / np.linalg.norm(e, axis=1, keepdims=True)


def prirad_novou_cetbu(config: dict, diary_db_path: str, limit: int = 400) -> int:
    """Přiřadí dosud nezpracované články ke koníčkům. Vrací počet zpracovaných.
    Selhání (PC spí, herní mód) = 0, zkusí se příští noc."""
    import numpy as np
    c = _cfg(config)
    prah = float(c.get("sim_min", 0.45))
    odstup = float(c.get("margin_min", 0.05))
    conn = sqlite3.connect(diary_db_path, timeout=10)
    try:
        _init(conn)
        hob = conn.execute("SELECT name, examples FROM hobbies "
                           "WHERE status='active'").fetchall()
        rows = conn.execute(
            "SELECT id, ts, title, note FROM diary WHERE event_type='web_read' "
            "AND length(note)>100 AND id NOT IN (SELECT diary_id FROM hobby_reading) "
            "ORDER BY ts LIMIT ?", (int(limit),)).fetchall()
        if len(hob) < 2 or not rows:
            return 0
        H = _embed(config, ["%s: %s" % (n, ", ".join(json.loads(e or "[]")[:6]))
                            for n, e in hob])
        if H is None:
            return 0
        hotovo = 0
        for i in range(0, len(rows), 64):
            part = rows[i:i + 64]
            E = _embed(config, ["%s. %s" % (t, (n or "")[:500])
                                for _, _, t, n in part])
            if E is None:
                break
            S = E @ H.T
            for (rid, ts, _, _), s in zip(part, S):
                o = np.sort(s)
                b = int(s.argmax())
                ok = s[b] >= prah and o[-1] - o[-2] >= odstup
                conn.execute("INSERT OR REPLACE INTO hobby_reading VALUES (?,?,?,?,?)",
                             (rid, ts, hob[b][0] if ok else None,
                              round(float(s[b]), 4), round(float(o[-1] - o[-2]), 4)))
            conn.commit()
            hotovo += len(part)
        return hotovo
    except Exception as e:
        _log.warning("learning: přiřazení četby selhalo: %s", e)
        return 0
    finally:
        conn.close()


def learning_progress(config: dict, diary_db_path: str) -> dict:
    """{koníček: (lp, n)}; lp None = málo dat (neutrální)."""
    c = _cfg(config)
    okno = float(c.get("window_days", 60)) * 86400
    posl = int(c.get("last_n", 10))
    min_n = int(c.get("min_n", 3))
    obecne_podil = float(c.get("common_share", 0.08))
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % diary_db_path, uri=True, timeout=5)
        try:
            rows = conn.execute(
                "SELECT hr.hobby, hr.ts, d.note FROM hobby_reading hr "
                "JOIN diary d ON d.id = hr.diary_id ORDER BY hr.ts").fetchall()
            vse = conn.execute("SELECT count(*) FROM hobby_reading").fetchone()[0]
            df = collections.Counter()
            for (note,) in conn.execute(
                    "SELECT d.note FROM hobby_reading hr JOIN diary d "
                    "ON d.id = hr.diary_id"):
                df.update(_kmeny(note))
        finally:
            conn.close()
    except Exception as e:
        _log.warning("learning: výpočet selhal: %s", e)
        return {}
    obecne = {k for k, n in df.items() if n > obecne_podil * max(vse, 1)}
    videno = collections.defaultdict(set)
    novost = collections.defaultdict(list)
    for hobby, ts, note in rows:
        if not hobby:
            continue
        k = _kmeny(note) - obecne
        if not k:
            continue
        novost[hobby].append((ts, len(k - videno[hobby]) / len(k)))
        videno[hobby] |= k
    ted = time.time()
    out = {}
    for hobby, L in novost.items():
        v = [n for ts, n in L if ts > ted - okno][-posl:]
        out[hobby] = ((sum(v) / len(v)) if len(v) >= min_n else None, len(v))
    return out


def nasobek(lp_map: dict, hobby: str) -> float:
    lp = (lp_map.get(hobby) or (None, 0))[0]
    return 1.0 if lp is None else 0.5 + lp
