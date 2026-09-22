"""One row of the Manual Sync panel: a single game's Force Upload / Force
Download buttons, wired to that game's own SessionController. Multiple rows
share nothing but the underlying Coordinator (owned by each controller) --
each row manages its own button state independently."""

import threading

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QMessageBox, QPushButton, QWidget


class GameSyncRow(QWidget):
    # Emitted (from a background thread, marshaled to the GUI thread by Qt)
    # when a force upload/download finishes, so MainWindow can show a
    # message box naming which game it was about.
    sync_finished = Signal(str, str, bool, str)  # game_id, action, success, message
    upload_precheck_done = Signal(str, str)  # verdict ("ok"/"redundant"/"stale"), message

    def __init__(self, game_id: str, display_name: str, controller, is_active: bool, parent=None):
        super().__init__(parent)
        self.game_id = game_id
        self.controller = controller

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 4)

        name_label = QLabel(f"{display_name}  ·  Active" if is_active else display_name)
        layout.addWidget(name_label)
        layout.addStretch()

        self.status_label = QLabel("")
        self.status_label.setProperty("role", "dim")
        layout.addWidget(self.status_label)

        self.upload_button = QPushButton("Force Upload")
        self.upload_button.clicked.connect(self._on_upload)
        layout.addWidget(self.upload_button)

        self.download_button = QPushButton("Force Download")
        self.download_button.clicked.connect(self._on_download)
        layout.addWidget(self.download_button)

        self.sync_finished.connect(self._on_finished)
        self.upload_precheck_done.connect(self._on_upload_precheck_done)

    def _on_upload(self):
        # Force Upload has no version awareness of its own -- it just
        # uploads whatever's on disk. Two checks run first, off the GUI
        # thread (redundancy is local-only/fast; freshness needs a
        # coordinator round-trip): redundant (nothing's changed since our
        # own last sync) skips the upload entirely -- no point creating a
        # duplicate and pruning a real older version to make room for it;
        # stale (local save is behind the cloud) asks for confirmation
        # instead of silently becoming the new "official" version for
        # everyone; otherwise it proceeds straight to uploading.
        self.upload_button.setEnabled(False)
        self.download_button.setEnabled(False)
        self.status_label.setText("Checking version...")

        def worker():
            is_redundant, redundant_msg = self.controller.check_upload_redundancy()
            if is_redundant:
                self.upload_precheck_done.emit("redundant", redundant_msg)
                return
            is_stale, stale_msg = self.controller.check_upload_freshness()
            if is_stale:
                self.upload_precheck_done.emit("stale", stale_msg)
            else:
                self.upload_precheck_done.emit("ok", "")

        threading.Thread(target=worker, daemon=True).start()

    def _on_upload_precheck_done(self, verdict: str, message: str):
        if verdict == "redundant":
            self.upload_button.setEnabled(True)
            self.download_button.setEnabled(True)
            self.status_label.setText("")
            QMessageBox.information(self.window(), "Force Upload Current Save", message)
            return
        if verdict == "stale":
            answer = QMessageBox.question(self.window(), "Force Upload Current Save", message)
            if answer != QMessageBox.Yes:
                self.upload_button.setEnabled(True)
                self.download_button.setEnabled(True)
                self.status_label.setText("")
                return
        self._run(self.controller.force_upload_current_save, "upload")

    def _on_download(self):
        answer = QMessageBox.question(
            self.window(),
            "Force Download Latest",
            "This will overwrite your current local save. A backup will be kept. Continue?",
        )
        if answer != QMessageBox.Yes:
            return
        self._run(self.controller.force_download_latest, "download")

    def _run(self, fn, action: str):
        self.upload_button.setEnabled(False)
        self.download_button.setEnabled(False)
        self.status_label.setText("Working...")

        def worker():
            success, msg = fn()
            self.sync_finished.emit(self.game_id, action, success, msg)

        threading.Thread(target=worker, daemon=True).start()

    def _on_finished(self, _game_id: str, _action: str, success: bool, msg: str):
        self.upload_button.setEnabled(True)
        self.download_button.setEnabled(True)
        self.status_label.setText("Done" if success else "Failed")
        if success:
            QMessageBox.information(self.window(), "Success", msg)
        else:
            QMessageBox.critical(self.window(), "Failed", msg)
