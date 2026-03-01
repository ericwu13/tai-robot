"""Backtest chart: candlestick chart with entry/exit markers using mplfinance."""

from __future__ import annotations

import math

from ..market_data.models import Bar
from .broker import Trade, OrderSide

try:
    import pandas as pd
    import mplfinance as mpf
    _MPF_AVAILABLE = True
except ImportError:
    _MPF_AVAILABLE = False

# Bars of context to show before entry / after exit when zooming to a trade
_PAD_BEFORE = 20
_PAD_AFTER = 10


def _compute_bollinger(
    bars: list[Bar], period: int = 20, num_std: float = 2.0,
) -> tuple[list[float], list[float], list[float]]:
    """Compute Bollinger Bands for a list of bars. Returns (upper, middle, lower)."""
    n = len(bars)
    upper = [float("nan")] * n
    middle = [float("nan")] * n
    lower = [float("nan")] * n

    for i in range(period - 1, n):
        window = [bars[j].close for j in range(i - period + 1, i + 1)]
        mean = sum(window) / period
        variance = sum((v - mean) ** 2 for v in window) / period
        std = math.sqrt(variance)
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
    if not _MPF_AVAILABLE:
        raise ImportError(
            "mplfinance and pandas are required for charting. "
            "Install with: pip install mplfinance pandas"
        )

    if not bars:
        return

    # Compute Bollinger Bands on full bar series (so edge bars have values)
    bb_upper, bb_middle, bb_lower = _compute_bollinger(bars, bb_period, bb_std)

    # Determine visible window
    n = len(bars)
    view_start = 0
    view_end = n

    if focus_trade_index is not None and 0 <= focus_trade_index < len(trades):
        ft = trades[focus_trade_index]
        view_start = max(0, ft.entry_bar_index - _PAD_BEFORE)
        view_end = min(n, ft.exit_bar_index + _PAD_AFTER + 1)
        trade_num = focus_trade_index + 1
        total = len(trades)
        title = f"{title}  —  Trade #{trade_num}/{total}" if title else f"Trade #{trade_num}/{total}"

    view_bars = bars[view_start:view_end]
    if not view_bars:
        return

    # Build DataFrame with DatetimeIndex (mplfinance requirement)
    data = {
        "Open": [b.open for b in view_bars],
        "High": [b.high for b in view_bars],
        "Low": [b.low for b in view_bars],
        "Close": [b.close for b in view_bars],
        "Volume": [b.volume for b in view_bars],
    }
    index = pd.DatetimeIndex([b.dt for b in view_bars])
    df = pd.DataFrame(data, index=index)

    # Slice Bollinger Bands to view window
    view_bb_upper = bb_upper[view_start:view_end]
    view_bb_middle = bb_middle[view_start:view_end]
    view_bb_lower = bb_lower[view_start:view_end]

    # Build marker series (NaN where no marker) and tooltip lookup
    # mplfinance uses integer x-axis (0..N-1) internally
    vn = len(view_bars)
    entry_long = [float("nan")] * vn
    entry_short = [float("nan")] * vn
    exit_win = [float("nan")] * vn
    exit_loss = [float("nan")] * vn

    # Map (x_index, y_price) -> tooltip text for click detection
    marker_info: list[tuple[int, float, str]] = []

    for t in trades:
        ei = t.entry_bar_index - view_start
        xi = t.exit_bar_index - view_start

        if 0 <= ei < vn:
            if t.side == OrderSide.LONG:
                entry_long[ei] = t.entry_price
            else:
                entry_short[ei] = t.entry_price
            entry_dt = view_bars[ei].dt.strftime("%Y-%m-%d %H:%M")
            marker_info.append((ei, t.entry_price,
                f"Entry {t.side.value}\n"
                f"Tag: {t.tag}\n"
                f"Date: {entry_dt}\n"
                f"Price: {t.entry_price:,}"))

        if 0 <= xi < vn:
            if t.pnl >= 0:
                exit_win[xi] = t.exit_price
            else:
                exit_loss[xi] = t.exit_price
            exit_dt = view_bars[xi].dt.strftime("%Y-%m-%d %H:%M")
            bars_held = t.exit_bar_index - t.entry_bar_index
            marker_info.append((xi, t.exit_price,
                f"Exit ({t.exit_tag})\n"
                f"Date: {exit_dt}\n"
                f"Price: {t.exit_price:,}\n"
                f"P&L: {t.pnl:+,}\n"
                f"Bars held: {bars_held}"))

    # Build addplot list — Bollinger Bands first (lines behind markers)
    add_plots = []

    add_plots.append(mpf.make_addplot(
        view_bb_upper, type="line", color="dodgerblue", width=1, alpha=0.7,
    ))
    add_plots.append(mpf.make_addplot(
        view_bb_middle, type="line", color="orange", width=1, alpha=0.8,
    ))
    add_plots.append(mpf.make_addplot(
        view_bb_lower, type="line", color="dodgerblue", width=1, alpha=0.7,
    ))

    # Trade markers — use colors distinct from red/green candles
    if any(not _isnan(v) for v in entry_long):
        add_plots.append(mpf.make_addplot(
            entry_long, type="scatter", markersize=120,
            marker="^", color="#1565C0",
        ))
    if any(not _isnan(v) for v in entry_short):
        add_plots.append(mpf.make_addplot(
            entry_short, type="scatter", markersize=120,
            marker="v", color="#E040FB",
        ))
    if any(not _isnan(v) for v in exit_win):
        add_plots.append(mpf.make_addplot(
            exit_win, type="scatter", markersize=120,
            marker="x", color="#00BCD4",
        ))
    if any(not _isnan(v) for v in exit_loss):
        add_plots.append(mpf.make_addplot(
            exit_loss, type="scatter", markersize=120,
            marker="x", color="#E040FB",
        ))

    chart_title = title or "Backtest"

    fig, axes = mpf.plot(
        df,
        type="candle",
        style="charles",
        title=chart_title,
        ylabel="Price",
        volume=True,
        addplot=add_plots if add_plots else None,
        figscale=1.3,
        figratio=(16, 9),
        tight_layout=True,
        warn_too_much_data=len(df) + 1,
        returnfig=True,
    )

    # Add legend to the price axis (axes[0])
    from matplotlib.lines import Line2D
    legend_handles = [
        Line2D([0], [0], color="dodgerblue", linewidth=1, label=f"BB Upper/Lower ({bb_period}, {bb_std})"),
        Line2D([0], [0], color="orange", linewidth=1, label=f"BB Middle (SMA {bb_period})"),
        Line2D([0], [0], marker="^", color="#1565C0", linestyle="None", markersize=8, label="Entry Long"),
        Line2D([0], [0], marker="v", color="#E040FB", linestyle="None", markersize=8, label="Entry Short"),
        Line2D([0], [0], marker="x", color="#00BCD4", linestyle="None", markersize=8, label="Exit (Win)"),
        Line2D([0], [0], marker="x", color="#E040FB", linestyle="None", markersize=8, label="Exit (Loss)"),
    ]
    axes[0].legend(handles=legend_handles, loc="upper left", fontsize=8, framealpha=0.8)

    # Click-to-show tooltip on markers
    ax = axes[0]
    tooltip = ax.annotate(
        "", xy=(0, 0), xytext=(15, 15), textcoords="offset points",
        bbox=dict(boxstyle="round,pad=0.5", fc="#ffffcc", ec="gray", alpha=0.95),
        fontsize=9, family="monospace",
        arrowprops=dict(arrowstyle="->", color="gray"),
        visible=False, zorder=100,
    )

    def _on_click(event):
        if event.inaxes != ax or event.xdata is None:
            return
        # Find nearest marker within threshold
        best_dist = float("inf")
        best_text = ""
        # Convert price range to get a reasonable y-threshold
        ylim = ax.get_ylim()
        y_range = ylim[1] - ylim[0]
        xlim = ax.get_xlim()
        x_range = xlim[1] - xlim[0]
        for mx, my, text in marker_info:
            # Normalize distances so x and y are comparable
            dx = (event.xdata - mx) / max(x_range, 1) * 100
            dy = (event.ydata - my) / max(y_range, 1) * 100
            dist = dx * dx + dy * dy
            if dist < best_dist:
                best_dist = dist
                best_text = text
                best_x, best_y = mx, my
        # Threshold: must be reasonably close
        if best_dist < 25 and best_text:
            tooltip.xy = (best_x, best_y)
            tooltip.set_text(best_text)
            tooltip.set_visible(True)
        else:
            tooltip.set_visible(False)
        fig.canvas.draw_idle()

    fig.canvas.mpl_connect("button_press_event", _on_click)

    # Scroll wheel zoom (centered on cursor)
    vol_ax = axes[2] if len(axes) > 2 else None

    def _on_scroll(event):
        if event.inaxes not in (ax, vol_ax) or event.xdata is None:
            return
        scale = 0.8 if event.button == "up" else 1.25  # zoom in / out
        xlim = ax.get_xlim()
        x_center = event.xdata
        new_half = (xlim[1] - xlim[0]) * scale / 2
        new_left = max(-0.5, x_center - new_half)
        new_right = min(vn - 0.5, x_center + new_half)
        ax.set_xlim(new_left, new_right)
        if vol_ax:
            vol_ax.set_xlim(new_left, new_right)
        # Auto-adjust y-axis to visible data
        lo = max(0, int(new_left + 0.5))
        hi = min(vn, int(new_right + 0.5))
        if lo < hi:
            visible_lows = [view_bars[i].low for i in range(lo, hi)]
            visible_highs = [view_bars[i].high for i in range(lo, hi)]
            y_min = min(visible_lows)
            y_max = max(visible_highs)
            margin = (y_max - y_min) * 0.05
            ax.set_ylim(y_min - margin, y_max + margin)
        fig.canvas.draw_idle()

    fig.canvas.mpl_connect("scroll_event", _on_scroll)

    import matplotlib.pyplot as plt
    plt.show()


def _isnan(v: float) -> bool:
    return v != v
