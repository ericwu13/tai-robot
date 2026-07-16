"""Self-update mechanism for tai-robot.

On startup the app checks GitHub Releases for a newer version (non-blocking,
in a background thread). When the user triggers an update, the newest release
zip is streamed to ``%TEMP%\\tai_update_<version>\\``, a batch script is written
that waits for this process to exit, robocopy-swaps the install directory
(preserving user state), cleans up, and relaunches the new exe.

Design notes
------------
- No new pip deps: streaming download uses ``httpx`` (already bundled); the
  checksum uses ``hashlib`` (stdlib).
- The GitHub owner/repo is derived from the ``origin`` remote when running
  from a source checkout, and falls back to the hard-coded default otherwise
  (the frozen exe has no .git).
- The swap runs from a detached ``.bat`` because a running exe cannot
  overwrite itself on Windows — a separate process must wait for it to exit.

The release zip lays its payload out under a top-level ``tai_backtest/``
folder (see build_release.py), so the swap source is
``<extract_dir>\\tai_backtest``.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Callable, NamedTuple

# GitHub repo this app updates from. Overridable via env for forks/testing.
GITHUB_OWNER = os.environ.get("TAI_UPDATE_OWNER", "ericwu13")
GITHUB_REPO = os.environ.get("TAI_UPDATE_REPO", "tai-robot")

# Top-level directory inside the release zip (matches build_release.py COLLECT
# name). The swap copies from ``<extract_dir>/<PAYLOAD_SUBDIR>`` into app_dir.
PAYLOAD_SUBDIR = "tai_backtest"

# Name of the exe launched after a successful swap.
EXE_NAME = "tai_backtest.exe"

# User-state directories/files that must survive an update. These are excluded
# from the robocopy swap so a new release never clobbers live data or secrets.
_EXCLUDE_DIRS = ("data", "sessions", "strategies")
_EXCLUDE_FILES = ("settings.yaml",)

_API_LATEST = "https://api.github.com/repos/{owner}/{repo}/releases/latest"


class ReleaseInfo(NamedTuple):
    version: str          # normalized, no leading "v" (e.g. "2.14.2")
    download_url: str     # browser_download_url of the release zip asset
    notes: str            # release body (markdown)


# ── version comparison ──────────────────────────────────────────────

_VERSION_RE = re.compile(r"(\d+)(?:\.(\d+))?(?:\.(\d+))?")


def _parse_version(version: str) -> tuple[int, int, int] | None:
    """Parse a version string into a (major, minor, patch) tuple.

    Tolerant of a leading ``v`` and of pre-release/build suffixes
    (``2.14.1-rc1`` → (2, 14, 1)). Missing components default to 0.
    Returns None if no numeric version can be found.
    """
    if not version:
        return None
    m = _VERSION_RE.search(version.strip())
    if not m:
        return None
    major = int(m.group(1))
    minor = int(m.group(2) or 0)
    patch = int(m.group(3) or 0)
    return (major, minor, patch)


def is_newer(latest_version: str, current_version: str) -> bool:
    """Return True if ``latest_version`` is strictly newer than ``current``.

    Unparseable inputs are treated conservatively: if ``latest`` cannot be
    parsed we return False (never offer a bogus update); if ``current``
    cannot be parsed but ``latest`` can, we treat it as newer.
    """
    latest = _parse_version(latest_version)
    if latest is None:
        return False
    current = _parse_version(current_version)
    if current is None:
        return True
    return latest > current


# ── GitHub release lookup ───────────────────────────────────────────

def get_latest_release(timeout: float = 10.0) -> ReleaseInfo | None:
    """Fetch the latest GitHub release. Returns None on any network error.

    Picks the first release asset whose name ends in ``.zip`` as the
    downloadable payload.
    """
    import httpx

    url = _API_LATEST.format(owner=GITHUB_OWNER, repo=GITHUB_REPO)
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        resp = httpx.get(url, headers=headers, timeout=timeout,
                         follow_redirects=True)
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return None

    tag = (data.get("tag_name") or data.get("name") or "").strip()
    version = tag.lstrip("vV")
    if not version:
        return None

    download_url = ""
    for asset in data.get("assets", []) or []:
        name = (asset.get("name") or "").lower()
        if name.endswith(".zip"):
            download_url = asset.get("browser_download_url", "")
            break
    if not download_url:
        return None

    notes = data.get("body") or ""
    return ReleaseInfo(version=version, download_url=download_url, notes=notes)


# ── download ────────────────────────────────────────────────────────

def download_release(
    url: str,
    dest_path: str,
    progress_cb: Callable[[int, int], None] | None = None,
    timeout: float = 30.0,
) -> str:
    """Stream a release zip to ``dest_path`` via httpx.

    ``progress_cb(bytes_done, total)`` is called as bytes arrive; ``total``
    is 0 when the server omits Content-Length. Returns ``dest_path``.
    """
    import httpx

    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    with httpx.stream("GET", url, follow_redirects=True, timeout=timeout) as resp:
        resp.raise_for_status()
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        with open(dest_path, "wb") as f:
            for chunk in resp.iter_bytes(chunk_size=64 * 1024):
                f.write(chunk)
                done += len(chunk)
                if progress_cb is not None:
                    progress_cb(done, total)
    return dest_path


# ── swap-script generation ──────────────────────────────────────────

def write_swap_script(
    old_pid: int,
    zip_path: str,
    extract_dir: str,
    app_dir: str,
    new_exe_path: str,
    script_path: str,
    temp_dir: str,
) -> str:
    """Write ``apply_update.bat`` that swaps the install after this exits.

    The script waits for ``old_pid`` to exit, expands the zip, robocopy-swaps
    the payload into ``app_dir`` (excluding user state), deletes ``temp_dir``,
    and launches ``new_exe_path``. Returns ``script_path``.
    """
    xd = " ".join(_EXCLUDE_DIRS)
    xf = " ".join(_EXCLUDE_FILES)
    payload_src = os.path.join(extract_dir, PAYLOAD_SUBDIR)

    # Call the system tools by absolute path. Bare names resolve via PATH,
    # which can be shadowed (e.g. a Unix `find`/`timeout` on PATH, or a
    # planted executable in the launch dir) — that would break the wait loop
    # or, worse, run an attacker's binary during the swap.
    sys32 = r"%SystemRoot%\System32"

    # Batch caveat: robocopy exit codes 0-7 are success; 8+ are failures. We
    # only relaunch when the copy did not hard-fail.
    script = f"""@echo off
setlocal
:: --- Wait for the old process (PID {old_pid}) to exit ---
:WAIT
{sys32}\\tasklist.exe /FI "PID eq {old_pid}" 2>NUL | {sys32}\\find.exe /I "{old_pid}" >NUL
if not errorlevel 1 (
    {sys32}\\timeout.exe /t 1 /nobreak >NUL
    goto WAIT
)

:: --- Extract the downloaded zip ---
{sys32}\\WindowsPowerShell\\v1.0\\powershell.exe -NoProfile -Command "Expand-Archive -Path '{zip_path}' -DestinationPath '{extract_dir}' -Force"

:: --- Swap the install directory (preserve user state) ---
{sys32}\\robocopy.exe "{payload_src}" "{app_dir}" /E /IS /IT /XD {xd} /XF {xf} /R:2 /W:1 /NFL /NDL /NJH /NJS >NUL
if %ERRORLEVEL% GEQ 8 (
    echo Update failed: robocopy error %ERRORLEVEL% 1>&2
    goto LAUNCH
)

:LAUNCH
:: --- Clean up the temp download folder ---
cd /d "%TEMP%"
rd /s /q "{temp_dir}"

:: --- Launch the updated exe ---
start "" "{new_exe_path}"
endlocal
"""
    os.makedirs(os.path.dirname(script_path), exist_ok=True)
    with open(script_path, "w", encoding="ascii", errors="replace") as f:
        f.write(script)
    return script_path


# ── orchestration ───────────────────────────────────────────────────

def _app_dir() -> str:
    """Directory of the running install (dir containing the exe when frozen)."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    # Source checkout: fall back to project root (parent of src/).
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def update_temp_dir(version: str) -> str:
    """Return ``%TEMP%\\tai_update_<version>`` (not created)."""
    safe = re.sub(r"[^0-9A-Za-z._-]", "_", version)
    return os.path.join(tempfile.gettempdir(), f"tai_update_{safe}")


def launch_update(
    zip_url: str,
    version: str,
    progress_cb: Callable[[int, int], None] | None = None,
) -> None:
    """Download the release, write the swap script, launch it detached, exit.

    This does NOT return under normal operation — it calls ``os._exit(0)``
    after launching the detached batch so the old exe releases its files for
    the swap. ``os._exit`` (not ``sys.exit``) is used because the UI invokes
    this from a background daemon thread: ``sys.exit`` only unwinds the calling
    thread, leaving the process — and its file locks — alive so the swap batch
    waits forever for the PID to die. ``os._exit`` terminates the whole process
    immediately from any thread. Exceptions before launch propagate to the
    caller.

    Refuses to run unless frozen: from a source checkout ``_app_dir()`` is the
    repo root, so the swap would robocopy a release over the working tree and
    relaunch a nonexistent exe. Self-update only makes sense for the packaged
    build.
    """
    if not getattr(sys, "frozen", False):
        raise RuntimeError(
            "Self-update is only available in the packaged app "
            "(running from source).")

    temp_dir = update_temp_dir(version)
    # Clear any stale/partial download from a prior attempt so re-downloading
    # never trips over a leftover (best-effort — a locked file is left as-is).
    shutil.rmtree(temp_dir, ignore_errors=True)
    os.makedirs(temp_dir, exist_ok=True)
    zip_path = os.path.join(temp_dir, f"tai_backtest_v{version}.zip")
    extract_dir = os.path.join(temp_dir, "extracted")
    script_path = os.path.join(temp_dir, "apply_update.bat")

    download_release(zip_url, zip_path, progress_cb)

    app_dir = _app_dir()
    new_exe_path = os.path.join(app_dir, EXE_NAME)

    write_swap_script(
        old_pid=os.getpid(),
        zip_path=zip_path,
        extract_dir=extract_dir,
        app_dir=app_dir,
        new_exe_path=new_exe_path,
        script_path=script_path,
        temp_dir=temp_dir,
    )

    # Launch the swap batch so it survives this process exiting.
    #
    # Use CREATE_NO_WINDOW, NOT DETACHED_PROCESS: a detached process has no
    # console, and `timeout.exe` in the wait loop refuses to run without one
    # ("Input redirection is not supported"), which silently aborts the whole
    # swap. CREATE_NO_WINDOW allocates a hidden console so the batch tools
    # work, while keeping the window invisible. Windows does not kill child
    # processes when the parent exits, so it keeps running after we exit.
    CREATE_NO_WINDOW = 0x08000000
    CREATE_NEW_PROCESS_GROUP = 0x00000200
    subprocess.Popen(
        ["cmd", "/c", script_path],
        creationflags=CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP,
        close_fds=True,
    )
    # os._exit (not sys.exit) so the process actually dies even when this runs
    # in a background daemon thread — sys.exit only unwinds the calling thread,
    # leaving the exe alive and the swap batch waiting forever for the PID.
    os._exit(0)


# ── startup cleanup ─────────────────────────────────────────────────

def cleanup_stale_updates() -> None:
    """Delete leftover ``tai_update_*`` folders in %TEMP%. Best-effort.

    Called on startup so a prior update's temp files don't accumulate. Never
    raises — cleanup failures (e.g. a folder still locked) are ignored.
    """
    temp_root = tempfile.gettempdir()
    try:
        entries = os.listdir(temp_root)
    except OSError:
        return
    for name in entries:
        if not name.startswith("tai_update_"):
            continue
        path = os.path.join(temp_root, name)
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
