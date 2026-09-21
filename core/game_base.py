"""The interface every game module implements. Nothing outside games/<id>.py
should need to know a specific game's save format, process name, launch
method, or log-scraping quirks -- that all lives behind these methods.

To add a new game: drop a games/<id>.py file with a GameAdapter subclass.
games/_discovery.py finds it automatically -- no other file needs to change.
"""

import time
from abc import ABC, abstractmethod
from pathlib import Path


class GameAdapter(ABC):
    game_id: str        # stable short id, e.g. "valheim" -- used for
                         # storage key prefixes and local version filenames
    display_name: str   # e.g. "Valheim" -- shown in the GUI

    # Describes this game's own config.json sub-section (config["games"][game_id])
    # so the settings UI can render a form for it generically, without knowing
    # anything game-specific. Each entry is (key, label, kind), kind one of
    # "text", "folder", "file" (the latter two get a Browse... button).
    config_fields: list[tuple[str, str, str]] = []

    def __init__(self, game_config: dict):
        """`game_config` is this game's own sub-section of config.json,
        i.e. config["games"][self.game_id]."""
        self.cfg = game_config

    # -- process control --

    @abstractmethod
    def is_running(self) -> bool: ...

    @abstractmethod
    def launch(self) -> None: ...

    def wait_for_start(self, timeout: float = 60) -> bool:
        start = time.time()
        while time.time() - start < timeout:
            if self.is_running():
                return True
            time.sleep(2)
        return False

    def wait_for_exit(self, poll_seconds: float = 5) -> None:
        while self.is_running():
            time.sleep(poll_seconds)

    # -- save handling (the on-disk format is entirely the adapter's
    # business -- core/ only ever deals in zip files) --

    @abstractmethod
    def has_local_save(self) -> bool:
        """Whether this game's save actually exists on disk yet, given its
        current config. Used to decide which configured games are
        "supported" for multi-game manual sync -- a game the user has
        configured but never played on this machine shouldn't get a
        Force Upload/Download row."""
        ...

    @abstractmethod
    def backup_local_save(self, backup_dir: Path) -> None: ...

    @abstractmethod
    def zip_save(self, dest_zip: Path) -> None: ...

    @abstractmethod
    def unzip_save(self, src_zip: Path) -> None: ...

    # -- optional: not every game exposes a join-code-style concept --

    def scrape_join_code(self) -> str | None:
        return None

    # -- storage namespacing --

    @property
    def save_key_prefix(self) -> str:
        return f"{self.game_id}_save_"
