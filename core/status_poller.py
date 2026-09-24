"""Polls the coordinator for "who's hosting what" on one shared loop, since
every configured game now gets its own SessionController simultaneously --
having each one poll and self-heal independently would mean duplicate
coordinator calls, duplicate self-heal races, and duplicate desktop
notifications for the exact same global claim. This replaces what used to
be SessionController.run_loop() back when only one game's controller ran
at a time."""

import logging
import threading
import time

import requests

log = logging.getLogger("moonberry-sync")

# How long an early-triggered poll (see trigger_poll) waits before actually
# hitting the coordinator, once woken -- same reasoning and same duration
# as the stale-claim self-heal's own post-release pause below: Cloudflare
# KV is only eventually consistent, so a read immediately after a write
# (ours or anyone else's, and a trigger fires right after one of ours) can
# still come back showing the pre-write state.
EARLY_POLL_SETTLE_SECONDS = 3


class StatusPoller:
    def __init__(self, coordinator, game_controllers: dict, player_name: str, update_checker, poll_interval_seconds: float):
        self.coordinator = coordinator
        self.game_controllers = game_controllers
        self.player_name = player_name
        self.update_checker = update_checker
        self.poll_interval_seconds = poll_interval_seconds
        self._poll_now = threading.Event()

    def trigger_poll(self) -> None:
        """Wakes the poll loop early instead of waiting out the rest of
        poll_interval_seconds -- for callers that just changed something
        coordinator-visible themselves (a host claim released) and want
        the rest of the app to notice sooner than the next scheduled
        tick. Safe to call from any thread. The loop still waits
        EARLY_POLL_SETTLE_SECONDS before actually polling (see above),
        and the NEXT scheduled tick is a full poll_interval_seconds after
        THIS poll, not after whenever the original tick would have
        landed -- an early wake resets the timer, it doesn't just
        squeeze in an extra poll."""
        self._poll_now.set()

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
                    and not controller.is_hosting
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
                    # A short pause, not an immediate re-check: the
                    # coordinator's backing store (Cloudflare KV) is only
                    # eventually consistent, confirmed in practice -- a read
                    # right after this release can still come back showing
                    # the old "hosting: true" state, which would otherwise
                    # trigger this exact branch again instantly, repeatedly,
                    # until the write actually propagates (observed as a
                    # burst of several duplicate "auto-releasing" log lines
                    # within under a second).
                    time.sleep(3)
                    continue

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

            woke_early = self._poll_now.wait(timeout=self.poll_interval_seconds)
            self._poll_now.clear()
            if woke_early:
                time.sleep(EARLY_POLL_SETTLE_SECONDS)
