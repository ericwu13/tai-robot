"""D4 + D5 — skip the AI plan call below min_design_trades; idle line.

Designed to FAIL on pre-fix master:

* an 18-trade design window still called the plan model (the fitness
  gate only annotated the prompt);
* a watermark skip never mentioned how long the book had been idle.

Default ``evolution.min_design_trades`` is 30 (fitness.MIN_TRADES) on
the Saturday/automatic path only. The manual Evolve button is exempt:
it logs ``below min_design_trades (N/30), manual override`` and still
calls the plan model. ``0`` disables the automatic check. Bots at or
above the threshold keep the previous plan-call path.

``evolution.idle_warn_days: 0`` disables the idle line. Fitness math
and watermark advance rules are unchanged: an automatic skip returns
before any AI call and before any watermark write.
"""

from __future__ import annotations

import types
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

import run_backtest as rb
from src.evolution.fitness import MIN_TRADES
from src.evolution.notify import load_watermark, save_watermark

_TPE = timezone(timedelta(hours=8))
_NO_CHANGE = '```json\n{"action": "no_change"}\n```'
_DESIGN_EXIT = "2026-01-01 10:00"
_HOLDOUT_EXIT = "2026-06-01 10:00"


class _PlainStrategy:
    """Not a knob-grid class, so Saturday settle falls through."""


class _InlineThread:
    def __init__(self, target=None, daemon=None, args=(), kwargs=None):
        self._target = target
        self._args = args
        self._kwargs = kwargs or {}

    def start(self):
        if self._target is not None:
            self._target(*self._args, **self._kwargs)


class _DiscordLog:
    def __init__(self):
        self.enabled = True
        self.msgs: list[str] = []

    def notify_evolution(self, msg):
        self.msgs.append(msg)


def _trade(exit_dt: str, pnl: int = 200):
    from src.backtest.broker import OrderSide, Trade

    return Trade(
        tag="t", side=OrderSide.LONG, qty=1,
        entry_price=20000, exit_price=20010,
        entry_bar_index=0, exit_bar_index=1, pnl=pnl,
        entry_dt=exit_dt, exit_dt=exit_dt, source="paper",
    )


def _design_book(n_design: int) -> list:
    """``n_design`` trades aged out of the 14-day holdout, plus one holdout."""
    book = [_trade(_DESIGN_EXIT, pnl=200 if i % 2 == 0 else -80)
            for i in range(n_design)]
    book.append(_trade(_HOLDOUT_EXIT, pnl=50))
    return book


def _freeze_now(monkeypatch, when: datetime) -> None:
    monkeypatch.setattr(rb, "_taipei_now", lambda: when)


def _app(bot_dir, trades, chat, settings_extra=None):
    from src.live.live_runner import LiveState

    runner = types.SimpleNamespace(
        state=LiveState.RUNNING,
        bot_dir=bot_dir,
        strategy=_PlainStrategy(),
        point_value=200,
        target_interval=60,
        symbol="TXF",
        get_bars=lambda: [],
    )
    result = types.SimpleNamespace(
        trades=trades, strategy_name="Plain", metrics=None)
    settings = {
        "evolution_holdout_days": 14,
        "evolution_auto_pipeline": True,
        "evolution_capital_base_twd": 100000,
        "ai_provider": "anthropic",
        "ultra_mode": False,
        "evolution_validation": "simple",
        # Weekly-path fixture. The structural lane defaults on; these
        # tests opt out so they still cover the plan/knob Saturday path
        # and do not write a lane ledger under the repo.
        "evolution_structural_lane": {"enabled": False},
    }
    if settings_extra:
        settings.update(settings_extra)
    app = types.SimpleNamespace(
        _live_runner=runner,
        _settings=settings,
        _chat_client=chat,
        _REVIEW_SINGLE_SHOT_CHAR_LIMIT=600_000,
        _quote_connected=False,
        _live_warmup_mode=False,
        _evo_fetch_cb=None,
        _trading_mode="paper",
        _last_bars=[],
        _strategy_store=types.SimpleNamespace(list_strategies=lambda: []),
        strategy_var=types.SimpleNamespace(get=lambda: "Plain"),
        chat=[],
        errors=[],
        dialogs=[],
        root=types.SimpleNamespace(after=lambda delay, fn, *args: fn(*args)),
        btn_send=types.SimpleNamespace(config=lambda **kwargs: None),
        _result=result,
    )
    app._gather_analysis_payload = lambda: (
        result, "report", "timeframe", "",
        [f"trade {i}" for i in range(len(trades))])
    app._ensure_chat_client = lambda: True
    app._resolve_strategy_source = lambda name, cls: "class Plain: pass\n"
    app._append_chat = lambda role, text: app.chat.append((role, text))
    app.set_status = lambda msg: None
    app._on_chat_response = lambda plan: None
    app._on_chat_error = lambda msg: app.errors.append(msg)
    app._remove_last_system_line = lambda: None
    app._evolution_capital_base = types.MethodType(
        rb.BacktestApp._evolution_capital_base, app)
    app._build_evolution_context = types.MethodType(
        rb.BacktestApp._build_evolution_context, app)
    app._start_evolution_pipeline = types.MethodType(
        rb.BacktestApp._start_evolution_pipeline, app)
    return app


def _chat():
    chat = MagicMock()
    chat.send_message.return_value = _NO_CHANGE
    chat.max_tokens = 1024
    chat.conversation = []
    return chat


def _run(monkeypatch, app, auto_run=True):
    discord = _DiscordLog()
    monkeypatch.setattr(rb, "_discord", discord)
    monkeypatch.setattr(rb.threading, "Thread", _InlineThread)

    def _ask(title, msg):
        app.dialogs.append((title, msg))
        return False

    monkeypatch.setattr(rb.messagebox, "askyesno", _ask)
    rb.BacktestApp._bot_evolution(app, auto_run=auto_run)
    return discord


def _chat_text(app) -> str:
    return "\n".join(text for _role, text in app.chat)


def _ai_calls(chat) -> int:
    return chat.send_message.call_count + chat.one_shot.call_count


# ── pure idle line ────────────────────────────────────────────────────


def test_idle_notice_boundary_uses_calendar_days():
    now = datetime(2026, 10, 24, 6, 0, tzinfo=_TPE)
    trades = [_trade("2026-10-01 09:00"), _trade("2026-10-17 23:59")]
    assert rb._evolution_idle_notice(trades, now, 7) == (
        "idle 7 days since last exit 2026-10-17")
    assert rb._evolution_idle_notice(
        [_trade("2026-10-18 00:01")], now, 7) == ""
    assert rb._evolution_idle_notice(trades, now, 0) == ""


def test_idle_notice_parses_seconds_and_ignores_future():
    now = datetime(2026, 10, 24, 6, 0)
    assert rb._evolution_idle_notice(
        [_trade("2026-10-01 10:00:00")], now, 7) == (
        "idle 23 days since last exit 2026-10-01")
    assert rb._evolution_idle_notice(
        [_trade("2026-11-01 10:00")], now, 7) == ""
    assert rb._evolution_idle_notice([], now, 7) == ""


# ── D4: sample gate ───────────────────────────────────────────────────


def test_eighteen_design_trades_skip_plan_call_and_keep_watermark(
        tmp_path, monkeypatch):
    """FAILS pre-fix: the plan call was made and the prompt was only annotated."""
    _freeze_now(monkeypatch, datetime(2026, 10, 24, 6, 0, tzinfo=_TPE))
    save_watermark(tmp_path, 2, at="2026-02-01 08:00:00")
    before = load_watermark(tmp_path)
    chat = _chat()
    app = _app(tmp_path, _design_book(18), chat)
    discord = _run(monkeypatch, app)

    assert _ai_calls(chat) == 0, app.errors
    text = _chat_text(app)
    assert "waiting for sample: 18/30" in text
    assert "manual override" not in text
    assert any("waiting for sample: 18/30" in m for m in discord.msgs)
    assert load_watermark(tmp_path) == before
    assert app.dialogs == []
    assert MIN_TRADES == 30


def test_custom_threshold_is_the_number_in_the_message(tmp_path, monkeypatch):
    chat = _chat()
    app = _app(tmp_path, _design_book(18), chat,
               {"evolution_min_design_trades": 40})
    _run(monkeypatch, app)
    assert _ai_calls(chat) == 0
    assert "waiting for sample: 18/40" in _chat_text(app)


def test_zero_min_design_trades_still_calls_ai_with_18_trades(
        tmp_path, monkeypatch):
    """0 disables the check. The plan call is made; no_change does not
    advance the watermark (issue #125, unchanged)."""
    save_watermark(tmp_path, 2, at="2026-02-01 08:00:00")
    before = load_watermark(tmp_path)
    chat = _chat()
    app = _app(tmp_path, _design_book(18), chat,
               {"evolution_min_design_trades": 0})
    discord = _run(monkeypatch, app)

    assert chat.send_message.call_count == 1, app.errors or _chat_text(app)
    assert chat.one_shot.call_count == 0
    assert "waiting for sample:" not in _chat_text(app)
    assert "waiting for sample:" not in "\n".join(discord.msgs)
    assert load_watermark(tmp_path) == before


def test_thirty_design_trades_still_call_the_plan(tmp_path, monkeypatch):
    """At the default threshold the previous path is unchanged: one plan
    call, no sample-wait line, watermark stays put on no_change."""
    save_watermark(tmp_path, 1, at="2026-02-01 08:00:00")
    before = load_watermark(tmp_path)
    chat = _chat()
    app = _app(tmp_path, _design_book(30), chat)
    discord = _run(monkeypatch, app)

    assert chat.send_message.call_count == 1, app.errors or _chat_text(app)
    assert chat.one_shot.call_count == 0
    blob = _chat_text(app) + "\n" + "\n".join(discord.msgs)
    assert "waiting for sample:" not in blob
    assert load_watermark(tmp_path) == before


def test_large_design_window_with_small_delta_still_calls_ai(
        tmp_path, monkeypatch):
    """The sample is the design window, not the post-watermark slice.

    40 design trades with only 5 new since the watermark must still call
    the plan model. Counting the delta would skip a bot that is already
    over the threshold.
    """
    save_watermark(tmp_path, 35, at="2026-02-01 08:00:00")
    before = load_watermark(tmp_path)
    chat = _chat()
    app = _app(tmp_path, _design_book(40), chat)
    _run(monkeypatch, app)
    assert chat.send_message.call_count == 1, app.errors or _chat_text(app)
    assert "waiting for sample:" not in _chat_text(app)
    assert load_watermark(tmp_path) == before


def test_holdout_trades_do_not_count_toward_the_sample(tmp_path, monkeypatch):
    """18 design trades plus a fat holdout is still 18/30."""
    book = [_trade(_DESIGN_EXIT) for _ in range(18)]
    book.extend(_trade(_HOLDOUT_EXIT) for _ in range(22))
    chat = _chat()
    app = _app(tmp_path, book, chat)
    _run(monkeypatch, app)
    assert _ai_calls(chat) == 0
    assert "waiting for sample: 18/30" in _chat_text(app)


def test_manual_below_threshold_bypasses_gate_and_logs_override(
        tmp_path, monkeypatch):
    """The on-demand Evolve button is exempt. It still calls the plan
    model and logs the override line. no_change does not advance the
    watermark."""
    save_watermark(tmp_path, 2, at="2026-02-01 08:00:00")
    before = load_watermark(tmp_path)
    chat = _chat()
    app = _app(tmp_path, _design_book(18), chat)
    discord = _run(monkeypatch, app, auto_run=False)

    assert chat.send_message.call_count == 1, app.errors or _chat_text(app)
    assert chat.one_shot.call_count == 0
    text = _chat_text(app)
    assert "below min_design_trades (18/30), manual override" in text
    assert "waiting for sample:" not in text
    assert all("waiting for sample:" not in m for m in discord.msgs)
    assert load_watermark(tmp_path) == before
    assert app.dialogs == []


# ── D5: idle line on watermark skip ───────────────────────────────────


def test_watermark_skip_adds_idle_line_at_default_seven_days(
        tmp_path, monkeypatch):
    """FAILS pre-fix: the skip text had no idle line."""
    _freeze_now(monkeypatch, datetime(2026, 10, 24, 6, 0, tzinfo=_TPE))
    trades = [_trade("2026-10-01 10:00") for _ in range(4)]
    save_watermark(tmp_path, len(trades), at="2026-10-02 05:00:00")
    before = load_watermark(tmp_path)
    chat = _chat()
    app = _app(tmp_path, trades, chat)
    discord = _run(monkeypatch, app)

    line = "idle 23 days since last exit 2026-10-01"
    text = _chat_text(app)
    assert line in text
    assert "No new design-window trades" in text
    assert any(line in m for m in discord.msgs)
    assert _ai_calls(chat) == 0
    assert load_watermark(tmp_path) == before


def test_idle_warn_days_zero_disables_the_line(tmp_path, monkeypatch):
    """0 turns the idle line off even when the book has been quiet for weeks."""
    _freeze_now(monkeypatch, datetime(2026, 10, 24, 6, 0, tzinfo=_TPE))
    trades = [_trade("2026-10-01 10:00") for _ in range(4)]
    save_watermark(tmp_path, len(trades), at="2026-10-02 05:00:00")
    chat = _chat()
    app = _app(tmp_path, trades, chat, {"evolution_idle_warn_days": 0})
    discord = _run(monkeypatch, app)
    text = _chat_text(app)
    assert "No new design-window trades" in text
    assert "idle " not in text
    assert all("idle " not in m for m in discord.msgs)


def test_watermark_skip_omits_idle_line_below_warn_days(tmp_path, monkeypatch):
    _freeze_now(monkeypatch, datetime(2026, 10, 24, 6, 0, tzinfo=_TPE))
    trades = [_trade("2026-10-18 10:00")]
    save_watermark(tmp_path, 1, at="2026-10-18 12:00:00")
    chat = _chat()
    app = _app(tmp_path, trades, chat)
    _run(monkeypatch, app)
    text = _chat_text(app)
    assert "No new design-window trades" in text
    assert "idle " not in text


def test_idle_line_uses_configured_warn_days(tmp_path, monkeypatch):
    _freeze_now(monkeypatch, datetime(2026, 10, 24, 6, 0, tzinfo=_TPE))
    trades = [_trade("2026-10-17 10:00")]
    save_watermark(tmp_path, 1, at="2026-10-17 12:00:00")
    chat = _chat()
    app = _app(tmp_path, trades, chat, {"evolution_idle_warn_days": 14})
    _run(monkeypatch, app)
    assert "idle " not in _chat_text(app)

    chat2 = _chat()
    app2 = _app(tmp_path, trades, chat2, {"evolution_idle_warn_days": 7})
    _run(monkeypatch, app2)
    assert "idle 7 days since last exit 2026-10-17" in _chat_text(app2)


def test_manual_watermark_skip_dialog_includes_idle_line(tmp_path, monkeypatch):
    _freeze_now(monkeypatch, datetime(2026, 10, 24, 6, 0, tzinfo=_TPE))
    trades = [_trade("2026-10-01 10:00")]
    save_watermark(tmp_path, 1, at="2026-10-02 05:00:00")
    before = load_watermark(tmp_path)
    chat = _chat()
    app = _app(tmp_path, trades, chat)
    _run(monkeypatch, app, auto_run=False)
    assert len(app.dialogs) == 1
    assert "idle 23 days since last exit 2026-10-01" in app.dialogs[0][1]
    assert _ai_calls(chat) == 0
    assert load_watermark(tmp_path) == before


# ── settings loader ───────────────────────────────────────────────────


def _cfg(tmp_path, monkeypatch, yaml_text: str) -> dict:
    (tmp_path / "settings.yaml").write_text(yaml_text, encoding="utf-8")
    monkeypatch.setattr(rb, "project_root", str(tmp_path))
    return rb._load_settings()


def test_settings_defaults_match_min_trades_and_seven(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch, "evolution:\n  cadence: weekly\n")
    assert cfg["evolution_min_design_trades"] == MIN_TRADES == 30
    assert cfg["evolution_idle_warn_days"] == 7


def test_settings_zero_and_explicit_values(tmp_path, monkeypatch):
    cfg = _cfg(
        tmp_path, monkeypatch,
        "evolution:\n  min_design_trades: 0\n  idle_warn_days: 14\n")
    assert cfg["evolution_min_design_trades"] == 0
    assert cfg["evolution_idle_warn_days"] == 14


@pytest.mark.parametrize("bad", ["-1", "abc", "null"])
def test_settings_junk_min_design_falls_back_to_30(tmp_path, monkeypatch, bad):
    cfg = _cfg(tmp_path, monkeypatch,
               f"evolution:\n  min_design_trades: {bad}\n")
    assert cfg["evolution_min_design_trades"] == 30


def test_settings_idle_warn_days_zero_is_kept(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch, "evolution:\n  idle_warn_days: 0\n")
    assert cfg["evolution_idle_warn_days"] == 0


def test_min_trades_import_failure_does_not_skip_later_settings(
        tmp_path, monkeypatch):
    """A failure importing MIN_TRADES must not abort the rest of the file.

    idle_warn_days, news, and regime are parsed after that import. The
    design-trade default falls back to 30 and parsing continues.
    """
    import builtins

    real_import = builtins.__import__

    def _boom(name, *args, **kwargs):
        if name == "src.evolution.fitness":
            raise ImportError("fitness unavailable")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _boom)
    cfg = _cfg(
        tmp_path, monkeypatch,
        "evolution:\n  cadence: weekly\n  idle_warn_days: 9\n"
        "news:\n  signal_path: sig.json\n"
        "regime:\n  long_strategy: LongLeg\n")
    assert cfg["evolution_cadence"] == "weekly"
    assert cfg["evolution_capital_base_twd"] == 100_000
    assert cfg["evolution_min_design_trades"] == 30
    assert cfg["evolution_idle_warn_days"] == 9
    assert cfg["news_signal_path"] == "sig.json"
    assert cfg["regime_long_strategy"] == "LongLeg"
