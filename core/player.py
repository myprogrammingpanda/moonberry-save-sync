"""Resolves this player's display name without requiring everyone to
hand-edit config.json. Priority:
  1. A previously-saved name in player_name.txt (next to this script/exe)
  2. A real (non-placeholder) name already set in config.json
  3. Ask once via a small popup, then remember it for next time
This lets one person build/share a single config.json + exe with everyone,
and each friend just gets asked their name the first time."""

from pathlib import Path


def get_player_name(app_dir: Path, cfg: dict) -> str:
    name_file = app_dir / "player_name.txt"
    if name_file.exists():
        saved = name_file.read_text(encoding="utf-8").strip()
        if saved:
            return saved

    configured = str(cfg.get("player_name", "")).strip()
    if configured and configured.lower() not in ("", "yournamehere", "change_me"):
        return configured

    import tkinter as tk
    from tkinter import simpledialog

    root = tk.Tk()
    root.withdraw()
    name = simpledialog.askstring(
        "Moonberry Save-Sync", "What's your name/handle? (shown to friends)"
    )
    root.destroy()

    name = (name or "Player").strip() or "Player"
    name_file.write_text(name, encoding="utf-8")
    return name
