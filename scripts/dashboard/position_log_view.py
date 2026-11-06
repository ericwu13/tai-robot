"""``GET /position-log/<bot_id>`` — the bot's own position log, read-only.

Reads at most ``_TAIL_BYTES`` from the end of
``<bot_dir>/position_log.jsonl`` via :func:`read_records`. Every event in
that window is projected to a logical #250 record, and ``limit`` then
keeps the last that many logical rows. Trailing ORDER lines do not hide
an earlier fill that is still inside the window. ``bot_position`` is
``bot_position_after`` from those fills, never the account net. The
account number is masked to its last 4 digits, as the dashboard does
everywhere else.
"""
from __future__ import annotations

import os

from src.live.position_log import POSITION_LOG_NAME, is_mismatch, read_records

DEFAULT_LIMIT = 200
MAX_LIMIT = 2000
_TAIL_BYTES = 2_000_000


def clamp_limit(limit: int | None) -> int:
    if not limit:
        return DEFAULT_LIMIT
    return max(1, min(MAX_LIMIT, limit))


def mask_account(account) -> str:
    text = str(account or "")
    return ("*" * max(0, len(text) - 4)) + text[-4:] if text else ""


def position_log(bot_dir: str, bot_id: str, limit: int = DEFAULT_LIMIT) -> dict:
    path = os.path.join(bot_dir, POSITION_LOG_NAME)
    records = []
    for rec in read_records(path, clamp_limit(limit), max_bytes=_TAIL_BYTES):
        view = dict(rec)
        view["account"] = mask_account(view.get("account"))
        view["mismatch"] = is_mismatch(view)
        records.append(view)
    last = records[-1] if records else None
    mismatches = [r for r in records if r["mismatch"]]
    return {
        "bot_id": bot_id,
        "records": records,                       # oldest first
        "count": len(records),
        "bot_position": last.get("bot_position_after") if last else None,
        "net_position": last.get("net_position_after") if last else None,
        "mismatch_count": len(mismatches),
        "last_mismatch": mismatches[-1] if mismatches else None,
    }
