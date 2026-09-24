"""Optional, best-effort notifications. Neither of these should ever block
or crash an actual game session -- both fail silently (but log a warning)
on any error."""

import logging
import time

import requests

log = logging.getLogger("moonberry-sync")


class DiscordNotifier:
    """Talks to Moonberry, a separately-deployed Cloudflare Worker/Discord
    bot. Sends `game_id` in the body of a single generic /notify endpoint
    (rather than a per-game URL) so one bot deployment can handle any number
    of games -- the bot picks the channel/message format by branching on
    `game_id`. Skipped silently if not configured."""

    def __init__(self, moonberry_url: str | None, moonberry_secret: str | None):
        self.url = moonberry_url
        self.secret = moonberry_secret

    @property
    def enabled(self) -> bool:
        return bool(self.url and self.secret)

    def notify(self, game_id: str, host_name: str, join_code: str | None = None, event: str = "started"):
        if not self.enabled:
            return  # Moonberry integration not configured, skip silently

        try:
            r = requests.post(
                f"{self.url}/notify",
                headers={"X-Auth": self.secret, "Content-Type": "application/json"},
                json={
                    "game_id": game_id,
                    "host_name": host_name,
                    "join_code": join_code,
                    "event": event,
                },
                timeout=10,
            )
            if r.status_code == 200 and r.json().get("ok"):
                log.info("Notified Moonberry (Discord) - event: %s.", event)
            else:
                log.warning(
                    "Moonberry responded but did NOT confirm success (status %s): %s",
                    r.status_code,
                    r.text[:300],
                )
        except requests.RequestException as e:
            log.warning("Could not reach Moonberry (Discord notification skipped): %s", e)


class UpdateChecker:
    """Checks GitHub's public Releases API for the latest published version.
    Throttled to once an hour -- returns the cached result in between. Never
    blocks or interrupts an actual game session."""

    def __init__(self, repo: str | None, current_version: str, interval_seconds: int = 3600):
        self.repo = repo
        self.current_version = current_version
        self.interval_seconds = interval_seconds
        self._last_check = 0.0
        self._cached_message: str | None = None
        self._cached_release: dict | None = None

    def latest_release_info(self) -> dict | None:
        """Full parsed GitHub release JSON for the currently known update,
        if any -- None if no update is available, or check() hasn't run
        yet. check() itself only needs the tag/url for the status message;
        the updater needs the rest of this (the release's asset list) to
        find the file to actually download, so it's cached here alongside
        the message rather than fetched a second time."""
        return self._cached_release

    def check(self) -> str | None:
        if not self.repo:
            return None  # update checking not configured, skip silently

        now = time.time()
        if now - self._last_check < self.interval_seconds:
            return self._cached_message

        self._last_check = now
        try:
            r = requests.get(
                f"https://api.github.com/repos/{self.repo}/releases/latest",
                timeout=10,
                headers={"Accept": "application/vnd.github+json"},
            )
            r.raise_for_status()
            data = r.json()
            latest_tag = data.get("tag_name", "").lstrip("v")

            if latest_tag and latest_tag != self.current_version:
                release_url = data.get("html_url", f"https://github.com/{self.repo}/releases/latest")
                self._cached_message = f"Update available: v{latest_tag} — {release_url}"
                self._cached_release = data
                log.info(
                    "A new version is available: v%s (you're on v%s). Get it: %s",
                    latest_tag,
                    self.current_version,
                    release_url,
                )
            else:
                self._cached_message = None
                self._cached_release = None
        except requests.RequestException as e:
            log.warning("Could not check for updates (non-fatal): %s", e)
            # keep whatever the previous cached result was rather than
            # clearing it over a transient network blip

        return self._cached_message
