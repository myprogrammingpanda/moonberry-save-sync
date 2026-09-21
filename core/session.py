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
        # Deliberately never auto-set, including on first launch -- opening
        # the app should only ever show status, never claim host or launch
        # the game on its own.
        self.play_requested = threading.Event()

        # Guards every zip/upload/download flow (a real hosting session or
        # either manual sync action) since they all touch the same temp
        # file paths and local save folder -- without this, a manual sync
        # running at the same moment as a real session could race on those
        # files.
        self._sync_lock = threading.Lock()

    def sync_down_if_needed(self, cloud_save_key: str | None) -> bool:
        """Downloads and applies the cloud save if the local copy doesn't
        already match it. Returns True if the local save now matches the
        cloud version (whether that took a fresh download or it already
        matched), or False if a download was needed but failed."""
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
            tmp_zip = self.app_dir / f"_incoming_save_{self.adapter.game_id}.zip"
            if self.storage.download_save(tmp_zip, effective_cloud_key):
                self.adapter.unzip_save(tmp_zip)
                tmp_zip.unlink(missing_ok=True)
                self.local_record.write(effective_cloud_key)
                log.info("Local save updated to '%s'.", effective_cloud_key)
                return True
            return False
        else:
            log.info("Local save is already up to date ('%s').", local_save_key)
            return True

    def become_host_and_play(self, update_status_text=None):
        with self._sync_lock:
            self._become_host_and_play_locked(update_status_text)

    def _become_host_and_play_locked(self, update_status_text=None):
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
            if update_status_text:
                update_status_text("Syncing your save...")
            self.sync_down_if_needed(current.get("save_key"))

            if update_status_text:
                update_status_text(f"Launching {self.adapter.display_name}...")
            self.adapter.launch()
            if not self.adapter.wait_for_start():
                log.warning("%s didn't seem to start within the timeout.", self.adapter.display_name)
                return  # falls through to finally, which releases the claim

            log.info(
                "You're hosting as '%s'. Friends can join you. This app "
                "will auto-sync the save when you close the game.",
                self.player_name,
            )
            if update_status_text:
                update_status_text(f"You're hosting as '{self.player_name}' — playing now.")

            log.info("Watching for a join code to share...")
            join_code = self.adapter.scrape_join_code()

            if join_code:
                self.coordinator.announce_join_code(join_code)
                log.info("Shared join code '%s' with the group.", join_code)
                if update_status_text:
                    update_status_text(f"Hosting as '{self.player_name}' — join code: {join_code}")
            else:
                log.info(
                    "No join code was found before the session ended (or this "
                    "game doesn't use them) -- that's fine."
                )

            self.notifier.notify(self.adapter.game_id, self.player_name, join_code, event="started")

            self.adapter.wait_for_exit()
            log.info("%s has closed.", self.adapter.display_name)
            if update_status_text:
                update_status_text(f"{self.adapter.display_name} closed — uploading your save...")

            out_zip = self.app_dir / f"_outgoing_save_{self.adapter.game_id}.zip"

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

    def force_upload_current_save(self) -> tuple[bool, str]:
        """Uploads whatever's currently in the save folder as a new
        version, independent of whether this app was used to host a
        session -- e.g. after playing solo, outside the app's normal
        flow. Still goes through the same claim/release ceremony as a
        real session, so it can't step on an actual in-progress host
        (this fails for ANY current claim-holder, not just other
        players -- the coordinator's /claim is a strict atomic check,
        not an identity check). Launching the game is skipped entirely;
        this is upload-only."""
        if self.adapter.is_running():
            return False, f"{self.adapter.display_name} is currently running — close it first."
        if not self._sync_lock.acquire(blocking=False):
            return False, "A sync operation is already in progress — try again in a moment."
        try:
            return self._force_upload_current_save_locked()
        finally:
            self._sync_lock.release()

    def _force_upload_current_save_locked(self) -> tuple[bool, str]:
        log.info("Manual upload requested. Attempting to claim host...")
        result = self.coordinator.claim_host()
        if not result.get("ok"):
            host_name = result.get("current", {}).get("host_name") or "someone"
            msg = f"Someone is currently hosting ({host_name}) — try again later."
            log.warning(msg)
            return False, msg

        uploaded_successfully = False
        uploaded_key = None
        try:
            out_zip = self.app_dir / f"_outgoing_save_{self.adapter.game_id}.zip"
            log.info("Zipping current save for manual upload...")
            self.adapter.zip_save(out_zip)
            uploaded_key = self.storage.new_save_key()
            log.info("Uploading save as '%s'...", uploaded_key)
            self.storage.upload_save(out_zip, uploaded_key)
            out_zip.unlink(missing_ok=True)
            uploaded_successfully = True
        except Exception:
            log.exception("Manual upload failed.")
        finally:
            if uploaded_successfully:
                self.coordinator.release_host(save_key=uploaded_key)
                self.local_record.write(uploaded_key)
                self.storage.prune_old_saves(keep=self.max_saved_versions)
            else:
                self.coordinator.release_host()

        if uploaded_successfully:
            msg = f"Save uploaded as '{uploaded_key}'."
            log.info(msg)
            return True, msg
        return False, "Upload failed — see log for details."

    def force_download_latest(self) -> tuple[bool, str]:
        """Downloads and applies whatever the coordinator currently has
        as the latest save, reusing sync_down_if_needed's own
        backup-before-overwrite logic -- the same protection a normal
        sync gets. No claim is taken: this only reads the coordinator's
        status and the storage bucket, neither of which is exclusive."""
        if self.adapter.is_running():
            return False, f"{self.adapter.display_name} is currently running — close it first."
        if not self._sync_lock.acquire(blocking=False):
            return False, "A sync operation is already in progress — try again in a moment."
        try:
            return self._force_download_latest_locked()
        finally:
            self._sync_lock.release()

    def _force_download_latest_locked(self) -> tuple[bool, str]:
        try:
            status = self.coordinator.get_status()
        except requests.RequestException as e:
            msg = f"Could not reach the coordinator: {e}"
            log.warning(msg)
            return False, msg

        log.info("Manual download requested.")
        try:
            ok = self.sync_down_if_needed(status.get("save_key"))
        except Exception:
            log.exception("Manual download failed.")
            return False, "Download failed — see log for details."

        if ok:
            return True, "Local save is up to date with the latest cloud version."
        return False, "Download failed — see log for details."

    def run_loop(self, update_status_text=None, notify_desktop=None, set_hosting_active=None):
        last_notified_host = None  # tracks who we've already notified about,
        # so we only pop a notification ONCE per session start, not every
        # poll cycle while that person keeps hosting.

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

                    # Play Now while someone else is hosting just opens the
                    # game so you can join with the code above -- it never
                    # claims host or touches your local save (joining never
                    # does; only the host's save gets synced). Deliberately
                    # NOT queued for later: actually joining still needs you
                    # at the keyboard once the game opens, so there'd be
                    # nothing for an unattended auto-launch to accomplish
                    # once they stop hosting -- worse, it would also grab
                    # the coordinator's host claim with no one there to
                    # finish starting a world, leaving friends looking at a
                    # "hosting" status with no join code ever showing up.
                    if self.play_requested.is_set():
                        self.play_requested.clear()
                        if host_name != self.player_name:
                            log.info("Join requested -- launching %s...", self.adapter.display_name)
                            if update_status_text:
                                update_status_text(f"Launching {self.adapter.display_name} to join {host_name}...")
                            self.adapter.launch()
                elif self.play_requested.is_set():
                    last_notified_host = None  # reset so the next host triggers a fresh notification
                    self.play_requested.clear()
                    if update_status_text:
                        update_status_text("No host — claiming and starting...")
                    if set_hosting_active:
                        set_hosting_active(True)
                    try:
                        self.become_host_and_play(update_status_text=update_status_text)
                    finally:
                        if set_hosting_active:
                            set_hosting_active(False)
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

            except requests.RequestException as e:
                log.error("Coordinator unreachable: %s", e)
                if update_status_text:
                    update_status_text("Coordinator unreachable — retrying...")

            # Waits up to the normal poll interval, but wakes immediately
            # if "Play Now" sets play_requested mid-wait -- same polling
            # cadence when idle, no lag when a click needs acting on.
            self.play_requested.wait(timeout=self.poll_interval_seconds)
