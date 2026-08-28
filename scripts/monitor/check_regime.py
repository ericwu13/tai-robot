"""Regime-brain health check (read-only).

Answers "did the regime bot classify last night, and did the resulting
recommendation actually get applied?".

Session identity is the OPEN date — the night opening Mon 15:00 and
closing Tue 05:00 is ``2026-08-24|NIGHT`` on both sides of midnight.
Every key compared here uses that convention.

Two history columns are DEAD by construction and are never read:
``applied`` / ``applied_at``.  Application evidence lives in
``regime_state.json``'s ``next_session.executed``.
"""

from __future__ import annotations

import csv
import os
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from datetime import datetime, timedelta  # noqa: E402

from scripts.monitor.common import (  # noqa: E402
    TZ_TPE, Finding, bot_name, default_base_dir, discover_bot_dirs,
    guard_stdout, last_activity, print_report, read_json,
)

# NEWS_SUPPRESSED rows above this in 24h means the news gate is eating a
# whole session's bars rather than gating a shock window.
NEWS_SUPPRESSED_BUDGET = 20
PNL_GRACE_HOURS = 2.0


def _switch_logic():
    """Import switch_logic lazily; None when it can't load (frozen EXE)."""
    try:
        from src.regime import switch_logic
        return switch_logic
    except Exception:  # noqa: BLE001
        return None


def _night_close_dt(open_date: str):
    try:
        d = datetime.strptime(open_date, "%Y-%m-%d")
    except (TypeError, ValueError):
        return None
    return d.replace(hour=15, tzinfo=TZ_TPE) + timedelta(hours=14)


def _was_running(bot_dir, night) -> bool:
    """True when the bot showed any activity after ``night`` opened.

    A stopped bot cannot classify, so a retired test-bot directory frozen
    in July must not be graded the same as a live bot that missed last
    night's classification.
    """
    if night is None:
        return True
    seen = last_activity(bot_dir)
    return seen is None or seen >= night.open_dt


def _check_state(now, bot_dir, name, state, sl, lines, findings):
    path = os.path.join(bot_dir, "regime_state.json")
    last_completed = sl.last_completed_night(now) if sl else None
    latest = sl.latest_night_session(now) if sl else None

    last_assessed = str(state.get("last_assessed") or "")
    is_placeholder = "regime" in state and "last_assessed" not in state
    if is_placeholder:
        lines.append("  state: deploy placeholder — never classified")
        findings.append(Finding(
            "P2", "regime",
            f"{name}: never classified yet (insufficient bars, or a fresh "
            f"deploy that has not reached a night close)",
            path))
        return

    lines.append(f"  effective: {state.get('effective_regime', '?')} "
                 f"(raw {state.get('raw_regime', '?')}, since "
                 f"{state.get('effective_since', '?')})")
    lines.append(f"  last_assessed: {last_assessed or '(none)'}"
                 + (f" | last completed night: {last_completed.key}"
                    if last_completed else ""))

    if last_completed and last_assessed:
        healthy = last_assessed in {last_completed.key,
                                    latest.key if latest else ""}
        if not healthy:
            if last_assessed < last_completed.key:
                findings.append(Finding(
                    "P1", "regime",
                    f"{name}: classification missed for night "
                    f"{last_completed.key} (state still at {last_assessed})",
                    path))
            else:
                lines.append("  note: last_assessed is ahead of the last "
                             "completed night (clock skew?)")
    elif not last_assessed:
        findings.append(Finding(
            "P2", "regime", f"{name}: empty last_assessed — never classified",
            path))

    if last_assessed and str(state.get("key_format", "")) != "open-date":
        findings.append(Finding(
            "P2", "regime",
            f"{name}: pre-v2.16 key format "
            f"({state.get('key_format') or 'absent'}) — migration pending",
            path))

    # Flip-counter pause
    paused_until = state.get("paused_until_session")
    session_count = state.get("session_count")
    if isinstance(paused_until, int) and isinstance(session_count, int):
        if paused_until >= session_count:
            findings.append(Finding(
                "P2", "regime",
                f"{name}: flip-counter pause active until session "
                f"{paused_until} (now {session_count}) — regime frozen; "
                f"flip_history={state.get('flip_history')}",
                path))
        else:
            lines.append(f"  flips: session {session_count}, "
                         f"pause cleared at {paused_until}")

    # Pending recommendation
    nxt = state.get("next_session") or {}
    if nxt:
        lines.append(
            f"  next_session: {nxt.get('date', '?')} {nxt.get('action', '?')} "
            f"-> {nxt.get('strategy', '?')} executed={nxt.get('executed')} "
            f"({nxt.get('reason', '')})")
        if not nxt.get("executed") and sl is not None:
            in_gap = sl.in_closed_gap(now)
            rec_key = f"{nxt.get('date', '')}|NIGHT"
            if not in_gap and last_completed and last_completed.key == rec_key:
                findings.append(Finding(
                    "P2", "regime",
                    f"{name}: recommendation never applied "
                    f"({nxt.get('action')} -> {nxt.get('strategy')}, "
                    f"{nxt.get('reason', '')}) — blocked by position/veto/replay?",
                    path))

    feats = state.get("last_features") or {}
    if "_vote_sources" in feats:
        sources = feats.get("_vote_sources") or []
        lines.append(f"  consumed votes: {', '.join(map(str, sources)) or 'none'}"
                     + (" (accelerated)" if feats.get("_vote_accelerated") else ""))
    else:
        lines.append("  consumed votes: (state predates vote-source persistence)")


def _check_history(now, bot_dir, name, lines, findings):
    path = os.path.join(bot_dir, "regime_history.csv")
    if not os.path.isfile(path):
        lines.append("  history: no regime_history.csv")
        return
    try:
        with open(path, encoding="utf-8", errors="replace", newline="") as f:
            rows = list(csv.reader(f))
    except OSError:
        lines.append("  history: unreadable")
        return
    if len(rows) < 2:
        lines.append("  history: empty")
        return

    header = [h.strip() for h in rows[0]]
    idx = {h: i for i, h in enumerate(header)}

    def cell(row, key, fallback=None):
        i = idx.get(key, fallback)
        if i is None or i >= len(row):
            return ""
        return (row[i] or "").strip()

    nights = [r for r in rows[1:] if len(r) > 1 and cell(r, "session", 1) == "NIGHT"]
    if not nights:
        lines.append("  history: no NIGHT rows")
        return

    last_night = nights[-1]
    date = cell(last_night, "date", 0)
    raw = cell(last_night, "raw_regime", 8)
    lines.append(f"  history: last NIGHT row {date} raw={raw or '(empty)'} "
                 f"effective={cell(last_night, 'effective_regime', 9) or '-'} "
                 f"pnl={cell(last_night, 'pnl', 15) or '(empty)'} "
                 f"trades={cell(last_night, 'trades', 16) or '-'}")
    if not raw:
        lines.append("  note: result row only — the bot wasn't classifying that night")

    classifications = [r for r in nights if cell(r, "raw_regime", 8)]
    if classifications:
        last_cls = classifications[-1]
        lines.append(f"  votes (last classification {cell(last_cls, 'date', 0)}): "
                     f"{cell(last_cls, 'votes', 21) or 'none recorded'}")
        close_dt = _night_close_dt(cell(last_cls, "date", 0))
        if (close_dt is not None
                and now >= close_dt + timedelta(hours=PNL_GRACE_HOURS)
                and not cell(last_cls, "pnl", 15)):
            findings.append(Finding(
                "P2", "regime",
                f"{name}: session P&L never recorded for night "
                f"{cell(last_cls, 'date', 0)}|NIGHT (closed "
                f"{close_dt.strftime('%Y-%m-%d %H:%M')} TPE)",
                path))


def _check_news_block(now, bot_dir, name, sl, lines, findings):
    path = os.path.join(bot_dir, "session.json")
    data = read_json(path)
    if not isinstance(data, dict):
        return
    news = data.get("news")
    if not isinstance(news, dict):
        return
    suppressed = bool(news.get("signal_suppressed") or news.get("event_suppressed"))
    key = str(news.get("signal_session_key") or "")
    reason = str(news.get("suppressed_reason") or "")
    if not suppressed:
        lines.append("  news gate: open")
        return

    valid_keys = set()
    if sl is not None:
        for fn in (sl.last_completed_night, sl.upcoming_night_session):
            try:
                sess = fn(now)
            except Exception:  # noqa: BLE001
                sess = None
            if sess is not None:
                valid_keys.add(sess.key)

    if key and valid_keys and key not in valid_keys:
        findings.append(Finding(
            "P1", "regime",
            f"{name}: news suppression LATCHED with stale session key {key} "
            f"(current keys {sorted(valid_keys)}) — session-boundary expiry "
            f"is not running; reason={reason}",
            path))
        lines.append(f"  news gate: LATCHED on stale key {key}")
    else:
        findings.append(Finding(
            "P3", "regime",
            f"{name}: news suppressed (by design): {reason or 'no reason recorded'} "
            f"[{key or 'no session key'}]",
            path))
        lines.append(f"  news gate: suppressed ({reason}) key={key or '-'}")


def _check_decisions(now, bot_dir, name, lines, findings):
    path = os.path.join(bot_dir, "decisions.csv")
    if not os.path.isfile(path):
        return
    cutoff = now - timedelta(hours=24)
    counts = {"NEWS_SUPPRESSED": 0, "REAL_ORDER_TIMEOUT": 0}
    try:
        with open(path, encoding="utf-8", errors="replace", newline="") as f:
            for row in csv.reader(f):
                if len(row) < 4:
                    continue
                action = (row[3] or "").strip()
                if action not in counts:
                    continue
                # Column 0 is stamped with datetime.now() — the MACHINE
                # clock (UTC-7 here), not TPE. Column 1 (bar_dt) is the
                # bar open in TPE, which is also the semantically right
                # base for "bars swallowed in the last 24h".
                try:
                    ts = datetime.strptime(row[1].strip(), "%Y-%m-%d %H:%M")
                except ValueError:
                    continue
                if ts.replace(tzinfo=TZ_TPE) >= cutoff:
                    counts[action] += 1
    except OSError:
        return

    lines.append(f"  decisions (24h): NEWS_SUPPRESSED={counts['NEWS_SUPPRESSED']} "
                 f"REAL_ORDER_TIMEOUT={counts['REAL_ORDER_TIMEOUT']}")
    if counts["NEWS_SUPPRESSED"] > NEWS_SUPPRESSED_BUDGET:
        findings.append(Finding(
            "P2", "regime",
            f"{name}: news gate swallowed {counts['NEWS_SUPPRESSED']} bars in 24h",
            path))
    if counts["REAL_ORDER_TIMEOUT"]:
        findings.append(Finding(
            "P2", "regime",
            f"{name}: {counts['REAL_ORDER_TIMEOUT']} REAL_ORDER_TIMEOUT rows in "
            f"24h — orders sent without a fill confirmation",
            path))


def check_regime(now: datetime, base_dir: str):
    """``(findings, report_lines)`` for every regime-switching bot."""
    findings = []
    lines = []
    sl = _switch_logic()

    last_completed = sl.last_completed_night(now) if sl else None
    lines.append(f"now: {now.strftime('%Y-%m-%d %H:%M:%S')} TPE")
    lines.append(f"last completed night: "
                 f"{last_completed.key if last_completed else 'unknown'}")
    if sl is None:
        lines.append("WARNING: src.regime.switch_logic unavailable — "
                     "session-key checks skipped")
    lines.append("")

    dirs = [d for d in discover_bot_dirs(base_dir)
            if os.path.isfile(os.path.join(d, "regime_state.json"))]
    if not dirs:
        lines.append(f"no regime bots under {base_dir}")
        return findings, lines

    for bot_dir in dirs:
        name = bot_name(bot_dir)
        lines.append(f"--- {name}")
        # Findings are collected per bot so a retired directory (no
        # activity since the last night opened) can be demoted wholesale:
        # a stopped bot cannot classify, apply, or expire anything, so
        # its frozen state is archaeology, not an incident.
        active = _was_running(bot_dir, last_completed)
        if not active:
            lines.append("  (stopped/retired — no activity since "
                         f"{last_completed.key if last_completed else '?'} "
                         "opened; findings demoted to P3)")
        findings_before = len(findings)
        state = read_json(os.path.join(bot_dir, "regime_state.json"))
        if not isinstance(state, dict):
            findings.append(Finding(
                "P1", "regime",
                f"{name}: regime_state.json is corrupt or unreadable — the "
                f"brain cannot restore its regime across a restart",
                os.path.join(bot_dir, "regime_state.json")))
            lines.append("  state: CORRUPT")
        else:
            _check_state(now, bot_dir, name, state, sl, lines, findings)
        _check_history(now, bot_dir, name, lines, findings)
        _check_news_block(now, bot_dir, name, sl, lines, findings)
        _check_decisions(now, bot_dir, name, lines, findings)
        if not active:
            for i in range(findings_before, len(findings)):
                f = findings[i]
                if f.level != "P3":
                    findings[i] = Finding(
                        "P3", f.area, f"[stopped bot] {f.message}", f.path)
        lines.append("")

    return findings, lines


def main() -> int:
    guard_stdout()
    from scripts.monitor.common import now_tpe
    base_dir = sys.argv[1] if len(sys.argv) > 1 else default_base_dir()
    findings, lines = check_regime(now_tpe(), base_dir)
    print_report("regime health", lines, findings)
    return 0


if __name__ == "__main__":
    sys.exit(main())
