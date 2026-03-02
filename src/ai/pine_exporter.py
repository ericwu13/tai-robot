"""Export Python backtest strategy to TradingView Pine Script via Claude."""

from __future__ import annotations

from .chat_client import ChatClient
from .prompts import PINE_EXPORT_SYSTEM_PROMPT


def export_to_pine(chat_client: ChatClient, strategy_source: str) -> str:
    """Translate a Python BacktestStrategy to Pine Script v5.

    Uses a separate conversation (fresh context) with PINE_EXPORT_SYSTEM_PROMPT.
    Returns the Pine Script code string.
    """
    # Save and restore original state
    original_system = chat_client.system_prompt
    original_conversation = chat_client.conversation

    try:
        chat_client.system_prompt = PINE_EXPORT_SYSTEM_PROMPT
        chat_client.conversation = []

        prompt = (
            "Translate this Python backtest strategy to TradingView Pine Script v5:\n\n"
            f"```python\n{strategy_source}\n```"
        )
        response = chat_client.send_message(prompt)

        # Extract Pine Script code block
        import re
        pattern = r"```(?:pine|pinescript)?\s*\n(.*?)```"
        match = re.search(pattern, response, re.DOTALL)
        if match:
            return match.group(1).strip()

        # If no code block found, return the full response
        return response

    finally:
        # Restore original state
        chat_client.system_prompt = original_system
        chat_client.conversation = original_conversation
