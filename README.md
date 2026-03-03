# tai-robot - Taiwan Futures Trading Bot

**Version: 1.0.0**

Trading bot for Taiwan futures (TAIEX Futures) using Capital API (SKCOM) v2.13.57.

Features:
- AI Strategy Workbench — chat with Claude/Gemini to design, backtest, and export strategies
- Backtesting engine with candlestick chart visualization (Bollinger Bands, trade markers)
- Live bot deployment with tick-based bar building
- Multiple data sources: Capital API (COM) and TradingView
- Multi-symbol support: TX00 (大台指), MTX00 (小台指)
- Pine Script export with 繁體中文 comments

## Prerequisites

- Windows 11 (x64)
- Python 3.13+
- Capital API SDK v2.13.57 (for development builds)
- Capital API certificate installed (per-user, from your broker)

## Development Setup

```bash
# Install dependencies
pip install pyyaml keyring comtypes httpx lightweight-charts pandas pyinstaller

# Optional: TradingView data feed
pip install tvDatafeed

# Copy and configure settings
cp settings.example.yaml settings.yaml
# Edit settings.yaml with your credentials

# Run tests
pytest tests/ -x

# Run the application
python run_backtest.py
```

### COM Registration (one-time, requires admin)

The Capital API uses COM objects. Register the DLLs once:

```cmd
cd CapitalAPI_2.13.57\CapitalAPI_2.13.57_PythonExample\SKDLLPythonTester\libs
regsvr32 SKCOM.dll
regsvr32 CTSecuritiesATL.dll
```

## Building the EXE

The app can be packaged as a standalone Windows EXE using PyInstaller (--onedir mode). All SDK DLLs are bundled — users do not need to install the Capital API SDK.

### Build Steps

```bash
# From the project root
python -m PyInstaller tai_backtest.spec --noconfirm
```

Output: `dist/tai_backtest/tai_backtest.exe`

### Build Files

| File | Purpose |
|------|---------|
| `tai_backtest.spec` | PyInstaller build specification (--onedir mode) |
| `runtime_hook_comtypes.py` | Runtime hook for writable comtypes.gen cache |

### What Gets Bundled

- SDK DLLs (8 files from `CapitalAPI_2.13.57/.../libs/`) into `_internal/libs/`
- `lightweight_charts/js/` (WebGL chart assets)
- `data/`, `src/`, `strategies/` directories
- `settings.example.yaml` as config template

## Deploying the EXE

### First-time Setup (per machine)

1. Copy the entire `dist/tai_backtest/` folder to the target machine
2. Place your `settings.yaml` next to `tai_backtest.exe` (not inside `_internal/`)
3. **Run `tai_backtest.exe` as Administrator once** (right-click > Run as administrator)
   - This auto-registers the COM DLLs via `regsvr32`
   - Subsequent runs do not need admin
4. Ensure the Capital API certificate is installed (from your broker's website)

### Directory Structure After Deployment

```
tai_backtest/
  tai_backtest.exe          # Main executable
  settings.yaml             # Your credentials (create from template)
  _internal/                # Bundled Python + dependencies (do not modify)
    libs/                   # Capital API SDK DLLs
    lightweight_charts/js/  # Chart assets
    ...
  _comtypes_cache/          # Auto-created on first run
  CapitalLog_Backtest/      # API logs (auto-created on login)
```

## Project Structure

```
src/
  ai/              # AI chat client, code sandbox, Pine exporter
  backtest/        # Backtest engine, broker, chart, metrics
  live/            # Live runner, bar aggregator, CSV logger
  market_data/     # Bar builder, data store, models
  strategy/        # Abstract strategy, indicators, example strategies
  config/          # Settings loader
  execution/       # Order execution engine
  risk/            # Risk management
  utils/           # Time utilities
tests/             # 196 tests
run_backtest.py    # Main GUI application
```

## Technical Notes

### PyInstaller COM Fix

PyInstaller's bootloader calls `SetDllDirectoryW(_MEIPASS)`, which overrides the Windows DLL search order. COM's `CoCreateInstance` uses `LoadLibrary` (not `LoadLibraryEx`), so it respects `SetDllDirectoryW` but ignores `os.add_dll_directory()`. The fix uses a three-phase approach:

1. `SetDllDirectoryW(libs_path)` — so COM can find SKCOM.dll's sibling DLLs
2. Create all COM objects
3. `SetDllDirectoryW(None)` — restore default search for system network DLLs

### Chart in Frozen EXE

`lightweight_charts` uses pywebview which starts a local HTTP server. In frozen EXEs, this server silently fails. The fix patches `abstract.INDEX` to a `file://` URL at **module level** (not inside `__main__`) so both the main process and the multiprocessing child process (where WebView2 runs) get the patch. `freeze_support()` intercepts child processes before any code after it runs.

## Changelog

### v1.0.0 (2026-03-02)

- AI Strategy Workbench with Claude/Gemini chat integration
- Backtesting engine with candlestick chart (Bollinger Bands, trade markers)
- Live bot deployment with tick-based 1m bar building and N-minute aggregation
- Multiple data sources: Capital API (COM KLine) and TradingView
- Multi-symbol support: TX00 (大台指), MTX00 (小台指)
- Pine Script export with 繁體中文 comments
- PyInstaller EXE packaging with bundled SDK DLLs
- Auto COM registration on first run
- 196 tests
