"""Tests for live_trading_period() — the report header's 資料範圍 Range.

Regression for the resumed-session bug: after a restart the process only
holds live bars since the restart, but the restored trade history goes back
to the original session start. The old header used live bars only, so a bot
running since 2026-04-23 reported a range starting at the restart date.
"""

from src.backtest.broker import OrderSide, Trade
from src.backtest.report import live_trading_period


def _trade(entry_dt: str, exit_dt: str) -> Trade:
    return Trade(
        tag="t", side=OrderSide.LONG, qty=1,
        entry_price=20000, exit_price=20100,
        entry_bar_index=0, exit_bar_index=1,
        entry_dt=entry_dt, exit_dt=exit_dt,
    )


class TestLiveTradingPeriod:
    def test_resumed_session_spans_restored_history(self):
        # Session started 4/23; process restarted 7/8 so live bars only
        # cover 7/8 15:00 onward. Range must still start at 4/23.
        trades = [
            _trade("2026-04-23 09:15", "2026-04-23 11:30"),
            _trade("2026-06-15 10:00", "2026-06-15 12:00"),
        ]
        period = live_trading_period(
            "2026-04-23T09:00:00", trades, "2026-07-09 13:15")
        assert period == ("2026-04-23 09:00", "2026-07-09 13:15")

    def test_started_at_iso_t_separator_normalized(self):
        period = live_trading_period("2026-04-23T09:00:00", [], "2026-04-23 10:00")
        assert period == ("2026-04-23 09:00", "2026-04-23 10:00")

    def test_legacy_session_without_started_at_uses_first_trade(self):
        trades = [_trade("2026-04-23 09:15", "2026-04-23 11:30")]
        period = live_trading_period(None, trades, "2026-07-09 13:15")
        assert period == ("2026-04-23 09:15", "2026-07-09 13:15")

    def test_first_trade_earlier_than_started_at_wins(self):
        # started_at can be later than the first trade if an old session
        # file predates the started_at field and was resumed since.
        trades = [_trade("2026-04-23 09:15", "2026-04-23 11:30")]
        period = live_trading_period(
            "2026-07-08T15:00:00", trades, "2026-07-09 13:15")
        assert period[0] == "2026-04-23 09:15"

    def test_last_trade_exit_later_than_last_bar_wins(self):
        # Bot offline since the last trade: no live bars past the exit.
        trades = [_trade("2026-04-23 09:15", "2026-07-09 13:10")]
        period = live_trading_period(
            "2026-04-23T09:00:00", trades, "2026-07-09 11:00")
        assert period[1] == "2026-07-09 13:10"

    def test_no_trades_no_bars_returns_none(self):
        # Fresh session still warming up — no end candidate, no Range line.
        assert live_trading_period("2026-07-08T15:00:00", [], None) is None

    def test_no_start_candidates_returns_none(self):
        assert live_trading_period(None, [], "2026-07-09 13:15") is None

    def test_empty_trade_dts_ignored(self):
        trades = [_trade("", "")]
        period = live_trading_period(
            "2026-04-23T09:00:00", trades, "2026-07-09 13:15")
        assert period == ("2026-04-23 09:00", "2026-07-09 13:15")

    def test_trades_none_is_safe(self):
        period = live_trading_period(
            "2026-04-23T09:00:00", None, "2026-07-09 13:15")
        assert period == ("2026-04-23 09:00", "2026-07-09 13:15")
