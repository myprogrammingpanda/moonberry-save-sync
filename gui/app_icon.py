"""The app's icon (window + system tray), drawn in code so there's no image
file to ship or lose -- QSystemTrayIcon won't show at all without one, and
until now the window just had Qt's generic default. A crescent moon with a
berry, on a round badge that reads in both light and dark taskbars."""

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap

_BADGE = QColor("#3b2a6b")
_MOON = QColor("#f3e6a8")
_BERRY = QColor("#c2185b")
_BERRY_SHINE = QColor("#f48fb1")
_LEAF = QColor("#66bb6a")


def _render(size: int) -> QPixmap:
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    s = size / 64.0  # everything below is laid out on a 64x64 grid

    p.setPen(Qt.NoPen)
    p.setBrush(QBrush(_BADGE))
    p.drawEllipse(QRectF(1 * s, 1 * s, 62 * s, 62 * s))

    moon = QPainterPath()
    moon.addEllipse(QRectF(10 * s, 9 * s, 40 * s, 40 * s))
    bite = QPainterPath()
    bite.addEllipse(QRectF(22 * s, 3 * s, 38 * s, 38 * s))
    p.setBrush(QBrush(_MOON))
    p.drawPath(moon.subtracted(bite))

    p.setPen(QPen(_LEAF, max(1.0, 3 * s), Qt.SolidLine, Qt.RoundCap))
    p.drawLine(QPointF(43 * s, 36 * s), QPointF(47 * s, 30 * s))
    p.setPen(Qt.NoPen)
    p.setBrush(QBrush(_BERRY))
    p.drawEllipse(QRectF(33 * s, 36 * s, 20 * s, 20 * s))
    p.setBrush(QBrush(_BERRY_SHINE))
    p.drawEllipse(QRectF(38 * s, 40 * s, 5 * s, 5 * s))

    p.end()
    return pm


def make_app_icon() -> QIcon:
    icon = QIcon()
    for size in (16, 20, 24, 32, 40, 48, 64, 128, 256):
        icon.addPixmap(_render(size))
    return icon
