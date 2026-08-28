"""Daily review orchestrator — runs every read-only check and grades them.

    C:/Python313/python.exe scripts/monitor/daily_review.py [--discord]
                                                           [--settings PATH]
                                                           [--no-report-file]

Best run ~05:10-05:30 TPE, just after the night session closes and the
regime classification/record pass has landed.  (The OS clock here is
UTC-7, so that is ~14:10 LOCAL on the PREVIOUS calendar day — never
schedule this against a naive local time.)

Always exits 0: a monitoring run that fails loudly is worse than one that
reports RED.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from datetime import datetime, timedelta, timezone  # noqa: E402

from scripts.monitor.check_bots import check_bots  # noqa: E402
from scripts.monitor.check_bridge import check_bridge  # noqa: E402
from scripts.monitor.check_deploy import check_deploy  # noqa: E402
from scripts.monitor.check_logs import check_logs  # noqa: E402
from scripts.monitor.check_regime import check_regime  # noqa: E402
from scripts.monitor.common import (  # noqa: E402
    Finding, bot_name, default_base_dir, discover_bot_dirs, fmt_findings,
    guard_stdout, load_settings, now_tpe, pid_alive, post_discord, read_json,
    read_lock_pid, redact, resolve_news_path, setting, verdict,
)

HEALTH_URL = "http://localhost:5678/healthz"
N8N_DB = os.path.join(os.path.expanduser("~"), ".n8n", "database.sqlite")
DISCORD_LIMIT = 1900
MAX_SUMMARY_BOTS = 6
MAX_SUMMARY_FINDINGS = 8

VERDICT_ICON = {"GREEN": "🟢", "YELLOW": "🟡", "RED": "🔴"}


# ── n8n quick check ─────────────────────────────────────────────────────

def _service_state(lines, findings) -> None:
    try:
        out = subprocess.run(["sc.exe", "query", "n8n-service"],
                             capture_output=True, text=True, timeout=10).stdout
    except Exception as exc:  # noqa: BLE001
        lines.append(f"n8n service: query failed ({type(exc).__name__})")
        findings.append(Finding("P2", "n8n", "could not query the n8n-service "
                                             "state (sc.exe failed)", ""))
        return
    state = next((ln.strip() for ln in out.splitlines() if "STATE" in ln), "")
    lines.append(f"n8n service: {state or 'STATE line not found'}")
    if "RUNNING" in state:
        return
    detail = ("NSSM PAUSED = crash-loop throttle; fix the cause then "
              "nssm stop + nssm start (admin)" if "PAUSED" in state
              else "service is not RUNNING")
    findings.append(Finding("P1", "n8n",
                            f"n8n-service not running — {detail}", ""))


def _healthz(lines, findings) -> None:
    try:
        import urllib.request
        with urllib.request.urlopen(HEALTH_URL, timeout=5) as resp:
            status = resp.status
            body = resp.read().decode("utf-8", "replace")[:60]
    except Exception as exc:  # noqa: BLE001
        lines.append(f"n8n healthz: UNREACHABLE ({type(exc).__name__})")
        findings.append(Finding(
            "P1", "n8n",
            "n8n healthz unreachable — service down or port 5678 conflict", ""))
        return
    lines.append(f"n8n healthz: {status} {body}")
    if status != 200:
        findings.append(Finding("P1", "n8n",
                                f"n8n healthz returned {status}", ""))


def _failed_executions(now, lines, findings) -> None:
    """Failed n8n executions in the last 24h, grouped by workflow name.

    Read-only URI against the live DB, reusing the exact table/column
    names the n8n-debug helper uses (execution_entity / workflow_entity).
    ``startedAt`` is stored in UTC.
    """
    try:
        import sqlite3
        if not os.path.exists(N8N_DB):
            raise FileNotFoundError(N8N_DB)
        con = sqlite3.connect(f"file:{N8N_DB.replace(os.sep, '/')}?mode=ro", uri=True)
        try:
            rows = con.execute(
                "select e.startedAt, w.name from execution_entity e "
                "join workflow_entity w on w.id = e.workflowId "
                "where e.status = 'error' order by e.id desc limit 500"
            ).fetchall()
        finally:
            con.close()
    except Exception as exc:  # noqa: BLE001
        lines.append(f"n8n failed executions: DB unreadable ({type(exc).__name__})")
        findings.append(Finding("P3", "n8n", "n8n DB unreadable — failed-execution "
                                             "count unavailable", N8N_DB))
        return

    cutoff = now.astimezone(timezone.utc) - timedelta(hours=24)
    counts = {}
    for started, name in rows:
        keep = True
        try:
            ts = datetime.fromisoformat(str(started).replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            keep = ts >= cutoff
        except (TypeError, ValueError):
            keep = True  # unparsable stamp — surface rather than hide
        if keep:
            counts[name] = counts.get(name, 0) + 1

    if not counts:
        lines.append("n8n failed executions (24h): none")
        return
    for name, count in sorted(counts.items()):
        lines.append(f"n8n failed executions (24h): {name} x{count}")
        findings.append(Finding(
            "P2", "n8n",
            f"{name}: {count} failed executions in 24h (use the n8n-debug "
            f"skill: execs --workflow <name> --errors, then detail <id>)", ""))


def check_n8n(now):
    findings, lines = [], []
    _service_state(lines, findings)
    _healthz(lines, findings)
    _failed_executions(now, lines, findings)
    return findings, lines


# ── summary ─────────────────────────────────────────────────────────────

def _bot_one_liners(base_dir: str):
    """Compact per-bot lines for LIVE bots only; the rest are counted.

    Liveness is the PID, not the lock file: ``data/live`` is full of
    retired bots whose lock was never cleaned.
    """
    running, idle = [], 0
    for bot_dir in discover_bot_dirs(base_dir):
        has_lock, pid = read_lock_pid(bot_dir)
        if not has_lock or pid is None or not pid_alive(pid):
            idle += 1
            continue
        data = read_json(os.path.join(bot_dir, "session.json")) or {}
        broker = data.get("broker") or {}
        leg = data.get("active_leg") or "-"
        strategy = data.get("strategy", "?")
        running.append(
            f"• {bot_name(bot_dir)}: {leg}/{strategy} | "
            f"{data.get('trading_mode', '?')} | pnl "
            f"{broker.get('_cumulative_pnl', 0):+} | lock pid {pid}")
    return running, idle


def _bridge_one_liners(now, settings):
    out = []
    try:
        from src.news.vote_status import vote_chip
        from src.regime.switch_logic import upcoming_night_session
        from src.news.vote_status import collect_vote_status
        sess = upcoming_night_session(now)
        report = collect_vote_status(
            resolve_news_path(setting(settings, "news.regime_vote_path"), _REPO),
            resolve_news_path(setting(settings, "news.signal_path"), _REPO),
            resolve_news_path(setting(settings, "news.rss_state_file"), _REPO),
            sess.key if sess else "", now)
        for st in report.sources:
            flag = "STALE" if st.stale else ("?" if not st.known else "ok")
            out.append(f"• {st.source} [{flag}] {vote_chip(st)}")
    except Exception as exc:  # noqa: BLE001
        out.append(f"• bridge status unavailable ({type(exc).__name__})")
    return out


def build_summary(now, grade: str, findings, base_dir: str, settings: dict) -> str:
    """Discord-sized digest (<= 1900 chars), fully redacted."""
    parts = [f"{VERDICT_ICON.get(grade, '')} tai-robot daily review — "
             f"{now.strftime('%Y-%m-%d')} (TPE)"]

    running, idle = _bot_one_liners(base_dir)
    parts.extend(running[:MAX_SUMMARY_BOTS])
    if len(running) > MAX_SUMMARY_BOTS:
        parts.append(f"• …{len(running) - MAX_SUMMARY_BOTS} more running bots")
    parts.append(f"• {idle} idle bot dir(s) (no live lock)")
    parts.extend(_bridge_one_liners(now, settings))

    # P3 is informational (retired dirs, "suppressed by design") and would
    # crowd out the actionable rows — summarise it as a count only.
    actionable = ([f for f in findings if f.level == "P1"]
                  + [f for f in findings if f.level == "P2"])
    p3 = sum(1 for f in findings if f.level == "P3")
    parts.append(f"findings: P1 {sum(1 for f in findings if f.level == 'P1')}, "
                 f"P2 {sum(1 for f in findings if f.level == 'P2')}, P3 {p3}")
    for f in actionable[:MAX_SUMMARY_FINDINGS]:
        parts.append(f"{f.level} [{f.area}] {f.message}")
    if len(actionable) > MAX_SUMMARY_FINDINGS:
        parts.append(f"…{len(actionable) - MAX_SUMMARY_FINDINGS} more — see the "
                     f"full report")
    if not actionable:
        parts.append("no P1/P2 findings")

    text = redact("\n".join(parts))
    if len(text) > DISCORD_LIMIT:
        text = text[:DISCORD_LIMIT - 3] + "..."
    return text


def run_review(now, settings: dict, base_dir: str):
    """Run every check. Returns ``(grade, findings, full_report_text)``."""
    signal_path = resolve_news_path(setting(settings, "news.signal_path"), _REPO)
    bridge_dir = os.path.dirname(signal_path) if signal_path else ""

    sections = [
        ("BOTS", lambda: check_bots(now, base_dir)),
        ("REGIME", lambda: check_regime(now, base_dir)),
        ("BRIDGE", lambda: check_bridge(now, settings, base_dir)),
        ("LOGS", lambda: check_logs(now, base_dir, bridge_dir)),
        ("DEPLOY", lambda: check_deploy(settings, base_dir)),
        ("N8N", lambda: check_n8n(now)),
    ]

    all_findings = []
    body = [f"# tai-robot daily review — {now.strftime('%Y-%m-%d %H:%M:%S')} TPE", ""]
    for title, fn in sections:
        try:
            findings, lines = fn()
        except Exception as exc:  # noqa: BLE001
            findings = [Finding("P2", title.lower(),
                                f"check crashed: {type(exc).__name__}: {exc}", "")]
            lines = [f"check crashed: {type(exc).__name__}: {exc}"]
        all_findings.extend(findings)
        body.append(f"## {title}")
        body.append("```")
        body.extend(redact(line) for line in lines)
        body.append("```")
        body.append(redact(fmt_findings(findings)))
        body.append("")

    grade = verdict(all_findings)
    body.insert(1, f"**verdict: {grade}**")
    return grade, all_findings, "\n".join(body)


def main() -> int:
    guard_stdout()
    ap = argparse.ArgumentParser(description="tai-robot daily monitoring review")
    ap.add_argument("--discord", action="store_true",
                    help="post the summary to news.discord_webhook")
    ap.add_argument("--settings", help="path to settings.yaml")
    ap.add_argument("--no-report-file", action="store_true",
                    help="skip writing data/monitor/daily_review_<date>.md")
    args = ap.parse_args()

    now = now_tpe()
    settings = load_settings(args.settings)
    base_dir = default_base_dir()

    grade, findings, report = run_review(now, settings, base_dir)
    print(report)

    summary = build_summary(now, grade, findings, base_dir, settings)
    print("\n--- discord summary ---")
    print(summary)

    if not args.no_report_file:
        out_dir = os.path.join(_REPO, "data", "monitor")
        try:
            os.makedirs(out_dir, exist_ok=True)
            out_path = os.path.join(
                out_dir, f"daily_review_{now.strftime('%Y-%m-%d')}.md")
            with open(out_path, "w", encoding="utf-8") as f:
                f.write(report + "\n\n## SUMMARY\n\n" + summary + "\n")
            print(f"\nreport written: {out_path}")
        except OSError as exc:
            print(f"\ncould not write report file: {exc}")

    if args.discord:
        webhook = setting(settings, "news.discord_webhook")
        if not webhook:
            print("\n--discord requested but news.discord_webhook is not configured")
        else:
            print(f"\ndiscord post: {'ok' if post_discord(webhook, summary) else 'FAILED'}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
