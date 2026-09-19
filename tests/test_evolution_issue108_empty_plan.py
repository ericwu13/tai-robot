"""issue #108 — an empty / token-truncated EVO 1/3 plan must abort the run.

Both the 2026-09-07 and the 2026-09-19 weekly runs got a plan response
with ZERO visible text: Gemini "pro" reported ``3,614 in / 0 out / 4,093
reasoning`` with ``finishReason=MAX_TOKENS`` — thinking tokens share the
``maxOutputTokens`` pool and the reporter's cap was the template's 4096.
HTTP was 200, so the #109 transient-status retry never fired, and
``parse_plan_directives``' deliberately tolerant default turned that
nothing into ``action="change"``: the watermark burned the trade delta
(issue #125 semantics) and codegen mutated the strategy from an empty
plan, producing a meaningless verdict.

Designed to FAIL against the pre-fix code:
  * every test in ``TestPlanUnusableReason`` (the helper did not exist);
  * every test in ``TestPipelineRefusesUnusablePlan`` (the glue had no
    gate between the plan call and the watermark / codegen);
  * ``TestSendMessageMaxTokens`` (``send_message`` took no override).
The two "still usable" cases are the guard rails: a complete plan that
merely lacks a JSON block, and a truncated plan that still carries a
valid directive block, must both keep running.
"""

from __future__ import annotations

import ast
import inspect
import json
import textwrap
from unittest.mock import MagicMock

import run_backtest as rb
from src.ai.chat_client import (
    ChatClient,
    PROVIDER_ANTHROPIC,
    PROVIDER_GOOGLE,
    TRUNCATION_WARNING,
    _format_usage_line,
)
from src.evolution.pipeline import plan_unusable_reason


# ── Annotated-response builders ───────────────────────────────────────
#
# Built with the client's OWN constant + formatter, never hand-copied
# literals, so a format drift in chat_client breaks these tests instead
# of silently defeating the strip.

def _annotate(body: str, *, truncated: bool,
              in_tok: int = 3614, out_tok: int = 0,
              think_tok: int = 4093) -> str:
    """Reproduce exactly what ``ChatClient`` returns to its caller."""
    text = body
    if truncated:
        text += f"\n\n{TRUNCATION_WARNING}"
    text += _format_usage_line(in_tok, out_tok, think_tok,
                               in_tok + out_tok + think_tok,
                               model="gemini-2.5-pro")
    return text


_DIRECTIVES = '```json\n{"action": "change"}\n```'


class TestPlanUnusableReason:

    def test_reporter_case_empty_body_plus_annotations(self):
        """The actual 09-19 response: no text, MAX_TOKENS, usage line."""
        plan = _annotate("", truncated=True)
        assert plan.strip(), "fixture must be non-empty before stripping"
        assert plan_unusable_reason(plan)

    def test_whitespace_only_is_unusable(self):
        assert plan_unusable_reason("   \n\t\n  ")
        assert plan_unusable_reason("")
        assert plan_unusable_reason(None)

    def test_whitespace_body_with_annotations_is_unusable(self):
        assert plan_unusable_reason(_annotate("\n   \n", truncated=True))

    def test_truncated_without_directive_block_is_unusable(self):
        plan = _annotate(
            "## 分析 Analysis\nThe strategy's stop is too tight; I propose"
            " widening it to 1.5 ATR because the last 30 trades show",
            truncated=True)
        assert plan_unusable_reason(plan)

    def test_truncated_with_valid_directive_block_stays_usable(self):
        """It reached the end of its answer; the cut came after."""
        plan = _annotate(f"## 計畫 Plan\nWiden the stop.\n{_DIRECTIVES}",
                         truncated=True)
        assert plan_unusable_reason(plan) == ""

    def test_complete_plan_without_json_block_stays_usable(self):
        """A formatting whim, not a failure — parse_plan_directives'
        tolerant default is deliberate for this case."""
        plan = _annotate("## 計畫 Plan\nWiden the stop to 1.5 ATR. "
                         "Everything else stays identical.", truncated=False)
        assert plan_unusable_reason(plan) == ""

    def test_normal_plan_is_usable(self):
        plan = _annotate(f"## 計畫 Plan\nWiden the stop.\n{_DIRECTIVES}",
                         truncated=False)
        assert plan_unusable_reason(plan) == ""

    def test_no_change_plan_is_usable(self):
        """A usable plan may well say "do nothing" — that decision belongs
        to ``parse_plan_directives``, not to this gate."""
        plan = _annotate('繼續收集數據 continue collecting data.\n'
                         '```json\n{"action": "no_change"}\n```',
                         truncated=False)
        assert plan_unusable_reason(plan) == ""

    def test_unannotated_plan_is_usable(self):
        """Anthropic responses carry no truncation warning at all."""
        assert plan_unusable_reason(f"Plan text.\n{_DIRECTIVES}") == ""

    def test_reason_is_a_nonempty_string_for_the_caller(self):
        reason = plan_unusable_reason(_annotate("", truncated=True))
        assert isinstance(reason, str) and reason.strip()

    def test_usage_line_alone_does_not_hide_an_empty_body(self):
        """No MAX_TOKENS, but still zero visible text."""
        assert plan_unusable_reason(_annotate("", truncated=False))

    def test_body_mentioning_tokens_is_not_mistaken_for_the_usage_line(self):
        plan = _annotate(
            "The plan: raise 📊 tokens: budget — see the note below.\n"
            f"{_DIRECTIVES}", truncated=False)
        assert plan_unusable_reason(plan) == ""


# ── Glue control-flow (inspect/ast; FAILS on pre-fix run_backtest.py) ──
#
# Same style as tests/test_evolution_issue125.py: the evolution worker is
# a closure inside a Tk method, so source inspection is how this repo
# pins its ordering.

def _pipeline_src() -> str:
    return textwrap.dedent(
        inspect.getsource(rb.BacktestApp._start_evolution_pipeline))


def _gate_node() -> ast.If:
    """The ``if <plan unusable>: ... return`` statement in the worker."""
    tree = ast.parse(_pipeline_src())
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and "plan_unusable_reason" in ast.dump(
                node.test):
            return node
    raise AssertionError(
        "no `if plan_unusable_reason(plan)` gate in the evolution worker "
        "— an empty plan would advance the watermark and run codegen")


class TestPipelineRefusesUnusablePlan:

    def test_gate_exists_and_returns(self):
        gate = _gate_node()
        assert any(isinstance(n, ast.Return) for n in gate.body), (
            "the gate must RETURN — falling through runs codegen on a "
            "plan of nothing")

    def test_gate_runs_before_watermark_and_codegen(self):
        src = _pipeline_src()
        i_gate = src.index("plan_unusable_reason(plan)")
        i_parse = src.index("parse_plan_directives(plan)")
        i_watermark = src.index("maybe_advance_watermark(")
        i_codegen = src.index("one_shot(")
        assert i_gate < i_parse, "refuse before parsing the directives"
        assert i_gate < i_watermark, (
            "issue #125 semantics: an unusable plan must NOT consume the "
            "trade delta")
        assert i_gate < i_codegen, "no candidate may be generated from it"

    def test_gate_notifies_discord_when_auto_run(self):
        gate = _gate_node()
        body = ast.dump(ast.Module(body=gate.body, type_ignores=[]))
        assert "auto_run" in body and "_notify_discord" in body, (
            "an unattended Saturday run must report the abort to Discord")

    def test_gate_message_is_bilingual_and_names_the_setting(self):
        gate = _gate_node()
        text = " ".join(
            n.value for n in ast.walk(ast.Module(body=gate.body,
                                                 type_ignores=[]))
            if isinstance(n, ast.Constant) and isinstance(n.value, str))
        assert "EVO" in text
        assert any("一" <= ch <= "鿿" for ch in text), (
            "release/chat messages are bilingual 繁體中文 + English")
        assert "watermark" in text, "say that nothing was consumed"
        assert "max_tokens" in text, "point at the settings.yaml knob"

    def test_plan_call_gets_the_codegen_token_floor(self):
        """The plan call must not inherit a 4096 cap from settings."""
        src = _pipeline_src()
        i_plan = src.index('call_site="bot_evolution"')
        window = src[i_plan - 400:i_plan + 400]
        assert "_CODE_GEN_MAX_TOKENS" in window, (
            "thinking tokens share maxOutputTokens — the plan call needs "
            "the same floor the codegen calls already have")


# ── send_message(max_tokens=...) plumbing ─────────────────────────────

def _google_ok(text: str = "plan") -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {
        "candidates": [{"content": {"parts": [{"text": text}]}}]}
    resp.text = json.dumps(resp.json.return_value)
    return resp


def _anthropic_ok(text: str = "plan") -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"content": [{"type": "text", "text": text}]}
    resp.text = json.dumps(resp.json.return_value)
    return resp


def _payload(client: ChatClient) -> dict:
    call = client._client.post.call_args
    return call.kwargs.get("json") or call[1].get("json")


class TestSendMessageMaxTokens:

    def test_google_override_reaches_max_output_tokens(self):
        client = ChatClient("goog", provider=PROVIDER_GOOGLE, max_tokens=4096)
        client._client = MagicMock()
        client._client.post.return_value = _google_ok()
        client.send_message("hi", max_tokens=16384)
        assert _payload(client)["generationConfig"]["maxOutputTokens"] == 16384

    def test_google_default_unchanged(self):
        client = ChatClient("goog", provider=PROVIDER_GOOGLE, max_tokens=4096)
        client._client = MagicMock()
        client._client.post.return_value = _google_ok()
        client.send_message("hi")
        assert _payload(client)["generationConfig"]["maxOutputTokens"] == 4096

    def test_anthropic_override_reaches_payload(self):
        client = ChatClient("sk", provider=PROVIDER_ANTHROPIC, max_tokens=4096)
        client._client = MagicMock()
        client._client.post.return_value = _anthropic_ok()
        client.send_message("hi", max_tokens=16384)
        assert _payload(client)["max_tokens"] == 16384

    def test_anthropic_default_unchanged(self):
        client = ChatClient("sk", provider=PROVIDER_ANTHROPIC, max_tokens=4096)
        client._client = MagicMock()
        client._client.post.return_value = _anthropic_ok()
        client.send_message("hi")
        assert _payload(client)["max_tokens"] == 4096

    def test_override_does_not_stick_to_the_client(self):
        client = ChatClient("goog", provider=PROVIDER_GOOGLE, max_tokens=4096)
        client._client = MagicMock()
        client._client.post.return_value = _google_ok()
        client.send_message("hi", max_tokens=16384)
        client.send_message("hi again")
        assert client.max_tokens == 4096
        assert _payload(client)["generationConfig"]["maxOutputTokens"] == 4096
