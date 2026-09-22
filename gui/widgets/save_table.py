"""Saves tab: one row per save-slot this game has -- every local save
found on disk, plus any save the coordinator knows about that hasn't been
pulled to this machine yet -- each with its own Host / Sync actions.
Mirrors GameSyncRow's background-thread-plus-signal pattern per action,
but keyed by save name instead of by game, and reuses the SAME
SessionController methods (host_now / force_download_latest) the Overview
tab's Host Now / Force Download already call -- just with save_name set to
this row's save instead of left at the configured default."""

import logging
import threading
from datetime import datetime

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.save_status import SaveRow, compute_save_rows

log = logging.getLogger("moonberry-sync")

COLUMNS = ["Save Name", "Modified", "Owner", "Size", "Status", "Actions"]

STATUS_LABELS = {
    "in_sync": "In sync",
    "cloud_has_changes": "Cloud has changes",
    "local_only": "Local only (never uploaded)",
    "cloud_only": "Cloud only (not downloaded)",
}


def _format_size(size_bytes: int | None) -> str:
    if size_bytes is None:
        return "—"
    size = float(size_bytes)
    for unit in ("B", "KB", "MB"):
        if size < 1024:
            return f"{int(size)} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def _format_modified(modified: datetime | None) -> str:
    return modified.strftime("%Y-%m-%d %H:%M") if modified else "—"


class _SaveRowActions(QWidget):
    """One row's Host + Sync buttons. Both go straight through the same
    SessionController methods the Overview tab uses -- Host = host_now,
    Sync = force_download_latest (pull-only, never claims the host lock,
    only meaningful when this save actually has a cloud copy to pull)."""

    def __init__(self, controller, row: SaveRow, finished_signal: Signal, on_busy_changed, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.row = row
        self.finished_signal = finished_signal
        self.on_busy_changed = on_busy_changed

        layout = QHBoxLayout(self)
        layout.setContentsMargins(2, 0, 2, 0)

        self.host_button = QPushButton("Host")
        self.host_button.setToolTip(
            "Claims the host slot for this save, syncs it, then waits for you to start the game."
        )
        self.host_button.clicked.connect(self._on_host)
        layout.addWidget(self.host_button)

        self.sync_button = QPushButton("Sync")
        self.sync_button.setToolTip(
            "Downloads the cloud version of this save if it differs from local. Never claims the host slot."
        )
        self.sync_button.setEnabled(bool(row.cloud_key))
        self.sync_button.clicked.connect(self._on_sync)
        layout.addWidget(self.sync_button)

    def set_enabled(self, enabled: bool) -> None:
        self.host_button.setEnabled(enabled)
        self.sync_button.setEnabled(enabled and bool(self.row.cloud_key))

    def _on_host(self):
        answer = QMessageBox.question(
            self.window(),
            "Host Save",
            f"Claim the host slot for '{self.row.save_name}'? "
            "This syncs it (downloading it first if you don't have it yet) "
            "and then waits for you to start the game.",
        )
        if answer != QMessageBox.Yes:
            return
        self._start("host", self.controller.host_now)

    def _on_sync(self):
        answer = QMessageBox.question(
            self.window(),
            "Sync Save",
            f"Download the cloud version of '{self.row.save_name}'? "
            "This only runs if it differs from what's on disk here. A backup of "
            "your current local save (if any) will be kept. The host slot is not claimed.",
        )
        if answer != QMessageBox.Yes:
            return
        self._start("sync", self.controller.force_download_latest)

    def _start(self, action: str, fn) -> None:
        self.set_enabled(False)
        self.on_busy_changed(True)
        save_name = self.row.save_name
        slot_id = self.row.slot_id

        def worker():
            success, msg = fn(save_name=save_name)
            self.finished_signal.emit(slot_id, action, success, msg)

        threading.Thread(target=worker, daemon=True).start()


class SaveTableWidget(QWidget):
    # Bubbles up to GameDetailPanel so it can disable Overview's own
    # controls (and every other row) while any one save action is
    # mid-flight -- every action here and on Overview shares one
    # _sync_lock on the controller, so only one can genuinely run at a
    # time regardless of where it was started from.
    busy_changed = Signal(bool)
    # slot_id, action ("host"/"sync"), success, message -- emitted from a
    # background thread, marshaled to the GUI thread by Qt.
    row_action_finished = Signal(str, str, bool, str)

    def __init__(self, game_id: str, controller, parent=None):
        super().__init__(parent)
        self.game_id = game_id
        self.controller = controller
        self._latest_status: dict | None = None
        self._rows_enabled = True
        # Disk scanning (and per-save size/mtime stat-ing) only happens
        # once this tab has actually been opened at least once -- a
        # never-opened Saves tab shouldn't cost anything on every shared
        # poller cycle for every configured game.
        self._activated = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.NoSelection)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeToContents)
        layout.addWidget(self.table)

        self.row_action_finished.connect(self._on_row_action_finished)

    def refresh_local_scan(self) -> None:
        self._activated = True
        self._render()

    def set_status(self, status: dict | None) -> None:
        self._latest_status = status
        if self._activated:
            self._render()

    def set_rows_enabled(self, enabled: bool) -> None:
        self._rows_enabled = enabled
        for row_idx in range(self.table.rowCount()):
            widget = self.table.cellWidget(row_idx, len(COLUMNS) - 1)
            if isinstance(widget, _SaveRowActions):
                widget.set_enabled(enabled)

    def _render(self) -> None:
        status = self._latest_status or {}
        try:
            rows = compute_save_rows(self.controller, status)
        except Exception:
            log.exception("Failed to compute save rows for %s", self.game_id)
            rows = []

        self.table.setRowCount(len(rows))
        for i, row in enumerate(rows):
            self.table.setItem(i, 0, QTableWidgetItem(row.save_name))
            self.table.setItem(i, 1, QTableWidgetItem(_format_modified(row.local_modified)))
            self.table.setItem(i, 2, QTableWidgetItem(row.owner or "—"))
            self.table.setItem(i, 3, QTableWidgetItem(_format_size(row.local_size_bytes)))
            self.table.setItem(i, 4, QTableWidgetItem(STATUS_LABELS.get(row.status, row.status)))

            actions = _SaveRowActions(self.controller, row, self.row_action_finished, self.busy_changed.emit)
            actions.set_enabled(self._rows_enabled)
            self.table.setCellWidget(i, 5, actions)

    def _on_row_action_finished(self, slot_id: str, action: str, success: bool, msg: str) -> None:
        self.busy_changed.emit(False)
        self.refresh_local_scan()
        title = "Host Save" if action == "host" else "Sync Save"
        if success:
            QMessageBox.information(self.window(), title, msg)
        else:
            QMessageBox.critical(self.window(), title, msg)
