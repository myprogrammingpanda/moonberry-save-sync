"""Client for the Cloudflare Worker that arbitrates who's hosting (one host
per game). Entirely game-agnostic -- it just tracks names, join codes, and
save keys, keyed by game_id."""

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

    def claim_host(self, game_id: str, save_slot: str, save_display_name: str) -> dict:
        r = requests.post(
            f"{self.worker_url}/claim",
            headers=self._headers(),
            json={
                "name": self.player_name,
                "game_id": game_id,
                "save_slot": save_slot,
                "save_display_name": save_display_name,
            },
            timeout=10,
        )
        return r.json()

    # game_id on the two calls below says which of this player's claims
    # (one per game) it's about. An older coordinator with only one global
    # claim just ignores it.

    def announce_join_code(self, game_id: str, join_code: str) -> dict:
        r = requests.post(
            f"{self.worker_url}/announce_code",
            headers=self._headers(),
            json={"name": self.player_name, "game_id": game_id, "join_code": join_code},
            timeout=10,
        )
        return r.json()

    def release_host(self, game_id: str, save_key: str | None = None) -> dict:
        r = requests.post(
            f"{self.worker_url}/release",
            headers=self._headers(),
            json={"name": self.player_name, "game_id": game_id, "save_key": save_key},
            timeout=10,
        )
        return r.json()
