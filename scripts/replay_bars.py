"""Replay a bot's recorded 1-min CSVs through a strategy, offline.

The audit companion to the ``NEWS_SUPPRESSED`` decision rows: when the
news circuit breaker (or any gate) held a bot out of the market, this
answers "what would the strategy have done?" from the bot's own recorded
bars — the breaker earns a track record instead of silently filtering
live behavior away from the backtest.

Usage:
    python scripts/replay_bars.py --bot-dir "data/live/TMF00_mybot" \
        --strategy strategies/bband_sma_short_v3.py [--since 2026-08-18]

    --fill-mode on_close|next_open   (default on_close, matching live)
    --point-value 10                 (TMF default)

Read-only: writes nothing, orders nothing.
"""
from __future__ import annotations

import argparse
import importlib.util
import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.backtest.engine import BacktestEngine            # noqa: E402
from src.backtest.strategy import BacktestStrategy        # noqa: E402
from src.live.bar_aggregator import BarAggregator         # noqa: E402
from src.live.live_runner import (                        # noqa: E402
    _INTERVAL_SECONDS,
    load_1m_bars_from_csvs,
)


def load_strategy_class(path: str) -> type[BacktestStrategy]:
    """Import *path* and return its (single) BacktestStrategy subclass."""
    spec = importlib.util.spec_from_file_location(Path(path).stem, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    classes = [
        obj for _, obj in inspect.getmembers(mod, inspect.isclass)
        if issubclass(obj, BacktestStrategy)
        and obj is not BacktestStrategy
        and obj.__module__ == mod.__name__
    ]
    if len(classes) != 1:
        raise SystemExit(
            f"expected exactly one BacktestStrategy subclass in {path}, "
            f"found {[c.__name__ for c in classes]}")
    return classes[0]


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Replay recorded 1-min bars through a strategy (offline)")
    ap.add_argument("--bot-dir", required=True,
                    help="bot data dir holding bars_1m_*.csv")
    ap.add_argument("--strategy", required=True,
                    help="path to the strategy .py (e.g. strategies/foo.py)")
    ap.add_argument("--fill-mode", choices=["on_close", "next_open"],
                    default="on_close")
    ap.add_argument("--point-value", type=int, default=10)
    ap.add_argument("--since", default="",
                    help="only PRINT trades exiting on/after this date "
                         "(YYYY-MM-DD); warmup always uses all bars")
    args = ap.parse_args()

    cls = load_strategy_class(args.strategy)
    strategy = cls()
    interval = _INTERVAL_SECONDS.get(
        (strategy.kline_type, strategy.kline_minute), 3600)

    bars_1m = load_1m_bars_from_csvs(args.bot_dir, "REPLAY")
    if not bars_1m:
        raise SystemExit(f"no bars_1m_*.csv found in {args.bot_dir}")
    print(f"1m bars: {len(bars_1m)} ({bars_1m[0].dt} .. {bars_1m[-1].dt})")

    if interval > 60:
        agg = BarAggregator("REPLAY", interval)
        bars = [b for b in (agg.on_bar(x) for x in bars_1m) if b is not None]
    else:
        bars = bars_1m
    print(f"{cls.__name__}: interval {interval}s -> {len(bars)} bars")

    engine = BacktestEngine(strategy, point_value=args.point_value,
                            fill_mode=args.fill_mode)
    engine.run(bars)

    trades = engine.broker.trades
    shown = [t for t in trades if not args.since or t.exit_dt >= args.since]
    print(f"\nTrades: {len(trades)} total"
          + (f", {len(shown)} since {args.since}" if args.since else ""))
    for t in shown:
        side = getattr(t.side, "value", t.side)
        print(f"  {side:5s} entry {t.entry_dt} @{t.entry_price:,}"
              f"  ->  exit {t.exit_dt} @{t.exit_price:,}"
              f"  [{t.exit_tag}]  pnl={t.pnl:+,}")
    if shown:
        total = sum(t.pnl for t in shown)
        print(f"  net: {total:+,}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
