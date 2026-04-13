"""Tests for Issue #20: APP TV backtest must match TradingView official results.

Root causes for divergence:
1. No session detection utility — each AI-generated strategy reinvented it wrong.
   The buggy strategy assumed bar.dt was UTC, but it's Taiwan time (TWT/UTC+8):
   - Missed day session close at 12:45 TWT
   - False session close at 20:00 TWT mid night-session
2. BrokerContext didn't expose trades — strategies used bar.close vs entry_price
   as a loss proxy, which miscounts TP exits where bar closes below entry.

Infrastructure fixes:
- src/market_data/sessions.py: is_last_bar_of_session() utility
- src/backtest/broker.py: BrokerContext.trades property
- src/ai/prompts.py + code_sandbox.py: updated code gen context and allowed imports
"""

from datetime import datetime

import pytest

from src.market_data.models import Bar
from src.market_data.sessions import (
    is_last_bar_of_session,
    is_last_n_bars_of_session,
    minutes_until_close,
)
from src.backtest.engine import BacktestEngine
from src.backtest.strategy import BacktestStrategy
from src.backtest.broker import BrokerContext, OrderSide, SimulatedBroker
from src.market_data.data_store import DataStore
from src.strategy.indicators import bollinger_bands, atr


# ── Helpers ──────────────────────────────────────────────────────────────────

def _bar(dt, close, *, open_=None, high_off=30, low_off=30, volume=1000):
    """Create a Bar with sensible defaults.

    Default: open=close, high=close+30, low=close-30.
    This gives lower_wick=30 which triggers entry cond_B.
    """
    return Bar(
        symbol="TX00", dt=dt,
        open=open_ if open_ is not None else close,
        high=close + high_off,
        low=close - low_off,
        close=close, volume=volume, interval=3600,
    )


def _make_warmup_bars(close=33000):
    """Create 21 bars that do NOT trigger entry conditions.

    Uses open=close-5, low=open (no lower wick) to prevent all conditions:
    - cond_A: open(32995) < MB(33000)
    - cond_B: lower_wick = min(32995,33000)-32995 = 0
    - cond_C: body_size(5)/safe_range(35) = 0.14 < 0.66
    - cond_D: lower_wick(0) < body_size*2(10)

    Times avoid session-close hours (12:45 and 04:00 TWT).
    """
    times = (
        # Night 1: Feb 2 15:00-23:00 (9 bars)
        [datetime(2026, 2, 2, h, 0) for h in range(15, 24)]
        # Night 1 continued: Feb 3 00:00-03:00 (4 bars)
        + [datetime(2026, 2, 3, h, 0) for h in range(0, 4)]
        # Day 1: Feb 3 08:45-11:45 (4 bars, skip 12:45)
        + [datetime(2026, 2, 3, h, 45) for h in range(8, 12)]
        # Night 2: Feb 3 15:00-18:00 (4 bars)
        + [datetime(2026, 2, 3, h, 0) for h in range(15, 19)]
    )
    assert len(times) == 21
    return [
        Bar(symbol="TX00", dt=dt,
            open=close - 5, high=close + 30, low=close - 5, close=close,
            volume=1000, interval=3600)
        for dt in times
    ]


# ── Fixed Strategy (uses infrastructure utility) ─────────────────────────────

class GeneralAtrBreakoutFixed(BacktestStrategy):
    """GeneralAtrBreakout using infrastructure session utility and broker.trades.

    This is how ALL future AI-generated strategies should work:
    1. Use is_last_bar_of_session() instead of hand-coded hour checks
    2. Use broker.trades[-1].pnl for loss counting
    """
    kline_type = 0
    kline_minute = 60

    def __init__(self, **kwargs):
        self.bb_length = kwargs.get("bb_length", 20)
        self.bb_mult = kwargs.get("bb_mult", 2.0)
        self.atr_period = kwargs.get("atr_period", 14)
        self.max_daily_losses = kwargs.get("max_daily_losses", 2)
        self.min_stop_loss = kwargs.get("min_stop_loss", 60)
        self.daily_loss_count = 0
        self.last_bar_date = None
        self.prev_position_size = 0
        self.entry_price = 0.0
        self.entry_sl_points = 0.0
        self.entry_bar_index = -1

    def required_bars(self) -> int:
        return max(self.bb_length, self.atr_period) + 1

    def _update_daily_state(self, bar, broker):
        current_date = bar.dt.date()
        if self.last_bar_date and current_date != self.last_bar_date:
            self.daily_loss_count = 0
        self.last_bar_date = current_date
        if self.prev_position_size > 0 and broker.position_size == 0:
            trades = broker.trades
            if trades and trades[-1].pnl < 0:
                self.daily_loss_count += 1
        self.prev_position_size = broker.position_size

    def _check_entry_conditions(self, bar, middle_band):
        if bar.low > middle_band or bar.close < middle_band:
            return False
        total_range = bar.high - bar.low
        safe_range = total_range if total_range > 0 else 0.0001
        body_size = abs(bar.close - bar.open)
        lower_wick = min(bar.open, bar.close) - bar.low
        cond_A = bar.open > middle_band
        cond_B = lower_wick > 0
        cond_C = (body_size / safe_range) >= 0.66
        cond_D = (body_size / safe_range) <= 0.33 and lower_wick >= (body_size * 2)
        return cond_A or cond_B or cond_C or cond_D

    def on_bar(self, bar, data_store, broker):
        closes = data_store.get_closes()
        highs = data_store.get_highs()
        lows = data_store.get_lows()
        bb_result = bollinger_bands(closes, self.bb_length, self.bb_mult)
        atr_val = atr(highs, lows, closes, self.atr_period)
        if not bb_result or not atr_val:
            return
        upper_band, middle_band, _ = bb_result
        self._update_daily_state(bar, broker)
        # FIX: use infrastructure utility instead of hand-coded hour checks
        is_last_bar = is_last_bar_of_session(bar.dt, self.kline_minute)
        if broker.position_size > 0:
            if is_last_bar:
                broker.close("Long", tag="Session Close")
                return
            bars_since_entry = (len(data_store) - 1) - self.entry_bar_index
            tp_price = upper_band if bars_since_entry > 0 else self.entry_price + 10000
            sl_price = self.entry_price - self.entry_sl_points
            broker.exit("ExitLong", "Long", limit=tp_price, stop=sl_price)
        else:
            if is_last_bar or self.daily_loss_count >= self.max_daily_losses:
                return
            if self._check_entry_conditions(bar, middle_band):
                self.entry_price = bar.close
                self.entry_sl_points = max(atr_val / 2, self.min_stop_loss)
                self.entry_bar_index = len(data_store) - 1
                broker.entry("Long", OrderSide.LONG, qty=1)


# ── Buggy Strategy (original from Issue #20) ─────────────────────────────────

class GeneralAtrBreakoutBuggy(BacktestStrategy):
    """Original strategy with hand-coded session close and bar.close loss counter.

    Demonstrates what goes wrong when strategies don't use the infrastructure.
    """
    kline_type = 0
    kline_minute = 60

    def __init__(self, **kwargs):
        self.bb_length = kwargs.get("bb_length", 20)
        self.bb_mult = kwargs.get("bb_mult", 2.0)
        self.atr_period = kwargs.get("atr_period", 14)
        self.max_daily_losses = kwargs.get("max_daily_losses", 2)
        self.min_stop_loss = kwargs.get("min_stop_loss", 60)
        self.daily_loss_count = 0
        self.last_bar_date = None
        self.prev_position_size = 0
        self.entry_price = 0.0
        self.entry_sl_points = 0.0
        self.entry_bar_index = -1

    def required_bars(self) -> int:
        return max(self.bb_length, self.atr_period) + 1

    def _is_last_bar_of_session(self, dt_utc) -> bool:
        # BUG: assumes UTC but bar.dt is Taiwan time
        is_day_close_bar = dt_utc.hour == 4 and dt_utc.minute == 0
        is_night_close_bar = dt_utc.hour == 20 and dt_utc.minute == 0
        return is_day_close_bar or is_night_close_bar

    def _update_daily_state(self, bar, broker):
        current_date = bar.dt.date()
        if self.last_bar_date and current_date != self.last_bar_date:
            self.daily_loss_count = 0
        self.last_bar_date = current_date
        if self.prev_position_size > 0 and broker.position_size == 0:
            # BUG: uses bar.close instead of actual trade P&L
            if bar.close < self.entry_price:
                self.daily_loss_count += 1
        self.prev_position_size = broker.position_size

    def _check_entry_conditions(self, bar, middle_band):
        if bar.low > middle_band or bar.close < middle_band:
            return False
        total_range = bar.high - bar.low
        safe_range = total_range if total_range > 0 else 0.0001
        body_size = abs(bar.close - bar.open)
        lower_wick = min(bar.open, bar.close) - bar.low
        cond_A = bar.open > middle_band
        cond_B = lower_wick > 0
        cond_C = (body_size / safe_range) >= 0.66
        cond_D = (body_size / safe_range) <= 0.33 and lower_wick >= (body_size * 2)
        return cond_A or cond_B or cond_C or cond_D

    def on_bar(self, bar, data_store, broker):
        closes = data_store.get_closes()
        highs = data_store.get_highs()
        lows = data_store.get_lows()
        bb_result = bollinger_bands(closes, self.bb_length, self.bb_mult)
        atr_val = atr(highs, lows, closes, self.atr_period)
        if not bb_result or not atr_val:
            return
        upper_band, middle_band, _ = bb_result
        self._update_daily_state(bar, broker)
        is_last_bar = self._is_last_bar_of_session(bar.dt)
        if broker.position_size > 0:
            if is_last_bar:
                broker.close("Long", tag="Session Close")
                return
            bars_since_entry = (len(data_store) - 1) - self.entry_bar_index
            tp_price = upper_band if bars_since_entry > 0 else self.entry_price + 10000
            sl_price = self.entry_price - self.entry_sl_points
            broker.exit("ExitLong", "Long", limit=tp_price, stop=sl_price)
        else:
            if is_last_bar or self.daily_loss_count >= self.max_daily_losses:
                return
            if self._check_entry_conditions(bar, middle_band):
                self.entry_price = bar.close
                self.entry_sl_points = max(atr_val / 2, self.min_stop_loss)
                self.entry_bar_index = len(data_store) - 1
                broker.entry("Long", OrderSide.LONG, qty=1)


# ── Unit Tests: Session Utility (infrastructure) ────────────────────────────

class TestIsLastBarOfSession:
    """Tests for the is_last_bar_of_session() infrastructure utility.

    This utility replaces per-strategy session detection.
    Must match Pine Script: (tw_h==12 and tw_m==45) or (tw_h==4 and tw_m==0)
    for 60-min bars, and generalize correctly for other intervals.
    """

    # --- 60-minute bars (matches Pine Script exactly) ---

    @pytest.mark.parametrize("tw_h,tw_m,expected", [
        (8, 45, False),    # Day session start
        (9, 45, False),    # Mid day
        (11, 45, False),   # Last non-close day bar
        (12, 45, True),    # Day session end
        (15, 0, False),    # Night session start
        (20, 0, False),    # Mid night (was BUGGY: falsely True)
        (23, 0, False),    # Late night
        (0, 0, False),     # Midnight
        (3, 0, False),     # Last non-close night bar
        (4, 0, True),      # Night session end
    ])
    def test_60min_bars(self, tw_h, tw_m, expected):
        dt = datetime(2026, 2, 4, tw_h, tw_m)
        assert is_last_bar_of_session(dt, 60) is expected

    # --- 15-minute bars ---

    @pytest.mark.parametrize("tw_h,tw_m,expected", [
        (13, 15, False),   # Not quite last
        (13, 30, True),    # Day close: 13:30+15 = 13:45
        (13, 0, False),    # 13:00+15 = 13:15 < 13:45
        (4, 30, False),    # 4:30+15 = 4:45 < 5:00
        (4, 45, True),     # Night close: 4:45+15 = 5:00
        (4, 0, False),     # 4:00+15 = 4:15 < 5:00
    ])
    def test_15min_bars(self, tw_h, tw_m, expected):
        dt = datetime(2026, 2, 4, tw_h, tw_m)
        assert is_last_bar_of_session(dt, 15) is expected

    # --- 5-minute bars ---

    @pytest.mark.parametrize("tw_h,tw_m,expected", [
        (13, 35, False),
        (13, 40, True),    # 13:40+5 = 13:45
        (4, 50, False),
        (4, 55, True),     # 4:55+5 = 5:00
    ])
    def test_5min_bars(self, tw_h, tw_m, expected):
        dt = datetime(2026, 2, 4, tw_h, tw_m)
        assert is_last_bar_of_session(dt, 5) is expected

    # --- 1-minute bars ---

    def test_1min_day_close(self):
        assert is_last_bar_of_session(datetime(2026, 2, 4, 13, 44), 1) is True
        assert is_last_bar_of_session(datetime(2026, 2, 4, 13, 43), 1) is False

    def test_1min_night_close(self):
        assert is_last_bar_of_session(datetime(2026, 2, 5, 4, 59), 1) is True
        assert is_last_bar_of_session(datetime(2026, 2, 5, 4, 58), 1) is False

    # --- Edge cases ---

    def test_mid_night_session_never_last(self):
        """Bars at 15:00-23:59 are never the last bar (session continues past midnight)."""
        for h in range(15, 24):
            assert is_last_bar_of_session(datetime(2026, 2, 4, h, 0), 60) is False

    def test_mid_gap_hours_not_last(self):
        """Bars between sessions (05:00-08:44, 13:45-14:59) are not session bars.

        The utility doesn't need to handle these since no bars exist at these times,
        but it should return False to be safe.
        """
        assert is_last_bar_of_session(datetime(2026, 2, 4, 6, 0), 60) is False
        assert is_last_bar_of_session(datetime(2026, 2, 4, 14, 0), 60) is False


# ── Unit Tests: Daily Loss Counter ───────────────────────────────────────────

class TestDailyLossCounter:
    """Tests for broker.trades-based loss counting (the secondary bug)."""

    def test_fixed_profitable_trade_not_counted_as_loss(self):
        """TP exit (profit) must NOT increment loss count,
        even when bar.close < entry_price."""
        broker = SimulatedBroker(point_value=200)
        broker.position_size = 1
        broker.position_side = OrderSide.LONG
        broker.entry_price = 33000
        broker.entry_tag = "Long"
        broker._current_bar_dt = "2026-03-05 22:00"
        broker._close_position("ExitLong", 33060, 5)  # TP profit
        assert broker.trades[-1].pnl > 0

        strategy = GeneralAtrBreakoutFixed()
        strategy.entry_price = 33000
        strategy.prev_position_size = 1
        strategy.last_bar_date = datetime(2026, 3, 5).date()

        bar = _bar(datetime(2026, 3, 5, 22, 0), 32990)  # close < entry
        strategy._update_daily_state(bar, broker.context)
        assert strategy.daily_loss_count == 0

    def test_buggy_miscounts_profitable_trade_as_loss(self):
        """BUG: bar.close(32990) < entry(33000) wrongly counted as loss."""
        broker = SimulatedBroker(point_value=200)
        strategy = GeneralAtrBreakoutBuggy()
        strategy.entry_price = 33000
        strategy.prev_position_size = 1
        strategy.last_bar_date = datetime(2026, 3, 5).date()

        bar = _bar(datetime(2026, 3, 5, 22, 0), 32990)
        strategy._update_daily_state(bar, broker.context)
        assert strategy.daily_loss_count == 1  # BUG

    def test_fixed_counts_real_losing_trade(self):
        """Actual SL loss must be counted."""
        broker = SimulatedBroker(point_value=200)
        broker.position_size = 1
        broker.position_side = OrderSide.LONG
        broker.entry_price = 33000
        broker.entry_tag = "Long"
        broker._current_bar_dt = "2026-03-05 22:00"
        broker._close_position("ExitLong", 32940, 5)  # SL loss
        assert broker.trades[-1].pnl < 0

        strategy = GeneralAtrBreakoutFixed()
        strategy.entry_price = 33000
        strategy.prev_position_size = 1
        strategy.last_bar_date = datetime(2026, 3, 5).date()

        bar = _bar(datetime(2026, 3, 5, 22, 0), 32950)
        strategy._update_daily_state(bar, broker.context)
        assert strategy.daily_loss_count == 1

    def test_daily_reset_on_new_date(self):
        """Loss counter resets when calendar date changes."""
        broker = SimulatedBroker(point_value=200)
        strategy = GeneralAtrBreakoutFixed()
        strategy.daily_loss_count = 2
        strategy.last_bar_date = datetime(2026, 3, 5).date()
        strategy.prev_position_size = 0

        bar = _bar(datetime(2026, 3, 6, 15, 0), 33000)
        strategy._update_daily_state(bar, broker.context)
        assert strategy.daily_loss_count == 0


# ── Integration Tests: Full Engine Run ───────────────────────────────────────

class TestIntegrationSessionClose:
    """Full backtest engine runs verifying session close behavior.

    Each test uses 21 warmup bars (no entry) + specific test bars.
    The first test bar triggers entry (lower_wick > 0 -> cond_B),
    and subsequent bars verify session close behavior.
    """

    def test_fixed_closes_at_day_session_end(self):
        """Entry at 11:45, must close at 12:45 TWT (day session end).

        Matches TV Trade 1: entry 2/4 08:45, exit 2/4 12:45 via session close.
        """
        bars = _make_warmup_bars()
        bars.append(_bar(datetime(2026, 2, 4, 11, 45), 33000))
        bars.append(_bar(datetime(2026, 2, 4, 12, 45), 33010))

        engine = BacktestEngine(GeneralAtrBreakoutFixed(), point_value=200)
        result = engine.run(bars)

        # entry_dt uses bar END time (fill time): 11:45 + 60min = 12:45
        t = [t for t in result.trades if "12:45" in t.entry_dt]
        assert len(t) == 1, f"Expected 1 trade entering at 12:45 (fill time), got {len(t)}"
        assert t[0].exit_tag == "Session Close"
        assert "13:45" in t[0].exit_dt
        assert t[0].pnl == (33010 - 33000) * 200

    def test_buggy_misses_day_session_close(self):
        """BUG: buggy code does NOT close at 12:45 TWT."""
        bars = _make_warmup_bars()
        bars.append(_bar(datetime(2026, 2, 4, 11, 45), 33000))
        bars.append(_bar(datetime(2026, 2, 4, 12, 45), 33010))

        engine = BacktestEngine(GeneralAtrBreakoutBuggy(), point_value=200)
        result = engine.run(bars)

        session_closes = [t for t in result.trades if t.exit_tag == "Session Close"]
        assert len(session_closes) == 0, "Buggy code should NOT detect 12:45 as session close"

    def test_fixed_no_false_close_at_2000(self):
        """20:00 TWT is mid night-session — position must NOT session-close."""
        bars = _make_warmup_bars()
        bars.append(_bar(datetime(2026, 2, 3, 19, 0), 33000))
        bars.append(_bar(datetime(2026, 2, 3, 20, 0), 33010))
        bars.append(_bar(datetime(2026, 2, 3, 21, 0), 33020))

        engine = BacktestEngine(GeneralAtrBreakoutFixed(), point_value=200)
        result = engine.run(bars)

        assert len(result.trades) >= 1
        assert result.trades[0].exit_tag != "Session Close", \
            "20:00 TWT is mid-session, should not trigger session close"

    def test_buggy_falsely_closes_at_2000(self):
        """BUG: buggy code closes at 20:00 TWT (hour==20 false match)."""
        bars = _make_warmup_bars()
        bars.append(_bar(datetime(2026, 2, 3, 19, 0), 33000))
        bars.append(_bar(datetime(2026, 2, 3, 20, 0), 33010))
        bars.append(_bar(datetime(2026, 2, 3, 21, 0), 33020))

        engine = BacktestEngine(GeneralAtrBreakoutBuggy(), point_value=200)
        result = engine.run(bars)

        # exit_dt uses bar END time: 20:00 + 60min = 21:00
        assert any(
            t.exit_tag == "Session Close" and "21:00" in t.exit_dt
            for t in result.trades
        ), "Buggy code should falsely close at 21:00 (fill time for 20:00 bar)"

    def test_no_entry_on_session_close_bar(self):
        """Entry must be blocked on session-close bars (matching Pine)."""
        bars = _make_warmup_bars()
        bars.append(_bar(datetime(2026, 2, 4, 12, 45), 33000))
        bars.append(_bar(datetime(2026, 2, 4, 15, 0), 33010))

        engine = BacktestEngine(GeneralAtrBreakoutFixed(), point_value=200)
        result = engine.run(bars)

        for trade in result.trades:
            # entry_dt uses bar END time: 12:45 + 60min = 13:45
            assert "13:45" not in trade.entry_dt, \
                "Should not enter on session-close bar"

    def test_night_session_close_at_0400(self):
        """Position must close at 04:00 TWT (night session end)."""
        bars = _make_warmup_bars()
        bars.append(_bar(datetime(2026, 2, 4, 3, 0), 33000))
        bars.append(_bar(datetime(2026, 2, 4, 4, 0), 33010))

        engine = BacktestEngine(GeneralAtrBreakoutFixed(), point_value=200)
        result = engine.run(bars)

        # entry_dt uses bar END time: 03:00 + 60min = 04:00
        t = [t for t in result.trades if "04:00" in t.entry_dt]
        assert len(t) == 1
        assert t[0].exit_tag == "Session Close"
        assert "05:00" in t[0].exit_dt


# ── is_last_n_bars_of_session tests ────────────────────────────────────────

class TestIsLastNBarsOfSession:
    """Tests for is_last_n_bars_of_session with various N and intervals."""

    # ── 1-min bars, day session (close 13:45 = 825) ──

    def test_1m_n1_last_bar(self):
        assert is_last_n_bars_of_session(datetime(2026, 2, 4, 13, 44), 1, 1) is True

    def test_1m_n1_not_last(self):
        assert is_last_n_bars_of_session(datetime(2026, 2, 4, 13, 43), 1, 1) is False

    def test_1m_n5_last_5(self):
        for m in (40, 41, 42, 43, 44):
            assert is_last_n_bars_of_session(datetime(2026, 2, 4, 13, m), 1, 5) is True

    def test_1m_n5_not_last(self):
        assert is_last_n_bars_of_session(datetime(2026, 2, 4, 13, 39), 1, 5) is False

    # ── 5-min bars, day session ──

    def test_5m_n1_last_bar(self):
        assert is_last_n_bars_of_session(datetime(2026, 2, 4, 13, 40), 5, 1) is True

    def test_5m_n1_not_last(self):
        assert is_last_n_bars_of_session(datetime(2026, 2, 4, 13, 35), 5, 1) is False

    def test_5m_n2_last_2(self):
        assert is_last_n_bars_of_session(datetime(2026, 2, 4, 13, 35), 5, 2) is True
        assert is_last_n_bars_of_session(datetime(2026, 2, 4, 13, 40), 5, 2) is True

    def test_5m_n2_not_last(self):
        assert is_last_n_bars_of_session(datetime(2026, 2, 4, 13, 30), 5, 2) is False

    # ── 15-min bars, day session ──

    def test_15m_n1_last_bar(self):
        assert is_last_n_bars_of_session(datetime(2026, 2, 4, 13, 30), 15, 1) is True

    def test_15m_n1_not_last(self):
        assert is_last_n_bars_of_session(datetime(2026, 2, 4, 13, 15), 15, 1) is False

    def test_15m_n3_last_3(self):
        for m in (0, 15, 30):
            assert is_last_n_bars_of_session(datetime(2026, 2, 4, 13, m), 15, 3) is True

    def test_15m_n3_not_last(self):
        assert is_last_n_bars_of_session(datetime(2026, 2, 4, 12, 45), 15, 3) is False

    # ── 60-min bars, day session ──

    def test_60m_n1_last_bar(self):
        assert is_last_n_bars_of_session(datetime(2026, 2, 4, 12, 45), 60, 1) is True

    def test_60m_n1_not_last(self):
        assert is_last_n_bars_of_session(datetime(2026, 2, 4, 11, 45), 60, 1) is False

    def test_60m_n2_last_2(self):
        assert is_last_n_bars_of_session(datetime(2026, 2, 4, 11, 45), 60, 2) is True

    # ── 240-min (4H) bars, day session ──

    def test_240m_day_n1_last(self):
        # Day session 4H: 08:45, 12:45.  12:45 is last.
        assert is_last_n_bars_of_session(datetime(2026, 2, 4, 12, 45), 240, 1) is True

    def test_240m_day_n1_not_last(self):
        assert is_last_n_bars_of_session(datetime(2026, 2, 4, 8, 45), 240, 1) is False

    def test_240m_day_n2_both(self):
        # Both 4H bars in day session are "last 2"
        assert is_last_n_bars_of_session(datetime(2026, 2, 4, 8, 45), 240, 2) is True

    # ── Night session (after midnight, close 05:00 = 300) ──

    def test_1m_night_n1_last(self):
        assert is_last_n_bars_of_session(datetime(2026, 2, 5, 4, 59), 1, 1) is True

    def test_1m_night_n1_not_last(self):
        assert is_last_n_bars_of_session(datetime(2026, 2, 5, 4, 58), 1, 1) is False

    def test_60m_night_n1_last(self):
        assert is_last_n_bars_of_session(datetime(2026, 2, 5, 4, 0), 60, 1) is True

    def test_60m_night_n1_not_last(self):
        assert is_last_n_bars_of_session(datetime(2026, 2, 5, 3, 0), 60, 1) is False

    def test_60m_night_n2(self):
        assert is_last_n_bars_of_session(datetime(2026, 2, 5, 3, 0), 60, 2) is True

    # ── Night session (before midnight, 15:00-23:59) ──

    def test_240m_night_before_midnight_not_last(self):
        # 23:00 bar: mins_to_close = 60+300=360. 1*240=240 < 360 → False
        assert is_last_n_bars_of_session(datetime(2026, 2, 4, 23, 0), 240, 1) is False

    def test_240m_night_before_midnight_n2(self):
        # 23:00 bar: 2*240=480 >= 360 → True
        assert is_last_n_bars_of_session(datetime(2026, 2, 4, 23, 0), 240, 2) is True

    def test_60m_night_2200_not_last(self):
        # 22:00 bar: mins_to_close = 120+300=420. 1*60=60 < 420 → False
        assert is_last_n_bars_of_session(datetime(2026, 2, 4, 22, 0), 60, 1) is False

    # ── Edge: first bar of session (never last for n=1) ──

    def test_first_bar_day_not_last(self):
        assert is_last_n_bars_of_session(datetime(2026, 2, 4, 8, 45), 60, 1) is False

    def test_first_bar_night_not_last(self):
        assert is_last_n_bars_of_session(datetime(2026, 2, 4, 15, 0), 60, 1) is False

    # ── Edge: between sessions (should return False) ──

    def test_between_sessions_false(self):
        assert is_last_n_bars_of_session(datetime(2026, 2, 4, 7, 0), 60, 1) is False
        assert is_last_n_bars_of_session(datetime(2026, 2, 4, 14, 0), 60, 1) is False

    # ── Consistency: n=1 matches is_last_bar_of_session ──

    @pytest.mark.parametrize("dt,km", [
        (datetime(2026, 2, 4, 12, 45), 60),
        (datetime(2026, 2, 4, 13, 30), 15),
        (datetime(2026, 2, 4, 13, 40), 5),
        (datetime(2026, 2, 4, 13, 44), 1),
        (datetime(2026, 2, 5, 4, 0), 60),
        (datetime(2026, 2, 5, 4, 45), 15),
        (datetime(2026, 2, 4, 11, 45), 60),
        (datetime(2026, 2, 4, 10, 0), 60),
    ])
    def test_n1_matches_is_last_bar(self, dt, km):
        assert is_last_n_bars_of_session(dt, km, 1) == is_last_bar_of_session(dt, km)


# ── minutes_until_close tests ─────────────────────────────────────────────

class TestMinutesUntilClose:
    """Tests for the pure minutes_until_close function in sessions.py."""

    def test_am_session_mid(self):
        assert minutes_until_close(datetime(2026, 3, 17, 10, 0)) == 225

    def test_am_session_near_close(self):
        assert minutes_until_close(datetime(2026, 3, 17, 13, 43)) == 2

    def test_night_session_early(self):
        assert minutes_until_close(datetime(2026, 3, 17, 15, 30)) == 810

    def test_night_after_midnight(self):
        assert minutes_until_close(datetime(2026, 3, 18, 2, 0)) == 180

    def test_night_near_close(self):
        assert minutes_until_close(datetime(2026, 3, 18, 4, 58)) == 2

    def test_between_sessions(self):
        assert minutes_until_close(datetime(2026, 3, 17, 6, 0)) is None
        assert minutes_until_close(datetime(2026, 3, 17, 14, 0)) is None

    def test_sunday(self):
        assert minutes_until_close(datetime(2026, 3, 22, 12, 0)) is None

    def test_saturday_before_5am(self):
        assert minutes_until_close(datetime(2026, 3, 21, 3, 0)) == 120

    def test_saturday_after_5am(self):
        assert minutes_until_close(datetime(2026, 3, 21, 6, 0)) is None

    def test_monday_before_5am_closed(self):
        assert minutes_until_close(datetime(2026, 3, 16, 3, 0)) is None
