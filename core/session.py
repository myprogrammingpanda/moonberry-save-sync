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

from core.save_status import resolve_slot_cloud_key
from core.storage import LocalContentHash, LocalSaveRecord, SaveStorage

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
        cfg: dict,
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
        self.cfg = cfg
        self.max_saved_versions = max_saved_versions

        # Per-save-slot (storage, local_record, local_content_hash, save_name)
        # tuples, built lazily and cached as multi-save actions touch new
        # slots. Seeded with the DEFAULT slot's exact instances passed in
        # above (the same ones registry.py always built) rather than
        # reconstructing filename-equivalent ones, so the configured
        # default save's behavior stays byte-identical to before
        # multi-save support existed.
        self._default_slot_id = adapter.slot_id_for(adapter.default_save_name)
        self._slot_resources: dict[str, tuple] = {
            self._default_slot_id: (storage, local_record, local_content_hash, adapter.default_save_name)
        }

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

    def _resources_for(self, save_name: str | None) -> tuple:
        """Resolves (and lazily builds/caches) the (storage, local_record,
        local_content_hash, save_name) tuple for the given save, or the
        configured default save if save_name is None -- every action
        method below goes through this instead of touching self.storage /
        self.local_record / self.local_content_hash directly, so acting on
        a non-default slot never disturbs the default slot's own tracking
        files or vice versa."""
        name = save_name or self.adapter.default_save_name
        slot_id = self.adapter.slot_id_for(name)
        if slot_id not in self._slot_resources:
            prefix = self.adapter.save_key_prefix_for(name)
            storage = SaveStorage(self.cfg, key_prefix=prefix)
            # slot_id=None for the default slot reproduces the exact
            # pre-multi-save filenames (see core/storage.py) -- but the
            # default slot is always already seeded in __init__, so this
            # branch only ever runs for a genuinely non-default slot.
            local_record = LocalSaveRecord(self.app_dir, self.adapter.game_id, prefix, slot_id=slot_id)
            local_content_hash = LocalContentHash(self.app_dir, self.adapter.game_id, slot_id=slot_id)
            self._slot_resources[slot_id] = (storage, local_record, local_content_hash, name)
        return self._slot_resources[slot_id]

    def _resolve_cloud_save_key(self, status: dict, save_name: str | None = None) -> str | None:
        """The coordinator's claim/status is one global lock shared across
        every game (now further keyed by save-slot too), but each save's
        actual data is independent -- status["save_keys"] is a
        {game_id: {slot_id: key}} map so each save can find its OWN
        latest key instead of whichever save most recently released. See
        core.save_status.resolve_slot_cloud_key for the full shape
        handling (new nested map, pre-multi-save flat per-game string, or
        the oldest single global flat save_key) -- adopting either flat
        form only if it actually matches OUR OWN storage prefix, confirming
        it's really this save's data and not some other save's (or some
        other game's) key handed to the wrong adapter (exactly the bug
        this replaced: Force Download/Host Now on Zomboid pulling down
        Valheim's key and unzipping Valheim's save data into Zomboid's
        save folder)."""
        name = save_name or self.adapter.default_save_name
        slot_id = self.adapter.slot_id_for(name)
        own_prefix = self.adapter.save_key_prefix_for(name)
        return resolve_slot_cloud_key(status, self.adapter.game_id, slot_id, own_prefix)

    def sync_down_if_needed(self, cloud_save_key: str | None, save_name: str | None = None) -> bool:
        """Downloads and applies the cloud save if the local copy doesn't
        already match it. Returns True if the local save now matches the
        cloud version (whether that took a fresh download or it already
        matched), or False if a download was needed but failed."""
        storage, local_record, _, name = self._resources_for(save_name)
        slot_id = self.adapter.slot_id_for(name)
        local_save_key = local_record.read()

        # Fallback for the transition period right after upgrading to the
        # versioned-save scheme: if the coordinator doesn't have a save_key
        # yet, fall back to the old static filename so existing saves
        # aren't stranded. storage.legacy_key is None unless a config
        # explicitly sets one (see core/storage.py) -- most games (anything
        # that isn't Valheim's original pre-multi-game save) will never
        # have one, which is correct: they never had unversioned legacy
        # data to migrate from in the first place.
        effective_cloud_key = cloud_save_key or storage.legacy_key

        if not cloud_save_key and effective_cloud_key:
            log.info(
                "Coordinator has no versioned save_key yet -- falling back to "
                "legacy filename '%s' for this sync.",
                effective_cloud_key,
            )

        if not effective_cloud_key:
            log.info("No cloud save known yet for %s -- nothing to sync down.", self.adapter.display_name)
            return True

        if effective_cloud_key != local_save_key:
            log.info(
                "Cloud save ('%s') differs from local record ('%s'). Downloading...",
                effective_cloud_key,
                local_save_key,
            )
            self.adapter.backup_local_save(self.app_dir / "local_backups", save_name=name)
            tmp_zip = self.app_dir / f"_incoming_save_{self.adapter.game_id}_{slot_id}.zip"
            if storage.download_save(tmp_zip, effective_cloud_key):
                self.adapter.unzip_save(tmp_zip)
                tmp_zip.unlink(missing_ok=True)
                local_record.write(effective_cloud_key)
                self._refresh_content_hash(save_name=name)
                log.info("Local save updated to '%s'.", effective_cloud_key)
                return True
            return False
        else:
            log.info("Local save is already up to date ('%s').", local_save_key)
            return True

    def _refresh_content_hash(self, save_name: str | None = None) -> None:
        """Records the current on-disk save's content hash as "last known
        synced" -- called right after a successful upload or download, so
        the next Force Upload can tell whether anything's actually
        changed since. A no-op if the adapter doesn't implement hashing
        (content_hash() returning None)."""
        _, _, local_content_hash, name = self._resources_for(save_name)
        content_hash = self.adapter.content_hash(save_name=name)
        if content_hash:
            local_content_hash.write(content_hash)

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

    def host_now(
        self, save_name: str | None = None, update_status_text=None, on_ready_for_launch=None, on_game_started=None
    ) -> tuple[bool, str]:
        """Claims the host slot (for save_name, or the configured default
        save if not given), syncs your local save, then waits for you to
        start the game yourself -- deliberately does NOT launch it (see
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
            return self._host_now_locked(save_name, update_status_text, on_ready_for_launch, on_game_started)
        finally:
            self._sync_lock.release()

    def _host_now_locked(
        self, save_name: str | None = None, update_status_text=None, on_ready_for_launch=None, on_game_started=None
    ) -> tuple[bool, str]:
        storage, local_record, _, name = self._resources_for(save_name)
        slot_id = self.adapter.slot_id_for(name)
        log.info("Host Now requested. Attempting to claim host...")
        result = self.coordinator.claim_host(self.adapter.game_id, slot_id, name)
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
            self.sync_down_if_needed(self._resolve_cloud_save_key(current, save_name=name), save_name=name)

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

            if not self.adapter.has_local_save(save_name=name):
                # Nothing was actually created/found at the configured save
                # location -- uploading now would zip empty/nonexistent
                # data and silently make THAT the group's new "official"
                # save. Most likely cause: this game's world/server name
                # (in Settings) doesn't match what was actually played --
                # e.g. joining an existing shared save under the wrong
                # name, so a real download landed under a different folder
                # than the one configured here. Refuse instead of
                # uploading garbage; the claim is released below with no
                # upload, same as a cancelled session.
                msg = (
                    f"No local save found at the configured location for {self.adapter.display_name} -- "
                    "not uploading. Check that the world/server name in Settings matches what you just played."
                )
                log.warning(msg)
                return False, msg

            if update_status_text:
                update_status_text(f"{self.adapter.display_name} closed — uploading your save...")

            out_zip = self.app_dir / f"_outgoing_save_{self.adapter.game_id}_{slot_id}.zip"

            zip_start = time.time()
            log.info("Zipping save...")
            self.adapter.zip_save(out_zip, save_name=name)
            zip_seconds = time.time() - zip_start
            zip_size_mb = out_zip.stat().st_size / (1024 * 1024)
            log.info("Zip complete: %.1f MB in %.1fs.", zip_size_mb, zip_seconds)

            uploaded_key = storage.new_save_key()
            upload_start = time.time()
            log.info("Uploading save as '%s'...", uploaded_key)
            storage.upload_save(out_zip, uploaded_key)
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
                local_record.write(uploaded_key)
                self._refresh_content_hash(save_name=name)
                log.info(
                    "Save uploaded ('%s') and host slot released. Thanks for playing!",
                    uploaded_key,
                )
                storage.prune_old_saves(keep=self.max_saved_versions)
                self.notifier.notify(self.adapter.game_id, self.player_name, event="ended")
            else:
                self.coordinator.release_host()
                log.info("Host slot released (no upload happened this time).")

        if uploaded_successfully:
            return True, f"Session ended — save uploaded as '{uploaded_key}'."
        return False, "Session ended without a successful upload — see log for details."

    def check_upload_redundancy(self, save_name: str | None = None) -> tuple[bool, str]:
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
        _, _, local_content_hash, name = self._resources_for(save_name)
        current_hash = self.adapter.content_hash(save_name=name)
        if not current_hash:
            return False, ""  # adapter doesn't support hashing, or nothing on disk yet

        last_known_hash = local_content_hash.read()
        if not last_known_hash or current_hash != last_known_hash:
            return False, ""

        return True, "Your save hasn't changed since your last upload or download — nothing new to upload."

    def check_upload_freshness(self, save_name: str | None = None) -> tuple[bool, str]:
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
        _, local_record, _, name = self._resources_for(save_name)
        try:
            status = self.coordinator.get_status()
        except requests.RequestException:
            return False, ""

        cloud_key = self._resolve_cloud_save_key(status, save_name=name)
        if not cloud_key:
            return False, ""  # coordinator has no versioned save yet -- nothing to compare against

        if cloud_key == local_record.read():
            return False, ""

        return True, (
            f"Your local save doesn't match the latest cloud version ('{cloud_key}'). "
            "Uploading now will make your current local save the new official version "
            "for everyone -- if it's actually older, this could undo others' progress. "
            "Continue anyway?"
        )

    def force_upload_current_save(self, save_name: str | None = None) -> tuple[bool, str]:
        """Uploads whatever's currently in the save folder (for save_name,
        or the configured default save if not given) as a new version,
        independent of whether this app was used to host a session -- e.g.
        after playing solo, outside the app's normal flow. Still goes
        through the same claim/release ceremony as a real session, so it
        can't step on an actual in-progress host (this fails for ANY
        current claim-holder, not just other players -- the coordinator's
        /claim is a strict atomic check, not an identity check). Launching
        the game is skipped entirely; this is upload-only. Doesn't check
        version freshness itself -- see check_upload_freshness, called
        separately by the GUI before this, so the (possibly slow)
        coordinator round-trip for that check doesn't block acquiring
        _sync_lock unnecessarily."""
        if self.adapter.is_running():
            return False, f"{self.adapter.display_name} is currently running — close it first."
        if not self.adapter.has_local_save(save_name=save_name):
            # Refuse before ever claiming -- zipping/uploading an empty or
            # nonexistent save would make that the group's new "official"
            # version. See the matching guard in _host_now_locked for the
            # full reasoning (most likely cause: a world/server name in
            # Settings that doesn't match what's actually on disk).
            return False, (
                f"No local save found at the configured location for {self.adapter.display_name} -- "
                "not uploading. Check that the world/server name in Settings matches your actual save."
            )
        if not self._sync_lock.acquire(blocking=False):
            return False, "A sync operation is already in progress — try again in a moment."
        try:
            return self._force_upload_current_save_locked(save_name)
        finally:
            self._sync_lock.release()

    def _force_upload_current_save_locked(self, save_name: str | None = None) -> tuple[bool, str]:
        storage, local_record, _, name = self._resources_for(save_name)
        slot_id = self.adapter.slot_id_for(name)
        log.info("Manual upload requested. Attempting to claim host...")
        result = self.coordinator.claim_host(self.adapter.game_id, slot_id, name)
        if not result.get("ok"):
            host_name = result.get("current", {}).get("host_name") or "someone"
            msg = f"Someone is currently hosting ({host_name}) — try again later."
            log.warning(msg)
            return False, msg

        uploaded_successfully = False
        uploaded_key = None
        try:
            out_zip = self.app_dir / f"_outgoing_save_{self.adapter.game_id}_{slot_id}.zip"
            log.info("Zipping current save for manual upload...")
            self.adapter.zip_save(out_zip, save_name=name)
            uploaded_key = storage.new_save_key()
            log.info("Uploading save as '%s'...", uploaded_key)
            storage.upload_save(out_zip, uploaded_key)
            out_zip.unlink(missing_ok=True)
            uploaded_successfully = True
        except Exception:
            log.exception("Manual upload failed.")
        finally:
            if uploaded_successfully:
                self.coordinator.release_host(save_key=uploaded_key)
                local_record.write(uploaded_key)
                self._refresh_content_hash(save_name=name)
                storage.prune_old_saves(keep=self.max_saved_versions)
            else:
                self.coordinator.release_host()

        if uploaded_successfully:
            msg = f"Save uploaded as '{uploaded_key}'."
            log.info(msg)
            return True, msg
        return False, "Upload failed — see log for details."

    def force_download_latest(self, save_name: str | None = None) -> tuple[bool, str]:
        """Downloads and applies whatever the coordinator currently has
        as the latest version of the given save (or the configured
        default save if not given), reusing sync_down_if_needed's own
        backup-before-overwrite logic -- the same protection a normal
        sync gets. No claim is taken: this only reads the coordinator's
        status and the storage bucket, neither of which is exclusive."""
        if self.adapter.is_running():
            return False, f"{self.adapter.display_name} is currently running — close it first."
        if not self._sync_lock.acquire(blocking=False):
            return False, "A sync operation is already in progress — try again in a moment."
        try:
            return self._force_download_latest_locked(save_name)
        finally:
            self._sync_lock.release()

    def _force_download_latest_locked(self, save_name: str | None = None) -> tuple[bool, str]:
        try:
            status = self.coordinator.get_status()
        except requests.RequestException as e:
            msg = f"Could not reach the coordinator: {e}"
            log.warning(msg)
            return False, msg

        log.info("Manual download requested.")
        try:
            ok = self.sync_down_if_needed(self._resolve_cloud_save_key(status, save_name=save_name), save_name=save_name)
        except Exception:
            log.exception("Manual download failed.")
            return False, "Download failed — see log for details."

        if ok:
            return True, "Local save is up to date with the latest cloud version."
        return False, "Download failed — see log for details."
