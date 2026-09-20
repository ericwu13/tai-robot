"""Tk workbench UI infrastructure (theme, widgets, bilingual labels).

Phase 1 of the UI modernization plan. This package must not import
``run_backtest`` — the dependency is one-way so ``HeadlessBotApp``
construction cannot cycle.
"""
