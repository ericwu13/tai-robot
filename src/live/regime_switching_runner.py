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
from ..regime.switch_logic import (
    SessionInfo,
    classification_due,
    current_session,
    in_closed_gap,
    last_completed_night,
)
from .bar_aggregator import BarAggregator, aggregate_bars
from .live_runner import LiveRunner, _INTERVAL_SECONDS, _mode_to_source, load_1m_bars_from_csvs
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

        self._classify_retry_after: datetime | None = None

        self._manager = RegimeManager(
            self.bot_dir, regime_cfg,
            bars_provider=bars_provider or self._classifier_bars,
        )
        # Write an inspectable placeholder regime_state.json at deploy time
        # so the bot folder reflects regime status before the first
        # night-end classification. No-op if a state file already exists.
        self._manager.ensure_initial_state()

        self._pending_recommendation: dict | None = None
        self._recorded_sessions: set[tuple[str, str]] = set()

        # Callback fired on strategy swap — wired to DiscordNotifier by the GUI
        self.on_regime_swap_cb: Callable[[str, str, str], None] | None = None

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

        # T1: Classify at NIGHT end — BEFORE the P&L record, so the record
        # lands on the freshly appended classification row instead of
        # spawning a second standalone row for the same session.
        classified = self._maybe_classify(now)
        if classified:
            lines.append(f"[REGIME] Classified: {classified.action}")

        # T2: Record session P&L at session end
        self._maybe_record_session(now)

        # T3: Apply pending recommendation in closed gap
        applied = self._maybe_apply_pending(now)
        if applied:
            lines.append(f"[REGIME] Applied: {applied}")

        return lines

    def _maybe_classify(self, now) -> object | None:
        if self._classify_retry_after is not None and now < self._classify_retry_after:
            return None
        last_assessed = self._manager._state.last_assessed
        sess = classification_due(now, last_assessed)
        if sess is None:
            return None

        catch_up = now >= sess.close_dt
        rec = self._manager.classify_session(sess.open_date, "NIGHT")
        if rec is None:
            # Insufficient bars or classifier error. The post-close
            # catch-up trigger would otherwise retry (and warn) on every
            # 30s poll until the next night is assessed — back off.
            self._classify_retry_after = now + timedelta(minutes=30)
            return None
        self._classify_retry_after = None

        self._pending_recommendation = {
            "date": sess.open_date, "action": rec.action,
            "strategy": rec.strategy_name, "qty_scale": rec.qty_scale,
            "reason": rec.reason,
        }
        tag = " (catch-up)" if catch_up else ""
        self._emit("on_status",
                    f"[REGIME] Classified {sess.open_date}{tag}: {rec.action} ({rec.strategy_name})")
        return rec

    def _maybe_record_session(self, now):
        sess = current_session(now)
        if sess is None:
            return  # gap / weekend / holiday — no phantom rows
        if (sess.close_dt - now).total_seconds() > 120:
            return
        key = (sess.open_date, sess.slot)
        if key in self._recorded_sessions:
            return
        self._recorded_sessions.add(key)
        self._record_session(sess)

    def _record_session(self, sess: SessionInfo):
        pnl, n_trades = self._compute_session_pnl(sess)
        strategy_name = self.strategy_display_name if self._active_leg != "idle" else "idle"
        self._manager.do_record_session_result(
            sess.open_date, sess.slot, pnl, n_trades,
            strategy_active=strategy_name,
            trading_mode=self.trading_mode,
        )
        self._emit("on_status",
                    f"[REGIME] Recorded {sess.slot} P&L: {pnl:+} ({n_trades} trades)")

    def _compute_session_pnl(self, sess: SessionInfo) -> tuple[float, int]:
        """Sum P&L of trades whose exit falls inside this session's window.

        Window-based (open_dt..close_dt), not calendar-date-based: the
        night session straddles midnight, so a date-only match dropped
        pre-midnight exits and double-counted post-midnight exits into
        the same date's DAY row. exit_dt strings ("YYYY-MM-DD HH:MM",
        sometimes with seconds from force-close) compare
        lexicographically == chronologically once sliced to minutes.
        """
        lo = sess.open_dt.strftime("%Y-%m-%d %H:%M")
        hi = sess.close_dt.strftime("%Y-%m-%d %H:%M")
        pnl = 0.0
        count = 0
        for t in self.broker.trades:
            exit_dt = (t.exit_dt or "")[:16]
            if lo <= exit_dt <= hi:
                pnl += t.pnl
                count += 1
        return pnl, count

    def _discard_pending(self, reason: str) -> None:
        """Drop the pending recommendation permanently (mark executed on
        disk so a restart does not re-arm it)."""
        self._pending_recommendation = None
        executed_at = datetime.now(_TZ_TAIPEI).strftime("%Y-%m-%d %H:%M:%S")
        self._manager.mark_recommendation_executed(executed_at)
        logger.warning("[REGIME] Discarded pending recommendation: %s", reason)
        self._emit("on_status", f"[REGIME] Discarded recommendation: {reason}")

    def _maybe_apply_pending(self, now) -> str | None:
        if self._pending_recommendation is None:
            return None

        rec = self._pending_recommendation

        # Staleness gate: a recommendation belongs to the night it
        # classified. If a NEWER night has completed since (apply blocked
        # for days, or re-armed after a long outage), acting on it would
        # trade tomorrow on stale data — a fresh classification should
        # decide instead (classify runs before apply in the same poll;
        # this only triggers when that classification couldn't produce).
        completed = last_completed_night(now)
        if (completed is not None and rec.get("date", "")
                and rec["date"] < completed.open_date):
            self._discard_pending(
                f"stale — assessed {rec.get('date')}, latest completed "
                f"night is {completed.open_date}")
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

        action = rec.get("action", "hold")
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
        except ValueError as e:
            # Permanent config error (leg strategy missing from the
            # registry) — retrying every 30s forever is useless.
            logger.exception("[REGIME] Unrecoverable apply error: %s", e)
            self._discard_pending(f"apply failed permanently: {e}")
            return None
        except Exception as e:
            # Transient (e.g. swap refused pending more 1-min history) —
            # keep the recommendation armed for the next poll.
            logger.exception("[REGIME] Error applying recommendation: %s", e)
            self._emit("on_status", f"[REGIME] Apply error: {e}")
            return None

        # Mark executed on disk FIRST, then drop the in-memory pending —
        # the reverse order could re-apply after a restart if the state
        # write failed silently.
        executed_at = datetime.now(_TZ_TAIPEI).strftime("%Y-%m-%d %H:%M:%S")
        self._manager.mark_recommendation_executed(executed_at)
        self._pending_recommendation = None
        self._auto_save_session()
        self._emit("on_status", f"[REGIME] Applied: {result}")
        return result

    def _apply_leg(self, leg: str, strategy_name: str) -> str:
        """Instantiate and swap to a leg strategy."""
        if strategy_name not in self._strategies_registry:
            raise ValueError(f"Strategy '{strategy_name}' not in registry")
        prev_leg = self._active_leg
        strategy_cls = self._strategies_registry[strategy_name]
        new_strategy = strategy_cls()
        ok, reason = self.swap_strategy(new_strategy, strategy_name)
        if not ok:
            raise RuntimeError(f"swap_strategy refused: {reason}")
        self._active_leg = leg
        self.regime_idle = False
        if callable(self.on_regime_swap_cb):
            try:
                self.on_regime_swap_cb(prev_leg, leg, strategy_name)
            except Exception:
                pass
        return f"{leg} → {strategy_name}"

    def _apply_sit_out(self) -> str:
        """Enter idle mode — keep current strategy object but suppress trading."""
        prev_leg = self._active_leg
        self.broker._pending_entries.clear()
        self.broker._pending_exits.clear()
        self.broker._pending_market_closes.clear()
        self._active_leg = "idle"
        self.regime_idle = True
        if callable(self.on_regime_swap_cb):
            try:
                self.on_regime_swap_cb(prev_leg, "idle", "")
            except Exception:
                pass
        return "sit_out (idle)"

    # ── Classifier bar supply ──

    def _classifier_bars(self) -> list:
        """Default bars provider for regime classification.

        Prefers the in-memory aggregated bars — seeded at deploy by
        ``feed_warmup_bars()`` with ~2 weeks of KLine history — so a
        fresh bot can classify from its first night instead of waiting
        days for its own CSVs to reach 52 classify-interval bars. Falls
        back to the CSV 1-min history whenever that yields more
        classify-interval bars (e.g. after a cross-timeframe swap
        rebuild dropped the warmup bars) or when the target interval
        cannot be re-binned into the classify interval (H4/daily legs).

        Reads ``self._aggregated_bars`` at call time — do NOT capture
        the list object; ``_rebuild_timeframe`` rebinds it (issue #43).
        """
        interval = self._regime_cfg.classify_interval
        candidates: list[list] = []
        if 0 < self.target_interval <= interval:
            # Dedup+sort defensively: a mid-gap deploy can append a
            # tick-replayed session behind warmup bars that already
            # cover it (warmup bars are not in _seen_1m_dts for >1m
            # timeframes), which would spam out-of-order warnings.
            mem = sorted({b.dt: b for b in self._aggregated_bars}.values(),
                         key=lambda b: b.dt)
            if mem:
                candidates.append(mem)
        csv_bars = load_1m_bars_from_csvs(self.bot_dir, self.symbol)
        if csv_bars:
            candidates.append(csv_bars)
        if not candidates:
            return []
        if len(candidates) == 1:
            return candidates[0]
        return max(candidates,
                   key=lambda bs: len(aggregate_bars(bs, interval)))

    # ── Session persistence (override) ──

    def restore_session(self, session_data: dict) -> int:
        n = super().restore_session(session_data)
        saved_leg = session_data.get("active_leg", "idle")
        if saved_leg in ("long", "short"):
            name = (self._long_strategy_name if saved_leg == "long"
                    else self._short_strategy_name)
            if name in self._strategies_registry:
                cls = self._strategies_registry[name]
                self.strategy = cls()
                self.strategy_display_name = name
                self.broker.strategy_label = name
                self._active_leg = saved_leg
                self.regime_idle = False
                new_interval = _INTERVAL_SECONDS.get(
                    (self.strategy.kline_type, self.strategy.kline_minute),
                    14400)
                if new_interval != self.target_interval:
                    self.target_interval = new_interval
                    self.aggregator = BarAggregator(self.symbol, new_interval)
                logger.info(
                    "[REGIME] Restored active leg from session: %s → %s",
                    saved_leg, name)
                logger.info(
                    "[REGIME] Restored timeframe to %ds for %s",
                    self.target_interval, name)
            else:
                logger.warning(
                    "[REGIME] Cannot restore leg '%s': strategy '%s' "
                    "not in registry", saved_leg, name)
        return n

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
                "last_report_key": self._last_report_key,
                "regime_mode": True,
                "active_leg": self._active_leg,
                "long_strategy": self._long_strategy_name,
                "short_strategy": self._short_strategy_name,
            }
            save_session(self._session_path, data)
        except Exception:
            pass

    # ── Daily report (override) ──

    def _generate_daily_report(self) -> None:
        """Inject regime status into the daily report.

        Dedup key uses the session OPEN date so a night session crossing
        midnight has one identity (issue #95). We override to inject the
        regime_switching block.
        """
        try:
            from ..daily_report.report_generator import generate_session_report
            from ..regime.switch_logic import current_session, session_slot

            session = current_session()
            if session:
                session_date = session.open_date
                session_type = session.slot
                open_dt_str = session.open_dt.strftime("%Y-%m-%d %H:%M")
                close_dt_str = session.close_dt.strftime("%Y-%m-%d %H:%M")
            else:
                fallback_date, fallback_slot = session_slot()
                session_date = fallback_date
                session_type = fallback_slot
                open_dt_str = ""
                close_dt_str = ""

            report_key = f"{session_date}_{session_type}"
            if report_key == self._last_report_key:
                return

            report = generate_session_report(
                broker=self.broker,
                data_store=self.data_store,
                strategy_name=self.strategy_display_name,
                strategy_params=getattr(self.strategy, "params", None),
                point_value=self.point_value,
                symbol=self.symbol,
                date=session_date,
                bot_name=self.bot_name,
                started_at=self._started_at,
                session_open_dt=open_dt_str,
                session_close_dt=close_dt_str,
            )
            if report is None:
                return
            self._last_report_key = report_key
            report["regime_switching"] = {
                "active_leg": self._active_leg,
                "long_strategy": self._long_strategy_name,
                "short_strategy": self._short_strategy_name,
                "effective_regime": self._manager._state.effective_regime,
            }
            self._auto_save_session()
            self._emit("on_daily_report", report)
        except Exception:
            logger.exception("Regime daily report generation failed (bot=%s)",
                             self.bot_name)

    # ── Stop (override) ──

    def stop(self):
        """Record the in-progress session result on teardown.

        Runs AFTER ``super().stop()`` so the force-close trade it may
        append is included in the recorded P&L. Recording is NOT skipped
        when the close-window record already fired — the store updates
        the existing row in place, so this just refreshes it with the
        final trade set. A stop during a gap/weekend records nothing
        (the session-end record already covered the last session).
        """
        now = datetime.now(_TZ_TAIPEI)
        sess = current_session(now)
        summary = super().stop()
        if sess is not None:
            try:
                self._recorded_sessions.add((sess.open_date, sess.slot))
                self._record_session(sess)
            except Exception as e:
                logger.warning("[REGIME] Error recording session on stop: %s", e)
        return summary

    # ── Status ──

    @property
    def active_leg(self) -> str:
        return self._active_leg

    @property
    def long_strategy_name(self) -> str:
        return self._long_strategy_name

    @property
    def short_strategy_name(self) -> str:
        return self._short_strategy_name

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
