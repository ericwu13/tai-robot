"""Tests for technical analysis indicators."""

import math

from src.strategy.indicators.ma import sma, ema
from src.strategy.indicators.rsi import rsi
from src.strategy.indicators.macd import macd
from src.strategy.indicators.bollinger import bollinger_bands


class TestSMA:
    def test_basic(self):
        assert sma([1, 2, 3, 4, 5], 3) == 4.0  # (3+4+5)/3

    def test_insufficient_data(self):
        assert sma([1, 2], 3) is None

    def test_exact_length(self):
        assert sma([10, 20, 30], 3) == 20.0

    def test_single_value(self):
        assert sma([42], 1) == 42.0


class TestEMA:
    def test_basic(self):
        values = [22, 22.27, 22.19, 22.08, 22.17, 22.18, 22.13, 22.23, 22.43, 22.24]
        result = ema(values, 5)
        assert result is not None
        assert abs(result - 22.247) < 0.1

    def test_insufficient_data(self):
        assert ema([1, 2], 5) is None

    def test_matches_sma_for_exact_period(self):
        values = [10, 20, 30, 40, 50]
        # EMA seeded with SMA over first 5 values, no further smoothing needed
        result = ema(values, 5)
        assert result == sma(values, 5)

    def test_weights_recent_more(self):
        values = [100, 100, 100, 100, 100, 200]
        result = ema(values, 5)
        # EMA should be closer to 200 than SMA would be
        assert result > sma(values, 5)


class TestRSI:
    def test_all_gains(self):
        values = list(range(1, 20))
        result = rsi(values, 14)
        assert result == 100.0

    def test_all_losses(self):
        values = list(range(20, 1, -1))
        result = rsi(values, 14)
        assert result is not None
        assert result < 1.0  # Essentially 0

    def test_insufficient_data(self):
        assert rsi([1, 2, 3], 14) is None

    def test_range(self):
        # Mixed gains and losses
        values = [44, 44.34, 44.09, 43.61, 44.33, 44.83, 45.10,
                  45.42, 45.84, 46.08, 45.89, 46.03, 45.61, 46.28, 46.28, 46.00]
        result = rsi(values, 14)
        assert result is not None
        assert 0 <= result <= 100


class TestMACD:
    def test_insufficient_data(self):
        values = list(range(20))
        assert macd(values) is None  # needs 26+9-1=34

    def test_basic_output(self):
        # Generate enough data points
        values = [100 + i * 0.5 for i in range(50)]
        result = macd(values)
        assert result is not None
        macd_line, signal_line, histogram = result
        assert isinstance(macd_line, float)
        assert isinstance(signal_line, float)
        assert abs(histogram - (macd_line - signal_line)) < 1e-10

    def test_uptrend_positive_macd(self):
        # Strong uptrend should produce positive MACD
        values = [100 + i * 2 for i in range(50)]
        result = macd(values)
        assert result is not None
        assert result[0] > 0  # macd_line positive in uptrend


class TestBollingerBands:
    def test_basic(self):
        values = [20] * 20
        result = bollinger_bands(values, 20, 2.0)
        assert result is not None
        upper, middle, lower = result
        assert middle == 20.0
        assert upper == 20.0  # no volatility
        assert lower == 20.0

    def test_with_volatility(self):
        values = list(range(1, 21))
        result = bollinger_bands(values, 20, 2.0)
        assert result is not None
        upper, middle, lower = result
        assert upper > middle > lower

    def test_insufficient_data(self):
        assert bollinger_bands([1, 2, 3], 20) is None

    def test_symmetric(self):
        values = [100 + ((-1) ** i) * 5 for i in range(20)]
        result = bollinger_bands(values, 20, 2.0)
        assert result is not None
        upper, middle, lower = result
        assert abs((upper - middle) - (middle - lower)) < 1e-10
