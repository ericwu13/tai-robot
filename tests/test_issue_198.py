"""Issue #198: a weekday TAIFEX holiday is not a live session.

The schedule is keyed by the session OPEN date. 2026-10-26 is the
observed day off for Taiwan Restoration Day. Monday's DAY and NIGHT
sessions are closed, and so is the Tuesday 00:00-05:00 tail of that
night. Friday 2026-10-23 night, including its Saturday tail, stays open.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

import pytest

from src.live.live_runner import (
    _TZ_TAIPEI,
    _clock_session_key,
    is_market_open,
    seconds_until_market_open,
    session_for,
)
from src.live.tick_watchdog import (
    TickWatchdog,
    _crossed_session_gap,
    _scheduled_session_key,
)


def _taipei(year, month, day, hour, minute, second=0):
    return datetime(year, month, day, hour, minute, second, tzinfo=_TZ_TAIPEI)


def _ts(dt: datetime) -> float:
    return dt.timestamp()


# Clock times that must be closed on the 2026-10-26 holiday.
_HOLIDAY_CLOSED = (
    (2026, 10, 26, 8, 45),
    (2026, 10, 26, 10, 0),
    (2026, 10, 26, 15, 0),
    (2026, 10, 26, 23, 0),
    (2026, 10, 27, 1, 0),  # tail of the holiday night
)


@pytest.mark.parametrize("parts", _HOLIDAY_CLOSED)
def test_holiday_open_date_is_closed(parts):
    dt = _taipei(*parts)
    assert session_for(dt) is None
    assert is_market_open(dt) is False
    assert _scheduled_session_key(dt) is None


def test_friday_night_stays_open_through_saturday_tail():
    """Fri 2026-10-23 night, including Sat 01:00, is one open session."""
    fri_open = _taipei(2026, 10, 23, 15, 0)
    fri_late = _taipei(2026, 10, 23, 23, 0)
    sat_tail = _taipei(2026, 10, 24, 1, 0)
    assert session_for(fri_open) == "2026-10-23|NIGHT"
    assert session_for(fri_late) == "2026-10-23|NIGHT"
    assert session_for(sat_tail) == "2026-10-23|NIGHT"
    assert is_market_open(fri_open) is True
    assert is_market_open(sat_tail) is True
    assert _scheduled_session_key(sat_tail) == "2026-10-23|NIGHT"


def test_monday_holiday_does_not_close_friday_night():
    """A Monday holiday does not close the preceding Friday night or its Sat tail."""
    from src.market_data.holidays import is_taifex_holiday

    assert is_taifex_holiday(date(2026, 10, 26)) is True
    assert session_for(_taipei(2026, 10, 23, 15, 0)) == "2026-10-23|NIGHT"
    assert session_for(_taipei(2026, 10, 24, 1, 0)) == "2026-10-23|NIGHT"
    assert is_market_open(_taipei(2026, 10, 23, 20, 0)) is True
    assert is_market_open(_taipei(2026, 10, 24, 4, 59)) is True
    assert session_for(_taipei(2026, 10, 26, 8, 45)) is None
    assert session_for(_taipei(2026, 10, 27, 1, 0)) is None


def test_tuesday_after_holiday_is_open_and_resubscribes():
    """A normal Tuesday 08:45 is open and session_resubscribe fires."""
    tuesday = _taipei(2026, 10, 27, 8, 45)
    assert is_market_open(tuesday) is True
    assert session_for(tuesday) == "2026-10-27|DAY"
    assert _scheduled_session_key(tuesday) == "2026-10-27|DAY"

    wd = TickWatchdog()
    wd.active = True
    # Last tick in the 05:00-08:45 gap. The day open is a session boundary.
    wd.last_tick_time = _ts(_taipei(2026, 10, 27, 5, 1))
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("src.live.live_runner._taipei_now", lambda: tuesday)
        assert wd.check(now=_ts(tuesday)) == "session_resubscribe"


@pytest.mark.parametrize("parts", _HOLIDAY_CLOSED)
def test_watchdog_holiday_does_not_act_or_count(parts):
    """No session_resubscribe, resubscribe, reconnect, or stand-down count."""
    dt = _taipei(*parts)
    wd = TickWatchdog(zero_tick_reconnect_limit=1)
    wd.active = True
    # Long enough that an open session would have reached reconnect.
    wd.last_tick_time = _ts(dt) - 900
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("src.live.live_runner._taipei_now", lambda: dt)
        assert wd.check(now=_ts(dt)) is None
    assert wd._zero_tick_reconnects == 0
    assert wd._stood_down is False


def test_holiday_open_does_not_session_resubscribe():
    """08:45 and 15:00 on the holiday are not session opens."""
    wd = TickWatchdog()
    wd.active = True
    for closed_at, check_at in (
        ((2026, 10, 26, 5, 1), (2026, 10, 26, 8, 45)),
        ((2026, 10, 26, 14, 0), (2026, 10, 26, 15, 0)),
    ):
        check = _taipei(*check_at)
        wd.last_tick_time = _ts(_taipei(*closed_at))
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("src.live.live_runner._taipei_now", lambda c=check: c)
            action = wd.check(now=_ts(check))
        assert action is None
        assert wd._zero_tick_reconnects == 0


def test_standdown_release_does_not_fire_on_holiday():
    wd = TickWatchdog(zero_tick_reconnect_limit=1)
    wd.active = True
    tick = _taipei(2026, 10, 23, 22, 0)
    wd.last_tick_time = _ts(tick)
    stale = _ts(tick) + 700
    stale_dt = datetime.fromtimestamp(stale, tz=_TZ_TAIPEI)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("src.live.live_runner._taipei_now", lambda: stale_dt)
        assert wd.check(now=stale) == "reconnect"
    assert wd._stood_down is True
    assert wd._stand_down_session == "2026-10-23|NIGHT"
    counted = wd._zero_tick_reconnects

    holiday = _taipei(2026, 10, 26, 8, 45)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("src.live.live_runner._taipei_now", lambda: holiday)
        assert wd._release_standdown_on_new_session() is False
        assert wd.check(now=_ts(holiday)) is None
    assert wd._stood_down is True
    assert wd._stand_down_session == "2026-10-23|NIGHT"
    assert wd._zero_tick_reconnects == counted

    # The holiday night is the same closed open-date, still no release.
    holiday_night = _taipei(2026, 10, 26, 15, 0)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("src.live.live_runner._taipei_now", lambda: holiday_night)
        assert wd._release_standdown_on_new_session() is False
    assert wd._stood_down is True


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


def test_closures_file_date_is_still_closed(closure_file, caplog):
    """2026-04-16 is a Thursday and not a static holiday."""
    closure_file.write_text("2026-04-16\n", encoding="utf-8")
    day = _taipei(2026, 4, 16, 10, 0)
    night = _taipei(2026, 4, 16, 15, 0)
    tail = _taipei(2026, 4, 17, 1, 0)
    with caplog.at_level(logging.WARNING, logger="src.market_data.holidays"):
        assert session_for(day) is None
        assert is_market_open(day) is False
        assert session_for(night) is None
        assert session_for(tail) is None
        assert _scheduled_session_key(day) is None
    assert any(r.getMessage().startswith("holiday") for r in caplog.records)

    wd = TickWatchdog(zero_tick_reconnect_limit=1)
    wd.active = True
    wd.last_tick_time = _ts(day) - 900
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("src.live.live_runner._taipei_now", lambda: day)
        assert wd.check(now=_ts(day)) is None
    assert wd._zero_tick_reconnects == 0


def test_holiday_lookup_failure_fails_open_once(monkeypatch, caplog):
    import src.live.live_runner as lr

    lr._holiday_schedule_warned = False

    def boom(_day):
        raise RuntimeError("calendar down")

    monkeypatch.setattr("src.market_data.holidays.is_taifex_holiday", boom)
    holiday_morning = _taipei(2026, 10, 26, 10, 0)
    with caplog.at_level(logging.WARNING, logger="src.live.live_runner"):
        assert session_for(holiday_morning) == "2026-10-26|DAY"
        assert is_market_open(holiday_morning) is True
        assert _scheduled_session_key(holiday_morning) == "2026-10-26|DAY"
        assert session_for(_taipei(2026, 10, 26, 8, 45)) == "2026-10-26|DAY"
        wd = TickWatchdog()
        wd.active = True
        wd.last_tick_time = _ts(holiday_morning) - 700
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("src.live.live_runner._taipei_now", lambda: holiday_morning)
            assert wd.check(now=_ts(holiday_morning)) == "reconnect"
    warnings = [
        r for r in caplog.records
        if "holiday schedule check failed" in r.getMessage()
    ]
    assert len(warnings) == 1


def _secs(hours: float) -> int:
    return int(round(hours * 3600))


def test_holiday_night_tail_waits_for_the_same_morning():
    """Tue 2026-10-27 02:00 is the closed tail of Monday's holiday night.

    The next open is Tue 08:45 (6.75h), not Wed 08:45 (30.75h).
    """
    now = _taipei(2026, 10, 27, 2, 0)
    assert session_for(now) is None
    assert seconds_until_market_open(now) == _secs(6.75)
    assert now + timedelta(seconds=_secs(6.75)) == _taipei(2026, 10, 27, 8, 45)


def test_lny_reopen_morning_is_the_same_day():
    """Mon 2026-02-23 02:00 is the first session after Lunar New Year.

    Feb 20 is the last holiday. The next open is Mon 08:45 (6.75h).
    """
    now = _taipei(2026, 2, 23, 2, 0)
    assert session_for(now) is None
    assert seconds_until_market_open(now) == _secs(6.75)
    assert now + timedelta(seconds=_secs(6.75)) == _taipei(2026, 2, 23, 8, 45)


def _first_session_open(now: datetime) -> datetime:
    """Earliest minute at or after ``now`` that ``session_for`` calls open."""
    cursor = now.replace(second=0, microsecond=0)
    if cursor < now:
        cursor += timedelta(minutes=1)
    for _ in range(16 * 24 * 60):
        if cursor >= now and session_for(cursor) is not None:
            return cursor
        cursor += timedelta(minutes=1)
    raise AssertionError(f"no session open within 16 days of {now}")


_NEXT_OPEN_SAMPLES = (
    _taipei(2026, 10, 27, 2, 0),    # holiday-night tail
    _taipei(2026, 10, 27, 4, 30),
    _taipei(2026, 2, 23, 2, 0),     # LNY reopen
    _taipei(2026, 10, 26, 2, 0),    # holiday Monday before 05:00
    _taipei(2026, 10, 26, 9, 0),    # holiday DAY
    _taipei(2026, 10, 26, 13, 50),  # holiday afternoon gap
    _taipei(2026, 10, 26, 16, 0),   # holiday NIGHT
    _taipei(2026, 10, 24, 10, 0),   # Saturday; Monday is the holiday
    _taipei(2026, 10, 25, 12, 0),   # Sunday
    _taipei(2026, 10, 23, 6, 0),    # Friday morning gap
    _taipei(2026, 10, 23, 14, 0),   # Friday afternoon gap
    _taipei(2026, 10, 23, 10, 0),   # Friday DAY, already open
    _taipei(2026, 10, 23, 23, 0),   # Friday NIGHT, already open
    _taipei(2026, 10, 21, 2, 0),    # ordinary night tail, already open
    _taipei(2026, 3, 2, 3, 0),      # Monday before 05:00, no Sunday night
    _taipei(2026, 3, 7, 10, 0),     # Saturday after a trading Friday
)


@pytest.mark.parametrize(
    "now",
    _NEXT_OPEN_SAMPLES,
    ids=lambda dt: dt.strftime("%Y-%m-%d %H:%M"),
)
def test_seconds_until_market_open_lands_on_first_session_for(now):
    """The delay ends on the first minute session_for is not None."""
    secs = seconds_until_market_open(now)
    if session_for(now) is not None:
        assert secs == 0
        return
    assert secs > 0
    target = now + timedelta(seconds=secs)
    assert target == _first_session_open(now)
    assert session_for(target) is not None
    assert session_for(target - timedelta(seconds=1)) is None


def test_weekday_night_tail_key_is_previous_open_date():
    """Tue 00:30 keeps Monday's NIGHT key.

    A wall-date key in the clock night branch would say Tuesday|NIGHT.
    2026-03-03 is a Tuesday and 2026-03-02 trades.
    """
    dt = _taipei(2026, 3, 3, 0, 30)
    assert _clock_session_key(dt) == "2026-03-02|NIGHT"
    assert session_for(dt) == "2026-03-02|NIGHT"
    assert _scheduled_session_key(dt) == "2026-03-02|NIGHT"


def test_gap_walk_treats_a_clock_open_holiday_as_closed():
    """09:00-12:00 on the holiday is inside the clock DAY session.

    The walk returns closed because session_for does, not because the
    clock has a gap.
    """
    start = _taipei(2026, 10, 26, 9, 0)
    end = _taipei(2026, 10, 26, 12, 0)
    assert _clock_session_key(start) == "2026-10-26|DAY"
    assert _clock_session_key(end) == "2026-10-26|DAY"
    assert session_for(start) is None
    assert _crossed_session_gap(_ts(start), _ts(end)) is True


def test_slow_retry_guard_skips_holiday_when_minutes_remain():
    """The slow-retry guard itself sees the holiday.

    minutes_until_session_close is patched to a large value so it cannot
    be what suppresses the retry.
    """
    wd = TickWatchdog(zero_tick_reconnect_limit=1, slow_retry_interval=60)
    wd._stood_down = True
    wd._stand_down_session = "2026-10-23|NIGHT"
    wd._last_slow_retry_at = 0.0
    holiday = _taipei(2026, 10, 26, 10, 0)
    assert _clock_session_key(holiday) == "2026-10-26|DAY"
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("src.live.live_runner._taipei_now", lambda: holiday)
        mp.setattr(
            "src.live.tick_watchdog.minutes_until_session_close",
            lambda order_symbol=None: 180,
        )
        assert wd._check_slow_retry(_ts(holiday)) is None
    assert wd._stood_down is True
    assert wd._zero_tick_reconnects == 0


def test_check_guard_skips_holiday_when_minutes_remain():
    """check() returns before the ladder on a holiday.

    minutes_until_session_close is patched so the settlement/near-close
    gate cannot be what returns None. A stale tick inside the holiday
    morning would otherwise reconnect or session_resubscribe.
    """
    wd = TickWatchdog()
    wd.active = True
    holiday = _taipei(2026, 10, 26, 10, 0)
    wd.last_tick_time = _ts(holiday) - 900
    assert _clock_session_key(holiday) is not None
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("src.live.live_runner._taipei_now", lambda: holiday)
        mp.setattr(
            "src.live.tick_watchdog.minutes_until_session_close",
            lambda order_symbol=None: 180,
        )
        assert wd.check(now=_ts(holiday)) is None
    assert wd._zero_tick_reconnects == 0
    assert wd._stood_down is False
