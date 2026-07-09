"""Unit tests for src.regime.switch_logic — pure functions."""

from datetime import datetime, timezone, timedelta

from src.regime.switch_logic import (
    session_slot,
    in_closed_gap,
    should_classify,
    validate_leg_strategies,
    _TZ_TAIPEI,
)


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


# ── should_classify ──

class TestShouldClassify:
    def test_fires_on_night_near_close(self):
        now = _tpe(2026, 7, 10, 4, 58)
        assert should_classify(now, ("2026-07-10", "NIGHT"), "", 1.5) is True

    def test_refuses_day_session(self):
        now = _tpe(2026, 7, 9, 13, 44)
        assert should_classify(now, ("2026-07-09", "DAY"), "", 1.0) is False

    def test_refuses_far_from_close(self):
        now = _tpe(2026, 7, 9, 22, 0)
        assert should_classify(now, ("2026-07-09", "NIGHT"), "", 60.0) is False

    def test_dedup_same_key(self):
        now = _tpe(2026, 7, 10, 4, 58)
        assert should_classify(now, ("2026-07-10", "NIGHT"), "2026-07-10|NIGHT", 1.5) is False

    def test_different_date_key_passes(self):
        now = _tpe(2026, 7, 10, 4, 58)
        assert should_classify(now, ("2026-07-10", "NIGHT"), "2026-07-09|NIGHT", 1.5) is True


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

    def test_timeframe_mismatch(self):
        class S1:
            kline_type = 0
            kline_minute = 60
        class S2:
            kline_type = 0
            kline_minute = 240
        registry = {"Long": S1, "Short": S2}
        errors = validate_leg_strategies("Long", "Short", registry)
        assert len(errors) == 1
        assert "mismatch" in errors[0].lower()
