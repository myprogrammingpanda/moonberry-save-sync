"""Who's hosting which game, read from a coordinator /status dict.

The coordinator allows one host per game at a time and reports them in a
`hosts` map (game_id -> that game's claim). An older coordinator only knew
one global host, in the top-level hosting/host_name/game_id/... fields --
read the same way here, so this client still works against it (it just
never sees more than one host)."""


def hosts_by_game(status: dict | None) -> dict[str, dict]:
    """{game_id: {"host_name", "save_slot", "save_display_name",
    "join_code", "since"}} for every game someone's hosting right now."""
    if not status:
        return {}
    hosts = status.get("hosts")
    if isinstance(hosts, dict):
        return {game_id: host for game_id, host in hosts.items() if isinstance(host, dict)}
    if not status.get("hosting"):
        return {}
    return {
        status.get("game_id") or "unknown": {
            "host_name": status.get("host_name"),
            "save_slot": status.get("save_slot"),
            "save_display_name": status.get("save_display_name"),
            "join_code": status.get("join_code"),
            "since": status.get("since"),
        }
    }
