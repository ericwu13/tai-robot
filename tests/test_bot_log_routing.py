"""Module loggers that must reach the bot log, not stderr only.

Reads ``run_backtest.py`` as text. Importing that module is not required:
deploy wiring runs at startup, and this check has to hold for both the
logger-name tuple and a later ``_APP_LOG_NAMES`` list.
"""

from __future__ import annotations

import importlib
from pathlib import Path

_ROUTED = (
    "quote_stuck_recovery",
    "host_stall",
    "main_heartbeat",
    "loss_baseline",
    "position_log",
    "degraded_restart",
)


def test_six_module_loggers_are_named_and_routed():
    source = (Path(__file__).resolve().parents[1] / "run_backtest.py").read_text(
        encoding="utf-8")
    missing = [name for name in _ROUTED if f'"src.live.{name}"' not in source]
    assert missing == []
    for name in _ROUTED:
        mod = importlib.import_module(f"src.live.{name}")
        assert mod.logger.name == f"src.live.{name}"
