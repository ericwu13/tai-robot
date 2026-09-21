"""Tests for src.updater — version comparison, release lookup, swap script.

The updater is GUI-independent. These tests verify semver comparison,
GitHub release parsing (with mocked httpx), swap-script generation, temp-dir
naming, and stale-folder cleanup. Nothing here touches the network or spawns
a real process.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

import httpx
import pytest

from src import updater


# ── _parse_version ──

class TestParseVersion:
    def test_full(self):
        assert updater._parse_version("2.14.1") == (2, 14, 1)

    def test_leading_v(self):
        assert updater._parse_version("v2.14.1") == (2, 14, 1)

    def test_partial(self):
        assert updater._parse_version("3") == (3, 0, 0)
        assert updater._parse_version("3.2") == (3, 2, 0)

    def test_prerelease_suffix(self):
        assert updater._parse_version("2.14.1-rc1") == (2, 14, 1)

    def test_empty_and_garbage(self):
        assert updater._parse_version("") is None
        assert updater._parse_version("not-a-version") is None


# ── is_newer ──

class TestIsNewer:
    def test_patch_bump(self):
        assert updater.is_newer("2.14.2", "2.14.1") is True

    def test_minor_bump(self):
        assert updater.is_newer("2.15.0", "2.14.9") is True

    def test_major_bump(self):
        assert updater.is_newer("3.0.0", "2.99.99") is True

    def test_equal_is_not_newer(self):
        assert updater.is_newer("2.14.1", "2.14.1") is False

    def test_older_is_not_newer(self):
        assert updater.is_newer("2.14.0", "2.14.1") is False

    def test_leading_v_ignored(self):
        assert updater.is_newer("v2.14.2", "2.14.1") is True
        assert updater.is_newer("2.14.2", "v2.14.2") is False

    def test_unparseable_latest_is_conservative(self):
        # Never offer a bogus update if we can't read the remote version.
        assert updater.is_newer("garbage", "2.14.1") is False

    def test_unparseable_current_treated_as_older(self):
        assert updater.is_newer("2.14.1", "garbage") is True

    def test_partial_versions(self):
        assert updater.is_newer("2.15", "2.14.9") is True
        assert updater.is_newer("2", "2.0.0") is False


# ── get_latest_release ──

def _mock_response(payload):
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = payload
    return resp


class TestGetLatestRelease:
    def test_parses_tag_and_zip_asset(self):
        payload = {
            "tag_name": "v2.15.0",
            "body": "release notes here",
            "assets": [
                {"name": "tai_backtest_v2.15.0_win_x64.zip",
                 "browser_download_url": "https://example.com/app.zip"},
            ],
        }
        with patch("httpx.get", return_value=_mock_response(payload)):
            info = updater.get_latest_release()
        assert info is not None
        assert info.version == "2.15.0"          # 'v' stripped
        assert info.download_url == "https://example.com/app.zip"
        assert info.notes == "release notes here"

    def test_skips_non_zip_assets(self):
        payload = {
            "tag_name": "2.15.0",
            "assets": [
                {"name": "tai_backtest_v2.15.0_win_x64.zip.sha256",
                 "browser_download_url": "https://example.com/app.zip.sha256"},
                {"name": "tai_backtest_v2.15.0_win_x64.zip",
                 "browser_download_url": "https://example.com/app.zip"},
            ],
        }
        with patch("httpx.get", return_value=_mock_response(payload)):
            info = updater.get_latest_release()
        assert info is not None
        assert info.download_url == "https://example.com/app.zip"

    def test_no_zip_asset_returns_none(self):
        payload = {"tag_name": "2.15.0", "assets": [
            {"name": "notes.txt", "browser_download_url": "https://x/y.txt"}]}
        with patch("httpx.get", return_value=_mock_response(payload)):
            assert updater.get_latest_release() is None

    def test_missing_tag_returns_none(self):
        payload = {"assets": [
            {"name": "a.zip", "browser_download_url": "https://x/a.zip"}]}
        with patch("httpx.get", return_value=_mock_response(payload)):
            assert updater.get_latest_release() is None

    def test_network_error_returns_none(self):
        with patch("httpx.get", side_effect=OSError("no network")):
            assert updater.get_latest_release() is None


# ── write_swap_script ──

class TestWriteSwapScript:
    def _write(self, tmp_path):
        temp_dir = str(tmp_path / "tai_update_9.9.9")
        os.makedirs(temp_dir, exist_ok=True)
        script_path = os.path.join(temp_dir, "apply_update.bat")
        updater.write_swap_script(
            old_pid=4242,
            zip_path=os.path.join(temp_dir, "app.zip"),
            extract_dir=os.path.join(temp_dir, "extracted"),
            app_dir=r"C:\apps\tai",
            new_exe_path=r"C:\apps\tai\tai_backtest.exe",
            script_path=script_path,
            temp_dir=temp_dir,
        )
        with open(script_path, encoding="ascii") as f:
            return script_path, f.read()

    def test_waits_for_pid(self, tmp_path):
        _, content = self._write(tmp_path)
        assert 'PID eq 4242' in content
        assert ':WAIT' in content
        assert 'goto WAIT' in content

    def test_excludes_user_state(self, tmp_path):
        _, content = self._write(tmp_path)
        assert '/XD data sessions strategies' in content
        assert '/XF settings.yaml' in content

    def test_robocopy_source_is_payload_subdir(self, tmp_path):
        _, content = self._write(tmp_path)
        expected = os.path.join(
            str(tmp_path / "tai_update_9.9.9" / "extracted"), "tai_backtest")
        assert expected in content

    def test_launches_new_exe_and_cleans_temp(self, tmp_path):
        _, content = self._write(tmp_path)
        assert r'start "" "C:\apps\tai\tai_backtest.exe"' in content
        assert 'rd /s /q' in content
        assert 'Expand-Archive' in content

    def test_written_as_ascii(self, tmp_path):
        # Batch files must be ascii-safe (no BOM / unicode surprises).
        script_path, _ = self._write(tmp_path)
        with open(script_path, "rb") as f:
            raw = f.read()
        raw.decode("ascii")  # must not raise

    def test_error_dialog_on_extract_failure(self, tmp_path):
        _, content = self._write(tmp_path)
        assert 'if %ERRORLEVEL% NEQ 0' in content
        assert 'Failed to extract update archive' in content
        assert 'Update Failed' in content

    def test_error_dialog_on_robocopy_failure(self, tmp_path):
        _, content = self._write(tmp_path)
        assert 'set ROBO_ERR=%ERRORLEVEL%' in content
        assert 'if %ROBO_ERR% GEQ 8' in content
        assert 'robocopy error %ROBO_ERR%' in content

    def test_no_launch_on_failure(self, tmp_path):
        _, content = self._write(tmp_path)
        # Both error paths goto CLEANUP, which does NOT contain a start command.
        # The start command appears only before CLEANUP, not after it.
        cleanup_section = content.split(':CLEANUP')[1]
        assert 'start ""' not in cleanup_section

    def test_version_in_notification(self, tmp_path):
        temp_dir = str(tmp_path / "tai_update_2.17.0")
        os.makedirs(temp_dir, exist_ok=True)
        script_path = os.path.join(temp_dir, "apply_update.bat")
        updater.write_swap_script(
            old_pid=4242,
            zip_path=os.path.join(temp_dir, "app.zip"),
            extract_dir=os.path.join(temp_dir, "extracted"),
            app_dir=r"C:\apps\tai",
            new_exe_path=r"C:\apps\tai\tai_backtest.exe",
            script_path=script_path,
            temp_dir=temp_dir,
            version="2.17.0",
        )
        with open(script_path, encoding="ascii") as f:
            content = f.read()
        assert 'Updated to v2.17.0 successfully!' in content

    def test_default_version_label(self, tmp_path):
        _, content = self._write(tmp_path)
        assert 'Updated to latest successfully!' in content

    def test_progress_script_written(self, tmp_path):
        self._write(tmp_path)
        progress = tmp_path / "tai_update_9.9.9" / "progress.ps1"
        assert progress.exists()
        content = progress.read_text(encoding="utf-8")
        assert "update_status.txt" in content
        assert "$form" in content
        assert "ShowDialog" in content

    def test_bat_writes_status_markers(self, tmp_path):
        _, content = self._write(tmp_path)
        for marker in ("WAITING", "EXTRACTING", "INSTALLING",
                        "LAUNCHING", "DONE", "ERROR"):
            assert f"echo {marker}" in content

    def test_bat_launches_progress_window(self, tmp_path):
        _, content = self._write(tmp_path)
        assert "progress.ps1" in content
        assert "-ExecutionPolicy Bypass" in content

    def test_progress_version_label(self, tmp_path):
        temp_dir = str(tmp_path / "tai_update_3.0.0")
        os.makedirs(temp_dir, exist_ok=True)
        script_path = os.path.join(temp_dir, "apply_update.bat")
        updater.write_swap_script(
            old_pid=1,
            zip_path=os.path.join(temp_dir, "app.zip"),
            extract_dir=os.path.join(temp_dir, "extracted"),
            app_dir=r"C:\apps\tai",
            new_exe_path=r"C:\apps\tai\tai_backtest.exe",
            script_path=script_path,
            temp_dir=temp_dir,
            version="3.0.0",
        )
        ps1 = os.path.join(temp_dir, "progress.ps1")
        with open(ps1, encoding="utf-8") as f:
            content = f.read()
        assert "Updating to v3.0.0" in content

    def test_progress_error_closes_window(self, tmp_path):
        self._write(tmp_path)
        ps1 = tmp_path / "tai_update_9.9.9" / "progress.ps1"
        content = ps1.read_text(encoding="utf-8")
        assert "ERROR" in content
        assert "$form.Close()" in content


# ── update_temp_dir ──

class TestUpdateTempDir:
    def test_uses_temp_and_version(self):
        path = updater.update_temp_dir("2.15.0")
        assert path.endswith(os.path.join("", "tai_update_2.15.0")) or \
            path.endswith("tai_update_2.15.0")

    def test_sanitizes_unsafe_chars(self):
        path = updater.update_temp_dir("2.15.0/../evil")
        base = os.path.basename(path)
        assert "/" not in base and "\\" not in base
        assert base.startswith("tai_update_")


# ── cleanup_stale_updates ──

class TestCleanupStaleUpdates:
    def test_removes_only_tai_update_folders(self, tmp_path, monkeypatch):
        monkeypatch.setattr(updater.tempfile, "gettempdir",
                            lambda: str(tmp_path))
        stale = tmp_path / "tai_update_2.14.0"
        stale.mkdir()
        (stale / "app.zip").write_text("x")
        keep = tmp_path / "something_else"
        keep.mkdir()

        updater.cleanup_stale_updates()

        assert not stale.exists()
        assert keep.exists()

    def test_no_temp_dir_is_safe(self, tmp_path, monkeypatch):
        missing = tmp_path / "does_not_exist"
        monkeypatch.setattr(updater.tempfile, "gettempdir",
                            lambda: str(missing))
        # Must not raise.
        updater.cleanup_stale_updates()


# ── launch_update source-mode guard ──

class TestLaunchUpdateGuard:
    def test_refuses_from_source(self, monkeypatch):
        # Running from a source checkout (sys.frozen falsy) must not attempt
        # a swap — it would robocopy over the repo. Must raise before any I/O.
        monkeypatch.delattr(sys, "frozen", raising=False)
        called = {"download": False}
        monkeypatch.setattr(updater, "download_release",
                            lambda *a, **k: called.__setitem__("download", True))
        with pytest.raises(RuntimeError, match="packaged app"):
            updater.launch_update("https://x/app.zip", "2.15.0")
        assert called["download"] is False


# ── download_release ──

def _stream_cm(chunks, headers=None, status=200):
    """httpx.stream context-manager mock that yields ``chunks``."""
    stream_cm = MagicMock()
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.status_code = status
    resp.headers = headers or {}
    resp.iter_bytes.return_value = iter(chunks)
    stream_cm.__enter__.return_value = resp
    stream_cm.__exit__.return_value = False
    return stream_cm


class _FakeClock:
    """Monotonic clock the test advances so RateWatch can fire without sleeps."""

    def __init__(self, t=0.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


class _TimedChunks:
    """Iterator that advances ``clock`` by ``dt`` before each chunk (issue #139)."""

    def __init__(self, chunks, clock, dt):
        self._chunks = list(chunks)
        self._clock = clock
        self._dt = dt

    def __iter__(self):
        return self

    def __next__(self):
        if not self._chunks:
            raise StopIteration
        self._clock.advance(self._dt)
        return self._chunks.pop(0)


class TestRateWatch:
    """Failing-first helper tests for issue #139 (slow CDN edge abort)."""

    def test_aborts_after_sustained_low_rate(self):
        clock = _FakeClock()
        watch = updater.RateWatch(min_bps=1_000, window_s=2.0, clock=clock)
        watch.feed(100)
        clock.advance(1.0)
        watch.feed(100)          # 200 B / 1s so far — still in grace
        clock.advance(1.0)
        with pytest.raises(updater.SlowDownloadError):
            watch.feed(100)      # 300 B / 2s = 150 B/s < 1000

    def test_allows_fast_stream(self):
        clock = _FakeClock()
        watch = updater.RateWatch(min_bps=1_000, window_s=2.0, clock=clock)
        watch.feed(10_000)
        clock.advance(2.0)
        watch.feed(10_000)       # 20 KB / 2s = 10 KB/s ≥ 1 KB/s
        clock.advance(2.0)
        watch.feed(10_000)       # still well above the floor

    def test_grace_window_does_not_abort(self):
        clock = _FakeClock()
        watch = updater.RateWatch(min_bps=1_000_000, window_s=8.0, clock=clock)
        watch.feed(1)            # elapsed 0 — never abort during grace
        clock.advance(7.9)
        watch.feed(1)            # still inside window_s

    def test_reset_drops_connect_time(self):
        clock = _FakeClock()
        watch = updater.RateWatch(min_bps=128_000, window_s=8.0, clock=clock)
        clock.advance(8.5)       # TLS / 302 / TTFB
        watch.reset()            # after headers — must not count connect
        watch.feed(65_536)

    def test_fast_then_slow_aborts(self):
        clock = _FakeClock()
        watch = updater.RateWatch(min_bps=1_000, window_s=2.0, clock=clock)
        watch.feed(10_000)
        clock.advance(0.5)
        watch.feed(10_000)
        clock.advance(2.0)
        with pytest.raises(updater.SlowDownloadError):
            watch.feed(100)      # ~50 B/s over the last 2s


class TestDownloadRelease:
    def test_streams_and_reports_progress(self, tmp_path):
        chunks = [b"a" * 100, b"b" * 50]
        dest = str(tmp_path / "out" / "app.zip")
        progress = []
        with patch("httpx.stream", return_value=_stream_cm(
                chunks, {"Content-Length": "150"})):
            updater.download_release(
                "https://x/app.zip", dest,
                progress_cb=lambda d, t: progress.append((d, t)))

        assert os.path.isfile(dest)
        with open(dest, "rb") as f:
            assert f.read() == b"a" * 100 + b"b" * 50
        assert progress == [(100, 150), (150, 150)]

    def test_retries_after_slow_stream(self, tmp_path):
        # Pre-fix download_release has no RateWatch / retry: this fails
        # because the first (slow) connection is the only attempt.
        clock = _FakeClock()
        dest = str(tmp_path / "app.zip")
        payload = b"HELLO-WORLD-OK"  # 14 bytes, completes on attempt 2

        slow = _stream_cm(
            _TimedChunks([b"xx", b"yy"], clock, dt=2.0),
            {"Content-Length": str(len(payload))},
        )
        fast = _stream_cm(
            [payload],
            {"Content-Length": str(len(payload))},
        )
        with patch("httpx.stream", side_effect=[slow, fast]) as stream:
            updater.download_release(
                "https://x/app.zip", dest,
                min_bps=1_000, window_s=2.0, max_attempts=3,
                range_workers=1, clock=clock,
            )

        with open(dest, "rb") as f:
            assert f.read() == payload
        assert stream.call_count == 2

    def test_resumes_with_range_header(self, tmp_path):
        # After a slow abort the next GET must send Range: bytes=<done>-
        # and append a 206 body (issue #139 resume).
        clock = _FakeClock()
        dest = str(tmp_path / "app.zip")
        first = _stream_cm(
            _TimedChunks([b"AAAA", b"BBBB"], clock, dt=1.0),
            {"Content-Length": "12", "Accept-Ranges": "bytes"},
        )
        second = _stream_cm(
            [b"CCCC"],
            {"Content-Length": "4",
             "Content-Range": "bytes 8-11/12",
             "Accept-Ranges": "bytes"},
            status=206,
        )
        captured = []

        def fake_stream(method, url, **kwargs):
            captured.append(kwargs.get("headers") or {})
            return first if len(captured) == 1 else second

        with patch("httpx.stream", side_effect=fake_stream):
            updater.download_release(
                "https://x/app.zip", dest,
                min_bps=1_000, window_s=2.0, max_attempts=3,
                range_workers=1, clock=clock,
            )

        with open(dest, "rb") as f:
            assert f.read() == b"AAAABBBBCCCC"
        assert captured[1].get("Range") == "bytes=8-"

    def test_raises_after_max_attempts(self, tmp_path):
        # Unreachable / dead edge: last-attempt fail-open cannot help.
        # Bounded retries then the error is surfaced (F1).
        dest = str(tmp_path / "app.zip")
        calls = {"n": 0}

        def always_dead(*_a, **_k):
            calls["n"] += 1
            raise httpx.ReadTimeout("timed out")

        with patch("httpx.stream", side_effect=always_dead):
            with pytest.raises(updater.SlowDownloadError, match="timed out"):
                updater.download_release(
                    "https://x/app.zip", dest,
                    max_attempts=2, range_workers=1,
                )
        assert calls["n"] == 2

    def test_uniformly_slow_link_completes_on_final_attempt(self, tmp_path):
        # F1: a slow-but-working link must still finish (last attempt
        # unwatched). Pre-review code failed this the same way every try.
        clock = _FakeClock()
        dest = str(tmp_path / "app.zip")
        payload = b"HELLO-WORLD-OK"

        def always_slow(*_a, **_k):
            return _stream_cm(
                _TimedChunks(
                    [b"HE", b"LL", b"O-", b"WO", b"RL", b"D-", b"OK"],
                    clock, dt=2.0,
                ),
                {"Content-Length": str(len(payload))},
            )

        with patch("httpx.stream", side_effect=always_slow) as stream:
            updater.download_release(
                "https://x/app.zip", dest,
                min_bps=1_000, window_s=2.0, max_attempts=2,
                range_workers=1, clock=clock,
            )
        with open(dest, "rb") as f:
            assert f.read() == payload
        assert stream.call_count == 2

    def test_retries_transport_error_mid_stream(self, tmp_path):
        dest = str(tmp_path / "app.zip")
        payload = b"HELLO-WORLD-OK"

        def boom():
            yield b"HE"
            raise httpx.ReadTimeout("timed out")

        first = _stream_cm(
            boom(),
            {"Content-Length": str(len(payload)), "Accept-Ranges": "bytes"},
        )
        second = _stream_cm(
            [b"LLO-WORLD-OK"],
            {"Content-Length": "12",
             "Content-Range": "bytes 2-13/14",
             "Accept-Ranges": "bytes"},
            status=206,
        )
        captured = []

        def fake_stream(method, url, **kwargs):
            captured.append(kwargs.get("headers") or {})
            return first if len(captured) == 1 else second

        with patch("httpx.stream", side_effect=fake_stream):
            updater.download_release(
                "https://x/app.zip", dest,
                max_attempts=3, range_workers=1,
            )
        with open(dest, "rb") as f:
            assert f.read() == payload
        assert captured[1].get("Range") == "bytes=2-"

    def test_parallel_ranges_assemble_file(self, tmp_path):
        # Optional #139 path: first GET advertises Accept-Ranges + size,
        # then 4 Range workers write their slices. Pre-fix has no Range
        # fan-out, so this fails (only the probe body would be saved).
        data = b"ABCDEFGHIJKLMNOP"  # 16 bytes
        dest = str(tmp_path / "app.zip")
        probe = _stream_cm(
            [data],
            {"Content-Length": "16", "Accept-Ranges": "bytes"},
        )
        parts = {
            "bytes=0-3": b"ABCD",
            "bytes=4-7": b"EFGH",
            "bytes=8-11": b"IJKL",
            "bytes=12-15": b"MNOP",
        }

        def fake_stream(method, url, **kwargs):
            headers = kwargs.get("headers") or {}
            rng = headers.get("Range")
            if rng is None:
                return probe
            body = parts[rng]
            return _stream_cm(
                [body],
                {"Content-Length": str(len(body)),
                 "Content-Range": f"{rng.replace('=', ' ')}/16",
                 "Accept-Ranges": "bytes"},
                status=206,
            )

        with patch("httpx.stream", side_effect=fake_stream):
            updater.download_release(
                "https://x/app.zip", dest,
                range_workers=4, min_parallel_bytes=1,
            )

        with open(dest, "rb") as f:
            assert f.read() == data

    def test_parallel_retries_slow_part(self, tmp_path):
        # Shared fake clocks + threads race, so the slow worker raises
        # SlowDownloadError from the iterator (the same exception RateWatch
        # uses). Resume must request the remaining suffix of that part.
        data = b"ABCDEFGH"  # 8 bytes, 2 workers
        dest = str(tmp_path / "app.zip")
        probe = _stream_cm(
            [data],
            {"Content-Length": "8", "Accept-Ranges": "bytes"},
        )

        def _slow_then_raise():
            yield b"E"
            raise updater.SlowDownloadError("mock slow edge")

        def fake_stream(method, url, **kwargs):
            headers = kwargs.get("headers") or {}
            rng = headers.get("Range")
            if rng is None:
                return probe
            if rng == "bytes=0-3":
                return _stream_cm(
                    [b"ABCD"],
                    {"Content-Length": "4", "Content-Range": "bytes 0-3/8"},
                    status=206,
                )
            if rng == "bytes=4-7":
                return _stream_cm(
                    _slow_then_raise(),
                    {"Content-Length": "4", "Content-Range": "bytes 4-7/8"},
                    status=206,
                )
            if rng == "bytes=5-7":
                return _stream_cm(
                    [b"FGH"],
                    {"Content-Length": "3", "Content-Range": "bytes 5-7/8"},
                    status=206,
                )
            raise AssertionError(f"unexpected Range {rng}")

        with patch("httpx.stream", side_effect=fake_stream):
            updater.download_release(
                "https://x/app.zip", dest,
                max_attempts=3, range_workers=2, min_parallel_bytes=1,
            )

        with open(dest, "rb") as f:
            assert f.read() == data

    def test_range_ignored_falls_back_to_serial(self, tmp_path):
        # F3: Accept-Ranges advertised but ranged GET returns 200 → serial
        # fallback, not a 4×4 "slow CDN edge" loop.
        data = b"ABCDEFGH"
        dest = str(tmp_path / "app.zip")
        calls = []

        def fake_stream(method, url, **kwargs):
            rng = (kwargs.get("headers") or {}).get("Range")
            calls.append(rng)
            return _stream_cm(
                [data],
                {"Content-Length": "8", "Accept-Ranges": "bytes"},
                status=200,
            )

        with patch("httpx.stream", side_effect=fake_stream):
            updater.download_release(
                "https://x/app.zip", dest,
                max_attempts=3, range_workers=2, min_parallel_bytes=1,
            )
        with open(dest, "rb") as f:
            assert f.read() == data
        none_calls = [c for c in calls if c is None]
        assert len(none_calls) >= 2          # probe + serial fallback
        assert len(calls) <= 5               # not 4 probes + 64 ranges

    def test_parallel_failure_does_not_restart_fanout(self, tmp_path):
        # F3 restart-loop coverage: one dead part must NOT re-probe / fan
        # out from byte 0. Error is surfaced after that part's own attempts.
        dest = str(tmp_path / "app.zip")
        data = b"ABCDEFGH"
        probe_calls = []
        range_calls = []
        probe = _stream_cm(
            [data],
            {"Content-Length": "8", "Accept-Ranges": "bytes"},
        )

        def fake_stream(method, url, **kwargs):
            rng = (kwargs.get("headers") or {}).get("Range")
            if rng is None:
                probe_calls.append(1)
                return probe
            range_calls.append(rng)
            if rng.startswith("bytes=0-"):
                return _stream_cm(
                    [b"ABCD"],
                    {"Content-Length": "4", "Content-Range": "bytes 0-3/8"},
                    status=206,
                )
            raise httpx.ReadTimeout("dead edge")

        with patch("httpx.stream", side_effect=fake_stream):
            with pytest.raises(updater.SlowDownloadError, match="stalled after 3") as excinfo:
                updater.download_release(
                    "https://x/app.zip", dest,
                    max_attempts=3, range_workers=2, min_parallel_bytes=1,
                )
        assert "dead edge" in str(excinfo.value.__cause__)
        assert probe_calls == [1]
        assert range_calls.count("bytes=0-3") == 1
        assert sum(1 for r in range_calls if r.startswith("bytes=4-")) == 3

    def test_check_declared_size_rejects_oversized(self, tmp_path):
        p = tmp_path / "f.zip"
        p.write_bytes(b"12345")
        with pytest.raises(updater.OversizedDownloadError):
            updater._check_declared_size(str(p), 4)
        assert not p.exists()

    def test_write_body_clamps_to_declared_length(self, tmp_path):
        dest = str(tmp_path / "app.zip")
        with patch("httpx.stream", return_value=_stream_cm(
                [b"12345678"], {"Content-Length": "4"})):
            updater.download_release(
                "https://x/app.zip", dest, range_workers=1)
        with open(dest, "rb") as f:
            assert f.read() == b"1234"

    def test_download_rejects_file_larger_than_declared(self, tmp_path):
        dest = str(tmp_path / "app.zip")

        def write_extra(resp, dest_path, **_k):
            with open(dest_path, "wb") as f:
                f.write(b"12345678")
            return 8

        with patch.object(updater, "_write_body", side_effect=write_extra):
            with patch("httpx.stream", return_value=_stream_cm(
                    [b"xxxx"], {"Content-Length": "4"})):
                with pytest.raises(updater.OversizedDownloadError):
                    updater.download_release(
                        "https://x/app.zip", dest, range_workers=1)
        assert not os.path.isfile(dest)
