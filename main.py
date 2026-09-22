"""
Moonberry Save-Sync
--------------------
Desktop app (PySide6/Qt GUI) that syncs game saves between friends:

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

import ctypes
import logging
import sys
import traceback
from pathlib import Path

APP_VERSION = "1.4.2"

APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "config.json"
LOG_PATH = APP_DIR / "sync.log"

# Only these two stdlib-only lines run before logging exists -- everything
# else (including anything that touches PySide6) is deferred into main(),
# so any failure there -- e.g. a dependency that wasn't (re)installed --
# still lands in sync.log instead of vanishing before logging is even set
# up. sys.stdout is None under pythonw.exe (no console attached), and
# StreamHandler(None) would blow up the very first log call, so the
# console mirror is only added when a real stdout actually exists.
_handlers = [logging.FileHandler(LOG_PATH, encoding="utf-8")]
if sys.stdout:
    _handlers.append(logging.StreamHandler(sys.stdout))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=_handlers,
)
log = logging.getLogger("moonberry-sync")


_INSTANCE_MUTEX_NAME = "MoonberrySaveSync_SingleInstanceMutex"
_WINDOW_TITLE_PREFIX = "Moonberry Save-Sync"


def _acquire_single_instance_lock() -> bool:
    """True if this is the only running instance. Two instances would
    share the same app_dir -- same temp zip paths, same local version
    record, same actual save folder on disk -- and SessionController's
    _sync_lock only guards against races within one process, so it can't
    protect against a second, completely separate process doing the same
    thing at the same time. Windows-only; fails open (treats it as the
    only instance) on any other OS or if anything here goes wrong, rather
    than block a legitimate launch over a non-essential guard."""
    if sys.platform != "win32":
        return True
    try:
        ERROR_ALREADY_EXISTS = 183
        ctypes.windll.kernel32.CreateMutexW(None, False, _INSTANCE_MUTEX_NAME)
        return ctypes.windll.kernel32.GetLastError() != ERROR_ALREADY_EXISTS
    except Exception:
        return True


def _focus_existing_window() -> None:
    """Brings the other instance's window to the front instead of just
    refusing to open a second one. Matches by title *prefix* rather than
    the exact title, since the real title varies by active game (e.g.
    "Moonberry Save-Sync — Valheim")."""
    try:
        user32 = ctypes.windll.user32
        found = []

        @ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
        def _enum_proc(hwnd, _lparam):
            length = user32.GetWindowTextLengthW(hwnd)
            if length and user32.IsWindowVisible(hwnd):
                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buf, length + 1)
                if buf.value.startswith(_WINDOW_TITLE_PREFIX):
                    found.append(hwnd)
                    return False  # stop enumerating, we found it
            return True

        user32.EnumWindows(_enum_proc, 0)
        if found:
            SW_RESTORE = 9
            user32.ShowWindow(found[0], SW_RESTORE)
            user32.SetForegroundWindow(found[0])
    except Exception:
        pass  # best-effort -- worst case it just doesn't focus


def _show_fatal_error(exc: BaseException) -> None:
    """Last-resort, dependency-free error surface for anything that goes
    wrong during startup -- most likely a missing/not-reinstalled
    dependency (e.g. PySide6 after an update). Can't rely on Qt for the
    dialog since the crash might BE Qt failing to import. Logging already
    works at this point regardless of what failed, since it's the only
    thing set up before any risky import is attempted."""
    log.critical("Fatal error during startup:\n%s", traceback.format_exc())
    if sys.platform == "win32":
        try:
            ctypes.windll.user32.MessageBoxW(
                None,
                f"Moonberry Save-Sync failed to start:\n\n{exc}\n\n"
                f"See {LOG_PATH.name} (next to the app) for the full details.",
                "Moonberry Save-Sync — Error",
                0x10,  # MB_ICONERROR
            )
        except Exception:
            pass


def main():
    if not _acquire_single_instance_lock():
        log.info("Another instance is already running -- focusing it instead of opening a new one.")
        _focus_existing_window()
        return

    from core.config import load_config
    from core.coordinator import Coordinator
    from core.notifications import DiscordNotifier, UpdateChecker
    from core.player import get_player_name
    from core.registry import build_game_controllers
    from games._discovery import discover_adapters
    from gui.app import App

    adapters = discover_adapters()

    if not CONFIG_PATH.exists():
        from gui.settings import run_setup_wizard

        if not run_setup_wizard(CONFIG_PATH, adapters):
            sys.exit(0)  # user closed the setup wizard without saving

    cfg = load_config(CONFIG_PATH)

    configured_games = sorted(cfg.get("games", {}))
    if not configured_games:
        log.error("config.json has no \"games\" configured at all -- nothing to show.")
        sys.exit(1)

    # active_game is now just "which sidebar item is selected by default" --
    # every configured game gets its own controller and its own dashboard
    # entry regardless, so a missing/stale value is worth a warning, not a
    # fatal error.
    active_game = cfg.get("active_game")
    if not active_game or active_game not in adapters or active_game not in configured_games:
        fallback = configured_games[0]
        log.warning(
            "config.json's \"active_game\" (%r) doesn't match a configured game (%s) -- "
            "defaulting the dashboard's initial selection to '%s'.",
            active_game,
            ", ".join(configured_games),
            fallback,
        )
        active_game = fallback

    player_name = get_player_name(APP_DIR, cfg)

    coordinator = Coordinator(cfg["worker_url"], cfg["worker_secret"], player_name)
    notifier = DiscordNotifier(cfg.get("moonberry_url"), cfg.get("moonberry_secret"))
    update_checker = UpdateChecker(cfg.get("github_repo"), APP_VERSION)

    game_controllers = build_game_controllers(cfg, adapters, APP_DIR, coordinator, notifier, player_name)

    App(
        game_controllers,
        active_game,
        APP_VERSION,
        app_dir=APP_DIR,
        coordinator=coordinator,
        player_name=player_name,
        update_checker=update_checker,
        poll_interval_seconds=cfg["poll_interval_seconds"],
    ).run()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        _show_fatal_error(exc)
        sys.exit(1)
