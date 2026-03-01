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
