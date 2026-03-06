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
