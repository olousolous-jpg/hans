"""
Tkinter Manager — single hidden Tk root for the whole program.
Thread ID is registered on first pump() call (= main OpenCV loop thread).
All subsequent calls from other threads are silently ignored.
"""

import tkinter as tk
import threading


class TkManager:

    def __init__(self):
        self._root: tk.Tk | None = None
        self._owner_thread: int | None = None   # set on first pump()
        self._pending: list = []                # thread-safe call queue
        self._pending_lock = threading.Lock()

    def call_soon(self, fn):
        """Schedule fn() to run on the main Tk thread on next pump().
        Safe to call from any thread."""
        with self._pending_lock:
            self._pending.append(fn)

    def _ok(self) -> bool:
        """True if called from the owner thread."""
        return self._owner_thread is not None and \
               threading.get_ident() == self._owner_thread

    def get_root(self) -> tk.Tk | None:
        """Return shared Tk root. Only works from owner thread."""
        if not self._ok():
            return None
        if self._root is None:
            try:
                self._root = tk.Tk()
                self._root.withdraw()
                self._root.protocol("WM_DELETE_WINDOW", lambda: None)
            except Exception as e:
                print(f"[TkManager] Cannot create Tk root: {e}")
                return None
        return self._root

    def pump(self):
        """
        Call once per frame from the main OpenCV loop.
        First call registers this thread as the owner.
        """
        # Register owner thread on first call
        if self._owner_thread is None:
            self._owner_thread = threading.get_ident()
            print(f"[TkManager] Owner thread registered: {self._owner_thread}")

        if not self._ok():
            return

        # Lazy-init root
        if self._root is None:
            try:
                self._root = tk.Tk()
                self._root.withdraw()
                self._root.protocol("WM_DELETE_WINDOW", lambda: None)
            except Exception as e:
                print(f"[TkManager] Tk init failed: {e}")
                return

        # Drain pending cross-thread calls
        with self._pending_lock:
            pending = self._pending[:]
            self._pending.clear()
        for fn in pending:
            try:
                fn()
            except Exception as e:
                print(f"[TkManager] pending call error: {e}")

        try:
            self._root.update()
        except (tk.TclError, RuntimeError):
            self._root = None

    def destroy(self):
        """Call on clean program exit from the owner thread."""
        if self._root:
            try:
                self._root.destroy()
            except Exception:
                pass
            self._root = None


tk_mgr = TkManager()
