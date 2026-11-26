"""Unit tests for Gemini Issue Triage helper and workflow security invariants."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from unittest import mock

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / ".github" / "scripts" / "gemini_triage_post.py"
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "gemini-triage.yml"


def _load_mod():
    spec = importlib.util.spec_from_file_location("gemini_triage_post", SCRIPT_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mod = _load_mod()


class TestIssueNumberParsing:
    @pytest.mark.parametrize("raw,expected", [("1", 1), ("369", 369), (" 42 ", 42)])
    def test_valid_issue_numbers(self, raw, expected):
        assert mod.parse_issue_number(raw) == expected

    @pytest.mark.parametrize("raw", ["", "0", "-5", "369; ls", "abc", "12.3"])
    def test_invalid_issue_numbers_rejected(self, raw):
        with pytest.raises(ValueError):
            mod.parse_issue_number(raw)


class TestJsonExtraction:
    def test_bare_json_object(self):
        raw = json.dumps({"labels": ["bug", "P1"], "summary_markdown": "## Gemini triage\nOK"})
        out = mod.extract_json_object(raw)
        assert out["labels"] == ["bug", "P1"]

    def test_fenced_json_with_surrounding_prose(self):
        raw = (
            "I read CLAUDE.md and checked the files.\n"
            "```json\n"
            '{"labels": ["bug", "P2"], "severity": "P2", "summary_markdown": "## Gemini triage\\nFound bug"}\n'
            "```\n"
        )
        out = mod.extract_json_object(raw)
        assert out["severity"] == "P2"
        assert "Found bug" in out["summary_markdown"]

    def test_unwrapped_json_embedded_in_prose(self):
        raw = 'Analysis complete: {"labels": ["question"], "severity": "none", "summary_markdown": "Needs logs"}'
        out = mod.extract_json_object(raw)
        assert out["labels"] == ["question"]

    @pytest.mark.parametrize("bad", ["", "no json here", "[]", '{"only": "unrelated"}'])
    def test_invalid_or_unrelated_output_fails_closed(self, bad):
        if bad == '{"only": "unrelated"}':
            # Direct JSON object parses, then format_triage_comment rejects missing summary_markdown
            parsed = mod.extract_json_object(bad)
            with pytest.raises(ValueError, match="summary_markdown"):
                mod.format_triage_comment(parsed, added_labels=[])
        else:
            with pytest.raises(ValueError):
                mod.extract_json_object(bad)


class TestLabelAllowlist:
    REPO_LABELS = {
        "bug", "enhancement", "documentation", "question", "duplicate",
        "P1", "P2", "P3", "autofix", "ultrathink", "claude-review",
        "needs-owner", "follow-up", "gemini-triage",
    }

    def test_blocks_privileged_and_unknown_labels(self):
        added = mod.select_labels_to_add(
            ["bug", "P1", "autofix", "ultrathink", "claude-review", "needs-owner", "made-up"],
            severity="P1",
            existing_labels=set(),
            repo_labels=self.REPO_LABELS,
        )
        assert added == ["bug", "P1"]

    def test_never_overrides_existing_severity_label(self):
        added = mod.select_labels_to_add(
            ["bug", "P1"],
            severity="P1",
            existing_labels={"bug", "P2"},
            repo_labels=self.REPO_LABELS,
        )
        assert added == []

    def test_picks_at_most_one_severity_label(self):
        added = mod.select_labels_to_add(
            ["bug", "P1", "P2"],
            severity="P2",
            existing_labels=set(),
            repo_labels=self.REPO_LABELS,
        )
        assert added == ["bug", "P2"]

    def test_severity_field_adds_label_even_when_omitted_from_labels_list(self):
        added = mod.select_labels_to_add(
            ["bug"],
            severity="P1",
            existing_labels=set(),
            repo_labels=self.REPO_LABELS,
        )
        assert added == ["bug", "P1"]


class TestCommentFormattingAndApply:
    def test_format_prepends_header_strips_spoofed_marker_and_truncates(self):
        body = mod.format_triage_comment(
            {"summary_markdown": f"Line 1 {mod.MARKER}\n" + ("x" * 20_000)},
            added_labels=["bug", "P2"],
            run_url="https://github.com/ericwu13/tai-robot/actions/runs/1",
        )
        assert body.startswith("## Gemini triage\n\n")
        assert body.count(mod.MARKER) == 1
        assert body.endswith(mod.MARKER)
        assert len(body) <= mod.MAX_COMMENT_CHARS
        assert "`bug`, `P2`" in body

    def test_find_existing_triage_comment_matches_bot_only(self):
        comments = [
            {"id": 10, "user": {"login": "random-user"}, "body": f"spoof {mod.MARKER}"},
            {"id": 11, "user": {"login": "github-actions[bot]"}, "body": f"old {mod.MARKER}"},
            {"id": 12, "user": {"login": "github-actions[bot]"}, "body": "unrelated bot comment"},
        ]
        assert mod.find_existing_triage_comment_id(comments) == 11

    def test_apply_triage_creates_then_updates_in_place(self, tmp_path):
        result_file = tmp_path / "result.txt"
        result_file.write_text(
            json.dumps({
                "labels": ["bug", "autofix", "P1"],
                "severity": "P1",
                "summary_markdown": "## Gemini triage\nRoot cause in `src/live/headless_app.py:760`.",
            }),
            encoding="utf-8",
        )

        calls = []

        def fake_api(path, *, method="GET", payload=None):
            calls.append((method, path, payload))
            if path == "repos/o/r/issues/369" and method == "GET":
                return {"number": 369, "labels": [{"name": "bug"}]}
            if path.startswith("repos/o/r/labels"):
                return [{"name": n} for n in TestLabelAllowlist.REPO_LABELS]
            if path.startswith("repos/o/r/issues/369/comments") and method == "GET":
                return []
            return {}

        with mock.patch.object(mod, "gh_api", side_effect=fake_api):
            out = mod.apply_triage("o/r", 369, result_file, run_url="https://example/run/1")

        assert out == {"action": "created", "added_labels": ["P1"]}
        assert ("POST", "repos/o/r/issues/369/labels", {"labels": ["P1"]}) in calls
        post_comments = [c for c in calls if c[0] == "POST" and c[1] == "repos/o/r/issues/369/comments"]
        assert len(post_comments) == 1
        assert mod.MARKER in post_comments[0][2]["body"]

        # Second run finds existing bot comment and PATCHes it in place without re-adding P1
        calls.clear()

        def fake_api_update(path, *, method="GET", payload=None):
            calls.append((method, path, payload))
            if path == "repos/o/r/issues/369" and method == "GET":
                return {"number": 369, "labels": [{"name": "bug"}, {"name": "P1"}]}
            if path.startswith("repos/o/r/labels"):
                return [{"name": n} for n in TestLabelAllowlist.REPO_LABELS]
            if path.startswith("repos/o/r/issues/369/comments") and method == "GET":
                return [{"id": 999, "user": {"login": "github-actions[bot]"}, "body": f"prev {mod.MARKER}"}]
            return {}

        with mock.patch.object(mod, "gh_api", side_effect=fake_api_update):
            out2 = mod.apply_triage("o/r", 369, result_file)

        assert out2 == {"action": "updated", "added_labels": []}
        assert not any(c[1].endswith("/labels") for c in calls)
        assert any(c[0] == "PATCH" and c[1] == "repos/o/r/issues/comments/999" for c in calls)

    def test_collect_rejects_pull_request(self, tmp_path):
        with mock.patch.object(mod, "gh_api", return_value={"number": 371, "pull_request": {}}):
            with pytest.raises(ValueError, match="not a plain issue"):
                mod.collect_context("o/r", 371, tmp_path / "ctx.json")


class TestWorkflowSecurityInvariants:
    def test_workflow_privilege_separation_and_readonly_tools(self):
        wf = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
        # PyYAML parses bare `on:` as boolean True
        triggers = wf.get("on") or wf.get(True)
        assert "issues" in triggers and "workflow_dispatch" in triggers
        assert "pull_request" not in triggers and "pull_request_target" not in triggers

        triage_job = wf["jobs"]["triage"]
        apply_job = wf["jobs"]["apply"]

        # Strict privilege separation: LLM job is read-only; apply job writes issues only
        assert triage_job["permissions"] == {"contents": "read", "issues": "read"}
        assert apply_job["permissions"] == {"contents": "read", "issues": "write"}

        gate_step = next(s for s in triage_job["steps"] if s.get("id") == "gate")
        assert "gemini-4-neon" in gate_step["run"]
        assert "gemini-2.5-pro" not in gate_step["run"]

        gemini_steps = [
            s for s in triage_job["steps"]
            if str(s.get("uses", "")).startswith("google-github-actions/run-gemini-cli@")
        ]
        assert len(gemini_steps) == 1
        step = gemini_steps[0]
        assert step["with"]["gemini_model"] == "${{ steps.gate.outputs.model }}"
        # Pinned to a 40-char commit SHA, never floating @v0/@main
        ref = step["uses"].split("@", 1)[1]
        assert len(ref) == 40 and all(c in "0123456789abcdef" for c in ref)
        # No GitHub token exposed to LLM step
        assert step["env"]["GITHUB_TOKEN"] == ""
        assert step["with"]["gemini_cli_version"] == "0.71.1"
        # Core tools strictly read-only
        settings = json.loads(step["with"]["settings"])
        core_tools = set(settings["tools"]["core"])
        assert core_tools <= {"read_file", "read_many_files", "list_directory", "glob", "grep_search"}
        assert not (core_tools & {"run_shell_command", "write_file", "replace", "web_fetch", "google_web_search"})

        # App token minting happens ONLY in apply (never inside the LLM triage job)
        assert not any(
            str(s.get("uses", "")).startswith("actions/create-github-app-token@")
            for s in triage_job["steps"]
        )
        app_steps = [
            s for s in apply_job["steps"]
            if str(s.get("uses", "")).startswith("actions/create-github-app-token@")
        ]
        assert len(app_steps) == 1
        assert app_steps[0]["with"]["permission-issues"] == "write"
        assert app_steps[0]["with"]["permission-contents"] == "read"

    def test_app_slug_matches_only_app_owned_comments(self):
        comments = [
            {"id": 11, "user": {"login": "github-actions[bot]"}, "body": f"old {mod.MARKER}"},
            {"id": 22, "user": {"login": "tai-robot-gemini[bot]"}, "body": f"app {mod.MARKER}"},
        ]
        assert mod.find_existing_triage_comment_id(comments, app_slug="") == 11
        assert mod.find_existing_triage_comment_id(comments, app_slug="tai-robot-gemini") == 22
        # Never attempts to PATCH a github-actions[bot] comment with an App token
        assert mod.find_existing_triage_comment_id(comments[:1], app_slug="tai-robot-gemini") is None

    def test_format_comment_includes_model_name_when_provided(self):
        body = mod.format_triage_comment(
            {"summary_markdown": "## Gemini triage\nLooks like a P2."},
            added_labels=["bug", "P2"],
            model_name="gemini-4-neon",
        )
        assert "model `gemini-4-neon`" in body
