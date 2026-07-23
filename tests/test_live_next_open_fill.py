"""Live entry fills use the next bar's open, matching backtest next_open mode.

Incident: a 15-min strategy signaled on the day session's last bar (13:30,
close 44,930). That bar only finalizes when the first night 1-min bar arrives
at ~15:01, so the real IOC order filled at the night open (44,774) while the
sim price recorded the 75-minute-stale day close — a 156-point phantom gap.
Backtest (fill_mode="next_open") fills the same signal at the 15:00 bar's
open, i.e. ~44,774. These tests pin live/paper fills to the same convention.

Every entry-fill test here is designed to FAIL against the old code, which
filled at the signal bar's close (44,930-style values).
"""

from datetime import datetime, timedelta

from src.backtest.broker import SimulatedBroker, Order, OrderSide
from src.backtest.strategy import BacktestStrategy
from src.market_data.data_store import DataStore
from src.market_data.models import Bar
from src.live.live_runner import LiveRunner, LiveState, _TZ_TAIPEI


DAY_CLOSE_PRICE = 44930   # last trade of the day session (13:44)
NIGHT_OPEN_PRICE = 44774  # first trade of the night session (15:00)


# ── Broker-level: entry_fill_price parameter ──

def _long_entry():
    return Order(tag="Long", side=OrderSide.LONG, qty=1)


class TestBrokerEntryFillPrice:
    def test_entry_fill_price_overrides_close(self):
        broker = SimulatedBroker(point_value=200)
        broker.queue_entry(_long_entry())
        broker.on_bar_close(0, DAY_CLOSE_PRICE,
                            entry_fill_price=NIGHT_OPEN_PRICE)
        assert broker.entry_price == NIGHT_OPEN_PRICE

    def test_default_still_fills_at_close(self):
        broker = SimulatedBroker(point_value=200)
        broker.queue_entry(_long_entry())
        broker.on_bar_close(0, DAY_CLOSE_PRICE)
        assert broker.entry_price == DAY_CLOSE_PRICE

    def test_zero_entry_fill_price_falls_back_to_close(self):
        broker = SimulatedBroker(point_value=200)
        broker.queue_entry(_long_entry())
        broker.on_bar_close(0, DAY_CLOSE_PRICE, entry_fill_price=0)
        assert broker.entry_price == DAY_CLOSE_PRICE

    def test_market_close_ignores_entry_fill_price(self):
        """broker.close() is market-on-close: always this bar's close,
        exactly like backtest — entry_fill_price must never leak into it."""
        broker = SimulatedBroker(point_value=200)
        broker.queue_entry(_long_entry())
        broker.on_bar_close(0, 44700)
        broker.queue_market_close("Session Close", "Long")
        broker.on_bar_close(1, DAY_CLOSE_PRICE,
                            entry_fill_price=NIGHT_OPEN_PRICE)
        assert len(broker.trades) == 1
        assert broker.trades[0].exit_price == DAY_CLOSE_PRICE


# ── LiveRunner-level ──

class BoundaryEntryStrategy(BacktestStrategy):
    """15-min strategy that enters only on the day session's last bar."""
    kline_type = 0
    kline_minute = 15

    def on_bar(self, bar, data_store, broker):
        if broker.position_size == 0 and (bar.dt.hour, bar.dt.minute) == (13, 30):
            broker.entry("Long", OrderSide.LONG)

    def required_bars(self):
        return 1


class OneMinAlwaysLong(BacktestStrategy):
    kline_type = 0
    kline_minute = 1

    def on_bar(self, bar, data_store, broker):
        if broker.position_size == 0:
            broker.entry("Long", OrderSide.LONG)

    def required_bars(self):
        return 1


def _bar_1m(dt: datetime, o: int, c: int | None = None, symbol: str = "TX00") -> Bar:
    c = o if c is None else c
    return Bar(symbol=symbol, dt=dt, open=o, high=max(o, c) + 5,
               low=min(o, c) - 5, close=c, volume=10, interval=60)


def _fresh_tick_dt() -> datetime:
    return datetime.now(_TZ_TAIPEI).replace(tzinfo=None)


class TestSessionBoundaryFill:
    def test_15min_boundary_entry_fills_at_night_open(self, tmp_path):
        """The incident scenario: signal on the 13:30 bar (day close 44,930),
        processed at 15:01 when the first night 1-min bar lands. The fill must
        be the night open (= partial bar's open = what backtest next_open and
        the real IOC order both got), not the stale day close."""
        runner = LiveRunner(BoundaryEntryStrategy(), "TX00",
                            point_value=200, log_dir=str(tmp_path))
        runner.state = LiveState.RUNNING
        decisions = []
        runner.on("on_decision", decisions.append)

        base = datetime(2026, 7, 22, 13, 30)
        # Day session's last 15-min window: 13:30..13:44, drifting up to 44,930
        for i in range(15):
            price = DAY_CLOSE_PRICE - (14 - i)
            runner.feed_1m_bar(_bar_1m(base + timedelta(minutes=i), price))
        assert runner.broker.position_size == 0  # window not finalized yet

        # First night 1-min bar (15:00) opens 156 points lower — this
        # finalizes the 13:30 bar and triggers the signal + fill.
        night = datetime(2026, 7, 22, 15, 0)
        runner.feed_1m_bar(_bar_1m(night, NIGHT_OPEN_PRICE))

        assert runner.broker.position_size == 1
        assert runner.broker.entry_price == NIGHT_OPEN_PRICE

        fills = [d for d in decisions if d["action"] == "ENTRY_FILL"]
        assert len(fills) == 1
        assert fills[0]["price"] == NIGHT_OPEN_PRICE
        assert fills[0]["reason"] == "filled at next-open"


class TestOneMinTickFill:
    def _runner(self, tmp_path):
        runner = LiveRunner(OneMinAlwaysLong(), "TX00",
                            point_value=200, log_dir=str(tmp_path))
        runner.state = LiveState.RUNNING
        return runner

    def test_fills_at_fresh_tick(self, tmp_path):
        runner = self._runner(tmp_path)
        runner.latest_tick_price = NIGHT_OPEN_PRICE
        runner.latest_tick_dt = _fresh_tick_dt()
        runner.feed_1m_bar(_bar_1m(datetime(2026, 7, 22, 15, 0), DAY_CLOSE_PRICE))
        assert runner.broker.entry_price == NIGHT_OPEN_PRICE

    def test_stale_tick_falls_back_to_bar_close(self, tmp_path):
        runner = self._runner(tmp_path)
        runner.latest_tick_price = NIGHT_OPEN_PRICE
        runner.latest_tick_dt = _fresh_tick_dt() - timedelta(minutes=10)
        runner.feed_1m_bar(_bar_1m(datetime(2026, 7, 22, 15, 0), DAY_CLOSE_PRICE))
        assert runner.broker.entry_price == DAY_CLOSE_PRICE

    def test_no_tick_falls_back_to_bar_close(self, tmp_path):
        runner = self._runner(tmp_path)
        runner.feed_1m_bar(_bar_1m(datetime(2026, 7, 22, 15, 0), DAY_CLOSE_PRICE))
        assert runner.broker.entry_price == DAY_CLOSE_PRICE
