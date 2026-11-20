"""Re-score W3's logged history with the volume normalisation rules.

``news_log.jsonl`` stores, per article, the ``run_net`` and ``session_net``
the scorer computed at the time.  Runs logged before volume normalisation
(``rss_scorer.REF_STORIES_PER_RUN``) and story dedup (``story_key``) are
plain sums, so the 新聞趨勢 chart and the drivers list mix two scales.
This replays the log in file order with the scorer's own functions:

* a headline whose ``story_key`` was scored in the previous
  ``STORY_TTL_HOURS`` (or earlier in the same run) gets ``run_scale: 0``
  and ``story_dup: true`` — still listed, no longer counted;
* ``run_net`` = clipped sum of the remaining stories x ``run_scale``;
* ``session_net`` = the per-session EMA chain (reset when the session key
  changes, decay ``EMA_DECAY`` per logged run), exactly like the scorer.

Approximation: a run whose articles were all skipped by Gemini decayed
the live EMA but logged nothing, so the replay cannot see it.

Usage (on the bridge host, between two W3 passes, e.g. at :10 or :40)::

    python rescale_news_log.py --log <state dir>/news/news_log.jsonl            # dry run
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


def rescale(lines: list[str]) -> tuple[list[str], dict]:
    """Rewritten lines plus a summary; unparsable lines pass through."""
    entries = []                      # (raw_line, record or None)
    for line in lines:
        try:
            rec = json.loads(line)
        except ValueError:
            rec = None
        entries.append((line, rec if isinstance(rec, dict) and rec.get("scored_at") else None))

    # Runs = consecutive records with the same scored_at (one scorer pass).
    runs: list[list[dict]] = []
    for _line, rec in entries:
        if rec is None:
            continue
        if runs and runs[-1][0]["scored_at"] == rec["scored_at"]:
            runs[-1].append(rec)
        else:
            runs.append([rec])

    clip = rss_scorer.ARTICLE_CLIP
    recent: dict[str, datetime] = {}
    sessions: dict[str, dict] = {}
    cum, session, peak = 0.0, None, 0.0
    run_nets: list[float] = []
    dups = 0
    for run in runs:
        at = _when(run[0]["scored_at"])
        if at is not None:
            cutoff = at - timedelta(hours=rss_scorer.STORY_TTL_HOURS)
            recent = {k: t for k, t in recent.items() if t >= cutoff}
        batch, counted = set(), []
        for rec in run:
            key = rss_scorer.story_key(rec.get("title", ""))
            if key and (key in recent or key in batch):
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
                recent[key] = at
        scale = rss_scorer.run_scale(len(counted))
        net = sum(max(-clip, min(clip, _num(r.get("score")))) for r in counted) * scale
        key = str(run[0].get("session", ""))
        if key != session:
            session, cum, peak = key, 0.0, 0.0
            run_nets = []
        cum = round(cum * rss_scorer.EMA_DECAY + net, 4)
        if (cum > 0 and cum > peak) or (cum < 0 and cum < peak):
            peak = cum
        run_nets.append(round(net, 4))
        info = sessions.setdefault(key, {"old_final": None, "runs": 0})
        info["old_final"] = _num(run[-1].get("session_net"))
        info["runs"] += 1
        info["new_final"] = cum
        info["new_peak"] = peak
        info["recent_runs"] = run_nets[-3:]
        for rec in run:
            if not rec.get("story_dup"):
                rec["run_scale"] = round(scale, 4)
            rec["run_net"] = round(net, 4)
            rec["session_net"] = cum

    out = []
    for line, rec in entries:
        if rec is None:
            out.append(line if line.endswith("\n") else line + "\n")
        else:
            out.append(json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n")
    last_key = runs[-1][0].get("session", "") if runs else ""
    recent_out = {k: t.isoformat(timespec="seconds") for k, t in recent.items()}
    return out, {"runs": len(runs), "records": sum(len(r) for r in runs),
                 "duplicates": dups, "sessions": sessions,
                 "last_session": last_key, "recent_stories": recent_out}


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
    ap.add_argument("--log", required=True, help="news_log.jsonl path")
    ap.add_argument("--state", help="rss_scorer_state.json to bring in line too")
    ap.add_argument("--apply", action="store_true", help="write (default: dry run)")
    args = ap.parse_args(argv)

    with open(args.log, encoding="utf-8") as f:
        lines = f.readlines()
    out, summary = rescale(lines)
    print(f"{summary['records']} records in {summary['runs']} runs; "
          f"{summary['duplicates']} duplicate stories now weighted 0")
    for key, info in list(summary["sessions"].items())[-10:]:
        print(f"  {key or '(no session)'}: session net {info['old_final']:+.2f} -> "
              f"{info['new_final']:+.2f} over {info['runs']} run(s)")

    state = None
    if args.state:
        with open(args.state, encoding="utf-8") as f:
            state = json.load(f)
        if update_state(state, summary):
            print(f"  state: session_net -> {state['session_net']:+.2f} "
                  f"({summary['last_session']})")
        else:
            print("  state: session_key is not the last logged session; left alone")
            state = None

    if not args.apply:
        print("DRY RUN - nothing written. Re-run with --apply.")
        return 0
    backup = _write(args.log, "".join(out))
    print(f"log rewritten; backup {backup}")
    if state is not None:
        backup = _write(args.state, json.dumps(state, ensure_ascii=False, indent=2))
        print(f"state rewritten; backup {backup}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
