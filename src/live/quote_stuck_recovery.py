"""One recovery attempt per open session while quote-stuck DEGRADED.

The link machine (#178) enters DEGRADED with reason ``stuck`` and then
only probes ``IsConnected``. When that read is 1 or 2 the quote link is
still up, so the recovery is the existing RequestTicks resubscribe, not
LeaveMonitor, EnterMonitor, login, or logout. This object decides once.
The host's RequestTicks helper may still retry a return code >= 3000
on its own.

The retry key is ``session_for``'s open-date key (``YYYY-MM-DD|DAY`` or
``YYYY-MM-DD|NIGHT``). It lives on this object, which the app holds for
the process. A closed session — clock gap, weekend, public holiday, or
a closures-file date — is ``None`` and never consumes the key. A second
stuck episode with the same key does not resubscribe. The next open
session does. Ticks after the attempt clear DEGRADED; if they do not
arrive the machine stays DEGRADED until manual Reconnect or a restart.
"""

from __future__ import annotations

import logging
from datetime import datetime

from .live_runner import session_for

logger = logging.getLogger(__name__)


class QuoteStuckRecovery:
    """Decides the one recovery attempt per session. Does not call the broker."""

    def __init__(self) -> None:
        self._attempted_key: str | None = None
        self._awaiting_ticks: bool = False
        self._logged_closed: bool = False
        self._logged_repeat: bool = False

    @property
    def attempted_key(self) -> str | None:
        """Session key of the last resubscribe, if any."""
        return self._attempted_key

    def consider(
        self,
        *,
        degraded: bool,
        reason: str,
        quote_state: int | None,
        now: datetime,
    ) -> str:
        """Return ``resubscribe`` or ``skip``.

        ``resubscribe`` is returned at most once per ``session_for`` key.
        """
        if not degraded or reason != "stuck" or quote_state not in (1, 2):
            return "skip"
        key = session_for(now)
        if key is None:
            if not self._logged_closed:
                self._logged_closed = True
                logger.info(
                    "[QUOTE-STUCK] no resubscribe: session closed "
                    "(IsConnected=%s)",
                    quote_state,
                )
            return "skip"
        self._logged_closed = False
        if key == self._attempted_key:
            if not self._logged_repeat:
                self._logged_repeat = True
                logger.info(
                    "[QUOTE-STUCK] no resubscribe: already attempted %s",
                    key,
                )
            return "skip"
        self._attempted_key = key
        self._logged_repeat = False
        self._awaiting_ticks = True
        logger.warning(
            "[QUOTE-STUCK] resubscribe once for %s (IsConnected=%s); "
            "no reconnect",
            key,
            quote_state,
        )
        return "resubscribe"

    def on_live_tick(self) -> bool:
        """A live tick after the one-shot. True means clear DEGRADED.

        History-replay ticks must not call this. A tick with no attempt
        outstanding returns False and does not change the key.
        """
        if not self._awaiting_ticks:
            return False
        self._awaiting_ticks = False
        logger.warning(
            "[QUOTE-STUCK] ticks resumed for %s; clearing DEGRADED",
            self._attempted_key,
        )
        return True
