"""RegimeSwitchingRunner — bot type 2: regime-driven strategy switching.

Extends LiveRunner with regime orchestration: classifies the market each
NIGHT session, swaps between long/short strategies at session boundaries,
and tracks genuine switching P&L from its own SimulatedBroker.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Callable

from ..regime.manager import RegimeManager
from ..regime.state_machine import RegimeConfig
from ..regime.switch_logic import session_slot, in_closed_gap, should_classify
from ..regime.store import record_session_result
from .live_runner import LiveRunner, _mode_to_source
from .session_store import save_session

logger = logging.getLogger(__name__)

_TZ_TAIPEI = timezone(timedelta(hours=8))


class RegimeSwitchingRunner(LiveRunner):
    """LiveRunner subclass that swaps strategies based on regime classification.

    Construction: always starts with long_strategy as the timeframe donor
    and ``regime_idle=True`` until the first recommendation is applied.
    """

    def __init__(
        self,
        strategy,
        symbol: str,
        point_value: int = 200,
        log_dir: str | None = None,
        bot_name: str = "",
        strategy_display_name: str = "",
        *,
        regime_cfg: RegimeConfig,
        long_strategy_name: str,
        short_strategy_name: str,
        strategies_registry: dict | None = None,
        bars_provider: Callable[[], list] | None = None,
    ):
        super().__init__(
            strategy, symbol, point_value=point_value,
            log_dir=log_dir, bot_name=bot_name,
            strategy_display_name=strategy_display_name or "Regime Switching",
        )
        self.regime_idle = True
        self._regime_cfg = regime_cfg
        self._long_strategy_name = long_strategy_name
        self._short_strategy_name = short_strategy_name
        self._strategies_registry = strategies_registry or {}
        self._active_leg: str = "idle"  # "long" | "short" | "idle"

        self._manager = RegimeManager(
            self.bot_dir, regime_cfg,
            bars_provider=bars_provider,
        )

        self._pending_recommendation: dict | None = None
        self._recorded_sessions: set[tuple[str, str]] = set()

        # Re-arm any unexecuted recommendation from a prior run
        pending = self._manager.get_pending_recommendation()
        if pending and not pending.get("executed", True):
            self._pending_recommendation = pending
            logger.info("[REGIME] Re-armed pending recommendation from prior run: %s", pending.get("action"))

    # ── Status poll (called from GUI 30s timer) ──

    def on_status_poll(self, now: datetime | None = None) -> list[str]:
        """Run regime orchestration checks. Returns status lines for the GUI."""
        if now is None:
            now = datetime.now(_TZ_TAIPEI)
        elif now.tzinfo is None:
            now = now.replace(tzinfo=_TZ_TAIPEI)

        lines = []
        slot = session_slot(now)
        minutes_to_close = self._minutes_until_session_close(now)

        # T2: Record session P&L at session end (before classification)
        self._maybe_record_session(now, slot, minutes_to_close)

        # T1: Classify at NIGHT end
        classified = self._maybe_classify(now, slot, minutes_to_close)
        if classified:
            lines.append(f"[REGIME] Classified: {classified.action}")

        # T3: Apply pending recommendation in closed gap
        applied = self._maybe_apply_pending(now)
        if applied:
            lines.append(f"[REGIME] Applied: {applied}")

        return lines

    def _maybe_classify(self, now, slot, minutes_to_close) -> object | None:
        last_assessed = self._manager._state.last_assessed
        if not should_classify(now, slot, last_assessed, minutes_to_close):
            return None

        session_date, _ = slot
        rec = self._manager.classify_session(session_date, "NIGHT")
        if rec is not None:
            self._pending_recommendation = {
                "date": session_date, "action": rec.action,
                "strategy": rec.strategy_name, "qty_scale": rec.qty_scale,
                "reason": rec.reason,
            }
            self._emit("on_status",
                        f"[REGIME] Classified {session_date}: {rec.action} ({rec.strategy_name})")
        return rec

    def _maybe_record_session(self, now, slot, minutes_to_close):
        if minutes_to_close > 2:
            return
        session_date, session_type = slot
        key = (session_date, session_type)
        if key in self._recorded_sessions:
            return
        self._recorded_sessions.add(key)

        # Compute session P&L from own broker trades
        pnl, n_trades = self._compute_session_pnl(session_date, session_type)
        strategy_name = self.strategy_display_name if self._active_leg != "idle" else "idle"
        self._manager.do_record_session_result(
            session_date, session_type, pnl, n_trades,
            strategy_active=strategy_name,
            trading_mode=self.trading_mode,
        )
        self._emit("on_status",
                    f"[REGIME] Recorded {session_type} P&L: {pnl:+} ({n_trades} trades)")

    def _compute_session_pnl(self, session_date: str, session_type: str) -> tuple[float, int]:
        """Sum P&L of trades closed in this session window."""
        pnl = 0.0
        count = 0
        for t in self.broker.trades:
            if not t.exit_dt:
                continue
            try:
                exit_date = t.exit_dt[:10]
            except (TypeError, IndexError):
                continue
            if exit_date == session_date:
                pnl += t.pnl
                count += 1
        return pnl, count

    def _maybe_apply_pending(self, now) -> str | None:
        if self._pending_recommendation is None:
            return None
        if not in_closed_gap(now):
            return None
        # Gates: flat, no veto, not in replay
        if self.broker.has_open_position():
            return None
        if self.mode_switch_veto is not None:
            reason = self.mode_switch_veto()
            if reason:
                return None
        if self.suppress_strategy or self._is_reloading:
            return None

        rec = self._pending_recommendation
        action = rec.get("action", "hold")
        strategy_name = rec.get("strategy", "")
        result = None

        try:
            if action == "deploy_long":
                result = self._apply_leg("long", self._long_strategy_name)
            elif action in ("deploy_short", "deploy_short_half"):
                result = self._apply_leg("short", self._short_strategy_name)
            elif action == "sit_out":
                result = self._apply_sit_out()
            elif action == "hold":
                result = "hold (no change)"
            else:
                result = f"unknown action: {action}"
        except Exception as e:
            logger.exception("[REGIME] Error applying recommendation: %s", e)
            self._emit("on_status", f"[REGIME] Apply error: {e}")
            return None

        # Mark executed
        self._pending_recommendation = None
        executed_at = datetime.now(_TZ_TAIPEI).strftime("%Y-%m-%d %H:%M:%S")
        self._manager.mark_recommendation_executed(executed_at)
        self._auto_save_session()
        self._emit("on_status", f"[REGIME] Applied: {result}")
        return result

    def _apply_leg(self, leg: str, strategy_name: str) -> str:
        """Instantiate and swap to a leg strategy."""
        if strategy_name not in self._strategies_registry:
            raise ValueError(f"Strategy '{strategy_name}' not in registry")
        strategy_cls = self._strategies_registry[strategy_name]
        new_strategy = strategy_cls()
        ok, reason = self.swap_strategy(new_strategy, strategy_name)
        if not ok:
            raise RuntimeError(f"swap_strategy refused: {reason}")
        self._active_leg = leg
        self.regime_idle = False
        return f"{leg} → {strategy_name}"

    def _apply_sit_out(self) -> str:
        """Enter idle mode — keep current strategy object but suppress trading."""
        # Clear any pending orders from outgoing strategy
        self.broker._pending_entries.clear()
        self.broker._pending_exits.clear()
        self.broker._pending_market_closes.clear()
        self._active_leg = "idle"
        self.regime_idle = True
        return "sit_out (idle)"

    # ── Session helpers ──

    def _minutes_until_session_close(self, now: datetime) -> float:
        """Estimate minutes until the current session's close."""
        minutes = now.hour * 60 + now.minute
        # DAY session: closes at 13:45
        if 525 <= minutes < 826:
            return max(0, 825 - minutes)
        # NIGHT session: closes at 05:00 (next day if after 15:00)
        if minutes >= 900:
            return (24 * 60 - minutes) + 300
        if minutes < 300:
            return 300 - minutes
        # In a gap — return large number
        return 999

    # ── Session persistence (override) ──

    def _auto_save_session(self) -> None:
        try:
            data = {
                "strategy": self.strategy_display_name,
                "symbol": self.symbol,
                "bot_name": self.bot_name,
                "point_value": self.point_value,
                "target_interval": self.target_interval,
                "trading_mode": self.trading_mode,
                "daily_loss_limit": self.daily_loss_limit,
                "started_at": self._started_at,
                "saved_at": datetime.now().isoformat(timespec="seconds"),
                "bar_index": self._bar_index,
                "broker": self.broker.to_dict(),
                "regime_mode": True,
                "active_leg": self._active_leg,
            }
            save_session(self._session_path, data)
        except Exception:
            pass

    # ── Stop (override) ──

    def stop(self):
        """Record in-progress session result before teardown."""
        try:
            now = datetime.now(_TZ_TAIPEI)
            slot = session_slot(now)
            session_date, session_type = slot
            key = (session_date, session_type)
            if key not in self._recorded_sessions:
                self._recorded_sessions.add(key)
                pnl, n_trades = self._compute_session_pnl(session_date, session_type)
                strategy_name = self.strategy_display_name if self._active_leg != "idle" else "idle"
                self._manager.do_record_session_result(
                    session_date, session_type, pnl, n_trades,
                    strategy_active=strategy_name,
                    trading_mode=self.trading_mode,
                )
        except Exception as e:
            logger.warning("[REGIME] Error recording session on stop: %s", e)
        return super().stop()

    # ── Status ──

    @property
    def active_leg(self) -> str:
        return self._active_leg

    @property
    def regime_manager(self) -> RegimeManager:
        return self._manager

    def get_regime_status(self) -> dict:
        """Return regime-specific status for the GUI."""
        mgr_status = self._manager.get_status()
        return {
            "active_leg": self._active_leg,
            "regime_idle": self.regime_idle,
            "pending_recommendation": self._pending_recommendation,
            "long_strategy": self._long_strategy_name,
            "short_strategy": self._short_strategy_name,
            **mgr_status,
        }
