"""A "show yourself" channel between a second launch and the already-running
instance. main.py's named mutex only tells a second launch that another
copy is running; finding that copy's window by title (EnumWindows) can't
restore one that's hidden to the system tray, and poking a Qt window's
visibility from outside with raw ShowWindow calls leaves Qt's own idea of
it out of sync anyway. So the running instance listens on a QLocalServer
(a named pipe on Windows) and shows itself through Qt when anything
connects."""

import sys

from PySide6.QtNetwork import QLocalServer, QLocalSocket

SERVER_NAME = "MoonberrySaveSync_ShowWindow"


def start_show_server(on_show, parent=None) -> QLocalServer | None:
    """Starts listening in the running instance; on_show() is called on
    the GUI thread for every incoming connection. Returns None (and the
    second-launch fallback in main.py takes over) if it can't listen."""
    server = QLocalServer(parent)
    if not server.listen(SERVER_NAME):
        QLocalServer.removeServer(SERVER_NAME)  # stale socket file (non-Windows) -- retry once
        if not server.listen(SERVER_NAME):
            return None

    def _on_new_connection():
        while server.hasPendingConnections():
            sock = server.nextPendingConnection()
            sock.disconnected.connect(sock.deleteLater)
            sock.disconnectFromServer()
        on_show()

    server.newConnection.connect(_on_new_connection)
    return server


def signal_running_instance(timeout_ms: int = 1500) -> bool:
    """Called by a second launch. True if the running instance got the
    message. Also grants any process the right to take the foreground
    first, so the running instance's activateWindow() actually brings it
    to the front instead of just flashing its taskbar button (Windows only
    lets the process that received the latest user input -- i.e. this
    one, just launched -- steal focus)."""
    if sys.platform == "win32":
        try:
            import ctypes

            ASFW_ANY = -1
            ctypes.windll.user32.AllowSetForegroundWindow(ASFW_ANY)
        except Exception:
            pass

    sock = QLocalSocket()
    sock.connectToServer(SERVER_NAME)
    if not sock.waitForConnected(timeout_ms):
        return False
    sock.write(b"show\n")
    sock.waitForBytesWritten(timeout_ms)
    sock.disconnectFromServer()
    return True
