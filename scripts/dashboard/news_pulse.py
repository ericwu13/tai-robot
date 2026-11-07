"""市場脈動: the one-glance header of the 新聞訊號 view (``GET /api/news/pulse``).

Replaces what the W3 per-run Discord embed used to say (session net vs the
vote threshold, peak / erosion, recent-run momentum, scoring errors) and
adds what Discord never showed in one place: tonight's W2/W3/W4 votes and
bridge liveness, the next calendar events, the run-by-run sentiment
series for the chart, and which articles are holding the net up.

Read-only and filter-independent (one cache entry for every open tab).
Bridge files go through the caller's ``read_json`` — the dashboard passes
its stat-gated share-delete reader so a bridge's bare ``os.replace`` never
meets an open handle here.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from scripts.dashboard import news_feed

_TZ_TPE = timezone(timedelta(hours=8))

# Mirrors of the scorer's constants, used only if it cannot be imported
# (the dashboard must render even when the bridge code is broken).
_FALLBACK_THRESHOLD = 1.5
_FALLBACK_DECAY = 0.8409
ARTICLE_CLIP = 0.5            # rss_scorer.aggregate_scores clips each article
EROSION_POINTS = 1.5          # the embed's "Eroding" rule
RUN_HISTORY_DAYS = 3          # chart window
DRIVERS_PER_SIDE = 3
EVENTS_SHOWN = 4


def _scorer():
    try:
        from scripts.news_bridge import rss_scorer
        return rss_scorer
    except Exception:  # noqa: BLE001 — advisory
        return None


def clip(score: float) -> float:
    return max(-ARTICLE_CLIP, min(ARTICLE_CLIP, float(score)))


def _ts(value: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=_TZ_TPE)


def _chart_time(dt: datetime) -> int:
    """TPE wall clock encoded as UTC — the page's lightweight-charts convention."""
    return int(dt.astimezone(_TZ_TPE).replace(tzinfo=timezone.utc).timestamp())


def _num(value, default=0.0) -> float:
    return news_feed._num(value, default)


# ── runs: one point per scoring pass ─────────────────────────────────────

def build_runs(items: list[dict], now: datetime, days: int = RUN_HISTORY_DAYS) -> list[dict]:
    """Oldest-first scoring runs from news rows (all rows of a run share
    ``scored_at``, ``run_net`` and ``session_net``)."""
    cutoff = now - timedelta(days=days)
    runs: dict[tuple, dict] = {}
    for r in items:
        dt = _ts(r["scored_at"])
        if dt is None or dt < cutoff:
            continue
        key = (r["session"], r["scored_at"])
        run = runs.get(key)
        if run is None:
            run = runs[key] = {"t": _chart_time(dt), "scored_at": r["scored_at"],
                               "session": r["session"], "run_net": r["run_net"],
                               "session_net": r["session_net"], "n": 0,
                               "bullish": 0, "bearish": 0, "neutral": 0}
        run["n"] += 1
        run[r["direction"]] += 1
    return sorted(runs.values(), key=lambda x: x["scored_at"])


# ── drivers: what is holding tonight's net up ────────────────────────────

def build_drivers(items: list[dict], session: str, decay: float) -> dict:
    """Each article's share of the session EMA today.

    The scorer does ``net = prev * decay + Σ clip(article)`` once per
    scoring run, so an article scored k runs ago now contributes
    ``clip(score) * decay**k``.  Runs that logged nothing (all Gemini
    calls failed) still decayed the EMA, so the sum is approximate.
    """
    rows = [r for r in items if r["session"] == session and r["direction"] != "neutral"]
    order = sorted({r["scored_at"] for r in items if r["session"] == session})
    later = {at: len(order) - 1 - i for i, at in enumerate(order)}
    out = []
    for r in rows:
        weight = decay ** later.get(r["scored_at"], 0)
        out.append({"title": r["title"], "link": r["link"], "source": r["source"],
                    "scored_at": r["scored_at"], "score": r["score"],
                    "reason": r["reason"], "weight": round(weight, 3),
                    "contribution": round(clip(r["score"]) * weight, 3)})
    up = sorted((d for d in out if d["contribution"] > 0),
                key=lambda d: -d["contribution"])
    down = sorted((d for d in out if d["contribution"] < 0),
                  key=lambda d: d["contribution"])
    total = round(sum(d["contribution"] for d in out), 3)
    shown = up[:DRIVERS_PER_SIDE] + down[:DRIVERS_PER_SIDE]
    rest = round(total - sum(d["contribution"] for d in shown), 3)
    return {"session": session, "up": up[:DRIVERS_PER_SIDE],
            "down": down[:DRIVERS_PER_SIDE], "total": total, "rest": rest,
            "rest_count": len(out) - len(shown), "runs": len(order)}


# ── W3 state (what the Discord embed showed) ─────────────────────────────

def w3_state(state: dict | None, now: datetime, tonight_session: str,
             threshold: float) -> dict:
    if not isinstance(state, dict):
        return {"known": False}
    key = str(state.get("session_key") or "")
    current = bool(key) and key == tonight_session
    net = _num(state.get("session_net")) if current else 0.0
    peak = _num(state.get("session_peak")) if current else 0.0
    recent = [_num(x) for x in (state.get("recent_runs") or [])
              if isinstance(x, (int, float))] if current else []
    eroding = ((peak > 0 and net < peak - EROSION_POINTS)
               or (peak < 0 and net > peak + EROSION_POINTS))
    streak = state.get("error_streak")
    streak = streak if isinstance(streak, int) and not isinstance(streak, bool) else 0
    direction = ("trending-up" if net >= threshold
                 else "trending-down" if net <= -threshold else "")

    def stamp(name):
        return str(state.get(name) or "")[:19]

    def count(name):
        v = state.get(name)
        return v if isinstance(v, int) and not isinstance(v, bool) else None

    return {
        "known": True,
        "session": key,
        "current": current,
        "session_net": round(net, 3),
        "session_peak": round(peak, 3),
        "to_threshold": round(threshold - abs(net), 3),
        "direction": direction,
        "eroding": eroding,
        "recent_runs": [round(x, 3) for x in recent],
        "momentum": round(sum(recent), 3),
        "last_check": stamp("last_check"),
        "failing": streak > 0,
        "error_streak": streak,
        "last_error": news_feed._str(state.get("last_error"), 200) if streak else "",
        "last_error_at": stamp("last_error_at"),
        "last_success_at": stamp("last_success_at"),
        "last_fetched": count("last_fetched"),
        "last_new": count("last_new"),
        "last_scored": count("last_articles_scored"),
        "last_skipped": count("last_skipped"),
    }


# ── votes + events ───────────────────────────────────────────────────────

def _votes(now: datetime, paths: dict, read_json) -> tuple[str, list[dict]]:
    try:
        from scripts.dashboard.snapshot import redact_line
        from src.news.vote_status import collect_vote_status, vote_chip
        from src.regime.switch_logic import upcoming_night_session
    except Exception:  # noqa: BLE001
        return "", []
    upcoming = upcoming_night_session(now)
    tonight = upcoming.key if upcoming else ""
    report = collect_vote_status(paths.get("regime_vote_path", ""),
                                 paths.get("signal_path", ""),
                                 paths.get("rss_state_file", ""),
                                 tonight, now, read_json=read_json)
    return tonight, [{
        "source": st.source, "vote": st.vote, "vote_expired": st.vote_expired,
        "chip": vote_chip(st), "known": st.known, "stale": st.stale,
        "failing": st.failing, "error": redact_line(st.error),
        "last_check": st.last_check[:16].replace("T", " "),
        "context": redact_line(st.context),
    } for st in report.sources]


def _events(now: datetime, path: str, read_json) -> dict:
    if not path:
        return {"configured": False, "items": [], "error": ""}
    try:
        from src.news.event_calendar import parse_events
    except Exception:  # noqa: BLE001
        return {"configured": True, "items": [], "error": "calendar code unavailable"}
    data = read_json(path)
    if data is None:
        return {"configured": True, "items": [], "error": "events file missing"}
    events, err = parse_events(data)
    today = now.astimezone(_TZ_TPE).date().isoformat()
    upcoming = sorted((e for e in events if e.date >= today), key=lambda e: e.date)
    return {"configured": True, "error": err, "items": [
        {"date": e.date, "name": news_feed._str(e.name, 120),
         "severity": e.severity, "sessions": list(e.sessions)}
        for e in upcoming[:EVENTS_SHOWN]]}


# ── payload ──────────────────────────────────────────────────────────────

def pulse(now: datetime, news_log_path: str | None, news_paths: dict | None,
          read_json) -> dict:
    scorer = _scorer()
    threshold = float(getattr(scorer, "SESSION_VOTE_THRESHOLD", _FALLBACK_THRESHOLD))
    decay = float(getattr(scorer, "EMA_DECAY", _FALLBACK_DECAY))
    paths = news_paths or {}
    session = scorer.night_session_key(now) if scorer else ""
    items = news_feed.read_items(news_log_path) if news_log_path else []
    state = read_json(paths["rss_state_file"]) if paths.get("rss_state_file") else None
    tonight, votes = _votes(now, paths, read_json) if paths else ("", [])
    return {
        "available": bool(news_paths) or bool(news_log_path),
        "now": now.isoformat(timespec="seconds"),
        "threshold": threshold,
        "article_clip": ARTICLE_CLIP,
        "session": session,
        "weekend_pause": bool(scorer and scorer.in_weekend_pause(now)),
        "w3": w3_state(state, now, session, threshold),
        "tonight": tonight,
        "votes": votes,
        "events": _events(now, paths.get("events_path", ""), read_json),
        "runs": build_runs(items, now),
        "drivers": build_drivers(items, session, decay),
    }
