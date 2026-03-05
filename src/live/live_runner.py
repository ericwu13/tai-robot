"""LiveRunner: orchestrates live bar processing, strategy execution, and logging.

Receives parsed KLine strings from the GUI (never touches COM directly).
Uses the same bar-processing sequence as BacktestEngine.run().

State machine: IDLE → WARMING_UP → RUNNING → STOPPED
"""

from __future__ import annotations

import os
from collections import deque
from datetime import datetime, timedelta, timezone
from enum import Enum

from ..market_data.models import Bar
from ..market_data.data_store import DataStore
from ..backtest.broker import SimulatedBroker, Trade
from ..backtest.strategy import BacktestStrategy
from ..backtest.data_loader import parse_kline_strings
from ..backtest.engine import BacktestResult
from ..backtest.metrics import calculate_metrics
from .bar_aggregator import BarAggregator, aggregate_bars
from .csv_logger import CsvLogger
from .session_store import save_session, load_session


class LiveState(Enum):
    IDLE = "IDLE"
    WARMING_UP = "WARMING_UP"
    RUNNING = "RUNNING"
    STOPPED = "STOPPED"


# Taipei timezone (UTC+8)
_TZ_TAIPEI = timezone(timedelta(hours=8))

# Taiwan futures sessions (approximate)
_AM_OPEN = (8, 45)   # 08:45
_AM_CLOSE = (13, 45)  # 13:45
_PM_OPEN = (15, 0)    # 15:00
# Night session closes at 05:00 next day


def _taipei_now() -> datetime:
    """Return current time in Taipei timezone."""
    return datetime.now(_TZ_TAIPEI)


def is_market_open(dt: datetime | None = None) -> bool:
    """Check if Taiwan futures market is open.

    Uses Taipei time (UTC+8). Closed on weekends (Sat/Sun).
    Sessions: AM 08:45-13:45, PM/Night 15:00-05:00+1
    Weekend rule: closes Sat 05:00, reopens Mon 08:45.
    """
    if dt is None:
        dt = _taipei_now()
    elif dt.tzinfo is None:
        dt = dt.replace(tzinfo=_TZ_TAIPEI)

    weekday = dt.weekday()  # Mon=0, Sun=6

    # Saturday: only night session carryover from Friday (00:00-05:00)
    if weekday == 5:  # Saturday
        h, m = dt.hour, dt.minute
        return h * 60 + m < 5 * 60

    # Sunday: fully closed
    if weekday == 6:
        return False

    h, m = dt.hour, dt.minute
    t = h * 60 + m

    am_open = _AM_OPEN[0] * 60 + _AM_OPEN[1]     # 525
    am_close = _AM_CLOSE[0] * 60 + _AM_CLOSE[1]   # 825
    pm_open = _PM_OPEN[0] * 60 + _PM_OPEN[1]      # 900
    night_close = 5 * 60                            # 300

    # Monday: no night carryover (market was closed Sun)
    if weekday == 0 and t < night_close:
        return False

    if am_open <= t < am_close:
        return True
    if t >= pm_open:
        return True
    if t < night_close:
        return True
    return False


# Map (kline_type, kline_minute) to interval in seconds
_INTERVAL_SECONDS = {
    (0, 240): 14400,
    (0, 60): 3600,
    (0, 30): 1800,
    (0, 15): 900,
    (0, 5): 300,
    (0, 1): 60,
    (4, 1): 86400,
}


class LiveRunner:
    """Orchestrates live bar processing without touching COM.

    GUI feeds it KLine strings; it aggregates, runs strategy, logs decisions.
    """

    def __init__(
        self,
        strategy: BacktestStrategy,
        symbol: str,
        point_value: int = 200,
        log_dir: str | None = None,
        bot_name: str = "",
        strategy_display_name: str = "",
    ):
        self.strategy = strategy
        self.symbol = symbol
        self.point_value = point_value
        self.bot_name = bot_name
        self.strategy_display_name = strategy_display_name or strategy.name
        self.state = LiveState.IDLE

        # Determine target interval from strategy
        kt = strategy.kline_type
        km = strategy.kline_minute
        self.target_interval = _INTERVAL_SECONDS.get((kt, km), 14400)

        # Core components
        self.broker = SimulatedBroker(point_value=point_value)
        self.data_store = DataStore(max_bars=5000)
        self.aggregator = BarAggregator(symbol, self.target_interval)

        # CSV logger — files go to data/live/{symbol}_{bot_name}/
        if log_dir is None:
            log_dir = os.path.join("data", "live")
        self.csv_logger = CsvLogger(log_dir, symbol, bot_name=bot_name)
        self.bot_dir = self.csv_logger._base_dir
        self._session_path = os.path.join(self.bot_dir, "session.json")
        self._started_at = datetime.now().isoformat(timespec="seconds")

        # Lock file for multi-instance conflict prevention
        self._lock_path = os.path.join(self.bot_dir, ".lock")

        # Tracking
        self._bar_index = 0  # running bar index for broker
        self._seen_1m_dts: set[datetime] = set()  # for dedup
        self._1m_bars: deque[Bar] = deque(maxlen=5000)  # raw 1-min bars
        self._aggregated_bars: list[Bar] = []  # all completed aggregated bars
        self._callbacks: dict[str, list] = {}
        self.suppress_strategy: bool = False  # suppress strategy during history catchup

    # ── Lock file ──

    def acquire_lock(self) -> None:
        """Write PID to lock file."""
        with open(self._lock_path, "w") as f:
            f.write(str(os.getpid()))

    def release_lock(self) -> None:
        """Remove lock file."""
        try:
            os.remove(self._lock_path)
        except OSError:
            pass

    @staticmethod
    def check_lock(bot_dir: str) -> tuple[bool, int]:
        """Check if a lock file exists and whether the owning process is alive.

        Returns (is_locked, pid).  ``is_locked`` is True only when the lock
        file exists AND the PID is still running.
        """
        lock = os.path.join(bot_dir, ".lock")
        if not os.path.isfile(lock):
            return False, 0
        try:
            with open(lock) as f:
                pid = int(f.read().strip())
        except (ValueError, OSError):
            return False, 0
        # Check if process is alive (Windows-compatible)
        try:
            os.kill(pid, 0)  # signal 0 = existence check
            return True, pid
        except OSError:
            return False, pid

    @staticmethod
    def bot_dir_for(base_dir: str, symbol: str, bot_name: str) -> str:
        """Return the bot directory path without creating it."""
        return os.path.join(base_dir, f"{symbol}_{bot_name}")

    # ── Callback system ──

    def on(self, event: str, handler) -> None:
        """Register a callback: 'on_bar', 'on_decision', 'on_status'."""
        self._callbacks.setdefault(event, []).append(handler)

    def _emit(self, event: str, *args) -> None:
        for handler in self._callbacks.get(event, []):
            try:
                handler(*args)
            except Exception:
                pass

    # ── Warmup ──

    def get_warmup_params(self) -> dict:
        """Return parameters the GUI needs to fetch warmup data.

        Returns dict with kline_type, kline_minute, days_back.
        """
        kt = self.strategy.kline_type
        km = self.strategy.kline_minute
        required = self.strategy.required_bars()

        # Estimate days needed for required bars
        if kt == 4:  # daily
            days = required * 2
        elif km >= 240:  # H4
            days = required * 2
        elif km >= 60:  # 1H
            days = max(required // 10, 30)
        else:
            days = max(required // 30, 14)

        return {
            "kline_type": kt,
            "kline_minute": km,
            "days_back": days,
            "interval": self.target_interval,
        }

    def feed_warmup_bars(self, kline_strings: list[str]) -> int:
        """Parse historical bars and seed DataStore. Returns bar count loaded."""
        self.state = LiveState.WARMING_UP

        bars = parse_kline_strings(
            kline_strings, symbol=self.symbol, interval=self.target_interval,
        )

        # No filtering needed — warmup data from COM KLine API is clean.
        # BB distortion at session gaps is handled in chart.py via gap detection.

        for bar in bars:
            self.data_store.add_bar(bar)
            self._bar_index += 1

        self._aggregated_bars.extend(bars)

        # For 1-min strategies, warmup bars are also 1-min — store for multi-TF charting
        if self.target_interval == 60:
            for bar in bars:
                self._1m_bars.append(bar)

        self.state = LiveState.RUNNING
        self._auto_save_session()  # persist immediately on start
        self._emit("on_status", f"Warmup complete: {len(bars)} bars loaded")
        return len(bars)

    # ── Live bar processing ──

    def feed_1m_bars(self, kline_strings: list[str]) -> list[Bar]:
        """Process polled 1-min bars: dedup → log CSV → aggregate → run strategy.

        Returns list of completed aggregated bars (may be empty).
        """
        if self.state != LiveState.RUNNING:
            return []

        # Parse 1-min bars
        bars_1m = parse_kline_strings(
            kline_strings, symbol=self.symbol, interval=60,
        )

        completed_agg: list[Bar] = []
        for bar in bars_1m:
            agg = self._ingest_1m_bar(bar)
            if agg is not None:
                completed_agg.append(agg)
        return completed_agg

    def feed_1m_bar(self, bar: Bar) -> Bar | None:
        """Process a single 1-min Bar object: dedup → log → aggregate → strategy.

        Used by tick-based live feed (BarBuilder produces Bar objects directly).
        Returns a completed aggregated bar if a timeframe boundary was crossed.
        """
        if self.state != LiveState.RUNNING:
            return None
        return self._ingest_1m_bar(bar)

    def _ingest_1m_bar(self, bar: Bar) -> Bar | None:
        """Internal: dedup → log CSV → aggregate → run strategy on one 1-min bar.

        Returns aggregated bar if boundary crossed, else None.
        """
        # Dedup: skip bars already seen
        if bar.dt in self._seen_1m_dts:
            return None
        self._seen_1m_dts.add(bar.dt)
        self._1m_bars.append(bar)

        # Log raw 1-min bar
        self.csv_logger.log_bar(bar)
        self._emit("on_1m_bar", bar)

        # Aggregate to target timeframe
        agg_bar = self.aggregator.on_bar(bar)
        if agg_bar is not None:
            self._process_aggregated_bar(agg_bar)
            return agg_bar
        return None

    def _process_aggregated_bar(self, bar: Bar) -> None:
        """Process a completed aggregated bar through the strategy pipeline.

        Same sequence as BacktestEngine.run() (engine.py:53-65).
        When suppress_strategy is True, only updates DataStore (no trading).
        """
        self.data_store.add_bar(bar)
        self._aggregated_bars.append(bar)
        idx = self._bar_index
        self._bar_index += 1

        # During history catchup, only build bar state — no trading
        if self.suppress_strategy:
            return

        ctx = self.broker.context

        # Check exit orders against this bar
        if idx > 0:
            self.broker.check_exits(idx, bar.open, bar.high, bar.low, bar.close)
            self._check_for_trade_close(bar, idx)

        # Run strategy if enough bars
        if len(self.data_store) >= self.strategy.required_bars():
            old_entries = len(self.broker._pending_entries)
            old_exits = len(self.broker._pending_exits)
            old_closes = len(self.broker._pending_market_closes)

            self.strategy.on_bar(bar, self.data_store, ctx)

            # Detect new entry/exit decisions
            if len(self.broker._pending_entries) > old_entries:
                for order in self.broker._pending_entries[old_entries:]:
                    self._log_decision(bar, "ENTRY", order.side.value, order.tag,
                                       bar.close, "strategy signal")
            if len(self.broker._pending_exits) > old_exits:
                for order in self.broker._pending_exits[old_exits:]:
                    price = order.limit or order.stop or bar.close
                    self._log_decision(bar, "EXIT_ORDER", order.side.value, order.tag,
                                       price, f"limit={order.limit} stop={order.stop}")
            if len(self.broker._pending_market_closes) > old_closes:
                for tag, from_entry in self.broker._pending_market_closes[old_closes:]:
                    self._log_decision(bar, "CLOSE", "", tag, bar.close, f"from={from_entry}")

        # Fill entry orders and market closes at bar close
        trades_before = len(self.broker.trades)
        self.broker.on_bar_close(idx, bar.close)
        # Check for market close trades (broker.close() processed inside on_bar_close)
        if len(self.broker.trades) > trades_before:
            self._check_for_trade_close(bar, idx)
        self._check_for_entry_fill(bar, idx)

        self._emit("on_bar", bar)

    def _check_for_trade_close(self, bar: Bar, idx: int) -> None:
        """Check if a trade was just closed by check_exits."""
        if self.broker.trades and self.broker.trades[-1].exit_bar_index == idx:
            trade = self.broker.trades[-1]
            self._log_decision(
                bar, "TRADE_CLOSE", trade.side.value, trade.exit_tag,
                trade.exit_price, f"PnL={trade.pnl:+}",
            )
            self._auto_save_session()

    def _check_for_entry_fill(self, bar: Bar, idx: int) -> None:
        """Check if an entry was just filled."""
        if self.broker.position_size > 0 and self.broker.entry_bar_index == idx:
            self._log_decision(
                bar, "ENTRY_FILL", self.broker.position_side.value,
                self.broker.entry_tag, self.broker.entry_price, "filled at bar close",
            )
            self._auto_save_session()

    def _log_decision(self, bar: Bar, action: str, side: str, tag: str,
                      price: int, reason: str) -> None:
        now = datetime.now()
        self.csv_logger.log_decision(
            dt=now, bar_dt=bar.dt, strategy=self.strategy.name,
            action=action, side=side, tag=tag, price=price, reason=reason,
        )
        self._emit("on_decision", {
            "dt": now, "bar_dt": bar.dt, "strategy": self.strategy.name,
            "action": action, "side": side, "tag": tag,
            "price": price, "reason": reason,
        })

    # ── Status & results ──

    def get_status(self) -> dict:
        """Return current status for GUI display."""
        pos = "Flat"
        if self.broker.position_size > 0:
            pos = f"{self.broker.position_side.value} @ {self.broker.entry_price:,}"

        return {
            "state": self.state.value,
            "position": pos,
            "trades": len(self.broker.trades),
            "pnl": sum(t.pnl for t in self.broker.trades),
            "bars_1m": len(self._seen_1m_dts),
            "bars_agg": len(self._aggregated_bars),
            "market_open": is_market_open(),
        }

    def get_result(self) -> BacktestResult:
        """Return a BacktestResult compatible with chart/trade display."""
        return BacktestResult(
            strategy_name=self.strategy.name,
            broker=self.broker,
            bars_processed=len(self._aggregated_bars),
        )

    def get_partial_bar(self) -> Bar | None:
        """Return a snapshot of the current in-progress aggregated bar."""
        return self.aggregator.get_partial_bar()

    def get_bars(self) -> list[Bar]:
        """Return all aggregated bars (warmup + live)."""
        return list(self._aggregated_bars)

    def get_1m_bars(self) -> list[Bar]:
        """Return snapshot of stored 1-min bars."""
        return list(self._1m_bars)

    def get_bars_at_interval(self, interval: int) -> list[Bar]:
        """Return bars at the given interval (seconds).

        If interval matches the strategy's native timeframe, returns aggregated bars.
        Otherwise, re-aggregates stored 1-min bars on demand.
        """
        if interval == self.target_interval:
            return list(self._aggregated_bars)
        return aggregate_bars(list(self._1m_bars), interval)

    def stop(self) -> dict:
        """Stop live runner: flush aggregator, force-close position, return summary."""
        if self.state == LiveState.STOPPED:
            return self._summary()

        # Flush partial aggregated bar
        partial = self.aggregator.flush()
        if partial is not None:
            self._process_aggregated_bar(partial)

        # Force close open position
        if self._aggregated_bars and self.broker.position_size > 0:
            last_bar = self._aggregated_bars[-1]
            self.broker.force_close(self._bar_index, last_bar.close)
            self._log_decision(
                last_bar, "FORCE_CLOSE", self.broker.trades[-1].side.value if self.broker.trades else "",
                "stop", last_bar.close, "live runner stopped",
            )

        self._auto_save_session()
        self.csv_logger.close()
        self.release_lock()
        self.state = LiveState.STOPPED
        self._emit("on_status", "Stopped")

        return self._summary()

    def _summary(self) -> dict:
        return {
            "trades": len(self.broker.trades),
            "pnl": sum(t.pnl for t in self.broker.trades),
            "bars_1m": len(self._seen_1m_dts),
            "bars_agg": len(self._aggregated_bars),
            "equity_curve": list(self.broker.equity_curve),
        }

    # ── Session persistence ──

    def _auto_save_session(self) -> None:
        """Save session state to disk (called on every trade event)."""
        try:
            data = {
                "strategy": self.strategy_display_name,
                "symbol": self.symbol,
                "bot_name": self.bot_name,
                "point_value": self.point_value,
                "target_interval": self.target_interval,
                "started_at": self._started_at,
                "saved_at": datetime.now().isoformat(timespec="seconds"),
                "bar_index": self._bar_index,
                "broker": self.broker.to_dict(),
            }
            save_session(self._session_path, data)
        except Exception:
            pass  # best-effort; don't crash the bot

    def restore_session(self, session_data: dict) -> int:
        """Restore broker state from a saved session.

        Call this BEFORE feed_warmup_bars(). The warmup will rebuild DataStore
        for the strategy, while the broker keeps the restored trade history.

        Returns the number of trades restored.
        """
        broker_data = session_data.get("broker", {})
        self.broker = SimulatedBroker.from_dict(broker_data)
        self._bar_index = session_data.get("bar_index", 0)
        self._started_at = session_data.get("started_at", self._started_at)
        return len(self.broker.trades)

    def reload_1m_bars(self) -> int:
        """Reload saved 1-min bar CSVs into _1m_bars and _seen_1m_dts.

        Call AFTER restore_session() and feed_warmup_bars().
        Populates the 1-min bar cache for multi-TF charting and prevents
        duplicate processing when tick history replays the same data.

        Returns the number of 1-min bars loaded.
        """
        import csv
        import glob as glob_mod

        pattern = os.path.join(self.bot_dir, "bars_1m_*.csv")
        csv_files = sorted(glob_mod.glob(pattern))
        if not csv_files:
            return 0

        count = 0
        for path in csv_files:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    reader = csv.reader(f)
                    header = next(reader, None)
                    if not header:
                        continue
                    for row in reader:
                        if len(row) < 6:
                            continue
                        try:
                            dt = datetime.strptime(row[0], "%Y/%m/%d %H:%M")
                            bar = Bar(
                                symbol=self.symbol,
                                dt=dt,
                                open=int(float(row[1])),
                                high=int(float(row[2])),
                                low=int(float(row[3])),
                                close=int(float(row[4])),
                                volume=int(float(row[5])),
                                interval=60,
                            )
                            if dt not in self._seen_1m_dts:
                                self._seen_1m_dts.add(dt)
                                self._1m_bars.append(bar)
                                count += 1
                        except (ValueError, IndexError):
                            continue
            except OSError:
                continue
        return count

    @property
    def session_path(self) -> str:
        return self._session_path
