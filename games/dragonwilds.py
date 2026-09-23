"""RuneScape: Dragonwilds-specific logic: in-game co-op hosting ("Play with
Friends", world lives on the host's PC -- not the separate dedicated
server product), with world saves stored one of two ways depending on the
edition:

- Steam: plain files in %LOCALAPPDATA%\\RSDragonwilds\\Saved\\SaveGames --
  "<World>.sav" plus the game's own rolling "<World>.sav.backup".
- Xbox app / Game Pass (PC): Xbox save containers (core/xbox_saves.py) --
  "<World>Qxav" plus its "<World>QxavQbak" backup, each holding one "Data"
  file.

Characters are separate (SaveCharacters\\*.json on Steam, "<Name>Qjson"
containers on Game Pass) and deliberately never synced: each player keeps
their own character and brings it into whichever world they join.

Both editions store the same world format inside ("SAVE" bytes); Game Pass
just wraps it in a 12-byte header + zlib. Zips always hold the raw form as
"<World>.sav" / "<World>.sav.backup", so a Steam player and a Game Pass
player can hand the same world back and forth. Verified on a real Game
Pass install: a world converted from a Steam .sav (Early Access era) loaded
and synced to the Xbox cloud. Not yet verified: a Steam 1.0 install --
the .sav.backup name and whether 1.0 Steam files are raw or wrapped are
from research, so the Steam side writes whichever form the existing local
file already uses."""

import hashlib
import logging
import os
import struct
import zipfile
import zlib
from datetime import datetime
from pathlib import Path

import psutil

from core.game_base import GameAdapter, GameEdition
from core.platform_detect import steam_app_installed, store_package_installed
from core.xbox_saves import XboxSaveStore

log = logging.getLogger("moonberry-sync")

STEAM_APP_ID = 1374490
STORE_PACKAGE_FAMILY = "JagexLimited.Dominion_srxstwq7wczqa"
STORE_APP_ID = "AppRSDragonwildsShipping"

# RSDragonwilds.exe (both editions' launcher stub) plus the Unreal
# shipping exe it starts (-Win64-Shipping on Steam, -WinGDK-Shipping on
# Game Pass -- the latter confirmed on a real install).
_PROCESS_PREFIX = "rsdragonwilds"

SAV_SUFFIX = ".sav"
BACKUP_SUFFIX = ".sav.backup"

# Game Pass container names: "." is written as "Q" and the world file
# extension is "xav" -- "TestWorldRS" -> "TestWorldRSQxav", its backup
# "TestWorldRSQxavQbak", character "Yinipur" -> "YinipurQjson" (observed on
# a real install).
_XBOX_WORLD_SUFFIX = "Qxav"
_XBOX_BACKUP_SUFFIX = "QxavQbak"
_XBOX_FILE = "Data"

# Game Pass blob header: u32 header size (12), u16 0, u16 1 (zlib),
# u32 uncompressed size.
_XBOX_HEADER = struct.Struct("<IHHI")
_SAVE_MAGIC = b"SAVE"

_LOCAL_SAVES = r"%LOCALAPPDATA%\RSDragonwilds\Saved\SaveGames"
_XBOX_SAVES = rf"%LOCALAPPDATA%\Packages\{STORE_PACKAGE_FAMILY}\SystemAppData\wgs"


def to_raw(data: bytes) -> bytes:
    """A world file's plain "SAVE" bytes, from either on-disk form."""
    if data[:4] == _SAVE_MAGIC:
        return data
    if len(data) >= _XBOX_HEADER.size:
        header_size, _zero, compression, raw_size = _XBOX_HEADER.unpack_from(data)
        if header_size == _XBOX_HEADER.size and compression == 1:
            raw = zlib.decompress(data[_XBOX_HEADER.size:])
            if len(raw) == raw_size and raw[:4] == _SAVE_MAGIC:
                return raw
    raise ValueError("not a Dragonwilds world save (unrecognised format)")


def to_wrapped(raw: bytes) -> bytes:
    """The Game Pass on-disk form of plain "SAVE" bytes."""
    return _XBOX_HEADER.pack(_XBOX_HEADER.size, 0, 1, len(raw)) + zlib.compress(raw)


def _is_wrapped(data: bytes) -> bool:
    return data[:4] != _SAVE_MAGIC


class DragonwildsAdapter(GameAdapter):
    game_id = "dragonwilds"
    display_name = "RuneScape: Dragonwilds"

    config_fields = [
        ("dragonwilds_saves_folder", "Saves folder", "folder"),
        ("dragonwilds_world_name", "World name", "text"),
        ("dragonwilds_launch_uri", "Launch URI", "text"),
    ]

    editions = {
        "steam": GameEdition(
            label="Steam",
            defaults={
                "dragonwilds_saves_folder": _LOCAL_SAVES,
                "dragonwilds_launch_uri": f"steam://rungameid/{STEAM_APP_ID}",
            },
        ),
        "gamepass": GameEdition(
            label="Xbox app / Game Pass (PC)",
            defaults={
                "dragonwilds_saves_folder": _XBOX_SAVES,
                "dragonwilds_launch_uri": rf"shell:appsFolder\{STORE_PACKAGE_FAMILY}!{STORE_APP_ID}",
            },
            hint=(
                "Game Pass keeps worlds in Xbox save storage. Launch Dragonwilds "
                "and create or load any world once on this PC before syncing, so "
                "that storage exists."
            ),
        ),
    }

    @classmethod
    def detect_editions(cls) -> list[str]:
        found = []
        if steam_app_installed(STEAM_APP_ID):
            found.append("steam")
        if store_package_installed(STORE_PACKAGE_FAMILY):
            found.append("gamepass")
        return found

    @property
    def default_save_name(self) -> str:
        return self.cfg["dragonwilds_world_name"]

    # -- process control --

    def is_running(self) -> bool:
        for proc in psutil.process_iter(["name"]):
            name = proc.info["name"]
            if name and name.lower().startswith(_PROCESS_PREFIX):
                return True
        return False

    def launch(self) -> None:
        log.info("Launching RuneScape: Dragonwilds...")
        os.startfile(self.cfg["dragonwilds_launch_uri"])  # noqa: S606 (Windows-only, intentional)

    # -- where saves live --

    @property
    def _saves_folder(self) -> Path:
        return Path(self.cfg["dragonwilds_saves_folder"])

    @property
    def _uses_xbox_store(self) -> bool:
        """The configured folder is the Game Pass "wgs" container root
        rather than a plain SaveGames folder -- decided by the configured
        path itself, never by the edition setting."""
        return self._saves_folder.name.lower() == "wgs"

    def _xbox_store(self) -> XboxSaveStore | None:
        root = self._saves_folder
        if not root.is_dir():
            return None
        stores = [d for d in root.iterdir() if d.is_dir() and (d / "containers.index").is_file()]
        if not stores:
            return None
        # More than one Xbox account on this PC: the one that played last.
        return XboxSaveStore(max(stores, key=lambda d: (d / "containers.index").stat().st_mtime))

    def _require_xbox_store(self) -> XboxSaveStore:
        store = self._xbox_store()
        if store is None:
            raise RuntimeError(
                "No Game Pass save storage found for Dragonwilds on this PC yet. "
                "Launch the game and load or create any world once, then try again."
            )
        return store

    # -- reading/writing one world, in either form --

    def _read_world(self, save_name: str | None = None) -> dict[str, bytes]:
        """{".sav": bytes, ".sav.backup": bytes} as stored on disk (either
        form), only the ones that exist."""
        name = save_name or self.default_save_name
        found = {}
        if self._uses_xbox_store:
            store = self._xbox_store()
            if store is None:
                return {}
            for suffix, container in ((SAV_SUFFIX, name + _XBOX_WORLD_SUFFIX),
                                      (BACKUP_SUFFIX, name + _XBOX_BACKUP_SUFFIX)):
                files = store.read(container)
                if files and _XBOX_FILE in files:
                    found[suffix] = files[_XBOX_FILE]
        else:
            for suffix in (SAV_SUFFIX, BACKUP_SUFFIX):
                path = self._saves_folder / (name + suffix)
                if path.is_file():
                    found[suffix] = path.read_bytes()
        return found

    def _write_world(self, name: str, raw_files: dict[str, bytes]) -> None:
        """Writes plain "SAVE" bytes for ".sav"/".sav.backup" into this
        edition's storage, converting to its on-disk form."""
        if self._uses_xbox_store:
            store = self._require_xbox_store()
            for suffix, container in ((SAV_SUFFIX, name + _XBOX_WORLD_SUFFIX),
                                      (BACKUP_SUFFIX, name + _XBOX_BACKUP_SUFFIX)):
                if suffix in raw_files:
                    store.write(container, {_XBOX_FILE: to_wrapped(raw_files[suffix])})
            return

        folder = self._saves_folder
        folder.mkdir(parents=True, exist_ok=True)
        for suffix, raw in raw_files.items():
            path = folder / (name + suffix)
            # Match whatever form this install already uses; plain if
            # there's nothing to go by (the only Steam form seen so far).
            wrap = path.is_file() and _is_wrapped(path.read_bytes())
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_bytes(to_wrapped(raw) if wrap else raw)
            os.replace(tmp, path)

    # -- GameAdapter save API --

    def has_local_save(self, save_name: str | None = None) -> bool:
        return SAV_SUFFIX in self._read_world(save_name)

    def list_local_saves(self) -> list[str]:
        """Every world found, excluding the game's own backup copies and
        character saves."""
        if self._uses_xbox_store:
            store = self._xbox_store()
            if store is None:
                return []
            return sorted(e.name[:-len(_XBOX_WORLD_SUFFIX)] for e in store.entries()
                          if e.name.endswith(_XBOX_WORLD_SUFFIX))
        folder = self._saves_folder
        if not folder.is_dir():
            return []
        return sorted(f.name[:-len(SAV_SUFFIX)] for f in folder.iterdir()
                      if f.is_file() and f.name.endswith(SAV_SUFFIX))

    def save_stat(self, save_name: str) -> tuple[datetime | None, int | None]:
        if self._uses_xbox_store:
            store = self._xbox_store()
            entries = [e for e in (store.entries() if store else [])
                       if e.name in (save_name + _XBOX_WORLD_SUFFIX, save_name + _XBOX_BACKUP_SUFFIX)]
            if not any(e.name.endswith(_XBOX_WORLD_SUFFIX) for e in entries):
                return None, None
            return max(e.modified for e in entries), sum(e.size for e in entries)
        files = [p for p in (self._saves_folder / (save_name + s) for s in (SAV_SUFFIX, BACKUP_SUFFIX))
                 if p.is_file()]
        if not any(p.name.endswith(SAV_SUFFIX) for p in files):
            return None, None
        stats = [p.stat() for p in files]
        return datetime.fromtimestamp(max(s.st_mtime for s in stats)), sum(s.st_size for s in stats)

    def content_hash(self, save_name: str | None = None) -> str | None:
        """Hashes the plain "SAVE" bytes, so the same world hashes the same
        whether it's stored as a Steam file or a Game Pass container."""
        files = self._read_world(save_name)
        if SAV_SUFFIX not in files:
            return None
        hasher = hashlib.sha256()
        for suffix in sorted(files):
            hasher.update(suffix.encode("utf-8"))
            hasher.update(to_raw(files[suffix]))
        return hasher.hexdigest()

    def backup_local_save(self, backup_dir: Path, save_name: str | None = None) -> None:
        name = save_name or self.default_save_name
        files = self._read_world(name)
        if files:
            backup_dir.mkdir(exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            for suffix, data in files.items():
                # Stored exactly as found on disk, either form.
                (backup_dir / f"{name}_{stamp}{suffix}").write_bytes(data)
        log.info("Backed up local save (if present) to %s", backup_dir)

    def zip_save(self, dest_zip: Path, save_name: str | None = None) -> None:
        """Flat "<World>.sav" (+ "<World>.sav.backup") in plain form -- the
        world name travels as the file name, which is also what the game
        requires it to be."""
        name = save_name or self.default_save_name
        files = self._read_world(name)
        with zipfile.ZipFile(dest_zip, "w", zipfile.ZIP_DEFLATED) as zf:
            for suffix, data in files.items():
                zf.writestr(name + suffix, to_raw(data))

    def unzip_save(self, src_zip: Path) -> None:
        worlds: dict[str, dict[str, bytes]] = {}
        with zipfile.ZipFile(src_zip, "r") as zf:
            for member in zf.namelist():
                base = Path(member).name
                for suffix in (BACKUP_SUFFIX, SAV_SUFFIX):  # longest first
                    if base.endswith(suffix):
                        worlds.setdefault(base[:-len(suffix)], {})[suffix] = to_raw(zf.read(member))
                        break
        for name, raw_files in worlds.items():
            self._write_world(name, raw_files)
