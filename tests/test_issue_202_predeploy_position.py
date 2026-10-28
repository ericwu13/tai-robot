"""Issue #202: pre-deploy OpenInterest must fail closed on UNKNOWN.

``001`` then ``##`` is an answered flat book. A bare ``##``, or an
error followed by ``##``, leaves the book UNKNOWN. A reply is credited
only to the query that was in flight when it was enqueued. A non-zero
``GetOpenInterestGW`` code, a timeout, or an exception refuses a
semi_auto/auto deploy. Paper is unchanged. ``--allow-existing-position``
does not allow an unknown book.
"""

from __future__ import annotations

import queue
from types import SimpleNamespace

import pytest

import run_backtest as rb
from src.live import headless as hl
from src.live.account_monitor import AccountMonitor
from src.live.deploy_request import DeployRequest
from src.live.headless_app import DeployDriver, HeadlessBotApp


class _Var:
    def __init__(self, value=""):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


def _drain_ui_queue() -> None:
    while True:
        try:
            rb._ui_queue.get_nowait()
        except queue.Empty:
            return


def _oi_app(monitor, *, fill_pending=False, on_fill=None):
    return SimpleNamespace(
        _account_monitor=monitor,
        real_pos_var=SimpleNamespace(set=lambda text: None),
        _trading_guard=SimpleNamespace(fill_pending=fill_pending),
        _live_runner=SimpleNamespace(symbol="TX00") if fill_pending else None,
        _update_real_account_display=lambda: None,
        _update_fill_poll_position=on_fill or (lambda raw, parsed: None),
    )


def test_001_then_hash_answers_via_the_callback_and_clear_keeps_it():
    """``001`` then ``##`` through OnOpenInterest is an empty snapshot.

    A row queued for the previous query is not credited after
    ``begin_oi_query``. ``clear_positions`` does not drop the answer.
    A bare ``##`` on the new generation stays UNKNOWN.
    """
    monitor = AccountMonitor()
    flat_labels = []
    app = _oi_app(monitor)
    app.real_pos_var = SimpleNamespace(set=lambda text: flat_labels.append(text))
    previous_app = rb._app
    rb._app = app
    try:
        _drain_ui_queue()
        old = monitor.begin_oi_query()
        rb.SKOrderLibEvents().OnOpenInterest("001,查無資料")
        latest = monitor.begin_oi_query()
        assert latest != old
        rb.BacktestApp._process_ui_queue(app)
        assert monitor.oi_snapshot_received is False
        assert monitor.positions == []

        rb.SKOrderLibEvents().OnOpenInterest("##")
        rb.BacktestApp._process_ui_queue(app)
        assert monitor.oi_snapshot_received is False

        rb.SKOrderLibEvents().OnOpenInterest("001,查無資料")
        rb.SKOrderLibEvents().OnOpenInterest("##")
        rb.BacktestApp._process_ui_queue(app)
        assert monitor.oi_snapshot_received is True
        assert monitor.positions == []
        assert monitor.has_open_positions() is False
        assert flat_labels == ["Flat (無持倉)", "Flat (無持倉)"]
        monitor.clear_positions()
        assert monitor.oi_snapshot_received is True
        assert monitor.positions == []
    finally:
        rb._app = previous_app
        _drain_ui_queue()


def test_stale_row_does_not_update_fill_poll_while_fill_pending():
    """A row tagged for an older query must not confirm a fill.

    Replacing ``outcome != "stale"`` with ``True`` in the queue handler
    calls ``_update_fill_poll_position`` for this row.
    """
    monitor = AccountMonitor()
    updates = []
    app = _oi_app(
        monitor, fill_pending=True,
        on_fill=lambda raw, parsed: updates.append((raw, parsed)),
    )
    previous_app = rb._app
    rb._app = app
    try:
        _drain_ui_queue()
        older = monitor.begin_oi_query()
        latest = monitor.begin_oi_query()
        row = "TF,ACCT,TXFD0,B,1,0,22500.0,15,2,ID"
        rb._ui_queue.put_nowait(("open_interest", (row, older)))
        rb.BacktestApp._process_ui_queue(app)
        assert updates == []
        assert monitor.oi_snapshot_received is False

        rb._ui_queue.put_nowait(("open_interest", (row, latest)))
        rb.BacktestApp._process_ui_queue(app)
        assert len(updates) == 1
        assert updates[0][1]["product"] == "TXFD0"
    finally:
        rb._app = previous_app
        _drain_ui_queue()


def test_machine_reconcile_starts_a_new_oi_generation(monkeypatch):
    """A reconnect reconcile is its own query, so its reply cannot finish a later deploy."""
    monitor = AccountMonitor()
    before = monitor.begin_oi_query()
    monitor.set_flat()
    assert monitor.oi_snapshot_received is True
    app = SimpleNamespace(
        _account_monitor=monitor,
        login_user_var=_Var("user"),
        _futures_account="F0123456",
    )
    app._requery_open_interest = lambda tag: rb.BacktestApp._requery_open_interest(app, tag)
    calls = []

    def _call_com(name, fn, *args, **kwargs):
        calls.append((name, monitor.oi_query_generation))
        return 0

    monkeypatch.setattr(rb, "_com_available", True)
    monkeypatch.setattr(rb, "skO", SimpleNamespace(GetOpenInterestGW=lambda *a: 0))
    monkeypatch.setattr(rb, "call_com", _call_com)
    rb.BacktestApp._on_machine_reconcile(app)
    assert calls == [("GetOpenInterestGW", monitor.oi_query_generation)]
    assert monitor.oi_query_generation != before
    assert monitor.oi_snapshot_received is False
    assert monitor.positions == []


def test_allow_flags_are_independent():
    """``--allow-existing-position`` must not imply an unknown book."""
    existing = hl.policy_from_args(hl.build_parser().parse_args(
        ["deploy", "--symbol", "TMF00", "--bot", "b1", "--allow-existing-position"]))
    unknown = hl.policy_from_args(hl.build_parser().parse_args(
        ["deploy", "--symbol", "TMF00", "--bot", "b1", "--allow-unknown-position"]))
    assert existing.allow_existing_position is True
    assert existing.allow_unknown_position is False
    assert unknown.allow_unknown_position is True
    assert unknown.allow_existing_position is False
    assert existing.answer(hl.TITLE_UNKNOWN_POSITION) is False
    assert unknown.answer(hl.TITLE_UNKNOWN_POSITION) is True
    assert unknown.answer(hl.TITLE_EXISTING_POSITION) is False
    src = __import__("inspect").getsource(rb.BacktestApp._deploy_live_from)
    assert hl.TITLE_UNKNOWN_POSITION in src


class _Gate:
    def __init__(self):
        self.continued = []
        self.confirms = []
        self.com_calls = []
        self.waits = []
        self.exit_code = None
        self.app = None


def _run_gate(monkeypatch, *, mode, policy, code=0, raises=False,
              answer_snapshot=False, bare_hash=False, use_driver=True,
              gui_answer=None):
    """Drive ``_deploy_live_from`` up to the position gate.

    ``gui_answer`` uses the real GUI ``_confirm`` (messagebox is patched
    by the caller). ``policy`` uses the headless answer.
    """
    gate = _Gate()
    monitor = AccountMonitor()
    monitor.set_flat()  # a previous answer must not satisfy this deploy
    strategy_name = next(iter(rb.STRATEGIES))
    app = SimpleNamespace(
        symbol_var=_Var("TMF00"),
        strategy_var=_Var(strategy_name),
        bot_name_var=_Var(),
        trading_mode_var=_Var(),
        pv_var=_Var("50"),
        login_user_var=_Var("user"),
        status_var=_Var(""),
        mode_combo=SimpleNamespace(config=lambda **kwargs: None),
        _mode_key_to_combo={
            "paper": "模擬 Paper",
            "semi_auto": "半自動 Semi-Auto",
            "auto": "全自動 Auto",
        },
        _settings={},
        _trading_guard=SimpleNamespace(daily_loss_limit=0, reset=lambda: None),
        _fill_poller=SimpleNamespace(reset=lambda: None),
        _session_end_exit_pending=False,
        _futures_account="F0123456",
        _account_monitor=monitor,
        _live_runner=None,
        _deploy_pending=False,
        policy=policy,
        alerts=[],
        root=SimpleNamespace(
            winfo_viewable=lambda: True,
            after=lambda *args, **kwargs: None,
            quit=lambda: None,
        ),
    )
    app.set_status = lambda msg, level="info": app.status_var.set(msg)
    app._alert = lambda title, message: app.alerts.append((title, message))

    def _confirm(title, message, default_no=False):
        gate.confirms.append((title, default_no))
        if gui_answer is not None:
            return rb.BacktestApp._confirm(
                app, title, message, default_no=default_no)
        return HeadlessBotApp._confirm(app, title, message)

    app._confirm = _confirm
    app._oi_wait_must_block = lambda: rb.BacktestApp._oi_wait_must_block(app)
    app._process_ui_queue = lambda: rb.BacktestApp._process_ui_queue(app)

    def _wait():
        gate.waits.append(1)
        return rb.BacktestApp._wait_oi_snapshot_blocking(app)

    app._wait_oi_snapshot_blocking = _wait

    def _continue(*args, **kwargs):
        gate.continued.append(args)
        app._live_runner = object()
        return True

    app._deploy_live_continue = _continue

    def _call_com(name, fn, *args, **kwargs):
        gate.com_calls.append(name)
        if raises:
            raise RuntimeError("gw down")
        if answer_snapshot and name == "GetOpenInterestGW":
            gen = app._account_monitor.oi_query_generation
            if bare_hash:
                app._account_monitor.apply_open_interest("##", generation=gen)
            else:
                app._account_monitor.apply_open_interest(
                    "001,查無資料", generation=gen)
                app._account_monitor.apply_open_interest("##", generation=gen)
        return code

    _drain_ui_queue()
    monkeypatch.setattr(rb, "call_com", _call_com)
    monkeypatch.setattr(
        rb, "skO", SimpleNamespace(GetOpenInterestGW=lambda *args: 0))
    monkeypatch.setattr(
        rb.LiveRunner, "check_lock", lambda bot_dir: (False, 0))

    req = DeployRequest(bot_name="issue202", trading_mode=mode, loss_limit="1000")
    app._deploy_live_from = lambda request: rb.BacktestApp._deploy_live_from(app, request)
    gate.app = app

    if not use_driver:
        gate.returned = app._deploy_live_from(req)
        return gate

    driver = DeployDriver.__new__(DeployDriver)
    driver.app = app
    driver.bot_dir = "issue202-not-a-live-tree"
    driver.req = req
    driver.exit_code = None
    driver.root = app.root
    driver._deploy()
    gate.exit_code = driver.exit_code
    return gate


@pytest.mark.parametrize("mode", ["auto", "semi_auto"])
def test_headless_unknown_timeout_exits_deploy_refused(monkeypatch, mode):
    monkeypatch.setattr(rb, "OI_SNAPSHOT_TIMEOUT_S", 0.0)
    gate = _run_gate(
        monkeypatch, mode=mode,
        policy=hl.HeadlessPolicy(),
        code=0,
    )
    assert gate.waits == [1]
    assert gate.com_calls == ["GetOpenInterestGW"]
    assert gate.continued == []
    assert gate.confirms and gate.confirms[0][0] == hl.TITLE_UNKNOWN_POSITION
    assert gate.confirms[0][1] is True  # GUI default No is requested
    assert gate.exit_code == hl.EXIT_DEPLOY_REFUSED


@pytest.mark.parametrize("mode", ["auto", "semi_auto"])
def test_nonzero_code_is_unknown_without_waiting(monkeypatch, mode):
    gate = _run_gate(
        monkeypatch, mode=mode,
        policy=hl.HeadlessPolicy(),
        code=5,
    )
    assert gate.waits == []
    assert gate.continued == []
    assert gate.exit_code == hl.EXIT_DEPLOY_REFUSED
    assert gate.app._predeploy_oi_unknown_reason == "GetOpenInterestGW code=5"
    assert gate.app._account_monitor.oi_snapshot_received is False


@pytest.mark.parametrize("mode", ["auto", "semi_auto"])
def test_headless_query_exception_exits_deploy_refused(monkeypatch, mode):
    gate = _run_gate(
        monkeypatch, mode=mode,
        policy=hl.HeadlessPolicy(allow_existing_position=True),
        raises=True,
    )
    assert gate.waits == []
    assert gate.continued == []
    assert gate.exit_code == hl.EXIT_DEPLOY_REFUSED
    assert gate.app._predeploy_oi_failed is True
    # A known open position is a different question. This refusal is UNKNOWN.
    assert [title for title, _default in gate.confirms] == [hl.TITLE_UNKNOWN_POSITION]


def test_allow_unknown_position_allows_the_deploy(monkeypatch):
    gate = _run_gate(
        monkeypatch, mode="auto",
        policy=hl.HeadlessPolicy(allow_unknown_position=True),
        code=7,
    )
    assert gate.continued
    assert gate.exit_code is None
    assert gate.app._live_runner is not None


def test_allow_existing_position_alone_still_refuses_unknown(monkeypatch):
    gate = _run_gate(
        monkeypatch, mode="auto",
        policy=hl.HeadlessPolicy(allow_existing_position=True),
        code=4,
    )
    assert gate.continued == []
    assert gate.exit_code == hl.EXIT_DEPLOY_REFUSED
    assert gate.confirms[0][0] == hl.TITLE_UNKNOWN_POSITION


def test_bare_hash_snapshot_refuses_auto(monkeypatch):
    """A deploy whose only reply is ``##`` stays UNKNOWN and is refused."""
    gate = _run_gate(
        monkeypatch, mode="auto",
        policy=hl.HeadlessPolicy(),
        code=0,
        answer_snapshot=True,
        bare_hash=True,
    )
    assert gate.continued == []
    assert gate.exit_code == hl.EXIT_DEPLOY_REFUSED
    assert gate.confirms and gate.confirms[0][0] == hl.TITLE_UNKNOWN_POSITION
    assert gate.app._account_monitor.oi_snapshot_received is False


def test_001_then_hash_allows_auto_without_unknown_dialog(monkeypatch):
    gate = _run_gate(
        monkeypatch, mode="auto",
        policy=hl.HeadlessPolicy(),
        code=0,
        answer_snapshot=True,
    )
    assert gate.confirms == []
    assert gate.continued
    assert gate.exit_code is None
    assert gate.app._account_monitor.oi_snapshot_received is True
    assert gate.app._account_monitor.has_open_positions() is False


def test_paper_mode_does_not_query_or_refuse(monkeypatch):
    gate = _run_gate(
        monkeypatch, mode="paper",
        policy=hl.HeadlessPolicy(),
        code=0,
    )
    assert gate.com_calls == []
    assert gate.confirms == []
    assert gate.continued
    assert gate.exit_code is None


@pytest.mark.parametrize("mode", ["auto", "semi_auto"])
def test_gui_unknown_confirm_defaults_to_no(monkeypatch, mode):
    seen = []

    def _askyesno(title, message, **kwargs):
        seen.append((title, kwargs.get("default")))
        return False

    monkeypatch.setattr(rb.messagebox, "askyesno", _askyesno)
    gate = _run_gate(
        monkeypatch, mode=mode,
        policy=None,
        code=9,
        use_driver=False,
        gui_answer=False,
    )
    assert gate.returned is False
    assert gate.continued == []
    assert seen == [(hl.TITLE_UNKNOWN_POSITION, rb.messagebox.NO)]
    assert "UNKNOWN" in gate.app.status_var.get()


def test_gui_yes_on_unknown_dialog_continues(monkeypatch):
    """No is the default; choosing Yes still deploys."""
    seen = []

    def _askyesno(title, message, **kwargs):
        seen.append(kwargs.get("default"))
        return True

    monkeypatch.setattr(rb.messagebox, "askyesno", _askyesno)
    gate = _run_gate(
        monkeypatch, mode="auto",
        policy=None,
        code=9,
        use_driver=False,
        gui_answer=True,
    )
    assert seen == [rb.messagebox.NO]
    assert gate.returned is True
    assert gate.continued
