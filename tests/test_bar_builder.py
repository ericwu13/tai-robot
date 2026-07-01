"""Tests for bar builder: tick aggregation into OHLCV bars."""

from datetime import datetime, timezone, timedelta

from src.market_data.bar_builder import BarBuilder
from src.market_data.models import Tick

TST = timezone(timedelta(hours=8))


def _tick(price: int, qty: int, second: int, minute: int = 0) -> Tick:
    dt = datetime(2024, 1, 15, 9, minute, second, tzinfo=TST)
    return Tick(symbol="TXFD0", dt=dt, price=price, qty=qty)


class TestBarBuilder:
    def test_single_tick_no_bar(self):
        bb = BarBuilder("TXFD0", 60)
        result = bb.on_tick(_tick(20000, 1, 0))
        assert result is None
        assert bb.current_bar is not None
        assert bb.current_bar.open == 20000

    def test_ticks_in_same_bar(self):
        bb = BarBuilder("TXFD0", 60)
        bb.on_tick(_tick(20000, 1, 0))
        bb.on_tick(_tick(20050, 2, 10))
        bb.on_tick(_tick(19950, 3, 30))
        bb.on_tick(_tick(20020, 1, 59))

        bar = bb.current_bar
        assert bar.open == 20000
        assert bar.high == 20050
        assert bar.low == 19950
        assert bar.close == 20020
        assert bar.volume == 7  # 1+2+3+1

    def test_new_bar_completes_previous(self):
        bb = BarBuilder("TXFD0", 60)
        bb.on_tick(_tick(20000, 1, 0, minute=0))
        bb.on_tick(_tick(20050, 2, 30, minute=0))

        # Tick in next minute -> completes the first bar
        completed = bb.on_tick(_tick(20100, 1, 0, minute=1))

        assert completed is not None
        assert completed.open == 20000
        assert completed.high == 20050
        assert completed.close == 20050
        assert completed.volume == 3
        assert len(bb.completed_bars) == 1

    def test_flush_current_bar(self):
        bb = BarBuilder("TXFD0", 60)
        bb.on_tick(_tick(20000, 1, 0))
        bb.on_tick(_tick(20050, 2, 30))

        flushed = bb.flush()
        assert flushed is not None
        assert flushed.close == 20050
        assert len(bb.completed_bars) == 1

    def test_flush_empty(self):
        bb = BarBuilder("TXFD0", 60)
        assert bb.flush() is None

    def test_multiple_bars(self):
        bb = BarBuilder("TXFD0", 60)
        for minute in range(3):
            bb.on_tick(_tick(20000 + minute * 10, 1, 0, minute=minute))
            bb.on_tick(_tick(20000 + minute * 10 + 5, 1, 30, minute=minute))

        # Force last bar
        bb.on_tick(_tick(20100, 1, 0, minute=3))
        assert len(bb.completed_bars) == 3

    def test_event_bus_integration(self, event_bus):
        from src.gateway.event_bus import EventType

        received = []
        event_bus.subscribe(EventType.BAR, lambda e: received.append(e.data))

        bb = BarBuilder("TXFD0", 60, event_bus)
        bb.on_tick(_tick(20000, 1, 0, minute=0))
        bb.on_tick(_tick(20100, 1, 0, minute=1))

        # Drain events
        event_bus.drain()
        assert len(received) == 1
        assert received[0].open == 20000


class TestOutOfOrderTickGuard:
    """Issue #78: drop stale/out-of-order ticks from post-reconnect replay."""

    def test_stale_tick_is_dropped(self):
        """A tick older than the last accepted tick must not mutate the bar."""
        bb = BarBuilder("TXFD0", 60)
        bb.on_tick(_tick(20000, 1, 0, minute=0))
        bb.on_tick(_tick(20050, 2, 30, minute=0))

        snapshot = (bb.current_bar.open, bb.current_bar.high,
                    bb.current_bar.low, bb.current_bar.close,
                    bb.current_bar.volume)

        # Burst replay: a tick with a timestamp that goes backwards.
        result = bb.on_tick(_tick(19000, 99, 10, minute=0))

        assert result is None
        assert (bb.current_bar.open, bb.current_bar.high,
                bb.current_bar.low, bb.current_bar.close,
                bb.current_bar.volume) == snapshot

    def test_duplicate_tick_is_dropped(self):
        """A tick at exactly the last timestamp (<=) is also dropped."""
        bb = BarBuilder("TXFD0", 60)
        bb.on_tick(_tick(20000, 1, 30, minute=0))
        vol_before = bb.current_bar.volume

        # Same timestamp, different price/qty — treated as a duplicate.
        result = bb.on_tick(_tick(20100, 5, 30, minute=0))

        assert result is None
        assert bb.current_bar.volume == vol_before
        assert bb.current_bar.close == 20000  # unchanged

    def test_stale_tick_does_not_spawn_new_bar(self):
        """A stale tick in a later minute must not create a spurious bar."""
        bb = BarBuilder("TXFD0", 60)
        bb.on_tick(_tick(20000, 1, 0, minute=5))  # last = 09:05:00
        # Older tick that aligns to an earlier minute — would wrongly finalize
        # and start a new bar without the guard.
        result = bb.on_tick(_tick(19900, 1, 0, minute=2))

        assert result is None
        assert len(bb.completed_bars) == 0
        assert bb.current_bar.dt.minute == 5

    def test_reset_accepts_first_tick_regardless(self):
        """After reset_stale_tracking(), the next tick is always accepted."""
        bb = BarBuilder("TXFD0", 60)
        bb.on_tick(_tick(20000, 1, 30, minute=10))  # last = 09:10:30

        # Reconnect: replay restarts from an earlier checkpoint.
        bb.reset_stale_tracking()

        result = bb.on_tick(_tick(19950, 3, 0, minute=2))  # older timestamp
        # New bar started fresh from the "older" replayed tick.
        assert bb.current_bar is not None
        assert bb.current_bar.dt.minute == 2
        assert bb.current_bar.open == 19950
        # Finalized the minute-10 bar on the boundary cross.
        assert result is not None
        assert result.dt.minute == 10
