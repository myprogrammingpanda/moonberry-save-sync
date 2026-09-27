"""S3-compatible cloud storage (R2, B2, etc) plus the local bookkeeping of
which save version this machine last synced to. Game-agnostic: every key is
namespaced by a per-game prefix (GameAdapter.save_key_prefix) so multiple
games can share one bucket without colliding."""

import logging
import re
import shutil
import time
from pathlib import Path

import boto3
from botocore.config import Config as BotoConfig

log = logging.getLogger("moonberry-sync")


class SaveStorage:
    """Despite living in a file named for R2, this works with any
    S3-compatible provider -- the full endpoint URL comes from config.json,
    so switching providers is just editing config.json, no code changes."""

    def __init__(self, cfg: dict, key_prefix: str):
        self.endpoint = cfg.get("storage_endpoint_url")
        if not self.endpoint:
            # Backward-compatible fallback for configs still using the old
            # R2-only field name.
            self.endpoint = f"https://{cfg['r2_account_id']}.r2.cloudflarestorage.com"

        self.access_key = cfg.get("storage_access_key_id", cfg.get("r2_access_key_id"))
        self.secret_key = cfg.get("storage_secret_access_key", cfg.get("r2_secret_access_key"))
        self.bucket = cfg.get("storage_bucket_name", cfg.get("r2_bucket_name"))
        # No default here on purpose: "world_save.zip" used to be hardcoded
        # as the fallback, a one-time migration aid for Valheim's original
        # unversioned save from before the versioned-per-game scheme (or
        # multi-game support) existed. Defaulting it for every game meant
        # any OTHER game's SaveStorage fell back to that exact same literal
        # key too, on a fresh coordinator with no save_key recorded yet --
        # confirmed as a real bug: Zomboid's Host Now downloaded and
        # unzipped whatever sat at that key (an old, unrelated legacy
        # object) into its own save folder. Only used if a config actually
        # sets it explicitly.
        self.legacy_key = cfg.get("storage_object_key", cfg.get("r2_object_key"))
        self.key_prefix = key_prefix

    def _client(self):
        return boto3.client(
            "s3",
            endpoint_url=self.endpoint,
            aws_access_key_id=self.access_key,
            aws_secret_access_key=self.secret_key,
            # Without explicit timeouts, a network hiccup can leave an
            # upload/download hanging far longer than reasonable for a
            # small save file.
            config=BotoConfig(
                connect_timeout=15,
                read_timeout=120,
                retries={"max_attempts": 2, "mode": "standard"},
            ),
        )

    def new_save_key(self) -> str:
        """Generates a unique key for a fresh upload, so each session's save
        gets its own file instead of overwriting the same one every time."""
        return f"{self.key_prefix}{int(time.time())}.zip"

    def download_save(self, dest_zip: Path, key: str) -> bool:
        client = self._client()
        try:
            client.download_file(self.bucket, key, str(dest_zip))
            return True
        except client.exceptions.ClientError as e:
            log.warning("Could not download save '%s' from cloud storage (%s)", key, e)
            return False

    def upload_save(self, src_zip: Path, key: str):
        client = self._client()
        client.upload_file(str(src_zip), self.bucket, key)

    def _is_own_save_key(self, key: str) -> bool:
        # Exactly <prefix><timestamp>.zip. A bare startswith() would also
        # match another save whose name merely begins with this one's
        # ("world" vs "world_old": prefix "valheim_world_" vs
        # "valheim_world_old_").
        rest = key[len(self.key_prefix):] if key.startswith(self.key_prefix) else ""
        return rest.endswith(".zip") and rest[:-4].isdigit()

    def prune_old_saves(self, keep: int = 5, protect: str | None = None):
        """Keeps cloud storage from growing forever now that every session
        creates a new file. Keeps the most recent `keep` save files for this
        save (plus `protect`, the one just uploaded) and deletes older ones
        for good. Never fatal.

        "For good" matters on Backblaze B2: its buckets keep every version
        by default, so a plain delete only hides a file and it keeps taking
        up space. Deleting each stored version by id removes it for real.
        Providers without versions (e.g. R2) don't list them; they get a
        plain delete, which is already permanent there."""
        try:
            client = self._client()
            try:
                versions = self._list_versions(client)
            except client.exceptions.ClientError as e:
                log.info("Storage doesn't list file versions (%s) -- pruning with plain deletes.", e)
                versions = None

            if versions is None:
                keys = [
                    obj["Key"]
                    for page in client.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=self.key_prefix)
                    for obj in page.get("Contents", [])
                    if self._is_own_save_key(obj["Key"])
                ]
                to_delete = self._keys_to_prune(keys, keep, protect)
                for key in to_delete:
                    client.delete_object(Bucket=self.bucket, Key=key)
                removed = len(to_delete)
            else:
                # key -> [version ids], and which keys are still visible
                # (their newest entry is a file, not a delete marker).
                visible = [key for key, entry in versions.items() if entry["visible"]]
                keep_set = set(visible) - set(self._keys_to_prune(visible, keep, protect))
                removed = 0
                for key, entry in versions.items():
                    if key in keep_set:
                        continue
                    for version_id in entry["ids"]:
                        client.delete_object(Bucket=self.bucket, Key=key, VersionId=version_id)
                    removed += 1

            if removed:
                log.info("Pruned %d old save version(s), kept the most recent %d.", removed, keep)
        except Exception:
            log.exception("Failed to prune old save versions (non-fatal, continuing).")

    def _list_versions(self, client) -> dict:
        """{key: {"ids": [version ids incl. delete markers], "visible": bool}}
        for this save's own keys."""
        versions = {}
        for page in client.get_paginator("list_object_versions").paginate(Bucket=self.bucket, Prefix=self.key_prefix):
            for kind in ("Versions", "DeleteMarkers"):
                for v in page.get(kind, []):
                    if not self._is_own_save_key(v["Key"]):
                        continue
                    entry = versions.setdefault(v["Key"], {"ids": [], "visible": False})
                    entry["ids"].append(v["VersionId"])
                    if kind == "Versions" and v.get("IsLatest"):
                        entry["visible"] = True
        return versions

    def _keys_to_prune(self, keys: list[str], keep: int, protect: str | None) -> list[str]:
        """Every key but the newest `keep` (and `protect`). Keys are
        <prefix><unix_timestamp>.zip, so newest = largest timestamp."""
        newest_first = sorted(keys, key=lambda k: int(k[len(self.key_prefix):-4]), reverse=True)
        return [k for k in newest_first[max(keep, 1):] if k != protect]


# Local backups are named "<save>_<YYYYmmdd_HHMMSS><suffix>" by every
# game's backup_local_save (a folder, or one file per suffix, e.g. Valheim's
# legacy .db + .fwl pair).
_BACKUP_NAME = re.compile(r"^(?P<base>.+)_(?P<stamp>\d{8}_\d{6})(?P<suffix>(\.[^_]*)?)$")


def prune_local_backups(backup_dir: Path, keep: int = 5) -> None:
    """Keeps the newest `keep` backups of each save in state/local_backups
    (one backup = every entry sharing a save name + timestamp) and deletes
    older ones. Anything not named like a backup is left alone. Never fatal."""
    try:
        if not backup_dir.is_dir():
            return
        groups: dict[str, dict[str, list[Path]]] = {}
        for entry in backup_dir.iterdir():
            m = _BACKUP_NAME.match(entry.name)
            if m:
                groups.setdefault(m["base"], {}).setdefault(m["stamp"], []).append(entry)

        removed = 0
        for stamps in groups.values():
            for stamp in sorted(stamps, reverse=True)[max(keep, 1):]:
                for entry in stamps[stamp]:
                    if entry.is_dir():
                        shutil.rmtree(entry)
                    else:
                        entry.unlink()
                removed += 1
        if removed:
            log.info("Removed %d old local backup(s), kept the newest %d per save.", removed, keep)
    except Exception:
        log.exception("Failed to prune old local backups (non-fatal, continuing).")


class LocalSaveRecord:
    """Tracks which cloud save key this machine last successfully synced to,
    per game (so switching the active game doesn't confuse version records).
    Deliberately strict: anything that doesn't look like a real key for this
    game's prefix is treated as unknown, forcing a fresh download from the
    coordinator's authoritative save_key rather than trusting stale/edited
    local state."""

    def __init__(self, app_dir: Path, game_id: str, key_prefix: str, slot_id: str | None = None):
        # slot_id=None reproduces the original filename exactly, so the
        # default save's tracking file (and every pre-multi-save
        # installation's existing file) is untouched -- only a non-default
        # slot gets a suffixed filename of its own.
        suffix = f"_{slot_id}" if slot_id else ""
        self.path = app_dir / f"local_version_{game_id}{suffix}.txt"
        self.pattern = re.compile(rf"^{re.escape(key_prefix)}\d+\.zip$")

    def read(self) -> str | None:
        if not self.path.exists():
            return None
        content = self.path.read_text().strip()
        if self.pattern.match(content):
            return content
        return None

    def write(self, key: str):
        self.path.write_text(key)


class LocalContentHash:
    """Tracks a hash of the local save's actual content data (from
    GameAdapter.content_hash()) as of the last time it was known to match
    the cloud -- either just uploaded or just downloaded. Separate from
    LocalSaveRecord: that tracks WHICH cloud version string this machine
    last saw, not whether the local content has since drifted from it, so
    it can't answer "has anything actually changed since I last synced."
    Per game, same as LocalSaveRecord."""

    def __init__(self, app_dir: Path, game_id: str, slot_id: str | None = None):
        # slot_id=None reproduces the original filename exactly -- see
        # LocalSaveRecord.__init__'s comment above, same rationale.
        suffix = f"_{slot_id}" if slot_id else ""
        self.path = app_dir / f"local_content_hash_{game_id}{suffix}.txt"

    def read(self) -> str | None:
        if not self.path.exists():
            return None
        content = self.path.read_text().strip()
        return content or None

    def write(self, content_hash: str):
        self.path.write_text(content_hash)
