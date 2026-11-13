"""AI Review payload text — the exact strings the workbench sends.

Pure functions (no Tk, no network) shared by ``run_backtest.py``
(AI Review button, Bot Evolution payload) and ``scripts/research/chat.py``
(the headless research loop), so the two can never drift apart.
"""

from __future__ import annotations


def review_timeframe_line(strategy_cls) -> str:
    """Return the Strategy Timeframe + Timezone note for AI Review prompts.

    Explicit here so the AI does not mis-infer the bar interval from
    session-aligned :45 timestamps (e.g. 09:45/10:45/13:45 for 60-min
    AM bars can look like 15-min bars to a naive reader).
    """
    timeframe_line = ""
    if strategy_cls is not None:
        kt = getattr(strategy_cls, "kline_type", 0)
        km = getattr(strategy_cls, "kline_minute", 0)
        if kt == 0 and km:
            if km % 60 == 0:
                label = f"{km // 60}-hour ({km}-minute)"
            else:
                label = f"{km}-minute"
            timeframe_line = (
                f"策略時框 Strategy Timeframe: {label} bars "
                f"(kline_type={kt}, kline_minute={km})\n"
            )
        elif kt == 4:
            timeframe_line = f"策略時框 Strategy Timeframe: Daily bars (kline_type={kt})\n"
        elif kt == 5:
            timeframe_line = f"策略時框 Strategy Timeframe: Weekly bars (kline_type={kt})\n"
        elif kt == 6:
            timeframe_line = f"策略時框 Strategy Timeframe: Monthly bars (kline_type={kt})\n"
    timeframe_line += (
        "時區 Timezone: 所有 entry/exit 時間皆為台灣時間 (TWT/UTC+8)。\n"
        "All entry/exit timestamps are Taiwan time (TWT/UTC+8). "
        "Bars are TAIFEX session-aligned, so 60-minute AM bars may carry "
        ":45 timestamps (08:45–13:45 session) — these are NOT 15-minute bars.\n"
    )
    return timeframe_line


def source_section(strategy_source: str) -> str:
    """The strategy-source block appended after the trade list ('' when unknown)."""
    if not strategy_source:
        return ""
    return (
        f"\n\n策略原始碼 Strategy Source Code:\n"
        f"```python\n{strategy_source}\n```"
    )


def trade_lines(trades) -> list[str]:
    """One line per trade — ALL trades, no cap (chunked delivery handles size)."""
    lines = []
    for i, t in enumerate(trades, 1):
        bars_held = t.exit_bar_index - t.entry_bar_index
        entry_dt = t.entry_dt or f"bar#{t.entry_bar_index}"
        exit_dt = t.exit_dt or f"bar#{t.exit_bar_index}"
        src_mark = " [real]" if getattr(t, "source", "") == "real" else ""
        lines.append(
            f"  {i}. {t.side.value} {t.tag}: "
            f"entry={entry_dt} @{t.entry_price:,} → "
            f"exit={exit_dt} @{t.exit_price:,} "
            f"P&L={t.pnl:+,} ({bars_held} bars){src_mark}"
        )
    return lines


def degradation_section(prev: dict | None, cur_pf: float, cur_dd: float) -> str:
    """REVERT-FIRST block when PF dropped or DD grew vs the previous review.

    ``prev`` is ``{"pf": float, "dd_pct": float}`` from the previous round,
    or None on the first review of a conversation.
    """
    if prev is None:
        return ""
    prev_pf = prev["pf"]
    prev_dd = prev["dd_pct"]
    pf_worse = cur_pf < prev_pf
    dd_worse = cur_dd > prev_dd
    if not (pf_worse or dd_worse):
        return ""
    return (
        f"## ⚠ DEGRADATION DETECTED — REVERT FIRST ⚠\n"
        f"Previous review metrics: PF={prev_pf:.2f}, DD={prev_dd:.2f}%\n"
        f"Current metrics:         PF={cur_pf:.2f}, DD={cur_dd:.2f}%\n"
        f"{'  → Profit Factor DROPPED' if pf_worse else ''}"
        f"{'  → Max Drawdown INCREASED' if dd_worse else ''}\n\n"
        f"**MANDATORY**: The last change made things worse. Your ONLY "
        f"recommendation must be to REVERT the last change and return to "
        f"the previous version. Do NOT suggest new changes on top of a "
        f"degraded strategy. Do NOT try to fix the degradation by adding "
        f"more complexity. Say: \"上一次的修改使績效惡化，建議還原到修改前的版本。"
        f"The last change degraded performance — revert to the prior version "
        f"before attempting any new changes.\"\n"
        f"The ONLY exception is if you identify an obvious code BUG (e.g. "
        f"wrong operator, missing round()) that clearly caused the "
        f"degradation — in that case, suggest fixing that specific bug only.\n\n"
    )


def review_preamble(report: str, timeframe_line: str, degradation: str = "") -> str:
    """Preamble of the AI Review message: rules, report, timeframe note."""
    return (
        f"以下是回測/實盤結果，請根據策略原始碼分析交易表現。\n"
        f"Below are the backtest/live results. Analyze the trading performance.\n\n"
        f"{degradation}"
        f"## Analysis rules (MUST FOLLOW)\n"
        f"1. **Identify what is WORKING first** — list 2-3 aspects that should NOT be changed.\n"
        f"2. **Suggest at most ONE change** — never multiple simultaneous changes. "
        f"Each change must be tested in isolation before adding the next.\n"
        f"3. **Distinguish bugs from tuning** — a code bug (wrong comparison, missing round()) "
        f"is a fix. Changing parameters or adding filters is tuning. Fix bugs first; "
        f"tune ONE parameter at a time only.\n"
        f"4. **MANDATORY: winner/loser impact count** — before suggesting ANY filter, "
        f"condition, or parameter change, you MUST scan the trade list and estimate:\n"
        f"   - How many LOSING trades would the change remove/avoid?\n"
        f"   - How many WINNING trades would the change also remove/avoid?\n"
        f"   - Present this as: \"Estimated impact: removes ~N losers, ~M winners\"\n"
        f"   - If you cannot estimate (e.g. insufficient data), say so explicitly "
        f"and do NOT recommend the change. A suggestion without impact analysis is "
        f"not allowed.\n"
        f"5. **Bias toward simplicity** — adding indicators, conditions, or parameters "
        f"rarely improves robustness. If the strategy has >4 entry conditions, "
        f"suggest REMOVING one before adding another.\n"
        f"6. **No fabricated statistics** — do not invent expected win rates or "
        f"improvements. Say \"建議回測驗證 / backtest to verify\" instead.\n"
        f"7. **If metrics are acceptable** (PF > 1.3, DD < 25%, WR > 40%), "
        f"say so and suggest NOT changing the strategy. Over-optimization is the "
        f"#1 cause of strategy degradation.\n\n"
        f"{report}\n\n"
        f"{timeframe_line}\n"
    )


def single_shot_context(preamble: str, lines: list[str], source: str,
                        total_trades: int) -> str:
    """The full single-message AI Review context (what single-shot mode sends)."""
    return (
        preamble
        + f"交易明細 Trade Details ({total_trades} trades):\n"
        + "\n".join(lines)
        + source
    )


def trimmed_review_stub(total_trades: int) -> str:
    """What replaces the trade-dump user turn in history after the reply."""
    return f"[trade review payload — {total_trades} trades, trimmed to save tokens]"
