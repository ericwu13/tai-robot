"""CLI entry point: ``python -m src.daily_report --date 2026-04-11``

Generates (or displays) a daily report. When called without --date,
defaults to today. With --show, prints an existing report instead of
generating a new one.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime

from .report_generator import load_report, list_reports


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m src.daily_report",
        description="Daily trade report pipeline for tai-robot",
    )
    parser.add_argument(
        "--date", default=_today(),
        help="Report date in YYYY-MM-DD format (default: today)",
    )
    parser.add_argument(
        "--show", action="store_true",
        help="Display an existing report instead of generating",
    )
    parser.add_argument(
        "--list", action="store_true", dest="list_reports",
        help="List all available report dates",
    )

    args = parser.parse_args()

    if args.list_reports:
        dates = list_reports()
        if not dates:
            print("No reports found.")
        else:
            print(f"Available reports ({len(dates)}):")
            for d in dates:
                print(f"  {d}")
        return

    if args.show:
        report = load_report(args.date)
        if report is None:
            print(f"No report found for {args.date}")
            sys.exit(1)
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return

    # Generate mode: load trades from live session data
    # This is a placeholder — in production, the caller (live runner or
    # backtest GUI) would invoke generate_daily_report() directly with
    # the actual trade and bar data. The CLI is mainly for --show/--list.
    report = load_report(args.date)
    if report:
        print(f"Report for {args.date} already exists. Use --show to view it.")
    else:
        print(
            f"No trade data available for {args.date}.\n"
            f"Reports are generated automatically by the backtest engine\n"
            f"or live runner. Use --show to view an existing report."
        )


if __name__ == "__main__":
    main()
