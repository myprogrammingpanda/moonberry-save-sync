"""Game-agnostic host/sync state machine. Drives a GameAdapter through
claim -> sync -> wait for you to start the game yourself -> watch -> zip ->
upload -> release. This mirrors the original single-file app's logic,
generalized to call through a GameAdapter instead of being wired directly
to one game -- except Host Now no longer launches the game itself (that's
Play Now's job, a separate dumb "just open it" button); Host Now's job is
purely the claim/sync/watch/upload lifecycle around whatever session you
start yourself.

Polling the coordinator for "who's hosting" and self-healing a stale claim
used to live here too (run_loop), back when only one game's controller was
ever active at a time. Now that every configured game gets its own
controller simultaneously, that's core.status_poller.StatusPoller's job
instead -- one shared poll loop instead of one per game, so multiple
controllers don't each hit the coordinator and each race their own
self-heal independently."""

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
        local_content_hash,
        notifier,
        player_name: str,
        max_saved_versions: int = 5,
    ):
        self.app_dir = app_dir
        self.adapter = adapter
        self.coordinator = coordinator
        self.storage = storage
        self.local_record = local_record
        self.local_content_hash = local_content_hash
        self.notifier = notifier
        self.player_name = player_name
        self.max_saved_versions = max_saved_versions

        # Guards every zip/upload/download flow (a real hosting session or
        # either manual sync action) since they all touch the same temp
        # file paths and local save folder -- without this, a manual sync
        # running at the same moment as a real session could race on those
        # files.
        self._sync_lock = threading.Lock()

        # True for the whole span between a successful Host Now claim and
        # its eventual release -- including the window where it's claimed
        # and synced but still waiting for you to actually start the game
        # yourself, which can legitimately take a few minutes. run_loop's
        # stale-claim self-heal (below) needs this to tell that apart from
        # a genuinely stale claim left over from a crashed previous run of
        # the app -- without it, the self-heal would race an active,
        # perfectly healthy Host Now call running concurrently on its own
        # thread and yank the claim out from under it.
        self._host_now_pending = False

        # Set by the GUI's "Stop Host" button to back out of a Host Now
        # call that's still waiting for you to start the game -- e.g. you
        # clicked it and changed your mind. Checked only during that wait;
        # once the game is actually running there's nothing safe left to
        # interrupt (killing a running game process mid-session is exactly
        # the kind of save-corrupting risk the rest of this file exists to
        # avoid), so it has no effect after that point.
        self._stop_requested = threading.Event()

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
                self._refresh_content_hash()
                log.info("Local save updated to '%s'.", effective_cloud_key)
                return True
            return False
        else:
            log.info("Local save is already up to date ('%s').", local_save_key)
            return True

    def _refresh_content_hash(self) -> None:
        """Records the current on-disk save's content hash as "last known
        synced" -- called right after a successful upload or download, so
        the next Force Upload can tell whether anything's actually
        changed since. A no-op if the adapter doesn't implement hashing
        (content_hash() returning None)."""
        content_hash = self.adapter.content_hash()
        if content_hash:
            self.local_content_hash.write(content_hash)

    # How long Host Now waits for you to actually start the game yourself
    # after claiming and syncing -- much longer than the old "did the
    # process we just launched actually start" timeout, since this is now
    # waiting on a human to notice and click Play Now (or launch it some
    # other way), not on the OS finishing a process launch.
    HOST_NOW_WAIT_SECONDS = 300

    def stop_host(self) -> None:
        """Cancels a Host Now call that's still waiting for you to start
        the game. Fire-and-forget: just flips a flag _wait_for_launch_or_
        stop checks on its next poll, so the actual claim release happens
        moments later on Host Now's own thread, surfaced through its usual
        (bool, str) return -- this method itself has nothing to report."""
        self._stop_requested.set()

    def _wait_for_launch_or_stop(self, timeout: float) -> str:
        """Polls for either the game starting or a stop request, instead
        of adapter.wait_for_start's own polling loop, so Stop Host has
        something to interrupt. Returns "started", "stopped", or
        "timeout"."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.adapter.is_running():
                return "started"
            if self._stop_requested.is_set():
                return "stopped"
            time.sleep(2)
        return "timeout"

    def host_now(self, update_status_text=None, on_ready_for_launch=None, on_game_started=None) -> tuple[bool, str]:
        """Claims the host slot, syncs your local save, then waits for you
        to start the game yourself -- deliberately does NOT launch it (see
        Play Now for that). Refuses outright if the game's already
        running: syncing a save the game already has loaded is unsafe --
        the write either fails outright, or succeeds and then gets
        silently overwritten again by the game's own next autosave anyway,
        since its in-memory state was never touched -- so "close it first"
        is the only safe answer, not a special-cased partial sync.

        on_ready_for_launch, if given, is called with no arguments the
        moment it becomes safe to start the game -- i.e. right after
        syncing finishes, not before (starting it earlier could have it
        read a save that's still being unzipped) and not only once this
        whole call returns (which wouldn't happen until you've already
        played and closed the game -- far too late for Play Now to still
        be waiting on).

        on_game_started, if given, is called once the game is actually
        detected running -- the point past which Stop Host (see
        stop_host) no longer has anything to interrupt, so the GUI can
        disable it instead of leaving a dead, do-nothing button up."""
        if self.adapter.is_running():
            return False, f"{self.adapter.display_name} is already running — close it first, then click Host Now."
        if not self._sync_lock.acquire(blocking=False):
            return False, "A sync operation is already in progress — try again in a moment."
        try:
            return self._host_now_locked(update_status_text, on_ready_for_launch, on_game_started)
        finally:
            self._sync_lock.release()

    def _host_now_locked(self, update_status_text=None, on_ready_for_launch=None, on_game_started=None) -> tuple[bool, str]:
        log.info("Host Now requested. Attempting to claim host...")
        result = self.coordinator.claim_host(self.adapter.game_id)
        if not result.get("ok"):
            host_name = result.get("current", {}).get("host_name") or "someone"
            msg = f"Someone is currently hosting ({host_name}) — try again later."
            log.warning(msg)
            return False, msg

        current = result["current"]
        self._host_now_pending = True
        self._stop_requested.clear()  # fresh for this attempt -- ignore any stale flag from a previous one

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

            if on_ready_for_launch:
                on_ready_for_launch()

            log.info("Host claimed. Waiting for you to start %s...", self.adapter.display_name)
            if update_status_text:
                update_status_text(
                    f"Host claimed — start {self.adapter.display_name} now (Play Now, or launch it yourself)."
                )
            outcome = self._wait_for_launch_or_stop(self.HOST_NOW_WAIT_SECONDS)
            if outcome == "stopped":
                msg = "Host Now cancelled — host claim released."
                log.info(msg)
                return False, msg  # falls through to finally, which releases the claim
            if outcome == "timeout":
                msg = f"{self.adapter.display_name} wasn't started in time — host claim released."
                log.warning(msg)
                return False, msg  # falls through to finally, which releases the claim

            if on_game_started:
                on_game_started()

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
            self._host_now_pending = False
            if uploaded_successfully:
                self.coordinator.release_host(save_key=uploaded_key)
                self.local_record.write(uploaded_key)
                self._refresh_content_hash()
                log.info(
                    "Save uploaded ('%s') and host slot released. Thanks for playing!",
                    uploaded_key,
                )
                self.storage.prune_old_saves(keep=self.max_saved_versions)
                self.notifier.notify(self.adapter.game_id, self.player_name, event="ended")
            else:
                self.coordinator.release_host()
                log.info("Host slot released (no upload happened this time).")

        if uploaded_successfully:
            return True, f"Session ended — save uploaded as '{uploaded_key}'."
        return False, "Session ended without a successful upload — see log for details."

    def check_upload_redundancy(self) -> tuple[bool, str]:
        """Compares the current on-disk save's content against the hash
        recorded the last time this machine's save was known to match the
        cloud (just uploaded or just downloaded). A save_key match alone
        (see check_upload_freshness) can't catch this: it only tells you
        the local save isn't BEHIND the cloud, not whether it's actually
        CHANGED since your own last sync -- clicking Force Upload twice in
        a row with nothing played in between would pass that check both
        times, minting a pointless duplicate version and pruning away a
        real, distinct older one to make room for it. Returns
        (is_unchanged, message); message is empty when not redundant (or
        when there's no baseline hash yet to compare against, e.g. before
        this machine's first upload/download since this feature shipped)."""
        current_hash = self.adapter.content_hash()
        if not current_hash:
            return False, ""  # adapter doesn't support hashing, or nothing on disk yet

        last_known_hash = self.local_content_hash.read()
        if not last_known_hash or current_hash != last_known_hash:
            return False, ""

        return True, "Your save hasn't changed since your last upload or download — nothing new to upload."

    def check_upload_freshness(self) -> tuple[bool, str]:
        """Compares the local save record against what the coordinator
        currently reports as the latest version. Force Upload has no
        other version awareness at all -- it just zips and uploads
        whatever's on disk, minting a key that's always newer by
        timestamp regardless of what's actually inside it, so a friend
        whose local save is behind could otherwise silently make their
        stale copy the new "official" version for the whole group and
        regress everyone else's next sync. Returns (is_stale, message);
        message is empty when not stale. Fails open (not stale, so the
        upload proceeds) if the coordinator can't be reached right now --
        the actual claim attempt right after this will surface that on
        its own."""
        try:
            status = self.coordinator.get_status()
        except requests.RequestException:
            return False, ""

        cloud_key = status.get("save_key")
        if not cloud_key:
            return False, ""  # coordinator has no versioned save yet -- nothing to compare against

        if cloud_key == self.local_record.read():
            return False, ""

        return True, (
            f"Your local save doesn't match the latest cloud version ('{cloud_key}'). "
            "Uploading now will make your current local save the new official version "
            "for everyone -- if it's actually older, this could undo others' progress. "
            "Continue anyway?"
        )

    def force_upload_current_save(self) -> tuple[bool, str]:
        """Uploads whatever's currently in the save folder as a new
        version, independent of whether this app was used to host a
        session -- e.g. after playing solo, outside the app's normal
        flow. Still goes through the same claim/release ceremony as a
        real session, so it can't step on an actual in-progress host
        (this fails for ANY current claim-holder, not just other
        players -- the coordinator's /claim is a strict atomic check,
        not an identity check). Launching the game is skipped entirely;
        this is upload-only. Doesn't check version freshness itself --
        see check_upload_freshness, called separately by the GUI before
        this, so the (possibly slow) coordinator round-trip for that
        check doesn't block acquiring _sync_lock unnecessarily."""
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
        result = self.coordinator.claim_host(self.adapter.game_id)
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
                self._refresh_content_hash()
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
