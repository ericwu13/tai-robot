"""Persistence for regime shadow mode: state JSON + history CSV.

- ``regime_state.json`` — the latest RegimeState plus the pending
  next-session recommendation (atomic write via temp + os.replace).
- ``regime_history.csv`` — one row per assessed session, with P&L
  backfilled once the following session's daily report arrives.
"""

import csv
import json
import os

from .state_machine import RegimeState
from .selector import Recommendation


def load_state(path: str) -> RegimeState:
    if not os.path.exists(path):
        return RegimeState()
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
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
        "reason": rec.reason, "dry_run": rec.dry_run,
        "executed": False, "executed_at": None,
    }
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def append_history(csv_path: str, session_date: str, state: RegimeState, rec: Recommendation):
    exists = os.path.exists(csv_path)
    with open(csv_path, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if not exists:
            w.writerow(["date", "session", "adx", "plus_di", "minus_di", "atr_ratio",
                        "ema_slope", "close", "raw_regime", "effective_regime",
                        "confirm_count", "decision", "strategy_deployed",
                        "dry_run", "override", "pnl", "trades"])
        # Feature keys mirror RegimeResult.to_dict(): adx / plus_di / minus_di /
        # atr_ratio / ema_slope / last_close (NOT the *_value dataclass attrs).
        feat = state.last_features
        w.writerow([session_date, "NIGHT",
                    round(feat.get("adx", 0), 1),
                    round(feat.get("plus_di", 0), 1),
                    round(feat.get("minus_di", 0), 1),
                    round(feat.get("atr_ratio", 0), 2),
                    round(feat.get("ema_slope", 0), 1),
                    feat.get("last_close", ""),
                    state.raw_regime, state.effective_regime,
                    state.pending_count,
                    rec.action, rec.strategy_name,
                    rec.dry_run, state.manual_override,
                    "", ""])


def backfill_pnl(csv_path: str, session_date: str, pnl: float, trades: int):
    """Backfill P&L for the most recent row matching session_date."""
    if not os.path.exists(csv_path):
        return
    with open(csv_path, newline="", encoding="utf-8") as f:
        rows = list(csv.reader(f))
    for i in range(len(rows) - 1, 0, -1):
        if rows[i][0] == session_date and rows[i][-2] == "":
            rows[i][-2] = str(pnl)
            rows[i][-1] = str(trades)
            break
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerows(rows)
