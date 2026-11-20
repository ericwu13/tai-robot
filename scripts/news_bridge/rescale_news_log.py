"""Re-score W3's logged history with the volume normalisation rules.

``news_log.jsonl`` stores, per article, the ``run_net`` and ``session_net``
the scorer computed at the time.  Runs logged before volume normalisation
(``rss_scorer.REF_STORIES_PER_RUN``) and story dedup (``story_key``) are
plain sums, so the 新聞趨勢 chart and the drivers list mix two scales.
This replays every ``log_files`` file as one stream, ordered by each file's
earliest ``scored_at`` (ties keep ``log_files`` order), carrying the session
EMA, dedup window and session summary across files, with the scorer's own functions:

* a headline whose ``story_key`` was scored in the previous
  ``STORY_TTL_HOURS`` (or earlier in the same run) gets ``run_scale: 0``
  and ``story_dup: true`` — still listed, no longer counted;
* ``run_net`` = clipped sum of the remaining stories x ``run_scale``;
* ``session_net`` = the per-session EMA chain (reset when the session key
  changes, decay ``EMA_DECAY`` per logged run), exactly like the scorer.

Approximation: a run whose articles were all skipped by Gemini decayed
the live EMA but logged nothing, so the replay cannot see it.

Usage (on the bridge host, between two W3 passes, e.g. at :10 or :40)::

    python rescale_news_log.py --log <state dir>/news/news_log.jsonl            # dry run (base + monthly files)
    python rescale_news_log.py --log ... --apply                                # rewrite (backup kept)
    python rescale_news_log.py --log ... --state rss_scorer_state.json --apply  # + live session net

``--state`` also rewrites the live state's ``session_net`` / ``session_peak``
/ ``recent_runs`` / ``recent_stories`` when its ``session_key`` is the last
logged session, so the next pass continues from the rescaled value.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
import rss_news_log  # noqa: E402
import rss_scorer  # noqa: E402


def _when(text: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(text))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=rss_scorer._TZ_TAIPEI)


def _num(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


class _Carry:
    """Session EMA, story-dedup window and session summary for one replay."""

    def __init__(self) -> None:
        self.recent: dict[str, datetime] = {}
        self.sessions: dict[str, dict] = {}
        self.cum = 0.0
        self.session: str | None = None
        self.peak = 0.0
        self.run_nets: list[float] = []
        self.last_session = ""


def _prepare(lines: list[str]):
    """``(entries, runs, earliest scored_at)``. Unparsable lines stay put."""
    entries = []                      # (raw_line, record or None)
    runs: list[list[dict]] = []
    earliest: datetime | None = None
    for line in lines:
        try:
            rec = json.loads(line)
        except ValueError:
            rec = None
        parsed = rec if isinstance(rec, dict) and rec.get("scored_at") else None
        entries.append((line, parsed))
        if parsed is None:
            continue
        # Runs = consecutive records with the same scored_at (one scorer pass).
        if runs and runs[-1][0]["scored_at"] == parsed["scored_at"]:
            runs[-1].append(parsed)
        else:
            runs.append([parsed])
            at = _when(parsed["scored_at"])
            if at is not None and (earliest is None or at < earliest):
                earliest = at
    return entries, runs, earliest


def _replay(runs: list[list[dict]], carry: _Carry) -> tuple[int, dict[str, int]]:
    """Replay ``runs`` into ``carry``. Returns (duplicates, per-session run counts)."""
    clip = rss_scorer.ARTICLE_CLIP
    dups = 0
    touched: dict[str, int] = {}
    for run in runs:
        at = _when(run[0]["scored_at"])
        if at is not None:
            cutoff = at - timedelta(hours=rss_scorer.STORY_TTL_HOURS)
            carry.recent = {k: t for k, t in carry.recent.items() if t >= cutoff}
        batch, counted = set(), []
        for rec in run:
            key = rss_scorer.story_key(rec.get("title", ""))
            if key and (key in carry.recent or key in batch):
                rec["story_dup"] = True
                rec["run_scale"] = 0.0
                dups += 1
                continue
            rec.pop("story_dup", None)
            if key:
                batch.add(key)
            counted.append(rec)
        if at is not None:
            for key in batch:
                carry.recent[key] = at
        scale = rss_scorer.run_scale(len(counted))
        net = sum(max(-clip, min(clip, _num(r.get("score")))) for r in counted) * scale
        key = str(run[0].get("session", ""))
        if key != carry.session:
            carry.session, carry.cum, carry.peak = key, 0.0, 0.0
            carry.run_nets = []
        carry.cum = round(carry.cum * rss_scorer.EMA_DECAY + net, 4)
        if (carry.cum > 0 and carry.cum > carry.peak) or (carry.cum < 0 and carry.cum < carry.peak):
            carry.peak = carry.cum
        carry.run_nets.append(round(net, 4))
        info = carry.sessions.setdefault(key, {"old_final": None, "runs": 0})
        info["old_final"] = _num(run[-1].get("session_net"))
        info["runs"] += 1
        info["new_final"] = carry.cum
        info["new_peak"] = carry.peak
        info["recent_runs"] = carry.run_nets[-3:]
        touched[key] = touched.get(key, 0) + 1
        for rec in run:
            if not rec.get("story_dup"):
                rec["run_scale"] = round(scale, 4)
            rec["run_net"] = round(net, 4)
            rec["session_net"] = carry.cum
        carry.last_session = run[0].get("session", "")
    return dups, touched


def _render(entries) -> list[str]:
    out = []
    for line, rec in entries:
        if rec is None:
            out.append(line if line.endswith("\n") else line + "\n")
        else:
            out.append(json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n")
    return out


def _file_order(earliest: datetime | None, index: int):
    """Earliest run first; a file with no parseable time goes last.

    ``log_files`` is the pre-split base file, then monthly names. That is
    scored_at order only when the base file is older than every monthly
    run, so the replay orders the files itself. Ties keep the input order.
    """
    if earliest is None:
        return (1, index)
    return (0, earliest, index)


def _file_summary(carry: _Carry, runs, dups: int, touched: dict[str, int]) -> dict:
    """This file's counts. Session nets are the chain after this file."""
    sessions = {}
    for key, n_runs in touched.items():
        info = carry.sessions[key]
        sessions[key] = {"old_final": info["old_final"], "runs": n_runs,
                         "new_final": info["new_final"], "new_peak": info["new_peak"],
                         "recent_runs": list(info["recent_runs"])}
    return {"runs": len(runs), "records": sum(len(r) for r in runs), "duplicates": dups,
            "sessions": sessions, "last_session": carry.last_session,
            "recent_stories": {k: t.isoformat(timespec="seconds") for k, t in carry.recent.items()}}


def rescale_files(files: list[tuple[str, list[str]]]) -> tuple[list[tuple[str, list[str], dict]], dict]:
    """Replay ``files`` as one chronological stream.

    Returns ``(per_file, summary)``. ``per_file`` is ``(path, lines, summary)``
    in replay order; each file is rewritten with only its own lines.
    ``summary`` is the combined chain (the last session overall).
    """
    prepared = []
    for index, (path, lines) in enumerate(files):
        entries, runs, earliest = _prepare(lines)
        prepared.append((earliest, index, path, entries, runs))
    prepared.sort(key=lambda item: _file_order(item[0], item[1]))

    carry = _Carry()
    per_file = []
    total_runs = total_records = total_dups = 0
    for _earliest, _index, path, entries, runs in prepared:
        dups, touched = _replay(runs, carry)
        per_file.append((path, _render(entries), _file_summary(carry, runs, dups, touched)))
        total_runs += len(runs)
        total_records += sum(len(r) for r in runs)
        total_dups += dups
    summary = {"runs": total_runs, "records": total_records, "duplicates": total_dups,
               "sessions": carry.sessions, "last_session": carry.last_session,
               "recent_stories": {k: t.isoformat(timespec="seconds")
                                  for k, t in carry.recent.items()}}
    return per_file, summary


def rescale(lines: list[str]) -> tuple[list[str], dict]:
    """Rewritten lines plus a summary; unparsable lines pass through."""
    per_file, summary = rescale_files([("", lines)])
    return per_file[0][1], summary


def _replace(tmp: str, path: str) -> None:
    # The dashboard may hold a share-delete read handle for a moment.
    for attempt in range(10):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 9:
                raise
            time.sleep(0.5)


def _write(path: str, text: str) -> str:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = f"{path}.bak-{stamp}"
    with open(path, encoding="utf-8") as src, open(backup, "w", encoding="utf-8",
                                                    newline="\n") as dst:
        dst.write(src.read())
    tmp = f"{path}.rescale.tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    _replace(tmp, path)
    return backup


def update_state(state: dict, summary: dict) -> bool:
    """Carry the rescaled tonight's numbers into the live scorer state."""
    key = summary["last_session"]
    info = summary["sessions"].get(key)
    if not info or state.get("session_key") != key:
        return False
    state["session_net"] = info["new_final"]
    state["session_peak"] = info["new_peak"]
    state["recent_runs"] = info["recent_runs"]
    state["recent_stories"] = summary["recent_stories"]
    return True


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--log", required=True, help="news_log.jsonl base path (every log_files file)")
    ap.add_argument("--state", help="rss_scorer_state.json to bring in line too")
    ap.add_argument("--apply", action="store_true", help="write (default: dry run)")
    args = ap.parse_args(argv)

    files = rss_news_log.log_files(args.log)
    if not files:
        with open(args.log, encoding="utf-8") as f:
            f.read(0)

    loaded = []
    for path in files:
        with open(path, encoding="utf-8") as f:
            loaded.append((path, f.readlines()))
    planned, summary = rescale_files(loaded)
    for path, _out, file_summary in planned:
        print(f"{path}: {file_summary['records']} records in {file_summary['runs']} runs; "
              f"{file_summary['duplicates']} duplicate stories now weighted 0")
        for key, info in list(file_summary["sessions"].items())[-10:]:
            print(f"  {key or '(no session)'}: session net {info['old_final']:+.2f} -> "
                  f"{info['new_final']:+.2f} over {info['runs']} run(s)")

    state = None
    if args.state:
        with open(args.state, encoding="utf-8") as f:
            state = json.load(f)
        if summary["runs"] and update_state(state, summary):
            print(f"  state: session_net -> {state['session_net']:+.2f} "
                  f"({summary['last_session']})")
        else:
            print("  state: session_key is not the last logged session; left alone")
            state = None

    if not args.apply:
        print("DRY RUN - nothing written. Re-run with --apply.")
        return 0
    for path, out, _summary in planned:
        backup = _write(path, "".join(out))
        print(f"log rewritten; backup {backup}")
    if state is not None:
        backup = _write(args.state, json.dumps(state, ensure_ascii=False, indent=2))
        print(f"state rewritten; backup {backup}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
