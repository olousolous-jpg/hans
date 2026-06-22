"""
HANS_MENU_V1 — start menu (Tkinter).

Vždy dostupné okno, které žije i bez živého video preview (poháněné sdíleným
tk_mgr.pump() z hlavní smyčky). Umožní vypnout těžké cv2 preview a běžné akce
(chat s naučenou osobou, seznam/mazání tváří, enroll, přepnutí preview, web
nastavení) řídit z tlačítek.

Akce, které potřebují živé video (enroll), si preview samy zapnou.
Chat preview NEpotřebuje — jen výběr ze seznamu naučených osob.
"""

import subprocess
import tkinter as tk
from tkinter import ttk, messagebox
import webbrowser

from scripts.tk_manager import tk_mgr

# Dark palette (sjednoceno s ostatními okny)
_BG      = "#1e1e2e"
_PANEL   = "#313244"
_FG      = "#cdd6f4"
_ACCENT  = "#89b4fa"
_MUTED   = "#9399b2"


class HansMenu:
    """Trvalé start menu. Vytvoří se přes tk_mgr.call_soon (thread-safe)."""

    def __init__(self, controller):
        self.controller = controller
        self.config     = getattr(controller, "config", {}) or {}
        self.root       = None
        self.combo      = None
        self.status     = None
        self.preview_btn = None
        self._last_detected = None   # HANS_MENU_PRESELECT_V1
        try:
            tk_mgr.call_soon(self._create_window)
        except Exception as e:
            print(f"[HansMenu] schedule error: {e}")

    # ── Window ────────────────────────────────────────────────────────────
    def _create_window(self):
        try:
            parent = tk_mgr.get_root()
            if parent is None:
                return
            self.root = tk.Toplevel(parent)
            persona = (self.config.get("persona", {}) or {}).get("name", "Hans")
            self.root.title(f"{persona} — menu")
            self.root.configure(bg=_BG)
            self.root.geometry("320x440")
            self.root.protocol("WM_DELETE_WINDOW", self._on_close)

            tk.Label(self.root, text=persona, bg=_BG, fg=_ACCENT,
                     font=("DejaVu Sans", 18, "bold")).pack(pady=(14, 2))
            tk.Label(self.root, text="ovládání", bg=_BG, fg=_MUTED,
                     font=("DejaVu Sans", 9)).pack(pady=(0, 10))

            # ── Chat: výběr naučené osoby (bez preview) ──────────────────
            box = tk.Frame(self.root, bg=_BG)
            box.pack(fill="x", padx=18, pady=(0, 6))
            tk.Label(box, text="Osoba:", bg=_BG, fg=_FG,
                     font=("DejaVu Sans", 10)).pack(side="left")
            self.combo = ttk.Combobox(box, state="readonly", width=18)
            self.combo.pack(side="right", fill="x", expand=True)

            self._btn("Chat s osobou", self._on_chat, accent=True)
            self._btn("Obnovit seznam", self._refresh_people)
            self._sep()
            self._btn("Přidat tvář (Enroll)", self._on_enroll)
            self._btn("Smazat vybranou tvář", self._on_delete)
            self._btn("Seznam tváří", self._on_list)
            self._sep()
            self.preview_btn = self._btn("Zapnout preview", self._on_preview)
            self._btn("Nastavení (web)", self._on_settings)
            self._sep()
            self._btn("Restart Hanse", self._on_restart)

            self.status = tk.Label(self.root, text="", bg=_BG, fg=_MUTED,
                                   font=("DejaVu Sans", 8), wraplength=290)
            self.status.pack(side="bottom", pady=8)

            # MENU_AUTOSIZE_V1 — okno se přizpůsobí počtu tlačítek (dřív pevných
            # 440 px → nové tlačítko se schovalo pod spodní okraj, dokud uživatel
            # okno ručně nezvětšil). Šířka 320, výška = potřebná pro vše + minsize.
            self.root.update_idletasks()
            _h = self.root.winfo_reqheight()
            self.root.geometry("320x%d" % _h)
            self.root.minsize(320, _h)

            self._refresh_people()
            self._sync_preview_label()
            self._poll_detected()  # HANS_MENU_PRESELECT_V1
        except Exception as e:
            print(f"[HansMenu] create error: {e}")

    def _btn(self, text, cmd, accent=False):
        b = tk.Button(self.root, text=text, command=cmd,
                      bg=_ACCENT if accent else _PANEL,
                      fg=_BG if accent else _FG,
                      activebackground=_ACCENT, activeforeground=_BG,
                      relief="flat", font=("DejaVu Sans", 11),
                      anchor="w", padx=14, pady=7, bd=0)
        b.pack(fill="x", padx=18, pady=3)
        return b

    def _sep(self):
        tk.Frame(self.root, bg=_PANEL, height=1).pack(fill="x", padx=18, pady=6)

    # ── Helpers ───────────────────────────────────────────────────────────
    def _toast(self, text):
        if self.status is not None:
            try:
                self.status.config(text=text)
            except Exception:
                pass

    def _selected(self):
        try:
            return (self.combo.get() or "").split("  (")[0].strip() or None
        except Exception:
            return None

    def _refresh_people(self):
        faces = self.controller.menu_list_faces()
        items = [f"{n}  ({c})" for n, c in faces]
        try:
            self.combo["values"] = items
            if items and not self.combo.get():
                self.combo.current(0)
        except Exception:
            pass
        self._toast(f"{len(items)} naučených osob" if items
                    else "Zatím žádné naučené tváře")

    def _select_name(self, name):
        """Nastaví combobox na danou osobu (najde položku „name  (n)")."""
        try:
            for v in self.combo["values"]:
                if v.split("  (")[0].strip() == name:
                    self.combo.set(v)
                    return True
        except Exception:
            pass
        return False

    def _poll_detected(self):  # HANS_MENU_PRESELECT_V1
        """Předvybere v menu osobu, kterou kamera právě rozpoznala.
        Edge-triggered: mění výběr jen při ZMĚNĚ detekované osoby, takže
        ruční výběr mezi změnami zůstane respektován."""
        try:
            name = self.controller.menu_current_person()
            if name and name != self._last_detected:
                if not self._select_name(name):
                    self._refresh_people()      # nová/čerstvě naučená osoba
                    self._select_name(name)
                self._last_detected = name
                self._toast(f"Detekováno: {name}")
        except Exception:
            pass
        try:
            if self.root is not None:
                self.root.after(1000, self._poll_detected)
        except Exception:
            pass

    def _sync_preview_label(self):
        on = getattr(self.controller, "_preview_on", False)
        if self.preview_btn is not None:
            try:
                self.preview_btn.config(
                    text="Vypnout preview" if on else "Zapnout preview")
            except Exception:
                pass

    # ── Actions ───────────────────────────────────────────────────────────
    def _on_chat(self):
        name = self._selected()
        if not name:
            self._toast("Vyber osobu ze seznamu")
            return
        if self.controller.menu_open_chat(name):
            self._toast(f"Chat otevřen: {name}")
        else:
            self._toast("Chat není dostupný")

    def _on_enroll(self):
        self.controller.menu_enroll()
        self._sync_preview_label()
        self._toast("Enroll spuštěn — preview zapnuto, postupuj v okně kamery")

    def _on_delete(self):
        name = self._selected()
        if not name:
            self._toast("Vyber osobu ke smazání")
            return
        if not messagebox.askyesno("Smazat tvář",
                                   f'Opravdu smazat naučenou tvář „{name}"?',
                                   parent=self.root):
            return
        if self.controller.menu_delete(name):
            self._toast(f"Smazáno: {name}")
            self._refresh_people()
        else:
            self._toast(f"Mazání selhalo: {name}")

    def _on_list(self):
        faces = self.controller.menu_list_faces()
        if not faces:
            messagebox.showinfo("Tváře", "Zatím žádné naučené tváře.",
                                parent=self.root)
            return
        txt = "\n".join(f"• {n}  —  {c} vzorků" for n, c in faces)
        messagebox.showinfo(f"Naučené tváře ({len(faces)})", txt,
                            parent=self.root)

    def _on_preview(self):
        on = self.controller.menu_toggle_preview()
        self._sync_preview_label()
        self._toast("Preview zapnuto" if on else "Preview vypnuto")

    def _on_settings(self):
        try:
            webbrowser.open("http://localhost:7860")
            self._toast("Nastavení otevřeno v prohlížeči")
        except Exception as e:
            self._toast(f"Nelze otevřít web: {e}")

    def _on_restart(self):
        if not messagebox.askyesno(
                "Restart Hanse",
                "Opravdu restartovat Hanse?\n"
                "Na chvíli se vypne a sám znovu naběhne.",
                parent=self.root):
            return
        self._toast("Restartuji Hanse…")
        try:
            # --no-block: job se zařadí do systemd a klient hned skončí,
            # takže se nestihne zabít při shození služby.
            subprocess.Popen(
                ["systemctl", "--user", "--no-block", "restart", "hans"],
                start_new_session=True)
        except Exception as e:
            self._toast(f"Restart selhal: {e}")

    def _on_close(self):
        # Menu je trvalé — zavření jen schová okno (lze znovu vyvolat).
        try:
            self.root.withdraw()
        except Exception:
            pass
