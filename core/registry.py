"""Builds one SessionController per configured game -- has a games/<id>.py
adapter and a config.json["games"][id] section. All controllers share a
single Coordinator (claim/release are global across games -- only one
person can be hosting anything at a time), but each gets its own
SaveStorage/LocalSaveRecord (already namespaced by game_id). Every
configured game gets a controller regardless of whether a local save exists
yet -- has_local_save() is purely a status signal for the GUI ("Not set up"
vs "Idle"), not a gate: Host Now already handles downloading a save fresh on
a first claim."""

from pathlib import Path

from core.coordinator import Coordinator
from core.notifications import DiscordNotifier
from core.session import SessionController
from core.storage import LocalContentHash, LocalSaveRecord, SaveStorage


def build_game_controllers(
    cfg: dict,
    adapters: dict[str, type],
    app_dir: Path,
    state_dir: Path,
    coordinator: Coordinator,
    notifier: DiscordNotifier,
    player_name: str,
) -> dict[str, SessionController]:
    """app_dir is the install root (where config.json lives -- some GUI
    code reads controller.app_dir / "config.json" directly). state_dir is
    where all of this app's own runtime files go instead (local save
    version/hash records, backups, temp zips) -- see main.py's STATE_DIR."""
    controllers: dict[str, SessionController] = {}

    for game_id, game_cfg in cfg.get("games", {}).items():
        adapter_cls = adapters.get(game_id)
        if not adapter_cls:
            continue

        adapter = adapter_cls(game_cfg)

        storage = SaveStorage(cfg, key_prefix=adapter.save_key_prefix)
        local_record = LocalSaveRecord(state_dir, adapter.game_id, adapter.save_key_prefix)
        local_content_hash = LocalContentHash(state_dir, adapter.game_id)

        controllers[game_id] = SessionController(
            app_dir=app_dir,
            state_dir=state_dir,
            adapter=adapter,
            coordinator=coordinator,
            storage=storage,
            local_record=local_record,
            local_content_hash=local_content_hash,
            notifier=notifier,
            player_name=player_name,
            cfg=cfg,
            max_saved_versions=cfg.get("max_saved_versions", 5),
        )

    return controllers
