"""The "Hide to tray or Exit?" prompt shown when the main window's X is
pressed (unless a remembered choice in config.json's "close_action" skips
it -- see MainWindow.closeEvent)."""

from PySide6.QtWidgets import QCheckBox, QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout

from gui.theme import set_titlebar_theme

CLOSE_ACTIONS = ("ask", "tray", "exit")


class CloseChoiceDialog(QDialog):
    """exec() returns Accepted if a choice was made; read .choice ("tray"
    or "exit") and .remember afterwards. Cancel/Esc/the dialog's own X
    leave .choice as None."""

    def __init__(self, dark: bool, parent=None):
        super().__init__(parent)
        self._dark = dark
        self.choice: str | None = None
        self.setWindowTitle("Close Moonberry Save-Sync")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 14)
        layout.setSpacing(10)

        message = QLabel(
            "Hide to the system tray to keep watching for hosts and syncing in the background, "
            "or exit the app completely?"
        )
        message.setWordWrap(True)
        layout.addWidget(message)

        self.remember_check = QCheckBox("Remember my choice")
        layout.addWidget(self.remember_check)
        hint = QLabel("You can change this later in File > Settings.")
        hint.setProperty("role", "dim")
        layout.addWidget(hint)

        buttons = QHBoxLayout()
        buttons.addStretch()
        tray_button = QPushButton("Hide to tray")
        tray_button.setProperty("role", "primary")
        tray_button.setDefault(True)
        tray_button.clicked.connect(lambda: self._choose("tray"))
        buttons.addWidget(tray_button)
        exit_button = QPushButton("Exit")
        exit_button.clicked.connect(lambda: self._choose("exit"))
        buttons.addWidget(exit_button)
        cancel_button = QPushButton("Cancel")
        cancel_button.clicked.connect(self.reject)
        buttons.addWidget(cancel_button)
        layout.addLayout(buttons)

        self.setFixedWidth(420)

    @property
    def remember(self) -> bool:
        return self.remember_check.isChecked()

    def _choose(self, choice: str) -> None:
        self.choice = choice
        self.accept()

    def showEvent(self, event):
        super().showEvent(event)
        set_titlebar_theme(self, self._dark)
