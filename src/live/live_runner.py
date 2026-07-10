"""LiveRunner: orchestrates live bar processing, strategy execution, and logging.

Receives parsed KLine strings from the GUI (never touches COM directly).
Uses the same bar-processing sequence as BacktestEngine.run().

State machine: IDLE → WARMING_UP → RUNNING → STOPPED
"""

from __future__ import annotations

import json
import os
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Callable

from ..market_data.models import Bar
from ..market_data.data_store import DataStore
from ..backtest.broker import SimulatedBroker, Trade, _mode_to_source
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
_AM_CLOSE = (13, 45)  # 13:45 — back-month + non-settlement days
_AM_CLOSE_SETTLEMENT = (13, 30)  # front-month on settlement day (3rd Wed)
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


def seconds_until_market_open() -> int:
    """Return seconds until the next market session opens, or 0 if already open.

    Sessions: AM 08:45-13:45, PM/Night 15:00-05:00+1.
    Reconnect should be scheduled ~2 min before open to allow login time.
    """
    now = _taipei_now()
    if is_market_open(now):
        return 0

    weekday = now.weekday()
    h, m = now.hour, now.minute
    t = h * 60 + m

    am_open = _AM_OPEN[0] * 60 + _AM_OPEN[1]   # 525
    pm_open = _PM_OPEN[0] * 60 + _PM_OPEN[1]    # 900

    # Sunday: next open is Monday 08:45
    if weekday == 6:
        return (24 * 60 - t + am_open) * 60

    # Saturday after 05:00: next open is Monday 08:45
    if weekday == 5:
        return ((24 * 60 - t) + 24 * 60 + am_open) * 60

    # Weekday gaps:
    # 05:00-08:45 → next open at 08:45 (AM)
    if 5 * 60 <= t < am_open:
        return (am_open - t) * 60

    # 13:45-15:00 → next open at 15:00 (PM/Night)
    if _AM_CLOSE[0] * 60 + _AM_CLOSE[1] <= t < pm_open:
        return (pm_open - t) * 60

    # Monday 00:00-05:00 (closed, no Fri carryover)
    if weekday == 0 and t < 5 * 60:
        return (am_open - t) * 60

    # Fallback: try AM open tomorrow
    return (24 * 60 - t + am_open) * 60


def _am_close_minutes(order_symbol: str | None = None,
                      now: datetime | None = None) -> int:
    """Return the AM-session close time as minutes-since-midnight.

    Normally 13:45 (= 825). On settlement day (3rd Wed) for the
    front-month contract, returns 13:30 (= 810) — TAIFEX force-settles
    the expiring near-month at that time.

    Back-month contracts and non-settlement days keep the standard 13:45.
    """
    if order_symbol:
        # Issue #58: if holidays lookup fails (e.g. mis-bundled frozen
        # EXE), degrade to normal close time rather than raising up the
        # stack into the tick watchdog and hanging the bot.
        try:
            from ..market_data.holidays import is_settlement_day, is_front_month_contract
            if now is None:
                now = _taipei_now()
            if is_settlement_day(now) and is_front_month_contract(order_symbol, now):
                return _AM_CLOSE_SETTLEMENT[0] * 60 + _AM_CLOSE_SETTLEMENT[1]
        except Exception:
            pass
    return _AM_CLOSE[0] * 60 + _AM_CLOSE[1]


def minutes_until_session_close(order_symbol: str | None = None) -> int | None:
    """Return minutes until the current session closes, or None if market is closed.

    Sessions: AM closes 13:45 (or 13:30 for front-month on settlement
    day), Night closes 05:00.  Pass ``order_symbol`` (e.g. "TXFD6") so
    the settlement-day adjustment can be applied for the front-month
    contract.

    Saturday night carryover closes at 05:00.
    """
    now = _taipei_now()
    if not is_market_open(now):
        return None

    h, m = now.hour, now.minute
    t = h * 60 + m

    am_close = _am_close_minutes(order_symbol, now)
    night_close = 5 * 60  # 300

    # AM session (08:45-13:45 or 13:30 on settlement day)
    if am_close > t >= _AM_OPEN[0] * 60 + _AM_OPEN[1]:
        return am_close - t

    # Night session (15:00-05:00+1)
    if t >= _PM_OPEN[0] * 60 + _PM_OPEN[1]:
        # After 15:00, close is at 05:00 next day = (24*60 - t) + 300
        return (24 * 60 - t) + night_close
    if t < night_close:
        # After midnight, before 05:00
        return night_close - t

    return None


def _tick_within_age(tick_dt: datetime | None, now: datetime | None,
                     max_tick_age_s: float) -> bool:
    """True if the live tick is fresh enough to trust as a market price.

    The guard is INACTIVE (always returns True) unless BOTH ``tick_dt`` and
    ``now`` are supplied — this preserves the 3-arg ``select_freshest_price``
    call form for callers that don't track a tick timestamp.

    A mixed naive/aware pair is normalized to naive before subtracting,
    mirroring how run_backtest's ``_on_com_tick`` computes ``tick_age`` (the
    tick dt is naive Taipei; ``_taipei_now()`` is aware Taipei). A tick dated
    in the future (negative age, e.g. minor clock skew) counts as fresh.
    """
    if tick_dt is None or now is None:
        return True
    now_naive = now.replace(tzinfo=None) if now.tzinfo else now
    tick_naive = tick_dt.replace(tzinfo=None) if tick_dt.tzinfo else tick_dt
    return (now_naive - tick_naive).total_seconds() <= max_tick_age_s


def select_freshest_price(tick_price: int, bars_1m, agg_bars, *,
                          tick_dt: datetime | None = None,
                          now: datetime | None = None,
                          max_tick_age_s: float = 120) -> tuple[int, str]:
    """Pick the freshest available market price and its source label.

    Freshness priority (strictly monotonic — newest first):
      1. live tick price (now)                  -> ``(tick_price, "即時 tick")``
      2. last CLOSED 1-min bar close (<=~1min old) -> ``(close, "1m bar")``
      3. last aggregated strategy-TF bar close (can be many minutes old)
                                                -> ``(close, "agg bar")``
      4. nothing available                      -> ``(0, "N/A")``

    ``bars_1m`` / ``agg_bars`` may be any sequence (``list`` or the
    ``deque`` that LiveRunner uses) — only truthiness and ``[-1].close``
    are touched, so both work.

    The agg-bar branch is the DOCUMENTED LAST RESORT for the startup edge
    (no live tick yet AND no closed 1-min bar). It is the stale value
    behind issue #61 — the session-end / stop force-close used to read it
    at an arbitrary wall-clock moment unrelated to any bar boundary, so it
    could be many minutes stale (incident: agg=45,879 vs true fill 45,778,
    ~101pt gap). Everything above it is fresher. A nonzero ``tick_price``
    always wins (truthiness check mirrors the original _get_latest_price);
    a tick that scaled to 0 is treated as "no tick".

    STALE-TICK GUARD (issue #61 follow-up): pass ``tick_dt`` (the tick's
    parsed datetime) and ``now`` (current Taipei time) to bound tick
    staleness. When the stored tick is older than ``max_tick_age_s`` (default
    120s, matching the live tick-watchdog), the tick rung is SKIPPED and the
    selection falls through to the fresher-by-construction 1-min bar close (a
    closed 1-min bar is at most ~1 min old). This catches a silent COM feed
    stall (disconnect / zombie subscription) where ``_live_last_tick_price``
    stays nonzero but is minutes stale. The guard is INACTIVE unless BOTH
    ``tick_dt`` and ``now`` are supplied, so the 3-arg call form is unchanged.
    """
    if tick_price and _tick_within_age(tick_dt, now, max_tick_age_s):
        return tick_price, "即時 tick"
    if bars_1m:
        return bars_1m[-1].close, "1m bar"
    if agg_bars:
        return agg_bars[-1].close, "agg bar"
    return 0, "N/A"


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


def load_1m_bars_from_csvs(bot_dir: str, symbol: str) -> list[Bar]:
    """Load ALL saved 1-min bars from a bot directory's daily CSVs.

    Unlike ``reload_1m_bars`` (which feeds the live in-memory caches and
    dedups against the session's seen-set), this is a pure reader used by
    the evolution pipeline to reconstruct the FULL session history — the
    in-memory deque only holds the last 5000 bars (~3.5 days), while the
    daily CSVs retain everything since the bot's first deploy.

    Returns bars sorted by dt; duplicate timestamps resolved by
    last-file-wins. Garbage rows and unreadable files are skipped.
    """
    import csv as csv_mod
    import glob as glob_mod

    out: dict[datetime, Bar] = {}
    for path in sorted(glob_mod.glob(os.path.join(bot_dir, "bars_1m_*.csv"))):
        try:
            with open(path, "r", encoding="utf-8") as f:
                reader = csv_mod.reader(f)
                next(reader, None)  # header
                for row in reader:
                    if len(row) < 6:
                        continue
                    try:
                        dt = datetime.strptime(row[0], "%Y/%m/%d %H:%M")
                        out[dt] = Bar(
                            symbol=symbol, dt=dt,
                            open=int(float(row[1])), high=int(float(row[2])),
                            low=int(float(row[3])), close=int(float(row[4])),
                            volume=int(float(row[5])), interval=60,
                        )
                    except (ValueError, IndexError):
                        continue
        except OSError:
            continue
    return [out[k] for k in sorted(out)]


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

        # MTF: opt-in higher-timeframe aggregators driven by completed
        # primary bars. Empty for single-TF strategies — zero-cost when
        # unused, no behaviour change for existing strategies.
        self._htf_intervals: list[int] = list(
            getattr(strategy, "htf_intervals", []) or []
        )
        self._htf_aggregators: dict[int, BarAggregator] = {}
        self._htf_required: dict[int, int] = {}
        if self._htf_intervals:
            from ..backtest.engine import validate_htf_intervals
            validate_htf_intervals(self.target_interval, self._htf_intervals)
            for iv in self._htf_intervals:
                self.data_store._register_htf(iv)
                self._htf_aggregators[iv] = BarAggregator(symbol, iv)
            self._htf_required = strategy.htf_required_bars() or {}

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
        self._warmup_bar_count: int = 0  # count of warmup bars in _aggregated_bars
        self._callbacks: dict[str, list] = {}
        self.suppress_strategy: bool = False  # suppress strategy during history catchup
        # Issue #79: True from warmup start until history replay completes.
        # While set, _check_mode_override() is skipped so a stale
        # mode_override.json (or a race with the resume UI sync) can't flip
        # the trading mode mid-replay and blank the mode tag. Backed by the
        # _is_reloading property, which stamps _reload_started_at on each
        # False→True flip so a window that never closes can be auto-cleared.
        self._reloading_flag: bool = False
        self._reload_started_at: float | None = None
        self.trading_mode: str = "paper"  # "paper", "semi_auto", or "auto"
        self.broker.trade_source = _mode_to_source(self.trading_mode)
        self.broker.strategy_label = self.strategy_display_name
        self.daily_loss_limit: int = 10000  # NTD, for session persistence

        # Hot-swap mode switching (Feature 2)
        self._allow_live_override: bool = False
        self.on_mode_changed: Callable[[str, str], None] | None = None
        # Optional external veto: returns a reason string to block the
        # switch, or None to allow it. The GUI wires this to the
        # TradingGuard so a switch can't happen while a real order is
        # in flight (fill_pending) — LiveRunner itself only sees the
        # simulated position, which is already flat once an exit order
        # has been sent.
        self.mode_switch_veto: Callable[[], str | None] | None = None

        # Regime switching: when True, strategy.on_bar is skipped but
        # DataStore/HTF/bar_index/CSV logging all continue. Distinct
        # from suppress_strategy (cleared on every reconnect replay).
        self.regime_idle: bool = False

        # Daily-report dedupe key: (date_str, "DAY"|"NIGHT") of the last
        # session for which a report was emitted. Prevents the 30s
        # session-end poll from re-firing within the same close window
        # and prevents a manual stop right after auto-fire from
        # producing a duplicate report.
        self._last_report_session: tuple[str, str] | None = None

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

    # ── Hot-swap mode switching ──

    # Safety cap on the reloading window (issue #79 review). If the
    # history→live transition never fires (e.g. the tick feed died), the
    # window would otherwise stay open forever and wedge hot-swap.
    RELOAD_TIMEOUT_SECONDS = 600  # 10 minutes

    @property
    def _is_reloading(self) -> bool:
        return self._reloading_flag

    @_is_reloading.setter
    def _is_reloading(self, value: bool) -> None:
        value = bool(value)
        if value and not self._reloading_flag:
            # False→True: stamp the start for the safety timeout.
            self._reload_started_at = time.monotonic()
        elif not value:
            self._reload_started_at = None
        self._reloading_flag = value

    def _check_mode_override(self) -> str | None:
        """Read and consume a mode_override.json file from the bot directory."""
        # Issue #79: never consume/apply a mode override during warmup or
        # history replay. Applying one mid-replay races with the resume UI
        # sync and makes the mode tag disappear until the user manually
        # switches modes. Live hot-swaps resume once _is_reloading clears.
        if self._is_reloading:
            # Safety timeout (issue #79 review): auto-clear a reloading window
            # that has been open too long so hot-swap isn't wedged forever.
            if (self._reload_started_at is not None
                    and time.monotonic() - self._reload_started_at
                    > self.RELOAD_TIMEOUT_SECONDS):
                self._emit(
                    "on_status",
                    "[RESUME] _is_reloading stuck > 10 min — auto-clearing "
                    "reload window")
                self._is_reloading = False
                # fall through and process the override normally below
            else:
                # Delete (not just skip) any mode_override.json queued during
                # the reload window (issue #79 review). Leaving it in place lets
                # a stale override apply the instant the window clears — possibly
                # flipping the bot to auto with no human in the loop.
                override_path = os.path.join(self.bot_dir, "mode_override.json")
                try:
                    if os.path.exists(override_path):
                        os.remove(override_path)
                        self._emit(
                            "on_status",
                            "[RESUME] Deleted stale mode_override.json during reload")
                except OSError:
                    pass
                return None
        override_path = os.path.join(self.bot_dir, "mode_override.json")
        try:
            if not os.path.exists(override_path):
                return None
            with open(override_path, "r") as f:
                data = json.load(f)
            os.remove(override_path)
            return data.get("trading_mode")
        except (json.JSONDecodeError, OSError):
            return None

    def _apply_mode_switch(self, new_mode: str, *, user_confirmed: bool = False) -> None:
        """Switch trading mode if safe to do so.

        ``user_confirmed=True`` means a human approved this switch via a
        GUI confirmation dialog — that bypasses the allow_live_override
        gate, which exists to stop UNATTENDED switches to auto (a dropped
        mode_override.json file has no human in the loop).
        """
        if new_mode == self.trading_mode:
            return
        if new_mode not in ("paper", "semi_auto", "auto"):
            self._emit("on_status", f"[MODE] rejected invalid mode: {new_mode!r}")
            return
        if new_mode == "auto" and not self._allow_live_override and not user_confirmed:
            self._emit("on_status", "[MODE] rejected auto override — allow_live_override=false in settings")
            return
        if self.broker.has_open_position():
            self._emit("on_status",
                        f"[MODE] cannot switch {self.trading_mode}→{new_mode}: open position exists, close it first")
            return
        if self.mode_switch_veto is not None:
            reason = self.mode_switch_veto()
            if reason:
                self._emit("on_status",
                            f"[MODE] cannot switch {self.trading_mode}→{new_mode}: {reason}")
                return
        old = self.trading_mode
        self.trading_mode = new_mode
        self.broker.trade_source = _mode_to_source(new_mode)
        self._emit("on_status", f"[MODE] switched {old} → {new_mode}")
        if self.on_mode_changed:
            self.on_mode_changed(old, new_mode)
        self._auto_save_session()

    # ── Strategy swap (regime switching) ──

    def swap_strategy(
        self, new_strategy: BacktestStrategy, display_name: str
    ) -> tuple[bool, str]:
        """Replace the active strategy in-place. Returns (ok, reason).

        Refuses unless the broker is flat, no external veto, and not in
        replay. A leg on a DIFFERENT timeframe is supported: the primary
        aggregator + DataStore are rebuilt from accumulated 1-min history
        (see ``_rebuild_timeframe``) before the swap commits.
        """
        # Gate 1: flat
        if self.broker.has_open_position():
            return False, "open position exists"
        # Gate 2: external veto (fill_pending / poller active)
        if self.mode_switch_veto is not None:
            reason = self.mode_switch_veto()
            if reason:
                return False, reason
        # Gate 3: not in replay
        if self.suppress_strategy or self._is_reloading:
            return False, "in replay/reload window"
        # Gate 4: timeframe. Same TF proceeds directly. A different TF
        # triggers a swap-time aggregator rebuild from the accumulated
        # 1-min history; if there is not yet enough of it, refuse the swap
        # so the pending recommendation is retried on the next poll.
        new_kt = new_strategy.kline_type
        new_km = new_strategy.kline_minute
        new_interval = _INTERVAL_SECONDS.get((new_kt, new_km), 14400)
        if (new_kt, new_km) != (self.strategy.kline_type, self.strategy.kline_minute):
            if not self._rebuild_timeframe(new_interval, new_strategy):
                return False, (
                    f"insufficient 1-min history to rebuild "
                    f"({new_kt},{new_km}) timeframe")
        # Gate 5: enough bars (already guaranteed when a rebuild happened)
        if new_strategy.required_bars() > len(self.data_store):
            return False, f"insufficient bars: need {new_strategy.required_bars()}, have {len(self.data_store)}"

        # Atomic swap
        self.strategy = new_strategy
        self.strategy_display_name = display_name
        self.broker.strategy_label = display_name
        # Clear stale orders from the outgoing strategy
        self.broker._pending_entries.clear()
        self.broker._pending_exits.clear()
        self.broker._pending_market_closes.clear()
        # Ensure HTF intervals are registered
        new_htf = list(getattr(new_strategy, "htf_intervals", []) or [])
        for iv in new_htf:
            if iv not in self._htf_aggregators:
                self.data_store._register_htf(iv)
                agg = BarAggregator(self.symbol, iv)
                self._htf_aggregators[iv] = agg
                for b in self._aggregated_bars:
                    completed = agg.on_bar(b)
                    if completed is not None:
                        self.data_store._add_htf_bar(iv, completed)
        self._htf_intervals = new_htf
        self._htf_required = new_strategy.htf_required_bars() or {}

        self._auto_save_session()
        self._emit("on_status", f"[SWAP] strategy → {display_name}")
        return True, ""

    def _rebuild_timeframe(
        self, new_interval: int, new_strategy: BacktestStrategy
    ) -> bool:
        """Rebuild the primary aggregator + DataStore for a new target interval.

        Called by ``swap_strategy`` when the incoming leg runs on a
        different timeframe than the current one. Re-aggregates the FULL
        1-min history — persisted ``bars_1m_*.csv`` (via
        ``load_1m_bars_from_csvs``) merged with the in-memory ``_1m_bars``
        deque, deduped and sorted — to ``new_interval``.

        The trailing partial bar is deliberately kept INSIDE the fresh
        aggregator as the in-progress bar (NOT flushed into the store), so
        the next live 1-min bar continues it instead of double-counting.

        Sufficiency is checked BEFORE any state is mutated: if the rebuilt
        bar count is below the new strategy's ``required_bars()`` the method
        logs a warning and returns ``False`` without touching the runner —
        the caller aborts the swap and the pending recommendation is
        retried later, once more 1-min history has accumulated.

        On success the new aggregator, ``target_interval``, ``data_store``,
        ``_aggregated_bars``, ``_warmup_bar_count`` and HTF aggregators are
        committed atomically and ``True`` is returned. ``_bar_index`` is
        left untouched so existing broker trade indices stay valid.
        """
        # 1. Gather full 1-min history: persisted CSVs + in-memory deque,
        #    deduped by timestamp (in-memory wins) and sorted ascending.
        bars_1m_map: dict[datetime, Bar] = {}
        for b in load_1m_bars_from_csvs(self.bot_dir, self.symbol):
            bars_1m_map[b.dt] = b
        for b in self._1m_bars:
            bars_1m_map[b.dt] = b
        bars_1m = [bars_1m_map[k] for k in sorted(bars_1m_map)]

        # 2. Re-aggregate to the new interval. Feed each 1-min bar through a
        #    fresh aggregator: completed bars are collected, the trailing
        #    partial stays inside `new_agg` (for interval==60 every bar is a
        #    pass-through and there is no partial).
        new_agg = BarAggregator(self.symbol, new_interval)
        completed: list[Bar] = []
        for b in bars_1m:
            done = new_agg.on_bar(b)
            if done is not None:
                completed.append(done)

        # 3. Sufficiency check BEFORE committing anything.
        required = new_strategy.required_bars()
        if len(completed) < required:
            self._emit(
                "on_status",
                f"[SWAP] rebuild to {new_interval}s refused: "
                f"{len(completed)} bars < required {required} — "
                f"need more accumulated 1-min history")
            return False

        # 4. Validate the new strategy's HTF intervals against the new
        #    primary interval before mutating anything.
        new_htf = list(getattr(new_strategy, "htf_intervals", []) or [])
        if new_htf:
            from ..backtest.engine import validate_htf_intervals
            validate_htf_intervals(new_interval, new_htf)

        # 5. Build the replacement DataStore + HTF aggregators off the
        #    completed bars (HTF aggregators keep their own trailing partial).
        maxlen = self.data_store._bars.maxlen or 5000
        new_store = DataStore(max_bars=maxlen)
        new_htf_aggs: dict[int, BarAggregator] = {}
        for iv in new_htf:
            new_store._register_htf(iv)
            new_htf_aggs[iv] = BarAggregator(self.symbol, iv)
        for b in completed:
            new_store.add_bar(b)
            for iv, agg in new_htf_aggs.items():
                hb = agg.on_bar(b)
                if hb is not None:
                    new_store._add_htf_bar(iv, hb)

        # 6. Atomic commit. `_bar_index` is intentionally NOT reset.
        self.aggregator = new_agg
        self.target_interval = new_interval
        self.data_store = new_store
        self._aggregated_bars = list(completed)
        self._warmup_bar_count = len(completed)
        self._htf_intervals = new_htf
        self._htf_aggregators = new_htf_aggs
        self._htf_required = new_strategy.htf_required_bars() or {}

        self._emit(
            "on_status",
            f"[SWAP] rebuilt timeframe → {new_interval}s: "
            f"{len(completed)} bars from {len(bars_1m)} 1-min bars")
        return True

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

    def reset_bar_monotonicity(self) -> None:
        """Reset the out-of-order bar guards on all aggregators (issue #78).

        Called by the GUI when a reconnect re-enters the tick-history replay
        window, so the first bar after the gap is always accepted without
        discarding any in-progress aggregation.
        """
        self.aggregator.reset_stale_tracking()
        for agg in self._htf_aggregators.values():
            agg.reset_stale_tracking()
        # Issue #78 (review): the first replayed tick force-finalizes the
        # in-progress 1-min bar into a TRUNCATED bar, which lands in the dedup
        # set. Drop that last entry so the correct, full replayed bar can be
        # re-accepted instead of being silently dedup-dropped.
        self.clear_last_bar_dedup()

    def clear_last_bar_dedup(self) -> None:
        """Remove only the most-recent 1-min bar from the dedup set (issue #78).

        On a COM reconnect the BarBuilder force-finalizes whatever in-progress
        1-min bar it held into a TRUNCATED bar (missing the ticks that arrived
        after the drop). Its timestamp is recorded in ``_seen_1m_dts``. When the
        replay then delivers the CORRECT full bar for that same minute,
        ``_ingest_1m_bar`` would dedup-drop it, leaving the truncated bar in
        place forever.

        Removing ONLY the last (newest) entry lets the full replayed bar be
        re-accepted while older bars stay deduped — we must not re-process
        those or they would double-count trades/volume.
        """
        if self._seen_1m_dts:
            self._seen_1m_dts.discard(max(self._seen_1m_dts))

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
        # Issue #79: warmup + the tick-history replay that follows are the
        # "reloading" window. Mark it so mode overrides are ignored until
        # the GUI clears the flag when live ticks begin.
        self._is_reloading = True

        bars = parse_kline_strings(
            kline_strings, symbol=self.symbol, interval=self.target_interval,
        )

        # No filtering needed — warmup data from COM KLine API is clean.
        # BB distortion at session gaps is handled in chart.py via gap detection.

        for bar in bars:
            self.data_store.add_bar(bar)
            self._feed_htf(bar)
            self._bar_index += 1

        self._aggregated_bars.extend(bars)
        self._warmup_bar_count = len(self._aggregated_bars)

        # For 1-min strategies, warmup bars are also 1-min. Store them
        # in _1m_bars for multi-TF charting AND track their dts in
        # _seen_1m_dts so a subsequent reload_1m_bars / tick-history
        # replay dedupes against them instead of adding duplicates
        # (issue #45).
        if self.target_interval == 60:
            for bar in bars:
                self._1m_bars.append(bar)
                self._seen_1m_dts.add(bar.dt)

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

    def check_tick_exit(self, price: int, tick_dt: str = "") -> dict | None:
        """Check if a tick price triggers a pending TP/SL exit.

        Called on every tick during RUNNING state for real-time exit
        detection.  This allows exits to fill at the exact tick price
        instead of waiting for the aggregated bar to complete.

        Returns a dict with trade info if an exit triggered, None otherwise.
        """
        if self.state != LiveState.RUNNING:
            return None
        if self.broker.position_size == 0 or not self.broker._pending_exits:
            return None

        from ..backtest.broker import OrderSide
        side = self.broker.position_side

        for order in list(self.broker._pending_exits):
            limit = order.limit
            stop = order.stop
            fill_price = None

            if side == OrderSide.LONG:
                if limit is not None and price >= limit:
                    fill_price = limit  # TP: fill at intended limit price
                elif stop is not None and price <= stop:
                    fill_price = price  # SL: fill at actual tick (market price)
            elif side == OrderSide.SHORT:
                if limit is not None and price <= limit:
                    fill_price = limit  # TP: fill at intended limit price
                elif stop is not None and price >= stop:
                    fill_price = price  # SL: fill at actual tick (market price)

            if fill_price is not None:
                self.broker._current_bar_dt = tick_dt
                # Use _bar_index - 1 (last processed bar's idx).
                # _bar_index was already incremented past current bar;
                # using it directly causes _check_for_trade_close to
                # match the NEXT bar and fire a duplicate TRADE_CLOSE.
                self.broker._close_position(
                    order.tag, fill_price, self._bar_index - 1)
                # Log and save
                last_trade = self.broker.trades[-1]
                # Create a minimal bar for logging (use TWT time)
                log_bar = Bar(symbol=self.symbol,
                              dt=datetime.now(_TZ_TAIPEI).replace(tzinfo=None),
                              open=price, high=price, low=price, close=price,
                              volume=0, interval=0)
                self._log_decision(
                    log_bar, "TRADE_CLOSE", last_trade.side.value,
                    last_trade.exit_tag, fill_price,
                    f"tick exit PnL={last_trade.pnl:+}",
                )
                self._auto_save_session()
                self._emit("on_tick_exit", last_trade)
                return {
                    "tag": order.tag,
                    "price": fill_price,
                    "pnl": last_trade.pnl,
                    "dt": tick_dt,
                }

        return None

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

    def _feed_htf(self, primary_bar: Bar) -> None:
        """Feed a completed primary bar through HTF aggregators."""
        if not self._htf_aggregators:
            return
        for iv, agg in self._htf_aggregators.items():
            completed = agg.on_bar(primary_bar)
            if completed is not None:
                self.data_store._add_htf_bar(iv, completed)

    def _htf_warmup_satisfied(self) -> bool:
        if not self._htf_required:
            return True
        for iv, n in self._htf_required.items():
            if self.data_store._htf_len(iv) < n:
                return False
        return True

    def _process_aggregated_bar(self, bar: Bar) -> None:
        """Process a completed aggregated bar through the strategy pipeline.

        Same sequence as BacktestEngine.run() (engine.py:53-65).
        When suppress_strategy is True, only updates DataStore (no trading).
        """
        new_mode = self._check_mode_override()
        if new_mode:
            self._apply_mode_switch(new_mode)

        self.data_store.add_bar(bar)
        self._feed_htf(bar)
        self._aggregated_bars.append(bar)
        idx = self._bar_index
        self._bar_index += 1

        # During history catchup or regime idle, only build bar state — no trading
        if self.suppress_strategy or self.regime_idle:
            return

        ctx = self.broker.context
        # Two timestamps:
        #   bar_close_dt  — synthetic bar END time (bar.dt + interval) at
        #                   second precision, used for bar-level exit
        #                   resolution where the actual fill moment within
        #                   the bar is unknown.
        #   fill_dt       — actual wall-clock TPE moment we're processing
        #                   the just-completed bar. This is the moment COM
        #                   delivered the next-minute tick that triggered
        #                   bar completion, i.e. the real entry/exit fill
        #                   time in live mode. For 30-min bars opening
        #                   10:45–11:15, fill_dt is when the 11:15 tick
        #                   actually arrived (e.g. 11:15:01.234), not
        #                   "11:15:00".
        bar_close_dt = ""
        if bar.dt:
            bar_close_dt = (bar.dt + timedelta(seconds=bar.interval)
                           ).strftime("%Y-%m-%d %H:%M:%S")
        fill_dt = datetime.now(_TZ_TAIPEI).replace(tzinfo=None
                  ).strftime("%Y-%m-%d %H:%M:%S")

        # Check exit orders against this bar
        if idx > 0:
            self.broker.check_exits(idx, bar.open, bar.high, bar.low, bar.close, bar_close_dt)
            self._check_for_trade_close(bar, idx)

        # Run strategy if enough bars (primary AND HTF warmup satisfied)
        if (len(self.data_store) >= self.strategy.required_bars()
                and self._htf_warmup_satisfied()):
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

            # Catch-up exit check: if strategy just queued new exits while a
            # position is open, check them immediately against this bar's OHLC.
            # Without this, strategies that set TP/SL one bar late (only when
            # position_size > 0) miss an entire bar of exit resolution — both
            # bar-level check_exits AND tick-level check_tick_exit are blind
            # because _pending_exits was empty until now.
            if (len(self.broker._pending_exits) > old_exits
                    and self.broker.position_size > 0):
                # Catch-up exit fired same bar as the strategy queued it —
                # use wall-clock fill_dt to record the actual moment.
                self.broker.check_exits(
                    idx, bar.open, bar.high, bar.low, bar.close, fill_dt)
                self._check_for_trade_close(bar, idx)

        # Fill entry orders and market closes at bar close.
        # Use wall-clock fill_dt (not synthetic bar boundary) so trade
        # entry_dt/exit_dt reflect when COM actually delivered the bar.
        trades_before = len(self.broker.trades)
        self.broker.on_bar_close(idx, bar.close, fill_dt)
        # Check for market close trades (broker.close() processed inside on_bar_close)
        if len(self.broker.trades) > trades_before:
            self._check_for_trade_close(bar, idx)
        self._check_for_entry_fill(bar, idx)

        self._emit("on_bar", bar)

    def _check_for_trade_close(self, bar: Bar, idx: int) -> None:
        """Check if a trade was just closed by check_exits."""
        if self.broker.trades and self.broker.trades[-1].exit_bar_index == idx:
            trade = self.broker.trades[-1]
            exit_type = self.broker.last_exit_type or "close"
            exit_limit = self.broker.last_exit_limit
            self._log_decision(
                bar, "TRADE_CLOSE", trade.side.value, trade.exit_tag,
                trade.exit_price, f"PnL={trade.pnl:+}",
                exit_type=exit_type, exit_limit=exit_limit,
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
                      price: int, reason: str, *,
                      exit_type: str = "", exit_limit: int | None = None) -> None:
        now = datetime.now()
        self.csv_logger.log_decision(
            dt=now, bar_dt=bar.dt, strategy=self.strategy.name,
            action=action, side=side, tag=tag, price=price, reason=reason,
        )
        decision = {
            "dt": now, "bar_dt": bar.dt, "strategy": self.strategy.name,
            "action": action, "side": side, "tag": tag,
            "price": price, "reason": reason,
        }
        if exit_type:
            decision["exit_type"] = exit_type
        if exit_limit is not None:
            decision["exit_limit"] = exit_limit
        self._emit("on_decision", decision)

    def log_blocked_signal(self, action: str, side: str, price: int,
                           reason_code: str, detail: str = "") -> None:
        """Record a strategy signal the risk gate/TradingGuard rejected (issue #62).

        When an entry or exit signal is blocked before a real order is sent
        — daily-loss limit reached, waiting on a prior fill, system halted,
        no confirmed real position, settlement-day no-entry window, etc. —
        the block used to be invisible in ``decisions.csv`` and the UI event
        log. The user could only discover it by digging through raw logs.

        This surfaces the block as a ``SIGNAL_BLOCKED`` decision tied to the
        most recent bar, using the same emit path as ``_log_decision`` so it
        renders in the UI event log table and is appended to ``decisions.csv``.

        Args:
            action: the original signal action ("ENTRY_FILL", "TRADE_CLOSE", ...)
            side: signal direction ("LONG", "SHORT", or "")
            price: signal price (int; TAIFEX prices are integers)
            reason_code: short machine-readable block code, stored in the
                ``tag`` column (e.g. "DAILY_LOSS_LIMIT", "FILL_PENDING")
            detail: human-readable explanation, stored in the ``reason`` column
        """
        bar_dt = (self._aggregated_bars[-1].dt if self._aggregated_bars
                  else datetime.now(_TZ_TAIPEI).replace(tzinfo=None))
        now = datetime.now()
        reason = f"{action}: {detail}" if detail else action
        price = int(price)
        self.csv_logger.log_decision(
            dt=now, bar_dt=bar_dt, strategy=self.strategy.name,
            action="SIGNAL_BLOCKED", side=side, tag=reason_code,
            price=price, reason=reason,
        )
        self._emit("on_decision", {
            "dt": now, "bar_dt": bar_dt, "strategy": self.strategy.name,
            "action": "SIGNAL_BLOCKED", "side": side, "tag": reason_code,
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

    def get_exit_info(self) -> dict | None:
        """Return current exit target info for 1-min bar logging.

        Works for ALL strategy timeframes (1m, 15m, 60m, H4 etc.) —
        the caller fires this on every 1-min bar regardless of the
        strategy's native timeframe.

        Two sources merged (broker-level takes priority):

        1. ``broker._pending_exits`` — for strategies using
           ``broker.exit(limit=, stop=)``.  Persists between strategy
           runs so values remain valid across 1-min bars even when the
           strategy only runs every 15/60 minutes.

        2. ``strategy.exit_levels()`` — optional opt-in for strategies
           that manage exits internally via ``broker.close()`` (e.g.
           trailing stops held in ``self.trailing_stop_price``).  Called
           via ``getattr`` so strategies without this method are safely
           ignored.

        Returns None when flat.
        """
        if self.broker.position_size == 0:
            return None
        info: dict = {
            "side": self.broker.position_side.value,
            "entry_price": self.broker.entry_price,
            "limit": None,
            "stop": None,
        }
        # Source 1: broker pending exit orders
        if self.broker._pending_exits:
            order = self.broker._pending_exits[0]
            if order.limit is not None:
                info["limit"] = int(order.limit)
            if order.stop is not None:
                info["stop"] = int(order.stop)
        # Source 2: strategy-reported levels (optional, via duck typing)
        exit_levels_fn = getattr(self.strategy, "exit_levels", None)
        if callable(exit_levels_fn):
            try:
                levels = exit_levels_fn() or {}
                if info["limit"] is None and levels.get("limit") is not None:
                    info["limit"] = int(levels["limit"])
                if info["stop"] is None and levels.get("stop") is not None:
                    info["stop"] = int(levels["stop"])
            except Exception:
                pass  # broken exit_levels() — silently fall back
        return info

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

    def get_live_bars(self) -> list[Bar]:
        """Return only live-trading aggregated bars (excluding warmup history)."""
        return list(self._aggregated_bars[self._warmup_bar_count:])

    @property
    def started_at(self) -> str:
        """When this session originally started (ISO string).

        Survives restarts: restore_session() replaces the process start
        time with the one persisted in session.json.
        """
        return self._started_at

    def get_1m_bars(self) -> list[Bar]:
        """Return snapshot of stored 1-min bars."""
        return list(self._1m_bars)

    def get_bars_at_interval(self, interval: int) -> list[Bar]:
        """Return bars at the given interval (seconds).

        If interval matches the strategy's native timeframe, returns aggregated
        bars plus the aggregator's in-progress partial (if any). Otherwise,
        re-aggregates stored 1-min bars on demand — aggregate_bars() already
        includes the partial via flush() at the end.

        Including the in-progress bar prevents the chart from showing stale
        data when a multi-minute bar is mid-formation (issue #44).
        """
        if interval == self.target_interval:
            bars = list(self._aggregated_bars)
            partial = self.aggregator.get_partial_bar()
            if partial is not None:
                bars.append(partial)
            return bars
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
            # Use wall-clock time — manual stop fires mid-bar, so neither
            # bar open nor bar end reflects the actual close moment.
            last_close_dt = datetime.now(_TZ_TAIPEI).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")
            # Issue #61: prefer the last CLOSED 1-min bar close over the
            # strategy-TF aggregated close, which can be many minutes stale
            # at an arbitrary stop moment. No GUI tick price is reachable
            # from inside the runner, so tick_price=0; the helper falls back
            # to the agg close only when no 1-min bar exists.
            exit_price, _src = select_freshest_price(
                0, self._1m_bars, self._aggregated_bars)
            if exit_price <= 0:
                exit_price = last_bar.close  # last-resort (no 1m, no agg)
            # NB: _log_decision runs AFTER force_close has closed the
            # position, so position_side is already None here — read the
            # just-appended trade's side (trades[-1]) for the log row.
            self.broker.force_close(self._bar_index, exit_price, last_close_dt)
            self._log_decision(
                last_bar, "FORCE_CLOSE", self.broker.trades[-1].side.value if self.broker.trades else "",
                "stop", exit_price, "live runner stopped",
            )

        self._auto_save_session()
        self._generate_daily_report()
        self.csv_logger.close()
        self.release_lock()
        self.state = LiveState.STOPPED
        self._emit("on_status", "Stopped")

        return self._summary()

    def _summary(self) -> dict:
        all_trades = self.broker.trades
        # "paper" = the full simulated view (every trade has a simulated
        # fill, including real-mirrored ones); "real" = the subset that
        # was actually executed at the broker.
        real = [t for t in all_trades if t.source == "real"]
        return {
            "trades": len(all_trades),
            "pnl": sum(t.pnl for t in all_trades),
            "paper_trades": len(all_trades),
            "paper_pnl": sum(t.pnl for t in all_trades),
            "real_trades": len(real),
            "real_pnl": sum(t.pnl for t in real),
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
                "trading_mode": self.trading_mode,
                "daily_loss_limit": self.daily_loss_limit,
                "started_at": self._started_at,
                "saved_at": datetime.now().isoformat(timespec="seconds"),
                "bar_index": self._bar_index,
                "broker": self.broker.to_dict(),
            }
            save_session(self._session_path, data)
        except Exception:
            pass  # best-effort; don't crash the bot

    def _session_key(self) -> tuple[str, str]:
        """Return ``(YYYY-MM-DD, "DAY"|"NIGHT")`` for the current TPE moment.

        Delegates to :func:`src.regime.switch_logic.session_slot` so the
        session-boundary definition is shared with the switching runner.
        """
        from ..regime.switch_logic import session_slot
        return session_slot()

    def _generate_daily_report(self) -> None:
        """Generate a daily report after session stop (best-effort).

        Debounced per ``(date, DAY|NIGHT)`` so that the same session is
        never reported twice — the 30s session-end poll fires this
        method repeatedly inside the close window, and a manual stop
        right after auto-fire would otherwise produce a duplicate.
        """
        try:
            key = self._session_key()
            if key == self._last_report_session:
                return
            self._last_report_session = key

            from ..daily_report.report_generator import generate_session_report
            report = generate_session_report(
                broker=self.broker,
                data_store=self.data_store,
                strategy_name=self.strategy_display_name,
                strategy_params=getattr(self.strategy, "params", None),
                point_value=self.point_value,
                symbol=self.symbol,
                bot_name=self.bot_name,
                started_at=self._started_at,
            )
            if report is not None:
                self._emit("on_daily_report", report)
        except Exception:
            pass  # best-effort; don't crash the bot on report failure

    def restore_session(self, session_data: dict) -> int:
        """Restore broker state from a saved session.

        Call this BEFORE feed_warmup_bars(). The warmup will rebuild DataStore
        for the strategy, while the broker keeps the restored trade history.

        Returns the number of trades restored.
        """
        broker_data = session_data.get("broker", {})
        self.broker = SimulatedBroker.from_dict(broker_data)
        # New trades belong to the strategy deployed NOW; a position
        # restored open keeps its persisted entry_strategy (regime mode:
        # the previous deploy may have run a different strategy).
        self.broker.strategy_label = self.strategy_display_name
        # from_dict does not restore trade_source; re-set it from the
        # current trading_mode so resumed sessions tag trades correctly.
        self.broker.trade_source = _mode_to_source(self.trading_mode)
        self._bar_index = session_data.get("bar_index", 0)
        self._started_at = session_data.get("started_at", self._started_at)
        return len(self.broker.trades)

    # Emit a progress tick every N bars during reload so the GUI can keep
    # the Tkinter event loop pumping (issue #79 — long-lookback strategies
    # froze the UI for seconds while thousands of saved bars were re-fed).
    _RELOAD_PROGRESS_EVERY = 500

    def reload_1m_bars(self, progress_cb: Callable[[int, int], None] | None = None) -> int:
        """Reload saved 1-min bar CSVs into _1m_bars and _seen_1m_dts.

        Call AFTER restore_session() and feed_warmup_bars().

        Populates the 1-min bar cache for multi-TF charting and prevents
        duplicate processing when tick history replays the same data.

        For 1-min NATIVE strategies, also merges the new CSV bars into
        _aggregated_bars and rebuilds data_store in sorted order so the
        live chart and strategy see a continuous bar history. Without
        this merge the live chart (which draws from _aggregated_bars)
        showed a visible gap between the warmup end and the first live
        bar — the COM warmup API does not return bars for the currently
        in-progress trading session, and the historical tick-replay
        that happens during tick subscription rebuilds those missing
        bars but then drops them via the _seen_1m_dts dedup before they
        can reach _aggregated_bars (issue #45).

        ``progress_cb`` (issue #79): optional ``(done, total) -> None`` hook
        invoked every ``_RELOAD_PROGRESS_EVERY`` bars during the two heavy
        loops (CSV parse and, for 1-min strategies, the data_store/HTF
        rebuild). LiveRunner is UI-agnostic, so the GUI passes a callback
        that logs progress and pumps the Tkinter event loop to stay
        responsive. ``total`` is 0 while it is not yet known (parse phase).
        Bars are still fed strictly in order — the callback only observes.

        Returns the number of NEW 1-min bars loaded from CSV.
        """
        import csv
        import glob as glob_mod

        def _tick(done: int, total: int) -> None:
            if progress_cb is not None:
                try:
                    progress_cb(done, total)
                except Exception:
                    pass  # a broken UI callback must never break the reload

        pattern = os.path.join(self.bot_dir, "bars_1m_*.csv")
        csv_files = sorted(glob_mod.glob(pattern))
        if not csv_files:
            return 0

        new_bars: list[Bar] = []
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
                            if dt in self._seen_1m_dts:
                                continue
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
                            self._seen_1m_dts.add(dt)
                            self._1m_bars.append(bar)
                            new_bars.append(bar)
                            # total unknown during parse → 0
                            if len(new_bars) % self._RELOAD_PROGRESS_EVERY == 0:
                                _tick(len(new_bars), 0)
                        except (ValueError, IndexError):
                            continue
            except OSError:
                continue

        # For 1-min native strategies, the CSV bars ARE target-timeframe
        # bars. Merge them into _aggregated_bars and rebuild data_store
        # in sorted order so the live chart and strategy indicators see
        # a continuous history without the gap between warmup end and
        # live feed start (issue #45). Non-1-min strategies handle
        # multi-TF charting via _1m_bars re-aggregation in
        # get_bars_at_interval(), so their _aggregated_bars is left
        # untouched here.
        if self.target_interval == 60 and new_bars:
            merged = sorted(
                list(self._aggregated_bars) + new_bars,
                key=lambda b: b.dt,
            )
            self._aggregated_bars[:] = merged
            # Rebuild data_store in sorted order. The deque maxlen caps
            # older history automatically so memory stays bounded.
            from ..market_data.data_store import DataStore
            maxlen = self.data_store._bars.maxlen or 5000
            new_store = DataStore(max_bars=maxlen)
            total = len(merged)
            for i, b in enumerate(merged, 1):
                new_store.add_bar(b)
                if i % self._RELOAD_PROGRESS_EVERY == 0:
                    _tick(i, total)
            self.data_store = new_store
            # MTF: rebuild HTF state by re-feeding primary bars through
            # fresh aggregators so backtest/live parity holds across
            # session resume.
            if self._htf_intervals:
                for iv in self._htf_intervals:
                    new_store._register_htf(iv)
                self._htf_aggregators = {
                    iv: BarAggregator(self.symbol, iv)
                    for iv in self._htf_intervals
                }
                for i, b in enumerate(merged, 1):
                    self._feed_htf(b)
                    if i % self._RELOAD_PROGRESS_EVERY == 0:
                        _tick(i, total)
            # CSV-loaded bars are historical, not live — bump
            # _warmup_bar_count so get_live_bars() continues to slice
            # off the correct prefix.
            self._warmup_bar_count = len(self._aggregated_bars)

        return len(new_bars)

    @property
    def session_path(self) -> str:
        return self._session_path
