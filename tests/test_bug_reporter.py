"""Tests for bug_reporter — debug zip creation and GitHub issue URL building."""

from __future__ import annotations

import os
import zipfile
from datetime import datetime

import pytest

from src.live.bug_reporter import build_bug_report, BugReport


@pytest.fixture
def bot_dir(tmp_path):
    """Create a fake bot directory with sample files."""
    d = tmp_path / "TMF1_test_bot"
    d.mkdir()
    (d / "session.json").write_text('{"strategy": "test"}')
    (d / "decisions.csv").write_text("time,action\n12:00,ENTRY\n")
    (d / "bars_1m_20260325.csv").write_text("time,o,h,l,c,v\n")
    (d / "debug.log").write_text("line1\nline2\n")
    # Should be excluded
    (d / "bug_report_old.zip").write_bytes(b"old zip")
    (d / "notes.txt").write_text("not collected")
    return str(d)


class TestBuildBugReport:

    def test_collects_files(self, bot_dir, tmp_path):
        report = build_bug_report(
            bot_dir=bot_dir,
            strategy="SMA Cross",
            symbol="TMF00",
            mode="semi_auto",
            position=1,
            app_version="2.4.1",
            now=datetime(2026, 3, 25, 13, 30, 0),
        )
        assert report is not None
        assert report.files_added == 4  # .json, .csv, .csv, .log
        assert os.path.exists(report.zip_path)

        with zipfile.ZipFile(report.zip_path) as zf:
            names = zf.namelist()
            assert "session.json" in names
            assert "decisions.csv" in names
            assert "debug.log" in names
            # Excluded files
            assert "bug_report_old.zip" not in names
            assert "notes.txt" not in names

    def test_includes_strategy_code(self, bot_dir):
        report = build_bug_report(
            bot_dir=bot_dir,
            strategy="Test",
            symbol="TX00",
            mode="paper",
            position=0,
            strategy_code="class MyStrategy: pass",
            now=datetime(2026, 3, 25, 13, 30, 0),
        )
        assert report.files_added == 5  # 4 files + strategy_code.py
        with zipfile.ZipFile(report.zip_path) as zf:
            assert "strategy_code.py" in zf.namelist()
            assert zf.read("strategy_code.py") == b"class MyStrategy: pass"

    def test_no_files_returns_none(self, tmp_path):
        empty_dir = tmp_path / "empty_bot"
        empty_dir.mkdir()
        report = build_bug_report(
            bot_dir=str(empty_dir),
            strategy="Test",
            symbol="TX00",
            mode="paper",
            position=0,
            now=datetime(2026, 3, 25, 13, 30, 0),
        )
        assert report is None
        # Zip should be cleaned up
        assert not any(f.endswith(".zip") for f in os.listdir(str(empty_dir)))

    def test_no_bot_dir(self, tmp_path, monkeypatch):
        """No bot dir → zip goes to data/ folder."""
        monkeypatch.chdir(tmp_path)
        report = build_bug_report(
            bot_dir=None,
            strategy="Test",
            symbol="TX00",
            mode="paper",
            position=0,
            strategy_code="code = True",
            now=datetime(2026, 3, 25, 13, 30, 0),
        )
        assert report is not None
        assert "data" in report.zip_path
        assert report.files_added == 1  # only strategy_code.py

    def test_issue_url_format(self, bot_dir):
        report = build_bug_report(
            bot_dir=bot_dir,
            strategy="AI: TrendFollower",
            symbol="TMF00",
            mode="semi_auto",
            position=-1,
            app_version="2.4.1",
            now=datetime(2026, 3, 25, 13, 30, 0),
        )
        assert "github.com/ericwu13/tai-robot/issues/new" in report.issue_url
        assert "title=" in report.issue_url
        assert "body=" in report.issue_url
        assert "TrendFollower" in report.title

    def test_url_not_too_long(self, bot_dir):
        """URL should stay under browser limits (no log dump)."""
        report = build_bug_report(
            bot_dir=bot_dir,
            strategy="A" * 200,  # long strategy name
            symbol="TMF00",
            mode="semi_auto",
            position=0,
            app_version="2.4.1",
            now=datetime(2026, 3, 25, 13, 30, 0),
        )
        assert len(report.issue_url) < 8000

    def test_custom_repo(self, bot_dir):
        report = build_bug_report(
            bot_dir=bot_dir,
            strategy="Test",
            symbol="TX00",
            mode="paper",
            position=0,
            repo="myuser/myrepo",
            now=datetime(2026, 3, 25, 13, 30, 0),
        )
        assert "myuser/myrepo" in report.issue_url

    def test_zip_timestamp(self, bot_dir):
        now = datetime(2026, 3, 25, 14, 5, 30)
        report = build_bug_report(
            bot_dir=bot_dir,
            strategy="Test",
            symbol="TX00",
            mode="paper",
            position=0,
            now=now,
        )
        assert "20260325_140530" in os.path.basename(report.zip_path)

    def test_excludes_subdirectories(self, bot_dir):
        """Subdirectories in bot_dir should not cause errors."""
        os.makedirs(os.path.join(bot_dir, "subdir"))
        report = build_bug_report(
            bot_dir=bot_dir,
            strategy="Test",
            symbol="TX00",
            mode="paper",
            position=0,
            now=datetime(2026, 3, 25, 13, 30, 0),
        )
        assert report is not None
        assert report.files_added == 4  # same as before, subdir ignored
