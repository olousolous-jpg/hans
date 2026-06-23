#!/usr/bin/env python3
"""
HANS_PLACE_V1 — Smysl pro místo / „Kde jsem" (frontier #4).

Hans má dnes jen ÚZKÝ pohled kamery (room_observer) + počasí + čas, ale žádný
KONTINUÁLNÍ strukturovaný model PROSTORU, ve kterém je. Tento modul dává Hansovi
groundovanou ZNALOST místa (ne 3D, ne navigaci — Hans se nehýbe):

  1. FAKTA OD UŽIVATELE (půdorys v textu): místnost, okna (orientace → co je za nimi),
     dveře (kam vedou), sousední místnosti, rozložení. Vstup přes chat `/misto`.
  2. MENTÁLNÍ MAPA Z ŠIRŠÍ FOTKY: uživatel nechá širší foto(y) celé místnosti
     (víc úhlů než pevná kamera) v `data/room_photos/` → qwen-VL z nich odvodí
     rozložení/okna/dveře/objekty NAD RÁMEC úzkého pohledu kamery. Idempotentní
     (sidecar `.map.json`), VRAM tanec jako room_observer.

KLÍČOVÉ RÁMOVÁNÍ (řeší konfabulaci): faktická vrstva je GROUNDOVANÁ (fakta od
uživatele + co reálně vidí na fotce + počasí za oknem). Subjektivní „představa
domova" (SDXL render) je oddělená feature (Fáze 3), sem nepatří.

Tabulka `place_facts` v hans_diary.db:
  id, category, content, source, created_ts, updated_ts
  category: room | window | door | neighbor | layout | note  (fakta od uživatele)
            mental_map                                        (z fotky přes qwen-VL)

API:
  store = PlaceStore(config, diary_db_path)
  store.add_fact(category, content, source='user') -> id          # dedup
  store.set_singleton(category, content, source) -> id            # room: 1 řádek
  store.get_facts(category=None) -> [dict]
  store.remove_fact(id) -> bool
  store.get_mental_map() -> str | ''                              # nejnovější
  store.get_context_string(weather_str=None, phase_label=None) -> str
  store.ingest_photos(config) -> int                             # drop-folder krok
"""
from __future__ import annotations

import base64
import logging
import os
import re
import sqlite3
import time

_log = logging.getLogger("hans_place")

PHOTO_DIR = os.path.join("data", "room_photos")
_IMG_EXT = (".jpg", ".jpeg", ".png", ".webp")
_WS = re.compile(r"\s+")

# Kategorie, které smí mít jen JEDEN aktivní řádek (nahrazují se)
_SINGLETON = {"room", "home_model"}
# Kategorie fakt od uživatele (pořadí = pořadí vykreslení v kontextu)
_USER_CATS = ("room", "window", "door", "neighbor", "layout", "note")

_CAT_LABEL = {
    "room":     "Místnost",
    "window":   "Okno",
    "door":     "Dveře",
    "neighbor": "Vedle",
    "layout":   "Rozložení",
    "note":     "Pozn.",
}

def _downscaled_b64(path: str, max_side: int = 1024, quality: int = 85) -> str | None:
    """Načti fotku, zmenši delší stranu na max_side a vrať JPEG base64.
    Velké 3MB fotky v plném rozlišení qwen-VL zakóduje pomalu (timeout) — zmenšení
    to zásadně zrychlí bez ztráty užitečné informace o prostoru. Fallback: raw."""
    try:
        import cv2
        img = cv2.imread(path)
        if img is None:
            raise ValueError("imread None")
        h, w = img.shape[:2]
        scale = max_side / float(max(h, w))
        if scale < 1.0:
            img = cv2.resize(img, (int(w * scale), int(h * scale)),
                             interpolation=cv2.INTER_AREA)
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, quality])
        if not ok:
            raise ValueError("imencode failed")
        return base64.b64encode(buf.tobytes()).decode("utf-8")
    except Exception as e:
        _log.warning("place: downscale %s selhal (%s) — posílám raw", path, e)
        try:
            with open(path, "rb") as fh:
                return base64.b64encode(fh.read()).decode("utf-8")
        except Exception:
            return None


def _caption_from_filename(fn: str) -> str:
    """Z názvu souboru udělej popisek (uživatel pojmenovává fotky podle toho,
    co na nich je, např. 'pohled od okna.jpg')."""
    base = os.path.splitext(os.path.basename(fn))[0]
    base = base.replace("_", " ").replace("-", " ").strip()
    base = _WS.sub(" ", base)
    return base


def _map_prompt(caption: str) -> str:
    head = (
        "Toto je širší fotka místnosti, kde žije domácí asistent (vidí jinak jen "
        "úzký výřez z pevné kamery). ")
    if caption:
        head += ("Uživatel fotku pojmenoval: „%s\" — ber to jako jistý fakt o "
                 "tom, co je na ní a kam co vede. " % caption)
    head += (
        "Popiš česky ve 2–3 větách, co na fotce REÁLNĚ vidíš, ať si asistent "
        "udělá představu o prostoru: hlavní nábytek a objekty, kde jsou okna/dveře "
        "a celková atmosféra. NEVYMÝŠLEJ nic, co na fotce ani v názvu není.")
    return head


_MAP_PROMPT = (
    "Toto je širší fotka místnosti, kde žije domácí asistent (vidí jinak jen úzký "
    "výřez z pevné kamery). Popiš česky, co na fotce REÁLNĚ vidíš, abys mu pomohl "
    "udělat si představu o prostoru. Zaměř se na: rozložení místnosti, kde jsou "
    "OKNA (a co je za nimi vidět), kde jsou DVEŘE (kam asi vedou), hlavní nábytek "
    "a objekty, celkovou atmosféru. NEVYMÝŠLEJ nic, co na fotce není. 3–5 vět."
)


def _norm(s: str) -> str:
    return _WS.sub(" ", (s or "").strip().lower())


class PlaceStore:
    def __init__(self, config: dict, diary_db_path: str):
        self.config = config or {}
        self._diary_path = diary_db_path
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self._diary_path) as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS place_facts (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    category    TEXT NOT NULL,
                    content     TEXT NOT NULL,
                    source      TEXT,
                    created_ts  REAL,
                    updated_ts  REAL
                )
            """)
            db.commit()

    def _connect(self):
        conn = sqlite3.connect(self._diary_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        return conn

    # ── zápis ──────────────────────────────────────────────────────────
    def add_fact(self, category: str, content: str, source: str = "user") -> int | None:
        category = (category or "").strip().lower()
        content = (content or "").strip()
        if not category or not content:
            return None
        if category in _SINGLETON:
            return self.set_singleton(category, content, source)
        now = time.time()
        norm = _norm(content)
        with self._connect() as conn:
            # dedup proti existujícím ve stejné kategorii
            for row in conn.execute(
                    "SELECT id, content FROM place_facts WHERE category=?", (category,)):
                if _norm(row["content"]) == norm:
                    conn.execute("UPDATE place_facts SET updated_ts=?, source=? WHERE id=?",
                                 (now, source, row["id"]))
                    conn.commit()
                    return row["id"]
            cur = conn.execute(
                "INSERT INTO place_facts (category, content, source, created_ts, updated_ts) "
                "VALUES (?,?,?,?,?)", (category, content, source, now, now))
            conn.commit()
            return cur.lastrowid

    def set_singleton(self, category: str, content: str, source: str = "user") -> int | None:
        category = (category or "").strip().lower()
        content = (content or "").strip()
        if not category or not content:
            return None
        now = time.time()
        with self._connect() as conn:
            conn.execute("DELETE FROM place_facts WHERE category=?", (category,))
            cur = conn.execute(
                "INSERT INTO place_facts (category, content, source, created_ts, updated_ts) "
                "VALUES (?,?,?,?,?)", (category, content, source, now, now))
            conn.commit()
            return cur.lastrowid

    def upsert_mental_map(self, source: str, content: str) -> int | None:
        """Jedna fotka = jeden mental_map řádek (klíč = source/název fotky).
        Re-ingest po obohacení qwenem nahradí caption-only obsah, nezduplikuje."""
        content = (content or "").strip()
        if not content:
            return None
        now = time.time()
        with self._connect() as conn:
            conn.execute("DELETE FROM place_facts WHERE category='mental_map' "
                         "AND source=?", (source,))
            cur = conn.execute(
                "INSERT INTO place_facts (category, content, source, created_ts, updated_ts) "
                "VALUES ('mental_map',?,?,?,?)", (content, source, now, now))
            conn.commit()
            return cur.lastrowid

    def remove_fact(self, fact_id: int) -> bool:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM place_facts WHERE id=?", (fact_id,))
            conn.commit()
            return cur.rowcount > 0

    # ── čtení ──────────────────────────────────────────────────────────
    def get_facts(self, category: str | None = None) -> list[dict]:
        with self._connect() as conn:
            if category:
                rows = conn.execute(
                    "SELECT * FROM place_facts WHERE category=? ORDER BY id",
                    (category.strip().lower(),)).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM place_facts ORDER BY category, id").fetchall()
        return [dict(r) for r in rows]

    def get_mental_map(self) -> str:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT content FROM place_facts WHERE category='mental_map' "
                "ORDER BY updated_ts DESC LIMIT 1").fetchone()
        return (row["content"] if row else "").strip()

    def get_mental_maps(self) -> list[str]:
        """Všechny pohledy z fotek (každá fotka = jeden pohled), v pořadí přidání."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT content FROM place_facts WHERE category='mental_map' "
                "ORDER BY id").fetchall()
        return [(r["content"] or "").strip() for r in rows if (r["content"] or "").strip()]

    def get_home_model(self) -> str:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT content FROM place_facts WHERE category='home_model' "
                "ORDER BY updated_ts DESC LIMIT 1").fetchone()
        return (row["content"] if row else "").strip()

    def has_any(self) -> bool:
        with self._connect() as conn:
            row = conn.execute("SELECT 1 FROM place_facts LIMIT 1").fetchone()
        return row is not None

    def synthesize_home_model(self, config: dict | None = None) -> str | None:
        """Z jednotlivých pohledů (fakta od uživatele + mentální mapy z fotek)
        sestaví JEDEN kompaktní koherentní odstavec — Hansovu vlastní představu
        domova v 1. osobě, striktně groundovanou (nic nevymýšlet). Uloží jako
        singleton 'home_model'. Base model (anti-konfabulace), VRAM tanec.
        Vrací text nebo None (žádný materiál / LLM dole)."""
        cfg = config or self.config or {}
        facts = self.get_facts()
        by_cat: dict[str, list[str]] = {}
        for f in facts:
            if f["category"] in ("home_model",):
                continue
            by_cat.setdefault(f["category"], []).append(f["content"])
        material: list[str] = []
        for cat in _USER_CATS:
            for c in by_cat.get(cat, []):
                material.append(f"{_CAT_LABEL.get(cat, cat)}: {c}")
        for m in by_cat.get("mental_map", []):
            material.append(f"Pohled: {m}")
        if not material:
            return None

        model = (cfg.get("place", {}) or {}).get("synth_model") \
            or cfg.get("evening_reflection", {}).get("model") \
            or "jobautomation/OpenEuroLLM-Czech:latest"
        ollama_url = cfg.get("openwebui_chat", {}).get("base_url",
                                                       "http://127.0.0.1:11434")
        timeout = int((cfg.get("place", {}) or {}).get("synth_timeout", 120))
        try:
            from scripts.hans_persona import persona_name
            pname = persona_name(cfg)
        except Exception:
            pname = "Hans"

        prompt = (
            "Jsi %s. Níže jsou fakta o tvém domově a jednotlivé pohledy z fotek "
            "(co je reálně na nich vidět). Sestav z nich JEDEN souvislý odstavec "
            "(4–6 vět) v 1. osobě — svou vlastní představu místa, kde žiješ: jak je "
            "rozložené, kde jsou okna a co je za nimi, kam vedou dveře, kde máš své "
            "místo. Piš věcně a klidně, jako bys popisoval svůj domov. Drž se POUZE "
            "informací níže — NIC si nevymýšlej a nepřidávej. Nepiš nadpis, jen ten "
            "odstavec.\n\nPODKLADY:\n- %s" % (pname, "\n- ".join(material)))

        from scripts.ollama_client import ollama_generate
        try:
            from scripts.avatar_render import (_ollama_loaded, _ollama_unload,
                                               _ollama_warm)
            vram = True
        except Exception:
            vram = False
        if vram:
            _ollama_unload(cfg, _ollama_loaded(cfg))
        try:
            text = ollama_generate(model, prompt, ollama_url=ollama_url,
                                   keep_alive=0, timeout=timeout)
        finally:
            if vram:
                dlg = (cfg.get("models", {}) or {}).get("dialog", "hans-czech:latest")
                _ollama_warm(cfg, dlg)
        text = (text or "").strip().strip('"')
        if not text:
            _log.warning("place: syntéza modelu domova prázdná (LLM dole?)")
            return None
        self.set_singleton("home_model", text, source="synthesis")
        _log.info("place: model domova syntetizován (%d zn)", len(text))
        return text

    # ── kontext do promptu ─────────────────────────────────────────────
    def get_context_string(self, weather_str: str | None = None,
                           phase_label: str | None = None) -> str:
        """Groundovaný blok 'Kde jsem' pro LLM kontext. '' když nic není."""
        facts = self.get_facts()
        if not facts:
            return ""
        by_cat: dict[str, list[str]] = {}
        for f in facts:
            by_cat.setdefault(f["category"], []).append(f["content"])

        lines: list[str] = []
        for cat in _USER_CATS:
            for content in by_cat.get(cat, []):
                lines.append(f"- {_CAT_LABEL.get(cat, cat)}: {content}")

        # Preferuj syntetizovaný kompaktní model domova (1 odstavec); jinak
        # vypiš jednotlivé pohledy z fotek (fallback, dokud syntéza neproběhla).
        home = (by_cat.get("home_model") or [None])[-1]
        if home:
            lines.append(f"- Jak to tu u mě doma vypadá: {home}")
        else:
            mmaps = by_cat.get("mental_map") or []
            if mmaps:
                lines.append("- Jak to tu vypadá (z fotek, jednotlivé pohledy):")
                for m in mmaps:
                    lines.append(f"  · {m}")

        # živé groundování: počasí = co je za oknem
        has_window = bool(by_cat.get("window"))
        if weather_str:
            if has_window:
                lines.append(f"- Za oknem právě: {weather_str}")
            else:
                lines.append(f"- Venku právě: {weather_str}")

        if not lines:
            return ""
        head = "Kde jsem (můj model domova — fakta, ber je jako jistá):"
        return head + "\n" + "\n".join(lines)

    # ── ingest fotek (drop-folder → qwen-VL mentální mapa) ──────────────
    def ingest_photos(self, config: dict | None = None,
                      photo_dir: str | None = None) -> int:
        """Projde data/room_photos/, nezpracované fotky pošle na qwen-VL,
        výsledek uloží jako mental_map. Idempotentní (sidecar .map.json).
        VRAM tanec jako room_observer. Vrací počet nově zpracovaných."""
        cfg = config or self.config or {}
        if photo_dir is None:
            photo_dir = (cfg.get("place", {}) or {}).get("photo_dir") or PHOTO_DIR
        if not os.path.isdir(photo_dir):
            return 0
        try:
            files = sorted(f for f in os.listdir(photo_dir)
                          if f.lower().endswith(_IMG_EXT))
        except Exception as e:
            _log.error("ingest_photos listdir: %s", e)
            return 0

        pending = []
        for fn in files:
            sidecar = os.path.join(photo_dir, fn + ".map.json")
            if not os.path.exists(sidecar):
                pending.append(fn)
        if not pending:
            return 0

        ro_cfg = cfg.get("room_observer", {}) or {}
        model = (cfg.get("place", {}) or {}).get("vision_model") \
            or ro_cfg.get("model", "qwen2.5vl:7b")
        ollama_url = cfg.get("openwebui_chat", {}).get("base_url",
                                                       "http://127.0.0.1:11434")
        timeout = int((cfg.get("place", {}) or {}).get("vision_timeout", 120))

        from scripts.ollama_client import ollama_generate
        try:
            from scripts.avatar_render import (_ollama_loaded, _ollama_unload,
                                               _ollama_warm)
            vram = True
        except Exception:
            vram = False

        done = 0
        if vram:
            _ollama_unload(cfg, _ollama_loaded(cfg))
        try:
            for fn in pending:
                path = os.path.join(photo_dir, fn)
                b64 = _downscaled_b64(path)
                if b64 is None:
                    _log.error("place: nelze číst/zmenšit %s", fn)
                    continue
                caption = _caption_from_filename(fn)
                _log.info("place: zpracovávám fotku místnosti %s (popisek: %s)",
                          fn, caption)
                # keep_alive="5m": qwen-VL zůstane teplý přes celou dávku
                # (8 studených loadů by každý vypršelo) — uvolní se po loopu.
                desc = ollama_generate(
                    model, _map_prompt(caption), images=[b64],
                    ollama_url=ollama_url, keep_alive="5m", timeout=timeout)
                desc = (desc or "").strip()
                # Popisek z názvu = groundovaný fakt (i kdyby qwen selhal).
                # Obsah = popisek + vizuální detail z qwen.
                if caption and desc:
                    content = f"{caption} — {desc}"
                elif desc:
                    content = desc
                elif caption:
                    content = caption
                else:
                    _log.warning("place: prázdný popis i název pro %s — přeskakuji", fn)
                    continue
                self.upsert_mental_map(fn, content)
                _log.info("place: mentální mapa z %s → %s", fn, content[:80])
                # sidecar = zpracováno (idempotence)
                try:
                    import json
                    with open(os.path.join(photo_dir, fn + ".map.json"), "w") as sf:
                        json.dump({"ts": time.time(), "caption": caption,
                                   "desc": desc, "content": content}, sf,
                                  ensure_ascii=False)
                except Exception as e:
                    _log.error("place: sidecar %s: %s", fn, e)
                done += 1
        finally:
            if vram:
                # qwen-VL byl teplý (keep_alive=5m) přes dávku → teď ho výslovně
                # uvolni, jinak by visel vedle hans-czech (14.7+10.8G > VRAM).
                try:
                    _ollama_unload(cfg, model)
                except Exception:
                    pass
                dlg = (cfg.get("models", {}) or {}).get("dialog", "hans-czech:latest")
                _ollama_warm(cfg, dlg)
        # Po obohacení přegeneruj kompaktní model domova (vlastní VRAM tanec).
        if done and self.get_mental_maps():
            try:
                self.synthesize_home_model(cfg)
            except Exception as _se:
                _log.warning("place: syntéza modelu domova selhala: %s", _se)
        return done


if __name__ == "__main__":
    import json
    import sys
    logging.basicConfig(level=logging.INFO)
    cfg = json.load(open("config.json")) if os.path.exists("config.json") else {}
    db = cfg.get("hans_idle", {}).get("diary_db", "data/hans_diary.db")
    store = PlaceStore(cfg, db)
    if len(sys.argv) > 1 and sys.argv[1] == "ingest":
        print("zpracováno fotek:", store.ingest_photos(cfg))
    elif len(sys.argv) > 1 and sys.argv[1] == "synth":
        print("model domova:", store.synthesize_home_model(cfg))
    print("--- fakta ---")
    for f in store.get_facts():
        print(f["id"], f["category"], ":", f["content"][:80])
    print("--- kontext ---")
    print(store.get_context_string(weather_str="zataženo, 12°C"))
