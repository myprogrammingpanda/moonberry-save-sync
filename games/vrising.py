"""V Rising-specific logic: process detection, launching, and save handling
for a client-hosted "Private Game" (not a dedicated server).

Each world is one GUID-named folder under a "Saves\\v4" folder, holding
rolling AutoSave_<n>.save.gz files (up to 10, one every couple of minutes)
plus a few small JSON files -- ServerHostSettings.json carries the world's
in-game name (and its password: the game asks the host for one every time
anyway, so syncing it is harmless). Worlds can live in two places, and the
game lists both side by side:
- the local folder, %USERPROFILE%\\AppData\\LocalLow\\Stunlock Studios\\
  VRising\\Saves\\v4 -- the one configured here, and where new worlds are
  downloaded to (away from Steam Cloud);
- the Steam Cloud folder, ...\\VRising\\CloudSaves\\<SteamID>\\v4 -- the
  game's default for worlds created in-game. Found automatically, so no
  one has to look up their Steam ID; a world already there is updated in
  place, so there's never a second copy of it.

Moving a world to a new host is officially supported: copy its folder into
the new host's saves folder and pick it from Continue/Load Game. Verified
on a real install: a copy holding only the newest autosave plus the JSON
files loads as a normal save, so that's all that's synced (~7 MB instead
of ~70 MB). Each player's character is keyed to their Steam account inside
the world, so everyone keeps their own when a different friend hosts.
"""

import hashlib
import json
import logging
import os
import re
import shutil
import zipfile
from datetime import datetime
from pathlib import Path

import psutil

from core.game_base import GameAdapter, sanitize_key_component

log = logging.getLogger("moonberry-sync")

STEAM_APP_ID = 1604030

# The client, plus the server it runs in the background while hosting a
# Private Game. The server writes the final save as it shuts down, so a
# session only counts as over once both are gone.
_PROCESS_NAMES = {"vrising.exe", "vrisingserver.exe"}

_HOST_SETTINGS = "ServerHostSettings.json"
_AUTOSAVE_RE = re.compile(r"AutoSave_(\d+)\.save(\.gz)?", re.IGNORECASE)


def _read_world_name(world_dir: Path) -> str | None:
    try:
        data = json.loads((world_dir / _HOST_SETTINGS).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    name = data.get("Name") if isinstance(data, dict) else None
    return name.strip() if isinstance(name, str) and name.strip() else None


def _autosaves(world_dir: Path) -> list[Path]:
    """This world's autosave files, oldest first (by modified time, then
    number -- the game loads the newest one)."""
    found = []
    for f in world_dir.iterdir():
        match = _AUTOSAVE_RE.fullmatch(f.name)
        if match and f.is_file():
            found.append((f.stat().st_mtime, int(match.group(1)), f))
    return [f for _mtime, _num, f in sorted(found)]


def _synced_files(world_dir: Path) -> list[Path]:
    """What a synced copy of the world holds: the newest autosave and the
    top-level JSON files -- not the older autosaves or the game's .TEMP
    scratch folder."""
    saves = _autosaves(world_dir)
    jsons = sorted(f for f in world_dir.glob("*.json") if f.is_file())
    return ([saves[-1]] if saves else []) + jsons


class VRisingAdapter(GameAdapter):
    game_id = "vrising"
    display_name = "V Rising"

    config_fields = [
        ("vrising_saves_folder", "Saves folder", "folder"),
        ("vrising_world_name", "World name", "text"),
        ("vrising_launch_uri", "Launch URI (Steam)", "text"),
    ]

    @property
    def default_save_name(self) -> str:
        """The world's in-game name, as shown in Continue/Load Game -- what
        friends type in Settings, never the GUID folder name."""
        return self.cfg["vrising_world_name"].strip()

    def slot_id_for(self, save_name: str) -> str:
        # Lowercased so a world name typed in a different case than the
        # game shows it still matches the same world and storage key.
        return sanitize_key_component(save_name).lower()

    def setup_problem(self) -> str | None:
        # The folder above Saves\v4 is the game's own data folder, which it
        # creates on first launch -- missing means the game has never run
        # here (or the path is wrong).
        if not self._data_folder.is_dir():
            return (
                "V Rising hasn't been started on this PC yet (or the Saves folder in its "
                "settings is wrong). Launch the game once, quit, then try again."
            )
        return None

    # -- process control --

    def is_running(self) -> bool:
        for proc in psutil.process_iter(["name"]):
            name = proc.info["name"]
            if name and name.lower() in _PROCESS_NAMES:
                return True
        return False

    def launch(self) -> None:
        log.info("Launching V Rising...")
        os.startfile(self.cfg["vrising_launch_uri"])  # noqa: S606 (Windows-only, intentional)

    # -- where worlds live --

    @property
    def _local_root(self) -> Path:
        return Path(self.cfg["vrising_saves_folder"])

    @property
    def _data_folder(self) -> Path:
        # ...\VRising\Saves\v4 -> ...\VRising
        return self._local_root.parent.parent

    def _roots(self) -> list[Path]:
        """The local saves folder, then each Steam account's cloud saves
        folder for the same save-format version (the "v4" part)."""
        roots = [self._local_root]
        cloud = self._data_folder / "CloudSaves"
        if cloud.is_dir():
            roots += sorted(d / self._local_root.name for d in cloud.iterdir() if (d / self._local_root.name).is_dir())
        return roots

    def _worlds(self) -> list[tuple[str, Path]]:
        """(in-game name, folder) for every world on this PC."""
        worlds = []
        for root in self._roots():
            if not root.is_dir():
                continue
            for d in root.iterdir():
                if d.is_dir():
                    name = _read_world_name(d)
                    if name:
                        worlds.append((name, d))
        return worlds

    def _world_dir(self, save_name: str | None = None) -> Path | None:
        """The folder holding the named world. If more than one world has
        that name (worlds are only told apart by name here), the most
        recently played one."""
        wanted = self.slot_id_for(save_name or self.default_save_name)
        matches = [d for name, d in self._worlds() if self.slot_id_for(name) == wanted]
        if not matches:
            return None
        return max(matches, key=lambda d: max((f.stat().st_mtime for f in _synced_files(d)), default=0))

    def _require_world_dir(self, save_name: str | None) -> Path:
        target = self._world_dir(save_name)
        if target is None:
            raise FileNotFoundError(f"No V Rising world named '{save_name or self.default_save_name}' on this PC.")
        return target

    # -- save handling --

    def has_local_save(self, save_name: str | None = None) -> bool:
        target = self._world_dir(save_name)
        return target is not None and bool(_autosaves(target))

    def list_local_saves(self) -> list[str]:
        """Each world's in-game name, once per distinct name -- a world
        with no autosave yet (created but never saved) isn't listed."""
        seen = {}
        for name, d in self._worlds():
            if _autosaves(d):
                seen.setdefault(self.slot_id_for(name), name)
        return sorted(seen.values(), key=str.lower)

    def save_stat(self, save_name: str) -> tuple[datetime | None, int | None]:
        target = self._world_dir(save_name)
        files = [f for f in target.rglob("*") if f.is_file()] if target else []
        if not files:
            return None, None
        stats = [f.stat() for f in files]
        return datetime.fromtimestamp(max(s.st_mtime for s in stats)), sum(s.st_size for s in stats)

    def content_hash(self, save_name: str | None = None) -> str | None:
        target = self._world_dir(save_name)
        files = _synced_files(target) if target else []
        if not files:
            return None
        hasher = hashlib.sha256()
        for f in files:
            hasher.update(f.name.encode("utf-8"))
            hasher.update(f.read_bytes())
        return hasher.hexdigest()

    def backup_local_save(self, backup_dir: Path, save_name: str | None = None) -> None:
        # Nothing to back up yet is normal (first download of this world).
        target = self._world_dir(save_name)
        if target is not None:
            backup_dir.mkdir(exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            label = sanitize_key_component(_read_world_name(target) or target.name)
            shutil.copytree(target, backup_dir / f"vrising_{label}_{target.name}_{stamp}")
        log.info("Backed up local save (if present) to %s", backup_dir)

    def zip_save(self, dest_zip: Path, save_name: str | None = None) -> None:
        """Keeps the world's GUID folder name as a prefix inside the zip
        ("<guid>/AutoSave_3.save.gz"), so every PC ends up with the same
        folder for the same world."""
        target = self._require_world_dir(save_name)
        files = _synced_files(target)
        if not any(_AUTOSAVE_RE.fullmatch(f.name) for f in files):
            raise FileNotFoundError(f"V Rising world '{save_name or self.default_save_name}' has no autosave yet.")
        with zipfile.ZipFile(dest_zip, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in files:
                zf.write(f, arcname=f"{target.name}/{f.name}")

    def unzip_save(self, src_zip: Path) -> None:
        """Replaces the world's whole folder with the zip's contents (it's
        been backed up first by the caller) -- merging would leave this
        PC's own older-numbered autosaves beside the incoming one, and the
        game might load the wrong one. The world already on this PC is
        found by folder name first, then by in-game name, so an update
        lands where the game already looks for it (local or Steam Cloud);
        a world new to this PC goes into the local saves folder."""
        with zipfile.ZipFile(src_zip, "r") as zf:
            members = {}
            for info in zf.infolist():
                if info.is_dir():
                    continue
                parts = info.filename.replace("\\", "/").split("/")
                # Only "<folder>/<file>" -- nothing that could climb out of it.
                if len(parts) != 2 or not parts[0] or parts[0] in (".", "..") or not parts[1]:
                    raise ValueError(f"Unexpected entry in V Rising save zip: {info.filename!r}")
                members[info.filename] = parts
            folders = {folder for folder, _name in members.values()}
            if len(folders) != 1:
                raise ValueError("V Rising save zip should hold exactly one world folder.")
            folder = folders.pop()
            files = {name: zf.read(arc) for arc, (_folder, name) in members.items()}

        incoming_name = None
        if _HOST_SETTINGS in files:
            try:
                data = json.loads(files[_HOST_SETTINGS].decode("utf-8-sig"))
                incoming_name = data.get("Name") if isinstance(data, dict) else None
            except ValueError:
                pass

        target = next((d for _name, d in self._worlds() if d.name.lower() == folder.lower()), None)
        if target is None and incoming_name:
            target = self._world_dir(incoming_name)
        if target is None:
            target = self._local_root / folder

        if target.exists():
            shutil.rmtree(target)
        target.mkdir(parents=True)
        for name, data in files.items():
            (target / name).write_bytes(data)
        log.info("Placed V Rising world '%s' in %s", incoming_name or folder, target)
