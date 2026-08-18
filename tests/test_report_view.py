"""Tests for src/backtest/report_view.py — the Report tab view model.

The tab must show the same numbers format_report prints; these tests
pin the shaping (tones, INF profit factor, real subset recompute,
per-strategy split) so the Tk renderer stays a dumb loop.
"""

from src.backtest.broker import OrderSide, Trade
from src.backtest.metrics import calculate_metrics
from src.backtest.report_view import (
    headline_cards,
    per_strategy_rows,
    real_subset,
    stat_sections,
)


def _trade(pnl, source="", strategy=""):
    return Trade(tag="L", side=OrderSide.LONG, qty=1,
                 entry_price=22000, exit_price=22000 + pnl,
                 entry_bar_index=0, exit_bar_index=1, pnl=pnl,
                 source=source, strategy=strategy)


def _metrics(pnls, balance=1_000_000):
    trades = [_trade(p) for p in pnls]
    eq, cum = [], 0
    for p in pnls:
        cum += p
        eq.append(cum)
    return calculate_metrics(trades, eq, initial_balance=balance)


# ── headline cards ───────────────────────────────────────────────────────

def test_headline_cards_tones_and_values():
    cards = headline_cards(_metrics([500, -200, 300]))
    by_label = {c.label: c for c in cards}
    pnl = by_label["總損益 Total P&L"]
    assert pnl.value == "+600" and pnl.tone == "good"
    wr = by_label["勝率 Win Rate"]
    assert wr.value == "66.7%" and "2W / 1L" in wr.sub
    assert by_label["最大回撤 Max Drawdown"].tone == "bad"


def test_headline_negative_pnl_is_bad():
    cards = headline_cards(_metrics([-500, -200]))
    pnl = next(c for c in cards if "Total P&L" in c.label)
    assert pnl.value == "-700" and pnl.tone == "bad"


def test_profit_factor_inf_rendered():
    cards = headline_cards(_metrics([500, 300]))     # no losses → INF
    pf = next(c for c in cards if "Profit Factor" in c.label)
    assert pf.value == "INF" and pf.tone == "good"


def test_losing_profit_factor_is_bad():
    cards = headline_cards(_metrics([100, -500]))
    pf = next(c for c in cards if "Profit Factor" in c.label)
    assert pf.tone == "bad"


def test_empty_result_neutral_not_crash():
    cards = headline_cards(_metrics([]))
    pnl = next(c for c in cards if "Total P&L" in c.label)
    assert pnl.tone == "neutral"


# ── sections ─────────────────────────────────────────────────────────────

def test_stat_sections_shape():
    sections = stat_sections(_metrics([500, -200]))
    titles = [t for t, _ in sections]
    assert titles == ["交易統計 Trade Stats", "風險 Risk"]
    for _, rows in sections:
        assert all(r.value for r in rows)


# ── real subset ──────────────────────────────────────────────────────────

def test_real_subset_none_without_real_trades():
    assert real_subset([_trade(100), _trade(-50)]) is None
    assert real_subset(None) is None


def test_real_subset_recomputes_on_real_only():
    trades = [_trade(500), _trade(-200, source="real"),
              _trade(300, source="real")]
    count, cards = real_subset(trades)
    assert count == 2
    pnl = next(c for c in cards if "Real P&L" in c.label)
    assert pnl.value == "+100"      # -200 + 300, simulated 500 excluded
    wr = next(c for c in cards if "Real WR" in c.label)
    assert "1W / 1L" in wr.sub


# ── per-strategy breakdown ───────────────────────────────────────────────

def test_per_strategy_none_for_single_strategy():
    assert per_strategy_rows([_trade(100, strategy="A")]) is None
    assert per_strategy_rows([]) is None


def test_per_strategy_split_and_order():
    trades = [_trade(500, strategy="AI: LongBot"),
              _trade(-200, strategy="AI: ShortBot"),
              _trade(300, strategy="AI: LongBot")]
    rows = per_strategy_rows(trades)
    assert [r[0] for r in rows] == ["AI: LongBot", "AI: ShortBot"]
    long_row = rows[0]
    assert long_row[1] == 2 and long_row[3] == 800
    assert rows[1][3] == -200


def test_per_strategy_unlabeled_bucket():
    rows = per_strategy_rows([_trade(100, strategy="A"), _trade(50)])
    assert any("unlabeled" in r[0] for r in rows)
