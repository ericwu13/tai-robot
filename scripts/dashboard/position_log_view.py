"""``GET /position-log/<bot_id>`` — the bot's own position log, read-only.

Reads ``<bot_dir>/position_log.jsonl`` (written by
``src/live/position_log.py``) through ``fileio`` so the bot's appends are
never blocked. The account number is masked to its last 4 digits, as the
dashboard does everywhere else; nothing else is altered.
"""
from __future__ import annotations

import json
import os

from scripts.dashboard import fileio
from src.live.position_log import POSITION_LOG_NAME, is_mismatch

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
    for line in fileio.tail_lines(path, clamp_limit(limit), _TAIL_BYTES):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if not isinstance(rec, dict):
            continue
        rec["account"] = mask_account(rec.get("account"))
        rec["mismatch"] = is_mismatch(rec)
        records.append(rec)
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
