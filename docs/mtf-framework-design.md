# Multi-Timeframe (MTF) Framework Design

**Version**: 2.7.0  
**Status**: Design Review  
**Author**: Claude (code gen), reviewed by human  

---

## Table of Contents

1. [Goals & Non-Goals](#1-goals--non-goals)
2. [Backtest-First Principle](#2-backtest-first-principle)
3. [No-Lookahead Guarantee](#3-no-lookahead-guarantee)
4. [API Specification](#4-api-specification)
5. [Data Flow](#5-data-flow)
6. [Session Alignment](#6-session-alignment)
7. [Edge Cases](#7-edge-cases)
8. [Backwards Compatibility](#8-backwards-compatibility)
9. [AI Code Gen Integration](#9-ai-code-gen-integration)
10. [Migration Path](#10-migration-path)
11. [Scalability Demonstration](#11-scalability-demonstration)
12. [Performance Considerations](#12-performance-considerations)

---

## 1. Goals & Non-Goals

### Goals

- **First-class MTF support**: strategies can declare higher-timeframe (HTF) data
  subscriptions and query completed HTF bars from within `on_bar()`.
- **Backtest-first**: MTF must work fully in backtest mode. Backtest is the primary
  validation path — users backtest an MTF strategy on historical data and get
  results identical to what would happen in live.
- **Backtest/live parity**: the same strategy code running on the same bars (backtest
  replay vs live stream) must produce **identical** trade decisions. Any difference
  is a bug.
- **No lookahead bias**: at primary-bar time T, HTF data only contains bars whose
  close time is <= T. This prevents the "repainting" problem from TradingView/Pine.
- **Zero overhead for single-TF strategies**: existing strategies that don't declare
  `htf_intervals` pay no performance or complexity cost.
- **Backwards compatible**: all existing strategies pass their tests unchanged, with
  no code modifications.

### Non-Goals

- **Cross-symbol MTF**: we do NOT support querying a different symbol's bars. MTF
  is same-symbol, multiple intervals only.
- **Lower-timeframe subscriptions**: strategies cannot subscribe to intervals
  *shorter* than their primary. Primary is the fastest clock.
- **Tick-level HTF**: HTF data is bar-based only. No tick access at HTF.
- **Arbitrary resample ratios**: HTF intervals must be exact multiples of the
  primary interval (e.g., primary=30m, HTF=60m is OK; primary=30m, HTF=45m is not).
- **Dynamic interval changes**: `htf_intervals` is declared at class definition
  time, not changed at runtime.

---

## 2. Backtest-First Principle

**This is the most important design requirement.** A framework that works in live but
not backtest (or vice versa) is useless.

### Backtest as primary validation

1. Users MUST be able to backtest an MTF strategy on historical data before
   deploying it live.
2. The backtest engine aggregates HTF bars on-the-fly from the primary bar stream.
   No separate HTF data files are required.
3. If historical data is only available as 1-min bars, the engine aggregates to both
   the primary interval and all HTF intervals on the fly.

### Backtest/live parity guarantee

The same strategy code running on the same bar sequence must produce **bit-identical**
decisions in both modes. This is enforced by:

1. **Shared code path**: both `BacktestEngine` and `LiveRunner` use the same
   `BarAggregator` class for HTF aggregation.
2. **Same DataStore API**: strategies call the same `htf_closes(interval, n)` etc.
   methods in both modes.
3. **Same completion rule**: HTF bars are only visible after their boundary is
   crossed — identical logic in both paths.
4. **Parity test**: an automated test feeds the same bar sequence through both
   backtest and a simulated live replay, asserting the decision sequences are
   bit-identical.

### Backtest data requirements

- **Minimum**: primary-interval bars (e.g., 30-min bars for a 30m primary strategy).
  The engine aggregates HTF (e.g., 60m) from these.
- **Alternative**: 1-min bars. The engine aggregates to both primary and HTF on the
  fly. This matches the live path exactly (live always receives 1-min bars).
- **Format**: `list[Bar]` — same as today. No new data format.

---

## 3. No-Lookahead Guarantee

### The repainting problem

In TradingView, `security(syminfo.tickerid, "60", close)` evaluated on a 15-min
chart "repaints" — it shows the final 60-min close value on ALL four 15-min bars
within that hour, even while the hour is still in progress. This causes backtests
to look unrealistically good because the strategy "sees" future price action.

### Our guarantee

**At primary bar time T, the strategy can ONLY see HTF bars whose close time <= T.**

This means:
- An HTF bar covering 09:00-10:00 is NOT visible at 09:15, 09:30, or 09:45.
- It becomes visible at 10:00 (when the first primary bar of the NEXT HTF period
  triggers the HTF bar completion).
- The strategy at 09:15 sees the PREVIOUS completed HTF bar (08:00-09:00).

### Implementation mechanism

1. HTF `BarAggregator` accumulates primary bars silently.
2. When a primary bar crosses an HTF boundary, the previous HTF bar is finalized
   and added to the HTF `DataStore`.
3. The strategy's `on_bar()` runs AFTER HTF DataStore update — so it sees the
   newly completed HTF bar (which covers data up to the previous HTF period).
4. The in-progress HTF bar is NEVER exposed to strategy code.

### Proof by construction

```
Primary: 30-min bars
HTF: 60-min bars

Bar sequence:
  09:00 (primary) → HTF agg starts [09:00-09:30)
  09:30 (primary) → HTF agg updates [09:00-10:00), boundary NOT crossed
  10:00 (primary) → HTF boundary crossed!
    1. HTF bar [09:00-10:00) finalized, added to HTF DataStore
    2. strategy.on_bar(bar=10:00) runs
    3. strategy calls htf_closes(3600, 1) → gets [09:00-10:00) bar
    4. The 10:00-11:00 HTF bar is accumulating but NOT in DataStore
```

At step 3, the strategy sees the 09:00-10:00 bar which is fully complete (its close
is the 09:30 bar's close, i.e., data from T-30min). No future data leaks.

---

## 4. API Specification

### 4.1 Strategy declaration

```python
class MyMTFStrategy(BacktestStrategy):
    kline_type = 0
    kline_minute = 30       # primary: 30-min bars

    # NEW: declare HTF subscriptions as list of seconds
    htf_intervals: list[int] = [3600]  # subscribe to 60-min bars

    def required_bars(self) -> int:
        return 20  # primary bars needed

    def htf_required_bars(self) -> dict[int, int]:
        """Optional: minimum completed HTF bars before strategy runs.
        Default: {interval: 1} for each declared interval.
        """
        return {3600: 20}  # need 20 completed 60-min bars

    def on_bar(self, bar: Bar, data_store: DataStore, broker: BrokerContext) -> None:
        # Primary data (unchanged API)
        closes = data_store.get_closes(20)

        # HTF data (NEW API — only completed bars)
        htf_closes = data_store.htf_closes(3600, 20)
        htf_highs  = data_store.htf_highs(3600, 20)
        htf_lows   = data_store.htf_lows(3600, 20)
        htf_opens  = data_store.htf_opens(3600, 20)
        htf_bars   = data_store.htf_bars(3600, 20)
```

### 4.2 `htf_intervals` class attribute

| Property | Value |
|----------|-------|
| Type | `list[int]` (seconds) |
| Default | `[]` (empty — single-TF, backwards compatible) |
| Validation | Each interval must be > primary interval |
| Validation | Each interval must be an exact multiple of primary interval |
| Examples | `[3600]` for 60m, `[3600, 14400]` for 60m+4H |

### 4.3 `htf_required_bars()` method

| Property | Value |
|----------|-------|
| Return type | `dict[int, int]` mapping interval (seconds) → minimum bar count |
| Default implementation | `{iv: 1 for iv in self.htf_intervals}` |
| Purpose | Strategy doesn't receive `on_bar()` until ALL HTF stores have enough bars |
| Override | Optional — strategies that need N periods of HTF history override this |

### 4.4 DataStore HTF methods (new)

All new methods are on the existing `DataStore` class. For single-TF strategies,
the HTF stores dict is empty and these methods are never called.

```python
class DataStore:
    # Existing methods — unchanged
    def get_bars(self, n=None) -> list[Bar]: ...
    def get_closes(self, n=None) -> list[int]: ...
    def get_highs(self, n=None) -> list[int]: ...
    def get_lows(self, n=None) -> list[int]: ...

    # NEW: HTF accessors
    def htf_bars(self, interval: int, n: int | None = None) -> list[Bar]: ...
    def htf_closes(self, interval: int, n: int | None = None) -> list[int]: ...
    def htf_highs(self, interval: int, n: int | None = None) -> list[int]: ...
    def htf_lows(self, interval: int, n: int | None = None) -> list[int]: ...
    def htf_opens(self, interval: int, n: int | None = None) -> list[int]: ...

    # NEW: internal — used by engine, not strategy
    def _register_htf(self, interval: int, max_bars: int = 5000) -> None: ...
    def _add_htf_bar(self, interval: int, bar: Bar) -> None: ...
    def _htf_len(self, interval: int) -> int: ...
```

**Design choice: extend DataStore, not create MultiDataStore.**

Rationale: the `on_bar()` signature already passes `data_store: DataStore`. By
adding HTF methods to the existing class, we avoid changing ANY method signatures.
Strategies that don't use HTF simply never call the `htf_*` methods. The HTF data
is stored internally as `dict[int, deque[Bar]]`.

### 4.5 `on_bar()` signature — UNCHANGED

```python
def on_bar(self, bar: Bar, data_store: DataStore, broker: BrokerContext) -> None:
```

We do NOT add an HTF parameter. Rationale:
- Changing the signature would break all existing strategies.
- The DataStore already provides all HTF data via `htf_*` methods.
- "Pull" model (strategy queries what it needs) is simpler than "push" model
  (engine decides what to pass).

### 4.6 Warmup behavior

The engine does NOT call `strategy.on_bar()` until ALL of these are satisfied:
1. `len(data_store) >= strategy.required_bars()` (existing check)
2. For each `(interval, min_bars)` in `strategy.htf_required_bars()`:
   `data_store._htf_len(interval) >= min_bars`

This ensures the strategy never runs on insufficient HTF history. The strategy
does not need to guard against empty HTF data — the engine guarantees it.

---

## 5. Data Flow

### 5.1 Backtest data flow

```
Input: list[Bar] at primary interval (e.g., 30-min bars)
       OR list[Bar] at 1-min interval (engine aggregates primary on-the-fly)

BacktestEngine.run(bars):
  For each primary bar:
    1. data_store.add_bar(bar)                    # primary store
    2. For each HTF interval:
       completed = htf_aggregators[interval].on_bar(bar)
       if completed:
         data_store._add_htf_bar(interval, completed)  # HTF store
    3. broker.on_bar_open(...)
    4. broker.check_exits(...)
    5. if warmup_satisfied:
         strategy.on_bar(bar, data_store, ctx)
    6. broker.on_bar_close(...)
```

**Key**: step 2 runs BEFORE step 5. The HTF DataStore is updated before the
strategy sees any data. But only COMPLETED HTF bars are added — partial bars
stay inside the aggregator.

### 5.2 Backtest with 1-min source data

When the source data is 1-min bars but the strategy's primary is 30-min:

```
BacktestEngine.run(bars_1m):
  primary_agg = BarAggregator(symbol, primary_interval=1800)  # 30m
  htf_aggs = {3600: BarAggregator(symbol, 3600)}              # 60m

  For each 1-min bar:
    primary_bar = primary_agg.on_bar(bar_1m)
    if primary_bar is None:
      continue  # no completed primary bar yet

    # A primary bar completed — now process it
    data_store.add_bar(primary_bar)
    for interval, agg in htf_aggs.items():
      completed = agg.on_bar(primary_bar)      # feed PRIMARY bars to HTF
      if completed:
        data_store._add_htf_bar(interval, completed)

    # ... broker + strategy as before
```

**Note**: HTF aggregators receive PRIMARY bars, not 1-min bars. This ensures
HTF boundaries align with primary bar boundaries. If we fed 1-min bars to HTF
aggregators, a 60-min bar would complete mid-primary-bar (e.g., at 09:59:00
instead of 10:00:00), violating the no-lookahead rule because the 30-min bar
at 09:30 hasn't completed yet.

### 5.3 Live data flow

```
LiveRunner:
  primary_agg = BarAggregator(symbol, primary_interval)
  htf_aggs = {iv: BarAggregator(symbol, iv) for iv in strategy.htf_intervals}

  On each 1-min bar from COM tick stream:
    primary_bar = primary_agg.on_bar(bar_1m)
    if primary_bar:
      data_store.add_bar(primary_bar)
      for interval, agg in htf_aggs.items():
        completed = agg.on_bar(primary_bar)    # feed PRIMARY bars
        if completed:
          data_store._add_htf_bar(interval, completed)
      # ... broker + strategy
```

**Identical to backtest**: both paths use `BarAggregator`, both feed primary bars
to HTF aggregators, both check warmup before calling `on_bar()`. This is how we
guarantee parity.

### 5.4 Warmup in live mode

Live warmup already fetches KLine data at the strategy's native interval. For MTF:

1. Fetch warmup at primary interval (existing behavior).
2. For each HTF interval, aggregate the warmup bars to build initial HTF history.
3. Both primary and HTF DataStores are populated before `state = RUNNING`.

No separate HTF data fetch is needed — the primary warmup bars contain all the
information needed to build HTF bars.

---

## 6. Session Alignment

### TAIFEX sessions

- **AM (day)**: 08:45 - 13:45 TWT (5 hours)
- **Night**: 15:00 - 05:00+1 TWT (14 hours)
- **Gap**: 13:45 - 15:00 (1h15m, no trading)

### HTF bar behavior at session boundaries

**Decision: HTF bars do NOT span the session gap.**

A 4H bar that would span 13:45 (day close) is truncated. The night session starts
a fresh 4H bar at 15:00.

**Rationale**: the gap has no price data. A "continuous" 4H bar covering 12:45-16:45
would have a 1h15m hole with no ticks, making OHLCV misleading. This matches how
the existing `session_align()` works — it uses session start as epoch, so AM and
Night bars are independently aligned.

**Example: 4H bars on a trading day**

| Session | Bar | Open time | Close time | Duration |
|---------|-----|-----------|------------|----------|
| AM | 1 | 08:45 | 12:45 | 4h |
| AM | 2 | 12:45 | 13:45 | 1h (truncated at session end) |
| Night | 1 | 15:00 | 19:00 | 4h |
| Night | 2 | 19:00 | 23:00 | 4h |
| Night | 3 | 23:00 | 03:00 | 4h (crosses midnight, same session) |
| Night | 4 | 03:00 | 05:00 | 2h (truncated at session end) |

This is the existing behavior of `BarAggregator` + `session_align()` and does not
change with MTF.

### HTF bar completion at session end

When the last primary bar of a session fires, the HTF aggregator may have a partial
bar. This partial bar is NOT flushed to the DataStore because:
1. The session gap doesn't carry trading information.
2. The next session will start fresh aggregation.
3. Flushing a 1-hour "4H bar" would distort indicator calculations.

The partial bar is implicitly abandoned when the next session's first primary bar
starts a new HTF aggregation period.

**Exception**: if a strategy needs end-of-day processing, it can use
`is_last_bar_of_session()` to detect the boundary.

---

## 7. Edge Cases

### 7.1 Warmup

**Problem**: if HTF requires 20 bars of 60-min history, and primary is 30-min, we
need at least 40 primary bars just for HTF warmup (plus the strategy's own
`required_bars` for primary warmup).

**Solution**: `htf_required_bars()` lets the strategy declare exact HTF needs. The
engine holds back `on_bar()` until all requirements are met. The total warmup is
the maximum of:
- `required_bars()` primary bars
- Enough primary bars to produce `htf_required_bars()[interval]` completed HTF bars
  for each interval

In backtest, this just means the first N bars are "wasted" on warmup (same as
single-TF strategies). No special handling needed.

### 7.2 First primary bar of a new HTF period

When a primary bar at T crosses an HTF boundary, the engine:
1. Finalizes the previous HTF bar (close time = T)
2. Adds it to HTF DataStore
3. Starts a new HTF accumulation period
4. Runs `strategy.on_bar(bar_at_T, data_store, ctx)`

At step 4, `data_store.htf_closes(3600, 1)` returns the just-completed HTF bar.
This is correct — the bar closed at T, and the strategy is processing the bar at T.

### 7.3 HTF interval not an exact multiple of primary

**Rejected at strategy load time.** If `htf_intervals` contains a value that isn't
an exact multiple of the primary interval, the engine raises `ValueError` before
any bars are processed:

```python
for iv in strategy.htf_intervals:
    if iv % primary_interval != 0:
        raise ValueError(
            f"HTF interval {iv}s must be exact multiple of "
            f"primary interval {primary_interval}s"
        )
```

### 7.4 Clock sync in live mode

Live mode receives 1-min bars from COM API. The `BarAggregator` aligns bars using
`session_align()` based on bar datetime, not wall clock. No clock sync issue arises
because both primary and HTF aggregators use the same bar datetime.

### 7.5 Multiple HTF intervals

A strategy can subscribe to multiple HTF intervals (e.g., 60m and 4H). Each gets
its own aggregator and DataStore deque. They are independent — a 60m bar completing
does not affect the 4H aggregator.

### 7.6 Session restore (live)

On session restore, `reload_1m_bars()` replays saved 1-min bars. For MTF, this
replay also feeds the HTF aggregators, rebuilding HTF DataStore state. The
`_seen_1m_dts` dedup mechanism prevents double-counting bars that were in the
warmup AND in the CSV logs.

---

## 8. Backwards Compatibility

### Contract

1. **All existing strategies work unchanged.** The default `htf_intervals = []`
   means no HTF processing, no warmup changes, no performance overhead.

2. **`on_bar()` signature unchanged.** `(bar, data_store, broker)` — no new params.

3. **`DataStore` API unchanged.** `get_bars()`, `get_closes()`, `get_highs()`,
   `get_lows()`, `__len__()` all work exactly as before.

4. **`BacktestEngine` API unchanged.** `run(bars)` still accepts `list[Bar]` and
   returns `BacktestResult`.

5. **`LiveRunner` external API unchanged.** `feed_warmup_bars()`, `feed_1m_bars()`,
   `get_bars_at_interval()` all work as before.

### What changes

| Component | Change | Backwards compatible? |
|-----------|--------|----------------------|
| `DataStore` | Add `htf_*` methods + `_register_htf`, `_add_htf_bar` | Yes — new methods only |
| `BacktestStrategy` | Add `htf_intervals = []` class attr | Yes — default empty |
| `BacktestStrategy` | Add `htf_required_bars()` with default impl | Yes — default returns {} |
| `BacktestEngine.run()` | Create HTF aggregators if `htf_intervals` non-empty | Yes — no-op if empty |
| `LiveRunner` | Create HTF aggregators if `htf_intervals` non-empty | Yes — no-op if empty |

### Verification

All existing tests (768+) must pass without modification. A new test explicitly
creates each of the 8 existing strategies and verifies they work with the
MTF-enhanced engine.

---

## 9. AI Code Gen Integration

### Prompt changes

Add to `STRATEGY_CODE_CONTEXT` (`src/ai/prompts.py`):

```
## Multi-Timeframe (MTF) Support

To use higher-timeframe data in your strategy:

1. Declare `htf_intervals` as a class attribute (list of seconds):
   htf_intervals = [3600]  # subscribe to 60-min bars

2. Optionally declare minimum HTF bars needed:
   def htf_required_bars(self) -> dict[int, int]:
       return {3600: 20}  # need 20 completed 60-min bars

3. Access HTF data in on_bar() via data_store:
   htf_closes = data_store.htf_closes(3600, 20)   # last 20 completed 60-min closes
   htf_highs  = data_store.htf_highs(3600, 20)    # last 20 completed 60-min highs
   htf_lows   = data_store.htf_lows(3600, 20)     # last 20 completed 60-min lows
   htf_opens  = data_store.htf_opens(3600, 20)    # last 20 completed 60-min opens
   htf_bars   = data_store.htf_bars(3600, 20)     # last 20 completed 60-min Bar objects

CRITICAL: HTF data only contains COMPLETED bars. If primary is 30-min and HTF
is 60-min, at 09:30 (mid-hour) you see the 08:00-09:00 bar, NOT the in-progress
09:00-10:00 bar. This prevents lookahead bias.

Rules:
- htf_intervals values must be LARGER than primary interval (kline_minute * 60)
- htf_intervals values must be exact multiples of primary interval
- Primary data uses existing API: data_store.get_closes(n), get_highs(n), etc.
- HTF data uses new API: data_store.htf_closes(interval, n), etc.
- The strategy won't run until BOTH primary and HTF warmup are satisfied
```

### Sandbox allowlist

No changes needed to `AVAILABLE_INDICATORS` — MTF doesn't add new indicators.
The `htf_*` methods are on DataStore, which is already in the sandbox namespace.

No changes needed to `ALLOWED_IMPORTS` — strategies import from the same modules.

### Example for AI reference

Include in the prompt a complete MTF strategy example (the MACD+BB strategy from
Step 3) so the AI can reference a working pattern.

---

## 10. Migration Path

### Upgrading an existing single-TF strategy to MTF

**Step 1**: Add `htf_intervals` class attribute.

```python
class MyStrategy(BacktestStrategy):
    kline_type = 0
    kline_minute = 30
    htf_intervals = [3600]  # NEW: 60-min HTF
```

**Step 2**: Optionally override `htf_required_bars()`.

```python
    def htf_required_bars(self) -> dict[int, int]:
        return {3600: 20}  # need 20 HTF bars for BB(20)
```

**Step 3**: Use HTF data in `on_bar()`.

```python
    def on_bar(self, bar, data_store, broker):
        # Existing primary logic
        closes = data_store.get_closes(20)
        bb = bollinger_bands(closes, 20)

        # NEW: HTF regime filter
        htf_closes = data_store.htf_closes(3600, 20)
        htf_bb = bollinger_bands(htf_closes, 20)
        if htf_bb and bar.close > htf_bb.middle:
            # Only trade when price is above HTF BB midline
            ...
```

**That's it.** No other changes needed. The engine handles aggregation,
warmup, and DataStore management automatically.

### No-migration path

Strategies that don't need MTF change nothing. The default `htf_intervals = []`
means zero MTF overhead.

---

## 11. Scalability Demonstration

The MACD+BB example is the minimum proof. The framework must generalize to ANY
MTF strategy without framework changes. Here we prove it handles every realistic
pattern.

### 11.1 Triple-timeframe: 5m primary + 15m MACD + 60m BB

```python
class TripleTfStrategy(BacktestStrategy):
    kline_type = 0
    kline_minute = 5                          # 5-min primary
    htf_intervals = [900, 3600]               # 15m + 60m

    def htf_required_bars(self) -> dict[int, int]:
        return {900: 26 + 9, 3600: 20}       # MACD(26,9) on 15m, BB(20) on 60m

    def on_bar(self, bar, data_store, broker):
        macd_r = macd(data_store.htf_closes(900, 35))       # 15m MACD
        bb = bollinger_bands(data_store.htf_closes(3600, 20))  # 60m BB
        closes = data_store.get_closes(14)                   # 5m ATR
        # ... combine signals
```

Adding a new HTF interval (e.g., 4H): one line change: `htf_intervals = [900, 3600, 14400]`.

### 11.2 Wide-ratio: 15m primary + 4H trend filter (16:1)

```python
class WideRatioStrategy(BacktestStrategy):
    kline_type = 0
    kline_minute = 15                         # 15-min primary
    htf_intervals = [14400]                   # 4H (16:1 ratio)

    def htf_required_bars(self) -> dict[int, int]:
        return {14400: 14}                    # ADX(14) on 4H

    def on_bar(self, bar, data_store, broker):
        htf_highs = data_store.htf_highs(14400, 15)
        htf_lows  = data_store.htf_lows(14400, 15)
        htf_cls   = data_store.htf_closes(14400, 15)
        trend = adx(htf_highs, htf_lows, htf_cls, 14)       # 4H ADX
        # ... trade on 15m when 4H trend > 25
```

16:1 ratio works because 14400 % 900 == 0. No framework changes.

### 11.3 Tight intraday: 1m primary + 5m + 15m momentum layers

```python
class TightIntradayStrategy(BacktestStrategy):
    kline_type = 0
    kline_minute = 1                          # 1-min primary
    htf_intervals = [300, 900]                # 5m + 15m

    def htf_required_bars(self) -> dict[int, int]:
        return {300: 14, 900: 20}

    def on_bar(self, bar, data_store, broker):
        rsi_5m = rsi(data_store.htf_closes(300, 15), 14)    # 5m RSI
        bb_15m = bollinger_bands(data_store.htf_closes(900, 20))  # 15m BB
        ema_1m = ema(data_store.get_closes(20), 20)          # 1m EMA
        # ... layer signals
```

### 11.4 Non-integer ratio edge: 30m primary + 4H (8:1) on TAIFEX

14400 % 1800 == 0 (exact multiple). Session alignment handles TAIFEX sessions
correctly — the 4H bar at 12:45-13:45 is shorter due to AM close, same as for
single-TF 4H strategies. No special framework handling.

### 11.5 Single-HTF, no primary-side indicators

```python
class PureHtfFilterStrategy(BacktestStrategy):
    kline_type = 0
    kline_minute = 1                          # 1-min bars
    htf_intervals = [3600]                    # 1H ADX only

    def required_bars(self) -> int:
        return 1                              # no primary indicators

    def htf_required_bars(self) -> dict[int, int]:
        return {3600: 15}

    def on_bar(self, bar, data_store, broker):
        htf_h = data_store.htf_highs(3600, 15)
        htf_l = data_store.htf_lows(3600, 15)
        htf_c = data_store.htf_closes(3600, 15)
        adx_val = adx(htf_h, htf_l, htf_c, 14)
        if adx_val and adx_val > 25 and broker.position_size == 0:
            broker.entry("trend_entry", OrderSide.LONG)
```

### 11.6 Many HTFs: 15m primary + 30m + 60m + 4H

```python
class FourLayerStrategy(BacktestStrategy):
    kline_type = 0
    kline_minute = 15
    htf_intervals = [1800, 3600, 14400]       # 30m + 60m + 4H

    def htf_required_bars(self) -> dict[int, int]:
        return {1800: 14, 3600: 20, 14400: 14}

    def on_bar(self, bar, data_store, broker):
        rsi_30m = rsi(data_store.htf_closes(1800, 15))
        bb_60m = bollinger_bands(data_store.htf_closes(3600, 20))
        adx_4h = adx(
            data_store.htf_highs(14400, 15),
            data_store.htf_lows(14400, 15),
            data_store.htf_closes(14400, 15), 14)
        # ... combine all layers
```

### Scalability checklist

| Test | Pass? | Evidence |
|------|-------|----------|
| Adding new HTF = one line | Yes | `htf_intervals.append(new_interval)` |
| New indicator at HTF = one call | Yes | `indicator_fn(data_store.htf_closes(iv, n))` |
| New interval (2m, 20m) = no framework change | Yes | `BarAggregator` accepts any int seconds; `session_align` handles any interval |
| Homogeneous API across intervals | Yes | `htf_closes`, `htf_highs`, `htf_lows`, `htf_opens`, `htf_bars` — all same signature `(interval, n)` |
| 1:1 through 16:1+ ratios | Yes | Any ratio works if interval % primary == 0 |
| Triple/quad timeframes | Yes | `htf_intervals` is a list, no limit on length |
| Primary-only indicators + HTF filter | Yes | Primary API unchanged, HTF additive |

---

## 12. Performance Considerations

### Single-TF strategies (htf_intervals = [])

**Zero overhead.** The engine checks `if strategy.htf_intervals:` and skips all
HTF logic. No aggregators created, no extra DataStore operations, no warmup
changes.

### MTF strategies

| Operation | Cost | Notes |
|-----------|------|-------|
| HTF aggregation per primary bar | O(K) where K = len(htf_intervals) | One `BarAggregator.on_bar()` call per HTF interval |
| HTF DataStore update | O(1) amortized | Only when HTF bar completes |
| `htf_closes(interval, n)` | O(n) | Same as `get_closes(n)` — list slice |
| Memory per HTF interval | ~5000 * sizeof(Bar) | Same ring buffer as primary |
| Warmup check | O(K) per bar | Check each HTF store length |

For typical usage (1-2 HTF intervals), overhead is negligible. The bottleneck
in backtests is indicator computation, not bar routing.

### Memory

Each HTF interval adds one `deque[Bar]` of up to 5000 bars. A Bar is ~80 bytes
(8 fields), so each HTF deque uses ~400KB. For 3 HTF intervals: ~1.2MB additional.
Negligible.

---

## Appendix A: Complete Example — MACD+BB MTF Strategy

```python
"""MTF MACD+BB: 30-min MACD entries filtered by 60-min Bollinger Bands.

Primary: 30-min bars (trade signals from MACD crossover)
HTF: 60-min bars (regime filter from Bollinger Bands position)

Entry: MACD bullish crossover on 30m, AND price above 60m BB midline
Exit: fixed TP/SL based on ATR
"""

from src.backtest.strategy import BacktestStrategy
from src.backtest.broker import BrokerContext, OrderSide
from src.market_data.models import Bar
from src.market_data.data_store import DataStore
from src.strategy.indicators import macd, bollinger_bands, atr


class MtfMacdBbStrategy(BacktestStrategy):
    kline_type = 0
    kline_minute = 30
    htf_intervals = [3600]  # 60-min Bollinger Bands

    def __init__(self, **kwargs):
        self.macd_fast = kwargs.get("macd_fast", 12)
        self.macd_slow = kwargs.get("macd_slow", 26)
        self.macd_signal = kwargs.get("macd_signal", 9)
        self.bb_period = kwargs.get("bb_period", 20)
        self.bb_std = kwargs.get("bb_std", 2.0)
        self.atr_period = kwargs.get("atr_period", 14)
        self.sl_mult = kwargs.get("sl_mult", 1.5)
        self.tp_mult = kwargs.get("tp_mult", 2.0)
        self._prev_hist = None

    def required_bars(self) -> int:
        return self.macd_slow + self.macd_signal

    def htf_required_bars(self) -> dict[int, int]:
        return {3600: self.bb_period}

    def on_bar(self, bar: Bar, data_store: DataStore, broker: BrokerContext) -> None:
        # --- Primary (30m): MACD ---
        closes = data_store.get_closes(self.macd_slow + self.macd_signal)
        macd_result = macd(closes, self.macd_fast, self.macd_slow, self.macd_signal)
        if macd_result is None:
            return

        # --- HTF (60m): Bollinger Bands ---
        htf_closes = data_store.htf_closes(3600, self.bb_period)
        bb = bollinger_bands(htf_closes, self.bb_period, self.bb_std)
        if bb is None:
            return

        # --- ATR for stop/target sizing ---
        highs = data_store.get_highs(self.atr_period + 1)
        lows = data_store.get_lows(self.atr_period + 1)
        p_closes = data_store.get_closes(self.atr_period + 1)
        atr_val = atr(highs, lows, p_closes, self.atr_period)
        if atr_val is None:
            return

        hist = macd_result.histogram

        # --- Entry logic ---
        if broker.position_size == 0 and self._prev_hist is not None:
            bullish_cross = self._prev_hist < 0 and hist >= 0
            above_midline = bar.close > bb.middle

            if bullish_cross and above_midline:
                sl = int(atr_val * self.sl_mult)
                tp = int(atr_val * self.tp_mult)
                broker.entry("macd_long", OrderSide.LONG)
                broker.exit("macd_exit", "macd_long",
                           limit=bar.close + tp,
                           stop=bar.close - sl)

        self._prev_hist = hist
```

---

## Appendix B: File Changes Summary

| File | Change type | Description |
|------|-------------|-------------|
| `src/market_data/data_store.py` | Modify | Add `htf_*` methods, `_htf_stores` dict |
| `src/backtest/strategy.py` | Modify | Add `htf_intervals`, `htf_required_bars()` defaults |
| `src/backtest/engine.py` | Modify | Create HTF aggregators, feed HTF bars, check HTF warmup |
| `src/live/live_runner.py` | Modify | Create HTF aggregators, feed HTF bars in live loop |
| `src/ai/prompts.py` | Modify | Add MTF API docs to `_CODE_CONTEXT_BODY` |
| `src/strategy/examples/mtf_macd_bb.py` | New | Example MTF strategy |
| `tests/test_mtf_framework.py` | New | Unit + integration + lookahead + parity tests |
| `docs/mtf-framework-design.md` | New | This document |
