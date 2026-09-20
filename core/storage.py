"""S3-compatible cloud storage (R2, B2, etc) plus the local bookkeeping of
which save version this machine last synced to. Game-agnostic: every key is
namespaced by a per-game prefix (GameAdapter.save_key_prefix) so multiple
games can share one bucket without colliding."""

import logging
import re
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
        self.legacy_key = cfg.get("storage_object_key", cfg.get("r2_object_key", "world_save.zip"))
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

    def prune_old_saves(self, keep: int = 5):
        """Keeps cloud storage from growing forever now that every session
        creates a new file. Keeps the most recent `keep` save files for this
        game and deletes older ones. Never fatal."""
        try:
            client = self._client()
            paginator = client.get_paginator("list_objects_v2")
            all_saves = []
            for page in paginator.paginate(Bucket=self.bucket, Prefix=self.key_prefix):
                for obj in page.get("Contents", []):
                    all_saves.append(obj["Key"])

            if len(all_saves) <= keep:
                return

            # Keys are named <prefix><unix_timestamp>.zip, so a plain string
            # sort works correctly for chronological order too.
            all_saves.sort(reverse=True)
            to_delete = all_saves[keep:]
            for key in to_delete:
                client.delete_object(Bucket=self.bucket, Key=key)
            log.info("Pruned %d old save version(s), kept the most recent %d.", len(to_delete), keep)
        except Exception:
            log.exception("Failed to prune old save versions (non-fatal, continuing).")


class LocalSaveRecord:
    """Tracks which cloud save key this machine last successfully synced to,
    per game (so switching the active game doesn't confuse version records).
    Deliberately strict: anything that doesn't look like a real key for this
    game's prefix is treated as unknown, forcing a fresh download from the
    coordinator's authoritative save_key rather than trusting stale/edited
    local state."""

    def __init__(self, app_dir: Path, game_id: str, key_prefix: str):
        self.path = app_dir / f"local_version_{game_id}.txt"
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
