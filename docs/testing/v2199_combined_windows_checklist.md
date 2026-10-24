# Windows checklist — v2.19.9 combined EXE (#177 on #195 on #194)

One run on the Windows host **oreopie**, and only inside `C:\tai-test-177\`.
Build and launch the head of `cursor/headless-exe-option-a-f28f` (draft
PR #177). That tree already contains #194 and #195. Do not build an
older SHA, and do not publish a GitHub release.

The binary is `C:\tai-test-177\app\tai_backtest.exe`, built with
`.venv-build`, not the system Python.

## Do not touch the live bots

oreopie also runs live bots. This checklist does not name their folders.
Treat every `tai_backtest.exe` whose path is **not** under
`C:\tai-test-177\` as a live bot.

- Do not open, edit, copy, or delete a live bot folder, its `data\`,
  its `settings.yaml`, or its `_comtypes_cache`.
- Do not copy a live `settings.yaml`, a live API key, or a chat key
  into `C:\tai-test-177\`.
- Do not click Stop on a live window. Do not stop a live bot from this EXE.
- Do not run `taskkill /IM tai_backtest.exe`. Do not run
  `Stop-Process -Name tai_backtest`. Either command kills every bot on
  the machine, including the live ones.
- Kill a process only after `ExecutablePath` starts with
  `C:\tai-test-177\` and you pass that PID to `Stop-Process -Id`.
  The cleanup block at the bottom is the only kill step.
- `%LOCALAPPDATA%\comtypes_cache` is shared. You may read it. Do not
  delete it.

Before you start, record the live PIDs. The same numbers must still be
there at the end:

```powershell
Get-CimInstance Win32_Process -Filter "Name = 'tai_backtest.exe'" |
  Where-Object { $_.ExecutablePath -notlike 'C:\tai-test-177\*' } |
  Select-Object ProcessId, ExecutablePath |
  Format-List
```

## Build (`.venv-build`, not the system Python)

No GitHub Actions workflow builds this EXE. `build_release.py` with no
flags creates a GitHub release and can send Discord. Use `--skip-release`.

Do not run `Activate.ps1`. A bare `python` is the system interpreter
when activation did not run. Every `pip` command and `build_release.py`
below uses `.\.venv-build\Scripts\python.exe`.

```powershell
New-Item -ItemType Directory -Force C:\tai-test-177 | Out-Null
cd C:\tai-test-177
git clone https://github.com/ericwu13/tai-robot.git src
cd C:\tai-test-177\src
git fetch origin
git checkout cursor/headless-exe-option-a-f28f
git rev-parse HEAD
git status --porcelain
```

Pass: `git status --porcelain` is empty. Record the SHA. That SHA is the
commit in `BUILD_INFO.json`.

```powershell
py -3.13 -m venv .venv-build
$py = ".\.venv-build\Scripts\python.exe"
& $py -m pip install --upgrade pip
& $py -m pip install -e .
& $py -m pip install -r requirements-build.txt
```

`pip install -e .` installs the project (pyyaml, keyring, httpx,
lightweight-charts, pandas, holidays). `requirements-build.txt` pins
PyInstaller **6.19.0** and comtypes **1.4.15**. Do not use a newer pair.

```powershell
Copy-Item settings.example.yaml settings.yaml
# Do not paste live credentials or a live chat API key into this file.

& $py build_release.py --skip-release
```

Pass:

- `.\.venv-build\Scripts\python.exe -m pip show pyinstaller` prints
  Version 6.19.0.
- `.\.venv-build\Scripts\python.exe -m pip show comtypes` prints
  Version 1.4.15.
- PyInstaller exits 0.
- `C:\tai-test-177\src\dist\tai_backtest\tai_backtest.exe` exists.
- `BUILD_INFO.json` `"commit"` equals `git rev-parse HEAD` and
  `"dirty"` is false.

The Capital API SDK directory is gitignored. If it is absent, the spec
bundles no `SKCOM` DLLs. Do not copy DLLs out of a live install.

```powershell
Copy-Item -Recurse C:\tai-test-177\src\dist\tai_backtest C:\tai-test-177\app
```

## Paper deploy — needs Eric's go-ahead

`tai_backtest.exe deploy` of a paper bot logs into the broker. Do not
run this block until Eric says the test account is allowed. Do not copy
a live `settings.yaml` or a live API key to make it work. If that
go-ahead is not in hand, skip this section and the status/stop lines
that name `cli177`, and continue at "list".

Dedicated bot name: `cli177`. Mode: `paper` only. Do not pass
`semi_auto`, `full_auto`, or any real-money flag. Do not reuse a live
bot's name.

```powershell
cd C:\tai-test-177\app
.\tai_backtest.exe deploy --symbol TMF00 --bot cli177 --strategy "H4 Bollinger" --mode paper --detach --wait-ready 240
echo "exit=$LASTEXITCODE"
```

Pass, only after Eric's go-ahead: exit code 0, and the detached child
`ExecutablePath` is under `C:\tai-test-177\`. Fail if a window asks for
a live password you were not told to use, or if any process outside
`C:\tai-test-177\` changes.

`deploy --detach` must not open a console window. While `cli177` is
still running, record the child (the process under `C:\tai-test-177\`
whose command line contains `cli177` and does not contain `--detach`):

```powershell
$bot = Get-CimInstance Win32_Process -Filter "Name = 'tai_backtest.exe'" |
  Where-Object {
    $_.ExecutablePath -like 'C:\tai-test-177\*' -and
    $_.CommandLine -like '*cli177*' -and
    $_.CommandLine -notlike '*--detach*'
  }
$bot | Select-Object ProcessId, ExecutablePath, CommandLine | Format-List
Get-Process -Id $bot.ProcessId | Select-Object Id, MainWindowHandle, MainWindowTitle
Get-CimInstance Win32_Process -Filter "Name = 'conhost.exe'" |
  Where-Object { $_.ParentProcessId -eq $bot.ProcessId } |
  Select-Object ProcessId, ParentProcessId, CommandLine
```

Pass: `MainWindowHandle` is **0**, `MainWindowTitle` is empty, and no
`conhost.exe` has that bot as its parent. Fail if the handle is
non-zero or a new conhost is attached to that PID. Write both into the
evidence block. Then continue with status, list, and stop.

Then, still only with that go-ahead:

```powershell
.\tai_backtest.exe status --symbol TMF00 --bot cli177
.\tai_backtest.exe list
.\tai_backtest.exe stop --symbol TMF00 --bot cli177 --wait 60
echo "exit=$LASTEXITCODE"
```

Pass: `status` and `list` show `cli177`, `stop` exits 0, and the
detached PID under `C:\tai-test-177\` is gone. `list` on its own does
not log in. You may run `list` with no bot deployed; an empty table is
fine when the deploy block was skipped.

## No args still opens the GUI

```powershell
cd C:\tai-test-177\app
Start-Process .\tai_backtest.exe
```

Pass: the workbench window opens. Close that window yourself. Do not
click Login. Fail if the process exits before a window, or if a second
`tai_backtest.exe` appears outside `C:\tai-test-177\`.

Record the GUI PID from the snapshot command below, then close it, and
confirm that PID is gone before the smoke gates.

## #194 gates (a) through (f)

Same pass rules as `docs/testing/grid_subprocess_windows_checklist.md`,
with this folder and payload size 131072 on gate (f). The smoke flag
exits before Tk and COM. It does not read `settings.yaml` and does not
use the network. Gate (f) is spelled out below: exit 1 is a pass only
together with the non-grid text, the payload size, and a prompt return.

Snapshot command (test processes only):

```powershell
Get-CimInstance Win32_Process -Filter "Name = 'tai_backtest.exe'" |
  Where-Object { $_.ExecutablePath -like 'C:\tai-test-177\*' } |
  Select-Object ProcessId, ParentProcessId, CreationDate, ExecutablePath, CommandLine |
  Format-List
```

The parent command line contains `--grid-subprocess-smoke` and does not
contain `--multiprocessing-fork`. The child command line contains
`--multiprocessing-fork`, its `ExecutablePath` is under
`C:\tai-test-177\`, and its `ParentProcessId` is the parent's PID.

Real grid once, before the hang gates:

```powershell
cd C:\tai-test-177\app
.\tai_backtest.exe --grid-subprocess-smoke --bars 120 --timeout-s 120
echo "exit=$LASTEXITCODE"
```

Pass: exit code 0, `mode=grid`, `outcome=ok`, `result_bytes` greater
than 0, no Tk window, no login prompt.

### (a) Child command line contains --multiprocessing-fork

```powershell
.\tai_backtest.exe --grid-subprocess-smoke --hang --timeout-s 90
```

While it runs, snapshot. Pass: a child PID whose command line contains
`--multiprocessing-fork`, `ExecutablePath` under `C:\tai-test-177\`,
and the printed `child_pid` matches. Leave the hang up for (b), (c),
and (d).

### (b) Child writes no comtypes cache and loads no Capital COM DLL

Snapshot `C:\tai-test-177\app\_comtypes_cache` before the hang and again
while the child is alive. Pass: no new paths and no newer timestamps
from the smoke. The child `tasklist /m` must not list `SKCOM` or
`CTSecuritiesATL`. Do not inspect a live bot's cache.

### (c) No Tk window on the child

The `--multiprocessing-fork` PID has `MainWindowHandle` 0 and an empty
`MainWindowTitle`. Fail on a non-zero handle or a second console.

### (d) STOP file stops the child

The printed `stop_file` path contains `tai-grid-smoke-`. If it does
not, do not write it. Create that file only.

Pass: `outcome=stopped`, exit code 3, and the `--multiprocessing-fork`
PID is gone.

### (e) Short wait is not the live 20-minute bound

```powershell
.\tai_backtest.exe --grid-subprocess-smoke --hang --timeout-s 8
echo "exit=$LASTEXITCODE"
```

Pass: exit code 2, `outcome=timeout`, `elapsed_s` about 8 seconds, child
PID gone. Do not edit `DEFAULT_GRID_TIMEOUT_S`. There is no settings key
for this.

### (f) Payload larger than the 8 KB Windows pipe

Use **131072** bytes (over the 8 KB pipe). The child builds the blob.
It is not in the pickled job. With `--payload-bytes` the child returns
a blob that is not a `GridOutcome`, so the smoke exits **1** with
`outcome=crash` and the text `not a grid outcome`. That is the design.
This checklist does not change those exit codes.

```powershell
.\tai_backtest.exe --grid-subprocess-smoke --payload-bytes 131072 --timeout-s 60
echo "exit=$LASTEXITCODE"
```

Pass, all of these:

- The process returns promptly and does not hang. Record `elapsed_s`
  from the smoke output. It must be below the `--timeout-s` used on
  this command (60).
- Exit code is **1**.
- Output contains `outcome=crash`.
- stdout or stderr contains `not a grid outcome`.
- `result_bytes` is greater than 131072.
- The `--multiprocessing-fork` PID is gone.
- No Tk window, no broker login, no API-key prompt.

An exit 1 without both the `not a grid outcome` text and
`result_bytes` > 131072 is a FAIL. That keeps this gate distinct from
a real crash, which also exits 1. Also fail on `outcome=timeout`, on
`elapsed_s` that is not below 60, if the process is still alive at the
timeout, or on exit code 0 or 2.

## Frozen hook imports src.paths

The frozen runtime hook calls `from src.paths import skips_gui_and_com`
before it installs `comtypes.gen`. This check is the no-args GUI launch
above. Pass only when all three are true:

- The EXE launches (the workbench window opened).
- `C:\tai-test-177\app\_comtypes_cache` is created.
- `comtypes.gen` imports. The window appears only after that import. A
  missing `src.paths` in the bundle raises in the hook first, so the
  EXE exits before the window and does not leave a usable cache.

```powershell
Test-Path C:\tai-test-177\app\_comtypes_cache
Test-Path C:\tai-test-177\app\_comtypes_cache\__init__.py
Get-Content C:\tai-test-177\app\_comtypes_cache\__init__.py
```

Fail if any of the three is missing. Record them in the evidence block.

## #195 — cache, comtypes.gen, strategies placeholder

After the no-args GUI has opened (the hook runs on that launch):

```powershell
Test-Path C:\tai-test-177\app\_comtypes_cache\__init__.py
Get-Content C:\tai-test-177\app\_comtypes_cache\__init__.py
Get-ChildItem C:\tai-test-177\app\_internal\strategies | Select-Object Name
```

Pass:

- The GUI launched (section above). A missing `comtypes.gen` aborts the
  EXE before the window, so a window plus this cache means the hook
  imported `comtypes.gen` and wrote the package.
- `_comtypes_cache\__init__.py` exists.
- `C:\tai-test-177\app\_internal\strategies` contains **only**
  `README.txt`. No private strategy file.

The grid-smoke parent and its `--multiprocessing-fork` child must not
be what created that cache. Gate (b) is the check that they did not
add files.

## Cleanup — test PIDs only

```powershell
$test = Get-CimInstance Win32_Process -Filter "Name = 'tai_backtest.exe'" |
  Where-Object { $_.ExecutablePath -like 'C:\tai-test-177\*' }
$test | Select-Object ProcessId, ExecutablePath, CommandLine | Format-List
foreach ($p in $test) {
  Stop-Process -Id $p.ProcessId -Force
}
Get-CimInstance Win32_Process -Filter "Name = 'tai_backtest.exe'" |
  Select-Object ProcessId, ExecutablePath |
  Format-List
```

Every live PID from the opening snapshot is still present. Nothing
under `C:\tai-test-177\` remains. `taskkill /IM` and
`Stop-Process -Name` were not used.

## Evidence to paste back

```
host: oreopie
test root: C:\tai-test-177\
branch: cursor/headless-exe-option-a-f28f
commit built (git rev-parse HEAD):
BUILD_INFO.json commit / dirty:
pyinstaller version (must be 6.19.0, from .venv-build\Scripts\python.exe):
comtypes version (must be 1.4.15, from .venv-build\Scripts\python.exe):
live settings or live API key copied in (must be no):
Eric go-ahead for paper deploy (yes/no):

deploy cli177 paper --detach --wait-ready (skip if no go-ahead): exit code:
deploy --detach: child MainWindowHandle (must be 0):
deploy --detach: conhost attached to that PID (must be no):
status cli177:
list:
stop cli177: exit code:

no-args GUI opened (must be yes) and closed before smoke (yes/no):
frozen hook imports src.paths: EXE launched (must be yes):
frozen hook imports src.paths: _comtypes_cache created (must be yes):
frozen hook imports src.paths: comtypes.gen imported (must be yes):
_comtypes_cache\__init__.py exists after GUI (must be yes):
_internal\strategies names (must be only README.txt):

grid mode: exit code (must be 0) / outcome / result_bytes:
(a) parent command line contains --grid-subprocess-smoke --hang:
(a) child command line contains --multiprocessing-fork:
(a) child ExecutablePath under C:\tai-test-177\ (yes/no):
(b) cache unchanged while child alive (yes/no):
(b) SKCOM / CTSecuritiesATL in the child (must be no):
(c) child MainWindowHandle 0 (yes/no):
(d) outcome=stopped and exit code 3 (yes/no):
(e) outcome=timeout, exit code 2, elapsed_s about 8:
(f) command: --grid-subprocess-smoke --payload-bytes 131072 --timeout-s 60
(f) exit code (must be 1):
(f) outcome=crash (must be yes):
(f) stdout/stderr contains "not a grid outcome" (must be yes):
(f) result_bytes (must be > 131072):
(f) elapsed_s (must be below --timeout-s 60, no hang):

cleanup: Stop-Process -Id only for ExecutablePath under C:\tai-test-177\ (yes/no)
taskkill /IM or Stop-Process -Name used (must be no):
live PIDs before:
live PIDs after (same numbers):
```
