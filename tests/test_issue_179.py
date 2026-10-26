"""Issue #179: emergency closures must not reconnect all session.

Pre-fix, TickWatchdog.check returned "reconnect" about every 10 minutes
whenever the schedule said open. After N fast reconnects the watchdog
retries on a slow interval (same reconnect action). A date listed in the
closures file costs no reconnects. A closures file did not exist, so
is_taifex_holiday could not express a typhoon day.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from src.live.live_runner import _TZ_TAIPEI
from src.live.tick_watchdog import TickWatchdog
from src.market_data.holidays import (
    _parse_closure_dates,
    closures_override_path,
    is_taifex_holiday,
    load_closure_override_dates,
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
    """A silent session does N fast reconnects, then slow ones, until the next open."""

    @pytest.mark.parametrize("limit,history", [(1, False), (3, False), (1, True), (3, True)])
    def test_dead_session_slow_retries_at_the_interval(self, limit, history):
        interval = TickWatchdog.SLOW_RETRY_INTERVAL
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
        fast, slow = before[:limit], before[limit:]

        assert len(fast) == limit
        assert max(fast) < _ts(_taipei_dt(2026, 10, 23, 12, 0))
        # No clock reset: every later poll is already past the 10-min
        # timeout, so the fast ones are one poll apart. History replay
        # restarts the clock, so the next fast one waits out the timeout.
        fast_step = 630 if history else 30
        assert [fast[i + 1] - fast[i] for i in range(len(fast) - 1)] == (
            [fast_step] * (limit - 1)
        )
        assert slow
        assert slow[0] - fast[-1] == interval
        assert [slow[i + 1] - slow[i] for i in range(len(slow) - 1)] == (
            [interval] * (len(slow) - 1)
        )
        # While stood down the only action is the slow reconnect. Warn
        # and resubscribe belong to the fast ladder.
        while_down = [
            action for t, action, stood in actions
            if stood and fast[-1] < t < next_open
        ]
        assert set(while_down) <= {None, "reconnect"}
        assert "reconnect" in while_down

        # Next session open restores the fast ladder, so a still-silent
        # feed may reconnect again, and only up to N before 15:25.
        assert 1 <= len(after) <= limit

    def test_default_limits_are_provisional(self):
        assert TickWatchdog.ZERO_TICK_RECONNECT_LIMIT == 3
        assert TickWatchdog.SLOW_RETRY_INTERVAL == 30 * 60
        wd = TickWatchdog()
        assert wd.zero_tick_reconnect_limit == 3
        assert wd.slow_retry_interval == 30 * 60

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
        # Saturday 02:00 is still Friday's night session, so the slow
        # retry fires. It is not a new session and not a weekend holiday.
        action, _t = _check_at(wd, _taipei_dt(2026, 10, 24, 2, 0))
        assert action == "reconnect"
        # Monday 2026-10-26 is a TAIFEX holiday (issue #198). The
        # stand-down must not release and must not count another reconnect.
        held = wd._zero_tick_reconnects
        action, _t = _check_at(wd, _taipei_dt(2026, 10, 26, 8, 50))
        assert action is None
        assert wd._stood_down is True
        assert wd._zero_tick_reconnects == held
        # Tuesday day open is the next real session.
        action, _t = _check_at(wd, _taipei_dt(2026, 10, 27, 8, 45))
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

    def test_public_holiday_costs_no_reconnect(self):
        """Issue #198: a static public holiday is closed.

        2026-01-01 is a Thursday public holiday. The schedule at 10:00
        used to look open and spend a reconnect. It now stays quiet and
        does not count a stand-down.
        """
        wd = TickWatchdog()
        wd.active = True
        dt = _taipei_dt(2026, 1, 1, 10, 0)
        wd.last_tick_time = _ts(dt) - 700
        with _patch_now(dt):
            assert wd.check(now=_ts(dt)) is None
        assert wd._zero_tick_reconnects == 0
        assert wd._stood_down is False

    def test_live_tick_restores_the_fast_ladder(self):
        wd = TickWatchdog(zero_tick_reconnect_limit=1, slow_retry_interval=1800)
        wd.active = True
        start = _taipei_dt(2026, 10, 23, 9, 0)
        wd.last_tick_time = _ts(start)
        action, _t = _check_at(wd, start, 700)
        assert action == "reconnect"
        assert wd._stood_down is True

        # Still inside the slow interval: no blind reconnect, no fast rung.
        action, _t = _check_at(wd, start, 760)
        assert action is None

        tick_at = _ts(start) + 800
        tick_dt = datetime.fromtimestamp(tick_at, tz=_TZ_TAIPEI)
        with _patch_now(tick_dt), patch("src.live.tick_watchdog.time.time", return_value=tick_at):
            wd.on_tick()
        assert wd._stood_down is False
        assert wd._zero_tick_reconnects == 0
        assert wd._last_slow_retry_at is None

        # Normal mode is the fast ladder again, well before the 30-min mark.
        action, _t = _check_at(wd, start, 800 + 150)
        assert action == "warn"
        action, _t = _check_at(wd, start, 800 + 360)
        assert action == "resubscribe"
        action, _t = _check_at(wd, start, 800 + 700)
        assert action == "reconnect"
        assert 700 < wd.slow_retry_interval

    def test_same_timestamp_counts_once(self):
        wd = TickWatchdog(zero_tick_reconnect_limit=3)
        wd.active = True
        dt = _taipei_dt(2026, 10, 23, 10, 0)
        t = _ts(dt)
        wd.last_tick_time = t - 700
        with _patch_now(dt):
            assert wd.check(now=t) == "reconnect"
            assert wd.check(now=t) == "reconnect"
        assert wd._zero_tick_reconnects == 1

    def test_stale_replay_is_not_passed_as_a_live_tick(self):
        text = (Path(__file__).resolve().parents[1] / "run_backtest.py").read_text(
            encoding="utf-8")
        assert "live=tick_age <= HISTORY_STALENESS_SECONDS" in text

    def test_slow_retry_uses_the_link_host_reconnect_path(self):
        text = (Path(__file__).resolve().parents[1] / "run_backtest.py").read_text(
            encoding="utf-8")
        start = text.index('elif action == "reconnect":')
        handler = text[start:text.index('elif action == "resubscribe":')]
        assert "_on_disconnected()" in handler
        disc = text.index("def _on_disconnected")
        body = text[disc:text.index("def _manual_reconnect")]
        assert "note_connection" in body


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

    def test_non_utf8_bytes_return_no_dates_and_warn(self, closure_file, caplog):
        # A latin-1 fallback would keep the date. UTF-8 must not.
        closure_file.write_bytes(b"\xff\xfe2026-04-16\n")
        with caplog.at_level(logging.WARNING, logger="src.market_data.holidays"):
            assert load_closure_override_dates() == frozenset()
            assert is_taifex_holiday(date(2026, 4, 16)) is False
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert len(warnings) == 1
        assert "unreadable" in warnings[0].getMessage()

    def test_yaml_parse_error_returns_no_dates_and_warns(self, closure_file, caplog):
        # The first line is a real date. A regex fallback would keep it.
        closure_file.write_text("2026-04-16\n:\n  - [\n", encoding="utf-8")
        with caplog.at_level(logging.WARNING, logger="src.market_data.holidays"):
            assert load_closure_override_dates() == frozenset()
            assert is_taifex_holiday(date(2026, 4, 16)) is False
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert len(warnings) == 1
        assert "unreadable" in warnings[0].getMessage()

    def test_value_error_returns_no_dates_and_warns(self, closure_file, caplog, monkeypatch):
        closure_file.write_text("2026-04-16\n", encoding="utf-8")
        real = Path.read_text

        def boom(self, *args, **kwargs):
            if self == closure_file:
                raise ValueError("bad closure value")
            return real(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", boom)
        with caplog.at_level(logging.WARNING, logger="src.market_data.holidays"):
            assert load_closure_override_dates() == frozenset()
            assert is_taifex_holiday(date(2026, 4, 16)) is False
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert len(warnings) == 1


class TestListedClosureCostsNoReconnects:
    def test_listed_date_costs_zero_reconnects(self, closure_file, caplog):
        closure_file.write_text("2026-10-23\n", encoding="utf-8")
        wd = TickWatchdog(zero_tick_reconnect_limit=1, slow_retry_interval=1800)
        wd.active = True
        start = _taipei_dt(2026, 10, 23, 9, 0)
        wd.last_tick_time = _ts(start)
        with caplog.at_level(logging.WARNING, logger="src.market_data.holidays"):
            for delta in (700, 700 + 1800, 700 + 3600):
                action, _t = _check_at(wd, start, delta)
                assert action is None
            # Same open date's night session is the same closure.
            night = _taipei_dt(2026, 10, 23, 22, 0)
            wd.last_tick_time = _ts(night) - timedelta(hours=2).total_seconds()
            action, _t = _check_at(wd, night)
            assert action is None
        assert wd._zero_tick_reconnects == 0
        holiday_logs = [r for r in caplog.records if r.getMessage().startswith("holiday")]
        assert holiday_logs
