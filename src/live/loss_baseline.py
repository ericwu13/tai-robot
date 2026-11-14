"""One daily-loss rule for a REAL bot.

1. A 15:00 TPE snapshot is valid for the key only if this process saw the roll (a read in 14:50-15:00 AND the roll read before 15:10, on a known trading day), or a same-key file already holds it. snapshot = the last read before 15:00
2. If this process also saw the close (a read in 04:50-05:00), hold L through 05:00-08:30. At 08:30, if f22 moved, re-anchor snap_net = latest non-zero net - held L once, and persist it as decided. A decided file is never re-anchored.
3. Otherwise L = full account net (master) for the rest of the key.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from src.live.main_heartbeat import atomic_write_json
from src.live.trading_guard import trading_day_key
from src.market_data.holidays import holiday_calendar_health, is_taifex_holiday

logger = logging.getLogger(__name__)

_TPE = timezone(timedelta(hours=8))
_VERSION = 2
_CLOSE_FROM = 4 * 60 + 50
_HOLD_FROM = 5 * 60
_DECIDE_AT = 8 * 60 + 30
_PRE_FROM = 14 * 60 + 50
_ROLL = 15 * 60
_ROLL_UNTIL = 15 * 60 + 10


def _as_tpe(now: datetime) -> datetime:
    if now.tzinfo is None:
        return now.replace(tzinfo=_TPE)
    return now.astimezone(_TPE)


def _minutes(now: datetime) -> int:
    local = _as_tpe(now)
    return local.hour * 60 + local.minute


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


def _is_zero_row(rights, net: int) -> bool:
    """Capital's all-zero batch row: net, realized P&L and fees are all 0."""
    try:
        realized = int(float(rights.get("realized_pnl", "0") or 0))
        cost = int(float(rights.get("realized_cost", "0") or 0))
    except (TypeError, ValueError):
        return False
    return net == 0 and realized == 0 and cost == 0


def _net_and_f22(rights):
    """Account net (f11 - f2 - f3 + f1) and f22, or None."""
    if not isinstance(rights, dict) or rights.get("valid") is False:
        return None
    try:
        realized = int(float(rights.get("realized_pnl", "0") or 0))
        fee = int(float(rights.get("realized_cost", "0") or 0))
        tax = int(float(rights.get("tax", "0") or 0))
        floating = int(float(rights.get("float_pnl", "0") or 0))
    except (TypeError, ValueError):
        return None
    if "start_equity" in rights:
        equity = str(rights.get("start_equity") or "").strip()
    elif "f22" in rights:
        equity = str(rights.get("f22") or "").strip()
    else:
        equity = ""
    return realized - fee - tax + floating, equity


@dataclass(frozen=True)
class LossView:
    account_net: int
    loss_today: int
    day_key: str
    basis: str
    held: bool
    settled_after_reset: bool
    day_budget_opened: bool = False
    day_failsafe_recheck: bool = False


class DailyLossBaseline:
    """Per-bot 15:00 snapshot. Two REAL bots on one account are deferred."""

    def __init__(self, path: str, account: str):
        self.path = path
        self._account_hash = hashlib.sha256(
            str(account or "").encode("utf-8")).hexdigest()[:12]
        self.key = ""
        self.snap_net: int | None = None
        self.snap_f22 = ""
        self.anchored = False
        self.last_L: int | None = None
        self.pause_failsafe = False
        self._account_net = 0
        # Previous successful read in this process. Not stored in the file.
        self._prev_read: datetime | None = None
        # Closed-window memory for this key. Not stored.
        self._master_rest = False
        self._decided = False
        self._nz_net: int | None = None
        self._nz_f22 = ""
        # True after a read in [04:50, 05:00) on this key, in this process.
        self._saw_close = False
        # Last read in 14:50-15:00. The snapshot uses this, not the roll read.
        self._pre_net: int | None = None
        self._pre_f22 = ""

    def observe(self, rights: dict, now_tpe: datetime) -> LossView:
        """One valid rights read. Invalid input does not move state."""
        parsed = _net_and_f22(rights)
        if parsed is None or now_tpe is None:
            return self._view(self._account_net, self._account_net, "master")
        net, f22 = parsed
        now = _as_tpe(now_tpe)
        key = trading_day_key(now)
        minute = _minutes(now)
        rolling = self._saw_roll(now)
        if key != self.key:
            pre_net, pre_f22 = self._pre_net, self._pre_f22
            self._clear_snap()
            self.key = key
            if rolling:
                self._pre_net, self._pre_f22 = pre_net, pre_f22
        if _PRE_FROM <= minute < _ROLL:
            self._pre_net = net
            self._pre_f22 = f22
        if rolling and self.snap_net is None and self._pre_net is not None:
            self.snap_net = self._pre_net
            self.snap_f22 = self._pre_f22
            self.anchored = False
        self._prev_read = now
        if _CLOSE_FROM <= minute < _HOLD_FROM:
            self._saw_close = True
        self._master_for_rest(minute)
        if self._in_hold(minute):
            if not _is_zero_row(rights, net):
                self._nz_net = net
                self._nz_f22 = f22
            held = net if self.last_L is None else self.last_L
            self._account_net = net
            basis = "snapshot" if self._snapshot_valid(now) else "master"
            return self._view(held, net, basis)
        self._decide(rights, now, net, f22, minute)
        if self._snapshot_valid(now):
            loss = net - int(self.snap_net)
            basis = "snapshot"
        else:
            loss = net
            basis = "master"
        self.last_L = loss
        self._account_net = net
        self._persist()
        return self._view(loss, net, basis)

    def load(self, now_tpe: datetime) -> None:
        """Restore this key's snapshot. A bad or stale file is not valid."""
        now = _as_tpe(now_tpe) if now_tpe is not None else _as_tpe(datetime.now(_TPE))
        if not self.path or not os.path.isfile(self.path):
            return
        try:
            with open(self.path, encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError, UnicodeError, ValueError):
            return
        if not isinstance(data, dict) or data.get("version") != _VERSION:
            return
        if data.get("account") != self._account_hash:
            return
        key = str(data.get("key") or "")
        if key != trading_day_key(now):
            return
        snap = _as_int(data.get("snap_net"))
        if snap is None:
            return
        self.key = key
        self.snap_net = snap
        self.snap_f22 = str(data.get("snap_f22") or "")
        self.anchored = bool(data.get("anchored"))
        self.last_L = _as_int(data.get("last_L"))
        self.pause_failsafe = bool(data.get("pause_failsafe"))
        if self.anchored:
            # The file already decided. Do not run _decide again.
            self._decided = True
        else:
            self._master_for_rest(_minutes(now))

    def save(self) -> None:
        self._persist()

    def consume_failsafe_notice(self) -> str | None:
        return None

    def set_pause_failsafe(self, flagged: bool) -> None:
        flagged = bool(flagged)
        if flagged == self.pause_failsafe:
            return
        self.pause_failsafe = flagged
        if self.key:
            self._persist()

    def _saw_roll(self, now: datetime) -> bool:
        """Previous read in 14:50–15:00, this read in 15:00–15:10, trading day."""
        prev = self._prev_read
        if prev is None:
            return False
        prev = _as_tpe(prev)
        prev_m = _minutes(prev)
        now_m = _minutes(now)
        return (prev.date() == now.date()
                and _PRE_FROM <= prev_m < _ROLL
                and _ROLL <= now_m < _ROLL_UNTIL
                and _known_trading_day(now.date()))

    def _master_for_rest(self, minute: int) -> bool:
        """Not valid, so the account net for the rest of the key."""
        if self._master_rest:
            return True
        if self.anchored or self._decided:
            return False
        if (self.snap_net is not None and not self._saw_close
                and _HOLD_FROM <= minute < _ROLL):
            self._master_rest = True
            self._decided = True
            return True
        return False

    def _in_hold(self, minute: int) -> bool:
        """05:00–08:30, and only while this key still has a snapshot to hold."""
        return (self.snap_net is not None and not self._master_rest
                and _HOLD_FROM <= minute < _DECIDE_AT)

    def _decide(self, rights, now: datetime, net: int, f22: str, minute: int) -> None:
        """Once, on a trading morning: anchor from the closed window, or master."""
        if self._decided or self._master_rest or self.snap_net is None:
            return
        if minute < _DECIDE_AT or minute >= _ROLL:
            return
        if not _known_trading_day(now.date()):
            self._master_rest = True
            self._decided = True
            return
        self._decided = True
        if not _is_zero_row(rights, net):
            self._nz_net = net
            self._nz_f22 = f22
        held = 0 if self.last_L is None else int(self.last_L)
        if (_is_zero_row(rights, net) or self._nz_net is None
                or not self._nz_f22 or not self.snap_f22
                or self._nz_f22 == self.snap_f22):
            self._master_rest = True
            return
        self.snap_net = int(self._nz_net) - held
        self.anchored = True

    def _snapshot_valid(self, now: datetime) -> bool:
        """False means gate on the account net."""
        if self._master_rest:
            return False
        if self.snap_net is None or self.key != trading_day_key(now):
            return False
        try:
            day = date.fromisoformat(self.key)
        except ValueError:
            return False
        if not _known_trading_day(day):
            return False
        return True

    def _clear_snap(self) -> None:
        self.snap_net = None
        self.snap_f22 = ""
        self.anchored = False
        self.last_L = None
        self._master_rest = False
        self._decided = False
        self._nz_net = None
        self._nz_f22 = ""
        self._saw_close = False
        self._pre_net = None
        self._pre_f22 = ""

    def _persist(self) -> None:
        if not self.path:
            return
        payload = {
            "version": _VERSION,
            "account": self._account_hash,
            "key": self.key,
            "snap_net": self.snap_net,
            "snap_f22": self.snap_f22,
            "anchored": self.anchored,
            "last_L": self.last_L,
            "pause_failsafe": self.pause_failsafe,
        }
        try:
            atomic_write_json(self.path, payload)
        except Exception:
            logger.warning("[LOSS] loss_baseline.json write failed", exc_info=True)

    def _view(self, loss: int, account: int, basis: str) -> LossView:
        return LossView(
            account_net=int(account),
            loss_today=int(loss),
            day_key=self.key,
            basis=basis,
            held=False,
            settled_after_reset=False,
        )
