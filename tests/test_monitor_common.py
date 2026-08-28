"""Tests for scripts/monitor/common.py — redaction, grading, log parsing.

These are the primitives every check leans on: if ``redact`` leaks a
webhook or ``verdict`` mis-grades, every downstream report is wrong.
"""

from datetime import datetime, timedelta

from scripts.monitor.common import (
    TZ_TPE, Finding, first_ts_in_line, last_tpe_timestamp, read_lock_pid,
    redact, resolve_news_path, setting, verdict,
)

NOW = datetime(2026, 8, 25, 6, 0, tzinfo=TZ_TPE)


# ── redaction ───────────────────────────────────────────────────────────

def test_redact_masks_discord_webhook():
    text = ("Discord send failed: https://discord.com/api/webhooks/"
            "1234567890123456789/AbCdEfGhIjKlMnOpQrStUvWxYz-0123456789 (403)")
    out = redact(text)
    assert "<webhook>" in out
    assert "webhooks/1234567890123456789" not in out
    assert "AbCdEfGhIjKlMnOpQrStUvWxYz" not in out


def test_redact_masks_long_token_runs():
    token = "A" * 30 + "-" + "b9" * 15
    assert len(token) >= 50
    out = redact(f"authorization Bot {token}")
    assert token not in out
    assert "<token>" in out


def test_redact_masks_vendor_keys():
    out = redact("key=sk-ant-api03-verysecretvalue and AIzaSyABCDEFGHIJ")
    assert "sk-ant-api03-verysecretvalue" not in out
    assert "AIzaSyABCDEFGHIJ" not in out
    assert out.count("<key>") == 2


def test_redact_leaves_ordinary_text_alone():
    text = "W4 voteless for 5 consecutive passes (no TAIFEX data)"
    assert redact(text) == text


# ── grading ─────────────────────────────────────────────────────────────

def test_verdict_precedence():
    p1 = Finding("P1", "bots", "dead")
    p2 = Finding("P2", "bridge", "stale")
    p3 = Finding("P3", "bots", "idle")
    assert verdict([]) == "GREEN"
    assert verdict([p3]) == "GREEN"
    assert verdict([p3, p2]) == "YELLOW"
    assert verdict([p3, p2, p1]) == "RED"
    assert verdict([p1]) == "RED"


# ── log timestamp parsing ───────────────────────────────────────────────

def test_first_ts_in_line_prefers_the_tpe_clock():
    """Debug lines carry TPE first, local second — never take the last."""
    line = ("[2026-08-21 14:29:48.799 TPE / 2026-08-20 23:29:48.799 local] "
            "策略 strategy tick")
    ts = first_ts_in_line(line)
    assert ts == datetime(2026, 8, 21, 14, 29, 48, tzinfo=TZ_TPE)


def test_first_ts_in_line_handles_single_clock_and_prefixed_lines():
    assert first_ts_in_line("[2026-08-21 14:29:48.799] hello") == \
        datetime(2026, 8, 21, 14, 29, 48, tzinfo=TZ_TPE)
    assert first_ts_in_line("[LIVE] [2026-08-21 14:30:00.000 TPE] hi") == \
        datetime(2026, 8, 21, 14, 30, 0, tzinfo=TZ_TPE)
    assert first_ts_in_line("2026-08-26 14:40:00 TPE | fresh 5/11") == \
        datetime(2026, 8, 26, 14, 40, 0, tzinfo=TZ_TPE)
    assert first_ts_in_line("no timestamp here") is None


def test_last_tpe_timestamp_takes_the_last_line_with_a_stamp():
    text = "\n".join([
        "[2026-08-21 14:29:48.799 TPE / 2026-08-20 23:29:48.799 local] a",
        "[2026-08-21 14:31:02.100 TPE / 2026-08-20 23:31:02.100 local] b",
        "continuation line with no stamp",
    ])
    assert last_tpe_timestamp(text) == datetime(2026, 8, 21, 14, 31, 2,
                                                tzinfo=TZ_TPE)
    assert last_tpe_timestamp("") is None


# ── settings helpers ────────────────────────────────────────────────────

def test_setting_dotted_lookup():
    cfg = {"news": {"signal_path": "C:/x/signal.json", "empty": None}}
    assert setting(cfg, "news.signal_path") == "C:/x/signal.json"
    assert setting(cfg, "news.empty") == ""
    assert setting(cfg, "news.missing", default="fallback") == "fallback"
    assert setting(cfg, "nope.nope") == ""


def test_resolve_news_path(tmp_path):
    assert resolve_news_path("", str(tmp_path)) == ""
    absolute = str(tmp_path / "signal.json")
    assert resolve_news_path(absolute, str(tmp_path)) == absolute
    rel = resolve_news_path("data/signal.json", str(tmp_path))
    assert rel.startswith(str(tmp_path))


# ── lock reading is READ-ONLY ───────────────────────────────────────────

def test_read_lock_pid_never_deletes_a_stale_lock(tmp_path):
    """LiveRunner.check_lock deletes stale locks; the monitor must not."""
    lock = tmp_path / ".lock"
    lock.write_text("4242", encoding="utf-8")
    assert read_lock_pid(str(tmp_path)) == (True, 4242)
    assert lock.exists()

    lock.write_text("not-a-pid", encoding="utf-8")
    assert read_lock_pid(str(tmp_path)) == (True, None)
    assert lock.exists()

    lock.unlink()
    assert read_lock_pid(str(tmp_path)) == (False, None)


def test_now_is_never_naive():
    """Guard against comparing a TPE log stamp with a naive local clock."""
    assert NOW.tzinfo is not None
    assert NOW.utcoffset() == timedelta(hours=8)
