"""Cumulative P&L by trading session — the 損益走勢 charts.

``GET /pnl-history/<bot_id>`` and ``GET /pnl-history/fleet`` both answer a
list, oldest session first, one point per session that closed a trade::

    {"session": "2026-11-05",          # trading day = its night-open date
     "sim_cumulative": 12340,          # simulated, every closed trade
     "real_cumulative": 8800,          # real-order, classification == "real" ONLY
     "real_est_cumulative": 9100,      # real + "estimated" (the 實單累計 tile's rule)
     "sim_pnl": 1200, "real_pnl": 900, "real_est_pnl": 950,   # this session alone
     "trades": 4, "real": 2, "estimated": 1, "unreliable": 0, "missing": 0,
     "is_current": true}               # the session the page calls 本交易日

Trades come from ``session.json`` (``broker.trades``) — the same closed
trades every other P&L figure on the dashboard uses; ``decisions.csv`` is
the signal/event log and carries no per-trade P&L. The real classification
is ``real_pnl.classify``. ``real_cumulative`` counts ``real`` trades only:
an ``estimated`` trade substitutes the simulated price for a fill that was
never recorded, so it is reported beside the line, not on it.

A trade belongs to the trading day it EXITED in, by the dashboard's own
boundary (``snapshot.trading_day_start``: the most recent night-session
open, 15:00 TPE, on the TAIFEX calendar).
"""
from __future__ import annotations

from datetime import datetime

from scripts.dashboard import real_pnl, snapshot

_KEYS = ("sim_pnl", "real_pnl", "real_est_pnl", "trades", "real",
         "estimated", "unreliable", "missing")


class _SessionOf:
    """exit_dt → session label, memoised (the calendar lookup is not free)."""

    def __init__(self):
        self._memo: dict[tuple, str] = {}

    def __call__(self, exit_dt) -> str | None:
        dt = snapshot.parse_tpe(exit_dt)
        if dt is None:
            return None
        # Exits are inside a session, so (date, before/after the 15:00 open)
        # decides the trading day; the lookup itself handles weekends/holidays.
        key = (dt.date(), dt.hour >= 15)
        if key not in self._memo:
            self._memo[key] = snapshot.trading_day_start(dt).strftime("%Y-%m-%d")
        return self._memo[key]


def _per_session(trades: list[dict], point_value: int, session_of) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for t in trades:
        session = session_of(t.get("exit_dt"))
        if session is None:
            continue
        row = out.setdefault(session, dict.fromkeys(_KEYS, 0))
        row["trades"] += 1
        row["sim_pnl"] += snapshot._to_int(t.get("pnl"))
        c = real_pnl.classify(t, point_value)
        status = c["status"]
        if status in ("real", "estimated", "unreliable", "missing"):
            row[status] += 1
        if status in real_pnl.COUNTED:
            row["real_est_pnl"] += c["real_pnl"]
        if status == "real":
            row["real_pnl"] += c["real_pnl"]
    return out


def _cumulate(per: dict[str, dict], current: str) -> list[dict]:
    sim = real = real_est = 0
    points = []
    for session in sorted(per):
        row = per[session]
        sim += row["sim_pnl"]
        real += row["real_pnl"]
        real_est += row["real_est_pnl"]
        points.append({"session": session, "sim_cumulative": sim,
                       "real_cumulative": real, "real_est_cumulative": real_est,
                       **row, "is_current": session == current})
    return points


def _merge(into: dict[str, dict], per: dict[str, dict]) -> None:
    for session, row in per.items():
        acc = into.setdefault(session, dict.fromkeys(_KEYS, 0))
        for k in _KEYS:
            acc[k] += row[k]


def current_session(now: datetime) -> str:
    return snapshot.trading_day_start(now).strftime("%Y-%m-%d")


def _point_value(bot_dir: str) -> int:
    sess = snapshot.read_session(bot_dir)
    broker = sess.get("broker") if isinstance(sess.get("broker"), dict) else {}
    return snapshot._to_int(sess.get("point_value") or broker.get("point_value"), 1)


def bot_history(now: datetime, bot_dir: str, session_of=None) -> list[dict]:
    session_of = session_of or _SessionOf()
    per = _per_session(snapshot._session_trades(bot_dir), _point_value(bot_dir), session_of)
    return _cumulate(per, current_session(now))


def fleet_history(now: datetime, base_dir: str, *, include_retired: bool = False,
                  pid_alive_fn=snapshot.pid_alive) -> list[dict]:
    """Every bot's sessions summed. Retired bots (the page's 顯示已停用 toggle)
    are left out unless asked for, so the chart matches the fleet tiles."""
    session_of = _SessionOf()
    combined: dict[str, dict] = {}
    for bot_dir in snapshot.discover_bot_dirs(base_dir):
        try:
            if not include_retired and not snapshot.bot_summary(
                    now, bot_dir, pid_alive_fn=pid_alive_fn).get("active", True):
                continue
            _merge(combined, _per_session(snapshot._session_trades(bot_dir),
                                          _point_value(bot_dir), session_of))
        except Exception:  # noqa: BLE001 — one broken bot must not blank the chart
            continue
    return _cumulate(combined, current_session(now))
