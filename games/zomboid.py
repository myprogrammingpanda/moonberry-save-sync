"""Project Zomboid-specific logic: process detection, launching, and save
handling for in-game "Host" co-op (not a standalone dedicated server)."""

import hashlib
import logging
import os
import shutil
import zipfile
from datetime import datetime
from pathlib import Path

import psutil

from core.game_base import GameAdapter

log = logging.getLogger("moonberry-sync")


class ZomboidAdapter(GameAdapter):
    game_id = "zomboid"
    display_name = "Project Zomboid"

    config_fields = [
        ("zomboid_saves_folder", "Multiplayer saves folder", "folder"),
        ("zomboid_server_name", "Server name", "text"),
        ("zomboid_launch_uri", "Launch URI (Steam)", "text"),
    ]

    # -- process control --

    def is_running(self) -> bool:
        for proc in psutil.process_iter(["name"]):
            if proc.info["name"] and proc.info["name"].lower() == "projectzomboid64.exe":
                return True
        return False

    def launch(self) -> None:
        log.info("Launching Project Zomboid...")
        os.startfile(self.cfg["zomboid_launch_uri"])  # noqa: S606 (Windows-only, intentional)

    # -- save format --

    def _save_dir(self) -> Path:
        """
        The authoritative world save (players.db, vehicles.db, map/chunk
        data). Hosting locally also creates a sibling "<name>_player"
        folder -- the host's own client-side map/chunk cache -- which is
        deliberately NOT synced here: it holds no player or world data of
        its own and is expected to regenerate on its own if missing.
        """
        return Path(self.cfg["zomboid_saves_folder"]) / self.cfg["zomboid_server_name"]

    def has_local_save(self) -> bool:
        return self._save_dir().is_dir()

    def content_hash(self) -> str | None:
        target = self._save_dir()
        if not target.is_dir():
            return None
        files = sorted(f for f in target.rglob("*") if f.is_file())
        if not files:
            return None
        hasher = hashlib.sha256()
        for f in files:
            hasher.update(str(f.relative_to(target)).encode("utf-8"))
            hasher.update(f.read_bytes())
        return hasher.hexdigest()

    def backup_local_save(self, backup_dir: Path) -> None:
        target = self._save_dir()
        if target.exists():
            backup_dir.mkdir(exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            shutil.copytree(target, backup_dir / f"{target.name}_{stamp}")
        log.info("Backed up local save (if present) to %s", backup_dir)

    def zip_save(self, dest_zip: Path) -> None:
        """Preserves the server-name folder as a path prefix inside the
        zip (e.g. "servertest/players.db") so extracting it back into the
        Multiplayer saves folder recreates the subfolder, not loose files."""
        target = self._save_dir()
        with zipfile.ZipFile(dest_zip, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in target.rglob("*"):
                if f.is_file():
                    arcname = str(Path(target.name) / f.relative_to(target))
                    zf.write(f, arcname=arcname)

    def unzip_save(self, src_zip: Path) -> None:
        folder = Path(self.cfg["zomboid_saves_folder"])
        folder.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(src_zip, "r") as zf:
            zf.extractall(folder)
