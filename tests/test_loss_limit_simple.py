"""Eric's rule: a night snapshot from 13:45-15:00, otherwise the account net."""
import json
from datetime import datetime

from src.live.loss_baseline import DailyLossBaseline
from src.live.trading_guard import TradingGuard


def _rights(net):
    return {
        "realized_pnl": str(net),
        "realized_cost": "0",
        "tax": "0",
        "float_pnl": "0",
    }


def _tracker(path):
    return DailyLossBaseline(str(path), "ACCT")


def test_a_afternoon_read_is_the_night_snapshot(tmp_path):
    """A 14:5x read is the snapshot. At night L is net minus that read."""
    tr = _tracker(tmp_path / "a.json")
    tr.observe(_rights(10000), datetime(2026, 11, 10, 14, 55))
    night = tr.observe(_rights(7000), datetime(2026, 11, 10, 20, 0))
    assert tr.snap_net == 10000
    assert night.basis == "snapshot"
    assert night.loss_today == -3000
    assert night.account_net == 7000


def test_b_first_read_after_1500_with_no_file_is_master(tmp_path):
    tr = _tracker(tmp_path / "b.json")
    view = tr.observe(_rights(-12000), datetime(2026, 11, 10, 16, 0))
    assert view.basis == "master"
    assert view.loss_today == view.account_net == -12000
    assert tr.snap_net is None


def test_c_night_restart_restores_a_same_key_window_file(tmp_path):
    path = tmp_path / "c.json"
    first = _tracker(path)
    first.observe(_rights(10000), datetime(2026, 11, 10, 14, 52))
    first.save()
    fresh = _tracker(path)
    fresh.load(datetime(2026, 11, 10, 21, 0))
    view = fresh.observe(_rights(7000), datetime(2026, 11, 10, 21, 0))
    assert fresh.snap_net == 10000
    assert view.basis == "snapshot"
    assert view.loss_today == -3000

    bare = json.loads(path.read_text(encoding="utf-8"))
    del bare["read_at"]
    path.write_text(json.dumps(bare), encoding="utf-8")
    missing = _tracker(path)
    missing.load(datetime(2026, 11, 10, 22, 0))
    lost = missing.observe(_rights(7000), datetime(2026, 11, 10, 22, 0))
    assert missing.snap_net is None
    assert lost.basis == "master"
    assert lost.loss_today == lost.account_net == 7000


def test_d_from_0500_onward_is_master(tmp_path):
    """05:00, the morning, and the day session are the full account net."""
    tr = _tracker(tmp_path / "d.json")
    tr.observe(_rights(10000), datetime(2026, 11, 10, 14, 55))
    night = tr.observe(_rights(7000), datetime(2026, 11, 10, 23, 0))
    assert night.loss_today == -3000
    for when in (
        datetime(2026, 11, 11, 5, 0),
        datetime(2026, 11, 11, 6, 30),
        datetime(2026, 11, 11, 8, 40),
        datetime(2026, 11, 11, 10, 0),
    ):
        view = tr.observe(_rights(7000), when)
        assert view.basis == "master", when
        assert view.loss_today == view.account_net == 7000


def test_oi_cached_rights_do_not_move_the_snapshot(tmp_path, monkeypatch):
    """An OpenInterest row must not observe the cached rights reply."""
    import queue
    from types import SimpleNamespace

    import run_backtest as rb
    from src.live.account_monitor import AccountMonitor

    monitor = AccountMonitor()
    monitor.update_rights(_rights(10000))
    monitor.begin_oi_query()
    tr = _tracker(tmp_path / "oi.json")
    stamp = datetime(2026, 11, 10, 14, 50)
    monkeypatch.setattr(rb, "_taipei_now", lambda: stamp)
    tr.observe(monitor.rights, stamp)
    assert tr.snap_net == 10000
    monitor.update_rights(_rights(5000))

    class _Var:
        def set(self, value):
            self.value = value

    app = SimpleNamespace(
        _account_monitor=monitor,
        _loss_baseline=tr,
        _trading_mode="paper",
        _trading_guard=SimpleNamespace(
            daily_loss_limit=0, paused=False, fill_pending=False,
            roll_day=lambda key: None),
        _loss_view=None,
        real_pos_var=_Var(), real_equity_var=_Var(), real_available_var=_Var(),
        real_pnl_var=_Var(), real_realized_var=_Var(), real_fees_var=_Var(),
        real_net_var=_Var(), real_maint_var=_Var(), real_loss_limit_var=_Var(),
    )
    app._observe_loss_baseline = (
        lambda now: rb.BacktestApp._observe_loss_baseline(app, now))
    app._update_real_account_display = (
        lambda: rb.BacktestApp._update_real_account_display(app))
    previous = rb._app
    rb._app = app
    try:
        while True:
            try:
                rb._ui_queue.get_nowait()
            except queue.Empty:
                break
        monkeypatch.setattr(
            rb, "_taipei_now", lambda: datetime(2026, 11, 10, 14, 55))
        rb.SKOrderLibEvents().OnOpenInterest(
            "TF,ACCT,TM0000,B,1,0,44774.0,15,2,ID")
        rb.BacktestApp._process_ui_queue(app)
    finally:
        rb._app = previous
    assert tr.snap_net == 10000
    assert tr.snap_read.replace(tzinfo=None) == stamp


def test_e_invalid_input_is_the_account_net(tmp_path):
    tr = _tracker(tmp_path / "e.json")
    tr.observe(_rights(10000), datetime(2026, 11, 10, 14, 55))
    night = tr.observe(_rights(7000), datetime(2026, 11, 10, 20, 0))
    assert night.loss_today == -3000
    skipped = tr.observe({"valid": False}, datetime(2026, 11, 10, 21, 0))
    assert skipped.basis == "master"
    assert skipped.loss_today == skipped.account_net == 7000
    guard = TradingGuard()
    guard.daily_loss_limit = 20000
    guard.update_pnl(night.loss_today, day_key=night.day_key)
    assert guard.paused is False
