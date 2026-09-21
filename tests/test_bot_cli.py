"""Headless CLI (run_bot_cli.py / src.live.headless*) — pure-helper tests.

No Tk root, no COM. The GUI-side contract under test is the dialog seam
carved out of ``BacktestApp._deploy_live``: the dialog result goes into a
``DeployRequest`` and everything after it lives in ``_deploy_live_from``,
which asks its questions through ``_confirm`` / ``_alert`` only.
"""

import inspect
import json
import os
from types import SimpleNamespace

import pytest

import run_backtest as rb
from src.live import headless as hl
from src.live.deploy_request import DeployRequest
from src.live.headless_app import HeadlessBotApp


# ── DeployRequest ↔ dialog tuple ──

DIALOG_TUPLE = ("night1", {"strategy": "X"}, "auto", "1500",
                True, "LongLeg", "ShortLeg", True, False, True)


def test_dialog_tuple_roundtrip_preserves_positional_order():
    req = DeployRequest.from_dialog_tuple(DIALOG_TUPLE)
    assert req.bot_name == "night1"
    assert req.resume_session == {"strategy": "X"}
    assert req.trading_mode == "auto"
    assert req.loss_limit == "1500"
    assert req.regime_enabled is True
    assert (req.regime_long, req.regime_short) == ("LongLeg", "ShortLeg")
    assert (req.news_enabled, req.news_tier2_enabled, req.news_directional) == (True, False, True)
    assert req.to_dialog_tuple() == DIALOG_TUPLE
    assert req.is_resume


def test_dialog_tuple_wrong_arity_rejected():
    with pytest.raises(ValueError):
        DeployRequest.from_dialog_tuple(DIALOG_TUPLE[:-1])


# ── GUI seam ──

def test_deploy_live_delegates_dialog_result_to_deploy_live_from():
    captured = {}
    app = SimpleNamespace(
        _quote_connected=True,
        symbol_var=SimpleNamespace(get=lambda: "TMF00"),
        status_var=SimpleNamespace(set=lambda *_: None),
        login_status_var=SimpleNamespace(set=lambda *_: None),
        _show_bot_session_dialog=lambda symbol, base_dir: DIALOG_TUPLE,
        _deploy_live_from=lambda req: captured.setdefault("req", req),
    )
    rb.BacktestApp._deploy_live(app)
    assert captured["req"] == DeployRequest.from_dialog_tuple(DIALOG_TUPLE)


def test_deploy_live_cancelled_dialog_does_not_deploy():
    called = []
    app = SimpleNamespace(
        _quote_connected=True,
        symbol_var=SimpleNamespace(get=lambda: "TMF00"),
        status_var=SimpleNamespace(set=lambda *_: None),
        login_status_var=SimpleNamespace(set=lambda *_: None),
        _show_bot_session_dialog=lambda symbol, base_dir: None,
        _deploy_live_from=lambda req: called.append(req),
    )
    rb.BacktestApp._deploy_live(app)
    assert called == []


def test_deploy_live_from_never_calls_messagebox_directly():
    """Every prompt on the headless-reachable deploy path must go through
    the _confirm/_alert seam, or the CLI would block on a hidden dialog."""
    src = inspect.getsource(rb.BacktestApp._deploy_live_from)
    assert "messagebox." not in src
    assert "self._confirm(" in src and "self._alert(" in src
    # refusals must be visible to the CLI as a False return
    assert src.count("return False") >= 4


def test_execute_backtest_range_prompt_uses_seam():
    src = inspect.getsource(rb.BacktestApp._execute_backtest)
    assert "messagebox." not in src
    assert hl.TITLE_DATA_RANGE in src


def test_seam_titles_match_gui_strings():
    src = inspect.getsource(rb.BacktestApp._deploy_live_from)
    assert hl.TITLE_STRATEGY_CHANGE in src
    assert hl.TITLE_EXISTING_POSITION in src


def test_deploy_existing_position_gates_on_nonzero_qty():
    """Issue #139: a qty=0 OI row must not fire Existing Position.

    The inventing path was ``if self._account_monitor.positions:`` — a
    truthy list of zero-qty rows. The title seam stays byte-exact.
    Distinct from #107 (ghost non-zero 持倉).
    """
    src = inspect.getsource(rb.BacktestApp._deploy_live_from)
    assert "if self._account_monitor.positions:" not in src
    assert "has_open_positions" in src
    assert hl.TITLE_EXISTING_POSITION in src
    assert src.count(hl.TITLE_EXISTING_POSITION) == 1


def test_manual_close_skips_zero_qty_before_deriving_order():
    """Issue #139 / #145: qty=0 rows and foreign products must not pick the side.

    The close decision lives in ``close_order_side`` (prefix + futures
    market). The button passes ``new_close=1`` (explicit close). A
    ``positions[0]`` read is the pre-#139 inventing path.
    """
    src = inspect.getsource(rb.BacktestApp._manual_close)
    assert "positions[0]" not in src
    assert "close_order_side" in src
    assert "new_close=1)" in src


def _manual_close_harness(symbol, *, rows=(), flat=False, last_side=None,
                          sim_size=0, sim_side=None):
    """Call ``_manual_close`` without Tk. Returns (dialogs, logs)."""
    from src.live.account_monitor import AccountMonitor, parse_open_interest

    monitor = AccountMonitor()
    if flat:
        monitor.set_flat()
    for raw in rows:
        parsed = parse_open_interest(raw)
        assert parsed is not None, raw
        monitor.add_position(parsed)
    dialogs = []
    logs = []
    side_obj = None if sim_side is None else SimpleNamespace(value=sim_side)
    app = SimpleNamespace(
        _live_runner=SimpleNamespace(
            symbol=symbol,
            broker=SimpleNamespace(position_size=sim_size, position_side=side_obj),
        ),
        _account_monitor=monitor,
        _last_real_order_side=last_side,
        _show_order_confirm_dialog=lambda *a, **k: dialogs.append((a, k)),
        _get_latest_price=lambda: (44774, "tick"),
        _live_log_msg=lambda msg, tag="status": logs.append((msg, tag)),
    )
    rb.BacktestApp._manual_close(app)
    return dialogs, logs


# Short option. A wrong pick would BUY; the futures rows below are long
# and must SELL. Same side on both rows would hide a missed market filter.
_TO_OPTION = "TO,ACCT,TX146500F6,S,1,0,500.0,15,2,ID"
_TF_TM_LONG = "TF,ACCT,TM0000,B,1,0,44774.0,15,2,ID"
_TF_TM_SHORT = "TF,ACCT,TM0000,S,1,0,44774.0,15,2,ID"
_TF_TM_ZERO = "TF,ACCT,TM0000,S,0,0,0,0,0,ID"
_TF_TX_LONG = "TF,ACCT,TXFD0,B,1,0,22500.0,15,2,ID"


def test_manual_close_ignores_foreign_option_on_flat_futures():
    """Issue #145: a lone TO option must not SELL TM futures.

    Pre-fix ``first_open_position()`` returned that row (qty != 0) and
    the dialog proposed 平倉賣 with the default ``new_close=2``, which
    opens a naked short on a flat futures book.
    """
    dialogs, logs = _manual_close_harness("TMF00", rows=(_TO_OPTION,))
    assert dialogs == []
    assert logs and "No position record" in logs[0][0]


def test_manual_close_same_prefix_option_is_not_a_tx_future():
    """Issue #145: TX options share the TX prefix. Market TO must not close TX."""
    dialogs, _logs = _manual_close_harness("TX00", rows=(_TO_OPTION,))
    assert dialogs == []


def test_manual_close_zero_qty_future_plus_option_does_not_order():
    """Issue #139 qty=0 skip must not then fall through to a TO option."""
    dialogs, _logs = _manual_close_harness(
        "TMF00", rows=(_TF_TM_ZERO, _TO_OPTION))
    assert dialogs == []


def test_manual_close_zero_qty_row_does_not_hide_a_later_future():
    """A qty=0 TM line before the real long must still SELL, new_close=1."""
    dialogs, _logs = _manual_close_harness(
        "TMF00", rows=(_TF_TM_ZERO, _TO_OPTION, _TF_TM_LONG))
    assert len(dialogs) == 1
    args, kwargs = dialogs[0]
    assert args[0] == 1
    assert kwargs.get("new_close") == 1


def test_manual_close_uses_this_product_side_and_explicit_close():
    """Matching TF long → SELL, and the close button passes new_close=1.

    Pre-fix the dialog omitted ``new_close``, so the default 2 (auto) applied.
    """
    dialogs, _logs = _manual_close_harness(
        "TMF00", rows=(_TO_OPTION, _TF_TM_LONG))
    assert len(dialogs) == 1
    args, kwargs = dialogs[0]
    assert args[0] == 1  # SELL to close a long
    assert args[1] == "TM0000"
    assert kwargs.get("action_type") == "exit"
    assert kwargs.get("new_close") == 1


def test_manual_close_short_future_buys_with_new_close_1():
    dialogs, _logs = _manual_close_harness("TMF00", rows=(_TF_TM_SHORT,))
    assert len(dialogs) == 1
    args, kwargs = dialogs[0]
    assert args[0] == 0  # BUY to close a short
    assert kwargs.get("new_close") == 1


def test_manual_close_refuses_flat_snapshot_despite_last_order_side():
    """Known-flat book must not fall through to ``_last_real_order_side``.

    Pre-fix that fallback still proposed a close, and sNewClose=2 opened
    a naked position.
    """
    dialogs, _logs = _manual_close_harness(
        "TMF00", flat=True, last_side=0, sim_size=1, sim_side="LONG")
    assert dialogs == []


def test_manual_close_unknown_snapshot_still_uses_last_side():
    """No OI answer yet: keep the last-real-order fallback, but new_close=1."""
    dialogs, _logs = _manual_close_harness("TMF00", last_side=0)
    assert len(dialogs) == 1
    args, kwargs = dialogs[0]
    assert args[0] == 1  # last order was BUY → close SELL
    assert kwargs.get("new_close") == 1


def test_manual_close_unknown_snapshot_uses_sim_side():
    dialogs, _logs = _manual_close_harness(
        "TMF00", sim_size=1, sim_side="SHORT")
    assert len(dialogs) == 1
    args, kwargs = dialogs[0]
    assert args[0] == 0  # sim short → BUY to close
    assert kwargs.get("new_close") == 1


def test_manual_close_tx_future_not_the_option_row():
    """Same prefix, both rows present: side comes from the TF future."""
    dialogs, _logs = _manual_close_harness(
        "TX00", rows=(_TO_OPTION, _TF_TX_LONG))
    assert len(dialogs) == 1
    args, kwargs = dialogs[0]
    assert args[0] == 1
    assert args[1] == "TXFD0"
    assert kwargs.get("new_close") == 1


# ── HeadlessPolicy ──

def test_policy_answers_by_title_and_declines_unknown():
    p = hl.HeadlessPolicy(use_selected_strategy=True, allow_existing_position=False)
    assert p.answer(hl.TITLE_STRATEGY_CHANGE) is True
    assert p.answer(hl.TITLE_EXISTING_POSITION) is False
    assert p.answer(hl.TITLE_DATA_RANGE) is True
    assert p.answer("確認切換 Confirm Mode Switch") is False


def test_headless_app_confirm_and_alert_use_policy_without_tk():
    app = HeadlessBotApp.__new__(HeadlessBotApp)
    app.policy = hl.HeadlessPolicy(allow_existing_position=True)
    app.alerts = []
    assert app._confirm(hl.TITLE_EXISTING_POSITION, "...") is True
    assert app._confirm(hl.TITLE_STRATEGY_CHANGE, "...") is False
    app._alert("Bot Name Conflict", "taken\nsecond line")
    assert app.alerts == [("Bot Name Conflict", "taken\nsecond line")]


# ── strategy name resolution ──

NAMES = ["1分K均線交叉 1m SMA Cross", "H4 布林多單 H4 Bollinger Long",
         "H4 布林ATR多單 H4 Bollinger ATR Long", "AI: BbandMonthShortV5"]


def test_resolve_exact_and_unique_substring():
    assert hl.resolve_strategy_name(NAMES[0], NAMES) == NAMES[0]
    assert hl.resolve_strategy_name("sma cross", NAMES) == NAMES[0]
    assert hl.resolve_strategy_name("bbandmonth", NAMES) == "AI: BbandMonthShortV5"


def test_resolve_ambiguous_and_missing():
    with pytest.raises(hl.HeadlessConfigError, match="ambiguous"):
        hl.resolve_strategy_name("Bollinger", NAMES)
    with pytest.raises(hl.HeadlessConfigError, match="not found"):
        hl.resolve_strategy_name("nope", NAMES)
    with pytest.raises(hl.HeadlessConfigError):
        hl.resolve_strategy_name("  ", NAMES)


# ── build_deploy_request ──

def _deploy_args(*extra):
    return hl.build_parser().parse_args(
        ["deploy", "--symbol", "TMF00", "--bot", "b1", *extra])


def test_build_request_paper_defaults():
    req = hl.build_deploy_request(_deploy_args("--strategy", "x"), None)
    assert req.trading_mode == "paper"
    assert req.loss_limit == "1000"
    assert req.origin == "cli"
    assert not req.regime_enabled and not req.news_enabled


def test_semi_auto_is_refused_headless():
    with pytest.raises(SystemExit):  # argparse choices
        _deploy_args("--mode", "semi_auto")


def test_regime_requires_both_legs_unless_resuming():
    with pytest.raises(hl.HeadlessConfigError, match="long-strategy"):
        hl.build_deploy_request(_deploy_args("--regime", "--long-strategy", "L"), None)
    req = hl.build_deploy_request(
        _deploy_args("--regime"), {"regime_mode": True, "long_strategy": "L"})
    assert req.regime_enabled and req.is_resume


def test_new_non_regime_bot_requires_strategy():
    with pytest.raises(hl.HeadlessConfigError, match="--strategy"):
        hl.build_deploy_request(_deploy_args(), None)
    # resuming a saved strategy is fine without --strategy
    req = hl.build_deploy_request(_deploy_args(), {"strategy": "Saved"})
    assert req.is_resume


def test_news_flags_need_regime():
    with pytest.raises(hl.HeadlessConfigError, match="--news only"):
        hl.build_deploy_request(_deploy_args("--strategy", "x", "--news"), None)
    with pytest.raises(hl.HeadlessConfigError, match="require --news"):
        hl.build_deploy_request(
            _deploy_args("--regime", "--long-strategy", "L", "--short-strategy", "S",
                         "--news-tier2"), None)
    req = hl.build_deploy_request(
        _deploy_args("--regime", "--long-strategy", "L", "--short-strategy", "S",
                     "--news", "--news-directional", "--mode", "auto",
                     "--loss-limit", "0"), None)
    assert req.news_enabled and req.news_directional and not req.news_tier2_enabled
    assert req.trading_mode == "auto" and req.loss_limit == "0"


def test_loss_limit_validation():
    with pytest.raises(hl.HeadlessConfigError):
        hl.build_deploy_request(_deploy_args("--strategy", "x", "--loss-limit", "abc"), None)
    with pytest.raises(hl.HeadlessConfigError):
        hl.build_deploy_request(_deploy_args("--strategy", "x", "--loss-limit", "-5"), None)


def test_policy_from_args_uses_selected_strategy_only_when_given():
    assert hl.policy_from_args(_deploy_args("--strategy", "x")).use_selected_strategy
    assert not hl.policy_from_args(_deploy_args()).use_selected_strategy
    assert hl.policy_from_args(_deploy_args("--allow-existing-position")).allow_existing_position


# ── STOP file ──

def test_stop_file_roundtrip(tmp_path):
    bot_dir = str(tmp_path / "TMF00_b1")
    assert not hl.stop_requested(bot_dir)
    path = hl.request_stop(bot_dir, "test")
    assert os.path.isfile(path) and hl.stop_requested(bot_dir)
    hl.clear_stop_file(bot_dir)
    assert not hl.stop_requested(bot_dir)
    hl.clear_stop_file(bot_dir)  # idempotent


# ── bot scan ──

def _write_session(bot_dir, **fields):
    os.makedirs(bot_dir, exist_ok=True)
    data = {
        "strategy": "H4 X", "trading_mode": "paper", "saved_at": "2026-09-16T10:00:00",
        "broker": {"trades": [{}, {}], "_cumulative_pnl": 1234,
                   "position_size": 1, "position_side": "LONG", "entry_price": 25000},
    }
    data.update(fields)
    with open(os.path.join(bot_dir, "session.json"), "w", encoding="utf-8") as f:
        json.dump(data, f)


def test_list_bots_reads_sessions_and_locks(tmp_path):
    base = str(tmp_path)
    _write_session(os.path.join(base, "TMF00_alpha"))
    _write_session(os.path.join(base, "TX00_beta"), regime_mode=True,
                   long_strategy="L", short_strategy="S", trading_mode="auto")
    os.makedirs(os.path.join(base, "TX00_empty"))
    os.makedirs(os.path.join(base, "junk"))
    bots = hl.list_bots(base)
    names = [(b.symbol, b.bot_name) for b in bots]
    assert names == [("TMF00", "alpha"), ("TX00", "beta"), ("TX00", "empty")]
    alpha, beta, empty = bots
    assert alpha.trades == 2 and alpha.pnl == 1234 and alpha.position == "LONG @ 25,000"
    assert not alpha.running and alpha.pid == 0
    assert beta.regime and beta.strategy == "Regime L=L S=S" and beta.trading_mode == "auto"
    assert empty.trades == 0 and empty.position == "Flat"
    assert [b.bot_name for b in hl.list_bots(base, symbol="TX00")] == ["beta", "empty"]
    table = hl.format_bot_table(bots)
    assert "alpha" in table and "+1,234" in table
    assert json.loads(hl.bots_as_json(bots))[1]["regime"] is True


def test_list_bots_missing_dir(tmp_path):
    assert hl.list_bots(str(tmp_path / "nope")) == []
    assert "no bots" in hl.format_bot_table([])


# ── parser surface ──

def test_parser_subcommands_and_dates():
    p = hl.build_parser()
    assert p.parse_args(["list", "--json"]).json
    st = p.parse_args(["status", "--symbol", "TX00", "--bot", "b"])
    assert (st.symbol, st.bot) == ("TX00", "b")
    bt = p.parse_args(["backtest", "--symbol", "TX00", "--strategy", "s",
                       "--start", "20260101", "--end", "20260301"])
    assert bt.source == "tv" and bt.timeout == 600
    assert hl.parse_yyyymmdd("20260101") == "20260101"
    with pytest.raises(hl.HeadlessConfigError):
        hl.parse_yyyymmdd("2026-01-01")


# ── detached launch ──

def test_strip_detach_args():
    argv = ["deploy", "--symbol", "TX00", "--detach", "--bot", "b",
            "--wait-ready", "30", "--strategy", "s", "--wait-ready=5"]
    assert hl.strip_detach_args(argv) == [
        "deploy", "--symbol", "TX00", "--bot", "b", "--strategy", "s"]
    assert hl.strip_detach_args(["list"]) == ["list"]


def test_parser_detach_defaults():
    a = hl.build_parser().parse_args(["deploy", "--symbol", "TX00", "--bot", "b"])
    assert a.detach is False and a.wait_ready == 180
    a = hl.build_parser().parse_args(
        ["deploy", "--symbol", "TX00", "--bot", "b", "--detach", "--wait-ready", "0"])
    assert a.detach is True and a.wait_ready == 0


def test_wait_for_ready_exited_wins(tmp_path):
    bot_dir = str(tmp_path / "TX00_b")
    out = str(tmp_path / "out.log")
    state, code = hl.wait_for_ready(lambda: 2, bot_dir, out, 5, pid=123, sleep_s=0)
    assert (state, code) == ("exited", 2)


def test_wait_for_ready_ignores_marker_from_previous_run(tmp_path):
    bot_dir = str(tmp_path / "TX00_b")
    os.makedirs(bot_dir)
    out = str(tmp_path / "out.log")
    with open(out, "w", encoding="utf-8") as f:
        f.write(f"old run: {hl.READY_MARKER}\n")
    offset = os.path.getsize(out)
    with open(os.path.join(bot_dir, ".lock"), "w") as f:
        f.write(str(os.getpid()))
    # marker only before the offset -> not ready -> times out
    state, _ = hl.wait_for_ready(lambda: None, bot_dir, out, 0.05, pid=os.getpid(),
                                 start_offset=offset, sleep_s=0.01)
    assert state == "timeout"
    with open(out, "a", encoding="utf-8") as f:
        f.write(f"[LIVE] 已訂閱 {hl.READY_MARKER} for TX00\n")
    state, _ = hl.wait_for_ready(lambda: None, bot_dir, out, 1, pid=os.getpid(),
                                 start_offset=offset, sleep_s=0.01)
    assert state == "ready"


def test_wait_for_ready_requires_lock_owned_by_child(tmp_path):
    bot_dir = str(tmp_path / "TX00_b")
    os.makedirs(bot_dir)
    out = str(tmp_path / "out.log")
    with open(out, "w", encoding="utf-8") as f:
        f.write(f"{hl.READY_MARKER}\n")
    with open(os.path.join(bot_dir, ".lock"), "w") as f:
        f.write("999999999")  # someone else's dead pid
    state, _ = hl.wait_for_ready(lambda: None, bot_dir, out, 0.05, pid=os.getpid(),
                                 sleep_s=0.01)
    assert state == "timeout"


def test_spawn_detached_runs_and_logs(tmp_path):
    import sys
    import time
    out = str(tmp_path / "logs" / "out.log")
    proc = hl.spawn_detached(
        [sys.executable, "-c", "print('child says hi', flush=True)"], out)
    for _ in range(200):
        if proc.poll() is not None:
            break
        time.sleep(0.05)
    assert proc.poll() == 0
    assert hl.tail_lines(out) == ["child says hi"]
    assert hl.tail_lines(out, start_offset=os.path.getsize(out)) == []


# ── read-only lock probe ──

def test_read_lock_never_deletes_stale_lock(tmp_path):
    """LiveRunner.check_lock deletes stale locks; list/status must not."""
    bot_dir = str(tmp_path / "TX00_b")
    os.makedirs(bot_dir)
    lock = os.path.join(bot_dir, ".lock")
    with open(lock, "w") as f:
        f.write("999999999")  # dead PID
    alive, pid = hl.read_lock(bot_dir)
    assert (alive, pid) == (False, 999999999)
    assert os.path.isfile(lock), "list/status must be read-only"
    with open(lock, "w") as f:
        f.write(str(os.getpid()))  # our own PID is alive
    assert hl.read_lock(bot_dir) == (True, os.getpid())
    assert hl.bot_info(str(tmp_path), "TX00", "b").running
    with open(lock, "w") as f:
        f.write("garbage")
    assert hl.read_lock(bot_dir) == (False, 0)
    assert os.path.isfile(lock)
