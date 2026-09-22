"""Polls the coordinator for "who's hosting what" on one shared loop, since
every configured game now gets its own SessionController simultaneously --
having each one poll and self-heal independently would mean duplicate
coordinator calls, duplicate self-heal races, and duplicate desktop
notifications for the exact same global claim. This replaces what used to
be SessionController.run_loop() back when only one game's controller ran
at a time."""

import logging
import time

import requests

log = logging.getLogger("moonberry-sync")


class StatusPoller:
    def __init__(self, coordinator, game_controllers: dict, player_name: str, update_checker, poll_interval_seconds: float):
        self.coordinator = coordinator
        self.game_controllers = game_controllers
        self.player_name = player_name
        self.update_checker = update_checker
        self.poll_interval_seconds = poll_interval_seconds

    def run_loop(self, on_status=None, notify_desktop=None, on_update_available=None):
        """on_status, if given, is called every poll with the raw status
        dict from the coordinator (hosting, host_name, game_id, join_code,
        ...) so the GUI can refresh every game's row from one poll instead
        of one per game.

        on_update_available, as before: called every idle poll with the
        currently known release info dict, or None."""
        last_notified = None  # (host_name, game_id) already notified about,
        # so a notification fires once per session start, not every poll
        # cycle while that person keeps hosting.

        while True:
            try:
                status = self.coordinator.get_status()
                game_id = status.get("game_id")
                controller = self.game_controllers.get(game_id)

                if (
                    status.get("hosting")
                    and status.get("host_name") == self.player_name
                    and controller is not None
                    and not controller.adapter.is_running()
                    and not controller._host_now_pending
                ):
                    # Our own claim, but that game isn't actually running on
                    # this machine, AND no Host Now call in this process is
                    # actively managing it (that flag covers the legitimate
                    # wait-for-you-to-start-the-game window, which can take
                    # minutes and looks identical to this otherwise) -- a
                    # previous run crashed, was force-closed, or the PC
                    # slept/lost power before it could release the claim.
                    log.warning(
                        "Found a stale host claim from a previous run (no %s "
                        "process is actually running). Auto-releasing it.",
                        controller.adapter.display_name,
                    )
                    self.coordinator.release_host()
                    last_notified = None
                    continue  # loop back around immediately to re-check status fresh

                if status.get("hosting"):
                    host_name = status.get("host_name")
                    join_code = status.get("join_code")
                    display_name = controller.adapter.display_name if controller else (game_id or "an unknown game")
                    code_part = f" (Join Code: {join_code})" if join_code else ""
                    log.info("%s is hosting %s — join now%s", host_name, display_name, code_part)

                    key = (host_name, game_id)
                    if key != last_notified and host_name != self.player_name and notify_desktop:
                        notify_desktop(
                            "Moonberry Save-Sync",
                            f"{host_name} started hosting {display_name} — open the game to join!{code_part}",
                        )
                    last_notified = key
                else:
                    last_notified = None
                    log.info("No one hosting.")
                    self.update_checker.check()  # refreshes the throttled cache; message itself isn't needed here
                    if on_update_available:
                        on_update_available(self.update_checker.latest_release_info())

                if on_status:
                    on_status(status)

            except requests.RequestException as e:
                log.error("Coordinator unreachable: %s", e)
                if on_status:
                    on_status(None)

            time.sleep(self.poll_interval_seconds)
