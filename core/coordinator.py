"""Client for the Cloudflare Worker that arbitrates who's hosting. Entirely
game-agnostic -- it just tracks a name, a join code, and a save key."""

import requests


class Coordinator:
    def __init__(self, worker_url: str, worker_secret: str, player_name: str):
        self.worker_url = worker_url.rstrip("/")
        self.worker_secret = worker_secret
        self.player_name = player_name

    def _headers(self):
        return {"X-Auth": self.worker_secret, "Content-Type": "application/json"}

    def get_status(self) -> dict:
        r = requests.get(f"{self.worker_url}/status", headers=self._headers(), timeout=10)
        r.raise_for_status()
        return r.json()

    def claim_host(self) -> dict:
        r = requests.post(
            f"{self.worker_url}/claim",
            headers=self._headers(),
            json={"name": self.player_name},
            timeout=10,
        )
        return r.json()

    def announce_join_code(self, join_code: str) -> dict:
        r = requests.post(
            f"{self.worker_url}/announce_code",
            headers=self._headers(),
            json={"name": self.player_name, "join_code": join_code},
            timeout=10,
        )
        return r.json()

    def release_host(self, save_key: str | None = None) -> dict:
        r = requests.post(
            f"{self.worker_url}/release",
            headers=self._headers(),
            json={"name": self.player_name, "save_key": save_key},
            timeout=10,
        )
        return r.json()
