"""RegimeSwitchingRunner — bot type 2: regime-driven strategy switching.

Extends LiveRunner with regime orchestration: classifies the market each
NIGHT session, swaps between long/short strategies at session boundaries,
and tracks genuine switching P&L from its own SimulatedBroker.

Phase 2 of the news framework hangs off the same 30s poll: after the
regime steps, ``_check_news`` reads the circuit-breaker signal file and
the scheduled-event calendar and executes whatever
``src.news.circuit_breaker`` plans. All branching lives in that pure
module; everything here is execution.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Callable

from ..news.circuit_breaker import (
    CLEAR_EVENT,
    DEPLOY_NEWS,
    DISCORD,
    FLATTEN,
    SRC_EVENT,
    SRC_SIGNAL,
    STAMP_EVENT,
    SUPPRESS,
    UNSUPPRESS,
    BreakerState,
    plan_calendar_gate,
    plan_signal_actions,
    should_revert_news,
)
from ..news.event_calendar import (
    active_event,
    calendar_stale,
    load_events,
    next_session,
    read_updated_at,
    upcoming_event,
)
from ..news.signal_file import SignalLedger, read_signal
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

if TYPE_CHECKING:                       # pragma: no cover
    # Type-only: src.config.settings pulls in PyYAML, which the live path
    # has no other reason to require.
    from ..config.settings import NewsConfig

logger = logging.getLogger(__name__)

_TZ_TAIPEI = timezone(timedelta(hours=8))

# Event-calendar re-read cadence. The gate is evaluated on every 30s poll
# but the file is parsed at most this often (an unchanged mtime skips the
# parse entirely, so a freshly written calendar is picked up immediately
# without polling the parser 120 times an hour).
_EVENTS_REFRESH_SEC = 300


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
        news_cfg: "NewsConfig | None" = None,
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

        # ── News circuit breaker (Phase 2) ──
        # Disabled by default: _news_enabled False means _check_news
        # returns before touching the filesystem, so a bot without a news
        # config behaves exactly as it did before this feature existed.
        self._news_cfg = news_cfg
        self._news_enabled = bool(news_cfg is not None and news_cfg.enabled)
        self._breaker_state = BreakerState()
        self._ledger: SignalLedger | None = None
        if self._news_enabled:
            # A missing ledger_path must NOT mean "no dedup" — without a
            # ledger a single risk_off re-flattens on every 30s poll for
            # its whole freshness window. Default it into the bot folder.
            ledger_path = news_cfg.ledger_path or os.path.join(
                self.bot_dir, "news_ledger.json")
            try:
                self._ledger = SignalLedger(ledger_path)
            except Exception as e:
                # No ledger = no exactly-once guarantee. Acting on every
                # poll is worse than not acting, so shut the feature off.
                self._news_enabled = False
                logger.warning(
                    "[NEWS] Could not open signal ledger %s: %s — "
                    "news framework DISABLED for this session",
                    ledger_path, e)
        # Event-calendar cache (path mtime + last parse time)
        self._events_cache: list = []
        self._events_updated_at: str = ""
        self._events_mtime: float = -1.0
        self._events_read_at: datetime | None = None
        self._events_error: str = ""
        self._calendar_stale_warned: bool = False
        # Callback for news announcements — wired to the regime Discord
        # channel by the GUI. Never holds an ID itself.
        self.on_news_notify_cb: Callable[[str], None] | None = None

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

        # T4: News circuit breaker — runs AFTER the regime steps so a
        # risk_off suppression is applied on top of whatever the regime
        # just decided, not overwritten by it on the same poll.
        lines.extend(self._check_news(now))

        return lines

    def _maybe_classify(self, now) -> object | None:
        if self._classify_retry_after is not None and now < self._classify_retry_after:
            return None
        last_assessed = self._manager._state.last_assessed
        sess = classification_due(now, last_assessed)
        if sess is None:
            return None

        catch_up = now >= sess.close_dt
        vote_directions = self._read_regime_vote(sess.key)
        rec = self._manager.classify_session(sess.open_date, "NIGHT", vote_directions=vote_directions)
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

    # ── Regime vote (news-as-votes acceleration) ──

    def _read_regime_vote(self, session_key: str) -> list[str]:
        """Read and consume all per-source vote files, returning a list of
        vote directions valid for *session_key*."""
        cfg = self._news_cfg
        if not cfg or not getattr(cfg, "regime_vote_path", ""):
            return []
        from ..news.regime_vote import read_all_regime_votes, consume_all_regime_votes
        try:
            votes = read_all_regime_votes(cfg.regime_vote_path, session_key)
            for v in votes:
                logger.info("[REGIME-VOTE] consumed vote: %s (source=%s)", v.direction, v.source)
            consume_all_regime_votes(cfg.regime_vote_path)
            return [v.direction for v in votes]
        except Exception as exc:
            logger.warning("[REGIME-VOTE] failed to read votes: %s", exc)
            return []

    # ── News circuit breaker (Phase 2) ──

    def _check_news(self, now) -> list[str]:
        """Signal → revert → calendar. Returns GUI status lines.

        Each step is isolated: a broken calendar file must not stop the
        signal from being read, and neither can take the 30s poll down.
        """
        if not self._news_enabled:
            return []
        if now is None:
            now = datetime.now(_TZ_TAIPEI)
        elif now.tzinfo is None:
            now = now.replace(tzinfo=_TZ_TAIPEI)
        lines: list[str] = []
        for step, label in ((self._check_news_signal, "signal"),
                            (self._check_news_revert, "revert"),
                            (self._check_event_calendar, "calendar")):
            try:
                lines.extend(step(now))
            except Exception as e:
                logger.exception("[NEWS] %s step failed: %s", label, e)
                lines.append(f"[NEWS] {label} error: {e}")
        return lines

    def _check_news_signal(self, now) -> list[str]:
        """Read the circuit-breaker signal file and execute its plan."""
        cfg = self._news_cfg
        signal, reject = read_signal(
            cfg.signal_path, now, cfg.max_signal_age_sec)
        if signal is None:
            # The overwhelmingly common case is "no file" — debug, not
            # a warning, or a bot with no pending signal logs every 30s.
            if reject:
                logger.debug("[NEWS] no actionable signal: %s", reject)
            return []

        consumed = (self._ledger.is_consumed(signal.signal_id)
                    if self._ledger is not None else False)
        decision = plan_signal_actions(
            signal,
            consumed,
            cfg.tier2_enabled,
            self.broker.has_open_position(),
            self._breaker_state,
            in_replay=bool(self.suppress_strategy or self._is_reloading),
        )
        if not decision:
            return []
        logger.info("[NEWS] Acting on signal %s (%s)",
                    signal.signal_id, signal.action)
        return self._execute_breaker(decision, now,
                                     label=f"signal {signal.action}")

    def _check_news_revert(self, now) -> list[str]:
        """Hand a deployed event strategy back to regime control.

        The event strategy is one-shot (it never re-enters), so leaving
        it running is not a trading risk — but it would silently park the
        bot outside regime control for every following session.
        """
        st = self._breaker_state
        nxt = next_session(now)
        if not should_revert_news(st, in_closed_gap(now),
                                  nxt.key if nxt is not None else ""):
            return []

        rec = self._manager.current_recommendation()
        try:
            if rec.action == "deploy_long":
                result = self._apply_leg("long", self._long_strategy_name)
            elif rec.action in ("deploy_short", "deploy_short_half"):
                result = self._apply_leg("short", self._short_strategy_name)
            else:
                result = self._apply_sit_out()
        except Exception as e:
            # Sitting out always succeeds (no swap involved), so the bot
            # can never get stuck on the event strategy because a leg
            # swap was refused.
            logger.warning("[NEWS] Revert to '%s' failed (%s) — sitting out",
                           rec.action, e)
            result = self._apply_sit_out()

        st.news_strategy_active = False
        st.news_deployed_session_key = ""
        self._sync_news_idle()
        self._auto_save_session()
        self._news_notify(
            "🔁 **新聞事件策略退場 News event strategy reverted**\n"
            f"交還 Regime 控制 handed back to regime: {result}\n"
            f"理由 Reason: {rec.reason}")
        line = f"[NEWS] Reverted to regime: {result}"
        self._emit("on_status", line)
        return [line]

    def _check_event_calendar(self, now) -> list[str]:
        """Stamp/clear ``_event_risk`` from the scheduled-event calendar."""
        cfg = self._news_cfg
        if not cfg.events_path:
            return []
        events = self._load_events_cached(now)
        stale = calendar_stale(self._events_updated_at, now)
        if stale:
            if not self._calendar_stale_warned:
                self._calendar_stale_warned = True
                detail = (self._events_error
                          or f"updated_at={self._events_updated_at!r}")
                logger.warning(
                    "[NEWS] Event calendar unusable or stale (%s) — "
                    "event gating disabled (fail open)", detail)
        else:
            self._calendar_stale_warned = False

        min_sev = cfg.calendar_min_severity or "high"
        act_ev = None if stale else active_event(events, now, min_sev)
        up_ev = None if stale else upcoming_event(events, now, min_sev)
        has_position = self.broker.has_open_position()
        decision = plan_calendar_gate(act_ev, up_ev, stale,
                                      self._breaker_state, has_position)
        if not decision:
            if (act_ev or up_ev) and has_position:
                # Deferred: suppression freezes the bar pipeline before
                # the strategy runs, which would strand an open trade
                # without stop management. Gating waits for it to exit.
                logger.debug(
                    "[NEWS] Event gate deferred while a position is open: %s",
                    (act_ev or up_ev).name)
            return []
        return self._execute_breaker(decision, now, label="calendar")

    def _load_events_cached(self, now) -> list:
        """Parse the calendar at most every 5 min (or when mtime moves)."""
        path = self._news_cfg.events_path
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            mtime = -1.0
        expired = (
            self._events_read_at is None
            or (now - self._events_read_at).total_seconds() >= _EVENTS_REFRESH_SEC
        )
        if mtime != self._events_mtime or expired:
            events, err = load_events(path)
            self._events_cache = events
            self._events_updated_at = read_updated_at(path)
            self._events_mtime = mtime
            self._events_read_at = now
            if err and err != self._events_error:
                logger.warning("[NEWS] Event calendar: %s", err)
            self._events_error = err
        return self._events_cache

    def _execute_breaker(self, decision, now, label: str) -> list[str]:
        """Run a planned action list, then ALWAYS mark the signal consumed.

        The ledger write lives in ``finally`` on purpose: an action that
        throws (swap refused, Discord down, disk full) must not leave the
        signal armed to be re-executed on the next 30s poll — a risk_off
        that flattens on every poll for its whole freshness window is
        exactly the failure the ledger exists to prevent.
        """
        lines: list[str] = []
        mark_id = decision.consumed_id
        try:
            for act in decision.executable:
                line = self._apply_breaker_action(act, now)
                if line:
                    lines.append(line)
                    self._emit("on_status", line)
        except Exception as e:
            logger.exception("[NEWS] Action failed (%s): %s", label, e)
            self._news_notify(
                f"⚠️ **新聞斷路器執行失敗 News breaker action failed** "
                f"(`{label}`)\n`[{type(e).__name__}] {e}`\n"
                "訊號已標記為已處理，不會重試 signal marked consumed, no retry")
            lines.append(f"[NEWS] {label} action error: {e}")
        finally:
            if mark_id and self._ledger is not None:
                self._ledger.mark_consumed(mark_id)
            self._sync_news_idle()
            self._auto_save_session()
        return lines

    def _apply_breaker_action(self, act, now) -> str:
        """Execute ONE planned action. Returns a status line (or "")."""
        st = self._breaker_state
        kind = act.kind

        if kind == FLATTEN:
            price = self.force_close_position(act.arg, act.reason)
            if price:
                return f"[NEWS] Flattened @ {price:,} — {act.reason}"
            return "[NEWS] Flatten skipped — already flat"

        if kind in (SUPPRESS, UNSUPPRESS):
            on = kind == SUPPRESS
            # Per-source, never a shared boolean: a `clear` releasing the
            # signal source must leave an event gate standing.
            if act.arg == SRC_EVENT:
                st.event_suppressed = on
            elif act.arg == SRC_SIGNAL:
                st.signal_suppressed = on
            else:
                logger.warning("[NEWS] Unknown suppression source: %r", act.arg)
                return ""
            if on:
                st.suppressed_reason = act.reason
            elif not st.suppressed:
                st.suppressed_reason = ""
            verb = "suppressed" if on else "released"
            return f"[NEWS] Entries {verb} ({act.arg}): {act.reason}"

        if kind == DEPLOY_NEWS:
            return self._deploy_news_strategy(act.arg, now)

        if kind == STAMP_EVENT:
            st.event_name = act.arg
            self._manager.set_event_risk(act.arg)
            return f"[NEWS] Event risk stamped: {act.arg}"

        if kind == CLEAR_EVENT:
            prev = st.event_name
            st.event_name = ""
            self._manager.set_event_risk("")
            return f"[NEWS] Event risk cleared: {prev}"

        if kind == DISCORD:
            self._news_notify(act.reason)
            return ""

        logger.warning("[NEWS] Unknown breaker action kind: %r", kind)
        return ""

    def _deploy_news_strategy(self, direction: str, now) -> str:
        """Emergency swap to the forced-entry event strategy.

        Bypasses ONLY the closed-gap timing condition that guards regime
        applies — a news deploy is worthless an hour after the news. Every
        ``swap_strategy`` gate still applies (flat, no fill-pending veto,
        not in replay, enough bars), and the planner already flattened.
        """
        from ..strategy.examples.news_event import (
            NEWS_LONG_DISPLAY, NEWS_SHORT_DISPLAY, NewsEventLong, NewsEventShort,
        )
        if direction == "short":
            cls, display = NewsEventShort, NEWS_SHORT_DISPLAY
        else:
            cls, display = NewsEventLong, NEWS_LONG_DISPLAY

        prev_leg = self._active_leg
        ok, reason = self.swap_strategy(cls(), display)
        if not ok:
            raise RuntimeError(f"news swap refused: {reason}")

        leg = f"news_{direction}"
        self._active_leg = leg
        # A regime sit_out would otherwise swallow the event strategy's
        # one and only entry.
        self.regime_idle = False
        st = self._breaker_state
        st.news_strategy_active = True
        st.news_deployed_session_key = self._news_session_key(now)
        if callable(self.on_regime_swap_cb):
            try:
                self.on_regime_swap_cb(prev_leg, leg, display)
            except Exception:
                pass
        return (f"[NEWS] Deployed {display} "
                f"(session {st.news_deployed_session_key or '?'})")

    def _news_session_key(self, now) -> str:
        """The session a news deploy belongs to: live one, else the next."""
        sess = current_session(now)
        if sess is not None:
            return sess.key
        nxt = next_session(now)
        return nxt.key if nxt is not None else ""

    def _sync_news_idle(self) -> None:
        """Mirror the breaker's suppression onto the bar pipeline."""
        self.news_idle = self._breaker_state.suppressed

    def _news_notify(self, message: str) -> None:
        if not message or not callable(self.on_news_notify_cb):
            return
        try:
            self.on_news_notify_cb(message)
        except Exception:
            pass  # best-effort; Discord must never break the poll

    @property
    def breaker_state(self) -> BreakerState:
        return self._breaker_state

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
        # News breaker state first: a risk_off suppression must survive a
        # restart, otherwise a crash-and-resume silently re-enables
        # entries the operator (or n8n) deliberately shut off. A saved
        # "news_*" leg is NOT re-instantiated below — the revert step
        # hands the bot back to regime control at the next gap.
        #
        # Only when news is enabled for THIS deploy. Restoring a
        # suppression into a bot the user just opted out of would idle it
        # with no poller left to ever clear the flag.
        if self._news_enabled:
            self._breaker_state = BreakerState.from_dict(session_data.get("news"))
            self._sync_news_idle()
            if self._breaker_state.suppressed:
                logger.info("[NEWS] Restored suppression from session: %s",
                            self._breaker_state.suppressed_reason or "(no reason)")
        elif session_data.get("news"):
            logger.info("[NEWS] Ignoring persisted breaker state — news is "
                        "disabled for this deploy")

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
                # Per-bot news enablement, so the deploy dialog can
                # pre-tick this bot's own choice on resume (settings.yaml
                # only supplies the default for a brand-new bot).
                "news_enabled": self._news_enabled,
                "news_tier2_enabled": bool(
                    self._news_cfg.tier2_enabled if self._news_cfg else False),
                "news": self._breaker_state.to_dict(),
            }
            save_session(self._session_path, data)
        except Exception:
            pass

    # ── Daily report (override) ──

    def _generate_daily_report(self) -> None:
        """Inject regime status into the daily report.

        Dedup is handled by the ``{date}_{session}`` key (persisted in
        session.json). We override only to inject the regime_switching
        block and to intercept the emit so it includes the extra data.
        """
        try:
            from ..daily_report.report_generator import generate_session_report
            report = generate_session_report(
                broker=self.broker,
                data_store=self.data_store,
                strategy_name=self.strategy_display_name,
                strategy_params=getattr(self.strategy, "params", None),
                point_value=self.point_value,
                symbol=self.symbol,
                bot_name=self.bot_name,
                started_at=self._started_at,
            )
            if report is None:
                return
            report_date = report.get("date", "")
            _, session_type = self._session_key()
            report_key = f"{report_date}_{session_type}" if report_date else ""
            if report_key and report_key == self._last_report_key:
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
            "news_enabled": self._news_enabled,
            "news": self._breaker_state.to_dict(),
            **mgr_status,
        }
