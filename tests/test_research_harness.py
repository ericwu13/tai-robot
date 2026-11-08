"""scripts/research harness + src/ai/review_payload (no network, no API key)."""

import ast
import io
import json
import os
from datetime import date, datetime
from types import SimpleNamespace

import pytest

from scripts.research import bt, chat, data
from src.ai import review_payload
from src.market_data.models import Bar

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RB_PATH = os.path.join(REPO, "run_backtest.py")


def _rb_source() -> str:
    with open(RB_PATH, encoding="utf-8") as fh:
        return fh.read()


def _rb_string_constants() -> set[str]:
    tree = ast.parse(_rb_source())
    return {n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)}


def _trade(pnl, entry, exit_=None, side="LONG"):
    return SimpleNamespace(pnl=pnl, entry_dt=entry, exit_dt=exit_ or entry,
                           side=SimpleNamespace(value=side), tag="t",
                           entry_price=100, exit_price=100 + pnl,
                           entry_bar_index=0, exit_bar_index=3)


def _bar(dt, o=100, v=1):
    return Bar(symbol="TMF00", dt=dt, open=o, high=o + 1, low=o - 1, close=o, volume=v, interval=60)


# ── review_payload (shared by the GUI and the harness) ──

class TestReviewPayload:
    def test_no_degradation_on_first_review(self):
        assert review_payload.degradation_section(None, 1.0, 10.0) == ""

    def test_no_degradation_when_not_worse(self):
        prev = {"pf": 1.2, "dd_pct": 10.0}
        assert review_payload.degradation_section(prev, 1.3, 9.0) == ""

    @pytest.mark.parametrize("pf,dd,flag", [(1.1, 10.0, "Profit Factor DROPPED"),
                                            (1.2, 11.0, "Max Drawdown INCREASED")])
    def test_degradation_when_pf_drops_or_dd_grows(self, pf, dd, flag):
        sec = review_payload.degradation_section({"pf": 1.2, "dd_pct": 10.0}, pf, dd)
        assert "REVERT FIRST" in sec and flag in sec

    def test_preamble_order_rules_report_timeframe(self):
        pre = review_payload.review_preamble("REPORT", "TF\n", "DEG\n")
        assert pre.index("DEG") < pre.index("## Analysis rules (MUST FOLLOW)") < pre.index("REPORT") < pre.index("TF")
        assert "Suggest at most ONE change" in pre

    def test_timeframe_line_hourly_and_daily(self):
        hourly = SimpleNamespace(kline_type=0, kline_minute=60)
        assert "1-hour (60-minute) bars" in review_payload.review_timeframe_line(hourly)
        assert "Daily bars" in review_payload.review_timeframe_line(SimpleNamespace(kline_type=4))
        assert "TWT/UTC+8" in review_payload.review_timeframe_line(None)

    def test_trade_lines_and_context(self):
        lines = review_payload.trade_lines([_trade(50, "2026-10-01 09:00", "2026-10-01 10:00")])
        assert lines == ["  1. LONG t: entry=2026-10-01 09:00 @100 → exit=2026-10-01 10:00 @150 P&L=+50 (3 bars)"]
        ctx = review_payload.single_shot_context("PRE", lines, review_payload.source_section("x=1"), 1)
        assert ctx.startswith("PRE") and "(1 trades)" in ctx and "```python\nx=1\n```" in ctx
        assert review_payload.source_section("") == ""

    def test_gui_uses_the_shared_module(self):
        """The workbench must build its review from review_payload — no second copy."""
        src = _rb_source()
        for call in ("review_payload.review_preamble(", "review_payload.degradation_section(",
                     "review_payload.trade_lines(", "review_payload.single_shot_context(",
                     "review_payload.review_timeframe_line("):
            assert call in src, call
        assert "## Analysis rules (MUST FOLLOW)" not in src


# ── chat: codegen text must match the GUI (drift guard) ──

class TestCodegenMatchesGui:
    def test_literals_exist_in_run_backtest(self):
        consts = _rb_string_constants()
        assert chat.NO_CODE_BLOCK in consts
        assert chat.TRUNCATION_HINT in consts
        first = chat.codegen_message("SUM", "CTX")
        head = first[: first.index("SUM")]
        assert head in consts
        retry = chat.codegen_retry_message("SUM", "ERR", "CTX")
        assert retry[: retry.index("SUM")] in consts

    def test_error_wording_exists_in_run_backtest(self):
        src = _rb_source()
        assert "The generated code had errors:\\n" in src
        assert "Unexpected error loading strategy:\\n" in src
        assert "Please fix the code and output a corrected version." in src
        msg = chat.codegen_error(ValueError("boom"), truncated=True, validation=True)
        assert msg.startswith(chat.TRUNCATION_HINT) and "boom" in msg

    def test_retry_without_summary_has_no_section(self):
        assert chat.codegen_retry_message("", "ERR", "CTX") == "ERR\n\nCTX"

    def test_force_ultra_uses_ultra_model(self):
        from src.ai.chat_client import GOOGLE_MODEL_ULTRA, GOOGLE_MODEL_FLASH, PROVIDER_GOOGLE
        assert chat.tier_model(PROVIDER_GOOGLE, "light", False, True) == GOOGLE_MODEL_ULTRA
        assert chat.tier_model(PROVIDER_GOOGLE, "light", False, False) == GOOGLE_MODEL_FLASH


# ── data ──

class TestData:
    def test_merge_prefers_higher_volume_and_sorts(self):
        t1, t2 = datetime(2026, 11, 2, 9, 0), datetime(2026, 11, 2, 8, 59)
        merged = data.merge_1m([_bar(t1, 100, 5)], [_bar(t1, 101, 9), _bar(t2, 99, 1)])
        assert [b.dt for b in merged] == [t2, t1]
        assert merged[1].open == 101

    def test_dashboard_rows_decode_to_tpe_wall_clock(self):
        from scripts.dashboard.snapshot import TZ_TPE, chart_time
        tpe = datetime(2026, 11, 3, 15, 1)
        rows = [{"time": chart_time(tpe.replace(tzinfo=TZ_TPE)), "open": 1, "high": 2,
                 "low": 0, "close": 1, "volume": 3}]
        assert data.dashboard_rows_to_bars(rows, "TMF00")[0].dt == tpe

    def test_dashboard_bars_pages_backwards(self, monkeypatch):
        calls = []
        pages = {"": {"bars": [{"time": 200, "open": 2, "high": 2, "low": 2, "close": 2}],
                      "oldest_day": "20261105"},
                 "20261105": {"bars": [{"time": 100, "open": 1, "high": 1, "low": 1, "close": 1}],
                              "oldest_day": "20261020"}}

        def fake_urlopen(url, timeout):
            calls.append(url)
            before = url.split("before=")[1].split("&")[0] if "before=" in url else ""
            return io.BytesIO(json.dumps(pages[before]).encode())

        monkeypatch.setattr(data.urllib.request, "urlopen", fake_urlopen)
        bars = data.dashboard_bars("http://h:8765/", "TMF00_x", days=15)
        assert [b.open for b in bars] == [1, 2]
        assert "days=10" in calls[0] and "days=5" in calls[1] and "before=20261105" in calls[1]

    def test_tv_interval_table_covers_documented_frames(self):
        assert {"30m", "1H", "4H", "1D"} <= set(data.TV_INTERVAL_NAMES)


# ── bt ──

class TestBt:
    def test_run_matches_gui_engine_call(self, monkeypatch):
        seen = {}

        class FakeEngine:
            def __init__(self, strategy, **kw):
                seen.update(kw)

            def run(self, bars):
                seen["n"] = len(bars)
                return SimpleNamespace(trades=[], equity_curve=[], metrics=None)

        import src.backtest.engine as engine_mod
        monkeypatch.setattr(engine_mod, "BacktestEngine", FakeEngine)
        bars = [_bar(datetime(2026, 11, d, 9, 0)) for d in (2, 3, 4)]
        res = bt.run(lambda: object(), bars, 10, end="2026-11-03")
        assert seen == {"point_value": 10, "fill_mode": "next_open", "n": 2}
        # metrics recomputed with the GUI balance, like _on_backtest_done
        assert res.metrics.initial_balance == bt.GUI_INITIAL_BALANCE

    def test_stats_and_cost(self):
        tr = [_trade(100, "2026-10-01"), _trade(-50, "2026-10-02"), _trade(30, "2026-10-03")]
        s = bt.stats(tr)
        assert (s["n"], s["pnl"], round(s["pf"], 2), s["maxdd"]) == (3, 80, 2.6, 50)
        assert bt.stats(tr, cost_per_trade=10)["pnl"] == 50
        assert bt.stats([])["pf"] == 0.0

    def test_segment_folds_monthly(self):
        tr = [_trade(10, "2026-09-30 10:00"), _trade(20, "2026-10-01 10:00"), _trade(5, "2026-10-20 10:00")]
        assert len(bt.segment(tr, "2026-10-01", "2026-10-31")) == 2
        f = bt.folds(tr, date(2026, 9, 30), date(2026, 10, 20), days=14)
        assert f == [("2026-09-30", 30, 2), ("2026-10-14", 5, 1)]
        assert list(bt.monthly(tr).items()) == [("2026-09", [10, 1]), ("2026-10", [25, 2])]

    def test_point_values_come_from_symbol_config(self):
        assert (bt.POINT_VALUES["TMF00"], bt.POINT_VALUES["MTX00"], bt.POINT_VALUES["TX00"]) == (10, 50, 200)
