"""Pure computation for the Saves tab: resolving a save-slot's cloud key
from the coordinator's status blob, and building the full per-save row
list (local disk scan + cloud-only entries) a GameDetailPanel's Saves tab
renders. Deliberately takes an already-fetched status dict rather than
calling the coordinator itself, so the Saves tab never issues its own
coordinator round-trip -- it only ever consumes whatever
core.status_poller.StatusPoller's shared poll loop already fetched (the
same "don't hammer Cloudflare KV" discipline the rest of this project
already follows)."""

from dataclasses import dataclass
from datetime import datetime


def resolve_slot_cloud_key(status: dict, game_id: str, slot_id: str, own_prefix: str) -> str | None:
    """Finds this (game, slot)'s latest known cloud save key in a
    coordinator status blob, across every shape that blob might currently
    be in (see worker.js): the new per-slot nested map, a pre-multi-save
    flat per-game string, or (older still) the single global flat
    save_key -- adopting either flat form only if it actually matches
    this slot's OWN storage prefix, so a key that belongs to some other
    save (or some other game entirely) never gets misattributed."""
    raw = (status.get("save_keys") or {}).get(game_id)
    if isinstance(raw, dict):
        return raw.get(slot_id) or None
    if isinstance(raw, str) and raw.startswith(own_prefix):
        return raw
    legacy_flat_key = status.get("save_key")
    if legacy_flat_key and legacy_flat_key.startswith(own_prefix):
        return legacy_flat_key
    return None


def resolve_slot_owner(status: dict, game_id: str, slot_id: str) -> str | None:
    owners = (status.get("save_owners") or {}).get(game_id)
    return owners.get(slot_id) if isinstance(owners, dict) else None


def resolve_slot_display_name(status: dict, game_id: str, slot_id: str) -> str | None:
    names = (status.get("save_display_names") or {}).get(game_id)
    return names.get(slot_id) if isinstance(names, dict) else None


@dataclass
class SaveRow:
    save_name: str
    slot_id: str
    exists_locally: bool
    local_modified: datetime | None
    local_size_bytes: int | None
    cloud_key: str | None
    owner: str | None
    status: str  # "in_sync" | "cloud_has_changes" | "local_only" | "cloud_only"


def compute_save_rows(controller, status: dict) -> list["SaveRow"]:
    """Builds one row per save-slot known either locally (on this
    machine's disk) or in the cloud (the coordinator's per-slot maps for
    this game), for the Saves tab table."""
    adapter = controller.adapter
    game_id = adapter.game_id

    cloud_slots = (status.get("save_keys") or {}).get(game_id)
    cloud_slots = cloud_slots if isinstance(cloud_slots, dict) else {}

    rows: dict[str, SaveRow] = {}

    for save_name in adapter.list_local_saves():
        slot_id = adapter.slot_id_for(save_name)
        own_prefix = adapter.save_key_prefix_for(save_name)
        cloud_key = resolve_slot_cloud_key(status, game_id, slot_id, own_prefix)
        owner = resolve_slot_owner(status, game_id, slot_id)
        _, local_record, _, _ = controller._resources_for(save_name)
        tracked_key = local_record.read()
        modified, size = adapter.save_stat(save_name)

        if not cloud_key:
            row_status = "local_only"
        elif tracked_key == cloud_key:
            row_status = "in_sync"
        else:
            row_status = "cloud_has_changes"

        rows[slot_id] = SaveRow(save_name, slot_id, True, modified, size, cloud_key, owner, row_status)

    for slot_id, cloud_key in cloud_slots.items():
        if slot_id in rows or not cloud_key:
            continue
        display_name = resolve_slot_display_name(status, game_id, slot_id) or slot_id
        owner = resolve_slot_owner(status, game_id, slot_id)
        rows[slot_id] = SaveRow(display_name, slot_id, False, None, None, cloud_key, owner, "cloud_only")

    return sorted(rows.values(), key=lambda r: r.save_name.lower())
