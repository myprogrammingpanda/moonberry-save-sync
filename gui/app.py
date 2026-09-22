"""PySide6 GUI. Sidebar of configured games (each with its own live status)
+ a detail pane for whichever one is selected -- every game gets its own
Play Now/Host Now/Force Upload/Force Download, independent of the others,
since Play Now/Host Now used to be hardwired to a single global
"active_game" (whichever was in config.json), which meant Host Now always
acted on that one game regardless of which one you actually meant to host.

Only one person can actually be hosting (any game) at a time -- that's
still a single global claim via the coordinator, not concurrent per-game
hosting -- but the coordinator's claim now carries a game_id (see
core/status_poller.py and core/coordinator.py), so every sidebar row can
show the right live status instead of only the one game the old UI
happened to be locked to."""

import ctypes
import json
import logging
import os
import sys
import threading
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QTextCharFormat
from PySide6.QtWidgets import (
    QApplication,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from core.status_poller import StatusPoller
from gui.log_handler import QtLogHandler
from gui.theme import apply_theme, resolve_theme, set_titlebar_theme
from gui.widgets.game_detail_panel import GameDetailPanel
from gui.widgets.toggle_switch import ToggleSwitch

log = logging.getLogger("moonberry-sync")

_LOG_COLOR_KEYS = {"WARNING": "warning", "ERROR": "error", "CRITICAL": "error"}
_GAME_ID_ROLE = Qt.UserRole


class MainWindow(QMainWindow):
    status_changed = Signal(str)
    desktop_notify = Signal(str, str)
    poller_status = Signal(object)  # raw coordinator status dict, or None on error
    update_info = Signal(object)
    update_prep_finished = Signal(bool, str, str)

    def __init__(
        self,
        game_controllers: dict,
        active_game_id: str,
        app_version: str,
        app_dir: Path,
        coordinator,
        player_name: str,
        update_checker,
        poll_interval_seconds: float,
    ):
        super().__init__()
        self.game_controllers = game_controllers
        self.active_game_id = active_game_id
        self.app_version = app_version
        self.app_dir = app_dir
        self.coordinator = coordinator
        self.player_name = player_name
        self.update_checker = update_checker
        self.poll_interval_seconds = poll_interval_seconds
        self._pending_release_info: dict | None = None
        self._update_in_progress = False
        self.panels: dict[str, GameDetailPanel] = {}

        self.setWindowTitle("Moonberry Save-Sync")
        self.resize(760, 560)
        self.setMinimumSize(600, 420)

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
        self.poller_status.connect(self._on_poller_status)
        self.update_info.connect(self._on_update_info)
        self.update_prep_finished.connect(self._on_update_prep_finished)

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

        top_row = QHBoxLayout()
        self.status_label = QLabel("Starting...")
        self.status_label.setProperty("role", "status")
        self.status_label.setWordWrap(True)
        top_row.addWidget(self.status_label, stretch=1)

        self.update_button = QPushButton()
        self.update_button.setProperty("role", "success")
        self.update_button.setToolTip("Downloads and installs the new version, then restarts the app.")
        self.update_button.setVisible(False)
        self.update_button.clicked.connect(self._on_update_now)
        top_row.addWidget(self.update_button)
        layout.addLayout(top_row)

        split_row = QHBoxLayout()
        split_row.setSpacing(10)

        self.game_list = QListWidget()
        self.game_list.setFixedWidth(200)
        self.game_list.currentItemChanged.connect(self._on_game_selected)
        split_row.addWidget(self.game_list)

        self.panel_stack = QStackedWidget()
        for game_id in sorted(self.game_controllers):
            panel = GameDetailPanel(game_id, self.game_controllers[game_id])
            self.panels[game_id] = panel
            self.panel_stack.addWidget(panel)

            item = QListWidgetItem(self.game_controllers[game_id].adapter.display_name)
            item.setData(_GAME_ID_ROLE, game_id)
            self.game_list.addItem(item)

        split_row.addWidget(self.panel_stack, stretch=1)
        layout.addLayout(split_row, stretch=1)

        log_group = QGroupBox("Activity Log")
        log_layout = QVBoxLayout(log_group)
        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setProperty("role", "log")
        log_layout.addWidget(self.log_view)
        layout.addWidget(log_group, stretch=1)

        self.setCentralWidget(central)

        self._select_game(self.active_game_id)

    def _select_game(self, game_id: str) -> None:
        for i in range(self.game_list.count()):
            item = self.game_list.item(i)
            if item.data(_GAME_ID_ROLE) == game_id:
                self.game_list.setCurrentItem(item)
                return

    # -- selection --

    def _on_game_selected(self, current: QListWidgetItem, _previous: QListWidgetItem):
        if not current:
            return
        game_id = current.data(_GAME_ID_ROLE)
        self.panel_stack.setCurrentWidget(self.panels[game_id])

    # -- status polling --

    def run_status_poller(self):
        poller = StatusPoller(
            self.coordinator, self.game_controllers, self.player_name, self.update_checker, self.poll_interval_seconds
        )
        t = threading.Thread(
            target=poller.run_loop,
            args=(self.poller_status.emit, self.desktop_notify.emit, self.update_info.emit),
            daemon=True,
        )
        t.start()

    def _on_poller_status(self, status: dict | None):
        if status is None:
            self.status_label.setText("Coordinator unreachable — retrying...")
            return
        self.status_label.setText("")

        for i, game_id in enumerate(sorted(self.game_controllers)):
            text = self._resolve_status_text(game_id, status)
            self.game_list.item(i).setText(f"{self.game_controllers[game_id].adapter.display_name}  ·  {text}")
            self.panels[game_id].set_status_text(text)

    def _resolve_status_text(self, game_id: str, status: dict) -> str:
        controller = self.game_controllers[game_id]
        if controller.adapter.is_running():
            return "Running"
        if status.get("hosting") and status.get("game_id") == game_id:
            host_name = status.get("host_name")
            join_code = status.get("join_code")
            code_part = f" (Join Code: {join_code})" if join_code else ""
            return f"Hosting — {host_name}{code_part}"
        if not controller.adapter.has_local_save():
            return "Not set up"
        return "Idle"

    # -- updates (app-wide, not per-game -- an update replaces this app's
    # own files and restarts it, so it's blocked while ANY game is hosting) --

    def _any_host_busy(self) -> bool:
        return any(not panel.host_button.isEnabled() for panel in self.panels.values())

    def _set_all_panels_enabled(self, enabled: bool) -> None:
        for panel in self.panels.values():
            panel.play_button.setEnabled(enabled)
            panel.host_button.setEnabled(enabled)

    def _on_update_now(self):
        release_info = self._pending_release_info
        if not release_info:
            return
        tag = release_info.get("tag_name", "").lstrip("v")
        reply = QMessageBox.question(
            self,
            "Update Moonberry Save-Sync",
            f"Update to v{tag}? The app will close, update, and reopen automatically.",
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        self._update_in_progress = True
        self.update_button.setEnabled(False)
        self._set_all_panels_enabled(False)
        self.status_changed.emit("Downloading update...")

        def report_progress(done: int, total: int | None):
            done_kb = done // 1024
            if total:
                self.status_changed.emit(f"Downloading update... {done_kb} / {total // 1024} KB")
            else:
                self.status_changed.emit(f"Downloading update... {done_kb} KB")

        def worker():
            from core.updater import UpdateError, prepare_update

            try:
                staging_dir = prepare_update(self.app_dir, release_info, progress_cb=report_progress)
            except UpdateError as e:
                self.update_prep_finished.emit(False, str(e), "")
                return
            except Exception as e:
                log.exception("Unexpected error preparing update.")
                self.update_prep_finished.emit(False, f"Unexpected error: {e}", "")
                return
            self.update_prep_finished.emit(True, "", str(staging_dir))

        threading.Thread(target=worker, daemon=True).start()

    def _on_update_prep_finished(self, success: bool, error_msg: str, staging_dir: str):
        if not success:
            self._update_in_progress = False
            self.update_button.setEnabled(True)
            self._set_all_panels_enabled(True)
            self.status_changed.emit("Update failed.")
            QMessageBox.critical(self, "Update failed", f"Could not prepare the update:\n\n{error_msg}")
            return

        from core.updater import begin_relaunch

        self.status_changed.emit("Update ready — restarting...")
        begin_relaunch(self.app_dir, Path(staging_dir))
        self.close()

    def _on_update_info(self, release_info: dict | None):
        self._pending_release_info = release_info
        self.update_button.setVisible(release_info is not None)
        if release_info:
            tag = release_info.get("tag_name", "").lstrip("v")
            self.update_button.setText(f"Update to v{tag}")
            self.update_button.setEnabled(not self._any_host_busy() and not self._update_in_progress)

    # -- theme / settings --

    def _read_theme_pref(self) -> str:
        try:
            cfg = json.loads((self.app_dir / "config.json").read_text(encoding="utf-8"))
            return cfg.get("theme", "system")
        except (OSError, json.JSONDecodeError):
            return "system"

    def _on_dark_toggled(self, is_dark: bool):
        self.theme_name = "dark" if is_dark else "light"
        self.colors = apply_theme(QApplication.instance(), self.theme_name)
        set_titlebar_theme(self, self.theme_name == "dark")
        self.dark_switch.set_palette(self.colors)

        config_path = self.app_dir / "config.json"
        try:
            cfg = json.loads(config_path.read_text(encoding="utf-8"))
            cfg["theme"] = self.theme_name
            config_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        except (OSError, json.JSONDecodeError):
            pass  # cosmetic preference only -- not worth failing over

    def _open_settings(self):
        from games._discovery import discover_adapters
        from gui.settings import SettingsDialog

        config_path = self.app_dir / "config.json"
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

    # -- log / notifications --

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


class App:
    """Thin wrapper matching main.py's expected App(...).run() shape --
    owns the QApplication instance, since one must exist before any QWidget
    (including MainWindow) is constructed."""

    def __init__(
        self,
        game_controllers: dict,
        active_game_id: str,
        app_version: str,
        app_dir: Path,
        coordinator,
        player_name: str,
        update_checker,
        poll_interval_seconds: float,
    ):
        self.qapp = QApplication.instance() or QApplication(sys.argv)
        self.window = MainWindow(
            game_controllers,
            active_game_id,
            app_version,
            app_dir,
            coordinator,
            player_name,
            update_checker,
            poll_interval_seconds,
        )

    def run(self):
        self.window.show()
        self.window.run_status_poller()
        self.qapp.exec()
