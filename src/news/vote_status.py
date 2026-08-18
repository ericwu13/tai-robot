"""Per-source regime-vote status for the GUI regime tab.

Pure data-shaping — no Tkinter. Reads the same on-disk artifacts the
bridges and the classifier use, WITHOUT consuming anything:

- pending votes: ``regime_vote_w2/w3/w4.json`` next to
  ``news.regime_vote_path`` (each self-labels its target session via
  ``expires_after_session`` — the only alignment source of truth)
- W2 liveness: ``monitor_state.json`` next to ``news.signal_path``
  (``last_check`` + ``last_result``, stamped every pass)
- W3 liveness: the rss scorer state file (``news.rss_state_file``);
  ``last_check`` + ``session_net``
- W4 liveness: ``.chips_state.json`` next to the vote base path
  (``last_check`` + ``last_result``; pre-upgrade files fall back to the
  newest OI history date)

Timeline rule: bridge state files supply ONLY liveness and context.
The vote of record is the vote FILE (pending) or the classification's
persisted ``_vote_sources`` (consumed) — never a bridge-side summary.

Relative state paths (e.g. the ``data/rss_scorer_state.json`` default)
resolve against the bridge's own cwd, which the GUI cannot know — a
missing file therefore reports "unknown", never "dead".
"""

from __future__ import annotations

import glob
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

_TZ_TAIPEI = timezone(timedelta(hours=8))

# Staleness thresholds per source, matched to each bridge's cadence.
# W2 polls repeatedly but only n8n knows its exact cron — one day
# catches "dead since yesterday" without false alarms outside US hours.
# W3 runs every ~30 min around the clock (weekend passes still stamp).
# W4 runs once per trade day — 72h spans a weekend + Monday holiday.
STALE_AFTER_H = {"W2": 24.0, "W3": 2.0, "W4": 72.0}

_ARROW = {"trending-up": "↑", "trending-down": "↓"}

SOURCE_LABELS = {
    "W2": "W2 跨市場 Cross-market",
    "W3": "W3 RSS 新聞 News",
    "W4": "W4 籌碼 Chips",
}


@dataclass
class SourceStatus:
    source: str                 # "W2" | "W3" | "W4"
    label: str = ""
    vote: str = ""              # pending direction valid for tonight
    vote_target: str = ""       # session key the pending file targets
    vote_expired: bool = False  # file exists but targets another session
    last_check: str = ""        # ISO timestamp of the bridge's last pass
    stale: bool = False         # last_check older than the source threshold
    known: bool = True          # False when no liveness file was found
    context: str = ""           # one-line summary from the bridge state


@dataclass
class VoteStatusReport:
    tonight_key: str
    sources: list = field(default_factory=list)   # list[SourceStatus]


def _read_json(path) -> dict | None:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def read_pending_votes(base_path: str | None, tonight_key: str = "") -> dict:
    """Best per-source vote file, keyed by upper-cased source ("W2"...).

    Read-only — never consumes. Malformed files are skipped. The key
    prefers the file's own ``source`` field (normalized to its "W\\d"
    stem, so "W3-manual" files count as W3) and falls back to the
    filename suffix.

    Several files can claim the same source (a mis-pathed manual run
    once left a ``regime_vote_w3_w3.json`` stray beside the real file).
    Per source, a file valid for *tonight_key* beats any expired one;
    among expired files the latest target wins — glob order must never
    decide, or a week-old stray shadows a live vote.
    """
    if not base_path:
        return {}
    stem, ext = os.path.splitext(str(base_path))
    votes = {}
    for fpath in glob.glob(f"{stem}_*{ext}"):
        data = _read_json(fpath)
        if not data or data.get("direction") not in ("trending-up", "trending-down"):
            continue
        source = str(data.get("source", "")).split("-")[0].upper()
        if not source:
            source = os.path.splitext(os.path.basename(fpath))[0].rsplit("_", 1)[-1].upper()
        prev = votes.get(source)
        if prev is not None:
            def rank(d):
                expires = str(d.get("expires_after_session", ""))
                return ((1 if tonight_key and expires == tonight_key else 0), expires)
            if rank(data) <= rank(prev):
                continue
        votes[source] = data
    return votes


def _parse_ts(value: str) -> datetime | None:
    try:
        ts = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=_TZ_TAIPEI)
    return ts


def _liveness(status: SourceStatus, state: dict | None, now: datetime) -> None:
    """Fill last_check/stale/known from a bridge state dict."""
    if state is None:
        status.known = False
        status.context = "狀態檔不存在 state file not found"
        return
    last_check = str(state.get("last_check") or "")
    status.last_check = last_check
    ts = _parse_ts(last_check)
    if ts is None:
        status.known = False
        status.context = "無 last_check unknown liveness"
        return
    if now.tzinfo is None:
        now = now.replace(tzinfo=_TZ_TAIPEI)
    age_h = (now - ts).total_seconds() / 3600.0
    status.stale = age_h > STALE_AFTER_H.get(status.source, 24.0)


def _w2_status(signal_path: str, now: datetime) -> SourceStatus:
    st = SourceStatus("W2", label=SOURCE_LABELS["W2"])
    state = None
    if signal_path:
        state = _read_json(os.path.join(os.path.dirname(str(signal_path)) or ".",
                                        "monitor_state.json"))
    _liveness(st, state, now)
    if state and not st.context:
        st.context = str(state.get("last_result") or "")
    return st


def _w3_status(rss_state_file: str, now: datetime) -> SourceStatus:
    st = SourceStatus("W3", label=SOURCE_LABELS["W3"])
    state = _read_json(rss_state_file) if rss_state_file else None
    _liveness(st, state, now)
    if state and not st.context:
        net = state.get("session_net")
        if isinstance(net, (int, float)):
            st.context = f"session net {net:+.2f}"
            peak = state.get("session_peak")
            if isinstance(peak, (int, float)) and peak != net:
                st.context += f" (peak {peak:+.2f})"
    return st


def _w4_status(regime_vote_path: str, now: datetime) -> SourceStatus:
    st = SourceStatus("W4", label=SOURCE_LABELS["W4"])
    state = None
    if regime_vote_path:
        state = _read_json(os.path.join(os.path.dirname(str(regime_vote_path)) or ".",
                                        ".chips_state.json"))
    if state is not None and "last_check" not in state:
        # Pre-upgrade sidecar (OI history only): best effort — the
        # newest recorded date is the last day TAIFEX data was fetched.
        history = state.get("history") or {}
        newest = max(history) if isinstance(history, dict) and history else ""
        st.known = False
        st.context = (f"最後資料 last data {newest}" if newest
                      else "無 last_check unknown liveness")
        return st
    _liveness(st, state, now)
    if state and not st.context:
        st.context = str(state.get("last_result") or "")
    return st


def collect_vote_status(
    regime_vote_path: str,
    signal_path: str,
    rss_state_file: str,
    tonight_key: str,
    now: datetime,
) -> VoteStatusReport:
    """Assemble per-source rows for the regime tab's vote section.

    *tonight_key* is the session key the NEXT classification will use
    (``"YYYY-MM-DD|NIGHT"``); pending votes are valid only when their
    ``expires_after_session`` matches it exactly.
    """
    pending = read_pending_votes(regime_vote_path, tonight_key)
    report = VoteStatusReport(tonight_key=tonight_key)
    for status in (_w2_status(signal_path, now),
                   _w3_status(rss_state_file, now),
                   _w4_status(regime_vote_path, now)):
        vote = pending.get(status.source)
        if vote:
            target = str(vote.get("expires_after_session", ""))
            status.vote_target = target
            if target == tonight_key:
                status.vote = str(vote.get("direction", ""))
            else:
                status.vote_expired = True
        report.sources.append(status)
    return report


def vote_chip(status: SourceStatus) -> str:
    """Short bilingual vote cell for one source row."""
    if status.vote:
        return f"{_ARROW.get(status.vote, '')} {status.vote}"
    if status.vote_expired:
        return f"過期 expired ({status.vote_target})"
    return "無投票 no vote"


def consumed_votes_line(last_features: dict | None) -> str:
    """One-line audit of the votes the LAST classification consumed,
    from regime_state.json's ``last_features`` (written by step()).

    Returns "" when the state predates vote-source persistence.
    """
    feat = last_features or {}
    sources = feat.get("_vote_sources")
    if sources is None:
        return ""
    if not sources:
        return "無投票 none"
    toks = []
    for item in sources:
        src, _, direction = str(item).partition(":")
        toks.append(f"{src}{_ARROW.get(direction, '')}" if direction else src)
    line = " + ".join(toks)
    if feat.get("_vote_accelerated"):
        line += " — 加速確認 accelerated confirm"
    return line
