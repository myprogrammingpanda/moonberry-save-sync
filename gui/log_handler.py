"""Bridges Python's logging module into the Qt GUI thread. Emits a Qt
signal per log record instead of the Tkinter version's manual
queue-plus-polling loop -- Qt automatically marshals a signal emitted from a
background thread onto the thread the receiving QObject lives on, as long
as that thread has a running event loop (true here, since the receiver is a
widget owned by the main/GUI thread)."""

import logging

from PySide6.QtCore import QObject, Signal


class QtLogHandler(QObject, logging.Handler):
    log_line = Signal(str, str)  # formatted line, level name

    def __init__(self):
        QObject.__init__(self)
        logging.Handler.__init__(self)

    def emit(self, record):
        self.log_line.emit(self.format(record), record.levelname)
