"""Bot process / liveness check (read-only).

Answers "is the deployed bot actually alive, and is it still ticking?".

Two facts drive everything:

- a CLEAN stop deletes ``.lock``, so a lock whose PID is dead means the
  process died without unwinding — a crash, not a shutdown.
- log silence is only meaningful INSIDE a trading session. Between
  sessions (and on weekends/TAIFEX holidays) no ticks flow, so the debug
  log is legitimately quiet for hours.

Nothing here mutates state: stale locks are REPORTED, never removed (the
app self-heals them on the next deploy).
"""

from __future__ import annotations

import os
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

import csv  # noqa: E402
from datetime import datetime, timedelta  # noqa: E402

from scripts.monitor.common import (  # noqa: E402
    TZ_TPE, Finding, age_minutes, bot_name, default_base_dir,
    discover_bot_dirs, guard_stdout, last_activity, last_tpe_timestamp,
    newest_debug_logs, pid_alive, print_report, read_json, read_lock_pid,
    tail_text,
)

# A live bot writes a status/tick line well inside this window.
STALE_LOG_MINUTES = 10.0

# data/live accumulates abandoned test-bot directories whose .lock was
# never cleaned (the app removes stale locks on the NEXT deploy of that
# bot, which for a retired bot never comes). A dead-PID lock is only an
# incident when the bot was recently alive — otherwise it is archaeology,
# and grading it P1 would pin the daily verdict at RED forever.
RECENT_CRASH_DAYS = 3.0


def count_decisions(now: datetime, bot_dir: str, action: str):
    """Rows in decisions.csv with *action* whose bar_dt is inside 24h.

    Returns None when there is no readable decisions.csv.  Keyed on
    column 1 (bar_dt, TPE) — column 0 is stamped with ``datetime.now()``,
    the MACHINE clock (UTC-7 on this box), and reading it as TPE shifts
    the window by 15h.  Shared with check_regime's NEWS_SUPPRESSED scan.
    """
    path = os.path.join(bot_dir, "decisions.csv")
    if not os.path.isfile(path):
        return None
    cutoff = now - timedelta(hours=24)
    count = 0
    try:
        with open(path, encoding="utf-8", errors="replace", newline="") as f:
            for row in csv.reader(f):
                if len(row) < 4 or (row[3] or "").strip() != action:
                    continue
                try:
                    ts = datetime.strptime(row[1].strip(), "%Y-%m-%d %H:%M")
                except ValueError:
                    continue
                if ts.replace(tzinfo=TZ_TPE) >= cutoff:
                    count += 1
    except OSError:
        return None
    return count


def _check_order_timeouts(now, bot_dir, name, lines, findings) -> None:
    """REAL_ORDER_TIMEOUT = the semi_auto confirm dialog auto-skipped
    after 10s; the signal ran paper-only (the order was never sent — see
    run_backtest._dismiss_order_dialog and the trade-source downgrade in
    test_trade_source_and_hotswap).

    P3 INFORMATIONAL by user decision (2026-09-09): unattended semi_auto
    skipping is the FEATURE — "trade real only when someone confirms" —
    so a recurring count must never grade P2 (it pinned every digest
    YELLOW for by-design behavior). The count stays visible as a report
    line + P3 because sim P&L (and the evolution fitness baseline) then
    includes trades the real account never took — context, not a fault.
    """
    timeouts = count_decisions(now, bot_dir, "REAL_ORDER_TIMEOUT")
    if not timeouts:
        return
    lines.append(f"  order timeouts (24h): {timeouts}")
    findings.append(Finding(
        "P3", "bots",
        f"{name}: {timeouts} semi_auto confirm auto-skip(s) in 24h — by "
        f"design (unattended signals run paper-only); sim P&L includes "
        f"trades the real account did not take",
        os.path.join(bot_dir, "decisions.csv")))


def _session_now(now: datetime):
    """``current_session(now)`` or None — degrades to None if unavailable."""
    try:
        from src.regime.switch_logic import current_session
        return current_session(now)
    except Exception:  # noqa: BLE001
        return None


def _build_info(repo_root: str, lines, findings) -> None:
    path = os.path.join(repo_root, "dist", "tai_backtest", "BUILD_INFO.json")
    info = read_json(path)
    if info is None:
        lines.append("build: dist/tai_backtest/BUILD_INFO.json not found "
                     "(no packaged EXE in this tree)")
        return
    version = info.get("version", "?")
    commit = str(info.get("commit", "?"))[:12]
    dirty = info.get("dirty")
    lines.append(f"build: v{version} commit {commit} dirty={dirty}")
    if dirty:
        findings.append(Finding(
            "P3", "build",
            f"packaged EXE v{version} was built from a DIRTY tree "
            f"(commit {commit}) — it may not match its tag",
            path))


def check_bots(now: datetime, base_dir: str, pid_alive_fn=pid_alive):
    """``(findings, report_lines)`` for every deployed bot directory."""
    findings = []
    lines = []

    session = _session_now(now)
    lines.append(f"now: {now.strftime('%Y-%m-%d %H:%M:%S')} TPE")
    lines.append(f"session: {session.key if session else 'CLOSED (gap/weekend/holiday)'}")
    lines.append("")

    bot_dirs = discover_bot_dirs(base_dir)
    if not bot_dirs:
        lines.append(f"no bot directories under {base_dir}")
        return findings, lines

    for bot_dir in bot_dirs:
        name = bot_name(bot_dir)
        lines.append(f"--- {name}")

        has_lock, pid = read_lock_pid(bot_dir)
        alive = False
        if not has_lock:
            lines.append("  lock: none — not running")
            findings.append(Finding(
                "P3", "bots", f"{name}: no .lock — bot is not running",
                bot_dir))
        elif pid is None:
            lines.append("  lock: present but PID unparsable")
            findings.append(Finding(
                "P2", "bots",
                f"{name}: unparsable lock file — cannot verify the owner",
                os.path.join(bot_dir, ".lock")))
        else:
            alive = bool(pid_alive_fn(pid))
            lines.append(f"  lock: pid {pid} {'ALIVE' if alive else 'DEAD'}")
            if not alive:
                seen = last_activity(bot_dir)
                days = ((now - seen).total_seconds() / 86400.0
                        if seen is not None else None)
                if days is None or days <= RECENT_CRASH_DAYS:
                    findings.append(Finding(
                        "P1", "bots",
                        f"{name}: lock held by dead PID {pid} — bot crashed "
                        f"(a clean stop deletes the lock); last activity "
                        f"{seen.strftime('%Y-%m-%d %H:%M') if seen else 'unknown'} TPE",
                        os.path.join(bot_dir, ".lock")))
                else:
                    findings.append(Finding(
                        "P3", "bots",
                        f"{name}: abandoned stale lock (dead PID {pid}, last "
                        f"activity {days:.0f} days ago) — archaeology, the app "
                        f"clears it on the next deploy of this bot",
                        os.path.join(bot_dir, ".lock")))

        logs = newest_debug_logs(bot_dir, 1)
        if not logs:
            lines.append("  log: no debug_YYYYMMDD.log")
        else:
            log_path = logs[0]
            ts = last_tpe_timestamp(tail_text(log_path, 200_000))
            if ts is None:
                lines.append(f"  log: {os.path.basename(log_path)} (no parsable timestamp)")
            else:
                mins = age_minutes(now, ts)
                lines.append(f"  log: {os.path.basename(log_path)} last line "
                             f"{ts.strftime('%Y-%m-%d %H:%M:%S')} TPE ({mins:.0f} min ago)")
                if session is not None and alive and mins > STALE_LOG_MINUTES:
                    findings.append(Finding(
                        "P1", "bots",
                        f"{name}: debug log frozen mid-session (hang?) — "
                        f"last line {mins:.0f} min ago during {session.key}",
                        log_path))
                elif session is None:
                    lines.append("  log freshness: not checked (market closed)")

        sess_path = os.path.join(bot_dir, "session.json")
        if os.path.isfile(sess_path):
            data = None
            try:
                from src.live.session_store import load_session
                data = load_session(sess_path)
            except Exception:  # noqa: BLE001
                data = read_json(sess_path)
            if data is None:
                findings.append(Finding(
                    "P2", "bots",
                    f"{name}: session.json unreadable — a resume would start flat",
                    sess_path))
                lines.append("  session: UNREADABLE")
            else:
                broker = data.get("broker") or {}
                pos = broker.get("position_size", 0)
                side = broker.get("position_side") or "flat"
                lines.append(
                    f"  session: {data.get('strategy', '?')} | "
                    f"{data.get('trading_mode', '?')} | saved {data.get('saved_at', '?')}")
                lines.append(
                    f"  broker: pnl {broker.get('_cumulative_pnl', 0):+} | "
                    f"position {side} x{pos} | trades {len(broker.get('trades') or [])}")
        else:
            lines.append("  session: no session.json")

        _check_order_timeouts(now, bot_dir, name, lines, findings)

    lines.append("")
    _build_info(_REPO, lines, findings)
    return findings, lines


def main() -> int:
    guard_stdout()
    from scripts.monitor.common import now_tpe
    base_dir = sys.argv[1] if len(sys.argv) > 1 else default_base_dir()
    findings, lines = check_bots(now_tpe(), base_dir)
    print_report("bot status", lines, findings)
    return 0


if __name__ == "__main__":
    sys.exit(main())
