"""Backtest chart: candlestick chart with entry/exit markers using lightweight-charts."""

from __future__ import annotations

import math
import queue
import threading

from ..market_data.models import Bar
from .broker import Trade, OrderSide

try:
    import pandas as pd
    from lightweight_charts import Chart
    _LWC_AVAILABLE = True
except ImportError:
    _LWC_AVAILABLE = False

import sys

# Bars of context to show before entry / after exit when zooming to a trade
_PAD_BEFORE = 20
_PAD_AFTER = 10


def _strip_motw() -> None:
    """Remove Mark-of-the-Web from Python.Runtime.dll if present.

    When users download a ZIP from the internet, Windows tags every extracted
    file with a Zone.Identifier NTFS alternate data stream.  .NET Framework
    refuses to load assemblies marked as "internet zone", which makes
    clr_loader's pyclr_get_function return NULL and raises
    "Failed to resolve Python.Runtime.Loader.Initialize".
    """
    import os
    try:
        import pythonnet
        dll = os.path.join(
            os.path.dirname(pythonnet.__file__), "runtime", "Python.Runtime.dll"
        )
        zone = dll + ":Zone.Identifier"
        if os.path.exists(zone):
            os.remove(zone)
    except Exception:
        pass


def _check_dotnet() -> None:
    """Verify pythonnet / .NET Framework is available (required by pywebview on Windows).

    Called before opening a chart so we can raise a clear error instead of
    silently crashing in the multiprocessing child process.
    """
    if sys.platform != 'win32' or not _LWC_AVAILABLE:
        return
    _strip_motw()
    try:
        import clr  # noqa: F401
    except RuntimeError as exc:
        raise RuntimeError(
            "Chart requires .NET Framework 4.8.\n"
            "Please enable it in Windows Settings → Apps → Optional features "
            "→ More Windows features → '.NET Framework 4.8 Advanced Services',\n"
            "or download from https://dotnet.microsoft.com/download/dotnet-framework/net48"
        ) from exc


def _compute_bollinger(
    bars: list[Bar], period: int = 20, num_std: float = 2.0,
) -> tuple[list[float], list[float], list[float]]:
    """Compute Bollinger Bands for a list of bars. Returns (upper, middle, lower).

    Uses O(n) rolling sum/sum-of-squares for efficiency.
    """
    n = len(bars)
    upper = [float("nan")] * n
    middle = [float("nan")] * n
    lower = [float("nan")] * n

    if n < period:
        return upper, middle, lower

    # Rolling sums for O(n) computation
    rolling_sum = 0.0
    rolling_sq_sum = 0.0

    for i in range(n):
        c = bars[i].close
        rolling_sum += c
        rolling_sq_sum += c * c

        if i >= period:
            old = bars[i - period].close
            rolling_sum -= old
            rolling_sq_sum -= old * old

        if i >= period - 1:
            mean = rolling_sum / period
            variance = rolling_sq_sum / period - mean * mean
            # Guard against floating point rounding producing tiny negatives
            std = math.sqrt(max(0.0, variance))
            middle[i] = mean
            upper[i] = mean + num_std * std
            lower[i] = mean - num_std * std

    return upper, middle, lower


def plot_backtest(
    bars: list[Bar],
    trades: list[Trade],
    title: str = "",
    focus_trade_index: int | None = None,
    bb_period: int = 20,
    bb_std: float = 2.0,
) -> None:
    """Plot candlestick chart with trade entry/exit markers and Bollinger Bands.

    Args:
        bars: List of OHLCV bars from the backtest.
        trades: List of completed trades with bar indices.
        title: Chart title (e.g. strategy name).
        focus_trade_index: If set, zoom to this trade (0-based index into trades list)
            with some padding bars. If None, show all bars.
        bb_period: Bollinger Band period.
        bb_std: Bollinger Band standard deviation multiplier.
    """
    if not _LWC_AVAILABLE:
        raise ImportError(
            "lightweight-charts and pandas are required for charting. "
            "Install with: pip install lightweight-charts pandas"
        )

    _check_dotnet()

    if not bars:
        return

    # Compute Bollinger Bands on full bar series
    bb_upper, bb_middle, bb_lower = _compute_bollinger(bars, bb_period, bb_std)

    # Build chart title
    chart_title = title or "Backtest"
    if focus_trade_index is not None and 0 <= focus_trade_index < len(trades):
        trade_num = focus_trade_index + 1
        total = len(trades)
        chart_title = (
            f"{title}  —  Trade #{trade_num}/{total}"
            if title else f"Trade #{trade_num}/{total}"
        )

    # Create chart
    chart = Chart(title=chart_title, width=1280, height=720)
    chart.legend(visible=True, ohlc=True, percent=True, lines=True,
                 font_size=13)
    chart.precision(0)  # TAIFEX prices are integers
    chart.layout(background_color='#1e1e1e', text_color='#d4d4d4')
    chart.grid(color='rgba(255,255,255,0.06)')
    chart.candle_style(
        up_color='#26a69a', down_color='#ef5350',
        wick_up_color='#26a69a', wick_down_color='#ef5350',
    )

    # Build OHLCV DataFrame — convert datetimes to pandas Timestamp[ns] to avoid
    # pandas 3.x microsecond resolution mismatch with lightweight-charts v2.1
    # (lightweight-charts does astype('int64') // 10**9 which assumes nanoseconds)
    df = pd.DataFrame({
        'time': pd.to_datetime([b.dt for b in bars]).as_unit('ns'),
        'open': [b.open for b in bars],
        'high': [b.high for b in bars],
        'low': [b.low for b in bars],
        'close': [b.close for b in bars],
        'volume': [b.volume for b in bars],
    })
    chart.set(df)

    # Bollinger Bands as overlay lines — use ns-resolution timestamps
    bb_times = pd.to_datetime([b.dt for b in bars]).as_unit('ns')

    # Filter out NaN values for each band
    bb_upper_data = pd.DataFrame({
        'time': bb_times,
        'BB Upper': bb_upper,
    }).dropna()
    line_upper = chart.create_line('BB Upper', color='dodgerblue', width=1)
    line_upper.set(bb_upper_data)

    bb_middle_data = pd.DataFrame({
        'time': bb_times,
        'BB Middle': bb_middle,
    }).dropna()
    line_middle = chart.create_line('BB Middle', color='orange', width=1)
    line_middle.set(bb_middle_data)

    bb_lower_data = pd.DataFrame({
        'time': bb_times,
        'BB Lower': bb_lower,
    }).dropna()
    line_lower = chart.create_line('BB Lower', color='dodgerblue', width=1)
    line_lower.set(bb_lower_data)

    # Trade markers — use exact candle timestamps to avoid _single_datetime_format
    # interval-snapping mismatch.  chart.candle_data['time'] holds the integer
    # Unix timestamps that the JS series actually uses.
    candle_times = chart.candle_data['time'].tolist()
    n_candles = len(candle_times)

    for i, t in enumerate(trades):
        ei = t.entry_bar_index
        xi = t.exit_bar_index

        # Entry marker
        if 0 <= ei < n_candles:
            if t.side == OrderSide.LONG:
                _add_marker(chart, candle_times[ei], 'belowBar', 'arrowUp',
                            '#1565C0', f"#{i+1} Entry {t.side.value} | {t.tag}")
            else:
                _add_marker(chart, candle_times[ei], 'aboveBar', 'arrowDown',
                            '#E040FB', f"#{i+1} Entry {t.side.value} | {t.tag}")

        # Exit marker
        if 0 <= xi < n_candles:
            pnl_str = f"{t.pnl:+,}"
            bars_held = t.exit_bar_index - t.entry_bar_index
            if t.pnl >= 0:
                color = '#00BCD4'  # cyan — profit
            elif t.side == OrderSide.LONG:
                color = '#E040FB'  # purple — long loss
            else:
                color = '#FF5252'  # red — short loss
            # Short exits buy back (arrow up, below bar); long exits sell (arrow down, above bar)
            if t.side == OrderSide.SHORT:
                _add_marker(chart, candle_times[xi], 'belowBar', 'arrowUp',
                            color, f"#{i+1} Exit ({t.exit_tag}) P&L:{pnl_str} [{bars_held}bars]")
            else:
                _add_marker(chart, candle_times[xi], 'aboveBar', 'arrowDown',
                            color, f"#{i+1} Exit ({t.exit_tag}) P&L:{pnl_str} [{bars_held}bars]")

    # Flush all markers to JS in one call
    chart._update_markers()

    # Zoom to specific trade if requested — use raw timestamps to avoid snapping
    if focus_trade_index is not None and 0 <= focus_trade_index < len(trades):
        ft = trades[focus_trade_index]
        view_start = max(0, ft.entry_bar_index - _PAD_BEFORE)
        view_end = min(n_candles - 1, ft.exit_bar_index + _PAD_AFTER)
        start_ts = int(candle_times[view_start])
        end_ts = int(candle_times[view_end])
        chart.run_script(
            f'{chart.id}.chart.timeScale().setVisibleRange('
            f'{{from: {start_ts}, to: {end_ts}}})'
        )

    chart.show(block=True)


def _add_marker(chart, time_val, position: str, shape: str, color: str, text: str):
    """Add a marker using the exact candle timestamp, bypassing interval-snapping."""
    marker_id = chart.win._id_gen.generate()
    chart.markers[marker_id] = {
        "time": int(time_val),
        "position": position,
        "shape": shape,
        "color": color,
        "text": text,
    }


def _compute_bb_point(
    closes: list, period: int, num_std: float,
) -> tuple[float, float, float] | None:
    """Compute a single Bollinger Band point from the last *period* closes."""
    if len(closes) < period:
        return None
    window = closes[-period:]
    mean = sum(window) / period
    variance = sum((x - mean) ** 2 for x in window) / period
    std = math.sqrt(max(0.0, variance))
    return (mean + num_std * std, mean, mean - num_std * std)


class LiveChart:
    """Real-time updating chart for live trading.

    Thread-safe: push_* methods can be called from any thread.
    run() blocks (must be called in a dedicated thread).
    """

    def __init__(
        self,
        initial_bars: list[Bar],
        initial_trades: list[Trade],
        title: str = "",
        bb_period: int = 20,
        bb_std: float = 2.0,
    ):
        self._initial_bars = list(initial_bars)
        self._initial_trades = list(initial_trades)
        self._title = title
        self._bb_period = bb_period
        self._bb_std = bb_std
        self._queue: queue.Queue = queue.Queue()
        self._alive = False
        # Track closes for incremental BB computation
        self._closes: list[float] = [b.close for b in initial_bars]

    # ── Thread-safe push API ──

    def push_bar(self, bar: Bar) -> None:
        """Enqueue a completed aggregated bar."""
        if self._alive:
            self._queue.put(("bar", bar))

    def push_partial(self, bar: Bar) -> None:
        """Enqueue a partial (forming) bar update."""
        if self._alive:
            self._queue.put(("partial", bar))

    def push_trade(self, trade: Trade, index: int) -> None:
        """Enqueue a new completed trade marker."""
        if self._alive:
            self._queue.put(("trade", trade, index))

    def close(self) -> None:
        """Signal chart to exit."""
        self._queue.put(("close",))

    @property
    def is_alive(self) -> bool:
        return self._alive

    # ── Blocking run (call in a daemon thread) ──

    def run(self) -> None:
        """Create chart, render initial data, start feeder, show chart (blocking)."""
        if not _LWC_AVAILABLE:
            return
        _check_dotnet()
        if not self._initial_bars:
            return

        bars = self._initial_bars
        trades = self._initial_trades

        # Create chart
        chart = Chart(title=self._title, width=1280, height=720)
        chart.legend(visible=True, ohlc=True, percent=True, lines=True,
                     font_size=13)
        chart.precision(0)  # TAIFEX prices are integers
        chart.layout(background_color='#1e1e1e', text_color='#d4d4d4')
        chart.grid(color='rgba(255,255,255,0.06)')
        chart.candle_style(
            up_color='#26a69a', down_color='#ef5350',
            wick_up_color='#26a69a', wick_down_color='#ef5350',
        )

        # Set initial OHLCV data
        df = pd.DataFrame({
            'time': pd.to_datetime([b.dt for b in bars]).as_unit('ns'),
            'open': [b.open for b in bars],
            'high': [b.high for b in bars],
            'low': [b.low for b in bars],
            'close': [b.close for b in bars],
            'volume': [b.volume for b in bars],
        })
        chart.set(df)

        # Bollinger Bands
        bb_upper, bb_middle, bb_lower = _compute_bollinger(
            bars, self._bb_period, self._bb_std,
        )
        bb_times = pd.to_datetime([b.dt for b in bars]).as_unit('ns')

        line_upper = chart.create_line('BB Upper', color='dodgerblue', width=1)
        line_upper.set(pd.DataFrame({
            'time': bb_times, 'BB Upper': bb_upper,
        }).dropna())

        line_middle = chart.create_line('BB Middle', color='orange', width=1)
        line_middle.set(pd.DataFrame({
            'time': bb_times, 'BB Middle': bb_middle,
        }).dropna())

        line_lower = chart.create_line('BB Lower', color='dodgerblue', width=1)
        line_lower.set(pd.DataFrame({
            'time': bb_times, 'BB Lower': bb_lower,
        }).dropna())

        # Initial trade markers
        candle_times = chart.candle_data['time'].tolist()
        n_candles = len(candle_times)

        for i, t in enumerate(trades):
            ei = t.entry_bar_index
            xi = t.exit_bar_index

            if 0 <= ei < n_candles:
                if t.side == OrderSide.LONG:
                    _add_marker(chart, candle_times[ei], 'belowBar', 'arrowUp',
                                '#1565C0', f"#{i+1} Entry {t.side.value} | {t.tag}")
                else:
                    _add_marker(chart, candle_times[ei], 'aboveBar', 'arrowDown',
                                '#E040FB', f"#{i+1} Entry {t.side.value} | {t.tag}")

            if 0 <= xi < n_candles:
                pnl_str = f"{t.pnl:+,}"
                bars_held = xi - ei
                if t.pnl >= 0:
                    color = '#00BCD4'
                elif t.side == OrderSide.LONG:
                    color = '#E040FB'
                else:
                    color = '#FF5252'
                if t.side == OrderSide.SHORT:
                    _add_marker(chart, candle_times[xi], 'belowBar', 'arrowUp',
                                color, f"#{i+1} Exit ({t.exit_tag}) P&L:{pnl_str} [{bars_held}bars]")
                else:
                    _add_marker(chart, candle_times[xi], 'aboveBar', 'arrowDown',
                                color, f"#{i+1} Exit ({t.exit_tag}) P&L:{pnl_str} [{bars_held}bars]")

        chart._update_markers()

        # Start feeder thread, then block on chart display
        self._alive = True
        feeder = threading.Thread(
            target=self._feeder_loop, daemon=True,
            args=(chart, line_upper, line_middle, line_lower),
        )
        feeder.start()

        chart.show(block=True)
        self._alive = False

    # ── Feeder loop (runs in its own daemon thread) ──

    def _feeder_loop(self, chart, line_upper, line_middle, line_lower):
        """Poll queue and apply updates to the live chart."""
        while self._alive:
            try:
                msg = self._queue.get(timeout=1)
            except queue.Empty:
                continue

            kind = msg[0]

            if kind == "close":
                try:
                    chart.exit()
                except Exception:
                    pass
                break

            try:
                if kind == "bar":
                    bar = msg[1]
                    ts = pd.Timestamp(bar.dt).as_unit('ns')
                    chart.update(pd.Series({
                        'time': ts,
                        'open': bar.open, 'high': bar.high,
                        'low': bar.low, 'close': bar.close,
                        'volume': bar.volume,
                    }))
                    self._closes.append(bar.close)
                    bb = _compute_bb_point(
                        self._closes, self._bb_period, self._bb_std,
                    )
                    if bb:
                        u, m, l = bb
                        line_upper.update(pd.Series({'time': ts, 'BB Upper': u}))
                        line_middle.update(pd.Series({'time': ts, 'BB Middle': m}))
                        line_lower.update(pd.Series({'time': ts, 'BB Lower': l}))

                elif kind == "partial":
                    bar = msg[1]
                    if bar is None:
                        continue
                    ts = pd.Timestamp(bar.dt).as_unit('ns')
                    chart.update(pd.Series({
                        'time': ts,
                        'open': bar.open, 'high': bar.high,
                        'low': bar.low, 'close': bar.close,
                        'volume': bar.volume,
                    }))
                    tentative = self._closes + [bar.close]
                    bb = _compute_bb_point(
                        tentative, self._bb_period, self._bb_std,
                    )
                    if bb:
                        u, m, l = bb
                        line_upper.update(pd.Series({'time': ts, 'BB Upper': u}))
                        line_middle.update(pd.Series({'time': ts, 'BB Middle': m}))
                        line_lower.update(pd.Series({'time': ts, 'BB Lower': l}))

                elif kind == "trade":
                    trade, index = msg[1], msg[2]
                    candle_times = chart.candle_data['time'].tolist()
                    n = len(candle_times)
                    ei, xi = trade.entry_bar_index, trade.exit_bar_index

                    if 0 <= ei < n:
                        if trade.side == OrderSide.LONG:
                            _add_marker(chart, candle_times[ei], 'belowBar', 'arrowUp',
                                        '#1565C0', f"#{index+1} Entry {trade.side.value} | {trade.tag}")
                        else:
                            _add_marker(chart, candle_times[ei], 'aboveBar', 'arrowDown',
                                        '#E040FB', f"#{index+1} Entry {trade.side.value} | {trade.tag}")

                    if 0 <= xi < n:
                        pnl_str = f"{trade.pnl:+,}"
                        bars_held = xi - ei
                        if trade.pnl >= 0:
                            color = '#00BCD4'
                        elif trade.side == OrderSide.LONG:
                            color = '#E040FB'
                        else:
                            color = '#FF5252'
                        if trade.side == OrderSide.SHORT:
                            _add_marker(chart, candle_times[xi], 'belowBar', 'arrowUp',
                                        color, f"#{index+1} Exit ({trade.exit_tag}) P&L:{pnl_str} [{bars_held}bars]")
                        else:
                            _add_marker(chart, candle_times[xi], 'aboveBar', 'arrowDown',
                                        color, f"#{index+1} Exit ({trade.exit_tag}) P&L:{pnl_str} [{bars_held}bars]")

                    chart._update_markers()

            except Exception:
                if not self._alive:
                    break
