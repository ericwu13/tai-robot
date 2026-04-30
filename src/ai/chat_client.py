"""Multi-provider AI chat client using httpx (no SDK dependencies)."""

from __future__ import annotations

import csv
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

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

# Tiered Gemini models — used by callers that want to pick light vs heavy reasoning.
GOOGLE_MODEL_PRO = "gemini-2.5-pro"
GOOGLE_MODEL_FLASH = "gemini-2.5-flash"

# Auto-truncate conversation when its total char size exceeds this threshold
# in send_message().  The first message is always kept; the most recent N
# messages that fit are kept, older ones in the middle are dropped.
_CONVERSATION_CHAR_LIMIT = 200_000

# CSV columns for the per-call usage log.  ``reasoning_tokens`` captures
# Gemini 2.5's thoughtsTokenCount — these are billed at the output rate but
# don't show up in candidatesTokenCount, so without this column the log
# under-reports cost for any thinking-enabled model.
_USAGE_LOG_HEADERS = [
    "timestamp", "call_site", "provider", "model",
    "input_tokens", "output_tokens", "reasoning_tokens", "total_tokens",
]

# Print one notice on the first write attempt so the user can confirm where
# logging lives (or see the failure reason).  Toggled to True after first call.
_usage_log_notified = False

_log = logging.getLogger(__name__)


def _resolve_usage_log_path() -> str:
    """Where to append the per-call usage row.

    Resolved at write time (not import time) so the path is robust against
    cwd changes.  Override with TAI_AI_USAGE_LOG for tests/CI.
    """
    override = os.environ.get("TAI_AI_USAGE_LOG")
    if override:
        return override
    # chat_client.py lives at <repo>/src/ai/chat_client.py
    return str(Path(__file__).resolve().parents[2] / "data" / "ai_usage.csv")


def model_for_tier(provider: str, tier: str) -> str | None:
    """Return a tier-appropriate model override, or None to use the user default.

    ``tier`` is ``"light"`` (cheap/fast: chat, recap) or ``"heavy"``
    (quality matters: codegen, trade review).  For non-Google providers we
    return None so the user-configured model is preserved (Anthropic users pick
    their own model in settings).
    """
    if provider == PROVIDER_GOOGLE:
        if tier == "light":
            return GOOGLE_MODEL_FLASH
        return GOOGLE_MODEL_PRO
    return None


def _log_token_usage(
    *, call_site: str, provider: str, model: str,
    input_tokens: int, output_tokens: int,
    reasoning_tokens: int = 0, total_tokens: int = 0,
) -> None:
    """Append one row to ``data/ai_usage.csv``.  Best-effort — never raises.

    ``total_tokens`` should be the provider's authoritative total when
    available (Gemini's ``totalTokenCount`` already includes thinking).
    Falls back to ``input + output + reasoning`` when zero.
    """
    global _usage_log_notified
    path = _resolve_usage_log_path()
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)

        # If a pre-existing file uses an older/different header, archive it
        # so we don't append rows with mismatched columns.
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    first_line = f.readline().strip()
                expected = ",".join(_USAGE_LOG_HEADERS)
                if first_line and first_line != expected:
                    os.rename(path, path + ".bak")
            except OSError:
                pass

        new_file = not os.path.exists(path)
        in_t = int(input_tokens or 0)
        out_t = int(output_tokens or 0)
        think_t = int(reasoning_tokens or 0)
        tot_t = int(total_tokens or 0) or (in_t + out_t + think_t)

        with open(path, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if new_file:
                w.writerow(_USAGE_LOG_HEADERS)
            w.writerow([
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
                call_site, provider, model,
                in_t, out_t, think_t, tot_t,
            ])

        if not _usage_log_notified:
            print(f"[ai_usage] logging to {path}", file=sys.stderr)
            _usage_log_notified = True
    except Exception as e:
        if not _usage_log_notified:
            print(f"[ai_usage] WARNING: failed to write {path}: {e}",
                  file=sys.stderr)
            _usage_log_notified = True
        _log.debug("Failed to write AI usage log: %s", e)


def _extract_anthropic_usage(data: dict) -> tuple[int, int, int, int]:
    """Return (input, output, reasoning, total) for an Anthropic response.

    Anthropic bills extended-thinking output as regular ``output_tokens``,
    so reasoning is reported as 0 and total = input + output.
    """
    usage = data.get("usage") or {}
    in_t = int(usage.get("input_tokens", 0))
    out_t = int(usage.get("output_tokens", 0))
    return in_t, out_t, 0, in_t + out_t


def _extract_google_usage(data: dict) -> tuple[int, int, int, int]:
    """Return (input, output, reasoning, total) for a Gemini response.

    Gemini 2.5 reports thinking under ``thoughtsTokenCount`` separately from
    ``candidatesTokenCount`` (visible output).  Both are billed at the output
    rate.  ``totalTokenCount`` is the API's authoritative billed total.
    """
    meta = data.get("usageMetadata") or {}
    in_t = int(meta.get("promptTokenCount", 0))
    out_t = int(meta.get("candidatesTokenCount", 0))
    think_t = int(meta.get("thoughtsTokenCount", 0))
    tot_t = int(meta.get("totalTokenCount", 0)) or (in_t + out_t + think_t)
    return in_t, out_t, think_t, tot_t


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
        self._client = httpx.Client(timeout=httpx.Timeout(
            connect=30.0, read=300.0, write=30.0, pool=30.0,
        ))

    def set_system_prompt(self, prompt: str) -> None:
        self.system_prompt = prompt

    def _enforce_conversation_size(self) -> None:
        """If conversation chars exceed _CONVERSATION_CHAR_LIMIT, drop oldest
        middle messages, keeping conversation[0] and the most recent tail.
        """
        conv = self.conversation
        total = sum(len(m.get("content", "")) for m in conv)
        if total <= _CONVERSATION_CHAR_LIMIT or len(conv) <= 2:
            return

        first = conv[0]
        first_size = len(first.get("content", ""))
        budget = _CONVERSATION_CHAR_LIMIT - first_size

        kept_tail: list[dict] = []
        running = 0
        # Walk from the end, keep as many recent messages as fit.
        for msg in reversed(conv[1:]):
            size = len(msg.get("content", ""))
            if running + size > budget and kept_tail:
                break
            kept_tail.append(msg)
            running += size
        kept_tail.reverse()

        dropped = len(conv) - 1 - len(kept_tail)
        if dropped <= 0:
            return

        new_conv = [first] + kept_tail
        self.conversation = new_conv
        _log.warning(
            "ChatClient: conversation auto-truncated (%d chars > %d); "
            "kept first message + last %d of %d, dropped %d middle messages.",
            total, _CONVERSATION_CHAR_LIMIT, len(kept_tail), len(conv) - 1, dropped,
        )

    def send_message(
        self,
        user_message: str,
        *,
        call_site: str = "unknown",
        model: str | None = None,
    ) -> str:
        """Send a message and return the assistant's response text.

        Blocking call — run from a background thread when used with Tkinter.
        """
        self.conversation.append({"role": "user", "content": user_message})
        self._enforce_conversation_size()

        try:
            if self.provider == PROVIDER_GOOGLE:
                return self._send_google(user_message, call_site=call_site, model=model)
            else:
                return self._send_anthropic(user_message, call_site=call_site, model=model)
        except Exception:
            # Remove the user message on failure
            self.conversation.pop()
            raise

    def _send_anthropic(self, user_message: str, *, call_site: str, model: str | None) -> str:
        """Send via Anthropic API."""
        used_model = model or self.model
        payload: dict = {
            "model": used_model,
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

        in_tok, out_tok, think_tok, tot_tok = _extract_anthropic_usage(data)
        _log_token_usage(
            call_site=call_site, provider=self.provider, model=used_model,
            input_tokens=in_tok, output_tokens=out_tok,
            reasoning_tokens=think_tok, total_tokens=tot_tok,
        )

        self.conversation.append({"role": "assistant", "content": assistant_text})
        return assistant_text

    def _send_google(self, user_message: str, *, call_site: str, model: str | None) -> str:
        """Send via Google Gemini API."""
        used_model = model or self.model

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

        url = GOOGLE_API_URL.format(model=used_model) + f"?key={self.api_key}"
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

        in_tok, out_tok, think_tok, tot_tok = _extract_google_usage(data)
        _log_token_usage(
            call_site=call_site, provider=self.provider, model=used_model,
            input_tokens=in_tok, output_tokens=out_tok,
            reasoning_tokens=think_tok, total_tokens=tot_tok,
        )

        self.conversation.append({"role": "assistant", "content": assistant_text})

        # Check if response was truncated
        if candidates and candidates[0].get("finishReason") == "MAX_TOKENS":
            assistant_text += "\n\n[WARNING: Response truncated due to token limit]"

        return assistant_text

    def one_shot(
        self,
        user_message: str,
        system_prompt: str | None = None,
        max_tokens: int | None = None,
        *,
        call_site: str = "unknown",
        model: str | None = None,
    ) -> str:
        """Single API call without modifying conversation history.

        Uses the given system_prompt (or self.system_prompt if None).
        Does NOT append to self.conversation.
        Useful for code generation where we don't want to bloat chat history.
        """
        prompt = system_prompt if system_prompt is not None else self.system_prompt
        tokens = max_tokens or self.max_tokens
        if self.provider == PROVIDER_GOOGLE:
            return self._one_shot_google(user_message, prompt, tokens,
                                         call_site=call_site, model=model)
        return self._one_shot_anthropic(user_message, prompt, tokens,
                                        call_site=call_site, model=model)

    def _one_shot_anthropic(self, user_message: str, system_prompt: str = "",
                            max_tokens: int = 0, *, call_site: str = "unknown",
                            model: str | None = None) -> str:
        used_model = model or self.model
        payload: dict = {
            "model": used_model,
            "max_tokens": max_tokens or self.max_tokens,
            "messages": [{"role": "user", "content": user_message}],
        }
        if system_prompt:
            payload["system"] = system_prompt

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

        in_tok, out_tok, think_tok, tot_tok = _extract_anthropic_usage(data)
        _log_token_usage(
            call_site=call_site, provider=self.provider, model=used_model,
            input_tokens=in_tok, output_tokens=out_tok,
            reasoning_tokens=think_tok, total_tokens=tot_tok,
        )
        return assistant_text

    def _one_shot_google(self, user_message: str, system_prompt: str = "",
                         max_tokens: int = 0, *, call_site: str = "unknown",
                         model: str | None = None) -> str:
        used_model = model or self.model
        contents = [{"role": "user", "parts": [{"text": user_message}]}]

        payload: dict = {
            "contents": contents,
            "generationConfig": {"maxOutputTokens": max_tokens or self.max_tokens},
        }
        if system_prompt:
            payload["system_instruction"] = {"parts": [{"text": system_prompt}]}

        url = GOOGLE_API_URL.format(model=used_model) + f"?key={self.api_key}"
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

        in_tok, out_tok, think_tok, tot_tok = _extract_google_usage(data)
        _log_token_usage(
            call_site=call_site, provider=self.provider, model=used_model,
            input_tokens=in_tok, output_tokens=out_tok,
            reasoning_tokens=think_tok, total_tokens=tot_tok,
        )

        # Check if response was truncated
        if candidates and candidates[0].get("finishReason") == "MAX_TOKENS":
            assistant_text += "\n\n[WARNING: Response truncated due to token limit]"

        return assistant_text

    @staticmethod
    def build_summary(conversation: list[dict], total_budget: int = 8000,
                      tier_a_limit: int = 3000) -> str:
        """Build a condensed conversation summary within a character budget.

        Prioritizes the first message (original context/code) and the last 4
        messages (final spec + recent exchanges).  Middle messages split the
        remaining budget equally and are dropped if budget is exhausted.
        """
        n = len(conversation)
        if n == 0:
            return ""

        tier_a_indices = {0} | {i for i in range(max(1, n - 4), n)}
        tier_b_count = n - len(tier_a_indices)

        tier_a_used = min(total_budget, len(tier_a_indices) * tier_a_limit)
        tier_b_budget = max(0, total_budget - tier_a_used)
        tier_b_per_msg = (tier_b_budget // tier_b_count) if tier_b_count > 0 else 0

        parts = []
        for i, msg in enumerate(conversation):
            role = "User" if msg["role"] == "user" else "Assistant"
            content = msg.get("content", "")
            max_len = tier_a_limit if i in tier_a_indices else tier_b_per_msg
            if max_len <= 0:
                continue
            if len(content) > max_len:
                content = content[:max_len] + "\n...(truncated)"
            parts.append(f"{role}: {content}")
        return "\n\n".join(parts)

    def reset(self) -> None:
        """Clear conversation history for a fresh start."""
        self.conversation.clear()

    def close(self) -> None:
        self._client.close()
