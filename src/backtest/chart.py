"""Backtest chart: candlestick chart with entry/exit markers using lightweight-charts."""

from __future__ import annotations

import math

from ..market_data.models import Bar
from .broker import Trade, OrderSide

try:
    import pandas as pd
    from lightweight_charts import Chart
    _LWC_AVAILABLE = True
except ImportError:
    _LWC_AVAILABLE = False

# Bars of context to show before entry / after exit when zooming to a trade
_PAD_BEFORE = 20
_PAD_AFTER = 10


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
    chart.legend(visible=True)
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
