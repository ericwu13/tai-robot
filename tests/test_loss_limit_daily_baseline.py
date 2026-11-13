"""REAL loss limit measured from the 15:00 TPE snapshot (Eric option B).

T1–T17 from the 2026-11-13 scope. The gated figure is the loss since the
last pre-roll read, not Capital's full account net. Master still gates on
the full net, so T1/T5/T6/T7/T9/T10/T11/T14/T16 fail there with the old
number. ``tests/test_loss_limit_day_reset.py`` stays the #242 latch.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

import run_backtest as rb
import src.live.main_heartbeat as hb
from scripts.dashboard import snapshot
from src.live.account_monitor import parse_future_rights
from tests.test_live_mode_request import harness
from tests.test_loss_gate_234 import _app, _push_rights, _rights

F22 = "71649"
F22_NEW = "64651"
# 11/13 14:59 and the night that follows. R = f11 - f2 - f3.
# 14:59: -6950 - 30 - 18 = -6998. Night: -12210 - 60 - 36 = -12306.
# Today's loss = -12306 - (-6998) = -5308.
NIGHT_LOSS = -5308
ACCOUNT_NIGHT = -12306


class _Clock:
    def __init__(self, when: datetime):
        self.when = when

    def __call__(self) -> datetime:
        return self.when


class _Discord:
    enabled = True

    def __init__(self):
        self.notes: list[tuple[str, bool]] = []
        self.loss: list[tuple] = []

    def notify(self, msg, priority=False):
        self.notes.append((msg, priority))

    def daily_loss_limit(self, net, limit, account_net=None):
        self.loss.append((net, limit, account_net))


def _baseline_cls():
    try:
        from src.live.loss_baseline import DailyLossBaseline
    except ImportError as exc:
        pytest.fail(f"master has no DailyLossBaseline ({exc})")
    return DailyLossBaseline


def _attach(app, path):
    """Bind a tracker when the fix is present. Master leaves it unset."""
    try:
        from src.live.loss_baseline import DailyLossBaseline
    except ImportError:
        return None
    tracker = DailyLossBaseline(str(path), app._futures_account)
    app._loss_baseline = tracker
    return tracker


def _clock(monkeypatch, when: datetime) -> _Clock:
    clock = _Clock(when)
    monkeypatch.setattr(rb, "_taipei_now", clock)
    return clock


def _raw(f11, f2, f3, f1=0, f22=F22) -> dict:
    return {
        "float_pnl": str(f1),
        "realized_cost": str(f2),
        "tax": str(f3),
        "realized_pnl": str(f11),
        "start_equity": str(f22),
        "equity": "1",
        "available": "1",
        "maint_rate": "100",
    }


def _wire_row(f11, f2, f3, f1=0, f22=F22) -> str:
    vals = ["0"] * 38
    vals[1] = str(f1)
    vals[2] = str(f2)
    vals[3] = str(f3)
    vals[11] = str(f11)
    vals[22] = str(f22)
    vals[25] = "TWD"
    return ",".join(vals)


def _drive(app, clock, when, raw):
    clock.when = when
    app._account_monitor.update_rights(raw)
    return rb.BacktestApp._update_real_account_display(app)


def _app_at(monkeypatch, when, mode="auto", limit=10000, path=None):
    monkeypatch.setattr(rb, "_discord", None)
    app = _app(mode)
    app._trading_guard.daily_loss_limit = limit
    clock = _clock(monkeypatch, when)
    tracker = _attach(app, path) if path is not None else None
    return app, clock, tracker


def _seed_friday_snapshot(observe, *, day=13):
    """14:59 candidate and the 15:00 commit on 2026-11-``day``."""
    pre = datetime(2026, 11, day, 14, 59, 59)
    roll = datetime(2026, 11, day, 15, 0, 29)
    row = _raw(-6950, 30, 18)
    observe(row, pre)
    return observe(row, roll)


def _night_row():
    return _raw(-12210, 60, 36)


def _load_file(path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _assert_each_reset_signal(tmp_path):
    """Each Capital-reset signal has to be able to fire on its own.

    (a) fee dropped below the running max, still at or above the snapshot.
    (b) fee is below the snapshot, and the running max does not show a drop.
    (c) f22 changed and the fee total did not.
    """
    DailyLossBaseline = _baseline_cls()

    # (a) only.
    path_a = tmp_path / "sig-a.json"
    tr = DailyLossBaseline(str(path_a), "ACCT")
    tr.observe(_raw(-6950, 30, 18), datetime(2026, 11, 12, 14, 59, 59))
    tr.observe(_raw(-6950, 30, 18), datetime(2026, 11, 12, 15, 0, 1))
    tr.observe(_night_row(), datetime(2026, 11, 13, 2, 0))
    # fees 60 < max 96, fees 60 > baseline 48, f22 unchanged, R rebased.
    view = tr.observe(_raw(-5248, 40, 20), datetime(2026, 11, 13, 5, 10))
    assert view.basis == "capital_reset", "signal (a) did not detect the reset"
    assert view.loss_today == NIGHT_LOSS, view.loss_today

    # (b) only — restored max does not show a drop, snapshot fees still do.
    path_b = tmp_path / "sig-b.json"
    account = DailyLossBaseline(str(path_b), "ACCT")._account_hash
    path_b.write_text(json.dumps({
        "version": 1,
        "account": account,
        "day_key": "2026-11-12",
        "basis": "snapshot",
        "baseline": {
            "B_r": -6998, "B_c": 0, "fees": 96, "f22": F22,
            "taken_at": "2026-11-12T14:59:59+08:00",
        },
        "candidate": None,
        "max_fees_seen": 40,
        "last_l": NIGHT_LOSS,
        "capital_reset": None,
        "pause_failsafe": False,
    }), encoding="utf-8")
    tr = DailyLossBaseline(str(path_b), "ACCT")
    tr.load(datetime(2026, 11, 13, 9, 0))
    view = tr.observe(_raw(-5248, 20, 20), datetime(2026, 11, 13, 9, 0))
    assert view.basis == "capital_reset", "signal (b) did not detect the reset"
    assert view.loss_today == NIGHT_LOSS, view.loss_today

    # (c) only — fees stay at the snapshot, f22 moves, R is already rebased.
    path_c = tmp_path / "sig-c.json"
    tr = DailyLossBaseline(str(path_c), "ACCT")
    tr.observe(_raw(-6950, 30, 18), datetime(2026, 11, 12, 14, 59, 59))
    tr.observe(_raw(-6950, 30, 18), datetime(2026, 11, 12, 15, 0, 1))
    caught = tr.observe(
        _raw(-5260, 30, 18, f22=F22_NEW), datetime(2026, 11, 13, 5, 10))
    assert caught.basis == "capital_reset", "signal (c) did not detect the reset"
    settled = tr.observe(
        _raw(-5260, 30, 18, f22=F22_NEW), datetime(2026, 11, 13, 8, 30))
    assert settled.loss_today == NIGHT_LOSS, settled.loss_today
    assert settled.basis == "capital_reset"


# ── T1 ─────────────────────────────────────────────────────────────────

def test_t1_friday_night_loss_is_5308_not_the_whole_account(tmp_path, monkeypatch):
    """11/13 14:59 → 15:00 → 11/14 00:00 → 05:00. Room against 20,000 is 14,692.

    Master stores the 05:00 account net (-12,306) and pauses at a 10,000 limit.
    """
    app, clock, _tracker = _app_at(
        monkeypatch, datetime(2026, 11, 13, 14, 59, 59), limit=10000,
        path=tmp_path / "loss_baseline.json")
    same = _raw(-6950, 30, 18)
    _drive(app, clock, datetime(2026, 11, 13, 14, 59, 59, 556000), same)
    _drive(app, clock, datetime(2026, 11, 13, 15, 0, 29, 666000), same)
    _drive(app, clock, datetime(2026, 11, 14, 0, 0, 39), same)
    _drive(app, clock, datetime(2026, 11, 14, 5, 0, 14), _night_row())

    guard = app._trading_guard
    assert guard._last_net_pnl == NIGHT_LOSS, guard._last_net_pnl
    assert guard.paused is False
    assert 20000 + guard._last_net_pnl == 14692
    view = getattr(app, "_loss_view", None)
    assert view is not None
    assert view.account_net == ACCOUNT_NIGHT
    assert view.loss_today == NIGHT_LOSS
    assert view.day_key == "2026-11-13"
    parsed = parse_future_rights(_wire_row(-6950, 30, 18))
    assert parsed["start_equity"] == F22
    assert parsed["realized_pnl"] == "-6950"


# ── T2 ─────────────────────────────────────────────────────────────────

def test_t2_restart_keeps_the_baseline_and_a_candidate_can_commit(tmp_path, monkeypatch):
    app, clock, tracker = _app_at(
        monkeypatch, datetime(2026, 11, 13, 14, 59, 59),
        path=tmp_path / "loss_baseline.json")
    if tracker is None:
        fresh, fresh_clock, _none = _app_at(
            monkeypatch, datetime(2026, 11, 14, 1, 0),
            path=None)
        _drive(fresh, fresh_clock, datetime(2026, 11, 14, 1, 0), _night_row())
        assert fresh._trading_guard._last_net_pnl == NIGHT_LOSS
        return

    same = _raw(-6950, 30, 18)
    _drive(app, clock, datetime(2026, 11, 13, 14, 59, 59), same)
    _drive(app, clock, datetime(2026, 11, 13, 15, 0, 29), same)
    assert (tmp_path / "loss_baseline.json").is_file()

    DailyLossBaseline = _baseline_cls()
    restarted = DailyLossBaseline(str(tmp_path / "loss_baseline.json"), "ACCT")
    restarted.load(datetime(2026, 11, 14, 1, 0))
    view = restarted.observe(_night_row(), datetime(2026, 11, 14, 1, 0))
    assert view.loss_today == NIGHT_LOSS, view.loss_today
    assert view.account_net == ACCOUNT_NIGHT
    saved = _load_file(tmp_path / "loss_baseline.json")
    assert saved["baseline"]["B_r"] == -6998

    # Only the candidate made it to disk. The 15:10 fill must stay outside it.
    cand_path = tmp_path / "candidate.json"
    first = DailyLossBaseline(str(cand_path), "ACCT")
    first.observe(same, datetime(2026, 11, 13, 14, 59, 59))
    second = DailyLossBaseline(str(cand_path), "ACCT")
    second.load(datetime(2026, 11, 13, 15, 10, 3))
    # f11 -7150, fees 40+18 → R -7208. Delta from -6998 is -210.
    filled = second.observe(
        _raw(-7150, 40, 18), datetime(2026, 11, 13, 15, 10, 3))
    assert filled.loss_today == -210, filled.loss_today
    assert _load_file(cand_path)["baseline"]["B_r"] == -6998


# ── T3 ─────────────────────────────────────────────────────────────────

def test_t3_missing_snapshot_failsafe_logs_once_and_counts_the_account(tmp_path, monkeypatch):
    discord = _Discord()
    monkeypatch.setattr(rb, "_discord", discord)
    app = _app("auto")
    app._trading_guard.daily_loss_limit = 20000
    clock = _clock(monkeypatch, datetime(2026, 11, 13, 15, 20))
    tracker = _attach(app, tmp_path / "loss_baseline.json")
    if tracker is None:
        pytest.fail("master has no failsafe baseline notice")

    row = _raw(-6950, 30, 18)
    _drive(app, clock, datetime(2026, 11, 13, 15, 20), row)
    _drive(app, clock, datetime(2026, 11, 13, 15, 40), row)

    guard = app._trading_guard
    assert guard._last_net_pnl == -6998
    view = app._loss_view
    assert view.basis == "failsafe"
    assert view.loss_today == view.account_net == -6998
    text = app.live_log.text()
    assert text.count("no 15:00 baseline for 2026-11-13") == 1
    assert len(discord.notes) == 1
    assert discord.notes[0][1] is False
    assert "no 15:00 baseline for 2026-11-13" in discord.notes[0][0]

    bot = tmp_path / "bot"
    bot.mkdir()
    writer = hb.HeartbeatWriter(str(bot))
    hb._writer = writer
    try:
        app._publish_loss_heartbeat()
        hb.tick()
        payload = json.loads((bot / "heartbeat.json").read_text(encoding="utf-8"))
    finally:
        hb._writer = None
    assert payload["loss_basis"] == "failsafe"
    assert payload["loss_net"] == -6998
    shown = snapshot.loss_status(payload)
    assert "no 15:00 baseline" in shown["text"]
    assert "today -6,998" in shown["text"]


# ── T4 ─────────────────────────────────────────────────────────────────

def test_t4_invalid_reply_does_not_commit_or_move_max_fees(tmp_path):
    DailyLossBaseline = _baseline_cls()
    tr = DailyLossBaseline(str(tmp_path / "loss_baseline.json"), "ACCT")
    good = _raw(-6950, 30, 18)
    tr.observe(good, datetime(2026, 11, 13, 14, 55))
    fees_after_candidate = _load_file(tmp_path / "loss_baseline.json")["max_fees_seen"]
    invalid = {
        "realized_pnl": "nope", "realized_cost": "1", "tax": "1",
        "float_pnl": "0", "start_equity": "999",
    }
    tr.observe(invalid, datetime(2026, 11, 13, 15, 0, 5))
    tr.observe(None, datetime(2026, 11, 13, 15, 0, 6))
    assert _load_file(tmp_path / "loss_baseline.json")["max_fees_seen"] == fees_after_candidate
    # The valid read at 15:00:35 commits the 14:55 candidate, not a new baseline.
    view = tr.observe(good, datetime(2026, 11, 13, 15, 0, 35))
    assert view.basis == "snapshot"
    assert view.loss_today == 0
    assert _load_file(tmp_path / "loss_baseline.json")["baseline"]["B_r"] == -6998
    assert _load_file(tmp_path / "loss_baseline.json")["max_fees_seen"] == 48


def test_t_case_fill_at_150103_stays_outside_the_baseline(tmp_path):
    """A fill on the first post-roll read is today's loss, not the baseline.

    7/23, 8/27 and 8/28 all printed a fill on the first poll after 15:00
    (the 8/27 and 8/28 prints are 15:01:03). Using that read as the
    baseline would report 0 here.
    """
    DailyLossBaseline = _baseline_cls()
    tr = DailyLossBaseline(str(tmp_path / "loss_baseline.json"), "ACCT")
    pre = _raw(-6950, 30, 18)
    tr.observe(pre, datetime(2026, 8, 28, 14, 59, 59))
    view = tr.observe(_raw(-7150, 40, 18), datetime(2026, 8, 28, 15, 1, 3))
    assert view.loss_today == -210, view.loss_today
    assert _load_file(tmp_path / "loss_baseline.json")["baseline"]["B_r"] == -6998


# ── T5 ─────────────────────────────────────────────────────────────────

def test_t5_failsafe_pause_rechecks_once_after_the_morning_batch(tmp_path, monkeypatch):
    """No snapshot, night loss pauses. The batch must not release before 08:30.

    A reload inside the limit releases. A reload still past the limit stays
    paused, and that new pause is not failsafe (no second recheck).
    """
    def _run(reload_f11, expect_paused):
        app, clock, tracker = _app_at(
            monkeypatch, datetime(2026, 11, 13, 15, 20), limit=5000,
            path=tmp_path / f"fs-{reload_f11}.json")
        if tracker is None:
            pytest.fail("master keeps a failsafe pause until the next 15:00")
        night = _raw(-6950, 30, 18)
        _drive(app, clock, datetime(2026, 11, 13, 15, 20), night)
        guard = app._trading_guard
        assert guard.paused is True
        assert guard._pause_failsafe is True
        _drive(app, clock, datetime(2026, 11, 14, 5, 22), _raw(0, 0, 0))
        assert guard.paused is True
        reload = _raw(reload_f11, 10, 0, f22=F22_NEW)
        _drive(app, clock, datetime(2026, 11, 14, 5, 36), reload)
        assert guard.paused is True, "released before 08:30"
        _drive(app, clock, datetime(2026, 11, 14, 8, 30), reload)
        assert guard.paused is expect_paused
        return guard

    released = _run(-990, False)  # R = -1000, inside a 5,000 limit
    assert released._pause_failsafe is False
    held = _run(-5990, True)  # R = -6000, still past the limit
    assert held.paused is True
    assert held._pause_failsafe is False


# ── T6 ─────────────────────────────────────────────────────────────────

def test_t6_monday_batch_holds_friday_night_loss_then_adds_the_day(tmp_path, monkeypatch):
    """11/16 05:22 zeros and 05:36 reload both stay at -5,308. B_r becomes 0.

    Settled at 08:30, still Friday night's figure. Per Eric (Q1) Monday's
    day session is its own budget, so the -3,000 day trade is -3,000, not
    -8,308.
    """
    app, clock, tracker = _app_at(
        monkeypatch, datetime(2026, 11, 13, 14, 59, 59), limit=20000,
        path=tmp_path / "loss_baseline.json")
    same = _raw(-6950, 30, 18)
    _drive(app, clock, datetime(2026, 11, 13, 14, 59, 59), same)
    _drive(app, clock, datetime(2026, 11, 13, 15, 0, 29), same)
    _drive(app, clock, datetime(2026, 11, 14, 5, 0, 14), _night_row())
    zeros = _raw(0, 0, 0)
    _drive(app, clock, datetime(2026, 11, 16, 5, 22), zeros)
    assert app._trading_guard._last_net_pnl == NIGHT_LOSS, app._trading_guard._last_net_pnl
    reload = _raw(-5260, 30, 18, f22=F22_NEW)
    _drive(app, clock, datetime(2026, 11, 16, 5, 36), reload)
    assert app._trading_guard._last_net_pnl == NIGHT_LOSS
    _drive(app, clock, datetime(2026, 11, 16, 8, 30), reload)
    assert app._trading_guard._last_net_pnl == NIGHT_LOSS
    if tracker is None:
        pytest.fail("master did not persist B_r = 0 after the batch")
    assert _load_file(tmp_path / "loss_baseline.json")["baseline"]["B_r"] == 0
    # -3,000 on top of the rebased night. Monday's own budget counts only
    # that -3,000 (B_day is the 08:30 net of -5,308).
    _drive(app, clock, datetime(2026, 11, 16, 9, 10), _raw(-8260, 30, 18, f22=F22_NEW))
    assert app._trading_guard._last_net_pnl == -3000
    _assert_each_reset_signal(tmp_path)


# ── T7 ─────────────────────────────────────────────────────────────────

def test_t7_f22_first_transient_does_not_pause(tmp_path, monkeypatch):
    """f22 moves while f11 and the fees are still yesterday's. Hold the gate.

    The limit sits between today's loss (-5,308) and the full account
    (-12,306), so the un-held formula would pause.
    """
    app, clock, _tracker = _app_at(
        monkeypatch, datetime(2026, 11, 12, 14, 59, 59), limit=10000,
        path=tmp_path / "loss_baseline.json")
    same = _raw(-6950, 30, 18)
    _drive(app, clock, datetime(2026, 11, 12, 14, 59, 59), same)
    _drive(app, clock, datetime(2026, 11, 12, 15, 0, 1), same)
    _drive(app, clock, datetime(2026, 11, 13, 2, 0), _night_row())
    assert app._trading_guard.paused is False
    _drive(app, clock, datetime(2026, 11, 13, 5, 35), _raw(-12210, 60, 36, f22=F22_NEW))
    assert app._trading_guard.paused is False
    assert app._trading_guard._last_net_pnl == NIGHT_LOSS, app._trading_guard._last_net_pnl


# ── T8 ─────────────────────────────────────────────────────────────────

def test_t8_first_read_after_the_batch_is_the_night_only(tmp_path):
    """Baseline persisted, bot down across the batch. Don't add the -6,998 back."""
    DailyLossBaseline = _baseline_cls()
    _assert_each_reset_signal(tmp_path)
    path = tmp_path / "down.json"
    account = DailyLossBaseline(str(path), "ACCT")._account_hash
    path.write_text(json.dumps({
        "version": 1,
        "account": account,
        "day_key": "2026-11-12",
        "basis": "snapshot",
        "baseline": {
            "B_r": -6998, "B_c": 0, "fees": 48, "f22": F22,
            "taken_at": "2026-11-12T14:59:59+08:00",
        },
        "candidate": {
            "date": "2026-11-12", "B_r": -6998, "B_c": 0, "fees": 48,
            "f22": F22, "taken_at": "2026-11-12T14:59:59+08:00",
        },
        "max_fees_seen": 96,
        "last_l": NIGHT_LOSS,
        "capital_reset": None,
        "pause_failsafe": False,
    }), encoding="utf-8")
    tr = DailyLossBaseline(str(path), "ACCT")
    tr.load(datetime(2026, 11, 13, 9, 0))
    view = tr.observe(_raw(-5260, 30, 18, f22=F22_NEW), datetime(2026, 11, 13, 9, 0))
    assert view.loss_today == NIGHT_LOSS, view.loss_today
    assert view.loss_today != NIGHT_LOSS + -6998
    assert view.basis == "capital_reset"


# ── T9 ─────────────────────────────────────────────────────────────────

def test_t9_open_profit_is_a_credit_and_an_open_loss_counts_in_full(tmp_path, monkeypatch):
    app, clock, _tracker = _app_at(
        monkeypatch, datetime(2026, 11, 12, 14, 59), limit=20000,
        path=tmp_path / "loss_baseline.json")
    # F0 = +3,000 → B_c 3,000. Later F = -1,000, R unchanged → L = -4,000.
    _drive(app, clock, datetime(2026, 11, 12, 14, 59), _raw(-980, 12, 8, f1=3000))
    _drive(app, clock, datetime(2026, 11, 12, 15, 0, 5), _raw(-980, 12, 8, f1=3000))
    _drive(app, clock, datetime(2026, 11, 13, 2, 0), _raw(-980, 12, 8, f1=-1000))
    assert app._trading_guard._last_net_pnl == -4000, app._trading_guard._last_net_pnl

    app2, clock2, _tracker2 = _app_at(
        monkeypatch, datetime(2026, 11, 12, 14, 59), limit=20000,
        path=tmp_path / "loss-open.json")
    _drive(app2, clock2, datetime(2026, 11, 12, 14, 59), _raw(-980, 12, 8, f1=-2000))
    _drive(app2, clock2, datetime(2026, 11, 12, 15, 0, 5), _raw(-980, 12, 8, f1=-2000))
    _drive(app2, clock2, datetime(2026, 11, 13, 2, 0), _raw(-980, 12, 8, f1=-2000))
    # B_c stays 0, so the open loss counts again in full.
    assert app2._trading_guard._last_net_pnl == -2000, app2._trading_guard._last_net_pnl


# ── T10 ────────────────────────────────────────────────────────────────

def test_t10_open_position_is_continuous_across_the_reset(tmp_path, monkeypatch):
    app, clock, tracker = _app_at(
        monkeypatch, datetime(2026, 11, 12, 14, 59), limit=20000,
        path=tmp_path / "loss_baseline.json")
    # B_r -1000, B_c 500 (F0 +500), fees 20.
    base = _raw(-980, 12, 8, f1=500)
    _drive(app, clock, datetime(2026, 11, 12, 14, 59), base)
    _drive(app, clock, datetime(2026, 11, 12, 15, 0, 5), base)
    # Night: R -4000, F -200, fees 40. L = -3700.
    _drive(app, clock, datetime(2026, 11, 13, 2, 0), _raw(-3960, 24, 16, f1=-200))
    assert app._trading_guard._last_net_pnl == -3700, app._trading_guard._last_net_pnl
    _drive(app, clock, datetime(2026, 11, 13, 5, 22), _raw(0, 0, 0, f1=-200))
    assert app._trading_guard._last_net_pnl == -3700, app._trading_guard._last_net_pnl
    # Reloaded night: R -3000, F -200, fees 20. B_c kept → still -3700.
    reload = _raw(-2980, 12, 8, f1=-200, f22=F22_NEW)
    _drive(app, clock, datetime(2026, 11, 13, 5, 36), reload)
    assert app._trading_guard._last_net_pnl == -3700
    _drive(app, clock, datetime(2026, 11, 13, 8, 30), reload)
    assert app._trading_guard._last_net_pnl == -3700, app._trading_guard._last_net_pnl
    if tracker is not None:
        saved = _load_file(tmp_path / "loss_baseline.json")
        assert saved["baseline"]["B_r"] == 0
        assert saved["baseline"]["B_c"] == 500


# ── T11 ────────────────────────────────────────────────────────────────

def test_t11_weekend_carries_friday_and_a_pause_retrips(tmp_path, monkeypatch):
    app, clock, tracker = _app_at(
        monkeypatch, datetime(2026, 11, 13, 14, 59, 59), limit=5000,
        path=tmp_path / "loss_baseline.json")
    same = _raw(-6950, 30, 18)
    _drive(app, clock, datetime(2026, 11, 13, 14, 59, 59), same)
    _drive(app, clock, datetime(2026, 11, 13, 15, 0, 29), same)
    _drive(app, clock, datetime(2026, 11, 14, 2, 0), _night_row())
    guard = app._trading_guard
    assert guard.paused is True
    assert guard._pause_day_key == "2026-11-13"
    # Saturday afternoon would be a fresh snapshot if the carry were dropped.
    _drive(app, clock, datetime(2026, 11, 14, 14, 50), _night_row())
    _drive(app, clock, datetime(2026, 11, 14, 15, 0, 1), _night_row())
    assert guard._last_net_pnl == NIGHT_LOSS, guard._last_net_pnl
    assert guard.paused is True
    assert guard._pause_day_key == "2026-11-14"
    _drive(app, clock, datetime(2026, 11, 15, 14, 50), _night_row())
    _drive(app, clock, datetime(2026, 11, 15, 15, 0, 1), _night_row())
    assert guard._last_net_pnl == NIGHT_LOSS
    assert guard.paused is True
    assert guard._pause_day_key == "2026-11-15"
    _drive(app, clock, datetime(2026, 11, 16, 5, 36), _raw(-5260, 30, 18, f22=F22_NEW))
    assert guard._last_net_pnl == NIGHT_LOSS, guard._last_net_pnl
    if tracker is not None:
        assert _load_file(tmp_path / "loss_baseline.json")["basis"] in (
            "carried", "capital_reset")


# ── T12 ────────────────────────────────────────────────────────────────

def test_t12_snapshot_pause_survives_the_batch_and_releases_at_1500(tmp_path, monkeypatch):
    """#242 latch for a snapshot pause. The morning batch is not a release.

    Wednesday night pauses. A same-key recovery and the Thursday batch
    both leave it paused. Thursday 15:00 commits a new snapshot at ~0.
    """
    app, clock, _tracker = _app_at(
        monkeypatch, datetime(2026, 11, 11, 14, 59), limit=5000,
        path=tmp_path / "loss_baseline.json")
    same = _raw(-6950, 30, 18)
    _drive(app, clock, datetime(2026, 11, 11, 14, 59), same)
    _drive(app, clock, datetime(2026, 11, 11, 15, 0, 5), same)
    _drive(app, clock, datetime(2026, 11, 12, 2, 0), _night_row())
    guard = app._trading_guard
    assert guard.paused is True and guard._pause_day_key == "2026-11-11"
    assert guard._pause_failsafe is False
    # Same-key recovery (fees rise, so this is not a Capital reset).
    # R = -7098, so today's loss against -6998 is -100.
    _drive(app, clock, datetime(2026, 11, 12, 4, 0), _raw(-6988, 70, 40))
    assert guard.paused is True
    assert guard._pause_day_key == "2026-11-11"
    assert guard._last_net_pnl == -100, guard._last_net_pnl
    _drive(app, clock, datetime(2026, 11, 12, 5, 22), _raw(0, 0, 0))
    _drive(app, clock, datetime(2026, 11, 12, 5, 36), _raw(-38, 44, 18, f22=F22_NEW))
    _drive(app, clock, datetime(2026, 11, 12, 8, 30), _raw(-38, 44, 18, f22=F22_NEW))
    _drive(app, clock, datetime(2026, 11, 12, 9, 0), _raw(-38, 44, 18, f22=F22_NEW))
    assert guard.paused is True
    assert guard._pause_day_key == "2026-11-11"
    flat = _raw(-38, 44, 18, f22=F22_NEW)
    _drive(app, clock, datetime(2026, 11, 12, 14, 59), flat)
    _drive(app, clock, datetime(2026, 11, 12, 15, 0, 5), flat)
    assert guard.paused is False
    assert guard._pause_day_key == ""
    assert guard._last_net_pnl == 0


# ── T13 ────────────────────────────────────────────────────────────────

def test_t13_limit_zero_tracks_the_loss_and_releases_the_entry_hold(monkeypatch):
    monkeypatch.setattr(rb, "_discord", None)
    monkeypatch.setattr(rb, "_taipei_now", lambda: datetime(2026, 11, 13, 21, 0))
    app = _app("auto")
    app._trading_guard.daily_loss_limit = 0
    app._equity_refresh_owed = True
    app._equity_refresh_held_alerted = True
    app._equity_refresh_retry_at = "stale"
    _push_rights(app, _rights(realized_pnl="-5000"))
    assert app._trading_guard.paused is False
    assert app._trading_guard._last_net_pnl == -5000
    assert app._equity_refresh_owed is False
    assert app._equity_refresh_held_alerted is False
    assert app._equity_refresh_retry_at is None


# ── T14 ────────────────────────────────────────────────────────────────

def _seed_tracker(path, steps):
    DailyLossBaseline = _baseline_cls()
    tr = DailyLossBaseline(str(path), "ACCT")
    for when, raw in steps:
        tr.observe(raw, when)
    tr.save()
    return tr


def test_t14_switch_allows_when_only_the_account_is_past_the_limit(harness, tmp_path, monkeypatch):
    h = harness
    tracker = None
    try:
        from src.live.loss_baseline import DailyLossBaseline
        tracker = DailyLossBaseline(str(tmp_path / "loss_baseline.json"), h.app._futures_account)
    except ImportError:
        tracker = None
    if tracker is not None:
        h.app._loss_baseline = tracker
        same = _raw(-6950, 30, 18)
        tracker.observe(same, datetime(2026, 11, 13, 14, 59, 59))
        tracker.observe(same, datetime(2026, 11, 13, 15, 0, 29))
        tracker.observe(_night_row(), datetime(2026, 11, 14, 5, 0, 14))
        monkeypatch.setattr(rb, "_taipei_now", lambda: datetime(2026, 11, 14, 5, 0, 14))
    h.request(loss_limit=10000, request_id="allow-today")
    h.poll()
    h.oi("001,查無資料", "##")
    h.rights(_wire_row(-12210, 60, 36))
    h.poll()
    res = h.result()
    assert res["status"] == "accepted", res


def test_t14_switch_refuses_on_loss_today_when_the_account_is_inside(harness, tmp_path, monkeypatch):
    """Yesterday's open profit is not today's budget. Account -4,000, today -12,000."""
    h = harness
    try:
        from src.live.loss_baseline import DailyLossBaseline
        tracker = DailyLossBaseline(str(tmp_path / "loss_baseline.json"), h.app._futures_account)
    except ImportError:
        tracker = None
    if tracker is not None:
        h.app._loss_baseline = tracker
        won = _raw(8020, 12, 8)  # R = +8000
        tracker.observe(won, datetime(2026, 11, 12, 14, 59))
        tracker.observe(won, datetime(2026, 11, 12, 15, 0, 5))
        monkeypatch.setattr(rb, "_taipei_now", lambda: datetime(2026, 11, 13, 10, 0))
    h.request(loss_limit=10000, request_id="refuse-today")
    h.poll()
    h.oi("001,查無資料", "##")
    # R = -4000. Today's loss against a +8000 baseline is -12,000.
    h.rights(_wire_row(-3980, 12, 8))
    h.poll()
    res = h.result()
    assert res["status"] == "refused", res
    assert "虧損上限" in res["reason"]


def test_t14_paper_observes_and_does_not_pause(tmp_path, monkeypatch):
    app, clock, tracker = _app_at(
        monkeypatch, datetime(2026, 11, 13, 14, 59, 59), mode="paper",
        limit=5000, path=tmp_path / "loss_baseline.json")
    same = _raw(-6950, 30, 18)
    _drive(app, clock, datetime(2026, 11, 13, 14, 59, 59), same)
    _drive(app, clock, datetime(2026, 11, 13, 15, 0, 29), same)
    _drive(app, clock, datetime(2026, 11, 14, 5, 0, 14), _night_row())
    assert app._trading_guard.paused is False
    assert app._trading_guard._last_net_pnl == 0
    if tracker is None:
        pytest.fail("paper bot did not observe a baseline on master")
    saved = _load_file(tmp_path / "loss_baseline.json")
    assert saved["basis"] == "snapshot"
    assert saved["baseline"]["B_r"] == -6998


# ── T15 ────────────────────────────────────────────────────────────────

def test_t15_bad_file_is_failsafe_and_the_write_is_atomic(tmp_path, monkeypatch, caplog):
    DailyLossBaseline = _baseline_cls()
    path = tmp_path / "loss_baseline.json"
    account = DailyLossBaseline(str(path), "ACCT")._account_hash

    def _bad(payload, reason):
        path.write_text(payload, encoding="utf-8")
        tr = DailyLossBaseline(str(path), "ACCT")
        tr.load(datetime(2026, 11, 13, 10, 0))
        view = tr.observe(_raw(-6950, 30, 18), datetime(2026, 11, 13, 10, 0))
        assert view.loss_today == -6998, view.loss_today
        assert view.basis == "failsafe"
        assert reason in caplog.text

    caplog.set_level("WARNING")
    _bad(json.dumps({
        "version": 1, "account": "deadbeef0000", "day_key": "2026-11-13",
        "basis": "snapshot",
        "baseline": {"B_r": -1, "B_c": 0, "fees": 1, "f22": "1", "taken_at": ""},
    }), "account mismatch")
    _bad("{not json", "corrupt")
    _bad(json.dumps({"version": 2, "account": account}), "unknown version")

    tr = DailyLossBaseline(str(path), "ACCT")
    tr.observe(_raw(-6950, 30, 18), datetime(2026, 11, 12, 14, 59))
    good = path.read_bytes()

    def _boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(hb.os, "replace", _boom)
    # A fee increase forces a write. A failed replace must not tear the file.
    tr.observe(_raw(-6900, 40, 18), datetime(2026, 11, 12, 14, 59, 30))
    assert path.read_bytes() == good


# ── T16 ────────────────────────────────────────────────────────────────

def test_t16_heartbeat_and_dashboard_show_today_and_the_account(tmp_path, monkeypatch):
    app, clock, _tracker = _app_at(
        monkeypatch, datetime(2026, 11, 13, 14, 59, 59), limit=20000,
        path=tmp_path / "loss_baseline.json")
    same = _raw(-6950, 30, 18)
    _drive(app, clock, datetime(2026, 11, 13, 14, 59, 59), same)
    _drive(app, clock, datetime(2026, 11, 13, 15, 0, 29), same)
    _drive(app, clock, datetime(2026, 11, 14, 5, 0, 14), _night_row())

    bot = tmp_path / "bot"
    bot.mkdir()
    writer = hb.HeartbeatWriter(str(bot))
    hb._writer = writer
    try:
        app._publish_loss_heartbeat()
        hb.tick()
        payload = json.loads((bot / "heartbeat.json").read_text(encoding="utf-8"))
    finally:
        hb._writer = None
    assert payload["loss_net"] == NIGHT_LOSS, payload.get("loss_net")
    assert payload["loss_account_net"] == ACCOUNT_NIGHT
    assert payload["loss_day"] == "2026-11-13"
    assert payload["loss_basis"] == "snapshot"
    text = snapshot.loss_status(payload)["text"]
    assert text == "today -5,308 (acct -12,306) / limit 20,000"


# ── T17 ────────────────────────────────────────────────────────────────

def test_t17_gated_loss_is_never_looser_than_mark_to_market(tmp_path):
    """Random sessions: batches, restarts, open float. L_gate <= MTM since the roll."""
    import random
    DailyLossBaseline = _baseline_cls()
    rng = random.Random(20261113)
    for case in range(24):
        path = tmp_path / f"p{case}.json"
        tr = DailyLossBaseline(str(path), "ACCT")
        r0 = rng.randint(-8000, 4000)
        f0 = rng.choice([0, rng.randint(-2500, 2500)])
        fees0 = rng.randint(10, 60)
        f22 = "70000"
        night_fees = 0
        n_since = 0
        float_now = f0
        ready = False
        f_roll = f0

        def emit(when, capital_r, capital_f, capital_fees, capital_f22, true_n, true_f):
            nonlocal ready
            f2 = capital_fees
            view = tr.observe(
                _raw(capital_r + f2, f2, 0, f1=capital_f, f22=capital_f22), when)
            if ready and view.basis in ("snapshot", "carried", "capital_reset"):
                mtm = true_n + true_f - f_roll
                assert view.loss_today <= mtm, (
                    case, when, view.loss_today, mtm, view.basis, view.held)

        pre = datetime(2026, 11, 11, 14, 55)
        roll = datetime(2026, 11, 11, 15, 0, 5)
        emit(pre, r0, f0, fees0, f22, 0, f0)
        emit(roll, r0, f0, fees0, f22, 0, f0)
        ready = True
        fees = fees0
        for step in range(rng.randint(1, 3)):
            dn = rng.randint(-1500, 400)
            df = rng.randint(-800, 800)
            dfee = rng.randint(0, 12)
            n_since += dn
            float_now = f0 + df
            fees += dfee
            night_fees = fees - fees0
            when = datetime(2026, 11, 11, 16, 0) + timedelta(hours=step)
            emit(when, r0 + n_since, float_now, fees, f22, n_since, float_now)
        if rng.random() < 0.5:
            tr.save()
            tr = DailyLossBaseline(str(path), "ACCT")
            tr.load(when)
        shape = rng.choice(["clean", "zeros", "f22"])
        new_f22 = "64000"
        rebased_fees = night_fees
        morning = datetime(2026, 11, 12, 5, 20)
        if shape == "zeros":
            emit(morning, 0, 0, 0, f22, n_since, float_now)
            morning = datetime(2026, 11, 12, 5, 36)
        elif shape == "f22":
            emit(morning, r0 + n_since, float_now, fees, new_f22, n_since, float_now)
            morning = datetime(2026, 11, 12, 5, 36)
        emit(morning, n_since, float_now, rebased_fees, new_f22, n_since, float_now)
        emit(datetime(2026, 11, 12, 8, 30), n_since, float_now, rebased_fees,
             new_f22, n_since, float_now)
        if rng.random() < 0.4:
            tr.save()
            tr = DailyLossBaseline(str(path), "ACCT")
            tr.load(datetime(2026, 11, 12, 8, 30))
            emit(datetime(2026, 11, 12, 9, 0), n_since, float_now, rebased_fees,
                 new_f22, n_since, float_now)


# ── Q1: a gap longer than one calendar day gets its own day budget ────
# Per Eric via Selina/TY. Friday night and Monday day are separate
# 20,000 budgets. The same split applies to a holiday gap by date
# arithmetic (no holiday calendar). A normal next calendar day stays
# one budget.

def _pnl(realized: int, fee: int, f1: int = 0, f22: str = "AAA") -> dict:
    """Rights whose realized net (f11 - f2 - f3) is ``realized``."""
    return _raw(realized + fee, fee, 0, f1=f1, f22=f22)


def _friday_into_monday(app, clock, *, with_day_open: bool):
    """Friday night loss of -15,000, then Capital's Monday batch.

    Baseline realized -5,000. Night realized -20,000. After the rebase
    the account shows -15,000, which is Friday night.
    """
    base = _pnl(-5000, 10, f22="AAA")
    night = _pnl(-20000, 30, f22="AAA")
    reload = _pnl(-15000, 20, f22="BBB")
    _drive(app, clock, datetime(2026, 11, 13, 14, 59), base)
    _drive(app, clock, datetime(2026, 11, 13, 15, 0, 5), base)
    _drive(app, clock, datetime(2026, 11, 14, 2, 0), night)
    _drive(app, clock, datetime(2026, 11, 16, 5, 22), _pnl(0, 0, f22="AAA"))
    _drive(app, clock, datetime(2026, 11, 16, 5, 36), reload)
    if with_day_open:
        _drive(app, clock, datetime(2026, 11, 16, 8, 35), reload)
    return reload


def test_q1a_monday_day_gets_a_fresh_20000_after_friday_night(tmp_path, monkeypatch):
    """Friday night -15,000. Monday's day session starts with a full 20,000."""
    app, clock, _tracker = _app_at(
        monkeypatch, datetime(2026, 11, 13, 14, 59), limit=10000,
        path=tmp_path / "loss_baseline.json")
    reload = _friday_into_monday(app, clock, with_day_open=True)
    guard = app._trading_guard
    assert guard._last_net_pnl == -15000, guard._last_net_pnl
    assert guard.paused is True
    _drive(app, clock, datetime(2026, 11, 16, 9, 0), reload)
    assert guard._last_net_pnl == 0, guard._last_net_pnl
    assert guard.paused is False
    assert 20000 + guard._last_net_pnl == 20000


def test_q1b_monday_restart_at_1000_does_not_rebaseline(tmp_path, monkeypatch):
    app, clock, tracker = _app_at(
        monkeypatch, datetime(2026, 11, 13, 14, 59), limit=20000,
        path=tmp_path / "loss_baseline.json")
    reload = _friday_into_monday(app, clock, with_day_open=True)
    _drive(app, clock, datetime(2026, 11, 16, 9, 0), reload)
    if tracker is None:
        pytest.fail("no day baseline to restore")
    DailyLossBaseline = _baseline_cls()
    restarted = DailyLossBaseline(str(tmp_path / "loss_baseline.json"), "ACCT")
    restarted.load(datetime(2026, 11, 16, 10, 0))
    # Another -4,000. Measured from the 08:35 baseline, not from this read.
    view = restarted.observe(
        _pnl(-19000, 25, f22="BBB"), datetime(2026, 11, 16, 10, 0))
    assert view.loss_today == -4000, view.loss_today
    saved = _load_file(tmp_path / "loss_baseline.json")
    assert saved["day_baseline"]["B_r"] == -15000


def test_q1c_missing_monday_baseline_keeps_the_night_and_rechecks_once(tmp_path, monkeypatch):
    """No 08:30–08:45 read. Keep Friday night's figure, and recheck once."""
    discord = _Discord()
    monkeypatch.setattr(rb, "_discord", discord)
    path = tmp_path / "loss_baseline.json"
    app, clock, tracker = _app_at(
        monkeypatch, datetime(2026, 11, 13, 14, 59), limit=20000, path=path)
    monkeypatch.setattr(rb, "_discord", discord)
    reload = _friday_into_monday(app, clock, with_day_open=False)
    if tracker is None:
        pytest.fail("master has no day-open baseline")
    # Fresh guard: the pause is caused by using the night figure, not by
    # a pause already latched on Friday.
    fresh, fresh_clock, _bound = _app_at(
        monkeypatch, datetime(2026, 11, 16, 10, 0), limit=10000, path=None)
    monkeypatch.setattr(rb, "_discord", discord)
    DailyLossBaseline = _baseline_cls()
    restored = DailyLossBaseline(str(path), "ACCT")
    restored.load(datetime(2026, 11, 16, 10, 0))
    fresh._loss_baseline = restored
    discord.notes.clear()
    _drive(fresh, fresh_clock, datetime(2026, 11, 16, 10, 0), reload)
    guard = fresh._trading_guard
    assert guard._last_net_pnl == -15000, guard._last_net_pnl
    assert guard.paused is True
    assert guard._pause_failsafe is False  # the one recheck already ran
    text = fresh.live_log.text()
    assert fresh.live_log.text().count("no 08:30 baseline") == 1
    assert len(discord.notes) == 1
    assert discord.notes[0][1] is False
    # A later read still uses the night budget (-16,000), not Monday-only
    # (-1,000), and does not get a second recheck.
    _drive(fresh, fresh_clock, datetime(2026, 11, 16, 11, 0), _pnl(-16000, 25, f22="BBB"))
    assert guard._last_net_pnl == -16000, guard._last_net_pnl
    assert guard.paused is True
    assert fresh.live_log.text().count("no 08:30 baseline") == 1
    assert len(discord.notes) == 1


def test_q1d_holiday_gap_gets_its_own_day_budget(tmp_path, monkeypatch):
    """Wednesday night, no Thursday, Friday day. Plain date arithmetic."""
    app, clock, _tracker = _app_at(
        monkeypatch, datetime(2026, 11, 11, 14, 59), limit=20000,
        path=tmp_path / "loss_baseline.json")
    base = _pnl(-5000, 10, f22="AAA")
    _drive(app, clock, datetime(2026, 11, 11, 14, 59), base)
    _drive(app, clock, datetime(2026, 11, 11, 15, 0, 5), base)
    _drive(app, clock, datetime(2026, 11, 12, 2, 0), _pnl(-12000, 30, f22="AAA"))
    reload = _pnl(-7000, 20, f22="BBB")
    _drive(app, clock, datetime(2026, 11, 13, 5, 30), _pnl(0, 0, f22="AAA"))
    _drive(app, clock, datetime(2026, 11, 13, 8, 35), reload)
    assert app._trading_guard._last_net_pnl == -7000
    _drive(app, clock, datetime(2026, 11, 13, 9, 0), reload)
    assert app._trading_guard._last_net_pnl == 0, app._trading_guard._last_net_pnl
    _drive(app, clock, datetime(2026, 11, 13, 10, 0), _pnl(-9000, 25, f22="BBB"))
    assert app._trading_guard._last_net_pnl == -2000, app._trading_guard._last_net_pnl


def test_q1e_the_next_calendar_day_stays_one_budget(tmp_path, monkeypatch):
    """Tuesday night into Wednesday day is one budget. Gap is one day."""
    app, clock, _tracker = _app_at(
        monkeypatch, datetime(2026, 11, 10, 14, 59), limit=20000,
        path=tmp_path / "loss_baseline.json")
    base = _pnl(-5000, 10, f22="AAA")
    _drive(app, clock, datetime(2026, 11, 10, 14, 59), base)
    _drive(app, clock, datetime(2026, 11, 10, 15, 0, 5), base)
    _drive(app, clock, datetime(2026, 11, 11, 2, 0), _pnl(-9000, 30, f22="AAA"))
    assert app._trading_guard._last_net_pnl == -4000
    _drive(app, clock, datetime(2026, 11, 11, 8, 35), _pnl(-9000, 30, f22="AAA"))
    _drive(app, clock, datetime(2026, 11, 11, 9, 10), _pnl(-10000, 35, f22="AAA"))
    assert app._trading_guard._last_net_pnl == -5000, app._trading_guard._last_net_pnl
