"""Fault-injection tests for issue #168 (restore fidelity).

Each test plants one resume fault and asserts the guard that must catch it:

* pending exit orders dropped by to_dict/from_dict
* strategy calls exit(stop=None) after a restart and wipes the restored stop
* open position resumes with no stop at all
* an exit fill still in flight is cleared on resume and sent again
* the first tick after restore is not evaluated (gap left unprotected)
* a flat session keeps a stale stop that closes the next trade
* a restored fill gate never restarts the fill poller (flat and open)
* a flat sim with a deferred close confirms the opposite real book
* the resumed poller is armed without the deferred close's sign
* live mode keeps a paper loss-limit stop
* the restore alert fires again
* a zero or negative stop/limit is restored as a real price

Nothing here is strategy-specific. The fixtures only model the generic
failure: a fresh strategy instance has no stop latched at entry.
"""

from datetime import datetime
from types import SimpleNamespace

from src.backtest.broker import Order, OrderSide, SimulatedBroker
from src.backtest.strategy import BacktestStrategy
from src.live.live_runner import LiveRunner, _LOSS_LIMIT_STOP_TAG
from src.live.session_store import load_session
from src.live.trading_guard import TradingGuard, apply_resume_guard
from src.market_data.data_store import DataStore
from src.market_data.models import Bar
from tests.test_live_runner import _kline


class SilentStrategy(BacktestStrategy):
    """Does not trade. A resumed stop must work without its help."""

    kline_type = 0
    kline_minute = 1

    def on_bar(self, bar: Bar, data_store: DataStore, broker) -> None:
        return None

    def required_bars(self) -> int:
        return 1


class WipesStopStrategy(BacktestStrategy):
    """In a position, queues exit(stop=None) — the post-restart shape.

    The previous process latched the stop at entry. This new instance
    has none, and exit() replaces the restored order.
    """

    kline_type = 0
    kline_minute = 1

    def on_bar(self, bar: Bar, data_store: DataStore, broker) -> None:
        if broker.position_size > 0:
            broker.exit("SL", "Long", stop=None)

    def exit_levels(self) -> dict:
        return {}

    def required_bars(self) -> int:
        return 1


class AlwaysEntryStrategy(BacktestStrategy):
    """Queues an entry on every bar, in a position or flat."""

    kline_type = 0
    kline_minute = 1

    def on_bar(self, bar: Bar, data_store: DataStore, broker) -> None:
        broker.entry("new", OrderSide.LONG)

    def required_bars(self) -> int:
        return 1


def _open_broker(side="LONG", entry=22500, stop=None, limit=None, tag="SL"):
    entry_tag = "Long" if side == "LONG" else "Short"
    data = {
        "point_value": 200,
        "fill_mode": "on_close",
        "position_size": 1,
        "position_side": side,
        "entry_price": entry,
        "entry_tag": entry_tag,
        "entry_bar_index": 4,
        "trades": [],
        "equity_curve": [],
        "_cumulative_pnl": 0,
        "_bar_index": 10,
        "_exit_bar_index": -1,
    }
    if stop is not None or limit is not None:
        data["pending_exits"] = [{
            "tag": tag,
            "side": "LONG",
            "qty": 0,
            "limit": limit,
            "stop": stop,
            "from_entry": entry_tag,
        }]
    return data


def _resume(tmp_path, strategy, session, *, mode="paper", loss_limit=10000):
    runner = LiveRunner(
        strategy, "TX00", point_value=200,
        log_dir=str(tmp_path), bot_name="restore168",
    )
    runner.trading_mode = mode
    runner.daily_loss_limit = loss_limit
    alerts: list[str] = []
    blocked: list[dict] = []
    runner.restore_session(session)
    runner.on("on_alert", alerts.append)
    runner.on("on_decision", lambda d: blocked.append(d)
              if d.get("action") == "SIGNAL_BLOCKED" else None)
    runner.feed_warmup_bars([
        _kline("2026-02-20 09:00", 22500, 22510, 22490, 22500),
        _kline("2026-02-20 09:01", 22500, 22510, 22490, 22500),
    ])
    return runner, alerts, blocked


def _bar(dt, o, h, low, c) -> Bar:
    return Bar(
        symbol="TX00", dt=dt, open=o, high=h, low=low, close=c,
        volume=10, interval=60,
    )


def test_pending_exits_survive_to_dict_roundtrip():
    """Round-trip keeps limit, stop, from_entry, and tag."""
    broker = SimulatedBroker(point_value=50)
    broker.position_size = 1
    broker.position_side = OrderSide.SHORT
    broker.entry_price = 18000
    broker.entry_tag = "S"
    broker.queue_exit(Order(
        tag="TP", side=OrderSide.LONG, qty=0,
        limit=17900, stop=18150, from_entry="S",
    ))

    restored = SimulatedBroker.from_dict(broker.to_dict())

    assert len(restored._pending_exits) == 1
    order = restored._pending_exits[0]
    assert order.tag == "TP"
    assert order.limit == 17900
    assert order.stop == 18150
    assert order.from_entry == "S"
    assert restored.position_size == 1
    assert restored.position_side == OrderSide.SHORT


def test_from_dict_without_pending_exits_stays_empty():
    """Sessions saved before #168 have no pending_exits key."""
    restored = SimulatedBroker.from_dict({
        "point_value": 50,
        "position_size": 1,
        "position_side": "LONG",
        "entry_price": 18000,
        "entry_tag": "entry1",
        "entry_bar_index": 5,
        "trades": [],
        "equity_curve": [],
        "_cumulative_pnl": 0,
        "_bar_index": 5,
    })
    assert restored._pending_exits == []
    assert restored.position_size == 1


def test_session_save_persists_exit_levels_and_pending_exits(tmp_path):
    runner = LiveRunner(
        SilentStrategy(), "TX00", point_value=200,
        log_dir=str(tmp_path), bot_name="save168",
    )
    runner.broker.position_size = 1
    runner.broker.position_side = OrderSide.LONG
    runner.broker.entry_price = 22500
    runner.broker.entry_tag = "Long"
    runner.broker.entry_bar_index = 3
    runner.broker.queue_exit(Order(
        tag="SL", side=OrderSide.LONG, qty=0,
        limit=23000, stop=22400, from_entry="Long",
    ))
    runner._auto_save_session()

    data = load_session(runner.session_path)
    assert data["broker"]["pending_exits"][0]["stop"] == 22400
    assert data["broker"]["pending_exits"][0]["limit"] == 23000
    assert data["broker"]["pending_exits"][0]["from_entry"] == "Long"
    assert data["broker"]["pending_exits"][0]["tag"] == "SL"
    assert data["exit_levels"]["stop"] == 22400
    assert data["exit_levels"]["limit"] == 23000
    assert data["restored_without_stop"] is False


def test_resume_bar_crossing_restored_stop_closes_trade(tmp_path):
    """A bar that crosses the restored stop closes, even after exit(stop=None)."""
    session = {
        "bar_index": 10,
        "broker": _open_broker(stop=22400, limit=23000, tag="SL"),
        "exit_levels": {
            "stop": 22400, "limit": 23000,
            "from_entry": "Long", "tag": "SL",
        },
    }
    runner, alerts, _blocked = _resume(tmp_path, WipesStopStrategy(), session)
    assert alerts == []
    assert runner.restored_without_stop is False

    # Does not reach the stop. The fresh strategy wipes it inside this bar.
    runner.feed_1m_bar(_bar(
        datetime(2026, 3, 2, 9, 0), 22520, 22580, 22450, 22540))
    assert any(o.stop == 22400 for o in runner.broker._pending_exits), (
        "strategy exit(stop=None) erased the restored stop"
    )
    assert runner.broker.position_size == 1

    # Crosses the restored stop. Must close at the stop (open is above it).
    runner.feed_1m_bar(_bar(
        datetime(2026, 3, 2, 9, 1), 22420, 22440, 22380, 22400))
    assert runner.broker.position_size == 0
    assert len(runner.broker.trades) == 1
    assert runner.broker.trades[-1].exit_price == 22400
    assert runner.broker.trades[-1].exit_tag == "SL"


def test_first_tick_after_restore_gap_fills_at_market(tmp_path):
    """First tick, before any live bar, fills a gapped stop at the tick.

    The session has strategy exit levels and no pending exit — the stop
    lived on the strategy, not on the broker. Backtest gap rule: fill at
    the market price that went through the stop, not at the stop itself.
    """
    session = {
        "bar_index": 10,
        "broker": _open_broker(),  # no pending exit
        "exit_levels": {
            "stop": 22400, "limit": 23000,
            "from_entry": "Long", "tag": "SL",
        },
    }
    runner, _alerts, _blocked = _resume(tmp_path, SilentStrategy(), session)
    assert runner._awaiting_restore_tick is True
    assert any(o.stop == 22400 for o in runner.broker._pending_exits)

    result = runner.check_tick_exit(22000, "2026-03-02 09:05:01")

    assert result is not None
    assert result["price"] == 22000  # market, not the 22400 stop
    assert runner.broker.position_size == 0
    assert runner.broker.trades[-1].exit_price == 22000
    assert runner._awaiting_restore_tick is False


def test_resume_without_stop_blocks_entries_and_alerts(tmp_path):
    """No recoverable stop: alert, paper loss-limit stop, no re-entry."""
    session = {
        "bar_index": 10,
        "broker": _open_broker(),  # open, no stop, no exit_levels
    }
    runner, alerts, blocked = _resume(
        tmp_path, AlwaysEntryStrategy(), session,
        mode="paper", loss_limit=10000,
    )

    assert runner.restored_without_stop is True
    assert alerts, "expected a restore alert"
    assert "no recoverable stop" in alerts[0]
    assert "Paper protective stop" in alerts[0]
    stops = [o for o in runner.broker._pending_exits
             if o.tag == _LOSS_LIMIT_STOP_TAG]
    assert len(stops) == 1
    # 10000 NTD / 200 per point / 1 lot = 50 points under the long entry.
    assert stops[0].stop == 22450

    # Gap through that protective stop. The strategy tries to re-enter
    # on the same bar the position closes.
    runner.feed_1m_bar(_bar(
        datetime(2026, 3, 2, 9, 0), 22400, 22420, 22300, 22350))

    assert runner.broker.trades, "paper loss-limit stop did not close"
    assert runner.broker.trades[-1].exit_tag == _LOSS_LIMIT_STOP_TAG
    assert runner.broker.position_size == 0, (
        "new entry filled while restored_without_stop"
    )
    assert any(d.get("tag") == "RESTORED_WITHOUT_STOP" for d in blocked), (
        "blocked entry was not recorded"
    )


def test_paper_loss_limit_stop_sits_against_the_open_side(tmp_path):
    """Short protective stop is above the entry; long is below it."""
    session = {
        "bar_index": 10,
        "broker": _open_broker(side="SHORT", entry=22500),
    }
    runner, alerts, _blocked = _resume(
        tmp_path, SilentStrategy(), session, mode="paper", loss_limit=10000,
    )
    stops = [o.stop for o in runner.broker._pending_exits
             if o.tag == _LOSS_LIMIT_STOP_TAG]
    assert stops == [22550]
    assert alerts and "alert-only" not in alerts[0]


def test_live_resume_without_stop_is_alert_only(tmp_path):
    """semi_auto/auto: alert and block entries, do not invent a stop."""
    session = {
        "bar_index": 10,
        "broker": _open_broker(),
    }
    runner, alerts, blocked = _resume(
        tmp_path, AlwaysEntryStrategy(), session,
        mode="auto", loss_limit=10000,
    )

    assert runner.restored_without_stop is True
    assert alerts and "alert-only" in alerts[0]
    assert "no recoverable stop" in alerts[0]
    assert all(o.tag != _LOSS_LIMIT_STOP_TAG
               for o in runner.broker._pending_exits)
    assert not any(o.stop is not None for o in runner.broker._pending_exits)

    runner.feed_1m_bar(_bar(
        datetime(2026, 3, 2, 9, 0), 22400, 22420, 22300, 22350))

    assert runner.broker.trades == []
    assert runner.broker.position_size == 1, "live mode closed on a synthetic stop"
    assert any(d.get("tag") == "RESTORED_WITHOUT_STOP" for d in blocked)


def test_resume_inflight_exit_does_not_send_new_exit(tmp_path):
    """Persisted fill_pending survives reset+reconcile. No second exit."""
    guard = TradingGuard()
    guard.on_entry_sent()
    guard.on_fill_pending("exit")
    guard.defer_close({"action": "TRADE_CLOSE", "side": "LONG", "price": 22400})

    runner = LiveRunner(
        SilentStrategy(), "TX00", point_value=200,
        log_dir=str(tmp_path), bot_name="inflight168",
    )
    runner.set_guard_state_provider(guard.to_dict)
    runner.broker.position_size = 1
    runner.broker.position_side = OrderSide.LONG
    runner.broker.entry_price = 22500
    runner.broker.entry_tag = "Long"
    runner._auto_save_session()

    saved = load_session(runner.session_path)["trading_guard"]
    assert saved["fill_pending"] is True
    assert saved["fill_pending_type"] == "exit"
    # The close was parked beside an in-flight exit. It is replayed only
    # on the next entry confirm, so it must not be written into the session.
    assert saved["deferred_close"] is None

    # Deploy calls reset() before resume. That is the wipe under test.
    fresh = TradingGuard()
    fresh.reset()
    outcome = apply_resume_guard(
        fresh, position_open=True, real_ok=True, persisted=saved,
    )
    assert outcome["kept_inflight"] is True
    assert outcome["cleared_stale"] is False
    assert fresh.fill_pending is True
    assert fresh.fill_pending_type == "exit"
    assert fresh.real_entry_confirmed is True
    assert fresh._deferred_close is None

    verdict, _details = fresh.decide("auto", "TRADE_CLOSE", "LONG")
    assert verdict == fresh.BLOCK_FILL_PENDING
    assert verdict != fresh.SEND_EXIT
    # The blocked decision must not clear the in-flight exit gate.
    assert fresh.fill_pending is True
    assert fresh.fill_pending_type == "exit"
    assert fresh._deferred_close is None


def test_legacy_resume_without_guard_snapshot_still_clears_fill_pending():
    """#79: no saved gate means a stale in-memory fill_pending is cleared."""
    guard = TradingGuard()
    guard.reset()
    guard.fill_pending = True
    guard.fill_pending_type = "entry"
    guard.defer_close({"action": "TRADE_CLOSE", "side": "LONG"})

    outcome = apply_resume_guard(
        guard, position_open=True, real_ok=True, persisted=None,
    )
    assert outcome["kept_inflight"] is False
    assert outcome["cleared_stale"] is True
    assert guard.fill_pending is False
    assert guard._deferred_close is None
    assert guard.real_entry_confirmed is True
    verdict, _details = guard.decide("auto", "TRADE_CLOSE", "LONG")
    assert verdict == guard.SEND_EXIT


def test_flat_session_drops_stale_stop_before_new_entry(tmp_path):
    """A flat book with a leftover SL must not close the next trade.

    from_dict used to restore pending exits even when position_size is 0,
    and nothing afterwards cleared them. The next long then stopped out
    at the stale price — in auto mode check_tick_exit would send that.
    """
    stale = {
        "point_value": 200,
        "fill_mode": "on_close",
        "position_size": 0,
        "position_side": None,
        "entry_price": 0,
        "entry_tag": "",
        "entry_bar_index": 0,
        "trades": [],
        "equity_curve": [],
        "_cumulative_pnl": 0,
        "_bar_index": 10,
        "_exit_bar_index": 9,
        "pending_exits": [{
            "tag": "SL",
            "side": "LONG",
            "qty": 0,
            "limit": None,
            "stop": 22400,
            "from_entry": "Long",
        }],
    }
    restored = SimulatedBroker.from_dict(stale)
    assert restored.position_size == 0
    assert restored._pending_exits == []

    session = {"bar_index": 10, "broker": stale}
    runner, _alerts, _blocked = _resume(
        tmp_path, SilentStrategy(), session, mode="auto",
    )
    assert runner.broker.position_size == 0
    assert runner.broker._pending_exits == []

    # The new long is opened after resume. The stale SL at 22400 must
    # not be sitting on the broker when the tick arrives.
    runner.broker.position_size = 1
    runner.broker.position_side = OrderSide.LONG
    runner.broker.entry_price = 22500
    runner.broker.entry_tag = "Long"
    assert runner.check_tick_exit(22400) is None
    assert runner.broker.position_size == 1
    assert runner.broker.trades == []


def test_live_resume_drops_existing_paper_safety_stop(tmp_path):
    """R7: auto/semi_auto must not keep a paper loss-limit stop.

    A session saved in paper mode can already hold the synthetic stop.
    Live resume is alert-only — that order has to be removed, not left
    where check_exits would send it.
    """
    session = {
        "bar_index": 10,
        "broker": _open_broker(stop=22450, tag=_LOSS_LIMIT_STOP_TAG),
    }
    runner, alerts, _blocked = _resume(
        tmp_path, SilentStrategy(), session, mode="auto", loss_limit=10000,
    )
    assert runner.restored_without_stop is True
    assert all(o.tag != _LOSS_LIMIT_STOP_TAG for o in runner.broker._pending_exits)
    assert not any(o.stop is not None for o in runner.broker._pending_exits)
    assert alerts and "alert-only" in alerts[0]
    assert "Paper protective stop" not in alerts[0]

    runner.feed_1m_bar(_bar(
        datetime(2026, 3, 2, 9, 0), 22400, 22420, 22300, 22350))
    assert runner.broker.trades == []
    assert runner.broker.position_size == 1


def test_restore_alert_fires_once(tmp_path):
    """R8: a second reconcile must not raise the restore alert again."""
    session = {"bar_index": 10, "broker": _open_broker()}
    runner, alerts, _blocked = _resume(
        tmp_path, SilentStrategy(), session, mode="paper", loss_limit=10000,
    )
    assert len(alerts) == 1
    runner._reconcile_restored_exits(alert=True)
    runner._emit_restore_alert()
    assert len(alerts) == 1


def test_zero_and_negative_prices_are_not_stops():
    """R9: 0 and negative prices are not a stop or a target.

    A negative short stop matches every positive tick (price >= stop).
    """
    restored = SimulatedBroker.from_dict({
        "point_value": 200,
        "position_size": 1,
        "position_side": "SHORT",
        "entry_price": 22500,
        "entry_tag": "Short",
        "entry_bar_index": 4,
        "trades": [],
        "equity_curve": [],
        "pending_exits": [{
            "tag": "SL",
            "side": "SHORT",
            "qty": 0,
            "limit": 0,
            "stop": -100,
            "from_entry": "Short",
        }],
    })
    assert len(restored._pending_exits) == 1
    order = restored._pending_exits[0]
    assert order.stop is None
    assert order.limit is None
    restored.check_exits(11, 22500, 22520, 22480, 22510, "2026-03-02 09:00")
    assert restored.position_size == 1
    assert restored.trades == []


def test_zero_or_negative_saved_levels_do_not_count_as_a_stop(tmp_path):
    """R9: exit_levels of 0 / negative are ignored, paper stop still applies."""
    session = {
        "bar_index": 10,
        "broker": _open_broker(),
        "exit_levels": {
            "stop": 0,
            "limit": -5,
            "from_entry": "Long",
            "tag": "SL",
            "entry_bar_index": 4,
        },
    }
    runner, alerts, _blocked = _resume(
        tmp_path, SilentStrategy(), session, mode="paper", loss_limit=10000,
    )
    assert runner.restored_without_stop is True
    assert alerts and "no recoverable stop" in alerts[0]
    foreign = [
        o for o in runner.broker._pending_exits
        if o.tag != _LOSS_LIMIT_STOP_TAG
    ]
    assert foreign == []
    prot = [
        o for o in runner.broker._pending_exits
        if o.tag == _LOSS_LIMIT_STOP_TAG
    ]
    assert len(prot) == 1
    assert prot[0].stop == 22450


class _Log:
    def __init__(self):
        self.lines = []

    def append(self, line, tag="status"):
        self.lines.append(line)


class _Root:
    def __init__(self):
        self.pending = {}
        self._next = 1

    def after(self, ms, fn, *args):
        token = self._next
        self._next += 1
        self.pending[token] = (ms, fn, args)
        return token

    def after_cancel(self, token):
        self.pending.pop(token, None)


def _resume_guard_app(*, position_size: int):
    """Minimal app for the resume-guard path. Deploy has already reset."""
    import run_backtest as rb
    from src.live.fill_poller import FillPoller
    from types import SimpleNamespace

    app = rb.BacktestApp.__new__(rb.BacktestApp)
    app.root = _Root()
    app.live_log = _Log()
    app._alerts = []
    app._alert = lambda title, msg: app._alerts.append((title, msg))
    guard = TradingGuard()
    guard.reset()
    app._trading_guard = guard
    app._fill_poller = FillPoller(guard, timeout=10.0)
    app._fill_poller.reset()
    app._fill_poll_timer_id = None
    app._futures_account = "ACC"
    side = SimpleNamespace(value="LONG") if position_size else None
    app._live_runner = SimpleNamespace(
        symbol="TX00",
        broker=SimpleNamespace(
            position_size=position_size,
            position_side=side,
            entry_bar_index=4,
        ),
    )
    signed = 1 if position_size else 0
    app._account_monitor = SimpleNamespace(
        get_signed_position=lambda prefix: signed,
        oi_snapshot_received=True,
    )
    return app


def _enable_fill_com(monkeypatch):
    import run_backtest as rb
    monkeypatch.setattr(rb, "_com_available", True)
    monkeypatch.setattr(rb, "skO", object())


def test_resume_in_position_restarts_fill_poller(monkeypatch):
    """Open position + in-flight exit: poller restarts, deal row can clear."""
    app = _resume_guard_app(position_size=1)
    assert app._fill_poller.active is False
    _enable_fill_com(monkeypatch)
    app._reconcile_resumed_guard(
        {
            "trading_guard": {
                "fill_pending": True,
                "fill_pending_type": "exit",
                "deferred_close": {
                    "action": "TRADE_CLOSE", "side": "LONG", "price": 22400,
                },
            },
        },
        "auto",
        "TX00",
    )
    guard = app._trading_guard
    poller = app._fill_poller
    assert guard.fill_pending is True
    assert guard.fill_pending_type == "exit"
    # The snapshot's close belongs to an exit, not a pending entry.
    assert guard._deferred_close is None
    assert poller.active is True
    assert poller.action_type == "exit"
    assert app.root.pending, "fill poll was not scheduled"
    assert app._alerts, "in-flight fill resumed with no alert"
    verdict, _details = guard.decide("auto", "TRADE_CLOSE", "LONG")
    assert verdict == guard.BLOCK_FILL_PENDING

    # The deal-row clear in run_backtest requires the poller to be active.
    assert poller.on_deal_confirmed("exit") is not None
    poller.confirm()
    assert guard.fill_pending is False
    verdict, _details = guard.decide("auto", "TRADE_CLOSE", "LONG")
    assert verdict != guard.BLOCK_FILL_PENDING


def test_resume_flat_restarts_entry_fill_poller(monkeypatch):
    """Flat book + restored entry gate: poller restarts and timeout clears it."""
    app = _resume_guard_app(position_size=0)
    assert app._fill_poller.active is False
    _enable_fill_com(monkeypatch)
    app._reconcile_resumed_guard(
        {
            "trading_guard": {
                "fill_pending": True,
                "fill_pending_type": "entry",
                "deferred_close": None,
            },
        },
        "auto",
        "TX00",
    )
    guard = app._trading_guard
    poller = app._fill_poller
    assert guard.fill_pending is True
    assert guard.fill_pending_type == "entry"
    assert poller.active is True
    assert poller.action_type == "entry"
    assert app.root.pending, "fill poll was not scheduled"
    assert app._alerts, "flat entry gate resumed with no alert"
    verdict, _details = guard.decide("auto", "ENTRY_FILL", "LONG")
    assert verdict == guard.BLOCK_FILL_PENDING

    action = poller.check_poll(
        now=poller._start_time + poller.FILL_POLL_TIMEOUT)
    assert action.type == "timeout"
    poller.timeout()
    assert guard.fill_pending is False
    assert poller.active is False
    verdict, _details = guard.decide("auto", "ENTRY_FILL", "LONG")
    assert verdict != guard.BLOCK_FILL_PENDING


def _guard_session(kind, deferred=None):
    return {
        "trading_guard": {
            "fill_pending": True,
            "fill_pending_type": kind,
            "deferred_close": deferred,
        },
    }


def test_opposite_real_book_does_not_arm_or_confirm(monkeypatch):
    """Sim LONG vs real SHORT: do not arm, do not confirm, do not replay.

    Arming the entry poller used to treat any non-zero OI as the fill.
    _on_fill_confirmed then replayed the saved close as a sell, adding
    to the real short. timeout() assumed the same fill after 10s.
    """
    app = _resume_guard_app(position_size=1)
    app._account_monitor = SimpleNamespace(
        get_signed_position=lambda prefix: -1,
        oi_snapshot_received=True,
        positions=[],
    )
    _enable_fill_com(monkeypatch)
    app._reconcile_resumed_guard(
        _guard_session("entry", {
            "action": "TRADE_CLOSE", "side": "LONG", "price": 22400,
        }),
        "auto",
        "TX00",
    )
    guard = app._trading_guard
    text = "".join(app.live_log.lines)
    assert "OPPOSITE" in text
    assert guard.real_entry_confirmed is False
    assert guard.fill_pending is True
    assert guard._deferred_close is None
    assert app._fill_poller.active is False
    assert app._alerts, "opposite resume blocked orders with no alert"
    assert "not started" in app._alerts[0][1]
    update = app._fill_poller.on_position_update(-1)
    assert update is None
    assert guard.real_entry_confirmed is False


def test_resumed_entry_timeout_does_not_confirm_opposite_side():
    """timeout() must not assume a resumed entry filled on the wrong sign."""
    from src.live.fill_poller import FillPoller

    guard = TradingGuard()
    guard.on_fill_pending("entry")
    guard.defer_close({"action": "TRADE_CLOSE", "side": "LONG", "price": 22400})
    poller = FillPoller(guard, timeout=10.0)
    poller.start("entry", -1, expected_sign=1, resumed=True)
    assert poller.on_position_update(-1) is None
    poller.timeout()
    assert guard.real_entry_confirmed is False
    assert guard.fill_pending is True
    assert guard._deferred_close["action"] == "TRADE_CLOSE"


def test_unknown_snapshot_does_not_confirm_inflight_exit(monkeypatch):
    """No OI snapshot: a 0 reading is not 'already flat'. Keep the gate."""
    for pos in (0, 1):
        app = _resume_guard_app(position_size=pos)
        app._account_monitor = SimpleNamespace(
            get_signed_position=lambda prefix: 0,
            oi_snapshot_received=False,
            positions=[],
        )
        _enable_fill_com(monkeypatch)
        app._reconcile_resumed_guard(_guard_session("exit"), "auto", "TX00")
        guard = app._trading_guard
        assert guard.fill_pending is True, pos
        assert guard.real_entry_confirmed is False
        assert app._fill_poller.active is False
        assert app._alerts, pos
        assert not any("FILL_CONFIRMED" in line for line in app.live_log.lines)
        verdict, _details = guard.decide("auto", "TRADE_CLOSE", "LONG")
        assert verdict == guard.BLOCK_FILL_PENDING


def test_no_com_resume_does_not_autoconfirm_entry(monkeypatch):
    """COM down at resume must not auto-confirm an in-flight entry.

    The real book matches, so #79 may mark the position confirmed.
    start() must still not clear the in-flight gate: with no COM it
    used to auto-confirm and the saved close could be replayed.
    """
    import run_backtest as rb
    app = _resume_guard_app(position_size=1)
    app._account_monitor = SimpleNamespace(
        get_signed_position=lambda prefix: 1,
        oi_snapshot_received=True,
        positions=[],
    )
    monkeypatch.setattr(rb, "_com_available", False)
    app._reconcile_resumed_guard(
        _guard_session("entry", {
            "action": "TRADE_CLOSE", "side": "LONG", "price": 1,
        }),
        "auto",
        "TX00",
    )
    guard = app._trading_guard
    assert guard.fill_pending is True
    assert guard._deferred_close is not None
    assert app._fill_poller.active is False
    assert app._alerts
    assert "not started" in app._alerts[0][1]
    assert not any("FILL_CONFIRMED" in line for line in app.live_log.lines)


def test_flat_sim_deferred_long_does_not_confirm_real_short(monkeypatch):
    """Sim flat, deferred close-LONG, real SHORT: do not arm or replay.

    The open-position book check used to be skipped because the sim was
    flat, so the poller started with expected_sign=None and confirmed on
    the real -1. The saved close then sold into that short.
    """
    app = _resume_guard_app(position_size=0)
    app._account_monitor = SimpleNamespace(
        get_signed_position=lambda prefix: -1,
        oi_snapshot_received=True,
        positions=[],
    )
    _enable_fill_com(monkeypatch)
    app._reconcile_resumed_guard(
        _guard_session("entry", {
            "action": "TRADE_CLOSE", "side": "LONG", "price": 22400,
        }),
        "auto",
        "TX00",
    )
    guard = app._trading_guard
    assert app._fill_poller.active is False
    assert app._fill_poller.on_position_update(-1) is None
    assert guard.fill_pending is True
    assert guard._deferred_close is None
    assert guard.real_entry_confirmed is False
    assert app._alerts
    assert "not started" in app._alerts[0][1]
    assert not any("FILL_CONFIRMED" in line for line in app.live_log.lines)
    verdict, _details = guard.decide("auto", "TRADE_CLOSE", "LONG")
    assert verdict == guard.BLOCK_FILL_PENDING


def test_flat_deferred_close_missing_book_drops_close(monkeypatch):
    """Flat sim + saved close: no snapshot, or no COM, drops the close.

    Arming either way has no position side to constrain the poller, so
    the close must not be left for a later confirm to replay.
    """
    import run_backtest as rb

    cases = (
        ("snapshot", True, False, 0),
        ("com", False, True, 1),
    )
    for name, com_on, snapshot, signed in cases:
        app = _resume_guard_app(position_size=0)
        app._account_monitor = SimpleNamespace(
            get_signed_position=lambda prefix, signed=signed: signed,
            oi_snapshot_received=snapshot,
            positions=[],
        )
        monkeypatch.setattr(rb, "_com_available", com_on)
        if com_on:
            monkeypatch.setattr(rb, "skO", object())
        app._reconcile_resumed_guard(
            _guard_session("entry", {
                "action": "TRADE_CLOSE", "side": "LONG", "price": 22400,
            }),
            "auto",
            "TX00",
        )
        guard = app._trading_guard
        assert app._fill_poller.active is False, name
        assert guard.fill_pending is True, name
        assert guard._deferred_close is None, name
        assert guard.real_entry_confirmed is False, name
        assert app._alerts, name
        assert "not started" in app._alerts[0][1], name


def test_flat_deferred_close_wires_expected_sign_to_poller(monkeypatch):
    """A matching real long reaches the poller as expected_sign=+1, resumed.

    Calling _start_fill_polling(action_type) without those kwargs leaves
    the poller direction-blind: the opposite OI confirms the entry.
    """
    app = _resume_guard_app(position_size=0)
    app._account_monitor = SimpleNamespace(
        get_signed_position=lambda prefix: 1,
        oi_snapshot_received=True,
        positions=[],
    )
    _enable_fill_com(monkeypatch)
    app._reconcile_resumed_guard(
        _guard_session("entry", {
            "action": "TRADE_CLOSE", "side": "LONG", "price": 22400,
        }),
        "auto",
        "TX00",
    )
    poller = app._fill_poller
    guard = app._trading_guard
    assert poller.active is True
    assert poller._expected_sign == 1
    assert poller._resumed is True
    assert guard._deferred_close["side"] == "LONG"
    assert poller.on_position_update(-1) is None
    assert guard.real_entry_confirmed is False
    confirmed = poller.on_position_update(1)
    assert confirmed is not None and confirmed.type == "confirmed"
