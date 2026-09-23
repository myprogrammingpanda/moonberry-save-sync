"""Reading and writing Xbox / Game Pass (PC) save containers -- the "wgs"
(Windows Game Saves) store under
%LOCALAPPDATA%\\Packages\\<package family name>\\SystemAppData\\wgs.

Game-agnostic: games whose Game Pass build keeps its saves here (instead
of in a plain folder the way Valheim's does) build their adapter on this,
passing their own package family name and container names.

Layout of one user's store (wgs\\<xuid hex>_<scid hex>\\):

    containers.index         -- every container: name, sequence number,
                                folder GUID, mtime, size
    <folder GUID>\\
        container.<seq>      -- that container's file list (name -> blob GUID)
        <blob GUID>          -- the raw bytes of one file in the container

GUIDs are stored little-endian ("bytes_le") and used as uppercase hex with
no dashes for folder/blob names. Every write gets a fresh blob GUID and a
bumped sequence number -- the game itself does the same, which is how the
Xbox app notices a container changed and needs uploading.

Only ever touch a store while the game is closed, and give the Xbox app a
minute after the game exits to finish its own cloud sync first."""

import logging
import os
import struct
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

log = logging.getLogger("moonberry-sync")

INDEX_NAME = "containers.index"
_INDEX_VERSION = 14
_CONTAINER_FILE_VERSION = 4
_CONTAINER_FILE_NAME_CHARS = 64

# Container flags, as observed (not documented anywhere): 1 = in sync with
# the cloud, 2 = changed locally since the last upload (keeps its cloud
# etag), 5 = created locally and never uploaded (no etag yet) -- the same
# value other working Game Pass save importers use for new containers.
_FLAG_MODIFIED_LOCAL = 2
_FLAG_NEW_LOCAL = 5

_FILETIME_EPOCH_OFFSET = 116444736000000000  # 1601-01-01 -> 1970-01-01, in 100ns ticks


class XboxSaveFormatError(Exception):
    """The store's files don't look like the format this module understands
    -- raised instead of guessing, so nothing ever gets written back over
    a store we might be misreading."""


def filetime_now() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 10_000_000) + _FILETIME_EPOCH_OFFSET


def filetime_to_datetime(value: int) -> datetime:
    return datetime.fromtimestamp((value - _FILETIME_EPOCH_OFFSET) / 10_000_000)


def _guid_name(guid: uuid.UUID) -> str:
    return guid.hex.upper()


class _Reader:
    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    def take(self, n: int) -> bytes:
        if self.pos + n > len(self.data):
            raise XboxSaveFormatError("unexpected end of file")
        chunk = self.data[self.pos:self.pos + n]
        self.pos += n
        return chunk

    def u8(self) -> int:
        return self.take(1)[0]

    def u32(self) -> int:
        return struct.unpack("<I", self.take(4))[0]

    def u64(self) -> int:
        return struct.unpack("<Q", self.take(8))[0]

    def guid(self) -> uuid.UUID:
        return uuid.UUID(bytes_le=self.take(16))

    def string(self) -> str:
        length = self.u32()
        return self.take(length * 2).decode("utf-16-le")


def _write_string(out: BytesIO, value: str) -> None:
    out.write(struct.pack("<I", len(value)))
    out.write(value.encode("utf-16-le"))


@dataclass
class ContainerEntry:
    name: str
    cloud_id: str       # the Xbox cloud's etag for this container; "" if never uploaded
    seq: int            # matches the container.<seq> file in the folder
    flag: int
    folder: uuid.UUID
    mtime: int          # FILETIME
    size: int           # total bytes of the container's blobs

    @property
    def modified(self) -> datetime:
        return filetime_to_datetime(self.mtime)


@dataclass
class _IndexHeader:
    flag1: int
    package_name: str
    mtime: int
    flag2: int
    index_id: str
    unknown: int


def _parse_index(data: bytes) -> tuple[_IndexHeader, list[ContainerEntry]]:
    r = _Reader(data)
    version = r.u32()
    if version != _INDEX_VERSION:
        raise XboxSaveFormatError(f"unsupported {INDEX_NAME} version {version}")
    count = r.u32()
    header = _IndexHeader(
        flag1=r.u32(), package_name=r.string(), mtime=r.u64(),
        flag2=r.u32(), index_id=r.string(), unknown=r.u64(),
    )
    entries = []
    for _ in range(count):
        name = r.string()
        if r.string() != name:
            raise XboxSaveFormatError(f"container name mismatch for {name!r}")
        cloud_id = r.string()
        seq = r.u8()
        flag = r.u32()
        folder = r.guid()
        mtime = r.u64()
        if r.u64() != 0:
            raise XboxSaveFormatError(f"unexpected data in entry {name!r}")
        size = r.u64()
        entries.append(ContainerEntry(name, cloud_id, seq, flag, folder, mtime, size))
    if r.pos != len(data):
        raise XboxSaveFormatError(f"{len(data) - r.pos} trailing bytes in {INDEX_NAME}")
    return header, entries


def _build_index(header: _IndexHeader, entries: list[ContainerEntry]) -> bytes:
    out = BytesIO()
    out.write(struct.pack("<III", _INDEX_VERSION, len(entries), header.flag1))
    _write_string(out, header.package_name)
    out.write(struct.pack("<QI", header.mtime, header.flag2))
    _write_string(out, header.index_id)
    out.write(struct.pack("<Q", header.unknown))
    for e in entries:
        _write_string(out, e.name)
        _write_string(out, e.name)
        _write_string(out, e.cloud_id)
        out.write(struct.pack("<BI", e.seq, e.flag))
        out.write(e.folder.bytes_le)
        out.write(struct.pack("<QQQ", e.mtime, 0, e.size))
    return out.getvalue()


def _parse_container_file(data: bytes) -> dict[str, uuid.UUID]:
    """container.<seq> -> {file name: blob GUID}."""
    r = _Reader(data)
    version = r.u32()
    if version != _CONTAINER_FILE_VERSION:
        raise XboxSaveFormatError(f"unsupported container file version {version}")
    files = {}
    for _ in range(r.u32()):
        name = r.take(_CONTAINER_FILE_NAME_CHARS * 2).decode("utf-16-le").split("\0", 1)[0]
        r.take(16)  # cloud-side GUID; mirrors the local one once uploaded
        files[name] = r.guid()
    return files


def _build_container_file(files: dict[str, uuid.UUID]) -> bytes:
    out = BytesIO()
    out.write(struct.pack("<II", _CONTAINER_FILE_VERSION, len(files)))
    for name, blob in files.items():
        encoded = name.encode("utf-16-le")
        if len(encoded) > _CONTAINER_FILE_NAME_CHARS * 2:
            raise ValueError(f"container file name too long: {name!r}")
        out.write(encoded.ljust(_CONTAINER_FILE_NAME_CHARS * 2, b"\0"))
        out.write(blob.bytes_le)
        out.write(blob.bytes_le)
    return out.getvalue()


def _write_atomic(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


class XboxSaveStore:
    """One Xbox user's save containers for one game."""

    def __init__(self, user_dir: Path):
        self.user_dir = Path(user_dir)

    @staticmethod
    def wgs_root(package_family_name: str) -> Path | None:
        local_appdata = os.environ.get("LOCALAPPDATA")
        if sys.platform != "win32" or not local_appdata:
            return None
        return Path(local_appdata) / "Packages" / package_family_name / "SystemAppData" / "wgs"

    @classmethod
    def find(cls, package_family_name: str) -> "XboxSaveStore | None":
        """The store for this game on this PC -- the most recently written
        one if more than one Xbox account has played here. None if the
        game has never saved anything yet."""
        root = cls.wgs_root(package_family_name)
        return cls.latest_in(root) if root is not None else None

    @classmethod
    def latest_in(cls, root: Path) -> "XboxSaveStore | None":
        """Same as find(), given the wgs folder itself."""
        root = Path(root)
        if not root.is_dir():
            return None
        user_dirs = [d for d in root.iterdir() if d.is_dir() and (d / INDEX_NAME).is_file()]
        if not user_dirs:
            return None
        return cls(max(user_dirs, key=lambda d: (d / INDEX_NAME).stat().st_mtime))

    # -- reading --

    def _load(self) -> tuple[_IndexHeader, list[ContainerEntry]]:
        return _parse_index((self.user_dir / INDEX_NAME).read_bytes())

    def entries(self) -> list[ContainerEntry]:
        return self._load()[1]

    def entry(self, name: str) -> ContainerEntry | None:
        return next((e for e in self.entries() if e.name == name), None)

    def _folder(self, entry: ContainerEntry) -> Path:
        return self.user_dir / _guid_name(entry.folder)

    def _files_of(self, entry: ContainerEntry) -> dict[str, uuid.UUID]:
        return _parse_container_file((self._folder(entry) / f"container.{entry.seq}").read_bytes())

    def read(self, name: str) -> dict[str, bytes] | None:
        """{file name: bytes} for every file in the named container, or
        None if there's no such container."""
        entry = self.entry(name)
        if entry is None:
            return None
        folder = self._folder(entry)
        return {fname: (folder / _guid_name(blob)).read_bytes()
                for fname, blob in self._files_of(entry).items()}

    # -- writing (game must be closed) --

    def write(self, name: str, files: dict[str, bytes]) -> None:
        """Creates or replaces the named container with exactly `files`.
        New blobs and the new container.<seq> are written first and the
        index is swapped in last, so a crash part-way leaves the old
        container intact (plus a few orphaned files) rather than a
        half-written one."""
        header, entries = self._load()
        now = filetime_now()
        existing = next((e for e in entries if e.name == name), None)

        if existing is None:
            entry = ContainerEntry(name=name, cloud_id="", seq=1, flag=_FLAG_NEW_LOCAL,
                                   folder=uuid.uuid4(), mtime=now, size=0)
            old_files: dict[str, uuid.UUID] = {}
            old_seq = None
        else:
            entry = existing
            old_files = self._files_of(existing)
            old_seq = existing.seq
            entry.seq = existing.seq % 255 + 1
            if entry.cloud_id:
                entry.flag = _FLAG_MODIFIED_LOCAL

        folder = self._folder(entry)
        folder.mkdir(parents=True, exist_ok=True)
        new_files = {fname: uuid.uuid4() for fname in files}
        for fname, blob in new_files.items():
            _write_atomic(folder / _guid_name(blob), files[fname])
        _write_atomic(folder / f"container.{entry.seq}", _build_container_file(new_files))

        entry.mtime = now
        entry.size = sum(len(data) for data in files.values())
        if existing is None:
            entries.append(entry)
        header.mtime = now
        _write_atomic(self.user_dir / INDEX_NAME, _build_index(header, entries))

        # Only now that the index points at the new files is the old
        # version unreferenced.
        if old_seq is not None:
            (folder / f"container.{old_seq}").unlink(missing_ok=True)
        for blob in old_files.values():
            (folder / _guid_name(blob)).unlink(missing_ok=True)
