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
REED_URL = "https://hooks.example/reed-secret-path-XYZ"
REED_KEY = "reed-secret-key-ABC"
SELINA_URL = "https://hooks.example/selina-secret-path-XYZ"
SELINA_KEY = "selina-secret-key-ABC"


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

    def __init__(self, pr=None, files=None, reviews=None, threads=None, merges=None,
                 open_prs=None, check_runs=None, review_obj=None):
        self.pr = make_pr() if pr is None else pr
        self.file_pages = files if files and isinstance(files[0], list) else pages_of(files or [])
        self.review_pages = (reviews if reviews and isinstance(reviews[0], list)
                             else pages_of(reviews or []))
        self.thread_pages = threads if threads is not None else [[]]
        self.merge_script = list(merges or [(0, "ok\n", "")])
        self.open_pr_pages = pages_of(open_prs or [])
        self.check_pages = pages_of(check_runs or [])
        self.review_obj = review_obj
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
        if "/reviews/" in base:
            if self.review_obj is None:
                raise AssertionError(path)
            return self.review_obj
        if base.endswith("/pulls"):
            return self._page(self.open_pr_pages, page)
        if base.endswith("/check-runs"):
            return {"check_runs": self._page(self.check_pages, page)}
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


# ---- retrigger-reed / notify-selina ---------------------------------------

class _HookResp:
    status = 204

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def getcode(self):
        return 204


def _gets(world):
    return [args[1] for args, _ in world.calls if args[:1] == ["api"] and len(args) == 2]


def _bodies(posts):
    return [json.loads(req.data.decode()) for req, _timeout in posts]


def _capture_open(posts):
    def opener(req, timeout=30):
        posts.append((req, timeout))
        return _HookResp()
    return opener


def _set_or_del(monkeypatch, name, value):
    if value is None:
        monkeypatch.delenv(name, raising=False)
    else:
        monkeypatch.setenv(name, value)


def _clear_hooks(monkeypatch):
    for name in (
        "SAGE_REVIEW_WEBHOOK_URL", "SAGE_REVIEW_WEBHOOK_KEY",
        "REED_QA_WEBHOOK_URL", "REED_QA_WEBHOOK_KEY",
        "SELINA_QA_WEBHOOK_URL", "SELINA_QA_WEBHOOK_KEY",
    ):
        monkeypatch.delenv(name, raising=False)


def check_run(name, status="completed", conclusion="success", rid=1,
              completed="2026-11-19T03:00:00Z", started="2026-11-19T02:00:00Z"):
    return {
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "id": rid,
        "completed_at": completed,
        "started_at": started,
    }


def green_checks():
    """test + review green. A later failing lint run must not count."""
    return [
        check_run("test", rid=11),
        check_run("review", rid=12),
        check_run("lint", conclusion="failure", rid=13, completed="2026-11-19T05:00:00Z"),
    ]


def wait_body():
    return marker(REED, "\n\n**WAIT**\n")


def _run_reed(monkeypatch, prs, reviews=None, checks=None, head_sha=SHA,
              url=REED_URL, key=REED_KEY, opener=None, repo=REPO,
              event="", workflow_name="Claude Review", pr=None, pr_number="7"):
    world = World(pr=pr, open_prs=prs, reviews=reviews, check_runs=checks)
    monkeypatch.setattr(mod.subprocess, "run", world.run)
    monkeypatch.setenv("GH_TOKEN", "test-token-not-on-argv")
    _set_or_del(monkeypatch, "GITHUB_REPOSITORY", repo)
    _set_or_del(monkeypatch, "EVENT_NAME", event)
    _set_or_del(monkeypatch, "PR", pr_number)
    _set_or_del(monkeypatch, "HEAD_SHA", head_sha)
    _set_or_del(monkeypatch, "WORKFLOW_NAME", workflow_name)
    _clear_hooks(monkeypatch)
    _set_or_del(monkeypatch, "REED_QA_WEBHOOK_URL", url)
    _set_or_del(monkeypatch, "REED_QA_WEBHOOK_KEY", key)
    posts = []
    monkeypatch.setattr(mod.urllib.request, "urlopen", opener or _capture_open(posts))
    rc = mod.main(["auto_merge_gate.py", "retrigger-reed"])
    return rc, world, posts


def _run_selina(monkeypatch, event, pr=None, review_obj=None, open_prs=None,
                url=SELINA_URL, key=SELINA_KEY, opener=None, repo=REPO, **env):
    world = World(pr=pr, open_prs=open_prs, review_obj=review_obj)
    monkeypatch.setattr(mod.subprocess, "run", world.run)
    monkeypatch.setenv("GH_TOKEN", "test-token-not-on-argv")
    _set_or_del(monkeypatch, "GITHUB_REPOSITORY", repo)
    _clear_hooks(monkeypatch)
    _set_or_del(monkeypatch, "SELINA_QA_WEBHOOK_URL", url)
    _set_or_del(monkeypatch, "SELINA_QA_WEBHOOK_KEY", key)
    _set_or_del(monkeypatch, "EVENT_NAME", event)
    _set_or_del(monkeypatch, "PR", env.get("pr_number", "7"))
    _set_or_del(monkeypatch, "REVIEW_ID", env.get("review_id", "42"))
    _set_or_del(monkeypatch, "REVIEW_STATE", env.get("review_state", "SPOOFED"))
    _set_or_del(monkeypatch, "HEAD_SHA", env.get("head_sha", "f" * 40))
    _set_or_del(monkeypatch, "WORKFLOW_NAME", env.get("workflow_name", "SPOOFED"))
    _set_or_del(monkeypatch, "WORKFLOW_CONCLUSION", env.get("conclusion", "SPOOFED"))
    _set_or_del(monkeypatch, "MERGED_HEAD_REF", env.get("merged_head_ref", "spoofed-ref"))
    posts = []
    monkeypatch.setattr(mod.urllib.request, "urlopen", opener or _capture_open(posts))
    rc = mod.main(["auto_merge_gate.py", "notify-selina"])
    return rc, world, posts


def _assert_hook(posts, secret):
    req, timeout = posts[0]
    assert req.get_header("Authorization") == "Bearer " + secret
    assert req.get_header("Content-type") == "application/json"
    assert timeout == 30
    blob = req.data.decode() + (req.get_header("Authorization") or "")
    assert "test-token-not-on-argv" not in blob
    assert secret in req.get_header("Authorization")


def test_retrigger_reed_posts_once_for_single_wait(monkeypatch):
    prs = [
        make_pr(number=7),
        make_pr(number=8, draft=True),
        make_pr(number=9, base={"ref": "feature"}),
        make_pr(number=10, head={"repo": {"full_name": "other/tai-robot"}}),
        make_pr(number=11, head={"sha": OTHER_SHA}),
    ]
    reviews = [review("COMMENTED", REED, rid=5, body=wait_body())]
    rc, world, posts = _run_reed(monkeypatch, prs, reviews=reviews, checks=green_checks())
    assert rc == 0
    assert _bodies(posts) == [{"pr": 7, "head_sha": SHA}]
    _assert_hook(posts, REED_KEY)
    assert REED_URL not in posts[0][0].data.decode()
    assert world.write_calls() == []
    _assert_no_admin(world)
    gets = _gets(world)
    assert f"repos/{REPO}/pulls?state=open&per_page=100&page=1" in gets
    assert f"repos/{REPO}/commits/{SHA}/check-runs?per_page=100&page=1" in gets
    assert any("/pulls/7/reviews?per_page=100&page=1" in g for g in gets)
    assert not any("/pulls/8/reviews" in g or "/pulls/9/reviews" in g for g in gets)


def test_retrigger_reed_counts_only_reviews_at_head(monkeypatch):
    reviews = [
        review("APPROVED", REED, sha=OTHER_SHA, rid=1),
        review("COMMENTED", REED, sha=OTHER_SHA, rid=2, body=wait_body()),
        review("COMMENTED", REED, rid=3, body=wait_body()),
    ]
    rc, _, posts = _run_reed(
        monkeypatch, [make_pr(number=7)], reviews=reviews, checks=green_checks(),
    )
    assert rc == 0
    assert _bodies(posts) == [{"pr": 7, "head_sha": SHA}]


def test_retrigger_reed_finds_wait_on_second_review_page(monkeypatch):
    reviews = [
        review("COMMENTED", REED, sha=OTHER_SHA, rid=i, body=wait_body())
        for i in range(1, 101)
    ]
    reviews.append(review("COMMENTED", REED, rid=101, body=wait_body()))
    rc, world, posts = _run_reed(
        monkeypatch, [make_pr(number=7)], reviews=reviews, checks=green_checks(),
    )
    assert rc == 0
    assert _bodies(posts) == [{"pr": 7, "head_sha": SHA}]
    assert any("page=2" in g and "/reviews?" in g for g in _gets(world))


def test_retrigger_reed_finds_pr_on_second_page(monkeypatch):
    prs = [make_pr(number=i, head={"sha": OTHER_SHA}) for i in range(1, 101)]
    prs.append(make_pr(number=101))
    rc, world, posts = _run_reed(
        monkeypatch, prs,
        reviews=[review("COMMENTED", REED, body=wait_body())],
        checks=green_checks(),
    )
    assert rc == 0
    assert _bodies(posts) == [{"pr": 101, "head_sha": SHA}]
    assert f"repos/{REPO}/pulls?state=open&per_page=100&page=2" in _gets(world)


def test_retrigger_reed_reads_check_runs_past_first_page(monkeypatch):
    old = []
    for i in range(100):
        name = "test" if i % 2 == 0 else "review"
        old.append(check_run(
            name, conclusion="failure", rid=i + 1, completed="2026-11-18T00:00:00Z",
        ))
    checks = old + [
        check_run("test", conclusion="success", rid=201, completed="2026-11-19T04:00:00Z"),
        check_run("review", conclusion="success", rid=202, completed="2026-11-19T04:00:00Z"),
    ]
    rc, world, posts = _run_reed(
        monkeypatch, [make_pr(number=7)],
        reviews=[review("COMMENTED", REED, body=wait_body())],
        checks=checks,
    )
    assert rc == 0
    assert _bodies(posts) == [{"pr": 7, "head_sha": SHA}]
    assert f"repos/{REPO}/commits/{SHA}/check-runs?per_page=100&page=2" in _gets(world)


def test_retrigger_reed_earlier_failed_review_then_success(monkeypatch):
    """An earlier review check can fail; the later success is the one that counts."""
    checks = [
        check_run("test", rid=1, conclusion="success"),
        check_run("review", rid=2, conclusion="failure", completed="2026-11-19T00:00:00Z"),
        check_run("review", rid=3, conclusion="success", completed="2026-11-19T04:00:00Z"),
    ]
    rc, _, posts = _run_reed(
        monkeypatch, [make_pr(number=7)],
        reviews=[review("COMMENTED", REED, body=wait_body())],
        checks=checks,
    )
    assert rc == 0
    assert _bodies(posts) == [{"pr": 7, "head_sha": SHA}]


_REAL_WAIT = (
    "Verdict: WAIT. Head `198c18dd975189cc8ddb710385ec481039c99981`. No P1/P2."
)


@pytest.mark.parametrize("rest", [
    "\n\n**WAIT**\n",
    "\n" + _REAL_WAIT + "\n",
    "\nverdict: WAIT. Head `abc`.\n",
    "\n\n**WAIT**\n\nbody mentions **CHANGES NEEDED** later\n",
])
def test_retrigger_reed_wait_verdicts_post(monkeypatch, rest):
    body = marker(REED, rest)
    assert mod.reed_verdict(body) == "WAIT"
    rc, _, posts = _run_reed(
        monkeypatch, [make_pr(number=7)],
        reviews=[review("COMMENTED", REED, body=body)],
        checks=green_checks(),
    )
    assert rc == 0
    assert _bodies(posts) == [{"pr": 7, "head_sha": SHA}]


@pytest.mark.parametrize("rest", [
    "\n\n**CHANGES NEEDED**\n",
    "\nVerdict: CHANGES NEEDED. Head `198c18dd975189cc8ddb710385ec481039c99981`.\n",
    "\nverdict: CHANGES NEEDED\n",
    "\n\nno verdict\n",
    "\nVerdict: LGTM\n",
    "\n\n",
])
def test_retrigger_reed_non_wait_verdicts_skip(monkeypatch, rest):
    body = marker(REED, rest)
    assert mod.reed_verdict(body) != "WAIT"
    rc, world, posts = _run_reed(
        monkeypatch, [make_pr(number=7)],
        reviews=[review("COMMENTED", REED, body=body)],
        checks=green_checks(),
    )
    assert rc == 0 and posts == []
    assert any("/reviews?" in g for g in _gets(world))


def test_queued_rerun_higher_id_is_not_green(monkeypatch):
    """A queued rerun has no timestamps. The higher id is the latest run."""
    queued = check_run(
        "test", rid=9, status="queued", conclusion=None, completed=None, started=None,
    )
    success = check_run("test", rid=1, conclusion="success")
    review_ok = check_run("review", rid=2)
    assert mod._latest_check([success, queued, review_ok], "test")["id"] == 9
    rc, world, posts = _run_reed(
        monkeypatch, [make_pr(number=7)],
        reviews=[review("COMMENTED", REED, body=wait_body())],
        checks=[success, queued, review_ok],
    )
    assert rc == 0 and posts == []
    assert any("check-runs" in g for g in _gets(world))
    assert not any("/reviews?" in g for g in _gets(world))


def test_cancelled_inverted_timestamps_do_not_change_the_pick(monkeypatch):
    """completed_at < started_at must not steal the pick from the higher id."""
    cancelled = check_run(
        "test", rid=3, conclusion="cancelled",
        completed="2026-11-19T05:00:00Z", started="2026-11-19T06:00:00Z",
    )
    success = check_run(
        "test", rid=8, conclusion="success",
        completed="2026-11-19T03:00:00Z", started="2026-11-19T02:00:00Z",
    )
    review_ok = check_run("review", rid=4)
    assert mod._latest_check([cancelled, success], "test")["id"] == 8
    rc, _, posts = _run_reed(
        monkeypatch, [make_pr(number=7)],
        reviews=[review("COMMENTED", REED, body=wait_body())],
        checks=[cancelled, success, review_ok],
        workflow_name="Tests",
    )
    assert rc == 0
    assert _bodies(posts) == [{"pr": 7, "head_sha": SHA}]


@pytest.mark.parametrize("winner,loser,test_id,review_id", [
    ("Claude Review", "Tests", 10, 20),
    ("Tests", "Claude Review", 30, 10),
])
def test_retrigger_reed_only_the_newer_check_posts(
    monkeypatch, winner, loser, test_id, review_id,
):
    checks = [check_run("test", rid=test_id), check_run("review", rid=review_id)]
    reviews = [review("COMMENTED", REED, body=wait_body())]
    prs = [make_pr(number=7)]
    rc, _, skipped = _run_reed(
        monkeypatch, prs, reviews=reviews, checks=checks, workflow_name=loser,
    )
    assert rc == 0 and skipped == []
    rc, _, posted = _run_reed(
        monkeypatch, prs, reviews=reviews, checks=checks, workflow_name=winner,
    )
    assert rc == 0
    assert _bodies(posted) == [{"pr": 7, "head_sha": SHA}]


def test_retrigger_reed_review_event_posts_once_when_green(monkeypatch):
    pr = make_pr(number=7)
    rc, world, posts = _run_reed(
        monkeypatch,
        [make_pr(number=99, head={"sha": OTHER_SHA})],
        pr=pr,
        reviews=[review("COMMENTED", REED, body=wait_body())],
        checks=green_checks(),
        event="pull_request_review",
        head_sha="",
        workflow_name="Tests",
    )
    assert rc == 0
    assert _bodies(posts) == [{"pr": 7, "head_sha": SHA}]
    gets = _gets(world)
    assert f"repos/{REPO}/pulls/7" in gets
    assert f"repos/{REPO}/commits/{SHA}/check-runs?per_page=100&page=1" in gets
    assert not any("state=open" in g or f"commits/{OTHER_SHA}" in g for g in gets)


def test_retrigger_reed_review_event_skips_when_checks_pending(monkeypatch):
    pr = make_pr(number=7)
    checks = [
        check_run("test", rid=1, status="queued", conclusion=None, completed=None, started=None),
        check_run("review", rid=2),
    ]
    rc, world, posts = _run_reed(
        monkeypatch, [], pr=pr,
        reviews=[review("COMMENTED", REED, body=wait_body())],
        checks=checks,
        event="pull_request_review",
        head_sha="",
    )
    assert rc == 0 and posts == []
    assert any("check-runs" in g for g in _gets(world))
    assert not any("/reviews?" in g for g in _gets(world))


@pytest.mark.parametrize("over", [
    {"draft": True},
    {"head": {"repo": {"full_name": "other/tai-robot"}}},
    {"base": {"ref": "develop"}},
    {"state": "closed"},
    {"head": {"sha": "abc"}},
], ids=["draft", "fork", "non-master", "closed", "bad-sha"])
def test_retrigger_reed_review_event_skips_unguarded(monkeypatch, over):
    pr = make_pr(number=7, **over)
    rc, world, posts = _run_reed(
        monkeypatch, [make_pr(number=7)], pr=pr,
        reviews=[review("COMMENTED", REED, body=wait_body())],
        checks=green_checks(),
        event="pull_request_review",
        head_sha="",
    )
    assert rc == 0 and posts == []
    gets = _gets(world)
    assert f"repos/{REPO}/pulls/7" in gets
    assert not any("check-runs" in g or "state=open" in g for g in gets)


_NOT_ONE_WAIT = [
    [],
    [review("COMMENTED", REED, sha=OTHER_SHA, body=wait_body())],
    [review("COMMENTED", SAGE, body=marker(SAGE, "\n\n**WAIT**\n"))],
    [review("COMMENTED", REED, user="eric", body=wait_body())],
    [
        review("COMMENTED", REED, rid=1, body=wait_body()),
        review("COMMENTED", REED, rid=2, body=wait_body()),
        review("COMMENTED", REED, rid=3, body=wait_body()),
    ],
    [review("APPROVED", REED, body=wait_body())],
    [review("COMMENTED", REED, body=marker(REED, "\n\n**CHANGES NEEDED**\n"))],
    [review("COMMENTED", REED, body=marker(REED, "\n\nno verdict\n"))],
]


@pytest.mark.parametrize("reviews", _NOT_ONE_WAIT, ids=[
    "none", "stale", "sage", "human", "three", "approved", "changes-needed", "no-wait",
])
def test_retrigger_reed_skips_unless_exactly_one_wait(monkeypatch, reviews):
    rc, world, posts = _run_reed(
        monkeypatch, [make_pr(number=7)], reviews=reviews, checks=green_checks(),
    )
    assert rc == 0 and posts == []
    assert any("/reviews?" in g for g in _gets(world))


@pytest.mark.parametrize("over", [
    {"draft": True},
    {"base": {"ref": "develop"}},
    {"head": {"repo": {"full_name": "other/tai-robot"}}},
    {"head": {"sha": OTHER_SHA}},
    {"state": "closed"},
], ids=["draft", "non-master", "fork", "other-sha", "closed"])
def test_retrigger_reed_skips_unguarded_prs(monkeypatch, over):
    rc, world, posts = _run_reed(
        monkeypatch,
        [make_pr(number=7, **over)],
        reviews=[review("COMMENTED", REED, body=wait_body())],
        checks=green_checks(),
    )
    assert rc == 0 and posts == []
    gets = _gets(world)
    assert any("pulls?state=open" in g for g in gets)
    assert not any("check-runs" in g for g in gets)


@pytest.mark.parametrize("checks", [
    [check_run("review", rid=1)],
    [check_run("test", rid=1)],
    [
        check_run("test", rid=1),
        check_run("review", rid=2, conclusion="success", completed="2026-11-19T04:00:00Z"),
        check_run("review", rid=3, status="in_progress", conclusion=None,
                  completed=None, started="2026-11-19T00:00:00Z"),
    ],
    [
        check_run("review", rid=1),
        check_run("test", rid=2, conclusion="success", completed="2026-11-19T04:00:00Z"),
        check_run("test", rid=3, status="in_progress", conclusion=None,
                  completed=None, started="2026-11-19T00:00:00Z"),
    ],
    [
        check_run("test", rid=1, conclusion="success", completed="2026-11-19T00:00:00Z"),
        check_run("test", rid=2, conclusion="failure", completed="2026-11-19T04:00:00Z"),
        check_run("review", rid=3),
    ],
    [
        check_run("test", rid=1, conclusion="success", completed="2026-11-19T01:00:00Z"),
        check_run("test", rid=2, conclusion="failure", completed="2026-11-19T01:00:00Z"),
        check_run("review", rid=3),
    ],
], ids=["missing-test", "missing-review", "review-in-progress", "test-in-progress",
        "later-failure", "tie-id-failure"])
def test_retrigger_reed_skips_unless_latest_checks_are_green(monkeypatch, checks):
    rc, world, posts = _run_reed(
        monkeypatch, [make_pr(number=7)],
        reviews=[review("COMMENTED", REED, body=wait_body())],
        checks=checks,
    )
    assert rc == 0 and posts == []
    assert any("check-runs" in g for g in _gets(world))
    assert not any("/reviews?" in g for g in _gets(world))


@pytest.mark.parametrize("url,key", [
    (None, None),
    ("", ""),
    (REED_URL, ""),
    ("", REED_KEY),
    ("  ", "  "),
])
def test_retrigger_reed_unset_secrets_skip(monkeypatch, capsys, url, key):
    rc, world, posts = _run_reed(
        monkeypatch, [make_pr(number=7)],
        reviews=[review("COMMENTED", REED, body=wait_body())],
        checks=green_checks(), url=url, key=key,
    )
    text = capsys.readouterr()
    flat = text.out + text.err
    assert rc == 0 and posts == [] and world.calls == []
    assert "skip" in flat.lower()
    assert REED_URL not in flat and REED_KEY not in flat


@pytest.mark.parametrize("repo,sha", [
    ("nope", SHA),
    ("", SHA),
    (REPO, "abc"),
    (REPO, "g" * 40),
    (REPO, "a" * 39),
    (REPO, ""),
])
def test_retrigger_reed_rejects_bad_repo_or_sha(monkeypatch, repo, sha):
    rc, world, posts = _run_reed(
        monkeypatch, [make_pr(number=7)],
        reviews=[review("COMMENTED", REED, body=wait_body())],
        checks=green_checks(), repo=repo, head_sha=sha,
    )
    assert rc == 1 and posts == [] and world.calls == []


@pytest.mark.parametrize("over,rev,expect", [
    ({}, review("COMMENTED", REED, rid=99), "COMMENTED"),
    ({}, review("COMMENTED", SAGE, rid=99), "COMMENTED"),
    ({"draft": True}, review("COMMENTED", REED, rid=99), "COMMENTED"),
    ({"draft": True}, review("APPROVED", REED, rid=99), "APPROVED"),
    ({"base": {"ref": "feature-a"}}, review("APPROVED", SAGE, rid=99), "APPROVED"),
    ({}, review("APPROVED", REED, rid=99), None),
    ({}, review("COMMENTED", REED, rid=99, user="eric"), None),
    ({}, review("COMMENTED", None, rid=99), None),
    ({}, review("COMMENTED", REED, rid=99, sha=OTHER_SHA), None),
    ({}, review("COMMENTED", REED, rid=99, body="hello\n" + marker(REED)), None),
    ({"state": "closed"}, review("COMMENTED", REED, rid=99), None),
    ({"head": {"repo": {"full_name": "other/tai-robot"}}},
     review("COMMENTED", REED, rid=99), None),
], ids=["reed-commented", "sage-commented", "draft-commented", "draft-approved",
        "stacked-approved", "ready-approved", "human", "no-marker", "stale",
        "marker-line-2", "closed", "fork"])
def test_notify_selina_cursor_review(monkeypatch, over, rev, expect):
    pr = make_pr(number=7, **over)
    rc, world, posts = _run_selina(
        monkeypatch, "pull_request_review", pr=pr, review_obj=rev,
    )
    assert rc == 0
    gets = _gets(world)
    assert f"repos/{REPO}/pulls/7" in gets
    head = over.get("head") or {}
    rejected = over.get("state") not in (None, "open") or bool(head.get("repo"))
    if rejected:
        assert not any("/reviews/" in g for g in gets)
    else:
        assert any("/reviews/42" in g for g in gets)
    if expect is None:
        assert posts == []
        return
    assert _bodies(posts) == [{
        "event": "cursor_review",
        "pr": 7,
        "head_sha": SHA,
        "review_id": 42,
        "review_state": expect,
    }]
    _assert_hook(posts, SELINA_KEY)
    assert world.write_calls() == []
    _assert_no_admin(world)


def test_notify_selina_review_skips_bad_head_sha(monkeypatch):
    pr = make_pr(number=7, head={"sha": "abc"})
    rc, _, posts = _run_selina(
        monkeypatch, "pull_request_review", pr=pr,
        review_obj=review("COMMENTED", REED, sha="abc", rid=42),
    )
    assert rc == 0 and posts == []


@pytest.mark.parametrize("pr_number,review_id", [
    ("", "42"), ("0", "42"), ("-1", "42"), ("7", ""), ("7", "0"), ("x", "1"),
])
def test_notify_selina_review_requires_positive_ids(monkeypatch, pr_number, review_id):
    rc, world, posts = _run_selina(
        monkeypatch, "pull_request_review",
        pr=make_pr(number=7),
        review_obj=review("COMMENTED", REED, rid=42),
        pr_number=pr_number, review_id=review_id,
    )
    assert rc == 1 and posts == [] and world.calls == []


@pytest.mark.parametrize("over", [
    {},
    {"draft": True},
    {"draft": True, "base": {"ref": "feature-stack"}},
], ids=["ready", "draft", "stacked-draft"])
def test_notify_selina_tests_failure(monkeypatch, over):
    pr = make_pr(number=7, **over)
    open_prs = [
        make_pr(number=4, head={"sha": OTHER_SHA}),
        pr,
        make_pr(number=8, head={"repo": {"full_name": "other/tai-robot"}}),
    ]
    rc, world, posts = _run_selina(
        monkeypatch, "workflow_run", open_prs=open_prs, pr_number="999",
        head_sha=SHA, workflow_name="Tests", conclusion="failure",
    )
    assert rc == 0
    assert _bodies(posts) == [{
        "event": "ci_failed", "workflow": "Tests", "pr": 7, "head_sha": SHA,
    }]
    assert world.write_calls() == []
    _assert_no_admin(world)


def test_notify_selina_claude_review_failure(monkeypatch):
    open_prs = [
        make_pr(number=2, head={"sha": OTHER_SHA}),
        make_pr(number=3, head={"repo": {"full_name": "other/tai-robot"}}),
        make_pr(number=6, draft=True, labels=[{"name": "needs-owner"}]),
        make_pr(number=7, draft=True),
        make_pr(number=8),
        make_pr(number=9, draft=True, labels=[{"name": "claude-review"}]),
        make_pr(number=10, base={"ref": "feature-stack"}),
    ]
    rc, world, posts = _run_selina(
        monkeypatch, "workflow_run", open_prs=open_prs, pr_number="999",
        head_sha=SHA, workflow_name="Claude Review", conclusion="failure",
    )
    assert rc == 0
    got = sorted(_bodies(posts), key=lambda b: b["pr"])
    assert got == [
        {"event": "ci_failed", "workflow": "Claude Review", "pr": n, "head_sha": SHA}
        for n in (8, 9, 10)
    ]
    assert world.write_calls() == []


def test_notify_selina_claude_passed_draft(monkeypatch):
    rc, world, posts = _run_selina(
        monkeypatch, "workflow_run",
        open_prs=[make_pr(number=7, draft=True)],
        pr_number="999", head_sha=SHA,
        workflow_name="Claude Review", conclusion="success",
    )
    assert rc == 0
    assert _bodies(posts) == [{
        "event": "claude_passed_draft", "pr": 7, "head_sha": SHA,
    }]
    _assert_hook(posts, SELINA_KEY)
    assert world.write_calls() == []


@pytest.mark.parametrize("over,workflow,conclusion", [
    ({}, "Claude Review", "success"),
    ({"draft": True, "base": {"ref": "feature"}}, "Claude Review", "success"),
    ({"draft": True}, "Tests", "success"),
    ({"draft": True}, "Claude Review", "cancelled"),
], ids=["ready", "stacked-draft", "tests-success", "cancelled"])
def test_notify_selina_skips_claude_passed_draft(monkeypatch, over, workflow, conclusion):
    rc, _, posts = _run_selina(
        monkeypatch, "workflow_run",
        open_prs=[make_pr(number=7, **over)],
        head_sha=SHA, workflow_name=workflow, conclusion=conclusion,
    )
    assert rc == 0 and posts == []


def test_notify_selina_workflow_rejects_fork(monkeypatch):
    pr = make_pr(number=7, head={"repo": {"full_name": "other/tai-robot"}})
    rc, world, posts = _run_selina(
        monkeypatch, "workflow_run", open_prs=[pr],
        head_sha=SHA, workflow_name="Tests", conclusion="failure",
    )
    assert rc == 0 and posts == []
    assert any("state=open" in g for g in _gets(world))


def test_notify_selina_base_pr_merged(monkeypatch):
    merged = make_pr(
        number=7, state="closed", merged=True, head={"ref": "  feature-a  "},
    )
    open_prs = [
        make_pr(number=20, base={"ref": "feature-a"}),
        make_pr(number=3, base={"ref": "feature-a"}, draft=True),
        make_pr(number=11, base={"ref": "spoofed-ref"}),
        make_pr(number=4, base={"ref": "feature-a"},
                head={"repo": {"full_name": "fork/tai-robot"}}),
        make_pr(number=15, state="closed", base={"ref": "feature-a"}),
    ]
    rc, world, posts = _run_selina(
        monkeypatch, "pull_request_target", pr=merged, open_prs=open_prs,
        merged_head_ref="spoofed-ref",
    )
    assert rc == 0
    assert _bodies(posts) == [{
        "event": "base_pr_merged",
        "merged_pr": 7,
        "merged_head_ref": "feature-a",
        "stacked_prs": [3, 20],
    }]
    _assert_hook(posts, SELINA_KEY)
    assert world.write_calls() == []
    _assert_no_admin(world)


def test_notify_selina_merged_without_stacked_prs(monkeypatch):
    merged = make_pr(number=7, state="closed", merged=True, head={"ref": "feature-a"})
    rc, world, posts = _run_selina(
        monkeypatch, "pull_request_target", pr=merged,
        open_prs=[make_pr(number=8, base={"ref": "master"})],
    )
    assert rc == 0 and posts == []
    assert any("state=open" in g for g in _gets(world))


@pytest.mark.parametrize("merged", [False, None])
def test_notify_selina_skips_when_not_merged(monkeypatch, merged):
    pr = make_pr(number=7, state="closed", merged=merged, head={"ref": "feature-a"})
    rc, world, posts = _run_selina(
        monkeypatch, "pull_request_target", pr=pr,
        open_prs=[make_pr(number=8, base={"ref": "feature-a"})],
    )
    assert rc == 0 and posts == []
    assert not any("state=open" in g for g in _gets(world))


def test_notify_selina_merged_rejects_fork(monkeypatch):
    pr = make_pr(
        number=7, state="closed", merged=True,
        head={"ref": "feature-a", "repo": {"full_name": "other/tai-robot"}},
    )
    rc, world, posts = _run_selina(
        monkeypatch, "pull_request_target", pr=pr,
        open_prs=[make_pr(number=8, base={"ref": "feature-a"})],
    )
    assert rc == 0 and posts == []
    assert not any("state=open" in g for g in _gets(world))


def test_notify_selina_merged_skips_blank_head_ref(monkeypatch):
    pr = make_pr(number=7, state="closed", merged=True, head={"ref": "  "})
    rc, world, posts = _run_selina(
        monkeypatch, "pull_request_target", pr=pr,
        open_prs=[make_pr(number=8, base={"ref": "feature-a"})],
    )
    assert rc == 0 and posts == []
    assert not any("state=open" in g for g in _gets(world))


@pytest.mark.parametrize("url,key", [
    (None, None), ("", ""), (SELINA_URL, ""), ("", SELINA_KEY), ("  ", "  "),
])
def test_notify_selina_unset_secrets_skip(monkeypatch, capsys, url, key):
    rc, world, posts = _run_selina(
        monkeypatch, "pull_request_review",
        pr=make_pr(number=7),
        review_obj=review("COMMENTED", REED, rid=42),
        url=url, key=key,
    )
    text = capsys.readouterr()
    flat = text.out + text.err
    assert rc == 0 and posts == [] and world.calls == []
    assert "skip" in flat.lower()
    assert SELINA_URL not in flat and SELINA_KEY not in flat


@pytest.mark.parametrize("sha", ["", "abc", "g" * 40])
def test_notify_selina_workflow_rejects_bad_sha(monkeypatch, sha):
    rc, world, posts = _run_selina(
        monkeypatch, "workflow_run",
        open_prs=[make_pr(number=7)],
        head_sha=sha, workflow_name="Tests", conclusion="failure",
    )
    assert rc == 1 and posts == [] and world.calls == []


def test_notify_selina_rejects_bad_repo(monkeypatch):
    rc, world, posts = _run_selina(
        monkeypatch, "pull_request_review", repo="nope",
        pr=make_pr(number=7), review_obj=review("COMMENTED", REED),
    )
    assert rc == 1 and posts == [] and world.calls == []


def test_notify_selina_merged_requires_pr(monkeypatch):
    rc, world, posts = _run_selina(
        monkeypatch, "pull_request_target", pr_number="0",
        pr=make_pr(number=7, state="closed", merged=True, head={"ref": "feature-a"}),
    )
    assert rc == 1 and posts == [] and world.calls == []


def test_notify_selina_unknown_event_skips(monkeypatch):
    rc, world, posts = _run_selina(monkeypatch, "push")
    assert rc == 0 and posts == [] and world.calls == []


@pytest.mark.parametrize("key,expected", [
    ("raw-secret", "Bearer raw-secret"),
    ("Bearer x", "Bearer x"),
    ("bearer x", "bearer x"),
    ("  Bearer x  ", "Bearer x"),
])
def test_post_webhook_bearer_normalization(monkeypatch, key, expected):
    seen = {}

    def opener(req, timeout=30):
        seen["auth"] = req.get_header("Authorization")
        seen["timeout"] = timeout
        seen["type"] = req.get_header("Content-type")
        return _HookResp()

    monkeypatch.setattr(mod.urllib.request, "urlopen", opener)
    rc = mod._post_webhook("reed", "  " + REED_URL + "  ", key, {"pr": 7, "head_sha": SHA})
    assert rc == 0
    assert seen["auth"] == expected
    assert "Bearer Bearer" not in seen["auth"]
    assert seen["timeout"] == 30
    assert seen["type"] == "application/json"


def test_post_webhook_unset_skip_or_fail(monkeypatch, capsys):
    def boom(*_a, **_k):
        raise AssertionError("urlopen should not be called")

    monkeypatch.setattr(mod.urllib.request, "urlopen", boom)
    assert mod._post_webhook("reed", "", "", {"pr": 1}, skip_if_unset=True) == 0
    out, err = capsys.readouterr()
    assert "skip" in out.lower() and err == ""
    assert mod._post_webhook("sage", "", "key", {"pr": 1}, skip_if_unset=False) == 1
    out, err = capsys.readouterr()
    assert err.strip() == "sage webhook is not configured"
    assert "key" not in err


@pytest.mark.parametrize("which,kind", [
    ("reed", "http"), ("reed", "url"), ("selina", "http"), ("selina", "url"),
])
def test_reed_and_selina_webhook_errors_do_not_leak(monkeypatch, capsys, which, kind):
    url = REED_URL if which == "reed" else SELINA_URL
    key = REED_KEY if which == "reed" else SELINA_KEY

    def boom(req, timeout=30):
        if kind == "http":
            raise urllib.error.HTTPError(url, 503, "nope " + key, hdrs=None, fp=None)

        class SecretReason(OSError):
            def __str__(self):
                return url + " " + key

        raise urllib.error.URLError(SecretReason())

    try:
        if which == "reed":
            rc, _world, _posts = _run_reed(
                monkeypatch, [make_pr(number=7)],
                reviews=[review("COMMENTED", REED, body=wait_body())],
                checks=green_checks(), opener=boom, url=url, key=key,
            )
        else:
            rc, _world, _posts = _run_selina(
                monkeypatch, "pull_request_review",
                pr=make_pr(number=7),
                review_obj=review("COMMENTED", REED, rid=42),
                opener=boom, url=url, key=key,
            )
    except Exception as exc:
        assert url not in str(exc) and key not in str(exc)
        raise
    text = capsys.readouterr()
    flat = text.out + text.err
    assert rc == 1
    assert f"{which} webhook failed:" in flat
    if kind == "http":
        assert "503" in flat
    else:
        assert "SecretReason" in flat
    assert url not in flat and key not in flat


def test_usage_includes_read_only_commands(capsys):
    assert mod.main(["auto_merge_gate.py"]) == 2
    err = capsys.readouterr().err
    assert "trigger-sage" in err and "gate" in err
    assert "retrigger-reed" in err and "notify-selina" in err


# ---- workflow --------------------------------------------------------------

def _workflow_on(doc):
    if "on" in doc:
        return doc["on"]
    return doc[True]


def test_workflow_yaml_is_least_privilege():
    text = (ROOT / ".github/workflows/auto-merge.yml").read_text(encoding="utf-8")
    py = (ROOT / "scripts/ci/auto_merge_gate.py").read_text(encoding="utf-8")
    assert "--admin" not in text and "sage-approved" not in text
    assert "--admin" not in py and "sage-approved" not in py
    assert "pull_request.head.sha" not in text
    doc = yaml.safe_load(text)
    assert doc["name"] == "Auto-merge gate"
    jobs = doc["jobs"]
    assert set(jobs) == {
        "trigger-sage", "eval-approval-gate", "retrigger-reed", "notify-selina",
    }
    trig, gate = jobs["trigger-sage"], jobs["eval-approval-gate"]
    reed, selina = jobs["retrigger-reed"], jobs["notify-selina"]
    assert trig["permissions"] == {"contents": "read", "pull-requests": "read"}
    assert gate["permissions"] == {
        "contents": "write",
        "pull-requests": "write",
        "statuses": "write",
    }
    read_checks = {"contents": "read", "pull-requests": "read", "checks": "read"}
    assert reed["permissions"] == read_checks
    assert selina["permissions"] == read_checks
    for job in (trig, reed, selina):
        assert "write" not in job["permissions"].values()
    for job in jobs.values():
        assert job["runs-on"] == "ubuntu-latest"
        assert job["timeout-minutes"] == 5
        assert job["concurrency"]["cancel-in-progress"] is False
        assert len(job["steps"]) == 2
        checkout = next(
            s for s in job["steps"] if str(s.get("uses", "")).startswith("actions/checkout@")
        )
        assert checkout["uses"] == "actions/checkout@v4"
        assert checkout["with"]["ref"] == "master"
        assert checkout["with"]["sparse-checkout"] == "scripts/ci"
        assert checkout["with"]["persist-credentials"] is False
        runs = [s for s in job["steps"] if "run" in s]
        assert len(runs) == 1
        assert "${{" not in (runs[0].get("run") or "")
        assert "pull_request.head.sha" not in yaml.dump(job)
    assert trig["concurrency"]["group"].startswith("trigger-sage-")
    assert gate["concurrency"]["group"].startswith("auto-merge-")
    assert reed["concurrency"]["group"] == (
        "retrigger-reed-${{ github.event.workflow_run.head_sha || github.event.pull_request.number }}"
    )
    assert selina["concurrency"]["group"] == (
        "notify-selina-${{ github.event_name }}-"
        "${{ github.event.review.id || github.event.pull_request.number || github.event.workflow_run.head_sha }}"
    )
    for job in (trig, gate):
        cond = job["if"]
        assert "github.event.pull_request.head.repo.full_name == github.repository" in cond
        assert "github.event.pull_request.base.ref == 'master'" in cond
        assert "github.event.pull_request.state == 'open'" in cond
    assert "github.event_name == 'pull_request_target'" in trig["if"]
    assert "ready_for_review" in trig["if"]
    assert "!github.event.pull_request.draft" in trig["if"]
    assert "closed" not in trig["if"]
    assert "needs-owner" in gate["if"]
    assert "cursor[bot]" in gate["if"]
    assert "github.event.pull_request.state == 'open'" in gate["if"]
    reed_if = reed["if"]
    assert "github.event_name == 'workflow_run'" in reed_if
    assert "github.event.workflow_run.conclusion == 'success'" in reed_if
    assert "github.event.workflow_run.event == 'pull_request'" in reed_if
    assert "github.event.workflow_run.head_repository.full_name == github.repository" in reed_if
    assert "github.event_name == 'pull_request_review'" in reed_if
    assert "github.event.action == 'submitted'" in reed_if
    assert "github.event.review.user.login == 'cursor[bot]'" in reed_if
    assert "github.event.pull_request.state == 'open'" in reed_if
    assert "github.event.pull_request.head.repo.full_name == github.repository" in reed_if
    sel_if = selina["if"]
    assert "github.event_name == 'pull_request_review'" in sel_if
    assert "github.event.action == 'submitted'" in sel_if
    assert "github.event.review.user.login == 'cursor[bot]'" in sel_if
    assert "github.event.pull_request.state == 'open'" in sel_if
    assert "github.event.pull_request.head.repo.full_name == github.repository" in sel_if
    assert "github.event.workflow_run.event == 'pull_request'" in sel_if
    assert "github.event.workflow_run.head_repository.full_name == github.repository" in sel_if
    assert "github.event.workflow_run.conclusion == 'failure'" in sel_if
    assert "github.event.workflow_run.conclusion == 'success'" in sel_if
    assert "github.event.workflow_run.name == 'Claude Review'" in sel_if
    assert "github.event_name == 'pull_request_target'" in sel_if
    assert "github.event.action == 'closed'" in sel_if
    assert "github.event.pull_request.merged == true" in sel_if
    assert "draft" not in sel_if
    assert "base.ref" not in sel_if
    trig_run = next(s for s in trig["steps"] if "run" in s)
    gate_run = next(s for s in gate["steps"] if "run" in s)
    reed_run = next(s for s in reed["steps"] if "run" in s)
    sel_run = next(s for s in selina["steps"] if "run" in s)
    assert trig_run["run"].strip() == "python3 scripts/ci/auto_merge_gate.py trigger-sage"
    assert gate_run["run"].strip() == "python3 scripts/ci/auto_merge_gate.py gate"
    assert reed_run["run"].strip() == "python3 scripts/ci/auto_merge_gate.py retrigger-reed"
    assert sel_run["run"].strip() == "python3 scripts/ci/auto_merge_gate.py notify-selina"
    assert set(reed_run["env"]) == {
        "GH_TOKEN", "GITHUB_REPOSITORY", "EVENT_NAME", "PR", "HEAD_SHA",
        "WORKFLOW_NAME", "REED_QA_WEBHOOK_URL", "REED_QA_WEBHOOK_KEY",
    }
    assert set(sel_run["env"]) == {
        "GH_TOKEN", "GITHUB_REPOSITORY", "EVENT_NAME", "PR", "REVIEW_ID",
        "REVIEW_STATE", "HEAD_SHA", "WORKFLOW_NAME", "WORKFLOW_CONCLUSION",
        "MERGED_HEAD_REF", "SELINA_QA_WEBHOOK_URL", "SELINA_QA_WEBHOOK_KEY",
    }
    assert reed_run["env"]["GH_TOKEN"] == "${{ github.token }}"
    assert reed_run["env"]["GITHUB_REPOSITORY"] == "${{ github.repository }}"
    assert reed_run["env"]["EVENT_NAME"] == "${{ github.event_name }}"
    assert reed_run["env"]["PR"] == "${{ github.event.pull_request.number }}"
    assert reed_run["env"]["HEAD_SHA"] == "${{ github.event.workflow_run.head_sha }}"
    assert reed_run["env"]["WORKFLOW_NAME"] == "${{ github.event.workflow_run.name }}"
    assert sel_run["env"]["GH_TOKEN"] == "${{ github.token }}"
    assert sel_run["env"]["GITHUB_REPOSITORY"] == "${{ github.repository }}"
    assert sel_run["env"]["HEAD_SHA"] == "${{ github.event.workflow_run.head_sha }}"
    assert sel_run["env"]["EVENT_NAME"] == "${{ github.event_name }}"
    assert sel_run["env"]["PR"] == "${{ github.event.pull_request.number }}"
    assert sel_run["env"]["REVIEW_ID"] == "${{ github.event.review.id }}"
    assert sel_run["env"]["REVIEW_STATE"] == "${{ github.event.review.state }}"
    assert sel_run["env"]["WORKFLOW_NAME"] == "${{ github.event.workflow_run.name }}"
    assert sel_run["env"]["WORKFLOW_CONCLUSION"] == "${{ github.event.workflow_run.conclusion }}"
    assert sel_run["env"]["MERGED_HEAD_REF"] == "${{ github.event.pull_request.head.ref }}"
    assert "secrets.SAGE_REVIEW_WEBHOOK_URL" in trig_run["env"]["SAGE_REVIEW_WEBHOOK_URL"]
    assert "secrets.SAGE_REVIEW_WEBHOOK_KEY" in trig_run["env"]["SAGE_REVIEW_WEBHOOK_KEY"]
    assert "secrets.REED_QA_WEBHOOK_URL" in reed_run["env"]["REED_QA_WEBHOOK_URL"]
    assert "secrets.REED_QA_WEBHOOK_KEY" in reed_run["env"]["REED_QA_WEBHOOK_KEY"]
    assert "secrets.SELINA_QA_WEBHOOK_URL" in sel_run["env"]["SELINA_QA_WEBHOOK_URL"]
    assert "secrets.SELINA_QA_WEBHOOK_KEY" in sel_run["env"]["SELINA_QA_WEBHOOK_KEY"]
    trig_dump, gate_dump = yaml.dump(trig), yaml.dump(gate)
    reed_dump, sel_dump = yaml.dump(reed), yaml.dump(selina)
    assert "SAGE_REVIEW_WEBHOOK" in trig_dump
    assert "REED_QA" not in trig_dump and "SELINA_QA" not in trig_dump
    assert "REED_QA_WEBHOOK" in reed_dump
    assert "SAGE_REVIEW" not in reed_dump and "SELINA_QA" not in reed_dump
    assert "SELINA_QA_WEBHOOK" in sel_dump
    assert "SAGE_REVIEW" not in sel_dump and "REED_QA" not in sel_dump
    assert "WEBHOOK" not in gate_dump
    on = _workflow_on(doc)
    assert on["pull_request_target"]["types"] == [
        "opened", "synchronize", "reopened", "ready_for_review",
        "converted_to_draft", "labeled", "unlabeled", "closed",
    ]
    assert on["pull_request_review"]["types"] == ["submitted", "dismissed"]
    assert on["workflow_run"]["workflows"] == ["Tests", "Claude Review"]
    assert on["workflow_run"]["types"] == ["completed"]
    assert "pull_request_target:" in text
    assert "pull_request_review:" in text
    assert "workflow_run:" in text
    for token in ("opened", "synchronize", "reopened", "ready_for_review",
                  "converted_to_draft", "labeled", "unlabeled", "closed",
                  "submitted", "dismissed"):
        assert token in text


def test_workflow_job_names_do_not_collide_with_status_context():
    """The Actions check name must not be the commit-status context."""
    text = (ROOT / ".github/workflows/auto-merge.yml").read_text(encoding="utf-8")
    doc = yaml.safe_load(text)
    assert set(doc["jobs"]) == {
        "trigger-sage", "eval-approval-gate", "retrigger-reed", "notify-selina",
    }
    for job_key, job_cfg in doc["jobs"].items():
        assert job_key != mod.STATUS_CONTEXT
        assert job_key != "approval-gate"
        assert (job_cfg or {}).get("name") != mod.STATUS_CONTEXT
        assert (job_cfg or {}).get("name") != "approval-gate"
    assert "pull_request.head.sha" not in text
