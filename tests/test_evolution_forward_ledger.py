"""Evo redesign phase 1e: the read-only forward ledger.

The ledger re-scores past verdicts on bars after their proposal time and
reports how often a PASS beat its incumbent. The integration tests use
the real engine on synthetic bars where the "right" answer is known:
prices fall before the proposal and rise after it, so a short-biased
candidate that won in-sample must LOSE forward against a long incumbent.
"""

from __future__ import annotations

import json
import os
import types
from datetime import datetime, timedelta

from src.evolution.forward_ledger import (
    SCORED,
    LedgerEntry,
    SourceResolver,
    entries_from_history,
    score_entry,
    summarize,
)
from src.evolution.history import build_evolution_record, record_evolution_attempt

_LONG = '''
from src.backtest.strategy import BacktestStrategy
from src.backtest.broker import OrderSide


class LedgerLong(BacktestStrategy):
    kline_type = 0
    kline_minute = 1

    def __init__(self, **kwargs):
        self.hold = int(kwargs.get("hold", 30))
        self._n = 0

    def required_bars(self):
        return 1

    def on_bar(self, bar, data_store, broker):
        if broker.position_size == 0:
            broker.entry("L", OrderSide.LONG)
            self._n = 0
            return
        self._n += 1
        if self._n >= self.hold:
            broker.close("L")
'''

_SHORT = _LONG.replace("LedgerLong", "LedgerLongEvo1").replace(
    "OrderSide.LONG", "OrderSide.SHORT")


# ── entries ──────────────────────────────────────────────────────────


def test_entries_keep_only_verdicts_sorted_with_knobs():
    rows = [
        build_evolution_record(ts="2026-10-10T05:06:00", bot="B", final_status="skip"),
        build_evolution_record(ts="2026-10-17T05:06:00", bot="B", final_status="fail",
                               reason_kind="grid_no_pick"),
        build_evolution_record(ts="2026-10-03T05:06:00", bot="B", final_status="pass",
                               reason_kind="validation",
                               directives={"action": "change", "knobs": {"hold": 60}}),
        build_evolution_record(ts="2026-10-24T05:06:00", bot="B",
                               final_status="no_change"),
        "not a dict",
    ]
    entries = entries_from_history(rows)
    assert [e.final_status for e in entries] == ["pass", "fail"]
    assert entries[0].knobs == {"hold": 60}
    assert entries[1].reason_kind == "grid_no_pick"


# ── scoring with fakes ───────────────────────────────────────────────


def _bar(dt):
    return types.SimpleNamespace(dt=dt)


def _t(entry_dt, pnl):
    return types.SimpleNamespace(entry_dt=entry_dt, pnl=pnl)


def _fake(trades_by_source):
    def load(source):
        return source

    def backtest(cls, bars, pv, **kwargs):
        for marker, trades in trades_by_source.items():
            if marker in cls:
                return trades
        return []
    return load, backtest


def _entry(**kw):
    base = dict(bot="B", ts="2026-10-10T05:06:00", final_status="pass",
                reason_kind="validation", incumbent_class="Inc",
                incumbent_hash="", candidate_class="", candidate_hash="")
    base.update(kw)
    return LedgerEntry(**base)


def _bars():
    return [_bar(datetime(2026, 10, 1)), _bar(datetime(2026, 11, 7, 4, 59))]


def test_forward_window_is_trades_entering_after_the_proposal():
    resolver = SourceResolver()
    inc = resolver.add("class Inc(X):\n INC\n")
    cand = resolver.add("class IncEvo1(X):\n CAND\n")
    load, backtest = _fake({
        "INC": [_t("2026-10-09 10:00", 9999), _t("2026-10-12 09:00", 100),
                _t("2026-10-20 09:00", -50)],
        "CAND": [_t("2026-10-10 05:05", 9999), _t("2026-10-13 09:00", -300)],
    })
    s = score_entry(_entry(incumbent_hash=inc, candidate_hash=cand), _bars(),
                    resolver, point_value=10, backtest=backtest, load=load)
    assert s.status == SCORED
    assert (s.inc_n, s.inc_pnl) == (2, 50)       # pre-proposal trade excluded
    assert (s.cand_n, s.cand_pnl) == (1, -300)  # 05:05 < 05:06 proposal
    assert s.delta == -350
    assert s.candidate_origin == "stored"


def test_incumbent_only_row_has_no_delta():
    resolver = SourceResolver()
    inc = resolver.add("class Inc(X):\n INC\n")
    load, backtest = _fake({"INC": [_t("2026-10-12 09:00", -700)]})
    s = score_entry(_entry(final_status="fail", reason_kind="grid_no_pick",
                           incumbent_hash=inc), _bars(), resolver,
                    point_value=10, backtest=backtest, load=load)
    assert s.status == SCORED and s.inc_pnl == -700
    assert s.delta is None and s.detail == "no candidate source"


def test_unscorable_rows_say_why():
    resolver = SourceResolver()
    load, backtest = _fake({})
    assert score_entry(_entry(incumbent_hash="0" * 64), _bars(), resolver,
                       point_value=10, backtest=backtest,
                       load=load).status == "no_incumbent_source"
    assert score_entry(_entry(ts="garbage"), _bars(), resolver, point_value=10,
                       backtest=backtest, load=load).status == "bad_ts"
    assert score_entry(_entry(), [], resolver, point_value=10,
                       backtest=backtest, load=load).status == "no_bars"
    late = _entry(ts="2026-11-08T05:06:00")
    assert score_entry(late, _bars(), resolver, point_value=10,
                       backtest=backtest, load=load).status == "no_forward_bars"


def test_knob_row_without_stored_candidate_is_rebuilt_from_incumbent():
    resolver = SourceResolver()
    inc = resolver.add(_LONG)
    seen = []

    def load(source):
        seen.append(source)
        return source

    def backtest(cls, bars, pv, **kwargs):
        pnl = 500 if "LedgerLongLedgerKnob" in cls else 100
        return [_t("2026-10-12 09:00", pnl)]

    s = score_entry(_entry(incumbent_class="LedgerLong", incumbent_hash=inc,
                           knobs={"hold": 90}), _bars(), resolver,
                    point_value=10, backtest=backtest, load=load)
    assert s.candidate_origin == "knob_rebuilt"
    assert s.delta == 400
    assert 'kwargs.get("hold", 90)' in seen[-1]


def test_summary_hit_rates_and_gate_lift():
    def s(status, delta):
        sc = types.SimpleNamespace(
            entry=types.SimpleNamespace(final_status=status), status=SCORED,
            delta=delta)
        return sc

    out = summarize([s("pass", -810), s("pass", 200), s("fail", 50),
                     s("fail", -10), s("fail", -20), s("fail", None)])
    assert out["pass"].n == 2 and out["pass"].hits == 1
    assert out["fail"].n == 3 and out["fail"].hits == 1
    assert out["incumbent_only"] == 1
    assert abs(out["gate_lift"] - (0.5 - 1 / 3)) < 1e-9
    assert summarize([])["gate_lift"] is None


# ── end to end: real engine, real files, the CLI ─────────────────────


def _write_bars(bot_dir, start: datetime, days: int, *, turn: datetime):
    """1-min bars 09:00-12:59; price falls before ``turn``, rises after."""
    price = 20000
    for d in range(days):
        day = start + timedelta(days=d)
        rows = ["datetime,open,high,low,close,volume"]
        for m in range(240):
            dt = day.replace(hour=9, minute=0) + timedelta(minutes=m)
            step = 2 if dt >= turn else -2
            o, price = price, price + step
            rows.append(f"{dt:%Y/%m/%d %H:%M},{o},{max(o, price)},"
                        f"{min(o, price)},{price},10")
        (bot_dir / f"bars_1m_{day:%Y%m%d}.csv").write_text(
            "\n".join(rows) + "\n", encoding="utf-8")


def test_cli_scores_a_pass_that_lost_forward_and_writes_nothing(tmp_path):
    from scripts.evo_forward_ledger import run

    bot = tmp_path / "TMF00_bot"
    bot.mkdir()
    turn = datetime(2026, 10, 12, 9, 0)
    _write_bars(bot, datetime(2026, 10, 5), 14, turn=turn)
    # A PASS on 10-10: the short candidate beat the long incumbent in-sample.
    assert record_evolution_attempt(
        bot, ts="2026-10-10T05:06:00", bot="LedgerLong", class_name="LedgerLong",
        source=_LONG, candidate_source=_SHORT, candidate_name="LedgerLongEvo1",
        final_status="pass", reason_kind="validation", verdict_text="PASS")
    # A FAIL the same week for a knob variant (only knobs recorded).
    assert record_evolution_attempt(
        bot, ts="2026-10-10T06:00:00", bot="LedgerLong", class_name="LedgerLong",
        source=_LONG, final_status="fail", reason_kind="validation",
        directives={"action": "change", "knobs": {"hold": 60}})
    before = sorted(os.listdir(bot))
    lines: list[str] = []
    out_json = tmp_path / "ledger.json"
    assert run([str(bot), "--json", str(out_json)], out=lines.append) == 0
    report = "\n".join(lines)
    assert "report only" in report
    assert "PASS vs incumbent forward: hit rate 0% (0/1)" in report, report
    data = json.loads(out_json.read_text(encoding="utf-8"))
    by_status = {s["entry"]["final_status"]: s for s in data["scores"]}
    p = by_status["pass"]
    assert p["status"] == SCORED and p["candidate_origin"] == "stored"
    assert p["inc_pnl"] > 0 > p["cand_pnl"] and p["delta"] < 0
    assert by_status["fail"]["candidate_origin"] == "knob_rebuilt"
    assert sorted(os.listdir(bot)) == before   # read-only


def test_cli_refuses_json_inside_a_bot_dir(tmp_path):
    from scripts.evo_forward_ledger import run

    bot = tmp_path / "bot"
    bot.mkdir()
    lines: list[str] = []
    assert run([str(bot), "--json", str(bot / "x.json")], out=lines.append) == 2
    assert not (bot / "x.json").exists()


# ── PASSes recorded only in the changelog (pre-history installs) ────


def _changelog_rows():
    from src.evolution.paper_gate import relative_eval_summary
    return [
        {"date": "2026-09-26 05:07:07", "strategy": "LedgerLong",
         "version_before": "LedgerLong", "version_after": "LedgerLongEvo1",
         "change_summary": "auto-evolution pool-validated", "initiated_by": "ai",
         "params_before": None, "params_after": None},
        {"date": "2026-09-26 05:07:08", "strategy": "LedgerLong",
         "version_before": "LedgerLong", "version_after": "LedgerLong",
         "change_summary": relative_eval_summary(True, "2026-09-26T05:07:08"),
         "initiated_by": "ai"},
        {"date": "2026-09-20 10:00:00", "strategy": "Manual",
         "version_before": "A", "version_after": "B",
         "change_summary": "hand edit", "initiated_by": "human"},
    ]


def test_changelog_pass_rows_become_entries():
    from src.evolution.forward_ledger import entries_from_changelog

    entries = entries_from_changelog(_changelog_rows())
    assert len(entries) == 1
    e = entries[0]
    assert (e.final_status, e.incumbent_class, e.candidate_class) == (
        "pass", "LedgerLong", "LedgerLongEvo1")
    assert e.reason_kind == "changelog"


def test_history_row_wins_over_the_same_changelog_pass():
    from src.evolution.forward_ledger import entries_from_changelog, merge_entries

    hist = [_entry(bot="LedgerLong", ts="2026-09-26T05:07:00",
                   candidate_class="LedgerLongEvo1")]
    merged = merge_entries(hist, entries_from_changelog(_changelog_rows()))
    assert merged == hist


def test_cli_scores_a_changelog_pass_by_class_name(tmp_path):
    from scripts.evo_forward_ledger import run

    bot = tmp_path / "TMF00_bot"
    bot.mkdir()
    _write_bars(bot, datetime(2026, 9, 21), 14, turn=datetime(2026, 9, 28, 9, 0))
    store = tmp_path / "strategies"
    store.mkdir()
    (store / "ledger_long.py").write_text(_LONG, encoding="utf-8")
    (store / "ledger_long_evo1.py").write_text(_SHORT, encoding="utf-8")
    log = tmp_path / "changelog.json"
    log.write_text(json.dumps(_changelog_rows()), encoding="utf-8")
    out_json = tmp_path / "ledger.json"
    lines: list[str] = []
    assert run([str(bot), "--changelog", str(log), "--source-dir", str(store),
                "--json", str(out_json)], out=lines.append) == 0
    data = json.loads(out_json.read_text(encoding="utf-8"))
    assert len(data["scores"]) == 1
    s = data["scores"][0]
    assert s["status"] == SCORED and s["candidate_origin"] == "by_class"
    assert s["delta"] < 0
    assert data["summary"]["pass"] == {"n": 1, "hits": 0,
                                       "total_delta": s["delta"]}


# ── review fix 1: single-TF strategies are scored on their own timeframe ──

_LONG_15M = _LONG.replace("kline_minute = 1", "kline_minute = 15").replace(
    "LedgerLong", "LedgerLong15").replace('kwargs.get("hold", 30)', 'kwargs.get("hold", 3)')


def _one_min_bars(days: int = 3):
    from src.market_data.models import Bar
    out, price = [], 20000
    for d in range(days):
        day = datetime(2026, 10, 5) + timedelta(days=d)
        for m in range(240):
            dt = day.replace(hour=9) + timedelta(minutes=m)
            step = 3 if (m // 20) % 2 == 0 else -2
            o, price = price, price + step
            out.append(Bar(symbol="TMF00", dt=dt, open=o, high=max(o, price) + 1,
                           low=min(o, price) - 1, close=price, volume=10,
                           interval=60))
    return out


def test_single_tf_15m_strategy_gets_the_same_trades_from_1min_or_15m_bars():
    from src.ai.code_sandbox import load_strategy_from_source
    from src.evolution.forward_ledger import backtest_trades
    from src.live.bar_aggregator import aggregate_bars

    cls = load_strategy_from_source(_LONG_15M)
    assert not getattr(cls(), "htf_intervals", None)
    one_min = _one_min_bars()
    fifteen = aggregate_bars(one_min, 900)

    def key(trades):
        return [(t.entry_dt, t.exit_dt, t.pnl) for t in trades]

    from_15m = backtest_trades(cls, fifteen, 10)
    from_1m = backtest_trades(cls, one_min, 10)
    assert from_15m, "fixture must trade"
    assert key(from_1m) == key(from_15m)
