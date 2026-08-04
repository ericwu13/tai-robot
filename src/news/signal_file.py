"""新聞斷路器訊號 News circuit-breaker signal reader (Phase 1 foundations).

An external n8n workflow drops a small JSON file when either the
cross-market monitor threshold-fires or a human confirms via a Discord
tap.  The bot polls it (30s cadence in Phase 2) and acts on it exactly
once:

- ``risk_off``     — Tier 1: flatten and suppress entries
- ``deploy_short`` / ``deploy_long`` — Tier 2: deploy a forced-entry
  event strategy (see ``src.strategy.examples.news_event``)
- ``clear``        — cancel the suppression

The file is UNTRUSTED input, and acting on a stale one is the expensive
failure (a 40-minute-old "risk_off" describes a move that already
happened).  :func:`read_signal` therefore never raises and rejects
anything it cannot fully validate, returning the reason so the caller
can log it.

:class:`SignalLedger` gives exactly-once semantics across both the 30s
poll loop and process restarts.

File schema::

    {
      "version": 1,
      "signal_id": "uuid-string",
      "issued_at": "2026-08-04T22:05:00+08:00",
      "action": "risk_off",
      "severity": "high",
      "source": "crossmarket-sox",
      "reason": "SOX -3.2% intraday"
    }
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

_TZ_TAIPEI = timezone(timedelta(hours=8))

SCHEMA_VERSION = 1

# Tier 1 = risk_off / clear, Tier 2 = the forced-entry deploys.
VALID_ACTIONS = ("risk_off", "deploy_short", "deploy_long", "clear")

# Tolerance for a signal stamped slightly in the future (n8n host clock
# drift). Anything further ahead is rejected — a future stamp would
# otherwise keep a signal "fresh" indefinitely.
CLOCK_SKEW_SEC = 120

# Most recent consumed signal_ids kept on disk (FIFO).
LEDGER_MAX_IDS = 200


@dataclass
class NewsSignal:
    """A validated circuit-breaker signal (已驗證的斷路器訊號)."""

    signal_id: str
    action: str
    issued_at: datetime          # timezone-aware
    severity: str = ""
    source: str = ""
    reason: str = ""


def _to_taipei(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(_TZ_TAIPEI)
    if now.tzinfo is None:
        return now.replace(tzinfo=_TZ_TAIPEI)
    return now


def read_signal(
    path: str | os.PathLike | None,
    now: datetime | None = None,
    max_age_sec: int = 900,
) -> tuple[NewsSignal | None, str]:
    """Read and validate the signal file. ``(signal, reject_reason)``.

    Never raises.  A signal is returned only when every field validates:

    - file exists and is well-formed JSON with ``version == 1``
    - ``signal_id`` is a non-empty string
    - ``action`` is one of :data:`VALID_ACTIONS`
    - ``issued_at`` parses AND carries a timezone.  A naive stamp is
      REJECTED rather than assumed local: n8n runs on its own host, and
      guessing wrong turns an 8-hour-old signal into a fresh one.
    - freshness: strictly younger than *max_age_sec* (a signal exactly
      ``max_age_sec`` old is already rejected) and no more than
      :data:`CLOCK_SKEW_SEC` in the future.

    Comparison is done in UTC, so any offset in ``issued_at`` works.
    """
    if not path:
        return None, "no signal_path configured"
    path = str(path)
    if not os.path.exists(path):
        return None, f"signal file not found: {path}"

    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        return None, f"malformed signal file: {e}"

    if not isinstance(data, dict):
        return None, "signal file root is not a JSON object"

    version = data.get("version")
    if version != SCHEMA_VERSION:
        return None, f"unsupported signal schema version: {version!r}"

    signal_id = data.get("signal_id")
    if not isinstance(signal_id, str) or not signal_id.strip():
        return None, "missing signal_id"

    action = data.get("action")
    if not isinstance(action, str) or action not in VALID_ACTIONS:
        return None, f"unknown action: {action!r}"

    issued_raw = data.get("issued_at")
    if not isinstance(issued_raw, str):
        return None, "missing issued_at"
    try:
        issued_at = datetime.fromisoformat(issued_raw)
    except ValueError:
        return None, f"unparsable issued_at: {issued_raw!r}"
    if issued_at.tzinfo is None:
        return None, f"issued_at has no timezone: {issued_raw!r}"

    now_utc = _to_taipei(now).astimezone(timezone.utc)
    age = (now_utc - issued_at.astimezone(timezone.utc)).total_seconds()
    if age >= max_age_sec:
        return None, f"stale signal: {age:.0f}s old (max {max_age_sec}s)"
    if age < -CLOCK_SKEW_SEC:
        return None, f"issued_at is {-age:.0f}s in the future"

    return NewsSignal(
        signal_id=signal_id.strip(),
        action=action,
        issued_at=issued_at,
        severity=str(data.get("severity") or ""),
        source=str(data.get("source") or ""),
        reason=str(data.get("reason") or ""),
    ), ""


class SignalLedger:
    """Exactly-once bookkeeping for consumed ``signal_id``s.

    The signal file stays on disk after the bot acts on it, and the poll
    loop re-reads it every 30s — without this ledger a single ``risk_off``
    would flatten the book on every poll for its whole freshness window,
    and again after a restart.

    Persisted as a small JSON file written atomically (tmp + fsync +
    ``os.replace``), mirroring ``src.regime.store.save_state``, so a crash
    mid-write can never leave a truncated ledger that would let a
    already-acted-on signal fire a second time.  Capped at the most
    recent :data:`LEDGER_MAX_IDS` ids (FIFO) so it cannot grow forever.
    """

    def __init__(self, path: str | os.PathLike, max_ids: int = LEDGER_MAX_IDS):
        self.path = str(path)
        self.max_ids = max_ids
        self._ids: list[str] = []
        self._seen: set[str] = set()
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            logger.warning("[NEWS] Corrupted signal ledger %s — starting empty",
                           self.path)
            return
        raw = data.get("consumed") if isinstance(data, dict) else None
        if not isinstance(raw, list):
            return
        self._ids = [s for s in raw if isinstance(s, str)][-self.max_ids:]
        self._seen = set(self._ids)

    def is_consumed(self, signal_id: str) -> bool:
        return signal_id in self._seen

    def mark_consumed(self, signal_id: str) -> None:
        """Record *signal_id* as acted on. Idempotent — a repeat is a no-op."""
        if not signal_id or signal_id in self._seen:
            return
        self._ids.append(signal_id)
        self._seen.add(signal_id)
        if len(self._ids) > self.max_ids:
            dropped = self._ids[:-self.max_ids]
            self._ids = self._ids[-self.max_ids:]
            self._seen.difference_update(dropped)
        self._save()

    def _save(self) -> None:
        payload = {"version": SCHEMA_VERSION, "consumed": self._ids}
        tmp = self.path + ".tmp"
        try:
            parent = os.path.dirname(self.path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except OSError as e:
            logger.warning("[NEWS] Could not persist signal ledger %s: %s",
                           self.path, e)
