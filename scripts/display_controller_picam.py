"""
Picamera2 Display Controller
Camera loop, enrollment state machine, servo dispatch, key handling.
Object detection runs during scan mode (no face detected).
HQ zoom crop: when detected box is small (far away), crop from main 1280×960
instead of lores 640×480 — gives ArcFace and YOLOv8 a sharper input.
"""

import cv2
import numpy as np
import time
import os
from pathlib import Path
from scripts.fisheye_corrector import FisheyeCorrector
from scripts.picam_helpers import (
    box_color, draw_hand_skeleton as _draw_hand_skeleton,
    box_area as _box_area, hq_crop as _hq_crop,
    draw_zoom_box as _draw_zoom_box,
    _HAND_CONNECTIONS,
)
from scripts.picam_camera import (
    get_tuning_for_model as _get_tuning_for_model,
    apply_camera_model as _apply_camera_model,
    setup_af as _setup_af,
    update_af as _update_af,
)
from scripts.picam_enrollment import EnrollmentManager
# B9_MINI_CLEANUP_V1: duplicitní importy EnrollmentManager + picam_camera odstraněny
from scripts.gesture_client import GestureClient
from scripts.kodi_client import KodiClient
from scripts.display_renderer import DisplayRenderer  # B1_RENDERER_SKELETON_V1
from scripts.frame_pipeline import FramePipeline, FrameContext  # B4_PIPELINE_SKELETON_V1, B5A_FRAMECTX_V1
from scripts.logger import get_logger
_syslog = get_logger('display')

# VOTE_DIAG_V1: Sdílený logger s AsyncRecognizerem — píše do data/recognition.log.
# AsyncRecognizer si ho zakládá při startu; my si jen vezmeme existující instanci.
# Pokud ještě nebyl založen (rare race), getLogger vrátí prázdný — vote řádky
# se ztratí, ale nic se nerozbije.
import logging as _logging_vote
_vote_log = _logging_vote.getLogger("recognition_diag")

# B8_CLEANUP_V1: _dbg import removed (moved to frame_pipeline)

# ── Monkey-patch picamera2 missing allocator attribute ───────────────────────
try:
    import picamera2.request as _picam_req

    class _AllocatorShim:
        def sync(self, *a, **kw):       return self
        def acquire(self, *a, **kw):    return None
        def release(self, *a, **kw):    return None
        def __call__(self, *a, **kw):   return self
        def __enter__(self):            return self
        def __exit__(self, *a):         return False

    _orig_cr_init = _picam_req.CompletedRequest.__init__

    def _patched_cr_init(self, request, picam2):
        if not hasattr(picam2, 'allocator'):
            picam2.allocator = _AllocatorShim()
        _orig_cr_init(self, request, picam2)

    _picam_req.CompletedRequest.__init__ = _patched_cr_init
    print('[PicamDisplay] picamera2 allocator patch applied')
except Exception as _patch_err:
    print(f'[PicamDisplay] allocator patch skipped: {_patch_err}')

from picamera2 import Picamera2

from scripts.hailo_client     import HailoClient, LABEL_FACE, ARCFACE_SIZE
# TODO: import gesture recognition client here
from scripts.face_db          import FaceDB
from scripts.async_recognizer import AsyncRecognizer
from scripts.face_track_manager import FaceTrackManager
from scripts.cluster_face_db    import ClusterFaceDB
from scripts.overlay_ui       import OverlayUI
# CONFIG_GUI_REMOVED_V1 — klávesa-S Tkinter config odstraněna (moc široká, nevejde se
# na obrazovku). Konfig nyní jen přes webadmin (schema-driven, SCHEMA_DRIVEN_TABS_V1).
from scripts.tk_manager       import tk_mgr
from scripts.surroundings_db  import SurroundingsDB  # B8_CLEANUP_V1: COCO_CLASSES moved to pipeline

try:
    from scripts.object_client import ObjectClient
    _OBJECT_CLIENT_AVAILABLE = True
except ImportError:
    _OBJECT_CLIENT_AVAILABLE = False
    print('[PicamDisplay] object_client not available — object detection disabled')

# DETECT_EVERY: čte se z configu (display_controller.detect_every), fallback 2.
# DETECT_EVERY_FROM_CONFIG_V1 — dřív natvrdo 2, konfig (3) se ignoroval.
def _load_detect_every():  # noqa: E305
    try:
        import json as _json
        with open("config.json", encoding="utf-8") as _f:
            _v = _json.load(_f).get("display_controller", {}).get("detect_every", 2)
        _v = int(_v)
        return _v if _v >= 1 else 2
    except Exception:
        return 2
DETECT_EVERY = _load_detect_every()

# B8_CLEANUP_V1: _OBJ_DETECT_INTERVAL = 6.0 removed (moved to frame_pipeline)

_THUMB_DIR = Path("data/object_thumbs")
_THUMB_W   = 160
_THUMB_H   = 120


# ── Box colour helper ─────────────────────────────────────────────────────────

# AF konstanty

# box_color, _HAND_CONNECTIONS — viz scripts/picam_helpers.py

# _draw_hand_skeleton — viz scripts/picam_helpers.draw_hand_skeleton


# _box_area, _hq_crop, _draw_zoom_box — viz scripts/picam_helpers.py


# _hq_crop — viz scripts/picam_helpers.hq_crop


# _draw_zoom_box — viz scripts/picam_helpers.draw_zoom_box


# ── Main controller ───────────────────────────────────────────────────────────


# ── Camera model helpers ──────────────────────────────────────────────────────

# _CAM_TUNING, AF konstanty — viz scripts/picam_camera.py


# _get_tuning_for_model — viz scripts/picam_camera.get_tuning_for_model


# _apply_camera_model — viz scripts/picam_camera.apply_camera_model

# _setup_af — viz scripts/picam_camera.setup_af


# AF state + _update_af — viz scripts/picam_camera.AutoFocusController


class PicamDisplayController:

    def __init__(self, config, database_manager=None,
                 openwebui_chat=None, servo_controller=None):
        self.config           = config
        self.database_manager = database_manager
        self.openwebui_chat   = openwebui_chat
        self.servo_controller = servo_controller
        self.processing       = True
        self._calib           = None   # SERVO_MANUAL_CALIB_V1 wizard state

        self._headless = bool(config.get("display", {}).get("headless", False))
        if self._headless:
            print("[PicamDisplay] Headless mode — video output disabled")
        # HANS_MENU_V1 — start menu + runtime preview toggle
        self._menu_mode   = bool(config.get("display", {}).get("menu_mode", True))
        self._preview_on  = (not self._headless) and (not self._menu_mode)
        self._win_created = False
        self._menu        = None
        self._present_known    = []    # HANS_MENU_PRESELECT_V1
        self._present_known_ts = 0.0

        cam = config.get("camera", {})
        self._MAIN_W  = int(cam.get("main_width",  1280))
        self._MAIN_H  = int(cam.get("main_height", 960))

        # Click-to-enroll state (anti-misclick: 2 kliky do 3s na stejný bbox)
        self._click_target_box  = None     # normalized [x1,y1,x2,y2] | None
        self._click_target_time = 0.0      # čas prvního kliku
        self._click_xy_pending  = None     # (x_px, y_px) | None — k zpracování
        self._HAILO_W = int(cam.get("lores_width", 640))
        self._HAILO_H = int(cam.get("lores_height", 480))
        print(f"[PicamDisplay] Main: {self._MAIN_W}×{self._MAIN_H}  "
              f"Lores: {self._HAILO_W}×{self._HAILO_H}")

        self._init_vision_clients()  # INIT_VISION_CLIENTS_V1

        self._init_kodi_and_weather()  # INIT_KODI_AND_WEATHER_V1

        self._init_hans_modules()  # INIT_HANS_MODULES_V1

        self._init_observers()  # INIT_OBSERVERS_V1


        self._init_gesture_and_objects()  # INIT_GESTURE_AND_OBJECTS_V1

        self._init_enrollment_and_voice()  # INIT_ENROLLMENT_AND_VOICE_V1
        self._init_memory()  # T4_FIX_WIRING_V1 — až po všech závislostech

        # B1_RENDERER_SKELETON_V1: rendering layer
        self._renderer = DisplayRenderer(self)

        # B4_PIPELINE_SKELETON_V1: per-frame processing layer
        self._pipeline = FramePipeline(self)

    # ── Zoom config helpers ───────────────────────────────────────────────────

    def _zoom_cfg(self)       -> dict:  return self.config.get("hq_zoom", {})
    def _zoom_enabled(self)   -> bool:  return self._zoom_cfg().get("enabled", True)
    def _zoom_threshold(self) -> float: return float(self._zoom_cfg().get("trigger_area", 0.04))
    def _zoom_padding(self)   -> float: return float(self._zoom_cfg().get("padding", 0.25))
    def _zoom_faces(self)     -> bool:  return self._zoom_cfg().get("zoom_faces", True)
    def _zoom_objects(self)   -> bool:  return self._zoom_cfg().get("zoom_objects", True)

    # ── Main loop ─────────────────────────────────────────────────────────────

    def start_loop(self) -> int:
        # PICAM2_SELF_V1
        import queue as _queue

        self._frame_q = _queue.SimpleQueue()  # GET_FRAMES_METHOD_V1
        self._picam2   = None
        self._vision_paused = False  # SLEEP_VISION_OFF_V1 — spánek: kamera+recognition off

        try:
            tuning = _get_tuning_for_model(self.config)
            self._picam2 = Picamera2(tuning=tuning) if tuning else Picamera2()
            # lores_clamp_patch: lores must be strictly smaller than main
            _lores_w = min(self._HAILO_W, self._MAIN_W - 2)
            _lores_h = min(self._HAILO_H, self._MAIN_H - 2)
            if _lores_w != self._HAILO_W or _lores_h != self._HAILO_H:
                print(f'[PicamDisplay] lores clamped to {_lores_w}×{_lores_h} '
                      f'(main={self._MAIN_W}×{self._MAIN_H})')
            cfg = self._picam2.create_preview_configuration(
                main    = {'size': (self._MAIN_W, self._MAIN_H), 'format': 'RGB888'},
                lores   = {'size': (_lores_w, _lores_h), 'format': 'RGB888'},
                controls= {'FrameRate': self.config.get("camera", {}).get("framerate", 30)},
            )
            self._picam2.configure(cfg)
            _apply_camera_model(self._picam2, self.config)
            # post_configure_lores_clamp: read back actual negotiated size
            # picamera2 may pick a smaller sensor mode than requested
            _actual = self._picam2.camera_configuration()
            if _actual:
                _real_w, _real_h = _actual['main']['size']
                if _lores_w >= _real_w or _lores_h >= _real_h:
                    _lores_w = min(_lores_w, _real_w - 2)
                    _lores_h = min(_lores_h, _real_h - 2)
                    print(f'[PicamDisplay] post-configure lores re-clamped '
                          f'to {_lores_w}×{_lores_h} '
                          f'(actual main={_real_w}×{_real_h})')
                    self._picam2.stop()
                    cfg2 = self._picam2.create_preview_configuration(
                        main    = {'size': (_real_w, _real_h), 'format': 'RGB888'},
                        lores   = {'size': (_lores_w, _lores_h), 'format': 'RGB888'},
                        controls= {'FrameRate': self.config.get('camera', {}).get('framerate', 30)},
                    )
                    self._picam2.configure(cfg2)
                    _apply_camera_model(self._picam2, self.config)
                    self._HAILO_W = _lores_w
                    self._HAILO_H = _lores_h

            self._picam2.pre_callback = self._frame_cb  # FRAME_CB_METHOD_V1
            self._picam2.start()
            _setup_af(self._picam2, self.config)
            # hdr_patch
            if self.config.get('camera_model') == 'v3_wide':
                _hdr = int(self.config.get('hdr_mode', 3))
                if _hdr > 0:
                    try:
                        self._picam2.set_controls({'HdrMode': _hdr})
                        print(f'[PicamDisplay] HDR mode={_hdr} zapnut')
                    except Exception as _e:
                        print(f'[PicamDisplay] HDR skipped: {_e}')
            time.sleep(1)

        except Exception as e:
            print(f"[PicamDisplay] Camera init failed: {e}")
            if self._picam2:
                try: self._picam2.stop()
                except: pass
            return 1

        print("[PicamDisplay] Camera started")
        if self._headless:
            print("  Headless mode — no video window")
            import threading, queue as _tq
            self._term_q = _tq.SimpleQueue()  # TERM_READER_METHOD_V1
            threading.Thread(target=self._term_reader, daemon=True).start()
        else:
            print("  E=enroll  D=delete  L=list  S=settings  C=chat  ESC=quit")
            self._term_q = None  # TERM_READER_METHOD_V1

        if self.servo_controller:
            self.servo_controller.update_image_dimensions(self._MAIN_W, self._MAIN_H)

        win = self.config.get('display', {}).get('window_name', 'Face Recognition')
        self._win_name = win  # HANS_MENU_V1
        if self._preview_on:
            cv2.namedWindow(win, cv2.WINDOW_AUTOSIZE)
            cv2.setMouseCallback(win, self._on_mouse)  # ON_MOUSE_METHOD_V1
            self._win_created = True
        # HANS_MENU_V1 — open start menu (works without preview)
        if self._menu_mode and self._menu is None:
            try:
                from scripts.hans_menu import HansMenu
                self._menu = HansMenu(self)
            except Exception as _e:
                print(f"[HansMenu] init failed: {_e}")

        boxes           = []
        box_labels      = []
        identities      = []
        hailo_results   = None  # PERSISTENT_UNKNOWN_METHOD_V1: init before loop
        frame_idx       = 0
        fps_t           = time.time()
        fps_cnt         = 0
        fps             = 0
        # B7A_OBJ_STATE_V1: last_obj_detect, last_obj_detect_with_face, obj_label
        # přesunuto na self._pipeline (persistují mezi iteracemi while)
        zoom_boxes      = []
        # pip_multi_patch — per-face zoom crop slots
        # {face_idx: {'crop': ndarray, 'time': float, 'name': str}}
        _zoom_pip_slots: dict = {}
        # af_nearest_patch — track which box is nearest for AF
        _af_nearest_box  = None   # box tuple of last nearest face
        _af_nearest_area = 0.0

        # QA_GUARD_V1: uložit na self ať worker thread má přístup
        self._recognizer = AsyncRecognizer(
            self.face_db, self.hailo,
            config=self.config,
            openwebui_chat=self.openwebui_chat,
        )
        recognizer = self._recognizer  # QA_GUARD_V1 alias
        # B6A_PIPELINE_STATE_V1: _cluster_db_last_save a _cluster_last_add
        # přesunuto na self._pipeline (persistují mezi iteracemi while)
        if hasattr(self, '_unknown_tracker'):
            self._unknown_tracker._recognizer = recognizer
        ui = OverlayUI(win)
        self._ui = ui  # HANS_MENU_V1 — expose for menu-driven enroll/delete

        # CONFIG_GUI_REMOVED_V1 — ConfigGUI (klávesa S) odstraněna; konfig přes webadmin.
        # _on_settings_save zůstává (hot-reload z web adminu níže).

        # ── Config file watcher (hot-reload z web adminu) ─────────────
        _config_path = Path('config.json')
        _config_mtime = _config_path.stat().st_mtime if _config_path.exists() else 0
        _config_check_interval = 5.0
        _config_last_check = time.time()

        # ── Frame loop ────────────────────────────────────────────────────
        while self.processing:
            main_frame, lores_frame = self._get_frames()
            if main_frame is None:
                continue

            lores_frame = self.fisheye.undistort_lores(lores_frame)

            frame_idx += 1
            fps_cnt   += 1
            now = time.time()
            if now - fps_t >= 1.0:
                fps     = fps_cnt
                fps_cnt = 0
                fps_t   = now

            # Clear gesture label + bbox after timeout
            # Fist během nahrávání: timeout ignoruj — stop jen přes druhý fist
            _voice_rec = getattr(getattr(self, "_voice", None), "is_recording", False)
            _GESTURE_TIMEOUT = 999.0 if (self._last_gesture == "fist" and _voice_rec) else 2.0
            if (self._last_gesture and
                    now - self._last_gesture_time > _GESTURE_TIMEOUT):
                self._last_gesture      = None
                self._last_gesture_bbox = None

            # TK_PUMP_RESTORE_V1 — pump sdíleného tk_mgr (chat popup, enroll review
            # okno). CONFIG_GUI_REMOVED_V1 omylem odstranil _config_gui.pump(),
            # který tk_mgr poháněl → call_soon okna se zařadila, ale nevykreslila.
            tk_mgr.pump()

            # Config file watcher — hot-reload z web adminu
            if now - _config_last_check >= _config_check_interval:
                _config_last_check = now
                try:
                    _new_mtime = _config_path.stat().st_mtime
                    if _new_mtime != _config_mtime:
                        _config_mtime = _new_mtime
                        import json as _cjson
                        _new_cfg = _cjson.loads(
                            _config_path.read_text(encoding='utf-8'))
                        self.config.update(_new_cfg)
                        self._on_settings_save(_new_cfg)  # ON_SETTINGS_SAVE_METHOD_V1
                        _syslog.info('Config reloaded from web admin')
                except Exception as _ce:
                    _syslog.debug('Config watch: %s', _ce)

            zoom_boxes = []  # B9_MINI_CLEANUP_V1: reset každý frame (původní komentář byl nepřesný)

            # B5A_FRAMECTX_V1: skeleton — ctx se zatím nepoužívá, jen ověřuje
            # že FrameContext lze instanciovat. V B.5b se Hailo block přesune
            # do pipeline.detect_faces(ctx) a ctx začne procházet metodami.
            _ctx = FrameContext(
                boxes=boxes,
                box_labels=box_labels,
                hailo_results=hailo_results,
                zoom_boxes=zoom_boxes,
                identities=identities,
                zoom_pip_slots=_zoom_pip_slots,
                frame_idx=frame_idx,  # B6C_VOTING_PIPELINE_V1
            )

            # ── Hailo face detection ──────────────────────────────────────
            # B5B_HAILO_PIPELINE_V1: extracted to self._pipeline.detect_faces
            if frame_idx % DETECT_EVERY == 0 and not self._vision_paused:  # SLEEP_VISION_OFF_V1 — recognition gate (framy tečou dál, jen detekce stojí)
                _ctx = self._pipeline.detect_faces(_ctx, main_frame, lores_frame)
                hailo_results = _ctx.hailo_results
                if hailo_results is not None:
                    # Unpack Hailo state → lokály (voting block je čte)
                    boxes           = _ctx.boxes
                    box_labels      = _ctx.box_labels
                    zoom_boxes      = _ctx.zoom_boxes
                    _zoom_pip_slots = _ctx.zoom_pip_slots

                    # B6C_VOTING_PIPELINE_V1: extracted to self._pipeline.recognize_and_vote
                    _ctx = self._pipeline.recognize_and_vote(_ctx, main_frame, now)
                    # Unpack ctx → lokály (downstream side effects v start_loop je čtou)
                    identities      = _ctx.identities
                    box_labels      = _ctx.box_labels
                    _zoom_pip_slots = _ctx.zoom_pip_slots

                self._update_servo(boxes, identities)
                # RoomObserver — jednou denne popis mistnosti
                if hasattr(self, '_room_observer') and main_frame is not None:
                    self._room_observer.submit_frame(main_frame)

                # HansIdle — vzdy informuj o viditelnych osobach
                if hasattr(self, '_hans_idle'):
                    self._hans_idle.person_seen(
                        [n for n, c in identities])

                # HANS_MENU_PRESELECT_V1 — předvybraná osoba v menu dle detekce
                _known_now = [n for n, c in identities
                              if n not in ("Unknown", "Person", "...", "?", "")]
                if _known_now:
                    self._present_known    = _known_now
                    self._present_known_ts = now

                # Greeting trigger — pozdrav rozpoznané osoby přes chat handler.
                # handle_face_recognition() je idempotentní (session/daily flag),
                # bezpečné volat každý frame. # GREETING_TRIGGER_V1
                if self.openwebui_chat is not None:
                    for _name, _conf in identities:
                        if _name and _name not in (
                                "Unknown", "Person", "...", "?", ""):
                            try:
                                self.openwebui_chat.handle_face_recognition(
                                    _name, float(_conf))
                            except Exception as _e:
                                # Nesmí shodit hlavní smyčku
                                print(f"[Greeting] trigger error: {_e}")
                # HansDialog — vzdy informuj o detekovanych objektech
                if hasattr(self, '_hans_dialog') and boxes:
                    pass  # update_detections se vola pri obj detection


            # ── Persistent unknown tracker ────────────────────────────────
            # PERSISTENT_UNKNOWN_METHOD_V1: extracted to self._handle_persistent_unknown
            self._handle_persistent_unknown(
                main_frame, boxes, identities, hailo_results, frame_idx)



            # ── face_visible ──────────────────────────────────────────────
            # FACE_VISIBLE_AF_METHOD_V1: extracted to self._compute_face_visible_and_af
            face_visible = self._compute_face_visible_and_af(
                boxes, identities, frame_idx)

            scanning = (self.servo_controller is not None and
                        getattr(self.servo_controller, 'scanning_active', False))

            # ── Gesture recognition ───────────────────────────────
            if frame_idx % DETECT_EVERY == 0 and not scanning:
                self.gesture.submit(lores_frame)

            # ── Gesture data collection ────────────────────────────
            # GESTURE_COLLECTION_METHOD_V1: extracted to self._handle_gesture_collection
            self._handle_gesture_collection(ui, now)

            # ── Object detection during scan mode ─────────────────────
            # B7C_OBJDET_PIPELINE_V1: extracted to self._pipeline.detect_objects
            self._pipeline.detect_objects(
                _ctx, main_frame, lores_frame, now, scanning, face_visible)

            # ── Draw + Display ────────────────────────────────────────────
            # HANS_MENU_V1 — runtime preview window open/close
            if self._preview_on and not self._win_created:
                cv2.namedWindow(win, cv2.WINDOW_AUTOSIZE)
                cv2.setMouseCallback(win, self._on_mouse)
                self._win_created = True
            elif (not self._preview_on) and self._win_created:
                try:
                    cv2.destroyWindow(win)
                    cv2.waitKey(1)
                except Exception:
                    pass
                self._win_created = False
            if self._preview_on:
                display = main_frame.copy()
                dh, dw  = display.shape[:2]
                # FACE_BOXES_METHOD_V1: extracted to self._renderer.draw_face_boxes
                n_face = self._renderer.draw_face_boxes(
                    display, boxes, box_labels, identities, dw, dh)


                # ── Draw gesture overlay (skeleton + palm bbox + gesture bbox/label) ─
                # DRAW_GESTURE_OVERLAY_METHOD_V1: extracted to self._draw_gesture_overlay
                self._draw_gesture_overlay(display, dw, dh)

                padding = self._zoom_padding()
                # HQ rámeček skryt — zobrazuje se v PIP
                # for zb in zoom_boxes:
                #     _draw_zoom_box(display, zb, padding)

                # ── Hand PIP — crop ruky z main_frame ────────────
                # DRAW_HAND_PIP_METHOD_V1: extracted to self._draw_hand_pip
                self._draw_hand_pip(display, main_frame, dh)

                # ── HUD: FPS + obj label + legenda ───────────────
                # DRAW_HUD_METHOD_V1: extracted to self._draw_hud
                self._draw_hud(display, dh, fps, n_face, self._pipeline.obj_label)  # B7A_OBJ_STATE_V1

                self._draw_enrollment_overlay(display, dw, dh)
                ui.draw(display)
                # FACE_PIP_GRID_METHOD_V1: extracted to self._renderer.draw_face_pip_grid
                _zoom_pip_slots = self._renderer.draw_face_pip_grid(
                    display, boxes, _zoom_pip_slots, dw, dh)
                # AVATAR_DISPLAY_V1 — Hansova tvář dle nálady (flag-gated, levý roh)
                self._renderer.draw_avatar(display, dw, dh)
                # AVATAR_KOLAC_DISPLAY_V1 — Koláč pod Hansem, když mluví (dialog)
                self._renderer.draw_kolac(display, dw, dh)
                # ── Click-to-enroll: zpracování pending kliku ─────────
                # CLICK_TO_ENROLL_METHOD_V1: extracted to self._handle_click_to_enroll
                self._handle_click_to_enroll(ui, boxes, display)

                cv2.imshow(win, display)
            else:
                # AVATAR_STATE_ALWAYS_V1 — preview OFF (menu_mode): avatar PiP se nekreslí,
                # ale stavový automat musí tikat + psát avatar_state.json, jinak dual-eye
                # displej i web mirror zamrznou. draw_avatar(None) = jen stav + zápis.
                self._renderer.draw_avatar(None, 0, 0)

            # ── Key handling ──────────────────────────────────────────────
            # KEY_DISPATCH_METHOD_V1: extracted to self._handle_keys
            if not self._handle_keys(ui, boxes, identities, now):
                break

            # QUICK_AUGMENT_TTS_V1 — session bump s TTS odpočtem
            # QUICK_AUGMENT_WATCHER_METHOD_V1: extracted to self._handle_quick_augment_flag
            self._handle_quick_augment_flag(ui)

            # # VIDEO_ENROLL_FEED — Video enroll session
            # VIDEO_ENROLL_WATCHER_METHOD_V1: extracted to self._handle_video_enroll_feed
            self._handle_video_enroll_feed(ui, main_frame, lores_frame, boxes)

            if self._enroll.active and self._enroll.countdown_start is not None:
                # GET_FRAMES_METHOD_V1_FIX
                self._tick_enrollment(ui, main_frame, lores_frame,
                                      recognizer, self._get_frames)

        if hasattr(self, '_unknown_tracker'):
            self._unknown_tracker.close()
        if getattr(self, '_hans_dialog', None):
            self._hans_dialog.stop()
        if getattr(self, '_hans_idle', None):
            self._hans_idle.stop()
        if getattr(self, '_kodi_monitor', None):
            self._kodi_monitor.stop()
        if getattr(self, '_voice', None):
            self._voice.stop()
        try:    self._picam2.stop()
        except: pass
        cv2.destroyAllWindows()
        if self.surr_db:
            self.surr_db.close()
        tk_mgr.destroy()
        print("[PicamDisplay] Stopped")
        return 0

    # ── Thumbnail saver ───────────────────────────────────────────────────

    def _save_object_thumbs(self, frame: np.ndarray, named: list):
        h, w = frame.shape[:2]
        for d in named:
            try:
                x1 = max(0, int(d["x1"] * w));  y1 = max(0, int(d["y1"] * h))
                x2 = min(w, int(d["x2"] * w));  y2 = min(h, int(d["y2"] * h))
                if x2 - x1 < 8 or y2 - y1 < 8: continue
                pw = int((x2-x1)*0.20); ph = int((y2-y1)*0.20)
                crop = frame[max(0,y1-ph):min(h,y2+ph),
                             max(0,x1-pw):min(w,x2+pw)]
                crop = cv2.resize(crop, (_THUMB_W, _THUMB_H),
                                  interpolation=cv2.INTER_LINEAR)
                cv2.rectangle(crop, (0,0), (_THUMB_W-1, _THUMB_H-1), (0,200,100), 2)
                cv2.putText(crop, f"{d['class_name']} {d['confidence']:.2f}",
                            (4, _THUMB_H-6), cv2.FONT_HERSHEY_SIMPLEX, 0.4,
                            (0,255,128), 1)
                safe = d["class_name"].replace(" ","_").replace("/","_")
                cv2.imwrite(str(_THUMB_DIR / f"{safe}.jpg"),
                            cv2.cvtColor(crop, cv2.COLOR_RGB2BGR))
            except Exception as e:
                print(f"[Thumb] save error for {d['class_name']}: {e}")

    # ── Servo dispatch ────────────────────────────────────────────────────

    def _update_servo(self, boxes, identities):
        """Track servo to best face target. TODO: add gesture target priority."""
        if not self.servo_controller:
            return
        if getattr(self.servo_controller, 'calibrating', False):
            return  # SERVO_MANUAL_CALIB_V1 — wizard owns the servo
        if not boxes:
            self.servo_controller.update_face_position(None)
            return

        def cx(b): return (b[0] + b[2]) / 2
        def cy(b): return (b[1] + b[3]) / 2
        def area(b): return (b[2]-b[0]) * (b[3]-b[1])

        _unk = ("Unknown", "...", "?", "")
        known   = [(b, i) for b, i in zip(boxes, identities) if i[0] not in _unk]
        unknown = [(b, i) for b, i in zip(boxes, identities) if i[0] in _unk]

        target = None
        if len(known) == 1:
            b = known[0][0]
            target = (int(cx(b)*self._MAIN_W), int(cy(b)*self._MAIN_H))
        elif len(known) > 1:
            target = (int(sum(cx(b) for b,_ in known)/len(known)*self._MAIN_W),
                      int(sum(cy(b) for b,_ in known)/len(known)*self._MAIN_H))
        elif unknown:
            b, _ = max(unknown, key=lambda x: area(x[0]))
            target = (int(cx(b)*self._MAIN_W), int(cy(b)*self._MAIN_H))

        self.servo_controller.update_face_position(target)

    # ── Enrollment overlay ────────────────────────────────────────────────

    def _draw_enrollment_overlay(self, display, dw, dh):
        self._enroll.draw_overlay(display, dw, dh)

    # ── Init: HansIdle + HansDialog + wiring (mood, body, introspection) ─

    def _init_memory(self):
        """T1_MEMORY_SKELETON_V1 — Tulvingovská paměťová fasáda (read-only)."""
        try:
            from scripts.hans_memory import Memory
            # T4_FIX_WIRING_V1 — opravená jména:
            #   surr_db (ne _surroundings_db), conversation_store zatím None
            #   (ConversationStore žije v openwebui handleru — referenci
            #    vytáhneme až v T.5 kde napojíme greeting).
            self._memory = Memory(
                self.config,
                relationships=getattr(self, "_relationships", None),
                kodi_monitor=getattr(self, "_kodi_monitor", None),
                surroundings_db=getattr(self, "surr_db", None),
                conversation_store=None,
            )
        except Exception as e:
            import logging
            logging.getLogger(__name__).error(
                "Memory init failed: %s — pokračuji bez ní.", e
            )
            self._memory = None

        # T5_DIALOG_RECALL_V1 — wire Memory do openwebui handleru
        if getattr(self, "_memory", None) is not None:
            _owc = getattr(self, "openwebui_chat", None)
            # G3A_WIRING_V1 — wire HansKnowledge (RAG query) do openwebui
            # handleru pro grounding (G.3b). _knowledge vzniklo v
            # _init_enrollment_and_voice (běží PŘED _init_memory). ✓
            _kn = getattr(self, '_knowledge', None)
            _owc_k = getattr(self, 'openwebui_chat', None)
            if _kn is not None and _owc_k is not None \
                    and hasattr(_owc_k, 'set_knowledge'):
                try:
                    _owc_k.set_knowledge(_kn)
                    import logging  # G3A_LOGFIX_V1
                    logging.getLogger(__name__).info(
                        'G3A: HansKnowledge wired into openwebui handler')
                except Exception as _kwe:
                    import logging  # G3A_LOGFIX_V1
                    logging.getLogger(__name__).warning(
                        'set_knowledge wire failed: %s', _kwe)
            else:
                import logging  # G3A_LOGFIX_V1
                logging.getLogger(__name__).warning(
                    'G3A: knowledge wiring skipped (kn=%s, owc=%s, has_setter=%s)',
                    _kn is not None, _owc_k is not None,
                    hasattr(_owc_k, 'set_knowledge') if _owc_k else False)

            if _owc is not None and hasattr(_owc, "set_memory"):
                try:
                    _owc.set_memory(self._memory)
                except Exception as _we:
                    import logging
                    logging.getLogger(__name__).warning(
                        "set_memory wire failed: %s", _we)

            # T6_CONSOLIDATE_V1 — wire HansRoutine (přes hans_idle) do Memory.
            # routine žije v self._hans_idle._routine (vzniká v _init_hans_modules,
            # tedy PŘED _init_memory). Memory ji potřebuje pro consolidate().
            _hi = getattr(self, "_hans_idle", None)
            _rt = getattr(_hi, "_routine", None) if _hi is not None else None
            if _rt is not None and hasattr(self._memory, "set_routine"):
                try:
                    self._memory.set_routine(_rt)
                except Exception as _re:
                    import logging
                    logging.getLogger(__name__).warning(
                        "set_routine wire failed: %s", _re)
            # T6B_ENCOUNTER_SUMMARY_V1 — wire synthesis (z hans_idle) do Memory
            _syn = getattr(_hi, "_synthesis", None) if _hi is not None else None
            if _syn is not None and hasattr(self._memory, "set_synthesis"):
                try:
                    self._memory.set_synthesis(_syn)
                except Exception as _se:
                    import logging
                    logging.getLogger(__name__).warning(
                        "set_synthesis wire failed: %s", _se)

    def _init_hans_modules(self):  # INIT_HANS_MODULES_V1
        """Inicializuje HansIdle (background agent) a HansDialog (interactive),
        propaguje state mezi nimi (curiosity, body, introspection) a spustí
        weather→mood background vlákno (hourly).

        Pozn: některé wiring řádky jsou DEAD (`if hasattr(self, '_room_observer')`
        atd.) protože observers vznikají AŽ v _init_observers (volaném po
        této metodě). Zachováno bit-for-bit kvůli původnímu pořadí; opravu
        viz otevřené vlákno."""
        config = self.config
        openwebui_chat = self.openwebui_chat

        # Hans idle mode
        from scripts.hans_idle import HansIdle
        self._hans_idle = HansIdle(config, self.kodi, openwebui_chat)

        # Hans dialog s plysákem
        from scripts.hans_dialog import HansDialog
        _tts = getattr(openwebui_chat, 'tts_speaker', None)
        self._hans_dialog = HansDialog(
            config,
            tts_speaker=_tts,
            diary_db=self._hans_idle._db,
        )
        # propoj curiosity s HansDialog
        if hasattr(self, '_hans_idle') and hasattr(self._hans_idle, '_curiosity'):
            self._hans_dialog._curiosity = self._hans_idle._curiosity
        # propoj room_observer s HansDialog
        if hasattr(self, '_room_observer'):
            self._hans_dialog._room_observer = self._room_observer
        # počasí do mood (jednou za hodinu v background vlákně)
        if hasattr(self._hans_idle, '_mood') and hasattr(self, '_weather'):
            import threading as _thr
            def _wx_mood_loop():
                # HANS_EVENT_API_REWRITE_V1
                import time as _t
                while True:
                    try:
                        wx = self._weather.get_weather()
                        if wx:
                            self._hans_idle.event_weather_changed(
                                wx.get('weathercode', 0))
                    except Exception:
                        pass
                    _t.sleep(3600)
            _thr.Thread(target=_wx_mood_loop, daemon=True).start()
        # propoj hans_idle s HansDialog (pro přístup k _body)
        self._hans_dialog._hans_idle = self._hans_idle

        # SLEEP_MODE_V1 — drátování periferií do routine pro spánek 02:00–09:00
        try:
            _rt = getattr(self._hans_idle, '_routine', None)
            if _rt is not None:
                _tts_for_routine = getattr(openwebui_chat, 'tts_speaker', None)
                if _tts_for_routine is not None and hasattr(_rt, 'set_tts'):
                    _rt.set_tts(_tts_for_routine)
                if self.servo_controller is not None and hasattr(_rt, 'set_servo'):
                    _rt.set_servo(self.servo_controller)
                if hasattr(_rt, 'set_vision'):  # SLEEP_VISION_OFF_V1
                    _rt.set_vision(self)
        except Exception as _e:
            print(f"[DisplayController] routine periph wire failed: {_e}")
        # propoj TTS s HansBody
        if hasattr(self._hans_idle, '_body'):
            _tts = getattr(openwebui_chat, 'tts_speaker', None)
            if _tts:
                self._hans_idle._body.tts_speaker = _tts

        # propoj moduly s HansIntrospection
        if hasattr(self._hans_idle, '_introspection'):
            intr = self._hans_idle._introspection
            intr._room_observer = self._room_observer if hasattr(self, '_room_observer') else None
            intr._weather       = self._weather if hasattr(self, '_weather') else None
            intr._kodi_monitor  = self._kodi_monitor if hasattr(self, '_kodi_monitor') else None
            if hasattr(self._hans_idle, '_curiosity'):
                intr._curiosity = self._hans_idle._curiosity

        # Propoj HansIdle s HansDialog
        self._hans_idle._hans_dialog = self._hans_dialog

        # Predej cluster_db do unknown trackeru
        if hasattr(self, '_unknown_tracker'):
            self._unknown_tracker._cluster_db = self._cluster_db

    # ── Init: enrollment + voice (Relationships, Knowledge, voice setup) ─

    def _init_enrollment_and_voice(self):  # INIT_ENROLLMENT_AND_VOICE_V1
        """Inicializuje poslední blok __init__: thumbnail dir, Relationships
        + HansKnowledge (defenzivně), EnrollmentManager, voice integration.
        Volá se ze __init__ po _init_gesture_and_objects a po wiring sekci."""
        config = self.config
        openwebui_chat = self.openwebui_chat

        _THUMB_DIR.mkdir(parents=True, exist_ok=True)

        # ENROLL_HOOKS_PATCH — Relationships + HansKnowledge pro
        # enroll/delete hooks (soft-delete + RAG re-upload).
        # Defenzivně: pokud import nebo init selže, hooky se přeskočí.
        self._relationships = None
        self._knowledge = None
        try:
            from scripts.hans_relationships import Relationships
            self._relationships = Relationships(config)
            self._relationships.seed_if_empty()
        except Exception as _e:
            print(f"[DisplayController] Relationships init failed: {_e}")
        try:
            from scripts.hans_knowledge import HansKnowledge
            self._knowledge = HansKnowledge(config)
        except Exception as _e:
            print(f"[DisplayController] HansKnowledge init failed: {_e}")

        # Enrollment state machine — viz picam_enrollment.EnrollmentManager
        self._enroll = EnrollmentManager(self)

        # ── Voice integration ─────────────────────────────────────────────
        # HW_CAPABILITIES_V1 — bez mikrofonu (features.voice_input=False) voice
        # nestartuj (jinak by zkoušel otvírat neexistující capture zařízení).
        self._voice = None
        self._voice_identities: list = []   # aktualizováno v hlavní smyčce
        if config.get('features', {}).get('voice_input', True):
            try:
                from scripts.voice_integration import setup_voice
                self._voice = setup_voice(config, openwebui_chat, self)
            except Exception as _ve:
                print(f"[DisplayController] voice init failed (continuing): {_ve}")
        else:
            print("[DisplayController] voice vypnuto (features.voice_input=False — bez mikrofonu)")

    # ── Init: RoomObserver + KodiMonitor (a propagace do openwebui_chat) ─

    def _init_observers(self):  # INIT_OBSERVERS_V1
        """Inicializuje RoomObserver (sledování místnosti přes object detection)
        a KodiMonitor (sledování co hraje + příchody/odchody). Propaguje
        observers do openwebui_chat pro RAG kontext.

        Volá se ze __init__ PO _init_hans_modules (čte self._hans_idle._log_entry,
        propaguje curiosity/mood do KodiMonitor)."""
        config = self.config
        openwebui_chat = self.openwebui_chat

        # Room observer
        from scripts.room_observer import RoomObserver
        self._room_observer = RoomObserver(
            config,
            diary_db_path=config.get('hans_idle', {}).get(
                'diary_db', 'data/hans_diary.db')
        )
        if openwebui_chat:
            openwebui_chat._room_observer = self._room_observer

        # Kodi monitor — sleduje co hraje + příchody/odchody
        from scripts.kodi_monitor import KodiMonitor
        _kodi_db = config.get('kodi', {}).get(
            'monitor_db', 'data/kodi_monitor.db')
        self._kodi_monitor = KodiMonitor(
            self.kodi, _kodi_db,
            poll_interval=float(
                config.get('kodi', {}).get('monitor_interval', 30.0)),
            diary_path=config.get('hans_idle', {}).get(
                'diary_db', 'data/hans_diary.db'),
            # DIARY_WRITER_PROPAGATE_DC
            diary_writer=(self._hans_idle._log_entry if getattr(self, '_hans_idle', None) else None))
        # propoj curiosity s KodiMonitor
        if hasattr(self, '_hans_idle') and hasattr(self._hans_idle, '_curiosity'):
            self._kodi_monitor._curiosity = self._hans_idle._curiosity
        # propoj mood s KodiMonitor
        if hasattr(self, '_hans_idle') and hasattr(self._hans_idle, '_mood'):
            self._kodi_monitor._mood = self._hans_idle._mood
        if openwebui_chat:
            openwebui_chat._kodi_monitor = self._kodi_monitor
            openwebui_chat._hans_idle = self._hans_idle
            openwebui_chat._hans_dialog = self._hans_dialog

    # ── Init: gesture client + object detection + surroundings ─────────

    def _init_gesture_and_objects(self):  # INIT_GESTURE_AND_OBJECTS_V1
        """Inicializuje gesture rozpoznávač, hailo socket connect (s retry),
        object detection client a SurroundingsDB. Propaguje surr_db do
        openwebui_chat pro RAG.

        Pozn: hailo už byl VYTVOŘEN v _init_vision_clients, ale .connect()
        retry loop je tady — zachováno bit-for-bit kvůli relativnímu pořadí
        v původním __init__."""
        config = self.config

        # Gesture recognizer — fires on_gesture callback
        # ON_GESTURE_METHOD_V1: handler je teď metoda self._on_gesture
        self.gesture = GestureClient(config, on_gesture=self._on_gesture)
        self._last_gesture      = None   # str label or None
        self._last_gesture_time = 0.0
        self._last_gesture_bbox = None   # (x1,y1,x2,y2) normalised
        # Gesture data collection
        self._gcollect_label    = None   # aktuální label pro sběr
        self._gcollect_until    = 0.0    # čas konce sběru
        self._gcollect_count    = 0      # počet uložených vzorků

        for attempt in range(10):
            if self.hailo.connect():
                break
            print(f"[HailoClient] Waiting for server... ({attempt+1}/10)")
            time.sleep(1.0)

        self.obj_client = None
        self.surr_db    = None
        if _OBJECT_CLIENT_AVAILABLE:
            self.obj_client = ObjectClient()
            if self.obj_client.connect():
                print("[PicamDisplay] Object detection client connected")
            else:
                print("[PicamDisplay] Object server not available — will retry")
            self.surr_db = SurroundingsDB(config)
            if self.openwebui_chat and hasattr(self.openwebui_chat, 'set_surroundings_db'):
                self.openwebui_chat.set_surroundings_db(self.surr_db)

    # ── Init: Kodi client + weather (a propagace do openwebui_chat) ──────

    def _init_kodi_and_weather(self):  # INIT_KODI_AND_WEATHER_V1
        """Inicializuje KodiClient a WeatherCHMU. Pokud existuje
        openwebui_chat, propaguje do něj weather pro use v RAG promptu.
        Volá se ze __init__ po _init_vision_clients."""
        config = self.config
        openwebui_chat = self.openwebui_chat

        # Kodi client
        self.kodi = KodiClient(config)

        # Pocasi
        from scripts.weather_chmu import WeatherCHMU
        _loc = config.get('weather', {})
        self._weather = WeatherCHMU(
            lat=float(_loc.get('lat', 50.04)),
            lon=float(_loc.get('lon', 15.78)),
        )
        if openwebui_chat:
            openwebui_chat._weather = self._weather

    # ── Init: vision clients (hailo, face DBs, fisheye, track manager) ──

    def _init_vision_clients(self):  # INIT_VISION_CLIENTS_V1
        """Inicializuje vision-related klienty a databáze:
        - HailoClient (face detection + arcface)
        - FaceDB (per-osoba embeddings)
        - PersistentUnknownTracker (pasivní enrollment)
        - FisheyeCorrector (lores undistort)
        - FacePreprocessor (aligned crop)
        - FaceTrackManager (per-frame voting)
        - ClusterFaceDB (cluster-based recognition)
        Volá se ze __init__. Čte self.config, self.database_manager,
        self.openwebui_chat."""
        config = self.config
        database_manager = self.database_manager
        openwebui_chat = self.openwebui_chat

        self.hailo   = HailoClient()
        self.face_db = FaceDB(database_manager, config=config)

        # Persistent unknown tracker — pasivni enrollment
        from scripts.persistent_unknown_tracker import PersistentUnknownTracker
        self._unknown_tracker = PersistentUnknownTracker(
            config, self.face_db, self.hailo, openwebui_chat)

        self.fisheye = FisheyeCorrector(config)
        from scripts.face_preprocess import FacePreprocessor
        self._face_prep = FacePreprocessor(config)

        self._track_mgr = FaceTrackManager(
            stale_timeout  = float(config.get('recognition_tuning', {}).get('track_stale_s', 2.0)),
            decision_after = int(config.get('recognition_tuning', {}).get('decision_after', 5)),
            max_embeddings = int(config.get('recognition_tuning', {}).get('max_track_emb', 20)),
        )
        self._cluster_db = ClusterFaceDB(
            db_path        = config.get('database', {}).get(
                                'faces_cluster_path', 'data/known_faces_cluster.pkl'),
            max_clusters   = int(config.get('recognition_tuning', {}).get('max_clusters', 6)),
            cluster_thresh = float(config.get('recognition_tuning', {}).get('cluster_thresh', 0.25)),
            match_thresh   = float(config.get('recognition_tuning', {}).get('arcface_thresh', 0.40)),
        )
        self._next_track_id = 0
        self._active_tracks: dict = {}

    # ── Video enroll phase callback (LensPosition + TTS prompt) ─────────

    def _video_enroll_on_phase(self, phase):  # ON_PHASE_METHOD_V1
        """Volá se z VideoEnrollSession při přechodu mezi fázemi
        multi-phase enrollmentu. Nastaví fixní LensPosition na kameru
        a přečte TTS prompt ("Stůjte jeden metr od kamery" atd.)."""
        lens = phase.get("lens")
        tts_text = phase.get("tts")
        if lens is not None:
            # AF_PHASE_LOGGER
            import logging as _log_mod
            _ve_log = _log_mod.getLogger("video_enroll")
            _pname = phase.get("name", "?")
            try:
                self._picam2.set_controls({"LensPosition": float(lens)})
                _ve_log.info(
                    "AF phase '%s' set LensPosition=%.3f (=%.2fm)",
                    _pname, float(lens),
                    (1.0 / lens) if lens > 0 else float("inf"))
            except Exception as _le:
                _ve_log.error("AF set lens failed phase=%s: %s",
                              _pname, _le)
        if tts_text:
            _tts = getattr(self.openwebui_chat,
                           "tts_speaker", None)
            if _tts:
                try:
                    _tts.speak(tts_text, priority=True)
                except Exception as _te:
                    print(f"[TTS] speak failed: {_te}")

    # ── Quick augment worker (TTS countdown + capture, runs in thread) ──

    def _quick_augment_worker(self, name, session, ui):  # QUICK_AUGMENT_WORKER_METHOD_V1
        """Background vlákno pro web-triggered quick augment:
        TTS intro + odpočet 10s + recognition guard + capture + závěr.
        Spouští se z hlavní smyčky když existuje data/.quick_augment flag.

        Markery zachované z původní closure:
        - QA_GUARD_V1: skip pokud žádný bbox neodpovídá jménu
        - QUICK_AUGMENT_TTS_V1: TTS countdown
        """
        import time as _t
        import json as _json  # ponechán pro paritu s původní closure
        try:
            _tts = None
            _ow = getattr(self, "openwebui_chat", None)
            if _ow is not None:
                _tts = getattr(_ow, "tts_speaker", None)

            def _say(text):
                if _tts:
                    try:
                        _tts.speak(text, priority=True)
                    except Exception as _e:
                        _syslog.error("[QuickAugment] TTS: %s", _e)

            _session_label = {
                "morning":   "ranní světlo",
                "afternoon": "odpolední světlo",
                "evening":   "večerní bodové světlo",
            }.get(session, session)

            _say(f"Vzorky pro {name}, {_session_label}. "
                 f"Začínám za deset sekund.")
            _t.sleep(4.0)
            for _word in ("Pět.", "Čtyři.", "Tři.",
                          "Dva.", "Jedna.", "Teď."):
                _say(_word)
                _t.sleep(1.0)

            # Capture
            _samples_before = 0
            try:
                _samples_before = len(
                    self.face_db.db_mgr.known_faces.get(name, []))
            except Exception:
                pass
            # QA_GUARD_V1 — najít bbox cílové osoby přes recognition.
            # Pokud žádný bbox neodpovídá jménu, skip + TTS varování.
            _target_box = None
            try:
                if hasattr(self, '_recognizer'):
                    _rb, _rids, _ = self._recognizer.get_identities()
                    if _rb and len(_rb) == len(_rids):
                        _matches = []
                        for _i, (_rn, _rc) in enumerate(_rids):
                            if _rn == name:
                                _matches.append((_rc, _rb[_i]))
                        if _matches:
                            _matches.sort(reverse=True,
                                          key=lambda x: x[0])
                            _target_box = list(_matches[0][1])
            except Exception as _ge:
                _syslog.error("[QuickAugment] guard: %s", _ge)

            if _target_box is None:
                _say("Vidím vás, ale nepoznávám. "
                     "Postavte se prosím sám před kameru "
                     "a zkuste znovu.")
                _syslog.warning(
                    "[QuickAugment] %s/%s: skip — "
                    "žádný bbox neodpovídá",
                    name, session)
                return

            try:
                self._enroll.quick_augment(
                    name, ui, self._get_frames,
                    target_box=_target_box)
            except Exception as _qae:
                _syslog.error("[QuickAugment] capture: %s", _qae)
            _samples_after = 0
            try:
                _samples_after = len(
                    self.face_db.db_mgr.known_faces.get(name, []))
            except Exception:
                pass
            _added = max(0, _samples_after - _samples_before)
            _syslog.info(
                "[QuickAugment] %s/%s: +%d vzorků (%d → %d)",
                name, session, _added, _samples_before, _samples_after)

            if _added > 0:
                _say(f"Hotovo, přidal jsem {_added} vzorků.")
                # Diary entry JEN při úspěchu
                if getattr(self, "_hans_idle", None):
                    try:
                        self._hans_idle.event_face_enroll(
                            name, session, _added)
                    except Exception as _de:
                        _syslog.error(
                            "[QuickAugment] diary: %s", _de)
            else:
                _say("Bohužel jsem nikoho neviděl, zkuste znovu.")
        finally:
            self._quick_augment_running = False

    # ── Gesture callback (open_hand → Kodi, fist → voice) ───────────────

    def _on_gesture(self, gesture, bbox=None):  # ON_GESTURE_METHOD_V1
        """Volá se z GestureClient když rozpozná gesto.
        - open_hand → toggle Kodi play/pause
        - fist → start/stop voice recording
        Aktualizuje self._last_gesture* state pro main loop."""
        self._last_gesture      = gesture
        self._last_gesture_time = time.time()
        self._last_gesture_bbox = bbox
        if gesture == "open_hand":
            # 🖐 Open hand → Kodi pause/play
            if not hasattr(self, "_kodi_playing"):
                self._kodi_playing = True
            if self._kodi_playing:
                if self.kodi.pause():
                    self._kodi_playing = False
                    print("[Gesture] Open hand → Kodi paused")
            else:
                if self.kodi.play():
                    self._kodi_playing = True
                    print("[Gesture] Open hand → Kodi resumed")
        elif gesture == "fist":
            import sys as _sys
            voice = getattr(self, '_voice', None)
            print(f'[Voice] fist handler: voice={voice is not None} recording={getattr(voice, "is_recording", None)} processing={getattr(voice, "_processing", None)}', file=_sys.stderr, flush=True)
            if voice:
                if voice.is_recording:
                    if not getattr(self, "_fist_stop_sent", False):
                        self._fist_stop_sent = True
                        voice.stop_recording()
                        print("[Voice] Fist stop", flush=True)
                else:
                    self._fist_stop_sent = False
                    voice.trigger()

    # ── Settings hot-reload (ConfigGUI on_save + web admin watcher) ──────

    def _on_settings_save(self, cfg):  # ON_SETTINGS_SAVE_METHOD_V1
        """Volá se když uživatel uloží nastavení přes ConfigGUI nebo web admin
        zapíše config.json. Propaguje změny do běžících komponent (fisheye,
        face_prep, gesture, servo_controller) bez nutnosti restartu."""
        self.fisheye.reload_config(cfg)
        self._face_prep.reload_config(cfg)
        self.gesture.reload_config(cfg)
        if self.servo_controller:
            sc = cfg.get('servo_tracking', {})
            self.servo_controller.max_step_degrees     = float(sc.get('max_step_degrees',     8.0))
            self.servo_controller.tracking_sensitivity = float(sc.get('tracking_sensitivity', 1.5))
            self.servo_controller.smoothing_factor     = float(sc.get('smoothing_factor',     0.6))
            self.servo_controller.center_tolerance     = float(sc.get('center_tolerance',    30.0))
            self.servo_controller.scanning_speed       = float(sc.get('scanning_speed',      15.0))
            self.servo_controller.face_lost_timeout    = float(sc.get('face_lost_timeout',    3.0))
            self.servo_controller.scanning_pan_min     = float(sc.get('scanning_pan_min',   -45.0))
            self.servo_controller.scanning_pan_max     = float(sc.get('scanning_pan_max',    45.0))

    # ── Mouse callback (GUI mód) ─────────────────────────────────────────

    def _on_mouse(self, event, x, y, flags, param):  # ON_MOUSE_METHOD_V1
        """cv2 setMouseCallback handler. Levý klik uloží souřadnice
        do self._click_xy_pending; hot loop je zpracuje (click-to-enroll)."""
        if event == cv2.EVENT_LBUTTONDOWN:
            self._click_xy_pending = (x, y)

    # ── Picamera2 frame callback ─────────────────────────────────────────

    def _frame_cb(self, req):  # FRAME_CB_METHOD_V1
        """Picamera2 pre_callback — kopíruje main a lores ze zachyceného requestu
        a strká je do self._frame_q. Drží jen poslední 2 framy.
        POZOR: NEgatovat tady — main loop dělá 'if main_frame is None: continue',
        takže zastavení framů zacyklí smyčku před UI/ESC. Gate je na recognition
        bloku (SLEEP_VISION_OFF_V1)."""
        try:
            main  = req.make_array('main').copy()
            lores = req.make_array('lores').copy()
            while self._frame_q.qsize() > 2:
                try: self._frame_q.get_nowait()
                except: pass
            self._frame_q.put((main, lores))
        except Exception:
            pass

    def pause_vision(self):  # SLEEP_VISION_OFF_V1 — gate-only (SLEEP_VISION_HANG_FIX)
        """Spánek: zastav recognition přes frame-gate (framy nepřitečou do fronty).
        Hardware picam2.stop() ZÁMĚRNĚ vynechán — volá se z chat threadu souběžně
        s main loopem, který sahá na picam2 (_update_af aj.); picamera2 NENÍ
        thread-safe → cross-thread stop zatuhl celý proces. Sensor-off je
        follow-up (nutná koordinace: stop/start z main loop threadu)."""
        self._vision_paused = True
        print("[Sleep] vision pozastaven (frame-gate, recognition off)")

    def resume_vision(self):  # SLEEP_VISION_OFF_V1
        """Probuzení: pusť frame-gate — framy zas tečou, recognition běží."""
        self._vision_paused = False
        print("[Sleep] vision obnoven (frame-gate off)")

    # ── Terminal input (headless) ─────────────────────────────────────────

    def _term_reader(self):  # TERM_READER_METHOD_V1
        """Čte řádky ze stdin a strká je do self._term_q.
        Běží v daemon vlákně, spouští se jen v headless módu."""
        while True:
            try:
                line = input().strip().lower()
                if self._term_q is not None:
                    self._term_q.put(line)
            except EOFError:
                break

    # ── Frame queue ───────────────────────────────────────────────────────

    def _get_frames(self):  # GET_FRAMES_METHOD_V1
        """Vrací (main, lores) z _frame_q s timeoutem 0.2s, jinak (None, None)."""
        try:
            return self._frame_q.get(timeout=0.2)
        except Exception:
            return None, None

    # ── Key handlers ──────────────────────────────────────────────────────

    def _handle_persistent_unknown(self, main_frame, boxes, identities,
                                    hailo_results, frame_idx):  # PERSISTENT_UNKNOWN_METHOD_V1
        if not hasattr(self, '_unknown_tracker'):
            return
        if main_frame is None or not boxes or hailo_results is None:
            return
        if frame_idx % DETECT_EVERY != 0:
            return
        self._unknown_tracker.process(
            main_frame, boxes, identities,
            hailo_results, self._active_tracks)

    def _compute_face_visible_and_af(self, boxes, identities, frame_idx):  # FACE_VISIBLE_AF_METHOD_V1
        face_visible = (frame_idx % DETECT_EVERY == 0 and len(boxes) > 0)
        if frame_idx % DETECT_EVERY != 0:
            return face_visible
        # AF priorita: neznama > znama
        _af_boxes = boxes
        _unknown_boxes = [
            b for b, (n, c) in zip(boxes, identities)
            if n in ("Unknown", "...", "?", "")]
        if _unknown_boxes:
            # Zaostri na NEJMENSI neznámou tvár (nejdale = nejmensi box)
            _af_boxes = [min(_unknown_boxes,
                key=lambda b: (b[2]-b[0])*(b[3]-b[1]))]
        elif (hasattr(self, "_unknown_tracker") and
                self._unknown_tracker._active_session is not None):
            _sess = self._unknown_tracker._active_session
            if _sess.bbox_hist:
                _af_boxes = [_sess.bbox_hist[-1]]
        _update_af(self._picam2, bool(boxes), self.config, _af_boxes)
        return face_visible

    def _handle_gesture_collection(self, ui, now):  # GESTURE_COLLECTION_METHOD_V1
        if (self._gcollect_label and now < self._gcollect_until
                and self.gesture.last_landmarks is not None):
            import json as _json
            from pathlib import Path as _Path
            _lm = self.gesture.last_landmarks
            if not all(v == 0.0 for v in _lm):
                # Normalizuj landmarks
                import numpy as _np
                _lm_arr = _np.array(_lm, dtype=_np.float32).reshape(21, 3)
                _lm_arr -= _lm_arr[0]
                _scale = _np.linalg.norm(_lm_arr[9])
                if _scale > 1e-6:
                    _lm_arr /= _scale
                _entry = {'label': self._gcollect_label,
                          'landmarks': _lm_arr.flatten().tolist()}
                _data_file = _Path("data/gesture_landmarks.jsonl")
                _data_file.parent.mkdir(exist_ok=True)
                with open(_data_file, 'a') as _f:
                    _f.write(_json.dumps(_entry) + '\n')
                self._gcollect_count += 1
        elif self._gcollect_label and now >= self._gcollect_until:
            ui.toast(f"Gesture collect: {self._gcollect_label} "
                     f"— {self._gcollect_count} vzorků uloženo",
                     duration=3.0, color=(0, 220, 0))
            self._gcollect_label = None
            self._gcollect_count = 0

    def _handle_click_to_enroll(self, ui, boxes, display):  # CLICK_TO_ENROLL_METHOD_V1
        # Zpracování pending kliku
        if self._click_xy_pending is not None and self._preview_on:  # HANS_MENU_V1
            cx_px, cy_px = self._click_xy_pending
            self._click_xy_pending = None
            dh_, dw_ = display.shape[:2]
            # Pixel → normalized
            cx_n = cx_px / dw_ if dw_ else 0.0
            cy_n = cy_px / dh_ if dh_ else 0.0
            # Najdi bbox, do kterého klik dopadl (boxes jsou normalized)
            hit_box = None
            for b in boxes:
                if (b[0] <= cx_n <= b[2]) and (b[1] <= cy_n <= b[3]):
                    hit_box = b
                    break
            now_t = time.time()
            if hit_box is None:
                # Klik mimo všechny bboxy — zruš výběr
                if self._click_target_box is not None:
                    ui.toast("Selection cleared")
                self._click_target_box  = None
                self._click_target_time = 0.0
            else:
                # Stejný bbox jako minule a do 3s? → spusť enrollment
                same_box = False
                if self._click_target_box is not None:
                    tb = self._click_target_box
                    # IoU > 0.5 ≈ stejný obličej
                    ix1 = max(tb[0], hit_box[0])
                    iy1 = max(tb[1], hit_box[1])
                    ix2 = min(tb[2], hit_box[2])
                    iy2 = min(tb[3], hit_box[3])
                    iw  = max(0.0, ix2 - ix1)
                    ih  = max(0.0, iy2 - iy1)
                    inter = iw * ih
                    a1 = (tb[2]-tb[0])*(tb[3]-tb[1])
                    a2 = (hit_box[2]-hit_box[0])*(hit_box[3]-hit_box[1])
                    union = a1 + a2 - inter
                    iou = inter / union if union > 0 else 0.0
                    if (iou > 0.5
                        and now_t - self._click_target_time <= 3.0):
                        same_box = True
                if same_box:
                    # 2. klik — quick augment / fallback na full enroll
                    # TARGET_BOX_V1: zachovat klepnutý bbox a předat dál,
                    # ať se vzorky přidají JEN té klepnuté osobě
                    _click_box = list(self._click_target_box) if self._click_target_box else None
                    self._click_target_box  = None
                    self._click_target_time = 0.0
                    self._enroll.start_via_click(
                        ui, self._reset_ema, self._get_frames,
                        target_box=_click_box)
                else:
                    # 1. klik — vyber tento bbox a počkej na potvrzení
                    self._click_target_box  = hit_box[:]
                    self._click_target_time = now_t
                    ui.toast("Click again to enroll",
                             color=(0, 200, 255))

        # 3s timeout pro pending výběr
        if (self._click_target_box is not None
                and time.time() - self._click_target_time > 3.0):
            self._click_target_box  = None
            self._click_target_time = 0.0

        # Vizuální feedback — žlutý rámeček kolem vybraného bboxu
        if self._click_target_box is not None:
            dh_, dw_ = display.shape[:2]
            tb = self._click_target_box
            tx1 = int(tb[0] * dw_); ty1 = int(tb[1] * dh_)
            tx2 = int(tb[2] * dw_); ty2 = int(tb[3] * dh_)
            cv2.rectangle(display, (tx1-3, ty1-3), (tx2+3, ty2+3),
                          (0, 220, 255), 3)
            cv2.putText(display, "Click again to enroll",
                        (tx1, max(ty1 - 8, 14)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.45,
                        (0, 220, 255), 1)

    def _handle_keys(self, ui, boxes, identities, now):  # KEY_DISPATCH_METHOD_V1
        """Dispatch klávesových akcí. Vrátí False pro ESC (= break loop), jinak True."""
        raw_key = (cv2.waitKey(1) & 0xFF) if self._preview_on else 0xFF  # HANS_MENU_V1

        # SERVO_MANUAL_CALIB_V1 — modal: wizard owns all keys while active
        # (must run before the ESC/quit check so ESC cancels calib, not the app).
        if self._calib is not None:
            self._handle_calib_key(raw_key, ui)
            return True

        if ui.handle_key(raw_key):
            return True
        if raw_key == 27:
            return False
        if raw_key in (ord('k'), ord('K')):
            self._calib_start(ui)
        if raw_key in (ord('e'), ord('E')):
            self._key_enroll(ui, self._reset_ema)
        elif raw_key == ord(' '):
            self._key_space(ui, boxes)
        elif raw_key in (ord('d'), ord('D')):
            self._key_delete(ui, self._reset_ema)
        elif raw_key in (ord('l'), ord('L')):
            names = self.face_db.list_faces()
            if names:
                counts = self.face_db.get_sample_counts()
                items  = [f"{n}  ({counts.get(n, 0)} samples)"
                          for n in sorted(names)]
                ui.show_list(f"Known faces ({len(names)})", items)
            else:
                ui.toast("No faces enrolled yet", color=(0, 140, 255))
        # CONFIG_GUI_REMOVED_V1 — klávesa S (Settings) odstraněna; konfig přes webadmin.
        elif raw_key in (ord('g'), ord('G')):
            self._gcollect_label = 'open_hand'
            self._gcollect_until = now + 5.0
            self._gcollect_count = 0
            ui.toast("Collecting: open_hand — ukazuj otevřenou dlaň 5s",
                     duration=5.0, color=(0, 200, 255))
        elif raw_key in (ord('h'), ord('H')):
            self._gcollect_label = 'thumbs_up'
            self._gcollect_until = now + 5.0
            self._gcollect_count = 0
            ui.toast("Collecting: fist — ukazuj zavřenou pěst 5s",
                     duration=5.0, color=(0, 200, 255))
        elif raw_key in (ord('j'), ord('J')):
            self._gcollect_label = 'none'
            self._gcollect_until = now + 5.0
            self._gcollect_count = 0
            ui.toast("Collecting: none — drž ruku klidně nebo odlož 5s",
                     duration=5.0, color=(0, 200, 255))
        elif raw_key in (ord('c'), ord('C')):
            if self._enroll.cancel(ui):
                pass
            else:
                if self.openwebui_chat:
                    known_visible = [
                        n for n, conf in identities
                        if n not in ("Unknown", "Person", "...", "?", "")
                    ]
                    target_name = (known_visible[0] if known_visible
                                   else (self.face_db.list_faces() or [None])[0])
                    if target_name:
                        from scripts.popup_chat_window import SimplePopupChat
                        SimplePopupChat(self.openwebui_chat, target_name,
                                        1.0, already_greeted=True)
                        ui.toast(f"Chat opened for '{target_name}'",
                                 color=(137, 180, 250))
                    else:
                        ui.toast("No faces enrolled — enroll someone first",
                                 color=(0, 140, 255))
                else:
                    ui.toast("Chat not available", color=(0, 140, 255))
        return True

    # ── SERVO_MANUAL_CALIB_V1 — interactive servo calibration wizard ─────────
    # Each phase: jog with arrows / WASD, SPACE captures the value, +/- changes
    # the step. Order: pan max, pan min, pan center, tilt max, tilt min, tilt
    # center. On the last SPACE the result is written to config.json.
    _CALIB_PHASES = [
        ('pan_max',    'PAN: najeď zcela VPRAVO (max)',  'pan'),
        ('pan_min',    'PAN: najeď zcela VLEVO (min)',   'pan'),
        ('pan_center', 'PAN: najeď na STŘED',            'pan'),
        ('tilt_max',   'TILT: najeď NAHORU (max)',       'tilt'),
        ('tilt_min',   'TILT: najeď DOLŮ (min)',         'tilt'),
        ('tilt_center','TILT: najeď na STŘED',           'tilt'),
    ]

    def _calib_start(self, ui):
        sc = self.servo_controller
        if not sc or not getattr(sc, 'pan_servo', None) or not getattr(sc, 'tilt_servo', None):
            ui.toast("Servo není dostupné — kalibrace nelze spustit", color=(0, 140, 255))
            return
        sc.calib_begin()
        p = float(sc.current_pan or 0.0)
        t = float(sc.current_tilt or 0.0)
        self._calib = {'i': 0, 'step': 5.0, 'pan': p, 'tilt': t, 'cap': {}}
        sc.calib_set(p, t)
        self._calib_prompt(ui)

    def _calib_prompt(self, ui):
        c = self._calib
        _, label, _ = self._CALIB_PHASES[c['i']]
        ui.set_banner(
            f"KALIBRACE [{c['i']+1}/{len(self._CALIB_PHASES)}] {label}   "
            f"| šipky/WASD=pohyb  +/-=krok({c['step']:.0f}°)  "
            f"MEZERA=potvrď  ESC=zruš   pan={c['pan']:+.0f}° tilt={c['tilt']:+.0f}°",
            color=(0, 255, 128))

    def _calib_finish(self, ui, save):
        sc = self.servo_controller
        ok = True
        if save:
            cap = self._calib['cap']
            ok = sc.apply_and_save_calibration(
                cap['pan_min'], cap['pan_max'], cap['pan_center'],
                cap['tilt_min'], cap['tilt_max'], cap['tilt_center'])
        self._calib = None
        ui.clear_banner()
        try:
            sc.calib_end()
            sc.move_to_center()
        except Exception:
            pass
        if not save:
            ui.toast("Kalibrace zrušena (neuloženo)", color=(0, 140, 255))
        else:
            ui.toast("Kalibrace uložena ✓ — píše se do config.json" if ok
                     else "Kalibrace: ZÁPIS SELHAL (viz log)",
                     color=(0, 220, 0) if ok else (0, 140, 255), duration=4.0)

    def _handle_calib_key(self, raw_key, ui):
        c = self._calib
        sc = self.servo_controller
        if raw_key in (27, ord('k'), ord('K')):      # ESC / K — cancel
            self._calib_finish(ui, save=False)
            return
        moved = False
        if raw_key in (81, ord('a'), ord('A')):       # left
            c['pan'] -= c['step']; moved = True
        elif raw_key in (83, ord('d'), ord('D')):     # right
            c['pan'] += c['step']; moved = True
        elif raw_key in (82, ord('w'), ord('W')):     # up
            c['tilt'] += c['step']; moved = True
        elif raw_key in (84, ord('s'), ord('S')):     # down
            c['tilt'] -= c['step']; moved = True
        elif raw_key in (ord('+'), ord('=')):
            c['step'] = min(15.0, c['step'] + 1.0)
        elif raw_key in (ord('-'), ord('_')):
            c['step'] = max(1.0, c['step'] - 1.0)
        elif raw_key == ord(' '):                     # capture current value
            key, _, axis = self._CALIB_PHASES[c['i']]
            c['cap'][key] = c['pan'] if axis == 'pan' else c['tilt']
            c['i'] += 1
            if c['i'] >= len(self._CALIB_PHASES):
                self._calib_finish(ui, save=True)
                return
            self._calib_prompt(ui)
            return
        else:
            return  # ignore other keys (incl. 0xFF no-key)
        if moved:
            c['pan'], c['tilt'] = sc.calib_set(c['pan'], c['tilt'])
        self._calib_prompt(ui)

    def _handle_quick_augment_flag(self, ui):  # QUICK_AUGMENT_WATCHER_METHOD_V1
        """Watcher pro flag data/.quick_augment. Spouští worker v daemon threadu."""
        try:
            _qa_flag = Path("data/.quick_augment")
            if _qa_flag.exists() and not getattr(self, "_quick_augment_running", False):
                try:
                    _qa_spec = _qa_flag.read_text().strip().split("|")
                    _qa_name = _qa_spec[0]
                    _qa_session = _qa_spec[1] if len(_qa_spec) > 1 else "unknown"
                    _qa_flag.unlink()  # single-shot

                    # Kolize: jiný enroll běží → log + skip
                    if getattr(self._enroll, "active", False):
                        _syslog.warning(
                            "[QuickAugment] Skip — jiný enroll běží (%s)",
                            self._enroll.name)
                    else:
                        self._quick_augment_running = True

                        # QUICK_AUGMENT_WORKER_METHOD_V1: worker je teď self._quick_augment_worker
                        import threading as _threading
                        _threading.Thread(
                            target=self._quick_augment_worker,
                            args=(_qa_name, _qa_session, ui),
                            daemon=True,
                        ).start()
                except Exception as _qe:
                    _syslog.error("[QuickAugment] Chyba flagu: %s", _qe)
                    self._quick_augment_running = False
                    try:
                        _qa_flag.unlink(missing_ok=True)
                    except Exception:
                        pass
        except Exception:
            pass

    def _handle_video_enroll_feed(self, ui, main_frame, lores_frame, boxes):  # VIDEO_ENROLL_WATCHER_METHOD_V1
        """Watcher pro flag data/.video_enroll. Spustí session a per-frame ji krmí."""
        try:
            _ve_flag = Path("data/.video_enroll")
            if _ve_flag.exists() and not getattr(self, "_video_enroll", None):
                try:
                    _spec = _ve_flag.read_text().strip().split("|")
                    _ve_name = _spec[0]
                    _ve_secs = int(_spec[1]) if len(_spec) > 1 else 30
                    _ve_flag.unlink()
                    # VIDEO_ENROLL_PHASE_CALLBACK
                    from scripts.video_enroll import VideoEnrollSession
                    # Multi-phase setup pokud spec obsahuje "multi"
                    _phases = None
                    if "multi" in _ve_name or _ve_secs == 0:
                        # Format: "name|0|multi" → multi-phase mode
                        _real_name = _ve_name.replace("multi:", "").strip()
                        _ve_name = _real_name or _ve_name
                        _phases = [
                            {"name": "close", "seconds": 30, "lens": 1.4,
                             "tts": "Stůjte prosím jeden metr od kamery."},
                            {"name": "mid", "seconds": 30, "lens": 0.67,
                             "tts": "Nyní ustupte na dva metry."},
                            {"name": "far", "seconds": 45, "lens": 0.40,
                             "tts": "A nakonec tři metry."},
                        ]
                        print(f"[VideoEnroll] multi-phase mode for {_ve_name}")

                    # ON_PHASE_METHOD_V1: callback je teď self._video_enroll_on_phase
                    self._video_enroll = VideoEnrollSession(
                        name=_ve_name, duration_s=_ve_secs,
                        enroll_manager=self._enroll, ui=ui,
                        phases=_phases, on_phase_start=self._video_enroll_on_phase)
                    self._video_enroll.start()
                    print(f"[VideoEnroll] start: {_ve_name} {_ve_secs}s")
                except Exception as _ve:
                    print(f"[VideoEnroll] start failed: {_ve}")
            if getattr(self, "_video_enroll", None):
                _ve_alive = self._video_enroll.feed(
                    main_frame, lores_frame, len(boxes))
                if not _ve_alive:
                    self._video_enroll = None
        except Exception as _ve_err:
            print(f"[VideoEnroll] loop error: {_ve_err}")

    def _draw_gesture_overlay(self, display, dw, dh):  # DRAW_GESTURE_OVERLAY_METHOD_V1
        """Vykreslí hand skeleton, palm bbox (debug) a gesture bbox/label do display."""
        # ── Draw hand skeleton ───────────────────────────────
        if self._last_gesture:
            _draw_hand_skeleton(display,
                                self.gesture.last_landmarks,
                                self._last_gesture_bbox)
        # ── Draw palm bbox always (debug) ─────────────────
        if (self._last_gesture_bbox and
                all(v > 0 for v in self._last_gesture_bbox)):
            _pb = self._last_gesture_bbox
            _px1=int(_pb[0]*dw); _py1=int(_pb[1]*dh)
            _px2=int(_pb[2]*dw); _py2=int(_pb[3]*dh)
            cv2.rectangle(display,(_px1,_py1),(_px2,_py2),(0,80,255),1)

        # ── Draw gesture bbox + label ─────────────────────
        if self._last_gesture:
            GESTURE_COLOR = (255, 100, 0)
            g_text = f'{self._last_gesture}'
            # Draw bbox if available
            if self._last_gesture_bbox and all(v > 0 for v in self._last_gesture_bbox):
                gx1 = int(self._last_gesture_bbox[0] * dw)
                gy1 = int(self._last_gesture_bbox[1] * dh)
                gx2 = int(self._last_gesture_bbox[2] * dw)
                gy2 = int(self._last_gesture_bbox[3] * dh)
                cv2.rectangle(display,
                              (gx1, gy1), (gx2, gy2),
                              GESTURE_COLOR, 2)
                cv2.putText(display, g_text,
                            (gx1, max(gy1 - 8, 20)),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.65,
                            GESTURE_COLOR, 2)
            else:
                # Fallback — corner label
                (gw, gh), _ = cv2.getTextSize(
                    g_text, cv2.FONT_HERSHEY_SIMPLEX, 0.75, 2)
                cv2.rectangle(display,
                              (6, dh - 80), (16 + gw, dh - 50),
                              (0, 0, 0), -1)
                cv2.putText(display, g_text, (10, dh - 58),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.75,
                            GESTURE_COLOR, 2)
            (gw, gh), _ = cv2.getTextSize(
                g_text, cv2.FONT_HERSHEY_SIMPLEX, 0.75, 2)
            cv2.rectangle(display,
                          (6, dh - 80),
                          (16 + gw, dh - 50),
                          (0, 0, 0), -1)
            cv2.putText(display, g_text,
                        (10, dh - 58),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.75,
                        GESTURE_COLOR, 2)

    def _draw_hand_pip(self, display, main_frame, dh):  # DRAW_HAND_PIP_METHOD_V1
        """Vykreslí PIP s cropem ruky (z _last_gesture_bbox) vlevo dole."""
        _hand_pip_size = 160
        _hand_pip_margin = 10
        if not (self._last_gesture_bbox and
                all(v > 0 for v in self._last_gesture_bbox) and
                main_frame is not None):
            return
        _hb = self._last_gesture_bbox
        _hH, _hW = main_frame.shape[:2]
        # Asymetrický padding — stejný jako server
        _bw = _hb[2] - _hb[0]; _bh = _hb[3] - _hb[1]
        _hx1 = max(0,   int((_hb[0] - _bw * 0.5) * _hW))
        _hy1 = max(0,   int((_hb[1] - _bh * 2.0) * _hH))
        _hx2 = min(_hW, int((_hb[2] + _bw * 0.5) * _hW))
        _hy2 = min(_hH, int((_hb[3] + _bh * 0.5) * _hH))
        if not (_hx2 > _hx1 and _hy2 > _hy1):
            return
        _hand_crop = main_frame[_hy1:_hy2, _hx1:_hx2]
        _hand_pip  = cv2.resize(_hand_crop,
                                (_hand_pip_size, _hand_pip_size),
                                interpolation=cv2.INTER_LINEAR)
        # Vlevo dole
        _hpx1 = _hand_pip_margin
        _hpy1 = dh - _hand_pip_size - _hand_pip_margin - 50
        _hpx2 = _hpx1 + _hand_pip_size
        _hpy2 = _hpy1 + _hand_pip_size
        if _hpy1 <= 0:
            return
        _roi = display[_hpy1:_hpy2, _hpx1:_hpx2]
        cv2.addWeighted(_hand_pip, 0.9, _roi, 0.1, 0, _roi)
        display[_hpy1:_hpy2, _hpx1:_hpx2] = _roi
        # Barevný rámeček podle gesta
        _gcol = (255,100,0) if self._last_gesture else (0,200,255)
        cv2.rectangle(display,
                      (_hpx1-2, _hpy1-2),
                      (_hpx2+2, _hpy2+2),
                      _gcol, 2)
        _glbl = self._last_gesture or "hand"
        cv2.putText(display, _glbl,
                    (_hpx1, _hpy1-6),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.45,
                    _gcol, 1)

    def _draw_hud(self, display, dh, fps, n_face, obj_label):  # DRAW_HUD_METHOD_V1
        """FPS + face count (top-left), obj_label (under FPS), legenda (bottom)."""
        cv2.putText(display,
                    f"FPS:{fps}  Face:{n_face}",
                    (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 200, 255), 2)
        if obj_label:
            (lw, lh), _ = cv2.getTextSize(
                obj_label, cv2.FONT_HERSHEY_SIMPLEX, 0.7, 2)
            cv2.rectangle(display, (6, 42), (16 + lw, 70), (0, 0, 0), -1)
            cv2.putText(display, obj_label, (10, 65),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 100), 2)
        cv2.putText(display,
                    "Green=known  Orange=unknown  Blue=tracking",
                    (10, dh - 30), cv2.FONT_HERSHEY_SIMPLEX, 0.45,
                    (180, 180, 180), 1)

    def _reset_ema(self):  # RESET_EMA_METHOD_V1
        """Vyčistí EMA sloty AsyncRecognizeru. Volá se z enroll/delete/tick."""
        self._recognizer._slots.clear()

    def _key_enroll(self, ui, reset_ema):
        self._enroll.start_via_key(ui, reset_ema)

    def _key_space(self, ui, boxes):
        self._enroll.start_capture(ui, boxes)

    def _key_delete(self, ui, reset_ema):
        self._enroll.delete_via_key(ui, reset_ema)

    # ── HANS_MENU_V1 — menu-driven actions (work without preview) ──────────
    def menu_set_preview(self, on):
        self._preview_on = bool(on)

    def menu_toggle_preview(self):
        self._preview_on = not self._preview_on
        return self._preview_on

    def menu_current_person(self):
        """HANS_MENU_PRESELECT_V1 — nejnovější rozpoznaná známá osoba
        (čerstvá ≤ 12 s), jinak None."""
        if self._present_known and (time.time() - self._present_known_ts) < 12.0:
            return self._present_known[0]
        return None

    def menu_enroll(self):
        """Enroll potřebuje živé video → zapne preview + spustí countdown."""
        self._preview_on = True
        ui = getattr(self, "_ui", None)
        if ui is not None:
            self._key_enroll(ui, self._reset_ema)

    def menu_delete(self, name=None):
        if name:
            try:
                self.face_db.remove(name)
                self._reset_ema()
                return True
            except Exception as e:
                print(f"[HansMenu] delete '{name}' failed: {e}")
                return False
        ui = getattr(self, "_ui", None)
        if ui is not None:
            self._preview_on = True
            self._key_delete(ui, self._reset_ema)
        return True

    def menu_list_faces(self):
        try:
            names = self.face_db.list_faces() or []
            counts = self.face_db.get_sample_counts() if names else {}
            return [(n, counts.get(n, 0)) for n in sorted(names)]
        except Exception:
            return []

    def menu_open_chat(self, name):
        """Chat preview NEpotřebuje — jen jméno naučené osoby."""
        if not (self.openwebui_chat and name):
            return False
        try:
            from scripts.popup_chat_window import SimplePopupChat
            SimplePopupChat(self.openwebui_chat, name, 1.0, already_greeted=True)
            return True
        except Exception as e:
            print(f"[HansMenu] chat open failed: {e}")
            return False

    # ── Enrollment tick ───────────────────────────────────────────────────

    def _tick_enrollment(self, ui, main_frame, lores_frame, recognizer, get_frames):
        self._enroll.tick(ui, main_frame, lores_frame, recognizer, get_frames)

    # _capture_hq_embedding — přesunuto do EnrollmentManager

    # _open_review_window — přesunuto do EnrollmentManager

    @staticmethod
    def _box_key(box, precision=2):
        cx = round((box[0] + box[2]) / 2, precision)
        cy = round((box[1] + box[3]) / 2, precision)
        return (cx, cy)

    def _assign_track_ids(self, boxes: list, prev: dict) -> dict:
        if not boxes:
            return {}
        new_map = {}
        used_tids = set()
        for box in boxes:
            bkey = self._box_key(box)
            best_tid  = None
            best_dist = 0.15
            cur_cx = (box[0] + box[2]) / 2
            cur_cy = (box[1] + box[3]) / 2
            for old_key, tid in prev.items():
                if tid in used_tids:
                    continue
                old_cx, old_cy = old_key
                dist = ((old_cx - cur_cx)**2 + (old_cy - cur_cy)**2) ** 0.5
                if dist < best_dist:
                    best_dist = dist
                    best_tid  = tid
            if best_tid is not None:
                used_tids.add(best_tid)
                new_map[bkey] = best_tid
            else:
                new_map[bkey] = self._next_track_id
                self._next_track_id += 1
        return new_map

    @staticmethod
    def _aligned_crop(frame, box, H, W, bw, bh):
        try:
            from scripts.async_recognizer import _ARCFACE_REF
            x1,y1,x2,y2 = box
            bx1=x1*W; by1=y1*H; bx2=x2*W; by2=y2*H
            fw=bx2-bx1; fh=by2-by1
            if fw < 20 or fh < 20: return None
            pts = np.array([
                [bx1+fw*0.30, by1+fh*0.37], [bx1+fw*0.70, by1+fh*0.37],
                [bx1+fw*0.50, by1+fh*0.55], [bx1+fw*0.35, by1+fh*0.75],
                [bx1+fw*0.65, by1+fh*0.75],
            ], dtype=np.float32)
            M, _ = cv2.estimateAffinePartial2D(pts, _ARCFACE_REF, method=cv2.LMEDS)
            if M is None: return None
            return cv2.warpAffine(frame, M, (ARCFACE_SIZE, ARCFACE_SIZE),
                                  flags=cv2.INTER_LINEAR,
                                  borderMode=cv2.BORDER_REPLICATE)
        except Exception:
            return None

    # _reset_enrollment — přesunuto do EnrollmentManager.reset()
