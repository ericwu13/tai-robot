"""One bounded run log per news-bridge workflow (stdlib only).

Each W1-W5 pass appends one JSON line to ``<bridge dir>/runs/<W>.jsonl``::

    {"v": 1, "workflow": "W4", "started_at": ..., "finished_at": ...,
     "duration_s": 3.1, "ok": true, "exit_code": 0, "result": "...",
     "error": "", "host": "HOST-A"}

so the dashboard's 工作流程 view can answer "did it run, when, did it
work, on which machine" without parsing four log formats.  ``host`` is
what lets a dashboard notice it is showing another machine's (or a
frozen) bridge directory.

Bookkeeping only: nothing here may raise into a bridge pass, and nothing
reads these files back into a vote or signal.  The bridges load this
module by file path (``src/news/__init__`` pulls a wider dependency
chain), so it must stay stdlib-only.
"""
from __future__ import annotations

import json
import os
import socket
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

VERSION = 1
WORKFLOWS = ("W1", "W2", "W3", "W4", "W5")
DIR_NAME = "runs"
MAX_TEXT = 300
# W2 runs every 2 minutes (~700 passes/day): trim to the newest
# KEEP_LINES once the file passes MAX_BYTES, i.e. roughly a day of W2.
MAX_BYTES = 256_000
KEEP_LINES = 600
REPLACE_RETRIES = 3
REPLACE_RETRY_SEC = 0.2

_TZ_TPE = timezone(timedelta(hours=8))


def runs_path(bridge_dir: str | os.PathLike, workflow: str) -> Path:
    return Path(bridge_dir) / DIR_NAME / f"{workflow}.jsonl"


def _iso(dt: datetime) -> str:
    return dt.astimezone(_TZ_TPE).isoformat(timespec="seconds")


def _clip(text) -> str:
    text = str(text or "")
    return text if len(text) <= MAX_TEXT else text[:MAX_TEXT - 1] + "…"


def _replace(tmp: str, path: Path) -> None:
    for attempt in range(REPLACE_RETRIES):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == REPLACE_RETRIES - 1:
                raise
            time.sleep(REPLACE_RETRY_SEC)


def _append(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    if path.stat().st_size > MAX_BYTES:
        with open(path, encoding="utf-8") as f:
            kept = f.readlines()[-KEEP_LINES:]
        # Never let one trim keep more than the cap allows.
        while kept and sum(len(x.encode("utf-8")) for x in kept) > MAX_BYTES:
            kept = kept[len(kept) // 2:] if len(kept) > 1 else []
        tmp = str(path) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.writelines(kept)
        _replace(tmp, path)


def record_run(bridge_dir: str | os.PathLike, workflow: str, *, ok: bool,
               result: str = "", error: str = "",
               started_at: datetime | None = None,
               finished_at: datetime | None = None,
               exit_code: int | None = None, host: str | None = None) -> bool:
    """Append one run row.  Returns False (never raises) on any failure."""
    try:
        if workflow not in WORKFLOWS:
            return False
        finished = finished_at or datetime.now(_TZ_TPE)
        started = started_at or finished
        row = {
            "v": VERSION,
            "workflow": workflow,
            "started_at": _iso(started),
            "finished_at": _iso(finished),
            "duration_s": round(max(0.0, (finished - started).total_seconds()), 1),
            "ok": bool(ok),
            "exit_code": exit_code,
            "result": _clip(result),
            "error": _clip(error),
            "host": host or socket.gethostname(),
        }
        _append(runs_path(bridge_dir, workflow), json.dumps(row, ensure_ascii=False))
        return True
    except Exception as e:  # noqa: BLE001 — bookkeeping must not fail a pass
        print(f"  run log write failed: {type(e).__name__}: {e}")
        return False


class run_recorder:  # noqa: N801 — used as ``with run_recorder(...)``
    """Context manager around one bridge pass.

    Set ``run.result`` / ``run.exit_code`` inside the block, or call
    ``run.fail(msg)`` for a pass that finished but did not do its job
    (W3 with every Gemini call failing).  An exception is recorded as a
    failed run and re-raised unchanged.
    """

    def __init__(self, bridge_dir: str | os.PathLike, workflow: str):
        self.bridge_dir = bridge_dir
        self.workflow = workflow
        self.result = ""
        self.exit_code: int | None = None
        self.error = ""
        self._failed = False
        self._started = datetime.now(_TZ_TPE)

    def fail(self, error: str) -> None:
        self._failed = True
        self.error = str(error)

    def __enter__(self) -> "run_recorder":
        self._started = datetime.now(_TZ_TPE)
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        try:
            ok = not self._failed and exc_type is None and self.exit_code in (None, 0)
            error = self.error
            if exc_type is not None:
                error = f"{exc_type.__name__}: {exc}"
            record_run(self.bridge_dir, self.workflow, ok=ok, result=self.result,
                       error=error, started_at=self._started,
                       exit_code=self.exit_code)
        except Exception:  # noqa: BLE001
            pass
        return False


def parse_runs(text: str | None) -> list[dict]:
    """Run rows from a ``runs/<W>.jsonl`` text, oldest first; junk skipped."""
    out = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and isinstance(row.get("ok"), bool):
            out.append(row)
    return out
