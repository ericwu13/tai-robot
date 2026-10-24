"""Issue #179: emergency closures must not reconnect all session.

Pre-fix, TickWatchdog.check returned "reconnect" for the whole scheduled
session whenever the feed was silent. A closures file did not exist, so
is_taifex_holiday could not express a typhoon day.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from src.live.live_runner import _TZ_TAIPEI
from src.live.tick_watchdog import TickWatchdog
from src.market_data.holidays import (
    _parse_closure_dates,
    closures_override_path,
    is_taifex_holiday,
)


def _taipei_dt(year, month, day, hour, minute, second=0):
    return datetime(year, month, day, hour, minute, second, tzinfo=_TZ_TAIPEI)


def _ts(dt: datetime) -> float:
    return dt.timestamp()


def _patch_now(dt):
    return patch("src.live.live_runner._taipei_now", return_value=dt)


def _check_at(wd: TickWatchdog, dt: datetime, delta: float = 0):
    t = _ts(dt) + delta
    moment = datetime.fromtimestamp(t, tz=_TZ_TAIPEI)
    with _patch_now(moment), patch("src.live.tick_watchdog.time.time", return_value=t):
        return wd.check(now=t), t


def _drive(wd: TickWatchdog, start: datetime, end: datetime, *, history: bool):
    """30s polls, the live status cadence. Returns (timestamp, action)."""
    base = _ts(start)
    stop = _ts(end)
    actions = []
    i = 0
    while True:
        t = base + i * 30
        if t > stop:
            break
        dt = datetime.fromtimestamp(t, tz=_TZ_TAIPEI)
        with _patch_now(dt), patch("src.live.tick_watchdog.time.time", return_value=t):
            action = wd.check(now=t)
            actions.append((t, action, wd._stood_down))
            if action == "reconnect" and history:
                # Stale replay restarts the clock and must not clear the streak.
                wd.on_resubscribe()
                wd.set_grace(30)
                wd.on_tick(live=False)
            elif action == "session_resubscribe":
                wd.set_grace(30)
                wd.on_session_resubscribe()
        i += 1
    return actions


class TestZeroTickStanddown:
    """A simulated closed day reconnects at most N times, then idles."""

    @pytest.mark.parametrize("limit,history", [(1, False), (3, False), (1, True), (3, True)])
    def test_closed_day_at_most_n_then_none_until_next_open(self, limit, history):
        wd = TickWatchdog(zero_tick_reconnect_limit=limit)
        wd.active = True
        # Friday 2026-10-23: schedule says open (typhoon day was not a
        # static holiday). Subscription clock at the day open, then silence.
        start = _taipei_dt(2026, 10, 23, 8, 45)
        wd.last_tick_time = _ts(start)
        actions = _drive(
            wd, start, _taipei_dt(2026, 10, 23, 15, 25), history=history,
        )

        next_open = _ts(_taipei_dt(2026, 10, 23, 15, 0))
        reconnects = [t for t, action, _stood in actions if action == "reconnect"]
        before = [t for t in reconnects if t < next_open]
        after = [t for t in reconnects if t >= next_open]

        assert len(before) == limit
        assert max(before) < _ts(_taipei_dt(2026, 10, 23, 12, 0))
        # The climb after reconnect N still warns until the next reconnect
        # would fire; that decision stands down. From then until the next
        # open the watchdog is idle.
        idle = [
            action for t, action, stood in actions
            if stood and max(before) < t < next_open
        ]
        assert idle
        assert set(idle) == {None}

        # Next session open resets the counter, so a still-silent feed
        # may reconnect again, and only up to N.
        assert 1 <= len(after) <= limit

    def test_default_limit_is_provisional_three(self):
        assert TickWatchdog.ZERO_TICK_RECONNECT_LIMIT == 3
        assert TickWatchdog().zero_tick_reconnect_limit == 3

    def test_live_tick_breaks_the_consecutive_streak(self):
        wd = TickWatchdog(zero_tick_reconnect_limit=2)
        wd.active = True
        start = _taipei_dt(2026, 10, 23, 9, 0)
        base = _ts(start)
        wd.last_tick_time = base
        reconnects = 0
        for i in range(70):
            t = base + i * 30
            dt = datetime.fromtimestamp(t, tz=_TZ_TAIPEI)
            with _patch_now(dt), patch("src.live.tick_watchdog.time.time", return_value=t):
                action = wd.check(now=t)
                if action != "reconnect":
                    continue
                reconnects += 1
                wd.on_tick(live=False)
                if reconnects == 2:
                    wd.on_tick(live=True)
        assert reconnects == 3

    def test_standdown_holds_across_midnight_and_resets_next_open(self):
        wd = TickWatchdog(zero_tick_reconnect_limit=1)
        wd.active = True
        tick = _taipei_dt(2026, 10, 23, 22, 0)
        wd.last_tick_time = _ts(tick)

        action, _t = _check_at(wd, tick, 700)
        assert action == "reconnect"
        action, _t = _check_at(wd, tick, 730)
        assert action is None
        # Saturday 02:00 is still Friday's night session.
        action, _t = _check_at(wd, _taipei_dt(2026, 10, 24, 2, 0))
        assert action is None
        # Monday day open is the next session.
        action, _t = _check_at(wd, _taipei_dt(2026, 10, 26, 8, 50))
        assert action == "session_resubscribe"

    def test_reset_clears_standdown_inside_the_session(self):
        wd = TickWatchdog(zero_tick_reconnect_limit=1)
        wd.active = True
        start = _taipei_dt(2026, 10, 23, 10, 0)
        wd.last_tick_time = _ts(start)
        action, _t = _check_at(wd, start, 700)
        assert action == "reconnect"
        action, _t = _check_at(wd, start, 730)
        assert action is None
        wd.reset()
        wd.active = True
        wd.last_tick_time = _ts(start) + 730
        action, _t = _check_at(wd, start, 730 + 700)
        assert action == "reconnect"

    def test_public_holiday_schedule_still_allows_one_reconnect(self):
        """The static calendar is not wired into the watchdog.

        2026-01-01 is a Thursday public holiday. The schedule at 10:00 is
        still open, so the first stale-tick reconnect is unchanged.
        """
        wd = TickWatchdog()
        wd.active = True
        dt = _taipei_dt(2026, 1, 1, 10, 0)
        wd.last_tick_time = _ts(dt) - 700
        with _patch_now(dt):
            assert wd.check(now=_ts(dt)) == "reconnect"

    def test_stale_replay_is_not_passed_as_a_live_tick(self):
        text = (Path(__file__).resolve().parents[1] / "run_backtest.py").read_text(
            encoding="utf-8")
        assert "live=tick_age <= HISTORY_STALENESS_SECONDS" in text


@pytest.fixture
def closure_file(tmp_path, monkeypatch):
    import src.market_data.holidays as hol

    path = tmp_path / "taifex_closures.yaml"
    monkeypatch.setattr(hol, "closures_override_path", lambda: path)
    hol._closures_cache = None
    hol._logged_closure_dates.clear()
    yield path
    hol._closures_cache = None
    hol._logged_closure_dates.clear()


class TestClosureOverride:
    def test_listed_date_returns_true_and_logs_holiday(self, closure_file, caplog):
        # 2026-04-16 is a Thursday and not a static TAIFEX holiday.
        closure_file.write_text("2026-04-16\n", encoding="utf-8")
        with caplog.at_level(logging.WARNING, logger="src.market_data.holidays"):
            assert is_taifex_holiday(date(2026, 4, 16)) is True
        assert "holiday" in caplog.text

    def test_logs_holiday_once_per_date(self, closure_file, caplog):
        closure_file.write_text("2026-04-16\n", encoding="utf-8")
        with caplog.at_level(logging.WARNING, logger="src.market_data.holidays"):
            assert is_taifex_holiday(date(2026, 4, 16)) is True
            assert is_taifex_holiday(date(2026, 4, 16)) is True
        holiday_logs = [r for r in caplog.records if r.getMessage().startswith("holiday")]
        assert len(holiday_logs) == 1

    def test_session_word_still_closes_the_whole_date(self, closure_file):
        closure_file.write_text("2026-04-16 NIGHT\n", encoding="utf-8")
        assert is_taifex_holiday(date(2026, 4, 16)) is True

    def test_yaml_list_form(self, closure_file):
        closure_file.write_text('dates:\n  - "2026-04-16"\n', encoding="utf-8")
        assert is_taifex_holiday(date(2026, 4, 16)) is True

    def test_other_dates_stay_open(self, closure_file):
        closure_file.write_text("2026-04-16\n", encoding="utf-8")
        assert is_taifex_holiday(date(2026, 4, 16)) is True
        assert is_taifex_holiday(date(2026, 4, 17)) is False

    def test_comments_and_bad_tokens_are_ignored(self, closure_file):
        closure_file.write_text(
            "# 2026-04-16\n\nnot-a-date\n2026-13-40\n",
            encoding="utf-8",
        )
        assert is_taifex_holiday(date(2026, 4, 16)) is False

    def test_missing_or_empty_file_leaves_the_calendar_unchanged(self, closure_file, caplog):
        with caplog.at_level(logging.WARNING, logger="src.market_data.holidays"):
            assert is_taifex_holiday(date(2026, 4, 16)) is False
            assert is_taifex_holiday(date(2026, 4, 11)) is True  # Saturday
            assert is_taifex_holiday(date(2026, 1, 1)) is True  # New Year
            assert is_taifex_holiday(date(2026, 10, 23)) is False
        assert "holiday" not in caplog.text

        closure_file.write_text("\n", encoding="utf-8")
        assert is_taifex_holiday(date(2026, 4, 16)) is False
        assert is_taifex_holiday(date(2026, 1, 1)) is True

    def test_rewrite_is_picked_up(self, closure_file):
        closure_file.write_text("2026-04-16\n", encoding="utf-8")
        assert is_taifex_holiday(date(2026, 4, 16)) is True
        closure_file.write_text("2026-04-17\n", encoding="utf-8")
        os.utime(closure_file, (time.time() + 10, time.time() + 10))
        assert is_taifex_holiday(date(2026, 4, 16)) is False
        assert is_taifex_holiday(date(2026, 4, 17)) is True

    def test_example_file_ships_no_closure_dates(self):
        example = Path(__file__).resolve().parents[1] / "taifex_closures.example.yaml"
        text = example.read_text(encoding="utf-8")
        assert "2026-10-23" not in text
        assert _parse_closure_dates(text) == frozenset()

    def test_default_path_is_repo_root_yaml(self):
        path = closures_override_path()
        assert path.name == "taifex_closures.yaml"
        assert path.parent == Path(__file__).resolve().parents[1]
