"""One daily-loss rule for a REAL bot.

A valid 15:00 TPE snapshot for this trading-day key gives
``L = net_now - snap_net``. Otherwise ``L`` is the account net, which is
what master gates on.

The snapshot is valid only when the file key is this session, the 15:00
read was a real account net on a known trading day, and — once the clock
is past 08:30 in the morning half of that session — the morning re-anchor
has happened. No file, a stale key, a non-trading or unknown day, a
missed 08:30 re-anchor, and a restart after 08:30 that never anchored are
that same fact: the snapshot is not valid.

The one extra is Capital's morning rebase. Issue-107 ``GetFutureRights``
logs show that batch at about 05:20–05:38 TPE, and it moves f22. The
first such read sets ``snap_net = net_now - last_L`` so ``L`` does not
jump.
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
_ANCHOR_FROM = 5 * 60 + 20
_ANCHOR_BY = 8 * 60 + 30
_ROLL = 15 * 60


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

    def observe(self, rights: dict, now_tpe: datetime) -> LossView:
        """One valid rights read. Invalid input does not move state."""
        parsed = _net_and_f22(rights)
        if parsed is None or now_tpe is None:
            shown = 0 if self.last_L is None else self.last_L
            return self._view(shown, self._account_net, "master")
        net, f22 = parsed
        now = _as_tpe(now_tpe)
        key = trading_day_key(now)
        if key != self.key:
            self._clear_snap()
            self.key = key
        if (self.snap_net is None and _minutes(now) >= _ROLL
                and _known_trading_day(now.date())):
            self.snap_net = net
            self.snap_f22 = f22
            self.anchored = False
        if self._anchor_due(now, f22):
            self.snap_net = net - int(self.last_L or 0)
            self.anchored = True
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

    def _anchor_due(self, now: datetime, f22: str) -> bool:
        if self.snap_net is None or self.anchored:
            return False
        if not f22 or not self.snap_f22 or f22 == self.snap_f22:
            return False
        minute = _minutes(now)
        return _ANCHOR_FROM <= minute <= _ANCHOR_BY

    def _snapshot_valid(self, now: datetime) -> bool:
        """False means gate on the account net."""
        if self.snap_net is None or self.key != trading_day_key(now):
            return False
        try:
            day = date.fromisoformat(self.key)
        except ValueError:
            return False
        if not _known_trading_day(day):
            return False
        minute = _minutes(now)
        if minute < _ROLL and minute >= _ANCHOR_BY and not self.anchored:
            return False
        return True

    def _clear_snap(self) -> None:
        self.snap_net = None
        self.snap_f22 = ""
        self.anchored = False
        self.last_L = None

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
