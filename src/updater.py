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
import time
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

# Sustained rate below this for SLOW_RATE_WINDOW_S aborts the TCP so the
# next attempt can land on a different CDN edge (issue #139). Reporter
# measured bad Fastly edges at ~57–80 KB/s and good ones at ~13 MB/s.
SLOW_RATE_MIN_BPS = 128_000
SLOW_RATE_WINDOW_S = 8.0
MAX_DOWNLOAD_ATTEMPTS = 4
RANGE_WORKERS = 4
MIN_PARALLEL_BYTES = 1_048_576  # 1 MiB — skip Range fan-out for tiny files


class SlowDownloadError(Exception):
    """One TCP connection stayed below ``min_bps`` for the whole window."""


class RateWatch:
    """Abort a stream that stays below ``min_bps`` for ``window_s``.

    The first ``window_s`` after construction (or ``reset()``) is a grace
    period so TLS / TTFB / a tiny first chunk cannot false-trigger. After
    that, if ``bytes / elapsed < min_bps``, ``feed`` raises
    ``SlowDownloadError``. Instant-complete downloads never trip this
    because they finish inside the grace window.
    """

    def __init__(
        self,
        min_bps: float = SLOW_RATE_MIN_BPS,
        window_s: float = SLOW_RATE_WINDOW_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.min_bps = float(min_bps)
        self.window_s = float(window_s)
        self.clock = clock
        self.reset()

    def reset(self) -> None:
        self._t0 = self.clock()
        self._bytes = 0

    def feed(self, n: int) -> None:
        if n < 0:
            raise ValueError("n must be >= 0")
        self._bytes += n
        if self.min_bps <= 0:
            return
        elapsed = self.clock() - self._t0
        if elapsed < self.window_s:
            return
        rate = self._bytes / elapsed
        if rate < self.min_bps:
            raise SlowDownloadError(
                f"download {rate:.0f} B/s < {self.min_bps:.0f} B/s "
                f"over {elapsed:.1f}s"
            )


def _total_from_content_range(value: str) -> int:
    """Parse the total size from a Content-Range header, or 0 if unknown."""
    if "/" not in value:
        return 0
    tail = value.rsplit("/", 1)[-1].strip()
    return int(tail) if tail.isdigit() else 0


def _split_ranges(total: int, n: int) -> list[tuple[int, int]]:
    """Inclusive ``(start, end)`` byte ranges covering ``[0, total)``."""
    n = max(1, min(int(n), total))
    base, extra = divmod(total, n)
    ranges: list[tuple[int, int]] = []
    cursor = 0
    for i in range(n):
        size = base + (1 if i < extra else 0)
        ranges.append((cursor, cursor + size - 1))
        cursor += size
    return ranges


def _interpret_response(
    resp, resume_from: int,
) -> tuple[int, int, bool]:
    """Return ``(total, write_start, append)`` from a streaming response.

    A 206 + resume appends; a 200 (server ignored Range) restarts at 0.
    """
    content_length = int(resp.headers.get("Content-Length") or 0)
    content_range = resp.headers.get("Content-Range") or ""
    ranged_total = _total_from_content_range(content_range)
    if resume_from > 0 and getattr(resp, "status_code", 200) == 206:
        total = ranged_total or (resume_from + content_length)
        return total, resume_from, True
    total = ranged_total or content_length
    return total, 0, False


def _write_body(
    resp,
    dest_path: str,
    *,
    start: int,
    append: bool,
    total: int,
    progress_cb: Callable[[int, int], None] | None,
    watch: RateWatch | None,
    chunk_cb: Callable[[int], None] | None = None,
) -> int:
    """Write ``resp.iter_bytes()`` to ``dest_path``. Returns bytes on disk."""
    mode = "ab" if append else "wb"
    done = start
    with open(dest_path, mode) as f:
        for chunk in resp.iter_bytes(chunk_size=64 * 1024):
            if not chunk:
                continue
            if total and done + len(chunk) > total:
                chunk = chunk[: total - done]
                if not chunk:
                    return done
            f.write(chunk)
            done += len(chunk)
            if chunk_cb is not None:
                chunk_cb(len(chunk))
            elif progress_cb is not None:
                progress_cb(done, total)
            if total and done >= total:
                return done
            if watch is not None:
                watch.feed(len(chunk))
    return done


def _accepts_ranges(resp) -> bool:
    return "bytes" in (resp.headers.get("Accept-Ranges") or "").lower()


def _download_one_range(
    url: str,
    part_path: str,
    start: int,
    end: int,
    *,
    timeout: float,
    min_bps: float,
    window_s: float,
    max_attempts: int,
    clock: Callable[[], float],
    chunk_cb: Callable[[int], None] | None,
) -> None:
    """Fetch ``bytes=start-end`` into ``part_path``, resuming on slow abort."""
    import httpx

    expected = end - start + 1
    last_err: Exception | None = None
    for _attempt in range(max_attempts):
        already = (
            os.path.getsize(part_path) if os.path.isfile(part_path) else 0
        )
        if already >= expected:
            return
        cursor = start + already
        watch = RateWatch(min_bps, window_s, clock=clock)
        headers = {"Range": f"bytes={cursor}-{end}"}
        try:
            with httpx.stream(
                "GET", url, headers=headers,
                follow_redirects=True, timeout=timeout,
            ) as resp:
                resp.raise_for_status()
                if resp.status_code != 206:
                    raise SlowDownloadError(
                        "server ignored HTTP Range; cannot write a slice"
                    )
                _write_body(
                    resp, part_path,
                    start=already, append=already > 0,
                    total=expected, progress_cb=None, watch=watch,
                    chunk_cb=chunk_cb,
                )
            have = (
                os.path.getsize(part_path) if os.path.isfile(part_path) else 0
            )
            if have >= expected:
                return
            last_err = SlowDownloadError(
                f"short range read {have}/{expected} for bytes={start}-{end}"
            )
        except SlowDownloadError as exc:
            last_err = exc
            continue
    raise SlowDownloadError(
        f"range {start}-{end} stalled after {max_attempts} attempts"
    ) from last_err


def _download_parallel(
    url: str,
    dest_path: str,
    total: int,
    n_parts: int,
    *,
    progress_cb: Callable[[int, int], None] | None,
    timeout: float,
    min_bps: float,
    window_s: float,
    max_attempts: int,
    clock: Callable[[], float],
) -> None:
    """HTTP Range fan-out (issue #139). Each part retries a slow edge."""
    import threading
    from concurrent.futures import ThreadPoolExecutor, as_completed

    ranges = _split_ranges(total, n_parts)
    part_paths = [f"{dest_path}.part{i}" for i in range(len(ranges))]
    done_lock = threading.Lock()
    done_box = [0]

    def on_chunk(n: int) -> None:
        with done_lock:
            done_box[0] += n
            cur = done_box[0]
        if progress_cb is not None:
            progress_cb(cur, total)

    def worker(part_path: str, start: int, end: int) -> None:
        _download_one_range(
            url, part_path, start, end,
            timeout=timeout, min_bps=min_bps, window_s=window_s,
            max_attempts=max_attempts, clock=clock, chunk_cb=on_chunk,
        )

    try:
        with ThreadPoolExecutor(max_workers=len(ranges)) as pool:
            futs = [
                pool.submit(worker, path, start, end)
                for path, (start, end) in zip(part_paths, ranges)
            ]
            for fut in as_completed(futs):
                fut.result()
        with open(dest_path, "wb") as out:
            for path in part_paths:
                with open(path, "rb") as inp:
                    shutil.copyfileobj(inp, out)
    finally:
        for path in part_paths:
            try:
                os.remove(path)
            except OSError:
                pass


def download_release(
    url: str,
    dest_path: str,
    progress_cb: Callable[[int, int], None] | None = None,
    timeout: float = 30.0,
    *,
    min_bps: float = SLOW_RATE_MIN_BPS,
    window_s: float = SLOW_RATE_WINDOW_S,
    max_attempts: int = MAX_DOWNLOAD_ATTEMPTS,
    range_workers: int = RANGE_WORKERS,
    min_parallel_bytes: int = MIN_PARALLEL_BYTES,
    clock: Callable[[], float] = time.monotonic,
) -> str:
    """Stream a release zip to ``dest_path`` via httpx.

    ``progress_cb(bytes_done, total)`` is called as bytes arrive; ``total``
    is 0 when the server omits Content-Length. Returns ``dest_path``.

    A connection that stays below ``min_bps`` for ``window_s`` is aborted
    and retried (new TCP, new CDN edge) up to ``max_attempts`` times.
    Partial bytes are resumed with ``Range`` when the server returns 206
    (issue #139). When the first response advertises ``Accept-Ranges`` and
    the asset is at least ``min_parallel_bytes``, the download fans out to
    ``range_workers`` parallel Range requests.
    """
    import httpx

    dest_dir = os.path.dirname(dest_path)
    if dest_dir:
        os.makedirs(dest_dir, exist_ok=True)

    last_err: Exception | None = None
    done = 0
    total = 0

    for _attempt in range(max_attempts):
        headers: dict[str, str] = {}
        if done > 0:
            headers["Range"] = f"bytes={done}-"
        watch = RateWatch(min_bps, window_s, clock=clock)
        try:
            switch_parallel = False
            with httpx.stream(
                "GET", url, headers=headers,
                follow_redirects=True, timeout=timeout,
            ) as resp:
                resp.raise_for_status()
                total, start, append = _interpret_response(resp, done)
                if (
                    start == 0
                    and getattr(resp, "status_code", 200) == 200
                    and range_workers > 1
                    and _accepts_ranges(resp)
                    and total >= min_parallel_bytes
                ):
                    switch_parallel = True
                else:
                    _write_body(
                        resp, dest_path, start=start, append=append,
                        total=total, progress_cb=progress_cb, watch=watch,
                    )
                    return dest_path
            if switch_parallel:
                _download_parallel(
                    url, dest_path, total=total, n_parts=range_workers,
                    progress_cb=progress_cb, timeout=timeout,
                    min_bps=min_bps, window_s=window_s,
                    max_attempts=max_attempts, clock=clock,
                )
                return dest_path
        except SlowDownloadError as exc:
            last_err = exc
            try:
                done = (
                    os.path.getsize(dest_path)
                    if os.path.isfile(dest_path) else 0
                )
            except OSError:
                done = 0
            continue

    raise SlowDownloadError(
        f"Download stalled below {min_bps / 1024:.0f} KB/s "
        f"after {max_attempts} attempts (slow CDN edge). Try again."
    ) from last_err


# ── progress window ────────────────────────────────────────────────

# PowerShell template for the update progress window.  Uses
# __STATUS_FILE__ and __VER_LABEL__ placeholders (no f-string braces,
# which would collide with PS's curly-brace syntax).
_PROGRESS_TEMPLATE = r"""Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

$statusFile = '__STATUS_FILE__'

$form = New-Object System.Windows.Forms.Form
$form.Text = 'tai-robot Update'
$form.ClientSize = New-Object System.Drawing.Size(370, 110)
$form.StartPosition = 'CenterScreen'
$form.FormBorderStyle = 'FixedDialog'
$form.MaximizeBox = $false
$form.MinimizeBox = $false
$form.TopMost = $true
$form.BackColor = [System.Drawing.Color]::White

$stepLabel = New-Object System.Windows.Forms.Label
$stepLabel.Text = 'Preparing update...'
$stepLabel.Location = New-Object System.Drawing.Point(20, 18)
$stepLabel.Size = New-Object System.Drawing.Size(330, 22)
$stepLabel.Font = New-Object System.Drawing.Font('Segoe UI', 10)
$form.Controls.Add($stepLabel)

$bar = New-Object System.Windows.Forms.ProgressBar
$bar.Location = New-Object System.Drawing.Point(20, 48)
$bar.Size = New-Object System.Drawing.Size(330, 22)
$bar.Style = 'Marquee'
$bar.MarqueeAnimationSpeed = 30
$form.Controls.Add($bar)

$verLabel = New-Object System.Windows.Forms.Label
$verLabel.Text = 'Updating to __VER_LABEL__'
$verLabel.Location = New-Object System.Drawing.Point(20, 78)
$verLabel.Size = New-Object System.Drawing.Size(330, 18)
$verLabel.ForeColor = [System.Drawing.Color]::Gray
$verLabel.Font = New-Object System.Drawing.Font('Segoe UI', 8.5)
$form.Controls.Add($verLabel)

$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = 250

$script:lastStatus = ''
$timer.Add_Tick({
    try {
        if (Test-Path $statusFile) {
            $s = (Get-Content $statusFile -Raw -ErrorAction SilentlyContinue)
            if ($s) { $s = $s.Trim() }
            if ($s -and $s -ne $script:lastStatus) {
                $script:lastStatus = $s
                switch ($s) {
                    'WAITING'    { $stepLabel.Text = 'Closing previous version...' }
                    'EXTRACTING' { $stepLabel.Text = 'Extracting update...' }
                    'INSTALLING' { $stepLabel.Text = 'Installing update...' }
                    'LAUNCHING'  { $stepLabel.Text = 'Launching updated app...' }
                    'DONE'       { $timer.Stop(); $form.Close() }
                    default {
                        if ($s.StartsWith('ERROR')) {
                            $timer.Stop(); $form.Close()
                        }
                    }
                }
            }
        }
    } catch {}
})

$form.Add_Shown({ $timer.Start() })

$timeout = New-Object System.Windows.Forms.Timer
$timeout.Interval = 120000
$timeout.Add_Tick({ $timeout.Stop(); $timer.Stop(); $form.Close() })
$timeout.Start()

[void]$form.ShowDialog()
$timer.Dispose()
$timeout.Dispose()
$form.Dispose()
"""


def _progress_script_content(status_file: str, version: str) -> str:
    """Return PowerShell source for the update progress window."""
    ver_label = f"v{version}" if version else "latest"
    sf = status_file.replace("'", "''")
    return _PROGRESS_TEMPLATE.replace("__STATUS_FILE__", sf).replace(
        "__VER_LABEL__", ver_label)


# ── swap-script generation ──────────────────────────────────────────

def write_swap_script(
    old_pid: int,
    zip_path: str,
    extract_dir: str,
    app_dir: str,
    new_exe_path: str,
    script_path: str,
    temp_dir: str,
    version: str = "",
) -> str:
    """Write ``apply_update.bat`` and ``progress.ps1`` for the swap phase.

    The batch waits for ``old_pid`` to exit, expands the zip, robocopy-swaps
    the payload into ``app_dir`` (excluding user state), deletes ``temp_dir``,
    and launches ``new_exe_path``. ``progress.ps1`` shows a WinForms window
    with step-by-step feedback while the batch runs. On failure the batch
    shows an error dialog and does NOT relaunch. Returns ``script_path``.
    """
    xd = " ".join(_EXCLUDE_DIRS)
    xf = " ".join(_EXCLUDE_FILES)
    payload_src = os.path.join(extract_dir, PAYLOAD_SUBDIR)

    # Call the system tools by absolute path. Bare names resolve via PATH,
    # which can be shadowed (e.g. a Unix `find`/`timeout` on PATH, or a
    # planted executable in the launch dir) — that would break the wait loop
    # or, worse, run an attacker's binary during the swap.
    sys32 = r"%SystemRoot%\System32"
    ps = f"{sys32}\\WindowsPowerShell\\v1.0\\powershell.exe"

    ver_label = f"v{version}" if version else "latest"

    status_file = os.path.join(temp_dir, "update_status.txt")
    progress_ps1 = os.path.join(temp_dir, "progress.ps1")
    os.makedirs(temp_dir, exist_ok=True)
    with open(progress_ps1, "w", encoding="utf-8") as f:
        f.write(_progress_script_content(status_file, version))

    # Batch caveat: robocopy exit codes 0-7 are success; 8+ are failures.
    # On failure we show an error dialog and skip the relaunch.
    script = f"""@echo off
setlocal
:: --- Launch progress window ---
>"{status_file}" echo WAITING
start "" {ps} -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{progress_ps1}"

:: --- Wait for the old process (PID {old_pid}) to exit ---
:WAIT
{sys32}\\tasklist.exe /FI "PID eq {old_pid}" 2>NUL | {sys32}\\find.exe /I "{old_pid}" >NUL
if not errorlevel 1 (
    {sys32}\\timeout.exe /t 1 /nobreak >NUL
    goto WAIT
)

:: --- Extract the downloaded zip ---
>"{status_file}" echo EXTRACTING
{ps} -NoProfile -Command "Expand-Archive -Path '{zip_path}' -DestinationPath '{extract_dir}' -Force"
if %ERRORLEVEL% NEQ 0 (
    >"{status_file}" echo ERROR
    {ps} -NoProfile -Command "Add-Type -AssemblyName System.Windows.Forms; [System.Windows.Forms.MessageBox]::Show('Failed to extract update archive. The previous version is still in place.', 'tai-robot Update Failed', 'OK', 'Error') | Out-Null"
    goto CLEANUP
)

:: --- Swap the install directory (preserve user state) ---
>"{status_file}" echo INSTALLING
{sys32}\\robocopy.exe "{payload_src}" "{app_dir}" /E /IS /IT /XD {xd} /XF {xf} /R:2 /W:1 /NFL /NDL /NJH /NJS >NUL
set ROBO_ERR=%ERRORLEVEL%
if %ROBO_ERR% GEQ 8 (
    >"{status_file}" echo ERROR
    {ps} -NoProfile -Command "Add-Type -AssemblyName System.Windows.Forms; [System.Windows.Forms.MessageBox]::Show('Update failed (robocopy error %ROBO_ERR%). The previous version is still in place.', 'tai-robot Update Failed', 'OK', 'Error') | Out-Null"
    goto CLEANUP
)

:: --- Launch the updated exe ---
>"{status_file}" echo LAUNCHING
start "" "{new_exe_path}"
>"{status_file}" echo DONE

:: --- Show success notification (background, non-blocking) ---
start /B "" {ps} -NoProfile -WindowStyle Hidden -Command "Add-Type -AssemblyName System.Windows.Forms; $n = New-Object System.Windows.Forms.NotifyIcon; $n.Icon = [System.Drawing.SystemIcons]::Information; $n.BalloonTipTitle = 'tai-robot'; $n.BalloonTipText = 'Updated to {ver_label} successfully!'; $n.Visible = $true; $n.ShowBalloonTip(5000); Start-Sleep 6; $n.Dispose()"

:CLEANUP
:: --- Give progress window time to close ---
{sys32}\\timeout.exe /t 1 /nobreak >NUL
:: --- Clean up the temp download folder ---
cd /d "%TEMP%"
rd /s /q "{temp_dir}"
endlocal
"""
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
        version=version,
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
