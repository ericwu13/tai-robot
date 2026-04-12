"""Daily report pipeline: trade reports, market regime classification, strategy changelog."""

from .regime_classifier import classify_regime, RegimeResult
from .report_generator import generate_daily_report, generate_report_from_backtest, generate_session_report
from .changelog import append_changelog, load_changelog, recent_changes
