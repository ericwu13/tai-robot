"""CSV trade records and rotating log files."""

from __future__ import annotations

import csv
import logging
import os
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

from ..config.settings import LoggingConfig


def setup_logging(config: LoggingConfig) -> None:
    """Configure the root logger with console and rotating file handlers."""
    log_dir = Path(config.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    level = getattr(logging, config.level.upper(), logging.INFO)

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root = logging.getLogger()
    root.setLevel(level)

    # Console handler
    console = logging.StreamHandler()
    console.setLevel(level)
    console.setFormatter(fmt)
    root.addHandler(console)

    # Rotating file handler (10MB x 5 files)
    log_file = log_dir / "tai-robot.log"
    file_handler = RotatingFileHandler(
        str(log_file), maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8",
    )
    file_handler.setLevel(level)
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)


CSV_HEADERS = [
    "timestamp", "symbol", "side", "qty", "price",
    "position_after", "realized_pnl", "reason", "mode",
]


class TradeLogger:
    """Logs every trade to a CSV file."""

    def __init__(self, csv_path: str):
        self._csv_path = csv_path
        Path(csv_path).parent.mkdir(parents=True, exist_ok=True)

        # Write header if file doesn't exist
        if not os.path.exists(csv_path):
            with open(csv_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(CSV_HEADERS)

    def log_trade(
        self,
        symbol: str,
        side: str,
        qty: int,
        price: float,
        position_after: int,
        realized_pnl: float,
        reason: str,
        mode: str,
    ) -> None:
        with open(self._csv_path, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow([
                datetime.now().isoformat(timespec="seconds"),
                symbol, side, qty, price,
                position_after, f"{realized_pnl:.1f}",
                reason, mode,
            ])
