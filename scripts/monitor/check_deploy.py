"""Deploy-configuration verification (read-only).

Checks that the wiring a deployed bot depends on is present — never the
secret VALUES, only whether they are configured and where they point.

The historically expensive miswiring is an empty ``news.regime_vote_path``:
with "" there the runner's ``_read_regime_vote`` is inert, so every W2/W3/W4
vote file expires unread and the regime brain silently runs vote-free.
"""

from __future__ import annotations

import os
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from scripts.monitor.common import (  # noqa: E402
    Finding, bot_name, default_base_dir, discover_bot_dirs, guard_stdout,
    load_settings, print_report, read_json, resolve_news_path, setting,
)

VALID_LEGS = {"long", "short", "idle"}

REDEPLOY_RECIPE = (
    "manual redeploy (there is NO headless deploy — the deploy dialog is a "
    "GUI modal):",
    "  1. start dist\\tai_backtest\\tai_backtest.exe",
    "  2. log in (Capital API credentials)",
    "  3. open the 部署 (deploy) tab",
    "  4. pick the saved session row for the bot — the dialog pre-fills "
    "strategy/mode/legs from session.json",
    "  5. deploy; the runner re-acquires .lock and resumes from session.json",
    "note: mode_override.json can only flip trading_mode on an ALREADY "
    "RUNNING bot — it cannot start one.",
)

_NEWS_KEYS = (
    ("news.signal_path", "P2",
     "the circuit-breaker signal can never be read"),
    ("news.regime_vote_path", "P2",
     "W2/W3/W4 votes expire UNREAD — this is the historical "
     "'votes never consumed' bug"),
    ("news.events_path", "P2", "the macro-event gate fails open"),
    ("news.rss_state_file", "P2", "W3 liveness cannot be checked"),
)


def _check_settings(settings: dict, lines, findings) -> None:
    lines.append("settings wiring:")
    for key, level, consequence in _NEWS_KEYS:
        raw = setting(settings, key)
        resolved = resolve_news_path(raw, _REPO)
        if not resolved:
            lines.append(f"  {key}: NOT SET")
            findings.append(Finding(
                level, "deploy", f"{key} is empty — {consequence}", ""))
            continue
        parent = os.path.dirname(resolved) or "."
        exists = os.path.isdir(parent)
        lines.append(f"  {key}: {resolved} (parent dir "
                     f"{'ok' if exists else 'MISSING'})")
        if not exists:
            findings.append(Finding(
                level, "deploy",
                f"{key} points into a missing directory ({parent}) — "
                f"{consequence}", resolved))

    token = setting(settings, "notifications.discord_bot_token")
    channel = setting(settings, "notifications.discord_channel_id")
    lines.append(f"  notifications.discord_bot_token: "
                 f"{'configured' if token else 'NOT SET'}")
    lines.append(f"  notifications.discord_channel_id: "
                 f"{'configured' if channel else 'NOT SET'}")
    if not token or not channel:
        findings.append(Finding(
            "P2", "deploy",
            "bot Discord disabled — discord_bot_token/discord_channel_id "
            "missing, so trade and daily-report notifications go nowhere", ""))

    webhook = setting(settings, "news.discord_webhook")
    lines.append(f"  news.discord_webhook: "
                 f"{'configured' if webhook else 'NOT SET'}")
    if not webhook:
        findings.append(Finding(
            "P3", "deploy",
            "news.discord_webhook empty — monitor notifications unavailable "
            "(daily_review --discord will be a no-op)", ""))


def check_deploy(settings: dict, base_dir: str):
    """``(findings, report_lines)`` for settings wiring + per-bot deploy shape."""
    findings = []
    lines = []
    _check_settings(settings, lines, findings)
    lines.append("")

    bot_dirs = discover_bot_dirs(base_dir)
    if not bot_dirs:
        lines.append(f"no bot directories under {base_dir}")
    for bot_dir in bot_dirs:
        name = bot_name(bot_dir)
        path = os.path.join(bot_dir, "session.json")
        if not os.path.isfile(path):
            continue
        data = read_json(path)
        if not isinstance(data, dict):
            lines.append(f"--- {name}: session.json UNREADABLE")
            findings.append(Finding(
                "P2", "deploy",
                f"{name}: session.json unreadable — the deploy dialog cannot "
                f"pre-fill and a resume would start flat", path))
            continue

        mode = data.get("trading_mode", "?")
        if not data.get("regime_mode"):
            lines.append(f"--- {name}: single-strategy | {mode} | "
                         f"{data.get('strategy', '?')}")
            continue

        long_s = str(data.get("long_strategy") or "")
        short_s = str(data.get("short_strategy") or "")
        leg = str(data.get("active_leg") or "")
        lines.append(f"--- {name}: regime | {mode} | leg={leg or '?'} | "
                     f"long={long_s or '(blank)'} short={short_s or '(blank)'}")
        if not long_s or not short_s:
            findings.append(Finding(
                "P2", "deploy",
                f"{name}: regime bot with a blank leg strategy "
                f"(long={long_s or 'blank'}, short={short_s or 'blank'}) — "
                f"a swap to that leg cannot apply", path))
        if leg not in VALID_LEGS:
            findings.append(Finding(
                "P2", "deploy",
                f"{name}: active_leg {leg!r} is not one of {sorted(VALID_LEGS)}",
                path))

    lines.append("")
    lines.extend(REDEPLOY_RECIPE)
    return findings, lines


def main() -> int:
    guard_stdout()
    argv = sys.argv[1:]
    settings_path = argv[argv.index("--settings") + 1] if "--settings" in argv else None
    findings, lines = check_deploy(load_settings(settings_path), default_base_dir())
    print_report("deploy check", lines, findings)
    return 0


if __name__ == "__main__":
    sys.exit(main())
