"""Tests for technical analysis indicators."""

import math

from src.strategy.indicators.ma import sma, ema
from src.strategy.indicators.rsi import rsi
from src.strategy.indicators.macd import macd
from src.strategy.indicators.bollinger import bollinger_bands
from src.strategy.indicators.adx import adx, plus_di, minus_di
from src.strategy.indicators.stochastic import stochastic


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


class TestADX:
    """Tests for ADX, +DI, -DI indicators."""

    def _trending_data(self, n=50):
        """Generate strong uptrend data for testing."""
        highs = [100 + i * 3 for i in range(n)]
        lows = [95 + i * 3 for i in range(n)]
        closes = [98 + i * 3 for i in range(n)]
        return highs, lows, closes

    def _sideways_data(self, n=50):
        """Generate sideways/choppy data."""
        highs = [105 + ((-1) ** i) * 2 for i in range(n)]
        lows = [95 + ((-1) ** i) * 2 for i in range(n)]
        closes = [100 + ((-1) ** i) * 1 for i in range(n)]
        return highs, lows, closes

    def test_insufficient_data(self):
        highs = list(range(10))
        lows = list(range(10))
        closes = list(range(10))
        assert adx(highs, lows, closes, 14) is None

    def test_returns_float_in_range(self):
        highs, lows, closes = self._trending_data()
        result = adx(highs, lows, closes, 14)
        assert result is not None
        assert 0 <= result <= 100

    def test_strong_trend_high_adx(self):
        highs, lows, closes = self._trending_data(n=60)
        result = adx(highs, lows, closes, 14)
        assert result is not None
        assert result > 25  # Strong trend should produce high ADX

    def test_sideways_lower_adx(self):
        highs, lows, closes = self._sideways_data(n=60)
        trending_adx = adx(*self._trending_data(n=60), 14)
        sideways_adx = adx(highs, lows, closes, 14)
        assert trending_adx is not None and sideways_adx is not None
        assert trending_adx > sideways_adx

    def test_plus_di_minus_di(self):
        highs, lows, closes = self._trending_data()
        pdi = plus_di(highs, lows, closes, 14)
        mdi = minus_di(highs, lows, closes, 14)
        assert pdi is not None and mdi is not None
        assert 0 <= pdi <= 100
        assert 0 <= mdi <= 100

    def test_uptrend_plus_di_greater(self):
        highs, lows, closes = self._trending_data(n=60)
        pdi = plus_di(highs, lows, closes, 14)
        mdi = minus_di(highs, lows, closes, 14)
        assert pdi is not None and mdi is not None
        assert pdi > mdi  # In uptrend, +DI should exceed -DI


class TestStochastic:
    """Tests for Stochastic Oscillator."""

    def test_insufficient_data(self):
        assert stochastic([1, 2], [1, 2], [1, 2], k_period=14, d_period=3) is None

    def test_returns_tuple(self):
        n = 20
        highs = [100 + i for i in range(n)]
        lows = [90 + i for i in range(n)]
        closes = [95 + i for i in range(n)]
        result = stochastic(highs, lows, closes, k_period=14, d_period=3)
        assert result is not None
        k_val, d_val = result
        assert isinstance(k_val, float)
        assert isinstance(d_val, float)

    def test_range_0_100(self):
        n = 20
        highs = [100 + i for i in range(n)]
        lows = [90 + i for i in range(n)]
        closes = [95 + i for i in range(n)]
        result = stochastic(highs, lows, closes, k_period=14, d_period=3)
        assert result is not None
        k_val, d_val = result
        assert 0 <= k_val <= 100
        assert 0 <= d_val <= 100

    def test_at_highest_high(self):
        """When close == highest high, %K should be 100."""
        n = 20
        highs = [100] * n
        lows = [90] * n
        closes = [95] * (n - 1) + [100]  # Last close at highest high
        result = stochastic(highs, lows, closes, k_period=14, d_period=1)
        assert result is not None
        assert result[0] == 100.0

    def test_at_lowest_low(self):
        """When close == lowest low, %K should be 0."""
        n = 20
        highs = [100] * n
        lows = [90] * n
        closes = [95] * (n - 1) + [90]  # Last close at lowest low
        result = stochastic(highs, lows, closes, k_period=14, d_period=1)
        assert result is not None
        assert result[0] == 0.0

    def test_zero_range(self):
        """When high == low (no range), should return 50."""
        n = 20
        highs = [100] * n
        lows = [100] * n
        closes = [100] * n
        result = stochastic(highs, lows, closes, k_period=14, d_period=3)
        assert result is not None
        assert result == (50.0, 50.0)
