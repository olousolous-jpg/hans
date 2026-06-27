"""
Display renderer — kreslení face boxes, face PIP grid a další view-only
rendering pro display_controller_picam.

Fáze B refaktoru: postupně sem migruje rendering kód ze start_loop.
View-only: nemodifikuje state, jen čte z controlleru a kreslí na display.

# B1_RENDERER_SKELETON_V1: vytvořeno jako prázdný skeleton.
# Metody se přidávají v dalších krocích (B.2, B.3, ...).
"""


class DisplayRenderer:
    """
    Rendering layer pro PicamDisplayController.

    Drží odkaz na controller (`ctrl`) ze kterého čte:
      - ctrl.config
      - ctrl._face_prep
      - ctrl._zoom_padding(), atd.

    Per-frame data (boxes, identities, _zoom_pip_slots, ...) přicházejí
    jako argumenty metod, ne jako state rendereru.
    """

    def __init__(self, ctrl):
        self.ctrl = ctrl

    # ── Face PIP grid ────────────────────────────────────────────────────
    def draw_face_pip_grid(self, display, boxes, _zoom_pip_slots, dw, dh):
        """
        Kreslí grid PIP náhledů obličejů (HQ zoom crops) v pravém sloupci.
        Pruning: expired slotů a slotů s indexem >= počet aktuálních boxes.

        FACE_PIP_GRID_METHOD_V1: extracted from start_loop (was inline pip_multi_patch).
        View-only kromě pruning _zoom_pip_slots (vraceno zpět callerovi).

        Returns: updated _zoom_pip_slots dict (po pruningu).
        """
        import cv2
        import time as _time
        _pip_size    = int(self.ctrl.config.get(
            'hq_zoom', {}).get('pip_size', 200))
        _pip_timeout = float(self.ctrl.config.get(
            'hq_zoom', {}).get('pip_timeout', 3.0))
        _pip_margin  = 10
        _now         = _time.time()
        # Expire stale slots
        # pip_stale_index_prune
        _n_faces = len(boxes)
        _zoom_pip_slots = {
            k: v for k, v in _zoom_pip_slots.items()
            if (_now - v['time'] < _pip_timeout
                and k < _n_faces)
        }
        # Draw each slot as a column from top-right downward
        # Seřaď podle plochy boxu — nejbližší nahoře
        _sorted_slots = sorted(
            _zoom_pip_slots.values(),
            key=lambda s: s.get('area', 0), reverse=True)
        for _slot_i, _slot in enumerate(_sorted_slots):
            _raw_crop = _slot['crop']
            # Zobraz preprocessovany crop (co jde do ArcFace)
            _proc_crop = self.ctrl._face_prep.enhance_crop(_raw_crop)
            _pip = cv2.resize(_proc_crop,
                              (_pip_size, _pip_size),
                              interpolation=cv2.INTER_LINEAR)
            _pip_bgr = _pip  # already RGB same as display
            _x1 = dw - _pip_size - _pip_margin
            _y1 = _pip_margin + _slot_i * (_pip_size + _pip_margin)
            _x2 = _x1 + _pip_size
            _y2 = _y1 + _pip_size
            if _y2 > dh:  # don't draw off screen
                break
            _roi = display[_y1:_y2, _x1:_x2]
            cv2.addWeighted(_pip_bgr, 0.85, _roi, 0.15, 0, _roi)
            display[_y1:_y2, _x1:_x2] = _roi
            # Border colour: green=known, orange=unknown
            _nm   = _slot.get('name', '?')
            _bcol = (0, 220, 0) if _nm not in                    ('Unknown', '?', '...', '') else (0, 165, 255)
            cv2.rectangle(display,
                          (_x1 - 2, _y1 - 2),
                          (_x2 + 2, _y2 + 2),
                          _bcol, 2)
            # Name label above PIP
            _lbl = _nm if _nm not in ('Unknown','?','...','') \
                   else 'HQ ZOOM'
            cv2.putText(display, _lbl,
                        (_x1, max(_y1 - 4, 12)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.45,
                        _bcol, 1)
        return _zoom_pip_slots

    # ── Face boxes ───────────────────────────────────────────────────────
    def draw_face_boxes(self, display, boxes, box_labels, identities, dw, dh):
        """
        Kreslí bboxy obličejů + jména/confidence labels na display.

        FACE_BOXES_METHOD_V1: extracted from start_loop (was inline loop).
        View-only.

        Returns: n_face (počet kreslených face boxů, používá se v HUD).
        """
        import cv2
        from scripts.picam_helpers import box_color
        from scripts.hailo_client import LABEL_FACE
        n_face = 0
        for i, box in enumerate(boxes):
            x1 = int(box[0] * dw);  y1 = int(box[1] * dh)
            x2 = int(box[2] * dw);  y2 = int(box[3] * dh)
            lbl        = box_labels[i] if i < len(box_labels) else LABEL_FACE
            name, conf = identities[i] if i < len(identities) else ("?", 0)
            color      = box_color(name)
            cv2.rectangle(display, (x1, y1), (x2, y2), color, 2)
            text = (f"{name} {conf:.2f}"
                    if name not in ("Unknown", "...") else name)
            n_face += 1
            cv2.putText(display, text, (x1, max(y1 - 8, 20)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.65, color, 2)
        return n_face

    # ── Avatar (AVATAR_DISPLAY_V1) ───────────────────────────────────────────
    def draw_avatar(self, display, dw, dh):
        """AVATAR_VIDEO_V1 — přehrává animované LivePortrait smyčky (PiP, levý roh):
        Hans mluví (TTS _speaking) → talk loop (hlp_d9 2. půlka); jinak idle loop
        (hlp_d13) + občas (ne moc často) náhodný delší klip (hans_idleloop/talkloop).
        Fallback statický idle.png. Flag-gated, klipy lazy-decode do paměti, try/except."""
        try:
            import os, cv2, time, random
            cfg = self.ctrl.config.get("hans_avatar", {}) or {}
            if not cfg.get("display_enabled", False):
                return
            size = int(cfg.get("display_size", 200))
            cdir = "data/avatar/clips"
            now = time.time()

            # AVATAR_TALK_SPEAKER_V1 — kdo mluví (sdílený TTS): Koláč má pitch +40Hz,
            # Hans +0Hz/None. Talk video JEN když mluví Hans; Koláč mluví → statický idle.
            speaking = False
            pitch = None
            try:
                tts = getattr(getattr(self.ctrl, "_hans_dialog", None), "tts", None)
                speaking = bool(getattr(tts, "_speaking", False))
                pitch = getattr(tts, "_current_pitch", None)
            except Exception:
                pass
            kolac_talking = speaking and pitch == "+40Hz"
            hans_talking = speaking and not kolac_talking

            # ── stavový automat (AVATAR_IDLE_STATIC_V1) ──
            #  mluví → talk loop; idle = statický PNG + krátká idle anim á 4s
            #  + náhodný „vytvořený" klip á ~10 min (variace). One-shoty → zpět na static.
            cur = getattr(self, "_av_cur", None)        # (kind, path, start, end) nebo None
            if hans_talking:
                desired = ("talk", os.path.join(cdir, "hlp_d13_00001.mp4"), 0.5)
            elif kolac_talking:
                desired = ("static", None, 0.0)         # Koláč mluví → Hans statický idle
            elif cur and cur[0] in ("idleanim", "extra"):
                desired = None                          # dohraj rozjetý one-shot
            else:
                idle_s = float(cfg.get("idle_anim_s", 4.0))
                var_s  = float(cfg.get("variety_s", 600.0))
                if getattr(self, "_av_next_idle", 0.0) == 0.0:
                    self._av_next_idle = now + idle_s
                if getattr(self, "_av_next_extra", 0.0) == 0.0:
                    self._av_next_extra = now + var_s
                if now >= self._av_next_extra:          # ~10 min: náhodný vytvořený klip
                    import glob as _g
                    pool = sorted(_g.glob(os.path.join(cdir, "*.mp4")))
                    desired = ("extra", random.choice(pool), 0.0) if pool else ("static", None, 0.0)
                    self._av_next_extra = now + var_s
                elif now >= self._av_next_idle:         # á 4s: krátká idle animace (hlp_d9)
                    desired = ("idleanim", os.path.join(cdir, "hlp_d9_00001.mp4"), 0.0)
                    self._av_next_idle = now + idle_s
                else:
                    desired = ("static", None, 0.0)     # statický PNG

            # přepnutí klipu / stavu
            if desired is not None and (cur is None or cur[0] != desired[0] or cur[1] != desired[1]):
                if desired[0] == "static":
                    self._av_cur = None
                else:
                    fr = self._av_clip_frames(desired[1], size)
                    if fr:
                        s = int(len(fr) * desired[2])
                        self._av_cur = (desired[0], desired[1], s, len(fr)); self._av_idx = s
                    else:
                        self._av_cur = None
                cur = getattr(self, "_av_cur", None)

            frame = None
            if cur:
                fr = self._av_clip_frames(cur[1], size)
                if fr:
                    i = getattr(self, "_av_idx", cur[2])
                    if i >= cur[3] or i < cur[2]:
                        if cur[0] in ("idleanim", "extra"):
                            self._av_cur = None; cur = None; i = None   # one-shot dohrál → static
                        else:
                            i = cur[2]                                  # loop (talk)
                    if i is not None:
                        frame = fr[i]; self._av_idx = i + 1

            if frame is None:                            # fallback statický idle.png
                frame = self._av_static_idle(size)
            if frame is None:
                return

            # AVATAR_STATE_ALWAYS_V1 — stav + zápis avatar_state.json BĚŽÍ VŽDY,
            # nezávisle na preview okně (feeduje dual-eye displej + web mirror).
            # Dřív bylo celé draw_avatar uvnitř `if preview_on` → v menu_mode (preview
            # OFF) se avatar_state.json nepsal → displej i web zamrzly.
            cur2 = getattr(self, "_av_cur", None)
            kind = cur2[0] if cur2 else "idle"
            # AVATAR_WEB_MIRROR_V1 — zapiš aktuální stav pro web/dual-eye (jen při ZMĚNĚ)
            try:
                _clip = os.path.basename(cur2[1]) if cur2 else None
                if getattr(self, "_av_web_state", None) != (kind, _clip):
                    self._av_web_state = (kind, _clip)
                    import json as _wj
                    with open("data/avatar/avatar_state.json", "w") as _wf:
                        _wj.dump({"mode": kind, "clip": _clip, "ts": now}, _wf)
            except Exception:
                pass

            # ── cv2 PiP do preview okna JEN když je preview zapnuté (display != None) ──
            if display is None:
                return
            # ── pozice pod HUD (AVATAR_DISPLAY_POS_V1) ──
            m = 10; hud_h = 74
            x1, y1 = m, m + hud_h
            x2, y2 = x1 + size, y1 + size
            if x2 > dw or y2 > dh:
                return
            display[y1:y2, x1:x2] = frame
            # stav do overlaye: mluvi / idle / klip:<jméno> (+ fallback idle)
            if kind == "extra" and cur2:
                nm = os.path.basename(cur2[1]).replace("hans_", "").replace("hlp_", "").replace("_00001.mp4", "")
                state = "klip:" + nm
            else:
                state = {"talk": "mluvi", "idle": "idle", "idleanim": "idle"}.get(kind, kind)
            col = (0, 180, 255) if kind == "talk" else (0, 220, 0)
            cv2.rectangle(display, (x1 - 2, y1 - 2), (x2 + 2, y2 + 2), col, 2)
            cv2.putText(display, "Hans - " + state, (x1, y2 + 18),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, col, 1)
        except Exception:
            pass

    def _av_clip_frames(self, path, size):
        """AVATAR_VIDEO_V1 — lazy decode mp4 do paměti (BGR, size×size), cache per
        (path,size). Vrací list snímků nebo []. Decode 1× při prvním použití klipu."""
        import os, cv2
        if not hasattr(self, "_av_clipcache"):
            self._av_clipcache = {}
        key = (path, size)
        if key in self._av_clipcache:
            return self._av_clipcache[key]
        out = []
        try:
            if os.path.exists(path):
                cap = cv2.VideoCapture(path)
                while True:
                    ok, f = cap.read()
                    if not ok:
                        break
                    out.append(cv2.resize(f, (size, size), interpolation=cv2.INTER_AREA))
                cap.release()
        except Exception:
            out = []
        self._av_clipcache[key] = out
        return out

    def _av_static_idle(self, size):
        """Fallback na statický idle.png nejnovější verze avataru."""
        import os, glob, cv2
        try:
            if getattr(self, "_av_idle_size", None) == size and getattr(self, "_av_idle_img", None) is not None:
                return self._av_idle_img
            vers = []
            for d in glob.glob("data/avatar/v*"):
                try:
                    vers.append((int(os.path.basename(d)[1:]), d))
                except ValueError:
                    pass
            if not vers:
                return None
            img = cv2.imread(os.path.join(max(vers)[1], "idle.png"))
            if img is None:
                return None
            self._av_idle_img = cv2.resize(img, (size, size), interpolation=cv2.INTER_AREA)
            self._av_idle_size = size
            return self._av_idle_img
        except Exception:
            return None

    def draw_kolac(self, display, dw, dh):
        """AVATAR_KOLAC_DISPLAY_V1 — obrázek Koláče POD Hansem, když Koláč „mluví"
        (po dobu Hans↔Koláč dialogu, flag hans_dialog._kolac_speaking). View-only,
        flag-gated (hans_avatar.display_enabled), cache, try/except."""
        try:
            cfg = self.ctrl.config.get("hans_avatar", {}) or {}
            if not cfg.get("display_enabled", False):
                return
            hd = getattr(self.ctrl, "_hans_dialog", None)
            if hd is None or not getattr(hd, "_kolac_speaking", False):
                return
            import os, cv2
            path = "data/avatar/kolac.png"
            if not os.path.exists(path):
                return
            size = int(cfg.get("display_size", 200))
            ksize = int(size * 0.85)
            if getattr(self, "_kolac_key", None) != (path, ksize):
                img = cv2.imread(path)               # BGR (jako main_frame)
                if img is None:
                    return
                self._kolac_img = cv2.resize(img, (ksize, ksize), interpolation=cv2.INTER_AREA)
                self._kolac_key = (path, ksize)
            m = 10
            hud_h = 74
            # pod Hansem (Hans: y od m+hud_h, výška size) + mezera
            x1 = m
            y1 = m + hud_h + size + 12
            x2, y2 = x1 + ksize, y1 + ksize
            if x2 > dw or y2 > dh:
                return
            display[y1:y2, x1:x2] = self._kolac_img
            cv2.rectangle(display, (x1 - 2, y1 - 2), (x2 + 2, y2 + 2), (0, 220, 220), 2)
            # KOLAC_NAME_CONFIGURABLE_V1 — label = zvolené jméno (ASCII-fold,
            # cv2 neumí diakritiku → „Koláč"→„Kolac", „Brepta"→„Brepta").
            import unicodedata as _ud
            from scripts.hans_kolac import kolac_name as _kn
            _lbl = (_ud.normalize('NFKD', _kn(self.ctrl.config))
                    .encode('ascii', 'ignore').decode() or 'Kolac')
            cv2.putText(display, _lbl, (x1, y2 + 18),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 220, 220), 1)
        except Exception:
            pass

    def _avatar_frames(self, vdir, base):
        """AVATAR_ANIM_V1 — cesty k frame sekvenci {base}_{i}.png (animace),
        jinak [{base}.png] (still) pokud existuje, jinak []. Cachované per
        (vdir,base) s re-scanem á 5 s, aby glob nešel každý frame (nový render dožene)."""
        import os, glob, time
        if not hasattr(self, "_av_frame_cache"):
            self._av_frame_cache = {}
        ck = (vdir, base)
        now = time.time()
        ent = self._av_frame_cache.get(ck)
        if ent and (now - ent[0]) < 5.0:
            return ent[1]
        seq = sorted(glob.glob(os.path.join(vdir, f"{base}_*.png")))
        if not seq:
            single = os.path.join(vdir, f"{base}.png")
            seq = [single] if os.path.exists(single) else []
        self._av_frame_cache[ck] = (now, seq)
        return seq
