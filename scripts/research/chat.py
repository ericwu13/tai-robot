"""Headless replica of the workbench AI loop, one turn per command.

    python -m scripts.research.chat say    <session> <msg.txt>        # 傳送 chat turn
    python -m scripts.research.chat gen    <session> <out.py>         # Generate Strategy (+2 auto-retries)
    python -m scripts.research.chat review <session> <strategy.py> --bars bars.pkl \\
           --design 2026-03-17:2026-09-15                             # backtest DESIGN + AI Review

Same prompts, tiers and message text as the GUI:
  chat   → STRATEGY_SYSTEM_PROMPT, light tier (gemini flash)
  gen    → one_shot(build_summary + STRATEGY_CODE_CONTEXT, CODE_GEN_SYSTEM_PROMPT), heavy tier
  review → src/ai/review_payload (the GUI's exact preamble + degradation gate), heavy tier
``--ultra`` forces the ultra model (gemini-3.1-pro-preview) for every call.

Settings: ``--settings`` (default env TAI_SETTINGS, else <repo>/settings.yaml).
A worktree has no settings.yaml — point at the main tree's copy; it is only
read. AI usage is logged to TAI_AI_USAGE_LOG if set (default data/ai_usage.csv
of this checkout). Sessions are JSON files under ``--session-dir``.
The holdout window is never sent to the model.
"""

from __future__ import annotations

import argparse
import json
import os
import pickle
import sys

from src.ai import review_payload

CODEGEN_RETRIES = 2  # the GUI's auto-retry budget

NO_CODE_BLOCK = ("Your response did not contain a ```python code block. "
                 "Please output the complete strategy class inside a single "
                 "```python ... ``` code block.")
TRUNCATION_HINT = ("Your previous response was truncated due to token limit. "
                   "Rewrite the SAME strategy more concisely: combine conditions, "
                   "reduce helper variables, keep under 150 lines. "
                   "Do NOT replace it with a different/simpler strategy.\n\n")


def codegen_message(summary: str, code_context: str) -> str:
    """First Generate Strategy message (run_backtest.py ``_generate_strategy``)."""
    return ("Based on this strategy discussion, write the complete strategy code.\n\n"
            "## Conversation Summary\n" + summary + "\n\n" + code_context)


def codegen_retry_message(summary: str, error_msg: str, code_context: str) -> str:
    """Auto-retry message (run_backtest.py ``_generation_retry``)."""
    section = ("## Conversation Summary (for context — generate the SAME strategy)\n"
               + summary + "\n\n") if summary else ""
    return section + error_msg + "\n\n" + code_context


def codegen_error(exc: Exception, truncated: bool, validation: bool) -> str:
    """Error text fed back on a failed load (``_on_generate_response``)."""
    hint = TRUNCATION_HINT if truncated else ""
    what = "The generated code had errors" if validation else "Unexpected error loading strategy"
    return f"{hint}{what}:\n{exc}\n\nPlease fix the code and output a corrected version."


# ── settings / client ──

def load_ai_settings(path: str) -> dict:
    import yaml
    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    return data.get("ai", {}) or {}


def make_client(ai: dict, conversation: list):
    from src.ai.chat_client import ChatClient, DEFAULT_MODELS, PROVIDER_ANTHROPIC, PROVIDER_GOOGLE
    from src.ai.prompts import STRATEGY_SYSTEM_PROMPT
    provider = ai.get("provider", PROVIDER_ANTHROPIC)
    key = ai.get("google_api_key" if provider == PROVIDER_GOOGLE else "anthropic_api_key", "")
    if not key:
        raise SystemExit(f"no API key for provider {provider!r} in settings ai.*")
    model = ai.get("model", "") or DEFAULT_MODELS.get(provider, "")
    client = ChatClient(key, provider=provider, model=model,
                        max_tokens=ai.get("max_tokens", 16384))
    client.set_system_prompt(STRATEGY_SYSTEM_PROMPT)
    client.conversation = conversation
    return client, provider


def tier_model(provider: str, tier: str, ultra_setting: bool, force_ultra: bool):
    from src.ai.chat_client import model_for_tier, resolve_ultra_mode
    if force_ultra:
        return model_for_tier(provider, "ultra")
    return model_for_tier(provider, tier, ultra_mode=resolve_ultra_mode(ultra_setting))


class Session:
    def __init__(self, directory: str, name: str):
        os.makedirs(directory, exist_ok=True)
        self.path = os.path.join(directory, name + ".json")
        self.data = {"conversation": [], "last_review_metrics": None, "codegen_summary": ""}
        if os.path.exists(self.path):
            with open(self.path, encoding="utf-8") as fh:
                self.data.update(json.load(fh))

    def save(self, conversation):
        self.data["conversation"] = conversation
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(self.data, fh, ensure_ascii=False, indent=1)


# ── commands ──

def cmd_say(a, ai, sess):
    client, provider = make_client(ai, sess.data["conversation"])
    with open(a.arg, encoding="utf-8") as fh:
        msg = fh.read()
    resp = client.send_message(msg, call_site="chat",
                               model=tier_model(provider, "light", ai.get("ultra_mode"), a.ultra))
    sess.save(client.conversation)
    print(resp)
    return 0


def cmd_gen(a, ai, sess):
    from src.ai.chat_client import ChatClient
    from src.ai.code_sandbox import (CodeExecutionError, CodeValidationError,
                                     extract_python_code, load_strategy_from_source)
    from src.ai.prompts import CODE_GEN_SYSTEM_PROMPT, STRATEGY_CODE_CONTEXT
    client, provider = make_client(ai, sess.data["conversation"])
    model = tier_model(provider, "heavy", ai.get("ultra_mode"), a.ultra)
    summary = ChatClient.build_summary(client.conversation)
    sess.data["codegen_summary"] = summary
    msg = codegen_message(summary, STRATEGY_CODE_CONTEXT)
    for attempt in range(CODEGEN_RETRIES + 1):
        resp = client.one_shot(msg, system_prompt=CODE_GEN_SYSTEM_PROMPT, max_tokens=16384,
                               call_site="codegen" if attempt == 0 else "codegen_retry",
                               model=model)
        truncated = "[WARNING: Response truncated" in resp
        src = extract_python_code(resp)
        if not src:
            err = NO_CODE_BLOCK
        else:
            try:
                cls = load_strategy_from_source(src)
            except (CodeValidationError, CodeExecutionError) as e:
                err = codegen_error(e, truncated, validation=True)
            except Exception as e:  # noqa: BLE001 — mirror the GUI's catch-all
                err = codegen_error(e, truncated, validation=False)
            else:
                with open(a.arg, "w", encoding="utf-8") as fh:
                    fh.write(src)
                sess.save(client.conversation)
                print(f"OK {cls.__name__} -> {a.arg}")
                print(resp.split("```")[-1].strip()[:3000])
                return 0
        print(f"attempt {attempt + 1} failed: {err.splitlines()[0]}", file=sys.stderr)
        msg = codegen_retry_message(summary, err, STRATEGY_CODE_CONTEXT)
    sess.save(client.conversation)
    print("Auto-retry exhausted.", file=sys.stderr)
    return 1


def cmd_review(a, ai, sess):
    from scripts.research import bt
    from src.backtest.report import format_report
    if not (a.bars and a.design):
        raise SystemExit("review needs --bars and --design")
    cls, src = bt.load_strategy_file(a.arg)
    with open(a.bars, "rb") as fh:
        bars = pickle.load(fh)
    res = bt.run(cls, bars, bt.POINT_VALUES[a.symbol], end=a.design[1])
    trades = bt.segment(res.trades, *a.design)
    # Metrics must describe the trades the model sees: when warmup trades
    # before the design start were dropped, recompute on a closed-trade
    # equity curve (the run's own curve also covers the dropped trades).
    from itertools import accumulate
    from src.backtest.metrics import calculate_metrics
    metrics = (res.metrics if len(trades) == len(res.trades)
               else calculate_metrics(trades, list(accumulate(t.pnl for t in trades)),
                                      initial_balance=bt.GUI_INITIAL_BALANCE))
    report = format_report(cls.__name__, metrics, trades=trades)
    deg = review_payload.degradation_section(
        sess.data.get("last_review_metrics"), metrics.profit_factor, metrics.max_drawdown_pct)
    sess.data["last_review_metrics"] = {"pf": metrics.profit_factor,
                                        "dd_pct": metrics.max_drawdown_pct}
    preamble = review_payload.review_preamble(report, review_payload.review_timeframe_line(cls), deg)
    context = review_payload.single_shot_context(
        preamble, review_payload.trade_lines(trades), review_payload.source_section(src), len(trades))
    client, provider = make_client(ai, sess.data["conversation"])
    resp = client.send_message(context, call_site="trade_review",
                               model=tier_model(provider, "heavy", ai.get("ultra_mode"), a.ultra))
    conv = client.conversation
    if len(conv) >= 2 and conv[-2].get("role") == "user":
        conv[-2]["content"] = review_payload.trimmed_review_stub(len(trades))
    sess.save(conv)
    print(report)
    print("=" * 30, "AI REVIEW", "=" * 30)
    print(resp)
    return 0


def main(argv=None) -> int:
    from scripts.research.bt import POINT_VALUES, _window
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("cmd", choices=["say", "gen", "review"])
    p.add_argument("session")
    p.add_argument("arg", help="say: message file · gen: output .py · review: strategy .py")
    p.add_argument("--settings", default=os.environ.get("TAI_SETTINGS", ""))
    p.add_argument("--session-dir", default="research_sessions")
    p.add_argument("--ultra", action="store_true", help="use the ultra model for every call")
    p.add_argument("--bars", default="")
    p.add_argument("--design", type=_window, default=None)
    p.add_argument("--symbol", default="TMF00", choices=sorted(POINT_VALUES))
    a = p.parse_args(argv)
    if not a.settings:
        from src.paths import project_root
        a.settings = os.path.join(project_root(), "settings.yaml")
    ai = load_ai_settings(a.settings)
    sess = Session(a.session_dir, a.session)
    return {"say": cmd_say, "gen": cmd_gen, "review": cmd_review}[a.cmd](a, ai, sess)


if __name__ == "__main__":
    # cp950 consoles cannot print every char in the help/report text.
    sys.stdout.reconfigure(errors="replace")
    sys.exit(main())
