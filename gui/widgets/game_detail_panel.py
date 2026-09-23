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
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from gui.widgets.game_settings_dialog import GameSettingsDialog
from gui.widgets.game_sync_row import GameSyncRow
from gui.widgets.save_table import SaveTableWidget

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

        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(0, 0, 0, 0)

        # None while idle/using the configured default save; set for the
        # span of a Host Now session that was started by "slotting in" a
        # non-default save from the Saves tab (see _start_hosting_save) --
        # lets Overview's already-complete Play Now/Stop Host flow work
        # for ANY save, not just the configured default, without
        # duplicating that flow inside the Saves tab itself.
        self._active_save_name: str | None = None

        self.tabs = QTabWidget()
        outer_layout.addWidget(self.tabs)

        self.overview_tab = QWidget()
        overview_tab = self.overview_tab
        layout = QVBoxLayout(overview_tab)
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

        # GameAdapter.setup_problem, re-checked on every status poll so it
        # disappears by itself once fixed.
        self.setup_warning = QLabel("")
        self.setup_warning.setProperty("role", "warning")
        self.setup_warning.setWordWrap(True)
        layout.addWidget(self.setup_warning)
        self._refresh_setup_warning()

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

        # Only for games whose join code can't be read automatically (see
        # GameAdapter.join_code_entry): enabled while you're actually
        # hosting, between the game starting and the session wrapping up.
        self.join_code_row = QWidget()
        join_layout = QHBoxLayout(self.join_code_row)
        join_layout.setContentsMargins(0, 0, 0, 0)
        join_layout.addWidget(QLabel("Join code"))
        self.join_code_edit = QLineEdit()
        self.join_code_edit.setPlaceholderText("Paste the code from the game's pause menu")
        self.join_code_edit.returnPressed.connect(self._on_share_join_code)
        join_layout.addWidget(self.join_code_edit, stretch=1)
        self.share_code_button = QPushButton("Share")
        self.share_code_button.setToolTip("Shows this code to everyone in the group.")
        self.share_code_button.clicked.connect(self._on_share_join_code)
        join_layout.addWidget(self.share_code_button)
        self.join_code_row.setVisible(controller.adapter.join_code_entry)
        self._set_join_code_enabled(False)
        layout.addWidget(self.join_code_row)

        self.sync_row = GameSyncRow(game_id, controller.adapter.display_name, controller, is_active=False)
        layout.addWidget(self.sync_row)

        layout.addStretch()

        self.tabs.addTab(overview_tab, "Overview")

        self.save_table = SaveTableWidget(game_id, controller)
        self.tabs.addTab(self.save_table, "Saves")
        self.tabs.currentChanged.connect(self._on_tab_changed)

        self.host_now_finished.connect(self._on_host_now_finished)
        self.ready_for_launch.connect(self._on_ready_for_launch)
        self.game_started.connect(self._on_game_started)
        self.sync_row.busy_changed.connect(self._set_busy)
        self.save_table.busy_changed.connect(self._set_busy)
        self.save_table.host_requested.connect(self._start_hosting_save)

        self._set_active_save(None)  # show the configured default save's name from the start

    def _on_tab_changed(self, index: int) -> None:
        if self.tabs.widget(index) is self.save_table:
            self.save_table.refresh_local_scan()

    def _set_busy(self, busy: bool) -> None:
        """Disables every action across both tabs while ANY one of them is
        mid-flight. Overview's Host Now/Force Upload/Force Download and
        every Saves-tab row's Host/Sync action all share one _sync_lock on
        the controller -- only one can genuinely run at a time -- so this
        keeps the UI honest about that instead of letting a second click
        just silently fail with "already in progress" from a background
        thread."""
        self.host_button.setEnabled(not busy)
        self.play_button.setEnabled(not busy)
        self.sync_row.set_enabled(not busy)
        self.save_table.set_rows_enabled(not busy)

    def _set_active_save(self, save_name: str | None) -> None:
        """None means "use the configured default save" -- the ordinary,
        always-worked case. Non-None means Overview's actions (Host Now,
        and Force Upload/Download via GameSyncRow) are scoped to that
        specific save instead, for the span of a Saves-tab-initiated Host
        session. Always reflected in the title, so it's never ambiguous
        which save Overview's buttons currently act on -- not just while
        a non-default save is slotted in."""
        self._active_save_name = save_name
        self.sync_row.set_active_save(save_name)
        effective_name = save_name or self.controller.adapter.default_save_name
        self.title_label.setText(f"{self.controller.adapter.display_name} — {effective_name}")

    def _start_hosting_save(self, save_name: str) -> None:
        """Entry point for the Saves tab's Host button: "slots in" the
        chosen save as Overview's active target and switches to it, then
        runs the exact same Host Now flow a manual click on Overview's own
        Host Now button would -- rather than duplicating that flow (claim/
        sync/wait-for-you-to-play/watch/upload/release, plus its Play Now
        and Stop Host integration) separately inside the Saves tab, where
        it would have no way to actually let you launch the game."""
        self._set_active_save(save_name)
        self.tabs.setCurrentWidget(self.overview_tab)
        self._on_host_now()

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
        self._set_busy(True)
        self.status_label.setText("Claiming host...")
        save_name = self._active_save_name

        def worker():
            success, msg = self.controller.host_now(
                save_name=save_name,
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
        self._set_join_code_enabled(True)

    def _set_join_code_enabled(self, enabled: bool) -> None:
        self.join_code_edit.setEnabled(enabled)
        self.share_code_button.setEnabled(enabled)
        if not enabled:
            self.join_code_edit.clear()

    def _on_share_join_code(self):
        code = self.join_code_edit.text().strip()
        if code and self.share_code_button.isEnabled():
            self.controller.submit_join_code(code)
            self.status_label.setText(f"Sharing join code {code}...")

    def _on_host_now_finished(self, success: bool, msg: str):
        self._set_join_code_enabled(False)
        self._set_busy(False)
        self.stop_host_button.setEnabled(False)
        self.host_stack.setCurrentWidget(self.host_button)
        # Deliberately NOT reset back to None/default here: _active_save_name
        # is "default/last-used" (see _set_active_save's docstring), so a
        # session you slotted in from the Saves tab stays the active save
        # -- and stays reflected in the title -- until you explicitly slot
        # in something else, rather than silently snapping back to the
        # configured default the moment the session ends (confusing: the
        # title would stop matching what you actually just did).
        self.save_table.refresh_local_scan()
        if success:
            QMessageBox.information(self, "Host Now", msg)
        else:
            QMessageBox.critical(self, "Host Now", msg)

    # -- status, driven by MainWindow's shared StatusPoller --

    def set_status_text(self, text: str) -> None:
        # While Host Now is running (host_button disabled), its own
        # update_status_text calls above already keep this label live
        # ("Claiming host...", "You're hosting...", etc) -- the poller's
        # idle/hosted-by-someone-else text would otherwise fight with that.
        if self.host_button.isEnabled():
            self.status_label.setText(text)

    def set_status(self, status: dict | None) -> None:
        """Forwards the shared poller's raw status dict to the Saves tab
        -- the only per-poll-cycle work it does; local disk scanning stays
        gated on that tab actually having been opened (see
        SaveTableWidget._activated)."""
        self._refresh_setup_warning()
        self.save_table.set_status(status)

    def _refresh_setup_warning(self) -> None:
        problem = self.controller.adapter.setup_problem()
        self.setup_warning.setText(f"⚠ {problem}" if problem else "")
        self.setup_warning.setVisible(bool(problem))
