"""D3: evolution PASS changelog entries carry params_before / params_after.

Knob path: applied knobs merged onto the baseline grid defaults.
Codegen path: public class-level literals, or None plus params_reason
when the source cannot be parsed. These tests fail before the helper
exists and before the worker passes the three kwargs.
"""

from __future__ import annotations

import inspect
import textwrap

import run_backtest as rb
from src.daily_report.changelog import append_changelog, load_changelog
from src.evolution.changelog_params import evolution_changelog_params
from src.evolution.knobs import DYNAMIC_EXIT_PULLBACK_GRID, read_strategy_knobs


def _use_changelog(monkeypatch, tmp_path):
    import src.daily_report.changelog as cl
    monkeypatch.setattr(cl, "_CHANGELOG_PATH", tmp_path / "changelog.json")


def _knob_probe():
    """No-arg class exposing every allowlisted knob. Not a live bot."""
    defaults = {}
    for key, options in DYNAMIC_EXIT_PULLBACK_GRID.items():
        defaults[key] = options[0]

    class DynamicExitPullbackProbe:
        ema_period = 999  # class-level decoy; knob path reads the instance

        def __init__(self, **kwargs):
            for key, default in defaults.items():
                setattr(self, key, kwargs.get(key, default))

    return DynamicExitPullbackProbe, defaults


def _record(**kwargs):
    before, after, reason = evolution_changelog_params(**kwargs)
    entry = append_changelog(
        strategy_name="Probe",
        version_before="Base",
        version_after="Cand",
        change_summary="pool-validated only",
        initiated_by="ai",
        params_before=before,
        params_after=after,
        params_reason=reason,
    )
    return entry


_BASE_SOURCE = """
class Alpha(BacktestStrategy):
    kline_type = 0
    kline_minute = 15
    ema_period = 50
    htf_intervals = [3600]
    sl_offset = -20
    _warmup = 3

    def __init__(self, **kwargs):
        self.atr_period = kwargs.get("atr_period", 14)
"""

_CAND_SOURCE = """
class AlphaEvo1(BacktestStrategy):
    kline_type = 0
    kline_minute = 15
    ema_period: int = 70
    htf_intervals = [3600]
    sl_offset = -20
    _warmup = 9

    def __init__(self, **kwargs):
        self.atr_period = kwargs.get("atr_period", 21)
"""


class TestEvolutionChangelogParams:
    def test_knob_path_pass_entry_has_params(self, tmp_path, monkeypatch):
        _use_changelog(monkeypatch, tmp_path)
        cls, defaults = _knob_probe()
        # Instance defaults, not the class-body decoy and not only the
        # changed key. Pick an allowlisted value other than the default.
        base_ema = defaults["ema_period"]
        applied_ema = next(
            v for v in DYNAMIC_EXIT_PULLBACK_GRID["ema_period"] if v != base_ema)
        applied = {"ema_period": applied_ema}

        entry = _record(
            knob_apply=applied,
            baseline_cls=cls,
            baseline_source=_BASE_SOURCE,
            candidate_source=_CAND_SOURCE,
            candidate_name="AlphaEvo1",
        )

        assert entry["params_before"] == read_strategy_knobs(
            cls, DYNAMIC_EXIT_PULLBACK_GRID)
        assert entry["params_before"]["ema_period"] == base_ema
        assert entry["params_before"]["ema_period"] != 999
        expected_after = dict(entry["params_before"])
        expected_after.update(applied)
        assert entry["params_after"] == expected_after
        assert set(entry["params_after"]) == set(DYNAMIC_EXIT_PULLBACK_GRID)
        assert "params_reason" not in entry
        # Class-level literals and the candidate's kwargs.get defaults
        # must not leak onto the knob snapshot.
        assert "kline_minute" not in entry["params_before"]
        assert entry["params_after"]["atr_period"] == defaults["atr_period"]

        loaded = load_changelog()
        assert loaded[0]["params_before"]
        assert loaded[0]["params_after"]["ema_period"] == applied_ema

    def test_codegen_path_pass_entry_has_params(self, tmp_path, monkeypatch):
        _use_changelog(monkeypatch, tmp_path)
        entry = _record(
            knob_apply=None,
            baseline_cls=None,
            baseline_source=_BASE_SOURCE,
            candidate_source=_CAND_SOURCE,
            candidate_name="AlphaEvo1",
            baseline_name="Alpha",
        )

        assert entry["params_before"]["ema_period"] == 50
        assert entry["params_after"]["ema_period"] == 70
        assert entry["params_before"]["kline_minute"] == 15
        assert entry["params_after"]["htf_intervals"] == [3600]
        assert entry["params_before"]["sl_offset"] == -20
        # Method-body kwargs.get defaults and private class attrs stay out.
        assert "atr_period" not in entry["params_before"]
        assert "atr_period" not in entry["params_after"]
        assert "_warmup" not in entry["params_before"]
        assert "_warmup" not in entry["params_after"]
        assert "params_reason" not in entry

        loaded = load_changelog()
        assert loaded[0]["params_before"]["ema_period"] == 50
        assert loaded[0]["params_after"]["ema_period"] == 70

    def test_unparseable_codegen_is_none_with_reason(self, tmp_path, monkeypatch):
        _use_changelog(monkeypatch, tmp_path)
        entry = _record(
            knob_apply=None,
            baseline_cls=None,
            baseline_source="class Alpha(:\n    ema_period = 50\n",
            candidate_source="this is not python {{{",
            candidate_name="AlphaEvo1",
            baseline_name="Alpha",
        )

        assert entry["params_before"] is None
        assert entry["params_after"] is None
        assert entry["params_reason"]
        assert "unparseable baseline" in entry["params_reason"]
        assert "unparseable candidate" in entry["params_reason"]
        assert "syntax error" in entry["params_reason"]

        loaded = load_changelog()
        assert loaded[0]["params_before"] is None
        assert loaded[0]["params_after"] is None
        assert loaded[0]["params_reason"] == entry["params_reason"]

    def test_nonliteral_class_body_is_none_with_reason(self, tmp_path, monkeypatch):
        _use_changelog(monkeypatch, tmp_path)
        source = (
            "class Alpha(BacktestStrategy):\n"
            "    ema_period = compute()\n"
        )
        entry = _record(
            knob_apply={},
            baseline_cls=None,
            baseline_source=source,
            candidate_source=source.replace("class Alpha", "class AlphaEvo1"),
            candidate_name="AlphaEvo1",
            baseline_name="Alpha",
        )
        assert entry["params_before"] is None
        assert entry["params_after"] is None
        assert "not literals" in entry["params_reason"]
        assert "ema_period" in entry["params_reason"]

    def test_worker_passes_params_without_touching_verdict_text(self):
        src = textwrap.dedent(inspect.getsource(
            rb.BacktestApp._start_evolution_pipeline))
        call = src.index("append_changelog(")
        window = src[call:src.index("EVO changelog append failed")]
        assert "params_before=params_before" in window
        assert "params_after=params_after" in window
        assert "params_reason=params_reason" in window
        assert "evolution_changelog_params(" in src
        # Verdict combination and the Discord payload are still the same call.
        assert "verdict = combine_evolution_verdicts(" in src
        assert "evolution_verdict(verdict.passed, block)" in src
        assert src.index("combine_evolution_verdicts(") < src.index(
            "append_changelog(")


class TestChangelogReasonIsAdditive:
    def test_existing_writer_omits_reason_key(self, tmp_path, monkeypatch):
        _use_changelog(monkeypatch, tmp_path)
        entry = append_changelog(
            strategy_name="SMA Cross",
            version_before="1.0",
            version_after="1.1",
            change_summary="Increased fast SMA from 3 to 5",
            initiated_by="human",
            params_before={"fast": 3},
            params_after={"fast": 5},
        )
        assert "params_reason" not in entry
        assert entry["params_before"] == {"fast": 3}
        assert load_changelog()[0]["params_after"] == {"fast": 5}
        assert "params_reason" not in load_changelog()[0]
