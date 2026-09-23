"""One-time (or repeatable, e.g. after a world/server rename) migration:
renames existing save objects in the bucket from an old save_key_prefix to
whatever each adapter's CURRENT save_key_prefix computes to, and updates
the coordinator's per-game save_keys record to point at the renamed
"latest" key -- so nothing has to redundantly re-download afterward.

Usage (run from the repo root, with config.json filled in):
    python scripts/migrate_save_key_prefix.py            # dry run -- lists what WOULD happen, changes nothing
    python scripts/migrate_save_key_prefix.py --apply     # actually renames the bucket objects + updates the coordinator

OLD_PREFIXES below is "what the prefix used to be before this migration" --
edit it if this is ever run again for a different rename (e.g. after
someone renames their Valheim world or Zomboid server).

Note: briefly claims the host slot for each game being migrated (to have a
safe way to write the coordinator's per-game save_key -- claim/release is
the only write path the coordinator exposes) and releases it immediately
after. Anyone polling status during that instant could see a flash of
"someone is hosting" for that game -- harmless, but worth knowing if this
runs while others might be online.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config import load_config
from core.coordinator import Coordinator
from core.storage import SaveStorage
from games._discovery import discover_adapters

APP_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = APP_DIR / "config.json"
STATE_DIR = APP_DIR / "state"

OLD_PREFIXES = {
    "valheim": "world_save_",
    "zomboid": "zomboid_save_",
}


def migrate_game(game_id: str, adapter, cfg: dict, coordinator: Coordinator, apply: bool) -> None:
    old_prefix = OLD_PREFIXES.get(game_id)
    if not old_prefix:
        print(f"[{game_id}] no old prefix registered in OLD_PREFIXES, skipping.")
        return

    new_prefix = adapter.save_key_prefix
    if new_prefix == old_prefix:
        print(f"[{game_id}] prefix unchanged ('{new_prefix}'), nothing to migrate.")
        return

    storage = SaveStorage(cfg, key_prefix=old_prefix)
    client = storage._client()

    old_keys = []
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=storage.bucket, Prefix=old_prefix):
        for obj in page.get("Contents", []):
            old_keys.append(obj["Key"])

    if not old_keys:
        print(f"[{game_id}] no objects found under old prefix '{old_prefix}', nothing to migrate.")
        return

    renames = []
    for old_key in sorted(old_keys):  # string-sortable timestamp suffixes -- sorted() == chronological
        suffix = old_key[len(old_prefix):]
        renames.append((old_key, f"{new_prefix}{suffix}"))

    print(f"[{game_id}] {len(renames)} object(s) under '{old_prefix}' -> '{new_prefix}':")
    for old_key, new_key in renames:
        print(f"    {old_key}  ->  {new_key}")

    if not apply:
        print(f"[{game_id}] dry run only -- pass --apply to actually rename these and update the coordinator.\n")
        return

    for old_key, new_key in renames:
        client.copy_object(Bucket=storage.bucket, CopySource={"Bucket": storage.bucket, "Key": old_key}, Key=new_key)
        client.head_object(Bucket=storage.bucket, Key=new_key)  # confirm the copy landed before deleting the original
        client.delete_object(Bucket=storage.bucket, Key=old_key)
    print(f"[{game_id}] renamed all {len(renames)} object(s).")

    latest_new_key = renames[-1][1]

    slot_id = adapter.slot_id_for(adapter.default_save_name)
    claim = coordinator.claim_host(game_id, slot_id, adapter.default_save_name)
    if not claim.get("ok"):
        print(
            f"[{game_id}] WARNING: could not claim to update the coordinator's record "
            f"(someone's currently hosting?) -- objects were renamed, but the coordinator "
            f"still points at the OLD key. Re-run once nobody's hosting."
        )
        return
    coordinator.release_host(save_key=latest_new_key)
    print(f"[{game_id}] coordinator's save_keys['{game_id}'] updated to '{latest_new_key}'.")

    STATE_DIR.mkdir(exist_ok=True)
    local_record_path = STATE_DIR / f"local_version_{game_id}.txt"
    local_record_path.write_text(latest_new_key)
    print(f"[{game_id}] local_version_{game_id}.txt updated to '{latest_new_key}' (avoids a redundant re-download).\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="Actually perform the migration (default: dry run).")
    args = parser.parse_args()

    cfg = load_config(CONFIG_PATH)
    adapters = discover_adapters()
    coordinator = Coordinator(cfg["worker_url"], cfg["worker_secret"], cfg["player_name"])

    for game_id, game_cfg in cfg.get("games", {}).items():
        adapter_cls = adapters.get(game_id)
        if not adapter_cls:
            continue
        migrate_game(game_id, adapter_cls(game_cfg), cfg, coordinator, apply=args.apply)


if __name__ == "__main__":
    main()
