"""The per-game half of both settings dialogs (File > Settings and a game's
own quick-settings dialog): an Edition dropdown when the game declares
editions, then one row per GameAdapter.config_fields entry. Built from
what the adapter declares alone -- knows nothing about any specific game."""

from pathlib import Path

from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QWidget,
)

from core.game_base import EDITION_KEY


class GameConfigForm:
    def __init__(self, parent: QWidget, form: QFormLayout, adapter_cls: type, existing_game_cfg: dict):
        self.parent = parent
        self.adapter_cls = adapter_cls
        self.entries: dict[str, QLineEdit] = {}
        self.edition_combo: QComboBox | None = None
        self._hint_label: QLabel | None = None
        self._current_edition = adapter_cls.edition_for(existing_game_cfg)

        if adapter_cls.editions:
            self._build_edition_row(form)

        preset = self._defaults(self._current_edition)
        for key, label, kind in adapter_cls.config_fields:
            # Only fill in an edition default where nothing is saved yet
            # (first run) -- an existing value is always shown as-is.
            entry = QLineEdit(str(existing_game_cfg.get(key) or preset.get(key, "")))
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

    # -- edition --

    def _build_edition_row(self, form: QFormLayout):
        self.edition_combo = QComboBox()
        for edition_id, edition in self.adapter_cls.editions.items():
            self.edition_combo.addItem(edition.label, edition_id)
        self.edition_combo.setCurrentIndex(self.edition_combo.findData(self._current_edition))
        self.edition_combo.currentIndexChanged.connect(self._on_edition_changed)

        detect = QPushButton("Detect")
        detect.setToolTip("Check which editions of this game are installed on this PC")
        detect.clicked.connect(self._on_detect)

        row = QWidget()
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.addWidget(self.edition_combo, stretch=1)
        row_layout.addWidget(detect)
        form.addRow("Edition", row)

        self._hint_label = QLabel()
        self._hint_label.setProperty("role", "dim")
        self._hint_label.setWordWrap(True)
        form.addRow("", self._hint_label)
        self._update_hint()

    def _defaults(self, edition_id: str | None) -> dict[str, str]:
        edition = self.adapter_cls.editions.get(edition_id) if edition_id else None
        return edition.defaults if edition else {}

    def _update_hint(self):
        if self._hint_label is None:
            return
        hint = self.adapter_cls.editions[self._current_edition].hint
        self._hint_label.setText(hint)
        self._hint_label.setVisible(bool(hint))

    def _on_edition_changed(self, _index: int):
        new_edition = self.edition_combo.currentData()
        old_defaults = self._defaults(self._current_edition)
        # A field whose preset differs between editions (a launch URI) is
        # inherently edition-specific, so it's always swapped -- the old
        # value can't be right for the new edition, even if it's a
        # hand-typed variant of the old preset (steam://run/ vs
        # steam://rungameid/). A field every edition shares a preset for
        # (a save folder) is only swapped while empty or still untouched,
        # so a customised one is kept.
        all_defaults = [e.defaults for e in self.adapter_cls.editions.values()]
        for key, value in self._defaults(new_edition).items():
            entry = self.entries.get(key)
            if entry is None:
                continue
            edition_specific = len({d.get(key) for d in all_defaults}) > 1
            current = entry.text().strip()
            if edition_specific or not current or current == old_defaults.get(key):
                entry.setText(value)
        self._current_edition = new_edition
        self._update_hint()

    def _on_detect(self):
        editions = self.adapter_cls.editions
        found = self.adapter_cls.detect_editions()
        name = self.adapter_cls.display_name
        if not found:
            QMessageBox.information(
                self.parent, "Detect edition",
                f"Couldn't find {name} installed from any supported store on this PC. "
                "Pick the edition you play manually.",
            )
            return
        if self._current_edition not in found:
            self.edition_combo.setCurrentIndex(self.edition_combo.findData(found[0]))
        labels = ", ".join(editions[e].label for e in found)
        if len(found) == 1:
            QMessageBox.information(self.parent, "Detect edition", f"Found {name} installed via {labels}.")
        else:
            QMessageBox.information(
                self.parent, "Detect edition",
                f"Found {name} installed via more than one store: {labels}.\n\n"
                "Make sure the edition selected is the one you actually play.",
            )

    # -- browse --

    def _browse_folder(self, entry: QLineEdit):
        chosen = QFileDialog.getExistingDirectory(self.parent, "Select folder", entry.text() or "")
        if chosen:
            entry.setText(chosen)

    def _browse_file(self, entry: QLineEdit):
        start_dir = str(Path(entry.text()).parent) if entry.text() else ""
        chosen, _filter = QFileDialog.getOpenFileName(self.parent, "Select file", start_dir)
        if chosen:
            entry.setText(chosen)

    # -- results --

    def missing_labels(self) -> list[str]:
        return [label for key, label, _kind in self.adapter_cls.config_fields if not self.entries[key].text().strip()]

    def apply_to(self, game_section: dict) -> None:
        """Writes this form's values into a copy of config["games"][game_id],
        leaving any other keys in it untouched."""
        for key, _label, _kind in self.adapter_cls.config_fields:
            game_section[key] = self.entries[key].text().strip()
        if self.edition_combo is not None:
            game_section[EDITION_KEY] = self.edition_combo.currentData()
