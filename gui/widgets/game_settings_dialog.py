"""Quick per-game settings dialog -- edits just one game's own config_fields
(world name, save folder, etc) from its own detail panel, instead of going
through the global File > Settings dialog and picking it out of a dropdown.
Writes the same config.json["games"][game_id] section that dialog writes,
leaving every other key (player name, coordinator, storage, other games'
sections) untouched."""

import json
from pathlib import Path

from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


class GameSettingsDialog(QDialog):
    def __init__(self, config_path: Path, game_id: str, adapter_cls: type, parent=None):
        super().__init__(parent)
        self.config_path = config_path
        self.game_id = game_id
        self.adapter_cls = adapter_cls
        self.saved = False
        self.entries: dict[str, QLineEdit] = {}

        try:
            self.existing_cfg = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self.existing_cfg = {}

        self.setWindowTitle(f"{adapter_cls.display_name} — Settings")
        self.setMinimumWidth(440)

        layout = QVBoxLayout(self)
        form = QFormLayout()
        layout.addLayout(form)

        existing_game_cfg = self.existing_cfg.get("games", {}).get(game_id, {})
        for key, label, kind in adapter_cls.config_fields:
            entry = QLineEdit(str(existing_game_cfg.get(key, "")))
            self.entries[key] = entry

            if kind == "text":
                form.addRow(f"{label} *", entry)
                continue

            row = QWidget()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.addWidget(entry)
            browse = QPushButton("Browse...")
            if kind == "folder":
                browse.clicked.connect(lambda _checked=False, e=entry: self._browse_folder(e))
            else:
                browse.clicked.connect(lambda _checked=False, e=entry: self._browse_file(e))
            row_layout.addWidget(browse)
            form.addRow(f"{label} *", row)

        button_row = QHBoxLayout()
        button_row.addStretch()
        cancel_button = QPushButton("Cancel")
        cancel_button.clicked.connect(self.reject)
        button_row.addWidget(cancel_button)
        save_button = QPushButton("Save")
        save_button.setProperty("role", "primary")
        save_button.clicked.connect(self._on_save)
        button_row.addWidget(save_button)
        layout.addLayout(button_row)

    def _browse_folder(self, entry: QLineEdit):
        chosen = QFileDialog.getExistingDirectory(self, "Select folder", entry.text() or "")
        if chosen:
            entry.setText(chosen)

    def _browse_file(self, entry: QLineEdit):
        start_dir = str(Path(entry.text()).parent) if entry.text() else ""
        chosen, _filter = QFileDialog.getOpenFileName(self, "Select file", start_dir)
        if chosen:
            entry.setText(chosen)

    def _on_save(self):
        missing = [label for key, label, _kind in self.adapter_cls.config_fields if not self.entries[key].text().strip()]
        if missing:
            QMessageBox.critical(self, "Missing required fields", "Please fill in:\n- " + "\n- ".join(missing))
            return

        cfg = dict(self.existing_cfg)  # preserve every unrelated key as-is
        games_cfg = dict(cfg.get("games", {}))
        game_section = dict(games_cfg.get(self.game_id, {}))
        for key, _label, _kind in self.adapter_cls.config_fields:
            game_section[key] = self.entries[key].text().strip()
        games_cfg[self.game_id] = game_section
        cfg["games"] = games_cfg

        self.config_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        self.saved = True
        self.accept()
