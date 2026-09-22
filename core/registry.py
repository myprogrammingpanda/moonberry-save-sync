"""Builds one SessionController per "supported" game -- has a games/<id>.py
adapter, a config.json["games"][id] section, and a save that actually exists
on disk. All controllers share a single Coordinator (claim/release are
global across games), but each gets its own SaveStorage/LocalSaveRecord
(already namespaced by game_id). Only the active game's controller runs its
poll loop; the rest just sit ready for Manual Sync."""

import logging
from pathlib import Path

from core.coordinator import Coordinator
from core.notifications import DiscordNotifier, UpdateChecker
from core.session import SessionController
from core.storage import LocalContentHash, LocalSaveRecord, SaveStorage

log = logging.getLogger("moonberry-sync")


def build_game_controllers(
    cfg: dict,
    adapters: dict[str, type],
    app_dir: Path,
    coordinator: Coordinator,
    notifier: DiscordNotifier,
    update_checker: UpdateChecker,
    player_name: str,
    active_game_id: str,
) -> dict[str, SessionController]:
    controllers: dict[str, SessionController] = {}

    for game_id, game_cfg in cfg.get("games", {}).items():
        adapter_cls = adapters.get(game_id)
        if not adapter_cls:
            continue

        adapter = adapter_cls(game_cfg)

        # The active game always gets a controller, even with no local save
        # yet -- Host Now downloads the save fresh on a first claim, same
        # as before multi-game support existed. Other games are only worth
        # a Manual Sync row if there's actually something on disk to sync.
        if game_id != active_game_id and not adapter.has_local_save():
            log.info(
                "Skipping '%s' for Manual Sync -- no local save found on disk yet.",
                game_id,
            )
            continue

        storage = SaveStorage(cfg, key_prefix=adapter.save_key_prefix)
        local_record = LocalSaveRecord(app_dir, adapter.game_id, adapter.save_key_prefix)
        local_content_hash = LocalContentHash(app_dir, adapter.game_id)

        controllers[game_id] = SessionController(
            app_dir=app_dir,
            adapter=adapter,
            coordinator=coordinator,
            storage=storage,
            local_record=local_record,
            local_content_hash=local_content_hash,
            notifier=notifier,
            update_checker=update_checker,
            player_name=player_name,
            poll_interval_seconds=cfg["poll_interval_seconds"],
            max_saved_versions=cfg.get("max_saved_versions", 5),
        )

    return controllers
