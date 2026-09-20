"""Settings/setup form -- lets the config.json values be entered and edited
through a GUI instead of hand-editing JSON. Shown automatically on first run
(no config.json yet), and reachable afterward from the main window's
Settings button. Purely a form over config.json's shape; knows nothing about
any specific game beyond what GameAdapter.config_fields declares."""

import json
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from gui.theme import apply_palette, resolve_theme, set_titlebar_theme

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


def _apply_theme(widget, theme_pref: str):
    style = ttk.Style(widget)
    theme_name = resolve_theme(theme_pref)
    palette = apply_palette(widget, style, theme_name)
    return style, palette


class _SettingsForm:
    def __init__(self, win, adapters: dict, existing_cfg: dict):
        self.win = win
        self.adapters = adapters
        self.existing_cfg = existing_cfg
        self.entries: dict[str, tk.StringVar] = {}
        self.saved = False
        self._show_secrets = tk.BooleanVar(value=False)

        _style, palette = _apply_theme(win, existing_cfg.get("theme", "system"))
        set_titlebar_theme(win, resolve_theme(existing_cfg.get("theme", "system")) == "dark")
        win.title("Moonberry Save-Sync — Settings")
        win.minsize(520, 200)

        outer = ttk.Frame(win, padding=14)
        outer.pack(fill="both", expand=True)

        canvas = tk.Canvas(outer, borderwidth=0, highlightthickness=0, background=palette["bg"])
        scrollbar = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        self.scroll_frame = ttk.Frame(canvas)
        self.scroll_frame.bind(
            "<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
        )
        canvas.create_window((0, 0), window=self.scroll_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        game_ids = sorted(adapters)
        default_game = existing_cfg.get("active_game") if existing_cfg.get("active_game") in adapters else (game_ids[0] if game_ids else "")
        self.active_game_var = tk.StringVar(value=default_game)

        game_row = ttk.Frame(self.scroll_frame)
        game_row.pack(fill="x", pady=(0, 10))
        ttk.Label(game_row, text="Game", width=28).pack(side="left")
        game_combo = ttk.Combobox(
            game_row, textvariable=self.active_game_var, values=game_ids,
            state="readonly", width=30,
        )
        game_combo.pack(side="left", fill="x", expand=True)
        game_combo.bind("<<ComboboxSelected>>", lambda e: self._rebuild_game_section())

        self._section("Player", _TOP_LEVEL_FIELDS)
        self._section("Coordinator (Cloudflare Worker)", _COORDINATOR_FIELDS)
        self._section("Cloud Storage", _STORAGE_FIELDS)
        self._section("Discord Notifications (optional)", _MOONBERRY_FIELDS)
        self._section("Updates (optional)", _MISC_FIELDS)

        self.game_section = ttk.LabelFrame(self.scroll_frame, text="Game Settings", padding=10)
        self.game_section.pack(fill="x", pady=(0, 10))
        self._rebuild_game_section()

        advanced = ttk.LabelFrame(self.scroll_frame, text="Advanced", padding=10)
        advanced.pack(fill="x", pady=(0, 10))
        self._field(advanced, "poll_interval_seconds", "Poll interval (seconds)", False, False)
        self._field(advanced, "max_saved_versions", "Saved versions to keep", False, False)

        show_check = ttk.Checkbutton(
            self.scroll_frame, text="Show secret values", variable=self._show_secrets,
            command=self._toggle_secret_visibility,
        )
        show_check.pack(anchor="w", pady=(0, 10))

        button_row = ttk.Frame(win, padding=(14, 0, 14, 14))
        button_row.pack(fill="x")
        ttk.Button(button_row, text="Save", command=self._on_save).pack(side="right")
        ttk.Button(button_row, text="Cancel", command=self._on_cancel).pack(side="right", padx=(0, 8))

    def _section(self, title, fields):
        frame = ttk.LabelFrame(self.scroll_frame, text=title, padding=10)
        frame.pack(fill="x", pady=(0, 10))
        for key, label, required, secret in fields:
            self._field(frame, key, label, required, secret)

    def _field(self, parent, key, label, required, secret, kind="text"):
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=2)
        label_text = f"{label} *" if required else label
        ttk.Label(row, text=label_text, width=28, anchor="w").pack(side="left")

        var = tk.StringVar(value=str(self.existing_cfg.get(key, "")))
        self.entries[key] = var
        show_char = "*" if secret else ""
        entry = ttk.Entry(row, textvariable=var, show=show_char)
        entry.pack(side="left", fill="x", expand=True)
        if secret:
            entry.secret = True  # tag for the show/hide toggle

        if kind == "folder":
            ttk.Button(
                row, text="Browse...",
                command=lambda v=var: self._browse_folder(v),
            ).pack(side="left", padx=(6, 0))
        elif kind == "file":
            ttk.Button(
                row, text="Browse...",
                command=lambda v=var: self._browse_file(v),
            ).pack(side="left", padx=(6, 0))

    def _browse_folder(self, var):
        chosen = filedialog.askdirectory(initialdir=var.get() or None)
        if chosen:
            var.set(chosen)

    def _browse_file(self, var):
        chosen = filedialog.askopenfilename(initialdir=str(Path(var.get()).parent) if var.get() else None)
        if chosen:
            var.set(chosen)

    def _rebuild_game_section(self):
        for child in self.game_section.winfo_children():
            child.destroy()

        game_id = self.active_game_var.get()
        adapter_cls = self.adapters.get(game_id)
        if not adapter_cls:
            ttk.Label(self.game_section, text="No games found in games/.").pack(anchor="w")
            return

        existing_game_cfg = self.existing_cfg.get("games", {}).get(game_id, {})
        for key, label, kind in adapter_cls.config_fields:
            row = ttk.Frame(self.game_section)
            row.pack(fill="x", pady=2)
            ttk.Label(row, text=f"{label} *", width=28, anchor="w").pack(side="left")
            var = tk.StringVar(value=str(existing_game_cfg.get(key, "")))
            self.entries[f"games.{game_id}.{key}"] = var
            entry = ttk.Entry(row, textvariable=var)
            entry.pack(side="left", fill="x", expand=True)
            if kind == "folder":
                ttk.Button(row, text="Browse...", command=lambda v=var: self._browse_folder(v)).pack(side="left", padx=(6, 0))
            elif kind == "file":
                ttk.Button(row, text="Browse...", command=lambda v=var: self._browse_file(v)).pack(side="left", padx=(6, 0))

    def _toggle_secret_visibility(self):
        show = self._show_secrets.get()
        for row in self.scroll_frame.winfo_children():
            self._toggle_recursive(row, show)

    def _toggle_recursive(self, widget, show):
        for child in widget.winfo_children():
            if isinstance(child, ttk.Entry) and getattr(child, "secret", False):
                child.configure(show="" if show else "*")
            self._toggle_recursive(child, show)

    def _on_cancel(self):
        self.saved = False
        self.win.destroy()

    def _on_save(self):
        game_id = self.active_game_var.get()
        adapter_cls = self.adapters.get(game_id)
        if not adapter_cls:
            messagebox.showerror("Missing game", "No game selected, or no game modules were found.")
            return

        missing = []
        for key, label, required, _ in (
            _TOP_LEVEL_FIELDS + _COORDINATOR_FIELDS + _STORAGE_FIELDS
        ):
            if required and not self.entries[key].get().strip():
                missing.append(label)
        for key, label, _kind in adapter_cls.config_fields:
            if not self.entries[f"games.{game_id}.{key}"].get().strip():
                missing.append(label)

        if missing:
            messagebox.showerror(
                "Missing required fields",
                "Please fill in:\n- " + "\n- ".join(missing),
            )
            return

        def _int_or_default(key, default):
            raw = self.entries[key].get().strip()
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
            cfg[key] = self.entries[key].get().strip()
        cfg["poll_interval_seconds"] = _int_or_default("poll_interval_seconds", 30)
        cfg["max_saved_versions"] = _int_or_default("max_saved_versions", 5)

        games_cfg = dict(cfg.get("games", {}))
        game_section = dict(games_cfg.get(game_id, {}))
        for key, _label, _kind in adapter_cls.config_fields:
            game_section[key] = self.entries[f"games.{game_id}.{key}"].get().strip()
        games_cfg[game_id] = game_section
        cfg["games"] = games_cfg

        self.result_cfg = cfg
        self.saved = True
        self.win.destroy()


def run_setup_wizard(config_path: Path, adapters: dict, parent=None) -> bool:
    """Shows the settings form. Writes config_path and returns True if the
    user saved; returns False (leaving config_path untouched) if they
    closed the window instead."""
    existing_cfg = {}
    if config_path.exists():
        try:
            existing_cfg = json.loads(config_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            existing_cfg = {}

    owns_root = parent is None
    win = tk.Tk() if owns_root else tk.Toplevel(parent)

    form = _SettingsForm(win, adapters, existing_cfg)

    if owns_root:
        win.mainloop()
    else:
        win.transient(parent)
        win.grab_set()
        parent.wait_window(win)

    if form.saved:
        config_path.write_text(json.dumps(form.result_cfg, indent=2), encoding="utf-8")
    return form.saved
