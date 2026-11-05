"""Every default daily loss limit is 20,000 NTD.

An explicit value, including 0 (limit off), is kept. The default applies
only when the caller did not supply one. A CLI ``--resume`` without
``--loss-limit`` uses the flag default (20,000) and does not rewrite the
saved session value. A GUI resume shows the saved value, including 0.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

import run_backtest as rb
from src.live import headless as hl
from src.live.deploy_request import DeployRequest
from src.live.trading_guard import (
    DEFAULT_DAILY_LOSS_LIMIT_NTD,
    TradingGuard,
)

ROOT = Path(__file__).resolve().parents[1]


def test_deploy_request_default_loss_limit_is_20000():
    assert DEFAULT_DAILY_LOSS_LIMIT_NTD == 20000
    # bot_name is required; the loss-limit field itself has the default.
    assert DeployRequest(bot_name="b").loss_limit == "20000"
    assert DeployRequest.__dataclass_fields__["loss_limit"].default == "20000"


@pytest.mark.parametrize("raw", [None, "", "   ", "abc"])
def test_parse_fallback_is_20000(raw):
    assert rb.parse_deploy_loss_limit(raw) == 20000


def test_parse_site_uses_the_helper():
    src = inspect.getsource(rb.BacktestApp._deploy_live_from)
    assert "parse_deploy_loss_limit(loss_limit_str)" in src
    assert "loss_limit = 1000" not in src


def test_explicit_zero_stays_zero_and_disables_the_limit():
    assert rb.parse_deploy_loss_limit("0") == 0
    assert rb.parse_deploy_loss_limit(0) == 0
    assert DeployRequest(bot_name="b", loss_limit="0").loss_limit == "0"
    guard = TradingGuard(daily_loss_limit=rb.parse_deploy_loss_limit("0"))
    assert guard.update_pnl(-1_000_000) is False
    assert guard.paused is False


def test_gui_prefill_uses_the_constant():
    assert rb._default_loss_limit({}) == 20000
    # An explicit settings value, including 0, is not replaced.
    assert rb._default_loss_limit({"daily_loss_limit_ntd": 0}) == 0
    assert rb._default_loss_limit({"daily_loss_limit_ntd": 7500}) == 7500
    src = inspect.getsource(rb.BacktestApp._show_bot_session_dialog)
    assert "str(_default_loss_limit(self._settings))" in src
    assert 'value="10000"' not in src
    assert 'value="1000"' not in src


@pytest.mark.parametrize("saved,expected", [(1500, "1500"), (0, "0"), ("0", "0")])
def test_gui_resume_keeps_the_saved_loss_limit(saved, expected):
    assert rb.loss_limit_for_gui_resume(
        {"daily_loss_limit": saved}, "20000") == expected


def test_gui_resume_uses_the_default_only_when_nothing_is_saved():
    assert rb.loss_limit_for_gui_resume({}, "20000") == "20000"
    assert rb.loss_limit_for_gui_resume(
        {"daily_loss_limit": None}, "20000") == "20000"
    assert rb.loss_limit_for_gui_resume(None, "20000") == "20000"
    src = inspect.getsource(rb.BacktestApp._show_bot_session_dialog)
    assert "loss_limit_for_gui_resume(sess, loss_var.get())" in src


@pytest.mark.parametrize("saved", [1500, 0])
def test_cli_resume_without_loss_limit_uses_20000(saved):
    session = {"strategy": "Saved", "daily_loss_limit": saved}
    args = hl.build_parser().parse_args(
        ["deploy", "--symbol", "TMF00", "--bot", "b1", "--resume"])
    assert args.resume is True
    assert args.loss_limit == "20000"
    req = hl.build_deploy_request(args, session)
    assert req.is_resume
    assert req.loss_limit == "20000"
    # The saved session value is not rewritten.
    assert session["daily_loss_limit"] == saved


def test_cli_resume_explicit_zero_stays_off():
    session = {"strategy": "Saved", "daily_loss_limit": 1500}
    args = hl.build_parser().parse_args(
        ["deploy", "--symbol", "TMF00", "--bot", "b1", "--resume",
         "--loss-limit", "0"])
    req = hl.build_deploy_request(args, session)
    assert req.loss_limit == "0"
    assert session["daily_loss_limit"] == 1500


def test_dashboard_placeholder_and_doc_say_20000():
    html = (ROOT / "scripts/dashboard/static/index.html").read_text(encoding="utf-8")
    assert 'placeholder: "例如 20000"' in html
    assert 'placeholder: "例如 10000"' not in html
    doc = (ROOT / "docs/ui-implementation-details.md").read_text(encoding="utf-8")
    assert 'loss-limit entry (default `"20000"`' in doc
    assert 'loss-limit entry (default `"10000"`' not in doc
