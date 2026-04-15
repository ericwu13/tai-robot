# tai-robot - Taiwan Futures Trading Bot

## Project Overview
Trading bot for Taiwan futures using Capital API (SKCOM) v2.13.57.
Two API approaches: DLL-based (ctypes via SKDLLPython.py) and COM-based (comtypes for KLine).

## Build & Test
```bash
pip install pyyaml keyring comtypes holidays
pytest tests/ -x          # run all tests (44 tests)
python test_connection.py  # DLL-based quote connection GUI
python test_kline.py       # COM-based KLine history GUI
```

## Key Paths
- SDK wrapper: `CapitalAPI_2.13.57/CapitalAPI_2.13.57_PythonExample/SKDLLPythonTester/SKDLLPython.py`
- SDK DLLs: `...SKDLLPythonTester/libs/SKCOM.dll`
- SDK reference examples: `CapitalAPI_2.13.57/CapitalAPI_2.13.57_PythonExample/PythonExample/`
- App source: `src/` (config, gateway, market_data, strategy, execution, risk, logging_, utils)
- Tests: `tests/`
- Config: `settings.yaml` (gitignored, has credentials), `settings.example.yaml` (template)

## Architecture
- EventBus: thread-safe queue pub/sub, DLL callbacks enqueue, main thread dispatches
- ConnectionManager: Login -> Reply(0) -> Quote(1) -> ProxyOrder(4) -> LoadCommodity
- BarBuilder: ticks -> time-aligned OHLCV bars
- Strategy: AbstractStrategy plugin interface with on_bar() returning Signal
- ExecutionEngine: paper/semi_auto/full_auto modes with risk gate
- Indicators: pure functions (sma, ema, rsi, macd, bollinger_bands)

## Capital API - DLL Approach (test_connection.py)
- SK class is fully static, callbacks via OnXxx(handler) pattern
- Connection sequence: Login -> ManageServerConnection(id,0,0) -> (id,0,1) -> (id,0,4)
- 77 exported DLL functions - KLine functions are NOT exported
- authority_flag: 0=prod, 1=test+prod, 2=test only

## Capital API - COM Approach (test_kline.py)
- Required for KLine historical data (RequestKLineAMByDate, OnNotifyKLineData)
- COM registration: `regsvr32 SKCOM.dll` and `regsvr32 CTSecuritiesATL.dll` (run as admin)
- Must use `comtypes` package (not win32com - ProgIDs not registered)
- MUST use `SKCenterLib_LoginSetQuote(uid, pwd, "Y")` instead of plain Login - the "Y" flag enables quote service
- Create COM objects at MODULE level before Tkinter mainloop (not inside methods)
- Register event handlers at module level with `comtypes.client.GetEvents(obj, handler)` - do NOT specify event interface param
- Event handler methods must NOT include `this` parameter - comtypes `without_this` strips it automatically
- OnReplyMessage `[out]` param sConfirmCode: return -1 from handler (comtypes handles as return value, not pointer write)
- Connection takes ~15s: Login(~7s) -> Reply(3001) -> Ready(3003)
- KLine types: 0=minute, 4=daily, 5=weekly, 6=monthly. For N-minute bars, use type=0 with minuteNumber=N
- Trade sessions: 0=Full Session, 1=AM Session
- KLine data format: "MM/DD/YYYY HH:MM, Open, High, Low, Close, Volume"
- Data arrives synchronously within RequestKLineAMByDate call
- **Bar timestamp convention**: COM API returns intraday N-min bars labeled by their CLOSE time (e.g. an AM 60-min bar covering 12:45–13:45 arrives as `13:45`). Everywhere else in this codebase (BarBuilder/BarAggregator/`is_last_bar_of_session`/strategies) `bar.dt` is the bar OPEN time. `parse_kline_strings()` auto-detects close-time labels and shifts them to open-time so downstream code sees a single convention. Do NOT undo this normalization without rewriting every consumer.

## Conventions
- Python 3.13+ on Windows 11
- Snake_case for functions/variables, PascalCase for classes
- No pandas/numpy - indicators use pure Python
- settings.yaml is NEVER committed (contains credentials)
- SDK directory (CapitalAPI_2.13.57/) is gitignored (large binaries)
- Test count: 754 tests (as of v2.5.7)

## COM Tick History Replay (CRITICAL — issue #50)
- After `RequestTicks`, COM replays historical ticks before sending live ticks
- History ticks SHOULD come via `OnNotifyHistoryTicksLONG` (is_history=True)
- **COM sometimes sends history via `OnNotifyTicksLONG` (is_history=False)** — this mislabeling caused the strategy to run on stale data
- Defense: `_on_com_tick` compares tick datetime vs `_taipei_now()`. If tick > 120s old, treat as history regardless of is_history flag
- `suppress_strategy` must be True during ALL history replay. Do NOT trust the is_history flag alone.

## TradingGuard Fill-Pending Gate
- `BLOCK_FILL_PENDING` blocks entries AND exits while waiting for fill confirmation
- `FORCE_CLOSE` bypasses the gate (user emergency exit)
- **Deferred close (issue #50)**: when `TRADE_CLOSE` is blocked by fill_pending, the decision is stored via `guard.defer_close()`. After `_on_fill_confirmed("entry")`, it's popped and replayed automatically. Without this, rapid bar replay can permanently lose exit orders.
- Never rebind `self._trading_guard` — `_fill_poller` holds a reference to it (issue #43)

## Real Entry Price Tracking (issue #45)
- `SimulatedBroker.real_entry_price`: set by `_on_fill_confirmed("entry")` from OpenInterest avg_cost
- `try_set_real_entry_price()`: guarded write with 4 checks (price>0, position>0, not already set, entry_bar_index matches). Prevents late callbacks from planting stale values on the next trade.
- `effective_entry_price()`: returns real if confirmed, else simulated. Strategies use this for slippage-aware stops.
- Belt-and-braces: `on_bar_close` resets `real_entry_price=0` on every new entry

## BarAggregator 1-min Pass-Through (issue #44)
- For `target_interval == 60`, `on_bar()` returns the bar immediately (no accumulation delay)
- Without this, 1-min strategies had a permanent 1-bar lag (chart and strategy both 1 bar behind)
- H1/H4 aggregation is unchanged (accumulate until boundary cross)

## CSV Reload for 1-min Strategies (issue #47)
- `reload_1m_bars()` merges CSV bars into `_aggregated_bars` for `target_interval == 60`
- `feed_warmup_bars()` seeds `_seen_1m_dts` for 1-min strategies to prevent duplicates
- COM warmup API does NOT return bars for the currently in-progress trading session
- Without the merge, the live chart had a ~10-hour gap between warmup end and first live bar

## AI Code Generation
- Indicators return named tuples: `BollingerResult(upper, middle, lower)`, `MacdResult(macd_line, signal_line, histogram)`, `StochasticResult(k, d)` — both `bb.middle` and `upper, mid, lower = bb` work
- `broker.queue_exit()` auto-rounds float limit/stop prices to int (TAIFEX prices are integers)
- `extract_python_code()` strips trailing markdown that leaks into code fences
- Trade.entry_dt and exit_dt are STRINGS ("YYYY-MM-DD HH:MM"), not datetime objects
- Code gen prompt uses task+constraint framing, NOT expertise persona (research: personas hurt code accuracy)
- AI Review prompt limits to 1 change per iteration, requires identifying what works first, warns about overfitting
- Python 3.13: exception variables (`e` in `except ... as e:`) are deleted after the block — use `lambda err=e:` not `lambda: ...e...`
