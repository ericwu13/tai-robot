"""Remote trading hosts (read-only) — check another PC's bots and bridge.

    C:/Users/eric8/.venvs/tai-monitor/Scripts/python.exe -s scripts/monitor/remote.py
        [--host NAME] [--check all|bots|regime|bridge|logs|deploy|tasks] [--hosts PATH]

Since 2026-10-24 the news bridge (W1-W4, Windows scheduled tasks) and the
08-04-rss regime bot run on a second trading PC.  Rather than installing
a second copy of every check there, ONE ssh call per host returns a
snapshot of exactly the files the checks read (tails for logs).  The
snapshot is written to a local mirror ``data/monitor/remote/<host>/``
with the REMOTE mtimes preserved, and the unchanged checks run against
the mirror.  PID liveness comes from the remote ``Get-Process`` probe in
the same call — a local ``pid_alive`` on a remote PID is meaningless.

Configuration lives ONLY in the gitignored tailscale hosts file (the same
one the ``tailscale-ssh`` skill uses).  A host is monitored when it has a
``monitor:`` block::

    hosts:
      steven:
        tailscale_ip: ...            # never in source
        user: ...
        identity_file: ~/.ssh/id_ed25519_<host>
        monitor:
          bridge_dir: 'C:\\Users\\<user>\\n8n-bridge'    # this host runs W1-W4
          live_dir: 'C:\\Users\\<user>\\tai-robot-src\\data\\live'
          bots: ['TMF00_08-04-rss-機器人']
          task_path: '\\TaiRobot\\'

A bot listed under ``bots`` is EXPECTED to run there, so the local
``data/live`` copy of the same name is excluded from the local checks.

Read-only contract (see ``common``): the remote script only reads; the
only thing written is this module's own mirror directory.  The remote
host's settings.yaml is never fetched (it holds credentials).
"""

from __future__ import annotations

import argparse
import base64
import binascii
import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from datetime import datetime  # noqa: E402

from scripts.monitor.common import (  # noqa: E402
    TZ_TPE, Finding, REPO_ROOT, guard_stdout, now_tpe, print_report, tail_text,
)

HOSTS_FILES = (
    os.path.join(os.path.expanduser("~"), ".ssh", "tailscale-hosts.local.yaml"),
    os.path.join(REPO_ROOT, ".claude", "skills", "tailscale-ssh", "hosts.local.yaml"),
)
MIRROR_ROOT = os.path.join(REPO_ROOT, "data", "monitor", "remote")

# Run by the remote sshd's default shell (cmd.exe); the script arrives on
# stdin, which sidesteps cmd's 8191-char command-line limit.
REMOTE_COMMAND = ('powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass '
                  '-Command "iex ([Console]::In.ReadToEnd())"')
CONNECT_TIMEOUT_S = 10
FETCH_TIMEOUT_S = 180
CLOCK_SKEW_MAX_S = 300

BRIDGE_SCOPE = "@bridge"
BRIDGE_FILES = (
    "signal.json", "events.json", "regime_vote*.json", "rss_scorer_state.json",
    ".chips_state.json", "monitor_state.json", "monitor.log",
    "chips_monitor.log", "task_*.last.log",
)
BOT_FILES = (
    ".lock", "session.json", "regime_state.json", "regime_history.csv",
    "heartbeat.json", "decisions.csv", "news_ledger.json",
)
# Mirrors check_bridge / check_logs: they read at most this much.
BRIDGE_LOG_TAIL = 500_000
BOT_LOG_TAIL = 2_000_000
BOT_LOG_COUNT = 2
_DEBUG_LOG = re.compile(r"^debug_\d{8}\.log$")

# Task Scheduler result codes that are not failures.
_TASK_OK = {
    0: "ok",
    0x41301: "running",
    0x41303: "has not run yet",
    0x41325: "queued",
}
_NEVER_RUN_BEFORE = 946684800  # 2000-01-01; Task Scheduler's "never" is 1999-11-30


@dataclass(frozen=True)
class RemoteHost:
    name: str
    address: str
    user: str
    identity_file: str = ""
    bridge_dir: str = ""
    live_dir: str = ""
    bots: tuple = ()
    task_path: str = ""

    @property
    def runs_bridge(self) -> bool:
        return bool(self.bridge_dir)


def load_hosts(path: str | None = None) -> list:
    """Monitored hosts from the gitignored hosts file. ``[]`` when none.

    Never raises — a monitoring run without the file simply has no
    remote hosts.  Hosts without a ``monitor:`` block are ignored.
    """
    for cand in ([path] if path else HOSTS_FILES):
        if not cand or not os.path.isfile(cand):
            continue
        try:
            import yaml
            with open(cand, encoding="utf-8-sig") as f:
                data = yaml.safe_load(f) or {}
        except Exception:  # noqa: BLE001
            continue
        out = []
        for name, entry in sorted((data.get("hosts") or {}).items()):
            if not isinstance(entry, dict) or not isinstance(entry.get("monitor"), dict):
                continue
            mon = entry["monitor"]
            if not re.fullmatch(r"[A-Za-z0-9_-]+", str(name)):
                continue
            out.append(RemoteHost(
                name=str(name),
                address=str(entry.get("tailscale_ip") or entry.get("address") or ""),
                user=str(entry.get("user") or ""),
                identity_file=str(entry.get("identity_file") or ""),
                bridge_dir=str(mon.get("bridge_dir") or ""),
                live_dir=str(mon.get("live_dir") or ""),
                bots=tuple(str(b) for b in (mon.get("bots") or [])),
                task_path=str(mon.get("task_path") or ""),
            ))
        return out
    return []


def relocated_bots(hosts) -> set:
    """Bot names that run on some remote host (skip their local copies)."""
    return {bot for h in hosts for bot in h.bots}


# ── fetch script ────────────────────────────────────────────────────────

# Wire format, one record per line, every free-text field base64(UTF-8):
#   NOW  <epoch>
#   DIR  <scope> <1|0>                     scope = "@bridge" or a bot name
#   FILE <scope> <name> <mtime-epoch> <content>
#   PID  <scope> <pid> <1|0>
#   TASK <json>
#   ERR  <scope> <message>
_PS_TEMPLATE = r"""
$ErrorActionPreference = 'SilentlyContinue'
$inv = [Globalization.CultureInfo]::InvariantCulture
$p = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('@@PARAMS@@')) | ConvertFrom-Json
function B64([string]$s) { [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($s)) }
function Emit([string]$line) { [Console]::Out.WriteLine($line) }
function Epoch($dt) { (([DateTimeOffset]$dt).ToUnixTimeMilliseconds() / 1000.0).ToString($inv) }
function Grab([string]$scope, $fi, [long]$tail) {
  try {
    $fs = [IO.File]::Open($fi.FullName, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]'ReadWrite,Delete')
    try {
      $n = $fs.Length
      if ($tail -gt 0 -and $n -gt $tail) { [void]$fs.Seek($n - $tail, [IO.SeekOrigin]::Begin); $n = $tail }
      $buf = New-Object byte[] $n
      $got = 0
      while ($got -lt $n) { $r = $fs.Read($buf, $got, $n - $got); if ($r -le 0) { break }; $got += $r }
    } finally { $fs.Close() }
    Emit ('FILE {0} {1} {2} {3}' -f (B64 $scope), (B64 $fi.Name), (Epoch $fi.LastWriteTimeUtc), [Convert]::ToBase64String($buf, 0, $got))
  } catch { Emit ('ERR {0} {1}' -f (B64 $scope), (B64 ($fi.Name + ': ' + $_.Exception.Message))) }
}
function Pick([string]$dir, $patterns) {
  Get-ChildItem -LiteralPath $dir -Force -File | Where-Object { $n = $_.Name; @($patterns | Where-Object { $n -like $_ }).Count -gt 0 }
}
Emit ('NOW {0}' -f (Epoch ([DateTime]::UtcNow)))
if ($p.bridge_dir) {
  if (Test-Path -LiteralPath $p.bridge_dir -PathType Container) {
    Emit ('DIR {0} 1' -f (B64 '@bridge'))
    foreach ($fi in @(Pick $p.bridge_dir $p.bridge_files)) {
      $tail = 0; if ($fi.Name -like '*.log') { $tail = [long]$p.bridge_log_tail }
      Grab '@bridge' $fi $tail
    }
  } else { Emit ('DIR {0} 0' -f (B64 '@bridge')) }
}
foreach ($bot in @($p.bots)) {
  $dir = Join-Path $p.live_dir $bot
  if (-not (Test-Path -LiteralPath $dir -PathType Container)) { Emit ('DIR {0} 0' -f (B64 $bot)); continue }
  Emit ('DIR {0} 1' -f (B64 $bot))
  foreach ($fi in @(Pick $dir $p.bot_files)) { Grab $bot $fi 0 }
  $logs = @(Get-ChildItem -LiteralPath $dir -Force -File -Filter 'debug_*.log' | Where-Object { $_.Name -match '^debug_\d{8}\.log$' } | Sort-Object Name -Descending | Select-Object -First ([int]$p.log_count))
  foreach ($fi in $logs) { Grab $bot $fi ([long]$p.log_tail) }
  $lock = Join-Path $dir '.lock'
  if (Test-Path -LiteralPath $lock -PathType Leaf) {
    $lp = 0
    if ([int]::TryParse(("" + (Get-Content -LiteralPath $lock -Raw)).Trim(), [ref]$lp)) {
      $alive = 0; if (Get-Process -Id $lp -ErrorAction SilentlyContinue) { $alive = 1 }
      Emit ('PID {0} {1} {2}' -f (B64 $bot), $lp, $alive)
    }
  }
}
if ($p.task_path) {
  try {
    foreach ($t in @(Get-ScheduledTask -TaskPath $p.task_path -ErrorAction Stop)) {
      $i = $t | Get-ScheduledTaskInfo
      $last = 0; if ($i.LastRunTime) { $last = [double](Epoch $i.LastRunTime) }
      $next = 0; if ($i.NextRunTime) { $next = [double](Epoch $i.NextRunTime) }
      $o = [ordered]@{ name = $t.TaskName; state = ('' + $t.State); last_result = [long]$i.LastTaskResult; last_run = $last; next_run = $next }
      Emit ('TASK {0}' -f (B64 (ConvertTo-Json $o -Compress)))
    }
  } catch { Emit ('ERR {0} {1}' -f (B64 '@tasks'), (B64 $_.Exception.Message)) }
}
"""


def build_fetch_script(host: RemoteHost) -> str:
    """The PowerShell snapshot script for *host* — pure ASCII.

    Paths and bot names (CJK) travel base64-encoded so stdin/cmd code
    pages cannot mangle them.  Address/user are NOT in the script.
    """
    params = {
        "bridge_dir": host.bridge_dir,
        "bridge_files": list(BRIDGE_FILES),
        "bridge_log_tail": BRIDGE_LOG_TAIL,
        "live_dir": host.live_dir,
        "bots": list(host.bots),
        "bot_files": list(BOT_FILES),
        "log_count": BOT_LOG_COUNT,
        "log_tail": BOT_LOG_TAIL,
        "task_path": host.task_path,
    }
    blob = base64.b64encode(json.dumps(params).encode("utf-8")).decode("ascii")
    return _PS_TEMPLATE.replace("@@PARAMS@@", blob)


def _ssh_binary() -> str:
    """Windows OpenSSH when present — git-bash's ssh (first on PATH in an
    agent shell) rejects ``-F nul``, and Windows OpenSSH accepts it."""
    native = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                          "System32", "OpenSSH", "ssh.exe")
    return native if os.name == "nt" and os.path.isfile(native) else "ssh"


def ssh_command(host: RemoteHost) -> list:
    # -F devnull: the owner's ~/.ssh/config carries a UTF-8 BOM that
    # git-bash's OpenSSH refuses to parse; this call never needs it.
    cmd = [_ssh_binary(), "-F", os.devnull]
    if host.identity_file:
        cmd += ["-i", os.path.expanduser(host.identity_file)]
    cmd += ["-o", "BatchMode=yes", "-o", f"ConnectTimeout={CONNECT_TIMEOUT_S}",
            f"{host.user}@{host.address}" if host.user else host.address,
            REMOTE_COMMAND]
    return cmd


# ── snapshot / mirror ───────────────────────────────────────────────────

@dataclass
class RemoteSnapshot:
    host: RemoteHost
    root: str = ""
    error: str = ""
    remote_now: float | None = None
    pids: dict = field(default_factory=dict)
    tasks: list = field(default_factory=list)
    missing: list = field(default_factory=list)   # scopes whose dir is absent
    errors: list = field(default_factory=list)    # per-file read errors

    @property
    def bridge_dir(self) -> str:
        return os.path.join(self.root, "bridge") if self.root else ""

    @property
    def live_dir(self) -> str:
        return os.path.join(self.root, "live") if self.root else ""

    def pid_alive(self, pid) -> bool:
        """Remote liveness of *pid*. Unprobed = ALIVE (cannot verify ≠ dead)."""
        try:
            return self.pids.get(int(pid), True)
        except (TypeError, ValueError):
            return False

    def bridge_settings(self) -> dict:
        """settings-shaped news paths pointing at the mirrored bridge files."""
        d = self.bridge_dir
        return {"news": {
            "signal_path": os.path.join(d, "signal.json"),
            "regime_vote_path": os.path.join(d, "regime_vote.json"),
            "events_path": os.path.join(d, "events.json"),
            "rss_state_file": os.path.join(d, "rss_scorer_state.json"),
        }}


def _unb64(field_: str) -> str:
    return base64.b64decode(field_).decode("utf-8", "replace")


def _safe_name(name: str, patterns) -> bool:
    if not name or name in (".", "..") or "/" in name or "\\" in name or ":" in name:
        return False
    return any(fnmatch.fnmatchcase(name, pat) for pat in patterns)


def _reset_mirror(root: str) -> None:
    """Empty this host's mirror. Only ever our own cache directory."""
    if os.path.isdir(root):
        shutil.rmtree(root)
    os.makedirs(root, exist_ok=True)


def materialize(host: RemoteHost, wire: str, mirror_root: str) -> RemoteSnapshot:
    """Parse the fetch output and write it under ``mirror_root/<host>``."""
    root = os.path.join(mirror_root, host.name)
    snap = RemoteSnapshot(host=host, root=root)
    _reset_mirror(root)
    os.makedirs(snap.bridge_dir, exist_ok=True)
    os.makedirs(snap.live_dir, exist_ok=True)
    bots = set(host.bots)
    seen_now = False
    for line in (wire or "").splitlines():
        parts = line.split()
        if not parts:
            continue
        kind = parts[0]
        try:
            if kind == "NOW" and len(parts) == 2:
                snap.remote_now = float(parts[1])
                seen_now = True
            elif kind == "DIR" and len(parts) == 3:
                scope = _unb64(parts[1])
                if parts[2] == "0":
                    snap.missing.append(scope)
                elif scope in bots:
                    os.makedirs(os.path.join(snap.live_dir, scope), exist_ok=True)
            elif kind == "FILE" and len(parts) in (4, 5):
                scope, name = _unb64(parts[1]), _unb64(parts[2])
                content = base64.b64decode(parts[4]) if len(parts) == 5 else b""
                if scope == BRIDGE_SCOPE and _safe_name(name, BRIDGE_FILES):
                    dest_dir = snap.bridge_dir
                elif scope in bots and (_safe_name(name, BOT_FILES)
                                        or _DEBUG_LOG.match(name)):
                    dest_dir = os.path.join(snap.live_dir, scope)
                else:
                    continue
                os.makedirs(dest_dir, exist_ok=True)
                dest = os.path.join(dest_dir, name)
                with open(dest, "wb") as f:
                    f.write(content)
                mtime = float(parts[3])
                os.utime(dest, (mtime, mtime))
            elif kind == "PID" and len(parts) == 4:
                if _unb64(parts[1]) in bots:
                    snap.pids[int(parts[2])] = parts[3] == "1"
            elif kind == "TASK" and len(parts) == 2:
                task = json.loads(_unb64(parts[1]))
                if isinstance(task, dict):
                    snap.tasks.append(task)
            elif kind == "ERR" and len(parts) == 3:
                snap.errors.append(f"{_unb64(parts[1])}: {_unb64(parts[2])}")
        except (ValueError, binascii.Error, OSError) as exc:
            snap.errors.append(f"unparsable {kind} record ({type(exc).__name__})")
    if not seen_now:
        snap.error = "no snapshot returned (remote script did not run)"
    return snap


def fetch_snapshot(host: RemoteHost, mirror_root: str = MIRROR_ROOT,
                   runner=subprocess.run) -> RemoteSnapshot:
    """One ssh round trip → mirrored snapshot. Never raises."""
    if not host.address:
        return RemoteSnapshot(host=host, error="no address in the hosts file")
    try:
        proc = runner(ssh_command(host), input=build_fetch_script(host).encode("ascii"),
                      capture_output=True, timeout=FETCH_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001
        return RemoteSnapshot(host=host, error=f"ssh failed: {type(exc).__name__}: {exc}")
    out = (proc.stdout or b"").decode("ascii", "replace")
    if proc.returncode != 0 and "NOW " not in out:
        err = (proc.stderr or b"").decode("utf-8", "replace").strip().splitlines()
        return RemoteSnapshot(host=host, error=(
            f"ssh exit {proc.returncode}: {err[-1] if err else 'no output'}"))
    try:
        return materialize(host, out, mirror_root)
    except OSError as exc:
        return RemoteSnapshot(host=host, error=f"mirror write failed: {exc}")


# ── checks that only exist remotely ─────────────────────────────────────

def _fmt_epoch(value) -> str:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return "-"
    if value < _NEVER_RUN_BEFORE:
        return "never"
    return datetime.fromtimestamp(value, TZ_TPE).strftime("%Y-%m-%d %H:%M")


def _task_log(snap: RemoteSnapshot, task_name: str) -> str:
    """``task_<W>.last.log`` for a ``Bridge-<W>`` task, if mirrored."""
    for cand in (task_name, task_name.split("-", 1)[-1]):
        path = os.path.join(snap.bridge_dir, f"task_{cand}.last.log")
        if os.path.isfile(path):
            return path
    return ""


def check_tasks(now: datetime, snap: RemoteSnapshot):
    """Scheduled tasks + host-level sanity (clock skew, unreadable files)."""
    findings, lines = [], []
    host = snap.host.name
    if snap.remote_now is not None:
        skew = snap.remote_now - now.timestamp()
        lines.append(f"remote clock: {skew:+.0f}s vs this PC")
        if abs(skew) > CLOCK_SKEW_MAX_S:
            findings.append(Finding(
                "P2", "host",
                f"{host} clock is {skew:+.0f}s off this PC — file ages and "
                f"heartbeat checks for it are unreliable", ""))
    for err in snap.errors:
        lines.append(f"read error: {err}")
        findings.append(Finding("P3", "host", f"{host}: {err}", ""))

    if not snap.host.task_path:
        lines.append("scheduled tasks: not configured")
        return findings, lines
    if not snap.tasks:
        lines.append(f"scheduled tasks under {snap.host.task_path}: NONE found")
        findings.append(Finding(
            "P2", "tasks",
            f"no scheduled tasks under {snap.host.task_path} on {host} — the "
            f"bridge has no scheduler", ""))
        return findings, lines

    for task in sorted(snap.tasks, key=lambda t: str(t.get("name"))):
        name = str(task.get("name") or "?")
        state = str(task.get("state") or "?")
        try:
            code = int(task.get("last_result") or 0)
        except (TypeError, ValueError):
            code = -1
        result = _TASK_OK.get(code, f"0x{code & 0xFFFFFFFF:X}")
        lines.append(f"  {name}: {state} | last {_fmt_epoch(task.get('last_run'))} "
                     f"({result}) | next {_fmt_epoch(task.get('next_run'))}")
        log = _task_log(snap, name)
        if log:
            tail = [ln for ln in tail_text(log, 4000).splitlines() if ln.strip()]
            if tail:
                lines.append(f"    last.log: {tail[-1].strip()[:160]}")
            if any("traceback" in ln.lower() for ln in tail):
                findings.append(Finding(
                    "P2", "tasks", f"{name}: last run printed a traceback "
                                   f"({os.path.basename(log)})", log))
        if state.lower() == "disabled":
            findings.append(Finding(
                "P2", "tasks", f"{name} is Disabled on {host} — it will never fire", ""))
        elif code not in _TASK_OK:
            findings.append(Finding(
                "P2", "tasks",
                f"{name} last run on {host} failed (result {result}, "
                f"{_fmt_epoch(task.get('last_run'))} TPE)", ""))
    return findings, lines


def _check_expected_bots(snap: RemoteSnapshot):
    """Configured bots must exist and hold a lock — they are EXPECTED to run.

    Locally a missing ``.lock`` is P3 (data/live is mostly retired bots);
    a bot named in the hosts file was put there to run.
    """
    findings, lines = [], []
    for bot in snap.host.bots:
        path = os.path.join(snap.live_dir, bot)
        if bot in snap.missing or not os.path.isdir(path):
            lines.append(f"{bot}: directory NOT FOUND under live_dir")
            findings.append(Finding(
                "P2", "bots",
                f"{bot}: directory not found on {snap.host.name} — moved again, "
                f"or live_dir in the hosts file is wrong", ""))
        elif not os.path.isfile(os.path.join(path, ".lock")):
            findings.append(Finding(
                "P2", "bots",
                f"{bot}: no .lock on {snap.host.name} — it is not running "
                f"(stopped, or not relaunched after a reboot)", path))
    return findings, lines


def _tag(result, host: str):
    findings, lines = result
    return [Finding(f.level, f"{f.area}@{host}", f.message, f.path)
            for f in findings], lines


def remote_sections(now: datetime, snap: RemoteSnapshot):
    """``[(title, fn)]`` for one host — every finding's area gets ``@host``."""
    from scripts.monitor.check_bots import check_bots
    from scripts.monitor.check_bridge import check_bridge
    from scripts.monitor.check_deploy import check_deploy
    from scripts.monitor.check_logs import check_logs
    from scripts.monitor.check_regime import check_regime

    host = snap.host.name
    if snap.error:
        what = ", ".join((["bridge W1-W4"] if snap.host.runs_bridge else [])
                         + list(snap.host.bots)) or "nothing configured"
        return [(f"HOST @{host}", lambda: ([Finding(
            "P1", f"host@{host}",
            f"{host} unreachable — {what} UNVERIFIED ({snap.error}); host down, "
            f"tailnet/ssh broken, or the key was not accepted", "")],
            [f"fetch failed: {snap.error}"]))]

    def bots():
        f1, l1 = _check_expected_bots(snap)
        f2, l2 = check_bots(now, snap.live_dir, pid_alive_fn=snap.pid_alive,
                            build_info=False)
        return f1 + f2, l1 + l2

    sections = [
        (f"BOTS @{host}", lambda: _tag(bots(), host)),
        (f"REGIME @{host}", lambda: _tag(check_regime(now, snap.live_dir), host)),
    ]
    if snap.host.runs_bridge:
        def bridge():
            if BRIDGE_SCOPE in snap.missing:
                return ([Finding("P1", "bridge",
                                 f"bridge_dir does not exist on {host} — W1-W4 "
                                 f"have nowhere to write", "")],
                        ["bridge_dir: NOT FOUND"])
            return check_bridge(now, snap.bridge_settings(), snap.live_dir)
        sections.append((f"BRIDGE @{host}", lambda: _tag(bridge(), host)))
    sections += [
        (f"LOGS @{host}", lambda: _tag(
            check_logs(now, snap.live_dir, "", scan_n8n=False), host)),
        (f"DEPLOY @{host}", lambda: _tag(check_deploy(None, snap.live_dir), host)),
        (f"TASKS @{host}", lambda: _tag(check_tasks(now, snap), host)),
    ]
    return sections


# ── CLI ─────────────────────────────────────────────────────────────────

def main() -> int:
    guard_stdout()
    ap = argparse.ArgumentParser(description="check remote trading hosts")
    ap.add_argument("--host", help="only this host (name from the hosts file)")
    ap.add_argument("--check", default="all",
                    choices=("all", "bots", "regime", "bridge", "logs", "deploy",
                             "tasks"))
    ap.add_argument("--hosts", help="path to the hosts file")
    args = ap.parse_args()

    hosts = [h for h in load_hosts(args.hosts) if not args.host or h.name == args.host]
    if not hosts:
        print("no monitored hosts (add a monitor: block to the tailscale hosts "
              "file — see .claude/skills/tailscale-ssh/hosts.local.example.yaml)")
        return 0
    now = now_tpe()
    for h in hosts:
        started = time.monotonic()
        snap = fetch_snapshot(h)
        print(f"# {h.name}: fetched in {time.monotonic() - started:.1f}s"
              + (f" — {snap.error}" if snap.error else f" → {snap.root}"))
        for title, fn in remote_sections(now, snap):
            if args.check != "all" and not title.lower().startswith(args.check) \
                    and not title.startswith("HOST"):
                continue
            findings, lines = fn()
            print_report(title, lines, findings)
            print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
