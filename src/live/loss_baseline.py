"""REAL daily loss measured from the 15:00 TPE account snapshot.

The gate counts the loss since the last read in the 13:50–15:00 window,
not Capital's full account net. Capital itself rebases the next morning
(~05:20–05:38 TPE). This module detects that batch and holds the gated
figure through the all-zero and f22-first interim reads.

A night and the next calendar day's session share that budget. When
every calendar day strictly between that night and the day session is a
known TAIFEX closure (weekend or holiday), that day gets its own budget
from the last settled read in 08:30–08:45. A skipped trading day, or a
day whose calendar is unknown, does not. The gate is then the account net.
A fresh deploy without
loss_baseline.json gets no separate gap budget until the next 15:00
snapshot. Field f37 (expiry settlement) is not read.

No Tk, no COM. The app calls :meth:`DailyLossBaseline.observe` with the
parsed rights dict and ``_taipei_now()``.
"""

from __future__ import annotations

import enum
import hashlib
import json
import logging
import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from src.live.main_heartbeat import atomic_write_json
from src.live.trading_guard import trading_day_key

logger = logging.getLogger(__name__)

_TPE = timezone(timedelta(hours=8))
_VERSION = 1
_CANDIDATE_SAVE_S = 60.0

# Fixed TPE clocks. Off-session hold freezes the gate; the morning batch
# is not settled until 08:30.
_OFF_LO = 5 * 60
_OFF_HI = 8 * 60 + 45
_OFF_MID_LO = 13 * 60 + 45
_OFF_MID_HI = 15 * 60
_CAND_LO = 13 * 60 + 50
_CAND_HI = 15 * 60
_MORNING_LO = 5 * 60
_MORNING_HI = 8 * 60 + 30
_SETTLE_AT = 8 * 60 + 30
_DAY_LO = 8 * 60 + 30
_DAY_HI = 8 * 60 + 45
_NIGHT_OPEN = 15 * 60


def _as_tpe(now: datetime) -> datetime:
    """Naive datetimes are Taipei wall time. Aware ones convert."""
    if now.tzinfo is None:
        return now.replace(tzinfo=_TPE)
    return now.astimezone(_TPE)


def _minutes(now: datetime) -> int:
    local = _as_tpe(now)
    return local.hour * 60 + local.minute


def _iso(now: datetime) -> str:
    return _as_tpe(now).isoformat(timespec="seconds")


def _parse_iso(text: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(text))
    except (TypeError, ValueError):
        return None
    return _as_tpe(parsed)


def _as_int(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _off_session(now: datetime) -> bool:
    minute = _minutes(now)
    if _OFF_LO <= minute < _OFF_HI:
        return True
    return _OFF_MID_LO <= minute < _OFF_MID_HI


def _in_candidate_window(now: datetime) -> bool:
    minute = _minutes(now)
    return _CAND_LO <= minute < _CAND_HI


def _detected_in_morning(detected: datetime) -> bool:
    minute = _minutes(detected)
    return _MORNING_LO <= minute < _MORNING_HI


def _at_or_after_0830(now: datetime) -> bool:
    return _minutes(now) >= _SETTLE_AT


def _in_day_open_window(now: datetime) -> bool:
    minute = _minutes(now)
    return _DAY_LO <= minute < _DAY_HI


class _Cal(enum.Enum):
    TRADING = "trading"
    CLOSED = "closed"
    UNKNOWN = "unknown"


def _day_status(day: date) -> _Cal:
    """Trading, known closure, or unknown.

    Weekends are closed without the calendar. A weekday is unknown when
    the holiday lookup raises, the year's holiday set is empty, or
    ``holiday_calendar_health`` is false. Unknown is not closed and not
    open: no commit, no roll, and no separate budget.
    """
    if day.weekday() >= 5:
        return _Cal.CLOSED
    try:
        from ..market_data.holidays import (
            _tw_public_holidays,
            holiday_calendar_health,
            is_taifex_holiday,
        )
        ok, _detail = holiday_calendar_health(day.year)
        days = _tw_public_holidays(day.year)
        if (not ok) or not days:
            return _Cal.UNKNOWN
        return _Cal.CLOSED if is_taifex_holiday(day) else _Cal.TRADING
    except Exception:
        return _Cal.UNKNOWN


def _day_status_of_key(key: str) -> _Cal:
    try:
        return _day_status(datetime.fromisoformat(key).date())
    except (TypeError, ValueError):
        return _Cal.UNKNOWN


def _gap_between(night: date, today: date) -> str:
    """How the days strictly between two dates look.

    ``none`` when the span is not a gap. ``closed`` when every day in
    between is a known TAIFEX closure. ``trading`` when any of them is a
    known session. ``unknown`` when none of them is a known session but
    at least one calendar is unknown.
    """
    if not (today - night).days > 1:
        return "none"
    saw_unknown = False
    day = night + timedelta(days=1)
    while day < today:
        status = _day_status(day)
        if status == _Cal.TRADING:
            return "trading"
        if status == _Cal.UNKNOWN:
            saw_unknown = True
        day += timedelta(days=1)
    return "unknown" if saw_unknown else "closed"


@dataclass(frozen=True)
class _Snap:
    date: str
    b_r: int
    b_c: int
    fees: int
    f22: str
    taken_at: str

    def material(self) -> tuple:
        return (self.b_r, self.fees, self.f22, self.b_c)

    def as_candidate(self) -> dict:
        return {
            "date": self.date,
            "B_r": self.b_r,
            "B_c": self.b_c,
            "fees": self.fees,
            "f22": self.f22,
            "taken_at": self.taken_at,
        }


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


def _components(rights):
    """R, F, FEE, W from a parsed rights dict, or None when it is not valid."""
    if not isinstance(rights, dict) or rights.get("valid") is False:
        return None
    try:
        realized = _as_int(float(rights.get("realized_pnl", "0") or 0))
        fee = _as_int(float(rights.get("realized_cost", "0") or 0))
        tax = _as_int(float(rights.get("tax", "0") or 0))
        floating = _as_int(float(rights.get("float_pnl", "0") or 0))
    except (TypeError, ValueError):
        return None
    if None in (realized, fee, tax, floating):
        return None
    if "start_equity" in rights:
        equity = str(rights.get("start_equity") or "").strip()
    elif "f22" in rights:
        equity = str(rights.get("f22") or "").strip()
    else:
        equity = ""
    return realized - fee - tax, floating, fee + tax, equity


class DailyLossBaseline:
    """Per-bot snapshot of the account net at the trading-day roll.

    Two REAL bots on one account would each gate on this same
    account-wide figure. That case is deferred until a second REAL bot
    exists; the fix would be one file per account. No logic for it here.
    """

    def __init__(self, path: str, account: str):
        self.path = path
        self._account_hash = hashlib.sha256(
            str(account or "").encode("utf-8")).hexdigest()[:12]
        self.day_key = ""
        self.night_date = ""
        self.basis = ""
        self.b_r = 0
        self.b_c = 0
        self.baseline_fees = 0
        self.baseline_f22 = ""
        self.baseline_taken_at = ""
        self.max_fees_seen = 0
        self.last_l: int | None = None
        self.candidate: _Snap | None = None
        self.capital_reset: dict | None = None
        self._detected_at: datetime | None = None
        self.pause_failsafe = False
        self._account_net = 0
        self._pending_notice: str | None = None
        self._failsafe_logged_key = ""
        self._unknown_noted: set[str] = set()
        self._last_realized: int | None = None
        self._last_candidate_mono = -1e9
        self._clear_day_budget()

    def observe(self, rights: dict, now_tpe: datetime) -> LossView:
        """One valid rights read. Invalid input does not move state."""
        parsed = _components(rights)
        if parsed is None or now_tpe is None:
            shown = self.last_l if self.last_l is not None else 0
            return self._view(shown, self._account_net, False, False, False, False)
        now = _as_tpe(now_tpe)
        realized, floating, fee, equity = parsed
        account = realized + floating
        self._account_net = account
        key = trading_day_key(now)
        dirty = False
        force = False

        if _in_candidate_window(now):
            snap = _Snap(
                date=now.date().isoformat(),
                b_r=realized,
                b_c=max(0, floating),
                fees=fee,
                f22=equity,
                taken_at=_iso(now),
            )
            previous = self.candidate.material() if self.candidate else None
            self.candidate = snap
            if previous != snap.material():
                dirty = True

        if _day_status(now.date()) == _Cal.UNKNOWN:
            self._note_unknown(now.date().isoformat())
        if key != self.day_key:
            if _day_status_of_key(key) == _Cal.UNKNOWN:
                # Same roll as master: the guard's key changes at 15:00
                # and the figure is the account net. No carried minimum,
                # and no separate budget. The warning uses this real key.
                self._note_unknown(key)
                self._adopt_master_day(key)
                dirty = True
                force = True
            else:
                self._roll(key, now, fee, equity)
                dirty = True
                force = True

        # Sticky for this key. A settled reset must not fire again on the
        # next poll just because f22 still differs from the pre-batch value.
        if not isinstance(self.capital_reset, dict):
            if self._reset_detected(fee, equity):
                self._begin_reset(now, fee, realized)
                dirty = True
                force = True

        if fee > self.max_fees_seen:
            self.max_fees_seen = fee
            dirty = True
            force = True

        held = False
        just_settled = False
        if self._reset_open() and self._can_settle(now, realized, fee, equity):
            self.b_r = 0
            self.capital_reset["settled"] = True
            just_settled = True
            dirty = True
            force = True

        before_day = (
            self._has_day_baseline, self.day_b_r, self.day_b_c,
            self.day_budget, self.day_failsafe)
        opened, recheck = self._consider_day_budget(
            now, realized, floating, fee, equity)
        if (opened or recheck or before_day != (
                self._has_day_baseline, self.day_b_r, self.day_b_c,
                self.day_budget, self.day_failsafe)):
            dirty = True
            force = True

        if self.day_budget:
            loss = account - self.day_b_r - self.day_b_c
        else:
            loss = account - self.b_r - self.b_c
            if self._reset_open():
                held_loss = self.capital_reset.get("l_pre")
                if held_loss is not None:
                    held = True
                    # Off-session used to freeze l_pre even when the
                    # formula had already worsened. Keep the stricter of
                    # the two. Do not substitute the raw account: an
                    # f22-first read still carries the pre-batch net.
                    loss = min(loss, int(held_loss))
        if (not self.day_budget
                and not self._reset_open()
                and self._span(now) == "closed"
                and self.last_l is not None
                and _day_status(now.date()) != _Cal.UNKNOWN):
            # A carried closure keeps the stricter held loss. A skipped
            # trading day and an unknown calendar do not.
            loss = min(loss, int(self.last_l))
        self._last_realized = realized
        self.last_l = loss
        if dirty:
            self._persist(candidate_only=dirty and not force)
        return self._view(loss, account, held, just_settled, opened, recheck)

    def load(self, now_tpe: datetime) -> None:
        """Restore today's baseline. A bad file becomes B=0 and does not raise."""
        now = _as_tpe(now_tpe) if now_tpe is not None else _as_tpe(datetime.now(_TPE))
        if not self.path or not os.path.isfile(self.path):
            return
        try:
            with open(self.path, encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError, UnicodeError, ValueError):
            self._ignore("corrupt")
            return
        if not isinstance(data, dict):
            self._ignore("corrupt")
            return
        if data.get("version") != _VERSION:
            self._ignore("unknown version")
            return
        if data.get("account") != self._account_hash:
            self._ignore("account mismatch")
            return
        if not self._restore(data, trading_day_key(now), now):
            self._ignore("corrupt")

    def save(self) -> None:
        self._persist(candidate_only=False)

    def consume_failsafe_notice(self) -> str | None:
        notice = self._pending_notice
        self._pending_notice = None
        return notice

    def set_pause_failsafe(self, flagged: bool) -> None:
        flagged = bool(flagged)
        if flagged == self.pause_failsafe:
            return
        self.pause_failsafe = flagged
        if self.day_key:
            self._persist(candidate_only=False)

    # ── roll / reset ──────────────────────────────────────────────────

    def _roll(self, key: str, now: datetime, fee: int, equity: str) -> None:
        status = _day_status_of_key(key)
        # A non-trading key (weekend or weekday holiday) carries the last
        # trading night. No commit and no failsafe, and night_date stays.
        if self.day_key and status == _Cal.CLOSED:
            self._carry(key)
            return
        # Before the night open, a real closure gap keeps the night so the
        # day budget can still open on the next trading morning.
        if (self.day_key and _minutes(now) < _NIGHT_OPEN
                and self._needs_day_budget(now)):
            self._carry(key)
            return
        # A skipped trading day, or an unknown day in the span, is not a
        # fresh night and not a failsafe. Keep the baseline.
        if (self.day_key and _minutes(now) < _NIGHT_OPEN
                and self._span(now) in ("trading", "unknown")):
            self._carry(key)
            return
        if self.candidate is not None and self.candidate.date == key:
            self._commit(self.candidate, key)
            return
        self._enter_failsafe(key, fee, equity, now)

    def _adopt_master_day(self, key: str) -> None:
        """Unknown calendar: gate on the account, on master's day key."""
        self.day_key = key
        self.b_r = 0
        self.b_c = 0
        self.basis = "failsafe"
        self.capital_reset = None
        self._detected_at = None
        self.last_l = None
        self._clear_day_budget()

    def _carry(self, key: str) -> None:
        self.day_key = key
        # The next session's Capital batch has to be visible. A reset
        # record carried over the gap would swallow it.
        self.capital_reset = None
        self._detected_at = None
        if self.basis not in ("failsafe", "day_open"):
            self.basis = "carried"

    def _commit(self, snap: _Snap, key: str) -> None:
        self.day_key = key
        self.b_r = snap.b_r
        self.b_c = snap.b_c
        self.baseline_fees = snap.fees
        self.baseline_f22 = snap.f22
        self.baseline_taken_at = snap.taken_at
        self.max_fees_seen = snap.fees
        self.basis = "snapshot"
        self.capital_reset = None
        self._detected_at = None
        self.last_l = None
        self.night_date = key
        self._clear_day_budget()

    def _enter_failsafe(self, key: str, fee: int, equity: str, now: datetime) -> None:
        self.day_key = key
        self.b_r = 0
        self.b_c = 0
        self.baseline_fees = fee
        self.baseline_f22 = equity
        self.baseline_taken_at = _iso(now)
        self.max_fees_seen = fee
        self.basis = "failsafe"
        self.capital_reset = None
        self._detected_at = None
        self.last_l = None
        self.night_date = key
        self._clear_day_budget()
        self._announce_failsafe(key)

    def _night(self) -> date | None:
        try:
            return datetime.fromisoformat(self.night_date).date()
        except (TypeError, ValueError):
            return None

    def _span(self, now: datetime) -> str:
        night = self._night()
        if night is None:
            return "none"
        return _gap_between(night, _as_tpe(now).date())

    def _uncertain(self, now: datetime) -> bool:
        """Missing calendar, or a trading day was skipped inside the span."""
        if _day_status(_as_tpe(now).date()) == _Cal.UNKNOWN:
            return True
        return self._span(now) in ("trading", "unknown")

    def _note_unknown(self, key: str) -> None:
        if not key or key in self._unknown_noted:
            return
        self._unknown_noted.add(key)
        logger.warning(
            "[LOSS] holiday calendar unknown for %s; no separate budget",
            key)

    def _needs_day_budget(self, now: datetime) -> bool:
        """A gap whose every intervening day is a known TAIFEX closure."""
        return self._span(now) == "closed"

    def _consider_day_budget(self, now, realized, floating, fee, equity):
        """Return (opened_now, failsafe_recheck_now)."""
        if not self._needs_day_budget(now) or self.day_budget or self.day_failsafe:
            return False, False
        # Weekend and holiday polls run every 30 s. An unknown weekday
        # is not a session either: no B_day, no open, no missing-baseline note.
        if _day_status(_as_tpe(now).date()) != _Cal.TRADING:
            return False, False
        waiting_on_window_reset = bool(
            isinstance(self.capital_reset, dict)
            and self.capital_reset.get("in_day_window")
            and self._reset_open())
        if _in_day_open_window(now):
            if waiting_on_window_reset:
                return False, False
            self._take_day_baseline(now, realized, floating, fee, equity)
            return False, False
        if _minutes(now) >= _DAY_HI:
            # The +2 min settle of a batch that started inside the window
            # can land just after 08:45. That settled read is B_day.
            if waiting_on_window_reset:
                return False, False
            if (not self._has_day_baseline
                    and isinstance(self.capital_reset, dict)
                    and self.capital_reset.get("in_day_window")
                    and not self._reset_open()):
                self._take_day_baseline(now, realized, floating, fee, equity)
            if self._has_day_baseline:
                self.day_budget = True
                self.basis = "day_open"
                return True, False
            self.day_failsafe = True
            self._announce_day_failsafe(now.date().isoformat())
            return False, True
        return False, False

    def _take_day_baseline(self, now, realized, floating, fee, equity) -> None:
        self.day_b_r = realized
        self.day_b_c = max(0, floating)
        self.day_fees = fee
        self.day_f22 = equity
        self.day_taken_at = _iso(now)
        self._has_day_baseline = True

    def _clear_day_budget(self) -> None:
        self._has_day_baseline = False
        self.day_b_r = 0
        self.day_b_c = 0
        self.day_fees = 0
        self.day_f22 = ""
        self.day_taken_at = ""
        self.day_budget = False
        self.day_failsafe = False

    def _announce_day_failsafe(self, day: str) -> None:
        message = (
            f"[LOSS] no 08:30 baseline for {day}; gating on the night budget")
        logger.warning(message)
        self._pending_notice = message

    def _reset_open(self) -> bool:
        return isinstance(self.capital_reset, dict) and not self.capital_reset.get("settled")

    def _reset_detected(self, fee: int, equity: str) -> bool:
        """Capital's window broke. Any one signal is enough; all three stay."""
        fee_below_max = fee < self.max_fees_seen
        fee_below_baseline = fee < self.baseline_fees
        equity_moved = equity != self.baseline_f22
        return fee_below_max or fee_below_baseline or equity_moved

    def _begin_reset(self, now: datetime, fee: int, realized: int) -> None:
        expect = self.max_fees_seen - self.baseline_fees
        in_window = _in_day_open_window(now)
        # The pre-batch R is the previous read. B_r stays until a read
        # shows the full reload, so a fee blip cannot become the account net.
        pre_r = self._last_realized if self._last_realized is not None else realized
        self.capital_reset = {
            "detected_at": _iso(now),
            "l_pre": self.last_l,
            "expect_fees": expect,
            "settled": False,
            "in_day_window": in_window,
            "pre_max_fees": self.max_fees_seen,
            "pre_f22": self.baseline_f22,
            "pre_r": pre_r,
        }
        self._detected_at = now
        self.basis = "capital_reset"
        # A pre-batch read already inside 08:30–08:45 is not B_day.
        if in_window:
            self._has_day_baseline = False

    def _rebase_confirmed(self, realized: int, equity: str) -> bool:
        """Completed morning reload: f22 moved and R left the pre-batch value.

        A fee drop alone is the zero interim or a blip. f22 alone is the
        f22-first read, which still carries the old realized. Neither is
        the reload.
        """
        reset = self.capital_reset or {}
        pre_f22 = str(reset.get("pre_f22") or "")
        pre_r = reset.get("pre_r")
        if pre_r is None or not equity or not pre_f22 or equity == pre_f22:
            return False
        return int(realized) != int(pre_r)

    def _can_settle(self, now: datetime, realized: int, fee: int, equity: str) -> bool:
        reset = self.capital_reset or {}
        if fee < int(reset.get("expect_fees") or 0):
            return False
        if not self._rebase_confirmed(realized, equity):
            return False
        detected = self._detected_at
        if detected is None:
            return False
        if _detected_in_morning(detected):
            return _at_or_after_0830(now)
        return now >= detected + timedelta(minutes=2)

    def _announce_failsafe(self, key: str) -> None:
        if self._failsafe_logged_key == key:
            return
        self._failsafe_logged_key = key
        message = (
            f"[LOSS] no 15:00 baseline for {key}; gating on full account net")
        logger.warning(message)
        self._pending_notice = message

    def _ignore(self, reason: str) -> None:
        logger.warning(
            "[LOSS] ignoring loss_baseline.json (%s); gating on full account net",
            reason)
        self.day_key = ""
        self.night_date = ""
        self.basis = ""
        self.b_r = 0
        self.b_c = 0
        self.baseline_fees = 0
        self.baseline_f22 = ""
        self.baseline_taken_at = ""
        self.max_fees_seen = 0
        self.last_l = None
        self.candidate = None
        self.capital_reset = None
        self._detected_at = None
        self.pause_failsafe = False
        self._clear_day_budget()

    # ── persistence ───────────────────────────────────────────────────

    def _restore(self, data: dict, key: str, now: datetime) -> bool:
        candidate = _snap_from(data.get("candidate"), with_date=True)
        file_key = str(data.get("day_key") or "")
        if file_key and file_key == key:
            return self._restore_state(data, candidate)
        if candidate is not None and candidate.date == key:
            self.candidate = candidate
            self._commit(candidate, key)
            self._persist(candidate_only=False)
            return True
        # Friday's file, loaded Monday before 15:00: the key differs
        # (Sunday) but the night budget is still the one to carry.
        if self._should_restore_gap(data, now):
            if not self._restore_state(data, candidate):
                return False
            night = self.night_date
            # An unknown day is not rolled onto. A closure, including a
            # weekend at or after 15:00, carries the way the live loop does.
            if (_day_status(_as_tpe(now).date()) != _Cal.UNKNOWN
                    and key and key != self.day_key):
                self._carry(key)
                self.night_date = night
            self._persist(candidate_only=False)
            return True
        # Stale file. Keep a candidate that might still commit at the roll.
        self.candidate = candidate
        self.day_key = ""
        self.night_date = ""
        self.b_r = 0
        self.b_c = 0
        return True

    def _should_restore_gap(self, data: dict, now: datetime) -> bool:
        """Keep a saved night across a closure, including at or after 15:00.

        A known trading day still restores before 15:00 when the file is
        from an earlier night, so a Monday 07:00 restart can open the day
        budget. At or after 15:00 on a trading day the file is stale.
        """
        if data.get("day_budget") or data.get("day_failsafe"):
            return False
        night = str(data.get("night_date") or data.get("day_key") or "")
        try:
            night_d = datetime.fromisoformat(night).date()
        except ValueError:
            return False
        today = _as_tpe(now).date()
        status = _day_status(today)
        if status in (_Cal.CLOSED, _Cal.UNKNOWN):
            return True
        if _minutes(now) >= _NIGHT_OPEN:
            return False
        # Only a real closure gap. A Wednesday load of Friday's file is
        # not one: Monday and Tuesday were trading days.
        return _gap_between(night_d, today) == "closed"

    def _restore_state(self, data: dict, candidate: _Snap | None) -> bool:
        baseline = data.get("baseline")
        if not isinstance(baseline, dict):
            return False
        b_r = _as_int(baseline.get("B_r"))
        b_c = _as_int(baseline.get("B_c"))
        fees = _as_int(baseline.get("fees"))
        if None in (b_r, b_c, fees):
            return False
        self.day_key = str(data.get("day_key") or "")
        self.night_date = str(data.get("night_date") or self.day_key or "")
        self.basis = str(data.get("basis") or "")
        self.b_r = b_r
        self.b_c = b_c
        self.baseline_fees = fees
        self.baseline_f22 = str(baseline.get("f22") or "")
        self.baseline_taken_at = str(baseline.get("taken_at") or "")
        seen = _as_int(data.get("max_fees_seen"))
        self.max_fees_seen = fees if seen is None else seen
        last = data.get("last_l")
        self.last_l = None if last is None else _as_int(last)
        self.candidate = candidate
        self.pause_failsafe = bool(data.get("pause_failsafe", False))
        self.capital_reset = None
        self._detected_at = None
        reset = data.get("capital_reset")
        if isinstance(reset, dict):
            detected = _parse_iso(reset.get("detected_at"))
            l_pre = reset.get("l_pre")
            expect = _as_int(reset.get("expect_fees"))
            self.capital_reset = {
                "detected_at": str(reset.get("detected_at") or ""),
                "l_pre": None if l_pre is None else _as_int(l_pre),
                "expect_fees": 0 if expect is None else expect,
                "settled": bool(reset.get("settled")),
                "in_day_window": bool(reset.get("in_day_window")),
                "pre_max_fees": _as_int(reset.get("pre_max_fees")),
                "pre_f22": str(reset.get("pre_f22") or ""),
                "pre_r": _as_int(reset.get("pre_r")),
            }
            self._detected_at = detected
            if detected is None and not self.capital_reset["settled"]:
                self.capital_reset["settled"] = True
        if self.basis == "failsafe":
            self._failsafe_logged_key = self.day_key
        self._clear_day_budget()
        raw_day = data.get("day_baseline")
        if isinstance(raw_day, dict):
            day_r = _as_int(raw_day.get("B_r"))
            day_c = _as_int(raw_day.get("B_c"))
            if day_r is not None and day_c is not None:
                self._has_day_baseline = True
                self.day_b_r = day_r
                self.day_b_c = day_c
                self.day_fees = _as_int(raw_day.get("fees")) or 0
                self.day_f22 = str(raw_day.get("f22") or "")
                self.day_taken_at = str(raw_day.get("taken_at") or "")
        self.day_budget = bool(data.get("day_budget"))
        self.day_failsafe = bool(data.get("day_failsafe"))
        return True

    def _payload(self) -> dict:
        reset = None
        if isinstance(self.capital_reset, dict):
            reset = {
                "detected_at": self.capital_reset.get("detected_at"),
                "l_pre": self.capital_reset.get("l_pre"),
                "expect_fees": self.capital_reset.get("expect_fees"),
                "settled": bool(self.capital_reset.get("settled")),
                "in_day_window": bool(self.capital_reset.get("in_day_window")),
                "pre_max_fees": self.capital_reset.get("pre_max_fees"),
                "pre_f22": self.capital_reset.get("pre_f22"),
                "pre_r": self.capital_reset.get("pre_r"),
            }
        return {
            "version": _VERSION,
            "account": self._account_hash,
            "day_key": self.day_key,
            "night_date": self.night_date,
            "basis": self.basis,
            "baseline": {
                "B_r": self.b_r,
                "B_c": self.b_c,
                "fees": self.baseline_fees,
                "f22": self.baseline_f22,
                "taken_at": self.baseline_taken_at,
            },
            "candidate": None if self.candidate is None else self.candidate.as_candidate(),
            "max_fees_seen": self.max_fees_seen,
            "last_l": self.last_l,
            "capital_reset": reset,
            "pause_failsafe": self.pause_failsafe,
            "day_baseline": None if not self._has_day_baseline else {
                "B_r": self.day_b_r,
                "B_c": self.day_b_c,
                "fees": self.day_fees,
                "f22": self.day_f22,
                "taken_at": self.day_taken_at,
            },
            "day_budget": self.day_budget,
            "day_failsafe": self.day_failsafe,
        }

    def _persist(self, *, candidate_only: bool) -> None:
        if not self.path:
            return
        if candidate_only:
            import time
            now_m = time.monotonic()
            if now_m - self._last_candidate_mono < _CANDIDATE_SAVE_S:
                return
            self._last_candidate_mono = now_m
        try:
            atomic_write_json(self.path, self._payload())
        except Exception:
            logger.warning("[LOSS] loss_baseline.json write failed", exc_info=True)

    def _view(self, loss: int, account: int, held: bool, settled: bool,
              opened: bool, recheck: bool) -> LossView:
        return LossView(
            account_net=int(account),
            loss_today=int(loss),
            day_key=self.day_key,
            basis=self.basis or "",
            held=held,
            settled_after_reset=settled,
            day_budget_opened=opened,
            day_failsafe_recheck=recheck,
        )


def _snap_from(raw, *, with_date: bool) -> _Snap | None:
    if not isinstance(raw, dict):
        return None
    b_r = _as_int(raw.get("B_r"))
    b_c = _as_int(raw.get("B_c"))
    fees = _as_int(raw.get("fees"))
    if None in (b_r, b_c, fees):
        return None
    return _Snap(
        date=str(raw.get("date") or ""),
        b_r=b_r,
        b_c=b_c,
        fees=fees,
        f22=str(raw.get("f22") or ""),
        taken_at=str(raw.get("taken_at") or ""),
    )
