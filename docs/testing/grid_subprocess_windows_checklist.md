# Windows frozen-EXE checklist — grid sweep subprocess

Operator checklist for the spawn-child grid sweep (PR #194, code commit
`cf58c7c3f9836ac67ef57544fe0f167bccc3c7fb`).

Run this on the Windows test host **oreopie**, and only inside
`C:\tai-test-194\`. That folder is a fresh copy. It does not log into
the broker and it does not use a chat API key.

The binary under test is built from `cf58c7c3f9836ac67ef57544fe0f167bccc3c7fb`.
This checklist is a later docs commit on the same branch. Do not build
from a dirty tree, and do not publish a GitHub release.

Do not build `644eac97`. That commit reads the result pipe only after the
child exits. A pickled grid result larger than the Windows pipe buffer
(8 KB) blocks the child in `send`. Do not build `12eefef` for this gate
either: it drains the pipe, but it has no key-free smoke entry, so the
only way to start a sweep there is the Evolve button, which asks for a
chat API key. Do not copy a live key.

## Do not touch the live bots

oreopie also runs live bots. This checklist does not name their folders.
Treat every `tai_backtest.exe` whose path is **not** under
`C:\tai-test-194\` as a live bot.

- Do not open, edit, copy, or delete a live bot folder, its `data\`,
  its `settings.yaml`, or its `_comtypes_cache`.
- Do not click Stop on a live window. Do not run `run_bot_cli.py stop`.
- The only STOP file you may create is the path printed as
  `grid-smoke: stop_file=...`. That path contains `tai-grid-smoke-`.
  If the printed path does not contain `tai-grid-smoke-`, do not write
  it.
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
checklist. If a command would affect a live bot's folder or process,
do not run it.

## Isolation rules

- All build files live under `C:\tai-test-194\`.
  `C:\tai-test-194\src` is the git checkout.
  `C:\tai-test-194\app` is the folder you launch.
- Do not copy a live `settings.yaml` into this tree. The smoke command
  does not read settings and does not log into the broker.
- Do not open the workbench. Do not click **登入 Login** or
  **🧬 進化 Evolution**. Those paths are not this gate.
- The test EXE is `C:\tai-test-194\app\tai_backtest.exe`. Leave every
  other `tai_backtest.exe` running.

## Build (clean venv, pinned spec, commit cf58c7c)

No GitHub Actions workflow builds this EXE. Build it on oreopie, inside
the test folder. The spec is `tai_backtest.spec` (PyInstaller `--onedir`,
`console=True`, runtime hook `runtime_hook_comtypes.py`).

`python build_release.py` with no flags creates a GitHub release and can
send Discord. Use `--skip-release`.

Known build workarounds (separate build PR):

- If `src\strategies` is missing, create it empty before PyInstaller.
- Pin PyInstaller **6.19.0** and comtypes **1.4.15**.
- This branch's spec also lists a top-level `strategies` datas entry.
  Create that empty too if it is missing, or Analysis fails.

```powershell
New-Item -ItemType Directory -Force C:\tai-test-194 | Out-Null
cd C:\tai-test-194
git clone https://github.com/ericwu13/tai-robot.git src
cd C:\tai-test-194\src
git fetch origin
git checkout cf58c7c3f9836ac67ef57544fe0f167bccc3c7fb
git rev-parse HEAD
# expect cf58c7c3f9836ac67ef57544fe0f167bccc3c7fb

if (-not (Test-Path .\src\strategies)) {
  New-Item -ItemType Directory -Force .\src\strategies | Out-Null
}
if (-not (Test-Path .\strategies)) {
  New-Item -ItemType Directory -Force .\strategies | Out-Null
}

py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install pyyaml keyring httpx lightweight-charts pandas holidays comtypes==1.4.15 pyinstaller==6.19.0

Copy-Item settings.example.yaml settings.yaml
# Do not paste live credentials or a live chat API key into this file.
# The smoke command does not read it.

python build_release.py --skip-release
```

Pass: `git rev-parse HEAD` prints `cf58c7c3f9836ac67ef57544fe0f167bccc3c7fb`,
`python -m pip show pyinstaller` prints Version 6.19.0,
`python -m pip show comtypes` prints Version 1.4.15,
PyInstaller exits 0, and these exist:

- `C:\tai-test-194\src\dist\tai_backtest\tai_backtest.exe`
- `C:\tai-test-194\src\dist\tai_backtest\BUILD_INFO.json` with `"commit"`
  equal to that SHA and `"dirty": false`

The Capital API SDK directory is gitignored. If it is absent, the spec
bundles no `SKCOM` DLLs. That is fine for this checklist. Do not copy DLLs
or `settings.yaml` out of a live install.

Copy the built folder to the launch directory. Do not start the GUI.

```powershell
Copy-Item -Recurse C:\tai-test-194\src\dist\tai_backtest C:\tai-test-194\app
```

## What the smoke entry does

```text
C:\tai-test-194\app\tai_backtest.exe --grid-subprocess-smoke [--bars N] [--timeout-s S] [--hang] [--payload-bytes N]
```

This exits before Tk and COM. It does not read `settings.yaml`, does not
open a window, and does not use the network. It builds synthetic bars in
memory and calls `run_grid_isolated` (spawn).

- Without `--hang`, the child runs real `run_grid_on_bars` on those bars.
  The smoke grid is capped at 4 settings so it returns quickly. Pass is
  `grid-smoke: outcome=ok` and a process exit code of 0.
- With `--hang`, the child sleeps instead of running the grid. The parent
  still uses the real spawn pipe. Use this to see the child, then stop
  it. Pass for a STOP file is `outcome=stopped` and exit code 3. Pass for
  a short wait is `outcome=timeout` and exit code 2.
- `--timeout-s S` is the wait for this process only. It is not a
  `settings.yaml` key and it does not change the live bot's 20-minute
  grid timeout. The smoke default when the flag is omitted is 120 seconds.
- `--payload-bytes N` makes the child build and send an N-byte blob
  through the real spawn pipe. The blob is not in the pickled job.
  That result is not a `GridOutcome`, so the process exits **1**
  (`outcome=crash`, detail `not a grid outcome`). Exit 1 here means
  the pipe carried the blob. Exit 2 means the wait timed out, which
  is a failure for this gate. Do not combine it with `--hang`.

The process prints these lines, flushed as they happen:

```text
grid-smoke: mode=grid|hang
grid-smoke: stop_file=<temp>\tai-grid-smoke-<id>\STOP
grid-smoke: timeout_s=<S>
grid-smoke: note=timeout is this process only; not a settings key
grid-smoke: child_pid=<pid>
grid-smoke: elapsed_s=<seconds>
grid-smoke: result_bytes=<n>
grid-smoke: outcome=ok|stopped|timeout|crash
grid-smoke: detail=<one line>
```

`child_pid` is printed when the child starts, before the wait returns.
A grid-mode run may finish before you can snapshot it. Gates (a)–(e)
use `--hang`, which holds the child until STOP or `--timeout-s`.

Run the real grid once, with no key and no broker, before the hang gates:

```powershell
cd C:\tai-test-194\app
.\tai_backtest.exe --grid-subprocess-smoke --bars 120 --timeout-s 120
echo "exit=$LASTEXITCODE"
```

Pass: exit code 0, `mode=grid`, `outcome=ok`, `result_bytes` greater than
0, no Tk window, no login prompt, no API-key prompt. Fail if the EXE
opens the workbench or asks for a key.

## Process snapshot command

Use this whenever a step says "snapshot test processes". It cannot return
a live bot, because of the path filter.

```powershell
Get-CimInstance Win32_Process -Filter "Name = 'tai_backtest.exe'" |
  Where-Object { $_.ExecutablePath -like 'C:\tai-test-194\*' } |
  Select-Object ProcessId, ParentProcessId, CreationDate, ExecutablePath, CommandLine |
  Format-List
```

The parent is the process you launched from `C:\tai-test-194\app\`. Its
command line contains `--grid-subprocess-smoke` and does not contain
`--multiprocessing-fork`. The child is the process whose `CommandLine`
contains `--multiprocessing-fork`, whose `ExecutablePath` is still under
`C:\tai-test-194\`, and whose `ParentProcessId` is the parent's PID.

A `tai_backtest.exe` with any other `ExecutablePath` is a live bot. Do not
include it in the pass/fail notes except to record that its PID was left
alone.

## (a) Child command line contains --multiprocessing-fork

In one PowerShell window:

```powershell
cd C:\tai-test-194\app
.\tai_backtest.exe --grid-subprocess-smoke --hang --timeout-s 90
```

Wait until `grid-smoke: child_pid=` is printed. Leave that window open.
In a second PowerShell window, snapshot test processes.

Pass:

- A second `tai_backtest.exe` exists with `ExecutablePath` under
  `C:\tai-test-194\`.
- Its command line contains `--multiprocessing-fork`.
- Its parent PID is the smoke parent (the process whose command line
  contains `--grid-subprocess-smoke --hang`).
- The printed `child_pid` matches that process.
- No live PID from the opening list was stopped.
- No Tk window and no API-key prompt.

Fail:

- No second process under `C:\tai-test-194\`.
- The second process has no `--multiprocessing-fork` argument.
- Any process outside `C:\tai-test-194\` changed.

Leave this hang running for (b), (c), and (d).

## (b) Child writes no comtypes cache and loads no Capital COM DLL

The frozen hook skips `import comtypes` for `--multiprocessing-fork` and
for `--grid-subprocess-smoke`. The smoke parent should not create
`C:\tai-test-194\app\_comtypes_cache`. Stock comtypes locations
(`%LOCALAPPDATA%\comtypes_cache` and a `comtypes\gen` tree) are not the
cache this EXE uses. You may read them. Do not delete them, and do not
look in a live bot's `_comtypes_cache`.

Take a cache snapshot before the hang in (a), and again while the child
is alive.

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

Replace the child PID from the smoke output. Confirm the PID is the
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
  `LastWriteTime` newer than the pre-hang snapshot, and no new path
  appears.
- The child's module list does not contain `SKCOM.dll`,
  `CTSecuritiesATL.dll`, or any other DLL loaded from
  `C:\tai-test-194\app\_internal\libs`.

`combase.dll`, `ole32.dll`, and `oleaut32.dll` are Windows libraries.
Record whether they are loaded. Their presence is not a fail by itself.
A fail is a Capital / SKCOM DLL, or a gen-cache file written while the
child is alive.

## (c) No second window

`console=True` means the smoke command you started has a console. The
child must not add another window. Live bots may already have windows.
Those do not count, and you do not close them.

While the hang child is alive:

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

Pass: the only new window is the console for the command you typed.
The `--multiprocessing-fork` PID has `MainWindowHandle` 0 and an empty
`MainWindowTitle`. No Tk workbench and no extra console for that PID.

Fail: a Tk window, a second console whose process is the child, or a
non-zero `MainWindowHandle` on the `--multiprocessing-fork` PID.

## (d) Child is gone after the smoke STOP file

The hang from (a) is still running (`--timeout-s 90`). Copy the
`grid-smoke: stop_file=` path from that window. It must contain
`tai-grid-smoke-`. Write only that file:

```powershell
Set-Content -Encoding utf8 "<paste the printed stop_file path>" "stop`n"
```

Pass, within a couple of seconds:

- The `--multiprocessing-fork` PID is gone.
- The smoke window prints `grid-smoke: outcome=stopped` and exits with
  code 3.
- Live PIDs outside `C:\tai-test-194\` are unchanged.

Fail: the child is still alive after several seconds, the outcome is not
`stopped`, or any live PID changed.

Do not write `data\live\...\STOP` and do not run `run_bot_cli.py stop`.

## (e) Child is gone after a short smoke timeout

`--timeout-s` is smoke-only. There is still no settings key and no
environment variable that shortens the live bot's 20-minute grid timeout.
Do not edit `DEFAULT_GRID_TIMEOUT_S`.

Start a new hang with an 8-second wait. Do not write a STOP file.

```powershell
cd C:\tai-test-194\app
.\tai_backtest.exe --grid-subprocess-smoke --hang --timeout-s 8
echo "exit=$LASTEXITCODE"
```

While it runs you can snapshot the child once (same checks as (a) if you
want a second look). Then wait.

Pass:

- The process exits with code 2.
- The output contains `grid-smoke: outcome=timeout` and
  `grid-smoke: note=timeout is this process only; not a settings key`.
- `elapsed_s` is about 8 seconds, not 20 minutes.
- The `--multiprocessing-fork` PID is gone.
- `settings.yaml` was not required and was not a live copy.

Fail: the child is still alive after about 15 seconds, the outcome is
not `timeout`, or the run waited the live 20-minute bound.

## (f) Payload larger than the 8 KB Windows pipe

The parent must read the result while the child is still running. A
blob bigger than the Windows pipe buffer (8 KB) used to block the child
in `send`. This step uses the smoke flag. It does not need a chat API
key and it does not run the grid.

```powershell
cd C:\tai-test-194\app
.\tai_backtest.exe --grid-subprocess-smoke --payload-bytes 70000 --timeout-s 60
echo "exit=$LASTEXITCODE"
```

`70000` is over 8 KB. The child builds those bytes. They are not in the
pickled job.

Pass:

- The process returns within well under 60 seconds. It does not hang.
- Exit code is **1**, not 0 and not 2.
- Output contains `grid-smoke: mode=payload`, `payload_bytes=70000`,
  `outcome=crash`, and `not a grid outcome`.
- `result_bytes` is greater than 70000 (the printed size is the pickled
  blob, which is slightly larger than the raw bytes).
- The `--multiprocessing-fork` PID is gone afterwards.
- No Tk window, no broker login, no API-key prompt.

Fail: the process hits `outcome=timeout` or is still alive at 60 seconds,
`result_bytes` is under 70000, or the exit code is 0. Exit code 0 would
mean the smoke treated a non-grid payload as a grid pass.

## Cleanup — test PIDs only

When the notes are done, a smoke process should already have exited. If
one is still running, stop it by PID. The `ExecutablePath` check is what
keeps this off the live bots. Do not remove the `Where-Object` line.

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
commit built: cf58c7c3f9836ac67ef57544fe0f167bccc3c7fb
BUILD_INFO.json commit / dirty:
pyinstaller version (must be 6.19.0):
comtypes version (must be 1.4.15):
src\strategies created empty if it was missing (yes/no):
exe path (must be C:\tai-test-194\app\tai_backtest.exe):
live settings or live API key copied in (must be no):
logged into broker (must be no):
GUI / Evolve opened (must be no):
live PIDs before (ExecutablePath NOT under C:\tai-test-194\):
live PIDs after (same numbers, still running):

grid mode (no --hang): exit code (must be 0):
grid mode: outcome / result_bytes / detail:

(a) smoke parent PID / command line (must contain --grid-subprocess-smoke --hang):
(a) child PID / command line (must contain --multiprocessing-fork):
(a) child ExecutablePath under C:\tai-test-194\ (yes/no):
(a) printed child_pid matches (yes/no):

(b) cache listing BEFORE hang:
(b) cache listing WHILE child alive (no new paths, no newer timestamps):
(b) tasklist /m (or listdlls) for the child PID only:
(b) SKCOM / CTSecuritiesATL / _internal\libs DLL in that child (must be no):
(b) combase.dll / ole32.dll present (record only):

(c) MainWindowTitle for test PIDs only:
(c) Tk window or extra child console (must be no):

(d) stop_file path printed (must contain tai-grid-smoke-):
(d) outcome=stopped and exit code 3 (yes/no):
(d) child PID gone after that file (yes/no):

(e) command: --grid-subprocess-smoke --hang --timeout-s 8
(e) outcome=timeout and exit code 2 (yes/no):
(e) elapsed_s:
(e) child gone (yes/no):
(e) settings key or env var used to shorten the live timeout (must be no):

(f) command: --grid-subprocess-smoke --payload-bytes 70000 --timeout-s 60
(f) exit code (must be 1, not 0 or 2):
(f) outcome=crash and "not a grid outcome" (yes/no):
(f) result_bytes (must be > 70000):
(f) returned without hanging (yes/no):
(f) child gone (yes/no):

cleanup: Stop-Process -Id used only for ExecutablePath under C:\tai-test-194\ (yes/no)
taskkill /IM or Stop-Process -Name used (must be no):

screenshots: smoke console (child_pid, outcome), process list with
ExecutablePath and CommandLine, tasklist /m for the child PID
```
