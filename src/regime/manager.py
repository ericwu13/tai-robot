"""RegimeManager — regime classification and recommendation engine.

Phase 3: driven by a wall-clock session-boundary trigger (not the
trade-gated daily report). Classifies once per NIGHT session, persists
state + history, and produces a Recommendation for the switching runner.
"""

import os
import logging
from typing import Callable

from src.daily_report.regime_classifier import classify_regime, RegimeResult
from src.live.bar_aggregator import aggregate_bars
from .state_machine import RegimeStateMachine, RegimeState, RegimeConfig
from .selector import StrategySelector, Recommendation
from .store import (
    load_state, save_state, append_history,
    record_session_result, migrate_legacy_history,
    write_placeholder_state,
)

logger = logging.getLogger(__name__)

# Minimum classify-interval (HTF) bars required to classify: EMA-50 needs
# 50 plus headroom for the leading/trailing partial aggregation bars.
MIN_CLASSIFY_BARS = 52

# Regime enum value → bilingual display label for Discord notifications.
# DISPLAY ONLY — the stored raw_regime/effective_regime values (state file,
# history CSV, pattern-matching in the state machine/selector) stay as the
# bare English enum. Never feed a translated label back into stored state.
_REGIME_LABELS = {
    "trending-up": "上升趨勢 trending-up",
    "trending-down": "下降趨勢 trending-down",
    "range-bound": "盤整 range-bound",
    "transitional": "過渡期 transitional",
    "transitional-bullish": "過渡偏多 transitional-bullish",
    "transitional-bearish": "過渡偏空 transitional-bearish",
    "high-volatility": "高波動 high-volatility",
    "low-volatility-chop": "低波動盤整 low-volatility-chop",
    "unknown": "未知 unknown",
}


def _regime_label(regime: str) -> str:
    """Bilingual display label for a regime enum value (falls back to raw)."""
    return _REGIME_LABELS.get(regime, regime)


class RegimeManager:
    def __init__(
        self,
        bot_dir: str,
        cfg: RegimeConfig,
        bars_provider: Callable[[], list] | None = None,
    ):
        self.bot_dir = bot_dir
        self.cfg = cfg
        self._state_path = os.path.join(bot_dir, "regime_state.json")
        self._hist_path = os.path.join(bot_dir, "regime_history.csv")

        migrate_legacy_history(self._hist_path)

        self._state = load_state(self._state_path)
        self._machine = RegimeStateMachine()
        self._selector = StrategySelector()
        self.discord_notify_cb = None

        self._bars_provider = bars_provider or self._default_bars_provider

    def ensure_initial_state(self) -> None:
        """Write an inspectable placeholder ``regime_state.json`` if none
        exists yet (deploy-time convenience). No-op when the file already
        exists, so a resumed bot's persisted state is preserved."""
        write_placeholder_state(self._state_path)

    def reload_config(self, cfg: RegimeConfig):
        self.cfg = cfg

    # ── Classification (Phase 3 wall-clock trigger) ──

    def classify_session(
        self, session_date: str, session_slot: str,
        vote_directions: list[str] | None = None,
    ) -> Recommendation | None:
        """Classify the current regime and produce a recommendation.

        Fires unconditionally on NIGHT sessions. On-disk dedup via
        ``state.last_assessed`` prevents double-classification across
        restarts.  Returns the Recommendation, or None if insufficient
        data or dedup blocks.

        *vote_directions*: list of cross-market regime votes from
        independent sources (W2, W3).  Passed through to the state
        machine; if any vote agrees with the raw classification,
        confirmation is accelerated.
        """
        if session_slot != "NIGHT":
            return None

        dedup_key = f"{session_date}|NIGHT"
        if self._state.last_assessed == dedup_key:
            logger.debug("[REGIME] classify_session skipped — already assessed %s", dedup_key)
            return None

        try:
            result = self._get_regime_result()
            if result is None:
                logger.warning("[REGIME] Could not compute regime — insufficient bars")
                return None
            self._state = self._machine.step(self._state, result, self.cfg, session_date, vote_directions=vote_directions or [])
            rec = self._selector.select(self._state, self.cfg)
        except Exception as e:
            logger.exception("[REGIME] classify_session error: %s", e)
            return None

        # Stamp the on-disk dedup key before persisting (survives restart)
        self._state.last_assessed = dedup_key

        try:
            save_state(self._state_path, self._state, rec, session_date)
        except Exception as e:
            logger.exception("[REGIME] Error saving state: %s", e)

        try:
            append_history(self._hist_path, session_date, self._state, rec)
        except Exception as e:
            logger.exception("[REGIME] Error appending history: %s", e)

        if self._state.manual_override != "auto":
            self._state.manual_override = "auto"
            try:
                save_state(self._state_path, self._state, rec, session_date)
            except Exception as e:
                logger.exception("[REGIME] Error saving state (override reset): %s", e)

        self._notify_discord(rec, session_date)
        logger.info(
            "[REGIME] %s: %s → %s → %s (%s)",
            session_date, self._state.raw_regime,
            self._state.effective_regime, rec.action, rec.strategy_name,
        )
        return rec

    def do_record_session_result(
        self,
        session_date: str,
        session_slot: str,
        pnl: float,
        trades: int,
        strategy_active: str = "",
        trading_mode: str = "",
    ) -> None:
        """Record a session's P&L from the switching runner's own broker."""
        try:
            record_session_result(
                self._hist_path, session_date, session_slot,
                pnl, trades, strategy_active, trading_mode,
            )
        except Exception as e:
            logger.exception("[REGIME] Error recording session result: %s", e)

    # ── Pending recommendation from persisted state ──

    def get_pending_recommendation(self) -> dict | None:
        """Return the next_session block from regime_state.json if unexecuted."""
        try:
            import json
            if not os.path.exists(self._state_path):
                return None
            with open(self._state_path, encoding="utf-8") as f:
                d = json.load(f)
            ns = d.get("next_session")
            if ns and not ns.get("executed", True):
                return ns
        except Exception:
            pass
        return None

    # ── Scheduled-event gate (news calendar) ──

    def set_event_risk(self, event_name: str) -> None:
        """Stamp or clear the selector's ``_event_risk`` gate.

        The stored value is the EVENT NAME; falsy clears the flag (see
        ``StrategySelector.select``). Nothing else stamps it, so nothing
        else clears it — leave it set and the selector sits out forever.

        Persisted by patching ``last_features`` in ``regime_state.json``
        in place: ``save_state`` demands a Recommendation this call has
        no business inventing, and the surrounding pending-recommendation
        block must survive untouched.
        """
        if event_name:
            if self._state.last_features.get("_event_risk") == event_name:
                return
            self._state.last_features["_event_risk"] = event_name
        else:
            if "_event_risk" not in self._state.last_features:
                return
            self._state.last_features.pop("_event_risk", None)
        self._patch_state_features()

    def _patch_state_features(self) -> None:
        """Write ``last_features`` back to regime_state.json atomically."""
        import json
        try:
            d = {}
            if os.path.exists(self._state_path):
                with open(self._state_path, encoding="utf-8") as f:
                    d = json.load(f)
            if not isinstance(d, dict):
                d = {}
            d["last_features"] = self._state.last_features
            tmp = self._state_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(d, f, indent=2, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self._state_path)
        except Exception as e:
            logger.warning("[REGIME] Could not persist last_features: %s", e)

    def current_recommendation(self) -> Recommendation:
        """Re-run the selector against the CURRENT state, without classifying.

        Used by the news circuit breaker when it hands a deployed event
        strategy back to regime control: the regime's own view has not
        changed, so the leg that should be running is whatever the
        selector says right now (including a ``_event_risk`` sit-out).
        """
        return self._selector.select(self._state, self.cfg)

    def mark_recommendation_executed(self, executed_at: str) -> None:
        """Mark the pending next_session recommendation as executed."""
        try:
            import json
            if not os.path.exists(self._state_path):
                return
            with open(self._state_path, encoding="utf-8") as f:
                d = json.load(f)
            ns = d.get("next_session")
            if ns:
                ns["executed"] = True
                ns["executed_at"] = executed_at
            tmp = self._state_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(d, f, indent=2, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self._state_path)
        except Exception as e:
            logger.exception("[REGIME] Error marking recommendation executed: %s", e)

    # ── Bar data ──

    def _get_regime_result(self) -> RegimeResult | None:
        try:
            bars = self._bars_provider()
            if not bars:
                return None
            bars_htf = aggregate_bars(bars, self.cfg.classify_interval)
            if len(bars_htf) < MIN_CLASSIFY_BARS:
                return None
            highs = [b.high for b in bars_htf]
            lows = [b.low for b in bars_htf]
            closes = [b.close for b in bars_htf]
            return classify_regime(highs, lows, closes)
        except Exception as e:
            logger.warning("[REGIME] _get_regime_result failed: %s", e)
            return None

    def _default_bars_provider(self) -> list:
        """Load 1-min bars from CSV files in the bot directory."""
        from src.live.live_runner import load_1m_bars_from_csvs
        symbol = os.path.basename(self.bot_dir).split("_")[0]
        return load_1m_bars_from_csvs(self.bot_dir, symbol)

    def htf_bar_count(self) -> int:
        """Return the number of HTF bars available for classification."""
        try:
            bars = self._bars_provider()
            if not bars:
                return 0
            return len(aggregate_bars(bars, self.cfg.classify_interval))
        except Exception:
            return 0

    # ── Discord ──

    def _notify_discord(self, rec, session_date: str):
        if not callable(self.discord_notify_cb):
            return
        action_label = {"deploy_long": "做多 LONG", "deploy_short": "做空 SHORT",
                        "deploy_short_half": "做空半倉 SHORT½", "sit_out": "觀望 SIT OUT",
                        "hold": "維持 HOLD"}.get(rec.action, rec.action)
        msg = (f"📊 **Regime 建議** — {session_date}\n"
               f"原始 Raw: `{_regime_label(self._state.raw_regime)}` (ADX {self._state.last_features.get('adx', 0):.1f})\n"
               f"有效 Effective: `{_regime_label(self._state.effective_regime)}` (since {self._state.effective_since})\n"
               f"建議 Recommendation: **{action_label}**"
               + (f" — `{rec.strategy_name}`" if rec.strategy_name else "")
               + f"\n理由 Reason: {rec.reason}")
        try:
            self.discord_notify_cb(msg)
        except Exception:
            pass

    # ── Status ──

    def get_status(self) -> dict:
        return {
            "state": self._state,
            "cfg": self.cfg,
            "state_path": self._state_path,
            "hist_path": self._hist_path,
        }
