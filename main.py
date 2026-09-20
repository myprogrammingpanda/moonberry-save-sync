"""
Moonberry Save-Sync
--------------------
Desktop app (Tkinter GUI) that syncs game saves between friends:

  1. Ask the coordinator (a Cloudflare Worker) whether anyone is currently
     hosting.
       - If yes: show who, so you can join them.
       - If no: claim the host slot, download the latest save from cloud
         storage, and launch the game for you.
  2. While you're hosting, watch the game process.
  3. When the game closes, if you were the host: zip up the save, upload it
     to cloud storage, and release the host claim.

Game-specific behavior (save format, launch command, join-code parsing)
lives behind the GameAdapter interface in games/ -- this file and core/ know
nothing about any particular game. All actions are written to sync.log next
to this script.
"""

import logging
import sys
from pathlib import Path

from core.config import load_config
from core.coordinator import Coordinator
from core.notifications import DiscordNotifier, UpdateChecker
from core.player import get_player_name
from core.session import SessionController
from core.storage import LocalSaveRecord, SaveStorage
from games._discovery import discover_adapters
from gui.app import App

APP_VERSION = "1.1.0"

APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "config.json"
LOG_PATH = APP_DIR / "sync.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("moonberry-sync")


def main():
    adapters = discover_adapters()

    if not CONFIG_PATH.exists():
        from gui.settings import run_setup_wizard

        if not run_setup_wizard(CONFIG_PATH, adapters):
            sys.exit(0)  # user closed the setup wizard without saving

    cfg = load_config(CONFIG_PATH)

    active_game = cfg.get("active_game")
    if not active_game or active_game not in adapters:
        log.error(
            "config.json's \"active_game\" (%r) doesn't match any game found "
            "in games/ (%s).",
            active_game,
            ", ".join(sorted(adapters)) or "none found",
        )
        sys.exit(1)

    adapter_cls = adapters[active_game]
    game_cfg = cfg.get("games", {}).get(active_game, {})
    adapter = adapter_cls(game_cfg)

    player_name = get_player_name(APP_DIR, cfg)

    coordinator = Coordinator(cfg["worker_url"], cfg["worker_secret"], player_name)
    storage = SaveStorage(cfg, key_prefix=adapter.save_key_prefix)
    local_record = LocalSaveRecord(APP_DIR, adapter.game_id, adapter.save_key_prefix)
    notifier = DiscordNotifier(cfg.get("moonberry_url"), cfg.get("moonberry_secret"))
    update_checker = UpdateChecker(cfg.get("github_repo"), APP_VERSION)

    controller = SessionController(
        app_dir=APP_DIR,
        adapter=adapter,
        coordinator=coordinator,
        storage=storage,
        local_record=local_record,
        notifier=notifier,
        update_checker=update_checker,
        player_name=player_name,
        poll_interval_seconds=cfg["poll_interval_seconds"],
        max_saved_versions=cfg.get("max_saved_versions", 5),
    )

    App(controller, APP_VERSION).run()


if __name__ == "__main__":
    main()
