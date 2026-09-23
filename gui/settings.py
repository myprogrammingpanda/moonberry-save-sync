"""Settings/setup form -- lets config.json values be entered and edited
through a GUI instead of hand-editing JSON. Shown automatically on first run
(no config.json yet), and reachable afterward from the main window's File >
Settings. Purely a form over config.json's shape; knows nothing about any
specific game beyond what GameAdapter.config_fields/editions declare."""

import json
import sys
from pathlib import Path

from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from gui.theme import apply_theme, resolve_theme, set_titlebar_theme
from gui.widgets.game_config_form import GameConfigForm

# (key, label, required, secret)
_TOP_LEVEL_FIELDS = [
    ("player_name", "Your player name", True, False),
]

_COORDINATOR_FIELDS = [
    ("worker_url", "Coordinator URL", True, False),
    ("worker_secret", "Coordinator shared secret", True, True),
]

_STORAGE_FIELDS = [
    ("storage_endpoint_url", "Endpoint URL", True, False),
    ("storage_access_key_id", "Access key ID", True, True),
    ("storage_secret_access_key", "Secret access key", True, True),
    ("storage_bucket_name", "Bucket name", True, False),
]

_MOONBERRY_FIELDS = [
    ("moonberry_url", "Discord bot URL", False, False),
    ("moonberry_secret", "Discord bot shared secret", False, True),
]

_MISC_FIELDS = [
    ("github_repo", "GitHub repo (owner/name, for update checks)", False, False),
]

_ADVANCED_FIELDS = [
    ("poll_interval_seconds", "Poll interval (seconds)", 30),
    ("max_saved_versions", "Saved versions to keep", 5),
]


class SettingsDialog(QDialog):
    def __init__(self, config_path: Path, adapters: dict, parent=None):
        super().__init__(parent)
        self.config_path = config_path
        self.adapters = adapters
        self.saved = False
        self.entries: dict[str, QLineEdit] = {}
        self._secret_entries: list[QLineEdit] = []

        self.existing_cfg = {}
        if config_path.exists():
            try:
                self.existing_cfg = json.loads(config_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                self.existing_cfg = {}

        self.theme_name = resolve_theme(self.existing_cfg.get("theme", "system"))
        self.colors = apply_theme(QApplication.instance(), self.theme_name)

        self.setWindowTitle("Moonberry Save-Sync — Settings")
        self.setMinimumSize(560, 480)
        self.resize(560, 640)

        outer = QVBoxLayout(self)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        self.content_layout = QVBoxLayout(content)
        scroll.setWidget(content)
        outer.addWidget(scroll, stretch=1)

        game_ids = sorted(adapters)
        default_game = (
            self.existing_cfg.get("active_game")
            if self.existing_cfg.get("active_game") in adapters
            else (game_ids[0] if game_ids else "")
        )

        game_row = QFormLayout()
        self.game_combo = QComboBox()
        self.game_combo.addItems(game_ids)
        if default_game:
            self.game_combo.setCurrentText(default_game)
        self.game_combo.currentTextChanged.connect(self._rebuild_game_section)
        game_row.addRow("Game", self.game_combo)
        self.content_layout.addLayout(game_row)

        self._section("Player", _TOP_LEVEL_FIELDS)
        self._section("Coordinator (Cloudflare Worker)", _COORDINATOR_FIELDS)
        self._section("Cloud Storage", _STORAGE_FIELDS)
        self._section("Discord Notifications (optional)", _MOONBERRY_FIELDS)
        self._section("Updates (optional)", _MISC_FIELDS)

        self.game_group = QGroupBox("Game Settings")
        self.game_group_layout = QFormLayout(self.game_group)
        self.content_layout.addWidget(self.game_group)
        self._rebuild_game_section(default_game)

        advanced_group = QGroupBox("Advanced")
        advanced_layout = QFormLayout(advanced_group)
        for key, label, default in _ADVANCED_FIELDS:
            entry = QLineEdit(str(self.existing_cfg.get(key, default)))
            self.entries[key] = entry
            advanced_layout.addRow(label, entry)
        self.content_layout.addWidget(advanced_group)

        self.show_secrets_check = QCheckBox("Show secret values")
        self.show_secrets_check.toggled.connect(self._toggle_secret_visibility)
        self.content_layout.addWidget(self.show_secrets_check)

        self.content_layout.addStretch()

        button_row = QHBoxLayout()
        button_row.addStretch()
        cancel_button = QPushButton("Cancel")
        cancel_button.clicked.connect(self._on_cancel)
        button_row.addWidget(cancel_button)
        save_button = QPushButton("Save")
        save_button.setProperty("role", "primary")
        save_button.clicked.connect(self._on_save)
        button_row.addWidget(save_button)
        outer.addLayout(button_row)

    def showEvent(self, event):
        super().showEvent(event)
        set_titlebar_theme(self, self.theme_name == "dark")

    # -- section/field construction --

    def _section(self, title: str, fields: list) -> None:
        group = QGroupBox(title)
        form = QFormLayout(group)
        for key, label, required, secret in fields:
            self._add_field(form, key, label, required, secret)
        self.content_layout.addWidget(group)

    def _add_field(self, form: QFormLayout, key: str, label: str, required: bool, secret: bool, kind: str = "text"):
        label_text = f"{label} *" if required else label
        entry = QLineEdit(str(self.existing_cfg.get(key, "")))
        if secret:
            entry.setEchoMode(QLineEdit.Password)
            self._secret_entries.append(entry)
        self.entries[key] = entry

        if kind == "text":
            form.addRow(label_text, entry)
            return

        row = QWidget()
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.addWidget(entry)
        browse = QPushButton("Browse...")
        if kind == "folder":
            browse.clicked.connect(lambda: self._browse_folder(entry))
        else:
            browse.clicked.connect(lambda: self._browse_file(entry))
        row_layout.addWidget(browse)
        form.addRow(label_text, row)

    def _browse_folder(self, entry: QLineEdit):
        chosen = QFileDialog.getExistingDirectory(self, "Select folder", entry.text() or "")
        if chosen:
            entry.setText(chosen)

    def _browse_file(self, entry: QLineEdit):
        start_dir = str(Path(entry.text()).parent) if entry.text() else ""
        chosen, _filter = QFileDialog.getOpenFileName(self, "Select file", start_dir)
        if chosen:
            entry.setText(chosen)

    def _rebuild_game_section(self, game_id: str = ""):
        game_id = game_id or self.game_combo.currentText()
        while self.game_group_layout.rowCount():
            self.game_group_layout.removeRow(0)

        adapter_cls = self.adapters.get(game_id)
        if not adapter_cls:
            self.game_group_layout.addRow(QLabel("No games found in games/."))
            return

        existing_game_cfg = self.existing_cfg.get("games", {}).get(game_id, {})
        self.game_form = GameConfigForm(self, self.game_group_layout, adapter_cls, existing_game_cfg)

    def _toggle_secret_visibility(self, checked: bool):
        mode = QLineEdit.Normal if checked else QLineEdit.Password
        for entry in self._secret_entries:
            entry.setEchoMode(mode)

    # -- save/cancel --

    def _on_cancel(self):
        self.saved = False
        self.reject()

    def _on_save(self):
        game_id = self.game_combo.currentText()
        adapter_cls = self.adapters.get(game_id)
        if not adapter_cls:
            QMessageBox.critical(self, "Missing game", "No game selected, or no game modules were found.")
            return

        missing = []
        for key, label, required, _secret in _TOP_LEVEL_FIELDS + _COORDINATOR_FIELDS + _STORAGE_FIELDS:
            if required and not self.entries[key].text().strip():
                missing.append(label)
        missing += self.game_form.missing_labels()

        if missing:
            QMessageBox.critical(self, "Missing required fields", "Please fill in:\n- " + "\n- ".join(missing))
            return

        def _int_or_default(key, default):
            raw = self.entries[key].text().strip()
            if not raw:
                return default
            try:
                return int(raw)
            except ValueError:
                return default

        cfg = dict(self.existing_cfg)  # preserve any unknown/extra keys already present
        cfg["active_game"] = game_id
        for key, _label, _required, _secret in (
            _TOP_LEVEL_FIELDS + _COORDINATOR_FIELDS + _STORAGE_FIELDS + _MOONBERRY_FIELDS + _MISC_FIELDS
        ):
            cfg[key] = self.entries[key].text().strip()
        for key, _label, default in _ADVANCED_FIELDS:
            cfg[key] = _int_or_default(key, default)

        games_cfg = dict(cfg.get("games", {}))
        game_section = dict(games_cfg.get(game_id, {}))
        self.game_form.apply_to(game_section)
        games_cfg[game_id] = game_section
        cfg["games"] = games_cfg

        self.config_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        self.saved = True
        self.accept()


def run_setup_wizard(config_path: Path, adapters: dict, parent=None) -> bool:
    """Shows the settings form modally. Writes config_path and returns True
    if the user saved; returns False (leaving config_path untouched) if
    they closed the dialog instead."""
    if QApplication.instance() is None:
        QApplication(sys.argv)

    dialog = SettingsDialog(config_path, adapters, parent=parent)
    dialog.exec()
    return dialog.saved
