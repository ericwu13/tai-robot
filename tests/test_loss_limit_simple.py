"""The one daily-loss rule: a valid 15:00 snapshot, or the account net."""
from datetime import datetime

from src.live.loss_baseline import DailyLossBaseline
from src.live.trading_guard import TradingGuard, trading_day_key


def _rights(net, f22, floating=0):
    """Rights whose account net (f11 - f2 - f3 + f1) is ``net``."""
    return {
        "realized_pnl": str(net - floating),
        "realized_cost": "0",
        "tax": "0",
        "float_pnl": str(floating),
        "start_equity": f22,
    }


def _tracker(path):
    return DailyLossBaseline(str(path), "ACCT")


def _roll(tr, when, rights):
    """Feed the read before 15:00, then the roll, in this same process."""
    tr.observe(rights, when.replace(hour=14, minute=59, second=0, microsecond=0))
    return tr.observe(rights, when)


def test_opening_gap_counts_in_the_new_day(tmp_path):
    """The 15:00 gap belongs to the new day. The snapshot is the 14:55 read."""
    tr = _tracker(tmp_path / "gap.json")
    tr.observe(_rights(10000, "AAA"), datetime(2026, 11, 10, 14, 55))
    rolled = tr.observe(_rights(7000, "AAA"), datetime(2026, 11, 10, 15, 2))
    assert tr.snap_net == 10000
    assert tr.snap_f22 == "AAA"
    assert rolled.basis == "snapshot"
    assert rolled.loss_today == -3000


def test_oi_cached_rights_do_not_move_the_baseline(tmp_path, monkeypatch):
    """An OpenInterest row re-reads cached rights. That must not observe."""
    import queue
    from types import SimpleNamespace

    import run_backtest as rb
    from src.live.account_monitor import AccountMonitor

    monitor = AccountMonitor()
    monitor.update_rights(_rights(-1000, "AAA"))
    monitor.begin_oi_query()
    tr = _tracker(tmp_path / "oi.json")
    stamp = datetime(2026, 11, 10, 14, 40)
    monkeypatch.setattr(rb, "_taipei_now", lambda: stamp)
    tr.observe(monitor.rights, stamp)

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
    app._adopt_persisted_pause_failsafe = lambda: None
    app._handle_loss_edges = lambda *args: False
    app._sync_loss_pause_flag = lambda: None
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
    assert tr._prev_read.replace(tzinfo=None) == stamp
    assert tr.snap_net is None


def test_non_trading_morning_is_master(tmp_path):
    """Saturday 08:30 is not a trading morning. The rest of the key is master."""
    tr = _tracker(tmp_path / "sat.json")
    _roll(tr, datetime(2026, 11, 13, 15, 0), _rights(-6998, "AAA"))
    tr.observe(_rights(-12306, "AAA"), datetime(2026, 11, 14, 4, 59))
    view = tr.observe(_rights(-12306, "AAA"), datetime(2026, 11, 14, 8, 30))
    assert view.basis == "master"
    assert view.loss_today == view.account_net == -12306


def test_started_2300_no_file_night_loss_uses_master(tmp_path):
    """A process that starts at 23:00 with no file never snapshots that night."""
    tr = _tracker(tmp_path / "late-start.json")
    view = tr.observe(_rights(-4000, "AAA"), datetime(2026, 11, 13, 23, 0))
    assert view.basis == "master"
    assert view.loss_today == view.account_net == -4000
    later = tr.observe(_rights(-9000, "AAA"), datetime(2026, 11, 14, 2, 0))
    assert later.basis == "master"
    assert later.loss_today == later.account_net == -9000


def test_same_key_file_restart_reuses_snapshot(tmp_path):
    """A 06:00 restart never saw a pre-05:00 read, so it uses the account."""
    path = tmp_path / "reuse.json"
    tr = _tracker(path)
    tr.observe(_rights(-6998, "AAA"), datetime(2026, 11, 13, 14, 59))
    tr.observe(_rights(-6998, "AAA"), datetime(2026, 11, 13, 15, 0, 29))
    tr.observe(_rights(-12306, "AAA"), datetime(2026, 11, 14, 4, 59))
    tr.observe(_rights(-12306, "AAA"), datetime(2026, 11, 14, 5, 0))
    tr.save()
    fresh = _tracker(path)
    fresh.load(datetime(2026, 11, 14, 6, 0))
    view = fresh.observe(_rights(-12306, "AAA"), datetime(2026, 11, 14, 6, 0))
    assert view.basis == "master"
    assert view.loss_today == view.account_net == -12306


def test_winning_day_after_the_reset_is_the_night_loss_and_not_paused(tmp_path):
    """Tue +12,000, night -9,000. Wednesday's reset still reads -9,000."""
    tr = _tracker(tmp_path / "win.json")
    _roll(tr, datetime(2026, 11, 10, 15, 0), _rights(12000, "AAA"))
    night = tr.observe(_rights(3000, "AAA"), datetime(2026, 11, 11, 2, 0))
    assert night.loss_today == -9000
    tr.observe(_rights(3000, "AAA"), datetime(2026, 11, 11, 4, 59))
    reset = tr.observe(_rights(-9000, "BBB"), datetime(2026, 11, 11, 5, 36))
    assert reset.loss_today == -9000
    guard = TradingGuard()
    guard.daily_loss_limit = 20000
    guard.update_pnl(reset.loss_today, day_key=reset.day_key)
    assert guard.paused is False


def test_losing_day_is_the_drop_from_the_snapshot(tmp_path):
    tr = _tracker(tmp_path / "lose.json")
    _roll(tr, datetime(2026, 11, 10, 15, 0), _rights(2000, "AAA"))
    view = tr.observe(_rights(-4000, "AAA"), datetime(2026, 11, 11, 2, 0))
    assert view.basis == "snapshot"
    assert view.loss_today == -6000


def test_replay_friday_night_is_exactly_5308(tmp_path):
    """11/13 15:00:29 snap -6,998. 11/14 05:00:14 account -12,306, L -5,308."""
    tr = _tracker(tmp_path / "replay.json")
    snap = {"realized_pnl": "-6950", "realized_cost": "30", "tax": "18",
            "float_pnl": "0", "start_equity": "71649"}
    night = {"realized_pnl": "-12210", "realized_cost": "60", "tax": "36",
             "float_pnl": "0", "start_equity": "71649"}
    tr.observe(snap, datetime(2026, 11, 13, 14, 59, 59))
    tr.observe(snap, datetime(2026, 11, 13, 15, 0, 29))
    # 05:00 is inside the hold, so the night loss has to be read before it.
    tr.observe(night, datetime(2026, 11, 14, 4, 59))
    view = tr.observe(night, datetime(2026, 11, 14, 5, 0, 14))
    assert view.account_net == -12306
    assert tr.snap_net == -6998
    assert view.loss_today == -5308


def test_morning_reset_reanchors_and_the_next_trade_continues(tmp_path):
    tr = _tracker(tmp_path / "anchor.json")
    _roll(tr, datetime(2026, 11, 10, 15, 0), _rights(12000, "AAA"))
    tr.observe(_rights(3000, "AAA"), datetime(2026, 11, 11, 2, 0))
    tr.observe(_rights(3000, "AAA"), datetime(2026, 11, 11, 4, 59))
    reset = tr.observe(_rights(-9000, "BBB"), datetime(2026, 11, 11, 5, 36))
    assert reset.loss_today == -9000
    assert tr.anchored is False
    opened = tr.observe(_rights(-9000, "BBB"), datetime(2026, 11, 11, 8, 30))
    assert opened.loss_today == -9000
    assert tr.anchored is True
    nxt = tr.observe(_rights(-10000, "BBB"), datetime(2026, 11, 11, 9, 10))
    assert nxt.loss_today == -10000
    assert nxt.basis == "snapshot"


def test_no_reset_by_0830_is_the_account(tmp_path):
    tr = _tracker(tmp_path / "miss.json")
    _roll(tr, datetime(2026, 11, 10, 15, 0), _rights(-6998, "AAA"))
    tr.observe(_rights(-12306, "AAA"), datetime(2026, 11, 11, 4, 59))
    tr.observe(_rights(-12306, "AAA"), datetime(2026, 11, 11, 5, 0))
    view = tr.observe(_rights(-12306, "AAA"), datetime(2026, 11, 11, 8, 30))
    assert view.basis == "master"
    assert view.loss_today == view.account_net == -12306


def test_same_key_restart_reuses_the_snapshot_and_can_still_anchor(tmp_path):
    """A 07:00 restart did not see 04:59, so it does not hold or anchor."""
    path = tmp_path / "restart.json"
    tr = _tracker(path)
    _roll(tr, datetime(2026, 11, 10, 15, 0), _rights(-6998, "AAA"))
    tr.observe(_rights(-12306, "AAA"), datetime(2026, 11, 11, 4, 59))
    tr.save()
    fresh = _tracker(path)
    fresh.load(datetime(2026, 11, 11, 7, 0))
    held = fresh.observe(_rights(-12306, "AAA"), datetime(2026, 11, 11, 7, 0))
    assert held.basis == "master"
    assert held.loss_today == held.account_net == -12306
    opened = fresh.observe(_rights(-5308, "BBB"), datetime(2026, 11, 11, 8, 30))
    assert opened.basis == "master"
    assert fresh.anchored is False
    assert opened.loss_today == opened.account_net == -5308


def test_no_file_and_a_stale_file_are_the_account(tmp_path):
    bare = _tracker(tmp_path / "none.json")
    view = bare.observe(_rights(-8000, "AAA"), datetime(2026, 11, 16, 10, 0))
    assert view.basis == "master"
    assert view.loss_today == view.account_net == -8000
    path = tmp_path / "stale.json"
    tr = _tracker(path)
    _roll(tr, datetime(2026, 11, 13, 15, 0), _rights(-6998, "AAA"))
    tr.save()
    fresh = _tracker(path)
    fresh.load(datetime(2026, 11, 16, 10, 0))
    stale = fresh.observe(_rights(-4000, "BBB"), datetime(2026, 11, 16, 10, 0))
    assert stale.basis == "master"
    assert stale.loss_today == stale.account_net == -4000
    assert trading_day_key(datetime(2026, 11, 16, 10, 0)) != "2026-11-13"


def test_restart_after_0830_without_an_anchor_is_the_account(tmp_path):
    path = tmp_path / "late.json"
    tr = _tracker(path)
    _roll(tr, datetime(2026, 11, 13, 15, 0), _rights(-6998, "AAA"))
    tr.observe(_rights(-12306, "AAA"), datetime(2026, 11, 14, 2, 0))
    tr.save()
    fresh = _tracker(path)
    fresh.load(datetime(2026, 11, 14, 9, 0))
    view = fresh.observe(_rights(-12306, "AAA"), datetime(2026, 11, 14, 9, 0))
    assert view.basis == "master"
    assert view.loss_today == view.account_net == -12306


def test_holiday_roll_is_the_account(tmp_path):
    """2026-01-01 is a Thursday TAIFEX holiday. No 15:00 snapshot."""
    tr = _tracker(tmp_path / "holiday.json")
    view = tr.observe(_rights(5000, "AAA"), datetime(2026, 1, 1, 15, 0))
    assert view.basis == "master"
    assert view.loss_today == view.account_net == 5000


def test_invalid_read_does_not_become_the_snapshot(tmp_path):
    tr = _tracker(tmp_path / "bad.json")
    tr.observe(_rights(-1000, "AAA"), datetime(2026, 11, 13, 14, 59))
    skipped = tr.observe({"valid": False}, datetime(2026, 11, 13, 15, 0))
    assert skipped.basis == "master"
    later = tr.observe(_rights(-1000, "AAA"), datetime(2026, 11, 13, 15, 5))
    assert later.basis == "snapshot"
    assert later.loss_today == 0


def test_not_valid_snapshot_is_always_the_account(tmp_path, monkeypatch):
    """Each not-valid case gates on the account net."""
    import src.live.loss_baseline as lb

    cases = []

    bare = _tracker(tmp_path / "none.json")
    cases.append(bare.observe(_rights(-8000, "AAA"), datetime(2026, 11, 16, 10, 0)))

    path = tmp_path / "stale.json"
    old = _tracker(path)
    _roll(old, datetime(2026, 11, 13, 15, 0), _rights(-6998, "AAA"))
    old.save()
    fresh = _tracker(path)
    fresh.load(datetime(2026, 11, 16, 10, 0))
    cases.append(fresh.observe(_rights(-4000, "BBB"), datetime(2026, 11, 16, 10, 0)))

    holiday = _tracker(tmp_path / "hol.json")
    cases.append(holiday.observe(_rights(5000, "AAA"), datetime(2026, 1, 1, 15, 0)))

    monkeypatch.setattr(lb, "holiday_calendar_health", lambda year=None: (False, "empty"))
    unknown = _tracker(tmp_path / "unk.json")
    cases.append(unknown.observe(_rights(-6998, "AAA"), datetime(2026, 11, 13, 15, 0)))
    monkeypatch.undo()

    missed = _tracker(tmp_path / "missed.json")
    _roll(missed, datetime(2026, 11, 10, 15, 0), _rights(-6998, "AAA"))
    cases.append(missed.observe(_rights(-12306, "AAA"), datetime(2026, 11, 11, 8, 30)))

    late_path = tmp_path / "late.json"
    late = _tracker(late_path)
    _roll(late, datetime(2026, 11, 13, 15, 0), _rights(-6998, "AAA"))
    late.save()
    restarted = _tracker(late_path)
    restarted.load(datetime(2026, 11, 14, 9, 0))
    cases.append(restarted.observe(_rights(-12306, "AAA"), datetime(2026, 11, 14, 9, 0)))

    for view in cases:
        assert view.basis == "master"
        assert view.loss_today == view.account_net


def _zero(f22):
    return {"realized_pnl": "0", "realized_cost": "0", "tax": "0",
            "float_pnl": "0", "start_equity": f22}


def _guard(loss, day_key, limit=10000):
    guard = TradingGuard()
    guard.daily_loss_limit = limit
    guard.update_pnl(loss, day_key=day_key)
    return guard.paused


def test_f22_first_row_waits_for_0830(tmp_path):
    """f22 moves at 05:35 while net is unchanged. Do not anchor on that row."""
    tr = _tracker(tmp_path / "f22first.json")
    _roll(tr, datetime(2026, 6, 9, 15, 0), _rights(9276, "32435"))
    tr.observe(_rights(9276, "32435"), datetime(2026, 6, 10, 4, 59))
    held = tr.observe(_rights(9276, "41711"), datetime(2026, 6, 10, 5, 35))
    assert held.loss_today == 0
    assert tr.anchored is False
    opened = tr.observe(_rights(0, "41711"), datetime(2026, 6, 10, 8, 30))
    assert opened.loss_today == 0
    assert _guard(opened.loss_today, opened.day_key) is False


def test_zero_row_before_the_reload_holds(tmp_path):
    """A zero row at 05:21 must not move last_L. The 08:30 reload anchors."""
    tr = _tracker(tmp_path / "zero.json")
    _roll(tr, datetime(2026, 7, 1, 15, 0), _rights(6320, "65209"))
    tr.observe(_rights(6320, "65209"), datetime(2026, 7, 2, 4, 59))
    held = tr.observe(_zero("65209"), datetime(2026, 7, 2, 5, 21))
    assert held.loss_today == 0
    assert tr.anchored is False
    reload = tr.observe(_rights(-678, "72207"), datetime(2026, 7, 2, 5, 36))
    assert reload.loss_today == 0
    opened = tr.observe(_rights(-678, "72207"), datetime(2026, 7, 2, 8, 30))
    assert opened.loss_today == 0
    assert opened.basis == "snapshot"


def test_0610_shape_is_not_a_false_pause(tmp_path):
    """6/10: 6d21 reads -9,276 against a flat account and pauses at 11:15."""
    tr = _tracker(tmp_path / "0610.json")
    _roll(tr, datetime(2026, 6, 9, 15, 0), _rights(9276, "32435"))
    tr.observe(_rights(9276, "32435"), datetime(2026, 6, 10, 4, 59))
    tr.observe(_rights(9276, "41711"), datetime(2026, 6, 10, 5, 35))
    flat = tr.observe(_zero("41711"), datetime(2026, 6, 10, 5, 36))
    assert flat.loss_today == 0
    assert flat.account_net == 0
    still = tr.observe(_zero("41711"), datetime(2026, 6, 10, 8, 30))
    assert still.loss_today == 0
    later = tr.observe(_rights(-764, "41711"), datetime(2026, 6, 10, 11, 15))
    assert later.loss_today == -764
    assert _guard(later.loss_today, later.day_key) is False


def test_0612_shape_is_not_paused_all_day(tmp_path):
    """6/12: 6d21 reads -13,406 and stays paused while the account is flat."""
    tr = _tracker(tmp_path / "0612.json")
    _roll(tr, datetime(2026, 6, 11, 15, 0), _rights(13406, "48003"))
    tr.observe(_rights(13406, "48003"), datetime(2026, 6, 12, 4, 59))
    tr.observe(_rights(13406, "61409"), datetime(2026, 6, 12, 5, 36, 21))
    flat = tr.observe(_zero("61409"), datetime(2026, 6, 12, 5, 36, 51))
    assert flat.loss_today == 0
    noon = tr.observe(_zero("61409"), datetime(2026, 6, 12, 11, 15))
    assert noon.loss_today == 0
    assert _guard(noon.loss_today, noon.day_key) is False


def test_monday_replay_0910_is_8308(tmp_path):
    """Carried night into the next morning of the same key. 09:10 is -8,308.

    6d21 anchors on the 05:36 row and reads +3,998. A calendar Monday is a
    new trading_day_key, so this is Wed 11/11, the morning that stays on
    the Tuesday key.
    """
    tr = _tracker(tmp_path / "mon.json")
    _roll(tr, datetime(2026, 11, 10, 15, 0), _rights(-6998, "71649"))
    tr.observe(_rights(-12306, "71649"), datetime(2026, 11, 11, 4, 59))
    at_five = tr.observe(_rights(-12306, "71649"), datetime(2026, 11, 11, 5, 0, 14))
    assert at_five.loss_today == -5308
    tr.observe(_zero("71649"), datetime(2026, 11, 11, 5, 22))
    tr.observe(_rights(-5308, "64651"), datetime(2026, 11, 11, 5, 36))
    tr.observe(_rights(-5308, "64651"), datetime(2026, 11, 11, 8, 30))
    opened = tr.observe(_rights(-8308, "64651"), datetime(2026, 11, 11, 9, 10))
    assert opened.loss_today == -8308
    assert opened.basis == "snapshot"


def test_roll_at_1600_without_a_1450_read_is_the_account(tmp_path):
    """A 16:00 read is not the roll. The previous read is 14:40, before 14:50."""
    tr = _tracker(tmp_path / "late-roll.json")
    tr.observe(_rights(-6998, "AAA"), datetime(2026, 11, 10, 14, 40))
    view = tr.observe(_rights(-12000, "AAA"), datetime(2026, 11, 10, 16, 0))
    assert view.basis == "master"
    assert view.loss_today == view.account_net == -12000
    assert tr.snap_net is None


def test_reed_1459_then_1600_is_the_account(tmp_path):
    """14:59 then 16:00 missed the 15:10 bound. The key uses the account net."""
    tr = _tracker(tmp_path / "reed-1600.json")
    tr.observe(_rights(-6998, "AAA"), datetime(2026, 11, 10, 14, 59))
    view = tr.observe(_rights(-12000, "AAA"), datetime(2026, 11, 10, 16, 0))
    assert view.basis == "master"
    assert view.loss_today == view.account_net == -12000
    assert tr.snap_net is None


def test_reed_1440_then_1505_is_the_account(tmp_path):
    """14:40 is before 14:50, so 15:05 does not confirm a roll."""
    tr = _tracker(tmp_path / "reed-1440.json")
    tr.observe(_rights(-6998, "AAA"), datetime(2026, 11, 10, 14, 40))
    view = tr.observe(_rights(-12000, "AAA"), datetime(2026, 11, 10, 15, 5))
    assert view.basis == "master"
    assert view.loss_today == view.account_net == -12000
    assert tr.snap_net is None


def test_invalid_read_returns_the_account_not_last_l(tmp_path):
    """observe rejects valid=False and must return the account net, not last_L."""
    tr = _tracker(tmp_path / "invalid-net.json")
    _roll(tr, datetime(2026, 11, 10, 15, 0), _rights(-6998, "AAA"))
    night = tr.observe(_rights(-12306, "AAA"), datetime(2026, 11, 11, 2, 0))
    assert night.loss_today == -5308
    assert night.account_net == -12306
    skipped = tr.observe({"valid": False}, datetime(2026, 11, 11, 2, 30))
    assert skipped.basis == "master"
    assert skipped.loss_today == skipped.account_net == -12306


def test_reed_restart_after_anchor_keeps_the_snap_and_pauses(tmp_path):
    """Reed's probe. Limit 20,000. 04:59 is inside 04:50–05:00.

    08:30 anchors with snap_net 0. 09:10 is −8,000. A 13:00 restart on
    account −27,000 must stay paused and leave snap_net alone. On
    91cf709 the restart gates at −8,000 and does not pause.
    """
    path = tmp_path / "anch.json"
    tr = _tracker(path)
    _roll(tr, datetime(2026, 11, 10, 15, 0), _rights(0, "AAA"))
    tr.observe(_rights(-8000, "AAA"), datetime(2026, 11, 11, 4, 59))
    anchored = tr.observe(_rights(-8000, "BBB"), datetime(2026, 11, 11, 8, 30))
    assert tr.anchored is True
    assert tr.snap_net == 0
    assert anchored.loss_today == -8000
    day = tr.observe(_rights(-8000, "BBB"), datetime(2026, 11, 11, 9, 10))
    assert day.loss_today == -8000
    tr.save()
    fresh = _tracker(path)
    fresh.load(datetime(2026, 11, 11, 13, 0))
    later = fresh.observe(_rights(-27000, "BBB"), datetime(2026, 11, 11, 13, 0))
    assert fresh.snap_net == 0
    assert later.loss_today == -27000
    assert _guard(later.loss_today, later.day_key, limit=20000) is True


def test_stall_at_0300_then_0830_is_master(tmp_path):
    """A 03:00 read is not the close. 06:00 does not hold and 08:30 does not anchor."""
    tr = _tracker(tmp_path / "stall.json")
    _roll(tr, datetime(2026, 11, 10, 15, 0), _rights(-6998, "AAA"))
    tr.observe(_rights(-20000, "AAA"), datetime(2026, 11, 11, 3, 0))
    held = tr.observe(_rights(-20000, "AAA"), datetime(2026, 11, 11, 6, 0))
    assert held.basis == "master"
    assert held.loss_today == held.account_net == -20000
    opened = tr.observe(_rights(-16000, "BBB"), datetime(2026, 11, 11, 8, 30))
    assert opened.basis == "master"
    assert tr.anchored is False
    assert opened.loss_today == opened.account_net == -16000


def test_reed_restart_in_the_window_pauses_on_the_account(tmp_path):
    """Reed's probe, limit 20,000. The file was saved at the 15:00 roll.

    This process comes back at 06:00 and never saw a pre-05:00 read, so
    the night loss is the account net and it pauses. The same reads with
    the process left up still anchor and pause at 09:10.
    """
    path = tmp_path / "reed.json"
    first = _tracker(path)
    _roll(first, datetime(2026, 11, 10, 15, 0), _rights(-6998, "AAA"))
    first.save()
    fresh = _tracker(path)
    fresh.load(datetime(2026, 11, 11, 6, 0))
    at_six = fresh.observe(_rights(-26998, "AAA"), datetime(2026, 11, 11, 6, 0))
    assert at_six.basis == "master"
    assert at_six.loss_today == at_six.account_net == -26998
    assert _guard(at_six.loss_today, at_six.day_key, limit=20000) is True
    at_open = fresh.observe(_rights(-20000, "BBB"), datetime(2026, 11, 11, 8, 30))
    assert at_open.basis == "master"
    assert fresh.anchored is False
    trade = fresh.observe(_rights(-35000, "BBB"), datetime(2026, 11, 11, 9, 10))
    assert trade.loss_today == -35000
    assert _guard(trade.loss_today, trade.day_key, limit=20000) is True

    stayed = _tracker(tmp_path / "up.json")
    _roll(stayed, datetime(2026, 11, 10, 15, 0), _rights(-6998, "AAA"))
    stayed.observe(_rights(-26998, "AAA"), datetime(2026, 11, 11, 4, 59))
    stayed.observe(_rights(-26998, "AAA"), datetime(2026, 11, 11, 6, 0))
    stayed.observe(_rights(-20000, "BBB"), datetime(2026, 11, 11, 8, 30))
    later = stayed.observe(_rights(-35000, "BBB"), datetime(2026, 11, 11, 9, 10))
    assert later.loss_today == -35000
    assert later.basis == "snapshot"
    assert _guard(later.loss_today, later.day_key, limit=20000) is True


def test_restart_inside_the_window_uses_file_last_l(tmp_path):
    """The file's last_L is not a hold when this process missed 04:59."""
    path = tmp_path / "hold.json"
    tr = _tracker(path)
    _roll(tr, datetime(2026, 11, 10, 15, 0), _rights(-6998, "71649"))
    tr.observe(_rights(-12306, "71649"), datetime(2026, 11, 11, 4, 59))
    tr.save()
    fresh = _tracker(path)
    fresh.load(datetime(2026, 11, 11, 6, 0))
    held = fresh.observe(_zero("71649"), datetime(2026, 11, 11, 6, 0))
    assert held.basis == "master"
    assert held.loss_today == held.account_net == 0
    assert fresh.anchored is False
