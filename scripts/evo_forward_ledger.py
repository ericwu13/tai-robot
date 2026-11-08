"""Re-score past evolution verdicts on the bars that came after them.

Report only: nothing is gated, applied, or written into a bot directory,
and no model is called. See ``src/evolution/forward_ledger.py``.

Usage (run against a COPY of the bot dirs, or on Steven over ssh):
    python -m scripts.evo_forward_ledger <bot_dir> [<bot_dir> ...]
        [--bars-dir DIR ...]      extra 1-min bars_1m_*.csv dirs (merged)
        [--source-dir DIR ...]    extra *.py sources (e.g. the strategies/ store)
        [--pool data/evolution_pool.db]   read-only: PASS candidate sources
        [--changelog data/changelog.json ...]  PASSes from before the history file
        [--symbol TMF00 | --point-value 10]
        [--fill-mode next_open|on_close]  (default next_open = GUI/live parity)
        [--ts-offset-hours 0]     history ts → TPE (Steven runs on TPE)
        [--min-forward-days 0] [--bot NAME] [--json out.json]

Every bot dir must hold the same ``--symbol``: the 1-min bars of all of
them (plus ``--bars-dir``) are merged into one market series.

Reads ``evolution_history.jsonl``, ``evolution_sources/`` and
``bars_1m_*.csv``, all append-only or write-once files, so it never holds
a handle on a file the bot replaces. Exit code 0 even when nothing could
be scored; the report says why.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.evolution.forward_ledger import (  # noqa: E402
    SourceResolver,
    entries_from_changelog,
    entries_from_history,
    format_report,
    merge_entries,
    score_entry,
    summarize,
)
from src.evolution.history import (  # noqa: E402
    evolution_history_path,
    load_evolution_history,
)
from src.market_data.kline_config import SYMBOL_CONFIG  # noqa: E402


def pool_sources(path: str) -> list[str]:
    """Every ``source_code`` in a StrategyPool db, opened read-only."""
    if not path or not os.path.isfile(path):
        return []
    uri = Path(os.path.abspath(path)).as_uri() + "?mode=ro"
    try:
        with sqlite3.connect(uri, uri=True) as conn:
            return [row[0] for row in conn.execute(
                "SELECT source_code FROM strategies") if row[0]]
    except sqlite3.Error:
        return []


def merged_bars(dirs: list[str], symbol: str) -> list:
    from src.live.live_runner import load_1m_bars_from_csvs
    by_dt = {}
    for d in dirs:
        for bar in load_1m_bars_from_csvs(d, symbol):
            old = by_dt.get(bar.dt)
            if old is None or bar.volume > old.volume:
                by_dt[bar.dt] = bar
    return [by_dt[k] for k in sorted(by_dt)]


def _inside(path: str, dirs: list[str]) -> bool:
    target = os.path.normcase(os.path.abspath(path))
    for d in dirs:
        root = os.path.normcase(os.path.abspath(d))
        if target == root or target.startswith(root + os.sep):
            return True
    return False


def run(argv=None, out=print) -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("bot_dirs", nargs="+")
    p.add_argument("--bars-dir", action="append", default=[])
    p.add_argument("--source-dir", action="append", default=[])
    p.add_argument("--pool", default="")
    p.add_argument("--changelog", action="append", default=[])
    p.add_argument("--symbol", default="TMF00", choices=sorted(SYMBOL_CONFIG))
    p.add_argument("--point-value", type=int, default=0)
    p.add_argument("--fill-mode", default="next_open",
                   choices=("next_open", "on_close"))
    p.add_argument("--capital-base", type=int, default=100_000)
    p.add_argument("--ts-offset-hours", type=float, default=0.0)
    p.add_argument("--min-forward-days", type=int, default=0)
    p.add_argument("--bot", default="")
    p.add_argument("--json", default="")
    a = p.parse_args(argv)

    if a.json and _inside(a.json, a.bot_dirs):
        out("refusing --json inside a bot directory (this tool never writes there)")
        return 2
    pv = a.point_value or int(SYMBOL_CONFIG[a.symbol]["pv"])
    resolver = SourceResolver(
        bot_dirs=a.bot_dirs, extra_dirs=a.source_dir,
        extra_sources=pool_sources(a.pool))
    history = []
    for bot_dir in a.bot_dirs:
        history += entries_from_history(
            load_evolution_history(evolution_history_path(bot_dir)))
    changelog = []
    for path in a.changelog:
        try:
            with open(path, "r", encoding="utf-8") as handle:
                rows = json.load(handle)
        except (OSError, ValueError):
            out(f"changelog unreadable: {path}")
            continue
        changelog += entries_from_changelog(rows if isinstance(rows, list) else [])
    entries = [e for e in merge_entries(history, changelog)
               if not a.bot or e.bot == a.bot]
    bars = merged_bars([*a.bot_dirs, *a.bars_dir], a.symbol) if entries else []
    cache: dict = {}
    scores = [
        score_entry(
            entry, bars, resolver, point_value=pv, fill_mode=a.fill_mode,
            capital_base=a.capital_base, ts_offset_hours=a.ts_offset_hours,
            min_forward_days=a.min_forward_days, cache=cache)
        for entry in entries
    ]
    summary = summarize(scores)
    out(format_report(scores, summary, point_value=pv, fill_mode=a.fill_mode))
    if a.json:
        payload = {
            "fill_mode": a.fill_mode,
            "point_value": pv,
            "scores": [s.to_dict() for s in scores],
            "summary": {
                "pass": {"n": summary["pass"].n, "hits": summary["pass"].hits,
                         "total_delta": summary["pass"].total_delta},
                "fail": {"n": summary["fail"].n, "hits": summary["fail"].hits,
                         "total_delta": summary["fail"].total_delta},
                "gate_lift": summary["gate_lift"],
                "incumbent_only": summary["incumbent_only"],
                "statuses": summary["statuses"],
            },
        }
        with open(a.json, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
    return 0


if __name__ == "__main__":
    # A zh-TW Windows console is cp950; the report has Δ / − / bot names.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    raise SystemExit(run())
