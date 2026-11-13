"""GUI-equivalent backtest + research statistics.

``run`` builds the engine exactly as the workbench's 回測 button does
(``run_backtest.py`` ``_execute_backtest``):
``BacktestEngine(strategy, point_value=pv, fill_mode="next_open")`` — the
engine's own default is ``on_close``, so never omit ``fill_mode``.

Bar contract: the GUI fetches bars AT the strategy's timeframe, and
``BacktestEngine`` aggregates 1-min input only for MTF strategies (non-empty
``htf_intervals``) — a single-timeframe strategy sees exactly the bars it is
given. ``run`` therefore aggregates finer intraday input to the strategy's
``kline_minute`` first (``bars_for_strategy``), so 1-min bars are safe input
for any intraday strategy. Daily/weekly/monthly strategies need bars of
their own timeframe.

Holdout discipline: pick a DESIGN window (shown to the AI / used for tuning)
and a later HOLDOUT window that is never shown to the model or used to pick
parameters. Report both; trust only the holdout.

    python -m scripts.research.bt bars.pkl strat_a.py strat_b.py \\
        --symbol TMF00 --design 2026-03-17:2026-09-15 --holdout 2026-09-16:2026-11-08
"""

from __future__ import annotations

import argparse
import collections
import pickle
import sys
from datetime import date, datetime, timedelta

from src.market_data.kline_config import SYMBOL_CONFIG

# Contract multipliers (NT$ per point), as the GUI's symbol dropdown sets them.
POINT_VALUES = {sym: cfg["pv"] for sym, cfg in SYMBOL_CONFIG.items()}


def load_strategy_file(path: str):
    """Return (strategy class, source) through the same sandbox the GUI uses."""
    from src.ai.code_sandbox import load_strategy_from_source
    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    return load_strategy_from_source(src), src


# The workbench's 初始資金 field default; metrics are recomputed with it.
GUI_INITIAL_BALANCE = 1_000_000


def run(cls, bars, point_value: int, params: dict | None = None,
        end: str | None = None, initial_balance: int = GUI_INITIAL_BALANCE):
    """Backtest ``cls`` on ``bars`` (≤ ``end`` date if given). Returns the BacktestResult.

    Same as the 回測 button: ``_execute_backtest`` builds the engine, then
    ``_on_backtest_done`` recomputes metrics with the balance field, so
    ``max_drawdown_pct`` (used by AI Review's degradation gate) is a
    percentage of that balance.
    """
    from src.backtest.engine import BacktestEngine
    from src.backtest.metrics import calculate_metrics
    strategy = cls(**(params or {}))
    engine = BacktestEngine(strategy, point_value=point_value, fill_mode="next_open")
    result = engine.run(engine_bars(strategy, bars, end=end))
    result.metrics = calculate_metrics(result.trades, result.equity_curve,
                                       initial_balance=initial_balance)
    return result


def engine_bars(strategy, bars, end: str | None = None):
    """``bars`` as the engine should get them, cut at ``end`` (YYYY-MM-DD).

    The workbench (``_execute_backtest``) applies ``filter_bars_by_date`` to
    bars already at ``kline_minute``: a bar is kept when its OPEN is on or
    before the end date. So the cut happens AFTER aggregation — cutting
    1-min bars at 23:59 first would flush the 23:00 night bucket of a 2h/4h
    strategy (which runs to 03:00) as a partial bar. For MTF strategies the
    engine aggregates the 1-min input itself, so the 1-min series is cut at
    the end of the last primary bucket that opens on or before ``end``.
    """
    from src.backtest.engine import strategy_primary_interval
    from src.live.bar_aggregator import aggregate_bars
    out = bars_for_strategy(strategy, bars)
    if not end or not out:
        return out
    stop = datetime.fromisoformat(end) + timedelta(days=1)     # first excluded open
    primary = strategy_primary_interval(strategy)
    if out[0].interval >= primary or getattr(strategy, "kline_type", 0) != 0:
        return [b for b in out if b.dt < stop]
    # MTF: 1-min input stays 1-min; find where the first excluded bucket opens.
    later = [b.dt for b in aggregate_bars(out, primary) if b.dt >= stop]
    if not later:
        return out
    cutoff = later[0]
    return [b for b in out if b.dt < cutoff]


def bars_for_strategy(strategy, bars):
    """The bars ``BacktestEngine`` should get for ``strategy`` (see module docstring).

    MTF strategies (``htf_intervals``) keep their 1-min input — the engine
    aggregates those itself. A single-timeframe intraday strategy given
    finer bars gets them aggregated to its ``kline_minute`` with the same
    session-aligned aggregator the live bots use; bars already at (or above)
    its timeframe pass through unchanged.
    """
    from src.backtest.engine import strategy_primary_interval
    from src.live.bar_aggregator import aggregate_bars
    if not bars or getattr(strategy, "htf_intervals", None):
        return bars
    if getattr(strategy, "kline_type", 0) != 0:
        if bars[0].interval < 86400:
            raise ValueError("daily/weekly/monthly strategy: pass bars of its own "
                             "timeframe (e.g. a TradingView 1D pull), not intraday bars")
        return bars
    primary = strategy_primary_interval(strategy)
    if primary <= 0 or bars[0].interval >= primary:
        return bars
    return aggregate_bars(bars, primary)


def segment(trades, start: str, end: str) -> list:
    """Trades whose ENTRY date is in [start, end] (YYYY-MM-DD, inclusive)."""
    return [t for t in trades if start <= (t.entry_dt or "")[:10] <= end]


def stats(trades, cost_per_trade: int = 0) -> dict:
    """Count, P&L (net of ``cost_per_trade`` NT$ per round trip), WR, PF, max DD."""
    pnls = [t.pnl - cost_per_trade for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    loss_sum = -sum(losses)
    pf = (sum(wins) / loss_sum) if loss_sum > 0 else (float("inf") if wins else 0.0)
    eq = peak = dd = 0
    for p in pnls:
        eq += p
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    n = len(pnls)
    return {"n": n, "pnl": sum(pnls), "wr": len(wins) / n if n else 0.0, "pf": pf,
            "maxdd": dd, "avg": sum(pnls) / n if n else 0.0,
            "long": sum(1 for t in trades if t.side.value == "LONG")}


def monthly(trades) -> "collections.OrderedDict[str, list[int]]":
    """{YYYY-MM of exit: [pnl, count]} in month order."""
    out: collections.OrderedDict[str, list[int]] = collections.OrderedDict()
    for t in sorted(trades, key=lambda t: t.exit_dt or ""):
        row = out.setdefault((t.exit_dt or "")[:7], [0, 0])
        row[0] += t.pnl
        row[1] += 1
    return out


def folds(trades, start: date, end: date, days: int = 14) -> list[tuple[str, int, int]]:
    """Consecutive ``days``-long windows from ``start`` to ``end``: (fold_start, pnl, n).

    A strategy whose P&L comes from one fold is luck; look at folds+ / worst.
    """
    out = []
    cur = start
    while cur <= end:
        stop = min(cur + timedelta(days=days - 1), end)
        f = segment(trades, str(cur), str(stop))
        out.append((str(cur), sum(t.pnl for t in f), len(f)))
        cur = stop + timedelta(days=1)
    return out


def fmt(s: dict) -> str:
    return (f"n={s['n']:3d} pnl={s['pnl']:+9,.0f} wr={s['wr']:.2f} pf={s['pf']:.2f} "
            f"maxdd={s['maxdd']:8,.0f} avg={s['avg']:+7.0f} long={s['long']}")


def _window(text: str) -> tuple[str, str]:
    a, b = text.split(":")
    date.fromisoformat(a)
    date.fromisoformat(b)
    return a, b


def report(cls, bars, point_value, design, holdout, cost=0, params=None) -> str:
    res = run(cls, bars, point_value, params=params)
    trades = res.trades
    d, h = segment(trades, *design), segment(trades, *holdout)
    lines = [f"{cls.__name__}",
             f"  DESIGN  {design[0]}..{design[1]}  {fmt(stats(d, cost))}",
             f"  HOLDOUT {holdout[0]}..{holdout[1]}  {fmt(stats(h, cost))}",
             "  monthly(gross) " + " ".join(f"{k[2:]}:{v[0]:+,}/{v[1]}" for k, v in monthly(d + h).items())]
    fl = folds(d + h, date.fromisoformat(design[0]), date.fromisoformat(holdout[1]))
    if fl:
        pos = sum(1 for _s, p, _n in fl if p > 0)
        lines.append(f"  14d folds+ {pos}/{len(fl)} worst {min(p for _s, p, _n in fl):+,} "
                     f"best {max(p for _s, p, _n in fl):+,}")
    return "\n".join(lines)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("bars", help="pickle of list[Bar] (1-min) from scripts.research.data")
    p.add_argument("strategies", nargs="+", help="strategy .py files")
    p.add_argument("--symbol", default="TMF00", choices=sorted(POINT_VALUES))
    p.add_argument("--design", type=_window, required=True, help="YYYY-MM-DD:YYYY-MM-DD")
    p.add_argument("--holdout", type=_window, required=True, help="YYYY-MM-DD:YYYY-MM-DD")
    p.add_argument("--cost", type=int, default=0,
                   help="NT$ per round trip (fees + tax + slippage), subtracted per trade")
    a = p.parse_args(argv)
    with open(a.bars, "rb") as fh:
        bars = pickle.load(fh)
    for path in a.strategies:
        cls, _src = load_strategy_file(path)
        print(report(cls, bars, POINT_VALUES[a.symbol], a.design, a.holdout, a.cost))
    return 0


if __name__ == "__main__":
    # cp950 consoles cannot print every char in the help/report text.
    sys.stdout.reconfigure(errors="replace")
    sys.exit(main())
