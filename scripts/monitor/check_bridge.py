"""News/vote bridge pipeline health (read-only).

Answers "are W2/W3/W4 still alive, and is anything they produce reaching
the bot?".

Read this before flagging anything: the ABSENCE of a vote file is the
normal resting state.  W4 deletes its vote when it has no opinion, and
the classifier consumes (and deletes) every pending vote after each
pass.  Liveness therefore comes from the bridges' own sidecar state
files, never from "is there a vote on disk".
"""

from __future__ import annotations

import os
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from datetime import datetime, timedelta  # noqa: E402

from scripts.monitor.common import (  # noqa: E402
    Finding, default_base_dir, discover_bot_dirs, first_ts_in_line,
    guard_stdout, load_settings, print_report, read_json, resolve_news_path,
    setting, tail_text, to_tpe,
)

MONITOR_SILENT_MINUTES = 60.0
EVENTS_STALE_DAYS = 14
SIGNAL_MAX_AGE_SEC = 900
UNCONSUMED_AFTER_MINUTES = 10.0
CHIPS_VOTELESS_STREAK = 3
CHIPS_PRIMARY_SLOT = (16, 15)
CHIPS_SLOT_TOLERANCE_MIN = 5

# Reject reasons that describe a HEALTHY resting state, not a bug.
_BENIGN_REJECTS = ("stale signal", "signal file not found")


def bridge_paths(settings: dict) -> dict:
    """Resolved bridge artefact paths from settings.yaml."""
    return {
        "signal_path": resolve_news_path(setting(settings, "news.signal_path"), _REPO),
        "regime_vote_path": resolve_news_path(
            setting(settings, "news.regime_vote_path"), _REPO),
        "events_path": resolve_news_path(setting(settings, "news.events_path"), _REPO),
        "rss_state_file": resolve_news_path(
            setting(settings, "news.rss_state_file"), _REPO),
    }


def _tonight_key(now) -> str:
    try:
        from src.regime.switch_logic import upcoming_night_session
        sess = upcoming_night_session(now)
        return sess.key if sess else ""
    except Exception:  # noqa: BLE001
        return ""


def _check_votes(now, paths, tonight_key, lines, findings):
    try:
        from src.news.vote_status import STALE_AFTER_H, collect_vote_status, vote_chip
    except Exception:  # noqa: BLE001
        lines.append("vote status: src.news.vote_status unavailable — skipped")
        return

    report = collect_vote_status(
        paths["regime_vote_path"], paths["signal_path"],
        paths["rss_state_file"], tonight_key, now)
    lines.append(f"tonight key: {report.tonight_key or '(unknown)'}")
    for st in report.sources:
        lines.append(
            f"  {st.source}: {vote_chip(st)} | last_check "
            f"{st.last_check or '-'} | {st.context or '-'}")
        if st.stale:
            findings.append(Finding(
                "P2", "bridge",
                f"{st.source} bridge stale (last_check {st.last_check or '?'}, "
                f"threshold {STALE_AFTER_H.get(st.source, 24.0):g}h)",
                paths["regime_vote_path"]))
        elif not st.known:
            findings.append(Finding(
                "P3", "bridge",
                f"{st.source} liveness unknown: {st.context or 'no state file'}",
                paths["regime_vote_path"]))
    lines.append("  (a missing vote file is NORMAL — W4 deletes on no-vote, "
                 "the classifier consumes and deletes after each pass)")


def _consumed_by(signal_id: str, base_dir: str):
    """Bot names whose news_ledger.json lists ``signal_id``."""
    owners = []
    for bot_dir in discover_bot_dirs(base_dir):
        ledger = read_json(os.path.join(bot_dir, "news_ledger.json"))
        if not isinstance(ledger, dict):
            continue
        consumed = ledger.get("consumed")
        if isinstance(consumed, list) and signal_id in consumed:
            owners.append(os.path.basename(os.path.normpath(bot_dir)))
    return owners


def _check_signal(now, paths, base_dir, lines, findings):
    try:
        from src.news.signal_file import read_signal
    except Exception:  # noqa: BLE001
        lines.append("signal: src.news.signal_file unavailable — skipped")
        return

    signal, reason = read_signal(paths["signal_path"], now=now,
                                 max_age_sec=SIGNAL_MAX_AGE_SEC)
    if signal is None:
        low = reason.lower()
        if any(low.startswith(b) for b in _BENIGN_REJECTS):
            lines.append(f"signal: none active ({reason}) — healthy resting state")
        elif "no signal_path configured" in low:
            lines.append("signal: no signal_path configured")
            findings.append(Finding(
                "P2", "bridge",
                "news.signal_path is empty — the bot can never read a "
                "circuit-breaker signal", ""))
        else:
            lines.append(f"signal: REJECTED ({reason})")
            findings.append(Finding(
                "P2", "bridge",
                f"signal file rejected by the reader — writer bug: {reason}",
                paths["signal_path"]))
        return

    age_min = (to_tpe(now) - to_tpe(signal.issued_at)).total_seconds() / 60.0
    owners = _consumed_by(signal.signal_id, base_dir)
    lines.append(f"signal: {signal.action} ({signal.severity or '-'}, "
                 f"{signal.direction or 'no direction'}) issued "
                 f"{signal.issued_at.isoformat()} — {age_min:.0f} min old")
    lines.append(f"  consumed by: {', '.join(owners) if owners else 'nobody yet'}")
    if not owners and age_min > UNCONSUMED_AFTER_MINUTES:
        findings.append(Finding(
            "P1", "bridge",
            f"fresh signal {signal.action} ({signal.signal_id[:8]}) is "
            f"{age_min:.0f} min old and consumed by NO bot — the poll loop "
            f"or news wiring is dead",
            paths["signal_path"]))
    else:
        findings.append(Finding(
            "P3", "bridge",
            f"fresh signal {signal.action} "
            f"{'consumed by ' + ', '.join(owners) if owners else 'pending consumption'}",
            paths["signal_path"]))


def _check_events(now, paths, lines, findings):
    path = paths["events_path"]
    data = read_json(path) if path else None
    if not isinstance(data, dict):
        lines.append(f"events: unreadable or missing ({path or 'not configured'})")
        findings.append(Finding(
            "P2", "bridge",
            "event calendar missing/unreadable — the event gate fails open",
            path))
        return
    raw = str(data.get("updated_at") or "")
    try:
        updated = datetime.fromisoformat(raw)
    except ValueError:
        updated = None
    if updated is None:
        lines.append(f"events: unparsable updated_at ({raw!r})")
        findings.append(Finding(
            "P2", "bridge",
            f"event calendar updated_at unparsable ({raw!r}) — the gate fails open",
            path))
        return
    age_days = (to_tpe(now) - to_tpe(updated)).total_seconds() / 86400.0
    count = len(data.get("events") or [])
    lines.append(f"events: {count} entries, updated {raw} ({age_days:.1f} days ago)")
    if age_days > EVENTS_STALE_DAYS:
        findings.append(Finding(
            "P2", "bridge",
            f"event calendar stale ({age_days:.0f} days) — bot will fail open "
            f"on macro events", path))


def _check_monitor_log(now, bridge_dir, lines, findings):
    path = os.path.join(bridge_dir, "monitor.log")
    text = tail_text(path, 500_000)
    if not text:
        lines.append("W2 monitor.log: missing or empty")
        findings.append(Finding(
            "P2", "bridge", "W2 monitor.log missing — cross-market monitor "
                            "may never have run", path))
        return
    recent = text.splitlines()[-200:]
    last_ts = None
    for line in reversed(recent):
        last_ts = first_ts_in_line(line)
        if last_ts:
            break
    if last_ts is None:
        lines.append("W2 monitor.log: no parsable timestamp in the last 200 lines")
    else:
        mins = (to_tpe(now) - last_ts).total_seconds() / 60.0
        lines.append(f"W2 monitor.log: last pass {last_ts.strftime('%Y-%m-%d %H:%M')} "
                     f"TPE ({mins:.0f} min ago)")
        if mins > MONITOR_SILENT_MINUTES:
            findings.append(Finding(
                "P2", "bridge",
                f"W2 monitor silent for {mins:.0f} min (threshold "
                f"{MONITOR_SILENT_MINUTES:.0f}) — check the W2 cron",
                path))

    fetch_fails = [ln for ln in recent if "fetch-fail" in ln.lower()]
    if len(fetch_fails) >= 3:
        findings.append(Finding(
            "P2", "bridge",
            f"W2 reported {len(fetch_fails)} fetch-fail lines in the last "
            f"200 passes — upstream quotes are unreliable", path))
    for needle, label in (("vote: contradiction", "vote contradiction"),
                          ("force-fire", "force-fire")):
        hits = [ln for ln in recent if needle in ln.lower()]
        if hits:
            findings.append(Finding(
                "P3", "bridge",
                f"W2 {label} x{len(hits)} (latest: {hits[-1].strip()[:160]})",
                path))


def _check_chips_log(now, bridge_dir, lines, findings):
    path = os.path.join(bridge_dir, "chips_monitor.log")
    text = tail_text(path, 500_000)
    if not text:
        lines.append("W4 chips_monitor.log: missing or empty")
        findings.append(Finding(
            "P2", "bridge", "W4 chips_monitor.log missing — the chips bridge "
                            "may never have run", path))
        return
    body = [ln for ln in text.splitlines() if ln.strip()]
    lines.append(f"W4 chips_monitor.log: {len(body)} lines, "
                 f"last: {body[-1].strip()[:160]}")

    streak = 0
    for line in reversed(body):
        if "no taifex data" in line.lower():
            streak += 1
        else:
            break
    if streak >= CHIPS_VOTELESS_STREAK:
        findings.append(Finding(
            "P2", "bridge",
            f"W4 voteless for {streak} consecutive passes "
            f"('no TAIFEX data') — the OpenAPI lag/retry path is not landing "
            f"data", path))
    else:
        lines.append(f"  voteless streak: {streak}")

    cutoff = to_tpe(now) - timedelta(days=7)
    slots = set()
    for line in body:
        ts = first_ts_in_line(line)
        if ts is None or ts < cutoff:
            continue
        slots.add((ts.hour, ts.minute))
    if slots:
        lines.append("  slots (7d): "
                     + ", ".join(f"{h:02d}:{m:02d}" for h, m in sorted(slots)))

        def is_primary(slot):
            h, m = slot
            return abs((h * 60 + m)
                       - (CHIPS_PRIMARY_SLOT[0] * 60 + CHIPS_PRIMARY_SLOT[1])
                       ) <= CHIPS_SLOT_TOLERANCE_MIN

        if all(is_primary(s) for s in slots):
            findings.append(Finding(
                "P2", "bridge",
                "W4 retry crons leave no trace — only the 16:15 slot appears "
                "in 7 days of chips_monitor.log; the live workflow may predate "
                "the 18:15/20:15/22:15/04:15 retry slots",
                path))


def check_bridge(now: datetime, settings: dict, base_dir: str | None = None):
    """``(findings, report_lines)`` for the news/vote bridge."""
    findings = []
    lines = []
    base_dir = base_dir or default_base_dir()
    paths = bridge_paths(settings)
    tonight_key = _tonight_key(now)

    lines.append(f"now: {now.strftime('%Y-%m-%d %H:%M:%S')} TPE")
    for key in ("signal_path", "regime_vote_path", "events_path", "rss_state_file"):
        lines.append(f"  {key}: {paths[key] or '(not configured)'}")
    lines.append("")

    _check_votes(now, paths, tonight_key, lines, findings)
    lines.append("")
    _check_signal(now, paths, base_dir, lines, findings)
    _check_events(now, paths, lines, findings)
    lines.append("")

    bridge_dir = os.path.dirname(paths["signal_path"] or paths["regime_vote_path"] or "")
    if bridge_dir and os.path.isdir(bridge_dir):
        _check_monitor_log(now, bridge_dir, lines, findings)
        _check_chips_log(now, bridge_dir, lines, findings)
        lines.append("  (rss_scorer.log is never written in production — W3 runs "
                     "the --once path; use rss_scorer_state.json for liveness)")
    else:
        lines.append(f"bridge log dir not found: {bridge_dir or '(unresolved)'}")
        findings.append(Finding(
            "P2", "bridge",
            "bridge working directory could not be resolved from settings — "
            "no liveness logs to read", bridge_dir))

    return findings, lines


def main() -> int:
    guard_stdout()
    from scripts.monitor.common import now_tpe
    settings_path = None
    argv = sys.argv[1:]
    if "--settings" in argv:
        settings_path = argv[argv.index("--settings") + 1]
    findings, lines = check_bridge(now_tpe(), load_settings(settings_path))
    print_report("bridge health", lines, findings)
    return 0


if __name__ == "__main__":
    sys.exit(main())
