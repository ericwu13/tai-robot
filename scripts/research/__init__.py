"""Headless strategy-research harness (see .claude/skills/strategy-research).

data.py  — bar sources: bot-dir 1-min CSVs, dashboard /api/bars, TradingView
bt.py    — GUI-equivalent backtest (next_open fill) + stats / folds / holdout
chat.py  — GUI-equivalent AI chat → Generate Strategy → AI Review loop
"""
