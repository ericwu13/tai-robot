"""Shared primitives for the read-only monitoring helpers.

Everything here is a pure function with an injected ``now`` (and an
injected ``pid_alive_fn`` where liveness matters), so the checks can be
tested without a wall clock or a live process.

**Read-only contract**: nothing in ``scripts/monitor`` may delete, move,
or rewrite bot/bridge state.  In particular this module deliberately
re-implements PID liveness instead of calling
``LiveRunner.check_lock``, which DELETES stale locks as a side effect.

Time: everything semantic is Asia/Taipei.  The repo convention is the
fixed offset ``timezone(timedelta(hours=8))`` (``src.regime.switch_logic``
``_TZ_TAIPEI``) — not zoneinfo.  The OS clock on this box runs UTC-7, so a
naive ``datetime.now()`` must never be compared against a TPE log stamp.
"""

from __future__ import annotations

import glob
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

TZ_TPE = timezone(timedelta(hours=8))

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

LEVELS = ("P1", "P2", "P3")


def now_tpe() -> datetime:
    """Current Taipei time (tz-aware)."""
    return datetime.now(TZ_TPE)


def to_tpe(dt: datetime) -> datetime:
    """Normalise a datetime to tz-aware Taipei."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=TZ_TPE)
    return dt.astimezone(TZ_TPE)


@dataclass
class Finding:
    """One actionable observation.

    P1 = act now (bot/bridge is broken), P2 = degraded / investigate
    today, P3 = informational, no action implied.
    """

    level: str
    area: str
    message: str
    path: str = ""


def verdict(findings) -> str:
    """RED if any P1, YELLOW if any P2, else GREEN."""
    levels = {f.level for f in findings}
    if "P1" in levels:
        return "RED"
    if "P2" in levels:
        return "YELLOW"
    return "GREEN"


def fmt_findings(findings) -> str:
    """Pretty-print findings grouped by severity."""
    if not findings:
        return "findings: none"
    out = []
    for level in LEVELS:
        rows = [f for f in findings if f.level == level]
        if not rows:
            continue
        out.append(f"{level} ({len(rows)}):")
        for f in rows:
            suffix = f"  [{f.path}]" if f.path else ""
            out.append(f"  - [{f.area}] {f.message}{suffix}")
    return "\n".join(out)


# ── settings ────────────────────────────────────────────────────────────

def load_settings(path: str | None = None) -> dict:
    """Load settings.yaml (falling back to settings.example.yaml).

    Never raises: an unreadable/absent file yields ``{}`` so the checks
    can still report "not configured" instead of crashing.
    """
    candidates = [path] if path else [
        os.path.join(REPO_ROOT, "settings.yaml"),
        os.path.join(REPO_ROOT, "settings.example.yaml"),
    ]
    for cand in candidates:
        if not cand or not os.path.isfile(cand):
            continue
        try:
            import yaml
            with open(cand, encoding="utf-8") as f:
                data = yaml.safe_load(f)
            if isinstance(data, dict):
                return data
        except Exception:  # noqa: BLE001 - monitoring must never crash
            continue
    return {}


def setting(data: dict, dotted: str, default=""):
    """Dotted lookup: ``setting(cfg, "news.signal_path")``."""
    node = data
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return default if node is None else node


def resolve_news_path(value, repo_root: str = REPO_ROOT) -> str:
    """Absolutise a configured news path (mirrors run_backtest._resolve_news_path)."""
    if not value:
        return ""
    text = str(value)
    if os.path.isabs(text):
        return text
    return os.path.join(repo_root, text)


# ── redaction ───────────────────────────────────────────────────────────

_RE_WEBHOOK = re.compile(
    r"https://(?:discord|discordapp)\.com/api/webhooks/\S+", re.IGNORECASE)
_RE_KEY = re.compile(r"\b(?:sk-ant-|AIza)\S+")
_RE_TOKEN = re.compile(r"[A-Za-z0-9._-]{50,}")


def redact(text) -> str:
    """Mask webhooks, API keys and token-shaped runs in any log excerpt.

    Applied to EVERY excerpt that leaves this package (stdout, report
    file, Discord).  Order matters: webhook URLs first (they contain the
    token run), then vendor keys, then the generic long-token sweep.
    """
    if text is None:
        return ""
    out = _RE_WEBHOOK.sub("<webhook>", str(text))
    out = _RE_KEY.sub("<key>", out)
    out = _RE_TOKEN.sub("<token>", out)
    return out


# ── Discord ─────────────────────────────────────────────────────────────

def post_discord(webhook_url: str, content: str) -> bool:
    """POST a plain message to a Discord webhook. True on success.

    Mirrors scripts/news_bridge/chips_monitor.post_discord — stdlib only,
    swallows everything, never raises into a monitoring run.
    """
    if not webhook_url or not content:
        return False
    try:
        import urllib.request
        body = json.dumps({"content": str(content)[:1900]}).encode("utf-8")
        req = urllib.request.Request(
            webhook_url, data=body,
            headers={"Content-Type": "application/json",
                     "User-Agent": "Mozilla/5.0 (tai-robot monitor)"})
        urllib.request.urlopen(req, timeout=15).read()
        return True
    except Exception:  # noqa: BLE001
        return False


# ── bot directories / processes ─────────────────────────────────────────

def discover_bot_dirs(base_dir: str) -> list:
    """Sub-directories of ``data/live`` that look like a deployed bot."""
    if not base_dir or not os.path.isdir(base_dir):
        return []
    found = []
    for name in sorted(os.listdir(base_dir)):
        path = os.path.join(base_dir, name)
        if not os.path.isdir(path):
            continue
        for marker in ("session.json", ".lock", "regime_state.json"):
            if os.path.exists(os.path.join(path, marker)):
                found.append(path)
                break
    return found


def pid_alive(pid: int) -> bool:
    """Read-only Windows PID liveness.

    Faithful copy of ``LiveRunner._pid_alive`` (src/live/live_runner.py):
    ``os.kill(pid, 0)`` is NOT an existence check on Windows (signal 0 is
    CTRL_C_EVENT), so use OpenProcess + GetExitCodeProcess. Any API
    failure is reported as ALIVE — a lock we cannot verify must never be
    called stale.  Unlike ``check_lock`` this deletes nothing.
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        process_query_limited_information = 0x1000
        error_access_denied = 5
        still_active = 259

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(
            process_query_limited_information, False, pid)
        if not handle:
            return ctypes.get_last_error() == error_access_denied
        try:
            exit_code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return True
            return exit_code.value == still_active
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


def read_lock_pid(bot_dir: str):
    """``(lock_exists, pid_or_None)`` — never deletes the lock file."""
    lock = os.path.join(bot_dir, ".lock")
    if not os.path.isfile(lock):
        return False, None
    try:
        with open(lock, encoding="utf-8", errors="replace") as f:
            return True, int(f.read().strip())
    except (OSError, ValueError):
        return True, None


# ── files / logs ────────────────────────────────────────────────────────

def read_json(path):
    """Parse a JSON file; ``None`` on any failure."""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def tail_text(path, max_bytes: int = 2_000_000) -> str:
    """Last ``max_bytes`` of a text file, decoded leniently. "" on failure."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
            raw = f.read()
        return raw.decode("utf-8", errors="replace")
    except OSError:
        return ""


_RE_TS = re.compile(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")


def first_ts_in_line(line: str):
    """The FIRST ``YYYY-MM-DD HH:MM:SS`` in a line, as TPE-aware datetime.

    Debug-log lines carry both clocks —
    ``[2026-08-21 14:29:48.799 TPE / 2026-08-20 23:29:48.799 local]`` —
    and the TPE one always comes first.  Single-clock lines
    (``[YYYY-MM-DD HH:MM:SS.mmm]``, ``2026-08-26 14:40:00 TPE | ...``)
    fall out of the same rule.
    """
    m = _RE_TS.search(line or "")
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ_TPE)
    except ValueError:
        return None


def last_tpe_timestamp(text: str, scan_lines: int = 50):
    """Newest TPE timestamp near the end of a log body, or None."""
    if not text:
        return None
    lines = text.splitlines()[-scan_lines:]
    for line in reversed(lines):
        ts = first_ts_in_line(line)
        if ts is not None:
            return ts
    return None


def debug_log_open_date(path: str):
    """The TPE date a ``debug_YYYYMMDD.log`` was OPENED, or None.

    The name is stamped once, from the Taipei clock, when the deploy
    creates the file; the file then grows for as long as that deploy
    runs.  So this is the START of the log's coverage, never the end —
    ``debug_20260828.log`` carried lines through 09-07.
    """
    stem = os.path.splitext(os.path.basename(path))[0]
    tag = stem.split("_", 1)[-1]
    if len(tag) != 8 or not tag.isdigit():
        return None
    try:
        return datetime.strptime(tag, "%Y%m%d").date()
    except ValueError:
        return None


def newest_debug_logs(bot_dir: str, count: int | None = 1) -> list:
    """Newest ``debug_YYYYMMDD.log`` paths, newest first (by TPE date).

    ``count=None`` returns every log in the directory.
    """
    dated = []
    for p in glob.glob(os.path.join(bot_dir, "debug_*.log")):
        opened = debug_log_open_date(p)
        if opened is not None:
            dated.append((opened, p))
    dated.sort(reverse=True)
    return [p for _, p in dated[:count]]


def bot_name(bot_dir: str) -> str:
    return os.path.basename(os.path.normpath(bot_dir))


def last_activity(bot_dir: str):
    """Newest evidence that a bot was alive, as a TPE-aware datetime.

    The most recent of: the ``.lock`` mtime (= deploy time),
    ``session.json``'s mtime (rewritten on every trade event), and the
    newest debug log's last TPE line.  ``None`` when nothing is readable.

    ``data/live`` accumulates retired test-bot directories, and their
    frozen artefacts otherwise look identical to a bot that broke last
    night.  This is what separates "incident" from "archaeology".
    """
    stamps = []
    for marker in (".lock", "session.json", "regime_state.json"):
        try:
            stamps.append(datetime.fromtimestamp(
                os.path.getmtime(os.path.join(bot_dir, marker)), TZ_TPE))
        except OSError:
            pass
    logs = newest_debug_logs(bot_dir, 1)
    if logs:
        ts = last_tpe_timestamp(tail_text(logs[0], 200_000))
        if ts is not None:
            stamps.append(ts)
    return max(stamps) if stamps else None


def age_minutes(now: datetime, ts: datetime) -> float:
    return (to_tpe(now) - to_tpe(ts)).total_seconds() / 60.0


def guard_stdout() -> None:
    """Make stdout survive the cp950 console (bilingual output)."""
    try:
        if sys.stdout and hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        if sys.stderr and hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass


def default_base_dir() -> str:
    return os.path.join(REPO_ROOT, "data", "live")


def print_report(title: str, lines, findings) -> None:
    """Uniform CLI output for every check script."""
    print(f"=== {title} ===")
    for line in lines:
        print(redact(line))
    print()
    print(redact(fmt_findings(findings)))
    print(f"\nverdict: {verdict(findings)}")
