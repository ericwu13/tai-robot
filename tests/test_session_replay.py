"""Session replay tests — feed real bot bars through strategies.

Loads recorded 1-min bars and strategies from session folders, replays
them through LiveRunner, and validates the NEW decisions:
1. All exit prices are valid integers (no float from ATR)
2. No duplicate TRADE_CLOSE (off-by-one _bar_index ghost)
3. Float stop/limit values don't leak into fill prices

Drop a session folder (session.json + decisions.csv + bars_1m_*.csv)
into tests/fixtures/sessions/ and it becomes a test case automatically.
"""

import csv
import json
import os
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import pytest

from src.ai.code_sandbox import load_strategy_from_source
from src.live.live_runner import LiveRunner, LiveState
from src.live.bar_aggregator import BarAggregator
from src.market_data.models import Bar


# ── Session discovery ──

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "sessions"
STRATEGIES_DIR = Path(__file__).parent.parent / "strategies"


def _discover_sessions():
    """Find all session folders with bars + strategy available."""
    if not FIXTURES_DIR.exists():
        return []
    sessions = []
    for d in sorted(FIXTURES_DIR.iterdir()):
        if not d.is_dir():
            continue
        if not (d / "session.json").exists():
            continue
        # Need at least one bars_1m CSV
        bar_files = sorted(d.glob("bars_1m_*.csv"))
        if not bar_files:
            continue
        # Check strategy is loadable
        with open(d / "session.json", encoding="utf-8") as f:
            config = json.load(f)
        strategy_name = config.get("strategy", "")
        # Strip "AI: " prefix to get class name
        class_name = strategy_name.replace("AI: ", "")
        # Convert PascalCase to snake_case for filename
        snake = ""
        for i, c in enumerate(class_name):
            if c.isupper() and i > 0:
                snake += "_"
            snake += c.lower()
        strategy_file = STRATEGIES_DIR / f"{snake}.py"
        if strategy_file.exists():
            sessions.append(d)
    return sessions


def _load_1m_bars(session_dir: Path, symbol: str) -> list[Bar]:
    """Load all 1-min bars from bars_1m_*.csv files, sorted by time."""
    bars = []
    for csv_path in sorted(session_dir.glob("bars_1m_*.csv")):
        with open(csv_path, newline="", encoding="utf-8") as f:
            reader = csv.reader(f)
            header = next(reader, None)
            for row in reader:
                if len(row) < 6:
                    continue
                try:
                    dt_str = row[0].strip()
                    for fmt in ("%Y/%m/%d %H:%M", "%Y-%m-%d %H:%M:%S",
                                "%Y-%m-%d %H:%M"):
                        try:
                            dt = datetime.strptime(dt_str, fmt)
                            break
                        except ValueError:
                            continue
                    else:
                        continue
                    bars.append(Bar(
                        symbol=symbol, dt=dt,
                        open=int(float(row[1])), high=int(float(row[2])),
                        low=int(float(row[3])), close=int(float(row[4])),
                        volume=int(float(row[5])), interval=60,
                    ))
                except (ValueError, IndexError):
                    continue
    return bars


def _load_strategy(config: dict):
    """Load strategy class from strategies/ directory."""
    strategy_name = config.get("strategy", "")
    class_name = strategy_name.replace("AI: ", "")
    snake = ""
    for i, c in enumerate(class_name):
        if c.isupper() and i > 0:
            snake += "_"
        snake += c.lower()
    strategy_file = STRATEGIES_DIR / f"{snake}.py"
    source = strategy_file.read_text(encoding="utf-8")
    return load_strategy_from_source(source)


def _aggregate_bars(bars_1m: list[Bar], interval_sec: int) -> list[Bar]:
    """Aggregate 1-min bars into target interval using BarAggregator."""
    agg = BarAggregator(bars_1m[0].symbol if bars_1m else "TX00", interval_sec)
    result = []
    for bar in bars_1m:
        completed = agg.on_bar(bar)
        if completed is not None:
            result.append(completed)
    return result


def _kline_from_bar(bar: Bar) -> str:
    """Convert a Bar to Capital API KLine string format."""
    return (f"{bar.dt.strftime('%m/%d/%Y %H:%M')},"
            f"{bar.open},{bar.high},{bar.low},{bar.close},{bar.volume}")


def _replay_session(session_dir: Path, tmp_path: Path):
    """Replay a session: feed real bars through strategy, return decisions."""
    with open(session_dir / "session.json", encoding="utf-8") as f:
        config = json.load(f)

    symbol = config["symbol"]
    point_value = config["point_value"]
    target_interval = config.get("target_interval", 3600)

    # Load strategy
    strategy_cls = _load_strategy(config)
    try:
        strategy = strategy_cls()
    except TypeError:
        strategy = strategy_cls(bb_period=20)

    # Load all 1m bars
    bars_1m = _load_1m_bars(session_dir, symbol)
    assert len(bars_1m) > 0, f"No 1m bars found in {session_dir}"

    # Split: enough warmup bars to satisfy strategy.required_bars()
    # at the target interval, rest as live feed
    required = strategy.required_bars()
    # Estimate how many 1m bars needed for N aggregated bars
    bars_per_agg = target_interval // 60
    warmup_count = min(
        required * bars_per_agg + bars_per_agg,  # required agg bars + 1 buffer
        int(len(bars_1m) * 0.9),  # max 90%
    )
    warmup_1m = bars_1m[:warmup_count]
    live_1m = bars_1m[warmup_count:]

    # Aggregate warmup bars to target interval
    warmup_agg = _aggregate_bars(warmup_1m, target_interval)
    warmup_klines = [_kline_from_bar(b) for b in warmup_agg]

    # Create runner
    runner = LiveRunner(
        strategy=strategy,
        symbol=symbol,
        point_value=point_value,
        log_dir=str(tmp_path),
    )

    # Collect decisions
    decisions = []
    runner.on("on_decision", lambda d: decisions.append(dict(d)))

    # Warmup
    runner.feed_warmup_bars(warmup_klines)
    assert runner.state == LiveState.RUNNING, "Runner didn't reach RUNNING"

    # Feed live 1m bars (batch per bar to let aggregator work)
    for bar_1m in live_1m:
        runner.feed_1m_bar(bar_1m)
        # Also test tick exits at the bar's high and low
        if runner.broker.position_size > 0:
            runner.check_tick_exit(bar_1m.high, bar_1m.dt.strftime("%Y-%m-%d %H:%M"))
            runner.check_tick_exit(bar_1m.low, bar_1m.dt.strftime("%Y-%m-%d %H:%M"))

    return decisions, runner, config


# ── Parametrize ──

_sessions = _discover_sessions()
_session_ids = [s.name for s in _sessions]


@pytest.fixture(params=_sessions, ids=_session_ids)
def replayed(request, tmp_path):
    """Replay a session and return (decisions, runner, config)."""
    return _replay_session(request.param, tmp_path)


# ── Tests: validate replayed decisions ──

class TestReplayedDecisions:
    """Validate decisions generated from replaying real production bars."""

    def test_no_float_exit_prices(self, replayed):
        """All TRADE_CLOSE prices must be integers."""
        decisions, runner, config = replayed
        float_exits = []
        for d in decisions:
            if d["action"] == "TRADE_CLOSE":
                price = d["price"]
                if isinstance(price, float) and price != int(price):
                    float_exits.append(f"{d.get('bar_dt')} price={price}")

        assert not float_exits, (
            f"Float exit prices:\n"
            + "\n".join(f"  {e}" for e in float_exits)
        )

    def test_no_duplicate_trade_close(self, replayed):
        """Each trade fires exactly one TRADE_CLOSE decision."""
        decisions, runner, config = replayed
        trade_closes = [d for d in decisions if d["action"] == "TRADE_CLOSE"]

        # Group by exit price + PnL (from reason field)
        seen = defaultdict(list)
        for d in trade_closes:
            reason = d.get("reason", "")
            pnl = reason.split("PnL=")[-1].strip() if "PnL=" in reason else ""
            key = (d.get("tag"), pnl)
            seen[key].append(str(d.get("bar_dt", "")))

        duplicates = {k: v for k, v in seen.items() if len(v) > 1}
        assert not duplicates, (
            f"Duplicate TRADE_CLOSE:\n"
            + "\n".join(
                f"  tag={k[0]} PnL={k[1]} x{len(v)}: {v}"
                for k, v in duplicates.items()
            )
        )

    def test_all_trades_have_integer_prices(self, replayed):
        """All completed trades in broker must have integer entry/exit prices."""
        decisions, runner, config = replayed
        float_trades = []
        for i, t in enumerate(runner.broker.trades):
            if t.exit_price != int(t.exit_price):
                float_trades.append(
                    f"trade[{i}] exit={t.exit_price} at {t.exit_dt}")
            if t.entry_price != int(t.entry_price):
                float_trades.append(
                    f"trade[{i}] entry={t.entry_price} at {t.entry_dt}")

        assert not float_trades, (
            f"Float prices in broker trades:\n"
            + "\n".join(f"  {t}" for t in float_trades)
        )

    def test_exit_bar_index_less_than_next_bar(self, replayed):
        """Tick exit bar_index must be < the next processed bar's index.
        This prevents the duplicate TRADE_CLOSE bug."""
        decisions, runner, config = replayed
        for t in runner.broker.trades:
            assert t.exit_bar_index < runner._bar_index, (
                f"Trade exit_bar_index={t.exit_bar_index} >= "
                f"runner._bar_index={runner._bar_index} — "
                f"would cause duplicate TRADE_CLOSE on next bar"
            )

    def test_produces_decisions(self, replayed):
        """Sanity check: replay ran without errors.

        May not produce trades if the session has fewer bars than the
        strategy's warmup requirement (live sessions get extra warmup
        from COM KLine history that isn't stored in the session folder).
        """
        decisions, runner, config = replayed
        # At minimum the runner should have processed some bars
        assert runner._bar_index > 0, "No bars were processed"
