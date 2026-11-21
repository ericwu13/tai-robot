"""W1 calendar bridge — auto-fetches official macro & earnings dates into events.json.

Fetches zero-API-key official public schedules via ``src/news/calendar_fetchers.py``
(Federal Reserve FOMC, US BLS CPI/NFP/PPI, US BEA PCE/GDP, Nasdaq Tier-1 AI/semi
earnings, TAIFEX monthly settlement) and merges local manual overrides from
``calendar_source.json`` (manual entries always take precedence).

Writes two files atomically into the bridge directory:
- ``events.json`` (schema v1, read by ``RegimeSwitchingRunner`` & Dashboard)
- ``calendar_state.json`` (sibling fetch-health sidecar + per-source cache)

Freshness safety: when ALL remote sources fail, ``events.json`` is NOT
overwritten with a fresh ``updated_at`` — ``calendar_state.json`` records the
failure streak and the script exits non-zero so ``runs/W1.jsonl`` and
``check_bridge.py`` alert immediately.

Backwards-compatible CLI:
    # Default (Steven daily 10:00 TPE task): auto-fetch + calendar_source.json overrides
    python calendar_bridge.py --out C:/n8n-bridge/events.json

    # Explicit local source file only (no network fetch; preserves legacy offline mode)
    python calendar_bridge.py --out C:/n8n-bridge/events.json --source my_events.json

    # Explicit local override file + live auto-fetch
    python calendar_bridge.py --out C:/n8n-bridge/events.json --source my_overrides.json --fetch

    # Preview fetched events without writing files
    python calendar_bridge.py --out C:/n8n-bridge/events.json --dry-run
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

_TZ_TAIPEI = timezone(timedelta(hours=8))
DEFAULT_SOURCE = Path(__file__).with_name("calendar_source.json")
STATE_FILENAME = "calendar_state.json"


def _ensure_repo_on_path() -> None:
    repo_root = str(Path(__file__).resolve().parents[2])
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)


class _NoRunLog:
    """Stand-in when src/news/bridge_runs.py cannot be loaded."""
    result = ""
    exit_code = None

    def fail(self, error):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _run_recorder(bridge_dir, workflow: str):
    """``bridge_runs.run_recorder`` (runs/<W>.jsonl for the dashboard's
    工作流程 view), loaded by file path like ``_get_write_regime_vote``.
    Bookkeeping only — a load failure degrades to a no-op."""
    try:
        import importlib.util
        mod_path = Path(__file__).resolve().parents[2] / "src" / "news" / "bridge_runs.py"
        spec = importlib.util.spec_from_file_location("bridge_runs", mod_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.run_recorder(bridge_dir, workflow)
    except Exception as e:  # noqa: BLE001
        print(f"  run log unavailable: {type(e).__name__}: {e}")
        return _NoRunLog()


def _atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = str(path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def _load_state(path: Path) -> dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _load_manual_source(path: Path, *, required: bool) -> tuple[list[dict] | None, str]:
    if not path.is_file():
        if required:
            return None, f"source file not found: {path}"
        return [], ""
    try:
        with open(path, encoding="utf-8") as f:
            events = json.load(f)
    except (OSError, ValueError) as exc:
        return None, f"failed to read source {path}: {exc}"
    if not isinstance(events, list):
        return None, f"source must be a JSON list of event entries, got {type(events)}"
    return events, ""


def _count_upcoming(events: list[dict], today_iso: str) -> tuple[int, int]:
    upcoming = [e for e in events if isinstance(e, dict) and str(e.get("date") or "") >= today_iso]
    upcoming_high = [e for e in upcoming if str(e.get("severity") or "").lower() == "high"]
    return len(upcoming), len(upcoming_high)


def main() -> int:
    ap = argparse.ArgumentParser(description="Publish events.json for the bot")
    ap.add_argument("--out", required=True, help="events.json path the bot reads")
    ap.add_argument(
        "--source",
        default=None,
        help="manual event override JSON list; passing --source without --fetch publishes only that file",
    )
    ap.add_argument("--config", default=None, help="calendar_sources.yaml override path")
    ap.add_argument("--fetch", action="store_true", help="enable live auto-fetch even when --source is set")
    ap.add_argument("--offline", action="store_true", help="publish local --source only (skip network fetch)")
    ap.add_argument("--dry-run", action="store_true", help="print events without writing files")
    args = ap.parse_args()
    with _run_recorder(Path(args.out).parent, "W1") as run:
        code, result = publish(args)
        run.exit_code = code
        run.result = result
        if code != 0:
            run.fail(result)
        return code


def publish(
    args,
    *,
    now: datetime | None = None,
    http_get=None,
) -> tuple[int, str]:
    """Build and atomically write ``events.json`` (+ ``calendar_state.json``).

    Returns ``(exit_code, one_line_result)``.
    """
    moment = now or datetime.now(_TZ_TAIPEI)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=_TZ_TAIPEI)
    moment_tpe = moment.astimezone(_TZ_TAIPEI)
    stamp = moment_tpe.isoformat(timespec="seconds")
    today_iso = moment_tpe.date().isoformat()

    explicit_source = getattr(args, "source", None)
    offline = bool(getattr(args, "offline", False))
    force_fetch = bool(getattr(args, "fetch", False))
    dry_run = bool(getattr(args, "dry_run", False))
    config_path = getattr(args, "config", None)

    # Preserve legacy offline contract when caller passes explicit --source without --fetch
    use_auto_fetch = (not offline) and (explicit_source is None or force_fetch)

    source_path = Path(explicit_source) if explicit_source else DEFAULT_SOURCE
    manual_events, err = _load_manual_source(source_path, required=bool(explicit_source) or not use_auto_fetch)
    if manual_events is None:
        print(err)
        return 1, err

    out = Path(args.out)
    state_path = out.parent / STATE_FILENAME

    if not use_auto_fetch:
        payload = {
            "version": 1,
            "updated_at": stamp,
            "events": manual_events,
        }
        if dry_run:
            print(f"[dry-run] would write {len(manual_events)} offline event(s) -> {out}")
            return 0, f"dry-run {len(manual_events)} event(s)"
        _atomic_write_json(out, payload)
        print(f"wrote {len(manual_events)} event(s) -> {out} (updated_at {stamp})")
        return 0, f"wrote {len(manual_events)} event(s)"

    _ensure_repo_on_path()
    from src.news.calendar_fetchers import (  # noqa: PLC0415
        build_calendar_events,
        load_calendar_config,
    )

    cfg = load_calendar_config(config_path)
    prev_state = _load_state(state_path)
    # None keeps each source's default: urllib for FOMC/BLS/BEA, curl_cffi
    # for Nasdaq. An injected http_get still covers every source.
    fetch_res = build_calendar_events(
        cfg,
        now=moment_tpe,
        manual_events=manual_events,
        prev_cached_by_source=prev_state.get("cached_by_source"),
        http_get=http_get,
    )

    upcoming_n, upcoming_high_n = _count_upcoming(fetch_res.events, today_iso)
    prev_streak = int(prev_state.get("error_streak") or 0)
    err_brief = "; ".join(f"{k}: {v}" for k, v in fetch_res.errors.items())[:300]

    new_state = {
        "last_check": stamp,
        "fetch_status": fetch_res.status,
        "sources_ok": list(fetch_res.sources_ok),
        "sources_failed": list(fetch_res.sources_failed),
        "error_streak": prev_streak + 1 if fetch_res.status == "failed" else 0,
        "last_error": err_brief if fetch_res.errors else "",
        "last_success_at": (
            prev_state.get("last_success_at") or ""
            if fetch_res.status == "failed"
            else stamp
        ),
        "event_count": len(fetch_res.events),
        "upcoming_count": upcoming_n,
        "upcoming_high_count": upcoming_high_n,
        "cached_by_source": fetch_res.cached_by_source,
    }

    if dry_run:
        print(
            f"[dry-run] status={fetch_res.status} ok={fetch_res.sources_ok} "
            f"failed={fetch_res.sources_failed} | {len(fetch_res.events)} events "
            f"({upcoming_n} upcoming, {upcoming_high_n} high)"
        )
        for ev in fetch_res.events:
            print(
                f"  {ev['date']} [{ev['severity']:6s}] {','.join(ev['sessions']):9s} "
                f"{ev['name']} ({ev.get('source', '')})"
            )
        return (0 if fetch_res.status != "failed" else 1), f"dry-run {upcoming_n} upcoming"

    # Never stamp a fresh updated_at on events.json when ALL remote sources failed
    if fetch_res.status == "failed":
        _atomic_write_json(state_path, new_state)
        msg = f"all {len(fetch_res.sources_failed)} calendar source(s) failed: {err_brief}"
        print(f"ERROR: {msg} — leaving {out} untouched")
        return 1, msg

    payload = {
        "version": 1,
        "updated_at": stamp,
        "events": fetch_res.events,
    }
    _atomic_write_json(out, payload)
    _atomic_write_json(state_path, new_state)

    status_note = ""
    if fetch_res.status == "degraded":
        failed = ",".join(fetch_res.sources_failed)
        # err_brief is the per-source detail (HTTP status included). The
        # all-failed branch above already prints it; degraded used to name
        # only the source.
        status_note = (
            f" [degraded: failed={failed}; {err_brief}]"
            if err_brief
            else f" [degraded: failed={failed}]"
        )
    result = (
        f"wrote {len(fetch_res.events)} event(s) "
        f"({upcoming_n} upcoming, {upcoming_high_n} high){status_note}"
    )
    print(f"{result} -> {out} (updated_at {stamp})")
    return 0, result


if __name__ == "__main__":
    sys.exit(main())
