"""Best-effort "is this game installed from store X on this machine?" checks,
for GameAdapter.detect_editions implementations to build on. Game-agnostic:
every function takes the store's own id for a game (Steam app id, Microsoft
Store package family name) as a parameter, so nothing here knows about any
specific game.

Every check fails closed (returns False) on any error or on non-Windows --
detection only ever *suggests* an edition in the settings UI, so a missed
detection just means the user picks it themselves."""

import logging
import os
import re
import sys
from pathlib import Path

log = logging.getLogger("moonberry-sync")

_VDF_PATH_PATTERN = re.compile(r'"path"\s+"((?:[^"\\]|\\.)*)"')


def _steam_library_dirs() -> list[Path]:
    """Every Steam library folder on this machine: the Steam install itself,
    plus any extra libraries listed in steamapps/libraryfolders.vdf."""
    if sys.platform != "win32":
        return []
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as key:
            steam_path = Path(winreg.QueryValueEx(key, "SteamPath")[0])
    except OSError:
        return []

    dirs = [steam_path]
    vdf = steam_path / "steamapps" / "libraryfolders.vdf"
    try:
        text = vdf.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return dirs
    for raw in _VDF_PATH_PATTERN.findall(text):
        path = Path(raw.replace("\\\\", "\\"))
        if path not in dirs:
            dirs.append(path)
    return dirs


def steam_app_installed(app_id: int) -> bool:
    """Whether Steam has the given app installed in any of its libraries --
    checked via the per-app appmanifest_<id>.acf Steam keeps for every
    installed app."""
    try:
        return any((d / "steamapps" / f"appmanifest_{app_id}.acf").is_file() for d in _steam_library_dirs())
    except Exception:
        log.debug("Steam install check failed for app %s", app_id, exc_info=True)
        return False


def store_package_installed(package_family_name: str) -> bool:
    """Whether a Microsoft Store / Xbox app (Game Pass) package is
    installed for this user. `package_family_name` is "<Name>_<PublisherId>",
    e.g. "CoffeeStainStudios.Valheim_496a1srhmar9w" -- the same id the
    game's shell:appsFolder launch URI uses."""
    if sys.platform != "win32":
        return False
    try:
        local_appdata = os.environ.get("LOCALAPPDATA")
        if local_appdata and (Path(local_appdata) / "Packages" / package_family_name).is_dir():
            return True

        # Registered packages are listed by full name,
        # "<Name>_<Version>_<Arch>_<ResourceId>_<PublisherId>".
        import winreg

        name, _, publisher_id = package_family_name.rpartition("_")
        repo = r"Software\Classes\Local Settings\Software\Microsoft\Windows\CurrentVersion\AppModel\Repository\Packages"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, repo) as key:
            index = 0
            while True:
                try:
                    full_name = winreg.EnumKey(key, index)
                except OSError:
                    return False
                if full_name.startswith(f"{name}_") and full_name.endswith(f"_{publisher_id}"):
                    return True
                index += 1
    except Exception:
        log.debug("Store package check failed for %s", package_family_name, exc_info=True)
        return False
