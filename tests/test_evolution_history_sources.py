"""Evo redesign phase 1b: history keeps the candidate source and the full verdict.

Before this change a history row kept only ``source_hash`` of the
incumbent and a 4,000-character ``reason``. The candidate's code was
not stored, so a FAIL could never be re-scored on later bars, and the
verdict block reached a human only through a Discord message cut at
~1,800 characters. Every test here fails on the pre-fix code.
"""

from __future__ import annotations

import hashlib
import os

import run_backtest as rb
from src.evolution.history import (
    SOURCES_DIRNAME,
    build_evolution_record,
    evolution_history_path,
    load_evolution_history,
    load_stored_source,
    record_evolution_attempt,
    store_source,
)
from tests.test_evolution_history import _HIST_BASE, _HIST_CAND, _run_validation


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_store_source_is_content_addressed_and_write_once(tmp_path):
    digest = store_source(tmp_path, "class A: pass\n")
    assert digest == _sha("class A: pass\n")
    path = tmp_path / SOURCES_DIRNAME / f"{digest}.py"
    assert path.read_text(encoding="utf-8") == "class A: pass\n"
    mtime = os.stat(path).st_mtime_ns
    assert store_source(tmp_path, "class A: pass\n") == digest
    assert os.stat(path).st_mtime_ns == mtime  # not rewritten
    assert load_stored_source(tmp_path, digest) == "class A: pass\n"
    assert load_stored_source(tmp_path, "0" * 64) is None
    assert load_stored_source(tmp_path, "../evil") is None


def test_store_source_skips_empty_and_missing_bot_dir(tmp_path):
    assert store_source(tmp_path, "") == ""
    missing = tmp_path / "nope"
    assert store_source(missing, "x = 1\n") == ""
    assert not missing.exists()


def test_record_stores_baseline_and_candidate_sources(tmp_path):
    ok = record_evolution_attempt(
        tmp_path, bot="B", class_name="Base", source=_HIST_BASE,
        candidate_source=_HIST_CAND, candidate_name="HistBaseEvo1",
        final_status="fail", reason_kind="validation", reason="short",
        verdict_text="V" * 9000 + "TAIL")
    assert ok
    row = load_evolution_history(evolution_history_path(tmp_path))[0]
    assert row["source_hash"] == _sha(_HIST_BASE)
    assert row["candidate_source_hash"] == _sha(_HIST_CAND)
    assert row["candidate_class"] == "HistBaseEvo1"
    assert load_stored_source(tmp_path, row["source_hash"]) == _HIST_BASE
    assert load_stored_source(tmp_path, row["candidate_source_hash"]) == _HIST_CAND
    # The full block, not the 4,000-char reason cap.
    assert row["verdict_text"].endswith("TAIL")
    assert len(row["verdict_text"]) == 9004


def test_record_without_candidate_keeps_fields_empty(tmp_path):
    record = build_evolution_record(bot="B", final_status="skip")
    assert record["candidate_source_hash"] == ""
    assert record["candidate_class"] == ""
    assert record["verdict_text"] == ""


def test_worker_validation_row_carries_candidate_source_and_full_block(
        tmp_path, monkeypatch):
    import src.evolution.pipeline as pipeline

    tail = "LAST-LINE-ONLY-IN-THE-FULL-BLOCK"
    long_block = "verdict line\n" * 600 + tail  # ~7.8k chars
    monkeypatch.setattr(
        pipeline, "format_weekend_verdict_block",
        lambda *args, **kwargs: long_block)
    app, rows = _run_validation(
        monkeypatch, tmp_path, paper_gate="relative", passed=False)
    assert app.errors == [], app.chat
    row = rows[0]
    assert row["reason_kind"] == "validation"
    assert row["candidate_class"] == "HistBaseEvo1"
    stored = load_stored_source(tmp_path, row["candidate_source_hash"])
    assert stored is not None and "class HistBaseEvo1" in stored
    assert load_stored_source(tmp_path, row["source_hash"]) == _HIST_BASE
    assert tail in row["verdict_text"]


def test_worker_early_exit_rows_carry_their_message_as_verdict_text(
        tmp_path, monkeypatch):
    """The grid no-pick exit has no candidate; its message is the verdict."""
    from tests.test_evolution_grid_no_pick_retire import _run_no_pick

    app, rows, *_ = _run_no_pick(monkeypatch, tmp_path)
    row = rows[0]
    assert row["reason_kind"] == "grid_no_pick"
    assert "Grid selected no setting" in row["verdict_text"]
    assert row["candidate_source_hash"] == ""
