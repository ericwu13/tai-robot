# tai-robot - Taiwan Futures Trading Bot

**Version: 1.0.2**

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

`build_release.py` does **not** pip-install into the active interpreter. A normal build uses whatever PyInstaller and comtypes that interpreter already has. `--no-install` is accepted and does the same thing. `--skip-release` builds and zips only (no GitHub release, no Discord).

```bash
python build_release.py --skip-release
```

A fresh `.venv-build` needs this project's packages as well as the toolchain. PyInstaller analyzes imports of pyyaml, keyring, httpx, lightweight-charts, pandas, and holidays. `--install-build-deps` always installs both into the target interpreter, including when the PyInstaller and comtypes pins already match: `pip install -e .` (those packages) and `pip install -r requirements-build.txt` (the pins). Pin checks before and after that install query the same interpreter, and PyInstaller runs with it. Prefer a dedicated build venv (`.venv-build/`, gitignored) so comtypes is not downgraded in a dev or live venv. When `.venv-build` exists, the flag installs into its interpreter. Otherwise it prints a warning naming the active interpreter (`sys.executable`) and installs there.

```bash
python -m venv .venv-build
# POSIX — manual equivalent of the flag
.venv-build/bin/python -m pip install -e .
.venv-build/bin/python -m pip install -r requirements-build.txt
.venv-build/bin/python -m PyInstaller tai_backtest.spec --noconfirm
# Windows
.venv-build\Scripts\python -m pip install -e .
.venv-build\Scripts\python -m pip install -r requirements-build.txt
.venv-build\Scripts\python -m PyInstaller tai_backtest.spec --noconfirm
```

`--install-build-deps` runs that `pip install -e .` and the requirements file on the target interpreter, then builds:

```bash
# POSIX
.venv-build/bin/python build_release.py --skip-release --install-build-deps
# Windows
.venv-build\Scripts\python build_release.py --skip-release --install-build-deps
```

`requirements-build.txt` pins `PyInstaller==6.19.0` and `comtypes==1.4.15`. Those pins are secondary. The comtypes 1.4.15 wheel does not ship a `comtypes/gen/` package (1.4.17 does not either), so `import comtypes.gen` fails until the runtime hook creates it. That hook is the startup fix. `pyinstaller-hooks-contrib` is not pinned: PyInstaller 6.19.0 requires `pyinstaller-hooks-contrib>=2026.0`, and pip may install any newer release that satisfies that. No specific hooks-contrib version was recorded with the Windows smoke.

The #194 Windows checklist (`docs/testing/grid_subprocess_windows_checklist.md`) checks out `cf58c7c`, which predates `--install-build-deps`. It creates `.venv-build` and runs `.\.venv-build\Scripts\python.exe` for `pip install -e .` (pyyaml, keyring, httpx, lightweight-charts, pandas, holidays) plus the pins, then that same interpreter runs `build_release.py --skip-release`. That command does not pip-install. Do not Activate, and do not build with the system Python. The empty `src\strategies` and `strategies` directories in that checklist are for the commit it checks out, whose spec still lists `('strategies', 'strategies')`. This branch's spec stages a placeholder instead.

Direct invocation, after the project and the pins are installed in the interpreter you intend to build with:

```bash
pip install -e .
pip install -r requirements-build.txt
python -m PyInstaller tai_backtest.spec --noconfirm
# or: pyinstaller tai_backtest.spec --noconfirm
```

The spec inserts `SPECPATH` onto `sys.path` so `import build_release` works for the `pyinstaller` entry-point script as well as `python -m PyInstaller`.

The Capital API SDK is gitignored. A worktree or fresh clone has no `CapitalAPI_2.13.57/` tree. Broker DLLs are read from this libs directory. `SKCOM.dll` and `CTSecuritiesATL.dll` must both be present and nonzero (`run_backtest.py` registers `CTSecuritiesATL.dll`, and `SKCOM.dll` loads its sibling DLLs from the same folder):

`CapitalAPI_2.13.57/CapitalAPI_2.13.57_PythonExample/SKDLLPythonTester/libs`

`tai_backtest.spec` and `build_release.py` both refuse to build when that directory is missing or either required DLL is missing or empty. The error names the file. Set `TAI_CAPITAL_SDK_DIR` to a libs directory that contains both files when the SDK lives outside the repo. A relative value is resolved against the project root — the directory that contains `tai_backtest.spec` (`SPECPATH` on a raw `pyinstaller` run) — not the shell's current directory. CI and Linux test builds that are not packaging a broker EXE can set `TAI_ALLOW_MISSING_CAPITAL_SDK=1` or pass `--allow-missing-broker-sdk`. Only the value `1` opts out; `true` or `yes` prints a warning and the build still fails. The opt-out prints a warning and the build continues; if a required DLL is still absent, the EXE has no broker DLLs.

```bash
export TAI_CAPITAL_SDK_DIR=/path/to/SKDLLPythonTester/libs
# or, relative to the project root:
export TAI_CAPITAL_SDK_DIR=CapitalAPI_2.13.57/CapitalAPI_2.13.57_PythonExample/SKDLLPythonTester/libs
python build_release.py --skip-release
```

Output: `dist/tai_backtest/tai_backtest.exe`

`strategies/` is gitignored and holds per-machine AI strategies. A fresh clone does not have that directory. The spec does not bundle it. At build time it stages an empty placeholder under `build/strategies_placeholder/` (a `README.txt` only) so PyInstaller has a datas source, and private `.py` files are never copied into the EXE. Saved strategies are loaded at runtime from the `strategies/` directory next to `tai_backtest.exe`, which the app creates on first use.

`runtime_hook_comtypes.py` points `comtypes.gen` at a writable `_comtypes_cache` next to the EXE. If that package is missing (`ModuleNotFoundError.name == 'comtypes.gen'`), the hook creates it in the cache. A broken `comtypes.gen` whose own import failed is not swallowed. The spec also lists `comtypes.gen` in `hiddenimports`. Confirming the EXE process actually starts still requires Windows.

### Build Files

| File | Purpose |
|------|---------|
| `requirements-build.txt` | Pinned PyInstaller and comtypes for the EXE build |
| `build_release.py` | Runs the spec and zips the dist. Does not pip-install unless `--install-build-deps` (then `pip install -e .` and the pins, into the target interpreter) |
| `tai_backtest.spec` | PyInstaller build specification (--onedir mode) |
| `runtime_hook_comtypes.py` | Runtime hook for writable comtypes.gen cache |

### What Gets Bundled

- SDK DLLs (8 files from `CapitalAPI_2.13.57/.../libs/`) into `_internal/libs/`
- `lightweight_charts/js/` (WebGL chart assets)
- `src/`
- an empty `strategies/` placeholder (private `strategies/` is not packaged)
- `settings.example.yaml` as config template

## Deploying the EXE

### First-time Setup (per machine)

1. Extract the release zip (or copy the `tai_backtest/` folder) to the target machine
2. **Windows SmartScreen**: On first launch, Windows may block the EXE with "Windows protected your PC". Click **More info** → **Run anyway**. This only happens once.
3. Place your `settings.yaml` next to `tai_backtest.exe` (not inside `_internal/`)
4. **Run `tai_backtest.exe` as Administrator once** (right-click > Run as administrator)
   - This auto-registers the COM DLLs via `regsvr32`
   - The console will show registration results — check for errors
   - Subsequent runs do not need admin
5. **Install the Capital API certificate** (required per machine)
   - Download from your broker's website (群益證券)
   - Install the `.pfx` or `.p12` certificate file
   - Without the certificate, login will fail with error 1097

### Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| "Windows protected your PC" | SmartScreen blocks unsigned EXE | Click **More info** → **Run anyway** |
| EXE doesn't launch at all | Missing VC++ runtime | Install [Visual C++ Redistributable](https://aka.ms/vs/17/release/vc_redist.x64.exe) |
| Login error 1097 | Certificate not installed | Install API certificate from broker website |
| COM registration failed | Not running as admin | Right-click EXE → Run as administrator (once) |
| Blank chart window | WebView2 not installed | Install [WebView2 Runtime](https://go.microsoft.com/fwlink/p/?LinkId=2124703) |

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

## Headless CLI (deploy bots without the GUI)

The headless CLI drives the **same** live-bot code as the workbench — it
constructs `BacktestApp` on a hidden Tk window, so order handling, fill
tracking, reconnects, session-end close and regime polling are identical to
a GUI deploy. Only the dialogs are replaced by flags.

Two ways to invoke it. Source checkouts (oreopie) use `python run_bot_cli.py`.
A packaged install has no Python on PATH: run `tai_backtest.exe <command>`
from the folder that contains the EXE. The EXE routes
`deploy|stop|list|status|strategies|backtest` to the CLI and exits with
that command's code. No arguments, `-h`/`--help`, or an unknown first
token opens the GUI (double-click is unchanged). v2.19.2 shipped the
source CLI; that tag's note that headless "ships in this tag" did not
route the release EXE. Headless from `tai_backtest.exe` starts with the
cut that contains this packaging change.

```bash
# --- packaged install (cmd / PowerShell, cd to the EXE folder) ---
tai_backtest.exe strategies
tai_backtest.exe list
tai_backtest.exe status --symbol TMF00 --bot night1 --json
tai_backtest.exe deploy --symbol TMF00 --bot night1 --strategy "H4 Bollinger Long" --mode paper --detach --wait-ready 240
tai_backtest.exe stop --symbol TMF00 --bot night1 --wait 60

# --- source checkout ---
python run_bot_cli.py strategies                       # display names (incl. saved AI: ...)
python run_bot_cli.py list                             # every data/live/{symbol}_{bot} + lock state
python run_bot_cli.py status --symbol TMF00 --bot night1 --json

# paper bot, new session (resumes automatically if session.json exists);
# --detach spawns it as a background process and waits until ticks are subscribed
python run_bot_cli.py deploy --symbol TMF00 --bot night1 --strategy "H4 Bollinger Long" \
    --detach --wait-ready 240

# regime-switching bot with the news breaker, real orders
python run_bot_cli.py deploy --symbol TMF00 --bot regime1 --mode auto --regime \
    --long-strategy "H4 Bollinger Long" --short-strategy "AI: SomeShort" \
    --news --news-directional --loss-limit 1500

# stop: writes data/live/{symbol}_{bot}/STOP, the CLI bot force-closes,
# saves the session, writes the daily report and exits
python run_bot_cli.py stop --symbol TMF00 --bot night1 --wait 60

# backtest from the shell (tv = TradingView download, api = Capital API login)
python run_bot_cli.py backtest --symbol TX00 --strategy "1m SMA" \
    --start 20260101 --end 20260301 --source tv --export trades.csv
```

Notes:

- Credentials come from `settings.yaml` (`credentials.user_id/password`);
  `TAI_USER_ID` / `TAI_PASSWORD` environment variables override them.
- `--mode semi_auto` is refused: it needs a human on the 10-second
  order-confirm dialog. Headless modes are `paper` and `auto`.
- Resume rules match the GUI: `--strategy` on a resumed bot deploys the
  selected strategy (keeps history/watermark); without it the saved strategy
  is used. `--new` refuses to resume, `--resume` refuses to start fresh.
- A real-order deploy waits for the futures account from the API and, like
  the GUI, aborts if the account already holds a position unless
  `--allow-existing-position` is given.
- Ctrl+C is the same as `stop`. Only CLI-started bots poll the STOP file; a
  GUI bot is stopped from the workbench.
- A running headless bot also polls `data/live/{symbol}_{bot}/CONTROL`
  every 2 seconds. Write one line; the bot renames the file to
  `CONTROL.ack` and applies it once:

  ```
  reconnect
  set-mode paper
  ```

  `reconnect` uses the same path as the workbench Reconnect button.
  `set-mode paper` switches the running bot to paper and honours the
  mode-switch veto (an open fill, for example, keeps the current mode).
  `set-mode auto`, `set-mode real`, and any other text are rejected and
  logged, including when `allow_live_override` is set. GUI bots do not
  read this file.
- `--detach` re-runs the command as a detached child (stdout appended to
  `data/live/{symbol}_{bot}/cli_stdout.log`) and, with `--wait-ready N`,
  returns `0` once the child owns the lock and logged `Tick subscription
  active`, the child's exit code if it died first, or `6` if it is still
  starting after N seconds (the child keeps running). From the EXE the
  child is `tai_backtest.exe <subcommand> …` (no `.py` argument). From
  source it is `python run_bot_cli.py <subcommand> …`. Both use the same
  `data/live` directory as the GUI, so `list`/`status` see either kind of bot.
- Agents: the `deploy` skill (`.claude/skills/deploy`) wraps all of this
  with the safety gates (mode consent, not-already-running, strategy
  resolution, resume vs switch).
- Exit codes: `0` ok · `1` usage · `2` login/connection · `3` COM unavailable ·
  `4` deploy refused (lock held, unknown strategy, config error) ·
  `5` bot stopped on its own (warmup/subscription failure — see
  `data/live/{symbol}_{bot}/debug_YYYYMMDD.log`) · `6` timeout · `7` backtest failed.
- The process must run in a desktop session (hidden Tk window); a Windows
  service running as LocalSystem is not supported.

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

`lightweight_charts` uses pywebview, which serves the chart page from a local HTTP server. Both dev and the frozen EXE use this HTTP server directly.

Historically the frozen EXE patched `abstract.INDEX` to a `file://` URL to work around the pywebview HTTP server appearing to fail in packaged builds. That workaround was removed after a WebView2 runtime update (~150.0.4078.83, 2026-07-24) broke GPU compositing on `file://` origins — candles stopped painting in the packaged EXE while dev (HTTP) worked fine. Serving over HTTP in all builds fixes the rendering and removes the `file://`-specific failure mode.

## Changelog

### v1.0.4 (2026-03-04)

- 重構 AI 提示詞為可組合模組，加入 few-shot 語氣範例
- 新增程式碼生成專用 persona，輸出結構化結果（程式碼 + Notes 說明）
- 修復 Pine Script 匯出：`ta.bb()` / `ta.macd()` 解構語法（`[a,b,c]=` 而非 `(a,b,c)=`）
- 新增 Pine Script 常見錯誤提示，防止 TradingView 語法錯誤

### v1.0.3 (2026-03-04)

- 新增聊天記錄持久化，修復 AI 策略原始碼檢視器

### v1.0.2 (2026-03-03)

- Include settings.yaml with default values in release (no longer requires manual creation)
- Include 必看安裝說明.txt (installation guide in Traditional Chinese) in release
- Release notes in Traditional Chinese

### v1.0.1 (2026-03-03)

- Fix: exclude data/ folder and sensitive files from release zip
- Fix: default authority_flag to 0 (production) in example config
- Fix: improved COM registration diagnostics (regsvr32 result logging)
- Fix: error 1097 now shows certificate installation hint
- Add: troubleshooting table and SmartScreen bypass instructions in README
- Remove: dead-weight bundled data/ from PyInstaller spec

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
