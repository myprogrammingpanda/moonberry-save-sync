"""PySide6 GUI, replacing the old Tkinter window (and, before that, the
original system tray icon). Every "supported" game (configured, with a
local save on disk) gets its own SessionController and its own Manual Sync
row -- but only the active game's controller runs run_loop() and drives the
top status line / Play Now, since hosting stays limited to one game at a
time, globally."""

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
    # SessionController.run_loop calls its update_status_text/notify_desktop
    # callbacks from a background thread -- these signals are how that
    # reaches the GUI thread safely (Qt marshals a signal emitted from any
    # thread onto the thread the receiving QObject lives on).
    status_changed = Signal(str)
    desktop_notify = Signal(str, str)
    hosting_active_changed = Signal(bool)

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
        self.hosting_active_changed.connect(self._on_hosting_active_changed)

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
        self.play_button.clicked.connect(self._on_play_now)
        play_row = QHBoxLayout()
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
        self.controller.play_requested.set()
        self.status_changed.emit("Play requested — will start shortly...")

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

    def _on_hosting_active_changed(self, active: bool):
        # Disabled for the whole claim-to-release span of a hosting
        # session, not just while the game process is open -- clicking
        # Play Now during the claim/sync-down or zip/upload phases (game
        # not running yet, or not running anymore, but the session is
        # still very much in progress) would otherwise silently queue
        # another auto-host attempt for right after this one finishes.
        self.play_button.setEnabled(not active)

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
            args=(self.status_changed.emit, self.desktop_notify.emit, self.hosting_active_changed.emit),
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
