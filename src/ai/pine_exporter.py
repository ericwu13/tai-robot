"""Export Python backtest strategy to TradingView Pine Script via Claude."""

from __future__ import annotations

from .chat_client import ChatClient, model_for_tier
from .prompts import PINE_EXPORT_SYSTEM_PROMPT


def export_to_pine(chat_client: ChatClient, strategy_source: str) -> str:
    """Translate a Python BacktestStrategy to Pine Script v5.

    Uses a one-shot API call with PINE_EXPORT_SYSTEM_PROMPT.
    Returns the Pine Script code string.
    """
    prompt = (
        "Translate this Python backtest strategy to TradingView Pine Script v5:\n\n"
        f"```python\n{strategy_source}\n```"
    )
    # Pine translation needs the strategy logic preserved exactly — use the
    # heavy tier so subtle entry/exit conditions don't get paraphrased away.
    pine_model = model_for_tier(chat_client.provider, "heavy")
    response = chat_client.one_shot(
        prompt, system_prompt=PINE_EXPORT_SYSTEM_PROMPT,
        call_site="pine_export", model=pine_model,
    )

    # Extract Pine Script code block
    import re
    pattern = r"```(?:pine|pinescript)?\s*\n(.*?)```"
    match = re.search(pattern, response, re.DOTALL)
    if match:
        return match.group(1).strip()

    # If no code block found, return the full response
    return response
