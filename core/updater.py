"""Auto-update: download -> verify -> extract -> spawn a detached helper
that waits for this process to exit, replaces the app's files, reinstalls
dependencies, and relaunches.

Windows doesn't lock running .py source files the way it locks a running
.exe, but the running process still can't safely swap its own
already-imported modules out from under itself mid-execution -- so this
module only does the parts that ARE safe to do while still running
(download, integrity check, extraction to a fresh staging directory) and
hands the actual file-replace/relaunch off to a detached helper script that
only starts touching anything after this process has fully exited.
"""

import logging
import os
import re
import subprocess
import tempfile
import zipfile
from pathlib import Path

import requests

log = logging.getLogger("moonberry-sync")


class UpdateError(Exception):
    """Anything that should abort the update and leave the current install
    untouched. Caught by the GUI and shown to the user instead of
    propagating into a crash -- nothing that raises this has modified the
    live app directory."""


# The release process for this repo deliberately does NOT distribute
# GitHub's auto-generated source archive (see README/release notes) -- a
# manually-built, custom-named zip is uploaded as the release asset every
# time instead. GitHub's auto zip/tarball links live under separate
# zipball_url/tarball_url fields on the release, never in `assets`, so
# matching by name here is just an extra safety net, not the only thing
# preventing a mismatch.
_ASSET_NAME_RE = re.compile(r"^moonberry-save-sync-.*\.zip$")


def find_release_asset(release_info: dict) -> dict:
    for asset in release_info.get("assets", []):
        if _ASSET_NAME_RE.match(asset.get("name", "")):
            return asset
    raise UpdateError("The latest release has no moonberry-save-sync-*.zip asset to download.")


def download_release_asset(asset: dict, dest_zip: Path, progress_cb=None) -> None:
    """Streams the asset to dest_zip, reporting progress via
    progress_cb(downloaded_bytes, total_bytes) if given (total_bytes may be
    None if GitHub didn't report a size). Verifies the final size against
    what GitHub reported for the asset -- cheap, and catches a
    truncated/interrupted download before it's ever extracted."""
    url = asset["browser_download_url"]
    expected_size = asset.get("size")
    downloaded = 0
    try:
        with requests.get(url, stream=True, timeout=30) as r:
            r.raise_for_status()
            with open(dest_zip, "wb") as f:
                for chunk in r.iter_content(chunk_size=256 * 1024):
                    f.write(chunk)
                    downloaded += len(chunk)
                    if progress_cb:
                        progress_cb(downloaded, expected_size)
    except requests.RequestException as e:
        raise UpdateError(f"Download failed: {e}") from e

    if expected_size and downloaded != expected_size:
        raise UpdateError(
            f"Downloaded file size ({downloaded} bytes) doesn't match the "
            f"size GitHub reported ({expected_size} bytes) -- likely a "
            f"truncated or corrupted download."
        )


def extract_update(zip_path: Path, dest_dir: Path) -> None:
    """Verifies the zip isn't corrupted, then extracts it into dest_dir --
    a fresh directory the caller creates, never the live app directory --
    stripping the single top-level `moonberry-save-sync-vX.X.X/` folder
    every release asset is built with (`git archive --prefix=...`), so
    files land directly in dest_dir instead of one level too deep."""
    try:
        with zipfile.ZipFile(zip_path) as zf:
            bad_entry = zf.testzip()
            if bad_entry:
                raise UpdateError(f"Downloaded update is corrupted (bad entry: {bad_entry}).")

            names = [n for n in zf.namelist() if not n.endswith("/")]
            if not names:
                raise UpdateError("Downloaded update archive is empty.")

            top_level = {n.split("/", 1)[0] for n in names if "/" in n}
            if len(top_level) != 1 or any("/" not in n for n in names):
                raise UpdateError(
                    "Downloaded update archive doesn't have the expected "
                    "single top-level folder -- refusing to extract."
                )
            prefix = next(iter(top_level)) + "/"

            dest_dir.mkdir(parents=True, exist_ok=True)
            dest_resolved = dest_dir.resolve()
            for name in names:
                relative = name[len(prefix):]
                if not relative:
                    continue
                target = (dest_dir / relative).resolve()
                # Defensive -- name ultimately came from a network download.
                # Confirms extraction never writes outside dest_dir even if
                # a malicious/corrupted entry tried a "../" escape.
                if dest_resolved != target and dest_resolved not in target.parents:
                    raise UpdateError(f"Update archive contains an unsafe path: {name!r}")
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(name) as src, open(target, "wb") as out:
                    out.write(src.read())
    except zipfile.BadZipFile as e:
        raise UpdateError(f"Downloaded file isn't a valid zip: {e}") from e


def prepare_update(app_dir: Path, release_info: dict, progress_cb=None) -> Path:
    """Runs everything that's safe to do while the app is still running:
    download, integrity check, extraction to a fresh staging directory.
    Returns the staging directory path on success. Raises UpdateError on
    any failure -- the live app directory is never touched by this
    function, so a failure here always leaves the current install exactly
    as it was."""
    asset = find_release_asset(release_info)

    tmp_root = Path(tempfile.mkdtemp(prefix="moonberry_update_"))
    zip_path = tmp_root / asset["name"]
    staging_dir = tmp_root / "staged"

    log.info("Downloading update '%s'...", asset["name"])
    download_release_asset(asset, zip_path, progress_cb)

    log.info("Verifying and extracting update...")
    extract_update(zip_path, staging_dir)
    zip_path.unlink(missing_ok=True)

    return staging_dir


_HELPER_SCRIPT_TEMPLATE = """@echo off
setlocal
title Moonberry Save-Sync Update

set "APP_DIR={app_dir}"
set "STAGING_DIR={staging_dir}"
set "MAIN_PID={main_pid}"

echo Waiting for Moonberry Save-Sync to close...
:waitloop
tasklist /FI "PID eq %MAIN_PID%" 2>NUL | find "%MAIN_PID%" >NUL
if not errorlevel 1 (
    timeout /t 1 /nobreak >nul
    goto waitloop
)

echo Copying updated files...
robocopy "%STAGING_DIR%" "%APP_DIR%" /E /IS /IT /NFL /NDL /NJH /NJS
if %errorlevel% GEQ 8 (
    echo File copy failed -- update aborted. Your existing install was left as-is.
    echo Delete "%STAGING_DIR%" manually once you've sorted this out.
    pause
    exit /b 1
)

echo Installing/updating dependencies -- this can take a minute...
where python >nul 2>nul
if errorlevel 1 (
    echo Could not find "python" on PATH -- skipping automatic dependency install.
    echo Run "pip install -r requirements.txt" yourself from the app folder.
) else (
    python -m pip install -r "%APP_DIR%\\requirements.txt"
)

rmdir /s /q "%STAGING_DIR%" 2>nul

echo Update complete. Relaunching...
REM `start` special-cases .bat/.cmd targets: it launches them via
REM "cmd /K" (run, then keep the window open) instead of "/C" (run,
REM then close), unlike any other target. Routing through an explicit
REM "cmd /c" ourselves forces normal run-then-close behavior, so this
REM relaunch's own window closes itself the same way this helper's does.
start "" cmd /c "%APP_DIR%\\run.bat"

exit /b 0
"""


def write_helper_script(app_dir: Path, staging_dir: Path, main_pid: int) -> Path:
    script = _HELPER_SCRIPT_TEMPLATE.format(app_dir=app_dir, staging_dir=staging_dir, main_pid=main_pid)
    fd, path = tempfile.mkstemp(suffix=".bat", prefix="moonberry_update_helper_")
    os.close(fd)
    script_path = Path(path)
    script_path.write_text(script, encoding="utf-8")
    return script_path


def launch_helper(script_path: Path) -> None:
    """Spawns the helper in its own visible console window (so pip
    install's progress -- the slowest, most opaque step -- is actually
    visible instead of silently running hidden) and detached from this
    process, so it survives the os._exit() that follows this call."""
    CREATE_NEW_CONSOLE = 0x00000010
    subprocess.Popen(
        ["cmd", "/c", str(script_path)],
        creationflags=CREATE_NEW_CONSOLE,
        cwd=str(script_path.parent),
    )


def begin_relaunch(app_dir: Path, staging_dir: Path) -> None:
    """Spawns the detached helper, which waits for THIS process to fully
    exit before touching any files. Call this right before shutting the
    app down -- never before -- and never call it without the caller then
    actually exiting: the helper's file swap won't start until it does."""
    script_path = write_helper_script(app_dir, staging_dir, os.getpid())
    log.info("Update staged. Relaunching via helper script to apply it...")
    launch_helper(script_path)
