"""One game's detail pane: Play Now / Host Now / Stop Host for that game's
own SessionController, plus its Force Upload/Force Download row (reused
as-is from GameSyncRow). One instance per configured game, kept alive in
MainWindow's QStackedWidget so an in-flight Host Now on a game you've
scrolled away from keeps its own button state correct instead of being
recreated (and losing that state) every time you switch which game is
selected in the sidebar."""

import logging
import threading

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QMessageBox, QPushButton, QSizePolicy, QStackedWidget, QVBoxLayout, QWidget

from gui.widgets.game_settings_dialog import GameSettingsDialog
from gui.widgets.game_sync_row import GameSyncRow

log = logging.getLogger("moonberry-sync")


class GameDetailPanel(QWidget):
    # Mirrors the signals MainWindow's top section used to emit for the one
    # active controller -- now one set per game, so each panel marshals its
    # own SessionController.host_now() callbacks onto the GUI thread.
    host_now_finished = Signal(bool, str)
    ready_for_launch = Signal()
    game_started = Signal()

    def __init__(self, game_id: str, controller, parent=None):
        super().__init__(parent)
        self.game_id = game_id
        self.controller = controller

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        title_row = QHBoxLayout()
        self.title_label = QLabel(controller.adapter.display_name)
        self.title_label.setProperty("role", "status")
        title_row.addWidget(self.title_label)
        title_row.addStretch()
        settings_button = QPushButton("Settings...")
        settings_button.setToolTip(f"Edit {controller.adapter.display_name}'s own settings (world name, save folder, etc).")
        settings_button.clicked.connect(self._open_game_settings)
        title_row.addWidget(settings_button)
        layout.addLayout(title_row)

        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self.play_button = QPushButton("Play Now")
        self.play_button.setProperty("role", "primary")
        self.play_button.setToolTip(f"Just opens {controller.adapter.display_name} -- doesn't claim host or sync.")
        self.play_button.clicked.connect(self._on_play_now)

        self.host_button = QPushButton("Host Now")
        self.host_button.setProperty("role", "success")
        self.host_button.setToolTip("Claims the host slot, syncs your save, then waits for you to start the game.")
        self.host_button.clicked.connect(self._on_host_now)

        self.stop_host_button = QPushButton("Stop Host")
        self.stop_host_button.setProperty("role", "danger")
        self.stop_host_button.setToolTip("Cancels a Host Now call that's still waiting for you to start the game.")
        self.stop_host_button.setEnabled(False)
        self.stop_host_button.clicked.connect(self._on_stop_host)

        # Same swap-in-place pattern as the old single-game top section --
        # Stop Host only ever makes sense in place of Host Now, never
        # alongside it.
        self.host_stack = QStackedWidget()
        self.host_stack.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.host_stack.addWidget(self.host_button)
        self.host_stack.addWidget(self.stop_host_button)
        self.host_stack.setCurrentWidget(self.host_button)

        equal_width = self.play_button.sizeHint().width()
        self.play_button.setFixedWidth(equal_width)
        self.host_stack.setFixedWidth(equal_width)

        play_row = QHBoxLayout()
        play_row.addWidget(self.host_stack)
        play_row.addWidget(self.play_button)
        play_row.addStretch()
        layout.addLayout(play_row)

        self.sync_row = GameSyncRow(game_id, controller.adapter.display_name, controller, is_active=False)
        layout.addWidget(self.sync_row)

        layout.addStretch()

        self.host_now_finished.connect(self._on_host_now_finished)
        self.ready_for_launch.connect(self._on_ready_for_launch)
        self.game_started.connect(self._on_game_started)

    # -- per-game settings --

    def _open_game_settings(self):
        config_path = self.controller.app_dir / "config.json"
        dialog = GameSettingsDialog(config_path, self.game_id, type(self.controller.adapter), parent=self)
        dialog.exec()
        if dialog.saved:
            QMessageBox.information(
                self,
                "Settings saved",
                f"{self.controller.adapter.display_name} settings saved. Restart the app for changes to take effect.",
            )

    # -- actions --

    def _on_play_now(self):
        try:
            self.controller.adapter.launch()
        except Exception as e:
            QMessageBox.critical(self, "Launch failed", f"Could not launch {self.controller.adapter.display_name}: {e}")

    def _on_host_now(self):
        self.host_button.setEnabled(False)
        self.play_button.setEnabled(False)
        self.status_label.setText("Claiming host...")

        def worker():
            success, msg = self.controller.host_now(
                update_status_text=lambda text: self.status_label.setText(text),
                on_ready_for_launch=self.ready_for_launch.emit,
                on_game_started=self.game_started.emit,
            )
            self.host_now_finished.emit(success, msg)

        threading.Thread(target=worker, daemon=True).start()

    def _on_ready_for_launch(self):
        self.play_button.setEnabled(True)
        self.stop_host_button.setEnabled(True)
        self.host_stack.setCurrentWidget(self.stop_host_button)

    def _on_stop_host(self):
        self.stop_host_button.setEnabled(False)
        self.status_label.setText("Stopping...")
        self.controller.stop_host()

    def _on_game_started(self):
        self.stop_host_button.setEnabled(False)
        self.host_stack.setCurrentWidget(self.host_button)  # still disabled -- session's still wrapping up

    def _on_host_now_finished(self, success: bool, msg: str):
        self.host_button.setEnabled(True)
        self.play_button.setEnabled(True)
        self.stop_host_button.setEnabled(False)
        self.host_stack.setCurrentWidget(self.host_button)
        if success:
            QMessageBox.information(self, "Host Now", msg)
        else:
            QMessageBox.critical(self, "Host Now", msg)

    # -- status text, driven by MainWindow's shared StatusPoller --

    def set_status_text(self, text: str) -> None:
        # While Host Now is running (host_button disabled), its own
        # update_status_text calls above already keep this label live
        # ("Claiming host...", "You're hosting...", etc) -- the poller's
        # idle/hosted-by-someone-else text would otherwise fight with that.
        if self.host_button.isEnabled():
            self.status_label.setText(text)
