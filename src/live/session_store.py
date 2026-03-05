"""Session persistence for live trading: save/load broker state + metadata.

Session file: ``{bot_dir}/session.json``
Auto-saved on every trade event so state survives crashes.
"""

from __future__ import annotations

import json
import os
from datetime import datetime


def save_session(path: str, data: dict) -> None:
    """Atomically write session data to a JSON file."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False, default=str)
    # Atomic rename (Windows: replaces existing)
    try:
        os.replace(tmp, path)
    except OSError:
        # Fallback for edge cases
        if os.path.exists(path):
            os.remove(path)
        os.rename(tmp, path)


def load_session(path: str) -> dict | None:
    """Load session data from a JSON file. Returns None if not found or corrupt."""
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def session_summary(data: dict) -> str:
    """Return a human-readable summary of a saved session."""
    broker = data.get("broker", {})
    trades = broker.get("trades", [])
    pnl = broker.get("_cumulative_pnl", 0)
    pos = broker.get("position_size", 0)
    side = broker.get("position_side", "")
    started = data.get("started_at", "?")
    saved = data.get("saved_at", "?")

    lines = [
        f"Started: {started}",
        f"Last saved: {saved}",
        f"Trades: {len(trades)}",
        f"P&L: {pnl:+,}",
    ]
    if pos > 0:
        lines.append(f"Open position: {side} @ {broker.get('entry_price', 0):,}")
    else:
        lines.append("Open position: Flat")
    return "\n".join(lines)
