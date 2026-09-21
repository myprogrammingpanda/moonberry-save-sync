"""Valheim-specific logic: save format detection, join-code parsing, process
detection, and launching. Everything here is ported as-is from the original
tested app -- only the packaging into a GameAdapter is new."""

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
    def save_key_prefix(self) -> str:
        # Keep the pre-existing "world_save_" prefix (rather than the
        # default "valheim_save_") so saves already uploaded to R2 under
        # the old app aren't orphaned by this rewrite.
        return "world_save_"

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

    def _get_world_target(self):
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
        name = self.cfg["valheim_world_name"]

        new_format_dir = folder / name
        if new_format_dir.is_dir():
            return "folder", new_format_dir

        return "files", [folder / f"{name}.db", folder / f"{name}.fwl"]

    def has_local_save(self) -> bool:
        kind, target = self._get_world_target()
        if kind == "folder":
            return target.is_dir()
        return all(f.exists() for f in target)

    def backup_local_save(self, backup_dir: Path) -> None:
        """Keep a timestamped copy of the current local save before
        overwriting, just in case something goes wrong with a cloud sync.
        Handles both the 1.0+ folder format and the legacy flat-file
        format."""
        backup_dir.mkdir(exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        kind, target = self._get_world_target()
        if kind == "folder":
            if target.exists():
                shutil.copytree(target, backup_dir / f"{target.name}_{stamp}")
        else:
            for f in target:
                if f.exists():
                    shutil.copy2(f, backup_dir / f"{f.stem}_{stamp}{f.suffix}")
        log.info("Backed up local save (if present) to %s", backup_dir)

    def zip_save(self, dest_zip: Path) -> None:
        """
        Zips whatever format is actually present. For the 1.0+ folder
        format, the world name is preserved as a path prefix inside the zip
        (e.g. "world/_main.1.db2") so that extracting it back into
        worlds_local correctly recreates the subfolder -- not just dumps
        loose chunk files into worlds_local directly.
        """
        kind, target = self._get_world_target()
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
