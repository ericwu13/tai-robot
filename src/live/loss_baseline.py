"""One daily-loss rule for a REAL bot.

During the night session (15:00 to 05:00 TPE), if this session has a valid 15:00 snapshot, L = net_now - snap_net. Otherwise, and at all other times, L = the full account net, exactly as master. A snapshot is valid only if it was read in the closed window 13:45 to 15:00 TPE of that trading day, by this process or from a same-key file.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

from src.live.main_heartbeat import atomic_write_json
from src.live.trading_guard import trading_day_key
from src.market_data.holidays import holiday_calendar_health, is_taifex_holiday

logger = logging.getLogger(__name__)

_TPE = timezone(timedelta(hours=8))
_VERSION = 3
_WINDOW_START = time(13, 45)
_WINDOW_END = time(15, 0)
_NIGHT_OPEN = time(15, 0)
_NIGHT_CLOSE = time(5, 0)


def _as_tpe(now: datetime) -> datetime:
    if now.tzinfo is None:
        return now.replace(tzinfo=_TPE)
    return now.astimezone(_TPE)


def _clock(now: datetime) -> time:
    local = _as_tpe(now)
    return local.timetz().replace(tzinfo=None)


def _in_window(now: datetime) -> bool:
    """Closed afternoon window [13:45, 15:00] TPE."""
    clock = _clock(now)
    return _WINDOW_START <= clock <= _WINDOW_END


# Known, accepted (Eric, 2026-11-15): from 05:00 until the broker's ~05:22-05:36 reset, L is the full account net,
# so a pause latched then carries into the next day session. This matches master; tracked in #306.
def _in_night(now: datetime) -> bool:
    """Night session [15:00, 05:00). 05:00 itself is the full account net."""
    clock = _clock(now)
    return clock >= _NIGHT_OPEN or clock < _NIGHT_CLOSE


def _as_int(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _known_trading_day(day: date) -> bool:
    """True only when the calendar says this weekday is a TAIFEX session."""
    if day.weekday() >= 5:
        return False
    ok, _detail = holiday_calendar_health(day.year)
    if not ok:
        return False
    try:
        return not is_taifex_holiday(day)
    except Exception:
        return False


def _net_of(rights):
    """Account net (f11 - f2 - f3 + f1), or None when the reply is unusable."""
    if not isinstance(rights, dict) or rights.get("valid") is False:
        return None
    try:
        realized = int(float(rights.get("realized_pnl", "0") or 0))
        fee = int(float(rights.get("realized_cost", "0") or 0))
        tax = int(float(rights.get("tax", "0") or 0))
        floating = int(float(rights.get("float_pnl", "0") or 0))
    except (TypeError, ValueError):
        return None
    return realized - fee - tax + floating


def _parse_read_at(value) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return _as_tpe(parsed)


@dataclass(frozen=True)
class LossView:
    account_net: int
    loss_today: int
    day_key: str
    basis: str


class DailyLossBaseline:
    """Per-bot night snapshot. Two REAL bots on one account are deferred."""

    def __init__(self, path: str, account: str):
        self.path = path
        self._account_hash = hashlib.sha256(
            str(account or "").encode("utf-8")).hexdigest()[:12]
        self.key = ""
        self.snap_net: int | None = None
        self.snap_read: datetime | None = None
        self._account_net = 0

    def observe(self, rights: dict, now_tpe: datetime) -> LossView:
        """One rights read. An unusable reply does not move state."""
        net = _net_of(rights)
        if net is None or now_tpe is None:
            return self._view(self._account_net, self._account_net, "master", self.key)
        now = _as_tpe(now_tpe)
        self._take_snapshot(net, now)
        self._account_net = net
        if self._snapshot_applies(now):
            loss = net - int(self.snap_net)
            basis = "snapshot"
        else:
            loss = net
            basis = "master"
        self._persist()
        return self._view(loss, net, basis, trading_day_key(now))

    def load(self, now_tpe: datetime) -> None:
        """Restore this night's snapshot when the file shows its read time."""
        now = _as_tpe(now_tpe) if now_tpe is not None else _as_tpe(datetime.now(_TPE))
        if not self.path or not os.path.isfile(self.path):
            return
        try:
            with open(self.path, encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError, UnicodeError, ValueError):
            return
        if not isinstance(data, dict):
            return
        if data.get("account") != self._account_hash:
            return
        key = str(data.get("key") or "")
        if key != trading_day_key(now):
            return
        read_at = _parse_read_at(data.get("read_at"))
        snap = _as_int(data.get("snap_net"))
        if not self._window_read(key, read_at) or snap is None:
            return
        self.key = key
        self.snap_net = snap
        self.snap_read = read_at

    def save(self) -> None:
        self._persist()

    def _take_snapshot(self, net: int, now: datetime) -> None:
        """Last read in [13:45, 15:00] of a trading day is that night's snapshot."""
        if not _in_window(now) or not _known_trading_day(now.date()):
            return
        self.key = now.date().isoformat()
        self.snap_net = net
        self.snap_read = now

    def _window_read(self, key: str, read_at: datetime | None) -> bool:
        if read_at is None or read_at.date().isoformat() != key:
            return False
        if not _in_window(read_at):
            return False
        try:
            day = date.fromisoformat(key)
        except ValueError:
            return False
        return _known_trading_day(day)

    def _snapshot_applies(self, now: datetime) -> bool:
        if not _in_night(now) or self.snap_net is None:
            return False
        return self._window_read(self.key, self.snap_read) and self.key == trading_day_key(now)

    def _persist(self) -> None:
        if not self.path or self.snap_read is None or self.snap_net is None:
            return
        payload = {
            "version": _VERSION,
            "account": self._account_hash,
            "key": self.key,
            "snap_net": self.snap_net,
            "read_at": self.snap_read.isoformat(),
        }
        try:
            atomic_write_json(self.path, payload)
        except Exception:
            logger.warning("[LOSS] loss_baseline.json write failed", exc_info=True)

    def _view(self, loss: int, account: int, basis: str, day_key: str) -> LossView:
        return LossView(
            account_net=int(account),
            loss_today=int(loss),
            day_key=day_key,
            basis=basis,
        )
