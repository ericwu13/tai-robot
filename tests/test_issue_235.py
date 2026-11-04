"""Force-close must book the last processed bar (issue #235).

``_bar_index`` has already moved past the last bar when a session-end
close or a news flatten runs. Booking the trade there makes the next
bar's ``_check_for_trade_close`` log a second TRADE_CLOSE and, if the
real entry is still confirmed, send another exit.

These tests fail on the pre-fix code: the next bar logs a second
TRADE_CLOSE, a repeated checker call logs the same trade again, and
the mocked order handler is invoked a second time with new_close=2.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import run_backtest as rb

from src.backtest.broker import OrderSide, Trade
from src.backtest.strategy import BacktestStrategy
from src.live.live_runner import LiveRunner, LiveState
from src.live.trading_guard import TradingGuard
from src.market_data.models import Bar


class FlatStrategy(BacktestStrategy):
    """Never trades. The next bar after a force-close must stay flat."""

    kline_type = 0
    kline_minute = 15

    def on_bar(self, bar, data_store, broker) -> None:
        return None

    def required_bars(self) -> int:
        return 1


class OneShotTpStrategy(BacktestStrategy):
    """Enter once, then hold a TP/SL until it fills. No re-entry."""

    kline_type = 0
    kline_minute = 15

    def on_bar(self, bar, data_store, broker) -> None:
        if broker.position_size == 0 and not broker.trades:
            broker.entry("Long", OrderSide.LONG)
        elif broker.position_size > 0:
            broker.exit("TP", "Long", limit=22600, stop=22400)

    def required_bars(self) -> int:
        return 1


class TickExitStrategy(BacktestStrategy):
    """Enter once and park a TP/SL for check_tick_exit. No re-entry."""

    kline_type = 0
    kline_minute = 15

    def on_bar(self, bar, data_store, broker) -> None:
        if broker.position_size == 0 and not broker.trades:
            broker.entry("Long", OrderSide.LONG)
        elif broker.position_size > 0:
            broker.exit("Exit", "Long", limit=22600, stop=22450)

    def required_bars(self) -> int:
        return 1


def _kline(dt_str, o=22500, h=22510, l=22490, c=22505, v=100):
    dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M")
    return f"{dt.strftime('%m/%d/%Y %H:%M')},{o},{h},{l},{c},{v}"


def _bar(symbol, dt, close, *, high=None, low=None, open_=None, interval=900):
    return Bar(
        symbol=symbol,
        dt=dt,
        open=close if open_ is None else open_,
        high=close if high is None else high,
        low=close if low is None else low,
        close=close,
        volume=100,
        interval=interval,
    )


def _runner(tmp_path, strategy=None):
    runner = LiveRunner(
        strategy or FlatStrategy(), "TX00", point_value=200,
        log_dir=str(tmp_path),
    )
    runner.feed_warmup_bars([
        _kline("2026-11-04 08:45"),
        _kline("2026-11-04 09:00"),
    ])
    assert runner.state == LiveState.RUNNING
    assert runner._bar_index >= 2
    return runner


def _open_long(runner, price=22500):
    runner.broker.position_size = 1
    runner.broker.position_side = OrderSide.LONG
    runner.broker.entry_price = price
    runner.broker.entry_tag = "Long"
    runner.broker.entry_bar_index = runner._bar_index - 1
    runner.broker._entry_dt = "2026-11-04 09:00:00"


def _watch(runner):
    rows = []
    runner.on("on_decision", rows.append)
    return rows


def _closes(rows):
    return [d for d in rows if d["action"] == "TRADE_CLOSE"]


def _next_bar(runner, **kwargs):
    last = runner._aggregated_bars[-1]
    return _bar(
        runner.symbol,
        last.dt + timedelta(minutes=15),
        last.close,
        interval=runner.target_interval,
        **kwargs,
    )


def _session_end_app(runner):
    return SimpleNamespace(
        _live_runner=runner,
        _SESSION_END_CLOSE_MINUTES=2,
        _SETTLEMENT_END_CLOSE_MINUTES=5,
        _get_latest_price=lambda: (22510, "test"),
        _live_log_msg=lambda *a, **k: None,
        _session_end_exit_pending=False,
    )


def _run_session_end_close(runner, monkeypatch, app=None):
    monkeypatch.setattr(rb, "minutes_until_session_close", lambda *_a, **_k: 1)
    app = app or _session_end_app(runner)
    rb.BacktestApp._check_session_end_close(app)
    return app


class TestForceCloseBooksLastProcessedBar:
    def test_session_end_next_bar_logs_no_second_trade_close(
            self, tmp_path, monkeypatch):
        """Session-end force close, then the next bar: one trade, same
        P&L, and no TRADE_CLOSE row."""
        runner = _runner(tmp_path)
        rows = _watch(runner)
        _open_long(runner)
        expected = max(runner._bar_index - 1, runner.broker.entry_bar_index)
        _run_session_end_close(runner, monkeypatch)

        assert len(runner.broker.trades) == 1
        trade = runner.broker.trades[-1]
        assert trade.exit_bar_index == expected
        assert trade.exit_bar_index < runner._bar_index
        pnl = trade.pnl
        assert _closes(rows) == []
        assert any(d["action"] == "FORCE_CLOSE" for d in rows)

        runner._process_aggregated_bar(_next_bar(runner))

        assert _closes(rows) == [], (
            "next bar logged a second TRADE_CLOSE for the force-closed trade"
        )
        assert len(runner.broker.trades) == 1
        assert runner.broker.trades[-1].pnl == pnl
        assert runner.broker._cumulative_pnl == pnl

        # Backstop: asking the checker for this trade's own index is a no-op.
        runner._check_for_trade_close(
            runner._aggregated_bars[-1], trade.exit_bar_index)
        assert _closes(rows) == []

    def test_news_risk_off_flatten_next_bar_logs_no_second_trade_close(
            self, tmp_path):
        """The news risk_off flatten (force_close_position) has the same
        off-by-one. The next bar must not log another TRADE_CLOSE."""
        runner = _runner(tmp_path)
        rows = _watch(runner)
        _open_long(runner)
        expected = max(runner._bar_index - 1, runner.broker.entry_bar_index)
        price = runner.force_close_position("news_risk_off", "news flatten")

        assert price > 0
        assert len(runner.broker.trades) == 1
        trade = runner.broker.trades[-1]
        assert trade.exit_bar_index == expected
        assert trade.exit_bar_index < runner._bar_index
        pnl = trade.pnl
        assert _closes(rows) == []
        forced = [d for d in rows if d["action"] == "FORCE_CLOSE"]
        assert len(forced) == 1
        assert forced[0]["tag"] == "news_risk_off"

        runner._process_aggregated_bar(_next_bar(runner))

        assert _closes(rows) == [], (
            "next bar logged a second TRADE_CLOSE after the news flatten"
        )
        assert len(runner.broker.trades) == 1
        assert runner.broker.trades[-1].pnl == pnl
        assert runner.broker._cumulative_pnl == pnl
        runner._check_for_trade_close(
            runner._aggregated_bars[-1], trade.exit_bar_index)
        assert _closes(rows) == []

    def test_force_close_bar_index_floors_at_entry_bar(self, tmp_path):
        """max(_bar_index - 1, entry_bar_index): an entry ahead of the
        counter is not booked on an earlier bar."""
        runner = _runner(tmp_path)
        _open_long(runner)
        runner.broker.entry_bar_index = runner._bar_index + 4
        price = runner.force_close_position("stop", "floor")
        assert price > 0
        assert runner.broker.trades[-1].exit_bar_index == runner._bar_index + 4


class TestCloseLoggedOnce:
    def test_normal_tp_close_logs_exactly_once(self, tmp_path):
        runner = _runner(tmp_path, OneShotTpStrategy())
        rows = _watch(runner)

        runner._process_aggregated_bar(_bar(
            "TX00", datetime(2026, 11, 4, 9, 15), 22500, interval=900))
        assert runner.broker.position_size == 1

        runner._process_aggregated_bar(_bar(
            "TX00", datetime(2026, 11, 4, 9, 30), 22510,
            high=22520, low=22480, open_=22500, interval=900))
        assert runner.broker.position_size == 1
        assert runner.broker._pending_exits

        runner._process_aggregated_bar(_bar(
            "TX00", datetime(2026, 11, 4, 9, 45), 22620,
            high=22680, low=22500, open_=22540, interval=900))
        assert runner.broker.position_size == 0
        assert len(runner.broker.trades) == 1
        assert len(_closes(rows)) == 1
        exit_idx = runner.broker.trades[-1].exit_bar_index

        runner._process_aggregated_bar(_bar(
            "TX00", datetime(2026, 11, 4, 10, 0), 22610, interval=900))
        assert len(_closes(rows)) == 1
        assert len(runner.broker.trades) == 1

        bar = runner._aggregated_bars[-1]
        runner._check_for_trade_close(bar, exit_idx)
        runner._check_for_trade_close(bar, exit_idx)
        assert len(_closes(rows)) == 1

    def test_tick_exit_logs_exactly_once(self, tmp_path):
        runner = _runner(tmp_path, TickExitStrategy())
        rows = _watch(runner)

        runner._process_aggregated_bar(_bar(
            "TX00", datetime(2026, 11, 4, 9, 15), 22500, interval=900))
        runner._process_aggregated_bar(_bar(
            "TX00", datetime(2026, 11, 4, 9, 30), 22505,
            high=22520, low=22480, open_=22500, interval=900))
        assert runner.broker.position_size == 1
        assert runner.broker._pending_exits

        result = runner.check_tick_exit(22440, "2026-11-04 09:31:30")
        assert result is not None
        assert runner.broker.position_size == 0
        assert len(_closes(rows)) == 1
        exit_idx = runner.broker.trades[-1].exit_bar_index

        runner._process_aggregated_bar(_bar(
            "TX00", datetime(2026, 11, 4, 9, 45), 22450, interval=900))
        assert len(_closes(rows)) == 1
        assert len(runner.broker.trades) == 1

        runner._check_for_trade_close(runner._aggregated_bars[-1], exit_idx)
        assert len(_closes(rows)) == 1

    def test_different_trade_is_logged_and_a_repeat_is_not(self, tmp_path):
        """The backstop is per trade identity, not a single global flag."""
        runner = _runner(tmp_path)
        _open_long(runner)
        runner.force_close_position("stop", "first")
        assert len(runner.broker.trades) == 1

        rows = _watch(runner)
        second = Trade(
            tag="Long2", side=OrderSide.LONG, qty=1,
            entry_price=22600, exit_price=22700,
            entry_bar_index=10, exit_bar_index=11,
            pnl=20000, exit_tag="TP",
            entry_dt="2026-11-04 10:00:00",
            exit_dt="2026-11-04 10:15:00",
        )
        runner.broker.trades.append(second)
        bar = runner._aggregated_bars[-1]
        runner._check_for_trade_close(bar, second.exit_bar_index)
        assert len(_closes(rows)) == 1
        runner._check_for_trade_close(bar, second.exit_bar_index)
        assert len(_closes(rows)) == 1


class TestNoSecondRealExit:
    def test_next_bar_sends_no_second_real_order_when_entry_still_confirmed(
            self, tmp_path, monkeypatch):
        """Order handler mocked. real_entry_confirmed stays set after the
        force close (missed fill confirmation, fill gate clear). The next
        bar must not send a second real exit."""
        runner = _runner(tmp_path)
        _open_long(runner)
        guard = TradingGuard()
        guard.real_entry_confirmed = True
        sent = []
        errors = []

        def _send(buy_sell, order_symbol, action_type, price, new_close=2,
                  **kwargs):
            sent.append({
                "action_type": action_type,
                "new_close": new_close,
                "buy_sell": buy_sell,
                "price": price,
            })
            return True

        app = _session_end_app(runner)
        app._trading_guard = guard
        app._trading_mode = "auto"
        app._log_order_decision = lambda *a, **k: None
        app._send_real_order = _send

        def _on_decision(decision):
            try:
                rb.BacktestApp._handle_semi_auto_order(app, decision)
            except Exception as exc:  # surface a swallowed callback error
                errors.append(exc)

        runner.on("on_decision", _on_decision)
        _run_session_end_close(runner, monkeypatch, app)

        assert errors == []
        assert len(sent) == 1
        assert sent[0]["action_type"] == "exit"
        assert sent[0]["new_close"] == 1
        # The missed-confirmation case the duplicate TRADE_CLOSE would hit.
        assert guard.real_entry_confirmed is True
        guard.fill_pending = False
        guard.halted = False

        runner._process_aggregated_bar(_next_bar(runner))

        assert errors == []
        assert guard.real_entry_confirmed is True
        assert len(sent) == 1, f"next bar sent a second real order: {sent}"
        assert all(row["new_close"] != 2 for row in sent)
