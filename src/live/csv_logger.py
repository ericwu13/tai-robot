"""CSV logging for live trading: raw 1-min bars and decision log.

Bar files rotate daily: data/live/{symbol}_1m_{YYYYMMDD}.csv
Decision log is append-only: data/live/{symbol}_decisions.csv
"""

from __future__ import annotations

import csv
import os
from datetime import datetime
from pathlib import Path

from ..market_data.models import Bar


class CsvLogger:
    """Logs raw 1-min bars (daily rotation) and trade decisions (append-only).

    Directory layout: ``{base_dir}/{symbol}_{bot_name}/``
    Files:
      - ``bars_1m_{YYYYMMDD}.csv``  (daily-rotated)
      - ``decisions.csv``           (append-only)
    """

    def __init__(self, base_dir: str, symbol: str, bot_name: str = ""):
        if bot_name:
            self._base_dir = os.path.join(base_dir, f"{symbol}_{bot_name}")
        else:
            self._base_dir = base_dir
        self._symbol = symbol
        self._bar_file = None
        self._bar_writer = None
        self._bar_date: str = ""
        self._decision_file = None
        self._decision_writer = None

        Path(self._base_dir).mkdir(parents=True, exist_ok=True)

    def log_bar(self, bar: Bar) -> None:
        """Write a 1-min bar to the daily CSV file. Auto-rotates on date change."""
        date_str = bar.dt.strftime("%Y%m%d")

        if date_str != self._bar_date:
            self._rotate_bar_file(date_str)

        self._bar_writer.writerow([
            bar.dt.strftime("%Y/%m/%d %H:%M"),
            bar.open, bar.high, bar.low, bar.close, bar.volume,
        ])
        self._bar_file.flush()

    def log_decision(
        self,
        dt: datetime,
        bar_dt: datetime,
        strategy: str,
        action: str,
        side: str,
        tag: str,
        price: int,
        reason: str,
    ) -> None:
        """Append a decision entry to the decision log."""
        if self._decision_writer is None:
            self._open_decision_file()

        self._decision_writer.writerow([
            dt.strftime("%Y-%m-%d %H:%M:%S"),
            bar_dt.strftime("%Y-%m-%d %H:%M"),
            strategy, action, side, tag, price, reason,
        ])
        self._decision_file.flush()

    def close(self) -> None:
        """Flush and close all file handles."""
        if self._bar_file:
            self._bar_file.close()
            self._bar_file = None
            self._bar_writer = None
            self._bar_date = ""
        if self._decision_file:
            self._decision_file.close()
            self._decision_file = None
            self._decision_writer = None

    def _rotate_bar_file(self, date_str: str) -> None:
        if self._bar_file:
            self._bar_file.close()

        filename = f"bars_1m_{date_str}.csv"
        path = os.path.join(self._base_dir, filename)
        is_new = not os.path.exists(path)

        self._bar_file = open(path, "a", newline="", encoding="utf-8")
        self._bar_writer = csv.writer(self._bar_file)
        self._bar_date = date_str

        if is_new:
            self._bar_writer.writerow(["datetime", "open", "high", "low", "close", "volume"])
            self._bar_file.flush()

    def _open_decision_file(self) -> None:
        filename = "decisions.csv"
        path = os.path.join(self._base_dir, filename)
        is_new = not os.path.exists(path)

        self._decision_file = open(path, "a", newline="", encoding="utf-8")
        self._decision_writer = csv.writer(self._decision_file)

        if is_new:
            self._decision_writer.writerow([
                "datetime", "bar_dt", "strategy", "action", "side", "tag", "price", "reason",
            ])
            self._decision_file.flush()
