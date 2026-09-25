"""Polls the coordinator for "who's hosting what" on one shared loop, since
every configured game now gets its own SessionController simultaneously --
having each one poll and self-heal independently would mean duplicate
coordinator calls, duplicate self-heal races, and duplicate desktop
notifications for the exact same claims. This replaces what used to
be SessionController.run_loop() back when only one game's controller ran
at a time."""

import logging
import threading
import time

import requests

from core.host_status import hosts_by_game

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

    def _release_stale_claims(self, hosts: dict) -> bool:
        """Releases every claim held under this player's name for a game
        that isn't actually running on this machine, and that no Host Now
        call in this process is actively managing (is_hosting covers the
        legitimate wait-for-you-to-start-the-game window, which can take
        minutes and looks identical to this otherwise) -- a previous run
        crashed, was force-closed, or the PC slept/lost power before it
        could release. True if any were released."""
        released = False
        for game_id, host in hosts.items():
            controller = self.game_controllers.get(game_id)
            if (
                host.get("host_name") == self.player_name
                and controller is not None
                and not controller.adapter.is_running()
                and not controller.is_hosting
            ):
                log.warning(
                    "Found a stale host claim from a previous run (no %s "
                    "process is actually running). Auto-releasing it.",
                    controller.adapter.display_name,
                )
                self.coordinator.release_host(game_id)
                released = True
        return released

    def run_loop(self, on_status=None, notify_desktop=None, on_update_available=None):
        """on_status, if given, is called every poll with the raw status
        dict from the coordinator (hosting, host_name, game_id, join_code,
        ...) so the GUI can refresh every game's row from one poll instead
        of one per game.

        on_update_available: called every poll while this player isn't
        hosting anything, with the currently known release info dict, or
        None."""
        notified = set()  # (game_id, host_name, since) already notified
        # about, so a notification fires once per session start, not every
        # poll cycle while that person keeps hosting.

        while True:
            try:
                status = self.coordinator.get_status()
                hosts = hosts_by_game(status)

                if self._release_stale_claims(hosts):
                    # A short pause, not an immediate re-check: against the
                    # older KV-backed coordinator, a read right after this
                    # release can still come back showing the old claim
                    # (eventual consistency, confirmed in practice), which
                    # would trigger this exact branch again instantly.
                    time.sleep(3)
                    continue

                for game_id, host in sorted(hosts.items()):
                    controller = self.game_controllers.get(game_id)
                    host_name = host.get("host_name")
                    join_code = host.get("join_code")
                    display_name = controller.adapter.display_name if controller else game_id
                    code_part = f" (Join Code: {join_code})" if join_code else ""
                    log.info("%s is hosting %s — join now%s", host_name, display_name, code_part)

                    key = (game_id, host_name, host.get("since"))
                    if key not in notified and host_name != self.player_name and notify_desktop:
                        notify_desktop(
                            "Moonberry Save-Sync",
                            f"{host_name} started hosting {display_name} — open the game to join!{code_part}",
                        )
                    notified.add(key)
                # Forget sessions that have ended, so the same person
                # hosting that game again later is announced again.
                live = {(g, h.get("host_name"), h.get("since")) for g, h in hosts.items()}
                notified &= live

                if not hosts:
                    log.info("No one hosting.")
                # Only offered while you're not hosting yourself -- others
                # hosting other games no longer holds it back.
                if not any(c.is_hosting for c in self.game_controllers.values()):
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
