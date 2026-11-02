"""W2 monitor liveness file.

The cross-market monitor rewrites this file on every cycle. ``signal.json``
cannot stand in for it: that file is written only when a breach fires, so a
quiet monitor and a dead monitor look the same.

``DEFAULT_MAX_HEARTBEAT_AGE_SEC`` is the one definition of the age limit.
Eric confirmed 900 seconds (15 minutes) on 2026-11-02. Callers must read
it from here — NewsConfig's field default, the settings loader, and the
resolver all do.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

# Confirmed by Eric on 2026-11-02. Do not copy this number into settings,
# the runner, or the monitor. Change it here if a later decision says
# 15 minutes is wrong.
DEFAULT_MAX_HEARTBEAT_AGE_SEC = 900

HEARTBEAT_VERSION = 1
_TZ_TAIPEI = timezone(timedelta(hours=8))
# Same skew window as signal_file.CLOCK_SKEW_SEC. A stamp far in the
# future is not a live monitor.
_FUTURE_SKEW_SEC = 120


def write_heartbeat(
    path: str | os.PathLike,
    now: datetime | None = None,
    *,
    source: str = "w2",
) -> None:
    """Atomically rewrite the heartbeat file. Raises on I/O failure.

    The monitor has its own stdlib copy of this schema so the bridge
    process does not import the bot. Tests pin the two writers against
    :func:`read_heartbeat`.
    """
    if now is None:
        now = datetime.now(_TZ_TAIPEI)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=_TZ_TAIPEI)
    payload = {
        "version": HEARTBEAT_VERSION,
        "source": source,
        "written_at": now.isoformat(),
    }
    path = str(path)
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def read_heartbeat(
    path: str | os.PathLike | None,
    now: datetime | None = None,
    max_age_sec: int = DEFAULT_MAX_HEARTBEAT_AGE_SEC,
) -> tuple[float | None, str]:
    """Return ``(age_sec, status)``.

    status is ``unconfigured`` (no path), ``missing``, ``corrupt``,
    ``stale`` (age >= max, or a future stamp), or ``ok``.

    Age comes from ``written_at`` when that stamp parses. A naive stamp
    is corrupt, matching the signal file: the monitor host is not
    assumed to be local. ``FileNotFoundError`` from ``open`` (the file
    disappeared between the exists check and the read) is ``missing``,
    not ``corrupt``. Never raises.
    """
    if now is None:
        now = datetime.now(_TZ_TAIPEI)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=_TZ_TAIPEI)
    if not path:
        return None, "unconfigured"
    path = str(path)
    if not os.path.exists(path):
        return None, "missing"
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return None, "missing"
    except (OSError, ValueError):
        return None, "corrupt"
    if not isinstance(data, dict) or data.get("version") != HEARTBEAT_VERSION:
        return None, "corrupt"
    raw = data.get("written_at")
    if not isinstance(raw, str):
        return None, "corrupt"
    try:
        stamp = datetime.fromisoformat(raw)
    except ValueError:
        return None, "corrupt"
    if stamp.tzinfo is None:
        return None, "corrupt"
    age = (now - stamp.astimezone(now.tzinfo)).total_seconds()
    if age < -_FUTURE_SKEW_SEC or age >= max_age_sec:
        return age, "stale"
    return age, "ok"
