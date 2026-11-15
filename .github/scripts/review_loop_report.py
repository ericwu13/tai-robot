#!/usr/bin/env python3
"""Weekly health report of the Claude review loop (review-loop-report.yml).

Reads the last N days of claude-review.yml runs from the Actions API and
says where the loop spends Opus time and where it churns:

  - runs by outcome: Opus review, verdict carried over (PR diff unchanged),
    not reviewed (ineligible), cancelled (superseded by a newer push),
    no verdict (Claude returned nothing, even after the retry);
  - per PR: Opus reviews, carry-overs, verdict sequence and PASS<->BLOCK flips;
  - open PRs/issues labeled `needs-owner`, and open PRs that still have
    unresolved Claude threads (these block the merge).

Not measured: P3-only commits and Reed/Claude duplicate findings (they need
reading the comments; check by hand when a PR shows many Opus reviews).

Output: markdown to $GITHUB_STEP_SUMMARY (or stdout) and a short Discord
message via DISCORD_REVIEW_WEBHOOK (optional). Env: GITHUB_REPOSITORY,
GH_TOKEN, DAYS (default 7), RUN_URL (link to the full report).
"""
import json
import os
import subprocess
import sys
import urllib.parse
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from claude_review_post import discord_notify, open_bot_threads  # noqa: E402

WORKFLOW = "claude-review.yml"
CLAUDE_STEPS = ("Run Claude review", "Retry Claude review once")
BUSY_OPUS_REVIEWS = 5   # a PR reviewed by Opus this often is churning
BUSY_FLIPS = 2          # PASS<->BLOCK flips that suggest unclear guidance
MAX_ROWS = 15


def _ts(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def classify_run(run, jobs):
    """One claude-review run -> record, or None for runs that are not about
    the review (another label added: job `review-ignored-label`). Pure.

    kind: opus | carried | not_reviewed | cancelled | no_verdict
    verdict: PASS | BLOCK | None
    """
    review = next((j for j in jobs if j.get("name") == "review"), None)
    if review is None:
        return None
    steps = {s.get("name"): s for s in review.get("steps") or []}

    def concl(name):
        return (steps.get(name) or {}).get("conclusion")

    opus_seconds, retried = 0.0, False
    for name in CLAUDE_STEPS:
        s = steps.get(name) or {}
        if s.get("conclusion") in ("success", "failure", "cancelled"):
            start, end = _ts(s.get("started_at")), _ts(s.get("completed_at"))
            if start and end:
                opus_seconds += max(0.0, (end - start).total_seconds())
            retried = retried or name == CLAUDE_STEPS[1]

    gate = concl("Verdict gate")
    verdict = {"success": "PASS", "failure": "BLOCK"}.get(gate)
    claude = concl(CLAUDE_STEPS[0])
    if (run.get("conclusion") or review.get("conclusion")) == "cancelled":
        kind, verdict = "cancelled", None
    elif concl("Checkout PR head") in (None, "skipped"):
        kind, verdict = "not_reviewed", None
    elif claude == "skipped" and concl("Reuse the verdict if the PR diff is unchanged") == "success":
        kind = "carried"
    elif concl("Post inline threads and summary") == "success":
        kind = "opus"
    elif "Post inline threads and summary" not in steps and claude == "success" and gate == "success":
        kind = "opus"  # pre-#273 layout: the gate alone held the verdict
    else:
        kind, verdict = "no_verdict", None

    prs = run.get("pull_requests") or []
    return {"pr": prs[0]["number"] if prs else None,
            "branch": run.get("head_branch") or "",
            "created": run.get("created_at") or "",
            "kind": kind, "verdict": verdict,
            "opus_seconds": opus_seconds, "retried": retried}


def link_branches(records, branch_to_pr):
    """Runs of merged/closed PRs lose their PR link; fill it from the head
    branch (the newest PR for that branch). Pure, returns new records."""
    return [dict(r, pr=branch_to_pr.get(r["branch"])) if r["pr"] is None else r
            for r in records]


def flips(verdicts):
    """PASS<->BLOCK changes in an ordered verdict list. Pure."""
    v = [x for x in verdicts if x in ("PASS", "BLOCK")]
    return sum(1 for a, b in zip(v, v[1:]) if a != b)


def summarize(records):
    """Totals and per-PR stats from classify_run records. Pure."""
    kinds = ("opus", "carried", "not_reviewed", "cancelled", "no_verdict")
    totals = {k: 0 for k in kinds}
    totals.update(runs=0, retries=0, opus_seconds=0.0, PASS=0, BLOCK=0)
    per_pr = {}
    for r in sorted(records, key=lambda r: r["created"]):
        totals["runs"] += 1
        totals[r["kind"]] += 1
        totals["retries"] += int(r["retried"])
        totals["opus_seconds"] += r["opus_seconds"]
        if r["verdict"]:
            totals[r["verdict"]] += 1
        key = r["pr"] if r["pr"] is not None else r["branch"]
        p = per_pr.setdefault(key, {"opus": 0, "carried": 0, "verdicts": [],
                                    "opus_seconds": 0.0})
        p["opus"] += int(r["kind"] == "opus")
        p["carried"] += int(r["kind"] == "carried")
        p["opus_seconds"] += r["opus_seconds"]
        if r["verdict"]:
            p["verdicts"].append(r["verdict"])
    for p in per_pr.values():
        p["flips"] = flips(p["verdicts"])
        p["last"] = p["verdicts"][-1] if p["verdicts"] else "-"
    return totals, per_pr


def busiest(per_pr, n=5):
    """PRs by Opus reviews, then flips. Pure."""
    return sorted(per_pr.items(), key=lambda kv: (-kv[1]["opus"], -kv[1]["flips"]))[:n]


def _ref(key):
    return f"#{key}" if isinstance(key, int) else f"`{key}`"


def render_markdown(totals, per_pr, days, needs_owner, open_threads):
    t = totals
    lines = [f"## Claude review loop, last {days} days", "",
             "| Runs | Opus reviews | Opus minutes | Retries | Carried over | Not reviewed | Cancelled | No verdict | PASS | BLOCK |",
             "|---|---|---|---|---|---|---|---|---|---|",
             f"| {t['runs']} | {t['opus']} | {t['opus_seconds'] / 60:.0f} | {t['retries']} | "
             f"{t['carried']} | {t['not_reviewed']} | {t['cancelled']} | {t['no_verdict']} | "
             f"{t['PASS']} | {t['BLOCK']} |", "",
             "### Per PR", "",
             "| PR | Opus reviews | Opus min | Carried | Flips | Verdicts | Now |",
             "|---|---|---|---|---|---|---|"]
    shown = [(k, p) for k, p in busiest(per_pr, n=len(per_pr)) if p["opus"] or p["verdicts"]]
    for key, p in shown[:MAX_ROWS]:
        seq = " ".join(v[0] for v in p["verdicts"]) or "-"
        flag = " :warning:" if p["opus"] >= BUSY_OPUS_REVIEWS or p["flips"] >= BUSY_FLIPS else ""
        lines.append(f"| {_ref(key)}{flag} | {p['opus']} | {p['opus_seconds'] / 60:.0f} | "
                     f"{p['carried']} | {p['flips']} | {seq} | {p['last']} |")
    rest = len(per_pr) - min(len(shown), MAX_ROWS)
    if rest:
        lines.append(f"| {rest} more | | | | | | (no verdict or below the top {MAX_ROWS}) |")
    lines += ["", f":warning: = {BUSY_OPUS_REVIEWS}+ Opus reviews or {BUSY_FLIPS}+ verdict flips: "
              "check whether the findings or the guidance keep changing. "
              "No verdict = Claude ran but returned nothing (auth failure, or a PR that "
              "edits claude-review.yml, which the action refuses to review).", "",
              "### Waiting on the owner (`needs-owner`)", ""]
    lines += [f"- {x}" for x in needs_owner] or ["- none"]
    lines += ["", "### Open PRs with unresolved Claude threads", ""]
    lines += [f"- #{n}: {c}" for n, c in open_threads] or ["- none"]
    lines += ["", "_Not measured: P3-only commits, Reed/Claude duplicate findings._"]
    return "\n".join(lines) + "\n"


def render_discord(totals, per_pr, days, needs_owner, open_threads, run_url=""):
    t = totals
    msg = [f"\U0001F4CA Claude review loop, last {days} days",
           f"Runs {t['runs']}: Opus {t['opus']} ({t['opus_seconds'] / 60:.0f} min, "
           f"{t['retries']} retries), carried over {t['carried']}, not reviewed "
           f"{t['not_reviewed']}, cancelled {t['cancelled']}, no verdict {t['no_verdict']}",
           f"Verdicts: PASS {t['PASS']} / BLOCK {t['BLOCK']}, "
           f"flips {sum(p['flips'] for p in per_pr.values())}"]
    busy = [f"{_ref(k)} {p['opus']} Opus, {p['flips']} flips (now {p['last']})"
            for k, p in busiest(per_pr, n=3) if p["opus"]]
    if busy:
        msg.append("Busiest: " + " · ".join(busy))
    msg.append("Needs owner: " + (", ".join(needs_owner) or "none"))
    if open_threads:
        msg.append("Unresolved Claude threads: "
                   + ", ".join(f"#{n} ({c})" for n, c in open_threads))
    if run_url:
        msg.append(f"Full report: {run_url}")
    return "\n".join(msg)


# ---- GitHub reads (not unit-tested) --------------------------------------

def gh_json(*args):
    out = subprocess.run(["gh", *args], capture_output=True, text=True,
                         encoding="utf-8", check=True).stdout
    return json.loads(out or "null")


def fetch_records(repo, since):
    q = urllib.parse.urlencode({"per_page": 100, "created": f">={since:%Y-%m-%d}"})
    pages = gh_json("api", "--paginate", "--slurp",
                    f"repos/{repo}/actions/workflows/{WORKFLOW}/runs?{q}")
    records = []
    for page in pages or []:
        for run in page.get("workflow_runs") or []:
            if run.get("status") != "completed":
                continue
            jobs = gh_json("api", f"repos/{repo}/actions/runs/{run['id']}/jobs?per_page=50")
            rec = classify_run(run, jobs.get("jobs") or [])
            if rec:
                records.append(rec)
    prs = gh_json("pr", "list", "--repo", repo, "--state", "all", "--limit", "300",
                  "--json", "number,headRefName") or []
    branch_to_pr = {}
    for pr in sorted(prs, key=lambda p: p["number"]):  # newest PR wins
        branch_to_pr[pr["headRefName"]] = pr["number"]
    return link_branches(records, branch_to_pr)


def fetch_needs_owner(repo):
    items = []
    for kind in ("pr", "issue"):
        for it in gh_json(kind, "list", "--repo", repo, "--state", "open",
                          "--label", "needs-owner", "--json", "number,title") or []:
            items.append(f"{'PR' if kind == 'pr' else 'issue'} #{it['number']} {it['title']}")
    return items


def fetch_open_threads(repo):
    out = []
    for pr in gh_json("pr", "list", "--repo", repo, "--state", "open",
                      "--json", "number", "--limit", "100") or []:
        try:
            n = len(open_bot_threads(repo, pr["number"]))
        except RuntimeError as exc:
            print(f"threads of #{pr['number']}: {exc}")
            continue
        if n:
            out.append((pr["number"], n))
    return out


def main():
    repo = os.environ["GITHUB_REPOSITORY"]
    days = int(os.environ.get("DAYS") or 7)
    since = datetime.now(timezone.utc) - timedelta(days=days)
    totals, per_pr = summarize(fetch_records(repo, since))
    needs_owner, open_threads = fetch_needs_owner(repo), fetch_open_threads(repo)

    md = render_markdown(totals, per_pr, days, needs_owner, open_threads)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(md)
    print(md)
    discord_notify(render_discord(totals, per_pr, days, needs_owner, open_threads,
                                  os.environ.get("RUN_URL", "")), mention="")


if __name__ == "__main__":
    main()
