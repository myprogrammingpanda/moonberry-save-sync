"""The interface every game module implements. Nothing outside games/<id>.py
should need to know a specific game's save format, process name, launch
method, or log-scraping quirks -- that all lives behind these methods.

To add a new game: drop a games/<id>.py file with a GameAdapter subclass.
games/_discovery.py finds it automatically -- no other file needs to change.
"""

import re
import time
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path


def sanitize_key_component(value: str) -> str:
    """Makes a user-typed name (world name, server name) safe to use as
    part of an S3 object key prefix -- collapses anything that isn't
    alphanumeric/underscore/hyphen into a single underscore and strips
    leading/trailing underscores, so save_key_prefix implementations can
    build a prefix from config values without worrying about spaces or
    other characters that are awkward (if not strictly invalid) in a
    bucket key."""
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "_", value.strip())
    return cleaned.strip("_") or "save"


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

    @property
    @abstractmethod
    def default_save_name(self) -> str:
        """The currently-configured save name (world/server name) for this
        game -- the one every save_name=None call below falls back to, so
        every pre-multi-save call site (Overview tab's Host Now/Force
        Upload/Force Download) keeps operating on exactly the save it
        always has, unchanged."""
        ...

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
    def has_local_save(self, save_name: str | None = None) -> bool:
        """Whether the given save (default_save_name if not given) actually
        exists on disk yet, given its current config. Used to decide which
        configured games are "supported" for multi-game manual sync -- a
        game the user has configured but never played on this machine
        shouldn't get a Force Upload/Download row."""
        ...

    @abstractmethod
    def backup_local_save(self, backup_dir: Path, save_name: str | None = None) -> None: ...

    @abstractmethod
    def zip_save(self, dest_zip: Path, save_name: str | None = None) -> None: ...

    @abstractmethod
    def unzip_save(self, src_zip: Path) -> None:
        """Extracts a zip built by zip_save straight into this game's saves
        folder. No save_name parameter needed: zip_save always embeds the
        real on-disk save name as the archive's own internal relative
        path, so extraction reconstructs the right save/subfolder
        regardless of which save is currently configured as default."""
        ...

    @abstractmethod
    def list_local_saves(self) -> list[str]:
        """Every save name found on disk for this game right now,
        independent of what's configured as the default -- drives the
        Saves tab's local-save discovery."""
        ...

    @abstractmethod
    def save_stat(self, save_name: str) -> tuple[datetime | None, int | None]:
        """(latest modified time, total size in bytes) across the given
        save's on-disk data, or (None, None) if it doesn't exist -- drives
        the Saves tab's Modified/Size columns."""
        ...

    # -- optional: not every game exposes a join-code-style concept --

    def scrape_join_code(self) -> str | None:
        return None

    def content_hash(self, save_name: str | None = None) -> str | None:
        """A hash of the actual save DATA on disk right now, independent
        of file timestamps or which on-disk format is in use -- lets
        Force Upload detect "nothing's actually changed since I last
        synced" and skip creating a pointless duplicate version. A
        save_key comparison alone can't tell this: save_key only tracks
        which cloud version this machine last saw, not whether the local
        content has since diverged from it, and two zips of an unchanged
        save folder can differ byte-for-byte purely from embedded mtimes.
        Optional -- returns None (meaning "can't tell, don't skip") by
        default; only worth implementing if hashing the save format is
        cheap and unambiguous."""
        return None

    # -- storage namespacing --

    def slot_id_for(self, save_name: str) -> str:
        """The stable, filesystem/bucket-key/coordinator-map-safe id for a
        given save name -- the same sanitized form save_key_prefix_for
        already embeds, exposed on its own so callers (SessionController,
        the Saves tab) can use it as a dict key without re-deriving it."""
        return sanitize_key_component(save_name)

    def save_key_prefix_for(self, save_name: str) -> str:
        return f"{self.game_id}_{self.slot_id_for(save_name)}_"

    @property
    def save_key_prefix(self) -> str:
        return self.save_key_prefix_for(self.default_save_name)
