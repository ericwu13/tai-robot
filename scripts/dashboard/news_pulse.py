"""市場脈動: the one-glance header of the 新聞訊號 view (``GET /api/news/pulse``).

Replaces what the W3 per-run Discord embed used to say (session net vs the
vote threshold, peak / erosion, recent-run momentum, scoring errors) and
adds what Discord never showed in one place: tonight's W2/W3/W4 votes and
bridge liveness, the next calendar events, and which articles are holding
the net up. The run-by-run chart series is ``news_trend`` (whole history).

Read-only and filter-independent (one cache entry for every open tab).
Bridge files go through the caller's ``read_json`` — the dashboard passes
its stat-gated share-delete reader so a bridge's bare ``os.replace`` never
meets an open handle here.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from scripts.dashboard import bridge_panels, news_feed

_TZ_TPE = timezone(timedelta(hours=8))

# Mirrors of the scorer's constants, used only if it cannot be imported
# (the dashboard must render even when the bridge code is broken).
_FALLBACK_THRESHOLD = 1.5
_FALLBACK_DECAY = 0.8409
_FALLBACK_EROSION = 1.5       # rss_scorer.EROSION_POINTS (the embed's "Eroding" rule)
DRIVERS_PER_SIDE = 3
EVENTS_SHOWN = 4


def _scorer():
    try:
        from scripts.news_bridge import rss_scorer
        return rss_scorer
    except Exception:  # noqa: BLE001 — advisory
        return None


clip = news_feed.clip          # the scorer's per-article clip (rss_scorer.ARTICLE_CLIP)


def _num(value, default=0.0) -> float:
    return news_feed._num(value, default)


# ── drivers: what is holding tonight's net up ────────────────────────────

def build_drivers(items: list[dict], session: str, decay: float,
                  *, clip_scores: bool = True) -> dict:
    """Each article's share of the session EMA today.

    The scorer does ``net = prev * decay + Σ clip(article) * run_scale``
    once per scoring run, so an article scored k runs ago now contributes
    ``clip(score) * run_scale * decay**k`` (or unclipped ``score * decay**k``
    for Pipeline v2 live rows).  Runs that logged nothing still decayed the
    EMA, so the sum is approximate.
    """
    rows = [r for r in items if r["session"] == session and r["direction"] != "neutral"]
    order = sorted({r["scored_at"] for r in items if r["session"] == session})
    later = {at: len(order) - 1 - i for i, at in enumerate(order)}
    out = []
    for r in rows:
        weight = decay ** later.get(r["scored_at"], 0)
        base = clip(r["score"]) * r.get("run_scale", 1.0) if clip_scores else float(r["score"])
        out.append({"title": r["title"], "link": r["link"], "source": r["source"],
                    "scored_at": r["scored_at"], "score": r["score"],
                    "reason": r["reason"], "weight": round(weight, 3),
                    "contribution": round(base * weight, 3)})
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

def _v2_thresholds(state: dict) -> tuple[float, float]:
    stats = state.get("pipeline_v2_last_stats")
    stats = stats if isinstance(stats, dict) else {}
    bear = _num(stats.get("bearish_vote_threshold"), 0.85) or 0.85
    bull = _num(stats.get("bullish_vote_threshold"), 0.85) or 0.85
    return bear, bull


def _v2_decay(state: dict) -> float:
    import math
    stats = state.get("pipeline_v2_last_stats")
    stats = stats if isinstance(stats, dict) else {}
    hl = _num(stats.get("ema_half_life_hours"), 10.0) or 10.0
    return math.exp(-math.log(2) * 0.5 / max(0.25, hl))


def w3_state(state: dict | None, now: datetime, tonight_session: str,
             threshold: float, erosion: float = _FALLBACK_EROSION) -> dict:
    if not isinstance(state, dict):
        return {"known": False}
    key = str(state.get("session_key") or "")
    current = bool(key) and key == tonight_session
    net = _num(state.get("session_net")) if current else 0.0
    peak = _num(state.get("session_peak")) if current else 0.0
    recent = [_num(x) for x in (state.get("recent_runs") or [])
              if isinstance(x, (int, float))] if current else []
    eroding = ((peak > 0 and net < peak - erosion)
               or (peak < 0 and net > peak + erosion))
    streak = state.get("error_streak")
    streak = streak if isinstance(streak, int) and not isinstance(streak, bool) else 0
    if state.get("pipeline_v2_mode") == "live":
        bear_th, bull_th = _v2_thresholds(state)
        active_vote = news_feed._str(state.get("v2_active_vote"), 20) if current else ""
        if active_vote in ("trending-up", "trending-down"):
            direction = active_vote
        elif net >= bull_th:
            direction = "trending-up"
        elif net <= -bear_th:
            direction = "trending-down"
        else:
            direction = ""
        eff_threshold = bear_th if net < 0 else bull_th
    else:
        direction = ("trending-up" if net >= threshold
                     else "trending-down" if net <= -threshold else "")
        eff_threshold = threshold

    def stamp(name):
        # Keep the +08:00 offset: the page computes ages with Date.parse,
        # which reads an offset-less ISO string as the VIEWER's local time.
        return news_feed._str(state.get(name), 40)

    def count(name):
        v = state.get(name)
        return v if isinstance(v, int) and not isinstance(v, bool) else None

    return {
        "known": True,
        "session": key,
        "current": current,
        "session_net": round(net, 3),
        "session_peak": round(peak, 3),
        "to_threshold": round(eff_threshold - abs(net), 3),
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
        "partial": ([p for p in state.get("v2_partial") if isinstance(p, dict)][:10]
                    if current and isinstance(state.get("v2_partial"), list) else []),
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
    if not err and events and not upcoming:
        err = f"日曆 {len(events)} 筆事件皆已過期"
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
    erosion = float(getattr(scorer, "EROSION_POINTS", _FALLBACK_EROSION))
    paths = news_paths or {}
    session = scorer.night_session_key(now) if scorer else ""
    items = news_feed.read_items(news_log_path) if news_log_path else []
    state = read_json(paths["rss_state_file"]) if paths.get("rss_state_file") else None
    tonight, votes = _votes(now, paths, read_json) if paths else ("", [])
    w3 = w3_state(state, now, session, threshold, erosion)
    v2_live = isinstance(state, dict) and state.get("pipeline_v2_mode") == "live"
    if v2_live:
        bear_th, bull_th = _v2_thresholds(state)
        threshold = bear_th if w3.get("session_net", 0.0) < 0 else bull_th
        decay = _v2_decay(state)
    return {
        "available": bool(news_paths) or bool(news_log_path),
        "now": now.isoformat(timespec="seconds"),
        "threshold": threshold,
        "article_clip": news_feed.article_clip(),
        "session": session,
        "weekend_pause": bool(scorer and scorer.in_weekend_pause(now)),
        "w3": w3,
        "tonight": tonight,
        "votes": votes,
        "events": _events(now, paths.get("events_path", ""), read_json),
        "drivers": build_drivers(items, session, decay, clip_scores=not v2_live),
        "w2": bridge_panels.crossmarket(now, paths, read_json),
        "w4": bridge_panels.chips(now, paths, read_json),
    }
