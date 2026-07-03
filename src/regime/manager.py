"""RegimeManager — Phase 1 shadow-mode orchestrator.

Driven from the live daily-report callback. On each NIGHT session it
reconstructs the higher-timeframe series from the bot's saved 1-min CSVs,
classifies the regime, advances the state machine, produces an advisory
recommendation, persists state + history, and (optionally) announces on
Discord. Nothing here places or modifies orders — it only logs and
recommends.
"""

import os
import logging

from src.daily_report.regime_classifier import classify_regime, RegimeResult
from src.live.live_runner import load_1m_bars_from_csvs
from src.live.bar_aggregator import aggregate_bars
from .state_machine import RegimeStateMachine, RegimeState, RegimeConfig
from .selector import StrategySelector
from .store import load_state, save_state, append_history, backfill_pnl

logger = logging.getLogger(__name__)


class RegimeManager:
    def __init__(self, bot_dir: str, cfg: RegimeConfig):
        self.bot_dir = bot_dir
        self.cfg = cfg
        self._state_path = os.path.join(bot_dir, "regime_state.json")
        self._hist_path = os.path.join(bot_dir, "regime_history.csv")
        self._state = load_state(self._state_path)
        self._machine = RegimeStateMachine()
        self._selector = StrategySelector()
        self._last_session_key: tuple | None = None
        self.discord_notify_cb = None   # set by run_backtest.py

    def reload_config(self, cfg: RegimeConfig):
        self.cfg = cfg

    def on_daily_report(self, report: dict):
        """Called from _on_live_daily_report (Tk thread) after every session report."""
        if not self.cfg.enabled:
            return
        session_key = (report.get("date", ""), report.get("session", ""))
        pnl = report.get("summary", {}).get("total_pnl", None)
        # Always backfill P&L for previous row
        if pnl is not None:
            try:
                backfill_pnl(self._hist_path, session_key[0], float(pnl),
                             int(report.get("summary", {}).get("total_trades", 0)))
            except Exception:
                pass

        # Only classify on NIGHT session and only once per session
        if session_key[1] != "NIGHT" or session_key == self._last_session_key:
            return
        self._last_session_key = session_key

        try:
            result = self._get_regime_result()
            if result is None:
                logger.warning("[REGIME] Could not compute regime — insufficient bars")
                return
            session_date = session_key[0]
            self._state = self._machine.step(self._state, result, self.cfg, session_date)
            rec = self._selector.select(self._state, self.cfg)
            save_state(self._state_path, self._state, rec, session_date)
            append_history(self._hist_path, session_date, self._state, rec)
            # Consume manual override (one-shot)
            if self._state.manual_override != "auto":
                self._state.manual_override = "auto"
                save_state(self._state_path, self._state, rec, session_date)
            self._notify_discord(rec, session_date)
            logger.info(f"[REGIME] {session_date}: {self._state.raw_regime} → {self._state.effective_regime} → {rec.action} ({rec.strategy_name})")
        except Exception as e:
            logger.exception(f"[REGIME] Error in on_daily_report: {e}")

    def _get_regime_result(self) -> RegimeResult | None:
        try:
            symbol = os.path.basename(self.bot_dir).split("_")[0]
            bars_1m = load_1m_bars_from_csvs(self.bot_dir, symbol)
            if not bars_1m:
                return None
            bars_htf = aggregate_bars(bars_1m, self.cfg.classify_interval)
            if len(bars_htf) < 52:
                return None
            highs = [b.high for b in bars_htf]
            lows = [b.low for b in bars_htf]
            closes = [b.close for b in bars_htf]
            return classify_regime(highs, lows, closes)
        except Exception as e:
            logger.warning(f"[REGIME] _get_regime_result failed: {e}")
            return None

    def _notify_discord(self, rec, session_date: str):
        if not callable(self.discord_notify_cb):
            return
        tag = "🔄 DRY-RUN" if rec.dry_run else "⚡ AUTO"
        action_label = {"deploy_long": "做多 LONG", "deploy_short": "做空 SHORT",
                        "deploy_short_half": "做空半倉 SHORT½", "sit_out": "觀望 SIT OUT",
                        "hold": "維持 HOLD"}.get(rec.action, rec.action)
        msg = (f"📊 **Regime 建議 {tag}** — {session_date}\n"
               f"原始 Raw: `{self._state.raw_regime}` (ADX {self._state.last_features.get('adx', 0):.1f})\n"
               f"有效 Effective: `{self._state.effective_regime}` (since {self._state.effective_since})\n"
               f"建議 Recommendation: **{action_label}**"
               + (f" — `{rec.strategy_name}`" if rec.strategy_name else "")
               + f"\n理由 Reason: {rec.reason}")
        try:
            self.discord_notify_cb(msg)
        except Exception:
            pass

    def get_status(self) -> dict:
        return {
            "state": self._state,
            "cfg": self.cfg,
            "state_path": self._state_path,
            "hist_path": self._hist_path,
        }
