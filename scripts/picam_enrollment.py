"""
Enrollment state machine pro PicamDisplayController.

Spravuje:
  - capture session (countdown → snap → countdown → snap × N)
  - aligned ArcFace embedding extraction
  - review window dialog
  - HQ embedding capture s fallbackem
"""
import shutil
import time
import uuid
from pathlib import Path

import cv2
import numpy as np

from scripts.hailo_client import LABEL_FACE, ARCFACE_SIZE


_MAX_CAPTURE_RETRIES = 6


class EnrollmentManager:
    """
    State machine pro manuální enrollment přes klávesy E/SPACE.

    Použití:
        em = EnrollmentManager(controller)
        em.start_via_key(ui)        # E key
        em.start_capture(ui, boxes) # SPACE key
        em.tick(ui, main, lores, recognizer, get_frames)  # každý frame
        em.draw_overlay(display, dw, dh)
        em.cancel(ui)
    """

    # ── Konstanty countdownu ─────────────────────────────────────────────────
    FIRST_COUNTDOWN = 5
    NEXT_COUNTDOWN  = 2

    def __init__(self, controller):
        """
        Args:
          controller: PicamDisplayController — pro přístup k face_db, hailo,
                      cluster_db, _face_prep, _aligned_crop, config.
        """
        self.ctrl = controller
        cfg = controller.config.get("enrollment", {})
        self.total_samples = int(cfg.get("enrollment_frames", 11))

        self.name             = None
        self.samples          = []
        self.crops            = []
        self.attempts         = 0
        self.countdown_start  = None

    # ── Stav ─────────────────────────────────────────────────────────────────

    @property
    def active(self) -> bool:
        return self.name is not None

    @property
    def accepted(self) -> int:
        return len(self.samples)

    def reset(self):
        self.name             = None
        self.samples          = []
        self.crops            = []
        self.attempts         = 0
        self.countdown_start  = None

    # ── Klávesy ──────────────────────────────────────────────────────────────

    def start_via_key(self, ui, reset_ema):
        """E key — request name, start enrollment."""
        if self.active:
            ui.toast(f"Already enrolling '{self.name}'", color=(0, 140, 255))
            return

        def _on_name(name):
            name = name.strip()
            if not name:
                ui.toast("No name entered — cancelled")
                return
            if name in self.ctrl.face_db.list_faces():
                ui.show_confirm(
                    f"'{name}' exists. Replace?",
                    on_yes=lambda: self._begin(name, ui, reset_ema),
                    on_no=lambda: ui.toast("Cancelled"))
            else:
                self._begin(name, ui, reset_ema)

        ui.show_input("Enter name to enroll:", on_confirm=_on_name)

    def _begin(self, name, ui, reset_ema):
        self.name             = name
        self.samples          = []
        self.crops            = []
        self.attempts         = 0
        self.countdown_start  = None
        reset_ema()
        ui.toast(f"Enrolling '{name}' — stand at MAX distance, then SPACE",
                 duration=4.0, color=(0, 200, 255))

    # ── Quick augment (click-to-enroll pro existující osoby) ─────────────────

    QUICK_AUGMENT_SAMPLES   = 5
    QUICK_AUGMENT_INTERVAL  = 0.15   # sekundy mezi snímky

    def start_via_click(self, ui, reset_ema, get_frames, target_box=None):
        """Spuštěno z double-click do bboxu.

        UNKNOWN_QUICK_ADD_V1 — sjednocený rychlý flow:
        1. Capture 5 vzorků HNED (před dialogem) s target_box pro cílení
        2. Dialog 'Enter name' — uživatel pojmenuje
        3. Existující jméno → přidá k němu; nové → vytvoří

        Klávesa E (start_via_key) zachovává full 3-pose enroll.
        """
        if self.active:
            ui.toast(f"Already enrolling '{self.name}'", color=(0, 140, 255))
            return

        # 1) Pre-capture 5 vzorků do paměti
        n_target = self.QUICK_AUGMENT_SAMPLES
        interval = self.QUICK_AUGMENT_INTERVAL
        ui.toast(f"Capturing {n_target} samples...",
                 duration=1.0, color=(0, 200, 255))
        embeddings = []
        for i in range(n_target):
            m, l = get_frames()
            if m is not None:
                emb, _crop = self._capture_hq_embedding(
                    m, l, target_box=target_box)
                if emb is not None:
                    embeddings.append(emb)
            if i < n_target - 1:
                time.sleep(interval)

        if not embeddings:
            ui.toast("No face captured — try again",
                     duration=2.5, color=(0, 100, 255))
            return

        n_captured = len(embeddings)

        # 2) Dialog na jméno (capture je v paměti, dialog se může klidně zdržet)
        def _on_name(name):
            name = name.strip()
            if not name:
                ui.toast("Cancelled")
                return
            # 3) Assign — face_db.add(force=True) přeskočí diversity check.
            # Pro existující osobu = quick augment; pro novou = vytvoří záznam.
            added = 0
            for emb in embeddings:
                if self.ctrl.face_db.add(name, emb, force=True):
                    self.ctrl._cluster_db.add(name, emb)
                    added += 1
            if added > 0:
                ui.toast(f"Added {added}/{n_captured} samples to '{name}'",
                         duration=2.5, color=(0, 220, 0))
            else:
                ui.toast(f"No samples added for '{name}'",
                         duration=2.5, color=(0, 100, 255))

        ui.show_input(
            f"Captured {n_captured} samples — enter name "
            "(existing → add, new → create):",
            on_confirm=_on_name)

    def quick_augment(self, name: str, ui, get_frames, target_box=None):
        """Bez countdownu vezme N snímků á INTERVAL sekund a přidá embeddingy
        k existujícímu jménu v face_db. Blokuje krátce (~750ms pro N=5).

        TARGET_BOX_V1: pokud je target_box dán, vybere se z framu bbox
        s nejvyšším IOU (ne max(area)).
        """
        n_target  = self.QUICK_AUGMENT_SAMPLES
        interval  = self.QUICK_AUGMENT_INTERVAL
        ui.toast(f"Quick add to '{name}' — {n_target} samples...",
                 duration=1.5, color=(0, 200, 255))
        added = 0
        for i in range(n_target):
            m, l = get_frames()
            if m is None:
                time.sleep(interval)
                continue
            emb, crop = self._capture_hq_embedding(m, l, target_box=target_box)
            if emb is not None:
                if self.ctrl.face_db.add(name, emb, force=True):
                    self.ctrl._cluster_db.add(name, emb)
                    added += 1
            if i < n_target - 1:
                time.sleep(interval)
        if added > 0:
            ui.toast(f"Added {added}/{n_target} samples to '{name}'",
                     duration=2.5, color=(0, 220, 0))
        else:
            ui.toast(f"No face captured for '{name}' — try again",
                     duration=2.5, color=(0, 100, 255))

    def start_capture(self, ui, boxes):
        """SPACE key — start countdown."""
        if not self.active or self.countdown_start is not None:
            return
        if boxes:
            self.countdown_start = time.time()
            self.attempts        = 0
            ui.toast(f"Hold still — capturing in {self.FIRST_COUNTDOWN}s",
                     duration=self.FIRST_COUNTDOWN, color=(0, 255, 128))
        else:
            ui.toast("No face detected — move into frame first",
                     color=(0, 140, 255))

    def cancel(self, ui):
        """C key — cancel."""
        if not self.active:
            return False
        ui.toast(f"Enrollment cancelled for '{self.name}'", color=(0, 140, 255))
        self.reset()
        return True

    def delete_via_key(self, ui, reset_ema):
        """D key — request name, delete from DB.

        DELETE_HOOKS_PATCH:
        Soft-delete strategie. face_db.remove() smaže face embeddings
        natvrdo, ale vztahová karta v SQL se jen DEAKTIVUJE (data se
        neztratí). RAG dokument z hans_identita se odebere okamžitě
        (jinak by Hans pořád viděl deaktivovanou osobu v retrieval).

        Když se osoba později znovu enrolluje (stejné jméno), seed_one
        v enrollment hooku kartu automaticky reaktivuje — sightings_count,
        characterization, family_links zůstanou zachovány.
        """
        def _on_name(dname):
            dname = dname.strip()
            if not dname:
                return
            if dname not in self.ctrl.face_db.list_faces():
                ui.toast(f"'{dname}' not found", color=(0, 140, 255))
                return

            def _do_delete():
                # 1) Face / cluster DB
                self.ctrl.face_db.remove(dname)
                self.ctrl._cluster_db.remove(dname)
                self.ctrl._cluster_db.save()
                reset_ema()

                # 2) DELETE_HOOKS_PATCH — soft-delete vztahové karty +
                # RAG dokument pryč z hans_identita.
                pid = dname.lower().strip()
                rels = getattr(self.ctrl, '_relationships', None)
                knowledge = getattr(self.ctrl, '_knowledge', None)
                rel_status = ""
                if rels is not None:
                    try:
                        if rels.deactivate(pid):
                            rel_status = " + karta deaktivována"
                        # RAG delete (jen pokud knowledge funguje)
                        if knowledge is not None and knowledge.enabled:
                            ok = knowledge.delete(
                                "hans_identita", f"relationship_{pid}")
                            if ok:
                                rel_status += " + RAG smazán"
                            # ok=False může znamenat "nebyl v RAG" (např.
                            # karta neměla charakteristiku) — to není chyba
                    except Exception as _e:
                        print(f"[Delete] hooks failed for '{pid}': {_e}")

                ui.toast(f"Deleted '{dname}'{rel_status}",
                         color=(0, 220, 0))

            ui.show_confirm(f"Delete '{dname}'?",
                            on_yes=_do_delete,
                            on_no=lambda: ui.toast("Cancelled"))

        ui.show_input("Enter name to delete:", on_confirm=_on_name)

    # ── Tick / capture ───────────────────────────────────────────────────────

    def tick(self, ui, main_frame, lores_frame, recognizer, get_frames):
        """Volá se každý frame z main loopu (jen když self.active)."""
        if not self.active or self.countdown_start is None:
            return

        accepted = self.accepted
        duration = self.FIRST_COUNTDOWN if accepted == 0 else self.NEXT_COUNTDOWN

        if time.time() - self.countdown_start < duration:
            return  # countdown stále běží

        self.countdown_start = None
        self.attempts       += 1

        # Použij čerstvý frame pokud je
        m2, l2 = get_frames()
        if m2 is None:
            m2, l2 = main_frame, lores_frame

        emb, crop = self._capture_hq_embedding(m2, l2)

        if emb is not None:
            if self.ctrl.face_db.add(self.name, emb, force=True):
                self.samples.append(emb)
                self.ctrl._cluster_db.add(self.name, emb)
                if crop is not None:
                    self.crops.append(crop)
                accepted = len(self.samples)
                if accepted >= self.total_samples:
                    ui.toast(f"Captured {accepted} samples — review window opening...",
                             duration=2.0, color=(0, 200, 255))
                    self._open_review_window(ui, recognizer)
                else:
                    remaining = self.total_samples - accepted
                    ui.toast(
                        f"Shot {accepted}/{self.total_samples} accepted — "
                        f"step closer! ({remaining} left)",
                        duration=self.NEXT_COUNTDOWN, color=(0, 220, 0))
                    self.countdown_start = time.time()
                    self.attempts        = 0
                return
            reason = "low quality — try better lighting / angle"
        else:
            reason = "no face detected"

        if self.attempts >= _MAX_CAPTURE_RETRIES:
            ui.toast(
                f"Shot {accepted+1} failed after {_MAX_CAPTURE_RETRIES} "
                f"retries ({reason}) — skipping",
                duration=2.5, color=(0, 140, 255))
            self.countdown_start = time.time()
            self.attempts        = 0
        else:
            ui.toast(
                f"Retrying ({self.attempts}/{_MAX_CAPTURE_RETRIES}) — {reason}",
                duration=self.NEXT_COUNTDOWN, color=(0, 140, 255))
            self.countdown_start = time.time()

    def _capture_hq_embedding(self, main_frame, lores_frame, target_box=None):
        """Returns (embedding, crop_image) or (None, None) on failure.

        TARGET_BOX_V1: pokud je target_box dán (normalizovaný [x1,y1,x2,y2]),
        vybere se bbox s nejvyšším IOU. Jinak fallback na max(area).
        """
        face_boxes = [r[0] for r in self.ctrl.hailo.infer(lores_frame)
                      if r[2] == LABEL_FACE]
        if not face_boxes or main_frame is None:
            return None, None

        if target_box is not None:
            # IOU match — najdi bbox nejbližší klepnutému / cílovému
            def _iou(a, b):
                ix1 = max(a[0], b[0]); iy1 = max(a[1], b[1])
                ix2 = min(a[2], b[2]); iy2 = min(a[3], b[3])
                iw = max(0.0, ix2 - ix1); ih = max(0.0, iy2 - iy1)
                inter = iw * ih
                area_a = (a[2]-a[0]) * (a[3]-a[1])
                area_b = (b[2]-b[0]) * (b[3]-b[1])
                union = area_a + area_b - inter
                return inter / union if union > 0 else 0.0
            scored = [(_iou(fb, target_box), fb) for fb in face_boxes]
            scored.sort(reverse=True, key=lambda x: x[0])
            best_iou, best_fb = scored[0]
            # Pokud overlap je špatný (osoba se mezitím odsunula), zahodit
            if best_iou < 0.30:
                return None, None
        else:
            # Legacy chování — největší tvář ve framu
            best_fb = max(face_boxes,
                          key=lambda b: (b[2] - b[0]) * (b[3] - b[1]))
        H, W = main_frame.shape[:2]
        x1, y1, x2, y2 = best_fb
        bw, bh = x2 - x1, y2 - y1

        # Aligned crop nejdřív
        crop = self.ctrl._aligned_crop(main_frame, best_fb, H, W, bw, bh)
        if crop is None:
            # Fallback — padded crop
            pad = 0.10
            ix1 = max(0, int((x1 - bw * pad) * W))
            iy1 = max(0, int((y1 - bh * pad) * H))
            ix2 = min(W, int((x2 + bw * pad) * W))
            iy2 = min(H, int((y2 + bh * pad) * H))
            if ix2 <= ix1 or iy2 <= iy1:
                return None, None
            crop = cv2.resize(main_frame[iy1:iy2, ix1:ix2],
                              (ARCFACE_SIZE, ARCFACE_SIZE),
                              interpolation=cv2.INTER_LINEAR)

        crop = self.ctrl._face_prep.enhance_crop(crop)
        emb_list = self.ctrl.hailo.embed_faces([crop])
        emb = emb_list[0] if emb_list and emb_list[0] is not None else None
        return emb, crop

    def _open_review_window(self, ui, recognizer):
        """Show captured crops in review window before saving to DB."""
        from scripts.unknown_enrollment_window import UnknownEnrollmentWindow

        session_dir = Path("data/unknown_faces") / f"enroll_{str(uuid.uuid4())[:8]}"
        session_dir.mkdir(parents=True, exist_ok=True)

        # Save actual crop images for the review grid
        for idx, crop in enumerate(self.crops):
            img_path = session_dir / f"{idx:03d}.jpg"
            display_crop = cv2.resize(crop, (160, 160))
            cv2.imwrite(str(img_path),
                        cv2.cvtColor(display_crop, cv2.COLOR_RGB2BGR))

        name       = self.name
        embeddings = list(self.samples)
        face_db    = self.ctrl.face_db

        # ENROLL_REVIEW_BYPASS_V1 — remove-first ODSTRANĚN (byl příčinou data
        # loss když review okno nedoběhlo). Captured vzorky zůstávají uložené.

        ctrl = self.ctrl

        def _on_done(result_name):
            if result_name:
                ctrl.face_db.reload()
                recognizer._slots.clear()
                recognizer.seed_name(result_name)
                for _emb in embeddings:
                    ctrl._cluster_db.add(result_name, _emb)
                ctrl._cluster_db.save()
                ui.toast(f"'{result_name}' enrolled successfully!",
                         duration=3.0, color=(0, 220, 0))
                print(f"[Enroll] '{result_name}' saved + DB reloaded")

                # ENROLL_HOOKS_PATCH — vztahová karta + RAG re-upload.
                # person_id = result_name (normalizováno na lowercase),
                # display_name = result_name jak ho napsal uživatel.
                # seed_one je idempotent (vytvoří kostru NEBO reaktivuje
                # existující kartu po předchozím delete).
                rels = getattr(ctrl, '_relationships', None)
                knowledge = getattr(ctrl, '_knowledge', None)
                # DIAG_PATCH
                print(f"[Enroll-DIAG] ctrl={type(ctrl).__name__} "
                      f"hasattr _relationships={hasattr(ctrl, '_relationships')} "
                      f"rels={rels!r} knowledge={knowledge!r}")
                if rels is not None:
                    try:
                        pid = result_name.lower().strip()
                        rels.seed_one(pid, result_name)
                        # Pokud karta měla charakteristiku (re-enroll scénář),
                        # uploadni ji zpět do RAGu.
                        card = rels.get(pid)
                        if (card and card.characterization
                                and knowledge is not None
                                and knowledge.enabled):
                            from scripts.hans_relationships \
                                import RelationshipReflection  # RELATIONSHIPS_MERGED_V1
                            text = RelationshipReflection._build_rag_document(
                                None, card)
                            doc_id = f"relationship_{pid}"
                            title = f"Vztah — {card.display_name}"
                            metadata = {
                                "typ": "vztahová karta",
                                "osoba": pid,
                                "jmeno": card.display_name,
                                "role": card.role or "",
                            }
                            ok = knowledge.upload(
                                "hans_identita", doc_id, title, text, metadata)
                            if ok:
                                print(f"[Enroll] RAG re-upload '{pid}' OK "
                                      f"({len(text)} znaků)")
                            else:
                                print(f"[Enroll] RAG re-upload '{pid}' FAIL")
                    except Exception as _e:
                        print(f"[Enroll] hooks failed: {_e}")
            else:
                ui.toast("Enrollment cancelled", color=(0, 140, 255))
            try:
                shutil.rmtree(session_dir)
            except Exception:
                pass

        # ENROLL_REVIEW_BYPASS_V1 — finalizuj rovnou (vzorky už uložené z
        # capture), bez křehkého review okna. _on_done(name) dělá reload + seed
        # recognizer + cluster add + vztahová karta/RAG + cleanup session_dir.
        _on_done(name)
        self.reset()

    # ── Overlay ──────────────────────────────────────────────────────────────

    def draw_overlay(self, display, dw, dh):
        """Vykreslí enrollment progress overlay."""
        if not self.active:
            cv2.putText(
                display,
                "E=enroll  D=delete  L=list  S=settings  C=chat  "
                "G=open_hand  H=fist  J=none  ESC=quit",
                (10, dh - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.45,
                (160, 160, 160), 1)
            return

        accepted = self.accepted
        cv2.putText(
            display,
            f"ENROLLING: {self.name}  [{accepted}/{self.total_samples}]",
            (10, 65), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 140, 255), 2)

        if self.countdown_start is None:
            cv2.putText(display, "Stand at MAX distance  —  SPACE to start",
                        (10, 100), cv2.FONT_HERSHEY_SIMPLEX, 0.65,
                        (0, 255, 128), 2)
        else:
            is_first  = accepted == 0
            duration  = self.FIRST_COUNTDOWN if is_first else self.NEXT_COUNTDOWN
            remaining = max(0.0, duration - (time.time() - self.countdown_start))
            instr = ("Stay at max distance" if is_first
                     else f"Step closer  (shot {accepted+1}/{self.total_samples})")
            cv2.putText(display, instr, (10, 100),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.65, (0, 200, 255), 2)
            cnt_str = f"{remaining:.1f}"
            (tw, th), _ = cv2.getTextSize(
                cnt_str, cv2.FONT_HERSHEY_SIMPLEX, 3.5, 6)
            cv2.putText(display, cnt_str,
                        ((dw - tw) // 2, (dh + th) // 2),
                        cv2.FONT_HERSHEY_SIMPLEX, 3.5, (0, 255, 128), 6)

        cv2.putText(display, "C=cancel enrollment",
                    (10, dh - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.5,
                    (0, 140, 255), 1)
