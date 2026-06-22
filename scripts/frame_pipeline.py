"""
Frame pipeline — Hailo face detection + recognition voting + object detection.

Fáze B refaktoru: postupně sem migruje per-frame processing kód ze start_loop.
Nedrží trvalý frame state v sobě — všechen per-frame state se vrací jako
return hodnoty zpět do start_loop, kde je callerovi a renderer ho čte.

# B4_PIPELINE_SKELETON_V1: vytvořeno jako prázdný skeleton.
# Metody se přidávají v dalších krocích (B.5 Hailo, B.6 voting, B.7 obj det).
"""

from dataclasses import dataclass, field
from typing import Optional, Any, List, Dict


# B5A_FRAMECTX_V1: per-frame state procházející pipeline metodami.
# Roste postupně:
#   B.5 Hailo detection: boxes, box_labels, hailo_results, zoom_boxes,
#                        identities, zoom_pip_slots
#   B.6 voting + cluster_db: přibyde voting state
#   B.7 object detection: přibyde obj det state
@dataclass
class FrameContext:
    """
    Per-frame state procházející FramePipeline metodami.

    Vytvořeno v start_loop na začátku každé iterace, předáváno do pipeline
    metod, hodnoty se po každé metodě rozbalí zpět do lokálů (postupná
    migrace v B.5b, B.6, B.7).
    """
    # B.5 Hailo detection
    boxes: List = field(default_factory=list)
    box_labels: List = field(default_factory=list)
    hailo_results: Optional[Any] = None
    zoom_boxes: List = field(default_factory=list)
    identities: List = field(default_factory=list)
    zoom_pip_slots: Dict = field(default_factory=dict)
    # DETECT_DEDUP_EMBED_V1: indexy hailo_results, které detect_faces už hi-res
    # embeddoval → async je znovu neembedduje (dedup, fallback na emb v results).
    hq_embedded_idx: set = field(default_factory=set)
    # B.6c voting needs frame_idx for cleanup intervals (%30, %9000)
    frame_idx: int = 0


class FramePipeline:
    """
    Per-frame processing pipeline pro PicamDisplayController.

    Drží odkaz na controller (`ctrl`) ze kterého čte dependencies:
      - ctrl.hailo, ctrl._recognizer, ctrl._cluster_db
      - ctrl.face_db, ctrl._face_prep
      - ctrl.obj_client, ctrl.surr_db
      - ctrl.config, ctrl._zoom_* helpers
      - ctrl._active_tracks (per-track stavu pro voting)

    Per-frame data (boxes, identities, hailo_results, ...) přicházejí
    jako argumenty metod a vrací se zpátky callerovi.
    """

    def __init__(self, ctrl):
        self.ctrl = ctrl
        # B6A_PIPELINE_STATE_V1: persistuje mezi framy (voting state)
        import time as _time
        self.cluster_db_last_save = _time.time()
        self.cluster_last_add: dict = {}  # name -> timestamp posledniho pridani

        # B7A_OBJ_STATE_V1: persistuje mezi framy (object detection state)
        self.last_obj_detect = 0.0
        self.last_obj_detect_with_face = 0.0
        self.obj_label = ""

    # ── Hailo face detection ─────────────────────────────────────────────
    def detect_faces(self, ctx, main_frame, lores_frame):
        """
        Hailo face detection + HQ zoom embedding pro malé boxy.

        Volá se POUZE když je 'detect frame' (caller drží
        `if frame_idx % DETECT_EVERY == 0:`).

        Modifikuje:
          - ctx.boxes, ctx.box_labels (přepisuje pokud detection vrátí výsledek)
          - ctx.hailo_results (přepisuje vždy — None pokud Hailo selhal)
          - ctx.zoom_boxes (appenduje boxy které prošly HQ zoomem)
          - ctx.zoom_pip_slots (nastavuje sloty pro HQ zoomované faces)
        Čte:
          - ctx.identities (z předchozího framu, pro PIP slot name)

        B5B_HAILO_PIPELINE_V1: extracted from start_loop.
        """
        import time as _time
        from scripts.picam_helpers import box_area as _box_area, hq_crop as _hq_crop
        from scripts.hailo_client import LABEL_FACE, ARCFACE_SIZE

        _lores_for_det = self.ctrl._face_prep.enhance_lores(lores_frame)
        ctx.hailo_results = self.ctrl.hailo.infer(_lores_for_det)
        if ctx.hailo_results is not None:
            ctx.boxes      = [r[0] for r in ctx.hailo_results]
            ctx.box_labels = [r[2] for r in ctx.hailo_results]

            if (self.ctrl._zoom_enabled() and self.ctrl._zoom_faces()
                    and main_frame is not None):
                thresh  = self.ctrl._zoom_threshold()
                padding = self.ctrl._zoom_padding()
                hq_results = []
                _H, _W = main_frame.shape[:2]
                # DETECT_BATCH_EMBED_V1: sebrat všechny kvalifikující HQ cropy a
                # embeddovat je JEDNÍM batch voláním (dřív N× embed_faces([crop])
                # per obličej = N socket round-tripů + N NPU dispatchů v main loopu).
                _hq_idx = []
                _hq_crops = []
                for _i, (box, emb, lbl) in enumerate(ctx.hailo_results):
                    if lbl == LABEL_FACE and _box_area(box) < thresh:
                        # Aligned crop (same path as enrollment) — kompatibilita
                        # HQ zoom embeddingů se vzorky v known_faces.pkl.
                        _bw = box[2] - box[0]
                        _bh = box[3] - box[1]
                        hq_crop = self.ctrl._aligned_crop(
                            main_frame, box, _H, _W, _bw, _bh)
                        if hq_crop is None:
                            hq_crop = _hq_crop(
                                main_frame, box, padding,
                                ARCFACE_SIZE, ARCFACE_SIZE)
                        _hq_idx.append(_i)
                        _hq_crops.append(hq_crop)
                # DETECT_DEDUP_EMBED_V1: enhance parita s asyncem (gamma za šera),
                # ať dedup nemění kvalitu embeddingu malých obličejů. PIP crop
                # (_crop_by_idx) zůstává neenhanced — enhance jen pro ArcFace.
                _emb_crops = _hq_crops
                if _hq_crops and self.ctrl._face_prep.enabled:
                    _emb_crops = [self.ctrl._face_prep.enhance_crop(_c)
                                  for _c in _hq_crops]
                _hq_embs = (self.ctrl.hailo.embed_faces(_emb_crops)
                            if _emb_crops else [])
                _emb_by_idx = {}
                _crop_by_idx = {}
                for _k, _i in enumerate(_hq_idx):
                    _e = _hq_embs[_k] if _k < len(_hq_embs) else None
                    if _e is not None:
                        _emb_by_idx[_i] = _e
                        _crop_by_idx[_i] = _hq_crops[_k]
                # indexy hi-res embeddované zde → async je přeskočí (dedup)
                ctx.hq_embedded_idx = set(_emb_by_idx.keys())
                for _i, (box, emb, lbl) in enumerate(ctx.hailo_results):
                    if _i in _emb_by_idx:
                        emb = _emb_by_idx[_i]
                        ctx.zoom_boxes.append(box)
                        # pip_multi_patch — keying zachováno (index zoom_boxes)
                        _fi = len(ctx.zoom_boxes) - 1
                        _nm = (ctx.identities[_fi][0]
                               if _fi < len(ctx.identities) else '?')
                        ctx.zoom_pip_slots[_fi] = {
                            'crop': _crop_by_idx[_i],
                            'time': _time.time(),
                            'name': _nm,
                            'area': _box_area(box),
                        }
                    hq_results.append((box, emb, lbl))
                ctx.hailo_results = hq_results

        return ctx

    # ── Voting + dedup + downstream consumers ────────────────────────────
    def recognize_and_vote(self, ctx, main_frame, now):
        """
        Per-track weighted voting (ArcFace + Cluster DB) + dedup +
        downstream konzumenti identit (voice_identities, openwebui kontext,
        KodiMonitor, Hans mood).

        Volá se POUZE když Hailo detekoval boxes (caller drží
        `if frame_idx % DETECT_EVERY == 0` + `if ctx.hailo_results is not None`).

        Modifikuje:
          - ctx.identities (přepisuje rec výsledkem, pak votingem, pak dedup)
          - ctx.box_labels (přepisuje recognizerem)
          - ctx.zoom_pip_slots (aktualizuje názvy po recognizeru)
        Čte:
          - ctx.boxes, ctx.hailo_results
        Side effects (přes self.ctrl):
          - self.ctrl._active_tracks (track ID mapping)
          - self.ctrl._cluster_db (match, add, save)
          - self.ctrl._track_mgr (update, cleanup)
          - self.ctrl._voice_identities (sync pro voice)
          - self.ctrl.openwebui_chat (visible_persons, pan_angle)
          - self.ctrl._kodi_monitor (update_visible)
          - self.ctrl._hans_idle (event_unknown_person)
        Pipeline state:
          - self.cluster_db_last_save, self.cluster_last_add (B6A)

        B6C_VOTING_PIPELINE_V1: extracted from start_loop.
        """
        import logging as _logging
        from scripts.hailo_client import LABEL_FACE
        _vote_log = _logging.getLogger("recognition_diag")

        recognizer = self.ctrl._recognizer

        recognizer.submit(ctx.hailo_results, main_frame=main_frame,
                          skip_embed_idx=ctx.hq_embedded_idx)  # DETECT_DEDUP_EMBED_V1
        rec_boxes, rec_ids, rec_labels = recognizer.get_identities()
        if len(rec_boxes) == len(ctx.boxes):
            ctx.identities = list(rec_ids)
            ctx.box_labels = rec_labels
        else:
            ctx.identities = [("...", 0.0)] * len(ctx.boxes)

        # Aktualizuj jmena v PIP slotech po votingu
        for _pi, (_pn, _pc) in enumerate(ctx.identities):
            if _pi in ctx.zoom_pip_slots:
                ctx.zoom_pip_slots[_pi]["name"] = _pn

        # per-track weighted voting + cluster DB
        self.ctrl._active_tracks = self.ctrl._assign_track_ids(
            ctx.boxes, self.ctrl._active_tracks)
        for i, (box, emb, lbl) in enumerate(ctx.hailo_results):
            if lbl != LABEL_FACE:
                continue
            tid = self.ctrl._active_tracks.get(self.ctrl._box_key(box))
            if tid is None:
                continue
            _c = (ctx.zoom_pip_slots[i]['crop']
                  if i in ctx.zoom_pip_slots else None)
            track = self.ctrl._track_mgr.update(tid, emb, _c)
            if track.ready():
                w_emb = track.weighted_embedding()

                # ── Hlas 1: AsyncRecognizer (EMA per-frame) ──
                arc_name, arc_conf = ctx.identities[i]
                arc_unknown = arc_name in ('Unknown', '...', '?', '')

                # ── Hlas 2: Cluster DB ───────────────────────
                # CLUSTER_RESCUE_V1 — match_with_margin vrací i odstup od 2. osoby
                c_name, c_dist, c_margin = self.ctrl._cluster_db.match_with_margin(w_emb)
                c_conf = round(1.0 - c_dist, 3)
                c_unknown = c_name in ('unknown', 'Unknown')

                # ── Strict voting — shoda obou hlasů ─────────
                # CLUSTER_RESCUE_V1 — prahy z configu; cluster rescue (arc slabý)
                # má nižší práh + margin guard (jednoznačně jedna osoba).
                _rt = self.ctrl.config.get('recognition_tuning', {})
                _thresh = float(_rt.get('vote_thresh', 0.65))
                _resc_t = float(_rt.get('cluster_rescue_thresh', 0.58))
                _resc_m = float(_rt.get('cluster_rescue_margin', 0.06))
                _cluster_rescue = (not c_unknown and c_conf >= _resc_t
                                   and c_margin >= _resc_m)
                if not arc_unknown and not c_unknown:
                    if arc_name == c_name:
                        # Oba shodne -> jisty vysledek
                        final_name = arc_name
                        final_conf = round(min(1.0, (arc_conf + c_conf) / 2 * 1.2), 3)
                    else:
                        # Ruzna jmena -> nejistota, vezmi lepsi
                        if arc_conf >= c_conf and arc_conf >= _thresh:
                            final_name = arc_name
                            final_conf = round(arc_conf * 0.7, 3)
                        elif c_conf >= _thresh:
                            final_name = c_name
                            final_conf = round(c_conf * 0.7, 3)
                        else:
                            final_name = '?'
                            final_conf = 0.0
                elif not arc_unknown and arc_conf >= _thresh:
                    # Jen AsyncRecognizer vi
                    final_name = arc_name
                    final_conf = arc_conf
                elif _cluster_rescue:
                    # Cluster zachrání off-angle identitu (margin guard drží záměny)
                    final_name = c_name
                    final_conf = c_conf
                else:
                    # Oba nejisti
                    final_name = arc_name
                    final_conf = arc_conf

                # VOTE_DIAG_V1 — klasifikace rozhodnutí pro log.
                if not arc_unknown and not c_unknown:
                    if arc_name == c_name:
                        _vote_decision = "agree+boost"
                    else:
                        if arc_conf >= c_conf and arc_conf >= _thresh:
                            _vote_decision = "arc_wins"
                        elif c_conf >= _thresh:
                            _vote_decision = "cluster_wins"
                        else:
                            _vote_decision = "both_disagree_low"
                elif not arc_unknown and arc_conf >= _thresh:
                    _vote_decision = "arc_only"
                elif _cluster_rescue:
                    _vote_decision = "cluster_only"
                else:
                    _vote_decision = "both_unsure"

                try:
                    _vote_log.info(
                        "VOTE tid=%s arc=%s:%.2f cluster=%s:%.2f(d=%.2f,m=%.2f) "
                        "-> %s:%.2f (%s)",
                        tid,
                        arc_name if arc_name else "Unknown", arc_conf or 0.0,
                        c_name if c_name else "unknown", c_conf or 0.0, c_dist or 1.0,
                        c_margin or 0.0,  # CLUSTER_RESCUE_V1 — margin do logu pro ladění
                        final_name if final_name else "?", final_conf or 0.0,
                        _vote_decision,
                    )
                except Exception:
                    pass  # nikdy ať logging nepoloží hlavní loop

                track.set_decision(final_name, final_conf)
                # Pouzij potvrzene rozhodnuti pokud existuje
                if track.decision:
                    ctx.identities[i] = (track.decision, track.decision_conf)
                else:
                    ctx.identities[i] = ("...", 0.0)
                # CLUSTER_AUTO_ADD_FLAG_V1
                # Cluster auto-add je řízen config.recognition.auto_enrollment.
                # Pozn.: dřív tento flag nic neovládal (mrtvý), teď ano.
                # Vypnutí zabrání feedback loop kontaminaci clusteru
                # (viz distance bob#2 ↔ alice#4 = 0.193).
                _auto_add = bool(self.ctrl.config.get('recognition', {})
                                 .get('auto_enrollment', True))
                if _auto_add and final_name not in ('unknown', 'Unknown', '...', '?', '') \
                        and final_conf >= (0.75 if len(ctx.boxes) > 1 else 0.65):
                    _info = self.ctrl._cluster_db.info().get(final_name, {})
                    _samples = _info.get('samples', 0)
                    if _samples < 500:      _cooldown = 5.0
                    elif _samples < 2000:   _cooldown = 30.0
                    elif _samples < 10000:  _cooldown = 120.0
                    else:                   _cooldown = 600.0
                    _last = self.cluster_last_add.get(final_name, 0)
                    if now - _last >= _cooldown:
                        self.ctrl._cluster_db.add(final_name, w_emb)
                        self.cluster_last_add[final_name] = now

        if ctx.frame_idx % 30 == 0:
            self.ctrl._track_mgr.cleanup_stale()
        if ctx.frame_idx % 9000 == 0:  # ~5 minut pri 30fps
            if hasattr(self.ctrl, "_unknown_tracker"):
                self.ctrl._unknown_tracker.cleanup_stale()
            alive = set(self.ctrl._track_mgr.tracks.keys())
            self.ctrl._active_tracks = {
                k: v for k, v in self.ctrl._active_tracks.items()
                if v in alive
            }
            if now - self.cluster_db_last_save >= 60.0:
                self.ctrl._cluster_db.save()
                self.cluster_db_last_save = now

        # ── Deduplikace — stejné jméno jen jednou ────────
        _skip = ("Unknown", "...", "?", "")
        _seen_names: dict = {}   # name -> (idx, conf)
        for _di, (_dn, _dc) in enumerate(ctx.identities):
            if _dn in _skip:
                continue
            if _dn not in _seen_names:
                _seen_names[_dn] = (_di, _dc)
            else:
                # Stejné jméno — porovnej confidence
                _prev_idx, _prev_conf = _seen_names[_dn]
                if _dc > _prev_conf:
                    # Nová detekce má vyšší confidence
                    # — předchozí → Unknown
                    ctx.identities[_prev_idx] = ("Unknown", 0.0)
                    _seen_names[_dn] = (_di, _dc)
                else:
                    # Předchozí má vyšší confidence
                    # — tato → Unknown
                    ctx.identities[_di] = ("Unknown", 0.0)

        # voice_integration — sdílej identities
        self.ctrl._voice_identities = list(ctx.identities)

        # LLM kontext — aktualizuj viditelne osoby + pan angle
        if self.ctrl.openwebui_chat:
            self.ctrl.openwebui_chat._visible_persons = [
                n for n, c in ctx.identities
            ]
            _pan = (self.ctrl.servo_controller.current_pan
                    if self.ctrl.servo_controller else None)
            self.ctrl.openwebui_chat._pan_angle = _pan
        # KodiMonitor — aktualizuj viditelne osoby
        if hasattr(self.ctrl, '_kodi_monitor'):
            self.ctrl._kodi_monitor.update_visible(
                [n for n, c in ctx.identities])
        # Mood: neznámé osoby — HANS_EVENT_API_REWRITE_V1
        if hasattr(self.ctrl, '_hans_idle'):
            _unknown = sum(1 for n,c in ctx.identities if n in ("Unknown","?","...",""))
            if _unknown > 0:
                self.ctrl._hans_idle.event_unknown_person()

        return ctx

    # ── Object detection ─────────────────────────────────────────────────
    def detect_objects(self, ctx, main_frame, lores_frame, now, scanning, face_visible):
        """
        Object detection během scan mode + HQ zoom redet pro malé objekty.
        Volá se KAŽDÝ FRAME (interval check je uvnitř).

        Modifikuje:
          - ctx.zoom_boxes (appenduje union box při HQ object zoom)
        Side effects (přes self.ctrl):
          - self.ctrl.servo_controller (scanning_active toggle, AF trigger)
          - self.ctrl._picam2 (AF triggered mode pro v3_wide)
          - self.ctrl.obj_client (detect, connect)
          - self.ctrl.surr_db (record_objects)
          - self.ctrl._hans_dialog (update_detections)
          - self.ctrl._hans_idle (event_objects_seen, event_observation_context)
          - self.ctrl._save_object_thumbs
        Pipeline state:
          - self.obj_label (Scan: ... / "")
          - self.last_obj_detect, self.last_obj_detect_with_face

        B7C_OBJDET_PIPELINE_V1: extracted from start_loop.
        """
        import time as _time
        from scripts.picam_helpers import box_area as _box_area, hq_crop as _hq_crop
        from scripts.surroundings_db import COCO_CLASSES
        from scripts.debug_log import dbg as _dbg
        from scripts.logger import get_logger as _get_logger
        _syslog = _get_logger('display')

        # _OBJ_DETECT_INTERVAL: must match controller module constant (6.0)
        _OBJ_DETECT_INTERVAL = 6.0

        # ── Object detection: bez osoby každých 6s, s osobou každých 30s
        _obj_interval = (_OBJ_DETECT_INTERVAL if not face_visible
                         else _OBJ_DETECT_INTERVAL * 5)
        _obj_last     = (self.last_obj_detect if not face_visible
                         else self.last_obj_detect_with_face)
        if (scanning and self.ctrl.obj_client and self.ctrl.surr_db and
                now - _obj_last >= _obj_interval):

            self.last_obj_detect = now
            pan = (self.ctrl.servo_controller.current_pan
                   if self.ctrl.servo_controller else 0.0)

            _servo_was_scanning = False
            if self.ctrl.servo_controller:
                _servo_was_scanning = getattr(
                    self.ctrl.servo_controller, 'scanning_active', False)
                if _servo_was_scanning:
                    self.ctrl.servo_controller.scanning_active = False
                    _settle_ms = int(self.ctrl.config.get(
                        "hq_zoom", {}).get("servo_settle_ms", 150))
                    _time.sleep(_settle_ms / 1000.0)
                    # obj_hq_af_patch — trigger AF na střed scény
                    if self.ctrl.config.get('camera_model') == 'v3_wide':
                        _af_mode = self.ctrl.config.get(
                            'autofocus_mode', 'triggered')
                        if _af_mode == 'triggered':
                            try:
                                self.ctrl._picam2.set_controls({
                                    'AfMode':    1,
                                    'AfTrigger': 0,
                                })
                                _af_settle = int(self.ctrl.config.get(
                                    'hq_zoom', {}).get(
                                    'af_obj_settle_ms', 500))
                                _time.sleep(_af_settle / 1000.0)
                            except Exception:
                                pass

            if not self.ctrl.obj_client.is_connected():
                self.ctrl.obj_client.connect()

            # obj_hq_af_patch — vezmi čerstvý frame po settle
            try:
                _fresh_main, _fresh_lores = self.ctrl._get_frames()
                if _fresh_lores is not None:
                    _detect_frame = _fresh_lores
                    _detect_main  = _fresh_main
                else:
                    _detect_frame = lores_frame
                    _detect_main  = main_frame
            except Exception:
                _detect_frame = lores_frame
                _detect_main  = main_frame

            dets = self.ctrl.obj_client.detect(_detect_frame)

            if (dets and self.ctrl._zoom_enabled() and self.ctrl._zoom_objects()
                    and main_frame is not None):
                thresh  = self.ctrl._zoom_threshold()
                padding = self.ctrl._zoom_padding()
                zoom_dets   = []
                normal_dets = []
                for d in dets:
                    box = [d["x1"], d["y1"], d["x2"], d["y2"]]
                    if _box_area(box) < thresh:
                        zoom_dets.append(d)
                    else:
                        normal_dets.append(d)

                if zoom_dets:
                    all_x1 = min(d["x1"] for d in zoom_dets)
                    all_y1 = min(d["y1"] for d in zoom_dets)
                    all_x2 = max(d["x2"] for d in zoom_dets)
                    all_y2 = max(d["y2"] for d in zoom_dets)
                    union_box    = [all_x1, all_y1, all_x2, all_y2]
                    hq_obj_frame = _hq_crop(_detect_main, union_box, padding,
                                            self.ctrl._HAILO_W, self.ctrl._HAILO_H)
                    zoom_redets  = self.ctrl.obj_client.detect(hq_obj_frame)
                    bw = all_x2 - all_x1; bh = all_y2 - all_y1
                    ox = max(0.0, all_x1 - bw * padding)
                    oy = max(0.0, all_y1 - bh * padding)
                    ow = min(1.0, all_x2 + bw * padding) - ox
                    oh = min(1.0, all_y2 + bh * padding) - oy
                    for d in zoom_redets:
                        normal_dets.append({
                            **d,
                            "x1": ox + d["x1"] * ow,
                            "y1": oy + d["y1"] * oh,
                            "x2": ox + d["x2"] * ow,
                            "y2": oy + d["y2"] * oh,
                        })
                    ctx.zoom_boxes.append(union_box)
                    pass  # object zoom triggered
                    dets = normal_dets

            # region agent log
            try:
                top = sorted(
                    [{"class_id": d.get("class_id"), "conf": float(d.get("confidence", 0.0))}
                     for d in (dets or [])],
                    key=lambda x: x["conf"], reverse=True
                )[:5]
                _dbg(
                    location="display_controller_picam.py:obj_detect",
                    message="Object detection batch",
                    data={
                        "scanning": bool(scanning),
                        "face_visible": bool(face_visible),
                        "n": len(dets or []),
                        "top5": top,
                        "lores_shape": list(getattr(_detect_frame, "shape", []))[:3],
                    },
                )
            except Exception:
                pass
            # endregion

            if dets:
                named = []
                for d in dets:
                    cid  = d["class_id"]
                    name = COCO_CLASSES.get(cid, f"class_{cid}")
                    named.append({**d, "class_name": name})
                self.ctrl.surr_db.record_objects(named, pan_angle=pan)
                # HansDialog — informuj o detekovaných objektech
                if hasattr(self.ctrl, '_hans_dialog'):
                    self.ctrl._hans_dialog.update_detections(
                        [d['class_name'] for d in named])
                # Objekty viděné — HANS_EVENT_API_REWRITE_V1
                # Hans_idle si rozdělí: curiosity[0] + mood na celý list
                if hasattr(self.ctrl, '_hans_idle'):
                    self.ctrl._hans_idle.event_objects_seen(
                        [d.get('class_name','') for d in named])
                self.ctrl._save_object_thumbs(_detect_frame, named)
                # Self-question při zajímavém objektu (10% šance)
                # HANS_EVENT_API_REWRITE_V1
                if hasattr(self.ctrl, '_hans_idle'):
                    import random as _rnd2
                    for _det2 in named:
                        _cn2 = _det2.get('class_name','')
                        # QUESTIONS_OBSERVATION_CZ_V1 — label do češtiny (COCO_CZ)
                        # + přirozená věta (model neechuje "Zaznamenán byl bed.");
                        # filtr na reálné EN labely (nudný nábytek).
                        if (_cn2 not in ('tv', 'couch', 'chair') and
                                _rnd2.random() < 0.1):
                            from scripts.surroundings_db import COCO_CZ as _CZ
                            _ctx2 = f'V místnosti jsem zahlédl {_CZ.get(_cn2, _cn2)}.'
                            self.ctrl._hans_idle.event_observation_context(
                                _ctx2, source_type='observation')
                            break
                seen = list({d["class_name"] for d in named})[:4]
                self.obj_label = "Scan: " + ", ".join(seen)
                _syslog.info("Objects pan=%.0f°: %s", pan,
                             [d["class_name"] for d in named])
            else:
                self.obj_label = ""

            if self.ctrl.servo_controller and _servo_was_scanning:
                self.ctrl.servo_controller.scanning_active = True
                self.ctrl.servo_controller.last_scan_update = _time.time()
                self.ctrl.servo_controller.scan_pausing = False
            if face_visible:
                self.last_obj_detect_with_face = _time.time()
            else:
                self.last_obj_detect = _time.time()

        if face_visible:
            self.obj_label = ""
