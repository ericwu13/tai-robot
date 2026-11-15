"""Review-loop report + Discord owner ping (.github/scripts).

Pure functions on fixed data: no network, no gh, no clock, no paths outside
the repo, so the result is the same on every machine.
"""
import importlib.util
import sys
import urllib.request
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / ".github" / "scripts"


def _load(name):
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


post = _load("claude_review_post")
report = _load("review_loop_report")


def _step(name, conclusion, start="2026-11-10T00:00:00Z", end="2026-11-10T00:00:00Z"):
    return {"name": name, "conclusion": conclusion, "started_at": start, "completed_at": end}


def _review_job(**over):
    """Steps of the claude-review `review` job; defaults = a full Opus PASS."""
    steps = {
        "Decide whether to review": "success",
        "Checkout PR head": "success",
        "Reuse the verdict if the PR diff is unchanged": "success",
        "Collect open Claude threads": "success",
        "Run Claude review": "success",
        "Retry Claude review once": "skipped",
        "Post inline threads and summary": "success",
        "Verdict gate": "success",
    }
    steps.update(over)
    out = []
    for name, c in steps.items():
        if name == "Run Claude review" and c != "skipped":
            out.append(_step(name, c, "2026-11-10T00:00:00Z", "2026-11-10T00:10:00Z"))
        elif name == "Retry Claude review once" and c != "skipped":
            out.append(_step(name, c, "2026-11-10T00:11:00Z", "2026-11-10T00:16:00Z"))
        else:
            out.append(_step(name, c))
    return {"name": "review", "conclusion": "success", "steps": out}


def _run(pr=293, conclusion="success", created="2026-11-10T00:00:00Z"):
    return {"pull_requests": [{"number": pr}] if pr else [], "head_branch": "feat/x",
            "created_at": created, "conclusion": conclusion}


# ---- classify_run ---------------------------------------------------------

def test_full_review_pass_counts_opus_minutes():
    r = report.classify_run(_run(), [_review_job()])
    assert (r["kind"], r["verdict"], r["pr"]) == ("opus", "PASS", 293)
    assert r["opus_seconds"] == 600 and r["retried"] is False


def test_full_review_block():
    r = report.classify_run(_run(), [_review_job(**{"Verdict gate": "failure"})])
    assert (r["kind"], r["verdict"]) == ("opus", "BLOCK")


def test_carried_over_verdict_costs_no_opus():
    # Step list of a real carried-over BLOCK run (Claude skipped, reuse ran).
    job = _review_job(**{"Collect open Claude threads": "skipped",
                         "Run Claude review": "skipped",
                         "Post inline threads and summary": "skipped",
                         "Verdict gate": "failure"})
    r = report.classify_run(_run(), [job])
    assert (r["kind"], r["verdict"], r["opus_seconds"]) == ("carried", "BLOCK", 0)


def test_ineligible_run_is_not_reviewed():
    job = _review_job(**{k: "skipped" for k in (
        "Checkout PR head", "Reuse the verdict if the PR diff is unchanged",
        "Collect open Claude threads", "Run Claude review",
        "Post inline threads and summary")}, **{"Verdict gate": "failure"})
    r = report.classify_run(_run(), [job])
    assert (r["kind"], r["verdict"]) == ("not_reviewed", None)


def test_cancelled_run_has_no_verdict_but_keeps_opus_time():
    job = _review_job(**{"Run Claude review": "cancelled",
                         "Post inline threads and summary": "skipped",
                         "Verdict gate": "cancelled"})
    r = report.classify_run(_run(conclusion="cancelled"), [job])
    assert (r["kind"], r["verdict"], r["opus_seconds"]) == ("cancelled", None, 600)


def test_no_verdict_after_retry():
    job = _review_job(**{"Retry Claude review once": "success",
                         "Post inline threads and summary": "skipped",
                         "Verdict gate": "failure"})
    r = report.classify_run(_run(), [job])
    assert (r["kind"], r["verdict"], r["retried"], r["opus_seconds"]) == (
        "no_verdict", None, True, 900)


def test_old_layout_without_post_step_takes_pass_from_gate():
    job = _review_job()
    job["steps"] = [st for st in job["steps"] if st["name"] != "Post inline threads and summary"]
    r = report.classify_run(_run(), [job])
    assert (r["kind"], r["verdict"]) == ("opus", "PASS")


def test_other_label_run_is_ignored():
    job = {"name": "review-ignored-label", "conclusion": "skipped", "steps": []}
    assert report.classify_run(_run(), [job]) is None


def test_run_without_pr_link_keys_by_branch():
    r = report.classify_run(_run(pr=None), [_review_job()])
    assert r["pr"] is None and r["branch"] == "feat/x"


def test_link_branches_fills_only_missing_pr():
    recs = [{"pr": None, "branch": "feat/x"}, {"pr": 7, "branch": "feat/x"},
            {"pr": None, "branch": "gone"}]
    out = report.link_branches(recs, {"feat/x": 293})
    assert [r["pr"] for r in out] == [293, 7, None]


# ---- summarize -------------------------------------------------------------

def test_flips_ignore_missing_verdicts():
    assert report.flips(["BLOCK", None, "PASS", "PASS", "BLOCK"]) == 2
    assert report.flips([]) == 0


def test_summarize_orders_runs_by_time_per_pr():
    def rec(pr, created, kind, verdict):
        return {"pr": pr, "branch": "", "created": created, "kind": kind,
                "verdict": verdict, "opus_seconds": 60.0 if kind == "opus" else 0.0,
                "retried": False}
    records = [  # deliberately out of order
        rec(293, "2026-11-12", "opus", "PASS"),
        rec(293, "2026-11-10", "opus", "BLOCK"),
        rec(293, "2026-11-13", "carried", "PASS"),
        rec(296, "2026-11-11", "not_reviewed", None),
    ]
    totals, per_pr = report.summarize(records)
    assert totals["runs"] == 4 and totals["opus"] == 2 and totals["carried"] == 1
    assert (totals["PASS"], totals["BLOCK"], totals["opus_seconds"]) == (2, 1, 120.0)
    p = per_pr[293]
    assert p["verdicts"] == ["BLOCK", "PASS", "PASS"]
    assert (p["flips"], p["last"], p["opus"], p["carried"]) == (1, "PASS", 2, 1)
    assert per_pr[296]["last"] == "-"


def test_renderers_name_busy_prs_and_owner_queue():
    rec = {"pr": 293, "branch": "", "created": "", "kind": "opus",
           "verdict": "BLOCK", "opus_seconds": 0.0, "retried": False}
    totals, per_pr = report.summarize([dict(rec, created=str(i)) for i in range(6)])
    md = report.render_markdown(totals, per_pr, 7, ["PR #286 x"], [(293, 2)])
    assert "| #293 :warning: | 6 |" in md and "PR #286 x" in md and "#293: 2" in md
    msg = report.render_discord(totals, per_pr, 7, [], [], "https://example.invalid/run")
    assert "#293 6 Opus" in msg and "Needs owner: none" in msg
    assert msg.endswith("https://example.invalid/run")


# ---- Discord ---------------------------------------------------------------

def test_payload_without_mention_pings_nobody():
    p = post.discord_payload("PR title with @everyone and <@222>")
    assert p["allowed_mentions"] == {"parse": [], "users": [], "roles": []}


def test_payload_with_mention_and_length_cap():
    p = post.discord_payload("x" * 5000, mention="<@111>")
    assert p["content"].startswith("<@111> ") and len(p["content"]) == post.DISCORD_LIMIT


def test_only_the_configured_mention_can_ping():
    # A user or role id in the PR title must not be notified.
    text = post.owner_ping_text("PR", 1, "ping <@222> and <@&333>", "u", "why")
    p = post.discord_payload(text, mention="<@!111> <@&444>")
    assert p["allowed_mentions"] == {"parse": [], "users": ["111"], "roles": ["444"]}


def test_notify_without_webhook_sends_nothing(monkeypatch):
    monkeypatch.delenv("DISCORD_REVIEW_WEBHOOK", raising=False)
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda *a, **k: pytest.fail("must not call Discord"))
    assert post.discord_notify("hi") is False


def test_notify_failure_never_raises(monkeypatch):
    monkeypatch.setenv("DISCORD_REVIEW_WEBHOOK", "https://example.invalid/hook")

    def boom(*a, **k):
        raise OSError("network down")
    monkeypatch.setattr(urllib.request, "urlopen", boom)
    assert post.discord_notify("hi") is False


def test_owner_ping_text():
    t = post.owner_ping_text("PR", 286, "Loss limit", "https://example.invalid/pr/286",
                             "Claude BLOCKed it 3 times")
    assert "PR #286" in t and "Loss limit" in t and t.endswith("/pr/286")
