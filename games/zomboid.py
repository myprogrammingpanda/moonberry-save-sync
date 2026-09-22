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

    @property
    def default_save_name(self) -> str:
        return self.cfg["zomboid_server_name"]

    # save_key_prefix (gamename_servername, e.g. "zomboid_servertest_",
    # matching Valheim's gamename_worldname scheme) is inherited from
    # GameAdapter.save_key_prefix, built from default_save_name above.

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

    def _save_dir(self, save_name: str | None = None) -> Path:
        """
        The authoritative world save (players.db, vehicles.db, map/chunk
        data). Hosting locally also creates a sibling "<name>_player"
        folder -- the host's own client-side map/chunk cache -- which is
        deliberately NOT synced here: it holds no player or world data of
        its own and is expected to regenerate on its own if missing.
        """
        return Path(self.cfg["zomboid_saves_folder"]) / (save_name or self.default_save_name)

    def has_local_save(self, save_name: str | None = None) -> bool:
        return self._save_dir(save_name).is_dir()

    def list_local_saves(self) -> list[str]:
        """Every server-name folder under zomboid_saves_folder, excluding
        the "<name>_player" sibling folders hosting locally creates --
        those are a disposable client-side chunk cache, not a real save,
        and must never be listed as if they were a selectable one."""
        root = Path(self.cfg["zomboid_saves_folder"])
        if not root.is_dir():
            return []
        return sorted(d.name for d in root.iterdir() if d.is_dir() and not d.name.endswith("_player"))

    def save_stat(self, save_name: str) -> tuple[datetime | None, int | None]:
        target = self._save_dir(save_name)
        files = [f for f in target.rglob("*") if f.is_file()] if target.is_dir() else []
        if not files:
            return None, None
        stats = [f.stat() for f in files]
        return datetime.fromtimestamp(max(s.st_mtime for s in stats)), sum(s.st_size for s in stats)

    def content_hash(self, save_name: str | None = None) -> str | None:
        target = self._save_dir(save_name)
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

    def backup_local_save(self, backup_dir: Path, save_name: str | None = None) -> None:
        target = self._save_dir(save_name)
        if target.exists():
            backup_dir.mkdir(exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            shutil.copytree(target, backup_dir / f"{target.name}_{stamp}")
        log.info("Backed up local save (if present) to %s", backup_dir)

    def zip_save(self, dest_zip: Path, save_name: str | None = None) -> None:
        """Preserves the server-name folder as a path prefix inside the
        zip (e.g. "servertest/players.db") so extracting it back into the
        Multiplayer saves folder recreates the subfolder, not loose files."""
        target = self._save_dir(save_name)
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
