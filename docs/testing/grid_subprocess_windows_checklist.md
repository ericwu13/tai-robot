# Windows frozen-EXE checklist — grid sweep subprocess

Operator checklist for the spawn-child grid sweep (PR #194, code commit
`644eac97d83c0246fbecd9d9497ec6818b0c8c04`). Run it on a Windows machine, in
a folder that is not a live bot's directory. This copy does not log into
the broker.

The binary under test is built from `644eac97`. This checklist is a later
docs commit on the same branch. Do not build from a dirty tree, and do not
publish a GitHub release.

## Isolation rules

- Fresh directory, for example `C:\tai-robot-grid-check`. Do not reuse a
  live bot folder, its `data\`, or its `settings.yaml`.
- `settings.yaml` is a copy of `settings.example.yaml`. Leave every Capital
  user id, password, and certificate field empty.
- Do not click **登入 Login**. Do not complete **部署機器人 Deploy Bot**.
- Paper / backtest only. TAIFEX public daily data is not a broker login.
- One `tai_backtest.exe`. Close any other copy before you start.

## Build (clean venv, pinned spec, commit 644eac97)

No GitHub Actions workflow builds this EXE. Build it locally. The spec is
`tai_backtest.spec` (PyInstaller `--onedir`, `console=True`, runtime hook
`runtime_hook_comtypes.py`). The repo does not pin a PyInstaller version.

`python build_release.py` with no flags creates a GitHub release and can
send Discord. Use `--skip-release`.

```powershell
cd C:\
git clone https://github.com/ericwu13/tai-robot.git tai-robot-grid-src
cd C:\tai-robot-grid-src
git fetch origin
git checkout 644eac97d83c0246fbecd9d9497ec6818b0c8c04
git rev-parse HEAD
# expect 644eac97d83c0246fbecd9d9497ec6818b0c8c04

py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install pyyaml keyring comtypes httpx lightweight-charts pandas holidays pyinstaller

Copy-Item settings.example.yaml settings.yaml
# Do not paste live credentials into this file.

python build_release.py --skip-release
```

Pass: `git rev-parse HEAD` prints `644eac97d83c0246fbecd9d9497ec6818b0c8c04`,
PyInstaller exits 0, and these exist:

- `dist\tai_backtest\tai_backtest.exe`
- `dist\tai_backtest\BUILD_INFO.json` with `"commit"` equal to that SHA
  and `"dirty": false`

The Capital API SDK directory is gitignored. If it is absent, the spec
bundles no `SKCOM` DLLs. That is fine for this checklist. Do not copy DLLs
or `settings.yaml` out of a live install.

Copy the folder, then run the copy. Leave the git checkout alone.

```powershell
Copy-Item -Recurse C:\tai-robot-grid-src\dist\tai_backtest C:\tai-robot-grid-check
cd C:\tai-robot-grid-check
.\tai_backtest.exe
```

The frozen app uses the directory of the EXE as its project root
(`data\` and `_comtypes_cache` are created beside `tai_backtest.exe`).

## What you are looking for

During an eligible sweep the worker starts a `spawn` child. On a frozen
EXE that child is another `tai_backtest.exe` whose command line contains
`--multiprocessing-fork`. The child runs the grid and exits. It must not
import comtypes, must not write a comtypes gen cache, and must not open a
window.

There is no CLI command that starts a sweep. `run_bot_cli.py` has
`deploy|stop|list|status|strategies|backtest` and no evolve subcommand.
The on-demand trigger is the toolbar button **🧬 進化 Evolution**
(`_bot_evolution`, `auto_run=False`). The Saturday path is the same
function with `auto_run=True` after the Friday-night session; do not wait
for it.

The grid runs only when all of the following are true:

- `evolution.auto_pipeline` is true (the example default).
- `evolution.search` is `grid_batch` (the example default). `llm_one`
  stays on the plan path and starts no child.
- The loaded class name contains `DynamicExitPullback` and the source
  has at least two literal `kwargs.get` knobs on the Saturday grid.
  Built-in strategies (SMA cross, Bollinger, news) do not start a child.
- The backtest has at least one trade, and at least one of those trades
  sits outside the 14-day holdout (`evolution.holdout_days`). If every
  trade is inside the holdout, the chat says `訓練窗沒有交易` and the
  sweep never starts.
- A chat API key is set. `_bot_evolution` returns before the grid when
  the chat client is missing. The key is not a broker credential. Do not
  put a Capital password in `settings.yaml`. The grid itself does not ask
  the model to pick the setting. The chat line that means the child
  should exist is:

  `EVO 1/3: 預先登記的旋鈕網格評分中`

## 0. Prepare the isolated copy

1. Start `C:\tai-robot-grid-check\tai_backtest.exe`.
2. Open **▶ 設定 Settings** and confirm `settings.yaml` has empty broker
   fields. If the app asks for an AI key, enter only that key.
3. Still in Settings, click **匯入 Import**. Choose
   `C:\tai-robot-grid-src\dynamic_exit_pullback_strategy_v2.py`
   (present at `644eac97`). Confirm the import. The strategy combo should
   show `AI: DynamicExitPullbackStrategyV2`.
4. Set the backtest range to more than 14 days (Start well before End).
5. Click **TAIFEX 回測 Backtest**. Accept the daily-bar warning. This
   download is the public TAIFEX API, not a broker login.
6. Pass for this step: the status line reports at least one trade, and
   **🧬 進化 Evolution** is enabled. Zero trades leaves the button
   disabled. Widen the range and run TAIFEX again.

Do not click **登入 Login** or **部署機器人 Deploy Bot**.

## Process snapshot command

Use this whenever a step says "snapshot processes".

```powershell
Get-CimInstance Win32_Process -Filter "Name = 'tai_backtest.exe'" |
  Select-Object ProcessId, ParentProcessId, CreationDate, CommandLine |
  Format-List
```

The parent is the process you launched. Its command line is the EXE path
and does not contain `--multiprocessing-fork`. The child is the process
whose `CommandLine` contains `--multiprocessing-fork` and whose
`ParentProcessId` is the parent's PID.

## (a) Child appears during an on-demand sweep

1. Snapshot processes. Expect one `tai_backtest.exe` and no
   `--multiprocessing-fork`.
2. Click **🧬 進化 Evolution**.
3. If a dialog says the design window has no new trades since the last
   evolution, click **No**. This fresh copy should not show that dialog.
   If it does, you are pointed at an old `data\` directory. Stop and fix
   the folder before continuing.
4. Watch the chat. As soon as `EVO 1/3: 預先登記的旋鈕網格評分中` appears,
   snapshot processes again. The grid is at most 48 backtests. Snapshot
   immediately. A fast machine can finish before a slow snapshot.

Pass:

- A second `tai_backtest.exe` exists.
- Its command line contains `--multiprocessing-fork`.
- Its parent PID is the workbench PID.
- The chat showed the EVO 1/3 line before that process appeared.

Fail:

- No second process, and the chat went to a plan (`Bot Evolution`)
  without the EVO 1/3 line. The class was not grid-eligible, or
  `evolution.search` is `llm_one`, or `evolution.auto_pipeline` is false.
- The second process has no `--multiprocessing-fork` argument.
- The chat says `訓練窗沒有交易` or `No design-window trades`.

There is no Saturday wait and no CLI evolve command. This button is the
on-demand trigger.

## (b) Child writes no comtypes cache and loads no Capital COM DLL

The frozen hook creates `{exe_dir}\_comtypes_cache` for the main process
only. It skips that directory and skips `import comtypes` when `argv[1]`
is `--multiprocessing-fork`. Stock comtypes locations
(`%LOCALAPPDATA%\comtypes_cache` and a `comtypes\gen` tree) are not the
cache this EXE uses. Check them anyway.

Take a cache snapshot after the EXE is idle, before Evolution, and again
while the child in (a) is alive.

```powershell
$roots = @(
  'C:\tai-robot-grid-check\_comtypes_cache',
  "$env:LOCALAPPDATA\comtypes_cache",
  'C:\tai-robot-grid-check\_internal\comtypes\gen'
)
Get-ChildItem -Recurse -Force -ErrorAction SilentlyContinue -Path $roots |
  Select-Object FullName, Length, LastWriteTime |
  Format-Table -AutoSize
```

Replace the child PID from the (a) snapshot:

```powershell
tasklist /m /fi "PID eq CHILD_PID"
```

If Sysinternals `listdlls` is installed:

```powershell
listdlls -p CHILD_PID
```

Pass:

- During the child's lifetime, no file under the three roots has a
  `LastWriteTime` newer than the pre-Evolution snapshot, and no new path
  appears. The parent may have created `_comtypes_cache` at its own
  startup. That directory existing before the click is not a fail.
- The child's module list does not contain `SKCOM.dll`,
  `CTSecuritiesATL.dll`, or any other DLL loaded from
  `_internal\libs`.

`combase.dll`, `ole32.dll`, and `oleaut32.dll` are Windows libraries.
Record whether they are loaded. Their presence is not a fail by itself.
A fail is a Capital / SKCOM DLL, or a gen-cache file written while the
child is alive.

## (c) No second window

The spec sets `console=True`, so the workbench you launched has its Tk
window and a console. The child must not add another window.

While the child is alive:

```powershell
Get-Process tai_backtest |
  Select-Object Id, MainWindowHandle, MainWindowTitle |
  Format-List
```

Pass: one window on the taskbar (the workbench you opened). The child PID
has `MainWindowHandle` 0 and an empty `MainWindowTitle`. No second Tk
window and no extra console.

Fail: a second window, a second console, or a non-zero
`MainWindowHandle` on the `--multiprocessing-fork` PID.

## (d) STOP file, GUI stop, and timeout

### No short-timeout knob

Nothing in `settings.yaml` and no environment variable changes the sweep
timeout. The constant is `DEFAULT_GRID_TIMEOUT_S = 20 * 60` (1200 seconds)
in `src/evolution/grid_process.py`. The worker uses the instance attribute
`_grid_timeout_s` when a test sets it, and otherwise uses 1200. The frozen
EXE never sets that attribute. Do not edit the constant and do not invent
a setting. A binary built from `644eac97` cannot be told to time out early.

The worker polls stop every 0.2 seconds (`poll_s` default in
`run_grid_isolated`). On timeout or stop it calls `TerminateProcess` and
the chat reports no trial evidence.

### STOP file and GUI stop need a deployed bot directory

Both controls are wired, and neither is reachable under this checklist's
no-login rule.

- GUI stop is the toolbar button after deploy has changed its label to
  **停止機器人 Stop Bot**. That calls `_stop_live`, which sets
  `_evolution_cancel`. The label stays **部署機器人 Deploy Bot** until a
  deploy is in progress. Clicking Deploy does not cancel a backtest-only
  sweep.
- The STOP file is `data\live\<symbol>_<bot_name>\STOP` (the live
  runner's `bot_dir`). The sweep polls it only when that runner exists
  and is not idle. A backtest-only **🧬 進化 Evolution** click leaves
  `bot_dir` unset, so the file is ignored. `run_bot_cli.py stop` writes
  the same file for a bot that was deployed headless. It does not start
  a sweep, and it does not affect a backtest-only sweep.

Deploy does not stay up without a quote login. With no quote connection,
tick subscription logs `未連線 Not connected, cannot subscribe to ticks`
and calls `_stop_live` immediately. Keeping **停止機器人 Stop Bot** on
screen requires **登入 Login**. This checklist does not do that.

While the child from (a) is alive, confirm the negative result. Do not
log in. If Deploy opens a dialog, click Cancel.

```powershell
# This file is not consulted during a backtest-only sweep.
New-Item -ItemType Directory -Force C:\tai-robot-grid-check\data\live\TX00_gridcheck | Out-Null
Set-Content -Encoding utf8 C:\tai-robot-grid-check\data\live\TX00_gridcheck\STOP "checklist negative test`n"
Get-CimInstance Win32_Process -Filter "Name = 'tai_backtest.exe'" |
  Select-Object ProcessId, CommandLine
```

Pass for this isolated copy:

- After writing that STOP file, the `--multiprocessing-fork` process is
  still running.
- Clicking **部署機器人 Deploy Bot** does not log in. Cancel any dialog.
  The child is still running.
- Record (d) as **not exercised**. Do not mark STOP or GUI stop as passed.

What a later run with a real `bot_dir` would check (do not do this here):

```powershell
Set-Content -Encoding utf8 C:\tai-robot-grid-check\data\live\<symbol>_<bot_name>\STOP "stop`n"
```

or click **停止機器人 Stop Bot**. Pass would be: within a couple of
seconds the `--multiprocessing-fork` PID is gone, and the chat contains
`已要求停止，網格子行程已終止` / `Stop requested — the grid subprocess was terminated`.

### Timeout

Do not shorten 1200 seconds. If the child is still alive 20 minutes after
the EVO 1/3 line:

```powershell
Get-CimInstance Win32_Process -Filter "Name = 'tai_backtest.exe'" |
  Select-Object ProcessId, CommandLine
```

Pass: the `--multiprocessing-fork` PID is gone, and the chat contains
`網格子行程逾時` / `Grid subprocess timed out`.

If the sweep finishes before 20 minutes, the timeout path was not
observed. Record that. It is the expected result for a normal grid, and
there is no knob that forces the short case.

## Evidence to paste back

```
commit built: 644eac97d83c0246fbecd9d9497ec6818b0c8c04
BUILD_INFO.json commit / dirty:
exe path:
settings broker fields empty (yes/no):
logged into broker (must be no):

(a) parent PID / command line:
(a) child PID / command line (must contain --multiprocessing-fork):
(a) chat line at snapshot:

(b) cache listing BEFORE Evolution:
(b) cache listing WHILE child alive (no new paths, no newer timestamps):
(b) tasklist /m (or listdlls) for child PID:
(b) SKCOM / CTSecuritiesATL / _internal\libs DLL in child (must be no):
(b) combase.dll / ole32.dll present (record only):

(c) MainWindowTitle per PID:
(c) second window or extra console (must be no):

(d) STOP file written at (path):
(d) child still running after that file (expected yes on this copy):
(d) Deploy clicked and cancelled without login (yes/no):
(d) STOP / GUI stop result: not exercised | passed on a deployed bot_dir
(d) timeout: not observed (sweep finished before 1200s) | child gone with 網格子行程逾時
    knob used: none (no settings key, no environment variable)

screenshots: process list, chat EVO 1/3 line, tasklist /m, window list
```
