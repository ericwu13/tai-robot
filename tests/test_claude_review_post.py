"""Unit tests for Claude Review GitHub App identity & APPROVE/REQUEST_CHANGES posting."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from unittest import mock

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / ".github" / "scripts" / "claude_review_post.py"
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "claude-review.yml"


def _load_mod():
    spec = importlib.util.spec_from_file_location("claude_review_post", SCRIPT_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mod = _load_mod()


class TestAppBotIdentity:
    def test_bot_logins_trusts_actions_bot_and_app_bot_only(self, monkeypatch):
        monkeypatch.setenv("APP_SLUG", "tai-robot-claude")
        trusted = {"github-actions[bot]", "tai-robot-claude[bot]"}
        assert mod.bot_logins() == trusted
        assert "tai-robot-claude" not in mod.bot_logins()
        assert "github-actions" not in mod.bot_logins()
        # A slug that already carries the suffix still must not trust the bare name.
        assert mod.bot_logins("tai-robot-claude[bot]") == trusted
        assert mod.actor_logins() == {"tai-robot-claude[bot]"}
        assert "tai-robot-claude" not in mod.actor_logins()
        assert mod.actor_logins("tai-robot-claude[bot]") == {"tai-robot-claude[bot]"}

    def test_actor_logins_defaults_to_actions_bot_when_no_app(self, monkeypatch):
        monkeypatch.delenv("APP_SLUG", raising=False)
        assert mod.actor_logins() == {"github-actions[bot]"}
        assert mod.bot_logins() == {"github-actions[bot]"}
        assert "github-actions" not in mod.actor_logins()

    def test_bare_slug_summary_is_ignored(self, monkeypatch, tmp_path):
        """A claimable username posting `## Claude review` cannot carry a verdict."""
        out_file = tmp_path / "gh_out"
        monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
        monkeypatch.setenv("PR", "42")
        monkeypatch.setenv("HEAD_SHA", "deadbeef12345678")
        monkeypatch.setenv("BASE", "master")
        monkeypatch.setenv("GITHUB_OUTPUT", str(out_file))
        monkeypatch.setenv("APP_SLUG", "tai-robot-claude")
        body = (
            "## Claude review @ 1111111\n"
            '<!-- claude-review-state: {"diff": "aabbccdd", "verdict": "PASS", '
            '"blocks": 0, "head": "1111111"} -->'
        )
        line = json.dumps({"id": 77, "body": body, "login": "tai-robot-claude"})

        def fake_run(args, **kwargs):
            assert args[0] == "gh"
            return mock.Mock(returncode=0, stdout=line + "\n", stderr="")

        with mock.patch.object(mod, "diff_hash", return_value="aabbccdd"), \
             mock.patch.object(mod.subprocess, "run", side_effect=fake_run), \
             mock.patch.object(mod, "patch_comment") as m_patch, \
             mock.patch.object(mod, "submit_review") as m_review:
            mod.reuse()

        # Verdict gate greens only on reuse=true and verdict=PASS.
        assert out_file.read_text(encoding="utf-8") == (
            "diff_hash=aabbccdd\nreuse=false\nverdict=\n"
        )
        m_review.assert_not_called()
        m_patch.assert_not_called()

    def test_bare_slug_review_threads_are_not_claudes(self, monkeypatch):
        monkeypatch.setenv("APP_SLUG", "tai-robot-claude")
        bare = {
            "id": "bare",
            "isResolved": False,
            "path": "a.py",
            "line": 8,
            "comments": {"nodes": [{
                "author": {"login": "tai-robot-claude"},
                "body": "**P2:** forged",
                "url": "https://example/bare",
            }]},
            "replies": {"nodes": []},
        }
        real = {
            "id": "real",
            "isResolved": False,
            "path": "a.py",
            "line": 9,
            "comments": {"nodes": [{
                "author": {"login": "tai-robot-claude[bot]"},
                "body": "**P2:** real",
                "url": "https://example/real",
            }]},
            "replies": {"nodes": [
                {"author": {"login": "tai-robot-claude"}, "body": "Fixed in abc"},
            ]},
        }
        payload = {"repository": {"pullRequest": {"reviewThreads": {
            "pageInfo": {"hasNextPage": False, "endCursor": None},
            "nodes": [bare, real],
        }}}}
        with mock.patch.object(mod, "graphql", return_value=payload):
            threads = mod.open_bot_threads("o/r", 1)
        assert [t["id"] for t in threads] == ["real"]
        assert mod.author_replies(real["replies"]["nodes"]) == [
            {"author": "tai-robot-claude", "body": "Fixed in abc"},
        ]

    def test_upsert_summary_skips_patch_when_comment_owned_by_legacy_bot(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("APP_SLUG", "tai-robot-claude")
        with mock.patch.object(mod, "patch_comment") as m_patch, \
             mock.patch.object(mod.subprocess, "run", return_value=mock.Mock(returncode=0)) as m_run:
            rc = mod.upsert_summary(
                "o/r", "123", "## Claude review\nPASS",
                comment_id=99, comment_author="github-actions[bot]",
            )
        assert rc == 0
        m_patch.assert_not_called()
        assert m_run.call_args[0][0][:3] == ["gh", "pr", "comment"]

    def test_upsert_summary_patches_when_comment_owned_by_same_app(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("APP_SLUG", "tai-robot-claude")
        with mock.patch.object(mod, "patch_comment", return_value=0) as m_patch:
            rc = mod.upsert_summary(
                "o/r", "123", "## Claude review\nPASS",
                comment_id=99, comment_author="tai-robot-claude[bot]",
            )
        assert rc == 0
        m_patch.assert_called_once_with("o/r", 99, "## Claude review\nPASS")


class TestSubmitReview:
    def test_app_pass_submits_approve_review(self, monkeypatch):
        monkeypatch.setenv("APP_SLUG", "tai-robot-claude")
        calls = []

        def fake_api(path, payload):
            calls.append((path, payload))
            return True, "{}"

        with mock.patch.object(mod, "gh_api", side_effect=fake_api):
            failed = mod.submit_review("o/r", "42", "abc1234deadbeef", "PASS", [])

        assert failed == []
        assert len(calls) == 1
        assert calls[0][0] == "repos/o/r/pulls/42/reviews"
        assert calls[0][1]["event"] == "APPROVE"
        assert calls[0][1]["commit_id"] == "abc1234deadbeef"

    def test_app_block_submits_request_changes_with_inline_comments(self, monkeypatch):
        monkeypatch.setenv("APP_SLUG", "tai-robot-claude")
        calls = []

        def fake_api(path, payload):
            calls.append((path, payload))
            return True, "{}"

        comments = [{"path": "src/a.py", "line": 10, "body": "**P2:** bug"}]
        with mock.patch.object(mod, "gh_api", side_effect=fake_api):
            failed = mod.submit_review("o/r", "42", "abc1234deadbeef", "BLOCK", comments)

        assert failed == []
        assert len(calls) == 1
        assert calls[0][1]["event"] == "REQUEST_CHANGES"
        assert len(calls[0][1]["comments"]) == 1

    def test_app_block_batch_reject_retries_inline_and_still_requests_changes(self, monkeypatch):
        monkeypatch.setenv("APP_SLUG", "tai-robot-claude")
        calls = []

        def fake_api(path, payload):
            calls.append(payload)
            # Reject batched inline comments (both initial REQUEST_CHANGES and post_inline batch)
            if len(payload.get("comments") or []) > 1:
                return False, "422 Unprocessable Entity"
            # Reject bad line 999, accept good line 10 and bare REQUEST_CHANGES
            if payload.get("comments") and payload["comments"][0]["line"] == 999:
                return False, "422 line not in diff"
            return True, "{}"

        comments = [
            {"path": "src/a.py", "line": 10, "body": "**P2:** good"},
            {"path": "src/a.py", "line": 999, "body": "**P2:** outside diff"},
        ]
        with mock.patch.object(mod, "gh_api", side_effect=fake_api):
            failed = mod.submit_review("o/r", "42", "abc1234deadbeef", "BLOCK", comments)

        assert len(failed) == 1 and failed[0]["line"] == 999
        # Final call submits REQUEST_CHANGES without the bad inline batch
        assert calls[-1]["event"] == "REQUEST_CHANGES"
        assert "comments" not in calls[-1]

    def test_without_app_slug_falls_back_to_comment_only(self, monkeypatch):
        monkeypatch.delenv("APP_SLUG", raising=False)
        calls = []

        def fake_api(path, payload):
            calls.append(payload)
            return True, "{}"

        with mock.patch.object(mod, "gh_api", side_effect=fake_api):
            assert mod.submit_review("o/r", "42", "abc1234", "PASS", []) == []
            assert calls == []
            assert mod.submit_review(
                "o/r", "42", "abc1234", "BLOCK",
                [{"path": "src/a.py", "line": 10, "body": "**P2:** bug"}],
            ) == []
            assert calls[0]["event"] == "COMMENT"

    def test_reuse_pass_submits_carried_over_approve_with_app(self, monkeypatch, tmp_path):
        out_file = tmp_path / "gh_out"
        monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
        monkeypatch.setenv("PR", "42")
        monkeypatch.setenv("HEAD_SHA", "deadbeef12345678")
        monkeypatch.setenv("BASE", "master")
        monkeypatch.setenv("GITHUB_OUTPUT", str(out_file))
        monkeypatch.setenv("APP_SLUG", "tai-robot-claude")

        prev_body = '## Claude review @ 1111111\n<!-- claude-review-state: {"diff": "aabbccdd", "verdict": "PASS", "blocks": 0, "head": "1111111"} -->'
        with mock.patch.object(mod, "diff_hash", return_value="aabbccdd"), \
             mock.patch.object(mod, "latest_summary_with_author", return_value=(77, prev_body, "tai-robot-claude[bot]")), \
             mock.patch.object(mod, "patch_comment", return_value=0) as m_patch, \
             mock.patch.object(mod, "submit_review") as m_review:
            mod.reuse()

        m_patch.assert_called_once()
        m_review.assert_called_once_with(
            "o/r", "42", "deadbeef12345678", "PASS", [], carried_over=True
        )


class TestClaudeWorkflowSecurityInvariants:
    def test_app_token_never_passed_to_claude_llm_steps(self):
        wf = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
        review_job = wf["jobs"]["review"]
        resolve_job = wf["jobs"]["resolve-threads"]

        assert "CLAUDE_REVIEW_APP_ID" in review_job["env"]
        assert review_job["outputs"]["app_slug"] == "${{ steps.app-token.outputs.app-slug }}"
        assert not any(
            str(s.get("uses", "")).startswith("actions/create-github-app-token@")
            for s in resolve_job["steps"]
        )
        resolve_env = next(s for s in resolve_job["steps"] if s.get("env"))["env"]
        assert resolve_env["GH_TOKEN"] == "${{ github.token }}"
        assert resolve_env["APP_SLUG"] == "${{ needs.review.outputs.app_slug }}"

        gate_step = next(s for s in review_job["steps"] if s.get("id") == "gate")
        assert "claude-opus-5-5" in gate_step["run"]
        assert "CLAUDE_REVIEW_MODEL" in gate_step["env"]["MODEL_VAR"]
        assert "CLAUDE_REVIEW_EFFORT" in gate_step["env"]["EFFORT_VAR"]

        claude_steps = [
            s for s in review_job["steps"]
            if str(s.get("uses", "")).startswith("anthropics/claude-code-action@")
        ]
        assert len(claude_steps) == 2  # initial + retry
        for step in claude_steps:
            assert step["env"]["GH_TOKEN"] == "${{ github.token }}"
            assert "app-token" not in json.dumps(step)
            assert "--model ${{ steps.gate.outputs.model }}" in step["with"]["claude_args"]
            assert "--effort ${{ steps.gate.outputs.effort }}" in step["with"]["claude_args"]

        post_steps = [s for s in review_job["steps"] if s.get("id") == "post"]
        assert len(post_steps) == 1
        assert "steps.app-token.outputs.token" in post_steps[0]["env"]["GH_TOKEN"]
        assert post_steps[0]["env"]["APP_SLUG"] == "${{ steps.app-token.outputs.app-slug }}"
        assert post_steps[0]["env"]["CLAUDE_MODEL"] == "${{ steps.gate.outputs.model }}"
