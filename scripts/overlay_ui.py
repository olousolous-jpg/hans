"""
Overlay UI Module
Pure-OpenCV modal dialogs and toast notifications drawn directly onto
the camera frame.  No terminal input() calls during normal operation.

Dialogs:
  show_input(prompt, on_confirm, on_cancel)  — single-line text entry
  show_confirm(question, on_yes, on_no)      — yes / no
  show_list(title, items)                    — scrollable list

Toasts:
  toast(msg, duration, color)                — timed banner at top of frame

Key routing:
  handle_key(raw_key) → True if the key was consumed by an open dialog
  is_open()           → True while any dialog is active

Drawing:
  draw(frame)         — call once per frame; draws dialogs + toasts in-place
"""

import time
import cv2


class OverlayUI:
    """Modal dialogs and toast notifications rendered onto OpenCV frames."""

    _FONT       = cv2.FONT_HERSHEY_SIMPLEX
    _PANEL_COL  = (20, 20, 20)
    _BORDER_COL = (0, 160, 255)
    _TEXT_COL   = (230, 230, 230)
    _CURSOR_COL = (0, 255, 128)
    _HINT_COL   = (140, 140, 140)
    _TOAST_COL  = (0, 200, 255)
    _OK_COL     = (0, 220, 0)
    _WARN_COL   = (0, 140, 255)

    def __init__(self, win_name: str):
        self.win    = win_name
        self._dlg   = None    # active dialog dict or None
        self._toasts = []     # list of {msg, expire, color}
        self._banner = None   # persistent bottom banner {text,color} or None

    # ── Public API ────────────────────────────────────────────────────────

    def show_input(self, prompt: str, on_confirm, on_cancel=None):
        """Open a text-input dialog.  on_confirm(text) called on Enter."""
        self._dlg = dict(kind='input', prompt=prompt, text='',
                         on_confirm=on_confirm, on_cancel=on_cancel,
                         blink_t=time.time())

    def show_confirm(self, question: str, on_yes, on_no=None):
        """Open a yes/no confirmation dialog."""
        self._dlg = dict(kind='confirm', question=question,
                         on_yes=on_yes, on_no=on_no)

    def show_list(self, title: str, items: list):
        """Open a scrollable list dialog."""
        self._dlg = dict(kind='list', title=title, items=items, offset=0)

    def toast(self, msg: str, duration: float = 2.5, color=None):
        """Show a non-modal timed banner at the top of the frame."""
        self._toasts.append(dict(
            msg=msg,
            expire=time.time() + duration,
            color=color or self._TOAST_COL,
        ))

    def set_banner(self, text: str, color=None):
        """Persistent banner pinned to the bottom of the frame (e.g. for the
        servo calibration wizard). Stays until clear_banner()."""
        self._banner = dict(text=text, color=color or self._BORDER_COL)

    def clear_banner(self):
        self._banner = None

    def is_open(self) -> bool:
        return self._dlg is not None

    def handle_key(self, key: int) -> bool:
        """
        Feed a cv2.waitKey() result here.
        Returns True if the key was consumed by the active dialog.
        """
        if self._dlg is None:
            return False
        d = self._dlg

        if d['kind'] == 'input':
            if key == 27:                       # ESC — cancel
                self._dlg = None
                if d.get('on_cancel'):
                    d['on_cancel']()
            elif key == 13:                     # Enter — confirm
                self._dlg = None
                d['on_confirm'](d['text'])
            elif key in (8, 127):               # Backspace
                d['text'] = d['text'][:-1]
            elif 32 <= key <= 126:
                d['text'] += chr(key)
            return True

        elif d['kind'] == 'confirm':
            if key in (ord('y'), ord('Y')):
                self._dlg = None
                d['on_yes']()
            elif key in (ord('n'), ord('N'), 27):
                self._dlg = None
                if d.get('on_no'):
                    d['on_no']()
            return True

        elif d['kind'] == 'list':
            if key in (27, ord('l'), ord('L')):
                self._dlg = None
            elif key in (82, ord('k')):         # up arrow / k
                d['offset'] = max(0, d['offset'] - 1)
            elif key in (84, ord('j')):         # down arrow / j
                d['offset'] = min(max(0, len(d['items']) - 8), d['offset'] + 1)
            return True

        return False

    def draw(self, frame):
        """Draw active dialog + toasts onto frame in-place."""
        self._draw_toasts(frame)
        if self._banner is not None:
            self._draw_banner(frame, self._banner)
        if self._dlg is None:
            return
        d = self._dlg
        if d['kind'] == 'input':
            self._draw_input(frame, d)
        elif d['kind'] == 'confirm':
            self._draw_confirm(frame, d)
        elif d['kind'] == 'list':
            self._draw_list(frame, d)

    # ── Internal drawing ──────────────────────────────────────────────────

    def _draw_banner(self, frame, b):
        H, W = frame.shape[:2]
        bh = 38
        y0 = H - bh
        overlay = frame.copy()
        cv2.rectangle(overlay, (0, y0), (W, H), self._PANEL_COL, -1)
        cv2.addWeighted(overlay, 0.78, frame, 0.22, 0, frame)
        cv2.rectangle(frame, (0, y0), (W, H), b['color'], 2)
        self._text(frame, b['text'], 14, y0 + 25, 0.6, b['color'], 2)

    def _panel(self, frame, x, y, w, h):
        overlay = frame.copy()
        cv2.rectangle(overlay, (x, y), (x + w, y + h), self._PANEL_COL, -1)
        cv2.addWeighted(overlay, 0.82, frame, 0.18, 0, frame)
        cv2.rectangle(frame,    (x, y), (x + w, y + h), self._BORDER_COL, 2)

    def _text(self, frame, txt, x, y, scale=0.6, color=None, thickness=1):
        cv2.putText(frame, txt, (x, y), self._FONT, scale,
                    color or self._TEXT_COL, thickness, cv2.LINE_AA)

    def _draw_input(self, frame, d):
        H, W = frame.shape[:2]
        pw, ph = 540, 120
        px, py = (W - pw) // 2, (H - ph) // 2
        self._panel(frame, px, py, pw, ph)
        self._text(frame, d['prompt'], px + 16, py + 32, 0.65, self._BORDER_COL, 2)
        show_cursor = int((time.time() - d.get('blink_t', time.time())) * 2) % 2 == 0
        field_text  = d['text'] + ('|' if show_cursor else ' ')
        self._text(frame, field_text, px + 16, py + 72, 0.7, self._CURSOR_COL, 2)
        self._text(frame, "Enter = confirm    Esc = cancel",
                   px + 16, py + 105, 0.45, self._HINT_COL)

    def _draw_confirm(self, frame, d):
        H, W = frame.shape[:2]
        pw, ph = 520, 100
        px, py = (W - pw) // 2, (H - ph) // 2
        self._panel(frame, px, py, pw, ph)
        self._text(frame, d['question'], px + 16, py + 38, 0.65, self._WARN_COL, 2)
        self._text(frame, "Y = yes    N / Esc = cancel",
                   px + 16, py + 80, 0.5, self._HINT_COL)

    def _draw_list(self, frame, d):
        H, W    = frame.shape[:2]
        items   = d['items']
        visible = 8
        pw      = 400
        ph      = 40 + visible * 28 + 30
        px, py  = (W - pw) // 2, (H - ph) // 2
        self._panel(frame, px, py, pw, ph)
        self._text(frame, d['title'], px + 12, py + 26, 0.6, self._BORDER_COL, 2)
        for row_i in range(visible):
            item_i = d['offset'] + row_i
            if item_i >= len(items):
                break
            ry = py + 52 + row_i * 28
            if row_i % 2 == 0:
                cv2.rectangle(frame, (px + 4, ry - 18),
                              (px + pw - 4, ry + 8), (35, 35, 35), -1)
            self._text(frame, f"{item_i+1:2d}. {items[item_i]}", px + 12, ry, 0.55)
        self._text(frame,
                   f"j/↓ k/↑ scroll    L/Esc close  ({len(items)} total)",
                   px + 8, py + ph - 8, 0.40, self._HINT_COL)

    def _draw_toasts(self, frame):
        now = time.time()
        self._toasts = [t for t in self._toasts if t['expire'] > now]
        for i, toast in enumerate(self._toasts):
            H, W = frame.shape[:2]
            msg  = toast['msg']
            (tw, th), _ = cv2.getTextSize(msg, self._FONT, 0.65, 2)
            tx = (W - tw) // 2
            ty = 60 + i * 40
            cv2.rectangle(frame, (tx - 10, ty - th - 6),
                          (tx + tw + 10, ty + 8), (10, 10, 10), -1)
            cv2.putText(frame, msg, (tx, ty), self._FONT, 0.65,
                        toast['color'], 2, cv2.LINE_AA)