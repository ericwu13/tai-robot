"""Multi-provider AI chat client using httpx (no SDK dependencies)."""

from __future__ import annotations

import httpx


# Provider constants
PROVIDER_ANTHROPIC = "anthropic"
PROVIDER_GOOGLE = "google"

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_API_VERSION = "2023-06-01"

GOOGLE_API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

# Default models per provider
DEFAULT_MODELS = {
    PROVIDER_ANTHROPIC: "claude-sonnet-4-20250514",
    PROVIDER_GOOGLE: "gemini-2.5-pro",
}


class ChatClient:
    """Stateful AI chat client supporting Anthropic and Google Gemini.

    Uses httpx directly instead of SDKs to keep PyInstaller builds small.
    """

    def __init__(
        self,
        api_key: str,
        provider: str = PROVIDER_ANTHROPIC,
        model: str = "",
        max_tokens: int = 16384,
    ):
        self.api_key = api_key
        self.provider = provider
        self.model = model or DEFAULT_MODELS.get(provider, "")
        self.max_tokens = max_tokens
        self.system_prompt: str = ""
        self.conversation: list[dict] = []
        self._client = httpx.Client(timeout=120.0)

    def set_system_prompt(self, prompt: str) -> None:
        self.system_prompt = prompt

    def send_message(self, user_message: str) -> str:
        """Send a message and return the assistant's response text.

        Blocking call — run from a background thread when used with Tkinter.
        """
        self.conversation.append({"role": "user", "content": user_message})

        try:
            if self.provider == PROVIDER_GOOGLE:
                return self._send_google(user_message)
            else:
                return self._send_anthropic(user_message)
        except Exception:
            # Remove the user message on failure
            self.conversation.pop()
            raise

    def _send_anthropic(self, user_message: str) -> str:
        """Send via Anthropic API."""
        payload: dict = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": self.conversation,
        }
        if self.system_prompt:
            payload["system"] = self.system_prompt

        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": ANTHROPIC_API_VERSION,
            "content-type": "application/json",
        }

        response = self._client.post(ANTHROPIC_API_URL, json=payload, headers=headers)

        if response.status_code != 200:
            error_body = response.text
            try:
                err_json = response.json()
                error_body = err_json.get("error", {}).get("message", response.text)
            except Exception:
                pass
            raise RuntimeError(f"Anthropic API error {response.status_code}: {error_body}")

        data = response.json()
        assistant_text = ""
        for block in data.get("content", []):
            if block.get("type") == "text":
                assistant_text += block["text"]

        self.conversation.append({"role": "assistant", "content": assistant_text})
        return assistant_text

    def _send_google(self, user_message: str) -> str:
        """Send via Google Gemini API."""
        # Build Gemini conversation format
        contents = []

        # System instruction is separate in Gemini
        if self.system_prompt:
            system_instruction = {"parts": [{"text": self.system_prompt}]}
        else:
            system_instruction = None

        # Convert conversation history to Gemini format
        for msg in self.conversation:
            role = "user" if msg["role"] == "user" else "model"
            contents.append({
                "role": role,
                "parts": [{"text": msg["content"]}],
            })

        payload: dict = {
            "contents": contents,
            "generationConfig": {
                "maxOutputTokens": self.max_tokens,
            },
        }
        if system_instruction:
            payload["system_instruction"] = system_instruction

        url = GOOGLE_API_URL.format(model=self.model) + f"?key={self.api_key}"
        headers = {"content-type": "application/json"}

        response = self._client.post(url, json=payload, headers=headers)

        if response.status_code != 200:
            error_body = response.text
            try:
                err_json = response.json()
                error_body = err_json.get("error", {}).get("message", response.text)
            except Exception:
                pass
            raise RuntimeError(f"Gemini API error {response.status_code}: {error_body}")

        data = response.json()
        assistant_text = ""
        candidates = data.get("candidates", [])
        if candidates:
            parts = candidates[0].get("content", {}).get("parts", [])
            for part in parts:
                if "text" in part:
                    assistant_text += part["text"]

        self.conversation.append({"role": "assistant", "content": assistant_text})
        return assistant_text

    def one_shot(self, user_message: str) -> str:
        """Single API call without modifying conversation history.

        Uses the same system prompt but does NOT append to self.conversation.
        Useful for code generation where we don't want to bloat chat history.
        """
        if self.provider == PROVIDER_GOOGLE:
            return self._one_shot_google(user_message)
        return self._one_shot_anthropic(user_message)

    def _one_shot_anthropic(self, user_message: str) -> str:
        payload: dict = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": [{"role": "user", "content": user_message}],
        }
        if self.system_prompt:
            payload["system"] = self.system_prompt

        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": ANTHROPIC_API_VERSION,
            "content-type": "application/json",
        }

        response = self._client.post(ANTHROPIC_API_URL, json=payload, headers=headers)

        if response.status_code != 200:
            error_body = response.text
            try:
                err_json = response.json()
                error_body = err_json.get("error", {}).get("message", response.text)
            except Exception:
                pass
            raise RuntimeError(f"Anthropic API error {response.status_code}: {error_body}")

        data = response.json()
        assistant_text = ""
        for block in data.get("content", []):
            if block.get("type") == "text":
                assistant_text += block["text"]
        return assistant_text

    def _one_shot_google(self, user_message: str) -> str:
        contents = [{"role": "user", "parts": [{"text": user_message}]}]

        payload: dict = {
            "contents": contents,
            "generationConfig": {"maxOutputTokens": self.max_tokens},
        }
        if self.system_prompt:
            payload["system_instruction"] = {"parts": [{"text": self.system_prompt}]}

        url = GOOGLE_API_URL.format(model=self.model) + f"?key={self.api_key}"
        headers = {"content-type": "application/json"}

        response = self._client.post(url, json=payload, headers=headers)

        if response.status_code != 200:
            error_body = response.text
            try:
                err_json = response.json()
                error_body = err_json.get("error", {}).get("message", response.text)
            except Exception:
                pass
            raise RuntimeError(f"Gemini API error {response.status_code}: {error_body}")

        data = response.json()
        assistant_text = ""
        candidates = data.get("candidates", [])
        if candidates:
            parts = candidates[0].get("content", {}).get("parts", [])
            for part in parts:
                if "text" in part:
                    assistant_text += part["text"]
        return assistant_text

    def reset(self) -> None:
        """Clear conversation history for a fresh start."""
        self.conversation.clear()

    def close(self) -> None:
        self._client.close()
