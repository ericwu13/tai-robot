"""Round-6 killers for #293 @ ff3a3026."""
from datetime import datetime

import pytest

import src.market_data.holidays as hol
from tests.test_loss_limit_daily_baseline import (
    _app_at, _drive, _pnl, _raw, _tracker, _thu_snapshot_fri_skipped)


def _break_calendar(monkeypatch):
    monkeypatch.setattr(hol, "_tw_public_holidays", lambda year: frozenset())
    monkeypatch.setattr(hol, "holiday_calendar_health", lambda year=None: (False, "empty"))


def _tue_candidate_then_stop(path, r=24900):
    tr = _tracker(path)
    win = _raw(r + 60, 30, 30, f22="A")
    for hh, mm in ((13, 50), (14, 0), (14, 30)):
        tr.observe(win, datetime(2026, 11, 10, hh, mm))
    return path


@pytest.mark.parametrize("load_at", [datetime(2026, 11, 11, 8, 0), datetime(2026, 11, 11, 9, 10)])
def test_k11_candidate_load_first_read_post_batch_is_not_a_false_pause(tmp_path, load_at):
    """Reed/Claude P2: Tue window R=+24,900, stop before 15:00, redeploy Wed; flat night, first read R=0 new f22."""
    path = _tue_candidate_then_stop(tmp_path / "k11.json")
    tr = _tracker(path)
    tr.load(load_at)
    for m in (0, 2, 20):
        when = load_at.replace(minute=load_at.minute + m) if load_at.minute + m < 60 else load_at
        v = tr.observe(_raw(0, 0, 0, f22="B"), when)
        assert v.loss_today >= -100, (when, v.loss_today)


def test_k12_candidate_load_losing_night_is_the_account(tmp_path):
    path = _tue_candidate_then_stop(tmp_path / "k12.json")
    tr = _tracker(path)
    tr.load(datetime(2026, 11, 11, 8, 0))
    for when in (datetime(2026, 11, 11, 8, 0), datetime(2026, 11, 11, 8, 31), datetime(2026, 11, 11, 8, 46)):
        v = tr.observe(_raw(-2990, 10, 0, f22="B"), when)
        assert v.loss_today == -3000, (when, v.loss_today)


def _tue_snapshot_night(path, b_r):
    tr = _tracker(path)
    base = _pnl(b_r, 10, f22="A")
    tr.observe(base, datetime(2026, 11, 10, 14, 59))
    tr.consume_failsafe_notice()
    tr.observe(base, datetime(2026, 11, 10, 15, 0, 5))
    tr.observe(_pnl(b_r - 9000, 10, f22="A"), datetime(2026, 11, 11, 2, 0))
    return tr


@pytest.mark.parametrize("b_r", [12000, -10000])
def test_k13_calendar_breaks_mid_key_unknown_today_is_the_account(tmp_path, monkeypatch, b_r):
    """Sage / Q2b: calendar turns unknown after Tue's snapshot; adopt does not run; Wed must be master's account."""
    tr = _tue_snapshot_night(tmp_path / "k13.json", b_r)
    _break_calendar(monkeypatch)
    v = tr.observe(_raw(0, 0, 0, f22="A"), datetime(2026, 11, 11, 5, 22))
    v = tr.observe(_raw(-8990, 10, 0, f22="B"), datetime(2026, 11, 11, 5, 36))
    assert v.loss_today == -9000, v.loss_today
    v = tr.observe(_raw(-9990, 10, 0, f22="B"), datetime(2026, 11, 11, 9, 0))
    assert v.loss_today == -10000, v.loss_today


@pytest.mark.parametrize("b_r", [12000, -10000])
def test_k14_same_key_restart_after_batch_unknown_today_is_the_account(tmp_path, monkeypatch, b_r):
    """Q2d: same-key file with B_r, restart Wed 09:00 on an unknown day (#58 EXE)."""
    path = tmp_path / "k14.json"
    _tue_snapshot_night(path, b_r).save()
    _break_calendar(monkeypatch)
    tr = _tracker(path)
    tr.load(datetime(2026, 11, 11, 9, 0))
    v = tr.observe(_raw(-8990, 10, 0, f22="B"), datetime(2026, 11, 11, 9, 0))
    assert v.loss_today == -9000, v.loss_today


def test_k3b_unknown_calendar_adopt_zeroes_b_r_and_b_c(tmp_path, monkeypatch):
    """Keeps U16 (adopt keeps B_r) killed once an unknown today gates on the account."""
    tr = _thu_snapshot_fri_skipped(tmp_path / "k3b.json", -5000)
    _break_calendar(monkeypatch)
    v = tr.observe(_pnl(-7000, 10, f22="CCC"), datetime(2026, 11, 16, 5, 40))
    assert v.loss_today == -7000
    assert (tr.b_r, tr.b_c) == (0, 0), (tr.b_r, tr.b_c)


@pytest.mark.parametrize("load_day,first", [(datetime(2026, 11, 11, 8, 0), "same"), (datetime(2026, 11, 16, 8, 0), "gap")])
def test_k15_file_without_last_realized_is_not_a_false_pause(tmp_path, load_day, first):
    """A file written before last_realized existed: first read post-batch, non-interim, flat."""
    import json
    path = tmp_path / "k15.json"
    tr = _tracker(path)
    d0 = 10 if first == "same" else 13
    snap = _raw(24960, 30, 30, f22="A")
    tr.observe(snap, datetime(2026, 11, d0, 14, 59))
    tr.consume_failsafe_notice()
    tr.observe(snap, datetime(2026, 11, d0, 15, 0, 5))
    tr.observe(snap, datetime(2026, 11, d0 + 1, 2, 0))
    tr.save()
    data = json.loads(path.read_text()); data.pop("last_realized", None); path.write_text(json.dumps(data))
    tr = _tracker(path)
    tr.load(load_day)
    for h in (8, 9):
        v = tr.observe(_raw(10, 10, 0, f22="B"), load_day.replace(hour=h))
        assert v.loss_today >= -100, (h, v.loss_today)


def test_k11b_candidate_load_non_interim_first_read(tmp_path):
    path = _tue_candidate_then_stop(tmp_path / "k11b.json")
    tr = _tracker(path)
    tr.load(datetime(2026, 11, 11, 8, 0))
    for h in (8, 9):
        v = tr.observe(_raw(10, 10, 0, f22="B"), datetime(2026, 11, 11, h, 0))
        assert v.loss_today >= -100, (h, v.loss_today)


def test_k13c_mid_key_unknown_with_b_c_is_the_account_not_account_minus_b_c(tmp_path, monkeypatch):
    """B_c +8,000 at Tue 15:00 (open profit), calendar breaks; Wed in session the account is master's figure."""
    tr = _tracker(tmp_path / "k13c.json")
    s = _raw(10, 10, 0, f1=8000, f22="A")
    tr.observe(s, datetime(2026, 11, 10, 14, 59))
    tr.consume_failsafe_notice()
    tr.observe(s, datetime(2026, 11, 10, 15, 0, 5))
    _break_calendar(monkeypatch)
    v = tr.observe(_raw(10, 10, 0, f1=-4000, f22="A"), datetime(2026, 11, 11, 9, 0))
    assert v.loss_today == -4000, v.loss_today


def test_k16_reed_w8hx_fee_blip_with_negative_b_r_is_not_the_account(tmp_path):
    """W8HX: B_r -25,000 at 15:00; a 16:01 fee drop with f22 unchanged must not gate on the whole account."""
    tr = _tracker(tmp_path / "k16.json")
    s = _raw(-24900, 100, 0, f22="A")
    tr.observe(s, datetime(2026, 11, 10, 14, 59))
    tr.consume_failsafe_notice()
    tr.observe(s, datetime(2026, 11, 10, 15, 0, 5))
    for when, row in ((datetime(2026, 11, 10, 16, 1), _raw(-24950, 50, 0, f22="A")),
                      (datetime(2026, 11, 10, 18, 0), s)):
        v = tr.observe(row, when)
        assert v.loss_today >= -100, (when, v.loss_today)


def test_k17_reed_w8hb_fill_in_f22_first_gap_is_not_looser(tmp_path):
    """W8Hb: 08:46 f22-first opens the day budget, 08:47 a -100 fill, 08:52 reload; the fill counts; 09:10 is -3,100 (fill + -3,000)."""
    tr = _tracker(tmp_path / "k17.json")
    snap = _raw(-6950, 30, 18, f22="71649")
    pre = _raw(-12210, 60, 36, f22="71649")
    tr.observe(snap, datetime(2026, 11, 13, 14, 59))
    tr.consume_failsafe_notice()
    tr.observe(snap, datetime(2026, 11, 13, 15, 0, 5))
    tr.observe(pre, datetime(2026, 11, 14, 5, 0))
    tr.observe(pre, datetime(2026, 11, 16, 8, 40))
    tr.observe(_raw(-12210, 60, 36, f22="64651"), datetime(2026, 11, 16, 8, 46))
    v = tr.observe(_raw(-12305, 65, 36, f22="64651"), datetime(2026, 11, 16, 8, 47))
    assert v.loss_today <= -100, v.loss_today
    v = tr.observe(_raw(-5355, 35, 18, f22="64651"), datetime(2026, 11, 16, 8, 52))
    assert v.loss_today == -100, v.loss_today
    v = tr.observe(_raw(-8355, 35, 18, f22="64651"), datetime(2026, 11, 16, 9, 10))
    assert v.loss_today == -3100, v.loss_today


def test_k18_w8hb_gap_fill_survives_a_restart_before_the_reload(tmp_path):
    path = tmp_path / "k18.json"
    tr = _tracker(path)
    snap = _raw(-6950, 30, 18, f22="71649")
    pre = _raw(-12210, 60, 36, f22="71649")
    tr.observe(snap, datetime(2026, 11, 13, 14, 59))
    tr.consume_failsafe_notice()
    tr.observe(snap, datetime(2026, 11, 13, 15, 0, 5))
    tr.observe(pre, datetime(2026, 11, 14, 5, 0))
    tr.observe(pre, datetime(2026, 11, 16, 8, 40))
    tr.observe(_raw(-12210, 60, 36, f22="64651"), datetime(2026, 11, 16, 8, 46))
    tr.observe(_raw(-12305, 65, 36, f22="64651"), datetime(2026, 11, 16, 8, 47))
    tr = _tracker(path)
    tr.load(datetime(2026, 11, 16, 8, 50))
    tr.observe(_raw(-5355, 35, 18, f22="64651"), datetime(2026, 11, 16, 8, 52))
    v = tr.observe(_raw(-8355, 35, 18, f22="64651"), datetime(2026, 11, 16, 9, 10))
    assert v.loss_today == -3100, v.loss_today


def _mon_snapshot_tue_day(path, tue_r):
    """Mon snapshot, Mon night fees 120 (pre-batch peak), Tue batch, Tue day to tue_r by 13:50-14:30; stop."""
    tr = _tracker(path)
    mon = _raw(60, 60, 0, f22="Z")
    tr.observe(mon, datetime(2026, 11, 9, 14, 59))
    tr.consume_failsafe_notice()
    tr.observe(mon, datetime(2026, 11, 9, 15, 0, 5))
    tr.observe(_raw(120, 120, 0, f22="Z"), datetime(2026, 11, 10, 2, 0))
    tr.observe(_raw(0, 0, 0, f22="Z"), datetime(2026, 11, 10, 5, 22))
    tr.observe(_raw(20, 20, 0, f22="A"), datetime(2026, 11, 10, 5, 36))
    tr.observe(_raw(20, 20, 0, f22="A"), datetime(2026, 11, 10, 8, 31))
    for hh, mm in ((13, 50), (14, 30)):
        tr.observe(_raw(tue_r + 60, 60, 0, f22="A"), datetime(2026, 11, 10, hh, mm))
    return path


def test_k19_reed_candidate_load_does_not_restore_old_key_last_l_or_fee_peak(tmp_path):
    """Reed/Claude @ff3a: Tue -25,000, stop 14:30, restart Tue 16:00 (and Wed). Old-key last_l / max_fees must not latch."""
    path = _mon_snapshot_tue_day(tmp_path / "k19.json", -25000)
    tr = _tracker(path)
    tr.load(datetime(2026, 11, 10, 16, 0))
    v = tr.observe(_raw(-24940, 60, 0, f22="A"), datetime(2026, 11, 10, 16, 0))
    assert v.loss_today == 0, v.loss_today
    v = tr.observe(_raw(-27940, 60, 0, f22="A"), datetime(2026, 11, 10, 22, 0))
    assert v.loss_today == -3000, v.loss_today
    tr.observe(_raw(0, 0, 0, f22="A"), datetime(2026, 11, 11, 5, 22))
    for when, r, want in ((datetime(2026, 11, 11, 5, 36), -3000, -3000), (datetime(2026, 11, 11, 8, 31), -3000, -3000),
                          (datetime(2026, 11, 11, 9, 0), -4000, -4000)):
        v = tr.observe(_raw(r + 10, 10, 0, f22="B"), when)
        assert v.loss_today == want, (when, v.loss_today)


def test_k19b_candidate_load_wed_0800_flat_is_zero(tmp_path):
    path = _mon_snapshot_tue_day(tmp_path / "k19b.json", -25000)
    tr = _tracker(path)
    tr.load(datetime(2026, 11, 11, 8, 0))
    for when in (datetime(2026, 11, 11, 8, 0), datetime(2026, 11, 11, 8, 31), datetime(2026, 11, 11, 9, 0)):
        v = tr.observe(_raw(10, 10, 0, f22="B"), when)
        assert v.loss_today == 0, (when, v.loss_today)


def test_k16b_w8hx_fill_during_the_dip_does_not_latch_the_account(tmp_path):
    """Kills 'apply account - B_c only when R moved': a -500 fill in the dip must read -510, not -25,510."""
    tr = _tracker(tmp_path / "k16b.json")
    s = _raw(-24900, 100, 0, f22="A")
    tr.observe(s, datetime(2026, 11, 10, 14, 59))
    tr.consume_failsafe_notice()
    tr.observe(s, datetime(2026, 11, 10, 15, 0, 5))
    tr.observe(_raw(-24950, 50, 0, f22="A"), datetime(2026, 11, 10, 16, 1))
    v = tr.observe(_raw(-25450, 60, 0, f22="A"), datetime(2026, 11, 10, 16, 30))
    assert v.loss_today == -510, v.loss_today
    v = tr.observe(_raw(-25400, 110, 0, f22="A"), datetime(2026, 11, 10, 18, 30))
    assert v.loss_today == -510, v.loss_today


@pytest.mark.parametrize("b_r", [12000, -12000])
def test_k20_zero_row_never_reads_a_profit(tmp_path, b_r):
    """Candidate load at 17:00 during a fee dip: the 05:22 zero row must not read a profit (master reads 0)."""
    path = tmp_path / "k20.json"
    tr = _tracker(path)
    for hh, mm in ((13, 50), (14, 30)):
        tr.observe(_raw(b_r + 60, 60, 0, f22="A"), datetime(2026, 11, 10, hh, mm))
    tr = _tracker(path)
    tr.load(datetime(2026, 11, 10, 17, 0))
    tr.observe(_raw(b_r + 30, 30, 0, f22="A"), datetime(2026, 11, 10, 17, 0))
    tr.observe(_raw(b_r - 3000 + 80, 80, 0, f22="A"), datetime(2026, 11, 10, 22, 0))
    v = tr.observe(_raw(0, 0, 0, f22="A"), datetime(2026, 11, 11, 5, 22))
    assert v.loss_today <= 0, v.loss_today
    v = tr.observe(_raw(-3000 + 20, 20, 0, f22="B"), datetime(2026, 11, 11, 5, 36))
    assert v.loss_today == -3000, v.loss_today


def test_k19c_candidate_load_does_not_open_a_spurious_reset(tmp_path):
    """The old key's fee peak (Mon night 120) must not trip fee < max_fees_seen on the first read after load."""
    path = _mon_snapshot_tue_day(tmp_path / "k19c.json", -25000)
    tr = _tracker(path)
    tr.load(datetime(2026, 11, 10, 16, 0))
    tr.observe(_raw(-24940, 60, 0, f22="A"), datetime(2026, 11, 10, 16, 0))
    assert tr.capital_reset is None, tr.capital_reset


def test_k21_fresh_deploy_in_a_fee_dip_settles_at_0830(tmp_path):
    """Fresh deploy Tue 17:00 during a fee-only dip: baseline fees too low, expect_fees too high. Wed must settle at 08:30."""
    tr = _tracker(tmp_path / "k21.json")
    tr.observe(_raw(-12000 + 30, 30, 0, f22="A"), datetime(2026, 11, 10, 17, 0))
    tr.consume_failsafe_notice()
    tr.observe(_raw(-12000 + 60, 60, 0, f22="A"), datetime(2026, 11, 10, 18, 30))
    tr.observe(_raw(-15000 + 80, 80, 0, f22="A"), datetime(2026, 11, 10, 22, 0))
    tr.observe(_raw(0, 0, 0, f22="A"), datetime(2026, 11, 11, 5, 22))
    tr.observe(_raw(-3000 + 20, 20, 0, f22="B"), datetime(2026, 11, 11, 5, 36))
    v = tr.observe(_raw(-3000 + 20, 20, 0, f22="B"), datetime(2026, 11, 11, 8, 30, 5))
    assert v.loss_today == -3000, v.loss_today
    v = tr.observe(_raw(-5000 + 30, 30, 0, f22="B"), datetime(2026, 11, 11, 9, 10))
    assert v.loss_today == -5000, v.loss_today


def test_n10_zero_row_inside_the_day_budget_is_not_a_profit(tmp_path):
    """N10: a day budget already open, then Capital's all-zero row, must not read a profit."""
    tr = _tracker(tmp_path / "n10.json")
    snap = _raw(-6950, 30, 18, f22="71649")
    pre = _raw(-12210, 60, 36, f22="71649")
    rel = _raw(-5260, 30, 18, f22="64651")
    tr.observe(snap, datetime(2026, 11, 13, 14, 59))
    tr.consume_failsafe_notice()
    tr.observe(snap, datetime(2026, 11, 13, 15, 0, 5))
    tr.observe(pre, datetime(2026, 11, 14, 5, 0))
    tr.observe(pre, datetime(2026, 11, 16, 8, 40))
    tr.observe(rel, datetime(2026, 11, 16, 8, 46))
    tr.observe(rel, datetime(2026, 11, 16, 8, 52))
    # Day baseline is the rebased night (negative). A later all-zero row
    # would otherwise print that baseline back as a profit.
    v = tr.observe(_raw(0, 0, 0, f22="64651"), datetime(2026, 11, 16, 9, 0))
    assert v.loss_today <= 0, v.loss_today


def test_xj8b_candidate_load_on_the_zero_row_is_not_a_loss_of_b_r(tmp_path, monkeypatch):
    """Xj8b / XrdE: stop before 15:00, first read is the 05:22 zero row. Not a loss of B_r."""
    path = tmp_path / "lb.json"
    app, clock, tr = _app_at(monkeypatch, datetime(2026, 11, 10, 13, 50), limit=20000, path=path)
    for w in (datetime(2026, 11, 10, 13, 50), datetime(2026, 11, 10, 14, 30)):
        _drive(app, clock, w, _raw(24960, 60, 0, f22="A"))
    app, clock, tr = _app_at(monkeypatch, datetime(2026, 11, 11, 5, 25), limit=20000, path=path)
    tr.load(datetime(2026, 11, 11, 5, 25))
    _drive(app, clock, datetime(2026, 11, 11, 5, 25), _raw(0, 0, 0, f22="A"))
    assert app._trading_guard._last_net_pnl >= -100
    assert not app._trading_guard.paused
    _drive(app, clock, datetime(2026, 11, 11, 5, 36), _raw(10, 10, 0, f22="B"))
    _drive(app, clock, datetime(2026, 11, 11, 9, 0), _raw(10, 10, 0, f22="B"))
    assert not app._trading_guard.paused


def _snap_night(tr, b_r_row, night_row, fri=False):
    d = (13, 14) if fri else (10, 11)
    tr.observe(b_r_row, datetime(2026, 11, d[0], 14, 59))
    tr.consume_failsafe_notice()
    tr.observe(b_r_row, datetime(2026, 11, d[0], 15, 0, 5))
    tr.observe(night_row, datetime(2026, 11, d[1], 5, 0))


def test_k22_reed_e9e1_reload_without_f22_is_not_looser_than_master(tmp_path):
    """Reed: Tue R -6,998; Wed 05:22 reload R -5,308, f22 unchanged. 09:30 is the account."""
    tr = _tracker(tmp_path / "k22.json")
    _snap_night(tr, _raw(-6950, 30, 18, f22="71649"), _raw(-12210, 60, 36, f22="71649"))
    v = tr.observe(_raw(-5260, 30, 18, f22="71649"), datetime(2026, 11, 11, 5, 22))
    assert v.loss_today == -5308, v.loss_today
    v = tr.observe(_raw(-20260, 50, 18, f22="71649"), datetime(2026, 11, 11, 9, 30))
    assert v.loss_today == v.account_net == -20328, (v.loss_today, v.account_net)


def test_k22b_restart_after_an_unseen_f22_unchanged_reload_is_not_looser(tmp_path):
    """Down from 16:30 to Wed 07:00. The unseen reload must not read looser than the account."""
    path = tmp_path / "k22b.json"
    tr = _tracker(path)
    s = _raw(-11940, 60, 0, f22="A")
    tr.observe(s, datetime(2026, 11, 10, 14, 59))
    tr.consume_failsafe_notice()
    tr.observe(s, datetime(2026, 11, 10, 15, 0, 5))
    tr.observe(s, datetime(2026, 11, 10, 16, 30))
    tr = _tracker(path)
    tr.load(datetime(2026, 11, 11, 7, 0))
    for when, row in ((datetime(2026, 11, 11, 7, 0), _raw(-2980, 20, 0, f22="A")),
                      (datetime(2026, 11, 11, 10, 0), _raw(-4970, 30, 0, f22="A"))):
        v = tr.observe(row, when)
        assert v.loss_today <= v.account_net, (when, v.loss_today, v.account_net)


def test_k22c_positive_b_r_reload_without_f22_is_not_a_false_loss(tmp_path):
    """B_r +12,000 and an f22-unchanged reload is the night figure, not R minus 12,000."""
    tr = _tracker(tmp_path / "k22c.json")
    _snap_night(tr, _raw(12060, 60, 0, f22="A"), _raw(9080, 80, 0, f22="A"))
    v = tr.observe(_raw(-2980, 20, 0, f22="A"), datetime(2026, 11, 11, 5, 36))
    assert v.loss_today == -3000, v.loss_today
    v = tr.observe(_raw(-4970, 30, 0, f22="A"), datetime(2026, 11, 11, 9, 10))
    assert v.loss_today == -5000, v.loss_today


def test_k22d_evening_fee_dip_then_f22_unchanged_reload(tmp_path):
    """A 16:01 dip opens the reset. The later reload still matches the last night R."""
    tr = _tracker(tmp_path / "k22d.json")
    s = _raw(-11940, 60, 0, f22="A")
    tr.observe(s, datetime(2026, 11, 10, 14, 59))
    tr.consume_failsafe_notice()
    tr.observe(s, datetime(2026, 11, 10, 15, 0, 5))
    tr.observe(_raw(-11970, 30, 0, f22="A"), datetime(2026, 11, 10, 16, 1))
    tr.observe(s, datetime(2026, 11, 10, 18, 30))
    tr.observe(_raw(-14920, 80, 0, f22="A"), datetime(2026, 11, 10, 22, 0))
    v = tr.observe(_raw(-2980, 20, 0, f22="A"), datetime(2026, 11, 11, 5, 36))
    assert v.loss_today == -3000, v.loss_today
    assert tr.capital_reset.get("r_rebased") is True
    v = tr.observe(_raw(-4970, 30, 0, f22="A"), datetime(2026, 11, 11, 9, 10))
    assert v.loss_today == v.account_net == -5000, (v.loss_today, v.account_net)


def test_k22e_fill_landing_on_pre_r_minus_b_r_is_not_a_reload(tmp_path):
    """A fill that lands on gap_r - B_r is not a reload when the fees do not match."""
    tr = _tracker(tmp_path / "k22e.json")
    s = _raw(-440, 60, 0, f22="A")
    tr.observe(s, datetime(2026, 11, 10, 14, 59))
    tr.consume_failsafe_notice()
    tr.observe(s, datetime(2026, 11, 10, 15, 0, 5))
    tr.observe(_raw(-470, 30, 0, f22="A"), datetime(2026, 11, 10, 16, 1))
    tr.observe(_raw(-500, 0, 0, f22="A"), datetime(2026, 11, 10, 16, 30))
    tr.observe(_raw(90, 90, 0, f22="A"), datetime(2026, 11, 10, 17, 0))
    assert not tr.capital_reset.get("r_rebased")
    v = tr.observe(_raw(-900, 100, 0, f22="A"), datetime(2026, 11, 10, 18, 0))
    assert v.loss_today == -500, v.loss_today


def test_k23_reed_xrdl_day_open_on_f22_first_does_not_repause_on_the_night(tmp_path, monkeypatch):
    """Limit 10,000. Monday's f22-first open reads 0, and 09:10 is -3,000 and not paused."""
    app, clock, _tr = _app_at(
        monkeypatch, datetime(2026, 11, 13, 14, 59), limit=10000, path=tmp_path / "k23.json")
    snap = _raw(-6950, 30, 18, f22="71649")
    night = _raw(-21902, 60, 36, f22="71649")
    for when, row in ((datetime(2026, 11, 13, 14, 59), snap), (datetime(2026, 11, 13, 15, 0, 5), snap),
                      (datetime(2026, 11, 14, 5, 0), night), (datetime(2026, 11, 16, 8, 40), night)):
        _drive(app, clock, when, row)
    assert app._trading_guard.paused
    _drive(app, clock, datetime(2026, 11, 16, 8, 46), _raw(-21902, 60, 36, f22="64651"))
    assert app._trading_guard._last_net_pnl == 0, app._trading_guard._last_net_pnl
    _drive(app, clock, datetime(2026, 11, 16, 8, 52), _raw(-14952, 30, 18, f22="64651"))
    _drive(app, clock, datetime(2026, 11, 16, 9, 10), _raw(-17952, 30, 18, f22="64651"))
    assert app._trading_guard._last_net_pnl == -3000
    assert not app._trading_guard.paused


def test_n10b_late_batch_zero_row_in_day_budget_with_positive_b_day(tmp_path):
    """Winning Friday and night: B_day is +12,000. The 08:50 zero row holds about 0."""
    tr = _tracker(tmp_path / "n10b.json")
    snap = _raw(5030, 30, 0, f22="A")
    pre = _raw(12060, 60, 0, f22="A")
    tr.observe(snap, datetime(2026, 11, 13, 14, 59))
    tr.consume_failsafe_notice()
    tr.observe(snap, datetime(2026, 11, 13, 15, 0, 5))
    tr.observe(pre, datetime(2026, 11, 14, 5, 0))
    tr.observe(pre, datetime(2026, 11, 16, 8, 31))
    tr.observe(pre, datetime(2026, 11, 16, 8, 46))
    v = tr.observe(_raw(0, 0, 0, f22="A"), datetime(2026, 11, 16, 8, 50))
    assert v.loss_today >= -100, v.loss_today
    v = tr.observe(_raw(7030, 30, 0, f22="B"), datetime(2026, 11, 16, 8, 52))
    assert v.loss_today >= -100, v.loss_today
    v = tr.observe(_raw(4030, 30, 0, f22="B"), datetime(2026, 11, 16, 9, 10))
    assert v.loss_today == -3000, v.loss_today
