"""Regime-vote sidecar files — cross-market confirmation acceleration.

External producers (W2 cross-market monitor, W3 RSS scorer) each write
their own vote file.  At classification time (04:58) the regime state
machine reads all vote files and, if any vote agrees with the raw
technical classification, skips the normal hysteresis confirmation delay
(2 nights → 1 night + vote).

Each source writes to a per-source file derived from the base path::

    regime_vote_path = "C:/n8n-bridge/regime_vote.json"
    W2 → "C:/n8n-bridge/regime_vote_w2.json"
    W3 → "C:/n8n-bridge/regime_vote_w3.json"

File schema (unchanged per file)::

    {
      "version": 1,
      "direction": "trending-up",
      "expires_after_session": "2026-08-05|NIGHT",
      "source": "W2"
    }
"""

from __future__ import annotations

import glob
import json
import logging
import os
from dataclasses import dataclass

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
VALID_DIRECTIONS = ("trending-up", "trending-down")


@dataclass
class RegimeVote:
    direction: str
    expires_after_session: str
    source: str = ""


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

    return RegimeVote(
        direction=direction,
        expires_after_session=expires,
        source=str(data.get("source", "")),
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
) -> None:
    """Write (or overwrite) the per-source vote file atomically."""
    payload = {
        "version": SCHEMA_VERSION,
        "direction": direction,
        "expires_after_session": expires_after_session,
        "source": source,
    }
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
