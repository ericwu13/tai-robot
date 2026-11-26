"""Unit tests for Gemini PR Review helper and workflow security invariants."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from unittest import mock

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / ".github" / "scripts" / "gemini_review_post.py"
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "gemini-review.yml"


def _load_mod():
    spec = importlib.util.spec_from_file_location("gemini_review_post", SCRIPT_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mod = _load_mod()


def _output_refs(text: str, prefix: str) -> set[str]:
    found = set()
    start = 0
    while True:
        i = text.find(prefix, start)
        if i < 0:
            return found
        j = i + len(prefix)
        k = j
        while k < len(text) and (text[k].isalnum() or text[k] == "_"):
            k += 1
        found.add(text[j:k])
        start = k


class TestNormalizationAndConsistency:
    def test_pass_with_zero_findings(self):
        out = mod.normalize_review_result({
            "verdict": "PASS", "p1": 0, "p2": 0, "p3": 1,
            "summary": "## Gemini review @ abc1234\nLooks good.",
            "inline_comments": [], "still_open_threads": [],
        })
        assert out["verdict"] == "PASS" and out["p3"] == 1

    @pytest.mark.parametrize("override", [
        {"p1": 1},
        {"p2": 1},
        {"inline_comments": [{"path": "a.py", "line": 10, "body": "**P2:** bug"}]},
        {"still_open_threads": ["PRRT_123"]},
    ])
    def test_contradictory_pass_is_forced_to_block(self, override):
        raw = {
            "verdict": "PASS", "p1": 0, "p2": 0, "p3": 0,
            "summary": "## Gemini review\nBody",
            "inline_comments": [], "still_open_threads": [],
            **override,
        }
        out = mod.normalize_review_result(raw)
        assert out["verdict"] == "BLOCK"

    def test_invalid_verdict_or_empty_summary_fails_closed(self):
        with pytest.raises(ValueError):
            mod.normalize_review_result({"verdict": "MAYBE", "summary": "x"})
        with pytest.raises(ValueError):
            mod.normalize_review_result({"verdict": "PASS", "summary": ""})


class TestNativeReviewSubmission:
    def test_app_pass_submits_approve(self):
        calls = []

        def fake_api(path, *, method="GET", payload=None):
            calls.append((method, path, payload))
            return True, {}

        with mock.patch.object(mod, "gh_api", side_effect=fake_api):
            failed = mod.submit_review("o/r", 50, "deadbeef1234", "PASS", [], app_slug="tai-robot-gemini")

        assert failed == []
        assert calls[0][1] == "repos/o/r/pulls/50/reviews"
        assert calls[0][2]["event"] == "APPROVE"
        assert calls[0][2]["commit_id"] == "deadbeef1234"

    def test_app_block_submits_comment_and_tags_inline_threads(self):
        calls = []

        def fake_api(path, *, method="GET", payload=None):
            calls.append(payload)
            return True, {}

        comments = [{"path": "src/a.py", "line": 42, "body": "**P1:** race"}]
        with mock.patch.object(mod, "gh_api", side_effect=fake_api):
            failed = mod.submit_review("o/r", 50, "deadbeef1234", "BLOCK", comments, app_slug="tai-robot-gemini")

        assert failed == []
        assert calls[0]["event"] == "COMMENT"
        assert "BLOCK" in calls[0]["body"]
        assert mod.THREAD_MARKER in calls[0]["comments"][0]["body"]

    @pytest.mark.parametrize("verdict", ["BLOCK", "FAIL", "CHANGES", ""])
    def test_non_pass_never_sends_request_changes(self, verdict):
        calls = []

        def fake_api(path, *, method="GET", payload=None):
            calls.append(payload)
            if len(calls) == 1 and payload.get("comments"):
                return False, "422 Unprocessable Entity"
            return True, {}

        comments = [{"path": "src/a.py", "line": 42, "body": "**P2:** race"}]
        with mock.patch.object(mod, "gh_api", side_effect=fake_api):
            mod.submit_review("o/r", 50, "deadbeef1234", verdict, comments, app_slug="tai-robot-gemini")

        assert calls
        assert all(c.get("event") != "REQUEST_CHANGES" for c in calls)
        assert all(c.get("event") == "COMMENT" for c in calls)
        assert "BLOCK" in calls[-1]["body"]

    def test_app_block_422_retries_inline_and_still_comments(self):
        calls = []

        def fake_api(path, *, method="GET", payload=None):
            calls.append(payload)
            if len(payload.get("comments") or []) > 1:
                return False, "422 Unprocessable Entity"
            if payload.get("comments") and payload["comments"][0]["line"] == 999:
                return False, "422 line not in diff"
            return True, {}

        comments = [
            {"path": "src/a.py", "line": 10, "body": "**P2:** ok"},
            {"path": "src/a.py", "line": 999, "body": "**P2:** bad line"},
        ]
        with mock.patch.object(mod, "gh_api", side_effect=fake_api):
            failed = mod.submit_review("o/r", 50, "deadbeef", "BLOCK", comments, app_slug="tai-robot-gemini")

        assert len(failed) == 1 and failed[0]["line"] == 999
        assert calls[-1]["event"] == "COMMENT"
        assert "REQUEST_CHANGES" not in {c.get("event") for c in calls}

    def test_without_app_slug_falls_back_to_comment_only(self):
        calls = []

        def fake_api(path, *, method="GET", payload=None):
            calls.append(payload)
            return True, {}

        with mock.patch.object(mod, "gh_api", side_effect=fake_api):
            assert mod.submit_review("o/r", 50, "deadbeef", "PASS", [], app_slug="") == []
            assert calls == []
            assert mod.submit_review(
                "o/r", 50, "deadbeef", "BLOCK",
                [{"path": "src/a.py", "line": 10, "body": "**P2:** bug"}],
                app_slug="",
            ) == []
            assert calls[0]["event"] == "COMMENT"


class TestThreadIsolationAndApply:
    def test_gemini_never_claims_claude_github_actions_threads(self):
        assert mod._is_gemini_thread("github-actions[bot]", "**P2:** claude finding", app_slug="") is False
        assert mod._is_gemini_thread("github-actions[bot]", f"**P2:** bug\n{mod.THREAD_MARKER}", app_slug="") is True
        assert mod._is_gemini_thread("tai-robot-gemini[bot]", "**P2:** bug", app_slug="tai-robot-gemini") is True
        assert mod._is_gemini_thread("tai-robot-claude[bot]", "**P2:** bug", app_slug="tai-robot-gemini") is False
        # Tokenless review job (app_slug="") still sees App-bot threads with marker, not human spoofs
        assert mod._is_gemini_thread("tai-robot-gemini[bot]", f"**P2:** bug\n{mod.THREAD_MARKER}", app_slug="") is True
        assert mod._is_gemini_thread("random-human", f"**P2:** bug\n{mod.THREAD_MARKER}", app_slug="") is False
        # Bare slug is not a REST author, and a GraphQL User with that login is not a bot.
        assert "tai-robot-gemini" not in mod.bot_logins("tai-robot-gemini")
        assert mod.bot_logins("tai-robot-gemini") == frozenset({
            "github-actions[bot]", "tai-robot-gemini[bot]",
        })
        assert mod._is_gemini_thread(
            "tai-robot-gemini", "**P2:** bug", app_slug="tai-robot-gemini",
        ) is False
        assert mod._is_gemini_thread(
            "tai-robot-gemini", "**P2:** bug", app_slug="tai-robot-gemini", typename="User",
        ) is False
        assert mod._is_gemini_thread(
            "tai-robot-gemini", "**P2:** own", app_slug="tai-robot-gemini", typename="Bot",
        ) is True
        assert mod._is_gemini_thread(
            "github-actions", f"**P2:** old\n{mod.THREAD_MARKER}",
            app_slug="tai-robot-gemini", typename="Bot",
        ) is True
        assert mod._is_gemini_thread(
            "github-actions", f"**P2:** old\n{mod.THREAD_MARKER}",
            app_slug="tai-robot-gemini", typename="User",
        ) is False

    def test_tokenless_reuse_sees_app_bot_summary_and_ignores_human_spoof(self):
        marker = '<!-- gemini-review-state: {"diff": "aabbcc", "verdict": "PASS", "head": "1234567"} -->'
        fake_comments = [
            {"id": 1, "user": {"login": "random-human"}, "body": f"## Gemini review\n{marker}"},
            {"id": 2, "user": {"login": "tai-robot-gemini[bot]"}, "body": f"## Gemini review\n{marker}"},
            {"id": 3, "user": {"login": "tai-robot-gemini"}, "body": f"## Gemini review\n{marker}"},
        ]
        with mock.patch.object(mod, "gh_api", return_value=(True, fake_comments)):
            cid, _body, author = mod.latest_summary_with_author("o/r", 50, app_slug="tai-robot-gemini")
        assert cid == 2 and author == "tai-robot-gemini[bot]"
        only_bare = [fake_comments[2]]
        with mock.patch.object(mod, "gh_api", return_value=(True, only_bare)):
            cid, _body, author = mod.latest_summary_with_author("o/r", 50, app_slug="tai-robot-gemini")
        assert cid is None and author == ""

    def test_graphql_user_slug_is_not_a_thread_and_actions_bot_is(self):
        assert mod.THREADS_QUERY.count("__typename") >= 2
        marked = f"**P2:** old\n{mod.THREAD_MARKER}"

        def node(tid, login, typename, body):
            return {
                "id": tid, "isResolved": False, "path": "a.py", "line": 1,
                "comments": {"nodes": [{
                    "author": {"login": login, "__typename": typename},
                    "body": body,
                }]},
                "replies": {"nodes": [{
                    "author": {"login": "tai-robot-gemini", "__typename": "User"},
                    "body": "human reply",
                }]},
            }

        payload = {"repository": {"pullRequest": {"reviewThreads": {
            "pageInfo": {"hasNextPage": False, "endCursor": None},
            "nodes": [
                node("USER", "tai-robot-gemini", "User", "**P2:** spoof"),
                node("ACTIONS", "github-actions", "Bot", marked),
                node("OWN", "tai-robot-gemini", "Bot", "**P2:** own"),
            ],
        }}}}
        with mock.patch.object(mod, "graphql", return_value=payload):
            gemini, other = mod.list_review_threads("o/r", 50, app_slug="tai-robot-gemini")
        ids = {t["id"] for t in gemini}
        assert ids == {"ACTIONS", "OWN"}
        assert {t["author"] for t in other} == {"tai-robot-gemini"}
        actions = next(t for t in gemini if t["id"] == "ACTIONS")
        assert actions["replies"] == [{"author": "tai-robot-gemini", "body": "human reply"}]

    def test_apply_review_queues_threads_and_approves(self, tmp_path, monkeypatch):
        res_file = tmp_path / "result.txt"
        ctx_file = tmp_path / "context.json"
        ctx_file.write_text(
            json.dumps({"open_gemini_threads": [{"id": "PRRT_FIXED"}]}),
            encoding="utf-8",
        )
        res_file.write_text(
            "```json\n" + json.dumps({
                "verdict": "PASS", "p1": 0, "p2": 0, "p3": 0,
                "summary": "## Gemini review @ abc1234\nAll P2s fixed.",
                "inline_comments": [], "still_open_threads": [],
            }) + "\n```",
            encoding="utf-8",
        )

        gql_calls = []
        rest_calls = []

        def fake_gql(q, v):
            gql_calls.append((q, v))
            return {}

        def fake_api(path, *, method="GET", payload=None):
            rest_calls.append((method, path, payload))
            if path.startswith("repos/o/r/issues/50/comments") and method == "GET":
                return True, []
            return True, {}

        out_file = tmp_path / "github_output.txt"
        monkeypatch.setenv("GITHUB_OUTPUT", str(out_file))
        with mock.patch.object(mod, "list_review_threads", return_value=([{"id": "PRRT_FIXED"}], [])), \
             mock.patch.object(mod, "graphql", side_effect=fake_gql), \
             mock.patch.object(mod, "gh_api", side_effect=fake_api):
            out = mod.apply_review(
                "o/r", 50, "abc1234deadbeef", res_file, ctx_file,
                diff_h="11223344", model_name="gemini-4-hydrogen", app_slug="tai-robot-gemini",
            )

        assert out["verdict"] == "PASS" and out["action"] == "created" and out["failed_inline"] == 0
        assert out["resolve_ids"] == ["PRRT_FIXED"]
        assert not any("resolveReviewThread" in q for q, _v in gql_calls)
        assert "resolve_ids=" in out_file.read_text(encoding="utf-8")
        reviews = [p for m, u, p in rest_calls if m == "POST" and u == "repos/o/r/pulls/50/reviews"]
        assert len(reviews) == 1 and reviews[0]["event"] == "APPROVE"
        comments = [p for m, u, p in rest_calls if m == "POST" and u == "repos/o/r/issues/50/comments"]
        assert len(comments) == 1 and "<!-- gemini-review-state:" in comments[0]["body"]
        assert "gemini-4-hydrogen" in comments[0]["body"]

    def test_resolve_failure_exits_nonzero_and_does_not_approve(self, capsys, monkeypatch):
        monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
        monkeypatch.setenv("PR", "50")
        monkeypatch.setenv("HEAD_SHA", "abc1234deadbeef")
        monkeypatch.setenv("IDS", '["PRRT_FIXED"]')
        monkeypatch.setenv("APP_SLUG", "tai-robot-gemini")

        def fake_gql(q, v):
            if "resolveReviewThread" in q:
                raise RuntimeError("Resource not accessible by integration")
            return {}

        with mock.patch.object(mod, "list_review_threads", return_value=([{"id": "PRRT_FIXED"}], [])), \
             mock.patch.object(mod, "graphql", side_effect=fake_gql), \
             mock.patch.object(mod, "submit_review") as submit:
            rc = mod.main(["resolve"])

        assert rc != 0
        assert "::error::" in capsys.readouterr().out
        assert submit.call_count == 0


class TestWorkflowSecurityInvariants:
    def test_workflow_privilege_separation_pinned_sha_and_readonly_tools(self):
        wf = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
        review_job = wf["jobs"]["gemini-review"]
        post_job = wf["jobs"]["gemini-post"]
        resolve_job = wf["jobs"]["gemini-resolve-threads"]
        assert set(wf["jobs"]) == {"gemini-review", "gemini-post", "gemini-resolve-threads"}
        assert post_job["needs"] == "gemini-review"
        assert resolve_job["needs"] == "gemini-post"
        assert resolve_job["permissions"] == {"contents": "write", "pull-requests": "write"}
        assert post_job["permissions"]["contents"] == "read"

        assert review_job["permissions"] == {"contents": "read", "pull-requests": "read"}
        assert post_job["permissions"] == {
            "contents": "read", "issues": "write", "pull-requests": "write",
        }

        gate_step = next(s for s in review_job["steps"] if s.get("id") == "gate")
        assert "gemini-4-hydrogen" in gate_step["run"]
        assert "gemini-2.5-pro" not in gate_step["run"]

        gemini_steps = [
            s for s in review_job["steps"]
            if str(s.get("uses", "")).startswith("google-github-actions/run-gemini-cli@")
        ]
        assert len(gemini_steps) == 1
        step = gemini_steps[0]
        assert step["with"]["gemini_model"] == "${{ steps.gate.outputs.model }}"
        ref = step["uses"].split("@", 1)[1]
        assert len(ref) == 40 and all(c in "0123456789abcdef" for c in ref)
        assert step["env"]["GITHUB_TOKEN"] == ""
        assert step["with"]["gemini_cli_version"] == "0.71.1"
        settings = json.loads(step["with"]["settings"])
        core_tools = set(settings["tools"]["core"])
        assert core_tools <= {"read_file", "read_many_files", "list_directory", "glob", "grep_search"}
        assert not (core_tools & {"run_shell_command", "write_file", "replace", "web_fetch", "google_web_search"})

        # App token minting exists ONLY in post job (never in review job)
        assert not any(
            str(s.get("uses", "")).startswith("actions/create-github-app-token@")
            for s in review_job["steps"]
        )
        app_steps = [
            s for s in post_job["steps"]
            if str(s.get("uses", "")).startswith("actions/create-github-app-token@")
        ]
        assert len(app_steps) == 1
        assert app_steps[0]["with"]["permission-pull-requests"] == "write"
        assert app_steps[0]["with"]["permission-contents"] == "read"

        raw = WORKFLOW_PATH.read_text(encoding="utf-8")
        assert "needs.review" not in raw
        referenced = _output_refs(yaml.dump(post_job), "needs.gemini-review.outputs.")
        assert referenced == set(review_job["outputs"])
        uploads = [
            s for s in review_job["steps"]
            if str(s.get("uses", "")).startswith("actions/upload-artifact@")
        ]
        downloads = [
            s for s in post_job["steps"]
            if str(s.get("uses", "")).startswith("actions/download-artifact@")
        ]
        assert len(uploads) == 1 and len(downloads) == 1
        assert uploads[0]["with"]["name"] == downloads[0]["with"]["name"]
        assert uploads[0]["with"]["path"] == downloads[0]["with"]["path"]

        assert post_job["outputs"]["app_slug"] == "${{ steps.app-token.outputs.app-slug }}"
        assert post_job["outputs"]["resolve_ids"] == "${{ steps.apply.outputs.resolve_ids }}"
        apply_step = next(s for s in post_job["steps"] if s.get("id") == "apply")
        assert "gemini_review_post.py apply" in str(apply_step.get("run"))
        post_runs = "\n".join(str(s.get("run") or "") for s in post_job["steps"])
        resolve_runs = "\n".join(str(s.get("run") or "") for s in resolve_job["steps"])
        assert 'gemini_review_post.py" resolve' not in post_runs
        assert 'gemini_review_post.py" resolve' in resolve_runs
        resolve_env = resolve_job["steps"][0]["env"]
        assert resolve_env["GH_TOKEN"] == "${{ github.token }}"
        assert resolve_env["APP_SLUG"] == "${{ needs.gemini-post.outputs.app_slug }}"
        assert resolve_env["IDS"] == "${{ needs.gemini-post.outputs.resolve_ids }}"
        resolve_dump = yaml.dump(resolve_job)
        assert "GEMINI_API_KEY" not in resolve_dump
        assert "secrets." not in resolve_dump
        assert "create-github-app-token" not in resolve_dump
        assert not any(
            str(s.get("uses", "")).startswith("actions/create-github-app-token@")
            for s in resolve_job["steps"]
        )

    def test_only_claude_review_and_test_own_required_check_names(self):
        """Required master contexts are `review` and `test`.

        A job's check run is the job `name:` when set, otherwise the job
        key. Only claude-review.yml may equal `review` (it is the producer),
        and only test.yml may equal `test`. An advisory workflow whose job
        key is `review` would satisfy the required check on a no-op run.
        """
        review_owners: set[str] = set()
        test_owners: set[str] = set()
        workflows = sorted((REPO_ROOT / ".github" / "workflows").glob("*.yml"))
        assert workflows, "expected workflow files"
        for path in workflows:
            doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            for key, cfg in (doc.get("jobs") or {}).items():
                labels = {str(key)}
                if isinstance(cfg, dict) and isinstance(cfg.get("name"), str):
                    labels.add(cfg["name"].strip())
                if "review" in labels:
                    review_owners.add(path.name)
                if "test" in labels:
                    test_owners.add(path.name)
        assert review_owners == {"claude-review.yml"}
        assert test_owners == {"test.yml"}
