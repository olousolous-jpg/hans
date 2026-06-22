"""
Surroundings Database
SQLite store for:
  - Detected objects (from YOLOv8s during scan mode)
  - Known persons (from config known_persons + face DB)

LLM context builder generates a summary injected into every system prompt.
"""

import sqlite3
import threading
import json
import time
from datetime import datetime
from pathlib import Path

# region agent log
from scripts.debug_log import dbg as _dbg
# endregion


# COCO classes — persons excluded (index 0)
COCO_CLASSES = {
    1: "bicycle", 2: "car", 3: "motorcycle", 4: "airplane", 5: "bus",
    6: "train", 7: "truck", 8: "boat", 9: "traffic light", 10: "fire hydrant",
    11: "stop sign", 12: "parking meter", 13: "bench", 14: "bird", 15: "cat",
    16: "dog", 17: "horse", 18: "sheep", 19: "cow", 20: "elephant",
    21: "bear", 22: "zebra", 23: "giraffe", 24: "backpack", 25: "umbrella",
    26: "handbag", 27: "tie", 28: "suitcase", 29: "frisbee", 30: "skis",
    31: "snowboard", 32: "sports ball", 33: "kite", 34: "baseball bat",
    35: "baseball glove", 36: "skateboard", 37: "surfboard", 38: "tennis racket",
    39: "bottle", 40: "wine glass", 41: "cup", 42: "fork", 43: "knife",
    44: "spoon", 45: "bowl", 46: "banana", 47: "apple", 48: "sandwich",
    49: "orange", 50: "broccoli", 51: "carrot", 52: "hot dog", 53: "pizza",
    54: "donut", 55: "cake", 56: "chair", 57: "couch", 58: "potted plant",
    59: "bed", 60: "dining table", 61: "toilet", 62: "tv", 63: "laptop",
    64: "mouse", 65: "remote", 66: "keyboard", 67: "cell phone",
    68: "microwave", 69: "oven", 70: "toaster", 71: "sink", 72: "refrigerator",
    73: "book", 74: "clock", 75: "vase", 76: "scissors", 77: "teddy bear",
    78: "hair drier", 79: "toothbrush",
}

# Czech translations for LLM context
COCO_CZ = {
    "bicycle": "jízdní kolo", "car": "auto", "motorcycle": "motorka",
    "airplane": "letadlo", "bus": "autobus", "train": "vlak",
    "truck": "nákladní auto", "boat": "loď", "traffic light": "semafor",
    "fire hydrant": "hydrant", "stop sign": "stopka", "bench": "lavička",
    "bird": "pták", "cat": "kočka", "dog": "pes", "horse": "kůň",
    "backpack": "batoh", "umbrella": "deštník", "handbag": "kabelka",
    "tie": "kravata", "suitcase": "kufr", "bottle": "láhev",
    "wine glass": "sklenička", "cup": "hrnek", "fork": "vidlička",
    "knife": "nůž", "spoon": "lžíce", "bowl": "miska", "banana": "banán",
    "apple": "jablko", "sandwich": "sandwich", "orange": "pomeranč",
    "pizza": "pizza", "donut": "donut", "cake": "dort", "chair": "židle",
    "couch": "pohovka", "potted plant": "pokojová rostlina", "bed": "postel",
    "dining table": "jídelní stůl", "toilet": "toaleta", "tv": "televizor",
    "laptop": "notebook", "mouse": "myš", "remote": "dálkový ovladač",
    "keyboard": "klávesnice", "cell phone": "mobilní telefon",
    "microwave": "mikrovlnka", "oven": "trouba", "toaster": "toustovač",
    "sink": "dřez", "refrigerator": "lednice", "book": "kniha",
    "clock": "hodiny", "vase": "váza", "scissors": "nůžky",
    "teddy bear": "plyšový medvěd", "hair drier": "fén",
    "toothbrush": "zubní kartáček",
}


def _apply_remapping(class_name: str, confidence: float,
                     box: dict, config: dict) -> str | None:
    """
    Apply object_remapping and object_detection filters from config.
    Returns remapped name, or None if the detection should be ignored.
    """
    det_cfg  = config.get("object_detection", {})
    remap    = config.get("object_remapping", {})

    # Confidence filter
    min_conf = float(det_cfg.get("min_confidence", 0.35))
    if confidence < min_conf:
        return None

    # Box area filter (normalized 0-1)
    min_area = float(det_cfg.get("min_box_area", 0.0))
    if min_area > 0 and box:
        w = box.get("x2", 1) - box.get("x1", 0)
        h = box.get("y2", 1) - box.get("y1", 0)
        if w * h < min_area:
            return None

    # Remapping — null means ignore
    if class_name in remap:
        mapped = remap[class_name]
        if mapped is None:
            return None          # explicitly ignored
        return str(mapped)       # renamed

    return class_name            # unchanged


# # p3_surroundings_cleaned
class SurroundingsDB:

    def __init__(self, config: dict):
        self.config   = config
        db_path       = config.get("surroundings", {}).get(
            "db_path", "data/surroundings.db")
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn    = sqlite3.connect(db_path, check_same_thread=False)
        self._db_lock = threading.Lock()
        self._init_schema()
        self._sync_persons()
        print(f"[SurroundingsDB] Ready — {db_path}")
        remap = config.get("object_remapping", {})
        active = {k: v for k, v in remap.items() if not k.startswith("_")}
        ignored = [k for k, v in active.items() if v is None]
        renamed = {k: v for k, v in active.items() if v is not None}
        if renamed:
            print(f"[SurroundingsDB] Remapped: {renamed}")
        if ignored:
            print(f"[SurroundingsDB] Ignored classes: {ignored}")

    # ── Schema ────────────────────────────────────────────────────────────────

    def _init_schema(self):
        c = self._conn
        c.execute("""
            CREATE TABLE IF NOT EXISTS objects (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                class_id    INTEGER NOT NULL,
                class_name  TEXT    NOT NULL,
                confidence  REAL    NOT NULL,
                pan_angle   REAL,
                seen_at     REAL    NOT NULL,
                seen_count  INTEGER DEFAULT 1
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS persons (
                name        TEXT PRIMARY KEY,
                gender      TEXT DEFAULT '',
                notes       TEXT DEFAULT '',
                updated_at  REAL NOT NULL
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_objects_seen ON objects(seen_at)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_objects_class ON objects(class_name)")
        c.commit()

    # ── Persons sync ──────────────────────────────────────────────────────────

    def _sync_persons(self):
        known = self.config.get("known_persons", {})
        now   = time.time()
        for name, data in known.items():
            if isinstance(data, dict):
                gender = data.get("gender", "")
                notes  = data.get("notes", "")
            else:
                gender, notes = "", str(data)
            self._conn.execute("""
                INSERT INTO persons (name, gender, notes, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    gender=excluded.gender,
                    notes=excluded.notes,
                    updated_at=excluded.updated_at
            """, (name, gender, notes, now))
        self._conn.commit()
        if known:
            print(f"[SurroundingsDB] Synced {len(known)} person(s) from config")

    def sync_persons(self):
        self._sync_persons()

    def add_person(self, name: str, gender: str = "", notes: str = ""):
        self._conn.execute("""
            INSERT INTO persons (name, gender, notes, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                gender=excluded.gender,
                notes=excluded.notes,
                updated_at=excluded.updated_at
        """, (name, gender, notes, time.time()))
        self._conn.commit()

    def get_persons(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT name, gender, notes FROM persons ORDER BY name"
        ).fetchall()
        return [{"name": r[0], "gender": r[1], "notes": r[2]} for r in rows]

    # ── Object recording ──────────────────────────────────────────────────────

    def record_objects(self, detections: list[dict], pan_angle: float = 0.0):
        """
        Record a batch of detections from one scan frame.
        Applies remapping and filtering before storing.
        detections: [{"class_id": int, "class_name": str, "confidence": float,
                       "x1": float, "y1": float, "x2": float, "y2": float}]
        """
        now = time.time()
        # region agent log
        raw_n = len(detections or [])
        kept = 0
        ignored = 0
        remapped = 0
        # endregion
        for det in detections:
            cid   = det["class_id"]
            cname = det["class_name"]
            conf  = det["confidence"]

            if cid == 0:   # skip person class
                # region agent log
                ignored += 1
                # endregion
                continue

            # Apply remapping + filters
            mapped_name = _apply_remapping(cname, conf, det, self.config)
            if mapped_name is None:
                # region agent log
                ignored += 1
                # endregion
                continue   # filtered out or ignored
            # region agent log
            kept += 1
            if mapped_name != cname:
                remapped += 1
            # endregion

            # Check if seen recently (within 30 min window)
            existing = self._conn.execute("""
                SELECT id, seen_count FROM objects
                WHERE class_name = ? AND seen_at > ?
                ORDER BY seen_at DESC LIMIT 1
            """, (mapped_name, now - 1800)).fetchone()

            if existing:
                self._conn.execute("""
                    UPDATE objects SET
                        seen_at    = ?,
                        confidence = MAX(confidence, ?),
                        pan_angle  = ?,
                        seen_count = seen_count + 1
                    WHERE id = ?
                """, (now, conf, pan_angle, existing[0]))
            else:
                self._conn.execute("""
                    INSERT INTO objects
                        (class_id, class_name, confidence, pan_angle, seen_at)
                    VALUES (?, ?, ?, ?, ?)
                """, (cid, mapped_name, conf, pan_angle, now))

        with self._db_lock:
            self._conn.commit()

        # region agent log
        try:
            _dbg(
                location="surroundings_db.py:record_objects",
                message="Recorded objects (after filters/remap)",
                data={
                    "raw_n": raw_n,
                    "kept_n": kept,
                    "ignored_n": ignored,
                    "remapped_n": remapped,
                    "pan_angle": float(pan_angle),
                },
            )
        except Exception:
            pass
        # endregion

    def get_recent_objects(self, max_age_s: int = 1800) -> list[dict]:
        cutoff = time.time() - max_age_s
        with self._db_lock:
            rows = self._conn.execute("""
                SELECT class_name, confidence, pan_angle, seen_at, seen_count
                FROM objects
                WHERE seen_at > ?
                GROUP BY class_name
                HAVING MAX(confidence)
                ORDER BY seen_count DESC, confidence DESC
            """, (cutoff,)).fetchall()
        return [
            {"class_name": r[0], "confidence": r[1],
             "pan_angle": r[2], "seen_at": r[3], "seen_count": r[4]}
            for r in rows
        ]

    def clear_old_objects(self, max_age_s: int = 3600):
        cutoff = time.time() - max_age_s
        self._conn.execute("DELETE FROM objects WHERE seen_at < ?", (cutoff,))
        self._conn.commit()

    def reload_config(self, config: dict):
        """Call after config changes to pick up new remapping rules."""
        self.config = config
        self._sync_persons()

    # ── LLM context builder ───────────────────────────────────────────────────

    def build_llm_context(self, max_age_s: int = 1800,
                          visible_persons: list = None,
                          pan_angle: float = None,
                          weather_str: str = None) -> str:
        lines  = []
        remap  = {k: v for k, v in self.config.get("object_remapping", {}).items()
                  if not k.startswith("_")}

        objects = self.get_recent_objects(max_age_s)
        if objects:
            counts: dict[str, int] = {}
            for obj in objects:
                raw_name = obj["class_name"]

                # Aplikuj remapping při čtení z DB (pro starší záznamy)
                if raw_name in remap:
                    mapped = remap[raw_name]
                    if mapped is None:
                        continue          # ignorovaná třída
                    display_name = str(mapped)
                else:
                    display_name = raw_name

                # Czech translation — zkus přeložit display_name i raw_name
                name_cz = COCO_CZ.get(display_name,
                          COCO_CZ.get(raw_name, display_name))
                counts[name_cz] = counts.get(name_cz, 0) + obj["seen_count"]

            parts = []
            for name_cz, cnt in sorted(counts.items(),
                                        key=lambda x: x[1], reverse=True)[:12]:
                parts.append(f"{name_cz} ({cnt}×)" if cnt > 1 else name_cz)
            if parts:
                lines.append("V místnosti vidím: " + ", ".join(parts) + ".")

        persons = self.get_persons()
        if persons:
            p_parts = []
            for p in persons:
                entry = p["name"]
                if p["gender"] == "žena":
                    entry += " (žena"
                elif p["gender"] == "muž":
                    entry += " (muž"
                else:
                    entry += " (pohlaví neznámé"
                if p["notes"]:
                    entry += f" — {p['notes']}"
                entry += ")"
                p_parts.append(entry)
            lines.append("Známé osoby v domě: " + ", ".join(p_parts) + ".")

        # Denni doba
        from datetime import datetime as _dt
        _h = _dt.now().hour
        if   5 <= _h < 12: _tod = "rano"
        elif 12 <= _h < 17: _tod = "odpoledne"
        elif 17 <= _h < 22: _tod = "vecer"
        else:               _tod = "v noci"
        _time_str = _dt.now().strftime("%H:%M")
        lines.append(f"Cas: {_time_str} ({_tod}).")

        # Aktualne viditelne osoby
        if visible_persons:
            known_vis = [n for n in visible_persons
                         if n not in ("Unknown", "...", "?", "")]
            unknown_cnt = sum(1 for n in visible_persons
                             if n in ("Unknown", "?", "..."))
            parts = []
            if known_vis:
                parts.append(", ".join(known_vis))
            if unknown_cnt:
                parts.append(f"{unknown_cnt} neznamych")
            if parts:
                lines.append("Prave vidim: " + ", ".join(parts) + ".")
        else:
            lines.append("Nikdo neni momentalne v zornem poli kamery.")

        # Pocasi
        if weather_str:
            lines.append(weather_str)

        # Smer pohledu kamery
        if pan_angle is not None:
            if pan_angle < -30:   _dir = "doleva"
            elif pan_angle > 30:  _dir = "doprava"
            else:                 _dir = "primo pred sebe"
            lines.append(f"Kamera se diva {_dir} ({pan_angle:.0f} stupnu).")


        return "\n".join(lines)
    def close(self):
        self._conn.close()
