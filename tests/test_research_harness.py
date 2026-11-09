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
        one_min = SimpleNamespace(kline_type=0, kline_minute=1, htf_intervals=[])
        res = bt.run(lambda: one_min, bars, 10, end="2026-11-03")
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


# ── bt.run feeds a single-timeframe strategy its own timeframe (PR #261 review) ──

def _session_1m_bars(days=6):
    """Day-session 1-min bars (08:45–13:44 TPE) with a slow wave, so a 30m SMA cross trades."""
    import math
    from datetime import timedelta
    out, i = [], 0
    day = datetime(2026, 10, 5)                     # a Monday
    while len(out) < days * 300:
        if day.weekday() < 5:
            t = day.replace(hour=8, minute=45)
            for _ in range(300):
                px = 20000 + int(150 * math.sin(i / 70.0)) + (i % 7)
                out.append(Bar(symbol="TMF00", dt=t, open=px, high=px + 3, low=px - 3,
                               close=px + 1, volume=10, interval=60))
                t += timedelta(minutes=1)
                i += 1
        day += timedelta(days=1)
    return out


def _sig(trades):
    return [(t.side.value, t.entry_dt, t.exit_dt, t.entry_price, t.exit_price) for t in trades]


class TestSingleTimeframeAggregation:
    def _cls(self):
        from src.strategy.examples.m1_sma_cross import M1SmaCrossStrategy

        class Sma30(M1SmaCrossStrategy):
            kline_minute = 30
        return Sma30

    def test_single_tf_1m_input_matches_preaggregated(self):
        from src.live.bar_aggregator import aggregate_bars
        cls = self._cls()
        bars = _session_1m_bars()
        agg = bt.run(cls, aggregate_bars(bars, 1800), 10)
        raw = bt.run(cls, bars, 10)
        assert agg.trades, "fixture must produce trades on 30m bars"
        assert _sig(raw.trades) == _sig(agg.trades)

    def test_prepare_bars_only_aggregates_lower_interval_input(self):
        cls = self._cls()
        bars = _session_1m_bars(days=1)
        assert len(bt.bars_for_strategy(cls(), bars)) == 10      # 300 min / 30
        thirty = bt.bars_for_strategy(cls(), bars)
        assert bt.bars_for_strategy(cls(), thirty) is thirty      # already at its timeframe

    def test_mtf_strategy_keeps_1m_input(self):
        cls = self._cls()

        class Mtf(cls):
            htf_intervals = [3600]
        bars = _session_1m_bars(days=1)
        assert bt.bars_for_strategy(Mtf(), bars) is bars           # engine aggregates MTF itself

    def test_non_intraday_strategy_needs_its_own_bars(self):
        cls = self._cls()

        class Daily(cls):
            kline_type = 4
        with pytest.raises(ValueError, match="daily"):
            bt.bars_for_strategy(Daily(), _session_1m_bars(days=1))


# ── bt.run(end=) cuts at the strategy timeframe, not on 1-min bars (PR #261 review 2) ──

def _day_and_night_1m_bars(first=datetime(2026, 11, 2), last=datetime(2026, 11, 6)):
    """Day (08:45–13:44) + night (15:00–04:59 next day) 1-min bars, Mon..Fri incl. Friday night."""
    import math
    from datetime import timedelta
    out, i, day = [], 0, first

    def add(t, n):
        nonlocal i
        for _ in range(n):
            px = 20000 + int(300 * math.sin(i / 400.0)) + i // 50
            out.append(Bar(symbol="TMF00", dt=t, open=px, high=px + 3, low=px - 3,
                           close=px + 1, volume=1, interval=60))
            t += timedelta(minutes=1)
            i += 1
    while day <= last:
        add(day.replace(hour=8, minute=45), 300)
        add(day.replace(hour=15, minute=0), 840)       # crosses midnight → Sat 04:59 on Friday
        day += timedelta(days=1)
    return out


class TestEndCutAtStrategyTimeframe:
    END = "2026-11-06"                                 # a Friday; its night runs into Saturday

    def _cls(self, minutes):
        from src.strategy.examples.m1_sma_cross import M1SmaCrossStrategy

        class SmaN(M1SmaCrossStrategy):
            kline_minute = minutes
        return SmaN

    def _reference(self, minutes):
        """The workbench order: bars at kline_minute, then filter_bars_by_date."""
        from src.live.bar_aggregator import aggregate_bars
        from src.market_data.kline_config import filter_bars_by_date
        bars = aggregate_bars(_day_and_night_1m_bars(), minutes * 60)
        return filter_bars_by_date(bars, "20261102", self.END.replace("-", ""))

    def test_4h_night_bucket_crossing_midnight_is_whole(self):
        cls = self._cls(240)
        got = bt.engine_bars(cls(), _day_and_night_1m_bars(), end=self.END)
        ref = self._reference(240)
        b23 = [b for b in got if b.dt == datetime(2026, 11, 6, 23, 0)]
        r23 = [b for b in ref if b.dt == datetime(2026, 11, 6, 23, 0)]
        assert r23 and b23 and (b23[0].close, b23[0].volume) == (r23[0].close, r23[0].volume)
        assert [(b.dt, b.close, b.volume) for b in got] == [(b.dt, b.close, b.volume) for b in ref]

    @pytest.mark.parametrize("minutes", [240, 120, 30])
    def test_trades_match_aggregate_then_filter(self, minutes):
        cls = self._cls(minutes)
        got = bt.run(cls, _day_and_night_1m_bars(), 10, end=self.END)
        ref = bt.run(cls, self._reference(minutes), 10)
        assert ref.trades, "fixture must trade"
        assert _sig(got.trades) == _sig(ref.trades)

    def test_mtf_end_cut_keeps_whole_primary_bucket(self):
        cls = self._cls(240)

        class Mtf(cls):
            htf_intervals = [86400]
        got = bt.engine_bars(Mtf(), _day_and_night_1m_bars(), end=self.END)
        assert got[0].interval == 60                     # engine still aggregates MTF input
        assert got[-1].dt == datetime(2026, 11, 7, 2, 59)  # 23:00 bucket kept to its end (03:00)
