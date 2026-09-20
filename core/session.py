"""Game-agnostic host/sync state machine. Drives a GameAdapter through
claim -> sync -> launch -> watch -> zip -> upload -> release. This mirrors
the original single-file app's logic exactly, generalized to call through a
GameAdapter instead of being wired directly to one game."""

import logging
import threading
import time
from pathlib import Path

import requests

log = logging.getLogger("moonberry-sync")


class SessionController:
    def __init__(
        self,
        app_dir: Path,
        adapter,
        coordinator,
        storage,
        local_record,
        notifier,
        update_checker,
        player_name: str,
        poll_interval_seconds: float,
        max_saved_versions: int = 5,
    ):
        self.app_dir = app_dir
        self.adapter = adapter
        self.coordinator = coordinator
        self.storage = storage
        self.local_record = local_record
        self.notifier = notifier
        self.update_checker = update_checker
        self.player_name = player_name
        self.poll_interval_seconds = poll_interval_seconds
        self.max_saved_versions = max_saved_versions

        # Set by the GUI's "Play Now" button. The loop only attempts to
        # become host when this is set -- otherwise it just reports status.
        # Without this, the app would auto-relaunch the game every poll
        # cycle forever after a session ends, since "nobody's hosting" is
        # true right up until someone (usually you again) claims it.
        self.play_requested = threading.Event()
        # Auto-launch on the very first run only, so opening the app for
        # the first time still just starts playing with no extra click.
        # Every session after that requires an explicit "Play Now" click --
        # this is what prevents the app from auto-relaunching in a loop
        # after a session ends.
        self.play_requested.set()

    def sync_down_if_needed(self, cloud_save_key: str | None):
        local_save_key = self.local_record.read()

        # Fallback for the transition period right after upgrading to the
        # versioned-save scheme: if the coordinator doesn't have a save_key
        # yet, fall back to the old static filename so existing saves
        # aren't stranded.
        effective_cloud_key = cloud_save_key or self.storage.legacy_key

        if not cloud_save_key:
            log.info(
                "Coordinator has no versioned save_key yet -- falling back to "
                "legacy filename '%s' for this sync.",
                effective_cloud_key,
            )

        if effective_cloud_key != local_save_key:
            log.info(
                "Cloud save ('%s') differs from local record ('%s'). Downloading...",
                effective_cloud_key,
                local_save_key,
            )
            self.adapter.backup_local_save(self.app_dir / "local_backups")
            tmp_zip = self.app_dir / "_incoming_save.zip"
            if self.storage.download_save(tmp_zip, effective_cloud_key):
                self.adapter.unzip_save(tmp_zip)
                tmp_zip.unlink(missing_ok=True)
                self.local_record.write(effective_cloud_key)
                log.info("Local save updated to '%s'.", effective_cloud_key)
        else:
            log.info("Local save is already up to date ('%s').", local_save_key)

    def become_host_and_play(self):
        log.info("No one is hosting. Attempting to claim host...")
        result = self.coordinator.claim_host()
        if not result.get("ok"):
            log.info("Someone beat us to it: %s", result.get("current"))
            return

        current = result["current"]

        # CRITICAL: everything from here on is wrapped in try/finally. If
        # ANYTHING goes wrong we MUST still release the host claim in the
        # finally block below, or the coordinator is stuck saying "X is
        # hosting" forever with no way to recover except manually resetting
        # it.
        uploaded_successfully = False
        uploaded_key = None
        try:
            self.sync_down_if_needed(current.get("save_key"))

            self.adapter.launch()
            if not self.adapter.wait_for_start():
                log.warning("%s didn't seem to start within the timeout.", self.adapter.display_name)
                return  # falls through to finally, which releases the claim

            log.info(
                "You're hosting as '%s'. Friends can join you. This app "
                "will auto-sync the save when you close the game.",
                self.player_name,
            )

            log.info("Watching for a join code to share...")
            join_code = self.adapter.scrape_join_code()

            if join_code:
                self.coordinator.announce_join_code(join_code)
                log.info("Shared join code '%s' with the group.", join_code)
            else:
                log.info(
                    "No join code was found before the session ended (or this "
                    "game doesn't use them) -- that's fine."
                )

            self.notifier.notify(self.adapter.game_id, self.player_name, join_code, event="started")

            self.adapter.wait_for_exit()
            log.info("%s has closed.", self.adapter.display_name)

            out_zip = self.app_dir / "_outgoing_save.zip"

            zip_start = time.time()
            log.info("Zipping save...")
            self.adapter.zip_save(out_zip)
            zip_seconds = time.time() - zip_start
            zip_size_mb = out_zip.stat().st_size / (1024 * 1024)
            log.info("Zip complete: %.1f MB in %.1fs.", zip_size_mb, zip_seconds)

            uploaded_key = self.storage.new_save_key()
            upload_start = time.time()
            log.info("Uploading save as '%s'...", uploaded_key)
            self.storage.upload_save(out_zip, uploaded_key)
            upload_seconds = time.time() - upload_start
            log.info("Upload complete in %.1fs.", upload_seconds)
            out_zip.unlink(missing_ok=True)
            uploaded_successfully = True

        except Exception:
            log.exception(
                "Something went wrong while hosting. Releasing the host "
                "claim so no one else gets locked out."
            )

        finally:
            if uploaded_successfully:
                self.coordinator.release_host(save_key=uploaded_key)
                self.local_record.write(uploaded_key)
                log.info(
                    "Save uploaded ('%s') and host slot released. Thanks for playing!",
                    uploaded_key,
                )
                self.storage.prune_old_saves(keep=self.max_saved_versions)
                self.notifier.notify(self.adapter.game_id, self.player_name, event="ended")
            else:
                self.coordinator.release_host()
                log.info("Host slot released (no upload happened this time).")

    def run_loop(self, update_status_text=None, notify_desktop=None):
        last_notified_host = None  # tracks who we've already notified about,
        # so we only pop a notification ONCE per session start, not every
        # poll cycle while that person keeps hosting.
        is_first_check = True  # the auto-play-on-open behavior should only
        # ever apply to this very first check -- if someone else is already
        # hosting right now, that intent gets discarded rather than sitting
        # around waiting to fire the instant they stop.

        while True:
            try:
                status = self.coordinator.get_status()

                if (
                    status.get("hosting")
                    and status.get("host_name") == self.player_name
                    and not self.adapter.is_running()
                ):
                    # This is OUR OWN claim, but the game isn't actually
                    # running on this machine -- a previous run crashed, was
                    # force-closed, or the PC slept/lost power before it
                    # could release the claim. Safe to auto-clear: if it
                    # were genuinely still hosting, is_running() would be
                    # True.
                    log.warning(
                        "Found a stale host claim from a previous run (no %s "
                        "process is actually running). Auto-releasing it.",
                        self.adapter.display_name,
                    )
                    self.coordinator.release_host()
                    last_notified_host = None
                    continue  # loop back around immediately to re-check status fresh

                if status.get("hosting"):
                    if is_first_check and self.play_requested.is_set():
                        # Someone else is already hosting on our very first
                        # check -- discard the auto-play intent instead of
                        # letting it linger until they eventually stop.
                        self.play_requested.clear()
                        log.info(
                            "Someone else is already hosting -- won't auto-play "
                            "later when they stop. Use 'Play Now' if you want to "
                            "host after them."
                        )

                    host_name = status.get("host_name")
                    join_code = status.get("join_code")
                    code_part = f" (Join Code: {join_code})" if join_code else ""
                    msg = f"{host_name} is hosting — join now{code_part}"
                    log.info(msg)
                    if update_status_text:
                        update_status_text(msg)

                    # Fire a one-time notification when someone NEW starts
                    # hosting -- but never for ourselves.
                    if host_name != last_notified_host and host_name != self.player_name and notify_desktop:
                        notify_desktop(
                            "Moonberry Save-Sync",
                            f"{host_name} started hosting — open the game to join!{code_part}",
                        )
                    last_notified_host = host_name
                elif self.play_requested.is_set():
                    last_notified_host = None  # reset so the next host triggers a fresh notification
                    self.play_requested.clear()
                    if update_status_text:
                        update_status_text("No host — claiming and starting...")
                    self.become_host_and_play()
                    if update_status_text:
                        update_status_text("Session ended. Click 'Play Now' to host again.")
                else:
                    last_notified_host = None
                    log.info("No one hosting. Waiting for 'Play Now' to be clicked.")
                    update_msg = self.update_checker.check()
                    if update_status_text:
                        if update_msg:
                            update_status_text(f"Idle — Play Now to host | {update_msg}")
                        else:
                            update_status_text("Idle — click 'Play Now' to host")

                is_first_check = False
            except requests.RequestException as e:
                log.error("Coordinator unreachable: %s", e)
                if update_status_text:
                    update_status_text("Coordinator unreachable — retrying...")

            time.sleep(self.poll_interval_seconds)
