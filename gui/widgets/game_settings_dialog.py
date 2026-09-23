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
    QFormLayout,
    QHBoxLayout,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from gui.widgets.game_config_form import GameConfigForm


class GameSettingsDialog(QDialog):
    def __init__(self, config_path: Path, game_id: str, adapter_cls: type, parent=None):
        super().__init__(parent)
        self.config_path = config_path
        self.game_id = game_id
        self.adapter_cls = adapter_cls
        self.saved = False

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
        self.game_form = GameConfigForm(self, form, adapter_cls, existing_game_cfg)

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

    def _on_save(self):
        missing = self.game_form.missing_labels()
        if missing:
            QMessageBox.critical(self, "Missing required fields", "Please fill in:\n- " + "\n- ".join(missing))
            return

        cfg = dict(self.existing_cfg)  # preserve every unrelated key as-is
        games_cfg = dict(cfg.get("games", {}))
        game_section = dict(games_cfg.get(self.game_id, {}))
        self.game_form.apply_to(game_section)
        games_cfg[self.game_id] = game_section
        cfg["games"] = games_cfg

        self.config_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        self.saved = True
        self.accept()
