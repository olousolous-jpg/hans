"""
Unknown Face Enrollment Window
- Opens after open_after captures (configurable)
- Photos arrive live via add_photo()
- Hard stops at target_count
- ✕ button removes a photo from selection
- Window stays open if person leaves — only closes on Enroll or Skip
"""

import tkinter as tk
import threading
import shutil
from pathlib import Path

try:
    from PIL import Image, ImageTk
    _PIL_OK = True
except ImportError:
    _PIL_OK = False

from scripts.tk_manager import tk_mgr

BG     = "#1e1e2e"
BG2    = "#2a2a3e"
BG3    = "#313145"
ACCENT = "#89b4fa"
GREEN  = "#a6e3a1"
RED    = "#f38ba8"
ORANGE = "#fab387"
FG     = "#cdd6f4"
FG2    = "#a6adc8"

THUMB_SIZE   = 90
PREVIEW_SIZE = 280
COLS         = 6


class ImagePreviewTooltip:
    def __init__(self, widget, img_path: Path):
        self.widget   = widget
        self.img_path = img_path
        self.tip      = None
        self._ref     = None
        widget.bind("<Enter>", self._show)
        widget.bind("<Leave>", self._hide)

    def _show(self, _=None):
        if not _PIL_OK or not self.img_path.exists():
            return
        try:
            x = self.widget.winfo_rootx() + THUMB_SIZE + 10
            y = self.widget.winfo_rooty()
            self.tip = tk.Toplevel(self.widget)
            self.tip.wm_overrideredirect(True)
            self.tip.wm_geometry(f"+{x}+{y}")
            self.tip.configure(bg=BG3)
            img = Image.open(self.img_path).resize(
                (PREVIEW_SIZE, PREVIEW_SIZE), Image.LANCZOS)
            self._ref = ImageTk.PhotoImage(img)
            tk.Label(self.tip, image=self._ref, bg=BG3,
                     relief="solid", bd=1).pack(padx=2, pady=2)
        except Exception:
            pass

    def _hide(self, _=None):
        if self.tip:
            try:
                self.tip.destroy()
            except Exception:
                pass
            self.tip = None
        self._ref = None


class UnknownEnrollmentWindow:
    """
    Live enrollment window.

    Parameters:
        session_dir  : Path       — directory for captured JPGs
        embeddings   : list       — initial list of np.ndarray embeddings
        face_db      : FaceDB
        on_done      : callable(name_or_None)
        target_count : int        — shown in progress, collecting stops here
    """

    def __init__(self, session_dir: Path, embeddings: list,
                 face_db, on_done=None, target_count: int = 20):
        self.session_dir  = session_dir
        self.embeddings   = list(embeddings)
        self.face_db      = face_db
        self.on_done      = on_done
        self.target_count = target_count
        self.active       = True

        self._win          = None
        self._grid_frame   = None
        self._canvas       = None
        self._count_lbl    = None
        self._status_lbl   = None
        self._progress_lbl = None
        self._collecting   = True

        # (img_path, embedding_idx, removed) per slot
        self._slots: list[dict] = []
        # Stable PIL refs keyed by slot index
        self._thumb_refs: dict[int, object] = {}

        # Load images already on disk when window opens
        self.img_paths = sorted(session_dir.glob("*.jpg"))

        try:
            tk_mgr.call_soon(self._create_window)
        except Exception as e:
            print(f"[UnknownEnroll] Schedule error: {e}")

    # ── Public API (called from collector thread) ──────────────────────────────

    def add_photo(self, img_path: Path, embedding):
        """Append a new photo. Thread-safe — deferred to Tk thread."""
        # Keep embeddings list in sync
        while len(self.embeddings) < len(sorted(self.session_dir.glob("*.jpg"))):
            self.embeddings.append(embedding)
        if self._win:
            try:
                self._win.after(0, self._on_new_photo)
            except Exception:
                pass

    def set_collecting_done(self):
        """Signal that capture has stopped."""
        self._collecting = False
        if self._win:
            try:
                self._win.after(0, self._update_progress_done)
            except Exception:
                pass

    # ── Window creation ────────────────────────────────────────────────────────

    def _create_window(self):
        try:
            root = tk_mgr.get_root()
            if root is None:
                # Not on owner thread yet — reschedule for next pump()
                print("[UnknownEnroll] Root not ready, rescheduling...", flush=True)
                tk_mgr.call_soon(self._create_window)
                return
            self._win = tk.Toplevel(root)
            self._win.title("📸 Neznámá osoba — Enrollovat")
            self._win.configure(bg=BG)
            self._win.resizable(True, True)
            # Window stays open — WM close = skip
            self._win.protocol("WM_DELETE_WINDOW", self._on_skip)
            self._build_ui()
            self._center_window()
            print("[UnknownEnroll] Window created OK", flush=True)
        except Exception as e:
            import traceback
            print(f"[UnknownEnroll] Window error: {e}", flush=True)
            traceback.print_exc()

    def _build_ui(self):
        # ── Header ────────────────────────────────────────────────────────
        hdr = tk.Frame(self._win, bg=BG2, pady=8)
        hdr.pack(fill=tk.X)
        tk.Label(hdr, text="📸 Neznámá osoba detekována",
                 font=("Segoe UI", 13, "bold"),
                 bg=BG2, fg=ACCENT).pack(side=tk.LEFT, padx=16)
        self._progress_lbl = tk.Label(
            hdr, text=self._progress_text(),
            font=("Segoe UI", 9), bg=BG2, fg=ORANGE)
        self._progress_lbl.pack(side=tk.RIGHT, padx=16)
        tk.Label(hdr, text="hover = náhled  |  ✕ = odebrat",
                 font=("Segoe UI", 9), bg=BG2, fg=FG2).pack(side=tk.RIGHT, padx=8)

        # ── Scrollable photo grid ─────────────────────────────────────────
        outer = tk.Frame(self._win, bg=BG)
        outer.pack(fill=tk.BOTH, expand=True, padx=8, pady=6)

        self._canvas = tk.Canvas(outer, bg=BG, highlightthickness=0, height=360)
        sb = tk.Scrollbar(outer, orient=tk.VERTICAL, command=self._canvas.yview)
        self._grid_frame = tk.Frame(self._canvas, bg=BG)
        self._grid_frame.bind(
            "<Configure>",
            lambda e: self._canvas.configure(
                scrollregion=self._canvas.bbox("all")))
        self._canvas.create_window((0, 0), window=self._grid_frame, anchor="nw")
        self._canvas.configure(yscrollcommand=sb.set)
        self._canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self._canvas.bind_all("<MouseWheel>",
            lambda e: self._canvas.yview_scroll(
                int(-1*(e.delta/120)), "units"))

        # Render any photos already captured
        for i, p in enumerate(self.img_paths):
            self._append_card(i, p)

        # ── Count label ───────────────────────────────────────────────────
        sel_frame = tk.Frame(self._win, bg=BG, pady=2)
        sel_frame.pack(fill=tk.X, padx=12)
        self._count_lbl = tk.Label(sel_frame, text=self._count_text(),
                                   bg=BG, fg=FG2, font=("Segoe UI", 9))
        self._count_lbl.pack(side=tk.LEFT)

        # ── Name entry ────────────────────────────────────────────────────
        name_frame = tk.Frame(self._win, bg=BG, pady=6)
        name_frame.pack(fill=tk.X, padx=12)
        tk.Label(name_frame, text="Jméno osoby:",
                 bg=BG, fg=FG, font=("Segoe UI", 11)).pack(side=tk.LEFT)
        self._name_var = tk.StringVar()
        name_entry = tk.Entry(name_frame, textvariable=self._name_var,
                              bg=BG3, fg=FG, insertbackground=FG,
                              relief="flat", font=("Consolas", 12), width=22)
        name_entry.pack(side=tk.LEFT, padx=10)
        name_entry.bind('<Return>', lambda _: self._on_enroll())
        name_entry.focus()

        self._status_lbl = tk.Label(name_frame, text="",
                                    bg=BG, fg=GREEN, font=("Segoe UI", 9))
        self._status_lbl.pack(side=tk.LEFT, padx=8)

        # ── Bottom buttons ────────────────────────────────────────────────
        btn_frame = tk.Frame(self._win, bg=BG2, pady=8)
        btn_frame.pack(fill=tk.X, side=tk.BOTTOM)
        tk.Button(btn_frame, text="✓  Enrollovat vybrané",
                  command=self._on_enroll,
                  bg=GREEN, fg=BG, font=("Segoe UI", 11, "bold"),
                  relief="flat", padx=20, cursor="hand2").pack(side=tk.RIGHT, padx=8)
        tk.Button(btn_frame, text="✕  Přeskočit",
                  command=self._on_skip,
                  bg=BG3, fg=RED, font=("Segoe UI", 10),
                  relief="flat", padx=16, cursor="hand2").pack(side=tk.RIGHT)

    # ── Card management ────────────────────────────────────────────────────────

    def _append_card(self, idx: int, img_path: Path):
        """Add a single new card to the grid. Never rebuilds existing cards."""
        slot = {"img_path": img_path, "emb_idx": idx, "removed": False,
                "frame": None}
        self._slots.append(slot)

        row = idx // COLS
        col = idx % COLS

        cell = tk.Frame(self._grid_frame, bg=BG, padx=2, pady=2)
        cell.grid(row=row, column=col, padx=3, pady=3)
        slot["frame"] = cell

        # Thumbnail
        if _PIL_OK and img_path.exists():
            try:
                img = Image.open(img_path).resize(
                    (THUMB_SIZE, THUMB_SIZE), Image.LANCZOS)
                photo = ImageTk.PhotoImage(img)
                self._thumb_refs[idx] = photo
                lbl = tk.Label(cell, image=photo, bg=BG, relief="solid", bd=1)
                lbl.pack()
                ImagePreviewTooltip(lbl, img_path)
            except Exception:
                tk.Label(cell, text="📷", bg=BG, fg=FG2,
                         font=("Segoe UI", 22),
                         width=5, height=3).pack()
        else:
            tk.Label(cell, text=f"#{idx+1}", bg=BG, fg=FG2,
                     font=("Consolas", 9), width=6, height=4).pack()

        # ✕ remove button
        def _remove(s=slot, c=cell):
            s["removed"] = True
            try:
                c.destroy()
            except Exception:
                pass
            # Remove PIL ref to free memory
            self._thumb_refs.pop(idx, None)
            self._refresh_count()

        tk.Button(cell, text="✕", command=_remove,
                  bg=BG3, fg=RED, font=("Consolas", 8),
                  relief="flat", cursor="hand2", pady=0).pack(fill=tk.X)

        # Scroll to show latest
        self._canvas.configure(scrollregion=self._canvas.bbox("all"))
        self._canvas.yview_moveto(1.0)
        self._refresh_count()

    def _refresh_count(self):
        if self._count_lbl:
            try:
                self._count_lbl.config(text=self._count_text())
            except Exception:
                pass

    # ── Live update callbacks ──────────────────────────────────────────────────

    def _on_new_photo(self):
        """Tk thread: add cards for any new photos on disk."""
        current_paths = sorted(self.session_dir.glob("*.jpg"))
        known = {s["img_path"] for s in self._slots}
        for p in current_paths:
            if p not in known:
                self._append_card(len(self._slots), p)
        if self._progress_lbl:
            try:
                self._progress_lbl.config(text=self._progress_text())
            except Exception:
                pass

    def _update_progress_done(self):
        if self._progress_lbl:
            try:
                n = len(self._slots)
                self._progress_lbl.config(
                    text=f"✓ Zachyceno {n}/{self.target_count} — sbírání dokončeno",
                    fg=GREEN)
            except Exception:
                pass

    # ── Actions ───────────────────────────────────────────────────────────────

    def _on_enroll(self):
        name = self._name_var.get().strip()
        if not name:
            self._status_lbl.config(text="⚠ Zadej jméno!", fg=ORANGE)
            return

        active_slots = [s for s in self._slots if not s["removed"]]
        if not active_slots:
            self._status_lbl.config(text="⚠ Žádné snímky!", fg=ORANGE)
            return

        enrolled = 0
        for s in active_slots:
            ei = s["emb_idx"]
            if ei < len(self.embeddings) and self.embeddings[ei] is not None:
                if self.face_db.add(name, self.embeddings[ei], force=True):
                    enrolled += 1

        if enrolled > 0:
            self._status_lbl.config(
                text=f"✓ Enrollováno {enrolled} snímků jako '{name}'",
                fg=GREEN)
            print(f"[UnknownEnroll] Enrolled '{name}' — {enrolled} embeddings")
            try:
                shutil.rmtree(self.session_dir)
            except Exception:
                pass
            if self.on_done:
                self.on_done(name)
            self._win.after(1500, self._close)
        else:
            self._status_lbl.config(
                text="⚠ Enrollování selhalo — embeddingy chybí", fg=RED)

    def _on_skip(self):
        print("[UnknownEnroll] Skipped")
        try:
            shutil.rmtree(self.session_dir)
        except Exception:
            pass
        if self.on_done:
            self.on_done(None)
        self._close()

    def _close(self):
        self.active = False
        if self._win:
            try:
                self._win.destroy()
            except Exception:
                pass
            self._win = None

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _progress_text(self) -> str:
        n = len(self._slots) if self._slots else len(self.img_paths)
        if self._collecting:
            return f"Sbírám... {n}/{self.target_count}"
        return f"✓ {n}/{self.target_count} — hotovo"

    def _count_text(self) -> str:
        active = sum(1 for s in self._slots if not s["removed"])
        total  = len(self._slots)
        return f"{active} / {total} snímků bude použito"

    def _center_window(self):
        try:
            self._win.update_idletasks()
            sw = self._win.winfo_screenwidth()
            sh = self._win.winfo_screenheight()
            w, h = 820, 580
            self._win.geometry(f"{w}x{h}+{(sw-w)//2}+{(sh-h)//2}")
        except Exception:
            pass
