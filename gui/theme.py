"""Light/dark palettes shared by the main window and the settings dialog,
plus Windows system-theme detection. Unlike the old Tkinter GUI -- which
needed three separate theming mechanisms because Tkinter has no unified
theming -- Qt widgets (including the menu bar) all respect a single
application-wide stylesheet, so this collapses to one QSS string plus the
same Windows DWM title-bar call (Qt's own theming still can't reach the
OS-drawn title bar)."""

import sys

PALETTES = {
    "light": {
        "bg": "#f4f4f4",
        "fg": "#1a1a1a",
        "entry_bg": "#ffffff",
        "entry_fg": "#1a1a1a",
        "log_bg": "#fbfbfb",
        "log_fg": "#1a1a1a",
        "warning": "#8a5a00",
        "error": "#b02a2a",
        "accent": "#0a66c2",
        "success": "#1f8a3d",
        "border": "#c9c9c9",
        "dim": "#6b6b6b",
    },
    "dark": {
        "bg": "#1e1e1e",
        "fg": "#e6e6e6",
        "entry_bg": "#2d2d2d",
        "entry_fg": "#e6e6e6",
        "log_bg": "#252526",
        "log_fg": "#d4d4d4",
        "warning": "#e0a951",
        "error": "#e06c6c",
        "accent": "#3a8ee6",
        "success": "#4caf50",
        "border": "#3c3c3c",
        "dim": "#9a9a9a",
    },
}


def detect_system_dark() -> bool:
    """Reads Windows' own light/dark app-theme setting. Never fatal --
    falls back to light (False) on any other OS or if the registry key
    isn't there (older Windows versions)."""
    if sys.platform != "win32":
        return False
    try:
        import winreg

        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        )
        value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
        return value == 0
    except OSError:
        return False


def resolve_theme(preference: str) -> str:
    """`preference` is config.json's "theme" key: "light", "dark", or
    "system" (also the default when the key is missing, for configs
    written before this feature existed)."""
    if preference in ("light", "dark"):
        return preference
    return "dark" if detect_system_dark() else "light"


def set_titlebar_theme(widget, dark: bool) -> None:
    """Colors the OS-drawn title bar of a top-level widget to match
    (Windows draws this itself -- Qt's stylesheet only reaches the window's
    content area). Uses the DWM "immersive dark mode" attribute (Windows 10
    1809+ / Windows 11). Never fatal: silently does nothing on older
    Windows or any other OS. Must be called after the widget has a native
    window handle (i.e. after it's shown, or after an explicit
    winId()/show())."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        hwnd = int(widget.winId())
        DWMWA_USE_IMMERSIVE_DARK_MODE = 20
        value = ctypes.c_int(1 if dark else 0)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE, ctypes.byref(value), ctypes.sizeof(value)
        )
        # DWM only repaints the frame on the next geometry/z-order change,
        # so nudge it to redraw immediately instead of waiting for one.
        SWP_NOMOVE, SWP_NOSIZE, SWP_NOZORDER, SWP_FRAMECHANGED = 0x2, 0x1, 0x4, 0x20
        ctypes.windll.user32.SetWindowPos(
            hwnd, 0, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_FRAMECHANGED
        )
    except Exception:
        pass


def build_stylesheet(palette: dict) -> str:
    p = palette
    return f"""
    QMainWindow, QDialog, QWidget {{
        background: {p['bg']};
        color: {p['fg']};
    }}
    QLabel {{
        background: transparent;
        color: {p['fg']};
    }}
    QLabel[role="dim"] {{
        color: {p['dim']};
    }}
    QLabel[role="warning"] {{
        color: {p['warning']};
    }}
    QLabel[role="status"] {{
        font-weight: 600;
        font-size: 12pt;
    }}
    QPushButton {{
        background: {p['entry_bg']};
        color: {p['fg']};
        border: 1px solid {p['border']};
        border-radius: 4px;
        padding: 6px 14px;
    }}
    QPushButton:hover:!disabled {{
        background: {p['accent']};
        color: #ffffff;
        border-color: {p['accent']};
    }}
    QPushButton:disabled {{
        color: {p['dim']};
    }}
    QPushButton[role="primary"] {{
        background: {p['accent']};
        color: #ffffff;
        border-color: {p['accent']};
        font-weight: 600;
        padding: 8px 20px;
    }}
    QPushButton[role="primary"]:hover:!disabled {{
        background: {p['accent']};
        border-color: {p['fg']};
    }}
    QPushButton[role="primary"]:disabled {{
        background: {p['border']};
        color: {p['dim']};
        border-color: {p['border']};
    }}
    QPushButton[role="danger"] {{
        background: {p['error']};
        color: #ffffff;
        border-color: {p['error']};
        font-weight: 600;
        padding: 8px 10px;
    }}
    QPushButton[role="danger"]:hover:!disabled {{
        background: {p['error']};
        border-color: {p['fg']};
    }}
    QPushButton[role="danger"]:disabled {{
        background: {p['border']};
        color: {p['dim']};
        border-color: {p['border']};
    }}
    QPushButton[role="success"] {{
        background: {p['success']};
        color: #ffffff;
        border-color: {p['success']};
        font-weight: 600;
        padding: 8px 10px;
    }}
    QPushButton[role="success"]:hover:!disabled {{
        background: {p['success']};
        border-color: {p['fg']};
    }}
    QPushButton[role="success"]:disabled {{
        background: {p['border']};
        color: {p['dim']};
        border-color: {p['border']};
    }}
    QLineEdit, QComboBox {{
        background: {p['entry_bg']};
        color: {p['entry_fg']};
        border: 1px solid {p['border']};
        border-radius: 3px;
        padding: 4px 6px;
    }}
    QLineEdit:disabled, QComboBox:disabled {{
        color: {p['dim']};
    }}
    QComboBox QAbstractItemView {{
        background: {p['entry_bg']};
        color: {p['entry_fg']};
        selection-background-color: {p['accent']};
        selection-color: #ffffff;
    }}
    QGroupBox {{
        border: 1px solid {p['border']};
        border-radius: 5px;
        margin-top: 12px;
        padding-top: 12px;
        font-weight: 600;
    }}
    QGroupBox::title {{
        subcontrol-origin: margin;
        left: 10px;
        padding: 0 4px;
    }}
    QMenuBar {{
        background: {p['bg']};
        color: {p['fg']};
        border-bottom: 1px solid {p['border']};
    }}
    QMenuBar::item:selected {{
        background: {p['accent']};
        color: #ffffff;
    }}
    QMenu {{
        background: {p['entry_bg']};
        color: {p['fg']};
        border: 1px solid {p['border']};
    }}
    QMenu::item:selected {{
        background: {p['accent']};
        color: #ffffff;
    }}
    QCheckBox {{
        background: transparent;
        color: {p['fg']};
    }}
    QScrollArea {{
        border: none;
        background: transparent;
    }}
    QListWidget {{
        background: {p['entry_bg']};
        color: {p['fg']};
        border: 1px solid {p['border']};
        border-radius: 4px;
        outline: none;
    }}
    QListWidget::item {{
        padding: 8px 10px;
        border-bottom: 1px solid {p['border']};
    }}
    QListWidget::item:selected {{
        background: {p['accent']};
        color: #ffffff;
    }}
    QScrollBar:vertical {{
        background: {p['bg']};
        width: 12px;
        border: none;
    }}
    QScrollBar::handle:vertical {{
        background: {p['border']};
        border-radius: 5px;
        min-height: 24px;
    }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
        height: 0px;
    }}
    QTextEdit[role="log"] {{
        background: {p['log_bg']};
        color: {p['log_fg']};
        border: 1px solid {p['border']};
        font-family: Consolas;
        font-size: 9pt;
    }}
    """


def apply_theme(app, theme_name: str) -> dict:
    """Applies the palette to the whole application via one stylesheet --
    every widget in every window (main window, settings dialog, message
    boxes) picks it up automatically, no per-widget color calls needed.
    Returns the resolved palette dict for callers that need raw colors
    (e.g. the custom-painted dark-mode toggle, or per-line log coloring)."""
    palette = PALETTES[theme_name]
    app.setStyleSheet(build_stylesheet(palette))
    return palette
