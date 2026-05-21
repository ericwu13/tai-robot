"""Tick watchdog: detects stale connections and session transitions.

Separated from the GUI to be independently testable. The GUI calls
check() every 30s and acts on the returned action.
"""

from __future__ import annotations

import time
from datetime import datetime

from .live_runner import is_market_open, minutes_until_session_close, _TZ_TAIPEI


class TickWatchdog:
    """Monitors tick freshness and decides when to resubscribe or reconnect.

    Usage::

        wd = TickWatchdog()
        wd.active = True
        # On each tick:
        wd.on_tick()
        # Every 30s:
        action = wd.check()
        # action is one of: None, "warn", "resubscribe", "reconnect",
        #                    "session_resubscribe"
    """

    # Thresholds (seconds)
    WARN_TIMEOUT = 120           # 2 min: start warning
    RESUBSCRIBE_TIMEOUT = 300    # 5 min: re-subscribe ticks
    RECONNECT_TIMEOUT = 600      # 10 min: full reconnect (re-login)
    RESUBSCRIBE_COOLDOWN = 180   # 3 min: don't retry resubscribe within this window
    NEAR_CLOSE_SUPPRESS = 10     # suppress within 10 min of session close

    def __init__(self):
        self.active: bool = False
        self.last_tick_time: float = 0.0
        self.grace_until: float = 0.0   # suppress warnings until this time
        self.last_resubscribe: float = 0.0  # for cooldown
        # Order symbol (e.g. "TM0000") — used so minutes_until_session_close
        # applies the settlement-day 13:30 close for front-month contracts.
        # Without this, the watchdog uses the default 13:45 close and warns
        # between 13:30 and 13:35 on settlement day (issue #66).
        self.order_symbol: str | None = None

    def on_tick(self) -> None:
        """Call when a real tick is received (live or history).

        Resets staleness + resubscribe cooldown — a real tick means the
        connection is healthy.
        """
        self.last_tick_time = time.time()
        self.last_resubscribe = 0.0

    def on_resubscribe(self) -> None:
        """Call when a resubscribe attempt was issued.

        Starts a cooldown so the watchdog doesn't loop-resubscribe every
        30s.  Does NOT touch last_tick_time — the quote server may not
        actually push ticks back (zombie session) and we need elapsed
        to keep climbing toward RECONNECT_TIMEOUT.
        """
        self.last_resubscribe = time.time()

    def set_grace(self, seconds: int = 30) -> None:
        """Set a grace period after reconnect/resubscribe."""
        self.grace_until = time.time() + seconds

    def reset(self) -> None:
        """Reset all state."""
        self.active = False
        self.last_tick_time = 0.0
        self.grace_until = 0.0
        self.last_resubscribe = 0.0
        self.order_symbol = None

    def check(self, now: float | None = None) -> str | None:
        """Check tick health and return the action needed.

        Args:
            now: Current time as time.time(). If None, uses time.time().
                 Exposed for testing.

        Returns:
            None — all OK, no action needed
            "warn" — no ticks for >2min, log a warning
            "resubscribe" — no ticks for >5min, try re-subscribing
            "reconnect" — no ticks for >10min, force full reconnect
            "session_resubscribe" — new session opened, resubscribe immediately
        """
        if not self.active or not self.last_tick_time:
            return None
        if not is_market_open():
            return None

        # Settlement-day awareness: for front-month contracts, the AM
        # session closes at 13:30 (not 13:45).  minutes_until_session_close
        # returns None when we're past the effective close for this
        # order_symbol even though the global is_market_open still says
        # True until 13:45.  Treat that window as closed — otherwise the
        # bot warns every 30s between 13:30 and 13:35 on settlement day
        # (issue #66).  Near-close suppression also uses the early close
        # so we don't warn during the last 10 min of real trading.
        mins_left = minutes_until_session_close(self.order_symbol)
        if mins_left is None:
            return None
        if mins_left <= self.NEAR_CLOSE_SUPPRESS:
            return None

        if now is None:
            now = time.time()

        # Grace period after reconnect
        if now < self.grace_until:
            return None

        # Session transition: last tick was during closed market.
        # The old subscription is stale — resubscribe immediately.
        last_dt = datetime.fromtimestamp(self.last_tick_time, tz=_TZ_TAIPEI)
        if not is_market_open(last_dt):
            return "session_resubscribe"

        # Normal staleness checks
        elapsed = now - self.last_tick_time
        if elapsed <= self.WARN_TIMEOUT:
            return None

        # Reconnect wins unconditionally once we hit the threshold —
        # cooldown does NOT suppress escalation.
        if elapsed > self.RECONNECT_TIMEOUT:
            return "reconnect"

        if elapsed > self.RESUBSCRIBE_TIMEOUT:
            # Suppress repeated resubscribes within the cooldown window
            # so elapsed can keep climbing toward RECONNECT_TIMEOUT.
            # Without this, a zombie COM session loop-resubscribes every
            # 30s and never escalates to a full re-login.
            if self.last_resubscribe and (now - self.last_resubscribe) < self.RESUBSCRIBE_COOLDOWN:
                return "warn"
            return "resubscribe"
        return "warn"

    def elapsed_minutes(self) -> int:
        """Return minutes since last tick (for log messages)."""
        if not self.last_tick_time:
            return 0
        return int((time.time() - self.last_tick_time) // 60)
