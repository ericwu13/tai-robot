"""Tests for AI chat client (send_message and one_shot)."""

import json
from unittest.mock import MagicMock, patch

import pytest

from src.ai.chat_client import (
    ChatClient,
    PROVIDER_ANTHROPIC,
    PROVIDER_GOOGLE,
)


# ---------------------------------------------------------------------------
# Helpers to build mock httpx responses
# ---------------------------------------------------------------------------

def _anthropic_response(text: str, status: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = {
        "content": [{"type": "text", "text": text}],
    }
    resp.text = json.dumps(resp.json.return_value)
    return resp


def _anthropic_error(msg: str, status: int = 400) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = {"error": {"message": msg}}
    resp.text = json.dumps(resp.json.return_value)
    return resp


def _google_response(text: str, status: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = {
        "candidates": [{"content": {"parts": [{"text": text}]}}],
    }
    resp.text = json.dumps(resp.json.return_value)
    return resp


def _google_error(msg: str, status: int = 400) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = {"error": {"message": msg}}
    resp.text = json.dumps(resp.json.return_value)
    return resp


# ---------------------------------------------------------------------------
# Anthropic provider tests
# ---------------------------------------------------------------------------

class TestAnthropicSendMessage:

    def _make_client(self) -> ChatClient:
        c = ChatClient(api_key="sk-test", provider=PROVIDER_ANTHROPIC)
        c.set_system_prompt("You are helpful.")
        return c

    def test_basic_send(self):
        client = self._make_client()
        client._client = MagicMock()
        client._client.post.return_value = _anthropic_response("Hello!")

        result = client.send_message("Hi")

        assert result == "Hello!"
        assert len(client.conversation) == 2
        assert client.conversation[0] == {"role": "user", "content": "Hi"}
        assert client.conversation[1] == {"role": "assistant", "content": "Hello!"}

    def test_conversation_accumulates(self):
        client = self._make_client()
        client._client = MagicMock()
        client._client.post.side_effect = [
            _anthropic_response("First reply"),
            _anthropic_response("Second reply"),
        ]

        client.send_message("msg1")
        client.send_message("msg2")

        assert len(client.conversation) == 4
        # Verify the full history was sent on second call
        second_call_payload = client._client.post.call_args_list[1][1]["json"]
        assert len(second_call_payload["messages"]) == 4

    def test_system_prompt_sent(self):
        client = self._make_client()
        client._client = MagicMock()
        client._client.post.return_value = _anthropic_response("ok")

        client.send_message("test")

        payload = client._client.post.call_args[1]["json"]
        assert payload["system"] == "You are helpful."

    def test_api_error_removes_user_message(self):
        client = self._make_client()
        client._client = MagicMock()
        client._client.post.return_value = _anthropic_error("rate limit", 429)

        with pytest.raises(RuntimeError, match="429"):
            client.send_message("test")

        assert len(client.conversation) == 0

    def test_multi_block_response(self):
        client = self._make_client()
        client._client = MagicMock()
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {
            "content": [
                {"type": "text", "text": "Part 1"},
                {"type": "text", "text": " Part 2"},
            ],
        }
        client._client.post.return_value = resp

        result = client.send_message("test")
        assert result == "Part 1 Part 2"


class TestAnthropicOneShot:

    def _make_client(self) -> ChatClient:
        c = ChatClient(api_key="sk-test", provider=PROVIDER_ANTHROPIC)
        c.set_system_prompt("You are helpful.")
        return c

    def test_does_not_modify_conversation(self):
        client = self._make_client()
        client._client = MagicMock()
        client._client.post.return_value = _anthropic_response("Generated code")

        # Pre-populate conversation
        client.conversation = [
            {"role": "user", "content": "old msg"},
            {"role": "assistant", "content": "old reply"},
        ]

        result = client.one_shot("Generate strategy")

        assert result == "Generated code"
        # Conversation unchanged
        assert len(client.conversation) == 2
        assert client.conversation[0]["content"] == "old msg"

    def test_sends_only_user_message(self):
        client = self._make_client()
        client._client = MagicMock()
        client._client.post.return_value = _anthropic_response("code")

        client.conversation = [
            {"role": "user", "content": "old"},
            {"role": "assistant", "content": "old reply"},
        ]

        client.one_shot("new prompt")

        payload = client._client.post.call_args[1]["json"]
        assert len(payload["messages"]) == 1
        assert payload["messages"][0]["content"] == "new prompt"

    def test_api_error_raises(self):
        client = self._make_client()
        client._client = MagicMock()
        client._client.post.return_value = _anthropic_error("bad request", 400)

        with pytest.raises(RuntimeError, match="400"):
            client.one_shot("test")


# ---------------------------------------------------------------------------
# Google provider tests
# ---------------------------------------------------------------------------

class TestGoogleSendMessage:

    def _make_client(self) -> ChatClient:
        c = ChatClient(api_key="goog-test", provider=PROVIDER_GOOGLE)
        c.set_system_prompt("You are helpful.")
        return c

    def test_basic_send(self):
        client = self._make_client()
        client._client = MagicMock()
        client._client.post.return_value = _google_response("Hello from Gemini!")

        result = client.send_message("Hi")

        assert result == "Hello from Gemini!"
        assert len(client.conversation) == 2

    def test_conversation_accumulates(self):
        client = self._make_client()
        client._client = MagicMock()
        client._client.post.side_effect = [
            _google_response("First"),
            _google_response("Second"),
        ]

        client.send_message("msg1")
        client.send_message("msg2")

        assert len(client.conversation) == 4
        # Gemini payload is built from self.conversation which already has
        # the new user msg appended but not yet the assistant reply, so 3 entries
        second_call_payload = client._client.post.call_args_list[1][1]["json"]
        contents = second_call_payload["contents"]
        assert len(contents) == 3
        assert contents[0]["role"] == "user"
        assert contents[1]["role"] == "model"
        assert contents[2]["role"] == "user"

    def test_system_instruction_sent(self):
        client = self._make_client()
        client._client = MagicMock()
        client._client.post.return_value = _google_response("ok")

        client.send_message("test")

        payload = client._client.post.call_args[1]["json"]
        assert payload["system_instruction"]["parts"][0]["text"] == "You are helpful."

    def test_api_error_removes_user_message(self):
        client = self._make_client()
        client._client = MagicMock()
        client._client.post.return_value = _google_error("quota exceeded", 429)

        with pytest.raises(RuntimeError, match="429"):
            client.send_message("test")

        assert len(client.conversation) == 0


class TestGoogleOneShot:

    def _make_client(self) -> ChatClient:
        c = ChatClient(api_key="goog-test", provider=PROVIDER_GOOGLE)
        c.set_system_prompt("You are helpful.")
        return c

    def test_does_not_modify_conversation(self):
        client = self._make_client()
        client._client = MagicMock()
        client._client.post.return_value = _google_response("Generated code")

        client.conversation = [
            {"role": "user", "content": "old"},
            {"role": "assistant", "content": "old reply"},
        ]

        result = client.one_shot("Generate strategy")

        assert result == "Generated code"
        assert len(client.conversation) == 2

    def test_sends_only_user_message(self):
        client = self._make_client()
        client._client = MagicMock()
        client._client.post.return_value = _google_response("code")

        client.conversation = [{"role": "user", "content": "old"}]

        client.one_shot("new prompt")

        payload = client._client.post.call_args[1]["json"]
        assert len(payload["contents"]) == 1
        assert payload["contents"][0]["parts"][0]["text"] == "new prompt"

    def test_api_error_raises(self):
        client = self._make_client()
        client._client = MagicMock()
        client._client.post.return_value = _google_error("bad", 400)

        with pytest.raises(RuntimeError, match="400"):
            client.one_shot("test")


# ---------------------------------------------------------------------------
# General tests
# ---------------------------------------------------------------------------

class TestChatClientGeneral:

    def test_reset_clears_conversation(self):
        client = ChatClient(api_key="test", provider=PROVIDER_ANTHROPIC)
        client.conversation = [{"role": "user", "content": "hi"}]
        client.reset()
        assert client.conversation == []

    def test_default_model_anthropic(self):
        client = ChatClient(api_key="test", provider=PROVIDER_ANTHROPIC)
        assert "claude" in client.model

    def test_default_model_google(self):
        client = ChatClient(api_key="test", provider=PROVIDER_GOOGLE)
        assert "gemini" in client.model

    def test_custom_model(self):
        client = ChatClient(api_key="test", model="custom-model-v1")
        assert client.model == "custom-model-v1"

    def test_no_system_prompt(self):
        client = ChatClient(api_key="test", provider=PROVIDER_ANTHROPIC)
        client._client = MagicMock()
        client._client.post.return_value = _anthropic_response("ok")

        client.send_message("test")

        payload = client._client.post.call_args[1]["json"]
        assert "system" not in payload


# ---------------------------------------------------------------------------
# build_summary tests
# ---------------------------------------------------------------------------

class TestBuildSummary:

    def test_empty_conversation(self):
        assert ChatClient.build_summary([]) == ""

    def test_short_conversation_full_context(self):
        """4 messages or fewer: all are tier A, no truncation needed."""
        conv = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there"},
            {"role": "user", "content": "Write code"},
            {"role": "assistant", "content": "Here is code"},
        ]
        summary = ChatClient.build_summary(conv)
        assert "Hello" in summary
        assert "Hi there" in summary
        assert "Write code" in summary
        assert "Here is code" in summary
        assert "truncated" not in summary

    def test_first_message_preserved_in_long_conversation(self):
        """First message (original code) must survive even with many messages."""
        original_code = "class MyStrategy:\n    pass\n" * 50  # long code
        conv = [{"role": "user", "content": original_code}]
        # Add 20 middle exchanges
        for i in range(20):
            conv.append({"role": "assistant", "content": f"suggestion {i}"})
            conv.append({"role": "user", "content": f"ok {i}"})
        # Final spec
        conv.append({"role": "assistant", "content": "Final strategy spec " * 100})

        summary = ChatClient.build_summary(conv)
        # First message present (possibly truncated but not dropped)
        assert "class MyStrategy" in summary
        # Last message present
        assert "Final strategy spec" in summary

    def test_last_4_messages_preserved(self):
        """Last 4 messages should always be included."""
        conv = [{"role": "user", "content": "first"}]
        for i in range(10):
            conv.append({"role": "assistant", "content": f"mid {i}"})
            conv.append({"role": "user", "content": f"mid reply {i}"})
        conv.append({"role": "assistant", "content": "FINAL_SPEC"})
        conv.append({"role": "user", "content": "GENERATE"})
        conv.append({"role": "assistant", "content": "CLICK_BUTTON"})

        summary = ChatClient.build_summary(conv)
        assert "FINAL_SPEC" in summary
        assert "GENERATE" in summary
        assert "CLICK_BUTTON" in summary

    def test_middle_messages_compressed_in_long_chat(self):
        """With many middle messages, each gets less budget."""
        conv = [{"role": "user", "content": "first msg"}]
        long_middle = "x" * 5000  # each middle msg is 5000 chars
        for i in range(30):
            role = "user" if i % 2 == 0 else "assistant"
            conv.append({"role": role, "content": long_middle})
        conv.extend([
            {"role": "assistant", "content": "last spec"},
            {"role": "user", "content": "gen"},
            {"role": "assistant", "content": "click btn"},
            {"role": "user", "content": "ok"},
        ])

        summary = ChatClient.build_summary(conv)
        # Total should be bounded, not 30 * 5000
        assert len(summary) < 25000
        # First and last messages still present
        assert "first msg" in summary
        assert "last spec" in summary

    def test_middle_messages_dropped_when_budget_exhausted(self):
        """If tier A uses the full budget, middle messages are skipped."""
        conv = [{"role": "user", "content": "first"}]
        for i in range(50):
            conv.append({"role": "assistant", "content": f"mid {i}"})
        conv.extend([
            {"role": "user", "content": "last1"},
            {"role": "assistant", "content": "last2"},
            {"role": "user", "content": "last3"},
            {"role": "assistant", "content": "last4"},
        ])

        # Very small budget — only tier A fits
        summary = ChatClient.build_summary(conv, total_budget=5000, tier_a_limit=1000)
        assert "first" in summary
        assert "last4" in summary

    def test_issue6_scenario(self):
        """Reproduce issue #6: 8-message conversation where first msg has
        strategy code and message 5 has full optimization spec.
        The key identifier must appear AFTER 800 chars so that a naive
        800-char truncation would lose it."""
        # Pad the code so the class name appears after 800 chars
        code = "# " + "x" * 900 + "\nclass RsiMomentumWithTwoPhaseExit(BacktestStrategy):\n" + "    pass\n" * 100
        # Put key info after 800 chars in the optimization spec too
        optimization_spec = "y" * 900 + "\nShort entry: RSI<20, SMA slope down\n" * 30
        conv = [
            {"role": "user", "content": code + "\n優化此策略"},
            {"role": "assistant", "content": "四項優化建議：1. R/R比 2. 移動停利 3. 成交量 4. 空方"},
            {"role": "user", "content": "好的"},
            {"role": "assistant", "content": "請問要先針對哪一項？"},
            {"role": "user", "content": "4個方向都優化"},
            {"role": "assistant", "content": optimization_spec},
            {"role": "user", "content": "請產生策略"},
            {"role": "assistant", "content": "請點擊 Generate Strategy 按鈕"},
        ]

        summary = ChatClient.build_summary(conv)
        # Original code class name (after 800 chars) must be present
        assert "RsiMomentumWithTwoPhaseExit" in summary
        # Short-side rules (after 800 chars in spec) must be present
        assert "RSI<20" in summary
        # Last message must be present
        assert "Generate Strategy" in summary

    def test_scales_with_50_messages(self):
        """50-message conversation stays within reasonable bounds."""
        conv = [{"role": "user", "content": "A" * 4000}]
        for i in range(48):
            role = "user" if i % 2 == 0 else "assistant"
            conv.append({"role": role, "content": "B" * 2000})
        conv.append({"role": "assistant", "content": "C" * 4000})

        summary = ChatClient.build_summary(conv)
        # First message preserved
        assert "A" * 100 in summary
        # Last message preserved
        assert "C" * 100 in summary
        # Total bounded (budget 8000 for tier A, middle messages get small slices)
        assert len(summary) < 20000
