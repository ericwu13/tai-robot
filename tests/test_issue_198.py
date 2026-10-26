"""Issue #198: a weekday TAIFEX holiday is not a live session.

The schedule is keyed by the session OPEN date. 2026-10-26 is the
observed day off for Taiwan Restoration Day. Monday's DAY and NIGHT
sessions are closed, and so is the Tuesday 00:00-05:00 tail of that
night. Friday 2026-10-23 night, including its Saturday tail, stays open.
"""

from __future__ import annotations

import logging
from datetime import date, datetime

import pytest

from src.live.live_runner import _TZ_TAIPEI, is_market_open, session_for
from src.live.tick_watchdog import TickWatchdog, _scheduled_session_key


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
