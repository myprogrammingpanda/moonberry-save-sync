"""Light/dark color palettes shared by the main window and the settings
dialog, plus Windows system-theme detection. Built on ttk's "clam" base
theme specifically -- native themes like "vista" mostly ignore color
overrides and always render with whatever the OS gives them, so a real
dark mode needs a theme that actually respects custom colors."""

import sys
from tkinter import ttk

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
        "border": "#c9c9c9",
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
        "border": "#3c3c3c",
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


def set_titlebar_theme(root, dark: bool) -> None:
    """Colors the OS-drawn title bar to match (Windows draws this itself --
    ttk theming only reaches the window's content area). Uses the DWM
    "immersive dark mode" attribute (Windows 10 1809+ / Windows 11). Never
    fatal: silently does nothing on older Windows or any other OS."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        root.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
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


def apply_menu_colors(menu, palette: dict) -> None:
    """Colors a tk.Menu dropdown (e.g. the one a ttk.Menubutton opens).
    Note this is NOT what makes a real root-level menu bar follow dark
    mode -- Windows renders that natively and ignores color options on it
    entirely, which is why the app fakes its menu bar with ttk.Menubutton
    instead. This only handles the dropdown contents those buttons open."""
    try:
        menu.configure(
            background=palette["entry_bg"],
            foreground=palette["fg"],
            activebackground=palette["accent"],
            activeforeground="#ffffff",
            disabledforeground=palette["border"],
        )
    except Exception:
        pass


def resolve_theme(preference: str) -> str:
    """`preference` is config.json's "theme" key: "light", "dark", or
    "system" (also the default when the key is missing, for configs
    written before this feature existed)."""
    if preference in ("light", "dark"):
        return preference
    return "dark" if detect_system_dark() else "light"


def apply_palette(root, style: ttk.Style, theme_name: str) -> dict:
    """Configures ttk's "clam" theme with the given palette and sets the
    raw Tk root's own background to match. Existing ttk widgets pick up
    the new colors immediately since they're driven by style, not
    per-widget options -- only plain tk widgets (root, Text/Canvas) need
    their own .configure() calls, done by the caller. Returns the
    resolved palette dict for that."""
    palette = PALETTES[theme_name]
    style.theme_use("clam")
    root.configure(bg=palette["bg"])

    style.configure(".", background=palette["bg"], foreground=palette["fg"], bordercolor=palette["border"])
    style.configure("TFrame", background=palette["bg"])
    style.configure("TLabel", background=palette["bg"], foreground=palette["fg"])
    style.configure("TCheckbutton", background=palette["bg"], foreground=palette["fg"])
    style.configure("TLabelframe", background=palette["bg"], foreground=palette["fg"], bordercolor=palette["border"])
    style.configure("TLabelframe.Label", background=palette["bg"], foreground=palette["fg"])
    style.configure("TButton", background=palette["entry_bg"], foreground=palette["fg"], bordercolor=palette["border"])
    style.map(
        "TButton",
        background=[("active", palette["accent"])],
        foreground=[("active", "#ffffff")],
    )
    style.configure(
        "TEntry", fieldbackground=palette["entry_bg"], foreground=palette["entry_fg"], bordercolor=palette["border"]
    )
    style.configure(
        "TCombobox",
        fieldbackground=palette["entry_bg"],
        foreground=palette["entry_fg"],
        background=palette["entry_bg"],
    )
    style.map(
        "TCombobox",
        fieldbackground=[("readonly", palette["entry_bg"])],
        foreground=[("readonly", palette["entry_fg"])],
    )
    style.configure(
        "Vertical.TScrollbar", background=palette["bg"], troughcolor=palette["bg"], bordercolor=palette["border"]
    )

    # Used for the fake menu bar (ttk.Menubutton row) instead of a native
    # tk.Menu bar -- Windows renders an actual root-level menu bar itself
    # and ignores color options on it entirely, so a themed dropdown
    # button standing in for "File"/"View" is the only way to make the
    # always-visible bar itself follow dark mode.
    style.configure(
        "Menubar.TMenubutton",
        background=palette["bg"],
        foreground=palette["fg"],
        borderwidth=0,
        relief="flat",
        padding=(10, 4),
    )
    style.map(
        "Menubar.TMenubutton",
        background=[("active", palette["accent"])],
        foreground=[("active", "#ffffff")],
    )
    return palette
