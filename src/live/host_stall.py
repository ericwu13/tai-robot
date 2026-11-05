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
- ``host_stall.claim.<N>`` — generation N of the stall rollup. The
  file is created with ``os.link`` of a finished temp file (``O_EXCL``
  when hard links are unavailable), so exactly one writer publishes
  that generation. Older generations are left in place.
- ``host_stall.recovery.<N>`` — the recovery rollup for generation N,
  created the same way.

A missing file is an empty marker. JSON that does not parse, or an
episode without a numeric ``hung_at``, is ignored and logged once.
Nothing in this module renames or unlinks a claim name. A new incident
publishes the next generation instead. No settings key.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile

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
_CLAIM_GEN = re.compile(r"^host_stall\.claim\.(\d+)$")
_RECOVERY_GEN = re.compile(r"^host_stall\.recovery\.(\d+)$")
_corrupt_logged: set[str] = set()

# Test seam. Called after a new generation number is chosen and before
# the exclusive create. None in production.
_before_claim_create = None


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


def _window(episodes: dict[str, dict], *, end: float) -> list[dict]:
    """Episodes within ``HOST_STALL_WINDOW_S`` of ``end`` on either side.

    Anchoring on a fixed ``end`` keeps an older incident visible after a
    later hang. Anchoring on the newest ``hung_at`` would drop that
    incident and swallow its recovery rollup.
    """
    lo = end - HOST_STALL_WINDOW_S
    hi = end + HOST_STALL_WINDOW_S
    grouped = [
        ep for ep in episodes.values()
        if lo <= ep["hung_at"] <= hi
    ]
    grouped.sort(key=lambda ep: (ep["hung_at"], ep["bot"]))
    return grouped


def _cluster(episodes: dict[str, dict]) -> list[dict]:
    """The stall window that ends at the newest ``hung_at``."""
    if not episodes:
        return []
    latest = max(ep["hung_at"] for ep in episodes.values())
    return _window(episodes, end=latest)


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
    claims = _claims(root)
    rollup = claims[-1][1] if claims else None
    recovery = None
    if claims:
        recovery = _read_json(_recovery_file(root, claims[-1][0]))
    payload = {
        "v": 1,
        "window_s": HOST_STALL_WINDOW_S,
        "min_bots": HOST_STALL_MIN_BOTS,
        "episodes": episodes,
        "rollup": rollup,
        "recovery": recovery,
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


def _generations(root: str, pattern: re.Pattern) -> list[tuple[int, str]]:
    try:
        names = os.listdir(root)
    except FileNotFoundError:
        return []
    except OSError:
        logger.exception("host stall claim list failed")
        return []
    found = []
    for name in names:
        match = pattern.match(name)
        if match:
            found.append((int(match.group(1)), os.path.join(root, name)))
    found.sort()
    return found


def _claim_file(root: str, generation: int) -> str:
    return os.path.join(root, f"{CLAIM_FILENAME}.{generation}")


def _recovery_file(root: str, generation: int) -> str:
    return os.path.join(root, f"{RECOVERY_FILENAME}.{generation}")


def _claims(root: str) -> list[tuple[int, dict]]:
    """Readable claim generations, oldest first. Unreadable files stay."""
    out = []
    for generation, path in _generations(root, _CLAIM_GEN):
        data = _read_json(path)
        if isinstance(data, dict) and _claim_bots(data):
            out.append((generation, data))
    return out


def _covers(claim: dict, cluster: list[dict]) -> bool:
    """True when this claim already announced ``cluster``."""
    return bool(_claim_bots(claim) & {ep["bot"] for ep in cluster})


def _incident(episodes: dict[str, dict], claim: dict) -> list[dict]:
    """Bots in the claim's own window, not the newest hang on the host.

    The window is ``[end - 120s, end + 120s]`` around the claim's latest
    ``hung_at``. A bot that hangs more than 120 s later is a new incident
    and does not erase this one's recovery.
    """
    hung = claim.get("hung_at")
    times = []
    if isinstance(hung, dict):
        for value in hung.values():
            number = _number(value)
            if number is not None:
                times.append(number)
    if not times:
        return []
    return _window(episodes, end=max(times))


def _publish_claim(path: str, payload: dict) -> bool:
    hook = _before_claim_create
    if hook is not None:
        hook()
    return _claim(path, payload)


def _maybe_send_stall(root: str, episodes: dict[str, dict], now: float, alert_fn) -> None:
    cluster = _cluster(episodes)
    if len(cluster) < HOST_STALL_MIN_BOTS:
        return
    # A writer can observe "no covering claim", then another writer can
    # link the generation this one was about to choose. Retry. If this
    # writer still wins a higher generation, it does not alert when an
    # earlier generation already covers the same bots, and it deletes
    # only the file it just created.
    for _attempt in range(8):
        if any(_covers(claim, cluster) for _gen, claim in _claims(root)):
            return
        numbers = [gen for gen, _path in _generations(root, _CLAIM_GEN)]
        generation = (max(numbers) if numbers else 0) + 1
        path = _claim_file(root, generation)
        payload = {
            "generation": generation,
            "bots": [ep["bot"] for ep in cluster],
            "span_s": (
                max(ep["hung_at"] for ep in cluster)
                - min(ep["hung_at"] for ep in cluster)
            ),
            "hung_at": {ep["bot"]: ep["hung_at"] for ep in cluster},
            "sent_at": now,
        }
        try:
            won = _publish_claim(path, payload)
        except OSError:
            logger.exception("host stall claim failed")
            return
        if not won:
            continue
        earlier = [
            claim for gen, claim in _claims(root)
            if gen < generation and _covers(claim, cluster)
        ]
        if earlier:
            try:
                os.unlink(path)
            except OSError:
                logger.exception("host stall redundant claim remove failed")
            return
        _emit(alert_fn, _stall_message(cluster))
        return


def _maybe_send_recovery(root: str, episodes: dict[str, dict], alert_fn) -> None:
    for generation, claim in _claims(root):
        path = _recovery_file(root, generation)
        if os.path.exists(path):
            continue
        # The claim's own window, including a bot that hung just after the
        # claim. A hang more than 120 s later is not a member, so it cannot
        # swallow this "K/K recovered".
        members = _incident(episodes, claim)
        if len(members) < HOST_STALL_MIN_BOTS:
            continue
        if any(ep.get("recovered_at") is None for ep in members):
            continue
        names = [ep["bot"] for ep in members]
        stalls = [
            float(ep["stall_s"]) for ep in members
            if ep.get("stall_s") is not None
        ]
        max_stall = max(stalls) if stalls else 0.0
        payload = {
            "generation": generation,
            "bots": names,
            "max_stall_s": max_stall,
            "sent_at": max(float(ep["recovered_at"]) for ep in members),
        }
        try:
            won = _claim(path, payload)
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
