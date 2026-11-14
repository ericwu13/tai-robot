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
    """A restart that loads this session's file keeps the snapshot it already took."""
    path = tmp_path / "reuse.json"
    tr = _tracker(path)
    tr.observe(_rights(-6998, "AAA"), datetime(2026, 11, 13, 14, 59))
    tr.observe(_rights(-6998, "AAA"), datetime(2026, 11, 13, 15, 0, 29))
    tr.observe(_rights(-12306, "AAA"), datetime(2026, 11, 14, 5, 0))
    tr.save()
    fresh = _tracker(path)
    fresh.load(datetime(2026, 11, 14, 6, 0))
    view = fresh.observe(_rights(-12306, "AAA"), datetime(2026, 11, 14, 6, 0))
    assert view.basis == "snapshot"
    assert view.loss_today == -5308


def test_winning_day_after_the_reset_is_the_night_loss_and_not_paused(tmp_path):
    """Tue +12,000, night -9,000. Wednesday's reset still reads -9,000."""
    tr = _tracker(tmp_path / "win.json")
    _roll(tr, datetime(2026, 11, 10, 15, 0), _rights(12000, "AAA"))
    night = tr.observe(_rights(3000, "AAA"), datetime(2026, 11, 11, 2, 0))
    assert night.loss_today == -9000
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
    view = tr.observe(night, datetime(2026, 11, 14, 5, 0, 14))
    assert view.account_net == -12306
    assert tr.snap_net == -6998
    assert view.loss_today == -5308


def test_morning_reset_reanchors_and_the_next_trade_continues(tmp_path):
    tr = _tracker(tmp_path / "anchor.json")
    _roll(tr, datetime(2026, 11, 10, 15, 0), _rights(12000, "AAA"))
    tr.observe(_rights(3000, "AAA"), datetime(2026, 11, 11, 2, 0))
    reset = tr.observe(_rights(-9000, "BBB"), datetime(2026, 11, 11, 5, 36))
    assert reset.loss_today == -9000
    assert tr.anchored is True
    nxt = tr.observe(_rights(-10000, "BBB"), datetime(2026, 11, 11, 9, 10))
    assert nxt.loss_today == -10000
    assert nxt.basis == "snapshot"


def test_no_reset_by_0830_is_the_account(tmp_path):
    tr = _tracker(tmp_path / "miss.json")
    _roll(tr, datetime(2026, 11, 13, 15, 0), _rights(-6998, "AAA"))
    tr.observe(_rights(-12306, "AAA"), datetime(2026, 11, 14, 5, 0))
    view = tr.observe(_rights(-12306, "AAA"), datetime(2026, 11, 14, 8, 30))
    assert view.basis == "master"
    assert view.loss_today == view.account_net == -12306


def test_same_key_restart_reuses_the_snapshot_and_can_still_anchor(tmp_path):
    path = tmp_path / "restart.json"
    tr = _tracker(path)
    _roll(tr, datetime(2026, 11, 13, 15, 0), _rights(-6998, "AAA"))
    tr.observe(_rights(-12306, "AAA"), datetime(2026, 11, 14, 5, 0))
    tr.save()
    fresh = _tracker(path)
    fresh.load(datetime(2026, 11, 14, 7, 0))
    held = fresh.observe(_rights(-12306, "AAA"), datetime(2026, 11, 14, 7, 0))
    assert held.loss_today == -5308
    anchored = fresh.observe(_rights(-5308, "BBB"), datetime(2026, 11, 14, 7, 10))
    assert anchored.loss_today == -5308


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
    later = tr.observe(_rights(-1000, "AAA"), datetime(2026, 11, 13, 16, 0))
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
    _roll(missed, datetime(2026, 11, 13, 15, 0), _rights(-6998, "AAA"))
    cases.append(missed.observe(_rights(-12306, "AAA"), datetime(2026, 11, 14, 8, 30)))

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
