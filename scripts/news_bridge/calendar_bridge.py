"""W1 calendar bridge — writes events.json from a hand-maintained source list.

Standalone stand-in for the n8n W1 workflow (docs/news_framework_n8n_setup.md §2).
Edit calendar_source.json (same entry schema as events.json), then run this to
publish: it stamps a fresh timezone-aware updated_at on EVERY run (the bot
treats a calendar older than 14 days as stale and stops gating), so schedule
it daily even when the event list hasn't changed.

Usage:
    python calendar_bridge.py --out C:/n8n-bridge/events.json
    python calendar_bridge.py --out ... --source my_events.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_TZ_TAIPEI = timezone(timedelta(hours=8))
DEFAULT_SOURCE = Path(__file__).with_name("calendar_source.json")


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


def main() -> int:
    ap = argparse.ArgumentParser(description="Publish events.json for the bot")
    ap.add_argument("--out", required=True, help="events.json path the bot reads")
    ap.add_argument("--source", default=str(DEFAULT_SOURCE),
                    help="hand-maintained event list (default: calendar_source.json)")
    args = ap.parse_args()
    with _run_recorder(Path(args.out).parent, "W1") as run:
        run.exit_code, run.result = publish(args)
        return run.exit_code


def publish(args) -> tuple[int, str]:
    """Write events.json; ``(exit code, one-line result)``."""
    with open(args.source, encoding="utf-8") as f:
        events = json.load(f)
    if not isinstance(events, list):
        msg = f"source must be a JSON list of event entries, got {type(events)}"
        print(msg)
        return 1, msg

    payload = {
        "version": 1,
        "updated_at": datetime.now(_TZ_TAIPEI).isoformat(timespec="seconds"),
        "events": events,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = str(out) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    os.replace(tmp, out)
    print(f"wrote {len(events)} event(s) -> {out} (updated_at {payload['updated_at']})")
    return 0, f"wrote {len(events)} event(s)"


if __name__ == "__main__":
    sys.exit(main())
