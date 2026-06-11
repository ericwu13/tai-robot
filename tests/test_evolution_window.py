"""Tests for the Bot Evolution trade window: which trades get re-sent.

Already-analyzed trades (below the watermark) are EXCLUDED from the
evolution payload — only new trades since the last run are sent.
"""

import run_backtest as rb


class TestEvolutionOmittedCount:
    def test_no_watermark_sends_everything(self):
        assert rb._evolution_omitted_count(65, None) == 0

    def test_partial_watermark_omits_covered_prefix(self):
        wm = {"trade_count": 40, "at": "2026-06-06 05:00:00"}
        assert rb._evolution_omitted_count(65, wm) == 40

    def test_watermark_at_total_means_no_new_trades(self):
        wm = {"trade_count": 65, "at": "2026-06-13 05:00:00"}
        assert rb._evolution_omitted_count(65, wm) == 65

    def test_stale_watermark_clamped_to_total(self):
        # Session reset reusing the same bot dir: watermark larger than
        # the current trade list must clamp, never go negative.
        wm = {"trade_count": 100, "at": "2026-06-13 05:00:00"}
        assert rb._evolution_omitted_count(65, wm) == 65

    def test_malformed_watermark_treated_as_none(self):
        assert rb._evolution_omitted_count(65, {"trade_count": None}) == 0
        assert rb._evolution_omitted_count(65, {}) == 0
