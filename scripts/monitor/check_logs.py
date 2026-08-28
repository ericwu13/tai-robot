"""Error-pattern scan over bot + bridge logs (read-only).

The app's log handler prints no levelname, so severity has to come from
the message body — hence the explicit pattern table below rather than a
"grep for ERROR" sweep.

Debug logs are per-TPE-date and unrotated (they reach ~9 MB), so only the
tail of the newest two per bot is read.  Every excerpt is redacted before
it leaves this module.
"""

from __future__ import annotations

import json
import os
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from datetime import datetime, timedelta  # noqa: E402

from scripts.monitor.common import (  # noqa: E402
    Finding, bot_name, default_base_dir, discover_bot_dirs, guard_stdout,
    newest_debug_logs, print_report, redact, tail_text, to_tpe,
)

N8N_EVENT_LOG = os.path.join(os.path.expanduser("~"), ".n8n", "n8nEventLog.log")

# message substring -> severity.  Matched case-insensitively.
PATTERNS = {
    "P1": (
        "news framework DISABLED",
        "Corrupted signal ledger",
        "Corrupted state file",
        "Unrecoverable apply error",
        "Cannot restore leg",
        "Traceback (most recent call last)",
    ),
    "P2": (
        "Event calendar unusable or stale",
        "Discord send failed",
        "Daily report generation failed",
        "malformed vote file",
        "could not remove vote file",
        "swap_strategy refused",
        "Discarded pending recommendation",
    ),
    "P3": (
        "Dropped out-of-order aggregated bar",
        "Discarded recommendation",
    ),
}

MAX_EXAMPLES = 5
LOGS_PER_BOT = 2
# data/live keeps every retired bot's logs forever (they are per-TPE-date
# and never rotated), so an unbounded scan resurrects a March traceback
# from a dead test bot on every run. Only logs from the last week can
# describe a CURRENT problem.
LOG_LOOKBACK_DAYS = 7
BOT_TAIL_BYTES = 2_000_000
BRIDGE_TAIL_BYTES = 500_000


def recent_logs(now, bot_dir: str):
    """Newest debug logs for a bot, dropping anything older than the window."""
    cutoff = (to_tpe(now) - timedelta(days=LOG_LOOKBACK_DAYS)).strftime("%Y%m%d")
    keep = []
    for path in newest_debug_logs(bot_dir, LOGS_PER_BOT):
        tag = os.path.splitext(os.path.basename(path))[0].split("_", 1)[-1]
        if tag >= cutoff:
            keep.append(path)
    return keep


def _scan_text(text: str, path: str, findings, lines) -> None:
    """Match PATTERNS against one log body; one finding per (pattern, file)."""
    if not text:
        return
    body = text.splitlines()
    lowered = [ln.lower() for ln in body]
    for level, needles in PATTERNS.items():
        for needle in needles:
            key = needle.lower()
            hits = [body[i] for i, low in enumerate(lowered) if key in low]
            if not hits:
                continue
            examples = [redact(h.strip()[:200]) for h in hits[-MAX_EXAMPLES:]]
            findings.append(Finding(
                level, "logs",
                f"'{needle}' x{len(hits)} in {os.path.basename(path)} — "
                + " | ".join(examples),
                path))
            lines.append(f"  [{level}] {needle} x{len(hits)}")


def _scan_n8n_events(now, findings, lines) -> None:
    """Count n8n.workflow.failed events in the last 24h (best effort)."""
    text = tail_text(N8N_EVENT_LOG, BRIDGE_TAIL_BYTES)
    if not text:
        lines.append(f"n8n event log: not readable ({N8N_EVENT_LOG})")
        return
    cutoff = to_tpe(now) - timedelta(hours=24)
    failures = []
    for line in text.splitlines():
        if "n8n.workflow.failed" not in line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        raw_ts = str(event.get("ts") or "")
        try:
            ts = to_tpe(datetime.fromisoformat(raw_ts))
        except ValueError:
            ts = None
        if ts is not None and ts < cutoff:
            continue
        payload = event.get("payload") or {}
        failures.append(str(payload.get("workflowName") or payload.get("workflowId")
                            or "unknown workflow"))
    lines.append(f"n8n workflow.failed (24h): {len(failures)}")
    if failures:
        names = sorted(set(failures))
        findings.append(Finding(
            "P2", "logs",
            f"{len(failures)} n8n workflow failures in 24h ({', '.join(names)}) "
            f"— drill down with the n8n-debug skill",
            N8N_EVENT_LOG))


def check_logs(now: datetime, base_dir: str, bridge_dir: str):
    """``(findings, report_lines)`` from bot debug logs + bridge/n8n logs."""
    findings = []
    lines = [f"now: {now.strftime('%Y-%m-%d %H:%M:%S')} TPE", ""]

    lines.insert(1, f"scanning debug logs from the last {LOG_LOOKBACK_DAYS} days")
    for bot_dir in discover_bot_dirs(base_dir):
        logs = recent_logs(now, bot_dir)
        if not logs:
            continue
        before = len(findings)
        section = []
        for path in logs:
            _scan_text(tail_text(path, BOT_TAIL_BYTES), path, findings, section)
        if len(findings) > before:
            lines.append(f"--- {bot_name(bot_dir)}")
            lines.extend(section)

    lines.append("")
    service_log = os.path.join(bridge_dir, "n8n-service.log") if bridge_dir else ""
    if service_log and os.path.isfile(service_log):
        section = []
        _scan_text(tail_text(service_log, BRIDGE_TAIL_BYTES), service_log,
                   findings, section)
        lines.append(f"--- n8n-service.log ({len(section)} pattern hits)")
        lines.extend(section)
    else:
        lines.append(f"n8n-service.log not found ({service_log or 'no bridge dir'})")

    _scan_n8n_events(now, findings, lines)

    if not findings:
        lines.append("no known error patterns found")
    return findings, lines


def main() -> int:
    guard_stdout()
    from scripts.monitor.common import (load_settings, now_tpe,
                                        resolve_news_path, setting)
    base_dir = default_base_dir()
    settings = load_settings()
    signal_path = resolve_news_path(setting(settings, "news.signal_path"), _REPO)
    bridge_dir = os.path.dirname(signal_path) if signal_path else ""
    findings, lines = check_logs(now_tpe(), base_dir, bridge_dir)
    print_report("log scan", lines, findings)
    return 0


if __name__ == "__main__":
    sys.exit(main())
