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


def cloud_key_uploaded_at(cloud_key: str | None, own_prefix: str) -> datetime | None:
    """Cloud save keys are minted as f"{prefix}{int(time.time())}.zip"
    (see SaveStorage.new_save_key) -- the upload time is embedded right
    in the key, so no extra coordinator/storage call is needed to know
    roughly when a save's cloud copy was last updated. Used purely as an
    advisory hint (e.g. in Sync's confirmation dialog) so you can compare
    it against your local save's own modified time before deciding to
    overwrite -- never to auto-decide anything: file mtimes and upload
    times are both weak proxies for actual game progress (a mtime can be
    touched by unrelated tools, an upload can happen after barely
    playing), and with a save multiple people touch, "which is newer"
    isn't even guaranteed to mean "which has more progress" if the two
    genuinely diverged. Returns None if the key doesn't look like one of
    ours or doesn't parse."""
    if not cloud_key or not cloud_key.startswith(own_prefix) or not cloud_key.endswith(".zip"):
        return None
    timestamp_str = cloud_key[len(own_prefix) : -len(".zip")]
    if not timestamp_str.isdigit():
        return None
    try:
        return datetime.fromtimestamp(int(timestamp_str))
    except (OSError, OverflowError, ValueError):
        return None


def resolve_slot_hosting_by(status: dict, game_id: str, slot_id: str) -> str | None:
    """The player name currently hosting THIS save, or None if nobody is
    (or someone's hosting a different game/save -- the coordinator's
    claim is global, but only ever names one specific game+slot at a
    time). save_slot is only present on claims from a client running
    this multi-save-aware version or later; an older client's claim has
    no save_slot at all, so it can never match a specific slot here --
    the sidebar/Overview status text (which only cares about game_id,
    not slot) still shows "someone is hosting" correctly either way,
    this is purely the Saves tab's per-row detail."""
    if not status.get("hosting") or status.get("game_id") != game_id:
        return None
    if status.get("save_slot") != slot_id:
        return None
    return status.get("host_name")


@dataclass
class SaveRow:
    save_name: str
    slot_id: str
    exists_locally: bool
    local_modified: datetime | None
    local_size_bytes: int | None
    cloud_key: str | None
    cloud_modified: datetime | None
    owner: str | None
    status: str  # "in_sync" | "cloud_has_changes" | "local_only" | "cloud_only"
    hosting_by: str | None  # player name currently hosting this exact save, or None


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
        cloud_modified = cloud_key_uploaded_at(cloud_key, own_prefix)

        if not cloud_key:
            row_status = "local_only"
        elif tracked_key == cloud_key:
            row_status = "in_sync"
        else:
            row_status = "cloud_has_changes"

        hosting_by = resolve_slot_hosting_by(status, game_id, slot_id)
        rows[slot_id] = SaveRow(
            save_name, slot_id, True, modified, size, cloud_key, cloud_modified, owner, row_status, hosting_by
        )

    for slot_id, cloud_key in cloud_slots.items():
        if slot_id in rows or not cloud_key:
            continue
        display_name = resolve_slot_display_name(status, game_id, slot_id) or slot_id
        owner = resolve_slot_owner(status, game_id, slot_id)
        own_prefix = adapter.save_key_prefix_for(display_name)
        cloud_modified = cloud_key_uploaded_at(cloud_key, own_prefix)
        hosting_by = resolve_slot_hosting_by(status, game_id, slot_id)
        rows[slot_id] = SaveRow(
            display_name, slot_id, False, None, None, cloud_key, cloud_modified, owner, "cloud_only", hosting_by
        )

    return sorted(rows.values(), key=lambda r: r.save_name.lower())
