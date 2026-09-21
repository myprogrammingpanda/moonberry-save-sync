"""A small on/off switch widget, since Qt has no built-in pill-style toggle.
Custom-painted (QPainter) rather than a styled QCheckBox, since QSS can't
draw a sliding thumb -- this is a direct Qt port of the old Tkinter
Canvas-based version."""

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QWidget

_WIDTH = 42
_HEIGHT = 22
_PADDING = 2


class ToggleSwitch(QWidget):
    toggled = Signal(bool)

    def __init__(self, initial: bool, palette: dict, parent=None):
        super().__init__(parent)
        self._checked = initial
        self._palette = palette
        self.setFixedSize(_WIDTH, _HEIGHT)
        self.setCursor(Qt.PointingHandCursor)

    def is_checked(self) -> bool:
        return self._checked

    def set_palette(self, palette: dict) -> None:
        self._palette = palette
        self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._checked = not self._checked
            self.update()
            self.toggled.emit(self._checked)

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)

        track_color = QColor(self._palette["accent"] if self._checked else self._palette["border"])
        painter.setBrush(track_color)
        painter.drawRoundedRect(QRectF(0, 0, _WIDTH, _HEIGHT), _HEIGHT / 2, _HEIGHT / 2)

        thumb_d = _HEIGHT - 2 * _PADDING
        thumb_x = (_WIDTH - _HEIGHT + _PADDING) if self._checked else _PADDING
        painter.setBrush(QColor("#ffffff"))
        painter.drawEllipse(QRectF(thumb_x, _PADDING, thumb_d, thumb_d))
