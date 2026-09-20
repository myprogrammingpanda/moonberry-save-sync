"""Plain-Tkinter GUI, replacing the old system tray icon. Runs the
SessionController's poll loop on a background thread and marshals its
callbacks into the Tk main thread via a queue, since Tkinter widgets are not
thread-safe."""

import logging
import os
import queue
import sys
import threading
import tkinter as tk
from tkinter import scrolledtext


class _QueueLogHandler(logging.Handler):
    """Pushes formatted log lines into a thread-safe queue so the Tk main
    loop can display them without touching widgets from a background
    thread."""

    def __init__(self, ui_queue: queue.Queue):
        super().__init__()
        self.ui_queue = ui_queue

    def emit(self, record):
        self.ui_queue.put(("log", self.format(record)))


class App:
    def __init__(self, controller, app_version: str):
        self.controller = controller
        self.app_version = app_version
        self.ui_queue: queue.Queue = queue.Queue()

        self.root = tk.Tk()
        self.root.title(f"Moonberry Save-Sync — {controller.adapter.display_name}")
        self.root.geometry("560x380")
        self.root.minsize(420, 280)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self.status_var = tk.StringVar(value="Starting...")
        tk.Label(
            self.root, textvariable=self.status_var, anchor="w", wraplength=520, justify="left"
        ).pack(fill="x", padx=10, pady=(10, 4))

        self.play_button = tk.Button(self.root, text="Play Now", command=self._on_play_now)
        self.play_button.pack(anchor="w", padx=10, pady=(0, 8))

        self.log_text = scrolledtext.ScrolledText(self.root, state="disabled", height=16)
        self.log_text.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        handler = _QueueLogHandler(self.ui_queue)
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        logging.getLogger("moonberry-sync").addHandler(handler)

        self.root.after(100, self._poll_queue)

    def _on_play_now(self):
        self.controller.play_requested.set()
        self.ui_queue.put(("status", "Play requested — will start shortly..."))

    def _on_close(self):
        os._exit(0)

    # -- callbacks handed to SessionController.run_loop; called from the
    # background thread, so they only ever touch the thread-safe queue --

    def _update_status(self, text: str):
        self.ui_queue.put(("status", text))

    def _notify_desktop(self, title: str, message: str):
        # Plain Tkinter has no built-in toast notification without extra
        # dependencies. Surface it prominently in the status line and log,
        # plus a system beep and (on Windows) a taskbar flash, so it's
        # still noticeable without needing anything beyond the stdlib.
        self.ui_queue.put(("status", f"{title}: {message}"))
        self.ui_queue.put(("alert", None))

    def _poll_queue(self):
        try:
            while True:
                kind, payload = self.ui_queue.get_nowait()
                if kind == "status":
                    self.status_var.set(payload)
                elif kind == "log":
                    self.log_text.configure(state="normal")
                    self.log_text.insert("end", payload + "\n")
                    self.log_text.see("end")
                    self.log_text.configure(state="disabled")
                elif kind == "alert":
                    self._flash_attention()
        except queue.Empty:
            pass
        self.root.after(100, self._poll_queue)

    def _flash_attention(self):
        try:
            self.root.bell()
        except Exception:
            pass
        if sys.platform == "win32":
            try:
                import ctypes

                ctypes.windll.user32.FlashWindow(self.root.winfo_id(), True)
            except Exception:
                pass

    def run(self):
        t = threading.Thread(
            target=self.controller.run_loop,
            args=(self._update_status, self._notify_desktop),
            daemon=True,
        )
        t.start()
        self.root.mainloop()
