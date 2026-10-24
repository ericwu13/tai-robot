"""Key-free grid subprocess smoke. No Tk, no COM, no network, no LLM.

``tai_backtest.exe --grid-subprocess-smoke`` (and the same flag on
``python run_backtest.py``) builds synthetic bars and runs
``run_grid_isolated``. The default child is the real
``run_grid_on_bars`` path. ``--hang`` sleeps in the spawn child so a
STOP file or a short ``--timeout-s`` can be checked. ``--timeout-s``
is this process only. It is not a settings key and it does not change
the live 20-minute bound.

The only write is a STOP file path under a fresh temp directory.
Nothing reads ``settings.yaml`` and nothing is written under ``data/``.
"""

from __future__ import annotations

import argparse
import os
import pickle
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timedelta

from src.evolution.grid_process import (
    GridJob,
    GridProcessFailure,
    hang_child_entry,
    run_grid_isolated,
)
from src.market_data.models import Bar

# Small on purpose. The function is the real grid; the cap keeps a
# smoke from running the full 48-setting Saturday search.
_SMOKE_MAX_SETTINGS = 4
_DEFAULT_BARS = 120
_DEFAULT_TIMEOUT_S = 120.0
_STOP_NAME = "STOP"

_SMOKE_SOURCE = """
from src.backtest.strategy import BacktestStrategy
from src.backtest.broker import OrderSide


class DynamicExitPullbackSmoke(BacktestStrategy):
    kline_type = 0
    kline_minute = 15

    def __init__(self, **kwargs):
        self.ema_period = kwargs.get("ema_period", 50)
        self.atr_period = kwargs.get("atr_period", 14)
        self.atr_stop_mult = kwargs.get("atr_stop_mult", 2.5)
        self.atr_buffer_mult = kwargs.get("atr_buffer_mult", 0.5)

    def required_bars(self):
        return 1

    def on_bar(self, bar, data_store, broker):
        n = len(data_store.get_bars())
        slot = int(self.ema_period) % 5
        if broker.position_size == 0 and n % 6 == slot:
            broker.entry("L", OrderSide.LONG)
        elif broker.position_size > 0 and n % 6 == (slot + 3) % 6:
            broker.close("L", "x")
"""

_EXIT = {"ok": 0, "timeout": 2, "stopped": 3, "crash": 1, "usage": 1}


@dataclass(frozen=True)
class SmokeReport:
    child_pid: int | None
    elapsed_s: float
    result_bytes: int
    outcome: str
    detail: str
    stop_file: str


def sample_bars(n: int) -> tuple:
    """Synthetic daily bars. No files and no network."""
    start = datetime(2024, 1, 2, 9, 0)
    count = max(1, int(n))
    return tuple(
        Bar(
            symbol="TXF",
            dt=start + timedelta(days=i),
            open=20000,
            high=20020,
            low=19980,
            close=20000 + (i % 7),
            volume=1,
            interval=900,
        )
        for i in range(count)
    )


def _smoke_job(*, bars: int, hang: bool, timeout_s: float) -> GridJob:
    evo = {
        "search": "grid_batch",
        "holdout_days": 14,
        "relative": {"fold_days": 14},
        "trial_penalty": {"max_settings": _SMOKE_MAX_SETTINGS},
    }
    if hang:
        # Outlive the parent's timeout so the child is still asleep
        # when STOP or --timeout-s fires.
        evo["hang_seconds"] = max(3600.0, float(timeout_s) + 60.0)
        evo["mode"] = "hang"
    return GridJob(
        source=_SMOKE_SOURCE,
        class_name="DynamicExitPullbackSmoke",
        bars=sample_bars(bars),
        point_value=200,
        capital_base=100000,
        evo=evo,
        fold_days=14,
        max_folds=6,
    )


def _one_line(text: str) -> str:
    flat = " ".join((text or "").split())
    if len(flat) > 300:
        return flat[:300] + "…"
    return flat


def _summarize(result) -> tuple[str, str]:
    if isinstance(result, GridProcessFailure):
        kind = result.kind if result.kind in ("timeout", "stopped", "crash") else "crash"
        return kind, _one_line(result.detail)
    try:
        from src.evolution.grid_search import GridOutcome
    except Exception:
        GridOutcome = ()  # type: ignore[misc, assignment]
    if isinstance(result, GridOutcome):
        n = len(result.settings or ())
        selected = result.selected if result.selected else None
        return "ok", _one_line(f"settings={n} selected={selected!s}")
    return "ok", _one_line(type(result).__name__)


def run_grid_smoke(
        *,
        bars: int = _DEFAULT_BARS,
        timeout_s: float = _DEFAULT_TIMEOUT_S,
        hang: bool = False,
        in_process: bool = False,
        stop_dir: str | None = None,
        poll_s: float = 0.2,
        echo=None) -> SmokeReport:
    """Run one smoke. ``in_process=True`` is for tests. The CLI leaves it false.

    ``echo``, when set, is called with the child-pid line as soon as the
    child starts, before the wait returns.
    """
    directory = stop_dir or tempfile.mkdtemp(prefix="tai-grid-smoke-")
    os.makedirs(directory, exist_ok=True)
    stop_file = os.path.join(directory, _STOP_NAME)
    holder: dict = {"pid": None}

    def _stop() -> bool:
        return os.path.isfile(stop_file)

    def _started(pid) -> None:
        holder["pid"] = pid
        if echo is not None:
            shown = "none" if not pid else str(int(pid))
            echo(f"grid-smoke: child_pid={shown}")

    job = _smoke_job(bars=bars, hang=hang, timeout_s=timeout_s)
    started = time.monotonic()
    result = run_grid_isolated(
        job,
        timeout_s=float(timeout_s),
        stop_check=_stop,
        in_process=in_process,
        poll_s=poll_s,
        entry=hang_child_entry if hang else None,
        on_start=_started,
    )
    elapsed = time.monotonic() - started
    outcome, detail = _summarize(result)
    try:
        size = len(pickle.dumps(result, protocol=pickle.HIGHEST_PROTOCOL))
    except Exception:
        size = len(detail.encode("utf-8", errors="replace"))
    pid = holder["pid"]
    if isinstance(result, GridProcessFailure) and result.pid:
        pid = result.pid
    return SmokeReport(
        child_pid=pid,
        elapsed_s=elapsed,
        result_bytes=size,
        outcome=outcome,
        detail=detail,
        stop_file=stop_file,
    )


def _parse(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="tai_backtest --grid-subprocess-smoke",
        description=(
            "Run the grid spawn child with synthetic bars. "
            "No chat API key, no broker login, no Tk window."
        ),
    )
    parser.add_argument(
        "--bars", type=int, default=_DEFAULT_BARS,
        help=f"synthetic bars (default {_DEFAULT_BARS})",
    )
    parser.add_argument(
        "--timeout-s", type=float, default=_DEFAULT_TIMEOUT_S,
        help=(
            "seconds this smoke process waits "
            f"(default {_DEFAULT_TIMEOUT_S:g}). Not a settings key. "
            "Does not change the live 20-minute grid timeout."
        ),
    )
    parser.add_argument(
        "--hang", action="store_true",
        help="sleep in the child instead of running the grid, for STOP and timeout checks",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """CLI entry. Prints ``grid-smoke:`` lines and returns 0/1/2/3."""
    raw = list(sys.argv[1:] if argv is None else argv)
    if raw and not str(raw[0]).startswith("-"):
        raw = raw[1:]
    raw = [arg for arg in raw if arg != "--grid-subprocess-smoke"]
    try:
        args = _parse(raw)
    except SystemExit:
        # argparse uses exit status 2. Timeout also uses 2, so a bad
        # flag must not look like a smoke timeout.
        return 1
    if args.bars < 1 or args.timeout_s <= 0:
        print("grid-smoke: outcome=usage", flush=True)
        print("grid-smoke: detail=--bars must be >= 1 and --timeout-s must be > 0",
              flush=True)
        return _EXIT["usage"]

    stop_dir = tempfile.mkdtemp(prefix="tai-grid-smoke-")
    stop_file = os.path.join(stop_dir, _STOP_NAME)
    mode = "hang" if args.hang else "grid"
    print(f"grid-smoke: mode={mode}", flush=True)
    print(f"grid-smoke: stop_file={stop_file}", flush=True)
    print(f"grid-smoke: timeout_s={args.timeout_s:g}", flush=True)
    print("grid-smoke: note=timeout is this process only; not a settings key",
          flush=True)

    def _echo(line: str) -> None:
        print(line, flush=True)

    report = run_grid_smoke(
        bars=args.bars,
        timeout_s=args.timeout_s,
        hang=args.hang,
        in_process=False,
        stop_dir=stop_dir,
        echo=_echo,
    )
    print(f"grid-smoke: elapsed_s={report.elapsed_s:.3f}", flush=True)
    print(f"grid-smoke: result_bytes={report.result_bytes}", flush=True)
    print(f"grid-smoke: outcome={report.outcome}", flush=True)
    print(f"grid-smoke: detail={report.detail}", flush=True)
    return _EXIT.get(report.outcome, 1)
