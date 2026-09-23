"""Valheim-specific logic: save format detection, join-code parsing, process
detection, and launching. Everything here is ported as-is from the original
tested app -- only the packaging into a GameAdapter is new."""

import hashlib
import logging
import os
import re
import shutil
import time
import zipfile
from datetime import datetime
from pathlib import Path

import psutil

from core.game_base import GameAdapter

log = logging.getLogger("moonberry-sync")

JOIN_CODE_PATTERN = re.compile(r"with join code (\w{4,8}) is active", re.IGNORECASE)
# NOTE: Valheim generates a transitional "registered with join code X" line
# immediately followed by a DIFFERENT, actually-active code (the one shown
# in the pause menu). Matching on "is active" specifically avoids grabbing
# the wrong (superseded) code -- confirmed as a real mismatch with a more
# permissive pattern.


class ValheimAdapter(GameAdapter):
    game_id = "valheim"
    display_name = "Valheim"

    config_fields = [
        ("valheim_worlds_folder", "Worlds folder", "folder"),
        ("valheim_world_name", "World name", "text"),
        ("valheim_log_path", "Player.log path", "file"),
        ("valheim_launch_uri", "Launch URI (Steam)", "text"),
    ]

    @property
    def default_save_name(self) -> str:
        return self.cfg["valheim_world_name"]

    # save_key_prefix (gamename_worldname, e.g. "valheim_MyWorld_") is
    # inherited from GameAdapter.save_key_prefix, built from
    # default_save_name above -- existing saves under the old
    # "world_save_" prefix were migrated forward to this scheme via
    # scripts/migrate_save_key_prefix.py rather than left orphaned; see
    # that script if this ever needs to happen again (e.g. after renaming
    # the world).

    # -- process control --

    def is_running(self) -> bool:
        for proc in psutil.process_iter(["name"]):
            if proc.info["name"] and proc.info["name"].lower() == "valheim.exe":
                return True
        return False

    def launch(self) -> None:
        log.info("Launching Valheim...")
        os.startfile(self.cfg["valheim_launch_uri"])  # noqa: S606 (Windows-only, intentional)

    # -- save format detection --

    def _get_world_target(self, save_name: str | None = None):
        """
        Valheim 1.0 changed the world save format entirely: pre-1.0, a world
        was two flat files (name.db + name.fwl). As of 1.0, it's a whole
        FOLDER named after the world, containing chunked data files,
        metadata, and integrity markers.

        This auto-detects which format is actually present on this machine,
        so sync keeps working correctly regardless of whether someone has
        updated to 1.0 or not.

        Returns ("folder", Path) for the 1.0+ format, or
                ("files", [Path, Path]) for the legacy pre-1.0 format.
        """
        folder = Path(self.cfg["valheim_worlds_folder"])
        name = save_name or self.default_save_name

        new_format_dir = folder / name
        if new_format_dir.is_dir():
            return "folder", new_format_dir

        return "files", [folder / f"{name}.db", folder / f"{name}.fwl"]

    def has_local_save(self, save_name: str | None = None) -> bool:
        kind, target = self._get_world_target(save_name)
        if kind == "folder":
            return target.is_dir()
        return all(f.exists() for f in target)

    # Valheim's own automatic/manual backup feature creates sibling
    # folders in worlds_local named "<world>_backup_YYYYMMDD-HHMMSS" or
    # "<world>_backup_auto-YYYYMMDD-HHMMSS" -- confirmed present on a real
    # worlds_local folder during testing. These are Valheim's own
    # disposable snapshots, not a real selectable save (same category of
    # trap as Zomboid's "<name>_player" client-side cache sibling), so
    # list_local_saves must exclude them rather than surfacing "saves"
    # that are really just backup copies of another entry in the list.
    _BACKUP_SUFFIX_PATTERN = re.compile(r"_backup_(auto-)?\d{8}-\d{6}$")

    def list_local_saves(self) -> list[str]:
        """Every world name found in valheim_worlds_folder, whichever
        format (1.0+ folder or legacy flat-file pair) it's actually stored
        in, excluding Valheim's own backup-snapshot siblings."""
        folder = Path(self.cfg["valheim_worlds_folder"])
        if not folder.is_dir():
            return []
        names = set()
        for entry in folder.iterdir():
            if self._BACKUP_SUFFIX_PATTERN.search(entry.stem if entry.is_file() else entry.name):
                continue
            if entry.is_dir():
                names.add(entry.name)  # 1.0+ folder format
            elif entry.suffix == ".fwl":
                names.add(entry.stem)  # legacy pre-1.0 format
        return sorted(names)

    def save_stat(self, save_name: str) -> tuple[datetime | None, int | None]:
        kind, target = self._get_world_target(save_name)
        if kind == "folder":
            files = [f for f in target.rglob("*") if f.is_file()] if target.is_dir() else []
        else:
            files = [f for f in target if f.exists()]
        if not files:
            return None, None
        stats = [f.stat() for f in files]
        return datetime.fromtimestamp(max(s.st_mtime for s in stats)), sum(s.st_size for s in stats)

    def content_hash(self, save_name: str | None = None) -> str | None:
        """Hashes actual file bytes, not mtimes, in a stable sorted order
        -- so it matches regardless of when the save was last touched on
        disk, and regardless of which zip_save() run produced a given
        upload (zip_save embeds per-file timestamps, so re-zipping an
        UNCHANGED save folder still produces byte-different zips; this
        looks past that to the real data)."""
        kind, target = self._get_world_target(save_name)
        hasher = hashlib.sha256()

        if kind == "folder":
            if not target.is_dir():
                return None
            files = sorted(f for f in target.rglob("*") if f.is_file())
            if not files:
                return None
            for f in files:
                hasher.update(str(f.relative_to(target)).encode("utf-8"))
                hasher.update(f.read_bytes())
        else:
            existing = sorted((f for f in target if f.exists()), key=lambda p: p.name)
            if not existing:
                return None
            for f in existing:
                hasher.update(f.name.encode("utf-8"))
                hasher.update(f.read_bytes())

        return hasher.hexdigest()

    def backup_local_save(self, backup_dir: Path, save_name: str | None = None) -> None:
        """Keep a timestamped copy of the current local save before
        overwriting, just in case something goes wrong with a cloud sync.
        Handles both the 1.0+ folder format and the legacy flat-file
        format."""
        backup_dir.mkdir(exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        kind, target = self._get_world_target(save_name)
        if kind == "folder":
            if target.exists():
                shutil.copytree(target, backup_dir / f"{target.name}_{stamp}")
        else:
            for f in target:
                if f.exists():
                    shutil.copy2(f, backup_dir / f"{f.stem}_{stamp}{f.suffix}")
        log.info("Backed up local save (if present) to %s", backup_dir)

    def zip_save(self, dest_zip: Path, save_name: str | None = None) -> None:
        """
        Zips whatever format is actually present. For the 1.0+ folder
        format, the world name is preserved as a path prefix inside the zip
        (e.g. "world/_main.1.db2") so that extracting it back into
        worlds_local correctly recreates the subfolder -- not just dumps
        loose chunk files into worlds_local directly.
        """
        kind, target = self._get_world_target(save_name)
        with zipfile.ZipFile(dest_zip, "w", zipfile.ZIP_DEFLATED) as zf:
            if kind == "folder":
                for f in target.rglob("*"):
                    if f.is_file():
                        arcname = str(Path(target.name) / f.relative_to(target))
                        zf.write(f, arcname=arcname)
            else:
                for f in target:
                    if f.exists():
                        zf.write(f, arcname=f.name)

    def unzip_save(self, src_zip: Path) -> None:
        """
        Extracts into worlds_local. Works correctly for both formats
        without needing to know which one is inside: zip_save() above
        always stores the right relative paths (either "world/..." for the
        folder format, or flat filenames for the legacy format), so a plain
        extractall() reconstructs whichever structure was actually zipped.
        """
        folder = Path(self.cfg["valheim_worlds_folder"])
        folder.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(src_zip, "r") as zf:
            zf.extractall(folder)

    # -- join code --

    def scrape_join_code(self) -> str | None:
        """
        Watches Player.log for a Join Code for as long as Valheim is
        actually running -- no arbitrary timeout, since session start time
        varies a lot depending on world size. Keeps checking every few
        seconds until either it finds a match, or Valheim's process exits.
        """
        log_path = Path(self.cfg["valheim_log_path"])

        while self.is_running():
            if log_path.exists():
                try:
                    text = log_path.read_text(encoding="utf-8", errors="ignore")
                    matches = JOIN_CODE_PATTERN.findall(text)
                    if matches:
                        return matches[-1]  # most recent "is active" code
                except Exception:
                    pass
            time.sleep(3)

        return None  # Valheim closed before a join code ever showed up
