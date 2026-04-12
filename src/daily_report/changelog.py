"""Strategy changelog: tracks every strategy change with before/after metrics.

Stored as ``data/changelog.json`` — a JSON array of change entries.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

_CHANGELOG_PATH = Path("data/changelog.json")


def _load_raw() -> list[dict]:
    if not _CHANGELOG_PATH.exists():
        return []
    try:
        data = json.loads(_CHANGELOG_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _save_raw(entries: list[dict]) -> None:
    _CHANGELOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    _CHANGELOG_PATH.write_text(
        json.dumps(entries, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def append_changelog(
    strategy_name: str,
    version_before: str,
    version_after: str,
    change_summary: str,
    initiated_by: str = "human",
    metrics_before: dict | None = None,
    metrics_after: dict | None = None,
    params_before: dict | None = None,
    params_after: dict | None = None,
) -> dict:
    """Append a new entry to the strategy changelog.

    Parameters
    ----------
    strategy_name : human-readable strategy display name
    version_before / version_after : version strings
    change_summary : free-text description of what changed
    initiated_by : "human" or "ai"
    metrics_before / metrics_after : performance metrics dicts (optional)
    params_before / params_after : strategy parameter dicts (optional)

    Returns
    -------
    dict : the new entry (also persisted to disk)
    """
    entries = _load_raw()

    entry = {
        "date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "strategy": strategy_name,
        "version_before": version_before,
        "version_after": version_after,
        "change_summary": change_summary,
        "initiated_by": initiated_by,
        "metrics_before": metrics_before,
        "metrics_after": metrics_after,
        "params_before": params_before,
        "params_after": params_after,
    }

    entries.append(entry)
    _save_raw(entries)
    return entry


def load_changelog() -> list[dict]:
    """Load the full changelog."""
    return _load_raw()


def recent_changes(n: int = 10) -> list[dict]:
    """Return the most recent *n* changelog entries (newest first)."""
    entries = _load_raw()
    return list(reversed(entries[-n:]))
