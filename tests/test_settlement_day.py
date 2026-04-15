"""Tests for TAIFEX settlement-day detection and front-month checks."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from src.market_data.holidays import (
    is_taifex_holiday,
    next_trading_day,
    third_wednesday,
    settlement_day,
    is_settlement_day,
    is_front_month_contract,
)


_TPE = timezone(timedelta(hours=8))


class TestThirdWednesday:
    def test_april_2026(self):
        assert third_wednesday(2026, 4) == date(2026, 4, 15)

    def test_february_2026(self):
        # Feb 2026: Wed are 4, 11, 18, 25 → 3rd is 18
        assert third_wednesday(2026, 2) == date(2026, 2, 18)

    def test_january_2026(self):
        # Jan 2026 Wed: 7, 14, 21, 28 → 3rd is 21
        assert third_wednesday(2026, 1) == date(2026, 1, 21)


class TestIsTaifexHoliday:
    def test_weekend(self):
        assert is_taifex_holiday(date(2026, 4, 11)) is True   # Saturday
        assert is_taifex_holiday(date(2026, 4, 12)) is True   # Sunday

    def test_normal_weekday(self):
        assert is_taifex_holiday(date(2026, 4, 15)) is False  # Wed (settlement day, but trading)
        assert is_taifex_holiday(date(2026, 4, 16)) is False  # Thu

    def test_known_holiday_new_year(self):
        # 2026-01-01 is New Year (and a Thursday)
        assert is_taifex_holiday(date(2026, 1, 1)) is True


class TestNextTradingDay:
    def test_already_trading(self):
        d = date(2026, 4, 15)  # Wed, trading day
        assert next_trading_day(d) == d

    def test_skips_weekend(self):
        sat = date(2026, 4, 11)
        assert next_trading_day(sat) == date(2026, 4, 13)  # Mon

    def test_skips_holiday_into_weekend(self):
        # 2026-01-01 (Thu) is a holiday; Fri 2026-01-02 should be trading
        assert next_trading_day(date(2026, 1, 1)) == date(2026, 1, 2)


class TestSettlementDay:
    def test_april_2026_normal(self):
        # April 2026: 3rd Wed = 15th, not a holiday
        assert settlement_day(2026, 4) == date(2026, 4, 15)

    def test_holiday_shift(self):
        # If 3rd Wed were a holiday, settlement should move to next trading day.
        # 2026-04-15 is NOT a holiday by default; we simulate via override.
        from src.market_data import holidays as h
        d = date(2026, 4, 15)
        original = h.OVERRIDE_HOLIDAYS
        try:
            h.OVERRIDE_HOLIDAYS = frozenset({d})
            assert settlement_day(2026, 4) == date(2026, 4, 16)  # Thu
        finally:
            h.OVERRIDE_HOLIDAYS = original

    def test_is_settlement_day_today(self):
        # Today (test runs on 2026-04-15) = settlement day for April
        assert is_settlement_day(date(2026, 4, 15)) is True

    def test_is_settlement_day_other(self):
        assert is_settlement_day(date(2026, 4, 14)) is False
        assert is_settlement_day(date(2026, 4, 16)) is False
        assert is_settlement_day(date(2026, 4, 22)) is False  # 4th Wed

    def test_is_settlement_day_accepts_datetime(self):
        dt = datetime(2026, 4, 15, 13, 25, tzinfo=_TPE)
        assert is_settlement_day(dt) is True


class TestIsFrontMonthContract:
    def test_april_2026_front_month(self):
        # April → letter D, year 2026 → digit 6 → "...D6"
        d = date(2026, 4, 15)
        assert is_front_month_contract("TXFD6", d) is True
        assert is_front_month_contract("MTXD6", d) is True
        assert is_front_month_contract("TMFD6", d) is True

    def test_april_2026_back_months(self):
        d = date(2026, 4, 15)
        assert is_front_month_contract("TXFE6", d) is False  # May
        assert is_front_month_contract("TXFF6", d) is False  # Jun
        assert is_front_month_contract("TXFI6", d) is False  # Sep

    def test_year_digit_mismatch(self):
        d = date(2026, 4, 15)
        assert is_front_month_contract("TXFD7", d) is False  # 2027

    def test_empty_or_short_symbol(self):
        d = date(2026, 4, 15)
        assert is_front_month_contract("", d) is False
        assert is_front_month_contract("X", d) is False

    def test_accepts_datetime(self):
        dt = datetime(2026, 4, 15, 9, 0, tzinfo=_TPE)
        assert is_front_month_contract("TXFD6", dt) is True


class TestMinutesUntilSessionCloseSettlement:
    """Verify the session-close calculation respects settlement day."""

    def test_settlement_day_front_month_returns_1330(self, monkeypatch):
        from src.live import live_runner
        # Pin "now" to 2026-04-15 13:00 TPE (settlement day, AM open)
        fake_now = datetime(2026, 4, 15, 13, 0, tzinfo=_TPE)
        monkeypatch.setattr(live_runner, "_taipei_now", lambda: fake_now)

        # Front-month TXFD6 → close at 13:30 → 30 min away
        mins = live_runner.minutes_until_session_close("TXFD6")
        assert mins == 30

    def test_settlement_day_back_month_returns_1345(self, monkeypatch):
        from src.live import live_runner
        fake_now = datetime(2026, 4, 15, 13, 0, tzinfo=_TPE)
        monkeypatch.setattr(live_runner, "_taipei_now", lambda: fake_now)

        # Back-month TXFE6 (May) → close at 13:45 → 45 min away
        mins = live_runner.minutes_until_session_close("TXFE6")
        assert mins == 45

    def test_non_settlement_day_returns_1345(self, monkeypatch):
        from src.live import live_runner
        # 2026-04-14 (Tue) is not settlement day
        fake_now = datetime(2026, 4, 14, 13, 0, tzinfo=_TPE)
        monkeypatch.setattr(live_runner, "_taipei_now", lambda: fake_now)

        mins = live_runner.minutes_until_session_close("TXFD6")
        assert mins == 45

    def test_no_symbol_uses_normal_close(self, monkeypatch):
        from src.live import live_runner
        # Even on settlement day, no symbol → defaults to 13:45
        fake_now = datetime(2026, 4, 15, 13, 0, tzinfo=_TPE)
        monkeypatch.setattr(live_runner, "_taipei_now", lambda: fake_now)

        mins = live_runner.minutes_until_session_close()
        assert mins == 45
