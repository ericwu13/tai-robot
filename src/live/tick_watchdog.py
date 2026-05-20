"""Tick watchdog: detects stale connections and session transitions.

Separated from the GUI to be independently testable. The GUI calls
check() every 30s and acts on the returned action.
"""

from __future__ import annotations

import time
from datetime import datetime

from .live_runner import is_market_open, minutes_until_session_close, _TZ_TAIPEI


def _crossed_session_gap(last_ts: float, now_ts: float) -> bool:
    """Return True if a market-closed period exists between last_ts and now_ts.

    Used to distinguish session boundary crossings from same-session
    zombie scenarios.  Both:

      * last tick during closed market (e.g. subscription auto-tick at 14:50
        during the 13:45-15:00 gap), and
      * last tick during prior session (e.g. last AM tick at 13:28, check at
        15:00 PM open — bot 0422 case from issue #66)

    are session-boundary scenarios that warrant a soft resubscribe rather
    than a 10-minute-elapsed reconnect.
    """
    if now_ts <= last_ts:
        return False
    last_dt = datetime.fromtimestamp(last_ts, tz=_TZ_TAIPEI)
    # If last tick was itself during closed market, definitely a gap
    if not is_market_open(last_dt):
        return True
    # Walk the interval in 5-min steps looking for a closed sample.
    # Real session gaps are >= 75 min (13:45-15:00) or >= 225 min (05:00-08:45),
    # so 5-min stride catches them with tens of iterations max.
    step = 300
    t = last_ts + step
    while t < now_ts:
        if not is_market_open(datetime.fromtimestamp(t, tz=_TZ_TAIPEI)):
            return True
        t += step
    return False


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

    def on_session_resubscribe(self) -> None:
        """Call after issuing a session-transition resubscribe.

        Unlike on_resubscribe(), this DOES advance last_tick_time to
        wall-clock now.  Rationale: the session_resubscribe trigger
        fires when a closed-market gap exists between last_tick_time
        and now — e.g. a subscription-time auto-tick at 14:50 during
        the 13:45-15:00 gap, OR a normal last AM tick at 13:28 with
        check at 15:00 PM open.  In either case the elapsed reading
        across the gap is meaningless and the trigger condition would
        keep firing on every 30s check.

        Advancing last_tick_time restarts the normal staleness clock.
        If ticks really aren't arriving (zombie session), the regular
        warn→resubscribe→reconnect escalation kicks in after WARN_TIMEOUT.
        Real ticks will reset state via on_tick() as usual.
        """
        now = time.time()
        self.last_tick_time = now
        self.last_resubscribe = now

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

        # Session boundary: a closed-market period exists between last
        # tick and now (last tick during closed gap, or last tick in a
        # prior session).  Prefer a soft resubscribe over a heavy
        # reconnect — the 90+ minute elapsed reading is a measurement
        # artifact of the gap, not a real zombie session.
        #
        # RESUBSCRIBE_COOLDOWN prevents the 30s-check loop; the GUI
        # handler should also call on_session_resubscribe() to advance
        # last_tick_time so subsequent checks see a fresh clock.  If
        # ticks still don't arrive, normal warn → resubscribe →
        # reconnect escalation takes over within minutes.
        if _crossed_session_gap(self.last_tick_time, now):
            in_cooldown = (self.last_resubscribe
                           and (now - self.last_resubscribe) < self.RESUBSCRIBE_COOLDOWN)
            if not in_cooldown:
                return "session_resubscribe"
            # In cooldown: stay quiet rather than escalating to reconnect
            # on a stale elapsed reading that spans the closed gap.
            return None

        # Normal staleness checks (same-session zombie)
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
