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

class TestDownloadRelease:
    def test_streams_and_reports_progress(self, tmp_path):
        chunks = [b"a" * 100, b"b" * 50]

        stream_cm = MagicMock()
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.headers = {"Content-Length": "150"}
        resp.iter_bytes.return_value = iter(chunks)
        stream_cm.__enter__.return_value = resp
        stream_cm.__exit__.return_value = False

        progress = []
        dest = str(tmp_path / "out" / "app.zip")
        with patch("httpx.stream", return_value=stream_cm):
            updater.download_release(
                "https://x/app.zip", dest,
                progress_cb=lambda d, t: progress.append((d, t)))

        assert os.path.isfile(dest)
        with open(dest, "rb") as f:
            assert f.read() == b"a" * 100 + b"b" * 50
        assert progress == [(100, 150), (150, 150)]
