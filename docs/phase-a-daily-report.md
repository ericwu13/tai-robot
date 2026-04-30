# Phase A: Daily Report Pipeline + Market Regime Classifier

## 1. Overview

Phase A is the **data foundation** for an automated strategy refinement system. The
full system will eventually run a closed loop:

```
Phase A (this PR)     Phase B (planned)        Phase C (planned)
  Daily Reports    ->  Walk-Forward Validator -> AI Diagnosis & Tuning
  Regime Classifier    Out-of-sample testing     Automated param tweaks
  Strategy Changelog   Performance grading       Feedback into changelog
```

Phase A answers the question: **"What happened today, and what was the market
doing?"** It produces structured, machine-readable data that Phase B and C
will consume to evaluate strategy fitness and propose improvements.

### What Phase A provides

| Component | Purpose |
|-----------|---------|
| **Daily Report Generator** | Per-day JSON reports with trade details, summary metrics, and market context |
| **Market Regime Classifier** | Labels the current market state (trending, range-bound, volatile, etc.) using ADX/ATR/EMA |
| **Strategy Changelog** | Audit trail of every strategy parameter change with before/after metrics |
| **CLI** | Command-line interface to list and view saved reports |
| **Discord Integration** | Posts daily report summaries to a Discord channel |

### What Phase A does NOT do (yet)

- It does not automatically trigger at the end of each trading session (that's a Phase B integration)
- It does not analyze whether a strategy should change (Phase C)
- It does not modify strategy parameters (Phase C)


## 2. What Triggers the Daily Report

### Automatic: LiveRunner post-session (built-in)

When `LiveRunner.stop()` is called — either by the user clicking "Stop Bot"
or by the GUI's end-of-session logic — it **automatically generates a daily
report** before releasing resources. The call chain is:

```
LiveRunner.stop()
  -> self._auto_save_session()      # save broker state
  -> self._generate_daily_report()  # NEW: auto-generate report
  -> self.csv_logger.close()        # close log files
  -> self.release_lock()
  -> state = STOPPED
```

The report is written to `data/daily-reports/YYYY-MM-DD.json` and an
`on_daily_report` event is emitted so the GUI can forward it to Discord or
display it. This is best-effort — if report generation fails for any reason,
it is silently caught and the bot stop proceeds normally.

The data used:
- **Trades**: from `self.broker.trades` (the same `SimulatedBroker` used by
  the strategy during the session)
- **Bar data**: from `self.data_store` (highs/lows/closes for regime
  classification)
- **Metadata**: `strategy_display_name`, `point_value`, `symbol` — all
  already available on the `LiveRunner` instance

### Manual: from backtest results

After running a backtest, call `generate_report_from_backtest()` with the
engine's output:

```python
reports = generate_report_from_backtest(
    trades=result.trades,
    equity_curve=result.equity_curve,
    bars_highs=[b.high for b in bars],
    bars_lows=[b.low for b in bars],
    bars_closes=[b.close for b in bars],
    strategy_name="H4 Bollinger Long",
    point_value=200,
    symbol="TXF1",
)
```

This splits all trades by exit date and writes one report per day.

### Hooking into Discord

To send the report to Discord when the live bot stops, listen for the
`on_daily_report` event:

```python
runner.on("on_daily_report", lambda report: discord_notifier.daily_report(report))
```

The GUI can wire this up at deployment time alongside the existing Discord
notifier setup.


## 3. Strategy Compatibility

### Short answer: works with ANY strategy

The report generator consumes `list[Trade]` from `src.backtest.broker.Trade`.
Every strategy in the system — whether hand-written, AI-generated, or loaded
from the strategy store — produces trades through the same `SimulatedBroker`
and therefore outputs the same `Trade` dataclass.

### Strategy types that are compatible

| Strategy type | How it produces trades | Compatible? |
|---------------|----------------------|-------------|
| `BacktestStrategy` subclass (e.g. `H4BollingerLong`) | Calls `broker.entry()`/`exit()`/`close()` directly | Yes |
| `AbstractStrategy` subclass (via `SignalStrategyAdapter`) | Returns `Signal` objects, adapter maps to broker calls | Yes |
| AI-generated strategies (from the Workbench) | Generated code calls `broker.entry()`/`exit()`/`close()` | Yes |
| Live deployed strategies | Same `SimulatedBroker` instance in `LiveRunner` | Yes |

### Trade dataclass fields the report uses

```python
@dataclass
class Trade:
    tag: str              # Entry order name (e.g. "Long", "Short")
    side: OrderSide       # LONG or SHORT
    qty: int              # Position size
    entry_price: int      # Entry fill price (raw integer)
    exit_price: int       # Exit fill price (raw integer)
    entry_bar_index: int  # Bar index at entry
    exit_bar_index: int   # Bar index at exit
    pnl: int              # Realized P&L in points
    exit_tag: str         # What triggered exit ("limit", "stop", "close", "force_close")
    entry_dt: str         # "YYYY-MM-DD HH:MM" format
    exit_dt: str          # "YYYY-MM-DD HH:MM" format
    real_entry_price: int # Broker-confirmed fill (live mode only, 0 if paper)
    real_exit_price: int  # (not yet captured)
```

All fields are populated by `SimulatedBroker` during both backtesting and live
trading. The report generator uses every field above. `exit_dt` is required for
date grouping — trades without `exit_dt` (i.e. still open) are excluded from
reports.

### Graceful field handling

The report generator uses `getattr()` with defaults for every field, so it
will not crash if a trade object is missing optional fields:

| Field | Required? | What happens if missing/zero |
|-------|-----------|----------------------------|
| `tag` | No | Empty string in report |
| `side` | Yes | Falls back to `str(side)` if not an enum |
| `qty` | No | Defaults to 1 |
| `entry_price`, `exit_price` | No | Defaults to 0 |
| `entry_dt`, `exit_dt` | Soft | Empty string; trade excluded from date grouping if `exit_dt` is empty |
| `pnl` | No | Defaults to 0 |
| `entry_bar_index`, `exit_bar_index` | No | Defaults to 0; `bars_held` = 0 |
| `exit_tag` | No | Empty string |
| `real_entry_price`, `real_exit_price` | No | Shows as `null` in JSON (paper mode or unconfirmed) |

### Assumptions

- **Prices are raw integers** as returned by the Capital API (TAIFEX convention).
  The `point_value` parameter on the report generator converts raw P&L to
  currency P&L (e.g. `pnl * 200` for TX futures, `pnl * 50` for MTX).
- **`exit_dt` should be a string starting with "YYYY-MM-DD"**. This is the
  format `SimulatedBroker` produces. Trades without a valid `exit_dt` are
  excluded from date-grouped reports but do not cause errors.
- **Bar data for regime classification** (highs/lows/closes as `list[int]`)
  is optional. If not provided (or if `DataStore` raises an error), the
  `market_regime` field in the report is `null`.


## 4. How to Run It

### Prerequisites

**No new dependencies.** Phase A uses only the standard library (`json`,
`pathlib`, `dataclasses`, `argparse`, `math`) plus existing project modules
(`src.backtest.broker`, `src.backtest.metrics`, `src.strategy.indicators`).

### Automatic generation (live trading)

When you deploy a live bot and later stop it (via the GUI or session end),
the daily report is generated automatically. No setup required.

The report file appears at `data/daily-reports/YYYY-MM-DD.json` and an
`on_daily_report` event fires so the GUI can display it or forward to Discord.

### CLI commands

All commands are run from the project root directory.

#### List available reports

```bash
python -m src.daily_report --list
```

Output:
```
Available reports (3):
  2026-04-09
  2026-04-10
  2026-04-11
```

#### View a specific report

```bash
python -m src.daily_report --date 2026-04-11 --show
```

Output (truncated):
```json
{
  "date": "2026-04-11",
  "symbol": "TXF1",
  "generated_at": "2026-04-11 14:30:00",
  "strategy": {
    "name": "H4 Bollinger Long",
    "version": "2.1",
    "params": {"bb_period": 20, "bb_std": 2.0}
  },
  "trades": [
    {
      "tag": "Long",
      "side": "LONG",
      "qty": 1,
      "entry_price": 22450,
      "exit_price": 22520,
      "entry_dt": "2026-04-11 09:15",
      "exit_dt": "2026-04-11 11:30",
      "pnl": 70,
      "pnl_currency": 14000,
      "bars_held": 9,
      "exit_tag": "limit",
      "real_entry_price": null,
      "real_exit_price": null
    }
  ],
  "summary": {
    "total_trades": 1,
    "winning_trades": 1,
    "losing_trades": 0,
    "win_rate": 1.0,
    "total_pnl": 70,
    "profit_factor": "Infinity",
    "max_drawdown": 0,
    ...
  },
  "market_regime": {
    "label": "trending-up",
    "trend_strength": "trending",
    "volatility": "normal",
    "direction": "bullish",
    "adx": 32.45,
    "plus_di": 28.12,
    "minus_di": 14.33,
    "atr": 85.6,
    "atr_ratio": 1.05,
    "ema_50": 22380.5,
    "last_close": 22520
  }
}
```

#### Default (no flags)

```bash
python -m src.daily_report --date 2026-04-11
```

If a report already exists for that date, prints a message to use `--show`.
Otherwise, tells you reports are generated by the backtest engine or live runner.

### Generating reports programmatically

#### From a backtest result

```python
from src.daily_report import generate_report_from_backtest

# After running BacktestEngine.run(bars):
reports = generate_report_from_backtest(
    trades=result.trades,
    equity_curve=result.equity_curve,
    bars_highs=[b.high for b in bars],
    bars_lows=[b.low for b in bars],
    bars_closes=[b.close for b in bars],
    strategy_name="H4 Bollinger Long",
    strategy_version="2.1",
    strategy_params={"bb_period": 20, "bb_std": 2.0},
    point_value=200,       # TX futures
    symbol="TXF1",
)
# Returns a list of dicts, one per trading day
# Files saved to data/daily-reports/YYYY-MM-DD.json
```

#### From live trading (single day)

```python
from src.daily_report import generate_daily_report

report = generate_daily_report(
    date="2026-04-11",
    trades=broker.trades,  # today's closed trades
    bars_highs=highs_list,
    bars_lows=lows_list,
    bars_closes=closes_list,
    strategy_name="M1 SMA Cross",
    strategy_version="1.0",
    strategy_params={"fast": 3, "slow": 8},
    point_value=50,        # MTX futures
    symbol="TMF1",
)
```

### Where reports are stored

```
data/
  daily-reports/
    2026-04-09.json
    2026-04-10.json
    2026-04-11.json
  changelog.json
```

The `data/` directory is gitignored. Reports persist locally on the machine
running the bot.

### Discord integration

The existing `DiscordNotifier` class now has a `daily_report()` method. If you
already have Discord configured in `settings.yaml` (with `bot_token` and
`channel_id`), you can send a report summary:

```python
from src.live.discord_notify import DiscordNotifier

notifier = DiscordNotifier(
    bot_token="your-bot-token",
    channel_id="your-channel-id",
    bot_name="MyBot",
    symbol="TXF1",
)
notifier.daily_report(report)  # pass the dict from generate_daily_report()
```

The Discord message includes: date, strategy name, trade count, win rate,
profit factor, P&L, max drawdown, and the regime label (if available).

There is no standalone `/report` Discord slash command yet. The `daily_report()`
method is a fire-and-forget REST call, same pattern as the existing
`order_sent()`, `fill_confirmed()`, etc.


## 5. Market Regime Classifier

### How it works

The classifier takes bar data (highs, lows, closes as integer lists) and
computes three independent dimensions:

```
                    ┌─────────────────────────┐
   Bar data ------->│  ADX (14-period)        │-----> Trend strength
   (H, L, C)       │  +DI, -DI               │       trending / range-bound / transitional
                    ├─────────────────────────┤
                    │  ATR (14-period)        │-----> Volatility
                    │  ATR ratio = curr / avg │       high / normal / low
                    ├─────────────────────────┤
                    │  EMA (50-period)        │-----> Direction
                    │  close vs EMA           │       bullish / bearish
                    └─────────────────────────┘
                              │
                              v
                    ┌─────────────────────────┐
                    │  Combine into label     │
                    └─────────────────────────┘
```

### Trend strength (ADX)

| ADX value | Classification |
|-----------|---------------|
| > 25 | **Trending** — strong directional movement |
| < 20 | **Range-bound** — no clear trend |
| 20-25 | **Transitional** — trend emerging or fading |

### Volatility (ATR ratio)

ATR ratio = current ATR(14) / average of the last 20 ATR values.

| ATR ratio | Classification |
|-----------|---------------|
| > 1.3 | **High** — volatility spike, 30%+ above recent average |
| < 0.7 | **Low** — unusually quiet, 30%+ below recent average |
| 0.7-1.3 | **Normal** — within typical range |

### Direction (EMA)

| Close vs EMA(50) | Classification |
|-------------------|---------------|
| Close >= EMA | **Bullish** |
| Close < EMA | **Bearish** |

### Combined regime labels

The three dimensions combine according to these priority rules:

| Regime label | When assigned |
|-------------|---------------|
| `high-volatility` | ATR ratio > 1.3 (overrides trend/direction) |
| `trending-up` | ADX > 25 and bullish, normal/low volatility |
| `trending-down` | ADX > 25 and bearish, normal/low volatility |
| `range-bound` | ADX < 20, normal volatility |
| `low-volatility-chop` | ADX < 20 and ATR ratio < 0.7 |
| `transitional-bullish` | ADX 20-25, bullish, normal/low volatility |
| `transitional-bearish` | ADX 20-25, bearish, normal/low volatility |

High volatility takes top priority because it signals a regime that affects all
strategies regardless of trend/direction.

### Raw indicator values

Every `RegimeResult` includes the raw values so that downstream AI analysis
(Phase C) can make nuanced decisions beyond the simple label:

- `adx` — trend strength 0-100
- `plus_di` — bullish directional pressure 0-100
- `minus_di` — bearish directional pressure 0-100
- `atr` — current Average True Range (raw integer, same scale as prices)
- `atr_ratio` — current ATR / 20-bar trailing average ATR
- `ema_50` — 50-period EMA value
- `last_close` — most recent closing price

### Data requirements

The classifier requires enough bars for the longest lookback:
- ADX(14) needs at least `2 * 14 + 1 = 29` bars
- EMA(50) needs at least 50 bars
- ATR ratio needs `14 + 1 + 20 = 35` bars for the trailing average

**Minimum: 50 bars.** If insufficient data is provided, `classify_regime()`
returns `None`.

### Customizing periods

```python
from src.daily_report import classify_regime

result = classify_regime(
    highs, lows, closes,
    adx_period=20,       # default 14
    atr_period=20,       # default 14
    ema_period=100,      # default 50
    atr_avg_period=30,   # default 20
)
```


## 6. Strategy Changelog

### How it works

The changelog is an append-only JSON array in `data/changelog.json`. Each entry
records a strategy modification with enough context for Phase C's AI to
understand the history of changes and their effects.

### Entry structure

```json
{
  "date": "2026-04-11 14:30:00",
  "strategy": "H4 Bollinger Long",
  "version_before": "2.0",
  "version_after": "2.1",
  "change_summary": "Tightened stop loss from 2x ATR to 1.5x ATR",
  "initiated_by": "ai",
  "metrics_before": {
    "win_rate": 0.45,
    "profit_factor": 1.2,
    "max_drawdown": 5000
  },
  "metrics_after": {
    "win_rate": 0.50,
    "profit_factor": 1.5,
    "max_drawdown": 3500
  },
  "params_before": {"atr_mult": 2.0},
  "params_after": {"atr_mult": 1.5}
}
```

### API

```python
from src.daily_report import append_changelog, load_changelog, recent_changes

# Record a change
append_changelog(
    strategy_name="H4 Bollinger Long",
    version_before="2.0",
    version_after="2.1",
    change_summary="Tightened stop loss from 2x ATR to 1.5x ATR",
    initiated_by="ai",            # or "human"
    metrics_before={"win_rate": 0.45, "profit_factor": 1.2},
    metrics_after={"win_rate": 0.50, "profit_factor": 1.5},
    params_before={"atr_mult": 2.0},
    params_after={"atr_mult": 1.5},
)

# Read all entries
all_entries = load_changelog()     # list[dict], chronological order

# Get last N entries (newest first)
last_5 = recent_changes(n=5)      # list[dict], reverse chronological
```

### Storage

- **File**: `data/changelog.json` (gitignored, local to the machine)
- **Format**: JSON array, pretty-printed with 2-space indent, UTF-8
- **Resilience**: if the file is corrupted or contains non-array JSON,
  `load_changelog()` returns an empty list instead of raising an exception


## 7. Configuration

### No new configuration required

Phase A introduces **no new config files, environment variables, or
settings.yaml entries**. It uses:

- Existing project structure (`data/` directory for output)
- Existing indicator implementations from `src/strategy/indicators/`
- Existing `Trade` dataclass from `src/backtest/broker`
- Existing `calculate_metrics()` from `src/backtest/metrics`
- Existing Discord notifier pattern from `src/live/discord_notify`

### Optional: Discord setup

If you want Discord report notifications, ensure `settings.yaml` has:

```yaml
discord:
  bot_token: "your-discord-bot-token"
  channel_id: "your-channel-id"
```

This is the same configuration the live trading bot already uses for order
and fill notifications. No additional Discord setup is needed.

### Report output directory

Reports are written to `data/daily-reports/`. This directory is created
automatically on first report generation. It lives under the gitignored
`data/` directory, so reports stay local.


## 8. Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────────┐
│                        tai-robot system                             │
│                                                                     │
│  ┌──────────────┐     ┌──────────────┐     ┌──────────────────┐    │
│  │   Backtest    │     │  Live Runner  │     │  AI Workbench    │    │
│  │   Engine      │     │  (LiveRunner) │     │  (code gen)      │    │
│  │              │     │              │     │                  │    │
│  │  (manual     │     │  stop() AUTO │     │                  │    │
│  │   call)      │     │  triggers    │     │                  │    │
│  └──────┬───────┘     └──────┬───────┘     └────────┬─────────┘    │
│         │                    │                       │              │
│         │  list[Trade]       │  list[Trade]          │ param change │
│         │  list[Bar]         │  list[Bar]            │              │
│         v                    v                       v              │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │              src/daily_report/  (Phase A)                    │   │
│  │                                                              │   │
│  │  ┌────────────────────┐    ┌──────────────────────────────┐ │   │
│  │  │  report_generator   │    │  regime_classifier           │ │   │
│  │  │                    │    │                              │ │   │
│  │  │  generate_daily_   │    │  classify_regime(H, L, C)    │ │   │
│  │  │  report()     ─────┼───>│    ├─ ADX(14) -> trend       │ │   │
│  │  │                    │    │    ├─ ATR ratio -> volatility │ │   │
│  │  │  generate_report_  │    │    └─ EMA(50) -> direction   │ │   │
│  │  │  from_backtest()   │    │                              │ │   │
│  │  │                    │    │  Returns: RegimeResult       │ │   │
│  │  │  Uses:             │    └──────────────────────────────┘ │   │
│  │  │  calculate_metrics │                                      │   │
│  │  │  (from backtest)   │    ┌──────────────────────────────┐ │   │
│  │  └────────┬───────────┘    │  changelog                   │ │   │
│  │           │                │                              │ │   │
│  │           │                │  append_changelog()          │ │   │
│  │           │                │  load_changelog()            │ │   │
│  │           │                │  recent_changes(n)           │ │   │
│  │           │                └──────────────┬───────────────┘ │   │
│  └───────────┼──────────────────────────────┼──────────────────┘   │
│              │                               │                      │
│              v                               v                      │
│  ┌───────────────────────┐    ┌──────────────────────────────┐     │
│  │ data/daily-reports/   │    │ data/changelog.json          │     │
│  │   2026-04-09.json     │    │   [{date, strategy, version  │     │
│  │   2026-04-10.json     │    │     change_summary, metrics  │     │
│  │   2026-04-11.json     │    │     params, initiated_by}]   │     │
│  └───────────┬───────────┘    └──────────────────────────────┘     │
│              │                                                      │
│              v                                                      │
│  ┌───────────────────────┐                                         │
│  │  Discord Notifier     │                                         │
│  │  daily_report(report) │───> Discord channel                     │
│  └───────────────────────┘                                         │
│                                                                     │
│  ┌───────────────────────┐                                         │
│  │  CLI                  │                                         │
│  │  python -m            │                                         │
│  │    src.daily_report   │                                         │
│  │    --show / --list    │                                         │
│  └───────────────────────┘                                         │
│                                                                     │
│  - - - - - - - - - - - - Future phases - - - - - - - - - - - - -   │
│                                                                     │
│  ┌───────────────────────┐    ┌──────────────────────────────┐     │
│  │  Phase B              │    │  Phase C                     │     │
│  │  Walk-Forward         │    │  AI Diagnosis                │     │
│  │  Validator            │    │  & Auto-Tuning               │     │
│  │  (reads daily reports │    │  (reads reports + changelog  │     │
│  │   + regime data)      │    │   proposes param changes)    │     │
│  └───────────────────────┘    └──────────────────────────────┘     │
└─────────────────────────────────────────────────────────────────────┘

Reused existing modules:
  src/strategy/indicators/  ── adx(), atr(), ema(), plus_di(), minus_di()
  src/backtest/metrics.py   ── calculate_metrics()
  src/backtest/broker.py    ── Trade dataclass
  src/live/discord_notify.py ── DiscordNotifier (extended with daily_report())
```
