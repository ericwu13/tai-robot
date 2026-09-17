"""Monte Carlo must discover params on ``**kwargs`` strategies.

``_numeric_param_defaults`` used to read only ``inspect.signature``
defaults. AI-generated strategies (and
``DynamicExitPullbackStrategyV2``) take ``def __init__(self, **kwargs)``
and set params via ``kwargs.get("x", default)``, so the signature has
no numeric defaults, discovery returned ``{}``, and
``monte_carlo_robustness`` took the empty-defaults early return — a
no-op. #119/#120 only threaded ``capital_base`` into that path; they
did not fix discovery.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from dynamic_exit_pullback_strategy_v2 import DynamicExitPullbackStrategyV2
from src.backtest.strategy import BacktestStrategy
from src.evolution.evaluator import (
    MC_TRIALS,
    _numeric_param_defaults,
    monte_carlo_robustness,
)
from src.market_data.models import Bar


def _bars(n: int = 40) -> list[Bar]:
    dt = datetime(2026, 6, 1, 9, 0)
    out = []
    for i in range(n):
        c = 20000 + i
        out.append(Bar(
            symbol="TEST", dt=dt + timedelta(minutes=15 * i),
            open=c, high=c + 1, low=c - 1, close=c,
            volume=1, interval=900,
        ))
    return out


class _SignatureStrategy(BacktestStrategy):
    def __init__(self, period=20, mult=2.5, enabled=True):
        self.period = period
        self.mult = mult
        self.enabled = enabled

    def required_bars(self) -> int:
        return 1

    def on_bar(self, bar, data_store, broker) -> None:
        pass


class _KwargsGetStrategy(BacktestStrategy):
    seen: list[dict] = []

    def __init__(self, **kwargs):
        type(self).seen.append(dict(kwargs))
        self.ema_period = kwargs.get("ema_period", 50)
        self.atr_period = kwargs.get("atr_period", 14)
        self.atr_stop_mult = kwargs.get("atr_stop_mult", 2.5)
        self.atr_buffer_mult = kwargs.get("atr_buffer_mult", 0.5)
        self.tag = kwargs.get("tag", "Long")
        self.side = None
        self.trailing_stop_price = 0

    def required_bars(self) -> int:
        return 1

    def on_bar(self, bar, data_store, broker) -> None:
        pass


class _WrappedKwargsGetStrategy(BacktestStrategy):
    def __init__(self, **kwargs):
        self.bb_std = float(kwargs.get("bb_std", 2.0))
        self.period = int(kwargs.get("period", 20))
        self.offset = kwargs.get("offset", -1)

    def required_bars(self) -> int:
        return 1

    def on_bar(self, bar, data_store, broker) -> None:
        pass


class _MixedSignatureKwargsStrategy(BacktestStrategy):
    def __init__(self, period=10, **kwargs):
        self.period = period
        self.mult = kwargs.get("mult", 2.5)

    def required_bars(self) -> int:
        return 1

    def on_bar(self, bar, data_store, broker) -> None:
        pass


class _InstanceAttrStrategy(BacktestStrategy):
    """No signature defaults, no kwargs.get — attrs are the only source.

    kwargs still override via setattr so MC perturbations actually land.
    """

    def __init__(self, **kwargs):
        self.period = 20
        self.mult = 2.5
        self.trailing_stop_price = 0
        self.side = None
        for k, v in kwargs.items():
            setattr(self, k, v)

    def required_bars(self) -> int:
        return 1

    def on_bar(self, bar, data_store, broker) -> None:
        pass


class _NoParamsStrategy(BacktestStrategy):
    def __init__(self):
        self.side = None
        self.trailing_stop_price = 0

    def required_bars(self) -> int:
        return 1

    def on_bar(self, bar, data_store, broker) -> None:
        pass


# ── discovery ──


class TestNumericParamDefaultsSignature:
    def test_signature_defaults_still_discovered(self):
        """Regression: explicit ``__init__`` numeric defaults must keep working."""
        d = _numeric_param_defaults(_SignatureStrategy)
        assert d["period"] == 20.0
        assert d["mult"] == 2.5

    def test_bool_signature_defaults_excluded(self):
        d = _numeric_param_defaults(_SignatureStrategy)
        assert "enabled" not in d


class TestNumericParamDefaultsKwargsGet:
    def test_kwargs_get_defaults_discovered(self):
        d = _numeric_param_defaults(_KwargsGetStrategy)
        assert d["ema_period"] == 50.0
        assert d["atr_period"] == 14.0
        assert d["atr_stop_mult"] == 2.5
        assert d["atr_buffer_mult"] == 0.5

    def test_non_numeric_and_state_attrs_excluded(self):
        d = _numeric_param_defaults(_KwargsGetStrategy)
        assert "tag" not in d
        assert "side" not in d
        assert "trailing_stop_price" not in d

    def test_float_and_int_wrappers_and_unary_minus(self):
        d = _numeric_param_defaults(_WrappedKwargsGetStrategy)
        assert d["bb_std"] == 2.0
        assert d["period"] == 20.0
        assert d["offset"] == -1.0

    def test_mixed_signature_and_kwargs_get(self):
        d = _numeric_param_defaults(_MixedSignatureKwargsStrategy)
        assert d["period"] == 10.0
        assert d["mult"] == 2.5

    def test_dynamic_exit_pullback_v2_params_discovered(self):
        """The live kwargs-based strategy that made MC a no-op."""
        d = _numeric_param_defaults(DynamicExitPullbackStrategyV2)
        assert d["ema_period"] == 50.0
        assert d["atr_period"] == 14.0
        assert d["atr_stop_mult"] == 2.5
        assert d["atr_buffer_mult"] == 0.5
        assert "trailing_stop_price" not in d
        assert "side" not in d


class TestNumericParamDefaultsInstanceAttrs:
    def test_instance_numeric_attrs_discovered(self):
        d = _numeric_param_defaults(_InstanceAttrStrategy)
        assert d["period"] == 20.0
        assert d["mult"] == 2.5
        assert "trailing_stop_price" not in d
        assert "side" not in d

    def test_no_var_keyword_does_not_dump_state_as_kwargs(self):
        """A no-args ``__init__`` must not pick instance state as MC params —
        those names cannot be passed back without TypeError."""
        d = _numeric_param_defaults(_NoParamsStrategy)
        assert d == {}


# ── Monte Carlo actually runs ──


class TestMonteCarloUsesDiscoveredKwargs:
    def test_monte_carlo_instantiates_with_perturbed_kwargs(self):
        _KwargsGetStrategy.seen = []
        monte_carlo_robustness(_KwargsGetStrategy, _bars(), trials=MC_TRIALS, seed=42)
        assert len(_KwargsGetStrategy.seen) == MC_TRIALS
        for call in _KwargsGetStrategy.seen:
            assert "ema_period" in call
            assert "atr_stop_mult" in call
        # At least one trial must actually jitter off the defaults —
        # otherwise MC is still a silent no-op.
        jittered = [
            c for c in _KwargsGetStrategy.seen
            if c.get("ema_period") != 50 or c.get("atr_stop_mult") != 2.5
        ]
        assert jittered, _KwargsGetStrategy.seen

    def test_monte_carlo_still_noops_when_there_are_no_params(self):
        mean, cv = monte_carlo_robustness(_NoParamsStrategy, _bars(), trials=3)
        assert (mean, cv) == (0.0, 0.0)
