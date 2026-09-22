"""PySide6 GUI, replacing the old Tkinter window (and, before that, the
original system tray icon). Every "supported" game (configured, with a
local save on disk) gets its own SessionController and its own Manual Sync
row -- but only the active game's controller runs run_loop() and drives the
top status line / Play Now / Host Now, since hosting stays limited to one
game at a time, globally.

Play Now and Host Now are two separate, independent actions: Play Now just
opens the game (whether you're starting your own world or joining someone
else's -- it doesn't know or care which); Host Now claims the host slot,
syncs your save, and waits for you to start the game yourself, then
watches/uploads when you're done. Clicking Host Now before Play Now is what
gets your save synced before you play -- there's no enforced ordering
between the two buttons, syncing just isn't possible anymore once the game
is already open."""

import ctypes
import json
import logging
import os
import sys
import threading

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QTextCharFormat
from PySide6.QtWidgets import (
    QApplication,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from gui.log_handler import QtLogHandler
from gui.theme import apply_theme, resolve_theme, set_titlebar_theme
from gui.widgets.game_sync_row import GameSyncRow
from gui.widgets.toggle_switch import ToggleSwitch

_LOG_COLOR_KEYS = {"WARNING": "warning", "ERROR": "error", "CRITICAL": "error"}


class MainWindow(QMainWindow):
    # SessionController.run_loop/host_now call these from a background
    # thread -- these signals are how that reaches the GUI thread safely
    # (Qt marshals a signal emitted from any thread onto the thread the
    # receiving QObject lives on).
    status_changed = Signal(str)
    desktop_notify = Signal(str, str)
    host_now_finished = Signal(bool, str)
    ready_for_launch = Signal()
    game_started = Signal()

    def __init__(self, game_controllers: dict, active_game_id: str, app_version: str):
        super().__init__()
        self.game_controllers = game_controllers
        self.active_game_id = active_game_id
        self.controller = game_controllers[active_game_id]
        self.app_version = app_version

        self.setWindowTitle(f"Moonberry Save-Sync — {self.controller.adapter.display_name}")
        self.resize(680, 540)
        self.setMinimumSize(520, 380)

        self.theme_name = resolve_theme(self._read_theme_pref())
        self.colors = apply_theme(QApplication.instance(), self.theme_name)

        self._build_menu_bar()
        self._build_central_widget()

        self._log_handler = QtLogHandler()
        self._log_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        self._log_handler.log_line.connect(self._append_log_line)
        logging.getLogger("moonberry-sync").addHandler(self._log_handler)

        self.status_changed.connect(self.status_label.setText)
        self.desktop_notify.connect(self._on_desktop_notify)
        self.host_now_finished.connect(self._on_host_now_finished)
        self.ready_for_launch.connect(self._on_ready_for_launch)
        self.game_started.connect(self._on_game_started)

    def showEvent(self, event):
        super().showEvent(event)
        set_titlebar_theme(self, self.theme_name == "dark")

    # -- construction --

    def _build_menu_bar(self):
        menubar = self.menuBar()
        file_menu = menubar.addMenu("File")
        file_menu.addAction("Settings...", self._open_settings)

        corner = QWidget()
        corner_layout = QHBoxLayout(corner)
        corner_layout.setContentsMargins(0, 0, 10, 0)
        corner_layout.addWidget(QLabel("Dark Mode"))
        self.dark_switch = ToggleSwitch(initial=(self.theme_name == "dark"), palette=self.colors)
        self.dark_switch.toggled.connect(self._on_dark_toggled)
        corner_layout.addWidget(self.dark_switch)
        menubar.setCornerWidget(corner, Qt.TopRightCorner)

    def _build_central_widget(self):
        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(14, 12, 14, 14)
        layout.setSpacing(10)

        self.status_label = QLabel("Starting...")
        self.status_label.setProperty("role", "status")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self.play_button = QPushButton("Play Now")
        self.play_button.setProperty("role", "primary")
        self.play_button.setToolTip(f"Just opens {self.controller.adapter.display_name} -- doesn't claim host or sync.")
        self.play_button.clicked.connect(self._on_play_now)

        self.host_button = QPushButton("Host Now")
        self.host_button.setProperty("role", "primary")
        self.host_button.setToolTip(
            "Claims the host slot, syncs your save, then waits for you to start the game."
        )
        self.host_button.clicked.connect(self._on_host_now)

        self.stop_host_button = QPushButton("Stop Host")
        self.stop_host_button.setProperty("role", "danger")
        self.stop_host_button.setToolTip(
            "Cancels a Host Now call that's still waiting for you to start the game."
        )
        self.stop_host_button.setEnabled(False)
        self.stop_host_button.clicked.connect(self._on_stop_host)

        # Host Now and Stop Host share one slot in the layout -- Stop Host
        # only ever makes sense in place of Host Now (never alongside it),
        # so swapping which one occupies that spot reads more clearly than
        # a third separate button sitting next to it.
        self.host_stack = QStackedWidget()
        self.host_stack.addWidget(self.host_button)
        self.host_stack.addWidget(self.stop_host_button)
        self.host_stack.setCurrentWidget(self.host_button)

        play_row = QHBoxLayout()
        play_row.addWidget(self.host_stack)
        play_row.addWidget(self.play_button)
        play_row.addStretch()
        layout.addLayout(play_row)

        sync_group = QGroupBox("Manual Sync")
        sync_layout = QVBoxLayout(sync_group)
        for game_id in self._ordered_game_ids():
            controller = self.game_controllers[game_id]
            row = GameSyncRow(
                game_id,
                controller.adapter.display_name,
                controller,
                is_active=(game_id == self.active_game_id),
            )
            sync_layout.addWidget(row)
        layout.addWidget(sync_group)

        log_group = QGroupBox("Activity Log")
        log_layout = QVBoxLayout(log_group)
        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setProperty("role", "log")
        log_layout.addWidget(self.log_view)
        layout.addWidget(log_group, stretch=1)

        self.setCentralWidget(central)

    def _ordered_game_ids(self) -> list[str]:
        others = sorted(g for g in self.game_controllers if g != self.active_game_id)
        return [self.active_game_id] + others

    # -- actions --

    def _on_play_now(self):
        # Deliberately dumb: just opens the game, no claim, no status
        # check, no sync. Joining someone else's session never touches
        # your local save (only the host's does), and starting your own
        # world is Host Now's job to wrap with claim/sync/upload -- this
        # button's only job is opening the game itself.
        try:
            self.controller.adapter.launch()
        except Exception as e:
            QMessageBox.critical(
                self, "Launch failed", f"Could not launch {self.controller.adapter.display_name}: {e}"
            )

    def _on_host_now(self):
        self.host_button.setEnabled(False)
        self.play_button.setEnabled(False)
        self.status_changed.emit("Claiming host...")

        def worker():
            success, msg = self.controller.host_now(
                update_status_text=self.status_changed.emit,
                on_ready_for_launch=self.ready_for_launch.emit,
                on_game_started=self.game_started.emit,
            )
            self.host_now_finished.emit(success, msg)

        threading.Thread(target=worker, daemon=True).start()

    def _on_ready_for_launch(self):
        self.play_button.setEnabled(True)
        # This is exactly the window Stop Host exists for: claimed and
        # synced, but still waiting for you to actually start the game --
        # cancelling stops making sense the moment it's actually running
        # (_on_game_started swaps back and disables it again then).
        self.stop_host_button.setEnabled(True)
        self.host_stack.setCurrentWidget(self.stop_host_button)

    def _on_stop_host(self):
        # stop_host() just sets a flag Host Now's own thread checks on its
        # next poll -- instant and thread-safe, no need for a thread of
        # its own here.
        self.stop_host_button.setEnabled(False)
        self.status_changed.emit("Stopping...")
        self.controller.stop_host()

    def _on_game_started(self):
        self.stop_host_button.setEnabled(False)
        self.host_stack.setCurrentWidget(self.host_button)  # still disabled -- session's still wrapping up

    def _on_host_now_finished(self, success: bool, msg: str):
        self.host_button.setEnabled(True)
        self.play_button.setEnabled(True)
        self.stop_host_button.setEnabled(False)
        self.host_stack.setCurrentWidget(self.host_button)  # covers the cancelled-before-game-started case too
        if success:
            QMessageBox.information(self, "Host Now", msg)
        else:
            QMessageBox.critical(self, "Host Now", msg)

    def _read_theme_pref(self) -> str:
        try:
            cfg = json.loads((self.controller.app_dir / "config.json").read_text(encoding="utf-8"))
            return cfg.get("theme", "system")
        except (OSError, json.JSONDecodeError):
            return "system"

    def _on_dark_toggled(self, is_dark: bool):
        self.theme_name = "dark" if is_dark else "light"
        self.colors = apply_theme(QApplication.instance(), self.theme_name)
        set_titlebar_theme(self, self.theme_name == "dark")
        self.dark_switch.set_palette(self.colors)

        config_path = self.controller.app_dir / "config.json"
        try:
            cfg = json.loads(config_path.read_text(encoding="utf-8"))
            cfg["theme"] = self.theme_name
            config_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        except (OSError, json.JSONDecodeError):
            pass  # cosmetic preference only -- not worth failing over

    def _open_settings(self):
        from games._discovery import discover_adapters
        from gui.settings import SettingsDialog

        config_path = self.controller.app_dir / "config.json"
        adapters = discover_adapters()
        dialog = SettingsDialog(config_path, adapters, parent=self)
        dialog.exec()
        if dialog.saved:
            QMessageBox.information(
                self,
                "Settings saved",
                "Settings saved. Restart the app for changes to take effect.",
            )

    def closeEvent(self, event):
        os._exit(0)

    # -- callbacks handed to SessionController.run_loop; called from the
    # background poll thread, so they only ever go through Qt signals --

    def _append_log_line(self, line: str, level: str):
        color_key = _LOG_COLOR_KEYS.get(level)
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(self.colors[color_key] if color_key else self.colors["log_fg"]))
        cursor = self.log_view.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        cursor.insertText(line + "\n", fmt)
        self.log_view.setTextCursor(cursor)
        self.log_view.ensureCursorVisible()

    def _on_desktop_notify(self, title: str, message: str):
        self.status_changed.emit(f"{title}: {message}")
        self._flash_attention()

    def _flash_attention(self):
        QApplication.beep()
        if sys.platform == "win32":
            try:
                ctypes.windll.user32.FlashWindow(int(self.winId()), True)
            except Exception:
                pass

    def run_session_loop(self):
        t = threading.Thread(
            target=self.controller.run_loop,
            args=(self.status_changed.emit, self.desktop_notify.emit),
            daemon=True,
        )
        t.start()


class App:
    """Thin wrapper matching main.py's expected App(...).run() shape --
    owns the QApplication instance, since one must exist before any QWidget
    (including MainWindow) is constructed."""

    def __init__(self, game_controllers: dict, active_game_id: str, app_version: str):
        self.qapp = QApplication.instance() or QApplication(sys.argv)
        self.window = MainWindow(game_controllers, active_game_id, app_version)

    def run(self):
        self.window.show()
        self.window.run_session_loop()
        self.qapp.exec()
