"""
Hans Web Admin
FastAPI webové rozhraní pro monitoring a konfiguraci.
Spusť: python web_admin.py
Dostupné: http://raspi5:7860
"""

import json
import pickle
import sqlite3
import time
import urllib.parse
from datetime import datetime
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from scripts.web_admin_questions import router as questions_router, init as questions_init

CONFIG_PATH   = Path("config.json")
DIARY_PATH    = Path("data/hans_diary.db")
FACES_PATH    = Path("data/known_faces.pkl")
CLUSTER_PATH  = Path("data/known_faces_cluster.pkl")
SURR_PATH     = Path("data/surroundings.db")
CONV_DIR      = Path("data/conversations")
SYSTEM_LOG    = Path("data/system.log")
TEMPLATES_DIR = Path("templates")
TEMPLATES_DIR.mkdir(exist_ok=True)

app = FastAPI(title="Hans Admin", docs_url=None, redoc_url=None)
# Hansovy otázky — async fronta dotazů obyvatelům
try:
    import json as _qj
    _qcfg = _qj.load(open("config.json"))
    _qdb  = _qcfg.get("hans_idle", {}).get("diary_db", "data/hans_diary.db")
    questions_init(_qcfg, _qdb)
    app.include_router(questions_router)
    print("[web_admin] questions module loaded -> /questions/")
except Exception as _qe:
    print(f"[web_admin] questions module FAILED: {_qe}")

# ── Config defaults — save/load ─────────────────────────────────────────────
@app.post("/api/config/save_default")
def save_config_default():
    """Uloží aktuální config.json jako config.default.json."""
    import shutil as _shutil
    src = Path("config.json")
    dst = Path("config.default.json")
    if not src.exists():
        raise HTTPException(404, "config.json nenalezen")
    _shutil.copy2(src, dst)
    return {"ok": True, "message": "Default uložen jako config.default.json"}

@app.post("/api/config/load_default")
def load_config_default():
    """Přepíše config.json z config.default.json."""
    import shutil as _shutil, json as _json
    src = Path("config.default.json")
    dst = Path("config.json")
    if not src.exists():
        raise HTTPException(404, "config.default.json neexistuje — nejdřív ulož default")
    # Záloha před přepisem
    bak = dst.with_suffix(f".json.bak_before_default_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
    _shutil.copy2(dst, bak)
    _shutil.copy2(src, dst)
    return {"ok": True, "message": f"Config obnoven z default (záloha: {bak.name})"}
# ── Švitorka mód ────────────────────────────────────────────────────────────
@app.post("/api/toaster/{action}")
def toaster_mode(action: str):
    """Zapne/vypne Švitorka mód. action = 'on' nebo 'off'."""
    try:
        import importlib, json
        cfg = json.loads(Path("config.json").read_text(encoding="utf-8"))
        # Najdi HansDialog instanci — importujeme přes hans_idle
        # V reálu to musí být propojené přes globální referenci
        # Prozatím: uložíme flag do config.json a hans_dialog si ho přečte
        cfg.setdefault("hans_dialog", {})
        cfg["hans_dialog"]["toaster_mode"] = (action == "on")
        Path("config.json").write_text(
            json.dumps(cfg, indent=4, ensure_ascii=False), encoding="utf-8")
        return {"ok": True, "toaster": action == "on"}
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.get("/api/toaster")
def toaster_status():
    """Vrátí stav Švitorka módu."""
    try:
        import json
        cfg = json.loads(Path("config.json").read_text(encoding="utf-8"))
        active = cfg.get("hans_dialog", {}).get("toaster_mode", False)
        return {"toaster": active}
    except Exception:
        return {"toaster": False}




# ── Config helpers ────────────────────────────────────────────────────────────

def load_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        return {"_error": str(e)}


def save_config(cfg: dict) -> bool:
    try:
        CONFIG_PATH.write_text(
            json.dumps(cfg, indent=4, ensure_ascii=False),
            encoding="utf-8")
        return True
    except Exception:
        return False


# ── API endpoints ─────────────────────────────────────────────────────────────

# ── Hans-Koláč dialog trigger ───────────────────────────────────────────────
# DIALOG_TRIGGER_ENDPOINT_PATCH
@app.post("/api/dialog/trigger")
def trigger_dialog():
    """Vyvolá manuální Hans-Koláč dialog. Vytvoří flag soubor,
    který hans_dialog._loop detekuje v dalším tiku (do 30s)."""
    try:
        flag = Path("data/.trigger_dialog")
        flag.parent.mkdir(exist_ok=True)
        flag.touch()
        return {"ok": True, "message": "Dialog naplánován (spustí se do 30s)"}
    except Exception as e:
        raise HTTPException(500, f"Trigger failed: {e}")

@app.get("/api/dialog/status")
def dialog_status():
    """Vrátí stav trigger flagu (pending = čeká na zpracování)."""
    flag = Path("data/.trigger_dialog")
    return {"pending": flag.exists()}

# ── Video enrollment trigger ────────────────────────────────────────────────
# VIDEO_ENROLL_ENDPOINT
class _VideoEnrollBody(__import__('pydantic').BaseModel):
    name: str
    seconds: int = 30

@app.post("/api/enroll/start")
def video_enroll_start(body: _VideoEnrollBody):
    """Spustí video enrollment session.
    # ENROLL_MULTI_ENDPOINT
    seconds=0 → multi-phase mode (close/mid/far)."""
    name = body.name.strip().lower()
    secs = int(body.seconds)
    if not name:
        raise HTTPException(400, "Chybí jméno")
    try:
        flag = Path("data/.video_enroll")
        flag.parent.mkdir(exist_ok=True)
        if secs <= 0:
            flag.write_text(f"multi:{name}|0")
            return {"ok": True, "name": name, "seconds": 0,
                    "message": f"Multi-phase enroll '{name}' (~2 min)"}
        secs = max(5, min(secs, 120))
        flag.write_text(f"{name}|{secs}")
        return {"ok": True, "name": name, "seconds": secs,
                "message": f"Video enroll '{name}' naplánován na {secs}s"}
    except Exception as e:
        raise HTTPException(500, str(e))

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    html = (TEMPLATES_DIR / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(html)


@app.get("/api/config")
async def get_config():
    return load_config()


# SCHEMA_DRIVEN_TABS_V1 — sdílené schéma polí (stejné jako Tkinter ConfigGUI)
@app.get("/api/config/schema")
def get_config_schema():
    from scripts import config_schema
    return {"groups": config_schema.web_groups(),
            "categories": config_schema.categories()}


@app.post("/api/config")
async def post_config(request: Request):
    try:
        body = await request.json()
        # Validace JSON
        json.dumps(body)
        ok = save_config(body)
        return {"ok": ok}
    except Exception as e:
        raise HTTPException(400, str(e))


@app.get("/api/diary")
async def get_diary(limit: int = 100, event_type: str = ""):
    if not DIARY_PATH.exists():
        return []
    try:
        conn = sqlite3.connect(str(DIARY_PATH))
        if event_type:
            rows = conn.execute(
                "SELECT datetime(ts,'unixepoch','localtime'), event_type, title, note "
                "FROM diary WHERE event_type=? ORDER BY ts DESC LIMIT ?",
                (event_type, limit)).fetchall()
        else:
            rows = conn.execute(
                "SELECT datetime(ts,'unixepoch','localtime'), event_type, title, note "
                "FROM diary ORDER BY ts DESC LIMIT ?",
                (limit,)).fetchall()
        conn.close()
        return [{"dt": r[0], "type": r[1], "title": r[2] or "", "note": r[3] or ""}
                for r in rows]
    except Exception as e:
        return {"error": str(e)}


# WEBADMIN_V2 — Deník = jen Hansovo reflektivní psaní (večerní introspekce ap.);
# firehose (person_seen…) je za „Rozšířené". Text žije v note NEBO data dle typu.
REFLECTIVE_TYPES = ("introspection", "evening_reflection", "night_summary", "dream",
                    "narrative_chapter", "book_reflection", "book_completion_reflection",
                    "reading_takeaway")


@app.get("/api/diary/reflective")
async def get_diary_reflective(limit: int = 150):
    if not DIARY_PATH.exists():
        return []
    try:
        conn = sqlite3.connect(str(DIARY_PATH))
        ph = ",".join("?" * len(REFLECTIVE_TYPES))
        rows = conn.execute(
            f"SELECT datetime(ts,'unixepoch','localtime'), event_type, title, "
            f"COALESCE(NULLIF(note,''), data) FROM diary "
            f"WHERE event_type IN ({ph}) ORDER BY ts DESC LIMIT ?",
            (*REFLECTIVE_TYPES, limit)).fetchall()
        conn.close()
        return [{"dt": r[0], "type": r[1], "title": r[2] or "", "note": r[3] or ""}
                for r in rows]
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/diary/entry")
async def get_diary_entry(type: str, offset: int = 0):
    """Jeden záznam daného typu na pozici offset (0=nejnovější) + celkový počet.
    Pro Deník karty s ◀▶ historií. ts slouží k live-detekci nového záznamu."""
    if not DIARY_PATH.exists():
        return {"total": 0, "offset": offset, "empty": True}
    try:
        conn = sqlite3.connect(str(DIARY_PATH))
        total = conn.execute("SELECT COUNT(*) FROM diary WHERE event_type=?",
                             (type,)).fetchone()[0]
        row = conn.execute(
            "SELECT datetime(ts,'unixepoch','localtime'), title, "
            "COALESCE(NULLIF(note,''), data), ts FROM diary "
            "WHERE event_type=? ORDER BY ts DESC LIMIT 1 OFFSET ?",
            (type, max(0, offset))).fetchone()
        conn.close()
        if not row:
            return {"total": total, "offset": offset, "empty": True}
        text = row[2] or ""
        # WEBADMIN_V2 — u díla (work_created) načti PLNÝ text eseje ze souboru
        # (note nese jen anonci + název .md souboru v data/hans_works/)
        if type == "work_created":
            import re as _re
            m = _re.search(r"([\w\-]+\.md)", row[2] or "")
            if m:
                wp = Path("data/hans_works") / m.group(1)
                if wp.exists():
                    try:
                        text = wp.read_text(encoding="utf-8").strip()
                    except Exception:
                        pass
        return {"total": total, "offset": offset, "dt": row[0],
                "title": row[1] or "", "text": text, "ts": row[3]}
    except Exception as e:
        return {"error": str(e), "empty": True}


@app.get("/api/diary/types")
async def get_diary_types():
    if not DIARY_PATH.exists():
        return []
    try:
        conn = sqlite3.connect(str(DIARY_PATH))
        rows = conn.execute(
            "SELECT DISTINCT event_type, COUNT(*) as n FROM diary "
            "GROUP BY event_type ORDER BY n DESC").fetchall()
        conn.close()
        return [{"type": r[0], "count": r[1]} for r in rows]
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/faces")
async def get_faces():
    result = []
    if FACES_PATH.exists():
        try:
            with open(FACES_PATH, "rb") as f:
                db = pickle.load(f)
            for name, embs in db.items():
                lst = embs if isinstance(embs, list) else [embs]
                result.append({"name": name, "samples": len(lst), "db": "arcface"})
        except Exception as e:
            result.append({"error": str(e)})
    if CLUSTER_PATH.exists():
        try:
            with open(CLUSTER_PATH, "rb") as f:
                cdb = pickle.load(f)
            for name, person in cdb.items():
                existing = next((r for r in result if r.get("name") == name), None)
                clusters = len(person.clusters) if hasattr(person, "clusters") else 0
                samples  = person.n_samples if hasattr(person, "n_samples") else 0
                if existing:
                    existing["clusters"] = clusters
                    existing["cluster_samples"] = samples
                else:
                    result.append({"name": name, "clusters": clusters,
                                   "cluster_samples": samples, "db": "cluster"})
        except Exception:
            pass

    # QUICK_AUGMENT_V1 — doplnit session info z Hans diáře.
    # Pro každou osobu najdi nejnovější timestamp face_enroll eventu per session.
    try:
        import sqlite3 as _sqlite
        _conn = _sqlite.connect("data/hans_diary.db")
        _cur = _conn.cursor()
        _cur.execute("SELECT ts, title, data FROM diary "
                     "WHERE event_type = 'face_enroll' ORDER BY ts ASC")
        _sessions = {}  # name → {morning: ts, afternoon: ts, evening: ts}
        for ts, name, data_json in _cur.fetchall():
            try:
                d = json.loads(data_json or "{}")
                sess = d.get("session")
                if sess in ("morning", "afternoon", "evening"):
                    _sessions.setdefault(name, {})[sess] = ts
            except Exception:
                pass
        _conn.close()
        for r in result:
            nm = r.get("name")
            if nm:
                r["sessions"] = _sessions.get(nm, {})
    except Exception:
        pass

    return sorted(result, key=lambda x: x.get("name", ""))


# QUICK_AUGMENT_V1 — endpoint pro spuštění session augment
class _QuickAugmentBody(__import__('pydantic').BaseModel):
    name: str
    session: str  # "morning" | "afternoon" | "evening"


@app.post("/api/enroll/quick_augment")
def quick_augment_start(body: _QuickAugmentBody):
    """Spustí quick augment pro existující osobu v konkrétní světelné podmínce.
    Přidá ~5 vzorků k aktuální FaceDB a ClusterDB.
    Vyžaduje že osoba už existuje (nejdřív full enroll přes klávesu E)."""
    name = body.name.strip().lower()
    session = body.session.strip().lower()

    if not name:
        raise HTTPException(400, "Chybí jméno")
    if session not in ("morning", "afternoon", "evening"):
        raise HTTPException(400,
            f"Neplatná session: {session!r}. Povolené: morning/afternoon/evening")

    # Ověř že osoba existuje ve FaceDB
    if not FACES_PATH.exists():
        raise HTTPException(400, "FaceDB neexistuje — proveď nejdřív full enroll klávesou E")
    try:
        with open(FACES_PATH, "rb") as f:
            db = pickle.load(f)
        if name not in db:
            raise HTTPException(400,
                f"Osoba '{name}' není ve FaceDB. Nejdřív full enroll klávesou E.")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Chyba čtení FaceDB: {e}")

    # Zkontroluj že není rozdělaný jiný flag
    flag = Path("data/.quick_augment")
    if flag.exists():
        raise HTTPException(409,
            "Předchozí quick augment ještě nedoběhl, počkej chvíli")

    flag.parent.mkdir(exist_ok=True)
    flag.write_text(f"{name}|{session}")
    return {"ok": True, "name": name, "session": session,
            "message": f"Quick augment '{name}' ({session}) naplánován"}


@app.get("/api/objects")
async def get_objects(limit: int = 50):
    if not SURR_PATH.exists():
        return []
    try:
        conn = sqlite3.connect(str(SURR_PATH))
        rows = conn.execute(
            "SELECT class_name, confidence, seen_count, "
            "datetime(seen_at,'unixepoch','localtime') "
            "FROM objects ORDER BY seen_at DESC LIMIT ?",
            (limit,)).fetchall()
        conn.close()
        return [{"name": r[0], "conf": round(r[1], 2),
                 "count": r[2], "seen": r[3]} for r in rows]
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/conversations")
async def get_conversations():
    result = []
    if not CONV_DIR.exists():
        return result
    for f in sorted(CONV_DIR.glob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            msgs = data.get("messages", [])
            result.append({
                "name":    f.stem,
                "turns":   len(msgs) // 2,
                "updated": data.get("updated", ""),
                "last":    msgs[-1].get("content", "")[:80] if msgs else "",
            })
        except Exception:
            pass
    return result


@app.delete("/api/conversations/{name}")
async def delete_conversation(name: str):
    p = CONV_DIR / f"{name}.json"
    if p.exists():
        p.unlink()
        return {"ok": True}
    raise HTTPException(404, "Not found")


@app.get("/api/log")
async def get_log(lines: int = 100, filter: str = ""):
    if not SYSTEM_LOG.exists():
        return {"lines": []}
    try:
        all_lines = SYSTEM_LOG.read_text(encoding="utf-8", errors="replace").splitlines()
        all_lines.reverse()
        if filter:
            all_lines = [l for l in all_lines if filter.lower() in l.lower()]
        return {"lines": all_lines[:lines]}
    except Exception as e:
        return {"lines": [str(e)]}


# AVATAR_WEB_SIDEBAR_V1 — Hansova tvář v sidebaru. AVATAR_SINGLE_IDLE_V1: jeden
# čistý obrázek (idle), bez variant dle nálady (zkoušeno, nekonzistentní → zrušeno).
@app.get("/api/avatar.png")
def avatar_image():
    """Servíruje idle avatar PNG (nejnovější verze)."""
    import glob
    vers = []
    for d in glob.glob("data/avatar/v*"):
        try:
            vers.append((int(Path(d).name[1:]), d))
        except ValueError:
            pass
    if not vers:
        raise HTTPException(status_code=404, detail="no avatar")
    p = f"{max(vers)[1]}/idle.png"
    if Path(p).exists():
        return FileResponse(p, media_type="image/png",
                            headers={"Cache-Control": "no-cache"})
    raise HTTPException(status_code=404, detail="no avatar image")


# AVATAR_WEB_ANIM_V1 — animovaný idle avatar (stejný LivePortrait klip jako video
# náhled na displeji). <video loop> v sidebaru; PNG zůstává jako poster/fallback.
@app.get("/api/avatar.mp4")
def avatar_video():
    """Servíruje idle loop klip pro animovaný avatar v sidebaru."""
    import glob
    cdir = Path("data/avatar/clips")
    for f in ("hans_idleloop_00001.mp4", "hlp_d9_00001.mp4",
              "hans_talkloop_00001.mp4"):
        p = cdir / f
        if p.exists():
            return FileResponse(str(p), media_type="video/mp4",
                                headers={"Cache-Control": "no-cache"})
    any_clips = sorted(glob.glob(str(cdir / "*.mp4")))
    if any_clips:
        return FileResponse(any_clips[0], media_type="video/mp4",
                            headers={"Cache-Control": "no-cache"})
    raise HTTPException(status_code=404, detail="no avatar video")


# ── HANS_ART_V1 — galerie obrazů, které Hans namaloval ke knihám ─────────────
@app.get("/api/art/list")
async def art_list(limit: int = 12):
    """Poslední obrazy (diary event 'artwork'): {ts, date, book, caption, file}."""
    import os as _os
    out = []
    try:
        conn = sqlite3.connect(str(DIARY_PATH))
        rows = conn.execute(
            "SELECT ts, title, note, data FROM diary WHERE event_type='artwork' "
            "ORDER BY ts DESC LIMIT ?", (int(limit),)).fetchall()
        conn.close()
        _SRC_LABEL = {"dream": "sen", "day": "den a nálada", "home": "domov",
                      "home_photo": "domov", "book": "kniha", "": "kniha"}
        for ts, title, note, data in rows:
            f = None
            src = ""
            try:
                d = json.loads(data or "{}") or {}
                f = _os.path.basename(d.get("path", ""))
                src = d.get("source", "")
            except Exception:
                pass
            if not f:
                continue
            out.append({
                "ts": ts,
                "date": datetime.fromtimestamp(ts).strftime("%-d.%-m.%Y"),
                "book": title or "kniha",
                "caption": note or "",
                "file": f,
                "source": src,
                "kind": _SRC_LABEL.get(src, "kniha"),
            })
    except Exception as e:
        print(f"[web_admin] art_list error: {e}")
    return out


@app.get("/api/art/image")
async def art_image(f: str):
    """Servíruje konkrétní obraz z data/hans_art/ (jen basename = bez path traversal)."""
    import os as _os
    name = _os.path.basename(f)
    p = Path("data/hans_art") / name
    if p.exists() and p.suffix.lower() == ".png":
        return FileResponse(str(p), media_type="image/png",
                            headers={"Cache-Control": "max-age=3600"})
    raise HTTPException(status_code=404, detail="no art")


@app.get("/api/place")
async def get_place():
    """HANS_PLACE_V1 — model domova „kde jsem": syntetizovaný model + fakta +
    nejnovější obraz domova (pro dashboard kartu)."""
    out = {"home_model": "", "facts": [], "image": None}
    _LBL = {"room": "Místnost", "window": "Okno", "door": "Dveře",
            "neighbor": "Vedle", "layout": "Rozložení", "note": "Pozn."}
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % DIARY_PATH, uri=True)
        row = conn.execute(
            "SELECT content FROM place_facts WHERE category='home_model' "
            "ORDER BY updated_ts DESC LIMIT 1").fetchone()
        if row:
            out["home_model"] = (row[0] or "").strip()
        for cat, content in conn.execute(
                "SELECT category, content FROM place_facts WHERE category IN "
                "('room','layout','window','door','neighbor','note') "
                "ORDER BY category, id").fetchall():
            out["facts"].append({"label": _LBL.get(cat, cat), "text": content})
        # nejnovější obraz domova
        for (data,) in conn.execute(
                "SELECT data FROM diary WHERE event_type='artwork' "
                "AND data LIKE '%\"source\": \"home%' ORDER BY ts DESC LIMIT 1").fetchall():
            try:
                import os as _os
                out["image"] = _os.path.basename((json.loads(data or "{}") or {}).get("path", "")) or None
            except Exception:
                pass
        conn.close()
    except Exception as e:
        print(f"[web_admin] get_place error: {e}")
    return out


@app.get("/api/musings")
async def get_musings(limit: int = 6):
    """Co Hans napsal sám pro sebe (diary event 'musing')."""
    out = []
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % DIARY_PATH, uri=True)
        rows = conn.execute(
            "SELECT ts, note FROM diary WHERE event_type='musing' AND note<>'' "
            "ORDER BY ts DESC LIMIT ?", (int(limit),)).fetchall()
        conn.close()
        for ts, note in rows:
            out.append({
                "date": datetime.fromtimestamp(ts).strftime("%-d.%-m.%Y"),
                "text": note,
            })
    except Exception as e:
        print(f"[web_admin] get_musings error: {e}")
    return out


# ── AVATAR_WEB_MIRROR_V1 — zrcadlení Pi preview stavu (mód + klip) ────────────
@app.get("/api/avatar/state")
def avatar_state():
    """Aktuální stav avatara z Pi (zapisuje display_renderer.draw_avatar).
    {mode: talk|extra|idleanim|idle, clip: <basename.mp4>|null}."""
    p = Path("data/avatar/avatar_state.json")
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"mode": "idle", "clip": None}


@app.get("/api/avatar/clip")
def avatar_clip(name: str):
    """Servíruje konkrétní klip dle stavu (jen basename .mp4 z clips/)."""
    safe = Path(name).name
    if not safe.endswith(".mp4"):
        raise HTTPException(status_code=400, detail="invalid clip")
    p = Path("data/avatar/clips") / safe
    if p.exists():
        return FileResponse(str(p), media_type="video/mp4",
                            headers={"Cache-Control": "max-age=3600"})
    raise HTTPException(status_code=404, detail="no clip")


@app.get("/api/status")
async def get_status():
    cfg = load_config()
    status = {
        "config_ok":  "_error" not in cfg,
        "faces_count": 0,
        "last_diary":  None,
        "last_thought": None,
        "last_read":   None,
        "model":       cfg.get("openwebui_direct", {}).get("model", "?"),
        "ollama_url":  cfg.get("openwebui_chat", {}).get("base_url", "?"),
        "cpu_temp":    0.0,
        "cpu_load":    0.0,
        "ram_pct":     0.0,
        "ollama_ok":   False,
    }

    # CPU teplota
    _cpu_temp = 0.0
    for _tp in ["/sys/class/thermal/thermal_zone0/temp",
                "/sys/class/thermal/thermal_zone1/temp"]:
        try:
            _v = int(Path(_tp).read_text().strip())
            _cpu_temp = _v / 1000.0 if _v > 1000 else float(_v)
            break
        except Exception:
            pass
    if _cpu_temp == 0.0:
        try:
            import subprocess as _sp
            _out = _sp.check_output(["vcgencmd","measure_temp"],timeout=2,text=True)
            _cpu_temp = float(_out.strip().replace("temp=","").replace("'C",""))
        except Exception:
            pass
    status["cpu_temp"] = round(_cpu_temp, 1)

    # CPU load
    try:
        import psutil as _ps
        status["cpu_load"] = round(_ps.cpu_percent(interval=0.3), 1)
    except Exception:
        try:
            import os as _os
            _load = float(Path("/proc/loadavg").read_text().split()[0])
            status["cpu_load"] = round(min(100.0, _load/(_os.cpu_count() or 4)*100), 1)
        except Exception:
            pass

    # RAM
    try:
        import psutil as _ps
        status["ram_pct"] = round(_ps.virtual_memory().percent, 1)
    except Exception:
        try:
            _mi = {}
            for _ln in Path("/proc/meminfo").read_text().splitlines():
                _pt = _ln.split()
                if len(_pt) >= 2:
                    _mi[_pt[0].rstrip(":")] = int(_pt[1])
            _tot = _mi.get("MemTotal", 1)
            _av  = _mi.get("MemAvailable", _tot)
            status["ram_pct"] = round((1 - _av/_tot)*100, 1)
        except Exception:
            pass

    # WEBADMIN_V2 — nálada JEDNOSLOVNĚ z posledního přechodu v system.log
    # (formát: "hans_mood: Mood: <z> → <na> (...)"); čteme jen konec logu.
    status["mood"] = None
    try:
        _logp = Path("data/system.log")
        if _logp.exists():
            import re as _re
            with open(_logp, "rb") as _f:
                _f.seek(0, 2); _sz = _f.tell(); _f.seek(max(0, _sz - 200000))
                _txt = _f.read().decode("utf-8", "ignore")
            _ms = _re.findall(r"hans_mood: Mood: \S+ → (\S+)", _txt)
            if _ms:
                status["mood"] = _ms[-1]
    except Exception:
        pass

    # Ollama ping
    try:
        import requests as _rq
        _url = cfg.get("openwebui_chat",{}).get("base_url","http://localhost:11434")
        _rr  = _rq.get(f"{_url}/api/tags", timeout=3)
        status["ollama_ok"] = _rr.status_code == 200
    except Exception:
        status["ollama_ok"] = False

    # WEBADMIN_V2 — aktivní model NAHRANÝ V PAMĚTI (Ollama /api/ps), ne statický config
    try:
        import requests as _rq
        _url = cfg.get("openwebui_chat",{}).get("base_url","http://localhost:11434")
        _ps  = _rq.get(f"{_url}/api/ps", timeout=3)
        if _ps.status_code == 200:
            _names = [m.get("name") or m.get("model") for m in _ps.json().get("models", [])]
            _names = [n for n in _names if n]
            if _names:
                status["model"] = ", ".join(_names)
            else:
                status["model"] = "—"  # nic nahráno v paměti
    except Exception:
        pass

    # Počet obličejů
    if FACES_PATH.exists():
        try:
            import pickle
            with open(FACES_PATH, "rb") as f:
                db = pickle.load(f)
            status["faces_count"] = len(db)
        except Exception:
            pass

    # Poslední záznamy z deníku
    if DIARY_PATH.exists():
        try:
            import sqlite3 as _sq
            conn = _sq.connect(str(DIARY_PATH))
            row = conn.execute(
                "SELECT datetime(ts,'unixepoch','localtime'), event_type, note "
                "FROM diary ORDER BY ts DESC LIMIT 1").fetchone()
            if row:
                status["last_diary"] = {"dt": row[0], "type": row[1],
                                        "note": (row[2] or "")[:100]}
            row = conn.execute(
                "SELECT note FROM diary WHERE event_type='introspection' "
                "ORDER BY ts DESC LIMIT 1").fetchone()
            if row:
                status["last_thought"] = row[0]
            # WEBADMIN_V2 — naposledy četl = aktuální KNIHA (titul + autor), ne web_read
            row = conn.execute(
                "SELECT book_title, author FROM hans_library WHERE status='reading' "
                "ORDER BY started_at DESC LIMIT 1").fetchone()
            if row:
                status["last_read"] = {"title": row[0] or "", "author": row[1] or ""}
            conn.close()
        except Exception:
            pass

    return status


# ── WEBADMIN_V2 — Kodi: aktuálně hraje + plakát (booklet) ────────────────────
def _kodi_conn():
    kc = load_config().get("kodi", {}) or {}
    host = kc.get("host", "localhost"); port = int(kc.get("port", 8080))
    user = kc.get("user", ""); pw = kc.get("password", "")
    auth = (user, pw) if user else None
    return f"http://{host}:{port}", auth, bool(kc.get("enabled", True))


def _kodi_rpc(method, params=None):
    base, auth, enabled = _kodi_conn()
    if not enabled:
        return None
    import requests as _rq
    try:
        r = _rq.post(f"{base}/jsonrpc",
                     json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
                     auth=auth, timeout=3)
        if r.status_code == 200:
            return r.json().get("result")
    except Exception:
        pass
    return None


def _poster_proxy_url(art, item):
    art = art or {}
    raw = (art.get("poster") or art.get("season.poster") or art.get("thumb")
           or (item or {}).get("thumbnail") or "")
    if not raw:
        return ""
    return "/api/kodi/poster?img=" + urllib.parse.quote(raw, safe="")


@app.get("/api/kodi/playing")
async def kodi_playing():
    """Aktuálně hraný titul z Kodi vč. plakátu. {playing:false} když nic nehraje."""
    players = _kodi_rpc("Player.GetActivePlayers")
    if not players:
        return {"playing": False}
    pid = players[0].get("playerid", 1)
    res = _kodi_rpc("Player.GetItem", {
        "playerid": pid,
        "properties": ["title", "year", "genre", "showtitle", "season",
                       "episode", "plot", "art", "thumbnail"]})
    item = (res or {}).get("item", {})
    if not (item.get("title") or item.get("label")):
        return {"playing": False}
    genre = item.get("genre", "")
    if isinstance(genre, list):
        genre = ", ".join(genre)
    return {
        "playing":  True,
        "title":    item.get("title") or item.get("label"),
        "showtitle": item.get("showtitle") or "",
        "year":     item.get("year") or "",
        "genre":    genre,
        "season":   item.get("season"),
        "episode":  item.get("episode"),
        "plot":     (item.get("plot") or "")[:400],
        "poster":   _poster_proxy_url(item.get("art"), item),
    }


@app.get("/api/kodi/poster")
async def kodi_poster(img: str):
    """Proxy plakátu z Kodi /image/ — skryje creds, doplní content-type."""
    base, auth, _ = _kodi_conn()
    try:
        import requests as _rq
        from fastapi.responses import Response as _Resp
        u = f"{base}/image/" + urllib.parse.quote(img, safe="")
        r = _rq.get(u, auth=auth, timeout=8)
        if r.status_code == 200 and r.content:
            ct = r.headers.get("content-type") or ""
            if not ct.startswith("image"):
                ct = "image/jpeg"
            return _Resp(content=r.content, media_type=ct,
                         headers={"Cache-Control": "max-age=3600"})
    except Exception:
        pass
    raise HTTPException(404, "poster nedostupný")


# WEBADMIN_V2 — retroaktivní dohledání plakátu podle titulu (Kodi knihovna) + cache
_POSTER_CACHE: dict = {}


def _resolve_poster(title: str) -> str:
    if not title:
        return ""
    key = title.strip().lower()
    if key in _POSTER_CACHE:
        return _POSTER_CACHE[key]
    url = ""
    for method, coll in (("VideoLibrary.GetEpisodes", "episodes"),
                         ("VideoLibrary.GetMovies", "movies")):
        res = _kodi_rpc(method, {
            "filter": {"field": "title", "operator": "is", "value": title},
            "properties": ["art", "thumbnail"], "limits": {"end": 1}})
        items = (res or {}).get(coll, []) if res else []
        if items:
            url = _poster_proxy_url(items[0].get("art"), items[0])
            if url:
                break
    _POSTER_CACHE[key] = url
    return url


def _clean_opinion(text: str, name: str = "Hans") -> str:
    """Odstraň degenerovaný úvodní self-identity prefix („Jmenuji se Hans.",
    „Jsem Hans.", „Rozumím.") z názoru — skutečný text následuje až za ním."""
    import re as _re
    t = (text or "").strip()
    pat = _re.compile(
        r'^\s*(jmenuji se %s|jsem %s|rozum[ií]m)\s*[.!]*\s*' % (
            _re.escape(name), _re.escape(name)), _re.IGNORECASE)
    for _ in range(6):
        new = pat.sub('', t, count=1).strip()
        if new == t:
            break
        t = new
    return t


@app.get("/api/movies_seen")
async def movies_seen(limit: int = 6):
    """Filmy/epizody které Hans viděl + jeho názor + plakát (best-effort z Kodi)."""
    if not DIARY_PATH.exists():
        return []
    try:
        _pname = (load_config().get("persona", {}) or {}).get("name", "Hans")
    except Exception:
        _pname = "Hans"
    try:
        conn = sqlite3.connect(str(DIARY_PATH))
        rows = conn.execute(
            "SELECT datetime(ts,'unixepoch','localtime'), title, "
            "COALESCE(NULLIF(data,''), note) FROM diary "
            "WHERE event_type='movie_opinion' AND title<>'' "
            "ORDER BY ts DESC LIMIT ?", (limit * 4,)).fetchall()
        conn.close()
    except Exception:
        return []
    seen, out = set(), []
    for dt, title, opinion in rows:
        key = (title or "").strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append({"dt": dt, "title": title,
                    "opinion": _clean_opinion(opinion, _pname)[:240],
                    "poster": _resolve_poster(title)})
        if len(out) >= limit:
            break
    return out


# ── WEBADMIN_V2 — Chat s Hansem (most přes JSON soubor → Hans vysloví na Pi) ──
@app.post("/api/chat/send")
async def chat_send(request: Request):
    body = await request.json()
    person = (body.get("person") or "").strip() or "Uživatel"
    message = (body.get("message") or "").strip()
    if not message:
        raise HTTPException(400, "prázdná zpráva")
    import uuid
    rid = uuid.uuid4().hex[:12]
    try:
        Path("data/.web_chat_req.json").write_text(
            json.dumps({"id": rid, "person": person, "message": message,
                        "ts": time.time()}, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        raise HTTPException(500, str(e))
    return {"ok": True, "id": rid}


@app.get("/api/chat/poll")
async def chat_poll(id: str):
    p = Path("data/.web_chat_resp.json")
    if p.exists():
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            if d.get("id") == id:
                return {"ready": True, "response": d.get("response", "")}
        except Exception:
            pass
    return {"ready": False}


@app.post("/api/web_read")  # WEBADMIN_V2 — fix 404 (chyběl decorator)
async def trigger_web_read(request: Request):
    """Spustí /read URL přímo z web UI."""
    try:
        body  = await request.json()
        url   = body.get("url", "").strip()
        if not url.startswith("http"):
            raise HTTPException(400, "Neplatná URL")
        # Import a spuštění
        import sys
        sys.path.insert(0, str(Path(__file__).parent))
        from scripts.web_reader import WebReader
        cfg = load_config()
        reader = WebReader(cfg)
        import threading
        def _read():
            result = reader.fetch_url(url, topic="manual")
            if result and DIARY_PATH.exists():
                conn = sqlite3.connect(str(DIARY_PATH))
                conn.execute(
                    "INSERT INTO diary (ts, event_type, title, note) VALUES (?,?,?,?)",
                    (time.time(), "web_read", result.title[:120],
                     f"[manual] {result.summary}"))
                conn.commit()
                conn.close()
        threading.Thread(target=_read, daemon=True).start()
        return {"ok": True, "message": f"Čtu: {url}"}
    except Exception as e:
        raise HTTPException(500, str(e))


if __name__ == "__main__":
    print("Hans Web Admin — http://0.0.0.0:7860")
    uvicorn.run(app, host="0.0.0.0", port=7860, log_level="warning")