"""Regime-vote sidecar file — cross-market confirmation acceleration.

An external producer (the W2 cross-market monitor) writes a small JSON
file when the semiconductor complex moves sharply in one direction.  At
classification time (04:58) the regime state machine reads this vote and,
if it agrees with the raw technical classification, skips the normal
hysteresis confirmation delay (2 nights → 1 night + vote).

Separate from ``signal_file.py`` (which controls circuit-breaker
position overrides).  This file controls regime confirmation acceleration
only.

File schema::

    {
      "version": 1,
      "direction": "trending-up",
      "expires_after_session": "2026-08-05|NIGHT",
      "source": "W2"
    }
"""

from __future__ import annotations

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


def write_regime_vote(
    path: str | os.PathLike,
    direction: str,
    expires_after_session: str,
    source: str = "W2",
) -> None:
    """Write (or overwrite) the vote file atomically."""
    payload = {
        "version": SCHEMA_VERSION,
        "direction": direction,
        "expires_after_session": expires_after_session,
        "source": source,
    }
    out = str(path)
    parent = os.path.dirname(out)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    os.replace(tmp, out)


def consume_regime_vote(path: str | os.PathLike | None) -> None:
    """Delete the vote file after it has been consumed (either way)."""
    if not path:
        return
    try:
        os.remove(str(path))
    except FileNotFoundError:
        pass
    except OSError as exc:
        logger.warning("[REGIME-VOTE] could not remove vote file: %s", exc)
