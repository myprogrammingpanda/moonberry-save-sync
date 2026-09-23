"""Saves tab: one row per save-slot this game has -- every local save
found on disk, plus any save the coordinator knows about that hasn't been
pulled to this machine yet -- each with its own Host / Sync action.

Sync is self-contained here (pull-only, no waiting on you to play, so
there's nothing for it to hand off). Host is NOT run directly from a row:
Host Now's full flow (claim/sync/wait-for-you-to-start-the-game/watch/
upload/release) is already built on the Overview tab, complete with Play
Now and Stop Host integration a row here has no way to reproduce -- so a
row's Host button just asks GameDetailPanel to "slot in" that save as
Overview's active target and switch to it, then runs Overview's own,
already-complete Host Now (see GameDetailPanel._start_hosting_save)."""

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
    "local_only": "Local only",
    "cloud_only": "Cloud only",
}

# Longer explanations, as tooltips rather than in the cell text itself --
# a couple of these ballooned the Status column wide enough to force a
# horizontal scrollbar for even a short, ordinary save list.
STATUS_TOOLTIPS = {
    "in_sync": "Your local copy matches the latest version in the cloud.",
    "cloud_has_changes": "The cloud has a version that differs from what's on disk here.",
    "local_only": "Exists on disk here, but has never been uploaded.",
    "cloud_only": "Known to the cloud, but not on this machine's disk yet.",
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
    """One row's Host + Sync buttons. Sync runs right here (pull-only,
    no host claim, no waiting on you to play -- see SaveTableWidget's
    docstring). Host doesn't run anything itself: it just asks
    (via on_host_requested) to be slotted in as Overview's active save
    and switched to, where the real Host Now flow already lives."""

    def __init__(self, controller, row: SaveRow, finished_signal: Signal, on_busy_changed, on_host_requested, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.row = row
        self.finished_signal = finished_signal
        self.on_busy_changed = on_busy_changed
        self.on_host_requested = on_host_requested

        layout = QHBoxLayout(self)
        layout.setContentsMargins(2, 0, 2, 0)

        self.host_button = QPushButton("Host")
        self.host_button.setToolTip(
            "Switches to the Overview tab with this save slotted in, then starts Host Now for it."
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
            f"Switch to Overview with '{self.row.save_name}' as the active save, and start Host Now for it? "
            "It'll be synced (downloaded first if you don't have it yet), then you start the game from there.",
        )
        if answer != QMessageBox.Yes:
            return
        self.on_host_requested(self.row.save_name)

    def _on_sync(self):
        local_str = _format_modified(self.row.local_modified)
        cloud_str = _format_modified(self.row.cloud_modified)
        comparison = ""
        if self.row.local_modified and self.row.cloud_modified:
            if self.row.local_modified > self.row.cloud_modified:
                comparison = (
                    "\n\nYour local save looks NEWER than the cloud version -- syncing "
                    "would overwrite it with the older cloud copy. (A file's modified time "
                    "isn't a guarantee of more progress, so use this as a hint, not certainty.)"
                )
            else:
                comparison = "\n\nThe cloud version looks newer than your local save."

        answer = QMessageBox.question(
            self.window(),
            "Sync Save",
            f"Download the cloud version of '{self.row.save_name}'?\n\n"
            f"Local last modified: {local_str}\n"
            f"Cloud last uploaded: {cloud_str}"
            f"{comparison}\n\n"
            "This only runs if the cloud version differs from what's on disk. A backup "
            "of your current local save (if any) will be kept. The host slot is not claimed.",
        )
        if answer != QMessageBox.Yes:
            return
        self._start_sync()

    def _start_sync(self) -> None:
        self.set_enabled(False)
        self.on_busy_changed(True)
        save_name = self.row.save_name
        slot_id = self.row.slot_id

        def worker():
            success, msg = self.controller.force_download_latest(save_name=save_name)
            self.finished_signal.emit(slot_id, "sync", success, msg)

        threading.Thread(target=worker, daemon=True).start()


class SaveTableWidget(QWidget):
    # Bubbles up to GameDetailPanel so it can disable Overview's own
    # controls (and every other row) while any one save action is
    # mid-flight -- every action here and on Overview shares one
    # _sync_lock on the controller, so only one can genuinely run at a
    # time regardless of where it was started from.
    busy_changed = Signal(bool)
    # slot_id, action ("sync"), success, message -- emitted from a
    # background thread, marshaled to the GUI thread by Qt.
    row_action_finished = Signal(str, str, bool, str)
    # save_name -- emitted when a row's Host button is confirmed.
    # GameDetailPanel handles it by slotting that save in as Overview's
    # active target, switching to it, and starting Overview's own Host
    # Now flow (see GameDetailPanel._start_hosting_save).
    host_requested = Signal(str)

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
        # Columns 1-4 are plain QTableWidgetItem text cells -- ResizeToContents
        # works reliably for those, sizing each to its actual displayed text.
        for col in (1, 2, 3, 4):
            self.table.horizontalHeader().setSectionResizeMode(col, QHeaderView.ResizeToContents)
        # Column 5 is NOT plain text -- it's a setCellWidget container (the
        # Host/Sync buttons), and ResizeToContents doesn't reliably size
        # itself off a container widget's real sizeHint (it collapsed to a
        # near-zero-width column with the button labels clipped away). A
        # fixed width sized for "Host"/"Sync" at this theme's default
        # button padding is simpler and reliable.
        self.table.horizontalHeader().setSectionResizeMode(5, QHeaderView.Fixed)
        self.table.setColumnWidth(5, 190)
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
            status_item = QTableWidgetItem(STATUS_LABELS.get(row.status, row.status))
            status_item.setToolTip(STATUS_TOOLTIPS.get(row.status, ""))
            self.table.setItem(i, 4, status_item)

            actions = _SaveRowActions(
                self.controller, row, self.row_action_finished, self.busy_changed.emit, self.host_requested.emit
            )
            actions.set_enabled(self._rows_enabled)
            self.table.setCellWidget(i, 5, actions)

    def _on_row_action_finished(self, slot_id: str, action: str, success: bool, msg: str) -> None:
        self.busy_changed.emit(False)
        self.refresh_local_scan()
        title = "Sync Save"
        if success:
            QMessageBox.information(self.window(), title, msg)
        else:
            QMessageBox.critical(self.window(), title, msg)
