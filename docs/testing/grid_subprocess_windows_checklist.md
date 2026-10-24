# Windows frozen-EXE checklist — grid sweep subprocess

Operator checklist for the spawn-child grid sweep (PR #194, code commit
`644eac97d83c0246fbecd9d9497ec6818b0c8c04`).

Run this on the Windows test host **oreopie**, and only inside
`C:\tai-test-194\`. That folder is a fresh copy with its own `data\`
directory. It does not log into the broker.

The binary under test is built from `644eac97`. This checklist is a later
docs commit on the same branch. Do not build from a dirty tree, and do not
publish a GitHub release.

## Do not touch the live bots

oreopie also runs live bots. This checklist does not name their folders.
Treat every `tai_backtest.exe` whose path is **not** under
`C:\tai-test-194\` as a live bot.

- Do not open, edit, copy, or delete anything outside `C:\tai-test-194\`.
  That includes another checkout, another `settings.yaml`, another
  `data\`, and another `_comtypes_cache`.
- Do not click Stop on a live window. Do not write a `STOP` file anywhere
  except the path in section (d), which is under `C:\tai-test-194\app\`.
- Do not run `taskkill /IM tai_backtest.exe`. Do not run
  `Stop-Process -Name tai_backtest`. Either command kills every bot on
  the machine, including the live ones.
- Kill a process only after `ExecutablePath` starts with
  `C:\tai-test-194\` and you pass that PID to `Stop-Process -Id`.
  The cleanup block at the bottom is the only kill step.
- `%LOCALAPPDATA%\comtypes_cache` is shared by the Windows user. Read it
  if you want. Do not delete it.

Before you start, record the live PIDs and confirm they are still there
when you finish:

```powershell
Get-CimInstance Win32_Process -Filter "Name = 'tai_backtest.exe'" |
  Where-Object { $_.ExecutablePath -notlike 'C:\tai-test-194\*' } |
  Select-Object ProcessId, ExecutablePath |
  Format-List
```

A live PID in that list must have the same number at the end of the
checklist. If a command would affect a path outside `C:\tai-test-194\`,
do not run it.

## Isolation rules

- All files for this run live under `C:\tai-test-194\`.
  `C:\tai-test-194\src` is the git checkout.
  `C:\tai-test-194\app` is the folder you launch.
- `C:\tai-test-194\app\settings.yaml` is a copy of
  `settings.example.yaml`. Leave every Capital user id, password, and
  certificate field empty. Do not copy a live `settings.yaml` into this
  tree.
- Do not click **登入 Login**. Do not complete **部署機器人 Deploy Bot**.
- Paper / backtest only. TAIFEX public daily data is not a broker login.
- The test workbench is `C:\tai-test-194\app\tai_backtest.exe`. Leave
  every other `tai_backtest.exe` running.

## Build (clean venv, pinned spec, commit 644eac97)

No GitHub Actions workflow builds this EXE. Build it on oreopie, inside
the test folder. The spec is `tai_backtest.spec` (PyInstaller `--onedir`,
`console=True`, runtime hook `runtime_hook_comtypes.py`). The repo does
not pin a PyInstaller version.

`python build_release.py` with no flags creates a GitHub release and can
send Discord. Use `--skip-release`.

```powershell
New-Item -ItemType Directory -Force C:\tai-test-194 | Out-Null
cd C:\tai-test-194
git clone https://github.com/ericwu13/tai-robot.git src
cd C:\tai-test-194\src
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

- `C:\tai-test-194\src\dist\tai_backtest\tai_backtest.exe`
- `C:\tai-test-194\src\dist\tai_backtest\BUILD_INFO.json` with `"commit"`
  equal to that SHA and `"dirty": false`

The Capital API SDK directory is gitignored. If it is absent, the spec
bundles no `SKCOM` DLLs. That is fine for this checklist. Do not copy DLLs
or `settings.yaml` out of a live install.

Copy the built folder to the launch directory. Leave the git checkout
alone after that. The EXE's project root is the directory that contains
the EXE, so `data\` and `_comtypes_cache` are created under
`C:\tai-test-194\app\`.

```powershell
Copy-Item -Recurse C:\tai-test-194\src\dist\tai_backtest C:\tai-test-194\app
cd C:\tai-test-194\app
.\tai_backtest.exe
```

## Which processes are the test

Use this whenever a step says "snapshot test processes". It cannot return
a live bot, because of the path filter.

```powershell
Get-CimInstance Win32_Process -Filter "Name = 'tai_backtest.exe'" |
  Where-Object { $_.ExecutablePath -like 'C:\tai-test-194\*' } |
  Select-Object ProcessId, ParentProcessId, CreationDate, ExecutablePath, CommandLine |
  Format-List
```

The parent is the process you launched from `C:\tai-test-194\app\`. Its
command line is that EXE path and does not contain `--multiprocessing-fork`.
The child is the process whose `CommandLine` contains `--multiprocessing-fork`,
whose `ExecutablePath` is still under `C:\tai-test-194\`, and whose
`ParentProcessId` is the parent's PID.

A `tai_backtest.exe` with any other `ExecutablePath` is a live bot. Do not
include it in the pass/fail notes except to record that its PID was left
alone.

## What you are looking for

During an eligible sweep the worker starts a `spawn` child. On a frozen
EXE that child is another `tai_backtest.exe` under `C:\tai-test-194\`
whose command line contains `--multiprocessing-fork`. The child runs the
grid and exits. It must not import comtypes, must not write a comtypes
gen cache, and must not open a window.

There is no CLI command that starts a sweep. `run_bot_cli.py` has
`deploy|stop|list|status|strategies|backtest` and no evolve subcommand.
Do not run `run_bot_cli.py stop` on oreopie: that writes a `STOP` file
into a bot directory and can stop a live bot. The on-demand trigger is
the toolbar button **🧬 進化 Evolution** on the test window
(`_bot_evolution`, `auto_run=False`). The Saturday path is the same
function with `auto_run=True` after the Friday-night session; do not wait
for it, and do not trigger it on a live bot.

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

1. Record live PIDs with the command in "Do not touch the live bots".
2. Start `C:\tai-test-194\app\tai_backtest.exe` only.
3. Open **▶ 設定 Settings** on that window and confirm `settings.yaml`
   has empty broker fields. If the app asks for an AI key, enter only
   that key.
4. Still in Settings, click **匯入 Import**. Choose
   `C:\tai-test-194\src\dynamic_exit_pullback_strategy_v2.py`
   (present at `644eac97`). Confirm the import. The strategy combo should
   show `AI: DynamicExitPullbackStrategyV2`.
5. Set the backtest range to more than 14 days (Start well before End).
6. Click **TAIFEX 回測 Backtest**. Accept the daily-bar warning. This
   download is the public TAIFEX API, not a broker login.
7. Pass for this step: the status line reports at least one trade, and
   **🧬 進化 Evolution** is enabled. Zero trades leaves the button
   disabled. Widen the range and run TAIFEX again.

Do not click **登入 Login** or **部署機器人 Deploy Bot**.

## (a) Child appears during an on-demand sweep

1. Snapshot test processes. Expect one EXE under `C:\tai-test-194\` and
   no `--multiprocessing-fork` on that path. Live PIDs may exist outside
   that path. Leave them out of this count.
2. On the test window, click **🧬 進化 Evolution**.
3. If a dialog says the design window has no new trades since the last
   evolution, click **No**. This fresh copy should not show that dialog.
   If it does, the test EXE is using some other `data\` directory. Stop
   and fix the folder before continuing. Do not point it at a live bot
   directory.
4. Watch the test window's chat. As soon as
   `EVO 1/3: 預先登記的旋鈕網格評分中` appears, snapshot test processes
   again. The grid is at most 48 backtests. Snapshot immediately. A fast
   machine can finish before a slow snapshot.

Pass:

- A second `tai_backtest.exe` exists with `ExecutablePath` under
  `C:\tai-test-194\`.
- Its command line contains `--multiprocessing-fork`.
- Its parent PID is the test workbench PID (also under that folder).
- The chat showed the EVO 1/3 line before that process appeared.
- No live PID from the opening list was stopped.

Fail:

- No second process under `C:\tai-test-194\`, and the chat went to a plan
  (`Bot Evolution`) without the EVO 1/3 line. The class was not
  grid-eligible, or `evolution.search` is `llm_one`, or
  `evolution.auto_pipeline` is false.
- The second process has no `--multiprocessing-fork` argument.
- The chat says `訓練窗沒有交易` or `No design-window trades`.
- Any process outside `C:\tai-test-194\` changed.

There is no Saturday wait and no CLI evolve command. This button on the
test window is the on-demand trigger.

## (b) Child writes no comtypes cache and loads no Capital COM DLL

The frozen hook creates `{exe_dir}\_comtypes_cache` for the main process
only. For this run that directory is
`C:\tai-test-194\app\_comtypes_cache`. The hook skips that directory and
skips `import comtypes` when `argv[1]` is `--multiprocessing-fork`.
Stock comtypes locations (`%LOCALAPPDATA%\comtypes_cache` and a
`comtypes\gen` tree) are not the cache this EXE uses. You may read them.
Do not delete them, and do not look in a live bot's `_comtypes_cache`.

Take a cache snapshot after the test EXE is idle, before Evolution, and
again while the child in (a) is alive.

```powershell
$roots = @(
  'C:\tai-test-194\app\_comtypes_cache',
  "$env:LOCALAPPDATA\comtypes_cache",
  'C:\tai-test-194\app\_internal\comtypes\gen'
)
Get-ChildItem -Recurse -Force -ErrorAction SilentlyContinue -Path $roots |
  Select-Object FullName, Length, LastWriteTime |
  Format-Table -AutoSize
```

Replace the child PID from the (a) snapshot. `tasklist /m` and `listdlls`
take a PID, so they inspect that process only. Confirm the PID is the
test child first (path under `C:\tai-test-194\`, command line contains
`--multiprocessing-fork`).

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
  appears. The parent may have created
  `C:\tai-test-194\app\_comtypes_cache` at its own startup. That
  directory existing before the click is not a fail.
- The child's module list does not contain `SKCOM.dll`,
  `CTSecuritiesATL.dll`, or any other DLL loaded from
  `C:\tai-test-194\app\_internal\libs`.

`combase.dll`, `ole32.dll`, and `oleaut32.dll` are Windows libraries.
Record whether they are loaded. Their presence is not a fail by itself.
A fail is a Capital / SKCOM DLL, or a gen-cache file written while the
child is alive.

## (c) No second window

The spec sets `console=True`, so the test workbench has its Tk window and
a console. The child must not add another window. Live bots may already
have windows. Those do not count, and you do not close them.

While the child is alive, restrict the window query to the test PIDs:

```powershell
$testIds = @(
  Get-CimInstance Win32_Process -Filter "Name = 'tai_backtest.exe'" |
    Where-Object { $_.ExecutablePath -like 'C:\tai-test-194\*' } |
    Select-Object -ExpandProperty ProcessId
)
Get-Process -Id $testIds |
  Select-Object Id, MainWindowHandle, MainWindowTitle |
  Format-List
```

Pass: among those PIDs, one window is the test workbench. The child PID
has `MainWindowHandle` 0 and an empty `MainWindowTitle`. No second Tk
window and no extra console for a `C:\tai-test-194\` PID.

Fail: a second window or a second console whose process path is under
`C:\tai-test-194\`, or a non-zero `MainWindowHandle` on the
`--multiprocessing-fork` PID.

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
  `_evolution_cancel`. On the test window the label stays
  **部署機器人 Deploy Bot** until a deploy is in progress. Clicking
  Deploy does not cancel a backtest-only sweep. Do not click Stop on any
  window whose EXE is outside `C:\tai-test-194\`.
- The STOP file is `data\live\<symbol>_<bot_name>\STOP` next to the EXE
  that owns the live runner (`bot_dir`). The sweep polls it only when
  that runner exists and is not idle. A backtest-only **🧬 進化
  Evolution** click leaves `bot_dir` unset, so the file is ignored.
  `run_bot_cli.py stop` writes a `STOP` file for a deployed bot. Do not
  run it on oreopie.

Deploy does not stay up without a quote login. With no quote connection,
tick subscription logs `未連線 Not connected, cannot subscribe to ticks`
and calls `_stop_live` immediately. Keeping **停止機器人 Stop Bot** on
screen requires **登入 Login**. This checklist does not do that.

While the child from (a) is alive, confirm the negative result on the
test copy only. Do not log in. If Deploy opens a dialog on the test
window, click Cancel.

```powershell
# This file is under the test EXE only. A backtest-only sweep does not read it.
New-Item -ItemType Directory -Force C:\tai-test-194\app\data\live\TX00_gridcheck | Out-Null
Set-Content -Encoding utf8 C:\tai-test-194\app\data\live\TX00_gridcheck\STOP "checklist negative test`n"
Get-CimInstance Win32_Process -Filter "Name = 'tai_backtest.exe'" |
  Where-Object { $_.ExecutablePath -like 'C:\tai-test-194\*' } |
  Select-Object ProcessId, ExecutablePath, CommandLine
```

Pass for this isolated copy:

- After writing that STOP file, the `--multiprocessing-fork` process
  under `C:\tai-test-194\` is still running.
- Clicking **部署機器人 Deploy Bot** on the test window does not log in.
  Cancel any dialog. The test child is still running.
- Live PIDs outside `C:\tai-test-194\` are unchanged.
- Record (d) as **not exercised**. Do not mark STOP or GUI stop as passed.

A later run that is allowed to deploy would write the STOP file only
under that run's own `C:\tai-test-194\app\data\live\<symbol>_<bot_name>\`
or would click **停止機器人 Stop Bot** on the test window. Pass would be:
within a couple of seconds the test `--multiprocessing-fork` PID is gone,
and the test chat contains `已要求停止，網格子行程已終止` /
`Stop requested — the grid subprocess was terminated`. Do not do that
deploy on this pass.

### Timeout

Do not shorten 1200 seconds. If the test child is still alive 20 minutes
after the EVO 1/3 line, snapshot test processes again with the path
filter above.

Pass: the `--multiprocessing-fork` PID under `C:\tai-test-194\` is gone,
and the test chat contains `網格子行程逾時` /
`Grid subprocess timed out`.

If the sweep finishes before 20 minutes, the timeout path was not
observed. Record that. It is the expected result for a normal grid, and
there is no knob that forces the short case.

## Cleanup — test PIDs only

When the notes are done, close the test window. If a test process is
still running, stop it by PID. The `ExecutablePath` check is what keeps
this off the live bots. Do not remove the `Where-Object` line.

```powershell
$test = Get-CimInstance Win32_Process -Filter "Name = 'tai_backtest.exe'" |
  Where-Object { $_.ExecutablePath -like 'C:\tai-test-194\*' }
$test | Select-Object ProcessId, ExecutablePath, CommandLine | Format-List
foreach ($p in $test) {
  Stop-Process -Id $p.ProcessId -Force
}
```

Then list what remains. Every live PID from the opening snapshot must
still be present. Nothing under `C:\tai-test-194\` should remain.

```powershell
Get-CimInstance Win32_Process -Filter "Name = 'tai_backtest.exe'" |
  Select-Object ProcessId, ExecutablePath |
  Format-List
```

## Evidence to paste back

```
host: oreopie
test root: C:\tai-test-194\
commit built: 644eac97d83c0246fbecd9d9497ec6818b0c8c04
BUILD_INFO.json commit / dirty:
exe path (must be C:\tai-test-194\app\tai_backtest.exe):
settings broker fields empty (yes/no):
logged into broker (must be no):
live PIDs before (ExecutablePath NOT under C:\tai-test-194\):
live PIDs after (same numbers, still running):

(a) test parent PID / command line:
(a) test child PID / command line (must contain --multiprocessing-fork):
(a) child ExecutablePath under C:\tai-test-194\ (yes/no):
(a) chat line at snapshot:

(b) cache listing BEFORE Evolution (test tree + LOCALAPPDATA read only):
(b) cache listing WHILE child alive (no new paths, no newer timestamps):
(b) tasklist /m (or listdlls) for the test child PID only:
(b) SKCOM / CTSecuritiesATL / _internal\libs DLL in that child (must be no):
(b) combase.dll / ole32.dll present (record only):

(c) MainWindowTitle for test PIDs only:
(c) second test window or extra test console (must be no):

(d) STOP file written at C:\tai-test-194\app\data\live\TX00_gridcheck\STOP:
(d) test child still running after that file (expected yes on this copy):
(d) Deploy on the test window clicked and cancelled without login (yes/no):
(d) STOP / GUI stop result: not exercised | passed on a deployed bot_dir
(d) timeout: not observed (sweep finished before 1200s) | test child gone with 網格子行程逾時
    knob used: none (no settings key, no environment variable)

cleanup: Stop-Process -Id used only for ExecutablePath under C:\tai-test-194\ (yes/no)
taskkill /IM or Stop-Process -Name used (must be no):

screenshots: test process list with ExecutablePath, chat EVO 1/3 line,
tasklist /m for the test child PID, window list for the test PIDs
```
