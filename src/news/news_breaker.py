"""Shared news circuit breaker for LiveRunner and RegimeSwitchingRunner.

One object owns the config, the ledger, the signal read, the plan, the
flatten/suppress execution, the session-key release, the calendar gate,
and the W2 heartbeat gate. Both runners call :meth:`NewsBreaker.poll`.
A plain bot does not read regime votes and does not swap legs; tier-2
deploy stays on the regime runner, which supplies ``_deploy_news_strategy``.

Disabled (no config, or ``enabled`` false) means :meth:`poll` returns
before any file is opened. That is the default.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone

from .circuit_breaker import (
    CLEAR_EVENT,
    DEPLOY_NEWS,
    DISCORD,
    FLATTEN,
    SRC_EVENT,
    SRC_SIGNAL,
    STAMP_EVENT,
    STAMP_NOTICE,
    SUPPRESS,
    UNSUPPRESS,
    BreakerState,
    plan_calendar_gate,
    plan_signal_actions,
    plan_suppression_maintenance,
)
from .event_calendar import (
    active_event,
    calendar_stale,
    load_events,
    next_session,
    read_updated_at,
    upcoming_event,
)
from .heartbeat import read_heartbeat
from .signal_file import SignalLedger, read_signal

logger = logging.getLogger(__name__)

_TZ_TAIPEI = timezone(timedelta(hours=8))
_EVENTS_REFRESH_SEC = 300

# Real-order modes. Anything that is not explicitly paper fails closed
# on a dead heartbeat, including an unexpected mode string.
_PAPER_MODE = "paper"


class _Health:
    def __init__(self) -> None:
        self.heartbeat_age_sec: float | None = None
        self.heartbeat_status: str = ""
        self.signal_age_sec: float | None = None
        self.events_age_sec: float | None = None
        self.votes_age_sec: float | None = None


class NewsBreaker:
    """Per-bot breaker. ``host`` is the LiveRunner (or a subclass)."""

    def __init__(self, host, news_cfg) -> None:
        self.host = host
        self.cfg = news_cfg
        self.enabled = bool(news_cfg is not None and news_cfg.enabled)
        self.state = BreakerState()
        self.ledger: SignalLedger | None = None
        self.events_cache: list = []
        self.events_updated_at: str = ""
        self.events_mtime: float = -1.0
        self.events_read_at: datetime | None = None
        self.events_error: str = ""
        self.calendar_stale_warned: bool = False
        self.health = _Health()
        # Last discord condition: None (never), "stale", "ok".
        self._hb_alert: str | None = None
        # Deploy warning for an empty heartbeat_path on a real bot.
        self._hb_unconfigured_warned: bool = False
        if self.enabled:
            ledger_path = news_cfg.ledger_path or os.path.join(
                host.bot_dir, "news_ledger.json")
            try:
                self.ledger = SignalLedger(ledger_path)
            except Exception as e:
                self.enabled = False
                logger.warning(
                    "[NEWS] Could not open signal ledger %s: %s — "
                    "news framework DISABLED for this session",
                    ledger_path, e)
        if self.enabled:
            logger.info(
                "[NEWS] 新聞斷路器啟用 News circuit breaker ENABLED "
                "(tier2=%s, signal=%s, heartbeat=%s)",
                self.cfg.tier2_enabled,
                self.cfg.signal_path,
                getattr(self.cfg, "heartbeat_path", ""),
            )
        else:
            logger.info(
                "[NEWS] 新聞斷路器停用 News circuit breaker DISABLED "
                "for this deploy")

    # ── Poll ──

    def poll(self, now: datetime | None = None) -> list[str]:
        """Signal → revert → calendar → maintenance → heartbeat.

        Each step is isolated. A bot with the breaker off returns before
        any of them touch the filesystem.
        """
        if not self.enabled:
            return []
        now = self._aware(now)
        lines: list[str] = []
        steps = (
            (self._check_signal, "signal"),
            (self._check_revert, "revert"),
            (self._check_calendar, "calendar"),
            (self._check_maintenance, "maintenance"),
            (self._check_heartbeat, "heartbeat"),
        )
        for step, label in steps:
            try:
                lines.extend(step(now))
            except Exception as e:
                logger.exception("[NEWS] %s step failed: %s", label, e)
                lines.append(f"[NEWS] {label} error: {e}")
        self._refresh_source_ages(now)
        try:
            self.host._auto_save_session()
        except Exception:
            logger.exception("[NEWS] session save after poll failed")
        return lines

    def _aware(self, now: datetime | None) -> datetime:
        if now is None:
            return datetime.now(_TZ_TAIPEI)
        if now.tzinfo is None:
            return now.replace(tzinfo=_TZ_TAIPEI)
        return now

    def _check_revert(self, now: datetime) -> list[str]:
        revert = getattr(self.host, "_check_news_revert", None)
        if not callable(revert):
            return []
        return list(revert(now) or [])

    def _check_signal(self, now: datetime) -> list[str]:
        cfg = self.cfg
        signal, reject = read_signal(
            cfg.signal_path, now, cfg.max_signal_age_sec)
        if signal is None:
            if reject:
                logger.debug("[NEWS] no actionable signal: %s", reject)
            return []
        consumed = (self.ledger.is_consumed(signal.signal_id)
                    if self.ledger is not None else False)
        side = self.host.broker.position_side
        decision = plan_signal_actions(
            signal,
            consumed,
            cfg.tier2_enabled,
            self.host.broker.has_open_position(),
            self.state,
            in_replay=bool(self.host.suppress_strategy or self.host._is_reloading),
            suppress_scope=getattr(cfg, "suppress_scope", "both") or "both",
            position_side=side.value.lower() if side else "",
            session_key=self.session_key(now),
        )
        if not decision:
            return []
        logger.info("[NEWS] Acting on signal %s (%s) at startup/poll",
                    signal.signal_id, signal.action)
        return self._execute(decision, now, label=f"signal {signal.action}")

    def _check_maintenance(self, now: datetime) -> list[str]:
        if self.host.suppress_strategy or self.host._is_reloading:
            return []
        if not self.state.suppressed:
            return []
        decision = plan_suppression_maintenance(
            self.state, self.session_key(now))
        if not decision:
            return []
        return self._execute(decision, now, label="maintenance")

    def _check_calendar(self, now: datetime) -> list[str]:
        cfg = self.cfg
        if not cfg.events_path:
            return []
        events = self._load_events_cached(now)
        stale = calendar_stale(self.events_updated_at, now)
        if stale:
            if not self.calendar_stale_warned:
                self.calendar_stale_warned = True
                detail = self.events_error or f"updated_at={self.events_updated_at!r}"
                logger.warning(
                    "[NEWS] Event calendar unusable or stale (%s) — "
                    "event gating disabled (fail open)", detail)
        else:
            self.calendar_stale_warned = False
        min_sev = cfg.calendar_min_severity or "high"
        act_ev = None if stale else active_event(events, now, min_sev)
        up_ev = None if stale else upcoming_event(events, now, min_sev)
        has_position = self.host.broker.has_open_position()
        decision = plan_calendar_gate(
            act_ev, up_ev, stale, self.state, has_position)
        if not decision:
            if (act_ev or up_ev) and has_position:
                logger.debug(
                    "[NEWS] Event gate deferred while a position is open: %s",
                    (act_ev or up_ev).name)
            return []
        return self._execute(decision, now, label="calendar")

    def _load_events_cached(self, now: datetime) -> list:
        path = self.cfg.events_path
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            mtime = -1.0
        expired = (
            self.events_read_at is None
            or (now - self.events_read_at).total_seconds() >= _EVENTS_REFRESH_SEC
        )
        if mtime != self.events_mtime or expired:
            events, err = load_events(path)
            self.events_cache = events
            self.events_updated_at = read_updated_at(path)
            self.events_mtime = mtime
            self.events_read_at = now
            if err and err != self.events_error:
                logger.warning("[NEWS] Event calendar: %s", err)
            self.events_error = err
        return self.events_cache

    def warn_unconfigured_heartbeat(self) -> str:
        """Warn once when a real ``--news`` bot has no heartbeat path.

        An empty or missing ``heartbeat_path`` still fails closed: the
        next poll blocks every new entry. This only makes that obvious
        at deploy. Paper mode is unchanged (entries stay open). Returns
        the warning text, or ``""`` when there is nothing to say.
        """
        if self._hb_unconfigured_warned or not self.enabled:
            return ""
        path = str(getattr(self.cfg, "heartbeat_path", "") or "")
        if path:
            return ""
        mode = str(getattr(self.host, "trading_mode", _PAPER_MODE) or "")
        if mode == _PAPER_MODE:
            return ""
        self._hb_unconfigured_warned = True
        text = (
            "⚠️ **W2 心跳未設定 W2 heartbeat UNCONFIGURED** — "
            "實單將阻擋全部進場 all entries will be blocked\n"
            f"模式 Mode: `{mode}`\n"
            "news.heartbeat_path is empty or unset. "
            "A real bot with --news blocks every new entry until the path "
            "is set and the W2 monitor is writing it.\n"
            "出場、停損、強制平倉不受影響 "
            "exits, stops and FORCE_CLOSE are not blocked"
        )
        flat = text.replace("\n", " | ")
        logger.warning("[NEWS] %s", flat)
        self.notify(text)
        return flat

    def _check_heartbeat(self, now: datetime) -> list[str]:
        """Fail closed for real bots. Paper logs a warning and keeps trading.

        Exits are never blocked here — the runner's entry filter reads
        ``heartbeat_entry_block`` and drops only new entries.
        """
        cfg = self.cfg
        path = getattr(cfg, "heartbeat_path", "") or ""
        max_age = int(getattr(cfg, "max_heartbeat_age_sec", 0) or 0)
        age, status = read_heartbeat(path, now, max_age)
        self.health.heartbeat_age_sec = age
        self.health.heartbeat_status = status
        stale = status != "ok"
        real = str(getattr(self.host, "trading_mode", _PAPER_MODE) or "") != _PAPER_MODE
        if stale and real:
            shown = f"{age:.0f}s" if age is not None else "n/a"
            reason = f"W2 heartbeat {status} ({shown})"
        else:
            reason = ""
        self.host.heartbeat_entry_block = reason

        condition = "stale" if stale else "ok"
        lines: list[str] = []
        if condition == self._hb_alert:
            return lines
        if stale:
            if real:
                text = (
                    "⚠️ **W2 心跳逾時 W2 heartbeat STALE** — "
                    "實單暫停進場 real entries blocked\n"
                    f"狀態 Status: `{status}`\n"
                    f"年齡 Age: {age if age is not None else 'n/a'}\n"
                    "出場、停損、強制平倉不受影響 "
                    "exits, stops and FORCE_CLOSE are not blocked"
                )
            else:
                text = (
                    "⚠️ **W2 心跳逾時 W2 heartbeat STALE** — "
                    "模擬僅警告 paper warning only (entries NOT blocked)\n"
                    f"狀態 Status: `{status}`\n"
                    f"年齡 Age: {age if age is not None else 'n/a'}"
                )
            logger.warning("[NEWS] %s", text.replace("\n", " | "))
            self.notify(text)
            lines.append(f"[NEWS] W2 heartbeat {status} — "
                         + ("real entries blocked" if real else "paper warning only"))
        elif self._hb_alert == "stale":
            text = (
                "✅ **W2 心跳恢復 W2 heartbeat RECOVERED** — "
                "進場恢復 entries allowed"
            )
            logger.info("[NEWS] %s", text)
            self.notify(text)
            lines.append("[NEWS] W2 heartbeat recovered")
        self._hb_alert = condition
        return lines

    # ── Execute ──

    def _execute(self, decision, now: datetime, label: str) -> list[str]:
        lines: list[str] = []
        mark_id = decision.consumed_id
        try:
            for act in decision.executable:
                line = self._apply(act, now)
                if line:
                    lines.append(line)
                    self.host._emit("on_status", line)
        except Exception as e:
            logger.exception("[NEWS] Action failed (%s): %s", label, e)
            self.notify(
                f"⚠️ **新聞斷路器執行失敗 News breaker action failed** "
                f"(`{label}`)\n`[{type(e).__name__}] {e}`\n"
                "訊號已標記為已處理，不會重試 signal marked consumed, no retry")
            lines.append(f"[NEWS] {label} action error: {e}")
        finally:
            if mark_id and self.ledger is not None:
                self.ledger.mark_consumed(mark_id)
            self.sync_idle()
            try:
                self.host._auto_save_session()
            except Exception:
                pass
        return lines

    def _apply(self, act, now: datetime) -> str:
        st = self.state
        kind = act.kind
        if kind == FLATTEN:
            price = self.host.force_close_position(act.arg, act.reason)
            if price:
                return f"[NEWS] Flattened @ {price:,} — {act.reason}"
            return "[NEWS] Flatten skipped — already flat"
        if kind in (SUPPRESS, UNSUPPRESS):
            on = kind == SUPPRESS
            if act.arg == SRC_EVENT:
                st.event_suppressed = on
            elif act.arg == SRC_SIGNAL:
                st.signal_suppressed = on
                st.signal_direction = act.direction if on else ""
                st.signal_severity = act.severity if on else ""
                st.signal_session_key = act.session_key if on else ""
                st.signal_suppressed_at = (
                    now.strftime("%Y-%m-%d %H:%M") if on else "")
            else:
                logger.warning("[NEWS] Unknown suppression source: %r", act.arg)
                return ""
            if on:
                st.suppressed_reason = act.reason
            elif not st.suppressed:
                st.suppressed_reason = ""
            verb = "suppressed" if on else "released"
            return f"[NEWS] Entries {verb} ({act.arg}): {act.reason}"
        if kind == STAMP_NOTICE:
            st.notice_session_key = act.arg
            return ""
        if kind == DEPLOY_NEWS:
            deploy = getattr(self.host, "_deploy_news_strategy", None)
            if not callable(deploy):
                raise RuntimeError(
                    "tier2 deploy requires a regime bot "
                    "(a plain runner does not swap legs)")
            return deploy(act.arg, now)
        if kind == STAMP_EVENT:
            st.event_name = act.arg
            stamp = getattr(self.host, "stamp_event_risk", None)
            if callable(stamp):
                stamp(act.arg)
            return f"[NEWS] Event risk stamped: {act.arg}"
        if kind == CLEAR_EVENT:
            prev = st.event_name
            st.event_name = ""
            clear = getattr(self.host, "clear_event_risk", None)
            if callable(clear):
                clear()
            return f"[NEWS] Event risk cleared: {prev}"
        if kind == DISCORD:
            self.notify(act.reason)
            return ""
        logger.warning("[NEWS] Unknown breaker action kind: %r", kind)
        return ""

    # ── State mirrored onto the runner ──

    def leg(self) -> str:
        if hasattr(self.host, "_active_leg"):
            return str(self.host._active_leg or "")
        return str(getattr(self.host, "_news_leg", "") or "")

    def sync_idle(self) -> None:
        scope = "both"
        if self.cfg is not None:
            scope = getattr(self.cfg, "suppress_scope", "both") or "both"
        gated = self.state.gates_leg(self.leg(), scope) if self.enabled else False
        self.host.news_idle = bool(gated)
        self.host.news_idle_reason = (
            self.state.suppressed_reason if self.host.news_idle else "")

    def session_key(self, now: datetime) -> str:
        from ..regime.switch_logic import current_session
        sess = current_session(now)
        if sess is not None:
            return sess.key
        nxt = next_session(now)
        return nxt.key if nxt is not None else ""

    def notify(self, message: str) -> None:
        cb = getattr(self.host, "on_news_notify_cb", None)
        if not message or not callable(cb):
            return
        try:
            cb(message)
        except Exception:
            pass

    def restore(self, session_data: dict | None) -> None:
        data = session_data if isinstance(session_data, dict) else {}
        if not self.enabled:
            if data.get("news"):
                logger.info(
                    "[NEWS] Ignoring persisted breaker state — news is "
                    "disabled for this deploy")
            self.sync_idle()
            return
        self.state = BreakerState.from_dict(data.get("news"))
        self.sync_idle()
        if self.state.suppressed:
            logger.info("[NEWS] Restored suppression from session: %s",
                        self.state.suppressed_reason or "(no reason)")

    def session_fields(self) -> dict:
        cfg = self.cfg
        return {
            "news_enabled": bool(self.enabled),
            "news_tier2_enabled": bool(cfg.tier2_enabled) if cfg else False,
            "news_suppress_scope": (
                (getattr(cfg, "suppress_scope", "both") or "both") if cfg else "both"
            ),
            "news": self.state.to_dict(),
            "news_health": self.health_dict(),
        }

    def health_dict(self) -> dict:
        host = self.host
        reason = ""
        blocked = False
        if self.enabled:
            reason = host._entry_block_reason()
            blocked = bool(reason)
        gate = "off"
        if self.enabled:
            gate = "suppressed" if host.news_idle else "clear"
        h = self.health
        return {
            "entries_blocked": blocked,
            "entries_blocked_reason": reason,
            "gate": gate,
            "gate_reason": host.news_idle_reason if self.enabled else "",
            "heartbeat_age_sec": h.heartbeat_age_sec,
            "heartbeat_status": h.heartbeat_status,
            "signal_age_sec": h.signal_age_sec,
            "events_age_sec": h.events_age_sec,
            "votes_age_sec": h.votes_age_sec,
        }

    def _refresh_source_ages(self, now: datetime) -> None:
        cfg = self.cfg
        self.health.signal_age_sec = _mtime_age(getattr(cfg, "signal_path", ""), now)
        self.health.events_age_sec = _mtime_age(getattr(cfg, "events_path", ""), now)
        if getattr(self.host, "reads_regime_votes", False):
            self.health.votes_age_sec = _mtime_age(
                getattr(cfg, "regime_vote_path", ""), now)
        else:
            # A plain bot must not stat the vote file. W2 votes are a
            # regime input; this breaker does not consume them.
            self.health.votes_age_sec = None


def _mtime_age(path: str, now: datetime) -> float | None:
    if not path or not os.path.exists(path):
        return None
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    return now.timestamp() - mtime
