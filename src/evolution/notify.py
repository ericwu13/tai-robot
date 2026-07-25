"""Strategy-improvement notification glue between evolution and Discord.

The evolution module ships the fitness math; this module wires it into
the live daily-report pipeline so the bot can announce when a session
has produced a new best composite score.

Why a separate file:
  - ``evaluator.py`` only scores paper sessions (the spec's promotion gate);
    notification cares about any mode the user is actually running.
  - ``fitness.py`` is pure metric math, no I/O.
  - The baseline format is intentionally lightweight (one JSON file per
    bot) so it has no schema migrations and no SQLite dependency. It
    lives next to the bot's session.json under data/live/{symbol}_{bot}/.

Public API (kept small for the GUI to call):
  - :func:`score_session_for_notification` — score any-mode trades.
  - :func:`detect_improvement` — compare against the persisted baseline,
    return a verdict + optional new baseline to write.
  - :func:`load_baseline` / :func:`save_baseline` — file I/O.
  - :func:`check_and_notify_after_report` — single entry-point the GUI
    hooks into ``_on_live_daily_report`` (does the scoring + comparison
    + Discord send + baseline write).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Any, Callable

import logging

from .fitness import (
    FitnessResult,
    SOURCE_BACKTEST,
    SOURCE_PAPER,
    SOURCE_REAL,
    compute_fitness_from_trades,
)

logger = logging.getLogger(__name__)


_TZ_TAIPEI = timezone(timedelta(hours=8))

# Minimum delta (in composite units, 0–1 scale) over the running best
# before we send a Discord "strategy improved" notification.  Chosen to
# be coarse enough to ignore one-trade-luck noise on low-volume days
# while still firing on real improvements.
IMPROVEMENT_DELTA = 0.05

# Filename used under each bot's directory.  Sits next to session.json
# / decisions.csv / bars_1m_*.csv, no migration story needed.
BASELINE_FILENAME = "evolution.json"

# Evolution watermark: which trades the on-demand Bot Evolution has
# already analyzed. Separate file from evolution.json (the fitness
# baseline) — save_baseline() rewrites that file wholesale, so a field
# added there would be silently dropped on the next baseline write.
# A high-water mark (trade count) is equivalent to per-trade "used"
# flags because broker.trades is append-only within a session.
WATERMARK_FILENAME = "evolution_watermark.json"


def watermark_path(bot_dir: str | os.PathLike) -> str:
    return os.path.join(os.fspath(bot_dir), WATERMARK_FILENAME)


def load_watermark(bot_dir: str | os.PathLike) -> dict[str, Any] | None:
    """Return {"trade_count": int, "at": str} or None (first run / corrupt)."""
    path = watermark_path(bot_dir)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        count = int(data.get("trade_count", 0) or 0)
        if count <= 0:
            return None
        return {"trade_count": count, "at": str(data.get("at", ""))}
    except (json.JSONDecodeError, OSError, ValueError, TypeError):
        return None


def save_watermark(bot_dir: str | os.PathLike, trade_count: int,
                   at: str | None = None) -> None:
    """Record that an evolution run covered the first ``trade_count`` trades."""
    os.makedirs(os.fspath(bot_dir), exist_ok=True)
    if at is None:
        at = datetime.now(_TZ_TAIPEI).strftime("%Y-%m-%d %H:%M:%S")
    tmp = watermark_path(bot_dir) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"trade_count": int(trade_count), "at": at}, f)
    os.replace(tmp, watermark_path(bot_dir))


# ── Evolution cadence ──
EVOLUTION_CADENCE_DAILY = "daily"
EVOLUTION_CADENCE_WEEKLY = "weekly"


def should_run_evolution_check(cadence: str, now: datetime | None = None) -> bool:
    """Gate the fitness/improvement check by configured cadence.

    "weekly" → run only on TPE Saturday. The trading week's final
    session is Friday's night session, which closes Saturday ~05:00
    TPE, so both the auto session-end report (fires 04:58–05:00) and a
    manual stop later that Saturday morning land on Saturday. Weekday
    session ends never do. Rationale: fitness gates the composite to 0
    below MIN_TRADES (30); a single day rarely crosses that, so daily
    scoring is noise — a week of accumulated trades is the smallest
    meaningful sample.

    Anything other than "weekly" behaves as daily (always run), so a
    typo in settings.yaml degrades to the old behavior, not silence.

    ``now`` may be naive (treated as TPE wall-clock) or tz-aware.
    """
    if cadence != EVOLUTION_CADENCE_WEEKLY:
        return True
    if now is None:
        now = datetime.now(_TZ_TAIPEI)
    elif now.tzinfo is not None:
        now = now.astimezone(_TZ_TAIPEI)
    return now.weekday() == 5  # Saturday


def is_post_close_evolution_window(now: datetime | None = None) -> bool:
    """True on TPE Saturday from 05:05 onward — AFTER the week's last
    session has closed (the Friday night session ends Saturday 05:00).

    The weekly auto-pipeline runs post-close deliberately: it never
    competes with session-end force-close handling, the market is
    quiet, and the week's data is complete (the 05:05 buffer lets the
    final bar/report settle). ``now`` may be naive (TPE wall-clock)
    or tz-aware.
    """
    if now is None:
        now = datetime.now(_TZ_TAIPEI)
    elif now.tzinfo is not None:
        now = now.astimezone(_TZ_TAIPEI)
    return now.weekday() == 5 and (now.hour, now.minute) >= (5, 5)


@dataclass
class Baseline:
    """The "best composite so far" record persisted per bot.

    Stored as JSON so it survives bot restarts and crashes; the schema
    is small enough that we don't need a migration story.  Fields are
    deliberately minimal — anything richer belongs in the StrategyPool.
    """
    best_composite: float
    best_recorded_at: str   # ISO timestamp in Taipei
    n_updates: int          # how many times we've written this file

    def to_dict(self) -> dict[str, Any]:
        return {
            "best_composite": self.best_composite,
            "best_recorded_at": self.best_recorded_at,
            "n_updates": self.n_updates,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Baseline":
        return cls(
            best_composite=float(d.get("best_composite", 0.0) or 0.0),
            best_recorded_at=str(d.get("best_recorded_at", "")),
            n_updates=int(d.get("n_updates", 0) or 0),
        )


@dataclass
class ImprovementVerdict:
    """Result of comparing a fresh fitness to the persisted baseline.

    ``send_notification=True`` means the GUI should fire the Discord
    message; ``new_baseline`` is what should be written to disk after
    the send (or ``None`` if the baseline shouldn't change).
    """
    send_notification: bool
    fitness: FitnessResult
    previous_best: float
    delta: float
    reason: str                       # human-readable rationale
    new_baseline: Baseline | None     # write this if not None


def _trading_mode_to_source(trading_mode: str | None) -> str:
    """Map the GUI's trading_mode string to a fitness `source` tag.

    Both "semi_auto" and "auto" run real market fills against a real
    broker, so the trades are paper-equivalent for scoring purposes (no
    look-ahead, real slippage).  Backtest is what comes from the
    historical replay path — never the live pipeline.  When in doubt
    default to paper so we don't accidentally tag live data as backtest.
    """
    if trading_mode == "backtest":
        return SOURCE_BACKTEST
    if trading_mode in ("semi_auto", "auto"):
        return SOURCE_REAL
    return SOURCE_PAPER


def score_session_for_notification(
    trades: list[Any],
    equity_curve: list[int] | None = None,
    trading_mode: str | None = None,
) -> FitnessResult:
    """Score accumulated trades without the paper-mode restriction.

    The pool-aware :func:`src.evolution.evaluator.score_paper_session`
    refuses ``trading_mode != "paper"`` because the spec's live-promotion
    gate requires paper provenance.  For *notification*, we don't care:
    the user wants a heads-up whenever the running strategy's composite
    moves up, regardless of mode.  This wrapper bypasses the pool
    machinery, calls the math directly, and stamps the source tag based
    on the mode for downstream audit.
    """
    source = _trading_mode_to_source(trading_mode)
    return compute_fitness_from_trades(
        trades=trades,
        equity_curve=equity_curve,
        source=source,
    )


def baseline_path(bot_dir: str | os.PathLike) -> str:
    """Return the path to a bot's baseline file."""
    return os.path.join(str(bot_dir), BASELINE_FILENAME)


def load_baseline(bot_dir: str | os.PathLike) -> Baseline | None:
    """Return the persisted baseline or ``None`` on first run / corrupt file."""
    path = baseline_path(bot_dir)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return Baseline.from_dict(json.load(f))
    except (json.JSONDecodeError, OSError, ValueError):
        # Corrupt baseline shouldn't crash the report pipeline.  Treat
        # it as "first run" — the next improvement detected will rewrite
        # the file with a clean record.
        return None


def save_baseline(bot_dir: str | os.PathLike, baseline: Baseline) -> None:
    """Atomically replace the baseline file under ``bot_dir``.

    Uses a temp file + os.replace so a crash mid-write can never leave
    a corrupt baseline on disk (the old one stays valid until the new
    one is fully flushed).
    """
    os.makedirs(str(bot_dir), exist_ok=True)
    path = baseline_path(bot_dir)
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(baseline.to_dict(), f, indent=2)
        f.flush()
        try:
            os.fsync(f.fileno())
        except OSError:
            pass  # fsync isn't supported on every filesystem (e.g. some Windows shares)
    os.replace(tmp_path, path)


def detect_improvement(
    fitness: FitnessResult,
    previous: Baseline | None,
    delta: float = IMPROVEMENT_DELTA,
    now: datetime | None = None,
) -> ImprovementVerdict:
    """Compare a fresh fitness to the persisted baseline.

    Rules:
      * If ``fitness.gated`` (not enough trades) → no notification, no
        baseline update.  The fitness machinery already zeroed the
        composite so a write here would inflate the file's update count
        without signal.
      * If there's no previous baseline → notify (first-ever score is
        the new best) and write the baseline.  This is the bootstrap
        case: the user sees a first-run notification telling them what
        the baseline now is.
      * If composite − previous.best_composite > delta → notify + write.
        Use strict > so identical scores don't spam notifications
        (cheap idempotency on retries).
      * Otherwise → no-op.
    """
    if now is None:
        now = datetime.now(_TZ_TAIPEI)

    if fitness.gated:
        return ImprovementVerdict(
            send_notification=False,
            fitness=fitness,
            previous_best=previous.best_composite if previous else 0.0,
            delta=0.0,
            reason=(
                f"gated: only {fitness.total_trades} trades, "
                f"minimum is enforced by fitness module"
            ),
            new_baseline=None,
        )

    ts = now.strftime("%Y-%m-%d %H:%M:%S")

    if previous is None:
        # First run for this bot.  Notify and persist the baseline so
        # subsequent days have something to compare against.
        return ImprovementVerdict(
            send_notification=True,
            fitness=fitness,
            previous_best=0.0,
            delta=fitness.composite,
            reason="first scored session for this bot",
            new_baseline=Baseline(
                best_composite=fitness.composite,
                best_recorded_at=ts,
                n_updates=1,
            ),
        )

    improvement = fitness.composite - previous.best_composite
    if improvement > delta:
        return ImprovementVerdict(
            send_notification=True,
            fitness=fitness,
            previous_best=previous.best_composite,
            delta=improvement,
            reason=(
                f"composite {fitness.composite:.3f} − "
                f"{previous.best_composite:.3f} = {improvement:+.3f} "
                f"(> {delta:.2f} threshold)"
            ),
            new_baseline=Baseline(
                best_composite=fitness.composite,
                best_recorded_at=ts,
                n_updates=previous.n_updates + 1,
            ),
        )

    return ImprovementVerdict(
        send_notification=False,
        fitness=fitness,
        previous_best=previous.best_composite,
        delta=improvement,
        reason=(
            f"no improvement: composite {fitness.composite:.3f}, "
            f"previous best {previous.best_composite:.3f}, "
            f"delta {improvement:+.3f} (≤ {delta:.2f} threshold)"
        ),
        new_baseline=None,
    )


def check_and_notify_after_report(
    bot_dir: str | os.PathLike,
    trades: list[Any],
    equity_curve: list[int] | None,
    trading_mode: str | None,
    *,
    send_notification: Callable[[ImprovementVerdict], None] | None = None,
    delta: float = IMPROVEMENT_DELTA,
) -> ImprovementVerdict:
    """End-to-end glue: score → compare → optionally notify → persist.

    ``send_notification`` is injected so this stays decoupled from the
    Discord module — the GUI passes a callback that calls
    :meth:`DiscordNotifier.strategy_improved`.  Tests can pass a spy.

    Returns the verdict regardless of whether anything was sent, so the
    caller can log the no-improvement path too if it wants.
    """
    fitness = score_session_for_notification(
        trades=trades,
        equity_curve=equity_curve,
        trading_mode=trading_mode,
    )
    previous = load_baseline(bot_dir)
    verdict = detect_improvement(fitness, previous, delta=delta)
    if verdict.send_notification:
        # Persist BEFORE the send so a Discord failure can't cost us
        # the baseline write.  The send is fire-and-forget anyway.
        if verdict.new_baseline is not None:
            save_baseline(bot_dir, verdict.new_baseline)
        if send_notification is not None:
            try:
                send_notification(verdict)
            except Exception:
                logger.exception("Evolution Discord notification failed")
    return verdict
