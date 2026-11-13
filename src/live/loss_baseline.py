"""REAL daily loss measured from the 15:00 TPE account snapshot.

The gate counts the loss since the last read in the 13:50–15:00 window,
not Capital's full account net. Capital itself rebases the next morning
(~05:20–05:38 TPE). This module detects that batch and holds the gated
figure through the all-zero and f22-first interim reads.

No Tk, no COM. The app calls :meth:`DailyLossBaseline.observe` with the
parsed rights dict and ``_taipei_now()``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

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
    """Per-bot snapshot of the account net at the trading-day roll."""

    def __init__(self, path: str, account: str):
        self.path = path
        self._account_hash = hashlib.sha256(
            str(account or "").encode("utf-8")).hexdigest()[:12]
        self.day_key = ""
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
        self._last_candidate_mono = -1e9

    def observe(self, rights: dict, now_tpe: datetime) -> LossView:
        """One valid rights read. Invalid input does not move state."""
        parsed = _components(rights)
        if parsed is None or now_tpe is None:
            shown = self.last_l if self.last_l is not None else 0
            return self._view(shown, self._account_net, False, False)
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

        if key != self.day_key:
            self._roll(key, now, fee, equity)
            dirty = True
            force = True

        if not self._reset_open():
            if self._reset_detected(fee, equity):
                self._begin_reset(now, fee)
                dirty = True
                force = True

        if fee > self.max_fees_seen:
            self.max_fees_seen = fee
            dirty = True
            force = True

        loss = account - self.b_r - self.b_c
        held = False
        just_settled = False
        if self._reset_open():
            if self._can_settle(now, fee):
                self.capital_reset["settled"] = True
                just_settled = True
                dirty = True
                force = True
            else:
                held_loss = self.capital_reset.get("l_pre")
                if held_loss is not None:
                    held = True
                    if _off_session(now):
                        loss = int(held_loss)
                    else:
                        loss = min(loss, int(held_loss))
        self.last_l = loss
        if dirty:
            self._persist(candidate_only=dirty and not force)
        return self._view(loss, account, held, just_settled)

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
        if not self._restore(data, trading_day_key(now)):
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
        if self.day_key and _is_weekend(key):
            self.day_key = key
            if self.basis not in ("failsafe", "capital_reset"):
                self.basis = "carried"
            return
        if self.candidate is not None and self.candidate.date == key:
            self._commit(self.candidate, key)
            return
        self._enter_failsafe(key, fee, equity, now)

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
        self._announce_failsafe(key)

    def _reset_open(self) -> bool:
        return isinstance(self.capital_reset, dict) and not self.capital_reset.get("settled")

    def _reset_detected(self, fee: int, equity: str) -> bool:
        """Capital's window broke. Any one signal is enough; all three stay."""
        fee_below_max = fee < self.max_fees_seen
        fee_below_baseline = fee < self.baseline_fees
        equity_moved = equity != self.baseline_f22
        return fee_below_max or fee_below_baseline or equity_moved

    def _begin_reset(self, now: datetime, fee: int) -> None:
        expect = self.max_fees_seen - self.baseline_fees
        self.capital_reset = {
            "detected_at": _iso(now),
            "l_pre": self.last_l,
            "expect_fees": expect,
            "settled": False,
        }
        self._detected_at = now
        self.b_r = 0
        self.basis = "capital_reset"

    def _can_settle(self, now: datetime, fee: int) -> bool:
        reset = self.capital_reset or {}
        if fee < int(reset.get("expect_fees") or 0):
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

    # ── persistence ───────────────────────────────────────────────────

    def _restore(self, data: dict, key: str) -> bool:
        candidate = _snap_from(data.get("candidate"), with_date=True)
        file_key = str(data.get("day_key") or "")
        if file_key and file_key == key:
            return self._restore_state(data, candidate)
        if candidate is not None and candidate.date == key:
            self.candidate = candidate
            self._commit(candidate, key)
            self._persist(candidate_only=False)
            return True
        # Stale file. Keep a candidate that might still commit at the roll.
        self.candidate = candidate
        self.day_key = ""
        self.b_r = 0
        self.b_c = 0
        return True

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
            }
            self._detected_at = detected
            if detected is None and not self.capital_reset["settled"]:
                self.capital_reset["settled"] = True
        if self.basis == "failsafe":
            self._failsafe_logged_key = self.day_key
        return True

    def _payload(self) -> dict:
        reset = None
        if isinstance(self.capital_reset, dict):
            reset = {
                "detected_at": self.capital_reset.get("detected_at"),
                "l_pre": self.capital_reset.get("l_pre"),
                "expect_fees": self.capital_reset.get("expect_fees"),
                "settled": bool(self.capital_reset.get("settled")),
            }
        return {
            "version": _VERSION,
            "account": self._account_hash,
            "day_key": self.day_key,
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

    def _view(self, loss: int, account: int, held: bool, settled: bool) -> LossView:
        return LossView(
            account_net=int(account),
            loss_today=int(loss),
            day_key=self.day_key,
            basis=self.basis or "",
            held=held,
            settled_after_reset=settled,
        )


def _is_weekend(key: str) -> bool:
    try:
        return datetime.fromisoformat(key).weekday() >= 5
    except (TypeError, ValueError):
        return False


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
