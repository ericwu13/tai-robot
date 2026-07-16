"""Unit tests for src.regime.switch_logic — pure functions."""

from datetime import datetime, timezone, timedelta

from src.regime.switch_logic import (
    session_slot,
    in_closed_gap,
    current_session,
    latest_night_session,
    last_completed_night,
    classification_due,
    validate_leg_strategies,
    _TZ_TAIPEI,
)

# Test-week calendar (2026): 07-09 Thu, 07-10 Fri, 07-11 Sat, 07-12 Sun,
# 07-13 Mon. No TW public holidays in this range.


def _tpe(year, month, day, hour, minute):
    """Build a timezone-aware Taipei datetime."""
    return datetime(year, month, day, hour, minute, tzinfo=_TZ_TAIPEI)


# ── session_slot ──

class TestSessionSlot:
    def test_day_open_boundary(self):
        dt = _tpe(2026, 7, 9, 8, 45)  # Wednesday 08:45
        assert session_slot(dt) == ("2026-07-09", "DAY")

    def test_just_before_day_open(self):
        dt = _tpe(2026, 7, 9, 8, 44)
        assert session_slot(dt) == ("2026-07-09", "NIGHT")

    def test_day_close_boundary(self):
        # 13:45 still classifies as DAY (grace minute)
        dt = _tpe(2026, 7, 9, 13, 45)
        assert session_slot(dt) == ("2026-07-09", "DAY")

    def test_one_minute_past_day_close(self):
        # 13:46 is NIGHT
        dt = _tpe(2026, 7, 9, 13, 46)
        assert session_slot(dt) == ("2026-07-09", "NIGHT")

    def test_night_session_evening(self):
        dt = _tpe(2026, 7, 9, 20, 0)
        assert session_slot(dt) == ("2026-07-09", "NIGHT")

    def test_night_session_after_midnight(self):
        dt = _tpe(2026, 7, 10, 3, 0)  # Thursday 03:00
        assert session_slot(dt) == ("2026-07-10", "NIGHT")

    def test_night_close_boundary(self):
        dt = _tpe(2026, 7, 10, 5, 0)  # 05:00 is NIGHT
        assert session_slot(dt) == ("2026-07-10", "NIGHT")

    def test_naive_datetime_treated_as_taipei(self):
        dt = datetime(2026, 7, 9, 10, 0)
        date_str, slot = session_slot(dt)
        assert slot == "DAY"


# ── in_closed_gap ──

class TestInClosedGap:
    def test_day_night_gap(self):
        dt = _tpe(2026, 7, 9, 14, 0)  # Wed 14:00
        assert in_closed_gap(dt) is True

    def test_just_after_day_close(self):
        dt = _tpe(2026, 7, 9, 13, 46)
        assert in_closed_gap(dt) is True

    def test_night_open_not_gap(self):
        dt = _tpe(2026, 7, 9, 15, 0)
        assert in_closed_gap(dt) is False

    def test_night_day_gap(self):
        dt = _tpe(2026, 7, 10, 6, 0)  # Thu 06:00
        assert in_closed_gap(dt) is True

    def test_just_after_night_close(self):
        dt = _tpe(2026, 7, 10, 5, 0)
        assert in_closed_gap(dt) is True

    def test_day_open_not_gap(self):
        dt = _tpe(2026, 7, 10, 8, 45)
        assert in_closed_gap(dt) is False

    def test_mid_day_session(self):
        dt = _tpe(2026, 7, 9, 10, 30)
        assert in_closed_gap(dt) is False

    def test_mid_night_session(self):
        dt = _tpe(2026, 7, 9, 22, 0)
        assert in_closed_gap(dt) is False

    def test_sunday_fully_closed(self):
        dt = _tpe(2026, 7, 12, 12, 0)  # Sunday
        assert in_closed_gap(dt) is True

    def test_saturday_after_night_close(self):
        dt = _tpe(2026, 7, 11, 8, 0)  # Saturday 08:00
        assert in_closed_gap(dt) is True

    def test_saturday_early_morning_open(self):
        dt = _tpe(2026, 7, 11, 3, 0)  # Saturday 03:00 (night carryover)
        assert in_closed_gap(dt) is False

    def test_monday_pre_open(self):
        dt = _tpe(2026, 7, 13, 7, 0)  # Monday 07:00
        assert in_closed_gap(dt) is True


# ── current_session (open-date session identity) ──

class TestCurrentSession:
    def test_day_session(self):
        sess = current_session(_tpe(2026, 7, 9, 10, 0))  # Thu 10:00
        assert (sess.open_date, sess.slot) == ("2026-07-09", "DAY")
        assert sess.open_dt == _tpe(2026, 7, 9, 8, 45)
        assert sess.close_dt == _tpe(2026, 7, 9, 13, 45)

    def test_night_before_midnight(self):
        sess = current_session(_tpe(2026, 7, 9, 22, 0))  # Thu 22:00
        assert (sess.open_date, sess.slot) == ("2026-07-09", "NIGHT")
        assert sess.close_dt == _tpe(2026, 7, 10, 5, 0)

    def test_night_after_midnight_same_identity(self):
        # THE root-cause fix: the same night session keeps its OPEN-date
        # identity on both sides of midnight (old session_slot flipped
        # the date at 00:00, splitting one session into two keys).
        before = current_session(_tpe(2026, 7, 9, 23, 59))
        after = current_session(_tpe(2026, 7, 10, 3, 0))
        assert before.key == after.key == "2026-07-09|NIGHT"

    def test_day_gap_returns_none(self):
        assert current_session(_tpe(2026, 7, 9, 14, 0)) is None

    def test_morning_gap_returns_none(self):
        assert current_session(_tpe(2026, 7, 10, 6, 0)) is None

    def test_at_day_close_returns_none(self):
        assert current_session(_tpe(2026, 7, 9, 13, 45)) is None

    def test_just_before_day_close(self):
        sess = current_session(_tpe(2026, 7, 9, 13, 44))
        assert (sess.open_date, sess.slot) == ("2026-07-09", "DAY")

    def test_saturday_daytime_returns_none(self):
        # Old code produced a phantom "2026-07-11,DAY" history row from a
        # Saturday 13:43 poll — Saturday has no DAY session.
        assert current_session(_tpe(2026, 7, 11, 13, 43)) is None

    def test_saturday_night_carryover(self):
        # Friday's night session runs until Saturday 05:00.
        sess = current_session(_tpe(2026, 7, 11, 3, 0))
        assert (sess.open_date, sess.slot) == ("2026-07-10", "NIGHT")

    def test_sunday_returns_none(self):
        assert current_session(_tpe(2026, 7, 12, 20, 0)) is None

    def test_monday_early_morning_returns_none(self):
        # No Sunday-open night session to carry over.
        assert current_session(_tpe(2026, 7, 13, 3, 0)) is None

    def test_holiday_returns_none(self, monkeypatch):
        from src.market_data import holidays as hol
        monkeypatch.setattr(hol, "is_taifex_holiday", lambda d: True)
        assert current_session(_tpe(2026, 7, 9, 10, 0)) is None


# ── latest_night_session / last_completed_night ──

class TestLatestNightSession:
    def test_in_progress_night(self):
        sess = latest_night_session(_tpe(2026, 7, 10, 4, 58))  # Fri 04:58
        assert sess.open_date == "2026-07-09"
        assert sess.close_dt == _tpe(2026, 7, 10, 5, 0)

    def test_completed_night_during_day(self):
        sess = latest_night_session(_tpe(2026, 7, 10, 12, 0))  # Fri noon
        assert sess.open_date == "2026-07-09"

    def test_weekend_holds_friday_night(self):
        # Sat, Sun, and Mon pre-open all resolve to the Friday-open night.
        for dt in (_tpe(2026, 7, 11, 12, 0), _tpe(2026, 7, 12, 12, 0),
                   _tpe(2026, 7, 13, 4, 58)):
            assert latest_night_session(dt).open_date == "2026-07-10"

    def test_evening_advances_to_new_night(self):
        sess = latest_night_session(_tpe(2026, 7, 13, 15, 30))  # Mon 15:30
        assert sess.open_date == "2026-07-13"


class TestLastCompletedNight:
    def test_during_night_returns_previous(self):
        sess = last_completed_night(_tpe(2026, 7, 9, 22, 0))  # in Thu-open night
        assert sess.open_date == "2026-07-08"

    def test_after_close_returns_that_night(self):
        sess = last_completed_night(_tpe(2026, 7, 10, 6, 0))
        assert sess.open_date == "2026-07-09"


# ── classification_due ──

class TestClassificationDue:
    def test_fires_in_close_window(self):
        sess = classification_due(_tpe(2026, 7, 10, 4, 58), "")
        assert sess is not None
        assert sess.open_date == "2026-07-09"  # keyed by session OPEN date

    def test_dedup_blocks(self):
        assert classification_due(
            _tpe(2026, 7, 10, 4, 58), "2026-07-09|NIGHT") is None

    def test_mid_night_not_due(self):
        # In-progress night, far from close: wait for the close window.
        assert classification_due(
            _tpe(2026, 7, 9, 22, 0), "2026-07-08|NIGHT") is None

    def test_catch_up_after_missed_window(self):
        # App slept across 04:58-05:00: the completed, unassessed night
        # still classifies later that morning (old code silently skipped
        # the whole day).
        sess = classification_due(_tpe(2026, 7, 10, 9, 30), "2026-07-08|NIGHT")
        assert sess is not None
        assert sess.open_date == "2026-07-09"

    def test_weekend_polls_inert(self):
        # Once Friday's night is assessed, Sat/Sun/Mon-morning polls stay
        # None — old code re-classified the same stale data on Sunday and
        # Monday 04:58, double-stepping the hysteresis state machine.
        for dt in (_tpe(2026, 7, 11, 12, 0), _tpe(2026, 7, 12, 4, 58),
                   _tpe(2026, 7, 13, 4, 58)):
            assert classification_due(dt, "2026-07-10|NIGHT") is None

    def test_saturday_morning_close_window_fires(self):
        # Friday's night legitimately closes Saturday 05:00.
        sess = classification_due(_tpe(2026, 7, 11, 4, 58), "2026-07-09|NIGHT")
        assert sess is not None
        assert sess.open_date == "2026-07-10"


# ── validate_leg_strategies ──

class TestValidateLegStrategies:
    def test_both_valid_same_tf(self):
        class S1:
            kline_type = 0
            kline_minute = 60
        class S2:
            kline_type = 0
            kline_minute = 60
        registry = {"Long A": S1, "Short B": S2}
        assert validate_leg_strategies("Long A", "Short B", registry) == []

    def test_missing_long(self):
        class S:
            kline_type = 0
            kline_minute = 60
        registry = {"Short B": S}
        errors = validate_leg_strategies("Missing", "Short B", registry)
        assert len(errors) == 1
        assert "Missing" in errors[0]

    def test_missing_both(self):
        errors = validate_leg_strategies("A", "B", {})
        assert len(errors) == 2

    def test_timeframe_mismatch_is_allowed(self, caplog):
        # Mixed timeframes are supported via a swap-time aggregator rebuild,
        # so validation no longer errors — it only logs a soft warning.
        import logging
        class S1:
            kline_type = 0
            kline_minute = 60
        class S2:
            kline_type = 0
            kline_minute = 240
        registry = {"Long": S1, "Short": S2}
        with caplog.at_level(logging.WARNING):
            errors = validate_leg_strategies("Long", "Short", registry)
        assert errors == []
        assert any("Mixed timeframes" in r.message for r in caplog.records)
