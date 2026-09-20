"""Plain-Tkinter GUI, replacing the old system tray icon. Runs the
SessionController's poll loop on a background thread and marshals its
callbacks into the Tk main thread via a queue, since Tkinter widgets are not
thread-safe."""

import json
import logging
import os
import queue
import sys
import threading
import tkinter as tk
from tkinter import font as tkfont
from tkinter import messagebox, scrolledtext, ttk

from gui.theme import apply_menu_colors, apply_palette, resolve_theme, set_titlebar_theme
from gui.toggle_switch import ToggleSwitch

_LOG_LEVEL_TAGS = ("WARNING", "ERROR", "CRITICAL")


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
        self.root.geometry("640x460")
        self.root.minsize(480, 320)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self.style = ttk.Style(self.root)
        self.theme_name = resolve_theme(self._read_theme_pref())
        self.palette = apply_palette(self.root, self.style, self.theme_name)
        set_titlebar_theme(self.root, self.theme_name == "dark")

        # A themed ttk.Menubutton row standing in for a real menu bar --
        # Windows draws an actual root-level tk.Menu bar with native
        # rendering that ignores color options entirely, so a native menu
        # bar can never follow dark mode. This can.
        self.file_menu = tk.Menu(self.root, tearoff=False)
        self.file_menu.add_command(label="Settings...", command=self._open_settings)

        menubar_row = ttk.Frame(self.root)
        menubar_row.pack(fill="x", side="top")
        ttk.Menubutton(
            menubar_row, text="File", menu=self.file_menu, style="Menubar.TMenubutton"
        ).pack(side="left")

        self.dark_switch = ToggleSwitch(
            menubar_row, initial=(self.theme_name == "dark"),
            on_toggle=self._on_dark_switch_toggled, palette=self.palette,
        )
        self.dark_switch.pack(side="right", padx=(0, 10), pady=2)
        ttk.Label(menubar_row, text="Dark Mode").pack(side="right", pady=2)

        self._apply_menu_theme()

        status_font = tkfont.Font(family="Segoe UI", size=11, weight="bold")
        log_font = tkfont.Font(family="Consolas", size=9)

        header = ttk.Frame(self.root, padding=(14, 12, 14, 8))
        header.pack(fill="x")

        self.status_var = tk.StringVar(value="Starting...")
        ttk.Label(
            header,
            textvariable=self.status_var,
            anchor="w",
            wraplength=580,
            justify="left",
            font=status_font,
        ).pack(fill="x", pady=(0, 10))

        self.play_button = ttk.Button(header, text="Play Now", command=self._on_play_now)
        self.play_button.pack(anchor="w")

        sync_frame = ttk.LabelFrame(self.root, text="Manual Sync", padding=(10, 8))
        sync_frame.pack(fill="x", padx=14, pady=(0, 4))

        self.upload_button = ttk.Button(
            sync_frame, text="Force Upload Current Save", command=self._on_force_upload
        )
        self.upload_button.pack(side="left")

        self.download_button = ttk.Button(
            sync_frame, text="Force Download Latest", command=self._on_force_download
        )
        self.download_button.pack(side="left", padx=(8, 0))

        log_frame = ttk.LabelFrame(self.root, text="Activity Log", padding=(8, 6))
        log_frame.pack(fill="both", expand=True, padx=14, pady=(4, 14))

        self.log_text = scrolledtext.ScrolledText(
            log_frame, state="disabled", height=16, font=log_font, wrap="word", borderwidth=0,
        )
        self.log_text.pack(fill="both", expand=True)
        self._apply_log_colors()

        handler = _QueueLogHandler(self.ui_queue)
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        logging.getLogger("moonberry-sync").addHandler(handler)

        self.root.after(100, self._poll_queue)

    def _on_play_now(self):
        self.controller.play_requested.set()
        self.ui_queue.put(("status", "Play requested — will start shortly..."))

    def _on_force_upload(self):
        self.upload_button.configure(state="disabled")
        self.ui_queue.put(("status", "Force-uploading current save..."))

        def worker():
            success, msg = self.controller.force_upload_current_save()
            self.ui_queue.put(("sync_done", ("upload", success, msg)))

        threading.Thread(target=worker, daemon=True).start()

    def _on_force_download(self):
        if not messagebox.askyesno(
            "Force Download Latest",
            "This will overwrite your current local save. A backup will be kept. Continue?",
            parent=self.root,
        ):
            return

        self.download_button.configure(state="disabled")
        self.ui_queue.put(("status", "Force-downloading latest save..."))

        def worker():
            success, msg = self.controller.force_download_latest()
            self.ui_queue.put(("sync_done", ("download", success, msg)))

        threading.Thread(target=worker, daemon=True).start()

    def _read_theme_pref(self) -> str:
        try:
            cfg = json.loads((self.controller.app_dir / "config.json").read_text(encoding="utf-8"))
            return cfg.get("theme", "system")
        except (OSError, json.JSONDecodeError):
            return "system"

    def _apply_menu_theme(self):
        apply_menu_colors(self.file_menu, self.palette)

    def _apply_log_colors(self):
        self.log_text.configure(
            background=self.palette["log_bg"],
            foreground=self.palette["log_fg"],
            insertbackground=self.palette["log_fg"],
        )
        for level in _LOG_LEVEL_TAGS:
            color = self.palette["error"] if level in ("ERROR", "CRITICAL") else self.palette["warning"]
            self.log_text.tag_config(level, foreground=color)

    def _on_dark_switch_toggled(self, is_dark: bool):
        self.theme_name = "dark" if is_dark else "light"
        self.palette = apply_palette(self.root, self.style, self.theme_name)
        set_titlebar_theme(self.root, self.theme_name == "dark")
        self._apply_menu_theme()
        self._apply_log_colors()
        self.dark_switch.set_palette(self.palette)

        config_path = self.controller.app_dir / "config.json"
        try:
            cfg = json.loads(config_path.read_text(encoding="utf-8"))
            cfg["theme"] = self.theme_name
            config_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        except (OSError, json.JSONDecodeError):
            pass  # cosmetic preference only -- not worth failing over

    def _open_settings(self):
        from games._discovery import discover_adapters
        from gui.settings import run_setup_wizard

        config_path = self.controller.app_dir / "config.json"
        adapters = discover_adapters()
        if run_setup_wizard(config_path, adapters, parent=self.root):
            messagebox.showinfo(
                "Settings saved",
                "Settings saved. Restart the app for changes to take effect.",
                parent=self.root,
            )

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
                    tag = next((lvl for lvl in _LOG_LEVEL_TAGS if f"[{lvl}]" in payload), None)
                    self.log_text.configure(state="normal")
                    self.log_text.insert("end", payload + "\n", tag if tag else ())
                    self.log_text.see("end")
                    self.log_text.configure(state="disabled")
                elif kind == "alert":
                    self._flash_attention()
                elif kind == "sync_done":
                    action, success, msg = payload
                    button = self.upload_button if action == "upload" else self.download_button
                    button.configure(state="normal")
                    self.status_var.set(msg)
                    if success:
                        messagebox.showinfo("Success", msg, parent=self.root)
                    else:
                        messagebox.showerror("Failed", msg, parent=self.root)
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
