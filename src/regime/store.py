"""Persistence for regime state JSON + history CSV.

- ``regime_state.json`` — the latest RegimeState plus the pending
  next-session recommendation (atomic write via temp + os.replace).
- ``regime_history.csv`` — v2: one row per assessed session, P&L
  recorded directly from the switching runner's own broker.
"""

import csv
import json
import logging
import os

from .state_machine import RegimeState
from .selector import Recommendation

logger = logging.getLogger(__name__)

# v2 header — superset of v1, 4 new columns at the end.
_V2_HEADER = [
    "date", "session", "adx", "plus_di", "minus_di", "atr_ratio",
    "ema_slope", "close", "raw_regime", "effective_regime",
    "confirm_count", "decision", "strategy_deployed",
    "dry_run", "override", "pnl", "trades",
    "strategy_active", "applied", "applied_at", "trading_mode",
]


def load_state(path: str) -> RegimeState:
    if not os.path.exists(path):
        return RegimeState()
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
    except (json.JSONDecodeError, ValueError):
        logger.warning("[REGIME] Corrupted state file %s — resetting to default", path)
        return RegimeState()
    s = RegimeState()
    for k, v in d.items():
        if hasattr(s, k):
            setattr(s, k, v)
    return s


def save_state(path: str, state: RegimeState, rec: Recommendation, session_date: str):
    import dataclasses
    d = dataclasses.asdict(state)
    d["next_session"] = {
        "date": session_date, "action": rec.action,
        "strategy": rec.strategy_name, "qty_scale": rec.qty_scale,
        "reason": rec.reason,
        "executed": False, "executed_at": None,
    }
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def write_placeholder_state(path: str) -> None:
    """Write an inspectable placeholder ``regime_state.json`` at deploy time.

    Only writes when no state file exists yet, so a resumed bot's real
    state (including any unexecuted pending recommendation) is never
    clobbered. Uses the same atomic temp+fsync+replace pattern as
    ``save_state`` so the file is never observed half-written. The first
    night-end classification overwrites this with the full RegimeState.
    """
    if os.path.exists(path):
        return
    d = {"regime": "unknown", "classified_at": None}
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def migrate_legacy_history(csv_path: str) -> None:
    """Rename a v1 history file to regime_history_legacy.csv.

    v1 rows measured the deployed single-strategy bot's P&L — a
    different quantity than v2 (switching bot's own P&L). Mixing
    them corrupts evaluation, so v1 is archived, not upgraded.
    """
    if not os.path.exists(csv_path):
        return
    try:
        with open(csv_path, newline="", encoding="utf-8") as f:
            reader = csv.reader(f)
            header = next(reader, None)
        if header is None:
            return
        if "strategy_active" in header:
            return  # already v2
    except (OSError, StopIteration):
        return
    legacy = csv_path.replace("regime_history.csv", "regime_history_legacy.csv")
    try:
        os.replace(csv_path, legacy)
        logger.info("[REGIME] Migrated v1 history → %s", legacy)
    except OSError as e:
        logger.warning("[REGIME] Could not migrate history: %s", e)


def append_history(
    csv_path: str,
    session_date: str,
    state: RegimeState,
    rec: Recommendation,
    *,
    strategy_active: str = "",
    applied: bool = False,
    applied_at: str = "",
    trading_mode: str = "",
):
    """Append a classification row to regime_history.csv (v2 schema)."""
    exists = os.path.exists(csv_path)
    with open(csv_path, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if not exists:
            w.writerow(_V2_HEADER)
        feat = state.last_features
        w.writerow([
            session_date, "NIGHT",
            round(feat.get("adx", 0), 1),
            round(feat.get("plus_di", 0), 1),
            round(feat.get("minus_di", 0), 1),
            round(feat.get("atr_ratio", 0), 2),
            round(feat.get("ema_slope", 0), 1),
            feat.get("last_close", ""),
            state.raw_regime, state.effective_regime,
            state.pending_count,
            rec.action, rec.strategy_name,
            "", state.manual_override,
            "", "",  # pnl, trades — filled by record_session_result
            strategy_active,
            str(applied).lower(), applied_at, trading_mode,
        ])


def record_session_result(
    csv_path: str,
    session_date: str,
    session_slot: str,
    pnl: float,
    trades: int,
    strategy_active: str = "",
    trading_mode: str = "",
):
    """Record a session's P&L directly from the switching runner's broker.

    For NIGHT sessions where a classification row already exists (pnl
    column blank), backfills that row.  For DAY sessions (or NIGHT
    sessions with no prior classification row), appends a standalone
    result row with ``decision=""``.
    """
    if not os.path.exists(csv_path):
        # Create with header + standalone row
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(_V2_HEADER)
            w.writerow(_result_row(session_date, session_slot, pnl, trades,
                                   strategy_active, trading_mode))
        return

    with open(csv_path, newline="", encoding="utf-8") as f:
        rows = list(csv.reader(f))

    # Try to backfill an existing classification row (matching date+session, blank pnl)
    backfilled = False
    session_col = _V2_HEADER.index("session")  # 1
    pnl_col = _V2_HEADER.index("pnl")         # 15
    trades_col = _V2_HEADER.index("trades")    # 16
    active_col = _V2_HEADER.index("strategy_active")  # 17
    mode_col = _V2_HEADER.index("trading_mode")        # 20
    for i in range(len(rows) - 1, 0, -1):
        if (len(rows[i]) > pnl_col
                and rows[i][0] == session_date
                and rows[i][session_col] == session_slot
                and rows[i][pnl_col] == ""):
            rows[i][pnl_col] = str(pnl)
            rows[i][trades_col] = str(trades)
            if len(rows[i]) > active_col and not rows[i][active_col]:
                rows[i][active_col] = strategy_active
            if len(rows[i]) > mode_col and not rows[i][mode_col]:
                rows[i][mode_col] = trading_mode
            backfilled = True
            break

    if backfilled:
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerows(rows)
    else:
        with open(csv_path, "a", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(
                _result_row(session_date, session_slot, pnl, trades,
                            strategy_active, trading_mode))


def _result_row(date, session, pnl, trades, strategy_active, trading_mode):
    """Build a standalone result row (no classification data)."""
    return [
        date, session,
        "", "", "", "", "", "",   # adx..close — no classification
        "", "",                   # raw/effective regime
        "",                       # confirm_count
        "", "",                   # decision, strategy_deployed
        "", "",                   # dry_run, override
        str(pnl), str(trades),
        strategy_active, "", "", trading_mode,
    ]
