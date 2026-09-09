"""Regime-vote sidecar files — cross-market confirmation acceleration.

External producers (W2 cross-market monitor, W3 RSS scorer, W4 chips
monitor) each write their own vote file.  At classification time (04:58) the regime state
machine reads all vote files and, if any vote agrees with the raw
technical classification, skips the normal hysteresis confirmation delay
(2 nights → 1 night + vote).

Each source writes to a per-source file derived from the base path::

    regime_vote_path = "C:/n8n-bridge/regime_vote.json"
    W2 → "C:/n8n-bridge/regime_vote_w2.json"
    W3 → "C:/n8n-bridge/regime_vote_w3.json"
    W4 → "C:/n8n-bridge/regime_vote_w4.json"

File schema (schema version stays 1 — ``fired_at`` is additive and
readers ignore unknown keys)::

    {
      "version": 1,
      "direction": "trending-up",
      "expires_after_session": "2026-08-05|NIGHT",
      "source": "W2",
      "fired_at": "2026-08-05T22:31:07+08:00"
    }

``fired_at`` exists because a vote's edge has a half-life.  W2 fires
during the US session but the nightly lane only reads it at the 04:58
classification, up to ~20 h later, and over that lag W2 was right 3
times in 10 — its edge lives in the ~4 h after the fire.  ``age_sec``
(computed at read time, from ``fired_at`` when parseable and otherwise
from the file mtime) lets the consumer apply a per-source age limit.
"""

from __future__ import annotations

import glob
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
VALID_DIRECTIONS = ("trending-up", "trending-down")

_TZ_TAIPEI = timezone(timedelta(hours=8))


@dataclass
class RegimeVote:
    direction: str
    expires_after_session: str
    source: str = ""
    fired_at: str = ""            # ISO-8601 +08:00, stamped by the writer
    age_sec: float | None = None  # computed at read time; None = unknowable


def _now_tpe() -> datetime:
    return datetime.now(_TZ_TAIPEI)


def vote_age_sec(fired_at: str, path: str, now: datetime | None = None) -> float | None:
    """Age of a vote in seconds.

    Prefers the writer's own ``fired_at`` stamp; falls back to the file's
    mtime for pre-``fired_at`` files (and for a stamp we cannot parse).
    Returns None when neither is available.  Never negative — a bridge
    host whose clock runs a few seconds fast must not read as "from the
    future" and skew an age gate.
    """
    now = now or _now_tpe()
    if now.tzinfo is None:
        now = now.replace(tzinfo=_TZ_TAIPEI)
    if fired_at:
        try:
            ts = datetime.fromisoformat(str(fired_at))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=_TZ_TAIPEI)
            return max(0.0, (now - ts).total_seconds())
        except (TypeError, ValueError):
            logger.debug("[REGIME-VOTE] unparseable fired_at %r — using mtime", fired_at)
    try:
        return max(0.0, now.timestamp() - os.path.getmtime(path))
    except OSError:
        return None


def read_regime_vote(
    path: str | os.PathLike | None,
    current_session_key: str,
) -> RegimeVote | None:
    """Read and validate the vote file.  Returns None on any problem.

    *current_session_key* is the ``"YYYY-MM-DD|NIGHT"`` key of the
    session being classified.  The vote is valid only when its
    ``expires_after_session`` matches that key — an older vote is expired
    and silently ignored.
    """
    if not path:
        return None
    path = str(path)
    if not os.path.exists(path):
        return None

    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as exc:
        logger.warning("[REGIME-VOTE] malformed vote file: %s", exc)
        return None

    if not isinstance(data, dict):
        logger.warning("[REGIME-VOTE] vote file root is not a JSON object")
        return None

    if data.get("version") != SCHEMA_VERSION:
        logger.warning("[REGIME-VOTE] unsupported version: %r", data.get("version"))
        return None

    direction = data.get("direction")
    if direction not in VALID_DIRECTIONS:
        logger.warning("[REGIME-VOTE] invalid direction: %r", direction)
        return None

    expires = data.get("expires_after_session", "")
    if not isinstance(expires, str) or not expires:
        logger.warning("[REGIME-VOTE] missing expires_after_session")
        return None

    if expires != current_session_key:
        logger.debug(
            "[REGIME-VOTE] vote expired: vote=%r, current=%r",
            expires, current_session_key,
        )
        return None

    fired_at = str(data.get("fired_at", "") or "")
    return RegimeVote(
        direction=direction,
        expires_after_session=expires,
        source=str(data.get("source", "")),
        fired_at=fired_at,
        age_sec=vote_age_sec(fired_at, path),
    )


def _source_path(base_path: str, source: str) -> str:
    """Derive per-source vote file path from a base path.

    ``regime_vote.json`` + ``"W2"`` → ``regime_vote_w2.json``
    ``regime_vote.json`` + ``"W3-manual"`` → ``regime_vote_w3.json``
    """
    stem, ext = os.path.splitext(base_path)
    writer = source.split("-")[0].lower()
    return f"{stem}_{writer}{ext}"


def _vote_glob(base_path: str) -> str:
    """Glob pattern matching all per-source vote files."""
    stem, ext = os.path.splitext(base_path)
    return f"{stem}_*{ext}"


def write_regime_vote(
    path: str | os.PathLike,
    direction: str,
    expires_after_session: str,
    source: str = "W2",
    data_date: str = "",
) -> None:
    """Write (or overwrite) the per-source vote file atomically.

    Stamps ``fired_at`` (ISO-8601 with the +08:00 offset) so readers can
    age the vote out; see ``vote_age_sec``.

    *data_date* (``"YYYYMMDD"``) is the trade date of the data the vote
    was computed from — audit only, and omitted when empty.  A source
    may vote from data that lags the session it targets (W4 walks the
    TAIFEX OpenAPI back up to 3 trading days), so the target session key
    alone no longer identifies the evidence.  Readers ignore unknown
    keys.
    """
    payload = {
        "version": SCHEMA_VERSION,
        "direction": direction,
        "expires_after_session": expires_after_session,
        "source": source,
        # Wall-clock fire time (TPE offset).  The session key says WHICH
        # night the vote targets; this says HOW OLD the evidence is, which
        # is what the nightly lane's per-source age gate needs.
        "fired_at": _now_tpe().isoformat(timespec="seconds"),
    }
    if data_date:
        payload["data_date"] = data_date
    out = _source_path(str(path), source)
    parent = os.path.dirname(out)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    os.replace(tmp, out)


def read_all_regime_votes(
    base_path: str | os.PathLike | None,
    current_session_key: str,
) -> list[RegimeVote]:
    """Read all per-source vote files.  Returns valid, non-expired votes."""
    if not base_path:
        return []
    pattern = _vote_glob(str(base_path))
    votes = []
    for fpath in glob.glob(pattern):
        vote = read_regime_vote(fpath, current_session_key)
        if vote:
            votes.append(vote)
    return votes


def consume_all_regime_votes(base_path: str | os.PathLike | None) -> None:
    """Delete all per-source vote files after classification."""
    if not base_path:
        return
    pattern = _vote_glob(str(base_path))
    for fpath in glob.glob(pattern):
        try:
            os.remove(fpath)
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.warning("[REGIME-VOTE] could not remove %s: %s", fpath, exc)


def consume_regime_vote(path: str | os.PathLike | None) -> None:
    """Delete a single vote file after it has been consumed."""
    if not path:
        return
    try:
        os.remove(str(path))
    except FileNotFoundError:
        pass
    except OSError as exc:
        logger.warning("[REGIME-VOTE] could not remove vote file: %s", exc)
