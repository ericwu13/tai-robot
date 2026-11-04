"""Host-wide hang rollup (issue #233).

Each bot's watchdog sees only its own process. Bots on one deploy root
(the parent of the bot directories, ``data/live``) share a marker so a
stall that hits several processes at once becomes one alert.

The rollup is additive. It never suppresses a bot's own P1.

Marker layout, all under the deploy root:

- ``host_stall.json`` — readable snapshot (episodes + rollup state).
  Rewritten atomically. Decisions do not trust it: a concurrent writer
  can leave it one publish behind, and the next publish rebuilds it.
- ``host_stall.d/<bot>.json`` — one episode per bot. A bot writes only
  its own file (temp file + ``os.replace``), so two bots cannot tear
  each other's episode.
- ``host_stall.claim`` — exclusive record that the stall rollup was
  sent. Created with ``os.link`` onto a finished temp file, so the name
  appears only once the JSON is complete. ``FileExistsError`` means
  another process already sent it.
- ``host_stall.recovery`` — the same gate for the recovery rollup.

A missing file is an empty marker. JSON that does not parse, or an
episode without a numeric ``hung_at``, is ignored and logged once.
Episodes older than ``HOST_STALL_STALE_S`` do not count. A claim whose
bots are outside the current 120 s window (recovered, crashed, or
older) is renamed aside so the next incident can alert. The rename is
the only removal: a process that loses the race does not unlink the
winner's new claim. No settings key.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import time

logger = logging.getLogger(__name__)

HOST_STALL_WINDOW_S = 120.0
HOST_STALL_MIN_BOTS = 3
# Long enough that a host stall still open after the 10-minute re-alert
# can still roll its recovery up, and short enough that a crashed bot's
# marker does not block the next incident all day.
HOST_STALL_STALE_S = 1800.0

MARKER_FILENAME = "host_stall.json"
EPISODE_DIRNAME = "host_stall.d"
CLAIM_FILENAME = "host_stall.claim"
RECOVERY_FILENAME = "host_stall.recovery"

_BOT_SAFE = re.compile(r"[^A-Za-z0-9._-]")
_corrupt_logged: set[str] = set()


def _warn_corrupt(path: str) -> None:
    if path in _corrupt_logged:
        return
    _corrupt_logged.add(path)
    logger.warning(
        "host stall marker unreadable, ignoring it: %s",
        os.path.basename(path) or path,
    )


def deploy_root(bot_dir: str) -> str | None:
    """Parent of ``bot_dir``. ``None`` when there is nowhere safe to write."""
    if not bot_dir:
        return None
    root = os.path.dirname(os.path.abspath(bot_dir))
    if not root or os.path.abspath(root) == os.path.abspath(bot_dir):
        return None
    return root


def _bot_name(bot_dir: str) -> str:
    return os.path.basename(os.path.abspath(bot_dir)) or "bot"


def _episode_filename(bot: str) -> str:
    cleaned = _BOT_SAFE.sub("_", bot).strip("._")
    if not cleaned or cleaned in {".", ".."}:
        cleaned = "bot"
    return cleaned + ".json"


def _marker_path(root: str) -> str:
    return os.path.join(root, MARKER_FILENAME)


def _episode_dir(root: str) -> str:
    return os.path.join(root, EPISODE_DIRNAME)


def _episode_path(root: str, bot: str) -> str:
    return os.path.join(_episode_dir(root), _episode_filename(bot))


def _claim_path(root: str) -> str:
    return os.path.join(root, CLAIM_FILENAME)


def _recovery_path(root: str) -> str:
    return os.path.join(root, RECOVERY_FILENAME)


def _read_json(path: str) -> dict | None:
    """A JSON object, or None when the file is missing or unusable."""
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        _warn_corrupt(path)
        return None
    if not isinstance(data, dict):
        _warn_corrupt(path)
        return None
    return data


def _number(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _normalize(bot: str, raw: dict) -> dict | None:
    hung_at = _number(raw.get("hung_at"))
    if hung_at is None:
        return None
    pid = raw.get("pid")
    if isinstance(pid, bool) or not isinstance(pid, int):
        pid = None
    return {
        "bot": str(raw.get("bot") or bot),
        "pid": pid,
        "hung_at": hung_at,
        "reason": str(raw.get("reason") or ""),
        "real": bool(raw.get("real")),
        "recovered_at": _number(raw.get("recovered_at")),
        "stall_s": _number(raw.get("stall_s")),
    }


def read_episodes(root: str, now: float) -> dict[str, dict]:
    """Episodes still inside the stale horizon, keyed by bot name.

    Corrupt files and episodes older than ``HOST_STALL_STALE_S`` are
    skipped. They are left on disk: deleting a file we just failed to
    parse can race a concurrent replace of that same name.
    """
    directory = _episode_dir(root)
    found: dict[str, dict] = {}
    try:
        names = os.listdir(directory)
    except FileNotFoundError:
        return found
    except OSError:
        logger.exception("host stall episode list failed")
        return found
    for name in names:
        if not name.endswith(".json"):
            continue
        path = os.path.join(directory, name)
        raw = _read_json(path)
        if raw is None:
            continue
        bot = str(raw.get("bot") or name[:-5])
        episode = _normalize(bot, raw)
        if episode is None:
            _warn_corrupt(path)
            continue
        if now - episode["hung_at"] > HOST_STALL_STALE_S:
            continue
        found[episode["bot"]] = episode
    return found


def _cluster(episodes: dict[str, dict]) -> list[dict]:
    """Episodes whose ``hung_at`` values sit inside one window.

    The window ends at the newest ``hung_at``. A hang that started more
    than ``HOST_STALL_WINDOW_S`` before that is a different incident.
    """
    if not episodes:
        return []
    latest = max(ep["hung_at"] for ep in episodes.values())
    grouped = [
        ep for ep in episodes.values()
        if latest - ep["hung_at"] <= HOST_STALL_WINDOW_S
    ]
    grouped.sort(key=lambda ep: (ep["hung_at"], ep["bot"]))
    return grouped


def _write_json_atomic(path: str, payload: dict) -> None:
    from src.live.main_heartbeat import atomic_write_json
    atomic_write_json(path, payload)


def _claim_exclusive(path: str, payload: dict) -> bool:
    """Create ``path`` with ``O_EXCL`` when ``os.link`` is unavailable."""
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    try:
        fd = os.open(path, flags, 0o644)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False)
        handle.flush()
    return True


def _claim(path: str, payload: dict) -> bool:
    """Publish ``payload`` at ``path`` exactly once.

    The temp file is written to completion, then linked onto ``path``.
    The link either creates the name or raises ``FileExistsError``.
    Readers never observe a half-written claim. A filesystem that cannot
    link falls back to an exclusive create of the finished bytes.
    """
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".host-stall-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
            handle.flush()
        try:
            os.link(tmp, path)
        except FileExistsError:
            return False
        except OSError:
            logger.warning(
                "host stall claim link failed for %s; using exclusive create",
                os.path.basename(path),
            )
            return _claim_exclusive(path, payload)
        return True
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _stall_message(cluster: list[dict]) -> str:
    span = max(ep["hung_at"] for ep in cluster) - min(ep["hung_at"] for ep in cluster)
    return (
        f"[HANG] host stall: {len(cluster)} bots stalled within {span:.0f} s; "
        f"per-bot alerts were sent"
    )


def _recovery_message(count: int, max_stall_s: float) -> str:
    return (
        f"[HANG] host stall: {count}/{count} recovered, "
        f"max stall {max_stall_s:.0f} s"
    )


def _emit(alert_fn, message: str) -> None:
    logger.warning("%s", message)
    if alert_fn is None:
        return
    try:
        alert_fn(message)
    except Exception:
        logger.exception("host stall rollup alert failed")


def _write_mirror(root: str, episodes: dict[str, dict]) -> None:
    payload = {
        "v": 1,
        "window_s": HOST_STALL_WINDOW_S,
        "min_bots": HOST_STALL_MIN_BOTS,
        "episodes": episodes,
        "rollup": _read_json(_claim_path(root)),
        "recovery": _read_json(_recovery_path(root)),
    }
    try:
        _write_json_atomic(_marker_path(root), payload)
    except Exception:
        logger.exception("host stall marker write failed")


def _claim_bots(claim) -> set[str]:
    bots = claim.get("bots") if isinstance(claim, dict) else None
    if not isinstance(bots, list):
        return set()
    return {str(bot) for bot in bots}


def _covers(claim, cluster: list[dict]) -> bool:
    """True when ``claim`` already announced this window.

    Bots outside the window (recovered, crashed, or older than 120 s)
    do not keep their claim. That used to block the next rollup for up
    to ``HOST_STALL_STALE_S``.
    """
    names = _claim_bots(claim)
    if not names:
        return False
    return bool(names & {ep["bot"] for ep in cluster})


def _read_bytes(path: str) -> bytes | None:
    try:
        with open(path, "rb") as handle:
            return handle.read()
    except FileNotFoundError:
        return None
    except OSError:
        logger.exception("host stall claim read failed")
        return None


def _restore_claim(dest: str, path: str) -> None:
    """Put ``dest`` back on the claim name if we renamed the wrong file."""
    try:
        os.link(dest, path)
        return
    except FileExistsError:
        return
    except OSError:
        pass
    try:
        os.rename(dest, path)
    except OSError:
        logger.exception("host stall claim restore failed")


def _retire_if_bytes(root: str, expected: bytes) -> str:
    """Rename the claim aside only when it is still ``expected``.

    A rename that moves a newer claim (the other writer's) is undone.
    Returns ``retired``, ``absent``, ``changed``, ``lost``, or ``error``.
    """
    path = _claim_path(root)
    current = _read_bytes(path)
    if current is None:
        return "absent" if not os.path.lexists(path) else "error"
    if current != expected:
        return "changed"
    dest = f"{path}.retired-{time.time_ns()}"
    try:
        os.rename(path, dest)
    except FileNotFoundError:
        return "lost"
    except OSError:
        logger.exception("host stall claim retire failed")
        return "error"
    moved = _read_bytes(dest)
    if moved != expected:
        _restore_claim(dest, path)
        return "changed"
    recovery = _recovery_path(root)
    if os.path.lexists(recovery):
        try:
            os.rename(recovery, f"{recovery}.retired-{time.time_ns()}")
        except OSError:
            pass
    return "retired"


def _release_stale_claim(root: str, cluster: list[dict]) -> bool:
    """True when this caller may try to create the stall claim.

    ``False`` means a claim for this window is already in place, or this
    caller moved the wrong file and put it back. It must not unlink.
    """
    path = _claim_path(root)
    blob = _read_bytes(path)
    if blob is None:
        return not os.path.lexists(path)
    try:
        claim = json.loads(blob.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        claim = None
    if _covers(claim, cluster):
        return False
    status = _retire_if_bytes(root, blob)
    if status in ("retired", "absent"):
        return True
    if status == "lost":
        # The other writer retired the same bytes. Link dedupes the create
        # if they have not published the replacement yet.
        return not os.path.lexists(path)
    return False


def _maybe_send_stall(root: str, episodes: dict[str, dict], now: float, alert_fn) -> None:
    cluster = _cluster(episodes)
    if len(cluster) < HOST_STALL_MIN_BOTS:
        return
    if not _release_stale_claim(root, cluster):
        return
    if os.path.exists(_claim_path(root)):
        return
    payload = {
        "bots": [ep["bot"] for ep in cluster],
        "span_s": (
            max(ep["hung_at"] for ep in cluster)
            - min(ep["hung_at"] for ep in cluster)
        ),
        "hung_at": {ep["bot"]: ep["hung_at"] for ep in cluster},
        "sent_at": now,
    }
    try:
        won = _claim(_claim_path(root), payload)
    except OSError:
        logger.exception("host stall claim failed")
        return
    if won:
        _emit(alert_fn, _stall_message(cluster))


def _maybe_send_recovery(root: str, episodes: dict[str, dict], alert_fn) -> None:
    claim_path = _claim_path(root)
    if not os.path.exists(claim_path):
        return
    if os.path.exists(_recovery_path(root)):
        return
    # Every bot still inside the stall window, including one that hung
    # after the claim was written. "3/3" while a fourth is HUNG is wrong.
    cluster = _cluster(episodes)
    if len(cluster) < HOST_STALL_MIN_BOTS:
        return
    claim = _read_json(claim_path)
    if not _covers(claim, cluster):
        return
    if any(ep.get("recovered_at") is None for ep in cluster):
        return
    names = [ep["bot"] for ep in cluster]
    stalls = [float(ep["stall_s"]) for ep in cluster if ep.get("stall_s") is not None]
    max_stall = max(stalls) if stalls else 0.0
    payload = {
        "bots": names,
        "max_stall_s": max_stall,
        "sent_at": max(float(ep["recovered_at"]) for ep in cluster),
    }
    try:
        won = _claim(_recovery_path(root), payload)
    except OSError:
        logger.exception("host stall recovery claim failed")
        return
    if won:
        _emit(alert_fn, _recovery_message(len(names), max_stall))


def record_hung(bot_dir: str, *, reason: str, real: bool, now: float,
                pid: int, alert_fn=None) -> None:
    """Remember that ``bot_dir`` just went HUNG. Maybe send the stall rollup.

    The per-bot P1 is the caller's job and must already have been sent.
    """
    root = deploy_root(bot_dir)
    if root is None:
        return
    bot = _bot_name(bot_dir)
    episode = {
        "bot": bot,
        "pid": int(pid),
        "hung_at": float(now),
        "reason": str(reason or ""),
        "real": bool(real),
        "recovered_at": None,
        "stall_s": None,
    }
    try:
        _write_json_atomic(_episode_path(root, bot), episode)
    except Exception:
        logger.exception("host stall episode write failed")
        return
    # Two scans. A replace that landed between them is picked up, and the
    # writer whose replace returned last sees every episode already linked.
    episodes = read_episodes(root, float(now))
    again = read_episodes(root, float(now))
    if len(again) >= len(episodes):
        episodes = again
    try:
        _maybe_send_stall(root, episodes, float(now), alert_fn)
    finally:
        _write_mirror(root, read_episodes(root, float(now)))


def record_recovered(bot_dir: str, *, stall_s: float, recovered_at: float,
                     now: float, alert_fn=None) -> None:
    """Mark this bot recovered. Maybe send the one recovery rollup."""
    root = deploy_root(bot_dir)
    if root is None:
        return
    bot = _bot_name(bot_dir)
    path = _episode_path(root, bot)
    if not os.path.isfile(path):
        # No hang was recorded. Do not invent an episode.
        return
    current = _read_json(path) or {}
    episode = _normalize(bot, current) or {
        "bot": bot,
        "pid": None,
        "hung_at": float(now),
        "reason": "",
        "real": False,
        "recovered_at": None,
        "stall_s": None,
    }
    episode["bot"] = bot
    episode["recovered_at"] = float(recovered_at)
    episode["stall_s"] = max(0.0, float(stall_s))
    try:
        _write_json_atomic(path, episode)
    except Exception:
        logger.exception("host stall recovery write failed")
        return
    episodes = read_episodes(root, float(now))
    try:
        _maybe_send_recovery(root, episodes, alert_fn)
    finally:
        _write_mirror(root, read_episodes(root, float(now)))
