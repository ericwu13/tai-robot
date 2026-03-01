# tai-robot - Taiwan Futures Trading Bot

## Project Overview
Trading bot for Taiwan futures using Capital API (SKCOM) v2.13.57.
Two API approaches: DLL-based (ctypes via SKDLLPython.py) and COM-based (comtypes for KLine).

## Build & Test
```bash
pip install pyyaml keyring comtypes
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

## Conventions
- Python 3.13+ on Windows 11
- Snake_case for functions/variables, PascalCase for classes
- No pandas/numpy - indicators use pure Python
- settings.yaml is NEVER committed (contains credentials)
- SDK directory (CapitalAPI_2.13.57/) is gitignored (large binaries)
