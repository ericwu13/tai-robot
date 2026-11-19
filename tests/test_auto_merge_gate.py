"""Approval-gate auto-merge. Pure mocks: no network, no live gh."""
import importlib.util
import json
import urllib.error
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
REPO = "ericwu13/tai-robot"
SHA = "a" * 40
OTHER_SHA = "b" * 40
REED = "91d043c7-dbb9-11f1-8120-06b44be262b7"
SAGE = "94d41356-e44b-11f1-9535-02900022bdcd"
OTHER_AUTO = "11111111-1111-1111-1111-111111111111"
RUN_URL = "https://github.com/ericwu13/tai-robot/actions/runs/1"
WEBHOOK_URL = "https://hooks.example/secret-path-XYZ"
WEBHOOK_KEY = "super-secret-key-ABC"


def _load():
    path = ROOT / "scripts" / "ci" / "auto_merge_gate.py"
    spec = importlib.util.spec_from_file_location("auto_merge_gate", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mod = _load()


class Proc:
    def __init__(self, rc=0, stdout="", stderr=""):
        self.returncode = rc
        self.stdout = stdout
        self.stderr = stderr


def make_pr(**kw):
    pr = {
        "state": "open",
        "draft": False,
        "head": {"sha": SHA, "repo": {"full_name": REPO}},
        "base": {"ref": "master"},
        "labels": [],
        "auto_merge": None,
    }
    for key, val in kw.items():
        if key in ("head", "base") and isinstance(val, dict):
            pr[key] = {**pr[key], **val}
        else:
            pr[key] = val
    return pr


def marker(automation_id, rest="\n\nverdict"):
    return f"<!-- CURSOR_AUTOMATION_ID: {automation_id} -->{rest}"


def review(state, automation, sha=SHA, user="cursor[bot]", submitted="2026-11-19T00:00:00Z",
           rid=1, body=None):
    if body is None:
        body = marker(automation) if automation else "no marker\n"
    return {
        "id": rid,
        "user": {"login": user},
        "commit_id": sha,
        "state": state,
        "submitted_at": submitted,
        "body": body,
    }


def thread(tid, login, body, resolved=False):
    author = None if login is None else {"login": login}
    nodes = [] if body is None and login is None else [{"author": author, "body": body}]
    if login == "" and body is None:
        nodes = []
    return {"id": tid, "isResolved": resolved, "comments": {"nodes": nodes}}


def pages_of(items, size=100):
    if not items:
        return [[]]
    return [items[i:i + size] for i in range(0, len(items), size)]


class World:
    """Routes gh subprocess calls. Merge results are a script of (rc, out, err)."""

    def __init__(self, pr=None, files=None, reviews=None, threads=None, merges=None):
        self.pr = make_pr() if pr is None else pr
        self.file_pages = files if files and isinstance(files[0], list) else pages_of(files or [])
        self.review_pages = (reviews if reviews and isinstance(reviews[0], list)
                             else pages_of(reviews or []))
        self.thread_pages = threads if threads is not None else [[]]
        self.merge_script = list(merges or [(0, "ok\n", "")])
        self.calls = []
        self.statuses = []
        self.resolves = []
        self.cursors = []

    def run(self, cmd, input=None, **_kw):
        assert cmd[0] == "gh"
        args = cmd[1:]
        self.calls.append((args, input))
        if args[:2] == ["pr", "merge"]:
            rc, out, err = (self.merge_script.pop(0) if self.merge_script else (1, "", "unexpected merge"))
            return Proc(rc, out, err)
        if args[:4] == ["api", "--method", "POST", "graphql"]:
            return self._graphql(json.loads(input))
        if args[:3] == ["api", "--method", "POST"] and "/statuses/" in args[3]:
            payload = json.loads(input)
            self.statuses.append(payload)
            return Proc(0, json.dumps({"state": payload["state"]}))
        if args[0] == "api" and len(args) == 2:
            return Proc(0, json.dumps(self._get(args[1])))
        return Proc(1, "", f"unexpected gh: {args}")

    def _get(self, path):
        base, _, query = path.partition("?")
        page = 1
        for part in query.split("&"):
            if part.startswith("page="):
                page = int(part.split("=", 1)[1])
        if base.endswith("/files"):
            return self._page(self.file_pages, page)
        if base.endswith("/reviews"):
            return self._page(self.review_pages, page)
        if "/pulls/" in base:
            return self.pr
        raise AssertionError(path)

    @staticmethod
    def _page(groups, page):
        if page < 1 or page > len(groups):
            return []
        return groups[page - 1]

    def _graphql(self, payload):
        query = payload["query"]
        variables = payload["variables"]
        if "resolveReviewThread" in query:
            self.resolves.append(variables["id"])
            body = {"data": {"resolveReviewThread": {"thread": {"isResolved": True}}}}
            return Proc(0, json.dumps(body))
        cursor = variables.get("cursor")
        self.cursors.append(cursor)
        idx = 0 if not cursor else int(cursor[1:]) + 1
        nodes = self.thread_pages[idx] if idx < len(self.thread_pages) else []
        has_next = idx + 1 < len(self.thread_pages)
        body = {"data": {"repository": {"pullRequest": {"reviewThreads": {
            "pageInfo": {"hasNextPage": has_next, "endCursor": f"c{idx}" if has_next else None},
            "nodes": nodes,
        }}}}}
        return Proc(0, json.dumps(body))

    def merge_calls(self):
        return [args for args, _ in self.calls if args[:2] == ["pr", "merge"]]

    def write_calls(self):
        writes = []
        for args, _ in self.calls:
            if args[:2] == ["pr", "merge"] or "--method" in args:
                writes.append(args)
        return writes


def _env(monkeypatch, world, **extra):
    monkeypatch.setattr(mod.subprocess, "run", world.run)
    monkeypatch.setenv("GITHUB_REPOSITORY", REPO)
    monkeypatch.setenv("PR", extra.get("pr", "7"))
    monkeypatch.setenv("GH_TOKEN", "test-token-not-on-argv")
    monkeypatch.setenv("RUN_URL", extra.get("run_url", RUN_URL))
    if "webhook_url" in extra:
        monkeypatch.setenv("SAGE_REVIEW_WEBHOOK_URL", extra["webhook_url"])
    else:
        monkeypatch.delenv("SAGE_REVIEW_WEBHOOK_URL", raising=False)
    if "webhook_key" in extra:
        monkeypatch.setenv("SAGE_REVIEW_WEBHOOK_KEY", extra["webhook_key"])
    else:
        monkeypatch.delenv("SAGE_REVIEW_WEBHOOK_KEY", raising=False)


def _gate(monkeypatch, pr=None, files=None, reviews=None, threads=None, merges=None, **env):
    world = World(pr=pr, files=files, reviews=reviews, threads=threads, merges=merges)
    _env(monkeypatch, world, **env)
    rc = mod.gate()
    return rc, world


def _status(world):
    assert len(world.statuses) == 1
    return world.statuses[0]


def _assert_no_admin(world):
    for args, blob in world.calls:
        flat = " ".join(args) + (blob or "")
        assert "--admin" not in flat
        assert "test-token-not-on-argv" not in flat


def _auto(sha=SHA):
    return ["pr", "merge", "7", "--repo", REPO, "--auto", "--squash", "--match-head-commit", sha]


def _direct(sha=SHA):
    return ["pr", "merge", "7", "--repo", REPO, "--squash", "--match-head-commit", sha]


def _disable():
    return ["pr", "merge", "7", "--repo", REPO, "--disable-auto"]


# ---- code PR: Reed only ----------------------------------------------------

def test_code_pr_missing_review_is_pending(monkeypatch):
    rc, world = _gate(monkeypatch, files=[{"filename": "src/a.py"}], reviews=[])
    assert rc == 0
    st = _status(world)
    assert st["state"] == "pending" and st["description"] == "Waiting for Reed QA"
    assert st["context"] == "approval-gate" and st["target_url"] == RUN_URL
    assert world.merge_calls() == []
    assert world.cursors == []


def test_code_pr_commented_only_is_pending(monkeypatch):
    rc, world = _gate(
        monkeypatch,
        files=[{"filename": "src/a.py"}],
        reviews=[review("COMMENTED", REED)],
    )
    assert rc == 0
    assert _status(world)["description"] == "Waiting for Reed QA"
    assert world.merge_calls() == []


def test_code_pr_dismissed_is_pending(monkeypatch):
    rc, world = _gate(
        monkeypatch,
        files=[{"filename": "src/a.py"}],
        reviews=[review("DISMISSED", REED)],
    )
    assert rc == 0
    assert _status(world)["state"] == "pending"
    assert world.merge_calls() == []


def test_code_pr_stale_commit_does_not_count(monkeypatch):
    rc, world = _gate(
        monkeypatch,
        files=[{"filename": "src/a.py"}],
        reviews=[review("APPROVED", REED, sha=OTHER_SHA)],
    )
    assert rc == 0
    assert _status(world)["description"] == "Waiting for Reed QA"
    assert world.merge_calls() == []


def test_code_pr_non_bot_approval_does_not_count(monkeypatch):
    rc, world = _gate(
        monkeypatch,
        files=[{"filename": "src/a.py"}],
        reviews=[review("APPROVED", REED, user="eric")],
    )
    assert rc == 0
    assert _status(world)["description"] == "Waiting for Reed QA"
    assert world.merge_calls() == []


def test_sage_approval_does_not_satisfy_reed(monkeypatch):
    rc, world = _gate(
        monkeypatch,
        files=[{"filename": "src/a.py"}],
        reviews=[review("APPROVED", SAGE, rid=2, submitted="2026-11-19T01:00:00Z")],
    )
    assert rc == 0
    assert _status(world)["description"] == "Waiting for Reed QA"
    assert world.merge_calls() == []
    assert world.resolves == []


def test_newer_commented_supersedes_approval(monkeypatch):
    reviews = [
        review("APPROVED", REED, rid=1, submitted="2026-11-19T00:00:00Z"),
        review("COMMENTED", REED, rid=2, submitted="2026-11-19T01:00:00Z"),
    ]
    rc, world = _gate(monkeypatch, files=[{"filename": "src/a.py"}], reviews=reviews)
    assert rc == 0
    assert _status(world)["state"] == "pending"
    assert world.merge_calls() == []


def test_newer_changes_requested_supersedes_approval(monkeypatch):
    reviews = [
        review("APPROVED", REED, rid=1, submitted="2026-11-19T00:00:00Z"),
        review("CHANGES_REQUESTED", REED, rid=2, submitted="2026-11-19T02:00:00Z"),
    ]
    rc, world = _gate(monkeypatch, files=[{"filename": "src/a.py"}], reviews=reviews)
    assert rc == 0
    assert _status(world)["description"] == "Waiting for Reed QA"


def test_commented_then_approved_enables_auto_merge(monkeypatch):
    reviews = [
        review("COMMENTED", REED, rid=1, submitted="2026-11-19T00:00:00Z"),
        review("APPROVED", REED, rid=2, submitted="2026-11-19T03:00:00Z"),
    ]
    rc, world = _gate(monkeypatch, files=[{"filename": "src/a.py"}], reviews=reviews)
    assert rc == 0
    st = _status(world)
    assert st["state"] == "success" and st["description"] == "Reed approved"
    assert world.merge_calls() == [_auto()]
    _assert_no_admin(world)


def test_same_timestamp_higher_id_wins(monkeypatch):
    reviews = [
        review("APPROVED", REED, rid=1, submitted="2026-11-19T00:00:00Z"),
        review("COMMENTED", REED, rid=5, submitted="2026-11-19T00:00:00Z"),
    ]
    rc, world = _gate(monkeypatch, files=[{"filename": "src/a.py"}], reviews=reviews)
    assert rc == 0
    assert _status(world)["state"] == "pending"
    assert world.merge_calls() == []


def test_leading_bom_still_matches_automation(monkeypatch):
    body = "\ufeff" + marker(REED)
    rc, world = _gate(
        monkeypatch,
        files=[{"filename": "src/a.py"}],
        reviews=[review("APPROVED", REED, body=body)],
    )
    assert rc == 0
    assert _status(world)["state"] == "success"
    assert world.merge_calls() == [_auto()]


def test_later_sage_comment_does_not_supersede_reed(monkeypatch):
    reviews = [
        review("APPROVED", REED, rid=1, submitted="2026-11-19T00:00:00Z"),
        review("COMMENTED", SAGE, rid=2, submitted="2026-11-19T04:00:00Z"),
    ]
    rc, world = _gate(monkeypatch, files=[{"filename": "src/a.py"}], reviews=reviews)
    assert rc == 0
    assert _status(world)["description"] == "Reed approved"
    assert world.merge_calls() == [_auto()]


def test_empty_run_url_omits_target_url(monkeypatch):
    rc, world = _gate(
        monkeypatch,
        files=[{"filename": "src/a.py"}],
        reviews=[review("APPROVED", REED)],
        run_url="",
    )
    assert rc == 0
    assert "target_url" not in _status(world)


# ---- skill / CLAUDE.md -----------------------------------------------------

def test_claude_md_reed_only_waits_for_sage(monkeypatch):
    rc, world = _gate(
        monkeypatch,
        files=[{"filename": "CLAUDE.md"}],
        reviews=[review("APPROVED", REED)],
    )
    assert rc == 0
    assert _status(world)["description"] == "Waiting for Sage"
    assert _status(world)["state"] == "pending"
    assert world.merge_calls() == []


def test_skill_path_sage_only_waits_for_reed(monkeypatch):
    rc, world = _gate(
        monkeypatch,
        files=[{"filename": ".claude/skills/foo/SKILL.md"}],
        reviews=[review("APPROVED", SAGE)],
        threads=_thread_pages(),
    )
    assert rc == 0
    assert _status(world)["description"] == "Waiting for Reed QA"
    assert world.resolves == ["sage-open"]
    assert world.merge_calls() == []


def test_skill_neither_review_names_both(monkeypatch):
    rc, world = _gate(
        monkeypatch,
        files=[{"filename": ".claude/skills/foo/SKILL.md"}],
        reviews=[],
    )
    assert rc == 0
    assert _status(world)["description"] == "Waiting for Reed QA and Sage"
    assert world.merge_calls() == []
    assert world.cursors == []


def test_skill_both_approved_enables_auto_merge(monkeypatch):
    reviews = [
        review("APPROVED", REED, rid=1, submitted="2026-11-19T00:00:00Z"),
        review("APPROVED", SAGE, rid=2, submitted="2026-11-19T00:01:00Z"),
    ]
    rc, world = _gate(
        monkeypatch,
        files=[{"filename": "CLAUDE.md"}, {"filename": "src/a.py"}],
        reviews=reviews,
    )
    assert rc == 0
    st = _status(world)
    assert st["state"] == "success" and st["description"] == "Reed + Sage approved"
    assert world.merge_calls() == [_auto()]
    _assert_no_admin(world)


def test_renamed_skill_previous_filename_requires_sage(monkeypatch):
    files = [{"filename": "docs/moved.md", "previous_filename": ".claude/skills/foo/SKILL.md"}]
    rc, world = _gate(monkeypatch, files=files, reviews=[review("APPROVED", REED)])
    assert rc == 0
    assert _status(world)["description"] == "Waiting for Sage"
    assert world.merge_calls() == []


def test_src_claude_md_is_not_a_skill_change(monkeypatch):
    rc, world = _gate(
        monkeypatch,
        files=[{"filename": "src/CLAUDE.md"}],
        reviews=[review("APPROVED", REED)],
    )
    assert rc == 0
    assert _status(world)["description"] == "Reed approved"
    assert world.merge_calls() == [_auto()]


# ---- thread resolve --------------------------------------------------------

def _thread_pages():
    page1 = [
        thread("reed-open", "cursor", marker(REED)),
        thread("reed-done", "cursor", marker(REED), resolved=True),
        thread("human", "eric", marker(REED)),
        thread("claude", "claude", marker(REED)),
        thread("actions-bot", "github-actions", marker(SAGE)),
        thread("no-marker", "cursor", "just a comment\n"),
    ]
    page2 = [
        thread("sage-open", "cursor", marker(SAGE)),
        thread("other-auto", "cursor", marker(OTHER_AUTO)),
        thread("actions", "github-actions[bot]", marker(REED)),
        thread("empty", "", None),
    ]
    return [page1, page2]


def test_reed_approved_resolves_only_reed_threads(monkeypatch):
    rc, world = _gate(
        monkeypatch,
        files=[{"filename": ".claude/skills/foo/SKILL.md"}],
        reviews=[review("APPROVED", REED)],
        threads=_thread_pages(),
    )
    assert rc == 0
    assert _status(world)["description"] == "Waiting for Sage"
    assert world.cursors == [None, "c0"]
    assert world.resolves == ["reed-open"]
    assert world.merge_calls() == []


def test_both_approved_resolves_reed_and_sage_threads(monkeypatch):
    reviews = [
        review("APPROVED", REED, rid=1),
        review("APPROVED", SAGE, rid=2, submitted="2026-11-19T00:05:00Z"),
    ]
    rc, world = _gate(
        monkeypatch,
        files=[{"filename": "CLAUDE.md"}],
        reviews=reviews,
        threads=_thread_pages(),
    )
    assert rc == 0
    assert world.cursors == [None, "c0"]
    assert world.resolves == ["reed-open", "sage-open"]
    kinds = []
    for args, blob in world.calls:
        if "graphql" in args and blob and "resolveReviewThread" in blob:
            kinds.append("resolve")
        elif "/statuses/" in " ".join(args):
            kinds.append("status")
        elif args[:2] == ["pr", "merge"]:
            kinds.append("merge")
    assert kinds.index("resolve") < kinds.index("status") < kinds.index("merge")
    assert world.merge_calls() == [_auto()]


def test_unapproved_does_not_list_threads(monkeypatch):
    rc, world = _gate(
        monkeypatch,
        files=[{"filename": "src/a.py"}],
        reviews=[review("COMMENTED", REED)],
        threads=_thread_pages(),
    )
    assert rc == 0
    assert world.cursors == []
    assert world.resolves == []


# ---- exclusions and disable-auto ------------------------------------------

def test_version_py_posts_status_and_disables_auto_merge(monkeypatch):
    pr = make_pr(auto_merge={"enabled_by": {"login": "ericwu13"}})
    rc, world = _gate(
        monkeypatch,
        pr=pr,
        files=[{"filename": "version.py"}],
        reviews=[review("APPROVED", REED)],
    )
    assert rc == 0
    assert _status(world)["state"] == "success"
    assert _status(world)["description"] == "Reed approved"
    assert world.merge_calls() == [_disable()]
    _assert_no_admin(world)


def test_workflow_file_is_excluded(monkeypatch):
    pr = make_pr(auto_merge={"enabled_by": {"login": "ericwu13"}})
    rc, world = _gate(
        monkeypatch,
        pr=pr,
        files=[{"filename": ".github/workflows/foo.yml"}],
        reviews=[review("APPROVED", REED)],
    )
    assert rc == 0
    assert _status(world)["description"] == "Reed approved"
    assert world.merge_calls() == [_disable()]


def test_workflow_renamed_out_is_still_excluded(monkeypatch):
    pr = make_pr(auto_merge={"enabled_by": {"login": "ericwu13"}})
    files = [{"filename": "docs/foo.yml", "previous_filename": ".github/workflows/foo.yml"}]
    rc, world = _gate(monkeypatch, pr=pr, files=files, reviews=[review("APPROVED", REED)])
    assert rc == 0
    assert _status(world)["state"] == "success"
    assert world.merge_calls() == [_disable()]


def test_ci_script_is_excluded_and_disarms_auto_merge(monkeypatch):
    pr = make_pr(auto_merge={"enabled_by": {"login": "ericwu13"}})
    rc, world = _gate(
        monkeypatch,
        pr=pr,
        files=[{"filename": "scripts/ci/auto_merge_gate.py"}],
        reviews=[review("APPROVED", REED)],
    )
    assert rc == 0
    st = _status(world)
    assert st["state"] == "success" and st["description"] == "Reed approved"
    assert world.merge_calls() == [_disable()]
    _assert_no_admin(world)


def test_ci_script_renamed_out_is_still_excluded(monkeypatch):
    pr = make_pr(auto_merge={"enabled_by": {"login": "ericwu13"}})
    files = [{"filename": "scripts/foo.py", "previous_filename": "scripts/ci/auto_merge_gate.py"}]
    rc, world = _gate(monkeypatch, pr=pr, files=files, reviews=[review("APPROVED", REED)])
    assert rc == 0
    assert _status(world)["state"] == "success"
    assert world.merge_calls() == [_disable()]


def test_status_error_disarms_auto_merge_first(monkeypatch):
    """An unapproved PR must drop auto-merge before the status post can fail."""
    pr = make_pr(auto_merge={"enabled_by": {"login": "ericwu13"}})
    world = World(pr=pr, files=[{"filename": "src/a.py"}], reviews=[])
    _env(monkeypatch, world)
    seen = {}

    def boom(*_args, **_kwargs):
        seen["merges"] = list(world.merge_calls())
        raise RuntimeError("status post failed")

    monkeypatch.setattr(mod, "post_status", boom)
    rc = mod.gate()
    assert rc != 0
    assert seen["merges"] == [_disable()]
    assert world.merge_calls() == [_disable()]


def test_needs_owner_blocks_auto_merge(monkeypatch):
    pr = make_pr(
        labels=[{"name": "needs-owner"}],
        auto_merge={"enabled_by": {"login": "ericwu13"}},
    )
    reviews = [
        review("APPROVED", REED, rid=1),
        review("APPROVED", SAGE, rid=2, submitted="2026-11-19T00:02:00Z"),
    ]
    rc, world = _gate(
        monkeypatch, pr=pr, files=[{"filename": "CLAUDE.md"}], reviews=reviews,
    )
    assert rc == 0
    assert _status(world)["description"] == "Reed + Sage approved"
    assert world.merge_calls() == [_disable()]


def test_draft_blocks_auto_merge(monkeypatch):
    pr = make_pr(draft=True, auto_merge={"enabled_by": {"login": "ericwu13"}})
    rc, world = _gate(
        monkeypatch, pr=pr, files=[{"filename": "src/a.py"}],
        reviews=[review("APPROVED", REED)],
    )
    assert rc == 0
    assert _status(world)["description"] == "Reed approved"
    assert world.merge_calls() == [_disable()]


def test_dismissed_review_disables_enabled_auto_merge(monkeypatch):
    pr = make_pr(auto_merge={"enabled_by": {"login": "ericwu13"}})
    reviews = [
        review("APPROVED", REED, rid=1, submitted="2026-11-19T00:00:00Z"),
        review("DISMISSED", REED, rid=2, submitted="2026-11-19T05:00:00Z"),
    ]
    rc, world = _gate(monkeypatch, pr=pr, files=[{"filename": "src/a.py"}], reviews=reviews)
    assert rc == 0
    assert _status(world)["state"] == "pending"
    assert _status(world)["description"] == "Waiting for Reed QA"
    assert world.merge_calls() == [_disable()]


def test_excluded_without_auto_merge_does_not_disable(monkeypatch):
    rc, world = _gate(
        monkeypatch,
        files=[{"filename": "version.py"}],
        reviews=[review("APPROVED", REED)],
    )
    assert rc == 0
    assert _status(world)["state"] == "success"
    assert world.merge_calls() == []


# ---- ignored PRs -----------------------------------------------------------

@pytest.mark.parametrize("over", [
    {"state": "closed"},
    {"head": {"repo": {"full_name": "other/tai-robot"}}},
    {"base": {"ref": "develop"}},
    {"head": {"sha": "abc"}},
    {"head": {"sha": "g" * 40}},
])
def test_gate_ignores_ineligible_pr(monkeypatch, over):
    rc, world = _gate(
        monkeypatch,
        pr=make_pr(**over),
        files=[{"filename": "src/a.py"}],
        reviews=[review("APPROVED", REED)],
    )
    assert rc == 0
    assert world.statuses == []
    assert world.write_calls() == []


# ---- pagination ------------------------------------------------------------

def test_file_on_second_page_still_excludes_release(monkeypatch):
    files = [{"filename": f"src/f{i}.py"} for i in range(100)]
    files.append({"filename": "version.py"})
    pr = make_pr(auto_merge={"enabled_by": {"login": "ericwu13"}})
    rc, world = _gate(
        monkeypatch, pr=pr, files=files, reviews=[review("APPROVED", REED)],
    )
    assert rc == 0
    assert _status(world)["description"] == "Reed approved"
    assert world.merge_calls() == [_disable()]
    got = [args[1] for args, _ in world.calls if args[0] == "api" and "/files?" in args[1]]
    assert any("per_page=100&page=1" in p for p in got)
    assert any("per_page=100&page=2" in p for p in got)


def test_approval_on_second_review_page_counts(monkeypatch):
    reviews = []
    for i in range(100):
        reviews.append(review(
            "COMMENTED", REED, rid=i + 1,
            submitted=f"2026-11-01T00:{i // 60:02d}:{i % 60:02d}Z",
        ))
    reviews.append(review("APPROVED", REED, rid=101, submitted="2026-11-19T00:00:00Z"))
    rc, world = _gate(monkeypatch, files=[{"filename": "src/a.py"}], reviews=reviews)
    assert rc == 0
    assert _status(world)["state"] == "success"
    assert world.merge_calls() == [_auto()]
    got = [args[1] for args, _ in world.calls if args[0] == "api" and "/reviews?" in args[1]]
    assert any("page=2" in p for p in got)


# ---- merge fallback --------------------------------------------------------

def test_clean_status_falls_back_to_direct_squash(monkeypatch):
    merges = [
        (1, "", "GraphQL: Pull request is in clean status\n"),
        (0, "merged\n", ""),
    ]
    rc, world = _gate(
        monkeypatch,
        files=[{"filename": "src/a.py"}],
        reviews=[review("APPROVED", REED)],
        merges=merges,
    )
    assert rc == 0
    assert world.merge_calls() == [_auto(), _direct()]
    _assert_no_admin(world)


def test_already_merged_on_auto_is_success(monkeypatch):
    rc, world = _gate(
        monkeypatch,
        files=[{"filename": "src/a.py"}],
        reviews=[review("APPROVED", REED)],
        merges=[(1, "Already Merged\n", "")],
    )
    assert rc == 0
    assert world.merge_calls() == [_auto()]


def test_already_merged_on_fallback_is_success(monkeypatch):
    merges = [
        (1, "", "Pull request is in clean status"),
        (1, "", "Pull request already merged"),
    ]
    rc, world = _gate(
        monkeypatch,
        files=[{"filename": "src/a.py"}],
        reviews=[review("APPROVED", REED)],
        merges=merges,
    )
    assert rc == 0
    assert world.merge_calls() == [_auto(), _direct()]


def test_non_clean_error_does_not_merge_directly(monkeypatch, capsys):
    rc, world = _gate(
        monkeypatch,
        files=[{"filename": "src/a.py"}],
        reviews=[review("APPROVED", REED)],
        merges=[(1, "", "merge conflict on src/a.py")],
    )
    assert rc == 1
    assert world.merge_calls() == [_auto()]
    err = capsys.readouterr().err
    assert "merge conflict" in err
    _assert_no_admin(world)


def test_fallback_error_does_not_retry_again(monkeypatch):
    merges = [
        (1, "", "Pull request is in clean status"),
        (2, "", "required status check missing"),
    ]
    rc, world = _gate(
        monkeypatch,
        files=[{"filename": "src/a.py"}],
        reviews=[review("APPROVED", REED)],
        merges=merges,
    )
    assert rc == 2
    assert world.merge_calls() == [_auto(), _direct()]


# ---- trigger-sage ----------------------------------------------------------

def _trigger(monkeypatch, pr=None, files=None, reviews=None, opener=None):
    world = World(pr=pr, files=files, reviews=reviews)
    _env(monkeypatch, world, webhook_url=WEBHOOK_URL, webhook_key=WEBHOOK_KEY)
    captured = {}

    def default_open(req, timeout=30):
        captured["req"] = req
        captured["timeout"] = timeout

        class Resp:
            status = 204

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def getcode(self):
                return 204

        return Resp()

    monkeypatch.setattr(mod.urllib.request, "urlopen", opener or default_open)
    rc = mod.trigger_sage()
    return rc, world, captured


def test_trigger_sage_skips_draft(monkeypatch):
    rc, world, captured = _trigger(
        monkeypatch, pr=make_pr(draft=True), files=[{"filename": "CLAUDE.md"}],
    )
    assert rc == 0 and captured == {}
    assert not any("/files?" in args[1] for args, _ in world.calls if len(args) == 2)


def test_trigger_sage_skips_non_skill(monkeypatch):
    rc, world, captured = _trigger(monkeypatch, files=[{"filename": "src/a.py"}])
    assert rc == 0 and captured == {}
    assert not any("/reviews?" in args[1] for args, _ in world.calls if len(args) == 2)


def test_trigger_sage_skips_when_sage_already_reviewed_head(monkeypatch):
    rc, world, captured = _trigger(
        monkeypatch,
        files=[{"filename": "CLAUDE.md"}],
        reviews=[review("COMMENTED", SAGE)],
    )
    assert rc == 0 and captured == {}
    assert world.write_calls() == []


def test_trigger_sage_does_not_skip_for_reed_review(monkeypatch):
    rc, world, captured = _trigger(
        monkeypatch,
        files=[{"filename": ".claude/skills/foo/SKILL.md"}],
        reviews=[review("APPROVED", REED)],
    )
    assert rc == 0
    req = captured["req"]
    assert json.loads(req.data.decode()) == {"pr": 7, "head_sha": SHA}
    assert req.get_header("Authorization") == "Bearer " + WEBHOOK_KEY
    assert req.get_header("Content-type") == "application/json"
    assert captured["timeout"] == 30
    assert world.merge_calls() == []


def test_trigger_sage_stale_sage_review_still_sends(monkeypatch):
    rc, _world, captured = _trigger(
        monkeypatch,
        files=[{"filename": "CLAUDE.md"}],
        reviews=[review("APPROVED", SAGE, sha=OTHER_SHA)],
    )
    assert rc == 0
    assert json.loads(captured["req"].data.decode())["head_sha"] == SHA


def test_trigger_sage_human_sage_marker_still_sends(monkeypatch):
    rc, _world, captured = _trigger(
        monkeypatch,
        files=[{"filename": "CLAUDE.md"}],
        reviews=[review("APPROVED", SAGE, user="eric")],
    )
    assert rc == 0 and "req" in captured


def test_trigger_sage_skill_file_on_second_page_sends(monkeypatch):
    files = [{"filename": f"src/f{i}.py"} for i in range(100)]
    files.append({"filename": "CLAUDE.md"})
    rc, world, captured = _trigger(monkeypatch, files=files, reviews=[])
    assert rc == 0 and "req" in captured
    got = [args[1] for args, _ in world.calls if args[0] == "api" and "/files?" in args[1]]
    assert any("page=2" in p for p in got)


def test_trigger_sage_review_on_second_page_dedups(monkeypatch):
    reviews = [review("COMMENTED", REED, rid=i + 1) for i in range(100)]
    reviews.append(review("COMMENTED", SAGE, rid=101, submitted="2026-11-19T02:00:00Z"))
    rc, world, captured = _trigger(
        monkeypatch, files=[{"filename": "CLAUDE.md"}], reviews=reviews,
    )
    assert rc == 0 and captured == {}
    got = [args[1] for args, _ in world.calls if args[0] == "api" and "/reviews?" in args[1]]
    assert any("page=2" in p for p in got)


@pytest.mark.parametrize("over", [
    {"state": "closed"},
    {"head": {"repo": {"full_name": "other/tai-robot"}}},
    {"base": {"ref": "develop"}},
    {"head": {"sha": "zz"}},
])
def test_trigger_sage_ignores_ineligible_pr(monkeypatch, over):
    rc, world, captured = _trigger(
        monkeypatch, pr=make_pr(**over), files=[{"filename": "CLAUDE.md"}],
    )
    assert rc == 0 and captured == {}
    assert world.write_calls() == []


def test_trigger_sage_http_error_does_not_leak(monkeypatch, capsys):
    def boom(req, timeout=30):
        raise urllib.error.HTTPError(WEBHOOK_URL, 503, "nope " + WEBHOOK_KEY, hdrs=None, fp=None)

    leaked = []
    try:
        rc, _world, _cap = _trigger(
            monkeypatch, files=[{"filename": "CLAUDE.md"}], opener=boom,
        )
    except Exception as exc:
        leaked.append(str(exc))
        raise
    captured_io = capsys.readouterr()
    text = captured_io.out + captured_io.err + "".join(leaked)
    assert rc == 1
    assert "503" in text
    assert WEBHOOK_URL not in text
    assert WEBHOOK_KEY not in text


def test_trigger_sage_url_error_does_not_leak(monkeypatch, capsys):
    class SecretReason(OSError):
        def __str__(self):
            return WEBHOOK_URL + " " + WEBHOOK_KEY

    def boom(req, timeout=30):
        raise urllib.error.URLError(SecretReason())

    try:
        rc, _world, _cap = _trigger(
            monkeypatch, files=[{"filename": ".claude/skills/foo/SKILL.md"}], opener=boom,
        )
    except Exception as exc:
        assert WEBHOOK_URL not in str(exc) and WEBHOOK_KEY not in str(exc)
        raise
    captured_io = capsys.readouterr()
    text = captured_io.out + captured_io.err
    assert rc == 1
    assert "SecretReason" in text
    assert WEBHOOK_URL not in text
    assert WEBHOOK_KEY not in text


def test_trigger_sage_missing_secret_does_not_call(monkeypatch, capsys):
    world = World(files=[{"filename": "CLAUDE.md"}])
    _env(monkeypatch, world, webhook_url="", webhook_key="")
    called = []
    monkeypatch.setattr(mod.urllib.request, "urlopen", lambda *a, **k: called.append(1))
    rc = mod.trigger_sage()
    err = capsys.readouterr().err
    assert rc == 1 and called == []
    assert "not configured" in err
    assert WEBHOOK_URL not in err


# ---- workflow --------------------------------------------------------------

def test_workflow_yaml_is_least_privilege():
    text = (ROOT / ".github/workflows/auto-merge.yml").read_text(encoding="utf-8")
    py = (ROOT / "scripts/ci/auto_merge_gate.py").read_text(encoding="utf-8")
    assert "--admin" not in text and "sage-approved" not in text
    assert "--admin" not in py and "sage-approved" not in py
    doc = yaml.safe_load(text)
    assert doc["name"] == "Auto-merge gate"
    jobs = doc["jobs"]
    assert set(jobs) == {"trigger-sage", "approval-gate"}
    trig, gate = jobs["trigger-sage"], jobs["approval-gate"]
    assert trig["permissions"] == {"contents": "read", "pull-requests": "read"}
    assert gate["permissions"] == {
        "contents": "write",
        "pull-requests": "write",
        "statuses": "write",
    }
    assert "write" not in trig["permissions"].values()
    for job, group in ((trig, "trigger-sage-"), (gate, "auto-merge-")):
        assert job["concurrency"]["cancel-in-progress"] is False
        assert group in job["concurrency"]["group"]
        cond = job["if"]
        assert "github.event.pull_request.head.repo.full_name == github.repository" in cond
        assert "github.event.pull_request.base.ref == 'master'" in cond
        assert "github.event.pull_request.state == 'open'" in cond
        checkout = next(s for s in job["steps"] if str(s.get("uses", "")).startswith("actions/checkout@"))
        assert checkout["with"]["ref"] == "master"
        assert checkout["with"]["sparse-checkout"] == "scripts/ci"
        for step in job["steps"]:
            assert "${{" not in (step.get("run") or "")
    trig_run = next(s for s in trig["steps"] if "run" in s)
    assert trig_run["run"].strip() == "python3 scripts/ci/auto_merge_gate.py trigger-sage"
    assert "secrets.SAGE_REVIEW_WEBHOOK_URL" in trig_run["env"]["SAGE_REVIEW_WEBHOOK_URL"]
    assert "secrets.SAGE_REVIEW_WEBHOOK_KEY" in trig_run["env"]["SAGE_REVIEW_WEBHOOK_KEY"]
    gate_run = next(s for s in gate["steps"] if "run" in s)
    assert gate_run["run"].strip() == "python3 scripts/ci/auto_merge_gate.py gate"
    assert "SAGE_REVIEW_WEBHOOK" not in yaml.dump(gate)
    assert "needs-owner" in gate["if"]
    assert "cursor[bot]" in gate["if"]
    assert "github.event_name == 'pull_request_target'" in trig["if"]
    assert "ready_for_review" in trig["if"]
    assert "!github.event.pull_request.draft" in trig["if"]
    assert "pull_request_target:" in text
    assert "pull_request_review:" in text
    for token in ("opened", "synchronize", "reopened", "ready_for_review",
                  "converted_to_draft", "labeled", "unlabeled", "submitted", "dismissed"):
        assert token in text
    assert "pull_request.head.sha" not in text
