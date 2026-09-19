"""Phase 1 UI infrastructure — theme, widgets, status strip, headless construct.

Tk-dependent cases skip when the interpreter has no tkinter (some CI
shells / this cloud image). Pure constants and the labels seam run
everywhere.
"""

from __future__ import annotations

import threading

import pytest

from src.live.headless import (
    TITLE_DATA_RANGE,
    TITLE_EXISTING_POSITION,
    TITLE_STRATEGY_CHANGE,
)
from src.ui import labels
from src.ui.theme import (
    CHAT_TAGS,
    DOT_COLORS,
    EPISODE_TAGS,
    LOG_TAGS,
    PALETTE,
    STATUS_LEVEL_STYLES,
    TONE,
)

# ── skip-guard (Tk unavailable in some CI shells) ──

def _tk_probe():
    try:
        import tkinter as tk
        root = tk.Tk()
        root.withdraw()
        root.destroy()
        return True
    except Exception:
        return False


_TK_OK = _tk_probe()
_tk_skip = pytest.mark.skipif(not _TK_OK, reason="Tk unavailable")


# ── (pure) palette / labels — designed to fail pre-Phase-1 ──

def test_palette_has_plan_tokens():
    expected = {
        "bg": "#16161a",
        "bg_raised": "#1e1e24",
        "bg_inset": "#121216",
        "border": "#2e2e36",
        "text": "#d6d6dc",
        "text_dim": "#8a8a94",
        "accent": "#4f8cff",
        "ok": "#2ecc8f",
        "warn": "#f0b429",
        "err": "#ef5350",
        "info": "#8ab4f8",
        "up": "#26a69a",
        "down": "#ef5350",
    }
    for key, value in expected.items():
        assert PALETTE[key] == value
    assert TONE["good"] == PALETTE["ok"]
    assert TONE["bad"] == PALETTE["err"]
    assert set(CHAT_TAGS) >= {"user", "assistant", "code", "error", "system"}
    assert set(LOG_TAGS) >= {"entry", "exit", "bar", "status"}
    assert DOT_COLORS["ok"] == PALETTE["ok"]
    assert "ep_long" in EPISODE_TAGS


def test_labels_import_seam_titles_not_redefined():
    """Seam titles must be the headless.py objects, not a second literal."""
    assert labels.TITLE_STRATEGY_CHANGE is TITLE_STRATEGY_CHANGE
    assert labels.TITLE_EXISTING_POSITION is TITLE_EXISTING_POSITION
    assert labels.TITLE_DATA_RANGE is TITLE_DATA_RANGE
    assert labels.DEPLOY_BOT == "部署機器人 Deploy Bot"
    assert labels.READY == "就緒 Ready"


def test_ui_package_does_not_import_run_backtest():
    import src.ui.theme as theme_mod
    import src.ui.widgets as widgets_mod
    import src.ui.labels as labels_mod
    for mod in (theme_mod, widgets_mod, labels_mod):
        assert "run_backtest" not in getattr(mod, "__dict__", {})
        src = open(mod.__file__, encoding="utf-8").read()
        assert "import run_backtest" not in src
        assert "from run_backtest" not in src


def test_status_level_styles_map():
    """set_status levels → named ttk styles (chunk 2)."""
    assert STATUS_LEVEL_STYLES["info"] == "Dim.TLabel"
    assert STATUS_LEVEL_STYLES["ok"] == "Status.Ok.TLabel"
    assert STATUS_LEVEL_STYLES["warn"] == "Status.Warn.TLabel"
    assert STATUS_LEVEL_STYLES["error"] == "Status.Err.TLabel"


# ── Tk tests ──

@_tk_skip
def test_init_theme_on_withdrawn_root_defines_named_styles():
    import tkinter as tk
    from tkinter import ttk
    from src.ui.theme import FONTS, init_theme

    root = tk.Tk()
    root.withdraw()
    try:
        style = init_theme(root)
        assert isinstance(style, ttk.Style)
        for name in (
            "Card.TFrame", "CardValue.TLabel",
            "Status.Ok.TLabel", "Status.Warn.TLabel", "Status.Err.TLabel",
            "Dim.TLabel",
        ):
            # lookup returns '' for unknown options on a missing style;
            # a configured style has a background or foreground.
            fg = style.lookup(name, "foreground")
            bg = style.lookup(name, "background")
            assert fg or bg, f"named style {name} was not configured"
        assert set(FONTS) >= {"title", "section", "value", "body", "mono", "mono_small"}
    finally:
        root.destroy()


@_tk_skip
def test_headless_bot_app_constructs_with_theme():
    """Headless invariant: every widget builds on a withdrawn root."""
    import tkinter as tk
    from src.live.headless_app import HeadlessBotApp

    root = tk.Tk()
    root.withdraw()
    try:
        app = HeadlessBotApp(root)
        assert app.root is root
        assert hasattr(app, "set_status")
        assert hasattr(app, "log_text")
        assert hasattr(app, "_conn_dot")
        app.set_status("hello", "info")
        assert app.status_var.get() == "hello"
        assert app._status_msg_label.cget("style") == "Dim.TLabel"
        app.set_status("boom", "error")
        assert app._status_msg_label.cget("style") == "Status.Err.TLabel"
        assert len(app._status_history) >= 2
    finally:
        try:
            root.destroy()
        except Exception:
            pass


@_tk_skip
def test_tag_text_log_cap_trims_from_top():
    import tkinter as tk
    from src.ui.theme import init_theme
    from src.ui.widgets import TagTextLog

    root = tk.Tk()
    root.withdraw()
    try:
        init_theme(root)
        log = TagTextLog(root, max_lines=5, show_toolbar=True)
        for i in range(8):
            log.append(f"line-{i}", "info")
        content = log.get("1.0", "end-1c").strip().splitlines()
        assert content[0] == "line-3"
        assert content[-1] == "line-7"
        assert len(content) == 5
    finally:
        root.destroy()


@_tk_skip
def test_tag_text_log_append_from_non_main_thread_raises():
    """Producers must go through _ui_queue — documented by this raise."""
    import tkinter as tk
    from src.ui.theme import init_theme
    from src.ui.widgets import TagTextLog

    root = tk.Tk()
    root.withdraw()
    try:
        init_theme(root)
        log = TagTextLog(root)
        errors: list[BaseException] = []

        def worker():
            try:
                log.append("from-worker")
            except RuntimeError as exc:
                errors.append(exc)

        t = threading.Thread(target=worker)
        t.start()
        t.join(timeout=5)
        assert errors, "off-thread append must raise RuntimeError"
        assert "Tk thread" in str(errors[0])
    finally:
        root.destroy()


@_tk_skip
def test_status_dot_states():
    import tkinter as tk
    from src.ui.theme import init_theme
    from src.ui.widgets import StatusDot

    root = tk.Tk()
    root.withdraw()
    try:
        init_theme(root)
        dot = StatusDot(root, text="conn")
        dot.set_state("ok")
        assert "●" in str(dot.cget("text"))
        assert dot.cget("style") == "Status.Ok.TLabel"
        dot.set_state("warn")
        assert dot.cget("style") == "Status.Warn.TLabel"
        dot.set_state("err")
        assert dot.cget("style") == "Status.Err.TLabel"
        dot.set_state("off")
        assert dot.cget("style") == "Dim.TLabel"
    finally:
        root.destroy()


def test_spec_lists_src_ui_hiddenimports():
    spec = open("tai_backtest.spec", encoding="utf-8").read()
    for name in ("src.ui", "src.ui.theme", "src.ui.widgets", "src.ui.labels"):
        assert f"'{name}'" in spec


def test_phase1_run_backtest_source_contracts():
    """Fail-against-pre-fix: the Phase 1 glue changes must be present."""
    import inspect
    import run_backtest as rb

    src = open(rb.__file__, encoding="utf-8").read()
    assert "def _attach_tooltip(" not in src
    assert "def _on_strategy_changed(" not in src
    assert "self.root.update()" not in inspect.getsource(rb.BacktestApp._fetch_tradingview_live)
    assert "threading.Thread" in inspect.getsource(rb.BacktestApp._fetch_tradingview_live)
    deploy_src = inspect.getsource(rb.BacktestApp._deploy_live_from)
    assert "self.root.update_idletasks" not in deploy_src
    assert "time.sleep(" not in deploy_src
    assert "_oi_wait_must_block" in deploy_src
    assert TITLE_EXISTING_POSITION in deploy_src
    assert TITLE_STRATEGY_CHANGE in deploy_src
    assert "return False" in deploy_src
    assert "messagebox." not in deploy_src
    assert "def set_status(" in src
    assert "transient_start" in inspect.getsource(rb.BacktestApp._append_chat)
    assert "transient_start" in inspect.getsource(rb.BacktestApp._remove_last_system_line)
    assert "_rendered_trade_count" in inspect.getsource(rb.BacktestApp._display_results)
    assert "_flush_report_render" in inspect.getsource(rb.BacktestApp._render_report_view)
    assert "log_ui" in inspect.getsource(rb._route_to_log_widget)
    assert "def _deploy_live_continue(" in src
    # Order-confirm dialog stays non-modal.
    confirm_src = inspect.getsource(rb.BacktestApp._show_order_confirm_dialog)
    assert "dlg.grab_set" not in confirm_src
    assert "countdown" in confirm_src
